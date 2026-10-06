"""XML Validation (launcher card 02): validate an existing BITS 2.2 / JATS
1.4 file against its DTD and repair what can be repaired safely
(core.bits.pipeline.fix_file -> core.bits.autofix). The input file is never
modified - the repaired copy is written next to it as <name>.fixed.xml."""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.bits import dtd, pipeline


def open_window(launcher_root, on_home):
    win = tk.Toplevel(launcher_root)
    win.title("BITS Tool - XML Validation (BITS / JATS)")
    win.geometry("1100x700")
    state = {"path": None}

    top = tk.Frame(win, padx=8, pady=8)
    top.pack(fill=tk.X)
    path_var = tk.StringVar()
    kind_var = tk.StringVar(value="Auto")
    tk.Label(top, text="XML file:").pack(side=tk.LEFT)
    tk.Entry(top, textvariable=path_var, width=70).pack(side=tk.LEFT, padx=4)

    def browse():
        p = filedialog.askopenfilename(parent=win, filetypes=[("XML files", "*.xml"), ("All files", "*.*")])
        if p:
            path_var.set(p)
            state["path"] = p

    tk.Button(top, text="Browse...", command=browse).pack(side=tk.LEFT)
    tk.Label(top, text="  Type:").pack(side=tk.LEFT)
    ttk.Combobox(top, textvariable=kind_var, values=["Auto", "BITS", "JATS"], width=7,
                 state="readonly").pack(side=tk.LEFT)
    dtd_var = tk.StringVar()
    row2 = tk.Frame(win, padx=8)
    row2.pack(fill=tk.X)
    tk.Label(row2, text="DTD file (optional - blank = bundled / profiles/<type>/dtd/):").pack(side=tk.LEFT)
    tk.Entry(row2, textvariable=dtd_var, width=60).pack(side=tk.LEFT, padx=4)

    def browse_dtd():
        p = filedialog.askopenfilename(parent=win, filetypes=[("DTD files", "*.dtd"), ("All files", "*.*")])
        if p:
            dtd_var.set(p)

    tk.Button(row2, text="Browse...", command=browse_dtd).pack(side=tk.LEFT)
    status = tk.Label(win, text="Choose a BITS (book) or JATS (article) XML file.", anchor="w", padx=8)
    status.pack(fill=tk.X)
    info = tk.Label(win, text=("BITS DTD: " + ("installed" if dtd.available("BITS") else dtd.problem("BITS"))
                               + "\nJATS DTD: " + ("installed" if dtd.available("JATS") else dtd.problem("JATS"))),
                    anchor="w", justify="left", padx=8, fg="#555", wraplength=1050)
    info.pack(fill=tk.X)

    cols = ("#", "Message")
    frame = tk.Frame(win)
    frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
    tree = ttk.Treeview(frame, columns=cols, show="headings")
    tree.heading("#", text="#")
    tree.column("#", width=50, stretch=False)
    tree.heading("Message", text="Message")
    tree.column("Message", width=1000)
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    ys.pack(side=tk.RIGHT, fill=tk.Y)
    tree.pack(fill=tk.BOTH, expand=True)

    def kind():
        return None if kind_var.get() == "Auto" else kind_var.get()

    def settings():
        d = dtd_var.get().strip()
        return {"bits_dtd_path": d, "jats_dtd_path": d} if d else None

    def show(rows):
        tree.delete(*tree.get_children())
        for i, r in enumerate(rows, 1):
            tree.insert("", "end", values=(i, r))

    def validate():
        p = path_var.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showwarning("Validate", "Choose an XML file first.", parent=win)
            return
        try:
            k, errors = pipeline.validate_file(p, kind(), settings())
        except dtd.DTDNotFound as e:
            messagebox.showerror("Validate", str(e), parent=win)
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Validate", f"Could not validate:\n{e}", parent=win)
            return
        show(errors or ["VALID - no DTD errors"])
        status.config(text=f"{k}: {'VALID' if not errors else str(len(errors)) + ' DTD error(s)'}")

    def autofix():
        p = path_var.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showwarning("Auto-fix", "Choose an XML file first.", parent=win)
            return
        try:
            out, rep = pipeline.fix_file(p, kind=kind(), settings=settings())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Auto-fix", f"Could not auto-fix:\n{e}", parent=win)
            return
        rows = [f"[FIX {rule}] {where}: {detail}" for rule, where, detail in rep.fixes]
        rows += [f"[REMAINING] {e}" for e in rep.errors_after]
        show(rows or ["Nothing to fix"])
        status.config(text=f"{rep.summary()} - written to {out} (the input file was not changed)")

    btns = tk.Frame(top)
    btns.pack(side=tk.RIGHT)
    tk.Button(btns, text="Validate", command=validate).pack(side=tk.LEFT, padx=2)
    tk.Button(btns, text="Validate & Auto-Fix", command=autofix).pack(side=tk.LEFT, padx=2)

    def close():
        win.destroy()
        if on_home:
            on_home()

    tk.Button(win, text="Home", command=close).pack(side=tk.BOTTOM, pady=6)
    win.protocol("WM_DELETE_WINDOW", close)
    win.validation_actions = {"validate": validate, "autofix": autofix, "path_var": path_var, "dtd_var": dtd_var,
                              "tree": tree}
    return win
