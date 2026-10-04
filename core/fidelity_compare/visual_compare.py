"""Secondary, offline perceptual-similarity helpers (spec section 21's
"perceptual hash" and section 51's "visual comparison only as a secondary
verification layer... do not use pixel comparison as the primary content
comparison"). A minimal average-hash (aHash) implemented directly with
PIL + numpy (already required project dependencies - see
packaging/dependency_report.txt) rather than adding a new third-party
imagehash dependency for one small algorithm."""
import io

import numpy as np
from PIL import Image

HASH_SIZE = 8


def average_hash(image_bytes: bytes) -> int:
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((HASH_SIZE, HASH_SIZE), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float64)
    mean = arr.mean()
    bits = (arr > mean).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming_distance(hash_a: int, hash_b: int) -> int:
    return bin(hash_a ^ hash_b).count("1")


def similarity_from_hashes(hash_a: int, hash_b: int) -> float:
    max_bits = HASH_SIZE * HASH_SIZE
    return 1.0 - (hamming_distance(hash_a, hash_b) / max_bits)


def compare_images(image_bytes_a: bytes, image_bytes_b: bytes) -> float:
    """Returns a 0-1 similarity score. Never the sole basis for a
    figure-changed verdict (see figure_compare.py, which combines this
    with dimensions/aspect-ratio/caption/position)."""
    try:
        return similarity_from_hashes(average_hash(image_bytes_a), average_hash(image_bytes_b))
    except Exception:
        return 0.0


def compare_page_regions(image_a, image_b, bbox_a, bbox_b) -> float:
    """Crops the same relative region from two already-rendered PIL page
    images and compares them - used by highlight_engine.py's own
    secondary-verification pass over ambiguous layout findings, never as
    a primary content check (spec section 51)."""
    try:
        crop_a = image_a.crop((bbox_a.x0, bbox_a.y0, bbox_a.x1, bbox_a.y1))
        crop_b = image_b.crop((bbox_b.x0, bbox_b.y0, bbox_b.x1, bbox_b.y1))
        buf_a, buf_b = io.BytesIO(), io.BytesIO()
        crop_a.convert("RGB").save(buf_a, format="PNG")
        crop_b.convert("RGB").save(buf_b, format="PNG")
        return compare_images(buf_a.getvalue(), buf_b.getvalue())
    except Exception:
        return 0.0
