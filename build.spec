# -*- mode: python ; coding: utf-8 -*-

import os
import qtwebview2

block_cipher = None

# qtwebview2 在运行时通过 get_absolute_path('lib/...') 从 sys._MEIPASS 加载
# WebView2 的 .NET 程序集和 native loader DLL，必须把这些文件打到打包根目录的 lib/ 下。
_QTWV2_LIB_DIR = os.path.join(os.path.dirname(qtwebview2.__file__), 'lib')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('launcher_data', 'launcher_data'),
        ('version.txt', '.'),
        (_QTWV2_LIB_DIR, 'lib'),
    ],
    hiddenimports=[
        'qtwebview2',
        'qtwebview2.widget',
        'qtwebview2._dotnet_bridge',
        'pythonnet',
        'clr_loader',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebEngineQuick',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NekroAgent',
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
    icon='assets/NekroAgent.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NekroAgent',
)
