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
from ..common.share import TextDirection
from importlib import resources
from fontTools.ttLib import TTFont
import base64
import math
import cv2
import numpy as np


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
    # print('ratio:', red_pixels / red_mask.size)
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

# DEFAULT_FONT_NAME = 'helv'
# root_pkg = __package__.split(".")[0]

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

# fangsong_path = str(resources.files(root_pkg).joinpath("fonts/仿宋_GB2312.ttf"))
# fangsong_line_height_ratio = Fonts.get_line_height_factor(TTFont(fangsong_path))
fangsong_line_height_ratio = 1.3

# dengxian_path = str(resources.files(root_pkg).joinpath("fonts/等线.ttf"))
# dengxian_line_height_ratio = Fonts.get_line_height_factor(TTFont(dengxian_path))
dengxian_line_height_ratio = 1.432

# arial_path = str(resources.files(root_pkg).joinpath("fonts/Arial.ttf"))
# arial_line_height_ratio = Fonts.get_line_height_factor(TTFont(arial_path))
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
            self.merge_image_with_overlap_lines(overlap_ratio=settings.get('merge_image_overlap_ratio', 0.1))
        # print('after merge:')
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

    def merge_image_with_overlap_lines(self, overlap_ratio=0.95):
        """
        1. 对所有 image block，用 Union-Find 将有交叉的合并为一个大的 image bbox
        2. 将与合并后 image bbox 有足够重叠的 text block 吸收进来（迭代扩大 bbox）
        3. 生成新的 image block，替换被合并的旧 blocks
        """

        intersect_threshold = 3  # 允许不严格重叠【为了合并图表的y轴】

        def bbox_area(b):
            return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

        def intersect(b1, b2, threshold=0):
            x0 = max(b1[0], b2[0])
            y0 = max(b1[1], b2[1])
            x1 = min(b1[2], b2[2])
            y1 = min(b1[3], b2[3])

            gap_x = x0 - x1  # 水平间距（>0 表示不重叠）
            gap_y = y0 - y1  # 垂直间距（>0 表示不重叠）

            if gap_x > threshold or gap_y > threshold:
                return None

            # 确保返回的 bbox 面积不为 0
            # 当不重叠时，制造一个最小为 1 像素的交集
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

        # ======== Phase 2: 迭代吸收与 image bbox 有足够重叠的 text blocks ========
        text_blocks = [b for b in self.blocks if b.store().get("type") != 1]

        removed_text_blocks = set()
        group_text_members = defaultdict(list)  # root -> [text_block, ...]

        # 迭代：合并 text block 会扩大 bbox，可能覆盖更多 text block
        changed = True
        while changed:
            changed = False
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

        # ======== Phase 3: 为发生合并的组生成新的 image block ========
        new_blocks = []
        all_removed = set()

        for root, members in groups.items():
            has_multiple_images = len(members) > 1
            has_text_merges = len(group_text_members.get(root, [])) > 0

            if not has_multiple_images and not has_text_merges:
                continue  # 单个 image 且没有吸收 text，保持原样

            merged_bbox = group_bboxes[root]
            # y轴加一点点【有时候图片截取不全】
            merged_bbox = (merged_bbox[0], merged_bbox[1], merged_bbox[2], merged_bbox[3]+2)
            rect = fitz.Rect(*merged_bbox)

            try:
                pix = self.page_engine.get_pixmap(clip=rect, dpi=200)
            except Exception as e:
                print(e)
                continue

            img_bytes = pix.tobytes("png")
            image_base64 = base64.b64encode(img_bytes).decode("ascii")

            if has_red_seal(image_base64, circularity_thresh=0.6):
                # print("red seal!")
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
        y_ref = Y0 # to calculate v-distance between sections
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
                    if (1-center_fuzzy_ratio) <= this_next_col_center_ratio <= (1+center_fuzzy_ratio) and next_x0 > this_x1 and split_col_i < 0:
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

                if current_num_col > 2:
                    # 额外处理左侧为小节标题的情况
                    if has_wide_col and hasattr(cols[0][0], "text") and len(cols[0][0].text.strip()) > 0 and cols[0][0].text.strip()[0] in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
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
            if current_num_col>2:
                current_num_col = 1

            # the width of two columns shouldn't have significant difference
            # 避免table被误判
            elif current_num_col==2:
                u0, v0, u1, v1 = cols[0].bbox
                m0, n0, m1, n1 = cols[1].bbox
                x0 = (u1+m0)/2.0
                c1, c2 = x0-X0, X1-x0 # column width
                w1, w2 = u1-u0, m1-m0 # line width
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
            if pre_num_col==2 and current_num_col==1:
                # though current row has one single column, it might have another virtual
                # and empty column. If so, it should be counted as 2-cols
                cols = lines.group_by_columns()
                pos = cols[0].bbox[2]
                if row.bbox[2]<=pos or row.bbox[0]>pos:
                    current_num_col = 2

                # pre_num_col!=current_num_col => to close section with collected lines,
                # before that, further check the height of collected lines
                else:
                    x0, y0, x1, y1 = lines.bbox
                    if y1-y0<settings['min_section_height']:
                        pre_num_col = 1


            elif pre_num_col==2 and current_num_col==2:
                # though both 2-cols, they don't align with each other
                combine = Collection(lines)
                combine.extend(row)
                if len(combine.group_by_columns(sorted=False))==1: current_num_col = 1


            # finalize pre-section if different from the column count of previous section
            if current_num_col!=pre_num_col:
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
    def _create_section(num_col:int, elements:Collection, h_range:tuple, y_ref:float):
        '''Create section based on column count, candidate elements and horizontal boundary.'''
        if not elements: return
        X0, X1 = h_range

        if num_col==1:
            x0, y0, x1, y1 = elements.bbox
            # Note: do not use Column((X0, y0, X1, y1)) directly here. We have to set final bbox
            # per update_bbox to avoid double rotation in case page rotation exists.
            column = Column().update_bbox((X0, y0, X1, y1)) # this is final bbox, must use update_bbox
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
            for col_i in range(len(cols)-1):
                next_x0 = cols[col_i + 1].bbox[0]
                this_x1 = cols[col_i].bbox[2]
                page_center = (X1 + X0) / 2
                this_next_col_center_ratio = ((this_x1 + next_x0) / 2) / page_center
                if (1-center_fuzzy_ratio) <= this_next_col_center_ratio <= (1+center_fuzzy_ratio) and next_x0 > this_x1:
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
            u = (u1+m0)/2.0

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
