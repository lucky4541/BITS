"""VALIDATE & AUTO-FIX EPUB - the closed-loop, data-safe repair engine.

tests/fixtures/broken_epub.py seeds every defect by name, so each expected
outcome comes from the fixture's own spec. Tests marked `epubcheck` use the
REAL EPUBCheck (Java + tools/epubcheck/epubcheck.jar) and are skipped when it
is not installed; everything else runs anywhere.

Run:  python -m pytest tests/test_epub_autofix.py -q
"""
import hashlib
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fixtures import broken_epub  # noqa: E402
from core.epub import (autofix_strategies as A, integrity_snapshot as I, package_builder, package_reader,  # noqa: E402
                       repair_engine, repair_planner)
from core.epub.repair_strategies import RepairActionResult, fix_mimetype, fix_undeclared_resources  # noqa: E402
from core.epub.xhtml_repair_strategy import fix_xhtml_document  # noqa: E402
from core.epubcheck import runner  # noqa: E402

HAVE_EPUBCHECK = runner.check_availability()[0]
epubcheck = pytest.mark.skipif(not HAVE_EPUBCHECK, reason="EPUBCheck (Java + tools/epubcheck/epubcheck.jar) "
                                                          "not installed")


def build(tmp_path, defects, name="book.epub"):
    return broken_epub.build(str(tmp_path / name), defects=defects)


def apply(path, fn, out):
    mp = package_builder.MutablePackage.load(path)
    res = fn(mp, package_reader.read_package(path))
    mp.write_epub(out)
    return res


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


# ===================================================== integrity guard
def test_integrity_identical_package_passes(tmp_path):
    p = build(tmp_path, broken_epub.ALL_DEFECTS)
    inv = I.build_inventory(p)
    assert I.diff(inv, inv).ok
    t = inv.totals()
    assert t["page_markers"] == 3 and t["image_files"] == 2 and t["headings"] == 5


def test_integrity_detects_every_kind_of_loss(tmp_path):
    p = build(tmp_path, ())
    out = str(tmp_path / "damaged.epub")
    with zipfile.ZipFile(p) as zi, zipfile.ZipFile(out, "w") as zo:
        for info in zi.infolist():
            b = zi.read(info.filename)
            if info.filename.endswith("ch1.xhtml"):
                b = (b.replace(b"The third section.", b"")                          # text
                     .replace(b'<h2 id="sec3">Section Three</h2>', b"")             # heading + id
                     .replace(b'<span epub:type="pagebreak" role="doc-pagebreak" id="page_2" aria-label="2"/>', b"")
                     .replace(b'<a href="#sec2">section two</a>', b"section two"))   # link
            if info.filename.endswith("fig2.png"):
                b = b + b"\0"                                                      # binary asset changed
            if info.filename.endswith("content.opf"):
                b = b.replace(b"<dc:creator>Test Author</dc:creator>", b"")         # metadata
            zo.writestr(info, b)
    res = I.diff(I.build_inventory(p), I.build_inventory(out))
    cats = {f.category for f in res.losses}
    assert not res.ok
    assert {"text", "heading", "id", "page marker", "link", "binary", "metadata"} <= cats


def test_integrity_allows_declared_renames_and_junk_removal(tmp_path):
    p = build(tmp_path, ("dup_id", "junk"))
    out = str(tmp_path / "fixed.epub")
    mp = package_builder.MutablePackage.load(p)
    pkg = package_reader.read_package(p)
    r1 = A.fix_package_hygiene(mp, pkg)
    r2 = A.fix_duplicate_ids_safely(mp, pkg)
    mp.write_epub(out)
    allowed = I.Allowed()
    allowed.merge(r1.allowed)
    allowed.merge(r2.allowed)
    res = I.diff(I.build_inventory(p), I.build_inventory(out), allowed)
    assert res.ok, res.summary()
    assert any(f.category == "id" and f.severity == "CHANGE" for f in res.findings)


