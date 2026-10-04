"""REPAIR PLAN for the closed-loop auto-fix engine: turns one validation
snapshot (core.epub.validation_runner) into root-cause groups, each with a
safety LEVEL, the strategy that can repair it, its priority and a plain
description - BEFORE anything is modified.

  LEVEL 1  SAFE            deterministic; applied automatically (batched)
  LEVEL 2  HIGH CONFIDENCE applied automatically, each in its own transaction
                           with EPUBCheck re-run immediately after it
  LEVEL 3  MEDIUM          suggestion only - needs the user's approval
  LEVEL 4  LOW / UNKNOWN   never modified automatically - manual review

Root causes: several EPUBCheck messages caused by one problem become ONE
group (all duplicate-ID messages for one id; every "fragment not defined"
pointing INTO a document that is not well-formed is a symptom of that
document's XML error, not a separate link problem). Groups are ordered by
dependency priority (package/container -> OPF -> missing resources ->
URIs -> XML -> duplicate IDs -> fragments -> links -> navigation -> CSS).
"""
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from core.epub import autofix_strategies as A
from core.epub.repair_strategies import (fix_broken_manifest_hrefs, fix_mimetype, fix_ncx_playorder,
                                         fix_undeclared_resources)
from core.epub.xhtml_repair_strategy import fix_xhtml_document

# category -> (level, priority, strategy, strategy label, recommended manual action)
CATALOG = {
    "PACKAGE_HYGIENE": (1, 0, A.fix_package_hygiene, "package hygiene",
                        "Remove the temporary / OS file from the package."),
    "MIMETYPE_STRUCTURE": (1, 1, fix_mimetype, "mimetype entry", "Repackage with 'mimetype' first and stored."),
    "BROKEN_HREF_FIXABLE": (1, 2, fix_broken_manifest_hrefs, "manifest href", "Correct the manifest href."),
    "UNDECLARED_RESOURCE": (1, 2, fix_undeclared_resources, "manifest entry", "Declare the file in the OPF."),
    "DUPLICATE_SPINE": (1, 2, A.fix_duplicate_spine, "spine duplicate", "Remove the duplicate itemref."),
    "MEDIA_TYPE_MISMATCH": (2, 2, A.fix_media_types, "media type",
                            "Check the file: re-save it in the declared format or correct the media-type."),
    "BROKEN_CONTENT_REFERENCE_FIXABLE": (2, 3, A.fix_resource_references, "resource reference",
                                         "Point the reference at the correct file."),
    "MISSING_RESOURCE": (2, 3, A.fix_resource_references, "resource reference",
                         "Restore the missing file into the package or correct the reference - "
                         "never delete the reference to silence the error."),
    "URI_SYNTAX": (1, 4, A.fix_resource_references, "URI syntax", "Write the reference as a relative URL."),
    "XML_ENTITY": (1, 5, A.fix_named_entities, "named entities", "Replace the entity by a numeric reference."),
    "XHTML_TAG_STRUCTURE": (1, 5, fix_xhtml_document, "XHTML structure",
                            "Close / re-nest the element by hand in the source editor (Edit EPUB)."),
    "DUPLICATE_ID": (2, 6, A.fix_duplicate_ids_safely, "duplicate id",
                     "Give the second element its own id and check which links should point to it."),
    "BROKEN_FRAGMENT": (2, 7, A.fix_broken_fragments, "fragment link",
                        "Add the id to the intended element, or point the link at the right one."),
    "UNIQUE_IDENTIFIER": (1, 2, A.fix_unique_identifier, "unique-identifier",
                          "Make package@unique-identifier name the dc:identifier's id."),
    "MISSING_IDENTIFIER": (4, 2, None, "", "Enter the book's ISBN (EPUB Structure: Normal ISBN) and "
                                          "regenerate, or add a <dc:identifier> to the OPF."),
    "INVALID_ROLE": (2, 8, A.fix_invalid_link_roles, "link role",
                     "Remove or correct the role attribute named in the message."),
    "METADATA_MODIFIED": (1, 2, A.fix_dcterms_modified, "dcterms:modified",
                          "Keep exactly one <meta property=\"dcterms:modified\"> in the OPF metadata."),
    "NCX_PLAYORDER": (1, 9, fix_ncx_playorder, "NCX playOrder", "Renumber playOrder."),
    "CSS_SYNTAX": (1, 11, A.fix_css_syntax, "CSS syntax", "Fix the stylesheet syntax by hand."),
    "NAV_STRUCTURE": (3, 8, None, "", "Correct the navigation document (nav.xhtml)."),
    "NCX_STRUCTURE": (3, 9, None, "", "Correct the NCX navigation."),
    "OPF_STRUCTURE": (3, 2, None, "", "Correct the package document (OPF) by hand."),
    "OPF_CASCADE": (3, 2, None, "", "Fix the OPF error first; this one may then disappear."),
    "PACKAGE_STRUCTURE": (3, 1, None, "", "Check the package structure."),
    "CONTENT_MARKUP": (3, 10, None, "", "Correct the markup in the source editor (Edit EPUB)."),
    "MEDIA_TYPE": (3, 2, None, "", "Correct the media type."),
    "ACCESSIBILITY": (4, 12, None, "", "Author the missing accessibility content (e.g. alt text)."),
    "PACKAGE_UNREADABLE": (4, 0, None, "", "The archive cannot be read - rebuild the package."),
    "UNCLASSIFIED": (4, 13, None, "", "Read EPUBCheck's message and fix by hand."),
}
LEVEL_NAMES = {1: "SAFE", 2: "HIGH CONFIDENCE", 3: "MEDIUM - needs approval", 4: "LOW - manual review"}
BLOCKING = ("FATAL", "ERROR")
PARSE_CATEGORIES = ("XML_ENTITY", "XHTML_TAG_STRUCTURE")


