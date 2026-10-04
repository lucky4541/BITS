"""CUPEPUB client validation - the rules of the client's own validation tool
(SPiXVali, Client CUPEPUB, Rule_List.xml EPUB-001 ... EPUB-051), reproduced
natively so they can run inside VALIDATE & AUTO-FIX and be re-checked after
every repair, without the Windows tool.

Each rule follows the tool's own logic (the same regular expressions, the
same XPath conditions, the same exemptions), e.g.:

  EPUB-009  a text node that is NOT inside an <a> or a <span> and whose
            parent is not <title>/<h1..9> containing a reference such as
            "chapter 3", "Figure 2.1", "equation 3", "[1863]", "www..." -
            chapter / part references only when that chapter / part file
            exists (_ch3.xhtml / _pt2.xhtml)
  EPUB-010  the mojibake list (CAN_Nast_Junk) matched against every XHTML
            file read as UTF-8 and read as UTF-7 - so every literal non-ASCII
            character whose bytes spell one of them is reported; ASCII-only
            XHTML (characters written as &#xHHHH;) is clean
  EPUB-043  in a document containing epub:type="index": every number in a
            text node that is not inside an <a> (a <span> does NOT exempt it)
  EPUB-044  "<a>123</a>-<a>25</a>": both ends must be links to #Page_N and
            the second must point to the full page (Page_125)
  EPUB-045  one <a> covering a whole range "12-15"
  EPUB-046  roman numerals not inside an <a> in li class="index"
  EPUB-047  the text right after <i>see</i> / <i>see also</i> not linked

validate(files, epub_name) -> ClientReport. `files` maps archive names to
bytes (a MutablePackage's entries, or a zip read by validate_epub)."""
import io
import os
import posixpath
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.epub.client_rules import cupepub_data as D
from core.epub.client_rules import rawmarkup as R
from core.epub.integrity_snapshot import parse_lenient

PROFILE = "CUPEPUB"
EPUB_NS = "http://www.idpf.org/2007/ops"
JUNK_RE = re.compile("(" + "|".join(D.JUNK_ALTERNATIVES) + ")")

LINK_RE = re.compile(r"\b(Chapters?\.?|Sec(tion)?s?\.?|Figs?(ures?|\.|)|Tabs?(les?|\.|)|Box|Equations?|Eqs?\.?|"
                     r"Appendix|Appendices|Part) +\(?(\d+|[A-RT-Z]+\b)|Chs?\. \d+|(www|http|https|@|\.com|\.org|\.co)"
                     r"|\[\d+((–|-|,) ?\d+)?(, \d+)*(-\d+)?\]", re.I)
INDEX_NUM_RE = re.compile(r"(.)\d+\b")
INDEX_RANGE_RE = re.compile(r"([0-9]+)(–|-)([0-9]+)")
INDEX_DASH_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:–|-|.#x2013;|#x2013;|.#x002D;|#x002D;|.#8211;|#8211;)\s*[lI]?(\d+)\b",
                           re.I)
INDEX_NOTE_RE = re.compile(r"(^|[^A-Za-z0-9])(n|nn)[.]?([0-9]+)(?=$|[^A-Za-z0-9])", re.I)
INDEX_DASH_ONLY_RE = re.compile(r"^(–|-|”|##x2013;|##8211;|##x002D;|##x201D;|##8221;)$", re.I)
INDEX_A_RANGE_RE = re.compile(r"\d+([–\-”]|.#x2013;|.#8211;|.#x002D;|.#x201D;|.#8221;)\d+")
ROMAN_RE = re.compile(r"([A-Z][a-z]+ )?\b(I{1,3}|I?V|VI{1,3}|I?X|XI{1,3}|XVI{0,3}|X{1,3}|XLV?I{0,3})\b", re.I)
SEE_RE = re.compile(r"^see( also)?$")
PAGE_HREF_RE = re.compile(r"#Page_(\d+)$", re.I)
XHTML_NAME_RE = re.compile(r"^\d+_[A-Za-z\d]{5}_[a-z]{2,3}\d*.xhtml$")
IMAGE_NAME_RES = (re.compile(r"^cover\.jpg$"), re.compile(r"^[a-z]+\d+-logo\.jpg$"),
                  re.compile(r"^[a-z]+\d+-(fig|figu|tbl|tblu|eqn|inline|codfig|icon)-\d+\.png$"))
ENTITY_FORMAT_RE = re.compile(r"&([Aa]mp|#x0?0026|#0?38);[a-zA-Z]{3,10};|&(amp|#x0?0026|#0?38);#\d{4};|"
                              r"&(amp|#x0?0026|#0?38);#x[\dA-Za-z]{4};")
MERGED_RE = re.compile(r"</a>([A-Za-z]|\d)")
LINK_CITED_RE = re.compile(r"\b(Tab(les?|\.|s\.)|Fig(ures?|\.|s\.)|Sec(tions?|\.|s\.)|Equations?|Eqns?\.|Eqs?\.)$", re.I)
FRONT_TITLES = re.compile(r"^(Cover Page|Halftitle Page|Series Page|Title Page|Copyright Page|Dedication)$", re.I)
FRONT_TYPES = re.compile(r"^(cover|halftitlepage|seriespage|titlepage|copyright-page|dedication)$")
H1_CLASSES = re.compile(r"^(fmtitle|FM-Title|bmtitle|chaptitle|parttitle|secttitle|apptitle|glosstitle|indtitle)$")
NAV_LANDMARKS = (("cover", "Cover Page"), ("toc", "Contents"), ("part", "Begin Reading"), ("index", "Index"))
OPF_GUIDE = (("cover", "Cover"), ("toc", "Table of Contents"), ("text", "Begin reading"), ("index", "Index"))
MAX_PIXELS = 4_200_000


@dataclass
class ClientFinding:
    code: str
    severity: str
    file: str
    line: int
    col: int
    message: str
    fix: str = ""                       # the automatic repair that addresses it ("" = manual)

    def to_dict(self):
        return dict(self.__dict__)

    def log_line(self, n):
        return f"{n}. {self.severity}[{self.code}]:{self.line}:{self.col} {self.message}"


