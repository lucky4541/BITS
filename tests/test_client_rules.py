"""CUPEPUB client rules (the client's SPiXVali validation, EPUB-001 ... 051):
native validation, the repairs (links or spans, index locators, junk,
OPF identifiers, guide / landmarks, DPI), the closed-loop integration and
the client-format OPF generated from the title / copyright pages."""
import io
import os
import shutil
import zipfile

import pytest

from core.epub import integrity_snapshot, package_builder, package_reader, repair_engine
from core.epub.client_rules import compare, cupepub, cupepub_fixes, front_matter, images, parse_log
from core.epubcheck import runner
from tests.fixtures import cup_book

epubcheck = pytest.mark.skipif(not runner.check_availability()[0], reason="EPUBCheck not installed")

LOG = """#Program Name           : SPiXVali Tool
#Client Name         \t: CUPEPUB
#Profile             \t: ED
#Total Error count:3
#Total Warning count:2
#Input XML File Path    \t: D:\\Epub_Conversion\\1543\\15431.epub

#List of errors:
#---------------

1. Error[EPUB-002]:1:1 The EPUB file name is incorrect, please check and update.
2. Error[EPUB-010]:12:12 Junk/control character present in the "05_91543_fm4.xhtml" file. Please check and update
3. Error[EPUB-014]:1:1 The print isbn property is missing in the OPF file.

#List of warnings:
#-----------------

1. Warning[EPUB-009]:18:518 Link missing in the xhtml file: 07_91543_fm6.xhtml, please verify and provide the link for "chapter 3".
2. Warning[EPUB-043]:22:79 Index page number is not cited. Please check 18_91543_bm3.xhtml
"""


@pytest.fixture()
def book(tmp_path):
    return cup_book.build(str(tmp_path / "15431.epub"))


def _apply(path, fn, out):
    mp = package_builder.MutablePackage.load(path)
    res = fn(mp, package_reader.read_package(path))
    mp.write_epub(out)
    return res


def _read(path, name):
    with zipfile.ZipFile(path) as z:
        return z.read("OEBPS/" + name).decode("utf-8")


# ------------------------------------------------------------------ log
def test_parse_tool_log():
    log = parse_log(LOG)
    assert log.client == "CUPEPUB" and log.totals == {"Error": 3, "Warning": 2}
    assert log.by_code() == {"EPUB-002": 1, "EPUB-009": 1, "EPUB-010": 1, "EPUB-014": 1, "EPUB-043": 1}
    assert log.findings[1].file == "05_91543_fm4.xhtml" and log.findings[1].line == 12
    assert log.findings[3].file == "07_91543_fm6.xhtml"
    rep = cupepub.ClientReport(findings=[cupepub.ClientFinding("EPUB-002", "Error", "", 1, 1, "x")])
    assert ("EPUB-002", 1, 1) in compare(log, rep)


# ------------------------------------------------------------ validation
def test_native_rules_report_what_the_client_tool_reports(book):
    rep = cupepub.validate_epub(book)
    codes = rep.by_code()
    for code in ("EPUB-002", "EPUB-009", "EPUB-010", "EPUB-011", "EPUB-014", "EPUB-019", "EPUB-021", "EPUB-026",
                 "EPUB-035", "EPUB-037", "EPUB-038", "EPUB-041", "EPUB-043", "EPUB-044", "EPUB-045", "EPUB-046",
                 "EPUB-047"):
        assert code in codes, code
    texts = [f.message for f in rep.findings if f.code == "EPUB-009"]
    # case-insensitive like the tool; chapter references only when that chapter exists
    assert any('"chapter 3"' in t for t in texts) and any('"[1863]"' in t for t in texts)
    assert any('"equation 3"' in t for t in texts)
    assert rep.counts()["Error"] > 0 and "Error[EPUB-002]:1:1" in rep.log_text()


