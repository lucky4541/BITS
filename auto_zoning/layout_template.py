"""Data containers + shared feature-vector schema for the auto-zoning
reference/template representation. Built once from one or more loaded
reference projects (reference_analyzer.py), then read-only for the whole
Auto Zone run (zone_matcher.py/tag_predictor.py). Pure data/math - no PDF,
GUI, or zone_manager coupling.

Every zone (reference OR new-PDF candidate) is reduced to the SAME small
feature dict via compute_features() below, so scoring always compares like
for like regardless of which side of the reference/candidate divide a
sample came from."""
import math
from collections import Counter
from dataclasses import dataclass, field


def compute_features(bbox, page_width, page_height, column_bounds, text, font_size, bold, italic,
                      body_font_size, list_marker=None) -> dict:
    """bbox: (x0,y0,x1,y1) in PDF points. page_width/page_height: that
    zone's own page's size (so the resulting x/y/w/h are comparable across
    documents of different page sizes - the spec's "normalized coordinates"
    requirement). column_bounds: (left_x, right_x) of whichever column/band
    this zone's bbox was assigned to (see column_detector.py) - indent is
    measured relative to THAT, not the raw page edge, so a list-item's
    extra indent inside a multi-column layout is still meaningful.
    body_font_size: that PAGE's own dominant/body font size
    (pdf_block_detector.page_body_font_size) - font_size is expressed as a
    RATIO to it, so two documents using different absolute body sizes still
    compare correctly."""
    x0, y0, x1, y1 = bbox
    w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
    col_x0, col_x1 = column_bounds
    col_w = max(1.0, col_x1 - col_x0)
    text = text or ""
    letters = [c for c in text if c.isalpha()]
    all_caps = bool(letters) and all(c.isupper() for c in letters)
    return {
        "x": (x0 / page_width) if page_width else 0.0,
        "y": (y0 / page_height) if page_height else 0.0,
        "w": (w / page_width) if page_width else 0.0,
        "h": (h / page_height) if page_height else 0.0,
        "indent": (x0 - col_x0) / col_w,
        "font_ratio": (font_size / body_font_size) if (font_size and body_font_size) else None,
        "bold": bool(bold),
        "italic": bool(italic),
        "line_count": (text.count("\n") + 1) if text else 0,
        "text_len": len(text),
        "all_caps": all_caps,
        "list_marker": list_marker,
    }


NUMERIC_KEYS = ("x", "y", "w", "h", "indent", "font_ratio", "text_len", "line_count")


@dataclass
class TagProfile:
    tag: str
    samples: list = field(default_factory=list)          # list[dict] - see compute_features
    parent_tags: Counter = field(default_factory=Counter)
    list_types: Counter = field(default_factory=Counter)

    @property
    def count(self) -> int:
        return len(self.samples)

    def stat(self, key):
        """(mean, stddev) for numeric feature `key` across every sample that
        has a non-None value for it, or (None, None) if there are none."""
        vals = [s[key] for s in self.samples if s.get(key) is not None]
        if not vals:
            return None, None
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, math.sqrt(var)

    def bool_ratio(self, key) -> float:
        vals = [s[key] for s in self.samples if key in s]
        return (sum(1 for v in vals if v) / len(vals)) if vals else 0.0


@dataclass
class LayoutTemplate:
    tag_profiles: dict = field(default_factory=dict)     # tag -> TagProfile
    source_names: list = field(default_factory=list)

    def add_sample(self, tag, features, parent_tag=None, list_type=None):
        prof = self.tag_profiles.setdefault(tag, TagProfile(tag=tag))
        prof.samples.append(features)
        if parent_tag:
            prof.parent_tags[parent_tag] += 1
        if list_type:
            prof.list_types[list_type] += 1


def merge_templates(templates: list) -> LayoutTemplate:
    """Combines multiple references' profiles by literally pooling their
    samples per tag - more references agreeing on a tag's typical
    geometry/font simply means more samples feeding the same mean/stddev
    (TagProfile.stat), which naturally tightens (or, if they disagree,
    widens) the resulting similarity scores - exactly the spec's "multiple
    references reinforce confidence" behavior, without any special-case
    averaging code or coordinate copying."""
    merged = LayoutTemplate()
    for t in templates:
        merged.source_names.extend(t.source_names)
        for tag, prof in t.tag_profiles.items():
            dest = merged.tag_profiles.setdefault(tag, TagProfile(tag=tag))
            dest.samples.extend(prof.samples)
            dest.parent_tags.update(prof.parent_tags)
            dest.list_types.update(prof.list_types)
    return merged
