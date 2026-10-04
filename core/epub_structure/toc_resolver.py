"""Steps 5-6 (spec sections 5-6) - finds the book's own printed "Contents"
page (document_type == "toc") and resolves each of its entries against a
REAL heading in the GLOBAL DOCUMENT REGISTRY, following the priority list
spec 5 itself lists. Never invents a link (spec 6: "NEVER generate {0}...
NEVER generate a fragment that does not exist").

Confirmed against a real production EPUB before writing this: printed-book
Contents pages are NOT clean data. In the same file, real entries were
found in every one of these shapes:
  "1.1 The discovery of fullerene-related carbon nanotubes"   (number first - clean)
  "Introduction  1"                                            (title, then a stray page number, no section number)
  "The arc-evaporation technique  2.1.1"                       (title FIRST, section number at the very end)
  "References"                                                 (repeats identically once per chapter - ambiguous by text alone)
Matching therefore never assumes a fixed token order, and disambiguates a
text-only match (like repeated "References") by proximity to whichever
document was most recently resolved - the Contents page is always read
top-to-bottom in the same order as the book itself."""
import difflib
import posixpath
import re
from dataclasses import dataclass, field

from core.epub_structure.xhtml_parser import local_name

_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)+\b|\b\d+\b")
_WHITESPACE_RE = re.compile(r"\s+")
FUZZY_MATCH_THRESHOLD = 0.90


@dataclass
class TocEntry:
    element: object            # the real <a> element in the Contents document
    raw_text: str
    resolved_path: str = ""      # target document's path, once resolved
    resolved_id: str = ""
    method: str = ""             # "number+text" / "text" / "number" / "fuzzy" / "" (unresolved)
    candidates_considered: int = 0


@dataclass
class TocResolution:
    toc_path: str = ""
    entries: list = field(default_factory=list)   # TocEntry
    resolved: int = 0
    unresolved: int = 0


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip().lower()


_WORD_TOKEN_RE = re.compile(r"\S+")


def _extract_numbers_and_residual(text: str, known_page_labels: frozenset = frozenset()):
    """known_page_labels: every real pagebreak label (e.g. "vii", "42")
    already found anywhere in THIS book's own pagebreak registry -
    confirmed against a real production EPUB: a trailing roman-numeral
    page number ("Preface vii") is exactly as common as a trailing Arabic
    one ("Introduction 1"), but a bare regex for roman numerals (i, v, x,
    l, c, d, m) would also match ordinary English words that happen to be
    spelled only with those letters ("mix", "did", "civic") - cross-
    checking against a REAL page label this book's own pagebreak analyzer
    already found is what makes stripping one safe here, never a blind
    pattern match."""
    numbers = _NUMBER_TOKEN_RE.findall(text)
    residual_text = _NUMBER_TOKEN_RE.sub(" ", text)
    if known_page_labels:
        words = _WORD_TOKEN_RE.findall(residual_text)
        while words and words[-1].lower() in known_page_labels:
            numbers.append(words.pop())
        residual_text = " ".join(words)
    residual = _normalize(residual_text)
    return numbers, residual


def _build_heading_index(registry):
    """(number -> [(doc, heading), ...], normalized-text -> [(doc, heading), ...])
    across every document EXCEPT the toc document itself - a Contents page
    is never allowed to link to its own other entries."""
    by_number, by_text = {}, {}
    for doc in registry.documents:
        if doc.document_type == "toc":
            continue
        for heading in doc.headings:
            if heading.number:
                by_number.setdefault(heading.number, []).append((doc, heading))
            norm = _normalize(heading.text)
            if norm:
                by_text.setdefault(norm, []).append((doc, heading))
    return by_number, by_text


def _closest_to(candidates, current_order):
    """candidates: [(doc, heading), ...]. Prefers the candidate at or after
    current_order (the natural reading direction of a Contents page), the
    CLOSEST such one; falls back to the closest one before it if nothing
    at or after exists. Never picks arbitrarily among ties beyond this
    deterministic rule."""
    forward = sorted((c for c in candidates if c[0].order >= current_order), key=lambda c: c[0].order)
    if forward:
        return forward[0]
    backward = sorted((c for c in candidates if c[0].order < current_order), key=lambda c: -c[0].order)
    return backward[0] if backward else None


