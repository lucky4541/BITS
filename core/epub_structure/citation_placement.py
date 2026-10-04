"""Steps 18-20/23-25 (spec: "FIRST-CITATION PLACEMENT" / "COMPLETE FIGURE
BLOCK" / "FIGURE/TABLE WITHOUT CITATION" / "DECORATIVE / INLINE IMAGES") -
decides, per figure/table, whether to move it and where, then performs the
move as a real DOM relocation (lxml Element.addnext - detach + reinsert
the SAME node, never a clone; spec 31: "Do not clone it").

Deliberately conservative about WHAT counts as "high confidence enough to
move automatically":
  - No citation anywhere -> KEPT, never moved (spec 24: "do not randomly
    move it").
  - First citation in a DIFFERENT document -> REVIEW only (a cross-file
    reference becomes a LINK - see core.epub_structure.figure_table_links -
    never a cross-file relocation, which would be a much bigger structural
    change than "first citation placement" is meant to cover).
  - First citation in the SAME document, at or after the figure's current
    position -> KEPT (it's already correctly placed relative to spec 18's
    own rule; moving it further would be wrong).
  - First citation in the SAME document, genuinely BEFORE the figure's
    current position -> MOVED, immediately after the citing paragraph."""
from dataclasses import dataclass, field

from core.epub_structure.figure_table_registry import first_citation
from core.epub_structure.xhtml_parser import local_name

# A block-level element immediately following a figure that is really
# PART of it (spec 19/20: "COMPLETE FIGURE BLOCK... notes... source...").
_ATTACHED_FOLLOWER_CLASSES = ("figsource", "source", "tblnote", "tablenote", "figurenote")


@dataclass
class PlacementDecision:
    entry: object                  # figure_table_registry.FigureTableEntry
    action: str                      # "moved" / "kept" / "review"
    confidence: str                   # "HIGH" / "MEDIUM" / "LOW"
    reason: str
    anchor_element: object = None       # the citing paragraph, when action == "moved"
    anchor_element_index: int = -1        # the citing paragraph's own element_index, when action == "moved"
    cross_file_target: str = ""          # set only for the "review: different document" case


def _is_attached_follower(el) -> bool:
    if local_name(el.tag) != "p":
        return False
    class_attr = (el.get("class") or "").lower()
    return any(marker in class_attr for marker in _ATTACHED_FOLLOWER_CLASSES)


def plan_placements(entries: list) -> list:
    """THE analysis-only entry point - decides but does not touch any DOM
    node. Every FigureTableEntry gets exactly one decision, whether or not
    it ends up moving."""
    decisions = []
    for entry in entries:
        if not entry.number:
            decisions.append(PlacementDecision(
                entry=entry, action="kept", confidence="LOW",
                reason="caption has no parseable figure/table number - position preserved"))
            continue

        fc = first_citation(entry)
        if fc is None:
            decisions.append(PlacementDecision(
                entry=entry, action="kept", confidence="LOW",
                reason="no citation found anywhere in the book - position preserved"))
            continue

        if fc.doc_path != entry.doc_path:
            decisions.append(PlacementDecision(
                entry=entry, action="review", confidence="MEDIUM",
                reason=f"first citation is in a different document ({fc.doc_path}) - "
                       "linked, not physically relocated across files", cross_file_target=fc.doc_path))
            continue

        if fc.element_index >= entry.element_index:
            decisions.append(PlacementDecision(
                entry=entry, action="kept", confidence="HIGH",
                reason="already positioned at or after its first citation"))
            continue

        decisions.append(PlacementDecision(
            entry=entry, action="moved", confidence="HIGH",
            reason=f"moved immediately after its first citation "
                   f"({entry.element_index - fc.element_index} element(s) earlier than its old position)",
            anchor_element=fc.element, anchor_element_index=fc.element_index))

    _resolve_order_conflicts(decisions)
    return decisions


