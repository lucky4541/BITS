"""Bounded in-memory page-image/text cache (spec section 65: "use...
page cache... image cache... do not load all page images into memory").
A simple size-capped LRU (stdlib OrderedDict) - the whole point is
capping memory for 1000+ page books, so this deliberately never grows
unbounded regardless of how many pages a comparison run touches."""
from collections import OrderedDict


class LRUCache:
    def __init__(self, max_items: int = 40):
        self.max_items = max_items
        self._store = OrderedDict()

    def get(self, key):
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key, value):
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self.max_items:
            self._store.popitem(last=False)

    def clear(self):
        self._store.clear()


class PageImageCache:
    """Caches rendered page images per (source_path, page_number, dpi) -
    used by highlight_engine.py / the synchronized viewer so scrolling
    back to an already-rendered page never re-rasterizes the PDF."""
    def __init__(self, max_items: int = 24):
        self._images = LRUCache(max_items)

    def get_or_render(self, pdf_document, page_number: int, dpi: int):
        key = (pdf_document.path, page_number, dpi)
        cached = self._images.get(key)
        if cached is not None:
            return cached
        img = pdf_document.render_page_image(page_number, dpi=dpi)
        self._images.put(key, img)
        return img


class TextCache:
    """Caches a Document's already-built pages/blocks by page number so
    repeated GUI lookups (difference-panel selection, side-by-side
    navigation) never re-run token/word alignment."""
    def __init__(self):
        self._by_page = {}

    def build(self, document):
        self._by_page = {page.number: page for page in document.pages}

    def page(self, number: int):
        return self._by_page.get(number)


# ---------------------------------------------------------------------------
# Persistent semantic-document cache
# ---------------------------------------------------------------------------
# Parsing a 300+ page PDF is much more expensive than reading an already-built
# Document model. Keep this cache outside the application source tree and
# invalidate it whenever the source file's size or modification timestamp
# changes. This is deliberately limited to the read-only fidelity subsystem.
import hashlib
import os
import pickle
import tempfile
from pathlib import Path

_CACHE_VERSION = 2

def _persistent_cache_dir() -> Path:
    override = os.environ.get("EPUBFORGE_FIDELITY_CACHE")
    if override:
        root = Path(override)
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EPUBForge" / "fidelity_compare_cache"
    else:
        root = Path.home() / ".cache" / "epubforge" / "fidelity_compare"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_key(path: str, source_kind: str) -> str:
    st = os.stat(path)
    identity = f"{Path(path).resolve()}|{st.st_size}|{st.st_mtime_ns}|{source_kind}|v{_CACHE_VERSION}"
    return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()


def load_persistent_document(path: str, source_kind: str):
    try:
        cache_path = _persistent_cache_dir() / f"{_cache_key(path, source_kind)}.pkl"
        with cache_path.open("rb") as fh:
            obj = pickle.load(fh)
        if isinstance(obj, dict) and obj.get("version") == _CACHE_VERSION:
            return obj.get("document")
    except Exception:
        return None
    return None


def save_persistent_document(path: str, source_kind: str, document) -> None:
    try:
        cache_dir = _persistent_cache_dir()
        cache_path = cache_dir / f"{_cache_key(path, source_kind)}.pkl"
        tmp = Path(tempfile.mkstemp(prefix="fidelity_", suffix=".tmp", dir=cache_dir)[1])
        try:
            with tmp.open("wb") as fh:
                pickle.dump({"version": _CACHE_VERSION, "document": document}, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache_path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        prune_persistent_cache()
    except Exception:
        # Cache failure must never fail a comparison.
        return


def prune_persistent_cache(max_files: int = 80) -> None:
    try:
        files = sorted(_persistent_cache_dir().glob("*.pkl"), key=lambda x: x.stat().st_mtime_ns, reverse=True)
        for old in files[max_files:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
