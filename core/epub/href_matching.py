"""A single, tiny shared helper for 'is this broken href/manifest entry
deterministically fixable' (spec: "EPUBForge - PHASE 3 - Universal
Auto-Fix Engine" example "broken href with deterministic target").

Used by BOTH core.epub.error_analyzer (at CLASSIFICATION time, to decide
whether a broken-resource finding is SAFE_AUTO_FIX or only REVIEW_REQUIRED)
and core.epub.repair_strategies (at REPAIR time, to actually pick the
replacement target) - kept in one neutral module so neither has to
duplicate the other's basename-matching logic (spec 1: "ONE implementation
only").

'Deterministic' here means exactly one real file in the package matches
the broken reference's own basename, case-insensitively - never a fuzzy/
best-guess match, and never a decision that could silently point at the
wrong file when several real candidates exist."""
import posixpath


def find_unique_basename_match(zip_names, broken_path: str, exclude=()) -> str:
    """zip_names: every real entry name in the package (e.g. package.
    zip_names). broken_path: the (nonexistent) resolved path a reference
    pointed at. Returns the single real entry whose basename matches
    broken_path's basename case-insensitively, or "" if there is no match
    or more than one (ambiguous - never guessed)."""
    if not broken_path:
        return ""
    target_name = posixpath.basename(broken_path).lower()
    candidates = [
        name for name in zip_names
        if name not in exclude and posixpath.basename(name).lower() == target_name
    ]
    return candidates[0] if len(candidates) == 1 else ""


def has_any_deterministic_manifest_fix(package) -> bool:
    """Package-wide check used by core.epub.error_analyzer's classification:
    True if AT LEAST ONE manifest item's href is broken (resolves to
    nothing in the package) but has exactly one real, unambiguous
    replacement candidate elsewhere in the archive."""
    for item in package.manifest:
        href = item.href or ""
        if not href:
            continue
        resolved = package.resolve_href(href)
        if resolved and resolved not in package.zip_names:
            if find_unique_basename_match(package.zip_names, resolved, exclude={package.opf_path}):
                return True
    return False
