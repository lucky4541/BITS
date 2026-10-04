"""PDF loading and rendering via PyMuPDF (fitz). Digital-text workflow: rendering is only
for on-screen display / image cropping, never for OCR of normal text."""
import fitz  # PyMuPDF

from core.constants import DPI


class PDFDocument:
    def __init__(self, path: str):
        self.path = path
        self.doc = fitz.open(path)

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def get_page(self, page_number: int) -> fitz.Page:
        """1-indexed page number."""
        return self.doc[page_number - 1]

    def page_size(self, page_number: int):
        page = self.get_page(page_number)
        return page.rect.width, page.rect.height

    def render_page(self, page_number: int, dpi: int = DPI) -> fitz.Pixmap:
        page = self.get_page(page_number)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        return page.get_pixmap(matrix=mat, alpha=False)

    def render_page_image(self, page_number: int, dpi: int = DPI):
        """Return a PIL.Image of the rendered page at the given DPI."""
        from PIL import Image
        pix = self.render_page(page_number, dpi)
        mode = "RGB" if pix.n < 4 else "RGBA"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if mode == "RGBA":
            img = img.convert("RGB")
        return img

    def crop_region(self, page_number: int, bbox, dpi: int = DPI):
        """Crop a region of the PDF page (bbox in PDF coordinate space) to a PIL.Image
        rendered at the given DPI, cropped from the original page (not from a screenshot)."""
        from PIL import Image
        page = self.get_page(page_number)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        clip = fitz.Rect(*bbox)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        mode = "RGB" if pix.n < 4 else "RGBA"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if mode == "RGBA":
            img = img.convert("RGB")
        return img

    def close(self):
        self.doc.close()
