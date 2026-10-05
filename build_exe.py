"""
ZoneTool / BITSTool Windows EXE Builder

Run this file from the ZoneTool project folder:

    python build_exe.py --clean

Debug build:

    python build_exe.py --debug --clean

The generated application is:

    dist\\BITSTool\\BITSTool.exe

IMPORTANT
---------
This builder keeps the project structure used by the development version.

In particular:

    profiles\\bits_profile.json, profiles\\jats_profile.json
    profiles\\BITS\\, profiles\\JATS\\ (semantic roles, DTDs)

are copied to:

    _internal\\profiles\\

Do NOT change the BITS / JATS conversion code just to fix an EXE
resource problem. The development version already proves that the
conversion logic works.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# ============================================================
# PROJECT PATHS
# ============================================================

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "main.py"

APP_NAME = "BITSTool"

DIST = ROOT / "dist"
BUILD = ROOT / "build"


# ============================================================
# MANUAL FILES
#
# Add individual files here when required.
#
# Example:
#
# EXTRA_FILES = [
#     r"config.json",
#     r"settings.json",
# ]
# ============================================================

EXTRA_FILES = [
    # r"config.json",
    # r"settings.json",
]


# ============================================================
# MANUAL FOLDERS
#
# Add folders here when required.
#
# Example:
#
# EXTRA_FOLDERS = [
#     r"assets",
#     r"templates",
# ]
#
# The folder will be copied preserving its folder name.
# ============================================================

EXTRA_FOLDERS = [
    # r"assets",
    # r"templates",
    # r"resources",
    # r"models",
]


# ============================================================
# IMPORTANT PROJECT DATA
#
# These are folders that must be included because the application
# uses them at runtime.
#
# PROFILES IS REQUIRED.
#
# Development:
#
#     profiles\\bits_profile.json, profiles\\JATS\\dtd\\...
#
# EXE:
#
#     _internal\\profiles\\bits_profile.json, ...
# ============================================================

REQUIRED_DATA_FOLDERS = [
    "profiles",
]


# ============================================================
# OPTIONAL AUTOMATIC DATA FOLDERS
#
# These are included only if they actually exist.
#
# Keep this list limited to project data folders.
# ============================================================

OPTIONAL_DATA_FOLDERS = [
    "assets",
    "asset",
    "resources",
    "resource",
    "templates",
    "template",
    "static",
    "data",
    "models",
    "profile",
    "config",
    "configs",
    "configuration",
    "configurations",
]


# ============================================================
# PYTHON HIDDEN IMPORTS
# ============================================================

HIDDEN_IMPORTS = [
    "core",
    "app",

    "fitz",
    "PIL",
    "cv2",
    "numpy",

    "paddle",
    "paddleocr",
]


# ============================================================
# COLLECT ALL SUBMODULES
# ============================================================

COLLECT_SUBMODULES = [
    "core",
    "app",
]


# ============================================================
# COLLECT PACKAGE DATA
#
# PaddleOCR contains package data that may not be detected
# automatically by PyInstaller.
# ============================================================

COLLECT_ALL = [
    "paddleocr",
]


# ============================================================
# OPTIONAL PADDLEOCR MODEL CACHE LOCATIONS
#
# These are included only when they already exist.
#
# IMPORTANT:
# The application must still know where to load bundled models
# from. Merely copying a cache does not change PaddleOCR's model
# lookup logic.
#
# Therefore this section is only for packaging existing model
# data; it does not alter OCR behavior.
# ============================================================

PADDLE_CACHE_DIRS = [
    Path.home() / ".paddleocr",
    Path.home() / ".paddleocr" / "models",

    Path(os.environ.get("LOCALAPPDATA", "")) / "PaddleOCR",
    Path(os.environ.get("LOCALAPPDATA", "")) / "paddleocr",

    Path(os.environ.get("APPDATA", "")) / "PaddleOCR",
    Path(os.environ.get("APPDATA", "")) / "paddleocr",
]


# ============================================================
# CLEAN
# ============================================================

def clean_output() -> None:
    """Remove previous PyInstaller output."""

    for path in (BUILD, DIST):
        if path.exists():
            print(f"[CLEAN] {path}")
            shutil.rmtree(path, ignore_errors=True)


# ============================================================
# PROJECT CHECK
# ============================================================

def check_project() -> None:
    """Verify that the builder is running from project root."""

    if not ENTRY.is_file():
        raise SystemExit(
            "\nERROR: main.py was not found.\n\n"
            f"Expected:\n{ENTRY}\n\n"
            "Put build_exe.py in the ZoneTool project root."
        )

    print("[OK] Project root :", ROOT)
    print("[OK] Entry point  :", ENTRY)


# ============================================================
# REQUIRED PROFILE CHECK
# ============================================================

def check_required_data() -> None:
    """
    Verify required runtime data before starting PyInstaller.

    This prevents a successful-looking EXE build which is missing
    required profile files.
    """

    print()
    print("=" * 70)
    print("CHECKING REQUIRED PROJECT DATA")
    print("=" * 70)

    for folder_name in REQUIRED_DATA_FOLDERS:
        folder = ROOT / folder_name

        if not folder.is_dir():
            raise SystemExit(
                "\nERROR: Required data folder was not found:\n"
                f"    {folder}\n\n"
                f"The {folder_name!r} folder must exist before building."
            )

        print("[OK] Folder:", folder)

    # Specifically verify the BITS / JATS profiles (tag lists, Auto Tag
    # vocabulary, the JATS DTD the output is validated against).
    profiles = ROOT / "profiles"
    bits_dir = profiles / "BITS"

    if not bits_dir.is_dir():
        raise SystemExit(
            "\nERROR: BITS profile folder was not found:\n"
            f"    {bits_dir}"
        )

    print("[OK] BITS profile:", bits_dir)

    required_files = [
        profiles / "bits_profile.json",
        profiles / "jats_profile.json",
        bits_dir / "semantic_roles.json",
        profiles / "JATS" / "dtd" / "JATS-journalpublishing1-4-mathml3.dtd",
    ]

    for file_path in required_files:
        if not file_path.is_file():
            raise SystemExit(
                "\nERROR: Required profile file was not found:\n"
                f"    {file_path}\n\n"
                "The EXE cannot work correctly without this file."
            )

        print("[OK] Profile file:", file_path)

    print("=" * 70)


# ============================================================
# PYINSTALLER CHECK
# ============================================================

def check_pyinstaller() -> None:
    """Verify PyInstaller is installed in the active Python."""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--version",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SystemExit(
            "\nPyInstaller is not installed in this Python environment.\n\n"
            "Install it with:\n\n"
            "    python -m pip install pyinstaller\n"
        )

    print("[OK] PyInstaller:", result.stdout.strip())


# ============================================================
# ADD DATA
# ============================================================

def add_data(
    command: list[str],
    source: Path,
    destination: str,
) -> None:
    """
    Add a PyInstaller data folder/file.

    Windows PyInstaller uses:

        source;destination
    """

    command.extend(
        [
            "--add-data",
            f"{source};{destination}",
        ]
    )


# ============================================================
# MANUAL ITEMS
# ============================================================

def existing_manual_items() -> tuple[list[Path], list[Path]]:
    """Resolve manually configured files and folders."""

    files: list[Path] = []
    folders: list[Path] = []

    for item in EXTRA_FILES:
        path = ROOT / item

        if path.is_file():
            files.append(path)
        else:
            print("[WARN] Missing manual file:", path)

    for item in EXTRA_FOLDERS:
        path = ROOT / item

        if path.is_dir():
            folders.append(path)
        else:
            print("[WARN] Missing manual folder:", path)

    return files, folders


# ============================================================
# BUILD
# ============================================================

def build(
    debug: bool = False,
    clean: bool = False,
) -> None:

    # --------------------------------------------------------
    # CHECKS
    # --------------------------------------------------------

    check_project()
    check_required_data()
    check_pyinstaller()

    # --------------------------------------------------------
    # CLEAN
    # --------------------------------------------------------

    if clean:
        clean_output()

    # --------------------------------------------------------
    # MANUAL ITEMS
    # --------------------------------------------------------

    manual_files, manual_folders = existing_manual_items()

    # --------------------------------------------------------
    # PYINSTALLER COMMAND
    # --------------------------------------------------------

    command = [
        sys.executable,
        "-m",
        "PyInstaller",

        "--noconfirm",
        "--clean",

        # IMPORTANT:
        # onedir keeps _internal resources visible and is much
        # easier to diagnose for this application.
        "--onedir",

        "--name",
        APP_NAME,

        "--distpath",
        str(DIST),

        "--workpath",
        str(BUILD),

        "--specpath",
        str(BUILD),

        "--paths",
        str(ROOT),
    ]

    # --------------------------------------------------------
    # CONSOLE / GUI
    # --------------------------------------------------------

    if debug:
        command.append("--console")
    else:
        command.append("--windowed")

    # --------------------------------------------------------
    # ENTRY POINT
    # --------------------------------------------------------

    command.append(str(ENTRY))

    # --------------------------------------------------------
    # HIDDEN IMPORTS
    # --------------------------------------------------------

    for module in HIDDEN_IMPORTS:
        command.extend(
            [
                "--hidden-import",
                module,
            ]
        )

    # --------------------------------------------------------
    # COLLECT SUBMODULES
    # --------------------------------------------------------

    for package in COLLECT_SUBMODULES:
        command.extend(
            [
                "--collect-submodules",
                package,
            ]
        )

    # --------------------------------------------------------
    # COLLECT PACKAGE DATA
    # --------------------------------------------------------

    for package in COLLECT_ALL:
        command.extend(
            [
                "--collect-all",
                package,
            ]
        )

    # --------------------------------------------------------
    # PREVENT DUPLICATE DATA ENTRIES
    # --------------------------------------------------------

    seen: set[str] = set()

    # --------------------------------------------------------
    # REQUIRED DATA FOLDERS
    #
    # profiles -> _internal/profiles
    #
    # This is the critical part for:
    #
    # profiles/bits_profile.json, profiles/jats_profile.json
    # profiles/BITS, profiles/JATS (semantic roles, DTDs)
    # --------------------------------------------------------

    for folder_name in REQUIRED_DATA_FOLDERS:

        source = ROOT / folder_name

        key = str(source.resolve()).lower()

        if key in seen:
            continue

        seen.add(key)

        add_data(
            command,
            source,
            folder_name,
        )

        print(
            "[REQUIRED DATA]",
            source,
            "->",
            f"_internal/{folder_name}",
        )

    # --------------------------------------------------------
    # OPTIONAL PROJECT DATA
    # --------------------------------------------------------

    for folder_name in OPTIONAL_DATA_FOLDERS:

        source = ROOT / folder_name

        if not source.is_dir():
            continue

        key = str(source.resolve()).lower()

        if key in seen:
            continue

        seen.add(key)

        add_data(
            command,
            source,
            folder_name,
        )

        print(
            "[DATA]",
            source,
            "->",
            f"_internal/{folder_name}",
        )

    # --------------------------------------------------------
    # MANUAL FOLDERS
    # --------------------------------------------------------

    for source in manual_folders:

        key = str(source.resolve()).lower()

        if key in seen:
            continue

        seen.add(key)

        add_data(
            command,
            source,
            source.name,
        )

        print(
            "[MANUAL FOLDER]",
            source,
            "->",
            f"_internal/{source.name}",
        )

    # --------------------------------------------------------
    # MANUAL FILES
    # --------------------------------------------------------

    for source in manual_files:

        key = str(source.resolve()).lower()

        if key in seen:
            continue

        seen.add(key)

        add_data(
            command,
            source,
            ".",
        )

        print(
            "[MANUAL FILE]",
            source,
            "->",
            "_internal/",
        )

    # --------------------------------------------------------
    # EXISTING PADDLEOCR MODEL DATA
    # --------------------------------------------------------

    for source in PADDLE_CACHE_DIRS:

        if not source.is_dir():
            continue

        key = str(source.resolve()).lower()

        if key in seen:
            continue

        seen.add(key)

        destination = f"paddle_models/{source.name}"

        add_data(
            command,
            source,
            destination,
        )

        print(
            "[OCR MODEL DATA]",
            source,
            "->",
            f"_internal/{destination}",
        )

    # --------------------------------------------------------
    # BUILD INFORMATION
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("BUILDING", APP_NAME)
    print("=" * 70)

    print("Root   :", ROOT)
    print("Entry  :", ENTRY)
    print("Output :", DIST / APP_NAME)
    print("Mode   :", "DEBUG" if debug else "GUI")

    print("=" * 70)
    print()

    # --------------------------------------------------------
    # SHOW COMMAND
    # --------------------------------------------------------

    print("[PYINSTALLER COMMAND]")

    print(
        " ".join(
            f'"{x}"' if " " in str(x) else str(x)
            for x in command
        )
    )

    print()

    # --------------------------------------------------------
    # RUN BUILD
    # --------------------------------------------------------

    result = subprocess.run(
        command,
        cwd=ROOT,
    )

    if result.returncode != 0:

        raise SystemExit(
            "\nBUILD FAILED.\n\n"
            "Run the debug build to see the complete error:\n\n"
            "    python build_exe.py --debug --clean\n"
        )

    # --------------------------------------------------------
    # VERIFY EXE
    # --------------------------------------------------------

    application_folder = DIST / APP_NAME

    exe = application_folder / f"{APP_NAME}.exe"

    if not exe.is_file():

        raise SystemExit(
            "\nBuild finished but EXE was not found:\n"
            f"{exe}"
        )

    # --------------------------------------------------------
    # VERIFY PACKAGED PROFILES
    #
    # With PyInstaller onedir, datas are normally placed inside
    # _internal.
    # --------------------------------------------------------

    internal = application_folder / "_internal"

    packaged_profiles = internal / "profiles"

    packaged_required = [
        packaged_profiles / "bits_profile.json",
        packaged_profiles / "jats_profile.json",
        packaged_profiles / "BITS" / "semantic_roles.json",
        packaged_profiles / "JATS" / "semantic_roles.json",
        packaged_profiles / "JATS" / "dtd" / "JATS-journalpublishing1-4-mathml3.dtd",
    ]

    print()
    print("=" * 70)
    print("VERIFYING PACKAGED FILES")
    print("=" * 70)

    print("EXE:", exe)

    if not internal.is_dir():
        print(
            "[WARN] _internal folder was not found:",
            internal,
        )
    else:
        print("[OK] _internal:", internal)

    missing = []
    for path in packaged_required:
        if path.is_file():
            print("[OK]", path)
        else:
            print("[ERROR] missing:", path)
            missing.append(path)

    print("=" * 70)

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    if missing:

        raise SystemExit(
            "\nBUILD COMPLETED, BUT REQUIRED PROFILE FILES ARE "
            "MISSING FROM THE EXE.\n\n"
            "Do NOT distribute this EXE.\n"
            "Check the PyInstaller data configuration."
        )

    print()
    print("=" * 70)
    print("BUILD SUCCESSFUL")
    print("=" * 70)

    print("EXE    :", exe)
    print("FOLDER :", application_folder)

    print()
    print("Verified:")
    for path in packaged_required:
        print("  [OK]", path.relative_to(internal))

    print("=" * 70)
    print()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build BITSTool Windows EXE with PyInstaller."
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Build with a visible console for startup/error "
            "diagnostics."
        ),
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Delete previous build and dist folders before "
            "building."
        ),
    )

    args = parser.parse_args()

    build(
        debug=args.debug,
        clean=args.clean,
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()