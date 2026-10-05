"""Packaging self-test (run after build_exe.bat / build_onefile.bat).

Verifies the built package actually contains every resource BITSTool
reads at runtime, and that no REQUIRED dependency silently failed to
bundle - so a missing resource is caught here, at build time, instead of
surfacing as a confusing runtime error on a customer's machine.

Usage:
    python verify_package.py                  # checks dist/BITSTool (onedir)
    python verify_package.py --onefile         # checks dist/BITSTool.exe (onefile)
    python verify_package.py --dist-dir PATH   # checks a specific onedir output

Exits non-zero (and build_exe.bat checks this) if anything required is
missing.
"""
import argparse
import glob
import os
import re
import sys

REQUIRED_RESOURCES = [
    "profiles/bits_profile.json",
    "profiles/jats_profile.json",
    "profiles/BITS/semantic_roles.json",
    "profiles/JATS/semantic_roles.json",
    "profiles/JATS/dtd/JATS-journalpublishing1-4-mathml3.dtd",
]

# Hard runtime dependencies - if PyInstaller's own dependency analysis
# reports any of these as a genuinely missing module (not one of the
# universally-benign POSIX/Java-only stdlib warnings every Windows
# PyInstaller build produces), the package is broken and must not ship.
REQUIRED_MODULES = ["fitz", "PIL", "lxml", "numpy", "cv2", "fontTools"]

# Known-safe, EXPECTED "missing module" warnings - optional OCR engines
# not installed in the build environment (core/ocr/paddle_engine.py
# already handles their absence gracefully at runtime - see
# OCREngine.is_available()). Never a build failure.
EXPECTED_OPTIONAL_MISSING = ["paddle", "paddleocr"]


def _fail(msg: str, failures: list):
    print(f"[FAIL] {msg}")
    failures.append(msg)


def _ok(msg: str):
    print(f"[OK]   {msg}")


def verify_onedir(dist_dir: str) -> list:
    failures = []
    exe_path = os.path.join(dist_dir, "BITSTool.exe")
    if os.path.isfile(exe_path):
        _ok(f"BITSTool.exe exists ({exe_path})")
    else:
        _fail(f"BITSTool.exe NOT FOUND at {exe_path}", failures)

    internal_dir = os.path.join(dist_dir, "_internal")
    for rel in REQUIRED_RESOURCES:
        full = os.path.join(internal_dir, *rel.split("/"))
        if os.path.isfile(full):
            _ok(f"bundled resource present: {rel}")
        else:
            _fail(f"REQUIRED resource missing from package: {rel}", failures)

    _check_warn_file(dist_dir, failures)
    return failures


def verify_onefile(exe_path: str) -> list:
    """A onefile EXE can't be inspected without actually running it (its
    contents only exist extracted at runtime in a temp directory) - this
    just confirms the EXE itself exists and is a plausible size (a
    near-empty file would mean the build silently failed to embed
    anything). The onedir build is the one this script can fully verify
    file-by-file, which is exactly why onedir is the primary production
    build (see BITSTool.spec's own docstring)."""
    failures = []
    if os.path.isfile(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        if size_mb > 5:
            _ok(f"BITSTool.exe exists ({exe_path}, {size_mb:.1f} MB)")
        else:
            _fail(f"BITSTool.exe exists but is suspiciously small ({size_mb:.1f} MB) - "
                  f"likely an incomplete bundle", failures)
    else:
        _fail(f"BITSTool.exe NOT FOUND at {exe_path}", failures)
    print("[INFO] onefile contents cannot be statically verified (only exist "
          "extracted at runtime) - run the EXE directly to confirm it starts.")
    return failures


def _check_warn_file(dist_dir: str, failures: list):
    """PyInstaller writes build/BITSTool/warn-BITSTool.txt during the
    build - if present (i.e. this ran right after a fresh build, same
    directory layout BITSTool.spec produces), cross-check it for any of
    our REQUIRED modules genuinely missing, distinct from the expected/
    benign optional-OCR-engine and POSIX/Java stdlib noise."""
    # build/BITSTool/ is a SIBLING of dist/ (both directly under the project
    # root) - not nested under dist_dir, which os.path.dirname(dist_dir)
    # would incorrectly assume.
    warn_path = os.path.join("build", "BITSTool", "warn-BITSTool.txt")
    if not os.path.isfile(warn_path):
        print(f"[INFO] {warn_path} not found - skipping hidden-import cross-check "
              f"(run this script right after build_exe.bat to get this check too).")
        return
    with open(warn_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    # Exact match only ("missing module named numpy -", not "...numpy._core.void -",
    # which is numpy's own harmless internal lazy-import, not numpy itself
    # missing) - a naive \b-terminated prefix match would treat every dotted
    # submodule name starting with a required package as a false failure.
    for mod in REQUIRED_MODULES:
        if re.search(rf"missing module named {re.escape(mod)} -", content):
            _fail(f"REQUIRED module '{mod}' reported missing by PyInstaller's own analysis", failures)
        else:
            _ok(f"required module present in dependency graph: {mod}")
    for mod in EXPECTED_OPTIONAL_MISSING:
        if re.search(rf"missing module named {re.escape(mod)} -", content):
            print(f"[INFO] optional OCR engine '{mod}' not bundled (not installed in the build "
                  f"environment) - expected, detected gracefully at runtime, not a failure.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default=os.path.join("dist", "BITSTool"),
                         help="onedir build output directory (default: dist/BITSTool)")
    parser.add_argument("--onefile", action="store_true",
                         help="verify a onefile build instead (dist/BITSTool.exe)")
    args = parser.parse_args()

    print("=" * 60)
    print("BITSTool packaging verification")
    print("=" * 60)

    if args.onefile:
        failures = verify_onefile(os.path.join("dist", "BITSTool.exe"))
    else:
        failures = verify_onedir(args.dist_dir)

    print("=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} problem(s) found - do NOT ship this build.")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PASSED: package looks complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
