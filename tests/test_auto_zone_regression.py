"""Mandatory regression suite for the CUPEPUB Auto Zone / Auto Tag /
character-level formatting engine.

The synthetic book (tests/fixtures/regression_doc.py) is generated from a
spec; expected values come from that spec, never from the engine. Real
supplied PDFs can be added to tests/regression/pdfs/ with a sidecar
<name>.expected.json (see test_supplied_pdfs at the bottom) - the engine
must discover everything from the document itself.

Run:  python -m pytest tests -q
"""
import glob
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import regression_doc  # noqa: E402
from core.pdf_loader import PDFDocument  # noqa: E402
from core import text_extractor, profile_manager  # noqa: E402
from core.formatting_ranges import ranges_for_style, parse_ranges  # noqa: E402
from core.zone_manager import ZoneManager  # noqa: E402
from auto_zoning import layout_engine  # noqa: E402
from auto_zoning.smart_auto_zone import SmartAutoZoner, record_manual_retag  # noqa: E402


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("reg") / "regression_book.pdf")
    truth = regression_doc.build(path)
    return path, truth


@pytest.fixture
def cup_mode():
    text_extractor.set_decoration_detection_enabled(True)
    yield
    text_extractor.set_decoration_detection_enabled(False)


@pytest.fixture(scope="module")
def cup_profile():
    return profile_manager.get_profile("CUPEPUB")


def _line_bbox(pdf, page_no, text):
    for li in layout_engine.extract_page_lines(pdf.get_page(page_no), with_decorations=False):
        if li.text == text:
            return li.bbox
    raise AssertionError(f"line not found: {text!r}")


# ------------------------------------------------------------ formatting
def test_underline_ranges_are_character_exact(book, cup_mode):
    """full word, partial word, single character, several separate ranges,
    phrase through spaces, interruption at a space, punctuation, accents,
    combined bold + underline (annotation)."""
    path, truth = book
    pdf = PDFDocument(path)
    for line_text, expected in truth["underline_lines"].items():
        page_no = 1 if line_text in regression_doc.VERSE else 2
        bbox = _line_bbox(pdf, page_no, line_text)
        tagged = text_extractor.extract_formatted_text(pdf.get_page(page_no), (bbox[0] - 1, bbox[1] - 1,
                                                                                 bbox[2] + 1, bbox[3] + 1))
        plain = parse_ranges(tagged)[0]
        assert plain == line_text, (plain, line_text)
        got = ranges_for_style(tagged, text_extractor._decoration_tag("underline"))
        assert got == [tuple(r) for r in expected], (line_text, tagged, got, expected)


def test_single_character_and_partial_word_stay_exact(book, cup_mode):
    path, _ = book
    pdf = PDFDocument(path)
    line0, line1 = regression_doc.VERSE[0], regression_doc.VERSE[1]
    t0 = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, line0))
    t1 = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, line1))
    u = text_extractor._decoration_tag("underline")
    assert f"thin<{u}>k</{u}>" in t0            # one letter inside "think"
    assert f"<{u}>wóods</{u}>" in t0             # whole accented word
    assert f"<{u}>hou</{u}>se" in t1             # partial word stays partial
    assert f"villag<{u}>e,</{u}>" in t1          # punctuation included, rest of word not


def test_separate_ranges_never_merge_across_undecorated_space(book, cup_mode):
    path, _ = book
    pdf = PDFDocument(path)
    line = regression_doc.VERSE[3]
    tagged = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, line))
    u = text_extractor._decoration_tag("underline")
    assert tagged.startswith(f"<{u}>To</{u}> <{u}>watch</{u}>"), tagged
    phrase = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, regression_doc.VERSE[2]))
    assert f"<{u}>will not see</{u}>" in phrase, phrase


def test_strike_through_detected(book, cup_mode):
    path, truth = book
    pdf = PDFDocument(path)
    line = regression_doc.VERSE[2]
    tagged = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, line))
    got = ranges_for_style(tagged, text_extractor._decoration_tag("strike"))
    assert got == [tuple(r) for r in truth["strike_lines"][line]], tagged


