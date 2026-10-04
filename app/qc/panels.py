"""Side panels of the PDF <-> XHTML QC workspace.

    DifferencePanel  - filters, list, details, ACCEPT / REJECT / EDIT / IGNORE /
                       REOPEN / APPLY TO ALL SIMILAR, image detail with
                       ACCEPT / REMAP / CHANGE / IGNORE
    SplitTree        - splits with page range, status, warnings, images,
                       markers, validation; drag & drop reorder and every split
                       operation (all through the package manager)
    PagesPanel       - per page printed number, marker state, scores
    ScoresPanel      - document / current page / current split scores

Every panel reads the session's unified mapping (ws.session.mapping) and
reports user actions back to the workspace (ws.*)."""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

from core.qc.mapping import FILTERS, STATE_LABELS, MATCH, MISSING, MODIFIED, UNCERTAIN
from core.qc.package_manager import PackageError

STATUSES = ("open", "accepted", "edited", "rejected", "ignored")


def _short(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


# =========================================================== differences
class DifferencePanel(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        self.items = []
        self._photos = []
        # filters
        filt = ttk.LabelFrame(self, text="Filters")
        filt.pack(fill=tk.X, padx=2, pady=2)
        self.filter_vars = {}
        for n, (name, _kinds) in enumerate(FILTERS):
            v = tk.BooleanVar(value=True)
            self.filter_vars[name] = v
            ttk.Checkbutton(filt, text=name, variable=v, command=self.refresh).grid(
                row=n // 2, column=n % 2, sticky="w")
        row = tk.Frame(filt)
        row.grid(row=len(FILTERS) // 2 + 1, column=0, columnspan=2, sticky="we")
        ttk.Button(row, text="All", width=5, command=lambda: self._set_all(True)).pack(side=tk.LEFT)
        ttk.Button(row, text="None", width=5, command=lambda: self._set_all(False)).pack(side=tk.LEFT)
        tk.Label(row, text=" Status:").pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="open")
        ttk.Combobox(row, textvariable=self.status_var, width=9, state="readonly",
                     values=("open", "all") + STATUSES[1:]).pack(side=tk.LEFT)
        self.status_var.trace_add("write", lambda *_: self.refresh())
        tk.Label(row, text=" Split:").pack(side=tk.LEFT)
        self.split_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="current only", variable=self.split_only, command=self.refresh).pack(side=tk.LEFT)
        # list
        cols = ("n", "type", "state", "page", "split", "conf", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12, selectmode="browse")
        for c, w, t in (("n", 40, "#"), ("type", 150, "Type"), ("state", 70, "State"), ("page", 45, "Page"),
                        ("split", 110, "Split"), ("conf", 50, "Conf."), ("status", 65, "Status")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c in ("type", "split"))
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        lst = tk.Frame(self)
        lst.pack(fill=tk.BOTH, expand=True, padx=2)
        ys.pack(in_=lst, side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(in_=lst, side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # actions
        act = tk.Frame(self)
        act.pack(fill=tk.X, padx=2, pady=2)
        self.buttons = {}
        for text, cmd in (("ACCEPT", self.accept), ("REJECT", self.reject), ("EDIT", self.edit),
                          ("IGNORE", self.ignore), ("REOPEN", self.reopen),
                          ("APPLY TO ALL SIMILAR", self.apply_all_similar)):
            b = ttk.Button(act, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=1)
            self.buttons[text] = b
        self.img_bar = tk.Frame(self)
        for text, cmd in (("REMAP", self.remap), ("CHANGE", self.change)):
            ttk.Button(self.img_bar, text=text, command=cmd).pack(side=tk.LEFT, padx=1)
        self.img_frame = tk.Frame(self)
        self.pdf_img_lbl = tk.Label(self.img_frame, text="PDF", compound="top", font=("Segoe UI", 8))
        self.pdf_img_lbl.pack(side=tk.LEFT, padx=4)
        self.x_img_lbl = tk.Label(self.img_frame, text="XHTML", compound="top", font=("Segoe UI", 8))
        self.x_img_lbl.pack(side=tk.LEFT, padx=4)
        # details
        self.details = tk.Text(self, height=11, wrap="word", font=("Segoe UI", 9))
        self.details.pack(fill=tk.BOTH, padx=2, pady=2)
        self.details.tag_configure("b", font=("Segoe UI", 9, "bold"))
        self.details.tag_configure("del", background="#FBD5D5", overstrike=True)
        self.details.tag_configure("ins", background="#D7F2DA", underline=True)

    def _set_all(self, value):
        for v in self.filter_vars.values():
            v.set(value)
        self.refresh()

    # -------------------------------------------------------------- data
    def current(self):
        sel = self.tree.selection()
        if not sel:
            return None
        k = int(sel[0])
        return self.items[k] if 0 <= k < len(self.items) else None

    def refresh(self, keep_id=None):
        s = self.ws.session
        prev = keep_id or (self.current().id if self.current() else None)
        self.tree.delete(*self.tree.get_children())
        self.items = []
        if s is None or s.mapping is None:
            self.ws.update_counter()
            return
        filters = [n for n, v in self.filter_vars.items() if v.get()]
        st = self.status_var.get()
        statuses = None if st == "all" else ([st] if st != "open" else ["open"])
        diffs = s.mapping.differences_for(filters=filters, statuses=statuses)
        if self.split_only.get() and self.ws.current_split:
            diffs = [d for d in diffs if d.split == self.ws.current_split]
        colors = self.ws.colors
        for state, col in colors.items():
            self.tree.tag_configure(state, foreground=col)
        sel = None
        for k, d in enumerate(diffs):
            self.items.append(d)
            self.tree.insert("", "end", iid=str(k), tags=(d.state,), values=(
                k + 1, d.label, STATE_LABELS[d.state], d.pdf_page or "",
                os.path.basename(d.split or ""), f"{d.confidence:.0%}", d.status))
            if d.id == prev:
                sel = str(k)
        if sel is not None:
            self.tree.selection_set(sel)
            self.tree.see(sel)
        self.ws.update_counter()

    def index(self):
        d = self.current()
        return self.items.index(d) if d in self.items else -1

    def select(self, k):
        if not self.items:
            return
        k = max(0, min(k, len(self.items) - 1))
        self.tree.selection_set(str(k))
        self.tree.see(str(k))

    def select_diff(self, diff):
        for k, d in enumerate(self.items):
            if d is diff or d.id == diff.id:
                self.select(k)
                return True
        return False

    # ------------------------------------------------------------ details
    def _on_select(self, _event=None):
        d = self.current()
        self.ws.update_counter()
        if d is None:
            return
        self._show_details(d)
        self.ws.navigate_to_difference(d)

    def _show_details(self, d):
        t = self.details
        t.configure(state="normal")
        t.delete("1.0", "end")

        def line(label, value):
            if value not in (None, ""):
                t.insert("end", f"{label}: ", "b")
                t.insert("end", f"{value}\n")
        line("Type", f"{d.label}  [{STATE_LABELS[d.state]}]")
        line("Severity", d.severity)
        line("Confidence", f"{d.confidence:.0%}")
        line("PDF page", d.pdf_page)
        line("Split", d.split)
        line("Source line", d.sourceline or None)
        line("Status", d.status)
        line("PDF text", _short(d.pdf_text, 400))
        line("XHTML text", _short(d.xhtml_text, 400))
        if d.segments:
            t.insert("end", "Character diff: ", "b")
            for op, a, b in d.segments[:200]:
                if op == "equal":
                    t.insert("end", a)
                else:
                    if a:
                        t.insert("end", a, "del")
                    if b:
                        t.insert("end", b, "ins")
            t.insert("end", "\n")
        line("Details", d.message)
        if d.correction:
            line("Suggested action", f"{d.correction.description} ({d.correction.confidence:.0%}"
                                     f"{', auto-safe' if d.correction.auto_safe else ''})")
        else:
            line("Suggested action", "none - review manually (EDIT / IGNORE)")
        if d.image:
            for k in ("pdf_uid", "pdf_size", "pdf_aspect", "xhtml_src", "xhtml_size", "xhtml_aspect", "similarity",
                      "pdf_caption", "xhtml_caption", "suggested_file"):
                if d.image.get(k) not in (None, ""):
                    line(k.replace("_", " "), d.image[k])
        t.configure(state="disabled")
        self._show_images(d)
        locked = bool(d.split and self.ws.session.mgr.is_locked(d.split))
        for name in ("ACCEPT", "EDIT", "APPLY TO ALL SIMILAR"):
            self.buttons[name].configure(state="disabled" if locked else "normal")

    def _show_images(self, d):
        self._photos = []
        if not d.image or d.category not in ("image", "caption"):
            self.img_frame.pack_forget()
            self.img_bar.pack_forget()
            return
        s = self.ws.session
        self.img_bar.pack(fill=tk.X, padx=2, before=self.details)
        self.img_frame.pack(fill=tk.X, padx=2, before=self.details)
        pimg = None
        uid = d.image.get("pdf_uid")
        if uid and s.pdf is not None:
            for im in s.pdf.page(d.image.get("pdf_page") or d.pdf_page or 1).images:
                if im.uid == uid:
                    try:
                        pimg = s.pdf.image_pil(im)
                    except Exception:
                        pimg = None
        ximg = None
        f = d.image.get("xhtml_file")
        if f and s.mgr.exists(f):
            try:
                ximg = Image.open(s.mgr.abspath(f)).convert("RGB")
            except Exception:
                ximg = None
        for lbl, img, title in ((self.pdf_img_lbl, pimg, "PDF image"), (self.x_img_lbl, ximg, "XHTML image")):
            if img is None:
                lbl.configure(image="", text=f"{title}\n(none)")
                continue
            img = ImageOps.contain(img.convert("RGB"), (150, 120))
            img = ImageOps.expand(img, border=3, fill=self.ws.colors[d.state])
            ph = ImageTk.PhotoImage(img)
            self._photos.append(ph)
            lbl.configure(image=ph, text=f"{title}  {STATE_LABELS[d.state]}")

    # ------------------------------------------------------------ actions
    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except (PackageError, ValueError, KeyError, IndexError) as e:
            messagebox.showerror("QC", str(e), parent=self)
        return None

    def accept(self):
        d = self.current()
        if d is None:
            return
        if self._guard(self.ws.session.accept, d) is not None or d.status == "accepted":
            self.ws.after_change(f"Accepted: {d.label}", keep_id=self._next_id(d))

    def reject(self):
        d = self.current()
        if d:
            self.ws.session.reject(d)
            self.ws.after_change(f"Rejected: {d.label}", keep_id=self._next_id(d))

    def ignore(self):
        d = self.current()
        if d:
            self.ws.session.ignore(d)
            self.ws.after_change(f"Ignored: {d.label}", keep_id=self._next_id(d))

    def reopen(self):
        d = self.current()
        if d:
            self.ws.session.reopen(d)
            self.ws.after_change(f"Reopened: {d.label}", keep_id=d.id)

    def edit(self):
        d = self.current()
        if d is None:
            return
        initial = d.pdf_text if d.pdf_text else d.xhtml_text
        new = EditDialog.ask(self, d, initial)
        if new is None:
            return
        if self._guard(self.ws.session.edit, d, new) is not None:
            self.ws.after_change(f"Edited: {d.label}", keep_id=self._next_id(d))

    def apply_all_similar(self):
        d = self.current()
        if d is None:
            return
        sims = self.ws.session.similar(d)
        if not sims:
            messagebox.showinfo("Apply to all similar", "No open differences with the same correction.", parent=self)
            return
        if not messagebox.askyesno("Apply to all similar",
                                   f"Apply '{d.correction.description}' type corrections to {len(sims)} open "
                                   f"{d.label} difference(s)?\n(One undo step.)", parent=self):
            return
        self._guard(self.ws.session.apply_all_similar, d)
        self.ws.after_change(f"Applied {len(sims)} similar correction(s)")

    def remap(self):
        d = self.current()
        if d is None or not d.image:
            return
        choices = self.ws.session.package_images()
        if not choices:
            messagebox.showinfo("Remap", "The package has no images.", parent=self)
            return
        pick = ImagePicker.ask(self, self.ws.session, choices, d.image.get("suggested_file"))
        if pick and self._guard(self.ws.session.remap_image, d, pick) is not None:
            self.ws.after_change(f"Remapped image to {pick}")

    def change(self):
        d = self.current()
        if d is None or not d.image:
            return
        path = filedialog.askopenfilename(parent=self, title="Replacement image", filetypes=[
            ("Images", "*.png *.jpg *.jpeg *.gif *.svg *.webp"), ("All files", "*.*")])
        if path and self._guard(self.ws.session.change_image, d, path) is not None:
            self.ws.after_change(f"Changed image to {os.path.basename(path)}")

    def _next_id(self, d):
        k = self.items.index(d) if d in self.items else -1
        return self.items[k + 1].id if 0 <= k < len(self.items) - 1 else None


class EditDialog(tk.Toplevel):
    def __init__(self, parent, diff, initial):
        super().__init__(parent)
        self.title(f"EDIT - {diff.label}")
        self.result = None
        tk.Label(self, text="PDF text:", anchor="w").pack(fill=tk.X, padx=6)
        a = tk.Text(self, height=4, wrap="word")
        a.insert("1.0", diff.pdf_text)
        a.configure(state="disabled")
        a.pack(fill=tk.X, padx=6)
        tk.Label(self, text="XHTML text:", anchor="w").pack(fill=tk.X, padx=6)
        b = tk.Text(self, height=4, wrap="word")
        b.insert("1.0", diff.xhtml_text)
        b.configure(state="disabled")
        b.pack(fill=tk.X, padx=6)
        tk.Label(self, text="Replace the XHTML range with:", anchor="w").pack(fill=tk.X, padx=6)
        self.text = tk.Text(self, height=5, wrap="word")
        self.text.insert("1.0", initial)
        self.text.pack(fill=tk.BOTH, expand=True, padx=6)
        row = tk.Frame(self)
        row.pack(fill=tk.X, pady=4)
        ttk.Button(row, text="Apply", command=self._ok).pack(side=tk.RIGHT, padx=6)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self.transient(parent)

    def _ok(self):
        self.result = self.text.get("1.0", "end-1c")
        self.destroy()

    @classmethod
    def ask(cls, parent, diff, initial):
        dlg = cls(parent, diff, initial)
        dlg.grab_set()
        parent.wait_window(dlg)
        return dlg.result


class ImagePicker(tk.Toplevel):
    def __init__(self, parent, session, choices, suggested):
        super().__init__(parent)
        self.title("REMAP image")
        self.result = None
        self.session = session
        self._photo = None
        self.lb = tk.Listbox(self, width=50, height=14)
        for c in choices:
            self.lb.insert("end", c + ("   (suggested)" if c == suggested else ""))
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.preview = tk.Label(self, text="preview")
        self.preview.pack(side=tk.LEFT, padx=6)
        self.choices = choices
        self.lb.bind("<<ListboxSelect>>", self._preview)
        self.lb.bind("<Double-1>", lambda e: self._ok())
        ttk.Button(self, text="Use image", command=self._ok).pack(side=tk.BOTTOM, pady=6)
        if suggested in choices:
            self.lb.selection_set(choices.index(suggested))
            self._preview()
        self.transient(parent)

    def _preview(self, _e=None):
        sel = self.lb.curselection()
        if not sel:
            return
        try:
            img = Image.open(self.session.mgr.abspath(self.choices[sel[0]])).convert("RGB")
            self._photo = ImageTk.PhotoImage(ImageOps.contain(img, (200, 200)))
            self.preview.configure(image=self._photo, text="")
        except Exception:
            self.preview.configure(image="", text="(no preview)")

    def _ok(self):
        sel = self.lb.curselection()
        if sel:
            self.result = self.choices[sel[0]]
        self.destroy()

    @classmethod
    def ask(cls, parent, session, choices, suggested):
        dlg = cls(parent, session, choices, suggested)
        dlg.grab_set()
        parent.wait_window(dlg)
        return dlg.result


# ============================================================ split tree
class SplitTree(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        cols = ("pages", "status", "warn", "imgs", "marks", "valid")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=10, selectmode="browse")
        self.tree.heading("#0", text="Split")
        self.tree.column("#0", width=150, stretch=True)
        for c, w, t in (("pages", 60, "PDF pp."), ("status", 70, "Status"), ("warn", 45, "Warn"),
                        ("imgs", 40, "Imgs"), ("marks", 45, "Marks"), ("valid", 55, "Match")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=False, anchor="center")
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        top = tk.Frame(self)
        top.pack(fill=tk.BOTH, expand=True)
        ys.pack(in_=top, side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(in_=top, side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.tree.bind("<B1-Motion>", self._drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self._drag = None
        bar = tk.Frame(self)
        bar.pack(fill=tk.X)
        bar2 = tk.Frame(self)
        bar2.pack(fill=tk.X)
        for parent_bar, items in (
                (bar, (("Add", self.add), ("Delete", self.delete), ("Merge Prev", self.merge_prev),
                       ("Merge Next", self.merge_next), ("Rename", self.rename))),
                (bar2, (("Split Here", self.split_here), ("Split at PDF Page", self.split_at_page),
                        ("Move ↑", lambda: self.move(-1)), ("Move ↓", lambda: self.move(+1)),
                        ("Lock", self.toggle_lock), ("Preview", self.preview)))):
            for text, cmd in items:
                ttk.Button(parent_bar, text=text, command=cmd).pack(side=tk.LEFT, padx=1, pady=1)
        self.info = tk.Label(self, text="", anchor="w", justify="left", font=("Segoe UI", 8), wraplength=420)
        self.info.pack(fill=tk.X, padx=2)

    def refresh(self):
        s = self.ws.session
        sel = self.selected()
        self.tree.delete(*self.tree.get_children())
        if s is None:
            return
        m = s.mapping
        markers_by = {}
        images_by = {}
        for mk in s.xhtml.markers:
            markers_by[mk.split] = markers_by.get(mk.split, 0) + 1
        for im in s.xhtml.images:
            images_by[im.split] = images_by.get(im.split, 0) + 1
        for info in s.mgr.splits():
            sm = m.split_maps.get(info.path) if m else None
            pages = f"{sm.pages[0]}-{sm.pages[-1]}" if sm and sm.pages else "-"
            warn = sm.scores.get("Warnings", 0) if sm else 0
            status = "locked" if info.locked else ("nav" if info.is_nav else ("ok" if not warn else "review"))
            valid = f"{sm.scores.get('Match', 0)}%" if sm else "-"
            tag = "locked" if info.locked else ("warn" if warn else "ok")
            self.tree.insert("", "end", iid=info.path, text=("🔒 " if info.locked else "") + info.name,
                             values=(pages, status, warn, images_by.get(info.path, 0),
                                     markers_by.get(info.path, 0), valid), tags=(tag,))
        self.tree.tag_configure("warn", foreground="#B45309")
        self.tree.tag_configure("locked", foreground="#6B7280")
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
            self.tree.see(sel)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def select(self, rel):
        if rel and self.tree.exists(rel) and self.selected() != rel:
            self.tree.selection_set(rel)
            self.tree.see(rel)

    def _on_select(self, _e=None):
        rel = self.selected()
        if rel and self._drag is None:
            s = self.ws.session
            info = next((i for i in s.mgr.splits() if i.path == rel), None)
            title = info.title if info else ""
            self.info.configure(text=f"{rel}  -  {title}")
            self.ws.show_split(rel)

    # ------------------------------------------------------- drag & drop
    def _drag_start(self, e):
        self._drag = None
        row = self.tree.identify_row(e.y)
        if row:
            self._drag = {"row": row, "y": e.y, "moved": False}

    def _drag_motion(self, e):
        if not self._drag:
            return
        if abs(e.y - self._drag["y"]) > 6:
            self._drag["moved"] = True
            self.tree.configure(cursor="sb_v_double_arrow")

    def _drag_end(self, e):
        d, self._drag = self._drag, None
        self.tree.configure(cursor="")
        if not d or not d["moved"]:
            return
        target = self.tree.identify_row(e.y)
        if not target or target == d["row"]:
            return
        order = list(self.ws.session.mgr.split_paths())
        if d["row"] not in order or target not in order:
            return
        order.remove(d["row"])
        order.insert(order.index(target) + (1 if e.y > self.tree.bbox(target)[1] + 8 else 0), d["row"])
        self._op(f"Reorder: {os.path.basename(d['row'])}", self.ws.session.reorder_splits, order)

    # ------------------------------------------------------------ actions
    def _need(self):
        rel = self.selected() or self.ws.current_split
        if not rel:
            messagebox.showinfo("Splits", "Select a split first.", parent=self)
        return rel

    def _op(self, label, fn, *args):
        try:
            res = fn(*args)
        except (PackageError, ValueError, IndexError, KeyError) as e:
            messagebox.showerror("Split operation", str(e), parent=self)
            return None
        extra = ""
        if res is not None and getattr(res, "needs_review", None):
            extra = "\n".join(f"- {r}" for r in res.needs_review[:15])
            messagebox.showwarning("Needs review", f"{label} completed.\n\nNeeds review:\n{extra}", parent=self)
        self.ws.after_change(label)
        return res

    def add(self):
        rel = self._need()
        if not rel:
            return
        title = simpledialog.askstring("Add split", "Title of the new split:", initialvalue="New Section",
                                       parent=self)
        if title:
            self._op(f"Add split after {os.path.basename(rel)}", self.ws.session.add_split, rel, title)

    def delete(self):
        rel = self._need()
        if not rel:
            return
        imp = self.ws.session.impact(rel)
        if not messagebox.askyesno("Delete split",
                                   "Delete EXACTLY this one split?\n\n" + imp.summary() +
                                   "\n\nOPF manifest/spine, NAV/TOC and NCX entries are removed; links pointing "
                                   "into it are unwrapped and listed for review. (Undo is available.)",
                                   icon="warning", parent=self):
            return
        self._op(f"Delete {os.path.basename(rel)}", self.ws.session.delete_split, rel)

    def merge_prev(self):
        rel = self._need()
        if rel:
            self._op(f"Merge {os.path.basename(rel)} with previous", self.ws.session.merge_with_previous, rel)

    def merge_next(self):
        rel = self._need()
        if rel:
            self._op(f"Merge {os.path.basename(rel)} with next", self.ws.session.merge_with_next, rel)

    def rename(self):
        rel = self._need()
        if not rel:
            return
        new = simpledialog.askstring("Rename split", "New file name:", initialvalue=os.path.basename(rel),
                                     parent=self)
        if new and new != os.path.basename(rel):
            self._op(f"Rename {os.path.basename(rel)}", self.ws.session.rename_split, rel, new)

    def split_here(self):
        j = self.ws.current_word()
        if j is None:
            messagebox.showinfo("Split here", "Click a word in the rendered XHTML view first.", parent=self)
            return
        self._op("Split at the selected XHTML position", self.ws.session.split_at_word, j)

    def split_at_page(self):
        s = self.ws.session
        if s.pdf is None or s.mapping is None:
            return
        n = simpledialog.askinteger("Split at PDF page", f"PDF page (1-{len(s.pdf.pages)}):",
                                    initialvalue=self.ws.pdf_view.page, minvalue=1, maxvalue=len(s.pdf.pages),
                                    parent=self)
        if n:
            self._op(f"Split at PDF page {n}", s.split_at_pdf_page, n)

    def move(self, direction):
        rel = self._need()
        if not rel:
            return
        order = list(self.ws.session.mgr.split_paths())
        k = order.index(rel)
        if not 0 <= k + direction < len(order):
            return
        order[k], order[k + direction] = order[k + direction], order[k]
        self._op(f"Move {os.path.basename(rel)}", self.ws.session.reorder_splits, order)

    def toggle_lock(self):
        rel = self._need()
        if not rel:
            return
        s = self.ws.session
        s.lock(rel, not s.mgr.is_locked(rel))
        self.ws.after_change(f"{'Locked' if s.mgr.is_locked(rel) else 'Unlocked'} {os.path.basename(rel)}")

    def preview(self):
        rel = self._need()
        if not rel:
            return
        s = self.ws.session
        imp = s.impact(rel)
        sm = s.mapping.split_maps.get(rel) if s.mapping else None
        lines = [imp.summary()]
        if sm:
            lines.append(f"PDF pages: {sm.pages[0]}-{sm.pages[-1]}" if sm.pages else "PDF pages: not mapped")
            lines.append("Scores: " + ", ".join(f"{k} {v}" for k, v in sm.scores.items()))
        lines.append(f"Outbound links: {imp.outbound_links}   Inbound: {len(imp.inbound_links)}")
        messagebox.showinfo(f"Preview - {os.path.basename(rel)}", "\n".join(lines), parent=self)


# ================================================================= pages
class PagesPanel(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        cols = ("page", "printed", "marker", "match", "text", "images", "diffs")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=10, selectmode="browse")
        for c, w, t in (("page", 45, "Page"), ("printed", 60, "Printed"), ("marker", 80, "Marker"),
                        ("match", 55, "Match"), ("text", 50, "Text"), ("images", 55, "Images"),
                        ("diffs", 45, "Diffs")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center", stretch=False)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.thumb = tk.Label(self)
        self.thumb.pack(side=tk.TOP, pady=4)
        self._photo = None
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self._sync = False

    def refresh(self):
        s = self.ws.session
        self.tree.delete(*self.tree.get_children())
        if s is None or s.mapping is None:
            return
        colors = self.ws.colors
        for st, col in colors.items():
            self.tree.tag_configure(st, foreground=col)
        for pm in s.mapping.pages:
            ms = pm.marker_state or ("-" if not pm.printed else UNCERTAIN)
            label = STATE_LABELS.get(ms, ms) if ms != "-" else "-"
            match = pm.scores.get("Match", 0)
            tag = MATCH if match >= 98 else (MODIFIED if match >= 85 else MISSING)
            self.tree.insert("", "end", iid=str(pm.page), tags=(tag,), values=(
                pm.page, pm.printed or "", label, f"{match}%", f"{pm.scores.get('Text', 0)}%",
                f"{pm.scores.get('Images', 0)}%", pm.scores.get("Differences", 0)))

    def select_page(self, page):
        iid = str(page)
        if self.tree.exists(iid) and self.tree.selection() != (iid,):
            self._sync = True
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.after(50, lambda: setattr(self, "_sync", False))
        self._thumb(page)

    def _thumb(self, page):
        s = self.ws.session
        if s is None or s.pdf is None:
            return
        try:
            img = s.pdf.render(page, 0.25)
            self._photo = ImageTk.PhotoImage(img)
            self.thumb.configure(image=self._photo)
        except Exception:
            self.thumb.configure(image="")

    def _on_select(self, _e=None):
        if self._sync:
            return
        sel = self.tree.selection()
        if sel:
            self.ws.goto_pdf_page(int(sel[0]))


# ================================================================ scores
class ScoresPanel(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        self.doc = tk.Frame(self)
        self.doc.pack(fill=tk.X, padx=4, pady=4)
        self.page_lbl = tk.Label(self, anchor="w", justify="left", font=("Segoe UI", 9))
        self.page_lbl.pack(fill=tk.X, padx=4, pady=4)
        self.split_lbl = tk.Label(self, anchor="w", justify="left", font=("Segoe UI", 9))
        self.split_lbl.pack(fill=tk.X, padx=4, pady=4)
        self.summary = tk.Label(self, anchor="w", justify="left", font=("Segoe UI", 9))
        self.summary.pack(fill=tk.X, padx=4, pady=4)

    def refresh(self):
        for w in self.doc.winfo_children():
            w.destroy()
        s = self.ws.session
        if s is None or s.mapping is None:
            return
        m = s.mapping
        tk.Label(self.doc, text="DOCUMENT", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        for k, (name, v) in enumerate(m.scores.items(), 1):
            col = "#2E7D32" if v >= 98 else ("#B88A00" if v >= 85 else "#C62828")
            tk.Label(self.doc, text=name, anchor="w").grid(row=k, column=0, sticky="w")
            tk.Label(self.doc, text=f"{v}%", fg=col, font=("Segoe UI", 9, "bold")).grid(row=k, column=1, sticky="e")
            bar = tk.Canvas(self.doc, width=140, height=10, highlightthickness=0, bg="#E5E7EB")
            bar.grid(row=k, column=2, padx=6)
            bar.create_rectangle(0, 0, 1.4 * v, 10, fill=col, width=0)
        counts = {}
        for d in m.differences:
            counts[d.status] = counts.get(d.status, 0) + 1
        self.summary.configure(text="Differences: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                               if counts else "No differences")
        self.update_current()

    def update_current(self):
        s = self.ws.session
        if s is None or s.mapping is None:
            return
        page = self.ws.pdf_view.page
        if 1 <= page <= len(s.mapping.pages):
            pm = s.mapping.pages[page - 1]
            self.page_lbl.configure(text=f"PAGE {page} (printed {pm.printed or '-'}, mapping "
                                         f"{pm.confidence:.0%})\n" +
                                         "  ".join(f"{k}: {v}" for k, v in pm.scores.items()))
        rel = self.ws.current_split
        sm = s.mapping.split_maps.get(rel) if rel else None
        if sm:
            self.split_lbl.configure(text=f"SPLIT {os.path.basename(rel)}\n" +
                                          "  ".join(f"{k}: {v}" for k, v in sm.scores.items()))
        else:
            self.split_lbl.configure(text="")


def legend(parent, colors):
    """Colour legend - every colour is accompanied by its text label."""
    f = tk.Frame(parent)
    seen = set()
    for st in (MATCH, MISSING, MODIFIED, "MOVED", "DUPLICATE", UNCERTAIN):
        if st in seen:
            continue
        seen.add(st)
        tk.Label(f, text="  ", bg=colors[st]).pack(side=tk.LEFT, padx=(6, 2))
        text = {MISSING: "Missing / Extra"}.get(st, STATE_LABELS[st])
        tk.Label(f, text=text, font=("Segoe UI", 8)).pack(side=tk.LEFT)
    return f
