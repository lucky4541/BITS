"""Per-page text/image quality metrics and DIGITAL/SCANNED/MIXED/
OCR_REQUIRED/UNKNOWN classification (spec section 3/4/5/46) - built
ENTIRELY on the existing PyMuPDF (fitz) page object's own extraction
(page.get_text/get_images/get_image_info), never on an OCR call. This is
what decides WHETHER a page needs OCR at all (spec: "Do NOT OCR an entire
digital PDF unnecessarily"), so it must work with zero OCR engine
installed - confirmed independent of core/ocr/paddle_engine.py.

Multi-factor, never one simplistic threshold (spec 46): character count,
word count, printable ratio, alphabetic ratio, garbage ratio, repeated-
character ratio, text/image coverage, and coordinate validity all
contribute to the final classification."""
import re
import unicodedata

# Named, adjustable thresholds (spec 46: "do not rely on one simplistic
# threshold" - multiple independent knobs, not one magic number).
MIN_CHARS_FOR_DIGITAL = 20
MIN_ALPHA_RATIO = 0.15
MAX_GARBAGE_RATIO = 0.30
MAX_REPEATED_CHAR_RATIO = 0.25
SCANNED_IMAGE_COVERAGE = 0.60
MIXED_MIN_TEXT_COVERAGE = 0.02

_REPEATED_RUN_RE = re.compile(r"(.)\1{4,}")  # same character 5+ times in a row


def _char_is_garbage(ch: str) -> bool:
    """A control character, the Unicode replacement character (U+FFFD -
    the classic signature of a broken text-extraction codec), or a
    private-use-area codepoint (a common symptom of a broken embedded
    font's cmap, seen in real corrupted-extraction PDFs) - never a
    legitimate accented/Greek/mathematical character (spec: Unicode must
    be preserved, this must never flag real content as garbage)."""
    if ch in ("\n", "\r", "\t"):
        return False
    if ch == "�":
        return True
    cat = unicodedata.category(ch)
    if cat == "Cc":  # control
        return True
    if cat == "Co":  # private use area
        return True
    return False


def analyze_page(pdf_document, page_number: int) -> dict:
    """Returns the raw metrics dict - classify_page() turns this into one
    of the 5 category labels. Kept as two functions so a caller (or a
    future UI "why was this page classified as X" diagnostic) can inspect
    the underlying numbers, not just the final label."""
    return analyze_page_object(pdf_document.get_page(page_number), page_number)


def analyze_page_object(page, page_number: int = 0) -> dict:
    """Same metrics as analyze_page, for a caller that already holds the
    raw fitz.Page object directly (e.g. core.text_extractor, which never
    carries a PDFDocument wrapper at its own call sites) - avoids forcing
    every such caller to construct one just to reach this page-only
    computation. analyze_page itself is now a thin wrapper around this."""
    page_w, page_h = page.rect.width, page.rect.height
    page_area = max(page_w * page_h, 1.0)

    text = page.get_text("text") or ""
    raw = page.get_text("rawdict")

    char_count = len(text)
    word_count = len(text.split())

    printable_count = sum(1 for ch in text if ch.isprintable() or ch in ("\n", "\t"))
    garbage_count = sum(1 for ch in text if _char_is_garbage(ch))
    alpha_count = sum(1 for ch in text if ch.isalpha())
    repeated_hits = sum(len(m.group(0)) for m in _REPEATED_RUN_RE.finditer(text))

    printable_ratio = (printable_count / char_count) if char_count else 0.0
    garbage_ratio = (garbage_count / char_count) if char_count else 0.0
    alpha_ratio = (alpha_count / char_count) if char_count else 0.0
    repeated_ratio = (repeated_hits / char_count) if char_count else 0.0

    text_block_count = 0
    text_area = 0.0
    has_valid_coordinates = False
    for block in raw.get("blocks", []):
        if block.get("type") == 0:  # text block (1 = image block)
            text_block_count += 1
            bbox = block.get("bbox", [0, 0, 0, 0])
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w > 0 and h > 0:
                has_valid_coordinates = True
                text_area += w * h

    image_count = len(page.get_images(full=True))
    image_area = 0.0
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox", [0, 0, 0, 0])
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w > 0 and h > 0:
                image_area += w * h
    except Exception:
        pass  # a malformed image xref must never abort page classification

    return {
        "page_number": page_number,
        "char_count": char_count,
        "word_count": word_count,
        "text_block_count": text_block_count,
        "image_count": image_count,
        "printable_ratio": printable_ratio,
        "alphabetic_ratio": alpha_ratio,
        "garbage_ratio": garbage_ratio,
        "repeated_char_ratio": repeated_ratio,
        "has_valid_coordinates": has_valid_coordinates,
        "text_coverage": min(text_area / page_area, 1.0),
        "image_coverage": min(image_area / page_area, 1.0),
    }