# ============================================= strategies (no EPUBCheck)
CASES = [
    ("entity", A.fix_named_entities, "&#160;", "&nbsp;"),
    ("img_backslash", A.fix_resource_references, 'src="images/fig1.png"', "images\\fig1.png"),
    ("img_case", A.fix_resource_references, 'src="images/fig2.png"', "Images/FIG2.png"),
    ("stale_split", A.fix_resource_references, 'href="ch1.xhtml#sec3"', "ch1-split.xhtml"),
    ("frag_case", A.fix_broken_fragments, 'href="#sec2"', "#Sec2"),
    ("frag_moved", A.fix_broken_fragments, 'href="ch2.xhtml#n5"', "ch1.xhtml#n5"),
    ("frag_renamed", A.fix_broken_fragments, 'href="#fig1"', "#fig-1"),
    ("dup_id", A.fix_duplicate_ids_safely, 'id="sec1-2"', None),
    ("media_type", A.fix_media_types, 'href="images/fig2.png" media-type="image/png"', "image/jpeg"),
    ("css_brace", A.fix_css_syntax, "text-indent: 1em; }", None),
    ("unclosed", fix_xhtml_document, "unclosed italic.</i></p>", None),
    ("undeclared_css", fix_undeclared_resources, 'href="extra.css"', None),
]


@pytest.mark.parametrize("defect, fn, expect, gone", CASES)
def test_strategy_repairs_minimally_and_loses_nothing(tmp_path, defect, fn, expect, gone):
    p = build(tmp_path, (defect,))
    out = str(tmp_path / "out.epub")
    res = apply(p, fn, out)
    assert res.applied
    with zipfile.ZipFile(out) as z:
        blob = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                       if n.endswith((".xhtml", ".opf", ".css")))
    assert expect in blob
    if gone:
        assert gone not in blob
    integ = I.diff(I.build_inventory(p), I.build_inventory(out), getattr(res, "allowed", None))
    assert integ.ok, integ.summary()


def test_textual_edits_keep_the_rest_of_the_file_byte_identical(tmp_path):
    p = build(tmp_path, ("frag_case",))
    out = str(tmp_path / "out.epub")
    apply(p, A.fix_broken_fragments, out)
    with zipfile.ZipFile(p) as a, zipfile.ZipFile(out) as b:
        before = a.read("OEBPS/ch1.xhtml").decode()
        after = b.read("OEBPS/ch1.xhtml").decode()
    assert after == before.replace('href="#Sec2"', 'href="#sec2"')       # only that attribute changed
    assert after.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>')


def test_unresolvable_references_are_never_invented_or_removed(tmp_path):
    p = build(tmp_path, ("img_missing", "frag_missing"))
    out = str(tmp_path / "out.epub")
    r1 = apply(p, A.fix_resource_references, out)
    r2 = apply(p, A.fix_broken_fragments, out)
    assert not r1.applied and not r2.applied
    assert any("never invented" in x["reason"] for x in r1.review)
    assert any("never invented" in x["reason"] for x in r2.review)


def test_duplicate_id_keeps_the_referenced_occurrence(tmp_path):
    p = build(tmp_path, ("dup_id",))
    out = str(tmp_path / "out.epub")
    res = apply(p, A.fix_duplicate_ids_safely, out)
    with zipfile.ZipFile(out) as z:
        ch1 = z.read("OEBPS/ch1.xhtml").decode()
    assert '<h1 id="sec1">' in ch1                     # the nav / heading target keeps its id
    assert '<p id="sec1-2">' in ch1                    # the later duplicate is renamed, not removed
    assert res.allowed.renamed_ids[("OEBPS/ch1.xhtml", "sec1")] == "sec1-2"


def test_junk_is_dropped_only_when_unreferenced(tmp_path):
    p = build(tmp_path, ("junk",))
    out = str(tmp_path / "out.epub")
    res = apply(p, A.fix_package_hygiene, out)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert not {"OEBPS/.DS_Store", "Thumbs.db", "OEBPS/ch1.xhtml.bak"} & names
    assert "OEBPS/ch1.xhtml" in names and res.allowed.removed_files


