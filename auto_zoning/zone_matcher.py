"""Deterministic, non-ML scoring of a candidate zone's feature vector
against every tag's learned profile (layout_template.TagProfile) in a
LayoutTemplate. No coordinates are ever copied - only the SIMILARITY of a
candidate's own (normalized) geometry/font/text pattern to what the
reference project(s) showed for each tag is used."""
import math

from auto_zoning.layout_template import NUMERIC_KEYS

# Per-feature weight in the combined score - position/size carry the most
# signal for distinguishing tags in this app's own tag set (e.g. a Title's
# near-top, wide bbox vs a Figure's), font/text-pattern refine it. A
# bullet/number marker's presence/absence is handled separately, below, as
# a near-hard multiplicative gate rather than one more additive term - it
# is close to deterministic evidence (a marker either is or isn't there),
# so it must not be drowned out by a handful of coincidentally-close
# geometry features the way an ordinary weighted-average term would be.
WEIGHTS = {
    "y": 2.0, "x": 1.0, "w": 1.5, "h": 1.0, "indent": 1.0,
    "font_ratio": 2.0, "bold": 1.5, "italic": 0.5, "all_caps": 1.0,
    "text_len": 0.5, "line_count": 0.5,
}
MARKER_MISMATCH_PENALTY = 0.25


def _numeric_similarity(value, mean, std) -> float:
    """1.0 = exact match to the reference mean, decaying smoothly with
    distance in units of the reference's own observed spread (z-score) - a
    tag the reference always draws in a tight, consistent spot scores a
    near-miss much harder than one the reference itself used loosely."""
    if value is None or mean is None:
        return 0.5  # neutral - neither side has data to compare
    std = std if std and std > 1e-6 else 0.05
    z = abs(value - mean) / std
    return math.exp(-0.5 * z * z)


def _bool_similarity(value, ratio) -> float:
    """`ratio` = fraction of reference samples for this tag that were True.
    A candidate whose own bool matches the majority scores high; the further
    the reference itself was split on this feature, the less either answer
    is penalized (a tag with a near-50/50 bold split tells us nothing)."""
    target = 1.0 if value else 0.0
    return 1.0 - abs(target - ratio)


def _text_presence_gate(candidate_text_len, profile) -> float:
    """Another multiplicative gate, same rationale as _list_marker_gate:
    graphic/figure/equation zones structurally NEVER carry text
    (ZoneManager._refresh_text explicitly skips extracting text for those
    tags), so every one of their reference samples has text_len==0 and
    font_ratio==None. Without this gate, a text-bearing candidate's font/
    text-pattern features simply have "no data to compare" (neutral 0.5
    similarity - see _numeric_similarity) against such a tag, which can
    accidentally outscore a genuine text tag whose OWN font happens to
    differ from the reference by more than that neutral 0.5 - i.e. "no
    evidence" must never look better than "evidence that mismatches"."""
    lens = [s.get("text_len") for s in profile.samples if s.get("text_len") is not None]
    if not lens:
        return 1.0
    profile_ever_has_text = any(n > 0 for n in lens)
    profile_always_has_text = all(n > 0 for n in lens)
    candidate_has_text = (candidate_text_len or 0) > 0
    if candidate_has_text and not profile_ever_has_text:
        return MARKER_MISMATCH_PENALTY   # candidate has real text; this tag is structurally textless
    if not candidate_has_text and profile_always_has_text:
        return MARKER_MISMATCH_PENALTY   # candidate has no text; this tag always has some
    return 1.0


def _list_marker_gate(candidate_marker, profile) -> float:
    """A multiplicative penalty (1.0 = no penalty) applied to a tag's whole
    score at the end, based on marker presence/absence - kept SEPARATE from
    the ordinary weighted-average features above (see WEIGHTS' comment) so
    it can't be outvoted by a few coincidentally-close geometry features.
    total == profile.count means every single reference sample of this tag
    carried a marker (the ordinary case for "list"/"list-item" - ZoneTool's
    list-item zones are only ever created for genuinely marked content), so
    an UNMARKED candidate is very unlikely to really be this tag; a MARKED
    candidate is likewise unlikely to be a tag the reference never showed
    marked content for at all."""
    has_list_data = bool(profile.list_types)
    total = sum(profile.list_types.values()) if has_list_data else 0
    if candidate_marker is not None and not has_list_data:
        return MARKER_MISMATCH_PENALTY
    if candidate_marker is None and has_list_data and total == profile.count:
        return MARKER_MISMATCH_PENALTY
    return 1.0


def score_candidate(features: dict, template) -> list:
    """Returns [(tag, score 0..1), ...] sorted best-first, one entry per tag
    the template has ANY samples for. A tag with too few samples to be
    trustworthy is still scored (never crashes on count==0 elsewhere) but
    tag_predictor.py's fallback heuristics take over when nothing scores
    convincingly - see there."""
    results = []
    for tag, profile in template.tag_profiles.items():
        if profile.count == 0:
            continue
        total_w = 0.0
        total_s = 0.0
        for key in NUMERIC_KEYS:
            mean, std = profile.stat(key)
            if mean is None:
                continue
            w = WEIGHTS.get(key, 1.0)
            total_w += w
            total_s += w * _numeric_similarity(features.get(key), mean, std)
        for key in ("bold", "italic", "all_caps"):
            w = WEIGHTS.get(key, 1.0)
            total_w += w
            total_s += w * _bool_similarity(features.get(key), profile.bool_ratio(key))
        score = (total_s / total_w) if total_w > 0 else 0.0
        score *= _list_marker_gate(features.get("list_marker"), profile)
        score *= _text_presence_gate(features.get("text_len"), profile)
        results.append((tag, score))
    results.sort(key=lambda t: t[1], reverse=True)
    return results
