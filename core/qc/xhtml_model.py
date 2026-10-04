"""XHTML side of the unified QC mapping model.

Walks every split in SPINE order (core.qc.package_manager) and records:
  * blocks (the innermost block-level element owning each run of text) with
    their XPath and source line, and every word with its block and character
    offset inside that block's text - so a difference can be shown in the
    rendered view, in the source view, and edited precisely;
  * page markers, recognised by the project's own structural signal
    (core.epub_structure.pagebreak_analyzer: epub:type="pagebreak"),
    never by a hard-coded tag name; their text is not content;
  * images: resolved file, perceptual hashes (same algorithm as the PDF
    side), size, the reading-order position (words before it), the figure
    caption (figcaption / caption / labelled adjacent paragraph);
  * ids and links (from the package manager).
Each split's analysis is cached by the SHA-1 of its bytes, so after an edit
only the changed split is re-analysed.
"""
import hashlib
import io
import os
import re
from dataclasses import dataclass, field

from core.qc import textnorm
from core.qc.pdf_model import image_hashes, thumbnail

BLOCK_NAMES = {"p", "div", "li", "dt", "dd", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th",
               "caption", "figcaption", "pre", "address", "section", "article", "aside", "header", "footer",
               "nav", "figure", "table", "tr", "ul", "ol", "dl", "body", "hr", "main"}
HEADING_NAMES = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_NAMES = {"script", "style", "head", "title"}
_CAPTION_RE = re.compile(r"^\s*([A-Za-zÀ-ɏ]{2,}\.?)\s*(\d+[A-Za-z]?([.\-:]\d+)*|[IVXLC]+)\b")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


@dataclass
class XWord:
    text: str
    split: str
    block: int                 # global block index
    start: int = 0             # char offset inside the block's text
    end: int = 0

    @property
    def key(self):
        return textnorm.key(self.text)


@dataclass
class XBlock:
    index: int
    split: str
    tag: str
    xpath: str
    sourceline: int = 0
    text: str = ""
    word_start: int = 0
    word_end: int = 0
    classes: str = ""

    @property
    def is_heading(self):
        return self.tag in HEADING_NAMES


@dataclass
class XMarker:
    split: str
    label: str
    id: str
    xpath: str
    sourceline: int = 0
    word_pos: int = 0          # global index of the next content word


@dataclass
class XImage:
    split: str
    src: str                   # as written
    file: str                  # package-relative path
    xpath: str
    sourceline: int = 0
    word_pos: int = 0
    alt: str = ""
    caption: str = ""
    caption_xpath: str = ""
    exists: bool = False
    sha1: str = ""
    ahash: int = 0
    dhash: int = 0
    width: int = 0
    height: int = 0
    thumb: str = ""

    @property
    def aspect(self):
        return (self.width / self.height) if self.height else 0.0

    @property
    def uid(self):
        return f"{self.split}:{self.xpath}"


@dataclass
class SplitData:
    path: str
    sha1: str
    blocks: list = field(default_factory=list)      # XBlock with split-local word/block indices
    words: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    images: list = field(default_factory=list)
    error: str = ""


_image_cache = {}


def package_image_info(mgr, rel):
    """(sha1, ahash, dhash, w, h, thumb) of a package image file (memoised by mtime)."""
    path = mgr.abspath(rel)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key in _image_cache:
        return _image_cache[key]
    try:
        from PIL import Image
        with open(path, "rb") as f:
            data = f.read()
        if rel.lower().endswith(".svg"):
            info = (hashlib.sha1(data).hexdigest(), 0, 0, 0, 0, "")
        else:
            im = Image.open(io.BytesIO(data))
            ah, dh = image_hashes(im)
            info = (hashlib.sha1(data).hexdigest(), ah, dh, im.width, im.height, thumbnail(im))
    except Exception:
        info = None
    _image_cache[key] = info
    return info


