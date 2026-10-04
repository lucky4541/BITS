"""PaddleOCR adapter - isolated CPU OCR worker.

ZoneTool runs PaddleOCR in a dedicated child process. This is intentional:
Paddle/PaddleX/PaddleOCR contains native C++ execution code and can terminate
the host process when a native runtime failure occurs on Windows. Keeping the
model and inference in a separate process prevents a native OCR failure from
taking down the Tkinter application.

Runtime:
    PaddleOCR 3.7.0
    PaddlePaddle 3.1.0
"""

import atexit
import multiprocessing as mp
import os
import sys
import time
import traceback
from pathlib import Path

# CPU runtime safeguards MUST exist before importing Paddle/PaddleOCR.
os.environ["FLAGS_enable_pir_api"] = "0"
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"

# Marker: proves the exact file loaded by ZoneTool.
try:
    _PADDLE_ENGINE_MARKER = Path(__file__).resolve().parents[1] / "PADDLE_ENGINE_ACTIVE.txt"
    _PADDLE_ENGINE_MARKER.write_text(
        "ACTIVE PADDLE ENGINE FILE:\n" + str(Path(__file__).resolve()) + "\n",
        encoding="utf-8",
    )
except Exception:
    pass

_OCR_DEBUG_LOG = Path(__file__).resolve().parents[1] / "ocr_debug.log"

# FAST CPU OCR input limit.
# The GUI renders pages at the configured DPI (normally 300). Sending the full
# 300-DPI page to the medium OCR models is unnecessarily expensive on CPU.
# Downscaling only the OCR input reduces inference time while the returned OCR
# coordinates are scaled back to the original rendered-image coordinates.
# 2200 px keeps book text readable while substantially reducing CPU work.
_OCR_MAX_INPUT_SIDE = 2200


def _ocr_debug(message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with _OCR_DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
            fh.flush()
    except Exception:
        pass


_ocr_debug("=" * 90)
_ocr_debug("ZoneTool PaddleOCR isolated-process engine imported")
_ocr_debug(f"Python executable: {sys.executable}")
_ocr_debug(f"Engine file: {Path(__file__).resolve()}")
_ocr_debug(f"FLAGS_enable_pir_api={os.environ.get('FLAGS_enable_pir_api')!r}")
_ocr_debug(f"FLAGS_use_mkldnn={os.environ.get('FLAGS_use_mkldnn')!r}")
_ocr_debug(
    "PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT="
    f"{os.environ.get('PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT')!r}"
)

from core.ocr.ocr_engine import OCREngine, OCREngineError, register
from core.ocr.result_model import OCRBlock, OCRResult

try:
    from core import debug_log
except Exception:
    debug_log = None

from core.ocr import coordinate_mapper


_OCR_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}


def _normalize_ocr_text(text: str) -> str:
    if not text:
        return text
    for bad, good in _OCR_LIGATURES.items():
        text = text.replace(bad, good)
    return text


def _worker_parse_prediction(results):
    """Convert PaddleOCR 3.x prediction objects to pickle-safe records."""
    blocks = []
    for res in results or []:
        data = (
            res
            if isinstance(res, dict)
            else getattr(res, "json", None)
            or getattr(res, "res", None)
            or res
        )
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        if not isinstance(data, dict):
            continue

        texts = data.get("rec_texts") or data.get("texts") or []
        scores = data.get("rec_scores") or data.get("scores") or [1.0] * len(texts)
        polys = data.get("rec_polys") or data.get("dt_polys") or data.get("boxes") or []

        for text, score, poly in zip(texts, scores, polys):
            try:
                poly_list = poly.tolist() if hasattr(poly, "tolist") else poly
            except Exception:
                poly_list = poly
            blocks.append(
                {
                    "text": str(text),
                    "confidence": float(score),
                    "polygon": poly_list,
                }
            )
    return blocks


