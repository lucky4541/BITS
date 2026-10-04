"""
EPUBForge XHTML structural repair strategy.

This module is independent from:
    - ZoneManager
    - zoning
    - OCR / PaddleOCR
    - paragraph detection
    - formatting detection
    - XML generation
    - reading order

It detects structural XHTML/XML tag problems directly from the source.

Supported:
    - missing closing tags
    - mismatched closing tags
    - incorrect nesting
    - unexpected closing tags

The module is deliberately conservative:
    - deterministic structural fixes can be marked SAFE
    - ambiguous structures are marked REVIEW
    - no PDF-specific rules
    - no filename-specific rules
    - no page-specific rules
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import re


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XHTML_NS = "http://www.w3.org/1999/xhtml"

CATEGORY = "XHTML_TAG_STRUCTURE"

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

_CLOSE_RE = re.compile(
    r"^<\s*/\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*>$",
    re.DOTALL,
)

_OPEN_RE = re.compile(
    r"^<\s*([A-Za-z_][A-Za-z0-9_.:-]*)"
    r"(?:\s+[^<>]*)?\s*/?\s*>$",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class XHTMLRepair:
    """One XHTML repair proposal."""

    kind: str
    description: str
    confidence: float

    file: str = ""

    # 1-based source position.
    line: int = 0
    column: int = 0

    tag: str = ""

    before: str = ""
    after: str = ""

    # Character offsets in the original source.
    start_offset: Optional[int] = None
    end_offset: Optional[int] = None

    # "safe" or "review"
    safety: str = "safe"

    metadata: dict = field(default_factory=dict)


@dataclass
class XHTMLRepairResult:
    """Result of repairing one XHTML document."""

    applied: bool
    description: str = ""
    files: list = field(default_factory=list)
    repairs: list[XHTMLRepair] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def _local_name(tag) -> str:
    """Return local tag name without XML namespace."""

    if not isinstance(tag, str):
        return ""

    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()

    return tag.lower()


def _is_void(tag: str) -> bool:
    return _local_name(tag) in VOID_TAGS


def _normalise_name(name: str) -> str:
    """
    Normalize a tag name.

    Namespace prefixes are preserved:

        epub:foo -> epub:foo
        svg:rect -> svg:rect
    """

    return (name or "").strip().lower()


def _line_column(source: str, offset: int) -> tuple[int, int]:
    """Return 1-based line and column for a character offset."""

    if offset < 0:
        offset = 0

    line = source.count("\n", 0, offset) + 1

    last_newline = source.rfind("\n", 0, offset)

    if last_newline < 0:
        column = offset + 1
    else:
        column = offset - last_newline

    return line, column


# ---------------------------------------------------------------------------
# Source tokenizer
# ---------------------------------------------------------------------------

def _scan_tags(source: str):
    """
    Tokenize XHTML/XML tags.

    Returns:

        (start, end, kind, name, raw)

    where kind is:

        open
        close
        self

    Comments, CDATA, declarations and processing instructions are ignored.
    """

    tokens = []

    i = 0
    n = len(source)

    while i < n:

        lt = source.find("<", i)

        if lt < 0:
            break

        # ---------------------------------------------------------------
        # Comment
        # ---------------------------------------------------------------

        if source.startswith("<!--", lt):

            end = source.find("-->", lt + 4)

            if end < 0:
                break

            i = end + 3
            continue

        # ---------------------------------------------------------------
        # CDATA
        # ---------------------------------------------------------------

        if source.startswith("<![CDATA[", lt):

            end = source.find("]]>", lt + 9)

            if end < 0:
                break

            i = end + 3
            continue

        # ---------------------------------------------------------------
        # XML processing instruction
        # ---------------------------------------------------------------

        if source.startswith("<?", lt):

            end = source.find("?>", lt + 2)

            if end < 0:
                break

            i = end + 2
            continue

        # ---------------------------------------------------------------
        # DOCTYPE / declaration
        # ---------------------------------------------------------------

        if source.startswith("<!", lt):

            end = source.find(">", lt + 2)

            if end < 0:
                break

            i = end + 1
            continue

        # ---------------------------------------------------------------
        # Find closing >
        #
        # Attribute values may themselves contain > characters.
        # ---------------------------------------------------------------

        j = lt + 1
        quote = None

        while j < n:

            ch = source[j]

            if quote is not None:

                if ch == quote:
                    quote = None

            else:

                if ch in ("'", '"'):
                    quote = ch

                elif ch == ">":
                    break

            j += 1

        if j >= n:
            break

        raw = source[lt:j + 1]

        # ---------------------------------------------------------------
        # Closing tag
        # ---------------------------------------------------------------

        close_match = _CLOSE_RE.match(raw)

        if close_match:

            name = _normalise_name(
                close_match.group(1)
            )

            tokens.append(
                (
                    lt,
                    j + 1,
                    "close",
                    name,
                    raw,
                )
            )

            i = j + 1
            continue

        # ---------------------------------------------------------------
        # Opening / self-closing tag
        # ---------------------------------------------------------------

        open_match = _OPEN_RE.match(raw)

        if open_match:

            name = _normalise_name(
                open_match.group(1)
            )

            self_closing = bool(
                re.search(
                    r"/\s*>$",
                    raw,
                    re.DOTALL,
                )
            )

            tokens.append(
                (
                    lt,
                    j + 1,
                    "self" if self_closing else "open",
                    name,
                    raw,
                )
            )

        i = j + 1

    return tokens


# ---------------------------------------------------------------------------
# Stack helper
# ---------------------------------------------------------------------------

def _find_matching_open(stack, name: str):
    """Find nearest matching opening tag."""

    for index in range(
        len(stack) - 1,
        -1,
        -1,
    ):

        if stack[index]["name"] == name:
            return index

    return None


# ---------------------------------------------------------------------------
# XHTML source analysis
# ---------------------------------------------------------------------------

def analyze_xhtml_source(
    source: str,
    *,
    file: str = "",
) -> list[XHTMLRepair]:
    """
    Analyze raw XHTML source.

    Examples:

        <p>Hello
            -> missing </p>

        <p><b>Hello</p>
            -> missing </b> before </p>

        Hello</p>
            -> unexpected </p>
    """

    if not isinstance(source, str):
        return []

    if not source:
        return []

    repairs: list[XHTMLRepair] = []

    tokens = _scan_tags(source)

    stack = []

    for (
        start,
        end,
        kind,
        name,
        raw,
    ) in tokens:

        if not name:
            continue

        local_name = _local_name(name)

        # ---------------------------------------------------------------
        # Self closing / void element
        # ---------------------------------------------------------------

        if kind == "self" or local_name in VOID_TAGS:
            continue

        # ---------------------------------------------------------------
        # Opening tag
        # ---------------------------------------------------------------

        if kind == "open":

            stack.append(
                {
                    "name": name,
                    "start": start,
                    "end": end,
                    "raw": raw,
                }
            )

            continue

        # ---------------------------------------------------------------
        # Closing tag
        # ---------------------------------------------------------------

        if kind != "close":
            continue

        # ---------------------------------------------------------------
        # Nothing is open.
        # ---------------------------------------------------------------

        if not stack:

            line, column = _line_column(
                source,
                start,
            )

            repairs.append(
                XHTMLRepair(
                    kind="unexpected_closing_tag",
                    description=(
                        f"Unexpected closing tag </{name}>"
                    ),
                    confidence=1.0,
                    file=file,
                    line=line,
                    column=column,
                    tag=name,
                    before=raw,
                    after="",
                    start_offset=start,
                    end_offset=end,
                    safety="safe",
                    metadata={
                        "reason": (
                            "No corresponding opening tag exists "
                            "in the active structural stack."
                        )
                    },
                )
            )

            continue

        # ---------------------------------------------------------------
        # Normal matching close.
        # ---------------------------------------------------------------

        top = stack[-1]

        if top["name"] == name:

            stack.pop()

            continue

        # ---------------------------------------------------------------
        # Closing tag does not match top element.
        # ---------------------------------------------------------------

        matching_index = _find_matching_open(
            stack,
            name,
        )

        # ---------------------------------------------------------------
        # No matching opening tag anywhere.
        # ---------------------------------------------------------------

        if matching_index is None:

            line, column = _line_column(
                source,
                start,
            )

            repairs.append(
                XHTMLRepair(
                    kind="unexpected_closing_tag",
                    description=(
                        f"Unexpected closing tag </{name}>"
                    ),
                    confidence=1.0,
                    file=file,
                    line=line,
                    column=column,
                    tag=name,
                    before=raw,
                    after="",
                    start_offset=start,
                    end_offset=end,
                    safety="safe",
                    metadata={
                        "reason": (
                            "No matching opening tag exists "
                            "in the active structural stack."
                        )
                    },
                )
            )

            continue

        # ---------------------------------------------------------------
        # Matching opening tag exists lower in the stack.
        #
        # Example:
        #
        # <p>
        #     <b>
        #         text
        # </p>
        #
        # Correct structural repair:
        #
        # <p>
        #     <b>
        #         text
        #     </b>
        # </p>
        # ---------------------------------------------------------------

        above = stack[
            matching_index + 1:
        ]

        safe = True

        for entry in above:

            if _is_void(entry["name"]):

                safe = False
                break

        if not safe:
            continue

        missing_closings = "".join(
            f"</{entry['name']}>"
            for entry in reversed(above)
        )

        line, column = _line_column(
            source,
            start,
        )

        repairs.append(
            XHTMLRepair(
                kind="mismatched_nesting",
                description=(
                    f"Missing closing tag(s) "
                    f"{missing_closings} before </{name}>"
                ),
                confidence=1.0,
                file=file,
                line=line,
                column=column,
                tag=name,
                before=raw,
                after=missing_closings + raw,
                start_offset=start,
                end_offset=end,
                safety="safe",
                metadata={
                    "expected_closings": [
                        entry["name"]
                        for entry in reversed(above)
                    ],
                    "closing_before": name,
                },
            )
        )

        # All elements above the matching tag are now considered closed,
        # as is the matching tag itself.
        stack = stack[
            :matching_index
        ]

    # -------------------------------------------------------------------
    # Remaining unclosed tags
    # -------------------------------------------------------------------

    if len(stack) == 1:

        entry = stack[0]

        tag = entry["name"]

        if not _is_void(tag):

            line, column = _line_column(
                source,
                entry["start"],
            )

            repairs.append(
                XHTMLRepair(
                    kind="missing_closing_tag",
                    description=(
                        f"Missing closing tag </{tag}>"
                    ),
                    confidence=1.0,
                    file=file,
                    line=line,
                    column=column,
                    tag=tag,
                    before=entry["raw"],
                    after=entry["raw"] + f"</{tag}>",
                    start_offset=len(source),
                    end_offset=len(source),
                    safety="safe",
                    metadata={
                        "append_at_end": True,
                    },
                )
            )

    elif len(stack) > 1:

        # Multiple unresolved tags are deliberately REVIEW.
        #
        # We do NOT guess where their closing tags belong.
        for entry in stack:

            tag = entry["name"]

            if _is_void(tag):
                continue

            line, column = _line_column(
                source,
                entry["start"],
            )

            repairs.append(
                XHTMLRepair(
                    kind="unclosed_tag_review",
                    description=(
                        f"Unclosed tag <{tag}> requires review"
                    ),
                    confidence=0.70,
                    file=file,
                    line=line,
                    column=column,
                    tag=tag,
                    safety="review",
                    metadata={
                        "reason": (
                            "Multiple structural elements remain open. "
                            "Automatic placement of closing tags could "
                            "change document structure."
                        )
                    },
                )
            )

    return repairs


# ---------------------------------------------------------------------------
# Apply repairs
# ---------------------------------------------------------------------------

def apply_repairs(
    source: str,
    repairs: list[XHTMLRepair],
) -> tuple[str, list[XHTMLRepair]]:
    """
    Apply only SAFE repairs.

    Repairs are processed from the end of the source toward the beginning.
    """

    if not source:
        return source, []

    if not repairs:
        return source, []

    safe_repairs = [
        repair
        for repair in repairs
        if repair.safety == "safe"
    ]

    if not safe_repairs:
        return source, []

    current = source

    applied: list[XHTMLRepair] = []

    # ---------------------------------------------------------------
    # Special case: append-at-end repairs.
    # ---------------------------------------------------------------

    append_repairs = [
        repair
        for repair in safe_repairs
        if repair.metadata.get("append_at_end")
    ]

    normal_repairs = [
        repair
        for repair in safe_repairs
        if not repair.metadata.get("append_at_end")
    ]

    # ---------------------------------------------------------------
    # Apply normal edits from right to left.
    # ---------------------------------------------------------------

    normal_repairs.sort(
        key=lambda repair: (
            int(repair.start_offset or 0),
            int(repair.end_offset or 0),
        ),
        reverse=True,
    )

    occupied_ranges = []

    for repair in normal_repairs:

        if (
            repair.start_offset is None
            or repair.end_offset is None
        ):
            continue

        start = int(repair.start_offset)
        end = int(repair.end_offset)

        if start < 0:
            continue

        if end < start:
            continue

        if end > len(current):
            continue

        # Prevent overlapping edits.
        overlap = False

        for old_start, old_end in occupied_ranges:

            if not (
                end <= old_start
                or start >= old_end
            ):
                overlap = True
                break

        if overlap:
            continue

        if repair.kind == "mismatched_nesting":

            replacement = repair.after

        elif repair.kind == "unexpected_closing_tag":

            replacement = ""

        elif repair.kind == "missing_closing_tag":

            replacement = f"</{repair.tag}>"

        else:
            continue

        current = (
            current[:start]
            + replacement
            + current[end:]
        )

        occupied_ranges.append(
            (start, end)
        )

        applied.append(repair)

    # ---------------------------------------------------------------
    # Append safe remaining closing tags.
    #
    # These are only generated when exactly one tag remains open.
    # ---------------------------------------------------------------

    for repair in append_repairs:

        if not repair.tag:
            continue

        closing = f"</{repair.tag}>"

        if not current.endswith(closing):

            current += closing

            applied.append(repair)

    applied.reverse()

    return current, applied


# ---------------------------------------------------------------------------
# Single-source repair
# ---------------------------------------------------------------------------

def repair_xhtml_source(
    source: str,
    *,
    file: str = "",
) -> tuple[str, XHTMLRepairResult]:
    """
    Analyze and repair one XHTML source string.
    """

    repairs = analyze_xhtml_source(
        source,
        file=file,
    )

    safe_repairs = [
        repair
        for repair in repairs
        if repair.safety == "safe"
    ]

    if not safe_repairs:

        return (
            source,
            XHTMLRepairResult(
                applied=False,
                description=(
                    "No safe XHTML structural repair available."
                ),
                files=[],
                repairs=repairs,
            ),
        )

    repaired, applied = apply_repairs(
        source,
        safe_repairs,
    )

    if repaired == source:

        return (
            source,
            XHTMLRepairResult(
                applied=False,
                description=(
                    "No XHTML repair was applied."
                ),
                files=[],
                repairs=repairs,
            ),
        )

    descriptions = "; ".join(
        repair.description
        for repair in applied
    )

    return (
        repaired,
        XHTMLRepairResult(
            applied=True,
            description=descriptions,
            files=[file] if file else [],
            repairs=repairs,
        ),
    )


# ---------------------------------------------------------------------------
# Existing EPUBForge RepairEngine adapter
# ---------------------------------------------------------------------------

def fix_xhtml_document(
    mp,
    package,
):
    """
    Adapter used by the existing RepairEngine / StrategyRegistry.

    IMPORTANT:
    RepairActionResult is imported HERE, not at module import time.

    This prevents:

        repair_strategies
            -> xhtml_repair_strategy
                -> repair_strategies

    circular import.
    """

    # ---------------------------------------------------------------
    # IMPORTANT:
    # Keep this import inside the function.
    # ---------------------------------------------------------------

    from core.epub.repair_strategies import RepairActionResult

    changed_files = []
    descriptions = []

    # ---------------------------------------------------------------
    # Iterate through manifest.
    # ---------------------------------------------------------------

    for item in package.manifest:

        if item.media_type != "application/xhtml+xml":
            continue

        if not item.href:
            continue

        # Resolve the manifest href using the existing package API.
        try:
            name = package.resolve_href(item.href)
        except Exception:
            continue

        if not name:
            continue

        try:
            exists = mp.exists(name)
        except Exception:
            exists = False

        if not exists:
            continue

        # -----------------------------------------------------------
        # Read original bytes.
        #
        # We intentionally work on the original text rather than
        # serializing an XML tree. This avoids unnecessary changes to:
        #
        #   whitespace
        #   attributes
        #   entities
        #   formatting
        #   existing XHTML
        # -----------------------------------------------------------

        try:
            raw = mp.get_bytes(name)
        except Exception:
            continue

        # -----------------------------------------------------------
        # UTF-8 is required for normal EPUB XHTML.
        # -----------------------------------------------------------

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Never guess an encoding automatically.
            continue

        # -----------------------------------------------------------
        # Analyze + repair.
        # -----------------------------------------------------------

        repaired, result = repair_xhtml_source(
            source,
            file=name,
        )

        if not result.applied:
            continue

        if repaired == source:
            continue

        # -----------------------------------------------------------
        # Ensure repaired content can be encoded as UTF-8.
        # -----------------------------------------------------------

        try:
            repaired_bytes = repaired.encode("utf-8")
        except UnicodeEncodeError:
            continue

        # -----------------------------------------------------------
        # Write only changed XHTML.
        # -----------------------------------------------------------

        try:
            mp.set_bytes(
                name,
                repaired_bytes,
            )
        except Exception:
            continue

        changed_files.append(name)

        descriptions.append(
            f"{name}: {result.description}"
        )

    # ---------------------------------------------------------------
    # Return the SAME result type used by the existing strategy system.
    # ---------------------------------------------------------------

    return RepairActionResult(
        applied=bool(changed_files),
        description="; ".join(descriptions),
        files=changed_files,
    )


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------

def register(registry) -> None:
    """
    Register this strategy with EPUBForge's existing registry.

    Normally repair_strategies.py will perform the registration directly.
    """

    registry.register(
        CATEGORY,
        fix_xhtml_document,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CATEGORY",
    "XHTMLRepair",
    "XHTMLRepairResult",
    "analyze_xhtml_source",
    "apply_repairs",
    "repair_xhtml_source",
    "fix_xhtml_document",
    "register",
]