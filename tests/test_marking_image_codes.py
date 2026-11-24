"""Импорт Data Matrix с синтетических изображений."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from app.core.marking import code_import, codes, datamatrix


def test_import_reads_multiple_marks_from_rotated_jpeg(tmp_path):
    ki = '010461060025006221ABC;,"DEFG123'
    first = f"{ki}{codes.GS}91TEST{codes.GS}92{'A' * 44}"
    second = f"010461060025006221SERIAL7{codes.GS}93ABCD"
    image = QImage(420, 410, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    with QPainter(image) as painter:
        for top, mark in [(24, first), (216, second)]:
            for y, row in enumerate(datamatrix.encode(mark)):
                for x, dark in enumerate(row):
                    if dark:
                        painter.fillRect(32 + x * 4, top + y * 4, 4, 4, Qt.GlobalColor.black)
    photo = tmp_path / "фото этикеток.JPG"
    assert image.transformed(QTransform().rotate(90)).save(str(photo), "JPEG", 75)

    imported, = code_import.read_files([str(photo)])

    assert not imported.error
    assert sorted(code.raw for code in imported.batch.valid) == sorted([first, second])
    assert all(code.full for code in imported.batch.valid)


def test_unreadable_photos_are_reported_without_stopping_batch(tmp_path):
    empty = tmp_path / "пусто.png"
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    assert image.save(str(empty))
    broken = tmp_path / "повреждено.jpg"
    broken.write_bytes(b"not an image")
    source = tmp_path / "коды.csv"
    mark = "010461060025006221SERIAL7"
    codes.save_codes_csv(str(source), [mark])
    progress = []

    first, second, third = code_import.read_files(
        [str(empty), str(broken), str(source)],
        progress=lambda done, total: progress.append((done, total)),
    )

    assert "не удалось считать коды маркировки" in first.error
    assert "Не удалось открыть изображение" in second.error
    assert not third.error
    assert [code.raw for code in third.batch.valid] == [mark]
    assert progress == [(1, 3), (2, 3), (3, 3)]
