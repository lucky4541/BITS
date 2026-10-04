"""
Step 5 - AUTO-CAPTURE METADATA

Detects book metadata from the CURRENT EPUB project.

Priority:
    1. Existing valid OPF metadata
    2. XHTML <title>, <meta>, xml:lang and structured metadata
    3. Title / half-title page
    4. Copyright page
    5. Other structured front matter

Never guesses a missing value and never inserts placeholders.
"""

import re
from dataclasses import dataclass, field
from lxml import etree

from core.epub_structure.xhtml_parser import local_name

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_DC = f"{{{DC_NS}}}"
_OPF = f"{{{OPF_NS}}}"


@dataclass
class MetadataField:
    value: str = ""
    source: str = ""
    confidence: str = ""


@dataclass
class DetectedMetadata:
    title: MetadataField = field(default_factory=MetadataField)
    subtitle: MetadataField = field(default_factory=MetadataField)
    author: MetadataField = field(default_factory=MetadataField)
    language: MetadataField = field(default_factory=MetadataField)
    publisher: MetadataField = field(default_factory=MetadataField)
    publication_date: MetadataField = field(default_factory=MetadataField)
    edition: MetadataField = field(default_factory=MetadataField)
    conflicts: list = field(default_factory=list)

    def missing_fields(self) -> list:
        return [
            name for name in ("title", "author", "language")
            if not getattr(self, name).value
        ]


_COPYRIGHT_KEYWORDS = (
    "copyright",
    "all rights reserved",
    "isbn",
    "published by",
    "published",
    "printed in",
    "first published",
    "edition",
)

_EDITION_RE = re.compile(
    r"\b("
    r"\d+(?:st|nd|rd|th)\s+edition"
    r"|first edition|second edition|third edition|fourth edition"
    r"|fifth edition|sixth edition|seventh edition|eighth edition"
    r"|ninth edition|tenth edition"
    r")\b",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")

_PUBLISHER_RE = re.compile(
    r"(?:published\s+by|publisher|imprint)\s*[:\-]?\s*"
    r"([A-Z][A-Za-z0-9&.,'’\-() ]{2,100})",
    re.IGNORECASE,
)

_AUTHOR_RE = re.compile(
    r"(?:by|author(?:s)?|written\s+by|edited\s+by|"
    r"editor(?:s)?)\s*[:\-]?\s*"
    r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,6})",
    re.IGNORECASE,
)

_LANGUAGE_MAP = {
    "english": "en",
    "eng": "en",
    "french": "fr",
    "fra": "fr",
    "fre": "fr",
    "german": "de",
    "deu": "de",
    "ger": "de",
    "spanish": "es",
    "spa": "es",
    "italian": "it",
    "ita": "it",
    "portuguese": "pt",
    "por": "pt",
    "dutch": "nl",
    "nld": "nl",
    "greek": "el",
    "ell": "el",
    "latin": "la",
    "rus": "ru",
    "russian": "ru",
}


def _clean(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" \t\r\n:;,.–—")


def _read_tree(scan, path):
    try:
        with open(scan.abspath(path), "rb") as f:
            return etree.fromstring(f.read())
    except (OSError, etree.XMLSyntaxError, ValueError):
        return None


def _plain_text(tree) -> str:
    if tree is None:
        return ""
    return _clean(" ".join(tree.itertext()))


def _extract_plain_text(scan, path: str) -> str:
    return _plain_text(_read_tree(scan, path))


def _first_child_by_local_name(parent, name):
    for child in parent:
        if local_name(child.tag) == name:
            return child
    return None


def _metadata_elements(metadata_el, local):
    return [
        e for e in metadata_el
        if local_name(e.tag) == local and _clean(e.text or "")
    ]