def test_junk_rule_follows_the_tools_utf7_reading():
    assert cupepub.junk_offsets("café".encode()) == [3]              # é reads as "Ã©" in UTF-7
    assert cupepub.junk_offsets("© 2006".encode()) == [0]
    assert cupepub.junk_offsets(b"caf&#x00E9; &#x2019;") == []          # references are clean
    text, n, skipped = cupepub_fixes.to_ascii_refs("<p>caf\u00e9 \u2019x\u2019</p><!-- \u00e9 -->")
    assert text == "<p>caf&#x00E9; &#x2019;x&#x2019;</p><!-- \u00e9 -->" and n == 3 and skipped == 1


def test_link_rule_ignores_text_inside_a_or_span_and_headings(tmp_path):
    files = {"OEBPS/01_12345_ch1.xhtml": b"", "OEBPS/02_12345_ch3.xhtml": (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 3</title></head><body>'
        '<h1>See chapter 3</h1><p><a href="x">chapter 3</a> <span>chapter 3</span> chapter 3 '
        'Part of the book, section in brief</p></body></html>').encode()}
    files["OEBPS/01_12345_ch1.xhtml"] = files["OEBPS/02_12345_ch3.xhtml"]
    book = cupepub.Book(files)
    refs = cupepub.link_matches(book, "OEBPS/01_12345_ch1.xhtml")
    assert [r.text for r in refs] == ["chapter 3"]


# ---------------------------------------------------------------- repairs
def test_junk_characters_become_references_with_identical_text(book, tmp_path):
    out = str(tmp_path / "out.epub")
    res = _apply(book, cupepub_fixes.fix_junk_characters, out)
    assert res.applied
    for name in cup_book.SPINE:
        assert all(b < 0x80 for b in _read(out, name).encode())
    integ = integrity_snapshot.diff(integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out))
    assert integ.ok, integ.summary()
    assert "EPUB-010" not in cupepub.validate_epub(out).by_code()


def test_cross_references_linked_or_spanned(book, tmp_path):
    out = str(tmp_path / "out.epub")
    res = _apply(book, cupepub_fixes.fix_cross_reference_links, out)
    pre = _read(out, "04_91543_fm3.xhtml")
    assert '<a href="07_91543_ch3.xhtml">chapter 3</a>' in pre
    assert '<a href="05_91543_ch1.xhtml#fig1_1">Figure 1.1</a>' in pre          # the full number is linked
    assert '<a href="06_91543_ch2.xhtml#eq3">equation 3</a>' in pre
    assert "<span>Table 9</span>" in pre                                         # no table 9: never guessed
    assert '<a href="http://www.utppublishing.com">www.utppublishing.com</a>' in pre
    assert "<span>[1863]</span>" in _read(out, "03_91543_fm2.xhtml")              # a year, not a reference
    assert any("Table 9" in r["reference"] for r in res.review)
    assert "EPUB-009" not in cupepub.validate_epub(out).by_code()
    integ = integrity_snapshot.diff(integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out))
    assert integ.ok, integ.summary()


def test_index_locators_ranges_and_see_also(book, tmp_path):
    out = str(tmp_path / "out.epub")
    _apply(book, cupepub_fixes.fix_index_links, out)
    idx = _read(out, "08_91543_bm1.xhtml")
    assert '<a href="05_91543_ch1.xhtml#Page_2">2</a>' in idx
    # a range is two links; the abbreviated end points to the full page
    assert ('<a href="07_91543_ch3.xhtml#Page_123">123</a>&#x2013;'
            '<a href="07_91543_ch3.xhtml#Page_125">25</a>') in idx.replace("–", "&#x2013;")
    assert "<span>45n3</span>" in idx                                            # page 45 does not exist
    assert '<a href="04_91543_fm3.xhtml#Page_viii">viii</a>' in idx               # roman page
    # one link over a range is split; a wrong range end is repointed
    assert '<a href="05_91543_ch1.xhtml#Page_1">1</a>' in idx and \
        '<a href="05_91543_ch1.xhtml#Page_3">3</a>' in idx
    assert '#Page_122">22</a>' in idx and "#Page_22" not in idx
    # see also -> the entry (an id is added to it); unknown entry -> span
    assert '<i>see also</i> <a href="#idx-church">Church</a>' in idx and 'id="idx-church"' in idx
    assert '<i>see</i> <a href="#ie-abbey">Abbey</a>' in idx
    assert "<span>Nowhere</span>" in idx and "<span>1863</span>" in idx and "<span>II</span>" in idx
    codes = cupepub.validate_epub(out).by_code()
    for code in ("EPUB-044", "EPUB-045", "EPUB-047"):
        assert code not in codes
    # what remains is exactly the text with no page (spans do not satisfy the tool's index rules)
    assert codes.get("EPUB-043") == 1 and codes.get("EPUB-046") == 1
    integ = integrity_snapshot.diff(integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out))
    assert integ.ok, integ.summary()


