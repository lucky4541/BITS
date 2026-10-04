# """
# Adaptive native-PDF formatting detection.

# Detects:
#     bold
#     italic
#     superscript
#     subscript
#     small caps
#     underline
#     uppercase

# The detector uses PDF flags as primary signal with geometry fallback.
# NO font name detection to avoid false positives.
# """

# import math
# import re
# from collections import Counter
# from typing import Optional, Tuple, Dict, Any

# # ============================================================================
# # PDF FLAGS (PyMuPDF)
# # ============================================================================
# FLAG_SUPERSCRIPT = 1 << 0
# FLAG_ITALIC = 1 << 1
# FLAG_SERIFED = 1 << 2
# FLAG_MONOSPACED = 1 << 3
# FLAG_BOLD = 1 << 4

# # ============================================================================
# # GEOMETRY TOLERANCES - VERY CONSERVATIVE
# # ============================================================================
# SIZE_TOLERANCE = 0.08
# BASELINE_TOLERANCE = 0.08
# SMALL_CAPS_MAX_RATIO = 0.88

# # Superscript/subscript size ratio - EXTREMELY CONSERVATIVE
# # Only mark if significantly smaller (55% of body text)
# # This prevents publication years (usually 70-80% of body text) from being marked
# SUP_SUB_SIZE_RATIO = 0.55

# # Shear detection - CONSERVATIVE
# SHEAR_RATIO_TOLERANCE = math.tan(math.radians(8.0))  # 8 degrees minimum
# MIN_SHEAR_SAMPLES = 4  # Need strong evidence


# # ============================================================================
# # SHEAR DETECTION
# # ============================================================================

# def _char_shear_ratio(ch: dict) -> Optional[float]:
#     """Calculate shear ratio for a single character."""
#     bbox = ch.get("bbox")
#     origin = ch.get("origin")
#     if not bbox or not origin or len(bbox) < 4 or len(origin) < 2:
#         return None
#     try:
#         ox, oy = float(origin[0]), float(origin[1])
#         bx0, by0 = float(bbox[0]), float(bbox[1])
#     except (TypeError, ValueError):
#         return None
#     dy = oy - by0
#     if dy <= 1:
#         return None
#     return (ox - bx0) / dy


# def _median(values: list) -> float:
#     """Calculate median of values."""
#     values = sorted(values)
#     return values[len(values) // 2]


# def line_shear_stats(line: dict) -> Optional[float]:
#     """Establish THIS line's own normal glyph-shear reference."""
#     ratios = [
#         r for r in (
#             _char_shear_ratio(ch)
#             for span in line.get("spans", [])
#             for ch in span.get("chars", [])
#         )
#         if r is not None
#     ]
#     if len(ratios) < MIN_SHEAR_SAMPLES:
#         return None
#     return _median(ratios)


# def span_shear_ratio(chars: list) -> Optional[float]:
#     """Calculate median shear ratio for a span."""
#     ratios = [r for r in (_char_shear_ratio(ch) for ch in chars) if r is not None]
#     if len(ratios) < MIN_SHEAR_SAMPLES:
#         return None
#     return _median(ratios)


# # ============================================================================
# # BOLD/ITALIC DETECTION - PDF FLAGS ONLY
# # ============================================================================

# def detect_bold_italic(
#     flags: int = 0,
#     font_name: str = "",
#     shear_ratio: Optional[float] = None,
#     dominant_shear_ratio: Optional[float] = None,
# ) -> Tuple[bool, bool]:
#     """Detect bold/italic without inspecting font names.

#     ``font_name`` is intentionally ignored and retained only for API
#     compatibility. Native PDF flags are used when present; italic also has a
#     geometry fallback for PDFs that do not expose the italic flag.
#     """
#     try:
#         flags = int(flags or 0)
#     except (TypeError, ValueError):
#         flags = 0

#     bold = bool(flags & FLAG_BOLD)
#     italic = bool(flags & FLAG_ITALIC)

#     if not italic and shear_ratio is not None:
#         absolute_italic = abs(float(shear_ratio)) >= SHEAR_RATIO_TOLERANCE
#         relative_italic = (
#             dominant_shear_ratio is not None
#             and abs(float(shear_ratio) - float(dominant_shear_ratio))
#             >= SHEAR_RATIO_TOLERANCE
#         )
#         if absolute_italic or relative_italic:
#             italic = True

#     return bold, italic


# # ============================================================================
# # LINE STATS
# # ============================================================================

# def _span_text(span: dict) -> str:
#     """Get text from a span."""
#     chars = span.get("chars", [])
#     if chars:
#         return "".join(ch.get("c", "") for ch in chars)
#     return span.get("text", "") or ""


# def _span_is_real_text(span: dict) -> bool:
#     """Check if span contains real text (not just whitespace)."""
#     text = _span_text(span)
#     return bool(text.strip())


# def _span_size(span: dict) -> float:
#     """Get font size from span."""
#     try:
#         return float(span.get("size", 0) or 0)
#     except (TypeError, ValueError):
#         return 0.0