def _read_existing_opf(scan) -> dict:
    """Read usable metadata already present in the CURRENT OPF."""
    if not scan.opf_path:
        return {}

    tree = _read_tree(scan, scan.opf_path)
    if tree is None:
        return {}

    metadata_el = next(
        (e for e in tree.iter() if local_name(e.tag) == "metadata"),
        None,
    )
    if metadata_el is None:
        return {}

    found = {}

    title_nodes = _metadata_elements(metadata_el, "title")
    creator_nodes = _metadata_elements(metadata_el, "creator")
    publisher_nodes = _metadata_elements(metadata_el, "publisher")
    language_nodes = _metadata_elements(metadata_el, "language")
    date_nodes = _metadata_elements(metadata_el, "date")

    if title_nodes:
        # Prefer title refined as main; otherwise first dc:title.
        main = None
        for title in title_nodes:
            tid = title.get("id")
            if tid:
                for meta in metadata_el:
                    if (
                        local_name(meta.tag) == "meta"
                        and (meta.get("refines") or "").lstrip("#") == tid
                        and (meta.get("property") or "").lower() == "title-type"
                        and _clean(meta.text or "").lower() == "main"
                    ):
                        main = title
                        break
            if main is not None:
                break
        title = main or title_nodes[0]
        found["title"] = _clean(title.text or "")

        # Find an explicitly refined subtitle.
        for title in title_nodes:
            tid = title.get("id")
            if not tid:
                continue
            for meta in metadata_el:
                if (
                    local_name(meta.tag) == "meta"
                    and (meta.get("refines") or "").lstrip("#") == tid
                    and (meta.get("property") or "").lower() == "title-type"
                    and _clean(meta.text or "").lower() == "subtitle"
                ):
                    found["subtitle"] = _clean(title.text or "")
                    break
            if "subtitle" in found:
                break

    if creator_nodes:
        # Prefer creator with role aut; otherwise first creator.
        author = None
        for creator in creator_nodes:
            cid = creator.get("id")
            if cid:
                for meta in metadata_el:
                    if (
                        local_name(meta.tag) == "meta"
                        and (meta.get("refines") or "").lstrip("#") == cid
                        and (meta.get("property") or "").lower() == "role"
                        and _clean(meta.text or "").lower() in {"aut", "author"}
                    ):
                        author = creator
                        break
            if author is not None:
                break
        author = author or creator_nodes[0]
        found["author"] = _clean(author.text or "")

    if publisher_nodes:
        found["publisher"] = _clean(publisher_nodes[0].text or "")

    if language_nodes:
        found["language"] = _clean(language_nodes[0].text or "").lower()

    if date_nodes:
        found["publication_date"] = _clean(date_nodes[0].text or "")

    for meta in metadata_el:
        if local_name(meta.tag) != "meta":
            continue

        prop = _clean(meta.get("property") or "").lower()
        text = _clean(meta.text or "")
        if not text:
            continue

        if prop in {"schema:bookedition", "bookedition"}:
            found.setdefault("edition", text)

        elif prop == "belongs-to-collection":
            # Do not mistake a series for an edition.
            continue

        elif prop in {"dcterms:publisher", "schema:publisher"}:
            found.setdefault("publisher", text)

    return found


def _set_if_empty(result, field_name, value, source, confidence):
    value = _clean(value)
    if not value:
        return

    field = getattr(result, field_name)
    if field.value:
        if field.value.casefold() != value.casefold():
            result.conflicts.append(
                f"{field_name}: {field.value!r} from {field.source} "
                f"vs {value!r} from {source}"
            )
        return

    setattr(
        result,
        field_name,
        MetadataField(value=value, source=source, confidence=confidence),
    )


def _normalise_language(value: str) -> str:
    value = _clean(value).lower()
    if not value:
        return ""

    # Already an ISO-style language code.
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", value):
        return value

    return _LANGUAGE_MAP.get(value, "")


