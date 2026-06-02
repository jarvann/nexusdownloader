# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

# Define application details
app_name = 'NexusDownloader'
main_script = 'run_gui.py'
src_dir = Path('src')

# Collect all source files
src_files = []
for pattern in ['**/*.py']:
    for file_path in src_dir.glob(pattern):
        src_files.append((str(file_path), str(file_path.parent)))

# Add data files (only if they exist)
data_files = []
potential_data_files = [
    ('src/config.json', 'src'),
    ('README.md', '.'),
    ('requirements.txt', '.'),
]

for src, dst in potential_data_files:
    if Path(src).exists():
        data_files.append((src, dst))

# Platform-specific configurations
if sys.platform.startswith('win'):
    icon_file = 'assets/icon.ico' if Path('assets/icon.ico').exists() else None
    console = False
    exclude_binaries = []
else:  # Linux/Unix
    icon_file = 'assets/icon.png' if Path('assets/icon.png').exists() else None
    console = False
    exclude_binaries = []

a = Analysis(
    [main_script],
    pathex=['.', 'src'],
    binaries=[],
    datas=data_files,
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtWidgets', 
        'PySide6.QtGui',
        'requests',
        'urllib3',
        'cryptography',
        'keyring',
        'colorama',
        'json',
        'threading',
        'queue',
        'concurrent.futures',
        'pathlib',
        'datetime',
        'hashlib',
        'logging',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'tkinter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=app_name,
)