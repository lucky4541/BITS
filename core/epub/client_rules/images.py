"""Image preparation for the client's (CUPEPUB) image rules:

  EPUB-019  cover 300 DPI; inline / icon images 135 DPI; all others 150 DPI
  EPUB-021  cover 1200 px wide or 1800 px high, never larger than
            1200 (W) x 1800 (H)

DPI is written into the image header only (PNG pHYs / JPEG JFIF density) -
the pixels stay byte-for-byte the same. The cover is the only image whose
pixels change: it is scaled (high-quality Lanczos) to fit the client's
1200 x 1800 box with its aspect ratio kept, then saved at 300 DPI."""
import io
import posixpath
import re
import struct
import zlib

COVER_W, COVER_H = 1200, 1800
COVER_DPI, INLINE_DPI, DEFAULT_DPI = 300, 135, 150
RASTER_EXT = (".png", ".jpg", ".jpeg")


def is_cover(name):
    return re.match(r"^cover\..*g$", posixpath.basename(name).lower()) is not None


def expected_dpi(name):
    base = posixpath.basename(name).lower()
    if is_cover(name):
        return COVER_DPI
    if "inline" in base or "icon" in base:
        return INLINE_DPI
    return DEFAULT_DPI


def cover_size_ok(w, h):
    return (h == COVER_H or w == COVER_W) and h <= COVER_H and w <= COVER_W


def cover_target_size(w, h):
    """The size that fits the 1200 x 1800 box exactly on one side."""
    scale = min(COVER_W / w, COVER_H / h)
    nw, nh = min(COVER_W, round(w * scale)), min(COVER_H, round(h * scale))
    if abs(scale - COVER_W / w) < 1e-9:
        nw = COVER_W
    else:
        nh = COVER_H
    return nw, nh


def info(data):
    """(format, width, height, dpi_x, dpi_y) or None."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            dpi = im.info.get("dpi") or (96, 96)
            if im.format == "JPEG" and "dpi" not in im.info and im.info.get("jfif_density"):
                dpi = im.info["jfif_density"]
            return im.format, im.size[0], im.size[1], int(round(float(dpi[0]))), int(round(float(dpi[1])))
    except Exception:  # noqa: BLE001
        return None


def set_png_dpi(data, dpi):
    """The PNG with its pHYs chunk set to `dpi` (pixels untouched)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    ppm = int(round(dpi / 0.0254))
    phys = struct.pack(">IIB", ppm, ppm, 1)
    chunk = struct.pack(">I", 9) + b"pHYs" + phys + struct.pack(">I", zlib.crc32(b"pHYs" + phys) & 0xFFFFFFFF)
    pos, out, done = 8, [data[:8]], False
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        whole = data[pos:pos + 12 + ln]
        if typ == b"pHYs":
            out.append(chunk)
            done = True
        elif typ == b"IDAT" and not done:
            out.append(chunk)
            out.append(whole)
            done = True
        else:
            out.append(whole)
        pos += 12 + ln
    return b"".join(out)


def set_jpeg_dpi(data, dpi):
    """The JPEG with its JFIF density set to `dpi` (entropy data untouched)."""
    if data[:2] != b"\xff\xd8":
        return None
    if data[2:4] == b"\xff\xe0" and data[6:11] == b"JFIF\x00":
        b = bytearray(data)
        b[13] = 1
        b[14:16] = struct.pack(">H", dpi)
        b[16:18] = struct.pack(">H", dpi)
        return bytes(b)
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x01" + struct.pack(">HH", dpi, dpi) + b"\x00\x00"
    return data[:2] + app0 + data[2:]


def set_dpi(data, dpi):
    if data[:4] == b"\x89PNG":
        return set_png_dpi(data, dpi)
    if data[:2] == b"\xff\xd8":
        return set_jpeg_dpi(data, dpi)
    return None


def resize_cover(data):
    """(new bytes, (w, h) before, (w, h) after) - the cover scaled into the
    client's box, same format, 300 DPI."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        fmt = im.format
        w, h = im.size
        nw, nh = cover_target_size(w, h)
        img = im.convert("RGB") if fmt == "JPEG" and im.mode not in ("RGB", "L") else im.copy()
    img = img.resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=95, subsampling=0, optimize=True, dpi=(COVER_DPI, COVER_DPI))
    else:
        img.save(buf, fmt or "PNG", dpi=(COVER_DPI, COVER_DPI), optimize=True)
    return buf.getvalue(), (w, h), (nw, nh)


def prepare(name, data, resize_cover_image=True):
    """(bytes, notes) - the image as the client requires it; notes lists what
    changed (empty when nothing did). Unreadable / non-raster images are
    returned unchanged."""
    if posixpath.splitext(name)[1].lower() not in RASTER_EXT:
        return data, []
    meta = info(data)
    if meta is None:
        return data, []
    _fmt, w, h, dx, dy = meta
    notes = []
    want = expected_dpi(name)
    if is_cover(name) and resize_cover_image and not cover_size_ok(w, h):
        data, before, after = resize_cover(data)
        notes.append(f"cover resized {before[0]}x{before[1]} -> {after[0]}x{after[1]} px")
        notes.append(f"DPI {dx} -> {want}")
        return data, notes
    if (dx, dy) != (want, want):
        new = set_dpi(data, want)
        if new is not None and (info(new) or (0,) * 5)[3:] == (want, want):
            data = new
            notes.append(f"DPI {dx} -> {want} (header only, pixels unchanged)")
    return data, notes
