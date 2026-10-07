"""Tag suggestions (core/tag_suggest.py): learned styles, content rules,
"apply to similar" and "Retag From Your Corrections".

A synthetic 3-page book: bold 15pt headings, 10pt body, "Figure N." captions.
Run:  python -m pytest tests/test_tag_suggest.py -q
"""
import os
import sys

import fitz
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import profile_manager  # noqa: E402
from core.pdf_loader import PDFDocument  # noqa: E402
from core.zone_manager import ZoneManager  # noqa: E402
from core.tag_suggest import TagSuggester  # noqa: E402

BODY = "The heart pumps blood through the body and its work has been studied for a long time by many people."


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("ts") / "book.pdf")
    doc = fitz.open()
    for n in range(1, 4):
        page = doc.new_page(width=480, height=600)
        page.insert_text((40, 70), f"Section heading number {n}", fontsize=15, fontname="hebo")
        page.insert_text((40, 110), BODY[:70], fontsize=10, fontname="helv")
        page.insert_text((40, 124), BODY[70:], fontsize=10, fontname="helv")
        page.insert_text((40, 170), f"Figure {n}. A drawing of the heart.", fontsize=9, fontname="heit")
    doc.save(path)
    doc.close()
    return path


def _zones(pdf, zm, auto):
    """One zone per text line; auto=True marks them as Auto Zone output."""
    out = {}
    for pno in range(1, pdf.page_count + 1):
        page = pdf.get_page(pno)
        lines = sorted((l["bbox"] for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])),
                       key=lambda r: r[1])
        kinds = ["heading", "body", "body", "caption"]
        for kind, bb in zip(kinds, lines):
            attrs = {"auto_engine": True} if auto else {}
            z = zm.add_zone(pno, "p", [bb[0] - 1, bb[1] - 1, bb[2] + 1, bb[3] + 1], attributes=attrs,
                            auto_parent=False)
            out.setdefault(pno, {}).setdefault(kind, []).append(z)
    return out


@pytest.fixture
def setup(book):
    pdf = PDFDocument(book)
    zm = ZoneManager(pdf)
    buttons = profile_manager.tag_buttons_of(profile_manager.reload_profile("BITS"))
    zones = _zones(pdf, zm, auto=True)
    return pdf, zm, buttons, zones


def _tag(zm, buttons, zone, label):
    lab, tag, attrs = next(b for b in buttons if b[0] == label)
    zm.set_tag(zone.zone_id, tag, attributes=dict(attrs, tag_label=lab, manual_override=True))


def test_caption_suggested_from_its_label(setup):
    pdf, zm, buttons, zones = setup
    sg = TagSuggester(pdf, zm, buttons).suggest(zones[2]["caption"][0], k=3)
    assert sg and sg[0].tag == "caption"
    assert any("figure label" in r for r in sg[0].reasons)


def test_learned_style_wins_after_one_correction(setup):
    pdf, zm, buttons, zones = setup
    _tag(zm, buttons, zones[1]["heading"][0], "Heading 2 (section)")
    s = TagSuggester(pdf, zm, buttons)
    top = s.suggest(zones[3]["heading"][0], k=3)[0]
    assert top.label == "Heading 2 (section)"
    assert any("same style" in r for r in top.reasons)


def test_similar_zones_and_learned_retags(setup):
    pdf, zm, buttons, zones = setup
    _tag(zm, buttons, zones[1]["heading"][0], "Heading 2 (section)")
    s = TagSuggester(pdf, zm, buttons)
    similar = {z.zone_id for z in s.similar_zones(zones[1]["heading"][0])}
    assert similar == {zones[2]["heading"][0].zone_id, zones[3]["heading"][0].zone_id}
    retags = s.learned_retags()
    assert {z.zone_id for z, _ in retags} == similar
    assert all(sg.label == "Heading 2 (section)" for _, sg in retags)


def test_conflicting_corrections_do_not_retag(setup):
    pdf, zm, buttons, zones = setup
    _tag(zm, buttons, zones[1]["heading"][0], "Heading 2 (section)")
    _tag(zm, buttons, zones[2]["heading"][0], "Heading 3 (section)")
    assert TagSuggester(pdf, zm, buttons).learned_retags() == []
