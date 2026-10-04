"""Refines token_aligner's coarse alignment into the specific content
difference types spec section 8 requires: MISSING_WORD, ADDED_WORD,
CHANGED_WORD, MERGED_WORD, SPLIT_WORD (plus their MISSING_SPACE/
EXTRA_SPACE companions) - the core of "content comparison" (section 8/9).

Consecutive 'replace' opcodes from token_aligner are grouped exactly like
paragraph_aligner groups block-level replace runs, then tested for a
merge (N original tokens -> 1 converted token) or split (1 original
token -> N converted tokens) via merged_word_detector before falling
back to a plain CHANGED_WORD (1:1) classified further by character_diff."""
from core.fidelity_compare import character_diff, merged_word_detector, spacing_detector


def _normalize_for_join(token: str) -> str:
    return token.strip(".,;:!?\"'()[]{}")


def find_word_differences(original_tokens: list, converted_tokens: list, pairs: list,
                           vocabulary: set) -> list:
    findings = []
    i = 0
    while i < len(pairs):
        p = pairs[i]
        if p.op == "equal":
            i += 1
            continue
        if p.op == "delete":
            findings.append({"type": "MISSING_WORD", "original_index": p.original_index,
                              "original_text": original_tokens[p.original_index], "converted_text": "",
                              "confidence": 0.95})
            i += 1
            continue
        if p.op == "insert":
            findings.append({"type": "ADDED_WORD", "converted_index": p.converted_index,
                              "original_text": "", "converted_text": converted_tokens[p.converted_index],
                              "confidence": 0.95})
            i += 1
            continue
        # Group the whole contiguous 'replace' run for merge/split analysis.
        group = [p]
        j = i + 1
        while j < len(pairs) and pairs[j].op == "replace":
            group.append(pairs[j])
            j += 1
        orig_idxs = sorted({g.original_index for g in group if g.original_index is not None})
        conv_idxs = sorted({g.converted_index for g in group if g.converted_index is not None})
        orig_words = [original_tokens[k] for k in orig_idxs]
        conv_words = [converted_tokens[k] for k in conv_idxs]

        handled = False
        if len(orig_words) >= 2 and len(conv_words) == 1:
            merge = merged_word_detector.evaluate_merge(
                [_normalize_for_join(w) for w in orig_words], _normalize_for_join(conv_words[0]), vocabulary)
            if merge.get("is_merge"):
                findings.append({"type": "MERGED_WORD", "companion": spacing_detector.companion_tag_for_merge(),
                                  "original_index": orig_idxs[0], "converted_index": conv_idxs[0],
                                  "original_text": " ".join(orig_words), "converted_text": conv_words[0],
                                  "confidence": merge["confidence"], "uncertain": merge.get("uncertain", False)})
                handled = True
        elif len(conv_words) >= 2 and len(orig_words) == 1:
            split = merged_word_detector.evaluate_split(
                _normalize_for_join(orig_words[0]), [_normalize_for_join(w) for w in conv_words], vocabulary)
            if split.get("is_split"):
                findings.append({"type": "SPLIT_WORD", "companion": spacing_detector.companion_tag_for_split(),
                                  "original_index": orig_idxs[0], "converted_index": conv_idxs[0],
                                  "original_text": orig_words[0], "converted_text": " ".join(conv_words),
                                  "confidence": split["confidence"], "uncertain": split.get("uncertain", False)})
                handled = True

        if not handled:
            # Fall back to per-position CHANGED_WORD (1:1 where possible).
            # original_index/converted_index (spec: "EXACT CONTENT
            # DIFFERENCE ENGINE" section 20 - "which word changed") mirror
            # the SAME field the MISSING_WORD/ADDED_WORD/MERGED_WORD/
            # SPLIT_WORD findings above already carry - this is the single
            # most common finding type (a plain 1:1 word substitution), so
            # leaving it index-less here was a real, confirmed gap.
            for k in range(max(len(orig_words), len(conv_words))):
                ow = orig_words[k] if k < len(orig_words) else ""
                cw = conv_words[k] if k < len(conv_words) else ""
                oidx = orig_idxs[k] if k < len(orig_idxs) else None
                cidx = conv_idxs[k] if k < len(conv_idxs) else None
                if not ow and cw:
                    findings.append({"type": "ADDED_WORD", "original_text": "", "converted_text": cw,
                                      "converted_index": cidx, "confidence": 0.7})
                elif ow and not cw:
                    findings.append({"type": "MISSING_WORD", "original_text": ow, "converted_text": "",
                                      "original_index": oidx, "confidence": 0.7})
                else:
                    diff = character_diff.diff_words(ow, cw)
                    if diff.get("changed"):
                        findings.append({"type": "CHANGED_WORD", "sub_classification": diff.get("classification"),
                                          "original_text": ow, "converted_text": cw, "char_diff": diff,
                                          "original_index": oidx, "converted_index": cidx, "confidence": 0.85})
        i = j
    return findings
