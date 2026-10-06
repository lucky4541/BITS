"""OCR-specific dialogs (spec 43: settings; spec 42: model/engine status).
Kept separate from gui/dialogs.py purely by size/topic, following the same
one-dialog-class-per-Toplevel convention already used there."""
import tkinter as tk
from tkinter import ttk
import threading
import queue
import time

from core.ocr import ocr_engine
from gui import theme

_PREPROCESS_OPTIONS = [
    ("deskew", "Deskew (auto-straighten rotated scans)"),
    ("remove_borders", "Remove scanner-bed borders"),
    ("grayscale", "Convert to grayscale"),
    ("enhance_contrast", "Enhance contrast (CLAHE)"),
    ("denoise", "Denoise"),
    ("adaptive_threshold", "Adaptive threshold (binarize)"),
]


class OCRProgressDialog(tk.Toplevel):
    """Shown while OCR runs on a background thread (spec: OCR must never
    block the GUI thread) - for either a single page (page_num given,
    total_pages=None) or a whole-document batch (total_pages given, spec
    24: "Page 24/471" progress reporting).

    Single-page mode uses an indeterminate bar - a lone OCR call has no
    meaningful percentage, only "running" vs "done". Batch mode uses a
    real determinate bar via set_progress(current, total), since the page
    count is known up front.

    Cancel is a SOFT cancel (spec allows this - there is no way to abort a
    library call already in flight without killing a process): it sets
    self.cancelled so the caller stops advancing to further pages (batch)
    or discards the result (single page) once the current background call
    finishes, rather than actually interrupting PaddleOCR mid-call. This
    is disclosed in the button's own label rather than implying an
    instant stop."""

    def __init__(self, parent, page_num: int = None, total_pages: int = None):
        super().__init__(parent)
        self.title("OCR Running")
        self.resizable(False, False)
        self.cancelled = False
        self._started_at = time.monotonic()
        self._last_current = 0
        self._total_pages = total_pages

        pad = tk.Frame(self, padx=20, pady=16)
        pad.pack(fill="both", expand=True)
        initial = (f"Running OCR on page {page_num}..." if total_pages is None
                   else f"Running OCR - page 1/{total_pages}...")
        self._status = tk.Label(pad, text=initial, anchor="w")
        self._status.pack(fill="x", pady=(0, 10))
        if total_pages is None:
            self._bar = ttk.Progressbar(pad, mode="indeterminate", length=280)
            self._bar.pack(fill="x")
            self._bar.start(12)
        else:
            self._bar = ttk.Progressbar(pad, mode="determinate", length=280, maximum=total_pages)
            self._bar.pack(fill="x")
        tk.Button(pad, text="Cancel (discard result when done)", command=self._cancel).pack(pady=(14, 0))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self._update_elapsed_clock()

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {sec:02d}s"
        if m:
            return f"{m}m {sec:02d}s"
        return f"{sec}s"

    def _update_elapsed_clock(self):
        if not self.winfo_exists():
            return
        elapsed = time.monotonic() - self._started_at
        # For batch runs, keep ETA live even while a single page is inside OCR.
        if self._total_pages and self._last_current > 0:
            avg = elapsed / self._last_current
            remaining = max(0, avg * (self._total_pages - self._last_current))
            self._status.configure(text=(
                f"OCR: page {self._last_current}/{self._total_pages} | "
                f"Elapsed: {self._fmt_duration(elapsed)} | "
                f"Remaining: {self._fmt_duration(remaining)}"))
        elif self._total_pages:
            self._status.configure(text=(
                f"OCR: page 0/{self._total_pages} | "
                f"Elapsed: {self._fmt_duration(elapsed)} | Remaining: calculating..."))
        else:
            self._status.configure(text=f"OCR running | Elapsed: {self._fmt_duration(elapsed)}")
        self.after(1000, self._update_elapsed_clock)

    def set_status(self, text: str):
        self._status.configure(text=text)

    def set_progress(self, current: int, total: int, page_number: int = None):
        """Update progress plus elapsed/estimated remaining time.
        ETA is calculated from the average completed-page duration and becomes
        more accurate as additional pages finish."""
        self._last_current = int(current)
        self._total_pages = int(total)
        self._bar.configure(value=current)
        elapsed = time.monotonic() - self._started_at
        avg = elapsed / current if current else 0.0
        remaining = max(0.0, avg * (total - current)) if current else 0.0
        page_text = f" (page {page_number})" if page_number is not None else ""
        eta_text = "calculating..." if current == 0 else self._fmt_duration(remaining)
        self.set_status(
            f"OCR: page {current}/{total}{page_text} | "
            f"Elapsed: {self._fmt_duration(elapsed)} | Remaining: {eta_text}")

    def _cancel(self):
        self.cancelled = True
        self._status.configure(text="Cancelling - waiting for the current OCR call to finish...")

    def close(self):
        if str(self._bar.cget("mode")) == "indeterminate":
            self._bar.stop()
        self.destroy()


