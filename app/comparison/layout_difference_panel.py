"""Dedicated Layout Differences view (spec sections 46/59) - the same
underlying Difference records as difference_panel.py, pre-filtered to
category == "layout" and shown with layout-specific columns (original/
converted value, page mapping) matching the report's own "LAYOUT
DIFFERENCES" section format."""
import tkinter as tk
from tkinter import ttk

from gui import theme


class LayoutDifferencePanel(ttk.Frame):
    def __init__(self, parent, on_select=None):
        super().__init__(parent)
        self._on_select = on_select
        palette = theme.current.palette

        tk.Label(self, text="Layout Differences", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_BODY_BOLD).pack(anchor="w", pady=(0, 4))

        columns = ("type", "text", "original", "converted", "severity", "confidence")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        widths = (170, 200, 140, 140, 70, 90)
        for col, width in zip(columns, widths):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=width, anchor="w")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._row_to_diff = {}

    def set_differences(self, differences: list):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._row_to_diff = {}
        for d in differences:
            if d.category != "layout":
                continue
            original_val = self._value_of(d.original_layout) or d.original_text
            converted_val = self._value_of(d.converted_layout) or d.converted_text
            row_id = self.tree.insert("", tk.END, values=(d.type, d.original_text or d.converted_text,
                                                            original_val, converted_val, d.severity,
                                                            d.confidence))
            self._row_to_diff[row_id] = d

    @staticmethod
    def _value_of(layout_dict):
        if not layout_dict:
            return ""
        if "alignment" in layout_dict:
            return layout_dict["alignment"]
        return ""

    def _on_tree_select(self, _event):
        sel = self.tree.selection()
        if not sel or not self._on_select:
            return
        diff = self._row_to_diff.get(sel[0])
        if diff:
            self._on_select(diff)