def test_mimetype_and_spine(tmp_path):
    p = build(tmp_path, ("compressed_mimetype", "dup_spine"))
    out = str(tmp_path / "out.epub")
    mp = package_builder.MutablePackage.load(p)
    pkg = package_reader.read_package(p)
    fix_mimetype(mp, pkg)
    A.fix_duplicate_spine(mp, pkg)
    mp.write_epub(out)
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        opf = z.read("OEBPS/content.opf").decode()
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert opf.count('idref="ch2"') == 1


def test_plan_groups_root_causes_and_junk(tmp_path):
    class Snap:                                     # a minimal stand-in for a validation snapshot
        pass
    from core.epub.error_analyzer import AnalyzableError, ErrorContext, RepairPlan, Repairability, RootCause, \
        ErrorAnalysis
    s = Snap()
    s.epub_path = build(tmp_path, ("junk",))

    def ana(cat, file, code="QV-023"):
        return ErrorAnalysis(AnalyzableError("Quick", code, "WARNING", "m", file), ErrorContext(file=file),
                             RootCause(cat, "x"), RepairPlan(Repairability.SAFE_AUTO_FIX, "s", deterministic=True))
    s.all_analyses = [ana("UNDECLARED_RESOURCE", "OEBPS/.DS_Store"), ana("UNDECLARED_RESOURCE", "Thumbs.db"),
                      ana("UNDECLARED_RESOURCE", "OEBPS/extra.css"),
                      ana("MIMETYPE_STRUCTURE", "mimetype", "QV-002"), ana("MIMETYPE_STRUCTURE", "x.epub", "PKG-006")]
    groups = repair_planner.build_plan(s)
    cats = [g.category for g in groups]
    assert cats[0] == "PACKAGE_HYGIENE" and groups[0].n == 2      # junk is never declared - it is dropped
    assert cats.count("MIMETYPE_STRUCTURE") == 1                   # one root cause, two symptoms
    assert "UNDECLARED_RESOURCE" in cats


def test_without_epubcheck_the_result_is_never_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "check_availability", lambda: (False, "not installed (test)"))
    p = build(tmp_path, ())
    rep = repair_engine.run_full_auto_repair(p)
    assert rep.overall == "NEEDS REVIEW"
    assert "EPUBCheck is not available" in rep.final_status
    assert rep.dashboard["EPUBCheck"]["status"] == "REVIEW"


# ================================================ closed loop (EPUBCheck)
@epubcheck
@pytest.mark.parametrize("defect", [d for d in broken_epub.SAFE_DEFECTS if d != "junk"])
def test_each_safe_defect_is_cleared_by_epubcheck(tmp_path, defect):
    p = build(tmp_path, (defect,))
    assert not runner.run_epubcheck(p).is_valid
    rep = repair_engine.run_full_auto_repair(p, session_dir=str(tmp_path / "s"))
    assert rep.overall == "PASS", (rep.final_status, rep.remaining_issues)
    assert rep.content_integrity_verified


@epubcheck
def test_closed_loop_all_defects(tmp_path):
    p = build(tmp_path, broken_epub.ALL_DEFECTS)
    original = sha(p)
    rep = repair_engine.run_full_auto_repair(p)
    # original untouched; session folders; backup identical
    assert sha(p) == original and rep.original_unchanged
    for sub in ("repair_backup", "working", "repair_history"):
        assert os.path.isdir(os.path.join(rep.session_dir, sub))
    assert sha(os.path.join(rep.session_dir, "repair_backup", "original.epub")) == original
    # only the two genuinely unresolvable defects remain - nothing invented, nothing deleted
    assert rep.overall == "NEEDS REVIEW"
    remaining = {(r["code"], r["root_cause"]) for r in rep.remaining_issues}
    assert remaining == {("RSC-007", "MISSING_RESOURCE"), ("RSC-012", "BROKEN_FRAGMENT")}
    assert all(r["why_not_fixed"] and r["recommended"] for r in rep.remaining_issues)
    final = runner.run_epubcheck(rep.output_path)                 # the PACKAGED file itself
    assert final.n_fatal == 0 and {m.code for m in final.messages} == {"RSC-007", "RSC-012"}
    assert rep.content_integrity_verified and rep.dashboard["Data loss"]["detail"] == "NONE"
    assert rep.totals_after["page_markers"] == rep.totals_before["page_markers"]
    assert rep.totals_after["images"] == rep.totals_before["images"]
    assert rep.totals_after["words"] == rep.totals_before["words"]
    with zipfile.ZipFile(rep.output_path) as z:
        assert "OEBPS/.DS_Store" not in z.namelist()              # clean build
        assert '<img src="images/lost.png"' in z.read("OEBPS/ch2.xhtml").decode()   # never removed
    assert all(h["status"] == "COMMITTED" for h in rep.history)
    for name in ("Report (HTML)", "Repair history", "EPUBCheck - final package", "Data integrity"):
        assert os.path.isfile(rep.reports[name])


