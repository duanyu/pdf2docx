'''A wrapper of pdf page engine (e.g. PyMuPDF, pdfminer) to do the following work:

* extract source contents
* clean up blocks/shapes, e.g. elements out of page
* calculate page margin
* parse page structure roughly, i.e. section and column
'''

import json, fitz, re, time
from abc import (ABC, abstractmethod)
from .BasePage import BasePage
from ..layout.Section import Section
from ..layout.Column import Column
from ..shape.Shape import Hyperlink, Stroke
from ..shape.Shapes import Shapes
from ..layout.Blocks import Blocks
from ..font.Fonts import Fonts
from ..text.TextSpan import TextSpan
from ..common.share import debug_plot
from ..common import constants
from ..common.Collection import Collection
from ..image.ImageBlock import ImageBlock
from ..common.share import TextDirection, is_toc_dots
from importlib import resources
from fontTools.ttLib import TTFont
import base64
import math
import cv2
import numpy as np
import re
import unicodedata

MATH_FONTS_PREFIX = ("CMR", "CMEX", "CMMI", "CMSY", "CMBX", "MSBM", "LMMath")
MATH_FONTS_EXACT = {"txmiaX", "txsys", "txexs", "StandardSymL"}
MATH_FONT_HINTS = ("MathMI", "ReguItal")

EQ_NUM_RE = re.compile(r"^\.?\s*\([\d.]+\)$")

# 常见数学关系/运算符（可按你的数据补充）
MATH_OP_RE = re.compile(r"[=<>±×÷∑∫√∞≈≠≤≥→←↔∈∉∩∪·•]")

# 纯英文长单词（自然语言特征）
LONG_WORD_RE = re.compile(r"^[a-z]{3,}$")

# 常见“函数名”可以保留，但不要单独当成公式证据
MATH_FUNC_WORDS = {"sin", "cos", "tan", "log", "ln", "exp", "min", "max", "lim", "const"}


def is_possible_stamp(rect):

    w = rect.width
    h = rect.height

    # 基础过滤
    if w <= 0 or h <= 0:
        return False

    # 面积
    area = w * h

    # 长宽比
    ratio = max(w, h) / min(w, h)

    # 1. 面积不能太大
    if area > 20000:
        return False

    # 2. 面积不能太小
    if area < 200:
        return False

    # 3. 印章通常接近正方形
    if ratio > 2.5:
        return False

    # 4. 最大边限制
    if max(w, h) > 180:
        return False

    return True

def has_red_seal(
    image_base64: str,
    red_ratio_thresh: float = 0.005,
    min_seal_radius: int = 30,
    circularity_thresh: float = 0.35,
    max_side: int = 800,
) -> bool:
    """
    检测图片中是否存在红色印章。

    判定逻辑（同时满足）：
      ① 红色像素占比 ≥ red_ratio_thresh
      ② 存在至少一个红色连通区域：面积 ≥ π·min_seal_radius²  且  圆度 ≥ circularity_thresh

    Parameters
    ----------
    image_base64 : str
        图片的 Base64 字符串，支持带 data-URI 前缀。
    red_ratio_thresh : float
        红色像素占总像素的最低比例（默认 0.5%）。
    min_seal_radius : int
        最小印章半径（缩放后的像素值，默认 30px）。
    circularity_thresh : float
        最低圆度，范围 0~1，1 为完美圆（默认 0.35，容忍文字/缺损）。
    max_side : int
        处理前将最长边缩放到此值（默认 800），越小越快。

    Returns
    -------
    bool
        True = 检测到红色印章
    """
    # ── 0. 去掉可能的 data-URI 前缀 ──
    if "," in image_base64[:64]:
        image_base64 = image_base64.split(",", 1)[1]

    # ── 1. 解码 ──
    img_bytes = base64.b64decode(image_base64)
    buf = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    del img_bytes, buf                         # 立即释放原始缓冲
    if img is None:
        return False

    # ── 2. 缩放（加速 + 省内存） ──
    h, w = img.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        img = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_AREA)

    # ── 3. BGR → HSV，提取红色掩膜 ──
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    del img                                    # 不再需要原图

    # 红色在 H 通道分布于两端：[0,10] ∪ [170,180]
    lo1, hi1 = np.array([0,   70, 50], np.uint8), np.array([10,  255, 255], np.uint8)
    lo2, hi2 = np.array([170, 70, 50], np.uint8), np.array([180, 255, 255], np.uint8)
    red_mask = cv2.inRange(hsv, lo1, hi1)
    red_mask |= cv2.inRange(hsv, lo2, hi2)     # 原地或运算，省一次分配
    del hsv

    # ── 4. 快速剪枝：红色占比不足 → 直接返回 ──
    red_pixels = cv2.countNonZero(red_mask)
    #  print('red pixel ratio:', red_pixels / red_mask.size)
    if red_pixels / red_mask.size < red_ratio_thresh:
        return False

    # ── 5. 形态学去噪 ──
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kern, iterations=2)
    red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN,  kern, iterations=1)

    # ── 6. 轮廓分析：找"大圆" ──
    contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    del red_mask

    min_area = math.pi * min_seal_radius * min_seal_radius
    # print('min_area:', min_area)

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:                    # 面积太小，跳过
            continue
        peri = cv2.arcLength(cnt, True)
        if peri == 0:
            continue
        circularity = 4.0 * math.pi * area / (peri * peri)
        # print('circularity:', circularity)
        if circularity >= circularity_thresh:  # 够圆 → 判定为印章
            return True

    return False

