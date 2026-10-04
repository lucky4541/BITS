"""Universal Error Analyzer + Root Cause Analysis (spec: "EPUBForge -
PHASE 2 ONLY - Universal Error Analyzer + Root Cause Analysis").

ANALYSIS ONLY (spec 5: "Do not automatically modify the EPUB... Create
RepairPlan objects but do not execute them") - this module never writes
to the package, never calls anything in core.epub.package_reader's own
read-only contract beyond reading it, and produces plain data (ErrorAnalysis)
for the Validation UI to display. No RepairEngine/RepairExecutor/
PackageBuilder exists yet - those are explicitly a LATER phase.

Operates on a single common AnalyzableError shape built from EITHER
core.epub.quick_validator.QuickValidationIssue OR core.epubcheck.parser.
EpubCheckMessage (spec 1: "Do not create duplicate validation systems") -
one analysis pipeline for both tools' findings, never two.

Deliberately NOT a fixed per-code lookup table (spec 2: "Do not hard-code
only RSC-005/RSC-001/RSC-012... arbitrary EPUBCheck codes... unknown codes
must not crash"): root-cause classification matches on CODE FAMILY
PREFIXES (RSC-/OPF-/NAV-/NCX-/HTM-/CSS-/MED-/ACC-/QV-) and message
keywords for the well-known EPUBCheck error classes, falling back to a
generic, honest RootCause.UNCLASSIFIED + Repairability.UNKNOWN for
anything this module doesn't yet recognize - never an exception, never a
guess dressed up as certainty."""
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from enum import Enum

from core.epub import href_matching

# RSC-007's own fixed message shape, confirmed directly against a real
# epubcheck.jar run: 'Referenced resource "X" could not be found in the
# EPUB.' - X is EPUBCheck's own resolved path, quoted verbatim, whether the
# broken reference came from the OPF manifest or from inside a content
# document's own img/src (etc.) attribute.
_MISSING_RESOURCE_PATH_RE = re.compile(r'[Rr]eferenced resource "([^"]+)" could not be found')


class Repairability(str, Enum):
    SAFE_AUTO_FIX = "SAFE_AUTO_FIX"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class AnalyzableError:
    """A common wrapper over a core.epub.quick_validator.QuickValidationIssue
    OR a core.epubcheck.parser.EpubCheckMessage - the Error Analyzer
    operates on this ONE shape regardless of which tool found the issue."""
    tool: str          # "Quick" | "EPUBCheck"
    code: str
    severity: str
    message: str
    file: str = ""
    line: int = -1
    column: int = -1
    raw_context: str = ""   # EpubCheckMessage.context verbatim (spec: PHASE 4 "show SOURCE and TARGET" - the
                             # surrounding markup EPUBCheck itself reported; "" for Quick issues, which have none)


def from_quick_issue(issue) -> AnalyzableError:
    return AnalyzableError(tool="Quick", code=issue.code, severity=issue.severity,
                            message=issue.message, file=issue.file)


def from_epubcheck_message(msg) -> AnalyzableError:
    return AnalyzableError(tool="EPUBCheck", code=msg.code, severity=msg.severity,
                            message=msg.message, file=msg.file, line=msg.line, column=msg.column,
                            raw_context=msg.context)


@dataclass
class ErrorContext:
    file: str = ""
    line: int = -1
    column: int = -1
    snippet: str = ""              # a few real lines of the actual file content around the error, "" if unavailable
    manifest_id: str = ""           # the matching manifest item's id, if `file` resolves to one
    is_spine_member: bool = False
    is_opf: bool = False
    is_nav: bool = False
    is_ncx: bool = False


@dataclass
class RootCause:
    category: str                   # short internal classification, e.g. "DUPLICATE_ID", "MISSING_RESOURCE"
    explanation: str
    is_cascading: bool = False        # True if this looks like a SYMPTOM of a separate, earlier root-cause error
    cascade_source_code: str = ""      # the code of the TRUE root-cause error, when is_cascading


@dataclass
class RepairPlan:
    repairability: Repairability
    summary: str
    proposed_action: str = ""        # human-readable description of what a FUTURE repair pass would do - never executed here
    deterministic: bool = False


@dataclass
class ErrorAnalysis:
    error: AnalyzableError
    context: ErrorContext
    root_cause: RootCause
    repair_plan: RepairPlan


