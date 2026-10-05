"""Shared constants: tag definitions, GUI button map, colors, allowed-children rules."""

DPI = 200
DEFAULT_ZOOM = 1.0
DEFAULT_JPEG_QUALITY = 95
# Target print content width for a cropped Figure image (typical article/
# book text-column width) - a Figure zone's cropped image wider than this,
# at the configured Image DPI, is proportionally downscaled to fit; an
# image already narrower is left exactly as cropped, never enlarged.
DEFAULT_FIGURE_CONTENT_WIDTH_INCHES = 6.5

# Internal XML tags
TAG_TITLE_GROUP = "title-group"
TAG_TITLE = "title"
TAG_SUBTITLE = "subtitle"
TAG_H1 = "h1"
TAG_H2 = "h2"
TAG_H3 = "h3"
TAG_H4 = "h4"
TAG_H5 = "h5"
TAG_H6 = "h6"
TAG_P = "p"
TAG_FIGURE = "figure"
TAG_LABEL = "label"
TAG_CAPTION = "caption"
TAG_GRAPHIC = "graphic"
TAG_BOXED_TEXT = "boxed-text"
TAG_BOXED_TEXT_START = "boxed-text-start"
TAG_BOXED_TEXT_END = "boxed-text-end"
TAG_PAGENUMBER = "pagenumber"
TAG_LIST = "list"
TAG_LIST_ITEM = "list-item"
TAG_EQUATION = "equation"
TAG_BIBLIOGRAPHY = "bibliography"
TAG_REFERENCE = "reference"
TAG_TABLE_CAPTION = "table-caption"
TAG_TABLE = "table"
TAG_TABLE_WRAP_FOOT = "table-wrap-foot"
TAG_LIST_BULLET = "list-bullet"

HEADING_TAGS = (TAG_H1, TAG_H2, TAG_H3, TAG_H4, TAG_H5, TAG_H6)

# Tags that carry no continuous prose/reading-flow text of their own - a
# real, independent piece of page content (an image, embedded graphic, or
# standalone equation), never merged as text and never deleted, but also
# never a genuine "paragraph boundary" the way a heading or a different
# text tag is (spec: "An image, advertisement, figure, or other object
# between two text pieces must not automatically break the logical
# continuation"). This is the XML profile's own literal tag-name default;
# EPUB/CUPEPUB derive their own equivalent from each tag's "asset_kind"
# attribute (see core/profile_manager.py/core/cup_config.py), since their
# per-client tag names aren't fixed strings. Reused by ZoneManager's own
# pre-existing _refresh_text skip-list (unchanged behavior - this just
# gives that inline tuple a shared name) and by the continuation-aware
# search in ZoneManager._find_previous_in_reading_order/core.paragraph_merge.
NON_FLOW_TAGS = {"graphic", "figure", "equation"}

# Button label -> (internal tag, extra default attributes)
# Order matches the spec's left-panel button order.
TAG_BUTTONS = [
    ("Title Group", TAG_TITLE_GROUP, {}),
    ("Title", TAG_TITLE, {}),
    ("Subtitle", TAG_SUBTITLE, {}),
    ("Heading 1", TAG_H1, {}),
    ("Heading 2", TAG_H2, {}),
    ("Heading 3", TAG_H3, {}),
    ("Heading 4", TAG_H4, {}),
    ("Heading 5", TAG_H5, {}),
    ("Heading 6", TAG_H6, {}),
    ("Paragraph", TAG_P, {}),
    ("Figure", TAG_FIGURE, {}),
    ("Label", TAG_LABEL, {}),
    ("Caption", TAG_CAPTION, {}),
    ("Graphic", TAG_GRAPHIC, {}),
    ("Boxed Text", TAG_BOXED_TEXT, {}),
    ("Boxed Text Start", TAG_BOXED_TEXT_START, {}),
    ("Boxed Text End", TAG_BOXED_TEXT_END, {}),
    ("Page Number", TAG_PAGENUMBER, {}),
    ("List Number", TAG_LIST, {"list_type": "number"}),
    ("List Alpha", TAG_LIST, {"list_type": "alpha-upper"}),
    ("List Lower Alpha", TAG_LIST, {"list_type": "alpha-lower"}),
    ("List Simple", TAG_LIST, {"list_type": "simple"}),
    ("List Item", TAG_LIST_ITEM, {}),
    ("Equation", TAG_EQUATION, {}),
    ("Bibliography", TAG_BIBLIOGRAPHY, {}),
    ("Reference", TAG_REFERENCE, {}),
    ("Table Caption", TAG_TABLE_CAPTION, {}),
    ("Table", TAG_TABLE, {}),
    # "Table Draw" reuses the SAME tag as "Table" above - it is a second,
    # more discoverable entry point (draw a rectangle around a table)
    # into the exact same real geometry-based row/column/cell/rowspan/
    # colspan pipeline (core.table_extractor + core.xml_generator._zone_
    # table / core.epub_xml_generator._gen_table_zone), not a new/
    # competing table type. Replaces the removed "Table Generator"
    # toolbox button (that was a read-only preview requiring an EXISTING
    # table zone, never a way to create one).
    ("Table Draw", TAG_TABLE, {}),
    ("Table Wrap Foot", TAG_TABLE_WRAP_FOOT, {}),
    ("List Bullet", TAG_LIST_BULLET, {}),
]

