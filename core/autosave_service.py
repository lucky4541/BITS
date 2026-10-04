"""Centralized, debounced, crash-safe autosave for the zoning project JSON
(spec: "ZONING - CUPPEUB DEFAULT PROFILE + CONTINUOUS AUTOSAVE"). ONE
service, reused by every mutation site in gui/main_window.py via
App.mark_dirty(reason) - no separate ZoneAutoSave/TableAutoSave/OCRAutoSave/
ProfileAutoSave systems. Reuses core.project_manager.build_project_data()
(the exact same dict Save Project already writes) - autosave is a
different WRITE STRATEGY (debounced, background thread, atomic replace,
backup + recovery) over the identical project state, never a second
project format.
"""
import json
import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import debug_log

SCHEMA_VERSION = 1
DEBOUNCE_SECONDS = 0.7
CRITICAL_DEBOUNCE_SECONDS = 0.15
RETRY_DELAYS_SECONDS = [1, 3, 5]
POLL_INTERVAL_SECONDS = 0.1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def recovery_path(project_path: str) -> str:
    p = Path(project_path)
    return str(p.with_name(p.stem + ".recovery" + p.suffix))


def backup_path(project_path: str) -> str:
    return project_path + ".bak"


def encode_json(data: dict) -> str:
    """Compact JSON text for a project snapshot. json.dump(..., indent=2)
    always runs Python's pure-Python encoder, which held the interpreter
    lock long enough (about a second on a large project) to freeze the UI
    on every autosave, even though it ran on a background thread. The
    compact form uses the C encoder and is ~10x faster; json.load reads
    both forms identically, so old pretty-printed projects still open."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def atomic_write_json(path: str, data, _encoded: str = None):
    """Never leaves `path` half-written or empty (spec section 15/44): the
    new content is fully written, flushed, and fsynced to a temp file in
    the SAME directory (so the final replace is a same-filesystem rename -
    atomic on both POSIX and Windows via os.replace), and `path` itself is
    only ever touched by that one atomic replace - a crash at any point
    before it leaves the OLD `path` completely untouched."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        text = _encoded if _encoded is not None else encode_json(data)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def save_with_recovery(project_path: str, data: dict):
    """Writes project.recovery.json FIRST, then rotates the previous
    project.json into project.json.bak (spec section 16 - one backup
    generation, never unlimited), then atomically replaces project.json
    itself. Because the recovery file is written before project.json's own
    replace, a crash in between the two leaves recovery.json's embedded
    saved_at strictly NEWER than project.json's - exactly the signal
    check_recovery() below looks for (spec section 17/29)."""
    payload = dict(data)
    payload["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
    payload["autosave_meta"] = {"saved_at": _now_iso(), "schema_version": SCHEMA_VERSION}
    encoded = encode_json(payload)  # encode ONCE, write it to both files
    atomic_write_json(recovery_path(project_path), payload, _encoded=encoded)
    if os.path.exists(project_path):
        try:
            shutil.copy2(project_path, backup_path(project_path))
        except OSError:
            pass  # best-effort only - must never block the actual save below
    atomic_write_json(project_path, payload, _encoded=encoded)


def _saved_at(data: dict):
    return (data or {}).get("autosave_meta", {}).get("saved_at")


def check_recovery(project_path: str):
    """Returns the recovery snapshot dict if project.recovery.json holds a
    strictly newer saved_at than project.json (the app crashed after the
    recovery flush but before project.json's own replace completed), or if
    project.json is missing/unreadable while a recovery file exists.
    Returns None when there's nothing to recover - the common case where
    both files agree, or neither exists yet."""
    rpath = recovery_path(project_path)
    if not os.path.isfile(rpath):
        return None
    try:
        with open(rpath, "r", encoding="utf-8") as f:
            recovery_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not os.path.isfile(project_path):
        return recovery_data
    try:
        with open(project_path, "r", encoding="utf-8") as f:
            project_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return recovery_data
    recovery_saved_at = _saved_at(recovery_data)
    project_saved_at = _saved_at(project_data)
    if recovery_saved_at and (not project_saved_at or recovery_saved_at > project_saved_at):
        return recovery_data
    return None


class AutoSaveService:
    """ONE autosave service (spec section 11). gui/main_window.py's
    App.mark_dirty(reason) is the single call site every mutation routes
    through; this class only owns debouncing, background writing, retry,
    and status reporting - it never knows WHAT changed, only THAT it did.

    `snapshot_fn` is called on the MAIN thread (so it can safely read live
    zone_manager/settings state) and must return an already-finished plain
    dict - the background write thread only ever touches that immutable
    copy and the filesystem (spec section 14: never serialize a mutable
    project object while the UI might still be modifying it)."""

    def __init__(self, root, snapshot_fn, on_status_change=None):
        self._root = root
        self._snapshot_fn = snapshot_fn
        self._on_status_change = on_status_change
        self.project_path = None
        self._dirty = False
        self._timer_id = None
        self._write_thread = None
        self._retry_index = 0
        self._closed = False
        # Tcl/Tk is not thread-safe: calling root.after()/after_cancel()
        # FROM the background write thread intermittently raised "main
        # thread is not in main loop" (confirmed during this feature's own
        # test development - Tk's createcommand() registration checks it's
        # running on the interpreter's own thread). The background thread
        # therefore only ever touches this plain, thread-safe queue.Queue;
        # a self-rescheduling poll loop started on the MAIN thread (see
        # _start_polling/_poll_results) is the only thing that ever calls
        # back into self._on_save_success/_on_save_error.
        self._result_queue = queue.Queue()
        self._poll_id = None

    # ---------------- lifecycle ----------------
    def start(self, project_path: str):
        """Associates this service with `project_path`. A Save As (spec
        section 36) simply calls start() again with the new path - that
        alone moves autosave association: the OLD path stops receiving
        changes, since nothing else still references it."""
        self._cancel_timer()
        self.project_path = project_path
        self._dirty = False
        self._retry_index = 0
        self._closed = False
        self._start_polling()
        debug_log.log("AUTOSAVE", f"Project opened: {project_path}")

    def mark_dirty(self, reason: str, critical: bool = False):
        if self._closed or not self.project_path:
            return
        debug_log.log("AUTOSAVE", f"Dirty: {reason}")
        self._dirty = True
        self._set_status("Unsaved changes")
        self.schedule(critical)

    def schedule(self, critical: bool = False):
        self._cancel_timer()
        delay = CRITICAL_DEBOUNCE_SECONDS if critical else DEBOUNCE_SECONDS
        self._timer_id = self._root.after(int(delay * 1000), self._flush)

    def save_now(self):
        """Manual Ctrl+S (spec section 35): cancels any pending debounce and
        saves immediately."""
        self._cancel_timer()
        self._flush()

    def save_snapshot(self):
        """Synchronous save on the CALLING thread - used only at shutdown
        (spec section 28), where waiting for a background thread to be
        scheduled and its result marshalled back via root.after would
        outlive the Tk mainloop this class otherwise depends on for both."""
        if not self.project_path:
            return
        self._cancel_timer()
        snapshot = self._snapshot_fn()
        try:
            save_with_recovery(self.project_path, snapshot)
            self._dirty = False
            debug_log.log("AUTOSAVE", "Save complete")
        except Exception as e:
            debug_log.log("AUTOSAVE", f"Save failed: {e}")

    def recover(self):
        """Returns the recoverable snapshot (see check_recovery) for the
        current project_path, or None. Never applies it automatically
        (spec: "Allow: Recover, Discard... never silently destroy the
        user's normal project")."""
        if not self.project_path:
            return None
        recovered = check_recovery(self.project_path)
        if recovered:
            debug_log.log("AUTOSAVE", "Recovery available")
        return recovered

    def shutdown(self):
        """App close (spec section 28): join any save already in flight,
        then flush synchronously if anything is still dirty, so no change
        made in the last debounce window is ever lost."""
        self._cancel_timer()
        self._stop_polling()
        if self._write_thread is not None and self._write_thread.is_alive():
            self._write_thread.join(timeout=5)
        self._drain_results()  # a result queued just before shutdown must still clear self._dirty
        if self._dirty:
            self.save_snapshot()
        self._closed = True

    # ---------------- internals ----------------
    def _cancel_timer(self):
        if self._timer_id is not None:
            try:
                self._root.after_cancel(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    def _start_polling(self):
        if self._poll_id is None:
            self._poll_id = self._root.after(int(POLL_INTERVAL_SECONDS * 1000), self._poll_results)

    def _stop_polling(self):
        if self._poll_id is not None:
            try:
                self._root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    def _drain_results(self):
        while True:
            try:
                kind, path, payload = self._result_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "success":
                self._on_save_success()
            else:
                self._on_save_error(path, payload)

    def _poll_results(self):
        """Runs on the MAIN thread only (scheduled via root.after, never
        called from the background write thread) - the one and only place
        that turns a queued write result into a status update / retry."""
        self._poll_id = None
        self._drain_results()
        if not self._closed:
            self._poll_id = self._root.after(int(POLL_INTERVAL_SECONDS * 1000), self._poll_results)

    def _flush(self):
        self._timer_id = None
        if not self._dirty or not self.project_path or self._closed:
            return
        self._dirty = False
        path = self.project_path
        snapshot = self._snapshot_fn()
        self._set_status("Saving...")
        debug_log.log("AUTOSAVE", "Saving snapshot")
        self._write_thread = threading.Thread(target=self._write_in_background, args=(path, snapshot), daemon=True)
        self._write_thread.start()

    def _write_in_background(self, path, snapshot):
        """Off the Tk main thread (spec section 14/45: autosave must never
        freeze the PDF viewer/tag panel/dragging/scrolling) - `snapshot` is
        an already-finished plain dict captured on the main thread before
        this thread started, so no lock is needed: nothing here is shared,
        mutable state. Only ever touches self._result_queue (thread-safe)
        and the filesystem - never a Tk call (see __init__'s docstring)."""
        try:
            save_with_recovery(path, snapshot)
        except Exception as e:
            self._result_queue.put(("error", path, e))
            return
        self._result_queue.put(("success", path, None))

    def _on_save_success(self):
        debug_log.log("AUTOSAVE", "Save complete")
        self._retry_index = 0
        self._set_status(f"Saved {time.strftime('%H:%M:%S')}")

    def _on_save_error(self, path, e):
        debug_log.log("AUTOSAVE", f"Save failed: {e}")
        self._set_status("Error")
        if self._closed or path != self.project_path:
            return
        delay = RETRY_DELAYS_SECONDS[min(self._retry_index, len(RETRY_DELAYS_SECONDS) - 1)]
        self._retry_index += 1
        self._dirty = True  # keep retrying until it succeeds or the project path changes
        self._timer_id = self._root.after(int(delay * 1000), self._flush)

    def _set_status(self, text: str):
        if self._on_status_change:
            try:
                self._on_status_change(text)
            except Exception:
                pass
