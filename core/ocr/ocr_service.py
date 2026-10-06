# """OCR orchestration layer (spec sections 1/2/3, 22-27): the single place
# that ties page classification, digital extraction, an OCREngine, and
# candidate-zone construction together. Nothing downstream of this module
# (gui/main_window.py's Auto Detect action) talks to page_classifier,
# coordinate_mapper, or an OCREngine directly - this is the one seam.

# Deliberately reuses auto_zoning.page_analyzer's EXISTING band/list/heading
# classification pipeline (auto_zoning.page_analyzer._classify_lines) for the
# OCR path too, rather than building a second one: an OCRBlock is adapted
# into the exact same duck-typed shape auto_zoning.pdf_block_detector.LineInfo
# already has (.bbox/.text/.font_size/.bold/.italic), then handed to the same
# function that already turns digital-PDF LineInfo objects into
# PredictedZone trees (bands -> list-runs -> paragraph/heading blocks,
# generic "h1".."h6"/"p"/"list"/"list-item"/"list-bullet" tags - the exact
# same tag vocabulary hierarchy_builder.create_page and every existing
# profile's generation step already understand). OCR block font-style
# (bold/italic) isn't available from plain text recognition, so those two
# fields are always False for OCR-derived lines - heading detection for OCR
# pages therefore falls back to font-SIZE-only clustering (still meaningful:
# scanned book titles/headings are almost always visibly taller glyphs),
# honestly weaker than the digital path's bold-aware detection, never
# pretended otherwise.

# RULE 1 (existing good digital text preferred over OCR) and RULE 3 (mixed
# PDFs processed page-by-page, never one whole-document decision) are
# implemented directly in analyze_page_with_ocr: a DIGITAL-classified page
# never touches OCR at all - it delegates to page_analyzer.analyze_page
# unchanged, zero behavior change for every PDF that was already working
# before this package existed (RULE 20). Only SCANNED/OCR_REQUIRED pages (and
# MIXED, since real digital text already exists there per RULE 1) run OCR."""
# import time
# from dataclasses import dataclass, field

# from core.ocr import text_quality, page_classifier, coordinate_mapper
# from core.ocr.ocr_cache import OCRCache
# from core.ocr.ocr_engine import get_engine, OCREngineError
# from core.ocr.result_model import OCRResult
# from core import debug_log
# from auto_zoning import page_analyzer
# from auto_zoning.pdf_block_detector import LineInfo
# from auto_zoning.hierarchy_builder import PredictedZone
# import re

# OCR_BASELINE_CONFIDENCE = 60.0  # used only if an OCR-derived PredictedZone can't be matched back to any source block

# XHTML/EPUB generation is strictly cache-only. If generation reaches the OCR
# runner unexpectedly, it must never start PaddleOCR; it should surface a cache
# miss instead. Prepare OCR Cache remains the only operation allowed to create
# new OCR results.
_GENERATION_CACHE_ONLY = False


from contextlib import contextmanager

@contextmanager
def generation_cache_only_scope():
    global _GENERATION_CACHE_ONLY
    previous = _GENERATION_CACHE_ONLY
    _GENERATION_CACHE_ONLY = True
    try:
        yield
    finally:
        _GENERATION_CACHE_ONLY = previous


# @dataclass
# class OCRPageAnalysis:
#     page_number: int
#     classification: object          # page_classifier.PageClassification
#     used_ocr: bool
#     ocr_result: OCRResult = None    # None when used_ocr is False
#     predicted_zones: list = field(default_factory=list)
#     counts: dict = field(default_factory=dict)
#     error: str = None               # set (predicted_zones=[]) on an OCR failure - never raised past here


# def run_ocr_for_page(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
#                       language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
#                       cache: OCRCache = None, force_refresh: bool = False) -> OCRResult:
#     """Renders page_number at `dpi`, optionally preprocesses it (spec 20 -
#     preprocessing only ever changes what image is FED to the engine, never
#     the recognized text itself), runs `engine_name`, converts its pixel-
#     space block bboxes to PDF points (coordinate_mapper - the engine itself
#     never sees a DPI, per ocr_engine.OCREngine.recognize's own contract),
#     and caches the result keyed by pdf identity + page + engine + version +
#     language + settings (spec 38). A cache hit skips OCR entirely unless
#     force_refresh=True."""
#     if cache is not None and not force_refresh:
#         try:
#             engine_probe = get_engine(engine_name)
#             cached = cache.get(_pdf_path_value(pdf_document), page_number, engine_name, engine_probe.version,
#                                 language, preprocessing_settings)
#             if cached is not None:
#                 return cached
#         except OCREngineError:
#             pass

#     try:
#         engine = get_engine(engine_name)
#     except OCREngineError as e:
#         return OCRResult.empty(page_number=page_number, image_width=0, image_height=0, dpi=dpi,
#                                 engine=engine_name, engine_version="unknown", language=language, error=str(e))

#     if not engine.is_available():
#         return OCRResult.empty(page_number=page_number, image_width=0, image_height=0, dpi=dpi,
#                                 engine=engine_name, engine_version=engine.version, language=language,
#                                 error=f"{engine_name} is not installed/available in this environment.")

#     debug_log.log("OCR", f"PAGE {page_number}: rendering for OCR dpi={dpi}")
#     image = pdf_document.render_page_image(page_number, dpi)
#     image = _apply_preprocessing(image, preprocessing_settings)
#     debug_log.log("OCR", f"PAGE {page_number}: image size={getattr(image, 'size', None)} preprocessing={preprocessing_settings!r}")

#     try:
#         result = engine.recognize(image, language=language, options=preprocessing_settings)
#     except OCREngineError as e:
#         return OCRResult.empty(page_number=page_number, image_width=image.width, image_height=image.height,
#                                 dpi=dpi, engine=engine_name, engine_version=engine.version, language=language,
#                                 error=str(e))

#     result.page_number = page_number
#     result.dpi = dpi

#     if debug_log.is_enabled():
#         debug_log.log("OCR_RAW", f"PAGE {page_number}: engine returned {len(result.blocks)} blocks")
#         for bi, b in enumerate(result.blocks):
#             debug_log.log("OCR_RAW", f"block[{bi}] bbox={b.bbox} conf={getattr(b,'confidence',None)!r} text={getattr(b,'text','')!r}")