@dataclass
class ClientReport:
    profile: str = PROFILE
    epub_name: str = ""
    findings: list = field(default_factory=list)

    def counts(self):
        c = Counter(f.severity for f in self.findings)
        return {"Error": c.get("Error", 0), "Warning": c.get("Warning", 0)}

    def by_code(self):
        c = Counter(f.code for f in self.findings)
        return dict(sorted(c.items()))

    def log_text(self):
        """The findings in SPiXVali's own log layout."""
        errs = [f for f in self.findings if f.severity == "Error"]
        warns = [f for f in self.findings if f.severity != "Error"]
        out = [f"#Client Name         \t: {self.profile}", f"#Input EPUB          \t: {self.epub_name}", "",
               f"#Total Error count:{len(errs)}", f"#Total Warning count:{len(warns)}", "",
               "#List of errors:", "#---------------", ""]
        out += [f.log_line(i) for i, f in enumerate(errs, 1)]
        out += ["", "#List of warnings:", "#-----------------", ""]
        out += [f.log_line(i) for i, f in enumerate(warns, 1)]
        return "\n".join(out) + "\n"


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def attr(el, name):
    """Attribute by the tool's flattened name (epub_type = epub:type)."""
    if name == "epub_type":
        return el.get(f"{{{EPUB_NS}}}type") or el.get("epub:type")
    return el.get(name)


def has_token(value, token):
    return token in (value or "").split()


# ============================================================ the book
class Book:
    """Read-only view of the package for the client rules (also used by the
    client-rule repairs to find link targets)."""

    def __init__(self, files: dict, epub_name: str = ""):
        self.files = files
        self.epub_name = epub_name
        self.names = sorted(n for n in files if not n.endswith("/"))
        self.xhtml = [n for n in self.names if n.lower().endswith(".xhtml")]
        self._raw, self._root, self._segs = {}, {}, {}
        self.opf = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf)
        self._targets = None

    # ------------------------------------------------------------ access
    def _find_opf(self):
        try:
            from lxml import etree
            c = etree.fromstring(self.files.get("META-INF/container.xml", b""))
            rf = next((e for e in c.iter() if _local(e.tag) == "rootfile"), None)
            if rf is not None and rf.get("full-path") in self.files:
                return rf.get("full-path")
        except Exception:  # noqa: BLE001
            pass
        return next((n for n in self.names if n.lower().endswith(".opf")), "")

    def raw(self, name):
        if name not in self._raw:
            b = self.files[name]
            self._raw[name] = b.decode("utf-8", errors="replace").lstrip("﻿")
        return self._raw[name]

    def root(self, name):
        if name not in self._root:
            self._root[name] = parse_lenient(self.files[name])[0]
        return self._root[name]

    def segs(self, name):
        if name not in self._segs:
            self._segs[name] = R.walk(self.raw(name))
        return self._segs[name]

    def elements(self, name, local=None):
        root = self.root(name)
        if root is None:
            return []
        return [e for e in root.iter() if isinstance(e.tag, str) and (local is None or _local(e.tag) == local)]

    def is_index_doc(self, name):
        return any(attr(e, "epub_type") == "index" for e in self.elements(name))

    def is_nav(self, name):
        return re.match(r"^nav(igation)?.xhtml$", posixpath.basename(name).lower()) is not None

    def images(self):
        return [n for n in self.names if any(p.lower().startswith("image") for p in n.split("/")[:-1])
                and posixpath.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp",
                                                         ".tif", ".tiff")]

    def rel(self, from_doc, target, frag=""):
        r = posixpath.relpath(target, posixpath.dirname(from_doc) or ".") if target != from_doc else ""
        return r + (f"#{frag}" if frag else "")

    def opf_text(self):
        return self.raw(self.opf) if self.opf else ""

    def isbn(self):
        m = re.search(r'<dc:identifier id="isbn-id">(?>urn:isbn:)?([^<]+?)</dc:identifier>', self.opf_text())
        return m.group(1).strip() if m else ""

    def any_isbn(self):
        """The ISBN of the publication whatever the identifier id is."""
        own = self.isbn()
        if own:
            return re.sub(r"[^0-9Xx]", "", own)
        for m in re.finditer(r"<dc:identifier\b[^>]*>(?:urn:isbn:)?\s*([0-9Xx][0-9Xx\- ]{8,20})</dc:identifier>",
                             self.opf_text()):
            return re.sub(r"[^0-9Xx]", "", m.group(1))
        return ""

    # ------------------------------------------------------------ targets
    @property
    def targets(self):
        if self._targets is None:
            self._targets = Targets(self)
        return self._targets


