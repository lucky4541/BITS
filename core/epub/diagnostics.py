"""EPUBForge validation diagnostic helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class ValidationSummary:
    errors: int = 0
    warnings: int = 0
    safe_fixes: int = 0
    review_fixes: int = 0


def classify_diagnostics(diagnostics: Iterable):
    """Classify validator diagnostics without modifying documents."""
    result = []
    for diagnostic in diagnostics:
        kind = getattr(diagnostic, "kind", "") or ""
        if kind in {"missing_closing_tag", "unexpected_closing_tag"}:
            safety = "safe"
        else:
            safety = "review"
        result.append((diagnostic, safety))
    return result
