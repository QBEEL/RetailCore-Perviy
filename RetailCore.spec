# -*- mode: python ; coding: utf-8 -*-
"""Сборка одного исполняемого файла.

Запуск:  .venv\\Scripts\\pyinstaller.exe RetailCore.spec --noconfirm
"""

# Модули Qt, которые приложение не использует. Без этого списка в сборку
# попадает весь Qt (WebEngine, 3D, Quick) — сотни лишних мегабайт.
EXCLUDED = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
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

# qtawesome тащит шрифты с иконками — без них интерфейс останется без значков.
DATAS = [("app/ui/assets", "app/ui/assets")] + collect_data_files("qtawesome")

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=DATAS,
    hiddenimports=["app", "rapidfuzz", "openpyxl", "PySide6.QtNetwork"],
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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # без чёрного окна консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app/ui/assets/app.ico",
)
