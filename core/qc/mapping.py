"""THE unified PDF <-> XHTML document mapping and difference model.

    PDF page -> PDF words / paragraphs / images / printed page number
        <-> (alignment) <->
    XHTML split -> blocks / words / page markers / images / ids / links
        -> differences (with colour state, confidence, suggested correction)
        -> page / split / document scores

Computed once here and used by every consumer (viewers, difference panel,
reports, auto-correction, split operations) - nothing re-derives it.

Text alignment is ANCHORED: words whose 5-gram is unique in both documents
form monotone anchors (longest increasing subsequence); only the stretches
between anchors are aligned with difflib, so large books align in near
linear time and one local defect never shifts the rest of the book.
Differences are classified as missing / extra / modified (word and
character level) / reordered (moved) / duplicate, plus merged / split
paragraphs, page-marker, image, caption, link, package and split problems.
"""
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from core.qc import textnorm
from core.qc.pdf_model import visual_similarity

# colour states (UI maps each to a configurable colour + a text label)
MATCH, MISSING, EXTRA, MODIFIED, MOVED, DUPLICATE, UNCERTAIN = (
    "MATCH", "MISSING", "EXTRA", "MODIFIED", "MOVED", "DUPLICATE", "UNCERTAIN")
STATE_LABELS = {MATCH: "Matched", MISSING: "Missing", EXTRA: "Extra", MODIFIED: "Modified", MOVED: "Moved",
                DUPLICATE: "Duplicate", UNCERTAIN: "Uncertain"}

# difference kinds -> (category, state, default severity)
KINDS = {
    "MISSING_TEXT": ("text", MISSING, "HIGH"),
    "EXTRA_TEXT": ("text", EXTRA, "MEDIUM"),
    "MODIFIED_TEXT": ("text", MODIFIED, "MEDIUM"),
    "REORDERED_TEXT": ("text", MOVED, "MEDIUM"),
    "DUPLICATE_TEXT": ("text", DUPLICATE, "MEDIUM"),
    "MERGED_PARAGRAPHS": ("structure", MODIFIED, "LOW"),
    "UNEXPECTED_PARAGRAPH_SPLIT": ("structure", MODIFIED, "LOW"),
    "MISSING_PAGE_MARKER": ("page", MISSING, "HIGH"),
    "WRONG_PAGE_MARKER": ("page", MODIFIED, "HIGH"),
    "MISPLACED_PAGE_MARKER": ("page", MODIFIED, "MEDIUM"),
    "DUPLICATE_PAGE_MARKER": ("page", DUPLICATE, "MEDIUM"),
    "EXTRA_PAGE_MARKER": ("page", EXTRA, "LOW"),
    "MISSING_IMAGE": ("image", MISSING, "HIGH"),
    "WRONG_IMAGE": ("image", MISSING, "HIGH"),
    "MODIFIED_IMAGE": ("image", MODIFIED, "MEDIUM"),
    "MOVED_IMAGE": ("image", MOVED, "MEDIUM"),
    "EXTRA_IMAGE": ("image", UNCERTAIN, "LOW"),
    "BROKEN_IMAGE_REFERENCE": ("image", MISSING, "HIGH"),
    "MISSING_CAPTION": ("caption", MISSING, "MEDIUM"),
    "WRONG_CAPTION": ("caption", MODIFIED, "MEDIUM"),
    "CAPTION_ON_WRONG_IMAGE": ("caption", MOVED, "MEDIUM"),
    "DUPLICATE_CAPTION": ("caption", DUPLICATE, "LOW"),
    "BROKEN_LINK": ("link", MISSING, "HIGH"),
    "PACKAGE_PROBLEM": ("package", MISSING, "HIGH"),
    "SPLIT_ORDER": ("split", MOVED, "MEDIUM"),
    "UNMAPPED_SPLIT": ("split", UNCERTAIN, "MEDIUM"),
    "LOW_CONFIDENCE_MAPPING": ("split", UNCERTAIN, "LOW"),
}
FILTERS = [
    ("Missing Text", {"MISSING_TEXT"}), ("Extra Text", {"EXTRA_TEXT"}), ("Modified Text", {"MODIFIED_TEXT"}),
    ("Reordered Text", {"REORDERED_TEXT"}), ("Duplicate Text", {"DUPLICATE_TEXT"}),
    ("Paragraph Structure", {"MERGED_PARAGRAPHS", "UNEXPECTED_PARAGRAPH_SPLIT"}),
    ("Missing Images", {"MISSING_IMAGE", "BROKEN_IMAGE_REFERENCE"}),
    ("Wrong Images", {"WRONG_IMAGE", "MODIFIED_IMAGE", "MOVED_IMAGE", "EXTRA_IMAGE"}),
    ("Captions", {"MISSING_CAPTION", "WRONG_CAPTION", "CAPTION_ON_WRONG_IMAGE", "DUPLICATE_CAPTION"}),
    ("Missing Page Numbers", {"MISSING_PAGE_MARKER"}),
    ("Wrong Page Numbers", {"WRONG_PAGE_MARKER", "MISPLACED_PAGE_MARKER", "DUPLICATE_PAGE_MARKER",
                            "EXTRA_PAGE_MARKER"}),
    ("Split Problems", {"SPLIT_ORDER", "UNMAPPED_SPLIT"}),
    ("Broken Links", {"BROKEN_LINK", "PACKAGE_PROBLEM"}),
    ("Low Confidence", {"LOW_CONFIDENCE_MAPPING"}),
]
ANCHOR_N = 5
MARKER_TOLERANCE_WORDS = 3
IMAGE_SLOT_WINDOW = 60
IMAGE_MATCH = 0.86
IMAGE_SIMILAR = 0.70
IMAGE_POS_TOLERANCE = 8      # words: an image further than this from its PDF reading position has moved


@dataclass
class Correction:
    action: str                 # insert_page_marker | relabel_page_marker | move_page_marker | remove_page_marker |
                                # remap_image | move_image | fix_link | package_fix | replace_text | insert_text |
                                # remove_text
    params: dict
    confidence: float
    auto_safe: bool = False
    description: str = ""