# tag -> hex color (configurable via Settings dialog / project settings)
DEFAULT_TAG_COLORS = {
    TAG_TITLE_GROUP: "#607D8B",
    TAG_TITLE: "#3F51B5",
    TAG_SUBTITLE: "#5C6BC0",
    TAG_H1: "#B71C1C",
    TAG_H2: "#D32F2F",
    TAG_H3: "#E53935",
    TAG_H4: "#EF5350",
    TAG_H5: "#EF9A9A",
    TAG_H6: "#FFCDD2",
    TAG_P: "#1976D2",
    TAG_FIGURE: "#388E3C",
    TAG_LABEL: "#66BB6A",
    TAG_CAPTION: "#43A047",
    TAG_GRAPHIC: "#2E7D32",
    TAG_BOXED_TEXT: "#F57C00",
    TAG_BOXED_TEXT_START: "#EF6C00",
    TAG_BOXED_TEXT_END: "#BF360C",
    TAG_PAGENUMBER: "#9E9E9E",
    TAG_LIST: "#7B1FA2",
    TAG_LIST_ITEM: "#AB47BC",
    TAG_EQUATION: "#00838F",
    TAG_BIBLIOGRAPHY: "#4E342E",
    TAG_REFERENCE: "#795548",
    TAG_TABLE_CAPTION: "#00695C",
    TAG_TABLE: "#00897B",
    TAG_TABLE_WRAP_FOOT: "#004D40",
    TAG_LIST_BULLET: "#8E24AA",
}

LIST_TYPE_ATTR = {
    "number": "number",
    "alpha-upper": "alpha-upper",
    "alpha-lower": "alpha-lower",
    "simple": "simple",
}

# Allowed children per tag (used for soft validation, not enforced hard)
ALLOWED_CHILDREN = {
    TAG_TITLE_GROUP: [TAG_TITLE],
    TAG_FIGURE: [TAG_LABEL, TAG_CAPTION, TAG_GRAPHIC],
    TAG_CAPTION: [TAG_TITLE],
    TAG_BOXED_TEXT: [TAG_LABEL, TAG_CAPTION, TAG_P, TAG_LIST, TAG_FIGURE],
    TAG_LIST: [TAG_LIST_ITEM],
    TAG_LIST_ITEM: [TAG_P, TAG_LIST, TAG_BOXED_TEXT, TAG_FIGURE],
    TAG_BIBLIOGRAPHY: [TAG_REFERENCE],
    **{h: [TAG_TITLE, TAG_P, TAG_FIGURE, TAG_BOXED_TEXT, TAG_LIST, TAG_PAGENUMBER, TAG_EQUATION] + list(HEADING_TAGS)
       for h in HEADING_TAGS},
}

# For horizontal split: STRUCTURAL/container tags map to a semantically
# appropriate child tag when split (List -> list-item, Figure/Boxed Text/
# Title Group -> ambiguous, resolved via SPLIT_CHILD_CHOICES + a dialog).
# Every other tag is a CONTENT/leaf tag: splitting it produces more zones of
# the SAME tag (e.g. a Paragraph split into 3 pieces gives 3 Paragraph
# zones) - see zone_manager.split_zone, whose fallback is parent.tag itself,
# not this map. list-item is deliberately left as a content tag here (so
# splitting one produces more list-item-tagged pieces, matching the generic
# rule) - xml_generator.py's _zone_list_item flattens a list-item nested
# directly inside another list-item into an extra <p>, since raw
# <list-item><list-item> nesting isn't valid BITS.
SPLIT_CHILD_TAG = {
    TAG_LIST: TAG_LIST_ITEM,
    TAG_FIGURE: TAG_LABEL,        # ambiguous - dialog offers label/caption
    TAG_BOXED_TEXT: TAG_P,        # ambiguous - dialog offers more choices
    TAG_TITLE_GROUP: TAG_LABEL,   # ambiguous - dialog offers label/title
    TAG_BIBLIOGRAPHY: TAG_REFERENCE,
}

