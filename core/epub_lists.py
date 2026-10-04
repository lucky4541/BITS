"""List structure for EPUB / CUPEPUB XHTML generation.

Before this module, every list zone was emitted as a bare element named
after its own tag - a CUPEPUB "NumList" zone became a literal <numlist>,
"BullList" a literal <bulllist>, and so on - because no Mapping.xml rule
(and no generator code) ever turned those intermediate tag names into real
XHTML lists. Consecutive items were never grouped either, so every item was
its own one-item "list", and nested (level 2+) items were never nested.

build_lists() runs once on the intermediate tree (core/epub_xml_generator.
EpubXmlGenerator.generate, BEFORE Mapping.xml) and turns every run of
consecutive list-item elements into proper, grouped, nested XHTML lists,
using the class names the CUP template.css already styles:

    NumList         -> <ol class="decimal">
    UpperAlphaList  -> <ol class="upper-alpha">
    LowerAlphaList  -> <ol class="lower-alpha">
    UpperRomanList  -> <ol class="upper-roman">
    LowerRomanList  -> <ol class="lower-roman">
    BullList        -> <ul class="bullet">
    PlainList       -> <ul class="plain">
    CustomList      -> <ul class="customlist">
    ListPara        -> stays <listpara> (Mapping.xml turns it into
                       <p class="listpara">) but is moved INSIDE the list
                       item it continues
    (EPUB profile)  a bare run of <li> with no <ol>/<ul> parent -> <ul>

Rules:
  - one <li> per item zone, in reading order; consecutive items of the same
    list kind share one <ol>/<ul>; a different kind at the same level
    starts a new list;
  - nesting: an explicit level suffix (numlist_2, BullList_3 ... - the
    CUPLookup.xml level variants) wins; otherwise an item whose zone starts
    clearly further right (INDENT_STEP pt) than the current level's items
    opens a nested list inside the previous <li>, and an item back at an
    outer indent closes the inner list(s);
  - a page-break marker (<span role="doc-pagebreak">) between two items
    stays at its position, inside the preceding <li>, so the list is not
    broken in two by a page boundary;
  - the printed marker ("1.", "a)", "iv.", "•") is removed from the item
    text only when the list style itself renders a marker (decimal/alpha/
    roman/bullet); PlainList and CustomList keep the source marker, since
    their style is list-style-type: none.

Never drops, duplicates or reorders content: elements are only re-parented.
"""
import re

from lxml import etree

INDENT_STEP = 8.0  # pt - an item this much further right than its level opens a nested level

# base tag -> (list element, class)
LIST_KINDS = {
    "numlist": ("ol", "decimal"),
    "upperalphalist": ("ol", "upper-alpha"),
    "loweralphalist": ("ol", "lower-alpha"),
    "upperromanlist": ("ol", "upper-roman"),
    "lowerromanlist": ("ol", "lower-roman"),
    "bulllist": ("ul", "bullet"),
    "plainlist": ("ul", "plain"),
    "customlist": ("ul", "customlist"),
}
# CUPLookup.xml "style___X" attribute -> class (e.g. NumList_2 style___A)
_STYLE_CLASS = {"1": "decimal", "A": "upper-alpha", "a": "lower-alpha", "I": "upper-roman", "i": "lower-roman"}
LIST_PARA_TAG = "listpara"

_ITEM_TAG_RE = re.compile(r"^(" + "|".join(LIST_KINDS) + r")(?:_(\d))?$")

_MARKERS = {
    "decimal": re.compile(r"^\s*\(?\d{1,3}[.)]?\s+|^\s*\(?\d{1,3}[.)]\s*"),
    "upper-alpha": re.compile(r"^\s*\(?[A-Z][.)]\s*"),
    "lower-alpha": re.compile(r"^\s*\(?[a-z][.)]\s*"),
    "upper-roman": re.compile(r"^\s*\(?[IVXLCDM]{1,7}[.)]\s*"),
    "lower-roman": re.compile(r"^\s*\(?[ivxlcdm]{1,7}[.)]\s*"),
    "bullet": re.compile(r"^\s*[•·●○◦▪■□‣⁃\-–—*]\s*"),
}


def item_kind(el):
    """(list_tag, class, explicit_level_or_None) for a list-item element,
    else None."""
    if not isinstance(el.tag, str):
        return None
    m = _ITEM_TAG_RE.match(el.tag)
    if not m:
        return None
    list_tag, cls = LIST_KINDS[m.group(1)]
    style = el.get("style") or ""
    if style.startswith("style___") and list_tag == "ol":
        cls = _STYLE_CLASS.get(style[len("style___"):], cls)
    return list_tag, cls, (int(m.group(2)) if m.group(2) else None)


