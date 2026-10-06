"""Builds the final BITS 2.2 book / JATS 1.4 article from the XML generator's
output (core.xml_generator: a flat stream of sec / p / fig / table-wrap /
list / ref-list ... plus elements named after the BITS/JATS zone tags).

    BITS  <book dtd-version="2.2">
            <book-meta>      book-title-group, contrib-group, isbn, publisher,
                             edition, permissions ...      (metadata zones)
            <front-matter>   dedication / foreword / preface / ack / toc /
                             front-matter-part             (part_type headings)
            <book-body>      <book-part book-part-type="part|chapter|...">
                               <book-part-meta> title-group, contrib-group,
                                                abstract, kwd-group
                               <body> ... </body>
                               <back> fn-group (notes), ref-list </back>
            <book-back>      book-app-group, notes, glossary, ref-list, index

    JATS  <article dtd-version="1.4">
            <front>  journal-meta, article-meta (title-group, contrib-group,
                     aff, author-notes, history, permissions, abstract,
                     kwd-group)
            <body>   sec ...
            <back>   ack, app-group, notes, glossary, ref-list, fn-group

Text is never dropped: every element of the input ends up somewhere in the
output (content_signature() before / after is checked by the pipeline)."""
import re
from copy import deepcopy

from lxml import etree

from core import lang as _lang

from core.xml_generator import PART_TYPE_ATTR

XLINK_NS = "http://www.w3.org/1999/xlink"
MML_NS = "http://www.w3.org/1998/Math/MathML"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

FRONT_PART_TYPES = {"dedication", "foreword", "preface", "ack", "toc", "front-matter-part"}
BACK_PART_TYPES = {"appendix", "notes", "glossary", "bibliography", "index"}
BOOK_META_TAGS = ("book-title", "book-subtitle", "contrib", "aff", "series-title", "publisher-name",
                  "publisher-loc", "isbn", "copyright-statement", "edition")
ARTICLE_META_TAGS = ("article-title", "subtitle", "contrib", "aff", "corresp", "author-note", "abstract", "kwd",
                     "history", "copyright-statement", "article-doi",
                     # book metadata zones met in an article (a project switched from BITS)
                     "book-title", "book-subtitle", "isbn", "publisher-name", "publisher-loc", "chapter-contrib")
LIST_TYPES = {"number": "order", "numbered": "order", "decimal": "order", "upper-alpha": "alpha-upper",
              "lower-alpha": "alpha-lower", "upper-roman": "roman-upper", "lower-roman": "roman-lower"}
_NUM_LABEL_RE = re.compile(r"^\s*(\[?\d+[a-z]?\]?|[*†‡§]+)[.)]?\s+")
# "Chapter 1", "Capítulo 1.2", "Chapitre IV", "Kapitel 3", "Parte II", "Sección I" ... or a bare number
# "Chapter 3", "Capítulo 1.2", "Глава 3", "3. fejezet", "第3章", "제3장", "Appendix A" ... in any
# language (core.lang), or a bare leading number "1 Beginnings" / "IV The End"
_CHAPTER_LABEL_RE = re.compile(r"^\s*(?:(" + _lang.label_core("chapter", "part", "section", "appendix")
                               + r")[.:：]?\s*|((?-i:[\dIVXL]+)(?:\.\d+)*)[.:]?\s+)(?=\S)", re.I)
_ISBN_RE = re.compile(r"((?:97[89][\s\-]?)?\d[\d\s\-]{7,15}[\dXx])")


# ================================================================ helpers
def text_of(el):
    return " ".join("".join(el.itertext()).split())


def content_signature(root):
    """The document's words (in document order)."""
    return _lang.words(" ".join(root.itertext()))


def lost_words(before, after):
    """Words of `before` missing from `after` (as a multiset - metadata such
    as "James Wimsatt" -> <surname>Wimsatt</surname><given-names>James ...
    and notes moved to the back legitimately change the ORDER, never the
    words). Empty = nothing lost."""
    from collections import Counter
    c = Counter(before)
    c.subtract(Counter(after))
    return sorted(w for w, n in c.items() for _ in range(n) if n > 0)


def added_words(before, after):
    return lost_words(after, before)


INLINE_RENAMES = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline",
                  "s": "strike", "del": "strike", "small-caps": "sc", "superscript": "sup", "subscript": "sub"}
TITLE_TAGS = ("title", "book-title", "article-title", "subtitle")


def _move_content(src, dst):
    """Moves src's text and children (mixed content) to the end of dst."""
    if src.text:
        if len(dst):
            dst[-1].tail = (dst[-1].tail or "") + src.text
        else:
            dst.text = (dst.text or "") + src.text
    src.text = None
    for c in list(src):
        dst.append(c)


def _wrap(el, tag, **attrib):
    """el's own content wrapped in a new child <tag> - <fn>text</fn> becomes
    <fn><p>text</p></fn>."""
    new = etree.Element(tag, **attrib)
    _move_content(el, new)
    el.append(new)
    return new


def _el(tag, text=None, **attrib):
    e = etree.Element(tag, {k.replace("_", "-"): v for k, v in attrib.items() if v is not None})
    if text is not None:
        e.text = text
    return e


def _strip_leading(el, regex):
    """Removes and returns a leading match (e.g. a note number) from el's
    own first text."""
    if el.text:
        m = regex.match(el.text)
        if m:
            el.text = el.text[m.end():]
            return m.group(1)
    return None


DECLARED = []      # words a build step deliberately turned into markup (e.g. "ISBN" -> <isbn>)


class _Ids:
    def __init__(self, prefix):
        self.prefix = prefix
        self.n = {}

    def next(self, kind):
        self.n[kind] = self.n.get(kind, 0) + 1
        return f"{self.prefix}-{kind}{self.n[kind]:03d}"


# ================================================================ normalize
def _flatten(gen_root):
    """Top-level stream of the generator output: body children then back
    children, in order; runs of top-level references grouped in a
    <ref-list>."""
    items = []
    for c in list(gen_root):
        if not isinstance(c.tag, str):
            continue
        if c.tag in ("body", "back"):
            items.extend(x for x in c if isinstance(x.tag, str))
        else:
            items.append(c)
    holder = _el("holder")
    for it in items:
        holder.append(it)
    _group_runs(holder)            # disp-quote / verse / def-list runs, at every level
    _place_page_targets(holder)
    for g in holder.iter():
        if isinstance(g.tag, str):
            g.attrib.pop("data-grouped", None)
    return _group_refs(list(holder))


