"""Always-on production startup/error log.

Logs to:
    %APPDATA%\\EPUBForge\\logs\\EPUBForge.log

This module provides:
- global Python exception logging
- Tkinter callback exception logging
- worker-thread exception logging
- complete traceback and exception-chain preservation
- user-friendly unexpected-error dialog

This module must remain independent from core.debug_log.
"""

import os
import sys
import threading
import traceback
from datetime import datetime

from core.resource_path import writable_path


_log_path = None

# Prevent the same exception from being logged repeatedly through multiple
# exception-routing mechanisms.
_logged_exception_ids = set()
_logged_exception_lock = threading.Lock()

# Prevent recursive error-dialog handling.
_handling_exception = threading.local()


def log_path() -> str:
    global _log_path

    if _log_path is None:
        _log_path = writable_path("logs", "BITSTool.log")
        os.makedirs(os.path.dirname(_log_path), exist_ok=True)

    return _log_path


def _write(level: str, message: str):
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} "
        f"[{level}] {message}"
    )

    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        # Logging must NEVER crash the application.
        pass


def info(message: str):
    _write("INFO", message)


def _mark_exception_logged(exc_value: BaseException) -> bool:
    """Return True only the first time this exception object is logged."""
    if exc_value is None:
        return True

    key = id(exc_value)

    with _logged_exception_lock:
        if key in _logged_exception_ids:
            return False

        _logged_exception_ids.add(key)

        # Prevent unlimited growth during a long-running application.
        if len(_logged_exception_ids) > 2048:
            _logged_exception_ids.clear()
            _logged_exception_ids.add(key)

    return True


def _format_exception(exc_type, exc_value, exc_tb) -> str:
    """Return the complete original traceback, including exception chaining."""

    try:
        return "".join(
            traceback.format_exception(
                exc_type,
                exc_value,
                exc_tb,
            )
        )
    except Exception:
        # Logging must still work even if traceback formatting itself fails.
        try:
            return (
                f"{getattr(exc_type, '__name__', str(exc_type))}: "
                f"{exc_value}\n"
            )
        except Exception:
            return "Unable to format exception traceback.\n"


def error(
    message: str,
    exc: BaseException = None,
    *,
    exc_type=None,
    exc_tb=None,
):
    """Write an ERROR entry with the complete original traceback."""

    if exc is not None:
        if not _mark_exception_logged(exc):
            return

        if exc_type is None:
            exc_type = type(exc)

        if exc_tb is None:
            exc_tb = exc.__traceback__

        traceback_text = _format_exception(
            exc_type,
            exc,
            exc_tb,
        )

        message = (
            f"{message}\n"
            f"Exception type: "
            f"{getattr(exc_type, '__name__', str(exc_type))}\n"
            f"Exception message: {exc}\n"
            f"Traceback:\n"
            f"{traceback_text}"
        )

    _write("ERROR", message)


def _show_error_dialog():
    """Show the normal production error dialog safely."""

    # Never recursively create another error dialog.
    if getattr(_handling_exception, "active", False):
        return

    _handling_exception.active = True

    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter._default_root
        owns_root = root is None

        if owns_root:
            root = tkinter.Tk()
            root.withdraw()

        try:
            messagebox.showerror(
                "BITS Tool - Unexpected Error",
                "BITS Tool encountered an unexpected error and needs to close.\n\n"
                f"Details were written to:\n{log_path()}",
                parent=root,
            )
        finally:
            if owns_root and root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    except Exception:
        # Never allow error reporting to replace the original exception.
        pass

    finally:
        _handling_exception.active = False


def _global_exception_hook(exc_type, exc_value, exc_tb):
    """Handle uncaught exceptions reaching sys.excepthook."""

    # Python uses this for normal uncaught exceptions.
    #
    # Do not replace the original traceback with anything generated during
    # cleanup, Tkinter shutdown, or dialog handling.
    try:
        error(
            "Unhandled exception",
            exc_value,
            exc_type=exc_type,
            exc_tb=exc_tb,
        )
    except Exception:
        # The logging system must never mask the original exception.
        pass

    _show_error_dialog()

    # Preserve the standard Python behavior after our logging.
    try:
        sys.__excepthook__(
            exc_type,
            exc_value,
            exc_tb,
        )
    except Exception:
        pass


def _tk_callback_exception_hook(exc_type, exc_value, exc_tb):
    """Handle exceptions raised inside Tkinter callbacks.

    Tkinter normally sends callback exceptions through
    report_callback_exception() instead of sys.excepthook().
    """

    try:
        error(
            "Unhandled Tkinter callback exception",
            exc_value,
            exc_type=exc_type,
            exc_tb=exc_tb,
        )
    except Exception:
        pass

    _show_error_dialog()


def _thread_exception_hook(args):
    """Handle uncaught exceptions from worker threads."""

    try:
        thread = getattr(args, "thread", None)
        thread_name = (
            getattr(thread, "name", None)
            or "<unknown>"
        )

        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)

        if exc_value is None:
            return

        error(
            f"Unhandled worker-thread exception "
            f"(thread={thread_name})",
            exc_value,
            exc_type=exc_type,
            exc_tb=exc_tb,
        )

        # Do not attempt to create a Tkinter messagebox from a worker
        # thread. Tkinter UI operations must remain on the GUI thread.

    except Exception:
        # Exception reporting must never crash the worker infrastructure.
        pass


def _install_tkinter_callback_hook():
    """Install the Tkinter callback exception hook globally.

    Tkinter creates the root after install_excepthook() is called, so
    installing directly on the root is insufficient.

    Tkinter's report_callback_exception() is defined on tkinter.Misc,
    which is inherited by Tk and widgets. Replacing that method here
    allows all Tkinter callback exceptions to reach our logger without
    requiring changes to launcher_window.py or any other GUI module.
    """

    try:
        import tkinter

        # Do not install more than once.
        if getattr(
            tkinter.Misc,
            "_epubforge_exception_hook_installed",
            False,
        ):
            return

        original = tkinter.Misc.report_callback_exception

        def report_callback_exception(
            widget_self,
            exc_type,
            exc_value,
            exc_tb,
        ):
            try:
                _tk_callback_exception_hook(
                    exc_type,
                    exc_value,
                    exc_tb,
                )
            except Exception:
                pass

            # Do NOT call the original Tkinter implementation after our
            # handler if it would merely print the same exception and cause
            # confusing duplicate diagnostics.
            #
            # The original exception has already been fully logged.

        # Keep a reference to the original method for diagnostics and
        # compatibility.
        tkinter.Misc._epubforge_original_report_callback_exception = (
            original
        )

        tkinter.Misc.report_callback_exception = (
            report_callback_exception
        )

        tkinter.Misc._epubforge_exception_hook_installed = True

    except Exception:
        # Tkinter may not be available during very early startup.
        pass


def _install_threading_exception_hook():
    """Install threading.excepthook when supported."""

    try:
        if hasattr(threading, "excepthook"):
            threading.excepthook = _thread_exception_hook
    except Exception:
        pass


def install_excepthook():
    """Install EPUBForge-wide exception handling.

    Covers:
        1. uncaught Python exceptions
        2. Tkinter callback exceptions
        3. worker-thread exceptions

    This function is safe to call more than once.
    """

    # Global Python exceptions.
    sys.excepthook = _global_exception_hook

    # Tkinter callback exceptions.
    _install_tkinter_callback_hook()

    # Worker-thread exceptions.
    _install_threading_exception_hook()