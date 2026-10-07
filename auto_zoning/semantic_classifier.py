"""Semantic ROLE classification of layout blocks.

Every block gets a ranked list of role candidates, each with a score in
0..1 and the evidence behind it. No decision is made from a single signal:
a heading needs size AND/OR weight AND isolation AND shortness; a footnote
needs the notes region OR small type plus a note marker; a caption needs a
float next to it plus a label pattern and/or caption typography; a list
item needs a marker run (list_detector) and so on.

Roles are the concepts listed in profiles/<profile>/semantic_roles.json -
this module never names a project tag. A second, contextual pass revises
roles that depend on neighbours (sources after quotations/verse, labels
above headings, reference runs, verse continuity).
"""
import re
from dataclasses import dataclass, field

_ROMAN_MARKER_RE = re.compile(r"^\s*\(?([ivxlcdm]{1,6}|[IVXLCDM]{1,6})[.)]\s")
_SOURCE_LEAD_RE = re.compile(r"^\s*[—–―-]\s*\S")
_BRACKET_NUM_RE = re.compile(r"^\s*\[\d{1,4}\]|^\s*\d{1,4}\.\s")


@dataclass
class RoleCandidate:
    role: str
    score: float
    evidence: list = field(default_factory=list)


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _list_role(block) -> str:
    lt = block.features.get("list_type")
    text = block.text
    if lt == "number":
        return "list_numbered"
    if lt == "simple":
        return "list_bullet"
    if lt in ("alpha-lower", "alpha-upper"):
        m = _ROMAN_MARKER_RE.match(text)
        if m and m.group(1).lower() not in ("c", "d", "l", "m") or (m and len(m.group(1)) > 1):
            return "list_lower_roman" if m.group(1).islower() else "list_upper_roman"
        return "list_lower_alpha" if lt == "alpha-lower" else "list_upper_alpha"
    return "list_plain"


def _heading_score(f, ctx):
    ev = []
    if f.get("chars", 0) == 0 or f.get("n_lines", 9) > 3 or f.get("chars", 999) > 160:
        return 0.0, ["too long for a heading"]
    fr = f.get("font_ratio") or 1.0
    size_sig = _clamp((fr - 1.03) / 0.35)
    bold = f.get("bold_ratio", 0.0)
    caps = 1.0 if f.get("caps_ratio", 0) > 0.85 and f.get("chars", 0) > 3 else 0.0
    gap_above = f.get("gap_above")
    isolated = 1.0 if (gap_above is None or gap_above > 0.9 * ctx.body_size or f.get("first_in_column")) else 0.0
    no_punct = 0.0 if f.get("ends_with_punct") and not f.get("text_ends_colon") else 1.0
    numbered = 1.0 if f.get("numbered_heading_depth") else 0.0
    if fr < 0.97 and bold < 0.5:
        return 0.0, ["smaller than body text and not bold"]
    score = 0.42 * size_sig + 0.24 * bold + 0.08 * caps + 0.12 * isolated + 0.08 * no_punct + 0.12 * numbered
    if size_sig > 0:
        ev.append(f"font {fr:.2f}x body")
    if bold >= 0.5:
        ev.append("bold")
    if caps:
        ev.append("all caps")
    if isolated:
        ev.append("separated from preceding text")
    if numbered:
        ev.append(f"section number depth {f.get('numbered_heading_depth')}")
    if f.get("words", 0) > 20:
        score *= 0.6
        ev.append("many words")
    return _clamp(score), ev


