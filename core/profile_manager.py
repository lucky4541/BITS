"""Loads Profile configuration (profiles/*.json) - the external, config-driven
tag/image/mapping definitions that the Profile dropdown (toolbar) switches
between. XML profile behavior is a pure ADDITION on top of the existing,
untouched core/constants.py: if profiles/xml_profile.json is missing or
fails to parse, get_profile("XML") falls back to a profile built directly
from constants.TAG_BUTTONS/DEFAULT_TAG_COLORS, so the existing app can never
regress just because a JSON file got deleted or corrupted. EPUB has no such
hardcoded fallback - profiles/epub_profile.json is the only source for it,
per the spec's "must NOT be hard-coded" requirement.
"""
import json
import os

from core.resource_path import resource_path

PROFILES_DIR = resource_path("profiles")

DEFAULT_PROFILE_NAME = "XML"


def _fallback_xml_profile() -> dict:
    from core.constants import TAG_BUTTONS, DEFAULT_TAG_COLORS, TAG_PAGENUMBER, NON_FLOW_TAGS
    return {
        "name": "XML",
        "xml_enabled": True,
        "xhtml_enabled": False,
        "root_tag": "book",
        "tag_buttons": [{"label": label, "tag": tag, "attrs": dict(attrs)} for label, tag, attrs in TAG_BUTTONS],
        "tag_colors": dict(DEFAULT_TAG_COLORS),
        "image_kinds": {},
        "component_types": [],
        "mapping_xml_path": "",
        # Merge Previous's "find previous compatible content" search
        # (core/zone_manager.py._find_previous_in_reading_order) always
        # skips these tags over rather than treating them as a blocker -
        # matches the literal "pagenumber" tag check hardcoded before this
        # was generalized, so XML profile behavior is unchanged.
        "page_marker_tags": [TAG_PAGENUMBER],
        "footnote_flow_tags": [],
        # Merge Previous / automatic continuation's "an image/figure/
        # equation must not block a continuation search" skip-list (spec:
        # "EPUBForge - Global Merge, Continuation, Reading Order and Exact
        # Text Preservation Engine") - the XML profile's own tag_buttons
        # don't use the asset_kind convention EPUB/CUPEPUB rely on (see
        # core/constants.py's NON_FLOW_TAGS docstring), so this is declared
        # explicitly here, matching page_marker_tags's own precedent.
        "non_flow_tags": sorted(NON_FLOW_TAGS),
        # XML/BITS has no multi-level Index Primary/Secondary/Territory
        # hierarchy (core/constants.py's own TAG_BUTTONS has no such tags -
        # confirmed by inspection; the XML profile's own "Index Entry" is a
        # single flat tag) - empty here (not the 3-level default other
        # profiles get) so gui/zone_panel.py's Auto Zone Index button, whose
        # visibility is tied to these tags actually being in the toolbox,
        # never appears for this profile at all.
        "index_hierarchy_tags": {},
    }


_cache: dict[str, dict] = {}


def _profile_path(name: str) -> str:
    return os.path.join(PROFILES_DIR, f"{name.strip().lower()}_profile.json")


def list_profile_names() -> list[str]:
    """Fixed, known order (not a directory listing) - matches the spec's
    "Profile: [ XML | EPUB | CUPEPUB ]" dropdown exactly. Extending profiles/
    with a new *_profile.json (or, for CUPEPUB, a new build_*_profile()
    function) in the future just needs its name added here."""
    return ["XML", "EPUB", "CUPEPUB"]


def load_profile(name: str) -> dict:
    """Reads profiles/<name>_profile.json fresh from disk (no cache) - so a
    user hand-editing the tag list/mapping path takes effect on the next
    profile switch or app restart without any code change. Raises
    ProfileLoadError with a clear message on missing/invalid JSON for any
    profile OTHER than XML (which has the constants.py fallback above).

    CUPEPUB is NOT a static JSON file - per spec, its tag list/shortcuts/
    mapping must be read live from profiles/CUPEPUB/*.xml (see
    core/cup_config.py), so it's synthesized fresh on every call here
    instead of read from a *_profile.json path."""
    if name.strip().upper() == "CUPEPUB":
        from core import cup_config
        try:
            return cup_config.build_cup_profile()
        except cup_config.CupConfigError as e:
            raise ProfileLoadError(str(e))
    path = _profile_path(name)
    rel_name = f"profiles/{os.path.basename(path)}"
    if not os.path.isfile(path):
        if name.strip().upper() == DEFAULT_PROFILE_NAME:
            return _fallback_xml_profile()
        # A clean, package-relative resource name only - never the raw
        # resolved absolute path (which, in a packaged build, is a
        # confusing internal bundle/extraction path with no meaning to an
        # end user).
        raise ProfileLoadError(f"Required resource not found: {rel_name}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        if name.strip().upper() == DEFAULT_PROFILE_NAME:
            return _fallback_xml_profile()
        raise ProfileLoadError(f"Could not parse {rel_name}: {e}")
    data.setdefault("tag_buttons", [])
    data.setdefault("tag_colors", {})
    data.setdefault("image_kinds", {})
    data.setdefault("component_types", [])
    data.setdefault("mapping_xml_path", "")
    data.setdefault("xml_enabled", name.strip().upper() == DEFAULT_PROFILE_NAME)
    data.setdefault("xhtml_enabled", name.strip().upper() != DEFAULT_PROFILE_NAME)
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
