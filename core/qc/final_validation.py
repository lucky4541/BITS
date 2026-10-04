"""Pre-export FINAL VALIDATION of a QC session.

Checks: XHTML well-formedness, text integrity and PDF <-> XHTML mapping
(from the unified mapping), page numbers, images and captions, splits, IDs
(duplicates), internal links, navigation, OPF manifest / spine, CSS and
media references, package structure - then the project's existing
validators: core.epub.quick_validator on a temporary build and the real
EPUBCheck (core.epubcheck.runner) when Java + epubcheck.jar are available.
"""
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field

from lxml import etree


@dataclass
class FinalReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)       # section -> "PASS" | "WARN" | "FAIL"
    epubcheck: object = None
    epub_path: str = ""

    def add(self, section, level, msg):
        {"error": self.errors, "warning": self.warnings, "info": self.info}[level].append(f"[{section}] {msg}")
        cur = self.sections.get(section, "PASS")
        new = {"error": "FAIL", "warning": "WARN", "info": "PASS"}[level]
        order = {"PASS": 0, "WARN": 1, "FAIL": 2}
        self.sections[section] = new if order[new] > order[cur] else cur

    def section(self, name):
        self.sections.setdefault(name, "PASS")

    def to_text(self):
        lines = ["FINAL VALIDATION", "=" * 50]
        for name, status in self.sections.items():
            lines.append(f"{status:5s}  {name}")
        for title, items in (("ERRORS", self.errors), ("WARNINGS", self.warnings), ("INFO", self.info)):
            if items:
                lines += ["", title] + [f"  - {i}" for i in items]
        return "\n".join(lines)


SECTIONS = ["XHTML validity", "Text integrity", "PDF/XHTML mapping", "Page numbers", "Images", "Captions",
            "Splits", "IDs", "Internal links", "Navigation", "OPF / spine / manifest", "CSS and media",
            "Package structure", "Quick Validator", "EPUBCheck"]


def run(session, run_epubcheck=True) -> FinalReport:
    rep = FinalReport()
    for s in SECTIONS:
        rep.section(s)
    mgr = session.mgr
    # XHTML validity
    for rel in mgr.content_documents():
        try:
            etree.parse(mgr.abspath(rel))
        except (OSError, etree.XMLSyntaxError) as e:
            rep.add("XHTML validity", "error", f"{rel}: {e}")
    # mapping-derived checks
    m = session.mapping
    if m is not None:
        open_d = [d for d in m.differences if d.status == "open"]
        by = Counter(d.kind for d in open_d)
        for kind, section, level in (
                ("MISSING_TEXT", "Text integrity", "error"), ("EXTRA_TEXT", "Text integrity", "warning"),
                ("MODIFIED_TEXT", "Text integrity", "warning"), ("DUPLICATE_TEXT", "Text integrity", "warning"),
                ("REORDERED_TEXT", "PDF/XHTML mapping", "warning"),
                ("LOW_CONFIDENCE_MAPPING", "PDF/XHTML mapping", "warning"),
                ("MISSING_PAGE_MARKER", "Page numbers", "error"), ("WRONG_PAGE_MARKER", "Page numbers", "error"),
                ("MISPLACED_PAGE_MARKER", "Page numbers", "warning"),
                ("DUPLICATE_PAGE_MARKER", "Page numbers", "warning"),
                ("MISSING_IMAGE", "Images", "error"), ("WRONG_IMAGE", "Images", "error"),
                ("BROKEN_IMAGE_REFERENCE", "Images", "error"), ("MOVED_IMAGE", "Images", "warning"),
                ("MODIFIED_IMAGE", "Images", "warning"),
                ("MISSING_CAPTION", "Captions", "warning"), ("WRONG_CAPTION", "Captions", "warning"),
                ("CAPTION_ON_WRONG_IMAGE", "Captions", "error"),
                ("SPLIT_ORDER", "Splits", "error"), ("UNMAPPED_SPLIT", "Splits", "warning"),
                ("BROKEN_LINK", "Internal links", "error"), ("PACKAGE_PROBLEM", "OPF / spine / manifest", "error")):
            if by.get(kind):
                rep.add(section, level, f"{by[kind]} open {kind.replace('_', ' ').lower()} difference(s)")
        rep.add("PDF/XHTML mapping", "info", "scores: " + ", ".join(f"{k} {v}%" for k, v in m.scores.items()))
    else:
        rep.add("PDF/XHTML mapping", "warning", "comparison has not been run")
    # IDs
    for rel in mgr.content_documents():
        try:
            ids = [el.get("id") for el in mgr.doc(rel).getroot().iter() if isinstance(el.tag, str) and el.get("id")]
        except Exception:
            continue
        dups = [i for i, c in Counter(ids).items() if c > 1]
        if dups:
            rep.add("IDs", "error", f"{rel}: duplicate id(s) {dups[:5]}")
    # links / nav
    for frm, href, reason, _sugg in mgr.broken_links():
        section = "Navigation" if frm in (mgr.nav_path, mgr.ncx_path) else "Internal links"
        rep.add(section, "error", f"{frm}: {href} - {reason}")
    if not mgr.nav_path:
        rep.add("Navigation", "warning", "no navigation document found")
    # package
    for kind, msg, _fix in mgr.package_problems():
        section = "CSS and media" if kind in ("unlisted_resource", "missing_resource") else "OPF / spine / manifest"
        rep.add(section, "error" if kind in ("missing_file", "missing_resource", "bad_spine", "no_opf") else "warning",
                msg)
    if not os.path.isfile(os.path.join(mgr.root, "META-INF", "container.xml")):
        rep.add("Package structure", "error", "META-INF/container.xml is missing")
    # existing validators on a temporary build
    tmp = os.path.join(tempfile.mkdtemp(prefix="qc_final_"), "check.epub")
    try:
        mgr.export_epub(tmp)
        from core.epub import package_reader, quick_validator
        qv = quick_validator.validate(package_reader.read_package(tmp))
        for issue in qv.issues:
            rep.add("Quick Validator", "error" if issue.severity == "ERROR" else "warning",
                    f"{issue.code} {issue.message}")
        if run_epubcheck:
            from core.epubcheck import runner
            ok, why = runner.check_availability()
            if ok:
                res = runner.run_epubcheck(tmp)
                rep.epubcheck = res
                if res.ran:
                    for msg in res.messages[:200]:
                        lvl = "error" if getattr(msg, "severity", "").upper() in ("ERROR", "FATAL") else "warning"
                        rep.add("EPUBCheck", lvl, f"{getattr(msg, 'id', '')} {getattr(msg, 'message', '')}")
                else:
                    rep.add("EPUBCheck", "warning", res.error)
            else:
                rep.add("EPUBCheck", "info", f"EPUBCheck not run - {why}")
                rep.sections["EPUBCheck"] = "SKIP"
    except Exception as e:  # noqa: BLE001
        rep.add("Package structure", "error", f"temporary build failed: {e}")
    return rep