@epubcheck
def test_safe_defects_reach_a_validated_package(tmp_path):
    p = build(tmp_path, broken_epub.SAFE_DEFECTS)
    rep = repair_engine.run_full_auto_repair(p)
    assert rep.overall == "PASS"
    assert rep.final_status.startswith("VALIDATED")
    assert runner.run_epubcheck(rep.output_path).is_valid
    assert all(v["status"] in ("PASS", "SKIP") for v in rep.dashboard.values())


@epubcheck
def test_clean_book_is_not_changed(tmp_path):
    p = build(tmp_path, ())
    rep = repair_engine.run_full_auto_repair(p)
    assert rep.overall == "PASS" and rep.committed == 0
    assert not rep.files_modified and not rep.files_removed


@epubcheck
def test_a_repair_that_loses_content_is_rolled_back(tmp_path, monkeypatch):
    def destructive(mp, pkg):
        name = "OEBPS/ch1.xhtml"
        mp.set_bytes(name, mp.get_bytes(name).replace(b"The third section.", b""))
        return RepairActionResult(applied=True, description="destructive", files=[name])
    monkeypatch.setitem(repair_planner.CATALOG, "CSS_SYNTAX", (1, 11, destructive, "destructive", "x"))
    p = build(tmp_path, ("css_brace",))
    rep = repair_engine.run_full_auto_repair(p)
    rec = [h for h in rep.history if h["strategy"] == "destructive"]
    assert rec and all(h["status"] == "ROLLED BACK" for h in rec)
    assert "content integrity" in rec[0]["reason"]
    assert rep.content_integrity_verified
    with zipfile.ZipFile(rep.output_path) as z:
        assert b"The third section." in z.read("OEBPS/ch1.xhtml")


@epubcheck
def test_repair_that_makes_epubcheck_worse_is_rolled_back(tmp_path, monkeypatch):
    def harmful(mp, pkg):                          # keeps the text but breaks the markup
        name = "OEBPS/ch2.xhtml"
        mp.set_bytes(name, mp.get_bytes(name).replace(b'href="#fig1"', b'href="#does-not-exist"'))
        return RepairActionResult(applied=True, description="harmful", files=[name])
    monkeypatch.setitem(repair_planner.CATALOG, "MEDIA_TYPE_MISMATCH", (2, 2, harmful, "harmful", "x"))
    p = build(tmp_path, ("media_type",))
    rep = repair_engine.run_full_auto_repair(p)
    rec = [h for h in rep.history if h["strategy"] == "harmful"]
    assert rec and rec[0]["status"] == "ROLLED BACK"


@epubcheck
def test_generated_epub_structure_package_is_valid(tmp_path):
    from tests.test_page_gap_filler import build_project
    from core.epub_structure import orchestrator
    root = str(tmp_path / "book")
    build_project(root)
    res = orchestrator.run_full_analysis(root, run_epubcheck=False, normal_isbn="9780306406157",
                                         printed_isbn="9780306406157")
    r = runner.run_epubcheck(res.output_epub_path)
    assert r.is_valid, [(m.code, m.message) for m in r.messages]
