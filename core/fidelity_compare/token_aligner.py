"""Coarse token-level alignment (spec section 53's "token" tier, between
sentence and word) - splits a block's text into whitespace-delimited
tokens (punctuation left attached, so "word," and "word" are distinct
tokens - this is intentional: it lets word_aligner.py notice a dropped
trailing comma as part of a CHANGED_WORD rather than losing it) and
aligns two token sequences via block_aligner's shared LCS implementation.
word_aligner.py consumes this module's output and refines each
'replace' region into the specific MERGED_WORD/SPLIT_WORD/CHANGED_WORD
finding it actually represents."""
from core.fidelity_compare import block_aligner


def tokenize(text: str) -> list:
    return text.split()


def align_tokens(original_text: str, converted_text: str) -> list:
    original_tokens = tokenize(original_text)
    converted_tokens = tokenize(converted_text)
    pairs = block_aligner.align_by_key(original_tokens, converted_tokens)
    return original_tokens, converted_tokens, pairs
