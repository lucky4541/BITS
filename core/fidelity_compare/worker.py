"""Background-thread runner for a full comparison (spec sections 65-67:
"use a background worker... do not load all page images into memory...
provide [Cancel Comparison]... cancellation must not modify any input
file"). Matches this project's existing GUI threading convention
(gui/ocr_dialogs.py: a daemon thread posts progress/result onto a
queue.Queue, the Tk side polls it via root.after()) rather than
inventing a new one - the fidelity_compare GUI (fidelity_compare_window.py)
polls this same way.

Cancellation is cooperative/"soft" (same as the existing OCR dialog): the
worker checks `should_cancel()` between pipeline stages and stops
advancing; it never leaves partial state behind because it never writes
anything to Original PDF / EPUB / Converted PDF in the first place - this
whole subsystem is read-only, so "cancellation must not modify any input
file" is automatically satisfied by that read-only design, not by any
special rollback logic here."""
import queue
import threading


class ComparisonWorker:
    def __init__(self, run_fn):
        """run_fn(progress_cb, should_cancel) -> result dict. Called on a
        background daemon thread; progress_cb(stage: str, current: int,
        total: int) and should_cancel() -> bool are both thread-safe."""
        self._run_fn = run_fn
        self._cancelled = threading.Event()
        self._queue = queue.Queue()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancelled.set()

    def should_cancel(self) -> bool:
        return self._cancelled.is_set()

    def _run(self):
        def progress_cb(stage, current, total):
            self._queue.put(("progress", stage, current, total))
        try:
            result = self._run_fn(progress_cb, self.should_cancel)
            self._queue.put(("done", result))
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI, never crashes the worker thread silently
            self._queue.put(("error", exc))

    def poll(self, timeout: float = 0.0):
        """Non-blocking (or short-blocking) drain of pending queue
        messages - the GUI calls this from a root.after() tick, exactly
        like gui/ocr_dialogs.py's own _poll_test pattern."""
        messages = []
        try:
            while True:
                messages.append(self._queue.get(block=timeout > 0, timeout=timeout or None))
                timeout = 0.0
        except queue.Empty:
            pass
        return messages