def _resolve_order_conflicts(decisions: list) -> None:
    """Mutates `decisions` in place (spec: "CRITICAL FIGURE ORDER BUG" /
    "FIGURE ORDER MUST BE SOURCE-AWARE") - downgrades a "moved" decision
    back to "kept" whenever performing the move would require leapfrogging
    another figure/table entry in the SAME document that is not itself
    moving to an equal-or-earlier position.

    Each "moved" decision only ever targets a position EARLIER than the
    entry's own original spot (plan_placements never moves anything later).
    If some OTHER figure/table originally sits inside that gap and is not
    also relocating to that same-or-earlier point, physically performing
    the move would place this figure BEFORE something the source always
    had it AFTER - exactly the reported bug (an uncaptioned image block,
    or a figure whose own first citation is in a different document and so
    physically never moves at all, ends up leapfrogged). Source print
    order between figures/tables is a HARDER constraint than "move to
    first citation": a move that would violate it is rejected, not forced
    (spec 25/31: never guess, prefer the safe/unchanged position).

    Iterates to a fixed point: rejecting one move can, in turn, remove the
    last remaining reason another move was still safe (real example: an
    uncaptioned figure blocks Fig 1.3's move; once Fig 1.3 is frozen back
    at its original spot, it can itself become the obstacle blocking
    Fig 1.6's own move a few figures later)."""
    by_doc = {}
    for d in decisions:
        if d.entry.kind not in ("figure", "table"):
            continue
        by_doc.setdefault(d.entry.doc_path, []).append(d)

    for doc_decisions in by_doc.values():
        changed = True
        while changed:
            changed = False
            for d in doc_decisions:
                if d.action != "moved":
                    continue
                lo, hi = d.anchor_element_index, d.entry.element_index
                blocker = None
                for other in doc_decisions:
                    if other is d:
                        continue
                    if not (lo < other.entry.element_index < hi):
                        continue
                    if other.action == "moved" and other.anchor_element_index <= lo:
                        continue   # also relocating to an equal-or-earlier point - no crossing, not an obstacle
                    blocker = other
                    break
                if blocker is not None:
                    d.action = "kept"
                    d.confidence = "HIGH"
                    d.reason = (f"first-citation move rejected: moving here would place this "
                                f"{d.entry.kind} before another {blocker.entry.kind}/{blocker.entry.id or 'entry'} "
                                f"that is not relocating far enough - preserving the source's own print order "
                                f"between figures/tables takes priority over first-citation placement")
                    d.anchor_element = None
                    d.anchor_element_index = -1
                    changed = True


def apply_placements(decisions: list) -> list:
    """Performs every "moved" decision as a real tree relocation. Processed
    in the SAME order the figures/tables themselves already appear in the
    book (doc_order, then original element_index) so that when several
    figures share the same citing paragraph as their anchor, they end up
    after it in their OWN original relative order - never reversed, never
    interleaved unpredictably. Returns [(entry, old_position_desc,
    new_position_desc), ...] for reporting."""
    moved = sorted((d for d in decisions if d.action == "moved"),
                   key=lambda d: (d.entry.doc_order, d.entry.element_index))
    changes = []
    last_after = {}   # id(anchor_element) -> the element currently sitting right after it (for stacking)

    for decision in moved:
        entry = decision.entry
        fig_el = entry.element
        anchor = decision.anchor_element
        old_desc = f"{entry.doc_path} (position {entry.element_index})"

        # Gather any attached follower(s) (spec 19: complete block) BEFORE
        # detaching the figure - they must move together, in their own
        # existing relative order.
        followers = []
        sib = fig_el.getnext()
        while sib is not None and _is_attached_follower(sib):
            followers.append(sib)
            sib = sib.getnext()

        insertion_point = last_after.get(id(anchor), anchor)
        insertion_point.addnext(fig_el)
        last_after[id(anchor)] = fig_el
        for follower in followers:
            fig_el.addnext(follower)
            fig_el = follower  # keep chaining so multiple followers keep their own order

        new_desc = f"{entry.doc_path}, immediately after its first citation"
        changes.append((entry, old_desc, new_desc, len(followers)))

    return changes
