"""Acceptance suite for the PDF <-> XHTML QC engine (core.qc).

The synthetic book (tests/fixtures/qc_book.py) generates the PDF and a
faithful EPUB from one content spec; every defect is seeded into the EPUB
on purpose, so the expected difference type and colour state come from the
spec, never from the engine.

Run:  python -m pytest tests/test_qc_engine.py -q
"""
import json
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fixtures import qc_book  # noqa: E402
from core.qc.engine import QCSession  # noqa: E402
from core.qc.mapping import MATCH, MISSING, EXTRA, MODIFIED, MOVED, DUPLICATE  # noqa: E402
from core.qc.package_manager import PackageError  # noqa: E402


@pytest.fixture(scope="module")
def pdf_path(tmp_path_factory):
    d = tmp_path_factory.mktemp("qc_pdf")
    p = str(d / "book.pdf")
    qc_book.build_pdf(p)
    return p


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("qc_cache"))


def open_session(pdf_path, cache_dir, tmp_path, defects=()):
    pkg = str(tmp_path / "pkg")
    qc_book.build_epub(pkg, defects=defects)
    s = QCSession.open(pdf_path, pkg, cache_dir=cache_dir)
    s.analyse()
    return s


def kinds(session, status="open"):
    return {d.kind for d in session.differences if d.status == status}


# ------------------------------------------------------------ clean book
def test_clean_book_matches_completely(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path)
    assert s.differences == []
    assert s.mapping.scores["Overall"] == 100.0
    assert all(st == MATCH for st in s.mapping.p_state)
    assert all(st == MATCH for st in s.mapping.x_state)
    # page numbers: unnumbered title page, roman front matter, arabic body
    printed = [pm.printed for pm in s.mapping.pages]
    assert printed[0] is None
    assert printed[1:] == ["ii", "1", "2", "3", "4"]
    assert all(pm.marker_state == MATCH for pm in s.mapping.pages if pm.printed)
    # both images matched in place
    assert [p["state"] for p in s.mapping.image_pairs] == [MATCH, MATCH]


# ------------------------------------------------------- seeded defects
@pytest.mark.parametrize("defect, kind, state", [
    ("missing_word", "MISSING_TEXT", MISSING),
    ("extra_word", "EXTRA_TEXT", EXTRA),
    ("modified", "MODIFIED_TEXT", MODIFIED),
    ("reorder", "REORDERED_TEXT", MOVED),
    ("duplicate", "DUPLICATE_TEXT", DUPLICATE),
    ("merged", "MERGED_PARAGRAPHS", MODIFIED),
    ("split_paragraph", "UNEXPECTED_PARAGRAPH_SPLIT", MODIFIED),
    ("missing_marker", "MISSING_PAGE_MARKER", MISSING),
    ("wrong_marker", "WRONG_PAGE_MARKER", MODIFIED),
    ("image_swap", "WRONG_IMAGE", MISSING),
    ("image_moved", "MOVED_IMAGE", MOVED),
    ("missing_caption", "MISSING_CAPTION", MISSING),
    ("broken_link", "BROKEN_LINK", MISSING),
    ("manifest_dup", "PACKAGE_PROBLEM", MISSING),
])
def test_each_defect_is_detected_with_its_colour_state(pdf_path, cache_dir, tmp_path, defect, kind, state):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=(defect,))
    found = [d for d in s.differences if d.kind == kind]
    assert found, f"{defect}: expected {kind}, got {sorted(kinds(s))}"
    assert all(d.state == state for d in found)
    assert s.mapping.scores["Overall"] < 100.0


def test_modified_text_has_character_level_segments(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("modified",))
    d = next(d for d in s.differences if d.kind == "MODIFIED_TEXT")
    assert any(op != "equal" for op, _a, _b in d.segments)
    assert d.pdf_page == 3 and d.split.endswith("ch1.xhtml")


def test_missing_marker_located_on_its_pdf_page(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("missing_marker",))
    d = next(d for d in s.differences if d.kind == "MISSING_PAGE_MARKER")
    assert d.pdf_page == 4
    assert d.correction is not None and d.correction.auto_safe


# ------------------------------------------------------------ corrections
def test_auto_correct_fixes_structure_never_text(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path,
                     defects=("missing_marker", "wrong_marker", "image_swap", "manifest_dup", "missing_word"))
    n = s.auto_correct()
    assert n >= 4
    left = kinds(s)
    assert not left & {"MISSING_PAGE_MARKER", "WRONG_PAGE_MARKER", "WRONG_IMAGE", "PACKAGE_PROBLEM"}
    assert "MISSING_TEXT" in left                    # text is never changed automatically
    # one undo step restores every auto correction
    s.undo()
    assert {"MISSING_PAGE_MARKER", "WRONG_PAGE_MARKER", "WRONG_IMAGE"} <= kinds(s)
    s.redo()
    assert "WRONG_IMAGE" not in kinds(s)


