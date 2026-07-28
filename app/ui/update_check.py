"""Проверка и загрузка обновлений с GitHub Releases (сетевой слой на Qt)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .. import __version__
from ..core import updater

GITHUB_OWNER = "QBEEL"
GITHUB_REPO = "RetailCore-Perviy"

_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_EXE_ASSET = "RetailCore.exe"
_MANIFEST_ASSET = "version.json"
_USER_AGENT = b"RetailCore-Updater"


class UpdateChecker(QObject):
    """Проверка последнего релиза и загрузка exe с прогрессом."""

    found = Signal(object)      # ReleaseManifest — версия новее текущей
    up_to_date = Signal()
    error = Signal(str)

    progress = Signal(int)      # проценты загрузки
    ready = Signal(str)         # путь к скачанному и проверенному exe
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        self._download_file = None

    # --- проверка версии --------------------------------------------------

    def check(self) -> None:
        request = QNetworkRequest(QUrl(_API_URL))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        reply = self._net.get(request)
        reply.finished.connect(lambda: self._on_release(reply))

    def _on_release(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            message = f"Не удалось проверить обновления: {reply.errorString()}"
            updater.log_event(message)
            self.error.emit(message)
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8"))
            assets = {asset["name"]: asset["browser_download_url"] for asset in data["assets"]}
            exe_url = assets[_EXE_ASSET]
            manifest_url = assets[_MANIFEST_ASSET]
            version = str(data["tag_name"]).lstrip("vV")
        except (KeyError, ValueError, TypeError) as exc:
            message = f"Неожиданный ответ GitHub при проверке обновлений: {exc}"
            updater.log_event(message)
            self.error.emit(message)
            return
        if not (updater.is_https(exe_url) and updater.is_https(manifest_url)):
            message = "Ссылка на обновление не по HTTPS — загрузка отклонена"
            updater.log_event(message)
            self.error.emit(message)
            return
        self._fetch_manifest(version, exe_url, manifest_url)

    def _fetch_manifest(self, version: str, exe_url: str, manifest_url: str) -> None:
        request = QNetworkRequest(QUrl(manifest_url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        reply = self._net.get(request)
        reply.finished.connect(lambda: self._on_manifest(reply, version, exe_url))

    def _on_manifest(self, reply: QNetworkReply, version: str, exe_url: str) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            message = f"Не удалось загрузить описание релиза: {reply.errorString()}"
            updater.log_event(message)
            self.error.emit(message)
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8"))
            manifest = updater.ReleaseManifest(
                version=version,
                exe_url=exe_url,
                mandatory=bool(data.get("mandatory", False)),
                sha256=str(data.get("sha256", "")),
                changelog=[str(item) for item in data.get("changelog", [])],
            )
        except (ValueError, TypeError) as exc:
            message = f"Неожиданный формат version.json: {exc}"
            updater.log_event(message)
            self.error.emit(message)
            return

        updater.log_event(f"Checked version\nCurrent: {__version__}\nLatest: {manifest.version}")
        if updater.is_newer(manifest.version, __version__):
            self.found.emit(manifest)
        else:
            self.up_to_date.emit()

    # --- загрузка -----------------------------------------------------------

    def download(self, manifest: updater.ReleaseManifest) -> None:
        if not updater.is_https(manifest.exe_url):
            self.failed.emit("Ссылка на файл обновления не по HTTPS")
            return
        directory = Path(tempfile.gettempdir()) / "RetailCore" / "update"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / _EXE_ASSET
        self._download_file = open(destination, "wb")
        self._download_path = str(destination)
        self._download_manifest = manifest

        request = QNetworkRequest(QUrl(manifest.exe_url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        reply = self._net.get(request)
        reply.downloadProgress.connect(self._on_progress)
        reply.readyRead.connect(lambda: self._download_file.write(reply.readAll()))
        reply.finished.connect(lambda: self._on_download_finished(reply))
        updater.log_event("Download started")

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.emit(int(done * 100 / total))

    def _on_download_finished(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        self._download_file.close()
        self._download_file = None
        path = self._download_path

        if reply.error() != QNetworkReply.NetworkError.NoError:
            message = f"Загрузка обновления прервалась: {reply.errorString()}"
            updater.log_event(message)
            Path(path).unlink(missing_ok=True)
            self.failed.emit(message)
            return

        if not updater.verify_sha256(path, self._download_manifest.sha256):
            message = "Загруженный файл повреждён (не совпадает контрольная сумма)"
            updater.log_event(message)
            Path(path).unlink(missing_ok=True)
            self.failed.emit(message)
            return

        updater.log_event("Download completed")
        self.ready.emit(path)