def test_opf_identifiers_from_the_copyright_page(book, tmp_path):
    out = str(tmp_path / "out.epub")
    res = _apply(book, cupepub_fixes.fix_opf_identifiers, out)
    opf = _read(out, "content.opf")
    assert 'unique-identifier="isbn-id"' in opf
    assert f'<dc:identifier id="isbn-id">urn:isbn:{cup_book.EBOOK_ISBN}</dc:identifier>' in opf
    assert '<meta refines="#isbn-id" property="identifier-type" scheme="onix:codelist5">15</meta>' in opf
    assert f'<dc:source id="src-id">urn:isbn:{cup_book.PRINT_ISBN}</dc:source>' in opf
    assert '<meta refines="#src-id" property="source-of">pagination</meta>' in opf
    assert "EPUB-014" not in cupepub.validate_epub(out).by_code()
    integ = integrity_snapshot.diff(integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out),
                                    res.allowed)
    assert integ.ok, integ.summary()


def test_guide_landmarks_author_linear(book, tmp_path):
    cur = book
    allowed = integrity_snapshot.Allowed()
    for k, fn in enumerate((cupepub_fixes.fix_opf_guide, cupepub_fixes.fix_nav_landmarks,
                            cupepub_fixes.fix_author_metadata, cupepub_fixes.fix_spine_linear)):
        nxt = str(tmp_path / f"s{k}.epub")
        allowed.merge(_apply(cur, fn, nxt).allowed)
        cur = nxt
    opf, nav = _read(cur, "content.opf"), _read(cur, "nav.xhtml")
    assert '<reference type="cover" title="Cover" href="01_91543_cv.xhtml"/>' in opf
    assert 'title="Begin reading"' in opf and 'linear="no"' not in opf
    assert "<dc:creator id=\"creator1\">James I. Wimsatt</dc:creator>" in opf
    assert '<meta refines="#creator1" property="file-as">Wimsatt, James I.</meta>' in opf
    assert '<h2 id="landmarks">Book Landmarks</h2>' in nav and '<ol class="none">' in nav
    assert '<a epub:type="cover" href="01_91543_cv.xhtml">Cover Page</a>' in nav
    assert '<a epub:type="part" href="05_91543_ch1.xhtml">Begin Reading</a>' in nav
    codes = cupepub.validate_epub(cur).by_code()
    for code in ("EPUB-035", "EPUB-037", "EPUB-038", "EPUB-041"):
        assert code not in codes, code
    integ = integrity_snapshot.diff(integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(cur),
                                    allowed)
    assert integ.ok, integ.summary()


def test_dpi_header_only_pixels_identical(book, tmp_path):
    out = str(tmp_path / "out.epub")
    res = _apply(book, cupepub_fixes.fix_image_dpi, out)
    with zipfile.ZipFile(book) as a, zipfile.ZipFile(out) as b:
        for n in ("OEBPS/images/ch1-fig-01.png", "OEBPS/images/fm2-fig-01.png", "OEBPS/images/cover.jpg"):
            assert integrity_snapshot.pixel_hash(a.read(n)) == integrity_snapshot.pixel_hash(b.read(n))
            want = 300 if "cover" in n else 150
            assert cupepub.image_info(b.read(n))[2:] == (want, want)
    codes = cupepub.validate_epub(out).by_code()
    assert "EPUB-019" not in codes and codes.get("EPUB-021") == 1      # the size is the cover-size repair's job
    inv0, inv1 = integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out)
    assert not integrity_snapshot.diff(inv0, inv1).ok                    # undeclared byte change = loss
    assert integrity_snapshot.diff(inv0, inv1, res.allowed).ok


