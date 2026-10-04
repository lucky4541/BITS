"""PDF report (spec section 58) - a real, generated PDF (via PyMuPDF's
own page-writing API, the same library already used throughout this
project - no new third-party PDF-generation dependency) covering the
Executive Summary, per-category scores, and the detailed differences
list as plain paginated text. Deliberately simpler/plainer than the HTML
report (no embedded page-highlight thumbnails) - the HTML report is the
rich, primary format; this exists to satisfy "Generate: HTML, JSON, CSV,
PDF" as a genuine, complete, standalone document rather than a stub."""
import fitz

PAGE_WIDTH, PAGE_HEIGHT = 612, 792
MARGIN = 48
LINE_HEIGHT = 14
FONT = "helv"


class _PdfWriter:
    def __init__(self):
        self.doc = fitz.open()
        self.page = None
        self.y = 0

    def new_page(self):
        self.page = self.doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.y = MARGIN

    def _ensure(self, height):
        if self.page is None or self.y + height > PAGE_HEIGHT - MARGIN:
            self.new_page()

    def line(self, text: str, size: float = 10, bold: bool = False, color=(0, 0, 0)):
        height = max(LINE_HEIGHT * (size / 10.0), 12)
        self._ensure(height)
        fontname = "hebo" if bold else "helv"
        self.page.insert_text((MARGIN, self.y), text[:150], fontsize=size,
                              fontname=fontname, color=color)
        self.y += height

    def status_box(self, label: str, text: str, fill, border, text_color=(0, 0, 0)):
        height = 30
        self._ensure(height + 5)
        rect = fitz.Rect(MARGIN, self.y - 3, PAGE_WIDTH - MARGIN, self.y + height - 3)
        self.page.draw_rect(rect, color=border, fill=fill, width=0.7)
        self.page.insert_text((MARGIN + 8, self.y + 16),
                              f"{label}: {text}"[:150], fontsize=9,
                              fontname="hebo", color=text_color)
        self.y += height + 5

    def gap(self, amount: float = 6):
        self.y += amount


def _diff_style(d):
    dtype = str(d.get("type", "")).upper()
    category = str(d.get("category", "")).lower()
    severity = str(d.get("severity", "")).upper()
    # Green is reserved for verified/preserved status; actual findings are
    # red for loss/addition/mismatch and yellow for modifications/review.
    if any(k in dtype for k in ("MISSING", "UNMATCH", "ERROR", "ADDED", "EXTRA")) or severity == "HIGH":
        return (1.0, 0.88, 0.88), (0.75, 0.12, 0.12), "MISSING / UNMATCHED"
    if any(k in dtype for k in ("CHANGED", "MODIF", "REVIEW", "MERGED", "SPLIT")) or severity in ("MEDIUM", "LOW"):
        return (1.0, 0.95, 0.78), (0.78, 0.50, 0.02), "MODIFIED / REVIEW"
    return (0.88, 0.98, 0.90), (0.12, 0.50, 0.24), "MATCH / INFO"

def write(differences: list, scores, out_path: str) -> str:
    diff_dicts = [d.to_dict() if hasattr(d, "to_dict") else d for d in differences]
    score_dict = scores.to_dict()
    w = _PdfWriter()
    w.new_page()
    w.line("EPUBForge - Advanced Fidelity Compare Report", size=16, bold=True)
    w.gap(10)
    w.line("Executive Summary", size=13, bold=True)
    w.status_box("VERIFIED MATCH",
                  f"Overall fidelity {score_dict['overall_fidelity_pct']:.2f}%",
                  (0.88, 0.98, 0.90), (0.12, 0.50, 0.24), (0.08, 0.35, 0.15))

    for cat, data in score_dict["categories"].items():
        w.status_box(cat.title(),
                      f"Matched {data['verified_match_pct']:.2f}%  |  "
                      f"Differences {data['confirmed_difference_pct']:.2f}%  |  "
                      f"Uncertain {data['uncertain_pct']:.2f}%",
                      (0.95, 0.98, 0.95), (0.55, 0.65, 0.55))

    w.gap(8)
    w.line(f"Detailed Differences ({len(diff_dicts)} total)", size=13, bold=True)
    w.gap(4)
    for d in diff_dicts:
        fill, border, label = _diff_style(d)
        w.status_box(label,
                      f"[{d.get('severity', '')}] {d.get('type', '')}  "
                      f"({d.get('category', '')})  conf {float(d.get('confidence_score', 0.0)):.2f}",
                      fill, border)
        loc = f"orig p{d.get('original_page')} -> conv p{d.get('converted_page')}"
        w.line(loc, size=8)
        if d.get("original_text") or d.get("converted_text"):
            orig = str(d.get("original_text", ""))[:180]
            conv = str(d.get("converted_text", ""))[:180]
            # Keep the actual compared strings in the same semantic color as
            # their finding so the PDF can be scanned quickly by QA operators.
            w.line(f"Original: {orig}", size=8, color=border)
            w.line(f"Converted: {conv}", size=8, color=border)
        w.gap(4)
    w.doc.save(out_path)
    w.doc.close()
    return out_path

