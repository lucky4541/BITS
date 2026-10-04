"""Synthetic 5-page book for Auto Zone section / continuation tests.

  p1  chapter heading, paragraph A, paragraph B that runs off the page
      mid-sentence (last line full measure)
  p2  continuation of B (lowercase, no first-line indent), paragraph C
      ending with a full stop at the foot of the page
  p3  paragraph D (new, indented), "Notes" heading, notes 1 and 2 -
      note 2 runs over the page
  p4  continuation of note 2, note 3, "References" heading, author-date
      entries with a hanging indent
  p5  new chapter heading (ends the references section), paragraph E
Expected values come from this spec, never from the engine.
"""
import fitz

PAGE_W, PAGE_H = 432.0, 648.0
LEFT, RIGHT = 54.0, 378.0
TOP, BOTTOM = 72.0, 590.0
BODY, LEAD, INDENT = 10.5, 14.0, 18.0
FONT, BOLD = "tiro", "tibo"

A = ("Every reader of the poems notices first the music of the lines, the way the sound of one word seems to "
     "call out the next, and only later begins to ask what kind of meaning such music could carry at all.")
B_ALL = ("The matter that the contemplation of the shape aims to settle on is the inscape, which the poet "
         "identifies as the very soul of art, the central term of his personal metaphysics, and in this chapter "
         "I call on the diaries, the verse and the theoretical writings to show how far the idea reaches into "
         "the repeating figures of speech sound that fill every stanza he wrote in those years.")
C = ("A new paragraph follows on the second page and comes to a proper end with a full stop, so nothing on "
     "the next page may be joined to it whatever the layout of that page looks like.")
D = ("The next page opens with a fresh paragraph that carries its own first-line indent and a capital letter, "
     "which marks it as new.")
NOTES = ["1. Brown, Journals and Papers, p. 231, where the glacier passage is discussed at length.",
         "2. The letters to Bridges return to this point several times, most fully in the long letter of "
         "February 1879, where the argument about sound and sense is set out with unusual care and a number "
         "of examples drawn from the early sonnets and from the unfinished drafts that survive.",
         "3. See also the lecture notes on rhythm."]
REFS = ["Brown, Daniel. Hopkins's Idealism: Philosophy, Physics, Poetry. Oxford: Clarendon Press, 1997.",
        "Mackey, Louis. Faith Order Understanding: Natural Theology in the Augustinian Tradition. Toronto: "
        "Pontifical Institute, 1999.",
        "Ward, Bernadette Waterman. World as Word: Philosophical Theology in Gerard Manley Hopkins. "
        "Washington: Catholic University of America Press, 2002."]
E = "The second chapter begins here with ordinary prose that belongs to no references list at all."


def _wrap(text, width, size=BODY, first_indent=0.0, font=FONT):
    words, lines, cur, avail = text.split(), [], "", width - first_indent
    for w in words:
        cand = (cur + " " + w).strip()
        if fitz.get_text_length(cand, fontname=font, fontsize=size) <= avail:
            cur = cand
        else:
            lines.append(cur)
            cur, avail = w, width
    if cur:
        lines.append(cur)
    return lines


class _Writer:
    def __init__(self, doc):
        self.doc = doc
        self.page = None
        self.y = TOP
        self.n = 0

    def new_page(self):
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.n += 1
        self.page.insert_text((RIGHT - 10, PAGE_H - 40), str(self.n), fontname=FONT, fontsize=9)
        self.y = TOP

    def line(self, x, text, size=BODY, font=FONT):
        self.page.insert_text((x, self.y), text, fontname=font, fontsize=size)
        self.y += LEAD if size <= BODY + 1 else size * 1.6

    def heading(self, text, size=16):
        self.y += 10
        self.line(LEFT, text, size=size, font=BOLD)
        self.y += 6

    def para_lines(self, lines, indent=INDENT, hanging=0.0):
        for k, ln in enumerate(lines):
            x = LEFT + (indent if k == 0 else hanging)
            self.line(x, ln)


def build(path):
    doc = fitz.open()
    w = _Writer(doc)
    width = RIGHT - LEFT
    # page 1
    w.new_page()
    w.heading("Chapter One: Inscape and Poetic Meaning", 15)
    w.para_lines(_wrap(A, width, first_indent=INDENT))
    b_lines = _wrap(B_ALL, width, first_indent=INDENT)
    split = 3
    w.para_lines(b_lines[:split])                 # runs off the page, last line full measure
    # page 2: continuation (no indent) + C
    w.new_page()
    w.para_lines(b_lines[split:], indent=0.0)
    w.para_lines(_wrap(C, width, first_indent=INDENT))
    # page 3: D, Notes heading, notes 1-2 (2 runs over)
    w.new_page()
    w.para_lines(_wrap(D, width, first_indent=INDENT))
    w.heading("Notes", 14)
    hang = 12.0
    w.para_lines(_wrap(NOTES[0], width, first_indent=0.0), indent=0.0, hanging=hang)
    w.y += 4
    n2 = _wrap(NOTES[1], width - hang)
    w.para_lines(n2[:2], indent=0.0, hanging=hang)
    # page 4: rest of note 2, note 3, References
    w.new_page()
    w.para_lines(n2[2:], indent=hang, hanging=hang)
    w.y += 4
    w.para_lines(_wrap(NOTES[2], width, first_indent=0.0), indent=0.0, hanging=hang)
    w.heading("References", 14)
    for ref in REFS:
        w.para_lines(_wrap(ref, width - hang), indent=0.0, hanging=hang)
        w.y += 4
    # page 5: new chapter ends the section
    w.new_page()
    w.heading("Chapter Two: The Sound of Sense", 15)
    w.para_lines(_wrap(E, width, first_indent=INDENT))
    doc.save(path)
    doc.close()
    return path


