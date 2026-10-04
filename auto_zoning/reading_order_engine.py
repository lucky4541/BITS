"""Computes the band/column-aware reading-order position of a page's
top-level auto-created zones, then hands off to core.zone_manager.
ZoneManager's own existing, unmodified renumbering contract
(renumber_reading_order) - this module NEVER assigns reading order as the
final answer by itself; it only seeds the relative order that
renumber_reading_order() then compacts into a clean 1..N sequence (the same
contract every other zone-creating operation in the app already relies on -
see zone_manager.split_zone/duplicate_zone)."""


def order_page(zone_manager, page: int, band_order: list):
    """`band_order`: the zone_ids of this page's own TOP-LEVEL zones
    (parent_id is None), already in the exact band/column reading order the
    auto-zone engine determined when it created them (see
    hierarchy_builder.create_page, which builds zones in this same order) -
    a zone with a parent (e.g. a list-item) never appears here, since it
    never independently holds a Reading Order position anyway
    (ZoneManager._counts_in_reading_order)."""
    for i, zid in enumerate(band_order, start=1):
        zone = zone_manager.zones.get(zid)
        if zone is not None:
            zone.serial = i
    zone_manager.renumber_reading_order(page)
