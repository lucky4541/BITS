"""The in-memory Span model the Verification window's rich-text pane
renders from and edits live.

A Span is one contiguous run of PLAIN TEXT sharing exactly one formatting
combination - the same concept core.text_extractor.extract_lines already
builds internally (its own `runs` list) before `_wrap_run` serializes it
into the `<b>/<i>/<sup>/<sub>/<smallcaps>` tag vocabulary used by the
main extraction pipeline (core.xhtml_writer passes these through
unchanged) and `<bold>/<italic>/...` used by the override pipeline
(core.xhtml_writer renames those to `<b>`/`<i>` before output). This
module does not invent a new markup vocabulary - it only round-trips the
EXISTING one to/from a flat Python list a Tk Text widget can render
without re-parsing tags on every keystroke.

PAGE -> ZONE -> SPAN is the hierarchy actually used (see the plan this
was built from): a "paragraph" in this codebase's own zoning model IS a
Zone (one bbox, one block of reading-order text) - there is no separate
paragraph layer to introduce without duplicating what Zone already is."""
import re
from dataclasses import dataclass, field

_TAG_TOKEN_RE = re.compile(r"(</?[a-z]+>)")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos);|&#\d+;")

# Tag name -> Span boolean field it toggles.  Two sets of synonyms:
#   <b>/<bold>  → bold      (text_extractor._wrap_run emits <b>; the
#   <i>/<italic>→ italic     verification override path emits <bold>/<italic>;
#                            both are accepted here so parse_tagged_text
#                            handles both sources without data loss)
# Plus underline/strike (manual-override-only, see xhtml_writer._INLINE_TAG_MAP).
_TAG_TO_FIELD = {
    "b": "bold", "bold": "bold",
    "i": "italic", "italic": "italic",
    "underline": "underline", "strike": "strike",
    "sup": "superscript", "sub": "subscript", "smallcaps": "small_caps",
}
_FIELD_TO_TAG = {v: k for k, v in _TAG_TO_FIELD.items()}

# Rendering order, innermost first - mirrors _wrap_run's own established
# nesting (sup/sub/smallcaps innermost, then italic, then bold) with the
# two override-only tags added as the outermost layer (their relative
# order to each other/to bold never matters semantically - all four are
# independent boolean properties of the same span, not a real hierarchy).
_RENDER_ORDER = ["superscript", "subscript", "small_caps", "italic", "bold", "underline", "strike"]


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False
    small_caps: bool = False

    def style_key(self) -> tuple:
        return (self.bold, self.italic, self.underline, self.strike,
                self.superscript, self.subscript, self.small_caps)

    def with_text(self, text: str) -> "Span":
        return Span(text, self.bold, self.italic, self.underline, self.strike,
                    self.superscript, self.subscript, self.small_caps)


def _plain_tokens(chunk: str):
    """One indivisible plain-text unit at a time - a whole XML entity or
    one literal character - matching core.text_extractor.strip_tags_to_
    plain's own character counting (see core.verification.inline_style's
    identical helper, kept independent here since span_model must not
    import a GUI-adjacent module for a two-line tokenizer)."""
    i, n = 0, len(chunk)
    while i < n:
        m = _ENTITY_RE.match(chunk, i)
        if m:
            yield m.group(0)
            i = m.end()
        else:
            yield chunk[i]
            i += 1


def parse_tagged_text(tagged_text: str) -> list:
    """Parses text_extractor-style tagged text (e.g. "See <i>Rogers
    v. Rogers</i> for details." or "See <italic>Rogers v. Rogers</italic>
    for details.") into a flat list[Span] - one entry per contiguous
    same-style run.  Both <b>/<i> (from _wrap_run) and <bold>/<italic>
    (from render_tagged_text/style overrides) are recognised as synonyms.
    Unknown/foreign tags are ignored (never crash), so this is safe to
    call on anything extract_zone_formatted_text ever produces."""
    if not tagged_text:
        return []
    from html import unescape
    open_tags = []  # stack of field names currently open
    spans = []
    buffer = []

    def _flush():
        if buffer:
            fields = {_TAG_TO_FIELD[t]: True for t in open_tags if t in _TAG_TO_FIELD}
            spans.append(Span(unescape("".join(buffer)), **fields))
            buffer.clear()

    for token in _TAG_TOKEN_RE.split(tagged_text):
        if not token:
            continue
        if token.startswith("</"):
            _flush()
            name = token[2:-1]
            if name in open_tags:
                open_tags.remove(name)
        elif token.startswith("<"):
            _flush()
            name = token[1:-1]
            if name in _TAG_TO_FIELD:
                open_tags.append(name)
            # an unrecognized tag is silently ignored (never opened/closed
            # against the stack) - safe default for markup this module
            # doesn't model, rather than corrupting the style stack.
        else:
            buffer.append(token)
    _flush()
    return spans


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_spans_to_tagged_text(spans: list) -> str:
    """The exact inverse of parse_tagged_text - round-trips through the
    same tag vocabulary, so feeding this module's own output back into
    extract_zone_formatted_text-consuming code (xhtml_writer, inline_
    style) behaves identically to the original extraction output."""
    out = []
    for span in spans:
        if not span.text:
            continue
        text = _xml_escape(span.text)
        for field in _RENDER_ORDER:
            if getattr(span, field):
                tag = _FIELD_TO_TAG[field]
                text = f"<{tag}>{text}</{tag}>"
        out.append(text)
    return "".join(out)