def test_combined_formatting_kept(book, cup_mode):
    path, truth = book
    pdf = PDFDocument(path)
    line = [k for k in truth["underline_lines"] if k not in regression_doc.VERSE][0]
    tagged = text_extractor.extract_formatted_text(pdf.get_page(2), _line_bbox(pdf, 2, line))
    plain, ranges, _ = parse_ranges(tagged)
    bold = [(r["start"], r["end"]) for r in ranges if r["style"] == "b"]
    start = line.index("underlined")
    assert any(s <= start and e >= start + len("underlined") for s, e in bold), tagged
    assert "<i>" not in tagged.split("phrase")[0], "bold face must not be reported as italic"
    italic = [(r["start"], r["end"]) for r in ranges if r["style"] == "i"]
    assert italic == [(line.index("this"), line.index("this") + 4)], tagged


def test_other_profiles_unchanged(book):
    """XML / EPUB profiles: decoration detection is off - no new markup."""
    path, _ = book
    pdf = PDFDocument(path)
    text_extractor.set_decoration_detection_enabled(False)
    line = regression_doc.VERSE[0]
    tagged = text_extractor.extract_formatted_text(pdf.get_page(1), _line_bbox(pdf, 1, line))
    assert tagged == line


# -------------------------------------------------------------- auto zone
_ROLE_FAMILY = {
    "page_number": ("page_number",), "label": ("label",), "heading": ("heading_",),
    "paragraph": ("paragraph",), "verse_line": ("verse_line",), "blockquote": ("blockquote",),
    "list_item": ("list_",), "footnote": ("footnote",), "figure": ("figure",), "caption": ("caption",),
}


def _run_auto_zone(path, profile, pages=(1, 2)):
    pdf = PDFDocument(path)
    zm = ZoneManager(pdf)
    az = SmartAutoZoner(pdf, zm, profile, {})
    results = {p: az.auto_zone_page(p) for p in pages}
    return pdf, zm, az, results


def test_auto_zone_structure_and_reading_order(book, cup_profile, cup_mode):
    path, truth = book
    _pdf, zm, _az, results = _run_auto_zone(path, cup_profile)
    for page, expected in truth["pages"].items():
        assert results[page].error is None
        zones = sorted(zm.zones_on_page(page), key=lambda z: z.serial)
        roles = [z.attributes.get("auto_role") for z in zones]
        assert len(roles) == len(expected), (page, roles, expected)
        for z, (exp_role, exp_text) in zip(zones, expected):
            assert z.attributes["auto_role"].startswith(_ROLE_FAMILY[exp_role]), (page, z.attributes["auto_role"],
                                                                                   exp_role, z.text)
            assert parse_ranges(z.text)[0].startswith(exp_text), (z.text, exp_text)
            # every tag applied exists in the project's own tag set
            assert z.tag in {b["tag"] for b in cup_profile["tag_buttons"]}
        assert [z.serial for z in zones] == list(range(1, len(zones) + 1))


def test_verse_lines_and_paragraph_boundaries(book, cup_profile, cup_mode):
    path, _ = book
    _pdf, zm, _az, _ = _run_auto_zone(path, cup_profile, pages=(1,))
    zones = sorted(zm.zones_on_page(1), key=lambda z: z.serial)
    verse = [parse_ranges(z.text)[0] for z in zones if z.attributes["auto_role"] == "verse_line"]
    assert verse == regression_doc.VERSE
    paras = [parse_ranges(z.text)[0] for z in zones if z.attributes["auto_role"].startswith("paragraph")]
    assert len(paras) == 2
    assert paras[0].startswith("The traveller") and paras[1].startswith("A second paragraph")


def test_formatting_survives_structural_tagging_through_generation(book, cup_profile, cup_mode, tmp_path):
    """Structural tag (verse line) + character formatting both reach the
    final XHTML: zone XML -> normalization -> Mapping.xml."""
    from core.epub_xml_generator import EpubXmlGenerator
    from core.mapping_engine import MappingEngine
    from core import tag_normalizer, xhtml_writer
    from lxml import etree
    path, _ = book
    pdf, zm, _az, _ = _run_auto_zone(path, cup_profile, pages=(1,))
    gen = EpubXmlGenerator(zm, pdf, str(tmp_path), "t", cup_profile, component_type="chapter")
    root = gen.generate()
    tag_normalizer.normalize_tree(root)
    u = text_extractor._decoration_tag("underline")
    verse_tag = next(z.tag for z in zm.zones.values() if z.attributes["auto_role"] == "verse_line")
    verse_els = list(root.iter(verse_tag))
    assert len(verse_els) == 4
    first = etree.tostring(verse_els[0], encoding="unicode")
    assert f"<{u}>wóods</{u}>" in first and f"<{u}>k</{u}>" in first, first
    final = MappingEngine(cup_profile["mapping_xml_path"]).load().apply(root)
    xhtml_writer.rename_internal_wrapper_tags(final)
    html = xhtml_writer.build_xhtml_document(final, title="t")
    out = etree.tostring(html, encoding="unicode")
    assert "<u>wóods</u>" in out and "<u>k</u>" in out and "<s>stopping</s>" in out
    assert "<u>To</u> <u>watch</u>" in out