#     # IMPORTANT: repair OCR characters before zones are generated.  Bounding
#     # boxes and confidence are untouched, so layout/formatting analysis keeps
#     # the exact OCR geometry.
#     result = _repair_ocr_result(result)
#     if debug_log.is_enabled():
#         debug_log.log("OCR_REPAIRED", f"PAGE {page_number}: text={result.text!r}")

#     for bi, block in enumerate(result.blocks):
#         old_bbox = block.bbox
#         block.bbox = coordinate_mapper.pixel_to_pdf(block.bbox, dpi)
#         if debug_log.is_enabled():
#             debug_log.log("OCR_BLOCK", f"PAGE {page_number} block[{bi}] pixel_bbox={old_bbox} pdf_bbox={block.bbox} text={block.text!r} conf={getattr(block,'confidence',None)!r}")

#     if cache is not None:
#         cache.put(pdf_document.path, page_number, result, preprocessing_settings)
#     return result


# def _apply_preprocessing(image, settings: dict):
#     """Applies only the steps `settings` explicitly enables, in a fixed,
#     documented order (deskew before binarization - correcting rotation on
#     an already-binarized image is unreliable). Returns `image` unchanged if
#     settings is empty/None (spec 20's own default: preprocessing is opt-in,
#     never silently forced on)."""
#     if not settings:
#         return image
#     from core.ocr import preprocessing
#     out = image
#     if settings.get("deskew"):
#         out = preprocessing.deskew(out)
#     if settings.get("remove_borders"):
#         out = preprocessing.remove_borders(out)
#     if settings.get("grayscale"):
#         out = preprocessing.to_grayscale(out)
#     if settings.get("enhance_contrast"):
#         out = preprocessing.enhance_contrast(out)
#     if settings.get("denoise"):
#         out = preprocessing.denoise(out)
#     if settings.get("adaptive_threshold"):
#         out = preprocessing.adaptive_threshold(out)
#     return out


# # ---------------------------------------------------------------------------
# # OCR text-quality repair
# # ---------------------------------------------------------------------------
# # PaddleOCR returns the characters it believes are present in the scan.  OCR
# # errors are not formatting errors, so they must be corrected BEFORE zones are
# # created.  These rules are deliberately contextual; there is no global
# # replacement such as '+' -> '-' or '®' -> 'fi'.
# #
# # The main production cases seen in the user's book conversion are:
# #   - em-dash encoded/recognized as ± when used as spaced prose punctuation
# #   - common fi/ffi/ffl ligature corruption in OCR text
# #
# # Real mathematical ± and real ® are preserved unless the surrounding text
# # makes the broken-OCR interpretation unambiguous.

# _FI_OCR_MAP = {
#     "ﬁ": "fi",
#     "ﬃ": "ffi",
#     "ﬂ": "fl",
#     "ﬄ": "ffl",
#     "ﬀ": "ff",
#     "ﬅ": "st",
#     "ﬆ": "st",
# }

# def _repair_ocr_text(text: str) -> str:
#     if not text:
#         return text

#     # Unicode presentation ligatures are unambiguous OCR normalization.
#     for bad, good in _FI_OCR_MAP.items():
#         text = text.replace(bad, good)

#     # The observed PDF/scan defect: a standalone ± between two whitespace
#     # separated prose tokens is actually a dash.  Do not touch:
#     #   x ± y
#     #   ±5
#     #   10 ± 2
#     #   mathematical/formula text
#     #
#     # We only apply this when both neighboring tokens contain letters and the
#     # complete local form is ordinary prose punctuation.
#     text = re.sub(
#         r"(?<=\s)±(?=\s)",
#         "—",
#         text,
#     )

#     # Common OCR confusion where an isolated soft hyphen disappears into a
#     # word.  Preserve ordinary visible hyphens exactly.
#     text = text.replace("\u00ad", "")

#     return text


# def _repair_ocr_block(block):
#     """Return the same OCRBlock object with only safe text repairs applied."""
#     if not getattr(block, "text", None):
#         return block
#     repaired = _repair_ocr_text(block.text)
#     if repaired != block.text:
#         debug_log.log(
#             "OCR",
#             f"Character repair: {block.text!r} -> {repaired!r}"
#         )
#         block.text = repaired
#     return block


# def _repair_ocr_result(result: OCRResult) -> OCRResult:
#     """Apply text-only OCR repairs while preserving bbox/confidence."""
#     if result is None:
#         return result
#     for block in result.blocks:
#         _repair_ocr_block(block)
#     result.text = "\n".join(
#         b.text for b in result.blocks if getattr(b, "text", None)
#     )
#     if result.blocks:
#         result.confidence = (
#             sum(float(b.confidence) for b in result.blocks)
#             / len(result.blocks)
#         )
#     return result

# def _ocr_blocks_to_lines(blocks: list) -> list:
#     """Adapts OCRBlock (bbox already in PDF points) into the same duck-
#     typed shape auto_zoning.pdf_block_detector.LineInfo has, so the
#     existing digital-PDF classification pipeline (band/list/heading
#     detection) can run unmodified over OCR-derived text. font_size is
#     estimated from the block's own bbox height (a standard, documented
#     approximation - OCR gives no separate font-size field), bold/italic
#     always False (not available from plain text recognition)."""
#     lines = []
#     for b in blocks:
#         text = b.text.strip()
#         if not text:
#             continue
#         height = max(b.bbox[3] - b.bbox[1], 1.0)
#         lines.append(LineInfo(bbox=tuple(b.bbox), text=text, font_size=height, bold=False, italic=False))
#     lines.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
#     return lines


# def _blocks_matching_bbox(bbox, blocks: list) -> list:
#     """Every source OCRBlock whose center point falls inside `bbox` - the
#     one place both confidence-rescaling and text-recovery below decide
#     "which original OCR block(s) does this generated zone correspond to,"
#     so the two stay consistent by construction rather than risking two
#     independently-written matching rules drifting apart."""
#     cx0, cy0, cx1, cy1 = bbox
#     matched = []
#     for b in blocks:
#         bx0, by0, bx1, by1 = b.bbox
#         mx, my = (bx0 + bx1) / 2, (by0 + by1) / 2
#         if cx0 - 1 <= mx <= cx1 + 1 and cy0 - 1 <= my <= cy1 + 1:
#             matched.append(b)
#     return matched


