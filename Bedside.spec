# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all


def _vertexui_path():
    """Where vertexui lives, so an editable install still freezes."""
    import importlib.util
    import os
    spec = importlib.util.find_spec("vertexui")
    if spec and spec.origin:
        return [os.path.dirname(os.path.dirname(os.path.abspath(spec.origin)))]
    return []

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
    # Derived, never hardcoded. A plain `pip install -r requirements.txt`
    # puts VertexUI in site-packages and PyInstaller finds it unaided, but
    # an editable/path install leaves only a .pth behind and the analysis
    # misses it — the frozen exe then dies on launch with "No module named
    # 'vertexui'". Asking the interpreter where it actually is covers both,
    # and hardcodes nobody's checkout.
    pathex=_vertexui_path(),
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