class OCRSettingsDialog(tk.Toplevel):
    """Engine / language / quality (DPI) / preprocessing toggles (spec 43),
    plus a plain Installed/Missing status line per registered engine (spec
    42) - status only, never a model downloader: PaddleOCR manages its own
    model downloads on first real use, so re-implementing that here would
    duplicate something the engine already does correctly.

    self.result is the settings dict on OK, or None on Cancel - identical
    shape to what core.ocr.ocr_service.analyze_page_with_ocr /
    run_ocr_for_page accept."""

    def __init__(self, parent, current_settings: dict):
        super().__init__(parent)
        self.title("OCR Settings")
        self.resizable(False, False)
        self.result = None

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        row = 0

        tk.Label(form, text="Engine:", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        engines = ocr_engine.available_engines() or ["PaddleOCR"]
        self._engine_var = tk.StringVar(value=current_settings.get("engine", engines[0]))
        engine_combo = ttk.Combobox(form, textvariable=self._engine_var, values=engines, state="readonly", width=20)
        engine_combo.grid(row=row, column=1, sticky="w", pady=4)
        engine_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_status())
        row += 1

        # Mode: user-controlled override of the automatic classify-then-
        # decide behavior (spec: "OCR must be automatic by default... but
        # provide manual control - Auto / Force OCR / Disable OCR") - see
        # core.ocr.ocr_service.analyze_page_with_ocr's own `mode` param.
        tk.Label(form, text="Mode:", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        self._mode_var = tk.StringVar(value=current_settings.get("mode", "auto"))
        mode_combo = ttk.Combobox(form, textvariable=self._mode_var, state="readonly", width=20,
                                    values=["auto", "force_ocr", "digital_only"])
        mode_combo.grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        tk.Label(form, text="auto = classify each page automatically\n"
                             "force_ocr = always OCR this page\n"
                             "digital_only = never OCR, digital extractor only",
                  anchor="w", justify="left", fg="#777777", font=("Segoe UI", 8)).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1

        status_row = tk.Frame(form)
        status_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self._status_label = tk.Label(status_row, text="", anchor="w", justify="left", fg="#555555")
        self._status_label.pack(side="left")
        self._test_btn = tk.Button(status_row, text="Test Engine", command=self._test_engine)
        self._test_btn.pack(side="left", padx=(10, 0))
        row += 1

        tk.Label(form, text="Language:", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        from core.lang import LANGUAGES
        self._lang_var = tk.StringVar(value=current_settings.get("language", "auto"))
        # ISO code ("auto" = from the page's own text / script); any OCR
        # engine model name ("ch", "latin", "cyrillic" ...) can be typed too
        ttk.Combobox(form, textvariable=self._lang_var, width=10,
                     values=[f"{k} — {v}" for k, v in LANGUAGES.items()]).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        tk.Label(form, text="Render DPI (quality):", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        self._dpi_var = tk.IntVar(value=current_settings.get("dpi", 300))
        ttk.Spinbox(form, from_=150, to=600, increment=50, textvariable=self._dpi_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1

        tk.Label(form, text="Preprocessing:", anchor="w", font=theme.FONT_SMALL_BOLD).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        row += 1
        prep = current_settings.get("preprocessing", {})
        self._prep_vars = {}
        for key, label in _PREPROCESS_OPTIONS:
            var = tk.BooleanVar(value=bool(prep.get(key, False)))
            self._prep_vars[key] = var
            tk.Checkbutton(form, text=label, variable=var, anchor="w").grid(
                row=row, column=0, columnspan=2, sticky="w")
            row += 1

        btns = tk.Frame(form)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self._refresh_status()
        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _refresh_status(self):
        """CHEAP check only (import, never construct/initialize - see
        OCREngine.is_available's own docstring) - safe to run every time
        this dialog opens or the engine combobox changes. Click "Test
        Engine" for a real, explicit initialization attempt (spec 16:
        "do not show misleading Ready status - test the actual import AND
        initialization")."""
        name = self._engine_var.get()
        try:
            engine = ocr_engine.get_engine(name)
            available = engine.is_available()
            version = engine.version
        except ocr_engine.OCREngineError as e:
            self._status_label.configure(text=f"Status: not registered ({e})", fg="#B71C1C")
            return
        if available:
            self._status_label.configure(
                text=f"Status: ✓ Installed (version {version}) - click Test Engine to confirm it initializes",
                fg="#1B5E20")
        else:
            self._status_label.configure(
                text=f"Status: ✗ Not Available - {name}'s Python package/runtime is not installed here.",
                fg="#B71C1C")

    def _test_engine(self):
        """Real, explicit-only initialization test (OCREngine.
        test_initialization) - run on a background thread since
        PaddleOCR's own construction can take real time (and may trigger
        a first-run model download), polled via after() so this modal
        dialog's own event loop keeps pumping instead of freezing."""
        name = self._engine_var.get()
        try:
            engine = ocr_engine.get_engine(name)
        except ocr_engine.OCREngineError as e:
            self._status_label.configure(text=f"Status: not registered ({e})", fg="#B71C1C")
            return
        self._test_btn.configure(state="disabled")
        self._status_label.configure(text=f"Status: Testing {name} - this may take a moment...", fg="#555555")
        result_queue = queue.Queue()
        threading.Thread(target=lambda: result_queue.put(engine.test_initialization()), daemon=True).start()
        self._poll_test(result_queue)

    def _poll_test(self, result_queue: queue.Queue):
        try:
            ok, message = result_queue.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_test(result_queue))
            return
        self._test_btn.configure(state="normal")
        self._status_label.configure(
            text=f"Status: {'✓ Ready' if ok else '✗ Not Available'} - {message}",
            fg="#1B5E20" if ok else "#B71C1C")

    def _ok(self):
        self.result = {
            "engine": self._engine_var.get(),
            "mode": self._mode_var.get(),
            "language": (self._lang_var.get().strip().split()[0] if self._lang_var.get().strip() else "auto"),
            "dpi": int(self._dpi_var.get()),
            "preprocessing": {k: v.get() for k, v in self._prep_vars.items()},
        }
        self.destroy()