# def _span_baseline(span: dict) -> float:
#     """Get baseline from span."""
#     origin = span.get("origin")
#     if origin and len(origin) >= 2:
#         try:
#             return float(origin[1])
#         except (TypeError, ValueError):
#             pass
    
#     bbox = span.get("bbox")
#     if bbox and len(bbox) >= 4:
#         try:
#             return float(bbox[3])
#         except (TypeError, ValueError):
#             pass
    
#     return 0.0


# def line_baseline_stats(line: dict) -> Tuple[float, float]:
#     """
#     Determine the normal font size and baseline from the actual line.
    
#     Uses character count weighting so a short special-format span
#     does not dominate the line reference.
#     """
#     spans = [
#         span
#         for span in line.get("spans", [])
#         if _span_is_real_text(span)
#     ]

#     if not spans:
#         return 0.0, 0.0

#     # Count weighted sizes
#     size_counts = {}
#     for span in spans:
#         size = round(_span_size(span), 2)
#         if size <= 0:
#             continue
        
#         text = _span_text(span)
#         weight = sum(not ch.isspace() for ch in text)
#         if weight <= 0:
#             weight = 1
        
#         size_counts[size] = size_counts.get(size, 0) + weight

#     if not size_counts:
#         return 0.0, 0.0

#     # Dominant size = most common size by character count
#     dominant_size = max(size_counts, key=size_counts.get)

#     # Calculate dominant baseline from spans of dominant size
#     baseline_values = []
#     for span in spans:
#         if round(_span_size(span), 2) != dominant_size:
#             continue
        
#         baseline = _span_baseline(span)
#         if baseline:
#             text = _span_text(span)
#             weight = max(1, sum(not ch.isspace() for ch in text))
#             baseline_values.extend([baseline] * weight)

#     if baseline_values:
#         dominant_baseline = sum(baseline_values) / len(baseline_values)
#     else:
#         dominant_baseline = _span_baseline(spans[0])

#     return dominant_size, dominant_baseline


# # ============================================================================
# # POSITION DETECTION - VERY CONSERVATIVE
# # ============================================================================

# def _relative_size(size: float, dominant_size: float) -> float:
#     """Calculate relative size ratio."""
#     if size <= 0 or dominant_size <= 0:
#         return 1.0
#     return size / dominant_size


# def _baseline_difference(origin_y: float, dominant_baseline: float) -> float:
#     """Calculate baseline difference."""
#     return origin_y - dominant_baseline


# def classify_position(
#     size: float,
#     origin_y: float,
#     dominant_size: float,
#     dominant_baseline: float,
#     superscript_flag: bool = False,
#     allow_size_only_fallback: bool = False,
# ) -> str:
#     """
#     Detect superscript/subscript using VERY conservative rules.
    
#     CRITICAL: A slightly smaller font (like publication years, page numbers)
#     should NEVER be marked as superscript unless it's clearly raised AND
#     significantly smaller (<= 55% of body text).
#     """
#     if dominant_size <= 0:
#         return "sup" if superscript_flag else "normal"

#     if size <= 0:
#         return "normal"

#     ratio = _relative_size(size, dominant_size)
#     shift = _baseline_difference(origin_y, dominant_baseline)

#     # Baseline threshold - require a VERY significant shift
#     # Publications years might be slightly raised, but not enough for superscript
#     baseline_threshold = max(1.0, dominant_size * BASELINE_TOLERANCE * 2.0)

#     # ========================================================================
#     # Native PDF superscript flag - trusted unconditionally
#     # ========================================================================
#     if superscript_flag:
#         return "sup"

#     # ========================================================================
#     # Geometric evidence - EXTREMELY CONSERVATIVE
#     # Requires BOTH:
#     # 1. Size <= 55% of body text (not just slightly smaller)
#     # 2. Position shifted by at least baseline_threshold
#     # ========================================================================
#     if ratio <= SUP_SUB_SIZE_RATIO:
#         if shift < -baseline_threshold:
#             return "sup"
#         if shift > baseline_threshold:
#             return "sub"

#     # ========================================================================
#     # Marker fallback - ONLY for explicit footnote markers
#     # ========================================================================
#     if allow_size_only_fallback and ratio < 0.45:
#         return "sup"

#     return "normal"


# # ============================================================================
# # SMALL CAPS DETECTION
# # ============================================================================

# def is_small_caps(
#     c: str,
#     size: float,
#     dominant_size: float,
#     pos: str,
# ) -> bool:
#     """
#     Detect small caps from the actual PDF size relationship.
    
#     Only uppercase alphabetic characters qualify.
#     """
#     if pos != "normal":
#         return False

#     if dominant_size <= 0 or size <= 0:
#         return False

#     if not c:
#         return False

#     # Only uppercase alphabetic characters can be small caps
#     if not c.isalpha():
#         return False
    
#     if not c.isupper():
#         return False

#     ratio = size / dominant_size
#     return ratio <= SMALL_CAPS_MAX_RATIO



# # ============================================================================
# # UNDERLINE DETECTION
# # ============================================================================

