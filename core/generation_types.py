"""TYPE dropdown category grouping + display labels (spec: "EPUBForge -
TYPE Dropdown, CUPEPUB Output Behavior & Exact Text Extraction").

Purely a DISPLAY/grouping layer over the EXISTING "component type" concept
already wired into generation - core.profile_manager's per-profile
`component_types` list (EPUB: a static list in profiles/epub_profile.json;
CUPEPUB: derived live from Mapping.xml by core.cup_config.
derive_component_types(), both already profile-aware with zero changes
needed here) and gui.main_window.App's existing `settings["epub_component_
type"]`, already read by App.generate_xhtml()/generate_xml() via
core.epub_xml_generator.EpubXmlGenerator(component_type=...) - see that
call site's own `component_type = self.settings.get("epub_component_type")
or self.active_profile.get("default_component_type", "chapter")` fallback,
completely unchanged by this module. This file introduces NO new
generation concept and touches no existing profile file - only a fixed
category table for how the TYPE dropdown GROUPS and LABELS the keys a
profile's own component_types already declares.

"seriestype" (the exact key spec requires) is a pure DISPLAY alias for the
existing "series" component_type key - grepped every profile file
(profiles/epub_profile.json, Mapping.xml-derived CUPEPUB types): neither
defines a literal "seriestype" key, both already use "series" for the
identical concept. resolve_type_key() is the ONLY place this alias is
consulted; the underlying settings value stored is always the REAL
existing key ("series"), never the invented display name, so generation
code needs zero awareness of this module at all.
"""

TYPE_CATEGORIES = [
    ("FRONT MATTER", [
        "cover", "halftitle", "titlepage", "copyrightpage", "dedication", "toc",
        "preface", "contributor", "acknow", "foreword", "introduction", "fm",
    ]),
    ("BODY", ["part", "chapter", "section", "appendix"]),
    ("BACK MATTER", ["bm", "glossary", "bibliography", "index", "seriestype"]),
]

TYPE_LABELS = {
    "cover": "Cover", "halftitle": "Half Title", "titlepage": "Title Page",
    "copyrightpage": "Copyright Page", "dedication": "Dedication", "toc": "TOC",
    "preface": "Preface", "contributor": "Contributor", "acknow": "Acknowledgements",
    "foreword": "Foreword", "introduction": "Introduction", "fm": "Front Matter",
    "part": "Part", "chapter": "Chapter", "section": "Section", "appendix": "Appendix",
    "bm": "Back Matter", "glossary": "Glossary", "bibliography": "Bibliography",
    "index": "Index", "seriestype": "Series",
}

# Display key -> the REAL component_type key a profile's own list actually
# uses, where it differs from this dropdown's spec-required spelling (see
# module docstring). A display key absent from this map resolves to itself.
_DISPLAY_TO_REAL_KEY = {"seriestype": "series"}


def resolve_type_key(display_key: str) -> str:
    return _DISPLAY_TO_REAL_KEY.get(display_key, display_key)


def available_types(component_types) -> list:
    """[(category, [(display_key, label), ...]), ...], in the fixed
    category order/membership above, filtered to only the display keys
    whose resolved real key the ACTIVE profile's own component_types
    actually declares (spec requirement 3: "Update available TYPE
    values" per profile). A category with nothing available is omitted
    entirely rather than shown empty. Never invents a key the active
    profile doesn't itself already offer."""
    real_keys = set(component_types or [])
    result = []
    for category, keys in TYPE_CATEGORIES:
        # Entries are returned as (REAL key, label) - resolve_type_key()
        # here, not the raw display key, so a caller storing this key
        # directly into settings["epub_component_type"] (see gui/toolbar.
        # py's refresh_type_dropdown/_on_type_selected) always writes a
        # value the active profile's own component_types actually
        # recognizes ("series", never the display-only "seriestype").
        entries = [(resolve_type_key(key), TYPE_LABELS.get(key, key.title()))
                   for key in keys if resolve_type_key(key) in real_keys]
        if entries:
            result.append((category, entries))
    return result
