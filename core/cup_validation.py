"""Mandatory-zone validation for the CUPEPUB profile, driven entirely by
CUPEPUB_ZoneValidation.xml's own Mandatory="true" list and per-name
attributes (core/cup_config.py's load_mandatory_zones) - no hardcoded tag
names anywhere in here.

IMPORTANT: "Mandatory=true" does NOT mean "this zone must exist somewhere
in every document" - a first version of this file read it that way and it
was wrong (a normal H1+Para document was blocked for lacking PageNum/
Box_Start/Box_End/ParaStart/ParaEnd, none of which apply unless the
document actually uses that structure). The correct, config-driven reading
is CONDITIONAL:

  - A name that is one half of a Start/End structural pair (detected purely
    by NAME SHAPE - "X_Start"/"X_End" or "XStart"/"XEnd", not a hardcoded
    "Box"/"Para" special case) is only checked when at least one zone of
    EITHER half is present in the document. If neither exists, the whole
    structure is simply unused - not an error. If one exists, every Start
    must have a matching End and vice versa, checked in true document
    reading order (page, then serial/created_order) via a stack, so
    multiple boxes and interleaving are handled correctly, not just "does
    at least one of each exist somewhere".
  - A standalone name (no Start/End counterpart by name shape - e.g.
    PageNum) is likewise never reported missing merely for being absent.
    It's only ever validated (using whatever structural attribute the
    config itself defines, e.g. validate_emptyzone) against whichever
    zones of that type actually exist; zero such zones means zero checks
    and zero issues, by construction.

CUPEPUB's designated page-number marker name (core/cup_config.py's
cup_pagenum_name, passed in as pagenum_name) is EXCLUDED from this
blocking pre-generation pass entirely - a DELIBERATE, explicit override
of whatever CUPEPUB_ZoneValidation.xml's own validate_emptyzone attribute
says for it. Two things changed here across two rounds of correction:
first, PageNum stopped being treated as "must be an empty marker" (its
real role is "carries the actual printed page number" -
core/epub_xml_generator.py's _gen_pagenum_zone uses that text as the EPUB
page value); then, an empty PageNum stopped being a BLOCKING error at all
(scanned/OCR PDFs routinely produce an empty or garbage value that the
user fixes via the manual PageNum editor AFTER seeing the generated
output, not before) - _gen_pagenum_zone now silently skips an empty
PageNum's pagebreak and reports it as a non-blocking generation warning
instead, so this function has nothing left to check for it. The override
is scoped to whichever name the profile itself identifies as the
page-marker concept, not a bare literal "PageNum" string, so it stays
config-driven rather than a hardcoded tag check.

Matched against each zone's ORIGINAL CUP tag name (zone.attributes
["cup_name"], stamped by core/cup_config.build_cup_profile), since the
mandatory list is written in CUP-name vocabulary, not output element names.
"""
import re

_START_RE = re.compile(r"^(.*?)(_?)Start$")
_END_RE = re.compile(r"^(.*?)(_?)End$")


def _pair_for(name):
    """Returns (start_name, end_name) if `name` participates in a
    Start/End structural pair by naming convention (e.g. "Box_Start" /
    "Box_End", "ParaStart" / "ParaEnd"), else None. Purely a string-shape
    test - works for any current or future paired marker name without
    listing any of them here."""
    m = _START_RE.match(name)
    if m:
        base, sep = m.group(1), m.group(2)
        return (name, f"{base}{sep}End")
    m = _END_RE.match(name)
    if m:
        base, sep = m.group(1), m.group(2)
        return (f"{base}{sep}Start", name)
    return None


def _document_order(zone_manager):
    """Whole-document reading order (page, then per-page serial, then
    creation order as a tiebreak for zones not yet normalized) - a
    self-contained sort over ALL zones (not just top-level ones), since a
    Start/End marker pair is not guaranteed to be top-level."""
    return sorted(
        zone_manager.zones.values(),
        key=lambda z: (z.page, z.serial if z.serial is not None else 10 ** 9, z.created_order),
    )


def _validate_pair(ordered_zones, by_cup_name, start_name, end_name):
    if not by_cup_name.get(start_name) and not by_cup_name.get(end_name):
        return []  # structure not used in this document at all - not an error
    issues = []
    open_stack = []
    for zone in ordered_zones:
        cup_name = zone.attributes.get("cup_name")
        if cup_name == start_name:
            open_stack.append(zone)
        elif cup_name == end_name:
            if open_stack:
                open_stack.pop()
            else:
                issues.append(f"{end_name} on page {zone.page} has no matching {start_name}.")
    for zone in open_stack:
        issues.append(f"Missing {end_name} for {start_name} on page {zone.page}.")
    return issues


def _validate_standalone(name, zones_present, meta, pagenum_name=None):
    if not zones_present:
        return []  # never required merely for being absent
    issues = []
    if name == pagenum_name:
        # PageNum is excluded from blocking pre-generation validation
        # entirely - see the module docstring. An empty PageNum zone is
        # valid (it stays in the project, editable) and is reported as a
        # non-blocking warning at GENERATION time instead (core/
        # epub_xml_generator.py._gen_pagenum_zone), never here.
        return []
    if meta.get("validate_emptyzone"):
        for zone in zones_present:
            if (zone.text or "").strip():
                issues.append(f"{name} on page {zone.page} should be an empty marker zone but contains text.")
    return issues


def validate_mandatory_zones(zone_manager, mandatory_names, mandatory_meta=None, pagenum_name=None) -> list:
    if not mandatory_names:
        return []
    mandatory_meta = mandatory_meta or {}
    ordered_zones = _document_order(zone_manager)
    by_cup_name = {}
    for zone in ordered_zones:
        cup_name = zone.attributes.get("cup_name")
        if cup_name:
            by_cup_name.setdefault(cup_name, []).append(zone)

    issues = []
    handled_pairs = set()
    for name in mandatory_names:
        pair = _pair_for(name)
        if pair:
            if pair in handled_pairs:
                continue
            handled_pairs.add(pair)
            issues.extend(_validate_pair(ordered_zones, by_cup_name, *pair))
        else:
            issues.extend(_validate_standalone(
                name, by_cup_name.get(name, []), mandatory_meta.get(name, {}), pagenum_name))
    return issues
