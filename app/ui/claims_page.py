"""Торг-2: недопоставки и браки.

Склад заводит заявку, категорийный менеджер (КМ) отвечает, закрывают оба.
Закрытые заявки уходят в архив — это вкладка того же списка, а не отдельное
место: вернуть ошибочно закрытую заявку из архива можно, потеряв ничего.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable

from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from ..core.claims import (
    Brand,
    Claim,
    Manager,
    TorgAct,
    TorgSettings,
    expiry_label,
    expiry_level,
    remote,
    torg,
)
from ..core.payments import transport
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.claim_dialogs import ClaimDialog, CloseDialog
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title, fade_in
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind
from .widgets.torg_dialogs import BrandsDialog, TorgDialog, TorgSettingsDialog

TAB_MINE, TAB_OPEN, TAB_ARCHIVE = range(3)

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

_EXPIRY_COLORS = {
    "expired": Palette.DANGER,
    "soon": Palette.WARNING,
}
_STATUS_COLORS = {
    "new": Palette.TEXT_MUTED,
    "accepted": Palette.SUCCESS,
    "closed": Palette.TEXT_FAINT,
}


class ClaimsPage(QWidget):
    """Список заявок с вкладками «Мои», «Все открытые» и «Архив»."""

    # Что-то изменилось у адресата: главное окно перечитывает «непрочитанное».
    inbox_changed = Signal()

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._items: list[Claim] = []
        self._managers: list[Manager] = []
        self._brands: list[Brand] = []
        self._loading = False
        self._last_dir = ""
        self._build()

    # --- разметка -------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Торг-2", self))
        root.addWidget(Subtitle(
            "Недопоставки и браки. Склад заводит заявку и прикладывает акт, "
            "категорийный менеджер отвечает, закрывают оба — закрытые уходят в архив.",
            self))

        card = Card(self)
        body = card.body()

        body.addWidget(self._build_pending_banner(card))

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Заявки", card))
        header.addStretch(1)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск: бренд, товар, штрих-код")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self._filter)
        # В архиве за раз приходят только последние заявки; остальное находится
        # поиском на сервере по Enter.
        self.search.returnPressed.connect(self._search_archive)
        header.addWidget(self.search)

        body.addLayout(header)

        # Кнопки — вторым рядом: в одну строку с поиском они не помещаются в
        # окне минимальной ширины.
        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.new_button = self._action(card, "Новая заявка", "plus", self.create)
        self.new_button.setObjectName("Primary")
        self.open_button = self._action(card, "Открыть", "card", self.open_selected)
        self.accept_button = self._action(card, "Принять в работу", "check", self.accept_selected)
        self.close_button = self._action(card, "Закрыть заявку", "folder", self.close_selected)
        self.torg_button = self._action(card, "Составить ТОРГ-2", "file", self.compose_torg)
        self.torg_button.setToolTip(
            "Акт ТОРГ-2 по выделенным заявкам — готовый файл Excel, он сразу "
            "приложится к заявкам. Выделить несколько: Ctrl или Shift + щелчок.")
        self.reopen_button = self._action(card, "Вернуть из архива", "reset", self.reopen_selected)
        self.delete_button = self._action(card, "Удалить", "trash", self.delete_selected)
        self.delete_button.setObjectName("Danger")
        for button in (self.new_button, self.open_button, self.accept_button,
                       self.close_button, self.torg_button, self.reopen_button,
                       self.delete_button):
            actions.addWidget(button)
        actions.addStretch(1)
        refresh = self._action(card, "Обновить", "refresh", self.restore)
        actions.addWidget(refresh)
        self.more_button = QPushButton("Ещё", card)
        self.more_button.setIcon(icons.icon("columns"))
        menu = QMenu(self.more_button)
        menu.addAction("Бренды…", self.edit_brands)
        menu.addAction("Реквизиты ТОРГ-2…", self.edit_torg_settings)
        self.more_button.setMenu(menu)
        actions.addWidget(self.more_button)
        body.addLayout(actions)

        self.tabs = QTabBar(card)
        self.tabs.setExpanding(False)
        for title in ("Мои", "Все открытые", "Архив"):
            self.tabs.addTab(title)
        self.tabs.setCurrentIndex(TAB_OPEN)
        self.tabs.currentChanged.connect(self._tab_changed)
        body.addWidget(self.tabs)

        self.summary = Hint("", card)
        body.addWidget(self.summary)

        self.table = DataTable(self._columns(), card)
        self.table.item_activated.connect(self._open)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.model_.set_row_tint(self._tint)
        body.addWidget(self.table, 1)
        root.addWidget(card, 1)

        self._update_actions()

    def _build_pending_banner(self, parent: QWidget) -> QFrame:
        """Полоса над списком, пока есть заявки, на которые КМ ещё не ответил.

        Всплывашка в углу исчезает через несколько секунд, а цифра в меню
        теряется среди прочего. Полоса остаётся на месте, пока КМ не нажмёт
        «Принять в работу» по каждой заявке, — забыть о заявке из-за закрытого
        уведомления нельзя.
        """
        banner = QFrame(parent)
        banner.setStyleSheet(
            f"QFrame {{ background: {Palette.WARNING_SOFT};"
            f" border: 1px solid {Palette.WARNING}; border-radius: 8px; }}"
            " QLabel { border: none; background: transparent; }")
        row = QHBoxLayout(banner)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(Metrics.GAP)
        self.pending_label = QLabel("", banner)
        self.pending_label.setWordWrap(True)
        row.addWidget(self.pending_label, 1)
        show = QPushButton("Показать", banner)
        show.setToolTip("Открыть вкладку «Мои» — там заявки, адресованные вам")
        show.clicked.connect(lambda _checked=False: self.show_pending())
        row.addWidget(show)
        banner.hide()
        self.pending_banner = banner
        return banner

    def set_pending(self, count: int) -> None:
        """Сколько заявок ждут ответа вошедшего КМ (считает сервер)."""
        self.pending_banner.setVisible(count > 0)
        if count > 0:
            self.pending_label.setText(
                f"Ждут вашего ответа: {count}. Откройте заявку и нажмите «Принять в "
                "работу» — пока этого не сделано, напоминание остаётся.")

    def show_pending(self) -> None:
        if self.tabs.currentIndex() != TAB_MINE:
            self.tabs.setCurrentIndex(TAB_MINE)
        else:
            self.restore()

    def _action(self, parent: QWidget, title: str, icon: str, handler) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(lambda _checked=False: handler())
        return button

    @property
    def archive(self) -> bool:
        return self.tabs.currentIndex() == TAB_ARCHIVE

    def _columns(self) -> list[Column]:
        columns = [
            Column("Дата", lambda c: f"{c.created_at:%d.%m.%Y}" if c.created_at else "", 100,
                   sort_key=lambda c: c.created_at.timestamp() if c.created_at else 0),
            Column("Вид", lambda c: c.kind_title, 110),
            Column("Бренд", lambda c: c.brand, 130, highlight=True),
            Column("Номенклатура", lambda c: c.product, 280, highlight=True),
            Column("Хар-ка", lambda c: c.spec, 110),
            Column("Штрих-код", lambda c: c.barcode, 130, highlight=True),
            Column("Маркировка", lambda c: "код есть" if c.mark_code else "", 100),
            Column("Кол-во", lambda c: c.quantity_text(), 80,
                   sort_key=lambda c: c.quantity, align=_RIGHT),
            Column("Срок годности",
                   lambda c: f"{c.expiry_date:%d.%m.%Y}" if c.expiry_date else "", 110,
                   sort_key=lambda c: c.expiry_date.toordinal() if c.expiry_date else 0),
            Column("До окончания", lambda c: expiry_label(c.expiry_date), 120,
                   sort_key=lambda c: c.expiry_date.toordinal() if c.expiry_date else 10**9,
                   color=lambda c: self._expiry_color(c)),
            Column("Заявитель", lambda c: c.applicant, 150),
            Column("КМ", lambda c: c.manager or "не назначен", 150,
                   color=lambda c: None if c.manager else QColor(Palette.WARNING)),
            Column("Ответ КМ", lambda c: c.status_title, 140,
                   color=lambda c: QColor(_STATUS_COLORS.get(c.status, Palette.TEXT))),
            Column("ТОРГ", self._torg_text, 130,
                   sort_key=lambda c: (c.act_number or 0, c.files)),
        ]
        if self.archive:
            columns += [
                Column("Закрыта", lambda c: f"{c.closed_at:%d.%m.%Y}" if c.closed_at else "", 100,
                       sort_key=lambda c: c.closed_at.timestamp() if c.closed_at else 0),
                Column("Итог", lambda c: c.resolution_title, 170),
                Column("Закрыл", lambda c: c.closed_by, 140),
            ]
        return columns

    @staticmethod
    def _torg_text(claim: Claim) -> str:
        """Акт и файлы заявки: «акт №62 · файлов: 1»."""
        parts = []
        if claim.act_number:
            parts.append(f"акт №{claim.act_number}")
        if claim.files:
            parts.append(f"файлов: {claim.files}")
        return " · ".join(parts)

    @staticmethod
    def _expiry_color(claim: Claim) -> QColor | None:
        # В архиве срок уже не горит: заявку решили.
        if claim.is_closed:
            return None
        color = _EXPIRY_COLORS.get(expiry_level(claim.expiry_date))
        return QColor(color) if color else None

    @staticmethod
    def _tint(claim: Claim) -> QColor | None:
        """Адресованное мне и ещё без ответа — заметно с первого взгляда."""
        if ClaimsPage._waits_for_me(claim):
            return QColor(Palette.PRIMARY_SOFT)
        return None

    @staticmethod
    def _waits_for_me(claim: Claim) -> bool:
        """Как считает сервер (`/inbox`): не закрыта, адресована мне, я на неё не
        ответил — или её вернули из архива, а я об этом ещё не знаю."""
        mine = claim.manager_id and claim.manager_id == transport.session.user_id
        return bool(mine and not claim.is_closed
                    and claim.applicant_id != claim.manager_id
                    and (claim.status == "new" or not claim.manager_seen))

    # --- данные ---------------------------------------------------------------

    def restore(self) -> None:
        """Читает список заново: заявки заводят и закрывают другие люди."""
        self._load(self.search.text().strip() if self.archive else "")

    def _load(self, text: str = "") -> None:
        if self._loading:
            return
        self._loading = True
        tab = self.tabs.currentIndex()
        run_task(_fetch, tab, text, on_result=self._apply, on_error=self._load_failed)

    def _load_failed(self, message: str) -> None:
        self._loading = False
        self.notify(message, ToastKind.ERROR)

    def _apply(self, result: tuple[int, list[Claim], list[Manager], list[Brand]]) -> None:
        self._loading = False
        tab, items, managers, brands = result
        if tab != self.tabs.currentIndex():
            # Пока читалось, человек перешёл на другую вкладку: этот ответ уже
            # не про то, что на экране.
            self.restore()
            return
        if managers:
            self._managers = managers
        if brands:
            # Список меняется на месте: окно брендов держит ссылку на него.
            self._brands[:] = brands
        self._items = items
        self.table.set_columns(self._columns())
        self.table.set_items(items)
        self.table.proxy.set_text(self.search.text())
        self._update_summary()
        self._update_actions()
        fade_in(self.table)

    def _update_summary(self) -> None:
        total = len(self._items)
        if not total:
            self.summary.setText(
                "Заявок в архиве нет." if self.archive else
                "Открытых заявок нет. Новую можно завести кнопкой справа.")
            return
        if self.archive:
            self.summary.setText(
                f"В архиве: {total}. Показаны последние закрытые; остальное "
                "находится поиском — наберите слово и нажмите Enter.")
            return
        waiting = sum(1 for c in self._items if c.status == "new")
        nobody = sum(1 for c in self._items if c.manager_id is None)
        text = f"Открыто: {total} · ждут ответа КМ: {waiting}"
        if nobody:
            text += f" · без КМ: {nobody}"
        self.summary.setText(text)

    def _tab_changed(self, _index: int) -> None:
        self.restore()

    def _filter(self, text: str) -> None:
        self.table.proxy.set_text(text)
        self.table.model_.set_terms(text.split())

    def _search_archive(self) -> None:
        if self.archive:
            self._load(self.search.text().strip())

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    # --- состояние кнопок -----------------------------------------------------

    def _current(self) -> Claim | None:
        item = self.table.current_item()
        return item if isinstance(item, Claim) else None

    def _update_actions(self, *_args: Any) -> None:
        claim = self._current()
        selected = self._selected()
        acceptable = [c for c in selected if c.can_accept]
        closable = [c for c in selected if c.can_close]
        self.open_button.setEnabled(claim is not None)
        # Принять и закрыть можно сразу несколько выделенных: Ctrl или Shift +
        # щелчок. В подписи — сколько заявок затронет нажатие.
        self.accept_button.setEnabled(bool(acceptable))
        self.accept_button.setText(
            f"Принять в работу ({len(acceptable)})" if len(acceptable) > 1
            else "Принять в работу")
        self.close_button.setEnabled(bool(closable))
        self.close_button.setText(
            f"Закрыть заявки ({len(closable)})" if len(closable) > 1
            else "Закрыть заявку")
        self.delete_button.setEnabled(bool(claim and claim.can_delete))
        self.torg_button.setEnabled(bool(self._selected()))
        # «Вернуть из архива» видна только там, где имеет смысл, и только тому,
        # кто вправе: пустая серая кнопка у остальных вкладок — это шум.
        self.reopen_button.setVisible(self.archive and transport.session.is_admin)
        self.reopen_button.setEnabled(bool(claim and claim.can_reopen))

    # --- действия -------------------------------------------------------------

    def create(self) -> None:
        dialog = ClaimDialog(None, self._managers, self._brands, self, settings=self.settings)
        if not dialog.exec():
            return
        run_task(_create_many, dialog.claims_fields(), list(dialog.staged),
                 list(dialog.new_brands),
                 on_result=self._created, on_error=self._failed)

    def _created(self, result: tuple[list[Claim], list[str]]) -> None:
        claims, failures = result
        if len(claims) == 1:
            self.notify(f"Заявка заведена: {claims[0].title}", ToastKind.SUCCESS)
        else:
            self.notify(f"Заведено заявок: {len(claims)}", ToastKind.SUCCESS)
        for failure in failures:
            self.notify(failure, ToastKind.ERROR)
        self._changed()
        if len(claims) > 1 and QMessageBox.question(
                self, "ТОРГ-2",
                f"Заведено заявок: {len(claims)}.\n\nСоставить ТОРГ-2 по этим заявкам "
                "сейчас? Акт можно составить и позже: выделите заявки и нажмите "
                "«Составить ТОРГ-2».") == QMessageBox.StandardButton.Yes:
            run_task(_prepare_act, claims, None,
                     on_result=self._edit_act, on_error=self._failed)

    def open_selected(self) -> None:
        if claim := self._current():
            self._open(claim)

    def _open(self, claim: Claim) -> None:
        if self._is_unseen_for_me(claim):
            # Открыл — значит, узнал: уведомление о ней больше не нужно.
            claim.manager_seen = True
            self.table.model_.refresh_item(claim)
            run_task(remote.mark_seen, claim.id, on_result=lambda _r: self.inbox_changed.emit(),
                     on_error=lambda _m: None)
        dialog = ClaimDialog(claim, self._managers, self._brands, self, settings=self.settings)
        if not dialog.exec() or not claim.can_edit:
            return
        if changes := dialog.changes():
            run_task(_update, claim.id, changes, list(dialog.new_brands),
                     on_result=lambda _r: self._done("Заявка сохранена"),
                     on_error=self._failed)

    @staticmethod
    def _is_unseen_for_me(claim: Claim) -> bool:
        return bool(claim.manager_id and claim.manager_id == transport.session.user_id
                    and not claim.manager_seen and not claim.is_closed)

    def accept_selected(self) -> None:
        targets = [c for c in self._selected() if c.can_accept]
        if not targets:
            self.notify("Выделите заявки, которые нужно принять в работу", ToastKind.WARNING)
            return
        run_task(_accept_many, [c.id for c in targets],
                 on_result=self._accepted, on_error=self._failed)

    def _accepted(self, result: tuple[int, list[str]]) -> None:
        done, failures = result
        if done:
            self.notify("Принято в работу" if done == 1 else f"Принято в работу заявок: {done}",
                        ToastKind.SUCCESS)
        for failure in failures:
            self.notify(failure, ToastKind.ERROR)
        self._changed()

    def close_selected(self) -> None:
        targets = [c for c in self._selected() if c.can_close]
        if not targets:
            self.notify("Выделите заявки, которые нужно закрыть", ToastKind.WARNING)
            return
        dialog = CloseDialog(targets[0], self, count=len(targets))
        if not dialog.exec():
            return
        resolution, note = dialog.value()
        run_task(_close_many, [c.id for c in targets], resolution, note,
                 on_result=self._closed, on_error=self._failed)

    def _closed(self, result: tuple[int, list[str]]) -> None:
        done, failures = result
        if done:
            self.notify("Заявка закрыта и ушла в архив" if done == 1
                        else f"Закрыто заявок: {done}, они ушли в архив",
                        ToastKind.SUCCESS)
        for failure in failures:
            self.notify(failure, ToastKind.ERROR)
        self._changed()

    def reopen_selected(self) -> None:
        if not (claim := self._current()):
            return
        if QMessageBox.question(
                self, "Вернуть из архива",
                f"Вернуть заявку «{claim.title}» в работу?\n\nИтог закрытия сотрётся, "
                "КМ получит уведомление.") != QMessageBox.StandardButton.Yes:
            return
        run_task(remote.reopen_claim, claim.id,
                 on_result=lambda _r: self._done("Заявка возвращена в работу"),
                 on_error=self._failed)

    def delete_selected(self) -> None:
        if not (claim := self._current()):
            return
        if QMessageBox.question(
                self, "Удалить заявку",
                f"Удалить заявку «{claim.title}» вместе с файлами?\n\n"
                "Это нельзя отменить. Если вопрос решён, заявку лучше закрыть.",
                ) != QMessageBox.StandardButton.Yes:
            return
        run_task(remote.delete_claim, claim.id,
                 on_result=lambda _r: self._done("Заявка удалена"),
                 on_error=self._failed)

    # --- ТОРГ-2, бренды, реквизиты --------------------------------------------

    def _selected(self) -> list[Claim]:
        return [c for c in self.table.selected_items() if isinstance(c, Claim)]

    def compose_torg(self) -> None:
        """Акт ТОРГ-2 по выделенным заявкам: новый или уже составленный."""
        selected = self._selected()
        if not selected:
            self.notify("Выделите заявки, по которым составляется акт", ToastKind.WARNING)
            return
        acts = {claim.act_id for claim in selected}
        if len(acts) > 1:
            self.notify(
                "Выделены заявки из разных актов или часть из них без акта. Выделите "
                "заявки одного акта, чтобы открыть его, или только заявки без акта.",
                ToastKind.WARNING)
            return
        act_id = acts.pop()
        if act_id is None and any(claim.is_closed for claim in selected):
            self.notify("Закрытые заявки в новый акт не добавляются", ToastKind.WARNING)
            return
        run_task(_prepare_act, selected, act_id,
                 on_result=self._edit_act, on_error=self._failed)

    def _edit_act(self, prepared: tuple[TorgAct, TorgSettings, list[dict]]) -> None:
        act, settings, suppliers = prepared
        dialog = TorgDialog(act, settings, suppliers, self)
        if not dialog.exec():
            return
        result = dialog.act_snapshot()
        folder = self._last_dir or QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить акт ТОРГ-2", os.path.join(folder, torg.file_name(result)),
            "Excel (*.xlsx)")
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        run_task(_make_act, result, dialog.settings, dialog.settings_changed, path,
                 on_result=self._act_done, on_error=self._failed)

    def _act_done(self, result: tuple[TorgAct, str, list[str]]) -> None:
        act, path, failures = result
        self.notify(f"Акт ТОРГ-2 №{act.number} составлен и приложен к заявкам",
                    ToastKind.SUCCESS)
        for failure in failures:
            self.notify(failure, ToastKind.ERROR)
        try:
            os.startfile(path)  # noqa: S606 — открыть готовый файл в Excel
        except (OSError, AttributeError):
            pass
        self._changed()

    def edit_brands(self) -> None:
        BrandsDialog(self._brands, transport.session.is_admin, self).exec()

    def edit_torg_settings(self) -> None:
        run_task(remote.torg_settings, on_result=self._settings_dialog,
                 on_error=self._failed)

    def _settings_dialog(self, settings: TorgSettings) -> None:
        dialog = TorgSettingsDialog(settings, self)
        if not dialog.exec():
            return
        run_task(remote.save_torg_settings, dialog.value(),
                 on_result=lambda _r: self.notify("Реквизиты ТОРГ-2 сохранены", ToastKind.SUCCESS),
                 on_error=self._failed)

    def _done(self, text: str) -> None:
        self.notify(text, ToastKind.SUCCESS)
        self._changed()

    def _changed(self) -> None:
        self.restore()
        self.inbox_changed.emit()

    def _failed(self, message: str) -> None:
        self.notify(message, ToastKind.ERROR)
        # Отказ мог быть из-за того, что заявку за это время изменили: список
        # перечитывается, чтобы человек видел её настоящее состояние.
        self.restore()


def _fetch(tab: int, text: str) -> tuple[int, list[Claim], list[Manager], list[Brand]]:
    claims = remote.list_claims(archive=tab == TAB_ARCHIVE, mine=tab == TAB_MINE, text=text)
    try:
        managers = remote.managers()
    except transport.ServerError:
        # Список КМ нужен только окну заявки; без него страница работает.
        managers = []
    try:
        brands = remote.brands()
    except transport.ServerError:
        # Справочник брендов — подсказка; без него бренд вводится руками.
        brands = []
    return tab, claims, managers, brands


def _each(ids: list[int], action, verb: str) -> tuple[int, list[str]]:
    """Применяет действие к каждой заявке по очереди и собирает отказы.

    Отказ по одной заявке (её за это время закрыли или переназначили) не
    отменяет остальные: они независимы, а уже принятое откатывать нечем.
    Если отказали все, ошибка поднимается наверх — иначе человек увидел бы
    тихое «ничего не сделано».
    """
    done = 0
    failures: list[str] = []
    for claim_id in ids:
        try:
            action(claim_id)
        except transport.ServerError as failure:
            failures.append(f"Заявка №{claim_id} не {verb}: {failure}")
        else:
            done += 1
    if not done and failures:
        if len(failures) == 1:
            raise transport.ServerError(failures[0])
        raise transport.ServerError(
            f"{failures[0]} (и ещё отказов: {len(failures) - 1})")
    return done, failures


def _accept_many(ids: list[int]) -> tuple[int, list[str]]:
    return _each(ids, remote.accept_claim, "принята")


def _close_many(ids: list[int], resolution: str, note: str) -> tuple[int, list[str]]:
    return _each(ids, lambda claim_id: remote.close_claim(claim_id, resolution, note),
                 "закрыта")


def _add_brands(names: list[str]) -> None:
    """Добавляет бренды в справочник. Отказ не мешает заявке: бренд в ней и так
    записан текстом, а в список его можно добавить позже."""
    for name in names:
        try:
            remote.add_brand(name)
        except transport.ServerError:
            pass


def _update(claim_id: int, changes: dict[str, Any], new_brands: list[str]) -> Claim:
    _add_brands(new_brands)
    return remote.update_claim(claim_id, changes)


def _create_many(fields_list: list[dict[str, Any]], files: list[str],
                 new_brands: list[str] | None = None) -> tuple[list[Claim], list[str]]:
    """Заводит заявку на каждую позицию поставки и прикладывает файлы к каждой.

    Отказ по одной позиции не отменяет остальные: они независимы, и уже
    заведённые заявки откатывать нечем и незачем. Если не заведена ни одна,
    ошибка поднимается наверх — иначе человек увидел бы «заведено: 0».
    Отказ по файлу не отменяет заявку: файл можно добавить из карточки.
    """
    _add_brands(new_brands or [])
    claims: list[Claim] = []
    failures: list[str] = []
    for number, fields in enumerate(fields_list, 1):
        try:
            claim = remote.create_claim(fields)
        except transport.ServerError as failure:
            if not claims and number == len(fields_list):
                raise
            failures.append(f"Позиция {number} не заведена: {failure}")
            continue
        claims.append(claim)
        for path in files:
            try:
                remote.attach_file(claim.id, path)
            except (OSError, ValueError, transport.ServerError) as failure:
                failures.append(f"Файл «{path}» не приложен к заявке №{claim.id}: {failure}")
    if not claims:
        raise transport.ServerError(failures[0] if failures else "Заявки не заведены")
    return claims, failures


def _prepare_act(claims: list[Claim], act_id: int | None
                 ) -> tuple[TorgAct, TorgSettings, list[dict]]:
    """Всё, что нужно окну акта: сам акт (новый или с сервера), реквизиты и
    поставщики из прошлых актов. Читается в фоне — три запроса подряд."""
    settings = remote.torg_settings()
    try:
        suppliers = remote.torg_suppliers()
    except transport.ServerError:
        # Подсказка поставщиков — удобство; без неё акт составляется.
        suppliers = []
    if act_id:
        act = remote.get_act(act_id)
    else:
        today = date.today()
        act = torg.act_from_claims(claims, remote.next_act_number(today.year), today)
    return act, settings, suppliers


def _make_act(act: TorgAct, settings: TorgSettings, save_settings: bool, path: str
              ) -> tuple[TorgAct, str, list[str]]:
    """Сохраняет акт на сервере, собирает файл и прикладывает его к заявкам.

    Порядок важен: сначала акт на сервере, потом файл. Если бланк не собрался,
    акт уже сохранён — его можно открыть и повторить; а наоборот был бы файл
    на диске, о котором сервер не знает и номер которого мог занять другой.
    """
    if save_settings:
        remote.save_torg_settings(settings)
    saved = remote.save_act(act)
    torg.save(saved, settings, path)
    return saved, path, remote.publish_act_file(saved, path)
