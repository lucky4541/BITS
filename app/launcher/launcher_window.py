"""BITS Tool launch screen / feature hub.

Presentation-only launcher redesign. Existing module handlers and lazy imports
are preserved; this file does not alter Zoning, Validation or Compare
Structure internals.
"""

import tkinter as tk

from gui import theme
from core import ui_prefs


APP_VERSION = "1.1.0"


class LauncherWindow:
    def __init__(self, root: tk.Tk):
        self.root = root

        prefs = ui_prefs.load_ui_prefs() or {}
        theme.current.set_mode(prefs.get("theme", "light"))
        theme.apply_ttk_style(root, theme.current.palette)
        self.p = theme.current.snapshot() 

        self.root.title("BITS Tool - PDF to BITS / JATS XML")
        self.root.geometry("960x610")
        self.root.minsize(820, 540)
        self.root.configure(bg=self.p["app_bg"])

        self._active_child = None
        self._build()

    # ------------------------------------------------------------------
    # Compact UI
    # ------------------------------------------------------------------

    def _build(self):
        self._build_header()

        main = tk.Frame(self.root, bg=self.p["app_bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=28, pady=(20, 18))

        self._build_welcome(main)
        self._build_cards(main)

        self._build_footer()

    def _build_header(self):
        p = self.p

        header = tk.Frame(
            self.root,
            bg=p["header_bg"],
            height=58,
        )
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        left = tk.Frame(header, bg=p["header_bg"])
        left.pack(side=tk.LEFT, padx=22, fill=tk.Y)

        tk.Label(
            left,
            text="◈",
            bg=p["header_bg"],
            fg=p["accent"],
            font=(theme.FONT_FAMILY, 20, "bold"),
        ).pack(side=tk.LEFT, pady=13, padx=(0, 8))

        tk.Label(
            left,
            text="BITS Tool",
            bg=p["header_bg"],
            fg=p["header_fg"],
            font=(theme.FONT_FAMILY, 17, "bold"),
        ).pack(side=tk.LEFT, pady=11)

        tk.Label(
            left,
            text="  PDF to BITS / JATS XML",
            bg=p["header_bg"],
            fg=p["header_fg_muted"],
            font=theme.FONT_SMALL,
        ).pack(side=tk.LEFT, pady=17)

        tk.Label(
            header,
            text=f"V{APP_VERSION}",
            bg=p["header_bg"],
            fg=p["header_fg_muted"],
            font=theme.FONT_SMALL,
        ).pack(side=tk.RIGHT, padx=22)

    def _build_welcome(self, parent):
        p = self.p

        row = tk.Frame(parent, bg=p["app_bg"])
        row.pack(fill=tk.X, pady=(0, 15))

        left = tk.Frame(row, bg=p["app_bg"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(
            left,
            text="WORKSPACE",
            bg=p["app_bg"],
            fg=p["accent"],
            font=theme.FONT_SMALL_BOLD,
        ).pack(anchor="w")

        tk.Label(
            left,
            text="Choose a tool",
            bg=p["app_bg"],
            fg=p["text"],
            font=(theme.FONT_FAMILY, 23, "bold"),
        ).pack(anchor="w", pady=(1, 2))

        tk.Label(
            left,
            text="Select a production module to continue.",
            bg=p["app_bg"],
            fg=p["text_muted"],
            font=theme.FONT_BODY,
        ).pack(anchor="w")

        tk.Label(
            row,
            text="●  OFFLINE",
            bg=p["app_bg"],
            fg=p["success"],
            font=theme.FONT_SMALL_BOLD,
        ).pack(side=tk.RIGHT, anchor="s", pady=(0, 5))

    def _build_cards(self, parent):
        p = self.p

        grid = tk.Frame(parent, bg=p["app_bg"])
        grid.pack(fill=tk.BOTH, expand=True)

        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)
        grid.grid_columnconfigure(2, weight=1)
        grid.grid_rowconfigure(0, weight=1)
        grid.grid_rowconfigure(1, weight=1)

        cards = [
            (
                "01",
                "ZONING & TAGGING",
                "Zone and tag PDFs, then generate\nBITS 2.2 / JATS 1.4 XML.",
                "READY",
                p["accent"],
                "Open",
                self._open_zoning,
            ),
            (
                "02",
                "XML VALIDATION",
                "Validate BITS / JATS XML against\nthe DTD and auto-fix safely.",
                "READY",
                p["warning"],
                "Open",
                self._open_validation,
            ),
            (
                "03",
                "PDF \u2194 XML COMPARE",
                "Compare the source PDF with the\ngenerated XML, word by word.",
                "READY",
                p["success"],
                "Open",
                self._open_comparison,
            ),
        ]

        for i, card_data in enumerate(cards):
            r, c = divmod(i, 3)
            self._make_card(grid, r, c, *card_data)

    def _make_card(
        self,
        parent,
        row,
        col,
        number,
        title,
        description,
        status,
        accent,
        button_text,
        command,
    ):
        p = self.p

        card = tk.Frame(
            parent,
            bg=p["surface"],
            highlightbackground=p["border"],
            highlightthickness=1,
            bd=0,
        )
        card.grid(
            row=row,
            column=col,
            sticky="nsew",
            padx=7,
            pady=7,
        )

        # Compact left accent strip.
        strip = tk.Frame(card, bg=accent, width=4)
        strip.pack(side=tk.LEFT, fill=tk.Y)
        strip.pack_propagate(False)

        content = tk.Frame(card, bg=p["surface"])
        content.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
            padx=15,
            pady=13,
        )

        top = tk.Frame(content, bg=p["surface"])
        top.pack(fill=tk.X)

        tk.Label(
            top,
            text=number,
            bg=p["surface"],
            fg=p["text_faint"],
            font=theme.FONT_SMALL_BOLD,
        ).pack(side=tk.LEFT)

        tk.Label(
            top,
            text=f"● {status}",
            bg=p["surface"],
            fg=accent,
            font=theme.FONT_SMALL_BOLD,
        ).pack(side=tk.RIGHT)

        tk.Label(
            content,
            text=title,
            bg=p["surface"],
            fg=p["text"],
            font=(theme.FONT_FAMILY, 14, "bold"),
        ).pack(anchor="w", pady=(8, 3))

        tk.Label(
            content,
            text=description,
            bg=p["surface"],
            fg=p["text_muted"],
            font=theme.FONT_SMALL,
            justify=tk.LEFT,
            anchor="w",
        ).pack(anchor="w", fill=tk.X)

        button = tk.Button(
            content,
            text=f"{button_text}  →",
            command=command,
            bg=accent,
            fg=p["accent_fg"],
            activebackground=p["accent_hover"],
            activeforeground=p["accent_fg"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            font=theme.FONT_SMALL_BOLD,
            cursor="hand2",
            padx=12,
            pady=6,
        )
        button.pack(anchor="w", pady=(10, 0))

        def enter(_event):
            card.configure(
                highlightbackground=accent,
                highlightthickness=2,
            )

        def leave(_event):
            card.configure(
                highlightbackground=p["border"],
                highlightthickness=1,
            )

        for widget in (card, strip, content, top):
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)

    def _build_footer(self):
        p = self.p

        footer = tk.Frame(
            self.root,
            bg=p["panel_bg"],
            height=30,
        )
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        footer.pack_propagate(False)

        tk.Label(
            footer,
            text="BITS 2.2 / JATS 1.4",
            bg=p["panel_bg"],
            fg=p["text_muted"],
            font=theme.FONT_SMALL,
        ).pack(side=tk.LEFT, padx=16)

        tk.Label(
            footer,
            text="All Rights Reserved @ Syncronic IT Solutions Pvt. Ltd.",
            bg=p["panel_bg"],
            fg=p["text_faint"],
            font=theme.FONT_SMALL,
        ).pack(side=tk.RIGHT, padx=16)

    # ------------------------------------------------------------------
    # Existing navigation behavior — unchanged
    # ------------------------------------------------------------------

    def _on_child_home(self):
        self._active_child = None
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _open_zoning(self):
        from gui.main_window import App

        self.root.withdraw()

        toplevel = tk.Toplevel(self.root)
        toplevel.title("BITS Tool - Zoning & Tagging")
        toplevel.geometry("1400x900")
        toplevel.minsize(1000, 700)

        self._active_child = App(
            toplevel,
            on_home=self._on_child_home,
        )

        toplevel.protocol(
            "WM_DELETE_WINDOW",
            self._active_child._go_home,
        )

    def _open_validation(self):
        from app.xml_validation import xml_validation_window

        self.root.withdraw()
        self._active_child = xml_validation_window.open_window(
            self.root,
            self._on_child_home,
        )

    def _open_comparison(self):
        from app.comparison import comparison_window

        self.root.withdraw()
        self._active_child = comparison_window.open_window(
            self.root,
            self._on_child_home,
        )


def run():
    root = tk.Tk()
    LauncherWindow(root)
    root.mainloop()


if __name__ == "__main__":
    run()
