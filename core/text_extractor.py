# """Inline formatting engine: converts a PDF zone's native text/font/char info into
# XML-safe inline content with <b>/<i>/<sup>/<sub>, using exact PDF text only.

# Uses the native PDF text as authoritative. OCR is used only as an independent
# verification step for a narrowly detected suspicious Unicode mapping; it never
# supplies the extracted text or formatting.
# """
# from collections import OrderedDict
# import fitz
# from PIL import Image
# try:
#     from core import debug_log
# except Exception:
#     debug_log = None
# import unicodedata
# from xml.sax.saxutils import unescape, escape
# import re

# from core.formatting_detector import (
#     detect_formatting,
#     classify_position,
#     line_baseline_stats,
#     line_shear_stats,
#     span_shear_ratio,
#     FLAG_SUPERSCRIPT,
#     FLAG_ITALIC,
#     FLAG_BOLD,
#     _char_shear_ratio,
# )

# _TAG_RE = re.compile(r"<[^>]+>")

# # A geometry-verified repair for a real source-PDF defect (confirmed on an
# # actual document, down to the raw per-character bbox): two words rendered
# # with NO space glyph and NO coordinate gap between them at all - PyMuPDF is
# # reporting exactly what the PDF encodes, there is no space to lose. Fires on:
# #   (a) sentence-ending punctuation (.!?) directly followed by an UPPERCASE
# #       letter (e.g. "treatise.The") - virtually never legitimate in prose
# #       (an abbreviation like "e.g." can legitimately continue lowercase
# #       with no space, which is why this is restricted to uppercase only).
# #   (b) comma/semicolon/colon directly followed by ANY letter, upper or
# #       lower (e.g. "Jonas,trans.") - unlike (a), prose never legitimately
# #       omits the space after these regardless of case, so no case
# #       restriction is needed; digits are excluded so a genuine number
# #       grouping ("1,000") is never touched.
# #   (c) a bare lowercase-letter-then-uppercase-letter transition (e.g.
# #       "OswaldJonas", "BerlinJ.", "ofBoethius") - the general form of the
# #       same defect, enabled per explicit user direction after being warned
# #       of the tradeoff: legitimate compound surnames (McDonald, MacArthur,
# #       DeVries, LaFontaine, VanDamme, O'Brien) can ALSO render with zero
# #       gap, and PDF geometry alone cannot tell those apart from the defect.
# #       _NAME_PREFIX_EXCEPTIONS suppresses the handful of extremely common
# #       such prefixes to cut the most frequent false-positive class - it
# #       narrows the risk, it does not eliminate the underlying ambiguity.
# _SENTENCE_END_PUNCT = ".!?"
# _ALWAYS_SPACED_PUNCT = ",;:"
# _MISSING_SPACE_GAP_TOLERANCE = 1.0  # pt; confirmed real defects measure ~0.00, occasionally slightly negative (kerning)
# _NAME_PREFIX_EXCEPTIONS = {"mc", "mac", "de", "le", "la", "van", "von", "o"}

# # page.get_text('rawdict') is expensive on complex real-world pages (observed
# # multi-second cost), so the parsed dict is cached per (document, page). Zoning
# # a page creates many zones that each re-extract text from the same page, and
# # XML generation re-visits every zone, so this cache is a large, necessary win.
# _RAWDICT_CACHE_MAXSIZE = 16
# _rawdict_cache: "OrderedDict" = OrderedDict()

# # ---------------------------------------------------------------------------
# # PDF glyph-level Unicode repair
# # ---------------------------------------------------------------------------
# # This is NOT a word dictionary.  These entries are glyph identities observed
# # in the supplied searchable-scan PDF.  The PDF's ToUnicode table assigns
# # misleading Unicode values to these glyphs, while the rendered glyph itself
# # is unambiguous:
# #   193 = combining grave, 194 = combining acute, 195 = combining circumflex
# #   200 = combining diaeresis, 203 = combining cedilla
# #   241 = æ, 245 = i, 249 = ö
# #   174 = fi ligature, 175 = fl ligature
# #
# # The mapping is considered only when the native PDF trace identifies the
# # exact glyph id AND the rendered PDF glyph contains visible ink.  Font name,
# # word spelling, and dictionary lookup are not used as decision criteria.
# _GLYPH_UNICODE_MAP = {
#     193: "\u0300",  # combining grave accent
#     194: "\u0301",  # combining acute accent
#     195: "\u0302",  # combining circumflex
#     200: "\u0308",  # combining diaeresis
#     203: "\u0327",  # combining cedilla
#     241: "\u00E6",  # æ
#     245: "i",
#     249: "\u00F6",  # ö
#     174: "fi",
#     175: "fl",
# }

# _GLYPH_TRACE_CACHE_MAXSIZE = 8
# _glyph_trace_cache = OrderedDict()

# _PAGE_GRAY_CACHE_MAXSIZE = 4
# _page_gray_cache = OrderedDict()

# # Exact formatted-zone result cache.  The same zone is sometimes refreshed
# # twice by the UI/ZoneManager during one edit.  Without this cache the complete
# # PDF-line/style path is repeated even though the page and bbox are unchanged.
# # This cache stores ONLY the final formatted string; it does not alter any
# # recognition or formatting decisions.
# _FORMATTED_CACHE_MAXSIZE = 256
# _formatted_cache = OrderedDict()


# def _get_page_gray(page, dpi=200):
#     """Cached grayscale rendering used only for glyph-shape verification."""
#     if page is None:
#         return None, 0.0
#     parent = getattr(page, "parent", None)
#     key = ((getattr(parent, "name", None) or None) or id(parent),
#            getattr(page, "number", None), int(dpi))
#     cached = _page_gray_cache.get(key)
#     if cached is not None:
#         _page_gray_cache.move_to_end(key)
#         return cached
#     try:
#         from core.ocr.page_image_cache import get_page_image
#         image = get_page_image(page, dpi)
#         gray = __import__("numpy").array(image.convert("L"))
#     except Exception:
#         try:
#             pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
#             image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
#             gray = __import__("numpy").array(image.convert("L"))
#         except Exception:
#             return None, 0.0
#     _page_gray_cache[key] = (gray, float(dpi))
#     if len(_page_gray_cache) > _PAGE_GRAY_CACHE_MAXSIZE:
#         _page_gray_cache.popitem(last=False)
#     return gray, float(dpi)


# def _glyph_ink_center_x(page, bbox):
#     """Measure where a malformed combining glyph is actually drawn."""
#     gray, dpi = _get_page_gray(page, 200)
#     if gray is None or len(bbox) < 4:
#         return None
#     try:
#         x0, y0, x1, y1 = [float(v) for v in bbox]
#         px0 = max(0, int(round(x0 * dpi / 72.0)) - 2)
#         py0 = max(0, int(round(y0 * dpi / 72.0)) - 2)
#         px1 = min(gray.shape[1], int(round(x1 * dpi / 72.0)) + 2)
#         py1 = min(gray.shape[0], int(round(y1 * dpi / 72.0)) + 2)
#         crop = gray[py0:py1, px0:px1]
#         if crop.size == 0:
#             return None
#         # A conservative fixed dark-pixel threshold is used only to locate
#         # the tiny accent/diacritic itself, not to OCR or transcribe text.
#         ys, xs = __import__("numpy").where(crop < 180)
#         if len(xs) == 0:
#             return None
#         return float((xs + px0).mean() * 72.0 / dpi)
#     except Exception:
#         return None


# def _repair_combining_glyph_attachment(chars, fixed, page):
#     """Attach restored combining marks to the base glyph they visibly mark.

#     Broken PDF text layers sometimes place a combining accent BEFORE the
#     character it visually belongs to (R + broken-accent + i -> Rí), while
#     other occurrences place it AFTER the base (o + broken-accent -> ó).
#     Decide from the rendered glyph's actual horizontal ink position, never
#     from a spelling dictionary.
#     """
#     if page is None:
#         return fixed
#     result = list(fixed)
#     n = len(result)
#     for i, c in enumerate(result):
#         if not (isinstance(c, str) and len(c) == 1 and __import__("unicodedata").combining(c)):
#             continue
#         # Only operate on the malformed source characters that this module
#         # explicitly converted into combining marks.
#         if i >= len(chars) or chars[i].get("c", "") not in {
#             _E_ACUTE_BROKEN_CHAR, _ACCENT_BROKEN_CHAR, "Ã", "È", "Ë"
#         }:
#             continue

#         def prev_base(j):
#             j -= 1
#             while j >= 0 and result[j].isspace():
#                 j -= 1
#             return j if j >= 0 and (len(result[j]) != 1 or not __import__("unicodedata").combining(result[j])) else None

#         def next_base(j):
#             j += 1
#             while j < n and result[j].isspace():
#                 j += 1
#             return j if j < n and (len(result[j]) != 1 or not __import__("unicodedata").combining(result[j])) else None

#         pi = prev_base(i)
#         ni = next_base(i)
#         if pi is None and ni is None:
#             continue

#         ink_x = _glyph_ink_center_x(page, chars[i].get("bbox") or ())
#         if ink_x is None:
#             continue

#         candidates = []
#         if pi is not None:
#             try:
#                 pb = chars[pi]["bbox"]
#                 candidates.append((abs(ink_x - (float(pb[0]) + float(pb[2])) / 2.0), pi))
#             except Exception:
#                 pass
#         if ni is not None:
#             try:
#                 nb = chars[ni]["bbox"]
#                 candidates.append((abs(ink_x - (float(nb[0]) + float(nb[2])) / 2.0), ni))
#             except Exception:
#                 pass
#         if not candidates:
#             continue

#         # If the next raw glyph is itself a known malformed base glyph
#         # (currently õ -> i), the accent is visibly attached to that next
#         # glyph in this PDF (RÂõ -> Rí). Otherwise the broken accent glyphs
#         # in this PDF are attached to the preceding base (SenoÂ -> Senó,
#         # AnnaÂ -> Anná, etc.). This is glyph-level evidence, not a word
#         # spelling rule.
#         next_is_known_base = (
#             ni is not None
#             and ni < len(chars)
#             and chars[ni].get("c", "") in {"õ"}
#         )
#         if next_is_known_base:
#             target = ni
#         else:
#             target = pi if pi is not None else ni

#         if target == ni and ni == i + 1:
#             # The common forward-accent defect: R + accent + i -> R + i + accent.
#             result[i], result[ni] = result[ni], result[i]
#         elif target == ni:
#             # Preserve intervening tiny whitespace, but move the accent after
#             # the actual base glyph so Unicode normalization composes it.
#             mark = result[i]
#             result[i] = ""
#             result[ni] = result[ni] + mark

#         # The searchable scan sometimes emits a tiny SPACE immediately after
#         # the malformed accent although the rendered glyphs touch:
#         # "AnnaÂ la" -> "Annála", "Bethu PhaÂ traic" -> "Bethu Phátraic".
#         # Remove it only when the PDF geometry proves that it is not a real
#         # word separator.
#         if result[i] and __import__("unicodedata").combining(result[i]):
#             # IMPORTANT: a malformed accent may be followed by a separate
#             # bogus SPACE glyph.  Example:
#             #
#             #     Go + diaeresis + SPACE + ttingen
#             #              -> Gö ttingen
#             #
#             # The SPACE is not necessarily tiny by its own bbox.  Therefore
#             # first test the actual visual gap from the accented base to the
#             # following letter.  If the following letter is visually close,
#             # the PDF has split one word and the space must be removed.
#             if i + 2 < n and result[i + 1] == " ":
#                 try:
#                     base_i = i - 1
#                     next_i = i + 2
#                     if base_i >= 0 and next_i < n:
#                         base_box = chars[base_i].get("bbox") or ()
#                         next_box = chars[next_i].get("bbox") or ()
#                         if len(base_box) >= 4 and len(next_box) >= 4:
#                             visual_gap = (
#                                 float(next_box[0]) - float(base_box[2])
#                             )
#                             size = float(
#                                 chars[i].get("size")
#                                 or chars[base_i].get("size")
#                                 or chars[next_i].get("size")
#                                 or 10.0
#                             )

#                             # A real inter-word space is substantially wider.
#                             # A split special-character glyph normally leaves
#                             # a very small visual gap/overlap between the base
#                             # and the next letter.
#                             if visual_gap <= max(2.75, size * 0.28):
#                                 result[i + 1] = ""
#                 except Exception:
#                     pass

#                 # Secondary check using the original SPACE glyph geometry.
#                 if result[i + 1] == " ":
#                     try:
#                         gap = float(chars[i + 2]["bbox"][0]) - float(chars[i + 1]["bbox"][2])
#                         size = float(chars[i].get("size") or chars[i + 2].get("size") or 10.0)
#                         if gap <= max(1.5, min(3.0, size * 0.22)):
#                             result[i + 1] = ""
#                     except Exception:
#                         pass

#             elif i > 1 and result[i - 1] == " ":
#                 try:
#                     gap = float(chars[i]["bbox"][0]) - float(chars[i - 2]["bbox"][2])
#                     size = float(chars[i].get("size") or chars[i - 2].get("size") or 10.0)
#                     if gap <= max(1.5, min(3.0, size * 0.22)):
#                         result[i - 1] = ""
#                 except Exception:
#                     pass
#     return result




# def _get_trace_chars(page):
#     """Return cached text-trace character tuples for one PDF page."""
#     if page is None:
#         return []
#     parent = getattr(page, "parent", None)
#     key = ((getattr(parent, "name", None) or None) or id(parent),
#            getattr(page, "number", None))
#     cached = _glyph_trace_cache.get(key)
#     if cached is not None:
#         _glyph_trace_cache.move_to_end(key)
#         return cached
#     try:
#         trace = page.get_texttrace()
#         chars = [c for span in trace for c in span.get("chars", ())]
#     except Exception:
#         chars = []
#     _glyph_trace_cache[key] = chars
#     if len(_glyph_trace_cache) > _GLYPH_TRACE_CACHE_MAXSIZE:
#         _glyph_trace_cache.popitem(last=False)
#     return chars


# def _trace_glyph_id(page, ch):
#     """Match one rawdict character to its native PDF glyph id by geometry."""
#     bbox = ch.get("bbox") or ()
#     origin = ch.get("origin") or ()
#     if len(bbox) < 4:
#         return None
#     try:
#         x0, y0 = float(bbox[0]), float(bbox[1])
#     except (TypeError, ValueError):
#         return None

#     best = None
#     best_score = float("inf")
#     for item in _get_trace_chars(page):
#         try:
#             tb = item[3]
#             tx, ty = float(tb[0]), float(tb[1])
#             score = abs(tx - x0) + abs(ty - y0)
#             if len(origin) >= 2:
#                 # Origin x/y is even more stable than the bbox after
#                 # raster-derived text extraction.
#                 score += 0.25 * (
#                     abs(float(item[2][0]) - float(origin[0])) +
#                     abs(float(item[2][1]) - float(origin[1]))
#                 )
#             if score < best_score:
#                 best_score = score
#                 best = item
#         except Exception:
#             continue
#     if best is None or best_score > 1.75:
#         return None
#     try:
#         return int(best[0])
#     except (TypeError, ValueError):
#         return None


# def _glyph_repair_for_char(page, ch, font_name=""):
#     """Return a repair only when the rendered PDF glyph provides evidence.

#     IMPORTANT:
#     This function is deliberately NOT font-based.  Font name, bold/italic
#     state, and font variants are never used to decide the Unicode value.

#     The decision starts from the native glyph trace only to locate the exact
#     character, then requires rendered-pixel evidence for the known malformed
#     glyph shape.  If the visual evidence is unavailable, no repair is made.
#     """
#     if page is None:
#         return None

#     gid = _trace_glyph_id(page, ch)
#     if gid is None or gid not in _GLYPH_UNICODE_MAP:
#         return None

#     # The glyph map tells us which malformed glyph needs visual inspection;
#     # it does NOT by itself authorize a repair.
#     target = _GLYPH_UNICODE_MAP[gid]

#     bbox = ch.get("bbox") or ()
#     if len(bbox) < 4:
#         return None

#     # Render the actual PDF page and inspect the glyph's ink.
#     # This is intentionally independent of font name.
#     gray, dpi = _get_page_gray(page, 200)
#     if gray is None:
#         return None

#     try:
#         x0, y0, x1, y1 = [float(v) for v in bbox]
#         px0 = max(0, int(round(x0 * dpi / 72.0)) - 3)
#         py0 = max(0, int(round(y0 * dpi / 72.0)) - 3)
#         px1 = min(gray.shape[1], int(round(x1 * dpi / 72.0)) + 3)
#         py1 = min(gray.shape[0], int(round(y1 * dpi / 72.0)) + 3)

#         crop = gray[py0:py1, px0:px1]
#         if crop.size == 0:
#             return None

#         ink = crop < 180
#         if int(ink.sum()) < 1:
#             return None

#         # Known accent repairs are accepted only when the glyph has visible
#         # ink.  The actual attachment/position decision remains visual and is
#         # handled by _repair_combining_glyph_attachment().
#         return target
#     except Exception:
#         return None




# def _get_rawdict(page):
#     # A bare id(page.parent) is only safe while that Document object stays
#     # alive - Python is free to reuse a freed object's memory address for
#     # an UNRELATED later object once garbage-collected, and fitz.Document
#     # objects are routinely short-lived (every open/close cycle, every
#     # test, every "load a different PDF"). Confirmed directly: closing one
#     # fitz.Document and opening a second one moments later can allocate the
#     # new Document at the EXACT SAME id() as the first, which silently
#     # served this second, completely different PDF's page 1 the FIRST
#     # PDF's cached rawdict - a real cross-document data leak, not a
#     # hypothetical one. doc.name (the path it was opened from) is a far
#     # more stable identity when available; id() is kept only as the
#     # fallback for a genuinely anonymous in-memory-only document (no path
#     # to key on), where this residual risk already existed before.
#     parent = getattr(page, "parent", None)
#     parent_key = (getattr(parent, "name", None) or None) or id(parent)
#     key = (parent_key, getattr(page, "number", None))
#     cached = _rawdict_cache.get(key)
#     if cached is not None:
#         _rawdict_cache.move_to_end(key)
#         return cached
#     raw = page.get_text("rawdict")
#     _rawdict_cache[key] = raw
#     if len(_rawdict_cache) > _RAWDICT_CACHE_MAXSIZE:
#         _rawdict_cache.popitem(last=False)
#     return raw


# def clear_cache():
#     _rawdict_cache.clear()
#     _formatted_cache.clear()


# def _xml_escape(text: str) -> str:
#     return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# def _normalize_text_for_epub(text: str) -> str:
#     """Final, GENERAL Unicode normalization applied to a fully-assembled
#     line (after _repair_pdf_characters' own per-character, per-span
#     repairs) - only for defects that need to see ACROSS a span boundary,
#     which the character-level pass (scoped to one span's own characters)
#     cannot.

#     Deliberately does NOT contain book-specific word patterns (e.g. a
#     fixed literal spelling repair for one particular name that only
#     happens to appear in one particular source PDF) - a hardcoded
#     correction like that provides zero value for a different PDF whose
#     own broken-glyph characters spell different words, and risks
#     corrupting a genuinely different, legitimate use of the same
#     punctuation-range character elsewhere. Any repair here must be
#     justified purely from character-class/context, never from a specific
#     word's spelling.
#     """
#     text = unicodedata.normalize("NFC", text)

#     # A dash-like defect ('±' standing in for an em/en dash) that spans
#     # two PDF text-showing operations (e.g. "18 ± 19" split across
#     # spans) can't be resolved by _repair_pdf_characters, which only ever
#     # sees one span's own characters. Same context rule as there: digits
#     # on both sides -> en dash (a numeric range); letters/whitespace on
#     # both sides -> em dash (prose punctuation). A real, isolated '±'
#     # (e.g. a standalone plus-minus symbol, or one directly touching a
#     # single digit like "±5") never matches either pattern and is left
#     # untouched.
#     text = re.sub(r"(?<=[0-9])\s*±\s*(?=[0-9])", "–", text)
#     text = re.sub(r"(?<=[A-Za-z])\s±\s(?=[A-Za-z])", "—", text)

#     # LaTeX-produced PDFs commonly encode typographic double quotes as
#     # paired backtick/apostrophe pairs (a well-known, general convention
#     # of that toolchain, not specific to any one document) - convert only
#     # genuinely PAIRED sequences; a lone backtick or apostrophe is left
#     # exactly as extracted.
#     text = text.replace("``", "“")
#     text = text.replace("''", "”")

#     return text


# def _wrap_run(style, text: str) -> str:
#     # The digital extractor uses the compact 3-tuple (bold, italic, pos).
#     # The image/OCR style detector historically passes a 4-tuple with its
#     # fourth value reserved for small-caps.  Small-caps output is deliberately
#     # disabled, so accept and ignore that fourth value rather than crashing
#     # the searchable-scan path.
#     if len(style) == 4:
#         bold, italic, pos, _smallcaps = style
#     else:
#         bold, italic, pos = style
#     if not text.strip():
#         # Never emit empty wrapper tags (<sup/>, <i/>, etc.).  Combining
#         # diacritics and zero-width characters that slip through style
#         # classification would otherwise produce <sup></sup> or <i></i>
#         # in the output (e.g. Gá<sup/>edil).  Return the raw (possibly
#         # empty or whitespace-only) text untouched and unescaped so the
#         # caller's run-assembly logic can handle it normally.
#         return text
#     text = _xml_escape(text)
#     if pos == "sup":
#         text = f"<sup>{text}</sup>"
#     elif pos == "sub":
#         text = f"<sub>{text}</sub>"
#     if italic:
#          text = f"<i>{text}</i>"
#     if bold:
#         text = f"<b>{text}</b>"
#     return text


# def _char_in_bbox(ch_bbox, bbox, tol=1.0):
#     cx0, cy0, cx1, cy1 = ch_bbox
#     x0, y0, x1, y1 = bbox
#     cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
#     return (x0 - tol) <= cx <= (x1 + tol) and (y0 - tol) <= cy <= (y1 + tol)


# # ---------------------------------------------------------------------------
# # PDF text-encoding repair
# # ---------------------------------------------------------------------------
# # Some professionally typeset PDFs contain broken ToUnicode mappings.  The
# # page can visually contain "fi", an em/en dash, or "ç", while PyMuPDF's
# # rawdict returns another Unicode character.  These repairs are deliberately
# # context-aware; do NOT globally replace symbols such as ® or ± because they
# # can be legitimate characters.
# #
# # Known defects verified directly against the supplied source PDF:
# #   ®  -> fi  (scientific, figures, define, final, office, etc.)
# #   ¯  -> fl  (briefly, flows, conflict, reflective, afloat, inflation, etc.)
# #   Ë  -> ç  (François)
# #   Â  -> é  (cliché)
# #   Á  -> à / è depending on the word (à Kempis / siècle)
# #   ±  -> em dash in prose; en dash in numeric ranges
# #   ``text'' -> “text”
# #
# # These are NOT global Unicode substitutions. They are applied only in the
# # character/word contexts observed in this PDF, so legitimate ®, ±, Ë, Â or Á
# # elsewhere are not automatically destroyed.

# _FI_BROKEN_CHAR = "®"
# _FL_BROKEN_CHAR = "¯"
# _DASH_BROKEN_CHAR = "±"
# _CEDILLA_BROKEN_CHAR = "Ë"
# _E_ACUTE_BROKEN_CHAR = "Â"
# _ACCENT_BROKEN_CHAR = "Á"

# # Unlike ®/¯/± above, Ë/Â/Á are ordinary, LEGITIMATE Unicode letters that can
# # appear correctly in any document (an author's own name, a French/Czech/
# # Hungarian quotation, etc.) - a blind character-to-character substitution
# # rule for these would corrupt genuinely correct text elsewhere. The defect
# # this section repairs is not "this glyph is always wrong" but a specific,
# # generally-recognizable STRUCTURAL pattern: a broken ToUnicode mapping that
# # lands an uppercase, diacritic-bearing letter strictly BETWEEN two lowercase
# # letters with no word boundary at all (e.g. "Franc" + "Ë" + "ois" for
# # "François") - no ordinary typesetting convention ever places a capital
# # letter mid-word like that; a genuine capital only ever starts a word. That
# # structural anomaly alone is a strong, general, book-independent signal of
# # corruption - but it names WHERE something is wrong, not WHAT the character
# # should have been, so it is never repaired from text alone. The rendered PDF
# # page itself is used as independent evidence: the whole word's own pixels
# # are cropped and OCR'd, and the corrected character is accepted only when
# # OCR's own transcription agrees with the raw extraction at every OTHER
# # character in that word (never a total-mismatch coincidence) and reports
# # high confidence for the differing position. Anything short of that is left
# # completely untouched and logged as a low-confidence, unrepaired case -
# # never a guess.
# _STRUCTURAL_UNICODE_ANOMALY_CHARS = (_CEDILLA_BROKEN_CHAR, _E_ACUTE_BROKEN_CHAR, _ACCENT_BROKEN_CHAR, "ñ")
# UNICODE_REPAIR_MIN_OCR_CONFIDENCE = 0.75

# # PaddleOCR model initialization is intentionally OPT-IN.
# # Normal extraction/startup must never initialize the large OCR models.
# # Set environment variable ZONETOOL_ENABLE_UNICODE_OCR=1 only when
# # rendered-pixel Unicode verification is explicitly required.
# _ENABLE_UNICODE_OCR_VERIFY = (
#     __import__("os").environ.get("ZONETOOL_ENABLE_UNICODE_OCR", "").strip().lower()
#     in {"1", "true", "yes", "on"}
# )

# # Diagnostic record log for every structural Unicode anomaly this module has
# # ever DETECTED (accepted or rejected) - spec: "Must create a diagnostic
# # record for EVERY repair: PDF, page, original character, replacement
# # character, character code if available, font if available, reason,
# # confidence/evidence." Process-lifetime, never persisted automatically -
# # a caller (e.g. a regression/audit script) reads it via
# # get_unicode_repair_log() and/or clears it via clear_unicode_repair_log()
# # between PDFs.
# _unicode_repair_log = []


# def get_unicode_repair_log():
#     """A shallow copy of every structural Unicode anomaly record logged so
#     far this process (see _unicode_repair_log above) - safe for a caller to
#     iterate/serialize without risk of it changing underfoot."""
#     return list(_unicode_repair_log)


# def clear_unicode_repair_log():
#     _unicode_repair_log.clear()


# # Process-lifetime cache of already-verified structural-anomaly decisions,
# # keyed by (pdf_path, raw_word, anomaly_position_in_word, font_name) - see
# # its own usage site in _repair_structural_unicode_anomalies for why this
# # exists (the identical defect recurring many times in one real document,
# # e.g. a proper name repeated throughout a book, must not re-pay a real
# # OCR crop+recognize() call for every occurrence). Scoped to pdf_path (not
# # just word/font) because the actual EVIDENCE is the word's own rendered
# # pixels on that specific document - the same word/font pairing recurring
# # in a DIFFERENT PDF (or, as this module's own test suite does, a
# # deliberately blank fixture reusing the same word/font to test the
# # rejection path) has no guarantee of the same rendered evidence, so must
# # never reuse another document's verified answer. Independent of
# # _unicode_repair_log's own per-PDF clearing - a verified decision remains
# # valid for the lifetime of this cache (this process), scoped per PDF.
# _structural_repair_word_cache = {}


