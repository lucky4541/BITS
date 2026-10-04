# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for EPUBForge (formerly ZoneTool - internal
module/package names are unchanged, only the shipped product name).

Produces dist/EPUBForge/EPUBForge.exe (onedir - the primary, reliable
production build; see EPUBForge_onefile.spec for the optional single-file
variant).

Bundled READ-ONLY application resources (resolved at runtime via
core.resource_path.resource_path, which points at sys._MEIPASS - the
_internal/ folder beside the EXE for this onedir build):
  - profiles/xml_profile.json, profiles/epub_profile.json
  - profiles/CUPEPUB/*.xml (Profile, Zoning, Master, AutoStyling,
    Character, ZoneValidation, CUPLookup, Mapping)

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
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ('profiles/xml_profile.json', 'profiles'),
    ('profiles/epub_profile.json', 'profiles'),
    ('profiles/CUPEPUB/CUPEPUB_Profile.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPEPUB_Zoning.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPEPUB_Master.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPEPUB_AutoStyling.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPEPUB_Character.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPEPUB_ZoneValidation.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/CUPLookup.xml', 'profiles/CUPEPUB'),
    ('profiles/CUPEPUB/Mapping.xml', 'profiles/CUPEPUB'),
]
# The EPUBForge "XHTML Profile" system's client-structure configs (see
# core/xhtml_profile_manager.py) - read fresh from JSON at runtime via
# core.resource_path.resource_path, so every profiles/xhtml/*.json must be
# bundled the same way the CUPEPUB configs above are, or a packaged build
# would only ever see unconfigured placeholders.
import glob
datas += [(f, 'profiles/xhtml') for f in glob.glob('profiles/xhtml/*.json')]
# Auto Zone / Auto Tag engine (CUPEPUB): semantic role vocabulary, plus any
# project DTD(s) and reference XML corpus dropped into the profile folder.
datas += [('profiles/CUPEPUB/semantic_roles.json', 'profiles/CUPEPUB')]
datas += [(f, 'profiles/CUPEPUB') for f in glob.glob('profiles/CUPEPUB/*.dtd')]
datas += [(f, 'profiles/CUPEPUB/reference_xml') for f in glob.glob('profiles/CUPEPUB/reference_xml/*.*ml')]
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
    # client rules (core.epub.client_rules): EPUB-010 reads files as UTF-7 like the client's tool
    'encodings.utf_7',
    # PDF <-> XHTML QC workspace (app.qc / core.qc)
    'tkinter.simpledialog', 'tkinter.colorchooser', 'PIL.ImageOps', 'PIL.ImageChops', 'PIL.ImageFilter',
    'app.qc.qc_window', 'core.qc.engine',
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
    name='EPUBForge',
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
    name='EPUBForge',
)