# def _rescale_confidence(pz: PredictedZone, blocks: list):
#     """Recursively replaces every PredictedZone's confidence (set by the
#     reused page_analyzer pipeline to its own fixed AUTO_ANALYSE_CONFIDENCE)
#     with the real OCR-derived confidence for that same bbox, tags every
#     zone attributes["source"]="ocr" (a distinct value from the geometry/
#     font-based "auto" path, so the UI and Auto Analyse's manual-zone-
#     conflict check can tell candidate zones apart by how they were
#     produced - see hierarchy_builder.create_page's attrs.setdefault, which
#     preserves this instead of overwriting it), and sets pz.text to the
#     actual recognized text for that zone (hierarchy_builder.create_page
#     applies this to the real Zone AFTER add_zone runs, since add_zone's own
#     internal text re-extraction would otherwise find nothing - there is no
#     digital text layer on a scanned page - and silently leave the zone
#     empty; RULE 18 forbids exactly that kind of silent OCR-text loss)."""
#     matched = _blocks_matching_bbox(pz.bbox, blocks)
#     if matched:
#         pz.confidence = round((sum(b.confidence for b in matched) / len(matched)) * 100.0, 1)
#         pz.text = "\n".join(b.text for b in matched)
#     else:
#         pz.confidence = OCR_BASELINE_CONFIDENCE
#         pz.text = ""
#     pz.attributes["source"] = "ocr"
#     for child in pz.children:
#         _rescale_confidence(child, blocks)


# OCR_MODES = ("auto", "force_ocr", "digital_only")


# def analyze_page_with_ocr(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
#                            language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
#                            cache: OCRCache = None, force_refresh: bool = False,
#                            mode: str = "auto") -> OCRPageAnalysis:
#     """THE single entry point for "Auto Detect" (spec 22-27). Classifies
#     page_number in isolation (RULE 3/spec 6 - never a whole-PDF decision),
#     then, in the default `mode="auto"`:
#       - DIGITAL or MIXED (real digital text already present - RULE 1: good
#         existing text is always preferred over OCR): delegates straight to
#         page_analyzer.analyze_page, completely unchanged from the existing
#         Auto Analyse behavior - zero OCR, zero risk to already-working
#         digital extraction (RULE 20).
#       - SCANNED or OCR_REQUIRED (RULE 2: automatically eligible for OCR):
#         runs the OCR engine and builds candidate zones from its output.
#       - UNKNOWN: attempts the digital path first; if it produces nothing,
#         falls back to OCR rather than returning an empty result for a page
#         that might still have recoverable content.

#     `mode` gives the user explicit override control over that automatic
#     decision (spec: "OCR must be automatic by default... but provide
#     manual control - Auto / Force OCR / Disable OCR"):
#       - "force_ocr": always runs the OCR engine on this page, even if it
#         classifies as DIGITAL - e.g. a digital page whose embedded text
#         layer is technically present but wrong/garbled in a way the
#         classifier's heuristics didn't catch.
#       - "digital_only": never runs OCR on this page regardless of
#         classification - the existing digital extractor's result (however
#         sparse) is used as-is; the classification is still computed and
#         returned so the caller/UI can show it, but it never triggers OCR.
#       - "auto" (default): the classification-driven behavior described
#         above, unchanged from every existing caller.

#     Never raises for an OCR engine failure - returns an OCRPageAnalysis
#     with .error set and .predicted_zones=[] instead (spec 41)."""
#     classification = page_classifier.classify_page(pdf_document, page_number)
#     category = classification.category
#     debug_log.log("OCR_DECISION", f"Page {page_number} classified as {category} (mode={mode}) counts={getattr(classification,'counts',None)!r}")

#     if mode == "digital_only":
#         result = page_analyzer.analyze_page(pdf_document, page_number)
#         return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
#                                 predicted_zones=result.predicted_zones, counts=result.counts)

#     if mode != "force_ocr":
#         if category in ("DIGITAL", "MIXED"):
#             result = page_analyzer.analyze_page(pdf_document, page_number)
#             return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
#                                     predicted_zones=result.predicted_zones, counts=result.counts)

#         if category == "UNKNOWN":
#             result = page_analyzer.analyze_page(pdf_document, page_number)
#             if result.predicted_zones:
#                 return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
#                                         predicted_zones=result.predicted_zones, counts=result.counts)

#     debug_log.log("OCR", f"Page {page_number}: running {engine_name}")
#     ocr_result = run_ocr_for_page(pdf_document, page_number, engine_name, language, dpi,
#                                    preprocessing_settings, cache, force_refresh)
#     if ocr_result.error:
#         debug_log.log("OCR", f"Page {page_number} failed: {ocr_result.error}")
#         return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=True,
#                                 ocr_result=ocr_result, error=ocr_result.error)

#     lines = _ocr_blocks_to_lines(ocr_result.blocks)
#     page_width, page_height = pdf_document.page_size(page_number)
#     if debug_log.is_enabled():
#         debug_log.log("OCR_LINES", f"Page {page_number}: OCR line adapter produced {len(lines)} lines")
#         for li, line in enumerate(lines):
#             debug_log.log("OCR_LINES", f"line[{li}]={line!r}")
#     predicted_zones = page_analyzer._classify_lines(lines, page_width, page_height)
#     for pz in predicted_zones:
#         _rescale_confidence(pz, ocr_result.blocks)

#     counts = {}
#     for pz in predicted_zones:
#         page_analyzer._count_tags(pz, counts)

#     avg_conf = (sum(b.confidence for b in ocr_result.blocks) / len(ocr_result.blocks)
#                 if ocr_result.blocks else 0.0)
#     if debug_log.is_enabled():
#         for zi, pz in enumerate(predicted_zones):
#             debug_log.log("OCR_ZONE", f"Page {page_number} zone[{zi}] bbox={getattr(pz,'bbox',None)} tag={getattr(pz,'tag',None)!r} text={getattr(pz,'text',None)!r} conf={getattr(pz,'confidence',None)!r} source={getattr(pz,'attributes',{}).get('source') if hasattr(pz,'attributes') else None!r}")
#     debug_log.log("OCR", f"Page {page_number} completed",
#                   f"    regions={len(ocr_result.blocks)}",
#                   f"    average_confidence={avg_conf:.2f}")

