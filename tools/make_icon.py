"""Собирает app.ico из app.svg во всех размерах, нужных Windows.

Запуск:  .venv\\Scripts\\python.exe tools\\make_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "ui" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render(svg: bytes, size: int) -> Image.Image:
    image = QImage(size, size, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    QSvgRenderer(QByteArray(svg)).render(painter)
    painter.end()

    buffer = image.constBits().tobytes()
    return Image.frombytes("RGBA", (size, size), buffer)


def main() -> int:
    QGuiApplication(sys.argv)  # QImage/QPainter требуют приложения
    svg = (ASSETS / "app.svg").read_bytes()

    frames = [render(svg, size) for size in SIZES]
    target = ASSETS / "app.ico"
    frames[-1].save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    frames[-1].save(ASSETS / "app.png", format="PNG")

    print(f"иконка: {target} ({target.stat().st_size} байт), размеры: {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
