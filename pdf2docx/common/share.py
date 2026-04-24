'''Common methods.'''

from enum import Enum
import random, re
from collections.abc import Iterable
from fitz.utils import getColorList, getColorInfoList


class BlockType(Enum):
    '''Block types.'''
    UNDEFINED = -1
    TEXT = 0
    IMAGE = 1
    LATTICE_TABLE = 2
    STREAM_TABLE = 3
    FLOAT_IMAGE = 4


class RectType(Enum):
    '''Shape type in context.'''
    HIGHLIGHT = 1
    UNDERLINE = 1<<1
    STRIKE = 1<<2
    HYPERLINK = 1<<3
    BORDER = 1<<4
    SHADING = 1<<5


class TextDirection(Enum):
    '''Text direction.
    * LEFT_RIGHT: from left to right within a line, and lines go from top to bottom
    * BOTTOM_TOP: from bottom to top within a line, and lines go from left to right
    * MIX       : a mixture if LEFT_RIGHT and BOTTOM_TOP
    * IGNORE    : neither LEFT_RIGHT nor BOTTOM_TOP
    '''
    IGNORE     = -1
    LEFT_RIGHT = 0
    BOTTOM_TOP = 1
    MIX        = 2


class TextAlignment(Enum):
    '''Text alignment.

    .. note::
        The difference between ``NONE`` and ``UNKNOWN``: 

        * NONE: none of left/right/center align -> need TAB stop
        * UNKNOWN: can't decide, e.g. single line only
    '''
    NONE    = -1
    UNKNOWN = 0
    LEFT    = 1
    CENTER  = 2
    RIGHT   = 3
    JUSTIFY = 4


class IText:
    '''Text related interface considering text direction.'''
    @property
    def text_direction(self):
        '''Text direction is from left to right by default.'''
        return TextDirection.LEFT_RIGHT

    @property
    def is_horizontal_text(self):
        '''Check whether text direction is from left to right.'''
        return self.text_direction == TextDirection.LEFT_RIGHT or \
                self.text_direction == TextDirection.MIX

    @property
    def is_vertical_text(self):
        '''Check whether text direction is from bottom to top.'''
        return self.text_direction == TextDirection.BOTTOM_TOP

    @property
    def is_mix_text(self):
        '''Check whether text direction is either from left to 
        right or from bottom to top.'''
        return self.text_direction == TextDirection.MIX


class lazyproperty:
    '''Calculate only once and cache property value.'''
    def __init__(self, func):
        self.func = func

    def __get__(self, instance, cls):
        if instance is None:
            return self
        else:
            value = self.func(instance)
            setattr(instance, self.func.__name__, value)
            return value


# -------------------------
# methods
# -------------------------
def is_number(str_number):
    '''Whether can be converted to a float.'''
    try:
        float(str_number)
    except ValueError:
        return False
    else:
        return True


def flatten(items, klass):
    '''Yield items from any nested iterable.'''
    for item in items:
        if isinstance(item, Iterable) and not isinstance(item, klass):
            yield from flatten(item, klass)
        else:
            yield item


def lower_round(number:float, ndigits:int=0):
    '''Round number to lower bound with specified digits, e.g. lower_round(1.26, 1)=1.2'''
    n = 10.0**ndigits
    return int(n*number) / n


def decode(s:str):
    '''Try to decode a unicode string.'''
    b = bytes(ord(c) for c in s)
    for encoding in ['utf-8', 'gbk', 'gb2312', 'iso-8859-1']:
        try:
            res = b.decode(encoding)
            break
        except:
            continue
    return res


# -------------------------
# color methods
# -------------------------
def rgb_component_from_name(name:str=''):
    '''Get a named RGB color (or random color) from fitz predefined colors, e.g. 'red' -> (1.0,0.0,0.0).'''
    # get color index
    if name and name.upper() in getColorList():
        pos = getColorList().index(name.upper())
    else:
        pos = random.randint(0, len(getColorList())-1)

    c = getColorInfoList()[pos]
    return (c[1] / 255.0, c[2] / 255.0, c[3] / 255.0)


