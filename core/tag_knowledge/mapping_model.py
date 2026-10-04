"""Structural knowledge read from the project's Mapping.xml.

Mapping.xml is the transformation from zone-level (intermediate) elements to
the final XHTML. Read structurally, its rules also describe the project's
document grammar:

  * enclose rules ("a|b|c" type=enclose) define FAMILIES - zone tags that
    belong together as consecutive siblings inside one generated wrapper
    (figure parts, poem parts, footnotes, table parts ...);
  * XPath steps ("//x/y") give parent -> child relationships;
  * coverAbove rules mark heading-like (section opening) elements;
  * prevEle rules mark elements that attach to the preceding sibling
    (list-item-like behaviour);
  * every replace template tells what the element BECOMES (element name,
    class, epub:type, role) - both a semantic hint for the element and the
    key for mapping final XHTML in a reference corpus back to zone tags.
"""
import os
import re
from dataclasses import dataclass, field

from lxml import etree

from core.tag_knowledge.tokens import tokenize

_STEP_RE = re.compile(r"[a-zA-Z_][\w\-]*")


@dataclass
class Family:
    members: list                  # zone-level element names, in rule order
    wrapper_tag: str = None
    wrapper_attrs: dict = field(default_factory=dict)


@dataclass
class MappingModel:
    path: str = ""
    families: list = field(default_factory=list)
    parent_child: set = field(default_factory=set)          # (parent, child)
    section_openers: set = field(default_factory=set)
    attach_to_previous: set = field(default_factory=set)
    add_first: set = field(default_factory=set)
    outputs: dict = field(default_factory=dict)              # element -> [ {tag, attrs} ]
    reverse: dict = field(default_factory=dict)              # (final_tag, class) -> element
    rule_count: int = 0
    errors: list = field(default_factory=list)

    # ------------------------------------------------------------ build
    @classmethod
    def load(cls, path: str) -> "MappingModel":
        model = cls(path=path)
        if not path or not os.path.isfile(path):
            model.errors.append(f"Mapping.xml not found: {path}")
            return model
        try:
            root = etree.parse(path, etree.XMLParser(remove_comments=True)).getroot()
        except etree.XMLSyntaxError as e:
            model.errors.append(f"Mapping.xml not well-formed: {e}")
            return model
        for tag_el in root.findall("tag"):
            find = tag_el.get("find")
            if not find:
                continue
            model.rule_count += 1
            kind = tag_el.get("type")
            repl = tag_el.find("replace_ele")
            templates = [t for t in (list(repl) if repl is not None else []) if isinstance(t.tag, str)]
            if kind == "enclose":
                names = [n.strip() for n in find.split("|") if n.strip()]
                wrapper = templates[0] if templates else None
                model.families.append(Family(
                    members=names,
                    wrapper_tag=wrapper.tag if wrapper is not None else None,
                    wrapper_attrs=dict(wrapper.attrib) if wrapper is not None else {}))
                continue
            if kind == "prevEle":
                model.attach_to_previous.update(n.strip() for n in find.split("|") if n.strip())
                continue
            targets = cls._xpath_targets(find)
            if kind == "coverAbove":
                model.section_openers.update(t for t, _p in targets)
            if kind == "addFirst":
                model.add_first.update(t for t, _p in targets)
            for target, parent in targets:
                if parent:
                    model.parent_child.add((parent, target))
                for tpl in templates:
                    attrs = {k: v for k, v in tpl.attrib.items() if "{" not in v}
                    model.outputs.setdefault(target, []).append({"tag": tpl.tag, "attrs": attrs})
                    if not kind:
                        cls_name = attrs.get("class")
                        model.reverse.setdefault((tpl.tag, cls_name), target)
        return model

    @staticmethod
    def _xpath_targets(expr: str) -> list:
        """[(target_element, direct_parent_or_None), ...] for every
        alternative of a simple XPath union ("//a | //b/c[@x]")."""
        out = []
        for alt in expr.split("|"):
            alt = re.sub(r"\[[^\]]*\]", "", alt.strip())
            steps = [s for s in re.split(r"/+", alt) if s and s not in (".", "..")]
            names = [s for s in steps if _STEP_RE.fullmatch(s)]
            if not names:
                continue
            target = names[-1]
            parent = names[-2] if len(names) >= 2 else None
            out.append((target, parent))
        return out

    # ------------------------------------------------------------ query
    def families_of(self, element: str) -> list:
        return [f for f in self.families if element in f.members]

    def same_family(self, a: str, b: str) -> bool:
        return any(a in f.members and b in f.members for f in self.families)

    def output_tokens(self, element: str) -> set:
        toks = set()
        for out in self.outputs.get(element, []):
            for key in ("class", "epub_type", "role"):
                if out["attrs"].get(key):
                    toks |= tokenize(out["attrs"][key])
        return toks

    def intermediate_for(self, final_tag: str, class_name: str = None):
        if (final_tag, class_name) in self.reverse:
            return self.reverse[(final_tag, class_name)]
        if class_name:
            for cls_part in class_name.split():
                if (final_tag, cls_part) in self.reverse:
                    return self.reverse[(final_tag, cls_part)]
        return None