def _is_listpara(el):
    return isinstance(el.tag, str) and el.tag == LIST_PARA_TAG


def _is_pagebreak(el):
    return isinstance(el.tag, str) and el.tag == "span" and el.get("role") == "doc-pagebreak"


_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _marker_value(marker, cls):
    """The ordinal a stripped source marker stands for ("4." -> 4, "c)" ->
    3, "iv." -> 4), or None."""
    token = re.sub(r"[^0-9A-Za-z]", "", marker or "")
    if not token:
        return None
    if cls == "decimal" and token.isdigit():
        return int(token)
    if cls in ("upper-alpha", "lower-alpha") and len(token) == 1 and token.isalpha():
        return ord(token.lower()) - ord("a") + 1
    if cls in ("upper-roman", "lower-roman"):
        total, prev = 0, 0
        for ch in reversed(token.lower()):
            v = _ROMAN.get(ch)
            if v is None:
                return None
            total = total - v if v < prev else total + v
            prev = max(prev, v)
        return total or None
    return None


def _strip_marker(li, cls):
    """Removes the printed marker the list style renders itself; returns
    its ordinal value (or None)."""
    pat = _MARKERS.get(cls)
    if pat is None:
        return None
    if li.text and li.text.strip():
        m = pat.match(li.text)
        if m:
            li.text = li.text[m.end():]
            return _marker_value(m.group(0), cls)
        return None
    # Marker may sit inside the first inline child, e.g. <i>1. Title</i>
    if len(li) and li[0].text:
        m = pat.match(li[0].text)
        if m:
            li[0].text = li[0].text[m.end():]
            return _marker_value(m.group(0), cls)
    return None


def _make_li(item_el, cls, src_attr):
    li = etree.Element("li")
    if item_el.get(src_attr):
        li.set(src_attr, item_el.get(src_attr))
    li.text = (item_el.text or "").lstrip()
    for child in list(item_el):
        li.append(child)
    if not li.text and len(li) and li[0].text:
        li[0].text = li[0].text.lstrip()
    value = _strip_marker(li, cls)
    if value is not None:
        li.set("_marker_value", str(value))
    return li


def _unwrap_empty_containers(root):
    """A list zone drawn AROUND its item zones (container + children) has no
    text of its own: its children are the items. Replace it by its children
    so they group like any other run."""
    for el in list(root.iter()):
        if item_kind(el) is None or (el.text or "").strip():
            continue
        kids = list(el)
        if kids and all(item_kind(k) is not None or (isinstance(k.tag, str) and k.tag == LIST_PARA_TAG)
                        or _is_pagebreak(k) for k in kids):
            parent = el.getparent()
            if parent is None:
                continue
            idx = parent.index(el)
            for offset, k in enumerate(kids):
                parent.insert(idx + offset, k)
            if el.tail:
                kids[-1].tail = (kids[-1].tail or "") + el.tail
            parent.remove(el)


def build_lists(root, zone_x0=None, src_attr="data-zt-src"):
    """zone_x0: callable(zone_id) -> left edge of that zone's bbox (or None)
    for indentation-based nesting."""
    _unwrap_empty_containers(root)
    for parent in list(root.iter()):
        if not isinstance(parent.tag, str) or parent.tag in ("ol", "ul"):
            continue
        _group_children(parent, zone_x0, src_attr)
    _wrap_bare_li_runs(root)
    _apply_start_numbers(root)


def _apply_start_numbers(root):
    """A numbered list that does not begin at 1 in the source (e.g. it
    resumes as "4." after a page break or an interruption) keeps its real
    numbering via the standard start="" attribute."""
    for lst in root.iter("ol"):
        items = [c for c in lst if isinstance(c.tag, str) and c.tag == "li"]
        if items and lst.get("class") in ("decimal", "upper-alpha", "lower-alpha", "upper-roman", "lower-roman"):
            first = items[0].get("_marker_value")
            if first and first.isdigit() and int(first) > 1:
                lst.set("start", first)
    for li in root.iter("li"):
        li.attrib.pop("_marker_value", None)


