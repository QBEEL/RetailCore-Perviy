"""Создание снимка загруженного каталога в фоне.

Общая точка для страниц «Сопоставление» и «Каталог»: снимок — побочный
эффект импорта, поэтому его ошибка не должна мешать работе с файлом.
"""
from __future__ import annotations

from typing import Callable

from ..core import snapshots
from ..core.models import Sheet
from ..core.settings import AppSettings
from .tasks import run_task
from .widgets.toast import ToastKind

Notify = Callable[[str, ToastKind], None]
Status = Callable[[str], None]


def capture(
    sheet: Sheet,
    settings: AppSettings,
    notify: Notify,
    status: Status | None = None,
) -> None:
    """Сохраняет состояние листа в историю данных, не блокируя интерфейс.

    `status` получает короткий текст о ходе работы: на больших каталогах
    запись занимает заметное время, и пользователь должен видеть, что идёт.
    """
    if not settings.snapshots_enabled:
        return
    run_task(
        snapshots.create,
        sheet,
        on_result=lambda snapshot: _done(snapshot, notify, status),
        on_error=lambda message: _failed(message, notify, status),
        on_progress=(lambda done, total: _progress(done, total, status)) if status else None,
    )


def _progress(done: int, total: int, status: Status | None) -> None:
    if status and total:
        status(f"сохранение снимка… {done * 100 // total}%")


def _done(snapshot: snapshots.Snapshot | None, notify: Notify, status: Status | None) -> None:
    # None означает, что файл с таким содержимым уже снят — сообщать не о чем,
    # иначе уведомление всплывало бы при каждом переключении вкладок.
    if status:
        status("" if snapshot is None else f"снимок сохранён · {snapshot.total_products} товаров")
    if snapshot is not None:
        notify(f"Снимок сохранён: {snapshot.total_products} товаров", ToastKind.INFO)


def _failed(message: str, notify: Notify, status: Status | None) -> None:
    if status:
        status("снимок не сохранён")
    notify(f"Снимок не сохранён: {message}", ToastKind.WARNING)
