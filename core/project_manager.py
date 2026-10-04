"""Save/load zoning projects as JSON. Restores all zones + counters + settings."""
import json
import os

from core import autosave_service
from core.zone_manager import Zone, ZoneManager

SCHEMA_VERSION = 1

DEFAULT_SETTINGS = {
    "prefix": "document",
    "jpeg_quality": 95,
    "root_tag": "book",
    "tag_colors": {},
    "image_dpi": 200,           # figure/equation crop DPI - independent of viewer zoom
    "viewer_dpi": 200,          # PDF-viewer rasterization quality - independent of zoom AND image_dpi
    "viewer_zoom_mode": "fit_width",  # "manual" | "fit_width" | "fit_page"
    "remove_image_background": False,
    "debug_logging": False,
    "auto_zone_thresholds": {"high": 90, "medium": 75},   # confidence-bucket cutoffs for Auto Zone
    "profile": "XML",           # "XML" | "EPUB" - see core/profile_manager.py
    "mapping_xml_path": "",     # EPUB only; "" = use the active profile's own default path
    "epub_component_type": "",  # EPUB only; "" = use the active profile's default_component_type
    # Zone canvas display toggles (gui/pdf_viewer.py._draw_zone) - purely
    # visual, never affect zone data/selection/generation. Defaults ON so
    # existing projects render identically to before these existed.
    "show_zone_borders": True,
    "show_tag_labels": True,
    "show_reading_order": True,
    # Index Primary/Secondary/Tertiary retag shortcuts (gui/main_window.py
    # App._apply_index_tag_shortcuts) - Tkinter bind sequence syntax,
    # user-editable in Settings. Ctrl+1/2/3/0 default per spec: confirmed
    # unused by any existing binding (core CUP shortcuts use Alt+letter,
    # never a plain Ctrl+digit) and safe from ordinary typing in a text
    # field. A project saved under the PREVIOUS Ctrl+Alt+1/2/3 default
    # keeps its own saved value on load (settings are only ever
    # setdefault'd, never overwritten) - only brand-new projects/settings
    # pick up this new default.
    "index_shortcut_primary": "<Control-Key-1>",
    "index_shortcut_secondary": "<Control-Key-2>",
    "index_shortcut_tertiary": "<Control-Key-3>",
    "index_shortcut_reset": "<Control-Key-0>",
    # EPUBForge "Generation Type" / "XHTML Profile" (spec: "EPUBForge -
    # MASTER APPLICATION ARCHITECTURE & XHTML PROFILE SYSTEM", Part 7/8) -
    # "CUPEPUB" here is a DIFFERENT, additive gate from the existing
    # "profile" setting above (XML/EPUB/CUPEPUB tag sets) - see
    # gui/toolbar.py's set_generation_type_options. xhtml_profile_key is
    # the profiles/xhtml/<key>.json to use when generation_type is
    # "Client XHTML" (core/xhtml_profile_manager.py); "" = none selected
    # yet. xhtml_profile_label mirrors the dropdown's own display text so
    # a reopened project shows the same combobox text without needing to
    # reload/re-lookup the profile just to redraw the control.
    "generation_type": "CUPEPUB",   # "CUPEPUB" | "XHTML-EPUB" | "Client XHTML"
    "xhtml_profile_key": "",
    "xhtml_profile_label": "",
}


