"""Auto Zone: references / endnotes sections and cross-page continuations.

tests/fixtures/notes_book.py builds the book; every expectation comes from
its spec. Run:  python -m pytest tests/test_auto_zone_sections.py -q
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fixtures import notes_book  # noqa: E402
from core import profile_manager, text_extractor  # noqa: E402
from core.pdf_loader import PDFDocument  # noqa: E402
from core.zone_manager import ZoneManager  # noqa: E402
from auto_zoning import continuity, section_context  # noqa: E402
from auto_zoning.smart_auto_zone import SmartAutoZoner  # noqa: E402


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("nb") / "notes_book.pdf")
    notes_book.build(path)
    return path


@pytest.fixture
def zoned(book):
    text_extractor.set_decoration_detection_enabled(True)
    pdf = PDFDocument(book)
    zm = ZoneManager(pdf)
    az = SmartAutoZoner(pdf, zm, profile_manager.get_profile("BITS"), {})
    results = {p: az.auto_zone_page(p) for p in range(1, 6)}
    yield pdf, zm, az, results
    text_extractor.set_decoration_detection_enabled(False)


def _content(zm, page):
    return [z for z in sorted(zm.zones_on_page(page), key=lambda z: (z.serial or 10 ** 9, z.created_order))
            if z.tag != "pagenumber"]


def _starting(zm, page, prefix):
    return next(z for z in zm.zones_on_page(page) if z.text.replace("<b>", "").startswith(prefix))


def _is_heading(zone, part_type):
    """A BITS section heading: <h1> opening a notes / bibliography part."""
    return zone.tag == "h1" and zone.attributes.get("part_type") == part_type


def test_section_map_finds_notes_and_references(book):
    smap = section_context.build_section_map(PDFDocument(book))
    kinds = {(h.page, h.text): h.kind for h in smap.headings if h.kind}
    assert kinds == {(3, "Notes"): "endnotes", (4, "References"): "references"}
    assert smap.region_at(3, 500) == "endnotes"
    assert smap.region_at(4, 50) == "endnotes"            # notes continue onto page 4
    assert smap.region_at(4, 300) == "references"
    assert smap.region_at(5, 300) is None                  # the next chapter heading ends the section
    assert smap.region_at(2, 300) is None


def test_paragraph_continuation_is_merged(zoned):
    _pdf, zm, _az, results = zoned
    first_b = _starting(zm, 1, "The matter that")
    cont = _starting(zm, 2, "and the theoretical")
    assert cont.attributes.get("merged_with_previous") is True
    assert cont.attributes.get("merge_target") == first_b.zone_id
    assert cont.attributes.get("auto_continuation", 0) >= 80
    d = next(d for d in results[2].continuations if d.zone_id == cont.zone_id)
    assert d.merge and "next starts lowercase" in d.reasons


def test_new_paragraph_after_page_break_is_not_merged(zoned):
    _pdf, zm, _az, results = zoned
    d_para = _starting(zm, 3, "The next page opens")
    assert not d_para.attributes.get("merged_with_previous")
    assert all(not d.merge for d in results[3].continuations if d.zone_id == d_para.zone_id)


def test_notes_section_tags(zoned):
    _pdf, zm, _az, _r = zoned
    assert _is_heading(_starting(zm, 3, "Notes"), "notes")
    for prefix in ("1. Brown", "2. The letters"):
        assert _starting(zm, 3, prefix).tag == "en"
    note3 = _starting(zm, 4, "3. See also")
    assert note3.tag == "en"                                  # split out of the block above it
    cont = _starting(zm, 4, "sense is set out")
    assert cont.tag == "en" and cont.attributes.get("merged_with_previous")
    assert cont.attributes.get("merge_target") == _starting(zm, 3, "2. The letters").zone_id


def test_references_section_tags(zoned):
    _pdf, zm, _az, _r = zoned
    assert _is_heading(_starting(zm, 4, "References"), "bibliography")
    refs = [z for z in zm.zones_on_page(4) if z.tag == "reference"]
    assert sorted(z.text.split(",")[0] for z in refs) == ["Brown", "Mackey", "Ward"]   # one zone per entry
    assert not any(z.attributes.get("merged_with_previous") for z in refs)


def test_section_ends_at_next_chapter(zoned):
    _pdf, zm, _az, _r = zoned
    tags = [z.tag for z in _content(zm, 5)]
    assert tags == ["h1", "p"]


def test_unmerged_continuation_is_not_linked_again(zoned, book):
    pdf, zm, az, _r = zoned
    cont = _starting(zm, 2, "and the theoretical")
    assert zm.unmerge(cont.zone_id)
    assert cont.attributes.get("continuation_rejected") is True
    az.link_continuations(2)
    assert not cont.attributes.get("merged_with_previous")


def test_score_pair_rules():
    class Z:
        def __init__(self, zid, tag, text, bbox=(54, 100, 378, 200), page=1):
            self.zone_id, self.tag, self.text, self.bbox, self.page = zid, tag, text, bbox, page
            self.attributes = {}
    # mid-sentence + lowercase -> merge
    assert continuity.score_pair(Z("a", "p", "the very"), Z("b", "p", "soul of art")).merge
    # complete sentence + capitalised -> no merge
    assert not continuity.score_pair(Z("a", "p", "It ended."), Z("b", "p", "Another one")).merge
    # broken word
    assert continuity.score_pair(Z("a", "p", "the lan-"), Z("b", "p", "guage of")).merge
    # new numbered note never merges
    assert not continuity.score_pair(Z("a", "en", "see the letter of"), Z("b", "en", "4. Ibid., p. 3")).merge
    # different column
    assert not continuity.score_pair(Z("a", "p", "the very"), Z("b", "p", "soul", bbox=(220, 0, 400, 50))).merge


# ------------------------------------------- book-end notes grouped by chapter
@pytest.fixture(scope="module")
def grouped_book(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("gb") / "grouped_notes.pdf")
    notes_book.build_grouped(path)
    return path


def test_book_end_notes_grouped_by_chapter(grouped_book):
    import re
    text_extractor.set_decoration_detection_enabled(True)
    try:
        pdf = PDFDocument(grouped_book)
        zm = ZoneManager(pdf)
        az = SmartAutoZoner(pdf, zm, profile_manager.get_profile("BITS"), {})
        for p in range(1, pdf.page_count + 1):
            az.auto_zone_page(p)
    finally:
        text_extractor.set_decoration_detection_enabled(False)
    stream = [z for p in range(1, pdf.page_count + 1) for z in _content(zm, p)]
    # in reading order: group heading, its notes (merged continuations folded in)
    groups, cur = [], None
    for z in stream:
        plain = re.sub(r"<[^>]+>", "", z.text).strip()
        if _is_heading(z, "notes"):
            cur = (plain, [])
            groups.append(cur)
        elif z.tag == "en" and not z.attributes.get("merged_with_previous"):
            cur[1].append(int(re.match(r"(\d+)\s", plain).group(1)))
        elif z.tag == "en":
            assert plain.startswith("1898 in a letter")      # year-led turn-over line: continuation, not a note
        else:
            raise AssertionError(f"unexpected zone {z.tag} {plain[:40]!r} on page {z.page}")
    expected = [("Notes", [])] + [(title, list(range(1, len(notes) + 1))) for title, notes in notes_book.GROUPS]
    assert groups == expected
    # the note broken by the page break is linked to its first part
    cont = next(z for z in stream if z.attributes.get("merged_with_previous"))
    target = zm.zones[cont.attributes["merge_target"]]
    assert re.sub(r"<[^>]+>", "", target.text).startswith("2 Freud")
    # running heads ("128  Notes to pages ...") and the blank-page notice are not zoned
    assert not any("Notes to pages" in z.text or "intentionally" in z.text for z in zm.zones.values())
