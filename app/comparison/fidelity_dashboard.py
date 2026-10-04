"""Score summary widget (spec section 60) - a strip of score cards
showing the OVERALL fidelity and each measured per-category percentage,
built entirely from the real core.fidelity_compare.score_calculator
output for the comparison that just ran - never a hardcoded "99%
accurate" placeholder."""
import tkinter as tk
from tkinter import ttk

from gui import theme

_LABELS = {
    "content": "Content", "structure": "Structure", "unicode": "Unicode", "layout": "Layout",
    "figure": "Figures", "table": "Tables", "hyphenation": "Hyphenation", "link": "Links", "ocr": "OCR",
}

# Card label -> the exact app.comparison.difference_panel filter name that
# card must apply on click (spec: "INTERACTIVE ERROR CARDS + EXACT
# DIFFERENCE VIEW" section 1/20/32 - "EVERY ERROR SUMMARY CARD MUST BE
# CLICKABLE... Apply corresponding filter"). A card with no entry here
# (OVERALL FIDELITY, the per-category percentage cards) has no natural
# 1:1 filter and stays a plain, non-interactive count - only the cards the
# spec explicitly enumerates by name become clickable.
_CARD_TO_FILTER = {
    "PRODUCTION STATUS": None,  # a status label, not a filterable count
    "REAL PRODUCTION ERRORS": "Production Errors Only",
    "NEED REVIEW": "Need Review",
    "INFORMATIONAL / LAYOUT": "Informational / Layout",
    "CHARACTER ERRORS": "Character Errors",
    "UNICODE ERRORS": "Unicode",
    "WORD ERRORS": "Word Errors",
    "MISSING CONTENT": "Missing",
    "EXTRA CONTENT": "Added",
    "PARAGRAPH ERRORS": "Paragraph Errors",
    "FIGURE ERRORS": "Figures",
    "TABLE ERRORS": "Tables",
}