def test_manual_override_and_lock_survive_reanalysis(book, cup_profile, cup_mode):
    path, _ = book
    pdf, zm, az, _ = _run_auto_zone(path, cup_profile, pages=(1,))
    zones = sorted(zm.zones_on_page(1), key=lambda z: z.serial)
    locked, edited = zones[5], zones[3]
    zm.set_locked(locked.zone_id, True)
    settings = {}
    zm.on_manual_retag = lambda z, t, a: record_manual_retag(settings, z, t, a)
    btn = next(b for b in cup_profile["tag_buttons"] if b["label"] == "Para_NoIndent")
    zm.set_tag(edited.zone_id, btn["tag"], dict(btn["attrs"]))
    assert edited.manual_override and edited.attributes["cup_name"] == "Para_NoIndent"
    assert settings["auto_tag_learning"]["paragraph"]["Para_NoIndent"] == 1
    res = az.auto_zone_page(1, reanalyse=True)
    assert res.error is None
    assert zm.zones[locked.zone_id].locked
    assert zm.zones[edited.zone_id].attributes["cup_name"] == "Para_NoIndent"
    # no duplicate zone was created on top of the protected ones
    for z in zm.zones_on_page(1):
        if z.zone_id in (locked.zone_id, edited.zone_id):
            continue
        assert not (abs(z.bbox[1] - locked.bbox[1]) < 1 and abs(z.bbox[0] - locked.bbox[0]) < 1)
    # Auto Tag never retags protected zones
    az.auto_tag_page(1)
    assert zm.zones[edited.zone_id].attributes["cup_name"] == "Para_NoIndent"
    assert not zm.apply_auto_tag(locked.zone_id, "p", {}, force=True)


def test_auto_zone_is_idempotent_and_cached(book, cup_profile, cup_mode, tmp_path):
    path, _ = book
    pdf = PDFDocument(path)
    zm = ZoneManager(pdf)
    az = SmartAutoZoner(pdf, zm, cup_profile, {}, cache_dir=str(tmp_path))
    first = az.auto_zone_page(1)
    again = az.auto_zone_page(1)
    assert first.created and not again.created
    files = os.listdir(os.path.join(str(tmp_path), az.fingerprint))
    assert any(f.startswith("p00001_") for f in files) and "context.json" in files
    # a fresh orchestrator re-uses the disk cache (no re-analysis needed)
    az2 = SmartAutoZoner(pdf, ZoneManager(pdf), cup_profile, {}, cache_dir=str(tmp_path))
    layout, roles = az2.analyse(1)
    assert len(layout.blocks) == len(az.analyse(1)[0].blocks)


def test_document_generator_supports_resume_and_cancel(book, cup_profile, cup_mode):
    import threading
    from auto_zoning.smart_auto_zone import DocumentRunState
    path, _ = book
    pdf = PDFDocument(path)
    az = SmartAutoZoner(pdf, ZoneManager(pdf), cup_profile, {})
    state = DocumentRunState(completed=[1])
    pages = [p for p, *_ in az.compute_document([1, 2], state=state)]
    assert pages == [2]
    cancel = threading.Event()
    cancel.set()
    assert list(az.compute_document([1, 2], cancel_event=cancel)) == []


def test_validation_report_clean(book, cup_profile, cup_mode):
    from core import auto_validation
    path, _ = book
    pdf, zm, az, _ = _run_auto_zone(path, cup_profile)
    rep = auto_validation.run(zm, pdf, cup_profile, az.knowledge)
    assert rep.ok, rep.to_text()
    assert rep.count(stage="reading_order") == 0
    assert rep.count(stage="formatting") == 0