def _find_opf_root_error(package, errors: list):
    """Spec 3: 'Do not independently repair cascading errors... Broken OPF
    -> manifest errors -> spine errors -> NAV errors... Repair the OPF
    root cause first.' Detects a package-level failure that would make
    every OTHER manifest/spine/navigation-related finding an unreliable,
    likely-downstream symptom rather than an independent problem of its
    own: either the OPF itself could not even be read at all (package.
    error), or EPUBCheck reported a FATAL specifically located at the OPF
    file.

    Deliberately FATAL only, never a plain ERROR (confirmed as a real
    false positive during testing): an ordinary ERROR whose reported
    location happens to be the OPF - e.g. Quick Validator's own QV-0xx
    codes, which always attribute a broken manifest/spine REFERENCE to
    the OPF simply because that's where the reference itself lives - does
    NOT mean the OPF is malformed, only that one of its entries points
    somewhere invalid. That is a genuinely independent finding of its
    own, not a symptom of a broken OPF, and wrongly promoting it to a
    package-wide root cause would cascade-mark every unrelated finding
    in the whole report."""
    if package.error:
        # core.epub.quick_validator.validate() already reports this exact
        # condition as a real QV-000 issue in its own results - reuse THAT
        # object (by identity) rather than synthesizing a second, distinct
        # one with the same content: `err is not opf_root_error` below
        # would otherwise treat the genuine QV-000 finding as cascading
        # from a different-identity copy of itself.
        for err in errors:
            if err.code == "QV-000":
                return err
        return AnalyzableError(tool="Quick", code="QV-000", severity="ERROR",
                                message=package.error, file=package.path)
    opf_name = os.path.basename(package.opf_path) if package.opf_path else None
    if not opf_name:
        return None
    for err in errors:
        if err.severity == "FATAL" and err.file and os.path.basename(err.file) == opf_name:
            return err
    return None


def _read_snippet(epub_path: str, file: str, line: int, context_lines: int = 2) -> str:
    """Reads a small window of REAL lines from the actual file inside the
    EPUB archive, around EPUBCheck's own reported (1-indexed) line number -
    the 'inspect surrounding content' step (spec 3.2). Returns "" (never
    raises) if the file/line can't be resolved - a missing snippet is not
    itself an error, just less context to show."""
    if not file or not line or line <= 0:
        return ""
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            if file not in zf.namelist():
                return ""
            raw = zf.read(file)
        text_lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(0, line - 1 - context_lines)
        end = min(len(text_lines), line + context_lines)
        window = text_lines[start:end]
        numbered = [f"{start + i + 1:>5}: {text_lines[start + i]}" for i in range(len(window))]
        return "\n".join(numbered)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError):
        return ""


def _build_context(epub_path: str, package, err: AnalyzableError) -> ErrorContext:
    ctx = ErrorContext(file=err.file, line=err.line, column=err.column)
    if err.file:
        opf_base = os.path.basename(package.opf_path) if package.opf_path else None
        ctx.is_opf = bool(opf_base) and os.path.basename(err.file) == opf_base
        ctx.is_nav = bool(package.nav_path) and (err.file == package.nav_path)
        ctx.is_ncx = bool(package.ncx_path) and (err.file == package.ncx_path)
        for item in package.manifest:
            resolved = package.resolve_href(item.href) if package.opf_path else ""
            if resolved == err.file or item.href == err.file or posixpath.basename(item.href) == posixpath.basename(err.file):
                ctx.manifest_id = item.id
                break
        if ctx.manifest_id:
            ctx.is_spine_member = any(ref.idref == ctx.manifest_id for ref in package.spine)
    ctx.snippet = _read_snippet(epub_path, err.file, err.line)
    return ctx


