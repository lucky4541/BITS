"""BITS 2.2 book / JATS 1.4 article output: structure, DTD validation (JATS
DTD bundled; BITS once its DTD is installed), data-safe auto-fix, profiles."""
import os
import sys

import pytest
from lxml import etree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import bits_book  # noqa: E402
from core.bits import autofix, dtd, pipeline, structure, vocabulary  # noqa: E402
from core.pdf_loader import PDFDocument  # noqa: E402
from core.zone_manager import ZoneManager  # noqa: E402

BITS_DTD = pytest.mark.skipif(not dtd.available("BITS"), reason="BITS 2.2 DTD not installed (profiles/BITS/dtd)")


@pytest.fixture(scope="module")
def pdf(tmp_path_factory):
    return PDFDocument(bits_book.build(str(tmp_path_factory.mktemp("bits") / "book.pdf")))


def _generate(pdf, kind, tmp_path):
    zm = bits_book.zone(ZoneManager(pdf), pdf, kind)
    out = str(tmp_path / f"{kind}.xml")
    res = pipeline.generate(zm, pdf, kind, out, str(tmp_path / "images"), prefix="t")
    return res, etree.parse(out).getroot(), out


# ------------------------------------------------------------------ JATS
def test_jats_article_is_valid_and_complete(pdf, tmp_path):
    res, root, out = _generate(pdf, "JATS", tmp_path)
    assert res.text_preserved, res.summary()
    assert res.dtd_available and res.valid, res.errors_after
    assert root.tag == "article" and root.get("dtd-version") == "1.4"
    with open(out, encoding="utf-8") as f:
        assert "JATS (Z39.96) Journal Publishing DTD with MathML3 v1.4" in f.read(400)
    meta = root.find("front/article-meta")
    assert meta.findtext("title-group/article-title") == "Chaucer and the Poems of Ch"
    name = meta.find("contrib-group/contrib/name")
    assert name.findtext("surname") == "Wimsatt" and name.findtext("given-names") == "James I."
    assert meta.find("permissions/copyright-year").text == "2006"
    secs = root.findall("body/sec")
    assert [s.findtext("title") for s in secs] == ["Preface", "Chapter 1 Beginnings", "Chapter 2 Middles"]
    assert secs[1].find("sec").findtext("title") == "Early Witnesses"         # h2 nested in h1
    back = root.find("back")
    assert back.find("ack").findtext("title") == "Index"
    refs = back.findall("ref-list/ref")
    assert len(refs) == 2 and back.findtext("ref-list/title") == "References"
    fn = back.find("fn-group/fn")
    assert fn.findtext("label") == "1" and fn.findtext("p").startswith("See the catalogue")
    # page markers sit inside paragraphs, in reading order
    targets = root.findall(".//target")
    assert [t.get("id") for t in targets] == ["page1", "page2", "page3", "page4"]
    assert all(t.getparent().tag == "p" for t in targets)
    # the disp-quote zone became a disp-quote with a paragraph
    assert secs[2].find("disp-quote/p/italic").text.startswith("Ask not")


def test_report_written(pdf, tmp_path):
    res, _root, _out = _generate(pdf, "JATS", tmp_path)
    with open(res.report_path, encoding="utf-8") as f:
        assert "JATS XML: VALID" in f.read()


# ------------------------------------------------------------------ BITS
def test_bits_book_structure(pdf, tmp_path):
    res, root, _out = _generate(pdf, "BITS", tmp_path)
    assert res.text_preserved, res.summary()
    assert root.tag == "book" and root.get("dtd-version") == "2.2"
    bm = root.find("book-meta")
    assert bm.findtext("book-title-group/book-title") == "Chaucer and the Poems of Ch"
    assert bm.find("book-title-group/subtitle/italic").text == "A Study of Manuscripts"
    isbn = bm.find("isbn")
    assert isbn.text == "9780802091543" and isbn.get("publication-format") == "print"
    assert bm.findtext("publisher/publisher-name") == "University of Toronto Press"
    assert root.find("front-matter/preface/book-part-meta/title-group/title").text == "Preface"
    parts = root.findall("book-body/book-part")
    assert [p.get("book-part-type") for p in parts] == ["chapter", "chapter"]
    tg = parts[0].find("book-part-meta/title-group")
    assert tg.findtext("label") == "Chapter 1" and tg.findtext("title") == "Beginnings"
    assert parts[0].find("body/sec/title").text == "Early Witnesses"
    assert parts[0].find("back/fn-group/fn/label").text == "1"               # chapter notes in the chapter
    back = root.find("book-back")
    assert back.findtext("ref-list/title") == "References" and len(back.findall("ref-list/ref")) == 2
    terms = [t.text or "".join(t.itertext()) for t in back.findall("index/index-entry/term")]
    assert [x.strip() for x in terms] == ["Abbey, 2, 3", "Church, 1"]