# -------------------------------------------------------- knowledge model
def test_roles_resolve_from_project_configuration(cup_profile):
    from core.tag_knowledge import TagKnowledgeModel
    km = TagKnowledgeModel.build(cup_profile, {})
    labels = {b["label"] for b in cup_profile["tag_buttons"]}
    for role in ("paragraph", "verse_line", "footnote", "page_number", "caption", "heading_1", "list_numbered"):
        best = km.best_for_role(role)
        assert best is not None and best.option.label in labels, role
    # a concept the project has no tag for is never applied
    assert km.best_for_role("running_header") is None
    # families come from Mapping.xml
    assert any(len(f.members) >= 3 for f in km.mapping.families)


def test_dtd_model_constraints():
    from core.tag_knowledge import DTDModel
    from lxml import etree
    dtd = DTDModel.from_string("""
        <!ELEMENT doc (title, (para | poem)*)>
        <!ELEMENT title (#PCDATA)>
        <!ELEMENT para (#PCDATA | b | u)*>
        <!ELEMENT poem (line+)>
        <!ELEMENT line (#PCDATA | u)*>
        <!ELEMENT b (#PCDATA)> <!ELEMENT u (#PCDATA)>
        <!ATTLIST poem kind (verse | song) #REQUIRED>""")
    assert dtd.allows_child("doc", "para") and not dtd.allows_child("doc", "line")
    assert dtd.allows_sequence("doc", None, "title") and not dtd.allows_sequence("doc", None, "para")
    assert dtd.allows_sequence("doc", "title", "poem") and dtd.allows_sequence("doc", "para", "para")
    assert [a.name for a in dtd.required_attributes("poem")] == ["kind"]
    assert dtd.attribute_values("poem", "kind") == ["verse", "song"]
    assert dtd.validate(etree.fromstring("<doc><title>t</title><poem kind='verse'><line>a</line></poem></doc>")) == []
    assert dtd.validate(etree.fromstring("<doc><poem><line>a</line></poem></doc>"))


def test_dtd_constraint_changes_tag_decision(cup_profile, tmp_path):
    """A DTD that forbids the best tag in context makes Auto Tag fall back to
    the next valid candidate and flag the decision for review."""
    from core.tag_knowledge import TagKnowledgeModel, DTDModel
    from auto_zoning.auto_tag_engine import decide_page
    from auto_zoning.layout_engine import LayoutBlock, PageLayout
    from auto_zoning.semantic_classifier import RoleCandidate
    km = TagKnowledgeModel.build(cup_profile, {})
    poem_tag = km.best_for_role("verse_line").option.tag
    para_tag = km.best_for_role("paragraph").option.tag
    km.dtds = [DTDModel.from_string(f"<!ELEMENT component ({para_tag})*><!ELEMENT {para_tag} (#PCDATA)>"
                                    f"<!ELEMENT {poem_tag} (#PCDATA)>")]
    km._role_cache.clear()
    b = LayoutBlock(kind="verse_line", bbox=(0, 0, 10, 10))
    layout = PageLayout(page=1, width=100, height=100, blocks=[b])
    roles = {id(b): [RoleCandidate("verse_line", 0.9, []), RoleCandidate("paragraph", 0.4, [])]}
    dec = decide_page(layout, roles, km)[0]
    assert dec.tag == para_tag
    assert dec.needs_review


def test_reference_corpus_statistics(cup_profile, tmp_path):
    from core.tag_knowledge import TagKnowledgeModel
    for i in range(3):
        (tmp_path / f"ref{i}.xhtml").write_text(
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p class='poemline'>One <u>line</u></p><p class='poemline'>Two</p><p>Prose follows.</p>"
            "</body></html>", encoding="utf-8")
    km = TagKnowledgeModel.build(cup_profile, {"auto_tag_reference_xml_dirs": [str(tmp_path)]})
    assert len(km.corpus.files) == 3
    poem = km.best_for_role("verse_line").option.tag
    assert km.corpus.text_stats[poem].count == 6
    assert km.corpus.succession_probability(poem, poem) > 0.4
    assert km.corpus.stats_for(poem)["decorated_ratio"] > 0