# Tags with ambiguous split children -> show "select child tag" dialog with these choices
SPLIT_CHILD_CHOICES = {
    TAG_FIGURE: [TAG_LABEL, TAG_CAPTION],
    TAG_BOXED_TEXT: [TAG_P, TAG_LIST, TAG_FIGURE, TAG_LABEL, TAG_CAPTION],
    TAG_TITLE_GROUP: [TAG_LABEL, TAG_TITLE],
}

# Tags where a Merge Previous chain's text can be meaningfully COMBINED into
# one output element (core/paragraph_merge.py + core/hierarchy.py). Merge
# Previous itself is generic - it applies to any tag - but combining "text"
# only makes sense for these; for image/container tags (figure, graphic,
# equation, list, boxed-text, title-group, ...) each zone in the chain is
# still generated normally/independently, just flagged+colored as merged.
TEXT_MERGE_TAGS = {TAG_P, TAG_TITLE, TAG_SUBTITLE, TAG_LABEL, TAG_CAPTION, TAG_LIST_ITEM, TAG_REFERENCE} | set(HEADING_TAGS)
# BITS / JATS text zones (core/bits/vocabulary.py TEXT_TAGS): a footnote,
# quote, abstract ... that runs across lines/pages and is zoned in pieces
# comes out as ONE element when the pieces are joined with Merge Previous.
TEXT_MERGE_TAGS |= {"disp-quote", "epigraph", "verse-line", "speech", "statement", "preformat", "term", "def",
                    "fn", "en", "abstract", "kwd", "book-title", "book-subtitle", "article-title", "contrib",
                    "chapter-contrib", "aff", "copyright-statement", "index-entry", "corresp", "author-note", "history"}

# Tags that are document BACK MATTER, never body content - a Bibliography
# zone (-> <ref-list>) or a standalone Reference zone/chain. Used by
# hierarchy.py to close any currently-open section before placing one of
# these (so it never ends up nested inside a <sec>), and by
# xml_generator.py to route it into <back> instead of <body>.
BACK_MATTER_TAGS = {TAG_BIBLIOGRAPHY, TAG_REFERENCE}


# Table-detection tuning (core/table_extractor.py) - all thresholds are in
# PDF points unless noted, and are the ones the spec explicitly calls out
# as "must be configurable". None of these affect any other zone type.
TABLE_COLUMN_X_TOLERANCE = 3.0       # px-equivalent slack when clustering column start positions
TABLE_ROW_Y_TOLERANCE = 2.0          # slack when grouping words into the same row band
TABLE_CELL_COORD_TOLERANCE = 2.0     # slack when testing whether a char/word falls inside a cell
TABLE_HEADER_BOLD_RATIO = 0.5        # fraction of a row's characters that must be bold to call it a header
TABLE_COLUMN_GAP_THRESHOLD = 15.0    # min horizontal gap between words to be treated as a column break
TABLE_COLUMN_MIN_OCCURRENCES = 2     # a candidate column start must recur across this many row bands to count
# Padding applied ONLY to the internal PDF text-extraction bbox used to
# re-extract a cell's formatted text (never to the zone's own drawn/stored
# bbox, never shown in the UI) - a cell's real content can legitimately
# start/end a few points outside the clustered column boundary (e.g. a bold
# header's glyph metrics differ slightly from the body rows the column
# average was computed from) or right at the user-drawn zone's own edge;
# without this, those characters are silently clipped (confirmed real bug -
# see core/table_extractor.py analyze_table). Kept small and configurable so
# it never reaches into a neighboring cell or an unrelated zone outside the
# table.
TABLE_EXTRACTION_BOUNDARY_TOLERANCE = 4.0

# Table-cell internal bullet-list detection (core/table_extractor.py) - a
# cell's own physical lines are only ever read as a <list> when there is
# real layout evidence, never merely because a line happens to start with a
# dash-like character. Deliberately a narrower character set than
# core.xml_generator's own _LIST_MARKER_PATTERNS["simple"] (which also
# matches plain "-"/"*") - that pattern only ever strips a marker from a
# zone the user already explicitly tagged as a List, where a false
# positive is impossible by construction; here detection itself decides
# whether something is a list at all, so it must stay conservative.
TABLE_LIST_BULLET_CHARS = "•●▪◦–"
TABLE_LIST_MIN_ITEMS = 2              # a cell needs at least this many marker lines to count as a list
TABLE_LIST_INDENT_TOLERANCE = 3.0     # pt slack when checking marker lines share the same left indentation


def heading_level(tag: str) -> int:
    if tag in HEADING_TAGS:
        return int(tag[1])
    return 0
