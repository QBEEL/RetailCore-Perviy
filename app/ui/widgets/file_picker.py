"""Поле выбора файла: drag&drop, обзор, список недавних, выбор листа."""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core import pdf_invoice
from .. import icons
from ..theme import Metrics, Palette
from .inputs import SelectBox
from .toast import ToastKind

EXCEL_FILTER = "Excel (*.xlsx *.xlsm *.xls);;Все файлы (*.*)"
# Поле открытия принимает и счёт поставщика в PDF: он переводится в Excel рядом
# с собой. Для сохранения остаётся EXCEL_FILTER — писать в PDF нечего.
OPEN_FILTER = ("Excel или счёт PDF (*.xlsx *.xlsm *.xls *.pdf);;"
               "Excel (*.xlsx *.xlsm *.xls);;Счёт PDF (*.pdf);;Все файлы (*.*)")
_EXCEL = (".xlsx", ".xlsm", ".xls")
CSV_FILTER = "Выгрузка 1С (*.csv *.txt);;Все файлы (*.*)"


class FilePicker(QFrame):
    """Одна строка выбора файла с подписью, путём и листом."""

    file_selected = Signal(str)
    sheet_changed = Signal(str)

    def __init__(
        self,
        label: str,
        hint: str = "",
        parent: QWidget | None = None,
        file_filter: str = OPEN_FILTER,
    ) -> None:
        super().__init__(parent)
        self._filter = file_filter
        self._accepts_pdf = "*.pdf" in file_filter
        self.setAcceptDrops(True)
        # Поля выбора файла не сжимаются: при нехватке места уступает таблица,
        # у которой есть прокрутка, а не элементы управления.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._path = ""
        self._pdf_source = ""
        self._recent: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        caption = QHBoxLayout()
        caption.setSpacing(7)
        title = QLabel(label, self)
        title.setStyleSheet("font-weight: 600;")
        caption.addWidget(title)
        self._hint = QLabel(hint, self)
        self._hint.setObjectName("Hint")
        caption.addWidget(self._hint, 1)
        root.addLayout(caption)

        row = QHBoxLayout()
        row.setSpacing(7)
        self.field = QLineEdit(self)
        self.field.setObjectName("Path")
        self.field.setPlaceholderText("Перетащите файл сюда или нажмите «Обзор»")
        self.field.setClearButtonEnabled(True)
        self.field.editingFinished.connect(self._on_typed)
        row.addWidget(self.field, 1)

        self.recent_button = QPushButton(self)
        self.recent_button.setIcon(icons.icon("recent"))
        self.recent_button.setToolTip("Недавние файлы")
        self.recent_button.setFixedWidth(38)
        self.recent_button.clicked.connect(self._show_recent)
        row.addWidget(self.recent_button)

        browse = QPushButton("Обзор", self)
        browse.setIcon(icons.icon("open"))
        browse.clicked.connect(self.browse)
        row.addWidget(browse)

        # Книга из PDF создаётся рядом с ним, а PDF часто открыт прямо из
        # почты — из временной папки, которую никто потом не найдёт. Поэтому
        # переведённый счёт можно сохранить туда, куда нужно человеку.
        self.download_button = QPushButton("Скачать Excel", self)
        self.download_button.setIcon(icons.icon("download"))
        self.download_button.setToolTip("Сохранить счёт, переведённый из PDF, как книгу Excel")
        self.download_button.clicked.connect(self.download_excel)
        self.download_button.setVisible(False)
        row.addWidget(self.download_button)
        root.addLayout(row)

        sheet_row = QHBoxLayout()
        sheet_row.setSpacing(7)
        self._sheet_label = QLabel("Лист:", self)
        self._sheet_label.setObjectName("Hint")
        self.sheet_box = SelectBox(self)
        self.sheet_box.setMinimumWidth(200)
        self.sheet_box.currentTextChanged.connect(self._on_sheet)
        sheet_row.addWidget(self._sheet_label)
        sheet_row.addWidget(self.sheet_box)
        sheet_row.addStretch(1)
        self._sheet_row = sheet_row
        root.addLayout(sheet_row)
        self._set_sheet_visible(False)

    @property
    def path(self) -> str:
        return self._path

    @property
    def sheet(self) -> str:
        return self.sheet_box.currentText()

    def set_recent(self, paths: list[str]) -> None:
        self._recent = [p for p in paths if os.path.exists(p)]
        self.recent_button.setEnabled(bool(self._recent))

    def set_sheets(self, sheets: list[str], current: str = "") -> None:
        self.sheet_box.blockSignals(True)
        self.sheet_box.clear()
        self.sheet_box.addItems(sheets)
        if current and current in sheets:
            self.sheet_box.setCurrentText(current)
        self.sheet_box.blockSignals(False)
        self._set_sheet_visible(len(sheets) > 1)

    def add_control(self, label: str, widget: QWidget) -> None:
        """Добавляет элемент в строку листа: всё про один файл — в одной строке."""
        caption = QLabel(label, self)
        caption.setObjectName("Hint")
        position = self._sheet_row.count() - 1  # перед распоркой
        self._sheet_row.insertWidget(position, caption)
        self._sheet_row.insertWidget(position + 1, widget)
        self.updateGeometry()

    def set_status(self, text: str, color: str = Palette.TEXT_MUTED) -> None:
        self._hint.setText(text)
        self._hint.setStyleSheet(f"color: {color}; font-size: 12px;")

    def set_path(self, path: str, notify: bool = True) -> None:
        if not path:
            return
        source = ""
        if self._accepts_pdf and path.lower().endswith(".pdf"):
            source, path = path, self._from_pdf(path)
            if not path:
                return
        self._pdf_source = source
        self.download_button.setVisible(bool(source))
        self._path = path
        self.field.setText(path)
        self.field.setToolTip(path)
        if notify:
            self.file_selected.emit(path)

    def _from_pdf(self, pdf_path: str) -> str:
        """Счёт PDF → книга Excel рядом с ним. Пусто — не получилось.

        Дальше страница работает с книгой, как с любым Excel-файлом: колонки
        находятся сами, а «Сохранить бланк» пишет в xlsx, а не в PDF. Книга,
        которая новее PDF, не перезаписывается — её могли поправить руками.
        """
        target = pdf_invoice.excel_path_for(pdf_path)
        if os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(pdf_path):
            self._announce(f"Счёт уже переведён в Excel — открыт {os.path.basename(target)}",
                           ToastKind.INFO)
            return target

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            target, invoice = pdf_invoice.convert(pdf_path, target)
        except pdf_invoice.PdfProblem as problem:
            QApplication.restoreOverrideCursor()
            self.set_status("PDF не разобран", Palette.DANGER)
            QMessageBox.warning(self, "Счёт PDF", str(problem))
            return ""
        QApplication.restoreOverrideCursor()

        if invoice.warnings:
            QMessageBox.warning(
                self, "Счёт PDF",
                f"Счёт переведён в {os.path.basename(target)}, но есть расхождения:\n\n"
                + "\n".join(f"• {text}" for text in invoice.warnings))
        else:
            self._announce(f"Счёт переведён в Excel: {os.path.basename(target)} · "
                           f"{len(invoice.rows)} строк", ToastKind.SUCCESS)
        return target

    def download_excel(self) -> None:
        """Копия переведённой книги — по умолчанию в «Загрузки»."""
        if not self._pdf_source or not os.path.exists(self._path):
            return
        folder = (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
                  or os.path.dirname(self._path))
        suggested = os.path.join(folder, os.path.basename(self._path))
        target, _ = QFileDialog.getSaveFileName(
            self, "Сохранить счёт в Excel", suggested, "Excel (*.xlsx)")
        if not target:
            return
        if not target.lower().endswith(".xlsx"):
            target += ".xlsx"
        try:
            if os.path.normcase(os.path.abspath(target)) != os.path.normcase(
                    os.path.abspath(self._path)):
                shutil.copyfile(self._path, target)
        except OSError as error:
            QMessageBox.warning(
                self, "Счёт PDF",
                f"Не удалось сохранить {os.path.basename(target)}: {error}\n\n"
                "Если файл с таким именем открыт в Excel, закройте его.")
            return
        self._announce(f"Счёт сохранён: {target}", ToastKind.SUCCESS)

    def _announce(self, text: str, kind: ToastKind) -> None:
        # Подпись над полем страница сразу перепишет своим «чтение файла…»,
        # поэтому о переводе сообщает всплывающее уведомление главного окна.
        if notify := getattr(self.window(), "notify", None):
            notify(text, kind)

    def browse(self) -> None:
        start = os.path.dirname(self._path) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл", start, self._filter)
        if path:
            self.set_path(path)

    def _on_typed(self) -> None:
        text = self.field.text().strip('" ')
        if text and text != self._path and os.path.exists(text):
            self.set_path(text)

    def _on_sheet(self, name: str) -> None:
        if name:
            self.sheet_changed.emit(name)

    def _show_recent(self) -> None:
        if not self._recent:
            return
        menu = QMenu(self)
        for path in self._recent:
            action = menu.addAction(f"{os.path.basename(path)}   —   {os.path.dirname(path)}")
            action.setIcon(icons.icon("file"))
            action.triggered.connect(lambda _=False, p=path: self.set_path(p))
        menu.exec(self.recent_button.mapToGlobal(self.recent_button.rect().bottomLeft()))

    def _set_sheet_visible(self, visible: bool) -> None:
        self._sheet_label.setVisible(visible)
        self.sheet_box.setVisible(visible)
        # Строка листа появляется уже после первичной раскладки, поэтому
        # родительскую карточку нужно пересчитать, иначе она обрежет поля.
        self.updateGeometry()
        if parent := self.parentWidget():
            parent.updateGeometry()

    def dragEnterEvent(self, event) -> None:
        if self._url_from(event) is not None:
            event.acceptProposedAction()
            self.setStyleSheet(f"QFrame {{ border: 1px dashed {Palette.PRIMARY}; border-radius: {Metrics.RADIUS_SM}px; }}")

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")

    def dropEvent(self, event) -> None:
        self.setStyleSheet("")
        if path := self._url_from(event):
            self.set_path(path)
            event.acceptProposedAction()

    def _url_from(self, event) -> str | None:
        data = event.mimeData()
        if not data.hasUrls():
            return None
        accepted = _EXCEL + ((".pdf",) if self._accepts_pdf else ())
        for url in data.urls():
            path = url.toLocalFile()
            if path.lower().endswith(accepted):
                return path
        return None
