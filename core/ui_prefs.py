"""Tiny app-level (not per-project) UI preference store - currently just
the Light/Dark theme choice (spec 66.25). Deliberately separate from
core/project_manager.py's per-project settings.json: a theme choice is a
preference about the APPLICATION, not about any one zoning project, so it
must survive across different projects/PDFs rather than round-tripping
with project save/load."""
import json
import os

from core.resource_path import writable_path

# A user-modifiable setting, NOT a bundled application resource - must
# resolve via writable_path (a real, persistent directory beside the EXE),
# never resource_path: a packaged onefile build's bundled-resource location
# (sys._MEIPASS) is a temp directory wiped on every exit, which would
# silently discard the user's theme/OCR preferences between runs.
PREFS_PATH = writable_path("profiles", "ui_prefs.json")

DEFAULT_PREFS = {
    "theme": "light",
    # OCR settings (spec 43) - app-level like theme, not per-project: the
    # user's preferred engine/language/quality/detection toggles carry
    # across different PDFs/projects rather than resetting each time.
    "ocr_engine": "PaddleOCR",
    "ocr_language": "en",
    "ocr_dpi": 300,
    # "auto" (classify then decide), "force_ocr" (always OCR this page),
    # "digital_only" (never OCR, existing extractor result as-is) - see
    # core.ocr.ocr_service.OCR_MODES.
    "ocr_mode": "auto",
    "ocr_preprocessing": {
        "deskew": False,
        "remove_borders": False,
        "grayscale": False,
        "enhance_contrast": False,
        "denoise": False,
        "adaptive_threshold": False,
    },
}


def load_ui_prefs() -> dict:
    if not os.path.isfile(PREFS_PATH):
        return dict(DEFAULT_PREFS)
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return dict(DEFAULT_PREFS, **data)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PREFS)


def save_ui_prefs(prefs: dict):
    """Merges `prefs` onto the EXISTING saved file (via load_ui_prefs,
    itself already merged onto DEFAULT_PREFS), not onto DEFAULT_PREFS
    directly - now that this file holds more than one independent setting
    (theme, OCR engine/language/dpi/preprocessing), a caller updating just
    one of them (e.g. toggle_theme saving only {"theme": ...}) must never
    silently reset every other already-saved setting back to its default."""
    current = load_ui_prefs()
    current.update(prefs)
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    with open(PREFS_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
