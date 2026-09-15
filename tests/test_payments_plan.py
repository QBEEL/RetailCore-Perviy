"""План оплат из Excel: шаблон, разбор присланного файла и замена плана.

Файлы настоящие: шаблон создаётся, заполняется через openpyxl и читается
обратно. Подмена разбора сделала бы проверку бессмысленной — расходятся как раз
шаблон и его чтение, а не логика поверх них.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Payment, PaymentOrigin, PaymentStatus, plan, service, store

OCTOBER = date(2026, 10, 1)
SUPPLIERS = ["Суперкосметикс ООО", "НеваЛайн ООО", "Мода Хаус ООО"]
MANAGERS = ["Иванов Евгений", "Валева Карина"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Временная база и источник данных, указывающий на неё."""
    path = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: path)
    from app.core.payments import data

    monkeypatch.setattr(data, "online", lambda: False)
    # Карточек поставщиков в этих проверках нет: привязка к ним — отдельный
    # механизм, а план обязан грузиться и по одному текстовому имени.
    monkeypatch.setattr(service, "supplier_names", lambda *a, **k: {})
    return path


@pytest.fixture
def template(tmp_path):
    destination = str(tmp_path / "Шаблон.xlsx")
    plan.write_template(destination, suppliers=SUPPLIERS, managers=MANAGERS,
                        manager="Иванов Евгений", year=2026, month=10)
    return destination


def fill(template_path: str, destination: str, rows: list[tuple], *, manager: str = "") -> str:
    """Заполняет шаблон строками так, как это сделал бы менеджер."""
    workbook = openpyxl.load_workbook(template_path)
    sheet = workbook[plan.TEMPLATE_SHEET]
    if manager:
        sheet["B2"] = manager
    for offset, values in enumerate(rows):
        for column, value in enumerate(values, start=1):
            sheet.cell(row=7 + offset, column=column, value=value)
    workbook.save(destination)
    workbook.close()
    return destination


# --- шаблон -------------------------------------------------------------------

def test_шаблон_содержит_оба_листа_и_шапку(template):
    workbook = openpyxl.load_workbook(template)
    assert plan.TEMPLATE_SHEET in workbook.sheetnames
    assert plan.DIRECTORY_SHEET in workbook.sheetnames
    sheet = workbook[plan.TEMPLATE_SHEET]
    assert sheet["B2"].value == "Иванов Евгений"
    assert sheet["B3"].value == "Октябрь 2026"
    assert [c.value for c in sheet[6][:5]] == list(plan.HEADERS)
    workbook.close()


def test_справочник_отдаёт_списки_для_выпадающих(template):
    workbook = openpyxl.load_workbook(template)
    sheet = workbook[plan.DIRECTORY_SHEET]
    names = [row[0] for row in sheet.iter_rows(min_row=2, max_col=1, values_only=True) if row[0]]
    managers = [row[0] for row in sheet.iter_rows(min_row=2, min_col=2, max_col=2,
                                                  values_only=True) if row[0]]
    assert names == SUPPLIERS
    assert managers == MANAGERS
    workbook.close()


def test_список_поставщиков_не_запрещает_своего(template):
    """Иначе нового поставщика нельзя было бы запланировать вовсе."""
    workbook = openpyxl.load_workbook(template)
    sheet = workbook[plan.TEMPLATE_SHEET]
    rules = list(sheet.data_validations.dataValidation)
    assert rules and all(rule.showErrorMessage is False for rule in rules)
    workbook.close()


# --- разбор -------------------------------------------------------------------

def test_заполненный_шаблон_читается_обратно(template, tmp_path):
    filled = fill(template, str(tmp_path / "Иванов.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", "счёт 114"),
        (date(2026, 10, 12), "НеваЛайн ООО", 480000, None, ""),
    ])
    got = plan.read(filled)

    assert got.manager == "Иванов Евгений"
    assert got.months == [(2026, 10)]
    assert [row.amount for row in got.rows] == [1200000.0, 480000.0]
    # Указанная ставка остаётся как есть, пустая читается как действующая 22 %.
    assert [row.percent for row in got.rows] == [20, 22]
    assert got.rows[0].comment == "счёт 114"


