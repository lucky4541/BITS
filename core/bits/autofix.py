"""Data-safe automatic repair of DTD problems in a generated BITS / JATS
tree, driven by the DTD itself (core.tag_knowledge.dtd_model):

  text where only elements are allowed     -> the text run (with its inline
                                              neighbours) wrapped in <p>
  an inline element where only blocks go   -> wrapped in <p>
  a block inside a paragraph               -> the paragraph is split around it
  an element the DTD does not know         -> a known equivalent (RENAMES), or
                                              <named-content content-type=".."> /
                                              <p content-type=".."> keeping it
  children in the wrong order              -> reordered by the content model's
                                              sequence (stable)
  a required child missing                 -> added (empty) when it may be empty
  a required attribute missing             -> added with its default / an id
  an attribute the element does not take   -> removed (listed in the report)
  an enumerated value not allowed          -> mapped (number -> order ...), else
                                              removed (listed)

Every pass is checked: the document's words must be exactly the same after
it, otherwise the pass is undone. What the DTD still rejects is reported
with the validator's message."""
import itertools
from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from core.bits.structure import content_signature

RENAMES = {"equation": "disp-formula", "figure": "fig", "bibliography": "ref-list", "reference": "ref",
           "list-bullet": "list", "h1": "title", "h2": "title", "h3": "title", "h4": "title", "h5": "title",
           "h6": "title", "pagenumber": "target", "en": "fn", "epigraph": "disp-quote", "chapter-contrib": "contrib",
           "article-doi": "article-id", "author-note": "fn", "series-title": "title", "book-subtitle": "subtitle"}
INTERNAL_ATTRS = {"data-part-type", "disp-level", "data-src", "src_zone", "index-level"}
VALUE_MAP = {("list", "list-type"): {"number": "order", "numbered": "order", "decimal": "order",
                                     "upper-alpha": "alpha-upper", "lower-alpha": "alpha-lower",
                                     "upper-roman": "roman-upper", "lower-roman": "roman-lower"}}
ATTR_DEFAULTS = {"book-part-type": "chapter", "contrib-type": "author", "ref-type": "other",
                 "pub-id-type": "other", "journal-id-type": "publisher-id", "book-id-type": "publisher-id",
                 "list-type": "simple", "fn-type": "other", "target-type": "other", "publication-format": "print"}
# elements whose children are READING ORDER (text flow) - never reordered;
# a block that follows a <sec> moves INTO that section instead
FLOW_CONTAINERS = {"body", "sec", "named-book-part-body", "app", "ack", "boxed-text", "book-app", "notes",
                   "glossary", "abstract", "trans-abstract", "bio", "front-matter-part", "preface", "foreword",
                   "dedication", "book-part", "book-body", "book-back", "back", "index", "index-div", "toc",
                   "toc-div", "list", "list-item", "disp-quote", "verse-group", "def-list", "ref-list", "fn-group"}
INLINE_HINT = {"target", "italic", "bold", "sup", "sub", "sc", "underline", "monospace", "xref", "named-content",
               "break", "inline-formula", "inline-graphic", "ext-link", "uri", "email", "styled-content"}
XML_NS = "http://www.w3.org/XML/1998/namespace"


@dataclass
class FixReport:
    fixes: list = field(default_factory=list)          # (rule, element path, detail)
    removed_attributes: list = field(default_factory=list)
    errors_before: list = field(default_factory=list)
    errors_after: list = field(default_factory=list)
    passes: int = 0
    reverted: list = field(default_factory=list)

    @property
    def valid(self):
        return not self.errors_after

    def summary(self):
        return (f"{len(self.errors_before)} DTD error(s) -> {len(self.errors_after)} after {len(self.fixes)} "
                f"automatic fix(es) in {self.passes} pass(es)")


def _path(el):
    out = []
    while el is not None and isinstance(el.tag, str):
        out.append(el.tag)
        el = el.getparent()
    return "/".join(reversed(out))


