"""Locates and runs the REAL, bundled W3C EPUBCheck jar (spec: "EPUBForge /
ZoneTool - Unified EPUB Editor + Validator" sections 9/46.1/46.8 - "Use the
REAL official W3C EPUBCheck implementation... DO NOT simulate EPUBCheck").

Resolution goes through core.resource_path (spec 46.14: "Use the existing
resource_path.py... Do NOT use developer-specific paths"), never a
hardcoded absolute path - resolves correctly in both dev mode (python
main.py, reading tools/epubcheck/epubcheck.jar from the project root) and
a future frozen build (reading the same relative location bundled beside
the EXE).

Java resolution: a FUTURE bundled JRE (resource_path("runtime", "java",
"bin", "java.exe") - spec 46.4) is checked FIRST and always wins when
present; the current build does not yet bundle one (production packaging
of an offline JRE is tracked separately - see this module's own README
note below), so this falls back to whatever "java" the system itself
already provides (PATH, then JAVA_HOME) - the ONLY thing that makes real,
working EPUBCheck validation possible today without a bundled runtime yet
in place. Once a bundled JRE is added to packaging, this module needs NO
change at all - the bundled-path check already takes priority.

NEVER downloads Java or EPUBCheck at runtime (spec 46.7) - a missing
component is reported as a clear, actionable unavailability reason, never
silently worked around over the network."""
import os
import shutil
import subprocess
import tempfile

from core.resource_path import resource_path
from core.epubcheck import parser

EPUBCHECK_TIMEOUT_SECONDS = 180

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # never flash a console child window from a --windowed app


def find_bundled_java() -> str:
    exe = "java.exe" if os.name == "nt" else "java"
    candidate = resource_path("runtime", "java", "bin", exe)
    return candidate if os.path.isfile(candidate) else ""


def find_system_java() -> str:
    found = shutil.which("java")
    if found:
        return found
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = "java.exe" if os.name == "nt" else "java"
        candidate = os.path.join(java_home, "bin", exe)
        if os.path.isfile(candidate):
            return candidate
    return ""


def find_java() -> str:
    return find_bundled_java() or find_system_java()


def find_epubcheck_jar() -> str:
    candidate = resource_path("tools", "epubcheck", "epubcheck.jar")
    return candidate if os.path.isfile(candidate) else ""


def check_availability() -> tuple:
    """Returns (available: bool, message: str) - a cheap, LOCAL-ONLY
    existence check (spec 46.21: "At startup perform only a lightweight
    local availability check... Do NOT run EPUBCheck"), never itself
    invoking Java/EPUBCheck."""
    java = find_java()
    jar = find_epubcheck_jar()
    if java and jar:
        return True, f"Java: {java}\nEPUBCheck: {jar}"
    missing = []
    if not java:
        missing.append("Java runtime")
    if not jar:
        missing.append("EPUBCheck (tools/epubcheck/epubcheck.jar)")
    return False, "Not available - missing: " + ", ".join(missing)


