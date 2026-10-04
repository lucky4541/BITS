"""Config-driven loader for the CUPEPUB profile.

Reads the CUP client's own XML configuration files (profiles/CUPEPUB/*.xml -
copies of the files the user supplied, renamed per the agreed CUPEPUB_*
naming but otherwise byte-identical, root elements untouched) and synthesizes
a profile dict in the exact same shape core/profile_manager.py already
returns for XML/EPUB (tag_buttons, tag_colors, image_kinds, component_types,
mapping_xml_path, xml_enabled, xhtml_enabled, root_tag, default_component_type)
plus a few CUPEPUB-only extra keys (cup_*) that generic code simply never
looks at. This is the ONLY place CUP tag names are read - nothing here is a
per-tag if/else; every tag, shortcut, attribute and image-kind comes from
parsing CUPEPUB_Zoning.xml + CUPLookup.xml generically.

Design that lets the rest of the app stay unchanged: CUPLookup.xml's
<item name="X" fname="Y" attribute="A___B"/> is translated directly into a
tag_buttons entry {"label": "X", "tag": "Y", "attrs": {"A": "B", ...}} - so
core/epub_xml_generator.py's existing "zone.tag IS the output element name,
zone.attributes are copied onto it" logic (unchanged) already produces
exactly the intermediate element Mapping.xml's own rules expect (e.g.
Para_NoIndent -> <p type="noindent"> -> Mapping.xml's //para[@type] rule ->
<p class="noindent">), with zero new per-tag Python code.

CUPEPUB_Master.xml (supplied) is not valid XML - confirmed by direct
inspection (a single ~6KB line of base64-like text, not parseable as XML at
all). Per the user's explicit decision: do NOT invent/decode/reconstruct it.
CUPEPUB_AutoStyling.xml's own (valid) auto-styling rules are used as a
temporary fallback for the "Master rules" slot ONLY while Master.xml stays
invalid - both files are still read from their own independent paths/slots
(see load_master_and_autostyling), so a corrected Master.xml dropped in
later is picked up automatically on the next profile load/reload, with no
persisted/duplicated copy of AutoStyling's rules ever written anywhere.
"""
import os
import re

from lxml import etree

from core.resource_path import resource_path
from core.constants import TAG_TABLE

PROFILE_NAME = "CUPEPUB"

# The CUP client's own convention name (SPiZONE-Edit_CUP.xml, Mandatory in
# CUPEPUB_ZoneValidation.xml) for the physical page-number marker zone -
# recognized by name (not by its translated output tag, which CUPLookup.xml
# happens to set to "pagenum" - a name that unrelatedly collides with a
# different Mapping.xml rule, see EpubXmlGenerator._gen_pagenum_zone).
# Exposed on the built profile as cup_pagenum_name, and only set at all if
# this literal name actually exists in the loaded zoning config (spec 55/67
# - never assumed).
PAGENUM_CUP_NAME = "PageNum"

# The CUP client's own convention names (SPiZONE-Edit_CUP.xml) for the
# box-container structural start/end markers - Box_Start/Box_End have no
# CUPLookup.xml entry at all, so they fall back to their literal lowercased
# name as their output tag ("box_start"/"box_end" - see the unmapped-tag
# fallback in build_cup_profile below). Recognized by CUP name, same
# pattern as PAGENUM_CUP_NAME above, and only activated if both names
# actually exist in the loaded zoning config.
BOX_START_CUP_NAME = "Box_Start"
BOX_END_CUP_NAME = "Box_End"

