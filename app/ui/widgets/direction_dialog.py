"""Ручное направление поставщика — только для администратора.

Направление поставщика обычно выводится само: из направлений менеджеров, за
которыми он закреплён. Но так не размечаются ни поставщики без закрепления, ни
расходы — аренду, налоги и маркетинг никто не «ведёт». Здесь администратор
задаёт направление руками, и пересчёт закреплений его больше не трогает.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ...core.suppliers.directory import Direction
from ..theme import Metrics
from .common import Hint, SectionTitle


class DirectionDialog(QDialog):
    """Отделы и статьи расхода флажками; «По закреплению» снимает ручное."""

    def __init__(self, names: list[str], directions: list[Direction],
                 chosen: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Сбросить ручное — отдельная кнопка, а не «ничего не отмечено»: пустой
        # набор флажков легко получить случайно, а возврат к расчёту по
        # закреплению — осознанное решение, и оно должно так и выглядеть.
        self.reset = False
        self.setWindowTitle("Направление поставщика")
        self.setMinimumWidth(460)
        self.boxes: list[tuple[str, QCheckBox]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        shown = ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
        root.addWidget(SectionTitle(shown or "Поставщик", self))
        root.addWidget(Hint(
            "Отдел — чьи это деньги, расход — на что они ушли. Заданное здесь "
            "закрепление за менеджером больше не пересчитывает. Статья расхода "
            "важнее отдела: в отборе оплат такой поставщик попадёт в неё.", self))

        for title, group in (
            ("Отдел", [d for d in directions if d.for_people]),
            ("Расход", [d for d in directions if not d.for_people]),
        ):
            if not group:
                continue
            root.addWidget(SectionTitle(title, self))
            row = QHBoxLayout()
            row.setSpacing(Metrics.GAP)
            for item in group:
                box = QCheckBox(item.title, self)
                box.setChecked(item.code in chosen)
                row.addWidget(box)
                self.boxes.append((item.code, box))
            row.addStretch(1)
            root.addLayout(row)

        if len(names) > 1:
            root.addWidget(Hint(
                "Отмечено то, что уже стоит у всех выделенных. Сохранение "
                "задаст отмеченное каждому из них целиком.", self))

        buttons = QDialogButtonBox(self)
        self.save = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.save.setText("Сохранить")
        self.save.setObjectName("Primary")
        auto = buttons.addButton("По закреплению", QDialogButtonBox.ButtonRole.ResetRole)
        auto.setToolTip("Снять ручное направление: оно снова будет считаться "
                        "по направлениям закреплённых менеджеров")
        auto.clicked.connect(self._reset)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        for _, box in self.boxes:
            box.toggled.connect(self._update_save)
        self._update_save()

    def _update_save(self) -> None:
        # Пустой выбор сервер понимает как «по закреплению». Сохранять его
        # этой кнопкой нельзя — для этого своя, иначе снятые по ошибке флажки
        # молча вернули бы поставщика под автоматическое правило.
        self.save.setEnabled(any(box.isChecked() for _, box in self.boxes))

    def _reset(self) -> None:
        self.reset = True
        self.accept()

    def codes(self) -> list[str]:
        """Выбранные коды. Пустой список серверу означает «по закреплению»."""
        if self.reset:
            return []
        return [code for code, box in self.boxes if box.isChecked()]
