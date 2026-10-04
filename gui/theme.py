# """Central design system for ZoneTool's UI - colors, fonts, spacing, ttk
# styles, tooltips and toast notifications. Presentation only: nothing here
# touches zone data, PDF extraction, mapping, or any generation pipeline
# (spec section 66.32).

# Two things intentionally do NOT get a full "modern flat" treatment even
# though the spec asks for a premium look everywhere: real drop shadows and
# smooth (100-200ms) transition animations. Tkinter/Tk has no compositor -
# there is no native box-shadow, and widget-property animation means manually
# stepping colors on a timer, which reads as janky rather than "quick and
# professional" on this toolkit. Where the spec calls for a shadow, a subtle
# 1px darker border is used instead (the closest honest equivalent); hover/
# press feedback is an instant color swap rather than a tween.
# """
# import tkinter as tk
# from tkinter import ttk

# FONT_FAMILY = "Segoe UI"

# FONT_APP_TITLE = (FONT_FAMILY, 13, "bold")
# FONT_PANEL_TITLE = (FONT_FAMILY, 9, "bold")
# FONT_BODY = (FONT_FAMILY, 9)
# FONT_BODY_BOLD = (FONT_FAMILY, 9, "bold")
# FONT_SMALL = (FONT_FAMILY, 8)
# FONT_SMALL_BOLD = (FONT_FAMILY, 8, "bold")
# FONT_MONO_SMALL = ("Consolas", 8)

# SPACE_XS = 2
# SPACE_SM = 4
# SPACE_MD = 8
# SPACE_LG = 12
# SPACE_XL = 16

# TOOLBAR_BTN_HEIGHT = 26

# LIGHT = {
#     "mode": "light",
#     "app_bg": "#EEF0F3",
#     "surface": "#FFFFFF",
#     "surface_alt": "#F7F8FA",
#     "header_bg": "#1F2430",
#     "header_fg": "#F4F6FA",
#     "header_fg_muted": "#AEB6C4",
#     "toolbar_bg": "#F3F4F6",
#     "toolbar_border": "#D8DCE1",
#     "panel_bg": "#F7F8FA",
#     "panel_header_bg": "#EEF0F3",
#     "border": "#D8DCE1",
#     "border_strong": "#B7BEC7",
#     "text": "#1B1F24",
#     "text_muted": "#6B7280",
#     "text_faint": "#9AA1AA",
#     "accent": "#2563EB",
#     "accent_hover": "#1D4ED8",
#     "accent_active": "#1E40AF",
#     "accent_tint": "#E4ECFD",
#     "accent_fg": "#FFFFFF",
#     "success": "#16A34A",
#     "success_tint": "#E4F6EA",
#     "warning": "#D97706",
#     "warning_tint": "#FDF1DF",
#     "error": "#DC2626",
#     "error_tint": "#FCE8E8",
#     "selected_row_bg": "#DCE8FE",
#     "hover_bg": "#E9EBEF",
#     "button_bg": "#FFFFFF",
#     "button_hover_bg": "#F1F3F5",
#     "button_pressed_bg": "#E3E6EA",
#     "button_disabled_fg": "#B0B5BC",
#     "canvas_bg": "#DADEE3",
#     "page_border": "#9AA1AA",
# }

