"""On-disk OCR result cache (spec section 38) - if a page was already
OCR'd with the same engine/version/language/settings, don't run it again.
Cache key includes everything spec 38 lists; a bump to any of them (a
PaddleOCR upgrade, a different language, changed preprocessing settings)
naturally produces a different key rather than silently returning a stale
result for the new configuration.

Layout: cache/<pdf_identity>/page_<NNN>_<engine>_<lang>.json (spec's own
"cache/pdf_hash/page_001.json" example, with the engine/language folded
into the filename since one PDF can legitimately be OCR'd with more than
one engine/language over its lifetime and both results are worth keeping)."""
import hashlib
import json
import os

from core.ocr.result_model import OCRResult


def pdf_identity(pdf_path: str) -> str:
    """A fast, good-enough PDF identity for cache partitioning - hashes
    the absolute path + file size + modification time, NOT the full file
    content (reading a 200MB scanned book just to compute a cache key
    would defeat the point of caching). Changes automatically if the file
    is replaced/edited (mtime or size differs), giving correct
    invalidation without ever reading the PDF's bytes. No PDF-identity
    concept existed anywhere in ZoneTool before this - confirmed by a
    full-codebase search - so this is the first one, not a duplicate."""
    try:
        stat = os.stat(pdf_path)
        raw = f"{os.path.abspath(pdf_path)}|{stat.st_size}|{stat.st_mtime}"
    except OSError:
        raw = os.path.abspath(pdf_path)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _settings_fingerprint(settings: dict) -> str:
    canonical = json.dumps(settings or {}, sort_keys=True)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]


class OCRCache:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir

    def _page_path(self, pdf_path: str, page_number: int, engine: str, engine_version: str,
                    language: str, preprocessing_settings: dict = None) -> str:
        doc_dir = os.path.join(self.cache_dir, pdf_identity(pdf_path))
        fp = _settings_fingerprint({
            "engine": engine, "engine_version": engine_version, "language": language,
            "preprocessing": preprocessing_settings or {},
        })
        return os.path.join(doc_dir, f"page_{page_number:04d}_{fp}.json")

    def get(self, pdf_path: str, page_number: int, engine: str, engine_version: str,
            language: str, preprocessing_settings: dict = None) -> OCRResult:
        """Returns the cached OCRResult, or None if nothing cached for this
        EXACT (pdf, page, engine, version, language, settings) combination."""
        path = self._page_path(pdf_path, page_number, engine, engine_version, language, preprocessing_settings)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return OCRResult.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError, KeyError):
            return None  # a corrupted cache entry is treated as a cache miss, never a crash

    def get_latest_for_page(self, pdf_path: str, page_number: int, engine: str,
                            language: str, preprocessing_settings: dict = None) -> OCRResult:
        """Find a usable cached OCR result without loading the OCR engine.

        This is intentionally used by zoning/generation paths where the OCR
        model must NEVER be initialized just to read an existing cache entry.
        The stored JSON is checked for engine/language/preprocessing identity;
        its stored engine_version is accepted because the cache filename may
        contain a version that is not known without importing the engine.
        """
        doc_dir = os.path.join(self.cache_dir, pdf_identity(pdf_path))
        if not os.path.isdir(doc_dir):
            return None
        wanted_pre = preprocessing_settings or {}
        prefix = f"page_{int(page_number):04d}_"
        try:
            names = sorted(n for n in os.listdir(doc_dir)
                           if n.startswith(prefix) and n.lower().endswith(".json"))
        except OSError:
            return None
        for name in reversed(names):
            path = os.path.join(doc_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if str(data.get("engine", "")) != str(engine):
                    continue
                if str(data.get("language", "")) != str(language):
                    continue
                stored_pre = data.get("preprocessing") or {}
                if stored_pre != wanted_pre:
                    continue
                result = OCRResult.from_dict(data)
                if result.blocks:
                    return result
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return None

    def put(self, pdf_path: str, page_number: int, result: OCRResult, preprocessing_settings: dict = None):
        path = self._page_path(pdf_path, page_number, result.engine, result.engine_version,
                                result.language, preprocessing_settings)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)

    def clear(self, pdf_path: str = None):
        """Clears the whole cache, or just one PDF's entries if pdf_path
        is given."""
        import shutil
        if pdf_path is None:
            if os.path.isdir(self.cache_dir):
                shutil.rmtree(self.cache_dir)
            return
        doc_dir = os.path.join(self.cache_dir, pdf_identity(pdf_path))
        if os.path.isdir(doc_dir):
            shutil.rmtree(doc_dir)
