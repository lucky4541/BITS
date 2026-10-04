"""Step 3 (spec section 3) - detects a content document's type. Layered,
most-reliable signal first, exactly as the spec requires ("Use semantic
XHTML markup first... filename patterns only as a secondary fallback...
Never rely only on filename"):

  1. An UNAMBIGUOUS <section epub:type="..."> or <body epub:type="...">
     value that already names a specific document type (never "chapter" -
     confirmed against a real production EPUB that a converter's own
     generic default epub:type="chapter" gets stamped on title pages and
     half-title pages too, so trusting it unconditionally would misclassify
     them).
  2. The document's own first heading text matching a known type keyword
     ("Contents", "Acknowledgements", "Bibliography", "Name index", ...).
  3. The first heading's own normalized text closely matching the book's
     real title (from the OPF, when available) - the standard way a half-
     title/title page is distinguished from any other front-matter page.
  4. A numbered first heading ("1 Introduction") inside a bodymatter
     document - unambiguously a chapter.
  5. Any OTHER bodymatter document that has at least one real heading -
     still a chapter (a real book was found with un-numbered chapter
     titles, e.g. "Future directions", that only its INTERNAL subsections
     are numbered - it is still structurally a chapter).
  6. Position: the very first document in the whole book, frontmatter,
     with no heading at all - a cover.
  7. Filename pattern - LAST resort only.
  8. The body-level frontmatter/backmatter bucket itself.
"""
import posixpath
import re

from core.epub_structure.xhtml_parser import epub_type, iter_by_local_name, local_name

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Direct epub:type values that are ALREADY a specific, trustworthy document
# type on their own (deliberately excludes "chapter" - see module docstring).
_EPUB_TYPE_MAP = {
    "cover": "cover", "titlepage": "titlepage", "halftitlepage": "halftitle",
    "copyright-page": "copyrightpage", "dedication": "dedication", "toc": "toc",
    "preface": "preface", "foreword": "foreword", "introduction": "introduction",
    "acknowledgments": "acknowledgements", "acknowledgements": "acknowledgements",
    "glossary": "glossary", "bibliography": "bibliography", "index": "index",
    "appendix": "appendix", "part": "part", "colophon": "backmatter",
}

# (keyword, document_type) - matched against a document's own FIRST heading
# text, normalized (lowercased, whitespace-collapsed). Longer/more specific
# keywords are listed first so e.g. "name index" wins over the bare "index".
_HEADING_KEYWORDS = [
    ("table of contents", "toc"), ("contents", "toc"),
    ("acknowledgements", "acknowledgements"), ("acknowledgments", "acknowledgements"),
    ("foreword", "foreword"), ("preface", "preface"),
    ("name index", "name-index"), ("subject index", "subject-index"), ("index", "index"),
    ("bibliography", "bibliography"), ("references", "bibliography"),
    ("glossary", "glossary"), ("dedication", "dedication"),
    ("appendix", "appendix"), ("errata", "backmatter"), ("colophon", "backmatter"),
]

_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.:]?\s+(\S.*)$")
_WHITESPACE_RE = re.compile(r"\s+")
# A dedication is conventionally a single short line starting "To ..."
# ("To Elaine, Katy and Laura") - gated on both the prefix AND a short word
# count so a genuinely-titled chapter that happens to start with "To" (rare,
# but possible - e.g. "To Boldly Go") is not misclassified.
_DEDICATION_RE = re.compile(r"^to\s+\S", re.IGNORECASE)


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "")).strip().lower()


def _first_heading_text(tree) -> str:
    for el in tree.iter():
        if local_name(el.tag) in _HEADING_TAGS:
            return "".join(el.itertext())
    return ""


def _filename_fallback(filename: str) -> str:
    stem = posixpath.splitext(posixpath.basename(filename))[0].lower()
    # Longest/most specific markers first, so e.g. "toc" beats a bare "fm".
    patterns = [
        (r"cov", "cover"), (r"toc", "toc"), (r"cvr", "cover"),
        (r"(^|[_-])ch\d*", "chapter"), (r"(^|[_-])bm\d*", "backmatter"),
        (r"(^|[_-])fm\d*", "frontmatter"), (r"(^|[_-])app\d*", "appendix"),
        (r"(^|[_-])idx", "index"), (r"(^|[_-])gloss", "glossary"),
        (r"(^|[_-])bib", "bibliography"), (r"(^|[_-])pref", "preface"),
    ]
    for pattern, dtype in patterns:
        if re.search(pattern, stem):
            return dtype
    return ""


def detect_document_type(tree, filename: str, order: int, book_title: str = "") -> str:
    """`order`: this document's 0-based position among ALL xhtml files in
    document order (used only for the position-based cover heuristic).
    `book_title`: the book's own dc:title from the OPF, if already known -
    used only to recognize a half-title/title page repeating it; an empty
    string simply skips that one check, never raises."""
    body = next((e for e in tree.iter() if local_name(e.tag) == "body"), None)
    body_type = epub_type(body) if body is not None else ""
    section_types = [epub_type(s) for s in iter_by_local_name(tree, "section")]

    heading_text = _first_heading_text(tree)
    normalized = _normalize(heading_text)

    # "index" is real but under-specific - a real production EPUB was found
    # to mark BOTH its name-index and subject-index sections identically as
    # epub:type="index", distinguishable only by the page's own heading
    # text - so the more specific name/subject variants are checked before
    # trusting the generic epub:type value.
    if "index" in section_types or body_type == "index":
        if "name" in normalized and "index" in normalized:
            return "name-index"
        if "subject" in normalized and "index" in normalized:
            return "subject-index"

    for et in section_types:
        if et in _EPUB_TYPE_MAP:
            return _EPUB_TYPE_MAP[et]
    if body_type in _EPUB_TYPE_MAP:
        return _EPUB_TYPE_MAP[body_type]

    if normalized:
        for keyword, dtype in _HEADING_KEYWORDS:
            if normalized == keyword or normalized.startswith(keyword + " ") or normalized.startswith(keyword + ":"):
                return dtype
        if _DEDICATION_RE.match(normalized) and len(normalized.split()) <= 8:
            return "dedication"

    if book_title and normalized:
        norm_title = _normalize(book_title)
        if norm_title and (normalized == norm_title or norm_title in normalized or normalized in norm_title):
            return "halftitle" if order <= 1 else "titlepage"

    if body_type == "bodymatter" and normalized:
        m = _NUMBERED_HEADING_RE.match(heading_text.strip())
        if m and "." not in m.group(1):
            return "chapter"
        return "chapter"  # an un-numbered but real bodymatter heading is still structurally a chapter

    if order == 0 and body_type in ("", "frontmatter") and not normalized:
        return "cover"

    fallback = _filename_fallback(filename)
    if fallback:
        return fallback

    if body_type == "frontmatter":
        return "frontmatter"
    if body_type == "backmatter":
        return "backmatter"
    return "other"