# DARK = {
#     "mode": "dark",
#     "app_bg": "#17181B",
#     "surface": "#1E2024",
#     "surface_alt": "#232529",
#     "header_bg": "#111318",
#     "header_fg": "#F4F6FA",
#     "header_fg_muted": "#8B93A1",
#     "toolbar_bg": "#1C1E22",
#     "toolbar_border": "#33363B",
#     "panel_bg": "#1A1C20",
#     "panel_header_bg": "#212327",
#     "border": "#33363B",
#     "border_strong": "#454850",
#     "text": "#E8E9EA",
#     "text_muted": "#9AA0A6",
#     "text_faint": "#6E727A",
#     "accent": "#5B9DF9",
#     "accent_hover": "#7CB0FA",
#     "accent_active": "#3E7FDB",
#     "accent_tint": "#1E2E4A",
#     "accent_fg": "#0B1220",
#     "success": "#4ADE80",
#     "success_tint": "#12301E",
#     "warning": "#FBBF24",
#     "warning_tint": "#3A2C0C",
#     "error": "#F87171",
#     "error_tint": "#3A1414",
#     "selected_row_bg": "#28405E",
#     "hover_bg": "#2A2D32",
#     "button_bg": "#232529",
#     "button_hover_bg": "#2A2D32",
#     "button_pressed_bg": "#33363B",
#     "button_disabled_fg": "#54585F",
#     "canvas_bg": "#0E0F11",
#     "page_border": "#454850",
# }

# TAG_CATEGORY_PALETTE = [
#     "#2563EB", "#7C3AED", "#059669", "#D97706", "#DB2777", "#0891B2", "#65A30D", "#DC2626",
# ]


# class ThemeManager:
#     """Single shared instance (gui.theme.current) - every widget-construction
#     site reads colors from here rather than hardcoding hex values, so
#     Settings > Theme can flip Light/Dark at runtime by re-applying styles
#     and having each panel re-read current.palette."""
#     def __init__(self):
#         self.palette = LIGHT
#         self._listeners = []

#     def set_mode(self, mode: str):
#         self.palette = DARK if mode == "dark" else LIGHT
#         for cb in list(self._listeners):
#             cb(self.palette)

#     def on_change(self, callback):
#         """callback(palette) is invoked immediately (with the current
#         palette) and again every time set_mode() runs - each panel
#         registers once in its own __init__ and re-applies its own colors."""
#         self._listeners.append(callback)
#         callback(self.palette)


# current = ThemeManager()


# def apply_ttk_style(root, palette):
#     """Configures the ttk widgets already used elsewhere (Treeview,
#     Combobox, Scrollbar, PanedWindow, Separator) - these have a real style
#     API, unlike the plain tk.Button/tk.Frame/tk.Label used for toolbar/tag
#     buttons, which are styled directly at their own construction site
#     instead (see style_button/style_panel_label below)."""
#     style = ttk.Style(root)
#     style.theme_use("clam")

#     style.configure("TFrame", background=palette["app_bg"])
#     style.configure("TSeparator", background=palette["border"])

#     style.configure("TCombobox", fieldbackground=palette["surface"], background=palette["surface"],
#                      foreground=palette["text"], arrowcolor=palette["text_muted"], bordercolor=palette["border"],
#                      lightcolor=palette["surface"], darkcolor=palette["surface"], padding=3)
#     style.map("TCombobox", fieldbackground=[("readonly", palette["surface"])],
#               foreground=[("disabled", palette["button_disabled_fg"])])

#     style.configure("Vertical.TScrollbar", background=palette["panel_bg"], troughcolor=palette["panel_bg"],
#                      bordercolor=palette["panel_bg"], arrowcolor=palette["text_muted"], relief="flat")
#     style.configure("Horizontal.TScrollbar", background=palette["panel_bg"], troughcolor=palette["panel_bg"],
#                      bordercolor=palette["panel_bg"], arrowcolor=palette["text_muted"], relief="flat")
#     style.map("Vertical.TScrollbar", background=[("active", palette["border_strong"])])
#     style.map("Horizontal.TScrollbar", background=[("active", palette["border_strong"])])

#     style.configure("Treeview", background=palette["surface"], fieldbackground=palette["surface"],
#                      foreground=palette["text"], bordercolor=palette["border"], borderwidth=0,
#                      rowheight=24, font=FONT_BODY)
#     style.map("Treeview", background=[("selected", palette["selected_row_bg"])],
#               foreground=[("selected", palette["text"])])
#     style.configure("Treeview.Heading", background=palette["panel_header_bg"], foreground=palette["text_muted"],
#                      relief="flat", font=FONT_SMALL_BOLD, padding=(4, 4))
#     style.map("Treeview.Heading", background=[("active", palette["hover_bg"])])