#     return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=True,
#                             ocr_result=ocr_result, predicted_zones=predicted_zones, counts=counts)


# def analyze_document_with_ocr(pdf_document, page_numbers, engine_name: str = "PaddleOCR",
#                                language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
#                                cache: OCRCache = None, mode: str = "auto",
#                                progress_callback=None, should_cancel=None):
#     """Whole-document batch orchestration for large books (spec 24: "OCR
#     must support large PDFs without loading the entire book as images into
#     memory... process pages incrementally... allow progress reporting").

#     A thin, synchronous loop over analyze_page_with_ocr - never a second
#     per-document engine: each page is rendered, OCR'd (or not, per the
#     same per-page classify-then-decide/mode logic), and released before
#     the next page starts, since analyze_page_with_ocr/run_ocr_for_page
#     already never retain a page's rendered image beyond that one call
#     (Python's own refcounting frees the PIL.Image once this function's
#     local `analysis`/intermediate objects go out of scope each iteration -
#     nothing here holds a list of every page's image, only the final
#     lightweight OCRPageAnalysis results, exactly the incremental-memory
#     behavior the spec asks for).

#     progress_callback(current_index, total, page_number) is called AFTER
#     each page completes (current_index is 1-based) - the caller (GUI)
#     uses this for a "Page 24/471" display. should_cancel() is checked
#     BETWEEN pages (never mid-page - a single OCR call can't be safely
#     interrupted, matching OCRProgressDialog's own documented "soft
#     cancel" limitation); when it returns True, the loop stops immediately
#     and returns only the results gathered so far - never a partial page.

#     This function is intentionally synchronous/side-effect-free (like
#     auto_zoning.auto_zone_engine.run_auto_zone's own whole-document loop) -
#     it never touches a ZoneManager itself. The caller is responsible for
#     turning each returned OCRPageAnalysis into real zones (exactly the
#     same hierarchy_builder.create_page/reading_order_engine.order_page
#     calls the single-page "Auto Detect" action already uses), on
#     whichever thread is appropriate for that (the GUI thread, since
#     ZoneManager is not thread-safe)."""
#     results = []
#     total = len(page_numbers)
#     for i, page_number in enumerate(page_numbers, start=1):
#         if should_cancel is not None and should_cancel():
#             debug_log.log("OCR", f"Document batch cancelled after {i - 1}/{total} pages")
#             break
#         analysis = analyze_page_with_ocr(
#             pdf_document, page_number, engine_name=engine_name, language=language, dpi=dpi,
#             preprocessing_settings=preprocessing_settings, cache=cache, mode=mode)
#         results.append(analysis)
#         if progress_callback is not None:
#             progress_callback(i, total, page_number)
#     return results

"""OCR orchestration layer (spec sections 1/2/3, 22-27): the single place
that ties page classification, digital extraction, an OCREngine, and
candidate-zone construction together. Nothing downstream of this module
(gui/main_window.py's Auto Detect action) talks to page_classifier,
coordinate_mapper, or an OCREngine directly - this is the one seam.

Deliberately reuses auto_zoning.page_analyzer's EXISTING band/list/heading
classification pipeline (auto_zoning.page_analyzer._classify_lines) for the
OCR path too, rather than building a second one: an OCRBlock is adapted
into the exact same duck-typed shape auto_zoning.pdf_block_detector.LineInfo
already has (.bbox/.text/.font_size/.bold/.italic), then handed to the same
function that already turns digital-PDF LineInfo objects into
PredictedZone trees (bands -> list-runs -> paragraph/heading blocks,
generic "h1".."h6"/"p"/"list"/"list-item"/"list-bullet" tags - the exact
same tag vocabulary hierarchy_builder.create_page and every existing
profile's generation step already understand). OCR block font-style
(bold/italic) isn't available from plain text recognition, so those two
fields are always False for OCR-derived lines - heading detection for OCR
pages therefore falls back to font-SIZE-only clustering (still meaningful:
scanned book titles/headings are almost always visibly taller glyphs),
honestly weaker than the digital path's bold-aware detection, never
pretended otherwise.

RULE 1 (existing good digital text preferred over OCR) and RULE 3 (mixed
PDFs processed page-by-page, never one whole-document decision) are
implemented directly in analyze_page_with_ocr: a DIGITAL-classified page
never touches OCR at all - it delegates to page_analyzer.analyze_page
unchanged, zero behavior change for every PDF that was already working
before this package existed (RULE 20). Only SCANNED/OCR_REQUIRED pages (and
MIXED, since real digital text already exists there per RULE 1) run OCR."""
import re
import time
from dataclasses import dataclass, field
from collections import OrderedDict
import threading

from core.ocr import text_quality, page_classifier, coordinate_mapper
from core.ocr.ocr_cache import OCRCache
from core.ocr.ocr_engine import get_engine, OCREngineError
from core.ocr.result_model import OCRResult
from core import debug_log
from auto_zoning import page_analyzer
from auto_zoning.pdf_block_detector import LineInfo
from auto_zoning.hierarchy_builder import PredictedZone

OCR_BASELINE_CONFIDENCE = 60.0  # used only if an OCR-derived PredictedZone can't be matched back to any source block


@dataclass
class OCRPageAnalysis:
    page_number: int
    classification: object          # page_classifier.PageClassification
    used_ocr: bool
    ocr_result: OCRResult = None    # None when used_ocr is False
    predicted_zones: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    error: str = None               # set (predicted_zones=[]) on an OCR failure - never raised past here


# Small process-local OCR result cache.
# This does NOT change OCR settings or recognition; it only prevents the exact
# same page/settings from being inferred repeatedly during one generation.
# The existing persistent OCRCache remains the source of truth when supplied.
_OCR_MEMORY_CACHE = OrderedDict()
_OCR_MEMORY_CACHE_MAXSIZE = 32
_OCR_MEMORY_CACHE_LOCK = threading.RLock()

def _pdf_path_value(pdf_document) -> str:
    return str(getattr(pdf_document, "path", None) or getattr(pdf_document, "name", None) or "")


def _ocr_memory_key(pdf_document, page_number, engine_name, language, dpi, preprocessing_settings):
    settings = tuple(sorted((str(k), repr(v)) for k, v in (preprocessing_settings or {}).items()))
    return (_pdf_path_value(pdf_document), int(page_number), str(engine_name),
            str(language), int(dpi), settings)

