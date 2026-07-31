"""Настройки API. Всё берётся из окружения — в образ не зашито ничего."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    secret: str
    token_hours: int
    files_dir: str

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
            files_dir=os.environ.get("API_FILES_DIR", "/srv/files"),
        )


settings = Settings.from_env()
