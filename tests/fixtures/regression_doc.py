"""Builds the synthetic regression book used by tests/test_auto_zone_regression.py.

The document is generated from a declarative spec; the GROUND TRUTH (which
characters are underlined, which blocks exist, their order and roles) is
taken from that spec, never from the engine under test. Underline segments
are drawn from the PDF's own rendered glyph boxes (second pass), so the
test exercises exactly what a real typeset PDF contains: text plus
independent vector rules.

Covered cases: full-word underline, partial-word underline, single-character
underline, several separate ranges in one line, a multi-word phrase
(underline runs through the spaces), underline interrupted at a space,
punctuation underline, accented characters, underline combined with
bold/italic, strike-through, an annotation underline, verse lines,
paragraph boundaries (first-line indent), headings, running heads, page
numbers, blockquote, numbered list, footnotes below a rule, a vector figure
with its caption and a two-column page."""
import fitz

PAGE_W, PAGE_H = 432.0, 648.0
LEFT, RIGHT = 54.0, 378.0
BODY = 10.0
LEAD = 13.0
FONT = "tiro"          # Times-Roman (base-14) - has Latin-1 accents
FONT_B = "tibo"
FONT_I = "tiit"

# One underline spec per verse/paragraph line: (line_key, [(start, end), ...]) half-open char ranges.
VERSE = [
    "Whose wóods these are I think I know.",
    "His house is in the village, though;",
    "He will not see me stopping here",
    "To watch his woods fill up with snow.",
]
# ranges are character offsets into each verse line (half-open)
VERSE_UNDERLINES = {
    0: [(6, 11), (28, 29)],          # full word "wóods" (accented) + single char "k" inside "think"
    1: [(4, 7), (26, 28)],           # partial word "hou" + "e," (punctuation included)
    2: [(3, 15)],                    # phrase "will not see" - one rule running through the spaces
    3: [(0, 2), (3, 8), (36, 37)],   # "To" and "watch" as two rules (space NOT underlined) + final "."
}
STRIKE = {2: [(19, 27)]}             # "stopping" struck through

PARA_1 = ("The traveller paused at the edge of the wood, considering the silence that followed the "
          "first snowfall of the year. Nothing moved in the hollow below, and the road was empty in "
          "both directions as far as he could see.")
PARA_2 = ("A second paragraph begins here with its own first-line indent, so the engine must keep it "
          "apart from the paragraph above even though the line spacing is identical.")
QUOTE = ("Quoted matter is set narrower than the body text, indented on both sides, and the engine "
         "has to recognise it from its geometry rather than from any keyword in the text itself.")
LIST_ITEMS = ["1. The first numbered point of the argument.",
              "2. The second point, which continues for long enough that it wraps onto a second line of text.",
              "3. The third and final point."]
FOOTNOTES = ["1 A footnote set in smaller type below the separator rule.",
             "2 A second note, also small, also at the foot of the page."]


def _wrap(text, width, font=FONT, size=BODY, first_indent=0.0):
    words = text.split()
    lines, cur = [], ""
    avail = width - first_indent
    for w in words:
        cand = (cur + " " + w).strip()
        if fitz.get_text_length(cand, fontname=font, fontsize=size) <= avail:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            avail = width
    if cur:
        lines.append(cur)
    return lines