def _classify_root_cause(err: AnalyzableError, context: ErrorContext, package, opf_root_error) -> RootCause:
    if opf_root_error is not None and err is not opf_root_error and not context.is_opf:
        opf_display = os.path.basename(package.opf_path) if package.opf_path else "the package document"
        return RootCause(
            category="OPF_CASCADE", is_cascading=True, cascade_source_code=opf_root_error.code,
            explanation=(f"The package's own OPF ({opf_display}) has its own error ({opf_root_error.code}). "
                         f"Manifest/spine/navigation errors reported alongside it may just be downstream "
                         f"symptoms of that - fix the OPF first, then re-validate, before treating this as "
                         f"an independent problem."))

    code = (err.code or "").upper()
    msg = (err.message or "").lower()

    # spec: "EPUBForge - PHASE 3 - Universal Auto-Fix Engine" section 2's
    # SAFE-repair examples - deterministic, mechanical, structure-only
    # categories, checked BEFORE the coarser REVIEW_REQUIRED/MANUAL_REQUIRED
    # buckets below so a package that qualifies for an automatic fix is
    # never shadowed by a less-specific classification.
    # Closed-loop auto-fix categories (core.epub.repair_engine / autofix_strategies),
    # matched on EPUBCheck's own message wording - codes such as RSC-005 / RSC-016
    # cover many different problems, so the code alone never decides.
    if code in ("PKG-006", "PKG-007") or ("mimetype" in msg and code.startswith("PKG-")):
        return RootCause("MIMETYPE_STRUCTURE",
                          "The 'mimetype' entry is missing, not first, compressed or has the wrong content "
                          "(EPUB OCF container rule).")
    if "entity" in msg and "referenced" in msg and "declared" in msg:
        return RootCause("XML_ENTITY",
                          "An HTML named entity (e.g. &nbsp;) is not defined in XHTML, so the whole document "
                          "cannot be parsed - every link INTO this file is reported broken as a consequence. "
                          "Replacing it by the numeric reference of the same character is lossless.")
    if ("must be terminated by the matching end-tag" in msg or "end-tag" in msg or "must be followed by" in msg
            or "markup in the document following the root element" in msg or "element type" in msg and
            "must be" in msg):
        return RootCause("XHTML_TAG_STRUCTURE",
                          "The document is not well-formed (an element is not closed or tags are wrongly "
                          "nested); links into it are reported broken as a consequence.")
    if code.startswith("RSC-020") or "is not a valid url" in msg:
        return RootCause("URI_SYNTAX",
                          "A reference is not a valid URL (backslash path separator, unescaped space or "
                          "character, absolute filesystem path).")
    if code.startswith("RSC-008") or "is not declared in the opf manifest" in msg:
        return RootCause("UNDECLARED_RESOURCE",
                          "A file that exists in the package is used but not declared in the OPF manifest.")
    if "itemref refers to the same manifest entry" in msg:
        return RootCause("DUPLICATE_SPINE",
                          "The same document is listed more than once in the spine (reading order).")
    if code.startswith("OPF-029") or "does not appear to match the media type" in msg:
        return RootCause("MEDIA_TYPE_MISMATCH",
                          "The media-type declared in the manifest does not match the file's real content.")
    if code.startswith("OPF-030") or "unique-identifier attribute does not resolve" in msg:
        return RootCause("UNIQUE_IDENTIFIER",
                          "package@unique-identifier does not name the id of a dc:identifier element.")
    if 'value of attribute "role" is invalid' in msg:
        return RootCause("INVALID_ROLE",
                          "An ARIA role that is not allowed on that element (e.g. doc-chapter on a link).")
    if "missing required element \"dc:identifier\"" in msg:
        return RootCause("MISSING_IDENTIFIER",
                          "The package has no dc:identifier (the book's ISBN or other unique identifier).")
    if "dcterms:modified" in msg:
        return RootCause("METADATA_MODIFIED",
                          "EPUB 3 requires exactly one dcterms:modified (last-modified date) in the OPF metadata.")
    if code.startswith("CSS-008") or ("css" in msg and "premature end" in msg):
        return RootCause("CSS_SYNTAX",
                          "A stylesheet ends prematurely (unclosed rule block or comment).")
    if code in ("QV-001", "QV-002", "QV-003", "QV-004"):
        return RootCause("MIMETYPE_STRUCTURE",
                          "The package's 'mimetype' entry itself (presence, position, compression, or exact "
                          "content) does not meet the EPUB OCF container requirement - a fixed, mechanical "
                          "structural rule, never book content.")
    if code == "QV-023":
        return RootCause("UNDECLARED_RESOURCE",
                          "A real file already present in the package is simply not declared in the OPF "
                          "manifest yet - the file's own extension deterministically implies its media type.")
    # RSC-005 is EPUBCheck's GENERIC schema-validation-error code - it
    # covers everything from a real duplicate id to "playOrder sequence has
    # gaps" to "missing required element" (all confirmed directly against
    # real epubcheck.jar output). Never trust the code prefix alone here;
    # only the message's own actual wording tells you what broke.
    if "duplicate id" in msg:
        return RootCause("DUPLICATE_ID",
                          "The same id attribute value is used on more than one element within the same "
                          "document, which makes fragment references to it ambiguous.")
    if "fragment identifier is not defined" in msg or code.startswith("RSC-012"):
        return RootCause("BROKEN_FRAGMENT",
                          'A link (href="...#id") points at a fragment identifier that does not exist as a '
                          "real id anywhere in the target document.")
    if code in ("QV-021", "QV-022") or code.startswith("RSC-001"):
        if href_matching.has_any_deterministic_manifest_fix(package):
            return RootCause("BROKEN_HREF_FIXABLE",
                              "A manifest item's href points at a file that does not exist, but exactly one "
                              "real file elsewhere in the package matches its name (a case or path mismatch) "
                              "- an unambiguous, deterministic target.")
        return RootCause("MISSING_RESOURCE",
                          "A file referenced by the package (manifest, spine, or a link/href) does not "
                          "actually exist inside the EPUB archive.")
    if "could not be found" in msg or code == "QV-030":
        # spec 2 example: 'broken href with deterministic target', applied
        # here to a CONTENT-DOCUMENT reference (img/src, svg image/xlink:
        # href, source/src, object/data) rather than an OPF manifest href
        # (BROKEN_HREF_FIXABLE above already covers that case) - a real,
        # observed defect: an extra/duplicated directory segment (e.g.
        # "images/images/fig.png") where the real file exists at the
        # correct, shorter path. Only classified SAFE_AUTO_FIX when EPUBCheck's
        # own quoted path has EXACTLY ONE real, unambiguous candidate
        # elsewhere in the archive - never a fuzzy/best guess.
        loc_match = _MISSING_RESOURCE_PATH_RE.search(err.message or "")
        if loc_match and href_matching.find_unique_basename_match(
                package.zip_names, loc_match.group(1), exclude={package.opf_path}):
            return RootCause("BROKEN_CONTENT_REFERENCE_FIXABLE",
                              "A content document references a file that does not exist at that exact path, "
                              "but exactly one real file elsewhere in the package matches its name (e.g. a "
                              "duplicated directory segment) - an unambiguous, deterministic target.")
        return RootCause("MISSING_RESOURCE",
                          "A file referenced by the package (manifest, spine, or a link/href) does not "
                          "actually exist inside the EPUB archive.")
    if context.is_opf or code.startswith("OPF-") or code in ("QV-010", "QV-011", "QV-012", "QV-020", "QV-050", "QV-051"):
        return RootCause("OPF_STRUCTURE",
                          "The package document (OPF) itself has a structural problem - manifest, spine, "
                          "or metadata entries, or the document's own well-formedness.")
    if context.is_nav or code.startswith("NAV-") or code == "QV-040":
        return RootCause("NAV_STRUCTURE", "The EPUB 3 navigation document (nav.xhtml) has a structural problem.")
    if (context.is_ncx or code.startswith("NCX-")) and "playorder" in msg:
        return RootCause("NCX_PLAYORDER",
                          "The NCX's navPoint playOrder values are inconsistent with the document's own real "
                          "order - deterministically fixable by renumbering them sequentially.")
    if context.is_ncx or code.startswith("NCX-"):
        return RootCause("NCX_STRUCTURE",
                          "The NCX navigation document has a structural problem (e.g. playOrder, navPoint targets).")
    if code.startswith("MED-"):
        return RootCause("MEDIA_TYPE",
                          "A declared or actual media type for a resource does not match what EPUBCheck detected.")
    if code.startswith("ACC-"):
        return RootCause("ACCESSIBILITY",
                          "An accessibility-related rule (e.g. missing alternative text, ARIA usage) was "
                          "flagged - this always requires real authored content, never a mechanical fix.")
    if code.startswith("HTM-") or code.startswith("CSS-"):
        return RootCause("CONTENT_MARKUP",
                          "A well-formedness or markup rule was violated inside an actual content document "
                          "(XHTML/CSS).")
    if code.startswith("QV-000"):
        return RootCause("PACKAGE_UNREADABLE", "The package itself could not be opened/parsed at all.")
    if code.startswith("QV-"):
        return RootCause("PACKAGE_STRUCTURE",
                          "Flagged directly by EPUBForge's own Quick Validator against the package's own "
                          "manifest/spine/mimetype structure.")
    return RootCause("UNCLASSIFIED",
                      "EPUBForge does not yet have a specific root-cause pattern for this code - the message "
                      "above is EPUBCheck's own diagnosis, shown as-is.")


