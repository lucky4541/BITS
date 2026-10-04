# -*- mode: python ; coding: utf-8 -*-
"""OPTIONAL single-file PyInstaller build for EPUBForge (formerly
ZoneTool) - produces dist/EPUBForge.exe (one EXE, no visible companion
folder). NOT the primary production build; see EPUBForge.spec's own
docstring for why onedir is preferred: a onefile EXE self-extracts its
entire bundle (including all of profiles/CUPEPUB/*.xml) into a fresh
temporary directory every time it starts, which is slower to launch and
cannot be file-by-file verified ahead of shipping the way
verify_package.py verifies the onedir build's dist/EPUBForge/_internal/
contents directly.

This still works correctly for EPUBForge specifically because every
resource this app reads is resolved through core.resource_path.
resource_path() (sys._MEIPASS-based), which PyInstaller sets correctly for
onefile too - and every WRITABLE path goes through writable_path()
(sys.executable's own directory), which correctly stays outside that
temp extraction directory, so user data still persists across runs even
though the bundled resources themselves don't.

Keep this datas/hiddenimports list in sync with EPUBForge.spec - they are
intentionally duplicated (a normal PyInstaller convention for an optional
alternate build mode) rather than sharing a spec-time import, since spec
files run in PyInstaller's own restricted exec context.
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
import glob
datas += [(f, 'profiles/xhtml') for f in glob.glob('profiles/xhtml/*.json')]
# Auto Zone / Auto Tag engine (CUPEPUB): semantic role vocabulary, plus any
# project DTD(s) and reference XML corpus dropped into the profile folder.
datas += [('profiles/CUPEPUB/semantic_roles.json', 'profiles/CUPEPUB')]
datas += [(f, 'profiles/CUPEPUB') for f in glob.glob('profiles/CUPEPUB/*.dtd')]
datas += [(f, 'profiles/CUPEPUB/reference_xml') for f in glob.glob('profiles/CUPEPUB/reference_xml/*.*ml')]
datas += collect_data_files('fitz')
datas += collect_data_files('fontTools')

hiddenimports = [
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
    a.binaries,
    a.datas,
    [],
    name='EPUBForge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
