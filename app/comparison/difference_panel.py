"""EPUBForge Difference Panel.

Replacement for the comparison DifferencePanel.

Features:
- Keeps pack/grid geometry managers separated.
- Red rows = content/text/symbol/missing/extra/wrong matches.
- Yellow rows = formatting/style/layout changes.
- Green rows = explicit MATCH/UNCHANGED results, if supplied by the engine.
- Shows PDF/XHTML page numbers and old/new text.
- Shows Unicode/special-character information in the detail callback.
- Search covers text, type, category, pages, Unicode and explanation.
- Previous/Next navigation remains compatible with the host window.
"""

import tkinter as tk
from tkinter import ttk

from gui import theme
from core.fidelity_compare.score_calculator import (
    is_production_error,
    SUMMARY_TYPE_BUCKETS,
)


FILTERS = [
    "Production Errors Only",
    "Need Review",
    "Informational / Layout",
    "All",
    "Character Errors",
    "Word Errors",
    "Paragraph Errors",
    "Missing",
    "Added",
    "Changed",
    "Merged",
    "Split",
    "Missing Space",
    "Merged Word",
    "Unicode",
    "Homoglyph",
    "Special Character",
    "Hyphenation",
    "Alignment",
    "Position",
    "Indentation",
    "Margin",
    "Font",
    "Line Spacing",
    "Paragraph Spacing",
    "Columns",
    "Figures",
    "Tables",
    "Captions",
    "Footnotes",
    "References",
    "Index",
    "Links",
    "OCR Uncertain",
]


_BUCKET_FILTER_NAMES = {
    "character_errors": "Character Errors",
    "word_errors": "Word Errors",
    "paragraph_errors": "Paragraph Errors",
}


def _bucket_predicate(types):
    return lambda d: d.type in types


_FILTER_MATCH = {
    "Production Errors Only": is_production_error,
    "Need Review": lambda d: (
        d.confidence == "LOW" or getattr(d, "uncertain", False)
    ),
    "Informational / Layout": lambda d: (
        not is_production_error(d) and d.confidence != "LOW"
    ),
    "All": lambda d: True,

    "Missing": lambda d: d.type.startswith("MISSING"),
    "Added": lambda d: (
        d.type.startswith("ADDED")
        or d.type.startswith("EXTRA")
    ),
    "Changed": lambda d: (
        d.type.startswith("CHANGED")
        or d.type.endswith("_CHANGED")
        or d.type in ("ALIGNMENT_CHANGED", "TEXT_CHANGED")
    ),
    "Merged": lambda d: "MERGED" in d.type,
    "Split": lambda d: "SPLIT" in d.type,
    "Missing Space": lambda d: d.type == "MISSING_SPACE",
    "Merged Word": lambda d: d.type == "MERGED_WORD",

    "Unicode": lambda d: (
        d.category == "unicode"
        or "UNICODE" in d.type
    ),
    "Homoglyph": lambda d: d.type == "HOMOGLYPH_SUBSTITUTION",
    "Special Character": lambda d: (
        d.type in (
            "SPECIAL_CHARACTER_CHANGED",
            "SYMBOL_MISMATCH",
            "SPECIAL_CHARACTER_MISMATCH",
        )
        or "SYMBOL" in d.type
        or "SPECIAL_CHARACTER" in d.type
    ),
    "Hyphenation": lambda d: (
        d.category == "hyphenation"
        or "HYPHEN" in d.type
    ),
    "Alignment": lambda d: d.type == "ALIGNMENT_CHANGED",
    "Position": lambda d: (
        "MOVED" in d.type or "POSITION" in d.type
    ),
    "Indentation": lambda d: "INDENTATION" in d.type,
    "Margin": lambda d: d.type == "MARGIN_CHANGED",
    "Font": lambda d: (
        "FONT" in d.type
        or d.type in (
            "BOLD_CHANGED",
            "ITALIC_CHANGED",
            "UNDERLINE_CHANGED",
            "SUPERSCRIPT_CHANGED",
            "SUBSCRIPT_CHANGED",
            "BOLD_MISMATCH",
            "ITALIC_MISMATCH",
            "UNDERLINE_MISMATCH",
            "SUPERSCRIPT_MISMATCH",
            "SUBSCRIPT_MISMATCH",
        )
    ),
    "Line Spacing": lambda d: d.type == "LINE_SPACING_CHANGED",
    "Paragraph Spacing": lambda d: d.type == "PARAGRAPH_SPACING_CHANGED",
    "Columns": lambda d: "COLUMN" in d.type,
    "Figures": lambda d: d.category == "figure",
    "Tables": lambda d: d.category == "table",
    "Captions": lambda d: "CAPTION" in d.type,
    "Footnotes": lambda d: d.category == "footnote",
    "References": lambda d: d.category == "reference",
    "Index": lambda d: d.category == "index",
    "Links": lambda d: d.category == "link",
    "OCR Uncertain": lambda d: (
        d.category == "ocr" or d.confidence == "LOW"
    ),
}