# def _rect_value(obj, key, default=0.0):
#     try:
#         return float(obj.get(key, default) or default)
#     except (TypeError, ValueError, AttributeError):
#         return default


# def detect_underline_chars(page, line: dict, chars: list) -> list:
#     """Return one boolean per character using PDF drawing geometry.

#     Underlines are normally vector drawing/line objects rather than a text
#     flag. This checks drawings close below the character baseline and only
#     marks characters whose horizontal center lies on an underline segment.
#     It never changes the extracted character.
#     """
#     result = [False] * len(chars)
#     if page is None or not chars:
#         return result

#     try:
#         drawings = page.get_drawings()
#     except Exception:
#         return result

#     # Character baseline and horizontal coverage.
#     for d in drawings:
#         try:
#             items = d.get("items", [])
#             rect = d.get("rect")
#             if not rect or len(rect) < 4:
#                 continue

#             x0, y0, x1, y1 = map(float, rect[:4])
#             height = abs(y1 - y0)

#             # An underline is a very thin horizontal vector.
#             horizontal = (x1 - x0) > 3.0 and height <= 2.0
#             if not horizontal:
#                 # Some PDFs expose the line as an item rather than a useful
#                 # aggregate rect.
#                 horizontal = False
#                 for item in items:
#                     if not item:
#                         continue
#                     op = item[0]
#                     if op == "l" and len(item) >= 3:
#                         p0, p1 = item[1], item[2]
#                         try:
#                             ix0, iy0 = float(p0.x), float(p0.y)
#                             ix1, iy1 = float(p1.x), float(p1.y)
#                         except Exception:
#                             continue
#                         if abs(iy1 - iy0) <= 2.0 and abs(ix1 - ix0) > 3.0:
#                             x0, x1 = sorted((ix0, ix1))
#                             y0 = y1 = (iy0 + iy1) / 2.0
#                             horizontal = True
#                             break

#             if not horizontal:
#                 continue

#             # Find characters whose baseline is just above this line.
#             for i, ch in enumerate(chars):
#                 bbox = ch.get("bbox")
#                 origin = ch.get("origin")
#                 if not bbox or len(bbox) < 4:
#                     continue

#                 cx = (float(bbox[0]) + float(bbox[2])) / 2.0
#                 baseline = (
#                     float(origin[1])
#                     if origin and len(origin) >= 2
#                     else float(bbox[3])
#                 )

#                 if x0 - 0.75 <= cx <= x1 + 0.75:
#                     # Typical underline distance is small and positive in
#                     # PDF coordinates: underline below the text baseline.
#                     distance = y0 - baseline
#                     if -1.0 <= distance <= 4.5:
#                         result[i] = True
#         except Exception:
#             continue

#     return result


# def detect_uppercase(c: str) -> bool:
#     """True only when the actual extracted Unicode character is uppercase."""
#     return bool(c and c.isalpha() and c.isupper())


# def is_uppercase_character(c: str) -> bool:
#     """Return True only for an actual uppercase Unicode letter."""
#     return bool(c and c.isalpha() and c.isupper())


# def is_uppercase_word(text: str) -> bool:
#     """Return True for a real all-uppercase word, not a single initial."""
#     letters = [c for c in (text or "") if c.isalpha()]
#     return len(letters) >= 2 and all(c.isupper() for c in letters)

# # ============================================================================
# # MAIN DETECTOR
# # ============================================================================

# def detect_formatting(
#     flags: int = 0,
#     font_name: str = "",  # IGNORED - kept for API compatibility
#     size: float = 0.0,
#     origin_y: float = 0.0,
#     dominant_size: float = 0.0,
#     dominant_baseline: float = 0.0,
#     superscript_flag: bool = False,
#     allow_size_only_fallback: bool = False,
#     shear_ratio: Optional[float] = None,
#     dominant_shear_ratio: Optional[float] = None,
#     char: str = "",
#     underline: bool = False,
# ) -> Dict[str, Any]:
#     """
#     Unified formatting detector.
    
#     Uses PDF flags as primary signal with conservative geometry fallback.
#     font_name is IGNORED to avoid false positives.
#     """
#     bold, italic = detect_bold_italic(
#         flags,
#         font_name,  # Passed but ignored
#         shear_ratio=shear_ratio,
#         dominant_shear_ratio=dominant_shear_ratio,
#     )

#     pos = classify_position(
#         size=size,
#         origin_y=origin_y,
#         dominant_size=dominant_size,
#         dominant_baseline=dominant_baseline,
#         superscript_flag=(
#             superscript_flag
#             or bool(flags & FLAG_SUPERSCRIPT)
#         ),
#         allow_size_only_fallback=allow_size_only_fallback,
#     )

#     uppercase = detect_uppercase(char)
#     smallcaps = is_small_caps(char, size, dominant_size, pos)

#     return {
#         "bold": bold,
#         "italic": italic,
#         "underline": bool(underline),
#         "superscript": pos == "sup",
#         "subscript": pos == "sub",
#         "smallcaps": smallcaps,
#         "uppercase": uppercase,
#         "position": pos,
#     }


