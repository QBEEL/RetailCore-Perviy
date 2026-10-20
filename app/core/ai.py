"""ИИ-правка текста: просьба и текст уходят на сервер оплат, оттуда — в Gemini.

Ключа к ИИ в приложении нет: он хранится на сервере. Поэтому без входа в оплаты
правка недоступна, хотя сами заметки работают и без сети. Модуль не знает про Qt —
вызывается из фоновой задачи, как остальные запросы к серверу.
"""
from __future__ import annotations

from .payments import transport
from .payments.transport import AuthError, ServerError

# Те же пределы, что у сервера (routers/ai.py): длиннее он всё равно откажет, а
# человеку лучше сказать об этом сразу, не отправляя текст.
MAX_TEXT = 20000
MAX_INSTRUCTION = 500


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