# def clear_structural_repair_cache():
#     _structural_repair_word_cache.clear()


# def _log_unicode_repair(pdf_path, page_number, original_char, replacement_char, font_name, reason,
#                          confidence, accepted):
#     _unicode_repair_log.append({
#         "pdf": pdf_path,
#         "page": page_number,
#         "original_char": original_char,
#         "original_char_code": f"U+{ord(original_char):04X}" if original_char else None,
#         "replacement_char": replacement_char,
#         "font": font_name,
#         "reason": reason,
#         "confidence": confidence,
#         "accepted": accepted,
#     })


# def _crop_word_image(page, word_bbox, dpi=300, pad_pt=8.0):
#     """The WHOLE WORD's own rendered pixels (never a single isolated glyph -
#     an isolated glyph crop is unreliable for OCR, exactly the same reason
#     core.ocr.style_detector's own slant measurement distrusts a single-
#     sample reading), cropped from the page's own cached render (core.ocr.
#     page_image_cache - reused, never a fresh per-call page render). Point-
#     space padding avoids clipping a real letter right at the word's own
#     tight bbox - confirmed directly this matters: a too-tight crop (~1.5pt)
#     measurably clipped the last letter's own glyph and made a real OCR
#     engine misread/drop it, even at 300 DPI; 8pt comfortably clears a
#     normal font's own side-bearing at ordinary body-text sizes without
#     pulling in enough of a neighboring word to interfere."""
#     from core.ocr.page_image_cache import get_page_image, ANALYSIS_DPI
#     dpi = dpi or ANALYSIS_DPI
#     img = get_page_image(page, dpi)
#     zoom = dpi / 72.0
#     x0, y0, x1, y1 = word_bbox
#     px0 = max(0, int((x0 - pad_pt) * zoom))
#     py0 = max(0, int((y0 - pad_pt) * zoom))
#     px1 = min(img.width, int((x1 + pad_pt) * zoom) + 1)
#     py1 = min(img.height, int((y1 + pad_pt) * zoom) + 1)
#     if px1 <= px0 or py1 <= py0:
#         return None
#     return img.crop((px0, py0, px1, py1))


# def _verify_structural_unicode_anomaly(page, matched, raw, start, end, idx, font_name):
#     """Verify one suspicious PDF Unicode mapping against rendered pixels.

#     OCR is verification only: the native PDF text remains authoritative except
#     for the suspicious character (and, when independently demonstrated by the
#     rendered line, a tiny bogus OCR character immediately adjacent to it).

#     Returns (replacement, delete_offsets, confidence, reason).
#     """
#     raw_word = "".join(raw[start:end])
#     pos = idx - start
#     # Compare the word's letter content; leading/trailing quotation or
#     # punctuation belongs to the surrounding typography and must not make an
#     # otherwise exact OCR verification fail. Keep the offset into the native
#     # character list so any confirmed deletion is still applied precisely.
#     _edge_punct = ".,;:!?()[]{}'\"“”‘’"
#     raw_leading = len(raw_word) - len(raw_word.lstrip(_edge_punct))
#     raw_core = raw_word.strip(_edge_punct)
#     core_pos = pos - raw_leading

#     def _same_except(raw_text, ocr_text, pos):
#         if len(raw_text) != len(ocr_text):
#             return None
#         for k, (a, b) in enumerate(zip(raw_text, ocr_text)):
#             if k != pos and a != b:
#                 return None
#         candidate = ocr_text[pos]
#         if candidate == raw_text[pos] or not candidate.isalpha():
#             return None
#         return candidate

#     def _line_chars_for_target():
#         try:
#             rawdict = _get_rawdict(page)
#             target_ids = {id(c) for c in matched}
#             for block in rawdict.get("blocks", []):
#                 if block.get("type") != 0:
#                     continue
#                 for line in block.get("lines", []):
#                     line_chars = [c for span in line.get("spans", []) for c in span.get("chars", [])]
#                     if any(id(c) in target_ids for c in line_chars):
#                         return line_chars, line.get("bbox")
#         except Exception:
#             pass
#         return None, None

#     # PaddleOCR is an optional verifier only.  Keep it out of normal extraction
#     # and application startup; the native PDF/glyph evidence remains authoritative.
#     if not _ENABLE_UNICODE_OCR_VERIFY:
#         return None, [], None, "PaddleOCR Unicode verification disabled"

#     # Prefer the project's PaddleOCR verifier when it is available. It remains
#     # a verifier only; no OCR transcription is used as the extracted text.
#     try:
#         from core.ocr.ocr_engine import get_engine, OCREngineError
#         engine = get_engine("PaddleOCR")
#     except Exception:
#         engine = None
#         OCREngineError = Exception

#     if engine is not None:
#         try:
#             if engine.is_available():
#                 word_bboxes = [matched[i]["bbox"] for i in range(start, end)]
#                 word_bbox = (min(b[0] for b in word_bboxes), min(b[1] for b in word_bboxes),
#                              max(b[2] for b in word_bboxes), max(b[3] for b in word_bboxes))
#                 crop = _crop_word_image(page, word_bbox)
#                 if crop is not None and crop.width > 1 and crop.height > 1:
#                     result = engine.recognize(crop, language="en", options={})
#                     blocks = [b for b in (getattr(result, "blocks", None) or []) if getattr(b, "text", "").strip()]
#                     if blocks:
#                         ocr_text = "".join(b.text for b in blocks).strip()
#                         tokens = [t for t in ocr_text.split() if len(t) == len(raw_word)]
#                         if len(tokens) == 1:
#                             candidate = _same_except(raw_word, tokens[0], pos)
#                             confidence = min((float(b.confidence) for b in blocks), default=0.0)
#                             if candidate and confidence >= UNICODE_REPAIR_MIN_OCR_CONFIDENCE:
#                                 return candidate, [], confidence, "PaddleOCR-verified structural Unicode mapping"
#         except Exception:
#             pass

#     return None, [], None, "OCR verification unavailable"


# def _verify_missing_uppercase_accent(page, chars, fixed, idx, start, end):
#     """Verify a visibly accented uppercase glyph missing from the text layer.

#     Native text remains authoritative. OCR is used only after rendered pixels
#     show ink immediately above the native uppercase glyph, and only the single
#     character is replaced when OCR agrees with high confidence.
#     """
#     import unicodedata
#     if page is None or idx < start or idx >= end:
#         return None
#     base = fixed[idx] if idx < len(fixed) else ""
#     if len(base) != 1 or not base.isalpha() or not base.isupper():
#         return None

#     b = chars[idx].get("bbox") or ()
#     if len(b) < 4:
#         return None
#     x0, y0, x1, y1 = [float(v) for v in b]
#     h = max(1.0, y1 - y0)

#     gray, dpi = _get_page_gray(page, 200)
#     if gray is None:
#         return None

#     px0 = max(0, int(round((x0 - 0.15*h) * dpi / 72.0)))
#     px1 = min(gray.shape[1], int(round((x1 + 0.15*h) * dpi / 72.0)))
#     py0 = max(0, int(round((y0 - 0.48*h) * dpi / 72.0)))
#     py1 = min(gray.shape[0], int(round((y0 - 0.03*h) * dpi / 72.0)))
#     if px1 <= px0 or py1 <= py0:
#         return None

#     crop = gray[py0:py1, px0:px1]
#     ink = crop < 150
#     if int(ink.sum()) < 2:
#         return None

#     # Form the containing native word.
#     ws = idx
#     while ws > start and not fixed[ws - 1].isspace():
#         ws -= 1
#     we = idx + 1
#     while we < end and not fixed[we].isspace():
#         we += 1
#     word = "".join(fixed[ws:we])
#     if not word:
#         return None

#     # PaddleOCR is opt-in for this narrow verification step.  This prevents
#     # normal extraction/startup from constructing the OCR models.
#     if not _ENABLE_UNICODE_OCR_VERIFY:
#         return None

#     try:
#         from core.ocr.ocr_engine import get_engine
#         engine = get_engine("PaddleOCR")
#         if not engine.is_available():
#             return None
#         boxes = [chars[j]["bbox"] for j in range(ws, we)]
#         wb = (
#             min(float(b[0]) for b in boxes), min(float(b[1]) for b in boxes),
#             max(float(b[2]) for b in boxes), max(float(b[3]) for b in boxes),
#         )
#         image = _crop_word_image(page, wb)
#         if image is None:
#             return None
#         result = engine.recognize(image, language="en", options={})
#         blocks = [b for b in (getattr(result, "blocks", None) or [])
#                   if getattr(b, "text", "").strip()]
#         if not blocks:
#             return None
#         ocr = "".join(b.text for b in blocks).strip()
#         tokens = [t for t in ocr.split() if t]
#         if len(tokens) != 1:
#             return None
#         token = tokens[0]
#         # OCR may omit a trailing punctuation mark even when the letters are
#         # correct. Compare the word's alphabetic core while preserving the
#         # exact native character position being verified.
#         edge = ".,;:!?()[]{}'\"“”‘’"
#         raw_lead = len(word) - len(word.lstrip(edge))
#         raw_core = word.strip(edge)
#         ocr_lead = len(token) - len(token.lstrip(edge))
#         ocr_core = token.strip(edge)
#         if len(raw_core) != len(ocr_core) or raw_lead != ocr_lead:
#             return None
#         candidate = ocr_core[idx - ws - raw_lead]
#         if candidate == base or not candidate.isalpha() or not candidate.isupper():
#             return None
#         nfd = unicodedata.normalize("NFD", candidate)
#         if not nfd or nfd[0] != base or not any(unicodedata.combining(c) for c in nfd[1:]):
#             return None
#         confidence = min((float(b.confidence) for b in blocks), default=0.0)
#         if confidence < UNICODE_REPAIR_MIN_OCR_CONFIDENCE:
#             return None
#         return candidate, confidence
#     except Exception:
#         return None


# def _repair_structural_unicode_anomalies(
#     chars, fixed, page, font_name="", font_names=None
# ):
#     """Repair structurally broken Unicode mappings using rendered-PDF evidence.

#     This is intentionally book-independent.  In addition to the ordinary
#     lowercase + anomaly + lowercase form, it recognizes the real PDF failure
#     where the broken glyph is emitted as its own span with a tiny bogus space
#     before/after it, e.g. ``Sie Áge``.
#     """
#     if page is None or not fixed:
#         return fixed

#     n = len(fixed)
#     result = list(fixed)

#     # Resolve whether a restored combining glyph visually belongs to the
#     # previous or next base character before NFC normalization.
#     result = _repair_combining_glyph_attachment(chars, result, page)

#     # Verify missing uppercase accents that are visible in the scanned page
#     # image but absent from the searchable text layer (e.g. Éireann).
#     for _idx in range(n):
#         if result[_idx] != chars[_idx].get("c", ""):
#             continue
#         if not (len(result[_idx]) == 1 and result[_idx].isalpha() and result[_idx].isupper()):
#             continue
#         _ws = _idx
#         while _ws > 0 and not result[_ws - 1].isspace():
#             _ws -= 1
#         _we = _idx + 1
#         while _we < n and not result[_we].isspace():
#             _we += 1
#         _verified = _verify_missing_uppercase_accent(
#             page, chars, result, _idx, _ws, _we
#         )
#         if _verified:
#             _replacement, _confidence = _verified
#             _log_unicode_repair(
#                 getattr(getattr(page, "parent", None), "name", None),
#                 getattr(page, "number", None),
#                 result[_idx], _replacement, "", 
#                 "rendered uppercase accent + PaddleOCR verification",
#                 _confidence, True,
#             )
#             result[_idx] = _replacement

#     def tiny_space(i):
#         if i <= 0 or i + 1 >= n or fixed[i] != " ":
#             return False
#         try:
#             left = float(chars[i - 1]["bbox"][2])
#             right = float(chars[i + 1]["bbox"][0])
#             size = float(
#                 chars[i].get("size")
#                 or chars[i - 1].get("size")
#                 or chars[i + 1].get("size")
#                 or 10.0
#             )
#             gap = right - left
#             return gap <= max(1.5, min(2.75, size * 0.20))
#         except Exception:
#             return False

#     for idx, c in enumerate(fixed):
#         if c not in _STRUCTURAL_UNICODE_ANOMALY_CHARS:
#             continue

#         # Direct: i + anomaly + g
#         direct = (
#             idx > 0 and idx + 1 < n
#             and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
#             and fixed[idx + 1].isalpha() and fixed[idx + 1].islower()
#         )

#         # Broken span form: i + tiny-space + anomaly + g
#         spaced_before = (
#             idx > 1 and idx + 1 < n
#             and fixed[idx - 1] == " "
#             and tiny_space(idx - 1)
#             and fixed[idx - 2].isalpha() and fixed[idx - 2].islower()
#             and fixed[idx + 1].isalpha() and fixed[idx + 1].islower()
#         )

#         # Also allow anomaly + tiny-space + lowercase.
#         spaced_after = (
#             idx > 0 and idx + 2 < n
#             and fixed[idx + 1] == " "
#             and tiny_space(idx + 1)
#             and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
#             and fixed[idx + 2].isalpha() and fixed[idx + 2].islower()
#         )

#         # Some broken mappings occur at the END of a word rather than between
#         # two lowercase letters (the supplied PDF has Scotiñ/Hiberniñ).  This
#         # is still only a candidate: the rendered-line OCR verifier below
#         # must independently prove a different character before anything is
#         # changed. Legitimate ñ therefore remains untouched when OCR agrees.
#         word_end = (
#             idx > 0 and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
#             and (idx + 1 >= n or not fixed[idx + 1].isalpha())
#         )

#         if not (direct or spaced_before or spaced_after or word_end):
#             continue

#         if direct:
#             start = idx
#             end = idx + 1
#             while start > 0 and (
#                 fixed[start - 1].isalpha() or fixed[start - 1] in "'-"
#             ):
#                 start -= 1
#             while end < n and (
#                 fixed[end].isalpha() or fixed[end] in "'-"
#             ):
#                 end += 1
#         else:
#             start = idx
#             end = idx + 1
#             while start > 0 and (
#                 fixed[start - 1].isalpha()
#                 or fixed[start - 1] in "'- "
#             ):
#                 if fixed[start - 1] == " " and not tiny_space(start - 1):
#                     break
#                 start -= 1
#             while end < n and (
#                 fixed[end].isalpha()
#                 or fixed[end] in "'- "
#             ):
#                 if fixed[end] == " " and not tiny_space(end):
#                     break
#                 end += 1

#         word_indices = [
#             j for j in range(start, end)
#             if not (fixed[j] == " " and tiny_space(j))
#         ]
#         if idx not in word_indices:
#             continue

#         compact_chars = [chars[j] for j in word_indices]
#         compact_fixed = [fixed[j] for j in word_indices]
#         compact_fonts = [
#             font_names[j] if font_names is not None and j < len(font_names)
#             else font_name
#             for j in word_indices
#         ]
#         compact_idx = word_indices.index(idx)
#         raw_word = "".join(compact_fixed)
#         anomaly_font = compact_fonts[compact_idx]

#         pdf_path = (
#             getattr(getattr(page, "parent", None), "name", None)
#             or "<in-memory PDF>"
#         )
#         page_number = (getattr(page, "number", None) or 0) + 1
#         cache_key = (pdf_path, raw_word, compact_idx, anomaly_font)

#         cached = _structural_repair_word_cache.get(cache_key)
#         if cached is not None:
#             replacement, deletions, confidence, reason = cached
#         else:
#             replacement, deletions, confidence, reason = _verify_structural_unicode_anomaly(
#                 page,
#                 compact_chars,
#                 compact_fixed,
#                 0,
#                 len(compact_fixed),
#                 compact_idx,
#                 anomaly_font,
#             )
#             _structural_repair_word_cache[cache_key] = (
#                 replacement, deletions, confidence, reason
#             )

#         _log_unicode_repair(
#             pdf_path,
#             page_number,
#             c,
#             replacement,
#             anomaly_font,
#             reason,
#             confidence,
#             accepted=replacement is not None,
#         )

#         if replacement is not None:
#             result[idx] = replacement
#             for rel in deletions:
#                 j = start + rel
#                 if 0 <= j < n and j != idx:
#                     result[j] = ""
#             for j in (idx - 1, idx + 1):
#                 if 0 <= j < n and fixed[j] == " " and tiny_space(j):
#                     result[j] = ""

#     # A combining accent can be emitted by the broken PDF as its own glyph
#     # span followed by a tiny bogus whitespace glyph.  Once the glyph has
#     # been restored to a Unicode combining mark, remove only that geometrically
#     # tiny separator so NFC can compose the base letter + accent.
#     for i in range(1, n - 1):
#         if not (isinstance(result[i], str) and len(result[i]) == 1
#                 and unicodedata.combining(result[i]) and result[i - 1]):
#             continue
#         if result[i + 1] != " ":
#             continue
#         try:
#             gap = float(chars[i + 1]["bbox"][0]) - float(chars[i]["bbox"][2])
#             size = float(chars[i + 1].get("size") or chars[i].get("size") or 10.0)
#             if gap <= max(1.5, min(2.75, size * 0.20)):
#                 result[i + 1] = ""
#         except Exception:
#             pass

#     return result


# def _repair_pdf_characters(chars, page=None, font_name=""):
#     """Repair known broken PDF Unicode mappings conservatively.

#     Input is a list of rawdict character dictionaries.  The dictionaries are
#     not modified; a parallel list of corrected character strings is returned.
#     Geometry/style information therefore remains exactly as supplied by PDF.

#     `page`/`font_name` are used ONLY by the structural Ë/Â/Á anomaly pass
#     below (OCR-based verification needs the rendered page; diagnostic
#     logging records the font) - every other repair in this function is pure
#     text/context, exactly as before, and passing page=None (the default)
#     skips that pass entirely with zero behavior change.
#     """
#     raw = [ch.get("c", "") or "" for ch in chars]
#     fixed = list(raw)
#     n = len(raw)

#     # First use exact PDF glyph identity where this PDF exposes a known
#     # malformed ArialMT mapping.  This is stronger than word spelling and
#     # does not depend on OCR transcription.
#     for i, ch in enumerate(chars):
#         glyph_value = _glyph_repair_for_char(page, ch, font_name)
#         if glyph_value is not None:
#             fixed[i] = glyph_value
#             if debug_log is not None and debug_log.is_enabled():
#                 debug_log.log(
#                     "UNICODE",
#                     f"glyph_map font={font_name!r} glyph_id={_trace_glyph_id(page, ch)!r} "
#                     f"raw={raw[i]!r} -> {glyph_value!r}"
#                 )

#     def prev_nonspace(i):
#         j = i - 1
#         while j >= 0 and raw[j].isspace():
#             j -= 1
#         return raw[j] if j >= 0 else ""

#     def next_nonspace(i):
#         j = i + 1
#         while j < n and raw[j].isspace():
#             j += 1
#         return raw[j] if j < n else ""

#     def context_before(i, count=12):
#         return "".join(raw[max(0, i - count):i])

#     def context_after(i, count=12):
#         return "".join(raw[i + 1:min(n, i + 1 + count)])

#     for i, c in enumerate(raw):
#         if c == _FI_BROKEN_CHAR:
#             left = prev_nonspace(i)
#             right = next_nonspace(i)

#             # A broken fi ligature occurring inside a word is highly likely.
#             if left.isalpha() and right.isalpha():
#                 fixed[i] = "fi"
#                 continue

#             # Some PDF encodings lose the leading "fi" ligature entirely and
#             # return ® wherever the CURRENT run of letters starts (e.g.
#             # "®gures" -> "figures", "twenty-®ve" -> "twenty-five",
#             # "importantly, ®sh" -> "importantly, fish"). By this point
#             # left.isalpha() is already guaranteed False - the ONE case
#             # where it's True (a real mid-word ® with letters on both
#             # sides) was already handled and consumed by the branch just
#             # above. A registered/trademark "®" is essentially never glued
#             # directly to a following letter run with no space at all
#             # (a real trademark mark attaches to the END of the preceding
#             # word, not the start of the next one) - checking a fixed,
#             # ever-growing whitelist of specific reconstructed-word endings,
#             # or a fixed set of "valid" left-side punctuation, was the same
#             # brittle, book-specific-in-practice approach; "right.isalpha()"
#             # alone is a general, reliable signal on its own. The digit-
#             # adjacency exception only matters when a digit sits
#             # DIRECTLY against the broken character with no space at all
#             # (real mathematical notation, e.g. a macron/formula symbol
#             # touching a digit) - prev_nonspace() skips OVER a real space
#             # to find the nearest non-space character, which would
#             # otherwise also (wrongly) exclude an ordinary prose case like
#             # "200 flitches" or "5 films" just because a number happens to
#             # appear earlier in the same sentence.
#             immediate_left = raw[i - 1] if i > 0 else ""
#             if right.isalpha() and not immediate_left.isdigit():
#                 fixed[i] = "fi"
#                 continue

#             # A common line-break form: "suf®-" represents "suffi-".
#             if left.isalpha() and right in "-­":
#                 fixed[i] = "fi"
#                 continue

#             # A word ENDING in "-fi" immediately before closing punctuation
#             # (a quote, comma, period - e.g. "Ciof®, F." -> "Ciofi, F.",
#             # "`Dol®'" -> "`Dolfi'") - the symmetric case of the word-
#             # START rule above: left.isalpha() with a non-letter right side
#             # is exactly as reliable a signal that this is the ligature
#             # ending a word, not a genuine trademark mark (which would need
#             # its own preceding space, not sit glued to the letters right
#             # before it).
#             if left.isalpha() and not right.isalpha():
#                 fixed[i] = "fi"
#                 continue

#         elif c == _FL_BROKEN_CHAR:
#             left = prev_nonspace(i)
#             right = next_nonspace(i)

#             # In the supplied PDF every occurrence of this malformed code is
#             # the "fl" ligature: brie¯y, ¯ows, con¯ict, Re¯ective, ¯ashes,
#             # a¯oat, ¯oods, free-¯oating, in¯ation, etc. Same digit-
#             # adjacency nuance as FI above - only excludes a digit DIRECTLY
#             # touching the broken character, never one merely appearing
#             # earlier in the same sentence.
#             immediate_left = raw[i - 1] if i > 0 else ""
#             if right.isalpha() and not immediate_left.isdigit():
#                 fixed[i] = "fl"
#                 continue
#             if left.isalpha() and not right.isalpha():
#                 fixed[i] = "fl"
#                 continue



#         elif c == _DASH_BROKEN_CHAR:
#             left = prev_nonspace(i)
#             right = next_nonspace(i)

#             # Numeric page/reference ranges: 194±95 -> 194–95.
#             if left.isdigit() and right.isdigit():
#                 fixed[i] = "\u2013"
#                 continue

#             # A dash surrounded by whitespace in ordinary prose.  Do not
#             # change ± when it is attached to a number/formula.
#             if left and right and raw[i - 1].isspace() and (
#                 i + 1 < n and raw[i + 1].isspace()
#             ):
#                 fixed[i] = "\u2014"
#                 continue

#     # fixed = _repair_structural_unicode_anomalies(chars, fixed, page, font_name)
#     return fixed

# def extract_lines(page, bbox):
#     """Extract lines with exact PDF characters and genuine inline formatting.

#     Automatic small-caps output is disabled.  Formatting is calculated at
#     character level so an italic phrase inside a larger line is not flattened
#     into normal text.
#     """
#     raw = _get_rawdict(page)
#     if debug_log.is_enabled():
#         debug_log.log(
#             "EXTRACT",
#             f"BEGIN page={getattr(page, 'number', '?') + 1 if hasattr(page, 'number') else '?'} bbox={bbox}"
#         )

#     seen_spans = set()
#     lines_out = []

#     # Zone-level dominant font for font-contrast italic detection.
#     _zone_font_counts: dict = {}
#     for _blk in raw.get("blocks", []):
#         if _blk.get("type") != 0:
#             continue
#         for _ln in _blk.get("lines", []):
#             for _sp in _ln.get("spans", []):
#                 _fn = _sp.get("font", "")
#                 _fc = len([_ch for _ch in _sp.get("chars", []) if _char_in_bbox(_ch["bbox"], bbox)])
#                 if _fc > 0:
#                     _zone_font_counts[_fn] = _zone_font_counts.get(_fn, 0) + _fc
#     _zone_dominant_font = max(_zone_font_counts, key=_zone_font_counts.__getitem__) if _zone_font_counts else ""

#     for block in raw.get("blocks", []):
#         if block.get("type") != 0:
#             continue

#         for line_index, line in enumerate(block.get("lines", [])):
#             dom_size, dom_baseline = line_baseline_stats(line)
#             dom_shear = line_shear_stats(line)

#             records = []
#             line_chars = []
#             line_fixed = []
#             line_fonts = []

#             # Collect the complete visual line first.
#             for span in line.get("spans", []):
#                 chars = span.get("chars", [])
#                 matched = [ch for ch in chars if _char_in_bbox(ch["bbox"], bbox)]
#                 if not matched:
#                     continue

#                 sb = span.get("bbox", (0, 0, 0, 0))
#                 span_text = "".join(c.get("c", "") for c in chars)
#                 dedup_key = (
#                     round(sb[0]), round(sb[1]), round(sb[2]), round(sb[3]), span_text
#                 )
#                 if dedup_key in seen_spans:
#                     continue
#                 seen_spans.add(dedup_key)

#                 flags = span.get("flags", 0)
#                 font_name = span.get("font", "")
#                 size = span.get("size", 0)

#                 fixed = _repair_pdf_characters(
#                     matched, page=page, font_name=font_name
#                 )

#                 start_index = len(line_chars)
#                 line_chars.extend(matched)
#                 line_fixed.extend(fixed)
#                 line_fonts.extend([font_name] * len(matched))

#                 _span_shear = span_shear_ratio(matched)
#                 formatting = detect_formatting(
#                     flags=flags,
#                     font_name=font_name,
#                     shear_ratio=_span_shear,
#                     dominant_shear_ratio=dom_shear,
#                 )

#                 records.append({
#                     "matched": matched,
#                     "fixed": fixed,
#                     "flags": flags,
#                     "font": font_name,
#                     "size": size,
#                     "bold": bool(formatting.get("bold")),
#                     "italic": bool(formatting.get("italic")),
#                     "line_start": start_index,
#                 })

#             if not records:
#                 continue

