"""Отчётность: сборка отчёта по акциям для поставщика из выгрузок продаж.

Заменяет ежемесячную ручную цепочку «сводная → удалить лишнее → сохранить под
именем месяца». Порядок на странице повторяет порядок работы: выбрать профиль,
набрать исходники, посмотреть, что получилось, сохранить файл.

Предпросмотр показывается до сохранения намеренно: отчёт уходит поставщику, и
последняя возможность заметить пропавший магазин или лишнюю акцию — здесь, а не
в отправленном письме.
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.payments import transport
from ..core.reports import DEFAULT_FOLDER, ReportProfile, SOURCE_EXTENSIONS, StoreRule
from ..core.reports import default_profile, pivot, service, store_from_name
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Card, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.report_dialogs import ProfileDialog, StoreRulesDialog
from .widgets.toast import ToastKind

# Сколько строк сводной показывать до сохранения. Больше двух сотен глазами
# всё равно не проверяют, а таблица на пять тысяч строк заметно тормозит.
PREVIEW_LIMIT = 200


class ReportsPage(QWidget):
    """Сборка отчёта поставщику: профиль, исходники, предпросмотр, выгрузка."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._profiles: list[ReportProfile] = []
        self._rules: list[StoreRule] = []
        self._paths: list[str] = []
        # Магазин, заданный руками для файла без колонки «Магазин».
        self._hints: dict[str, str] = {}
        self._result: service.BuildResult | None = None
        self._busy = False
        self._build()

    # --- разметка -------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6,
                                Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Отчётность", self))
        root.addWidget(Subtitle(
            "Отчёт по акциям для поставщика собирается из выгрузок продаж: фильтр, "
            "сводная и оформление берутся из профиля, а продажи объединённых "
            "магазинов складываются по общим правилам.", self))

        root.addWidget(self._profile_card())
        root.addWidget(self._sources_card())
        root.addWidget(self._result_card(), 1)

    def _profile_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Профиль отчёта", card))
        header.addStretch(1)
        self.rules_button = self._action(card, "Объединение магазинов", "link",
                                         self.edit_rules)
        self.edit_button = self._action(card, "Настроить", "settings", self.edit_profile)
        self.new_button = self._action(card, "Новый", "plus", self.new_profile)
        self.drop_button = self._action(card, "Удалить", "trash", self.delete_profile)
        self.drop_button.setObjectName("Danger")
        for button in (self.rules_button, self.edit_button, self.new_button,
                       self.drop_button):
            header.addWidget(button)
        body.addLayout(header)

        chooser = QHBoxLayout()
        chooser.setSpacing(9)
        self.profile_box = QComboBox(card)
        self.profile_box.setMinimumWidth(280)
        self.profile_box.currentIndexChanged.connect(self._on_profile_changed)
        chooser.addWidget(self.profile_box, 1)
        self.source_label = QLabel("", card)
        self.source_label.setObjectName("Hint")
        chooser.addWidget(self.source_label)
        body.addLayout(chooser)

        self.profile_hint = Hint("", card)
        body.addWidget(self.profile_hint)
        return card

    def _sources_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Исходные файлы", card))
        header.addStretch(1)
        header.addWidget(self._action(card, "Добавить файлы", "open", self.add_files))
        header.addWidget(self._action(card, "Взять папку целиком", "folder",
                                      self.add_folder))
        self.store_button = self._action(card, "Задать магазин", "card",
                                         self.set_store_hint)
        header.addWidget(self.store_button)
        header.addWidget(self._action(card, "Очистить", "clear", self.clear_sources))
        body.addLayout(header)

        self.files = QTableWidget(0, 3, card)
        self.files.setHorizontalHeaderLabels(["Файл", "Магазин", "Папка"])
        self.files.verticalHeader().setVisible(False)
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.setMaximumHeight(150)
        head = self.files.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        body.addWidget(self.files)

        self.sources_hint = Hint(
            "Магазин берётся из колонки файла. Если её нет — из имени файла; "
            "кнопкой «Задать магазин» его можно поправить.", card)
        body.addWidget(self.sources_hint)
        return card

    def _result_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Отчёт", card))
        header.addStretch(1)
        self.build_button = self._action(card, "Собрать (F5)", "run", self.run_build)
        self.build_button.setObjectName("Primary")
        self.save_button = self._action(card, "Сохранить файл", "export", self.save)
        self.save_button.setEnabled(False)
        header.addWidget(self.build_button)
        header.addWidget(self.save_button)
        body.addLayout(header)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_rows = MetricTile("Строк в отчёте", Palette.PRIMARY, card)
        self.tile_used = MetricTile("Продаж учтено", Palette.SUCCESS, card)
        self.tile_dropped = MetricTile("Отсеяно фильтром", Palette.TEXT_MUTED, card)
        self.tile_moved = MetricTile("Перенесено по правилам", Palette.INFO, card)
        for tile in (self.tile_rows, self.tile_used, self.tile_dropped,
                     self.tile_moved):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.warnings = Hint("", card)
        self.warnings.setStyleSheet(f"color: {Palette.WARNING};")
        self.warnings.setVisible(False)
        body.addWidget(self.warnings)

        self.preview = QTableWidget(0, 0, card)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setAlternatingRowColors(True)
        body.addWidget(self.preview, 1)

        self.result_hint = Hint(
            "Выберите профиль и исходные файлы, затем нажмите «Собрать».", card)
        body.addWidget(self.result_hint)
        return card

    def _action(self, parent: QWidget, title: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(handler)
        return button

    # --- загрузка настроек ------------------------------------------------------

    def restore(self) -> None:
        """Читает профили и правила при каждом открытии вкладки.

        Они общие: коллега мог завести правило, пока вкладка была закрыта, и
        показывать устаревший набор — прямой путь к расхождению в отчётах.
        """
        self.source_label.setText(
            "общая база" if service.online() else "своя база на этом компьютере")
        run_task(
            _load_settings,
            on_result=self._apply_settings,
            on_error=lambda message: self.notify(
                f"Не удалось прочитать настройки отчётности: {message}",
                ToastKind.ERROR),
        )

    def _apply_settings(self, loaded: tuple[list[ReportProfile], list[StoreRule]]
                        ) -> None:
        self._profiles, self._rules = loaded
        current = self.profile_box.currentData()
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        for profile in self._profiles:
            self.profile_box.addItem(profile.display_name, profile.id)
        if current is not None and (index := self.profile_box.findData(current)) >= 0:
            self.profile_box.setCurrentIndex(index)
        self.profile_box.blockSignals(False)
        self._on_profile_changed()
        self._update_actions()

    @property
    def profile(self) -> ReportProfile | None:
        wanted = self.profile_box.currentData()
        return next((p for p in self._profiles if p.id == wanted), None)

    def _on_profile_changed(self) -> None:
        profile = self.profile
        if profile is None:
            self.profile_hint.setText(
                "Профилей пока нет. Нажмите «Новый» — предложенные значения "
                "повторяют отчёт, который собирался вручную.")
            return
        rows = ", ".join(role.title for role in profile.rows) or "—"
        columns = ", ".join(role.title for role in profile.columns) or "без разреза"
        metrics = ", ".join(metric.title for metric in profile.metrics) or "—"
        filtered = ("только акционные продажи" if profile.filters.promo_only
                    else "все продажи")
        active = sum(1 for rule in self._rules if rule.enabled)
        rules = (f"правил объединения: {active}" if profile.apply_store_rules
                 else "правила объединения выключены")
        self.profile_hint.setText(
            f"Строки: {rows} · колонки: {columns} · показатели: {metrics}\n"
            f"Отбор: {filtered} · {rules} · имя файла: «{profile.file_name}»")

    # --- профили ----------------------------------------------------------------

    def new_profile(self) -> None:
        profile = default_profile()
        profile.name = ""
        dialog = ProfileDialog(profile, self)
        if dialog.exec() != ProfileDialog.DialogCode.Accepted:
            return
        self._save_profile(profile, "Профиль создан")

    def edit_profile(self) -> None:
        if (profile := self.profile) is None:
            self.notify("Сначала выберите профиль", ToastKind.WARNING)
            return
        dialog = ProfileDialog(profile, self)
        if dialog.exec() != ProfileDialog.DialogCode.Accepted:
            return
        self._save_profile(profile, "Профиль сохранён")

    def _save_profile(self, profile: ReportProfile, message: str) -> None:
        run_task(
            service.save_profile,
            profile,
            on_result=lambda saved: self._on_profile_saved(saved, message),
            on_error=lambda error: self.notify(
                f"Не удалось сохранить профиль: {error}", ToastKind.ERROR),
        )

    def _on_profile_saved(self, saved: ReportProfile, message: str) -> None:
        self.notify(message, ToastKind.SUCCESS)
        run_task(
            _load_settings,
            on_result=lambda loaded: self._select_after_reload(loaded, saved.id),
            on_error=lambda error: self.notify(str(error), ToastKind.ERROR),
        )

    def _select_after_reload(self, loaded, profile_id: int) -> None:
        self._apply_settings(loaded)
        if (index := self.profile_box.findData(profile_id)) >= 0:
            self.profile_box.setCurrentIndex(index)

    def delete_profile(self) -> None:
        if (profile := self.profile) is None:
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Удалить профиль")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(f"Удалить профиль «{profile.display_name}»?")
        confirm.setInformativeText(
            "Профиль общий: он пропадёт у всех менеджеров. "
            "Уже сохранённые файлы отчётов это не затронет.")
        yes = confirm.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.exec()
        if confirm.clickedButton() is not yes:
            return
        run_task(
            service.delete_profile,
            profile.id,
            on_result=lambda _: (self.notify("Профиль удалён", ToastKind.SUCCESS),
                                 self.restore()),
            on_error=lambda error: self.notify(
                f"Не удалось удалить профиль: {error}", ToastKind.ERROR),
        )

    def edit_rules(self) -> None:
        """Правила объединения магазинов.

        Список магазинов для подсказки берётся из уже прочитанных исходников:
        набирать названия руками — верный способ ошибиться в скобках, которых
        в них хватает («Артем (Первый Парфюмерный)»).
        """
        stores = sorted({row.store for item in (self._result.sources if self._result
                                                else [])
                         for row in item.rows if row.store})
        dialog = StoreRulesDialog(
            self._rules, stores, service.save_rule, service.delete_rule, self,
            may_delete=not service.online() or transport.session.is_admin)
        dialog.exec()
        self._rules = dialog.rules
        self._on_profile_changed()

    # --- исходники ---------------------------------------------------------------

    def add_files(self) -> None:
        mask = " ".join(f"*{extension}" for extension in SOURCE_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Выгрузки продаж", self._start_folder(),
            f"Книги Excel ({mask})")
        self._add(paths)

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Папка с выгрузками", self._start_folder())
        if not folder:
            return
        found = service.scan_folder(folder)
        if not found:
            self.notify("В папке нет книг Excel", ToastKind.WARNING)
            return
        self._add(found)

    def _start_folder(self) -> str:
        """Папка «Для отчётности» рядом с приложением — то место, куда
        выгрузки складываются сейчас."""
        if self._paths:
            return os.path.dirname(self._paths[-1])
        default = os.path.join(os.getcwd(), DEFAULT_FOLDER)
        return default if os.path.isdir(default) else os.getcwd()

    def _add(self, paths) -> None:
        added = 0
        for path in paths:
            if path and path not in self._paths:
                self._paths.append(path)
                added += 1
        if added:
            self._refresh_files()
            self.notify(f"Добавлено файлов: {added}", ToastKind.INFO)

    def clear_sources(self) -> None:
        self._paths.clear()
        self._result = None
        self._refresh_files()
        self.preview.clear()
        self.preview.setRowCount(0)
        self.preview.setColumnCount(0)
        for tile in (self.tile_rows, self.tile_used, self.tile_dropped,
                     self.tile_moved):
            tile.set_value(0)
        self.warnings.setVisible(False)
        self.save_button.setEnabled(False)
        self.result_hint.setText("Список исходников очищен.")

    def set_store_hint(self) -> None:
        """Название магазина для выбранного файла.

        Нужно точечным выгрузкам: колонки «Магазин» в них нет, а из имени файла
        название вытаскивается не всегда верно.
        """
        row = self.files.currentRow()
        if not 0 <= row < len(self._paths):
            self.notify("Сначала выберите файл в списке", ToastKind.WARNING)
            return
        path = self._paths[row]
        current = self._hints.get(path, "") or store_from_name(path)
        name, ok = QInputDialog.getText(
            self, "Магазин файла",
            f"Какому магазину принадлежат продажи из «{os.path.basename(path)}»?",
            text=current)
        if not ok:
            return
        if name.strip():
            self._hints[path] = name.strip()
        else:
            self._hints.pop(path, None)
        self._refresh_files()

    def _refresh_files(self) -> None:
        self.files.setRowCount(len(self._paths))
        for row, path in enumerate(self._paths):
            # Колонка «Магазин» внутри файла станет известна только при чтении,
            # поэтому до сборки здесь показывается лишь то, что видно снаружи:
            # заданное руками название или догадка по имени файла.
            hint = self._hints.get(path, "")
            guess = store_from_name(path)
            shown = hint or (f"{guess} (из имени файла)" if guess
                             else "из колонки файла")
            for column, text in enumerate(
                    (os.path.basename(path), shown, os.path.dirname(path))):
                item = QTableWidgetItem(text)
                if column == 1 and not hint:
                    item.setForeground(Qt.GlobalColor.gray)
                self.files.setItem(row, column, item)
        self._update_actions()

    def _update_actions(self) -> None:
        has_profile = self.profile is not None
        self.build_button.setEnabled(bool(self._paths) and has_profile and not self._busy)
        self.edit_button.setEnabled(has_profile)
        self.drop_button.setEnabled(has_profile)
        self.store_button.setEnabled(bool(self._paths))

    # --- сборка --------------------------------------------------------------------

    def run_build(self) -> None:
        if (profile := self.profile) is None:
            self.notify("Сначала выберите профиль отчёта", ToastKind.WARNING)
            return
        if not self._paths:
            self.notify("Добавьте хотя бы один файл с продажами", ToastKind.WARNING)
            return
        self._busy = True
        self._update_actions()
        self.result_hint.setText("Читаем выгрузки…")
        run_task(
            service.build_from_paths,
            list(self._paths),
            profile,
            store_hints=dict(self._hints),
            on_result=self._show_result,
            on_error=self._on_build_error,
            on_progress=lambda done, total: self.result_hint.setText(
                f"Читаем выгрузки… {done} из {total}"),
        )

    def _on_build_error(self, message: str) -> None:
        self._busy = False
        self._update_actions()
        self.result_hint.setText("Собрать отчёт не удалось.")
        self.notify(f"Не удалось собрать отчёт: {message}", ToastKind.ERROR)

    def _show_result(self, result: service.BuildResult) -> None:
        self._busy = False
        self._result = result
        table = result.table
        self.tile_rows.set_value(len(table.rows))
        self.tile_used.set_value(table.used_rows)
        self.tile_dropped.set_value(table.dropped_rows)
        self.tile_moved.set_value(table.moved_rows)

        grid = pivot.preview(table, PREVIEW_LIMIT)
        header, body = (grid[0], grid[1:]) if grid else ([], [])
        self.preview.setColumnCount(len(header))
        self.preview.setHorizontalHeaderLabels(header)
        self.preview.setRowCount(len(body))
        for row, line in enumerate(body):
            for column, text in enumerate(line):
                item = QTableWidgetItem(text)
                if column >= len(table.row_fields):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.preview.setItem(row, column, item)
        if header:
            self.preview.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Stretch)

        if result.warnings:
            self.warnings.setText("• " + "\n• ".join(result.warnings))
            self.warnings.setVisible(True)
        else:
            self.warnings.setVisible(False)

        self.save_button.setEnabled(result.ok)
        self._update_actions()
        shown = min(len(table.rows), PREVIEW_LIMIT)
        tail = (f" Показаны первые {shown} из {len(table.rows)}."
                if len(table.rows) > shown else "")
        self.result_hint.setText(
            f"Период: {table.period.title or 'не определён'} · "
            f"файл будет назван «{table.file_name()}.xlsx».{tail}")
        fade_in(self.preview)
        if result.ok:
            self.notify(f"Отчёт собран: {len(table.rows)} строк", ToastKind.SUCCESS)

    # --- выгрузка --------------------------------------------------------------------

    def save(self) -> None:
        if self._result is None or not self._result.ok:
            self.notify("Сначала соберите отчёт", ToastKind.WARNING)
            return
        table = self._result.table
        suggested = service.suggested_path(table, self._start_folder())
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить отчёт", suggested, "Книга Excel (*.xlsx)")
        if not path:
            return
        author = transport.session.full_name if transport.session.active else ""
        run_task(
            service.save,
            table,
            path,
            author=author,
            on_result=lambda saved: self._on_saved(saved),
            on_error=lambda error: self.notify(
                f"Не удалось сохранить отчёт: {error}", ToastKind.ERROR),
        )

    def _on_saved(self, path: str) -> None:
        self.notify(f"Отчёт сохранён: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.result_hint.setText(f"Готово: {path}")


def _load_settings() -> tuple[list[ReportProfile], list[StoreRule]]:
    """Профили и правила одним заходом — оба списка нужны странице сразу."""
    return service.profiles(), service.rules()