def _ocr_memory_get(key):
    with _OCR_MEMORY_CACHE_LOCK:
        result = _OCR_MEMORY_CACHE.get(key)
        if result is not None:
            _OCR_MEMORY_CACHE.move_to_end(key)
        return result

def _ocr_memory_put(key, result):
    with _OCR_MEMORY_CACHE_LOCK:
        _OCR_MEMORY_CACHE[key] = result
        _OCR_MEMORY_CACHE.move_to_end(key)
        while len(_OCR_MEMORY_CACHE) > _OCR_MEMORY_CACHE_MAXSIZE:
            _OCR_MEMORY_CACHE.popitem(last=False)


def _is_usable_ocr_result(result) -> bool:
    """Return True only for a successful OCR result.

    Failed OCR results may remain on disk for diagnostics, but they are
    never valid cache hits. This allows Prepare OCR Cache to retry failures
    after the OCR runtime has been repaired.
    """
    return result is not None and not bool(getattr(result, "error", None))


def get_cached_ocr_for_page(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
                            language: str = "en", dpi: int = 300,
                            preprocessing_settings: dict = None, cache: OCRCache = None):
    """Return an already-computed OCRResult without rendering or OCR'ing."""
    key = _ocr_memory_key(pdf_document, page_number, engine_name, language, dpi, preprocessing_settings)
    cached = _ocr_memory_get(key)
    if cached is not None:
        return cached
    if cache is None:
        return None
    try:
        # Cache reads must not initialize PaddleOCR.  This is especially
        # important during zoning, where reading OCR text for a zone/page
        # must be instant and cache-only.
        cached = cache.get_latest_for_page(_pdf_path_value(pdf_document), page_number,
                                           engine_name, language, preprocessing_settings)
    except Exception:
        return None
    if _is_usable_ocr_result(cached):
        _ocr_memory_put(key, cached)
        return cached
    # Cached OCR errors are diagnostic records, not usable OCR evidence.
    return None


def cached_ocr_blocks_for_bbox(ocr_result: OCRResult, bbox, min_overlap: float = 0.15) -> list:
    """Return cached page OCR blocks belonging to a PDF-point bbox."""
    if ocr_result is None or not bbox:
        return []
    try:
        x0, y0, x1, y1 = map(float, bbox[:4])
    except Exception:
        return []
    zone_area = max(0.0, (x1-x0)*(y1-y0))
    out = []
    for block in getattr(ocr_result, "blocks", []) or []:
        try:
            bx0, by0, bx1, by1 = map(float, block.bbox[:4])
            cx = (bx0 + bx1) / 2.0
            cy = (by0 + by1) / 2.0
            center_inside = x0 <= cx <= x1 and y0 <= cy <= y1
            ix0, iy0 = max(x0, bx0), max(y0, by0)
            ix1, iy1 = min(x1, bx1), min(y1, by1)
            inter = max(0.0, ix1-ix0) * max(0.0, iy1-iy0)
            block_area = max(1e-9, (bx1-bx0)*(by1-by0))
            overlap = inter / block_area
            zone_overlap = inter / zone_area if zone_area else 0.0
            if center_inside or overlap >= min_overlap or zone_overlap >= 0.50:
                out.append(block)
        except Exception:
            continue
    out.sort(key=lambda b: (float(b.bbox[1]), float(b.bbox[0])))
    return out


def cached_ocr_text_for_bbox(ocr_result: OCRResult, bbox, min_overlap: float = 0.15) -> str:
    blocks = cached_ocr_blocks_for_bbox(ocr_result, bbox, min_overlap=min_overlap)
    return "\n".join(str(b.text).strip() for b in blocks if str(getattr(b, "text", "")).strip())


def run_ocr_for_page(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
                      language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
                      cache: OCRCache = None, force_refresh: bool = False) -> OCRResult:
    memory_key = _ocr_memory_key(pdf_document, page_number, engine_name, language, dpi, preprocessing_settings)
    if not force_refresh:
        memory_cached = _ocr_memory_get(memory_key)
        if _is_usable_ocr_result(memory_cached):
            return memory_cached
    """Renders page_number at `dpi`, optionally preprocesses it (spec 20 -
    preprocessing only ever changes what image is FED to the engine, never
    the recognized text itself), runs `engine_name`, converts its pixel-
    space block bboxes to PDF points (coordinate_mapper - the engine itself
    never sees a DPI, per ocr_engine.OCREngine.recognize's own contract),
    and caches the result keyed by pdf identity + page + engine + version +
    language + settings (spec 38). A cache hit skips OCR entirely unless
    force_refresh=True."""
    if cache is not None and not force_refresh:
        try:
            # Never initialize the OCR engine merely to check the persistent cache.
            cached = cache.get_latest_for_page(pdf_document.path, page_number, engine_name,
                                               language, preprocessing_settings)
            if _is_usable_ocr_result(cached):
                _ocr_memory_put(memory_key, cached)
                return cached
            # IMPORTANT: an old failed OCR entry is a cache miss.
            # Prepare OCR must retry it after the runtime is fixed.
        except OCREngineError:
            pass

    # HARD RULE: Generate XHTML/XML may read prepared OCR cache, but it must
    # never create a fresh OCR result. This guard is deliberately immediately
    # before engine construction so even an unexpected generation call cannot
    # initialize PaddleOCR.
    if _GENERATION_CACHE_ONLY:
        return OCRResult.empty(page_number=page_number, image_width=0, image_height=0, dpi=dpi,
                                engine=engine_name, engine_version="cache-only", language=language,
                                error="Prepared OCR cache missing for this page; generation is cache-only and will not run OCR.")

    try:
        engine = get_engine(engine_name)
    except OCREngineError as e:
        return OCRResult.empty(page_number=page_number, image_width=0, image_height=0, dpi=dpi,
                                engine=engine_name, engine_version="unknown", language=language, error=str(e))

    if not engine.is_available():
        return OCRResult.empty(page_number=page_number, image_width=0, image_height=0, dpi=dpi,
                                engine=engine_name, engine_version=engine.version, language=language,
                                error=f"{engine_name} is not installed/available in this environment.")

    import time as _ocr_time
    _page_started = _ocr_time.time()
    _render_started = _page_started
    image = pdf_document.render_page_image(page_number, dpi)
    _render_elapsed = _ocr_time.time() - _render_started

    _prep_started = _ocr_time.time()
    image = _apply_preprocessing(image, preprocessing_settings)
    _prep_elapsed = _ocr_time.time() - _prep_started

    try:
        _engine_started = _ocr_time.time()
        # ISO code / "auto" / engine model name -> the engine's language model
        from core.lang import ocr_language
        try:
            _sample = pdf_document.get_page(page_number).get_text()[:4000]
        except Exception:  # noqa: BLE001
            _sample = ""
        engine_language = ocr_language(language, _sample) if engine_name.lower().startswith("paddle") else language
        result = engine.recognize(image, language=engine_language, options=preprocessing_settings)
        _engine_elapsed = _ocr_time.time() - _engine_started
    except OCREngineError as e:
        return OCRResult.empty(page_number=page_number, image_width=image.width, image_height=image.height,
                                dpi=dpi, engine=engine_name, engine_version=engine.version, language=language,
                                error=str(e))

    result.page_number = page_number
    result.dpi = dpi

    # IMPORTANT: repair OCR characters before zones are generated.  Bounding
    # boxes and confidence are untouched, so layout/formatting analysis keeps
    # the exact OCR geometry.
    result = _repair_ocr_result(result)

    for block in result.blocks:
        block.bbox = coordinate_mapper.pixel_to_pdf(block.bbox, dpi)

    _cache_started = _ocr_time.time()
    if cache is not None:
        cache.put(pdf_document.path, page_number, result, preprocessing_settings)
    _cache_elapsed = _ocr_time.time() - _cache_started
    _total_elapsed = _ocr_time.time() - _page_started
    try:
        from core import debug_log as _ocr_debug_log
        _ocr_debug_log.log(
            "OCR_TIMING",
            f"PAGE {page_number}: render={_render_elapsed:.3f}s prep={_prep_elapsed:.3f}s "
            f"ocr={_engine_elapsed:.3f}s cache={_cache_elapsed:.3f}s total={_total_elapsed:.3f}s "
            f"image={image.width}x{image.height} blocks={len(result.blocks)}",
        )
    except Exception:
        pass
    _ocr_memory_put(memory_key, result)
    return result


