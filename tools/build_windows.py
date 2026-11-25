"""Сборка нативного Windows EXE и установщика RetailCore."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from make_version_json import ROOT, app_version


def verify_compilation(report: Path) -> None:
    modules = ElementTree.parse(report).getroot().findall("module")
    application = [
        module for module in modules
        if module.attrib["name"] in {"__main__", "app"}
        or module.attrib["name"].startswith("app.")
    ]
    compiled = {
        module.attrib["name"] for module in application
        if module.attrib["kind"].startswith("CompiledPython")
        or module.attrib["kind"] == "PythonMainModule"
    }
    missing = {"__main__", "app", "app.main", "app.core.matching"} - compiled
    missing.update(module.attrib["name"] for module in application
                   if module.attrib["name"] not in compiled)
    if missing:
        raise SystemExit("Nuitka не скомпилировал: " + ", ".join(sorted(missing)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--makensis", required=True, type=Path, help="Путь к makensis.exe")
    parser.add_argument("--mingw64", action="store_true", help="Использовать MinGW64 вместо MSVC")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    makensis = args.makensis.resolve(strict=True)
    output = args.output_dir.resolve()
    work = ROOT / "build" / "windows-native"
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    sing_box = ROOT / "vendor" / "sing-box" / "sing-box.exe"
    if not sing_box.is_file():
        raise SystemExit("Сначала выполните python tools/fetch_singbox.py")

    with tempfile.TemporaryDirectory(prefix="build-", dir=work, ignore_cleanup_errors=True) as directory:
        stage = Path(directory)
        # Nuitka исключает EXE из файлов данных; файл с пометкой .bin будет
        # распакован как ресурс и скопирован приложением под исходным именем.
        report = stage / "compilation.xml"
        subprocess.run([
            sys.executable, "-m", "nuitka", "--mode=onefile",
            "--mingw64" if args.mingw64 else "--msvc=latest",
            "--assume-yes-for-downloads",
            "--enable-plugin=pyside6",
            "--include-qt-plugins=multimedia",
            "--windows-console-mode=disable",
            "--windows-icon-from-ico=app/ui/assets/app.ico",
            f"--product-version={app_version()}",
            "--product-name=RetailCore",
            "--file-description=RetailCore",
            "--include-package=app",
            "--include-module=win32timezone",
            "--include-data-dir=app/ui/assets=app/ui/assets",
            "--include-data-files=CHANGELOG.md=CHANGELOG.md",
            "--include-data-files=vendor/sing-box/sing-box.exe=vpn/sing-box.bin",
            f"--output-dir={stage}",
            "--output-filename=RetailCore.exe",
            f"--report={report}",
            "run.py",
        ], cwd=ROOT, check=True)
        verify_compilation(report)
        exe = stage / "RetailCore.exe"
        selftest = subprocess.run([str(exe), "--selftest"], cwd=stage, timeout=120,
                                  env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        log = stage / "selftest.log"
        if log.is_file():
            print(log.read_text(encoding="utf-8"))
        selftest.check_returncode()
        setup = stage / "RetailCore-Setup.exe"
        subprocess.run([
            str(makensis), "/WX", "/INPUTCHARSET", "UTF8",
            f"/DRetailCoreVersion={app_version()}",
            f"/DRetailCoreExe={exe}", f"/DRetailCoreSetup={setup}",
            str(ROOT / "tools" / "RetailCore.nsi"),
        ], cwd=ROOT, check=True)
        for artifact in (exe, setup):
            artifact.replace(output / artifact.name)


if __name__ == "__main__":
    main()
