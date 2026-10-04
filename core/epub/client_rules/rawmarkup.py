"""A small tokenizer over the RAW source of an XHTML document, so client-rule
repairs can be minimal textual edits (the repair engine never re-serializes
a document).

walk(raw) yields one TextSeg per run of character data, with:
  start, end    offsets of the run in `raw`
  text          the decoded text (character references resolved)
  offsets       offsets[i] = position in `raw` of decoded character i
                (len(text)+1 entries, the last one is `end`)
  stack         the open elements as Node(name, attrs, start)
  prev, next    the sibling node before / after the run: a Node for an
                element (with .text when it is complete), "text" or None
"""
import html
import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>|"
                       r"</\s*([\w:.-]+)\s*>|<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>",
                       re.S | re.I)
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)')")
_REF_RE = re.compile(r"&(#x[0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]*);")


@dataclass
class Node:
    name: str                       # local name, lower case
    attrs: dict
    start: int
    end: int = -1                   # end of the start tag
    close: int = -1                 # end of the end tag (complete elements)
    text: str = ""                  # decoded text content (complete elements)

    def get(self, attr, default=""):
        return self.attrs.get(attr, default)

    @property
    def epub_type(self):
        return self.attrs.get("epub:type", "")

    @property
    def classes(self):
        return self.attrs.get("class", "").split()


@dataclass
class TextSeg:
    start: int
    end: int
    text: str
    offsets: list
    stack: list
    prev: object = None
    next: object = None
    raw: str = ""

    def raw_span(self, i, j):
        """raw offsets of decoded characters i..j (exclusive)."""
        return self.offsets[i], self.offsets[j]

    def inside(self, *names):
        return any(n.name in names for n in self.stack)

    @property
    def parent(self):
        return self.stack[-1] if self.stack else None


def _local(qname):
    return qname.rsplit(":", 1)[-1].lower()


def attrs_of(attr_text):
    out = {}
    for m in _ATTR_RE.finditer(attr_text or ""):
        v = m.group(3) if m.group(3) is not None else m.group(4)
        out[m.group(1)] = html.unescape(v)
    return out


def decode(raw_text, base=0):
    """(text, offsets) for a run of character data."""
    text, offs = [], []
    pos = 0
    for m in _REF_RE.finditer(raw_text):
        for k in range(pos, m.start()):
            text.append(raw_text[k])
            offs.append(base + k)
        ref = m.group(1)
        try:
            if ref.startswith("#x"):
                ch = chr(int(ref[2:], 16))
            elif ref.startswith("#"):
                ch = chr(int(ref[1:]))
            else:
                ch = html.unescape(m.group(0))
        except (ValueError, OverflowError):
            ch = m.group(0)
        for c in ch:
            text.append(c)
            offs.append(base + m.start())
        pos = m.end()
    for k in range(pos, len(raw_text)):
        text.append(raw_text[k])
        offs.append(base + k)
    offs.append(base + len(raw_text))
    return "".join(text), offs


def walk(raw):
    """Yields TextSeg for every character-data run inside <body> (or the
    whole document when it has no body)."""
    has_body = re.search(r"<body[\s>]", raw, re.I) is not None
    stack = []
    children = [[]]                       # per open element: its child nodes so far
    segs = []
    pos = 0
    in_body = not has_body

    def text_run(a, b):
        if a >= b:
            return
        t, offs = decode(raw[a:b], a)
        seg = TextSeg(a, b, t, offs, list(stack), raw=raw)
        sib = children[-1]
        seg.prev = sib[-1] if sib else None
        sib.append(seg)
        if in_body:
            segs.append(seg)
        for n in stack:
            n.text += t

    for m in _TOKEN_RE.finditer(raw):
        text_run(pos, m.start())
        pos = m.end()
        tok = m.group(0)
        if tok.startswith("<!--") or tok.startswith("<?") or tok.upper().startswith("<!DOCTYPE"):
            continue
        if tok.startswith("<![CDATA["):
            body = tok[9:-3]
            for n in stack:
                n.text += body
            continue
        if m.group(1):                                   # end tag
            name = _local(m.group(1))
            # close up to the matching element (tolerates small mismatches)
            idx = next((k for k in range(len(stack) - 1, -1, -1) if stack[k].name == name), None)
            if idx is None:
                continue
            while len(stack) > idx:
                node = stack.pop()
                node.close = m.end()
                children.pop()
                children[-1].append(node)
            if name == "body":
                in_body = False
            continue
        name = _local(m.group(2))
        node = Node(name, attrs_of(m.group(3)), m.start(), m.end())
        if m.group(4):                                   # self-closing
            node.close = m.end()
            children[-1].append(node)
            continue
        stack.append(node)
        children.append([])
        if name == "body":
            in_body = True
    text_run(pos, len(raw))
    # next-sibling links
    _link_next(segs)
    return segs


def _link_next(segs):
    # prev of a segment is known; next is the node that follows it among the
    # same parent's children - recover it from the following segment / node.
    by_parent = {}
    for s in segs:
        key = id(s.stack[-1]) if s.stack else 0
        by_parent.setdefault(key, []).append(s)
    for s in segs:
        s.next = None
    # a segment's next sibling is an element when the raw text right after it
    # opens a tag that is not an end tag of the parent
    for s in segs:
        if s.end < len(s.raw) and s.raw.startswith("<", s.end) and not s.raw.startswith("</", s.end) \
                and not s.raw.startswith("<!--", s.end):
            m = _TOKEN_RE.match(s.raw, s.end)
            if m and m.group(2):
                s.next = Node(_local(m.group(2)), attrs_of(m.group(3)), m.start(), m.end())


def line_col(raw, offset):
    line = raw.count("\n", 0, offset) + 1
    col = offset - (raw.rfind("\n", 0, offset) + 1) + 1
    return line, col


def escape_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(s):
    return escape_text(s).replace('"', "&quot;")


def apply_edits(raw, edits):
    """edits: [(start, end, replacement)] on raw offsets, non-overlapping."""
    out, pos = [], 0
    for a, b, rep in sorted(edits, key=lambda e: (e[0], e[1])):
        if a < pos:
            continue                                    # overlapping edit - skipped
        out.append(raw[pos:a])
        out.append(rep)
        pos = b
    out.append(raw[pos:])
    return "".join(out)


@dataclass
class EditList:
    items: list = field(default_factory=list)

    def add(self, a, b, rep):
        if any(not (b <= x or a >= y) for x, y, _r in self.items):
            return False
        self.items.append((a, b, rep))
        return True