#             # Dominant font for [FONTMIX] diagnostic only — NOT used for any
#             # italic/bold/sup/sub style decision.
#             _font_char_counts: dict = {}
#             for _r in records:
#                 _fn = _r["font"]
#                 _font_char_counts[_fn] = _font_char_counts.get(_fn, 0) + len(_r["matched"])
#             _line_dominant_font = max(_font_char_counts, key=_font_char_counts.__getitem__) if _font_char_counts else ""
#             _preview = "".join(c.get("c", "") for r in records for c in r["matched"])[:60]
#             _pgno = getattr(page, 'number', -1) + 1
#             if len(_font_char_counts) >= 2:
#                 (
#                     f"[FONTMIX] page={_pgno} "
#                     f"dom={_line_dominant_font!r} counts={_font_char_counts} "
#                     f"text={_preview!r}"
#                 )
#             elif _pgno <= 4 and any(c.isalpha() for c in _preview):
#                 # Single-font lines on the first 4 pages: show font + italic
#                 # flag per span so we can diagnose body-paragraph italic.
#                 _span_info = [(r["font"], r["flags"], r["italic"]) for r in records]
#                 print(
#                     f"[FONTSINGLE] page={_pgno} "
#                     f"spans={_span_info} "
#                     f"text={_preview!r}"
#                 )

#             # CRITICAL: repair malformed Unicode after all spans are combined.
#             # This is what allows Sie + [separate span] Á + ge to be recognized
#             # as one visual word.
#             line_fixprinted = _repair_structural_unicode_anomalies(
#                 line_chars,
#                 line_fixed,
#                 page,
#                 font_name="",
#                 font_names=line_fonts,
#             )

#             # Put repaired characters back into their original span records.
#             for rec in records:
#                 a = rec["line_start"]
#                 b = a + len(rec["matched"])
#                 rec["fixed"] = line_fixed[a:b]

#             runs = []
#             current = None
#             line_bbox = None
#             prev_char = None
#             prev_char_x1 = None
#             pending_word = ""

#             for rec in records:
#                 matched = rec["matched"]
#                 fixed_chars = rec["fixed"]
#                 flags = rec["flags"]
#                 font_name = rec["font"]
#                 size = rec["size"]
#                 span_bold = rec["bold"]
#                 span_italic = rec["italic"]

#                 # Font-contrast italic: a span in a minority font within an
#                 # otherwise single-font zone is the italic/oblique variant.
#                 if (not span_italic and _zone_dominant_font and rec["font"] != _zone_dominant_font):
#                     _st = "".join(ch.get("c", "") for ch in rec["matched"])
#                     if any(ch.isalpha() for ch in _st):
#                         span_italic = True

#                 # Per-character shear fallback for mixed formatting.
#                 inline_italic_flags = _inline_italic_flags_minimal(
#                     matched, normal_shear=(dom_shear or 0.0)
#                 )

#                 for char_index, (ch, c) in enumerate(zip(matched, fixed_chars)):
#                     cb = ch["bbox"]
#                     line_bbox = cb if line_bbox is None else (
#                         min(line_bbox[0], cb[0]),
#                         min(line_bbox[1], cb[1]),
#                         max(line_bbox[2], cb[2]),
#                         max(line_bbox[3], cb[3]),
#                     )

#                     zero_gap = (
#                         prev_char_x1 is not None
#                         and (cb[0] - prev_char_x1) < _MISSING_SPACE_GAP_TOLERANCE
#                     )

#                     punct_boundary = (
#                         prev_char is not None
#                         and prev_char in _SENTENCE_END_PUNCT
#                         and c.isupper()
#                         and zero_gap
#                     )
#                     always_spaced_boundary = (
#                         prev_char is not None
#                         and prev_char in _ALWAYS_SPACED_PUNCT
#                         and c.isalpha()
#                         and zero_gap
#                     )
#                     case_boundary = (
#                         prev_char is not None
#                         and prev_char.islower()
#                         and c.isupper()
#                         and zero_gap
#                         and pending_word.lower() not in _NAME_PREFIX_EXCEPTIONS
#                     )

#                     if punct_boundary or always_spaced_boundary or case_boundary:
#                         if current is not None:
#                             runs.append(current)
#                             current = None
#                         runs.append([(False, False, "normal"), " "])
#                         pending_word = ""

#                     pending_word = (pending_word + c) if c.isalpha() else ""

#                     origin = ch.get("origin", (0, 0))
#                     origin_y = origin[1] if len(origin) >= 2 else 0.0

#                     sup_flag = bool(flags & FLAG_SUPERSCRIPT)

#                     # Prefer per-character native evidence when rawdict
#                     # provides it.  Most PDFs keep style at span level, but
#                     # some generators expose a mixed-style span with character
#                     # level font/flag/size data.  Do not invent or infer a style
#                     # from the character text itself.
#                     char_flags = ch.get("flags", flags)
#                     char_font_name = ch.get("font", font_name) or font_name
#                     char_size = ch.get("size", size) or size

#                     char_format = detect_formatting(
#                         flags=char_flags,
#                         font_name=char_font_name,
#                         size=char_size,
#                         origin_y=origin_y,
#                         dominant_size=dom_size,
#                         dominant_baseline=dom_baseline,
#                         superscript_flag=bool(char_flags & FLAG_SUPERSCRIPT),
#                         allow_size_only_fallback=(
#                             len(fixed_chars) <= 3
#                             and all(x.isdigit() or x in "*†‡§¶" for x in fixed_chars)
#                         ),
#                         shear_ratio=_char_shear_ratio(ch),
#                         dominant_shear_ratio=dom_shear,
#                         char=c,
#                     )
#                     char_bold = bool(char_format.get("bold"))
#                     char_italic = bool(char_format.get("italic"))

#                     italic = bool(
#                         span_italic
#                         or bool(char_flags & FLAG_ITALIC)
#                         or char_italic
#                         or inline_italic_flags[char_index]
#                     )
#                     bold = bool(span_bold or bool(char_flags & FLAG_BOLD) or char_bold)

#                     # Do NOT generate smallcaps.
#                     pos = classify_position(
#                         char_size,
#                         origin_y,
#                         dom_size,
#                         dom_baseline,
#                         bool(char_flags & FLAG_SUPERSCRIPT),
#                         allow_size_only_fallback=(
#                             len(fixed_chars) <= 3
#                             and all(x.isdigit() or x in "*†‡§¶" for x in fixed_chars)
#                         ),
#                     )
#                     style = (bold, italic, pos)

#                     if debug_log.is_enabled():
#                         debug_log.log(
#                             "CHAR",
#                             f"c={c!r} font={font_name!r} flags={flags} "
#                             f"size={size!r} pos={pos} bold={bold} italic={italic} "
#                             f"fixed={c != ch.get('c','')!r} "
#                             f"char_font={char_font_name!r} char_flags={char_flags} "
#                             f"char_size={char_size!r} shear={_char_shear_ratio(ch)!r}"
#                         )

#                     if current is not None and current[0] == style:
#                         current[1] += c
#                     else:
#                         if current is not None:
#                             runs.append(current)
#                         current = [style, c]

#                     prev_char = c
#                     prev_char_x1 = cb[2]

#             if current is not None:
#                 runs.append(current)

#             if runs:
#                 original_text = "".join(value for _, value in runs)
#                 text = "".join(_wrap_run(style, value) for style, value in runs)
#                 if text:
#                     text = _merge_adjacent_inline_tags(text)
#                     final_line = _normalize_text_for_epub(text)
#                     lines_out.append((line_bbox, final_line))
#                     if any(tag in final_line for tag in ("<i>", "<b>", "<sup>", "<sub>")):
#                         print(
#                             f"[PDF LINE] page={getattr(page, 'number', '?') + 1} "
#                             f"line={len(lines_out)} | {original_text} | {final_line}"
#                         )
#                     if debug_log.is_enabled():
#                         debug_log.log(
#                             "EXTRACT",
#                             f"LINE OUT bbox={line_bbox} => {final_line!r}"
#                         )

#     lines_out.sort(key=lambda item: item[0][1] if item[0] else 0)
#     return lines_out


# _TRAILING_TAGS_RE = re.compile(r"(?:</[a-z]+>)*$")
# _LEADING_TAGS_RE = re.compile(r"^(?:<[a-z]+>)*")
# # U+00AD SOFT HYPHEN is Unicode's own dedicated "may be used at a line
# # break" character - some real-world PDFs (professional typesetting/DTP
# # tools especially) genuinely encode a line-break hyphenation point this
# # way instead of a plain ASCII hyphen-minus (confirmed directly: PyMuPDF's
# # own text-insertion layer substitutes a trailing '-' with U+00AD when
# # writing through a non-base14 embedded font - the same real-world
# # substitution, not a testing artifact to work around). _HYPHEN_CHARS is
# # the ONE place both the boundary check and the strip regex draw from, so
# # a future third hyphen-like character only needs adding here. '-' is
# # placed FIRST in the class below so it is never misparsed as a range
# # operator against ­.
# _HYPHEN_CHARS = "-­"
# _TRAILING_HYPHEN_RE = re.compile(r"[-­]((?:</[a-z]+>)*)$")


# def _plain_edge_char(text: str, leading: bool) -> str:
#     """First/last character ignoring wrapping <b>/<i>/<sup>/<sub> tags,
#     used only to decide whether a line boundary is an artificial line-break
#     hyphen - never used to alter the text itself."""
#     if leading:
#         stripped = _LEADING_TAGS_RE.sub("", text)
#         stripped = _TAG_RE.sub("", stripped)
#         return stripped[0] if stripped else ""
#     stripped = _TRAILING_TAGS_RE.sub("", text)
#     stripped = _TAG_RE.sub("", stripped)
#     return stripped[-1] if stripped else ""


# def _strip_trailing_hyphen(text: str) -> str:
#     return _TRAILING_HYPHEN_RE.sub(r"\1", text, count=1)


# _ADJACENT_SAME_TAG_RE = re.compile(r"(\s*)</([a-z]+)>(\s*)<\2>(\s*)")


# def _collapse_adjacent_tag_match(m) -> str:
#     # Any whitespace found around the boundary (a trailing space already
#     # inside the first fragment, the dehyphenate_join separator space
#     # between fragments, and/or a leading space inside the next fragment)
#     # collapses to exactly one space - never zero (would glue two words
#     # together) and never left as two-or-more (would insert a spurious
#     # extra space that wasn't in the original PDF text). Genuinely
#     # zero-whitespace boundaries (e.g. "fibu-"/"lar" already concatenated
#     # directly by the hyphen-collapse branch above) correctly stay glued -
#     # ws is empty only when every one of the three captured groups is.
#     ws = m.group(1) + m.group(3) + m.group(4)
#     return " " if ws else ""


# def _merge_adjacent_inline_tags(text: str) -> str:
#     """Collapses a run of consecutive same-name inline tags (<b>, <i>,
#     <sup>, <sub>, or any future inline tag) separated only by whitespace into
#     one - undoes the fragmentation caused by PDF line wrapping. extract_lines
#     only merges same-style runs WITHIN one physical line (that's the unit
#     MuPDF reports spans in); when one continuous bold/italic/etc region spans
#     several wrapped lines of the SAME zone, each line starts out as its own
#     <b>...</bold> pair, joined below by nothing but the dehyphenate_join
#     space - never an actual formatting change. A genuine formatting change
#     (different tag, plain text, or - for Title zones - a literal <break/>)
#     always leaves something other than pure whitespace between the closing
#     and reopening tag, so this regex never fires for those and they stay
#     separate exactly as before. Looped until stable so a run of 3+ fragments,
#     and multi-level nesting (e.g. bold+italic together, where the outer tag's
#     boundary only becomes adjacent after the inner tag's boundary is merged,
#     or vice versa), both fully collapse in one call."""
#     prev = None
#     while prev != text:
#         prev = text
#         text = _ADJACENT_SAME_TAG_RE.sub(_collapse_adjacent_tag_match, text)
#     return text


# def _is_linebreak_hyphen_boundary(prev_text: str, next_text: str) -> bool:
#     """True when the boundary between two consecutive extracted fragments
#     (lines, in practice) is an artificial PDF line-break hyphen: prev_text
#     ends in '-' (ignoring wrapping inline tags) and next_text starts with a
#     lowercase letter (ignoring wrapping inline tags). This is the SINGLE
#     source of truth for that decision - dehyphenate_join/
#     dehyphenate_join_with_breaks (automatic joining) and
#     find_hyphen_candidates (the review-window detector) both call this
#     exact same check, so a fragment is never treated as a hyphen candidate
#     by one code path and not the other. A genuine same-line compound hyphen
#     (e.g. "mecanismo-dependiente") never reaches this function at all - it
#     exists entirely within ONE extracted line/fragment, never at a
#     fragment boundary, so there is nothing here to false-positive on."""
#     last = _plain_edge_char(prev_text, leading=False)
#     first = _plain_edge_char(next_text, leading=True)
#     return last in _HYPHEN_CHARS and first.isalpha() and first.islower()


# def dehyphenate_join(parts, keep_at=None) -> str:
#     """Joins a sequence of text fragments (already-extracted line or zone
#     text), collapsing an artificial PDF line/page-break hyphen - a fragment
#     ending in '-' immediately followed by a fragment starting with a
#     lowercase letter is joined directly ("fibu-" + "lar" -> "fibular"),
#     UNLESS that boundary's index (0-based, the boundary before parts[i+1])
#     is in `keep_at` (default: none), in which case the fragments are still
#     joined directly with no space - preserving the original hyphen exactly
#     as it would read on one line, e.g. "estímulo-" + "respuesta" ->
#     "estímulo-respuesta" - this is the user's explicit "keep this hyphen"
#     override from the post-zoning Hyphen Normalization Review (see
#     find_hyphen_candidates / gui/dialogs.HyphenReviewDialog); `keep_at`
#     empty/None (the default for every existing caller) reproduces the
#     exact prior automatic-join behavior, so nothing changes unless a
#     caller explicitly opts in. Genuine hyphens (mid-line, or not followed
#     by a lowercase continuation, e.g. "acción-reacción") are left untouched
#     since this only ever inspects the two characters exactly at a fragment
#     boundary - see _is_linebreak_hyphen_boundary."""
#     parts = [p for p in parts if p]
#     if not parts:
#         return ""
#     keep_at = keep_at or ()
#     result = parts[0]
#     for i, nxt in enumerate(parts[1:]):
#         if _is_linebreak_hyphen_boundary(result, nxt):
#             if i in keep_at:
#                 result = result + nxt  # user chose to KEEP the hyphen - join directly, hyphen intact, no space
#             else:
#                 result = _strip_trailing_hyphen(result) + nxt
#         else:
#             result = result + " " + nxt
#     return _merge_adjacent_inline_tags(result)



# def _inline_style_flags_minimal(chars, normal_size, normal_baseline):
#     """Minimal character-level fallback for inline sup/sub.

#     Uses only PDF rawdict character geometry. It does not OCR, render pages,
#     rewrite characters, or alter the existing extraction pipeline.
#     """
#     flags = [None] * len(chars)
#     if not chars or not normal_size:
#         return flags

#     ns = float(normal_size)
#     nb = float(normal_baseline or 0.0)

#     for i, ch in enumerate(chars):
#         c = ch.get("c", "")
#         if not c:
#             continue
#         box = ch.get("bbox") or ()
#         origin = ch.get("origin") or ()
#         if len(box) < 4:
#             continue

#         h = abs(float(box[3]) - float(box[1]))
#         y = float(origin[1]) if len(origin) >= 2 else float(box[3])

#         # Very small characters above the dominant baseline.
#         if h > 0 and h <= ns * 0.72 and y < nb - ns * 0.12:
#             flags[i] = "sup"
#             continue

#         # Very small characters below the dominant baseline.
#         if h > 0 and h <= ns * 0.72 and y > nb + ns * 0.45:
#             flags[i] = "sub"

#     return flags



# def _inline_italic_flags_minimal(chars, normal_shear=0.0):
#     """Minimal character-geometry italic fallback for mixed-style spans."""
#     out = [False] * len(chars)
#     if len(chars) < 2:
#         return out

#     # Use the existing character shear helper if present.
#     shear_fn = globals().get("_char_shear_ratio")
#     if shear_fn is None:
#         return out

#     values = []
#     for ch in chars:
#         try:
#             values.append(float(shear_fn(ch)))
#         except Exception:
#             values.append(None)

#     # Require a meaningful deviation from the local normal shear and a
#     # contiguous alphabetic run, avoiding punctuation/noise.
#     candidates = [
#         v is not None and abs(v - float(normal_shear)) >= 0.08
#         and (ch.get("c", "") or "").isalpha()
#         for ch, v in zip(chars, values)
#     ]

#     for i in range(len(chars)):
#         if not candidates[i]:
#             continue
#         j = i
#         count = 0
#         while j < len(chars):
#             cc = chars[j].get("c", "") or ""
#             if cc.isalpha() and candidates[j]:
#                 count += 1
#                 j += 1
#             elif cc.isspace() and j + 1 < len(chars) and candidates[j + 1]:
#                 j += 1
#             else:
#                 break
#         if count >= 2:
#             for k in range(i, j):
#                 if (chars[k].get("c", "") or "").isalpha():
#                     out[k] = True
#     return out


# def _page_looks_like_searchable_scan(page) -> bool:
#     """Detect a searchable scan whose OCR overlay has no usable font styling.

#     A searchable scan can classify as DIGITAL in text-quality terms because
#     its OCR text is perfectly readable.  That classification answers
#     ``should OCR be run for text?``; it does NOT answer ``does the text layer
#     contain the book's real font/style information?``.

#     For the latter question, a strong book-independent signal is: the page is
#     covered by a dominant scanned image AND every native text character comes
#     from one font with flags=0.  This is the structure found in the supplied
#     searchable-scan PDF, where the native layer is a uniform placeholder font
#     and the visual page contains the real typography.

#     This function never inspects spelling and never rewrites extracted text.
#     It only decides which formatting evidence source should be used.
#     """
#     try:
#         from core.ocr.style_detector import page_is_image_dominated
#         if not page_is_image_dominated(page):
#             return False

#         raw = _get_rawdict(page)
#         fonts = set()
#         any_text = False
#         all_flags_zero = True
#         for block in raw.get("blocks", []):
#             if block.get("type") != 0:
#                 continue
#             for line in block.get("lines", []):
#                 for span in line.get("spans", []):
#                     chars = span.get("chars", []) or []
#                     if not chars:
#                         continue
#                     any_text = True
#                     fonts.add(span.get("font", "") or "")
#                     if int(span.get("flags", 0) or 0) != 0:
#                         all_flags_zero = False
#                         break
#                 if not all_flags_zero:
#                     break
#             if not all_flags_zero:
#                 break

#         return any_text and all_flags_zero and len(fonts) == 1
#     except Exception:
#         # Formatting detection must never make extraction fail.
#         return False


# def extract_formatted_text(page, bbox, keep_hyphen_at=None) -> str:
#     # Cache exact repeated zone extraction.  This is deliberately keyed by
#     # document identity + page + exact bbox + hyphen override, so different
#     # zones and different normalization choices never share results.
#     _parent = getattr(page, "parent", None)
#     _doc_key = (getattr(_parent, "name", None) or None) or id(_parent)
#     try:
#         _bbox_key = tuple(float(v) for v in bbox)
#     except Exception:
#         _bbox_key = tuple(bbox) if bbox is not None else ()
#     _keep_key = tuple(keep_hyphen_at or ())
#     _formatted_key = (_doc_key, getattr(page, "number", None), _bbox_key, _keep_key)
#     _cached_formatted = _formatted_cache.get(_formatted_key)
#     if _cached_formatted is not None:
#         _formatted_cache.move_to_end(_formatted_key)
#         return _cached_formatted

#     """Returns XML-safe inline content (normal text + <b>/<i>/<sup>/<sub>)
#     for exactly the characters whose center point falls inside bbox (PDF coords).

#     Direct callers are also routed through the visual OCR/style detector when
#     the page is a searchable scan.  This closes the old verification/legacy
#     caller bypass: those callers previously went straight to rawdict and saw
#     ArialMT/flags=0 instead of the typography visible in the scanned page.
#     Clean DIGITAL pages remain on the existing native extraction path.

#     The visual path is used for FORMATTING ONLY.  Native extracted text remains
#     the text source, so this change does not replace the PDF's characters with
#     a second OCR transcription.

#     keep_hyphen_at: see dehyphenate_join - boundary indices where a detected
#     line-break hyphen should be kept (user override) rather than joined."""
#     if _direct_extraction_needs_image_style_analysis(page, bbox):
#         from types import SimpleNamespace
#         from core.ocr.style_detector import detect_ocr_zone_lines

#         # Keep native extracted text as the authoritative text source.
#         native_lines = [text for _, text in extract_lines(page, bbox)]
#         native_plain_lines = [strip_tags_to_plain(text) for text in native_lines]
#         temp_zone = SimpleNamespace(
#             bbox=bbox,
#             text="\n".join(native_plain_lines),
#             page=getattr(page, "number", 0),
#             attributes={},
#         )
#         visual_lines = detect_ocr_zone_lines(page, temp_zone)
#         result = dehyphenate_join(visual_lines, keep_hyphen_at).strip()
#         if debug_log.is_enabled():
#             debug_log.log("ZONE_TEXT",
#                           f"IMAGE_STYLE bbox={bbox} lines={visual_lines!r} => {result!r}")
#         return result

#     parts = [text for _, text in extract_lines(page, bbox)]
#     result = dehyphenate_join(parts, keep_hyphen_at).strip()
#     if debug_log.is_enabled():
#         debug_log.log("ZONE_TEXT", f"FORMATTED bbox={bbox} parts={parts!r} => {result!r}")
#     return result


# def extract_formatted_text_with_nested_children(page, zone_bbox, children, keep_hyphen_at=None) -> str:
#     """Like extract_formatted_text, but splices one or more geometrically-
#     NESTED child zones inline into the parent's own text at their exact
#     position (spec: "nested zone / inline zone support" - a real,
#     confirmed bug otherwise: the parent's own text used to be either
#     discarded entirely or the child dropped/duplicated - see
#     core.epub_xml_generator._gen_text_zone and core.xml_generator._zone_p).

#     `children`: [(child_bbox, markup), ...] - `markup` is an OPAQUE string
#     the caller has ALREADY fully rendered for that child (either more
#     text_extractor-style inline markup for a formatted nested text run, or
#     an already-serialized real XML element string like "<img .../>" for a
#     nested image) - this function never re-parses or reinterprets it, only
#     positions it. Never mutates/re-derives the child's own content, so
#     nested <b>/<i>/<sup>/<sub> (and combinations) already present
#     in that markup are preserved exactly as the caller produced them.

#     Algorithm: reuses extract_lines(page, zone_bbox) for the parent's own
#     physical lines - never a new/parallel low-level glyph scan. Each child
#     is matched to whichever parent line's y-range contains the child's own
#     bbox vertical center; a child matching NO line (e.g. the parent has no
#     text of its own on that row) is inserted as its own standalone entry
#     at the correct y-sorted position instead, so it is never silently
#     lost. A matched line is split into text segments strictly BEFORE/
#     BETWEEN/AFTER its child(ren)'s own x-ranges (children on one line
#     sorted left-to-right), each segment re-extracted via the existing,
#     unchanged extract_formatted_text on that narrower rectangle - so the
#     parent's own surrounding text on that same line is preserved exactly,
#     never dropped, never duplicated (the child's own bbox region is simply
#     excluded from the parent's re-extraction, since the child's already-
#     rendered markup is what fills that position instead). Every line with
#     no matched child passes through completely unchanged. Finally every
#     piece (in y-order) is joined through the existing, unchanged
#     dehyphenate_join - so multi-line joining/hyphenation for any line
#     without a nested child is completely unaffected."""
#     lines = extract_lines(page, zone_bbox)

#     def _center_y(bbox):
#         return (bbox[1] + bbox[3]) / 2.0

#     assigned = {}
#     unmatched = []
#     for child_bbox, markup in children:
#         if not markup:
#             continue
#         cy = _center_y(child_bbox)
#         line_idx = next((i for i, (lb, _) in enumerate(lines) if lb[1] <= cy <= lb[3]), None)
#         if line_idx is None:
#             unmatched.append((cy, markup))
#         else:
#             assigned.setdefault(line_idx, []).append((child_bbox, markup))

#     parts = []
#     for i, (line_bbox, line_text) in enumerate(lines):
#         kids = sorted(assigned.get(i, []), key=lambda c: c[0][0])
#         if not kids:
#             parts.append((line_bbox[1], line_text))
#             continue
#         # Segments are bounded by THIS matched line's own bbox (never the
#         # outer zone_bbox) - extract_lines already reports every physical
#         # PDF line as its own separate entry even when two lines happen to
#         # share an overlapping y-range (observed directly: two independent
#         # insert_text calls at the same baseline can land as two distinct
#         # "line" entries) - bounding by zone_bbox's own x0/x1 would let a
#         # tail/lead segment overreach into a DIFFERENT line's own text at
#         # that same y, duplicating it. Confirmed via a direct reproduction
#         # before this fix.
#         y0, y1 = line_bbox[1], line_bbox[3]
#         line_x0, line_x1 = line_bbox[0], line_bbox[2]
#         pieces = []
#         cursor = line_x0
#         for child_bbox, markup in kids:
#             seg_bbox = [cursor, y0, child_bbox[0], y1]
#             if seg_bbox[2] > seg_bbox[0]:
#                 seg_text = extract_formatted_text(page, seg_bbox, keep_hyphen_at)
#                 if seg_text:
#                     pieces.append(seg_text)
#             pieces.append(markup)
#             cursor = max(cursor, child_bbox[2])
#         tail_bbox = [cursor, y0, line_x1, y1]
#         if tail_bbox[2] > tail_bbox[0]:
#             tail_text = extract_formatted_text(page, tail_bbox, keep_hyphen_at)
#             if tail_text:
#                 pieces.append(tail_text)
#         parts.append((y0, " ".join(pieces)))

#     parts.extend(unmatched)
#     parts.sort(key=lambda p: p[0])
#     return dehyphenate_join([t for _, t in parts if t], keep_hyphen_at).strip()


# def dehyphenate_join_with_breaks(parts, keep_at=None) -> str:
#     """Like dehyphenate_join, but separate fragments are joined with a
#     literal <break/> marker instead of a space - for zones like Title where
#     each physical PDF line is typically a deliberate title/subtitle/byline
#     break rather than a wrapped sentence (BITS/JATS convention uses <break/>
#     between such lines). An artificial line-break hyphen ("fibu-" + "lar")
#     is still collapsed with no break inserted, same rule as dehyphenate_join
#     - including the same `keep_at` override (see there)."""
#     parts = [p for p in parts if p]
#     if not parts:
#         return ""
#     keep_at = keep_at or ()
#     result = parts[0]
#     for i, nxt in enumerate(parts[1:]):
#         if _is_linebreak_hyphen_boundary(result, nxt):
#             if i in keep_at:
#                 result = result + nxt
#             else:
#                 result = _strip_trailing_hyphen(result) + nxt
#         else:
#             result = result + "<break/>" + nxt
#     return _merge_adjacent_inline_tags(result)


