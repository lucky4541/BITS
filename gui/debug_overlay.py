"""Debug overlays for the Auto Zone / Auto Tag engine, drawn on the PDF
viewer canvas on top of the zones (purely visual - never touches zone data).

Every overlay is computed from the same caches the engine uses (cached
rawdict, cached decoration segments, cached page analysis), so turning
overlays on never re-runs OCR or layout analysis."""
from core.text_extractor import _get_rawdict

TAG = "debug_overlay"


def _rect(viewer, bbox, **kw):
    x0, y0, x1, y1 = viewer.pdf_to_screen(bbox)
    return viewer.canvas.create_rectangle(x0, y0, x1, y1, tags=(TAG,), **kw)


def _text(viewer, x, y, text, color, anchor="nw", size=7):
    sx, sy, _, _ = viewer.pdf_to_screen((x, y, x + 1, y + 1))
    t = viewer.canvas.create_text(sx, sy, text=text, anchor=anchor, fill=color, font=("Segoe UI", size), tags=(TAG,))
    bb = viewer.canvas.bbox(t)
    if bb:
        bg = viewer.canvas.create_rectangle(bb[0] - 1, bb[1], bb[2] + 1, bb[3], fill="#FFFFFF", outline="",
                                            tags=(TAG,))
        viewer.canvas.tag_lower(bg, t)
    return t


def draw(viewer, app, ui):
    page_no = app.current_page
    page = app.pdf_document.get_page(page_no)
    c = viewer.canvas
    c.delete(TAG)

    if ui.overlay_enabled("chars") or ui.overlay_enabled("spans"):
        raw = _get_rawdict(page)
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if ui.overlay_enabled("spans") and span.get("bbox"):
                        _rect(viewer, span["bbox"], outline="#1E88E5", width=1)
                    if ui.overlay_enabled("chars"):
                        for ch in span.get("chars", []):
                            if ch.get("c", " ").strip():
                                _rect(viewer, ch["bbox"], outline="#B0BEC5", width=1)

    if ui.overlay_enabled("decorations"):
        from core import underline_detector
        for ev in underline_detector.debug_page_decorations(page):
            color = "#D50000" if ev["kind"] == "underline" else "#6200EA"
            for ch in ev["chars"]:
                _rect(viewer, ch["bbox"], outline=color, width=1, fill=color, stipple="gray12")
            s = ev["segment"]
            x0, y0, x1, _ = viewer.pdf_to_screen((s["x0"], s["y"], s["x1"], s["y"] + 0.1))
            c.create_line(x0, y0, x1, y0, fill=color, width=3, tags=(TAG,))
            chars = "".join(ch["c"] for ch in ev["chars"])
            _text(viewer, s["x0"], s["y"] + 1.5, f"{ev['kind'][0].upper()} {chars!r} {ev['confidence']:.2f}", color)

    need_layout = any(ui.overlay_enabled(k) for k in ("blocks", "floats"))
    if need_layout:
        layout, role_map = ui.zoner().analyse(page_no)
        for b in layout.blocks:
            is_float = b.kind in ("figure", "table")
            if is_float and ui.overlay_enabled("floats"):
                _rect(viewer, b.bbox, outline="#2E7D32", width=3, dash=(6, 3))
                _text(viewer, b.bbox[0], b.bbox[1], b.kind.upper(), "#2E7D32", size=8)
            elif not is_float and ui.overlay_enabled("blocks"):
                _rect(viewer, b.bbox, outline="#EF6C00", width=1, dash=(2, 2))
                roles = role_map.get(id(b), [])
                top = f"{roles[0].role} {roles[0].score:.2f}" if roles else b.kind
                _text(viewer, b.bbox[2], b.bbox[1], f"#{b.order} {top}", "#EF6C00", anchor="ne")

    zones = app.zone_manager.zones_on_page(page_no)
    if ui.overlay_enabled("reading_order"):
        ordered = sorted((z for z in zones if isinstance(z.serial, int) and z.parent_id is None),
                         key=lambda z: z.serial)
        pts = []
        for z in ordered:
            x0, y0, x1, y1 = viewer.pdf_to_screen(z.bbox)
            pts.append(((x0 + x1) / 2, (y0 + y1) / 2))
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            c.create_line(ax, ay, bx, by, fill="#00838F", width=2, arrow="last", tags=(TAG,))

    if ui.overlay_enabled("hierarchy"):
        by_id = {z.zone_id: z for z in zones}
        for z in zones:
            if z.parent_id and z.parent_id in by_id:
                p = by_id[z.parent_id]
                ax0, ay0, ax1, ay1 = viewer.pdf_to_screen(z.bbox)
                bx0, by0, bx1, by1 = viewer.pdf_to_screen(p.bbox)
                c.create_line((ax0 + ax1) / 2, ay0, (bx0 + bx1) / 2, by0 + 6, fill="#8E24AA", width=2,
                              arrow="last", dash=(4, 2), tags=(TAG,))

    if ui.overlay_enabled("decisions"):
        for z in zones:
            a = z.attributes
            if not a.get("auto_role") and a.get("confidence") is None:
                continue
            bd = a.get("confidence_breakdown") or {}
            parts = [f"{a.get('auto_role', '?')} -> {a.get('cup_name') or z.tag}"]
            if a.get("confidence") is not None:
                parts.append(f"{a['confidence']:.0f}%")
            if bd:
                parts.append(" ".join(f"{k[0].upper()}{v:.2f}" for k, v in bd.items()))
            if z.manual_override:
                parts.append("MANUAL")
            if z.locked:
                parts.append("LOCKED")
            color = "#C2185B" if a.get("needs_review") else "#37474F"
            if a.get("needs_review"):
                parts.append("REVIEW")
            _text(viewer, z.bbox[0], z.bbox[3] + 1, "  ".join(parts), color)
