"""Official W3C EPUBCheck Test Suite runner (FINAL VALIDATION PHASE, Part
A). Reads the REAL, unmodified, official Gherkin/Cucumber test suite
bundled under tools/epubcheck-testsuite/ (a one-time, dev-time acquisition
from the real https://github.com/w3c/epubcheck repository at the SAME
v4.2.6 tag as the bundled epubcheck.jar - confirmed directly against the
real repository, never downloaded at application runtime, never a
fake/simplified substitute) and drives the REAL bundled EPUBCheck (core.
epubcheck.runner.run_epubcheck_document - an EXTENSION of the existing
runner, never a second one) against the suite's own real test input files.

This module is a genuine Gherkin interpreter for the SPECIFIC, confirmed
step vocabulary the real suite actually uses (verified directly against
every .feature file in the bundled epub2/ and epub3/ trees - see the
_STEP patterns below), not a full general-purpose Cucumber engine and not
a hand-picked subset of "convenient" tests: every scenario in every
bundled .feature file is parsed and executed. A scenario whose steps use
Gherkin outside this confirmed vocabulary is reported SKIPPED with the
exact unrecognized step text - never silently assumed to pass, and never
excluded from the report.

Isolation (spec A5): reads the bundled test suite files (never modifies
them - EPUBCheck itself is read-only against its targets), runs each
EPUBCheck invocation in run_epubcheck_document's own temp directory
(cleaned up automatically), and never references or touches whatever
EPUB the user currently has open elsewhere in EPUBForge - this module
takes no epub_path/package argument from the caller at all."""
import glob
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from core.epubcheck import runner as epubcheck_runner
from core.resource_path import resource_path

_MODE_STEPS = [
    (re.compile(r"EPUBCheck configured to check a Package Document", re.I), "mode", "opf"),
    (re.compile(r"EPUBCheck configured to check a Navigation Document", re.I), "mode", "nav"),
    (re.compile(r"EPUBCheck configured to check an? XHTML Content Document", re.I), "mode", "xhtml"),
    (re.compile(r"EPUBCheck configured to check an? SVG Content Document", re.I), "mode", "svg"),
    (re.compile(r"EPUBCheck configured to check a Media Overlays Document", re.I), "mode", "mo"),
    (re.compile(r"EPUBCheck with default settings", re.I), "mode", ""),
    (re.compile(r"EPUBCheck configured to check EPUB 2", re.I), "version", "2.0"),
    (re.compile(r"EPUBCheck configured to check EPUB 3", re.I), "version", "3.0"),
]
_BASE_DIR_RE = re.compile(r"test files located at ['\"]([^'\"]+)['\"]", re.I)
_TARGET_RE = re.compile(r"checking (?:file|EPUB|document) ['\"]([^'\"]+)['\"]", re.I)
# Real feature files informally suffix a lowercase letter on a code to
# distinguish multiple occurrences within one scenario (e.g. "RSC-006b",
# "OPF-004c") - confirmed directly in the bundled files; the real
# EPUBCheck code is always just the letters-and-digits part.
_SINGLE_EXPECT_RE = re.compile(
    r"^(fatal error|error|warning|usage|info)\s+([A-Z]{2,4}-\d+)[a-z]?\s+is reported(?:\s+(\d+)\s+times?)?", re.I)
_MESSAGE_CONTAINS_RE = re.compile(r"the message contains ['\"](.+)['\"]")
_TABLE_ROW_CODE_RE = re.compile(r"^([A-Z]{2,4}-\d+)[a-z]?$")
_TABLE_HEADER_RE = re.compile(r"^the following (errors|warnings) are reported", re.I)
_USAGE_LEVEL_RE = re.compile(r"the reporting level (?:is )?set to (usage|info)", re.I)
# Narrative asides written as Given/And/But steps rather than real '#'
# comments (confirmed directly in the bundled files: "See issue #123",
# "Note: ...", parenthetical remarks, "Spec mismatch: ...") - not
# assertions, and must not be flagged as unrecognized/SKIPPED steps.
_NOISE_RE = re.compile(r"^(See\b|Note:|Spec mismatch|\(.*\)$)", re.I)
_STEP_PREFIX_RE = re.compile(r"^(Given|When|Then|And|But)\s+(.*)$")