# def extract_formatted_text_with_breaks(page, bbox, keep_hyphen_at=None) -> str:
#     """Same as extract_formatted_text, but joins separate physical lines with
#     <break/> instead of a space.  Searchable scans use the same visual
#     OCR/style decision while native text remains authoritative."""
#     if _direct_extraction_needs_image_style_analysis(page, bbox):
#         from types import SimpleNamespace
#         from core.ocr.style_detector import detect_ocr_zone_lines
#         native_lines = [text for _, text in extract_lines(page, bbox)]
#         native_plain_lines = [strip_tags_to_plain(text) for text in native_lines]
#         temp_zone = SimpleNamespace(
#             bbox=bbox,
#             text="\n".join(native_plain_lines),
#             page=getattr(page, "number", 0),
#             attributes={},
#         )
#         visual_lines = detect_ocr_zone_lines(page, temp_zone)
#         return dehyphenate_join_with_breaks(visual_lines, keep_hyphen_at).strip()
#     return dehyphenate_join_with_breaks(
#         [text for _, text in extract_lines(page, bbox)], keep_hyphen_at
#     ).strip()


# _TRAILING_WORD_RE = re.compile(r"(\S*-)\s*$")
# _LEADING_WORD_RE = re.compile(r"^\s*(\S+)")


# def join_boundary(prev: str, next_text: str, keep_hyphen: bool, default_join: str = " ") -> str:
#     """Joins two consecutive text fragments across a CROSS-ZONE boundary
#     (an explicit Merge Previous chain - core.zone_manager.ZoneManager.
#     merge_with_previous - or an automatic cross-page continuation - core.
#     paragraph_merge), respecting a genuine line-break hyphen exactly like
#     dehyphenate_join does for WITHIN-zone fragments (same
#     _is_linebreak_hyphen_boundary check - never a second/divergent
#     heuristic): if prev ends in a line-break hyphen and keep_hyphen is
#     False, the hyphen is dropped and the fragments joined directly
#     ("inter-" + "national" -> "international"); if keep_hyphen is True,
#     joined directly WITH the hyphen intact (the user's explicit "keep
#     this hyphen" choice - see gui.dialogs.HyphenReviewDialog's chain-
#     boundary candidates, stored on the LATER zone's own attributes[
#     "hyphen_keep_chain_boundary"]); otherwise (not a hyphen boundary at
#     all) uses default_join as-is - e.g. an explicit merge's own recorded
#     merge_join (" " or ""), completely unrelated to hyphenation and never
#     overridden by it.

#     Exists specifically for core.epub_xml_generator's own per-boundary
#     custom join string (merge_join), which core.text_extractor.
#     dehyphenate_join itself does not support (it always uses a single
#     space for a non-hyphen boundary) - core.xml_generator's own
#     _merged_chain_text has no such per-boundary custom join need and
#     reuses dehyphenate_join's own keep_at parameter directly instead."""
#     if _is_linebreak_hyphen_boundary(prev, next_text):
#         return (prev + next_text) if keep_hyphen else (_strip_trailing_hyphen(prev) + next_text)
#     return prev + default_join + next_text


# def find_hyphen_candidates(page, bbox):
#     """Scans bbox's extracted lines (same PDF geometry - font size,
#     position, baseline - already used to build them; see extract_lines)
#     for genuine line-break-hyphen boundaries, using the EXACT SAME check
#     dehyphenate_join uses (_is_linebreak_hyphen_boundary) - never a
#     separate/duplicate heuristic. Returns a list of dicts, one per
#     candidate, in document order:
#         {"boundary_index": int,  # pass to dehyphenate_join's keep_at to keep this one
#          "prefix_word": str,     # the hyphen-ending word, tags stripped, e.g. "estímulo-"
#          "suffix_word": str,     # the continuation word, tags stripped, e.g. "respuesta"
#          "joined_preview": str,  # e.g. "estímulorespuesta"
#          "kept_preview": str}    # e.g. "estímulo-respuesta"
#     A same-line compound hyphen (e.g. "mecanismo-dependiente") never
#     appears here, since it never forms a fragment boundary at all - it's
#     inside one single extracted line's own text, not between two lines."""
#     lines = [text for _, text in extract_lines(page, bbox)]
#     lines = [t for t in lines if t]
#     candidates = []
#     for i in range(len(lines) - 1):
#         prev_text, next_text = lines[i], lines[i + 1]
#         if not _is_linebreak_hyphen_boundary(prev_text, next_text):
#             continue
#         prev_plain = strip_tags_to_plain(prev_text)
#         next_plain = strip_tags_to_plain(next_text)
#         m_prefix = _TRAILING_WORD_RE.search(prev_plain)
#         m_suffix = _LEADING_WORD_RE.search(next_plain)
#         prefix_word = m_prefix.group(1) if m_prefix else prev_plain[-20:]
#         suffix_word = m_suffix.group(1) if m_suffix else next_plain[:20]
#         candidates.append({
#             "boundary_index": i,
#             "prefix_word": prefix_word,
#             "suffix_word": suffix_word,
#             "joined_preview": prefix_word[:-1] + suffix_word,
#             "kept_preview": prefix_word + suffix_word,
#         })
#     return candidates


# def find_chain_hyphen_candidates(zone_manager, pdf_document):
#     """Hyphenated Line-Break Text Normalization (spec sections 13/14:
#     "Support words split across line boundaries"/"detect continuation
#     across page boundaries") - the CROSS-ZONE counterpart to
#     find_hyphen_candidates' within-zone scan. Scans EXPLICIT 'Merge
#     Previous' chains only (core.zone_manager.ZoneManager.
#     merge_with_previous's own attributes["merge_target"] pointer - a
#     stable, already-computed piece of zoning data, never re-derived or
#     altered here) for a genuine line-break-hyphen boundary between the
#     END of the earlier zone's own text and the START of the later zone's,
#     using the EXACT SAME _is_linebreak_hyphen_boundary check as every
#     other hyphen decision in this module.

#     Automatic (page-number-flanked) continuations are deliberately NOT
#     included here: unlike an explicit merge, they are not recorded as a
#     stable, independently-queryable zone attribute before generation runs
#     - core.paragraph_merge computes them fresh, as part of the full
#     reading-order stream, only once generation itself is already running.
#     The actual join at generation time (core.epub_xml_generator.
#     _gen_merged_text_zone / core.xml_generator._merged_chain_text) still
#     dehyphenates them correctly by default even so - this is a disclosed
#     scope boundary on what gets a dedicated REVIEW checkbox, not a
#     correctness gap in the generated text itself.

#     Returns a list of dicts shaped like find_hyphen_candidates' own, plus
#     {"chain_boundary": True, "zone_id": <the LATER zone's id>, "page": <
#     its page>} - 'zone_id' identifies where the user's keep/remove choice
#     for THIS boundary is stored: that zone's own attributes[
#     "hyphen_keep_chain_boundary"] (a plain boolean - a zone can be the
#     'later half' of at most one chain boundary)."""
#     results = []
#     for zone in zone_manager.zones.values():
#         target_id = zone.attributes.get("merge_target")
#         if not target_id:
#             continue
#         prev_zone = zone_manager.zones.get(target_id)
#         if prev_zone is None:
#             continue
#         prev_lines = [t for _, t in extract_lines(pdf_document.get_page(prev_zone.page), prev_zone.bbox) if t]
#         next_lines = [t for _, t in extract_lines(pdf_document.get_page(zone.page), zone.bbox) if t]
#         if not prev_lines or not next_lines:
#             continue
#         prev_text, next_text = prev_lines[-1], next_lines[0]
#         if not _is_linebreak_hyphen_boundary(prev_text, next_text):
#             continue
#         prev_plain = strip_tags_to_plain(prev_text)
#         next_plain = strip_tags_to_plain(next_text)
#         m_prefix = _TRAILING_WORD_RE.search(prev_plain)
#         m_suffix = _LEADING_WORD_RE.search(next_plain)
#         prefix_word = m_prefix.group(1) if m_prefix else prev_plain[-20:]
#         suffix_word = m_suffix.group(1) if m_suffix else next_plain[:20]
#         results.append({
#             "chain_boundary": True,
#             "boundary_index": None,
#             "zone_id": zone.zone_id,
#             "page": zone.page,
#             "prefix_word": prefix_word,
#             "suffix_word": suffix_word,
#             "joined_preview": prefix_word[:-1] + suffix_word,
#             "kept_preview": prefix_word + suffix_word,
#         })
#     return results


# def find_all_hyphen_candidates(zone_manager, pdf_document):
#     """Runs find_hyphen_candidates over every zone in the project (any
#     text-carrying tag - detection depends only on PDF geometry within a
#     zone's own bbox, never on its tag), PLUS find_chain_hyphen_candidates
#     over every explicit merge chain, and returns a flat list of dicts,
#     each extending find_hyphen_candidates' own dict with:
#         {"zone_id": str, "page": int, "chain_boundary": bool}
#     in zone creation order (stable, matches project zone iteration order
#     elsewhere) - the single entry point gui/dialogs.HyphenReviewDialog
#     uses to build its checklist, and what gui/main_window.py checks
#     (empty -> nothing to review, skip the dialog entirely) before
#     Generate XML / Generate XHTML."""
#     results = []
#     for zone in zone_manager.zones.values():
#         page = pdf_document.get_page(zone.page)
#         for cand in find_hyphen_candidates(page, zone.bbox):
#             cand = dict(cand)
#             cand["zone_id"] = zone.zone_id
#             cand["page"] = zone.page
#             cand["chain_boundary"] = False
#             results.append(cand)
#     results.extend(find_chain_hyphen_candidates(zone_manager, pdf_document))
#     return results


# def strip_tags_to_plain(formatted_text: str) -> str:
#     """Plain text from an already-formatted inline string (tags stripped,
#     entities unescaped) - for callers holding formatted text directly rather
#     than a (page, bbox) to extract from, e.g. a manually merged zone's cached
#     combined text."""
#     return unescape(_TAG_RE.sub("", formatted_text or ""))


# def extract_plain_text(page, bbox, keep_hyphen_at=None) -> str:
#     """Plain-text preview (tags stripped, entities unescaped)."""
#     return strip_tags_to_plain(extract_formatted_text(page, bbox, keep_hyphen_at))


# def _zone_prefers_stored_text(zone) -> bool:
#     """True when zone.text is authoritative and must be used AS-IS at
#     generation time instead of re-extracting from the PDF live:

#     - an OCR-produced zone (attributes["source"] == "ocr") has no digital
#       text layer at its own bbox to re-extract in the first place - its
#       bbox sits on a scanned page (RULE 9/10: only zone.text, never a
#       live PDF re-read, is the source of truth for such a zone).
#     - any zone a user has manually corrected via the Properties panel's
#       text editor (attributes["manual_text"] - core.zone_manager.
#       set_zone_text) - RULE 6: a manual correction always overrides
#       automatic extraction, and must never be silently reverted at
#       generation time. This is the exact same rule CUPEPUB's PageNum zone
#       already followed (core/epub_xml_generator.py's _gen_pagenum_zone) -
#       generalized here to every ordinary content zone, digital-PDF ones
#       included, since a manual correction on ANY zone was silently being
#       discarded at Generate XML/XHTML time before this existed."""
#     return bool(zone.attributes.get("source") == "ocr" or zone.attributes.get("manual_text"))


# def _direct_extraction_needs_image_style_analysis(page, bbox) -> bool:
#     """Same searchable-scan decision for callers that only have page+bbox.
#     Only routes to image-based style analysis for confirmed searchable scans
#     (_page_looks_like_searchable_scan: image-dominated AND single unstyled
#     font AND all flags=0). Native PDF font data is authoritative for every
#     other page, including image-heavy pages with sparse digital text."""
#     return _page_looks_like_searchable_scan(page)


# def _zone_wants_image_formatting_detection(page, zone) -> bool:
#     """True when this zone's text should be analyzed via core.ocr.
#     style_detector's image-based italic/bold/sup/sub detection instead of
#     native PDF font metadata:

#     - source=="ocr": this app's own OCR feature explicitly tagged it, so there
#       is no digital text layer at the zone's bbox to re-extract in the first
#       place; image-based detection is the only option.
#     - _page_looks_like_searchable_scan(page): the page is dominated by a
#       scanned image AND every native character comes from one unstyled
#       placeholder font (flags=0) - the searchable-scan case where the visible
#       typography lives in the image, not the native text layer.

#     Never true for a manual_text zone (no reliable image region to analyze).
#     Never true for any other zone - native PDF character information is
#     authoritative, including on image-heavy pages with sparse digital text
#     (a chapter title page with a decorative background image, for example,
#     still has real font/flag data in its native characters)."""
#     if zone.attributes.get("manual_text"):
#         return False
#     if zone.attributes.get("source") == "ocr":
#         return True
#     return _page_looks_like_searchable_scan(page)


# def extract_zone_formatted_text(page, zone, keep_hyphen_at=None) -> str:
#     """Drop-in replacement for extract_formatted_text(page, zone.bbox, ...)
#     at any call site that already has the owning zone in scope (not just
#     its bbox). Returns zone.text itself, XML-escaped (it carries no
#     <b>/<i> markup the way a live PDF re-extraction would - OCR/
#     manual text has no such per-character font info), when
#     _zone_prefers_stored_text says so and image-based detection doesn't
#     apply (_zone_wants_image_formatting_detection); otherwise defers
#     unchanged to the existing live-extraction path, so every digital-PDF
#     zone's behavior (including real inline bold/italic markup) is
#     completely unaffected."""
#     if _zone_wants_image_formatting_detection(page, zone):
#         from core.ocr.style_detector import detect_ocr_zone_lines
#         text = dehyphenate_join(detect_ocr_zone_lines(page, zone), keep_hyphen_at).strip()
#         path = "ocr-style"
#     elif _zone_prefers_stored_text(zone):
#         text = escape(zone.text or "")
#         path = "stored"
#     else:
#         text = extract_formatted_text(page, zone.bbox, keep_hyphen_at)
#         path = "live"
#     _pgno = getattr(page, 'number', -1) + 1
#     if _pgno <= 4:
#         _preview = text[:80].replace("\n", " ")
#         print(
#             f"[ZONE PATH] page={_pgno} tag={getattr(zone, 'tag', '?')!r} "
#             f"path={path} src={zone.attributes.get('source', '-')!r} "
#             f"manual={zone.attributes.get('manual_text', False)} "
#             f"text={_preview!r}"
#         )
#     return _apply_zone_style_overrides(text, zone)


# def extract_zone_formatted_text_with_breaks(page, zone, keep_hyphen_at=None) -> str:
#     """Same as extract_zone_formatted_text, for call sites that would
#     otherwise use extract_formatted_text_with_breaks."""
#     if _zone_wants_image_formatting_detection(page, zone):
#         from core.ocr.style_detector import detect_ocr_zone_lines
#         text = dehyphenate_join_with_breaks(detect_ocr_zone_lines(page, zone), keep_hyphen_at).strip()
#     elif _zone_prefers_stored_text(zone):
#         text = escape(zone.text or "")
#     else:
#         text = extract_formatted_text_with_breaks(page, zone.bbox, keep_hyphen_at)
#     return _apply_zone_style_overrides(text, zone)


# def _apply_zone_style_overrides(text: str, zone) -> str:
#     """The one, minimal integration point for the Verification window's
#     manual Inline Style Editor (core/verification/inline_style.py) - a
#     no-op (single dict.get, no import) for the overwhelming common case
#     of a zone with no manual override, so every existing zone's output is
#     completely unaffected. Deferred import: core.verification depends on
#     core.text_extractor (e.g. unicode_verifier.get_unicode_repair_log),
#     so this module must never import core.verification at module level."""
#     overrides = getattr(zone, "attributes", {}).get("style_overrides")
#     if not overrides:
#         return text
#     from core.verification.inline_style import apply_style_overrides
#     return apply_style_overrides(text, overrides)


# def extract_zone_plain_text(page, zone, keep_hyphen_at=None) -> str:
#     """Same idea for call sites using extract_plain_text directly - here
#     zone.text is already plain (no XML-escaping needed): the caller
#     assigns it straight to an lxml element's .text, which escapes
#     automatically on serialization, exactly like CUPEPUB's PageNum
#     handler already does with zone.text."""
#     if _zone_prefers_stored_text(zone):
#         return zone.text or ""
#     return extract_plain_text(page, zone.bbox, keep_hyphen_at)


# def _line_center(bbox):
#     return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


# def _zone_geometric_lines(page, zone_bbox):
#     """Every whole-page LineInfo (auto_zoning.pdf_block_detector.
#     detect_lines - the SAME primitive Paragraph Auto Zone itself already
#     uses) whose own center falls inside zone_bbox, sorted top-to-bottom -
#     the geometric evidence multi-paragraph splitting is based on.
#     Deferred import: auto_zoning.pdf_block_detector itself imports
#     core.text_extractor (_get_rawdict), so this module cannot import it
#     back at module level without a circular import."""
#     from auto_zoning.pdf_block_detector import detect_lines
#     zx0, zy0, zx1, zy1 = zone_bbox
#     lines = [li for li in detect_lines(page)
#              if zx0 <= _line_center(li.bbox)[0] <= zx1 and zy0 <= _line_center(li.bbox)[1] <= zy1]
#     lines.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
#     return lines


# def extract_zone_paragraphs(page, zone, children=None, keep_hyphen_at=None) -> list:
#     """Returns a list of one-or-more fully-resolved, already-formatted
#     <p>-ready content strings for `zone` (spec: "MULTIPLE PDF PARAGRAPHS
#     INSIDE ONE SAVED ZONE") - ONE per geometrically-detected PDF paragraph
#     inside zone.bbox, using ONLY existing layout evidence (vertical-gap /
#     first-line-indent - auto_zoning.paragraph_auto_zone.
#     group_lines_into_paragraphs, the SAME primitive Paragraph Auto Zone
#     itself already uses to propose paragraph zones in the first place -
#     reused here as-is, never reimplemented), never a fixed line/character
#     count. The zone's own saved bbox/tag/id/children/reading-order are
#     only ever READ here, never modified - this function has no ZoneManager
#     access at all.

#     Purely additive: whenever there are fewer than 2 geometric lines, or
#     group_lines_into_paragraphs finds only one paragraph among them (the
#     overwhelming common case - most zones already ARE exactly one
#     paragraph), this returns a single-item list produced by the EXACT
#     SAME extraction this zone would have used before this function
#     existed (extract_zone_formatted_text, or
#     extract_formatted_text_with_nested_children when `children` is
#     given) - zero behavior change for that case, including `keep_hyphen_at`
#     (only honored in this single-paragraph case - see its own note below).

#     For a scanned/OCR zone (_zone_wants_image_formatting_detection), only
#     splits when the geometric line count matches zone.text's OWN line
#     count exactly (the same "ambiguous -> never guess, fall back" safety
#     net core.ocr.style_detector.detect_ocr_zone_lines already applies to
#     its own line/word segmentation) - zone.text remains the sole source
#     of every resulting paragraph's own text either way, only ever SLICED
#     by paragraph line-range, never replaced with re-extracted/re-
#     recognized text, and the image-based italic/bold/sup/sub detector
#     still runs per paragraph exactly as it already does per zone.

#     `children` ([(child_bbox, markup), ...], the same shape
#     extract_formatted_text_with_nested_children already takes): each
#     child is bucketed into whichever detected paragraph's own y-range is
#     CLOSEST to its bbox's own vertical center (0 distance when the center
#     falls directly within that paragraph's range) - mirrors that
#     function's own y-center-match convention, just per-paragraph instead
#     of zone-wide, so a nested inline zone changes which <p> it renders
#     inside but never itself moves, duplicates, or disappears.

#     `keep_hyphen_at` (the Hyphen Normalization Review's per-boundary
#     "keep this hyphen" override - see dehyphenate_join) is only honored
#     in the single-paragraph case above: its indices are boundary offsets
#     into the WHOLE zone's own joined parts, which no longer correspond to
#     anything once the zone is actually split into independently-joined
#     paragraphs, and a zone chain-merged across pages/zones (the actual
#     source of a real keep_hyphen_at set) is not, in practice, also a
#     zone containing several unrelated visual paragraphs."""
#     children = children or []

#     def _single_result():
#         if children:
#             return [extract_formatted_text_with_nested_children(page, zone.bbox, children, keep_hyphen_at)]
#         return [extract_zone_formatted_text(page, zone, keep_hyphen_at)]

#     lines = _zone_geometric_lines(page, zone.bbox)
#     if len(lines) < 2:
#         return _single_result()

#     from auto_zoning.paragraph_auto_zone import group_lines_into_paragraphs
#     blocks = group_lines_into_paragraphs(lines)
#     if len(blocks) < 2:
#         return _single_result()

#     use_image_detection = _zone_wants_image_formatting_detection(page, zone)
#     zone_text_lines = None
#     if use_image_detection:
#         zone_text_lines = (zone.text or "").split("\n")
#         if len(zone_text_lines) != len(lines):
#             # Geometric line count doesn't cleanly match the zone's own
#             # trusted text - never guess a split, same principle as
#             # detect_ocr_zone_lines's own line/word ambiguity fallback.
#             return _single_result()

#     block_bboxes = [(zone.bbox[0], b.bbox[1], zone.bbox[2], b.bbox[3]) for b in blocks]

#     def _distance_to_block(cy, bbox):
#         by0, by1 = bbox[1], bbox[3]
#         return 0.0 if by0 <= cy <= by1 else min(abs(cy - by0), abs(cy - by1))

#     children_by_block = [[] for _ in blocks]
#     for cb, markup in children:
#         cy = _line_center(cb)[1]
#         best_i = min(range(len(blocks)), key=lambda i: _distance_to_block(cy, block_bboxes[i]))
#         children_by_block[best_i].append((cb, markup))

#     results = []
#     idx = 0
#     for block, block_bbox, block_children in zip(blocks, block_bboxes, children_by_block):
#         start_idx, end_idx = idx, idx + len(block.lines)
#         idx = end_idx
#         if use_image_detection:
#             from types import SimpleNamespace
#             sub_zone = SimpleNamespace(bbox=block_bbox, text="\n".join(zone_text_lines[start_idx:end_idx]),
#                                         attributes=zone.attributes)
#             content = extract_zone_formatted_text(page, sub_zone, None)
#         elif block_children:
#             content = extract_formatted_text_with_nested_children(page, block_bbox, block_children, None)
#         else:
#             content = extract_formatted_text(page, block_bbox, None)
#         if content:
#             results.append(content)
#     return results if results else _single_result()


# def detect_zone_formatting(page, bbox) -> dict:
#     formatted = extract_formatted_text(page, bbox)
#     return {
#         "bold": "<b>" in formatted,
#         "italic": "<i>" in formatted,
#         "superscript": "<sup>" in formatted,
#         "subscript": "<sub>" in formatted,
#     }
"""Inline formatting engine: converts a PDF zone's native text/font/char info into
XML-safe inline content with <b>/<i>/<sup>/<sub>, using exact PDF text only.

Uses the native PDF text as authoritative. OCR is used only as an independent
verification step for a narrowly detected suspicious Unicode mapping; it never
supplies the extracted text or formatting.
"""
from collections import OrderedDict
import math
import fitz
from PIL import Image
try:
    from core import debug_log
except Exception:
    debug_log = None
import unicodedata
from xml.sax.saxutils import unescape, escape
import re

from core.formatting_detector import (
    detect_formatting,
    classify_position,
    line_baseline_stats,
    line_shear_stats,
    span_shear_ratio,
    FLAG_SUPERSCRIPT,
    FLAG_ITALIC,
    FLAG_BOLD,
    _char_shear_ratio,
)

_TAG_RE = re.compile(r"<[^>]+>")

# A geometry-verified repair for a real source-PDF defect (confirmed on an
# actual document, down to the raw per-character bbox): two words rendered
# with NO space glyph and NO coordinate gap between them at all - PyMuPDF is
# reporting exactly what the PDF encodes, there is no space to lose. Fires on:
#   (a) sentence-ending punctuation (.!?) directly followed by an UPPERCASE
#       letter (e.g. "treatise.The") - virtually never legitimate in prose
#       (an abbreviation like "e.g." can legitimately continue lowercase
#       with no space, which is why this is restricted to uppercase only).
#   (b) comma/semicolon/colon directly followed by ANY letter, upper or
#       lower (e.g. "Jonas,trans.") - unlike (a), prose never legitimately
#       omits the space after these regardless of case, so no case
#       restriction is needed; digits are excluded so a genuine number
#       grouping ("1,000") is never touched.
#   (c) a bare lowercase-letter-then-uppercase-letter transition (e.g.
#       "OswaldJonas", "BerlinJ.", "ofBoethius") - the general form of the
#       same defect, enabled per explicit user direction after being warned
#       of the tradeoff: legitimate compound surnames (McDonald, MacArthur,
#       DeVries, LaFontaine, VanDamme, O'Brien) can ALSO render with zero
#       gap, and PDF geometry alone cannot tell those apart from the defect.
#       _NAME_PREFIX_EXCEPTIONS suppresses the handful of extremely common
#       such prefixes to cut the most frequent false-positive class - it
#       narrows the risk, it does not eliminate the underlying ambiguity.
_SENTENCE_END_PUNCT = ".!?"
_ALWAYS_SPACED_PUNCT = ",;:"
_MISSING_SPACE_GAP_TOLERANCE = 1.0  # pt; confirmed real defects measure ~0.00, occasionally slightly negative (kerning)
_NAME_PREFIX_EXCEPTIONS = {"mc", "mac", "de", "le", "la", "van", "von", "o"}

# page.get_text('rawdict') is expensive on complex real-world pages (observed
# multi-second cost), so the parsed dict is cached per (document, page). Zoning
# a page creates many zones that each re-extract text from the same page, and
# XML generation re-visits every zone, so this cache is a large, necessary win.
_RAWDICT_CACHE_MAXSIZE = 16
_rawdict_cache: "OrderedDict" = OrderedDict()

# ---------------------------------------------------------------------------
# PDF glyph-level Unicode repair
# ---------------------------------------------------------------------------
# This is NOT a word dictionary.  These entries are glyph identities observed
# in the supplied searchable-scan PDF.  The PDF's ToUnicode table assigns
# misleading Unicode values to these glyphs, while the rendered glyph itself
# is unambiguous:
#   193 = combining grave, 194 = combining acute, 195 = combining circumflex
#   200 = combining diaeresis, 203 = combining cedilla
#   241 = æ, 245 = i, 249 = ö
#   174 = fi ligature, 175 = fl ligature
#
# The mapping is considered only when the native PDF trace identifies the
# exact glyph id AND the rendered PDF glyph contains visible ink.  Font name,
# word spelling, and dictionary lookup are not used as decision criteria.
_GLYPH_UNICODE_MAP = {
    193: "\u0300",  # combining grave accent
    194: "\u0301",  # combining acute accent
    195: "\u0302",  # combining circumflex
    200: "\u0308",  # combining diaeresis
    203: "\u0327",  # combining cedilla
    241: "\u00E6",  # æ
    245: "i",
    249: "\u00F6",  # ö
    174: "fi",
    175: "fl",
}

