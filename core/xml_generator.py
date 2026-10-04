"""Generates ONE complete BITS-style XML document for the whole PDF (all pages),
never just the current page. Full pipeline: reading order -> hierarchy -> per-zone
XML element construction -> figure/equation asset cropping -> pretty-printed file.
"""
import os
import re

from lxml import etree

from core import reading_order, hierarchy, text_extractor, debug_log, table_extractor
from core.constants import TEXT_MERGE_TAGS, BACK_MATTER_TAGS, SPLIT_CHILD_TAG
from core.image_extractor import AssetManager

XLINK_NS = "http://www.w3.org/1999/xlink"


def sanitize_xml_text(text):
    """Strips characters illegal in XML 1.0 (e.g. a raw control character
    like \\x07 sometimes present in a PDF's extracted text - the exact cause
    of "lxml.etree.XMLSyntaxError: PCDATA invalid Char value 7") from text
    that is about to become XML output. Removes ONLY codepoints outside the
    XML 1.0 legal-character ranges - never guesses at what an illegal
    character "meant" or substitutes a replacement for it. This is a purely
    XML-output-safety step, applied here (at the point an assembled fragment
    is about to be parsed into the document) and nowhere upstream - the
    zone's own stored/extracted text (zone.text, and whatever
    text_extractor.py returned) is never touched, so it remains exact for
    any later Unicode correction/matching that inspects it."""
    if not text:
        return text
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r")
        or 0x20 <= ord(ch) <= 0xD7FF
        or 0xE000 <= ord(ch) <= 0xFFFD
        or 0x10000 <= ord(ch) <= 0x10FFFF
    )


_TITLE_INLINE_TAG_RE = re.compile(r"</?(?:bold|italic)>")

_BOLD_TAG_RE = re.compile(r"</?bold>")


def _strip_bold_wrapper(xml_fragment: str) -> str:
    """Removes ONLY <bold>/</bold> tags (unwrapping - keeping their inner
    text, and any OTHER inline formatting such as italic/sup/sub, exactly
    as before) - used for <th> header-cell content: a header cell's
    visual bold styling is already implied by <th> itself, so it must
    not ALSO be represented as a nested <bold> element (spec: "remove
    ONLY the redundant <bold> wrapper from <th>"). Deliberately narrower
    than normalize_title_text below (which strips ALL inline formatting,
    for <title>) - legitimate italic/sup/sub inside a header cell is
    still preserved, and <td> cells are never touched by this at all."""
    if not xml_fragment:
        return xml_fragment
    return _BOLD_TAG_RE.sub("", xml_fragment)


def _split_index_for_point(cutoffs, value):
    """How many of the sorted cutoff values `value` is at-or-past - used
    to place a coordinate into exactly one of len(cutoffs)+1 bands, the
    same "a boundary IS the boundary" rule table_extractor.py's manual
    grid already uses (a value exactly on a cutoff belongs to the band
    AFTER it, never split between two)."""
    idx = 0
    for cutoff in cutoffs:
        if value >= cutoff:
            idx += 1
        else:
            break
    return idx


def split_zone_into_regions(zone):
    """The single generic manual-split mechanism, used for EVERY tag (see
    _gen_zone_multi) except table (whose horizontal_splits/vertical_splits
    instead form a row x column grid consumed directly by
    table_extractor.analyze_table via _zone_table - a table remains ONE
    <table-wrap>, never multiple). Reads zone.bbox and
    zone.attributes["horizontal_splits"]/["vertical_splits"] only - never
    the zone's text, never any other zone - and returns a list of
    EffectiveRegion dicts tiling that bbox exactly, ordered top-to-bottom
    then left-to-right within a row. A zone with no manual splits on
    either axis returns exactly ONE region equal to the zone's own full,
    untouched bbox, so an unsplit zone's output is byte-for-byte the same
    as before this mechanism existed - splitting is purely additive."""
    x0, y0, x1, y1 = zone.bbox
    h_splits = sorted(zone.attributes.get("horizontal_splits", []) or [])
    v_splits = sorted(zone.attributes.get("vertical_splits", []) or [])
    y_edges = [y0] + h_splits + [y1]
    x_edges = [x0] + v_splits + [x1]
    regions = []
    idx = 0
    for r in range(len(y_edges) - 1):
        for c in range(len(x_edges) - 1):
            regions.append({
                "source_zone_id": zone.zone_id,
                "bbox": [x_edges[c], y_edges[r], x_edges[c + 1], y_edges[r + 1]],
                "row_index": r,
                "column_index": c,
                "split_index": idx,
                "tag": zone.tag,
            })
            idx += 1
    return regions
# PDF-layout/invisible spacing characters that should behave as a plain
# space inside a <title> - NBSP, the various fixed-width Unicode spaces
# (en/em/thin/hair/figure/punctuation/six-per-em/four-per-em/three-per-em
# space, U+2000-U+200A), zero-width space, narrow/medium mathematical
# space, ideographic space, and a stray BOM. None of these overlap with
# any letter used in real text (Greek, accented Latin, etc), so this can
# never touch meaningful content - only whitespace-class code points.
_TITLE_INVISIBLE_SPACE_RE = re.compile("[  -​  　﻿]")
_TITLE_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")


def normalize_title_text(xml_fragment: str) -> str:
    """Normalizes an already-tagged title fragment (as text_extractor.py
    produces it, e.g. "<bold>I.  NEURONAS...</bold>") for use inside a
    <title> element:
      - unwraps <bold>/<italic> (a <title> is never rendered with bold/
        italic formatting in this project's output - spec: "NO <bold>
        INSIDE <title>"), keeping their text content exactly where it was.
        <sup>/<sub> are deliberately NOT unwrapped here - a title
        containing a genuine subscript/superscript (e.g. "bloqueador
        H<sub>2</sub>") must keep that markup; only bold/italic styling
        is stripped.
      - collapses PDF layout/invisible whitespace (repeated spaces, NBSP,
        en/em/thin space, zero-width space, ...) to a single normal space
      - strips leading/trailing whitespace
    <break/> (multi-line title/subtitle line separation - an existing,
    already-tested feature, see _zone_title) is deliberately left
    untouched: it is structural, not an inline formatting wrapper, so
    normalizing a title never regresses it. Returns an XML FRAGMENT
    STRING (may still contain a real <break/> and/or <sup>/<sub>), meant
    to be handed to _parse_inline exactly like the un-normalized string
    was before - never a bare plain-text return, so those elements still
    parse correctly and any &amp;-style entities still parse correctly
    too. Applied ONLY at <title> construction sites - paragraphs,
    captions elsewhere, list content, and references keep their bold/
    italic exactly as before."""
    if not xml_fragment:
        return xml_fragment
    text = _TITLE_INLINE_TAG_RE.sub("", xml_fragment)
    text = _TITLE_INVISIBLE_SPACE_RE.sub(" ", text)
    text = _TITLE_WHITESPACE_COLLAPSE_RE.sub(" ", text)
    return text.strip()


_XML_ENTITY_RE = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos|#\\d+|#x[0-9A-Fa-f]+);)"
)


def _escape_raw_ampersands(xml_fragment: str) -> str:
    """Escape raw ampersands while preserving valid XML entities."""
    if not xml_fragment:
        return xml_fragment
    return _XML_ENTITY_RE.sub("&amp;", xml_fragment)


def _parse_inline(xml_fragment: str):
    """Parse a generated inline XML fragment safely.

    PDF text can contain a literal '&', which is illegal in XML unless it is
    escaped. Existing valid XML entities are preserved and are not escaped
    a second time.
    """
    safe_fragment = sanitize_xml_text(xml_fragment)
    safe_fragment = _escape_raw_ampersands(safe_fragment)
    return etree.fromstring(safe_fragment.encode("utf-8"))


_DIGIT_GAP_RE = re.compile(r"(?<=\d)\s+(?=\d)")


def _digits(text: str) -> str:
    """The zone's own page-number digits, closing a stray whitespace gap
    that sits directly BETWEEN two digits first (e.g. a genuine printed
    page number "208" that PDF text extraction reported as "20 8" - a
    confirmed real symptom of font kerning/spacing on some page-number
    glyphs reading as a word gap even though it's one single number).
    Without this, a plain `re.search(r"\\d+")` would silently stop at the
    first digit run and truncate "20 8" down to just "20", dropping the
    "8" entirely - this only ever closes a digit-to-digit gap, never a
    gap next to a non-digit character, so a genuine "p. 20"-style label
    is completely unaffected."""
    text = _DIGIT_GAP_RE.sub("", text or "")
    m = re.search(r"\d+", text)
    return m.group(0) if m else ""


# Leading figure-number label at the start of a Figure Caption zone's own
# text (e.g. "FIGURA 5-1 Escala...", "Figura 10-7.", "FIGURE 2-1:",
# "Fig. 3-4 ...", hyphen or en-dash between the two numbers) - "Figura"/
# "Figure"/"Fig.", matched case-insensitively so the very common
# ALL-CAPS PDF convention ("FIGURA 5-1") is recognized, not just the
# mixed-case "Figura" this previously required (a confirmed real bug: an
# all-caps label silently fell through to no match at all, leaving the
# identifier stuck inside <title> with no <label> ever produced). \s
# already matches Unicode whitespace (NBSP, en/em/thin space, etc) under
# Python 3's default str regex behavior - verified directly, no special-
# casing needed there. The optional trailing "." or ":" separator is
# matched OUTSIDE the captured group (in _FIGURE_LABEL_PLAIN_RE/
# _FIGURE_LABEL_WRAPPED_RE below), never inside it - the label itself
# must never include that punctuation (spec: <label>FIGURA 5-1</label>,
# not "FIGURA 5-1."), and the caption remainder must never start with a
# leftover separator either.
# _FIGURE_LABEL_WRAPPED_RE additionally matches the label when the PDF's own
# formatting made just that leading phrase bold/italic (a real, confirmed
# case: "<bold>Figura 10-7 </bold>Sección..." from extract_formatted_text) -
# tried first so the label comes out WITHOUT the wrapping tag (a bare
# "Figura 10-7" in <label>, not "<bold>Figura 10-7</bold>" inside it).
# Used only to split a caption's own leading label out of its body text when
# the figure has no separately-drawn Label zone of its own; see
# extract_figure_label_and_caption / XMLGenerator._extract_figure_label.
_FIGURE_LABEL_CORE = r"(?:Figura|Figure|Fig\.)\s+\d+[-–]\d+"
_FIGURE_LABEL_WRAPPED_RE = re.compile(
    rf"^<(bold|italic)>\s*({_FIGURE_LABEL_CORE})\s*[.:]?\s*</\1>", re.IGNORECASE)
_FIGURE_LABEL_PLAIN_RE = re.compile(rf"^({_FIGURE_LABEL_CORE})\s*[.:]?\s*", re.IGNORECASE)


def extract_figure_label_and_caption(text: str):
    """Pure text-parsing helper: splits a figure caption's raw extracted
    text into (label, caption) when it begins with a recognizable figure
    identifier (see _FIGURE_LABEL_CORE - "FIGURA 5-1", "Figure 5-1.",
    "FIG. 5-1", case-insensitive, ONLY at the very start of the text,
    never searched for mid-string). Returns (None, None) when no
    identifier is recognized there. The identifier is returned verbatim
    (original case/spacing preserved, never normalized), with its trailing
    "." or ":" separator (if any) stripped along with the whitespace
    around it; the caption text is everything after that, completely
    unaltered - no paraphrasing, no Unicode/punctuation/number changes."""
    if not text:
        return None, None
    m = _FIGURE_LABEL_WRAPPED_RE.match(text)
    if m:
        return m.group(2), text[m.end():].lstrip()
    m = _FIGURE_LABEL_PLAIN_RE.match(text)
    if m:
        return m.group(1), text[m.end():].lstrip()
    return None, None


# Leading table-number label at the start of a Table Caption zone's own
# text - same pattern/shape as _FIGURE_LABEL_CORE above (see its comments
# for the full rationale: case-insensitive so ALL-CAPS "TABLA 3-1" is
# recognized, trailing "." / ":" matched OUTSIDE the captured group so it
# never ends up in <label> or leaking into <title>), extended with "." as
# an additional valid separator BETWEEN the chapter/table numbers (spec
# explicitly lists "TABLA 3.1" alongside "TABLA 3-1"/"TABLA 3–1" as
# equivalent numbering formats) - Figure's own pattern is deliberately
# left untouched rather than retrofitted to accept this, since it was
# never reported for Figure and changing it isn't part of this feature.
#
# The keyword itself ("Tabla"/"Table") tolerates optional whitespace
# BETWEEN each of its own letters ("T\s*a\s*b\s*l\s*a") - a confirmed
# real-world PDF can render a table identifier with deliberate letter-
# spacing/tracking as a typographic style ("T A B L A 8-1"), which a
# plain "Tabla" literal would never match. This never widens what counts
# as a match beyond that literal 5-letter sequence, so it can't false-
# positive on unrelated text.
_TABLE_KEYWORD_CORE = r"(?:T\s*a\s*b\s*l\s*a|T\s*a\s*b\s*l\s*e)"
_TABLE_LABEL_CORE = rf"{_TABLE_KEYWORD_CORE}\s+\d+[-–.]\d+"
_TABLE_LABEL_WRAPPED_RE = re.compile(
    rf"^<(bold|italic)>\s*({_TABLE_LABEL_CORE})\s*[.:]?\s*</\1>", re.IGNORECASE)
_TABLE_LABEL_PLAIN_RE = re.compile(rf"^({_TABLE_LABEL_CORE})\s*[.:]?\s*", re.IGNORECASE)