def resolve_toc(registry) -> TocResolution:
    """THE single entry point. Returns an empty TocResolution (toc_path="")
    when the book has no detected Contents page at all - not every book
    has one, and that is not itself an error."""
    toc_doc = next((d for d in registry.documents if d.document_type == "toc"), None)
    if toc_doc is None:
        return TocResolution()

    by_number, by_text = _build_heading_index(registry)
    known_page_labels = frozenset(
        pb.label.lower() for doc in registry.documents for pb in doc.pagebreaks if pb.label)
    result = TocResolution(toc_path=toc_doc.path)
    if toc_doc.tree is None:
        return result

    current_order = 0
    for el in toc_doc.tree.iter():
        if local_name(el.tag) != "a":
            continue
        raw_text = "".join(el.itertext())
        entry = TocEntry(element=el, raw_text=raw_text)
        result.entries.append(entry)

        numbers, residual = _extract_numbers_and_residual(raw_text, known_page_labels)

        # Priority 1/3: a number token that uniquely (or via proximity)
        # identifies a real heading, cross-checked against the residual
        # text so a coincidental page-number collision is never trusted
        # blindly (e.g. "9" as a chapter number vs "9" as a page number).
        chosen = None
        for token in numbers:
            candidates = by_number.get(token)
            if not candidates:
                continue
            if len(candidates) == 1:
                doc, heading = candidates[0]
            else:
                doc, heading = _closest_to(candidates, current_order)
            if residual and _normalize(heading.text) and \
                    difflib.SequenceMatcher(None, residual, _normalize(heading.text)).ratio() < 0.3 and \
                    len(candidates) > 1:
                continue  # ambiguous number match with no textual support - keep looking
            entry.candidates_considered += len(candidates)
            chosen = (doc, heading, "number+text" if residual else "number")
            break

        # Priority 2: exact normalized text match (handles "References",
        # "Introduction", entries with no number at all).
        if chosen is None and residual:
            candidates = by_text.get(residual)
            if candidates:
                doc, heading = (candidates[0] if len(candidates) == 1
                                 else _closest_to(candidates, current_order))
                entry.candidates_considered += len(candidates)
                chosen = (doc, heading, "text")

        # Priority 6: controlled fuzzy match - only when nothing exact
        # matched, only above a high similarity bar, and only when the
        # runner-up isn't nearly as good (never a coin-flip guess).
        if chosen is None and residual:
            scored = []
            for norm_text, candidates in by_text.items():
                ratio = difflib.SequenceMatcher(None, residual, norm_text).ratio()
                if ratio >= FUZZY_MATCH_THRESHOLD:
                    scored.append((ratio, candidates))
            scored.sort(key=lambda x: -x[0])
            if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
                doc, heading = (scored[0][1][0] if len(scored[0][1]) == 1
                                 else _closest_to(scored[0][1], current_order))
                entry.candidates_considered += len(scored[0][1])
                chosen = (doc, heading, "fuzzy")

        if chosen is None:
            result.unresolved += 1
            continue

        doc, heading, method = chosen
        entry.resolved_path = doc.path
        entry.resolved_id = heading.id
        entry.method = method
        current_order = doc.order
        result.resolved += 1

    return result


def apply_toc_resolution(registry, resolution: TocResolution) -> list:
    """Writes each resolved TocEntry's real href="{relative_path}#{id}"
    into its own <a> element (mutating the toc document's own in-memory
    tree) - an unresolved entry's href is left COMPLETELY UNTOUCHED (spec
    6: never invent, never generate a fragment that doesn't exist; spec 13:
    "If not resolvable with confidence, mark REVIEW"). Returns
    [(old_href, new_href), ...] for reporting; a resolved entry whose href
    was ALREADY correct contributes nothing (idempotency - spec 19)."""
    toc_doc = registry.by_path.get(resolution.toc_path)
    changes = []
    if toc_doc is None:
        return changes
    toc_dir = posixpath.dirname(toc_doc.path)
    for entry in resolution.entries:
        if not entry.resolved_path or not entry.resolved_id:
            continue
        rel = posixpath.relpath(entry.resolved_path, toc_dir) if toc_dir else entry.resolved_path
        new_href = f"{rel}#{entry.resolved_id}"
        old_href = entry.element.get("href") or ""
        if old_href == new_href:
            continue
        entry.element.set("href", new_href)
        changes.append((old_href, new_href))
    return changes