def plain_text_of(spans: list) -> str:
    return "".join(s.text for s in spans)


def split_spans_at(spans: list, start: int, end: int) -> list:
    """Splits whichever span(s) straddle the [start, end) character
    boundary (measured in plain-text offsets across the whole list) into
    up to 3 pieces each, so a style can be applied to EXACTLY that range
    without touching a single character outside it - spec's own "split
    span if necessary" requirement. Returns a NEW list; does not mutate
    the input."""
    if start >= end:
        return list(spans)
    result = []
    pos = 0
    for span in spans:
        span_start, span_end = pos, pos + len(span.text)
        pos = span_end
        if span_end <= start or span_start >= end:
            result.append(span)
            continue
        cut_start = max(start, span_start) - span_start
        cut_end = min(end, span_end) - span_start
        if cut_start > 0:
            result.append(span.with_text(span.text[:cut_start]))
        result.append(span.with_text(span.text[cut_start:cut_end]))
        if cut_end < len(span.text):
            result.append(span.with_text(span.text[cut_end:]))
    return result


def merge_adjacent_spans(spans: list) -> list:
    """Coalesces consecutive spans sharing the EXACT SAME style
    combination into one - a real, confirmed rendering-verbosity
    artifact of split_spans_at + apply_style_to_range: adding bold to
    "v." inside an existing native <italic>Rogers v. Rogers</italic> run
    splits it into "Rogers " / "v." (bold+italic) / " Rogers" - the
    first and third pieces both end up plain-italic, and without this
    merge they'd render as TWO SEPARATE <italic>...</italic> pairs
    around the bold word instead of one continuous italic run with the
    bold nested inside it (spec: "sanitize/normalize formatting spans so
    that accidental...formatting does not survive"). Purely cosmetic -
    plain_text_of(spans) is always identical before and after (the same
    characters in the same order), never a semantic change, and never
    merges across a genuine style difference."""
    if not spans:
        return []
    merged = [spans[0]]
    for span in spans[1:]:
        last = merged[-1]
        if span.style_key() == last.style_key():
            merged[-1] = last.with_text(last.text + span.text)
        else:
            merged.append(span)
    return merged


def apply_style_to_range(spans: list, start: int, end: int, field: str, value: bool = True) -> list:
    """split_spans_at + toggling one style field onto every resulting
    span whose text falls entirely within [start, end) - the whole
    "apply this style to the current selection" operation in one call.
    `field` is a Span attribute name (e.g. "bold", "superscript").
    Adjacent same-style results are merged back together (see
    merge_adjacent_spans) so this never fragments an existing run into
    more separate spans than the actual styling requires."""
    split = split_spans_at(spans, start, end)
    result = []
    pos = 0
    for span in split:
        span_start, span_end = pos, pos + len(span.text)
        pos = span_end
        if span_start >= start and span_end <= end:
            kwargs = {f: getattr(span, f) for f in _FIELD_TO_TAG}
            kwargs[field] = value
            result.append(Span(span.text, **kwargs))
        else:
            result.append(span)
    return merge_adjacent_spans(result)


def style_at_range(spans: list, start: int, end: int) -> dict:
    """Returns {field: True/False/"mixed"} for every style field across
    the [start, end) selection - drives the toolbar's own active/mixed
    button-state reflection (spec: "selection containing mixed formatting
    -> button shows mixed/neutral state")."""
    fields = list(_FIELD_TO_TAG.keys())
    if start >= end:
        return {f: False for f in fields}
    values = {f: set() for f in fields}
    pos = 0
    for span in spans:
        span_start, span_end = pos, pos + len(span.text)
        pos = span_end
        if span_end <= start or span_start >= end:
            continue
        for f in fields:
            values[f].add(getattr(span, f))
    result = {}
    for f in fields:
        vs = values[f]
        result[f] = "mixed" if len(vs) > 1 else (next(iter(vs)) if vs else False)
    return result
