"""Orchestrates actual Zone creation for one page's auto-detected
candidates, in an order that lets core.zone_manager.ZoneManager's OWN
existing geometric containment auto-parenting (add_zone(auto_parent=True))
wire up list->list-item relationships for free - this module only decides
CREATION ORDER (a container before its contained children) and, for the
rare candidate whose bbox isn't strictly contained in its intended
container, an explicit zone_manager.set_parent() call. It never
reimplements containment detection itself."""
from dataclasses import dataclass, field


@dataclass
class PredictedZone:
    bbox: tuple
    tag: str
    confidence: float          # 0..100
    attributes: dict = field(default_factory=dict)
    children: list = field(default_factory=list)   # nested PredictedZone (e.g. a "list" candidate's list-items)
    text: str = None            # optional pre-supplied zone text - see _create_zone_tree's docstring


def _create_zone_tree(zone_manager, page: int, pz: PredictedZone) -> str:
    """Creates pz's own zone, then RECURSIVELY creates every descendant in
    pz.children, however deep (e.g. table > list-bullet > list-item, a
    3-level case Auto Analyse's table detection produces that the original,
    single-level version of this function - built only for Auto Zone's
    simpler list->list-item / figure->[graphic,caption] cases - could not
    have handled: it only ever looped pz.children once, silently dropping
    any grandchildren). A container is always created before its own
    children, so each child's add_zone call sees its container already in
    place and picks it up via the SAME existing geometric containment
    auto-parenting every manually-drawn nested zone already relies on
    (smallest containing zone wins - so a grandchild correctly lands on
    its immediate container, e.g. list-item on list-bullet, not on the
    outer table). Every created zone's attributes["confidence"]=<score> is
    set at creation time (round-trip through the existing generic
    `attributes` dict - zero Zone dataclass changes needed).
    attributes["source"] defaults to "auto" but is left alone if the
    PredictedZone already carries its own value (e.g. core.ocr.ocr_service
    tags its candidates "ocr" instead, so callers can tell an OCR-derived
    candidate apart from a geometry/font-based one).

    pz.text is left as None by every EXISTING caller (Auto Zone/Auto
    Analyse): add_zone's own internal _refresh_text() call already
    populates real text by re-extracting the digital PDF's text layer at
    the new zone's bbox, which is always correct there and must not be
    disturbed. An OCR-derived candidate has no digital text layer to
    extract (its bbox sits on a scanned page) - core.ocr.ocr_service sets
    pz.text explicitly to the actual OCR-recognized text for exactly that
    reason, applied here (after add_zone, overriding its own now-empty
    _refresh_text result) only when a PredictedZone actually supplies one."""
    attrs = dict(pz.attributes)
    attrs.setdefault("source", "auto")
    attrs["confidence"] = pz.confidence
    zone = zone_manager.add_zone(page, pz.tag, list(pz.bbox), attributes=attrs)
    if pz.text is not None:
        zone.text = pz.text
    for child_pz in pz.children:
        child_id = _create_zone_tree(zone_manager, page, child_pz)
        if zone_manager.zones[child_id].parent_id != zone.zone_id:
            # bbox wasn't strictly contained (e.g. a marker-detected list
            # item a few points outside the run's own union bbox) - fall
            # back to an explicit parent link rather than leaving it a
            # stray top-level zone.
            zone_manager.set_parent(child_id, zone.zone_id)
    return zone.zone_id


def create_page(zone_manager, page: int, predicted_zones: list) -> list:
    """Creates every zone for `predicted_zones` (top-level candidates, each
    optionally carrying nested `.children`, to any depth) via
    _create_zone_tree. Returns the created top-level zone_ids IN THE SAME
    ORDER passed in (band/column reading order - see
    reading_order_engine.py, which consumes this list directly)."""
    return [_create_zone_tree(zone_manager, page, pz) for pz in predicted_zones]
