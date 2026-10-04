"""Merged-word / missing-space / split-word detection (spec sections 9-11).

Signal priority is exactly as the spec mandates: PDF GEOMETRY IS PRIMARY.
When both sides have real word bounding boxes, a horizontal gap between
two original words that is at least `MIN_SPACE_GAP_RATIO` of the local
font size is treated as strong, direct evidence a real space existed
there - the dictionary/vocabulary check is only ever SUPPORTING evidence,
used to raise or lower confidence, never to overrule geometry, and never
consulted at all when geometry alone is already conclusive.

The "dictionary" here is deliberately NOT a bundled corpus (spec doesn't
require one, and bundling a real dictionary would be a much larger,
separate undertaking) - it is the union of every word ALREADY seen
elsewhere in the same comparison's own two documents (self-referential:
if "of" and "india" both appear as standalone words anywhere else in the
book, that's real, honest evidence they're genuine words) plus a small
fixed list of extremely common short English function words as a safety
net for short documents. This satisfies spec section 11's real
requirement directly: a word like "EPUBForge" or "COVID-19" is NEVER
split just because it's "unknown" - it is only ever reported as a merge
candidate when its own pieces are independently attested words AND/OR
geometry directly supports the split; anything else is left as UNCERTAIN
rather than guessed at."""
_COMMON_SHORT_WORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "but", "if", "so", "no",
    "do", "did", "does", "is", "are", "was", "were", "am", "be", "been", "being",
    "this", "that", "these", "those", "from", "with", "for", "by", "as", "it", "its",
    "he", "she", "they", "we", "you", "i", "not", "his", "her", "their", "our", "your",
    "up", "down", "out", "off", "over", "under", "into", "onto", "than", "then", "there",
    "here", "when", "where", "who", "what", "why", "how", "all", "any", "each", "some",
    "one", "two", "new", "old", "may", "can", "will", "shall", "must", "has", "have", "had",
}

MIN_SPACE_GAP_RATIO = 0.18   # gap-width / font-size threshold for "a real space was here"


def build_vocabulary(*documents) -> set:
    vocab = set(_COMMON_SHORT_WORDS)
    for document in documents:
        for _page, block in document.all_blocks():
            for word in block.text.semantic.split():
                cleaned = word.strip(".,;:!?\"'()[]{}")
                if cleaned:
                    vocab.add(cleaned)
    return vocab


def geometry_supports_split(gap_width: float, font_size: float) -> bool:
    if font_size <= 0:
        return False
    return (gap_width / font_size) >= MIN_SPACE_GAP_RATIO


def evaluate_merge(original_words: list, converted_word: str, vocabulary: set,
                    geometry_gap_confirmed: bool = False) -> dict:
    """original_words: 2+ consecutive original word strings whose
    concatenation (case-insensitively, ignoring internal spaces) equals
    converted_word - the caller (word_aligner) is responsible for finding
    this candidate; this function only judges how confidently it should
    be reported."""
    joined = "".join(w.casefold() for w in original_words)
    if joined != converted_word.casefold():
        return {"is_merge": False}
    dictionary_support = sum(1 for w in original_words if w.casefold() in vocabulary) / len(original_words)
    if geometry_gap_confirmed:
        confidence = 0.85 + 0.15 * dictionary_support
    else:
        confidence = 0.35 + 0.5 * dictionary_support
    return {
        "is_merge": True,
        "confidence": min(confidence, 0.995),
        "dictionary_support": dictionary_support,
        "geometry_supported": geometry_gap_confirmed,
        "uncertain": (not geometry_gap_confirmed) and dictionary_support < 0.5,
    }


def evaluate_split(original_word: str, converted_words: list, vocabulary: set) -> dict:
    joined = "".join(w.casefold() for w in converted_words)
    if joined != original_word.casefold():
        return {"is_split": False}
    dictionary_support = sum(1 for w in converted_words if w.casefold() in vocabulary) / len(converted_words)
    confidence = 0.4 + 0.5 * dictionary_support
    return {
        "is_split": True,
        "confidence": min(confidence, 0.99),
        "dictionary_support": dictionary_support,
        "uncertain": dictionary_support < 0.5,
    }
