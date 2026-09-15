# -*- mode: python ; coding: utf-8 -*-
"""Сборка .app для macOS.

Запуск:  pyinstaller RetailCore.macos.spec --noconfirm

Отличия от Windows-сборки:
- BUNDLE вместо одиночного EXE → .app с корректной интеграцией в macOS
- Иконка .icns (конвертируется в CI из app.png)
- argv_emulation=True — macOS передаёт файлы через Apple Events, а не argv
- pywin32 отсутствует — КриптоПро на macOS невозможна
"""

import os

# Модули Qt, которые приложение не использует.
EXCLUDED = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSerialPort",
    "PySide6.QtSensors", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtNetworkAuth", "PySide6.QtHttpServer", "PySide6.QtUiTools",
    "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtPrintSupport",
    "PySide6.QtGraphs", "PySide6.QtGraphsWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest", "PySide6.QtVirtualKeyboard",
    # Тяжёлые научные пакеты в приложении не участвуют.
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "tkinter", "pytest", "PyQt5", "PyQt6",
]

from PyInstaller.utils.hooks import collect_data_files

DATAS = [("app/ui/assets", "app/ui/assets")] + collect_data_files("qtawesome")

# Иконка: CI конвертирует app.png → app.icns перед сборкой.
ICON = "app/ui/assets/app.icns" if os.path.exists("app/ui/assets/app.icns") else None

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=DATAS,
    hiddenimports=["app", "rapidfuzz", "openpyxl", "PySide6.QtNetwork", "PySide6.QtCharts"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED,
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
    name="RetailCore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,    # macOS: файлы приходят через Apple Events
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

app = BUNDLE(
    exe,
    name="RetailCore.app",
    icon=ICON,
    bundle_identifier="ru.qbeely.retailcore",
    info_plist={
        "CFBundleDisplayName": "RetailCore",
        "CFBundleShortVersionString": "3.2.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
