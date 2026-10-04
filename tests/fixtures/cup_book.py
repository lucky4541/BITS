"""A small CUP-style EPUB 3 ("15431.epub") with the problems the client's
validation tool (SPiXVali, Client CUPEPUB) reported on a real delivery:

  EPUB-002  the file is not named <ISBN>.epub
  EPUB-010  literal non-ASCII characters (quotes, dashes, accents) in XHTML
  EPUB-011  a double-escaped reference "&amp;#x2019;"
  EPUB-014  identifier id="pub-id"; no print ISBN <dc:source id="src-id">
  EPUB-019  images at 200 / 144 DPI, the cover at 200 DPI
  EPUB-021  cover not 1200 x 1800 (needs a new cover - review only)
  EPUB-009  "chapter 3", "Figure 1.1", "equation 3", "Table 9" (no table 9),
            "[1863]" (a year, no reference), "www.utppublishing.com"
  EPUB-026  page "viii" twice in the preface (review only)
  EPUB-035  OPF guide without Cover, with "Begin Reading" (tool wants
            "Begin reading"); nav landmarks without "Cover Page", Begin
            Reading as bodymatter (tool wants epub:type="part")
  EPUB-037  landmarks heading <h2>Guide</h2>, <ol> without class="none"
  EPUB-038  cover linear="no"
  EPUB-041  dc:creator "Wimsatt, James I." / title page "James I. Wimsatt"
  EPUB-043  index locators as plain text (incl. 123-25 and 45n3; page 45
            does not exist), "Treaty of 1863" (a year)
  EPUB-044  "<a>120</a>-<a>22</a>" second link to Page_22 instead of Page_122
  EPUB-045  one link over the range "1-3"
  EPUB-046  "Wars, II" (upper-case roman, no page)
  EPUB-047  "see also Church" (an entry) and "see Nowhere" (no entry)

The copyright page carries both ISBNs: cloth (print) and EPUB (e-book)."""
import io
import zipfile

from PIL import Image


def isbn13(first12):
    check = (10 - sum(int(d) * (1 if k % 2 == 0 else 3) for k, d in enumerate(first12)) % 10) % 10
    return first12 + str(check)


PRINT_ISBN = isbn13("978080209154")
EBOOK_ISBN = isbn13("978144268123")

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" '
        'xml:lang="en">\n<head>\n<title>{title}</title>\n'
        '<link rel="stylesheet" type="text/css" href="template.css"/>\n</head>\n<body>\n')
TAIL = "</body>\n</html>\n"


def pb(label):
    return (f'<span epub:type="pagebreak" role="doc-pagebreak" id="Page_{label}" aria-label="{label}" '
            f'title="{label}"/>')


def hyph(i):
    return f"{i[:3]}-{i[3]}-{i[4:8]}-{i[8:12]}-{i[12]}"


FILES = {}


def _doc(name, title, body):
    FILES[name] = HEAD.format(title=title) + body + TAIL


_doc("01_91543_cv.xhtml", "Cover Page",
     '<section epub:type="cover">\n<img src="images/cover.jpg" alt="Cover"/>\n</section>\n')
_doc("02_91543_fm1.xhtml", "Title Page",
     '<section epub:type="titlepage">\n' + pb("iii") +
     '\n<h1 class="booktitle">Chaucer and the Poems of “Ch”</h1>\n'
     '<p class="bookauthor">James I. Wimsatt</p>\n'
     '<p class="bookpublisher">University of Toronto Press</p>\n</section>\n')
_doc("03_91543_fm2.xhtml", "Copyright Page",
     '<section epub:type="copyright-page">\n' + pb("iv") +
     '\n<p>© University of Toronto Press Incorporated 2006<br/>Toronto Buffalo London<br/>'
     'Printed in Canada</p>\n'
     f'<p>ISBN {hyph(PRINT_ISBN)} (cloth)</p>\n<p>ISBN {hyph(EBOOK_ISBN)} (EPUB)</p>\n'
     '<p>First edition published by D.S. Brewer [1863].</p>\n'
     '<p><img src="images/fm2-fig-01.png" alt="Logo"/></p>\n</section>\n')
_doc("04_91543_fm3.xhtml", "Preface",
     '<section epub:type="preface">\n' + pb("vii") +
     '\n<header><h1 class="fmtitle">Preface</h1></header>\n'
     '<p>The argument of chapter 3 rests on Figure 1.1 and on equation 3; Table 9 is not in this book.</p>\n'
     '<p>Its readers’ “responses” are discussed — briefly — in Chapter 1. See www.utppublishing.com.</p>\n'
     + pb("viii") + '\n<p>A repeated page marker follows.</p>\n' + pb("viii").replace('id="Page_viii"',
                                                                                     'id="Page_viii_2"') +
     '\n</section>\n')
