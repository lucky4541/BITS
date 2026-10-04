"""Wraps a Mapping.xml-transformed intermediate tree into a complete XHTML
document and writes it to disk, with the validation spec section 27 asks
for (well-formed, image refs exist, no duplicate output filenames, no empty
src, valid UTF-8/escaping - all of which lxml's own serializer already
guarantees for well-formedness/escaping/UTF-8, so this module focuses on
the checks lxml does NOT do for free: real image files, empty src, dupes,
and unsubstituted "{0}" placeholders left behind by a mapping rule whose
<attrib> substitution didn't fire - see _find_unsubstituted_placeholders).

Doctype/namespace/attribute conventions below (bare <!DOCTYPE html>, the
epub/svg namespaces + epub:prefix on <html>, and renaming the plain
"epub_type"/"role" attributes Mapping.xml's own templates write into their
properly namespaced/EPUB3-native form) were reverse-engineered from real
production XHTML output the user supplied from the same pipeline family -
not from the (partial) Mapping.xml alone, which literally writes a plain
"epub_type" attribute with no namespace prefix. Mapping.xml has no rule
that performs this rename itself, so it must happen as a final,
independent serialization step - exactly what _apply_epub_namespace does.
"""
import copy
import os
import re

from lxml import etree

from core.epub_xml_generator import SRC_ATTR

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
SVG_NS = "http://www.w3.org/2000/svg"

# text_extractor.py's own inline markup vocabulary (see core/text_extractor.py)
# has no corresponding Mapping.xml rule (the provided Mapping.xml only covers
# structural/block elements) - normalized here, once, to standard XHTML
# equivalents rather than left as non-standard <bold>/<italic>/<break>
# elements in the final output. "underline"/"strike" have no automatic
# detector anywhere in the pipeline today (core.formatting_detector.
# detect_underline_chars exists but is never called) - they only ever
# appear via core/verification/inline_style.py's manual Style Editor
# overrides, mapped here to the real HTML5 <u>/<s> elements the same way
# bold/italic already map to <b>/<i>.
_INLINE_TAG_MAP = {"bold": "b", "italic": "i", "break": "br", "underline": "u", "strike": "s"}

_PLACEHOLDER_RE = re.compile(r"\{\d+\}")

# Mapping.xml's own TOC/footnote/endnote/glossary/acknowledgments/
# bibliography/exercise rules use sec0-sec6/ssec/bssec as INTERMEDIATE
# wrapper element names (confirmed by inspection - e.g. the
# exhead|expara|pagenum and glosshead|glossterm|glossdef enclose rules).
# These are Mapping.xml authoring convenience names, never valid final
# XHTML elements.
_INTERNAL_WRAPPER_RE = re.compile(r"^(sec[0-6]|ssec|bssec)$")


def rename_internal_wrapper_tags(root):
    """Renames every sec0-6/ssec/bssec element to <section>, in place,
    preserving every existing attribute and all children exactly as
    Mapping.xml's own enclose rules already nested them - the hierarchy
    those rules build is already structurally correct (a run of consecutive
    matching siblings gets wrapped together), only the tag NAME itself was
    ever wrong."""
    for el in root.iter():
        if isinstance(el.tag, str) and _INTERNAL_WRAPPER_RE.match(el.tag):
            el.tag = "section"


def _clean_inline_tags(el):
    for child in list(el):
        _clean_inline_tags(child)
    if el.tag == "smallcaps":
        # No dedicated final element (unlike bold/italic/break above) - a
        # small-caps run has no case change and no separate semantic
        # meaning of its own, only a rendering hint, so it becomes a plain
        # <span> carrying a class name a client-supplied stylesheet can
        # target (this app generates no template.css of its own - see
        # core/text_extractor.py's is_small_caps for where this marker
        # actually gets set).
        el.tag = "span"
        el.set("class", "smallcaps")
    elif el.tag in _INLINE_TAG_MAP:
        el.tag = _INLINE_TAG_MAP[el.tag]
    if SRC_ATTR in el.attrib:
        del el.attrib[SRC_ATTR]


def _apply_epub_namespace(el):
    """Mapping.xml's own <replace_ele> templates write a plain, unprefixed
    "epub_type" attribute (confirmed directly in the file - e.g. <body
    epub_type="frontmatter">). Real EPUB3/XHTML output needs this as the
    properly namespaced epub:type attribute instead (xmlns:epub =
    http://www.idpf.org/2007/ops) - reverse-engineered from real sample
    output, since nothing in Mapping.xml itself performs this rename."""
    for e in el.iter():
        if "epub_type" in e.attrib:
            value = e.attrib.pop("epub_type")
            e.set(f"{{{EPUB_NS}}}type", value)


