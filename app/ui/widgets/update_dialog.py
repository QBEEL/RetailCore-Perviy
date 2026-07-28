"""Диалог автообновления: проверка, предложение обновиться, загрузка, перезапуск."""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from ...core import updater
from ...core.settings import AppSettings
from .. import icons
from ..theme import Metrics, Palette
from ..update_check import GITHUB_OWNER, GITHUB_REPO, UpdateChecker
from .common import Hint, SectionTitle

_RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_SNOOZE = timedelta(hours=24)


class UpdateDialog(QWidget):
    """Немодальное окно поверх главного: не должно мешать работе, пока не нужно."""

    def __init__(self, settings: AppSettings, checker: UpdateChecker, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Dialog)
        self.settings = settings
        self.checker = checker
        self._manifest: updater.ReleaseManifest | None = None
        self._downloaded_path: str | None = None
        self._respect_snooze = True
        self.setWindowTitle("Обновления")
        self.setFixedWidth(420)
        self._build()

        checker.found.connect(self._on_found)
        checker.up_to_date.connect(self._on_up_to_date)
        checker.error.connect(self._on_error)
        checker.progress.connect(self._on_progress)
        checker.ready.connect(self._on_ready)
        checker.failed.connect(self._on_error)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        head = QHBoxLayout()
        self._icon = QLabel(self)
        self._icon.setFixedSize(28, 28)
        head.addWidget(self._icon)
        self._title = SectionTitle("", self)
        head.addWidget(self._title, 1)
        root.addLayout(head)

        self._message = Hint("", self)
        root.addWidget(self._message)

        self._changelog = QLabel("", self)
        self._changelog.setWordWrap(True)
        self._changelog.setObjectName("Hint")
        root.addWidget(self._changelog)

        self._progress = QProgressBar(self)
        self._progress.setTextVisible(True)
        root.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        buttons.addStretch(1)
        self._tertiary = QPushButton(self)
        self._tertiary.setAutoDefault(False)
        buttons.addWidget(self._tertiary)
        self._secondary = QPushButton(self)
        self._secondary.setAutoDefault(False)
        buttons.addWidget(self._secondary)
        self._primary = QPushButton(self)
        self._primary.setObjectName("Primary")
        self._primary.setAutoDefault(False)
        buttons.addWidget(self._primary)
        root.addLayout(buttons)

    # --- запуск -----------------------------------------------------------

    def run_silent(self) -> None:
        """Тихая проверка при старте: окно появляется, только если есть новость."""
        self._respect_snooze = True
        self.checker.check()

    def run_visible(self) -> None:
        """Проверка по кнопке «Проверить сейчас»: путь виден целиком."""
        self._respect_snooze = False
        self._show_checking("Проверка обновлений…")
        self.show()
        self.checker.check()

    # --- реакции на сигналы UpdateChecker ----------------------------------

    def _on_found(self, manifest: updater.ReleaseManifest) -> None:
        self._manifest = manifest
        if self._respect_snooze:
            if manifest.version == self.settings.update_skip_version:
                return
            if self._is_snoozed():
                return
        if self.settings.update_download_auto and updater.is_frozen():
            self._show_checking(f"Загружается версия {manifest.version}…")
            self.show()
            self.checker.download(manifest)
        else:
            self._show_available(manifest)
            self.show()

    def _on_up_to_date(self) -> None:
        if not self._respect_snooze:
            self._show_up_to_date()
            self.show()

    def _on_error(self, message: str) -> None:
        if self._manifest is not None:
            # Фоновая автозагрузка не удалась — оставляем обычное предложение
            # обновиться вручную вместо голой ошибки без выхода.
            self._show_available(self._manifest, extra_hint=message)
            self.show()
        elif not self._respect_snooze:
            self._show_error(message)
            self.show()

    def _on_progress(self, percent: int) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(percent)

    def _on_ready(self, path: str) -> None:
        self._downloaded_path = path
        self._show_restart_ready()
        self.show()

    # --- состояния окна -----------------------------------------------------

    def _reset_buttons(self) -> None:
        for button in (self._primary, self._secondary, self._tertiary):
            button.setVisible(False)
            if getattr(button, "_wired", False):
                button.clicked.disconnect()
                button._wired = False

    def _wire(self, button: QPushButton, slot) -> None:
        button.clicked.connect(slot)
        button._wired = True

    def _show_checking(self, text: str) -> None:
        self._reset_buttons()
        self._icon.setPixmap(icons.icon("info", Palette.PRIMARY).pixmap(28, 28))
        self._title.setText(text)
        self._message.setText("")
        self._changelog.setText("")
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)

    def _show_up_to_date(self) -> None:
        self._reset_buttons()
        self._icon.setPixmap(icons.icon("check", Palette.SUCCESS).pixmap(28, 28))
        self._title.setText("У вас последняя версия")
        self._message.setText("")
        self._changelog.setText("")
        self._progress.setVisible(False)
        self._primary.setText("Закрыть")
        self._primary.setVisible(True)
        self._wire(self._primary, self.close)

    def _show_error(self, message: str) -> None:
        self._reset_buttons()
        self._icon.setPixmap(icons.icon("warning", Palette.WARNING).pixmap(28, 28))
        self._title.setText("Не удалось проверить обновления")
        self._message.setText(message)
        self._changelog.setText("")
        self._progress.setVisible(False)
        self._primary.setText("Закрыть")
        self._primary.setVisible(True)
        self._wire(self._primary, self.close)

    def _show_available(self, manifest: updater.ReleaseManifest, extra_hint: str = "") -> None:
        self._reset_buttons()
        self._icon.setPixmap(icons.icon("update", Palette.PRIMARY).pixmap(28, 28))
        self._title.setText(f"Доступна новая версия {manifest.version}")
        self._message.setText(extra_hint or "Обновить сейчас?")
        if self.settings.update_show_changelog and manifest.changelog:
            self._changelog.setText("Что нового:\n" + "\n".join(f"• {line}" for line in manifest.changelog))
        else:
            self._changelog.setText("")
        self._progress.setVisible(False)

        self._primary.setText("Обновить")
        self._primary.setVisible(True)
        self._wire(self._primary, lambda: self._start_update(manifest))

        if not manifest.mandatory:
            self._secondary.setText("Напомнить позже")
            self._secondary.setVisible(True)
            self._wire(self._secondary, lambda: self._remind_later(manifest))

            self._tertiary.setText("Пропустить эту версию")
            self._tertiary.setVisible(True)
            self._wire(self._tertiary, lambda: self._skip(manifest))

    def _show_restart_ready(self) -> None:
        self._reset_buttons()
        self._icon.setPixmap(icons.icon("check", Palette.SUCCESS).pixmap(28, 28))
        self._title.setText(f"Версия {self._manifest.version} готова к установке")
        self._message.setText("Приложение перезапустится с новой версией.")
        self._changelog.setText("")
        self._progress.setVisible(False)
        self._primary.setText("Перезапустить сейчас")
        self._primary.setVisible(True)
        self._wire(self._primary, self._restart)

    # --- действия -------------------------------------------------------------

    def _start_update(self, manifest: updater.ReleaseManifest) -> None:
        if not updater.is_frozen():
            # Из исходников подменять нечего — открываем страницу релиза.
            QDesktopServices.openUrl(QUrl(_RELEASES_PAGE))
            self.close()
            return
        self._show_checking(f"Загружается версия {manifest.version}…")
        self.checker.download(manifest)

    def _restart(self) -> None:
        updater.log_event("Update successful")
        updater.apply_update(self._downloaded_path)
        QApplication.instance().quit()

    def _remind_later(self, manifest: updater.ReleaseManifest) -> None:
        self.settings.update_remind_after = (datetime.now() + _SNOOZE).isoformat()
        self.settings.save()
        self.close()

    def _skip(self, manifest: updater.ReleaseManifest) -> None:
        self.settings.update_skip_version = manifest.version
        self.settings.save()
        self.close()

    def _is_snoozed(self) -> bool:
        if not self.settings.update_remind_after:
            return False
        try:
            until = datetime.fromisoformat(self.settings.update_remind_after)
        except ValueError:
            return False
        return datetime.now() < until