#     style.configure("TPanedwindow", background=palette["app_bg"])
#     style.configure("Sash", sashthickness=6, gripcount=0)

#     root.configure(bg=palette["app_bg"])


# def style_toplevel(widget, palette=None):
#     widget.configure(bg=(palette or current.palette)["app_bg"])


# def style_button(btn: tk.Button, palette, kind: str = "secondary", height: int = TOOLBAR_BTN_HEIGHT):
#     """Applies consistent flat, professional button styling plus a hover
#     (Enter/Leave) and pressed (ButtonPress/ButtonRelease) color swap -
#     the closest honest equivalent to a CSS hover/active transition on a
#     plain tk.Button, which has no built-in visual states beyond its
#     default 3D relief.

#     kind: "primary" (accent-filled, for the ONE most important action in a
#     group, e.g. Generate XHTML), "secondary" (neutral, most toolbar
#     buttons), "ghost" (no border, for icon-only/quiet actions)."""
#     if kind == "primary":
#         bg, hover, pressed, fg = palette["accent"], palette["accent_hover"], palette["accent_active"], palette["accent_fg"]
#         border = palette["accent"]
#     else:
#         bg, hover, pressed, fg = palette["button_bg"], palette["button_hover_bg"], palette["button_pressed_bg"], palette["text"]
#         border = palette["border"] if kind != "ghost" else bg

#     btn.configure(
#         bg=bg, fg=fg, activebackground=pressed, activeforeground=fg,
#         relief="flat", bd=1, highlightthickness=1, highlightbackground=border, highlightcolor=border,
#         font=FONT_BODY, cursor="hand2", padx=10, pady=3,
#     )

#     def _enter(_e):
#         if str(btn["state"]) != "disabled":
#             btn.configure(bg=hover)

#     def _leave(_e):
#         if str(btn["state"]) != "disabled":
#             btn.configure(bg=bg)

#     def _press(_e):
#         if str(btn["state"]) != "disabled":
#             btn.configure(bg=pressed)

#     def _release(_e):
#         if str(btn["state"]) != "disabled":
#             btn.configure(bg=hover)

#     btn.bind("<Enter>", _enter)
#     btn.bind("<Leave>", _leave)
#     btn.bind("<ButtonPress-1>", _press, add="+")
#     btn.bind("<ButtonRelease-1>", _release, add="+")
#     return btn


# class Tooltip:
#     """Standard delayed hover tooltip (spec 66.28 - meaningful labels via
#     tooltips on toolbar/icon controls). One instance per widget; shows
#     after a short delay so it doesn't flash during normal mouse travel."""
#     def __init__(self, widget, text: str, delay_ms: int = 500):
#         self.widget = widget
#         self.text = text
#         self.delay_ms = delay_ms
#         self._after_id = None
#         self._tip = None
#         widget.bind("<Enter>", self._schedule, add="+")
#         widget.bind("<Leave>", self._hide, add="+")
#         widget.bind("<ButtonPress>", self._hide, add="+")

#     def set_text(self, text: str):
#         self.text = text

#     def _schedule(self, _event=None):
#         self._cancel()
#         self._after_id = self.widget.after(self.delay_ms, self._show)

#     def _show(self):
#         if self._tip is not None or not self.text:
#             return
#         x = self.widget.winfo_rootx() + 12
#         y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
#         self._tip = tk.Toplevel(self.widget)
#         self._tip.wm_overrideredirect(True)
#         self._tip.wm_geometry(f"+{x}+{y}")
#         palette = current.palette
#         tk.Label(self._tip, text=self.text, bg=palette["text"], fg=palette["surface"],
#                   font=FONT_SMALL, padx=6, pady=3, justify="left").pack()
#         self._tip.attributes("-topmost", True)

