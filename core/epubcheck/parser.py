"""Parses REAL EPUBCheck --json output into structured results (spec:
"EPUBForge / ZoneTool - Unified EPUB Editor + Validator" section 9:
"capture ... code / severity / message / file / line / column").

Schema confirmed directly against a real epubcheck.jar 4.2.6 run (not
guessed from documentation): {"messages": [{"ID", "severity", "message",
"locations": [{"path", "line", "column", "context"}], "suggestion"}],
"checker": {"checkerVersion", "nFatal", "nError", "nWarning", "nUsage"}}.
A message can carry MULTIPLE locations (e.g. RSC-005 duplicate-ID reports
both places the same ID appears) - this module keeps every location as
its own EpubCheckMessage row (one row per file/line a message points at),
since the validation UI's own results table is naturally one-row-per-
location, not one-row-per-message-with-a-hidden-list.

Deliberately tolerant of unknown/future fields and message codes (spec
10: "Unknown future EPUBCheck error codes must not crash the program") -
this module never matches against a fixed code list; it only reshapes
whatever EPUBCheck itself reported."""
import json
from dataclasses import dataclass, field


@dataclass
class EpubCheckMessage:
    code: str                  # e.g. "RSC-005" - whatever EPUBCheck itself reports, never validated against a fixed list
    severity: str               # "FATAL" | "ERROR" | "WARNING" | "USAGE" | "INFO" - as EPUBCheck itself reports it
    message: str
    file: str = ""
    line: int = -1               # -1 = EPUBCheck itself did not report a line (e.g. a package-level/missing-file error)
    column: int = -1
    context: str = ""
    suggestion: str = ""


@dataclass
class EpubCheckResult:
    ran: bool                   # False if the process itself could not be executed at all (see runner.py)
    error: str = ""              # non-empty only when ran=False - why EPUBCheck could not run at all
    version: str = ""
    is_valid: bool = False        # True only when ran=True and n_fatal == n_error == 0
    n_fatal: int = 0
    n_error: int = 0
    n_warning: int = 0
    n_usage: int = 0
    messages: list = field(default_factory=list)   # [EpubCheckMessage, ...]
    epub_path: str = ""
    elapsed_ms: int = 0
    raw_stdout: str = ""
    raw_stderr: str = ""


def parse_json_output(data: dict, epub_path: str = "") -> EpubCheckResult:
    """`data`: the parsed contents of the --json output file EPUBCheck
    itself wrote (see runner.run_epubcheck). Never raises on a missing/
    unexpected field - a field EPUBCheck doesn't happen to report for a
    given message/version just falls back to a safe default, so a future
    EPUBCheck release adding/removing fields degrades gracefully instead
    of crashing the whole validation run."""
    checker = data.get("checker") or {}
    messages = []
    for m in data.get("messages") or []:
        locations = m.get("locations") or [{}]
        for loc in locations:
            messages.append(EpubCheckMessage(
                code=str(m.get("ID") or "UNKNOWN"),
                severity=str(m.get("severity") or "ERROR").upper(),
                message=str(m.get("message") or ""),
                file=str(loc.get("path") or ""),
                line=int(loc.get("line")) if isinstance(loc.get("line"), (int, float)) else -1,
                column=int(loc.get("column")) if isinstance(loc.get("column"), (int, float)) else -1,
                context=str(loc.get("context") or ""),
                suggestion=str(m.get("suggestion") or ""),
            ))
    n_fatal = int(checker.get("nFatal") or 0)
    n_error = int(checker.get("nError") or 0)
    return EpubCheckResult(
        ran=True,
        version=str(checker.get("checkerVersion") or ""),
        is_valid=(n_fatal == 0 and n_error == 0),
        n_fatal=n_fatal,
        n_error=n_error,
        n_warning=int(checker.get("nWarning") or 0),
        n_usage=int(checker.get("nUsage") or 0),
        messages=messages,
        epub_path=epub_path,
        elapsed_ms=int(checker.get("elapsedTime") or 0),
    )


def parse_json_file(json_path: str, epub_path: str = "") -> EpubCheckResult:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return parse_json_output(data, epub_path=epub_path)
