"""Loads the zoning profiles (profiles/*_profile.json) the Profile dropdown
switches between:

    BITS  profiles/bits_profile.json - BITS 2.2 book tags
    JATS  profiles/jats_profile.json - JATS 1.4 article tags

Both files are generated from core/bits/vocabulary.py (python -m
core.bits.vocabulary) and may be edited by hand; when one is missing or
invalid the built-in vocabulary is used, so the app always starts."""
import json
import os

from core.resource_path import resource_path

PROFILES_DIR = resource_path("profiles")

DEFAULT_PROFILE_NAME = "BITS"


_cache: dict[str, dict] = {}


def _profile_path(name: str) -> str:
    return os.path.join(PROFILES_DIR, f"{name.strip().lower()}_profile.json")


def list_profile_names() -> list[str]:
    """BITS (BITS 2.2 book) and JATS (JATS 1.4 journal article)."""
    return ["BITS", "JATS"]


def load_profile(name: str) -> dict:
    """Reads profiles/<name>_profile.json fresh from disk (no cache) - so a
    user hand-editing the tag list/mapping path takes effect on the next
    profile switch or app restart without any code change. Raises
    ProfileLoadError with a clear message on missing/invalid JSON for any
    profile OTHER than XML (which has the constants.py fallback above).

    BITS / JATS fall back to the built-in vocabulary (core.bits.vocabulary)
    when their JSON file is missing."""
    path = _profile_path(name)
    rel_name = f"profiles/{os.path.basename(path)}"
    if not os.path.isfile(path):
        if name.strip().upper() in ("BITS", "JATS"):
            from core.bits import vocabulary
            return vocabulary.profile(name.strip().upper())
        # A clean, package-relative resource name only - never the raw
        # resolved absolute path (which, in a packaged build, is a
        # confusing internal bundle/extraction path with no meaning to an
        # end user).
        raise ProfileLoadError(f"Required resource not found: {rel_name}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        if name.strip().upper() in ("BITS", "JATS"):
            from core.bits import vocabulary
            return vocabulary.profile(name.strip().upper())
        raise ProfileLoadError(f"Could not parse {rel_name}: {e}")
    data.setdefault("tag_buttons", [])
    data.setdefault("tag_colors", {})
    data.setdefault("image_kinds", {})
    data.setdefault("component_types", [])
    data.setdefault("mapping_xml_path", "")
    data.setdefault("xml_enabled", True)
    data.setdefault("xhtml_enabled", False)
    data.setdefault("root_tag", "book")
    # Merge Previous's "find previous compatible content" search (core/
    # zone_manager.py) - empty by default so a profile that doesn't
    # declare these gets no special skip-behavior beyond what it already
    # had (XML's own xml_profile.json declares page_marker_tags itself).
    data.setdefault("page_marker_tags", [])
    data.setdefault("footnote_flow_tags", [])
    # Same idea for the automatic-continuation/Merge-Previous "an image/
    # figure must not block a genuine text continuation" skip-list (spec:
    # "EPUBForge - Global Merge, Continuation, Reading Order and Exact
    # Text Preservation Engine"). A profile that doesn't declare its own
    # gets it DERIVED from its own tag_buttons' existing "asset_kind"
    # attribute - the same convention EPUB's own profiles/epub_profile.json
    # tag_buttons already use for image-file-naming (see core/constants.py's
    # NON_FLOW_TAGS docstring) - never a second, separately-maintained
    # image-tag list.
    if "non_flow_tags" not in data:
        data["non_flow_tags"] = [b["tag"] for b in data.get("tag_buttons", [])
                                   if b.get("attrs", {}).get("asset_kind")]
    # Index Auto-Zone's level->tag mapping (auto_zoning/index_auto_zone.py,
    # core/epub_xml_generator.py's _build_index_hierarchy) - a JSON profile
    # that doesn't declare its own gets the same three tag names EPUB's own
    # profiles/epub_profile.json tag_buttons already use (spec: "read the
    # active profile... this allows future client profiles to use different
    # tags" - a profile that DOES want different tags just adds its own
    # "index_hierarchy_tags" key, no code change needed).
    data.setdefault("index_hierarchy_tags", {1: "indexprimary", 2: "indexsecondary", 3: "indexterritory"})
    return data


class ProfileLoadError(Exception):
    pass


def get_profile(name: str) -> dict:
    key = (name or DEFAULT_PROFILE_NAME).strip().upper()
    if key not in _cache:
        _cache[key] = load_profile(key)
    return _cache[key]


def reload_profile(name: str) -> dict:
    """Discards the cached copy and reloads from disk - used after the user
    edits a profile JSON externally and wants it picked up without
    restarting the app (Settings > Reload Profiles, or automatically on
    every profile switch - see gui/main_window.py App.set_profile)."""
    key = (name or DEFAULT_PROFILE_NAME).strip().upper()
    _cache.pop(key, None)
    return get_profile(key)


def tag_buttons_of(profile: dict) -> list[tuple[str, str, dict]]:
    """Adapts profile["tag_buttons"] (list of {label,tag,attrs} dicts, the
    JSON-friendly shape) back into the (label, tag, attrs) tuple shape
    gui/zone_panel.py's TagPanel and gui/dialogs.py already expect from the
    old constants.TAG_BUTTONS - so both call sites need only the smallest
    possible change (an injected list instead of a module-level import)."""
    return [(b["label"], b["tag"], dict(b.get("attrs", {}))) for b in profile.get("tag_buttons", [])]
