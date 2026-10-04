"""Programmatic DTD model.

Parses a project DTD with lxml (a real DTD parser, never regexes) and keeps,
per element: its content type (empty/any/mixed/element), the full content
model (sequence / choice / cardinality tree), the set of allowed children,
the first-position children, ordering (which child may follow which), and
every attribute with its type, default kind (#REQUIRED / #IMPLIED / #FIXED)
and enumerated values. Entities are kept too.

The model answers the questions Auto Tag asks before applying a tag:
  * may element C appear inside element P?            allows_child
  * may C directly follow B inside P?                  allows_sequence
  * which attributes must C carry?                     required_attributes
  * is this generated tree valid?                      validate (lxml)
"""
import io
import os
from dataclasses import dataclass, field

from lxml import etree


@dataclass
class ContentNode:
    kind: str                 # "pcdata" | "element" | "seq" | "or"
    occur: str                # "once" | "opt" | "mult" | "plus"
    name: str = None
    children: list = field(default_factory=list)

    def to_text(self) -> str:
        suffix = {"once": "", "opt": "?", "mult": "*", "plus": "+"}.get(self.occur, "")
        if self.kind == "pcdata":
            return "#PCDATA"
        if self.kind == "element":
            return f"{self.name}{suffix}"
        sep = ", " if self.kind == "seq" else " | "
        return "(" + sep.join(c.to_text() for c in self.children) + ")" + suffix


@dataclass
class AttributeDecl:
    name: str
    type: str
    default: str              # "required" | "implied" | "fixed" | "none"
    default_value: str = None
    values: list = field(default_factory=list)


@dataclass
class ElementDecl:
    name: str
    type: str                 # "empty" | "any" | "mixed" | "element"
    content: ContentNode = None
    attributes: dict = field(default_factory=dict)
    allowed_children: set = field(default_factory=set)
    first_children: set = field(default_factory=set)
    follows: dict = field(default_factory=dict)      # child -> set(children that may follow it)
    required_children: set = field(default_factory=set)
    child_cardinality: dict = field(default_factory=dict)  # child -> occur string

    @property
    def mixed(self) -> bool:
        return self.type == "mixed"


def _convert(node) -> ContentNode:
    if node is None:
        return None
    out = ContentNode(kind=node.type, occur=node.occur, name=node.name)
    # lxml exposes binary trees (left/right) for seq/or nodes - flatten
    # same-kind chains so (a, b, c) reads as one sequence.
    if node.type in ("seq", "or"):
        stack = [node.left, node.right]
        flat = []
        while stack:
            n = stack.pop(0)
            if n is None:
                continue
            if n.type == node.type and n.occur == "once":
                stack = [n.left, n.right] + stack
            else:
                flat.append(_convert(n))
        out.children = flat
    return out


def _names(node: ContentNode) -> set:
    if node is None:
        return set()
    if node.kind == "element":
        return {node.name}
    out = set()
    for c in node.children:
        out |= _names(c)
    return out


def _nullable(node: ContentNode) -> bool:
    if node is None:
        return True
    if node.occur in ("opt", "mult"):
        return True
    if node.kind == "pcdata":
        return True
    if node.kind == "element":
        return False
    if node.kind == "seq":
        return all(_nullable(c) for c in node.children)
    return any(_nullable(c) for c in node.children)


def _first(node: ContentNode) -> set:
    if node is None:
        return set()
    if node.kind == "element":
        return {node.name}
    if node.kind == "pcdata":
        return set()
    if node.kind == "or":
        out = set()
        for c in node.children:
            out |= _first(c)
        return out
    out = set()
    for c in node.children:
        out |= _first(c)
        if not _nullable(c):
            break
    return out


def _last(node: ContentNode) -> set:
    if node is None:
        return set()
    if node.kind == "element":
        return {node.name}
    if node.kind == "pcdata":
        return set()
    if node.kind == "or":
        out = set()
        for c in node.children:
            out |= _last(c)
        return out
    out = set()
    for c in reversed(node.children):
        out |= _last(c)
        if not _nullable(c):
            break
    return out


def _follow(node: ContentNode, table: dict):
    """Glushkov follow sets (by element name)."""
    if node is None or node.kind in ("element", "pcdata"):
        if node is not None and node.kind == "element" and node.occur in ("mult", "plus"):
            table.setdefault(node.name, set()).add(node.name)
        return
    for c in node.children:
        _follow(c, table)
    if node.kind == "seq":
        for i, c in enumerate(node.children):
            for nxt in node.children[i + 1:]:
                for a in _last(c):
                    table.setdefault(a, set()).update(_first(nxt))
                if not _nullable(nxt):
                    break
    if node.occur in ("mult", "plus"):
        for a in _last(node):
            table.setdefault(a, set()).update(_first(node))


