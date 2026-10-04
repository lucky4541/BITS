"""Figure comparison (spec section 21) - matches figures across documents
using dimensions/aspect-ratio/perceptual-hash/caption/position TOGETHER
(never pixel-perfect equality, never any single signal alone), reusing
visual_compare.py's aHash as one of several signals.

Perceptual hashes are computed EXACTLY ONCE per figure, up front, and
reused for every pairwise comparison - the O(N*M) matching loop below
only ever does a cheap Hamming-distance compare on the already-computed
hash, never a fresh image decode+hash per pair. An earlier version
recomputed each figure's hash freely inside the pairwise loop itself
(len(conv_figs) redundant decodes per original figure, and vice versa) -
harmless for a handful of figures, but a real, confirmed cause of a
multi-minute "Comparing Figures" stall on a real book with a few hundred
full-resolution figures (spec section 65: "Support large books... Use...
image cache" - this IS that cache, just scoped to one comparison run
rather than persisted)."""
from core.fidelity_compare import block_aligner, visual_compare

IMAGE_SIMILARITY_MATCH_THRESHOLD = 0.80
ASPECT_RATIO_TOLERANCE = 0.05
POSITION_TOLERANCE_PT = 10.0


def _aspect_ratio(fig) -> float:
    return (fig.width / fig.height) if fig.height else 0.0


def _precomputed_hashes(figs: list) -> list:
    hashes = []
    for _page, fig in figs:
        if fig.image_bytes:
            try:
                hashes.append(visual_compare.average_hash(fig.image_bytes))
            except Exception:
                hashes.append(None)
        else:
            hashes.append(None)
    return hashes


def _match_score(orig_fig, conv_fig, orig_hash, conv_hash) -> float:
    scores, weights = [], []
    if orig_hash is not None and conv_hash is not None:
        scores.append(visual_compare.similarity_from_hashes(orig_hash, conv_hash))
        weights.append(3.0)
    if orig_fig.height and conv_fig.height:
        ar_diff = abs(_aspect_ratio(orig_fig) - _aspect_ratio(conv_fig))
        scores.append(1.0 - min(1.0, ar_diff / max(ASPECT_RATIO_TOLERANCE, 0.01)))
        weights.append(1.0)
    if orig_fig.caption and conv_fig.caption:
        scores.append(block_aligner.similarity(orig_fig.caption.semantic, conv_fig.caption.semantic))
        weights.append(2.0)
    if not scores:
        return 0.0
    return sum(s * w for s, w in zip(scores, weights)) / sum(weights)


def compare(original_doc, converted_doc, progress_cb=None, page_groups=None) -> list:
    orig_figs = [(page, fig) for page in original_doc.pages for fig in page.figures]
    conv_figs = [(page, fig) for page in converted_doc.pages for fig in page.figures]
    orig_hashes = _precomputed_hashes(orig_figs)
    conv_hashes = _precomputed_hashes(conv_figs)
    findings = []
    used_conv = set()

    # Most figures stay inside their aligned page group. Restrict the expensive
    # candidate loop to those pages first; retain a global fallback only when
    # no page-group candidate exists, so reflowed books remain supported.
    page_candidates = {}
    if page_groups:
        for g in page_groups:
            conv_pages = set(g.get("converted_pages", []))
            for opage in g.get("original_pages", []):
                page_candidates[opage] = conv_pages
    order_orig, order_conv = [], []
    total = len(orig_figs) or 1
    for oi, (op, ofig) in enumerate(orig_figs):
        if progress_cb:
            progress_cb(oi + 1, total)
        best_j, best_score = None, 0.0
        allowed_pages = page_candidates.get(op.number)
        candidate_indices = (
            [ci for ci, (cp, _cfig) in enumerate(conv_figs) if cp.number in allowed_pages]
            if allowed_pages else range(len(conv_figs))
        )
        for ci in candidate_indices:
            if ci in used_conv:
                continue
            score = _match_score(ofig, conv_figs[ci][1], orig_hashes[oi], conv_hashes[ci])
            if score > best_score:
                best_score, best_j = score, ci
        if best_j is not None and best_score >= IMAGE_SIMILARITY_MATCH_THRESHOLD:
            used_conv.add(best_j)
            cp, cfig = conv_figs[best_j]
            order_orig.append(oi)
            order_conv.append(best_j)
            if ofig.bbox.x0 or ofig.bbox.y0 or cfig.bbox.x0 or cfig.bbox.y0:
                dx = cfig.bbox.x0 - ofig.bbox.x0
                dy = cfig.bbox.y0 - ofig.bbox.y0
                if (abs(dx) > POSITION_TOLERANCE_PT or abs(dy) > POSITION_TOLERANCE_PT) and op.number != cp.number:
                    findings.append({"type": "FIGURE_POSITION_CHANGED", "original_page": op.number,
                                      "converted_page": cp.number, "severity": "MEDIUM", "confidence": 0.7})
            if ofig.caption and cfig.caption:
                if ofig.caption.semantic != cfig.caption.semantic:
                    findings.append({"type": "CHANGED_CAPTION", "original_page": op.number,
                                      "converted_page": cp.number, "original_text": ofig.caption.raw,
                                      "converted_text": cfig.caption.raw, "severity": "MEDIUM", "confidence": 0.8})
            elif ofig.caption and not cfig.caption:
                findings.append({"type": "MISSING_CAPTION", "original_page": op.number,
                                  "text": ofig.caption.raw, "severity": "MEDIUM", "confidence": 0.75})
            if best_score < 0.97:
                findings.append({"type": "CHANGED_FIGURE", "original_page": op.number,
                                  "converted_page": cp.number, "similarity": round(best_score, 3),
                                  "severity": "LOW", "confidence": best_score})
        else:
            findings.append({"type": "MISSING_FIGURE", "original_page": op.number, "severity": "HIGH",
                              "confidence": 0.8})
    for ci, (cp, cfig) in enumerate(conv_figs):
        if ci not in used_conv:
            findings.append({"type": "ADDED_FIGURE", "converted_page": cp.number, "severity": "HIGH",
                              "confidence": 0.8})
    if order_orig != sorted(order_orig):
        findings.append({"type": "FIGURE_REORDERED", "severity": "MEDIUM", "confidence": 0.6})
    return findings
