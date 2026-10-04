"""EPUB 3 package with SEEDED defects for the auto-repair engine tests.

Every defect is switched on by name, so each test knows the ground truth:

  dup_id          ch1: a second element with id="sec1" (a later <p>); the nav
                  and a cross-reference point at the heading (the original)
  frag_case       ch1 link "#Sec2" - the real id is "sec2"
  frag_moved      ch1 link "ch1.xhtml#n5" - note n5 lives in ch2.xhtml (moved by a split)
  frag_renamed    ch2 link "#fig-1" - the real id is "fig1"
  frag_missing    ch2 link "#nowhere" - no plausible target at all (must stay for review)
  img_backslash   ch1 <img src="images\\fig1.png">
  img_case        ch2 <img src="Images/FIG2.png">
  img_missing     ch2 <img src="images/lost.png"> - the file does not exist anywhere
  undeclared_css  extra.css exists, linked from ch2, not in the manifest
  media_type      fig2.png declared as image/jpeg
  dup_spine       ch2 listed twice in the spine
  junk            .DS_Store / Thumbs.db / chapter1.xhtml.bak in the archive
  entity          ch1 uses &nbsp; and &mdash; (undefined in XHTML -> not well-formed)
  unclosed        ch2 "<p>An <i>unclosed italic.</p>"
  stale_split     nav link "ch1-split.xhtml#sec3" - that file no longer exists; sec3 is in ch1.xhtml
  css_brace       style.css missing its final closing brace
  compressed_mimetype  mimetype stored compressed and not first
"""
import base64
import io
import zipfile

PNG_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP4z8CAFTEMLQkAkL8/wVtRY6YAAAAASUVORK5CYII=")
PNG_BLUE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGNgYPiPFTEMLQkAkPU/wSlGlzsAAAAASUVORK5CYII=")

ALL_DEFECTS = ("dup_id", "frag_case", "frag_moved", "frag_renamed", "frag_missing", "img_backslash", "img_case",
               "img_missing", "undeclared_css", "media_type", "dup_spine", "junk", "entity", "unclosed",
               "stale_split", "css_brace", "compressed_mimetype")
SAFE_DEFECTS = tuple(d for d in ALL_DEFECTS if d not in ("frag_missing", "img_missing"))

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" '
        'xml:lang="en">\n<head><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/>'
        '{extra}</head>\n<body>\n')
TAIL = "</body>\n</html>\n"


def _pb(label):
    return f'<span epub:type="pagebreak" role="doc-pagebreak" id="page_{label}" aria-label="{label}"/>'


def chapter1(d):
    sp = "&nbsp;" if "entity" in d else "&#160;"
    dash = "&mdash;" if "entity" in d else "&#8212;"
    img = "images\\fig1.png" if "img_backslash" in d else "images/fig1.png"
    to_sec2 = "#Sec2" if "frag_case" in d else "#sec2"
    to_n5 = "ch1.xhtml#n5" if "frag_moved" in d else "ch2.xhtml#n5"
    dup = '<p id="sec1">A later paragraph that wrongly reuses the id of the heading.</p>\n' if "dup_id" in d else \
        '<p>A later paragraph that wrongly reuses the id of the heading.</p>\n'
    return (HEAD.format(title="Chapter One", extra="") +
            '<section epub:type="chapter">\n'
            f'{_pb("1")}<h1 id="sec1">Chapter One</h1>\n'
            f'<p id="p1">The opening{sp}paragraph{dash}with an entity or two. See <a href="{to_sec2}">section '
            f'two</a> and <a href="{to_n5}">note five</a>.</p>\n'
            f'<figure id="f1"><img src="{img}" alt="A red square"/><figcaption>Figure 1. Red.</figcaption></figure>\n'
            f'{dup}'
            f'<h2 id="sec2">Section Two</h2>\n{_pb("2")}'
            '<p>Text on the second page of chapter one, with <i>inline</i> and <b>bold</b> formatting.</p>\n'
            '<h2 id="sec3">Section Three</h2>\n<p>The third section.</p>\n'
            '</section>\n' + TAIL)