# # ============================================================================
# # DEBUGGING UTILITY
# # ============================================================================

# def debug_detection(
#     flags: int = 0,
#     font_name: str = "",
#     size: float = 0.0,
#     origin_y: float = 0.0,
#     dominant_size: float = 0.0,
#     dominant_baseline: float = 0.0,
#     superscript_flag: bool = False,
#     char: str = "",
# ) -> Dict[str, Any]:
#     """
#     Debug function to understand why a character is being classified.
#     """
#     if dominant_size <= 0:
#         return {"status": "No dominant size", "result": "normal"}
    
#     ratio = size / dominant_size if dominant_size > 0 else 1.0
#     shift = origin_y - dominant_baseline
#     baseline_threshold = max(1.0, dominant_size * BASELINE_TOLERANCE * 2.0)
    
#     result = classify_position(
#         size, origin_y, dominant_size, dominant_baseline,
#         superscript_flag
#     )
    
#     bold = bool(flags & FLAG_BOLD)
#     italic = bool(flags & FLAG_ITALIC)
    
#     return {
#         "size": size,
#         "uppercase": is_uppercase_character(char),
#         "smallcaps": is_small_caps(char, size, dominant_size, result),
#         "underline": False,
#         "dominant_size": dominant_size,
#         "ratio": ratio,
#         "ratio_threshold": SUP_SUB_SIZE_RATIO,
#         "shift": shift,
#         "baseline_threshold": baseline_threshold,
#         "superscript_flag": superscript_flag,
#         "bold_flag": bold,
#         "italic_flag": italic,
#         "font_name": font_name,
#         "result": result,
#         "is_sup": result == "sup",
#         "is_sub": result == "sub",
#         "reason": (
#             "PDF superscript flag" if superscript_flag
#             else f"Size ratio {ratio:.2f} <= {SUP_SUB_SIZE_RATIO} and shift {shift:.2f} < -{baseline_threshold:.2f}" if result == "sup"
#             else f"Size ratio {ratio:.2f} <= {SUP_SUB_SIZE_RATIO} and shift {shift:.2f} > {baseline_threshold:.2f}" if result == "sub"
#             else "Normal text"
#         )
#     }


# # ============================================================================
# # ADVANCED: PER-PAGE ADAPTIVE DETECTION
# # ============================================================================

# class AdaptiveFormattingDetector:
#     """
#     Adaptive detector that can adjust thresholds per page/document.
#     Useful for PDFs with inconsistent formatting.
#     """
    
#     def __init__(self):
#         self.page_stats = {}
#         self.current_page = None
    
#     def analyze_page(self, page) -> Dict[str, float]:
#         """Analyze a page to determine adaptive thresholds."""
#         raw = page.get_text("rawdict")
        
#         all_sizes = []
#         all_baselines = []
        
#         for block in raw.get("blocks", []):
#             if block.get("type") != 0:
#                 continue
#             for line in block.get("lines", []):
#                 size, baseline = line_baseline_stats(line)
#                 if size > 0:
#                     all_sizes.append(size)
#                     all_baselines.append(baseline)
        
#         if not all_sizes:
#             return {}
        
#         # Calculate page statistics
#         avg_size = sum(all_sizes) / len(all_sizes)
#         size_std = math.sqrt(sum((s - avg_size) ** 2 for s in all_sizes) / len(all_sizes))
        
#         return {
#             "avg_size": avg_size,
#             "size_std": size_std,
#             "sup_sub_ratio": min(0.65, max(0.45, avg_size / (avg_size + size_std * 2))),
#         }
    
#     def detect_formatting_adaptive(
#         self,
#         flags: int = 0,
#         font_name: str = "",
#         size: float = 0.0,
#         origin_y: float = 0.0,
#         dominant_size: float = 0.0,
#         dominant_baseline: float = 0.0,
#         superscript_flag: bool = False,
#         allow_size_only_fallback: bool = False,
#         shear_ratio: Optional[float] = None,
#         dominant_shear_ratio: Optional[float] = None,
#         page=None,
#     ) -> Dict[str, Any]:
#         """Adaptive detection using page-specific thresholds."""
        
#         # Use standard detection first
#         result = detect_formatting(
#             flags=flags,
#             font_name=font_name,
#             size=size,
#             origin_y=origin_y,
#             dominant_size=dominant_size,
#             dominant_baseline=dominant_baseline,
#             superscript_flag=superscript_flag,
#             allow_size_only_fallback=allow_size_only_fallback,
#             shear_ratio=shear_ratio,
#             dominant_shear_ratio=dominant_shear_ratio,
#         )
        
#         # If page is provided, adapt thresholds
#         if page and not any([result['bold'], result['italic'], result['superscript'], result['subscript']]):
#             stats = self.analyze_page(page)
#             if stats:
#                 # Use adaptive ratio for superscript detection
#                 global SUP_SUB_SIZE_RATIO
#                 original_ratio = SUP_SUB_SIZE_RATIO
#                 SUP_SUB_SIZE_RATIO = stats["sup_sub_ratio"]
                