class Targets:
    """Everything a generated link may point to - only targets that exist."""

    def __init__(self, book: Book):
        self.book = book
        self.pages = {}                 # label (lower) -> (file, id)
        self.page_dups = Counter()
        self.chapters, self.parts, self.appendices = {}, {}, {}
        self.labels = defaultdict(list)   # ("figure"|"table"|"equation"|"section"|"appendix"|"box", key) -> [(f, id)]
        self.refs = defaultdict(list)     # reference number -> [(file, id)]
        self.index_entries = defaultdict(list)   # normalized head word -> [(file, id or None, start offset)]
        self._build()

    def _build(self):
        b = self.book
        for n in b.xhtml:
            base = posixpath.basename(n).lower()
            m = re.search(r"_ch(\d+)[a-z]?\.", base)
            if m:
                self.chapters.setdefault(int(m.group(1)), n)
            m = re.search(r"_pt(\d+)[a-z]?\.", base)
            if m:
                self.parts.setdefault(int(m.group(1)), n)
            m = re.search(r"_app(\d+)[a-z]?\.", base)
            if m:
                self.appendices.setdefault(int(m.group(1)), n)
            if b.is_nav(n):
                continue
            for el in b.elements(n):
                ln = _local(el.tag)
                eid = el.get("id")
                if (attr(el, "epub_type") and has_token(attr(el, "epub_type"), "pagebreak")) or \
                        el.get("role") == "doc-pagebreak":
                    label = (el.get("aria-label") or el.get("title") or "".join(el.itertext())).strip()
                    if eid and label:
                        key = label.lower()
                        self.page_dups[(n, key)] += 1
                        self.pages.setdefault(key, (n, eid))
                    continue
                if not eid:
                    continue
                text = " ".join("".join(el.itertext()).split())
                if ln in ("figure", "table", "div", "aside", "section", "p") or ln.startswith("h"):
                    cap = self._caption(el)
                    for kind, key in _labels_in(cap, ln):
                        self.labels[(kind, key)].append((n, eid))
                lid = eid.lower()
                m = re.match(r"^(fig|f|tab|tbl|t|eqn?|eq|sec|box)[-_.]?0*(\d+(?:[-_.]\d+)*)$", lid)
                if m:
                    kind = {"fig": "figure", "f": "figure", "tab": "table", "tbl": "table", "t": "table",
                            "eq": "equation", "eqn": "equation", "sec": "section", "box": "box"}[m.group(1)]
                    key = re.sub(r"[-_]", ".", m.group(2))
                    if (n, eid) not in self.labels[(kind, key)]:
                        self.labels[(kind, key)].append((n, eid))
                if ln in ("li", "p", "div") and re.match(r"^(ref|bib|r|cit|biblio)[-_]?0*(\d+)$", lid):
                    num = re.match(r"^(?:ref|bib|r|cit|biblio)[-_]?0*(\d+)$", lid).group(1)
                    self.refs[num].append((n, eid))
                elif ln in ("li", "p") and re.match(r"^\[?(\d+)[\].]\s", text):
                    num = re.match(r"^\[?(\d+)[\].]\s", text).group(1)
                    if any(_local(a.tag) in ("section", "ol", "ul", "div") and
                           re.search(r"bibliograph|references|reflist", (attr(a, "epub_type") or "") +
                                     (a.get("class") or "") + (a.get("role") or ""), re.I)
                           for a in el.iterancestors()):
                        self.refs[num].append((n, eid))
            if b.is_index_doc(n):
                self._index_entries(n)

    @staticmethod
    def _caption(el):
        for c in el.iter():
            if _local(c.tag) in ("figcaption", "caption", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6"):
                t = " ".join("".join(c.itertext()).split())
                if t:
                    return t[:80]
        return " ".join("".join(el.itertext()).split())[:80]

    def _index_entries(self, n):
        """Head word of every index entry (li / p), with its id when it has one."""
        raw = self.book.raw(n)
        for m in re.finditer(r"<(li|p)\b([^>]*)>", raw):
            a = R.attrs_of(m.group(2))
            # the entry's own text up to the first locator / sub-list / see
            rest = raw[m.end(): m.end() + 600]
            stop = re.search(r"</?(?:ul|ol|li|p)\b|<a\b|<i>\s*see\b", rest, re.I)
            head = R.decode(re.sub(r"<[^>]+>", "", rest[: stop.start() if stop else len(rest)]))[0]
            head = re.split(r",\s*(?:\d|[ivxlc]+\b)|\s\d", head)[0]
            key = norm_entry(head)
            if key:
                self.index_entries[key].append((n, a.get("id"), m.start(), m.end()))

    # ------------------------------------------------------------ lookups
    def page(self, label):
        return self.pages.get(str(label).strip().lower())

    def unique(self, kind, key):
        v = self.labels.get((kind, key)) or []
        v = list(dict.fromkeys(v))
        return v[0] if len(v) == 1 else None


def norm_entry(s):
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return " ".join(s.split())


_LABEL_RE = re.compile(r"^\s*(Figure|Fig\.?|Table|Tab\.?|Equation|Eq\.?|Section|Sec\.?|Box|Appendix|Map|Plate)\s+"
                       r"([A-Z]?\d+(?:[.\-]\d+)*[a-z]?|[A-Z])\b", re.I)
_KIND = {"figure": "figure", "fig": "figure", "map": "figure", "plate": "figure", "table": "table", "tab": "table",
         "equation": "equation", "eq": "equation", "section": "section", "sec": "section", "box": "box",
         "appendix": "appendix"}


def _labels_in(caption, ln):
    m = _LABEL_RE.match(caption or "")
    if not m:
        return []
    kind = _KIND[m.group(1).lower().rstrip(".")]
    if ln.startswith("h") and kind not in ("section", "appendix"):
        return []
    return [(kind, m.group(2).replace("-", ".").upper() if kind == "appendix" else m.group(2).replace("-", "."))]


def roman_to_int(s):
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(c not in vals for c in s):
        return 0
    total = 0
    for i, c in enumerate(s):
        v = vals[c]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total


# ======================================================== link matches
@dataclass
class LinkRef:
    """One EPUB-009 match in a text segment."""
    seg: object
    i: int                      # decoded start
    j: int                      # decoded end
    text: str
    kind: str                   # chapter|part|figure|table|section|equation|box|appendix|url|citation
    key: str


def link_matches(book: Book, name: str):
    """EPUB-009 exactly as the tool reports it."""
    t = book.targets
    out = []
    raw = book.raw(name)
    has_body = "<body" in raw
    segs = book.segs(name) if has_body else []
    for seg in segs:
        if seg.inside("a", "span"):
            continue
        parent = seg.parent
        if parent is not None and re.match(r"^(title|h[1-9]+)$", parent.name):
            continue
        txt = R.escape_text(seg.text)            # XText.ToString() escapes &, <, >
        for m in LINK_RE.finditer(txt):
            word = m.group(1) or ""
            if re.search(r"Tabs?(les?|\.|)", word) and any(
                    n.name == "figure" and n.get("class").lower() == "table" for n in seg.stack):
                continue
            if re.search(r"Figs?(ures?|\.|)", word) and any(
                    n.name == "figure" and "class" not in n.attrs for n in seg.stack):
                continue
            value = m.group(0)
            if re.match(r"^parts? \(", value.lower()):
                continue
            num = m.group(5) or ""
            if num and not num.isdigit() and len(num) > 1 and roman_to_int(num) == 0:
                continue                         # "Part of", "Section in": not a number
            kind, key = _classify(word, num, value)
            if kind == "chapter" and re.match(r"chapters?\.?$", word, re.I):
                if int(key or 0) not in t.chapters:
                    continue
            elif kind == "part":
                if int(key or 0) not in t.parts:
                    continue
            i, j = _unescaped_span(seg.text, txt, m.start(), m.end())
            out.append(LinkRef(seg, i, j, seg.text[i:j], kind, key))
    return out


def _classify(word, num, value):
    w = (word or "").lower()
    if w.startswith("chapter") or re.match(r"^chs?\.", value.lower()):
        n = num or re.search(r"\d+", value).group(0)
        return "chapter", n if n.isdigit() else str(roman_to_int(n))
    if w == "part":
        n = num if num.isdigit() else str(roman_to_int(num))
        return "part", n
    if w.startswith("fig"):
        return "figure", num
    if w.startswith("tab"):
        return "table", num
    if w.startswith("sec"):
        return "section", num
    if w.startswith("eq"):
        return "equation", num
    if w == "box":
        return "box", num
    if w.startswith("append"):
        return "appendix", num.upper()
    if value.startswith("["):
        return "citation", value.strip("[]")
    return "url", value


def _unescaped_span(text, esc, a, b):
    """Map offsets in R.escape_text(text) back to offsets in text."""
    starts, pos = [], 0
    for ch in text:
        starts.append(pos)
        pos += len(R.escape_text(ch))
    starts.append(pos)
    i = next((k for k, p in enumerate(starts) if p >= a), len(text))
    j = next((k for k, p in enumerate(starts) if p >= b), len(text))
    return i, j


# ===================================================== index matches
@dataclass
class IndexToken:
    code: str                    # EPUB-043 | EPUB-046 | EPUB-047
    seg: object
    i: int
    j: int


def index_findings(book: Book, name: str):
    """(code, line, col) of EPUB-043 / 044 / 045 / 046 / 047 in one document."""
    raw = book.raw(name)
    out = []
    for seg in book.segs(name):
        if seg.inside("a"):
            continue
        s = R.escape_text(seg.text)
        for m in INDEX_NUM_RE.finditer(s):
            if re.match(r" |\d|;", m.group(1)):
                out.append(("EPUB-043", seg.start + m.start() + 2))
        for m in INDEX_RANGE_RE.finditer(s):
            out.append(("EPUB-043", seg.start + m.start(3) + 1))
        for m in INDEX_DASH_RE.finditer(s):
            out.append(("EPUB-043", seg.start + m.start(1) + 1))
        for m in INDEX_NOTE_RE.finditer(s):
            out.append(("EPUB-043", seg.start + m.start(3) + 1))
        # EPUB-046: roman numerals in li class="index"
        if any(n.name == "li" and n.get("class").lower() == "index" for n in seg.stack):
            for m in ROMAN_RE.finditer(s):
                if not m.group(1):
                    out.append(("EPUB-046", seg.start + m.start() + 1))
        # EPUB-047: text right after <i>see</i>
        prev = seg.prev
        if seg.text.strip() and isinstance(prev, R.Node) and prev.name == "i" and \
                SEE_RE.match(prev.text.strip().lower()):
            out.append(("EPUB-047", seg.start))
        # EPUB-044: a dash between two elements
        nxt = seg.next
        if isinstance(prev, R.Node) and isinstance(nxt, R.Node) and INDEX_DASH_ONLY_RE.match(seg.text.strip()):
            bad = prev.name != "a" or nxt.name != "a" or "href" not in prev.attrs or "href" not in nxt.attrs
            if not bad:
                m1, m2 = PAGE_HREF_RE.search(prev.get("href")), PAGE_HREF_RE.search(nxt.get("href"))
                t1 = prev.text.strip()
                t2 = _anchor_text(raw, nxt)
                if m1 and m2 and re.match(r"^\d+$", t1) and re.match(r"^\d+$", t2):
                    full = expand_range_end(t1, t2)
                    bad = m2.group(1) != full
            if bad:
                out.append(("EPUB-044", seg.start))
    if not book.is_nav(name):
        for seg_a in _anchors(raw):
            if INDEX_A_RANGE_RE.search(seg_a[1]):
                out.append(("EPUB-045", seg_a[0]))
    return [(c, *R.line_col(raw, off)) for c, off in out]


def _anchor_text(raw, node):
    m = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S).match(raw, node.start)
    return R.decode(re.sub(r"<[^>]+>", "", m.group(1)))[0].strip() if m else ""


