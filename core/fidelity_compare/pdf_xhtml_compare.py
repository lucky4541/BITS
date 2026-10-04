
"""
EPUBForge PDF -> XHTML Direct Fidelity Comparator
==================================================

Replacement module for:
    core/fidelity_compare/pdf_xhtml_compare.py

Checks independently:
    CONTENT
      * exact Unicode text
      * words / numbers
      * punctuation
      * symbols
      * missing / extra / changed text
      * Unicode code points

    FORMATTING
      * bold
      * italic
      * underline
      * superscript
      * subscript

Important:
    A symbol difference must never shift the word alignment.

Example:
    PDF:   ethics ± as they do
    XHTML: ethics - as they do

Result:
    SYMBOL_MISMATCH: ± -> -

and NOT:
    EXTRA: 2
    MISSING: as
    ...

The XHTML side also reads:
    <b>/<strong>, <i>/<em>, <u>, <sup>, <sub>
    inline CSS
    linked CSS files
    common EPUB class names

The PDF side reads:
    font flags
    font name / metadata
    PDF underline annotations/drawings
    superscript/subscript geometry
    rendered-page visual italic heuristic

The visual italic heuristic exists because some PDFs (including PDFs
where the font descriptor says ArialMT, ItalicAngle=0, flags=0) can still
render italic glyphs while exposing no italic metadata to PyMuPDF.
"""

from __future__ import annotations

import html
import json
import math
import re
import statistics
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

try:
    import pymupdf as fitz
except Exception:
    try:
        import fitz
    except Exception as exc:
        raise ImportError(
            "PyMuPDF is required. Install with: pip install pymupdf"
        ) from exc

try:
    import cv2
    import numpy as np
    _CV_AVAILABLE = True
except Exception:
    cv2 = None
    np = None
    _CV_AVAILABLE = False


BLOCK_TAGS = {
    "p", "div", "li", "dt", "dd", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "th", "caption", "figcaption",
    "pre", "address", "section", "article",
}

STYLE_PROPERTIES = (
    "bold",
    "italic",
    "underline",
    "superscript",
    "subscript",
)

WS_RE = re.compile(r"\s+")


# ============================================================================
# DATA
# ============================================================================

@dataclass
class PDFToken:
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    paragraph: int = 0
    style: dict = field(default_factory=dict)
    font: str = ""
    flags: int = 0
    size: float = 0.0
    token_kind: str = "word"
    proof_status: str = "MATCH"


@dataclass
class XHTMLToken:
    text: str
    file: str
    element: str
    paragraph: int
    word_index: int
    source_line: int = 0
    style: dict = field(default_factory=dict)
    token_kind: str = "word"
    proof_status: str = "MATCH"


@dataclass
class Difference:
    kind: str
    severity: str
    pdf_text: str
    xhtml_text: str

    pdf_page: Optional[int] = None
    pdf_bbox: Optional[tuple] = None

    xhtml_file: Optional[str] = None
    xhtml_element: Optional[str] = None
    xhtml_line: Optional[int] = None
    xhtml_paragraph: Optional[int] = None
    xhtml_word: Optional[int] = None

    confidence: float = 0.0
    message: str = ""

    pdf_style: dict = field(default_factory=dict)
    xhtml_style: dict = field(default_factory=dict)
    style_property: str = ""
    range_text: str = ""

    pdf_unicode: str = ""
    xhtml_unicode: str = ""

    # Exact token positions used by the interactive proof viewer.
    # These remove ambiguity when the same word occurs many times.
    pdf_token_index: Optional[int] = None
    xhtml_token_index: Optional[int] = None


@dataclass
class CompareResult:
    pdf_path: str
    xhtml_root: str
    pdf_pages: int
    pdf_words: int
    xhtml_words: int
    pdf_paragraphs: int
    xhtml_paragraphs: int

    matched_words: int = 0
    missing_words: int = 0
    extra_words: int = 0
    changed_words: int = 0
    style_errors: int = 0
    order_changes: int = 0

    differences: list[Difference] = field(default_factory=list)

    # In-memory proof tokens used by the GUI. They are deliberately removed
    # from JSON/HTML serialization because they are rendering data, not the
    # compact QA result model.
    pdf_tokens: list = field(default_factory=list, repr=False)
    xhtml_tokens: list = field(default_factory=list, repr=False)

    @property
    def total_changes(self):
        return len(self.differences)

    @property
    def symbol_errors(self):
        return sum(
            1 for d in self.differences
            if "SYMBOL" in d.kind
        )

    @property
    def content_fidelity_percent(self):
        denominator = max(self.pdf_words, self.xhtml_words, 1)
        errors = (
            self.missing_words
            + self.extra_words
            + self.changed_words
        )
        return round(
            max(
                0.0,
                min(
                    100.0,
                    (1.0 - errors / denominator) * 100.0,
                ),
            ),
            2,
        )

    @property
    def formatting_fidelity_percent(self):
        denominator = max(self.matched_words, 1)
        return round(
            max(
                0.0,
                min(
                    100.0,
                    (1.0 - self.style_errors / denominator) * 100.0,
                ),
            ),
            2,
        )

    @property
    def fidelity_percent(self):
        return round(
            self.content_fidelity_percent * 0.75
            + self.formatting_fidelity_percent * 0.25,
            2,
        )

    def to_dict(self):
        data = asdict(self)
        data.pop("pdf_tokens", None)
        data.pop("xhtml_tokens", None)
        data["total_changes"] = self.total_changes
        data["symbol_errors"] = self.symbol_errors
        data["content_fidelity_percent"] = self.content_fidelity_percent
        data["formatting_fidelity_percent"] = self.formatting_fidelity_percent
        data["fidelity_percent"] = self.fidelity_percent
        return data

    def write_json(self, filename):
        Path(filename).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_html(self, filename):
        def esc(v):
            return html.escape("" if v is None else str(v))

        rows = []

        for n, d in enumerate(self.differences, 1):
            if d.kind in {
                "MISSING",
                "EXTRA",
                "MISSING_SYMBOL",
                "EXTRA_SYMBOL",
                "SYMBOL_MISMATCH",
                "STYLE_MISMATCH",
            }:
                color = "#dc2626"
            elif d.kind.endswith("_MISMATCH"):
                color = "#d97706"
            else:
                color = "#d97706"

            rows.append(
                "<tr>"
                f"<td>{n}</td>"
                f'<td style="color:{color};font-weight:700">'
                f"{esc(d.kind)}</td>"
                f"<td>{esc(d.pdf_text)}</td>"
                f"<td>{esc(d.xhtml_text)}</td>"
                f"<td>{esc(d.pdf_page)}</td>"
                f"<td>{esc(d.pdf_bbox)}</td>"
                f"<td>{esc(d.xhtml_file)}</td>"
                f"<td>{esc(d.xhtml_line)}</td>"
                f"<td>{esc(d.style_property)}</td>"
                f"<td>{esc(d.pdf_unicode)}</td>"
                f"<td>{esc(d.xhtml_unicode)}</td>"
                f"<td>{esc(d.message)}</td>"
                "</tr>"
            )

        doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>EPUBForge PDF → XHTML Fidelity Report</title>