#                 # Re-run detection with adaptive threshold
#                 pos = classify_position(
#                     size=size,
#                     origin_y=origin_y,
#                     dominant_size=dominant_size,
#                     dominant_baseline=dominant_baseline,
#                     superscript_flag=bool(flags & FLAG_SUPERSCRIPT),
#                     allow_size_only_fallback=allow_size_only_fallback,
#                 )
#                 result['superscript'] = pos == "sup"
#                 result['subscript'] = pos == "sub"
#                 result['position'] = pos
                
#                 # Restore global
#                 SUP_SUB_SIZE_RATIO = original_ratio
        
#         return result

"""
Adaptive native-PDF formatting detection.

Detects:
    bold
    italic
    superscript
    subscript
    small caps
    underline
    uppercase

The detector uses PDF flags as primary signal with geometry fallback.
NO font name detection to avoid false positives.
"""

import math
import re
from collections import Counter
from typing import Optional, Tuple, Dict, Any

# ============================================================================
# PDF FLAGS (PyMuPDF)
# ============================================================================
FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC = 1 << 1
FLAG_SERIFED = 1 << 2
FLAG_MONOSPACED = 1 << 3
FLAG_BOLD = 1 << 4

# ============================================================================
# GEOMETRY TOLERANCES - VERY CONSERVATIVE
# ============================================================================
SIZE_TOLERANCE = 0.08
BASELINE_TOLERANCE = 0.08
SMALL_CAPS_MAX_RATIO = 0.88

# Superscript/subscript size ratio - EXTREMELY CONSERVATIVE
# Only mark if significantly smaller (55% of body text)
# This prevents publication years (usually 70-80% of body text) from being marked
SUP_SUB_SIZE_RATIO = 0.55

# Shear detection - CONSERVATIVE
SHEAR_RATIO_TOLERANCE = math.tan(math.radians(8.0))  # 8 degrees minimum
MIN_SHEAR_SAMPLES = 4  # Need strong evidence


# ============================================================================
# SHEAR DETECTION
# ============================================================================

def _char_shear_ratio(ch: dict) -> Optional[float]:
    """Calculate shear ratio for a single character."""
    bbox = ch.get("bbox")
    origin = ch.get("origin")
    if not bbox or not origin or len(bbox) < 4 or len(origin) < 2:
        return None
    try:
        ox, oy = float(origin[0]), float(origin[1])
        bx0, by0 = float(bbox[0]), float(bbox[1])
    except (TypeError, ValueError):
        return None
    dy = oy - by0
    if dy <= 1:
        return None
    return (ox - bx0) / dy