_doc("05_91543_ch1.xhtml", "1 Beginnings",
     '<section epub:type="chapter">\n' + pb("1") +
     '\n<header><h1 class="Chapter-Number">1</h1><h1 class="Chapter-Title">Beginnings</h1></header>\n'
     '<p>The poet&amp;#x2019;s café years, as chapter 2 shows.</p>\n'
     '<figure id="fig1_1"><img src="images/ch1-fig-01.png" alt="A diagram"/>'
     '<figcaption>Figure 1.1 A diagram</figcaption></figure>\n' + pb("2") + '\n<p>More text.</p>\n'
     + pb("3") + '\n<p>End of the first chapter.</p>\n</section>\n')
_doc("06_91543_ch2.xhtml", "2 Middles",
     '<section epub:type="chapter">\n' + pb("4") +
     '\n<header><h1 class="Chapter-Number">2</h1><h1 class="Chapter-Title">Middles</h1></header>\n'
     '<p>As stated in chapter 1, the form is</p>\n'
     '<div class="equation" id="eq3"><p>x = y (3)</p></div>\n' + pb("5") + '\n<p>Text.</p>\n'
     + pb("6") + '\n<p>Text.</p>\n</section>\n')
_doc("07_91543_ch3.xhtml", "3 Ends",
     '<section epub:type="chapter">\n' + pb("7") +
     '\n<header><h1 class="Chapter-Number">3</h1><h1 class="Chapter-Title">Ends</h1></header>\n'
     + "".join(pb(str(p)) + f"\n<p>Page {p} text.</p>\n" for p in (8, 9, 120, 121, 122, 123, 124, 125)) +
     '</section>\n')
_doc("08_91543_bm1.xhtml", "Index",
     '<section epub:type="index">\n' + pb("126") +
     '\n<header><h1 class="indtitle">Index</h1></header>\n<ul class="none">\n'
     '<li class="index" id="ie-abbey">Abbey, 2, 5–7, 123–25, 45n3, viii; <i>see also</i> Church</li>\n'
     '<li class="index">Church, <a href="05_91543_ch1.xhtml#Page_1">1–3</a>, 8</li>\n'
     '<li class="index">Monastery, <a href="07_91543_ch3.xhtml#Page_120">120</a>–'
     '<a href="07_91543_ch3.xhtml#Page_22">22</a>; <i>see</i> Abbey</li>\n'
     '<li class="index">Treaty of 1863, 4</li>\n'
     '<li class="index">Wars, II, 9; <i>see</i> Nowhere</li>\n'
     '<li class="index">Preface, vii–viii</li>\n'
     '</ul>\n</section>\n')

SPINE = ["01_91543_cv.xhtml", "02_91543_fm1.xhtml", "03_91543_fm2.xhtml", "04_91543_fm3.xhtml",
         "05_91543_ch1.xhtml", "06_91543_ch2.xhtml", "07_91543_ch3.xhtml", "08_91543_bm1.xhtml"]
PAGES = [("01_91543_cv.xhtml", None)] + [(f, p) for f, ps in (
    ("02_91543_fm1.xhtml", ["iii"]), ("03_91543_fm2.xhtml", ["iv"]), ("04_91543_fm3.xhtml", ["vii", "viii"]),
    ("05_91543_ch1.xhtml", ["1", "2", "3"]), ("06_91543_ch2.xhtml", ["4", "5", "6"]),
    ("07_91543_ch3.xhtml", ["7", "8", "9", "120", "121", "122", "123", "124", "125"]),
    ("08_91543_bm1.xhtml", ["126"])) for p in ps]

NAV = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
       '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" '
       'xml:lang="en">\n<head>\n<title>Contents</title>\n</head>\n<body>\n'
       '<nav epub:type="toc" role="doc-toc" id="toc">\n<h1>Contents</h1>\n<ol class="none">\n'
       + "".join(f'<li><a href="{f}">{t}</a></li>\n' for f, t in (
           ("01_91543_cv.xhtml", "Cover"), ("02_91543_fm1.xhtml", "Title Page"), ("04_91543_fm3.xhtml", "Preface"),
           ("05_91543_ch1.xhtml", "1 Beginnings"), ("06_91543_ch2.xhtml", "2 Middles"),
           ("07_91543_ch3.xhtml", "3 Ends"), ("08_91543_bm1.xhtml", "Index"))) +
       '</ol>\n</nav>\n'
       '<nav epub:type="landmarks" id="guide" hidden="hidden">\n<h2>Guide</h2>\n<ol>\n'
       '<li><a epub:type="toc" href="nav.xhtml#toc">Contents</a></li>\n'
       '<li><a epub:type="bodymatter" href="05_91543_ch1.xhtml">Begin Reading</a></li>\n'
       '<li><a epub:type="index" href="08_91543_bm1.xhtml">Index</a></li>\n'
       '</ol>\n</nav>\n'
       '<nav epub:type="page-list" role="doc-pagelist" hidden="hidden">\n<ol class="none">\n'
       + "".join(f'<li><a href="{f}#Page_{p}">{p}</a></li>\n' for f, p in PAGES if p) +
       '</ol>\n</nav>\n</body>\n</html>\n')

