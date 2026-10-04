"""Confidence-bucket thresholds for auto-zoned candidates. Thresholds are
configurable (spec: "Make these thresholds configurable") via
app.settings["auto_zone_thresholds"] (see core.project_manager.
DEFAULT_SETTINGS for the default, gui/dialogs.py SettingsDialog for the UI)
- this module never hardcodes them beyond a fallback default for a caller
that passes no settings at all (e.g. a standalone/test run)."""

DEFAULT_THRESHOLDS = {"high": 90, "medium": 75}


def bucket(confidence: float, thresholds: dict = None) -> str:
    thresholds = thresholds or DEFAULT_THRESHOLDS
    if confidence >= thresholds.get("high", 90):
        return "high"
    if confidence >= thresholds.get("medium", 75):
        return "medium"
    return "low"