def normalize_inner(el, ids):
    """Element-level conversions shared by BITS and JATS (in place, whole
    subtree)."""
    for e in list(el.iter()):
        if not isinstance(e.tag, str):
            continue
        tag = e.tag
        if tag in INLINE_RENAMES:
            e.tag = tag = INLINE_RENAMES[tag]
        if tag == "bold" and _inside(e, TITLE_TAGS) and _is_whole(e):
            # a heading's own bold face is implied by <title> - never a nested <bold>
            _unwrap(e)
            continue
        if tag == "equation":
            e.tag = "disp-formula"
            e.set("id", e.get("id") or ids.next("eq"))
        elif tag == "list":
            lt = e.get("list-type")
            if lt in LIST_TYPES:
                e.set("list-type", LIST_TYPES[lt])
        elif tag == "sec":
            e.attrib.pop("disp-level", None)
        elif tag == "preformat":
            e.set("preformat-type", e.get("preformat-type") or "code")
        elif tag == "statement":
            if not any(isinstance(c.tag, str) and c.tag in ("p", "label", "title") for c in e):
                label = _strip_leading(e, re.compile(r"^\s*((?:Theorem|Lemma|Proposition|Corollary|Definition|"
                                                       r"Proof|Example|Remark)\s*[\d.]*)[.:]?\s+", re.I))
                _wrap(e, "p")
                if label:
                    e.insert(0, _el("label", label))
        elif tag == "speech":
            if not any(isinstance(c.tag, str) and c.tag == "p" for c in e):
                speaker = _strip_leading(e, re.compile(r"^\s*([A-Z][A-Za-z .'-]{0,40}?)\s*:\s+"))
                _wrap(e, "p")
                if speaker:
                    e.insert(0, _el("speaker", speaker))
    _place_page_targets(el)


def _inside(el, tags):
    p = el.getparent()
    while p is not None:
        if p.tag in tags:
            return True
        p = p.getparent()
    return False


def _is_whole(el):
    """el holds ALL the text of its parent (a fully bold title)."""
    p = el.getparent()
    return p is not None and text_of(el) == text_of(p)


