"""Step 7 rule (spec section 7) - assigns a fresh id ONLY to a heading/
pagebreak/figure/table that does not already have one, following the
SAME convention already used elsewhere in this codebase (core.
epub_xml_generator.EpubXmlGenerator.assign_missing_ids): "{prefix}_{tag}_
{N}" for headings (e.g. "bm3_h1_1"), "{prefix}_fig{N}"/"{prefix}_tbl{N}"
for figures/tables - prefix derived from the file's own name (its content
prefix, stripping any leading numeric/underscore ordering segment, e.g.
"23_980AR_bm3.xhtml" -> "bm3"). Pagebreak ids are the SAME one deliberate
exception the rest of the app already documents: "page_{label}", NEVER
carrying the content prefix.

This module NEVER touches an id that already exists - spec 16/20's
content-preservation guarantee only holds if a real, working existing id
(and every href already pointing at it) is left completely alone."""
import posixpath
import re

_PREFIX_RE = re.compile(r"^\d+_(.+)$")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def derive_content_prefix(path: str) -> str:
    """"23_980AR_bm3.xhtml" -> "bm3" (mirrors core.component_output.
    derive_id_prefix's own "short code" convention): strip the leading
    numeric ordering segment, then take the LAST underscore-separated
    segment of what remains. A filename with no leading digits (a book
    that names its own files differently) falls back to the whole stem,
    sanitized - never crashes, never returns an empty prefix."""
    stem = posixpath.splitext(posixpath.basename(path))[0]
    m = _PREFIX_RE.match(stem)
    remainder = m.group(1) if m else stem
    prefix = remainder.rsplit("_", 1)[-1] if "_" in remainder else remainder
    prefix = _ID_SAFE_RE.sub("", prefix)
    return prefix or "doc"


def ensure_heading_ids(document_record, existing_ids: set) -> list:
    """Walks document_record.headings in order; any Heading whose real
    element has no id attribute gets "{prefix}_{tag}_{N}" (N restarts at 1
    per tag name, per document - matching the existing convention exactly).
    Mutates the underlying lxml Element in place (id assignment only - no
    other attribute, no text, is ever touched) and updates document_record.
    fragment_ids/existing_ids so a LATER assignment in the same pass can
    never collide with one just made. Returns [(heading, new_id), ...] for
    reporting; empty if every heading already had a real id."""
    prefix = derive_content_prefix(document_record.path)
    counters = {}
    assigned = []
    for heading in document_record.headings:
        if heading.id:
            continue
        tag = f"h{heading.tag_level}"
        n = counters.get(tag, 0) + 1
        candidate = f"{prefix}_{tag}_{n}"
        while candidate in existing_ids:
            n += 1
            candidate = f"{prefix}_{tag}_{n}"
        counters[tag] = n
        heading.element.set("id", candidate)
        heading.id = candidate
        existing_ids.add(candidate)
        document_record.fragment_ids.add(candidate)
        assigned.append((heading, candidate))
    return assigned


def ensure_pagebreak_ids(document_record, existing_ids: set) -> list:
    """Pagebreak ids are "page_{label}" - NEVER carrying this document's
    own content prefix (the one deliberate exception the rest of this app
    already documents). A pagebreak with no usable label at all (neither
    an existing id nor an aria-label) is skipped rather than given a fake
    sequential number that could collide with, or be confused for, a real
    printed page - spec 10: "Do not construct page IDs from filenames...
    do not invent"."""
    assigned = []
    for pb in document_record.pagebreaks:
        if pb.id:
            continue
        if not pb.label:
            continue
        candidate = f"page_{_ID_SAFE_RE.sub('', pb.label)}"
        suffix = 2
        base = candidate
        while candidate in existing_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        pb.element.set("id", candidate)
        pb.id = candidate
        existing_ids.add(candidate)
        document_record.fragment_ids.add(candidate)
        assigned.append((pb, candidate))
    return assigned