def _extract_xhtml_metadata(scan, registry, result):
    """Use explicit XHTML metadata before visual/text heuristics."""
    for doc in registry.documents:
        tree = getattr(doc, "tree", None)
        if tree is None:
            tree = _read_tree(scan, doc.path)
        if tree is None:
            continue

        source = f"XHTML metadata ({doc.path})"

        lang = tree.get(f"{{{_XML_NS}}}lang") or tree.get("lang")
        lang = _normalise_language(lang or "")
        if lang:
            _set_if_empty(result, "language", lang, source, "HIGH")

        head = next(
            (e for e in tree.iter() if local_name(e.tag) == "head"),
            None,
        )
        if head is None:
            continue

        title = next(
            (e for e in head if local_name(e.tag) == "title" and _clean(e.text or "")),
            None,
        )
        if title is not None:
            _set_if_empty(result, "title", title.text, source, "MEDIUM")

        for meta in head:
            if local_name(meta.tag) != "meta":
                continue

            name = (meta.get("name") or meta.get("property") or "").lower()
            content = _clean(meta.get("content") or meta.text or "")
            if not content:
                continue

            if name in {"dc.title", "dcterms:title", "title"}:
                _set_if_empty(result, "title", content, source, "HIGH")
            elif name in {"dc.creator", "dcterms:creator", "author", "creator"}:
                _set_if_empty(result, "author", content, source, "HIGH")
            elif name in {"dc.language", "dcterms:language", "language"}:
                language = _normalise_language(content)
                if language:
                    _set_if_empty(result, "language", language, source, "HIGH")
            elif name in {"dc.publisher", "dcterms:publisher", "publisher"}:
                _set_if_empty(result, "publisher", content, source, "HIGH")
            elif name in {"dc.date", "dcterms:date", "date", "publication_date"}:
                _set_if_empty(result, "publication_date", content, source, "HIGH")
            elif name in {"edition", "schema:bookedition"}:
                _set_if_empty(result, "edition", content, source, "HIGH")


def _looks_like_copyright_page(text: str) -> bool:
    lowered = text.lower()
    score = sum(1 for kw in _COPYRIGHT_KEYWORDS if kw in lowered)
    return score >= 2


def _document_priority(doc):
    dtype = (getattr(doc, "document_type", "") or "").lower()
    if dtype == "titlepage":
        return 0
    if dtype == "halftitle":
        return 1
    if dtype == "copyrightpage":
        return 0
    if dtype == "frontmatter":
        return 2
    return 3


def _candidate_frontmatter_documents(registry):
    docs = [
        d for d in registry.documents
        if (getattr(d, "document_type", "") or "").lower()
        in {"titlepage", "halftitle", "copyrightpage", "frontmatter"}
    ]
    return sorted(docs, key=_document_priority)


def _detect_from_title_page(scan, doc, result):
    text = _extract_plain_text(scan, doc.path)
    if not text:
        return

    source = f"title page ({doc.path})"

    # Registry-provided title is preferred when available.
    doc_title = _clean(getattr(doc, "title", "") or "")
    if doc_title:
        _set_if_empty(result, "title", doc_title, source, "HIGH")

    lines = [
        _clean(x)
        for x in re.split(r"[\r\n]+", text)
        if _clean(x)
    ]

    # Remove very short/noisy lines when selecting title candidates.
    candidates = [
        x for x in lines
        if len(x) >= 3
        and not _YEAR_RE.fullmatch(x)
        and not _EDITION_RE.fullmatch(x)
    ]

    if not result.title.value and candidates:
        # Prefer the first substantial title-like line, not "Contents", "By", etc.
        ignored = {"contents", "copyright", "title page", "by"}
        candidate = next(
            (x for x in candidates if x.lower() not in ignored),
            "",
        )
        if candidate:
            _set_if_empty(result, "title", candidate, source, "MEDIUM")

    author_match = _AUTHOR_RE.search(text)
    if author_match:
        _set_if_empty(
            result,
            "author",
            author_match.group(1),
            source,
            "MEDIUM",
        )

    edition_match = _EDITION_RE.search(text)
    if edition_match:
        _set_if_empty(
            result,
            "edition",
            edition_match.group(1),
            source,
            "MEDIUM",
        )


def _find_copyright_doc(scan, registry):
    docs = [
        d for d in registry.documents
        if (getattr(d, "document_type", "") or "").lower() == "copyrightpage"
    ]
    if docs:
        return docs[0]

    # Inspect only early front matter, where copyright information normally occurs.
    for doc in list(registry.documents)[:10]:
        text = _extract_plain_text(scan, doc.path)
        if _looks_like_copyright_page(text):
            return doc

    return None