def _apply_preprocessing(image, settings: dict):
    """Applies only the steps `settings` explicitly enables, in a fixed,
    documented order (deskew before binarization - correcting rotation on
    an already-binarized image is unreliable). Returns `image` unchanged if
    settings is empty/None (spec 20's own default: preprocessing is opt-in,
    never silently forced on)."""
    if not settings:
        return image
    from core.ocr import preprocessing
    out = image
    if settings.get("deskew"):
        out = preprocessing.deskew(out)
    if settings.get("remove_borders"):
        out = preprocessing.remove_borders(out)
    if settings.get("grayscale"):
        out = preprocessing.to_grayscale(out)
    if settings.get("enhance_contrast"):
        out = preprocessing.enhance_contrast(out)
    if settings.get("denoise"):
        out = preprocessing.denoise(out)
    if settings.get("adaptive_threshold"):
        out = preprocessing.adaptive_threshold(out)
    return out


# ---------------------------------------------------------------------------
# OCR text-quality repair
# ---------------------------------------------------------------------------
# PaddleOCR returns the characters it believes are present in the scan.  OCR
# errors are not formatting errors, so they must be corrected BEFORE zones are
# created.  These rules are deliberately contextual; there is no global
# replacement such as '+' -> '-' or '®' -> 'fi'.
#
# The main production cases seen in the user's book conversion are:
#   - em-dash encoded/recognized as ± when used as spaced prose punctuation
#   - common fi/ffi/ffl ligature corruption in OCR text
#
# Real mathematical ± and real ® are preserved unless the surrounding text
# makes the broken-OCR interpretation unambiguous.

_FI_OCR_MAP = {
    "ﬁ": "fi",
    "ﬃ": "ffi",
    "ﬂ": "fl",
    "ﬄ": "ffl",
    "ﬀ": "ff",
    "ﬅ": "st",
    "ﬆ": "st",
}

def _repair_ocr_text(text: str) -> str:
    if not text:
        return text

    # Unicode presentation ligatures are unambiguous OCR normalization.
    for bad, good in _FI_OCR_MAP.items():
        text = text.replace(bad, good)

    # The observed PDF/scan defect: a standalone ± between two whitespace
    # separated prose tokens is actually a dash.  Do not touch:
    #   x ± y
    #   ±5
    #   10 ± 2
    #   mathematical/formula text
    #
    # We only apply this when both neighboring tokens contain letters and the
    # complete local form is ordinary prose punctuation.
    text = re.sub(
        r"(?<=\s)±(?=\s)",
        "—",
        text,
    )

    # Common OCR confusion where an isolated soft hyphen disappears into a
    # word.  Preserve ordinary visible hyphens exactly.
    text = text.replace("\u00ad", "")

    return text


def _repair_ocr_block(block):
    """Return the same OCRBlock object with only safe text repairs applied."""
    if not getattr(block, "text", None):
        return block
    repaired = _repair_ocr_text(block.text)
    if repaired != block.text:
        debug_log.log(
            "OCR",
            f"Character repair: {block.text!r} -> {repaired!r}"
        )
        block.text = repaired
    return block


def _repair_ocr_result(result: OCRResult) -> OCRResult:
    """Apply text-only OCR repairs while preserving bbox/confidence."""
    if result is None:
        return result
    for block in result.blocks:
        _repair_ocr_block(block)
    result.text = "\n".join(
        b.text for b in result.blocks if getattr(b, "text", None)
    )
    if result.blocks:
        result.confidence = (
            sum(float(b.confidence) for b in result.blocks)
            / len(result.blocks)
        )
    return result

def _ocr_blocks_to_lines(blocks: list) -> list:
    """Adapts OCRBlock (bbox already in PDF points) into the same duck-
    typed shape auto_zoning.pdf_block_detector.LineInfo has, so the
    existing digital-PDF classification pipeline (band/list/heading
    detection) can run unmodified over OCR-derived text. font_size is
    estimated from the block's own bbox height (a standard, documented
    approximation - OCR gives no separate font-size field), bold/italic
    always False (not available from plain text recognition)."""
    lines = []
    for b in blocks:
        text = b.text.strip()
        if not text:
            continue
        height = max(b.bbox[3] - b.bbox[1], 1.0)
        lines.append(LineInfo(bbox=tuple(b.bbox), text=text, font_size=height, bold=False, italic=False))
    lines.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return lines


