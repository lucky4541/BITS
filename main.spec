# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


# ============================================================
# PROJECT DATA
# ============================================================

datas = [
    # IMPORTANT:
    # Bundle the complete profiles directory and preserve
    # the same relative path inside the EXE _internal folder.
    ('profiles', 'profiles'),
]


# ============================================================
# HIDDEN IMPORTS
# ============================================================

hiddenimports = []

# If your project dynamically imports modules from app/core,
# collect their submodules automatically.
hiddenimports += collect_submodules('app')
hiddenimports += collect_submodules('core')


# ============================================================
# ANALYSIS
# ============================================================

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


# ============================================================
# PYZ
# ============================================================

pyz = PYZ(
    a.pure
)


# ============================================================
# EXE
# ============================================================

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='main',
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