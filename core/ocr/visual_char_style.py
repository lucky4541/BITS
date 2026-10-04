"""Image/pixel-based style detection for native-PDF character runs.

All italic, bold, superscript, and subscript decisions come from rendered
page pixels and character geometry — never from font name, font family,
font flags, dominant font, or any other font metadata.

Public API:
  detect_line_char_styles(page, line_chars, line_fixed)
      → list of (bold, italic, pos, _) per char, same length as line_chars.
        Elements may be None when pixel analysis was inconclusive.
"""


def detect_line_char_styles(page, line_chars, line_fixed):
    """Pixel-based per-character style for one extracted PDF line.

    Parameters
    ----------
    page:        fitz.Page — the source PDF page (not rendered, used to crop)
    line_chars:  list of rawdict char dicts (each has "bbox", "c", etc.)
    line_fixed:  list of str, same length as line_chars, repaired Unicode text

    Returns
    -------
    list of (bold: bool, italic: bool, pos: str, _) or None per char.
    ``pos`` is "sup", "sub", or "normal".
    A None entry means no reliable pixel measurement could be made for that
    character's word; the caller should fall back gracefully.
    """
    from core.ocr.style_detector import _native_word_style_map  # noqa: PLC0415
    return _native_word_style_map(page, line_chars, line_fixed)