def _paddle_worker(request_queue, response_queue, language, model_kwargs):
    """Persistent PaddleOCR worker. No Tkinter/UI code runs in this process."""
    # Re-assert before Paddle imports in the child.
    os.environ["FLAGS_enable_pir_api"] = "0"
    os.environ["FLAGS_use_mkldnn"] = "0"
    os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"

    try:
        _ocr_debug("WORKER START - FAST OCR MODE")
        import paddle
        import paddleocr
        _ocr_debug(
            f"WORKER RUNTIME PaddlePaddle={getattr(paddle, '__version__', 'unknown')} "
            f"PaddleOCR={getattr(paddleocr, '__version__', 'unknown')}"
        )

        from paddleocr import PaddleOCR

        kwargs = dict(model_kwargs)
        kwargs.update(
            {
                "lang": language,
                # FAST CPU OCR: the document-orientation, unwarping and
                # textline-orientation models are disabled. They are not
                # required for normal book-page OCR and add substantial CPU
                # time. Detection + recognition remain enabled.
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "enable_mkldnn": False,
                "device": "cpu",
                "engine": "paddle",
            }
        )
        _ocr_debug(
            "FAST OCR CONFIG: doc_orientation=False, unwarping=False, "
            "textline_orientation=False, max_input_side="
            f"{_OCR_MAX_INPUT_SIDE}"
        )

        _ocr_debug(f"WORKER CONSTRUCTOR kwargs keys={sorted(kwargs.keys())}")
        construct_started = time.time()
        ocr = PaddleOCR(**kwargs)
        _ocr_debug(f"WORKER CONSTRUCTOR SUCCESS elapsed={time.time()-construct_started:.3f}s")
        response_queue.put(("ready", None))

    except BaseException as exc:
        _ocr_debug(f"WORKER INITIALIZATION FAILED: {type(exc).__name__}: {exc}")
        _ocr_debug(traceback.format_exc())
        try:
            response_queue.put(("init_error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
        return

    while True:
        try:
            request = request_queue.get()
            if request is None:
                _ocr_debug("WORKER STOP")
                return

            arr = request
            _ocr_debug(
                f"WORKER PREDICT START shape={getattr(arr, 'shape', None)} "
                f"dtype={getattr(arr, 'dtype', None)}"
            )

            predict_started = time.time()
            results = ocr.predict(arr)
            predict_elapsed = time.time() - predict_started
            records = _worker_parse_prediction(results)
            _ocr_debug(f"WORKER PREDICT SUCCESS blocks={len(records)} elapsed={predict_elapsed:.3f}s")
            response_queue.put(("result", records))

        except BaseException as exc:
            _ocr_debug(f"WORKER PREDICT FAILED: {type(exc).__name__}: {exc}")
            _ocr_debug(traceback.format_exc())
            try:
                response_queue.put(("error", f"{type(exc).__name__}: {exc}"))
            except Exception:
                return


@register
class PaddleOCREngine(OCREngine):
    name = "PaddleOCR"

    _MODEL_DIR_PARAMS = {
        "doc_orientation_classify_model_dir": "PP-LCNet_x1_0_doc_ori",
        "doc_unwarping_model_dir": "UVDoc",
        "textline_orientation_model_dir": "PP-LCNet_x1_0_textline_ori",
        "text_detection_model_dir": "PP-OCRv6_medium_det",
        "text_recognition_model_dir": "PP-OCRv6_medium_rec",
    }

    def __init__(self):
        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._lang = None
        self._worker_lock = None
        try:
            import threading
            self._worker_lock = threading.RLock()
        except Exception:
            self._worker_lock = None

    @property
    def version(self) -> str:
        try:
            import paddleocr
            return getattr(paddleocr, "__version__", "unknown")
        except ImportError:
            return "not installed"

    def is_available(self) -> bool:
        try:
            import paddle
            import paddleocr
        except ImportError:
            return False
        return True

    def test_initialization(self) -> tuple:
        if not self.is_available():
            return False, (
                "PaddleOCR is not installed in this Python environment. "
                "Install PaddleOCR 3.7.0 and PaddlePaddle 3.1.0."
            )
        try:
            self._ensure_worker("en")
            return True, f"PaddleOCR {self.version} initialized successfully in isolated OCR worker."
        except OCREngineError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Could not initialize PaddleOCR: {e}"

    def _bundled_model_dir_kwargs(self) -> dict:
        from core.resource_path import resource_path

        kwargs = {}
        for param, folder in self._MODEL_DIR_PARAMS.items():
            path = resource_path("ocr_models", folder)
            if os.path.isdir(path):
                kwargs[param] = path
        return kwargs

    def _check_runtime_compatibility(self):
        try:
            import paddle
            import paddleocr
            pv = str(getattr(paddle, "__version__", "unknown"))
            ov = str(getattr(paddleocr, "__version__", "unknown"))
            _ocr_debug(f"Runtime check: PaddlePaddle={pv}")
            _ocr_debug(f"Runtime check: PaddleOCR={ov}")
            if ov.startswith("3.7") and pv.startswith("3.3"):
                raise OCREngineError(
                    f"Incompatible OCR runtime: PaddleOCR {ov} + PaddlePaddle {pv}. "
                    "Use PaddlePaddle 3.1.0."
                )
        except ImportError as e:
            raise OCREngineError(
                "PaddleOCR/PaddlePaddle is not installed. "
                "Install PaddleOCR 3.7.0 + PaddlePaddle 3.1.0."
            ) from e

    def _ensure_worker(self, language: str):
        self._check_runtime_compatibility()

        lock = self._worker_lock
        if lock is None:
            return self._ensure_worker_unlocked(language)

        with lock:
            return self._ensure_worker_unlocked(language)

    def _ensure_worker_unlocked(self, language: str):
        if (
            self._process is not None
            and self._process.is_alive()
            and self._lang == language
        ):
            return

        self._stop_worker()

        self._request_queue = None
        self._response_queue = None

        # Spawn is explicit on Windows and keeps native Paddle code outside
        # the Tkinter process.
        ctx = mp.get_context("spawn")
        self._request_queue = ctx.Queue(maxsize=1)
        self._response_queue = ctx.Queue(maxsize=1)

        model_kwargs = self._bundled_model_dir_kwargs()
        self._process = ctx.Process(
            target=_paddle_worker,
            args=(self._request_queue, self._response_queue, language, model_kwargs),
            name="ZoneTool-PaddleOCR-Worker",
            daemon=False,
        )

        _ocr_debug("STARTING isolated PaddleOCR worker process")
        self._process.start()

        try:
            kind, payload = self._response_queue.get(timeout=180)
        except Exception as exc:
            code = self._process.exitcode
            self._stop_worker()
            raise OCREngineError(
                f"PaddleOCR worker did not initialize within 180 seconds "
                f"(process exit code={code}). Check ocr_debug.log."
            ) from exc

        if kind != "ready":
            code = self._process.exitcode
            self._stop_worker()
            raise OCREngineError(
                f"PaddleOCR worker initialization failed (exit code={code}): {payload}"
            )

        self._lang = language
        _ocr_debug("isolated PaddleOCR worker READY")

    def _stop_worker(self):
        proc = self._process
        rq = self._request_queue

        try:
            if rq is not None and proc is not None and proc.is_alive():
                rq.put_nowait(None)
        except Exception:
            pass

        try:
            if proc is not None and proc.is_alive():
                proc.join(timeout=5)
        except Exception:
            pass

        try:
            if proc is not None and proc.is_alive():
                proc.terminate()
                proc.join(timeout=3)
        except Exception:
            pass

        for q in (self._request_queue, self._response_queue):
            try:
                if q is not None:
                    q.close()
                    q.join_thread()
            except Exception:
                pass

        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._lang = None

    def close(self):
        self._stop_worker()

    def __del__(self):
        try:
            self._stop_worker()
        except Exception:
            pass

    def recognize(self, image, language: str = "en", options: dict = None) -> OCRResult:
        options = options or {}
        started = time.time()
        w, h = image.size

        _ocr_debug(
            f"RECOGNIZE START: language={language!r}, image_size=({w}x{h}), "
            f"image_type={type(image).__name__}"
        )

        try:
            import numpy as np
            arr = np.ascontiguousarray(np.array(image.convert("RGB"), dtype=np.uint8))
        except Exception as e:
            raise OCREngineError(f"Could not prepare image for PaddleOCR: {e}") from e

        lock = self._worker_lock
        if lock is None:
            return self._recognize_unlocked(arr, w, h, language, started)

        with lock:
            return self._recognize_unlocked(arr, w, h, language, started)

    @staticmethod
    def _prepare_fast_input(arr):
        """Downscale only the OCR input when a rendered page is very large.

        Returns (ocr_array, scale_x, scale_y).  OCR coordinates are in the
        resized image coordinate system and must be divided by these scales
        before they are converted back to PDF coordinates.
        """
        try:
            height, width = int(arr.shape[0]), int(arr.shape[1])
        except Exception:
            return arr, 1.0, 1.0

        max_side = max(width, height)
        if max_side <= _OCR_MAX_INPUT_SIDE:
            return arr, 1.0, 1.0

        scale = float(_OCR_MAX_INPUT_SIDE) / float(max_side)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))

        try:
            import numpy as np
            from PIL import Image
            resized = Image.fromarray(arr, mode="RGB").resize(
                (new_w, new_h), Image.Resampling.LANCZOS
            )
            out = np.ascontiguousarray(np.asarray(resized, dtype=np.uint8))
        except Exception as exc:
            _ocr_debug(f"FAST INPUT RESIZE FAILED; using original image: {exc}")
            return arr, 1.0, 1.0

        scale_x = float(new_w) / float(width)
        scale_y = float(new_h) / float(height)
        _ocr_debug(
            f"FAST INPUT RESIZE: original=({width}x{height}) "
            f"ocr=({new_w}x{new_h}) scale=({scale_x:.5f},{scale_y:.5f})"
        )
        return out, scale_x, scale_y

    @staticmethod
    def _scale_polygon_back(polygon, scale_x, scale_y):
        if not polygon or (scale_x == 1.0 and scale_y == 1.0):
            return polygon
        try:
            return [
                [float(point[0]) / scale_x, float(point[1]) / scale_y]
                for point in polygon
            ]
        except Exception:
            return polygon

    def _recognize_unlocked(self, arr, w, h, language, started):
        try:
            self._ensure_worker_unlocked(language)
            ocr_arr, scale_x, scale_y = self._prepare_fast_input(arr)
            _ocr_debug(
                f"Sending OCR image to worker shape={ocr_arr.shape} dtype={ocr_arr.dtype} "
                f"(original={arr.shape})"
            )
            self._request_queue.put(ocr_arr, timeout=30)

            kind, payload = self._response_queue.get(timeout=300)

            if kind == "error":
                raise OCREngineError(f"PaddleOCR worker inference failed: {payload}")

            if kind != "result":
                raise OCREngineError(f"Unexpected PaddleOCR worker response: {kind}")

            blocks = []
            for rec in payload or []:
                polygon = self._scale_polygon_back(
                    rec.get("polygon", []), scale_x, scale_y
                )
                bbox = coordinate_mapper.polygon_to_bbox(polygon)
                blocks.append(
                    OCRBlock(
                        text=_normalize_ocr_text(str(rec.get("text", ""))),
                        bbox=bbox,
                        confidence=float(rec.get("confidence", 0.0)),
                    )
                )

            text = "\n".join(b.text for b in blocks)
            confidence = (
                sum(b.confidence for b in blocks) / len(blocks)
                if blocks else 0.0
            )

            _ocr_debug(
                f"RECOGNIZE SUCCESS: blocks={len(blocks)}, text_chars={len(text)}, "
                f"confidence={confidence:.4f}, elapsed={time.time()-started:.3f}s"
            )

            if debug_log is not None and debug_log.is_enabled():
                debug_log.log("PADDLE_RAW", f"recognize: parsed {len(blocks)} blocks")

            return OCRResult(
                page_number=0,
                image_width=w,
                image_height=h,
                dpi=0,
                text=text,
                blocks=blocks,
                confidence=confidence,
                engine=self.name,
                engine_version=self.version,
                language=language,
                processing_time=time.time() - started,
            )

        except OCREngineError:
            raise
        except Exception as e:
            _ocr_debug(f"RECOGNIZE FAILED: {type(e).__name__}: {e}")
            _ocr_debug(traceback.format_exc())
            # A dead native worker must never kill the GUI.
            if self._process is not None and not self._process.is_alive():
                _ocr_debug(f"OCR worker exited unexpectedly: exitcode={self._process.exitcode}")
                self._stop_worker()
            return OCRResult.empty(
                page_number=0,
                image_width=w,
                image_height=h,
                dpi=0,
                engine=self.name,
                engine_version=self.version,
                language=language,
                error=f"PaddleOCR recognition failed: {e}",
            )
