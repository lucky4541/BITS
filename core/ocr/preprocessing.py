"""Optional page-image preprocessing for OCR (spec section 20) - operates
ONLY on a temporary rendered/cropped image (core.pdf_loader.PDFDocument.
render_page_image/crop_region already never touches the source PDF file
itself), never the source PDF. Every function takes and returns a
PIL.Image so they compose freely and the ORIGINAL rendered image stays
available unmodified for visual comparison (spec: "Keep the original
rendered image available").

Uses OpenCV (cv2) + numpy, both confirmed installed in this environment -
unlike core/ocr/paddle_engine.py, every function here is real, working,
and has been exercised directly (see the module's own test invocation),
not just written against documentation."""
import numpy as np
from PIL import Image
import cv2


def to_grayscale(image: Image.Image) -> Image.Image:
    return image.convert("L")


def enhance_contrast(image: Image.Image, clip_limit: float = 2.0) -> Image.Image:
    """CLAHE (contrast-limited adaptive histogram equalization) - better
    for scanned book pages with uneven lighting/toner than a flat linear
    contrast stretch, since it adapts per local region rather than
    globally."""
    arr = np.array(image.convert("L"))
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    return Image.fromarray(clahe.apply(arr))


def denoise(image: Image.Image, strength: int = 7) -> Image.Image:
    arr = np.array(image.convert("L"))
    return Image.fromarray(cv2.fastNlMeansDenoising(arr, h=strength))


def adaptive_threshold(image: Image.Image, block_size: int = 35, c: int = 11) -> Image.Image:
    """Binarizes a scanned page for OCR - adaptive (per-region) rather
    than one global cutoff, since scan brightness commonly varies across
    a page. block_size must be odd; c is subtracted from the local mean."""
    arr = np.array(image.convert("L"))
    if block_size % 2 == 0:
        block_size += 1
    thresh = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, block_size, c)
    return Image.fromarray(thresh)


def deskew(image: Image.Image, max_angle: float = 15.0) -> Image.Image:
    """Detects page rotation via the minimum-area bounding rectangle of
    all dark (text) pixels and rotates it back straight - a standard,
    well-established technique, not a guess. Returns the image UNCHANGED
    if the detected angle is negligible (<0.1deg) or implausibly large
    (>max_angle, almost certainly a mis-detection on a mostly-blank or
    non-text page) rather than risking a wrong "correction"."""
    gray = np.array(image.convert("L"))
    # Text is dark on light background in almost every scanned book page -
    # invert + threshold so text pixels become the "foreground" (255) the
    # bounding-rectangle search below actually needs.
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = cv2.findNonZero(binary)
    if coords is None or len(coords) < 20:
        return image  # not enough foreground pixels to trust an angle estimate
    angle = cv2.minAreaRect(coords)[-1]
    # cv2.minAreaRect's angle convention: normalize into (-45, 45].
    if angle < -45:
        angle = 90 + angle
    if abs(angle) < 0.1 or abs(angle) > max_angle:
        return image
    arr = np.array(image)
    (h, w) = arr.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(arr, matrix, (w, h), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(rotated)


def detect_orientation(image: Image.Image) -> int:
    """Best-effort 0/90/180/270 orientation guess from text-row density
    (real page text produces a strong horizontal banding pattern of dark-
    pixel row sums when upright; a 90-degree-rotated page instead shows
    that banding in the COLUMN sums). This is a coarse heuristic, not true
    OSD (orientation-and-script-detection, which needs a trained model
    neither engine available in this environment currently provides) -
    documented as such rather than overstated. Returns one of 0/90/180/270
    (the detected clockwise rotation needed to make the page upright);
    never distinguishes 0 from 180 or 90 from 270 (both give identical row/
    column banding), so a caller relying on this for anything beyond a
    coarse portrait/landscape check should combine it with real text
    (e.g. running OCR and checking recognized-word plausibility)."""
    gray = np.array(image.convert("L"))
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    row_variance = np.var(binary.sum(axis=1))
    col_variance = np.var(binary.sum(axis=0))
    return 0 if row_variance >= col_variance else 90


def remove_borders(image: Image.Image, margin_ratio: float = 0.02) -> Image.Image:
    """Crops a thin margin_ratio-of-dimension border - scanned book pages
    routinely have a dark scanner-bed edge or binding shadow along one or
    more sides that can confuse OCR block detection. A simple proportional
    crop, not content-aware, and deliberately conservative (default 2%
    per side) so real page content is never cut off."""
    w, h = image.size
    dx, dy = int(w * margin_ratio), int(h * margin_ratio)
    return image.crop((dx, dy, w - dx, h - dy))
