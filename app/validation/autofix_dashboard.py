"""VALIDATE & AUTO-FIX result window: the validation dashboard (overall
PASS / NEEDS REVIEW / FAIL + one tile per check), remaining issues (root
cause, attempted repair, why it was not fixed, recommended manual action),
repair history (before / after of every change, COMMITTED / ROLLED BACK),
the repair plan, data integrity, and the classic summary text. Buttons open
the HTML report, the session folder, the repaired EPUB in the editor, and
the PDF <-> XHTML compare workspace when a PDF was given."""
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from tkinter import ttk

STATUS_COLORS = {"PASS": "#1b7f3b", "REVIEW": "#b7791f", "NEEDS REVIEW": "#b7791f", "FAIL": "#c53030",
                 "SKIP": "#718096", "COMMITTED": "#1b7f3b", "ROLLED BACK": "#c53030"}
SYMBOL = {"PASS": "✓", "REVIEW": "!", "FAIL": "✗", "SKIP": "–"}


def _open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - opening the user's own report / folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file://" + os.path.abspath(path))


def _tree(parent, columns, widths, height=12):
    frame = tk.Frame(parent)
    tree = ttk.Treeview(frame, columns=columns, show="headings", height=height)
    for c, w in zip(columns, widths):
        tree.heading(c, text=c)
        tree.column(c, width=w, stretch=c in ("Message", "Why not fixed", "Changes", "Detail", "Reason"))
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    ys.pack(side=tk.RIGHT, fill=tk.Y)
    xs.pack(side=tk.BOTTOM, fill=tk.X)
    tree.pack(fill=tk.BOTH, expand=True)
    return frame, tree


