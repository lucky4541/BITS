"""Missing page numbers in EPUB Structure generation.

The synthetic project mirrors a real book whose chapters each end on an
unmarked blank verso page: markers vii-viii, 1-45, 47-72, 74-95, 97-123,
125-162. Expected: 46 / 73 / 96 / 124 filled at the end of the chapter
before each gap and present in the NAV page-list; front-matter i-vi
reported (not invented); nothing else changed.

Run:  python -m pytest tests/test_page_gap_filler.py -q
"""
import os
import re
import sys

from lxml import etree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.epub_structure import orchestrator  # noqa: E402

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><title>{title}</title></head>
<body epub:type="{btype}"><section epub:type="{stype}" id="{sid}">
<h1>{title}</h1>
{paras}
</section></body></html>
"""
PB = '<span epub:type="pagebreak" role="doc-pagebreak" id="page_{label}" aria-label="{label}"/>'

DOCS = [  # file, title, labels
    ("fm1.xhtml", "Preface", ["vii", "viii"]),
    ("fm9.xhtml", "Introduction", [str(n) for n in range(1, 18)]),
    ("ch1.xhtml", "Chapter One", [str(n) for n in range(18, 46)]),
    ("ch2.xhtml", "Chapter Two", [str(n) for n in range(47, 73)]),
    ("ch3.xhtml", "Chapter Three", [str(n) for n in range(74, 96)]),
    ("ch4.xhtml", "Chapter Four", [str(n) for n in range(97, 124)]),
    ("ch5.xhtml", "Chapter Five", [str(n) for n in range(125, 142)]),
    ("bm1.xhtml", "Conclusion", [str(n) for n in range(142, 163)]),
]


def build_project(root):
    text_dir = os.path.join(root, "OEBPS", "xhtml")
    os.makedirs(text_dir, exist_ok=True)
    for k, (name, title, labels) in enumerate(DOCS):
        paras = "\n".join(f"<p>{PB.format(label=lab)}Text of page {lab} in {title.lower()}.</p>" for lab in labels)
        btype = "frontmatter" if name.startswith("fm") else ("backmatter" if name.startswith("bm") else "bodymatter")
        with open(os.path.join(text_dir, name), "w", encoding="utf-8") as f:
            f.write(XHTML.format(title=title, btype=btype, stype="chapter", sid=f"sec{k}", paras=paras))
    # the book's own reading order comes from its OPF spine (not from file names)
    items = "".join(f'<item id="d{k}" href="xhtml/{n}" media-type="application/xhtml+xml"/>'
                    for k, (n, _t, _l) in enumerate(DOCS))
    spine = "".join(f'<itemref idref="d{k}"/>' for k in range(len(DOCS)))
    with open(os.path.join(root, "OEBPS", "content.opf"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                'version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="uid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>'
                '<dc:title>Gap Book</dc:title><dc:language>en</dc:language></metadata>'
                f'<manifest>{items}</manifest><spine>{spine}</spine></package>')


def page_list_labels(root):
    nav = None
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith((".xhtml", ".html")):
                path = os.path.join(dirpath, name)
                data = open(path, encoding="utf-8").read()
                if 'epub:type="page-list"' in data:
                    nav = etree.parse(path)
    assert nav is not None, "no page-list generated"
    ns = {"x": "http://www.w3.org/1999/xhtml", "epub": "http://www.idpf.org/2007/ops"}
    pl = nav.xpath("//x:nav[@epub:type='page-list']", namespaces=ns)[0]
    return [("".join(a.itertext()).strip(), a.get("href")) for a in pl.iter("{http://www.w3.org/1999/xhtml}a")]


def test_blank_chapter_end_pages_are_filled(tmp_path):
    root = str(tmp_path / "book")
    build_project(root)
    res = orchestrator.run_full_analysis(root, run_epubcheck=False)
    assert not res.error, res.error
    assert not res.rolled_back
    gap = res.page_gap_result
    filled = {f.label: f for f in gap.filled}
    assert set(filled) == {"46", "73", "96", "124"}
    expected_doc = {"46": "ch1.xhtml", "73": "ch2.xhtml", "96": "ch3.xhtml", "124": "ch4.xhtml"}
    for label, f in filled.items():
        assert f.doc_path.endswith(expected_doc[label])
        assert f.confidence == "HIGH"
    # front matter i-vi reported, never invented
    leading = [g for g in gap.unfilled if g.kind == "leading"]
    assert leading and leading[0].labels == ["i", "ii", "iii", "iv", "v", "vi"]
    # page-list is now contiguous from vii to 162, in reading order
    labels = [lab for lab, _href in page_list_labels(root)]
    assert labels == ["vii", "viii"] + [str(n) for n in range(1, 163)]
    hrefs = dict(page_list_labels(root))
    assert hrefs["46"].endswith("ch1.xhtml#page_46")
    # the marker is the LAST thing in chapter 1 (blank page after 45), empty, same house style
    ch1 = etree.parse(os.path.join(root, "OEBPS", "xhtml", "ch1.xhtml"))
    last = [el for el in ch1.iter() if el.get("{http://www.idpf.org/2007/ops}type") == "pagebreak"][-1]
    assert last.get("aria-label") == "46" and last.get("id") == "page_46"
    assert last.get("role") == "doc-pagebreak" and not (last.text or "").strip()
    assert last.getnext() is None
    assert "MISSING PAGE NUMBERS" in __import__("core.epub_structure.validator", fromlist=["x"]).render_report(
        res.report)


def test_gap_inside_one_document_is_filled_before_next_page_for_review(tmp_path):
    root = str(tmp_path / "book")
    build_project(root)
    path = os.path.join(root, "OEBPS", "xhtml", "ch5.xhtml")
    data = open(path, encoding="utf-8").read()
    data = re.sub(r'<span epub:type="pagebreak" role="doc-pagebreak" id="page_130" aria-label="130"/>', "", data)
    open(path, "w", encoding="utf-8").write(data)
    res = orchestrator.run_full_analysis(root, run_epubcheck=False)
    f = next(f for f in res.page_gap_result.filled if f.label == "130")
    assert f.confidence == "MEDIUM" and f.doc_path.endswith("ch5.xhtml")
    assert res.report.page_numbers_review >= 1
    assert "130" in [lab for lab, _h in page_list_labels(root)]


def test_option_off_only_reports(tmp_path):
    root = str(tmp_path / "book")
    build_project(root)
    res = orchestrator.run_full_analysis(root, run_epubcheck=False, fill_missing_pages=False)
    assert res.page_gap_result.filled == []
    assert res.page_gap_result.missing_detected == 4 + 6
    assert "46" not in [lab for lab, _h in page_list_labels(root)]
