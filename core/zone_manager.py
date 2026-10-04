"""Zone data model + ZoneManager: CRUD, parent auto-detection via containment,
manual parent override, undo/redo, and tag-aware horizontal split."""
import copy
from dataclasses import dataclass, field
from typing import Optional

from core.constants import SPLIT_CHILD_TAG, NON_FLOW_TAGS
from core import text_extractor, debug_log
from core.auto_split_numbered_notes import detect as detect_numbered_notes


@dataclass
class Zone:
    zone_id: str
    page: int
    tag: str
    bbox: list  # [x0, y0, x1, y1] in PDF coordinate space
    text: str = ""
    parent_id: Optional[str] = None
    level: int = 0
    serial: Optional[int] = None  # Reading Order - a MANUALLY maintained, persisted, continuous global
    # 1..N sequence across the whole project (see ZoneManager.normalize_reading_order/set_reading_order).
    # NEVER recomputed from bbox/position/page/tag/hierarchy - hierarchy.finalize_order only touches
    # `level` now, never `serial`. May be a float momentarily mid-reorder (e.g. two split pieces get
    # parent.serial + 0.0001/0.0002 before the next normalize pass compacts everything back to clean
    # integers) but is always a clean int immediately after any zone-sequence-changing operation.
    column: Optional[int] = None
    children: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    created_order: int = 0          # zone-creation sequence, for display/debugging ONLY - never used as reading order
    split_group_id: Optional[str] = None  # shared by all zones created together by one Horizontal Split
    is_split: bool = False          # True if this zone was created by a Horizontal Split
    source_zone_id: Optional[str] = None  # the original zone a split zone came from

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "page": self.page,
            "tag": self.tag,
            "bbox": list(self.bbox),
            "text": self.text,
            "parent_id": self.parent_id,
            "level": self.level,
            "serial": self.serial,
            "column": self.column,
            "children": list(self.children),
            "attributes": dict(self.attributes),
            "created_order": self.created_order,
            "split_group_id": self.split_group_id,
            "is_split": self.is_split,
            "source_zone_id": self.source_zone_id,
        }

    # ---- Auto Zone / Auto Tag state (stored in `attributes`, so the
    # project file format and every existing reader stay unchanged) ----
    @property
    def locked(self) -> bool:
        """A locked zone survives Auto Zone, Auto Tag, OCR and re-analysis."""
        return bool(self.attributes.get("locked"))

    @property
    def manual_override(self) -> bool:
        """The user changed an automatically created zone (tag or geometry)."""
        return bool(self.attributes.get("manual_override"))

    @property
    def is_auto(self) -> bool:
        return self.attributes.get("source") in ("auto", "ocr", "auto_index", "auto_paragraph") \
            and not self.manual_override

    @property
    def protected(self) -> bool:
        """Never replaced/retagged by automatic processing: locked, manually
        overridden, or created by hand in the first place."""
        return self.locked or self.manual_override or not self.attributes.get("source")

    @property
    def confidence(self):
        return self.attributes.get("confidence")

    @property
    def needs_review(self) -> bool:
        return bool(self.attributes.get("needs_review"))

    @property
    def auto_role(self):
        return self.attributes.get("auto_role")

    @property
    def formatting_ranges(self) -> list:
        """Character-offset formatting ranges of this zone's text (plain-text
        offsets, one entry per separate range) - see core.formatting_ranges."""
        from core.formatting_ranges import parse_ranges
        return parse_ranges(self.text or "")[1]

    @staticmethod
    def from_dict(d: dict) -> "Zone":
        return Zone(
            zone_id=d["zone_id"], page=d["page"], tag=d["tag"], bbox=list(d["bbox"]),
            text=d.get("text", ""), parent_id=d.get("parent_id"), level=d.get("level", 0),
            serial=d.get("serial"), column=d.get("column"), children=list(d.get("children", [])),
            attributes=dict(d.get("attributes", {})),
            created_order=d.get("created_order", 0),
            split_group_id=d.get("split_group_id"),
            is_split=d.get("is_split", False),
            source_zone_id=d.get("source_zone_id"),
        )


# Attribute keys a user retag (set_tag with a fresh attribute dict) keeps.
_PRESERVED_ON_RETAG = ("locked", "auto_engine", "auto_role", "manual_override")
# Attribute keys an Auto Tag retag keeps (user-entered data never belongs
# to the engine).
_PRESERVED_ON_AUTOTAG = ("locked", "manual_text", "merged_with_previous", "style_overrides",
                         "hyphen_keep_boundaries", "manual_parent")


def contains(outer, inner, tol=2.0) -> bool:
    ox0, oy0, ox1, oy1 = outer
    ix0, iy0, ix1, iy1 = inner
    return (ox0 - tol) <= ix0 and (oy0 - tol) <= iy0 and (ox1 + tol) >= ix1 and (oy1 + tol) >= iy1


