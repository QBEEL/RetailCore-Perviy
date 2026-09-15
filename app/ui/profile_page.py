"""Личный кабинет: кто вошёл, что за ним закреплено и как сменить пароль.

Страница отвечает на вопросы, которые раньше задавать было некому: под кем я
работаю, какие оплаты считаются моими, за какое направление я отвечаю и
сколько поставщиков за мной числится. До общей базы всё это не имело смысла —
программа была одна на одного человека.

Показатели читаются с сервера в фоне и обновляются при каждом открытии:
закрепление меняет администратор, и цифра, показанная при запуске, к обеду
может устареть.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.payments import transport
from ..core.settings import AppSettings
from ..core.suppliers import directory
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Badge, Card, Divider, Hint, MetricTile, SectionTitle, Subtitle, Title
from .widgets.toast import ToastKind


class ProfilePage(QWidget):
    """Личный кабинет пользователя."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
        on_sign_out: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._on_sign_out = on_sign_out
        self._badges: list[QWidget] = []
        self._build()
        self.refresh()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 8, Metrics.PAD + 4,
                                Metrics.PAD + 8, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(Title("Личный кабинет", self))
        self.subtitle = Subtitle("", self)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)

        self.password_button = QPushButton("Сменить пароль", self)
        self.password_button.setIcon(icons.icon("key"))
        self.password_button.clicked.connect(self.change_password)
        head.addWidget(self.password_button)

        self.sign_out_button = QPushButton("Выйти", self)
        self.sign_out_button.setObjectName("Danger")
        self.sign_out_button.clicked.connect(self.sign_out)
        head.addWidget(self.sign_out_button)
        root.addLayout(head)

        root.addWidget(self._metrics_row())
        root.addWidget(self._account_card())
        root.addWidget(self._about_card())
        root.addStretch(1)

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_suppliers = MetricTile("Поставщиков за мной", Palette.PRIMARY, row)
        self.tile_drafts = MetricTile("Ждут подтверждения", Palette.WARNING, row)
        self.tile_conflicts = MetricTile("Платил чужим", Palette.INFO, row)
        self.tile_names = MetricTile("Имён в выгрузке 1С", Palette.TEXT_MUTED, row)
        for tile in (self.tile_suppliers, self.tile_drafts,
                     self.tile_conflicts, self.tile_names):
            layout.addWidget(tile)
        return row

    def _account_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Учётная запись", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        self.value_name = self._row(grid, 0, "ФИО")
        self.value_login = self._row(grid, 1, "Логин")
        self.value_role = self._row(grid, 2, "Права")
        body.addLayout(grid)

        body.addWidget(Divider(card))
        body.addWidget(SectionTitle("Направления", card))
        body.addWidget(Hint(
            "Направление задаёт администратор. По нему вкладка «Поставщики» "
            "отбирает список при открытии — но чужое направление всегда можно "
            "посмотреть переключателем.", card))
        self.directions_row = QHBoxLayout()
        self.directions_row.setSpacing(6)
        self.directions_row.addStretch(1)
        body.addLayout(self.directions_row)

        body.addWidget(Divider(card))
        body.addWidget(SectionTitle("Мои имена в выгрузке 1С", card))
        body.addWidget(Hint(
            "Оплаты с этими ответственными считаются вашими — их можно "
            "править. Один человек попадает в выгрузку под несколькими "
            "написаниями; если своего имени здесь нет, скажите администратору.",
            card))
        self.names_label = QLabel("", card)
        self.names_label.setWordWrap(True)
        body.addWidget(self.names_label)
        return card

    def _about_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Подключение", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        self.value_server = self._row(grid, 0, "Сервер")
        self.value_session = self._row(grid, 1, "Вход действует до")
        self.value_version = self._row(grid, 2, "Версия программы")
        body.addLayout(grid)
        return card

    def _row(self, grid: QGridLayout, line: int, title: str) -> QLabel:
        caption = QLabel(title, self)
        caption.setObjectName("Hint")
        grid.addWidget(caption, line, 0, Qt.AlignmentFlag.AlignRight)
        value = QLabel("—", self)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(value, line, 1)
        return value

    # --- данные ---------------------------------------------------------------

    def restore(self) -> None:
        """Вызывается при открытии страницы."""
        self.refresh()

    def refresh(self) -> None:
        session = transport.session
        online = session.active

        self.subtitle.setText(
            f"{session.full_name} · вход выполнен" if online
            else "Работа без сервера: показатели и смена пароля недоступны")
        self.value_name.setText(session.full_name or "—")
        self.value_login.setText(session.login or "—")
        self.value_role.setText(
            "администратор" if session.is_admin else
            "категорийный менеджер" if session.directions else "менеджер")
        self.value_server.setText(session.base_url or "—")
        self.value_session.setText(
            f"{session.expires_at:%d.%m.%Y %H:%M}" if session.expires_at else "—")
        self.value_version.setText(__version__)

        names = ", ".join(session.responsible)
        self.names_label.setText(names or "имена не привязаны — оплаты править нельзя")
        self.tile_names.set_value(len(session.responsible))

        self._fill_directions(session.directions)
        for button in (self.password_button, self.sign_out_button):
            button.setEnabled(online)
        if online:
            self._load_counters()
        else:
            for tile in (self.tile_suppliers, self.tile_drafts, self.tile_conflicts):
                tile.set_value("—")

    def _fill_directions(self, codes: tuple[str, ...]) -> None:
        while self._badges:
            badge = self._badges.pop()
            self.directions_row.removeWidget(badge)
            badge.setParent(None)
            badge.deleteLater()
        if not codes:
            hint = QLabel("направление не задано — поставщиков вы не ведёте", self)
            hint.setObjectName("Hint")
            self.directions_row.insertWidget(0, hint)
            self._badges.append(hint)
            return
        for index, code in enumerate(codes):
            badge = Badge(code.capitalize(), Palette.PRIMARY,
                          Palette.PRIMARY_SOFT, self)
            self.directions_row.insertWidget(index, badge)
            self._badges.append(badge)

    def _load_counters(self) -> None:
        """Показатели с сервера. Их отсутствие не должно ломать страницу."""
        run_task(_counters, on_result=self._show_counters,
                 on_error=lambda _: None)

    def _show_counters(self, payload: tuple[int, int, int]) -> None:
        mine, drafts, conflicts = payload
        self.tile_suppliers.set_value(mine)
        self.tile_drafts.set_value(drafts)
        self.tile_conflicts.set_value(conflicts)

    # --- действия -------------------------------------------------------------

    def change_password(self) -> None:
        from .widgets.password_dialog import PasswordDialog

        if PasswordDialog(parent=self).exec() == QDialog.DialogCode.Accepted:
            self.notify("Пароль изменён", ToastKind.SUCCESS)
            self.refresh()

    def sign_out(self) -> None:
        answer = QMessageBox.question(
            self, "Выйти из учётной записи",
            "Выйти? Программа закроется — работать без входа нельзя.\n\n"
            "При следующем запуске потребуется пароль.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._on_sign_out is not None:
            self._on_sign_out()


def _counters() -> tuple[int, int, int]:
    """Мои поставщики, мои неподтверждённые заявки и оплаты чужим.

    Одним походом и в фоне: три коротких запроса подряд из интерфейса — это
    три повода подождать на странице, которая должна открываться мгновенно.
    """
    me = transport.session.user_id
    if not me:
        return 0, 0, 0
    mine = directory.suppliers(manager=me, page_size=1).total
    drafts = sum(
        1 for entry in directory.suppliers(manager=me, state="draft",
                                           page_size=500).items
        if any(person.user_id == me and not person.fixed
               for person in entry.assigned))
    return mine, drafts, len(directory.conflicts())
