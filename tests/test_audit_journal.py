"""Журнал изменений: по записи видно, что это за оплата и что в ней поменялось."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import admin

# Серверный модуль не зависит от базы, пока не вызвана `record`, — поэтому его
# можно проверять здесь, не поднимая сервер.
_SPEC = importlib.util.spec_from_file_location(
    "auditlog", Path(__file__).resolve().parents[1] / "server/api/app/auditlog.py")
auditlog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(auditlog)


# --- сервер: что попадает в журнал ----------------------------------------------

def test_в_журнал_идёт_только_изменённое_и_прежнее_значение():
    old = {"amount": Decimal("100000.00"), "status": "planned", "comment": "",
           "pay_date": date(2026, 10, 14), "priority": ""}
    sent = {"amount": 90000.0, "status": "planned", "comment": None,
            "pay_date": date(2026, 10, 14), "priority": ""}

    changes = auditlog.diff(old, sent)

    assert changes == {"amount": 90000.0, "was": {"amount": 100000.0}}


def test_повторная_отправка_тех_же_значений_не_создаёт_записи():
    old = {"amount": Decimal("100.00"), "status": "paid", "vat": Decimal("0")}

    assert auditlog.diff(old, {"amount": 100.004, "status": "paid", "vat": 0.0}) == {}


def test_снятая_дата_видна_как_изменение():
    changes = auditlog.diff({"pay_date": date(2026, 10, 14)}, {"pay_date": None})

    assert changes == {"pay_date": None, "was": {"pay_date": "2026-10-14"}}


def test_оплата_описывается_словами():
    about = auditlog.snapshot({
        "recipient": "Шкурная Юлия Александровна ИП", "amount": Decimal("100000.00"),
        "pay_date": date(2026, 10, 14), "doc_number": "", "comment": "лишнее"})

    assert auditlog.describe(about) == \
        "Шкурная Юлия Александровна ИП · 100 000 ₽ · 14.10.2026"


def test_сумма_с_копейками_и_номер_документа():
    text = auditlog.describe({"recipient": "ООО Седлко", "amount": 1234.5,
                              "doc_number": "А-17"})

    assert text == "ООО Седлко · 1 234,5 ₽ · док. А-17"


# --- клиент: как это видно в таблице --------------------------------------------

def _entry(**fields) -> admin.Entry:
    return admin.Entry(id=1, user="Иванов", entity="оплата", **fields)


def test_запись_показывает_номер_и_что_за_оплата():
    entry = _entry(entity_id=22122, action="создание",
                   subject="Шкурная Юлия Александровна ИП · 100 000 ₽")

    assert entry.record == "№22122 · Шкурная Юлия Александровна ИП · 100 000 ₽"


def test_сервер_прежней_версии_даёт_просто_номер():
    assert _entry(entity_id=22122).record == "№22122"
    assert _entry().record == ""


def test_изменение_показывается_как_было_стало():
    entry = _entry(changes={
        "amount": 90000.0, "status": "moved", "pay_date": "2026-10-15",
        "was": {"amount": 100000.0, "status": "planned", "pay_date": "2026-10-14"}})

    assert entry.summary == (
        "сумма: 100 000 → 90 000 · статус: Запланировано → Перенесено"
        " · дата оплаты: 14.10.2026 → 15.10.2026")


def test_прежняя_сумма_из_старых_записей_тоже_читается():
    entry = _entry(changes={"amount": 90000.0, "amount_was": 100000.0})

    assert entry.summary == "сумма: 100 000 → 90 000"


def test_получатель_не_повторяется_если_он_уже_в_записи():
    entry = _entry(subject="Седлко ООО · 5 000 ₽",
                   changes={"recipient": "Седлко ООО", "responsible": "Валева Карина"})

    assert entry.summary == "ответственный: Валева Карина"


def test_удалённая_оплата_оставляет_след_в_изменениях():
    entry = _entry(entity_id=22121, action="удаление", subject="Шкурная ИП · 100 000 ₽",
                   changes={"status": "planned", "comment": "", "responsible": "Иванов"})

    assert entry.summary == "статус: Запланировано · комментарий: — · ответственный: Иванов"