def _group_children(parent, zone_x0, src_attr):
    children = list(parent)
    i = 0
    while i < len(children):
        if item_kind(children[i]) is None:
            i += 1
            continue
        # Collect one run: list items, any <listpara> (belongs to the item
        # before it) and page-break markers that sit BETWEEN list content.
        run = []
        j = i
        while j < len(children):
            el = children[j]
            if item_kind(el) is not None or _is_listpara(el):
                run.append(el)
                j += 1
            elif _is_pagebreak(el):
                k = j
                while k < len(children) and _is_pagebreak(children[k]):
                    k += 1
                if k < len(children) and (item_kind(children[k]) is not None or _is_listpara(children[k])):
                    run.extend(children[j:k])
                    j = k
                else:
                    break
            else:
                break
        insert_at = parent.index(run[0])
        tail_text = run[-1].tail
        for el in run:
            parent.remove(el)
            el.tail = None
        top_lists = _build_run(run, zone_x0, src_attr)
        for offset, lst in enumerate(top_lists):
            parent.insert(insert_at + offset, lst)
        if tail_text:
            top_lists[-1].tail = tail_text
        children = list(parent)
        i = insert_at + len(top_lists)


def _build_run(run, zone_x0, src_attr):
    top_lists = []
    # stack entries: {"list": el, "kind": (tag, cls), "indent": x0, "depth": n}
    stack = []

    def new_list(kind):
        lst = etree.Element(kind[0])
        lst.set("class", kind[1])
        return lst

    def last_li():
        for frame in reversed(stack):
            if len(frame["list"]):
                return frame["list"][-1]
        return None

    for el in run:
        kind_info = item_kind(el)
        if kind_info is None:
            li = last_li()
            if li is not None:
                li.append(el)  # listpara / page break stays with the item it follows
            continue
        tag, cls, explicit = kind_info
        kind = (tag, cls)
        x0 = zone_x0(el.get(src_attr)) if zone_x0 and el.get(src_attr) else None

        if explicit is not None:
            depth = max(1, explicit)
            while stack and stack[-1]["depth"] > depth:
                stack.pop()
            if stack and stack[-1]["depth"] < depth:
                stack.append(_open_nested(stack, kind, depth, x0, new_list, last_li))
        else:
            if not stack:
                depth = 1
            elif x0 is not None and stack[-1]["indent"] is not None and x0 > stack[-1]["indent"] + INDENT_STEP \
                    and len(stack[-1]["list"]):
                stack.append(_open_nested(stack, kind, stack[-1]["depth"] + 1, x0, new_list, last_li))
            else:
                while len(stack) > 1 and x0 is not None and stack[-1]["indent"] is not None \
                        and x0 < stack[-1]["indent"] - INDENT_STEP:
                    stack.pop()

        if not stack:
            frame = {"list": new_list(kind), "kind": kind, "indent": x0, "depth": 1}
            stack.append(frame)
            top_lists.append(frame["list"])
        elif stack[-1]["kind"] != kind:
            # Different list kind at the same level -> a new sibling list.
            old = stack.pop()
            frame = {"list": new_list(kind), "kind": kind, "indent": x0 if x0 is not None else old["indent"],
                     "depth": old["depth"]}
            holder = old["list"].getparent()
            if holder is None:
                top_lists.append(frame["list"])
            else:
                holder.append(frame["list"])
            stack.append(frame)
        if stack[-1]["indent"] is None:
            stack[-1]["indent"] = x0
        stack[-1]["list"].append(_make_li(el, cls, src_attr))
    return top_lists


def _open_nested(stack, kind, depth, x0, new_list, last_li):
    lst = new_list(kind)
    holder = last_li()
    if holder is not None:
        holder.append(lst)
    else:
        stack[-1]["list"].append(lst)
    return {"list": lst, "kind": kind, "indent": x0, "depth": depth}


def _wrap_bare_li_runs(root):
    """EPUB profile: <li> zones drawn without a List zone around them."""
    for parent in list(root.iter()):
        if not isinstance(parent.tag, str) or parent.tag in ("ol", "ul") or parent.tag.startswith("__"):
            continue
        children = list(parent)
        i = 0
        while i < len(children):
            el = children[i]
            if not (isinstance(el.tag, str) and el.tag == "li"):
                i += 1
                continue
            j = i
            while j < len(children) and isinstance(children[j].tag, str) and children[j].tag == "li":
                j += 1
            run = children[i:j]
            ul = etree.Element("ul")
            parent.insert(parent.index(run[0]), ul)
            for li in run:
                ul.append(li)  # lxml moves the element (and its tail) into the list
            children = list(parent)
            i = parent.index(ul) + 1