def _median(values: list) -> float:
    """Calculate median of values."""
    values = sorted(values)
    return values[len(values) // 2]


def line_shear_stats(line: dict) -> Optional[float]:
    """Establish THIS line's own normal glyph-shear reference."""
    ratios = [
        r for r in (
            _char_shear_ratio(ch)
            for span in line.get("spans", [])
            for ch in span.get("chars", [])
        )
        if r is not None
    ]
    if len(ratios) < MIN_SHEAR_SAMPLES:
        return None
    return _median(ratios)


def span_shear_ratio(chars: list) -> Optional[float]:
    """Calculate median shear ratio for a span."""
    ratios = [r for r in (_char_shear_ratio(ch) for ch in chars) if r is not None]
    if len(ratios) < MIN_SHEAR_SAMPLES:
        return None
    return _median(ratios)


# ============================================================================
# BOLD/ITALIC DETECTION - PDF FLAGS ONLY
# ============================================================================

def detect_bold_italic(
    flags: int = 0,
    font_name: str = "",
    shear_ratio: Optional[float] = None,
    dominant_shear_ratio: Optional[float] = None,
) -> Tuple[bool, bool]:
    """Detect bold/italic without inspecting font names.

    ``font_name`` is intentionally ignored and retained only for API
    compatibility. Native PDF flags are used when present; italic also has a
    geometry fallback for PDFs that do not expose the italic flag.
    """
    try:
        flags = int(flags or 0)
    except (TypeError, ValueError):
        flags = 0

    bold = bool(flags & FLAG_BOLD)
    italic = bool(flags & FLAG_ITALIC)

    if not italic and shear_ratio is not None:
        absolute_italic = abs(float(shear_ratio)) >= SHEAR_RATIO_TOLERANCE
        relative_italic = (
            dominant_shear_ratio is not None
            and abs(float(shear_ratio) - float(dominant_shear_ratio))
            >= SHEAR_RATIO_TOLERANCE
        )
        if absolute_italic or relative_italic:
            italic = True

    return bold, italic


# ============================================================================
# LINE STATS
# ============================================================================

def _span_text(span: dict) -> str:
    """Get text from a span."""
    chars = span.get("chars", [])
    if chars:
        return "".join(ch.get("c", "") for ch in chars)
    return span.get("text", "") or ""


def _span_is_real_text(span: dict) -> bool:
    """Check if span contains real text (not just whitespace)."""
    text = _span_text(span)
    return bool(text.strip())


def _span_size(span: dict) -> float:
    """Get font size from span."""
    try:
        return float(span.get("size", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _span_baseline(span: dict) -> float:
    """Get baseline from span."""
    origin = span.get("origin")
    if origin and len(origin) >= 2:
        try:
            return float(origin[1])
        except (TypeError, ValueError):
            pass
    
    bbox = span.get("bbox")
    if bbox and len(bbox) >= 4:
        try:
            return float(bbox[3])
        except (TypeError, ValueError):
            pass
    
    return 0.0


def line_baseline_stats(line: dict) -> Tuple[float, float]:
    """
    Determine the normal font size and baseline from the actual line.
    
    Uses character count weighting so a short special-format span
    does not dominate the line reference.
    """
    spans = [
        span
        for span in line.get("spans", [])
        if _span_is_real_text(span)
    ]

    if not spans:
        return 0.0, 0.0

    # Count weighted sizes
    size_counts = {}
    for span in spans:
        size = round(_span_size(span), 2)
        if size <= 0:
            continue
        
        text = _span_text(span)
        weight = sum(not ch.isspace() for ch in text)
        if weight <= 0:
            weight = 1
        
        size_counts[size] = size_counts.get(size, 0) + weight

    if not size_counts:
        return 0.0, 0.0

    # Dominant size = most common size by character count
    dominant_size = max(size_counts, key=size_counts.get)

    # Calculate dominant baseline from spans of dominant size
    baseline_values = []
    for span in spans:
        if round(_span_size(span), 2) != dominant_size:
            continue
        
        baseline = _span_baseline(span)
        if baseline:
            text = _span_text(span)
            weight = max(1, sum(not ch.isspace() for ch in text))
            baseline_values.extend([baseline] * weight)

    if baseline_values:
        dominant_baseline = sum(baseline_values) / len(baseline_values)
    else:
        dominant_baseline = _span_baseline(spans[0])

    return dominant_size, dominant_baseline


# ============================================================================
# POSITION DETECTION - VERY CONSERVATIVE
# ============================================================================

def _relative_size(size: float, dominant_size: float) -> float:
    """Calculate relative size ratio."""
    if size <= 0 or dominant_size <= 0:
        return 1.0
    return size / dominant_size


def _baseline_difference(origin_y: float, dominant_baseline: float) -> float:
    """Calculate baseline difference."""
    return origin_y - dominant_baseline


def classify_position(
    size: float,
    origin_y: float,
    dominant_size: float,
    dominant_baseline: float,
    superscript_flag: bool = False,
    allow_size_only_fallback: bool = False,
    prev_char: str = "",
    next_char: str = "",
) -> str:
    """
    Conservative superscript/subscript detection.

    Native superscript flags remain authoritative.  Geometric subscript
    detection is deliberately restricted so ordinary smaller/lower numbers
    (years, chapter numbers, references) are not converted to <sub>.
    """
    if dominant_size <= 0:
        return "sup" if superscript_flag else "normal"
    if size <= 0:
        return "normal"

    ratio = _relative_size(size, dominant_size)
    shift = _baseline_difference(origin_y, dominant_baseline)

    # Stronger than the old 8% baseline tolerance.  Normal PDF glyph metrics
    # often differ by a few points even when the text is not subscript.
    baseline_threshold = max(1.0, dominant_size * BASELINE_TOLERANCE * 2.0)

    if superscript_flag:
        return "sup"

    if ratio <= SUP_SUB_SIZE_RATIO:
        # Superscript can still be recognized from clearly raised geometry.
        if shift < -baseline_threshold:
            return "sup"

        # PyMuPDF exposes a superscript flag but no reliable native subscript
        # flag.  Therefore <sub> requires both strong geometric evidence and
        # local context tying the small glyph to an alphanumeric/math token.
        # This blocks: "chapter 3", "(1941)", "(1987)", "reference 12", etc.
        strict_sub_threshold = max(2.0, dominant_size * 0.20)
        has_sub_context = (
            any(ch.isalpha() for ch in (prev_char or ""))
            or any(ch.isalpha() for ch in (next_char or ""))
            or "_" in (prev_char or "")
            or "_" in (next_char or "")
        )
        if shift > strict_sub_threshold and has_sub_context:
            return "sub"

    if allow_size_only_fallback and ratio < 0.45:
        return "sup"

    return "normal"


# ============================================================================
# SMALL CAPS DETECTION
# ============================================================================

def is_small_caps(
    c: str,
    size: float,
    dominant_size: float,
    pos: str,
) -> bool:
    """
    Detect small caps from the actual PDF size relationship.
    
    Only uppercase alphabetic characters qualify.
    """
    if pos != "normal":
        return False

    if dominant_size <= 0 or size <= 0:
        return False

    if not c:
        return False

    # Only uppercase alphabetic characters can be small caps
    if not c.isalpha():
        return False
    
    if not c.isupper():
        return False

    ratio = size / dominant_size
    return ratio <= SMALL_CAPS_MAX_RATIO



# ============================================================================
# UNDERLINE DETECTION
# ============================================================================

def _rect_value(obj, key, default=0.0):
    try:
        return float(obj.get(key, default) or default)
    except (TypeError, ValueError, AttributeError):
        return default


def detect_underline_chars(page, line: dict, chars: list) -> list:
    """Return one boolean per character using PDF drawing geometry.

    Underlines are normally vector drawing/line objects rather than a text
    flag. This checks drawings close below the character baseline and only
    marks characters whose horizontal center lies on an underline segment.
    It never changes the extracted character.
    """
    result = [False] * len(chars)
    if page is None or not chars:
        return result

    try:
        drawings = page.get_drawings()
    except Exception:
        return result

    # Character baseline and horizontal coverage.
    for d in drawings:
        try:
            items = d.get("items", [])
            rect = d.get("rect")
            if not rect or len(rect) < 4:
                continue

            x0, y0, x1, y1 = map(float, rect[:4])
            height = abs(y1 - y0)

            # An underline is a very thin horizontal vector.
            horizontal = (x1 - x0) > 3.0 and height <= 2.0
            if not horizontal:
                # Some PDFs expose the line as an item rather than a useful
                # aggregate rect.
                horizontal = False
                for item in items:
                    if not item:
                        continue
                    op = item[0]
                    if op == "l" and len(item) >= 3:
                        p0, p1 = item[1], item[2]
                        try:
                            ix0, iy0 = float(p0.x), float(p0.y)
                            ix1, iy1 = float(p1.x), float(p1.y)
                        except Exception:
                            continue
                        if abs(iy1 - iy0) <= 2.0 and abs(ix1 - ix0) > 3.0:
                            x0, x1 = sorted((ix0, ix1))
                            y0 = y1 = (iy0 + iy1) / 2.0
                            horizontal = True
                            break

            if not horizontal:
                continue

            # Find characters whose baseline is just above this line.
            for i, ch in enumerate(chars):
                bbox = ch.get("bbox")
                origin = ch.get("origin")
                if not bbox or len(bbox) < 4:
                    continue

                cx = (float(bbox[0]) + float(bbox[2])) / 2.0
                baseline = (
                    float(origin[1])
                    if origin and len(origin) >= 2
                    else float(bbox[3])
                )

                if x0 - 0.75 <= cx <= x1 + 0.75:
                    # Typical underline distance is small and positive in
                    # PDF coordinates: underline below the text baseline.
                    distance = y0 - baseline
                    if -1.0 <= distance <= 4.5:
                        result[i] = True
        except Exception:
            continue

    return result


def detect_uppercase(c: str) -> bool:
    """True only when the actual extracted Unicode character is uppercase."""
    return bool(c and c.isalpha() and c.isupper())


def is_uppercase_character(c: str) -> bool:
    """Return True only for an actual uppercase Unicode letter."""
    return bool(c and c.isalpha() and c.isupper())


def is_uppercase_word(text: str) -> bool:
    """Return True for a real all-uppercase word, not a single initial."""
    letters = [c for c in (text or "") if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)

# ============================================================================
# MAIN DETECTOR
# ============================================================================

def detect_formatting(
    flags: int = 0,
    font_name: str = "",  # IGNORED - kept for API compatibility
    size: float = 0.0,
    origin_y: float = 0.0,
    dominant_size: float = 0.0,
    dominant_baseline: float = 0.0,
    superscript_flag: bool = False,
    allow_size_only_fallback: bool = False,
    shear_ratio: Optional[float] = None,
    dominant_shear_ratio: Optional[float] = None,
    char: str = "",
    underline: bool = False,
    prev_char: str = "",
    next_char: str = "",
) -> Dict[str, Any]:
    """
    Unified formatting detector.
    
    Uses PDF flags as primary signal with conservative geometry fallback.
    font_name is IGNORED to avoid false positives.
    """
    bold, italic = detect_bold_italic(
        flags,
        font_name,  # Passed but ignored
        shear_ratio=shear_ratio,
        dominant_shear_ratio=dominant_shear_ratio,
    )

    pos = classify_position(
        size=size,
        origin_y=origin_y,
        dominant_size=dominant_size,
        dominant_baseline=dominant_baseline,
        superscript_flag=(
            superscript_flag
            or bool(flags & FLAG_SUPERSCRIPT)
        ),
        allow_size_only_fallback=allow_size_only_fallback,
        prev_char=prev_char,
        next_char=next_char,
    )

    uppercase = detect_uppercase(char)
    smallcaps = is_small_caps(char, size, dominant_size, pos)

    return {
        "bold": bold,
        "italic": italic,
        "underline": bool(underline),
        "superscript": pos == "sup",
        "subscript": pos == "sub",
        "smallcaps": smallcaps,
        "uppercase": uppercase,
        "position": pos,
    }


# ============================================================================
# DEBUGGING UTILITY
# ============================================================================

def debug_detection(
    flags: int = 0,
    font_name: str = "",
    size: float = 0.0,
    origin_y: float = 0.0,
    dominant_size: float = 0.0,
    dominant_baseline: float = 0.0,
    superscript_flag: bool = False,
    char: str = "",
) -> Dict[str, Any]:
    """
    Debug function to understand why a character is being classified.
    """
    if dominant_size <= 0:
        return {"status": "No dominant size", "result": "normal"}
    
    ratio = size / dominant_size if dominant_size > 0 else 1.0
    shift = origin_y - dominant_baseline
    baseline_threshold = max(1.0, dominant_size * BASELINE_TOLERANCE * 2.0)
    
    result = classify_position(
        size, origin_y, dominant_size, dominant_baseline,
        superscript_flag
    )
    
    bold = bool(flags & FLAG_BOLD)
    italic = bool(flags & FLAG_ITALIC)
    
    return {
        "size": size,
        "uppercase": is_uppercase_character(char),
        "smallcaps": is_small_caps(char, size, dominant_size, result),
        "underline": False,
        "dominant_size": dominant_size,
        "ratio": ratio,
        "ratio_threshold": SUP_SUB_SIZE_RATIO,
        "shift": shift,
        "baseline_threshold": baseline_threshold,
        "superscript_flag": superscript_flag,
        "bold_flag": bold,
        "italic_flag": italic,
        "font_name": font_name,
        "result": result,
        "is_sup": result == "sup",
        "is_sub": result == "sub",
        "reason": (
            "PDF superscript flag" if superscript_flag
            else f"Size ratio {ratio:.2f} <= {SUP_SUB_SIZE_RATIO} and shift {shift:.2f} < -{baseline_threshold:.2f}" if result == "sup"
            else f"Size ratio {ratio:.2f} <= {SUP_SUB_SIZE_RATIO} and shift {shift:.2f} > {baseline_threshold:.2f}" if result == "sub"
            else "Normal text"
        )
    }


# ============================================================================
# ADVANCED: PER-PAGE ADAPTIVE DETECTION
# ============================================================================

class AdaptiveFormattingDetector:
    """
    Adaptive detector that can adjust thresholds per page/document.
    Useful for PDFs with inconsistent formatting.
    """
    
    def __init__(self):
        self.page_stats = {}
        self.current_page = None
    
    def analyze_page(self, page) -> Dict[str, float]:
        """Analyze a page to determine adaptive thresholds."""
        raw = page.get_text("rawdict")
        
        all_sizes = []
        all_baselines = []
        
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                size, baseline = line_baseline_stats(line)
                if size > 0:
                    all_sizes.append(size)
                    all_baselines.append(baseline)
        
        if not all_sizes:
            return {}
        
        # Calculate page statistics
        avg_size = sum(all_sizes) / len(all_sizes)
        size_std = math.sqrt(sum((s - avg_size) ** 2 for s in all_sizes) / len(all_sizes))
        
        return {
            "avg_size": avg_size,
            "size_std": size_std,
            "sup_sub_ratio": min(0.65, max(0.45, avg_size / (avg_size + size_std * 2))),
        }
    
    def detect_formatting_adaptive(
        self,
        flags: int = 0,
        font_name: str = "",
        size: float = 0.0,
        origin_y: float = 0.0,
        dominant_size: float = 0.0,
        dominant_baseline: float = 0.0,
        superscript_flag: bool = False,
        allow_size_only_fallback: bool = False,
        shear_ratio: Optional[float] = None,
        dominant_shear_ratio: Optional[float] = None,
        page=None,
    ) -> Dict[str, Any]:
        """Adaptive detection using page-specific thresholds."""
        
        # Use standard detection first
        result = detect_formatting(
            flags=flags,
            font_name=font_name,
            size=size,
            origin_y=origin_y,
            dominant_size=dominant_size,
            dominant_baseline=dominant_baseline,
            superscript_flag=superscript_flag,
            allow_size_only_fallback=allow_size_only_fallback,
            shear_ratio=shear_ratio,
            dominant_shear_ratio=dominant_shear_ratio,
        )
        
        # If page is provided, adapt thresholds
        if page and not any([result['bold'], result['italic'], result['superscript'], result['subscript']]):
            stats = self.analyze_page(page)
            if stats:
                # Use adaptive ratio for superscript detection
                global SUP_SUB_SIZE_RATIO
                original_ratio = SUP_SUB_SIZE_RATIO
                SUP_SUB_SIZE_RATIO = stats["sup_sub_ratio"]
                
                # Re-run detection with adaptive threshold
                pos = classify_position(
                    size=size,
                    origin_y=origin_y,
                    dominant_size=dominant_size,
                    dominant_baseline=dominant_baseline,
                    superscript_flag=bool(flags & FLAG_SUPERSCRIPT),
                    allow_size_only_fallback=allow_size_only_fallback,
                )
                result['superscript'] = pos == "sup"
                result['subscript'] = pos == "sub"
                result['position'] = pos
                
                # Restore global
                SUP_SUB_SIZE_RATIO = original_ratio
        
        return result