def analyse_split(mgr, rel: str) -> SplitData:
    from core.epub_structure.xhtml_parser import epub_type
    try:
        with open(mgr.abspath(rel), "rb") as f:
            raw = f.read()
    except OSError as e:
        return SplitData(path=rel, sha1="", error=str(e))
    sd = SplitData(path=rel, sha1=hashlib.sha1(raw).hexdigest())
    try:
        tree = mgr.doc(rel)
    except Exception as e:  # noqa: BLE001
        sd.error = str(e)
        return sd
    root = tree.getroot()
    body = next((el for el in root.iter() if _local(el.tag) == "body"), None)
    if body is None:
        sd.error = "no <body>"
        return sd

    current = {"block": None}
    block_text = {}

    def open_block(el):
        blk = XBlock(index=len(sd.blocks), split=rel, tag=_local(el.tag), xpath=tree.getpath(el),
                     sourceline=el.sourceline or 0, classes=el.get("class") or "")
        sd.blocks.append(blk)
        block_text[blk.index] = []
        return blk

    def add_text(text):
        if not text or not text.strip():
            if text and current["block"] is not None:
                block_text[current["block"].index].append(text)
            return
        blk = current["block"]
        parts = block_text[blk.index]
        base = sum(len(p) for p in parts)
        for m in re.finditer(r"\S+", text):
            sd.words.append(XWord(text=m.group(0), split=rel, block=blk.index,
                                  start=base + m.start(), end=base + m.end()))
        parts.append(text)

    def walk(el, in_marker=False):
        name = _local(el.tag)
        if not isinstance(el.tag, str) or name in SKIP_NAMES or el.get("hidden") is not None:
            return
        is_marker = epub_type(el) == "pagebreak" or (el.get("role") or "") == "doc-pagebreak"
        if is_marker:
            label = el.get("aria-label") or el.get("title") or " ".join("".join(el.itertext()).split())
            sd.markers.append(XMarker(split=rel, label=label.strip(), id=el.get("id") or "",
                                      xpath=tree.getpath(el), sourceline=el.sourceline or 0,
                                      word_pos=len(sd.words)))
            return  # marker text is not content (its tail is handled by the parent)
        if name in ("img", "image"):
            src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
            target, _ = mgr.resolve(rel, src)
            img = XImage(split=rel, src=src, file=target or "", xpath=tree.getpath(el),
                         sourceline=el.sourceline or 0, word_pos=len(sd.words), alt=el.get("alt") or "")
            img.exists = bool(target) and mgr.exists(target)
            info = package_image_info(mgr, target) if img.exists else None
            if info:
                img.sha1, img.ahash, img.dhash, img.width, img.height, img.thumb = info
            img.caption, img.caption_xpath = _caption_of(tree, el)
            sd.images.append(img)
            return
        saved = current["block"]
        if name in BLOCK_NAMES:
            current["block"] = open_block(el)
        add_text(el.text)
        for child in el:
            walk(child)
            add_text(child.tail)
        current["block"] = saved

    current["block"] = open_block(body)
    walk(body)
    for blk in sd.blocks:
        blk.text = "".join(block_text.get(blk.index, []))
    # word ranges per block (local indices)
    for i, w in enumerate(sd.words):
        blk = sd.blocks[w.block]
        if blk.word_end == 0 and blk.word_start == 0 and (i == 0 or sd.words[i - 1].block != w.block):
            blk.word_start = i
        blk.word_end = i + 1
    return sd


def _caption_of(tree, img_el):
    """Caption of an image: figcaption/caption of its figure-like ancestor,
    else a labelled paragraph right after/before the image's block."""
    node = img_el.getparent()
    for _ in range(4):
        if node is None:
            break
        for c in node.iter():
            if _local(c.tag) in ("figcaption", "caption") and c is not img_el:
                return " ".join("".join(c.itertext()).split()), tree.getpath(c)
        if _local(node.tag) in ("figure", "table", "div", "section", "body"):
            break
        node = node.getparent()
    block = img_el.getparent()
    while block is not None and _local(block.tag) not in BLOCK_NAMES:
        block = block.getparent()
    if block is None:
        return "", ""
    for sib in (block.getnext(), block.getprevious()):
        if sib is not None and isinstance(sib.tag, str):
            text = " ".join("".join(sib.itertext()).split())
            if text and _CAPTION_RE.match(text) and len(text) < 400:
                return text, tree.getpath(sib)
    return "", ""


class XhtmlModel:
    """Spine-ordered, global view over all splits (rebuilt cheaply from the
    per-split cache)."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.cache = {}            # rel -> SplitData
        self.splits = []
        self.words, self.blocks, self.markers, self.images = [], [], [], []
        self.split_ranges = {}     # rel -> (word_start, word_end, block_start, block_end)

    def build(self, progress=None, changed=None):
        """Re-analyses only splits whose bytes changed (or `changed`)."""
        paths = self.mgr.split_paths()
        n = len(paths)
        for i, rel in enumerate(paths):
            try:
                with open(self.mgr.abspath(rel), "rb") as f:
                    sha = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                sha = ""
            cached = self.cache.get(rel)
            if cached is None or cached.sha1 != sha or (changed and rel in changed):
                self.cache[rel] = analyse_split(self.mgr, rel)
            if progress:
                progress("XHTML splits", i + 1, n)
        for rel in list(self.cache):
            if rel not in paths:
                del self.cache[rel]
        self.splits = paths
        self.words, self.blocks, self.markers, self.images = [], [], [], []
        self.split_ranges = {}
        for rel in paths:
            sd = self.cache[rel]
            w0, b0 = len(self.words), len(self.blocks)
            for blk in sd.blocks:
                gb = XBlock(index=b0 + blk.index, split=rel, tag=blk.tag, xpath=blk.xpath,
                            sourceline=blk.sourceline, text=blk.text, word_start=w0 + blk.word_start,
                            word_end=w0 + blk.word_end, classes=blk.classes)
                self.blocks.append(gb)
            for w in sd.words:
                self.words.append(XWord(text=w.text, split=rel, block=b0 + w.block, start=w.start, end=w.end))
            for m in sd.markers:
                self.markers.append(XMarker(split=rel, label=m.label, id=m.id, xpath=m.xpath,
                                            sourceline=m.sourceline, word_pos=w0 + m.word_pos))
            for im in sd.images:
                gi = XImage(**{**im.__dict__})
                gi.word_pos = w0 + im.word_pos
                self.images.append(gi)
            self.split_ranges[rel] = (w0, len(self.words), b0, len(self.blocks))
        return self

    def split_of_word(self, idx):
        if 0 <= idx < len(self.words):
            return self.words[idx].split
        return self.splits[-1] if self.splits else None

    def block_of_word(self, idx):
        if 0 <= idx < len(self.words):
            return self.blocks[self.words[idx].block]
        return None
