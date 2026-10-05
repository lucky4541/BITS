# -*- mode: python ; coding: utf-8 -*-
"""OPTIONAL single-file PyInstaller build for BITSTool (formerly
ZoneTool) - produces dist/BITSTool.exe (one EXE, no visible companion
folder). NOT the primary production build; see BITSTool.spec's own
docstring for why onedir is preferred: a onefile EXE self-extracts its
entire bundle (including all of profiles/BITS, profiles/JATS) into a fresh
temporary directory every time it starts, which is slower to launch and
cannot be file-by-file verified ahead of shipping the way
verify_package.py verifies the onedir build's dist/BITSTool/_internal/
contents directly.

This still works correctly for BITSTool specifically because every
resource this app reads is resolved through core.resource_path.
resource_path() (sys._MEIPASS-based), which PyInstaller sets correctly for
onefile too - and every WRITABLE path goes through writable_path()
(sys.executable's own directory), which correctly stays outside that
temp extraction directory, so user data still persists across runs even
though the bundled resources themselves don't.

Keep this datas/hiddenimports list in sync with BITSTool.spec - they are
intentionally duplicated (a normal PyInstaller convention for an optional
alternate build mode) rather than sharing a spec-time import, since spec
files run in PyInstaller's own restricted exec context.
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
datas += collect_data_files('fitz')
datas += collect_data_files('fontTools')

hiddenimports = [
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
    a.binaries,
    a.datas,
    [],
    name='BITSTool',
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