def _unwrap(el):
    """Replaces el by its content (text and children kept in place)."""
    parent = el.getparent()
    idx = parent.index(el)
    prev = el.getprevious()
    lead = el.text or ""
    if prev is not None:
        prev.tail = (prev.tail or "") + lead
    else:
        parent.text = (parent.text or "") + lead
    for i, c in enumerate(list(el)):
        parent.insert(idx + i, c)
    last = parent[idx + len(el) - 1] if len(el) else None
    tail = el.tail or ""
    parent.remove(el)
    if last is not None and last is not el:
        last.tail = (last.tail or "") + tail
    elif prev is not None:
        prev.tail = (prev.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail


def _group_refs(items):
    """Consecutive top-level <ref> -> one <ref-list>."""
    out = []
    for it in items:
        if it.tag == "ref":
            if out and out[-1].tag == "ref-list" and out[-1].get("data-auto") == "1":
                out[-1].append(it)
                continue
            rl = _el("ref-list")
            rl.set("data-auto", "1")
            rl.append(it)
            out.append(rl)
            continue
        out.append(it)
    for rl in out:
        if rl.tag == "ref-list":
            rl.attrib.pop("data-auto", None)
    return out


def _group_runs(parent):
    """Consecutive sibling zones that form ONE structure: disp-quote /
    epigraph paragraphs -> one <disp-quote>, verse lines -> <verse-group>,
    term / def -> <def-list>, abstract paragraphs -> one <abstract>. Applied
    to every element's children."""
    for p in list(parent.iter()):
        if not isinstance(p.tag, str) or not len(p):
            continue
        children = list(p)
        i = 0
        while i < len(children):
            c = children[i]
            tag = c.tag if isinstance(c.tag, str) else None
            if tag in ("disp-quote", "epigraph", "verse-line", "term", "def") and c.get("data-grouped") is None:
                j = i
                kinds = {"disp-quote": ("disp-quote",), "epigraph": ("epigraph",),
                         "verse-line": ("verse-line",), "term": ("term", "def"), "def": ("term", "def")}[tag]
                while j + 1 < len(children) and isinstance(children[j + 1].tag, str) and \
                        children[j + 1].tag in kinds and not (children[j].tail or "").strip():
                    j += 1
                run = children[i:j + 1]
                tail = run[-1].tail
                pos = p.index(run[0])
                group = _build_group(tag, run)
                group.set("data-grouped", "1")
                for r in run:                      # emptied originals (their content is in the group)
                    if r.getparent() is p:
                        r.tail = None
                        p.remove(r)
                p.insert(pos, group)
                group.tail = tail
                i = j + 1
                continue
            i += 1


def _build_group(tag, run):
    if tag in ("disp-quote", "epigraph"):
        g = _el("disp-quote", content_type="epigraph" if tag == "epigraph" else None)
        for r in run:
            if any(isinstance(x.tag, str) and x.tag == "p" for x in r):
                _move_content(r, g)
            else:
                para = _el("p")
                _move_content(r, para)
                g.append(para)
        return g
    if tag == "verse-line":
        g = _el("verse-group")
        for r in run:
            r.tag = "verse-line"
            g.append(r)
        return g
    g = _el("def-list")
    item = None
    for r in run:
        if r.tag == "term" or item is None:
            item = _el("def-item")
            g.append(item)
        if r.tag == "term":
            item.append(r)
        else:
            d = _el("def")
            para = _el("p")
            _move_content(r, para)
            d.append(para)
            item.append(d)
    return g


_BLOCK_WITH_P = ("sec", "body", "boxed-text", "disp-quote", "named-book-part-body", "app", "ack", "abstract",
                 "notes", "glossary", "list-item", "fn", "statement", "holder", "ref-list", "index", "index-entry",
                 "book-part", "book-body", "book-back", "front-matter-part", "preface", "foreword", "dedication",
                 "back", "book-app", "article")


def _place_page_targets(root):
    """<target target-type="pagenum"> between blocks moves into the
    following paragraph (start) or the preceding one (end)."""
    for t in list(root.iter("target")):
        parent = t.getparent()
        if parent is None or parent.tag not in _BLOCK_WITH_P:
            continue
        # nearest paragraph before the marker (skipping figures / tables /
        # other markers that hold no paragraph), else the nearest after it
        before = after = None
        prv = t.getprevious()
        while prv is not None and before is None:
            if isinstance(prv.tag, str) and prv.tag not in ("target", "caption"):
                before = prv if prv.tag in _TEXT_HOLDERS else _last_p_outside_captions(prv)
            prv = prv.getprevious()
        if before is None:
            nxt = t.getnext()
            while nxt is not None and after is None:
                if isinstance(nxt.tag, str) and nxt.tag not in ("target", "caption"):
                    after = nxt if nxt.tag in _TEXT_HOLDERS else _first_p_outside_captions(nxt)
                nxt = nxt.getnext()
        if before is not None:            # the page ends after the preceding text
            _detach(t)
            before.append(t)
            t.tail = None
        elif after is not None:           # ... or begins with the following text
            _detach(t)
            t.tail = after.text
            after.text = None
            after.insert(0, t)


_TEXT_HOLDERS = ("p", "term")


def _paras_outside_captions(el):
    return [x for x in el.iter(*_TEXT_HOLDERS)
            if not any(a.tag in ("caption", "table-wrap-foot", "td", "th", "title") for a in x.iterancestors())
            or x is el]


def _last_p_outside_captions(el):
    ps = _paras_outside_captions(el)
    return ps[-1] if ps else None


def _first_p_outside_captions(el):
    ps = _paras_outside_captions(el)
    return ps[0] if ps else None


def _last_p(el):
    ps = [x for x in el.iter(*_TEXT_HOLDERS)]
    return ps[-1] if ps else None


def _first_p(el):
    return next(iter(el.iter(*_TEXT_HOLDERS)), None)


def _detach(el):
    """Removes el from its parent, keeping its tail text in place."""
    parent = el.getparent()
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    el.tail = None
    parent.remove(el)


# ================================================================ metadata
def parse_names(text):
    """"James I. Wimsatt and Jane Roe" -> [("Wimsatt", "James I."), ("Roe", "Jane")]."""
    text = _lang.strip_byline(text)
    if _lang.dominant_script(text) in ("Han", "Hangul", "Hiragana", "Katakana"):
        # 张三、李四 / 김철수, 이영희: family name first, names separated by 、，, or spaces
        names = [n for n in re.split("[" + re.escape(_lang.CJK_NAME_SEPARATORS) + r"\s]+", text) if n]
        return [(n[:1], n[1:]) if len(n) in (2, 3) and _lang.dominant_script(n) != "Hiragana" else (n, "")
                for n in names]
    # joiners in any language: and / y / et / und / и / και / و ...
    parts = [p.strip(" ,") for p in _lang.name_split_regex().split(text) if p.strip(" ,")]
    # "Gunes Orman, Amy Mehollin-Ray, Thierry A. Huisman": a list of full
    # names (every comma piece has 2+ words), not "Surname, Given"
    split = []
    for p in parts:
        pieces = [x.strip() for x in p.split(",") if x.strip()]
        if len(pieces) > 1 and all(len(x.split()) >= 2 for x in pieces):
            split.extend(pieces)
        else:
            split.append(p)
    parts = split
    out = []
    for p in parts:
        if "," in p:
            sur, given = [x.strip() for x in p.split(",", 1)]
        else:
            bits = p.split()
            suffix = ""
            if len(bits) > 2 and re.fullmatch(r"(Jr\.?|Sr\.?|II|III|IV)", bits[-1]):
                suffix = bits.pop()
            sur, given = bits[-1], " ".join(bits[:-1])
            if suffix:
                sur = f"{sur} {suffix}"
        out.append((sur, given))
    return out


_NAME_STYLE_TAGS = ("bold", "italic", "sc", "underline", "b", "i", "u")


def _contrib_group(contribs, ids):
    group = _el("contrib-group")
    for c in contribs:
        ctype = c.get("contrib-type") or "author"
        # a byline set in bold/italic is still just names
        only_style = all(isinstance(k.tag, str) and k.tag in _NAME_STYLE_TAGS and not len(k) for k in c)
        if (len(c) and not only_style) or not text_of(c):
            con = _el("contrib", contrib_type=ctype)
            sn = _el("string-name")
            _move_content(c, sn)
            con.append(sn)
            group.append(con)
            continue
        names = parse_names(text_of(c))
        # joiners ("and", "y", ",") become the contrib structure - declared, not lost
        from collections import Counter
        kept = Counter(w for sur, given in names for w in f"{given} {sur}".split())
        for w in text_of(c).split():
            if kept[w] > 0:
                kept[w] -= 1
            else:
                DECLARED.append(w)
        for sur, given in names:
            con = _el("contrib", contrib_type=ctype)
            name = _el("name")
            if _lang.dominant_script(sur + given) in ("Han", "Hangul", "Hiragana", "Katakana"):
                name.set("name-style", "eastern")
            name.append(_el("surname", sur))
            if given:
                name.append(_el("given-names", given))
            con.append(name)
            group.append(con)
    return group


def _isbn_el(el):
    """"ISBN 978-0-8020-9154-3 (cloth)" -> <isbn publication-format="print"
    content-type="cloth">9780802091543</isbn> (the label and qualifier
    become attributes - declared, not lost)."""
    txt = text_of(el)
    m = _ISBN_RE.search(txt)
    if not m:
        return _el("isbn", txt)
    value = re.sub(r"[\s\-]", "", m.group(1))
    rest = (txt[:m.start(1)] + " " + txt[m.end(1):]).strip()
    qual = re.sub(r"^\s*ISBN(?:-1[03])?\s*:?", "", rest, flags=re.I).strip(" :;,()")
    q = qual.lower()
    fmt = "electronic" if re.search(r"e-?book|epub|electronic|pdf|online|ebk", q) else (
        "print" if re.search(r"cloth|hard|paper|print|pbk|hbk", q) else None)
    out = _el("isbn", value, publication_format=fmt, content_type=qual or None)
    DECLARED.extend(rest.split() + m.group(1).split())
    return out


def _permissions(copyright_els):
    perm = _el("permissions")
    for c in copyright_els:
        cs = _el("copyright-statement")
        _move_content(c, cs)
        perm.append(cs)
        years = re.findall(r"\b(?:1[5-9]|20)\d{2}\b", text_of(cs))
        if years and perm.find("copyright-year") is None:
            perm.append(_el("copyright-year", years[-1]))
        holder = re.sub(r"(?:copyright|©|\(c\))", "", text_of(cs), flags=re.I)
        holder = re.sub(r"\b(?:1[5-9]|20)\d{2}\b", "", holder).strip(" .,")
        if holder and perm.find("copyright-holder") is None:
            perm.append(_el("copyright-holder", holder))
    return perm


def _paragraphs_of(el):
    """el's content as <p> elements (it already is when it holds p's)."""
    if any(isinstance(c.tag, str) and c.tag == "p" for c in el):
        return [c for c in el]
    p = _el("p")
    _move_content(el, p)
    return [p]


def _kwd_group(kwds):
    g = _el("kwd-group")
    for k in kwds:
        txt = text_of(k)
        txt = re.sub(r"^\s*key\s*words?\s*[:\-]\s*", "", txt, flags=re.I)
        if len(k):
            kw = _el("kwd")
            _move_content(k, kw)
            g.append(kw)
            continue
        for part in [x.strip() for x in re.split(r"[;,·•]", txt) if x.strip()]:
            g.append(_el("kwd", part))
    return g


# ================================================================ notes
def _make_fn(el, ids, fn_type=None):
    fn = _el("fn", id=el.get("id") or ids.next("fn"), fn_type=fn_type)
    if any(isinstance(c.tag, str) and c.tag == "p" for c in el):
        label = _strip_leading(el, _NUM_LABEL_RE)
        _move_content(el, fn)
    else:
        label = _strip_leading(el, _NUM_LABEL_RE)
        p = _el("p")
        _move_content(el, p)
        fn.append(p)
    if label:
        fn.insert(0, _el("label", label.strip("[]")))
    return fn


# ================================================================ BITS
class _Part:
    def __init__(self, kind, sec=None, title_group=None):
        self.kind = kind                    # chapter / part / preface / ...
        self.sec = sec                      # the <sec> the heading opened (or None)
        self.title_group = title_group
        self.content = []                   # body elements
        self.notes = []                     # fn / en
        self.refs = []                      # ref-list
        self.meta = []                      # chapter-contrib / abstract / kwd
        self.children = []                  # chapters inside a part


def build_bits_book(gen_root, settings=None, prefix="b"):
    settings = settings or {}
    ids = _Ids(prefix)
    items = _flatten(gen_root)
    for it in items:
        normalize_inner(it, ids)
    meta = {t: [] for t in BOOK_META_TAGS}
    front, body_parts, back_parts = [], [], []
    loose_front = []                    # content before the first book part
    book_notes, book_refs = [], []
    current = None
    current_part = None                 # an open "part" collecting chapters
    pending_title_group = None
    index_entries = []

    def target_list():
        return current.content if current is not None else loose_front

    pending_label = None                # a "Chapter Number / Label" zone just before a part heading
    items = _hoist_chapter_labels(items)

    for it in items:
        tag = it.tag
        opens_part = tag == "sec" and (it.get(PART_TYPE_ATTR) or current is None)
        if pending_label is not None and not opens_part:
            target_list().append(pending_label)
            pending_label = None
        if tag == "label" and _is_chapter_label(it):
            pending_label = it
            continue
        if tag in meta and (current is None or tag in ("book-title", "book-subtitle", "isbn", "publisher-name",
                                                        "publisher-loc", "series-title", "edition",
                                                        "copyright-statement")):
            meta[tag].append(it)
            continue
        if tag == "title-group" and current is None:
            pending_title_group = it
            continue
        if tag == "sec" and it.get(PART_TYPE_ATTR) or tag == "sec" and current is None:
            kind = it.get(PART_TYPE_ATTR) or "chapter"
            it.attrib.pop(PART_TYPE_ATTR, None)
            if pending_label is not None:
                if pending_title_group is None:
                    pending_title_group = _el("title-group")
                if pending_title_group.find("label") is None:
                    pending_title_group.insert(0, pending_label)
                else:
                    target_list().append(pending_label)
                pending_label = None
            part = _Part(kind, it, pending_title_group)
            pending_title_group = None
            # the heading's own section content is the part's body
            for c in list(it):
                if c.tag != "title":
                    part.content.append(c)
            if kind == "part":
                current_part = part
                body_parts.append(part)
            elif kind in FRONT_PART_TYPES and not body_parts:
                front.append(part)
            elif kind in BACK_PART_TYPES:
                back_parts.append(part)
                current_part = None
            elif kind in ("chapter", "introduction") and current_part is not None:
                current_part.children.append(part)
            else:
                body_parts.append(part)
            current = part
            _absorb_part_items(part, index_entries)
            continue
        if tag in ("fn", "en"):
            (current.notes if current is not None else book_notes).append(it)
            continue
        if tag == "ref-list":
            if current is not None and (current.kind not in BACK_PART_TYPES or current.kind == "bibliography"):
                current.refs.append(it)
            else:
                book_refs.append(it)
            continue
        if tag == "index-entry":
            index_entries.append(it)
            continue
        if tag in ("chapter-contrib", "abstract", "kwd") and current is not None:
            current.meta.append(it)
            continue
        if tag == "title-group" and current is not None and current.title_group is None and not current.content:
            current.title_group = it
            continue
        target_list().append(it)

    if pending_label is not None:
        target_list().append(pending_label)
    # page markers that precede all content (the page-1 marker before the
    # first heading) belong in the first paragraph, not an empty front matter
    lead_targets = []
    while loose_front and loose_front[0].tag == "p" and not text_of(loose_front[0]).strip() \
            and len(loose_front[0]) and all(c.tag == "target" for c in loose_front[0]):
        lead_targets += list(loose_front.pop(0))
    if lead_targets:
        def walk(parts):
            for part in parts:
                yield part
                yield from walk(getattr(part, "children", []))
        first_p = None
        for part in walk(front + body_parts):
            first_p = next((p for c in part.content for p in c.iter("p") if text_of(p).strip()), None)
            if first_p is not None:
                break
        if first_p is not None:
            for t in reversed(lead_targets):
                t.tail = (t.tail or "") + (first_p.text or "")
                first_p.text = None
                first_p.insert(0, t)
        else:
            loose_front.append(_el("p"))
            loose_front[-1].extend(lead_targets)
    book = etree.Element("book", nsmap={"xlink": XLINK_NS, "mml": MML_NS})
    book.set("dtd-version", "2.2")
    book.set(XML_LANG, settings.get("language") or "en")
    if settings.get("book_type"):
        book.set("book-type", settings["book_type"])
    cm = _collection_meta(meta)
    if cm is not None:
        book.append(cm)
    book.append(_book_meta(meta, settings, ids))
    if front or loose_front:
        fm = etree.SubElement(book, "front-matter")
        if loose_front:
            fmp = etree.SubElement(fm, "front-matter-part", {"id": ids.next("fm"), "book-part-type": "front-matter"})
            nb = etree.SubElement(fmp, "named-book-part-body")
            nb.extend(_as_blocks(loose_front))
        for part in front:
            fm.append(_front_part(part, ids))
    bb = etree.SubElement(book, "book-body")
    for part in body_parts:
        bb.append(_book_part(part, ids))
    if not len(bb):
        bb.append(_el("book-part", id=ids.next("ch"), book_part_type="chapter"))
    if back_parts or book_notes or book_refs or index_entries:
        back = etree.SubElement(book, "book-back")
        apps = [p for p in back_parts if p.kind == "appendix"]
        if apps:
            grp = etree.SubElement(back, "book-app-group")
            for p in apps:
                app = etree.SubElement(grp, "book-app", {"id": ids.next("app")})
                _part_meta_and_body(app, p, ids)
        for p in back_parts:
            if p.kind == "notes":
                notes = etree.SubElement(back, "notes", {"id": ids.next("notes")})
                notes.append(_title_el(p))
                notes.extend(_as_blocks(p.content))
                if p.notes:
                    fg = etree.SubElement(notes, "fn-group")
                    fg.extend(_make_fn(n, ids) for n in p.notes)
            elif p.kind == "glossary":
                gl = etree.SubElement(back, "glossary", {"id": ids.next("gloss")})
                gl.append(_title_el(p))
                gl.extend(_as_blocks(p.content))
            elif p.kind == "bibliography":
                for rl in p.refs or [None]:
                    if rl is None:
                        rl = _el("ref-list")
                    if rl.find("title") is None and _title_el(p) is not None:
                        rl.insert(0, _title_el(p))
                    for c in p.content:
                        rl.insert(1, c) if c.tag in ("p",) else rl.append(c)
                    back.append(rl)
        if book_notes:
            fg = etree.SubElement(etree.SubElement(back, "notes", {"id": ids.next("notes")}), "fn-group")
            fg.extend(_make_fn(n, ids, "other") for n in book_notes)
        for rl in book_refs:
            back.append(rl)
        idx_part = next((p for p in back_parts if p.kind == "index"), None)
        if index_entries or idx_part is not None:
            back.append(_index(idx_part, index_entries, ids))
    return book


def _is_chapter_label(el):
    return el.tag == "label" and bool(_CHAPTER_LABEL_RE.match(text_of(el).strip() + " x"))


def _trailing_label(el):
    """A chapter label ("Capítulo 1.2") that the generator nested at the very
    end of the previous chapter's last section."""
    while True:
        kids = [c for c in el if isinstance(c.tag, str)]
        if not kids:
            return None
        last = kids[-1]
        if _is_chapter_label(last):
            return last
        if last.tag not in ("sec", "body", "boxed-text"):
            return None
        el = last


def _hoist_chapter_labels(items):
    out = []
    for it in items:
        if it.tag == "sec" and it.get(PART_TYPE_ATTR) and out and not _is_chapter_label(out[-1]):
            lab = _trailing_label(out[-1])
            if lab is not None:
                _detach(lab)
                out.append(lab)
        out.append(it)
    return out


def _absorb_part_items(part, index_entries):
    """Notes / ref-lists / chapter metadata that the generator placed INSIDE
    the heading's <sec> move to their structural place."""
    for c in part.content:
        for n in list(c.iter("fn", "en", "index-entry")):
            if n is c:
                continue
            _detach(n)
            (index_entries if n.tag == "index-entry" else part.notes).append(n)
    keep = []
    for c in part.content:
        if c.tag in ("fn", "en"):
            part.notes.append(c)
        elif c.tag == "ref-list":
            part.refs.append(c)
        elif c.tag == "index-entry":
            index_entries.append(c)
        elif c.tag in ("chapter-contrib", "abstract", "kwd"):
            part.meta.append(c)
        elif c.tag == "title-group" and part.title_group is None and not keep:
            part.title_group = c
        else:
            keep.append(c)
    part.content = keep


def _title_el(part):
    if part.sec is None:
        return None
    t = part.sec.find("title")
    return deepcopy(t) if t is not None else None


def _title_group(part):
    tg = _el("title-group")
    src = part.title_group
    label = title = subtitle = None
    if src is not None:
        label = src.find("label")
        title = src.find("title")
        subtitle = src.find("subtitle")
    sec_title = part.sec.find("title") if part.sec is not None else None
    if title is None and sec_title is not None:
        title = sec_title
    elif title is not None and sec_title is not None and text_of(sec_title):
        # the heading zone is the title; the title-group only provides the label
        if label is None:
            label = _el("label")
            _move_content(title, label)
        title = sec_title
    if label is None and title is not None and title.text:
        m = _CHAPTER_LABEL_RE.match(title.text)
        if m and not (m.group(1) or m.group(2)):
            m = None
        if m and part.kind in ("chapter", "part", "appendix") and len(title.text) > m.end():
            label = _el("label", title.text[:m.end()].strip())     # keep "I." / "1:" punctuation
            title.text = title.text[m.end():]
    if label is not None:
        tg.append(label)
    tg.append(title if title is not None else _el("title"))
    if subtitle is not None:
        tg.append(subtitle)
    if src is not None:
        for c in list(src):           # anything else the title-group held is kept, never lost
            if c is not label and c is not title and c is not subtitle:
                tg.append(c)
    return tg




def _is_empty_ref_heading(sec):
    if sec.tag != "sec":
        return False
    rest = [c for c in sec if isinstance(c.tag, str) and c.tag != "title"]
    if any(c.tag != "target" for c in rest) or text_of(sec).strip() == "":
        return False
    title = text_of(sec.find("title")) if sec.find("title") is not None else ""
    return sec.get("sec-type") == "references" or _lang.is_heading(title, "references")


def _ref_labels(rl):
    """"1. Rose J, Tuma F. ..." -> <ref><label>1.</label><mixed-citation>Rose J ..."""
    for ref in rl.iter("ref"):
        if ref.find("label") is not None:
            continue
        mc = ref.find("mixed-citation")
        if mc is None or not mc.text:
            continue
        m = re.match(r"^\s*(\[?\d+[a-z]?[.)\]]?)\s+", mc.text)
        if m:
            ref.insert(ref.index(mc), _el("label", m.group(1)))
            mc.text = mc.text[m.end():]


def _title_ref_lists(part):
    """A chapter's reference heading ("REFERENCIAS CLAVE") is the title of its
    reference list, not an empty section; a list split by a column / page
    break is one list."""
    if not part.refs:
        return
    merged = []
    for rl in part.refs:
        if merged and rl.find("title") is None:
            for c in list(rl):
                merged[-1].append(c)
            continue
        merged.append(rl)
    part.refs = merged
    heads = [s for c in part.content for s in c.iter("sec") if _is_empty_ref_heading(s)]
    untitled = [rl for rl in merged if rl.find("title") is None]
    for sec, rl in zip(heads, untitled):
        title = sec.find("title")
        rl.insert(0, title)
        targets = [t for t in sec if t.tag == "target"]
        first = rl.find(".//mixed-citation")
        for t in reversed(targets):
            if first is not None:
                t.tail = first.text
                first.text = None
                first.insert(0, t)
        if sec in part.content:
            part.content.remove(sec)
        if sec.getparent() is not None:
            _detach(sec)
    for rl in merged:
        _ref_labels(rl)


def _sections_last(blocks):
    """JATS / BITS content models put a container's paragraphs, lists,
    figures ... BEFORE its sections. A block that follows a section in the
    reading order belongs to that section (its last, deepest subsection) -
    the reading order is kept, nothing is reordered."""
    out = []
    for b in blocks:
        if out and out[-1].tag == "sec" and b.tag not in ("sec", "target"):
            host = out[-1]
            while len(host) and host[-1].tag == "sec":
                host = host[-1]
            host.append(b)
            continue
        out.append(b)
    return out


def _part_meta_and_body(el, part, ids):
    _title_ref_lists(part)
    bpm = etree.SubElement(el, "book-part-meta")
    bpm.append(_title_group(part))
    contribs = [m for m in part.meta if m.tag == "chapter-contrib"]
    if contribs:
        bpm.append(_contrib_group(contribs, ids))
    for a in [m for m in part.meta if m.tag == "abstract"]:
        ab = etree.SubElement(bpm, "abstract")
        ab.extend(_paragraphs_of(a))
    kwds = [m for m in part.meta if m.tag == "kwd"]
    if kwds:
        bpm.append(_kwd_group(kwds))
    if part.content or part.children:
        body = etree.SubElement(el, "body")
        body.extend(_sections_last(_as_blocks(part.content)))
        for ch in part.children:
            body.append(_book_part(ch, ids))
    if part.notes or part.refs:
        back = etree.SubElement(el, "back")
        if part.notes:
            fg = etree.SubElement(back, "fn-group")
            fg.extend(_make_fn(n, ids, "other" if n.tag == "en" else None) for n in part.notes)
        back.extend(part.refs)


def _book_part(part, ids):
    kind = part.kind if part.kind in ("part", "chapter", "introduction") else part.kind
    bp = _el("book-part", id=ids.next("pt" if kind == "part" else "ch"), book_part_type=kind)
    _part_meta_and_body(bp, part, ids)
    return bp


def _front_part(part, ids):
    tag = {"dedication": "dedication", "foreword": "foreword", "preface": "preface", "ack": "ack",
           "toc": "toc"}.get(part.kind, "front-matter-part")
    el = _el(tag, id=ids.next("fm"))
    if tag == "front-matter-part":
        el.set("book-part-type", part.kind)
    if tag == "ack":
        t = _title_el(part)
        if t is not None:
            el.append(t)
        el.extend(_as_blocks(part.content))
        return el
    if tag == "toc":
        el.append(_el("toc-title-group"))
        el[0].append(_title_el(part) if _title_el(part) is not None else _el("title"))
        div = etree.SubElement(el, "toc-div")
        for c in part.content:
            entry = etree.SubElement(div, "toc-entry")
            t = _el("title")
            _move_content(c, t)
            entry.append(t)
        return el
    bpm = etree.SubElement(el, "book-part-meta")
    bpm.append(_title_group(part))
    if part.content:
        nb = etree.SubElement(el, "named-book-part-body")
        nb.extend(_as_blocks(part.content))
    return el


def _as_blocks(elements):
    """Body-level elements; a bare inline element (e.g. a stray <target>)
    becomes a paragraph so nothing sits where only blocks are allowed."""
    out = []
    for e in elements:
        if e.tag in ("target", "italic", "bold", "sup", "sub", "xref", "named-content"):
            p = _el("p")
            p.append(e)
            out.append(p)
        else:
            out.append(e)
    return out


def _book_meta(meta, settings, ids):
    bm = _el("book-meta")
    if settings.get("book_id"):
        bm.append(_el("book-id", settings["book_id"], book_id_type=settings.get("book_id_type", "publisher-id")))
    if settings.get("doi"):
        bm.append(_el("book-id", settings["doi"], book_id_type="doi"))
    btg = etree.SubElement(bm, "book-title-group")
    titles = meta["book-title"]
    bt = _el("book-title", settings.get("book_title") if not titles else None)
    for t in titles:
        if len(bt) or bt.text:
            bt.append(_el("break"))
        _move_content(t, bt)
    btg.append(bt)
    for s in meta["book-subtitle"]:
        st = _el("subtitle")
        _move_content(s, st)
        btg.append(st)
    if meta["contrib"]:
        bm.append(_contrib_group(meta["contrib"], ids))
    for a in meta["aff"]:
        aff = _el("aff", id=ids.next("aff"))
        _move_content(a, aff)
        bm.append(aff)
    for i in meta["isbn"]:
        bm.append(_isbn_el(i))
    if settings.get("isbn") and not meta["isbn"]:
        bm.append(_el("isbn", re.sub(r"[\s\-]", "", settings["isbn"])))
    if meta["publisher-name"] or meta["publisher-loc"] or settings.get("publisher"):
        pub = etree.SubElement(bm, "publisher")
        pn = _el("publisher-name", settings.get("publisher") if not meta["publisher-name"] else None)
        for n in meta["publisher-name"]:
            _move_content(n, pn)
        pub.append(pn)
        for loc in meta["publisher-loc"]:
            pl = _el("publisher-loc")
            _move_content(loc, pl)
            pub.append(pl)
    if meta["edition"]:                 # BITS book-meta order: isbn*, publisher*, edition*, permissions?
        ed = _el("edition")
        for e in meta["edition"]:
            _move_content(e, ed)
        bm.append(ed)
    if meta["copyright-statement"]:
        bm.append(_permissions(meta["copyright-statement"]))
    return bm


def _collection_meta(meta):
    """Series title -> <collection-meta>, a sibling BEFORE <book-meta>
    (BITS: book = processing-meta?, collection-meta*, book-meta?, ...)."""
    if not meta["series-title"]:
        return None
    cm = _el("collection-meta")
    tg = etree.SubElement(cm, "title-group")
    for s in meta["series-title"]:
        t = _el("title")
        _move_content(s, t)
        tg.append(t)
    return cm


def _index(idx_part, entries, ids):
    index = _el("index", id=ids.next("idx"))
    if idx_part is not None and idx_part.sec is not None:
        itg = etree.SubElement(index, "index-title-group")
        itg.append(_title_el(idx_part))
        trailing = []
        if idx_part.content:
            # paragraphs under the index heading (e.g. "Page numbers in italics ...")
            for c in idx_part.content:
                if c.tag == "index-entry":
                    entries.append(c)
                elif c.tag == "target":
                    trailing.append(c)          # a page marker ends the index page
                else:
                    index.append(c)
    else:
        trailing = []
    stack = [(0, index)]
    for e in entries:
        level = int(e.get("index-level") or 1)
        e.attrib.pop("index-level", None)
        entry = _el("index-entry", id=ids.next("ie"))
        term = _el("term")
        _move_content(e, term)
        entry.append(term)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if not stack:
            stack = [(0, index)]
        stack[-1][1].append(entry)
        stack.append((level, entry))
    index.extend(trailing)
    return index


# ================================================================ JATS
def build_jats_article(gen_root, settings=None, prefix="a"):
    settings = settings or {}
    ids = _Ids(prefix)
    items = _flatten(gen_root)
    for it in items:
        normalize_inner(it, ids)
    meta = {t: [] for t in ARTICLE_META_TAGS}
    body, back_secs, notes, refs = [], [], [], []
    for it in items:
        tag = it.tag
        if tag in meta:
            meta[tag].append(it)
            continue
        if tag == "sec" and it.get(PART_TYPE_ATTR) in ("ack", "appendix", "notes", "glossary", "bibliography"):
            back_secs.append(it)
            _pull(it, notes, refs, meta)
            continue
        if tag == "sec":
            it.attrib.pop(PART_TYPE_ATTR, None)
            _pull(it, notes, refs, meta)
            body.append(it)
            continue
        if tag in ("fn", "en"):
            notes.append(it)
            continue
        if tag == "ref-list":
            refs.append(it)
            continue
        body.append(it)
    art = etree.Element("article", nsmap={"xlink": XLINK_NS, "mml": MML_NS})
    art.set("article-type", settings.get("article_type", "research-article"))
    art.set("dtd-version", "1.4")
    art.set(XML_LANG, settings.get("language") or "en")
    front = etree.SubElement(art, "front")
    jm = _journal_meta(settings)
    if meta["publisher-name"] and jm.find("publisher") is None:
        pub = etree.SubElement(jm, "publisher")
        pn = _el("publisher-name")
        for n in meta["publisher-name"]:
            _move_content(n, pn)
        pub.append(pn)
        for loc in meta["publisher-loc"]:
            pl = _el("publisher-loc")
            _move_content(loc, pl)
            pub.append(pl)
    elif meta["publisher-name"] or meta["publisher-loc"]:
        for n in meta["publisher-name"] + meta["publisher-loc"]:       # never dropped
            n.tag = "p"
            n.set("content-type", "publisher")
            meta.setdefault("_body", []).append(n)
    front.append(jm)
    front.append(_article_meta(meta, settings, ids))
    b = etree.SubElement(art, "body")
    b.extend(_as_blocks(meta.get("_body", []) + body))
    if back_secs or notes or refs:
        back = etree.SubElement(art, "back")
        for s in back_secs:
            kind = s.attrib.pop(PART_TYPE_ATTR)
            title = s.find("title")
            if kind == "ack":
                s.tag = "ack"
                s.attrib.pop("disp-level", None)
                back.append(s)
            elif kind == "appendix":
                grp = back.find("app-group")
                if grp is None:
                    grp = etree.SubElement(back, "app-group")
                s.tag = "app"
                grp.append(s)
            elif kind == "notes":
                s.tag = "notes"
                back.append(s)
            elif kind == "glossary":
                s.tag = "glossary"
                back.append(s)
            elif kind == "bibliography":
                if refs:
                    if refs[0].find("title") is None and title is not None:
                        refs[0].insert(0, title)
                    for c in [c for c in s if c is not title]:
                        refs[0].insert(1, c)
                else:
                    s.tag = "ref-list"
                    refs.append(s)
        for rl in refs:
            back.append(rl)
        if notes:
            fg = etree.SubElement(back, "fn-group")
            fg.extend(_make_fn(n, ids) for n in notes)
    return art


def _pull(sec, notes, refs, meta):
    for c in list(sec.iter()):
        if c is sec or not isinstance(c.tag, str):
            continue
        if c.tag in ("fn", "en"):
            _detach(c)
            notes.append(c)
        elif c.tag == "ref-list" and c.getparent() is not None:
            _detach(c)
            refs.append(c)
        elif c.tag in ("abstract", "kwd") and c.getparent() is not None:
            _detach(c)
            meta[c.tag].append(c)


def _journal_meta(settings):
    jm = _el("journal-meta")
    jm.append(_el("journal-id", settings.get("journal_id") or "journal", journal_id_type="publisher-id"))
    if settings.get("journal_title"):
        jtg = etree.SubElement(jm, "journal-title-group")
        jtg.append(_el("journal-title", settings["journal_title"]))
    jm.append(_el("issn", settings.get("issn") or "0000-0000", publication_format=settings.get("issn_format", "print")))
    if settings.get("publisher"):
        pub = etree.SubElement(jm, "publisher")
        pub.append(_el("publisher-name", settings["publisher"]))
    return jm


def _article_meta(meta, settings, ids):
    am = _el("article-meta")
    doi = settings.get("doi")
    for d in meta["article-doi"]:
        doi = doi or re.sub(r"^\s*(?:doi\s*:?\s*|https?://(?:dx\.)?doi\.org/)", "", text_of(d), flags=re.I)
    if doi:
        am.append(_el("article-id", doi, pub_id_type="doi"))
    tg = etree.SubElement(am, "title-group")
    at = _el("article-title")
    for t in meta["article-title"] + meta["book-title"]:
        if len(at) or at.text:
            at.append(_el("break"))
        _move_content(t, at)
    tg.append(at)
    for s in meta["subtitle"] + meta["book-subtitle"]:
        st = _el("subtitle")
        _move_content(s, st)
        tg.append(st)
    if meta["contrib"] or meta["chapter-contrib"]:
        am.append(_contrib_group(meta["contrib"] + meta["chapter-contrib"], ids))
    for a in meta["aff"]:
        aff = _el("aff", id=ids.next("aff"))
        _move_content(a, aff)
        am.append(aff)
    if meta["corresp"] or meta["author-note"]:
        an = etree.SubElement(am, "author-notes")
        for c in meta["corresp"]:
            co = _el("corresp", id=ids.next("cor"))
            _move_content(c, co)
            an.append(co)
        for n in meta["author-note"]:
            an.append(_make_fn(n, ids))
    if settings.get("pub_year"):
        pd = etree.SubElement(am, "pub-date", {"publication-format": "print", "date-type": "pub"})
        pd.append(_el("year", str(settings["pub_year"])))
    for i in meta["isbn"]:
        am.append(_isbn_el(i))
    for k, tag in (("volume", "volume"), ("issue", "issue"), ("fpage", "fpage"), ("lpage", "lpage")):
        if settings.get(k):
            am.append(_el(tag, str(settings[k])))
    if meta["history"]:
        h = etree.SubElement(am, "history")
        for x in meta["history"]:
            d = _el("date", date_type="received")
            sd = _el("string-date")
            _move_content(x, sd)
            d.append(sd)
            h.append(d)
    if meta["copyright-statement"]:
        am.append(_permissions(meta["copyright-statement"]))
    for a in meta["abstract"]:
        if am.find("abstract") is None:
            etree.SubElement(am, "abstract")
        am.find("abstract").extend(_paragraphs_of(a))
    if meta["kwd"]:
        am.append(_kwd_group(meta["kwd"]))
    return am


def build(kind, gen_root, settings=None, prefix=None):
    del DECLARED[:]
    # xml:lang: the setting, or detected from the whole text (before the
    # builders move the content out of gen_root)
    settings = dict(settings or {})
    settings["language"] = _lang.resolve_language(settings.get("language"), " ".join(gen_root.itertext()))
    if kind.upper() in ("BITS", "BITS-BOOK", "BOOK"):
        root = build_bits_book(gen_root, settings, prefix or "b")
    else:
        root = build_jats_article(gen_root, settings, prefix or "a")
    _place_page_targets(root)
    _join_continuations(root)
    return root


_FLOW_PARENTS = ("sec", "body", "named-book-part-body", "list-item", "boxed-text", "app", "notes", "ack",
                 "preface", "foreword", "dedication", "book-app")
_NOT_FLOW = ("caption", "table-wrap", "fig", "ref-list", "fn", "title-group", "book-part-meta", "abstract",
             "disp-quote", "def-list")


def _plain_start(p):
    for t in p.itertext():
        t = t.lstrip()
        if t:
            return t
    return ""


def _join_continuations(root):
    """A paragraph that continues the text before a column / page break,
    past a figure or table set in between ("... disminuyen desde el 0 hasta
    el ta-" | table | "maño USP 2-0 ..."), is the same paragraph: it starts
    lowercase and the text before it stops mid-sentence. It is joined to
    that paragraph (or list item paragraph); a word hyphenated across the
    break is rejoined. Nothing is reordered within the text itself."""
    last = None
    doc_lang = root.get(XML_LANG)
    for p in list(root.iter("p")):
        if any(a.tag in _NOT_FLOW for a in p.iterancestors()):
            continue
        start = _plain_start(p)
        if last is not None and p.getparent() is not None and p.getparent().tag in _FLOW_PARENTS:
            prev_text = "".join(last.itertext()).rstrip()
            if prev_text and prev_text[-1] not in ":;" and _lang.continues(prev_text, start, doc_lang):
                _append_paragraph(last, p, hyphen=bool(re.search(r"[^\W\d_]-$", prev_text))
                                  and _lang.starts_lowercase(start))
                continue
        last = p


def _append_paragraph(dst, src, hyphen):
    # where dst's text ends: the tail of its last child, or its own text
    if len(dst):
        tail_holder = dst[-1]
        end = tail_holder.tail or ""
    else:
        tail_holder, end = None, dst.text or ""
    if hyphen and end.rstrip().endswith("-"):
        stripped = end.rstrip()
        frag = re.search(r"(\w+-)$", stripped).group(1)
        end = stripped[:-1]
        first = _plain_start(src).split()[0] if _plain_start(src).split() else ""
        DECLARED.extend([frag, first])          # "múscu-" + "los" -> "músculos"
        sep = ""
    else:
        end = end.rstrip()
        sep = _lang.joiner(end, _plain_start(src))
    if tail_holder is None:
        dst.text = end
    else:
        tail_holder.tail = end
    text = (src.text or "").lstrip()
    if tail_holder is None and not len(dst):
        dst.text = end + sep + text
    else:
        holder = dst[-1]
        holder.tail = (holder.tail or "") + sep + text
    for c in list(src):
        dst.append(c)
    parent = src.getparent()
    if src.tail and src.tail.strip():
        prev = src.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + src.tail
        else:
            parent.text = (parent.text or "") + src.tail
    parent.remove(src)