#     def _cancel(self):
#         if self._after_id is not None:
#             self.widget.after_cancel(self._after_id)
#             self._after_id = None

#     def _hide(self, _event=None):
#         self._cancel()
#         if self._tip is not None:
#             self._tip.destroy()
#             self._tip = None


# _TOAST_KIND_KEYS = {
#     "success": ("success", "success_tint", "✓"),
#     "warning": ("warning", "warning_tint", "⚠"),
#     "error": ("error", "error_tint", "✕"),
#     "info": ("accent", "accent_tint", "ℹ"),
# }


# def show_toast(host: tk.Widget, message: str, kind: str = "success", duration_ms: int = 2800):
#     """Compact, non-blocking notification (spec 66.15) anchored to the
#     bottom-right of `host` (typically the PDF viewer canvas's parent), for
#     routine confirmations that don't need an acknowledged dialog (Zone
#     created, Merge completed, Project saved...). Auto-dismisses; never
#     steals focus, never blocks input, never permanently covers the PDF
#     (stacks upward and expires within a few seconds)."""
#     palette = current.palette
#     color_key, tint_key, glyph = _TOAST_KIND_KEYS.get(kind, _TOAST_KIND_KEYS["info"])
#     color, tint = palette[color_key], palette[tint_key]
#     frame = tk.Frame(host, bg=tint, highlightthickness=1, highlightbackground=color)
#     tk.Label(frame, text=glyph, bg=tint, fg=color, font=FONT_BODY_BOLD).pack(side="left", padx=(10, 4), pady=6)
#     tk.Label(frame, text=message, bg=tint, fg=palette["text"], font=FONT_BODY).pack(side="left", padx=(0, 10), pady=6)
#     frame.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
#     frame.lift()
#     host.after(duration_ms, frame.destroy)
#     return frame


# def bind_touchpad_scroll(widget: tk.Widget, callback) -> bool:
#     """Binds Tk 9's <TouchpadScroll> event (TIP 684 - core.tcl-lang.org/
#     tips/doc/main/tip/684.md) to callback(dx, dy), called with already-
#     unpacked, signed-integer high-resolution deltas.

#     Confirmed as a REAL, currently-observed gap, not a hypothetical one: a
#     real Tk-9.0-on-Windows user report showed a laptop's precision
#     touchpad generating ZERO <MouseWheel> events (verified via this app's
#     own global bind_all diagnostic catching NOTHING for the gesture)
#     despite scrolling working correctly in every other Windows
#     application - Tk 9.0 introduced <TouchpadScroll> as a SEPARATE event
#     type from the legacy <MouseWheel> specifically for this class of
#     high-resolution touchpad input, and this Windows Precision Touchpad
#     driver evidently only emits the new one.

#     Tkinter's own Python Event object does not yet parse this event's new
#     %D substitution into any attribute (confirmed directly: binding
#     <TouchpadScroll> through the normal .bind() API leaves event.delta at
#     a hardcoded 0 and there is no .D attribute at all) - so this bypasses
#     Python's Event-object translation entirely and binds a raw Tcl script
#     that calls tk::PreciseScrollDeltas (Tk 9.0's OWN official unpacking
#     helper for this exact substitution, confirmed present via `info
#     commands`) directly on %D, then forwards the two already-unpacked
#     numbers to a real Python callback via widget.register - reusing Tk's
#     own unpacking logic rather than reimplementing the bit-packing format
#     a second time.