@BITS_DTD
def test_bits_book_is_valid(pdf, tmp_path):
    res, _root, _out = _generate(pdf, "BITS", tmp_path)
    assert res.valid, res.errors_after


def test_bits_without_dtd_says_so(pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(dtd, "available", lambda kind, settings=None: False)
    res, _root, _out = _generate(pdf, "BITS", tmp_path)
    assert res.status == "NOT VALIDATED - DTD not installed" and res.text_preserved


# --------------------------------------------------------------- autofix
@pytest.fixture(scope="module")
def jats():
    return dtd.load("JATS")


def _article(body):
    return etree.fromstring(
        '<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="other" dtd-version="1.4"><front>'
        '<journal-meta><journal-id journal-id-type="publisher-id">j</journal-id><issn>0000-0000</issn>'
        '</journal-meta><article-meta><title-group><article-title>T</article-title></title-group>'
        f'</article-meta></front><body>{body}</body></article>')


def test_autofix_wraps_text_and_inline_in_paragraphs(jats):
    root = _article('<sec><title>S</title>loose text <italic>here</italic><list list-type="number">'
                    '<list-item><p>a</p></list-item></list></sec>')
    words = structure.content_signature(root)
    rep = autofix.fix(root, jats)
    assert rep.valid, rep.errors_after
    assert structure.content_signature(root) == words
    sec = root.find("body/sec")
    assert sec[1].tag == "p" and "loose text" in sec[1].text and sec[1].find("italic").text == "here"
    assert root.find(".//list").get("list-type") == "order"                     # value mapped


def test_autofix_splits_paragraph_around_block(jats):
    root = _article('<p>before <sec><title>Inner</title><p>x</p></sec> after</p>')
    rep = autofix.fix(root, jats)
    assert rep.valid, rep.errors_after
    texts = [t.strip() for t in root.find("body").itertext() if t.strip()]
    assert texts == ["before", "Inner", "x", "after"]                          # reading order kept
    assert root.find("body/p").text.strip() == "before"


def test_autofix_unknown_element_kept(jats):
    root = _article('<p>A <smallcaps>name</smallcaps> here</p><poem-line>verse</poem-line>')
    rep = autofix.fix(root, jats)
    assert rep.valid, rep.errors_after
    assert root.find(".//named-content[@content-type='smallcaps']").text == "name"
    assert root.find("body/p[@content-type='poem-line']").text == "verse"


def test_autofix_moves_page_marker_and_keeps_reading_order(jats):
    root = _article('<p>one</p><sec><title>S</title><p>two</p></sec><p>three</p>'
                    '<target target-type="pagenum" id="page9"/>')
    rep = autofix.fix(root, jats)
    assert rep.valid, rep.errors_after
    texts = [t for t in root.find("body").itertext() if t.strip()]
    assert texts == ["one", "S", "two", "three"]                              # order kept
    t = root.find(".//target")
    assert t.getparent().tag == "p" and t.getparent().text == "three"


def test_autofix_never_changes_text(jats, monkeypatch):
    def destructive(self, root):
        for p in root.iter("p"):
            p.text = "changed"
        return 1
    monkeypatch.setattr(autofix.Fixer, "run_pass", destructive)
    root = _article('<p>original <bold>x</bold></p><sec/>')
    rep = autofix.fix(root, jats)
    assert root.find("body/p").text == "original "
    assert rep.reverted


def test_fix_file_never_touches_input(tmp_path, jats):
    src = tmp_path / "in.xml"
    src.write_bytes(etree.tostring(_article("<sec><title>S</title>text</sec>")))
    before = src.read_bytes()
    out, rep = pipeline.fix_file(str(src))
    assert src.read_bytes() == before and out.endswith(".fixed.xml")
    assert pipeline.validate_file(out)[1] == [] and rep.valid


# -------------------------------------------------------------- profiles
@pytest.mark.parametrize("kind", ["BITS", "JATS"])
def test_profiles(kind):
    from core import profile_manager
    p = profile_manager.reload_profile(kind)
    labels = [b["label"] for b in p["tag_buttons"]]
    assert len(labels) == len(set(labels))
    assert p == vocabulary.profile(kind) or p["tag_buttons"] == vocabulary.profile(kind)["tag_buttons"]
    assert p["root_tag"] == ("book" if kind == "BITS" else "article")
    seen = [g["label"] for g in p["tag_groups"]]
    assert len(seen) == len(set(seen))


def test_profile_names():
    from core import profile_manager
    assert profile_manager.list_profile_names() == ["BITS", "JATS"]
    assert profile_manager.DEFAULT_PROFILE_NAME == "BITS"
