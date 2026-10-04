"""Character-fidelity layer for digital PDF text extraction.

Core principle (verified, not assumed): PyMuPDF resolves each glyph to a
Unicode character via the PDF's own /ToUnicode CMap (or font encoding).
When that CMap is broken/wrong for one glyph - a real, reproduced defect,
not hypothetical: a hand-corrupted ToUnicode entry for the "f" glyph in an
embedded Arial subset made PyMuPDF report U+00AE (R) in the middle of
"definition" - the glyph drawn on the page is still visually correct, but
every text-extraction call reports the wrong character.

This module never assumes PDF character code = Unicode character for a
SUSPICIOUS character. It cross-references the embedded font's OWN internal
'cmap' table (via fontTools, parsed straight from the font program's raw
bytes - completely independent of the PDF's separate, possibly-corrupted
ToUnicode CMap PyMuPDF normally resolves through) to recover what that
specific glyph actually represents.

Every correction is scoped to (document, font xref, glyph id) - built once
per font and cached. NEVER a global character-substitution table: a
genuine (R) must stay (R); a corrupted glyph that merely LOOKS like (R) but
is really "f" in one specific embedded font must become f, and nowhere
else does (R) -> f happen. See _is_suspicious_char/_font_glyph_unicode_map/
_char_correction_map below for the three-stage detect -> cross-reference ->
correct pipeline (spec section P's required flow).

Integrates into the EXISTING extraction path (core.text_extractor.
extract_lines) as a per-character substitution at the exact point
ch["c"] is normally used - no second/parallel extraction pipeline, no
zone-type-specific special casing. Every zone type already funnels through
extract_lines, so every zone type benefits automatically.
"""
import io
import unicodedata
from collections import OrderedDict

from core import debug_log

try:
    from fontTools.ttLib import TTFont
    _FONTTOOLS_AVAILABLE = True
except ImportError:  # fontTools not installed - fidelity layer becomes a safe no-op, never crashes ZoneTool
    _FONTTOOLS_AVAILABLE = False

_FONT_CMAP_CACHE_MAXSIZE = 32
_font_cmap_cache: "OrderedDict" = OrderedDict()

# Every correction applied this run (spec section T's diagnostic mode) -
# not persisted, not shown anywhere by default; a caller (e.g. a future
# "Show Character Corrections" menu item) can read this list to display
# exactly what was found/fixed, in the RAW/FONT/GLYPH/TOUNICODE/FINAL shape
# the spec's own diagnostic example uses. Cleared by clear_cache() along
# with the two lookup caches, so a fresh document run starts clean.
correction_log: list = []

_CORRECTION_MAP_CACHE_MAXSIZE = 16
_correction_map_cache: "OrderedDict" = OrderedDict()

# Characters that are NEVER legitimate in ordinary extracted body text,
# regardless of context - always suspicious on sight (spec section D).
_ALWAYS_SUSPICIOUS = {"�"}  # U+FFFD REPLACEMENT CHARACTER


def clear_cache():
    _font_cmap_cache.clear()
    _correction_map_cache.clear()
    correction_log.clear()


def _log_correction(page, font_name: str, glyph_id: int, raw_uni: int, true_uni: int, origin):
    """Records one applied correction (spec section T's diagnostic mode:
    ZONE/PAGE/RAW/FONT/GLYPH/ToUnicode/FINAL). Always recorded (cheap - a
    real correction is rare); also mirrored to debug_log when debug logging
    is on, so it shows up alongside every other ZoneTool diagnostic without
    a separate on/off switch to remember."""
    entry = {
        "page": getattr(page, "number", None),
        "font": font_name,
        "glyph_id": glyph_id,
        "raw_char": chr(raw_uni), "raw_code": f"U+{raw_uni:04X}",
        "final_char": chr(true_uni), "final_code": f"U+{true_uni:04X}",
        "origin": origin,
    }
    correction_log.append(entry)
    if debug_log.is_enabled():
        debug_log.log(
            "FIDELITY",
            f"  PAGE: {entry['page']}",
            f"  FONT: {font_name}",
            f"  GLYPH: {glyph_id}",
            f"  RAW: {entry['raw_char']!r} ({entry['raw_code']})",
            f"  FINAL: {entry['final_char']!r} ({entry['final_code']})",
            f"  SOURCE: DIGITAL",
        )


def _is_always_suspicious(ch: str) -> bool:
    if ch in _ALWAYS_SUSPICIOUS:
        return True
    cat = unicodedata.category(ch)
    if cat == "Cc" and ch not in ("\n", "\t", "\r"):  # control character
        return True
    if cat == "Co":  # private-use area
        return True
    return False


