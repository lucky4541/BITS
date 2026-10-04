"""Real XPath/element-tree processor for the external Mapping.xml file (the
source-of-truth transformation rules provided by the user - see
profiles/epub_profile.json's mapping_xml_path). Deliberately NOT a string-
replace: every rule is applied via lxml XPath evaluation and proper element
construction/insertion, per spec section 9.

Mapping.xml's <tag> rules come in four shapes, inferred from the file's own
structure and naming (no formal spec was provided for the "type" values -
this is a best-effort, documented interpretation; see the docstring on each
_apply_* method below for exactly what was inferred and why):

  - no "type" attribute: plain rename/replace. The matched element is
    replaced by a deep copy of <replace_ele>'s template, with any
    <attrib findAttr="X" replAttr="Y"/> children copying the matched
    element's attribute X into the template's "{0}" placeholder in
    attribute Y, and the matched element's own text/children moved into
    whichever template leaf isn't a pure image placeholder (see
    _find_content_target).
  - type="enclose": `find` is a bare "|"-separated list of local element
    names (not an XPath). Every maximal run of CONSECUTIVE sibling
    elements whose tag is in that name list gets wrapped in a new copy of
    the template (e.g. "img|figtitle|caption|figsource" -> wraps that run
    in <figure>...</figure>). Rules apply in file order, so a later
    enclose rule can further wrap elements that an earlier one already
    nested (figtitle|caption|figsource -> <figcaption> runs after
    img|figtitle|caption|figsource -> <figure>, nesting figcaption inside
    the figure it was just moved into).
  - type="addFirst": `find` IS an XPath (has "//"). A deep copy of the
    template is inserted as the FIRST child of every match (e.g. a
    <break/> before an eqn_img/protitle/etc).
  - type="coverAbove": `find` is an XPath over heading-like elements
    (h1..h6, boxsec_1..boxsec_3). The matched element is renamed to the
    template tag AND "covers above" its own following siblings up to (not
    including) the next sibling of equal-or-higher heading level, which
    become ITS children instead - i.e. this is where section nesting
    actually happens, not pre-baked by the zoning/hierarchy engine (see
    core/epub_xml_generator.py's module docstring for why).
  - type="prevEle", no <replace_ele> at all (self-closing <tag .../>):
    `find` is a bare "|"-separated local-name list. Each match is moved to
    become the LAST CHILD of its immediately preceding sibling (e.g. a
    flat run of li/ol/ul-ish TOC entry tags gets nested into the list
    element right before it). A match with no preceding sibling is left
    in place rather than dropped.

A <tag> with no `find=` attribute at all (Mapping.xml has exactly one -
a component/@type -> display-title lookup table, not a transformation) is
skipped, not treated as an error.
"""
import copy
import os
import re

from lxml import etree

_LEVEL_RE = re.compile(r"(\d+)$")


class MappingLoadError(Exception):
    pass


class MappingApplyError(Exception):
    pass


def _local_names(find_expr: str) -> list[str]:
    return [n.strip() for n in find_expr.split("|") if n.strip()]


def _heading_level(tag: str) -> int:
    m = _LEVEL_RE.search(tag or "")
    return int(m.group(1)) if m else 0