# Fallback patterns for when the keyword and its number were extracted as
# two SEPARATE physical lines (e.g. a stylized, vertically-offset numeral
# badge) rather than appearing together on one line - see
# extract_table_label_and_caption_from_lines.
_TABLE_KEYWORD_ONLY_RE = re.compile(rf"^{_TABLE_KEYWORD_CORE}\s*[.:]?\s*$", re.IGNORECASE)
_TABLE_BARE_NUMBER_RE = re.compile(r"^(\d+[-–.]\d+)\s*[.:]?\s*$")
_LETTERSPACED_WORD_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")


def _collapse_letterspacing(s: str) -> str:
    """Collapses a run of 3+ single-character tokens separated by single
    spaces ("T A B L A") back into one word ("TABLA") - undoes deliberate
    PDF letter-spacing/tracking typography so the matched label text is
    output cleanly, never with the original inter-letter spaces baked in.
    Leaves normal multi-letter words (and the mandatory space before the
    trailing number) untouched, since those never match the pattern."""
    return _LETTERSPACED_WORD_RE.sub(lambda m: m.group(0).replace(" ", ""), s)


def extract_table_label_and_caption(text: str):
    """Table-caption counterpart of extract_figure_label_and_caption (see
    there for the full contract) - splits a Table Caption zone's raw
    extracted text into (label, caption) when it begins with a
    recognizable table identifier ("TABLA 3-1", "Table 3.1", "TABLA 3–1",
    letter-spaced "T A B L A 8-1", case-insensitive, only at the very
    start, never searched mid-string). Returns (None, None) when nothing
    is recognized. Verbatim label text (letter-spacing collapsed),
    trailing separator stripped, remainder completely unaltered."""
    if not text:
        return None, None
    m = _TABLE_LABEL_WRAPPED_RE.match(text)
    if m:
        return _collapse_letterspacing(m.group(2)).strip(), text[m.end():].lstrip()
    m = _TABLE_LABEL_PLAIN_RE.match(text)
    if m:
        return _collapse_letterspacing(m.group(1)).strip(), text[m.end():].lstrip()
    return None, None


def extract_table_label_and_caption_from_lines(lines):
    """Line-level counterpart of extract_table_label_and_caption, operating
    on a Table Caption zone's own physical lines
    (text_extractor.extract_lines' [(line_bbox, formatted_text), ...]
    output, already Y-sorted) instead of its single already-joined string.
    This is what makes label recognition robust against a real, observed
    PDF layout quirk a start-of-string regex alone cannot handle: the
    identifier's number ("8-1") occasionally gets extracted as its OWN
    physical line, separate from the "TABLA"/"TABLE" keyword's line (e.g.
    a stylized, vertically-offset numeral badge design) - and because
    MuPDF's own line ordering is Y-position based, that number's line can
    end up sorted to an unexpected position relative to the surrounding
    title lines (its own tight glyph bbox can legitimately sit at an
    unusual Y), landing it in the MIDDLE of the joined caption text
    instead of right after "TABLA". Since this function locates the label
    by PATTERN across every line (not by position), that sort quirk never
    causes the number to get stuck mid-title - it is always correctly
    identified and pulled out, wherever it sorted to.

    Letter-spaced PDF typography ("T A B L A 8-1", all on one physical
    line) is handled by extract_table_label_and_caption itself (tried
    first, per line), so the ordinary single-line case covers it too.

    Returns (label_or_None, caption_formatted_text) - same contract as
    extract_table_label_and_caption. Known, disclosed limitation: does
    NOT apply the zone's Hyphen Review overrides (keep_hyphen_at) to the
    reconstructed caption text - those boundary indices are computed by
    find_hyphen_candidates over the zone's FULL, unfiltered line
    sequence, and no longer line up once label-bearing lines are removed
    from the join; a table-caption zone with both a detected label AND a
    genuine line-break-hyphen needing the manual "keep" override is
    expected to be rare enough that automatic hyphen-join behavior
    (the same default every zone without a review override already
    gets) is an acceptable fallback here."""
    if not lines:
        return None, ""
    texts = [t for _, t in lines]

    # Case A: one physical line's own text already starts with the full
    # recognizable label (the ordinary case, including a letter-spaced
    # "T A B L A 8-1" on a single line).
    for i, text in enumerate(texts):
        label, remainder = extract_table_label_and_caption(text)
        if label:
            rest = list(texts)
            rest[i] = remainder
            caption = text_extractor.dehyphenate_join([t for t in rest if t])
            return label, caption

    # Case B: the keyword ("TABLA"/"TABLE") and its number ("8-1") were
    # extracted as two separate, otherwise-bare physical lines - search
    # for both independently, regardless of where each ended up in the
    # sorted sequence, and remove exactly those two lines.
    keyword_idx = keyword_word = None
    for i, text in enumerate(texts):
        plain = text_extractor.strip_tags_to_plain(text).strip()
        if _TABLE_KEYWORD_ONLY_RE.match(plain):
            keyword_idx = i
            keyword_word = re.sub(r"\s+", "", plain).rstrip(".:")
            break
    if keyword_idx is not None:
        for j, text in enumerate(texts):
            if j == keyword_idx:
                continue
            plain = text_extractor.strip_tags_to_plain(text).strip()
            m = _TABLE_BARE_NUMBER_RE.match(plain)
            if m:
                label = f"{keyword_word} {m.group(1)}"
                rest = [t for k, t in enumerate(texts) if k not in (keyword_idx, j)]
                caption = text_extractor.dehyphenate_join([t for t in rest if t])
                return label, caption

    # Nothing recognized anywhere - unchanged existing behavior, whole
    # zone text becomes the caption, no label invented.
    return None, text_extractor.dehyphenate_join([t for t in texts if t])


# Leading list-marker patterns, keyed by the GUI's list_type attribute. Matched
# against the plain start of a line's text to detect item boundaries and to
# strip the marker itself out of the generated <p> (BITS list-type="..." already
# implies the numbering/lettering, so it must not also be baked into the text).
_LIST_MARKER_PATTERNS = {
    "number": re.compile(r"^\s*\d+[.)]\s*"),
    "alpha-upper": re.compile(r"^\s*[A-Z][.)]\s*"),
    "alpha-lower": re.compile(r"^\s*[a-z][.)]\s*"),
    "simple": re.compile(r"^\s*[•\-*•●■⁃]\s*"),
}


def _strip_leading_marker(text: str, list_type: str) -> str:
    pat = _LIST_MARKER_PATTERNS.get(list_type)
    if not pat or not text:
        return text
    m = pat.match(text)
    return text[m.end():] if m else text


# ---------- Bibliography / Reference citation parsing ----------
# Turns one Reference zone's plain extracted text into structured BITS/JATS
# mixed-citation fields (person-group/name, article-title, source, year,
# volume, issue, fpage/lpage, edition, publisher-loc, publisher-name, doi,
# uri). Deliberately regex/heuristic-based, not a general bibliographic
# parser: it recognizes the same handful of citation shapes a Vancouver-style
# medical-journal bibliography actually uses (journal article, book, book
# with editors), and always falls back to preserving the ORIGINAL text
# unsplit into further structure rather than guessing/inventing a value - see
# _parse_citation's fallback branch. Works purely on PLAIN text (tags already
# stripped by the caller): patterns 10/11 of the spec require semantic
# reference tags to take priority over the PDF's own visual bold/italic on a
# journal name or book title, so no inline-formatting markup is preserved
# inside a reference's fields, unlike every other zone type in this file.
_NAME_TOKEN = r"[A-ZÀ-Ö][A-Za-zÀ-Öà-öø-ÿ'\-]*\s+[A-Z]{1,4}\.?"
_AUTHOR_LIST_RE = re.compile(
    r"^(?P<names>(?:" + _NAME_TOKEN + r")(?:,\s*(?:" + _NAME_TOKEN + r"))*)"
    r"(?:,?\s*(?P<etal>et al)|,?\s*(?P<eds>eds?))?"
    r"\.\s*(?P<rest>.+)$", re.S)
_JOURNAL_TAIL_RE = re.compile(
    r"(?P<year>\d{4});(?P<volume>\d+)(?:\((?P<issue>[^)]+)\))?"
    r":(?P<fpage>[A-Za-z]?\d+)-(?P<lpage>[A-Za-z]?\d+)\.?\s*$")
_BOOK_TAIL_RE = re.compile(
    r"(?:(?P<edition>\d+\w*\s+ed\.)\s*)?"
    r"(?P<pub_loc>[A-Za-z][A-Za-z ]*,\s*[A-Z]{2}):\s*"
    r"(?P<pub_name>[A-Za-z0-9 &'\-.]+?);\s*"
    r"(?P<year2>\d{4})\.?\s*$")
_DOI_RE = re.compile(r"\bdoi:\s*(\S+)", re.I)
_URL_RE = re.compile(r"https?://\S+")
_BIBLIOGRAPHY_TITLE_RE = re.compile(r"^\s*(Bibliograf[íi]a|References?|Bibliography)\.?\s*", re.I)


def _split_title_source(middle: str):
    middle = middle.strip()
    if middle.endswith("."):
        middle = middle[:-1]
    if ". " in middle:
        title, source = middle.rsplit(". ", 1)
        return title.strip(), source.strip()
    return (middle.strip() or None), None


def _parse_citation(text: str) -> dict:
    """Returns a dict of structured citation fields (see module docstring
    above) extracted from one reference's plain text. Never invented: every
    non-None field is a verbatim substring of `text` (only whitespace is
    normalized); anything the regexes can't confidently place lands in
    "article_title" (journal/book tail didn't match) or, if even the author
    segment doesn't match, the ENTIRE original text is preserved verbatim in
    "raw" for the caller to fall back to - a reference is never partially
    dropped just because its shape doesn't fit."""
    result = {"authors": [], "etal": False, "eds": False, "article_title": None,
              "source": None, "year": None, "volume": None, "issue": None,
              "fpage": None, "lpage": None, "edition": None,
              "publisher_loc": None, "publisher_name": None,
              "doi": None, "uri": None, "raw": None}
    plain = re.sub(r"\s+", " ", text or "").strip()
    if not plain:
        return result
    result["raw"] = plain

    doi_m = _DOI_RE.search(plain)
    if doi_m:
        result["doi"] = doi_m.group(1).rstrip(".")
        plain = (plain[:doi_m.start()] + plain[doi_m.end():])
    url_m = _URL_RE.search(plain)
    if url_m:
        result["uri"] = url_m.group(0).rstrip(".")
        plain = (plain[:url_m.start()] + plain[url_m.end():])
    plain = re.sub(r"\s+", " ", plain).strip()

    m = _AUTHOR_LIST_RE.match(plain)
    if not m:
        return result  # only "raw" is set - unrecognized shape, preserve verbatim
    result["etal"] = bool(m.group("etal"))
    result["eds"] = bool(m.group("eds"))
    for nm in re.split(r",\s*", m.group("names")):
        nm = nm.rstrip(".").strip()
        if not nm:
            continue
        surname, _, given = nm.rpartition(" ")
        if surname:
            result["authors"].append((surname.strip(), given.rstrip(".").strip()))
    rest = m.group("rest").strip()

    jm = _JOURNAL_TAIL_RE.search(rest)
    if jm:
        result["year"] = jm.group("year")
        result["volume"] = jm.group("volume")
        result["issue"] = jm.group("issue")
        result["fpage"] = jm.group("fpage")
        result["lpage"] = jm.group("lpage")
        result["article_title"], result["source"] = _split_title_source(rest[:jm.start()])
        return result

    bm = _BOOK_TAIL_RE.search(rest)
    if bm:
        result["edition"] = bm.group("edition")
        result["publisher_loc"] = bm.group("pub_loc")
        result["publisher_name"] = bm.group("pub_name").strip()
        result["year"] = bm.group("year2")
        title = rest[:bm.start()].strip()
        result["article_title"] = title[:-1].strip() if title.endswith(".") else (title or None)
        return result

    # Neither a recognizable journal nor book tail - keep the author segment
    # already parsed above, and preserve everything after it as the title
    # rather than fabricating source/year/pages that aren't actually there.
    result["article_title"] = rest.rstrip(".").strip() or None
    return result


def _split_bibliography_title(own_text: str):
    """If own_text (a Bibliography zone's own, non-reference-child text)
    starts with a recognizable bibliography heading word ("Bibliografía",
    "References", "Bibliography"), returns (title, remainder) with that
    leading word/line split off; otherwise (None, own_text) - the whole
    thing is left as reference content, never guessed at."""
    if not own_text:
        return None, own_text or ""
    m = _BIBLIOGRAPHY_TITLE_RE.match(own_text)
    if not m:
        return None, own_text
    return m.group(1), own_text[m.end():]


def _shrink_excluding(outer, exclude):
    """Clips outer (a bbox) to the side of exclude (another bbox) that leaves
    the larger remaining area - used so a label/pagenumber sub-region isn't
    also re-extracted as part of a sibling's 'own text'."""
    ox0, oy0, ox1, oy1 = outer
    ex0, ey0, ex1, ey1 = exclude
    if abs(ex0 - ox0) <= abs(ox1 - ex1):
        new_bbox = (max(ox0, ex1), oy0, ox1, oy1)
    else:
        new_bbox = (ox0, oy0, min(ox1, ex0), oy1)
    if new_bbox[2] <= new_bbox[0]:
        return None
    return new_bbox


def _bbox_excluding_all(outer, excludes):
    """Shrinks outer vertically to exclude the union of excludes (label/caption
    bboxes), clipping whichever side (top or bottom) they sit closer to - so
    the default figure image crop doesn't bake in label/caption text."""
    excludes = [e for e in excludes if e]
    if not excludes:
        return outer
    ox0, oy0, ox1, oy1 = outer
    ey0 = min(e[1] for e in excludes)
    ey1 = max(e[3] for e in excludes)
    if (ey0 - oy0) <= (oy1 - ey1):
        new_bbox = (ox0, max(oy0, ey1), ox1, oy1)
    else:
        new_bbox = (ox0, oy0, ox1, min(oy1, ey0))
    if new_bbox[3] <= new_bbox[1]:
        return outer
    return list(new_bbox)