def _anchors(raw):
    for m in re.finditer(r"<a\b[^>]*>(.*?)</a>", raw, re.S):
        yield m.start(), R.decode(re.sub(r"<[^>]+>", "", m.group(1)))[0]


def expand_range_end(start, end):
    """"123-25" -> "125" (an abbreviated range end takes the leading digits)."""
    if len(end) < len(start):
        return start[: len(start) - len(end)] + end
    return end


# ============================================================ validate
def validate(files: dict, epub_name: str = "") -> ClientReport:
    book = Book(files, epub_name)
    rep = ClientReport(epub_name=epub_name)

    def add(code, file, line, col, message=None, fix=""):
        sev, desc = D.RULES.get(code, ("Warning", ""))
        rep.findings.append(ClientFinding(code, sev, posixpath.basename(file) if file else "", line, col,
                                          message or desc, fix))

    opf = book.opf_text()
    stem = os.path.splitext(os.path.basename(epub_name or ""))[0]
    # 001 / 002 file name
    isbn = book.isbn()
    if isbn and stem and not re.match("^" + re.escape(isbn) + "$", stem):
        add("EPUB-001", "", 1, 1, fix="delivery copy named <ISBN>.epub")
    if stem and not re.match(r"^\d{13}$", stem):
        add("EPUB-002", "", 1, 1, fix="delivery copy named <ISBN>.epub")
    # 003 unused images
    used = set()
    for n in book.xhtml:
        for m in re.finditer(r"<img[^>]+src=\"[^\"]+/([^\"]+?)\"[^>]*?/>", book.raw(n)):
            used.add(m.group(1).lower())
    unused = [posixpath.basename(i) for i in book.images() if posixpath.basename(i).lower() not in used]
    if unused:
        add("EPUB-003", "", 1, 1, D.RULES["EPUB-003"][1] + " " + ", ".join(unused))
    # 004 spaces
    for n in book.xhtml + book.images():
        if " " in posixpath.basename(n):
            add("EPUB-004", n, 1, 1, D.RULES["EPUB-004"][1].replace("{image/xhtml}", posixpath.basename(n)))
    # 005 mimetype
    mt = files.get("mimetype")
    if mt is None:
        add("EPUB-005", "", 1, 1, "mimetype file not found in epub package.")
    elif re.search(r"\napplication/epub\+zip", mt.decode("latin-1")):
        add("EPUB-005", "mimetype", 1, 1, fix="mimetype content")
    # 006 META-INF
    for n in book.names:
        if n.startswith("META-INF/") and not re.search(r"container\.xml$", n):
            add("EPUB-006", n, 1, 1, D.RULES["EPUB-006"][1].replace("{file}", posixpath.basename(n)),
                fix="unwanted META-INF file" if _removable_meta(n) else "")
    # 007 fonts
    if any(re.search(r"(^|/)font[^/]*/", n, re.I) for n in book.names):
        add("EPUB-007", "", 1, 1)
    for n in book.xhtml:
        raw = book.raw(n)
        base = posixpath.basename(n)
        # 008 empty anchors / 051 dummy anchors
        for m in re.finditer(r"<a\b([^>]*?)(/>|>(.*?)</a>)", raw, re.S):
            inner = re.sub(r"<[^>]+>", "", m.group(3) or "")
            if not R.decode(inner)[0].strip() and "<img" not in (m.group(3) or ""):
                add("EPUB-008", n, *R.line_col(raw, m.start()), D.RULES["EPUB-008"][1] + ": " + base)
        # 009
        for ref in link_matches(book, n):
            line, col = R.line_col(raw, ref.seg.offsets[ref.i])
            add("EPUB-009", n, line, col, D.RULES["EPUB-009"][1].replace("{file}", base).replace("{text}", ref.text),
                fix="link / span")
        # 010 junk
        for off in junk_offsets(files[n]):
            add("EPUB-010", n, *R.line_col(raw, min(off, len(raw))), D.RULES["EPUB-010"][1].replace("{XHTML}", base),
                fix="characters as numeric references")
        # 012 / 013
        for m in re.finditer(r"<(m(ml)?:)?math>", raw):
            add("EPUB-012", n, *R.line_col(raw, m.start()), D.RULES["EPUB-012"][1].replace("{XHTML}", base))
        for el in book.elements(n, "img"):
            if el.get("alt") is None:
                add("EPUB-013", n, el.sourceline or 1, 1, D.RULES["EPUB-013"][1].replace("{XHTML}", base))
        # 015 page break missing
        if not re.search(r"(^nav|\.*_cv).xhtml", base.lower()):
            if not any(has_token(attr(e, "epub_type"), "pagebreak") and e.get("role") == "doc-pagebreak"
                       for e in book.elements(n, "span")):
                add("EPUB-015", n, 1, 1, D.RULES["EPUB-015"][1].replace("{XHTML}", base))
        # 016 naming
        if not re.match(r"^nav\.xhtml", base.lower()) and not XHTML_NAME_RE.match(base.lower()):
            add("EPUB-016", n, 1, 1, D.RULES["EPUB-016"][1].replace("{XHTML}", base))
    # 011 entity format
    for n in book.names:
        if n.lower().endswith((".xhtml", ".ncx", ".opf")):
            raw = book.raw(n)
            for m in ENTITY_FORMAT_RE.finditer(raw):
                add("EPUB-011", n, *R.line_col(raw, m.start()),
                    D.RULES["EPUB-011"][1].replace("{file}", posixpath.basename(n)), fix="double-escaped reference")
    # 014 identifiers
    if book.opf:
        if not re.search(r'<dc:identifier id="isbn-id">urn:isbn:([^<]+?)</dc:identifier>', opf):
            add("EPUB-014", book.opf, 1, 1, "The ISBN identifier must be <dc:identifier id=\"isbn-id\">urn:isbn:...",
                fix="OPF identifiers")
        if not re.search(r'<meta[^>]+refines="#src-id"[^>]+>15</', opf) or \
                not re.search(r'<meta[^>]+refines="#src-id"[^>]+>pagination</', opf):
            add("EPUB-014", book.opf, 1, 1, fix="OPF identifiers")
    else:
        add("EPUB-014", "", 1, 1, "opf file not found in epub package.")
    # 017 sequence
    _sequence(book, add)
    # 018 image naming, 019 DPI, 020 pixels, 021 cover size, 048 / 050
    _images(book, add)
    # 023 cover property
    if book.opf:
        if not re.search(r"<meta\b(?=[^>]*\bname=\"cover\")(?=[^>]*\bcontent=\"cover-image\")[^>]*>", opf):
            add("EPUB-023", book.opf, 1, 1, D.RULES["EPUB-023"][1].replace("{element}", "metadata"),
                fix="cover image property")
        if not re.search(r"<item\b(?=[^>]*\bid=\"cover-image\")(?=[^>]*\bproperties=\"cover-image\")[^>]*>", opf):
            add("EPUB-023", book.opf, 1, 1, D.RULES["EPUB-023"][1].replace("{element}", "manifest"),
                fix="cover image property")
    # 024 epub:type / role
    for n in book.xhtml:
        for el in book.elements(n):
            et, role = attr(el, "epub_type"), el.get("role")
            if _in_landmarks(el):
                continue
            for typ, want in D.EPUB_TYPE_ROLES.items():
                if typ in ("endnote", "biblioentry"):
                    continue
                if et == typ and role != want:
                    add("EPUB-024", n, el.sourceline or 1, 1, D.RULES["EPUB-024"][1].replace("{XHTML}",
                                                                                          posixpath.basename(n)),
                        fix="epub:type / role pair")
                elif role == want and et != typ:
                    add("EPUB-024", n, el.sourceline or 1, 1, D.RULES["EPUB-024"][1].replace("{XHTML}",
                                                                                          posixpath.basename(n)),
                        fix="epub:type / role pair")
    # 025 title, 026 repeated pages, 027 tables, 029 / 032 page breaks
    for n in book.xhtml:
        _title_rule(book, n, add)
        seen = set()
        for el in book.elements(n, "span"):
            if attr(el, "epub_type") == "pagebreak" and el.get("aria-label") is not None:
                label = el.get("aria-label")
                if label in seen:
                    add("EPUB-026", n, el.sourceline or 1, 1, D.RULES["EPUB-026"][1].replace("{XHTML}",
                                                                                          posixpath.basename(n)))
                seen.add(label)
                if el.get("id") is not None and not re.search("_" + re.escape(label) + "$", el.get("id")):
                    add("EPUB-032", n, el.sourceline or 1, 1)
            if has_token(attr(el, "epub_type"), "pagebreak") and not (
                    el.get("role") == "doc-pagebreak" and el.get("id") and el.get("aria-label")):
                add("EPUB-029", n, el.sourceline or 1, 1, D.RULES["EPUB-029"][1].replace("{XHTML}",
                                                                                      posixpath.basename(n)))
        for el in book.elements(n, "table"):
            add("EPUB-027", n, el.sourceline or 1, 1, D.RULES["EPUB-027"][1] + " " + posixpath.basename(n))
    # 028 CSS version
    css = [n for n in book.names if n.lower().endswith(".css")]
    if css:
        m = re.search(r"Version (\d+\.\d+)\b", book.raw(css[0]))
        if not m or m.group(1) != "1.0":
            add("EPUB-028", css[0], 1, 1)
    # 031 page list
    _page_list(book, add)
    # 033 html
    if any(n.lower().endswith(".html") for n in book.names):
        add("EPUB-033", "", 1, 1)
    # 035 / 037 guide and landmarks
    _landmarks(book, add)
    # 036 cover page number
    for n in book.xhtml:
        if n.lower().endswith("_cv.xhtml") and any(attr(e, "epub_type") == "pagebreak"
                                                    for e in book.elements(n, "span")):
            add("EPUB-036", n, 1, 1)
    # 038 linear
    for m in re.finditer(r"<itemref\b[^>]*\blinear=\"no\"", opf):
        add("EPUB-038", book.opf, *R.line_col(opf, m.start()), fix="spine linear")
    # 040 duplicate image use
    for n in book.xhtml:
        srcs = [(e.get("src") or "").split("/")[-1].lower().strip() for e in book.elements(n, "img")]
        for s, c in Counter(srcs).items():
            if s and c > 1:
                add("EPUB-040", n, 1, 1, D.RULES["EPUB-040"][1] + " " + posixpath.basename(n))
    # 041 author
    _author_rule(book, add)
    # 042 link not cited correctly ("Table <a>3</a>")
    for n in book.xhtml:
        raw = book.raw(n)
        for m in re.finditer(r"<a\b", raw):
            before = R.decode(re.sub(r".*>", "", raw[max(0, m.start() - 80): m.start()], flags=re.S))[0].strip()
            if LINK_CITED_RE.search(before):
                add("EPUB-042", n, *R.line_col(raw, m.start()), D.RULES["EPUB-042"][1] + " " + posixpath.basename(n),
                    fix="label inside the link")
    # 043 - 047 index
    for n in book.xhtml:
        if book.is_index_doc(n):
            for code, line, col in index_findings(book, n):
                add(code, n, line, col, D.RULES[code][1] + " " + posixpath.basename(n), fix="index links")
    # 049 merged text
    for n in book.xhtml:
        raw = book.raw(n)
        for m in MERGED_RE.finditer(raw):
            add("EPUB-049", n, *R.line_col(raw, m.start()), D.RULES["EPUB-049"][1] + " " + posixpath.basename(n),
                fix="index links" if book.is_index_doc(n) else "")
    # 051 dummy a
    for n in book.xhtml:
        raw = book.raw(n)
        for m in re.finditer(r"<a(\s*/?>|\s*>\s*</a>)", raw):
            add("EPUB-051", n, *R.line_col(raw, m.start()))
    return rep


