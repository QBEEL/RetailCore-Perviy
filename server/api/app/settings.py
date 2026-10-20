"""Настройки API. Всё берётся из окружения — в образ не зашито ничего."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    secret: str
    token_hours: int
    # Сколько дней можно продлевать вход, не вводя пароль. Отсчёт идёт от
    # входа по паролю, а не от последнего продления.
    session_days: int
    files_dir: str
    # ИИ-правка заметок. Ключ необязателен: без него остальное работает, а
    # запрос к ИИ получает понятный отказ.
    gemini_key: str = ""
    # Название модели у Google меняется, поэтому оно в окружении, а не в коде.
    ai_model: str = "gemini-3.5-flash-lite"
    # Обращений к ИИ на человека за сутки. Квота ключа общая (500 запросов в сутки
    # у этой модели), поэтому предел на человека — двадцать: два десятка
    # пользователей укладываются, а один не выберет квоту за всех.
    ai_daily_limit: int = 20
    # Исходящий прокси до Google, если сервер стоит там, где API закрыт.
    ai_proxy: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("API_SECRET", "")
        if len(secret) < 32:
            # Пустой или короткий ключ означал бы, что токены может подделать
            # любой желающий. Лучше не запуститься, чем работать так.
            raise RuntimeError(
                "API_SECRET не задан или короче 32 символов —"
                " сгенерируйте: openssl rand -base64 48")
        return cls(
            dsn=os.environ["DATABASE_URL"],
            secret=secret,
            # Смена длиннее рабочего дня: заново вводить пароль после обеда
            # никто не должен, но и вечный токен оставлять незачем.
            token_hours=int(os.environ.get("API_TOKEN_HOURS", "12")),
            session_days=int(os.environ.get("API_SESSION_DAYS", "7")),
            files_dir=os.environ.get("API_FILES_DIR", "/srv/files"),
            gemini_key=os.environ.get("GEMINI_API_KEY", "").strip(),
            ai_model=os.environ.get("AI_MODEL", "").strip() or "gemini-3.5-flash-lite",
            ai_daily_limit=int(os.environ.get("AI_DAILY_LIMIT", "20") or 20),
            ai_proxy=os.environ.get("AI_HTTPS_PROXY", "").strip(),
        )


settings = Settings.from_env()