# ------------------------------------------------- book-end notes by chapter
GROUPS = [
    ("Introduction", ["Arguably the leading journal in the field regularly publishes articles whose perspectives "
                      "derive from all these disciplines and others, and any given issue is likely to feature "
                      "essays on very diverse topics.",
                      "Erica Fudge, Perceiving Animals (Urbana: University of Illinois Press, 2002).",
                      "Ibid., 31."]),
    ("1 Jewels of Women", ["Arcangela Tarabotti, La semplicita ingannata (1654).",
                           "Freud's interest in the painter was expressed by him as early as "
                           "1898 in a letter to a friend, in which he states that perhaps the most famous "
                           "left-handed individual was the painter himself, who is not known to have had "
                           "any love affairs.",
                           "See Richard A. Goldthwaite, Wealth and the Demand for Art in Italy, 1300 to",
                           "Ariosto, Orlando furioso (43.78.5-8).",
                           "Charles Ricketts, Titian (London: Methuen, 1910), 92.",
                           "Ludovico Ariosto, Satire e lettere, ed. Cesare Segre (Turin: Einaudi, 1976).",
                           "Alberti, Il cavallo vivo, critical edition and translation (Naples, 1981).",
                           "Juliana Schiesari, The Domestication of Woman, Stanford Italian Review 11:2 (1991).",
                           "Edward Topsell, The Historie of Foure-Footed Beastes (London, 1607), 1: 135.",
                           "On these collections see Paula Findlen, Possessing Nature (Berkeley: University of "
                           "California Press, 1994)."]),
    ("2 Portrait of the Poet as a Dog", ["Petrarch, Letters on Familiar Matters, trans. Aldo Bernardo.",
                                         "Ibid., 127."]),
]


def build_grouped(path, bottom=330.0):
    """Book-end Notes grouped by chapter, CUP style: 'Notes' title, bold
    group headings, numbering restarting at 1 under each, right-aligned
    note numbers with a hanging text column, running heads 'N  Notes to
    pages ...' that differ on every page, a note whose turn-over line
    starts with a year, notes running over pages, a blank last page.
    `bottom` keeps the text area short so page breaks fall inside notes."""
    doc = fitz.open()
    text_x, num_right = 50.0, 46.0
    width = RIGHT - text_x
    state = {"page": doc.new_page(width=PAGE_W, height=PAGE_H), "y": 150.0, "folio": 127}
    state["page"].insert_text((LEFT - 18, 90), "Notes", fontname=FONT, fontsize=18)

    def new_page():
        state["page"] = doc.new_page(width=PAGE_W, height=PAGE_H)
        state["folio"] += 1
        f = state["folio"]
        state["page"].insert_text((LEFT, 45), f"{f}  Notes to pages {f - 120}-{f - 110}", fontname=FONT, fontsize=9)
        state["y"] = 75.0

    def put(x, text, font=FONT):
        if state["y"] > bottom:
            new_page()
        state["page"].insert_text((x, state["y"]), text, fontname=font, fontsize=9)
        state["y"] += 12.0

    for title, notes in GROUPS:
        state["y"] += 10
        put(LEFT - 18, title, font=BOLD)
        state["y"] += 8
        for n, note in enumerate(notes, 1):
            head, sep, rest = note.partition(" 1898 ")
            lines = [head] + _wrap("1898 " + rest, width, size=9) if sep else _wrap(note, width, size=9)
            num = str(n)
            if state["y"] > bottom:
                new_page()
            state["page"].insert_text((num_right - fitz.get_text_length(num, fontname=FONT, fontsize=9),
                                       state["y"]), num, fontname=FONT, fontsize=9)
            for k, ln in enumerate(lines):
                if sep and k == 1:
                    new_page()          # the page break falls inside this note: "1898 in a letter" turns over
                put(text_x, ln)
    blank = doc.new_page(width=PAGE_W, height=PAGE_H)
    blank.insert_text((PAGE_W / 2 - 80, PAGE_H / 2), "This page intentionally left blank", fontname="tiit",
                      fontsize=12)
    doc.save(path)
    doc.close()
    return path