for _bucket_key, _filter_name in _BUCKET_FILTER_NAMES.items():
    _FILTER_MATCH[_filter_name] = _bucket_predicate(
        SUMMARY_TYPE_BUCKETS[_bucket_key]
    )


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------

_RED_TYPES = {
    "MISSING_WORD",
    "MISSING_CHARACTER",
    "MISSING_TEXT",
    "MISSING_SPACE",
    "EXTRA_WORD",
    "EXTRA_CHARACTER",
    "EXTRA_TEXT",
    "EXTRA",
    "EXTRA_SUPERSCRIPT",
    "EXTRA_SUBSCRIPT",
    "TEXT_CHANGED",
    "WORD_CHANGED",
    "CHARACTER_CHANGED",
    "UNICODE_MISMATCH",
    "HOMOGLYPH_SUBSTITUTION",
    "SPECIAL_CHARACTER_CHANGED",
    "SPECIAL_CHARACTER_MISMATCH",
    "SYMBOL_MISMATCH",
    "HYPHENATION_MISMATCH",
    "MISSING",
    "ADDED",
}

_YELLOW_TYPES = {
    "ITALIC_MISMATCH",
    "BOLD_MISMATCH",
    "UNDERLINE_MISMATCH",
    "SUPERSCRIPT_MISMATCH",
    "SUBSCRIPT_MISMATCH",
    "ITALIC_CHANGED",
    "BOLD_CHANGED",
    "UNDERLINE_CHANGED",
    "SUPERSCRIPT_CHANGED",
    "SUBSCRIPT_CHANGED",
    "FONT_CHANGED",
    "ALIGNMENT_CHANGED",
    "POSITION_CHANGED",
    "MARGIN_CHANGED",
    "INDENTATION_CHANGED",
    "LINE_SPACING_CHANGED",
    "PARAGRAPH_SPACING_CHANGED",
    "COLUMN_CHANGED",
}


def _type_upper(d):
    return str(getattr(d, "type", "") or "").upper()


def _row_kind(d):
    """Return match/error/style for Treeview row colouring."""

    typ = _type_upper(d)

    # Explicit successful result.
    if typ in {
        "MATCH",
        "MATCHED",
        "UNCHANGED",
        "CONTENT_MATCH",
        "STYLE_MATCH",
    }:
        return "match"

    # Formatting differences are yellow.
    if typ in _YELLOW_TYPES:
        return "modified"

    if (
        typ.endswith("_MISMATCH")
        and any(
            key in typ
            for key in (
                "ITALIC",
                "BOLD",
                "UNDERLINE",
                "SUPERSCRIPT",
                "SUBSCRIPT",
                "FONT",
            )
        )
    ):
        return "modified"

    # Anything explicitly involving missing/extra/wrong text or symbols
    # is a real red error.
    if typ in _RED_TYPES:
        return "error"

    if (
        typ.startswith("MISSING")
        or typ.startswith("EXTRA")
        or typ.startswith("ADDED")
        or "SYMBOL" in typ
        or "UNICODE" in typ
        or "HOMOGLYPH" in typ
        or "CHARACTER" in typ
        or typ in {"TEXT_CHANGED", "WORD_CHANGED"}
    ):
        return "error"

    # Generic *_CHANGED is a modification/review item.
    if typ.endswith("_CHANGED"):
        return "modified"

    # Unknown differences are safer as red than green.
    return "error"