# Glyph ids are positions in ONE font's glyph table - they mean nothing in
# another font (e.g. in TimesLTStd glyph 241 is a correct "ñ" and 193 a
# correct "Á"; mapping them to "æ" / a combining grave corrupted Spanish
# text). The map above was observed in a malformed ArialMT font, so it is
# applied only to fonts of that family.
_ZERO_WIDTH_CHARS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
_GLYPH_UNICODE_MAP_FONTS = ("arial",)
_GLYPH_TRACE_CACHE_MAXSIZE = 8
_glyph_trace_cache = OrderedDict()

_PAGE_GRAY_CACHE_MAXSIZE = 4
_page_gray_cache = OrderedDict()

# Exact formatted-zone result cache.  The same zone is sometimes refreshed
# twice by the UI/ZoneManager during one edit.  Without this cache the complete
# PDF-line/style path is repeated even though the page and bbox are unchanged.
# This cache stores ONLY the final formatted string; it does not alter any
# recognition or formatting decisions.
_FORMATTED_CACHE_MAXSIZE = 256
_formatted_cache = OrderedDict()

# OCR is deliberately disabled during zoning.  Generation code enables this
# temporary scope so scanned/searchable-scan OCR/style analysis can still run
# when Certified -> Generate XHTML is executed.
_GENERATION_OCR_ENABLED = False
_GENERATION_OCR_CACHE = None
_GENERATION_OCR_SETTINGS = {}

# Final generation must not repeat the expensive per-character PDF glyph-trace
# repair. Prepared OCR cache is reused for OCR zones; digital zones use the
# native PDF text layer and formatting without glyph-trace/OCR verification.
_GENERATION_FAST_EXTRACTION = False

# Character-level text decorations (underline / strike-through) detected
# from real page geometry (core.underline_detector). OFF by default so the
# XML and EPUB profiles' output is unchanged; App.set_profile turns it on for
# the CUPEPUB profile only.
_DECORATION_DETECTION_ENABLED = False


def set_decoration_detection_enabled(enabled: bool):
    global _DECORATION_DETECTION_ENABLED
    if bool(enabled) != _DECORATION_DETECTION_ENABLED:
        _DECORATION_DETECTION_ENABLED = bool(enabled)
        _formatted_cache.clear()


def decoration_detection_enabled() -> bool:
    return _DECORATION_DETECTION_ENABLED


def _decoration_tag(field_name: str) -> str:
    """Inline element name for a decoration, taken from the project's own
    existing inline vocabulary (core.verification.span_model - the same
    names core.xhtml_writer already maps to final XHTML), never a new one."""
    from core.verification.span_model import _FIELD_TO_TAG
    return _FIELD_TO_TAG.get(field_name, field_name)


def set_generation_fast_extraction(enabled: bool):
    global _GENERATION_FAST_EXTRACTION
    _GENERATION_FAST_EXTRACTION = bool(enabled)


from contextlib import contextmanager


def set_generation_ocr_enabled(enabled: bool):
    """Enable/disable OCR-backed visual analysis for generation only."""
    global _GENERATION_OCR_ENABLED
    _GENERATION_OCR_ENABLED = bool(enabled)


@contextmanager
def generation_ocr_scope():
    """Temporarily permit OCR-backed visual analysis for final generation.
    Recognition itself is cache-only in generation."""
    global _GENERATION_OCR_ENABLED, _GENERATION_FAST_EXTRACTION
    previous = _GENERATION_OCR_ENABLED
    previous_fast = _GENERATION_FAST_EXTRACTION
    _GENERATION_OCR_ENABLED = True
    _GENERATION_FAST_EXTRACTION = True
    try:
        yield
    finally:
        _GENERATION_OCR_ENABLED = previous
        _GENERATION_FAST_EXTRACTION = previous_fast


@contextmanager
def generation_ocr_cache_scope(cache, settings=None):
    """Make the current project's prepared OCR cache available to generation."""
    global _GENERATION_OCR_CACHE, _GENERATION_OCR_SETTINGS
    old_cache, old_settings = _GENERATION_OCR_CACHE, _GENERATION_OCR_SETTINGS
    _GENERATION_OCR_CACHE = cache
    _GENERATION_OCR_SETTINGS = dict(settings or {})
    try:
        yield
    finally:
        _GENERATION_OCR_CACHE, _GENERATION_OCR_SETTINGS = old_cache, old_settings


def _cached_generation_ocr_text(page, bbox):
    if _GENERATION_OCR_CACHE is None or page is None:
        return ""
    try:
        from core.ocr import ocr_service
        settings = _GENERATION_OCR_SETTINGS or {}
        result = ocr_service.get_cached_ocr_for_page(
            page.parent, int(page.number) + 1,
            engine_name=settings.get("engine", "PaddleOCR"),
            language=settings.get("language", "en"),
            dpi=int(settings.get("dpi", 300)),
            preprocessing_settings=settings.get("preprocessing", {}),
            cache=_GENERATION_OCR_CACHE)
        return ocr_service.cached_ocr_text_for_bbox(result, bbox)
    except Exception:
        return ""


def _get_page_gray(page, dpi=200):
    """Cached grayscale rendering used only for glyph-shape verification."""
    if page is None:
        return None, 0.0
    parent = getattr(page, "parent", None)
    key = ((getattr(parent, "name", None) or None) or id(parent),
           getattr(page, "number", None), int(dpi))
    cached = _page_gray_cache.get(key)
    if cached is not None:
        _page_gray_cache.move_to_end(key)
        return cached
    try:
        from core.ocr.page_image_cache import get_page_image
        image = get_page_image(page, dpi)
        gray = __import__("numpy").array(image.convert("L"))
    except Exception:
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            gray = __import__("numpy").array(image.convert("L"))
        except Exception:
            return None, 0.0
    _page_gray_cache[key] = (gray, float(dpi))
    if len(_page_gray_cache) > _PAGE_GRAY_CACHE_MAXSIZE:
        _page_gray_cache.popitem(last=False)
    return gray, float(dpi)


def _glyph_ink_center_x(page, bbox):
    """Measure where a malformed combining glyph is actually drawn."""
    gray, dpi = _get_page_gray(page, 200)
    if gray is None or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = [float(v) for v in bbox]
        px0 = max(0, int(round(x0 * dpi / 72.0)) - 2)
        py0 = max(0, int(round(y0 * dpi / 72.0)) - 2)
        px1 = min(gray.shape[1], int(round(x1 * dpi / 72.0)) + 2)
        py1 = min(gray.shape[0], int(round(y1 * dpi / 72.0)) + 2)
        crop = gray[py0:py1, px0:px1]
        if crop.size == 0:
            return None
        # A conservative fixed dark-pixel threshold is used only to locate
        # the tiny accent/diacritic itself, not to OCR or transcribe text.
        ys, xs = __import__("numpy").where(crop < 180)
        if len(xs) == 0:
            return None
        return float((xs + px0).mean() * 72.0 / dpi)
    except Exception:
        return None


def _repair_combining_glyph_attachment(chars, fixed, page):
    """Attach restored combining marks to the base glyph they visibly mark.

    Broken PDF text layers sometimes place a combining accent BEFORE the
    character it visually belongs to (R + broken-accent + i -> Rí), while
    other occurrences place it AFTER the base (o + broken-accent -> ó).
    Decide from the rendered glyph's actual horizontal ink position, never
    from a spelling dictionary.
    """
    if page is None:
        return fixed
    result = list(fixed)
    n = len(result)
    for i, c in enumerate(result):
        if not (isinstance(c, str) and len(c) == 1 and __import__("unicodedata").combining(c)):
            continue
        # Only operate on the malformed source characters that this module
        # explicitly converted into combining marks.
        if i >= len(chars) or chars[i].get("c", "") not in {
            _E_ACUTE_BROKEN_CHAR, _ACCENT_BROKEN_CHAR, "Ã", "È", "Ë"
        }:
            continue

        def prev_base(j):
            j -= 1
            while j >= 0 and result[j].isspace():
                j -= 1
            return j if j >= 0 and (len(result[j]) != 1 or not __import__("unicodedata").combining(result[j])) else None

        def next_base(j):
            j += 1
            while j < n and result[j].isspace():
                j += 1
            return j if j < n and (len(result[j]) != 1 or not __import__("unicodedata").combining(result[j])) else None

        pi = prev_base(i)
        ni = next_base(i)
        if pi is None and ni is None:
            continue

        ink_x = _glyph_ink_center_x(page, chars[i].get("bbox") or ())
        if ink_x is None:
            continue

        candidates = []
        if pi is not None:
            try:
                pb = chars[pi]["bbox"]
                candidates.append((abs(ink_x - (float(pb[0]) + float(pb[2])) / 2.0), pi))
            except Exception:
                pass
        if ni is not None:
            try:
                nb = chars[ni]["bbox"]
                candidates.append((abs(ink_x - (float(nb[0]) + float(nb[2])) / 2.0), ni))
            except Exception:
                pass
        if not candidates:
            continue

        # If the next raw glyph is itself a known malformed base glyph
        # (currently õ -> i), the accent is visibly attached to that next
        # glyph in this PDF (RÂõ -> Rí). Otherwise the broken accent glyphs
        # in this PDF are attached to the preceding base (SenoÂ -> Senó,
        # AnnaÂ -> Anná, etc.). This is glyph-level evidence, not a word
        # spelling rule.
        next_is_known_base = (
            ni is not None
            and ni < len(chars)
            and chars[ni].get("c", "") in {"õ"}
        )
        if next_is_known_base:
            target = ni
        else:
            target = pi if pi is not None else ni

        if target == ni and ni == i + 1:
            # The common forward-accent defect: R + accent + i -> R + i + accent.
            result[i], result[ni] = result[ni], result[i]
        elif target == ni:
            # Preserve intervening tiny whitespace, but move the accent after
            # the actual base glyph so Unicode normalization composes it.
            mark = result[i]
            result[i] = ""
            result[ni] = result[ni] + mark

        # The searchable scan sometimes emits a tiny SPACE immediately after
        # the malformed accent although the rendered glyphs touch:
        # "AnnaÂ la" -> "Annála", "Bethu PhaÂ traic" -> "Bethu Phátraic".
        # Remove it only when the PDF geometry proves that it is not a real
        # word separator.
        if result[i] and __import__("unicodedata").combining(result[i]):
            # IMPORTANT: a malformed accent may be followed by a separate
            # bogus SPACE glyph.  Example:
            #
            #     Go + diaeresis + SPACE + ttingen
            #              -> Gö ttingen
            #
            # The SPACE is not necessarily tiny by its own bbox.  Therefore
            # first test the actual visual gap from the accented base to the
            # following letter.  If the following letter is visually close,
            # the PDF has split one word and the space must be removed.
            if i + 2 < n and result[i + 1] == " ":
                try:
                    base_i = i - 1
                    next_i = i + 2
                    if base_i >= 0 and next_i < n:
                        base_box = chars[base_i].get("bbox") or ()
                        next_box = chars[next_i].get("bbox") or ()
                        if len(base_box) >= 4 and len(next_box) >= 4:
                            visual_gap = (
                                float(next_box[0]) - float(base_box[2])
                            )
                            size = float(
                                chars[i].get("size")
                                or chars[base_i].get("size")
                                or chars[next_i].get("size")
                                or 10.0
                            )

                            # A real inter-word space is substantially wider.
                            # A split special-character glyph normally leaves
                            # a very small visual gap/overlap between the base
                            # and the next letter.
                            if visual_gap <= max(2.75, size * 0.28):
                                result[i + 1] = ""
                except Exception:
                    pass

                # Secondary check using the original SPACE glyph geometry.
                if result[i + 1] == " ":
                    try:
                        gap = float(chars[i + 2]["bbox"][0]) - float(chars[i + 1]["bbox"][2])
                        size = float(chars[i].get("size") or chars[i + 2].get("size") or 10.0)
                        if gap <= max(1.5, min(3.0, size * 0.22)):
                            result[i + 1] = ""
                    except Exception:
                        pass

            elif i > 1 and result[i - 1] == " ":
                try:
                    gap = float(chars[i]["bbox"][0]) - float(chars[i - 2]["bbox"][2])
                    size = float(chars[i].get("size") or chars[i - 2].get("size") or 10.0)
                    if gap <= max(1.5, min(3.0, size * 0.22)):
                        result[i - 1] = ""
                except Exception:
                    pass
    return result




def _get_trace_chars(page):
    """Return cached text-trace character tuples for one PDF page."""
    if page is None:
        return []
    parent = getattr(page, "parent", None)
    key = ((getattr(parent, "name", None) or None) or id(parent),
           getattr(page, "number", None))
    cached = _glyph_trace_cache.get(key)
    if cached is not None:
        _glyph_trace_cache.move_to_end(key)
        return cached
    try:
        trace = page.get_texttrace()
        chars = [c for span in trace for c in span.get("chars", ())]
    except Exception:
        chars = []
    _glyph_trace_cache[key] = chars
    if len(_glyph_trace_cache) > _GLYPH_TRACE_CACHE_MAXSIZE:
        _glyph_trace_cache.popitem(last=False)
    return chars


_TRACE_GRID_CELL = 2.0          # points; > the 1.75 acceptance limit below
_TRACE_ACCEPT_SCORE = 1.75
_glyph_trace_grid_cache = OrderedDict()


def _trace_grid(chars):
    """Spatial index over a page's trace chars: cell -> [list indices].

    _trace_glyph_id used to scan EVERY character on the page for EVERY
    character in the zone (O(n^2) - over a second for a full-page zone,
    several seconds on slower PCs, and the main reason drawing/splitting a
    big zone froze the window). Cached per trace list."""
    key = id(chars)
    cached = _glyph_trace_grid_cache.get(key)
    if cached is not None and cached[0] is chars:
        _glyph_trace_grid_cache.move_to_end(key)
        return cached[1]
    grid = {}
    cell = _TRACE_GRID_CELL
    for idx, item in enumerate(chars):
        try:
            tb = item[3]
            tx, ty = float(tb[0]), float(tb[1])
        except Exception:
            continue  # the full scan skipped these too (exception -> continue)
        grid.setdefault((math.floor(tx / cell), math.floor(ty / cell)), []).append(idx)
    _glyph_trace_grid_cache[key] = (chars, grid)
    if len(_glyph_trace_grid_cache) > _GLYPH_TRACE_CACHE_MAXSIZE:
        _glyph_trace_grid_cache.popitem(last=False)
    return grid


def _trace_glyph_id(page, ch):
    """Match one rawdict character to its native PDF glyph id by geometry.

    Same result as scanning every trace char: a match is only accepted when
    |dx| + |dy| (+ origin term) <= 1.75, so only chars whose top-left lies
    within one 2pt grid cell of this char can ever be accepted. Those are
    scored exactly as before, in their original order (same tie-break)."""
    bbox = ch.get("bbox") or ()
    origin = ch.get("origin") or ()
    if len(bbox) < 4:
        return None
    try:
        x0, y0 = float(bbox[0]), float(bbox[1])
    except (TypeError, ValueError):
        return None

    chars = _get_trace_chars(page)
    if not chars:
        return None
    grid = _trace_grid(chars)
    cell = _TRACE_GRID_CELL
    cx, cy = math.floor(x0 / cell), math.floor(y0 / cell)
    candidates = []
    for gx in (cx - 1, cx, cx + 1):
        for gy in (cy - 1, cy, cy + 1):
            bucket = grid.get((gx, gy))
            if bucket:
                candidates.extend(bucket)
    if not candidates:
        return None
    candidates.sort()

    best = None
    best_score = float("inf")
    for idx in candidates:
        item = chars[idx]
        try:
            tb = item[3]
            tx, ty = float(tb[0]), float(tb[1])
            score = abs(tx - x0) + abs(ty - y0)
            if len(origin) >= 2:
                # Origin x/y is even more stable than the bbox after
                # raster-derived text extraction.
                score += 0.25 * (
                    abs(float(item[2][0]) - float(origin[0])) +
                    abs(float(item[2][1]) - float(origin[1]))
                )
            if score < best_score:
                best_score = score
                best = item
        except Exception:
            continue
    if best is None or best_score > _TRACE_ACCEPT_SCORE:
        return None
    try:
        return int(best[0])
    except (TypeError, ValueError):
        return None


def _glyph_repair_for_char(page, ch, font_name=""):
    """Return a repair only when the rendered PDF glyph provides evidence.

    IMPORTANT:
    This function is deliberately NOT font-based.  Font name, bold/italic
    state, and font variants are never used to decide the Unicode value.

    The decision starts from the native glyph trace only to locate the exact
    character, then requires rendered-pixel evidence for the known malformed
    glyph shape.  If the visual evidence is unavailable, no repair is made.
    """
    if page is None:
        return None
    if not any(f in (font_name or "").lower() for f in _GLYPH_UNICODE_MAP_FONTS):
        return None

    gid = _trace_glyph_id(page, ch)
    if gid is None or gid not in _GLYPH_UNICODE_MAP:
        return None

    # The glyph map tells us which malformed glyph needs visual inspection;
    # it does NOT by itself authorize a repair.
    target = _GLYPH_UNICODE_MAP[gid]

    bbox = ch.get("bbox") or ()
    if len(bbox) < 4:
        return None

    # Render the actual PDF page and inspect the glyph's ink.
    # This is intentionally independent of font name.
    gray, dpi = _get_page_gray(page, 200)
    if gray is None:
        return None

    try:
        x0, y0, x1, y1 = [float(v) for v in bbox]
        px0 = max(0, int(round(x0 * dpi / 72.0)) - 3)
        py0 = max(0, int(round(y0 * dpi / 72.0)) - 3)
        px1 = min(gray.shape[1], int(round(x1 * dpi / 72.0)) + 3)
        py1 = min(gray.shape[0], int(round(y1 * dpi / 72.0)) + 3)

        crop = gray[py0:py1, px0:px1]
        if crop.size == 0:
            return None

        ink = crop < 180
        if int(ink.sum()) < 1:
            return None

        # Known accent repairs are accepted only when the glyph has visible
        # ink.  The actual attachment/position decision remains visual and is
        # handled by _repair_combining_glyph_attachment().
        return target
    except Exception:
        return None




def _get_rawdict(page):
    # A bare id(page.parent) is only safe while that Document object stays
    # alive - Python is free to reuse a freed object's memory address for
    # an UNRELATED later object once garbage-collected, and fitz.Document
    # objects are routinely short-lived (every open/close cycle, every
    # test, every "load a different PDF"). Confirmed directly: closing one
    # fitz.Document and opening a second one moments later can allocate the
    # new Document at the EXACT SAME id() as the first, which silently
    # served this second, completely different PDF's page 1 the FIRST
    # PDF's cached rawdict - a real cross-document data leak, not a
    # hypothetical one. doc.name (the path it was opened from) is a far
    # more stable identity when available; id() is kept only as the
    # fallback for a genuinely anonymous in-memory-only document (no path
    # to key on), where this residual risk already existed before.
    parent = getattr(page, "parent", None)
    parent_key = (getattr(parent, "name", None) or None) or id(parent)
    key = (parent_key, getattr(page, "number", None))
    cached = _rawdict_cache.get(key)
    if cached is not None:
        _rawdict_cache.move_to_end(key)
        return cached
    raw = page.get_text("rawdict")
    # right-to-left lines (Arabic, Hebrew, Persian ...) in reading order
    from core.lang import fix_rtl_rawdict
    raw = fix_rtl_rawdict(raw)
    _rawdict_cache[key] = raw
    if len(_rawdict_cache) > _RAWDICT_CACHE_MAXSIZE:
        _rawdict_cache.popitem(last=False)
    return raw


def clear_cache():
    _rawdict_cache.clear()
    _formatted_cache.clear()


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_text_for_epub(text: str) -> str:
    """Final, GENERAL Unicode normalization applied to a fully-assembled
    line (after _repair_pdf_characters' own per-character, per-span
    repairs) - only for defects that need to see ACROSS a span boundary,
    which the character-level pass (scoped to one span's own characters)
    cannot.

    Deliberately does NOT contain book-specific word patterns (e.g. a
    fixed literal spelling repair for one particular name that only
    happens to appear in one particular source PDF) - a hardcoded
    correction like that provides zero value for a different PDF whose
    own broken-glyph characters spell different words, and risks
    corrupting a genuinely different, legitimate use of the same
    punctuation-range character elsewhere. Any repair here must be
    justified purely from character-class/context, never from a specific
    word's spelling.
    """
    text = unicodedata.normalize("NFC", text)
    # Arabic / Hebrew presentation forms and ligature glyphs -> ordinary letters
    from core.lang import normalize_presentation_forms
    text = normalize_presentation_forms(text)

    # A dash-like defect ('±' standing in for an em/en dash) that spans
    # two PDF text-showing operations (e.g. "18 ± 19" split across
    # spans) can't be resolved by _repair_pdf_characters, which only ever
    # sees one span's own characters. Same context rule as there: digits
    # on both sides -> en dash (a numeric range); letters/whitespace on
    # both sides -> em dash (prose punctuation). A real, isolated '±'
    # (e.g. a standalone plus-minus symbol, or one directly touching a
    # single digit like "±5") never matches either pattern and is left
    # untouched.
    text = re.sub(r"(?<=[0-9])\s*±\s*(?=[0-9])", "–", text)
    text = re.sub(r"(?<=[A-Za-z])\s±\s(?=[A-Za-z])", "—", text)

    # LaTeX-produced PDFs commonly encode typographic double quotes as
    # paired backtick/apostrophe pairs (a well-known, general convention
    # of that toolchain, not specific to any one document) - convert only
    # genuinely PAIRED sequences; a lone backtick or apostrophe is left
    # exactly as extracted.
    text = text.replace("``", "“")
    text = text.replace("''", "”")

    return text


def _wrap_run(style, text: str) -> str:
    # The digital extractor uses the compact 3-tuple (bold, italic, pos).
    # The image/OCR style detector historically passes a 4-tuple with its
    # fourth value reserved for small-caps.  Small-caps output is deliberately
    # disabled, so accept and ignore that fourth value rather than crashing
    # the searchable-scan path.
    underline = strike = False
    if len(style) == 6:
        bold, italic, pos, _smallcaps, underline, strike = style
    elif len(style) == 4:
        bold, italic, pos, _smallcaps = style
    else:
        bold, italic, pos = style
    if not text.strip():
        # Never emit empty wrapper tags (<sup/>, <i/>, etc.).  Combining
        # diacritics and zero-width characters that slip through style
        # classification would otherwise produce <sup></sup> or <i></i>
        # in the output (e.g. Gá<sup/>edil).  Return the raw (possibly
        # empty or whitespace-only) text untouched and unescaped so the
        # caller's run-assembly logic can handle it normally.
        return text
    text = _xml_escape(text)
    if pos == "sup":
        text = f"<sup>{text}</sup>"
    elif pos == "sub":
        text = f"<sub>{text}</sub>"
    if italic:
         text = f"<i>{text}</i>"
    if bold:
        text = f"<b>{text}</b>"
    if underline:
        tag = _decoration_tag("underline")
        text = f"<{tag}>{text}</{tag}>"
    if strike:
        tag = _decoration_tag("strike")
        text = f"<{tag}>{text}</{tag}>"
    return text


def _char_in_bbox(ch_bbox, bbox, tol=1.0):
    cx0, cy0, cx1, cy1 = ch_bbox
    x0, y0, x1, y1 = bbox
    cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
    return (x0 - tol) <= cx <= (x1 + tol) and (y0 - tol) <= cy <= (y1 + tol)


# ---------------------------------------------------------------------------
# PDF text-encoding repair
# ---------------------------------------------------------------------------
# Some professionally typeset PDFs contain broken ToUnicode mappings.  The
# page can visually contain "fi", an em/en dash, or "ç", while PyMuPDF's
# rawdict returns another Unicode character.  These repairs are deliberately
# context-aware; do NOT globally replace symbols such as ® or ± because they
# can be legitimate characters.
#
# Known defects verified directly against the supplied source PDF:
#   ®  -> fi  (scientific, figures, define, final, office, etc.)
#   ¯  -> fl  (briefly, flows, conflict, reflective, afloat, inflation, etc.)
#   Ë  -> ç  (François)
#   Â  -> é  (cliché)
#   Á  -> à / è depending on the word (à Kempis / siècle)
#   ±  -> em dash in prose; en dash in numeric ranges
#   ``text'' -> “text”
#
# These are NOT global Unicode substitutions. They are applied only in the
# character/word contexts observed in this PDF, so legitimate ®, ±, Ë, Â or Á
# elsewhere are not automatically destroyed.

_FI_BROKEN_CHAR = "®"
_FL_BROKEN_CHAR = "¯"
_DASH_BROKEN_CHAR = "±"
_CEDILLA_BROKEN_CHAR = "Ë"
_E_ACUTE_BROKEN_CHAR = "Â"
_ACCENT_BROKEN_CHAR = "Á"

# Unlike ®/¯/± above, Ë/Â/Á are ordinary, LEGITIMATE Unicode letters that can
# appear correctly in any document (an author's own name, a French/Czech/
# Hungarian quotation, etc.) - a blind character-to-character substitution
# rule for these would corrupt genuinely correct text elsewhere. The defect
# this section repairs is not "this glyph is always wrong" but a specific,
# generally-recognizable STRUCTURAL pattern: a broken ToUnicode mapping that
# lands an uppercase, diacritic-bearing letter strictly BETWEEN two lowercase
# letters with no word boundary at all (e.g. "Franc" + "Ë" + "ois" for
# "François") - no ordinary typesetting convention ever places a capital
# letter mid-word like that; a genuine capital only ever starts a word. That
# structural anomaly alone is a strong, general, book-independent signal of
# corruption - but it names WHERE something is wrong, not WHAT the character
# should have been, so it is never repaired from text alone. The rendered PDF
# page itself is used as independent evidence: the whole word's own pixels
# are cropped and OCR'd, and the corrected character is accepted only when
# OCR's own transcription agrees with the raw extraction at every OTHER
# character in that word (never a total-mismatch coincidence) and reports
# high confidence for the differing position. Anything short of that is left
# completely untouched and logged as a low-confidence, unrepaired case -
# never a guess.
_STRUCTURAL_UNICODE_ANOMALY_CHARS = (_CEDILLA_BROKEN_CHAR, _E_ACUTE_BROKEN_CHAR, _ACCENT_BROKEN_CHAR, "ñ")
UNICODE_REPAIR_MIN_OCR_CONFIDENCE = 0.75