def classify_block(block, ctx, page_index_in_doc=None) -> list:
    f = block.features
    kind = block.kind
    c = []
    if kind == "page_number":
        return [RoleCandidate("page_number", 0.97, ["number-only line in the page margin band"])]
    if kind == "running":
        role = "running_header" if f.get("in_header") else "running_footer"
        return [RoleCandidate(role, 0.95, ["text repeated in the same margin band on several pages"])]
    if kind == "figure":
        ev = ["vector illustration" if f.get("vector") else "embedded image"]
        return [RoleCandidate("figure", 0.9 if not f.get("vector") else 0.86, ev)]
    if kind == "table":
        if f.get("has_text"):
            return [RoleCandidate("table", 0.88, ["ruled/aligned grid containing text"]),
                    RoleCandidate("figure", 0.3, ["could be kept as an image"])]
        return [RoleCandidate("figure", 0.6, ["ruled region without text"])]
    if kind == "list_item":
        if block.region == "notes":
            return [RoleCandidate("footnote", 0.85, ["marker-initial entry below the footnote rule"])]
        role = _list_role(block)
        ev = [f"list marker ({f.get('list_type')})", f"nesting level {f.get('list_level', 1)}"]
        return [RoleCandidate(role, 0.9, ev), RoleCandidate("paragraph", 0.35, ["could be prose"])]
    if kind == "verse_line":
        hs, hev = _heading_score(f, ctx)
        if hs >= 0.5 and (f.get("bold_ratio", 0) >= 0.6 or f.get("caps_ratio", 0) > 0.85
                          or (f.get("font_ratio") or 1.0) >= 1.1):
            level = ctx.heading_level(f.get("font_size", ctx.body_size or 10.0), f.get("bold_ratio", 0) >= 0.5)
            return [RoleCandidate(f"heading_{level}", hs, hev + [f"document heading ladder level {level}"]),
                    RoleCandidate("verse_line", 0.4, ["short line in a run of short lines"])]
        score = 0.72 + (0.12 if f.get("verse_group_size", 0) >= 3 else 0.0) + \
            (0.06 if f.get("left_indent", 0) > ctx.body_size else 0.0)
        ev = [f"short line in a run of {f.get('verse_group_size')} short lines", "ragged right, line breaks are not wraps"]
        return [RoleCandidate("verse_line", _clamp(score), ev),
                RoleCandidate("paragraph", 0.3, ["could be short prose lines"])]

    body = ctx.body_size or 10.0
    fr = f.get("font_ratio") or 1.0

    # footnotes
    if block.region == "notes":
        s = 0.8 + (0.12 if f.get("note_marker") else 0.0) + (0.05 if fr < 0.95 else 0.0)
        c.append(RoleCandidate("footnote", _clamp(s), ["below the footnote separator rule",
                                                         "note marker" if f.get("note_marker") else "continuation"]))
    elif fr <= 0.92 and f.get("note_marker") and f.get("y", 0) > 0.7 and f.get("last_in_column"):
        c.append(RoleCandidate("footnote", 0.62, ["small type with a note marker at the foot of the column"]))

    # headings
    hs, hev = _heading_score(f, ctx)
    if hs > 0.2:
        level = ctx.heading_level(f.get("font_size", body), f.get("bold_ratio", 0) >= 0.5)
        depth = f.get("numbered_heading_depth") or 0
        if depth and abs(fr - 1.0) < 0.1:
            level = min(6, max(level, depth))
        c.append(RoleCandidate(f"heading_{level}", hs, hev + [f"document heading ladder level {level}"]))

    # a chapter / part / appendix number on its own line, in any language
    # ("Capítulo 1.2", "Глава 3", "第3章", "3. fejezet") - the label of the title next to it
    from core import lang as _lang
    _m = _lang.match_label(block.text.strip(), "chapter", "part", "appendix", "section")
    if _m and not _m[1] and f.get("n_lines", 9) <= 2:
        c.append(RoleCandidate("label", 0.88, [f"\"{_m[0]}\" - a chapter / part number on its own"]))

    # label (chapter/part number line) - short, number pattern, bigger heading below
    if f.get("label_number") and f.get("words", 9) <= 4 and f.get("n_lines", 9) == 1:
        nxt = f.get("next_font_ratio")
        s = 0.55 + (0.3 if nxt and nxt > fr + 0.1 else 0.0) + (0.05 if f.get("first_in_column") else 0.0)
        c.append(RoleCandidate("label", _clamp(s), ["word + number pattern",
                                                     "larger heading follows" if nxt and nxt > fr + 0.1 else ""]))

    # title (document title page)
    if page_index_in_doc == 0 and fr >= 1.6 and f.get("y", 1) < 0.5 and f.get("n_lines", 9) <= 3:
        c.append(RoleCandidate("title", 0.6 + 0.2 * (1 if f.get("centered") else 0),
                               ["largest type near the top of the first page"]))

    # captions
    fa, fb = f.get("float_above"), f.get("float_below")
    if fa or fb:
        s = 0.45 + (0.3 if f.get("label_number") else 0.0) + (0.1 if fr <= 0.95 else 0.0) + \
            (0.08 if f.get("italic_ratio", 0) > 0.5 else 0.0) + (0.05 if f.get("n_lines", 9) <= 4 else 0.0)
        ev = [f"directly {'below' if fa else 'above'} a {fa or fb}"]
        if f.get("label_number"):
            ev.append("label + number pattern")
        role = "table_caption" if (fb == "table" or fa == "table") else "caption"
        c.append(RoleCandidate(role, _clamp(s), ev))

    # a block that opens with a figure / table label in any language ("Figura 1.2.3.",
    # "Рис. 2", "表1", "Tabla 1.1.1") in body-size or smaller type is a caption, even
    # when the illustration is not directly above / below it
    if not (fa or fb) and fr <= 1.02:
        if _lang.match_label(block.text.strip(), "figure"):
            c.append(RoleCandidate("caption", 0.8, ["starts with a figure label"]))
        elif _lang.match_label(block.text.strip(), "table"):
            c.append(RoleCandidate("table_caption", 0.8, ["starts with a table label"]))

    # prose paragraph family
    n = f.get("n_lines", 1)
    p = 0.5 + (0.22 if n >= 2 and f.get("line_fill", 0) >= 0.85 else 0.0) + \
        (0.12 if abs(fr - 1.0) <= 0.08 else 0.0) + (0.06 if f.get("ends_with_punct") else 0.0)
    if f.get("words", 0) <= 3 and n == 1:
        p -= 0.15
    pev = ["body-size type" if abs(fr - 1.0) <= 0.08 else f"type {fr:.2f}x body",
           "full measure lines" if f.get("line_fill", 0) >= 0.85 else "short measure"]
    para_role = "paragraph"
    if f.get("centered") and n >= 1 and f.get("chars", 0) > 0:
        para_role = "paragraph_center"
        pev.append("every line centred in the column")
    elif f.get("right_aligned"):
        para_role = "paragraph_right"
        pev.append("right aligned")
    elif f.get("hanging"):
        para_role = "paragraph_hanging"
        pev.append("hanging indent")
    elif ctx.paragraph_indent > 0 and n >= 2 and abs(f.get("first_line_indent", 0)) < 0.35 * ctx.paragraph_indent \
            and f.get("left_indent", 0) < 0.5 * body:
        para_role = "paragraph_noindent"
        pev.append(f"no first-line indent where the document indents paragraphs by {ctx.paragraph_indent:.0f}pt")
    c.append(RoleCandidate(para_role, _clamp(p), pev))
    if para_role != "paragraph":
        c.append(RoleCandidate("paragraph", _clamp(p - 0.12), ["plain paragraph fallback"]))

    # blockquote / epigraph: indented block (both sides or smaller type)
    li, ri = f.get("left_indent", 0), f.get("right_indent", 0)
    if li >= 1.4 * body and (ri >= 0.8 * body or fr <= 0.95) and (n >= 2 or f.get("chars", 0) > 60):
        s = 0.62 + (0.12 if ri >= 0.8 * body else 0.0) + (0.1 if fr <= 0.95 else 0.0) + \
            (0.06 if f.get("line_fill", 0) >= 0.7 else 0.0)
        ev = [f"indented {li:.0f}pt left / {ri:.0f}pt right", "smaller type" if fr <= 0.95 else "body type"]
        near_top = f.get("first_in_column") or (f.get("prev_font_ratio") or 1.0) > 1.15
        if near_top and f.get("y", 1) < 0.45:
            c.append(RoleCandidate("epigraph", _clamp(s), ev + ["at the opening of a section"]))
            c.append(RoleCandidate("blockquote", _clamp(s - 0.08), ev))
        else:
            c.append(RoleCandidate("blockquote", _clamp(s), ev))

    # references
    if f.get("has_year") and (f.get("hanging") or _BRACKET_NUM_RE.match(block.text)) and f.get("chars", 0) > 40:
        role = "reference_numbered" if _BRACKET_NUM_RE.match(block.text) else "reference_author_date"
        c.append(RoleCandidate(role, 0.6, ["publication year", "hanging indent" if f.get("hanging") else "numbered"]))

    # equations
    if f.get("math_ratio", 0) >= 0.12 and f.get("n_lines", 9) <= 3 and (f.get("centered") or li > 2 * body):
        c.append(RoleCandidate("equation", 0.55 + f.get("math_ratio", 0), ["mathematical symbols", "display position"]))

    c.sort(key=lambda r: -r.score)
    return c


