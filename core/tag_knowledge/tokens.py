"""Name tokenizer shared by the knowledge model: splits project tag names,
labels, class names and epub:type values into comparable lowercase tokens
("PoemLine" -> poem, line; "Para_NoIndent" -> para, no, indent, noindent;
"H1" -> h, 1, h1; "doc-footnote" -> doc, footnote)."""
import re

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(*names) -> set:
    out = set()
    for name in names:
        if not name:
            continue
        name = str(name)
        for part in re.split(r"[\s_\-:./\\|,]+", name):
            if not part:
                continue
            low = part.lower()
            out.add(low)
            pieces = _CAMEL_RE.findall(part)
            lowered = [p.lower() for p in pieces]
            out.update(lowered)
            # adjacent-piece joins: "No"+"Indent" -> "noindent"
            for i in range(len(lowered) - 1):
                out.add(lowered[i] + lowered[i + 1])
        whole = re.sub(r"[^a-z0-9]", "", name.lower())
        if whole:
            out.add(whole)
    return out


def pieces(name) -> list:
    """The atomic name pieces only (no joins/whole-word forms):
    "Para_NoIndent" -> [para, no, indent]."""
    out = []
    for part in re.split(r"[\s_\-:./\\|,]+", str(name or "")):
        out.extend(p.lower() for p in _CAMEL_RE.findall(part))
    return out
