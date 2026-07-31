"""Точка входа API оплат RetailCore."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .routers import (
    audit,
    auth,
    budgets,
    files,
    imports,
    payments,
    recipients,
    users,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Пул открывается при старте, а не при первом запросе: так неверный пароль
    # к базе виден сразу в журнале контейнера, а не через час работы.
    db.pool.open(wait=True, timeout=30)
    yield
    db.pool.close()


app = FastAPI(
    title="RetailCore — оплаты",
    description="Общая база оплат поставщикам для категорийных менеджеров.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(audit.router)
app.include_router(payments.router)
app.include_router(files.router)
app.include_router(budgets.router)
app.include_router(recipients.router)
app.include_router(imports.router)
app.include_router(users.router)


@app.get("/api/health", tags=["Служебное"], summary="Проверка живости")
def health() -> dict:
    row = db.fetch_one("SELECT COUNT(*) AS payments FROM payment")
    return {"status": "ok", "payments": row["payments"]}
