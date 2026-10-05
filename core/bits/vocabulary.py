"""Zone tags of the two BITS tool profiles - what the user can tag a zone as
on the page - and where each one goes in the output:

    BITS  -> BITS 2.2 <book> (book-meta, front-matter, book-body/book-part,
             book-back)
    JATS  -> JATS 1.4 <article> (front/article-meta, body, back)

A zone tag is either handled by the XML generator itself (p, h1-h6, figure,
table, list, boxed-text, bibliography/reference, equation, pagenumber ...)
or emitted as an element of the same name and placed by
core.bits.structure (book-title, contrib, abstract, fn, index-entry ...).

Tag-button attributes:
    part_type   on a heading: the kind of book part / section it opens
                (chapter, part, preface, foreword, introduction, dedication,
                appendix, ack, notes, glossary, index, bibliography)
    @name       copied to the XML element as attribute `name`
    list_type   list numbering (the XML generator's own convention)

profile(kind) builds the profile dict the zoning GUI uses (tag buttons,
groups, colours); write_profiles() writes them to profiles/*.json."""
import json
import os

# (group, label, tag, attrs)
_COMMON_TEXT = [
    ("Text", "Paragraph", "p", {}),
    ("Text", "Extract / Quote", "disp-quote", {}),
    ("Text", "Epigraph", "epigraph", {}),
    ("Text", "Verse Line", "verse-line", {}),
    ("Text", "Speech", "speech", {}),
    ("Text", "Statement (Theorem...)", "statement", {}),
    ("Text", "Code / Preformat", "preformat", {}),
    ("Text", "Definition Term", "term", {}),
    ("Text", "Definition", "def", {}),
    ("Text", "Label", "label", {}),
    ("Text", "Page Number", "pagenumber", {}),
    ("Lists", "List Bullet", "list-bullet", {}),
    ("Lists", "List Number", "list", {"list_type": "order"}),
    ("Lists", "List Alpha", "list", {"list_type": "alpha-upper"}),
    ("Lists", "List Lower Alpha", "list", {"list_type": "alpha-lower"}),
    ("Lists", "List Roman", "list", {"list_type": "roman-lower"}),
    ("Lists", "List Simple", "list", {"list_type": "simple"}),
    ("Lists", "List Item", "list-item", {}),
    ("Figures & tables", "Figure", "figure", {"asset_kind": "figure"}),
    ("Figures & tables", "Caption", "caption", {}),
    ("Figures & tables", "Graphic", "graphic", {"asset_kind": "figure"}),
    ("Figures & tables", "Equation (display)", "equation", {"asset_kind": "equation"}),
    ("Figures & tables", "Table", "table", {}),
    ("Figures & tables", "Table Draw", "table", {}),
    ("Figures & tables", "Table Caption", "table-caption", {}),
    ("Figures & tables", "Table Wrap Foot", "table-wrap-foot", {}),
    ("Boxes", "Boxed Text", "boxed-text", {}),
    ("Boxes", "Boxed Text Start", "boxed-text-start", {}),
    ("Boxes", "Boxed Text End", "boxed-text-end", {}),
    ("Notes & references", "Footnote", "fn", {}),
    ("Notes & references", "Endnote", "en", {}),
    ("Notes & references", "Bibliography", "bibliography", {}),
    ("Notes & references", "Reference", "reference", {}),
]

BITS_TAGS = [
    ("Book metadata", "Book Title", "book-title", {}),
    ("Book metadata", "Book Subtitle", "book-subtitle", {}),
    ("Book metadata", "Author", "contrib", {"@contrib-type": "author"}),
    ("Book metadata", "Editor", "contrib", {"@contrib-type": "editor"}),
    ("Book metadata", "Affiliation", "aff", {}),
    ("Book metadata", "Series Title", "series-title", {}),
    ("Book metadata", "Publisher", "publisher-name", {}),
    ("Book metadata", "Publisher Place", "publisher-loc", {}),
    ("Book metadata", "ISBN", "isbn", {}),
    ("Book metadata", "Copyright", "copyright-statement", {}),
    ("Book metadata", "Edition", "edition", {}),
    ("Book parts", "Part Title", "h1", {"part_type": "part"}),
    ("Book parts", "Chapter Title", "h1", {"part_type": "chapter"}),
    ("Book parts", "Chapter Number / Label", "label", {}),
    ("Book parts", "Chapter Author", "chapter-contrib", {"@contrib-type": "author"}),
    ("Book parts", "Chapter Abstract", "abstract", {}),
    ("Book parts", "Keywords", "kwd", {}),
    ("Book parts", "Title Group", "title-group", {}),
    ("Book parts", "Title", "title", {}),
    ("Book parts", "Subtitle", "subtitle", {}),
    ("Front matter", "Dedication", "h1", {"part_type": "dedication"}),
    ("Front matter", "Foreword Title", "h1", {"part_type": "foreword"}),
    ("Front matter", "Preface Title", "h1", {"part_type": "preface"}),
    ("Front matter", "Introduction Title", "h1", {"part_type": "introduction"}),
    ("Front matter", "Acknowledgments Title", "h1", {"part_type": "ack"}),
    ("Front matter", "Contents Title", "h1", {"part_type": "toc"}),
    ("Front matter", "Front Matter Title", "h1", {"part_type": "front-matter-part"}),
    ("Headings", "Heading 2 (section)", "h2", {}),
    ("Headings", "Heading 3 (section)", "h3", {}),
    ("Headings", "Heading 4 (section)", "h4", {}),
    ("Headings", "Heading 5 (section)", "h5", {}),
    ("Headings", "Heading 6 (section)", "h6", {}),
] + _COMMON_TEXT + [
    ("Back matter", "Appendix Title", "h1", {"part_type": "appendix"}),
    ("Back matter", "Notes Title", "h1", {"part_type": "notes"}),
    ("Back matter", "Glossary Title", "h1", {"part_type": "glossary"}),
    ("Back matter", "Bibliography Title", "h1", {"part_type": "bibliography"}),
    ("Back matter", "Index Title", "h1", {"part_type": "index"}),
    ("Index", "Index Entry", "index-entry", {}),
    ("Index", "Index Subentry", "index-entry", {"@index-level": "2"}),
    ("Index", "Index Sub-subentry", "index-entry", {"@index-level": "3"}),
]