<style>
body {{
  font-family: Arial, sans-serif;
  margin: 24px;
  background: #f3f5f8;
  color: #172033;
}}
.card {{
  display: inline-block;
  background: white;
  border: 1px solid #d5dbe3;
  padding: 12px 18px;
  margin: 5px;
  min-width: 120px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: white;
}}
th, td {{
  border: 1px solid #d5dbe3;
  padding: 7px;
  text-align: left;
  vertical-align: top;
}}
th {{ background: #edf0f4; }}
</style>
</head>
<body>
<h1>EPUBForge PDF → XHTML Direct Fidelity QA</h1>

<p><b>PDF:</b> {esc(self.pdf_path)}</p>
<p><b>XHTML/EPUB:</b> {esc(self.xhtml_root)}</p>

<div class="card"><b>Overall</b><br>{self.fidelity_percent:.2f}%</div>
<div class="card"><b>Content</b><br>{self.content_fidelity_percent:.2f}%</div>
<div class="card"><b>Formatting</b><br>{self.formatting_fidelity_percent:.2f}%</div>
<div class="card"><b>Matched</b><br>{self.matched_words}</div>
<div class="card"><b>Missing</b><br>{self.missing_words}</div>
<div class="card"><b>Extra</b><br>{self.extra_words}</div>
<div class="card"><b>Changed</b><br>{self.changed_words}</div>
<div class="card"><b>Symbol errors</b><br>{self.symbol_errors}</div>
<div class="card"><b>Style errors</b><br>{self.style_errors}</div>

<table>
<thead>
<tr>
<th>#</th><th>Type</th><th>PDF</th><th>XHTML</th>
<th>PDF page</th><th>PDF bbox</th><th>XHTML file</th>
<th>Line</th><th>Style</th><th>PDF Unicode</th>
<th>XHTML Unicode</th><th>Details</th>
</tr>
</thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</body>
</html>"""

        Path(filename).write_text(doc, encoding="utf-8")


    def write_pdf(self, filename):
        """Generate a standalone PDF QA report using PyMuPDF only.

        This intentionally avoids ReportLab so the ZoneTool environment only
        needs its existing PyMuPDF dependency.
        """
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=842, height=595)
        margin = 28
        y = margin

        def add_line(text, size=9, color=(0, 0, 0), gap=12):
            nonlocal page, y
            if y > 565:
                page = doc.new_page(width=842, height=595)
                y = margin
            page.insert_text((margin, y), str(text)[:170], fontsize=size, color=color)
            y += gap

        add_line("EPUBForge PDF -> XHTML Direct Fidelity QA", 18, (0.05,0.1,0.2), 24)
        add_line(f"PDF: {self.pdf_path}", 8)
        add_line(f"XHTML/EPUB: {self.xhtml_root}", 8, gap=16)
        add_line(f"Overall Fidelity: {self.fidelity_percent:.2f}%   Content: {self.content_fidelity_percent:.2f}%   Formatting: {self.formatting_fidelity_percent:.2f}%", 10, gap=16)
        add_line(f"Matched: {self.matched_words}   Missing: {self.missing_words}   Extra: {self.extra_words}   Changed: {self.changed_words}   Symbols: {self.symbol_errors}   Styles: {self.style_errors}", 9, gap=18)
        add_line("DIFFERENCES", 13, (0.05,0.1,0.2), 18)

        for n, d in enumerate(self.differences, 1):
            if y > 520:
                page = doc.new_page(width=842, height=595)
                y = margin
            kind = str(getattr(d, "kind", ""))
            color = (0.75,0.05,0.05) if ("MISSING" in kind or "EXTRA" in kind or "SYMBOL" in kind or kind in {"CHANGED", "WRONG"}) else (0.8,0.45,0.0)
            add_line(f"{n}. {kind}", 10, color, 13)
            add_line(f"PDF page: {getattr(d,'pdf_page',None)}   PDF: {getattr(d,'pdf_text','')}", 8)
            add_line(f"XHTML: {getattr(d,'xhtml_text','')}   File: {getattr(d,'xhtml_file',None)}   Line: {getattr(d,'xhtml_line',None)}", 8)
            add_line(f"PDF Unicode: {getattr(d,'pdf_unicode','')}   XHTML Unicode: {getattr(d,'xhtml_unicode','')}", 8)
            add_line(f"Details: {getattr(d,'message','')}", 8, gap=15)

        doc.save(str(filename))
        doc.close()


# ============================================================================
# TEXT / UNICODE
# ============================================================================

def normalize_text(text):
    # NFC is intentional. Do NOT use NFKC.
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\u00ad", "")
    return WS_RE.sub(" ", text).strip()


def unicode_string(text):
    return " ".join(
        f"U+{ord(c):04X}"
        for c in (text or "")
    )


def token_kind(text):
    if not text:
        return "word"

    if all(
        unicodedata.category(c)[0] in {"L", "M"}
        for c in text
    ):
        return "word"

    if all(
        unicodedata.category(c)[0] == "N"
        for c in text
    ):
        return "number"

    if any(
        unicodedata.category(c)[0] == "S"
        for c in text
    ):
        return "symbol"

    if any(
        unicodedata.category(c)[0] == "P"
        for c in text
    ):
        return "punctuation"

    return "word"


def tokenize_exact(text):
    """
    Unicode-safe tokenization.

    Letters/numbers/marks are grouped.
    Punctuation and symbols are kept as atomic tokens.

    This means:
        ±
        -
        –
        —
        …
        '
        ’
    are distinct tokens.
    """
    text = normalize_text(text)

    if not text:
        return []

    out = []
    buf = []

    def flush():
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in text:
        if ch.isspace():
            flush()
            continue

        cat = unicodedata.category(ch)

        if cat[0] in {"L", "N", "M"}:
            buf.append(ch)
        else:
            flush()
            out.append(ch)

    flush()
    return out


def is_semantic_word(text):
    return bool(text) and (
        any(
            unicodedata.category(c)[0] == "L"
            for c in text
        )
        or any(
            unicodedata.category(c)[0] == "N"
            for c in text
        )
    )


def is_symbol_or_punctuation(text):
    return bool(text) and any(
        unicodedata.category(c)[0] in {"P", "S"}
        for c in text
    )


# ============================================================================
# XHTML CSS
# ============================================================================

def empty_style():
    return {
        "bold": False,
        "italic": False,
        "underline": False,
        "superscript": False,
        "subscript": False,
    }


def merge_style(a, b):
    r = dict(a)
    r.update(b)
    return r


def parse_css_declarations(value):
    result = {}

    for item in (value or "").split(";"):
        if ":" not in item:
            continue

        name, val = item.split(":", 1)
        name = name.strip().lower()
        val = val.strip().lower()

        if name == "font-weight":
            result["bold"] = (
                val in {
                    "bold", "bolder", "black",
                    "heavy", "semibold", "demibold",
                }
                or (
                    val.isdigit()
                    and int(val) >= 600
                )
            )

        elif name == "font-style":
            result["italic"] = val in {
                "italic", "oblique"
            }

        elif name == "text-decoration":
            result["underline"] = (
                "underline" in val
            )

        elif name == "vertical-align":
            if "super" in val:
                result["superscript"] = True
                result["subscript"] = False
            elif "sub" in val:
                result["subscript"] = True
                result["superscript"] = False

    return result


def parse_css_stylesheet(css):
    """
    Small, dependency-free CSS reader for EPUB stylesheets.

    Supports:
        tag
        .class
        #id
        tag.class
        tag#id

    It intentionally ignores media queries and complex selectors.
    """
    rules = {}

    css = re.sub(
        r"/\*.*?\*/",
        "",
        css or "",
        flags=re.S,
    )

    for selector_text, declarations in re.findall(
        r"([^{}]+)\{([^{}]*)\}",
        css,
        flags=re.S,
    ):
        props = parse_css_declarations(
            declarations
        )

        if not props:
            continue

        for selector in selector_text.split(","):
            selector = selector.strip().lower()

            if (
                " " in selector
                or ">" in selector
                or "+" in selector
                or "~" in selector
                or ":" in selector
            ):
                continue

            rules[selector] = props

    return rules


def css_for_element(tag, attrs, rules):
    tag = (tag or "").lower()
    cls = (attrs.get("class") or "").strip().lower()
    ident = (attrs.get("id") or "").strip().lower()

    out = {}

    selectors = [
        tag,
    ]

    if cls:
        for c in cls.split():
            selectors.append("." + c)
            selectors.append(tag + "." + c)

    if ident:
        selectors.append("#" + ident)
        selectors.append(tag + "#" + ident)

    for selector in selectors:
        if selector in rules:
            out.update(rules[selector])

    return out


# ============================================================================
# XHTML PARSING
# ============================================================================

def local_name(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def parse_xhtml(data):
    try:
        from lxml import etree

        parser = etree.XMLParser(
            recover=False,
            resolve_entities=False,
            no_network=True,
        )
        return etree.fromstring(
            data,
            parser,
        )
    except Exception:
        import xml.etree.ElementTree as ET
        return ET.fromstring(data)


def source_items(path):
    p = Path(path)

    if p.is_file() and p.suffix.lower() == ".epub":
        with zipfile.ZipFile(p, "r") as z:
            names = sorted(
                n for n in z.namelist()
                if n.lower().endswith(
                    (".xhtml", ".html", ".htm")
                )
            )

            for name in names:
                yield name, z.read(name)
        return

    if p.is_file():
        yield str(p), p.read_bytes()
        return

    for f in sorted(p.rglob("*")):
        if (
            f.is_file()
            and f.suffix.lower()
            in {".xhtml", ".html", ".htm"}
        ):
            yield str(f), f.read_bytes()


def load_css_for_source(
    filename,
    data,
    root_path,
):
    """
    Load linked CSS for an XHTML source.

    For an unpacked EPUB:
        XHTML directory / ../ CSS path

    For an EPUB zip:
        CSS files are read from the same archive through
        source_items is not enough, so this function simply
        returns inline rules when external resolution isn't
        available.
    """
    rules = {}

    try:
        root = parse_xhtml(data)
    except Exception:
        return rules

    base = Path(filename).parent

    for node in root.iter():
        if local_name(node.tag) != "link":
            continue

        attrs = dict(node.attrib)

        if (attrs.get("rel") or "").lower() != "stylesheet":
            continue

        href = attrs.get("href", "")
        if not href:
            continue

        href = href.split("#", 1)[0]

        if "://" in href:
            continue

        if root_path:
            candidate = (
                Path(root_path)
                / base
                / href
            )

            candidate = candidate.resolve()

            try:
                css = candidate.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                rules.update(
                    parse_css_stylesheet(css)
                )
            except Exception:
                pass

    # Inline <style>
    for node in root.iter():
        if local_name(node.tag) == "style":
            css = "".join(
                node.itertext()
            )
            rules.update(
                parse_css_stylesheet(css)
            )

    return rules


def append_tokens(
    text,
    style,
    filename,
    element,
    paragraph,
    line,
    output,
):
    for index, tok in enumerate(
        tokenize_exact(text),
        1,
    ):
        output.append(
            XHTMLToken(
                text=tok,
                file=filename,
                element=element,
                paragraph=paragraph,
                word_index=index,
                source_line=line,
                style=dict(style),
                token_kind=token_kind(tok),
            )
        )


def extract_xhtml_tokens(path):
    tokens = []
    paragraph_count = 0

    p = Path(path)
    unpacked_root = (
        p.parent if p.is_file()
        else p
    )

    for filename, data in source_items(path):
        try:
            root = parse_xhtml(data)
        except Exception:
            continue

        css_rules = load_css_for_source(
            filename,
            data,
            unpacked_root,
        )

        body = None

        for node in root.iter():
            if local_name(node.tag) == "body":
                body = node
                break

        if body is None:
            continue

        def walk(
            node,
            inherited,
            paragraph,
        ):
            nonlocal paragraph_count

            tag = local_name(node.tag)
            attrs = dict(
                getattr(node, "attrib", {}) or {}
            )

            style = merge_style(
                inherited,
                css_for_element(
                    tag,
                    attrs,
                    css_rules,
                ),
            )

            if tag in {"b", "strong"}:
                style["bold"] = True

            if tag in {"i", "em"}:
                style["italic"] = True

            if tag == "u":
                style["underline"] = True

            if tag == "sup":
                style["superscript"] = True
                style["subscript"] = False

            if tag == "sub":
                style["subscript"] = True
                style["superscript"] = False

            inline = parse_css_declarations(
                attrs.get("style", "")
            )
            style.update(inline)

            cls = (
                attrs.get("class") or ""
            ).lower()

            if re.search(
                r"(^|[-_\s])"
                r"(bold|fontbold)"
                r"([-_\s]|$)",
                cls,
            ):
                style["bold"] = True

            if re.search(
                r"(^|[-_\s])"
                r"(italic|ital|oblique)"
                r"([-_\s]|$)",
                cls,
            ):
                style["italic"] = True

            if re.search(
                r"(^|[-_\s])"
                r"(underline|underlined)"
                r"([-_\s]|$)",
                cls,
            ):
                style["underline"] = True

            if re.search(
                r"(^|[-_\s])"
                r"(sup|super|superscript)"
                r"([-_\s]|$)",
                cls,
            ):
                style["superscript"] = True
                style["subscript"] = False

            if re.search(
                r"(^|[-_\s])"
                r"(sub|subscript)"
                r"([-_\s]|$)",
                cls,
            ):
                style["subscript"] = True
                style["superscript"] = False

            line = int(
                getattr(
                    node,
                    "sourceline",
                    0,
                ) or 0
            )

            if tag in BLOCK_TAGS:
                paragraph_count += 1
                paragraph = paragraph_count

            if node.text:
                append_tokens(
                    node.text,
                    style,
                    filename,
                    tag,
                    paragraph or max(
                        1,
                        paragraph_count,
                    ),
                    line,
                    tokens,
                )

            for child in list(node):
                walk(
                    child,
                    style,
                    paragraph,
                )

                if getattr(
                    child,
                    "tail",
                    None,
                ):
                    append_tokens(
                        child.tail,
                        style,
                        filename,
                        tag,
                        paragraph or max(
                            1,
                            paragraph_count,
                        ),
                        line,
                        tokens,
                    )

        for child in list(body):
            walk(
                child,
                empty_style(),
                None,
            )

    return tokens, paragraph_count


# ============================================================================
# PDF STYLE
# ============================================================================

def font_name_style(font):
    name = (font or "").lower()

    bold = any(
        x in name
        for x in (
            "bold",
            "black",
            "heavy",
            "semibold",
            "demibold",
            "extrabold",
            "ultrabold",
        )
    )

    italic = any(
        x in name
        for x in (
            "italic",
            "oblique",
            "slanted",
            "inclined",
            "kursiv",
        )
    )

    return bold, italic


def pdf_flag_style(flags):
    flags = int(flags or 0)

    return {
        "bold": bool(flags & 16),
        "italic": bool(flags & 2),
        "underline": False,
        "superscript": bool(flags & 1),
        "subscript": False,
    }


def super_sub_style(span, line):
    """
    Conservative PDF superscript/subscript detection.

    Do not classify normal body text as subscript just because its span
    sits slightly below an asymmetric PDF line bounding box.

    A real super/subscript span must be substantially smaller than the
    normal line text AND have a meaningful baseline displacement.
    """
    result = {
        "superscript": False,
        "subscript": False,
    }

    try:
        size = float(span.get("size", 0) or 0)
        sb = span.get("bbox")
        lb = line.get("bbox")

        if not sb or not lb or size <= 0:
            return result

        sy0, sy1 = float(sb[1]), float(sb[3])
        ly0, ly1 = float(lb[1]), float(lb[3])

        line_height = max(1.0, ly1 - ly0)
        span_height = max(0.1, sy1 - sy0)

        # Normal spans can have a slightly different vertical extent from
        # the line bbox. Requiring a clearly smaller span avoids false
        # SUBSCRIPT_MISMATCH reports on ordinary words.
        if span_height > line_height * 0.72:
            return result

        origin = span.get("origin")
        baseline_y = None

        if isinstance(origin, (list, tuple)) and len(origin) >= 2:
            try:
                baseline_y = float(origin[1])
            except Exception:
                baseline_y = None

        if baseline_y is None:
            baseline_y = sy1

        # Approximate normal baseline from the bottom of the line bbox.
        delta = baseline_y - ly1

        displacement = max(
            1.5,
            line_height * 0.18,
            size * 0.30,
        )

        if delta < -displacement:
            result["superscript"] = True
        elif delta > displacement:
            result["subscript"] = True

    except Exception:
        pass

    return result

def underline_detected(page, bbox):
    try:
        target = fitz.Rect(*bbox)

        for annot in page.annots() or []:
            at = str(
                getattr(
                    annot,
                    "type",
                    "",
                )
            ).lower()

            if "underline" in at:
                if annot.rect.intersects(
                    target
                ):
                    return True
    except Exception:
        pass

    try:
        x0, y0, x1, y1 = bbox
        width = max(
            1.0,
            x1 - x0,
        )

        for drawing in page.get_drawings():
            rect = drawing.get("rect")

            if rect is None:
                continue

            if rect.height > 2.5:
                continue

            overlap = max(
                0.0,
                min(x1, rect.x1)
                - max(x0, rect.x0),
            )

            if overlap < width * 0.45:
                continue

            if y1 - 2 <= rect.y0 <= y1 + 5:
                return True

    except Exception:
        pass

    return False


# ============================================================================
# VISUAL ITALIC DETECTION
# ============================================================================

def _visual_slant_score(
    page,
    bbox,
    matrix_scale=3.0,
):
    """
    Estimate italic slant from the rendered glyphs.

    This is deliberately used as a SECONDARY signal only.

    Returns:
        None if unavailable
        positive score if vertical strokes tend to lean right
    """
    if not _CV_AVAILABLE:
        return None

    try:
        rect = fitz.Rect(*bbox)

        if rect.width < 2 or rect.height < 2:
            return None

        pix = page.get_pixmap(
            matrix=fitz.Matrix(
                matrix_scale,
                matrix_scale,
            ),
            clip=rect,
            colorspace=fitz.csGRAY,
            alpha=False,
        )

        if pix.width < 8 or pix.height < 8:
            return None

        arr = np.frombuffer(
            pix.samples,
            dtype=np.uint8,
        ).reshape(
            pix.height,
            pix.width,
        )

        # Black/gray text only.
        ink = (
            arr < 180
        ).astype(
            np.uint8
        ) * 255

        # Remove tiny isolated noise.
        kernel = np.ones(
            (2, 2),
            np.uint8,
        )

        ink = cv2.morphologyEx(
            ink,
            cv2.MORPH_OPEN,
            kernel,
        )

        edges = cv2.Canny(
            ink,
            40,
            120,
        )

        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180,
            threshold=max(
                5,
                int(min(
                    pix.width,
                    pix.height,
                ) * 0.08),
            ),
            minLineLength=max(
                4,
                int(
                    pix.height * 0.20
                ),
            ),
            maxLineGap=2,
        )

        deviations = []

        if lines is not None:
            for item in lines[:, 0]:
                xa, ya, xb, yb = map(
                    int,
                    item,
                )

                dx = xb - xa
                dy = yb - ya

                if abs(dy) < abs(dx) * 1.5:
                    continue

                # Angle away from vertical.
                angle = math.degrees(
                    math.atan2(
                        dx,
                        -dy,
                    )
                )

                # Normalize to [-90, 90].
                while angle > 90:
                    angle -= 180

                while angle < -90:
                    angle += 180

                if -35 <= angle <= 35:
                    deviations.append(
                        angle
                    )

        if len(deviations) < 2:
            return None

        # Positive means right-leaning upward strokes.
        return float(
            statistics.median(
                deviations
            )
        )

    except Exception:
        return None


def detect_visual_italic(
    page,
    bbox,
):
    """
    Conservative visual italic classifier.

    It only returns True when the raster signal is strong enough.

    IMPORTANT:
        This is not allowed to override an explicit PDF italic flag.
        It exists for PDFs whose font metadata is unreliable.
    """
    score = _visual_slant_score(
        page,
        bbox,
    )

    if score is None:
        return False

    # Conservative threshold.
    return score >= 10.0


# ============================================================================
# PDF EXTRACTION
# ============================================================================

def extract_pdf_tokens(pdf_path):
    tokens = []
    paragraph_count = 0

    doc = fitz.open(pdf_path)

    try:
        for page_number, page in enumerate(
            doc,
            1,
        ):
            data = page.get_text(
                "dict",
                sort=True,
            )

            for block in data.get(
                "blocks",
                [],
            ):
                if block.get("type") != 0:
                    continue

                paragraph_count += 1

                for line in block.get(
                    "lines",
                    [],
                ):
                    for span in line.get(
                        "spans",
                        [],
                    ):
                        raw = str(
                            span.get(
                                "text",
                                "",
                            )
                            or ""
                        )

                        if not raw.strip():
                            continue

                        bbox = tuple(
                            float(x)
                            for x in span.get(
                                "bbox",
                                (0, 0, 0, 0),
                            )
                        )

                        flags = int(
                            span.get(
                                "flags",
                                0,
                            )
                            or 0
                        )

                        font = str(
                            span.get(
                                "font",
                                "",
                            )
                            or ""
                        )

                        size = float(
                            span.get(
                                "size",
                                0,
                            )
                            or 0
                        )

                        style = pdf_flag_style(
                            flags
                        )

                        fb, fi = font_name_style(
                            font
                        )

                        style["bold"] |= fb
                        style["italic"] |= fi

                        style.update(
                            super_sub_style(
                                span,
                                line,
                            )
                        )

                        style["underline"] = (
                            underline_detected(
                                page,
                                bbox,
                            )
                        )

                        # Raster italic is only a secondary signal.
                        if not style["italic"]:
                            if detect_visual_italic(
                                page,
                                bbox,
                            ):
                                style["italic"] = True
                                style[
                                    "_visual_italic"
                                ] = True

                        toks = tokenize_exact(
                            raw
                        )

                        if not toks:
                            continue

                        x0, y0, x1, y1 = bbox
                        total_chars = max(
                            1,
                            len(raw),
                        )

                        cursor = x0

                        for tok in toks:
                            width = max(
                                0.1,
                                (x1 - x0)
                                * len(tok)
                                / total_chars,
                            )

                            token_bbox = (
                                round(
                                    cursor,
                                    2,
                                ),
                                round(
                                    y0,
                                    2,
                                ),
                                round(
                                    min(
                                        x1,
                                        cursor + width,
                                    ),
                                    2,
                                ),
                                round(
                                    y1,
                                    2,
                                ),
                            )

                            tokens.append(
                                PDFToken(
                                    text=tok,
                                    page=page_number,
                                    bbox=token_bbox,
                                    paragraph=paragraph_count,
                                    style=dict(
                                        style
                                    ),
                                    font=font,
                                    flags=flags,
                                    size=size,
                                    token_kind=token_kind(
                                        tok
                                    ),
                                )
                            )

                            cursor += width

    finally:
        doc.close()

    return tokens, paragraph_count


# ============================================================================
# ALIGNMENT
# ============================================================================

def semantic_indices(tokens):
    return [
        i
        for i, tok in enumerate(tokens)
        if is_semantic_word(tok.text)
    ]


def word_key(text):
    # Exact Unicode comparison.
    return normalize_text(text)


def _locate_pdf_token_index(result, diff):
    """Find the exact PDF proof token from page/bbox/text, not first text hit."""
    tokens = getattr(result, "pdf_tokens", []) or []
    text = getattr(diff, "pdf_text", "") or ""
    page = getattr(diff, "pdf_page", None)
    bbox = getattr(diff, "pdf_bbox", None)
    best = None
    for i, token in enumerate(tokens):
        if page is not None and getattr(token, "page", None) != page:
            continue
        if text and getattr(token, "text", "") != text:
            continue
        tb = getattr(token, "bbox", None)
        score = 0
        if bbox and tb:
            try:
                score = sum(abs(float(a)-float(b)) for a,b in zip(tb, bbox))
            except Exception:
                score = 999999
        if best is None or score < best[0]:
            best = (score, i)
    return best[1] if best else None


def _locate_xhtml_token_index(result, diff):
    """Find XHTML token by file + paragraph + word + text."""
    tokens = getattr(result, "xhtml_tokens", []) or []
    text = getattr(diff, "xhtml_text", "") or ""
    file_name = getattr(diff, "xhtml_file", None)
    para = getattr(diff, "xhtml_paragraph", None)
    word = getattr(diff, "xhtml_word", None)
    for i, token in enumerate(tokens):
        if file_name and getattr(token, "file", None) != file_name:
            continue
        if para is not None and getattr(token, "paragraph", None) != para:
            continue
        if word is not None and getattr(token, "word_index", None) != word:
            continue
        if text and getattr(token, "text", "") != text:
            continue
        return i
    return None


def add_difference(
    result,
    diff,
    max_differences,
):
    if len(result.differences) >= max_differences:
        return False

    # Store stable exact token indices for the GUI.  The old GUI searched by
    # text and therefore selected the first occurrence of repeated words.
    if getattr(diff, "pdf_token_index", None) is None:
        diff.pdf_token_index = _locate_pdf_token_index(result, diff)
    if getattr(diff, "xhtml_token_index", None) is None:
        diff.xhtml_token_index = _locate_xhtml_token_index(result, diff)

    result.differences.append(diff)
    return True


def _set_proof_status(tokens, status):
    """Mark extracted proof tokens for GUI highlighting."""
    for token in tokens:
        token.proof_status = status


def _set_difference_status(tokens, status):
    """Alias kept separate for readability at call sites."""
    _set_proof_status(tokens, status)


def _semantic_gap(tokens, semantic_positions, start, end):
    """Return raw punctuation/symbol tokens between semantic anchors.

    ``start`` and ``end`` are positions in the semantic-index list, not raw
    token offsets. This distinction is critical: using semantic_indices[start:end]
    would accidentally throw away punctuation such as "-", "±", "–", "—", etc.
    """
    if start >= end:
        return []

    raw_start = (
        semantic_positions[start - 1] + 1
        if start > 0
        else 0
    )
    raw_end = semantic_positions[end - 1]

    return [
        tokens[i]
        for i in range(raw_start, raw_end)
        if not is_semantic_word(tokens[i].text)
    ]


def _gap_between_semantic(tokens, semantic_positions, left_index, right_index):
    """Return raw non-semantic tokens strictly between two semantic words."""
    if left_index < 0 or right_index >= len(semantic_positions):
        return []
    raw_start = semantic_positions[left_index] + 1
    raw_end = semantic_positions[right_index]
    return [
        tokens[i]
        for i in range(raw_start, raw_end)
        if not is_semantic_word(tokens[i].text)
    ]


def _trailing_gap(tokens, semantic_positions):
    if not semantic_positions:
        return list(tokens)
    raw_start = semantic_positions[-1] + 1
    return [
        tokens[i]
        for i in range(raw_start, len(tokens))
        if not is_semantic_word(tokens[i].text)
    ]


def _bbox_union(token_list):
    boxes = [
        getattr(t, "bbox", None)
        for t in token_list
        if getattr(t, "bbox", None)
    ]
    if not boxes:
        return None

    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def compare_gap_symbols(
    pdf_gap,
    xhtml_gap,
    result,
    max_differences,
):
    """
    Compare punctuation/symbols between already aligned semantic words.

    This is the crucial anti-drift mechanism.
    """
    if not pdf_gap and not xhtml_gap:
        return

    p = [t.text for t in pdf_gap]
    x = [t.text for t in xhtml_gap]

    # Direct one-to-one punctuation/symbol replacement.
    if (
        len(pdf_gap) == 1
        and len(xhtml_gap) == 1
        and is_symbol_or_punctuation(
            pdf_gap[0].text
        )
        and is_symbol_or_punctuation(
            xhtml_gap[0].text
        )
    ):
        pw = pdf_gap[0]
        xw = xhtml_gap[0]

        if pw.text != xw.text:
            result.changed_words += 1
            pw.proof_status = "ERROR"
            xw.proof_status = "ERROR"

            add_difference(
                result,
                Difference(
                    kind="SYMBOL_MISMATCH",
                    severity="ERROR",
                    pdf_text=pw.text,
                    xhtml_text=xw.text,
                    pdf_page=pw.page,
                    pdf_bbox=pw.bbox,
                    xhtml_file=xw.file,
                    xhtml_element=xw.element,
                    xhtml_line=xw.source_line,
                    xhtml_paragraph=xw.paragraph,
                    xhtml_word=xw.word_index,
                    confidence=0.99,
                    message=(
                        "Different Unicode "
                        "punctuation/symbol. "
                        f"PDF={unicode_string(pw.text)}; "
                        f"XHTML={unicode_string(xw.text)}."
                    ),
                    pdf_style=dict(
                        pw.style
                    ),
                    xhtml_style=dict(
                        xw.style
                    ),
                    pdf_unicode=unicode_string(
                        pw.text
                    ),
                    xhtml_unicode=unicode_string(
                        xw.text
                    ),
                ),
                max_differences,
            )

        return

    matcher = SequenceMatcher(
        None,
        p,
        x,
        autojunk=False,
    )

    for tag, p0, p1, x0, x1 in (
        matcher.get_opcodes()
    ):
        if tag == "equal":
            continue

        ps = pdf_gap[p0:p1]
        xs = xhtml_gap[x0:x1]

        if tag == "delete":
            for pw in ps:
                result.changed_words += 1
                pw.proof_status = "ERROR"

                add_difference(
                    result,
                    Difference(
                        kind="MISSING_SYMBOL",
                        severity="ERROR",
                        pdf_text=pw.text,
                        xhtml_text="",
                        pdf_page=pw.page,
                        pdf_bbox=pw.bbox,
                        confidence=0.98,
                        message=(
                            "Punctuation/symbol is "
                            "present in PDF but missing "
                            "from XHTML."
                        ),
                        pdf_style=dict(
                            pw.style
                        ),
                        pdf_unicode=unicode_string(
                            pw.text
                        ),
                    ),
                    max_differences,
                )

        elif tag == "insert":
            for xw in xs:
                result.changed_words += 1
                xw.proof_status = "ERROR"

                add_difference(
                    result,
                    Difference(
                        kind="EXTRA_SYMBOL",
                        severity="ERROR",
                        pdf_text="",
                        xhtml_text=xw.text,
                        xhtml_file=xw.file,
                        xhtml_element=xw.element,
                        xhtml_line=xw.source_line,
                        xhtml_paragraph=xw.paragraph,
                        xhtml_word=xw.word_index,
                        confidence=0.98,
                        message=(
                            "Punctuation/symbol is "
                            "present in XHTML but "
                            "missing from PDF."
                        ),
                        xhtml_style=dict(
                            xw.style
                        ),
                        xhtml_unicode=unicode_string(
                            xw.text
                        ),
                    ),
                    max_differences,
                )

        else:
            pw = ps[0] if ps else None
            xw = xs[0] if xs else None

            result.changed_words += max(
                len(ps),
                len(xs),
            )
            _set_proof_status(ps, "ERROR")
            _set_proof_status(xs, "ERROR")

            add_difference(
                result,
                Difference(
                    kind="SYMBOL_MISMATCH",
                    severity="ERROR",
                    pdf_text="".join(
                        t.text for t in ps
                    ),
                    xhtml_text="".join(
                        t.text for t in xs
                    ),
                    pdf_page=(
                        pw.page
                        if pw else None
                    ),
                    pdf_bbox=(
                        pw.bbox
                        if pw else None
                    ),
                    xhtml_file=(
                        xw.file
                        if xw else None
                    ),
                    xhtml_element=(
                        xw.element
                        if xw else None
                    ),
                    xhtml_line=(
                        xw.source_line
                        if xw else None
                    ),
                    xhtml_paragraph=(
                        xw.paragraph
                        if xw else None
                    ),
                    xhtml_word=(
                        xw.word_index
                        if xw else None
                    ),
                    confidence=0.94,
                    message=(
                        "Punctuation/symbol sequence "
                        "differs."
                    ),
                    pdf_style=(
                        dict(pw.style)
                        if pw else {}
                    ),
                    xhtml_style=(
                        dict(xw.style)
                        if xw else {}
                    ),
                    pdf_unicode=unicode_string(
                        "".join(
                            t.text for t in ps
                        )
                    ),
                    xhtml_unicode=unicode_string(
                        "".join(
                            t.text for t in xs
                        )
                    ),
                ),
                max_differences,
            )



def _same_style(a, b, prop):
    return bool(a.get(prop, False)) == bool(b.get(prop, False))


def _merge_word_range(tokens, start, end):
    """
    Join adjacent token text into a human-readable formatting range.
    Keeps the source words exactly as extracted.
    """
    if start >= end:
        return ""
    return " ".join(
        t.text for t in tokens[start:end]
    ).strip()


def _formatting_range(
    pdf_tokens,
    xhtml_tokens,
    p_start,
    x_start,
    prop,
    max_words=40,
):
    """
    Expand a style mismatch over the contiguous aligned words that have
    the same formatting state on each side.

    This converts:
        Ethics
        Communities
        The
        Myth
        ...

    into meaningful ranges where the source formatting is continuous.

    The expansion stops at:
      * a paragraph boundary
      * a style-state change
      * a word alignment boundary
      * max_words
    """
    p_end = p_start
    x_end = x_start

    base_pdf = bool(
        pdf_tokens[p_start].style.get(prop, False)
    )
    base_xhtml = bool(
        xhtml_tokens[x_start].style.get(prop, False)
    )

    while (
        p_end < len(pdf_tokens)
        and x_end < len(xhtml_tokens)
        and (p_end - p_start) < max_words
        and (x_end - x_start) < max_words
    ):
        p = pdf_tokens[p_end]
        x = xhtml_tokens[x_end]

        if p.paragraph != pdf_tokens[p_start].paragraph:
            break
        if x.paragraph != xhtml_tokens[x_start].paragraph:
            break

        if bool(p.style.get(prop, False)) != base_pdf:
            break
        if bool(x.style.get(prop, False)) != base_xhtml:
            break

        p_end += 1
        x_end += 1

    return (
        p_end,
        x_end,
        _merge_word_range(
            pdf_tokens,
            p_start,
            p_end,
        ),
        _merge_word_range(
            xhtml_tokens,
            x_start,
            x_end,
        ),
    )


# ============================================================================
# MAIN COMPARISON
# ============================================================================

def compare_pdf_to_xhtml(
    pdf_path,
    xhtml_path_or_directory,
    *,
    max_differences=20000,
    compare_styles=True,
):
    pdf_tokens, pdf_paragraphs = (
        extract_pdf_tokens(
            pdf_path
        )
    )

    xhtml_tokens, xhtml_paragraphs = (
        extract_xhtml_tokens(
            xhtml_path_or_directory
        )
    )

    result = CompareResult(
        pdf_path=str(pdf_path),
        xhtml_root=str(
            xhtml_path_or_directory
        ),
        pdf_pages=max(
            (
                t.page
                for t in pdf_tokens
            ),
            default=0,
        ),
        pdf_words=sum(
            1
            for t in pdf_tokens
            if is_semantic_word(t.text)
        ),
        xhtml_words=sum(
            1
            for t in xhtml_tokens
            if is_semantic_word(t.text)
        ),
        pdf_paragraphs=pdf_paragraphs,
        xhtml_paragraphs=xhtml_paragraphs,
    )

    # Keep the complete extracted token streams in memory for the interactive
    # proof panes. They are never serialized to the report.
    result.pdf_tokens = pdf_tokens
    result.xhtml_tokens = xhtml_tokens

    # Start from green: every token is considered an exact proof match until
    # an actual content/symbol/style mismatch marks it otherwise.
    _set_proof_status(pdf_tokens, "MATCH")
    _set_proof_status(xhtml_tokens, "MATCH")

    p_indices = semantic_indices(
        pdf_tokens
    )
    x_indices = semantic_indices(
        xhtml_tokens
    )

    p_words = [
        word_key(
            pdf_tokens[i].text
        )
        for i in p_indices
    ]

    x_words = [
        word_key(
            xhtml_tokens[i].text
        )
        for i in x_indices
    ]

    matcher = SequenceMatcher(
        None,
        p_words,
        x_words,
        autojunk=False,
    )

    previous_p = 0
    previous_x = 0

    for tag, p0, p1, x0, x1 in (
        matcher.get_opcodes()
    ):
        # Symbols between semantic anchors. IMPORTANT: these are raw token
        # slices, not slices of semantic_indices, otherwise punctuation and
        # symbols (e.g. PDF "–" vs XHTML "±") disappear before comparison.
        compare_gap_symbols(
            _semantic_gap(pdf_tokens, p_indices, previous_p, p0),
            _semantic_gap(xhtml_tokens, x_indices, previous_x, x0),
            result,
            max_differences,
        )

        if tag == "equal":
            # Compare every punctuation/symbol gap INSIDE an equal semantic
            # word run. This catches "ethics – as" vs "ethics ± as" without
            # disturbing the semantic word alignment.
            for gap_pos in range(p0, max(p0, p1 - 1)):
                # Compare the RAW tokens between the two adjacent semantic
                # anchors. This is where punctuation such as "-" / "–" / "±"
                # lives.
                compare_gap_symbols(
                    _gap_between_semantic(pdf_tokens, p_indices, gap_pos, gap_pos + 1),
                    _gap_between_semantic(xhtml_tokens, x_indices, gap_pos, gap_pos + 1),
                    result,
                    max_differences,
                )

            count = min(
                p1 - p0,
                x1 - x0,
            )

            result.matched_words += count

            if compare_styles:
                for offset in range(
                    count
                ):
                    pw = pdf_tokens[
                        p_indices[
                            p0 + offset
                        ]
                    ]

                    xw = xhtml_tokens[
                        x_indices[
                            x0 + offset
                        ]
                    ]

                    # Exact aligned text is GREEN in the proof panes.
                    pw.proof_status = "MATCH"
                    xw.proof_status = "MATCH"

                    # Formatting is reported as contiguous ranges,
                    # not one error per word.
                    #
                    # Example:
                    #   Christian Ethics in Secular Worlds
                    #
                    # becomes ONE italic mismatch rather than several
                    # separate "Ethics", "in", "Secular", "Worlds" errors.
                    for prop in STYLE_PROPERTIES:
                        pv = bool(
                            pw.style.get(
                                prop,
                                False,
                            )
                        )
                        xv = bool(
                            xw.style.get(
                                prop,
                                False,
                            )
                        )

                        if pv == xv:
                            continue

                        if prop == "italic":
                            kind = "ITALIC_MISMATCH"
                        elif prop == "bold":
                            kind = "BOLD_MISMATCH"
                        elif prop == "underline":
                            kind = "UNDERLINE_MISMATCH"
                        elif prop == "superscript":
                            kind = "SUPERSCRIPT_MISMATCH"
                        elif prop == "subscript":
                            kind = "SUBSCRIPT_MISMATCH"
                        else:
                            kind = "STYLE_MISMATCH"

                        # The outer aligned words are available through the
                        # current p0/x0 block. Expand only inside that block.
                        range_p_end = min(
                            p1,
                            p_indices.__len__(),
                        )
                        range_x_end = min(
                            x1,
                            x_indices.__len__(),
                        )

                        rp = p0 + offset
                        rx = x0 + offset

                        p_run_end = rp + 1
                        x_run_end = rx + 1

                        while (
                            p_run_end < range_p_end
                            and x_run_end < range_x_end
                        ):
                            pp = pdf_tokens[
                                p_indices[p_run_end]
                            ]
                            xx = xhtml_tokens[
                                x_indices[x_run_end]
                            ]

                            if pp.paragraph != pw.paragraph:
                                break
                            if xx.paragraph != xw.paragraph:
                                break

                            if bool(
                                pp.style.get(
                                    prop,
                                    False,
                                )
                            ) != pv:
                                break

                            if bool(
                                xx.style.get(
                                    prop,
                                    False,
                                )
                            ) != xv:
                                break

                            p_run_end += 1
                            x_run_end += 1

                        pdf_range_tokens = [
                            pdf_tokens[
                                p_indices[i]
                            ]
                            for i in range(
                                rp,
                                p_run_end,
                            )
                        ]

                        xhtml_range_tokens = [
                            xhtml_tokens[
                                x_indices[i]
                            ]
                            for i in range(
                                rx,
                                x_run_end,
                            )
                        ]

                        pdf_range = " ".join(
                            t.text
                            for t in pdf_range_tokens
                        )
                        xhtml_range = " ".join(
                            t.text
                            for t in xhtml_range_tokens
                        )

                        # Same text but wrong typography = YELLOW.
                        _set_proof_status(pdf_range_tokens, "STYLE")
                        _set_proof_status(xhtml_range_tokens, "STYLE")

                        result.style_errors += 1

                        # Skip the later words of this same formatting run.
                        # They are already represented by this one range.
                        # The loop remains safe because we don't mutate the
                        # SequenceMatcher indices.
                        add_difference(
                            result,
                            Difference(
                                kind=kind,
                                severity="WARNING",
                                pdf_text=pw.text,
                                xhtml_text=xw.text,
                                pdf_page=pw.page,
                                pdf_bbox=(
                                    pdf_range_tokens[0].bbox
                                    if pdf_range_tokens
                                    else pw.bbox
                                ),
                                xhtml_file=xw.file,
                                xhtml_element=xw.element,
                                xhtml_line=xw.source_line,
                                xhtml_paragraph=xw.paragraph,
                                xhtml_word=xw.word_index,
                                confidence=(
                                    0.90
                                    if prop == "italic"
                                    else 0.96
                                ),
                                message=(
                                    f"{prop.upper()} "
                                    "formatting differs over "
                                    f"{len(pdf_range_tokens)} "
                                    "aligned word(s). "
                                    f"PDF={pv}; XHTML={xv}."
                                ),
                                pdf_style=dict(
                                    pw.style
                                ),
                                xhtml_style=dict(
                                    xw.style
                                ),
                                style_property=prop,
                                range_text=(
                                    f"PDF: {pdf_range}\\n"
                                    f"XHTML: {xhtml_range}"
                                ),
                                pdf_unicode=unicode_string(
                                    pdf_range
                                ),
                                xhtml_unicode=unicode_string(
                                    xhtml_range
                                ),
                            ),
                            max_differences,
                        )

        elif tag == "delete":
            for pi in range(
                p0,
                p1,
            ):
                pw = pdf_tokens[
                    p_indices[pi]
                ]

                result.missing_words += 1
                pw.proof_status = "ERROR"

                add_difference(
                    result,
                    Difference(
                        kind="MISSING",
                        severity="ERROR",
                        pdf_text=pw.text,
                        xhtml_text="",
                        pdf_page=pw.page,
                        pdf_bbox=pw.bbox,
                        confidence=0.98,
                        message=(
                            "Text exists in PDF but "
                            "is missing from XHTML."
                        ),
                        pdf_style=dict(
                            pw.style
                        ),
                        pdf_unicode=unicode_string(
                            pw.text
                        ),
                    ),
                    max_differences,
                )

        elif tag == "insert":
            for xi in range(
                x0,
                x1,
            ):
                xw = xhtml_tokens[
                    x_indices[xi]
                ]

                result.extra_words += 1
                xw.proof_status = "ERROR"

                add_difference(
                    result,
                    Difference(
                        kind=(
                            "EXTRA_SUPERSCRIPT"
                            if xw.style.get("superscript")
                            else (
                                "EXTRA_SUBSCRIPT"
                                if xw.style.get("subscript")
                                else "EXTRA"
                            )
                        ),
                        severity="ERROR",
                        pdf_text="",
                        xhtml_text=xw.text,
                        xhtml_file=xw.file,
                        xhtml_element=xw.element,
                        xhtml_line=xw.source_line,
                        xhtml_paragraph=xw.paragraph,
                        xhtml_word=xw.word_index,
                        confidence=0.98,
                        message=(
                            "Text exists in XHTML but "
                            "has no matching PDF text."
                        ),
                        xhtml_style=dict(
                            xw.style
                        ),
                        style_property=(
                            "superscript"
                            if xw.style.get("superscript")
                            else (
                                "subscript"
                                if xw.style.get("subscript")
                                else ""
                            )
                        ),
                        xhtml_unicode=unicode_string(
                            xw.text
                        ),
                    ),
                    max_differences,
                )

        else:
            ps = [
                pdf_tokens[
                    p_indices[i]
                ]
                for i in range(
                    p0,
                    p1,
                )
            ]

            xs = [
                xhtml_tokens[
                    x_indices[i]
                ]
                for i in range(
                    x0,
                    x1,
                )
            ]

            if (
                len(ps) == 1
                and len(xs) == 1
            ):
                pw = ps[0]
                xw = xs[0]

                result.changed_words += 1
                pw.proof_status = "ERROR"
                xw.proof_status = "ERROR"

                add_difference(
                    result,
                    Difference(
                        kind="CHANGED",
                        severity="ERROR",
                        pdf_text=pw.text,
                        xhtml_text=xw.text,
                        pdf_page=pw.page,
                        pdf_bbox=pw.bbox,
                        xhtml_file=xw.file,
                        xhtml_element=xw.element,
                        xhtml_line=xw.source_line,
                        xhtml_paragraph=xw.paragraph,
                        xhtml_word=xw.word_index,
                        confidence=0.96,
                        message=(
                            "Text differs at the same "
                            "semantic position."
                        ),
                        pdf_style=dict(
                            pw.style
                        ),
                        xhtml_style=dict(
                            xw.style
                        ),
                        pdf_unicode=unicode_string(
                            pw.text
                        ),
                        xhtml_unicode=unicode_string(
                            xw.text
                        ),
                    ),
                    max_differences,
                )

            else:
                result.changed_words += max(
                    len(ps),
                    len(xs),
                )

                pw = ps[0] if ps else None
                xw = xs[0] if xs else None
                _set_proof_status(ps, "ERROR")
                _set_proof_status(xs, "ERROR")

                add_difference(
                    result,
                    Difference(
                        kind="CHANGED",
                        severity="ERROR",
                        pdf_text=" ".join(
                            t.text for t in ps
                        ),
                        xhtml_text=" ".join(
                            t.text for t in xs
                        ),
                        pdf_page=(
                            pw.page
                            if pw else None
                        ),
                        pdf_bbox=(
                            pw.bbox
                            if pw else None
                        ),
                        xhtml_file=(
                            xw.file
                            if xw else None
                        ),
                        xhtml_element=(
                            xw.element
                            if xw else None
                        ),
                        xhtml_line=(
                            xw.source_line
                            if xw else None
                        ),
                        xhtml_paragraph=(
                            xw.paragraph
                            if xw else None
                        ),
                        xhtml_word=(
                            xw.word_index
                            if xw else None
                        ),
                        confidence=0.90,
                        message=(
                            "Aligned semantic text differs."
                        ),
                        pdf_style=(
                            dict(pw.style)
                            if pw else {}
                        ),
                        xhtml_style=(
                            dict(xw.style)
                            if xw else {}
                        ),
                    ),
                    max_differences,
                )

        previous_p = p1
        previous_x = x1

    # Final symbol/punctuation gap after the last semantic word.
    compare_gap_symbols(
        _trailing_gap(pdf_tokens, p_indices),
        _trailing_gap(xhtml_tokens, x_indices),
        result,
        max_differences,
    )

    # Collapse duplicate formatting reports produced by the word-level
    # alignment loop. Content differences are never collapsed.
    if result.differences:
        collapsed = []
        seen_style = set()

        for d in result.differences:
            if d.kind.endswith("_MISMATCH"):
                key = (
                    d.kind,
                    d.pdf_page,
                    d.xhtml_file,
                    d.xhtml_paragraph,
                    d.style_property,
                    d.range_text,
                )
                if key in seen_style:
                    continue
                seen_style.add(key)

            collapsed.append(d)

        result.differences = collapsed
        result.style_errors = sum(
            1
            for d in result.differences
            if d.kind.endswith("_MISMATCH")
            and "SYMBOL" not in d.kind
        )

    return result


# ============================================================================
# UI ADAPTER
# ============================================================================

def issue_to_fidelity_record(issue):
    if (
        "MISMATCH" in issue.kind
        and "SYMBOL" not in issue.kind
    ):
        category = "style"
    elif "SYMBOL" in issue.kind:
        category = "unicode"
    else:
        category = "content"

    return {
        "type": issue.kind,
        "severity": issue.severity,
        "category": category,
        "confidence": issue.confidence,

        "pages": str(
            issue.pdf_page or ""
        ),
        "pdf_page": issue.pdf_page,
        "pdf_bbox": issue.pdf_bbox,

        "xhtml_file": issue.xhtml_file,
        "xhtml_element": issue.xhtml_element,
        "xhtml_line": issue.xhtml_line,
        "xhtml_paragraph": issue.xhtml_paragraph,
        "xhtml_word": issue.xhtml_word,

        "pdf_text": issue.pdf_text,
        "xhtml_text": issue.xhtml_text,

        "pdf_style": issue.pdf_style,
        "xhtml_style": issue.xhtml_style,
        "style_property": issue.style_property,

        "pdf_unicode": issue.pdf_unicode,
        "xhtml_unicode": issue.xhtml_unicode,

        "message": issue.message,
    }


def compare_and_write_report(
    pdf_path,
    xhtml_path_or_directory,
    html_report,
    json_report=None,
):
    result = compare_pdf_to_xhtml(
        pdf_path,
        xhtml_path_or_directory,
        compare_styles=True,
    )

    result.write_html(
        html_report
    )

    if json_report:
        result.write_json(
            json_report
        )

    return result


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "EPUBForge PDF -> XHTML direct "
            "content + Unicode + formatting QA"
        )
    )

    parser.add_argument("pdf")
    parser.add_argument("xhtml")
    parser.add_argument(
        "--html",
        default="pdf_xhtml_report.html",
    )
    parser.add_argument(
        "--json",
        default="pdf_xhtml_report.json",
    )

    args = parser.parse_args()

    result = compare_pdf_to_xhtml(
        args.pdf,
        args.xhtml,
        compare_styles=True,
    )

    result.write_html(
        args.html
    )
    result.write_json(
        args.json
    )

    print()
    print(
        "EPUBForge PDF -> XHTML "
        "DIRECT FIDELITY QA"
    )
    print(
        "========================================="
    )
    print(
        f"PDF pages           : {result.pdf_pages}"
    )
    print(
        f"PDF words           : {result.pdf_words}"
    )
    print(
        f"XHTML words         : {result.xhtml_words}"
    )
    print(
        f"Matched             : {result.matched_words}"
    )
    print(
        f"Content fidelity    : "
        f"{result.content_fidelity_percent:.2f}%"
    )
    print(
        f"Formatting fidelity : "
        f"{result.formatting_fidelity_percent:.2f}%"
    )
    print(
        f"Overall fidelity    : "
        f"{result.fidelity_percent:.2f}%"
    )
    print(
        f"Missing             : {result.missing_words}"
    )
    print(
        f"Extra               : {result.extra_words}"
    )
    print(
        f"Changed             : {result.changed_words}"
    )
    print(
        f"Symbol errors       : {result.symbol_errors}"
    )
    print(
        f"Style errors        : {result.style_errors}"
    )
    print(
        f"Differences         : {result.total_changes}"
    )

    if result.differences:
        print()
        print("DIFFERENCES")
        print("-----------")

        for d in result.differences:
            print(
                f"{d.kind:22} "
                f"page={str(d.pdf_page or '-'):>3} "
                f"PDF={d.pdf_text!r} "
                f"XHTML={d.xhtml_text!r} "
                f"style={d.style_property or '-'}"
            )
            print(
                f"  PDF Unicode   : "
                f"{d.pdf_unicode}"
            )
            print(
                f"  XHTML Unicode : "
                f"{d.xhtml_unicode}"
            )
            print(
                f"  {d.message}"
            )
