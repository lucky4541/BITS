"""COMPARISON module entry point - now the real "Advanced Fidelity
Compare" implementation (see app/comparison/fidelity_compare_window.py),
replacing the earlier navigable placeholder. Kept as its own thin module
(rather than folding fidelity_compare_window.py's own open_window
directly into launcher_window.py) so the Launcher's own import
(`from app.comparison import comparison_window`) never needs to change -
only this module's internals did."""
import tkinter as tk

from app.comparison.fidelity_compare_window import open_window as _open_fidelity_compare_window


def open_window(launcher_root: tk.Tk, on_home):
    return _open_fidelity_compare_window(launcher_root, on_home)