# simsun_path = str(resources.files(root_pkg).joinpath("fonts/simsun.ttc"))
# SIMSUN_FONT_OBJ = fitz.Font(fontname="SimSun", fontfile=simsun_path)
# simsun_line_height_ratio = Fonts.get_line_height_factor(TTFont(simsun_path, fontNumber=0))
simsun_line_height_ratio = 1.3
# simhei_path = str(resources.files(root_pkg).joinpath("fonts/SimHei.ttf"))
# SIMHEI_FONT_OBJ = fitz.Font(fontname="SimHei", fontfile=simhei_path)
# simhei_line_height_ratio = Fonts.get_line_height_factor(TTFont(simhei_path))
simhei_line_height_ratio = 1.3
# roman_path = str(resources.files(root_pkg).joinpath("fonts/Times New Roman/times new roman.ttf"))
# times_new_roman_line_height_ratio = Fonts.get_line_height_factor(TTFont(roman_path))
times_new_roman_line_height_ratio = 1.115
fangsong_line_height_ratio = 1.3
dengxian_line_height_ratio = 1.432
arial_line_height_ratio = 1.15

center_fuzzy_ratio = 0.1  # 距离中线多少比例认为是双栏（允许的误差范围）

class RawPage(BasePage, ABC):
    '''A wrapper of page engine.'''

    def __init__(self, page_engine=None):
        ''' Initialize page layout.

        Args:
            page_engine (Object): Source pdf page.
        '''
        BasePage.__init__(self)
        self.page_engine = page_engine
        self.blocks = Blocks(parent=self)
        self.shapes = Shapes(parent=self)


    @abstractmethod
    def extract_raw_dict(self, **settings):
        '''Extract source data with page engine. Return a dict with the following structure:
        ```
            {
                "width" : w,
                "height": h,
                "blocks": [{...}, {...}, ...],
                "shapes" : [{...}, {...}, ...]
            }
        ```
        '''


    @property
    def text(self):
        '''All extracted text in this page, with images considered as ``<image>``.
        Should be run after ``restore()`` data.'''
        return '\n'.join([block.text for block in self.blocks])

    @property
    def raw_text(self):
        '''Extracted raw text in current page. Should be run after ``restore()`` data.'''
        return '\n'.join([block.raw_text for block in self.blocks])


    @debug_plot('Source Text Blocks')
    def restore(self, **settings):
        '''Initialize layout extracted with ``PyMuPDF``.'''
        raw_dict = self.extract_raw_dict(**settings)
        self.blocks.restore(raw_dict.get('blocks', []))
        self.shapes.restore(raw_dict.get('shapes', []))
        return self.blocks


    @debug_plot('Cleaned Shapes')
    def clean_up(self, **settings):
        '''Clean up raw blocks and shapes, e.g.

        * remove negative or duplicated instances,
        * detect semantic type of shapes
        '''
        # clean up blocks first
        # print('before merge:')
        # print(len(self.blocks))
        # for b in self.blocks:
        #     print(b, b.text, b.store()['type'])
        if settings.get('merge_image_with_overlap_lines', False):
            self.merge_image_with_overlap_lines(overlap_ratio=settings.get('merge_image_overlap_ratio', 0.1), intersect_threshold=0)
        # print('after merge:')
        # print(len(self.blocks))
        # for b in self.blocks:
        #     print(b, b.text, b.store()['type'])

        if settings.get('complex_equation_to_image', False):
            self.complex_equation_to_image()
        # print('after equation to image:')
        # print(len(self.blocks))
        # for b in self.blocks:
        #     print(b, b.text, b.store()['type'])

        self.blocks.clean_up(
            settings['float_image_ignorable_gap'],
            settings['line_overlap_threshold'],
            settings.get('parse_arxiv_markup', False),
            settings.get('single_group_as_float', False))
        # clean up shapes
        self.shapes.clean_up(
            settings['max_border_width'],
            settings['shape_min_dimension'])
        return self.shapes

    def complex_equation_to_image(self):
        """
        把相邻的 equation block 先合并为 group（list of block）。
        如果某个 group 中任意一个 block 包含复杂公式，则对整个 group 截图，
        并将该 group 替换为一个 image block。
        """

        def is_math_symbol_char(ch: str) -> bool:
            # Sm: Symbol, Math
            return unicodedata.category(ch) == "Sm" or ch in "αβγδεζηθικλμνξοπρστυφχψω"

        def is_equation(block, ratio_th=0.5, min_total_chars=1):
            store = block.store()
            if store.get("type") != 0:
                return False

            total = 0
            math_like = 0

            long_word_cnt = 0
            word_cnt = 0

            for line in store.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip().lower()
                    if not text:
                        continue

                    # 编号/函数词 直接不参与统计
                    if EQ_NUM_RE.match(text) or text in MATH_FUNC_WORDS:
                        continue

                    font = span.get("font", "")

                    # 大型运算符，直接设定为equation
                    if font.startswith('CMEX') or '∑︁' in text or '∑' in text:
                        return True

                    total += len(text)

                    # 2) 自然语言信号：长英文单词
                    for tok in re.split(r"\s+", text):
                        if not tok:
                            continue
                        if tok.isalpha():
                            word_cnt += 1  # 英文word计数
                            if len(tok) >= 3 and tok not in MATH_FUNC_WORDS:
                                long_word_cnt += 1  # 长英文word计数

                    # 3) 字体命中（主要逻辑）
                    font_hit = (
                            font.startswith(MATH_FONTS_PREFIX)
                            or any(h in font for h in MATH_FONT_HINTS)
                            or font in MATH_FONTS_EXACT
                    )

                    # 4) 字符级数学符号命中（补充字体逻辑）
                    sym_hit_chars = sum(1 for ch in text if is_math_symbol_char(ch))

                    if font_hit:
                        math_like += len(text)
                    elif sym_hit_chars:
                        math_like += sym_hit_chars

            if total < min_total_chars:
                return False

            ratio = math_like / total

            # 排除：自然语言特征太强（长词占比高）
            if word_cnt > 0 and (long_word_cnt / word_cnt) >= 0.3:
                return False

            return ratio >= ratio_th

        def is_simple_left_to_right(
                blocks_in_group: list,
                vertical_overlap_threshold: float = 0.3
        ) -> bool:
            """
            判断一个文本块组（block group）是否为简单的从左到右排列。

            这个函数通过检查相邻文本块之间的垂直重叠来工作。如果任何一对
            从左到右排序的相邻块没有足够的垂直重叠，则认为它是一个复杂结构
            （如分数、带上下限的运算符等）。

            Args:
                blocks_in_group (List[block]): 一个包含 block 对象的列表。
                    每个 block 对象必须有一个 .store().get("bbox") 方法来返回其边界框。
                    边界框格式为 [x0, y0, x1, y1]。
                vertical_overlap_threshold (float): 垂直重叠的阈值。
                    如果两个相邻块的垂直重叠高度小于它们中较小那个高度的这个比例，
                    则认为它们是堆叠的。默认值为 0.3 (30%)。

            Returns:
                bool: 如果是简单的从左到右排列，返回 True；否则返回 False。
            """
            # 1. 处理边界情况：0或1个块一定是简单的
            bboxes = []
            for block in blocks_in_group:
                store = block.store()
                if store.get("type") != 0:
                    continue
                for line in store.get("lines", []):
                    for span in line.get("spans", []):
                        bbox = span.get("bbox")
                        if bbox and bbox[3] > bbox[1] and bbox[2] > bbox[0]:  # 确保bbox有效
                            bboxes.append(bbox)

            if len(bboxes) < 2:
                return True

            # 3. 根据水平起始位置 (x0) 对边界框进行排序
            sorted_bboxes = sorted(bboxes, key=lambda b: b[0])

            # 4. 遍历排序后的边界框，逐对比较相邻的两个
            for i in range(len(sorted_bboxes) - 1):
                bbox1 = sorted_bboxes[i]
                bbox2 = sorted_bboxes[i + 1]

                # 提取Y坐标和高度
                y0_1, y1_1 = bbox1[1], bbox1[3]
                y0_2, y1_2 = bbox2[1], bbox2[3]
                height1 = y1_1 - y0_1
                height2 = y1_2 - y0_2

                # 5. 计算垂直重叠的高度
                # 重叠区域的顶端是两个框顶端的较大值
                # 重叠区域的底端是两个框底端的较小值
                overlap_y_start = max(y0_1, y0_2)
                overlap_y_end = min(y1_1, y1_2)

                overlap_height = max(0, overlap_y_end - overlap_y_start)

                # 确定用于比较的最小高度
                min_height = min(height1, height2)

                # 6. 判断重叠是否足够
                # 如果重叠高度小于最小高度的阈值比例，则认为它们是堆叠的（复杂结构）
                if overlap_height < min_height * vertical_overlap_threshold:
                    # 打印调试信息（可选）
                    # print(f"复杂结构嫌疑: {bbox1} 和 {bbox2} 垂直重叠不足。")
                    # print(f"重叠高度: {overlap_height}, 最小高度: {min_height}, 阈值: {min_height * vertical_overlap_threshold}")
                    return False

            # 如果所有相邻对都通过了检查，则该组是简单的
            return True

        # 新增辅助函数：判断 group 是否与后一个 block 在同一行；但允许是序号
        def is_inline_with_next(group_end_idx, group_y0, group_y1):
            if group_end_idx >= len(original_blocks) - 1:
                return False
            next_block = original_blocks[group_end_idx + 1]
            next_bbox = next_block.store().get("bbox")
            if not next_bbox or len(next_bbox) != 4:
                return False

            if isinstance(next_block.text, str) and EQ_NUM_RE.match(next_block.text.strip()):
                return False

            next_y_center = (next_bbox[1] + next_bbox[3]) / 2
            group_y_center = (group_y0 + group_y1) / 2
            return (group_y0 <= next_y_center <= group_y1) or (next_bbox[1] <= group_y_center <= next_bbox[3])

        def is_inline_with_prev(group_start_idx, group_y0, group_y1):
            """
            判断 group 是否与前一个 block 在同一行（inline 公式）。
            通过检查两者的垂直中心点是否互相落在对方的 y 范围内来判断。
            """
            if group_start_idx == 0:
                return False

            prev_block = original_blocks[group_start_idx - 1]
            prev_bbox = prev_block.store().get("bbox")
            if not prev_bbox or len(prev_bbox) != 4:
                return False

            prev_y_center = (prev_bbox[1] + prev_bbox[3]) / 2
            group_y_center = (group_y0 + group_y1) / 2

            # 前一个 block 的垂直中心在 group 的 y 范围内，
            # 或 group 的垂直中心在前一个 block 的 y 范围内
            return (group_y0 <= prev_y_center <= group_y1) or (prev_bbox[1] <= group_y_center <= prev_bbox[3])

        # 先转成普通 list，便于按索引分组和重建
        original_blocks = list(self.blocks)
        if not original_blocks:
            return

        # ======== Phase 1: 将相邻的 equation block 合并为 group ========
        equation_groups = []
        current_group = []
        current_start = None

        for idx, block in enumerate(original_blocks):
            if is_equation(block):
                if not current_group:
                    current_start = idx
                current_group.append(block)
            elif current_group:
                equation_groups.append({
                    "start": current_start,
                    "end": idx - 1,
                    "blocks": current_group
                })
                current_group = []
                current_start = None

        if current_group:
            equation_groups.append({
                "start": current_start,
                "end": len(original_blocks) - 1,
                "blocks": current_group
            })

        if not equation_groups:
            return

        # ======== Phase 2: 对包含复杂公式的 group 截图并转为 image block ========
        group_to_image = {}

        for group in equation_groups:
            blocks_in_group = group["blocks"]

            # 注释掉下面的代码意味着：所有的interline equation都转为image
            # group 中有复杂公式，才进行merge
            if is_simple_left_to_right(blocks_in_group):
                continue

            group_bboxes = []
            for block in blocks_in_group:
                bbox = block.store().get("bbox")
                if bbox and len(bbox) == 4:
                    group_bboxes.append(bbox)

            if not group_bboxes:
                continue

            x0 = min(bbox[0] for bbox in group_bboxes)
            y0 = min(bbox[1] for bbox in group_bboxes)
            x1 = max(bbox[2] for bbox in group_bboxes)
            y1 = max(bbox[3] for bbox in group_bboxes)

            # ---- inline 公式直接跳过，不截图 ----
            if is_inline_with_prev(group["start"], y0, y1) or is_inline_with_next(group["end"], y0, y1):
                continue

            # ---- y0 纠正：仅对 interline 公式 ----
            group_start_idx = group["start"]
            if group_start_idx > 0:
                prev_block = original_blocks[group_start_idx - 1]
                prev_bbox = prev_block.store().get("bbox")
                if prev_bbox and len(prev_bbox) == 4:
                    prev_y1 = prev_bbox[3]
                    if y0 < prev_y1:
                        y0 = prev_y1

            expanded_bbox = (x0, y0, x1, y1)
            rect = fitz.Rect(*expanded_bbox)

            try:
                pix = self.page_engine.get_pixmap(clip=rect, dpi=200)
                img_bytes = pix.tobytes("png")
                image_base64 = base64.b64encode(img_bytes).decode("ascii")
            except Exception as e:
                print(e)
                continue

            new_image_block = {
                "type": 1,
                "bbox": expanded_bbox,
                "image": image_base64,
                "width": expanded_bbox[2] - expanded_bbox[0],
                "height": expanded_bbox[3] - expanded_bbox[1],
            }

            group_to_image[group["start"]] = {
                "end": group["end"],
                "image_block": new_image_block
            }

        if not group_to_image:
            return

        # ======== Phase 3: 重建 blocks 列表，保持原有顺序 ========
        final_blocks = Blocks(parent=self)
        i = 0
        n = len(original_blocks)

        while i < n:
            if i in group_to_image:
                replacement = group_to_image[i]
                final_blocks.append(ImageBlock(replacement["image_block"]).to_text_block())
                i = replacement["end"] + 1
            else:
                final_blocks.append(original_blocks[i])
                i += 1

        self.blocks = final_blocks

    def merge_image_with_overlap_lines(self, overlap_ratio=0.95, intersect_threshold=0):
        """
        intersect_threshold 允许不严格重叠【为了合并图表的y轴】
        1. 对所有 image block，用 Union-Find 将有交叉的合并为一个大的 image bbox
        2. 将与合并后 image bbox 有足够重叠的 text block 吸收进来（迭代扩大 bbox）
           同时迭代检查组间是否因扩大而产生交叉，若有则合并组
        3. 生成新的 image block，替换被合并的旧 blocks
        """

        def bbox_area(b):
            return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

        def intersect(b1, b2, threshold=0):
            x0 = max(b1[0], b2[0])
            y0 = max(b1[1], b2[1])
            x1 = min(b1[2], b2[2])
            y1 = min(b1[3], b2[3])

            gap_x = x0 - x1
            gap_y = y0 - y1

            if gap_x > threshold or gap_y > threshold:
                return None

            x0 = min(x0, x1)
            y0 = min(y0, y1)
            x1 = max(x1, x0 + 1)
            y1 = max(y1, y0 + 1)

            return (x0, y0, x1, y1)

        def union_bbox(b1, b2):
            return (min(b1[0], b2[0]), min(b1[1], b2[1]),
                    max(b1[2], b2[2]), max(b1[3], b2[3]))

        # ---- Union-Find ----
        uf_parent = {}

        def find(x):
            while uf_parent[x] != x:
                uf_parent[x] = uf_parent[uf_parent[x]]
                x = uf_parent[x]
            return x

        def uf_union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                uf_parent[ra] = rb

        # ======== Phase 1: 合并所有相交的 image blocks ========
        image_blocks = [b for b in self.blocks if b.store().get("type") == 1]

        if not image_blocks:
            # 没有image的直接不需要走后续的逻辑
            return

        for b in image_blocks:
            uf_parent[id(b)] = id(b)

        # 两两检查交叉，有交叉就 union
        for i in range(len(image_blocks)):
            for j in range(i + 1, len(image_blocks)):
                if intersect(image_blocks[i].bbox, image_blocks[j].bbox, threshold=intersect_threshold):
                    uf_union(id(image_blocks[i]), id(image_blocks[j]))

        # 按 root 分组，计算每组的合并 bbox
        from collections import defaultdict
        groups = defaultdict(list)
        for b in image_blocks:
            groups[find(id(b))].append(b)

        group_bboxes = {}
        for root, members in groups.items():
            merged = members[0].bbox
            for m in members[1:]:
                merged = union_bbox(merged, m.bbox)
            group_bboxes[root] = merged

        # ======== Phase 2: 迭代吸收 text blocks + 合并重叠的组 ========
        # ★ 修复点3: 排除已分组的 image blocks，组间合并由 Sub-step A 处理
        image_block_set = set(image_blocks)
        text_blocks = [b for b in self.blocks if b not in image_block_set]

        removed_text_blocks = set()
        group_text_members = defaultdict(list)

        # 迭代：合并 text block 会扩大 bbox，可能覆盖更多 text block
        changed = True
        while changed:
            changed = False

            # ★ 修复点1 (Sub-step A): 检查任意两组是否因 bbox 扩大而重叠，有则合并
            roots = list(group_bboxes.keys())
            for i in range(len(roots)):
                if changed:
                    break
                for j in range(i + 1, len(roots)):
                    ri, rj = roots[i], roots[j]
                    if ri not in group_bboxes or rj not in group_bboxes:
                        continue
                    if intersect(group_bboxes[ri], group_bboxes[rj], threshold=intersect_threshold):
                        # 合并 rj 到 ri
                        group_bboxes[ri] = union_bbox(group_bboxes[ri], group_bboxes[rj])
                        groups[ri].extend(groups.pop(rj))
                        del group_bboxes[rj]
                        if rj in group_text_members:
                            group_text_members[ri].extend(group_text_members.pop(rj))
                        changed = True
                        break

            if changed:
                continue  # 组合并后 bbox 再次扩大，需要重头检查

            # Sub-step B: 吸收与组 bbox 有足够重叠的 text blocks
            for tb in text_blocks:
                if tb in removed_text_blocks:
                    continue
                tb_bbox = tb.store()["bbox"]
                for root in list(group_bboxes.keys()):
                    inter = intersect(group_bboxes[root], tb_bbox, threshold=intersect_threshold)
                    if not inter:
                        continue
                    real_ratio = bbox_area(inter) / (bbox_area(tb_bbox) + 1e-6)
                    # print(tb.text, real_ratio)
                    if real_ratio >= overlap_ratio:
                        span_inter_list = []  # 处理L形状的问题(block.bbox太大）
                        for line in tb.lines:
                            for span in line.spans:
                                if intersect(group_bboxes[root], span.bbox, threshold=intersect_threshold):
                                    span_inter_list.append(True)
                                else:
                                    span_inter_list.append(False)
                        if any(span_inter_list):
                            # span level并没有任何inter，那么不是真的intersect
                            group_bboxes[root] = union_bbox(group_bboxes[root], tb_bbox)
                            removed_text_blocks.add(tb)
                            group_text_members[root].append(tb)
                            changed = True
                            break  # bbox 已变化，重新开始迭代
                # ★ 修复点2: 外层循环也要 break，确保立即回到 while 重新检查组间重叠
                if changed:
                    break

        # ======== Phase 3: 为发生合并的组生成新的 image block ========
        new_blocks = []
        all_removed = set()

        for root, members in groups.items():
            has_multiple_images = len(members) > 1
            has_text_merges = len(group_text_members.get(root, [])) > 0

            if not has_multiple_images and not has_text_merges:
                continue  # 单个 image 且没有吸收 text，保持原样

            merged_bbox = group_bboxes[root]
            # y轴不增加（不可控）
            merged_bbox = (merged_bbox[0], merged_bbox[1], merged_bbox[2], merged_bbox[3])
            rect = fitz.Rect(*merged_bbox)

            try:
                # 避免报错
                pix = self.page_engine.get_pixmap(clip=rect, dpi=200)

                cs = pix.colorspace
                if cs is None or cs not in (fitz.csGRAY, fitz.csRGB):
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                # 去 alpha
                if pix.alpha:
                    pix = fitz.Pixmap(pix, 0)

                # 导出 png
                img_bytes = pix.tobytes("png")
                image_base64 = base64.b64encode(img_bytes).decode("ascii")
            except Exception as e:
                print(e)
                continue

            # 用图片大小来明显降低误判
            if has_red_seal(image_base64, circularity_thresh=0.6) and is_possible_stamp(rect):
                # print("red seal skipped!", merged_bbox)
                continue

            new_image_block = {
                "type": 1,
                "bbox": merged_bbox,
                "image": image_base64,
                "width": merged_bbox[2] - merged_bbox[0],
                "height": merged_bbox[3] - merged_bbox[1],
            }
            new_blocks.append(new_image_block)

            # 标记该组所有原始 block 为待移除
            for m in members:
                all_removed.add(m)
            for tb in group_text_members.get(root, []):
                all_removed.add(tb)

        # ======== Phase 4: 重建 blocks 列表 ========
        if all_removed and new_blocks:
            final_blocks = Blocks(parent=self)

            for block in self.blocks:
                if block not in all_removed:
                    final_blocks.append(block)

            for b in new_blocks:
                final_blocks.append(ImageBlock(b).to_text_block())

            self.blocks = final_blocks

    def process_font(self, fonts:Fonts):
        '''Update font properties, e.g. font name, font line height ratio, of ``TextSpan``.

        Args:
            fonts (Fonts): Fonts parsed by ``fonttools``.
        '''

        def has_chinese(text):
            return bool(re.search(r'[\u4e00-\u9fff]', text))

        # get all text span
        spans = []
        for line in self.blocks:
            spans.extend([span for span in line.spans if isinstance(span, TextSpan)])

        # check and update font name, line height
        for span in spans:
            font = fonts.get(span.font)
            if not font:
                # 没有font的，给个默认字体
                # 对于中文用宋体，其他用times new roman
                if has_chinese(span.text):
                    span.font = 'SimSun'
                    span.line_height = simsun_line_height_ratio * span.size
                else:
                    span.font = 'Times New Roman'
                    span.line_height = times_new_roman_line_height_ratio * span.size
            else:
                # 对font提取结果也要做个处理（很多解析出来的name，无法直接用在word/wps中，这会导致默认字体的排版不符合要求）
                # update font properties with font parsed by fonttools
                extracted_font_name = font.name
                lower_extracted_font_name = extracted_font_name.lower()

                if extracted_font_name in ["Microsoft YaHei", "Calibri", "Arial"]:
                    span.font = extracted_font_name
                    if font.line_height:
                        span.line_height = font.line_height * span.size
                elif 'arial' in lower_extracted_font_name:
                    # Arial
                    span.font = "Arial"
                    span.line_height = arial_line_height_ratio * span.size
                elif 'dengxian' in lower_extracted_font_name:
                    # 等线
                    span.font = "DengXian"
                    span.line_height = dengxian_line_height_ratio * span.size
                elif 'fangsong' in lower_extracted_font_name or 'fzfs' in lower_extracted_font_name:
                    # 仿宋；wps的行距会偏大
                    span.font = "FangSong"
                    span.line_height = fangsong_line_height_ratio * span.size
                elif 'song' in lower_extracted_font_name or 'simsun' in lower_extracted_font_name or 'st' in lower_extracted_font_name:
                    # 宋体
                    span.font = "SimSun"
                    span.line_height = simsun_line_height_ratio * span.size
                elif 'times' in lower_extracted_font_name:
                    # times new roman
                    span.font = 'Times New Roman'
                    span.line_height = times_new_roman_line_height_ratio * span.size
                elif 'hei' in lower_extracted_font_name or 'fzht' in lower_extracted_font_name:
                    # 黑体
                    span.font = "SimHei"
                    span.line_height = simhei_line_height_ratio * span.size
                elif 'kai' in lower_extracted_font_name or 'fzkt' in lower_extracted_font_name:
                    # 楷体
                    span.font = "KaiTi"
                    span.line_height = 1.3 * span.size
                else:
                    if has_chinese(span.text):
                        span.font = 'SimSun'
                        span.line_height = simsun_line_height_ratio * span.size
                    else:
                        span.font = 'Times New Roman'
                        span.line_height = times_new_roman_line_height_ratio * span.size

    def calculate_margin(self, **settings):
        """Calculate page margin.

        .. note::
            Ensure this method is run right after cleaning up the layout, so the page margin is
            calculated based on valid layout, and stay constant.
        """
        # Exclude hyperlink from shapes because hyperlink might exist out of page unreasonably,
        # while it should always within page since attached to text.
        shapes = Shapes([shape for shape in self.shapes if not isinstance(shape, Hyperlink)])

        # return default margin if no blocks exist
        if not self.blocks and not shapes: return (constants.ITP, ) * 4

        x0, y0, x1, y1 = self.bbox
        u0, v0, u1, v1 = self.blocks.bbox | shapes.bbox

        # margin
        left = max(u0-x0, 0.0)
        right = max(x1-u1-constants.MINOR_DIST, 0.0)
        top = max(v0-y0, 0.0)
        bottom = max(y1-v1, 0.0)

        # reduce calculated top/bottom margin to leave some free space
        top *= settings['page_margin_factor_top']
        left *= settings['page_margin_factor_left']
        bottom *= settings['page_margin_factor_bottom']
        right *= settings['page_margin_factor_right']

        # use normal margin if calculated margin is large enough
        return (
            min(constants.ITP, round(left, 1)),
            min(constants.ITP, round(right, 1)),
            min(constants.ITP, round(top, 1)),
            min(constants.ITP, round(bottom, 1)))


    def parse_section(self, **settings):
        '''Detect and create page sections.

        .. note::
            - Only two-columns Sections are considered for now.
            - Page margin must be parsed before this step.
        '''
        # bbox
        X0, Y0, X1, _ = self.working_bbox

        # collect all blocks (line level) and shapes
        elements = Collection()
        elements.extend(self.blocks)
        elements.extend(self.shapes)
        if not elements: return

        # to create section with collected lines
        lines = Collection()
        sections = []
        def close_section(num_col, elements, y_ref):
            # append to last section if both single column
            if sections and sections[-1].num_cols==num_col==1:
                column = sections[-1][0] # type: Column
                column.union_bbox(elements)
                column.add_elements(elements)
            # otherwise, create new section
            else:
                section = self._create_section(num_col, elements, (X0, X1), y_ref)
                if section:
                    sections.append(section)

        # check section row by row
        pre_num_col = 1
        y_ref = Y0  # to calculate v-distance between sections
        for row in elements.group_by_rows():
            # check column col by col
            cols = row.group_by_columns()
            current_num_col = len(cols)

            # print('current_num_col:', current_num_col)
            # for col_i, col in enumerate(cols):
            #     for _col_i, _col in enumerate(col.store()):
            #         for _span_i, _span in enumerate(_col.get('spans', {})):
            #             print(f"col-{col_i}-{_col_i}, span-{_span_i}: {_span.get('text')}: {_span}")
            # print()

            # 修正cols；如果有两个col的中间位置大概在页面中间，这说明是双栏，把中间左边的、中间右边的分为两栏；只能对比不同col的内容
            if current_num_col == 2:
                # 2 col也需要判断是否符合要求
                split_col_i = -1
                center_col_margin = 0.0
                col_margin_list = []
                has_wide_col = any([((col.bbox[2] - col.bbox[0]) / X1) > 0.3 for col in cols])  # 是否有一个col很宽（很可能是正文）
                # 判断是否为双栏布局（col的中点在页面中间）
                for col_i in range(len(cols) - 1):
                    next_x0 = cols[col_i + 1].bbox[0]
                    this_x1 = cols[col_i].bbox[2]
                    col_margin_list.append(next_x0 - this_x1)
                    page_center = (X1 + X0) / 2
                    this_next_col_center_ratio = ((this_x1 + next_x0) / 2) / page_center
                    if (1 - center_fuzzy_ratio) <= this_next_col_center_ratio <= (
                            1 + center_fuzzy_ratio) and next_x0 > this_x1 and split_col_i < 0:
                        split_col_i = col_i
                        center_col_margin = next_x0 - this_x1
                if split_col_i >= 0 and has_wide_col and center_col_margin > 3:
                    # 符合要求，确实为双栏
                    pass
                # Abstract的特殊逻辑
                elif has_wide_col and hasattr(cols[0][0], "text") and cols[0][0].text.strip() == 'Abstract':
                    pass
                else:
                    # 否则为单栏
                    current_num_col = 1
            elif current_num_col > 2:
                # print('current_num_col before:', current_num_col)
                flat_cols = []
                for col_index, col_list in enumerate(cols):
                    for col in col_list:
                        flat_cols.append(col)

                split_col_i = -1
                center_col_margin = 0.0
                col_margin_list = []
                has_wide_col = any([((col.bbox[2]-col.bbox[0])/X1) > 0.3 for col in cols]) # 是否有一个col很宽（很可能是正文）
                # 判断是否为双栏布局（col的中点在页面中间）
                for col_i in range(len(cols) - 1):
                    next_x0 = cols[col_i + 1].bbox[0]
                    this_x1 = cols[col_i].bbox[2]
                    col_margin_list.append(next_x0-this_x1)
                    page_center = (X1 + X0) / 2
                    this_next_col_center_ratio = ((this_x1 + next_x0) / 2) / page_center
                    if (1 - center_fuzzy_ratio) <= this_next_col_center_ratio <= (
                            1 + center_fuzzy_ratio) and next_x0 > this_x1 and split_col_i < 0:
                        split_col_i = col_i
                        center_col_margin = next_x0-this_x1
                if split_col_i >= 0 and has_wide_col and center_col_margin > 3:
                    col1 = Collection()
                    col2 = Collection()
                    for col_i in range(len(cols)):
                        if col_i <= split_col_i:
                            col1.extend(cols[col_i])
                        else:
                            col2.extend(cols[col_i])

                    cols = [col1, col2]
                    current_num_col = 2

                # 额外处理左侧为小节标题的情况（中点不在中间）；但要filter目录的情况
                if current_num_col > 2 and not any([is_toc_dots(col[0].text) for col in cols if hasattr(col[0], 'text')]):
                    if has_wide_col and hasattr(cols[0][0], "text") and len(cols[0][0].text.strip()) > 0 and \
                            cols[0][0].text.strip()[0] in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
                        first_col = Collection()
                        second_col = Collection()
                        for col in flat_cols:
                            if col.bbox[0] < 0.5 * X1:
                                # 左侧
                                first_col.append(col)
                            else:
                                second_col.append(col)
                        cols = [first_col, second_col]
                        current_num_col = 2

                    # print('current num col仍然大于2！')
                    # print(cols[0][0].store())
                    # print(cols[0][0].text)

                # print('current_num_col after:', current_num_col)

                # print('【修正后】')
                # print('current_num_col:', current_num_col)
                # for col_i, col in enumerate(cols):
                #     for _col_i, _col in enumerate(col.store()):
                #         for _span_i, _span in enumerate(_col.get('spans', {})):
                #             print(f"col-{col_i}-{_col_i}, span-{_span_i}: {_span.get('text')}: {_col}")
                # print()

            # column check:
            # consider 2-cols only
            if current_num_col > 2:
                current_num_col = 1

            # the width of two columns shouldn't have significant difference
            # 避免table被误判
            elif current_num_col == 2:
                u0, v0, u1, v1 = cols[0].bbox
                m0, n0, m1, n1 = cols[1].bbox
                x0 = (u1 + m0) / 2.0
                c1, c2 = x0 - X0, X1 - x0  # column width
                w1, w2 = u1 - u0, m1 - m0  # line width
                f = 2.0
                if not 1/f<=c1/c2<=f or w1/c1<0.33 or w2/c2<0.33:
                    if hasattr(cols[0][0], "text") and len(cols[0][0].text.strip()) > 0 and (cols[0][0].text.strip()[0] in ['1', '2', '3', '4', '5', '6', '7', '8', '9'] or cols[0][0].text.strip() in ['Abstract', 'Acknowledgements', 'References']):
                        # 小节标题、Abstract忽略
                        pass
                    elif hasattr(cols[1][0], "text") and len(cols[1][0].text.strip()) > 0 and cols[1][0].text.strip()[0] in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
                        pass
                    else:
                        current_num_col = 1

            # process exceptions
            if pre_num_col == 2 and current_num_col == 1:
                # though current row has one single column, it might have another virtual
                # and empty column. If so, it should be counted as 2-cols
                cols = lines.group_by_columns()
                pos = cols[0].bbox[2]
                if row.bbox[2] <= pos or row.bbox[0] > pos:
                    current_num_col = 2

                # pre_num_col!=current_num_col => to close section with collected lines,
                # before that, further check the height of collected lines
                else:
                    x0, y0, x1, y1 = lines.bbox
                    if y1 - y0 < settings['min_section_height']:
                        pre_num_col = 1


            elif pre_num_col == 2 and current_num_col == 2:
                # though both 2-cols, they don't align with each other
                combine = Collection(lines)
                combine.extend(row)
                if len(combine.group_by_columns(sorted=False)) == 1: current_num_col = 1

            # finalize pre-section if different from the column count of previous section
            if current_num_col != pre_num_col:
                # process pre-section
                close_section(pre_num_col, lines, y_ref)
                if sections:
                    y_ref = sections[-1][-1].bbox[3]

                # start potential new section
                lines = Collection(row)
                pre_num_col = current_num_col

            # otherwise, collect current lines for further processing
            else:
                lines.extend(row)

        # don't forget the final section
        close_section(current_num_col, lines, y_ref)

        # print('sections:')
        # for sec_i, section in enumerate(sections):
        #     for col_i, col in enumerate(section.store()['columns']):
        #         for block_i, block in enumerate(col.get('blocks', [])):
        #             for span_i, span in enumerate(block.get('spans', [])):
        #                 print(f"sec-{sec_i}, col-{col_i}, block-{block_i}, span-{span_i}: {span.get('text')}")
        #     print()

        return sections

    @staticmethod
    def _create_section(num_col: int, elements: Collection, h_range: tuple, y_ref: float):
        '''Create section based on column count, candidate elements and horizontal boundary.'''
        if not elements: return
        X0, X1 = h_range

        if num_col == 1:
            x0, y0, x1, y1 = elements.bbox
            # Note: do not use Column((X0, y0, X1, y1)) directly here. We have to set final bbox
            # per update_bbox to avoid double rotation in case page rotation exists.
            column = Column().update_bbox((X0, y0, X1, y1))  # this is final bbox, must use update_bbox
            column.add_elements(elements)
            section = Section(space=0, columns=[column])
            before_space = y0 - y_ref
        else:
            cols = elements.group_by_columns()
            # print('create section:')
            # for col in cols:
            #     print(col.store())
            split_col_i = -1
            # 这里也要考虑到多col的情况，把中点两边的分为两个col
            for col_i in range(len(cols) - 1):
                next_x0 = cols[col_i + 1].bbox[0]
                this_x1 = cols[col_i].bbox[2]
                page_center = (X1 + X0) / 2
                this_next_col_center_ratio = ((this_x1 + next_x0) / 2) / page_center
                if (1 - center_fuzzy_ratio) <= this_next_col_center_ratio <= (
                        1 + center_fuzzy_ratio) and next_x0 > this_x1:
                    split_col_i = col_i

            if split_col_i >= 0:
                col1 = Collection()
                col2 = Collection()
                for col_i in range(len(cols)):
                    if col_i <= split_col_i:
                        col1.extend(cols[col_i])
                    else:
                        col2.extend(cols[col_i])
                u0, v0, u1, v1 = col1.bbox
                m0, n0, m1, n1 = col2.bbox
            else:
                # print('split col i < 0')
                # print('col num', len(cols))
                u0, v0, u1, v1 = cols[0].bbox
                m0, n0, m1, n1 = cols[1].bbox
            u = (u1 + m0) / 2.0

            column_1 = Column().update_bbox((X0, v0, u, v1))
            column_1.add_elements(elements)

            column_2 = Column().update_bbox((u, n0, X1, n1))
            column_2.add_elements(elements)

            section = Section(space=0, columns=[column_1, column_2])
            # print('column1:', column_1.store())
            # print('column2:', column_2.store())
            before_space = v0 - y_ref

        section.before_space = round(before_space, 1)
        return section
