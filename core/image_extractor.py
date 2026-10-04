"""Crops figure/equation images from the ORIGINAL PDF page (never a screenshot) at
200 DPI and saves them into assets/ using a global, never-restarting sequence
(fig001, fig002, ... / eqn001, eqn002, ...) shared across the whole document."""
import io
import os

import fitz

from core.constants import DPI, DEFAULT_JPEG_QUALITY, DEFAULT_FIGURE_CONTENT_WIDTH_INCHES
from core import text_extractor


def remove_background_to_white(img, tolerance: int = 12):
    """Lightweight, dependency-free background cleanup: flood-fills from each
    corner (connected near-uniform region only, via PIL's own threshold flood
    fill) replacing it with pure white. Only touches pixels CONNECTED to a
    corner within the color tolerance, so an isolated diagram element that
    happens to share a similar color far from the edges is left alone -
    unlike a global color-threshold replace, which could damage it.

    tolerance is capped at 16: PIL's floodfill(thresh=...) is documented as
    "experimental", and empirically (this Pillow version) any thresh above
    ~16 makes it silently fill ZERO pixels instead of more - so higher is
    not "more tolerant", it's "broken". 16 is already enough slack for
    typical JPEG/render anti-aliasing noise at a background's edge.
    """
    from PIL import Image, ImageDraw
    tolerance = max(0, min(16, tolerance))
    img = img.convert("RGB")
    w, h = img.size
    if w < 2 or h < 2:
        return img
    out = img.copy()
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        try:
            ImageDraw.floodfill(out, corner, (255, 255, 255), thresh=tolerance)
        except Exception:
            pass
    return out


def _matches_requested_dpi(img, bbox, dpi: int, tolerance: float = 0.15) -> bool:
    """True only if img's actual pixel dimensions are AT LEAST what rendering
    bbox at the requested dpi would produce (within a small tolerance for
    PDF coordinate/rounding slack) - i.e. an embedded image is trusted as a
    substitute for rendering ONLY when its native resolution genuinely meets
    the requested extraction DPI for this specific crop region. An embedded
    image that exceeds the request (e.g. a 300 DPI scan when 200 was asked
    for) is still fine to use as-is - no forced downsampling, since that
    would just discard quality for no benefit. But an embedded image BELOW
    the requested DPI (e.g. a ~96 DPI embedded raster when 200 was
    configured) must never be silently accepted - see AssetManager.dpi."""
    x0, y0, x1, y1 = bbox
    width_pts, height_pts = max(0.0, x1 - x0), max(0.0, y1 - y0)
    if width_pts <= 0 or height_pts <= 0:
        return False
    expected_w = width_pts * dpi / 72.0
    expected_h = height_pts * dpi / 72.0
    if expected_w <= 0 or expected_h <= 0:
        return False
    actual_w, actual_h = img.size
    return actual_w >= expected_w * (1 - tolerance) and actual_h >= expected_h * (1 - tolerance)


