"""Фоновые операции: загрузка, сопоставление, сохранение. UI не блокируется."""
from __future__ import annotations

import inspect
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class TaskSignals(QObject):
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Запускает функцию в пуле потоков и отдаёт результат сигналом.

    Функция получает callback прогресса, если принимает аргумент `progress`.
    """

    def __init__(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.signals = TaskSignals()
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        kwargs = dict(self._kwargs)
        if _accepts_progress(self._function):
            kwargs["progress"] = self._report
        try:
            result = self._function(*self._args, **kwargs)
        except Exception as error:  # noqa: BLE001 — текст уходит в уведомление интерфейса
            self._emit(self.signals.failed, str(error))
            return
        self._emit(self.signals.finished, result)

    def _report(self, done: int, total: int) -> None:
        self._emit(self.signals.progress, done, total)

    @staticmethod
    def _emit(signal: Signal, *args: Any) -> None:
        """Молча пропускает сигнал, если получателя уже нет.

        При закрытии окна Qt удаляет виджеты, а задача может ещё выполняться:
        без этой защиты в поток летит RuntimeError и приложение пишет в лог
        трассировку на ровном месте.
        """
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


def _accepts_progress(function: Callable[..., Any]) -> bool:
    try:
        return "progress" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


# Пул не владеет Python-объектом задачи: без своей ссылки её соберёт сборщик мусора
# ещё до выполнения, и результат никогда не придёт.
_active: set[Task] = set()


def run_task(
    function: Callable[..., Any],
    *args: Any,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    **kwargs: Any,
) -> Task:
    task = Task(function, *args, **kwargs)
    if on_result:
        task.signals.finished.connect(on_result)
    if on_error:
        task.signals.failed.connect(on_error)
    if on_progress:
        task.signals.progress.connect(on_progress)

    _active.add(task)
    task.signals.finished.connect(lambda _=None, t=task: _active.discard(t))
    task.signals.failed.connect(lambda _=None, t=task: _active.discard(t))

    QThreadPool.globalInstance().start(task)
    return task
