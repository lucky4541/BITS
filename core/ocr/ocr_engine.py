"""Abstract OCR provider interface.

Keeps the rest of the pipeline decoupled from a specific OCR library.
PaddleOCREngine is the primary/default implementation; other engines can
implement the same interface.

This version adds OCR_TRACE diagnostics around engine registration and
singleton creation/reuse. The diagnostics are intentionally outside the
OCR implementation so they can prove whether a PaddleOCREngine constructor
is reached before a recognition call.
"""

import importlib
import sys
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# ZONETOOL OCR ENGINE ACTIVE MARKER
# ---------------------------------------------------------------------------
# This file writes a small marker independently of ZoneTool debug logging.
try:
    from pathlib import Path as _OCR_ENGINE_PATH
    _OCR_ENGINE_MARKER = _OCR_ENGINE_PATH(__file__).resolve().parents[1] / "OCR_ENGINE_ACTIVE.txt"
    _OCR_ENGINE_MARKER.write_text(
        "ACTIVE OCR ENGINE FILE:\n" + str(_OCR_ENGINE_PATH(__file__).resolve()) + "\n",
        encoding="utf-8",
    )
except Exception:
    pass



class OCREngineError(Exception):
    """Raised for OCR setup/environment failures that the caller should show
    to the user rather than allowing an unhandled exception in the GUI."""


class OCREngine(ABC):
    name = "base"

    @property
    @abstractmethod
    def version(self) -> str:
        """Installed engine/model version string."""
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap availability/import check; must not construct the model."""
        raise NotImplementedError

    def test_initialization(self) -> tuple:
        """Explicit heavy initialization test."""
        ok = self.is_available()
        return ok, (
            "Ready (import check only - not yet initialized)"
            if ok
            else f"{self.name} is not installed in this Python environment."
        )

    @abstractmethod
    def recognize(self, image, language: str = "en", options: dict = None):
        """Recognize text from an image and return OCRResult."""
        raise NotImplementedError


_REGISTRY = {}

# Built-in engines are imported lazily. This avoids importing PaddleOCR merely
# because core.ocr.ocr_engine itself was imported.
_BUILTIN_ENGINE_MODULES = ("core.ocr.paddle_engine",)
_builtin_engines_loaded = False


def _trace(message: str):
    """Write OCR_TRACE without making logging itself capable of breaking OCR."""
    try:
        from core import debug_log
        debug_log.log("OCR_TRACE", message)
    except Exception:
        # Logging must never change OCR behavior.
        pass


def _ensure_builtin_engines_registered():
    global _builtin_engines_loaded

    _trace(
        f"_ensure_builtin_engines_registered ENTER "
        f"loaded={_builtin_engines_loaded!r} "
        f"registry={list(_REGISTRY.keys())!r}"
    )

    if _builtin_engines_loaded:
        _trace(
            "_ensure_builtin_engines_registered SKIP "
            f"already_loaded registry={list(_REGISTRY.keys())!r}"
        )
        return

    _builtin_engines_loaded = True

    for module_name in _BUILTIN_ENGINE_MODULES:
        if module_name in sys.modules:
            _trace(
                f"builtin module already imported: {module_name!r}; "
                "registration should already exist"
            )
            continue

        _trace(f"IMPORTING builtin engine module: {module_name!r}")

        try:
            importlib.import_module(module_name)

            _trace(
                f"builtin module import SUCCESS: {module_name!r}; "
                f"registry={list(_REGISTRY.keys())!r}"
            )
        except ImportError as e:
            _trace(
                f"builtin module ImportError: {module_name!r}: "
                f"{type(e).__name__}: {e}"
            )
            # Missing optional engine dependency simply means it does not
            # register. Other engines remain unaffected.
            pass
        except Exception as e:
            _trace(
                f"builtin module IMPORT FAILED: {module_name!r}: "
                f"{type(e).__name__}: {e}"
            )
            raise


def register(engine_cls):
    """Register an OCR engine class."""
    _REGISTRY[engine_cls.name] = engine_cls

    _trace(
        f"ENGINE REGISTERED name={engine_cls.name!r} "
        f"class={engine_cls.__module__}.{engine_cls.__name__} "
        f"registry={list(_REGISTRY.keys())!r}"
    )

    return engine_cls


def available_engines() -> list:
    """Return names of registered built-in OCR engines."""
    _trace("available_engines ENTER")

    _ensure_builtin_engines_registered()

    result = list(_REGISTRY.keys())

    _trace(f"available_engines RETURN {result!r}")

    return result


_INSTANCES = {}


def get_engine(name: str) -> OCREngine:
    """Return the singleton OCR engine instance for ``name``.

    The singleton is important because PaddleOCREngine itself caches the
    constructed PaddleOCR pipeline. Recreating the wrapper for every page
    would discard that model cache and cause unnecessary initialization.
    """
    _trace(
        f"get_engine ENTER name={name!r} "
        f"registry={list(_REGISTRY.keys())!r} "
        f"instances={list(_INSTANCES.keys())!r}"
    )

    _ensure_builtin_engines_registered()

    _trace(
        f"get_engine AFTER registration name={name!r} "
        f"registry={list(_REGISTRY.keys())!r} "
        f"instances={list(_INSTANCES.keys())!r}"
    )

    if name not in _REGISTRY:
        _trace(
            f"get_engine UNKNOWN ENGINE name={name!r} "
            f"available={list(_REGISTRY.keys())!r}"
        )

        raise OCREngineError(
            f"Unknown OCR engine: {name!r}. Available: {available_engines()}"
        )

    if name not in _INSTANCES:
        engine_cls = _REGISTRY[name]

        _trace(
            f"get_engine CREATING INSTANCE name={name!r} "
            f"class={engine_cls.__module__}.{engine_cls.__name__}"
        )

        try:
            instance = engine_cls()

            _trace(
                f"get_engine INSTANCE CONSTRUCTOR RETURNED name={name!r} "
                f"type={type(instance).__module__}.{type(instance).__name__}"
            )

            _INSTANCES[name] = instance

            _trace(
                f"get_engine INSTANCE STORED name={name!r} "
                f"instances={list(_INSTANCES.keys())!r}"
            )

        except OCREngineError as e:
            _trace(
                f"get_engine CONSTRUCTOR OCREngineError name={name!r}: {e}"
            )
            raise

        except Exception as e:
            _trace(
                f"get_engine CONSTRUCTOR EXCEPTION name={name!r}: "
                f"{type(e).__name__}: {e}"
            )
            raise

    else:
        instance = _INSTANCES[name]

        _trace(
            f"get_engine REUSING INSTANCE name={name!r} "
            f"type={type(instance).__module__}.{type(instance).__name__}"
        )

    _trace(
        f"get_engine RETURN name={name!r} "
        f"type={type(_INSTANCES[name]).__module__}."
        f"{type(_INSTANCES[name]).__name__}"
    )

    return _INSTANCES[name]