class FidelityDashboard(ttk.Frame):
    def __init__(self, parent, on_card_click=None):
        super().__init__(parent)
        palette = theme.current.palette
        self._palette = palette
        self._on_card_click = on_card_click
        self.configure(style="TFrame")
        self._cards_frame = tk.Frame(self, bg=palette["app_bg"])
        self._cards_frame.pack(fill=tk.X)
        self.show_empty()

    def _clear(self):
        for w in self._cards_frame.winfo_children():
            w.destroy()

    def show_empty(self):
        self._clear()
        p = self._palette
        box = tk.Frame(self._cards_frame, bg=p["surface"],
                       highlightbackground=p["border"], highlightthickness=1)
        box.pack(fill=tk.X)
        tk.Label(
            box, text="READY", bg=p["surface"], fg=p["text_muted"],
            font=theme.FONT_SMALL_BOLD
        ).pack(side=tk.LEFT, padx=(12, 8), pady=8)
        tk.Label(
            box, text="Run a comparison to see the fidelity summary.",
            bg=p["surface"], fg=p["text"], font=theme.FONT_SMALL
        ).pack(side=tk.LEFT, pady=8)

    _SUMMARY_LABELS = {
        "character_errors": "Character errors",
        "unicode_errors": "Unicode errors",
        "word_errors": "Word errors",
        "missing_content": "Missing",
        "extra_content": "Extra",
        "paragraph_errors": "Paragraph errors",
        "figure_errors": "Figures",
        "table_errors": "Tables",
    }

    def show_scores(self, score_dict: dict, production_status: str = None, summary: dict = None):
        self._clear()
        p = self._palette

        box = tk.Frame(self._cards_frame, bg=p["surface"],
                       highlightbackground=p["border"], highlightthickness=1)
        box.pack(fill=tk.X)

        status = production_status or "READY"
        status_color = {
            "PASS": p["success"],
            "PASS WITH WARNINGS": p["warning"],
            "FAIL": p["error"],
        }.get(status, p["text"])

        self._metric(box, "STATUS", status, status_color)

        overall = float(score_dict.get("overall_fidelity_pct", 0))
        overall_color = p["success"] if overall >= 95 else p["warning"] if overall >= 80 else p["error"]
        self._metric(box, "FIDELITY", f"{overall:.2f}%", overall_color)

        if summary:
            errors = int(summary.get("real_errors", 0))
            review = int(summary.get("need_review", 0))
            info = int(summary.get("informational", 0))
            self._metric(box, "ERRORS", str(errors), p["error"] if errors else p["success"],
                         "Production Errors Only")
            self._metric(box, "REVIEW", str(review), p["warning"], "Need Review")
            self._metric(box, "INFO", str(info), p["text_muted"], "Informational / Layout")

        cat = tk.Frame(box, bg=p["surface"])
        cat.pack(side=tk.LEFT, padx=(10, 12), pady=5)
        tk.Label(
            cat, text="CATEGORY", bg=p["surface"], fg=p["text_muted"],
            font=theme.FONT_SMALL_BOLD
        ).pack(anchor="w")

        for name, data in list(score_dict.get("categories", {}).items())[:7]:
            pct = float(data.get("verified_match_pct", 0))
            color = p["success"] if pct >= 95 else p["warning"] if pct >= 80 else p["error"]
            row = tk.Frame(cat, bg=p["surface"])
            row.pack(anchor="w")
            tk.Label(
                row, text=_LABELS.get(name, name.title()),
                width=11, anchor="w", bg=p["surface"], fg=p["text_muted"],
                font=theme.FONT_SMALL
            ).pack(side=tk.LEFT)
            tk.Label(
                row, text=f"{pct:.1f}%", bg=p["surface"], fg=color,
                font=theme.FONT_SMALL_BOLD
            ).pack(side=tk.LEFT)

    def _metric(self, parent, label, value, color, filter_name=None):
        p = self._palette
        frame = tk.Frame(parent, bg=p["surface"],
                         highlightbackground=p["border"], highlightthickness=1,
                         cursor="hand2" if filter_name else "")
        frame.pack(side=tk.LEFT, padx=4, pady=5, ipadx=7, ipady=2)

        tk.Label(
            frame, text=value, bg=p["surface"], fg=color,
            font=(theme.FONT_FAMILY, 14, "bold")
        ).pack(anchor="w")
        tk.Label(
            frame, text=label, bg=p["surface"], fg=p["text_muted"],
            font=theme.FONT_SMALL
        ).pack(anchor="w")

        if filter_name and self._on_card_click:
            cb = lambda e, fn=filter_name: self._on_card_click(fn)
            frame.bind("<Button-1>", cb)
            for child in frame.winfo_children():
                child.bind("<Button-1>", cb)
            theme.Tooltip(frame, f"Click to filter: {filter_name}")

    def _card(self, parent, label, value, color, sub=None):
        palette = self._palette
        filter_name = _CARD_TO_FILTER.get(label)
        clickable = bool(filter_name) and self._on_card_click is not None
        card = tk.Frame(parent, bg=palette["surface"], highlightbackground=palette["border"],
                         highlightthickness=1, cursor="hand2" if clickable else "")
        card.pack(side=tk.LEFT, padx=4, pady=4, ipadx=8, ipady=4)
        value_lbl = tk.Label(card, text=value, bg=palette["surface"], fg=color,
                              font=(theme.FONT_FAMILY, 16, "bold"))
        value_lbl.pack(anchor="w")
        label_lbl = tk.Label(card, text=label, bg=palette["surface"], fg=palette["text_muted"],
                              font=theme.FONT_SMALL)
        label_lbl.pack(anchor="w")
        widgets = [card, value_lbl, label_lbl]
        if sub:
            sub_lbl = tk.Label(card, text=sub, bg=palette["surface"], fg=palette["text_faint"],
                                font=theme.FONT_SMALL)
            sub_lbl.pack(anchor="w")
            widgets.append(sub_lbl)
        if clickable:
            # Spec section 1: "EVERY ERROR SUMMARY CARD MUST BE CLICKABLE" -
            # bound on the card frame AND every label inside it, since a
            # click on any of the stacked Labels would otherwise not reach
            # the surrounding Frame's own binding in Tk.
            for w in widgets:
                w.bind("<Button-1>", lambda e, fn=filter_name: self._on_card_click(fn))
            theme.Tooltip(card, f"Click to filter: {filter_name}")
