"""High-resolution PDF page-image cache for scanned-PDF formatting
analysis (spec: "HIGH-RESOLUTION PDF PAGE CACHE / THUMB SYSTEM FOR
ACCURATE SCANNED-PDF ANALYSIS").

core/ocr/style_detector.py (added the session before this one) already
correctly detects italic/bold/sup/sub from a scanned zone's own cropped
pixels - it just re-rendered/cropped directly from the live PDF via
page.get_pixmap(clip=...) on every single call, with no persistent cache.
This module adds that cache underneath it, mirroring the client's own
"<PDF filename>_Thumb" workflow: one high-resolution PNG per page, cached
in a "<PDF_BASENAME>_Thumb" folder BESIDE the source PDF (a genuinely new
placement pattern for this codebase - every other cache/data folder here
lives under core.resource_path.writable_root(), the app's own central
data root, keyed by core/ocr/ocr_cache.py's pdf_identity() - followed
here instead per the user's own explicit, repeated instruction, since
this specific workflow is meant to mirror an external client convention,
not this app's own internal one).

Cache identity/validation deliberately mirrors core/ocr/ocr_cache.py's
own philosophy (PDF absolute path + size + mtime, never a full content
hash) - never touches the zoning JSON at all; every piece of cache
metadata lives in a small sidecar file INSIDE the Thumb folder itself.

Never the authoritative source for zone geometry (that's always the
saved zone.bbox/zone.page, read here only to know WHERE to crop) and
never used for final EPUB/figure image extraction (core/image_extractor.py
has its own separate, unrelated, unaffected pipeline for that) - this is
an analysis-only cache for core/ocr/style_detector.py."""
import json
import os
from collections import OrderedDict

from PIL import Image

# The one, explicit, already-established analysis resolution - was
# already the bare-literal default core/ocr/style_detector.py's own
# _crop_binary used, and already matches core/ocr/ocr_service.py's own
# run_ocr_for_page default - named here as the single source of truth
# instead of a value repeated as a literal in two places.
ANALYSIS_DPI = 300

_CACHE_VERSION = 1
_META_FILENAME = "cache_meta.json"

# In-memory LRU of already-loaded/rendered full-page images, keyed by
# (pdf_path, page_number, dpi) - mirrors core/text_extractor.py's own
# _rawdict_cache/_RAWDICT_CACHE_MAXSIZE pattern exactly (same rationale:
# many zones on one page, and repeated generation runs, would otherwise
# re-read/re-decode the same cached PNG over and over within one run).
_IMAGE_CACHE_MAXSIZE = 8
_image_cache: "OrderedDict" = OrderedDict()
# Per-PDF validation state for this process run, keyed by pdf_path:
# {"thumb_dir": ..., "trust_disk": bool, "confirmed_pages": set()}.
# "trust_disk" is True when cache_meta.json still matches the PDF's
# current size/mtime/page_count/dpi (spec: reuse an existing, valid
# cache) - every already-rendered page on disk is trusted as-is. When it
# does NOT match (the PDF changed, or this is the very first time this
# process has seen this path), NO existing PNG is trusted merely because
# a matching filename happens to be sitting on disk; only pages actually
# (re-)rendered during THIS invalidated state are trusted (added to
# confirmed_pages as each is individually re-requested) - this is what
# makes invalidation lazy/per-page (spec: "only render pages that are
# missing or invalid") rather than a bulk delete-and-rerender-everything.
_pdf_state: dict = {}


def clear_cache():
    """Clears the IN-MEMORY caches only (mirrors core.text_extractor.
    clear_cache's own scope/name) - never touches the on-disk <PDF>_Thumb
    folder itself, which is meant to persist across runs/sessions."""
    _image_cache.clear()
    _pdf_state.clear()


def thumb_dir_for_pdf(pdf_path: str) -> str:
    """<PDF_BASENAME>_Thumb, beside the PDF - e.g. "09_249AR_pt1.pdf" ->
    ".../09_249AR_pt1_Thumb" (spec section 2's own exact naming/placement
    example)."""
    directory = os.path.dirname(os.path.abspath(pdf_path))
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(directory, f"{stem}_Thumb")


def _page_png_path(thumb_dir: str, page_number: int, dpi: int) -> str:
    # dpi is part of the filename (not just the folder-level cache_meta.json
    # fingerprint) - confirmed directly this matters: this app's own real
    # generation pipeline only ever renders at one fixed ANALYSIS_DPI, so
    # this never mattered in practice, but a SECOND, different dpi request
    # for the same already-cached page within one process (or across
    # processes before cache_meta.json's own dpi field is next revalidated)
    # would otherwise silently load and reuse the WRONG resolution's PNG,
    # violating "same PDF/page/DPI/crop must produce equivalent formatting
    # measurements" (spec: "IMAGE CACHE").
    return os.path.join(thumb_dir, f"page_{page_number:03d}_{dpi}dpi.png")


