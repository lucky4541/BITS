"""Character-level formatting RANGES over a zone's text.

A zone's text (zone.text / extract_zone_formatted_text) is plain text with
inline formatting elements (<b>, <i>, <sup>, <sub>, <underline>, <strike>,
...). This module converts that markup into the separated model the engine
reasons with - the exact plain text plus a list of formatting ranges:

    "He <b>will <underline>not</underline></b> go"
      -> plain  "He will not go"
      -> ranges [{start: 3, end: 11, style: "b"}, {start: 8, end: 11, style: "underline"}]

and back. Structural tags (the zone tag) and formatting ranges are kept
apart, so applying one never destroys the other. Offsets count plain-text
characters (an XML entity counts as the one character it stands for).
"""
import re
from xml.sax.saxutils import escape, unescape

_TOKEN_RE = re.compile(r"(<[^>]+>)")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")


def _plain_len(chunk: str) -> int:
    return len(unescape(_ENTITY_RE.sub(lambda m: "\x00", chunk)).replace("\x00", "x"))


def parse_ranges(tagged: str):
    """(plain_text, ranges, issues). Unbalanced or crossing tags are
    reported in `issues` and closed at the end, never silently dropped."""
    plain_parts = []
    ranges = []
    stack = []
    issues = []
    pos = 0
    for tok in _TOKEN_RE.split(tagged or ""):
        if not tok:
            continue
        if tok.startswith("<"):
            name = tok.strip("<>/ ").split()[0] if tok.strip("<>/ ") else ""
            if tok.endswith("/>"):
                continue  # empty element such as <break/> - no range
            if tok.startswith("</"):
                for k in range(len(stack) - 1, -1, -1):
                    if stack[k][0] == name:
                        if k != len(stack) - 1:
                            issues.append(f"crossing tags: </{name}> closes over <{stack[-1][0]}>")
                        start = stack[k][1]
                        stack.pop(k)
                        ranges.append({"start": start, "end": pos, "style": name})
                        break
                else:
                    issues.append(f"unmatched closing tag </{name}>")
            else:
                stack.append((name, pos))
        else:
            text = unescape(tok)
            plain_parts.append(text)
            pos += len(text)
    for name, start in stack:
        issues.append(f"unclosed tag <{name}>")
        ranges.append({"start": start, "end": pos, "style": name})
    ranges.sort(key=lambda r: (r["start"], -r["end"], r["style"]))
    return "".join(plain_parts), ranges, issues


def normalize_ranges(ranges: list, plain_len: int, exact_styles=()) -> list:
    """Tag normalization on the range model:
      * drop empty / out-of-bounds ranges;
      * drop exact duplicates (same style over the same characters);
      * merge identical styles that touch or overlap;
      * keep separate ranges separate - two ranges of an EXACT style
        (underline/strike) separated by even one undecorated character are
        never merged."""
    out = []
    by_style = {}
    for r in ranges:
        s, e = max(0, int(r["start"])), min(plain_len, int(r["end"]))
        if e <= s:
            continue
        by_style.setdefault(r["style"], []).append([s, e])
    for style, spans in by_style.items():
        spans.sort()
        merged = [spans[0]]
        for s, e in spans[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out.extend({"start": s, "end": e, "style": style} for s, e in merged)
    out.sort(key=lambda r: (r["start"], -r["end"], r["style"]))
    return out


# Nesting order used when rebuilding markup - innermost first, matching
# core.text_extractor._wrap_run (position, then italic, bold, decorations).
_DEFAULT_ORDER = ["sup", "sub", "smallcaps", "i", "italic", "b", "bold", "underline", "u", "strike", "s"]


def build_markup(plain: str, ranges: list, order=None) -> str:
    """Rebuilds valid, minimal inline markup from plain text + ranges.
    Characters are grouped into maximal runs sharing the same style set,
    and each run is wrapped innermost-first, so the result is always well
    nested whatever the input ranges looked like."""
    order = list(order or _DEFAULT_ORDER)
    rank = {name: i for i, name in enumerate(order)}
    styles_at = [set() for _ in range(len(plain))]
    for r in ranges:
        for i in range(max(0, r["start"]), min(len(plain), r["end"])):
            styles_at[i].add(r["style"])
    out = []
    i = 0
    while i < len(plain):
        j = i
        while j < len(plain) and styles_at[j] == styles_at[i]:
            j += 1
        text = escape(plain[i:j])
        for name in sorted(styles_at[i], key=lambda n: rank.get(n, len(order))):
            text = f"<{name}>{text}</{name}>"
        out.append(text)
        i = j
    return "".join(out)


def ranges_for_style(tagged: str, style: str) -> list:
    """[(start, end), ...] plain-text offsets of one style, separate ranges
    kept separate."""
    plain, ranges, _ = parse_ranges(tagged)
    return [(r["start"], r["end"]) for r in normalize_ranges(ranges, len(plain)) if r["style"] == style]
