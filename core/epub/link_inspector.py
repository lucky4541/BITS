"""Best-effort SOURCE/TARGET description for broken-link-style findings
(spec: "EPUBForge - PHASE 4 ONLY - Real EPUB Editor" - "For broken links:
show SOURCE and TARGET"). NOT a new validator or classification system -
reads straight off the SAME core.epub.error_analyzer.ErrorAnalysis a
finding already has.

TARGET extraction is deliberately best-effort and layered, most-reliable
first:
  1. An href="...#fragment"-style attribute in the finding's OWN real
     snippet (core.epub.error_analyzer's ErrorContext.snippet - actual
     lines read from the actual file around the reported line/column) -
     this is the broken reference's own real markup, the most trustworthy
     source available.
  2. The manifest item's OWN current href, when the finding is about a
     manifest entry (ErrorContext.manifest_id).
  3. The last quoted substring in EPUBCheck's raw message/context text
     (confirmed against real EPUBCheck output: e.g. Quick Validator's own
     "Manifest item 'nav' references 'nav.xhtml'..." quotes the target).
If none of these find anything, "—" is shown rather than a guess - never
invents a target that isn't actually present in real data."""
import re

_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']')

# Categories core.epub.error_analyzer classifies that represent SOME kind
# of reference (manifest href, fragment, or an undeclared file another
# document might already be linking to) - the only ones worth a
# SOURCE/TARGET display at all.
LINK_CATEGORIES = {"BROKEN_HREF_FIXABLE", "MISSING_RESOURCE", "BROKEN_FRAGMENT", "UNDECLARED_RESOURCE"}


def is_link_finding(analysis) -> bool:
    return analysis.root_cause.category in LINK_CATEGORIES


def describe_source(analysis) -> str:
    ctx = analysis.context
    if not ctx.file:
        return "—"
    loc = ctx.file
    if ctx.line and ctx.line > 0:
        loc += f":{ctx.line}"
        if ctx.column and ctx.column > 0:
            loc += f":{ctx.column}"
    return loc


def describe_target(analysis, package) -> str:
    ctx = analysis.context

    href_matches = _HREF_RE.findall(ctx.snippet or "")
    if href_matches:
        fragment_matches = [h for h in href_matches if "#" in h]
        return (fragment_matches or href_matches)[-1]

    if ctx.manifest_id and package is not None:
        item = next((i for i in package.manifest if i.id == ctx.manifest_id), None)
        if item and item.href:
            return item.href

    for text in (analysis.error.raw_context, analysis.error.message):
        matches = _QUOTED_RE.findall(text or "")
        if matches:
            return matches[-1]

    return "—"
