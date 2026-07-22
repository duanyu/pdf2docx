# -*- coding: utf-8 -*-

'''Image object.

Data structure defined in link https://pymupdf.readthedocs.io/en/latest/textpage.html::

    {
        'type': 1,
        'bbox': (x0,y0,x1,y1),
        'width': w,
        'height': h,
        'image': b'',

        # --- discard properties ---
        'ext': 'png',
        'colorspace': n,
        'xref': xref, 'yref': yref, 'bpc': bpc
    }
'''

import base64
from io import BytesIO
from ..common import docx
from ..common.Element import Element
from ..common.constants import MAX_PAGE_PT

import io
# 修改点 1：使用 as 给 PIL 的 Image 起个专属别名，防止与其他库冲突
from PIL import Image as PILImage


def compress_image_bytes(img_bytes, size_threshold_mb=2, max_dimension=1920, jpeg_quality=70):
    size_bytes = len(img_bytes)
    threshold_bytes = size_threshold_mb * 1024 * 1024

    if size_bytes < threshold_bytes:
        return img_bytes

    try:
        # --- 修改：设置安全像素上限，替代直接置为 None ---
        original_max_pixels = PILImage.MAX_IMAGE_PIXELS
        # 根据 10GB 内存设置上限，这里取 2_000_000_000 像素（约7.5G内存峰值）
        PILImage.MAX_IMAGE_PIXELS = 2_000_000_000
    
        # 修改点 2：使用 PILImage 代替 Image
        img = PILImage.open(io.BytesIO(img_bytes))

        if img.mode in ('RGBA', 'LA', 'P'):
            # 修改点 3：全部替换为 PILImage
            background = PILImage.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[3] if len(img.split()) >= 4 else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        if img.width > max_dimension or img.height > max_dimension:
            # 修改点 4：全部替换为 PILImage
            img.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)

        output_io = io.BytesIO()
        img.save(output_io, format='JPEG', quality=jpeg_quality, optimize=True)
        # img.save(output_io, format='PNG', quality=jpeg_quality, optimize=True)

        compressed_bytes = output_io.getvalue()

        if len(compressed_bytes) < size_bytes:
            return compressed_bytes
        else:
            return img_bytes

    except Exception as e:
        print(f"Warning: Image compression failed - {e}")
        return img_bytes
        
    finally:
        # --- 新增：恢复原来的像素限制 ---
        PILImage.MAX_IMAGE_PIXELS = original_max_pixels

class Image(Element):
    '''Base image object.'''

    def __init__(self, raw:dict=None):
        if raw is None: raw = {}        
        self.width = raw.get('width', 0.0)
        self.height = raw.get('height', 0.0)

        # source image bytes
        # - image bytes passed from PyMuPDF -> use it directly
        # - base64 encoded string restored from json file -> encode to bytes and decode with base64 -> image bytes 
        image = raw.get('image', b'')
        self.image = image if isinstance(image, bytes) else base64.b64decode(image.encode())
        
        super().__init__(raw)


    @property
    def text(self):
        '''Get an image placeholder ``<image>``.'''
        return '<image>'


    def from_image(self, image):
        '''Update with image block/span.
        
        Args:
            image (Image): Target image block/span.
        '''
        self.width = image.width
        self.height = image.height
        self.image = image.image
        self.update_bbox(image.bbox)
        return self


    def store(self):
        '''Store image with base64 encode.

        * Encode image bytes with base64 -> base64 bytes
        * Decode base64 bytes -> str -> so can be serialized in json format
        '''
        res = super().store()
        res.update({
            'width': self.width,
            'height': self.height,
            'image': base64.b64encode(self.image).decode() # serialize image with base64
        })

        return res


    def plot(self, page, color:tuple):
        '''Plot image bbox with diagonal lines (for debug purpose).
        
        Args: 
            page (fitz.Page): Plotting page.
        '''
        x0, y0, x1, y1 = self.bbox
        page.draw_line((x0, y0), (x1, y1), color=color, width=0.5)
        page.draw_line((x0, y1), (x1, y0), color=color, width=0.5)
        super().plot(page, stroke=color)


    def make_docx(self, paragraph):
        '''Add image span to a docx paragraph.'''
        # add image
        # print('原image大小:', len(self.image)/(1024*1024))
        self.image = compress_image_bytes(
            self.image,
            size_threshold_mb=1,
            jpeg_quality=90,
        )
        # print('压缩后image大小:', len(self.image) / (1024 * 1024))

        # 对于超宽的图片，进行rescale
        width = self.bbox.x1-max(0.0, self.bbox.x0)
        height = self.bbox.y1-max(0.0, self.bbox.y0)
        max_value = max(width, height)

        if max_value > MAX_PAGE_PT:
            width = width * MAX_PAGE_PT / max_value
            height = height * MAX_PAGE_PT / max_value
        
        # print('image bbox:', self.bbox)
        docx.add_image(paragraph, BytesIO(self.image), width, height)