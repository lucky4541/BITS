"""Per-PDF isolated output layout for EPUB/CUPEPUB generation (spec:
"CUPEPUB - OUTPUT FOLDER + FILE NAMING STANDARD"). Deliberately scoped to
the EPUB/CUPEPUB generation path only (gui/main_window.py's
generate_xhtml) - the XML/BITS profile's own generate_xml keeps its
existing, separate, already-working output/{prefix}.xml + top-level
assets/ layout completely unchanged; nothing here is imported or used by
core/xml_generator.py.

Client naming convention: "SeqNum_lastfiveISBN_componentseq", e.g.
"06_918AR_ch1" - the FULL string ("06_918AR_ch1") remains the "prefix"
used everywhere it already was (the .xhtml filename, element ids - see
core/epub_xml_generator.py's assign_missing_ids, completely unchanged by
this module) EXCEPT the output folder name, which is now the user's own
project-level output folder name (one shared folder per PDF, entered once
at Open-PDF time), never derived from any single component's own prefix -
see ComponentOutputManager's own docstring below. Only IMAGE filenames use
a further-derived SHORT component code ("ch1"), per the client's own image
examples (ch1-fig-01.png, not 06_918AR_ch1-fig-01.png) - see
derive_image_prefix."""
import os
import re
from pathlib import Path

_COMPONENT_CODE_RE = re.compile(r"^\d+_[^_]+_(.+)$")


def derive_id_prefix(prefix: str) -> str:
    """"23_980AR_bm3" -> "bm3", "05_466AR_fm6" -> "fm6", "document" ->
    "document" (unchanged - no underscore at all to split on). This is the
    ELEMENT-ID prefix (spec: "ZONETOOL - MASTER..." sections 2-4/41-43:
    "Filename and ID prefix are TWO DIFFERENT things" - "For element IDs
    only, derive the prefix from the portion AFTER THE LAST underscore").

    Deliberately a SEPARATE, more general rule than derive_image_prefix's
    own 3-segment "NN_ISBN5_component" regex below (image filenames keep
    their own, narrower, pre-existing convention, unrelated to this) -
    this one applies to ANY string with at least one underscore, splitting
    once on the LAST occurrence only (rsplit maxsplit=1), matching
    gui/main_window.py.open_pdf's own identical derivation for the exact
    same rule (kept here too so every OTHER call site - Generate XML,
    Generate XHTML, Client XHTML - can share the ONE function instead of
    re-deriving it inline)."""
    return (prefix or "document").rsplit("_", 1)[-1]


def derive_image_prefix(prefix: str) -> str:
    """"06_918AR_ch1" -> "ch1", "01_918AR_cv" -> "cv", "12_918AR_bm1" ->
    "bm1" - strips the client convention's leading "SeqNum_ISBN5_" pair.
    Falls back to the full prefix unchanged when it doesn't match that
    convention (a user-typed prefix with no leading "NN_XXXXX_" pattern) -
    there is no reliable way to guess a short component code from an
    arbitrary string, so the safe, non-crashing default is to use the
    whole thing, exactly matching this module's pre-existing behavior for
    every prefix that isn't in the client's own naming convention."""
    m = _COMPONENT_CODE_RE.match(prefix or "")
    return m.group(1) if m else (prefix or "document")


class ComponentOutputManager:
    """Single source of truth for one PDF PROJECT's EPUB/CUPEPUB output
    layout - the "ComponentOutputManager" the spec explicitly asks for.
    Constructed fresh at Generate-XHTML time from the project's current
    prefix (never persisted, never dependent on a prior run's paths - spec
    "PROJECT REOPEN": recalculated from the project's own identity every
    time), so reopening a project and regenerating always recomputes the
    same deterministic layout from its own settings["prefix"], with no
    stale/temporary path ever surviving across runs.

    output_root IS the one shared folder for the WHOLE PDF project (named
    by the user at Open-PDF time - see gui/main_window.py's
    OutputFolderNameDialog/open_pdf), never a per-component subfolder of
    some larger root - every component (each frontmatter/chapter section
    generated separately, each its own `prefix`) writes directly into this
    SAME directory and SAME images/ subfolder:

    {output_root}/                      <- self.component_dir (shared across every component)
    ├── {prefix}.xhtml                  <- self.xhtml_path (per component)
    ├── {prefix}.txt                    <- self.txt_path (per component, only if an OCR/debug dump is ever written)
    └── images/                         <- self.images_dir (shared across every component)
        └── {image_prefix}-{token}-{NN}.{ext}

    A real, confirmed bug this fixes: `component_dir` used to be
    `output_root / prefix`, so EVERY component got its OWN top-level
    folder and its OWN images/ subfolder ("Book_001/01_001AR_fm1/",
    "Book_001/02_001AR_fm2/", ...) instead of one shared "Book_001/"
    folder for the whole PDF. `prefix` still drives only the FILENAMES
    (unchanged naming convention) - it no longer drives the folder path at
    all. `ensure_dirs()`'s `exist_ok=True` already makes repeated calls
    from different components into this same shared folder safe."""

    def __init__(self, output_root, prefix: str):
        self.prefix = prefix
        self.image_prefix = derive_image_prefix(prefix)
        self.component_dir = Path(output_root)
        self.images_dir = self.component_dir / "images"
        self.xhtml_path = self.component_dir / f"{prefix}.xhtml"
        self.txt_path = self.component_dir / f"{prefix}.txt"

    def ensure_dirs(self):
        """Creates component_dir and images_dir (parents=True covers both
        in one call) - idempotent, safe to call on every Generate click."""
        os.makedirs(self.images_dir, exist_ok=True)

    def image_src(self, filename: str) -> str:
        """The exact <img src="..."> value for a filename AssetManager
        already returned (a bare basename) - always forward-slashed
        (spec: "All EPUB paths must use forward slashes"), always relative
        to the component's own XHTML (images/ is XHTML's own sibling
        directory), never an absolute filesystem path."""
        return f"images/{filename}"