class XMLGenerator:
    def __init__(self, zone_manager, pdf_document, assets_dir: str, prefix: str = "document",
                 jpeg_quality: int = 95, root_tag: str = "book", image_dpi: int = None,
                 remove_image_background: bool = False):
        self.zm = zone_manager
        self.pdf = pdf_document
        self.prefix = prefix
        self.root_tag = root_tag
        # image_dpi is the Settings > Image DPI value - independent of the PDF
        # viewer's fixed on-screen render DPI (core.constants.DPI). Falls back
        # to that same constant only when the caller doesn't specify one.
        assets_kwargs = {} if image_dpi is None else {"dpi": image_dpi}
        self.assets = AssetManager(assets_dir, prefix, jpeg_quality, remove_background=remove_image_background,
                                    **assets_kwargs)
        self.counters = {"section": 0, "boxed_text": 0, "caption": 0, "ref": 0, "table": 0}
        # figure_zone_id -> caption_zone_id for a Figure/Caption pair drawn as
        # two separate, NON-overlapping zones (the actual real-world zoning
        # workflow: Figure = image only, Caption = its own adjacent zone
        # below it) - such a Caption never becomes a geometric child of the
        # Figure via ZoneManager's containment-based auto-parenting, so
        # without this it would serialize as an independent top-level
        # <caption> sibling instead of nesting inside <fig>. Populated by
        # _collect_orphan_figure_captions() at the start of generate(), from
        # the already-built, unmodified doc_tree/zone graph - see there for
        # exactly how the association is inferred (immediate reading-order
        # adjacency), and _consumed_zone_ids for how such a
        # Caption is skipped as its own top-level entry once consumed here.
        self._orphan_caption_for_figure = {}
        self._consumed_zone_ids = set()
        # table_zone_id -> table_caption_zone_id, the Table equivalent of
        # _orphan_caption_for_figure above - see _collect_orphan_table_captions.
        # A Table Caption zone is ALWAYS a standalone zone (never a Table's
        # containment child, spec part 19), so unlike Figure this is the
        # ONLY association mechanism needed, no "already has a real child"
        # case to also check.
        self._table_caption_for_table = {}
        # table_zone_id -> table_wrap_foot_zone_id - mirrors
        # _table_caption_for_table but with the adjacency direction
        # FORWARD (the foot zone comes immediately AFTER the table, not
        # before - spec: "table immediately preceding the foot") - see
        # _collect_table_wrap_foot_zones. Also standalone-only, same as
        # Table Caption.
        self._table_wrap_foot_for_table = {}

    def _next_id(self, tag: str) -> str:
        """Sequential, never-reused id in the SAME {prefix}-{tag}{NNN}
        format already used for sec/fig/cap/tbl/boxed-text (see _gen_sec,
        _zone_figure, _zone_table) - extended here to also cover p/
        title/subtitle, which previously had no id at all. One counter
        per tag, incremented exactly once per emitted element regardless
        of which code path produced it - a flattened split piece
        (_gen_zone_multi), a generic region-split piece
        (_gen_zone_region_split), and a plain unsplit zone all resolve
        to exactly one _zone_<tag> call each, so numbering is always
        sequential and never reused, matching the existing counters'
        established behavior."""
        self.counters[tag] = self.counters.get(tag, 0) + 1
        return f"{self.prefix}-{tag}{self.counters[tag]:03d}"

    def _p_with_id(self, content: str):
        """Builds a <p id="..."> element from already-extracted inline
        content, or returns None for empty content (callers append
        nothing in that case - never an empty <p/>). The single place
        every <p> gets its sequential id, whatever container it ends up
        inside (Section, Boxed Text, List, List Item, List Bullet, a
        merge chain) - so numbering is always consistent and continuous
        regardless of which handler built it. Deliberately NOT used by
        _build_cell_list_element (a table cell's own internal bullet
        list) - table-cell content is part of the existing, already-
        correct Table pipeline and is left untouched."""
        if not content:
            return None
        el = _parse_inline(f"<p>{content}</p>")
        el.set("id", self._next_id("p"))
        return el

    def _hyphen_keep_at(self, zone):
        """The user's post-zoning Hyphen Normalization Review overrides for
        zone's own text (see core.text_extractor.find_all_hyphen_candidates
        / gui.dialogs.HyphenReviewDialog) - a set of boundary indices (see
        text_extractor.dehyphenate_join) where a detected line-break hyphen
        must be KEPT rather than auto-joined. Empty by default (no review
        performed, or the user left every candidate checked), which
        reproduces the prior automatic-join behavior exactly - so nothing
        changes for any project that never uses the review window. Only
        ever meaningful when the caller extracts zone.bbox UNMODIFIED: the
        review scans each zone's own full bbox, so a boundary index only
        lines up correctly with a call site using that exact same bbox - a
        shrunk/derived sub-bbox (excluding a nested Page Number or child
        zone's own region) can have a different extracted-line sequence,
        so those call sites deliberately do not apply this (see
        _list_item_own_text_from_bbox, the one place that conditionally
        does, only when its bbox argument equals zone.bbox)."""
        return set(zone.attributes.get("hyphen_keep_boundaries", []))

    def generate(self, output_path: str):
        debug_log.log("XML", f"generating {output_path} (prefix={self.prefix}, image_dpi={self.assets.dpi})")
        page_order = reading_order.compute_page_order(self.zm)
        doc_tree, boundary_issues = hierarchy.build_document_tree(self.zm, page_order)
        if boundary_issues:
            debug_log.log("XML", *boundary_issues)
        hierarchy.finalize_order(self.zm, doc_tree)

        self._orphan_caption_for_figure = {}
        self._consumed_zone_ids = set()
        self._table_caption_for_table = {}
        self._table_wrap_foot_for_table = {}
        self._collect_orphan_figure_captions(doc_tree["children"])
        self._collect_orphan_table_captions(doc_tree["children"])
        self._collect_table_wrap_foot_zones(doc_tree["children"])

        root_el = etree.Element(self.root_tag, nsmap={"xlink": XLINK_NS})
        body_nodes, back_nodes = [], []
        for n in doc_tree["children"]:
            (back_nodes if self._is_back_matter_node(n) else body_nodes).append(n)
        if back_nodes:
            # Only introduce <body>/<back> wrapping when the document
            # actually HAS back matter (a Bibliography or standalone
            # Reference zone) - every other document keeps its existing,
            # already-tested top-level structure (sec/p/... directly under
            # the root) completely unchanged. body_nodes/back_nodes each
            # preserve doc_tree["children"]'s original relative order, so
            # content already in reading order within each group stays
            # that way, with all back matter collected into ONE <back>
            # regardless of where it fell in the raw top-level stream.
            if body_nodes:
                body_el = etree.SubElement(root_el, "body")
                body_el.extend(self._gen_node_list(body_nodes))
            back_el = etree.SubElement(root_el, "back")
            back_el.extend(self._gen_node_list(back_nodes))
        else:
            root_el.extend(self._gen_node_list(doc_tree["children"]))

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        tree = etree.ElementTree(root_el)
        tree.write(output_path, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        debug_log.log("XML", f"wrote {output_path}, sections={self.counters['section']}, "
                              f"boxed_text={self.counters['boxed_text']}, assets={dict(self.assets.counters)}")
        return output_path, dict(self.counters), dict(self.assets.counters)

    def _is_back_matter_node(self, node) -> bool:
        """True for a top-level doc_tree node that belongs in <back> rather
        than <body> - a Bibliography zone (-> <ref-list>) or a standalone
        Reference zone/merge-chain (the normal case is a Reference inside a
        Bibliography zone, already consumed as that zone's own child and
        never a top-level node at all; a bare top-level Reference is the
        rarer case of one drawn without a Bibliography parent, still back
        matter either way). Every other node type (sec, boxed-text, a
        merged_p chain of any other tag, or a zone of any other tag) is
        body content, exactly as before this feature existed. Relies on
        hierarchy.py's close_for_back_matter() having already popped any
        open section before placing such a zone, so it always shows up as
        a genuine top-level doc_tree child here - see BACK_MATTER_TAGS in
        core/constants.py, the single shared source of truth for this tag
        set (also used by hierarchy.py, never duplicated)."""
        ntype = node.get("type")
        if ntype == "zone":
            return self.zm.zones[node["zone_id"]].tag in BACK_MATTER_TAGS
        if ntype == "merged_p":
            first_zid = node["zone_ids"][0]
            return self.zm.zones[first_zid].tag in BACK_MATTER_TAGS
        return False

    def _collect_orphan_figure_captions(self, nodes):
        """Read-only pre-pass over the ALREADY-assembled doc_tree (built by
        hierarchy.py, unmodified here) - finds a top-level Figure "zone" node
        immediately followed, within the SAME sibling list, by a Caption
        "zone" node that has no parent_id (i.e. ZoneManager's geometric
        containment never linked it to any figure, because the two were
        drawn as separate, non-overlapping zones - see zone_manager.py,
        untouched) and records that association in
        self._orphan_caption_for_figure for _zone_figure to consume, plus
        the caption's id in self._consumed_zone_ids so it is
        skipped as its own independent top-level entry (see
        _gen_zone_multi). A Figure that already has a REAL (containment-
        based) Caption child is left completely untouched - this only ever
        fires for the "drawn as two separate zones" case. Recurses into
        "sec" and "boxed-text" nodes' own children lists so the same
        adjacency check applies inside a section or boxed-text, never
        across their boundaries. Never mutates doc_tree, hierarchy.py, or
        any zone - purely builds a lookup table consulted later."""
        for i, node in enumerate(nodes):
            ntype = node.get("type")
            if ntype in ("sec", "boxed-text"):
                self._collect_orphan_figure_captions(node["children"])
            if ntype != "zone" or i + 1 >= len(nodes):
                continue
            zone = self.zm.zones[node["zone_id"]]
            if zone.tag != "figure":
                continue
            next_node = nodes[i + 1]
            if next_node.get("type") != "zone":
                continue
            next_zone = self.zm.zones[next_node["zone_id"]]
            if next_zone.tag != "caption" or next_zone.parent_id is not None:
                continue
            if any(c.tag == "caption" for c in self._sorted_children(zone)):
                continue
            self._orphan_caption_for_figure[zone.zone_id] = next_zone.zone_id
            self._consumed_zone_ids.add(next_zone.zone_id)

    def _collect_orphan_table_captions(self, nodes):
        """Table-Caption/Table counterpart of _collect_orphan_figure_captions
        above, adjacency REVERSED: a top-level "table-caption" zone node
        immediately followed by a top-level "table" zone node (the caption
        comes BEFORE the table - spec: "the caption occurring immediately
        before the table", opposite of Figure->Caption). Table Caption is
        ALWAYS a standalone zone (never a Table's containment child), so
        unlike Figure there is no "already has a real child" exception to
        check. Recurses into "sec"/"boxed-text" node children exactly like
        the figure version. Never mutates doc_tree, hierarchy.py, or any
        zone - purely builds a lookup table consulted later."""
        for i, node in enumerate(nodes):
            ntype = node.get("type")
            if ntype in ("sec", "boxed-text"):
                self._collect_orphan_table_captions(node["children"])
            if ntype != "zone" or i + 1 >= len(nodes):
                continue
            zone = self.zm.zones[node["zone_id"]]
            if zone.tag != "table-caption":
                continue
            next_node = nodes[i + 1]
            if next_node.get("type") != "zone":
                continue
            next_zone = self.zm.zones[next_node["zone_id"]]
            if next_zone.tag != "table":
                continue
            self._table_caption_for_table[next_zone.zone_id] = zone.zone_id
            self._consumed_zone_ids.add(zone.zone_id)

    def _collect_table_wrap_foot_zones(self, nodes):
        """Table/Table-Wrap-Foot counterpart of _collect_orphan_table_captions,
        adjacency FORWARD this time (the foot comes immediately AFTER the
        table, not before - spec: "table immediately preceding the foot",
        opposite direction from Table Caption->Table, same direction as
        Figure->Caption). A Table Wrap Foot zone is ALWAYS standalone
        (never a Table's containment child), so - like Table Caption -
        there is no "already has a real child" exception to check.
        Recurses into "sec"/"boxed-text" node children exactly like the
        other two orphan collectors. Never mutates doc_tree, hierarchy.py,
        or any zone - purely builds a lookup table consulted later."""
        for i, node in enumerate(nodes):
            ntype = node.get("type")
            if ntype in ("sec", "boxed-text"):
                self._collect_table_wrap_foot_zones(node["children"])
            if ntype != "zone" or i + 1 >= len(nodes):
                continue
            zone = self.zm.zones[node["zone_id"]]
            if zone.tag != "table":
                continue
            next_node = nodes[i + 1]
            if next_node.get("type") != "zone":
                continue
            next_zone = self.zm.zones[next_node["zone_id"]]
            if next_zone.tag != "table-wrap-foot":
                continue
            self._table_wrap_foot_for_table[zone.zone_id] = next_zone.zone_id
            self._consumed_zone_ids.add(next_zone.zone_id)

    # ---------- section / heading / merged-paragraph / boxed-text nodes ----------
    def _gen_node(self, node):
        if node["type"] == "sec":
            return self._gen_sec(node)
        if node["type"] == "boxed-text":
            return self._gen_boxed_text_node(node)
        if node["type"] == "merged_p":
            return self._gen_merged_p(node)
        return self._gen_zone(self.zm.zones[node["zone_id"]])

    def _gen_boxed_text_node(self, node):
        """A Boxed Text Start/End pair (reading-order boundary markers, not a
        geometric zone) - the markers themselves are never serialized, only
        their children. See _zone_boxed_text for the geometric-zone variant,
        which stays fully supported alongside this one."""
        self.counters["boxed_text"] += 1
        box_id = f"{self.prefix}-box{self.counters['boxed_text']:03d}"
        el = etree.Element("boxed-text", attrib={"id": box_id})
        el.extend(self._gen_node_list(node["children"]))
        return el

    def _gen_node_multi(self, node):
        """Same role as _gen_node, but for a slot that may legitimately expand
        into several sibling elements - see _gen_zone_multi for why."""
        if node["type"] in ("sec", "boxed-text", "merged_p"):
            el = self._gen_node(node)
            return el if isinstance(el, list) else [el]
        return self._gen_zone_multi(self.zm.zones[node["zone_id"]])

    def _list_run_zone_of(self, node):
        """The real Zone behind a doc_tree "zone"-type node, IF it starts
        or extends a list RUN that _gen_node_list must group - either a
        manually tagged "list-bullet" zone, or a "list-item" zone with NO
        parent_id (a standalone list-item the user zoned directly, with
        no containing "List" zone drawn around it - such an orphan must
        never surface as a bare top-level <list-item>, which isn't valid
        BITS/JATS outside a <list>). A list-item that DOES have a real
        parent_id is a genuine child of an explicit List zone - already
        correctly wrapped by that List's own <list> element via
        _zone_list/_gen_children_grouped - so it never reaches here.
        Returns None for every other node type/tag."""
        if node.get("type") != "zone":
            return None
        zone = self.zm.zones.get(node.get("zone_id"))
        if zone is None:
            return None
        if zone.tag == "list-bullet":
            return zone
        if zone.tag == "list-item" and zone.parent_id is None:
            return zone
        return None

    def _flatten_same_tag_split(self, zone):
        """If zone is a content/leaf Horizontal/Vertical Split parent (ALL
        its children are is_split pieces of itself - the exact condition
        _gen_zone_multi's own flatten logic checks, mirrored here), returns
        its LEAF split pieces instead (recursively, so a split-of-a-split
        still resolves to only its own leaves) - never the zone itself.
        This is critical for a manually split "list-bullet"/orphan-
        "list-item" zone: the split PARENT's own bbox still spans the WHOLE
        original, pre-split region, so treating it as one atomic item (as
        _build_bullet_list/_build_list_run naturally do for a plain,
        unsplit zone) would silently merge every split region's text into
        one combined item - the confirmed bug this fixes. Returns [zone]
        unchanged for a zone that was never split.

        Matches _gen_zone_multi's own reasoning exactly (see that
        function's docstring for the full rationale): keyed off THIS
        zone's own tag against SPLIT_CHILD_TAG, not off whether every
        individual piece's CURRENT tag still equals it - a user may retag
        any split piece independently afterward (spec: "FIX SPLIT-ZONE TAG
        INHERITANCE") without that piece silently falling out of this
        zone's own list run back into one re-merged, mistagged item."""
        children = zone.children
        if children:
            child_zones = [self.zm.zones[c] for c in children if c in self.zm.zones]
            if child_zones and zone.tag not in SPLIT_CHILD_TAG and all(
                    c.is_split and c.source_zone_id == zone.zone_id for c in child_zones):
                result = []
                for c in child_zones:
                    result.extend(self._flatten_same_tag_split(c))
                return result
        return [zone]

    def _log_list_debug(self, original_zone, leaves):
        """Temporary/debug-only diagnostic (core.debug_log, off by default -
        Settings > Debug logging) for the manual-split-into-list-items
        pipeline: prints the ORIGINAL zone (before flattening) and
        whether it was actually a split parent, then every resulting
        leaf zone's reading order/bbox/text - so it's possible to
        confirm, before XML is even written, that a manually split
        list-bullet/list-item zone's split children (not its own
        whole-region bbox) are what's about to be used."""
        if not debug_log.is_enabled():
            return
        is_split_parent = leaves != [original_zone]
        debug_log.log("LIST", f"LIST PARENT {original_zone.zone_id}",
                       f"    tag={original_zone.tag}",
                       f"    list_type={original_zone.attributes.get('list_type', 'bullet' if original_zone.tag == 'list-bullet' else '-')}",
                       f"    split_parent={is_split_parent}",
                       f"    children={len(leaves)}")
        for leaf in leaves:
            page = self.pdf.get_page(leaf.page)
            preview = text_extractor.extract_zone_formatted_text(page, leaf, self._hyphen_keep_at(leaf))
            debug_log.log("LIST", f"  CHILD {leaf.zone_id}",
                           f"      reading_order={leaf.serial}",
                           f"      bbox={[round(v, 1) for v in leaf.bbox]}",
                           f"      text={preview[:80]!r}")

    def _gen_node_list(self, nodes):
        """THE single place every doc_tree sibling stream is turned into
        XML elements - top-level document children (generate()), a
        <sec>'s own children, and a Boxed Text Start/End pair's children
        all funnel through here now, so this one rule applies
        everywhere uniformly: EVERY manually-tagged "list-bullet" zone,
        or orphan "list-item" zone (no explicit List parent), produces
        its OWN independent <list> - built ONLY from that one zone's own
        content (its split children if it's a Horizontal/Vertical Split
        parent, via _flatten_same_tag_split, or just itself if unsplit).
        Two SEPARATE manually zoned list-bullet zones are NEVER combined
        into one shared <list>, no matter how adjacent they are in the
        reading-order stream or how many split children each already
        has of its own - each manually created (and independently
        split) zone is its own authoritative list, always (spec: "one
        zone = one list, always; NEVER merge with another list zone").
        Every other node type is completely untouched, dispatched
        exactly as _gen_node_multi already did before this existed."""
        elements = []
        for node in nodes:
            zone = self._list_run_zone_of(node)
            if zone is not None:
                leaves = self._flatten_same_tag_split(zone)
                self._log_list_debug(zone, leaves)
                elements.append(self._build_list_run(leaves, zone.tag))
            else:
                elements.extend(self._gen_node_multi(node))
        return elements

    def _build_list_run(self, zones, run_tag):
        if run_tag == "list-bullet":
            return self._build_bullet_list(zones)
        # A run of orphan "list-item" zones (no explicit List parent) -
        # synthesize a <list list-type="simple"> wrapper (the only type
        # info available absent a real List zone to read list_type
        # from), each zone contributing its own <list-item> via the
        # SAME existing _zone_list_item handling a real List's children
        # already use (via _gen_zone_multi, so nested children and a
        # further-split list-item piece are still handled correctly,
        # never a simplified/duplicated version of that logic).
        el = etree.Element("list", attrib={"list-type": "simple", "id": self._next_id("list")})
        for zone in zones:
            el.extend(self._gen_zone_multi(zone))
        return el

    def _build_bullet_list(self, zones):
        """Builds ONE <list list-type="bullet"> from a run of manually
        tagged "list-bullet" zones (see _gen_node_list/_gen_zone_list).

        Each zone contributes its <list-item>(s) one of two ways:
          - If the zone has real List Item child zones drawn inside it
            (e.g. Table > List Bullet > List Item nesting, or any other
            container use of "list-bullet") - each child becomes its OWN
            independently-extracted <list-item>, via the exact same
            child-generation path a "list" zone's own List Item children
            already use (_gen_zone_list_grouped/_zone_list_item), so a
            List Bullet used as a CONTAINER behaves exactly like a List
            with list_type="bullet". This is what fixes empty
            <list-item/> output for a manually nested Table > List Bullet
            > List Item structure - the parent zone's own full bbox is
            never re-extracted as one flattened item once real children
            exist.
          - Otherwise (no children - the established, unmodified "each
            List Bullet zone IS one item" workflow): the zone contributes
            exactly one <list-item>, using that zone's OWN full extracted
            text directly from its own bbox (never a second global-page-
            text search, never split further by any in-text bullet
            marker - the manual zoning IS the item boundary). A leading
            bullet-marker character, if the PDF happens to include one
            inside the drawn zone, is stripped (never REQUIRED for the
            zone to count - a zone drawn without the glyph in its bbox
            still produces a real <list-item>, which is exactly what
            fixes the earlier confirmed content-loss bug: zones whose
            bbox didn't start with a recognized marker character were
            previously silently skipped entirely)."""
        el = etree.Element("list", attrib={"list-type": "bullet", "id": self._next_id("list")})
        for zone in zones:
            children = self._sorted_children(zone)
            if children:
                for child_el in self._gen_zone_list_grouped(children):
                    el.append(child_el)
                continue
            page = self.pdf.get_page(zone.page)
            content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
            content = _strip_leading_marker(content, "simple")
            p_el = self._p_with_id(content)
            if p_el is None:
                continue
            li = etree.Element("list-item")
            li.append(p_el)
            el.append(li)
        return el

    def _merged_chain_text(self, zone_ids, with_breaks=False):
        """Combines the independently-extracted text of every zone in a merge
        chain (Page-Number-flanked automatic continuation, OR an explicit
        Merge Previous chain - see paragraph_merge.py) into one string. Each
        zone's OWN bbox is used - never touched, never unioned.

        Hyphenated Line-Break Text Normalization (spec sections 13/14/20):
        the outer join across zone boundaries already dehyphenates a
        genuine line-break hyphen sitting exactly at a chain boundary by
        default (dehyphenate_join's own behavior, unchanged) - chain_keep_at
        (built below) is the user's explicit override for one of those
        specific boundaries (attributes["hyphen_keep_chain_boundary"] on
        the LATER zone, set only via gui.dialogs.HyphenReviewDialog's
        chain-boundary candidates - core.text_extractor.
        find_chain_hyphen_candidates), threaded through dehyphenate_join's
        existing keep_at parameter rather than a second join mechanism."""
        parts = []
        chain_keep_at = set()
        for zid in zone_ids:
            zone = self.zm.zones[zid]
            if zone.tag == "pagenumber":
                parts.append(self._pagenum_target_string(zone))
                continue
            page = self.pdf.get_page(zone.page)
            keep_at = self._hyphen_keep_at(zone)
            content = (text_extractor.extract_zone_formatted_text_with_breaks(page, zone, keep_at) if with_breaks
                       else text_extractor.extract_zone_formatted_text(page, zone, keep_at))
            if content:
                if parts and zone.attributes.get("hyphen_keep_chain_boundary"):
                    chain_keep_at.add(len(parts) - 1)  # the boundary immediately BEFORE this new part
                parts.append(content)
        join = text_extractor.dehyphenate_join_with_breaks if with_breaks else text_extractor.dehyphenate_join
        return join(parts, chain_keep_at)

    def _merged_chain_paragraphs(self, zone_ids):
        """Paragraph-aware counterpart of _merged_chain_text for a "p"
        chain: each zone contributes one string per real PDF paragraph it
        holds (_p_paragraph_contents); Merge Previous joins only the
        previous zone's LAST paragraph with this zone's FIRST one (A1 |
        A2+B1 | B2) - every other genuine paragraph boundary survives, none
        is flattened, duplicated or reordered. Same dehyphenation / chain-
        boundary hyphen override / page-number handling as
        _merged_chain_text."""
        paragraphs, keeps = [[]], [set()]
        for zid in zone_ids:
            zone = self.zm.zones[zid]
            if zone.tag == "pagenumber":
                paragraphs[-1].append(self._pagenum_target_string(zone))
                continue
            if zone.tag == "p" and not zone.children:
                # (children-free only: _p_paragraph_contents would render a
                # nested graphic child - an asset/counter side effect - and
                # _merged_chain_text has never included nested children)
                page = self.pdf.get_page(zone.page)
                contents = [c for c in text_extractor.extract_zone_paragraphs(
                    page, zone, [], self._hyphen_keep_at(zone)) if c]
            else:
                page = self.pdf.get_page(zone.page)
                text = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
                contents = [text] if text else []
            if not contents:
                continue
            if paragraphs[-1] and zone.attributes.get("hyphen_keep_chain_boundary"):
                keeps[-1].add(len(paragraphs[-1]) - 1)
            paragraphs[-1].append(contents[0])
            for extra in contents[1:]:
                paragraphs.append([extra])
                keeps.append(set())
        return [text_extractor.dehyphenate_join(parts, keep) for parts, keep in zip(paragraphs, keeps) if parts]

    def _gen_merged_p(self, node):
        """A merge chain collapsed by paragraph_merge.py: EITHER an automatic
        [p, pagenumber+, p, ...] page/column-boundary continuation, OR an
        explicit Merge Previous chain for a text-bearing tag (any of
        TEXT_MERGE_TAGS - see spec: Merge Previous is a generic zoning
        operation, not paragraph-specific; hierarchy.build_document_tree
        already filtered out non-text-bearing tags and headings before a
        node ever reaches here, so this only ever needs to combine text).
        Uses the chain's first zone's tag for the output element."""
        zone_ids = node["zone_ids"]
        first_tag = self.zm.zones[zone_ids[0]].tag
        if first_tag == "reference":
            # A Reference that continues across a page break (spec Pattern
            # 16, e.g. only a Page Number zone between the two halves) - the
            # chain's combined plain text becomes ONE <ref>, exactly like an
            # unsplit single-zone reference, never two.
            plain = text_extractor.strip_tags_to_plain(self._merged_chain_text(zone_ids))
            return self._build_ref_element(plain)
        if first_tag == "p":
            paragraphs = self._merged_chain_paragraphs(zone_ids)
            if len(paragraphs) > 1:
                # Several real paragraphs across the chain (spec: "Never
                # flatten unrelated paragraphs into one paragraph") - one
                # <p> each, ids from the same shared counter.
                elements = []
                for content in paragraphs:
                    el = _parse_inline(f"<p>{content}</p>") if content else etree.Element("p")
                    el.set("id", self._next_id("p"))
                    elements.append(el)
                return elements
        inner = self._merged_chain_text(zone_ids)
        if first_tag == "list-item":
            el = etree.Element("list-item")
            p_el = self._p_with_id(inner)
            if p_el is not None:
                el.append(p_el)
            return el
        el = _parse_inline(f"<{first_tag}>{inner}</{first_tag}>") if inner else etree.Element(first_tag)
        if first_tag in ("p", "title"):
            # Same id convention as an unmerged zone of this tag (_zone_p/
            # _zone_title) - a merge chain still produces exactly ONE
            # element, so it gets exactly one id from the SAME shared
            # counter, keeping numbering sequential across both merged
            # and unmerged paragraphs/titles. Subtitle deliberately gets
            # NO id (spec: "Do NOT generate an ID for <subtitle>"), same
            # as _zone_subtitle below.
            el.set("id", self._next_id(first_tag))
        return el

    def _gen_sec(self, node):
        self.counters["section"] += 1
        sec_id = f"{self.prefix}-sec{self.counters['section']:03d}"
        el = etree.Element("sec", attrib={"disp-level": str(node["level"]), "id": sec_id})
        merged_ids = node.get("merged_zone_ids")
        if merged_ids:
            # A heading merged via Merge Previous combines DISTINCT zones -
            # same "separate lines, not one wrapped sentence" situation as
            # Title, so <break/> between them, not a space.
            title_text = self._merged_chain_text(merged_ids, with_breaks=True)
        else:
            # A single (unmerged) heading's own possibly-multi-line text
            # keeps its prior plain-space wrapping behavior, unchanged.
            zone = self.zm.zones[node["zone_id"]]
            page = self.pdf.get_page(zone.page)
            title_text = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        title_text = normalize_title_text(title_text)
        el.append(_parse_inline(f"<title>{title_text}</title>") if title_text else etree.Element("title"))
        # MUST be _gen_node_list (multi-aware AND list-bullet-run-aware),
        # never the singular _gen_node: a <sec>'s own children are exactly
        # where a split zone (Horizontal/Vertical Split, or Ctrl+Shift+R/C
        # region splits) or a run of manually zoned List Bullet siblings
        # most commonly lives in a real document (any content under a
        # heading). _gen_node singular has no split-flatten logic at all,
        # so a split zone reached through here would silently fall
        # through to _gen_zone and re-extract its OWN whole pre-split
        # bbox as one combined element instead of emitting its split
        # pieces - a confirmed real bug, found via the exact "heading
        # followed by split paragraphs" scenario every real chapter has.
        el.extend(self._gen_node_list(node["children"]))
        return el

    # ---------- zone -> element ----------
    def _sorted_children(self, zone):
        ids = reading_order.sort_children_ids(self.zm, zone.zone_id)
        return [self.zm.zones[cid] for cid in ids]

    def _gen_children_grouped(self, zone):
        """Generates one XML element per group of self._sorted_children(zone) -
        see _gen_zone_list_grouped for the actual grouping logic, factored
        out so a table cell's own MATCHED SUBSET of a Table zone's children
        (see _match_table_cell_children) can reuse the exact same grouping
        rules instead of a second/duplicated version of them."""
        return self._gen_zone_list_grouped(self._sorted_children(zone))

    def _gen_zone_list_grouped(self, children):
        """Generates one XML element per group of an already-ordered zone
        list, where consecutive SIBLING zones linked by Merge Previous
        (attributes["merged_with_previous"]/["merge_target"]) are combined
        into ONE element instead of two - the same grouping concept
        core/paragraph_merge.py applies to the top-level document stream,
        here applied to a container's children (e.g. two merged List Item
        children of the same List) or a table cell's matched children (see
        _match_table_cell_children). Only combines TEXT_MERGE_TAGS - anything
        else is generated normally even if flagged merged."""
        groups = []
        for c in children:
            if (groups and c.tag in TEXT_MERGE_TAGS and c.attributes.get("merged_with_previous")
                    and c.attributes.get("merge_target") == groups[-1][-1].zone_id):
                groups[-1].append(c)
            else:
                groups.append([c])
        # A manually-tagged "list-bullet" child (never part of a merge-
        # chain group - "list-bullet" isn't in TEXT_MERGE_TAGS, so it
        # always forms its own length-1 group above) produces its OWN
        # independent <list> - built only from that zone's own split
        # children if any (_flatten_same_tag_split), NEVER combined with
        # another list-bullet sibling, exactly the same rule
        # _gen_node_list applies to a doc_tree sibling stream - see
        # _build_bullet_list.
        elems = []
        for group in groups:
            if len(group) == 1 and group[0].tag == "list-bullet":
                leaves = self._flatten_same_tag_split(group[0])
                self._log_list_debug(group[0], leaves)
                elems.append(self._build_bullet_list(leaves))
            elif len(group) == 1:
                elems.extend(self._gen_zone_multi(group[0]))
            else:
                merged = self._gen_merged_p({"zone_ids": [g.zone_id for g in group]})
                elems.extend(merged if isinstance(merged, list) else [merged])
        return elems

    def _gen_zone(self, zone):
        tag = zone.tag
        handler = getattr(self, f"_zone_{tag.replace('-', '_')}", None)
        if handler:
            return handler(zone)
        return self._zone_generic(zone)

    def _gen_zone_multi(self, zone):
        """Returns a list of XML elements for a zone in a generic sibling
        stream - almost always exactly one element. The exception: Horizontal
        Split preserves the ORIGINAL (pre-split) zone as a parent with the
        split pieces as its children (core.zone_manager.split_zone - "the
        parent zone is preserved, not replaced"). Whether that parent's own
        element is bypassed in favor of its pieces depends on whether the
        split was a content/leaf split or a STRUCTURAL split (see
        core.constants.SPLIT_CHILD_TAG):
          - content/leaf split (zone.tag not in SPLIT_CHILD_TAG - e.g. a
            Paragraph split into more Paragraph pieces): the parent must
            never regenerate its own whole, pre-split bbox text (that would
            silently duplicate/garble the pieces' text) and must never wrap
            them (<p><p>...</p></p> is invalid for a leaf tag) - so the
            pieces are emitted as flat siblings in the parent's place
            instead, EACH USING ITS OWN CURRENT TAG (spec: "FIX SPLIT-ZONE
            TAG INHERITANCE" - a user may retag any individual piece to
            anything after splitting, e.g. Paragraph/Heading/Paragraph -
            the pieces need not keep matching the parent's or each other's
            tag to still be flattened this way). Recurses so a split-of-a-
            split still flattens fully.
          - STRUCTURAL split (zone.tag IS a SPLIT_CHILD_TAG key - a List
            split into list-item pieces, a Figure split into label/caption
            pieces, a Boxed Text split into p pieces, a Title Group split
            into label/title pieces): the parent is a structural container
            whose own element must still be generated normally (<list>,
            <fig>, <boxed-text>, <title-group>) - its dedicated _zone_*
            handler already consumes such children correctly via
            _sorted_children/_gen_children_grouped, so this check must NOT
            fire for them.

            A prior version of this check required EVERY child's own tag to
            still equal the parent's tag (rather than testing the parent's
            OWN tag against SPLIT_CHILD_TAG) - correct only by coincidence
            for a content/leaf split where every piece still shares the
            parent's original tag, but as soon as a user retagged even ONE
            piece differently (e.g. Paragraph/Heading/Paragraph), the
            all(...) check failed for the WHOLE zone and fell through to
            regenerating the ORIGINAL PARENT's own whole bbox as one
            element - silently discarding every piece's own individually
            assigned tag and re-merging their separate text back into one
            (confirmed directly: reproduced exactly this "3 tagged pieces
            collapse into 1 merged element, losing the differing tag"
            output before this fix). An even older version tested only
            is_split/source_zone_id with no tag reasoning at all, which
            incorrectly flattened a split List/Figure/Boxed-Text/Title-
            Group too - losing the wrapper element and leaking bare
            children out as independent top-level XML nodes. Keying off
            the PARENT's own tag against SPLIT_CHILD_TAG (the same map
            zone_manager.split_zone itself already uses to decide a
            structural child's default tag) avoids both failure modes:
            content/leaf pieces always flatten regardless of what any
            individual piece is later retagged to, and a genuine
            structural split is recognized from the PARENT's own identity,
            never from whether any one piece's CURRENT tag happens to
            still match."""
        if zone.zone_id in self._consumed_zone_ids:
            # Single source-of-truth "already emitted by a parent/container"
            # check - currently populated only by
            # _collect_orphan_figure_captions (a Caption zone claimed by an
            # immediately-preceding Figure as its orphan caption, then
            # serialized INSIDE that <fig> by _zone_figure/
            # _build_caption_element), but deliberately generic so any
            # future "zone X's content is folded into zone Y's element"
            # case can reuse the same mechanism instead of inventing its
            # own ad hoc skip-flag.
            return []
        if zone.tag != "table" and (zone.attributes.get("horizontal_splits")
                                     or zone.attributes.get("vertical_splits")):
            # Generic manual-split regions (Ctrl+Shift+R/C Region Split
            # Mode) - a completely different mechanism from the
            # materialized-child-zones split handled just below (that one
            # comes from the OLDER "Horizontal Split" toolbar feature's
            # Confirm step; this one is computed purely from this zone's
            # own stored bbox+attributes, never creates new Zone objects).
            # Table is excluded here since its own splits form a row x
            # column grid inside ONE <table-wrap>, handled by _zone_table.
            return self._gen_zone_region_split(zone)
        children = self._sorted_children(zone)
        if children and zone.tag not in SPLIT_CHILD_TAG and all(
                c.is_split and c.source_zone_id == zone.zone_id for c in children):
            result = []
            for c in children:
                result.extend(self._gen_zone_multi(c))
            return result
        result = self._gen_zone(zone)
        # _zone_figure returns a LIST (not one <fig> element) when it finds
        # more than one independently-numbered image nested under a single
        # Figure-tagged zone - see _zone_figure's own docstring note.
        return result if isinstance(result, list) else [result]

    def _gen_zone_region_split(self, zone):
        """Generic manual-split consumption, identical for every non-table
        tag: one XML element per region from split_zone_into_regions,
        each produced by the SAME _zone_<tag> handler _gen_zone already
        dispatches to for an unsplit zone of this tag - the tag is never
        hard-coded here. Achieved by temporarily substituting the real
        zone's own bbox (and, for a container tag with real containment
        children, the subset of those children whose own bbox center
        falls inside this particular region - same center-point rule
        used everywhere else in this codebase) for the duration of one
        region's handler call, then restoring both to their real stored
        values before the next region (or anything else) touches this
        zone again - so the zone object itself, and every id it's
        referenced by elsewhere (doc_tree, orphan-caption/table lookups),
        stays completely valid throughout. A childless container (e.g. a
        Boxed Text with no geometric children) falls through to its
        handler's own existing "no children -> extract bbox text"
        branch, unchanged, once per region."""
        regions = split_zone_into_regions(zone)
        if len(regions) <= 1:
            return [self._gen_zone(zone)]
        orig_bbox = zone.bbox
        orig_children = zone.children
        h_splits = sorted(zone.attributes.get("horizontal_splits", []) or [])
        v_splits = sorted(zone.attributes.get("vertical_splits", []) or [])
        region_by_index = {(r["row_index"], r["column_index"]): r for r in regions}
        elements = []
        try:
            for region in regions:
                zone.bbox = region["bbox"]
                if orig_children:
                    kept = []
                    for cid in orig_children:
                        child = self.zm.zones.get(cid)
                        if child is None:
                            continue
                        cx = (child.bbox[0] + child.bbox[2]) / 2
                        cy = (child.bbox[1] + child.bbox[3]) / 2
                        row = _split_index_for_point(h_splits, cy)
                        col = _split_index_for_point(v_splits, cx)
                        if region_by_index.get((row, col)) is region:
                            kept.append(cid)
                    zone.children = kept
                elements.append(self._gen_zone(zone))
        finally:
            zone.bbox = orig_bbox
            zone.children = orig_children
        return elements

    def _zone_p(self, zone):
        # EXISTING JSON FORMATTING ANALYSIS FIX (spec: "EXISTING JSON
        # FORMATTING DETECTION ONLY") - this method used to check
        # zone.attributes.get("merged_formatted_text") first and use that
        # cached STRING as-is when present, bypassing live PDF extraction
        # entirely. That attribute is confirmed DEAD CODE for any merge
        # performed by the current app: core.zone_manager.py's
        # merge_with_previous() pops it immediately ("superseded (round-6)
        # cached-text approach") and nothing anywhere in core/ or gui/ ever
        # sets it again - a merge chain is instead handled entirely by
        # _gen_merged_p/_merged_chain_text (both already 100% LIVE,
        # extracting fresh from the PDF via extract_zone_formatted_text
        # every time), which _zone_p is never even called for. The ONLY
        # way this attribute could still be non-None today is a zone saved
        # by an OLDER, pre-round-6 version of this app whose cached string
        # was captured with WHATEVER bold/italic/sup/sub detection existed
        # BACK THEN - reading it here silently froze that zone's
        # formatting forever, never benefiting from any later detection
        # improvement even after reopening the project and regenerating.
        # Removing this read costs nothing (no current merge ever relies
        # on it) and means EVERY "p" zone - old JSON or new - always gets
        # fresh, up-to-date character-level formatting analysis from the
        # PDF at generation time, using its EXISTING, unchanged bbox
        # (never re-zoned, never resized, never moved) as the source of
        # truth for WHERE to read, while the PDF itself remains the source
        # of truth for WHAT formatting is actually there. The attribute is
        # left untouched in zone.attributes/the saved JSON either way -
        # this only changes what generation reads, never what's stored.
        contents = self._p_paragraph_contents(zone)
        elements = []
        for content in contents:
            el = _parse_inline(f"<p>{content}</p>") if content else etree.Element("p")
            el.set("id", self._next_id("p"))
            elements.append(el)
        if not elements:
            el = etree.Element("p")
            el.set("id", self._next_id("p"))
            return el
        return elements if len(elements) > 1 else elements[0]

    def _p_paragraph_contents(self, zone) -> list:
        """One content string per real PDF paragraph inside a "p" zone,
        nested inline children spliced in (moved verbatim from _zone_p) -
        shared by _zone_p and _merged_chain_paragraphs so a zone holding
        several paragraphs keeps its paragraph boundaries whether or not
        it is part of a Merge Previous chain."""
        page = self.pdf.get_page(zone.page)
        # Nested/inline zone support (spec: "NESTED ZONE / INLINE ZONE
        # SUPPORT") - a real, confirmed bug otherwise: hierarchy.py's
        # doc_tree walk only visits zones with parent_id is None, so a
        # geometrically-nested child of a Paragraph was silently dropped
        # in its entirety, never generated anywhere. Built the SAME way
        # regardless of whether this zone turns out to hold one paragraph
        # or several - text_extractor.extract_zone_paragraphs buckets
        # each (child_bbox, markup) pair into whichever detected
        # paragraph its own bbox falls into.
        children = []
        for child in self._sorted_children(zone):
            if child.tag == "graphic":
                child_el = self._zone_graphic(child)
                markup = etree.tostring(child_el, encoding="unicode") if child_el is not None else ""
            else:
                markup = text_extractor.extract_zone_formatted_text(page, child, self._hyphen_keep_at(child))
            if markup:
                children.append((child.bbox, markup))
        # MULTIPLE PDF PARAGRAPHS INSIDE ONE SAVED ZONE (spec: "FAST FIX -
        # MULTIPLE PARAGRAPHS PER ZONE") - a single drawn zoning box can
        # legitimately span several real PDF paragraphs (blank-line-
        # separated or first-line-indented); extract_zone_paragraphs
        # returns exactly one string for the overwhelming common single-
        # paragraph case (identical to the old extract_zone_formatted_
        # text/_nested_inline_content_for_paragraph call this replaced),
        # or one string per genuinely detected paragraph otherwise - the
        # zone's own saved bbox/children/reading-order are only ever read
        # here, never modified.
        return text_extractor.extract_zone_paragraphs(page, zone, children, self._hyphen_keep_at(zone))

    def _zone_title(self, zone):
        # A Title zone's separate physical lines (e.g. title line + author
        # byline) are joined with <break/>, not a space - unlike a Paragraph,
        # where wrapped lines flow together as one sentence.
        page = self.pdf.get_page(zone.page)
        content = normalize_title_text(
            text_extractor.extract_zone_formatted_text_with_breaks(page, zone, self._hyphen_keep_at(zone)))
        el = _parse_inline(f"<title>{content}</title>") if content else etree.Element("title")
        el.set("id", self._next_id("title"))
        return el

    def _zone_subtitle(self, zone):
        # Same extraction shape _zone_generic would have used for this
        # tag before this handler existed (plain extract_formatted_text,
        # no break-joining/normalize like Title). Deliberately NO id -
        # <subtitle> must never receive one (unlike p/title), so this
        # handler exists purely to keep that behavior explicit and
        # future-proof rather than relying on _zone_generic's fallback.
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        return _parse_inline(f"<subtitle>{content}</subtitle>") if content else etree.Element("subtitle")

    def _zone_label(self, zone):
        page = self.pdf.get_page(zone.page)
        children = self._sorted_children(zone)
        if not children:
            content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
            return _parse_inline(f"<label>{content}</label>") if content else etree.Element("label")
        # e.g. a Page Number zoned inside a running-header Label (title-group
        # case): emit the target inline, then the label's own remaining text
        # (excluding the page-number's own region, so its digits aren't
        # duplicated into the label text).
        parts = []
        text_bbox = zone.bbox
        for c in children:
            if c.tag == "pagenumber":
                parts.append(self._pagenum_target_string(c))
                shrunk = _shrink_excluding(text_bbox, c.bbox)
                if shrunk:
                    text_bbox = shrunk
            else:
                parts.append(etree.tostring(self._gen_zone(c), encoding="unicode"))
        own_text = text_extractor.extract_formatted_text(page, text_bbox)
        if own_text:
            parts.append(own_text)
        inner = text_extractor.dehyphenate_join(parts)
        return _parse_inline(f"<label>{inner}</label>") if inner else etree.Element("label")

    def _zone_title_group(self, zone):
        el = etree.Element("title-group")
        children = self._sorted_children(zone)
        if children:
            for child_el in self._gen_children_grouped(zone):
                el.append(child_el)
            return el
        # No explicit Label/Title child zones drawn inside this Title Group
        # (the whole visual block - e.g. a chapter-number badge plus its
        # heading text - was tagged as ONE zone): auto-split its own physical
        # lines instead of emitting an empty <title-group/>. The first
        # physical PDF line is the label (chapter number / "Capitulo N"
        # token); any remaining lines are the title, joined with <break/> per
        # the same convention a standalone Title zone uses (see _zone_title).
        # (This is specific to Title Group, unlike List - see _zone_list,
        # where an unsplit zone is always exactly ONE list-item.)
        page = self.pdf.get_page(zone.page)
        lines = [t for _, t in text_extractor.extract_lines(page, zone.bbox) if t]
        if not lines:
            return el
        if len(lines) == 1:
            el.append(_parse_inline(f"<title>{normalize_title_text(lines[0])}</title>"))
        else:
            el.append(_parse_inline(f"<label>{lines[0]}</label>"))
            title_text = normalize_title_text(text_extractor.dehyphenate_join_with_breaks(lines[1:]))
            el.append(_parse_inline(f"<title>{title_text}</title>"))
        return el

    def _build_caption_element(self, zone, cap_id=None):
        el = etree.Element("caption", attrib=({"id": cap_id} if cap_id else {}))
        children = self._sorted_children(zone)
        if children and all(c.is_split and c.source_zone_id == zone.zone_id and c.tag == zone.tag
                             for c in children):
            # Same-tag Horizontal Split (e.g. the user split a standalone
            # Caption zone at a PDF line boundary, or further split an
            # already figure-owned Caption child) - the pieces are text
            # CONTINUATIONS of ONE logical caption, never separate captions.
            # A <caption> can only ever hold one <title>, so this can't
            # simply delegate to _gen_zone_multi's own same-tag flattening
            # (that produces N sibling elements, not one combined field) -
            # combine the pieces' text the same way a Merge Previous/
            # page-number-flanked chain is combined (see _merged_chain_text)
            # into ONE <title>. Without this, each piece independently
            # dispatched through _zone_caption, producing nested
            # <caption><caption>...</caption><caption>...</caption></caption>
            # with a duplicate id - a confirmed real bug.
            content = normalize_title_text(self._merged_chain_text([c.zone_id for c in children]))
            if content:
                el.append(_parse_inline(f"<title>{content}</title>"))
            return el
        if children:
            for child_el in self._gen_children_grouped(zone):
                el.append(child_el)
        else:
            page = self.pdf.get_page(zone.page)
            content = normalize_title_text(
                text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone)))
            if content:
                el.append(_parse_inline(f"<title>{content}</title>"))
        return el

    def _zone_caption(self, zone):
        # Standalone caption (not part of a Figure, e.g. inside a Boxed Text) -
        # gets its own independently-counted id, same convention as a
        # figure-owned caption but not tied to any fig number.
        self.counters["caption"] += 1
        cap_id = f"{self.prefix}-cap{self.counters['caption']:03d}"
        return self._build_caption_element(zone, cap_id)

    def _extract_figure_label(self, caption_zone):
        """If caption_zone is a plain leaf zone (no sub-zoning of its own),
        extracts its formatted text and delegates the actual identifier
        recognition to extract_figure_label_and_caption (the pure,
        zone-independent parsing helper - see there for the exact rules).
        Returns (None, None) when the zone has its own child zones (caller
        keeps recursing via _build_caption_element as before, since a
        sub-zoned caption already has an explicit structure to preserve)
        or when there's no text/no recognizable identifier (caller then
        falls back to the existing, unchanged behavior: full text goes
        into <title>, no <label> is invented). Only ever consulted when
        the figure has no separately-drawn Label zone - an explicit Label
        zone always wins and this is never called in that case, so
        existing figures with a real Label zone are completely unaffected."""
        if self._sorted_children(caption_zone):
            return None, None
        page = self.pdf.get_page(caption_zone.page)
        content = text_extractor.extract_zone_formatted_text(page, caption_zone, self._hyphen_keep_at(caption_zone))
        return extract_figure_label_and_caption(content)

    def _zone_figure(self, zone):
        children = self._sorted_children(zone)
        graphic_indices = [i for i, c in enumerate(children) if c.tag == "graphic"]
        if len(graphic_indices) > 1:
            # Real, confirmed structural defect (spec: "no content loss" /
            # "multiple independently numbered figures must not be
            # incorrectly combined into one logical figure"): this zone's
            # containment children (parent_id, assigned outside this file)
            # hold more than one independently-numbered image. The
            # single-pairing code below picks only the FIRST label/caption/
            # graphic via next() - left as-is, it would silently drop every
            # other pairing's content. Instead, split into one independent
            # <fig> per graphic: a "label" child attaches FORWARD to the
            # next graphic (a label conventionally precedes its image),
            # every other non-graphic child (a caption, typically)
            # attaches BACKWARD to the nearest preceding graphic (a caption
            # conventionally follows its image); anything before the very
            # first graphic falls back to that first graphic's group
            # rather than being dropped.
            debug_log.log("XML", f"{zone.zone_id}: {len(graphic_indices)} independently-numbered "
                                  f"images found - generating {len(graphic_indices)} separate <fig> elements")
            group_of_index = {idx: g for g, idx in enumerate(graphic_indices)}
            groups = [[] for _ in graphic_indices]
            for i, c in enumerate(children):
                if i in group_of_index:
                    groups[group_of_index[i]].append(c)
                elif c.tag == "label":
                    nxt = next((idx for idx in graphic_indices if idx > i), None)
                    groups[group_of_index[nxt] if nxt is not None else len(groups) - 1].append(c)
                else:
                    prv = None
                    for idx in graphic_indices:
                        if idx < i:
                            prv = idx
                        else:
                            break
                    groups[group_of_index[prv] if prv is not None else 0].append(c)
            return [self._build_figure_element(zone, group, allow_orphan_caption=False) for group in groups]
        return self._build_figure_element(zone, children, allow_orphan_caption=True)

    def _build_figure_element(self, zone, children, allow_orphan_caption):
        label_zone = next((c for c in children if c.tag == "label"), None)
        caption_zone = next((c for c in children if c.tag == "caption"), None)
        graphic_zone = next((c for c in children if c.tag == "graphic"), None)
        if caption_zone is None and allow_orphan_caption:
            # No geometric (containment) Caption child - check whether this
            # Figure was matched with an adjacent, separately-drawn Caption
            # zone by _collect_orphan_figure_captions (the actual real-world
            # zoning workflow: Figure = image only, Caption = its own
            # non-overlapping zone right after it in reading order). Only
            # consulted for the single-pairing case above - this lookup is
            # keyed by the OUTER zone's id, so applying it to more than one
            # synthesized <fig> from the same zone would duplicate the same
            # orphan caption across multiple figures.
            orphan_cap_id = self._orphan_caption_for_figure.get(zone.zone_id)
            if orphan_cap_id:
                caption_zone = self.zm.zones.get(orphan_cap_id)
        if graphic_zone:
            img_bbox = graphic_zone.bbox
        else:
            # No explicit Graphic zone: crop the figure region minus any
            # label/caption sub-zones so their text isn't baked into the image.
            exclude = [c.bbox for c in (label_zone, caption_zone) if c is not None]
            img_bbox = _bbox_excluding_all(zone.bbox, exclude)
        filename = self.assets.save_figure_asset(self.pdf, zone.page, img_bbox)
        # fig/caption/graphic share ONE number per figure (fig003/cap003/gr003),
        # matching the project's BITS convention - not three independent counters.
        fig_num = self.assets.counters["figure"]
        fig_id = f"{self.prefix}-fig{fig_num:03d}"
        el = etree.Element("fig", attrib={"id": fig_id})

        extracted_label, caption_remainder = None, None
        if label_zone is None and caption_zone is not None:
            extracted_label, caption_remainder = self._extract_figure_label(caption_zone)

        if label_zone is not None:
            el.append(self._gen_zone(label_zone))
        elif extracted_label is not None:
            el.append(_parse_inline(f"<label>{extracted_label}</label>"))

        if caption_zone is not None:
            cap_id = f"{self.prefix}-cap{fig_num:03d}"
            if extracted_label is not None:
                # The label was split out of the caption's own text above -
                # build <caption> from the REMAINDER only, never the
                # original full text, so the label is never duplicated
                # inside <title> (see TEST B/C).
                cap_el = etree.Element("caption", attrib={"id": cap_id})
                remainder_text = normalize_title_text(caption_remainder)
                if remainder_text:
                    cap_el.append(_parse_inline(f"<title>{remainder_text}</title>"))
                self._append_caption_once(el, cap_el)
            else:
                self._append_caption_once(el, self._build_caption_element(caption_zone, cap_id))
        graphic_el = etree.Element("graphic", attrib={"id": f"{self.prefix}-gr{fig_num:03d}"})
        graphic_el.set(f"{{{XLINK_NS}}}href", filename)
        el.append(graphic_el)
        return el

    def _append_caption_once(self, fig_el, cap_el):
        """Defensive guard (spec: "before appending a caption, check
        whether that figure already has a caption element") - a <fig> may
        only ever contain ONE <caption>. _zone_figure's own logic already
        only ever builds one caption element per call, but this makes that
        invariant explicit and future-proof: if <fig> somehow already has
        a <caption> child by the time this runs, the new one is dropped
        (logged, never silently duplicated) instead of being appended
        alongside it."""
        if fig_el.find("caption") is not None:
            debug_log.log("XML", f"{fig_el.get('id')}: duplicate <caption> suppressed")
            return
        fig_el.append(cap_el)

    def _zone_graphic(self, zone):
        # orphan graphic zone with no figure parent
        filename = self.assets.save_figure_asset(self.pdf, zone.page, zone.bbox)
        gr_num = self.assets.counters["figure"]
        graphic_el = etree.Element("graphic", attrib={"id": f"{self.prefix}-gr{gr_num:03d}"})
        graphic_el.set(f"{{{XLINK_NS}}}href", filename)
        return graphic_el

    def _zone_boxed_text(self, zone):
        self.counters["boxed_text"] += 1
        box_id = f"{self.prefix}-box{self.counters['boxed_text']:03d}"
        el = etree.Element("boxed-text", attrib={"id": box_id})
        children = self._sorted_children(zone)
        if children:
            for child_el in self._gen_children_grouped(zone):
                el.append(child_el)
        else:
            page = self.pdf.get_page(zone.page)
            content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
            p_el = self._p_with_id(content)
            if p_el is not None:
                el.append(p_el)
        return el

    def _pagenum_target_string(self, zone) -> str:
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_plain_text(page, zone)
        num = _digits(content)
        target_id = f"page{num}" if num else f"page_unknown_{zone.zone_id}"
        return f'<target target-type="pagenum" id="{target_id}"/>'

    def _zone_pagenumber(self, zone):
        return _parse_inline(self._pagenum_target_string(zone))

    def _zone_list(self, zone):
        list_type = zone.attributes.get("list_type", "simple")
        # One shared, global, sequential counter for id="...-listNNN"
        # across EVERY list-type value (bullet/simple/number/alpha/...) -
        # never a separate counter per type, same established pattern as
        # every other counted tag (_next_id).
        el = etree.Element("list", attrib={"list-type": list_type, "id": self._next_id("list")})
        children = self._sorted_children(zone)
        if children:
            for child_el in self._gen_children_grouped(zone):
                el.append(child_el)
            return el
        # No explicit List Item child zones (user didn't Horizontal Split this
        # list): the number of <list-item> elements comes ONLY from actual
        # Horizontal Split structure, never from analyzing individual PDF
        # lines or marker characters within one unsplit zone - so the whole
        # zone is exactly ONE list-item, holding its full text as one
        # flowing <p> (same extraction as a Paragraph zone - see _zone_p).
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        li = etree.Element("list-item")
        p_el = self._p_with_id(content)
        if p_el is not None:
            li.append(p_el)
        el.append(li)
        return el

    def _zone_list_item(self, zone):
        el = etree.Element("list-item")
        children = self._sorted_children(zone)
        if children:
            # A list-item with children (e.g. a nested sub-list) may still
            # have its OWN leading text above/around those children (e.g.
            # "Definir los compartimentos" before a nested alpha-lower list) -
            # extract that from the zone's own bbox MINUS the children's
            # bboxes so the nested content's text isn't duplicated into it,
            # and emit it as this list-item's own <p> before its children.
            own_bbox = _bbox_excluding_all(zone.bbox, [c.bbox for c in children])
            own_text = self._list_item_own_text_from_bbox(zone, own_bbox)
            p_el = self._p_with_id(own_text)
            if p_el is not None:
                el.append(p_el)
            for c in children:
                if c.tag == "list-item":
                    # Horizontal Split's same-tag default on a list-item
                    # produces list-item-tagged pieces; <list-item> cannot
                    # nest directly inside <list-item> in valid BITS, so each
                    # piece is promoted to an extra <p> (or its own real
                    # children) of THIS list-item instead - a multi-paragraph
                    # list item, not an invalid nested list-item.
                    el.extend(self._list_item_fragment_elements(c))
                else:
                    el.extend(self._gen_zone_multi(c))
        else:
            content = self._list_item_own_text(zone)
            p_el = self._p_with_id(content)
            if p_el is not None:
                el.append(p_el)
        return el

    def _list_item_own_text(self, zone) -> str:
        return self._list_item_own_text_from_bbox(zone, zone.bbox)

    def _list_item_own_text_from_bbox(self, zone, bbox) -> str:
        page = self.pdf.get_page(zone.page)
        # Hyphen-review overrides only apply when bbox is zone's own,
        # UNMODIFIED bbox - the review scans that exact region, so its
        # boundary indices only line up there. A shrunk bbox (own text
        # excluding a nested child's region, from _zone_list_item /
        # _list_item_fragment_elements) can have a different extracted-line
        # sequence, so it deliberately gets no override here.
        keep_at = self._hyphen_keep_at(zone) if bbox == zone.bbox else None
        # Only the zone's own, unmodified bbox can safely prefer stored
        # zone.text - a shrunk bbox (excluding a nested child's own region)
        # has no equivalent sub-region concept in a flat zone.text string,
        # so that case always re-extracts live from the PDF, unchanged.
        if bbox == zone.bbox:
            content = text_extractor.extract_zone_formatted_text(page, zone, keep_at)
        else:
            content = text_extractor.extract_formatted_text(page, bbox, keep_at)
        parent = self.zm.zones.get(zone.parent_id) if zone.parent_id else None
        if parent is not None:
            content = _strip_leading_marker(content, parent.attributes.get("list_type"))
        return content

    def _list_item_fragment_elements(self, zone):
        children = self._sorted_children(zone)
        if children:
            elems = []
            own_bbox = _bbox_excluding_all(zone.bbox, [c.bbox for c in children])
            own_text = self._list_item_own_text_from_bbox(zone, own_bbox)
            p_el = self._p_with_id(own_text)
            if p_el is not None:
                elems.append(p_el)
            for c in children:
                if c.tag == "list-item":
                    elems.extend(self._list_item_fragment_elements(c))
                else:
                    elems.extend(self._gen_zone_multi(c))
            return elems
        content = self._list_item_own_text(zone)
        p_el = self._p_with_id(content)
        return [p_el] if p_el is not None else []

    def _zone_list_bullet(self, zone):
        """Fallback for a SINGLE manually-tagged "list-bullet" zone reached
        in isolation (e.g. via a direct _gen_zone/_gen_zone_multi call
        that didn't go through the sibling-level run-grouping in
        _gen_node_list/_gen_children_grouped - normally every List
        Bullet zone IS reached that way, since those are now THE place
        consecutive List Bullet siblings are combined into one shared
        <list>). Still flattens a same-tag split parent into its own
        leaf pieces first (see _flatten_same_tag_split) - this zone may
        have been reached in isolation precisely BECAUSE it was split,
        so its own bbox alone would span the whole pre-split region."""
        return self._build_bullet_list(self._flatten_same_tag_split(zone))

    def _zone_bibliography(self, zone):
        """<ref-list> container - see _parse_citation module docs. Mirrors
        _zone_list's own established rule exactly: the number of <ref>
        elements comes ONLY from actual Horizontal Split structure (one
        Reference child = one <ref>), NEVER from PDF line wrapping or how
        many citation-shaped sentences the zone's raw text happens to
        contain - an unsplit Bibliography zone is always exactly ONE <ref>,
        whatever text it holds. A leading "Bibliografía"/"References" line
        in the zone's own (non-child) text becomes <title>; if that heading
        is its own separate zone instead (spec Pattern 19), it's simply
        never part of this zone's own text and no <title> is emitted here,
        so a duplicate title can never happen either way.

        When Reference children exist, Horizontal Split always tiles the
        ENTIRE parent bbox among them (core.zone_manager.split_zone) -
        there is never a genuine gap left over for a title line, so
        _bbox_excluding_all degenerates back to the full parent bbox (see
        its own docstring). Re-extracting that full bbox here would
        duplicate whatever the children already produce, so own-text title
        detection only ever runs for an UNSPLIT zone (Pattern 20a); a title
        line embedded as the first of several split pieces is handled by
        retagging that one piece to "title" (Pattern 20b) - the existing
        generic Title handler then emits it as a normal <ref-list> sibling
        with no Bibliography-specific detection needed at all."""
        el = etree.Element("ref-list")
        children = self._sorted_children(zone)
        title_text, remainder_text = None, ""
        if not children:
            page = self.pdf.get_page(zone.page)
            own_text = text_extractor.extract_zone_plain_text(page, zone, self._hyphen_keep_at(zone))
            title_text, remainder_text = _split_bibliography_title(own_text)
        if title_text:
            title_el = etree.Element("title")
            title_el.text = sanitize_xml_text(title_text)
            el.append(title_el)
        if children:
            for child_el in self._gen_children_grouped(zone):
                el.append(child_el)
        else:
            content_text = remainder_text.strip()
            if content_text:
                el.append(self._build_ref_element(content_text))
            else:
                debug_log.log("XML", f"{zone.zone_id}: bibliography zone has no reference content")
        return el

    def _zone_reference(self, zone):
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        plain = text_extractor.strip_tags_to_plain(content)
        return self._build_ref_element(plain)

    def _build_ref_element(self, plain_text: str):
        self.counters["ref"] += 1
        ref_id = f"ref{self.counters['ref']:03d}"
        citation = _parse_citation(plain_text)
        el = etree.Element("ref", attrib={"id": ref_id})
        mc = etree.SubElement(el, "mixed-citation")
        self._append_citation_children(mc, citation)
        return el

    def _append_citation_children(self, mc_el, citation: dict):
        def set_text(parent, tag, value, **attrib):
            if not value:
                return
            child = etree.SubElement(parent, tag, attrib=attrib)
            child.text = sanitize_xml_text(value)

        if citation["authors"] or citation["etal"]:
            pg = etree.SubElement(mc_el, "person-group", attrib={"person-group-type": "author"})
            for surname, given in citation["authors"]:
                name_el = etree.SubElement(pg, "name")
                set_text(name_el, "surname", surname)
                set_text(name_el, "given-names", given)
            if citation["etal"]:
                etree.SubElement(pg, "etal")
        set_text(mc_el, "article-title", citation["article_title"])
        set_text(mc_el, "source", citation["source"])
        set_text(mc_el, "edition", citation["edition"])
        set_text(mc_el, "publisher-loc", citation["publisher_loc"])
        set_text(mc_el, "publisher-name", citation["publisher_name"])
        set_text(mc_el, "year", citation["year"])
        set_text(mc_el, "volume", citation["volume"])
        set_text(mc_el, "issue", citation["issue"])
        set_text(mc_el, "fpage", citation["fpage"])
        set_text(mc_el, "lpage", citation["lpage"])
        set_text(mc_el, "pub-id", citation["doi"], **{"pub-id-type": "doi"})
        set_text(mc_el, "uri", citation["uri"])
        # Nothing recognizable at all (e.g. an empty/garbled zone) - never
        # emit a bare <mixed-citation/>, preserve whatever text there was
        # (spec Pattern 30: prefer preserving content over an empty element).
        if len(mc_el) == 0 and citation["raw"]:
            mc_el.text = sanitize_xml_text(citation["raw"])

    def _zone_table_caption(self, zone):
        """Fallback path for a table-caption zone that was NOT consumed as
        an orphan by an adjacent Table zone (_collect_orphan_table_captions) -
        e.g. a Table Caption drawn with no following Table zone at all. Must
        still return a single valid element (matching _gen_zone's one-
        element-per-zone contract), so it's wrapped in a minimal
        <table-wrap> holding just <label>/<caption><title> - never a bare,
        unprecedented "label+caption with no container" shape, and never a
        <table> child (there is no table geometry to build one from)."""
        self.counters["table"] += 1
        tbl_num = self.counters["table"]
        page = self.pdf.get_page(zone.page)
        lines = text_extractor.extract_lines(page, zone.bbox)
        label, caption_text = extract_table_label_and_caption_from_lines(lines)
        el = etree.Element("table-wrap", attrib={"id": f"{self.prefix}-tbl{tbl_num:03d}", "specific-use": "inline"})
        if label:
            el.append(_parse_inline(f"<label>{label}</label>"))
        cap_text = normalize_title_text(caption_text)
        if cap_text:
            cap_el = etree.Element("caption", attrib={"id": f"{self.prefix}-cap{tbl_num:03d}"})
            cap_el.append(_parse_inline(f"<title>{cap_text}</title>"))
            el.append(cap_el)
        return el

    def _append_table_caption_once(self, wrap_el, cap_el):
        """Table-wrap counterpart of _append_caption_once - a <table-wrap>
        may only ever contain ONE <caption>, and it must never end up
        nested inside <table> (spec: caption must never be inside <table>).
        This guard runs before the <table> child itself is appended, so
        find("caption") only ever sees a prior <caption>, never a <td>/<th>
        that happens to hold matching text."""
        if wrap_el.find("caption") is not None:
            debug_log.log("XML", f"{wrap_el.get('id')}: duplicate <caption> suppressed")
            return
        wrap_el.append(cap_el)

    def _table_row_element(self, row_cells, cell_tag: str, cell_children=None, suppressed_cell_ids=None):
        tr_el = etree.Element("tr")
        for cell in row_cells:
            if cell is None:
                continue
            attrib = {"align": "left", "valign": "top"}
            if cell.colspan > 1:
                attrib["colspan"] = str(cell.colspan)
            if cell.rowspan > 1:
                attrib["rowspan"] = str(cell.rowspan)
            matched = cell_children.get(id(cell)) if cell_children else None
            if not matched and suppressed_cell_ids and id(cell) in suppressed_cell_ids:
                # This auto-detected cell is a geometric ARTIFACT of a real
                # child zone spanning more auto-detected rows than the
                # other column has (see _match_table_cell_children) - its
                # content already lives in that child's own "home" cell
                # elsewhere in this table, so it renders genuinely empty
                # rather than duplicating/fragmenting that content via
                # table_extractor's own independent guess for this region.
                etree.SubElement(tr_el, cell_tag, attrib=attrib)
                continue
            if matched:
                # AUTHORITATIVE source: real, manually-drawn child zones
                # (List/List Bullet/List Item/Paragraph/etc, any tag) whose
                # bbox falls in this cell - see _match_table_cell_children.
                # Their own independently-extracted content is used
                # directly, via the exact same child-generation path every
                # other container in this file already uses for its own
                # children, NEVER table_extractor's own from-scratch
                # text-pattern guess for this region (cell.text/
                # cell.is_list/cell.list_items are simply not consulted
                # for a cell that has real matched children - this is what
                # fixes empty <list-item/> output for a manually zoned
                # Table > List Bullet > List Item / Table > List > List
                # Item structure: the text was always there in the real
                # zones, it just wasn't being read from them).
                cell_el = etree.SubElement(tr_el, cell_tag, attrib=attrib)
                for child_el in self._gen_zone_list_grouped(matched):
                    cell_el.append(child_el)
                continue
            if getattr(cell, "is_list", False) and cell.list_items:
                # Detected as a genuine bullet list (core.table_extractor.
                # _detect_bullet_list_items) - the <list> stays INSIDE this
                # <td>/<th>, one independent list per cell, never merged
                # across columns and never emitted as plain marker-prefixed
                # text.
                cell_el = etree.SubElement(tr_el, cell_tag, attrib=attrib)
                items = [_strip_bold_wrapper(it) for it in cell.list_items] if cell_tag == "th" else cell.list_items
                cell_el.append(self._build_cell_list_element(items))
                continue
            # <th> never nests a redundant <bold> (header styling is
            # already implied by the element itself) - <td> is untouched.
            cell_text = _strip_bold_wrapper(cell.text) if cell_tag == "th" else cell.text
            cell_xml = f"<{cell_tag} " + " ".join(f'{k}="{v}"' for k, v in attrib.items()) + f">{cell_text}</{cell_tag}>"
            try:
                tr_el.append(_parse_inline(cell_xml))
            except Exception:
                td_el = etree.SubElement(tr_el, cell_tag, attrib=attrib)
                td_el.text = sanitize_xml_text(text_extractor.strip_tags_to_plain(cell.text))
        return tr_el

    def _match_table_cell_children(self, table_zone, structure):
        """Maps each auto-detected TableCell (id(cell) -> [Zone, ...], in
        reading order) to the real, manually-drawn child zones of
        table_zone (List/List Bullet/List Item/Paragraph/etc - any tag)
        whose bbox falls inside it, so table-cell generation
        (_table_row_element) can use their own independently-extracted
        text/structure instead of table_extractor's own from-scratch
        text-pattern guess for that region.

        table_extractor.analyze_table's cell.bbox is a purely geometric,
        AUTOMATIC measurement (built from whatever PDF words/lines it
        found) - it will rarely line up to the pixel with the user's own
        hand-drawn child zone boundary, even though both describe the
        same visual cell, and a single hand-drawn child (e.g. one List
        Bullet zone covering a whole column) can legitimately span MORE
        THAN ONE auto-detected cell if table_extractor's own row-grouping
        split that column into more row bands than the other column
        actually has (a real, pre-existing limitation of automatic row
        detection on mixed list/prose content - not something this fix
        changes). Every cell the child MEANINGFULLY overlaps (>= 30% of
        that cell's own area, not just an incidental sliver) is therefore
        tracked: the child's real content is attached to the topmost/
        leftmost such cell (its "home"), and every OTHER overlapped cell
        is marked suppressed - rendered as a genuinely empty cell rather
        than letting it fall back to table_extractor's own guess, which
        would otherwise duplicate/fragment content the real child zone
        already accounts for. A child with no meaningful overlap against
        any cell (should not normally happen, since cells collectively
        span the table's own bbox) is simply never used as an override -
        that cell falls back to table_extractor's own detection exactly
        as before this fix.

        Only DIRECT children of table_zone are matched - a List Bullet's
        own List Item children are never matched separately here; they
        stay nested under their List Bullet parent and are reached
        through it (_build_bullet_list), exactly like a List zone's own
        List Item children already are through _zone_list.

        Returns (cell_children, suppressed_cell_ids): cell_children maps
        id(cell) -> [Zone, ...] (a cell's "home" children, in reading
        order); suppressed_cell_ids is a set of id(cell) for cells that
        must render empty (their content lives in a DIFFERENT cell's
        entry in cell_children)."""
        children = self._sorted_children(table_zone)
        all_cells = [cell for row in structure.rows for cell in row if cell is not None]
        cell_children = {}
        suppressed_cell_ids = set()
        for child in children:
            if child.page != table_zone.page:
                continue
            overlapping = []
            for cell in all_cells:
                ox0 = max(cell.bbox[0], child.bbox[0])
                oy0 = max(cell.bbox[1], child.bbox[1])
                ox1 = min(cell.bbox[2], child.bbox[2])
                oy1 = min(cell.bbox[3], child.bbox[3])
                overlap = max(0.0, ox1 - ox0) * max(0.0, oy1 - oy0)
                cell_w = max(0.0, cell.bbox[2] - cell.bbox[0])
                cell_h = max(0.0, cell.bbox[3] - cell.bbox[1])
                cell_area = cell_w * cell_h
                if cell_area > 0 and overlap / cell_area >= 0.3:
                    overlapping.append(cell)
            if not overlapping:
                continue
            overlapping.sort(key=lambda c: (c.row, c.col))
            home_cell = overlapping[0]
            cell_children.setdefault(id(home_cell), []).append(child)
            for cell in overlapping[1:]:
                suppressed_cell_ids.add(id(cell))
        # A cell that ended up BOTH a home (has real matched children) and
        # incidentally suppressed by some other child is never actually
        # emptied - its own matched content always wins.
        suppressed_cell_ids -= set(cell_children.keys())
        return cell_children, suppressed_cell_ids

    def _build_cell_list_element(self, items):
        list_el = etree.Element("list", attrib={"list-type": "bullet"})
        for item_text in items:
            li_el = etree.SubElement(list_el, "list-item")
            if item_text:
                li_el.append(_parse_inline(f"<p>{item_text}</p>"))
        return list_el

    def _zone_table(self, zone):
        """Main <table-wrap> builder. The user-drawn zone.bbox is the ONLY
        source of table geometry - table_extractor.analyze_table looks
        exclusively inside it (never expanded to nearby paragraphs/
        captions/headings/page numbers). A Table Caption zone, if matched
        as this table's orphan predecessor by _collect_orphan_table_captions
        (reading-order adjacency, caption immediately BEFORE the table -
        opposite direction from Figure->Caption), shares this table's own
        number for both ids (table-wrap/caption share one number, same
        established convention as fig/caption/graphic sharing fig_num in
        _zone_figure) - never an independently invented id format."""
        self.counters["table"] += 1
        tbl_num = self.counters["table"]
        tbl_id = f"{self.prefix}-tbl{tbl_num:03d}"
        wrap_el = etree.Element("table-wrap", attrib={"id": tbl_id, "specific-use": "inline"})

        caption_zone_id = self._table_caption_for_table.get(zone.zone_id)
        caption_zone = self.zm.zones.get(caption_zone_id) if caption_zone_id else None
        if caption_zone is not None:
            page = self.pdf.get_page(caption_zone.page)
            lines = text_extractor.extract_lines(page, caption_zone.bbox)
            label, caption_text = extract_table_label_and_caption_from_lines(lines)
            if label:
                wrap_el.append(_parse_inline(f"<label>{label}</label>"))
            cap_text = normalize_title_text(caption_text)
            if cap_text:
                cap_el = etree.Element("caption", attrib={"id": f"{self.prefix}-cap{tbl_num:03d}"})
                cap_el.append(_parse_inline(f"<title>{cap_text}</title>"))
                self._append_table_caption_once(wrap_el, cap_el)

        page = self.pdf.get_page(zone.page)
        # Manual split guides (gui.pdf_viewer's Region Split Mode,
        # Ctrl+Shift+R / Ctrl+Shift+C) - PDF page-space Y/X positions the
        # user drew as override boundaries for this Table zone
        # specifically, stored directly on its own attributes
        # (horizontal_splits/vertical_splits - the SAME generic attribute
        # keys every other tag's manual split uses, see
        # split_zone_into_regions below; for a table specifically they
        # form a row x column GRID instead of separate sibling elements -
        # see _gen_zone_multi). Same established pattern as
        # hyphen_keep_boundaries - round-trips through project save/load
        # with no extra code. Never separate XML zones, never present in
        # the generated XML themselves.
        structure = table_extractor.analyze_table(
            page, zone.bbox, self._hyphen_keep_at(zone),
            manual_row_splits=zone.attributes.get("horizontal_splits"),
            manual_column_splits=zone.attributes.get("vertical_splits"))
        # Real, manually-drawn child zones (List/List Bullet/List Item/
        # Paragraph/etc nested inside this Table zone via ordinary
        # containment) take priority over table_extractor's own auto-
        # detected cell text/list guess for whichever specific cell they
        # fall in - see _match_table_cell_children/_table_row_element.
        cell_children, suppressed_cell_ids = self._match_table_cell_children(zone, structure)

        def _row_has_real_content(row_cells):
            # A row where every cell is either genuinely absent (colspan/
            # rowspan-consumed position) or a suppressed geometric artifact
            # (see _match_table_cell_children) contributes nothing - never
            # emitted as an empty <tr>, since that artifact row exists only
            # because a real child zone happened to span more auto-
            # detected rows than a sibling column has, not because the
            # table genuinely has an extra row here.
            real_cells = [c for c in row_cells if c is not None]
            return any(id(c) in cell_children or id(c) not in suppressed_cell_ids for c in real_cells)

        table_el = etree.Element("table", attrib={"frame": "void"})
        header_rows = [r for r in structure.rows[:structure.header_row_count] if _row_has_real_content(r)]
        body_rows = [r for r in structure.rows[structure.header_row_count:] if _row_has_real_content(r)]
        if header_rows:
            thead_el = etree.SubElement(table_el, "thead")
            for row_cells in header_rows:
                thead_el.append(self._table_row_element(row_cells, "th", cell_children, suppressed_cell_ids))
        if body_rows:
            tbody_el = etree.SubElement(table_el, "tbody")
            for row_cells in body_rows:
                tbody_el.append(self._table_row_element(row_cells, "td", cell_children, suppressed_cell_ids))
        wrap_el.append(table_el)

        # A Table Wrap Foot zone, if matched as this table's orphan
        # successor by _collect_table_wrap_foot_zones (reading-order
        # adjacency, foot immediately AFTER the table) - appended AFTER
        # <table> and INSIDE this same <table-wrap>, never as a
        # standalone top-level element (spec part 13).
        foot_zone_id = self._table_wrap_foot_for_table.get(zone.zone_id)
        foot_zone = self.zm.zones.get(foot_zone_id) if foot_zone_id else None
        if foot_zone is not None:
            foot_el = self._build_table_wrap_foot(foot_zone)
            if foot_el is not None:
                wrap_el.append(foot_el)
        return wrap_el

    def _build_table_wrap_foot(self, foot_zone):
        """Builds <table-wrap-foot><attrib>...</attrib></table-wrap-foot>
        from a table-wrap-foot zone's own formatted text (bold/italic/sup/
        sub/hyphenation all reused unchanged via extract_formatted_text,
        same as every other tag) - reused by both _zone_table (the normal,
        associated case) and _zone_table_wrap_foot (the orphan fallback
        below). Returns None for an empty zone, so callers never append a
        bare <table-wrap-foot/>."""
        page = self.pdf.get_page(foot_zone.page)
        content = text_extractor.extract_zone_formatted_text(page, foot_zone, self._hyphen_keep_at(foot_zone))
        if not content:
            return None
        foot_el = etree.Element("table-wrap-foot")
        foot_el.append(_parse_inline(f"<attrib>{content}</attrib>"))
        return foot_el

    def _zone_table_wrap_foot(self, zone):
        """Fallback path for a table-wrap-foot zone that was NOT consumed
        as an orphan by a preceding Table zone (_collect_table_wrap_foot_zones) -
        e.g. drawn with no preceding Table zone at all. Spec part 13: a
        <table-wrap-foot> must NEVER become a standalone top-level XML
        element, so - mirroring _zone_table_caption's own fallback - it's
        still wrapped in a minimal <table-wrap> rather than emitted bare."""
        self.counters["table"] += 1
        tbl_num = self.counters["table"]
        wrap_el = etree.Element("table-wrap", attrib={"id": f"{self.prefix}-tbl{tbl_num:03d}", "specific-use": "inline"})
        foot_el = self._build_table_wrap_foot(zone)
        if foot_el is not None:
            wrap_el.append(foot_el)
        return wrap_el

    def _zone_equation(self, zone):
        filename = self.assets.save_equation_asset(self.pdf, zone.page, zone.bbox)
        el = etree.Element("equation")
        graphic_el = etree.Element("graphic")
        graphic_el.set(f"{{{XLINK_NS}}}href", filename)
        el.append(graphic_el)
        return el

    def _zone_generic(self, zone):
        page = self.pdf.get_page(zone.page)
        content = text_extractor.extract_zone_formatted_text(page, zone, self._hyphen_keep_at(zone))
        if content:
            try:
                return _parse_inline(f"<{zone.tag}>{content}</{zone.tag}>")
            except Exception:
                pass
        el = etree.Element(zone.tag)
        for c in self._sorted_children(zone):
            el.extend(self._gen_zone_multi(c))
        return el


def generate_xml(zone_manager, pdf_document, output_path: str, assets_dir: str,
                  prefix: str = "document", jpeg_quality: int = 95, root_tag: str = "book",
                  image_dpi: int = None, remove_image_background: bool = False):
    gen = XMLGenerator(zone_manager, pdf_document, assets_dir, prefix, jpeg_quality, root_tag, image_dpi,
                        remove_image_background)
    return gen.generate(output_path)