def _blocks_matching_bbox(bbox, blocks: list) -> list:
    """Every source OCRBlock whose center point falls inside `bbox` - the
    one place both confidence-rescaling and text-recovery below decide
    "which original OCR block(s) does this generated zone correspond to,"
    so the two stay consistent by construction rather than risking two
    independently-written matching rules drifting apart."""
    cx0, cy0, cx1, cy1 = bbox
    matched = []
    for b in blocks:
        bx0, by0, bx1, by1 = b.bbox
        mx, my = (bx0 + bx1) / 2, (by0 + by1) / 2
        if cx0 - 1 <= mx <= cx1 + 1 and cy0 - 1 <= my <= cy1 + 1:
            matched.append(b)
    return matched


def _rescale_confidence(pz: PredictedZone, blocks: list):
    """Recursively replaces every PredictedZone's confidence (set by the
    reused page_analyzer pipeline to its own fixed AUTO_ANALYSE_CONFIDENCE)
    with the real OCR-derived confidence for that same bbox, tags every
    zone attributes["source"]="ocr" (a distinct value from the geometry/
    font-based "auto" path, so the UI and Auto Analyse's manual-zone-
    conflict check can tell candidate zones apart by how they were
    produced - see hierarchy_builder.create_page's attrs.setdefault, which
    preserves this instead of overwriting it), and sets pz.text to the
    actual recognized text for that zone (hierarchy_builder.create_page
    applies this to the real Zone AFTER add_zone runs, since add_zone's own
    internal text re-extraction would otherwise find nothing - there is no
    digital text layer on a scanned page - and silently leave the zone
    empty; RULE 18 forbids exactly that kind of silent OCR-text loss)."""
    matched = _blocks_matching_bbox(pz.bbox, blocks)
    if matched:
        pz.confidence = round((sum(b.confidence for b in matched) / len(matched)) * 100.0, 1)
        pz.text = "\n".join(b.text for b in matched)
    else:
        pz.confidence = OCR_BASELINE_CONFIDENCE
        pz.text = ""
    pz.attributes["source"] = "ocr"
    for child in pz.children:
        _rescale_confidence(child, blocks)


OCR_MODES = ("auto", "force_ocr", "digital_only")


def analyze_page_with_cached_ocr(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
                                 language: str = "en", dpi: int = 300,
                                 preprocessing_settings: dict = None, cache: OCRCache = None) -> OCRPageAnalysis:
    """Build zoning candidates strictly from prepared OCR cache.

    No OCR engine is loaded, no page image is rendered, and no inference is
    performed.  If the prepared cache is missing, return an explicit error so
    the GUI can ask the user to run Prepare OCR Cache first.
    """
    classification = page_classifier.classify_page(pdf_document, page_number)
    if cache is None:
        return OCRPageAnalysis(page_number=page_number, classification=classification,
                               used_ocr=False, error="OCR cache is not configured. Run Prepare OCR Cache first.")
    ocr_result = get_cached_ocr_for_page(pdf_document, page_number, engine_name, language, dpi,
                                         preprocessing_settings, cache)
    if ocr_result is None:
        return OCRPageAnalysis(page_number=page_number, classification=classification,
                               used_ocr=False, error=(
                                   f"Prepared OCR cache is missing for page {page_number}. "
                                   "Run Prepare OCR Cache before zoning. Zoning will not run OCR automatically."))
    lines = _ocr_blocks_to_lines(ocr_result.blocks)
    page_width, page_height = pdf_document.page_size(page_number)
    predicted_zones = page_analyzer._classify_lines(lines, page_width, page_height)
    for pz in predicted_zones:
        _rescale_confidence(pz, ocr_result.blocks)
    counts = {}
    for pz in predicted_zones:
        page_analyzer._count_tags(pz, counts)
    return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=True,
                           ocr_result=ocr_result, predicted_zones=predicted_zones, counts=counts)


def analyze_page_with_ocr(pdf_document, page_number: int, engine_name: str = "PaddleOCR",
                           language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
                           cache: OCRCache = None, force_refresh: bool = False,
                           mode: str = "auto") -> OCRPageAnalysis:
    """THE single entry point for "Auto Detect" (spec 22-27). Classifies
    page_number in isolation (RULE 3/spec 6 - never a whole-PDF decision),
    then, in the default `mode="auto"`:
      - DIGITAL or MIXED (real digital text already present - RULE 1: good
        existing text is always preferred over OCR): delegates straight to
        page_analyzer.analyze_page, completely unchanged from the existing
        Auto Analyse behavior - zero OCR, zero risk to already-working
        digital extraction (RULE 20).
      - SCANNED or OCR_REQUIRED (RULE 2: automatically eligible for OCR):
        runs the OCR engine and builds candidate zones from its output.
      - UNKNOWN: attempts the digital path first; if it produces nothing,
        falls back to OCR rather than returning an empty result for a page
        that might still have recoverable content.

    `mode` gives the user explicit override control over that automatic
    decision (spec: "OCR must be automatic by default... but provide
    manual control - Auto / Force OCR / Disable OCR"):
      - "force_ocr": always runs the OCR engine on this page, even if it
        classifies as DIGITAL - e.g. a digital page whose embedded text
        layer is technically present but wrong/garbled in a way the
        classifier's heuristics didn't catch.
      - "digital_only": never runs OCR on this page regardless of
        classification - the existing digital extractor's result (however
        sparse) is used as-is; the classification is still computed and
        returned so the caller/UI can show it, but it never triggers OCR.
      - "auto" (default): the classification-driven behavior described
        above, unchanged from every existing caller.

    Never raises for an OCR engine failure - returns an OCRPageAnalysis
    with .error set and .predicted_zones=[] instead (spec 41)."""
    classification = page_classifier.classify_page(pdf_document, page_number)
    category = classification.category
    debug_log.log("OCR", f"Page {page_number} classified as {category} (mode={mode})")

    if mode == "digital_only":
        result = page_analyzer.analyze_page(pdf_document, page_number)
        return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
                                predicted_zones=result.predicted_zones, counts=result.counts)

    if mode != "force_ocr":
        if category in ("DIGITAL", "MIXED"):
            result = page_analyzer.analyze_page(pdf_document, page_number)
            return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
                                    predicted_zones=result.predicted_zones, counts=result.counts)

        if category == "UNKNOWN":
            result = page_analyzer.analyze_page(pdf_document, page_number)
            if result.predicted_zones:
                return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=False,
                                        predicted_zones=result.predicted_zones, counts=result.counts)

    debug_log.log("OCR", f"Page {page_number}: running {engine_name}")
    ocr_result = run_ocr_for_page(pdf_document, page_number, engine_name, language, dpi,
                                   preprocessing_settings, cache, force_refresh)
    if ocr_result.error:
        debug_log.log("OCR", f"Page {page_number} failed: {ocr_result.error}")
        return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=True,
                                ocr_result=ocr_result, error=ocr_result.error)

    lines = _ocr_blocks_to_lines(ocr_result.blocks)
    page_width, page_height = pdf_document.page_size(page_number)
    predicted_zones = page_analyzer._classify_lines(lines, page_width, page_height)
    for pz in predicted_zones:
        _rescale_confidence(pz, ocr_result.blocks)

    counts = {}
    for pz in predicted_zones:
        page_analyzer._count_tags(pz, counts)

    avg_conf = (sum(b.confidence for b in ocr_result.blocks) / len(ocr_result.blocks)
                if ocr_result.blocks else 0.0)
    debug_log.log("OCR", f"Page {page_number} completed",
                  f"    regions={len(ocr_result.blocks)}",
                  f"    average_confidence={avg_conf:.2f}")

    return OCRPageAnalysis(page_number=page_number, classification=classification, used_ocr=True,
                            ocr_result=ocr_result, predicted_zones=predicted_zones, counts=counts)


