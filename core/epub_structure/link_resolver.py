"""Step 13 (spec section 13) - validates and, where safely possible,
repairs every internal href/src/data reference across every XHTML content
document. Reuses core.epub.href_matching.find_unique_basename_match - the
SAME "exactly one real candidate, or don't touch it" rule already proven
(against a real production EPUB, in this very session) for the zip-based
repair engine - never a second, divergent matching algorithm for the
directory-based case.

Three outcomes per link, exactly matching spec 13's own list:
  - fine: the reference already resolves to a real file (+ a real
    fragment, when one is given).
  - auto-fixed: the file part doesn't exist, but exactly one real file in
    the project matches its basename - repointed in place.
  - REVIEW: anything else (file genuinely missing everywhere, ambiguous
    basename match, or a fragment that doesn't exist in an otherwise-real
    target file) - NEVER guessed, NEVER silently dropped."""
import posixpath
from dataclasses import dataclass, field

from core.epub.href_matching import find_unique_basename_match

_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")


@dataclass
class LinkIssue:
    doc_path: str
    element: object
    attr: str
    old_value: str
    new_value: str = ""
    status: str = ""          # "fixed" / "review"
    reason: str = ""


@dataclass
class LinkResolution:
    checked: int = 0
    valid: int = 0
    fixed: list = field(default_factory=list)     # LinkIssue
    review: list = field(default_factory=list)     # LinkIssue


def _is_external(path_part: str) -> bool:
    lowered = path_part.lower()
    return any(lowered.startswith(p) for p in _EXTERNAL_PREFIXES)


def resolve_links(scan, registry) -> LinkResolution:
    """THE single entry point. Mutates the registry's own in-memory trees
    in place for every auto-fixed reference (the caller is responsible for
    serializing changed documents back to disk later, exactly like every
    other generation step in this pipeline) - a reference marked REVIEW is
    left completely untouched.

    Always reads the CURRENT live attribute value straight off link.element
    - never the LinkRecord.value cached at registry-build time - so this
    still gives an accurate "what's actually still broken" count even when
    an earlier pass (e.g. toc_resolver, which rewrites <a href> directly on
    the same elements) already fixed some of these same references."""
    result = LinkResolution()
    all_names = set(scan.all_files)

    for doc in registry.documents:
        if doc.tree is None:
            continue
        doc_dir = posixpath.dirname(doc.path)
        for link in doc.links:
            current_value = link.element.get(link.attr) or ""
            path_part, _, fragment = current_value.partition("#")
            if not path_part:
                if fragment and fragment not in doc.fragment_ids:
                    result.checked += 1
                    result.review.append(LinkIssue(
                        doc_path=doc.path, element=link.element, attr=link.attr, old_value=current_value,
                        status="review", reason=f"fragment '#{fragment}' does not exist in this same document"))
                else:
                    result.checked += 1
                    result.valid += 1
                continue
            if _is_external(path_part):
                continue

            result.checked += 1
            resolved = posixpath.normpath(posixpath.join(doc_dir, path_part)) if doc_dir else posixpath.normpath(path_part)
            if resolved in all_names:
                target_doc = registry.by_path.get(resolved)
                if fragment and target_doc is not None and fragment not in target_doc.fragment_ids:
                    result.review.append(LinkIssue(
                        doc_path=doc.path, element=link.element, attr=link.attr, old_value=current_value,
                        status="review",
                        reason=f"'{resolved}' exists but has no id='{fragment}'"))
                else:
                    result.valid += 1
                continue

            match = find_unique_basename_match(all_names, resolved, exclude={doc.path})
            if match:
                new_value = posixpath.relpath(match, doc_dir) if doc_dir else match
                if fragment:
                    new_value += f"#{fragment}"
                link.element.set(link.attr, new_value)
                issue = LinkIssue(doc_path=doc.path, element=link.element, attr=link.attr,
                                   old_value=current_value, new_value=new_value, status="fixed",
                                   reason="exactly one real file elsewhere in the project matches this reference's name")
                link.value = new_value
                result.fixed.append(issue)
            else:
                result.review.append(LinkIssue(
                    doc_path=doc.path, element=link.element, attr=link.attr, old_value=current_value,
                    status="review", reason="target file does not exist anywhere in the project (no unique match)"))

    return result