class MappingEngine:
    def __init__(self, mapping_xml_path: str):
        self.path = mapping_xml_path
        self.rules: list[dict] = []
        # Non-fatal per-rule application problems (spec section 25: report
        # source element / rule / error rather than silently corrupting
        # output) - collected during apply(), never raised as exceptions,
        # since one bad rule/match must not abort the whole document.
        self.apply_errors: list[str] = []

    def load(self) -> "MappingEngine":
        if not self.path:
            raise MappingLoadError("No Mapping.xml path is configured for the EPUB profile.")
        if not os.path.isfile(self.path):
            raise MappingLoadError(f"Mapping.xml not found: {self.path}")
        parser = etree.XMLParser(remove_comments=True, recover=False)
        try:
            tree = etree.parse(self.path, parser)
        except etree.XMLSyntaxError as e:
            raise MappingLoadError(
                f"Mapping.xml is not valid XML - line {e.lineno}, column {e.position[1] if e.position else '?'}: {e.msg}")
        root = tree.getroot()
        for tag_el in root.findall("tag"):
            find_expr = tag_el.get("find")
            if not find_expr:
                continue  # not a transformation rule (e.g. the component/@type title lookup table)
            replace_ele = tag_el.find("replace_ele")
            attrib_els = tag_el.findall("attrib")
            self.rules.append({
                "find": find_expr,
                "type": tag_el.get("type"),
                "template_roots": list(replace_ele) if replace_ele is not None else [],
                "attribs": [(a.get("findAttr"), a.get("replAttr")) for a in attrib_els
                            if a.get("findAttr") and a.get("replAttr")],
            })
        if not self.rules:
            raise MappingLoadError(f"Mapping.xml at {self.path} contains no usable <tag find=\"...\"> rules.")
        return self

    def apply(self, intermediate_root):
        """Applies every rule, in file order, directly against
        intermediate_root and returns the resulting root (NOT guaranteed to
        be the same object passed in - see _apply_root_rule: the root's own
        <component type="X"> wrapper can legitimately be REPLACED by a
        differently-named element, e.g. <body>, which callers must use
        going forward). Errors from an individual rule are collected in
        self.apply_errors, not raised - one malformed rule must not prevent
        every other rule from running."""
        self.apply_errors = []
        try:
            intermediate_root = self._apply_root_rule(intermediate_root)
        except Exception as e:  # noqa: BLE001
            self.apply_errors.append(f"Root element rule: {e}")
        for rule in self.rules:
            try:
                self._apply_rule(intermediate_root, rule)
            except Exception as e:  # noqa: BLE001 - reported, not fatal
                self.apply_errors.append(
                    f"Rule find={rule['find']!r} type={rule.get('type')!r}: {e}")
        self._repair_orphan_aria_labelledby(intermediate_root)
        return intermediate_root

    def _apply_root_rule(self, root):
        """A plain-replace rule matching the tree's OWN root element (e.g.
        Mapping.xml's <tag find="//component[@type='chapter']">...</tag>)
        can never fire through the normal per-rule loop: _apply_plain_replace
        requires match.getparent() to know where to reinsert the
        replacement, and the root has none by definition - confirmed by
        direct inspection as the reason <component type="..."> was leaking
        untouched into final XHTML (every component[@type=X] rule in
        Mapping.xml exists specifically to eliminate it, they just could
        never reach it). Root is temporarily given a throwaway parent so
        the EXACT SAME plain-replace code path (same template deep-copy,
        same <attrib> substitution) runs unchanged; only a rule with no
        "type" (a plain rename/replace - how every component[@type=X] rule
        is actually authored) is eligible. Returns the new root (wrapper's
        remaining child) whether or not a rule actually matched."""
        wrapper = etree.Element("__root_wrapper__")
        wrapper.append(root)
        for rule in self.rules:
            if rule.get("type") is not None:
                continue
            try:
                matches = wrapper.xpath(rule["find"])
            except etree.XPathEvalError:
                continue
            if root in matches:
                self._apply_plain_replace(wrapper, rule["find"], rule["template_roots"], rule["attribs"])
                break
        return wrapper[0]

    @staticmethod
    def _repair_orphan_aria_labelledby(root):
        """Several Mapping.xml templates write an aria-labelledby="X{0}"-
        style placeholder (or a literal id like "c{0}") with NO <attrib>
        substitution wired up anywhere in the file for it - confirmed by
        inspection (e.g. the component[@type='chapter'] rule's own
        aria-labelledby="c{0}"), so read literally it leaves either a
        broken reference or a literal "{0}" behind. The standard EPUB idiom
        this is reaching for - a <section> describing itself via
        aria-labelledby pointing at its OWN heading - is repaired
        generically here: any element whose aria-labelledby doesn't
        resolve to a real id anywhere in the document gets it replaced
        with the id of the first heading (h1..h6) found inside it
        (assigning that heading a deterministic id first if it doesn't
        have one), or removed entirely if it contains no heading at all."""
        all_ids = {el.get("id") for el in root.iter() if el.get("id")}
        counter = 0
        for el in root.iter():
            target = el.get("aria-labelledby")
            if not target or target in all_ids:
                continue
            heading = None
            for h_el in el.iter():
                local = etree.QName(h_el).localname if isinstance(h_el.tag, str) and "}" in h_el.tag else h_el.tag
                if local in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    heading = h_el
                    break
            if heading is None:
                del el.attrib["aria-labelledby"]
                continue
            h_id = heading.get("id")
            if not h_id or "{" in h_id:
                counter += 1
                h_id = f"_auto_heading_{counter}"
                while h_id in all_ids:
                    counter += 1
                    h_id = f"_auto_heading_{counter}"
                heading.set("id", h_id)
                all_ids.add(h_id)
            el.set("aria-labelledby", h_id)

    def _apply_rule(self, root, rule):
        rtype = rule["type"]
        find_expr = rule["find"]
        if rtype == "enclose":
            if rule["template_roots"]:
                self._apply_enclose(root, _local_names(find_expr), rule["template_roots"][0])
        elif rtype == "prevEle":
            # DISABLED: this rule's match list includes bare "li"/"ol"/"ul"
            # (alongside TOC-specific tags like tocfm/tocsec_1/parttocentry
            # this pipeline doesn't build yet - TOC auto-generation is
            # explicitly out of scope, see core/epub_xml_generator.py). It
            # was designed to fold UNWRAPPED flat TOC-entry elements into a
            # preceding wrapper - but core/epub_xml_generator.py's own
            # footnote/endnote/reference/index-entry sections already build
            # correctly-nested <ol>/<ul>/<li> structures directly, using
            # those same generic tag names for an unrelated purpose. Because
            # _apply_prev_ele mutates the tree while a single root.iter()
            # snapshot is still being walked, an EARLIER match's relocation
            # changes what a LATER "parent" in that same snapshot sees as
            # its own children - reproduced concretely: a real 3-footnote
            # document (from a book the user supplied) had its 2nd and 3rd
            # <li> silently spliced INSIDE an unrelated <i> element from the
            # 1st footnote's own content, well-formed XML but catastrophically
            # wrong nesting. Kept implemented (_apply_prev_ele below) for a
            # future TOC-generation feature that actually produces the
            # tocfm/tocsec_N-style tags this rule was written for.
            pass
        elif rtype == "addFirst":
            if rule["template_roots"]:
                self._apply_add_first(root, find_expr, rule["template_roots"][0])
        elif rtype == "coverAbove":
            # SUPERSEDED: real production output (confirmed across two
            # different books) keeps h1..h6 as literal heading elements
            # wrapped in <section aria-labelledby="..."> rather than
            # renamed to <sec1>/<sec2>/etc - the opposite of a literal
            # reading of this rule. core/epub_xml_generator.py now builds
            # that (verified-correct) nesting itself via core.hierarchy
            # BEFORE Mapping.xml ever runs, so applying this rule here
            # would just re-mangle headings it already got right. Kept
            # implemented (_apply_cover_above below) rather than deleted,
            # in case a future sample shows it's genuinely needed for some
            # other coverAbove-typed tag this profile doesn't use yet.
            pass
        else:
            self._apply_plain_replace(root, find_expr, rule["template_roots"], rule["attribs"])

    # ---------------------------------------------------------- plain replace
    @staticmethod
    def _find_content_target(el):
        """First empty, non-image leaf in el's subtree (depth-first,
        document order) - where the matched element's own text/children get
        moved into. An "image leaf" (has a `src` attribute) never receives
        content: those templates are pure image placeholders, and the
        zones that hit them (uncapfig, tblimage, ...) never have text
        children of their own anyway."""
        if len(el) == 0:
            return None if "src" in el.attrib else el
        for child in el:
            target = MappingEngine._find_content_target(child)
            if target is not None:
                return target
        return None

    def _apply_plain_replace(self, root, find_expr, template_roots, attribs):
        if not template_roots:
            return
        for match in list(root.xpath(find_expr)):
            parent = match.getparent()
            if parent is None:
                continue
            new_roots = [copy.deepcopy(t) for t in template_roots]
            for find_attr, repl_attr in attribs:
                src_val = match.get(find_attr)
                if src_val is None:
                    continue
                for new_root in new_roots:
                    for el in new_root.iter():
                        cur = el.get(repl_attr)
                        if cur is not None and "{0}" in cur:
                            el.set(repl_attr, cur.replace("{0}", src_val))
            target = None
            for nr in new_roots:
                target = self._find_content_target(nr)
                if target is not None:
                    break
            if target is not None:
                target.text = match.text
                for c in match:
                    target.append(copy.deepcopy(c))
            idx = list(parent).index(match)
            tail = match.tail
            parent.remove(match)
            for offset, nr in enumerate(new_roots):
                parent.insert(idx + offset, nr)
            if new_roots:
                new_roots[-1].tail = tail

    # -------------------------------------------------------------- enclose
    def _apply_enclose(self, root, names, template_el):
        names = set(names)
        for parent in list(root.iter()):
            children = list(parent)
            i = 0
            while i < len(children):
                if children[i].tag in names:
                    j = i
                    while j < len(children) and children[j].tag in names:
                        j += 1
                    run = children[i:j]
                    wrapper = copy.deepcopy(template_el)
                    idx = list(parent).index(run[0])
                    for el in run:
                        parent.remove(el)
                        wrapper.append(el)
                    parent.insert(idx, wrapper)
                    children = list(parent)
                    i = idx + 1
                else:
                    i += 1

    # ------------------------------------------------------------- addFirst
    @staticmethod
    def _apply_add_first(root, find_expr, template_el):
        for match in list(root.xpath(find_expr)):
            match.insert(0, copy.deepcopy(template_el))

    # ----------------------------------------------------------- coverAbove
    @staticmethod
    def _apply_cover_above(root, find_expr, template_el):
        for match in list(root.xpath(find_expr)):
            parent = match.getparent()
            if parent is None:
                continue
            my_level = _heading_level(match.tag)
            siblings = list(parent)
            start = siblings.index(match)
            absorbed = []
            for sib in siblings[start + 1:]:
                lvl = _heading_level(sib.tag)
                if lvl and lvl <= my_level:
                    break
                absorbed.append(sib)
            new_el = copy.deepcopy(template_el)
            new_el.text = match.text
            for c in match:
                new_el.append(c)
            for sib in absorbed:
                parent.remove(sib)
                new_el.append(sib)
            new_el.tail = match.tail
            parent.replace(match, new_el)

    # -------------------------------------------------------------- prevEle
    @staticmethod
    def _apply_prev_ele(root, names):
        """Not currently called (see the DISABLED comment in _apply_rule)
        but kept correct for when a future TOC-generation feature actually
        produces the tocfm/tocsec_N-style tags this rule targets. The
        previous version re-fetched list(parent) as its live children after
        every move, which - combined with a single root.iter() snapshot
        computed up front for the OUTER loop - let an earlier move at one
        parent level change what a LATER parent (already queued in that
        same snapshot, e.g. an element that had just gained a new child)
        saw as ITS OWN children, cascading a match into a completely
        unrelated element several levels down. Fixed by freezing every
        parent's children ONCE, before any move happens, and skipping a
        child that a different (already-processed) parent already moved
        out from under this one."""
        names = set(names)
        frozen = [(parent, list(parent)) for parent in root.iter()]
        for parent, original_children in frozen:
            for i, child in enumerate(original_children):
                if child.tag in names and i > 0 and child.getparent() is parent:
                    prev = original_children[i - 1]
                    parent.remove(child)
                    prev.append(child)