class DifferencePanel(ttk.Frame):
    def __init__(self, parent, on_select=None):
        super().__init__(parent)
        self._on_select = on_select
        self._differences = []
        self._visible_row_ids = []
        self._current_index = -1

        palette = theme.current.palette

        # Header uses pack().
        top = tk.Frame(self, bg=palette["app_bg"])
        top.pack(fill=tk.X, pady=(0, 4))

        tk.Label(
            top,
            text="Filter:",
            bg=palette["app_bg"],
            fg=palette["text"],
            font=theme.FONT_BODY,
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.filter_var = tk.StringVar(
            value="Production Errors Only"
        )

        combo = ttk.Combobox(
            top,
            textvariable=self.filter_var,
            values=FILTERS,
            state="readonly",
            width=22,
        )
        combo.pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        self.count_label = tk.Label(
            top,
            text="",
            bg=palette["app_bg"],
            fg=palette["text_muted"],
            font=theme.FONT_SMALL,
        )
        self.count_label.pack(side=tk.RIGHT)

        # Navigation uses pack().
        nav = tk.Frame(self, bg=palette["app_bg"])
        nav.pack(fill=tk.X, pady=(0, 4))

        tk.Button(
            nav,
            text="< Previous Difference",
            command=self.select_previous,
        ).pack(side=tk.LEFT)

        tk.Button(
            nav,
            text="Next Difference >",
            command=self.select_next,
        ).pack(side=tk.LEFT, padx=(6, 0))

        self.position_label = tk.Label(
            nav,
            text="No differences",
            bg=palette["app_bg"],
            fg=palette["text_muted"],
            font=theme.FONT_SMALL,
        )
        self.position_label.pack(side=tk.LEFT, padx=12)

        # IMPORTANT:
        # DifferencePanel itself uses pack().
        # Treeview and scrollbars use grid() ONLY inside this child frame.
        table = tk.Frame(self, bg=palette["surface"])
        table.pack(fill=tk.BOTH, expand=True)

        # Include old/new text so the user can immediately see what differs.
        columns = (
            "severity",
            "type",
            "category",
            "page",
            "old",
            "new",
            "confidence",
        )

        self.tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            selectmode="browse",
        )

        headings = {
            "severity": "Severity",
            "type": "Type",
            "category": "Category",
            "page": "PDF → XHTML Page",
            "old": "PDF / Original",
            "new": "XHTML / Generated",
            "confidence": "Confidence",
        }

        widths = {
            "severity": 80,
            "type": 190,
            "category": 100,
            "page": 125,
            "old": 260,
            "new": 260,
            "confidence": 100,
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(
                col,
                width=widths[col],
                minwidth=50,
                anchor="w",
                stretch=(col in {"old", "new"}),
            )

        # Row colours.
        self.tree.tag_configure(
            "error",
            background="#ffd6d6",
            foreground="#b00000",
        )
        self.tree.tag_configure(
            "modified",
            background="#fff1b8",
            foreground="#8a5a00",
        )
        self.tree.tag_configure(
            "match",
            background="#d6f5d6",
            foreground="#087a2f",
        )

        vsb = ttk.Scrollbar(
            table,
            orient="vertical",
            command=self.tree.yview,
        )
        hsb = ttk.Scrollbar(
            table,
            orient="horizontal",
            command=self.tree.xview,
        )

        self.tree.configure(
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)

        self.tree.bind(
            "<<TreeviewSelect>>",
            self._on_tree_select,
        )

        self.tree.bind(
            "<F7>",
            lambda e: (self.select_next(), "break")[1],
        )
        self.tree.bind(
            "<Shift-F7>",
            lambda e: (self.select_previous(), "break")[1],
        )

        self._row_to_diff = {}
        self._search_query = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_differences(self, differences: list):
        self._differences = list(differences or [])
        self._refresh()

    def search(self, query: str) -> int:
        self._search_query = (query or "").strip().lower()
        self._refresh()

        if self._visible_row_ids:
            self.select_next()

        return len(self._visible_row_ids)

    def apply_filter_and_select_first(
        self,
        filter_name: str,
    ) -> bool:
        if filter_name not in FILTERS:
            return False

        self._search_query = ""
        self.filter_var.set(filter_name)
        self._refresh()

        if not self._visible_row_ids:
            return False

        self.select_next()
        return True

    def select_next(self):
        if not self._visible_row_ids:
            return

        self._select_at(
            (self._current_index + 1)
            % len(self._visible_row_ids)
        )

    def select_previous(self):
        if not self._visible_row_ids:
            return

        self._select_at(
            (self._current_index - 1)
            % len(self._visible_row_ids)
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _matches_search(self, d) -> bool:
        if not self._search_query:
            return True

        haystack = [
            str(getattr(d, "type", "")),
            str(getattr(d, "category", "")),
            str(getattr(d, "original_text", "")),
            str(getattr(d, "converted_text", "")),
            str(getattr(d, "original_page", "") or ""),
            str(getattr(d, "converted_page", "") or ""),
            str(getattr(d, "explanation", "")),
        ]

        for attr in ("original_unicode", "converted_unicode"):
            u = getattr(d, attr, None)
            if u:
                haystack.extend(
                    [
                        str(u.get("codepoint", "")),
                        str(u.get("name", "")),
                        str(u.get("char", "")),
                    ]
                )

        q = self._search_query
        return any(q in h.lower() for h in haystack)

    # ------------------------------------------------------------------
    # Tree rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _text(value):
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _page_value(d):
        op = getattr(d, "original_page", None)
        cp = getattr(d, "converted_page", None)

        if op is None and cp is None:
            return "-"

        return f"{op or '-'} → {cp or '-'}"

    def _refresh(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        self._row_to_diff = {}
        self._visible_row_ids = []
        self._current_index = -1

        filt = self.filter_var.get()
        predicate = _FILTER_MATCH.get(filt)

        shown = 0

        for d in self._differences:
            if predicate and not predicate(d):
                continue

            if not self._matches_search(d):
                continue

            old_text = self._text(
                getattr(d, "original_text", "")
            )
            new_text = self._text(
                getattr(d, "converted_text", "")
            )

            row_id = self.tree.insert(
                "",
                tk.END,
                values=(
                    self._text(getattr(d, "severity", "")),
                    self._text(getattr(d, "type", "")),
                    self._text(getattr(d, "category", "")),
                    self._page_value(d),
                    old_text,
                    new_text,
                    self._text(getattr(d, "confidence", "")),
                ),
                tags=(_row_kind(d),),
            )

            self._row_to_diff[row_id] = d
            self._visible_row_ids.append(row_id)
            shown += 1

        self.count_label.configure(
            text=f"{shown} / {len(self._differences)} shown"
        )

        self._update_position_label()

    def _update_position_label(self):
        total = len(self._visible_row_ids)

        if total == 0:
            self.position_label.configure(
                text="No differences"
            )
        elif self._current_index < 0:
            self.position_label.configure(
                text=f"0 of {total}"
            )
        else:
            self.position_label.configure(
                text=(
                    f"Difference "
                    f"{self._current_index + 1} "
                    f"of {total}"
                )
            )

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select_at(self, index: int):
        if not self._visible_row_ids:
            return

        row_id = self._visible_row_ids[index]

        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.see(row_id)

        self._current_index = index
        self._update_position_label()

        diff = self._row_to_diff.get(row_id)

        if diff and self._on_select:
            self._on_select(diff)

    def _on_tree_select(self, _event):
        sel = self.tree.selection()

        if not sel or not self._on_select:
            return

        row_id = sel[0]
        diff = self._row_to_diff.get(row_id)

        if row_id in self._visible_row_ids:
            self._current_index = (
                self._visible_row_ids.index(row_id)
            )
            self._update_position_label()

        if diff:
            self._on_select(diff)