@dataclass
class Difference:
    kind: str
    pdf_text: str = ""
    xhtml_text: str = ""
    pdf_page: int = None
    pdf_bboxes: list = field(default_factory=list)
    split: str = None
    x_start: int = None          # global XHTML word index range
    x_end: int = None
    p_start: int = None          # global PDF word index range
    p_end: int = None
    block_xpath: str = ""
    sourceline: int = 0
    confidence: float = 0.9
    message: str = ""
    segments: list = field(default_factory=list)    # [(op, pdf_chunk, xhtml_chunk)] character level
    correction: Correction = None
    image: dict = None
    severity: str = None
    status: str = "open"         # open | accepted | rejected | ignored | edited | fixed
    id: str = ""

    @property
    def category(self):
        return KINDS[self.kind][0]

    @property
    def state(self):
        return KINDS[self.kind][1]

    @property
    def label(self):
        return self.kind.replace("_", " ").title()

    def signature(self) -> str:
        """Stable identity across re-analysis (for remembered decisions)."""
        raw = "|".join(str(x) for x in (self.kind, self.pdf_page, self.split, textnorm.clean(self.pdf_text)[:80],
                                         textnorm.clean(self.xhtml_text)[:80],
                                         (self.image or {}).get("pdf_uid"), (self.image or {}).get("xhtml_src")))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class PageMap:
    page: int
    printed: str = None
    printed_confidence: float = 0.0
    p_start: int = 0
    p_end: int = 0
    x_start: int = None
    x_end: int = None
    splits: list = field(default_factory=list)
    matched: int = 0
    confidence: float = 0.0
    marker: object = None        # XMarker matched to this page
    marker_state: str = None
    images: list = field(default_factory=list)   # [(pdf_uid, state)]
    scores: dict = field(default_factory=dict)


@dataclass
class SplitMap:
    split: str
    pages: list = field(default_factory=list)
    x_start: int = 0
    x_end: int = 0
    matched: int = 0
    confidence: float = 0.0
    scores: dict = field(default_factory=dict)
    differences: int = 0


# ------------------------------------------------------------- alignment
def _lis(pairs):
    """Longest strictly increasing subsequence on the second element
    (pairs already sorted by the first)."""
    if not pairs:
        return []
    tails, tails_idx, prev = [], [], [-1] * len(pairs)
    for i, (_a, b) in enumerate(pairs):
        k = _bisect_left(tails, b)
        if k == len(tails):
            tails.append(b)
            tails_idx.append(i)
        else:
            tails[k] = b
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k > 0 else -1
    out, i = [], tails_idx[-1]
    while i != -1:
        out.append(pairs[i])
        i = prev[i]
    return out[::-1]