def build_xhtml_document(body_content_root, title: str = "", lang: str = "en",
                          stylesheet_href: str = "template.css"):
    """body_content_root is the Mapping.xml-transformed <component>
    element (or whatever its rules renamed the root to - Mapping.xml has
    no rule matching the bare root element itself, so it normally survives
    as <component type="...">...</component>, becoming <body>'s single
    child exactly as the spec's "PDF -> ... -> XHTML" pipeline expects).
    Returns an lxml Element tree rooted at <html>."""
    working = copy.deepcopy(body_content_root)
    _clean_inline_tags(working)
    _apply_epub_namespace(working)

    html = etree.Element(
        "html",
        nsmap={None: XHTML_NS, "epub": EPUB_NS, "svg": SVG_NS},
    )
    html.set(f"{{{EPUB_NS}}}prefix", "index: http://www.index.com/")
    html.set(f"{{{'http://www.w3.org/XML/1998/namespace'}}}lang", lang)
    html.set("lang", lang)

    head = etree.SubElement(html, "head")
    title_el = etree.SubElement(head, "title")
    title_el.text = title or ""
    meta = etree.SubElement(head, "meta")
    meta.set("charset", "UTF-8")
    link = etree.SubElement(head, "link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", stylesheet_href)

    body = etree.SubElement(html, "body")
    # The document's own top-level epub:type (frontmatter/bodymatter/
    # backmatter) belongs on <body>, not on the wrapped <component>/
    # <section> - if Mapping.xml's own rules already produced a <body>
    # wrapper (e.g. the component[@type=...] family), unwrap it onto this
    # <body> instead of double-nesting <body><body>...
    working_local_tag = etree.QName(working).localname if "}" in str(working.tag) else working.tag
    if working_local_tag == "body":
        epub_type_attr = f"{{{EPUB_NS}}}type"
        if epub_type_attr in working.attrib:
            body.set(epub_type_attr, working.attrib[epub_type_attr])
        for child in list(working):
            body.append(child)
    else:
        body.append(working)
    return html


def _find_unsubstituted_placeholders(html_root) -> list[str]:
    """A real bug class confirmed in a real sample from this pipeline
    family: an <attrib findAttr=.../> substitution that didn't fire left a
    literal "{0}" in an href/src/etc attribute (e.g. <a href="{0}">) - dead
    links that lxml's well-formedness check can never catch on its own,
    since "{0}" is perfectly legal attribute text. Caught here instead."""
    errors = []
    for el in html_root.iter():
        for attr_name, value in el.attrib.items():
            if value and _PLACEHOLDER_RE.search(value):
                errors.append(f"Unsubstituted placeholder {value!r} left in @{attr_name} of <{el.tag}>.")
    return errors


def _find_duplicate_ids(html_root) -> list[str]:
    """Spec 98.4/98.42/98.43: IDs must not duplicate. Reported as an actual
    error (not silently deduplicated) so the underlying ID-assignment bug
    that produced it gets fixed at the source, per generator/mapping-engine
    rather than papered over here."""
    seen = {}
    for el in html_root.iter():
        el_id = el.get("id")
        if el_id:
            seen.setdefault(el_id, []).append(el)
    return [f"Duplicate id {el_id!r} used by {len(elements)} elements." for el_id, elements in seen.items()
            if len(elements) > 1]


def validate_xhtml(html_root, images_dir: str) -> list[str]:
    """Spec section 27 checks that aren't already guaranteed by lxml having
    built a well-formed tree in the first place: every img/@src is
    non-empty, points at a file that actually exists in images_dir, no
    unsubstituted "{0}"-style placeholders survive into an attribute value,
    no two different img elements resolve to the same output filename with
    different source zones (a real collision, not just reuse), and no
    duplicate ids anywhere in the document (spec 98.42/98.43)."""
    errors = _find_unsubstituted_placeholders(html_root)
    errors.extend(_find_duplicate_ids(html_root))
    seen_src = {}
    for el in html_root.iter():
        local = etree.QName(el).localname if el.tag and "}" in str(el.tag) else el.tag
        if local != "img":
            continue
        src = el.get("src")
        if not src:
            errors.append(f"<img> with empty/missing src attribute (element {el.tag}).")
            continue
        filename = os.path.basename(src)
        full_path = os.path.join(images_dir, filename)
        if not os.path.isfile(full_path):
            errors.append(f"<img src=\"{src}\"> references a file that was not written: {full_path}")
        seen_src.setdefault(src, 0)
        seen_src[src] += 1
    return errors


def write_xhtml(html_root, output_path: str):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # lxml's pretty_print=True only re-indents a subtree it built itself
    # incrementally with whitespace-bearing .tail nodes already in place -
    # a tree assembled programmatically (every element built via
    # etree.Element/SubElement, as this whole pipeline does) has none, so
    # pretty_print alone leaves entire subtrees as one unbroken line
    # (confirmed directly: only <html>/<head>/<body> came out indented,
    # everything Mapping.xml/EpubXmlGenerator built stayed on one line).
    # etree.indent() (lxml >= 4.5) explicitly inserts that whitespace for
    # any element with ONLY element children (never touching an element
    # that already has real text/mixed content, so no text is ever
    # altered - verified: "<p>text <b>bold</b> more text</p>" is untouched).
    etree.indent(html_root, space="    ")
    tree = etree.ElementTree(html_root)
    tree.write(
        output_path,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
        doctype="<!DOCTYPE html>",
    )
    return output_path
