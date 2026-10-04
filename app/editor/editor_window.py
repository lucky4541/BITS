"""EDITOR module (spec: "EPUBForge - PHASE 4 - Real EPUB Editor", extended
by "EPUB VALIDATION MODULE - PROFESSIONAL XHTML/EPUB EDITOR" into a real
multi-tab code editor). A real (never mock) text editor over an EPUB's own
actual files: a categorized Package Tree (META-INF / OPF / NAV / NCX /
XHTML / CSS / Images / Fonts / Other - core.epub.package_tree, GUI-free and
directly testable) beside a tabbed text editor area, plus an embedded
results panel that runs the SAME validators Phase 1-3 already built
(core.epub.validation_runner.run_validation - "Do not create another
validator").

Editing model: the EPUB is loaded once into a core.epub.package_builder.
MutablePackage kept entirely in memory - the ORIGINAL FILE IS NEVER OPENED
FOR WRITING (spec: "Never directly overwrite original EPUB. Use temporary
working package."). Ctrl+S ("Save") commits the ACTIVE tab's buffer into
that in-memory package (clearing its own dirty flag) - there is no
meaningful "write this one XHTML file to disk" operation in a zip-based
EPUB, so "Save" here means "commit to the canonical in-memory package",
exactly mirroring how Validate/Save EPUB As already worked before this
pass (both used to call a single _commit_current_file()). "Save EPUB As"
(was "Save As...") always writes a backup of the pristine original before
writing the new file.

Tabs (spec section 3): each open file gets its OWN tk.Text widget (a
_EditorTab instance, kept alive for as long as its tab stays open) - Tk
gives every Text widget its own independent undo stack for free, so
"each tab must maintain cursor position, selection, undo history" falls
out of this design rather than needing custom undo-stack bookkeeping.

Error navigation (spec section "ERROR NAVIGATION"): selecting a row in the
embedded results table opens/activates the affected file's tab, jumps to
the exact line/column, and highlights it; for link-shaped findings (core.
epub.link_inspector.LINK_CATEGORIES) a SOURCE/TARGET bar is also
populated. Go To Line (Ctrl+G) accepts the same "line" or "line:column"
form EPUBCheck messages use (e.g. "RSC-005:484:78").

Lazy imports throughout (core.epub is only imported once this window
actually opens), matching every other app/*/*.py window in this codebase.
Nothing in this file touches gui/, core/zone_manager.py, auto_zoning/, or
any other zoning-related module.

Deliberately NOT implemented in this pass (disclosed, not silently
dropped): code folding, a dedicated crash-recovery-file mechanism, external-file-change
polling, and a visual before/after diff for Auto-Fix. Find/Replace/Regex/Go To Line/Save/Save
As/Save All/Undo/Redo - the operations spec section 74 calls out as most
important - are fully implemented."""
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
import zipfile
import threading
import html as _html
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from gui import theme

STATE_READY = "READY"
STATE_OPENING = "OPENING"
STATE_VALIDATING = "VALIDATING"
STATE_COMPLETE = "COMPLETE"
STATE_FAILED = "FAILED"

# ---------------------------------------------------------------- syntax highlighting
# Regex-based, not a real parser - deliberately conservative (spec section
# 34: "Do NOT remove or rewrite namespaces during formatting" - this only
# ever ADDS color tags, never touches buffer text). Applied debounced
# (spec section 57: never re-highlight synchronously on every keystroke).
_XML_TAG_RE = re.compile(r"</?[A-Za-z][\w:.\-]*")
_XML_ATTR_RE = re.compile(r'([\w:.\-]+)(=)("[^"]*"|\'[^\']*\')')
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_XML_ENTITY_RE = re.compile(r"&(?:#x?[0-9A-Fa-f]+|[A-Za-z][\w]*);")
_XML_DECL_RE = re.compile(r"<\?.*?\?>|<!DOCTYPE.*?>", re.DOTALL | re.IGNORECASE)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_SELECTOR_RE = re.compile(r"([^{}]+)\{", re.MULTILINE)
_CSS_PROPERTY_RE = re.compile(r"([\w\-]+)\s*(:)\s*([^;{}]+)(;|(?=\}))")
_XML_LIKE_EXT = {".xhtml", ".xml", ".opf", ".ncx", ".html", ".htm"}

# VS Code-style XML/XHTML tag completion.  Kept centralized and local to the
# editor; it does not alter the saved package until the normal Save/Auto Save
# path commits the Text widget contents.
_XML_TAG_COMPLETIONS = (
    "a", "abbr", "address", "article", "aside", "audio", "b", "bdi", "bdo",
    "blockquote", "body", "br", "button", "caption", "cite", "code", "col",
    "colgroup", "dd", "del", "div", "dl", "dt", "em", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hr", "html", "i", "iframe", "img", "input", "ins", "kbd", "label",
    "li", "link", "main", "mark", "meta", "nav", "ol", "p", "pre", "q",
    "rp", "rt", "ruby", "s", "script", "section", "select", "small", "span",
    "strong", "style", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "title", "tr", "u", "ul", "var", "video", "wbr",
    # Common EPUB/CSS/XML names.
    "package", "metadata", "manifest", "item", "spine", "itemref",
    "guide", "reference", "nav", "epub", "dc:title", "dc:creator",
    "dc:language", "dc:identifier", "ol", "li", "landmarks", "toc",
)

_XML_VOID_COMPLETIONS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# XML/XHTML structural checking. This is deliberately lightweight and
# non-destructive: it only reports/highlights structural tag problems and
# never rewrites the editor buffer.
_XML_TAG_TOKEN_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE.*?>|"
    r"</?\s*[A-Za-z][\w:.\-]*(?:\s+[^<>]*?)?\s*/?>",
    re.DOTALL | re.IGNORECASE,
)
_XML_TAG_NAME_RE = re.compile(r"^<\s*(/?)\s*([A-Za-z][\w:.\-]*)")
_XML_HTML_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

def _xml_tag_structure_issues(content: str):
    """Return structural XML/XHTML tag issues as (start, end, kind, message).

    The checker is intentionally conservative. It ignores comments, CDATA,
    declarations and processing instructions, supports namespaces, accepts
    explicit self-closing tags, and treats standard HTML void elements as
    self-closing for .html/.htm-style content.
    """
    stack = []
    issues = []

    for m in _XML_TAG_TOKEN_RE.finditer(content):
        token = m.group(0)
        if token.startswith(("<!--", "<![CDATA[", "<?", "<!DOCTYPE", "<!doctype")):
            continue

        nm = _XML_TAG_NAME_RE.match(token)
        if not nm:
            continue

        closing = bool(nm.group(1))
        name = nm.group(2)
        lname = name.lower()
        self_closing = token.rstrip().endswith("/>") or lname in _XML_HTML_VOID

        if closing:
            if not stack:
                issues.append((m.start(), m.end(), "unexpected_closing",
                                f"Unexpected closing tag </{name}>"))
                continue

            # Normal LIFO match.
            if stack[-1][0] == lname:
                stack.pop()
                continue

            # If the requested closing tag exists deeper in the stack, all
            # tags above it are missing their closing tags. Highlight those
            # opening tags rather than incorrectly pairing them.
            found = None
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == lname:
                    found = idx
                    break

            if found is None:
                issues.append((m.start(), m.end(), "unexpected_closing",
                                f"Unexpected closing tag </{name}>"))
            else:
                for bad_name, bad_start, bad_end in stack[found + 1:]:
                    issues.append((
                        bad_start, bad_end, "missing_closing",
                        f"Missing closing tag </{bad_name}> before </{name}>"
                    ))
                del stack[found:]
        elif not self_closing:
            stack.append((lname, m.start(), m.end()))

    for lname, start, end in stack:
        issues.append((
            start, end, "missing_closing",
            f"Missing closing tag </{lname}>"
        ))

    return issues