def build_project_data(pdf_path: str, dpi: int, zoom: float, zone_manager: ZoneManager,
                        settings: dict = None, counters: dict = None, page_count: int = 0,
                        page_rotations: dict = None, verification_data: dict = None) -> dict:
    """Builds the COMPLETE editable-project-state dict (spec: "ZONING -
    CUPPEUB DEFAULT PROFILE + CONTINUOUS AUTOSAVE" section 18) - the single
    function both the manual Save Project button (save_project below) and
    core.autosave_service.AutoSaveService's continuous background autosave
    call, so there is exactly ONE place that decides what a project file
    contains, never two divergent serializers."""
    settings = dict(DEFAULT_SETTINGS, **(settings or {}))
    return {
        "schema_version": SCHEMA_VERSION,
        # Top-level mirrors of the CURRENTLY ACTIVE profile/generation
        # config (spec section 4's literal JSON example) - settings
        # ["profile"]/["generation_type"]/etc. below remain the ONE
        # canonical value actually read back by load_into_zone_manager's
        # caller (gui/main_window.py's load_project) - this is an explicit,
        # documented top-level VIEW of the same data, never a second place
        # it's stored or a second thing that could disagree with it.
        "profile": settings.get("profile", "XML"),
        "profile_version": settings.get("profile_version"),
        "generation_settings": {
            "generation_type": settings.get("generation_type"),
            "epub_component_type": settings.get("epub_component_type"),
            "xhtml_profile_key": settings.get("xhtml_profile_key"),
        },
        "pdf": pdf_path,
        # Source-PDF fingerprint (spec section 30) - compared on reopen so
        # a project can warn ("the source PDF has changed since this
        # project was saved") without ever blocking or discarding zoning.
        "pdf_size": os.path.getsize(pdf_path) if pdf_path and os.path.exists(pdf_path) else None,
        "pdf_mtime": os.path.getmtime(pdf_path) if pdf_path and os.path.exists(pdf_path) else None,
        "dpi": dpi,
        "zoom": zoom,
        "pages": {"count": page_count},
        "zones": [z.to_dict() for z in zone_manager.zones.values()],
        "settings": settings,
        "counters": dict(counters or {"figure": 0, "equation": 0, "section": 0, "boxed_text": 0}),
        "next_zone_serial": zone_manager._next_serial_num,
        "next_created_order": zone_manager._next_created_order,
        # Per-page VIEW/EDIT rotation (0/90/180/270, keyed by page number) -
        # a display/layout property only, never baked into the PDF itself
        # and never affecting zone.bbox (always stored in the PDF's own
        # native, unrotated coordinate space - see gui/pdf_viewer.py's
        # rotation-aware pdf_to_screen/screen_to_pdf). json.dump naturally
        # stringifies the int page-number keys, matching the documented
        # {"1": 0, "2": 90, ...} project-file shape.
        "page_rotations": dict(page_rotations or {}),
        # Verification workflow state (core/verification/) - absent (or {})
        # in any project saved before this feature existed, or one whose
        # operator never opened Verify; core.verification.verification_
        # session.session_from_project_data's own .get("verification", {})
        # backfills this exactly like every other optional key here.
        "verification": dict(verification_data or {}),
    }


def save_project(path: str, pdf_path: str, dpi: int, zoom: float, zone_manager: ZoneManager,
                  settings: dict = None, counters: dict = None, page_count: int = 0,
                  page_rotations: dict = None, verification_data: dict = None):
    data = build_project_data(pdf_path, dpi, zoom, zone_manager, settings, counters,
                               page_count, page_rotations, verification_data)
    # Atomic write (spec section 15) even for an explicit, one-off manual
    # Save/Save As - a plain open()+json.dump() could leave a half-written
    # file if interrupted mid-write; os.replace() (inside atomic_write_json)
    # never can. Same helper the continuous autosave service uses for its
    # own project.json write, just without the .bak/recovery rotation
    # that's specific to that always-on background service.
    autosave_service.atomic_write_json(path, data)
    return path


def load_project(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("settings", dict(DEFAULT_SETTINGS))
    data.setdefault("counters", {"figure": 0, "equation": 0, "section": 0, "boxed_text": 0})
    # Absent in a project saved before this feature existed - defaults to
    # "no page has any rotation" without error (every page reads as 0°).
    data.setdefault("page_rotations", {})
    data.setdefault("verification", {})
    return data


def load_into_zone_manager(data: dict, zone_manager: ZoneManager):
    zone_manager.zones = {}
    for zd in data.get("zones", []):
        zone = Zone.from_dict(zd)
        zone_manager.zones[zone.zone_id] = zone
    next_serial = data.get("next_zone_serial", 1)
    zone_manager.load_counter(max(next_serial, 1))
    zone_manager._next_created_order = max(data.get("next_created_order", 1), 1)
    zone_manager._undo_stack.clear()
    zone_manager._redo_stack.clear()
    # Reading Order migration: zones saved by a version of this app that
    # already had a proper Reading Order keep their exact relative sequence
    # (normalize_reading_order's sort key preserves any existing serial
    # value's relative order, only compacting gaps/duplicates). A zone from
    # an OLDER project file with no serial at all (None) sorts after every
    # zone that has one, tie-broken by created_order, so it lands at its
    # true original creation position instead of an arbitrary one. Either
    # way, zones are NEVER reordered by bbox/page/coordinates here.
    # Merge/split metadata migration (older projects could keep a Merge
    # Previous relationship on a split parent, whose own text is never
    # emitted) - re-pointed to the pieces that carry the text. No change to
    # the saved JSON shape; a no-op for already-consistent projects.
    zone_manager.normalize_merge_relationships()
    zone_manager.normalize_reading_order()
