"""Paragraph-level structure comparison (spec section 18): detects
MERGED_PARAGRAPH / SPLIT_PARAGRAPH using block_aligner's LCS alignment as
the starting point, then confirms a merge/split (never guesses from
similarity alone - block_aligner's grouping already comes from real
sequence alignment, similarity() here is only the CONFIRMATION step) so
that a paragraph reflow is reported as "CONTENT PRESERVED, STRUCTURE
CHANGED" rather than as missing/added content."""
from core.fidelity_compare import block_aligner

MERGE_SPLIT_SIMILARITY_THRESHOLD = 0.85


def find_paragraph_structure_changes(original_blocks: list, converted_blocks: list) -> list:
    """original_blocks/converted_blocks: Block objects with kind ==
    'paragraph' only (caller filters). Returns a list of dicts describing
    each confirmed MERGED_PARAGRAPH/SPLIT_PARAGRAPH group, each carrying
    the original/converted block indices involved so the caller can
    exclude them from ordinary missing/added-content reporting."""
    pairs = block_aligner.align_blocks(original_blocks, converted_blocks)
    results = []
    i = 0
    while i < len(pairs):
        p = pairs[i]
        if p.op != "replace":
            i += 1
            continue
        group = [p]
        j = i + 1
        while j < len(pairs) and pairs[j].op == "replace":
            group.append(pairs[j])
            j += 1
        orig_idxs = sorted({g.original_index for g in group if g.original_index is not None})
        conv_idxs = sorted({g.converted_index for g in group if g.converted_index is not None})
        if len(orig_idxs) >= 2 and len(conv_idxs) == 1:
            joined = " ".join(original_blocks[k].text.semantic for k in orig_idxs)
            target = converted_blocks[conv_idxs[0]].text.semantic
            sim = block_aligner.similarity(joined, target)
            if sim >= MERGE_SPLIT_SIMILARITY_THRESHOLD:
                results.append({"type": "MERGED_PARAGRAPH", "original_indices": orig_idxs,
                                 "converted_indices": conv_idxs, "confidence": sim})
        elif len(conv_idxs) >= 2 and len(orig_idxs) == 1:
            joined = " ".join(converted_blocks[k].text.semantic for k in conv_idxs)
            target = original_blocks[orig_idxs[0]].text.semantic
            sim = block_aligner.similarity(joined, target)
            if sim >= MERGE_SPLIT_SIMILARITY_THRESHOLD:
                results.append({"type": "SPLIT_PARAGRAPH", "original_indices": orig_idxs,
                                 "converted_indices": conv_idxs, "confidence": sim})
        i = j
    return results