_REPAIRABILITY_BY_CATEGORY = {
    "MIMETYPE_STRUCTURE": (Repairability.SAFE_AUTO_FIX,
                            "A fixed, spec-mandated literal ('application/epub+zip', stored uncompressed, "
                            "first in the archive) - never guessed, never book content."),
    "UNDECLARED_RESOURCE": (Repairability.SAFE_AUTO_FIX,
                             "The file already exists; only a manifest <item> entry needs adding, with a "
                             "media type deterministically implied by the file's own extension."),
    "BROKEN_HREF_FIXABLE": (Repairability.SAFE_AUTO_FIX,
                             "Exactly one real file in the package unambiguously matches the broken "
                             "reference's name - repointing the href loses no data and invents nothing."),
    "NCX_PLAYORDER": (Repairability.SAFE_AUTO_FIX,
                       "playOrder values are renumbered sequentially by the document's own existing order - "
                       "purely mechanical, never a content decision."),
    "XML_ENTITY": (Repairability.SAFE_AUTO_FIX,
                   "Each named entity becomes the numeric reference of the identical character - no text "
                   "changes."),
    "XHTML_TAG_STRUCTURE": (Repairability.SAFE_AUTO_FIX,
                            "Closed / re-nested only where the correct structure is unambiguous (the "
                            "XHTML repair strategy marks everything else REVIEW); text and formatting kept."),
    "URI_SYNTAX": (Repairability.SAFE_AUTO_FIX,
                   "The reference is rewritten as a valid relative URL to the SAME existing file."),
    "DUPLICATE_SPINE": (Repairability.SAFE_AUTO_FIX,
                        "The later duplicate itemref is removed; the document stays in the reading order once."),
    "MEDIA_TYPE_MISMATCH": (Repairability.REVIEW_REQUIRED,
                            "Corrected automatically only when the file's own bytes and its extension name "
                            "the same type; otherwise the file needs review."),
    "UNIQUE_IDENTIFIER": (Repairability.SAFE_AUTO_FIX,
                          "Pointed at the package's only dc:identifier; nothing is invented."),
    "INVALID_ROLE": (Repairability.REVIEW_REQUIRED,
                     "A role not allowed on a link is removed from that link (attribute only); other elements "
                     "need review."),
    "MISSING_IDENTIFIER": (Repairability.MANUAL_REQUIRED,
                           "The identifier (ISBN) must come from the publisher - never invented."),
    "METADATA_MODIFIED": (Repairability.SAFE_AUTO_FIX,
                          "A missing modification date is added (package metadata, not content); several are "
                          "only reported, never deleted."),
    "CSS_SYNTAX": (Repairability.SAFE_AUTO_FIX,
                   "Only a missing closing brace / comment end is appended; no rule is removed or changed."),
    "OPF_CASCADE": (Repairability.REVIEW_REQUIRED,
                    "Resolve the OPF root-cause error first, then re-validate - this finding may disappear "
                    "or change once that's fixed."),
    "DUPLICATE_ID": (Repairability.SAFE_AUTO_FIX,
                      "A duplicate id can usually be resolved deterministically by renaming the later "
                      "occurrence and updating matching fragment references."),
    "BROKEN_FRAGMENT": (Repairability.REVIEW_REQUIRED,
                         "Repaired automatically only when exactly ONE target id can be identified (URL "
                         "encoding, letter case, id moved by a split, id renamed); otherwise the link needs "
                         "review - a missing id is never invented."),
    "MISSING_RESOURCE": (Repairability.REVIEW_REQUIRED,
                          "Repaired automatically only when exactly ONE existing file is the intended target "
                          "(letter case, wrong directory, stale split file holding the #fragment); a missing "
                          "file is never invented and the reference is never removed."),
    "BROKEN_CONTENT_REFERENCE_FIXABLE": (Repairability.SAFE_AUTO_FIX,
                                          "Exactly one real file in the package unambiguously matches the "
                                          "broken content-document reference's name - repointing the src/href "
                                          "loses no data and invents nothing."),
    "OPF_STRUCTURE": (Repairability.MANUAL_REQUIRED,
                       "OPF structural issues often require understanding the book's own intended "
                       "structure and are not safely auto-fixable without risking further damage."),
    "NAV_STRUCTURE": (Repairability.REVIEW_REQUIRED,
                       "Navigation issues may be derivable from the existing heading/TOC structure, but "
                       "require confirmation before applying."),
    "NCX_STRUCTURE": (Repairability.REVIEW_REQUIRED,
                       "NCX issues (e.g. playOrder) are often mechanically derivable but require "
                       "confirmation before applying."),
    "MEDIA_TYPE": (Repairability.REVIEW_REQUIRED,
                    "The manifest's declared media type may just need correcting to match the real file - "
                    "confirm before applying."),
    "ACCESSIBILITY": (Repairability.UNSUPPORTED,
                       "Accessibility content (e.g. alternative text) requires real authored content "
                       "EPUBForge must never invent."),
    "CONTENT_MARKUP": (Repairability.REVIEW_REQUIRED,
                        "Markup well-formedness issues can sometimes be mechanically fixed, but risk "
                        "altering real content and need confirmation."),
    "PACKAGE_UNREADABLE": (Repairability.MANUAL_REQUIRED,
                            "The package itself is not readable - this needs direct investigation, not an "
                            "automated fix."),
    "PACKAGE_STRUCTURE": (Repairability.REVIEW_REQUIRED,
                           "A package-structure issue found by Quick Validator - repairability depends on "
                           "the specific missing/invalid element."),
    "UNCLASSIFIED": (Repairability.UNKNOWN,
                      "EPUBForge has no repair-pattern knowledge for this error code yet."),
}


