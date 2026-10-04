"""Synthetic PDF book + EPUB project for the PDF <-> XHTML QC acceptance
tests. Both are generated from ONE content spec, so the clean EPUB is a
faithful conversion of the PDF; `defects` seeds known problems into the
EPUB so each difference type can be verified against ground truth.

PDF (6 pages):
  1  title page (unnumbered)
  2  preface                         printed "ii"
  3  chapter one: paragraphs A B C   printed "1"
  4  paragraph D, IMAGE 1 + caption, paragraph E   printed "2"
  5  paragraph F, IMAGE 2 + caption, paragraph G   printed "3"
  6  chapter two: paragraphs H I J K printed "4"
Running header "THE SILENT WOOD" on pages 2-6.
"""
import io
import os

import fitz

PAGE_W, PAGE_H = 432.0, 648.0
LEFT, RIGHT = 54, 378
SIZE, LEAD = 10, 13
TITLE = "The Silent Wood"
PREFACE = ("This preface explains how the small book came to be written over many quiet winters "
           "spent beside the edge of the old forest.")
A = "The quick brown fox jumps over the lazy dog while the morning mist lifts slowly from the valley floor."
B = "Every traveller who passed along the road stopped for a moment to listen to the silence of the trees."
C = ("The local organization kept careful records of the colour of the leaves through every season "
     "of the year.")
D = "Later that week the first heavy snow arrived and covered every path that led into the dark wood."
E = ("Nobody expected the snow to remain for so long, and the villagers soon grew anxious about "
     "their stores of grain and firewood.")
F = "In the spring the river rose quickly and flooded the lower meadows where the sheep usually grazed."
G = "By midsummer the water had returned to its banks and the meadows were green again under a clear sky."
H = "The second chapter begins on a bright autumn morning when the old miller set out for the market town."
I_ = "He carried two sacks of flour on his cart and whistled a tune that nobody in the village recognised."
J = "At the crossroads he met a stranger who asked the way to the abandoned house beyond the hill."
K = "The miller pointed north and warned the stranger that the house had been empty for many years."
CAP1 = "Figure 1.1 A red square drawn on a pale background."
CAP2 = "Figure 1.2 A blue circle drawn on a white background."


