"""Single, centralized resource/data-path resolver for ZoneTool.

Distinguishes the two kinds of on-disk location every other module needs,
per the packaging requirements this exists to satisfy:

  - READ-ONLY APPLICATION RESOURCES (profiles/*.json, profiles/CUPEPUB/*.xml,
    Mapping.xml, tag definitions) - bundled INSIDE the packaged app. Resolved
    via resource_root()/resource_path().

  - WRITABLE USER DATA (projects/, output/, ocr_cache/, app settings) - must
    always be a real, persistent directory the app can write to, and must
    NEVER be PyInstaller's onefile temp-extraction directory (wiped on every
    exit). Resolved via writable_root()/writable_path().

Before this module existed, THREE separate modules (core/profile_manager.py,
core/cup_config.py, core/ui_prefs.py) each independently computed their own
APP_ROOT as os.path.dirname(os.path.dirname(os.path.abspath(__file__))) -
correct when running from source (`python main.py`), but fragile once
frozen: __file__ for a module compiled into PyInstaller's PYZ archive is not
guaranteed to be a real, navigable filesystem path, so directory arithmetic
on it is undefined in a packaged build. gui/main_window.py separately had
its OWN correct, frozen-aware APP_ROOT (checking sys.frozen and using
sys.executable) for its own writable directories - this module generalizes
that SAME already-proven pattern into the one place every other module
should use instead of re-deriving it.

Nothing about WHICH resources exist or where they logically live (e.g.
"profiles/CUPEPUB/Mapping.xml") changes; only HOW the base directory in
front of that relative path is computed changes, and only when frozen -
running from source (`python main.py`) is completely unaffected, since
resource_root() and writable_root() both resolve to the same project root
in that case, exactly as every caller's own pre-existing APP_ROOT already
did.
"""
import os
import sys


def is_frozen() -> bool:
    """True only when running inside a PyInstaller-built EXE (bootloader
    sets sys.frozen) - never assumed true just because __file__ looks odd,
    and never assumed false just because sys._MEIPASS happens to be unset
    for some other reason."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> str:
    """Base directory for bundled, READ-ONLY application resources.

    sys._MEIPASS is the one PyInstaller attribute that resolves correctly
    for BOTH build modes: onedir (points at the _internal/ directory
    PyInstaller places beside the EXE) and onefile (points at the
    temporary directory PyInstaller extracts the bundle into at startup) -
    which is exactly why resources are resolved through it here instead of
    sys.executable's own directory (see writable_root() below for why that
    one is used for user data instead)."""
    if is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def writable_root() -> str:
    """Base directory for USER-GENERATED data (projects, output, OCR
    cache, app settings, logs) - always a real, persistent, user-writable
    directory, NEVER sys._MEIPASS (a onefile build's _MEIPASS is a temp
    directory deleted when the process exits, so anything written there
    would silently vanish between runs).

    A packaged build resolves this to %APPDATA%\\EPUBForge - NOT beside
    the EXE (sys.executable's own directory), which an earlier version of
    this function used: that was correct for a dev-style onedir folder the
    user runs directly from anywhere, but breaks once the installer places
    the EXE under C:\\Program Files\\EPUBForge\\, which a standard,
    non-elevated user cannot write to at all (every autosave/OCR-cache/
    log write would silently fail or force the app to require admin
    rights just to run normally). %APPDATA% is always writable by the
    current user without elevation, exactly like every other well-behaved
    Windows application's per-user data."""
    if is_frozen():
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "EPUBForge")
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts: str) -> str:
    """Absolute path to a bundled, read-only application resource, e.g.
    resource_path("profiles", "CUPEPUB", "Mapping.xml")."""
    return os.path.join(resource_root(), *parts)


def writable_path(*parts: str) -> str:
    """Absolute path to a writable, user-data location, e.g.
    writable_path("output")."""
    return os.path.join(writable_root(), *parts)


class ResourceNotFoundError(Exception):
    """Raised by require_resource() - the message is always a clean,
    package-relative resource name (e.g. "profiles/CUPEPUB/Mapping.xml"),
    NEVER the raw resolved absolute filesystem path (which, in a packaged
    build, would be a confusing internal extraction/bundle path with no
    meaning to an end user - "do not expose development-specific paths to
    normal users")."""


def required_resource_list() -> list:
    """The single canonical list of bundled, READ-ONLY resources every
    EPUBForge build (onedir or onefile) must ship - each entry is a
    resource_path()-relative path (a file) or a directory that must
    contain at least one file. Used by main.py's --selfcheck-resources
    flag (runs INSIDE the actual built EXE, the only way to genuinely
    verify a onefile build's bundle - see packaging/check_package.py's
    verify_onefile(), which launches the real EXE with this flag rather
    than trying to statically inspect a onefile archive). Kept here
    (rather than duplicated in packaging/check_package.py, which has its
    own separate file-by-file onedir check against dist/EPUBForge/
    _internal/ directly) so the list end users' own running app checks
    against can never silently drift from what resource_path() itself
    resolves."""
    profile_files = [
        "profiles/xml_profile.json",
        "profiles/epub_profile.json",
        "profiles/CUPEPUB/CUPEPUB_Profile.xml",
        "profiles/CUPEPUB/CUPEPUB_Zoning.xml",
        "profiles/CUPEPUB/CUPEPUB_Master.xml",
        "profiles/CUPEPUB/CUPEPUB_AutoStyling.xml",
        "profiles/CUPEPUB/CUPEPUB_Character.xml",
        "profiles/CUPEPUB/CUPEPUB_ZoneValidation.xml",
        "profiles/CUPEPUB/CUPLookup.xml",
        "profiles/CUPEPUB/Mapping.xml",
    ]
    ocr_model_dirs = [
        "ocr_models/PP-LCNet_x1_0_doc_ori", "ocr_models/UVDoc",
        "ocr_models/PP-LCNet_x1_0_textline_ori",
        "ocr_models/PP-OCRv6_medium_det", "ocr_models/PP-OCRv6_medium_rec",
    ]
    validation_files = ["tools/epubcheck/epubcheck.jar", "runtime/java/bin/java.exe"]
    validation_dirs = ["tools/epubcheck-testsuite/epub2", "tools/epubcheck-testsuite/epub3"]
    return profile_files + ocr_model_dirs + validation_files + validation_dirs


def selfcheck_missing() -> list:
    """Resolves every entry in required_resource_list() through
    resource_path() and returns the ones that are actually missing (a
    file that doesn't exist, or a directory that's empty/absent) - empty
    list means the running build's bundle is genuinely complete."""
    missing = []
    for rel in required_resource_list():
        full = resource_path(*rel.split("/"))
        if os.path.isdir(full):
            if not any(os.scandir(full)):
                missing.append(rel)
        elif not os.path.isfile(full):
            missing.append(rel)
    return missing


def require_resource(*parts: str) -> str:
    """Like resource_path(), but fails loudly and cleanly: raises
    ResourceNotFoundError immediately if the resolved file/directory is
    missing, instead of letting some later, unrelated open()/parse call
    surface a raw FileNotFoundError with a confusing internal path deep in
    a stack trace. Callers that want this checked at startup (so a missing
    resource is caught during the build's own verification pass rather
    than at first use) can call this directly; callers that already have
    their own fallback behavior for a missing file (e.g.
    profile_manager.py's XML-profile constants.py fallback) keep using
    resource_path() and their own os.path.isfile() check unchanged."""
    full = resource_path(*parts)
    if not os.path.exists(full):
        raise ResourceNotFoundError(f"Required resource not found: {'/'.join(parts)}")
    return full