def _classify_repairability(root_cause: RootCause) -> RepairPlan:
    repairability, summary = _REPAIRABILITY_BY_CATEGORY.get(
        root_cause.category, (Repairability.UNKNOWN, "EPUBForge has no repair-pattern knowledge for this error."))
    proposed = ""
    if root_cause.category == "DUPLICATE_ID":
        proposed = "Rename the duplicate id to a unique value; update matching href=\"#id\" references."
    elif root_cause.category == "MIMETYPE_STRUCTURE":
        proposed = "Restore/fix the 'mimetype' entry (content, position, and compression)."
    elif root_cause.category == "UNDECLARED_RESOURCE":
        proposed = "Add a manifest <item> entry for the existing file, with a media type from its extension."
    elif root_cause.category == "BROKEN_HREF_FIXABLE":
        proposed = "Repoint the manifest item's href to the one matching real file found in the package."
    elif root_cause.category == "BROKEN_CONTENT_REFERENCE_FIXABLE":
        proposed = "Repoint the content document's src/href attribute to the one matching real file found in the package."
    elif root_cause.category == "NCX_PLAYORDER":
        proposed = "Renumber every navPoint's playOrder sequentially, in the NCX's own document order."
    return RepairPlan(repairability=repairability, summary=summary, proposed_action=proposed,
                       deterministic=(repairability == Repairability.SAFE_AUTO_FIX))


def analyze_errors(epub_path: str, package, errors: list) -> list:
    """THE single entry point. `errors`: a flat list of AnalyzableError
    (built via from_quick_issue/from_epubcheck_message by the caller).
    Returns one ErrorAnalysis per error, in the SAME order - a pure,
    read-only pass; never touches the original EPUB (spec 5)."""
    opf_root_error = _find_opf_root_error(package, errors)
    analyses = []
    for err in errors:
        context = _build_context(epub_path, package, err)
        root_cause = _classify_root_cause(err, context, package, opf_root_error)
        repair_plan = _classify_repairability(root_cause)
        analyses.append(ErrorAnalysis(error=err, context=context, root_cause=root_cause, repair_plan=repair_plan))
    return analyses