def _png(kind):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (240, 160), (245, 240, 230) if kind == 1 else (255, 255, 255))
    d = ImageDraw.Draw(im)
    if kind == 1:
        d.rectangle([50, 20, 190, 140], fill=(200, 30, 30))
        d.line([0, 0, 240, 160], fill=(0, 0, 0), width=6)
    else:
        d.ellipse([70, 10, 210, 150], fill=(30, 60, 200))
        d.rectangle([0, 120, 60, 160], fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _wrap(text, width, size=SIZE, font="tiro"):
    out, cur = [], ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if fitz.get_text_length(cand, fontname=font, fontsize=size) <= width:
            cur = cand
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def build_pdf(path):
    doc = fitz.open()

    def page(number_label=None, header=True):
        p = doc.new_page(width=PAGE_W, height=PAGE_H)
        if header:
            p.insert_text((LEFT, 28), "THE SILENT WOOD", fontname="tiro", fontsize=7)
        if number_label:
            p.insert_text((PAGE_W / 2 - 4, PAGE_H - 26), number_label, fontname="tiro", fontsize=9)
        return p

    def para(p, y, text, indent=14):
        for k, ln in enumerate(_wrap(text, RIGHT - LEFT - indent)):
            p.insert_text((LEFT + (indent if k == 0 else 0), y), ln, fontname="tiro", fontsize=SIZE)
            y += LEAD
        return y + 6

    p = page(header=False)
    p.insert_text((LEFT, 200), TITLE, fontname="tibo", fontsize=24)
    p = page("ii")
    p.insert_text((LEFT, 90), "Preface", fontname="tibo", fontsize=16)
    para(p, 120, PREFACE)
    p = page("1")
    p.insert_text((LEFT, 90), "Chapter One", fontname="tibo", fontsize=16)
    y = para(p, 125, A, indent=0)
    y = para(p, y, B)
    para(p, y, C)
    p = page("2")
    y = para(p, 80, D)
    p.insert_image(fitz.Rect(96, y + 4, 336, y + 164), stream=_png(1))
    y += 180
    p.insert_text((LEFT + 20, y), CAP1, fontname="tiit", fontsize=8.5)
    para(p, y + 26, E)
    p = page("3")
    y = para(p, 80, F)
    p.insert_image(fitz.Rect(96, y + 4, 336, y + 164), stream=_png(2))
    y += 180
    p.insert_text((LEFT + 20, y), CAP2, fontname="tiit", fontsize=8.5)
    para(p, y + 26, G)
    p = page("4")
    p.insert_text((LEFT, 90), "Chapter Two", fontname="tibo", fontsize=16)
    y = para(p, 125, H, indent=0)
    y = para(p, y, I_)
    y = para(p, y, J)
    para(p, y, K)
    doc.save(path)
    doc.close()


XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
{body}
</body>
</html>
"""


def _pb(label):
    return f'<span epub:type="pagebreak" role="doc-pagebreak" id="page_{label}" aria-label="{label}"/>'


def _fig(n, src, caption):
    cap = f"<figcaption><p>{caption}</p></figcaption>" if caption else ""
    return f'<figure id="fig{n}"><img src="images/{src}" alt="figure {n}"/>{cap}</figure>'


def build_epub(root, defects=()):
    defects = set(defects)
    os.makedirs(os.path.join(root, "META-INF"), exist_ok=True)
    os.makedirs(os.path.join(root, "OEBPS", "images"), exist_ok=True)
    with open(os.path.join(root, "mimetype"), "w") as f:
        f.write("application/epub+zip")
    with open(os.path.join(root, "META-INF", "container.xml"), "w") as f:
        f.write('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>')
    for n in (1, 2):
        with open(os.path.join(root, "OEBPS", "images", f"fig{n}.png"), "wb") as f:
            f.write(_png(n))
    with open(os.path.join(root, "OEBPS", "style.css"), "w") as f:
        f.write("body { font-family: serif; }\n")

    a = A.replace(" jumps", "") if "missing_word" in defects else A
    b = B.replace("stopped for", "suddenly stopped for") if "extra_word" in defects else B
    c = C.replace("organization", "organisation").replace("colour", "color") if "modified" in defects else C
    e = E
    fig1_src, fig2_src = ("fig2.png", "fig1.png") if "image_swap" in defects else ("fig1.png", "fig2.png")
    cap2 = None if "missing_caption" in defects else CAP2
    marker2 = "" if "missing_marker" in defects else _pb("2")
    marker3 = _pb("9").replace('id="page_9"', 'id="page_3"') if "wrong_marker" in defects else _pb("3")
    if "image_moved" in defects:
        img = f'<img src="images/{fig1_src}" alt="figure 1"/>'
        d_block = f"<p>{img} {D}</p>"
        fig1 = f'<figure id="fig1"><figcaption><p>{CAP1}</p></figcaption></figure>'
    else:
        d_block = f"<p>{D}</p>"
        fig1 = _fig(1, fig1_src, CAP1)
    if "split_paragraph" in defects:
        cut = e.index(", and") + 1
        e_block = f"<p>{e[:cut]}</p>\n<p>{e[cut + 1:]}</p>"
    else:
        e_block = f"<p>{e}</p>"
    link = '<p><a href="#nowhere">see the note</a></p>' if "broken_link" in defects else ""
    ch1 = (f'<section id="ch1">{_pb("1")}<h1 id="ch1-title">Chapter One</h1>\n<p>{a}</p>\n<p>{b}</p>\n<p>{c}</p>\n'
           f'{marker2}{d_block}\n{fig1}\n{e_block}\n{marker3}<p>{F}</p>\n{_fig(2, fig2_src, cap2)}\n<p>{G}</p>'
           f'{link}</section>')
    h, i = (I_, H) if "reorder" in defects else (H, I_)
    jk = f"<p>{J} {K}</p>" if "merged" in defects else f"<p>{J}</p>\n<p>{K}</p>"
    dup = f"<p>{D}</p>" if "duplicate" in defects else ""
    ch2 = (f'<section id="ch2">{_pb("4")}<h1 id="ch2-title">Chapter Two</h1>\n<p>{h}</p>\n<p>{i}</p>\n{jk}\n'
           f'{dup}</section>')
    docs = {
        "title.xhtml": ("Title", f"<h1 id=\"title\">{TITLE}</h1>"),
        "preface.xhtml": ("Preface", f'{_pb("ii")}<h1 id="preface">Preface</h1>\n<p>{PREFACE}</p>'),
        "ch1.xhtml": ("Chapter One", ch1),
        "ch2.xhtml": ("Chapter Two", ch2),
    }
    for name, (title, body) in docs.items():
        with open(os.path.join(root, "OEBPS", name), "w", encoding="utf-8") as f:
            f.write(XHTML.format(title=title, body=body))
    nav = ('<nav epub:type="toc" id="toc"><ol>'
           '<li><a href="title.xhtml#title">The Silent Wood</a></li>'
           '<li><a href="preface.xhtml#preface">Preface</a></li>'
           '<li><a href="ch1.xhtml#ch1-title">Chapter One</a></li>'
           '<li><a href="ch2.xhtml#ch2-title">Chapter Two</a></li></ol></nav>\n'
           '<nav epub:type="page-list" hidden="hidden"><ol>'
           '<li><a href="preface.xhtml#page_ii">ii</a></li><li><a href="ch1.xhtml#page_1">1</a></li>'
           '<li><a href="ch1.xhtml#page_2">2</a></li><li><a href="ch1.xhtml#page_3">3</a></li>'
           '<li><a href="ch2.xhtml#page_4">4</a></li></ol></nav>')
    with open(os.path.join(root, "OEBPS", "nav.xhtml"), "w", encoding="utf-8") as f:
        f.write(XHTML.format(title="Contents", body=nav))
    dup_css = '<item id="css2" href="style.css" media-type="text/css"/>' if "manifest_dup" in defects else ""
    ncx = ('<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           '<head><meta name="dtb:uid" content="urn:test:qc"/></head><docTitle><text>The Silent Wood</text></docTitle>'
           '<navMap>'
           '<navPoint id="np1" playOrder="1"><navLabel><text>Preface</text></navLabel>'
           '<content src="preface.xhtml#preface"/></navPoint>'
           '<navPoint id="np2" playOrder="2"><navLabel><text>Chapter One</text></navLabel>'
           '<content src="ch1.xhtml#ch1-title"/></navPoint>'
           '<navPoint id="np3" playOrder="3"><navLabel><text>Chapter Two</text></navLabel>'
           '<content src="ch2.xhtml#ch2-title"/></navPoint>'
           '</navMap></ncx>')
    with open(os.path.join(root, "OEBPS", "toc.ncx"), "w", encoding="utf-8") as f:
        f.write(ncx)
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:test:qc</dc:identifier>
<dc:title>{TITLE}</dc:title><dc:language>en</dc:language></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
<item id="preface" href="preface.xhtml" media-type="application/xhtml+xml"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/>{dup_css}
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="img1" href="images/fig1.png" media-type="image/png"/>
<item id="img2" href="images/fig2.png" media-type="image/png"/>
</manifest>
<spine toc="ncx"><itemref idref="title"/><itemref idref="preface"/><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>
"""
    with open(os.path.join(root, "OEBPS", "content.opf"), "w", encoding="utf-8") as f:
        f.write(opf)
    return root
