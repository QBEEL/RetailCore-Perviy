"""Свежие статусы выбранных марок из True API, отдельно от печати."""
from collections import Counter
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton
from ...core.marking import service
from ..tasks import run_task
from .common import Hint


class CodeStatusDialog(QDialog):
    def __init__(self, batches, contour, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Статусы марок в Честном знаке")
        self.resize(920, 600)
        self._codes = [code for batch in batches for code in batch.codes]
        self._contour = contour
        self._generation = 0
        layout = QVBoxLayout(self)
        self.summary = Hint("Статусы ещё не запрошены. Печать не означает нанесение марки.")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Код", "Статус ЧЗ", "Примечание"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.refresh = QPushButton("Обновить статусы", self)
        self.refresh.clicked.connect(self.load)
        layout.addWidget(self.refresh)

    def load(self):
        if not service.signed_in() or service.current().contour != self._contour:
            self.summary.setText("Войдите в Честный знак в контуре выбранных блоков.")
            return
        self._generation += 1
        generation = self._generation
        source = (service.current().inn, service.current().thumbprint, self._contour)
        self.refresh.setEnabled(False)
        self.table.setRowCount(0)
        self.summary.setText("Получаем свежие статусы…")

        def valid():
            return generation == self._generation and service.signed_in() and source == (
                service.current().inn, service.current().thumbprint, service.current().contour)

        def finish(items):
            self.refresh.setEnabled(True)
            if not valid():
                self.summary.setText("Сеанс изменился. Обновите статусы заново.")
                return
            counts = Counter()
            self.table.setRowCount(len(items))
            for row, item in enumerate(items):
                title = item.state.title if item.found else "Нет сведений"
                counts[title] += 1
                for column, text in enumerate((item.code, title, "; ".join(item.problems))):
                    cell = QTableWidgetItem(text)
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, column, cell)
            self.summary.setText(f"Проверено {datetime.now():%d.%m.%Y %H:%M:%S}. " +
                                 "; ".join(f"{key}: {value}" for key, value in counts.items()))
            self.table.resizeColumnsToContents()

        def fail(message):
            self.refresh.setEnabled(True)
            if valid():
                self.summary.setText(f"Статусы не получены: {message}")
            else:
                self.summary.setText("Сеанс изменился. Обновите статусы заново.")

        run_task(service.check, self._codes, contour=self._contour, use_cache=False,
                 on_result=finish, on_error=fail)

    def done(self, result):
        self._generation += 1
        super().done(result)