#     Returns False (a harmless no-op, never an error) on any Tk build
#     older than 9.0, where tk::PreciseScrollDeltas doesn't exist and the
#     event is never generated in the first place - every existing
#     <MouseWheel>-based binding this is layered alongside keeps working
#     completely unchanged there."""
#     try:
#         widget.tk.call("info", "commands", "tk::PreciseScrollDeltas")
#     except tk.TclError:
#         return False
#     cmd_name = widget.register(lambda dx, dy: callback(int(dx), int(dy)))
#     script = f"set _ztPadD [tk::PreciseScrollDeltas %D]; {cmd_name} [lindex $_ztPadD 0] [lindex $_ztPadD 1]"
#     try:
#         # "+"-prefixed raw Tcl bind (Tcl's own additive-bind syntax, the
#         # equivalent of Python bind()'s add="+") so this never clobbers
#         # some other <TouchpadScroll> binding already on the same widget.
#         widget.tk.call("bind", str(widget), "<TouchpadScroll>", "+" + script)
#         return True
#     except tk.TclError:
#         return False
"""Central design system for ZoneTool's UI - colors, fonts, spacing, ttk
styles, tooltips and toast notifications. Presentation only."""
import tkinter as tk
from tkinter import ttk

FONT_FAMILY = "Segoe UI"

FONT_APP_TITLE    = (FONT_FAMILY, 13, "bold")
FONT_PANEL_TITLE  = (FONT_FAMILY, 9, "bold")
FONT_BODY         = (FONT_FAMILY, 9)
FONT_BODY_BOLD    = (FONT_FAMILY, 9, "bold")
FONT_SMALL        = (FONT_FAMILY, 8)
FONT_SMALL_BOLD   = (FONT_FAMILY, 8, "bold")
FONT_MONO_SMALL   = ("Consolas", 8)

SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL = 2, 4, 8, 12, 16

TOOLBAR_BTN_HEIGHT = 26

LIGHT = {
    "mode": "light",
    "app_bg": "#EEF0F3", "surface": "#FFFFFF", "surface_alt": "#F7F8FA",
    "header_bg": "#1F2430", "header_fg": "#F4F6FA", "header_fg_muted": "#AEB6C4",
    "toolbar_bg": "#F3F4F6", "toolbar_border": "#D8DCE1",
    "panel_bg": "#F7F8FA", "panel_header_bg": "#EEF0F3",
    "border": "#D8DCE1", "border_strong": "#B7BEC7",
    "text": "#1B1F24", "text_muted": "#6B7280", "text_faint": "#9AA1AA",
    "accent": "#2563EB", "accent_hover": "#1D4ED8", "accent_active": "#1E40AF",
    "accent_tint": "#E4ECFD", "accent_fg": "#FFFFFF",
    "success": "#16A34A", "success_tint": "#E4F6EA",
    "warning": "#D97706", "warning_tint": "#FDF1DF",
    "error":   "#DC2626", "error_tint":   "#FCE8E8",
    "selected_row_bg": "#DCE8FE", "hover_bg": "#E9EBEF",
    "button_bg": "#FFFFFF", "button_hover_bg": "#F1F3F5",
    "button_pressed_bg": "#E3E6EA", "button_disabled_fg": "#B0B5BC",
    "canvas_bg": "#DADEE3", "page_border": "#9AA1AA",
}

DARK = {
    "mode": "dark",
    "app_bg": "#17181B", "surface": "#1E2024", "surface_alt": "#232529",
    "header_bg": "#111318", "header_fg": "#F4F6FA", "header_fg_muted": "#8B93A1",
    "toolbar_bg": "#1C1E22", "toolbar_border": "#33363B",
    "panel_bg": "#1A1C20", "panel_header_bg": "#212327",
    "border": "#33363B", "border_strong": "#454850",
    "text": "#E8E9EA", "text_muted": "#9AA0A6", "text_faint": "#6E727A",
    "accent": "#5B9DF9", "accent_hover": "#7CB0FA", "accent_active": "#3E7FDB",
    "accent_tint": "#1E2E4A", "accent_fg": "#0B1220",
    "success": "#4ADE80", "success_tint": "#12301E",
    "warning": "#FBBF24", "warning_tint": "#3A2C0C",
    "error":   "#F87171", "error_tint":   "#3A1414",
    "selected_row_bg": "#28405E", "hover_bg": "#2A2D32",
    "button_bg": "#232529", "button_hover_bg": "#2A2D32",
    "button_pressed_bg": "#33363B", "button_disabled_fg": "#54585F",
    "canvas_bg": "#0E0F11", "page_border": "#454850",
}