def test_accept_reject_ignore_edit_decisions(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("missing_word", "extra_word", "modified"))
    miss = next(d for d in s.differences if d.kind == "MISSING_TEXT")
    s.edit(miss, miss.pdf_text)
    assert "MISSING_TEXT" not in kinds(s)
    extra = next(d for d in s.differences if d.kind == "EXTRA_TEXT")
    s.reject(extra)
    s.refresh()
    # a user decision survives re-analysis
    assert next(d for d in s.differences if d.kind == "EXTRA_TEXT").status == "rejected"
    for mod in [d for d in s.differences if d.kind == "MODIFIED_TEXT"]:
        s.ignore(mod)
    s.refresh()
    assert all(d.status == "ignored" for d in s.differences if d.kind == "MODIFIED_TEXT")
    s.reopen(next(d for d in s.differences if d.kind == "MODIFIED_TEXT"))
    assert "MODIFIED_TEXT" in kinds(s)


def test_accept_moved_image_restores_reading_order(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("image_moved",))
    d = next(d for d in s.differences if d.kind == "MOVED_IMAGE")
    s.accept(d)
    assert "MOVED_IMAGE" not in kinds(s)
    assert [p["state"] for p in s.mapping.image_pairs] == [MATCH, MATCH]


def test_remap_image_to_package_file(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("image_swap",))
    wrong = [d for d in s.differences if d.kind == "WRONG_IMAGE"]
    assert len(wrong) == 2                          # the two images are swapped
    for _ in wrong:
        d = next(d for d in s.differences if d.kind == "WRONG_IMAGE")
        s.remap_image(d, d.image["suggested_file"])
    assert "WRONG_IMAGE" not in kinds(s)
    assert [p["state"] for p in s.mapping.image_pairs] == [MATCH, MATCH]


# ------------------------------------------------------- split management
def test_split_at_pdf_page_then_merge_back(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path)
    before = s.mgr.split_paths()
    s.split_at_pdf_page(5)
    after = s.mgr.split_paths()
    assert len(after) == len(before) + 1
    new = [p for p in after if p not in before][0]
    assert new in s.mgr.manifest()
    assert s.differences == []                      # content and mapping unchanged
    s.merge_with_previous(new)
    assert s.mgr.split_paths() == before
    assert s.differences == []
    assert not s.mgr.broken_links()


def test_delete_exactly_one_split_with_impact_and_undo(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path)
    before = s.mgr.split_paths()
    rel = next(p for p in before if p.endswith("preface.xhtml"))
    imp = s.impact(rel)
    assert imp.words > 0 and "ii" in imp.pages
    s.delete_split(rel)
    assert s.mgr.split_paths() == [p for p in before if p != rel]
    assert rel not in s.mgr.manifest()
    assert not s.mgr.broken_links()
    assert "MISSING_TEXT" in kinds(s)
    s.undo()
    assert s.mgr.split_paths() == before
    assert s.differences == []


def test_reorder_detected_and_fixed(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path)
    order = s.mgr.split_paths()
    swapped = order[:]
    swapped[-1], swapped[-2] = swapped[-2], swapped[-1]
    s.reorder_splits(swapped)
    d = next(d for d in s.differences if d.kind == "SPLIT_ORDER")
    s.accept(d)
    assert s.mgr.split_paths() == order
    assert "SPLIT_ORDER" not in kinds(s)


def test_rename_add_lock(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path)
    ch2 = next(p for p in s.mgr.split_paths() if p.endswith("ch2.xhtml"))
    s.rename_split(ch2, "chapter-two.xhtml")
    assert any(p.endswith("chapter-two.xhtml") for p in s.mgr.split_paths())
    assert not s.mgr.broken_links()
    first = s.mgr.split_paths()[0]
    s.add_split(first, "Dedication")
    assert len(s.mgr.split_paths()) == 5
    s.lock(first)
    with pytest.raises(PackageError):
        s.delete_split(first)


# ---------------------------------------------------------------- output
def test_reports_and_final_validation_and_build(pdf_path, cache_dir, tmp_path):
    s = open_session(pdf_path, cache_dir, tmp_path, defects=("missing_word", "image_swap"))
    for ext in ("html", "json", "csv", "pdf"):
        out = str(tmp_path / f"report.{ext}")
        s.export_report(out)
        assert os.path.getsize(out) > 0
    data = json.load(open(str(tmp_path / "report.json"), encoding="utf-8"))
    assert {d["type"] for d in data["differences"]} >= {"MISSING_TEXT", "WRONG_IMAGE"}
    assert len(data["pages"]) == 6
    rep = s.final_validation()
    assert rep.sections["Text integrity"] == "FAIL"
    assert rep.sections["Images"] == "FAIL"
    assert rep.sections["EPUBCheck"] in ("PASS", "WARN", "FAIL", "SKIP")
    epub = str(tmp_path / "out.epub")
    path, _rep = s.build_epub(epub)
    with zipfile.ZipFile(path) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