def classify_page(metrics: dict) -> str:
    """DIGITAL | SCANNED | MIXED | OCR_REQUIRED | UNKNOWN (spec section 3).
    Images alone never cause a SCANNED classification (spec 4) - only the
    COMBINATION of high image coverage AND unusable text does."""
    char_count = metrics["char_count"]
    printable_ratio = metrics["printable_ratio"]
    alpha_ratio = metrics["alphabetic_ratio"]
    garbage_ratio = metrics["garbage_ratio"]
    repeated_ratio = metrics["repeated_char_ratio"]
    image_coverage = metrics["image_coverage"]
    text_coverage = metrics["text_coverage"]
    has_coords = metrics["has_valid_coordinates"]

    text_is_bad = (
        char_count < MIN_CHARS_FOR_DIGITAL
        or printable_ratio < 0.5
        or garbage_ratio > MAX_GARBAGE_RATIO
        or repeated_ratio > MAX_REPEATED_CHAR_RATIO
        or (char_count >= MIN_CHARS_FOR_DIGITAL and alpha_ratio < MIN_ALPHA_RATIO)
    )

    if not text_is_bad and char_count >= MIN_CHARS_FOR_DIGITAL and has_coords:
        return "DIGITAL"

    if text_is_bad and image_coverage >= SCANNED_IMAGE_COVERAGE:
        return "SCANNED"

    # A page with literally nothing extractable (no text, no meaningful
    # image either) is genuinely ambiguous - possibly a real blank page,
    # possibly a rendering/extraction problem - not a confident "OCR would
    # find something here" call. Checked BEFORE the generic text_is_bad
    # fallback below, since char_count == 0 always satisfies text_is_bad
    # and would otherwise always win first, making this branch dead code.
    if char_count == 0 and image_coverage < SCANNED_IMAGE_COVERAGE:
        return "UNKNOWN"

    if text_is_bad:
        return "OCR_REQUIRED"

    if image_coverage >= SCANNED_IMAGE_COVERAGE and text_coverage < MIXED_MIN_TEXT_COVERAGE:
        return "MIXED" if char_count >= MIN_CHARS_FOR_DIGITAL else "SCANNED"

    return "MIXED" if char_count >= MIN_CHARS_FOR_DIGITAL else "UNKNOWN"


def is_text_usable(metrics: dict) -> bool:
    """True when the EXISTING digital extraction should be used as-is -
    no OCR call needed for this page (spec: is_text_usable(page))."""
    return classify_page(metrics) in ("DIGITAL", "MIXED")


def needs_ocr(metrics: dict) -> bool:
    """True when this page should go through an OCREngine instead (spec:
    needs_ocr(page)). The exact complement of is_text_usable EXCEPT for
    UNKNOWN, which is neither confidently usable nor confidently
    OCR-worthy - callers should treat UNKNOWN as "ask the user" rather
    than silently picking a side."""
    return classify_page(metrics) in ("SCANNED", "OCR_REQUIRED")