def _is_suspicious_char(ch: str, prev_ch: str, next_ch: str) -> bool:
    """A character is suspicious when it's ALWAYS illegitimate (replacement
    char, control char, private-use), OR when it's a non-alphabetic symbol
    sitting BETWEEN two ordinary letters that would otherwise form a normal
    word - e.g. "de(R)nition": (R) is flanked by "e" and "n", both letters,
    which real prose never does mid-word. A symbol between two spaces, at a
    word boundary, or next to punctuation is NOT flagged - that's ordinary
    text (an actual (R)/(C)/(TM) mark next to a brand name, a bullet, a
    section mark), not glyph corruption. This intentionally never fires for
    plain typographic punctuation (dashes, quotes, ellipsis) - those aren't
    symbols "inside a word" in this sense; they sit at word/clause
    boundaries same as any other legitimate punctuation."""
    if _is_always_suspicious(ch):
        return True
    if ch.isalpha() or ch.isspace() or ch.isdigit():
        return False
    if unicodedata.category(ch) in ("Zs", "Pi", "Pf", "Pd", "Po") and ch not in "®©™°":
        # Ordinary punctuation/dash/quote categories are never flagged here
        # UNLESS they're one of the specific marks (R)/(C)/(TM) that are the
        # exact confirmed symptom class (a broken ToUnicode CMap entry
        # landing on one of these particular code points is the reproduced
        # defect) - still only suspicious when flanked by letters, checked
        # below, not on sight.
        return False
    return bool(prev_ch) and bool(next_ch) and prev_ch.isalpha() and next_ch.isalpha()


def _font_info(doc, xref: int) -> dict:
    """{"glyph_map": {glyph_id: true_unicode}, "ps_name": str_or_None},
    built once per (document, font xref) from the embedded font program's
    OWN 'cmap' and 'name' tables - ground truth completely independent of
    the PDF's separate ToUnicode CMap. Returns {"glyph_map": {}, "ps_name":
    None} (never raises) for a font that can't be extracted/parsed (Type3
    bitmap fonts, non-embedded base-14 fonts with no font program to
    extract, unusual formats fontTools can't read) - callers simply get no
    correction available for that font, exactly as if the fidelity layer
    didn't exist for it.

    ps_name exists because page.get_texttrace() identifies a span's font by
    its INTERNAL PostScript name (the font program's own 'name' table
    entry, e.g. "ArialMT") - which does NOT match page.get_fonts()'s own
    basefont ("Arial Regular") or PDF resource name ("F0") for the exact
    same font (confirmed directly: PyMuPDF reports these three differently
    for one identical embedded font). Parsing the font's own name table is
    the only reliable way found to link a texttrace span back to the xref
    get_fonts()/extract_font() need."""
    key = (id(doc), xref)
    cached = _font_cmap_cache.get(key)
    if cached is not None:
        _font_cmap_cache.move_to_end(key)
        return cached

    result = {"glyph_map": {}, "ps_name": None}
    if _FONTTOOLS_AVAILABLE:
        try:
            extracted = doc.extract_font(xref)
            fbuffer = extracted[3] if len(extracted) > 3 else None
            if fbuffer:
                tt = TTFont(io.BytesIO(fbuffer), fontNumber=0, lazy=True)
                if "name" in tt:
                    result["ps_name"] = tt["name"].getDebugName(6)
                glyph_order = tt.getGlyphOrder()
                best_cmap = tt.getBestCmap() or {}
                name_to_unicode = {}
                for uni, gname in best_cmap.items():
                    name_to_unicode.setdefault(gname, uni)
                glyph_map = {}
                for gid, gname in enumerate(glyph_order):
                    uni = name_to_unicode.get(gname)
                    if uni is None:
                        uni = _agl_unicode_for_glyph_name(gname)
                    if uni is not None:
                        glyph_map[gid] = uni
                result["glyph_map"] = glyph_map
        except Exception:  # noqa: BLE001 - a font we can't parse must never crash extraction (Type3/CFF/corrupt/etc)
            result = {"glyph_map": {}, "ps_name": None}

    _font_cmap_cache[key] = result
    if len(_font_cmap_cache) > _FONT_CMAP_CACHE_MAXSIZE:
        _font_cmap_cache.popitem(last=False)
    return result


def _font_xref_for_trace_name(doc, page, trace_font_name: str):
    """Finds the xref (among fonts page.get_fonts() reports) whose OWN
    parsed PostScript name matches trace_font_name - see _font_info's
    docstring for why this indirection is needed at all. Linear over the
    page's own font list (typically 1-5 fonts), and _font_info is cached,
    so this is cheap after the first lookup per page."""
    try:
        fonts = page.get_fonts()
    except Exception:  # noqa: BLE001
        return None
    for f in fonts:
        xref = f[0]
        if _font_info(doc, xref).get("ps_name") == trace_font_name:
            return xref
    return None