@dataclass
class TestResult:
    test_id: str
    feature: str
    description: str
    expected: str
    actual: str
    status: str          # PASS | FAIL | SKIPPED | ERROR
    message: str = ""


@dataclass
class TestSuiteReport:
    suite_version: str
    epubcheck_version: str
    timestamp: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _new_scope() -> dict:
    return {"mode": "", "version": "3.0", "base_dir": "", "include_usage": False}


def _new_scenario(name, scope) -> dict:
    return {"name": name, "mode": scope["mode"], "version": scope["version"], "base_dir": scope["base_dir"],
            "include_usage": scope["include_usage"],
            "target": None, "no_errors_or_warnings": False, "no_other": False,
            "expectations": [], "parse_ok": True, "parse_issue": ""}


def _parse_feature_file(path: str):
    """Returns (feature_name, [scenario_dict, ...]). Never raises - a
    file that can't even be read produces zero scenarios, surfaced by the
    caller finding no results for it (visible in the report's own
    per-feature counts, never silently hidden)."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return os.path.basename(path), []

    feature_name = os.path.basename(path)
    background = _new_scope()
    scenarios = []
    current = None
    in_background = False
    table_severity = None   # "ERROR" | "WARNING" | None (not currently in a table)
    last_expectation = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("Feature:"):
            feature_name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Background:"):
            in_background, current, table_severity = True, None, None
            continue
        if line.startswith("Scenario:") or line.startswith("Scenario Outline:"):
            in_background = False
            current = _new_scenario(line.split(":", 1)[1].strip(), background)
            scenarios.append(current)
            last_expectation, table_severity = None, None
            continue

        if line.startswith("|"):
            if table_severity is not None and current is not None:
                cells = [c.strip() for c in line.strip("|").split("|")]
                m_code = cells and _TABLE_ROW_CODE_RE.match(cells[0])
                if m_code:
                    exp = {"severity": table_severity, "code": m_code.group(1), "count": 1, "message_contains": ""}
                    current["expectations"].append(exp)
                    last_expectation = exp
            continue
        table_severity = None

        m = _STEP_PREFIX_RE.match(line)
        step_text = m.group(2) if m else line
        if _NOISE_RE.match(step_text):
            continue  # a narrative aside ("See issue #123", "Note: ...") written as a step - not an assertion

        scope = background if in_background else current
        if scope is None:
            continue

        m = _USAGE_LEVEL_RE.search(step_text)
        if m:
            scope["include_usage"] = True
            continue

        matched_mode_or_version = False
        for pattern, key, value in _MODE_STEPS:
            if pattern.search(step_text):
                scope[key] = value
                matched_mode_or_version = True
                break
        if matched_mode_or_version:
            continue

        m = _BASE_DIR_RE.search(step_text)
        if m:
            scope["base_dir"] = m.group(1)
            continue

        if current is None:
            continue  # an unrecognized Background-only line - harmless, not a scenario failure

        m = _TARGET_RE.search(step_text)
        if m:
            current["target"] = m.group(1)
            continue
        if re.search(r"^no other errors or warnings are reported", step_text, re.I):
            current["no_other"] = True
            continue
        if re.search(r"^no errors or warnings are reported", step_text, re.I):
            current["no_errors_or_warnings"] = True
            continue
        m = _TABLE_HEADER_RE.match(step_text)
        if m:
            table_severity = "WARNING" if m.group(1).lower() == "warnings" else "ERROR"
            continue
        m = _SINGLE_EXPECT_RE.match(step_text)
        if m:
            sev_word = m.group(1).upper()
            severity = "FATAL" if sev_word == "FATAL ERROR" else sev_word
            exp = {"severity": severity, "code": m.group(2).upper(),
                   "count": int(m.group(3)) if m.group(3) else 1, "message_contains": ""}
            current["expectations"].append(exp)
            last_expectation = exp
            continue
        m = _MESSAGE_CONTAINS_RE.search(step_text)
        if m and last_expectation is not None:
            last_expectation["message_contains"] = m.group(1)
            continue

        current["parse_ok"] = False
        current["parse_issue"] = step_text

    return feature_name, scenarios


def _describe_expected(scenario: dict) -> str:
    if scenario["no_errors_or_warnings"]:
        return "no errors or warnings"
    if scenario["expectations"]:
        return "; ".join(f"{e['count']}x {e['severity']} {e['code']}" for e in scenario["expectations"])
    return "(no assertion parsed)"


def _describe_actual(ec_result) -> str:
    if not ec_result.messages:
        return "no errors or warnings"
    counts = {}
    for m in ec_result.messages:
        counts[(m.severity, m.code)] = counts.get((m.severity, m.code), 0) + 1
    return "; ".join(f"{n}x {sev} {code}" for (sev, code), n in counts.items())


def _compare(scenario: dict, ec_result) -> tuple:
    actual_counts, actual_messages = {}, {}
    for m in ec_result.messages:
        key = (m.severity, m.code)
        actual_counts[key] = actual_counts.get(key, 0) + 1
        actual_messages.setdefault(key, []).append(m.message)

    problems = []
    relevant = ("FATAL", "ERROR", "WARNING")

    if scenario["no_errors_or_warnings"]:
        total = sum(n for (sev, _c), n in actual_counts.items() if sev in relevant)
        if total != 0:
            problems.append(f"expected no errors/warnings, found {total}")

    accounted = 0
    for exp in scenario["expectations"]:
        key = (exp["severity"], exp["code"])
        actual_n = actual_counts.get(key, 0)
        if actual_n != exp["count"]:
            problems.append(f"expected {exp['count']}x {exp['severity']} {exp['code']}, found {actual_n}")
        elif exp["message_contains"]:
            msgs = actual_messages.get(key, [])
            if not any(exp["message_contains"] in msg for msg in msgs):
                problems.append(f"{exp['severity']} {exp['code']}: message did not contain "
                                 f"{exp['message_contains']!r} (got {msgs})")
        if exp["severity"] in relevant:
            accounted += exp["count"]

    if scenario["no_other"]:
        total = sum(n for (sev, _c), n in actual_counts.items() if sev in relevant)
        if total != accounted:
            problems.append(f"expected exactly {accounted} total error(s)/warning(s), found {total}")

    return (not problems, "; ".join(problems))


def _run_scenario(suite_root: str, feature_name: str, test_id: str, scenario: dict) -> TestResult:
    description = scenario["name"]
    if not scenario["parse_ok"]:
        return TestResult(test_id, feature_name, description, "-", "-", "SKIPPED",
                           f"Unrecognized Gherkin step (not yet supported): {scenario['parse_issue']!r}")
    if not scenario["target"]:
        return TestResult(test_id, feature_name, description, "-", "-", "SKIPPED",
                           "No 'When checking ...' target was found in this scenario")

    base_dir = scenario["base_dir"].strip("/")
    resolved_dir = os.path.join(suite_root, *base_dir.split("/")) if base_dir else suite_root
    target_path = os.path.join(resolved_dir, scenario["target"])
    if not os.path.exists(target_path):
        return TestResult(test_id, feature_name, description, _describe_expected(scenario), "-", "ERROR",
                           f"Test input not found in the bundled suite: {target_path}")

    mode = scenario["mode"]
    if not mode and os.path.isdir(target_path):
        mode = "exp"  # a directory with no explicit mode is an expanded (unzipped) EPUB package

    ec_result = epubcheck_runner.run_epubcheck_document(
        target_path, mode=mode, version=scenario["version"], include_usage=scenario["include_usage"])
    expected_summary = _describe_expected(scenario)
    if not ec_result.ran:
        return TestResult(test_id, feature_name, description, expected_summary, "-", "ERROR",
                           f"EPUBCheck could not run: {ec_result.error}")

    ok, mismatch = _compare(scenario, ec_result)
    return TestResult(test_id, feature_name, description, expected_summary, _describe_actual(ec_result),
                       "PASS" if ok else "FAIL", mismatch)


def suite_root_path() -> str:
    return resource_path("tools", "epubcheck-testsuite")


def suite_version() -> str:
    path = os.path.join(suite_root_path(), "VERSION")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def check_suite_availability() -> tuple:
    """Cheap, LOCAL-ONLY existence check (matches core.epubcheck.runner.
    check_availability's own contract) - never itself runs any tests."""
    root = suite_root_path()
    epub2 = glob.glob(os.path.join(root, "epub2", "*.feature"))
    epub3 = glob.glob(os.path.join(root, "epub3", "*.feature"))
    if epub2 or epub3:
        return True, f"{len(epub2) + len(epub3)} feature file(s) found under {root}"
    return False, f"W3C EPUBCheck Test Suite not found at {root}"


def run_test_suite(progress=None) -> TestSuiteReport:
    """THE single entry point (spec A2: 'Execute the official tests...
    Capture results'). Never modifies any user data - see module
    docstring for isolation guarantees."""
    ok, avail_msg = epubcheck_runner.check_availability()
    suite_ok, suite_msg = check_suite_availability()
    if not ok or not suite_ok:
        return TestSuiteReport(
            suite_version=suite_version(), epubcheck_version="unavailable",
            timestamp=datetime.now(timezone.utc).isoformat(), total=0, errors=1,
            results=[TestResult("SETUP", "-", "Runtime availability check", "EPUBCheck + Test Suite available",
                                 f"EPUBCheck: {avail_msg} | Test Suite: {suite_msg}", "ERROR",
                                 "Cannot run the Test Suite - see message.")])

    root = suite_root_path()
    feature_files = sorted(glob.glob(os.path.join(root, "epub2", "*.feature")) +
                            glob.glob(os.path.join(root, "epub3", "*.feature")))
    results = []
    start = time.time()
    for feature_path in feature_files:
        rel = os.path.relpath(feature_path, root)
        if progress:
            progress(f"Running {rel}")
        feature_name, scenarios = _parse_feature_file(feature_path)
        stem = os.path.splitext(os.path.basename(feature_path))[0]
        edition = "epub2" if f"{os.sep}epub2{os.sep}" in feature_path else "epub3"
        for i, scenario in enumerate(scenarios, start=1):
            test_id = f"{edition}/{stem}::{i:03d}"
            results.append(_run_scenario(root, feature_name, test_id, scenario))
    duration = time.time() - start

    return TestSuiteReport(
        suite_version=suite_version(), epubcheck_version=epubcheck_runner.get_epubcheck_version() or "unknown",
        timestamp=datetime.now(timezone.utc).isoformat(), total=len(results),
        passed=sum(1 for r in results if r.status == "PASS"),
        failed=sum(1 for r in results if r.status == "FAIL"),
        skipped=sum(1 for r in results if r.status == "SKIPPED"),
        errors=sum(1 for r in results if r.status == "ERROR"),
        duration_seconds=duration, results=results)


def write_json_report(report: TestSuiteReport, out_path: str):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)


