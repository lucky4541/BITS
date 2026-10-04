"""Loads profiles/xhtml/*.json - the client-specific XHTML STRUCTURE
profiles (spec: "EPUBForge - MASTER APPLICATION ARCHITECTURE & XHTML
PROFILE SYSTEM", Part 5/8). A DIFFERENT concept from core/profile_manager.py's
existing XML/EPUB/CUPEPUB "Profile" (which controls the TAG SET offered in
the Tag Toolbox) - this one controls the OUTPUT DOCUMENT STRUCTURE for the
"Client XHTML" generation mode specifically (body epub:type, section
attributes, header/title attributes, id patterns), read fresh from JSON so
adding/editing a client's structure never needs a Python code change (spec:
"Profiles must be configuration-driven... Do NOT hardcode all XHTML
structures throughout Python code").

Only "index" ships fully configured (the one profile the spec supplies an
exact, authoritative structure for - see index.json). Every other profile
loads as a clearly-marked placeholder (configured=False) - spec: "If a
profile's client structure has not yet been supplied, create the
configuration placeholder and clearly mark it... Do not invent
client-specific semantics." core/client_xhtml_generator.py refuses to
generate from an unconfigured profile rather than guess."""
import json
import os

from core.resource_path import resource_path

XHTML_PROFILES_DIR = resource_path("profiles", "xhtml")

_cache: dict = None


def _load_all() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    profiles = {}
    if os.path.isdir(XHTML_PROFILES_DIR):
        for fname in sorted(os.listdir(XHTML_PROFILES_DIR)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(XHTML_PROFILES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            key = data.get("key") or os.path.splitext(fname)[0]
            profiles[key] = data
    _cache = profiles
    return profiles


def reload():
    """Re-reads every profiles/xhtml/*.json fresh from disk - called after
    Settings > XHTML Profiles edits a file, so changes take effect without
    an app restart (spec: "Normal client profile changes must NOT require
    source-code changes")."""
    global _cache
    _cache = None
    return _load_all()


def list_profiles() -> list:
    """[(key, label, configured), ...] in a stable, sensible display order
    (configured ones first, matching Part 5's own listing with Index
    prominent), then alphabetical."""
    profiles = _load_all()
    items = [(k, v.get("label", k), bool(v.get("configured"))) for k, v in profiles.items()]
    items.sort(key=lambda t: (not t[2], t[1].lower()))
    return items


def get_profile(key: str) -> dict:
    return _load_all().get(key)


# Filename -> suggested profile key (spec Part 6: "Filename detection is
# ONLY a suggestion... Do NOT assume bm1/bm2/bm3 = one fixed semantic
# type... user-selected profile is authoritative"). Deliberately coarse -
# a "_bm" filename only ever suggests the GENERIC "Back Matter" category,
# never guesses a specific back-matter type (Index/Bibliography/Glossary)
# from the filename alone, exactly matching the spec's own worked example.
_FILENAME_PATTERNS = (
    ("cv", "cover"),
    ("half", "halftitle"),
    ("tp", "titlepage"),
    ("cp", "copyrightpage"),
    ("ded", "dedication"),
    ("toc", "toc"),
    ("pref", "preface"),
    ("contrib", "contributor"),
    ("ack", "acknowledgement"),
    ("fwd", "foreword"),
    ("foreword", "foreword"),
    ("intro", "introduction"),
    ("fm", "frontmatter"),
    ("bm", "backmatter"),
    ("pt", "part"),
    ("ch", "chapter"),
    ("app", "appendix"),
    ("ser", "series"),
)


def suggest_profile_key(filename: str):
    """Best-effort filename->profile suggestion (spec Part 6's own
    "01_63802_cv -> Cover", "05_63802_pt1 -> Part", "06_63802_ch1 ->
    Chapter", "12_63802_bm1 -> Back Matter" examples) - matches the
    LAST "_token" segment (ignoring trailing digits) against a known
    short-code table. Returns None (no suggestion, never a wrong guess)
    if nothing matches - the caller falls back to leaving the profile
    selector unset rather than a confident-looking wrong default."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0].lower()
    parts = [p for p in stem.split("_") if p]
    for part in reversed(parts):
        token = part.rstrip("0123456789")
        if not token:
            continue
        for code, key in _FILENAME_PATTERNS:
            if token == code:
                return key
    return None
