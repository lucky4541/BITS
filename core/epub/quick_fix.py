"""EPUBForge safe Quick Fix engine.

This module is deliberately independent of the GUI, zoning, OCR, and XML
generation code.  It works on text buffers and validation diagnostics.

Only deterministic repairs are classified SAFE.  Ambiguous repairs are
returned as REVIEW and are never silently applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Optional


SAFE = "safe"
REVIEW = "review"
UNSUPPORTED = "unsupported"


@dataclass
class Diagnostic:
    file: str
    line: int
    column: int
    code: str
    message: str
    kind: str = ""
    severity: str = "error"
    expected_tag: Optional[str] = None
    found_tag: Optional[str] = None


@dataclass
class FixProposal:
    title: str
    kind: str
    safety: str
    confidence: float
    file: str
    line: int
    column: int
    before: str
    after: str
    replacement_start: int
    replacement_end: int
    reason: str
    diagnostic: Optional[Diagnostic] = None


@dataclass
class FixResult:
    applied: bool
    text: str
    proposal: FixProposal
    message: str


class QuickFixEngine:
    """Generate and apply only provably safe text repairs."""

    _TAG_RE = re.compile(
        r"<\s*(/?)\s*([A-Za-z_][\w:.-]*)"
        r"(?:\s+[^<>]*?)?\s*(/?)\s*>",
        re.DOTALL,
    )

    def proposals(self, text: str, diagnostic: Diagnostic) -> list[FixProposal]:
        kind = (diagnostic.kind or "").lower()

        if kind in {"missing_closing_tag", "missing-close-tag"}:
            proposal = self._missing_closing_tag(text, diagnostic)
            return [proposal] if proposal else []

        if kind in {"unexpected_closing_tag", "unexpected-close-tag"}:
            proposal = self._unexpected_closing_tag(text, diagnostic)
            return [proposal] if proposal else []

        return []

    def _missing_closing_tag(
        self, text: str, diagnostic: Diagnostic
    ) -> Optional[FixProposal]:
        tag = diagnostic.expected_tag

        if not tag:
            match = re.search(
                r"missing\s+closing\s+tag\s*</\s*([A-Za-z_][\w:.-]*)\s*>",
                diagnostic.message,
                re.I,
            )
            if match:
                tag = match.group(1)

        if not tag:
            return None

        # Insert immediately before the reported offending closing tag, if
        # the diagnostic identifies one. Otherwise insert at the line end.
        pos = self._diagnostic_offset(text, diagnostic)
        if pos is None:
            return None

        line_start = text.rfind("\n", 0, pos) + 1
        line_end = text.find("\n", pos)
        if line_end < 0:
            line_end = len(text)

        closing = f"</{tag}>"

        # If the reported position already points at a closing tag, insert
        # before that tag. This is the safest case.
        match = re.match(r"</\s*[A-Za-z_][\w:.-]*\s*>", text[pos:])
        if match:
            insert_at = pos
        else:
            insert_at = line_end

        before = text[max(line_start, insert_at - 80):insert_at]
        after = before + closing

        return FixProposal(
            title=f"Add missing </{tag}>",
            kind="missing_closing_tag",
            safety=SAFE,
            confidence=1.0,
            file=diagnostic.file,
            line=diagnostic.line,
            column=diagnostic.column,
            before=before,
            after=after,
            replacement_start=insert_at,
            replacement_end=insert_at,
            reason=f"The validator identified </{tag}> as the required closing tag.",
            diagnostic=diagnostic,
        )

    def _unexpected_closing_tag(
        self, text: str, diagnostic: Diagnostic
    ) -> Optional[FixProposal]:
        # Removing an unexpected closing tag is safe only when the diagnostic
        # explicitly identifies it and it has no matching open tag in the
        # active structural context. We therefore require found_tag.
        tag = diagnostic.found_tag
        if not tag:
            match = re.search(
                r"unexpected\s+closing\s+tag\s*</\s*([A-Za-z_][\w:.-]*)\s*>",
                diagnostic.message,
                re.I,
            )
            if match:
                tag = match.group(1)

        if not tag:
            return None

        pos = self._diagnostic_offset(text, diagnostic)
        if pos is None:
            return None

        pattern = re.compile(
            rf"</\s*{re.escape(tag)}\s*>",
            re.I,
        )
        match = pattern.search(text, pos)
        if not match:
            return None

        # Verify that the diagnostic position is close to the actual tag.
        line = text.count("\n", 0, match.start()) + 1
        if abs(line - diagnostic.line) > 1:
            return None

        before = text[max(0, match.start() - 80):match.end()]
        after = text[max(0, match.start() - 80):match.start()]

        return FixProposal(
            title=f"Remove unexpected </{tag}>",
            kind="unexpected_closing_tag",
            safety=SAFE,
            confidence=1.0,
            file=diagnostic.file,
            line=diagnostic.line,
            column=diagnostic.column,
            before=before,
            after=after,
            replacement_start=match.start(),
            replacement_end=match.end(),
            reason=f"The validator explicitly reported </{tag}> as unexpected.",
            diagnostic=diagnostic,
        )

    @staticmethod
    def _diagnostic_offset(text: str, diagnostic: Diagnostic) -> Optional[int]:
        if diagnostic.line < 1 or diagnostic.column < 1:
            return None

        lines = text.splitlines(keepends=True)
        if diagnostic.line > len(lines):
            return None

        offset = sum(len(line) for line in lines[: diagnostic.line - 1])
        line_text = lines[diagnostic.line - 1]

        # Diagnostics conventionally use 1-based columns.
        offset += min(diagnostic.column - 1, len(line_text))
        return offset

    def preview(self, text: str, proposal: FixProposal) -> str:
        if not (0 <= proposal.replacement_start <= proposal.replacement_end <= len(text)):
            raise ValueError("Invalid fix range.")

        return (
            text[: proposal.replacement_start]
            + (
                proposal.after[len(proposal.before):]
                if proposal.replacement_end == proposal.replacement_start
                else ""
            )
            + text[proposal.replacement_end:]
        )

    def apply(self, text: str, proposal: FixProposal) -> FixResult:
        if proposal.safety != SAFE:
            return FixResult(
                False, text, proposal,
                "This repair is not marked SAFE and was not applied.",
            )

        if not (
            0 <= proposal.replacement_start
            <= proposal.replacement_end
            <= len(text)
        ):
            return FixResult(False, text, proposal, "Invalid repair range.")

        if proposal.kind == "missing_closing_tag":
            replacement = proposal.after[len(proposal.before):]
        elif proposal.kind == "unexpected_closing_tag":
            replacement = ""
        else:
            return FixResult(False, text, proposal, "Unsupported repair type.")

        new_text = (
            text[:proposal.replacement_start]
            + replacement
            + text[proposal.replacement_end:]
        )

        return FixResult(
            True,
            new_text,
            proposal,
            f"Applied: {proposal.title}",
        )