def _required(node: ContentNode) -> set:
    if node is None or node.occur in ("opt", "mult"):
        return set()
    if node.kind == "element":
        return {node.name}
    if node.kind == "seq":
        out = set()
        for c in node.children:
            out |= _required(c)
        return out
    if node.kind == "or":
        sets = [_required(c) for c in node.children]
        return set.intersection(*sets) if sets else set()
    return set()


def _cardinality(node: ContentNode, out: dict, outer="once"):
    if node is None:
        return
    occ = node.occur if outer == "once" else ("mult" if "mult" in (outer, node.occur) else outer)
    if node.kind == "element":
        out[node.name] = occ
    for c in node.children:
        _cardinality(c, out, occ)


class DTDModel:
    def __init__(self, dtd: etree.DTD, source: str = ""):
        self.dtd = dtd
        self.source = source
        self.elements = {}
        self.entities = {}
        for el in dtd.elements():
            content = _convert(el.content)
            decl = ElementDecl(name=el.name, type=el.type, content=content)
            for a in el.attributes():
                decl.attributes[a.name] = AttributeDecl(name=a.name, type=a.type, default=a.default,
                                                        default_value=a.default_value, values=list(a.values()))
            decl.allowed_children = _names(content)
            decl.first_children = _first(content)
            follows = {}
            _follow(content, follows)
            decl.follows = follows
            decl.required_children = _required(content)
            _cardinality(content, decl.child_cardinality)
            self.elements[el.name] = decl
        for ent in dtd.entities():
            self.entities[ent.name] = ent.content

    # ---------------------------------------------------------- loading
    @classmethod
    def from_file(cls, path: str) -> "DTDModel":
        with open(path, "rb") as f:
            return cls(etree.DTD(f), source=path)

    @classmethod
    def from_string(cls, text: str, source: str = "<string>") -> "DTDModel":
        return cls(etree.DTD(io.StringIO(text)), source=source)

    @classmethod
    def discover(cls, directory: str) -> list:
        """Every *.dtd under `directory` (recursive), parsed. Unparseable
        files are reported, never silently ignored."""
        models, errors = [], []
        if not directory or not os.path.isdir(directory):
            return models, errors
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if name.lower().endswith(".dtd"):
                    path = os.path.join(root, name)
                    try:
                        models.append(cls.from_file(path))
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{path}: {e}")
        return models, errors

    # ---------------------------------------------------------- queries
    def has_element(self, name: str) -> bool:
        return name in self.elements

    def allows_child(self, parent: str, child: str):
        """True / False, or None when the DTD does not declare `parent`."""
        decl = self.elements.get(parent)
        if decl is None:
            return None
        if decl.type == "any":
            return True
        if decl.type == "empty":
            return False
        return child in decl.allowed_children

    def allows_text(self, element: str):
        decl = self.elements.get(element)
        if decl is None:
            return None
        return decl.type in ("mixed", "any")

    def allows_sequence(self, parent: str, prev_child: str, child: str):
        decl = self.elements.get(parent)
        if decl is None or decl.type in ("any", "mixed"):
            return None if decl is None else (decl.type == "any" or child in decl.allowed_children)
        if prev_child is None:
            return child in decl.first_children
        return child in decl.follows.get(prev_child, set())

    def required_attributes(self, element: str) -> list:
        decl = self.elements.get(element)
        if decl is None:
            return []
        return [a for a in decl.attributes.values() if a.default == "required"]

    def attribute_values(self, element: str, attr: str) -> list:
        decl = self.elements.get(element)
        if decl is None or attr not in decl.attributes:
            return []
        return list(decl.attributes[attr].values)

    def parents_of(self, child: str) -> set:
        return {name for name, d in self.elements.items() if child in d.allowed_children or d.type == "any"}

    def validate(self, root) -> list:
        """lxml DTD validation of a generated tree -> list of error strings."""
        ok = self.dtd.validate(root)
        if ok:
            return []
        return [f"line {e.line}: {e.message}" for e in self.dtd.error_log.filter_from_errors()]

    def describe(self, name: str) -> str:
        decl = self.elements.get(name)
        if decl is None:
            return f"{name}: (not declared)"
        model = decl.content.to_text() if decl.content else decl.type.upper()
        attrs = ", ".join(f"{a.name}{'*' if a.default == 'required' else ''}" for a in decl.attributes.values())
        return f"{name}: {model}" + (f"  [attrs: {attrs}]" if attrs else "")