def test_строки_без_даты_или_суммы_пропускаются_с_пояснением(template, tmp_path):
    filled = fill(template, str(tmp_path / "кривой.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", ""),
        (None, "НеваЛайн ООО", 480000, None, ""),
        (date(2026, 10, 9), "", 90000, None, ""),
    ])
    got = plan.read(filled)

    assert len(got.rows) == 1
    assert "строка 8" in got.skipped[0] and "дата" in got.skipped[0]
    assert "строка 9" in got.skipped[1] and "поставщик" in got.skipped[1]


def test_суммы_и_ставки_принимаются_в_любой_записи(template, tmp_path):
    filled = fill(template, str(tmp_path / "разное.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", "1 200 000,50", "Без НДС", ""),
        (date(2026, 10, 6), "НеваЛайн ООО", 480000, 0.2, ""),
        (date(2026, 10, 7), "Мода Хаус ООО", 90000, "10 %", ""),
    ])
    got = plan.read(filled)

    assert [row.amount for row in got.rows] == [1200000.5, 480000.0, 90000.0]
    assert [row.percent for row in got.rows] == [0, 20, 10]


def test_чужая_таблица_отвергается_понятной_ошибкой(tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Артикул", "Название", "Остаток"])
    sheet.append(["123", "Крем", 5])
    path = str(tmp_path / "остатки.xlsx")
    workbook.save(path)
    workbook.close()

    with pytest.raises(plan.PlanProblem, match="Дата"):
        plan.read(path)


def test_месяцы_плана_берутся_из_дат_а_не_из_шапки(template, tmp_path):
    """Строка, поставленная на соседний месяц, входит в замену наравне с прочими."""
    filled = fill(template, str(tmp_path / "два месяца.xlsx"), [
        (date(2026, 10, 28), "Суперкосметикс ООО", 100000, None, ""),
        (date(2026, 11, 3), "НеваЛайн ООО", 200000, None, ""),
    ])
    got = plan.read(filled)

    assert got.months == [(2026, 10), (2026, 11)]
    assert got.months_title == "октябрь 2026, ноябрь 2026"


# --- загрузка в базу ----------------------------------------------------------

def test_план_становится_оплатами_в_календаре(db, template, tmp_path):
    filled = fill(template, str(tmp_path / "Иванов.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", "счёт 114"),
        (date(2026, 10, 12), "НеваЛайн ООО", 480000, None, ""),
    ])
    report = service.analyze_plan(filled, db_path=db)
    assert len(report.created) == 2 and report.changes == 2

    service.apply_plan(report, db_path=db)
    rows = sorted(store.list_payments(path=db), key=lambda p: p.pay_date)

    assert [p.pay_date for p in rows] == [date(2026, 10, 5), date(2026, 10, 12)]
    assert all(p.status is PaymentStatus.PLANNED for p in rows)
    assert all(p.origin is PaymentOrigin.PLAN for p in rows)
    assert all(p.responsible == "Иванов Евгений" for p in rows)
    assert rows[0].vat == pytest.approx(200000.0)


def test_повторная_загрузка_того_же_файла_ничего_не_меняет(db, template, tmp_path):
    filled = fill(template, str(tmp_path / "Иванов.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", ""),
    ])
    service.apply_plan(service.analyze_plan(filled, db_path=db), db_path=db)

    again = service.analyze_plan(filled, db_path=db)

    assert again.changes == 0
    assert len(again.same) == 1
    assert len(store.list_payments(path=db)) == 1