def fit_width_to_content(img, max_width_px):
    """Proportionally downscales img so its width never exceeds
    max_width_px - preserves the exact aspect ratio (height scales by the
    same factor as width, never stretched/distorted independently), and
    never enlarges an image that's already narrower than the target (only
    ever shrinks, never grows). A no-op when max_width_px is falsy/<=0 or
    the image already fits."""
    if not max_width_px or max_width_px <= 0 or img.width <= max_width_px:
        return img
    from PIL import Image
    scale = max_width_px / img.width
    new_size = (int(max_width_px), max(1, round(img.height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def _find_matching_image_info(page, bbox, coverage_min=0.85, area_tol=0.5):
    """The single source of truth for "does an embedded image closely
    match bbox" - returns that image's raw get_image_info() dict (which
    includes its placement "transform" matrix, xref, etc) or None. Shared
    by _try_extract_embedded (which image DATA to reuse instead of
    re-rendering) and detect_image_transform_rotation_key (which
    ROTATION that same placement implies) - never two separate/competing
    matching heuristics."""
    target = fitz.Rect(*bbox)
    target_area = target.width * target.height
    if target_area <= 0:
        return None
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return None
    for im in infos:
        ibbox = fitz.Rect(im["bbox"])
        inter = ibbox & target
        if inter.is_empty:
            continue
        inter_area = inter.width * inter.height
        ibbox_area = ibbox.width * ibbox.height
        if ibbox_area <= 0:
            continue
        coverage = inter_area / target_area
        area_diff = abs(ibbox_area - target_area) / target_area
        if coverage >= coverage_min and area_diff <= area_tol and im.get("xref"):
            return im
    return None


def _try_extract_embedded(pdf_document, page_number, bbox, coverage_min=0.85, area_tol=0.5):
    """If an embedded image closely matches bbox, return its original-resolution
    PIL image; else None. Preferred over re-rendering when it applies."""
    page = pdf_document.get_page(page_number)
    im = _find_matching_image_info(page, bbox, coverage_min, area_tol)
    if im is None:
        return None
    try:
        base = pdf_document.doc.extract_image(im["xref"])
        from PIL import Image
        img = Image.open(io.BytesIO(base["image"]))
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception:
        return None


# ---------- figure orientation detection/fix ----------
# Maps a direction vector's nearest cardinal orientation to the rotation
# (degrees, clockwise) the CONTENT was placed at relative to normal
# upright reading - never a guess from image width/height. 0 = already
# upright. Verified empirically against PyMuPDF's own convention: a text
# span/image placed with fitz's rotate=90 (rotated 90 degrees counter-
# clockwise) reports a local x-axis direction of (0, -1); rotate=180 gives
# (-1, 0); rotate=270 (=90 clockwise) gives (0, 1).
_DIR_TO_ROTATION_KEY = {
    (1, 0): 0,
    (0, -1): 90,    # content rotated 90 CCW from upright
    (-1, 0): 180,
    (0, 1): 270,    # content rotated 90 CW from upright
}
# The fix for each detected rotation key - the OPPOSITE turn, undoing it
# back to upright (spec: "if rotation is 90 clockwise, rotate 90 counter-
# clockwise" etc). Values are PIL Image.Transpose constants (looked up
# lazily below, PIL isn't imported at module level here, matching this
# file's existing convention).
_ROTATION_KEY_TO_FIX_TRANSPOSE_NAME = {
    0: None,
    90: "ROTATE_270",   # undo a 90 CCW rotation with a 90 CW (=270 CCW) turn
    180: "ROTATE_180",
    270: "ROTATE_90",   # undo a 90 CW rotation with a 90 CCW turn
}


def _direction_to_rotation_key(dx, dy):
    """Rounds a (possibly non-unit) direction vector to the nearest
    cardinal direction and looks up the rotation key - None for a
    diagonal/degenerate vector (not a reliable orientation signal, so it's
    simply not counted rather than guessed at)."""
    import math
    mag = math.hypot(dx, dy)
    if mag < 1e-6:
        return None
    rdx, rdy = round(dx / mag), round(dy / mag)
    return _DIR_TO_ROTATION_KEY.get((rdx, rdy))


def detect_text_rotation_key(page, bbox):
    """Returns the dominant TEXT rotation (0/90/180/270 - degrees the
    content was placed at relative to upright reading) for characters
    within bbox, using each PDF text line's own "dir" (writing-direction
    unit vector) from the page's rawdict - the actual PDF geometry, never
    an image-dimension guess. Votes are weighted by character count so a
    handful of stray/misplaced characters can't outvote the real body of
    text. Returns None when no text falls within bbox at all (common for
    pure diagrams/images with no embedded text) - the caller then falls
    back to detect_image_transform_rotation_key."""
    raw = text_extractor._get_rawdict(page)
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            char_count = sum(
                1 for span in spans for ch in span.get("chars", [])
                if _char_in_bbox(ch["bbox"], bbox)
            )
            if char_count == 0:
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            key = _direction_to_rotation_key(dx, dy)
            if key is not None:
                votes[key] += char_count
    if not any(votes.values()):
        return None
    return max(votes, key=votes.get)


def detect_image_transform_rotation_key(page, bbox):
    """Fallback used only when detect_text_rotation_key found no text in
    bbox: decomposes the placement "transform" matrix (a, b, c, d, e, f -
    the same PDF image-placement matrix section 7 of the spec calls out
    explicitly) of the single embedded image that closely matches bbox
    (see _find_matching_image_info - the SAME matching used to decide
    whether to reuse that image's data at all). (a, b) is the image's own
    local x-axis (its "width" direction) expressed in page space - for a
    non-rotated placement this points straight along the page's own
    x-axis (1, 0); a rotated placement's local x-axis points along
    whichever cardinal direction the rotation turned it to, decoded via
    the exact same _direction_to_rotation_key table text detection uses.
    Returns None when no closely-matching single embedded image exists
    (mixed/vector content, or several overlapping images) - callers must
    not fabricate a rotation guess from dimensions alone in that case."""
    im = _find_matching_image_info(page, bbox)
    if im is None:
        return None
    transform = im.get("transform")
    if not transform or len(transform) < 4:
        return None
    a, b = transform[0], transform[1]
    return _direction_to_rotation_key(a, b)


def apply_rotation_fix(img, rotation_key):
    """Rotates img to undo rotation_key (see detect_text_rotation_key /
    detect_image_transform_rotation_key) and bring it to upright, normal
    reading orientation. rotation_key of 0 or None is a no-op (already
    upright, or orientation genuinely couldn't be determined - never
    rotate on a guess). Uses PIL's own lossless transpose (not a resample/
    rotate-by-arbitrary-angle), so there is no quality loss, no stretching,
    and no distortion - width/height are exactly swapped for a 90/270 fix,
    exactly preserved for 180."""
    transpose_name = _ROTATION_KEY_TO_FIX_TRANSPOSE_NAME.get(rotation_key)
    if not transpose_name:
        return img
    from PIL import Image
    return img.transpose(getattr(Image, transpose_name))


def _char_in_bbox(ch_bbox, bbox, tol=1.0):
    """Same center-point-inside-bbox test text_extractor.py's own
    extraction uses (kept as a local copy here rather than importing a
    private helper across modules for a one-line geometry check)."""
    cx0, cy0, cx1, cy1 = ch_bbox
    x0, y0, x1, y1 = bbox
    cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
    return (x0 - tol) <= cx <= (x1 + tol) and (y0 - tol) <= cy <= (y1 + tol)


class AssetManager:
    """`dpi` here is the IMAGE EXTRACTION dpi (Settings > Image DPI) - a
    completely separate concept from the PDF viewer's on-screen zoom/render
    DPI (core.constants.DPI, gui/pdf_viewer.py). Changing one must never
    affect the other: this class never reads the viewer's zoom state, and
    the viewer never reads this class's dpi."""

    def __init__(self, assets_dir: str, prefix: str = "document", jpeg_quality: int = DEFAULT_JPEG_QUALITY,
                 counters: dict = None, dpi: int = DPI, remove_background: bool = False,
                 figure_content_width_inches: float = DEFAULT_FIGURE_CONTENT_WIDTH_INCHES,
                 image_prefix: str = None):
        self.assets_dir = assets_dir
        self.prefix = prefix
        # EPUB/CUPEPUB output-naming spec: an image filename uses the SHORT
        # component code (e.g. "ch1" derived from "06_918AR_ch1"), never the
        # full prefix - deliberately a SEPARATE value from self.prefix
        # (which XML-profile _next_name, and every id/filename elsewhere,
        # keeps using unchanged) so this never touches the XML profile's
        # existing fig001.jpg-style output or any element id. Defaults to
        # prefix itself when not given, so every existing caller (XML
        # profile's AssetManager construction, any code that hasn't been
        # updated to pass one) is completely unaffected.
        self.image_prefix = image_prefix if image_prefix is not None else prefix
        self.jpeg_quality = jpeg_quality
        self.dpi = dpi
        # Off by default: some scientific diagrams have an intentional
        # background, so this must stay an explicit opt-in (Settings).
        self.remove_background = remove_background
        # Target print width for a cropped Figure image, converted to
        # pixels via the SAME dpi used to crop it - so raising Image DPI
        # in Settings scales the fit-width target consistently, instead of
        # a fixed pixel count that would mean something different at every
        # DPI. Only ever downscales (see fit_width_to_content) - an image
        # already narrower than this is left exactly as cropped.
        self.figure_max_width_px = figure_content_width_inches * self.dpi
        self.counters = counters if counters is not None else {"figure": 0, "equation": 0}
        os.makedirs(self.assets_dir, exist_ok=True)

    def _next_name(self, kind: str, tag: str) -> str:
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return f"{self.prefix}-{tag}{self.counters[kind]:03d}.jpg"

    def _next_epub_name(self, kind: str, token: str, ext: str, digits: int, no_prefix: bool = False) -> str:
        """Profile-driven naming for the EPUB profile (core/epub_xml_generator.py)
        - deliberately a SEPARATE counter/format from _next_name above (hyphen
        before the number, 2-digit default, PNG default) so it can never
        collide with or alter the XML profile's existing fig001.jpg-style
        filenames. `kind` is the independent-counter key (e.g. "img",
        "uncapfig", "eqn_img" - one per Mapping.xml image tag, per the "counters
        must remain independent per image type" requirement); `token` is the
        filename word (e.g. "fig", "figu", "eqn"). digits<=0 means "no counter
        suffix on the first one" (Cover/Logo: cover.jpg, {prefix}-logo.jpg) -
        a second asset of that same kind still gets a numeric suffix so it
        never silently overwrites the first file on disk."""
        self.counters[kind] = self.counters.get(kind, 0) + 1
        n = self.counters[kind]
        if digits <= 0:
            suffix = "" if n == 1 else f"-{n}"
            base = token if no_prefix else f"{self.image_prefix}-{token}"
            return f"{base}{suffix}.{ext}"
        return f"{self.image_prefix}-{token}-{n:0{digits}d}.{ext}"

    def save_image_asset(self, pdf_document, page_number: int, bbox, kind: str, token: str,
                          ext: str = "png", digits: int = 2, no_prefix: bool = False,
                          prefer_embedded: bool = True):
        """Generic EPUB-profile asset save: the exact same crop / embedded-
        image-reuse / orientation-fix / background-removal / fit-width
        pipeline as save_figure_asset/save_equation_asset below (which are
        thin wrappers around the ORIGINAL _next_name, untouched - this is a
        genuinely separate code path, not a refactor of theirs), just with a
        Profile-supplied kind/token/ext/digits instead of the two hardcoded
        figure/equation cases. Returns (filename, warning) - warning is a
        human-readable string when a lower-resolution embedded image had to
        be re-rendered, or None."""
        filename = self._next_epub_name(kind, token, ext, digits, no_prefix)
        img = None
        if prefer_embedded:
            img = _try_extract_embedded(pdf_document, page_number, bbox)
            if img is not None and not _matches_requested_dpi(img, bbox, self.dpi):
                img = None
        if img is None:
            img = pdf_document.crop_region(page_number, bbox, dpi=self.dpi)
        page = pdf_document.get_page(page_number)
        rotation_key = detect_text_rotation_key(page, bbox)
        if rotation_key is None:
            rotation_key = detect_image_transform_rotation_key(page, bbox)
        if rotation_key:
            img = apply_rotation_fix(img, rotation_key)
        if self.remove_background:
            img = remove_background_to_white(img)
        img = fit_width_to_content(img, self.figure_max_width_px)
        path = os.path.join(self.assets_dir, filename)
        ext_upper = ext.lstrip(".").upper()
        pil_format = "JPEG" if ext_upper in ("JPG", "JPEG") else ext_upper
        save_kwargs = {"dpi": (self.dpi, self.dpi)}
        if pil_format == "JPEG":
            if img.mode != "RGB":
                img = img.convert("RGB")
            save_kwargs["quality"] = self.jpeg_quality
        img.save(path, pil_format, **save_kwargs)
        return filename

    def save_figure_asset(self, pdf_document, page_number: int, bbox, prefer_embedded=True) -> str:
        filename = self._next_name("figure", "fig")
        img = None
        if prefer_embedded:
            img = _try_extract_embedded(pdf_document, page_number, bbox)
            if img is not None and not _matches_requested_dpi(img, bbox, self.dpi):
                # The embedded image's native resolution is below what the
                # configured extraction DPI requires for this crop region -
                # never silently ship a lower-resolution image just because
                # one happened to exist; re-render from the page instead.
                img = None
        if img is None:
            img = pdf_document.crop_region(page_number, bbox, dpi=self.dpi)
        # Orientation fix - MUST happen before remove_background/fit_width
        # (spec order: Crop -> Detect Orientation -> Rotate -> remove
        # margins -> Fit Width -> Save; rotating a portrait crop to
        # landscape changes what "width" even means for the later fit-
        # width step, and a rotated image's TRUE corners for background
        # flood-fill are only correct once it's the right way up). Text
        # orientation (PDF span "dir" vectors) is tried first since it's
        # the more reliable signal when the figure has any readable
        # text/labels at all; only falls back to the embedded image's own
        # placement transform matrix when no text was found in bbox -
        # never a guess from the crop's width/height.
        page = pdf_document.get_page(page_number)
        rotation_key = detect_text_rotation_key(page, bbox)
        if rotation_key is None:
            rotation_key = detect_image_transform_rotation_key(page, bbox)
        if rotation_key:
            img = apply_rotation_fix(img, rotation_key)
        if self.remove_background:
            img = remove_background_to_white(img)
        # Proportional fit-to-content-width - crop dimensions/aspect ratio
        # are already correct at this point (exact Figure zone bbox, label/
        # caption already excluded upstream by xml_generator._zone_figure's
        # own img_bbox computation, untouched here); this only ever shrinks
        # an oversized image down, never stretches or enlarges.
        img = fit_width_to_content(img, self.figure_max_width_px)
        path = os.path.join(self.assets_dir, filename)
        # Pixel dimensions alone aren't enough: a JPEG with no resolution tag
        # displays as "96 DPI" in Windows Explorer/most viewers (their own
        # fallback default for missing metadata) even when the pixel data
        # itself was correctly rendered at self.dpi - so the DPI tag must be
        # written too, not just correct pixel dimensions.
        img.save(path, "JPEG", quality=self.jpeg_quality, dpi=(self.dpi, self.dpi))
        return filename

    def save_equation_asset(self, pdf_document, page_number: int, bbox, prefer_embedded=False) -> str:
        filename = self._next_name("equation", "eqn")
        img = None
        if prefer_embedded:
            img = _try_extract_embedded(pdf_document, page_number, bbox)
            if img is not None and not _matches_requested_dpi(img, bbox, self.dpi):
                img = None
        if img is None:
            img = pdf_document.crop_region(page_number, bbox, dpi=self.dpi)
        path = os.path.join(self.assets_dir, filename)
        # Pixel dimensions alone aren't enough: a JPEG with no resolution tag
        # displays as "96 DPI" in Windows Explorer/most viewers (their own
        # fallback default for missing metadata) even when the pixel data
        # itself was correctly rendered at self.dpi - so the DPI tag must be
        # written too, not just correct pixel dimensions.
        img.save(path, "JPEG", quality=self.jpeg_quality, dpi=(self.dpi, self.dpi))
        return filename
