"""Grammar verification - a small, lightweight, offline rule set (no new
dependency; language_tool_python would need a Java runtime plus a ~200MB
download, contradicting this app's existing offline-first precedent for
OCR models). Grammar findings are ALWAYS advisory (spec: "Grammar
checking must NEVER freely rewrite book content") - verification_models.
VerificationIssue.from_difference hard-codes GRAMMAR into its
_ADVISORY_ONLY_TYPES, so nothing here can ever reach AUTO_FIX_AVAILABLE
regardless of how confident a rule is.

Deliberately only a few, high-precision rules rather than broad coverage
- a missed error is safe (the operator simply doesn't get a suggestion);
a wrong suggestion on quoted/intentional author wording is not, so every
rule here is narrow enough to rarely fire on legitimate prose."""
import re

_REPEATED_WORD_RE = re.compile(r"\b(\w+)([ \t]+)(\1)\b", re.IGNORECASE)
_OF_FOR_HAVE_RE = re.compile(r"\b(would|could|should|might|must)\s+of\b", re.IGNORECASE)

_PLURAL_SUBJECTS = r"(results|studies|findings|data|authors|researchers|scholars|critics|sources)"
_SINGULAR_VERBS = r"(shows|proves|suggests|indicates|demonstrates|argues|claims|reveals|confirms)"
_SUBJECT_VERB_RE = re.compile(rf"\b{_PLURAL_SUBJECTS}\s+{_SINGULAR_VERBS}\b", re.IGNORECASE)
_VERB_PLURAL = {
    "shows": "show", "proves": "prove", "suggests": "suggest", "indicates": "indicate",
    "demonstrates": "demonstrate", "argues": "argue", "claims": "claim", "reveals": "reveal",
    "confirms": "confirm",
}

_DOUBLE_QUOTE_RE = re.compile(r'["“”]')


def find_grammar_issues(text: str) -> list:
    """Returns raw finding dicts:
        {"rule": str, "char_start": int, "char_end": int, "match_text": str,
         "suggestion": str, "explanation": str}"""
    if not text or not text.strip():
        return []
    findings = []

    for m in _REPEATED_WORD_RE.finditer(text):
        findings.append({
            "rule": "repeated_word", "char_start": m.start(), "char_end": m.end(), "match_text": m.group(0),
            "suggestion": m.group(1), "explanation": f'repeated word "{m.group(1)}"',
        })

    for m in _OF_FOR_HAVE_RE.finditer(text):
        modal = m.group(1)
        findings.append({
            "rule": "of_for_have", "char_start": m.start(), "char_end": m.end(), "match_text": m.group(0),
            "suggestion": f"{modal} have", "explanation": f'"{modal} of" should be "{modal} have"',
        })

    for m in _SUBJECT_VERB_RE.finditer(text):
        subject, verb = m.group(1), m.group(2)
        plural_verb = _VERB_PLURAL.get(verb.lower(), verb)
        findings.append({
            "rule": "subject_verb_agreement", "char_start": m.start(), "char_end": m.end(),
            "match_text": m.group(0), "suggestion": f"{subject} {plural_verb}",
            "explanation": f'plural subject "{subject}" with singular verb "{verb}"',
        })

    quote_count = len(_DOUBLE_QUOTE_RE.findall(text))
    if quote_count % 2 == 1:
        findings.append({
            "rule": "mismatched_quotes", "char_start": 0, "char_end": len(text), "match_text": "",
            "suggestion": None, "explanation": f"odd number of double-quote marks ({quote_count}) in this zone",
        })

    return findings