def validate_epub(path: str) -> ClientReport:
    with zipfile.ZipFile(path) as zf:
        files = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.filename.endswith("/")}
    return validate(files, os.path.basename(path))


# ============================================================ helpers
def utf7_view(data: bytes) -> str:
    """The file as .NET's UTF-7 decoder returns it: bytes >= 0x80 become the
    character with that code."""
    import codecs
    try:
        codecs.lookup_error("spix-passthrough")
    except LookupError:
        codecs.register_error("spix-passthrough",
                              lambda e: ("".join(chr(b) for b in e.object[e.start:e.end]), e.end))
    try:
        return data.decode("utf-7", errors="spix-passthrough")
    except Exception:  # noqa: BLE001
        return data.decode("latin-1")


def junk_offsets(data: bytes):
    """Offsets (in the UTF-8 text) the tool reports for EPUB-010."""
    text = data.decode("utf-8", errors="replace")
    out = [m.start() for m in JUNK_RE.finditer(text)]
    if any(b >= 0x80 for b in data):
        # map each UTF-7-view match back to the UTF-8 character it comes from
        v = utf7_view(data)
        if len(v) == len(data):
            byte_to_char = []
            for k, ch in enumerate(text):
                byte_to_char += [k] * len(ch.encode("utf-8"))
            for m in JUNK_RE.finditer(v):
                if m.start() < len(byte_to_char):
                    out.append(byte_to_char[m.start()])
        else:
            out += [m.start() for m in JUNK_RE.finditer(v)]
    return sorted(set(out))


