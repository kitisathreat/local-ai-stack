# PyInstaller spec for the Local AI Stack GUI
# Build: pyinstaller installer/gui.spec
# Or via: .\LocalAIStack.ps1 -BuildInstaller

import sys
import pathlib

repo = pathlib.Path(SPECPATH).parent

a = Analysis(
    [str(repo / 'gui' / 'main.py')],
    pathex=[str(repo)],
    binaries=[],
    datas=[
        (str(repo / 'assets'), 'assets'),
        (str(repo / 'config'), 'config'),
    ],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtCharts',
        'httpx',
        'yaml',
        'passlib',
        'passlib.handlers.bcrypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='gui',
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
    icon=str(repo / 'assets' / 'icon.ico') if (repo / 'assets' / 'icon.ico').exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui',
)