def _bisect_left(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def anchored_opcodes(P: list, X: list, n: int = ANCHOR_N):
    """difflib-style opcodes over two key sequences, anchored on unique
    n-grams (monotone via LIS) so the result scales to whole books."""
    def grams(seq):
        c = Counter()
        pos = {}
        for i in range(len(seq) - n + 1):
            g = tuple(seq[i:i + n])
            if not all(g):
                continue
            c[g] += 1
            pos[g] = i
        return {g: pos[g] for g, k in c.items() if k == 1}
    gp, gx = grams(P), grams(X)
    pairs = sorted((gp[g], gx[g]) for g in gp.keys() & gx.keys())
    anchors = _lis(pairs)
    # merge overlapping anchors into equal runs
    runs = []
    for a, b in anchors:
        if runs and a <= runs[-1][1] and b <= runs[-1][3] and a - runs[-1][0] == b - runs[-1][2]:
            runs[-1][1] = max(runs[-1][1], a + n)
            runs[-1][3] = max(runs[-1][3], b + n)
        elif runs and (a < runs[-1][1] or b < runs[-1][3]):
            continue
        else:
            runs.append([a, a + n, b, b + n])
    ops = []
    pi = xi = 0
    for a0, a1, b0, b1 in runs + [[len(P), len(P), len(X), len(X)]]:
        if a0 > pi or b0 > xi:
            sm = SequenceMatcher(None, P[pi:a0], X[xi:b0], autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                ops.append([tag, pi + i1, pi + i2, xi + j1, xi + j2])
        if a1 > a0:
            ops.append(["equal", a0, a1, b0, b1])
        pi, xi = a1, b1
    merged = []
    for op in ops:
        if merged and merged[-1][0] == op[0] and merged[-1][2] == op[1] and merged[-1][4] == op[3]:
            merged[-1][2], merged[-1][4] = op[2], op[4]
        elif op[1] != op[2] or op[3] != op[4]:
            merged.append(op)
    return merged


def char_segments(a: str, b: str):
    """[(op, a_chunk, b_chunk)] - only the differing characters are marked."""
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return [(tag, a[i1:i2], b[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes()]


# =============================================================== engine
class DocumentMapping:
    def __init__(self, pdf, xhtml, mgr, decisions: dict = None):
        self.pdf = pdf
        self.x = xhtml
        self.mgr = mgr
        self.decisions = decisions or {}
        self.p2x = []
        self.x2p = []
        self.p_state = []          # per PDF word colour state
        self.x_state = []          # per XHTML word colour state
        self.differences = []
        self.pages = []
        self.split_maps = {}
        self.scores = {}
        self.image_pairs = []      # dicts describing PDF/XHTML image relations
        self.marker_state = {}     # xhtml marker index -> state

    # ------------------------------------------------------------- run
    def compute(self, progress=None):
        P = [w.key for w in self.pdf.words]
        X = [w.key for w in self.x.words]
        self.p2x = [-1] * len(P)
        self.x2p = [-1] * len(X)
        self.p_state = [MISSING] * len(P)
        self.x_state = [EXTRA] * len(X)
        self.differences = []
        if progress:
            progress("Aligning text", 0, 1)
        ops = anchored_opcodes(P, X)
        self._text_differences(ops)
        if progress:
            progress("Mapping pages", 0, 1)
        self._page_maps()
        self._paragraph_structure()
        if progress:
            progress("Comparing page markers", 0, 1)
        self._page_markers()
        if progress:
            progress("Comparing images", 0, 1)
        self._images()
        self._links_and_package()
        self._splits()
        self._apply_decisions()
        self._scores()
        for d in self.differences:
            if not d.severity:
                d.severity = KINDS[d.kind][2]
            if not d.id:
                d.id = d.signature()
        self.differences.sort(key=lambda d: (d.pdf_page if d.pdf_page is not None else 10 ** 6,
                                             d.x_start if d.x_start is not None else 10 ** 9))
        return self

    # --------------------------------------------------------- helpers
    def _add(self, kind, **kw):
        d = Difference(kind=kind, **kw)
        self.differences.append(d)
        return d

    def _x_split(self, j):
        return self.x.split_of_word(j)

    def _block(self, j):
        return self.x.block_of_word(j)

    def _p_text(self, i1, i2):
        return " ".join(w.text for w in self.pdf.words[i1:i2])

    def _x_text(self, j1, j2):
        return " ".join(w.text for w in self.x.words[j1:j2])

    def _bboxes(self, i1, i2):
        out = []
        for w in self.pdf.words[i1:i2]:
            out.append(w.bbox)
            out.extend(w.extra_bboxes)
        return out

    def _xinfo(self, j):
        blk = self._block(j)
        return {"split": self._x_split(j), "block_xpath": blk.xpath if blk else "",
                "sourceline": blk.sourceline if blk else 0}

    def expected_x(self, p_index):
        """XHTML word index corresponding to PDF word index `p_index`
        (nearest mapped neighbour - used for insert positions)."""
        n = len(self.p2x)
        if not n:
            return 0
        p_index = max(0, min(p_index, n))
        for i in range(p_index, n):
            if self.p2x[i] >= 0:
                return self.p2x[i]
        for i in range(min(p_index, n) - 1, -1, -1):
            if self.p2x[i] >= 0:
                return self.p2x[i] + 1
        return 0

    def expected_p(self, x_index):
        n = len(self.x2p)
        for j in range(max(0, x_index), n):
            if self.x2p[j] >= 0:
                return self.x2p[j]
        for j in range(min(x_index, n) - 1, -1, -1):
            if self.x2p[j] >= 0:
                return self.x2p[j] + 1
        return 0

    # ------------------------------------------------------------ text
    def _text_differences(self, ops):
        pw, xw = self.pdf.words, self.x.words
        deletes, inserts = [], []
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal":
                for k in range(i2 - i1):
                    self.p2x[i1 + k] = j1 + k
                    self.x2p[j1 + k] = i1 + k
                    a, b = textnorm.exact(pw[i1 + k].text), textnorm.exact(xw[j1 + k].text)
                    if a == b:
                        self.p_state[i1 + k] = self.x_state[j1 + k] = MATCH
                    else:
                        self.p_state[i1 + k] = self.x_state[j1 + k] = MODIFIED
                        self._modified(i1 + k, i1 + k + 1, j1 + k, j1 + k + 1, minor=True)
            elif tag == "replace":
                self._replace(i1, i2, j1, j2, deletes, inserts)
            elif tag == "delete":
                deletes.append((i1, i2))
            elif tag == "insert":
                inserts.append((j1, j2))
        self._moves_and_duplicates(deletes, inserts)
        for i1, i2 in deletes:
            self._emit_missing(i1, i2)
        for j1, j2 in inserts:
            self._emit_extra(j1, j2)

    def _replace(self, i1, i2, j1, j2, deletes, inserts):
        """Word-level pairing inside a replaced stretch: similar words are
        MODIFIED (character diff), the rest missing / extra."""
        pw, xw = self.pdf.words, self.x.words
        if (i2 - i1 > 3 or j2 - j1 > 3) and SequenceMatcher(
                None, [w.key for w in pw[i1:i2]], [w.key for w in xw[j1:j2]], autojunk=False).ratio() < 0.5:
            # unrelated stretches (e.g. a moved chapter): no word pairing -
            # missing + extra, so move / duplicate detection can see them whole
            deletes.append((i1, i2))
            inserts.append((j1, j2))
            return
        # pair by character similarity in order
        i, j = i1, j1
        while i < i2 and j < j2:
            ratio = SequenceMatcher(None, pw[i].key, xw[j].key).ratio() if (pw[i].key or xw[j].key) else 0
            if ratio >= 0.5 or (i2 - i1 == j2 - j1 and (i2 - i1) <= 3):
                k = 1
                # extend a run of pairwise-similar words into one difference
                while i + k < i2 and j + k < j2 and SequenceMatcher(
                        None, pw[i + k].key, xw[j + k].key).ratio() >= 0.5:
                    k += 1
                for t in range(k):
                    self.p2x[i + t] = j + t
                    self.x2p[j + t] = i + t
                    self.p_state[i + t] = self.x_state[j + t] = MODIFIED
                self._modified(i, i + k, j, j + k)
                i += k
                j += k
                continue
            # not similar: whichever side is longer loses a word
            if (i2 - i) >= (j2 - j):
                deletes.append((i, i + 1))
                i += 1
            else:
                inserts.append((j, j + 1))
                j += 1
        if i < i2:
            deletes.append((i, i2))
        if j < j2:
            inserts.append((j, j2))

    def _modified(self, i1, i2, j1, j2, minor=False):
        ptxt, xtxt = self._p_text(i1, i2), self._x_text(j1, j2)
        info = self._xinfo(j1)
        segs = char_segments(ptxt, xtxt)
        changed_chars = sum(max(len(p), len(x)) for op, p, x in segs if op != "equal")
        conf = 0.97 if minor else max(0.6, 1.0 - changed_chars / max(len(ptxt), len(xtxt), 1) * 0.5)
        kind_msg = "punctuation / case / accent difference" if minor else "text differs"
        d = self._add("MODIFIED_TEXT", pdf_text=ptxt, xhtml_text=xtxt, pdf_page=self.pdf.words[i1].page,
                      pdf_bboxes=self._bboxes(i1, i2), x_start=j1, x_end=j2, p_start=i1, p_end=i2,
                      segments=segs, confidence=conf, message=kind_msg, severity="LOW" if minor else None, **info)
        d.correction = Correction("replace_text", {"x_start": j1, "x_end": j2, "text": ptxt}, confidence=conf * 0.6,
                                  auto_safe=False, description=f"Replace XHTML '{xtxt}' with PDF '{ptxt}'")

    def _moves_and_duplicates(self, deletes, inserts):
        """A PDF-only run whose words appear as an XHTML-only run elsewhere
        is REORDERED (moved); an XHTML-only run repeating text that is
        already matched elsewhere is DUPLICATE."""
        pw, xw = self.pdf.words, self.x.words

        def merge(runs):
            runs = sorted(runs)
            out = []
            for a, b in runs:
                if out and a <= out[-1][1]:
                    out[-1][1] = max(out[-1][1], b)
                else:
                    out.append([a, b])
            return out
        deletes[:] = [tuple(r) for r in merge(deletes)]
        inserts[:] = [tuple(r) for r in merge(inserts)]
        used_ins = set()
        new_deletes = []
        for i1, i2 in deletes:
            pk = [pw[i].key for i in range(i1, i2)]
            moved = False
            if len(pk) >= 3:
                for n, (j1, j2) in enumerate(inserts):
                    if n in used_ins or abs((j2 - j1) - (i2 - i1)) > max(2, 0.2 * (i2 - i1)):
                        continue
                    xk = [xw[j].key for j in range(j1, j2)]
                    if SequenceMatcher(None, pk, xk, autojunk=False).ratio() >= 0.85:
                        used_ins.add(n)
                        moved = True
                        for t in range(min(i2 - i1, j2 - j1)):
                            self.p2x[i1 + t] = j1 + t
                            self.x2p[j1 + t] = i1 + t
                        for i in range(i1, i2):
                            self.p_state[i] = MOVED
                        for j in range(j1, j2):
                            self.x_state[j] = MOVED
                        info = self._xinfo(j1)
                        self._add("REORDERED_TEXT", pdf_text=self._p_text(i1, i2), xhtml_text=self._x_text(j1, j2),
                                  pdf_page=pw[i1].page, pdf_bboxes=self._bboxes(i1, i2), x_start=j1, x_end=j2,
                                  p_start=i1, p_end=i2, confidence=0.9,
                                  message=f"content found at a different reading position "
                                          f"(XHTML {info['split']})", **info)
                        break
            if not moved:
                new_deletes.append((i1, i2))
        deletes[:] = new_deletes
        # duplicates among the remaining inserts
        matched_index = defaultdict(list)
        for j, k in enumerate(xw):
            if self.x2p[j] >= 0 and j + 3 <= len(xw):
                matched_index[tuple(w.key for w in xw[j:j + 3])].append(j)
        new_inserts = []
        for n, (j1, j2) in enumerate(inserts):
            if n in used_ins:
                continue
            xk = [xw[j].key for j in range(j1, j2)]
            dup = False
            if len(xk) >= 3:
                for start in matched_index.get(tuple(xk[:3]), []):
                    other = [w.key for w in xw[start:start + len(xk)]]
                    if other == xk:
                        dup = True
                        for j in range(j1, j2):
                            self.x_state[j] = DUPLICATE
                        info = self._xinfo(j1)
                        p_here = self.expected_p(j1)
                        self._add("DUPLICATE_TEXT", xhtml_text=self._x_text(j1, j2),
                                  pdf_page=self.pdf.page_of_word(min(p_here, len(pw) - 1)) if pw else None,
                                  x_start=j1, x_end=j2, confidence=0.93,
                                  message=f"same text already appears in {self._x_split(start)}", **info)
                        self.differences[-1].correction = Correction(
                            "remove_text", {"x_start": j1, "x_end": j2}, confidence=0.6,
                            description="Remove the duplicated occurrence")
                        break
            if not dup:
                new_inserts.append((j1, j2))
        inserts[:] = new_inserts

    def _emit_missing(self, i1, i2):
        pw = self.pdf.words
        # one difference per PDF paragraph segment
        start = i1
        for i in range(i1, i2 + 1):
            if i == i2 or pw[i].para != pw[start].para:
                xpos = self.expected_x(start)
                info = self._xinfo(min(xpos, len(self.x.words) - 1)) if self.x.words else {}
                txt = self._p_text(start, i)
                d = self._add("MISSING_TEXT", pdf_text=txt, pdf_page=pw[start].page,
                              pdf_bboxes=self._bboxes(start, i), p_start=start, p_end=i, x_start=xpos, x_end=xpos,
                              confidence=0.95, message="present in the PDF, missing from the XHTML", **info)
                d.correction = Correction("insert_text", {"x_pos": xpos, "text": txt}, confidence=0.55,
                                          description=f"Insert '{txt[:40]}' into the XHTML at the mapped position")
                start = i

    def _emit_extra(self, j1, j2):
        xw = self.x.words
        start = j1
        for j in range(j1, j2 + 1):
            if j == j2 or xw[j].block != xw[start].block:
                info = self._xinfo(start)
                p_here = self.expected_p(start)
                page = self.pdf.page_of_word(min(p_here, len(self.pdf.words) - 1)) if self.pdf.words else None
                d = self._add("EXTRA_TEXT", xhtml_text=self._x_text(start, j), pdf_page=page, x_start=start,
                              x_end=j, confidence=0.9, message="present in the XHTML, not in the PDF", **info)
                d.correction = Correction("remove_text", {"x_start": start, "x_end": j}, confidence=0.5,
                                          description="Remove the extra XHTML text")
                start = j

    # ---------------------------------------------------- page mapping
    def _page_maps(self):
        self.pages = []
        for pg in self.pdf.pages:
            pm = PageMap(page=pg.number, printed=pg.printed, printed_confidence=pg.printed_confidence,
                         p_start=pg.word_start, p_end=pg.word_end)
            xs = [self.p2x[i] for i in range(pg.word_start, pg.word_end) if self.p2x[i] >= 0]
            pm.matched = sum(1 for i in range(pg.word_start, pg.word_end) if self.p_state[i] == MATCH)
            n = pg.word_end - pg.word_start
            if xs:
                pm.x_start, pm.x_end = min(xs), max(xs) + 1
                pm.splits = sorted({self._x_split(j) for j in xs}, key=lambda s: self.x.splits.index(s))
            pm.confidence = (len(xs) / n) if n else 0.0
            self.pages.append(pm)
        # image-only / empty pages: position between neighbours
        for k, pm in enumerate(self.pages):
            if pm.x_start is None:
                prev = next((self.pages[t].x_end for t in range(k - 1, -1, -1) if self.pages[t].x_end is not None),
                            0)
                pm.x_start = pm.x_end = prev
                sp = self._x_split(min(prev, len(self.x.words) - 1)) if self.x.words else None
                pm.splits = [sp] if sp else []
                pm.confidence = 0.5 if pg_has_no_text(self.pdf, pm.page) else 0.0
            if pm.confidence < 0.5 and (pm.p_end - pm.p_start) >= 20:
                self._add("LOW_CONFIDENCE_MAPPING", pdf_page=pm.page,
                          split=pm.splits[0] if pm.splits else None, confidence=1 - pm.confidence,
                          message=f"only {pm.confidence:.0%} of page {pm.page}'s words could be mapped to the XHTML")

    def page_start_x(self, page_no):
        """XHTML word index where PDF page `page_no` begins."""
        pm = self.pages[page_no - 1]
        pg = self.pdf.page(page_no)
        if pg.word_end > pg.word_start:
            return self.expected_x(pg.word_start)
        return pm.x_start or 0

    # ----------------------------------------------- paragraph structure
    def _paragraph_structure(self):
        pw, xw = self.pdf.words, self.x.words
        merged, split = defaultdict(list), defaultdict(list)
        for i in range(len(pw) - 1):
            j, jn = self.p2x[i], self.p2x[i + 1]
            if j < 0 or jn != j + 1 or self.p_state[i] == MOVED:
                continue
            same_page = pw[i].page == pw[i + 1].page
            p_break = pw[i].para != pw[i + 1].para
            x_break = xw[j].block != xw[jn].block
            if p_break and not x_break and same_page:
                merged[xw[j].block].append(i + 1)
            elif x_break and not p_break and same_page and xw[j].split == xw[jn].split:
                split[pw[i].para].append(jn)
        for blk_idx, starts in merged.items():
            blk = self.x.blocks[blk_idx]
            i = starts[0]
            self._add("MERGED_PARAGRAPHS", pdf_text=self._p_text(max(0, i - 6), min(len(pw), i + 6)),
                      xhtml_text=blk.text[:200], pdf_page=pw[i].page, pdf_bboxes=self._bboxes(i, i + 1),
                      split=blk.split, block_xpath=blk.xpath, sourceline=blk.sourceline, x_start=blk.word_start,
                      x_end=blk.word_end, p_start=i, p_end=i + 1, confidence=0.75,
                      message=f"{len(starts) + 1} PDF paragraphs are one XHTML <{blk.tag}>")
        for para, xs in split.items():
            j = xs[0]
            blk = self.x.blocks[xw[j].block]
            i = self.x2p[j]
            self._add("UNEXPECTED_PARAGRAPH_SPLIT", pdf_text=self._p_text(max(0, i - 6), min(len(pw), i + 6)),
                      xhtml_text=blk.text[:200], pdf_page=pw[i].page, pdf_bboxes=self._bboxes(i, i + 1),
                      split=blk.split, block_xpath=blk.xpath, sourceline=blk.sourceline, x_start=j, x_end=j + 1,
                      p_start=i, p_end=i + 1, confidence=0.7,
                      message=f"one PDF paragraph is split into {len(xs) + 1} XHTML blocks")

    # ---------------------------------------------------- page markers
    def _page_markers(self):
        markers = self.x.markers
        numbered = [pm for pm in self.pages if pm.printed and pm.printed_confidence >= 0.5]
        if not numbered:
            return
        uses_markers = len(markers) >= max(1, 0.3 * len(numbered))
        if not uses_markers:
            self._add("MISSING_PAGE_MARKER", pdf_page=numbered[0].page, confidence=0.8, severity="MEDIUM",
                      message=f"the XHTML has {len(markers)} page marker(s) for {len(numbered)} printed page numbers "
                              "in the PDF - the project may not use page markers")
            return
        by_label = defaultdict(list)
        for k, m in enumerate(markers):
            by_label[m.label].append(k)
        expected_labels = {pm.printed for pm in numbered}
        used = set()
        for pm in numbered:
            exp_x = self.page_start_x(pm.page)
            ks = by_label.get(pm.printed, [])
            conf = round(min(pm.printed_confidence, max(pm.confidence, 0.5)), 3)
            info = self._xinfo(min(exp_x, len(self.x.words) - 1)) if self.x.words else {}
            pg = self.pdf.page(pm.page)
            bb = [pg.printed_bbox] if pg.printed_bbox else []
            if len(ks) > 1:
                for k in ks[1:]:
                    m = markers[k]
                    self.marker_state[k] = DUPLICATE
                    used.add(k)
                    d = self._add("DUPLICATE_PAGE_MARKER", pdf_text=pm.printed, xhtml_text=m.label, pdf_page=pm.page,
                                  pdf_bboxes=bb, split=m.split, block_xpath=m.xpath, sourceline=m.sourceline,
                                  x_start=m.word_pos, x_end=m.word_pos, confidence=0.9,
                                  message=f"page marker {m.label} appears {len(ks)} times")
                    d.correction = Correction("remove_page_marker", {"split": m.split, "xpath": m.xpath},
                                              confidence=0.85, auto_safe=False,
                                              description=f"Remove the duplicate page marker {m.label}")
            if ks:
                k = min(ks, key=lambda t: abs(markers[t].word_pos - exp_x))
                m = markers[k]
                used.add(k)
                pm.marker = m
                off = abs(m.word_pos - exp_x)
                if off <= MARKER_TOLERANCE_WORDS or (pg.word_end == pg.word_start):
                    pm.marker_state = self.marker_state[k] = MATCH
                else:
                    pm.marker_state = self.marker_state[k] = MODIFIED
                    d = self._add("MISPLACED_PAGE_MARKER", pdf_text=pm.printed, xhtml_text=m.label, pdf_page=pm.page,
                                  pdf_bboxes=bb, split=m.split, block_xpath=m.xpath, sourceline=m.sourceline,
                                  x_start=m.word_pos, x_end=m.word_pos, confidence=conf,
                                  message=f"page marker {m.label} is {off} words away from where page {pm.printed} "
                                          "starts in the PDF")
                    d.correction = Correction("move_page_marker", {"split": m.split, "xpath": m.xpath,
                                                                   "x_pos": exp_x},
                                              confidence=conf * 0.9, auto_safe=conf >= 0.9 and pm.confidence >= 0.9,
                                              description=f"Move page marker {m.label} to the start of PDF page "
                                                          f"{pm.page}")
                continue
            # no marker with this label: a wrong label near the expected spot?
            near = [k for k, m in enumerate(markers) if k not in used and m.label not in expected_labels
                    and abs(m.word_pos - exp_x) <= MARKER_TOLERANCE_WORDS + 2]
            if near:
                k = near[0]
                m = markers[k]
                used.add(k)
                pm.marker = m
                pm.marker_state = self.marker_state[k] = MODIFIED
                d = self._add("WRONG_PAGE_MARKER", pdf_text=pm.printed, xhtml_text=m.label, pdf_page=pm.page,
                              pdf_bboxes=bb, split=m.split, block_xpath=m.xpath, sourceline=m.sourceline,
                              x_start=m.word_pos, x_end=m.word_pos, confidence=conf,
                              message=f"XHTML marker says {m.label} where the PDF page is {pm.printed}")
                d.correction = Correction("relabel_page_marker", {"split": m.split, "xpath": m.xpath,
                                                                  "label": pm.printed},
                                          confidence=conf, auto_safe=conf >= 0.9,
                                          description=f"Relabel page marker {m.label} -> {pm.printed}")
                continue
            pm.marker_state = MISSING
            d = self._add("MISSING_PAGE_MARKER", pdf_text=pm.printed, pdf_page=pm.page, pdf_bboxes=bb,
                          x_start=exp_x, x_end=exp_x, confidence=conf,
                          message=f"MISSING PAGE MARKER: {pm.printed}", **info)
            d.correction = Correction("insert_page_marker", {"x_pos": exp_x, "label": pm.printed},
                                      confidence=conf, auto_safe=conf >= 0.9,
                                      description=f"Insert page marker {pm.printed} where PDF page {pm.page} "
                                                  "starts")
        for k, m in enumerate(markers):
            if k in used:
                continue
            if m.label in expected_labels:
                self.marker_state[k] = MATCH
                continue
            self.marker_state[k] = EXTRA
            p = self.expected_p(m.word_pos)
            self._add("EXTRA_PAGE_MARKER", xhtml_text=m.label, split=m.split, block_xpath=m.xpath,
                      sourceline=m.sourceline, x_start=m.word_pos, x_end=m.word_pos,
                      pdf_page=self.pdf.page_of_word(min(p, len(self.pdf.words) - 1)) if self.pdf.words else None,
                      confidence=0.7, message=f"page marker {m.label} has no matching printed page in the PDF")

    # ---------------------------------------------------------- images
    def _images(self):
        pimgs, ximgs = self.pdf.images, self.x.images
        self.image_pairs = []
        for xi in ximgs:
            if not xi.exists:
                self._add("BROKEN_IMAGE_REFERENCE", xhtml_text=xi.src, split=xi.split, block_xpath=xi.xpath,
                          sourceline=xi.sourceline, x_start=xi.word_pos, x_end=xi.word_pos, confidence=0.99,
                          message=f"image file {xi.src} does not exist in the package",
                          image={"xhtml_src": xi.src, "xhtml_split": xi.split})
        live = [x for x in ximgs if x.exists]
        exp = {pi.uid: self.expected_x(pi.word_pos) for pi in pimgs}
        # 1. slot each PDF image to the nearest XHTML image around its expected position
        slots = {}
        taken = set()
        cand = sorted(((abs(x.word_pos - exp[p.uid]), p.uid, n) for p in pimgs for n, x in enumerate(live)
                       if abs(x.word_pos - exp[p.uid]) <= IMAGE_SLOT_WINDOW), key=lambda t: t[0])
        for dist, puid, n in cand:
            if puid in slots or n in taken:
                continue
            slots[puid] = n
            taken.add(n)
        package_images = self._package_image_index()

        def sim(p, x):
            return visual_similarity(p, x)

        content_match = {}   # live index -> pdf uid (visual identity anywhere)
        for n, x in enumerate(live):
            best = max(pimgs, key=lambda p: sim(p, x), default=None)
            if best is not None and sim(best, x) >= IMAGE_MATCH:
                content_match[n] = best.uid
        for p in pimgs:
            pm = self.pages[p.page - 1]
            n = slots.get(p.uid)
            base = {"pdf_uid": p.uid, "pdf_page": p.page, "pdf_bbox": p.bbox, "pdf_size": (p.width, p.height),
                    "pdf_aspect": round(p.aspect, 3), "pdf_sha1": p.sha1, "pdf_caption": p.caption}
            if n is not None:
                x = live[n]
                s = sim(p, x)
                ar = abs(p.aspect - x.aspect) / max(p.aspect, 0.01) if p.aspect and x.aspect else 0
                info = dict(base, xhtml_src=x.src, xhtml_file=x.file, xhtml_split=x.split, xhtml_xpath=x.xpath,
                            xhtml_size=(x.width, x.height), xhtml_aspect=round(x.aspect, 3), xhtml_sha1=x.sha1,
                            similarity=round(s, 3), xhtml_caption=x.caption)
                if s >= IMAGE_MATCH and ar < 0.04:
                    state = MATCH
                elif s >= IMAGE_SIMILAR:
                    state = MODIFIED
                    self._add("MODIFIED_IMAGE", pdf_page=p.page, pdf_bboxes=[p.bbox], split=x.split,
                              block_xpath=x.xpath, sourceline=x.sourceline, x_start=x.word_pos, x_end=x.word_pos,
                              confidence=0.75, image=info, xhtml_text=x.src,
                              message=f"same picture, but {'cropped/resized' if ar >= 0.04 else 'visually different'} "
                                      f"(similarity {s:.0%}, aspect {p.aspect:.2f} vs {x.aspect:.2f})")
                else:
                    state = MISSING
                    right = self._best_package_file(p, package_images)
                    d = self._add("WRONG_IMAGE", pdf_page=p.page, pdf_bboxes=[p.bbox], split=x.split,
                                  block_xpath=x.xpath, sourceline=x.sourceline, x_start=x.word_pos, x_end=x.word_pos,
                                  confidence=0.9, image=dict(info, suggested_file=right[0] if right else None),
                                  xhtml_text=x.src,
                                  message=f"XHTML shows {x.src} where the PDF has a different image "
                                          f"(similarity {s:.0%})")
                    if right:
                        d.correction = Correction("remap_image", {"split": x.split, "xpath": x.xpath,
                                                                  "file": right[0]},
                                                  confidence=right[1], auto_safe=right[1] >= 0.95,
                                                  description=f"Point this image at {right[0]} "
                                                              f"(visual match {right[1]:.0%})")
                offset = x.word_pos - exp[p.uid]
                if state in (MATCH, MODIFIED) and abs(offset) > IMAGE_POS_TOLERANCE:
                    state = MOVED
                    d = self._add("MOVED_IMAGE", pdf_page=p.page, pdf_bboxes=[p.bbox], split=x.split,
                                  block_xpath=x.xpath, sourceline=x.sourceline, x_start=x.word_pos, x_end=x.word_pos,
                                  confidence=0.85, image=info, xhtml_text=x.src,
                                  message=f"image sits {abs(offset)} words {'after' if offset > 0 else 'before'} its "
                                          "PDF reading position (TEXT / IMAGE / TEXT order changed)")
                    d.correction = Correction("move_image", {"split": x.split, "xpath": x.xpath,
                                                             "x_pos": exp[p.uid]},
                                              confidence=0.8, description="Move the image back to its PDF reading "
                                                                          "position")
                self._captions(p, x, info)
                self.image_pairs.append(dict(info, state=state, xhtml_index=self.x.images.index(x)))
                pm.images.append((p.uid, state))
                continue
            # no image at the expected place: moved elsewhere, or missing
            elsewhere = [n for n, puid in content_match.items() if puid == p.uid and n not in taken]
            if elsewhere:
                n = elsewhere[0]
                taken.add(n)
                x = live[n]
                info = dict(base, xhtml_src=x.src, xhtml_file=x.file, xhtml_split=x.split, xhtml_xpath=x.xpath,
                            xhtml_size=(x.width, x.height), similarity=round(sim(p, x), 3))
                d = self._add("MOVED_IMAGE", pdf_page=p.page, pdf_bboxes=[p.bbox], split=x.split,
                              block_xpath=x.xpath, sourceline=x.sourceline, x_start=x.word_pos, x_end=x.word_pos,
                              confidence=0.85, image=info, xhtml_text=x.src,
                              message=f"image is {abs(x.word_pos - exp[p.uid])} words away from its PDF position "
                                      f"(expected near XHTML word {exp[p.uid]} in "
                                      f"{self._x_split(min(exp[p.uid], len(self.x.words) - 1))})")
                d.correction = Correction("move_image", {"split": x.split, "xpath": x.xpath, "x_pos": exp[p.uid]},
                                          confidence=0.75, description="Move the image back to its PDF reading "
                                                                       "position")
                self.image_pairs.append(dict(info, state=MOVED, xhtml_index=self.x.images.index(x)))
                pm.images.append((p.uid, MOVED))
                continue
            right = self._best_package_file(p, package_images)
            xpos = exp[p.uid]
            d = self._add("MISSING_IMAGE", pdf_page=p.page, pdf_bboxes=[p.bbox], x_start=xpos, x_end=xpos,
                          confidence=0.9, image=dict(base, suggested_file=right[0] if right else None),
                          message="image on the PDF page has no counterpart in the XHTML"
                                  + (f" ({right[0]} in the package matches it)" if right else ""),
                          **(self._xinfo(min(xpos, len(self.x.words) - 1)) if self.x.words else {}))
            if right:
                d.correction = Correction("insert_image", {"x_pos": xpos, "file": right[0]}, confidence=0.6,
                                          description=f"Insert {right[0]} at the PDF reading position")
            self.image_pairs.append(dict(base, state=MISSING))
            pm.images.append((p.uid, MISSING))
        for n, x in enumerate(live):
            if n in taken:
                continue
            p_here = self.expected_p(x.word_pos)
            page = self.pdf.page_of_word(min(p_here, len(self.pdf.words) - 1)) if self.pdf.words else None
            self._add("EXTRA_IMAGE", split=x.split, block_xpath=x.xpath, sourceline=x.sourceline, x_start=x.word_pos,
                      x_end=x.word_pos, pdf_page=page, confidence=0.6, xhtml_text=x.src,
                      image={"xhtml_src": x.src, "xhtml_file": x.file, "xhtml_split": x.split,
                             "xhtml_xpath": x.xpath, "xhtml_size": (x.width, x.height)},
                      message="XHTML image has no corresponding image in the PDF")
            self.image_pairs.append({"xhtml_src": x.src, "xhtml_split": x.split, "xhtml_xpath": x.xpath,
                                     "state": UNCERTAIN, "xhtml_index": self.x.images.index(x)})
        # duplicate captions
        caps = Counter(textnorm.key(x.caption) for x in live if x.caption)
        for x in live:
            if x.caption and caps[textnorm.key(x.caption)] > 1:
                self._add("DUPLICATE_CAPTION", xhtml_text=x.caption, split=x.split, block_xpath=x.caption_xpath,
                          x_start=x.word_pos, x_end=x.word_pos, confidence=0.8,
                          message="the same caption is used by more than one image")

    def _captions(self, p, x, info):
        if not p.caption:
            return
        if not x.caption:
            self._add("MISSING_CAPTION", pdf_text=p.caption, pdf_page=p.page,
                      pdf_bboxes=[p.caption_bbox] if p.caption_bbox else [], split=x.split, block_xpath=x.xpath,
                      x_start=x.word_pos, x_end=x.word_pos, confidence=0.75, image=info,
                      message="the PDF image has a caption; the XHTML image has none")
            return
        r = SequenceMatcher(None, textnorm.key(p.caption), textnorm.key(x.caption)).ratio()
        if r >= 0.85:
            return
        other = max(self.pdf.images, key=lambda q: SequenceMatcher(None, textnorm.key(q.caption),
                                                                  textnorm.key(x.caption)).ratio())
        if other is not p and other.caption and SequenceMatcher(
                None, textnorm.key(other.caption), textnorm.key(x.caption)).ratio() >= 0.85:
            self._add("CAPTION_ON_WRONG_IMAGE", pdf_text=p.caption, xhtml_text=x.caption, pdf_page=p.page,
                      split=x.split, block_xpath=x.caption_xpath, x_start=x.word_pos, x_end=x.word_pos,
                      confidence=0.85, image=info,
                      message=f"this caption belongs to the image on PDF page {other.page}")
        else:
            self._add("WRONG_CAPTION", pdf_text=p.caption, xhtml_text=x.caption, pdf_page=p.page, split=x.split,
                      block_xpath=x.caption_xpath, x_start=x.word_pos, x_end=x.word_pos, confidence=0.7, image=info,
                      segments=char_segments(p.caption, x.caption), message=f"caption differs ({r:.0%} similar)")

    def _package_image_index(self):
        from core.qc.xhtml_model import package_image_info
        out = []
        try:
            files = [rel for rel, it in self.mgr.manifest().items()
                     if (it.get("media-type") or "").startswith("image/")]
        except Exception:
            files = []
        if not files:
            import os
            for dirpath, _d, names in os.walk(self.mgr.root):
                for n in names:
                    if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        files.append(os.path.relpath(os.path.join(dirpath, n), self.mgr.root).replace(os.sep, "/"))
        for rel in files:
            info = package_image_info(self.mgr, rel)
            if info:
                out.append((rel, info))
        return out

    @staticmethod
    def _best_package_file(p, package_images):
        from types import SimpleNamespace
        best = None
        for rel, (_sha, ah, dh, _w, _h, thumb) in package_images:
            s = visual_similarity(p, SimpleNamespace(ahash=ah, dhash=dh, thumb=thumb))
            if best is None or s > best[1]:
                best = (rel, s)
        return best if best and best[1] >= IMAGE_MATCH else None

    # --------------------------------------------------- links/package
    def _links_and_package(self):
        try:
            broken = self.mgr.broken_links()
        except Exception:
            broken = []
        for frm, href, reason, sugg in broken:
            d = self._add("BROKEN_LINK", xhtml_text=href, split=frm, confidence=0.99,
                          message=f"{reason}" + (f" - the id exists in {sugg.split('#')[0] or frm}" if sugg else ""))
            if sugg:
                d.correction = Correction("fix_link", {"file": frm, "old": href, "new": sugg}, confidence=0.95,
                                          auto_safe=True, description=f"Point the link at {sugg}")
        try:
            problems = self.mgr.package_problems()
        except Exception:
            problems = []
        for kind, msg, fix in problems:
            d = self._add("PACKAGE_PROBLEM", xhtml_text=kind, message=msg, confidence=0.99)
            if fix:
                d.correction = Correction("package_fix", {"fix": list(fix)}, confidence=0.97, auto_safe=True,
                                          description=msg)

    # ----------------------------------------------------------- splits
    def _splits(self):
        self.split_maps = {}
        for rel in self.x.splits:
            w0, w1, _b0, _b1 = self.x.split_ranges[rel]
            sm = SplitMap(split=rel, x_start=w0, x_end=w1)
            ps = [self.x2p[j] for j in range(w0, w1) if self.x2p[j] >= 0]
            sm.matched = sum(1 for j in range(w0, w1) if self.x_state[j] == MATCH)
            sm.confidence = (len(ps) / (w1 - w0)) if w1 > w0 else 1.0
            if ps:
                pages = sorted({self.pdf.words[i].page for i in ps})
                # pages carrying a real share of the split (stray single-word
                # matches such as a repeated heading word are ignored)
                cnt = Counter(self.pdf.words[i].page for i in ps)
                sm.pages = [p for p in pages if cnt[p] >= max(2, 0.1 * len(ps))] or \
                    [cnt.most_common(1)[0][0]]
            self.split_maps[rel] = sm
            if w1 - w0 >= 20 and sm.confidence < 0.4:
                self._add("UNMAPPED_SPLIT", split=rel, x_start=w0, x_end=w0, confidence=1 - sm.confidence,
                          message=f"only {sm.confidence:.0%} of {rel}'s words were found in the PDF")
        prev_rel, prev_first = None, None
        for rel in self.x.splits:
            sm = self.split_maps[rel]
            if not sm.pages:
                continue
            first = sm.pages[0]
            if prev_first is not None and first < prev_first and \
                    self.split_maps[prev_rel].pages and first < self.split_maps[prev_rel].pages[0]:
                d = self._add("SPLIT_ORDER", split=rel, pdf_page=first, x_start=sm.x_start, x_end=sm.x_start,
                              confidence=0.85,
                              message=f"{rel} (PDF pages {first}-{sm.pages[-1]}) comes after {prev_rel} "
                                      f"(PDF pages {prev_first}-...) in the spine")
                order = sorted(self.x.splits, key=lambda r: (self.split_maps[r].pages or [10 ** 6])[0])
                d.correction = Correction("reorder_splits", {"order": order}, confidence=0.8,
                                          description="Reorder the spine to follow the PDF")
            prev_rel, prev_first = rel, first

    # -------------------------------------------------------- decisions
    def _apply_decisions(self):
        for d in self.differences:
            d.id = d.signature()
            dec = self.decisions.get(d.id)
            if dec:
                d.status = dec.get("status", d.status)

    # ----------------------------------------------------------- scores
    def _scores(self):
        def pct(v):
            return round(100.0 * max(0.0, min(1.0, v)), 1)
        open_diffs = [d for d in self.differences if d.status not in ("ignored", "rejected")]
        np_, nx = len(self.pdf.words), len(self.x.words)
        matched = self.p_state.count(MATCH)
        modified = self.p_state.count(MODIFIED)
        moved = self.p_state.count(MOVED)
        text = (matched + 0.5 * modified + 0.75 * moved) / max(np_, nx, 1)
        paras = max(1, len({w.para for w in self.pdf.words}))
        struct_issues = sum(1 for d in open_diffs if d.category in ("structure",))
        structure = 1.0 - struct_issues / paras
        n_img = max(1, len(self.pdf.images))
        img_ok = sum(1 for p in self.image_pairs if p.get("state") == MATCH) + \
            0.5 * sum(1 for p in self.image_pairs if p.get("state") in (MODIFIED, MOVED) and "pdf_uid" in p)
        images = img_ok / n_img if self.pdf.images else 1.0
        numbered = [pm for pm in self.pages if pm.printed and pm.printed_confidence >= 0.5]
        markers = (sum(1 for pm in numbered if pm.marker_state == MATCH) / len(numbered)) if numbered else 1.0
        reading = 1.0 - moved / max(matched + modified + moved, 1)
        try:
            n_links = max(1, len(self.mgr.link_index()))
        except Exception:
            n_links = 1
        links = 1.0 - sum(1 for d in open_diffs if d.kind == "BROKEN_LINK") / n_links
        try:
            n_items = max(1, len(self.mgr.manifest()))
        except Exception:
            n_items = 1
        package = 1.0 - sum(1 for d in open_diffs if d.kind == "PACKAGE_PROBLEM") / n_items
        overall = (0.4 * text + 0.15 * structure + 0.15 * images + 0.1 * markers + 0.1 * reading
                   + 0.05 * links + 0.05 * package)
        self.scores = {"Overall": pct(overall), "Text": pct(text), "Structure": pct(structure),
                       "Images": pct(images), "Page Markers": pct(markers), "Reading Order": pct(reading),
                       "Links": pct(links), "Package": pct(package)}
        per_page = defaultdict(list)
        for d in open_diffs:
            if d.pdf_page:
                per_page[d.pdf_page].append(d)
        for pm in self.pages:
            n = pm.p_end - pm.p_start
            t = (sum(1 for i in range(pm.p_start, pm.p_end) if self.p_state[i] == MATCH) +
                 0.5 * sum(1 for i in range(pm.p_start, pm.p_end) if self.p_state[i] in (MODIFIED, MOVED))) / n \
                if n else 1.0
            im = (sum(1 for _u, s in pm.images if s == MATCH) / len(pm.images)) if pm.images else 1.0
            mk = 1.0 if (pm.marker_state in (None, MATCH) or not pm.printed) else 0.0
            st = 1.0 - min(1.0, sum(1 for d in per_page[pm.page] if d.category == "structure") / 5)
            pm.scores = {"Match": pct(0.55 * t + 0.2 * im + 0.15 * mk + 0.1 * st), "Text": pct(t),
                         "Images": pct(im), "Page Marker": pct(mk), "Structure": pct(st),
                         "Differences": len(per_page[pm.page])}
        per_split = defaultdict(list)
        for d in open_diffs:
            if d.split:
                per_split[d.split].append(d)
        for rel, sm in self.split_maps.items():
            n = sm.x_end - sm.x_start
            t = (sm.matched + 0.5 * sum(1 for j in range(sm.x_start, sm.x_end) if self.x_state[j] in (MODIFIED, MOVED))
                 ) / n if n else 1.0
            ds = per_split[rel]
            sm.differences = len(ds)
            sm.scores = {"Match": pct(t * 0.8 + 0.2 * (1 - min(1.0, len(ds) / 20))), "Text": pct(t),
                         "Missing": sum(1 for d in ds if d.kind == "MISSING_TEXT"),
                         "Extra": sum(1 for d in ds if d.kind == "EXTRA_TEXT"),
                         "Images": sum(1 for d in ds if d.category in ("image", "caption")),
                         "Page Markers": sum(1 for d in ds if d.category == "page"),
                         "Links": sum(1 for d in ds if d.kind == "BROKEN_LINK"),
                         "Warnings": len(ds)}

    # ------------------------------------------------------------ query
    def differences_for(self, filters=None, statuses=None):
        out = []
        for d in self.differences:
            if filters and not any(d.kind in kinds for name, kinds in FILTERS if name in filters):
                continue
            if statuses and d.status not in statuses:
                continue
            out.append(d)
        return out

    def location_for_pdf(self, page, x=None, y=None):
        """(split, xhtml word index) for a PDF point (click-to-map)."""
        pg = self.pdf.page(page)
        best = None
        if x is not None and y is not None:
            for i in range(pg.word_start, pg.word_end):
                w = self.pdf.words[i]
                d = abs((w.bbox[1] + w.bbox[3]) / 2 - y) * 3 + abs((w.bbox[0] + w.bbox[2]) / 2 - x)
                if best is None or d < best[0]:
                    best = (d, i)
        i = best[1] if best else pg.word_start
        j = self.p2x[i] if 0 <= i < len(self.p2x) and self.p2x[i] >= 0 else self.expected_x(i)
        j = min(j, max(0, len(self.x.words) - 1))
        return self._x_split(j), j

    def location_for_xhtml(self, j):
        """(pdf page, bbox) for an XHTML word index."""
        i = self.x2p[j] if 0 <= j < len(self.x2p) and self.x2p[j] >= 0 else self.expected_p(j)
        i = min(i, max(0, len(self.pdf.words) - 1))
        if not self.pdf.words:
            return 1, None
        w = self.pdf.words[i]
        return w.page, w.bbox


def pg_has_no_text(pdf, page_no) -> bool:
    pg = pdf.page(page_no)
    return pg.word_end == pg.word_start
