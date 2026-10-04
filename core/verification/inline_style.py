"""Manual inline-style overrides for the Verification window's Style
Editor (spec: "select specific extracted text and apply an inline style
... to the selected text only ... do not alter surrounding text ... do
not flatten existing formatting").

A Zone has no notion of "manually overridden formatting" today -
core.text_extractor.extract_zone_formatted_text derives bold/italic/sup/
sub/smallcaps entirely from live PDF font data (or OCR/visual analysis)
every time it runs; nothing is stored as markup on the zone itself. This
module adds that missing piece ADDITIVELY: overrides are stored as plain
data (character ranges + style values, relative to the zone's own PLAIN
text) in zone.attributes["style_overrides"] - the same freely-extensible
dict every other per-zone flag in this app already uses (attributes
["source"], ["manual_text"], etc.) - and applied as a POST-PROCESSING
pass over the ALREADY-COMPUTED, already-tagged extraction result, never
by touching text_extractor.py's own per-character detection loop. This
keeps the (delicate, heavily-exercised) automatic detection code
completely untouched: when no zone has any override, apply_style_
overrides is a no-op and the pipeline behaves exactly as it always has.

CONFIRMED BUG FIXED HERE: the previous implementation stored an override
as bare style NAMES (["italic"]) and could only ever OPEN a tag for a
listed name - there was no way to express "force this OFF", so removing
an already-italic word's italic in the Verification window updated the
live in-memory display (span_model's own spans, correct) but persisted
an override that still said "italic belongs here", which is a no-op on
text already native-italic - the word silently stayed italic through any
FRESH re-extraction (reopening Verification, Generate XHTML, ...). Fixed
by reusing core.verification.span_model's own parse/apply/render round-
trip (the exact same, already-tested logic the live editor's instant
toggle uses) instead of a second, separately-maintained, add-only tag-
insertion algorithm - now an override records an explicit {field:
True|False} value per style and can express "OFF" just as validly as
"ON". Storage format is now {"start", "end", "styles": {name: bool}};
normalize_overrides transparently upgrades an OLDER, already-persisted
{"styles": [name, ...]} list (pre-fix format, meaning "set these True")
so no existing project file needs migrating.

A second, related bug fixed in the same pass: the old STYLE_TAG_NAMES
vocabulary used "strikethrough"/"smallcaps" while every other caller
(gui/verification_window.py's own toolbar fields, core.verification.
span_model's own Span fields) uses "strike"/"small_caps" - the mismatch
meant normalize_overrides silently DROPPED every Strike/Small Caps
override before it could ever be stored, for any project, since Pass 1.
Reusing span_model's own field vocabulary directly (ALL_STYLE_NAMES,
below) eliminates the second, independently-named list entirely."""
from core.verification import span_model as sm

# The exact field names of core.verification.span_model.Span (its own
# authoritative vocabulary, reused verbatim rather than re-derived) -
# also exactly what gui/verification_window.py's own toolbar buttons use.
ALL_STYLE_NAMES = ("bold", "italic", "underline", "strike", "superscript", "subscript", "small_caps")


def normalize_overrides(overrides: list) -> list:
    """Drops empty/invalid ranges and clamps styles to the known set -
    never trusts a caller-supplied override blindly. Transparently
    upgrades the OLDER, pre-fix {"styles": [name, ...]} list format
    (meaning "set these True") to the current {"styles": {name: bool}}
    dict format, so an already-persisted project file's own overrides
    keep working unchanged rather than needing a data migration."""
    out = []
    for ov in overrides or []:
        start, end = ov.get("start"), ov.get("end")
        if start is None or end is None or end <= start:
            continue
        raw_styles = ov.get("styles")
        if isinstance(raw_styles, dict):
            styles = {k: bool(v) for k, v in raw_styles.items() if k in ALL_STYLE_NAMES}
        else:
            styles = {k: True for k in (raw_styles or []) if k in ALL_STYLE_NAMES}
        if not styles:
            continue
        out.append({"start": int(start), "end": int(end), "styles": styles})
    return out


def apply_style_overrides(tagged_text: str, overrides: list) -> str:
    """tagged_text: the string extract_zone_formatted_text/_with_breaks
    already produced (bold/italic/sup/sub/smallcaps tags from automatic
    detection, exactly as today). overrides: zone.attributes.get(
    "style_overrides", []), each {"start": int, "end": int, "styles":
    {name: bool, ...}} in PLAIN-TEXT character offsets (i.e. offsets
    into text_extractor.strip_tags_to_plain(tagged_text) - the same text
    the Verification window's editable pane shows and the operator makes
    a selection in). Applied in stored order via span_model.apply_
    style_to_range, so a later override always wins over an earlier one
    for any range they both touch - correct behavior for a zone with
    several historical overrides on overlapping ranges."""
    overrides = normalize_overrides(overrides)
    if not overrides or not tagged_text:
        return tagged_text
    spans = sm.parse_tagged_text(tagged_text)
    for ov in overrides:
        for field, value in ov["styles"].items():
            spans = sm.apply_style_to_range(spans, ov["start"], ov["end"], field, value)
    return sm.render_spans_to_tagged_text(spans)


def add_override(zone, start: int, end: int, styles):
    """Adds one override to zone.attributes["style_overrides"] (creating
    the list if absent) - the one mutation point the Verification
    window's Style Editor calls on Apply. `styles` is normally a dict
    {field_name: bool} (explicit set/unset, e.g. {"italic": False} to
    remove italic); a bare list/tuple of field names is also accepted
    for any older call site and treated as "set these to True", matching
    normalize_overrides's own backward-compatible reading."""
    if end <= start or not styles:
        return
    if not isinstance(styles, dict):
        styles = {name: True for name in styles}
    existing = zone.attributes.setdefault("style_overrides", [])
    existing.append({"start": int(start), "end": int(end), "styles": dict(styles)})
    zone.attributes["style_overrides"] = normalize_overrides(existing)


def clear_overrides(zone):
    zone.attributes.pop("style_overrides", None)