def rgb_component(srgb:int):
    '''srgb value to R,G,B components, e.g. 16711680 -> (255, 0, 0).

    Equal to PyMuPDF built-in method::

        [int(255*x) for x in fitz.sRGB_to_pdf(x)]
    '''
    # decimal to hex: 0x...
    s = hex(srgb)[2:].zfill(6)
    return [int(s[i:i+2], 16) for i in [0, 2, 4]]


def rgb_to_value(rgb:list):
    '''RGB components to decimal value, e.g. (1,0,0) -> 16711680.'''
    res = 0
    for (i,x) in enumerate(rgb):
        res += int(x*(16**2-1)) * 16**(4-2*i)
    return int(res)


def cmyk_to_rgb(c:float, m:float, y:float, k:float, cmyk_scale:float=100):
    '''CMYK components to GRB value.'''
    r = (1.0 - c / float(cmyk_scale)) * (1.0 - k / float(cmyk_scale))
    g = (1.0 - m / float(cmyk_scale)) * (1.0 - k / float(cmyk_scale))
    b = (1.0 - y / float(cmyk_scale)) * (1.0 - k / float(cmyk_scale))
    res = rgb_to_value([r, g, b]) # type: int
    return res


def rgb_value(components:list):
    '''Gray/RGB/CMYK mode components to color value.'''
    num = len(components)
    # CMYK mode
    if num==4:
        c, m, y, k = map(float, components)
        color = cmyk_to_rgb(c, m, y, k, cmyk_scale=1.0)
    # RGB mode
    elif num==3:
        r, g, b = map(float, components)
        color = rgb_to_value([r, g, b])
    # gray mode
    elif num==1:
        g = float(components[0])
        color = rgb_to_value([g,g,g])    
    else:
        color = 0

    return color


# -------------------------
# pdf plot
# -------------------------
def new_page(doc, width:float, height:float, title:str):
    '''Insert a new page with given title.

    Args:
        doc (fitz.Document): pdf document object.
        width (float): Page width.
        height (float): Page height.
        title (str): Page title shown in page.
    '''
    # insert a new page
    page = doc.new_page(width=width, height=height)    

    # plot title at the top-left corner
    gray = rgb_component_from_name('gray')
    page.insert_text((5, 16), title, color=gray, fontsize=15)

    return page


def debug_plot(title:str, show=True):
    '''Plot the returned objects of inner function.

    Args:
        title (str): Page title.
        show (bool, optional): Don't plot if show==False. Default to True.

    .. note::
        Prerequisite of the inner function: 
            - the first argument is a :py:class:`~pdf2docx.page.BasePage` instance.
            - the last argument is configuration parameters in ``dict`` type.
    '''
    def wrapper(func):
        def inner(*args, **kwargs):
            # execute function
            objects = func(*args, **kwargs)

            # check if plot page
            page = args[0] # BasePage object
            debug = kwargs.get('debug', False)
            doc = kwargs.get('debug_doc', None)
            filename = kwargs.get('debug_filename', None)

            if show and objects and debug and doc is not None:                
                # create a new page
                debug_page = new_page(doc, page.width, page.height, title)
                # plot objects, e.g. text blocks, shapes, tables...
                objects.plot(debug_page)
                doc.save(filename)

            return objects
        return inner
    return wrapper


def is_toc_dots(text):
    """
    判断文本是否为目录中的点号引导符（如 . . . . . . . . .）

    Args:
        text: 要检查的文本字符串

    Returns:
        bool: 如果是目录点号符号返回True，否则返回False
    """
    if not text:
        return False

    if text.count('.') < 5 and text.count('•') < 5:
        return False

    if re.search(r'([•\.]\s*){5,}', text):
        return True

    return False