def build(path):
    doc = fitz.open()
    truth = {"pages": {}, "underline_lines": {}, "strike_lines": {}}

    # ---------------------------------------------------------------- page 1
    p = doc.new_page(width=PAGE_W, height=PAGE_H)
    y = 70.0
    blocks = []

    def text_line(page, x, y, s, font=FONT, size=BODY):
        page.insert_text((x, y), s, fontname=font, fontsize=size)

    text_line(p, LEFT, 30, "THE SILENT WOOD", size=8)                       # running head
    text_line(p, PAGE_W / 2 - 5, PAGE_H - 30, "1", size=9)                   # page number
    blocks.append(("page_number", "1"))
    text_line(p, LEFT, y, "Chapter 1", font=FONT_B, size=12)
    blocks.append(("label", "Chapter 1"))
    y += 26
    text_line(p, LEFT, y, "Stopping by Woods", font=FONT_B, size=18)
    blocks.append(("heading", "Stopping by Woods"))
    y += 30
    for i, ln in enumerate(_wrap(PARA_1, RIGHT - LEFT, first_indent=0)):
        text_line(p, LEFT, y, ln)
        y += LEAD
    blocks.append(("paragraph", PARA_1[:20]))
    for i, ln in enumerate(_wrap(PARA_2, RIGHT - LEFT, first_indent=14)):
        text_line(p, LEFT + (14 if i == 0 else 0), y, ln)
        y += LEAD
    blocks.append(("paragraph", PARA_2[:20]))
    y += 10
    verse_y = []
    for ln in VERSE:
        text_line(p, LEFT + 40, y, ln)
        verse_y.append(y)
        y += LEAD
    for v in VERSE:
        blocks.append(("verse_line", v[:15]))
    y += 10
    for i, ln in enumerate(_wrap(QUOTE, RIGHT - LEFT - 60, size=9)):
        text_line(p, LEFT + 30, y, ln, size=9)
        y += 11.5
    blocks.append(("blockquote", QUOTE[:20]))
    y += 10
    text_line(p, LEFT, y, "Notes on Method", font=FONT_B, size=13)
    blocks.append(("heading", "Notes on Method"))
    y += 20
    for it in LIST_ITEMS:
        lines = _wrap(it, RIGHT - LEFT - 14)
        for k, ln in enumerate(lines):
            text_line(p, LEFT + (0 if k == 0 else 14), y, ln)
            y += LEAD
        blocks.append(("list_item", it[:12]))
    # footnote rule + notes
    rule_y = PAGE_H - 110
    p.draw_line((LEFT, rule_y), (LEFT + 90, rule_y), width=0.5)
    fy = rule_y + 14
    for fn in FOOTNOTES:
        text_line(p, LEFT, fy, fn, size=8)
        fy += 10
        blocks.append(("footnote", fn[:10]))
    truth["pages"][1] = blocks

    # ---------------------------------------------------------------- page 2
    p2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    text_line(p2, LEFT, 30, "THE SILENT WOOD", size=8)
    text_line(p2, PAGE_W / 2 - 5, PAGE_H - 30, "2", size=9)
    b2 = [("page_number", "2")]
    y = 70
    # vector figure
    shape = p2.new_shape()
    shape.draw_circle((PAGE_W / 2, y + 60), 45)
    shape.draw_bezier((LEFT + 60, y + 100), (LEFT + 120, y + 10), (LEFT + 200, y + 110), (RIGHT - 60, y + 20))
    shape.finish(color=(0, 0, 0), fill=(0.85, 0.85, 0.85), width=1)
    shape.commit()
    b2.append(("figure", ""))
    y += 140
    text_line(p2, LEFT + 30, y, "Figure 1.1 The wood in winter, drawn from memory.", font=FONT_I, size=8.5)
    b2.append(("caption", "Figure 1.1"))
    y += 30
    col_w = (RIGHT - LEFT - 18) / 2
    left_text = ("The left column carries the first half of a two-column passage. Its lines must be read "
                 "completely before the right column begins, whatever their vertical positions.")
    right_text = ("The right column continues the passage. A reader that sorted every line by its vertical "
                  "position alone would interleave the two columns and scramble the text.")
    yy = y
    for ln in _wrap(left_text, col_w):
        text_line(p2, LEFT, yy, ln)
        yy += LEAD
    yy2 = y
    for ln in _wrap(right_text, col_w):
        text_line(p2, LEFT + col_w + 18, yy2, ln)
        yy2 += LEAD
    b2.append(("paragraph", left_text[:20]))
    b2.append(("paragraph", right_text[:20]))
    y = max(yy, yy2) + 20
    x = LEFT
    for piece, font in (("A ", FONT), ("bold and underlined", FONT_B), (" phrase sits in ", FONT),
                        ("this", FONT_I), (" closing line.", FONT)):
        text_line(p2, x, y, piece, font=font, size=BODY)
        x += fitz.get_text_length(piece, fontname=font, fontsize=BODY)
    b2.append(("paragraph", "A bold and"))
    combo_y = y
    truth["pages"][2] = b2

    doc.save(path)
    doc.close()

    # ------------------------------------------- second pass: decorations
    doc = fitz.open(path)
    page = doc[0]
    raw = page.get_text("rawdict")

    def line_chars(baseline):
        out = []
        for b in raw["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    for c in s["chars"]:
                        if abs(c["origin"][1] - baseline) < 0.5:
                            out.append(c)
        out.sort(key=lambda c: c["origin"][0])
        return out

    for idx, base in enumerate(verse_y):
        chars = line_chars(base)
        text = "".join(c["c"] for c in chars)
        assert text == VERSE[idx], (text, VERSE[idx])
        for (a, b) in VERSE_UNDERLINES.get(idx, []):
            x0 = chars[a]["bbox"][0]
            x1 = chars[b - 1]["bbox"][2]
            page.draw_line((x0, base + 1.6), (x1, base + 1.6), width=0.55)
        for (a, b) in STRIKE.get(idx, []):
            x0 = chars[a]["bbox"][0]
            x1 = chars[b - 1]["bbox"][2]
            page.draw_line((x0, base - 2.6), (x1, base - 2.6), width=0.55)
        truth["underline_lines"][VERSE[idx]] = VERSE_UNDERLINES.get(idx, [])
        truth["strike_lines"][VERSE[idx]] = STRIKE.get(idx, [])
    # page 2: "bold and underlined" -> underline via annotation over "underlined"
    page2 = doc[1]
    raw = page2.get_text("rawdict")
    chars = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                for c in s["chars"]:
                    if abs(c["origin"][1] - combo_y) < 0.5:
                        chars.append(c)
    chars.sort(key=lambda c: c["origin"][0])
    text = "".join(c["c"] for c in chars)
    start = text.index("underlined")
    end = start + len("underlined")
    r = fitz.Rect(chars[start]["bbox"][0], chars[start]["bbox"][1], chars[end - 1]["bbox"][2], chars[end - 1]["bbox"][3])
    page2.add_underline_annot(r)
    truth["underline_lines"][text] = [(start, end)]
    truth["strike_lines"][text] = []
    doc.saveIncr()
    doc.close()
    return truth
