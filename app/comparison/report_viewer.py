"""Report export controls (spec section 58) - generates the HTML/JSON/
CSV/PDF report from the last comparison result into the user's writable
output directory (core.resource_path.writable_path("output"), the same
writable-root convention every other export in this project already
uses) and opens it with the OS default handler."""
import os
import tkinter as tk
from tkinter import messagebox, filedialog
from tkinter import ttk

from core.fidelity_compare import report_csv, report_html, report_json, report_pdf
from core.resource_path import writable_path
from gui import theme


class ReportViewer(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        palette = theme.current.palette
        self._result = None
        self._original_pdf_path = ""
        self._converted_pdf_path = ""

        tk.Label(self, text="Export Report:", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_BODY_BOLD).pack(side=tk.LEFT, padx=(0, 8))
        for label, fmt in (("HTML", "html"), ("JSON", "json"), ("CSV", "csv"), ("PDF", "pdf")):
            tk.Button(self, text=label, command=lambda f=fmt: self._export(f)).pack(side=tk.LEFT, padx=2)

    def set_result(self, result: dict, original_pdf_path: str, converted_pdf_path: str):
        self._result = result
        self._original_pdf_path = original_pdf_path
        self._converted_pdf_path = converted_pdf_path

    def _export(self, fmt: str):
        if not self._result:
            messagebox.showinfo("Advanced Fidelity Compare", "Run a comparison first.")
            return

        diffs = self._result["differences"]
        scores = self._result["scores"]

        if fmt == "pdf":
            out_path = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(),
                title="Save Fidelity Comparison Report",
                defaultextension=".pdf",
                filetypes=[("PDF report", "*.pdf")],
                initialfile="fidelity_comparison_report.pdf",
            )
            if not out_path:
                return
        else:
            out_dir = writable_path("output", "fidelity_compare_reports")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"fidelity_report.{fmt}")

        try:
            if fmt == "html":
                report_html.write(diffs, scores, out_path,
                                  self._original_pdf_path, self._converted_pdf_path)
            elif fmt == "json":
                report_json.write(diffs, scores, out_path)
            elif fmt == "csv":
                report_csv.write(diffs, out_path)
            elif fmt == "pdf":
                report_pdf.write(diffs, scores, out_path)
        except Exception as e:
            messagebox.showerror(
                "Advanced Fidelity Compare",
                f"Failed to write {fmt.upper()} report:\n{e}",
                parent=self.winfo_toplevel()
            )
            return

        try:
            os.startfile(out_path)
        except Exception:
            messagebox.showinfo(
                "Report Saved",
                f"Report saved to:\n{out_path}",
                parent=self.winfo_toplevel()
            )
