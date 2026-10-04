"""HTML report (spec sections 58/59) - self-contained (no external
assets), covering every required section: Executive Summary, Overall
Fidelity, per-category fidelity (Content/Structure/Unicode/Layout/
Figure/Table/Hyphenation/Link), OCR Confidence, Detailed Differences,
Page-by-Page Results, and a dedicated Layout Differences section. Links
to `file://` PDF paths with a `#page=N` fragment (supported by most
desktop PDF viewers) stand in for "clickable navigation to both PDFs" -
a static HTML file has no way to embed a live, scrollable PDF viewer of
its own."""
import html
import pathlib
from datetime import datetime

_CATEGORY_LABELS = {
    "content": "Content Fidelity", "structure": "Structure Fidelity", "unicode": "Unicode Fidelity",
    "layout": "Layout Fidelity", "figure": "Figure Fidelity", "table": "Table Fidelity",
    "hyphenation": "Hyphenation Fidelity", "link": "Link Fidelity", "ocr": "OCR Confidence",
}

_SEVERITY_COLOR = {"HIGH": "#c0392b", "MEDIUM": "#d68910", "LOW": "#2471a3"}

_STYLE = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; padding: 24px 40px;
       background: #f7f8fa; color: #1c1e21; }
h1, h2 { color: #14213d; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;
                margin: 16px 0 28px; }
.score-card { background: #fff; border: 1px solid #e2e4e8; border-radius: 8px; padding: 14px 16px; }
.score-card .value { font-size: 26px; font-weight: 700; }
.score-card .label { font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: .04em; }
table { border-collapse: collapse; width: 100%; margin-bottom: 28px; background: #fff; }
th, td { border: 1px solid #e2e4e8; padding: 6px 10px; font-size: 13px; text-align: left; }
th { background: #eef1f5; }
.sev { font-weight: 700; padding: 1px 6px; border-radius: 4px; color: #fff; font-size: 11px; }
.mono { font-family: Consolas, monospace; }
.filters { margin-bottom: 10px; }
"""


def _fmt(v) -> str:
    return html.escape("" if v is None else str(v))


def _sev_badge(sev: str) -> str:
    color = _SEVERITY_COLOR.get(sev, "#555")
    return f'<span class="sev" style="background:{color}">{_fmt(sev)}</span>'


def _pdf_link(path: str, page) -> str:
    if not path:
        return ""
    uri = pathlib.Path(path).resolve().as_uri()
    frag = f"#page={page}" if page else ""
    return f'<a href="{uri}{frag}" target="_blank">page {page}</a>' if page else f'<a href="{uri}">open</a>'


def build(differences: list, scores, original_pdf_path: str = "", converted_pdf_path: str = "") -> str:
    score_dict = scores.to_dict()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_cards = [f"""<div class="score-card"><div class="value">{score_dict['overall_fidelity_pct']:.2f}%</div>
        <div class="label">Overall Fidelity</div></div>"""]
    for cat, data in score_dict["categories"].items():
        label = _CATEGORY_LABELS.get(cat, cat.title())
        summary_cards.append(f"""<div class="score-card"><div class="value">{data['verified_match_pct']:.2f}%</div>
            <div class="label">{_fmt(label)}</div></div>""")

    diff_rows = []
    layout_rows = []
    for d in differences:
        row = f"""<tr>
            <td>{_fmt(d['id'])}</td><td>{_fmt(d['type'])}</td><td>{_fmt(d['category'])}</td>
            <td>{_sev_badge(d['severity'])}</td><td>{_fmt(d['confidence'])} ({d['confidence_score']:.2f})</td>
            <td>{_pdf_link(original_pdf_path, d['original_page'])}</td>
            <td>{_pdf_link(converted_pdf_path, d['converted_page'])}</td>
            <td class="mono">{_fmt(d['original_text'])}</td><td class="mono">{_fmt(d['converted_text'])}</td>
            <td>{_fmt(d['explanation'])}</td></tr>"""
        diff_rows.append(row)
        if d["category"] == "layout":
            ol = d.get("original_layout") or {}
            cl = d.get("converted_layout") or {}
            layout_rows.append(f"""<tr>
                <td>{_fmt(d['type'])}</td>
                <td>{_pdf_link(original_pdf_path, d['original_page'])}</td>
                <td>{_pdf_link(converted_pdf_path, d['converted_page'])}</td>
                <td class="mono">{_fmt(d['original_text'])}</td>
                <td class="mono">{_fmt(ol)}</td><td class="mono">{_fmt(cl)}</td>
                <td>{_sev_badge(d['severity'])}</td><td>{d['confidence_score']:.2%}</td></tr>""")

    pages = sorted({d["original_page"] for d in differences if d.get("original_page")}
                    | {d["converted_page"] for d in differences if d.get("converted_page")})
    page_sections = []
    for page in pages:
        page_diffs = [d for d in differences if d.get("original_page") == page or d.get("converted_page") == page]
        items = "".join(f"<li>{_fmt(d['type'])} - {_sev_badge(d['severity'])} - {_fmt(d['explanation'])}</li>"
                         for d in page_diffs)
        page_sections.append(f"<h3>Page {page}</h3><ul>{items}</ul>")

    return f"""<title>EPUBForge Fidelity Compare Report</title>
<style>{_STYLE}</style>
<h1>EPUBForge - Advanced Fidelity Compare Report</h1>
<p>Generated {generated}</p>

<h2>Executive Summary</h2>
<div class="summary-grid">{''.join(summary_cards)}</div>

<h2>Detailed Differences ({len(differences)} total)</h2>
<table>
<tr><th>ID</th><th>Type</th><th>Category</th><th>Severity</th><th>Confidence</th>
<th>Original</th><th>Converted</th><th>Original Text</th><th>Converted Text</th><th>Explanation</th></tr>
{''.join(diff_rows)}
</table>

<h2>Layout Differences</h2>
<table>
<tr><th>Type</th><th>Original</th><th>Converted</th><th>Text</th><th>Original Layout</th>
<th>Converted Layout</th><th>Severity</th><th>Confidence</th></tr>
{''.join(layout_rows) or '<tr><td colspan="8">No layout differences found.</td></tr>'}
</table>

<h2>Page-by-Page Results</h2>
{''.join(page_sections) or '<p>No page-anchored differences.</p>'}
"""


def write(differences: list, scores, out_path: str, original_pdf_path: str = "", converted_pdf_path: str = ""):
    diff_dicts = [d.to_dict() if hasattr(d, "to_dict") else d for d in differences]
    html_content = build(diff_dicts, scores, original_pdf_path, converted_pdf_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return out_path
