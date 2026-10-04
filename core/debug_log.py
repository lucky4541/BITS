"""Optional debug logging, toggled via Settings > Debug logging (off by
default). When enabled, prints tagged lines to the console for zone
mutations: [ZONE] [RESIZE] [SPLIT] [MERGE] [READING_ORDER] [PARENT] [XML]
[SCROLL] (mouse-wheel/touchpad routing - see gui/pdf_viewer.py/gui/
zone_panel.py's own wheel handlers).

Also mirrors every line to debug.log next to the app (core.resource_path.
writable_root - the same real, persistent directory project/settings/OCR-
cache data already lives in, never a onefile build's temp _MEIPASS) - a
packaged --windowed EXE build has no visible console for print() to reach
at all, so the console-only version of this module was useless for
diagnosing anything (e.g. real touchpad hardware behavior) on such a
build; the file is readable regardless of how the app was launched."""
import os

from core.resource_path import writable_root

_enabled = False
# Set ZONETOOL_TRACE_EXTRACTION=1 before launching ZoneTool to force verbose
# extraction/OCR diagnostics even when the GUI Debug logging toggle is off.
_forced = os.environ.get("ZONETOOL_TRACE_EXTRACTION", "").strip().lower() in {"1", "true", "yes", "on"}
_log_path = os.path.join(writable_root(), "debug.log")
_file_started = False  # True once this PROCESS has truncated/started its own fresh log


def set_enabled(value: bool):
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    return _enabled or _forced


def log_path() -> str:
    return _log_path


def log(tag: str, *lines):
    if not _enabled:
        return
    global _file_started
    formatted = [f"[{tag}] {line}" for line in lines]
    for line in formatted:
        print(line)
    try:
        mode = "w" if not _file_started else "a"
        with open(_log_path, mode, encoding="utf-8") as f:
            for line in formatted:
                f.write(line + "\n")
        _file_started = True
    except OSError:
        pass  # logging must never itself crash the app