def test_новый_файл_заменяет_прошлый_план(db, template, tmp_path):
    first = fill(template, str(tmp_path / "план 1.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", ""),
        (date(2026, 10, 12), "НеваЛайн ООО", 480000, None, ""),
        (date(2026, 10, 20), "Мода Хаус ООО", 90000, None, ""),
    ])
    service.apply_plan(service.analyze_plan(first, db_path=db), db_path=db)

    # Суперкосметикс подорожал, НеваЛайн остался, Мода Хаус из плана убрана,
    # добавилась новая строка.
    second = fill(template, str(tmp_path / "план 2.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1350000, "20%", ""),
        (date(2026, 10, 12), "НеваЛайн ООО", 480000, None, ""),
        (date(2026, 10, 26), "Суперкосметикс ООО", 300000, None, "допзаказ"),
    ])
    report = service.analyze_plan(second, db_path=db)

    assert len(report.updated) == 1 and report.updated[0].amount == 1350000.0
    assert len(report.same) == 1
    assert len(report.created) == 1
    assert [p.title for p in report.removed] == ["Мода Хаус ООО"]

    service.apply_plan(report, db_path=db)
    rows = sorted(store.list_payments(path=db), key=lambda p: p.pay_date)
    assert [(p.pay_date.day, p.amount) for p in rows] == [
        (5, 1350000.0), (12, 480000.0), (26, 300000.0)]


def test_оплаченное_не_трогается_и_не_задваивается(db, template, tmp_path):
    filled = fill(template, str(tmp_path / "план.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", ""),
    ])
    service.apply_plan(service.analyze_plan(filled, db_path=db), db_path=db)
    paid = store.list_payments(path=db)[0]
    paid.status = PaymentStatus.PAID
    store.save_payment(paid, db)

    report = service.analyze_plan(filled, db_path=db)

    assert report.changes == 0
    assert len(report.paid) == 1
    service.apply_plan(report, db_path=db)
    assert len(store.list_payments(path=db)) == 1


def test_замена_не_касается_чужого_плана_и_ручных_оплат(db, template, tmp_path):
    store.save_payment(Payment(
        amount=777.0, pay_date=date(2026, 10, 5), recipient="Суперкосметикс ООО",
        responsible="Иванов Евгений", origin=PaymentOrigin.MANUAL), db)
    other = fill(template, str(tmp_path / "Валева.xlsx"), [
        (date(2026, 10, 7), "Мода Хаус ООО", 90000, None, ""),
    ], manager="Валева Карина")
    service.apply_plan(service.analyze_plan(other, db_path=db), db_path=db)

    mine = fill(template, str(tmp_path / "Иванов.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, "20%", ""),
    ])
    report = service.analyze_plan(mine, db_path=db)

    # Ни ручная оплата, ни план коллеги в замену не попадают.
    assert not report.removed
    assert len(report.created) == 1
    service.apply_plan(report, db_path=db)
    assert len(store.list_payments(path=db)) == 3


def test_имя_из_окна_сильнее_шапки_файла(db, template, tmp_path):
    filled = fill(template, str(tmp_path / "чужой шаблон.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, None, ""),
    ])
    report = service.analyze_plan(filled, manager="Валева Карина", db_path=db)

    assert report.plan.manager == "Валева Карина"
    assert report.created[0].responsible == "Валева Карина"
    assert report.created[0].origin_ref == plan.plan_ref("Валева Карина", 2026, 10)


def test_план_без_менеджера_не_грузится(db, template, tmp_path):
    filled = fill(template, str(tmp_path / "без имени.xlsx"), [
        (date(2026, 10, 5), "Суперкосметикс ООО", 1200000, None, ""),
    ], manager=" ")

    with pytest.raises(plan.PlanProblem, match="менеджер"):
        service.analyze_plan(filled, db_path=db)


def test_метка_плана_не_зависит_от_написания_имени():
    """В базе один человек записан десятком написаний — метка должна сойтись."""
    assert plan.plan_ref("Иванов Евгений", 2026, 10) == plan.plan_ref(
        "  ИВАНОВ   ЕВГЕНИЙ ", 2026, 10)