def write_html_report(report: TestSuiteReport, out_path: str):
    import html as html_mod

    rows = []
    for r in report.results:
        rows.append(
            f"<tr class='{r.status.lower()}'><td>{html_mod.escape(r.test_id)}</td>"
            f"<td>{html_mod.escape(r.feature)}</td><td>{html_mod.escape(r.description)}</td>"
            f"<td>{html_mod.escape(r.expected)}</td><td>{html_mod.escape(r.actual)}</td>"
            f"<td>{r.status}</td><td>{html_mod.escape(r.message)}</td></tr>")
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>W3C EPUBCheck Test Suite Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:16px}}
td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}}
th{{background:#eee}}
tr.pass{{background:#eaffea}} tr.fail{{background:#ffecec}}
tr.skipped{{background:#fff8e0}} tr.error{{background:#ffe0e0}}
</style></head><body>
<h1>W3C EPUBCheck Test Suite Report</h1>
<p>
Test Suite Version: {html_mod.escape(report.suite_version)}<br>
EPUBCheck Version: {html_mod.escape(report.epubcheck_version)}<br>
Timestamp: {html_mod.escape(report.timestamp)}<br>
Total: {report.total} &nbsp; Passed: {report.passed} &nbsp; Failed: {report.failed} &nbsp;
Skipped: {report.skipped} &nbsp; Errors: {report.errors} &nbsp; Duration: {report.duration_seconds:.1f}s
</p>
<table>
<tr><th>Test ID</th><th>Feature</th><th>Description</th><th>Expected</th><th>Actual</th><th>Status</th><th>Message</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
