"""Turns a loaded reference project (reference_loader.RawReference) into a
LayoutTemplate: for every reference zone, normalizes its geometry to its own
page and pulls font stats from the reference PDF itself, then groups
samples by tag (layout_template.TagProfile). Never modifies the reference
project or its PDF."""
from collections import Counter

from core import text_extractor, column_detector
from core.formatting_detector import detect_bold_italic

from auto_zoning.layout_template import LayoutTemplate, compute_features
from auto_zoning import pdf_block_detector


def _char_center_in_bbox(cb, bbox, tol=1.0):
    cx0, cy0, cx1, cy1 = cb
    x0, y0, x1, y1 = bbox
    cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
    return (x0 - tol) <= cx <= (x1 + tol) and (y0 - tol) <= cy <= (y1 + tol)


def _zone_font_stats(page, bbox):
    """Dominant font size + majority bold/italic for exactly the characters
    inside bbox - walks the same cached rawdict text_extractor already
    builds for this page (text_extractor._get_rawdict), so this doesn't
    reparse the page a second way; only the bbox-filtering + aggregation
    differs from text_extractor.extract_lines (which returns inline-
    formatted TEXT for XML, not raw per-line font size)."""
    raw = text_extractor._get_rawdict(page)
    size_counts = Counter()
    bold_chars = italic_chars = total_chars = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                matched = [c for c in chars if _char_center_in_bbox(c["bbox"], bbox)]
                if not matched:
                    continue
                bold, italic = detect_bold_italic(span.get("flags", 0), span.get("font", ""))
                size = round(span.get("size", 0), 1)
                n = len(matched)
                size_counts[size] += n
                total_chars += n
                if bold:
                    bold_chars += n
                if italic:
                    italic_chars += n
    if not size_counts:
        return None, False, False
    dom_size = size_counts.most_common(1)[0][0]
    return dom_size, (total_chars > 0 and bold_chars / total_chars >= 0.5), \
           (total_chars > 0 and italic_chars / total_chars >= 0.5)


def _band_for_bbox(bands, bbox):
    y_mid = (bbox[1] + bbox[3]) / 2.0
    for band in bands:
        if band.y0 - 1.0 <= y_mid <= band.y1 + 1.0:
            return band
    return bands[-1] if bands else None


def analyze(raw_reference, template: LayoutTemplate = None) -> LayoutTemplate:
    """Builds (or extends, if `template` is given - used by loading a
    second/third "Add Reference") a LayoutTemplate from raw_reference.zones.
    Per page, computes the page's own body font size and column bands ONCE
    (shared context for every zone on that page), then adds one sample per
    zone to the matching TagProfile."""
    template = template or LayoutTemplate()
    template.source_names.append(raw_reference.json_path)

    zones_by_id = {z.zone_id: z for z in raw_reference.zones}
    zones_by_page = {}
    for z in raw_reference.zones:
        zones_by_page.setdefault(z.page, []).append(z)

    for page_num, zones in zones_by_page.items():
        page_w, page_h = raw_reference.page_sizes.get(page_num, (0, 0))
        if not page_w or not page_h:
            continue
        page = raw_reference.pdf_document.get_page(page_num)
        lines = pdf_block_detector.detect_lines(page)
        body_size = pdf_block_detector.page_body_font_size(lines)
        bands = column_detector.detect_bands(page_w, page_h, lines)

        for zone in zones:
            font_size, bold, italic = _zone_font_stats(page, zone.bbox)
            band = _band_for_bbox(bands, zone.bbox)
            col_bounds = column_detector.column_bounds_for_bbox(band, zone.bbox) if band else (0.0, page_w)
            list_marker = zone.attributes.get("list_type") if zone.tag == "list" else None
            if zone.tag == "list-item" and zone.parent_id in zones_by_id:
                list_marker = zones_by_id[zone.parent_id].attributes.get("list_type")
            features = compute_features(
                bbox=zone.bbox, page_width=page_w, page_height=page_h, column_bounds=col_bounds,
                text=zone.text, font_size=font_size, bold=bold, italic=italic,
                body_font_size=body_size, list_marker=list_marker)
            parent_tag = zones_by_id[zone.parent_id].tag if zone.parent_id in zones_by_id else None
            # `list_marker` (not zone.attributes.get("list_type")) so a
            # list-item's OWN TagProfile.list_types is populated too (via
            # its inherited marker) - not just the "list" container tag's -
            # otherwise zone_matcher's list-marker feature never penalizes
            # a plain, unmarked candidate against the list-item profile
            # specifically, since that profile's own list_types would stay
            # empty forever (list-item zones never carry a "list_type"
            # attribute of their own).
            template.add_sample(zone.tag, features, parent_tag=parent_tag, list_type=list_marker)
    return template