def _looks_like_names(text: str) -> bool:
    """"Gary Andrew Dildy", "Radu Apostol y Farr Nezhat", "A. B. Smith, C. Jones" -
    most words capitalised (or initials / joiners), no sentence verbs."""
    from core import lang
    words = [w.strip(",;.") for w in (text or "").split() if w.strip(",;.")]
    if not words:
        return False
    joiners = set(lang.NAME_JOINERS) | {"&", "de", "van", "von", "der", "da", "del", "di", "le", "la"}
    good = sum(1 for w in words if w[:1].isupper() or w.lower() in joiners or not w[:1].isalpha())
    return good / len(words) >= 0.8


def classify_page(layout, ctx, page_index_in_doc=None) -> dict:
    """{id(block): [RoleCandidate, ...]} for every block of the page, after
    the contextual second pass."""
    out = {id(b): classify_block(b, ctx, page_index_in_doc) for b in layout.blocks}
    _contextual_pass(layout, ctx, out)
    return out


def _top(cands):
    return cands[0].role if cands else None


def _contextual_pass(layout, ctx, out):
    flow = [b for b in layout.blocks if b.kind not in ("page_number", "running")]
    for i, b in enumerate(flow):
        cands = out[id(b)]
        prev_b = flow[i - 1] if i > 0 else None
        prev_role = _top(out[id(prev_b)]) if prev_b else None
        f = b.features
        short = f.get("n_lines", 9) <= 2 and f.get("words", 99) <= 14
        # source / attribution line after a quotation, epigraph or verse
        if short and prev_role in ("blockquote", "epigraph", "verse_line") and \
                (f.get("right_aligned") or _SOURCE_LEAD_RE.match(b.text) or
                 (f.get("left_indent", 0) > 2 * ctx.body_size and prev_role != "verse_line")):
            role = {"blockquote": "blockquote_source", "epigraph": "epigraph_source",
                    "verse_line": "verse_source"}[prev_role]
            cands.insert(0, RoleCandidate(role, 0.82, [f"short attribution line right after a {prev_role}",
                                                         "right aligned" if f.get("right_aligned") else "dash lead"]))
        # the line of names right under a chapter / article title (a chapter
        # label may sit between them): its author(s)
        title_b = None
        for back in (1, 2):
            pb = flow[i - back] if i - back >= 0 else None
            if pb is not None and _top(out[id(pb)]) in ("heading_1", "title"):
                title_b = pb
                break
            if pb is None or _top(out[id(pb)]) != "label":
                break
        if title_b is not None and f.get("n_lines", 9) <= 2 and 1 < f.get("words", 0) <= 16 \
                and not f.get("ends_with_punct") and _looks_like_names(b.text) \
                and 0 <= b.bbox[1] - title_b.bbox[3] <= 2.5 * (ctx.body_size or 10):
            role = "author" if _top(out[id(title_b)]) == "title" else "chapter_author"
            cands.insert(0, RoleCandidate(role, 0.8, ["a line of names right under the title"]))
        # a single verse line split off by the segmenter, between verse lines
        nxt_b = flow[i + 1] if i + 1 < len(flow) else None
        if b.kind == "text" and prev_role == "verse_line" and nxt_b is not None and \
                _top(out[id(nxt_b)]) == "verse_line" and f.get("n_lines", 9) == 1:
            cands.insert(0, RoleCandidate("verse_line", 0.7, ["single short line inside a verse run"]))
        # reference runs: several consecutive reference-like blocks reinforce each other
        if cands and cands[0].role.startswith("reference_"):
            neighbours = sum(1 for nb in (prev_b, nxt_b) if nb is not None and
                             any(rc.role.startswith("reference_") for rc in out[id(nb)]))
            if neighbours:
                cands[0].score = _clamp(cands[0].score + 0.15 * neighbours)
                cands[0].evidence.append(f"part of a run of {neighbours + 1}+ reference entries")
        cands.sort(key=lambda r: -r.score)
