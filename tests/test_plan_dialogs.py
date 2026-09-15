"""Окна плана оплат: что видит человек до того, как нажмёт «Применить».

Фон не запускается: разбор проверен в `test_payments_plan`, здесь важно, что
готовый отчёт превращается в понятный список и что кнопка включается только
тогда, когда есть что применять.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.payments import Payment, PaymentStatus, plan
from app.ui.widgets.plan_dialogs import PlanImportDialog, PlanTemplateDialog

MANAGERS = ["Иванов Евгений", "Валева Карина"]


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def payment(day: int, amount: float, name: str, status=PaymentStatus.PLANNED) -> Payment:
    return Payment(amount=amount, pay_date=date(2026, 10, day),
                   recipient=name, status=status)


@pytest.fixture
def report() -> plan.PlanReport:
    file = plan.PlanFile(path="C:/планы/Иванов.xlsx", manager="Иванов Евгений")
    file.rows = [plan.PlanRow(number=7, pay_date=date(2026, 10, 5),
                              supplier="Суперкосметикс ООО", amount=1200000.0)]
    file.skipped = ["строка 9: не заполнено — сумма"]
    return plan.PlanReport(
        plan=file,
        created=[payment(5, 1200000.0, "Суперкосметикс ООО")],
        updated=[payment(12, 480000.0, "НеваЛайн ООО")],
        removed=[payment(20, 90000.0, "Мода Хаус ООО")],
        paid=[payment(2, 250000.0, "БИГДИЛС ООО", PaymentStatus.PAID)],
        unlinked=1,
    )


def test_отчёт_показывает_все_группы_построчно(application, report):
    dialog = PlanImportDialog(MANAGERS)
    dialog._ready(report)

    lines = [dialog.details.item(i).text() for i in range(dialog.details.count())]
    assert any("Появится (1)" in line for line in lines)
    assert any("Изменится (1)" in line for line in lines)
    assert any("Исчезнет из плана (1)" in line for line in lines)
    assert any("Уже оплачено" in line for line in lines)
    assert any("Мода Хаус" in line for line in lines)
    assert any("пропущено: строка 9" in line for line in lines)
    assert any("Без карточки поставщика: 1" in line for line in lines)


def test_шапка_называет_менеджера_и_месяцы(application, report):
    dialog = PlanImportDialog(MANAGERS)
    dialog._ready(report)

    assert "Иванов Евгений" in dialog.summary.text()
    assert "октябрь 2026" in dialog.summary.text()
    assert dialog.apply_button.isEnabled()


def test_без_изменений_применять_нечего(application, report):
    quiet = plan.PlanReport(plan=report.plan, same=[payment(5, 1200000.0, "Тот же")])
    dialog = PlanImportDialog(MANAGERS)
    dialog._ready(quiet)

    assert not dialog.apply_button.isEnabled()
    assert dialog.apply_button.text() == "Изменений нет"


def test_ошибка_разбора_показывается_вместо_отчёта(application):
    dialog = PlanImportDialog(MANAGERS)
    dialog._failed("Не нашёл таблицу плана: нужны колонки «Дата», «Поставщик» и «Сумма».")

    assert "Не нашёл таблицу плана" in dialog.summary.text()
    assert not dialog.apply_button.isEnabled()


def test_имя_шаблона_подсказывает_месяц_и_менеджера(application):
    dialog = PlanTemplateDialog(MANAGERS, manager="Валева Карина", year=2026, month=10)

    assert dialog._suggested_name() == "План оплат Октябрь 2026 Валева Карина.xlsx"


def test_шаблон_без_менеджера_называется_по_месяцу(application):
    dialog = PlanTemplateDialog(MANAGERS, year=2026, month=10)

    assert dialog._suggested_name() == "План оплат Октябрь 2026.xlsx"
