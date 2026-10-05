"""A small synthetic book (PDF) and its zones, for the BITS / JATS tests.

build(path) writes the PDF; zone(zone_manager, pdf, kind) adds one zone per
line group exactly as a user would draw them (tags from the BITS or JATS
profile)."""
import fitz

PAGES = [
    # (text, size, bold, italic)
    [("Chaucer and the Poems of Ch", 20, True, False), ("A Study of Manuscripts", 14, False, True),
     ("James I. Wimsatt", 13, False, False), ("University of Toronto Press", 11, False, False),
     ("ISBN 978-0-8020-9154-3 (cloth)", 9, False, False),
     ("Copyright University of Toronto Press 2006", 9, False, False)],
    [("Preface", 16, True, False),
     ("This book began as a set of lectures given in Toronto.", 10, False, False),
     ("1", 9, False, False)],
    [("Chapter 1 Beginnings", 16, True, False),
     ("The manuscript tradition is older than usually assumed.", 10, False, False),
     ("Early Witnesses", 12, True, False),
     ("The first witness is a fragment held in Paris.", 10, False, False),
     ("1 See the catalogue of 1863 for details.", 8, False, False),
     ("2", 9, False, False)],
    [("Chapter 2 Middles", 16, True, False),
     ("A second argument concerns the scribes.", 10, False, False),
     ("Ask not what the poem can do for you.", 10, False, True),
     ("References", 12, True, False),
     ("Brewer, D. S. 1863. Chaucer Studies. London: Brewer.", 9, False, False),
     ("Smith, J. 1999. Manuscripts. Toronto: UTP.", 9, False, False),
     ("3", 9, False, False)],
    [("Index", 16, True, False),
     ("Abbey, 2, 3", 9, False, False),
     ("Church, 1", 9, False, False),
     ("4", 9, False, False)],
]

BITS_TAGS = [
    [("book-title", {}), ("book-subtitle", {}), ("contrib", {"@contrib-type": "author"}), ("publisher-name", {}),
     ("isbn", {}), ("copyright-statement", {})],
    [("h1", {"part_type": "preface"}), ("p", {}), ("pagenumber", {})],
    [("h1", {"part_type": "chapter"}), ("p", {}), ("h2", {}), ("p", {}), ("fn", {}), ("pagenumber", {})],
    [("h1", {"part_type": "chapter"}), ("p", {}), ("disp-quote", {}), ("h1", {"part_type": "bibliography"}),
     ("reference", {}), ("reference", {}), ("pagenumber", {})],
    [("h1", {"part_type": "index"}), ("index-entry", {}), ("index-entry", {}), ("pagenumber", {})],
]

JATS_TAGS = [
    [("article-title", {}), ("subtitle", {}), ("contrib", {"@contrib-type": "author"}), ("aff", {}),
     ("p", {}), ("copyright-statement", {})],
    [("h1", {}), ("p", {}), ("pagenumber", {})],
    [("h1", {}), ("p", {}), ("h2", {}), ("p", {}), ("fn", {}), ("pagenumber", {})],
    [("h1", {}), ("p", {}), ("disp-quote", {}), ("h1", {"part_type": "bibliography"}),
     ("reference", {}), ("reference", {}), ("pagenumber", {})],
    [("h1", {"part_type": "ack"}), ("p", {}), ("p", {}), ("pagenumber", {})],
]


def build(path):
    doc = fitz.open()
    for lines in PAGES:
        page = doc.new_page(width=420, height=600)
        y = 60
        for text, size, bold, italic in lines:
            font = "helv" if not bold and not italic else ("hebo" if bold else "heit")
            if text.isdigit():
                page.insert_text((200, 570), text, fontsize=size, fontname=font)
                continue
            page.insert_text((50, y), text, fontsize=size, fontname=font)
            y += size * 2.4
    doc.save(path)
    doc.close()
    return path


def zone(zm, pdf, kind="BITS"):
    """One zone per line, tagged like a user would."""
    from auto_zoning import layout_engine
    tags = BITS_TAGS if kind.upper() == "BITS" else JATS_TAGS
    for pno, (lines, page_tags) in enumerate(zip(PAGES, tags), 1):
        page_lines = layout_engine.extract_page_lines(pdf.get_page(pno), with_decorations=False)
        for (text, *_rest), (tag, attrs) in zip(lines, page_tags):
            li = next(li for li in page_lines if li.text.strip() == text)
            x0, y0, x1, y1 = li.bbox
            zm.add_zone(pno, tag, [x0 - 1, y0 - 1, x1 + 1, y1 + 1], attributes=dict(attrs), auto_parent=False)
    return zm
