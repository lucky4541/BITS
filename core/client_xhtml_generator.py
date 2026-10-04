""""Client XHTML" generation mode (spec: "EPUBForge - MASTER APPLICATION
ARCHITECTURE & XHTML PROFILE SYSTEM", Part 9-12) - produces XHTML matching
a client's own EXACT, profile-specified structure (body epub:type, section
attributes, header/h1 attributes, id patterns), completely bypassing
Mapping.xml (which CUPEPUB/EPUB's existing "XHTML/EPUB" generation mode
still uses, unchanged - spec: "Do not mix CUPEPUB XML and Client XHTML
generation").

Deliberately NOT a second zoning/generation engine (spec: "Do NOT
duplicate the existing zoning engine"): this reuses core.epub_xml_
generator.EpubXmlGenerator for every piece of real work - zone hierarchy
walking, index-list nesting (indexprimary/secondary/territory ->
nested <ul>/<li>, the SAME engine core/index_auto_zone.py's detected
zones already feed into), dehyphenation, pagebreak handling, image
extraction, and the maintitle->h1/id-assignment pipeline already built
for the existing "Generate XHTML" flow (core.epub_xml_generator.
EpubXmlGenerator.promote_maintitle_to_h1/assign_missing_ids/
extract_document_title) - only the FINAL wrapping (what gets put around
that already-correct content) is profile-specific here."""
from lxml import etree

from core.epub_xml_generator import EpubXmlGenerator


class ClientProfileNotConfigured(Exception):
    """Raised when generation is attempted against an XHTML profile whose
    client structure hasn't been supplied yet (profiles/xhtml/<key>.json's
    own "configured": false - spec: "If a profile's client structure has
    not yet been supplied... clearly mark it: CLIENT PROFILE NOT
    CONFIGURED... Do not invent client-specific semantics"). Never
    produces a guessed/placeholder XHTML structure - generation simply
    refuses, with a message the caller shows the user directly."""
    def __init__(self, profile_label: str):
        super().__init__(
            f"CLIENT PROFILE NOT CONFIGURED: \"{profile_label}\" has no client-specific XHTML "
            f"structure supplied yet. Generation is blocked until one is provided "
            f"(Settings > XHTML Profiles, or edit profiles/xhtml/<key>.json directly).")
        self.profile_label = profile_label


# The generic index-hierarchy tag family every profile that contains an
# index entry list shares - matches core/index_auto_zone.py's own
# DEFAULT_INDEX_HIERARCHY_TAGS exactly (the same tag vocabulary the
# existing Index Auto-Zone feature already produces zones with), so
# Client XHTML generation understands index zones from that OR from
# CUPEPUB's own real config identically.
_DEFAULT_INDEX_HIERARCHY_TAGS = {1: "indexprimary", 2: "indexsecondary", 3: "indexterritory"}


def generate(zone_manager, pdf_document, xhtml_profile: dict, prefix: str, images_dir: str,
             image_prefix: str = None, index_hierarchy_tags: dict = None):
    """Returns (body_element, document_title). `xhtml_profile`: a loaded
    profiles/xhtml/<key>.json dict (see core.xhtml_profile_manager).
    Raises ClientProfileNotConfigured if the profile isn't configured -
    the caller must not call this at all for an unconfigured profile
    without expecting/handling that."""
    if not xhtml_profile or not xhtml_profile.get("configured"):
        raise ClientProfileNotConfigured((xhtml_profile or {}).get("label", "?"))

    generator_profile = {
        "index_hierarchy_tags": index_hierarchy_tags or _DEFAULT_INDEX_HIERARCHY_TAGS,
        "page_marker_tags": [],
        "footnote_flow_tags": [],
        "auto_pagebreak": True,
    }
    gen = EpubXmlGenerator(zone_manager, pdf_document, images_dir, prefix, generator_profile,
                            component_type=xhtml_profile["key"], image_prefix=image_prefix)
    component_root = gen.generate()
    # Same maintitle->h1 promotion + deterministic id assignment the
    # existing "Generate XHTML" flow already runs (gui/main_window.py's
    # App.generate_xhtml) - reused here unchanged so a project zoned with
    # either "title" or "maintitle" as its title-zone tag works
    # identically either way.
    gen.promote_maintitle_to_h1(component_root)
    gen.assign_missing_ids(component_root)

    body = etree.Element("body")
    body.set("epub_type", xhtml_profile["body_epub_type"])
    section = etree.SubElement(body, "section")
    for k, v in (xhtml_profile.get("section") or {}).items():
        if v is not None:
            section.set(k, v)
    title_id = xhtml_profile["id_pattern"].format(prefix=prefix)
    section.set("aria-labelledby", title_id)
    for child in list(component_root):
        section.append(child)

    # The profile's own declared header/title attributes are authoritative
    # (spec: "Client XHTML structure must be followed exactly when
    # supplied") - overrides whatever EpubXmlGenerator's own internal
    # component-type-keyed default table (_H1_CLASS_BY_COMPONENT) or
    # assign_missing_ids's generic id-numbering scheme produced, so the id
    # is always exactly the profile's own deterministic pattern regardless
    # of which zone tag ("title" vs "maintitle") supplied the text.
    header_cfg = xhtml_profile.get("header") or {}
    for h1 in section.iter(header_cfg.get("title_tag", "h1")):
        h1.set("id", title_id)
        if header_cfg.get("title_class"):
            h1.set("class", header_cfg["title_class"])
        if header_cfg.get("title_epub_type"):
            h1.set("epub_type", header_cfg["title_epub_type"])
        break  # only the document's own title heading, never a later same-tag sibling

    document_title = gen.extract_document_title(section) or xhtml_profile.get("html_title") or prefix
    return body, document_title