# Client image-naming convention (spec: "CUPEPUB - OUTPUT FOLDER + FILE
# NAMING STANDARD", sections 9/10/16) - explicit short tokens for every
# image-property CUP zoning name confirmed present in CUPEPUB_Zoning.xml
# (CoverImage/PubLogo/CopyLogo/Image/Img_map/Img_illus/Img_photo/
# Img_chart/Img_graph/Img_diagram/IconFig/TblImage/UnCapTblImage/
# Eqn_img/ProImage - verified directly against the real zoning file, not
# assumed). Without this table, build_cup_profile's generic derivation
# (lowercase the CUP name, strip a leading "img_") would produce long,
# non-conforming tokens like "coverimage"/"tblimage"/"iconfig" instead of
# the client's mandated "cover"/"tbl"/"icon" - confirmed as a real gap
# (see the investigation this table's introduction was based on).
#
# The six visual FIGURE SUBTYPES the zoning tool offers (map/illustration/
# photo/chart/graph/diagram) all collapse onto ONE shared "fig" counter
# via counter_key="image" - the client spec only ever defines a single
# generic "fig" sequence (section 9's "FIGURE" naming rule), never
# separate per-subtype tokens, so e.g. a diagram and a photo appearing in
# reading order both count against the same ch1-fig-NN sequence rather
# than each starting its own independent (and colliding) "-01".
_CLIENT_IMAGE_TOKEN_OVERRIDES = {
    "coverimage":     {"token": "cover", "ext": "jpg", "digits": 0, "no_prefix": True},
    "publogo":        {"token": "logo",  "ext": "jpg", "digits": 0},
    "copylogo":       {"token": "logo",  "ext": "jpg", "digits": 0},
    "image":          {"token": "fig",   "ext": "png", "digits": 2},
    "img_map":        {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "img_illus":      {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "img_photo":      {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "img_chart":      {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "img_graph":      {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "img_diagram":    {"token": "fig",   "ext": "png", "digits": 2, "counter_key": "image"},
    "iconfig":        {"token": "icon",  "ext": "png", "digits": 2},
    "tblimage":       {"token": "tbl",   "ext": "png", "digits": 2},
    "uncaptblimage":  {"token": "tblu",  "ext": "png", "digits": 2},
    "eqn_img":        {"token": "eqn",   "ext": "png", "digits": 2},
    "proimage":       {"token": "codfig", "ext": "png", "digits": 2},
}

CUP_DIR = resource_path("profiles", "CUPEPUB")

PROFILE_XML = os.path.join(CUP_DIR, "CUPEPUB_Profile.xml")
ZONING_XML = os.path.join(CUP_DIR, "CUPEPUB_Zoning.xml")
MASTER_XML = os.path.join(CUP_DIR, "CUPEPUB_Master.xml")
AUTOSTYLING_XML = os.path.join(CUP_DIR, "CUPEPUB_AutoStyling.xml")
CHARACTER_XML = os.path.join(CUP_DIR, "CUPEPUB_Character.xml")
ZONEVALIDATION_XML = os.path.join(CUP_DIR, "CUPEPUB_ZoneValidation.xml")
LOOKUP_XML = os.path.join(CUP_DIR, "CUPLookup.xml")
MAPPING_XML = os.path.join(CUP_DIR, "Mapping.xml")

_ZONING_CATEGORIES = [
    ("frontmatter", "Frontmatter"),
    ("bodypart", "Body"),
    ("backmatter", "Backmatter"),
    ("float", "Float"),
]


class CupConfigError(Exception):
    """Raised only for the files CUPEPUB cannot function at all without
    (Zoning + CUPLookup + Mapping.xml) - everything else degrades its own
    feature independently and is reported via diagnostics instead."""


def _parse_bool(value) -> bool:
    return str(value or "").strip().lower() == "yes"


def _safe_parse(path: str):
    """Returns (root_or_None, status_string). Never raises - every caller
    decides for itself whether its particular file is essential (raise
    CupConfigError) or optional (degrade + report), per spec section 55/67."""
    if not path or not os.path.isfile(path):
        return None, f"NOT FOUND ({path})"
    try:
        parser = etree.XMLParser(remove_comments=True, recover=False)
        tree = etree.parse(path, parser)
        return tree.getroot(), "Loaded"
    except etree.XMLSyntaxError as e:
        return None, f"INVALID - not well-formed XML (line {e.lineno}: {e.msg})"
    except OSError as e:
        return None, f"NOT FOUND ({e})"


# ------------------------------------------------------------------ zoning
def load_zoning(path: str = ZONING_XML):
    """Returns {"frontmatter": [...], "bodypart": [...], "backmatter": [...],
    "float": [...]}, each item a dict with the item's own text (the CUP tag
    name) plus every attribute CUPEPUB_Zoning.xml defines for it - nothing
    assumed beyond what's literally present."""
    root, status = _safe_parse(path)
    if root is None:
        raise CupConfigError(f"CUPEPUB_Zoning.xml: {status}")
    styles_el = root.find("styles")
    if styles_el is None:
        raise CupConfigError("CUPEPUB_Zoning.xml: no <styles> element found.")
    result = {}
    for key, _label in _ZONING_CATEGORIES:
        cat_el = styles_el.find(key)
        items = []
        if cat_el is not None:
            for item_el in cat_el.findall("item"):
                name = (item_el.text or "").strip()
                if not name:
                    continue
                items.append({
                    "name": name,
                    "shortcutKey": item_el.get("shortcutKey", ""),
                    "usectrl": _parse_bool(item_el.get("usectrl")),
                    "useshift": _parse_bool(item_el.get("useshift")),
                    "usealt": _parse_bool(item_el.get("usealt")),
                    "subelement": _parse_bool(item_el.get("subelement")),
                    "property": item_el.get("property"),
                })
        result[key] = items
    return result, status


# ------------------------------------------------------------------ lookup
def load_lookup(path: str = LOOKUP_XML):
    """Returns (lookup_dict, diagnostics_list). lookup_dict[name] =
    {"fname":.., "attr_name": .. or None, "attr_value": .. or None,
    "type": .. or None}. attribute="A___B" -> attr_name="A", attr_value="B";
    attribute="-" (or absent) -> no attribute. A duplicate <item name="X">
    keeps its FIRST occurrence (matching normal top-to-bottom precedence)
    and is reported in diagnostics rather than silently overwritten -
    CUPLookup.xml as supplied has one such duplicate (IndexSE)."""
    root, status = _safe_parse(path)
    if root is None:
        raise CupConfigError(f"CUPLookup.xml: {status}")
    lookup = {}
    diagnostics = []
    for item_el in root.findall("item"):
        name = item_el.get("name")
        if not name:
            continue
        if name in lookup:
            diagnostics.append(
                f"CUPLookup.xml: duplicate <item name=\"{name}\"> - keeping the first "
                f"occurrence (fname={lookup[name]['fname']!r}), ignoring this one "
                f"(fname={item_el.get('fname')!r}).")
            continue
        fname = item_el.get("fname") or name.lower()
        attribute = item_el.get("attribute") or "-"
        attr_name = attr_value = None
        if attribute != "-" and "___" in attribute:
            attr_name, _, attr_value = attribute.partition("___")
        lookup[name] = {
            "fname": fname,
            "attr_name": attr_name,
            "attr_value": attr_value,
            "type": item_el.get("type"),
        }
    return lookup, diagnostics, status


# ------------------------------------------------------------- master/auto
def _load_style_pattern_rules(root):
    """CUPEPUB_Master.xml / CUPEPUB_AutoStyling.xml share one shape:
    <Item type="startwith_string|startwith_regex" startstring="..."
    stylename="..."/>. Order preserved (later rules must not silently
    reorder ahead of earlier ones - same principle as Mapping.xml)."""
    rules = []
    for item_el in root.findall("Item"):
        rules.append({
            "type": item_el.get("type", ""),
            "startstring": item_el.get("startstring", ""),
            "stylename": item_el.get("stylename", ""),
        })
    return rules


def load_master_and_autostyling(master_path: str = MASTER_XML, autostyling_path: str = AUTOSTYLING_XML):
    """Returns (effective_rules, diagnostics_dict). Master and AutoStyling
    are two INDEPENDENT slots (per user's decision, both stay independently
    reloadable from their own paths - no file is ever duplicated/generated
    on disk): if Master.xml fails to parse, AutoStyling.xml's own rules are
    used as the effective Master ruleset until a valid Master.xml appears,
    at which point it automatically takes precedence again on the next
    reload (this function has no persisted state of its own)."""
    master_root, master_status = _safe_parse(master_path)
    auto_root, autostyling_status = _safe_parse(autostyling_path)

    master_rules = _load_style_pattern_rules(master_root) if master_root is not None else None
    autostyling_rules = _load_style_pattern_rules(auto_root) if auto_root is not None else None

    if master_rules:
        effective_rules = master_rules
        effective_source = "Master"
    elif autostyling_rules:
        effective_rules = autostyling_rules
        effective_source = "AutoStyling (fallback - Master.xml invalid/missing)"
    else:
        effective_rules = []
        effective_source = "NONE - both Master and AutoStyling unavailable"

    diagnostics = {
        "master_status": master_status,
        "autostyling_status": autostyling_status,
        "effective_source": effective_source,
        "effective_rule_count": len(effective_rules),
    }
    return effective_rules, diagnostics


# --------------------------------------------------------------- character
def load_character_map(path: str = CHARACTER_XML):
    """Returns (per_font_dict, merged_dict, status). per_font_dict[font_name]
    = [(find, replace), ...] in file order, preserved for a future
    font-aware caller. merged_dict is a flattened find->replace dict used by
    apply_character_map below, since core/text_extractor.py's public API
    returns a plain formatted-text string with no per-character font
    identity attached - true font-scoped substitution isn't possible at
    that boundary today; this is the documented, honest fallback, not a
    silent behavior change dressed up as the real thing."""
    root, status = _safe_parse(path)
    if root is None:
        return {}, {}, status
    per_font = {}
    merged = {}
    for font_el in root.findall("font"):
        font_name = font_el.get("name", "")
        pairs = []
        for ch_el in font_el.findall("Character"):
            find = ch_el.get("Find")
            replace = ch_el.get("Replace")
            if find:
                pairs.append((find, replace or ""))
                merged[find] = replace or ""
        per_font[font_name] = pairs
    return per_font, merged, status


def apply_character_map(root_element, char_map: dict) -> int:
    """Walks every element's .text/.tail in root_element's subtree and
    applies char_map's find->replace substitutions. Returns the number of
    elements touched. No-op (and never called) for any profile that doesn't
    supply a character map, so XML/EPUB text is never touched by this."""
    if not char_map:
        return 0
    touched = 0
    for el in root_element.iter():
        changed = False
        if el.text:
            new_text = el.text
            for find, replace in char_map.items():
                if find in new_text:
                    new_text = new_text.replace(find, replace)
                    changed = True
            if changed:
                el.text = new_text
        if el.tail:
            new_tail = el.tail
            tail_changed = False
            for find, replace in char_map.items():
                if find in new_tail:
                    new_tail = new_tail.replace(find, replace)
                    tail_changed = True
            if tail_changed:
                el.tail = new_tail
                changed = True
        if changed:
            touched += 1
    return touched


# -------------------------------------------------------------- validation
def load_mandatory_zones(path: str = ZONEVALIDATION_XML):
    """Returns (list_of_names, meta_by_name, status). Reads every <Style
    Name="X" Mandatory="true" validate_emptyzone=".." .../> - the Mandatory
    list, and every other attribute the file defines per name, is exactly
    and only what this file says. meta_by_name[name] carries those raw
    attributes (as bools) for core/cup_validation.py's generic structural
    checks to consult - nothing here decides what any of them MEAN, since
    "Mandatory=true" turning into a document-wide existence requirement (the
    original, wrong reading) vs. a conditional/structural rule is entirely
    core/cup_validation.py's job."""
    root, status = _safe_parse(path)
    if root is None:
        return [], {}, status
    names = []
    meta = {}
    for style_el in root.findall("Style"):
        if str(style_el.get("Mandatory", "")).strip().lower() == "true":
            name = style_el.get("Name")
            if name:
                names.append(name)
                meta[name] = {
                    "validate_emptyzone": str(style_el.get("validate_emptyzone", "")).strip().lower() == "true",
                    "validate_associate_text": str(style_el.get("Validate_AssociateText", "")).strip().lower() == "true",
                }
    return names, meta, status


# ------------------------------------------------------------------ profile id
def load_project_id(path: str = PROFILE_XML):
    root, status = _safe_parse(path)
    if root is None:
        return "CUP", status
    proj_el = root.find("project")
    project_id = (proj_el.text or "").strip() if proj_el is not None else ""
    return project_id or "CUP", status


# ------------------------------------------------------- component types
_COMPONENT_TYPE_RE = re.compile(r"component\[@type=(['\"])([^'\"]+)\1\]")


def derive_component_types(mapping_path: str = MAPPING_XML):
    """Component types (chapter/part/toc/glossary/...) are never listed
    anywhere in the CUP zoning config - they're a Mapping.xml-level concept
    (which top-level <component type="..."> wrapper a whole generated file
    represents). Rather than hand-typing a second copy of that list (which
    would silently drift the moment Mapping.xml gains/loses one), it's
    derived directly from every distinct component[@type='X'] appearing in
    Mapping.xml's own find= expressions - Mapping.xml stays the single
    source of truth, same principle as every other CUPEPUB config file."""
    if not mapping_path or not os.path.isfile(mapping_path):
        return []
    try:
        with open(mapping_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []
    seen = []
    for match in _COMPONENT_TYPE_RE.finditer(content):
        value = match.group(2)
        if value not in seen:
            seen.append(value)
    return seen


# ------------------------------------------------------------- tag colors
def _generate_tag_colors(ordered_fnames):
    """No color data is supplied anywhere in the CUP configuration - a
    deterministic HSV wheel (evenly spaced by position, fixed saturation/
    value for consistent legibility) gives every distinct output element
    name its own stable color across app restarts, without hand-picking or
    hardcoding any specific tag's color."""
    import colorsys
    colors = {}
    n = max(len(ordered_fnames), 1)
    for i, fname in enumerate(ordered_fnames):
        if fname in colors:
            continue
        hue = (i / n) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.75)
        colors[fname] = "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))
    return colors


# --------------------------------------------------------------- shortcuts
def _shortcut_seqs(item):
    """Builds Tk bind sequences for one zoning item's (usectrl, useshift,
    usealt, shortcutKey) combination. Two casings are bound for any letter
    key (matching this codebase's own existing precedent in
    gui/main_window.py._bind_keys for Ctrl-Shift-R/C/X - Tk's Shift-modifier
    keysym casing for letter keys isn't fully consistent across platforms),
    since a single physical key can legitimately appear multiple times in
    CUPEPUB_Zoning.xml under different modifier combinations - each becomes
    its own, fully independent bind sequence keyed on (ctrl, shift, alt,
    key), never a plain key->tag lookup that would collide."""
    key = (item.get("shortcutKey") or "").strip()
    if not key:
        return []
    mods = []
    if item.get("usectrl"):
        mods.append("Control")
    if item.get("useshift"):
        mods.append("Shift")
    if item.get("usealt"):
        mods.append("Alt")
    if key.isalpha():
        forms = {key.upper(), key.lower()}
    else:
        forms = {key}
    seqs = []
    for form in forms:
        parts = mods + [form]
        seqs.append("<" + "-".join(parts) + ">")
    return seqs


# ------------------------------------------------------------ build profile
def build_cup_profile() -> dict:
    zoning, zoning_status = load_zoning()
    lookup, lookup_diag, lookup_status = load_lookup()
    master_rules, master_diag = load_master_and_autostyling()
    per_font_chars, merged_chars, character_status = load_character_map()
    mandatory_zones, mandatory_meta, validation_status = load_mandatory_zones()
    project_id, profile_id_status = load_project_id()
    component_types = derive_component_types()

    if not os.path.isfile(MAPPING_XML):
        raise CupConfigError(f"Mapping.xml: NOT FOUND ({MAPPING_XML})")

    tag_buttons = []
    tag_groups = []
    cup_shortcuts = []
    unmapped_tags = []
    ordered_fnames = []
    all_cup_names = {item["name"] for items in zoning.values() for item in items}
    pagenum_cup_name = PAGENUM_CUP_NAME if PAGENUM_CUP_NAME in all_cup_names else None

    for cat_key, cat_label in _ZONING_CATEGORIES:
        items = zoning.get(cat_key, [])
        if not items:
            continue
        tag_groups.append({"label": cat_label, "first": items[0]["name"]})
        for item in items:
            cup_name = item["name"]
            entry = lookup.get(cup_name)
            if entry:
                fname = entry["fname"]
                attr_name, attr_value, ltype = entry["attr_name"], entry["attr_value"], entry["type"]
            else:
                fname = cup_name.lower()
                attr_name = attr_value = ltype = None
                unmapped_tags.append(cup_name)

            attrs = {"cup_name": cup_name}
            is_image = (item.get("property") == "image") or (ltype == "image")
            if is_image:
                asset_kind = cup_name.lower()
                attrs["asset_kind"] = asset_kind
                if attr_name == "type" and attr_value:
                    attrs["img_type"] = attr_value
            elif attr_name and attr_value:
                attrs[attr_name] = attr_value

            tag_buttons.append({"label": cup_name, "tag": fname, "attrs": attrs})
            ordered_fnames.append(fname)
            cup_shortcuts.append({
                "cup_name": cup_name, "tag": fname, "attrs": dict(attrs),
                "seqs": _shortcut_seqs(item),
            })

        if cat_key == "bodypart" and not any(b["tag"] == TAG_TABLE for b in tag_buttons):
            # "Table Draw" - a synthetic button reusing the SAME real
            # grid-capable "table" tag both generators (core.
            # xml_generator._zone_table and core.epub_xml_generator.
            # _gen_table_zone) already fully support via core.
            # table_extractor's own geometry-based row/column/cell/
            # rowspan/colspan detection. CUPEPUB's own external Zoning
            # config has no cup_name mapped to that tag - its own real
            # Table entry ("tblimage") is a DIFFERENT, image-based
            # representation with no grid structure (see App.
            # table_generator's own refusal message for that tag) - so
            # this button is added here in code rather than requiring an
            # edit to the external CUPEPUB Zoning config file. The `not
            # any(...)` guard means a future Zoning config that DOES map
            # a real cup_name onto "table" is never shadowed/duplicated.
            tag_buttons.append({"label": "Table Draw", "tag": TAG_TABLE, "attrs": {"cup_name": "Table Draw"}})
            ordered_fnames.append(TAG_TABLE)

    tag_colors = _generate_tag_colors(ordered_fnames)

    # Merge Previous's "find previous compatible content" search (core/
    # zone_manager.py._find_previous_in_reading_order) - page_marker_tags
    # is always skipped over; footnote_flow_tags is skipped whenever it
    # doesn't match the searching zone's own flow membership. Reuses
    # EpubXmlGenerator's own _NOTE_TAGS keys as the canonical footnote-flow
    # vocabulary (the exact same "fn"/"en" tags its footnote/endnote
    # handling already recognizes) rather than a second, separately
    # maintained literal.
    from core.epub_xml_generator import EpubXmlGenerator
    page_marker_tags = [b["tag"] for b in tag_buttons if b["label"] == pagenum_cup_name] if pagenum_cup_name else []
    footnote_flow_tags = [t for t in EpubXmlGenerator._NOTE_TAGS if t in ordered_fnames]
    # Same "image/figure must not block a genuine cross-page text
    # continuation" skip-list as EPUB's own derivation (core/
    # profile_manager.py) - any CUP tag this same function already
    # flagged as is_image above (attrs["asset_kind"] set) via CUPLookup's
    # own property=="image"/ltype=="image" convention, never a second,
    # separately-maintained image-tag list and never a hardcoded CUP tag
    # name (which vary per client).
    non_flow_tags = [b["tag"] for b in tag_buttons if b["attrs"].get("asset_kind")]
    # Index Auto-Zone's level->tag mapping (auto_zoning/index_auto_zone.py) -
    # only includes a level whose fname is ACTUALLY present in THIS
    # profile's own live-loaded tag set (same derivation pattern as
    # footnote_flow_tags just above), so a future client CUPLookup.xml that
    # renames/omits a level is reflected automatically, never a second
    # hardcoded copy of EpubXmlGenerator's own default.
    index_hierarchy_tags = {level: tag for tag, level in EpubXmlGenerator._INDEX_HIERARCHY_LEVELS.items()
                             if tag in ordered_fnames}

    # Box_Start/Box_End are pure structural zoning markers (spec: "must
    # NOT appear as final XHTML elements") - core/hierarchy.py's existing
    # Boxed-Text Start/End stack mechanism (built for the XML profile's
    # own boxed-text-start/-end tags) is reused for them via this pair,
    # rather than a second grouping engine - see EpubXmlGenerator.generate
    # and its "boxed-text" node rendering.
    box_start_fname = next((b["tag"] for b in tag_buttons if b["label"] == BOX_START_CUP_NAME), None)
    box_end_fname = next((b["tag"] for b in tag_buttons if b["label"] == BOX_END_CUP_NAME), None)
    box_container_tags = [box_start_fname, box_end_fname] if box_start_fname and box_end_fname else None

    image_kinds = {}
    for btn in tag_buttons:
        asset_kind = btn["attrs"].get("asset_kind")
        if asset_kind and asset_kind not in image_kinds:
            override = _CLIENT_IMAGE_TOKEN_OVERRIDES.get(asset_kind)
            if override is not None:
                image_kinds[asset_kind] = dict(override)
                continue
            # No override for this asset_kind (a CUP zoning name the
            # client naming convention doesn't explicitly cover) - the
            # prior, unchanged fallback: lowercase the CUP name itself,
            # stripping a leading "img_" prefix. Never crashes, never
            # blocks generation for an unrecognized image type; it just
            # won't match the client's exact short-token convention.
            token = asset_kind
            if token.startswith("img_"):
                token = token[4:]
            image_kinds[asset_kind] = {"token": token, "ext": "png", "digits": 2}

    diagnostics = {
        "CUPEPUB_Profile.xml": profile_id_status,
        "CUPEPUB_Zoning.xml": zoning_status,
        "CUPEPUB_Master.xml": master_diag["master_status"],
        "CUPEPUB_AutoStyling.xml": master_diag["autostyling_status"],
        "CUPEPUB_Character.xml": character_status,
        "CUPEPUB_ZoneValidation.xml": validation_status,
        "CUPLookup.xml": lookup_status,
        "Mapping.xml": "Loaded" if os.path.isfile(MAPPING_XML) else "NOT FOUND",
        "Master rules effective source": master_diag["effective_source"],
        "Master rule count": master_diag["effective_rule_count"],
        "Mandatory zones": ", ".join(mandatory_zones) if mandatory_zones else "(none)",
        "Unmapped CUP tags (no CUPLookup entry - using literal lowercase name)":
            ", ".join(unmapped_tags) if unmapped_tags else "(none)",
        "CUPLookup duplicate keys": "; ".join(lookup_diag) if lookup_diag else "(none)",
        "Page-number marker tag": pagenum_cup_name or "(none - no 'PageNum' item in CUPEPUB_Zoning.xml)",
    }

    return {
        "name": PROFILE_NAME,
        "description": "Complete CUP EPUB workflow - tags/shortcuts/mapping all read live from profiles/CUPEPUB/*.xml.",
        "xml_enabled": False,
        "xhtml_enabled": True,
        "root_tag": "component",
        "mapping_xml_path": MAPPING_XML,
        "component_types": component_types,
        "default_component_type": "chapter" if "chapter" in component_types else (component_types[0] if component_types else "chapter"),
        "image_kinds": image_kinds,
        "tag_buttons": tag_buttons,
        "tag_colors": tag_colors,
        "tag_groups": tag_groups,
        "project_id": project_id,
        "cup_shortcuts": cup_shortcuts,
        "cup_master_rules": master_rules,
        "cup_character_map": merged_chars,
        "cup_character_map_per_font": per_font_chars,
        "cup_mandatory_zones": mandatory_zones,
        "cup_mandatory_meta": mandatory_meta,
        "cup_diagnostics": diagnostics,
        "cup_pagenum_name": pagenum_cup_name,
        # CUPEPUB has an explicit PageNum tag, so a pagebreak must ONLY come
        # from an actual PageNum zone (spec 98.10-98.15) - the EPUB profile
        # has no such tag and relies entirely on EpubXmlGenerator's existing
        # automatic per-PDF-page pagebreak (_maybe_page_break), which stays
        # on by default (profile.get("auto_pagebreak", True)) for it and
        # for any other profile that doesn't set this key at all.
        "auto_pagebreak": False,
        "page_marker_tags": page_marker_tags,
        "footnote_flow_tags": footnote_flow_tags,
        "non_flow_tags": non_flow_tags,
        "index_hierarchy_tags": index_hierarchy_tags,
        "box_container_tags": box_container_tags,
    }