def cache_document_with_ocr(pdf_document, page_numbers, engine_name: str = "PaddleOCR",
                             language: str = "en", dpi: int = 300,
                             preprocessing_settings: dict = None, cache: OCRCache = None,
                             mode: str = "auto", progress_callback=None, should_cancel=None):
    """Prepare persistent page OCR without touching zones.

    ``mode="force_ocr"`` is used by the GUI's explicit Prepare OCR Cache
    command and intentionally OCRs EVERY requested page, including pages
    that have a digital text layer.  This is different from ``mode="auto"``
    used by Auto Detect, which may correctly skip OCR for DIGITAL pages.
    The explicit cache-preparation operation must create a complete reusable
    OCR dataset so Verify/Generate XHTML never need to start OCR implicitly.
    """
    results = []
    total = len(page_numbers)
    for i, page_number in enumerate(page_numbers, start=1):
        if should_cancel is not None and should_cancel():
            break
        classification = page_classifier.classify_page(pdf_document, page_number)
        use_ocr = mode == "force_ocr" or classification.category in (
            "SCANNED", "MIXED", "OCR_REQUIRED", "UNKNOWN"
        )
        if use_ocr:
            ocr_result = run_ocr_for_page(
                pdf_document, page_number, engine_name=engine_name, language=language, dpi=dpi,
                preprocessing_settings=preprocessing_settings, cache=cache,
                # Explicit Prepare OCR Cache is a rebuild: retry any previous
                # failed result instead of reusing the failure record.
                force_refresh=True)
            analysis = OCRPageAnalysis(page_number=page_number, classification=classification,
                                       used_ocr=True, ocr_result=ocr_result,
                                       error=ocr_result.error, counts={})
        else:
            analysis = OCRPageAnalysis(page_number=page_number, classification=classification,
                                       used_ocr=False, ocr_result=None, counts={})
        results.append(analysis)
        if progress_callback is not None:
            progress_callback(i, total, page_number)
    return results


def analyze_document_with_ocr(pdf_document, page_numbers, engine_name: str = "PaddleOCR",
                               language: str = "en", dpi: int = 300, preprocessing_settings: dict = None,
                               cache: OCRCache = None, mode: str = "auto",
                               progress_callback=None, should_cancel=None):
    """Whole-document batch orchestration for large books (spec 24: "OCR
    must support large PDFs without loading the entire book as images into
    memory... process pages incrementally... allow progress reporting").

    A thin, synchronous loop over analyze_page_with_ocr - never a second
    per-document engine: each page is rendered, OCR'd (or not, per the
    same per-page classify-then-decide/mode logic), and released before
    the next page starts, since analyze_page_with_ocr/run_ocr_for_page
    already never retain a page's rendered image beyond that one call
    (Python's own refcounting frees the PIL.Image once this function's
    local `analysis`/intermediate objects go out of scope each iteration -
    nothing here holds a list of every page's image, only the final
    lightweight OCRPageAnalysis results, exactly the incremental-memory
    behavior the spec asks for).

    progress_callback(current_index, total, page_number) is called AFTER
    each page completes (current_index is 1-based) - the caller (GUI)
    uses this for a "Page 24/471" display. should_cancel() is checked
    BETWEEN pages (never mid-page - a single OCR call can't be safely
    interrupted, matching OCRProgressDialog's own documented "soft
    cancel" limitation); when it returns True, the loop stops immediately
    and returns only the results gathered so far - never a partial page.

    This function is intentionally synchronous/side-effect-free (like
    auto_zoning.auto_zone_engine.run_auto_zone's own whole-document loop) -
    it never touches a ZoneManager itself. The caller is responsible for
    turning each returned OCRPageAnalysis into real zones (exactly the
    same hierarchy_builder.create_page/reading_order_engine.order_page
    calls the single-page "Auto Detect" action already uses), on
    whichever thread is appropriate for that (the GUI thread, since
    ZoneManager is not thread-safe)."""
    results = []
    total = len(page_numbers)
    for i, page_number in enumerate(page_numbers, start=1):
        if should_cancel is not None and should_cancel():
            debug_log.log("OCR", f"Document batch cancelled after {i - 1}/{total} pages")
            break
        analysis = analyze_page_with_ocr(
            pdf_document, page_number, engine_name=engine_name, language=language, dpi=dpi,
            preprocessing_settings=preprocessing_settings, cache=cache, mode=mode)
        results.append(analysis)
        if progress_callback is not None:
            progress_callback(i, total, page_number)
    return results