def test_image_preparation_rules():
    cover = cup_book.image("JPEG", (700, 1014), 200, (120, 30, 30))
    data, notes = images.prepare("OEBPS/images/cover.jpg", cover)
    fmt, w, h, dx, dy = images.info(data)
    assert (fmt, w, h, dx, dy) == ("JPEG", 1200, 1738, 300, 300) and notes
    assert images.cover_size_ok(w, h) and cupepub.cover_ok_019(w, h, dx, dy)
    big = cup_book.image("PNG", (2400, 3000), 72, (0, 0, 0))
    assert images.info(images.prepare("x/cover.png", big)[0])[1:3] == (1200, 1500)
    tall = cup_book.image("JPEG", (600, 1200), 300, (0, 0, 0))
    assert images.info(images.prepare("x/cover.jpg", tall)[0])[1:3] == (900, 1800)
    ok = cup_book.image("JPEG", (1200, 1800), 300, (0, 0, 0))
    assert images.prepare("x/cover.jpg", ok) == (ok, [])                       # already right: untouched
    fig = cup_book.image("PNG", (30, 20), 200, (0, 150, 0))
    new, notes = images.prepare("x/ch3-fig-01.png", fig)
    assert images.info(new)[3:] == (150, 150) and "pixels unchanged" in notes[0]
    assert integrity_snapshot.pixel_hash(new) == integrity_snapshot.pixel_hash(fig)
    icon = cup_book.image("PNG", (10, 10), 72, (0, 0, 0))
    assert images.info(images.prepare("x/ch1-icon-01.png", icon)[0])[3:] == (135, 135)


def test_cover_size_repair_is_declared(book, tmp_path):
    out = str(tmp_path / "out.epub")
    res = _apply(book, cupepub_fixes.fix_cover_size, out)
    with zipfile.ZipFile(out) as z:
        fmt, w, h, dx, dy = images.info(z.read("OEBPS/images/cover.jpg"))
    assert (w, h, dx) == (1200, round(101 * 1200 / 70), 300)
    codes = cupepub.validate_epub(out).by_code()
    assert "EPUB-021" not in codes
    inv0, inv1 = integrity_snapshot.build_inventory(book), integrity_snapshot.build_inventory(out)
    assert not integrity_snapshot.diff(inv0, inv1).ok                           # undeclared resize = loss
    assert integrity_snapshot.diff(inv0, inv1, res.allowed).ok


# ------------------------------------------------------------ front matter
def test_front_matter_metadata(book):
    with zipfile.ZipFile(book) as z:
        docs = [(n, integrity_snapshot.parse_lenient(z.read("OEBPS/" + n))[0]) for n in cup_book.SPINE]
    fm = front_matter.extract(docs)
    assert fm.title == "Chaucer and the Poems of “Ch”" and fm.authors == ["James I. Wimsatt"]
    assert fm.publisher == "University of Toronto Press"
    assert fm.rights == "© University of Toronto Press Incorporated 2006" and fm.date == "2006-01-01T00:00:00Z"
    assert fm.ebook_isbn() == cup_book.EBOOK_ISBN and fm.print_isbn() == cup_book.PRINT_ISBN
    assert front_matter.file_as("James I. Wimsatt") == "Wimsatt, James I."
    assert front_matter.split_authors("Jane Roe and John Doe") == ["Jane Roe", "John Doe"]


