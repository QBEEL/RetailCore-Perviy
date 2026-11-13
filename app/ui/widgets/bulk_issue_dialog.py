"""Выбор количества кодов по нескольким товарам одного заказа."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QSpinBox
from ...core.marking import issue
from .common import Hint
from .issue_dialogs import _buttons


class BulkIssueDialog(QDialog):
    def __init__(self, order, products, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Получить коды выбранных товаров")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(Hint("Укажите количество по каждому GTIN. Ноль — пропустить товар. "
                             "Полученные блоки сразу сохраняются на диск. "
                             "При ошибке получение останавливается; повтор автоматически не выполняется."))
        self.items = []
        table = QTableWidget(0, 4, self)
        table.setHorizontalHeaderLabels(["GTIN", "Название", "Доступно", "Получить"])
        for buffer in order.buffers:
            if buffer.left <= 0:
                continue
            product = products.get(buffer.gtin) or issue.Product(gtin=buffer.gtin)
            row = table.rowCount()
            table.insertRow(row)
            for col, text in enumerate((buffer.gtin, product.name, str(buffer.left))):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, col, item)
            count = QSpinBox(table)
            count.setRange(0, min(buffer.left, issue.MAX_PER_REQUEST))
            table.setCellWidget(row, 3, count)
            self.items.append((product, count))
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        layout.addWidget(_buttons(self, "Получить выбранные", "download"))

    @property
    def requests(self):
        return [(product, count.value()) for product, count in self.items if count.value()]

    def accept(self):
        if self.requests:
            super().accept()