TAG_CATEGORY_PALETTE = [
    "#2563EB", "#7C3AED", "#059669", "#D97706", "#DB2777", "#0891B2", "#65A30D", "#DC2626",
]

# Any key a widget might read. ensure_palette_complete() guarantees all are
# present and non-empty so a widget construction can never see None for a
# Tk color option (which raises TclError deep inside tk.call).
_REQUIRED_KEYS = set(LIGHT.keys()) | set(DARK.keys())

_FALLBACKS = {**LIGHT, **DARK}


def ensure_palette_complete(palette: dict) -> dict:
    """Returns a NEW dict with every required key guaranteed non-empty.
    Real values always win; only missing/blank entries get a fallback."""
    out = dict(palette)
    for k in _REQUIRED_KEYS:
        if not out.get(k):
            out[k] = _FALLBACKS[k]
    return out


class ThemeManager:
    """Single shared instance (gui.theme.current). Every widget reads colors
    from here rather than hardcoding hex values, so Settings > Theme can
    flip Light/Dark at runtime."""
    def __init__(self):
        self.palette = ensure_palette_complete(LIGHT)
        self._listeners = []

    def set_mode(self, mode: str):
        self.palette = ensure_palette_complete(DARK if mode == "dark" else LIGHT)
        for cb in list(self._listeners):
            try:
                cb(self.palette)
            except Exception:
                # A single misbehaving listener must not break the mode
                # switch for every other panel.
                import traceback; traceback.print_exc()

    def snapshot(self) -> dict:
        """Frozen copy of the current palette. Panels should capture one of
        these in __init__ rather than reading theme.current.palette live
        during construction - a mid-construction set_mode() otherwise
        changes colors under the constructor's feet and can crash ttk."""
        return ensure_palette_complete(self.palette)

    def on_change(self, callback):
        self._listeners.append(callback)
        callback(self.palette)


current = ThemeManager()


def apply_ttk_style(root, palette):
    palette = ensure_palette_complete(palette)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure("TFrame", background=palette["app_bg"])
    style.configure("TSeparator", background=palette["border"])

    style.configure("TCombobox", fieldbackground=palette["surface"], background=palette["surface"],
                     foreground=palette["text"], arrowcolor=palette["text_muted"],
                     bordercolor=palette["border"], lightcolor=palette["surface"],
                     darkcolor=palette["surface"], padding=3)
    style.map("TCombobox", fieldbackground=[("readonly", palette["surface"])],
              foreground=[("disabled", palette["button_disabled_fg"])])

    style.configure("Vertical.TScrollbar", background=palette["panel_bg"], troughcolor=palette["panel_bg"],
                     bordercolor=palette["panel_bg"], arrowcolor=palette["text_muted"], relief="flat")
    style.configure("Horizontal.TScrollbar", background=palette["panel_bg"], troughcolor=palette["panel_bg"],
                     bordercolor=palette["panel_bg"], arrowcolor=palette["text_muted"], relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", palette["border_strong"])])
    style.map("Horizontal.TScrollbar", background=[("active", palette["border_strong"])])

    style.configure("Treeview", background=palette["surface"], fieldbackground=palette["surface"],
                     foreground=palette["text"], bordercolor=palette["border"], borderwidth=0,
                     rowheight=24, font=FONT_BODY)
    style.map("Treeview", background=[("selected", palette["selected_row_bg"])],
              foreground=[("selected", palette["text"])])
    style.configure("Treeview.Heading", background=palette["panel_header_bg"],
                     foreground=palette["text_muted"], relief="flat", font=FONT_SMALL_BOLD, padding=(4, 4))
    style.map("Treeview.Heading", background=[("active", palette["hover_bg"])])

    style.configure("TPanedwindow", background=palette["app_bg"])
    style.configure("Sash", sashthickness=6, gripcount=0)

    root.configure(bg=palette["app_bg"])


def style_toplevel(widget, palette=None):
    widget.configure(bg=ensure_palette_complete(palette or current.palette)["app_bg"])


def style_button(btn: tk.Button, palette, kind: str = "secondary", height: int = TOOLBAR_BTN_HEIGHT):
    palette = ensure_palette_complete(palette)
    if kind == "primary":
        bg, hover, pressed, fg = palette["accent"], palette["accent_hover"], palette["accent_active"], palette["accent_fg"]
        border = palette["accent"]
    else:
        bg, hover, pressed, fg = palette["button_bg"], palette["button_hover_bg"], palette["button_pressed_bg"], palette["text"]
        border = palette["border"] if kind != "ghost" else bg

    btn.configure(
        bg=bg, fg=fg, activebackground=pressed, activeforeground=fg,
        relief="flat", bd=1, highlightthickness=1, highlightbackground=border,
        highlightcolor=border, font=FONT_BODY, cursor="hand2", padx=10, pady=3,
    )

    def _enter(_e):
        if str(btn["state"]) != "disabled": btn.configure(bg=hover)
    def _leave(_e):
        if str(btn["state"]) != "disabled": btn.configure(bg=bg)
    def _press(_e):
        if str(btn["state"]) != "disabled": btn.configure(bg=pressed)
    def _release(_e):
        if str(btn["state"]) != "disabled": btn.configure(bg=hover)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)
    btn.bind("<ButtonPress-1>", _press, add="+")
    btn.bind("<ButtonRelease-1>", _release, add="+")
    return btn