def _current_pdf_fingerprint(pdf_path: str, page_count: int) -> dict:
    # Deliberately independent of dpi - dpi is a PER-CALL rendering
    # choice, not a property of the PDF FILE on disk, so it has no
    # business ever making an otherwise-valid, unchanged PDF's cache look
    # stale. (It was included here originally, which is exactly what let
    # a second, different-dpi request for the same PDF silently reuse the
    # WRONG resolution's already-cached PNG - see _page_png_path's own
    # comment. dpi-specific identity now lives entirely in the PNG
    # filename and in confirmed_pages' own (page_number, dpi) keys below.)
    stat = os.stat(pdf_path)
    return {
        "cache_version": _CACHE_VERSION,
        "pdf_size": stat.st_size,
        "pdf_mtime": stat.st_mtime,
        "page_count": page_count,
    }


def _ensure_pdf_state(pdf_path: str, page_count: int) -> dict:
    """Creates <PDF_BASENAME>_Thumb beside the PDF if missing (spec
    section 2) and returns this run's cached validation state for
    `pdf_path` - computed once per process per path (mirrors
    core.text_extractor._get_rawdict's own one-key-per-page-per-process
    caching rationale), never re-stat'ing the PDF on every single zone.

    If the PDF's own identity (size/mtime/page count) still matches what
    cache_meta.json last recorded, the ENTIRE existing on-disk cache is
    trusted (spec section 3: never delete/recreate an existing, valid
    cache). If it does not match (spec section 17: cache validity - the
    PDF changed since this cache was written, or this is the first time
    this process has seen this file), the metadata is rewritten to the
    current fingerprint but NOTHING already on disk is trusted yet - each
    (page, dpi) is only trusted again once it has actually been
    (re-)rendered under the new fingerprint, which get_page_image does
    lazily, one requested page at a time, never a bulk delete."""
    if pdf_path in _pdf_state:
        return _pdf_state[pdf_path]
    thumb_dir = thumb_dir_for_pdf(pdf_path)
    os.makedirs(thumb_dir, exist_ok=True)
    meta_path = os.path.join(thumb_dir, _META_FILENAME)
    current = _current_pdf_fingerprint(pdf_path, page_count)
    existing = None
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            existing = None
    trust_disk = existing == current
    if not trust_disk:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(current, f)
    state = {"thumb_dir": thumb_dir, "trust_disk": trust_disk, "confirmed_pages": set()}
    _pdf_state[pdf_path] = state
    return state


def get_page_image(page, dpi: int = ANALYSIS_DPI) -> Image.Image:
    """The one entry point core/ocr/style_detector.py calls - returns a
    full-page PIL.Image (grayscale-friendly RGB, matching what page.
    get_pixmap already produced before this cache existed) rendered at
    `dpi`, reused across every zone/call for the same page instead of
    re-rendering from the live PDF each time.

    Resolves the owning PDF's own path/page-number the exact same way
    core.text_extractor._get_rawdict already does (page.parent.name /
    page.number) - no new parameter threaded through any of this app's
    ~31 existing extract_zone_formatted_text* call sites. Falls back to a
    pure in-memory render (today's exact pre-cache behavior, no disk I/O)
    for an anonymous/path-less document or an unwritable PDF directory -
    never a crash, never blocks analysis on cache-folder availability."""
    parent = getattr(page, "parent", None)
    pdf_path = getattr(parent, "name", None) or None
    page_number = (getattr(page, "number", None) or 0) + 1
    zoom = dpi / 72.0

    if not pdf_path or not os.path.isfile(pdf_path):
        return _render_in_memory(page, zoom)

    cache_key = (pdf_path, page_number, dpi)
    cached = _image_cache.get(cache_key)
    if cached is not None:
        _image_cache.move_to_end(cache_key)
        return cached

    try:
        page_count = parent.page_count
        state = _ensure_pdf_state(pdf_path, page_count)
        thumb_dir = state["thumb_dir"]
        png_path = _page_png_path(thumb_dir, page_number, dpi)
        page_key = (page_number, dpi)
        use_existing = os.path.isfile(png_path) and (
            state["trust_disk"] or page_key in state["confirmed_pages"]
        )
        if use_existing:
            img = Image.open(png_path)
            img.load()  # force-read now - the file handle must not outlive this function
        else:
            img = _render_in_memory(page, zoom)
            img.save(png_path)
            state["confirmed_pages"].add(page_key)
    except OSError:
        # An unwritable/inaccessible PDF directory (spec: "never a crash,
        # never blocks analysis") - degrade to the pre-cache behavior.
        return _render_in_memory(page, zoom)

    _image_cache[cache_key] = img
    if len(_image_cache) > _IMAGE_CACHE_MAXSIZE:
        _image_cache.popitem(last=False)
    return img


def _render_in_memory(page, zoom: float) -> Image.Image:
    import fitz
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    mode = "RGB" if pix.n < 4 else "RGBA"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    if mode == "RGBA":
        img = img.convert("RGB")
    return img