def show(parent, report, summary_text="", on_edit=None, on_compare=None, pdf_path=None):
    top = tk.Toplevel(parent)
    top.title("EPUB Validation & Auto-Fix")
    top.geometry("1180x780")
    overall = report.overall or "NEEDS REVIEW"
    head = tk.Frame(top, bg=STATUS_COLORS.get(overall, "#4a5568"))
    head.pack(fill=tk.X)
    tk.Label(head, text=f"EPUB VALIDATION   Overall: {overall}", fg="white", bg=head["bg"],
             font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=12, pady=8)
    tk.Label(head, text=report.final_status, fg="white", bg=head["bg"], font=("Segoe UI", 9),
             wraplength=640, justify="left").pack(side=tk.LEFT, padx=8)

    btns = tk.Frame(top)
    btns.pack(fill=tk.X, padx=8, pady=4)
    html_report = report.reports.get("Report (HTML)")
    if html_report:
        ttk.Button(btns, text="Open full report", command=lambda: _open_path(html_report)).pack(side=tk.LEFT)
    if report.session_dir:
        ttk.Button(btns, text="Open session folder (backup / working / history)",
                   command=lambda: _open_path(report.session_dir)).pack(side=tk.LEFT, padx=4)
    if report.output_path and os.path.exists(report.output_path):
        ttk.Button(btns, text="Show repaired EPUB",
                   command=lambda: _open_path(os.path.dirname(report.output_path))).pack(side=tk.LEFT, padx=4)
        if on_edit:
            ttk.Button(btns, text="Edit repaired EPUB", command=lambda: on_edit(report.output_path)).pack(
                side=tk.LEFT, padx=4)
        if on_compare and pdf_path:
            ttk.Button(btns, text="PDF ↔ XHTML compare",
                       command=lambda: on_compare(pdf_path, report.output_path)).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Close", command=top.destroy).pack(side=tk.RIGHT)

    # tiles
    tiles = tk.Frame(top)
    tiles.pack(fill=tk.X, padx=8, pady=4)
    for k, (name, v) in enumerate(report.dashboard.items()):
        st = v["status"]
        cell = tk.Frame(tiles, bd=1, relief="solid", padx=6, pady=4)
        cell.grid(row=k // 5, column=k % 5, sticky="nsew", padx=3, pady=3)
        tiles.grid_columnconfigure(k % 5, weight=1)
        tk.Label(cell, text=f"{SYMBOL.get(st, '?')} {name}", fg=STATUS_COLORS.get(st, "#333"),
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(cell, text=v["detail"], font=("Segoe UI", 8), wraplength=200, justify="left",
                 anchor="w").pack(fill=tk.X)

    nb = ttk.Notebook(top)
    nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

    # remaining issues
    f, t = _tree(nb, ("Code", "File", "Line", "Message", "Root cause", "Level", "Attempted repair",
                      "Why not fixed", "Recommended action"), (70, 140, 40, 260, 130, 40, 150, 260, 240))
    for r in report.remaining_issues:
        t.insert("", "end", values=(r["code"], r["file"], r["line"], r["message"], r["root_cause"], r["level"],
                                    r["attempted"], "; ".join(r["why_not_fixed"]), r["recommended"]))
    nb.add(f, text=f"Remaining issues ({len(report.remaining_issues)})")

    # history with details
    hist = tk.Frame(nb)
    pw = ttk.PanedWindow(hist, orient="vertical")
    pw.pack(fill=tk.BOTH, expand=True)
    f2, t2 = _tree(pw, ("#", "Pass", "Level", "Repair", "Codes", "Files", "Confidence", "EPUBCheck", "Status",
                        "Reason"), (35, 45, 45, 160, 120, 200, 70, 110, 100, 300), height=10)
    detail = tk.Text(pw, height=10, wrap="word", font=("Consolas", 9))
    pw.add(f2, weight=3)
    pw.add(detail, weight=2)
    t2.tag_configure("COMMITTED", foreground=STATUS_COLORS["COMMITTED"])
    t2.tag_configure("ROLLED BACK", foreground=STATUS_COLORS["ROLLED BACK"])
    for h in report.history:
        t2.insert("", "end", iid=str(h["n"]), tags=(h["status"],), values=(
            h["n"], h["pass"], h["level"], h["strategy"], ", ".join(h["codes"]), ", ".join(h["files"])[:120],
            f"{h['confidence']:.0%}", h["epubcheck"], h["status"], h["reason"]))

    def on_sel(_e=None):
        sel = t2.selection()
        if not sel:
            return
        h = next(x for x in report.history if str(x["n"]) == sel[0])
        detail.delete("1.0", "end")
        detail.insert("end", f"REPAIR #{h['n']}  {h['strategy']}  ({h['status']})\n"
                             f"Time: {h['timestamp']}   Level: {h['level']}   Codes: {', '.join(h['codes'])}\n"
                             f"Integrity: {h['integrity']}\nEPUBCheck: {h['epubcheck'] or '-'}\n"
                             f"{('Reason: ' + h['reason']) if h['reason'] else ''}\n\n")
        for c in h["changes"]:
            detail.insert("end", f"{c['file']}  {c.get('location', '')}\n   {c['reason']}  "
                                 f"(confidence {c.get('confidence', 1):.0%})\n")
            if c.get("before") or c.get("after"):
                detail.insert("end", f"   BEFORE: {c['before']}\n   AFTER:  {c['after']}\n")
    t2.bind("<<TreeviewSelect>>", on_sel)
    nb.add(hist, text=f"Repair history ({len(report.history)})")

    # plan
    plan = tk.Text(nb, wrap="word", font=("Consolas", 9))
    for i, g in enumerate(report.initial_plan, 1):
        plan.insert("end", f"{i}. {g['category'].replace('_', ' ').title()}  [{', '.join(g['codes'])}]  "
                           f"x{g['errors']}\n   Files: {', '.join(g['files'])}\n"
                           + (f"   ROOT CAUSE: {g['cascade_of']} (symptom)\n" if g["cascade_of"] else "")
                           + f"   Action: {g['action']}\n   Level: {g['level']} - {g['level_name']}  "
                             f"Status: {'automatic' if g['level'] <= 2 else 'manual'}\n\n")
    plan.configure(state="disabled")
    nb.add(plan, text=f"Repair plan ({len(report.initial_plan)})")

    # integrity
    integ = tk.Frame(nb)
    tk.Label(integ, text=report.content_integrity, anchor="w", font=("Segoe UI", 10, "bold")).pack(fill=tk.X, padx=6)
    f3, t3 = _tree(integ, ("Measure", "Original", "Final", "Difference"), (200, 100, 100, 100), height=12)
    for k, b in report.totals_before.items():
        a = report.totals_after.get(k)
        t3.insert("", "end", values=(k, b, a, "" if a == b else f"{(a or 0) - (b or 0):+d}"))
    f3.pack(fill=tk.BOTH, expand=True)
    f4, t4 = _tree(integ, ("Severity", "Category", "File", "Detail"), (80, 120, 220, 600), height=8)
    for x in report.integrity_findings:
        t4.insert("", "end", values=(x["severity"], x["category"], x["file"], x["detail"]))
    f4.pack(fill=tk.BOTH, expand=True)
    nb.add(integ, text="Data integrity")

    # client rules (e.g. CUPEPUB)
    if getattr(report, "client_profile", "") and report.client_after:
        cl = tk.Frame(nb)
        cb, ca = report.client_before.get("counts", {}), report.client_after.get("counts", {})
        info = (f"{report.client_profile}: before {cb.get('Error', 0)} error(s) / {cb.get('Warning', 0)} warning(s)"
                f"  ->  after {ca.get('Error', 0)} / {ca.get('Warning', 0)}")
        if report.delivery_path:
            info += f"    Delivery copy: {report.delivery_path}"
        tk.Label(cl, text=info, anchor="w", font=("Segoe UI", 10, "bold")).pack(fill=tk.X, padx=6)
        tool = (report.client_tool_log or {}).get("by_code", {})
        before_codes, after_codes = report.client_before.get("by_code", {}), report.client_after.get("by_code", {})
        f5, t5 = _tree(cl, ("Rule", "Client tool log", "Before", "After"), (90, 110, 80, 80), height=8)
        for code in sorted(set(before_codes) | set(after_codes) | set(tool)):
            t5.insert("", "end", values=(code, tool.get(code, "") if tool else "-", before_codes.get(code, 0),
                                         after_codes.get(code, 0)))
        f5.pack(fill=tk.BOTH, expand=True)
        f6, t6 = _tree(cl, ("Rule", "Severity", "File", "Line", "Message", "Why not fixed", "Recommended action"),
                       (80, 70, 150, 50, 330, 260, 300), height=10)
        for f in report.client_findings:
            t6.insert("", "end", values=(f["code"], f["severity"], f["file"], f"{f['line']}:{f['col']}",
                                         f["message"], "; ".join(f.get("why_not_fixed") or []), f["recommended"]))
        f6.pack(fill=tk.BOTH, expand=True)
        nb.add(cl, text=f"Client rules ({ca.get('Error', 0) + ca.get('Warning', 0)})")

    if summary_text:
        txt = tk.Text(nb, wrap="word", font=("Consolas", 9))
        txt.insert("1.0", summary_text)
        txt.configure(state="disabled")
        nb.add(txt, text="Summary")
    if report.remaining_issues:
        nb.select(0)
    elif report.history:
        nb.select(1)
    top.report = report
    return top


def run_in_background(parent, epub_path, pdf_path=None, on_status=None, on_done=None, launcher_root=None,
                      on_home=None, client_profile="default"):
    """Runs VALIDATE & AUTO-FIX on a worker thread (EPUBCheck runs many
    times), reports progress through on_status(text) on the Tk thread and
    then shows the dashboard. Used after EPUB generation (EPUB Structure,
    QC workspace) so a freshly built EPUB is only reported as successful
    once it has been validated. client_profile: the client rules to apply
    as well ("default" = core.epub.client_rules.DEFAULT_PROFILE, None = off)."""
    import queue
    import threading
    from core.epub import client_rules, repair_engine
    if client_profile == "default":
        client_profile = client_rules.DEFAULT_PROFILE
    q = queue.Queue()

    def work():
        try:
            q.put(("done", repair_engine.run_full_auto_repair(
                epub_path, progress=lambda t: q.put(("progress", t)), pdf_path=pdf_path,
                client_profile=client_profile)))
        except Exception as e:  # noqa: BLE001
            q.put(("error", e))
    threading.Thread(target=work, daemon=True).start()

    def _edit(path):
        from app.editor import editor_window
        editor_window.open_window(launcher_root or parent, on_home or (lambda: None), initial_epub_path=path)

    def _compare(pdf, epub):
        from app.qc import qc_window
        qc_window.open_window(launcher_root or parent, None, pdf_path=pdf, epub_path=epub, master=parent)

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "progress":
                    if on_status:
                        on_status(f"Auto-fix: {val}")
                    continue
                if kind == "error":
                    from tkinter import messagebox
                    messagebox.showerror("Validate & Auto-Fix", f"Could not complete: {val}", parent=parent)
                    if on_status:
                        on_status(f"Auto-fix failed: {val}")
                    if on_done:
                        on_done(None)
                    return
                if on_status:
                    on_status(f"Validate & Auto-Fix: {val.overall} - {val.final_status}")
                show(parent, val, on_edit=_edit, on_compare=_compare, pdf_path=pdf_path)
                if on_done:
                    on_done(val)
                return
        except queue.Empty:
            pass
        parent.after(150, poll)
    poll()
