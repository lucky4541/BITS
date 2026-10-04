"""General content verification - missing/extra/wrong words and pure
punctuation changes, via word-level alignment (difflib, the same
alignment strategy core.fidelity_compare's own engine.py uses) between
the zone's own extracted text and an independent second reading (OCR
evidence). Word-COUNT-changing mismatches (a real merge/split) are
deliberately left to word_join_verifier.py - this module only reports
"replace" blocks where both sides have the SAME number of words (a true
word-for-word substitution) plus pure delete/insert (missing/extra
words), so the two verifiers never double-report the same alignment gap.
"""
import difflib
import re

_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# Structure / page-number-merge detection (spec sections 18/35): a real
# production shape - "ARTURO VALENZUELA" (an all-caps heading/name run,
# e.g. an editorial-board listing) immediately followed by a bare page
# number with nothing but whitespace between them, both swallowed into
# the same zone's text at extraction time. Deliberately narrow/high-
# precision: requires the number to sit at the very START or END of the
# WHOLE zone text (never mid-paragraph - "In 1993 the population reached
# 84 million" never matches, since neither "1993" nor "84" is the first/
# last token) AND the adjacent word to be capitalized/all-caps with no
# sentence-ending punctuation directly before it (an ordinary sentence
# ending in a period is left alone - far more likely to be a coincidental
# number than a genuine page-number merge, and the spec explicitly warns
# against ambiguous auto-merging).
_TRAILING_NUM_RE = re.compile(r"(.*\S)[ \t]+(\d{1,4})\s*$", re.DOTALL)
_LEADING_NUM_RE = re.compile(r"^\s*(\d{1,4})[ \t]+(\S.*)$", re.DOTALL)


def _words(text: str) -> list:
    return (text or "").split()


def _strip_punct(word: str) -> str:
    return word.strip(".,;:!?\"'()[]{}‘’“”–—")


def _classify_word_pair(original_word: str, converted_word: str) -> str:
    """CONTENT (a genuinely different word) vs PUNCTUATION (same letters,
    only surrounding/internal punctuation differs)."""
    o_bare, c_bare = _strip_punct(original_word), _strip_punct(converted_word)
    if o_bare.casefold() == c_bare.casefold() and original_word != converted_word:
        return "PUNCTUATION"
    return "CONTENT"


def find_content_issues(extracted_text: str, ocr_text: str) -> list:
    """Returns raw finding dicts (see verification_engine.py for how these
    become Difference/VerificationIssue objects):
        {"kind": "missing", "words": [...], "word_index": int}
        {"kind": "extra", "words": [...], "word_index": int}
        {"kind": "changed", "issue_type": "CONTENT"|"PUNCTUATION",
         "original_word": str, "converted_word": str, "word_index": int}
    `words`/`original_word` refer to the OCR (evidence) side, `converted_*`
    to the zone's own currently-extracted text - same original/converted
    convention as the rest of this package's reused detectors."""
    if not ocr_text or not ocr_text.strip():
        return []
    ocr_words = _words(ocr_text)
    extracted_words = _words(extracted_text)
    if not ocr_words:
        return []

    matcher = difflib.SequenceMatcher(
        a=[w.casefold() for w in ocr_words], b=[w.casefold() for w in extracted_words], autojunk=False)
    findings = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            findings.append({"kind": "missing", "words": ocr_words[i1:i2], "word_index": j1})
        elif tag == "insert":
            findings.append({"kind": "extra", "words": extracted_words[j1:j2], "word_index": j1})
        elif tag == "replace":
            ocr_chunk, extracted_chunk = ocr_words[i1:i2], extracted_words[j1:j2]
            if len(ocr_chunk) != len(extracted_chunk):
                continue  # a real merge/split - word_join_verifier's own job, not this module's
            for offset, (ow, cw) in enumerate(zip(ocr_chunk, extracted_chunk)):
                if ow == cw:
                    continue
                findings.append({"kind": "changed", "issue_type": _classify_word_pair(ow, cw),
                                  "original_word": ow, "converted_word": cw, "word_index": j1 + offset})
    return findings


def _looks_like_heading_boundary(word: str) -> bool:
    return bool(word) and (word.isupper() or word[:1].isupper())


def find_structure_issues(text: str, ocr_text: str = None) -> list:
    """Returns raw finding dicts:
        {"kind": "page_number_merge", "position": "trailing"|"leading",
         "number": str, "content": str}
    `ocr_text` is accepted for API symmetry with the other find_* functions
    in this module (a future enhancement could use it to confirm the
    number is genuinely a separate line in the source) but is not
    required - this check is precise enough from the zone's own text
    alone (see this module's own top-of-file comment for why)."""
    findings = []
    if not text or not text.strip():
        return findings
    stripped = text.strip()

    m = _TRAILING_NUM_RE.match(stripped)
    if m:
        prefix, number = m.group(1), m.group(2)
        words = prefix.split()
        last_word = words[-1] if words else ""
        if (len(words) >= 2 and _looks_like_heading_boundary(last_word)
                and not prefix.rstrip().endswith((".", ",", ";", ":"))):
            findings.append({"kind": "page_number_merge", "position": "trailing",
                              "number": number, "content": prefix})

    m = _LEADING_NUM_RE.match(stripped)
    if m:
        number, rest = m.group(1), m.group(2)
        words = rest.split()
        first_word = words[0] if words else ""
        if len(words) >= 2 and _looks_like_heading_boundary(first_word):
            findings.append({"kind": "page_number_merge", "position": "leading",
                              "number": number, "content": rest})

    return findings