def _agl_unicode_for_glyph_name(gname: str):
    """Fallback glyph-name -> Unicode decoding for a glyph that has no
    entry in the font's own cmap (common for a subset font whose cmap only
    covers the characters actually used) but whose NAME still identifies it
    unambiguously via the standard Adobe Glyph List convention - either a
    registered AGL name (e.g. "endash", "quotedblleft") or the "uniXXXX"/
    "uXXXXXX" hex-codepoint convention most font tools emit for glyphs
    outside the registered list."""
    try:
        from fontTools import agl
        if gname in agl.AGL2UV:
            return agl.AGL2UV[gname]
    except ImportError:
        pass
    if gname.startswith("uni") and len(gname) == 7:
        try:
            return int(gname[3:], 16)
        except ValueError:
            return None
    if gname.startswith("u") and 5 <= len(gname) <= 7:
        try:
            return int(gname[1:], 16)
        except ValueError:
            return None
    return None


def _char_correction_map(doc, page) -> dict:
    """{(round(x_origin), round(y_origin)): corrected_char} for exactly the
    characters on this page that are (a) suspicious by context and (b) have
    a font-cmap-derived true value that DIFFERS from what PyMuPDF resolved
    and is itself a plausible replacement (alphabetic, matching the
    surrounding word's shape) - built once per (document, page number),
    cached like core.text_extractor's own rawdict cache. Keyed by rounded
    glyph origin (the same origin get_texttrace and rawdict both report for
    the same content-stream glyph placement), which core.text_extractor
    looks up per-character when assembling extracted text."""
    key = (id(doc), getattr(page, "number", None))
    cached = _correction_map_cache.get(key)
    if cached is not None:
        _correction_map_cache.move_to_end(key)
        return cached

    corrections = {}
    try:
        trace = page.get_texttrace()
    except Exception:  # noqa: BLE001 - never let a diagnostic pass crash real extraction
        trace = []

    xref_by_trace_name = {}

    for span in trace:
        chars = span.get("chars", [])
        font_name = span.get("font")
        if font_name not in xref_by_trace_name:
            xref_by_trace_name[font_name] = _font_xref_for_trace_name(doc, page, font_name)
        xref = xref_by_trace_name[font_name]
        glyph_map = _font_info(doc, xref)["glyph_map"] if xref else {}
        n = len(chars)
        for i, ch_entry in enumerate(chars):
            uni, glyph_id = ch_entry[0], ch_entry[1]
            if not isinstance(uni, int) or uni <= 0:
                continue
            ch = chr(uni)
            prev_ch = chr(chars[i - 1][0]) if i > 0 and isinstance(chars[i - 1][0], int) and chars[i - 1][0] > 0 else ""
            next_ch = chr(chars[i + 1][0]) if i + 1 < n and isinstance(chars[i + 1][0], int) and chars[i + 1][0] > 0 else ""
            if not _is_suspicious_char(ch, prev_ch, next_ch):
                continue
            true_uni = glyph_map.get(glyph_id)
            if true_uni is None or true_uni == uni:
                continue
            true_ch = chr(true_uni)
            # Only accept a correction that actually resolves the
            # suspicion - a replacement that is itself non-alphabetic
            # (when flanked by letters) or still always-suspicious isn't a
            # real fix, so it's rejected rather than substituted blindly.
            if _is_always_suspicious(true_ch):
                continue
            if prev_ch and next_ch and prev_ch.isalpha() and next_ch.isalpha() and not true_ch.isalpha():
                continue
            origin = ch_entry[2] if len(ch_entry) > 2 else None
            if origin is None:
                continue
            corrections[(round(origin[0]), round(origin[1]))] = true_ch
            _log_correction(page, font_name, glyph_id, uni, true_uni, origin)

    _correction_map_cache[key] = corrections
    if len(_correction_map_cache) > _CORRECTION_MAP_CACHE_MAXSIZE:
        _correction_map_cache.popitem(last=False)
    return corrections


def corrected_char(doc, page, ch: str, origin) -> str:
    """Single call-site core.text_extractor.extract_lines uses per
    character: returns ch unchanged unless a font-cmap-verified correction
    exists at this exact glyph origin, in which case the corrected
    character is returned instead. origin: (x, y) as reported by rawdict's
    own per-char "origin" (falls back to bbox top-left when a char has no
    separate origin field, matching how rawdict chars are keyed)."""
    if origin is None:
        return ch
    corrections = _char_correction_map(doc, page)
    if not corrections:
        return ch
    return corrections.get((round(origin[0]), round(origin[1])), ch)
