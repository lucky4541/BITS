"""Book metadata read from the book's own TITLE PAGE and COPYRIGHT PAGE -
the source the client's OPF format is filled from:

    title page      <h1 class="booktitle">, <p class="booksubtitle">,
                    <p class="bookauthor"> (CUP classes), else the first
                    heading / a "by ..." line
    copyright page  "(c) University of Toronto Press Incorporated 2006"
                    -> dc:rights, the year (dc:date), the publisher (the
                    rights holder without "Incorporated"/"Inc."/"Ltd."),
                    and every "ISBN 978-0-8020-9154-3 (cloth)" with its
                    qualifier: the e-book ISBN becomes the identifier, the
                    print ISBN the dc:source whose pagination the page
                    list follows.

Nothing is guessed: a value that is not on the pages stays empty."""
import re
from dataclasses import dataclass, field

from core.epub_structure.isbn import validate_isbn

EPUB_NS = "http://www.idpf.org/2007/ops"
EBOOK_QUALIFIERS = ("epub", "e-pub", "ebook", "e-book", "electronic", "ebk", "pdf", "online", "digital")
PRINT_QUALIFIERS = ("cloth", "hardcover", "hardback", "hbk", "paper", "paperback", "pbk", "print", "softcover")
_ISBN_RE = re.compile(r"\bISBN(?:-1[03])?[:\s]*((?:97[89][\s\-]?)?\d[\d\s\-]{7,15}[\dXx])(?:\s*\(([^)]{1,40})\))?"
                      r"(?:\s*[-:,]?\s*\b([A-Za-z\-]{3,12})\b)?", re.I)
_CORP_SUFFIX_RE = re.compile(r",?\s+(Incorporated|Inc\.?|Ltd\.?|Limited|LLC|Corporation|Corp\.?|plc)$", re.I)
_BY_RE = re.compile(r"^\s*(?:by|edited by|written by)\s+(.+)$", re.I)


@dataclass
class Isbn:
    isbn13: str
    qualifier: str = ""
    raw: str = ""

    @property
    def kind(self):
        q = self.qualifier.lower()
        if any(k in q for k in EBOOK_QUALIFIERS):
            return "ebook"
        if any(k in q for k in PRINT_QUALIFIERS):
            return "print"
        return ""


@dataclass
class FrontMatter:
    title: str = ""
    subtitle: str = ""
    authors: list = field(default_factory=list)
    publisher: str = ""
    rights: str = ""
    year: str = ""
    isbns: list = field(default_factory=list)
    title_page: str = ""
    copyright_page: str = ""

    def ebook_isbn(self, exclude=()):
        c = [i for i in self.isbns if i.kind == "ebook" and i.isbn13 not in exclude]
        c.sort(key=lambda i: next((k for k, q in enumerate(EBOOK_QUALIFIERS) if q in i.qualifier.lower()), 99))
        return c[0].isbn13 if c else ""

    def print_isbn(self, exclude=()):
        c = [i for i in self.isbns if i.kind == "print" and i.isbn13 not in exclude]
        if c:
            return c[0].isbn13
        other = [i for i in self.isbns if i.isbn13 not in exclude and i.kind != "ebook"]
        return other[0].isbn13 if len(other) == 1 else ""

    @property
    def date(self):
        return f"{self.year}-01-01T00:00:00Z" if self.year else ""


def to_isbn13(raw):
    v = validate_isbn(raw)
    if not v.valid:
        return ""
    if v.kind == "ISBN-13":
        return v.normalized
    core = "978" + v.normalized[:9]
    check = (10 - sum(int(d) * (1 if k % 2 == 0 else 3) for k, d in enumerate(core)) % 10) % 10
    return core + str(check)


def file_as(name):
    """"James I. Wimsatt" -> "Wimsatt, James I." (already inverted names and
    single words are kept)."""
    name = " ".join((name or "").split())
    if not name or "," in name or " " not in name:
        return name
    parts = name.split(" ")
    suffix = ""
    if re.fullmatch(r"(Jr\.?|Sr\.?|II|III|IV)", parts[-1]) and len(parts) > 2:
        suffix = ", " + parts.pop()
    return f"{parts[-1]}, {' '.join(parts[:-1])}{suffix}"


def split_authors(text):
    text = " ".join((text or "").split())
    text = _BY_RE.sub(r"\1", text)
    parts = re.split(r"\s*(?:,\s*and\s+|\band\b|&|;)\s*", text)
    return [p.strip(" ,") for p in parts if p.strip(" ,")]


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el):
    return " ".join("".join(el.itertext()).split())