@epubcheck
def test_generated_opf_follows_the_client_format(tmp_path):
    from core.epub_structure import orchestrator
    root = tmp_path / "book"
    with zipfile.ZipFile(cup_book.build(str(tmp_path / "src.epub"))) as z:
        z.extractall(root)
    for n in ("OEBPS/content.opf", "OEBPS/nav.xhtml", "OEBPS/toc.ncx"):
        os.remove(root / n)
    idx = root / "OEBPS" / "08_91543_bm1.xhtml"                  # the fixture's deliberate broken range end
    idx.write_text(idx.read_text(encoding="utf-8").replace("#Page_22", "#Page_122"), encoding="utf-8")
    res = orchestrator.run_full_analysis(str(root), run_epubcheck=False)       # no ISBN typed in
    assert res.isbn_sources.get("normal", "").startswith("copyright page")
    with zipfile.ZipFile(res.output_epub_path) as z:
        opf_name = next(n for n in z.namelist() if n.endswith(".opf"))
        opf = z.read(opf_name).decode()
    assert f'<dc:identifier id="isbn-id">urn:isbn:{cup_book.EBOOK_ISBN}</dc:identifier>' in opf
    assert f'<dc:source id="src-id">urn:isbn:{cup_book.PRINT_ISBN}</dc:source>' in opf
    assert '<dc:creator id="creator1">James I. Wimsatt</dc:creator>' in opf
    assert 'property="file-as">Wimsatt, James I.</meta>' in opf
    assert "<dc:publisher>University of Toronto Press</dc:publisher>" in opf
    assert "<dc:rights>© University of Toronto Press Incorporated 2006</dc:rights>" in opf
    assert "<dc:date>2006-01-01T00:00:00Z</dc:date>" in opf
    with zipfile.ZipFile(res.output_epub_path) as z:                             # images prepared for the client
        cov = images.info(z.read(next(n for n in z.namelist() if n.endswith("cover.jpg"))))
        fig = images.info(z.read(next(n for n in z.namelist() if n.endswith("ch1-fig-01.png"))))
    assert cov[1:] == (1200, round(101 * 1200 / 70), 300, 300) and fig[3:] == (150, 150)
    assert images.info((root / "OEBPS" / "images" / "cover.jpg").read_bytes())[1:3] == (70, 101)   # source untouched
    assert any("cover resized" in "; ".join(n) for _p, n in res.image_preparation)
    assert '<meta name="cover" content="cover-image"/>' in opf and 'properties="cover-image"' in opf
    order = [opf.index(t) for t in ("<dc:title", "<dc:creator", "schema:accessibilitySummary", "<dc:language",
                                    "<dc:publisher", "<dc:identifier", "<dc:source", "<dc:date", "dcterms:modified",
                                    "<dc:rights", 'name="cover"')]
    assert order == sorted(order)
    rep = cupepub.validate_epub(res.output_epub_path)
    for code in ("EPUB-014", "EPUB-023", "EPUB-041"):
        assert code not in rep.by_code(), [f.message for f in rep.findings if f.code == code]
    r = runner.run_epubcheck(res.output_epub_path)
    assert r.is_valid, [(m.code, m.message) for m in r.messages]


# --------------------------------------------------------------- closed loop
@epubcheck
def test_closed_loop_with_client_rules(book, tmp_path):
    sha = repair_engine._sha256(book)
    log = tmp_path / "spix.log"
    log.write_text(LOG, encoding="utf-8")
    rep = repair_engine.run_full_auto_repair(book, client_profile="CUPEPUB", client_log=str(log))
    assert repair_engine._sha256(book) == sha
    assert rep.content_integrity_verified and rep.dashboard["EPUBCheck"]["status"] == "PASS"
    before, after = rep.client_before["counts"], rep.client_after["counts"]
    assert after["Error"] + after["Warning"] < (before["Error"] + before["Warning"]) / 5
    assert os.path.basename(rep.delivery_path) == f"{cup_book.EBOOK_ISBN}.epub"
    left = set(rep.client_after["by_code"])
    assert left <= {"EPUB-024", "EPUB-026", "EPUB-032", "EPUB-043", "EPUB-046"}, left
    assert "Client rules (CUPEPUB)" in rep.dashboard
    assert any(r["root_cause"] == "CLIENT RULES (CUPEPUB)" for r in rep.remaining_issues)
    assert rep.client_tool_log["totals"] == {"Error": 3, "Warning": 2}
    assert os.path.isfile(rep.reports["Client validation - final"])
    assert all(h["status"] == "COMMITTED" for h in rep.history if h["pass"] == "CLIENT")