NCX = ('<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
       f'<head><meta name="dtb:uid" content="urn:isbn:{EBOOK_ISBN}"/></head>\n'
       '<docTitle><text>Chaucer</text></docTitle>\n<navMap>\n'
       + "".join(f'<navPoint id="np{k}" playOrder="{k}"><navLabel><text>{t}</text></navLabel>'
                 f'<content src="{f}"/></navPoint>\n'
                 for k, (f, t) in enumerate((("05_91543_ch1.xhtml", "1 Beginnings"),
                                             ("06_91543_ch2.xhtml", "2 Middles"),
                                             ("07_91543_ch3.xhtml", "3 Ends")), 1)) +
       '</navMap>\n<pageList>\n'
       + "".join(f'<pageTarget id="pt{k}" type="normal" value="{p}"><navLabel><text>{p}</text></navLabel>'
                 f'<content src="{f}#Page_{p}"/></pageTarget>\n' for k, (f, p) in enumerate(
                     [x for x in PAGES if x[1]], 1)) +
       '</pageList>\n</ncx>\n')


def opf():
    items = "".join(f'<item id="x{k:02d}" href="{f}" media-type="application/xhtml+xml"/>\n'
                    for k, f in enumerate(SPINE, 1))
    linear_no = ' linear="no"'
    spine = "".join(f'<itemref idref="x{k:02d}"{linear_no if k == 1 else ""}/>\n' + (
        '<itemref idref="nav"/>\n' if k == 3 else "") for k in range(1, len(SPINE) + 1))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" '
            'xml:lang="en">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '<dc:title id="t1">Chaucer and the Poems of Ch</dc:title>\n'
            '<meta refines="#t1" property="display-seq">1</meta>\n'
            '<dc:creator id="creator1">Wimsatt, James I.</dc:creator>\n'
            '<meta refines="#creator1" property="role" scheme="marc:relators" id="role1">aut</meta>\n'
            '<meta property="schema:accessibilitySummary">This publication conforms to the EPUB Accessibility '
            'specification at WCAG Level 2.0 AA.</meta>\n'
            '<meta property="schema:accessMode">textual</meta>\n'
            '<meta property="schema:accessMode">visual</meta>\n'
            '<meta property="schema:accessModeSufficient">textual,visual</meta>\n'
            '<meta property="schema:accessibilityHazard">none</meta>\n'
            '<meta property="schema:accessibilityFeature">tableOfContents</meta>\n'
            '<meta property="schema:accessibilityFeature">structuralNavigation</meta>\n'
            '<meta property="schema:accessibilityFeature">printPageNumbers</meta>\n'
            '<meta property="schema:accessibilityFeature">alternativeText</meta>\n'
            '<meta property="schema:accessibilityFeature">index</meta>\n'
            '<dc:language>en</dc:language>\n'
            '<dc:publisher>University of Toronto Press</dc:publisher>\n'
            f'<dc:identifier id="pub-id">urn:isbn:{EBOOK_ISBN}</dc:identifier>\n'
            '<meta refines="#pub-id" property="identifier-type" scheme="onix:codelist5">15</meta>\n'
            '<dc:date>2006-01-01T00:00:00Z</dc:date>\n'
            '<meta property="dcterms:modified">2026-10-01T00:00:00Z</meta>\n'
            '<dc:rights>© University of Toronto Press Incorporated 2006</dc:rights>\n'
            '<meta name="cover" content="cover-image"/>\n'
            '</metadata>\n<manifest>\n'
            '<item id="css" href="template.css" media-type="text/css"/>\n'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            + items +
            '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>\n'
            '<item id="img1" href="images/fm2-fig-01.png" media-type="image/png"/>\n'
            '<item id="img2" href="images/ch1-fig-01.png" media-type="image/png"/>\n'
            '</manifest>\n<spine toc="ncx">\n' + spine + '</spine>\n'
            '<guide>\n<reference type="toc" title="Table of Contents" href="nav.xhtml"/>\n'
            '<reference type="text" title="Begin Reading" href="05_91543_ch1.xhtml"/>\n'
            '<reference type="index" title="Index" href="08_91543_bm1.xhtml"/>\n</guide>\n'
            '</package>\n')


def image(fmt, size, dpi, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, dpi=(dpi, dpi))
    return buf.getvalue()


def build(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container">\n<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/'
                   'oebps-package+xml"/></rootfiles>\n</container>\n', zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf())
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/toc.ncx", NCX)
        z.writestr("OEBPS/template.css", "/* CUP template Version 1.0 */\nbody { margin: 0; }\n"
                                         "ol.none { list-style: none; }\n")
        for name in SPINE:
            z.writestr("OEBPS/" + name, FILES[name])
        z.writestr("OEBPS/images/cover.jpg", image("JPEG", (70, 101), 200, (120, 30, 30)))
        z.writestr("OEBPS/images/fm2-fig-01.png", image("PNG", (20, 10), 144, (0, 0, 200)))
        z.writestr("OEBPS/images/ch1-fig-01.png", image("PNG", (30, 20), 200, (0, 150, 0)))
    return path
