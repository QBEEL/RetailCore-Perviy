"""ИИ-правка текста: просьба и текст уходят на сервер оплат, оттуда — в Gemini.

Ключа к ИИ в приложении нет: он хранится на сервере. Поэтому без входа в оплаты
правка недоступна, хотя сами заметки работают и без сети. Модуль не знает про Qt —
вызывается из фоновой задачи, как остальные запросы к серверу.
"""
from __future__ import annotations

from dataclasses import dataclass

from .payments import transport
from .payments.transport import AuthError, ServerError

# Те же пределы, что у сервера (routers/ai.py): длиннее он всё равно откажет, а
# человеку лучше сказать об этом сразу, не отправляя текст. Сервер присылает свои
# значения вместе с остатком (см. Usage), эти — на случай, пока ответа нет.
MAX_TEXT = 20000
MAX_INSTRUCTION = 500


@dataclass(frozen=True, slots=True)
class Usage:
    """Сколько обращений к ИИ использовано сегодня и сколько можно всего."""

    used: int = 0
    # Предел человека: личный, заданный администратором, или общий. 0 — закрыт.
    limit: int = 0
    # Через сколько секунд счётчик обнулится.
    reset_seconds: int = 0
    max_text: int = MAX_TEXT
    max_instruction: int = MAX_INSTRUCTION
    # Ключ на сервере задан. Без него правка невозможна ни у кого.
    configured: bool = True

    @property
    def closed(self) -> bool:
        """Администратор закрыл человеку ИИ-правку."""
        return self.limit <= 0

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def available(self) -> bool:
        return self.configured and not self.closed and self.remaining > 0

    @property
    def ratio(self) -> float:
        """Доля использованного, от 0 до 1."""
        return min(self.used / self.limit, 1.0) if self.limit > 0 else 1.0

    @classmethod
    def from_server(cls, data: dict) -> "Usage":
        return cls(
            used=int(data.get("used", 0)), limit=int(data.get("limit", 0)),
            reset_seconds=int(data.get("reset_seconds", 0)),
            max_text=int(data.get("max_text", MAX_TEXT)),
            max_instruction=int(data.get("max_instruction", MAX_INSTRUCTION)),
            configured=bool(data.get("configured", True)))


def usage() -> Usage:
    """Остаток обращений на сегодня. Для показа — не для разрешения: решает сервер."""
    return Usage.from_server(transport.get("/api/ai/usage") or {})


def reset_in(seconds: int) -> str:
    """«через 5 ч 12 мин» — когда обнулится счётчик."""
    minutes = max(seconds, 0) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"через {hours} ч {minutes:02d} мин" if minutes else f"через {hours} ч"
    return f"через {max(minutes, 1)} мин"


def edit_text(instruction: str, text: str) -> str:
    """Возвращает текст, исправленный по просьбе. ServerError — с готовым для показа текстом."""
    instruction, text = instruction.strip(), text.strip()
    if not instruction:
        raise ServerError("Напишите, что сделать с текстом.")
    if not text:
        raise ServerError("Нечего править: текст пуст.")
    if len(instruction) > MAX_INSTRUCTION:
        raise ServerError(f"Просьба длиннее {MAX_INSTRUCTION} знаков — сократите её.")
    if len(text) > MAX_TEXT:
        raise ServerError(
            f"Текст длиннее {MAX_TEXT} знаков. Выделите часть, которую нужно поправить.")
    try:
        answer = transport.post("/api/ai/edit", {"instruction": instruction, "text": text})
    except AuthError:
        raise AuthError("Для ИИ-правки войдите в оплаты: текст обрабатывает сервер.") from None
    result = str((answer or {}).get("text", "")).strip()
    if not result:
        raise ServerError("ИИ не вернул текст. Переформулируйте просьбу.")
    return result
