# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# The window/taskbar icon is read at runtime from assets/app_settings/
# icon.png; without this the frozen build falls back to imgui_bundle's own.
datas = [('assets', 'assets')]
binaries = []
hiddenimports = ['vertexui.toasts']
tmp_ret = collect_all('imgui_bundle')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('OpenGL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run.py'],
    # No pathex: VertexUI comes from requirements.txt, so it is importable
    # from the venv like any other dependency. A hardcoded path to one
    # machine's checkout is not something to ship in a public repo.
    pathex=[],
    binaries=binaries,
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
    name='Bedside',
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
    icon=['assets/icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Bedside',
)