def get_epubcheck_version() -> str:
    """Runs `java -jar epubcheck.jar --version` once - the ONLY EPUBCheck
    invocation that isn't a real file validation, used purely for the
    version-reporting requirement (spec 46.20). Returns "" (never raises)
    if Java/EPUBCheck aren't available or the call fails for any reason.

    Confirmed directly against a real epubcheck.jar 4.2.6: --version still
    expects a file argument and prints extra noise alongside the real
    version ("EPUBCheck v4.2.6\\nNo file specified in the arguments.
    Exiting.\\nEPUBCheck completed") - only the FIRST line ("EPUBCheck
    vX.Y.Z") is ever the actual version, so that's the only line returned;
    the rest is discarded rather than shown to the user as if it were part
    of the version string."""
    java, jar = find_java(), find_epubcheck_jar()
    if not java or not jar:
        return ""
    try:
        proc = subprocess.run([java, "-jar", jar, "--version"], capture_output=True, text=True,
                               timeout=30, creationflags=_CREATE_NO_WINDOW)
        first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        return first_line[0].strip() if first_line else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def get_java_version() -> str:
    """FINAL VALIDATION PHASE (Part K: 'Display locally... Java Runtime
    Version... Do NOT retrieve version information from the internet at
    runtime') - runs `java -version` once against whichever java find_java()
    resolves (the bundled runtime/java JRE when present, per its own
    priority order). Returns "" (never raises) if no java is available at
    all. `java -version` prints to STDERR, not stdout - confirmed directly
    against the bundled Temurin JRE."""
    java = find_java()
    if not java:
        return ""
    try:
        proc = subprocess.run([java, "-version"], capture_output=True, text=True,
                               timeout=15, creationflags=_CREATE_NO_WINDOW)
        first_line = (proc.stderr or proc.stdout or "").strip().splitlines()
        return first_line[0].strip() if first_line else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _run_epubcheck_subprocess(cmd_args: list, target_path: str, timeout: int):
    """Shared subprocess/error-handling core for BOTH run_epubcheck() (a
    real user .epub, no extra flags) and run_epubcheck_document() (the
    W3C Test Suite's own single-document/'exp' expanded-package checks,
    which need extra --mode/-v flags) - one implementation of "launch
    java -jar epubcheck.jar ... --json <tmp file>, then parse it",
    never two copies that could quietly drift apart. `cmd_args` is the
    complete argv (java, -jar, jar, target, [--mode ... -v ...]); the
    --json flag and its temp path are appended here."""
    with tempfile.TemporaryDirectory(prefix="epubforge_epubcheck_") as tmp_dir:
        json_path = os.path.join(tmp_dir, "result.json")
        try:
            proc = subprocess.run(
                cmd_args + ["--json", json_path],
                capture_output=True, text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return parser.EpubCheckResult(
                ran=False, error=f"EPUBCheck timed out after {timeout} seconds.", epub_path=target_path)
        except OSError as e:  # noqa: BLE001 - a launch failure must never crash EPUBForge, only be reported
            return parser.EpubCheckResult(ran=False, error=f"Could not run EPUBCheck: {e}", epub_path=target_path)

        if not os.path.isfile(json_path):
            # A catastrophic EPUBCheck failure (e.g. a truly unreadable
            # file) can exit nonzero WITHOUT writing a json result at all -
            # never silently treated as "valid" in that case.
            return parser.EpubCheckResult(
                ran=False,
                error=f"EPUBCheck produced no result (exit code {proc.returncode}).\n{(proc.stderr or '')[:2000]}",
                epub_path=target_path, raw_stdout=proc.stdout, raw_stderr=proc.stderr)

        try:
            result = parser.parse_json_file(json_path, epub_path=target_path)
        except (OSError, ValueError) as e:
            return parser.EpubCheckResult(ran=False, error=f"Could not parse EPUBCheck output: {e}",
                                           epub_path=target_path)
        result.raw_stdout, result.raw_stderr = proc.stdout, proc.stderr
        return result


def run_epubcheck(epub_path: str, timeout: int = EPUBCHECK_TIMEOUT_SECONDS):
    """Runs the REAL epubcheck.jar against epub_path via --json (a real
    result file EPUBCheck itself writes, never stdout text-scraping - see
    core.epubcheck.parser's own docstring for the confirmed schema).
    Returns a parser.EpubCheckResult - ran=False (with a human-readable
    .error) for every failure mode (Java/jar missing, process launch
    failure, timeout, no result file produced, unparseable output) so a
    caller never has to distinguish "EPUBCheck says it's invalid" from
    "EPUBCheck could not be run at all" by inspecting exceptions."""
    java, jar = find_java(), find_epubcheck_jar()
    if not java or not jar:
        _, reason = check_availability()
        return parser.EpubCheckResult(ran=False, error=reason, epub_path=epub_path)
    return _run_epubcheck_subprocess([java, "-jar", jar, epub_path], epub_path, timeout)


def run_epubcheck_document(path: str, mode: str = "", version: str = "3.0", include_usage: bool = False,
                            timeout: int = EPUBCHECK_TIMEOUT_SECONDS):
    """FINAL VALIDATION PHASE (spec Part A: "Official W3C EPUBCheck Test
    Suite") - the real epubcheck.jar CLI (confirmed via --help) supports
    checking a STANDALONE document or an EXPANDED (unzipped) EPUB
    directory via '--mode {opf|xhtml|nav|svg|mo|exp} -v {2.0|3.0}',
    instead of a real .epub file - exactly what the official Test Suite's
    own Gherkin scenarios require (core.epubcheck.test_suite_runner).

    mode="" behaves exactly like run_epubcheck() (no extra flags, for a
    real .epub file). include_usage adds '--usage' (confirmed via --help:
    "include ePub feature usage information in output (default is OFF)")
    - only affects whether USAGE-severity messages appear, never fatal/
      error/warning detection - needed by the Test Suite's own "usage
    CODE is reported" scenarios, which would otherwise never see any
    USAGE messages at all. This is an EXTENSION of the existing runner, never
    a second/competing EPUBCheck runner - it reuses find_java()/
    find_epubcheck_jar()/check_availability() and the SAME subprocess/
    parsing core as run_epubcheck() via _run_epubcheck_subprocess().
    run_epubcheck() itself is completely unchanged and is never used for
    Test Suite scenarios, nor is this function ever used for normal user-
    EPUB validation."""
    java, jar = find_java(), find_epubcheck_jar()
    if not java or not jar:
        _, reason = check_availability()
        return parser.EpubCheckResult(ran=False, error=reason, epub_path=path)
    cmd = [java, "-jar", jar, path]
    if mode:
        cmd += ["--mode", mode, "-v", version]
    if include_usage:
        cmd += ["--usage"]
    return _run_epubcheck_subprocess(cmd, path, timeout)