def non_ascii(data: bytes):
    return any(b >= 0x80 for b in data)


def _removable_meta(n):
    base = posixpath.basename(n).lower()
    return base not in ("encryption.xml", "signatures.xml", "rights.xml", "manifest.xml", "metadata.xml")


def _in_landmarks(el):
    for a in el.iterancestors():
        if _local(a.tag) == "nav" and attr(a, "epub_type") == "landmarks":
            return True
    return False


def _sequence(book, add):
    numbered = sorted((posixpath.basename(n) for n in book.xhtml if re.match(r"^\d", posixpath.basename(n))),
                      key=lambda b: int(re.match(r"^\d+", b).group(0)))
    for k, b in enumerate(numbered):
        if int(re.match(r"^\d+", b).group(0)) != k + 1:
            add("EPUB-017", b, 1, 1, D.RULES["EPUB-017"][1] + ": " + b)
            break
    for pat in ("fm", "pt", "ch", "app", "bm"):
        nums = sorted(int(re.search("_" + pat + r"(\d+)\.xhtml", posixpath.basename(n)).group(1))
                      for n in book.xhtml if re.search("_" + pat + r"(\d+)\.xhtml", posixpath.basename(n)))
        for k, v in enumerate(nums):
            if v != k + 1:
                add("EPUB-017", "", 1, 1, D.RULES["EPUB-017"][1] + f": _{pat}{v}.xhtml")
                break


