# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the BITS Tool (PDF zoning -> BITS 2.2 / JATS 1.4 XML).
module/package names are unchanged, only the shipped product name).

Produces dist/BITSTool/BITSTool.exe (onedir - the primary, reliable
production build; see BITSTool_onefile.spec for the optional single-file
variant).

Bundled READ-ONLY application resources (resolved at runtime via
core.resource_path.resource_path, which points at sys._MEIPASS - the
_internal/ folder beside the EXE for this onedir build):
  - profiles/bits_profile.json, profiles/jats_profile.json
  - profiles/BITS, profiles/JATS (semantic roles, DTDs)

NOT bundled (deliberately - see the build's own final report, not
guessed here): profiles/_reserved/TandF_Zoning.xml (confirmed unused by
any code path - grepped, zero references), and anything under
projects/ output/ assets/ ocr_cache/ (USER-GENERATED data, never
application resources - see core.resource_path.writable_root).

Optional OCR engines (PaddleOCR/paddlepaddle) are NOT bundled because
they are not installed in the build environment - core/ocr/paddle_engine.py
already detects this at runtime (OCREngine.is_available()) and reports it
cleanly via the OCR Settings dialog's Installed/Missing status rather than
crashing; digital-PDF processing is completely unaffected either way. If a
future build environment has them installed, PyInstaller's own import-graph
analysis will pick them up automatically the next time this spec is built -
no spec change needed.
"""
import os
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ('profiles/bits_profile.json', 'profiles'),
    ('profiles/jats_profile.json', 'profiles'),
    ('profiles/BITS/semantic_roles.json', 'profiles/BITS'),
    ('profiles/JATS/semantic_roles.json', 'profiles/JATS'),
]
import glob
# JATS 1.4 DTD (bundled, public domain) and the BITS 2.2 DTD once installed
# in profiles/BITS/dtd/ - every module / entity file, keeping the folders
for base in ('profiles/JATS/dtd', 'profiles/BITS/dtd'):
    for f in glob.glob(base + '/**/*', recursive=True):
        if os.path.isfile(f):
            datas.append((f, os.path.dirname(f)))
# reference BITS / JATS XML corpus for Auto Tag (optional)
for kind in ('BITS', 'JATS'):
    datas += [(f, f'profiles/{kind}/reference_xml') for f in glob.glob(f'profiles/{kind}/reference_xml/*.*ml')]
# fitz/PyMuPDF ships its own font resources; fontTools' 'agl' submodule
# carries the Adobe Glyph List data table core/glyph_fidelity.py reads at
# runtime - both are real runtime data dependencies, not just importable
# code, so they need collect_data_files rather than relying on
# hiddenimports (which only handles import graph, not package data).
datas += collect_data_files('fitz')
datas += collect_data_files('fontTools')

hiddenimports = [
    # Explicit insurance alongside PyInstaller's own static import-graph
    # analysis (which already follows every plain `import`/`from...import`
    # statement in the source, try/except-wrapped or not) - harmless if
    # already auto-detected, a safety net for anything the analysis
    # misses (e.g. a package that does its own internal dynamic imports).
    'fitz', 'PIL', 'PIL.Image', 'PIL.ImageTk',
    'lxml.etree', 'lxml._elementpath',
    'fontTools.ttLib', 'fontTools.agl',
    'numpy', 'cv2',
    'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
    'tkinter.simpledialog', 'tkinter.colorchooser',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BITSTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BITSTool',
)