@dataclass
class PlanGroup:
    key: str
    category: str
    level: int
    priority: int
    strategy: object
    strategy_label: str
    files: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    errors: list = field(default_factory=list)          # ErrorAnalysis
    explanation: str = ""
    action: str = ""
    manual_action: str = ""
    cascade_of: str = ""                                # key of the root group this is a symptom of
    status: str = "planned"                             # planned | committed | rolled back | review | blocked

    @property
    def n(self):
        return len(self.errors)

    @property
    def automatic(self):
        return self.level <= 2 and self.strategy is not None

    def to_dict(self):
        return {"key": self.key, "category": self.category, "level": self.level,
                "level_name": LEVEL_NAMES[self.level], "priority": self.priority,
                "strategy": self.strategy_label, "files": self.files, "codes": sorted(set(self.codes)),
                "errors": self.n, "messages": self.messages[:5], "explanation": self.explanation,
                "action": self.action, "manual_action": self.manual_action, "cascade_of": self.cascade_of,
                "status": self.status}


def _is_blocking(a):
    return (a.error.severity or "").upper() in BLOCKING


def _hrefs_on_line(epub_path, file, line):
    if not file or line is None or line < 1:
        return []
    try:
        with zipfile.ZipFile(epub_path) as z:
            name = file if file in z.namelist() else next((n for n in z.namelist() if n.endswith(file)), None)
            if not name:
                return []
            text = z.read(name).decode("utf-8", errors="replace").splitlines()
    except (OSError, zipfile.BadZipFile):
        return []
    if line > len(text):
        return []
    base = posixpath.dirname(name)
    out = []
    for h in re.findall(r"""(?:href|src)\s*=\s*["']([^"']*)["']""", text[line - 1]):
        path = h.partition("#")[0]
        out.append(name if not path else posixpath.normpath(posixpath.join(base, unquote(path))))
    return out


def build_plan(snapshot, include_warnings=False) -> list:
    """Root-cause groups for every blocking finding of a validation
    snapshot (EPUBCheck + Quick Validator), ordered by priority."""
    groups = {}
    analyses = [a for a in snapshot.all_analyses if include_warnings or _is_blocking(a)
                or a.root_cause.category in ("UNDECLARED_RESOURCE", "MIMETYPE_STRUCTURE")]
    for a in analyses:
        cat = a.root_cause.category
        file = a.error.file or a.context.file or ""
        if cat == "UNDECLARED_RESOURCE" and A.JUNK_RE.search(file):
            cat = "PACKAGE_HYGIENE"                     # never declare a temp / OS file - drop it
        level, prio, fn, label, manual = CATALOG.get(cat, CATALOG["UNCLASSIFIED"])
        detail = ""
        if cat == "DUPLICATE_ID":
            m = re.search(r'"([^"]+)"', a.error.message or "")
            detail = m.group(1) if m else ""
        if cat in ("MIMETYPE_STRUCTURE", "PACKAGE_HYGIENE"):
            key = cat                                   # one package-level root cause
        elif cat == "DUPLICATE_ID":
            key = f"{cat}:{file}:{detail}"
        else:
            key = f"{cat}:{file}"
        g = groups.get(key)
        if g is None:
            action = a.repair_plan.proposed_action or a.repair_plan.summary
            if cat == "PACKAGE_HYGIENE":
                action = "Temporary / OS / backup file - it is not packaged (never added to the manifest)."
            g = groups[key] = PlanGroup(key=key, category=cat, level=level, priority=prio, strategy=fn,
                                        strategy_label=label, explanation=a.root_cause.explanation,
                                        action=action, manual_action=manual)
        g.errors.append(a)
        if file and file not in g.files:
            g.files.append(file)
        g.codes.append(a.error.code)
        if a.error.message not in g.messages:
            g.messages.append(a.error.message)
    # cascades: broken links INTO a document that does not parse
    malformed = {}
    for g in groups.values():
        if g.category in PARSE_CATEGORIES:
            for f in g.files:
                malformed[f] = g.key
    if malformed:
        for g in list(groups.values()):
            if g.category != "BROKEN_FRAGMENT":
                continue                                # a missing FILE is never a parse cascade
            hits = set()
            for a in g.errors:
                for target in _hrefs_on_line(snapshot.epub_path, a.error.file, a.error.line):
                    for mf, mkey in malformed.items():
                        if target == mf or target.endswith("/" + mf) or mf.endswith("/" + target):
                            hits.add(mkey)
            if hits:
                g.cascade_of = sorted(hits)[0]
                g.explanation = ("Symptom of a root cause: the target document is not well-formed, so "
                                 "its ids cannot be read. It is re-checked after that document is repaired.")
    return sorted(groups.values(), key=lambda g: (g.priority, g.level, g.key))


def plan_text(groups) -> str:
    lines = ["REPAIR PLAN", "=" * 60]
    for n, g in enumerate(groups, 1):
        lines.append(f"{n}. {g.category.replace('_', ' ').title()}  [{', '.join(sorted(set(g.codes)))}]  "
                     f"x{g.n}")
        lines.append(f"   Files: {', '.join(g.files) or '-'}")
        if g.cascade_of:
            lines.append(f"   ROOT CAUSE: {g.cascade_of} (this group is a symptom)")
        lines.append(f"   Action: {g.action or g.manual_action}")
        lines.append(f"   Level: {g.level} - {LEVEL_NAMES[g.level]}"
                     + ("  (automatic)" if g.automatic else "  (not automatic)"))
    return "\n".join(lines)