def _lines(el):
    """The element's text split at <br/> (each line of a copyright block)."""
    out = [""]

    def walk(e):
        if e.text:
            out[-1] += e.text
        for c in e:
            if isinstance(c.tag, str):
                if _local(c.tag) == "br":
                    out.append("")
                else:
                    walk(c)
            if c.tail:
                out[-1] += c.tail
    walk(el)
    return [" ".join(x.split()) for x in out if x.strip()]


def _etype(el):
    return el.get(f"{{{EPUB_NS}}}type") or el.get("epub:type") or ""


def _classes(el):
    return (el.get("class") or "").lower().split()


def extract(docs) -> FrontMatter:
    """docs: [(name, lxml root)] in reading order."""
    fm = FrontMatter()
    title_doc = copy_doc = None
    for name, root in docs:
        if root is None:
            continue
        types = {t for e in root.iter() if isinstance(e.tag, str) for t in _etype(e).split()}
        text = _text(root)
        if title_doc is None and ("titlepage" in types or any(
                c in ("bookauthor", "booktitle") for e in root.iter() if isinstance(e.tag, str) for c in _classes(e))):
            title_doc = (name, root)
        if copy_doc is None and ("copyright-page" in types or (
                ("©" in text or "copyright" in text.lower()) and "isbn" in text.lower())):
            copy_doc = (name, root)
    if title_doc:
        fm.title_page = title_doc[0]
        _title_page(title_doc[1], fm)
    if copy_doc:
        fm.copyright_page = copy_doc[0]
        _copyright_page(copy_doc[1], fm)
    return fm


def _title_page(root, fm):
    body = next((e for e in root.iter() if _local(e.tag) == "body"), root)
    for e in body.iter():
        if not isinstance(e.tag, str):
            continue
        cls = _classes(e)
        if "booktitle" in cls and not fm.title:
            fm.title = _text(e)
        elif "booksubtitle" in cls and not fm.subtitle:
            fm.subtitle = _text(e)
        elif "bookauthor" in cls:
            fm.authors += [a for a in split_authors(_text(e)) if a not in fm.authors]
        elif "bookpublisher" in cls and not fm.publisher:
            fm.publisher = _text(e)
    if not fm.title:
        h = next((e for e in body.iter() if _local(e.tag) in ("h1", "h2") and _text(e)), None)
        if h is not None:
            fm.title = _text(h)
    if not fm.authors:
        for e in body.iter():
            if _local(e.tag) == "p":
                m = _BY_RE.match(_text(e))
                if m:
                    fm.authors = split_authors(m.group(1))
                    break


def _copyright_page(root, fm):
    body = next((e for e in root.iter() if _local(e.tag) == "body"), root)
    blocks = [line for e in body.iter() if _local(e.tag) in ("p", "div", "li", "span") and _text(e)
              and not any(_local(c.tag) in ("p", "div", "li") for c in e) for line in _lines(e)]
    for t in blocks:
        if not fm.rights:
            m = re.search(r"(?:Copyright\s*)?(©|\(c\)|Copyright)\s*(.+)", t, re.I)
            if m:
                rest = re.split(r"(?<=\d{4})[.;]|\s{2,}|\bAll rights reserved\b", m.group(2), flags=re.I)[0]
                rest = rest.strip(" .;,")
                if re.search(r"\b(1[5-9]|20)\d{2}\b", rest):
                    fm.rights = "© " + rest
                    years = re.findall(r"\b(?:1[5-9]|20)\d{2}\b", rest)
                    fm.year = years[-1] if years else ""
                    holder = re.sub(r"\b(?:1[5-9]|20)\d{2}\b", "", rest).strip(" ,.")
                    holder = re.sub(r"^(?:by\s+)?the\s+", "", holder, flags=re.I) if holder.lower().startswith(
                        "by ") else holder
                    if re.search(r"\b(Press|Publish|Publications|Books|University)\b", holder) and not fm.publisher:
                        fm.publisher = _CORP_SUFFIX_RE.sub("", holder).strip(" ,.")
        for m in _ISBN_RE.finditer(t):
            num = to_isbn13(m.group(1))
            if num and num not in [i.isbn13 for i in fm.isbns]:
                qual = (m.group(2) or m.group(3) or "").strip()
                fm.isbns.append(Isbn(num, qual, m.group(0)))
        if not fm.publisher:
            m = re.search(r"\bPublished by\s+(?:the\s+)?([A-Z][\w&.,' -]{2,80}?)(?:[.,;]|$| in )", t)
            if m:
                fm.publisher = _CORP_SUFFIX_RE.sub("", m.group(1)).strip(" ,.")
    if not fm.year:
        m = re.search(r"\b(?:first\s+published|published)\b[^.]{0,40}?\b((?:1[5-9]|20)\d{2})\b", " ".join(blocks),
                      re.I)
        if m:
            fm.year = m.group(1)
