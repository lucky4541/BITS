"""Spell verification - pyspellchecker-backed, advisory only (spec:
"Spell checking is advisory unless PDF/OCR evidence confirms the
correction"). Books routinely contain proper names, authors,
organizations, technical terms, foreign words, Latin, citations, and
abbreviations that a bare dictionary would flag constantly - this module
applies conservative heuristics to skip the common false-positive shapes
BEFORE ever consulting the dictionary, rather than flagging everything
and hoping the operator dismisses it:

  - ALL-CAPS tokens (acronyms/initialisms: "NASA", "EPUB").
  - Tokens containing a digit (model numbers, dates, citations: "COVID-19", "1993").
  - A capitalized word that is NOT the first word of its sentence (the
    standard proper-noun signal - "Corráin" mid-sentence is very unlikely
    to be a spelling MISTAKE of an ordinary lowercase word).
  - Anything already in the project's own "Add to Dictionary" list
    (verification_models.VerificationSession.dictionary_additions).

A flagged word only ever reaches AUTO_FIX_AVAILABLE (see
verification_models.VerificationIssue.from_difference's own
auto_fixable parameter) when an independent second source - the OCR
evidence text, or a merged/split-word finding from word_join_verifier -
corroborates pyspellchecker's own top suggestion; otherwise it is
REVIEW-only, exactly matching the spec."""
import re

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_END = ".!?"

_checker = None


def _get_checker():
    global _checker
    if _checker is None:
        from spellchecker import SpellChecker
        _checker = SpellChecker()
    return _checker


def _looks_skippable(word: str, is_sentence_start: bool) -> bool:
    if not word:
        return True
    if word.isupper() and len(word) > 1:
        return True  # acronym
    if any(ch.isdigit() for ch in word):
        return True
    if word[:1].isupper() and not is_sentence_start:
        return True  # probable proper noun
    return False


def find_spelling_issues(text: str, dictionary_additions=None) -> list:
    """Returns raw finding dicts:
        {"word": str, "char_start": int, "char_end": int, "suggestion": str|None, "candidates": set}
    Only for words pyspellchecker's own dictionary doesn't recognize AND
    that survive the proper-noun/acronym/numeric heuristics above."""
    if not text or not text.strip():
        return []
    checker = _get_checker()
    allow = {w.casefold() for w in (dictionary_additions or [])}

    findings = []
    sentence_start = True
    for m in _WORD_RE.finditer(text):
        word = m.group(0)
        bare = word.split("'")[0]  # possessive/contraction prefix, e.g. "Corráin's" -> "Corráin"
        is_start = sentence_start
        sentence_start = False
        trailing = text[m.end():m.end() + 2]
        if any(ch in _SENTENCE_END for ch in trailing[:1]):
            sentence_start = True

        if bare.casefold() in allow:
            continue
        if _looks_skippable(bare, is_start):
            continue
        if checker.known([bare.casefold()]):
            continue
        candidates = checker.candidates(bare.casefold()) or set()
        suggestion = checker.correction(bare.casefold())
        findings.append({
            "word": word, "char_start": m.start(), "char_end": m.end(),
            "suggestion": suggestion, "candidates": candidates,
        })
    return findings
