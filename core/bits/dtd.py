"""The DTDs the output is validated against.

    JATS  profiles/JATS/dtd/JATS-journalpublishing1-4-mathml3.dtd (bundled,
          NLM JATS 1.4 - public domain)
    BITS  the BITS 2.2 book DTD: profiles/BITS/dtd/ (any BITS-book*.dtd in
          it, e.g. the unzipped "BITS 2.2 DTD" package from
          https://jats.nlm.nih.gov/extensions/bits/2.2/) or the path in the
          setting "bits_dtd_path".

The DOCTYPE written to the output uses the DTD file's own public identifier
(from its header) and file name."""
import glob
import os
import re

from lxml import etree

from core.resource_path import resource_path
from core.tag_knowledge.dtd_model import DTDModel

JATS_DTD = ("JATS-journalpublishing1-4-mathml3.dtd", "JATS-journalpublishing1-4.dtd",
            "JATS-archivearticle1-4-mathml3.dtd")
_cache = {}


class DTDNotFound(Exception):
    pass


def dtd_path(kind: str, settings: dict = None) -> str:
    settings = settings or {}
    kind = kind.upper()
    if kind == "JATS":
        if settings.get("jats_dtd_path") and os.path.isfile(settings["jats_dtd_path"]):
            return settings["jats_dtd_path"]
        d = resource_path("profiles", "JATS", "dtd")
        for name in JATS_DTD:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        raise DTDNotFound(f"JATS DTD not found in {d}")
    if settings.get("bits_dtd_path") and os.path.isfile(settings["bits_dtd_path"]):
        return settings["bits_dtd_path"]
    d = resource_path("profiles", "BITS", "dtd")
    found = sorted(glob.glob(os.path.join(d, "**", "BITS-book*.dtd"), recursive=True))
    if not found:
        raise DTDNotFound("BITS 2.2 DTD not installed - put the unzipped BITS 2.2 DTD files in "
                          f"{d} (or set bits_dtd_path)")
    # prefer the MathML3 variant of the newest version
    found.sort(key=lambda p: ("mathml3" not in p.lower(), -_version(p)))
    return found[0]


def _version(path):
    m = re.search(r"(\d+)-(\d+)", os.path.basename(path))
    return int(m.group(1)) * 100 + int(m.group(2)) if m else 0


def load(kind: str, settings: dict = None) -> DTDModel:
    path = dtd_path(kind, settings)
    if path not in _cache:
        _cache[path] = DTDModel(etree.DTD(path), source=path)   # by path: relative modules resolve
    return _cache[path]


def doctype(kind: str, settings: dict = None) -> str:
    """'<!DOCTYPE book PUBLIC "..." "file.dtd">' for the DTD in use."""
    root = "book" if kind.upper() == "BITS" else "article"
    try:
        path = dtd_path(kind, settings)
    except DTDNotFound:
        return ""
    public = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.read(4000)
    m = re.search(r'"(-//NLM//DTD [^"]+)"', head)
    if m:
        public = m.group(1)
    sysid = os.path.basename(path)
    return f'<!DOCTYPE {root} PUBLIC "{public}" "{sysid}">' if public else f'<!DOCTYPE {root} SYSTEM "{sysid}">'


def available(kind: str, settings: dict = None) -> bool:
    try:
        dtd_path(kind, settings)
        return True
    except DTDNotFound:
        return False