class ListItemDetector:
    """列表项检测器，带快速过滤优化"""

    # 定义快速过滤用的符号集合（O(1)查找）
    QUICK_FILTER_CHARS = {
        # Bullet 符号
        '•', '-', '*', '○', '●', '□', '■', '◆', '◇', '▪', '▫',
        '✓', '✔', '➢', '➣', '➤', '►', '⦿', '⦾',
        # 点和顿号
        '.', '、', '。',
        # 括号（用于编号）
        '(', ')', '（', '）', '[', ']',
    }

    # 详细的正则模式
    PATTERNS = [
        # 1. Bullet 符号
        r'^[•\-*○●□■◆◇▪▫✓✔➢➣➤►⦿⦾]\s',

        # 2. 数字编号
        r'^\d{1,3}\.\s',  # 1. 2. 3.
        r'^\d{1,3}\)\s',  # 1) 2) 3)
        r'^\d{1,3}[、]\s*',  # 1、2、3、
        r'^\(\d{1,3}\)\s*',  # (1) (2) (3)
        r'^\[\d{1,3}\]\s*',  # [1] [2] [3]

        # ✅ 新增：多级数字编号
        r'^\d+(\.\d+){1,3}\.?\s',  # 4.3 xxx / 4.3.3 xxx / 1.2.3.4. xxx

        # # 3. 字母编号 - 太容易在参考文献中误判，去掉
        # r'^[A-Za-z]\.\s',  # A. B. C.
        # r'^[A-Za-z]\)\s',  # A) B) C)
        # r'^\([A-Za-z]\)\s*',  # (A) (B) (C)
        # r'^\[[A-Za-z]\]\s*',  # [A] [B] [C]

        # # 4. 罗马数字
        # r'^[IVXLCDMivxlcdm]+\.\s',  # I. II. III.
        # r'^[IVXLCDMivxlcdm]+\)\s',  # I) II) III)
        # r'^\([IVXLCDMivxlcdm]+\)\s*',  # (I) (II) (III)

        # 5. 中文数字
        r'^[一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+[、.]\s*',
        r'^[（(][一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+[）)]\s*',

        # 6. 中文章节标记
        r'^第[一二三四五六七八九十百千万\d]+[章节条款项部分篇回集卷]\s*',
        r'^第[（(]?\d+[）)]?[章节条款项部分篇回集卷]\s*',
    ]

    def __init__(self):
        # 预编译正则表达式以提高性能
        self.compiled_patterns = [re.compile(p) for p in self.PATTERNS]

    def quick_filter(self, text):
        """
        快速过滤：检查文本前几个字符是否包含列表符号

        参数:
            text (str): 输入文本

        返回:
            bool: 如果可能是列表项返回True，否则返回False
        """
        # 检查前5个字符（大部分列表标记在前面）
        prefix = text[:5] if len(text) >= 5 else text

        # 快速检查是否包含任何列表相关符号
        return any(char in self.QUICK_FILTER_CHARS for char in prefix)

    def is_list_item(self, text):
        """
        判断给定文本是否为列表项

        参数:
            text (str): 输入文本

        返回:
            bool: 如果是列表项返回True，否则返回False
        """
        if not text or not isinstance(text, str):
            return False

        # 去除首尾空白
        text = text.strip()

        if not text:
            return False

        # 🚀 快速过滤：如果不包含任何列表符号；或者不是toc，直接返回False
        if not self.quick_filter(text) and not is_toc_dots(text):
            return False

        # 详细匹配：使用预编译的正则表达式
        for pattern in self.compiled_patterns:
            if pattern.match(text):
                return True

        return False

# 简化版函数接口
_detector = ListItemDetector()

def is_list_item(text):
    """
    便捷函数：判断文本是否为列表项

    参数:
        text (str): 输入文本

    返回:
        bool: 如果是列表项返回True，否则返回False
    """
    return _detector.is_list_item(text)