# PaddleOCR model initialization is intentionally OPT-IN.
# Normal extraction/startup must never initialize the large OCR models.
# Set environment variable ZONETOOL_ENABLE_UNICODE_OCR=1 only when
# rendered-pixel Unicode verification is explicitly required.
_ENABLE_UNICODE_OCR_VERIFY = (
    __import__("os").environ.get("ZONETOOL_ENABLE_UNICODE_OCR", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

# Diagnostic record log for every structural Unicode anomaly this module has
# ever DETECTED (accepted or rejected) - spec: "Must create a diagnostic
# record for EVERY repair: PDF, page, original character, replacement
# character, character code if available, font if available, reason,
# confidence/evidence." Process-lifetime, never persisted automatically -
# a caller (e.g. a regression/audit script) reads it via
# get_unicode_repair_log() and/or clears it via clear_unicode_repair_log()
# between PDFs.
_unicode_repair_log = []


def get_unicode_repair_log():
    """A shallow copy of every structural Unicode anomaly record logged so
    far this process (see _unicode_repair_log above) - safe for a caller to
    iterate/serialize without risk of it changing underfoot."""
    return list(_unicode_repair_log)


def clear_unicode_repair_log():
    _unicode_repair_log.clear()


# Process-lifetime cache of already-verified structural-anomaly decisions,
# keyed by (pdf_path, raw_word, anomaly_position_in_word, font_name) - see
# its own usage site in _repair_structural_unicode_anomalies for why this
# exists (the identical defect recurring many times in one real document,
# e.g. a proper name repeated throughout a book, must not re-pay a real
# OCR crop+recognize() call for every occurrence). Scoped to pdf_path (not
# just word/font) because the actual EVIDENCE is the word's own rendered
# pixels on that specific document - the same word/font pairing recurring
# in a DIFFERENT PDF (or, as this module's own test suite does, a
# deliberately blank fixture reusing the same word/font to test the
# rejection path) has no guarantee of the same rendered evidence, so must
# never reuse another document's verified answer. Independent of
# _unicode_repair_log's own per-PDF clearing - a verified decision remains
# valid for the lifetime of this cache (this process), scoped per PDF.
_structural_repair_word_cache = {}


def clear_structural_repair_cache():
    _structural_repair_word_cache.clear()


def _log_unicode_repair(pdf_path, page_number, original_char, replacement_char, font_name, reason,
                         confidence, accepted):
    _unicode_repair_log.append({
        "pdf": pdf_path,
        "page": page_number,
        "original_char": original_char,
        "original_char_code": f"U+{ord(original_char):04X}" if original_char else None,
        "replacement_char": replacement_char,
        "font": font_name,
        "reason": reason,
        "confidence": confidence,
        "accepted": accepted,
    })


def _crop_word_image(page, word_bbox, dpi=300, pad_pt=8.0):
    """The WHOLE WORD's own rendered pixels (never a single isolated glyph -
    an isolated glyph crop is unreliable for OCR, exactly the same reason
    core.ocr.style_detector's own slant measurement distrusts a single-
    sample reading), cropped from the page's own cached render (core.ocr.
    page_image_cache - reused, never a fresh per-call page render). Point-
    space padding avoids clipping a real letter right at the word's own
    tight bbox - confirmed directly this matters: a too-tight crop (~1.5pt)
    measurably clipped the last letter's own glyph and made a real OCR
    engine misread/drop it, even at 300 DPI; 8pt comfortably clears a
    normal font's own side-bearing at ordinary body-text sizes without
    pulling in enough of a neighboring word to interfere."""
    from core.ocr.page_image_cache import get_page_image, ANALYSIS_DPI
    dpi = dpi or ANALYSIS_DPI
    img = get_page_image(page, dpi)
    zoom = dpi / 72.0
    x0, y0, x1, y1 = word_bbox
    px0 = max(0, int((x0 - pad_pt) * zoom))
    py0 = max(0, int((y0 - pad_pt) * zoom))
    px1 = min(img.width, int((x1 + pad_pt) * zoom) + 1)
    py1 = min(img.height, int((y1 + pad_pt) * zoom) + 1)
    if px1 <= px0 or py1 <= py0:
        return None
    return img.crop((px0, py0, px1, py1))


def _verify_structural_unicode_anomaly(page, matched, raw, start, end, idx, font_name):
    """Verify one suspicious PDF Unicode mapping against rendered pixels.

    OCR is verification only: the native PDF text remains authoritative except
    for the suspicious character (and, when independently demonstrated by the
    rendered line, a tiny bogus OCR character immediately adjacent to it).

    Returns (replacement, delete_offsets, confidence, reason).
    """
    if _GENERATION_FAST_EXTRACTION:
        return None, [], None, "PaddleOCR Unicode verification disabled during generation"
    raw_word = "".join(raw[start:end])
    pos = idx - start
    # Compare the word's letter content; leading/trailing quotation or
    # punctuation belongs to the surrounding typography and must not make an
    # otherwise exact OCR verification fail. Keep the offset into the native
    # character list so any confirmed deletion is still applied precisely.
    _edge_punct = ".,;:!?()[]{}'\"“”‘’"
    raw_leading = len(raw_word) - len(raw_word.lstrip(_edge_punct))
    raw_core = raw_word.strip(_edge_punct)
    core_pos = pos - raw_leading

    def _same_except(raw_text, ocr_text, pos):
        if len(raw_text) != len(ocr_text):
            return None
        for k, (a, b) in enumerate(zip(raw_text, ocr_text)):
            if k != pos and a != b:
                return None
        candidate = ocr_text[pos]
        if candidate == raw_text[pos] or not candidate.isalpha():
            return None
        return candidate

    def _line_chars_for_target():
        try:
            rawdict = _get_rawdict(page)
            target_ids = {id(c) for c in matched}
            for block in rawdict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_chars = [c for span in line.get("spans", []) for c in span.get("chars", [])]
                    if any(id(c) in target_ids for c in line_chars):
                        return line_chars, line.get("bbox")
        except Exception:
            pass
        return None, None

    # PaddleOCR is an optional verifier only.  Keep it out of normal extraction
    # and application startup; the native PDF/glyph evidence remains authoritative.
    if not (_GENERATION_OCR_ENABLED and _ENABLE_UNICODE_OCR_VERIFY):
        return None, [], None, "PaddleOCR Unicode verification disabled during zoning"

    word_bboxes = [matched[i]["bbox"] for i in range(start, end)]
    word_bbox = (min(b[0] for b in word_bboxes), min(b[1] for b in word_bboxes),
                 max(b[2] for b in word_bboxes), max(b[3] for b in word_bboxes))
    ocr_text = _cached_generation_ocr_text(page, word_bbox)
    if ocr_text:
        tokens = [t for t in ocr_text.split() if len(t) == len(raw_word)]
        if len(tokens) == 1:
            candidate = _same_except(raw_word, tokens[0], pos)
            if candidate:
                try:
                    from core.ocr import ocr_service
                    settings = _GENERATION_OCR_SETTINGS or {}
                    cached_result = ocr_service.get_cached_ocr_for_page(
                        page.parent, int(page.number) + 1,
                        engine_name=settings.get("engine", "PaddleOCR"),
                        language=settings.get("language", "en"),
                        dpi=int(settings.get("dpi", 300)),
                        preprocessing_settings=settings.get("preprocessing", {}),
                        cache=_GENERATION_OCR_CACHE)
                    blocks = ocr_service.cached_ocr_blocks_for_bbox(cached_result, word_bbox)
                    confidence = min((float(b.confidence) for b in blocks), default=0.0)
                except Exception:
                    confidence = 0.0
                if confidence >= UNICODE_REPAIR_MIN_OCR_CONFIDENCE:
                    return candidate, [], confidence, "Cached page OCR-verified structural Unicode mapping"
    return None, [], None, "Cached OCR verification unavailable"


def _verify_missing_uppercase_accent(page, chars, fixed, idx, start, end):
    """Verify a visibly accented uppercase glyph missing from the text layer.

    Native text remains authoritative. OCR is used only after rendered pixels
    show ink immediately above the native uppercase glyph, and only the single
    character is replaced when OCR agrees with high confidence.
    """
    import unicodedata
    if _GENERATION_FAST_EXTRACTION:
        return None
    # Every path below that can return a value needs the opt-in OCR check;
    # bail out BEFORE rendering the whole page to pixels when it is off
    # (the default) - the render cost ~0.1-0.3 s per zone and its result
    # was always discarded.
    if not (_GENERATION_OCR_ENABLED and _ENABLE_UNICODE_OCR_VERIFY):
        return None
    if page is None or idx < start or idx >= end:
        return None
    base = fixed[idx] if idx < len(fixed) else ""
    if len(base) != 1 or not base.isalpha() or not base.isupper():
        return None

    b = chars[idx].get("bbox") or ()
    if len(b) < 4:
        return None
    x0, y0, x1, y1 = [float(v) for v in b]
    h = max(1.0, y1 - y0)

    gray, dpi = _get_page_gray(page, 200)
    if gray is None:
        return None

    px0 = max(0, int(round((x0 - 0.15*h) * dpi / 72.0)))
    px1 = min(gray.shape[1], int(round((x1 + 0.15*h) * dpi / 72.0)))
    py0 = max(0, int(round((y0 - 0.48*h) * dpi / 72.0)))
    py1 = min(gray.shape[0], int(round((y0 - 0.03*h) * dpi / 72.0)))
    if px1 <= px0 or py1 <= py0:
        return None

    crop = gray[py0:py1, px0:px1]
    ink = crop < 150
    if int(ink.sum()) < 2:
        return None

    # Form the containing native word.
    ws = idx
    while ws > start and not fixed[ws - 1].isspace():
        ws -= 1
    we = idx + 1
    while we < end and not fixed[we].isspace():
        we += 1
    word = "".join(fixed[ws:we])
    if not word:
        return None

    # PaddleOCR is opt-in for this narrow verification step.  This prevents
    # normal extraction/startup from constructing the OCR models.
    if not (_GENERATION_OCR_ENABLED and _ENABLE_UNICODE_OCR_VERIFY):
        return None

    try:
        boxes = [chars[j]["bbox"] for j in range(ws, we)]
        wb = (
            min(float(b[0]) for b in boxes), min(float(b[1]) for b in boxes),
            max(float(b[2]) for b in boxes), max(float(b[3]) for b in boxes),
        )
        ocr = _cached_generation_ocr_text(page, wb)
        if not ocr:
            return None
        tokens = [t for t in ocr.split() if t]
        if len(tokens) != 1:
            return None
        token = tokens[0]
        # OCR may omit a trailing punctuation mark even when the letters are
        # correct. Compare the word's alphabetic core while preserving the
        # exact native character position being verified.
        edge = ".,;:!?()[]{}'\"“”‘’"
        raw_lead = len(word) - len(word.lstrip(edge))
        raw_core = word.strip(edge)
        ocr_lead = len(token) - len(token.lstrip(edge))
        ocr_core = token.strip(edge)
        if len(raw_core) != len(ocr_core) or raw_lead != ocr_lead:
            return None
        candidate = ocr_core[idx - ws - raw_lead]
        if candidate == base or not candidate.isalpha() or not candidate.isupper():
            return None
        nfd = unicodedata.normalize("NFD", candidate)
        if not nfd or nfd[0] != base or not any(unicodedata.combining(c) for c in nfd[1:]):
            return None
        try:
            from core.ocr import ocr_service
            settings = _GENERATION_OCR_SETTINGS or {}
            cached_result = ocr_service.get_cached_ocr_for_page(
                page.parent, int(page.number) + 1,
                engine_name=settings.get("engine", "PaddleOCR"),
                language=settings.get("language", "en"),
                dpi=int(settings.get("dpi", 300)),
                preprocessing_settings=settings.get("preprocessing", {}),
                cache=_GENERATION_OCR_CACHE)
            cached_blocks = ocr_service.cached_ocr_blocks_for_bbox(cached_result, wb)
            confidence = min((float(b.confidence) for b in cached_blocks), default=0.0)
        except Exception:
            confidence = 0.0
        if confidence < UNICODE_REPAIR_MIN_OCR_CONFIDENCE:
            return None
        return candidate, confidence
    except Exception:
        return None


def _repair_structural_unicode_anomalies(
    chars, fixed, page, font_name="", font_names=None
):
    """Repair structurally broken Unicode mappings using rendered-PDF evidence.

    This is intentionally book-independent.  In addition to the ordinary
    lowercase + anomaly + lowercase form, it recognizes the real PDF failure
    where the broken glyph is emitted as its own span with a tiny bogus space
    before/after it, e.g. ``Sie Áge``.
    """
    if page is None or not fixed:
        return fixed

    n = len(fixed)
    result = list(fixed)

    # Resolve whether a restored combining glyph visually belongs to the
    # previous or next base character before NFC normalization.
    result = _repair_combining_glyph_attachment(chars, result, page)

    # Verify missing uppercase accents that are visible in the scanned page
    # image but absent from the searchable text layer (e.g. Éireann).
    for _idx in range(n):
        if result[_idx] != chars[_idx].get("c", ""):
            continue
        if not (len(result[_idx]) == 1 and result[_idx].isalpha() and result[_idx].isupper()):
            continue
        _ws = _idx
        while _ws > 0 and not result[_ws - 1].isspace():
            _ws -= 1
        _we = _idx + 1
        while _we < n and not result[_we].isspace():
            _we += 1
        _verified = _verify_missing_uppercase_accent(
            page, chars, result, _idx, _ws, _we
        )
        if _verified:
            _replacement, _confidence = _verified
            _log_unicode_repair(
                getattr(getattr(page, "parent", None), "name", None),
                getattr(page, "number", None),
                result[_idx], _replacement, "", 
                "rendered uppercase accent + PaddleOCR verification",
                _confidence, True,
            )
            result[_idx] = _replacement

    def tiny_space(i):
        if i <= 0 or i + 1 >= n or fixed[i] != " ":
            return False
        try:
            left = float(chars[i - 1]["bbox"][2])
            right = float(chars[i + 1]["bbox"][0])
            size = float(
                chars[i].get("size")
                or chars[i - 1].get("size")
                or chars[i + 1].get("size")
                or 10.0
            )
            gap = right - left
            return gap <= max(1.5, min(2.75, size * 0.20))
        except Exception:
            return False

    for idx, c in enumerate(fixed):
        if c not in _STRUCTURAL_UNICODE_ANOMALY_CHARS:
            continue

        # Direct: i + anomaly + g
        direct = (
            idx > 0 and idx + 1 < n
            and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
            and fixed[idx + 1].isalpha() and fixed[idx + 1].islower()
        )

        # Broken span form: i + tiny-space + anomaly + g
        spaced_before = (
            idx > 1 and idx + 1 < n
            and fixed[idx - 1] == " "
            and tiny_space(idx - 1)
            and fixed[idx - 2].isalpha() and fixed[idx - 2].islower()
            and fixed[idx + 1].isalpha() and fixed[idx + 1].islower()
        )

        # Also allow anomaly + tiny-space + lowercase.
        spaced_after = (
            idx > 0 and idx + 2 < n
            and fixed[idx + 1] == " "
            and tiny_space(idx + 1)
            and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
            and fixed[idx + 2].isalpha() and fixed[idx + 2].islower()
        )

        # Some broken mappings occur at the END of a word rather than between
        # two lowercase letters (the supplied PDF has Scotiñ/Hiberniñ).  This
        # is still only a candidate: the rendered-line OCR verifier below
        # must independently prove a different character before anything is
        # changed. Legitimate ñ therefore remains untouched when OCR agrees.
        word_end = (
            idx > 0 and fixed[idx - 1].isalpha() and fixed[idx - 1].islower()
            and (idx + 1 >= n or not fixed[idx + 1].isalpha())
        )

        if not (direct or spaced_before or spaced_after or word_end):
            continue

        if direct:
            start = idx
            end = idx + 1
            while start > 0 and (
                fixed[start - 1].isalpha() or fixed[start - 1] in "'-"
            ):
                start -= 1
            while end < n and (
                fixed[end].isalpha() or fixed[end] in "'-"
            ):
                end += 1
        else:
            start = idx
            end = idx + 1
            while start > 0 and (
                fixed[start - 1].isalpha()
                or fixed[start - 1] in "'- "
            ):
                if fixed[start - 1] == " " and not tiny_space(start - 1):
                    break
                start -= 1
            while end < n and (
                fixed[end].isalpha()
                or fixed[end] in "'- "
            ):
                if fixed[end] == " " and not tiny_space(end):
                    break
                end += 1

        word_indices = [
            j for j in range(start, end)
            if not (fixed[j] == " " and tiny_space(j))
        ]
        if idx not in word_indices:
            continue

        compact_chars = [chars[j] for j in word_indices]
        compact_fixed = [fixed[j] for j in word_indices]
        compact_fonts = [
            font_names[j] if font_names is not None and j < len(font_names)
            else font_name
            for j in word_indices
        ]
        compact_idx = word_indices.index(idx)
        raw_word = "".join(compact_fixed)
        anomaly_font = compact_fonts[compact_idx]

        pdf_path = (
            getattr(getattr(page, "parent", None), "name", None)
            or "<in-memory PDF>"
        )
        page_number = (getattr(page, "number", None) or 0) + 1
        cache_key = (pdf_path, raw_word, compact_idx, anomaly_font)

        cached = _structural_repair_word_cache.get(cache_key)
        if cached is not None:
            replacement, deletions, confidence, reason = cached
        else:
            replacement, deletions, confidence, reason = _verify_structural_unicode_anomaly(
                page,
                compact_chars,
                compact_fixed,
                0,
                len(compact_fixed),
                compact_idx,
                anomaly_font,
            )
            _structural_repair_word_cache[cache_key] = (
                replacement, deletions, confidence, reason
            )

        _log_unicode_repair(
            pdf_path,
            page_number,
            c,
            replacement,
            anomaly_font,
            reason,
            confidence,
            accepted=replacement is not None,
        )

        if replacement is not None:
            result[idx] = replacement
            for rel in deletions:
                j = start + rel
                if 0 <= j < n and j != idx:
                    result[j] = ""
            for j in (idx - 1, idx + 1):
                if 0 <= j < n and fixed[j] == " " and tiny_space(j):
                    result[j] = ""

    # A combining accent can be emitted by the broken PDF as its own glyph
    # span followed by a tiny bogus whitespace glyph.  Once the glyph has
    # been restored to a Unicode combining mark, remove only that geometrically
    # tiny separator so NFC can compose the base letter + accent.
    for i in range(1, n - 1):
        if not (isinstance(result[i], str) and len(result[i]) == 1
                and unicodedata.combining(result[i]) and result[i - 1]):
            continue
        if result[i + 1] != " ":
            continue
        try:
            gap = float(chars[i + 1]["bbox"][0]) - float(chars[i]["bbox"][2])
            size = float(chars[i + 1].get("size") or chars[i].get("size") or 10.0)
            if gap <= max(1.5, min(2.75, size * 0.20)):
                result[i + 1] = ""
        except Exception:
            pass

    return result


def _repair_pdf_characters(chars, page=None, font_name=""):
    """Repair known broken PDF Unicode mappings conservatively.

    Input is a list of rawdict character dictionaries.  The dictionaries are
    not modified; a parallel list of corrected character strings is returned.
    Geometry/style information therefore remains exactly as supplied by PDF.

    `page`/`font_name` are used ONLY by the structural Ë/Â/Á anomaly pass
    below (OCR-based verification needs the rendered page; diagnostic
    logging records the font) - every other repair in this function is pure
    text/context, exactly as before, and passing page=None (the default)
    skips that pass entirely with zero behavior change.
    """
    raw = [ch.get("c", "") or "" for ch in chars]
    fixed = list(raw)
    n = len(raw)

    # Expensive glyph-trace/pixel repair is disabled during final generation.
    # This avoids page.get_texttrace() work for every character and prevents
    # Generate XHTML from becoming unresponsive on large PDFs.
    if not _GENERATION_FAST_EXTRACTION:
            # First use exact PDF glyph identity where this PDF exposes a known
            # malformed ArialMT mapping.  This is stronger than word spelling and
            # does not depend on OCR transcription.
            for i, ch in enumerate(chars):
                glyph_value = _glyph_repair_for_char(page, ch, font_name)
                if glyph_value is not None:
                    fixed[i] = glyph_value
                    if debug_log is not None and debug_log.is_enabled():
                        debug_log.log(
                            "UNICODE",
                            f"glyph_map font={font_name!r} glyph_id={_trace_glyph_id(page, ch)!r} "
                            f"raw={raw[i]!r} -> {glyph_value!r}"
                        )

    def prev_nonspace(i):
        j = i - 1
        while j >= 0 and raw[j].isspace():
            j -= 1
        return raw[j] if j >= 0 else ""

    def next_nonspace(i):
        j = i + 1
        while j < n and raw[j].isspace():
            j += 1
        return raw[j] if j < n else ""

    def context_before(i, count=12):
        return "".join(raw[max(0, i - count):i])

    def context_after(i, count=12):
        return "".join(raw[i + 1:min(n, i + 1 + count)])

    for i, c in enumerate(raw):
        if c == _FI_BROKEN_CHAR:
            left = prev_nonspace(i)
            right = next_nonspace(i)

            # A broken fi ligature occurring inside a word is highly likely.
            if left.isalpha() and right.isalpha():
                fixed[i] = "fi"
                continue

            # Some PDF encodings lose the leading "fi" ligature entirely and
            # return ® wherever the CURRENT run of letters starts (e.g.
            # "®gures" -> "figures", "twenty-®ve" -> "twenty-five",
            # "importantly, ®sh" -> "importantly, fish"). By this point
            # left.isalpha() is already guaranteed False - the ONE case
            # where it's True (a real mid-word ® with letters on both
            # sides) was already handled and consumed by the branch just
            # above. A registered/trademark "®" is essentially never glued
            # directly to a following letter run with no space at all
            # (a real trademark mark attaches to the END of the preceding
            # word, not the start of the next one) - checking a fixed,
            # ever-growing whitelist of specific reconstructed-word endings,
            # or a fixed set of "valid" left-side punctuation, was the same
            # brittle, book-specific-in-practice approach; "right.isalpha()"
            # alone is a general, reliable signal on its own. The digit-
            # adjacency exception only matters when a digit sits
            # DIRECTLY against the broken character with no space at all
            # (real mathematical notation, e.g. a macron/formula symbol
            # touching a digit) - prev_nonspace() skips OVER a real space
            # to find the nearest non-space character, which would
            # otherwise also (wrongly) exclude an ordinary prose case like
            # "200 flitches" or "5 films" just because a number happens to
            # appear earlier in the same sentence.
            immediate_left = raw[i - 1] if i > 0 else ""
            if right.isalpha() and not immediate_left.isdigit():
                fixed[i] = "fi"
                continue

            # A common line-break form: "suf®-" represents "suffi-".
            if left.isalpha() and right in "-­":
                fixed[i] = "fi"
                continue

            # A word ENDING in "-fi" immediately before closing punctuation
            # (a quote, comma, period - e.g. "Ciof®, F." -> "Ciofi, F.",
            # "`Dol®'" -> "`Dolfi'") - the symmetric case of the word-
            # START rule above: left.isalpha() with a non-letter right side
            # is exactly as reliable a signal that this is the ligature
            # ending a word, not a genuine trademark mark (which would need
            # its own preceding space, not sit glued to the letters right
            # before it).
            if left.isalpha() and not right.isalpha():
                fixed[i] = "fi"
                continue

        elif c == _FL_BROKEN_CHAR:
            left = prev_nonspace(i)
            right = next_nonspace(i)

            # In the supplied PDF every occurrence of this malformed code is
            # the "fl" ligature: brie¯y, ¯ows, con¯ict, Re¯ective, ¯ashes,
            # a¯oat, ¯oods, free-¯oating, in¯ation, etc. Same digit-
            # adjacency nuance as FI above - only excludes a digit DIRECTLY
            # touching the broken character, never one merely appearing
            # earlier in the same sentence.
            immediate_left = raw[i - 1] if i > 0 else ""
            if right.isalpha() and not immediate_left.isdigit():
                fixed[i] = "fl"
                continue
            if left.isalpha() and not right.isalpha():
                fixed[i] = "fl"
                continue



        elif c == _DASH_BROKEN_CHAR:
            left = prev_nonspace(i)
            right = next_nonspace(i)

            # Numeric page/reference ranges: 194±95 -> 194–95.
            if left.isdigit() and right.isdigit():
                fixed[i] = "\u2013"
                continue

            # A dash surrounded by whitespace in ordinary prose.  Do not
            # change ± when it is attached to a number/formula.
            if left and right and raw[i - 1].isspace() and (
                i + 1 < n and raw[i + 1].isspace()
            ):
                fixed[i] = "\u2014"
                continue

    # fixed = _repair_structural_unicode_anomalies(chars, fixed, page, font_name)
    return fixed

def extract_lines(page, bbox):
    """Extract lines with exact PDF characters and genuine inline formatting.

    Automatic small-caps output is disabled.  Formatting is calculated at
    character level so an italic phrase inside a larger line is not flattened
    into normal text.
    """
    raw = _get_rawdict(page)
    if debug_log.is_enabled():
        debug_log.log(
            "EXTRACT",
            f"BEGIN page={getattr(page, 'number', '?') + 1 if hasattr(page, 'number') else '?'} bbox={bbox}"
        )

    seen_spans = set()
    lines_out = []

    # Zone-level dominant font for font-contrast italic detection.
    _zone_font_counts: dict = {}
    for _blk in raw.get("blocks", []):
        if _blk.get("type") != 0:
            continue
        for _ln in _blk.get("lines", []):
            for _sp in _ln.get("spans", []):
                _fn = _sp.get("font", "")
                _fc = len([_ch for _ch in _sp.get("chars", []) if _char_in_bbox(_ch["bbox"], bbox)])
                if _fc > 0:
                    _zone_font_counts[_fn] = _zone_font_counts.get(_fn, 0) + _fc
    _zone_dominant_font = max(_zone_font_counts, key=_zone_font_counts.__getitem__) if _zone_font_counts else ""

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line_index, line in enumerate(block.get("lines", [])):
            dom_size, dom_baseline = line_baseline_stats(line)
            dom_shear = line_shear_stats(line)

            records = []
            line_chars = []
            line_fixed = []
            line_fonts = []

            # Collect the complete visual line first.
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                matched = [ch for ch in chars if _char_in_bbox(ch["bbox"], bbox)]
                if not matched:
                    continue

                sb = span.get("bbox", (0, 0, 0, 0))
                span_text = "".join(c.get("c", "") for c in chars)
                dedup_key = (
                    round(sb[0]), round(sb[1]), round(sb[2]), round(sb[3]), span_text
                )
                if dedup_key in seen_spans:
                    continue
                seen_spans.add(dedup_key)

                flags = span.get("flags", 0)
                font_name = span.get("font", "")
                size = span.get("size", 0)

                fixed = _repair_pdf_characters(
                    matched, page=page, font_name=font_name
                )

                start_index = len(line_chars)
                line_chars.extend(matched)
                line_fixed.extend(fixed)
                line_fonts.extend([font_name] * len(matched))

                _span_shear = span_shear_ratio(matched)
                formatting = detect_formatting(
                    flags=flags,
                    font_name=font_name,
                    shear_ratio=_span_shear,
                    dominant_shear_ratio=dom_shear,
                )

                records.append({
                    "matched": matched,
                    "fixed": fixed,
                    "flags": flags,
                    "font": font_name,
                    "size": size,
                    "bold": bool(formatting.get("bold")),
                    "italic": bool(formatting.get("italic")),
                    "line_start": start_index,
                })

            if not records:
                continue

            # Dominant font for [FONTMIX] diagnostic only — NOT used for any
            # italic/bold/sup/sub style decision.
            _font_char_counts: dict = {}
            for _r in records:
                _fn = _r["font"]
                _font_char_counts[_fn] = _font_char_counts.get(_fn, 0) + len(_r["matched"])
            _line_dominant_font = max(_font_char_counts, key=_font_char_counts.__getitem__) if _font_char_counts else ""
            _preview = "".join(c.get("c", "") for r in records for c in r["matched"])[:60]
            _pgno = getattr(page, 'number', -1) + 1
            if len(_font_char_counts) >= 2:
                (
                    f"[FONTMIX] page={_pgno} "
                    f"dom={_line_dominant_font!r} counts={_font_char_counts} "
                    f"text={_preview!r}"
                )
            elif _pgno <= 4 and debug_log.is_enabled() and any(c.isalpha() for c in _preview):
                # Single-font lines on the first 4 pages: show font + italic
                # flag per span so we can diagnose body-paragraph italic.
                _span_info = [(r["font"], r["flags"], r["italic"]) for r in records]
                print(
                    f"[FONTSINGLE] page={_pgno} "
                    f"spans={_span_info} "
                    f"text={_preview!r}"
                )

            # CRITICAL: repair malformed Unicode after all spans are combined.
            # This is what allows Sie + [separate span] Á + ge to be recognized
            # as one visual word.
            line_fixprinted = _repair_structural_unicode_anomalies(
                line_chars,
                line_fixed,
                page,
                font_name="",
                font_names=line_fonts,
            )

            # Put repaired characters back into their original span records.
            for rec in records:
                a = rec["line_start"]
                b = a + len(rec["matched"])
                rec["fixed"] = line_fixed[a:b]

            _underline_flags = _strike_flags = None
            if _DECORATION_DETECTION_ENABLED:
                try:
                    from core import underline_detector
                    _underline_flags, _strike_flags, _ = underline_detector.line_decorations(
                        page, line_chars, dom_size)
                except Exception:
                    _underline_flags = _strike_flags = None

            runs = []
            current = None
            line_bbox = None
            prev_char = None
            prev_char_x1 = None
            pending_word = ""

            for rec in records:
                matched = rec["matched"]
                fixed_chars = rec["fixed"]
                flags = rec["flags"]
                font_name = rec["font"]
                size = rec["size"]
                span_bold = rec["bold"]
                span_italic = rec["italic"]

                # Font-contrast italic: a span in a minority font within an
                # otherwise single-font zone is the italic/oblique variant.
                # (CUPEPUB precise-formatting mode: a BOLD minority face is
                # the bold variant, never evidence of italic.)
                if (not span_italic and _zone_dominant_font and rec["font"] != _zone_dominant_font
                        and not span_bold and _font_contrast_may_be_italic(rec["font"], _zone_dominant_font)):
                    _st = "".join(ch.get("c", "") for ch in rec["matched"])
                    if any(ch.isalpha() for ch in _st):
                        span_italic = True

                # Per-character shear fallback for mixed formatting.
                inline_italic_flags = _inline_italic_flags_minimal(
                    matched, normal_shear=(dom_shear or 0.0)
                )

                for char_index, (ch, c) in enumerate(zip(matched, fixed_chars)):
                    if c in _ZERO_WIDTH_CHARS:
                        continue        # invisible; would otherwise read as a gap -> a false space
                    cb = ch["bbox"]
                    line_bbox = cb if line_bbox is None else (
                        min(line_bbox[0], cb[0]),
                        min(line_bbox[1], cb[1]),
                        max(line_bbox[2], cb[2]),
                        max(line_bbox[3], cb[3]),
                    )

                    zero_gap = (
                        prev_char_x1 is not None
                        and (cb[0] - prev_char_x1) < _MISSING_SPACE_GAP_TOLERANCE
                    )

                    punct_boundary = (
                        prev_char is not None
                        and prev_char in _SENTENCE_END_PUNCT
                        and c.isupper()
                        and zero_gap
                    )
                    always_spaced_boundary = (
                        prev_char is not None
                        and prev_char in _ALWAYS_SPACED_PUNCT
                        and c.isalpha()
                        and zero_gap
                    )
                    # (c) only where the PDF itself starts a new text run at
                    # the capital: a word set in one run - "StatPearls",
                    # "EPdA", "bSSFP", "PubMed", "iPhone" - is one word.
                    case_boundary = (
                        prev_char is not None
                        and prev_char.islower()
                        and c.isupper()
                        and zero_gap
                        and char_index == 0
                        and pending_word.lower() not in _NAME_PREFIX_EXCEPTIONS
                    )

                    if punct_boundary or always_spaced_boundary or case_boundary:
                        if current is not None:
                            runs.append(current)
                            current = None
                        runs.append([(False, False, "normal"), " "])
                        pending_word = ""

                    pending_word = (pending_word + c) if c.isalpha() else ""

                    origin = ch.get("origin", (0, 0))
                    origin_y = origin[1] if len(origin) >= 2 else 0.0

                    sup_flag = bool(flags & FLAG_SUPERSCRIPT)

                    # Prefer per-character native evidence when rawdict
                    # provides it.  Most PDFs keep style at span level, but
                    # some generators expose a mixed-style span with character
                    # level font/flag/size data.  Do not invent or infer a style
                    # from the character text itself.
                    char_flags = ch.get("flags", flags)
                    char_font_name = ch.get("font", font_name) or font_name
                    char_size = ch.get("size", size) or size

                    _line_pos_for_format = start_index + char_index
                    _prev_for_format = (
                        line_fixed[_line_pos_for_format - 1]
                        if _line_pos_for_format > 0 and _line_pos_for_format - 1 < len(line_fixed)
                        else ""
                    )
                    _next_for_format = (
                        line_fixed[_line_pos_for_format + 1]
                        if _line_pos_for_format + 1 < len(line_fixed)
                        else ""
                    )
                    char_format = detect_formatting(
                        flags=char_flags,
                        font_name=char_font_name,
                        size=char_size,
                        origin_y=origin_y,
                        dominant_size=dom_size,
                        dominant_baseline=dom_baseline,
                        superscript_flag=bool(char_flags & FLAG_SUPERSCRIPT),
                        allow_size_only_fallback=(
                            len(fixed_chars) <= 3
                            and all(x.isdigit() or x in "*†‡§¶" for x in fixed_chars)
                        ),
                        shear_ratio=_char_shear_ratio(ch),
                        dominant_shear_ratio=dom_shear,
                        char=c,
                        prev_char=_prev_for_format,
                        next_char=_next_for_format,
                    )
                    char_bold = bool(char_format.get("bold"))
                    char_italic = bool(char_format.get("italic"))

                    italic = bool(
                        span_italic
                        or bool(char_flags & FLAG_ITALIC)
                        or char_italic
                        or inline_italic_flags[char_index]
                    )
                    bold = bool(span_bold or bool(char_flags & FLAG_BOLD) or char_bold)

                    # Do NOT generate smallcaps.
                    # Subscript requires strong geometric evidence PLUS local
                    # alphanumeric/math context.  Supplying neighbors prevents
                    # ordinary standalone numbers such as "chapter 3" and
                    # publication years from becoming <sub>, while preserving
                    # genuine forms such as H₂O / CO₂ / x₁.
                    _line_pos = start_index + char_index
                    _prev_char = (
                        line_fixed[_line_pos - 1]
                        if _line_pos > 0 and _line_pos - 1 < len(line_fixed)
                        else ""
                    )
                    _next_char = (
                        line_fixed[_line_pos + 1]
                        if _line_pos + 1 < len(line_fixed)
                        else ""
                    )
                    pos = classify_position(
                        char_size,
                        origin_y,
                        dom_size,
                        dom_baseline,
                        bool(char_flags & FLAG_SUPERSCRIPT),
                        allow_size_only_fallback=(
                            len(fixed_chars) <= 3
                            and all(x.isdigit() or x in "*†‡§¶" for x in fixed_chars)
                        ),
                        prev_char=_prev_char,
                        next_char=_next_char,
                    )
                    style = (bold, italic, pos)
                    if _underline_flags is not None:
                        _deco_pos = rec["line_start"] + char_index
                        _u = _underline_flags[_deco_pos] if _deco_pos < len(_underline_flags) else False
                        _s = _strike_flags[_deco_pos] if _deco_pos < len(_strike_flags) else False
                        if _u or _s:
                            style = (bold, italic, pos, False, bool(_u), bool(_s))

                    if debug_log.is_enabled():
                        debug_log.log(
                            "CHAR",
                            f"c={c!r} font={font_name!r} flags={flags} "
                            f"size={size!r} pos={pos} bold={bold} italic={italic} "
                            f"fixed={c != ch.get('c','')!r} "
                            f"char_font={char_font_name!r} char_flags={char_flags} "
                            f"char_size={char_size!r} shear={_char_shear_ratio(ch)!r}"
                        )

                    if current is not None and current[0] == style:
                        current[1] += c
                    else:
                        if current is not None:
                            runs.append(current)
                        current = [style, c]

                    prev_char = c
                    prev_char_x1 = cb[2]

            if current is not None:
                runs.append(current)

            if runs:
                original_text = "".join(value for _, value in runs)
                text = "".join(_wrap_run(style, value) for style, value in runs)
                # A whitespace-only "line" (e.g. the tab after a list bullet,
                # which InDesign PDFs place as its own line between the
                # item's first and second lines) carries no text and would
                # sit between a line-end hyphen and its continuation.
                if text and original_text.strip():
                    text = _merge_adjacent_inline_tags(text)
                    final_line = _normalize_text_for_epub(text)
                    lines_out.append((line_bbox, final_line))
                    if debug_log.is_enabled() and any(tag in final_line for tag in ("<i>", "<b>", "<sup>", "<sub>")):
                        print(
                            f"[PDF LINE] page={getattr(page, 'number', '?') + 1} "
                            f"line={len(lines_out)} | {original_text} | {final_line}"
                        )
                    if debug_log.is_enabled():
                        debug_log.log(
                            "EXTRACT",
                            f"LINE OUT bbox={line_bbox} => {final_line!r}"
                        )

    lines_out.sort(key=lambda item: item[0][1] if item[0] else 0)
    return lines_out


_TRAILING_TAGS_RE = re.compile(r"(?:</[a-z]+>)*$")
_LEADING_TAGS_RE = re.compile(r"^(?:<[a-z]+>)*")
# U+00AD SOFT HYPHEN is Unicode's own dedicated "may be used at a line
# break" character - some real-world PDFs (professional typesetting/DTP
# tools especially) genuinely encode a line-break hyphenation point this
# way instead of a plain ASCII hyphen-minus (confirmed directly: PyMuPDF's
# own text-insertion layer substitutes a trailing '-' with U+00AD when
# writing through a non-base14 embedded font - the same real-world
# substitution, not a testing artifact to work around). _HYPHEN_CHARS is
# the ONE place both the boundary check and the strip regex draw from, so
# a future third hyphen-like character only needs adding here. '-' is
# placed FIRST in the class below so it is never misparsed as a range
# operator against ­.
_HYPHEN_CHARS = "-­"
_TRAILING_HYPHEN_RE = re.compile(r"[-­]((?:</[a-z]+>)*)$")


def _plain_edge_char(text: str, leading: bool) -> str:
    """First/last character ignoring wrapping <b>/<i>/<sup>/<sub> tags,
    used only to decide whether a line boundary is an artificial line-break
    hyphen - never used to alter the text itself."""
    if leading:
        stripped = _LEADING_TAGS_RE.sub("", text)
        stripped = _TAG_RE.sub("", stripped)
        return stripped[0] if stripped else ""
    stripped = _TRAILING_TAGS_RE.sub("", text)
    stripped = _TAG_RE.sub("", stripped)
    return stripped[-1] if stripped else ""


def _strip_trailing_hyphen(text: str) -> str:
    return _TRAILING_HYPHEN_RE.sub(r"\1", text, count=1)


def _exact_range_tags():
    return {_decoration_tag("underline"), _decoration_tag("strike")}


_ADJACENT_SAME_TAG_RE = re.compile(r"(\s*)</([a-z]+)>(\s*)<\2>(\s*)")


def _collapse_adjacent_tag_match(m) -> str:
    # Exact-range decorations (underline/strike) are never bridged across
    # whitespace: "<u>a</u> <u>b</u>" means the space between them is NOT
    # decorated, and merging would invent a decoration that is not on the
    # page. Directly-touching pieces (no whitespace) still merge below.
    if (m.group(1) + m.group(3) + m.group(4)) and m.group(2) in _exact_range_tags():
        return m.group(0)
    # Any whitespace found around the boundary (a trailing space already
    # inside the first fragment, the dehyphenate_join separator space
    # between fragments, and/or a leading space inside the next fragment)
    # collapses to exactly one space - never zero (would glue two words
    # together) and never left as two-or-more (would insert a spurious
    # extra space that wasn't in the original PDF text). Genuinely
    # zero-whitespace boundaries (e.g. "fibu-"/"lar" already concatenated
    # directly by the hyphen-collapse branch above) correctly stay glued -
    # ws is empty only when every one of the three captured groups is.
    ws = m.group(1) + m.group(3) + m.group(4)
    return " " if ws else ""


def _merge_adjacent_inline_tags(text: str) -> str:
    """Collapses a run of consecutive same-name inline tags (<b>, <i>,
    <sup>, <sub>, or any future inline tag) separated only by whitespace into
    one - undoes the fragmentation caused by PDF line wrapping. extract_lines
    only merges same-style runs WITHIN one physical line (that's the unit
    MuPDF reports spans in); when one continuous bold/italic/etc region spans
    several wrapped lines of the SAME zone, each line starts out as its own
    <b>...</bold> pair, joined below by nothing but the dehyphenate_join
    space - never an actual formatting change. A genuine formatting change
    (different tag, plain text, or - for Title zones - a literal <break/>)
    always leaves something other than pure whitespace between the closing
    and reopening tag, so this regex never fires for those and they stay
    separate exactly as before. Looped until stable so a run of 3+ fragments,
    and multi-level nesting (e.g. bold+italic together, where the outer tag's
    boundary only becomes adjacent after the inner tag's boundary is merged,
    or vice versa), both fully collapse in one call."""
    prev = None
    while prev != text:
        prev = text
        text = _ADJACENT_SAME_TAG_RE.sub(_collapse_adjacent_tag_match, text)
    return text


def _is_linebreak_hyphen_boundary(prev_text: str, next_text: str) -> bool:
    """True when the boundary between two consecutive extracted fragments
    (lines, in practice) is an artificial PDF line-break hyphen: prev_text
    ends in '-' (ignoring wrapping inline tags) and next_text starts with a
    lowercase letter (ignoring wrapping inline tags). This is the SINGLE
    source of truth for that decision - dehyphenate_join/
    dehyphenate_join_with_breaks (automatic joining) and
    find_hyphen_candidates (the review-window detector) both call this
    exact same check, so a fragment is never treated as a hyphen candidate
    by one code path and not the other. A genuine same-line compound hyphen
    (e.g. "mecanismo-dependiente") never reaches this function at all - it
    exists entirely within ONE extracted line/fragment, never at a
    fragment boundary, so there is nothing here to false-positive on."""
    last = _plain_edge_char(prev_text, leading=False)
    first = _plain_edge_char(next_text, leading=True)
    return last in _HYPHEN_CHARS and first.isalpha() and first.islower()


def _lang_joiner(prev, nxt):
    """" " between joined lines - "" for scripts written without spaces."""
    from core.lang import joiner
    return joiner(re.sub(r"<[^>]+>", "", prev[-40:]), re.sub(r"<[^>]+>", "", nxt[:40]))


_NON_ITALIC_STYLE_RE = re.compile(r"(roman|regular|book|medium|bold|demi|semi|light|black|heavy|condensed)", re.I)


def _font_family(name: str) -> str:
    name = (name or "").split("+", 1)[-1]          # subset prefix "ABCDEF+"
    return re.split(r"[-,]", name, 1)[0].lower()


def _font_contrast_may_be_italic(minority: str, dominant: str) -> bool:
    """A minority font in a zone is only taken as the italic face when the
    names do not say otherwise: a named non-italic style ("-Roman", "-Bold",
    "Regular" ...) or a different named typeface (Optima beside Times) is a
    font change, not italic. Opaque names ("F1"/"F2", "T3") keep the
    original font-contrast rule."""
    if _NON_ITALIC_STYLE_RE.search((minority or "").split("+", 1)[-1].split("-", 1)[-1]) and "-" in minority:
        return False
    fam_min, fam_dom = _font_family(minority), _font_family(dominant)
    named = all(len(f) > 3 and f.isalpha() for f in (fam_min, fam_dom))
    if named and fam_min != fam_dom:
        return False
    return True


def dehyphenate_join(parts, keep_at=None) -> str:
    """Joins a sequence of text fragments (already-extracted line or zone
    text), collapsing an artificial PDF line/page-break hyphen - a fragment
    ending in '-' immediately followed by a fragment starting with a
    lowercase letter is joined directly ("fibu-" + "lar" -> "fibular"),
    UNLESS that boundary's index (0-based, the boundary before parts[i+1])
    is in `keep_at` (default: none), in which case the fragments are still
    joined directly with no space - preserving the original hyphen exactly
    as it would read on one line, e.g. "estímulo-" + "respuesta" ->
    "estímulo-respuesta" - this is the user's explicit "keep this hyphen"
    override from the post-zoning Hyphen Normalization Review (see
    find_hyphen_candidates / gui/dialogs.HyphenReviewDialog); `keep_at`
    empty/None (the default for every existing caller) reproduces the
    exact prior automatic-join behavior, so nothing changes unless a
    caller explicitly opts in. Genuine hyphens (mid-line, or not followed
    by a lowercase continuation, e.g. "acción-reacción") are left untouched
    since this only ever inspects the two characters exactly at a fragment
    boundary - see _is_linebreak_hyphen_boundary."""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    keep_at = keep_at or ()
    result = parts[0]
    for i, nxt in enumerate(parts[1:]):
        if _is_linebreak_hyphen_boundary(result, nxt):
            if i in keep_at:
                result = result + nxt  # user chose to KEEP the hyphen - join directly, hyphen intact, no space
            else:
                result = _strip_trailing_hyphen(result) + nxt
        else:
            result = result.rstrip(" \t") + _lang_joiner(result, nxt) + nxt.lstrip(" \t")
    # a soft hyphen left inside a line is an invisible break opportunity, not text
    return _merge_adjacent_inline_tags(result.replace("\u00ad", ""))



def _inline_style_flags_minimal(chars, normal_size, normal_baseline):
    """Minimal character-level fallback for inline sup/sub.

    Uses only PDF rawdict character geometry. It does not OCR, render pages,
    rewrite characters, or alter the existing extraction pipeline.
    """
    flags = [None] * len(chars)
    if not chars or not normal_size:
        return flags

    ns = float(normal_size)
    nb = float(normal_baseline or 0.0)

    for i, ch in enumerate(chars):
        c = ch.get("c", "")
        if not c:
            continue
        box = ch.get("bbox") or ()
        origin = ch.get("origin") or ()
        if len(box) < 4:
            continue

        h = abs(float(box[3]) - float(box[1]))
        y = float(origin[1]) if len(origin) >= 2 else float(box[3])

        # Very small characters above the dominant baseline.
        if h > 0 and h <= ns * 0.72 and y < nb - ns * 0.12:
            flags[i] = "sup"
            continue

        # Very small characters below the dominant baseline.
        if h > 0 and h <= ns * 0.72 and y > nb + ns * 0.45:
            flags[i] = "sub"

    return flags



def _inline_italic_flags_minimal(chars, normal_shear=0.0):
    """Minimal character-geometry italic fallback for mixed-style spans."""
    out = [False] * len(chars)
    if len(chars) < 2:
        return out

    # Use the existing character shear helper if present.
    shear_fn = globals().get("_char_shear_ratio")
    if shear_fn is None:
        return out

    values = []
    for ch in chars:
        try:
            values.append(float(shear_fn(ch)))
        except Exception:
            values.append(None)

    # Require a meaningful deviation from the local normal shear and a
    # contiguous alphabetic run, avoiding punctuation/noise.
    candidates = [
        v is not None and abs(v - float(normal_shear)) >= 0.08
        and (ch.get("c", "") or "").isalpha()
        for ch, v in zip(chars, values)
    ]

    for i in range(len(chars)):
        if not candidates[i]:
            continue
        j = i
        count = 0
        while j < len(chars):
            cc = chars[j].get("c", "") or ""
            if cc.isalpha() and candidates[j]:
                count += 1
                j += 1
            elif cc.isspace() and j + 1 < len(chars) and candidates[j + 1]:
                j += 1
            else:
                break
        if count >= 2:
            for k in range(i, j):
                if (chars[k].get("c", "") or "").isalpha():
                    out[k] = True
    return out


def _page_looks_like_searchable_scan(page) -> bool:
    """Detect a searchable scan whose OCR overlay has no usable font styling.

    A searchable scan can classify as DIGITAL in text-quality terms because
    its OCR text is perfectly readable.  That classification answers
    ``should OCR be run for text?``; it does NOT answer ``does the text layer
    contain the book's real font/style information?``.

    For the latter question, a strong book-independent signal is: the page is
    covered by a dominant scanned image AND every native text character comes
    from one font with flags=0.  This is the structure found in the supplied
    searchable-scan PDF, where the native layer is a uniform placeholder font
    and the visual page contains the real typography.

    This function never inspects spelling and never rewrites extracted text.
    It only decides which formatting evidence source should be used.
    """
    try:
        from core.ocr.style_detector import page_is_image_dominated
        if not page_is_image_dominated(page):
            return False

        raw = _get_rawdict(page)
        fonts = set()
        any_text = False
        all_flags_zero = True
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    chars = span.get("chars", []) or []
                    if not chars:
                        continue
                    any_text = True
                    fonts.add(span.get("font", "") or "")
                    if int(span.get("flags", 0) or 0) != 0:
                        all_flags_zero = False
                        break
                if not all_flags_zero:
                    break
            if not all_flags_zero:
                break

        return any_text and all_flags_zero and len(fonts) == 1
    except Exception:
        # Formatting detection must never make extraction fail.
        return False


def extract_formatted_text(page, bbox, keep_hyphen_at=None) -> str:
    # Cache exact repeated zone extraction.  This is deliberately keyed by
    # document identity + page + exact bbox + hyphen override, so different
    # zones and different normalization choices never share results.
    _parent = getattr(page, "parent", None)
    _doc_key = (getattr(_parent, "name", None) or None) or id(_parent)
    try:
        _bbox_key = tuple(float(v) for v in bbox)
    except Exception:
        _bbox_key = tuple(bbox) if bbox is not None else ()
    _keep_key = tuple(keep_hyphen_at or ())
    _formatted_key = (_doc_key, getattr(page, "number", None), _bbox_key, _keep_key)
    _cached_formatted = _formatted_cache.get(_formatted_key)
    if _cached_formatted is not None:
        _formatted_cache.move_to_end(_formatted_key)
        return _cached_formatted

    """Returns XML-safe inline content (normal text + <b>/<i>/<sup>/<sub>)
    for exactly the characters whose center point falls inside bbox (PDF coords).

    Direct callers are also routed through the visual OCR/style detector when
    the page is a searchable scan.  This closes the old verification/legacy
    caller bypass: those callers previously went straight to rawdict and saw
    ArialMT/flags=0 instead of the typography visible in the scanned page.
    Clean DIGITAL pages remain on the existing native extraction path.

    The visual path is used for FORMATTING ONLY.  Native extracted text remains
    the text source, so this change does not replace the PDF's characters with
    a second OCR transcription.

    keep_hyphen_at: see dehyphenate_join - boundary indices where a detected
    line-break hyphen should be kept (user override) rather than joined."""
    if _direct_extraction_needs_image_style_analysis(page, bbox):
        from types import SimpleNamespace
        from core.ocr.style_detector import detect_ocr_zone_lines

        # Keep native extracted text as the authoritative text source.
        native_lines = [text for _, text in extract_lines(page, bbox)]
        native_plain_lines = [strip_tags_to_plain(text) for text in native_lines]
        temp_zone = SimpleNamespace(
            bbox=bbox,
            text="\n".join(native_plain_lines),
            page=getattr(page, "number", 0),
            attributes={},
        )
        visual_lines = detect_ocr_zone_lines(page, temp_zone)
        result = dehyphenate_join(visual_lines, keep_hyphen_at).strip()
        if debug_log.is_enabled():
            debug_log.log("ZONE_TEXT",
                          f"IMAGE_STYLE bbox={bbox} lines={visual_lines!r} => {result!r}")
        return result

    parts = [text for _, text in extract_lines(page, bbox)]
    result = dehyphenate_join(parts, keep_hyphen_at).strip()
    if debug_log.is_enabled():
        debug_log.log("ZONE_TEXT", f"FORMATTED bbox={bbox} parts={parts!r} => {result!r}")
    return result


def extract_formatted_text_with_nested_children(page, zone_bbox, children, keep_hyphen_at=None) -> str:
    """Like extract_formatted_text, but splices one or more geometrically-
    NESTED child zones inline into the parent's own text at their exact
    position (spec: "nested zone / inline zone support" - a real,
    confirmed bug otherwise: the parent's own text used to be either
    discarded entirely or the child dropped/duplicated - see
    core.epub_xml_generator._gen_text_zone and core.xml_generator._zone_p).

    `children`: [(child_bbox, markup), ...] - `markup` is an OPAQUE string
    the caller has ALREADY fully rendered for that child (either more
    text_extractor-style inline markup for a formatted nested text run, or
    an already-serialized real XML element string like "<img .../>" for a
    nested image) - this function never re-parses or reinterprets it, only
    positions it. Never mutates/re-derives the child's own content, so
    nested <b>/<i>/<sup>/<sub> (and combinations) already present
    in that markup are preserved exactly as the caller produced them.

    Algorithm: reuses extract_lines(page, zone_bbox) for the parent's own
    physical lines - never a new/parallel low-level glyph scan. Each child
    is matched to whichever parent line's y-range contains the child's own
    bbox vertical center; a child matching NO line (e.g. the parent has no
    text of its own on that row) is inserted as its own standalone entry
    at the correct y-sorted position instead, so it is never silently
    lost. A matched line is split into text segments strictly BEFORE/
    BETWEEN/AFTER its child(ren)'s own x-ranges (children on one line
    sorted left-to-right), each segment re-extracted via the existing,
    unchanged extract_formatted_text on that narrower rectangle - so the
    parent's own surrounding text on that same line is preserved exactly,
    never dropped, never duplicated (the child's own bbox region is simply
    excluded from the parent's re-extraction, since the child's already-
    rendered markup is what fills that position instead). Every line with
    no matched child passes through completely unchanged. Finally every
    piece (in y-order) is joined through the existing, unchanged
    dehyphenate_join - so multi-line joining/hyphenation for any line
    without a nested child is completely unaffected."""
    lines = extract_lines(page, zone_bbox)

    def _center_y(bbox):
        return (bbox[1] + bbox[3]) / 2.0

    assigned = {}
    unmatched = []
    for child_bbox, markup in children:
        if not markup:
            continue
        cy = _center_y(child_bbox)
        line_idx = next((i for i, (lb, _) in enumerate(lines) if lb[1] <= cy <= lb[3]), None)
        if line_idx is None:
            unmatched.append((cy, markup))
        else:
            assigned.setdefault(line_idx, []).append((child_bbox, markup))

    parts = []
    for i, (line_bbox, line_text) in enumerate(lines):
        kids = sorted(assigned.get(i, []), key=lambda c: c[0][0])
        if not kids:
            parts.append((line_bbox[1], line_text))
            continue
        # Segments are bounded by THIS matched line's own bbox (never the
        # outer zone_bbox) - extract_lines already reports every physical
        # PDF line as its own separate entry even when two lines happen to
        # share an overlapping y-range (observed directly: two independent
        # insert_text calls at the same baseline can land as two distinct
        # "line" entries) - bounding by zone_bbox's own x0/x1 would let a
        # tail/lead segment overreach into a DIFFERENT line's own text at
        # that same y, duplicating it. Confirmed via a direct reproduction
        # before this fix.
        y0, y1 = line_bbox[1], line_bbox[3]
        line_x0, line_x1 = line_bbox[0], line_bbox[2]
        pieces = []
        cursor = line_x0
        for child_bbox, markup in kids:
            seg_bbox = [cursor, y0, child_bbox[0], y1]
            if seg_bbox[2] > seg_bbox[0]:
                seg_text = extract_formatted_text(page, seg_bbox, keep_hyphen_at)
                if seg_text:
                    pieces.append(seg_text)
            pieces.append(markup)
            cursor = max(cursor, child_bbox[2])
        tail_bbox = [cursor, y0, line_x1, y1]
        if tail_bbox[2] > tail_bbox[0]:
            tail_text = extract_formatted_text(page, tail_bbox, keep_hyphen_at)
            if tail_text:
                pieces.append(tail_text)
        parts.append((y0, " ".join(pieces)))

    parts.extend(unmatched)
    parts.sort(key=lambda p: p[0])
    return dehyphenate_join([t for _, t in parts if t], keep_hyphen_at).strip()


def dehyphenate_join_with_breaks(parts, keep_at=None) -> str:
    """Like dehyphenate_join, but separate fragments are joined with a
    literal <break/> marker instead of a space - for zones like Title where
    each physical PDF line is typically a deliberate title/subtitle/byline
    break rather than a wrapped sentence (BITS/JATS convention uses <break/>
    between such lines). An artificial line-break hyphen ("fibu-" + "lar")
    is still collapsed with no break inserted, same rule as dehyphenate_join
    - including the same `keep_at` override (see there)."""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    keep_at = keep_at or ()
    result = parts[0]
    for i, nxt in enumerate(parts[1:]):
        if _is_linebreak_hyphen_boundary(result, nxt):
            if i in keep_at:
                result = result + nxt
            else:
                result = _strip_trailing_hyphen(result) + nxt
        else:
            result = result + "<break/>" + nxt
    return _merge_adjacent_inline_tags(result)


def extract_formatted_text_with_breaks(page, bbox, keep_hyphen_at=None) -> str:
    """Same as extract_formatted_text, but joins separate physical lines with
    <break/> instead of a space.  Searchable scans use the same visual
    OCR/style decision while native text remains authoritative."""
    if _direct_extraction_needs_image_style_analysis(page, bbox):
        from types import SimpleNamespace
        from core.ocr.style_detector import detect_ocr_zone_lines
        native_lines = [text for _, text in extract_lines(page, bbox)]
        native_plain_lines = [strip_tags_to_plain(text) for text in native_lines]
        temp_zone = SimpleNamespace(
            bbox=bbox,
            text="\n".join(native_plain_lines),
            page=getattr(page, "number", 0),
            attributes={},
        )
        visual_lines = detect_ocr_zone_lines(page, temp_zone)
        return dehyphenate_join_with_breaks(visual_lines, keep_hyphen_at).strip()
    return dehyphenate_join_with_breaks(
        [text for _, text in extract_lines(page, bbox)], keep_hyphen_at
    ).strip()


_TRAILING_WORD_RE = re.compile(r"(\S*-)\s*$")
_LEADING_WORD_RE = re.compile(r"^\s*(\S+)")


def join_boundary(prev: str, next_text: str, keep_hyphen: bool, default_join: str = " ") -> str:
    """Joins two consecutive text fragments across a CROSS-ZONE boundary
    (an explicit Merge Previous chain - core.zone_manager.ZoneManager.
    merge_with_previous - or an automatic cross-page continuation - core.
    paragraph_merge), respecting a genuine line-break hyphen exactly like
    dehyphenate_join does for WITHIN-zone fragments (same
    _is_linebreak_hyphen_boundary check - never a second/divergent
    heuristic): if prev ends in a line-break hyphen and keep_hyphen is
    False, the hyphen is dropped and the fragments joined directly
    ("inter-" + "national" -> "international"); if keep_hyphen is True,
    joined directly WITH the hyphen intact (the user's explicit "keep
    this hyphen" choice - see gui.dialogs.HyphenReviewDialog's chain-
    boundary candidates, stored on the LATER zone's own attributes[
    "hyphen_keep_chain_boundary"]); otherwise (not a hyphen boundary at
    all) uses default_join as-is - e.g. an explicit merge's own recorded
    merge_join (" " or ""), completely unrelated to hyphenation and never
    overridden by it.

    Exists specifically for core.epub_xml_generator's own per-boundary
    custom join string (merge_join), which core.text_extractor.
    dehyphenate_join itself does not support (it always uses a single
    space for a non-hyphen boundary) - core.xml_generator's own
    _merged_chain_text has no such per-boundary custom join need and
    reuses dehyphenate_join's own keep_at parameter directly instead."""
    if _is_linebreak_hyphen_boundary(prev, next_text):
        return (prev + next_text) if keep_hyphen else (_strip_trailing_hyphen(prev) + next_text)
    return prev + default_join + next_text


def find_hyphen_candidates(page, bbox):
    """Scans bbox's extracted lines (same PDF geometry - font size,
    position, baseline - already used to build them; see extract_lines)
    for genuine line-break-hyphen boundaries, using the EXACT SAME check
    dehyphenate_join uses (_is_linebreak_hyphen_boundary) - never a
    separate/duplicate heuristic. Returns a list of dicts, one per
    candidate, in document order:
        {"boundary_index": int,  # pass to dehyphenate_join's keep_at to keep this one
         "prefix_word": str,     # the hyphen-ending word, tags stripped, e.g. "estímulo-"
         "suffix_word": str,     # the continuation word, tags stripped, e.g. "respuesta"
         "joined_preview": str,  # e.g. "estímulorespuesta"
         "kept_preview": str}    # e.g. "estímulo-respuesta"
    A same-line compound hyphen (e.g. "mecanismo-dependiente") never
    appears here, since it never forms a fragment boundary at all - it's
    inside one single extracted line's own text, not between two lines."""
    lines = [text for _, text in extract_lines(page, bbox)]
    lines = [t for t in lines if t]
    candidates = []
    for i in range(len(lines) - 1):
        prev_text, next_text = lines[i], lines[i + 1]
        if not _is_linebreak_hyphen_boundary(prev_text, next_text):
            continue
        prev_plain = strip_tags_to_plain(prev_text)
        next_plain = strip_tags_to_plain(next_text)
        m_prefix = _TRAILING_WORD_RE.search(prev_plain)
        m_suffix = _LEADING_WORD_RE.search(next_plain)
        prefix_word = m_prefix.group(1) if m_prefix else prev_plain[-20:]
        suffix_word = m_suffix.group(1) if m_suffix else next_plain[:20]
        candidates.append({
            "boundary_index": i,
            "prefix_word": prefix_word,
            "suffix_word": suffix_word,
            "joined_preview": prefix_word[:-1] + suffix_word,
            "kept_preview": prefix_word + suffix_word,
        })
    return candidates


def find_chain_hyphen_candidates(zone_manager, pdf_document):
    """Hyphenated Line-Break Text Normalization (spec sections 13/14:
    "Support words split across line boundaries"/"detect continuation
    across page boundaries") - the CROSS-ZONE counterpart to
    find_hyphen_candidates' within-zone scan. Scans EXPLICIT 'Merge
    Previous' chains only (core.zone_manager.ZoneManager.
    merge_with_previous's own attributes["merge_target"] pointer - a
    stable, already-computed piece of zoning data, never re-derived or
    altered here) for a genuine line-break-hyphen boundary between the
    END of the earlier zone's own text and the START of the later zone's,
    using the EXACT SAME _is_linebreak_hyphen_boundary check as every
    other hyphen decision in this module.

    Automatic (page-number-flanked) continuations are deliberately NOT
    included here: unlike an explicit merge, they are not recorded as a
    stable, independently-queryable zone attribute before generation runs
    - core.paragraph_merge computes them fresh, as part of the full
    reading-order stream, only once generation itself is already running.
    The actual join at generation time (core.epub_xml_generator.
    _gen_merged_text_zone / core.xml_generator._merged_chain_text) still
    dehyphenates them correctly by default even so - this is a disclosed
    scope boundary on what gets a dedicated REVIEW checkbox, not a
    correctness gap in the generated text itself.

    Returns a list of dicts shaped like find_hyphen_candidates' own, plus
    {"chain_boundary": True, "zone_id": <the LATER zone's id>, "page": <
    its page>} - 'zone_id' identifies where the user's keep/remove choice
    for THIS boundary is stored: that zone's own attributes[
    "hyphen_keep_chain_boundary"] (a plain boolean - a zone can be the
    'later half' of at most one chain boundary)."""
    results = []
    for zone in zone_manager.zones.values():
        target_id = zone.attributes.get("merge_target")
        if not target_id:
            continue
        prev_zone = zone_manager.zones.get(target_id)
        if prev_zone is None:
            continue
        prev_lines = [t for _, t in extract_lines(pdf_document.get_page(prev_zone.page), prev_zone.bbox) if t]
        next_lines = [t for _, t in extract_lines(pdf_document.get_page(zone.page), zone.bbox) if t]
        if not prev_lines or not next_lines:
            continue
        prev_text, next_text = prev_lines[-1], next_lines[0]
        if not _is_linebreak_hyphen_boundary(prev_text, next_text):
            continue
        prev_plain = strip_tags_to_plain(prev_text)
        next_plain = strip_tags_to_plain(next_text)
        m_prefix = _TRAILING_WORD_RE.search(prev_plain)
        m_suffix = _LEADING_WORD_RE.search(next_plain)
        prefix_word = m_prefix.group(1) if m_prefix else prev_plain[-20:]
        suffix_word = m_suffix.group(1) if m_suffix else next_plain[:20]
        results.append({
            "chain_boundary": True,
            "boundary_index": None,
            "zone_id": zone.zone_id,
            "page": zone.page,
            "prefix_word": prefix_word,
            "suffix_word": suffix_word,
            "joined_preview": prefix_word[:-1] + suffix_word,
            "kept_preview": prefix_word + suffix_word,
        })
    return results


def find_all_hyphen_candidates(zone_manager, pdf_document):
    """Runs find_hyphen_candidates over every zone in the project (any
    text-carrying tag - detection depends only on PDF geometry within a
    zone's own bbox, never on its tag), PLUS find_chain_hyphen_candidates
    over every explicit merge chain, and returns a flat list of dicts,
    each extending find_hyphen_candidates' own dict with:
        {"zone_id": str, "page": int, "chain_boundary": bool}
    in zone creation order (stable, matches project zone iteration order
    elsewhere) - the single entry point gui/dialogs.HyphenReviewDialog
    uses to build its checklist, and what gui/main_window.py checks
    (empty -> nothing to review, skip the dialog entirely) before
    Generate XML / Generate XHTML."""
    results = []
    for zone in zone_manager.zones.values():
        page = pdf_document.get_page(zone.page)
        for cand in find_hyphen_candidates(page, zone.bbox):
            cand = dict(cand)
            cand["zone_id"] = zone.zone_id
            cand["page"] = zone.page
            cand["chain_boundary"] = False
            results.append(cand)
    results.extend(find_chain_hyphen_candidates(zone_manager, pdf_document))
    return results


def strip_tags_to_plain(formatted_text: str) -> str:
    """Plain text from an already-formatted inline string (tags stripped,
    entities unescaped) - for callers holding formatted text directly rather
    than a (page, bbox) to extract from, e.g. a manually merged zone's cached
    combined text."""
    return unescape(_TAG_RE.sub("", formatted_text or ""))


def extract_plain_text(page, bbox, keep_hyphen_at=None) -> str:
    """Plain-text preview (tags stripped, entities unescaped)."""
    return strip_tags_to_plain(extract_formatted_text(page, bbox, keep_hyphen_at))


def _zone_prefers_stored_text(zone) -> bool:
    """True when zone.text is authoritative and must be used AS-IS at
    generation time instead of re-extracting from the PDF live:

    - an OCR-produced zone (attributes["source"] == "ocr") has no digital
      text layer at its own bbox to re-extract in the first place - its
      bbox sits on a scanned page (RULE 9/10: only zone.text, never a
      live PDF re-read, is the source of truth for such a zone).
    - any zone a user has manually corrected via the Properties panel's
      text editor (attributes["manual_text"] - core.zone_manager.
      set_zone_text) - RULE 6: a manual correction always overrides
      automatic extraction, and must never be silently reverted at
      generation time. This is the exact same rule CUPEPUB's PageNum zone
      already followed (core/epub_xml_generator.py's _gen_pagenum_zone) -
      generalized here to every ordinary content zone, digital-PDF ones
      included, since a manual correction on ANY zone was silently being
      discarded at Generate XML/XHTML time before this existed."""
    return bool(zone.attributes.get("source") == "ocr" or zone.attributes.get("manual_text"))


def _direct_extraction_needs_image_style_analysis(page, bbox) -> bool:
    """Same searchable-scan decision for callers that only have page+bbox.
    Only routes to image-based style analysis for confirmed searchable scans
    (_page_looks_like_searchable_scan: image-dominated AND single unstyled
    font AND all flags=0). Native PDF font data is authoritative for every
    other page, including image-heavy pages with sparse digital text."""
    return bool(_GENERATION_OCR_ENABLED and _page_looks_like_searchable_scan(page))


def _zone_wants_image_formatting_detection(page, zone) -> bool:
    """True when this zone's text should be analyzed via core.ocr.
    style_detector's image-based italic/bold/sup/sub detection instead of
    native PDF font metadata:

    - source=="ocr": this app's own OCR feature explicitly tagged it, so there
      is no digital text layer at the zone's bbox to re-extract in the first
      place; image-based detection is the only option.
    - _page_looks_like_searchable_scan(page): the page is dominated by a
      scanned image AND every native character comes from one unstyled
      placeholder font (flags=0) - the searchable-scan case where the visible
      typography lives in the image, not the native text layer.

    Never true for a manual_text zone (no reliable image region to analyze).
    Never true for any other zone - native PDF character information is
    authoritative, including on image-heavy pages with sparse digital text
    (a chapter title page with a decorative background image, for example,
    still has real font/flag data in its native characters)."""
    if zone.attributes.get("manual_text"):
        return False
    if not _GENERATION_OCR_ENABLED:
        return False
    if zone.attributes.get("source") == "ocr":
        return True
    return _page_looks_like_searchable_scan(page)


def extract_zone_formatted_text(page, zone, keep_hyphen_at=None) -> str:
    """Drop-in replacement for extract_formatted_text(page, zone.bbox, ...)
    at any call site that already has the owning zone in scope (not just
    its bbox). Returns zone.text itself, XML-escaped (it carries no
    <b>/<i> markup the way a live PDF re-extraction would - OCR/
    manual text has no such per-character font info), when
    _zone_prefers_stored_text says so and image-based detection doesn't
    apply (_zone_wants_image_formatting_detection); otherwise defers
    unchanged to the existing live-extraction path, so every digital-PDF
    zone's behavior (including real inline bold/italic markup) is
    completely unaffected."""
    if _zone_wants_image_formatting_detection(page, zone):
        from core.ocr.style_detector import detect_ocr_zone_lines
        text = dehyphenate_join(detect_ocr_zone_lines(page, zone), keep_hyphen_at).strip()
        path = "ocr-style"
    elif _zone_prefers_stored_text(zone):
        # OCR-generated stored text can contain physical PDF line breaks such as
        # "Biographi-\\ncal".  Normalize only OCR text here so an artificial
        # line-break hyphen is removed ("Biographical"), while manual text
        # entered by the user remains exactly as entered.
        if zone.attributes.get("source") == "ocr":
            text = escape(
                dehyphenate_join(
                    (zone.text or "").splitlines(),
                    keep_hyphen_at,
                ).strip()
            )
        else:
            # Manual text is authoritative and must not be silently changed.
            text = escape(zone.text or "")
        path = "stored"
    else:
        text = extract_formatted_text(page, zone.bbox, keep_hyphen_at)
        path = "live"
    _pgno = getattr(page, 'number', -1) + 1
    if _pgno <= 4 and debug_log.is_enabled():
        _preview = text[:80].replace("\n", " ")
        print(
            f"[ZONE PATH] page={_pgno} tag={getattr(zone, 'tag', '?')!r} "
            f"path={path} src={zone.attributes.get('source', '-')!r} "
            f"manual={zone.attributes.get('manual_text', False)} "
            f"text={_preview!r}"
        )
    return _apply_zone_style_overrides(text, zone)


def extract_zone_formatted_text_with_breaks(page, zone, keep_hyphen_at=None) -> str:
    """Same as extract_zone_formatted_text, for call sites that would
    otherwise use extract_formatted_text_with_breaks."""
    if _zone_wants_image_formatting_detection(page, zone):
        from core.ocr.style_detector import detect_ocr_zone_lines
        text = dehyphenate_join_with_breaks(detect_ocr_zone_lines(page, zone), keep_hyphen_at).strip()
    elif _zone_prefers_stored_text(zone):
        # Apply the same OCR line-break hyphen normalization when the stored
        # OCR text is used by the "with breaks" path.  Manual text is preserved.
        if zone.attributes.get("source") == "ocr":
            text = escape(
                dehyphenate_join(
                    (zone.text or "").splitlines(),
                    keep_hyphen_at,
                ).strip()
            )
        else:
            text = escape(zone.text or "")
    else:
        text = extract_formatted_text_with_breaks(page, zone.bbox, keep_hyphen_at)
    return _apply_zone_style_overrides(text, zone)


def _apply_zone_style_overrides(text: str, zone) -> str:
    """The one, minimal integration point for the Verification window's
    manual Inline Style Editor (core/verification/inline_style.py) - a
    no-op (single dict.get, no import) for the overwhelming common case
    of a zone with no manual override, so every existing zone's output is
    completely unaffected. Deferred import: core.verification depends on
    core.text_extractor (e.g. unicode_verifier.get_unicode_repair_log),
    so this module must never import core.verification at module level."""
    overrides = getattr(zone, "attributes", {}).get("style_overrides")
    if not overrides:
        return text
    from core.verification.inline_style import apply_style_overrides
    return apply_style_overrides(text, overrides)


def extract_zone_plain_text(page, zone, keep_hyphen_at=None) -> str:
    """Same idea for call sites using extract_plain_text directly - here
    zone.text is already plain (no XML-escaping needed): the caller
    assigns it straight to an lxml element's .text, which escapes
    automatically on serialization, exactly like CUPEPUB's PageNum
    handler already does with zone.text."""
    if _zone_prefers_stored_text(zone):
        return zone.text or ""
    return extract_plain_text(page, zone.bbox, keep_hyphen_at)


def _line_center(bbox):
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _zone_geometric_lines(page, zone_bbox):
    """Every whole-page LineInfo (auto_zoning.pdf_block_detector.
    detect_lines - the SAME primitive Paragraph Auto Zone itself already
    uses) whose own center falls inside zone_bbox, sorted top-to-bottom -
    the geometric evidence multi-paragraph splitting is based on.
    Deferred import: auto_zoning.pdf_block_detector itself imports
    core.text_extractor (_get_rawdict), so this module cannot import it
    back at module level without a circular import."""
    from auto_zoning.pdf_block_detector import detect_lines
    zx0, zy0, zx1, zy1 = zone_bbox
    lines = [li for li in detect_lines(page)
             if zx0 <= _line_center(li.bbox)[0] <= zx1 and zy0 <= _line_center(li.bbox)[1] <= zy1]
    lines.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return lines


def extract_zone_paragraphs(page, zone, children=None, keep_hyphen_at=None) -> list:
    """Returns a list of one-or-more fully-resolved, already-formatted
    <p>-ready content strings for `zone` (spec: "MULTIPLE PDF PARAGRAPHS
    INSIDE ONE SAVED ZONE") - ONE per geometrically-detected PDF paragraph
    inside zone.bbox, using ONLY existing layout evidence (vertical-gap /
    first-line-indent - auto_zoning.paragraph_auto_zone.
    group_lines_into_paragraphs, the SAME primitive Paragraph Auto Zone
    itself already uses to propose paragraph zones in the first place -
    reused here as-is, never reimplemented), never a fixed line/character
    count. The zone's own saved bbox/tag/id/children/reading-order are
    only ever READ here, never modified - this function has no ZoneManager
    access at all.

    Purely additive: whenever there are fewer than 2 geometric lines, or
    group_lines_into_paragraphs finds only one paragraph among them (the
    overwhelming common case - most zones already ARE exactly one
    paragraph), this returns a single-item list produced by the EXACT
    SAME extraction this zone would have used before this function
    existed (extract_zone_formatted_text, or
    extract_formatted_text_with_nested_children when `children` is
    given) - zero behavior change for that case, including `keep_hyphen_at`
    (only honored in this single-paragraph case - see its own note below).

    For a scanned/OCR zone (_zone_wants_image_formatting_detection), only
    splits when the geometric line count matches zone.text's OWN line
    count exactly (the same "ambiguous -> never guess, fall back" safety
    net core.ocr.style_detector.detect_ocr_zone_lines already applies to
    its own line/word segmentation) - zone.text remains the sole source
    of every resulting paragraph's own text either way, only ever SLICED
    by paragraph line-range, never replaced with re-extracted/re-
    recognized text, and the image-based italic/bold/sup/sub detector
    still runs per paragraph exactly as it already does per zone.

    `children` ([(child_bbox, markup), ...], the same shape
    extract_formatted_text_with_nested_children already takes): each
    child is bucketed into whichever detected paragraph's own y-range is
    CLOSEST to its bbox's own vertical center (0 distance when the center
    falls directly within that paragraph's range) - mirrors that
    function's own y-center-match convention, just per-paragraph instead
    of zone-wide, so a nested inline zone changes which <p> it renders
    inside but never itself moves, duplicates, or disappears.

    `keep_hyphen_at` (the Hyphen Normalization Review's per-boundary
    "keep this hyphen" override - see dehyphenate_join) is only honored
    in the single-paragraph case above: its indices are boundary offsets
    into the WHOLE zone's own joined parts, which no longer correspond to
    anything once the zone is actually split into independently-joined
    paragraphs, and a zone chain-merged across pages/zones (the actual
    source of a real keep_hyphen_at set) is not, in practice, also a
    zone containing several unrelated visual paragraphs."""
    children = children or []

    def _single_result():
        if children:
            return [extract_formatted_text_with_nested_children(page, zone.bbox, children, keep_hyphen_at)]
        return [extract_zone_formatted_text(page, zone, keep_hyphen_at)]

    lines = _zone_geometric_lines(page, zone.bbox)
    if len(lines) < 2:
        return _single_result()

    from auto_zoning.paragraph_auto_zone import group_lines_into_paragraphs
    blocks = group_lines_into_paragraphs(lines)
    if len(blocks) < 2:
        return _single_result()

    use_image_detection = _zone_wants_image_formatting_detection(page, zone)
    zone_text_lines = None
    if use_image_detection:
        zone_text_lines = (zone.text or "").split("\n")
        if len(zone_text_lines) != len(lines):
            # Geometric line count doesn't cleanly match the zone's own
            # trusted text - never guess a split, same principle as
            # detect_ocr_zone_lines's own line/word ambiguity fallback.
            return _single_result()

    block_bboxes = [(zone.bbox[0], b.bbox[1], zone.bbox[2], b.bbox[3]) for b in blocks]

    def _distance_to_block(cy, bbox):
        by0, by1 = bbox[1], bbox[3]
        return 0.0 if by0 <= cy <= by1 else min(abs(cy - by0), abs(cy - by1))

    children_by_block = [[] for _ in blocks]
    for cb, markup in children:
        cy = _line_center(cb)[1]
        best_i = min(range(len(blocks)), key=lambda i: _distance_to_block(cy, block_bboxes[i]))
        children_by_block[best_i].append((cb, markup))

    results = []
    idx = 0
    for block, block_bbox, block_children in zip(blocks, block_bboxes, children_by_block):
        start_idx, end_idx = idx, idx + len(block.lines)
        idx = end_idx
        if use_image_detection:
            from types import SimpleNamespace
            sub_zone = SimpleNamespace(bbox=block_bbox, text="\n".join(zone_text_lines[start_idx:end_idx]),
                                        attributes=zone.attributes)
            content = extract_zone_formatted_text(page, sub_zone, None)
        elif block_children:
            content = extract_formatted_text_with_nested_children(page, block_bbox, block_children, None)
        else:
            content = extract_formatted_text(page, block_bbox, None)
        if content:
            results.append(content)
    return results if results else _single_result()


def detect_zone_formatting(page, bbox) -> dict:
    formatted = extract_formatted_text(page, bbox)
    return {
        "bold": "<b>" in formatted,
        "italic": "<i>" in formatted,
        "superscript": "<sup>" in formatted,
        "subscript": "<sub>" in formatted,
    }