# ------------------------------------------------------------ normalizer
def test_tag_normalization_preserves_text():
    from lxml import etree
    from core.tag_normalizer import normalize_tree
    root = etree.fromstring("<c><p>A <b>bold </b><underline><b>under</b></underline> <underline>x</underline> "
                            "<underline>y</underline><i></i><b><b>z</b></b><i>e</i><i>f</i></p></c>")
    before = "".join(root.itertext())
    normalize_tree(root)
    assert "".join(root.itertext()) == before
    out = etree.tostring(root, encoding="unicode")
    assert "<b>bold <underline>under</underline></b>" in out
    assert "<underline>x</underline> <underline>y</underline>" in out
    assert "<i></i>" not in out and "<b><b>" not in out and "<i>ef</i>" in out


# --------------------------------------------------------------- scanned
@pytest.mark.parametrize("font,size", [("helv", 12), ("hebo", 14), ("tiro", 14), ("tiit", 12), ("cour", 24)])
def test_raster_underline_on_scanned_line(font, size):
    import fitz
    import numpy as np
    from core.underline_detector import raster_line_decorations
    text = "Hello world - again yg"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontname=font, fontsize=size)
    raw = page.get_text("rawdict")
    chars = [c for b in raw["blocks"] for l in b["lines"] for s in l["spans"] for c in s["chars"]]
    base = 100 + size * 0.12
    for a, b in ((6, 8), (14, 14)):
        page.draw_line((chars[a]["bbox"][0], base), (chars[b]["bbox"][2], base), width=size * 0.06)
    clip = fitz.Rect(70, 100 - size * 1.2, 72 + size * 16, 100 + size * 0.5)
    pix = page.get_pixmap(dpi=300, clip=clip, colorspace=fitz.csGRAY)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
    _boxes, under, _strike, _segs = raster_line_decorations(gray < 140, text, 300 / 72, (clip.x0, clip.y0))
    got = [i for i, f in enumerate(under) if f]
    assert got == [6, 7, 8, 14], (font, size, got)


# --------------------------------------------------- supplied real PDFs
SUPPLIED = sorted(glob.glob(os.path.join(ROOT, "tests", "regression", "pdfs", "*.pdf")))


@pytest.mark.parametrize("pdf_path", SUPPLIED or [None])
def test_supplied_pdfs(pdf_path, cup_mode):
    """Drop a real PDF into tests/regression/pdfs/ with <name>.expected.json:
    {"underlines": [{"page": 1, "line": "<exact line text>", "ranges": [[start, end], ...]}],
     "strikes": [...same shape...]}
    The engine must find the line and its exact ranges by itself."""
    if pdf_path is None:
        pytest.skip("no supplied regression PDFs in tests/regression/pdfs/")
    expected_path = os.path.splitext(pdf_path)[0] + ".expected.json"
    if not os.path.isfile(expected_path):
        pytest.skip(f"no {os.path.basename(expected_path)}")
    with open(expected_path, encoding="utf-8") as f:
        expected = json.load(f)
    pdf = PDFDocument(pdf_path)
    for kind in ("underlines", "strikes"):
        style = text_extractor._decoration_tag("underline" if kind == "underlines" else "strike")
        for case in expected.get(kind, []):
            lines = layout_engine.extract_page_lines(pdf.get_page(case["page"]), with_decorations=False)
            match = [li for li in lines if li.text.strip() == case["line"].strip()]
            assert match, f"line not found on page {case['page']}: {case['line']!r}"
            bbox = match[0].bbox
            tagged = text_extractor.extract_formatted_text(pdf.get_page(case["page"]), bbox)
            assert ranges_for_style(tagged, style) == [tuple(r) for r in case["ranges"]], tagged


def test_engine_bookkeeping_never_reaches_output(book, cup_profile, cup_mode, tmp_path):
    from core.epub_xml_generator import EpubXmlGenerator
    from lxml import etree
    path, _ = book
    pdf, zm, _az, _ = _run_auto_zone(path, cup_profile, pages=(1,))
    root = EpubXmlGenerator(zm, pdf, str(tmp_path), "t", cup_profile, component_type="chapter").generate()
    xml = etree.tostring(root, encoding="unicode")
    for key in ("auto_engine", "auto_role", "confidence", "needs_review", "locked", "manual_override"):
        assert f'{key}="' not in xml, key