class Tooltip:
    def __init__(self, widget, text: str, delay_ms: int = 500):
        self.widget, self.text, self.delay_ms = widget, text, delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str): self.text = text

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip is not None or not self.text: return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        palette = current.snapshot()
        tk.Label(self._tip, text=self.text, bg=palette["text"], fg=palette["surface"],
                  font=FONT_SMALL, padx=6, pady=3, justify="left").pack()
        self._tip.attributes("-topmost", True)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id); self._after_id = None

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy(); self._tip = None


_TOAST_KIND_KEYS = {
    "success": ("success", "success_tint", "✓"),
    "warning": ("warning", "warning_tint", "⚠"),
    "error":   ("error", "error_tint", "✕"),
    "info":    ("accent", "accent_tint", "ℹ"),
}


def show_toast(host: tk.Widget, message: str, kind: str = "success", duration_ms: int = 2800):
    palette = current.snapshot()
    color_key, tint_key, glyph = _TOAST_KIND_KEYS.get(kind, _TOAST_KIND_KEYS["info"])
    color, tint = palette[color_key], palette[tint_key]
    frame = tk.Frame(host, bg=tint, highlightthickness=1, highlightbackground=color)
    tk.Label(frame, text=glyph, bg=tint, fg=color, font=FONT_BODY_BOLD).pack(side="left", padx=(10, 4), pady=6)
    tk.Label(frame, text=message, bg=tint, fg=palette["text"], font=FONT_BODY).pack(side="left", padx=(0, 10), pady=6)
    frame.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
    frame.lift()
    host.after(duration_ms, frame.destroy)
    return frame


def bind_touchpad_scroll(widget: tk.Widget, callback) -> bool:
    try:
        widget.tk.call("info", "commands", "tk::PreciseScrollDeltas")
    except tk.TclError:
        return False
    cmd_name = widget.register(lambda dx, dy: callback(int(dx), int(dy)))
    script = f"set _ztPadD [tk::PreciseScrollDeltas %D]; {cmd_name} [lindex $_ztPadD 0] [lindex $_ztPadD 1]"
    try:
        widget.tk.call("bind", str(widget), "<TouchpadScroll>", "+" + script)
        return True
    except tk.TclError:
        return False