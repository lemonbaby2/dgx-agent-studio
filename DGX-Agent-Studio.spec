# -*- mode: python ; coding: utf-8 -*-
"""DGX Agent Studio · PyInstaller 打包配置（单文件 exe）"""

import os

block_cipher = None

HERE = os.path.dirname(os.path.abspath(SPEC))

# 前端静态资源 + pywebview 自带的 JS 桥接文件
datas = [
    (os.path.join(HERE, 'web'), 'web'),
]
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files('webview', includes=['*.js', '*.html', '*.css'])
except Exception:
    pass

hiddenimports = [
    'server',
    'server.core',
    'server.fs_api',
    'server.app',
    'webview',
    'webview.platforms',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr',
    'bottle',
    'proxy_tools',
]

a = Analysis(
    [os.path.join(HERE, 'main.py')],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL', 'cv2',
        'tkinter', 'PyQt5', 'PySide2', 'PySide6', 'wx', 'test',
        'unittest', 'pydoc_data', 'IPython', 'notebook',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DGX-Agent-Studio',
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
    icon=None,
)