def chapter2(d):
    img2 = "Images/FIG2.png" if "img_case" in d else "images/fig2.png"
    to_fig = "#fig-1" if "frag_renamed" in d else "#fig1"
    unclosed = "<p>An <i>unclosed italic.</p>\n" if "unclosed" in d else "<p>An <i>closed italic</i>.</p>\n"
    lost = '<p><img src="images/lost.png" alt="A lost picture"/></p>\n' if "img_missing" in d else ""
    missing = '<p>See <a href="#nowhere">nowhere</a>.</p>\n' if "frag_missing" in d else ""
    extra = '<link rel="stylesheet" type="text/css" href="extra.css"/>' if "undeclared_css" in d else ""
    return (HEAD.format(title="Chapter Two", extra=extra) +
            '<section epub:type="chapter">\n'
            f'{_pb("3")}<h1 id="ch2">Chapter Two</h1>\n'
            f'<p>The second chapter refers to <a href="{to_fig}">figure one</a>.</p>\n'
            f'<figure id="fig1"><img src="{img2}" alt="A blue square"/></figure>\n'
            f'{unclosed}{lost}{missing}'
            '<aside epub:type="footnote" id="n5"><p>5. The fifth note.</p></aside>\n'
            '</section>\n' + TAIL)


def nav(d):
    stale = "ch1-split.xhtml#sec3" if "stale_split" in d else "ch1.xhtml#sec3"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
            '<head><title>Contents</title></head>\n<body>\n<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n'
            '<li><a href="ch1.xhtml#sec1">Chapter One</a><ol>'
            '<li><a href="ch1.xhtml#sec2">Section Two</a></li>'
            f'<li><a href="{stale}">Section Three</a></li></ol></li>\n'
            '<li><a href="ch2.xhtml">Chapter Two</a></li>\n</ol></nav>\n'
            '<nav epub:type="page-list" hidden=""><ol>'
            '<li><a href="ch1.xhtml#page_1">1</a></li><li><a href="ch1.xhtml#page_2">2</a></li>'
            '<li><a href="ch2.xhtml#page_3">3</a></li></ol></nav>\n</body>\n</html>\n')


def opf(d):
    png2_type = "image/jpeg" if "media_type" in d else "image/png"
    spine_extra = '<itemref idref="ch2"/>' if "dup_spine" in d else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="uid">urn:uuid:7d3b3c8e-2f7a-4f43-9c4a-0f8e5b1a2c3d</dc:identifier>'
            '<dc:title>Broken Book</dc:title><dc:language>en</dc:language>'
            '<dc:creator>Test Author</dc:creator>'
            '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>\n<manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="css" href="style.css" media-type="text/css"/>'
            '<item id="img1" href="images/fig1.png" media-type="image/png"/>'
            f'<item id="img2" href="images/fig2.png" media-type="{png2_type}"/>'
            '</manifest>\n<spine><itemref idref="ch1"/><itemref idref="ch2"/>' + spine_extra +
            '</spine>\n</package>\n')


def build(path, defects=ALL_DEFECTS):
    d = set(defects)
    css = "body { margin: 0; }\nh1 { font-size: 1.5em; }\np { text-indent: 1em;" + ("" if "css_brace" in d else " }") + "\n"
    files = [
        ("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                                   '<rootfile full-path="OEBPS/content.opf" '
                                   'media-type="application/oebps-package+xml"/></rootfiles></container>'),
        ("OEBPS/content.opf", opf(d)),
        ("OEBPS/nav.xhtml", nav(d)),
        ("OEBPS/ch1.xhtml", chapter1(d)),
        ("OEBPS/ch2.xhtml", chapter2(d)),
        ("OEBPS/style.css", css),
        ("OEBPS/images/fig1.png", PNG_RED),
        ("OEBPS/images/fig2.png", PNG_BLUE),
    ]
    if "undeclared_css" in d:
        files.append(("OEBPS/extra.css", "aside { font-size: 0.9em; }\n"))
    if "junk" in d:
        files += [("OEBPS/.DS_Store", b"\x00\x00\x00\x01Bud1"), ("Thumbs.db", b"\xd0\xcf\x11\xe0"),
                  ("OEBPS/ch1.xhtml.bak", "old copy")]
    with zipfile.ZipFile(path, "w") as z:
        if "compressed_mimetype" in d:
            z.writestr("META-INF/container.xml", files[0][1], compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_DEFLATED)
            files = files[1:]
        else:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files:
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return path