JATS_TAGS = [
    ("Article metadata", "Article Title", "article-title", {}),
    ("Article metadata", "Subtitle", "subtitle", {}),
    ("Article metadata", "Author", "contrib", {"@contrib-type": "author"}),
    ("Article metadata", "Affiliation", "aff", {}),
    ("Article metadata", "Corresponding Author", "corresp", {}),
    ("Article metadata", "Author Notes", "author-note", {}),
    ("Article metadata", "Abstract", "abstract", {}),
    ("Article metadata", "Keywords", "kwd", {}),
    ("Article metadata", "Received / Accepted dates", "history", {}),
    ("Article metadata", "Copyright", "copyright-statement", {}),
    ("Article metadata", "DOI", "article-doi", {}),
    ("Headings", "Heading 1", "h1", {}),
    ("Headings", "Heading 2", "h2", {}),
    ("Headings", "Heading 3", "h3", {}),
    ("Headings", "Heading 4", "h4", {}),
    ("Headings", "Heading 5", "h5", {}),
    ("Headings", "Heading 6", "h6", {}),
    ("Headings", "Title Group", "title-group", {}),
    ("Headings", "Title", "title", {}),
] + _COMMON_TEXT + [
    ("Back matter", "Acknowledgments Title", "h1", {"part_type": "ack"}),
    ("Back matter", "Appendix Title", "h1", {"part_type": "appendix"}),
    ("Back matter", "Notes Title", "h1", {"part_type": "notes"}),
    ("Back matter", "Glossary Title", "h1", {"part_type": "glossary"}),
    ("Back matter", "References Title", "h1", {"part_type": "bibliography"}),
]

_GROUP_COLORS = {
    "Book metadata": "#8E24AA", "Article metadata": "#8E24AA", "Book parts": "#3949AB", "Front matter": "#5E35B1",
    "Headings": "#1E88E5", "Text": "#43A047", "Lists": "#00897B", "Figures & tables": "#F4511E",
    "Boxes": "#6D4C41", "Notes & references": "#FB8C00", "Back matter": "#546E7A", "Index": "#C0CA33",
}
_TAG_COLORS = {"p": "#43A047", "pagenumber": "#9E9E9E", "h1": "#1E88E5", "h2": "#1E88E5", "h3": "#42A5F5",
               "h4": "#64B5F6", "h5": "#90CAF9", "h6": "#BBDEFB"}

# zone tags whose text may be combined by Merge Previous (core.constants.TEXT_MERGE_TAGS)
TEXT_TAGS = {"disp-quote", "epigraph", "verse-line", "speech", "statement", "preformat", "term", "def", "fn", "en",
             "abstract", "kwd", "book-title", "book-subtitle", "article-title", "contrib", "chapter-contrib", "aff",
             "copyright-statement", "index-entry", "corresp", "author-note", "history"}


def tags(kind: str) -> list:
    return BITS_TAGS if kind.upper() == "BITS" else JATS_TAGS


def profile(kind: str) -> dict:
    kind = kind.upper()
    rows = tags(kind)
    colors = {}
    for group, _label, tag, _attrs in rows:
        colors.setdefault(tag, _TAG_COLORS.get(tag, _GROUP_COLORS.get(group, "#607D8B")))
    groups = []                       # the tag panel's tabs: {"label": group, "first": first button label}
    for group, label, _tag, _attrs in rows:
        if not groups or groups[-1]["label"] != group:
            assert all(g["label"] != group for g in groups), f"group {group} is not contiguous"
            groups.append({"label": group, "first": label})
    return {
        "name": kind,
        "description": ("BITS 2.2 book (book-meta, front-matter, book-body / book-part, book-back)" if kind == "BITS"
                        else "JATS 1.4 journal article (front / article-meta, body, back)"),
        "output": "bits-book" if kind == "BITS" else "jats-article",
        "xml_enabled": True,
        "xhtml_enabled": False,
        "root_tag": "book" if kind == "BITS" else "article",
        "page_marker_tags": ["pagenumber"],
        "footnote_flow_tags": ["fn", "en"],
        "non_flow_tags": ["figure", "graphic", "equation"],
        # tag_label: internal (never output) - which button a zone was tagged with,
        # so Auto Tag learning can tell "Chapter Title" from "Part Title" (both h1)
        "tag_buttons": [{"label": label, "tag": tag, "attrs": dict(attrs, tag_label=label)}
                        for _g, label, tag, attrs in rows],
        "tag_groups": groups,
        "tag_colors": colors,
        "index_hierarchy_tags": {},
        "image_kinds": {},
        "component_types": [],
        "mapping_xml_path": "",
    }


def write_profiles(profiles_dir: str):
    for kind in ("BITS", "JATS"):
        path = os.path.join(profiles_dir, f"{kind.lower()}_profile.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile(kind), f, indent=2, ensure_ascii=False)
            f.write("\n")


if __name__ == "__main__":
    import sys
    from core.resource_path import resource_path
    write_profiles(sys.argv[1] if len(sys.argv) > 1 else resource_path("profiles"))
