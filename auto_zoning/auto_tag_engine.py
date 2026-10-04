"""AUTO TAG decision engine.

For every analysed block (in reading order) it combines

    semantic role candidates (semantic_classifier)
  x project tag options for each role (TagKnowledgeModel.options_for_role)
  + structural verdicts (DTD, Mapping.xml families, reference-corpus succession)

and picks the highest-scoring STRUCTURALLY VALID tag. If the best option
violates a hard constraint, the next valid candidate is evaluated. When no
candidate is valid, or the evidence is weak / ambiguous, the decision is
flagged NEEDS REVIEW instead of being presented as certain.

Every decision carries a confidence breakdown:
    layout      - how clean the block segmentation is
    formatting  - reliability of the character-level formatting evidence
    semantic    - role score and its margin over the runner-up
    tag         - how well the project tag matches the role
    structure   - DTD / family / corpus plausibility in context
"""
from dataclasses import dataclass, field

WEIGHTS = {"layout": 0.15, "formatting": 0.1, "semantic": 0.35, "tag": 0.2, "structure": 0.2}
AMBIGUOUS_MARGIN = 0.06


@dataclass
class TagDecision:
    block: object
    role: str = None
    option: object = None             # TagOption or None (role has no project tag -> not zoned)
    score: float = 0.0
    confidence: float = 0.0           # 0..100
    breakdown: dict = field(default_factory=dict)
    needs_review: bool = False
    review_reasons: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)   # [(label, role, score)]
    skipped_reason: str = None

    @property
    def tag(self):
        return self.option.tag if self.option else None

    def zone_attributes(self) -> dict:
        attrs = self.option.zone_attributes() if self.option else {}
        attrs.update({
            "source": "auto",
            "auto_engine": "layout-semantic-v1",
            "auto_role": self.role,
            "confidence": round(self.confidence, 1),
            "confidence_breakdown": {k: round(v, 3) for k, v in self.breakdown.items()},
            "auto_evidence": [e for e in self.evidence if e][:8],
            "auto_alternatives": [[a[0], a[1], round(a[2], 3)] for a in self.alternatives[:4]],
        })
        if self.needs_review:
            attrs["needs_review"] = True
            attrs["review_reasons"] = list(self.review_reasons)
        return attrs


def _layout_confidence(block) -> float:
    f = block.features
    if block.kind in ("figure", "table"):
        return 0.85 if f.get("vector") else 0.9
    conf = 0.93
    if f.get("source") == "ocr" or f.get("ocr") or any(getattr(li, "source", "pdf") == "ocr" for li in block.lines):
        conf -= 0.15
    sizes = [li.font_size for li in block.lines] or list(f.get("_line_sizes", []))
    if sizes and max(sizes) - min(sizes) > 1.0:
        conf -= 0.12
    if f.get("n_lines", 1) == 1 and f.get("words", 9) <= 1 and block.kind == "text":
        conf -= 0.08
    return max(0.3, conf)


def _formatting_confidence(block) -> float:
    deco = block.features.get("decorated_ratio", 0.0)
    if not deco:
        return 0.97
    return 0.88


def decide_page(layout, role_map: dict, km, thresholds: dict = None) -> list:
    """One TagDecision per block, in reading order."""
    thresholds = thresholds or {"high": 90, "medium": 75}
    decisions = []
    prev_tag = None
    blocks = list(layout.blocks)
    # look-ahead tags (best unconstrained guess) for next-neighbour checks
    lookahead = []
    for b in blocks:
        cands = role_map.get(id(b), [])
        tag = None
        for rc in cands:
            m = km.best_for_role(rc.role)
            if m:
                tag = m.option.tag
                break
        lookahead.append(tag)

    for i, block in enumerate(blocks):
        cands = role_map.get(id(block), [])
        dec = TagDecision(block=block)
        next_tag = lookahead[i + 1] if i + 1 < len(lookahead) else None
        scored = []
        for rc in cands:
            matches = km.options_for_role(rc.role)
            if not matches:
                continue
            for m in matches[:3]:
                verdict = km.structural_verdict(m.option.tag, prev_tag=prev_tag, next_tag=next_tag)
                total = rc.score * (0.75 + 0.25 * min(1.0, m.score)) + verdict.adjust
                scored.append((total, rc, m, verdict))
        if not scored:
            top_role = cands[0].role if cands else None
            dec.role = top_role
            dec.skipped_reason = (f"role '{top_role}' has no tag in this project" if top_role
                                  else "no role candidate")
            decisions.append(dec)
            continue
        scored.sort(key=lambda t: -t[0])
        valid = [t for t in scored if t[3].ok]
        chosen = valid[0] if valid else scored[0]
        total, rc, m, verdict = chosen
        dec.role, dec.option, dec.score = rc.role, m.option, total
        dec.evidence = list(rc.evidence) + list(m.reasons) + list(verdict.reasons)
        seen = set()
        for t in scored:
            key = t[2].option.label
            if key in seen or key == m.option.label:
                continue
            seen.add(key)
            dec.alternatives.append((key, t[1].role, t[0]))

        # confidence breakdown
        role_scores = sorted({c.role: c.score for c in cands}.values(), reverse=True)
        margin = (role_scores[0] - role_scores[1]) if len(role_scores) > 1 else role_scores[0]
        semantic = rc.score * (0.75 + 0.25 * min(1.0, margin / 0.2))
        structure = 0.3 if not verdict.ok else max(0.0, min(1.0, 0.92 + verdict.adjust))
        dec.breakdown = {
            "layout": _layout_confidence(block),
            "formatting": _formatting_confidence(block),
            "semantic": max(0.0, min(1.0, semantic)),
            "tag": max(0.0, min(1.0, m.score)),
            "structure": structure,
        }
        dec.confidence = 100.0 * sum(WEIGHTS[k] * v for k, v in dec.breakdown.items())
        if not verdict.ok:
            dec.needs_review = True
            dec.review_reasons.append("no structurally valid tag - best guess kept: " + "; ".join(verdict.reasons))
        if valid and chosen is not scored[0]:
            # Error recovery: the preferred tag was structurally invalid and
            # the next valid candidate was used - right structure, but a
            # human should confirm the semantic choice.
            dec.needs_review = True
            dec.review_reasons.append(f"top candidate {scored[0][2].option.label} rejected: "
                                      + "; ".join(scored[0][3].reasons))
        if dec.confidence < thresholds.get("medium", 75):
            dec.needs_review = True
            dec.review_reasons.append(f"low confidence ({dec.confidence:.0f}%)")
        if margin < AMBIGUOUS_MARGIN and len(role_scores) > 1 and \
                dec.alternatives and dec.alternatives[0][1] != rc.role:
            dec.needs_review = True
            dec.review_reasons.append(f"ambiguous between {rc.role} and {dec.alternatives[0][1]}")
        decisions.append(dec)
        prev_tag = dec.tag
    return decisions
