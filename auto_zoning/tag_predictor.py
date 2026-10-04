"""Chooses the single best tag (+ confidence 0-100) for a candidate,
combining zone_matcher's template-driven score with small built-in
fallback heuristics for tags the reference template has too few (or zero)
samples of - so the engine degrades gracefully instead of mis-tagging
everything as the template's single most common tag."""
from auto_zoning import zone_matcher

FALLBACK_BLEND = 0.4              # discount applied to a PURELY fallback-derived score (no template data at all)


def _fallback_scores(features: dict) -> dict:
    """Small, tag-set-aware heuristics used only to fill gaps in a sparse
    reference template (spec: "falls back to small built-in heuristics...
    degrade gracefully rather than mis-tagging everything as p") - never the
    primary decision path when the template has real data for a tag."""
    scores = {}
    if features.get("list_marker") is not None:
        scores["list-item"] = 0.9
    font_ratio = features.get("font_ratio")
    if features.get("y", 1.0) < 0.12 and font_ratio and font_ratio >= 1.3:
        scores["title"] = 0.8
    if features.get("bold") and font_ratio and font_ratio >= 1.15 and features.get("text_len", 999) < 120:
        scores["h2"] = 0.6
    if features.get("all_caps") and features.get("text_len", 999) < 80:
        scores["h3"] = 0.5
    scores.setdefault("p", 0.5)
    return scores


def predict_tag(features: dict, template) -> tuple:
    """Returns (tag, confidence 0..100). A tag the template has ANY samples
    for is judged purely on zone_matcher's template score - the fallback
    heuristic only ever fills in a tag the template has NO samples for at
    all (discounted by FALLBACK_BLEND, since it isn't calibrated to the
    same scale as the template's own z-score similarity), and can never
    outscore/override a tag the reference actually demonstrated."""
    combined = dict(zone_matcher.score_candidate(features, template))
    for tag, f_score in _fallback_scores(features).items():
        if tag not in combined:
            combined[tag] = f_score * FALLBACK_BLEND
    if not combined:
        return "p", 50.0
    best_tag, best_score = max(combined.items(), key=lambda kv: kv[1])
    return best_tag, round(max(0.0, min(1.0, best_score)) * 100, 1)