def area(bbox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


class ZoneManager:
    def __init__(self, pdf_document=None):
        self.pdf_document = pdf_document
        self.zones: dict[str, Zone] = {}
        self._next_serial_num = 1
        self._next_created_order = 1  # zone-creation sequence, NEVER used as reading order
        self._undo_stack = []
        self._redo_stack = []
        self._max_undo = 100
        self._batch_depth = 0  # see begin_batch/end_batch
        # Called as on_manual_retag(zone, old_tag, old_attrs) when the USER
        # retags an automatically created zone (Auto Tag learning).
        self.on_manual_retag = None

    def _next_created_order_value(self) -> int:
        n = self._next_created_order
        self._next_created_order += 1
        return n

    # ---------- persistence helpers ----------
    def set_pdf_document(self, pdf_document):
        self.pdf_document = pdf_document

    def load_counter(self, next_id: int):
        self._next_serial_num = next_id

    def next_zone_id(self) -> str:
        zid = f"z{self._next_serial_num:05d}"
        self._next_serial_num += 1
        return zid

    # ---------- undo/redo ----------
    @staticmethod
    def _detached_attributes(attrs: dict) -> dict:
        """Independent copy of a zone's attributes dict. Almost every value
        is a str/number/bool/None, so a plain dict() copy is already fully
        independent; only the rare nested list/dict value needs deepcopy.
        (A blanket copy.deepcopy of every zone on every edit was the main
        cause of the multi-second freeze after drawing/splitting a zone on
        a large project.)"""
        for v in attrs.values():
            if isinstance(v, (dict, list, set, tuple)):
                return copy.deepcopy(attrs)
        return dict(attrs)

    def _snapshot(self) -> dict:
        # to_dict() already copies bbox/children; attributes are copied by
        # _detached_attributes - together equivalent to the old deepcopy.
        snap = {}
        for zid, z in self.zones.items():
            d = z.to_dict()
            d["attributes"] = self._detached_attributes(z.attributes)
            snap[zid] = d
        return snap

    def _restore(self, snapshot: dict):
        zones = {}
        for zid, d in snapshot.items():
            zone = Zone.from_dict(d)
            # never share nested attribute values with the stored snapshot
            # (it can be restored again by a later redo/undo)
            zone.attributes = self._detached_attributes(d.get("attributes", {}))
            zones[zid] = zone
        self.zones = zones

    def _push_undo(self):
        if self._batch_depth > 0:
            # Inside begin_batch()/end_batch() - that call already pushed the
            # ONE snapshot this whole batch undoes to; every individual
            # add_zone/etc. inside it must NOT also push its own (spec:
            # "the entire Auto Zone operation must be ONE undoable
            # operation... Undo must remove those generated zones together").
            return
        self._undo_stack.append(self._snapshot())
        if len(self._undo_stack) > self._max_undo:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def begin_batch(self):
        """Collapses every add_zone/set_tag/etc. call made until the matching
        end_batch() into ONE undo step, by pushing exactly one snapshot now
        and suppressing every nested _push_undo() in between. Nestable (a
        batch started while already inside one just increments a depth
        counter - only the outermost begin_batch actually pushes). Existing
        callers that never call this (manual zoning, Auto Zone, Auto
        Analyse, Auto Zone Index) are completely unaffected - _batch_depth
        stays 0 for them, so _push_undo behaves exactly as before this
        existed."""
        if self._batch_depth == 0:
            self._push_undo()
        self._batch_depth += 1

    def end_batch(self):
        self._batch_depth = max(0, self._batch_depth - 1)

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(self._snapshot())
        snap = self._undo_stack.pop()
        self._restore(snap)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(self._snapshot())
        snap = self._redo_stack.pop()
        self._restore(snap)
        return True

    # ---------- text preview ----------
    def _refresh_text(self, zone: Zone):
        if self.pdf_document is None:
            return
        if zone.tag in NON_FLOW_TAGS:
            return
        # A manually-entered value (see set_zone_text - CUPEPUB PageNum's
        # manual entry, for when OCR/extraction can't reliably read a
        # printed Roman-numeral/Arabic page label) must survive a later
        # resize/move/retag, never be silently overwritten by re-running
        # extraction against the (possibly now slightly different) bbox.
        if zone.attributes.get("manual_text"):
            return
        # Same protection for an OCR-produced zone's text (RULE 18: never
        # silently correct/overwrite OCR output). Its bbox almost always
        # sits on a SCANNED page with no digital text layer at all, so
        # re-running text_extractor here after a resize/move would find
        # nothing and silently replace real OCR-recognized text with an
        # empty string - exactly the kind of silent loss the OCR spec
        # forbids. Once the user explicitly edits it via set_zone_text,
        # manual_text above takes over as the (stronger) guard instead.
        if zone.attributes.get("source") == "ocr":
            return
        try:
            page = self.pdf_document.get_page(zone.page)
            debug_log.log("ZONE_EXTRACT", f"zone={zone.zone_id} page={zone.page} tag={zone.tag!r} bbox={zone.bbox} BEFORE extract_plain_text")
            # extract_formatted_text() already performs the complete extraction
            # and formatting/style-analysis path. Calling extract_plain_text()
            # first runs that same expensive path again and its result is
            # immediately discarded. Keep one authoritative formatted pass.
            zone.text = text_extractor.extract_formatted_text(page, zone.bbox)
            debug_log.log("ZONE_EXTRACT", f"zone={zone.zone_id} AFTER extract_formatted_text => {zone.text!r}")
        except Exception:
            pass

    def refresh_text(self, zone_id: str):
        zone = self.zones.get(zone_id)
        if zone:
            self._refresh_text(zone)

    def set_zone_text(self, zone_id: str, text: str) -> bool:
        """Manually overrides a zone's text - required for CUPEPUB's
        PageNum manual entry (OCR/PDF text extraction cannot always
        correctly read Roman numerals or a printed page label; the user
        must be able to type the correct value directly). Trims
        whitespace, marks attributes["manual_text"]=True so a later
        resize/move/retag (_refresh_text) never silently overwrites it
        again, and is undoable/redoable like any other zone mutation."""
        zone = self.zones.get(zone_id)
        if not zone:
            return False
        self._push_undo()
        zone.text = text.strip()
        zone.attributes["manual_text"] = True
        return True

    # ---------- level computation ----------
    def _compute_level(self, tag: str, parent_id: Optional[str]) -> int:
        """Interim estimate only (containment-chain depth), shown until the next
        full refresh recomputes the real value via reading_order/hierarchy.
        finalize_order - which is the authoritative source, since true
        hierarchy level also accounts for heading-based sectioning that has no
        geometric parent_id. Deliberately distinct from heading disp-level
        (h1..h6), which is fixed by the tag itself - see hierarchy.finalize_order."""
        depth = 0
        pid = parent_id
        while pid and pid in self.zones:
            depth += 1
            pid = self.zones[pid].parent_id
        return depth

    # ---------- parent detection ----------
    def find_smallest_containing_zone(self, page: int, bbox, exclude_id=None):
        best_id, best_area = None, None
        for zid, z in self.zones.items():
            if zid == exclude_id or z.page != page:
                continue
            if contains(z.bbox, bbox):
                a = area(z.bbox)
                if best_area is None or a < best_area:
                    best_area, best_id = a, zid
        return best_id

    def _reparent_no_undo(self, zone_id: str, new_parent_id: Optional[str]):
        zone = self.zones[zone_id]
        old_parent_id = zone.parent_id
        if old_parent_id and old_parent_id in self.zones:
            if zone_id in self.zones[old_parent_id].children:
                self.zones[old_parent_id].children.remove(zone_id)
        zone.parent_id = new_parent_id
        if new_parent_id and new_parent_id in self.zones:
            if zone_id not in self.zones[new_parent_id].children:
                self.zones[new_parent_id].children.append(zone_id)
            zone.column = self.zones[new_parent_id].column
        self._recompute_subtree_levels(zone_id)

    def reparent_page(self, page: int):
        """Re-evaluates parent_id for every non-manually-parented zone on
        `page` as the smallest zone that geometrically contains it (skipping
        any candidate that would create a cycle).

        Containment detection at zone-creation time only looks for an
        EXISTING container holding the new zone. That is one-directional: if
        the user draws a Title Group / Figure / Boxed Text / List box AROUND
        zones that already exist (a very common workflow - zone the content
        first, then draw the grouping box around it), those pre-existing
        zones never got a chance to notice the new, smaller/better container.
        Re-running full containment on every zone after each addition makes
        parent assignment order-independent, fixing containers that were
        generated empty despite having obvious children on the page.
        """
        zones_on_page = [z for z in self.zones.values() if z.page == page]
        for z in zones_on_page:
            if z.attributes.get("manual_parent"):
                continue  # explicit user override (Set Parent / Remove Parent) - never auto-revert it
            best_id, best_area = None, None
            for other in zones_on_page:
                if other.zone_id == z.zone_id:
                    continue
                if self._is_descendant(z.zone_id, other.zone_id):
                    continue  # would create a cycle
                if contains(other.bbox, z.bbox):
                    a = area(other.bbox)
                    if best_area is None or a < best_area:
                        best_area, best_id = a, other.zone_id
            if best_id != z.parent_id:
                debug_log.log("PARENT", f"{z.zone_id} -> {best_id}")
                self._reparent_no_undo(z.zone_id, best_id)

    # ---------- CRUD ----------
    def add_zone(self, page: int, tag: str, bbox, attributes=None, auto_parent=True, parent_id=None) -> Zone:
        self._push_undo()
        zid = self.next_zone_id()
        bbox = [float(v) for v in bbox]
        if auto_parent and parent_id is None:
            parent_id = self.find_smallest_containing_zone(page, bbox)
        column = None
        if parent_id and parent_id in self.zones:
            column = self.zones[parent_id].column
        debug_log.log("ZONE_ADD", f"ADDING page={page} tag={tag!r} bbox={bbox} source={attributes.get('source') if attributes else None!r}")
        zone = Zone(zone_id=zid, page=page, tag=tag, bbox=bbox, parent_id=parent_id,
                    attributes=dict(attributes or {}), column=column,
                    created_order=self._next_created_order_value())
        zone.level = self._compute_level(tag, parent_id)
        zone.serial = 10 ** 9  # placeholder guaranteeing "last" until normalize_reading_order compacts it
        self.zones[zid] = zone
        if parent_id and parent_id in self.zones:
            self.zones[parent_id].children.append(zid)
        self._refresh_text(zone)
        if auto_parent:
            # A full reparent scan is only needed when the newly-created zone
            # can be a container around zones that already existed on the page.
            # For normal content zones, the single containment lookup above is
            # sufficient and avoids an O(n^2) scan after every zone creation.
            new_zone_contains_existing = any(
                other.zone_id != zone.zone_id
                and other.page == page
                and contains(zone.bbox, other.bbox)
                for other in self.zones.values()
            )
            if new_zone_contains_existing:
                self.reparent_page(page)
        self.normalize_reading_order(page)  # new zone lands at the END of ITS PAGE's Reading Order sequence
        debug_log.log("ZONE", f"created {zid} tag={tag} page={page} bbox={[round(v, 1) for v in bbox]} parent={zone.parent_id}")
        return zone

    def get_zone(self, zone_id: str) -> Optional[Zone]:
        return self.zones.get(zone_id)

    def zones_on_page(self, page: int):
        return [z for z in self.zones.values() if z.page == page]

    def update_bbox(self, zone_id: str, bbox):
        """Resize/move. Never touches tag/parent/level/column/serial."""
        self._push_undo()
        zone = self.zones.get(zone_id)
        if not zone:
            return
        zone.bbox = [float(v) for v in bbox]
        if zone.attributes.get("auto_engine"):
            zone.attributes["manual_override"] = True
        self._refresh_text(zone)
        debug_log.log("RESIZE", f"{zone_id} bbox -> {[round(v, 1) for v in zone.bbox]}")

    def set_tag(self, zone_id: str, tag: str, attributes=None, page_marker_tags=None):
        """page_marker_tags (default {"pagenumber"}, matching every other
        profile-aware caller in this codebase - see core.paragraph_merge's
        own identical default) - when the NEW tag is the active profile's
        own page-marker tag, this zone's Reading Order is immediately
        forced to 1 within its own page (spec: "PageNum MUST automatically
        become Reading Order = 1... The user must NOT manually set its
        reading order"). Reuses the existing set_reading_order primitive
        unchanged - it already shifts every other zone on the page by
        exactly one while preserving their relative order, and already
        persists through the existing zone-attribute save/reopen path.
        Wrapped in begin_batch()/end_batch() so the tag change and the
        resulting reorder are ONE undo step, not two - a user pressing
        Undo once after tagging a zone PageNum should get back the exact
        pre-tag state, not a zone still tagged PageNum sitting at its old
        Reading Order."""
        self.begin_batch()
        try:
            self._push_undo()
            zone = self.zones.get(zone_id)
            if not zone:
                return
            old_tag, old_attrs = zone.tag, dict(zone.attributes)
            zone.tag = tag
            if attributes is not None:
                zone.attributes = dict(attributes)
                # Lock state and Auto Tag provenance survive a retag.
                for key in _PRESERVED_ON_RETAG:
                    if key in old_attrs and key not in zone.attributes:
                        zone.attributes[key] = old_attrs[key]
            if old_attrs.get("auto_engine") and (old_tag != tag or (attributes is not None and
                                                  attributes.get("cup_name") != old_attrs.get("cup_name"))):
                # USER MANUAL OVERRIDE > automatic detection: never retagged
                # by Auto Zone / Auto Tag again unless re-analysis is forced.
                zone.attributes["manual_override"] = True
                zone.attributes.pop("needs_review", None)
                zone.attributes.pop("review_reasons", None)
                if callable(self.on_manual_retag):
                    try:
                        self.on_manual_retag(zone, old_tag, old_attrs)
                    except Exception:
                        pass
            zone.level = self._compute_level(tag, zone.parent_id)
            self._refresh_text(zone)
            marker_tags = page_marker_tags if page_marker_tags is not None else {"pagenumber"}
            if tag in marker_tags:
                self.set_reading_order(zone_id, 1)
        finally:
            self.end_batch()

    # ---------- lock / automatic tagging ----------
    def set_locked(self, zone_id: str, locked: bool) -> bool:
        zone = self.zones.get(zone_id)
        if not zone:
            return False
        self._push_undo()
        if locked:
            zone.attributes["locked"] = True
        else:
            zone.attributes.pop("locked", None)
        return True

    def apply_auto_tag(self, zone_id: str, tag: str, attributes: dict, force: bool = False) -> bool:
        """Retag by the Auto Tag engine. Refuses (returns False) for a
        protected zone - locked, manually overridden, or hand-made - unless
        `force` (explicit user-requested re-analysis; a LOCKED zone is never
        touched even then). Not a user action: never sets manual_override."""
        zone = self.zones.get(zone_id)
        if not zone or zone.locked:
            return False
        if zone.protected and not force:
            return False
        self._push_undo()
        keep = {k: zone.attributes[k] for k in _PRESERVED_ON_AUTOTAG if k in zone.attributes}
        zone.tag = tag
        zone.attributes = dict(attributes)
        zone.attributes.update(keep)
        if force:
            zone.attributes.pop("manual_override", None)
        zone.level = self._compute_level(tag, zone.parent_id)
        return True

    def _is_descendant(self, ancestor_id: str, candidate_id: str) -> bool:
        """True if candidate_id is somewhere in ancestor_id's subtree."""
        stack = list(self.zones[ancestor_id].children) if ancestor_id in self.zones else []
        seen = set()
        while stack:
            cid = stack.pop()
            if cid == candidate_id:
                return True
            if cid in seen or cid not in self.zones:
                continue
            seen.add(cid)
            stack.extend(self.zones[cid].children)
        return False

    def set_parent(self, zone_id: str, new_parent_id: Optional[str]) -> bool:
        """Explicit user override (GUI "Set Parent" / "Remove Parent"). Marks
        the zone so the automatic containment-based reparent_page() pass
        never silently reverts this choice later. Returns False (no-op) if
        new_parent_id would make zone its own ancestor/descendant (a cycle)."""
        zone = self.zones.get(zone_id)
        if not zone:
            return False
        if new_parent_id == zone_id:
            return False
        if new_parent_id and new_parent_id in self.zones and self._is_descendant(zone_id, new_parent_id):
            return False
        self._push_undo()
        zone.attributes["manual_parent"] = True
        old_parent_id = zone.parent_id
        if old_parent_id and old_parent_id in self.zones:
            if zone_id in self.zones[old_parent_id].children:
                self.zones[old_parent_id].children.remove(zone_id)
        zone.parent_id = new_parent_id
        if new_parent_id and new_parent_id in self.zones:
            if zone_id not in self.zones[new_parent_id].children:
                self.zones[new_parent_id].children.append(zone_id)
            zone.column = self.zones[new_parent_id].column
        self._recompute_subtree_levels(zone_id)
        return True

    def _recompute_subtree_levels(self, zone_id: str):
        zone = self.zones.get(zone_id)
        if not zone:
            return
        zone.level = self._compute_level(zone.tag, zone.parent_id)
        for cid in list(zone.children):
            self._recompute_subtree_levels(cid)

    # ---------- Reading Order (manual, PER-PAGE, persisted - see Zone.serial) ----------
    # EPUB profile's Index Primary/Secondary/Territory hierarchy (see
    # core/epub_xml_generator.py's _build_index_hierarchy) is a THIRD split
    # scenario neither "same tag throughout" nor "different tag, subsumed
    # into the parent" anticipated: one zone drawn over a multi-line index
    # block, Horizontal Split into one piece per line, then EACH piece
    # individually retagged to whichever level that specific line actually
    # is - by design, deliberately not preserving the parent's original tag
    # on every piece the way a content/leaf split does. Without this
    # exception a retagged piece looks identical to a genuine STRUCTURAL
    # split (Figure -> Label/Caption) and gets serial=None, clustering ALL
    # such pieces at the very end of the page (sorted only by creation
    # time, never true position) instead of each holding its own correct
    # Reading Order slot - confirmed as the exact cause of a real "all
    # Index Primary entries first, every Secondary/Territory dumped at the
    # end" bug report. Scoped to these three EPUB-only tag names
    # specifically: zero effect on the XML profile's own Figure/List/
    # Boxed-Text/Title-Group structural splits, which never use these tags.
    _INDEX_HIERARCHY_TAGS = {"indexprimary", "indexsecondary", "indexterritory"}

    def _is_same_tag_split_piece(self, zone: Zone) -> bool:
        """True if `zone` is one of a Horizontal/Vertical-Split parent's OWN
        pieces, AND that split was a content/leaf split (the parent's tag is
        not a registered structural-container tag - see SPLIT_CHILD_TAG) -
        meaning core/xml_generator.py's _gen_zone_multi flattens the pieces
        out to occupy the parent's former top-level slot, so each piece
        needs its own Reading Order position there, WHATEVER tag any
        individual piece has since been retagged to (spec: "FIX SPLIT-ZONE
        TAG INHERITANCE" - a user may retag any split piece independently
        without that piece silently losing its own Reading Order slot).
        False for a genuine STRUCTURAL split (the parent's tag IS a
        SPLIT_CHILD_TAG key - e.g. a List split into list-item pieces, a
        Figure split into label/caption pieces): those pieces stay nested
        inside their parent's own generated element (<list>, <fig>, ...)
        and never occupy a top-level slot themselves.

        A previous version of this check required the piece's OWN tag to
        still equal the parent's tag (with a narrow carve-out,
        _INDEX_HIERARCHY_TAGS, for the one already-known case where that
        assumption broke: an Index Primary/Secondary/Territory line-split
        block, individually retagged per piece). Keying off the PARENT's
        own tag against SPLIT_CHILD_TAG instead - the same map
        ZoneManager.split_zone itself already uses to decide a structural
        child's default tag - generalizes correctly to ANY content/leaf
        split with independently retagged pieces (confirmed: a plain
        Paragraph split into Paragraph/Heading/Paragraph pieces suffered
        the exact same "loses its own Reading Order slot" defect this
        historical carve-out already fixed for one specific tag set), and
        still covers the index-hierarchy case exactly as before -
        indexprimary/indexsecondary/indexterritory are not SPLIT_CHILD_TAG
        keys either, so _INDEX_HIERARCHY_TAGS is kept only as documentation
        of that already-solved case, not as a separate code path."""
        if not (zone.is_split and zone.source_zone_id):
            return False
        parent = self.zones.get(zone.source_zone_id)
        if parent is None:
            return False
        return parent.tag not in SPLIT_CHILD_TAG

    def _is_same_tag_split_source(self, zone: Zone) -> bool:
        """True if zone has been entirely superseded, for Reading-Order/XML
        purposes, by its own same-tag split pieces (see
        _is_same_tag_split_piece) - i.e. ALL of its children are such
        pieces. False if it has no children, or its children are a
        structural split (different tag) or plain geometric containment
        (e.g. a List's manually-drawn List Item children, or a Figure's
        Label/Caption/Graphic) - those stay nested, the parent keeps its own
        Reading Order position."""
        children = [self.zones[cid] for cid in zone.children if cid in self.zones]
        return bool(children) and all(self._is_same_tag_split_piece(c) for c in children)

    def _counts_in_reading_order(self, zone: Zone) -> bool:
        """True if this zone occupies its OWN position in its page's
        continuous 1..N Reading Order sequence.

        False for a zone explicitly Merge-Previous-ed into an earlier zone
        (attributes["merged_with_previous"]) - it is folded into the
        earlier zone's single combined output ("Merged(B+C)" in the spec's
        terms; this app never creates a separate merged-parent object, the
        earlier/target zone simply IS that combined item), so the later
        zone doesn't get a separate slot either - regardless of whether it
        is itself top-level or nested (Merge Previous can pair either
        siblings-under-a-parent or top-level zones).

        Otherwise: a TOP-LEVEL zone (parent_id is None) counts UNLESS it has
        been entirely superseded by its own same-tag split pieces (see
        _is_same_tag_split_source) - e.g. a split Paragraph. A zone WITH a
        parent counts ONLY if it is itself one of those same-tag split
        pieces (see _is_same_tag_split_piece) - i.e. it has flattened out to
        occupy its parent's former top-level slot. Every OTHER kind of
        child (a List's List Items, a Figure's Label/Caption/Graphic,
        anything reached via ordinary geometric containment) never counts,
        whether or not it came from a split - it stays nested inside its
        parent's own generated element and must never consume a top-level
        Reading Order number (spec: "List Items ... MUST NOT consume
        additional top-level reading-order numbers").

        A non-counting zone gets serial=None (see _resync_noncounting_serials)
        - never a fractional placeholder. Fractional values (e.g. 14.5, 16.5)
        must NEVER appear in reading_order."""
        if zone.attributes.get("merged_with_previous"):
            return False
        if zone.parent_id is None:
            return not self._is_same_tag_split_source(zone)
        return self._is_same_tag_split_piece(zone)

    # ---------- split/merge relationship resolution ----------
    # The attribute keys that together describe ONE explicit Merge Previous
    # relationship, always stored on the LATER (continuing) zone. Moved as
    # a unit by normalize_merge_relationships() - never split up.
    _MERGE_ATTR_KEYS = ("merged_with_previous", "merge_target", "merge_join", "hyphen_keep_chain_boundary")

    def content_split_leaves(self, zone_id: str) -> list:
        """The zones that actually carry `zone_id`'s CONTENT, in reading
        order: the zone itself when it is not a content/leaf split parent,
        otherwise its split pieces - recursively, so a split-of-a-split
        resolves to its own leaf pieces (same "content split" rule as
        _is_same_tag_split_source / xml_generator._gen_zone_multi /
        epub_xml_generator._resolve_zone_to_elements). Read-only."""
        from core import reading_order
        zone = self.zones.get(zone_id)
        if zone is None:
            return []
        if not self._is_same_tag_split_source(zone):
            return [zone]
        leaves = []
        for cid in reading_order.sort_children_ids(self, zone_id):
            leaves.extend(self.content_split_leaves(cid))
        return leaves

    def repair_orphan_merges(self, replaced: Optional[dict] = None) -> int:
        """Fixes Merge Previous links whose target zone no longer exists
        (the "continues zone zXXXX, which no longer exists (orphaned
        continuation fragment)" error that blocked Generate XHTML after
        the target zone was deleted).

        `replaced` maps a removed zone id -> the zone that took its place
        in the text flow (the deleted zone's OWN merge target when it was
        itself a continuation, or the surviving zone of an Index Merge).
        A continuation is re-pointed along that chain (A <- B <- C, delete
        B => C continues A). When nothing survives to continue, the zone
        simply becomes a normal zone again and gets its Reading Order slot
        back at its physical position on its page. Idempotent; returns the
        number of zones fixed."""
        replaced = replaced or {}
        fixed = 0
        pages = set()
        for zone in list(self.zones.values()):
            if not zone.attributes.get("merged_with_previous"):
                continue
            original = zone.attributes.get("merge_target")
            target_id = original
            seen = set()
            while target_id and target_id not in self.zones and target_id in replaced and target_id not in seen:
                seen.add(target_id)
                target_id = replaced[target_id]
            if target_id and target_id in self.zones and target_id != zone.zone_id:
                if target_id != original:
                    zone.attributes["merge_target"] = target_id
                    fixed += 1
                continue
            for key in self._MERGE_ATTR_KEYS:
                zone.attributes.pop(key, None)
            from core import reading_order
            zone.serial = reading_order._physical_slot(zone, self)
            pages.add(zone.page)
            fixed += 1
        for page in pages:
            self.normalize_reading_order(page)
        if fixed:
            debug_log.log("MERGE", f"repair_orphan_merges: {fixed} continuation(s) whose target was removed fixed")
        return fixed

    def _removal_replacements(self, removed_ids) -> dict:
        """For zones about to be removed: removed id -> its own merge target
        (so a continuation of a removed continuation can follow the chain)."""
        out = {}
        for zid in removed_ids:
            z = self.zones.get(zid)
            if z is not None and z.attributes.get("merged_with_previous") and z.attributes.get("merge_target"):
                out[zid] = z.attributes["merge_target"]
        return out

    def _subtree_ids(self, zone_id: str) -> list:
        out, stack = [], [zone_id]
        while stack:
            zid = stack.pop()
            z = self.zones.get(zid)
            if z is None or zid in out:
                continue
            out.append(zid)
            stack.extend(z.children)
        return out

    def normalize_merge_relationships(self) -> int:
        """Keeps Merge Previous metadata attached to zones that still
        CARRY content (spec: "The split operation must not destroy merge
        metadata" / "remove obsolete merge relationships"). A content/leaf
        split parent never emits its own text any more - its pieces do -
        so a merge relationship that still names the superseded parent is
        re-pointed at the piece that now holds that end of the text:

          - the split parent was itself Merge-Previous'd onto an earlier
            zone (Merge -> Split): the relationship moves to its FIRST leaf
            piece (the piece that actually continues the earlier zone), and
            is removed from the parent;
          - some later zone was Merge-Previous'd onto the split parent: its
            merge_target is re-pointed at the parent's LAST leaf piece (the
            piece the later zone actually continues).

        Structural splits (a List/Figure/... parent - see SPLIT_CHILD_TAG)
        keep their own element and are left untouched. Idempotent; returns
        the number of relationships changed. Called by split_zone() and by
        project loading (core/project_manager.py), so projects saved by an
        older version (merge flags still on a split parent) are migrated
        transparently - the saved JSON shape is unchanged."""
        total = 0
        for _ in range(10):  # a relationship moved in one pass may name another split parent - settle it
            changed = self._normalize_merge_relationships_pass()
            total += changed
            if not changed:
                break
        if total:
            debug_log.log("MERGE", f"normalize_merge_relationships: {total} relationship(s) re-pointed "
                                   "from split parent(s) to their content pieces")
        total += self.repair_orphan_merges()
        return total

    def _normalize_merge_relationships_pass(self) -> int:
        changed = 0
        for zone in list(self.zones.values()):
            if not self._is_same_tag_split_source(zone):
                continue
            leaves = self.content_split_leaves(zone.zone_id)
            if not leaves:
                continue
            if zone.attributes.get("merged_with_previous"):
                first = leaves[0]
                moved = {k: zone.attributes.pop(k) for k in self._MERGE_ATTR_KEYS if k in zone.attributes}
                if not first.attributes.get("merged_with_previous") and moved.get("merge_target") not in (
                        None, first.zone_id):
                    first.attributes.update(moved)
                changed += 1
            last_id = leaves[-1].zone_id
            for other in self.zones.values():
                if other.attributes.get("merge_target") == zone.zone_id and other.zone_id != last_id:
                    other.attributes["merge_target"] = last_id
                    changed += 1
        return changed

    def get_logical_top_level_zones(self, page: int):
        """Returns page `page`'s logical Reading-Order zones (see
        _counts_in_reading_order) in their CURRENT order - preserving
        whatever serial/created_order they already have, never recalculated
        from bbox/x/y. This is the read side of the single source of truth;
        renumber_reading_order() is the write side."""
        return sorted(
            (z for z in self.zones.values() if z.page == page and self._counts_in_reading_order(z)),
            key=lambda z: (z.serial if z.serial is not None else 10 ** 9, z.created_order),
        )

    def _resync_noncounting_serials(self, page: int):
        """Every NON-counting zone on `page` (see _counts_in_reading_order)
        gets serial=None - it holds NO top-level Reading Order position,
        period (spec: "Children have NO top-level reading_order"). This is
        the only place that clears it, so a currently-merged or
        currently-split-away zone always displays as "-" (PDF label, Zone
        Hierarchy, status bar) rather than a confusing fractional number."""
        for zone in self.zones.values():
            if zone.page == page and not self._counts_in_reading_order(zone):
                zone.serial = None

    def renumber_reading_order(self, page: int):
        """THE single central function that renumbers Reading Order for one
        page. Reading Order is PER-PAGE (page 1 restarts at 1, page 2
        restarts at 1, etc - never a running count across pages). Equivalent
        to: for index, zone in enumerate(get_logical_top_level_zones(page),
        start=1): zone.serial = index. A non-counting zone (Horizontal Split
        parent, Merge-Previous-consumed zone) never receives one of the 1..N
        integers - see _counts_in_reading_order/_resync_noncounting_serials.
        EVERY operation that changes a page's zone sequence (create, delete,
        split, merge, unmerge, explicit reorder, project load) calls this
        for the affected page - it is the ONLY place that assigns Reading
        Order integers anywhere in the project, so there is exactly one
        algorithm, never duplicated elsewhere. Never computed from
        bbox/position/tag/hierarchy - only from whatever order the zones
        already have (so calling this on already-consistent data is a
        no-op renumbering: relative order is always preserved)."""
        counter = 0
        for zone in self.get_logical_top_level_zones(page):
            counter += 1
            zone.serial = counter
        self._resync_noncounting_serials(page)
        debug_log.log("READING_ORDER", f"page {page} normalized: {counter} zone(s)")

    def normalize_reading_order(self, page: Optional[int] = None):
        """Bulk entry point used by project load (where several pages may
        need migration at once) and by callers that don't have one specific
        page in mind: renumbers ONE page if given, or every page that has
        zones if not. Always delegates to renumber_reading_order() - never a
        second/parallel numbering algorithm."""
        pages = {page} if page is not None else {z.page for z in self.zones.values()}
        for p in pages:
            self.renumber_reading_order(p)

    def set_reading_order(self, zone_id: str, new_position) -> bool:
        """Moves zone_id to `new_position` (1-based) within its OWN PAGE's
        Reading Order sequence - Reading Order is per-page, so this never
        moves a zone across pages, only within the page it's already on.
        NOT a bare `zone.serial = new_position` assignment: the zone is
        removed from its current spot and reinserted at the clamped target
        position, and every zone between the two positions shifts by
        exactly one. Integer positions only, clamped to [1, N] for that
        page. Returns False (no-op) for an unknown zone, a non-counting
        zone (a split parent or a merge-consumed zone can't be
        independently repositioned - move its associated piece/target
        instead), or a non-integer position."""
        zone = self.zones.get(zone_id)
        if not zone or not self._counts_in_reading_order(zone):
            return False
        try:
            new_position = int(new_position)
        except (TypeError, ValueError):
            return False
        page_zones = self.get_logical_top_level_zones(zone.page)
        if zone not in page_zones:
            return False
        self._push_undo()
        page_zones.remove(zone)
        n = len(page_zones) + 1
        pos = max(1, min(new_position, n))
        page_zones.insert(pos - 1, zone)
        for i, z in enumerate(page_zones, start=1):
            z.serial = i
        self._resync_noncounting_serials(zone.page)
        # Marks this zone as manually pinned (spec: "EPUBForge - Paragraph
        # Continuation + Split/Merge + Reading Order Fix" - "USER MANUALLY
        # CHANGES READING ORDER -> manual order becomes authoritative...
        # Do NOT immediately overwrite a manual reading-order change with
        # an automatic geometry recalculation"). core.reading_order.
        # recompute_page_order reads this flag and preserves a pinned
        # zone's RELATIVE position among its page's other zones on every
        # later automatic recompute (create/split/merge/delete/move/resize
        # elsewhere on the page), instead of re-deriving its position from
        # geometry every time - the same "sticky until the user changes it
        # again" convention set_zone_text's own manual_text flag already
        # uses, applied here to Reading Order. move_up/move_down both
        # delegate to this same function, so Move Up/Move Down and the Set
        # Reading Order dialog are covered identically, with no separate
        # flag-setting logic duplicated at either call site.
        zone.attributes["manual_ro"] = True
        debug_log.log("READING_ORDER", f"{zone_id} -> page {zone.page} position {pos} (manually pinned)")
        return True

    def move_up(self, zone_id: str) -> bool:
        """Shifts this zone one position earlier within its page's Reading Order."""
        zone = self.zones.get(zone_id)
        if not zone or not self._counts_in_reading_order(zone) or not zone.serial or zone.serial <= 1:
            return False
        return self.set_reading_order(zone_id, zone.serial - 1)

    def move_down(self, zone_id: str) -> bool:
        """Shifts this zone one position later within its page's Reading Order."""
        zone = self.zones.get(zone_id)
        if not zone or not self._counts_in_reading_order(zone):
            return False
        n = sum(1 for z in self.zones.values() if z.page == zone.page and self._counts_in_reading_order(z))
        if not zone.serial or zone.serial >= n:
            return False
        return self.set_reading_order(zone_id, zone.serial + 1)

    # ---------- manual merge ----------
    def _find_previous_in_reading_order(self, zone_id: str, page_marker_tags=None,
                                          footnote_flow_tags=None, non_flow_tags=None) -> Optional[str]:
        """"Previous zone" means the previous COMPATIBLE CONTENT zone in
        the same logical reading flow - NOT simply the immediately
        preceding zone. page_marker_tags (e.g. CUPEPUB's "pagenum", the
        XML profile's "pagenumber") are ALWAYS skipped over, never treated
        as a blocker or a match. footnote_flow_tags (e.g. "fn"/"en") are
        skipped whenever they don't match the SELECTED zone's own flow
        membership - a main-flow zone (p, Para, ...) searching backward
        skips over a footnote sitting between it and the real previous
        paragraph; a footnote zone searching backward (its own Merge
        Previous, rare but not disallowed) is likewise kept within the
        footnote flow, skipping main-flow content instead. Both default to
        the tag literals hardcoded here before this generalization existed
        ({"pagenumber"} / empty) so a caller that passes neither (nothing
        in the codebase besides gui/main_window.py, which passes the
        active profile's own page_marker_tags/footnote_flow_tags) sees
        ZERO behavior change.

        Uses the CURRENT PERSISTED Reading Order (zone.serial) - never
        CURRENT PERSISTED Reading Order (zone.serial) - never recomputed
        here, never derived from creation order/zone id/hierarchy/screen
        position.

        A zone that COUNTS in Reading Order (see _counts_in_reading_order)
        is looked up STRICTLY ON ITS OWN PAGE via
        get_logical_top_level_zones(zone.page), which already returns
        exactly that page's counting zones in their current persisted
        order - so this NEVER crosses a page boundary (a zone one page
        over is never even a candidate, regardless of its own Reading
        Order number - each page's numbering restarts at 1, so "one less"
        can easily belong to a completely different page), and NEVER lands
        on an inactive Horizontal-Split parent or an already-merge-
        consumed zone (both excluded by the same _counts_in_reading_order
        rule everything else in this class relies on). A same-tag Split
        PIECE (parent_id set, but DOES count - see
        _is_same_tag_split_piece) is included exactly like any other
        counting zone on that page - scoping split pieces down to just
        their own split-siblings (an earlier implementation) was the root
        cause of Merge Previous wrongly reporting "No previous zone in
        reading order to merge with" for the first piece of a split, even
        when some earlier, unrelated zone clearly precedes it on the page.

        If zone_id is the FIRST counting zone on its own page (nothing
        before it there, once page-number zones are skipped), this now
        falls back to the LAST counting zone on the nearest EARLIER page
        that has any - e.g. a Table/List/Paragraph that visually
        continues from page 1 onto page 2, where the second half is
        naturally the very first zoned thing on page 2. This is the only
        way a cross-page candidate is ever returned: found by walking
        backward through actual pages that have content, never by an
        arbitrary document-wide scan - so this never surfaces some
        unrelated, merely-earlier-in-the-project zone, only "the last
        thing before this page break." merge_with_previous (below) still
        decides whether that specific cross-page pairing is actually
        allowed (matching tags only - see its own docstring); this method
        only ever finds the CANDIDATE.

        A zone that does NOT count (an ordinary geometric child - a List
        Item, a Figure's Label/Caption/Graphic, or anything nested via
        plain containment) keeps the ORIGINAL, unchanged behavior: previous
        SIBLING under the same parent, since such a zone never holds a
        page-level Reading Order slot to look up in the first place - a
        parent zone is always on one single page, so its children's own
        Merge Previous never needs to cross a page boundary. A non-
        counting zone with no parent_id (the SELECTED zone itself being an
        inactive split parent or an already-merged zone - a rare edge
        case, not the normal "select a zone and merge it" flow) keeps the
        original document-wide fallback (reading_order.flatten_document_order),
        unchanged.

        Page Number zones are skipped over, never counted as a blocker,
        matching the automatic merge engine's treatment of them.

        non_flow_tags (spec: "EPUBForge - Global Merge, Continuation,
        Reading Order and Exact Text Preservation Engine" - "An image,
        advertisement, figure, or other object between two text pieces
        must not automatically break the logical continuation") is
        ALWAYS skipped over too, regardless of the searching zone's own
        tag/flow membership - unlike footnote_flow_tags (which is only a
        blocker when it doesn't match), a non-flow zone (image/figure/
        equation) never has real reading-flow text to be "the previous
        zone" for ANY searching zone, so it is never a valid match either
        way. Defaults to core.constants.NON_FLOW_TAGS (the XML profile's
        own literal tag names) when not given - correct out of the box
        for the XML profile; gui/main_window.py threads the active
        profile's own derived set (see core/profile_manager.py/
        core/cup_config.py) for EPUB/CUPEPUB, whose tag names differ."""
        zone = self.zones.get(zone_id)
        if not zone:
            return None
        page_marker_tags = page_marker_tags if page_marker_tags is not None else {"pagenumber"}
        footnote_flow_tags = footnote_flow_tags or set()
        non_flow_tags = non_flow_tags if non_flow_tags is not None else NON_FLOW_TAGS
        current_is_footnote_flow = zone.tag in footnote_flow_tags

        def _skip(candidate) -> bool:
            if candidate.tag in page_marker_tags:
                return True
            if candidate.tag in non_flow_tags:
                return True
            if (candidate.tag in footnote_flow_tags) != current_is_footnote_flow:
                return True
            return False

        from core import reading_order
        if self._counts_in_reading_order(zone):
            ordered = [z.zone_id for z in self.get_logical_top_level_zones(zone.page)]
            if zone_id not in ordered:
                return None
            idx = ordered.index(zone_id)
            j = idx - 1
            while j >= 0 and _skip(self.zones[ordered[j]]):
                j -= 1
            if j >= 0:
                return ordered[j]
            for page in sorted({z.page for z in self.zones.values() if z.page < zone.page}, reverse=True):
                candidates = [z.zone_id for z in self.get_logical_top_level_zones(page)]
                k = len(candidates) - 1
                while k >= 0 and _skip(self.zones[candidates[k]]):
                    k -= 1
                if k >= 0:
                    return candidates[k]
            return None
        elif zone.parent_id and zone.parent_id in self.zones:
            ordered = reading_order.sort_children_ids(self, zone.parent_id)
        else:
            ordered = reading_order.flatten_document_order(self)
        if zone_id not in ordered:
            return None
        idx = ordered.index(zone_id)
        j = idx - 1
        while j >= 0 and _skip(self.zones[ordered[j]]):
            j -= 1
        return ordered[j] if j >= 0 else None

    def get_zone_pages(self, zone_id: str) -> set:
        """Returns the set of PDF pages represented by zone_id's Merge
        Previous chain - zone_id itself plus every zone linked to it in
        either direction via attributes["merge_target"] pointers (its own
        target, that target's target, and so on back through the chain;
        plus any later zone that points AT one of those). Each individual
        merge_with_previous call already enforces same-page between a zone
        and its immediate target, so in normal operation a chain is always
        single-page - this exists purely as a defensive check (spec Part 5)
        against a chain that spans multiple pages, e.g. from a hand-edited
        or legacy project file, which merge_with_previous below rejects
        rather than extending."""
        if zone_id not in self.zones:
            return set()
        visited = {zone_id}
        stack = [zone_id]
        while stack:
            zid = stack.pop()
            target_id = self.zones[zid].attributes.get("merge_target")
            if target_id and target_id in self.zones and target_id not in visited:
                visited.add(target_id)
                stack.append(target_id)
        changed = True
        while changed:
            changed = False
            for z in self.zones.values():
                if z.zone_id in visited:
                    continue
                if z.attributes.get("merge_target") in visited:
                    visited.add(z.zone_id)
                    changed = True
        return {self.zones[zid].page for zid in visited}

    def merge_with_previous(self, zone_id: str, join: str = " ", page_marker_tags=None,
                              footnote_flow_tags=None, non_flow_tags=None):
        """Marks zone_id as a LOGICAL continuation of the previous
        COMPATIBLE CONTENT zone in the same logical reading flow (see
        _find_previous_in_reading_order - page_marker_tags/
        footnote_flow_tags are forwarded straight through to it; gui/
        main_window.py supplies the active profile's own sets). This is
        NOT a geometric operation:
        Merge Previous == logical text merge, never a bbox union, never a
        deletion. Both zones remain fully independent physical zones -
        separately selectable, resizable, listed in the hierarchy, with
        their own untouched bbox/page/tag - the zone count never changes.

        zone_id gets attributes["merged_with_previous"]=True,
        attributes["merge_target"]=<previous zone id>, and
        attributes["merge_join"]=join (" " for "James"+"Joyce" ->
        "James Joyce", "" for "inter"+"national" -> "international" - spec
        98.17/98.18). Those flags drive (a) a yellow display override
        (gui/pdf_viewer.py._zone_color) and (b) core/epub_xml_generator.py
        (CUPEPUB path) / core/paragraph_merge.py combining the two zones'
        independently-extracted text into ONE logical element at
        generation time, using join as the separator (TEXT_MERGE_TAGS
        pairs only - for anything else the flag still applies for display,
        generation just leaves them as separate elements).

        TAG COMPATIBILITY (spec 98.22): a structural/container zone cannot
        safely absorb a different tag's content (e.g. "H1 + Para should
        not automatically become one zone") - required for EVERY pairing,
        same-page or cross-page, not just cross-page as an earlier version
        of this check only enforced.

        CROSS-PAGE merging: _find_previous_in_reading_order already only
        ever returns a different-page candidate when zone_id is the very
        first counting zone on its own page (e.g. a Table/List/Paragraph
        that visually continues from page 1 onto page 2) - never an
        arbitrary earlier zone. core/paragraph_merge.py's merge-chain
        resolution and core/xml_generator.py's _merged_chain_text already
        read each chain zone's OWN page independently (never assume same-
        page) - the explicit Merge Previous path was always cross-page-
        safe end-to-end.

        Returns (success, message, zone_id) - zone_id is returned unchanged
        (nothing is deleted, so there's no different id to re-select)."""
        zone = self.zones.get(zone_id)
        if not zone:
            return False, "No zone selected.", None
        prev_id = self._find_previous_in_reading_order(zone_id, page_marker_tags, footnote_flow_tags, non_flow_tags)
        if not prev_id:
            return False, "No compatible previous content zone found.", None
        prev = self.zones[prev_id]
        if prev.tag != zone.tag:
            return False, "Previous content is not compatible with this zone.", None

        self._push_undo()
        zone.attributes.pop("merged_formatted_text", None)  # superseded (round-6) cached-text approach
        zone.attributes["merged_with_previous"] = True
        zone.attributes["merge_target"] = prev_id
        zone.attributes["merge_join"] = join
        # merged zone no longer counts on its own (see _counts_in_reading_order) -
        # only ITS OWN page needs renumbering; prev's page/count/status is
        # unaffected either way (same-page or cross-page).
        self.normalize_reading_order(zone.page)
        debug_log.log("MERGE", f"current={zone_id} previous={prev_id}",
                       f"current_bbox={[round(v, 1) for v in zone.bbox]} (unchanged)",
                       f"previous_bbox={[round(v, 1) for v in prev.bbox]} (unchanged)",
                       f"cross_page={prev.page != zone.page}", f"join={join!r}",
                       "merged_with_previous=True")
        return True, "", zone_id

    # ---------- Index-only merge ----------
    # This is deliberately separate from merge_with_previous().  Index Merge
    # is a PHYSICAL merge for split index entries: two index rectangles become
    # one rectangle and the lower zone is removed.  Normal Merge Previous is
    # still logical-only and is completely unchanged.
    _INDEX_MERGE_TAGS = {"indexprimary", "indexsecondary", "indexterritory"}

    def is_index_zone(self, zone: Optional[Zone]) -> bool:
        """Return True only for the EPUB index hierarchy zone tags."""
        return bool(zone and zone.tag in self._INDEX_MERGE_TAGS)

    @staticmethod
    def _bbox_union(a, b):
        return [
            min(float(a[0]), float(b[0])),
            min(float(a[1]), float(b[1])),
            max(float(a[2]), float(b[2])),
            max(float(a[3]), float(b[3])),
        ]

    @staticmethod
    def _horizontal_overlap(a, b) -> float:
        return max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))

    def _find_previous_index_zone(self, zone_id: str) -> Optional[str]:
        """Find the nearest Index zone physically above the selected zone.

        Index levels and parent IDs do NOT have to match. The zone above
        determines the final tag and parent of the merged result.
        """
        zone = self.zones.get(zone_id)
        if not self.is_index_zone(zone):
            return None

        zx0, zy0, zx1, zy1 = map(float, zone.bbox)
        zmid = (zx0 + zx1) / 2.0
        candidates = []

        for other in self.zones.values():
            if other.zone_id == zone_id or other.page != zone.page:
                continue
            if not self.is_index_zone(other):
                continue

            ox0, oy0, ox1, oy1 = map(float, other.bbox)

            # Candidate must be above the selected zone.
            if oy1 > zy0 + 3.0:
                continue

            # Stay in the same visual column. Overlap is preferred; a modest
            # horizontal gap is allowed for scanned index layouts.
            overlap = self._horizontal_overlap(other.bbox, zone.bbox)
            if overlap <= 0.0:
                omid = (ox0 + ox1) / 2.0
                if abs(omid - zmid) > max(20.0, (zx1 - zx0) * 0.35):
                    continue

            gap = max(0.0, zy0 - oy1)
            candidates.append((
                gap,
                -oy1,
                abs(((ox0 + ox1) / 2.0) - zmid),
                other.serial if other.serial is not None else 10**9,
                other.created_order,
                other.zone_id,
            ))

        if not candidates:
            return None

        candidates.sort()
        return candidates[0][-1]

    def merge_index_zones(self, zone_ids, join: str = " "):
        """Physically merge Index zones.

        Different Index levels are allowed. The TOPMOST zone always survives,
        and its tag/parent become the final tag/parent.

        Examples:
            IndexPrimary + IndexTerritory  -> IndexPrimary
            IndexSecondary + IndexTerritory -> IndexSecondary
            IndexPrimary + IndexSecondary -> IndexPrimary
        """
        if isinstance(zone_ids, str):
            zone_ids = [zone_ids]

        ids = list(dict.fromkeys(zone_ids or []))
        zones = [self.zones.get(zid) for zid in ids]

        if not zones or any(z is None for z in zones):
            return False, "One or more selected zones no longer exist.", None
        if len(zones) < 2:
            return False, "Select at least two Index zones.", None
        if any(not self.is_index_zone(z) for z in zones):
            return False, "Index Merge is available only for Index zones.", None
        if len({z.page for z in zones}) != 1:
            return False, "Index Merge requires zones from the same page.", None

        # IMPORTANT: no same-tag and no same-parent requirement.
        # Sort by actual page geometry, not Reading Order.
        zones.sort(key=lambda z: (
            float(z.bbox[1]),
            float(z.bbox[0]),
            z.serial if z.serial is not None else 10**9,
            z.created_order,
        ))

        for upper, lower in zip(zones, zones[1:]):
            if float(lower.bbox[1]) < float(upper.bbox[1]):
                return False, "Index zones are not in top-to-bottom order.", None

            overlap = self._horizontal_overlap(upper.bbox, lower.bbox)
            if overlap <= 0.0:
                ux = (float(upper.bbox[0]) + float(upper.bbox[2])) / 2.0
                lx = (float(lower.bbox[0]) + float(lower.bbox[2])) / 2.0
                if abs(ux - lx) > max(
                    20.0,
                    (float(lower.bbox[2]) - float(lower.bbox[0])) * 0.35
                ):
                    return False, "Selected Index zones are not in the same column.", None

        self._push_undo()

        # The TOPMOST zone is the survivor.
        keep = zones[0]
        final_tag = keep.tag
        final_parent = keep.parent_id
        union_bbox = list(keep.bbox)

        pieces = []
        for z in zones:
            piece = (z.text or "").strip()
            if piece:
                pieces.append(piece)
            union_bbox = self._bbox_union(union_bbox, z.bbox)

        keep.bbox = union_bbox
        keep.text = join.join(pieces) if join else "".join(pieces)

        # Final tag and parent ALWAYS come from the zone above.
        keep.tag = final_tag
        keep.parent_id = final_parent
        keep.level = self._compute_level(keep.tag, keep.parent_id)

        keep.attributes.pop("merged_with_previous", None)
        keep.attributes.pop("merge_target", None)
        keep.attributes.pop("merge_join", None)

        for z in zones[1:]:
            # Preserve any children.
            for cid in list(z.children):
                child = self.zones.get(cid)
                if child is not None:
                    child.parent_id = keep.zone_id
                    if cid not in keep.children:
                        keep.children.append(cid)

            if z.parent_id and z.parent_id in self.zones:
                siblings = self.zones[z.parent_id].children
                if z.zone_id in siblings:
                    siblings.remove(z.zone_id)

            self.zones.pop(z.zone_id, None)

        # Do not run reparent_page() here: after the bbox union it could move
        # the surviving Index zone into an unrelated geometric container.
        self._recompute_subtree_levels(keep.zone_id)
        self.normalize_reading_order(keep.page)
        self.repair_orphan_merges({z.zone_id: keep.zone_id for z in zones[1:]})

        debug_log.log(
            "INDEX_MERGE",
            f"kept={keep.zone_id}",
            f"merged={[z.zone_id for z in zones[1:]]}",
            f"final_tag={keep.tag}",
            f"final_parent={keep.parent_id}",
            f"bbox={[round(v, 1) for v in keep.bbox]}",
        )
        return True, "", keep.zone_id

    def merge_index_with_previous(self, zone_id: str, join: str = " "):
        """Merge an Index zone with the nearest Index zone physically above.

        No Index level/parent matching is required. The zone above determines
        the resulting tag and parent. This never calls merge_with_previous().
        """
        zone = self.zones.get(zone_id)
        if not self.is_index_zone(zone):
            return False, "Index Merge is available only on index zones.", None

        prev_id = self._find_previous_index_zone(zone_id)
        if not prev_id:
            return False, "No Index zone above this zone in the same column.", None

        return self.merge_index_zones([prev_id, zone_id], join=join)

    def unmerge(self, zone_id: str) -> bool:
        """Removes a Merge Previous relationship from zone_id, restoring its
        normal (non-yellow) display and its OWN Reading Order position -
        spec Part 8/TEST 4/TEST 10 ("Split Merged(B+C)" -> restores B and C
        as independent top-level items at the merged entity's former
        position): since this app never creates a separate merged-parent
        object (Merge Previous is logical-only - see merge_with_previous),
        "splitting" a merge means exactly this: clear the flag so zone_id
        counts in Reading Order again, and reinsert it immediately after its
        former merge_target (which is where it always logically belonged -
        "next zone after the target it was merged into"), before every
        following zone shifts down by one. Never touches bbox - geometry was
        never changed by merging in the first place, so there's nothing to
        restore there.

        Cross-page merges (see merge_with_previous) need one adjustment:
        the former target's serial is a position in a DIFFERENT page's own
        1..N sequence (Reading Order restarts at 1 per page), so tying to
        it would be meaningless on zone's own page. A cross-page merge
        target is only ever found when zone_id was the very FIRST counting
        zone on its own page to begin with (see
        _find_previous_in_reading_order), so unmerging puts it back
        exactly there: first on its own page."""
        zone = self.zones.get(zone_id)
        if not zone or not zone.attributes.get("merged_with_previous"):
            return False
        target = self.zones.get(zone.attributes.get("merge_target"))
        self._push_undo()
        zone.attributes.pop("merged_with_previous", None)
        zone.attributes.pop("merge_target", None)
        # merge_join has no meaning without the two flags above - leaving
        # it behind was a confirmed real bug: a later re-split/re-zoning
        # of the surrounding region could produce a brand-new zone that
        # never had its own explicit merge relationship at all, yet still
        # carried this orphaned attribute forward if anything ever copied
        # zone.attributes wholesale - indistinguishable, on inspection,
        # from a genuine merge that just lost its two authoritative flags.
        zone.attributes.pop("merge_join", None)
        if zone.attributes.pop("auto_continuation", None) is not None:
            # an automatic cross-page continuation the user undid: Auto Zone
            # must never link this pair again (auto_zoning.continuity)
            zone.attributes.pop("auto_continuation_reason", None)
            zone.attributes["continuation_rejected"] = True
        if target is not None and target.page == zone.page and target.serial is not None:
            # Temporary tie with its former target's CURRENT serial - purely
            # to seed the correct sort position for the renumber pass below
            # (stable sort + created_order tie-break places it immediately
            # after the target, since it was necessarily created later).
            # Immediately overwritten with a clean integer by
            # normalize_reading_order(), so this value is never displayed.
            zone.serial = target.serial
        elif target is not None and target.page != zone.page:
            zone.serial = 0  # sorts first on zone's own page - see docstring
        self.normalize_reading_order(zone.page)
        debug_log.log("MERGE", f"unmerge {zone_id}")
        return True

    def delete_zone(self, zone_id: str, cascade: bool = True):
        """cascade=True (default): deletes the zone and its whole subtree.
        cascade=False: deletes only this zone, promoting its direct children
        up to its own parent so they (and their content) survive."""
        self._push_undo()
        zone_for_page = self.zones.get(zone_id)
        page = zone_for_page.page if zone_for_page else None
        if cascade:
            replaced = self._removal_replacements(self._subtree_ids(zone_id))
            self._delete_recursive(zone_id)
            if page is not None:
                self.normalize_reading_order(page)  # close the gap left by the deleted zone(s)
            self.repair_orphan_merges(replaced)  # a zone merged onto a deleted one must not dangle
            return
        zone = self.zones.get(zone_id)
        if not zone:
            return
        grandparent_id = zone.parent_id
        for cid in list(zone.children):
            self._reparent_no_undo(cid, grandparent_id)
        if grandparent_id and grandparent_id in self.zones:
            if zone_id in self.zones[grandparent_id].children:
                self.zones[grandparent_id].children.remove(zone_id)
        replaced = self._removal_replacements([zone_id])
        self.zones.pop(zone_id, None)
        self.normalize_reading_order(page)
        self.repair_orphan_merges(replaced)

    def _delete_recursive(self, zone_id: str):
        zone = self.zones.get(zone_id)
        if not zone:
            return
        for cid in list(zone.children):
            self._delete_recursive(cid)
        if zone.parent_id and zone.parent_id in self.zones:
            if zone_id in self.zones[zone.parent_id].children:
                self.zones[zone.parent_id].children.remove(zone_id)
        self.zones.pop(zone_id, None)

    def duplicate_zone(self, zone_id: str, offset=(10, 10)) -> Optional[Zone]:
        self._push_undo()
        src = self.zones.get(zone_id)
        if not src:
            return None
        zid = self.next_zone_id()
        bbox = [src.bbox[0] + offset[0], src.bbox[1] + offset[1],
                src.bbox[2] + offset[0], src.bbox[3] + offset[1]]
        zone = Zone(zone_id=zid, page=src.page, tag=src.tag, bbox=bbox, parent_id=src.parent_id,
                    level=src.level, column=src.column, attributes=dict(src.attributes),
                    created_order=self._next_created_order_value(), source_zone_id=src.zone_id)
        zone.serial = 10 ** 9  # placeholder - normalize_reading_order appends it at the end
        self.zones[zid] = zone
        if zone.parent_id and zone.parent_id in self.zones:
            self.zones[zone.parent_id].children.append(zid)
        self._refresh_text(zone)
        self.normalize_reading_order(zone.page)
        return zone

    # ---------- automatic numbered-notes split ----------
    def auto_split_numbered_notes(self, parent_zone_id: str, note_tag: str = "fn",
                                  page_marker_tags=None):
        """Automatically split ONE selected notes/endnotes zone by numbered notes.

        Detection is deliberately limited to the selected zone.  The resulting
        pieces are created through the existing split_zone() implementation so
        split history, child/source metadata, undo batching and reading-order
        behavior remain identical to Horizontal Split.

        Returns a result dictionary; no mutation occurs when detection fails.
        """
        parent = self.zones.get(parent_zone_id)
        if not parent:
            return {"ok": False, "error": "Selected zone no longer exists."}

        if self.pdf_document is None:
            return {"ok": False, "error": "No PDF document is loaded."}

        try:
            page = self.pdf_document.get_page(parent.page)
            detection = detect_numbered_notes(page, parent.bbox, text_extractor)
        except Exception as exc:
            debug_log.log("AUTO_SPLIT_NUMBERS", f"detection failed: {exc}")
            return {"ok": False, "error": f"Could not analyze the selected zone: {exc}"}

        if not detection.note_lines:
            return {
                "ok": False,
                "error": "No reliable numbered notes were detected in the selected zone.",
                "warnings": detection.warnings,
            }

        # A boundary is needed between at least two logical regions.  A single
        # note with no heading/folio is already the selected zone and should
        # not be turned into an artificial split.
        if not detection.boundaries:
            return {
                "ok": False,
                "error": "Only one numbered-note region was detected; nothing to split.",
                "warnings": detection.warnings,
            }

        marker_tags = list(dict.fromkeys(page_marker_tags or []))
        if detection.page_number_bbox and not marker_tags:
            # Do not invent a page-marker tag.  The active profile owns this
            # vocabulary; the GUI will report that the folio remains unmapped.
            debug_log.log(
                "AUTO_SPLIT_NUMBERS",
                f"{parent_zone_id}: printed page number detected but active profile has no page-marker tag"
            )

        created = []
        self.begin_batch()
        try:
            # Reuse the canonical Horizontal Split backend.  It creates the
            # parent/child split-history structure and is suppressed to one
            # undo entry by begin_batch().
            created = self.split_zone(
                parent_zone_id,
                detection.boundaries,
                child_tag=note_tag,
                axis="h",
            )

            if not created:
                return {
                    "ok": False,
                    "error": "The split operation created no zones.",
                    "warnings": detection.warnings,
                }

            # Classify the children by their physical Y ranges.  The detector
            # produced boundaries in the exact top-to-bottom order used by
            # split_zone(), so classification is deterministic.
            hb = detection.heading_bbox[3] if detection.heading_bbox else None
            folio_y = detection.page_number_bbox[1] if detection.page_number_bbox else None
            cont_bottom = detection.continuation_bbox[3] if detection.continuation_bbox else None

            for child in created:
                cy0, cy1 = child.bbox[1], child.bbox[3]

                if hb is not None and cy0 < hb and cy1 <= hb + 1.0:
                    child.tag = "h2"
                    child.attributes["auto_split_role"] = "heading"
                elif cont_bottom is not None and cy1 <= cont_bottom + 2.0 and (hb is None or cy0 >= hb - 1.0):
                    # Leading unnumbered text = the continuation of the
                    # previous page's last note. Kept as a note piece of its
                    # own (never a heading, never glued to the first
                    # numbered note) so Merge Previous can attach exactly
                    # this text to that note.
                    child.tag = note_tag
                    child.attributes["auto_split_role"] = "continuation"
                elif folio_y is not None and cy0 >= folio_y - 1.0:
                    if marker_tags:
                        child.tag = marker_tags[0]
                        child.attributes["auto_split_role"] = "page_number"
                    else:
                        child.tag = note_tag
                        child.attributes["auto_split_role"] = "page_number_unmapped"
                else:
                    child.tag = note_tag
                    child.attributes["auto_split_role"] = "numbered_note"

                child.level = self._compute_level(child.tag, child.parent_id)
                self._refresh_text(child)

            self.normalize_reading_order(parent.page)

            debug_log.log(
                "AUTO_SPLIT_NUMBERS",
                f"{parent_zone_id}: created={len(created)} notes={len(detection.note_lines)} "
                f"heading={bool(detection.heading_bbox)} folio={bool(detection.page_number_bbox)}"
            )
        finally:
            self.end_batch()

        return {
            "ok": True,
            "parent_id": parent_zone_id,
            "created": created,
            "note_count": len(detection.note_lines),
            "heading": bool(detection.heading_bbox),
            "page_number": detection.page_number_text,
            "page_number_mapped": bool(detection.page_number_bbox and marker_tags),
            "warnings": detection.warnings,
        }

    # ---------- horizontal split ----------
    def split_zone(self, parent_zone_id: str, split_coords: list, child_tag: Optional[str] = None, axis: str = "h"):
        """Splits parent_zone_id into child zones at the given boundaries
        (PDF coords, sorted) - axis="h" (default): stacked top-to-bottom
        at Y boundaries (the original behavior, unchanged). axis="v":
        side-by-side left-to-right at X boundaries. The parent zone is
        preserved (not replaced) as split-history metadata only - it
        stops being its own XML content zone once it has split children
        (xml_generator._gen_zone_multi flattens it out, emitting only
        the children - recursively, so a split-of-a-split still resolves
        to only its own leaf pieces, never the intermediate parent).
        Works identically for any tag - child_tag defaults to the
        semantic mapping in SPLIT_CHILD_TAG for a structural container
        (List -> list-item, etc), or the parent's own tag for every
        other (content/leaf) tag."""
        self._push_undo()
        parent = self.zones.get(parent_zone_id)
        if not parent:
            return []
        x0, y0, x1, y1 = parent.bbox
        lo, hi = (x0, x1) if axis == "v" else (y0, y1)
        boundaries = sorted(c for c in split_coords if lo < c < hi)
        edges = [lo] + boundaries + [hi]
        # Structural containers map to a semantic child tag (List -> list-item,
        # etc, see SPLIT_CHILD_TAG); every other tag is content/leaf and
        # splitting it produces more zones of the SAME tag by default.
        tag = child_tag or SPLIT_CHILD_TAG.get(parent.tag, parent.tag)
        split_group_id = f"split_{parent_zone_id}_{self._next_created_order}"
        created = []
        for i in range(len(edges) - 1):
            c0, c1 = edges[i], edges[i + 1]
            bbox = [c0, y0, c1, y1] if axis == "v" else [x0, c0, x1, c1]
            zid = self.next_zone_id()
            zone = Zone(zone_id=zid, page=parent.page, tag=tag, bbox=bbox,
                        parent_id=parent_zone_id, column=parent.column,
                        created_order=self._next_created_order_value(),
                        split_group_id=split_group_id, is_split=True, source_zone_id=parent_zone_id)
            zone.level = self._compute_level(tag, parent_zone_id)
            self.zones[zid] = zone
            parent.children.append(zid)
            self._refresh_text(zone)
            created.append(zone)
        # The split pieces take over the parent's position in the Reading
        # Order sequence (the parent itself stops counting once it has
        # split children - see _counts_in_reading_order), occupying
        # consecutive positions right after it, in piece order (top-to-
        # bottom for a horizontal split, left-to-right for a vertical one
        # - "edges" is already built in that order for either axis).
        if parent.serial is not None:
            base = parent.serial
        else:
            # A parent with no slot of its own (e.g. a Merge-Previous'd
            # zone) still has a derived position - the same one
            # flatten_document_order sorts it by. Seeding from 0 here used
            # to throw a merged zone's split pieces to the TOP of its page.
            from core import reading_order
            rank, value, _created = reading_order._order_key(parent, self)
            base = value if rank == 0 else 10 ** 9
        for i, piece in enumerate(created, start=1):
            piece.serial = base + i * 0.0001
        # Merge -> Split: a merge relationship on (or pointing at) the now-
        # superseded parent is carried over to the piece that holds that
        # end of the text - see normalize_merge_relationships.
        self.normalize_merge_relationships()
        self.normalize_reading_order(parent.page)
        debug_log.log("SPLIT", parent_zone_id, f"axis={axis}", f"boundaries = {[round(b, 1) for b in boundaries]}",
                       f"created {len(created)} zones")
        return created

    # ---------- defensive hierarchy validation ----------
    def validate_hierarchy(self) -> list:
        """Read-only consistency check over the physical parent/child graph -
        NEVER modifies or deletes anything, only reports. Returns a list of
        human-readable issue strings (empty = clean). Intended as a
        diagnostic safety net (e.g. before Generate XML, or after a project
        load from disk) to catch data-model corruption early rather than
        letting it silently produce wrong/missing XML downstream.

        Checks:
          - missing parent IDs (parent_id points to a zone that no longer exists)
          - children lists referencing nonexistent zone IDs
          - parent/child mismatch (a listed child whose own parent_id doesn't
            point back, or a zone with a parent_id whose children list omits it)
          - a zone listed as its own parent
          - cycles anywhere in the parent_id chain
          - duplicate child IDs within one zone's children list
          - incorrect levels (zone.level not matching the actual containment-
            chain depth from parent_id - skipped for any zone already
            flagged as part of a cycle, since depth is undefined there)
        """
        issues = []
        cyclic_ids = set()

        for zid, zone in self.zones.items():
            if zone.parent_id == zid:
                issues.append(f"{zid}: is listed as its own parent")
            if zone.parent_id is not None and zone.parent_id not in self.zones:
                issues.append(f"{zid}: parent_id {zone.parent_id!r} does not exist")

            seen_children = set()
            for cid in zone.children:
                if cid in seen_children:
                    issues.append(f"{zid}: duplicate child id {cid!r} in children list")
                    continue
                seen_children.add(cid)
                if cid not in self.zones:
                    issues.append(f"{zid}: children list references nonexistent zone {cid!r}")
                    continue
                child = self.zones[cid]
                if child.parent_id != zid:
                    issues.append(f"{zid}: lists {cid!r} as child, but {cid}.parent_id={child.parent_id!r}")

            if zone.parent_id is not None and zone.parent_id in self.zones:
                parent = self.zones[zone.parent_id]
                if zid not in parent.children:
                    issues.append(f"{zid}: parent_id={zone.parent_id!r} but "
                                  f"{zone.parent_id}.children does not list {zid!r}")

        # Cycle detection: walk each zone's parent_id chain looking for a repeat.
        for zid in self.zones:
            path_seen = set()
            cur = zid
            while cur is not None and cur in self.zones:
                if cur in path_seen:
                    issues.append(f"{zid}: parent_id chain contains a cycle at {cur!r}")
                    cyclic_ids.update(path_seen)
                    break
                path_seen.add(cur)
                cur = self.zones[cur].parent_id

        # Level check - uses a cycle-safe bounded walk (never trusts
        # self._compute_level here, since that would hang forever on a cycle
        # this same pass is meant to be able to detect and report).
        for zid, zone in self.zones.items():
            if zid in cyclic_ids:
                continue
            depth = 0
            seen = set()
            pid = zone.parent_id
            while pid and pid in self.zones and pid not in seen:
                seen.add(pid)
                depth += 1
                pid = self.zones[pid].parent_id
            if zone.level != depth:
                issues.append(f"{zid}: level={zone.level} but computed containment depth={depth}")

        return issues