def _qname(el):
    """Element name as the DTD declares it (mml:math)."""
    tag = el.tag
    if tag.startswith("{http://www.w3.org/1998/Math/MathML}"):
        return "mml:" + tag.split("}", 1)[1]
    return tag


def _ancestor_of(a, b):
    p = b.getparent()
    while p is not None:
        if p is a:
            return True
        p = p.getparent()
    return False


class Fixer:
    def __init__(self, model, id_prefix="x"):
        self.m = model
        self.rep = FixReport()
        self._ids = itertools.count(1)
        self.id_prefix = id_prefix
        self.used_ids = set()

    # --------------------------------------------------------- utilities
    def note(self, rule, el, detail):
        self.rep.fixes.append((rule, _path(el), detail))

    def decl(self, el):
        return self.m.elements.get(_qname(el))

    def allows(self, parent, child_name):
        r = self.m.allows_child(_qname(parent), child_name)
        return bool(r)

    def is_inline(self, name):
        return name in INLINE_HINT or bool(self.m.allows_child("p", name)) and name not in (
            "list", "fig", "table-wrap", "disp-quote", "disp-formula", "boxed-text", "def-list", "verse-group",
            "speech", "statement", "preformat", "code", "graphic", "media", "array", "chem-struct-wrap")

    def new_id(self, tag):
        while True:
            i = f"{self.id_prefix}-{tag}{next(self._ids):03d}"
            if i not in self.used_ids:
                self.used_ids.add(i)
                return i

    # --------------------------------------------------------- one pass
    def run_pass(self, root):
        changed = 0
        self.used_ids |= {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}
        for el in list(root.iter()):
            if not isinstance(el.tag, str) or el.getparent() is None and el is not root:
                continue
            changed += self.fix_unknown(el)
            changed += self.fix_attributes(el)
        for el in list(root.iter()):
            if isinstance(el.tag, str):
                changed += self.fix_text(el)
        for el in list(root.iter()):
            if isinstance(el.tag, str):
                changed += self.fix_children(el)
        for el in list(root.iter()):
            if isinstance(el.tag, str):
                changed += self.fix_flow(el)
        for el in list(root.iter()):
            if isinstance(el.tag, str):
                changed += self.fix_required(el)
                changed += self.fix_order(el)
        return changed

    # --------------------------------------------------------- rules
    def fix_unknown(self, el):
        if self.decl(el) is not None or not isinstance(el.tag, str) or el.tag.startswith("{"):
            return 0
        old = el.tag
        parent = el.getparent()
        new = RENAMES.get(old)
        if new and self.m.has_element(new) and (parent is None or self.allows(parent, new)):
            el.tag = new
            self.note("rename", el, f"<{old}> -> <{new}>")
            return 1
        if parent is None:
            return 0
        if self.allows(parent, "named-content") and (len(el) == 0 or all(
                self.is_inline(c.tag) for c in el if isinstance(c.tag, str))):
            el.tag = "named-content"
            el.set("content-type", old)
            self.note("unknown element", el, f"<{old}> kept as <named-content content-type=\"{old}\">")
            return 1
        if self.allows(parent, "p") and all(self.is_inline(c.tag) for c in el if isinstance(c.tag, str)):
            el.tag = "p"
            el.set("content-type", old)
            self.note("unknown element", el, f"<{old}> kept as <p content-type=\"{old}\">")
            return 1
        if self.allows(parent, "boxed-text"):
            el.tag = "boxed-text"
            el.set("content-type", old)
            self.note("unknown element", el, f"<{old}> kept as <boxed-text content-type=\"{old}\">")
            return 1
        return 0

    def fix_attributes(self, el):
        d = self.decl(el)
        if d is None:
            return 0
        changed = 0
        for (etag, attr), mapping in VALUE_MAP.items():   # well-known synonyms, even where the DTD allows any value
            if el.tag == etag and el.get(attr) in mapping:
                self.note("attribute value", el, f"{attr}=\"{el.get(attr)}\" -> \"{mapping[el.get(attr)]}\"")
                el.set(attr, mapping[el.get(attr)])
                changed += 1
        for name in list(el.attrib):
            local = name.split("}", 1)[-1]
            key = "xml:" + local if name.startswith("{" + XML_NS) else (
                "xlink:" + local if name.startswith("{http://www.w3.org/1999/xlink}") else name)
            ad = d.attributes.get(key)
            if ad is None:
                value = el.attrib.pop(name)
                if name not in INTERNAL_ATTRS:
                    self.rep.removed_attributes.append((_path(el), name, value))
                    self.note("attribute", el, f"{name}=\"{value}\" removed (not allowed on <{el.tag}>)")
                changed += 1
                continue
            if ad.values and el.get(name) not in ad.values:
                mapped = VALUE_MAP.get((el.tag, name), {}).get(el.get(name))
                if mapped in ad.values:
                    self.note("attribute value", el, f"{name}=\"{el.get(name)}\" -> \"{mapped}\"")
                    el.set(name, mapped)
                else:
                    value = el.attrib.pop(name)
                    self.rep.removed_attributes.append((_path(el), name, value))
                    self.note("attribute value", el, f"{name}=\"{value}\" removed (allowed: {', '.join(ad.values)})")
                changed += 1
        for ad in d.attributes.values():
            if ad.default != "required":
                continue
            key = ad.name
            if key.startswith("xml:") or key.startswith("xlink:"):
                ns = XML_NS if key.startswith("xml:") else "http://www.w3.org/1999/xlink"
                attr = "{" + ns + "}" + key.split(":", 1)[1]
            else:
                attr = key
            if el.get(attr) is not None:
                continue
            if key == "id":
                value = self.new_id(el.tag.replace("-", ""))
            elif ad.values:
                value = ATTR_DEFAULTS.get(key) if ATTR_DEFAULTS.get(key) in ad.values else ad.values[0]
            else:
                value = ATTR_DEFAULTS.get(key)
            if value is None:
                continue
            el.set(attr, value)
            self.note("required attribute", el, f"{key}=\"{value}\" added")
            changed += 1
        return changed

    def fix_text(self, el):
        """Text runs inside an element that takes no text."""
        d = self.decl(el)
        if d is None or d.type in ("mixed", "any") or len(el) == 0 and not (el.text or "").strip():
            return 0
        if not ((el.text or "").strip() or any((c.tail or "").strip() for c in el)):
            return 0
        if not self.allows(el, "p"):
            return 0
        # wrap every maximal run of text + inline children in a <p>
        nodes = []
        if el.text:
            nodes.append(("text", el.text))
        for c in el:
            nodes.append(("el", c))
            if c.tail:
                nodes.append(("tail", c))
        el.text = None
        runs, cur = [], []
        for kind, x in nodes:
            if kind in ("text", "tail") or (kind == "el" and isinstance(x.tag, str) and not self.allows(el, _qname(x))
                                            and self.is_inline(_qname(x))):
                cur.append((kind, x))
            else:
                if cur:
                    runs.append(cur)
                    cur = []
                runs.append([(kind, x)])
        if cur:
            runs.append(cur)
        out = []
        changed = 0
        for run in runs:
            if len(run) == 1 and run[0][0] == "el":
                out.append(run[0][1])
                continue
            text = "".join(x if k == "text" else (x.tail or "") if k == "tail" else "" for k, x in run)
            if not text.strip() and not any(k == "el" for k, _ in run):
                # whitespace only: keep it as formatting whitespace
                continue
            p = etree.Element("p")
            last = None
            for k, x in run:
                if k == "text":
                    p.text = (p.text or "") + x
                elif k == "el":
                    x.tail = None
                    p.append(x)
                    last = x
                else:  # tail of an element that stays outside the run
                    if last is not None:
                        last.tail = (last.tail or "") + (x.tail or "")
                    else:
                        p.text = (p.text or "") + (x.tail or "")
            out.append(p)
            changed += 1
        for c in list(el):
            c.tail = None
            el.remove(c)
        for c in out:
            el.append(c)
        if changed:
            self.note("text in block", el, f"{changed} text run(s) wrapped in <p>")
        return changed

    def fix_children(self, el):
        changed = 0
        d = self.decl(el)
        if d is None:
            return 0
        for c in list(el):
            if not isinstance(c.tag, str) or c.getparent() is not el:
                continue
            name = _qname(c)
            if self.allows(el, name) or self.m.elements.get(name) is None:
                continue
            # a page marker (no text) in a place that cannot hold it -> the nearest
            # paragraph before it in document order (or the next one)
            if name == "target" and not (c.text or "").strip() and not len(c):
                dest = self.nearest_holder(c)
                if dest is not None:
                    self.detach(c)
                    dest[0].insert(len(dest[0]), c) if dest[1] == "end" else dest[0].insert(0, c)
                    if dest[1] == "start":
                        c.tail, dest[0].text = dest[0].text, None
                    self.note("page marker", c, f"moved into the {'preceding' if dest[1] == 'end' else 'following'} "
                                                f"<{dest[0].tag}>")
                    changed += 1
                    continue
            # inline element where blocks go -> wrap in a paragraph
            if self.allows(el, "p") and self.allows_in_p(name):
                p = etree.Element("p")
                el.insert(el.index(c), p)
                p.append(c)
                p.tail, c.tail = c.tail, None
                self.note("inline in block", c, f"<{name}> wrapped in <p>")
                changed += 1
                continue
            # block element inside a paragraph-like parent -> split the parent around it
            gp = el.getparent()
            if gp is not None and self.allows(gp, name) and self.allows(gp, _qname(el)):
                self.split_around(el, c)
                self.note("block in paragraph", c, f"<{_qname(el)}> split around <{name}>")
                changed += 1
                return changed
            # last resort in a text flow: a text element that does not belong here
            # (e.g. <publisher-name> in a body) becomes a paragraph, its text kept
            if self.allows(el, "p") and all(self.is_inline(_qname(x)) for x in c if isinstance(x.tag, str)) \
                    and c.tag not in ("sec", "body", "back", "front"):
                c.tag = "p"
                c.attrib.clear()
                c.set("content-type", name)
                self.note("misplaced element", c, f"<{name}> kept as <p content-type=\"{name}\">")
                changed += 1
        return changed

    def nearest_holder(self, marker):
        root = marker.getroottree().getroot()
        order = [e for e in root.iter() if isinstance(e.tag, str)]
        i = order.index(marker)
        for e in reversed(order[:i]):
            if e.tag == "p" and not _ancestor_of(e, marker) and self.allows(e, "target"):
                return e, "end"
        for e in order[i + 1:]:
            if e.tag == "p" and self.allows(e, "target"):
                return e, "start"
        return None

    @staticmethod
    def detach(el):
        parent = el.getparent()
        if el.tail:
            prev = el.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + el.tail
            else:
                parent.text = (parent.text or "") + el.tail
        el.tail = None
        parent.remove(el)

    def allows_in_p(self, name):
        return bool(self.m.allows_child("p", name))

    def split_around(self, parent, child):
        """<p>a <list/> b</p> -> <p>a </p><list/><p>b</p> (empty halves are
        not created)."""
        gp = parent.getparent()
        idx = gp.index(parent)
        after = etree.Element(parent.tag, dict(parent.attrib))
        after.attrib.pop("id", None)
        after.text = child.tail
        child.tail = None
        for s in list(child.itersiblings()):
            after.append(s)
        parent.remove(child)
        gp.insert(idx + 1, child)
        child.tail = None
        if (after.text or "").strip() or len(after):
            gp.insert(idx + 2, after)
            after.tail, parent.tail = parent.tail, None
        else:
            child.tail, parent.tail = parent.tail, None
        if not (parent.text or "").strip() and not len(parent):
            child.tail = (child.tail or "")
            gp.remove(parent)

    def fix_required(self, el):
        d = self.decl(el)
        if d is None or d.type != "element":
            return 0
        present = {_qname(c) for c in el if isinstance(c.tag, str)}
        changed = 0
        for req in d.required_children:
            if req in present:
                continue
            rd = self.m.elements.get(req)
            if rd is None or rd.required_children or rd.type == "element" and rd.required_children:
                continue
            if any(a.default == "required" and a.name != "id" for a in rd.attributes.values()):
                continue
            new = etree.Element(req)
            el.insert(self._insert_pos(el, req), new)
            self.note("required child", el, f"empty <{req}> added")
            changed += 1
        return changed

    def _rank(self, el):
        """content-model position of every child name (top-level sequence)."""
        d = self.decl(el)
        if d is None or d.content is None or d.content.kind != "seq":
            return None
        rank = {}
        from core.tag_knowledge.dtd_model import _names
        for i, node in enumerate(d.content.children):
            for n in _names(node):
                rank.setdefault(n, i)
        return rank

    def _insert_pos(self, el, name):
        rank = self._rank(el)
        if rank is None or name not in rank:
            return 0
        r = rank[name]
        pos = 0
        for i, c in enumerate(el):
            if isinstance(c.tag, str) and rank.get(_qname(c), 99) <= r:
                pos = i + 1
        return pos

    def fix_flow(self, el):
        """In a text-flow container, a block that follows a <sec> belongs to
        that section (reading order kept): move it to the end of the last
        (deepest) preceding section."""
        if _qname(el) not in ("body", "sec", "app", "named-book-part-body", "book-app", "notes", "ack",
                              "boxed-text", "abstract"):
            return 0
        if self.m.validate_element(el) if hasattr(self.m, "validate_element") else False:
            return 0
        changed = 0
        last_sec = None
        for c in list(el):
            if not isinstance(c.tag, str):
                continue
            if c.tag == "sec":
                last_sec = c
                continue
            if last_sec is not None and c.tag not in ("fn-group", "glossary", "ref-list", "sig-block") and \
                    self.allows(last_sec, _qname(c)):
                target = last_sec
                while True:
                    inner = [x for x in target if isinstance(x.tag, str) and x.tag == "sec"]
                    if not inner or target[-1] is not inner[-1]:
                        break
                    target = inner[-1]
                if not self.allows(target, _qname(c)):
                    target = last_sec
                tail = c.tail
                el.remove(c)
                c.tail = tail
                target.append(c)
                changed += 1
        if changed:
            self.note("flow", el, f"{changed} block(s) after a section moved into that section (reading order "
                                  f"kept)")
        return changed

    def fix_order(self, el):
        if _qname(el) in FLOW_CONTAINERS:
            return 0
        rank = self._rank(el)
        if rank is None:
            return 0
        kids = [c for c in el if isinstance(c.tag, str)]
        if len(kids) < 2 or any(_qname(c) not in rank for c in kids):
            return 0
        if not (el.text or "").strip() and all(not (c.tail or "").strip() for c in kids):
            order = sorted(kids, key=lambda c: rank[_qname(c)])
            if order != kids:
                for c in kids:
                    el.remove(c)
                for c in order:
                    el.append(c)
                self.note("order", el, "children reordered to the DTD sequence")
                return 1
        return 0


def fix(root, model, max_passes=6, id_prefix="x"):
    """Repairs root in place; returns a FixReport."""
    fx = Fixer(model, id_prefix)
    rep = fx.rep
    rep.errors_before = model.validate(root)
    if not rep.errors_before:
        rep.errors_after = []
        return rep
    signature = content_signature(root)
    for _ in range(max_passes):
        snapshot = deepcopy(root)
        n_fixes = len(rep.fixes)
        changed = fx.run_pass(root)
        rep.passes += 1
        if content_signature(root) != signature:
            # never accept a pass that changed the words - undo it
            rep.reverted.append(f"pass {rep.passes}: text would change - undone")
            del rep.fixes[n_fixes:]
            _restore(root, snapshot)
            break
        if not changed or not model.validate(root):
            break
    rep.errors_after = model.validate(root)
    return rep


def _restore(root, snapshot):
    root.text, root.tail = snapshot.text, snapshot.tail
    root.attrib.clear()
    root.attrib.update(snapshot.attrib)
    for c in list(root):
        root.remove(c)
    for c in snapshot:
        root.append(c)