def _highlight_xml(text_widget):
    content = text_widget.get("1.0", "end-1c")
    for tag in ("xml_tag", "xml_attr_name", "xml_attr_value", "xml_comment", "xml_entity", "xml_decl"):
        text_widget.tag_remove(tag, "1.0", "end")
    for m in _XML_COMMENT_RE.finditer(content):
        text_widget.tag_add("xml_comment", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
    for m in _XML_DECL_RE.finditer(content):
        text_widget.tag_add("xml_decl", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
    for m in _XML_TAG_RE.finditer(content):
        text_widget.tag_add("xml_tag", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
    for m in _XML_ATTR_RE.finditer(content):
        text_widget.tag_add("xml_attr_name", f"1.0+{m.start(1)}c", f"1.0+{m.end(1)}c")
        text_widget.tag_add("xml_attr_value", f"1.0+{m.start(3)}c", f"1.0+{m.end(3)}c")
    for m in _XML_ENTITY_RE.finditer(content):
        text_widget.tag_add("xml_entity", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
    # Comments/declarations must win over a generic tag-name match that
    # happens to fall inside one (e.g. "<" inside a comment) - reapplying
    # them last, on top, is simplest and correct given Tk's own
    # highest-tag-priority-wins rendering (raised after being (re)added).
    text_widget.tag_raise("xml_comment")
    text_widget.tag_raise("xml_decl")


def _highlight_css(text_widget):
    content = text_widget.get("1.0", "end-1c")
    for tag in ("css_comment", "css_selector", "css_property", "css_value"):
        text_widget.tag_remove(tag, "1.0", "end")
    for m in _CSS_COMMENT_RE.finditer(content):
        text_widget.tag_add("css_comment", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
    for m in _CSS_SELECTOR_RE.finditer(content):
        text_widget.tag_add("css_selector", f"1.0+{m.start(1)}c", f"1.0+{m.end(1)}c")
    for m in _CSS_PROPERTY_RE.finditer(content):
        text_widget.tag_add("css_property", f"1.0+{m.start(1)}c", f"1.0+{m.end(1)}c")
        text_widget.tag_add("css_value", f"1.0+{m.start(3)}c", f"1.0+{m.end(3)}c")
    text_widget.tag_raise("css_comment")


def _pretty_print_xml(source: str) -> str:
    """Reindents an XML/XHTML/OPF/NCX buffer using lxml, preserving the
    original XML declaration/DOCTYPE presence exactly. `remove_blank_text`
    only discards text nodes that are ENTIRELY whitespace before adding
    fresh indentation whitespace back in - it never touches a text node
    that has real content (spec: "Do NOT remove or rewrite... during
    formatting" / "whitespace collapsing" is only ever applied here to
    whitespace that was already insignificant). The caller is still
    required to verify visible text is unchanged before accepting this
    output (see _format_current below) - this function reformats, it does
    not itself guarantee safety."""
    from lxml import etree
    try:
        parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
        tree = etree.fromstring(source.encode("utf-8"), parser=parser).getroottree()
    except etree.XMLSyntaxError as e:
        raise ValueError(f"not well-formed XML ({e}).")
    docinfo = tree.docinfo
    encoding = docinfo.encoding or "UTF-8"
    formatted = etree.tostring(
        tree, pretty_print=True, xml_declaration=bool(docinfo.xml_version),
        encoding=encoding, doctype=docinfo.doctype or None)
    return formatted.decode(encoding)


_CSS_TOKEN_RE = re.compile(r"/\*.*?\*/|[{};]", re.DOTALL)


def _pretty_print_css(source: str) -> str:
    """A conservative, non-parser CSS reformatter (matches this file's own
    established 'regex-based, not a real parser' approach to CSS - see
    _highlight_css above): it only ever inserts indentation/newlines around
    the structural characters { } ; and never touches any other character,
    so it cannot itself corrupt a value/selector/comment. _format_current's
    whitespace-stripped equality check is still the authority that decides
    whether the result is safe to accept."""
    out = []
    indent = 0
    buf = ""
    i, n = 0, len(source)
    while i < n:
        m = _CSS_TOKEN_RE.match(source, i)
        if not m:
            buf += source[i]
            i += 1
            continue
        token = m.group(0)
        if token.startswith("/*"):
            buf += token
            i = m.end()
            continue
        if token == "{":
            head = buf.strip()
            out.append(("    " * indent) + (head + " {" if head else "{"))
            buf = ""
            indent += 1
        elif token == "}":
            if buf.strip():
                out.append(("    " * indent) + buf.strip())
            indent = max(0, indent - 1)
            out.append(("    " * indent) + "}")
            buf = ""
        else:  # ";"
            buf += ";"
            out.append(("    " * indent) + buf.strip())
            buf = ""
        i = m.end()
    if buf.strip():
        out.append(("    " * indent) + buf.strip())
    return "\n".join(out) + "\n"


def _configure_highlight_tags(text_widget, palette):
    text_widget.tag_configure("xml_tag", foreground="#3B82C4")
    text_widget.tag_configure("xml_attr_name", foreground="#B5731E")
    text_widget.tag_configure("xml_attr_value", foreground="#3E8E52")
    text_widget.tag_configure("xml_comment", foreground="#888888")
    text_widget.tag_configure("xml_entity", foreground="#B5507A")
    text_widget.tag_configure("xml_decl", foreground="#7E57C2")
    text_widget.tag_configure("css_comment", foreground="#888888")
    text_widget.tag_configure("css_selector", foreground="#3B82C4")
    text_widget.tag_configure("css_property", foreground="#B5731E")
    text_widget.tag_configure("css_value", foreground="#3E8E52")
    text_widget.tag_configure("current_line", background=palette["hover_bg"])
    text_widget.tag_configure("find_match", background=palette["accent_tint"])
    text_widget.tag_configure("find_current", background=palette["accent"], foreground=palette["accent_fg"])
    text_widget.tag_configure("error_highlight", background=palette["warning"], foreground=palette["app_bg"])
    # Structural XML/XHTML diagnostics. These are intentionally separate from
    # validation-result highlighting so the editor can flag an unfinished tag
    # immediately, before the user runs EPUBCheck.
    text_widget.tag_configure("missing_tag", underline=True, foreground=palette["error"])
    text_widget.tag_configure("unexpected_tag", underline=True, foreground=palette["error"])
    text_widget.tag_configure("tag_match", background=palette["accent_tint"])
    text_widget.tag_lower("current_line")


# ---------------------------------------------------------------- one open tab
class _EditorTab:
    """One open file's complete editor state (spec section 3) - its own
    tk.Text widget (own undo stack, cursor, selection - Tk gives this for
    free per widget), its own line-number gutter kept in sync, and its own
    debounced syntax-highlight scheduling. Created once per file the first
    time it's opened (spec section 58: "Load files when opened", never all
    at startup) and kept alive - never destroyed - for as long as its tab
    stays open, so switching away and back preserves everything exactly."""

    HIGHLIGHT_DEBOUNCE_MS = 400

    def __init__(self, container, palette, name: str, text_content: str, on_modified):
        self.name = name
        self.palette = palette
        self.on_modified = on_modified
        self._highlight_after_id = None
        self._structure_after_id = None
        self._completion_window = None
        self._completion_list = None
        self._completion_items = []

        self.frame = tk.Frame(container, bg=palette["app_bg"])
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(1, weight=1)

        self.linenumbers = tk.Text(self.frame, width=5, padx=4, pady=4, takefocus=0, border=0,
                                    background=palette["panel_header_bg"], foreground=palette["text_muted"],
                                    font=theme.FONT_MONO_SMALL, state="disabled", wrap="none")
        self.linenumbers.grid(row=0, column=0, sticky="ns")

        self.text = tk.Text(self.frame, wrap="none", undo=True, maxundo=-1, autoseparators=True,
                             font=theme.FONT_MONO_SMALL, bg=palette["surface"], fg=palette["text"],
                             insertbackground=palette["text"], padx=4, pady=4)
        self.text.grid(row=0, column=1, sticky="nsew")
        vsb = ttk.Scrollbar(self.frame, orient="vertical", command=self._yview_both)
        vsb.grid(row=0, column=2, sticky="ns")
        hsb = ttk.Scrollbar(self.frame, orient="horizontal", command=self.text.xview)
        hsb.grid(row=1, column=1, sticky="ew")
        self.text.configure(yscrollcommand=self._make_yscroll_sync(vsb), xscrollcommand=hsb.set)

        _configure_highlight_tags(self.text, palette)

        self.text.insert("1.0", text_content)
        # edit_reset() clears the undo/redo stacks that the initial load
        # insert above just pushed onto them (undo=True is active from the
        # moment the widget is constructed) - without this, a user's very
        # first Ctrl+Z could undo the file OPEN itself back to an empty
        # buffer, which is never a sensible "undo" target (confirmed as a
        # real bug during this feature's own test development: repeated
        # undo/redo cycles on a freshly opened file emptied it entirely).
        # Undo history should only ever cover the user's OWN edits.
        self.text.edit_reset()
        self.loaded_text = text_content
        self.is_dirty = False
        self.text.edit_modified(False)

        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<KeyRelease>", lambda e: (self._update_line_numbers(), self._highlight_current_line()))
        self.text.bind("<ButtonRelease-1>", lambda e: self._highlight_current_line())

        # Editor conveniences: indentation, duplicate/delete line, comment
        # toggle and XML tag auto-completion are all local Text-widget actions.
        self.text.bind("<Tab>", self._indent_selection)
        self.text.bind("<Shift-Tab>", self._unindent_selection)
        self.text.bind("<Control-d>", self._duplicate_line)
        self.text.bind("<Control-D>", self._duplicate_line)
        self.text.bind("<Control-Shift-k>", self._delete_line)
        self.text.bind("<Control-Shift-K>", self._delete_line)
        self.text.bind("<Control-slash>", self._toggle_comment)
        self.text.bind(">", self._maybe_auto_close_tag)

        # VS Code-style XML/XHTML tag completion.
        self.text.bind("<Control-space>", self._show_tag_completion)
        self.text.bind("<KeyRelease>", self._completion_key_release, add="+")
        self.text.bind("<Escape>", self._close_tag_completion, add="+")
        self.text.bind("<Return>", self._accept_tag_completion, add="+")
        self.text.bind("<Tab>", self._accept_tag_completion, add="+")
        self.text.bind("<Up>", self._completion_up, add="+")
        self.text.bind("<Down>", self._completion_down, add="+")
        self.text.bind("<KeyPress>", self._completion_keypress, add="+")

        self._update_line_numbers()
        self._run_highlight()
        self._run_structure_check()

    # ---- editor conveniences ----
    def _selected_line_range(self):
        try:
            start = self.text.index("sel.first linestart")
            end = self.text.index("sel.last lineend +1c")
            return start, end
        except tk.TclError:
            line = self.text.index("insert").split(".")[0]
            return f"{line}.0", f"{line}.end +1c"

    def _indent_selection(self, _event=None):
        start, end = self._selected_line_range()
        start_line = int(self.text.index(start).split(".")[0])
        end_line = int(self.text.index(end).split(".")[0])
        self.text.edit_separator()
        for line in range(start_line, end_line + 1):
            self.text.insert(f"{line}.0", "    ")
        self.text.edit_separator()
        return "break"

    def _unindent_selection(self, _event=None):
        start, end = self._selected_line_range()
        start_line = int(self.text.index(start).split(".")[0])
        end_line = int(self.text.index(end).split(".")[0])
        self.text.edit_separator()
        for line in range(start_line, end_line + 1):
            prefix = self.text.get(f"{line}.0", f"{line}.0+4c")
            if prefix.startswith("    "):
                self.text.delete(f"{line}.0", f"{line}.0+4c")
            else:
                prefix2 = self.text.get(f"{line}.0", f"{line}.0+1c")
                if prefix2 in ("\\t", " "):
                    self.text.delete(f"{line}.0", f"{line}.0+1c")
        self.text.edit_separator()
        return "break"

    def _duplicate_line(self, _event=None):
        line = self.text.index("insert").split(".")[0]
        content = self.text.get(f"{line}.0", f"{line}.end")
        self.text.edit_separator()
        self.text.insert(f"{line}.end", "\n" + content)
        self.text.mark_set("insert", f"{int(line)+1}.{self.text.index(f'{line}.end').split('.')[1]}")
        self.text.edit_separator()
        return "break"

    def _delete_line(self, _event=None):
        line = int(self.text.index("insert").split(".")[0])
        last_line = int(self.text.index("end-1c").split(".")[0])
        self.text.edit_separator()
        if line < last_line:
            self.text.delete(f"{line}.0", f"{line+1}.0")
        else:
            self.text.delete(f"{line}.0", f"{line}.end")
        self.text.edit_separator()
        return "break"

    def _toggle_comment(self, _event=None):
        ext = self._ext()
        line = int(self.text.index("insert").split(".")[0])
        start, end = self._selected_line_range()
        start_line = int(self.text.index(start).split(".")[0])
        end_line = int(self.text.index(end).split(".")[0])

        is_xml = ext in _XML_LIKE_EXT
        is_css = ext == ".css"
        if not (is_xml or is_css):
            return "break"

        marker = "<!--" if is_xml else "/*"
        closing = "-->" if is_xml else "*/"

        self.text.edit_separator()
        lines = [
            self.text.get(f"{n}.0", f"{n}.end")
            for n in range(start_line, end_line + 1)
        ]
        nonempty = [x for x in lines if x.strip()]
        already = bool(nonempty) and all(
            x.lstrip().startswith(marker) and x.rstrip().endswith(closing)
            for x in nonempty
        )
        for n, value in zip(range(start_line, end_line + 1), lines):
            if not value.strip():
                continue
            leading = value[:len(value) - len(value.lstrip())]
            body = value[len(leading):]
            if already:
                if body.startswith(marker):
                    body = body[len(marker):].lstrip()
                if body.endswith(closing):
                    body = body[:-len(closing)].rstrip()
                new_value = leading + body
            else:
                new_value = leading + marker + " " + body + " " + closing
            self.text.delete(f"{n}.0", f"{n}.end")
            self.text.insert(f"{n}.0", new_value)
        self.text.edit_separator()
        return "break"

    def _maybe_auto_close_tag(self, _event=None):
        """After typing > for a normal opening XML tag, insert its closing tag.

        The behavior is deliberately conservative: only a just-completed
        opening tag on the current line is considered, never declarations,
        comments, self-closing tags or tags that already have an immediate
        closing partner.
        """
        self.text.insert("insert", ">")
        ext = self._ext()
        if ext not in _XML_LIKE_EXT:
            return "break"

        before = self.text.get("insert-1c linestart", "insert")
        m = re.search(r"<([A-Za-z][\w:.\-]*)(?:\s[^<>]*?)?$", before)
        if not m:
            return "break"

        tag_name = m.group(1)
        if tag_name.lower() in _XML_HTML_VOID:
            return "break"

        line = self.text.index("insert").split(".")[0]
        after = self.text.get("insert", f"{line}.end")
        if after.lstrip().startswith(f"</{tag_name}>"):
            return "break"

        # Put the cursor between the newly-created pair.
        self.text.insert("insert", f"</{tag_name}>")
        self.text.mark_set("insert", "insert-1c")
        self._update_line_numbers()
        self.schedule_highlight()
        self.schedule_structure_check()
        return "break"

    # ---- XML/XHTML tag completion ----
    def _completion_prefix(self):
        """Return (prefix, closing, start_index) for the tag being typed."""
        if self._ext() not in _XML_LIKE_EXT:
            return "", False, None
        pos = self.text.index("insert")
        line_start = f"{pos} linestart"
        before = self.text.get(line_start, pos)
        # Completion only occurs inside a tag name after '<' or '</'.
        m = re.search(r"<\s*(/?)\s*([A-Za-z][\w:.\-]*)?$", before)
        if not m:
            return "", False, None
        closing = bool(m.group(1))
        prefix = m.group(2) or ""
        start_col = len(before) - len(prefix)
        return prefix, closing, f"{line_start}+{start_col}c"

    def _completion_candidates(self, prefix, closing):
        pool = _XML_TAG_COMPLETIONS
        if closing:
            # Prefer actual currently-open tags so closing completion is
            # structurally useful rather than just a static tag list.
            open_tags = []
            try:
                content = self.current_text()
                stack = []
                for m in _XML_TAG_TOKEN_RE.finditer(content):
                    token = m.group(0)
                    if token.startswith(("<!--", "<![CDATA[", "<?", "<!DOCTYPE", "<!doctype")):
                        continue
                    nm = _XML_TAG_NAME_RE.match(token)
                    if not nm:
                        continue
                    is_close = bool(nm.group(1))
                    name = nm.group(2)
                    lname = name.lower()
                    self_close = token.rstrip().endswith("/>") or lname in _XML_HTML_VOID
                    if is_close:
                        for i in range(len(stack) - 1, -1, -1):
                            if stack[i].lower() == lname:
                                del stack[i:]
                                break
                    elif not self_close:
                        stack.append(name)
                open_tags = list(reversed(stack))
            except Exception:
                open_tags = []
            pool = tuple(dict.fromkeys(open_tags + list(_XML_TAG_COMPLETIONS)))
        p = prefix.lower()
        return [x for x in pool if x.lower().startswith(p)][:30]

    def _show_tag_completion(self, _event=None):
        prefix, closing, start = self._completion_prefix()
        if start is None:
            return "break"
        candidates = self._completion_candidates(prefix, closing)
        if not candidates:
            self._close_tag_completion()
            return "break"

        self._completion_items = candidates
        if self._completion_window is None or not self._completion_window.winfo_exists():
            popup = tk.Toplevel(self.text)
            popup.wm_overrideredirect(True)
            popup.transient(self.text.winfo_toplevel())
            popup.configure(bg=self.palette["border"])
            frame = tk.Frame(popup, bg=self.palette["surface"], bd=1, relief="solid")
            frame.pack(fill=tk.BOTH, expand=True)
            lb = tk.Listbox(
                frame, height=min(10, max(2, len(candidates))),
                width=max(20, min(34, max(map(len, candidates)) + 4)),
                activestyle="none", exportselection=False,
                bg=self.palette["surface"], fg=self.palette["text"],
                selectbackground=self.palette["accent"],
                selectforeground=self.palette["accent_fg"],
                relief="flat", borderwidth=0,
                font=theme.FONT_MONO_SMALL,
            )
            lb.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            lb.bind("<Double-Button-1>", self._accept_tag_completion)
            lb.bind("<Return>", self._accept_tag_completion)
            lb.bind("<Escape>", self._close_tag_completion)
            self._completion_window = popup
            self._completion_list = lb

        lb = self._completion_list
        lb.delete(0, "end")
        for item in candidates:
            lb.insert("end", item)
        lb.selection_set(0)
        lb.activate(0)

        # Position just below the insertion cursor.
        try:
            x, y, _, h = self.text.bbox("insert")
            root_x = self.text.winfo_rootx() + x
            root_y = self.text.winfo_rooty() + y + h + 2
            self._completion_window.geometry(f"+{root_x}+{root_y}")
        except Exception:
            pass
        self._completion_window.deiconify()
        self._completion_window.lift()
        return "break"

    def _completion_keypress(self, event):
        """Route navigation/selection keys to the completion popup."""
        if not self._completion_window or not self._completion_window.winfo_exists():
            return
        if event.keysym in ("Up", "Down", "Return", "Tab", "Escape"):
            return "break"
        return None

    def _completion_up(self, _event=None):
        if self._completion_window and self._completion_window.winfo_exists():
            lb = self._completion_list
            if lb and lb.size():
                cur = lb.curselection()[0] if lb.curselection() else 0
                lb.selection_clear(0, "end")
                lb.selection_set(max(0, cur - 1))
                lb.activate(max(0, cur - 1))
            return "break"

    def _completion_down(self, _event=None):
        if self._completion_window and self._completion_window.winfo_exists():
            lb = self._completion_list
            if lb and lb.size():
                cur = lb.curselection()[0] if lb.curselection() else -1
                nxt = min(lb.size() - 1, cur + 1)
                lb.selection_clear(0, "end")
                lb.selection_set(nxt)
                lb.activate(nxt)
            return "break"

    def _accept_tag_completion(self, _event=None):
        if not self._completion_window or not self._completion_window.winfo_exists():
            return None
        lb = self._completion_list
        if lb is None or not lb.curselection():
            return "break"
        name = lb.get(lb.curselection()[0])
        prefix, closing, start = self._completion_prefix()
        if start is None:
            self._close_tag_completion()
            return "break"

        # Replace only the partially typed tag name.
        self.text.delete(start, "insert")
        if closing:
            self.text.insert("insert", name + ">")
        elif name.lower() in _XML_VOID_COMPLETIONS:
            self.text.insert("insert", name + " />")
        else:
            self.text.insert("insert", name + "></" + name + ">")
            self.text.mark_set("insert", "insert-" + str(len(name) + 3) + "c")

        self._close_tag_completion()
        self._update_line_numbers()
        self.schedule_highlight()
        self.schedule_structure_check()
        return "break"

    def _close_tag_completion(self, _event=None):
        if self._completion_window is not None:
            try:
                self._completion_window.destroy()
            except Exception:
                pass
        self._completion_window = None
        self._completion_list = None
        self._completion_items = []
        return "break"

    def _completion_key_release(self, event=None):
        if event and event.keysym in ("Return", "Tab", "Escape", "Up", "Down"):
            return
        if self._ext() not in _XML_LIKE_EXT:
            self._close_tag_completion()
            return
        # Only show automatically while the cursor is inside a tag name.
        prefix, closing, start = self._completion_prefix()
        if start is None:
            self._close_tag_completion()
            return
        self._show_tag_completion()

    # ---- structural XML/XHTML diagnostics ----
    def schedule_structure_check(self):
        if self._structure_after_id is not None:
            try:
                self.text.after_cancel(self._structure_after_id)
            except Exception:
                pass
        self._structure_after_id = self.text.after(500, self._fire_structure_check)

    def _fire_structure_check(self):
        self._structure_after_id = None
        self._run_structure_check()

    def _run_structure_check(self):
        ext = self._ext()
        if ext not in _XML_LIKE_EXT:
            return
        try:
            content = self.current_text()
            self.text.tag_remove("missing_tag", "1.0", "end")
            self.text.tag_remove("unexpected_tag", "1.0", "end")
            issues = _xml_tag_structure_issues(content)
            for start, end, kind, _message in issues:
                tag_name = "missing_tag" if kind == "missing_closing" else "unexpected_tag"
                self.text.tag_add(
                    tag_name,
                    f"1.0+{start}c",
                    f"1.0+{end}c",
                )
            self._structure_issues = issues
            try:
                self.text.event_generate("<<StructureChecked>>", when="tail")
            except tk.TclError:
                pass
        except tk.TclError:
            pass

    # ---- scrolling ----
    def _yview_both(self, *args):
        self.text.yview(*args)
        self.linenumbers.yview(*args)

    def _make_yscroll_sync(self, vsb):
        def cb(first, last):
            vsb.set(first, last)
            self.linenumbers.yview_moveto(first)
        return cb

    # ---- line numbers ----
    def _update_line_numbers(self):
        n_lines = int(self.text.index("end-1c").split(".")[0])
        current = self.linenumbers.get("1.0", "end-1c").count("\n") + 1
        if current == n_lines:
            return  # avoid a full gutter rebuild on every keystroke when line count hasn't changed
        self.linenumbers.config(state="normal")
        self.linenumbers.delete("1.0", "end")
        self.linenumbers.insert("1.0", "\n".join(str(i) for i in range(1, n_lines + 1)))
        self.linenumbers.config(state="disabled")
        self.linenumbers.yview_moveto(self.text.yview()[0])

    def _highlight_current_line(self):
        self.text.tag_remove("current_line", "1.0", "end")
        line = self.text.index("insert").split(".")[0]
        self.text.tag_add("current_line", f"{line}.0", f"{line}.0+1line")

    # ---- syntax highlighting (debounced - spec section 57) ----
    def _ext(self):
        return posixpath.splitext(self.name)[1].lower()

    def _run_highlight(self):
        ext = self._ext()
        try:
            if ext in _XML_LIKE_EXT:
                _highlight_xml(self.text)
            elif ext == ".css":
                _highlight_css(self.text)
        except tk.TclError:
            pass  # widget torn down mid-schedule (tab closed) - never fatal

    def schedule_highlight(self):
        if self._highlight_after_id is not None:
            try:
                self.text.after_cancel(self._highlight_after_id)
            except Exception:
                pass
        self._highlight_after_id = self.text.after(self.HIGHLIGHT_DEBOUNCE_MS, self._fire_highlight)

    def _fire_highlight(self):
        self._highlight_after_id = None
        self._run_highlight()

    # ---- dirty tracking ----
    def _on_text_modified(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        self._update_line_numbers()
        self.schedule_highlight()
        self.schedule_structure_check()
        content = self.text.get("1.0", "end-1c")
        was_dirty = self.is_dirty
        self.is_dirty = content != self.loaded_text
        if self.is_dirty != was_dirty:
            self.on_modified(self.name)

    def current_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def mark_saved(self):
        """Called after the buffer is committed into MutablePackage
        (spec: "Save" = commit, see module docstring) - resets the dirty
        baseline to the CURRENT buffer content, never re-reads from disk."""
        self.loaded_text = self.current_text()
        self.is_dirty = False

    def jump_to(self, line, col):
        self.text.tag_remove("error_highlight", "1.0", "end")
        if not line or line <= 0:
            return
        col_index = max(0, (col or 1) - 1)
        self.text.tag_add("error_highlight", f"{line}.0", f"{line}.end")
        self.text.see(f"{line}.0")
        self.text.mark_set("insert", f"{line}.{col_index}")
        self.text.focus_set()
        self._highlight_current_line()

    def destroy(self):
        self.frame.destroy()


def open_window(launcher_root: tk.Tk, on_home, initial_epub_path: str = None):
    from core.epub import link_inspector, package_builder, package_reader, package_tree
    from core.epub import regression_checker, validation_runner
    from core.epubcheck import runner as epubcheck_runner

    palette = theme.current.palette
    win = tk.Toplevel(launcher_root)
    win.title("EPUBForge - Editor")
    win.geometry("1320x860")
    win.configure(bg=palette["app_bg"])

    state = {
        "epub_path": None, "mp": None, "package": None,
        "tabs": {}, "tab_order": [], "active_tab": None, "tab_chip_widgets": {},
        "tree_item_to_name": {}, "name_to_tree_item": {},
        "row_details": {}, "row_analysis": {},
        "find_history": [], "replace_history": [],
        "autosave_enabled": True, "autosave_delay_ms": 1000, "autosave_after_id": None,
        "auto_fix_running": False,
        "validation_running": False,
        "last_report": None,
    }

    # ---------------- header ----------------
    header = tk.Frame(win, bg=palette["header_bg"], height=40)
    header.pack(side=tk.TOP, fill=tk.X)
    header.pack_propagate(False)
    left = tk.Frame(header, bg=palette["header_bg"])
    left.pack(side=tk.LEFT, padx=14)
    home_btn = tk.Label(left, text="← Home", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                         font=theme.FONT_BODY_BOLD, cursor="hand2", padx=6)
    home_btn.pack(side=tk.LEFT, padx=(0, 10))

    def _any_dirty_tabs():
        return [name for name, tab in state["tabs"].items() if tab.is_dirty]

    def _confirm_close_all_dirty(title="Unsaved Changes") -> bool:
        """Spec section 48: "When closing EPUB with multiple modified
        files: show affected files." Returns True if it's OK to proceed
        (nothing dirty, or the user chose Discard); False to abort the
        close entirely. There is no on-disk "save" for an individual
        package member (see module docstring) - the only real save target
        is a NEW epub via Save EPUB As, so the offered choice here is
        Save EPUB As / Discard / Cancel, not a per-file save."""
        dirty = _any_dirty_tabs()
        if not dirty:
            return True
        listed = "\n".join(f"  - {posixpath.basename(n)}" for n in dirty)
        choice = messagebox.askyesnocancel(
            title,
            f"{len(dirty)} file(s) have unsaved changes:\n\n{listed}\n\n"
            "Yes = Save EPUB As... first\nNo = Discard changes and close\nCancel = don't close",
            parent=win)
        if choice is None:
            return False
        if choice:
            return bool(_save_epub_as())
        return True

    def _go_home():
        _run_autosave()
        if not _confirm_close_all_dirty():
            return
        win.destroy()
        on_home()

    home_btn.bind("<Button-1>", lambda e: _go_home())
    win.protocol("WM_DELETE_WINDOW", _go_home)
    tk.Label(left, text="◈ EPUBForge Editor", bg=palette["header_bg"], fg=palette["header_fg"],
             font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)

    ok_avail, avail_msg = epubcheck_runner.check_availability()
    avail_text = "EPUBCheck: available" if ok_avail else "EPUBCheck: NOT AVAILABLE"
    avail_label = tk.Label(header, text=avail_text, bg=palette["header_bg"],
                            fg=palette["success"] if ok_avail else palette["error"], font=theme.FONT_SMALL_BOLD)
    avail_label.pack(side=tk.RIGHT, padx=14)
    theme.Tooltip(avail_label, avail_msg)

    # ---------------- toolbar ----------------
    toolbar = tk.Frame(win, bg=palette["toolbar_bg"], padx=12, pady=8)
    toolbar.pack(side=tk.TOP, fill=tk.X)
    open_btn = tk.Button(toolbar, text="Open EPUB...")
    theme.style_button(open_btn, palette, kind="secondary")
    open_btn.pack(side=tk.LEFT, padx=(0, 8))
    save_btn = tk.Button(toolbar, text="Save", state="disabled")
    theme.style_button(save_btn, palette, kind="secondary")
    save_btn.pack(side=tk.LEFT, padx=(0, 4))
    theme.Tooltip(save_btn, "Ctrl+S - commits the active tab's edits into the in-memory EPUB package.")
    save_all_btn = tk.Button(toolbar, text="Save All", state="disabled")
    theme.style_button(save_all_btn, palette, kind="secondary")
    save_all_btn.pack(side=tk.LEFT, padx=(0, 8))
    theme.Tooltip(save_all_btn, "Ctrl+Shift+S - commits every modified open tab.")
    save_epub_btn = tk.Button(toolbar, text="Save EPUB As...", state="disabled")
    theme.style_button(save_epub_btn, palette, kind="secondary")
    save_epub_btn.pack(side=tk.LEFT, padx=(0, 8))
    theme.Tooltip(save_epub_btn, "Writes a new .epub file. The original is never modified; "
                                  "a backup of it is created the first time you do this.")
    find_btn = tk.Button(toolbar, text="Find", state="disabled")
    theme.style_button(find_btn, palette, kind="secondary")
    find_btn.pack(side=tk.LEFT, padx=(0, 4))
    replace_btn = tk.Button(toolbar, text="Replace", state="disabled")
    theme.style_button(replace_btn, palette, kind="secondary")
    replace_btn.pack(side=tk.LEFT, padx=(0, 4))
    goto_btn = tk.Button(toolbar, text="Go To Line", state="disabled")
    theme.style_button(goto_btn, palette, kind="secondary")
    goto_btn.pack(side=tk.LEFT, padx=(0, 4))
    format_btn = tk.Button(toolbar, text="Pretty Print", state="disabled")
    theme.style_button(format_btn, palette, kind="secondary")
    format_btn.pack(side=tk.LEFT, padx=(0, 6))
    theme.Tooltip(format_btn, "Ctrl+Shift+F - reindents XML/XHTML/OPF/NCX/HTML or CSS. "
                               "Refuses to apply if the visible content would change - "
                               "only whitespace is ever touched.")
    help_btn = tk.Button(toolbar, text="Help")
    theme.style_button(help_btn, palette, kind="secondary")
    help_btn.pack(side=tk.LEFT, padx=(0, 6))
    theme.Tooltip(help_btn, "F1 - keyboard shortcuts and editor help")
    report_btn = tk.Button(toolbar, text="Report", state="disabled")
    theme.style_button(report_btn, palette, kind="secondary")
    report_btn.pack(side=tk.LEFT, padx=(0, 10))
    theme.Tooltip(report_btn, "Scan the complete EPUB for missing files, images, chapters, parts, tables, references, index links, footnotes, and broken internal links.")
    validate_btn = tk.Button(toolbar, text="Validate", state="disabled")
    theme.style_button(validate_btn, palette, kind="primary")
    validate_btn.pack(side=tk.LEFT, padx=(0, 6))
    auto_fix_btn = tk.Button(toolbar, text="Auto Fix", state="disabled")
    theme.style_button(auto_fix_btn, palette, kind="primary")
    auto_fix_btn.pack(side=tk.LEFT, padx=(0, 8))
    theme.Tooltip(auto_fix_btn, "Ctrl+Alt+F - apply only deterministic safe repairs, revalidate, and report what remains. The original EPUB is never modified.")

    # Auto Save is an explicit UI control.  It is ON by default and uses the
    # existing debounced 1-second save-to-in-memory-package mechanism.
    autosave_btn = tk.Button(toolbar, text="Auto Save: ON", state="disabled")
    theme.style_button(autosave_btn, palette, kind="secondary")
    autosave_btn.pack(side=tk.LEFT, padx=(0, 16))
    theme.Tooltip(
        autosave_btn,
        "Auto Save: ON. Changes are committed to the in-memory EPUB package "
        "after 1 second of inactivity. Ctrl+Alt+S toggles it."
    )
    # Long-running operations use a visible progress strip.  Validation and
    # Auto Fix run in worker threads so the Tk editor remains responsive.
    progress_frame = tk.Frame(win, bg=palette["surface"], padx=8, pady=3)
    progress_var = tk.StringVar(value="")
    progress_lbl = tk.Label(progress_frame, textvariable=progress_var,
                            bg=palette["surface"], fg=palette["text_muted"],
                            font=theme.FONT_SMALL, anchor="w")
    progress_lbl.pack(side=tk.LEFT, padx=(2, 8))
    progress_bar = ttk.Progressbar(progress_frame, mode="indeterminate", length=220)
    progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

    def _start_progress(message):
        progress_var.set(message)
        progress_frame.pack(side=tk.TOP, fill=tk.X)
        progress_bar.start(12)
        win.update_idletasks()

    def _stop_progress(message=""):
        progress_bar.stop()
        progress_var.set(message)
        if not message:
            progress_frame.pack_forget()
        win.update_idletasks()

    file_label = tk.Label(toolbar, text="(no EPUB open)", bg=palette["toolbar_bg"], fg=palette["text"],
                           font=theme.FONT_BODY_BOLD, anchor="w")
    file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ---------------- main content: tree | (tabs / editor / results) ----------------
    main_paned = ttk.PanedWindow(win, orient=tk.HORIZONTAL)
    main_paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    tree_frame = tk.Frame(main_paned, bg=palette["app_bg"])
    main_paned.add(tree_frame, weight=1)
    tk.Label(tree_frame, text="PACKAGE TREE", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.TOP, anchor="w", padx=8, pady=(8, 4))
    tree = ttk.Treeview(tree_frame, show="tree")
    tree_vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tree_vsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))
    tree_vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 8))

    right_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
    main_paned.add(right_paned, weight=4)

    # ---- editor area: tab bar + stacked per-tab frames ----
    editor_frame = tk.Frame(right_paned, bg=palette["app_bg"])
    right_paned.add(editor_frame, weight=3)

    tab_bar = tk.Frame(editor_frame, bg=palette["panel_header_bg"])
    tab_bar.pack(side=tk.TOP, fill=tk.X)

    info_bar = tk.Frame(editor_frame, bg=palette["surface"], padx=10, pady=4)
    info_bar.pack(side=tk.TOP, fill=tk.X)
    tk.Label(info_bar, text="Source:", bg=palette["surface"], fg=palette["text_muted"],
              font=theme.FONT_SMALL).pack(side=tk.LEFT)
    source_lbl = tk.Label(info_bar, text="—", bg=palette["surface"], fg=palette["text"], font=theme.FONT_SMALL_BOLD)
    source_lbl.pack(side=tk.LEFT, padx=(4, 20))
    tk.Label(info_bar, text="Target:", bg=palette["surface"], fg=palette["text_muted"],
              font=theme.FONT_SMALL).pack(side=tk.LEFT)
    target_lbl = tk.Label(info_bar, text="—", bg=palette["surface"], fg=palette["text"], font=theme.FONT_SMALL_BOLD)
    target_lbl.pack(side=tk.LEFT, padx=(4, 0))

    structure_lbl = tk.Label(
        info_bar, text="", bg=palette["surface"], fg=palette["error"],
        font=theme.FONT_SMALL_BOLD, anchor="w"
    )
    structure_lbl.pack(side=tk.RIGHT, padx=(10, 0))
    structure_lbl.configure(cursor="hand2")

    editor_stack = tk.Frame(editor_frame, bg=palette["app_bg"])
    editor_stack.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    editor_stack.rowconfigure(0, weight=1)
    editor_stack.columnconfigure(0, weight=1)

    empty_label = tk.Label(editor_stack, text="Select a file from the Package Tree to begin editing.",
                            bg=palette["app_bg"], fg=palette["text_faint"], font=theme.FONT_BODY)
    empty_label.grid(row=0, column=0, sticky="nsew")

    # ---- status bar (spec section 69) ----
    status_bar = tk.Frame(win, bg=palette["header_bg"])
    status_bar.pack(side=tk.BOTTOM, fill=tk.X)
    status_msg_lbl = tk.Label(status_bar, text=f"Status: {STATE_READY}", bg=palette["header_bg"],
                               fg=palette["header_fg_muted"], font=theme.FONT_SMALL, anchor="w", padx=10)
    status_msg_lbl.pack(side=tk.LEFT)
    status_pos_lbl = tk.Label(status_bar, text="", bg=palette["header_bg"],
                               fg=palette["header_fg_muted"], font=theme.FONT_SMALL, anchor="e", padx=10)
    status_pos_lbl.pack(side=tk.RIGHT)

    def _set_status(new_state: str, detail: str = ""):
        status_msg_lbl.config(text=f"Status: {new_state}" + (f" — {detail}" if detail else ""))
        win.update_idletasks()

    def _update_position_status(_event=None):
        tab = state["tabs"].get(state["active_tab"])
        if not tab:
            status_pos_lbl.config(text="")
            return
        try:
            line, col = tab.text.index("insert").split(".")
        except tk.TclError:
            return
        ext = tab._ext().lstrip(".").upper() or "TEXT"
        modified = "Modified" if tab.is_dirty else "Saved"
        issue_count = len(getattr(tab, "_structure_issues", []))
        structure_lbl.config(
            text=(f"⚠ {issue_count} tag issue(s)" if issue_count else "✓ Tags OK"),
            fg=palette["error"] if issue_count else palette["success"],
        )
        status_pos_lbl.config(text=f"Ln {line}, Col {int(col) + 1}  |  UTF-8  |  {ext}  |  {modified}")

    # ---------------- tag problem navigation ----------------
    def _tag_problem_location(tab, issue):
        """Convert the structure-checker's character offset to 1-based line/column."""
        start = issue[0]
        content = tab.current_text()
        line = content.count("\n", 0, start) + 1
        line_start = content.rfind("\n", 0, start) + 1
        col = start - line_start + 1
        return line, col

    def _jump_to_tag_issue(tab, issue):
        line, col = _tag_problem_location(tab, issue)
        tab.jump_to(line, col)
        _update_position_status()

    def _show_tag_problems(event=None):
        tab = state["tabs"].get(state["active_tab"])
        if not tab:
            return "break" if event is not None else None
        issues = list(getattr(tab, "_structure_issues", []))
        if not issues:
            return "break" if event is not None else None

        dialog = tk.Toplevel(win)
        dialog.title(f"Tag Problems — {len(issues)} issue(s)")
        dialog.geometry("820x420")
        dialog.minsize(680, 320)
        dialog.transient(win)
        dialog.configure(bg=palette["app_bg"])

        top = tk.Frame(dialog, bg=palette["app_bg"], padx=12, pady=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="Tag Problems", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_BODY_BOLD).pack(side=tk.LEFT)
        tk.Label(top, text="Double-click a problem to jump to the exact source tag.",
                 bg=palette["app_bg"], fg=palette["text_muted"], font=theme.FONT_SMALL).pack(side=tk.LEFT, padx=14)

        frame = tk.Frame(dialog, bg=palette["app_bg"], padx=12)
        frame.pack(fill=tk.BOTH, expand=True)
        cols = ("line", "column", "type", "message")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        heads = {"line":"Line", "column":"Column", "type":"Type", "message":"Problem"}
        widths = {"line":65, "column":75, "type":145, "message":480}
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w", stretch=(c == "message"))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        issue_by_iid = {}
        for issue in issues:
            line, col = _tag_problem_location(tab, issue)
            kind = issue[2]
            label = "Missing closing tag" if kind == "missing_closing" else "Unexpected closing tag"
            iid = tv.insert("", "end", values=(line, col, label, issue[3]))
            issue_by_iid[iid] = issue

        def jump(_event=None):
            iid = tv.selection()[0] if tv.selection() else None
            issue = issue_by_iid.get(iid)
            if issue is None:
                return "break"
            _jump_to_tag_issue(tab, issue)
            dialog.destroy()
            return "break"

        tv.bind("<Double-1>", jump)
        tv.bind("<Return>", jump)
        tv.focus_set()
        if issues:
            first = tv.get_children()[0]
            tv.selection_set(first)
            tv.focus(first)

        bottom = tk.Frame(dialog, bg=palette["app_bg"], padx=12, pady=10)
        bottom.pack(fill=tk.X)
        tk.Button(bottom, text="Jump to Selected", command=jump).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(bottom, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.bind("<Return>", jump)
        dialog.update_idletasks()
        x = win.winfo_rootx() + max(20, (win.winfo_width() - dialog.winfo_width()) // 2)
        y = win.winfo_rooty() + 100
        dialog.geometry(f"+{x}+{y}")
        return "break" if event is not None else None

    structure_lbl.bind("<Button-1>", _show_tag_problems)

    # ---------------- results area ----------------
    results_frame = tk.Frame(right_paned, bg=palette["app_bg"])
    right_paned.add(results_frame, weight=2)
    tk.Label(results_frame, text="VALIDATION RESULTS (Problems)", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.TOP, anchor="w", padx=8, pady=(8, 4))

    results_table_frame = tk.Frame(results_frame, bg=palette["app_bg"])
    results_table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8)
    columns = ("tool", "severity", "code", "file", "line", "col", "message")
    headings = {"tool": "Tool", "severity": "Severity", "code": "Code", "file": "File",
                "line": "Line", "col": "Column", "message": "Message"}
    widths = {"tool": 80, "severity": 75, "code": 85, "file": 200, "line": 50, "col": 55, "message": 320}
    tree_results = ttk.Treeview(results_table_frame, columns=columns, show="headings", selectmode="browse", height=8)
    for col in columns:
        tree_results.heading(col, text=headings[col])
        tree_results.column(col, width=widths[col], anchor="w", stretch=(col == "message"))
    results_vsb = ttk.Scrollbar(results_table_frame, orient="vertical", command=tree_results.yview)
    results_hsb = ttk.Scrollbar(results_table_frame, orient="horizontal", command=tree_results.xview)
    tree_results.configure(yscrollcommand=results_vsb.set, xscrollcommand=results_hsb.set)
    tree_results.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    results_hsb.pack(side=tk.BOTTOM, fill=tk.X)
    results_vsb.pack(side=tk.RIGHT, fill=tk.Y)
    tree_results.tag_configure("ERROR", foreground=palette["error"])
    tree_results.tag_configure("FATAL", foreground=palette["error"])
    tree_results.tag_configure("WARNING", foreground=palette["warning"])

    detail_frame = tk.Frame(results_frame, bg=palette["surface"], highlightthickness=1,
                             highlightbackground=palette["border"], padx=10, pady=6)
    detail_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(6, 8))
    detail_fields = {}
    for label_text, key in [("Root Cause:", "root_cause"), ("Repairability:", "repairability")]:
        row = tk.Frame(detail_frame, bg=palette["surface"])
        row.pack(side=tk.TOP, fill=tk.X, anchor="w")
        tk.Label(row, text=label_text, bg=palette["surface"], fg=palette["text_muted"], font=theme.FONT_SMALL,
                 width=12, anchor="w").pack(side=tk.LEFT)
        value_lbl = tk.Label(row, text="—", bg=palette["surface"], fg=palette["text"], font=theme.FONT_SMALL,
                              anchor="w", justify=tk.LEFT, wraplength=900)
        value_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        detail_fields[key] = value_lbl

    # ---------------- tab management ----------------
    def _set_editor_controls_enabled(enabled: bool):
        state_ = "normal" if enabled else "disabled"
        for b in (find_btn, replace_btn, goto_btn, format_btn, auto_fix_btn):
            b.config(state=state_)
        report_btn.config(state=state_)
        if state_ == "disabled":
            autosave_btn.config(state="disabled")

    def _rebuild_tab_bar():
        for chip in state["tab_chip_widgets"].values():
            chip.destroy()
        state["tab_chip_widgets"] = {}
        for name in state["tab_order"]:
            tab = state["tabs"][name]
            is_active = (name == state["active_tab"])
            chip = tk.Frame(tab_bar, bg=palette["accent_tint"] if is_active else palette["panel_header_bg"],
                             highlightthickness=1, highlightbackground=palette["border"])
            chip.pack(side=tk.LEFT, padx=(4, 0), pady=3)
            label_text = posixpath.basename(name) + (" *" if tab.is_dirty else "")
            lbl = tk.Label(chip, text=label_text, bg=chip["bg"], fg=palette["text"], font=theme.FONT_SMALL,
                            cursor="hand2", padx=6, pady=2)
            lbl.pack(side=tk.LEFT)
            lbl.bind("<Button-1>", lambda e, n=name: _activate_tab(n))
            close_lbl = tk.Label(chip, text="✕", bg=chip["bg"], fg=palette["text_faint"], font=theme.FONT_SMALL,
                                  cursor="hand2")
            close_lbl.pack(side=tk.LEFT, padx=(0, 6))
            close_lbl.bind("<Button-1>", lambda e, n=name: _close_tab(n))
            state["tab_chip_widgets"][name] = chip

    def _on_tab_modified(name):
        _rebuild_tab_bar()
        _update_position_status()
        _schedule_autosave()
        iid = state["name_to_tree_item"].get(name)
        if iid:
            tag = state["tabs"][name]
            tree.item(iid, text=posixpath.basename(name) + (" *" if tag.is_dirty else ""))

    def _activate_tab(name):
        if name not in state["tabs"]:
            return
        if state["active_tab"] is not None and state["active_tab"] in state["tabs"]:
            state["tabs"][state["active_tab"]].frame.grid_forget()
        state["active_tab"] = name
        empty_label.grid_forget()
        tab = state["tabs"][name]
        tab.frame.grid(row=0, column=0, sticky="nsew")
        tab.text.focus_set()
        file_label.config(text=name)
        _rebuild_tab_bar()
        _update_position_status()
        iid = state["name_to_tree_item"].get(name)
        if iid:
            tree.selection_set(iid)
            tree.see(iid)

    def _open_file_in_editor(name):
        if name in state["tabs"]:
            _activate_tab(name)
            return
        data = state["mp"].get_bytes(name)
        text_content = data.decode("utf-8", errors="replace")
        tab = _EditorTab(editor_stack, palette, name, text_content, on_modified=_on_tab_modified)
        tab.text.bind("<ButtonRelease-1>", lambda e, t=tab: (t._highlight_current_line(), _update_position_status()),
                       add="+")
        tab.text.bind("<KeyRelease>", lambda e: _update_position_status(), add="+")
        tab.text.bind("<<StructureChecked>>", lambda e: _update_position_status(), add="+")
        state["tabs"][name] = tab
        state["tab_order"].append(name)
        _activate_tab(name)
        _set_editor_controls_enabled(True)

    def _show_unsupported(name):
        _set_status(STATE_READY, f"'{name}' is a binary or unsupported file type and cannot be edited here "
                                  f"(supported: {', '.join(sorted(package_tree.EDITABLE_EXTENSIONS))})")

    def _commit_tab(name):
        """'Save' (spec sections 6/54 of the editor prompt) - see module
        docstring for why this commits into the in-memory package rather
        than writing a standalone file to disk."""
        tab = state["tabs"].get(name)
        if not tab or not tab.is_dirty:
            return
        state["mp"].set_bytes(name, tab.current_text().encode("utf-8"))
        tab.mark_saved()
        _on_tab_modified(name)

    def _commit_all_tabs():
        for name in list(state["tabs"]):
            _commit_tab(name)

    def _schedule_autosave():
        if not state.get("autosave_enabled") or state.get("mp") is None:
            return
        old_id = state.get("autosave_after_id")
        if old_id is not None:
            try:
                win.after_cancel(old_id)
            except tk.TclError:
                pass
        state["autosave_after_id"] = win.after(state["autosave_delay_ms"], _run_autosave)

    def _run_autosave():
        state["autosave_after_id"] = None
        if not state.get("autosave_enabled") or state.get("mp") is None:
            return
        dirty = _any_dirty_tabs()
        if dirty:
            _commit_all_tabs()
            _set_status(STATE_READY, f"Auto Saved {len(dirty)} file(s) to package")

    def _update_autosave_ui():
        enabled = bool(state.get("autosave_enabled", True))
        autosave_btn.config(text="Auto Save: ON" if enabled else "Auto Save: OFF")
        theme.style_button(autosave_btn, palette, kind="secondary" if enabled else "secondary")
        autosave_btn.config(
            fg=palette["success"] if enabled else palette["text_muted"]
        )

    def _toggle_autosave(event=None):
        state["autosave_enabled"] = not state.get("autosave_enabled", True)
        if not state["autosave_enabled"]:
            if state.get("autosave_after_id") is not None:
                try:
                    win.after_cancel(state["autosave_after_id"])
                except tk.TclError:
                    pass
                state["autosave_after_id"] = None
            _set_status(STATE_READY, "Auto Save OFF")
        else:
            _set_status(STATE_READY, "Auto Save ON — 1 second delay")
            _schedule_autosave()
        _update_autosave_ui()
        return "break"

    autosave_btn.config(command=_toggle_autosave)
    _update_autosave_ui()

    def _close_tab(name):
        tab = state["tabs"].get(name)
        if not tab:
            return
        if tab.is_dirty:
            choice = messagebox.askyesnocancel(
                "Unsaved Changes", f"'{posixpath.basename(name)}' has unsaved changes.\n\n"
                                    "Yes = Save (commit to package)\nNo = Don't Save\nCancel = don't close",
                parent=win)
            if choice is None:
                return
            if choice:
                _commit_tab(name)
        if state["active_tab"] == name:
            state["active_tab"] = None
        tab.destroy()
        del state["tabs"][name]
        state["tab_order"].remove(name)
        if state["tab_order"]:
            _activate_tab(state["tab_order"][-1])
        else:
            empty_label.grid(row=0, column=0, sticky="nsew")
            _set_editor_controls_enabled(False)
        _rebuild_tab_bar()

    def _rebuild_tree():
        tree.delete(*tree.get_children())
        state["tree_item_to_name"] = {}
        state["name_to_tree_item"] = {}
        groups = package_tree.categorize(state["package"])
        for category in package_tree.CATEGORY_ORDER:
            names = groups.get(category)
            if not names:
                continue
            cat_iid = tree.insert("", "end", text=f"{category} ({len(names)})",
                                   open=(category in ("OPF", "NAV", "XHTML")))
            for name in names:
                iid = tree.insert(cat_iid, "end", text=posixpath.basename(name))
                state["tree_item_to_name"][iid] = name
                state["name_to_tree_item"][name] = iid

    def _on_tree_select(_event=None):
        selection = tree.selection()
        if not selection:
            return
        name = state["tree_item_to_name"].get(selection[0])
        if name is None:
            return  # a category header, not a file
        if name == state["active_tab"]:
            # _activate_tab() itself calls tree.selection_set() to keep the
            # tree in sync with the active tab (e.g. after error-navigation
            # or clicking a tab chip) - without this guard, THAT call fires
            # <<TreeviewSelect>> right back into this handler, which would
            # call _open_file_in_editor() -> _activate_tab() ->
            # tree.selection_set() again, forever (confirmed: a real
            # infinite loop during this feature's own test development).
            return
        if package_tree.is_editable(name):
            _open_file_in_editor(name)
        else:
            _show_unsupported(name)

    tree.bind("<<TreeviewSelect>>", _on_tree_select)

    # ---------------- open / load ----------------
    def _load_epub(path):
        if state.get("autosave_after_id") is not None:
            try:
                win.after_cancel(state["autosave_after_id"])
            except tk.TclError:
                pass
            state["autosave_after_id"] = None
        _set_status(STATE_OPENING, os.path.basename(path))
        for name in list(state["tabs"]):
            state["tabs"][name].destroy()
        state["epub_path"] = path
        state["mp"] = package_builder.MutablePackage.load(path)
        state["package"] = package_reader.read_package(path)
        state["tabs"] = {}
        state["tab_order"] = []
        state["active_tab"] = None
        _rebuild_tab_bar()
        empty_label.grid(row=0, column=0, sticky="nsew")
        _set_editor_controls_enabled(False)
        tree_results.delete(*tree_results.get_children())
        state["row_details"] = {}
        state["row_analysis"] = {}
        for lbl in detail_fields.values():
            lbl.config(text="—")
        source_lbl.config(text="—")
        target_lbl.config(text="—")
        _rebuild_tree()
        file_label.config(text=os.path.basename(path))
        save_btn.config(state="normal")
        save_all_btn.config(state="normal")
        save_epub_btn.config(state="normal")
        validate_btn.config(state="normal")
        auto_fix_btn.config(state="normal")
        autosave_btn.config(state="normal")
        _update_autosave_ui()
        _set_status(
            STATE_READY,
            "package loaded — Auto Save ON (1s)"
            if state.get("autosave_enabled", True)
            else "package loaded — Auto Save OFF"
        )

    def _open_epub():
        if not _confirm_close_all_dirty("Open a Different EPUB"):
            return
        path = filedialog.askopenfilename(
            title="Open EPUB", filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")], parent=win)
        if not path:
            return
        _load_epub(path)

    open_btn.config(command=_open_epub)

    # ---------------- complete EPUB scan / report ----------------
    def _report_scan_epub(epub_path):
        """Scan every package member and build a human-readable structural report.
        This is intentionally complementary to EPUBCheck: it does not replace or
        duplicate the validator. It answers the practical editor question
        "what is missing and where?" across the whole EPUB."""
        report = {
            "files": [], "xhtml": [], "images": [], "missing_images": [],
            "links": [], "broken_links": [], "footnote_links": [],
            "chapters": [], "parts": [], "tables": [], "references": [],
            "index_links": [], "broken_footnotes": [], "nav_links": [],
            "warnings": [], "summary": {}
        }
        with zipfile.ZipFile(epub_path, "r") as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            report["files"] = names
            name_set = set(names)
            xhtml_names = [n for n in names if posixpath.splitext(n)[1].lower() in {".xhtml",".html",".htm"}]
            report["xhtml"] = xhtml_names
            image_names = [n for n in names if posixpath.splitext(n)[1].lower() in
                           {".jpg",".jpeg",".png",".gif",".svg",".webp",".avif"}]
            report["images"] = image_names

            def clean_ref(ref):
                ref = _html.unescape((ref or "").strip())
                ref = ref.split("#", 1)[0].split("?", 1)[0]
                return ref

            def resolve(source, ref):
                ref = clean_ref(ref)
                if not ref or ref.startswith(("http://","https://","mailto:","data:","javascript:")):
                    return None
                return posixpath.normpath(posixpath.join(posixpath.dirname(source), ref))

            tag_re = re.compile(r"<(img|image)\b[^>]*?(?:src|href|xlink:href)\s*=\s*([\"'])(.*?)\2",
                                re.I | re.S)
            a_re = re.compile(r"<a\b[^>]*?\bhref\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a\s*>",
                              re.I | re.S)
            id_re = re.compile(r"""\bid\s*=\s*([\'"])(.*?)\1""", re.I | re.S)
            class_re = re.compile(r"""\bclass\s*=\s*([\'"])(.*?)\1""", re.I | re.S)
            heading_re = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
            table_re = re.compile(r"<table\b", re.I)
            ref_word_re = re.compile(r"\b(references?|bibliography|works\s+cited)\b", re.I)
            index_word_re = re.compile(r"\bindex\b", re.I)
            chapter_re = re.compile(r"\bchapter\s+([0-9]+|[ivxlcdm]+)\b", re.I)
            part_re = re.compile(r"\bpart\s+([0-9]+|[ivxlcdm]+)\b", re.I)
            footnote_re = re.compile(r"(footnote|footnotes|endnote|endnotes|\bfn\d*\b|\bnote\d*\b)", re.I)

            def text_only(s):
                return re.sub(r"<[^>]+>", " ", _html.unescape(s)).replace("\xa0"," ").strip()

            for name in xhtml_names:
                try:
                    raw = z.read(name).decode("utf-8", errors="replace")
                except Exception as e:
                    report["warnings"].append(f"{name}: could not decode as UTF-8 ({e})")
                    continue

                for m in tag_re.finditer(raw):
                    target = resolve(name, m.group(3))
                    if target:
                        if target not in name_set:
                            report["missing_images"].append((name, target, m.group(3)))
                        report["links"].append((name, target, "image"))

                for m in a_re.finditer(raw):
                    ref = m.group(2)
                    target = resolve(name, ref)
                    label = text_only(m.group(3))
                    if target:
                        exists = target in name_set
                        anchor = ref.split("#",1)[1] if "#" in ref else ""
                        record = (name, target, anchor, label, ref)
                        report["links"].append((name, target, "internal"))
                        if not exists:
                            report["broken_links"].append(record)
                        is_foot = bool(footnote_re.search(ref) or footnote_re.search(label) or
                                       footnote_re.search(anchor))
                        if is_foot:
                            report["footnote_links"].append(record)
                            if not exists:
                                report["broken_footnotes"].append(record)
                        is_index = bool(index_word_re.search(name) or index_word_re.search(label) or
                                        index_word_re.search(ref))
                        if is_index:
                            report["index_links"].append(record)

                for m in heading_re.finditer(raw):
                    heading = text_only(m.group(2))
                    if not heading:
                        continue
                    loc = f"{name}: heading h{m.group(1)}"
                    if chapter_re.search(heading):
                        report["chapters"].append((loc, heading))
                    if part_re.search(heading):
                        report["parts"].append((loc, heading))
                    if ref_word_re.search(heading):
                        report["references"].append((loc, heading))
                table_count = len(table_re.findall(raw))
                if table_count:
                    report["tables"].append((name, table_count))
                if ref_word_re.search(raw):
                    report["references"].append((f"{name}: content", "Reference/Bibliography text detected"))
                if index_word_re.search(name) or index_word_re.search(raw):
                    # Keep this file visible in the report even when no index link exists.
                    report["index_links"].append((name, "", "", "Index file/content detected", ""))

            # Detect NAV/NCX internal targets separately, since these are often
            # the source of "chapter/page link missing" issues.
            for name in names:
                if not name.lower().endswith((".xhtml",".html",".ncx")):
                    continue
                try:
                    raw = z.read(name).decode("utf-8", errors="replace")
                except Exception:
                    continue
                for m in re.finditer(r"""(?:href|src)\s*=\s*([\'"])(.*?)\1""", raw, re.I | re.S):
                    target = resolve(name, m.group(2))
                    if target and target not in name_set:
                        report["nav_links"].append((name, target, m.group(2)))

            # Numbering gap detection for chapters and parts.
            def number_gaps(items):
                nums = []
                for _, heading in items:
                    m = re.search(r"\b(?:chapter|part)\s+([0-9]+)\b", heading, re.I)
                    if m:
                        nums.append(int(m.group(1)))
                if not nums:
                    return []
                present = sorted(set(nums))
                return [n for n in range(1, max(present)+1) if n not in present]

            report["missing_chapters"] = number_gaps(report["chapters"])
            report["missing_parts"] = number_gaps(report["parts"])
            report["summary"] = {
                "package_files": len(names),
                "xhtml_files": len(xhtml_names),
                "image_files": len(image_names),
                "chapters": len(report["chapters"]),
                "parts": len(report["parts"]),
                "tables": sum(n for _, n in report["tables"]),
                "references": len(report["references"]),
                "internal_links": sum(1 for x in report["links"] if x[2] == "internal"),
                "broken_links": len(report["broken_links"]),
                "footnote_links": len(report["footnote_links"]),
                "broken_footnotes": len(report["broken_footnotes"]),
                "missing_images": len(report["missing_images"]),
                "index_links": len(report["index_links"]),
                "nav_broken_links": len(report["nav_links"]),
            }
        return report

    def _show_report(report):
        if not report:
            return
        dialog = tk.Toplevel(win)
        dialog.title("EPUB Complete Scan Report")
        dialog.geometry("1100x700")
        dialog.minsize(850, 500)
        dialog.transient(win)
        dialog.configure(bg=palette["app_bg"])

        top = tk.Frame(dialog, bg=palette["app_bg"], padx=12, pady=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="EPUB Complete Scan Report", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)
        tk.Label(top, text="All package files scanned", bg=palette["app_bg"], fg=palette["text_muted"],
                 font=theme.FONT_SMALL).pack(side=tk.LEFT, padx=14)

        nb = ttk.Notebook(dialog)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        summary = report["summary"]
        overview = tk.Frame(nb, bg=palette["app_bg"], padx=16, pady=14)
        nb.add(overview, text="Summary")
        rows = [
            ("Package files", summary["package_files"]),
            ("XHTML/HTML files", summary["xhtml_files"]),
            ("Images", summary["image_files"]),
            ("Chapters detected", summary["chapters"]),
            ("Missing chapter numbers", ", ".join(map(str, report["missing_chapters"])) or "None"),
            ("Parts detected", summary["parts"]),
            ("Missing part numbers", ", ".join(map(str, report["missing_parts"])) or "None"),
            ("Tables", summary["tables"]),
            ("References/Bibliography", summary["references"]),
            ("Internal links", summary["internal_links"]),
            ("Broken internal links", summary["broken_links"]),
            ("Missing image references", summary["missing_images"]),
            ("Footnote/endnote links", summary["footnote_links"]),
            ("Broken footnote links", summary["broken_footnotes"]),
            ("Index-related links/files", summary["index_links"]),
            ("Broken NAV/NCX targets", summary["nav_broken_links"]),
        ]
        for r, (k,v) in enumerate(rows):
            tk.Label(overview, text=k, bg=palette["app_bg"], fg=palette["text_muted"],
                     font=theme.FONT_SMALL_BOLD, anchor="w", width=30).grid(row=r,column=0,sticky="w",pady=3)
            fg = palette["error"] if ("Missing" in k or "Broken" in k) and str(v) not in ("0","None") else palette["text"]
            tk.Label(overview, text=str(v), bg=palette["app_bg"], fg=fg,
                     font=theme.FONT_SMALL_BOLD).grid(row=r,column=1,sticky="w",pady=3)

        def add_list_tab(title, columns, rows_data):
            frame = tk.Frame(nb, bg=palette["app_bg"])
            nb.add(frame, text=title)
            wrap = tk.Frame(frame, bg=palette["app_bg"])
            wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            tv = ttk.Treeview(wrap, columns=columns, show="headings")
            for c in columns:
                tv.heading(c, text=c.replace("_"," ").title())
                tv.column(c, width=260 if c not in ("line","column") else 80, anchor="w")
            vs = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
            hs = ttk.Scrollbar(wrap, orient="horizontal", command=tv.xview)
            tv.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
            tv.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            hs.pack(side=tk.BOTTOM, fill=tk.X)
            vs.pack(side=tk.RIGHT, fill=tk.Y)
            for row in rows_data:
                tv.insert("", "end", values=row)
            return tv

        add_list_tab("Missing Images", ("source","target","original_ref"), report["missing_images"])
        add_list_tab("Broken Links", ("source","target","anchor","label","ref"), report["broken_links"])
        add_list_tab("Footnotes", ("source","target","anchor","label","ref"), report["footnote_links"])
        add_list_tab("Chapters / Parts", ("location","heading"),
                     [("Chapter", f"{loc} — {heading}") for loc,heading in report["chapters"]] +
                     [("Part", f"{loc} — {heading}") for loc,heading in report["parts"]])
        add_list_tab("Tables / References", ("file_or_location","count_or_detail"),
                     [(f, n) for f,n in report["tables"]] +
                     [(loc, detail) for loc,detail in report["references"]])
        add_list_tab("Index", ("source","target","anchor","label","ref"), report["index_links"])
        add_list_tab("NAV / NCX Issues", ("source","target","ref"), report["nav_links"])

        bottom = tk.Frame(dialog, bg=palette["app_bg"], padx=10, pady=8)
        bottom.pack(fill=tk.X)
        def save_report():
            path = filedialog.asksaveasfilename(parent=dialog, title="Save EPUB Scan Report",
                                                defaultextension=".txt",
                                                initialfile="epub_scan_report.txt",
                                                filetypes=[("Text report","*.txt"),("HTML report","*.html")])
            if not path:
                return
            lines_out = ["EPUBFORGE COMPLETE EPUB SCAN REPORT", "="*70, ""]
            lines_out += [f"{k}: {v}" for k,v in summary.items()]
            lines_out += ["", "MISSING IMAGES"]
            lines_out += [f"{a} -> {b} ({c})" for a,b,c in report["missing_images"]]
            lines_out += ["", "BROKEN LINKS"]
            lines_out += [f"{a} -> {b}#{c} | {d} | {e}" for a,b,c,d,e in report["broken_links"]]
            lines_out += ["", "BROKEN FOOTNOTE LINKS"]
            lines_out += [f"{a} -> {b}#{c} | {d} | {e}" for a,b,c,d,e in report["broken_footnotes"]]
            lines_out += ["", "MISSING CHAPTER NUMBERS", str(report["missing_chapters"])]
            lines_out += ["MISSING PART NUMBERS", str(report["missing_parts"])]
            lines_out += ["", "CHAPTERS"] + [f"{a}: {b}" for a,b in report["chapters"]]
            lines_out += ["", "PARTS"] + [f"{a}: {b}" for a,b in report["parts"]]
            lines_out += ["", "TABLES"] + [f"{a}: {b}" for a,b in report["tables"]]
            lines_out += ["", "REFERENCES"] + [f"{a}: {b}" for a,b in report["references"]]
            lines_out += ["", "INDEX"] + [f"{a} -> {b}#{c} | {d}" for a,b,c,d,e in report["index_links"]]
            lines_out += ["", "NAV/NCX BROKEN TARGETS"] + [f"{a} -> {b} ({c})" for a,b,c in report["nav_links"]]
            Path(path).write_text("\n".join(lines_out), encoding="utf-8")
            messagebox.showinfo("Report Saved", f"Report saved to:\n{path}", parent=dialog)
        tk.Button(bottom, text="Save Report...", command=save_report).pack(side=tk.RIGHT, padx=4)
        tk.Button(bottom, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    def _run_report_scan(show=True):
        if state["mp"] is None:
            return
        _commit_all_tabs()
        _start_progress("Scanning every EPUB file and checking links, images, chapters, parts, tables, references, index and footnotes…")
        try:
            with tempfile.TemporaryDirectory(prefix="epubforge_editor_report_") as tmp:
                temp_path = os.path.join(tmp, "report_snapshot.epub")
                state["mp"].write_epub(temp_path)
                report = _report_scan_epub(temp_path)
            state["last_report"] = report
            _set_status(STATE_COMPLETE, f"Report scan complete — {report['summary']['broken_links']} broken links, {report['summary']['missing_images']} missing images")
            if show:
                _show_report(report)
        except Exception as e:
            _set_status(STATE_FAILED, f"Report scan failed: {e}")
            messagebox.showerror("Report Scan Failed", str(e), parent=win)
        finally:
            _stop_progress()

    report_btn.config(command=_run_report_scan)

    # ---------------- validate ----------------
    def _populate_results(snapshot):
        tree_results.delete(*tree_results.get_children())
        state["row_details"] = {}
        state["row_analysis"] = {}
        for lbl in detail_fields.values():
            lbl.config(text="—")

        def _add(tool, severity, code, file_, line, col, message, analysis=None):
            iid = tree_results.insert("", "end", values=(tool, severity, code, file_, line, col, message),
                                       tags=(severity,))
            state["row_details"][iid] = {"root_cause": "—", "repairability": "—"}
            if analysis is not None:
                state["row_analysis"][iid] = analysis
                cascade_note = " [cascading]" if analysis.root_cause.is_cascading else ""
                state["row_details"][iid]["root_cause"] = (
                    f"{analysis.root_cause.category}{cascade_note} — {analysis.root_cause.explanation}")
                state["row_details"][iid]["repairability"] = (
                    f"{analysis.repair_plan.repairability.value} — {analysis.repair_plan.summary}")

        for issue, analysis in zip(snapshot.quick_result.issues, snapshot.qv_analyses):
            _add("Quick", issue.severity, issue.code, issue.file, "", "", issue.message, analysis)

        ec = snapshot.epubcheck_result
        if ec is not None and ec.ran:
            paired = sorted(
                zip(ec.messages, snapshot.ec_analyses),
                key=lambda p: ({"FATAL": 0, "ERROR": 1, "WARNING": 2}.get(p[0].severity, 3), p[0].file, p[0].line))
            for m, analysis in paired:
                _add("EPUBCheck", m.severity, m.code, m.file,
                     m.line if m.line >= 0 else "", m.column if m.column >= 0 else "", m.message, analysis)
        elif ec is None:
            _add("EPUBCheck", "WARNING", "EC-UNAVAILABLE", "", "", "",
                 "EPUBCheck is not available on this machine - only Quick Validator results are shown.")
        elif not ec.ran:
            _add("EPUBCheck", "ERROR", "EC-UNAVAILABLE", "", "", "", f"EPUBCheck could not run: {ec.error}")

    def _run_validate():
        if state["mp"] is None or state.get("validation_running"):
            return
        _commit_all_tabs()
        state["validation_running"] = True
        validate_btn.config(state="disabled")
        auto_fix_btn.config(state="disabled")
        report_btn.config(state="disabled")
        open_btn.config(state="disabled")
        _start_progress("Validate: preparing current EPUB…")
        _set_status(STATE_VALIDATING, "preparing temporary snapshot")

        try:
            tmp_dir = tempfile.mkdtemp(prefix="epubforge_editor_")
            temp_path = os.path.join(tmp_dir, "snapshot.epub")
            state["mp"].write_epub(temp_path)
        except Exception as e:
            state["validation_running"] = False
            _stop_progress()
            validate_btn.config(state="normal")
            auto_fix_btn.config(state="normal")
            report_btn.config(state="normal")
            open_btn.config(state="normal")
            messagebox.showerror("Validation Failed", str(e), parent=win)
            return

        def worker():
            try:
                snapshot = validation_runner.run_validation(temp_path)
                report = _report_scan_epub(temp_path)
                win.after(0, lambda: finish(snapshot, report, None))
            except Exception as e:
                win.after(0, lambda: finish(None, None, e))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        def finish(snapshot, report, error):
            state["validation_running"] = False
            _stop_progress()
            if error is not None:
                _set_status(STATE_FAILED, f"Validation failed: {error}")
                messagebox.showerror("Validation Failed", str(error), parent=win)
            else:
                state["package"] = snapshot.package
                state["last_report"] = report
                _populate_results(snapshot)
                s = report["summary"]
                _set_status(
                    STATE_COMPLETE,
                    ("VALID" if snapshot.is_valid else "INVALID - see validation results")
                    + f" | Scan: {s['broken_links']} broken links, {s['missing_images']} missing images"
                )
            validate_btn.config(state="normal")
            auto_fix_btn.config(state="normal")
            report_btn.config(state="normal")
            open_btn.config(state="normal")

        _set_status(STATE_VALIDATING, "running Quick Validator + real EPUBCheck + complete EPUB scan…")
        threading.Thread(target=worker, daemon=True).start()

    validate_btn.config(command=_run_validate)

    # ---------------- safe deterministic Auto Fix ----------------
    def _show_auto_fix_report(report):
        """Show a concise result while keeping the editor as the source of truth.

        Auto Fix is deliberately limited to the existing centralized repair
        engine/strategy registry. It does not guess at EPUB content and never
        writes the original EPUB.
        """
        details = [
            f"Initial errors: {report.initial_errors}",
            f"Automatically fixed: {report.fixed_automatically}",
            f"Remaining errors: {report.remaining}",
            f"Repair passes: {report.passes_run}",
            "",
            report.final_status or "COMPLETE",
        ]
        if report.files_modified:
            details += ["", "Files modified:", *[f"  • {name}" for name in report.files_modified[:25]]]
            if len(report.files_modified) > 25:
                details.append(f"  • ... and {len(report.files_modified) - 25} more")
        if report.rolled_back:
            details += ["", "No unsafe pass was kept; the last known-good state was retained."]
        if report.content_integrity_verified:
            details += ["", "Content integrity: VERIFIED"]
        else:
            details += ["", f"Content integrity: {report.content_integrity or 'not verified'}"]

        messagebox.showinfo("Auto Fix Complete", "\n".join(details), parent=win)

    def _refresh_open_tabs_from_package(changed_files):
        """Refresh only open tabs whose package bytes changed after Auto Fix."""
        for name in list(state["tabs"]):
            tab = state["tabs"][name]
            if name not in changed_files or not state["mp"].exists(name):
                continue
            try:
                new_text = state["mp"].get_bytes(name).decode("utf-8", errors="replace")
            except Exception:
                continue
            if new_text == tab.current_text():
                tab.mark_saved()
                continue
            cursor = tab.text.index("insert")
            tab.text.edit_modified(False)
            tab.text.delete("1.0", "end")
            tab.text.insert("1.0", new_text)
            tab.text.edit_reset()
            tab.text.edit_modified(False)
            tab.loaded_text = new_text
            tab.is_dirty = False
            try:
                tab.text.mark_set("insert", cursor)
            except tk.TclError:
                tab.text.mark_set("insert", "1.0")
            tab._update_line_numbers()
            tab._run_highlight()
            tab._run_structure_check()
        _rebuild_tab_bar()
        _update_position_status()

    def _run_auto_fix(event=None):
        if state["mp"] is None or state.get("auto_fix_running") or state.get("validation_running"):
            return "break" if event is not None else None

        _commit_all_tabs()
        state["auto_fix_running"] = True
        auto_fix_btn.config(state="disabled")
        validate_btn.config(state="disabled")
        report_btn.config(state="disabled")
        open_btn.config(state="disabled")
        _start_progress("Auto Fix: inspecting EPUB and applying safe deterministic repairs…")
        _set_status(STATE_VALIDATING, "Auto Fix: preparing working package")

        try:
            tmp_dir = tempfile.mkdtemp(prefix="epubforge_editor_autofix_")
            working_path = os.path.join(tmp_dir, "working.epub")
            state["mp"].write_epub(working_path)
        except Exception as e:
            state["auto_fix_running"] = False
            _stop_progress()
            auto_fix_btn.config(state="normal")
            validate_btn.config(state="normal")
            report_btn.config(state="normal")
            open_btn.config(state="normal")
            messagebox.showerror("Auto Fix Failed", str(e), parent=win)
            return "break" if event is not None else None

        def worker():
            try:
                from core.epub import repair_engine as repair_engine_module
                from core.epub.repair_strategies import DEFAULT_REGISTRY
                try:
                    from core.epub.xhtml_repair_strategy import register as register_xhtml_strategy
                    register_xhtml_strategy(DEFAULT_REGISTRY)
                except Exception:
                    pass

                report = repair_engine_module.run_full_auto_repair(working_path)
                repaired_path = report.output_path
                if not repaired_path or not os.path.isfile(repaired_path):
                    raise RuntimeError("Auto Fix did not produce a repaired package.")
                new_mp = package_builder.MutablePackage.load(repaired_path)
                new_package = package_reader.read_package(repaired_path)
                changed = set(report.files_modified) | set(report.files_added) | set(report.files_removed)
                scan = _report_scan_epub(repaired_path)
                win.after(0, lambda: finish(report, new_mp, new_package, changed, scan, None))
            except Exception as e:
                win.after(0, lambda: finish(None, None, None, set(), None, e))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        def finish(report, new_mp, new_package, changed, scan, error):
            state["auto_fix_running"] = False
            _stop_progress()
            if error is not None:
                _set_status(STATE_FAILED, f"Auto Fix failed: {error}")
                messagebox.showerror(
                    "Auto Fix Failed",
                    "Auto Fix could not complete. No original EPUB file was modified.\n\n"
                    f"{type(error).__name__}: {error}", parent=win)
            else:
                state["mp"] = new_mp
                state["package"] = new_package
                state["last_report"] = scan
                _refresh_open_tabs_from_package(changed)
                _rebuild_tree()
                _set_status(
                    STATE_COMPLETE,
                    (report.final_status or "Auto Fix complete") +
                    f" | Scan: {scan['summary']['broken_links']} broken links, "
                    f"{scan['summary']['missing_images']} missing images"
                )
                _show_auto_fix_report(report)
            if state.get("mp") is not None:
                auto_fix_btn.config(state="normal")
                validate_btn.config(state="normal")
                report_btn.config(state="normal")
            open_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()
        return "break" if event is not None else None

    auto_fix_btn.config(command=_run_auto_fix)

    def _on_result_select(_event=None):
        selection = tree_results.selection()
        if not selection:
            return
        iid = selection[0]
        detail = state["row_details"].get(iid, {})
        for key, lbl in detail_fields.items():
            lbl.config(text=str(detail.get(key, "—")) or "—")

        analysis = state["row_analysis"].get(iid)
        if analysis is None:
            source_lbl.config(text="—")
            target_lbl.config(text="—")
            return

        is_link = link_inspector.is_link_finding(analysis)
        source_text = link_inspector.describe_source(analysis) if is_link else "—"
        target_text = link_inspector.describe_target(analysis, state["package"]) if is_link else "—"

        ctx = analysis.context
        if ctx.file and state["mp"] is not None and state["mp"].exists(ctx.file):
            if package_tree.is_editable(ctx.file):
                _open_file_in_editor(ctx.file)
                state["tabs"][ctx.file].jump_to(ctx.line, ctx.column)
                _update_position_status()
            else:
                _set_status(STATE_READY, f"'{ctx.file}' is not an editable file type")

        source_lbl.config(text=source_text)
        target_lbl.config(text=target_text)

    tree_results.bind("<<TreeviewSelect>>", _on_result_select)

    # ---------------- save ----------------
    def _save_epub_as():
        if state["mp"] is None:
            return False
        _commit_all_tabs()
        suggested = os.path.splitext(os.path.basename(state["epub_path"]))[0] + ".edited.epub"
        out_path = filedialog.asksaveasfilename(
            title="Save Edited EPUB As", parent=win, defaultextension=".epub",
            initialfile=suggested, filetypes=[("EPUB files", "*.epub")])
        if not out_path:
            return False
        backup_path = os.path.splitext(state["epub_path"])[0] + ".backup.epub"
        try:
            # Only made once per original file: a Save As backup exists to
            # preserve the PRISTINE original, not to snapshot every save.
            if not os.path.isfile(backup_path):
                shutil.copyfile(state["epub_path"], backup_path)
            state["mp"].write_epub(out_path)
        except OSError as e:
            messagebox.showerror("Save Edited EPUB", f"Could not save: {e}", parent=win)
            return False
        messagebox.showinfo(
            "Save Edited EPUB",
            f"Saved to:\n{out_path}\n\nBackup of the original:\n{backup_path}\n\n"
            "The original EPUB was never modified.", parent=win)
        _set_status(STATE_READY, f"saved to {os.path.basename(out_path)}")
        return True

    save_epub_btn.config(command=_save_epub_as)

    def _save_current():
        if state["active_tab"]:
            _commit_tab(state["active_tab"])
            _set_status(STATE_READY, f"{posixpath.basename(state['active_tab'])} committed to package")

    def _save_all():
        _commit_all_tabs()
        _set_status(STATE_READY, "all open files committed to package")

    save_btn.config(command=_save_current)
    save_all_btn.config(command=_save_all)

    # ---------------- Find / Replace (spec sections 13-20, 42, 46, 61-62) ----------------
    find_state = {"dialog": None}

    def _active_text():
        tab = state["tabs"].get(state["active_tab"])
        return tab.text if tab else None

    def _open_find_dialog(with_replace: bool):
        text_widget = _active_text()
        if text_widget is None:
            return
        if find_state["dialog"] is not None and find_state["dialog"].winfo_exists():
            find_state["dialog"].show(with_replace)
            return
        find_state["dialog"] = _FindReplaceDialog(win, palette, state, _active_text)
        find_state["dialog"].show(with_replace)

    find_btn.config(command=lambda: _open_find_dialog(False))
    replace_btn.config(command=lambda: _open_find_dialog(True))

    def _find_next(event=None):
        if find_state["dialog"] is not None and find_state["dialog"].winfo_exists():
            find_state["dialog"].find_next()
        return "break"

    def _find_previous(event=None):
        if find_state["dialog"] is not None and find_state["dialog"].winfo_exists():
            find_state["dialog"].find_previous()
        return "break"

    # ---------------- Go To Line / Column ----------------
    def _go_to_line(event=None):
        tab = state["tabs"].get(state["active_tab"])
        if not tab:
            return "break" if event is not None else None

        dialog = tk.Toplevel(win)
        dialog.title("Go To Line / Column")
        dialog.configure(bg=palette["app_bg"])
        dialog.transient(win)
        dialog.resizable(False, False)

        box = tk.Frame(dialog, bg=palette["app_bg"], padx=18, pady=15)
        box.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            box, text="Go to location", bg=palette["app_bg"],
            fg=palette["text"], font=theme.FONT_BODY_BOLD
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        tk.Label(
            box, text="Line", bg=palette["app_bg"], fg=palette["text_muted"],
            font=theme.FONT_SMALL
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))

        line_var = tk.StringVar(value=tab.text.index("insert").split(".")[0])
        line_entry = tk.Entry(box, textvariable=line_var, width=14)
        line_entry.grid(row=1, column=1, sticky="ew", pady=3)

        tk.Label(
            box, text="Column", bg=palette["app_bg"], fg=palette["text_muted"],
            font=theme.FONT_SMALL
        ).grid(row=2, column=0, sticky="w", padx=(0, 8))

        col_var = tk.StringVar(value=str(int(tab.text.index("insert").split(".")[1]) + 1))
        col_entry = tk.Entry(box, textvariable=col_var, width=14)
        col_entry.grid(row=2, column=1, sticky="ew", pady=3)

        tk.Label(
            box, text="Tip: Ctrl+G • also accepts 484:78",
            bg=palette["app_bg"], fg=palette["text_faint"],
            font=theme.FONT_SMALL
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 12))

        def go():
            try:
                line = int(line_var.get().strip())
                col = int(col_var.get().strip())
                if line < 1 or col < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Go To Line / Column",
                    "Enter positive numbers for line and column.",
                    parent=dialog,
                )
                return

            tab.jump_to(line, col)
            _update_position_status()
            dialog.destroy()

        def key_go(e=None):
            go()
            return "break"

        buttons = tk.Frame(box, bg=palette["app_bg"])
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew")

        ok = tk.Button(buttons, text="Go", command=go)
        theme.style_button(ok, palette, kind="primary")
        ok.pack(side=tk.RIGHT, padx=(6, 0))

        cancel = tk.Button(buttons, text="Cancel", command=dialog.destroy)
        theme.style_button(cancel, palette, kind="secondary")
        cancel.pack(side=tk.RIGHT)

        dialog.bind("<Return>", key_go)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        line_entry.focus_set()
        line_entry.selection_range(0, tk.END)

        # Keep the popup close to the editor window.
        dialog.update_idletasks()
        x = win.winfo_rootx() + max(20, (win.winfo_width() - dialog.winfo_width()) // 2)
        y = win.winfo_rooty() + 110
        dialog.geometry(f"+{x}+{y}")

        if event is not None:
            return "break"

    goto_btn.config(command=_go_to_line)

    # ---------------- Pretty Print (ZERO CONTENT LOSS guarantee) ----------------
    def _format_current():
        """Reindents the active tab's buffer (XML-like via lxml, CSS via a
        conservative token-preserving reformatter) and NEVER applies the
        result unless the visible content is provably unchanged - reuses
        core.epub.regression_checker.extract_visible_text, the SAME
        function core.epub.repair_engine's own before/after content-
        integrity check uses for a whole-EPUB rebuild, applied here to a
        single in-memory buffer. Any parse failure or content mismatch
        refuses the format and leaves the buffer completely untouched
        (spec: "If a repair cannot be performed safely: DO NOT GUESS...
        preserve the original... unchanged")."""
        name = state["active_tab"]
        tab = state["tabs"].get(name)
        if not tab:
            return
        ext = tab._ext()
        original = tab.current_text()

        try:
            if ext in _XML_LIKE_EXT:
                formatted = _pretty_print_xml(original)
            elif ext == ".css":
                formatted = _pretty_print_css(original)
            else:
                messagebox.showinfo(
                    "Pretty Print",
                    f"'{posixpath.basename(name)}' has no formatter available "
                    "(only XML/XHTML/OPF/NCX/HTML and CSS are supported).", parent=win)
                return
        except ValueError as e:
            messagebox.showerror("Pretty Print", f"Cannot format '{posixpath.basename(name)}': {e}\n\n"
                                                   "The file was left unchanged.", parent=win)
            return

        if formatted == original:
            _set_status(STATE_READY, "already formatted - no changes made")
            return

        if ext in _XML_LIKE_EXT:
            before_visible = regression_checker.extract_visible_text(original.encode("utf-8"))
            after_visible = regression_checker.extract_visible_text(formatted.encode("utf-8"))
            if before_visible is None or after_visible is None:
                messagebox.showerror(
                    "Pretty Print",
                    f"Cannot verify '{posixpath.basename(name)}' would be unchanged - formatting refused.",
                    parent=win)
                return
        else:
            before_visible = re.sub(r"\s+", "", original)
            after_visible = re.sub(r"\s+", "", formatted)

        if before_visible != after_visible:
            messagebox.showerror(
                "Pretty Print",
                f"Refused to format '{posixpath.basename(name)}': the result would change visible "
                "content, not just whitespace. The file was left unchanged.", parent=win)
            return

        # Single undo transaction, same pattern as Replace All above.
        text_widget = tab.text
        cursor = text_widget.index("insert")
        text_widget.config(autoseparators=False)
        try:
            text_widget.edit_separator()
            text_widget.delete("1.0", "end")
            text_widget.insert("1.0", formatted)
            text_widget.edit_separator()
        finally:
            text_widget.config(autoseparators=True)
        try:
            text_widget.mark_set("insert", cursor)
        except tk.TclError:
            pass
        _set_status(STATE_READY, f"{posixpath.basename(name)} formatted")

    format_btn.config(command=_format_current)

    # ---------------- Help / keyboard shortcuts ----------------
    def _show_shortcuts(event=None):
        dialog = tk.Toplevel(win)
        dialog.title("EPUBForge Editor — Keyboard Shortcuts")
        dialog.geometry("720x560")
        dialog.minsize(600, 420)
        dialog.transient(win)
        dialog.configure(bg=palette["app_bg"])

        outer = tk.Frame(dialog, bg=palette["app_bg"], padx=18, pady=15)
        outer.pack(fill=tk.BOTH, expand=True)
        tk.Label(outer, text="Keyboard Shortcuts", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_APP_TITLE).pack(anchor="w")
        tk.Label(outer, text="EPUBForge Editor quick reference", bg=palette["app_bg"],
                 fg=palette["text_muted"], font=theme.FONT_SMALL).pack(anchor="w", pady=(2, 10))

        rows = [
            ("F1", "Open this Help window"),
            ("Ctrl+S", "Commit current file to the in-memory EPUB package"),
            ("Ctrl+Shift+S", "Commit all open files"),
            ("Ctrl+G / Ctrl+L", "Go to Line / Column"),
            ("Ctrl+F", "Find"),
            ("Ctrl+H", "Find / Replace"),
            ("F3 / Shift+F3", "Find next / previous"),
            ("Ctrl+Shift+F", "Pretty Print"),
            ("Ctrl+Alt+F", "Safe Auto Fix + revalidate"),
            ("Ctrl+Alt+T", "Open Tag Problems"),
            ("Report button", "Scan all EPUB files for missing images, chapters, parts, tables, references, index links, footnotes and broken internal links"),
            ("Ctrl+Alt+S", "Toggle Auto Save (1-second delay)"),
            ("Double-click tag problem", "Jump to exact offending source tag"),
            ("Enter in Tag Problems", "Jump to selected problem"),
            ("Ctrl+D", "Duplicate current line"),
            ("Ctrl+Shift+K", "Delete current line"),
            ("Ctrl+/", "Comment / uncomment selected lines"),
            ("Tab / Shift+Tab", "Indent / unindent"),
            ("Esc", "Close the current dialog"),
        ]
        table = tk.Frame(outer, bg=palette["app_bg"])
        table.pack(fill=tk.BOTH, expand=True)
        for i, (key, action) in enumerate(rows):
            tk.Label(table, text=key, bg=palette["app_bg"], fg=palette["text"],
                     font=theme.FONT_SMALL_BOLD, width=25, anchor="w").grid(row=i, column=0, sticky="w", pady=4)
            tk.Label(table, text=action, bg=palette["app_bg"], fg=palette["text_muted"],
                     font=theme.FONT_SMALL, anchor="w", justify=tk.LEFT).grid(row=i, column=1, sticky="w", pady=4)
        table.columnconfigure(1, weight=1)
        tk.Button(outer, text="Close", command=dialog.destroy).pack(anchor="e", pady=(10, 0))
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.update_idletasks()
        x = win.winfo_rootx() + max(20, (win.winfo_width() - dialog.winfo_width()) // 2)
        y = win.winfo_rooty() + 90
        dialog.geometry(f"+{x}+{y}")
        return "break" if event is not None else None

    help_btn.config(command=_show_shortcuts)

    # ---------------- keyboard shortcuts (spec section 67) ----------------
    win.bind("<Control-s>", lambda e: (_save_current(), "break")[1])
    win.bind("<Control-S>", lambda e: (_save_current(), "break")[1])
    win.bind("<Control-Shift-S>", lambda e: (_save_all(), "break")[1])
    win.bind("<Control-Shift-s>", lambda e: (_save_all(), "break")[1])
    win.bind("<Control-f>", lambda e: (_open_find_dialog(False), "break")[1])
    win.bind("<Control-h>", lambda e: (_open_find_dialog(True), "break")[1])
    win.bind("<Control-g>", lambda e: (_go_to_line(), "break")[1])
    win.bind("<Control-Shift-F>", lambda e: (_format_current(), "break")[1])
    win.bind("<Control-Shift-f>", lambda e: (_format_current(), "break")[1])
    win.bind("<Control-Alt-f>", lambda e: (_run_auto_fix(), "break")[1])
    win.bind("<Control-Alt-F>", lambda e: (_run_auto_fix(), "break")[1])
    # Common editor navigation/convenience shortcuts.
    win.bind("<Control-l>", lambda e: (_go_to_line(), "break")[1])
    win.bind("<Control-L>", lambda e: (_go_to_line(), "break")[1])
    win.bind("<Control-Alt-t>", lambda e: (_show_tag_problems(), "break")[1])
    win.bind("<Control-Alt-T>", lambda e: (_show_tag_problems(), "break")[1])
    win.bind("<Control-Alt-s>", _toggle_autosave)
    win.bind("<Control-Alt-S>", _toggle_autosave)
    win.bind("<F1>", _show_shortcuts)
    win.bind("<Control-d>", lambda e: (_active_text() and state["tabs"][state["active_tab"]]._duplicate_line(e), "break")[1])
    win.bind("<Control-D>", lambda e: (_active_text() and state["tabs"][state["active_tab"]]._duplicate_line(e), "break")[1])
    win.bind("<Control-Shift-k>", lambda e: (_active_text() and state["tabs"][state["active_tab"]]._delete_line(e), "break")[1])
    win.bind("<Control-Shift-K>", lambda e: (_active_text() and state["tabs"][state["active_tab"]]._delete_line(e), "break")[1])
    win.bind("<F3>", _find_next)
    win.bind("<Shift-F3>", _find_previous)

    if initial_epub_path:
        win.after(10, lambda: _load_epub(initial_epub_path))

    # Exposed for automated testing (tests/test_editor_professional_features.py)
    # and any future external driver - `state` and the handful of internal
    # functions below are otherwise pure closures with no way to observe or
    # drive them from outside this factory function.
    win.editor_state = state
    win.editor_widgets = {
        "tree": tree, "open_btn": open_btn, "save_btn": save_btn, "save_all_btn": save_all_btn,
        "save_epub_btn": save_epub_btn, "find_btn": find_btn, "replace_btn": replace_btn,
        "goto_btn": goto_btn, "validate_btn": validate_btn, "tree_results": tree_results,
        "format_btn": format_btn, "help_btn": help_btn, "report_btn": report_btn,
        "auto_fix_btn": auto_fix_btn, "progress_bar": progress_bar,
    }
    win.editor_actions = {
        "load_epub": _load_epub, "open_file": _open_file_in_editor, "activate_tab": _activate_tab,
        "close_tab": _close_tab, "commit_tab": _commit_tab, "commit_all": _commit_all_tabs,
        "go_to_line": _go_to_line, "save_epub_as": _save_epub_as, "open_find_dialog": _open_find_dialog,
        "find_next": _find_next, "find_previous": _find_previous, "format_current": _format_current,
        "show_tag_problems": _show_tag_problems, "show_shortcuts": _show_shortcuts,
        "auto_fix": _run_auto_fix,
        "toggle_autosave": _toggle_autosave, "report_scan": _run_report_scan,
    }

    return win


# ---------------------------------------------------------------- Find/Replace dialog
class _FindReplaceDialog(tk.Toplevel):
    """Non-modal Find/Replace panel (spec sections 13-20) - stays open
    while the user keeps editing/switching tabs; every action re-reads
    `get_active_text()` at call time, so it always operates on whichever
    tab is currently active, never a stale reference to the tab that was
    active when the dialog was first opened."""

    def __init__(self, parent, palette, app_state, get_active_text):
        super().__init__(parent)
        self.palette = palette
        self.app_state = app_state
        self.get_active_text = get_active_text
        self.title("Find / Replace")
        self.configure(bg=palette["app_bg"])
        self.transient(parent)
        self.resizable(True, False)
        self._matches = []  # list of (start_index_str, end_index_str)
        self._current_match = -1

        pad = {"padx": 8, "pady": 4}
        grid = tk.Frame(self, bg=palette["app_bg"])
        grid.pack(side=tk.TOP, fill=tk.X, **pad)

        tk.Label(grid, text="Find:", bg=palette["app_bg"], fg=palette["text"], width=8, anchor="w").grid(
            row=0, column=0, sticky="w")
        self.find_var = tk.StringVar()
        find_entry = tk.Entry(grid, textvariable=self.find_var, width=40)
        find_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 8))
        find_entry.bind("<Return>", lambda e: self.find_next())

        self.replace_row_label = tk.Label(grid, text="Replace:", bg=palette["app_bg"], fg=palette["text"],
                                           width=8, anchor="w")
        self.replace_var = tk.StringVar()
        self.replace_entry = tk.Entry(grid, textvariable=self.replace_var, width=40)

        grid.columnconfigure(1, weight=1)

        opts = tk.Frame(self, bg=palette["app_bg"])
        opts.pack(side=tk.TOP, fill=tk.X, **pad)
        self.match_case_var = tk.BooleanVar(value=False)
        self.whole_word_var = tk.BooleanVar(value=False)
        self.regex_var = tk.BooleanVar(value=False)
        self.wrap_var = tk.BooleanVar(value=True)
        for text, var in (("Match Case", self.match_case_var), ("Whole Word", self.whole_word_var),
                          ("Regular Expression", self.regex_var), ("Wrap Around", self.wrap_var)):
            cb = tk.Checkbutton(opts, text=text, variable=var, bg=palette["app_bg"], fg=palette["text"],
                                 selectcolor=palette["surface"], command=self._recompute_matches)
            cb.pack(side=tk.LEFT, padx=(0, 10))

        self.match_count_lbl = tk.Label(self, text="0 matches", bg=palette["app_bg"], fg=palette["text_muted"],
                                         font=theme.FONT_SMALL, anchor="w")
        self.match_count_lbl.pack(side=tk.TOP, fill=tk.X, padx=8)

        btn_row = tk.Frame(self, bg=palette["app_bg"])
        btn_row.pack(side=tk.TOP, fill=tk.X, **pad)
        find_next_btn = tk.Button(btn_row, text="Find Next", command=self.find_next)
        theme.style_button(find_next_btn, palette, kind="primary")
        find_next_btn.pack(side=tk.LEFT, padx=(0, 4))
        find_prev_btn = tk.Button(btn_row, text="Find Previous", command=self.find_previous)
        theme.style_button(find_prev_btn, palette, kind="secondary")
        find_prev_btn.pack(side=tk.LEFT, padx=(0, 12))
        self.replace_btn_w = tk.Button(btn_row, text="Replace", command=self.replace_one)
        theme.style_button(self.replace_btn_w, palette, kind="secondary")
        self.replace_all_btn_w = tk.Button(btn_row, text="Replace All", command=self.replace_all)
        theme.style_button(self.replace_all_btn_w, palette, kind="secondary")
        close_btn = tk.Button(btn_row, text="Close", command=self.withdraw)
        theme.style_button(close_btn, palette, kind="secondary")
        close_btn.pack(side=tk.RIGHT)

        self.find_var.trace_add("write", lambda *a: self._recompute_matches())
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self._with_replace = False
        self._apply_mode()
        find_entry.focus_set()

    def show(self, with_replace: bool):
        self._with_replace = with_replace
        self._apply_mode()
        self.deiconify()
        self.lift()
        self._recompute_matches()

    def _apply_mode(self):
        if self._with_replace:
            self.title("Replace")
            self.replace_row_label.grid(row=1, column=0, sticky="w")
            self.replace_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 8))
            self.replace_btn_w.pack(side=tk.LEFT, padx=(0, 4))
            self.replace_all_btn_w.pack(side=tk.LEFT, padx=(0, 12))
        else:
            self.title("Find")
            self.replace_row_label.grid_forget()
            self.replace_entry.grid_forget()
            self.replace_btn_w.pack_forget()
            self.replace_all_btn_w.pack_forget()

    # ---- matching ----
    def _compile_pattern(self):
        needle = self.find_var.get()
        if not needle:
            return None
        if self.regex_var.get():
            pattern = needle
        else:
            pattern = re.escape(needle)
            if self.whole_word_var.get():
                pattern = r"\b" + pattern + r"\b"
        flags = 0 if self.match_case_var.get() else re.IGNORECASE
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            self.match_count_lbl.config(text=f"Invalid regular expression: {e}", fg=self.palette["error"])
            return False  # distinct from None ("no search term yet") - a real compile failure

    def _recompute_matches(self):
        text_widget = self.get_active_text()
        self._matches = []
        self._current_match = -1
        if text_widget is None:
            self.match_count_lbl.config(text="No file open", fg=self.palette["text_muted"])
            return
        text_widget.tag_remove("find_match", "1.0", "end")
        text_widget.tag_remove("find_current", "1.0", "end")
        pattern = self._compile_pattern()
        if pattern is False:
            return  # error already shown by _compile_pattern
        self.match_count_lbl.config(fg=self.palette["text_muted"])
        if pattern is None:
            self.match_count_lbl.config(text="0 matches")
            return
        content = text_widget.get("1.0", "end-1c")
        for m in pattern.finditer(content):
            if m.start() == m.end():
                continue  # a zero-width regex match (e.g. bare "a*") would infinite-loop navigation
            start = f"1.0+{m.start()}c"
            end = f"1.0+{m.end()}c"
            self._matches.append((start, end))
            text_widget.tag_add("find_match", start, end)
        self.match_count_lbl.config(text=f"{len(self._matches)} matches")

    def _select_match(self, index):
        text_widget = self.get_active_text()
        if text_widget is None or not self._matches:
            return
        text_widget.tag_remove("find_current", "1.0", "end")
        self._current_match = index
        start, end = self._matches[index]
        text_widget.tag_add("find_current", start, end)
        text_widget.see(start)
        text_widget.mark_set("insert", start)
        self.match_count_lbl.config(text=f"Match {index + 1} of {len(self._matches)}")

    def find_next(self):
        if not self._matches:
            self._recompute_matches()
        if not self._matches:
            return
        nxt = self._current_match + 1
        if nxt >= len(self._matches):
            if not self.wrap_var.get():
                return
            nxt = 0
        self._select_match(nxt)

    def find_previous(self):
        if not self._matches:
            self._recompute_matches()
        if not self._matches:
            return
        prev = self._current_match - 1
        if prev < 0:
            if not self.wrap_var.get():
                return
            prev = len(self._matches) - 1
        self._select_match(prev)

    # ---- replace (spec sections 16-20, 61-62: file-aware, confirmed, previewable count) ----
    def _replacement_text(self, match_text: str) -> str:
        """Applies regex backreferences ($1/\\1 style) when in regex mode
        by re-matching the single piece of text - safe because this is
        only ever called on text this dialog itself just found."""
        if not self.regex_var.get():
            return self.replace_var.get()
        pattern = self._compile_pattern()
        if not pattern:
            return self.replace_var.get()
        # re.sub understands \1 backreferences directly; also accept the
        # more familiar $1 form by translating it first (spec section 18's
        # own example uses "$1").
        template = re.sub(r"\$(\d+)", r"\\\1", self.replace_var.get())
        m = pattern.match(match_text)
        if not m:
            return self.replace_var.get()
        try:
            return m.expand(template)
        except re.error:
            return self.replace_var.get()

    def replace_one(self):
        text_widget = self.get_active_text()
        if text_widget is None:
            return
        if self._current_match < 0 or self._current_match >= len(self._matches):
            self.find_next()
            return
        start, end = self._matches[self._current_match]
        original = text_widget.get(start, end)
        replacement = self._replacement_text(original)
        text_widget.delete(start, end)
        text_widget.insert(start, replacement)
        text_widget.edit_separator()
        self._recompute_matches()
        self.find_next()

    def replace_all(self):
        text_widget = self.get_active_text()
        if text_widget is None:
            return
        self._recompute_matches()
        n = len(self._matches)
        if n == 0:
            messagebox.showinfo("Replace All", "No matches found.", parent=self)
            return
        # spec section 17: "Before Replace All show: Found: N matches...
        # Replace all N occurrences?" - never a silent mass edit.
        if not messagebox.askyesno("Replace All", f"Found: {n} match(es).\n\nReplace all {n} occurrence(s)?",
                                    parent=self):
            return
        # Apply back-to-front so earlier offsets in `self._matches` stay
        # valid as the buffer shrinks/grows from prior replacements in
        # this same pass.
        #
        # Single undo transaction (spec sections 17/56: "Ctrl+Z must revert
        # that ENTIRE operation... do NOT require N separate undo actions") -
        # edit_separator() alone does NOT retroactively merge prior edits
        # into one group, it only marks a boundary going forward; the
        # correct way to make N delete+insert pairs undo as ONE step is to
        # disable Tk's OWN automatic separator insertion for the duration
        # of the loop (confirmed as a real bug during this feature's own
        # test development: without this, each replacement got its own
        # separate undo step from autoseparators=True, exactly the
        # "N separate undo actions" the spec explicitly forbids).
        text_widget.config(autoseparators=False)
        try:
            text_widget.edit_separator()  # close off whatever came before this transaction
            for start, end in reversed(self._matches):
                original = text_widget.get(start, end)
                replacement = self._replacement_text(original)
                text_widget.delete(start, end)
                text_widget.insert(start, replacement)
            text_widget.edit_separator()  # close off this transaction so the NEXT edit gets its own group
        finally:
            text_widget.config(autoseparators=True)
        self._recompute_matches()
        messagebox.showinfo("Replace All", f"Replaced {n} occurrence(s).", parent=self)