def image_info(data: bytes):
    """(width, height, dpi_x, dpi_y) or None when the image is unreadable."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            dpi = im.info.get("dpi") or (96, 96)
            if im.format == "JPEG" and "dpi" not in im.info and im.info.get("jfif_density"):
                dpi = im.info["jfif_density"]
            return im.size[0], im.size[1], int(round(float(dpi[0]))), int(round(float(dpi[1])))
    except Exception:  # noqa: BLE001
        return None


def expected_dpi(name):
    base = posixpath.basename(name).lower()
    if re.match(r"^cover\..*g$", base):
        return 300
    if "inline" in base or "icon" in base:
        return 135
    return 150


def cover_ok_019(w, h, dx, dy):
    return (dx == 300 and dy == 300) or (w == 1200 and h == 1800)


def cover_ok_021(w, h):
    return (h == 1800 or w == 1200) and h <= 1800 and w <= 1200


def _images(book, add):
    for n in book.images():
        base = posixpath.basename(n)
        if not any(r.match(base) for r in IMAGE_NAME_RES):
            add("EPUB-018", n, 1, 1, D.RULES["EPUB-018"][1] + " " + base)
        ext = posixpath.splitext(base)[1].lower()
        if re.search(r"(cover|logo)", base) and ext != ".jpg" or not re.search(r"(cover|logo)", base) and ext != ".png":
            add("EPUB-048", n, 1, 1, D.RULES["EPUB-048"][1] + " " + base)
        info = image_info(book.files[n])
        if info is None:
            add("EPUB-050", n, 1, 1, D.RULES["EPUB-050"][1] + " " + base)
            continue
        w, h, dx, dy = info
        want = expected_dpi(n)
        cover = re.match(r"^cover\..*g$", base.lower()) is not None
        if cover:
            if not cover_ok_019(w, h, dx, dy):
                add("EPUB-019", n, 1, 1, "The cover image should be 300 dpi or 1800 (H) * 1200 (W), please check "
                                         f"and update it. {base} (Found {dx}x{dy} DPI, {h}(H) x {w}(W))",
                    fix="image DPI")
            if not cover_ok_021(w, h):
                add("EPUB-021", n, 1, 1, D.RULES["EPUB-021"][1] + f" (Found {h}(H) x {w}(W); expected height 1800 "
                                                                  "or width 1200, without exceeding 1800(H) x 1200(W))",
                    fix="cover size")
        elif (dx, dy) != (want, want):
            add("EPUB-019", n, 1, 1, f"The image should be {want} dpi, please check and update it. {base} "
                                     f"(Expected {want} DPI, Found {dx}x{dy})", fix="image DPI")
        if w * h > MAX_PIXELS:
            add("EPUB-020", n, 1, 1, D.RULES["EPUB-020"][1].replace("{image}", base))


def _title_rule(book, n, add):
    root = book.root(n)
    if root is None:
        return
    titles = [e for e in book.elements(n, "title") if _local(e.getparent().tag) == "head"]
    if not titles:
        return
    title = " ".join("".join(titles[0].itertext()).split())
    if FRONT_TITLES.match(title) and any(FRONT_TYPES.match(attr(e, "epub_type") or "") for e in book.elements(n)):
        return
    want = expected_title(book, n)
    if want and not title_matches(title, want):
        add("EPUB-025", n, titles[0].sourceline or 1, 1, D.RULES["EPUB-025"][1] + " " + posixpath.basename(n),
            fix="document title")


def title_matches(title, want):
    num, _sep, rest = want.partition("\x00")
    if rest:
        return re.match("^" + re.escape(num) + " ?" + re.escape(rest) + "$", title) is not None
    return " ".join(want.split()) == title


def expected_title(book, n):
    """The <title> the heading asks for: "<number>\x00<title>" for a
    Chapter-Number + Chapter-Title header, else the classed h1 text."""
    num = title = ""
    for e in book.elements(n, "h1"):
        p = e.getparent()
        if p is None or _local(p.tag) != "header":
            continue
        cls = e.get("class") or ""
        t = " ".join("".join(e.itertext()).split())
        if cls == "Chapter-Number" and not num:
            num = t
        elif cls == "Chapter-Title" and not title:
            title = t
    if title:
        return f"{num}\x00{title}" if num else title
    for e in book.elements(n, "h1"):
        p = e.getparent()
        if p is not None and _local(p.tag) == "header" and H1_CLASSES.match(e.get("class") or ""):
            return " ".join("".join(e.itertext()).split())
    return ""


def _page_list(book, add):
    nav = next((n for n in book.xhtml if book.is_nav(n)), None)
    ncx = next((n for n in book.names if posixpath.basename(n).lower() == "toc.ncx"), None)
    nav_pages = set()
    if nav:
        for e in book.elements(nav, "nav"):
            if e.get("class") == "pageList" or attr(e, "epub_type") == "page-list" or e.get("role") == "doc-pagelist":
                for a in e.iter():
                    if _local(a.tag) == "a" and a.get("href"):
                        nav_pages.add(" ".join("".join(a.itertext()).split()))
                        m = PAGE_HREF_RE.search(a.get("href"))
                        if m:
                            nav_pages.add(m.group(1))
    ncx_pages = set()
    if ncx:
        for e in book.elements(ncx, "pageTarget"):
            ncx_pages.add(e.get("value") or "")
            ncx_pages.add(" ".join("".join(e.itertext()).split()))
    for n in book.xhtml:
        for el in book.elements(n, "span"):
            if attr(el, "epub_type") == "pagebreak" and el.get("aria-label") is not None:
                label = el.get("aria-label").strip()
                miss_nav = nav is not None and label not in nav_pages
                miss_ncx = ncx is not None and label not in ncx_pages
                if miss_nav or miss_ncx:
                    where = "nav/ncx" if miss_nav and miss_ncx else ("nav" if miss_nav else "ncx")
                    add("EPUB-031", n, el.sourceline or 1, 1,
                        D.RULES["EPUB-031"][1].replace("{page}", label).replace("{nav/ncx}", where) + " " +
                        posixpath.basename(n))


def _landmarks(book, add):
    nav = next((n for n in book.xhtml if book.is_nav(n)), None)
    if nav:
        lm = [e for e in book.elements(nav, "nav") if attr(e, "epub_type") == "landmarks"]
        for e in lm:
            line = e.sourceline or 1
            h2 = [c for c in e if _local(c.tag) == "h2" and c.get("id") == "landmarks"]
            if not h2 or not any(" ".join("".join(h.itertext()).split()) == "Book Landmarks" for h in h2):
                add("EPUB-037", nav, line, 1, "Navigation file landmarks nav should contain h2 id='landmarks' with "
                                              "text 'Book Landmarks'.", fix="landmarks")
            if not any(_local(c.tag) == "ol" and c.get("class") == "none" for c in e):
                add("EPUB-037", nav, line, 1, "Navigation file landmarks nav should contain ol class='none'.",
                    fix="landmarks")
            for typ, label in NAV_LANDMARKS:
                if not any(_local(a.tag) == "a" and attr(a, "epub_type") == typ and
                           "".join(a.itertext()).strip() == label for a in e.iter()):
                    add("EPUB-035", nav, line, 1, D.RULES["EPUB-035"][1].replace("{entry}", label).replace(
                        "{file}", "Navigation file"), fix="landmarks")
        navs = {attr(e, "epub_type") for e in book.elements(nav, "nav")}
        for typ, label in (("toc", "TOC"), ("landmarks", "Guide"), ("page-list", "Page List")):
            if typ not in navs:
                add("EPUB-037", nav, 1, 1, D.RULES["EPUB-037"][1].replace("{entry}", label),
                    fix="landmarks" if typ == "landmarks" else "")
        for e in book.elements(nav, "nav"):
            if attr(e, "epub_type") and not re.match(r"^(toc|landmarks|page-list)$", attr(e, "epub_type")):
                add("EPUB-037", nav, e.sourceline or 1, 1, "Navigation file contains unwanted data.")
    if book.opf:
        opf = book.opf_text()
        if "<guide" in opf:
            line = R.line_col(opf, opf.find("<guide"))[0]
            for typ, title in OPF_GUIDE:
                if not re.search(r"<reference\b(?=[^>]*\btype=\"%s\")(?=[^>]*\btitle=\"%s\")[^>]*>" % (
                        re.escape(typ), re.escape(title)), opf):
                    add("EPUB-035", book.opf, line, 6, D.RULES["EPUB-035"][1].replace("{entry}", title).replace(
                        "{file}", "OPF file"), fix="OPF guide")
        else:
            add("EPUB-035", book.opf, 1, 1, D.RULES["EPUB-035"][1].replace("{entry}", "Guide").replace(
                "{file}", "OPF file"), fix="OPF guide")


def _author_rule(book, add):
    if not book.opf:
        return
    creators = [" ".join((e.text or "").split()) for e in (book.elements(book.opf, "creator"))]
    for n in book.xhtml:
        if not any(attr(e, "epub_type") == "titlepage" and _local(e.tag) == "section" for e in book.elements(n)):
            continue
        authors = [e for e in book.elements(n, "p") if e.get("class") == "bookauthor"]
        if not authors:
            add("EPUB-041", n, 1, 1, 'class="bookauthor" is not available in title page.')
            continue
        for a in authors:
            text = "".join(a.itertext())
            if not any(re.match("^" + re.escape(text) + "$", c) or c == text for c in creators):
                add("EPUB-041", n, a.sourceline or 1, 1, D.RULES["EPUB-041"][1] + " " + posixpath.basename(n),
                    fix="author metadata")


# ===================================================== manual actions
MANUAL = {
    "EPUB-001": "Deliver the package as <ISBN>.epub - a correctly named copy is written to client_delivery/.",
    "EPUB-002": "Deliver the package as <ISBN>.epub (13 digits) - a correctly named copy is written to "
                "client_delivery/.",
    "EPUB-003": "Remove the unused image or reference it where it belongs.",
    "EPUB-004": "Rename the file without spaces (and update every reference).",
    "EPUB-007": "Confirm the font licence allows distribution.",
    "EPUB-008": "Check the empty anchor: keep it only when it is a link target.",
    "EPUB-009": "No single valid target exists - the text is marked with <span>; add the link by hand if the "
                "target exists under another name.",
    "EPUB-012": "Convert the MathML equation as the client requires (image / text).",
    "EPUB-013": "Write the alt text for the image (accessibility content is never invented).",
    "EPUB-014": "Add the print ISBN: <dc:source id=\"src-id\">urn:isbn:PRINT</dc:source> with identifier-type 15 "
                "and source-of pagination.",
    "EPUB-015": "Add the print page marker for this document (check the PDF).",
    "EPUB-016": "Rename the XHTML file to NN_ISBN5_xxN.xhtml (and update every reference).",
    "EPUB-017": "Renumber the XHTML file sequence (and update every reference).",
    "EPUB-018": "Rename the image to the client's pattern (ch1-fig-01.png, cover.jpg ...) and update references.",
    "EPUB-019": "The DPI could not be set without touching the pixels - export the image again at the required DPI.",
    "EPUB-020": "Reduce the image below 4.2 million pixels at the source.",
    "EPUB-021": "The cover could not be resized automatically - supply a cover 1200 px wide or 1800 px high "
                "(never larger).",
    "EPUB-022": "Check the package size against the client's limit.",
    "EPUB-023": "Mark the cover image: item id=\"cover-image\" properties=\"cover-image\" and "
                "<meta name=\"cover\" content=\"cover-image\"/>.",
    "EPUB-024": "Add the role that matches the epub:type. role=\"doc-cover\" is only allowed on <img> (EPUBCheck "
                "RSC-005), so a cover <section> needs the client's decision.",
    "EPUB-025": "Make <title> match the chapter heading.",
    "EPUB-026": "Remove the repeated page marker after checking the PDF page.",
    "EPUB-027": "Check whether the table must be delivered as an image.",
    "EPUB-028": "Use the client's CSS template (Version 1.0).",
    "EPUB-029": "Give the page marker epub:type=\"pagebreak\", role=\"doc-pagebreak\", id and aria-label.",
    "EPUB-031": "Add the page to the nav page-list / NCX pageList.",
    "EPUB-032": "The page marker id must end with _<page label> (e.g. Page_12).",
    "EPUB-033": "Convert the .html file to .xhtml.",
    "EPUB-036": "Remove the page number from the cover page.",
    "EPUB-039": "Move the figure / table after its first callout.",
    "EPUB-040": "Check the image used twice in the same file.",
    "EPUB-041": "Make dc:creator match the author printed on the title page.",
    "EPUB-043": "No page marker exists for this locator (marked with <span>) - check it against the print index.",
    "EPUB-044": "Link both ends of the range to their own pages.",
    "EPUB-046": "No page exists for this roman numeral (marked with <span>) - check it against the print index.",
    "EPUB-047": "No index entry with this name exists (marked with <span>) - check the cross-reference.",
    "EPUB-048": "Use .jpg for cover / logo images and .png for all others.",
    "EPUB-049": "Move the text that follows the link inside it (or separate it).",
    "EPUB-050": "Replace the corrupted image.",
    "EPUB-051": "Remove the dummy <a> element.",
}


def manual_action(code):
    return MANUAL.get(code, D.RULES.get(code, ("", "Check against the client's rules."))[1])
