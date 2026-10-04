"""PDF <-> XHTML comparison report (same formats the existing Fidelity
Compare module exports: JSON, HTML, CSV, PDF). Contains the overall score,
page scores, split scores and every difference (missing / extra / modified /
reordered / duplicate text, image, caption, page-marker, link, split and
low-confidence problems) with its location and decision status."""
import csv
import html
import json
import os

STATE_COLORS = {"MATCH": "#2e7d32", "MISSING": "#c62828", "EXTRA": "#c62828", "MODIFIED": "#b88a00",
                "MOVED": "#1565c0", "DUPLICATE": "#ef6c00", "UNCERTAIN": "#6a1b9a"}


def _rows(session):
    for n, d in enumerate(session.differences, 1):
        yield {
            "n": n, "id": d.id, "type": d.kind, "state": d.state, "severity": d.severity, "status": d.status,
            "confidence": round(d.confidence * 100, 1), "pdf_page": d.pdf_page, "split": d.split or "",
            "sourceline": d.sourceline, "pdf_text": d.pdf_text, "xhtml_text": d.xhtml_text, "message": d.message,
            "suggestion": d.correction.description if d.correction else "",
            "auto_safe": bool(d.correction and d.correction.auto_safe),
        }


def to_dict(session):
    m = session.mapping
    return {
        "pdf": session.pdf.path if session.pdf else "",
        "package": session.mgr.source_epub or session.mgr.root,
        "scores": m.scores,
        "pages": [{"page": p.page, "printed": p.printed, "splits": p.splits, "confidence": round(p.confidence, 3),
                   **p.scores} for p in m.pages],
        "splits": [{"split": s.split, "pdf_pages": [s.pages[0], s.pages[-1]] if s.pages else [], **s.scores}
                   for s in m.split_maps.values()],
        "differences": list(_rows(session)),
    }


def export(session, path):
    ext = os.path.splitext(path)[1].lower()
    data = to_dict(session)
    if ext == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    elif ext == ".csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            rows = data["differences"]
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["n"])
            w.writeheader()
            w.writerows(rows)
    elif ext == ".pdf":
        _pdf(data, path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_html(data))
    return path


def _html(data):
    e = html.escape
    cards = "".join(f'<div class="card"><b>{e(k)}</b><br>{v}%</div>' for k, v in data["scores"].items())
    pages = "".join(f"<tr><td>{p['page']}</td><td>{e(str(p['printed'] or ''))}</td>"
                    f"<td>{e(', '.join(p['splits']))}</td><td>{p.get('Match')}</td><td>{p.get('Text')}</td>"
                    f"<td>{p.get('Images')}</td><td>{p.get('Page Marker')}</td><td>{p.get('Differences')}</td></tr>"
                    for p in data["pages"])
    splits = "".join(f"<tr><td>{e(s['split'])}</td><td>{e('-'.join(map(str, s['pdf_pages'])))}</td>"
                     f"<td>{s.get('Match')}</td><td>{s.get('Missing')}</td><td>{s.get('Extra')}</td>"
                     f"<td>{s.get('Images')}</td><td>{s.get('Page Markers')}</td><td>{s.get('Links')}</td>"
                     f"<td>{s.get('Warnings')}</td></tr>" for s in data["splits"])
    diffs = "".join(
        f"<tr><td>{d['n']}</td><td style='color:{STATE_COLORS.get(d['state'], '#000')};font-weight:700'>"
        f"{e(d['type'])}<br><small>{e(d['state'])}</small></td><td>{e(d['severity'] or '')}</td>"
        f"<td>{d['confidence']}%</td><td>{e(str(d['pdf_page'] or ''))}</td><td>{e(d['split'])}</td>"
        f"<td>{e(d['pdf_text'][:300])}</td><td>{e(d['xhtml_text'][:300])}</td><td>{e(d['message'])}</td>"
        f"<td>{e(d['suggestion'])}</td><td>{e(d['status'])}</td></tr>" for d in data["differences"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>PDF - XHTML Comparison Report</title>
<style>body{{font-family:Arial,sans-serif;margin:24px;color:#1a2233}}.card{{display:inline-block;border:1px solid #ccd;
padding:10px 16px;margin:4px;min-width:110px}}table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #d5dbe3;padding:5px;vertical-align:top;font-size:12px}}th{{background:#eef1f5}}</style></head>
<body><h1>PDF &#8596; XHTML Comparison Report</h1><p><b>PDF:</b> {e(data['pdf'])}<br><b>Package:</b> {e(data['package'])}</p>
{cards}
<h2>Differences ({len(data['differences'])})</h2><table><tr><th>#</th><th>Type</th><th>Severity</th><th>Confidence</th>
<th>PDF page</th><th>Split</th><th>PDF</th><th>XHTML</th><th>Details</th><th>Suggested action</th><th>Status</th></tr>
{diffs}</table>
<h2>Page scores</h2><table><tr><th>Page</th><th>Printed</th><th>Splits</th><th>Match %</th><th>Text %</th><th>Images %</th>
<th>Page marker %</th><th>Differences</th></tr>{pages}</table>
<h2>Split scores</h2><table><tr><th>Split</th><th>PDF pages</th><th>Match %</th><th>Missing</th><th>Extra</th><th>Images</th>
<th>Page markers</th><th>Links</th><th>Warnings</th></tr>{splits}</table></body></html>"""


def _pdf(data, path):
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=842.0, height=595.0)
    y = [30.0]

    def line(text, size=9, color=(0, 0, 0)):
        if y[0] > 565:
            nonlocal page
            page = doc.new_page(width=842.0, height=595.0)
            y[0] = 30.0
        page.insert_text((28, y[0]), str(text)[:175], fontsize=size, color=color)
        y[0] += size + 4

    line("PDF <-> XHTML Comparison Report", 16)
    line(f"PDF: {data['pdf']}", 8)
    line(f"Package: {data['package']}", 8)
    line("   ".join(f"{k}: {v}%" for k, v in data["scores"].items()), 10)
    y[0] += 6
    for d in data["differences"]:
        col = tuple(int(STATE_COLORS.get(d["state"], "#000000")[i:i + 2], 16) / 255 for i in (1, 3, 5))
        line(f"{d['n']}. {d['type']} [{d['severity']}] {d['confidence']}%  page {d['pdf_page']}  {d['split']}  "
             f"({d['status']})", 9, col)
        if d["pdf_text"]:
            line(f"     PDF: {d['pdf_text'][:150]}", 8)
        if d["xhtml_text"]:
            line(f"     XHTML: {d['xhtml_text'][:150]}", 8)
        line(f"     {d['message'][:160]}", 8)
    doc.save(path)
    doc.close()