def _detect_from_copyright(scan, doc, result):
    text = _extract_plain_text(scan, doc.path)
    if not text:
        return

    source = f"copyright page ({doc.path})"

    publisher_match = _PUBLISHER_RE.search(text)
    if publisher_match:
        publisher = _clean(publisher_match.group(1))
        # Stop accidental capture at common copyright-page boundaries.
        publisher = re.split(
            r"\b(?:isbn|copyright|printed in|first published|second edition)\b",
            publisher,
            flags=re.IGNORECASE,
        )[0]
        _set_if_empty(result, "publisher", publisher, source, "MEDIUM")

    edition_match = _EDITION_RE.search(text)
    if edition_match:
        _set_if_empty(
            result,
            "edition",
            edition_match.group(1),
            source,
            "MEDIUM",
        )

    # Prefer a year explicitly associated with publication.
    publication_patterns = [
        r"(?:first\s+published|published|publication)\s*(?:in)?\s*"
        r"((?:18|19|20)\d{2})",
        r"(?:copyright|©)\s*(?:[^\d]{0,20})((?:18|19|20)\d{2})",
    ]

    year = ""
    for pattern in publication_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            year = m.group(1)
            break

    # Only fall back to a year when the page has strong copyright evidence.
    if not year and _looks_like_copyright_page(text):
        years = _YEAR_RE.findall(text)
        if len(years) == 1:
            year = years[0]

    if year:
        _set_if_empty(
            result,
            "publication_date",
            year,
            source,
            "MEDIUM",
        )


def detect_metadata(scan, registry) -> DetectedMetadata:
    """
    Single entry point.

    Existing OPF values always win. Missing values are progressively
    filled from the current EPUB's XHTML metadata, title page, copyright
    page and front matter. Unknown fields remain empty.
    """
    result = DetectedMetadata()

    # 1. Existing OPF: highest priority.
    opf_values = _read_existing_opf(scan)
    for field_name in (
        "title",
        "subtitle",
        "author",
        "language",
        "publisher",
        "publication_date",
        "edition",
    ):
        value = opf_values.get(field_name, "")
        if field_name == "language":
            value = _normalise_language(value)
        if value:
            setattr(
                result,
                field_name,
                MetadataField(
                    value=value,
                    source="existing OPF metadata",
                    confidence="HIGH",
                ),
            )

    # 2. Explicit XHTML metadata.
    _extract_xhtml_metadata(scan, registry, result)

    # 3. Title / half-title pages.
    for doc in _candidate_frontmatter_documents(registry):
        dtype = (getattr(doc, "document_type", "") or "").lower()
        if dtype in {"titlepage", "halftitle"}:
            _detect_from_title_page(scan, doc, result)

    # 4. Copyright page.
    copyright_doc = _find_copyright_doc(scan, registry)
    if copyright_doc is not None:
        _detect_from_copyright(scan, copyright_doc, result)

    # 5. Other front matter.
    if not result.author.value or not result.publisher.value:
        for doc in _candidate_frontmatter_documents(registry):
            dtype = (getattr(doc, "document_type", "") or "").lower()
            if dtype not in {"frontmatter", "copyrightpage"}:
                continue

            text = _extract_plain_text(scan, doc.path)
            if not text:
                continue

            source = f"front matter ({doc.path})"

            if not result.author.value:
                m = _AUTHOR_RE.search(text)
                if m:
                    _set_if_empty(
                        result,
                        "author",
                        m.group(1),
                        source,
                        "LOW",
                    )

            if not result.publisher.value:
                m = _PUBLISHER_RE.search(text)
                if m:
                    _set_if_empty(
                        result,
                        "publisher",
                        m.group(1),
                        source,
                        "LOW",
                    )

            if not result.edition.value:
                m = _EDITION_RE.search(text)
                if m:
                    _set_if_empty(
                        result,
                        "edition",
                        m.group(1),
                        source,
                        "LOW",
                    )

    return result
