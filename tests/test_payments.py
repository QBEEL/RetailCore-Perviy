"""Тесты оплат: разбор выгрузки, дедупликация, статусы, хранилище."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import (
    Budget,
    Filter,
    ImportProblem,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    clean_name,
    importer,
    plan,
    level_of,
    parse_amount,
    parse_date,
    recipient_key,
    service,
    store,
)
from app.core.payments.models import DEFAULT_DAY_LEVELS, DayLevel
from app.core.payments.recipients import compare_key, guess_supplier, legal_form

TODAY = date(2026, 7, 30)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "payments.db")


# --- разбор значений ----------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1 649 018,00", 1649018.0),
    ("1 649 018,00", 1649018.0),
    ("274 374,2", 274374.2),
    ("500", 500.0),
    ("0,73", 0.73),
    ("", 0.0),
    ("не число", 0.0),
])
def test_сумма_разбирается_с_любым_пробелом(text, expected):
    assert parse_amount(text) == pytest.approx(expected)


def test_дата_заявки_в_виде_времени_это_день_выгрузки():
    """1С печатает только время для заявок, созданных в день выгрузки."""
    assert parse_date("11:19", TODAY) == TODAY
    assert parse_date("25.10.2022") == date(2022, 10, 25)
    assert parse_date("") is None


def test_несуществующая_дата_не_ломает_разбор():
    assert parse_date("31.02.2024") is None


# --- статусы ------------------------------------------------------------------

def test_факт_оплаты_сильнее_согласования():
    """В выгрузке есть заявки, оплаченные при статусе «Не согласована»."""
    status = importer.status_of(True, "Не согласована", date(2022, 1, 1), TODAY)
    assert status is PaymentStatus.PAID


def test_отклонённая_заявка_отменена_а_не_просрочена():
    status = importer.status_of(False, "Отклонена", date(2022, 1, 1), TODAY)
    assert status is PaymentStatus.CANCELLED


def test_неоплаченная_с_прошедшей_датой_просрочена():
    status = importer.status_of(False, "К оплате", date(2022, 1, 1), TODAY)
    assert status is PaymentStatus.OVERDUE


def test_неоплаченная_с_будущей_датой_запланирована():
    status = importer.status_of(False, "К оплате", date(2026, 9, 1), TODAY)
    assert status is PaymentStatus.PLANNED


def test_просрочка_не_трогает_оплаченное_и_отменённое(db):
    """Иначе отклонённые заявки прошлых лет стали бы вечным долгом."""
    for status in (PaymentStatus.PAID, PaymentStatus.CANCELLED, PaymentStatus.MOVED):
        store.save_payment(Payment(
            amount=100.0, recipient="Тест", pay_date=date(2022, 1, 1), status=status), db)
    assert store.refresh_overdue(TODAY, db) == 0
    kept = {p.status for p in store.list_payments(None, db)}
    assert PaymentStatus.OVERDUE not in kept


def test_просрочка_переводит_только_запланированное(db):
    store.save_payment(Payment(
        amount=100.0, recipient="Тест", pay_date=date(2022, 1, 1),
        status=PaymentStatus.PLANNED), db)
    store.save_payment(Payment(
        amount=200.0, recipient="Тест", pay_date=date(2026, 12, 1),
        status=PaymentStatus.PLANNED), db)
    assert store.refresh_overdue(TODAY, db) == 1
    statuses = sorted(p.status.value for p in store.list_payments(None, db))
    assert statuses == ["overdue", "planned"]


def test_платёж_без_даты_не_становится_просроченным(db):
    """Незавершённая заявка без даты платежа — не просрочка: срока у неё нет."""
    store.save_payment(Payment(amount=100.0, recipient="Тест", status=PaymentStatus.PLANNED), db)
    assert store.refresh_overdue(TODAY, db) == 0


# --- получатели ---------------------------------------------------------------

def test_форма_юрлица_не_различает_поставщиков():
    assert recipient_key("НеваЛайн ООО") == recipient_key("ООО НеваЛайн")
    assert recipient_key("Сафило СНГ ООО") == recipient_key("Сафило СНГ")


def test_мусор_в_имени_вычищается():
    assert clean_name("Домосканова Регина") == "Домосканова Регина"
    assert clean_name("Харитонова  Елена Александровна ИП") == "Харитонова Елена Александровна ИП"
    # Буква, потерянная при перекодировке, не должна попадать в ключ.
    assert "?" not in recipient_key("Л?вкина София")


def test_имя_из_одной_формы_не_теряется():
    """Если кроме формы ничего нет, она и есть имя — иначе ключ был бы пустым."""
    assert recipient_key("ООО") == "ооо"


def test_форма_определяется():
    assert legal_form("НеваЛайн ООО") == "ООО"
    assert legal_form("НОСОВА ВИОЛЕТТА ИГОРЕВНА ИП") == "ИП"
    assert legal_form("ЗВЕЗДА") == ""


def test_общие_слова_убираются_только_для_сравнения():
    assert compare_key("Игнат Торговый дом ООО") == "игнат"
    assert recipient_key("Игнат Торговый дом ООО") == "игнат торговый дом"


def test_имя_подмножество_не_привязывается():
    """«ЗВЕЗДА» — не «Звезда Востока»: лишнее слово почти всегда другое юрлицо."""
    assert guess_supplier("ЗВЕЗДА", {1: "Звезда Востока", 2: "Полярная Звезда"}) is None
    assert guess_supplier("Сафило", {1: "Сафило СНГ"}) is None


def test_опечатка_в_длинном_имени_привязывается():
    """У длинного имени одна буква не мешает; у короткого — сознательно мешает."""
    guess = guess_supplier("Суперкосметик ООО", {7: "Суперкосметикс"})
    assert guess is not None and guess.supplier_id == 7
    assert guess_supplier("Кларис ООО", {7: "Кларис-2"}) is None


def test_совпадение_без_формы_привязывается():
    guess = guess_supplier("НеваЛайн ООО", {7: "НеваЛайн"})
    assert guess is not None and guess.supplier_id == 7 and guess.confident


# --- цветовая шкала -----------------------------------------------------------

def test_уровень_дня_по_порогам():
    assert level_of(0) is DayLevel.EMPTY
    assert level_of(90_000) is DayLevel.LIGHT
    assert level_of(1_000_000) is DayLevel.MEDIUM
    assert level_of(2_500_000) is DayLevel.HIGH
    assert level_of(9_000_000) is DayLevel.CRITICAL


def test_пороги_настраиваются():
    small = (100_000.0, 300_000.0, 700_000.0)
    assert level_of(500_000, small) is DayLevel.HIGH
    assert level_of(500_000, DEFAULT_DAY_LEVELS) is DayLevel.LIGHT


# --- хранилище ----------------------------------------------------------------

def test_оплата_без_суммы_не_сохраняется(db):
    with pytest.raises(ValueError):
        store.save_payment(Payment(amount=0.0, recipient="Тест"), db)


def test_оплата_без_получателя_не_сохраняется(db):
    with pytest.raises(ValueError):
        store.save_payment(Payment(amount=100.0), db)


def test_фильтр_по_периоду_и_статусу(db):
    store.save_payment(Payment(
        amount=100.0, recipient="А", pay_date=date(2026, 1, 15),
        status=PaymentStatus.PAID), db)
    store.save_payment(Payment(
        amount=200.0, recipient="Б", pay_date=date(2026, 6, 15),
        status=PaymentStatus.PLANNED), db)
    found = store.list_payments(Filter(start=date(2026, 6, 1)), db)
    assert [p.recipient for p in found] == ["Б"]
    found = store.list_payments(Filter(statuses=(PaymentStatus.PAID,)), db)
    assert [p.recipient for p in found] == ["А"]


def test_фильтр_по_источнику_отделяет_план_от_выгрузки(db):
    """В таблице план и выгрузка 1С лежат вперемешку и на глаз не различаются."""
    store.save_payment(Payment(
        amount=100.0, recipient="А", pay_date=date(2026, 9, 10),
        origin=PaymentOrigin.PLAN), db)
    store.save_payment(Payment(
        amount=200.0, recipient="Б", pay_date=date(2026, 9, 11),
        origin=PaymentOrigin.IMPORT), db)
    found = store.list_payments(Filter(origins=(PaymentOrigin.PLAN,)), db)
    assert [p.recipient for p in found] == ["А"]
    both = Filter(origins=(PaymentOrigin.PLAN, PaymentOrigin.IMPORT))
    assert len(store.list_payments(both, db)) == 2
    assert len(store.list_payments(Filter(), db)) == 2


def test_пустой_отбор_по_источнику_ничего_не_ограничивает():
    assert Filter().active is False
    assert Filter(origins=(PaymentOrigin.PLAN,)).active is True


def test_поиск_по_тексту(db):
    store.save_payment(Payment(
        amount=100.0, recipient="НеваЛайн ООО", doc_number="IP00-000001",
        request_date=date(2026, 1, 1)), db)
    store.save_payment(Payment(amount=200.0, recipient="Сафило СНГ ООО"), db)
    assert len(store.list_payments(Filter(text="невалайн"), db)) == 0
    assert len(store.list_payments(Filter(text="НеваЛайн"), db)) == 1
    assert len(store.list_payments(Filter(text="IP00-000001"), db)) == 1


def test_массовое_изменение_статуса(db):
    ids = [
        store.save_payment(Payment(amount=100.0, recipient=f"П{n}"), db).id
        for n in range(3)
    ]
    assert store.update_many(ids, db, status=PaymentStatus.PAID) == 3
    found = store.list_payments(None, db)
    assert all(p.status is PaymentStatus.PAID and p.paid_flag for p in found)


def test_бюджет_месяца_перезаписывается(db):
    store.save_budget(Budget(year=2026, month=8, amount=15_000_000.0), db)
    store.save_budget(Budget(year=2026, month=8, amount=20_000_000.0, note="уточнён"), db)
    found = store.get_budget(2026, 8, db)
    assert found is not None
    assert found.amount == 20_000_000.0 and found.note == "уточнён"
    assert len(store.budgets(db)) == 1


def test_неверный_месяц_бюджета_отклоняется(db):
    with pytest.raises(ValueError):
        store.save_budget(Budget(year=2026, month=13, amount=1.0), db)


def test_привязка_получателя_переносится_на_платежи(db):
    store.save_payment(Payment(amount=100.0, recipient="НеваЛайн ООО"), db)
    store.save_payment(Payment(amount=200.0, recipient="ООО НеваЛайн"), db)
    # Обе записи нормализуются в один ключ, значит привязка накроет обе.
    assert store.save_recipient_link("НеваЛайн ООО", 42, db) == 2
    assert all(p.supplier_id == 42 for p in store.list_payments(None, db))
    assert store.unlinked_recipients(db) == []


def test_снятая_привязка_освобождает_платежи(db):
    store.save_payment(Payment(amount=100.0, recipient="НеваЛайн ООО"), db)
    store.save_recipient_link("НеваЛайн ООО", 42, db)
    assert store.drop_recipient_link("НеваЛайн ООО", db)
    assert store.list_payments(None, db)[0].supplier_id == 0


# --- разбор файла -------------------------------------------------------------

HEADER = (
    "Номер;Дата заявки;Есть файлы;Сумма;НДС;Валюта;Статус;Сверх лимита;Приоритет;"
    "Дата платежа;Оплачена / Закрыта;Хозяйственная операция;Получатель;"
    "Состояние ЭДО;Заявитель;Автор"
)


def _csv(tmp_path, *rows: str, name: str = "оплаты.csv") -> str:
    path = tmp_path / name
    path.write_bytes(("\n".join([HEADER, *rows])).encode("cp1251"))
    return str(path)


def _row(number: str, request: str, amount: str, recipient: str, pay: str = "",
         paid: str = "Нет", status: str = "К оплате") -> str:
    return (f"{number};{request};1;{amount};;руб.;{status};Нет;;"
            f"{pay};{paid};Оплата поставщику;{recipient};;Иванов;Иванов")


def test_выгрузка_читается_из_cp1251(tmp_path, db):
    path = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО",
                              "11.01.2022", "Да"))
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert report.new == 1 and not report.skipped
    assert report.payments[0].recipient == "НеваЛайн ООО"
    assert report.payments[0].amount == pytest.approx(95_700.0)


def test_номер_обнуляется_каждый_год_и_не_создаёт_дубликат(tmp_path, db):
    """Один и тот же номер живёт в каждом году — ключ только с датой заявки."""
    path = _csv(
        tmp_path,
        _row("IP00-000001", "09.01.2022", "95 700,00", "Игнат Торговый дом ООО", "11.01.2022", "Да"),
        _row("IP00-000001", "03.01.2023", "292 350,00", "НеваЛайн ООО", "09.01.2023", "Да"),
        _row("IP00-000001", "01.01.2026", "3 300,00", "ЗВЕЗДА", "27.01.2026", "Да"),
    )
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert report.new == 3
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 3


def test_повторный_импорт_не_создаёт_дубликатов(tmp_path, db):
    path = _csv(
        tmp_path,
        _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО", "11.01.2022", "Да"),
        _row("IP00-000002", "10.01.2022", "1 000,00", "Сафило СНГ ООО", "12.01.2022", "Да"),
    )
    first = service.analyze_import(path, today=TODAY, db_path=db)
    service.apply_import(first, today=TODAY, db_path=db, link=False)
    second = service.analyze_import(path, today=TODAY, db_path=db)
    assert (second.new, second.updated, second.same) == (0, 0, 2)
    service.apply_import(second, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 2


def test_изменившаяся_сумма_обновляет_запись(tmp_path, db):
    first = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО",
                               "11.01.2022", "Да"), name="первая.csv")
    service.apply_import(
        service.analyze_import(first, today=TODAY, db_path=db), today=TODAY, db_path=db, link=False)
    second = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "99 999,00", "НеваЛайн ООО",
                                "11.01.2022", "Да"), name="вторая.csv")
    report = service.analyze_import(second, today=TODAY, db_path=db)
    assert (report.new, report.updated) == (0, 1)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 1
    assert store.list_payments(None, db)[0].amount == pytest.approx(99_999.0)


def test_импорт_не_затирает_комментарий(tmp_path, db):
    """Комментарий принадлежит пользователю: 1С о нём не знает."""
    path = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО",
                              "11.01.2022", "Да"))
    service.apply_import(
        service.analyze_import(path, today=TODAY, db_path=db), today=TODAY, db_path=db, link=False)
    saved = store.list_payments(None, db)[0]
    saved.comment = "оплатить после сверки"
    store.save_payment(saved, db)

    changed = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "111 000,00", "НеваЛайн ООО",
                                 "11.01.2022", "Да"), name="изменённая.csv")
    service.apply_import(
        service.analyze_import(changed, today=TODAY, db_path=db),
        today=TODAY, db_path=db, link=False)
    kept = store.list_payments(None, db)[0]
    assert kept.comment == "оплатить после сверки"
    assert kept.amount == pytest.approx(111_000.0)


def test_импорт_не_трогает_созданное_вручную(tmp_path, db):
    """У ручной записи нет номера из выгрузки — совпадение было бы случайным."""
    manual = store.save_payment(Payment(
        amount=500.0, recipient="НеваЛайн ООО", doc_number="IP00-000001",
        request_date=date(2022, 1, 9), origin=PaymentOrigin.MANUAL), db)
    path = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО",
                              "11.01.2022", "Да"))
    service.apply_import(
        service.analyze_import(path, today=TODAY, db_path=db), today=TODAY, db_path=db, link=False)
    kept = store.get_payment(manual.id, db)
    assert kept is not None and kept.amount == pytest.approx(500.0)


def _planned(db, amount: float, recipient: str = "АСТЭРА ГК ООО",
             pay: date = date(2026, 10, 13), **fields) -> Payment:
    return store.save_payment(Payment(
        amount=amount, recipient=recipient, pay_date=pay,
        origin=fields.pop("origin", PaymentOrigin.MANUAL), **fields), db)


def test_заявка_1с_занимает_ручную_оплату_того_же_дня(tmp_path, db):
    """Оплату наметили в приложении, потом завели заявку в 1С — запись одна.

    Так задвоились «АСТЭРА» на 13.10 и «Смарт Бьюти» на 10.11: ручная запись
    осталась, а заявка из выгрузки легла рядом.
    """
    manual = _planned(db, 800_000.0, "Смарт Бьюти", pay=date(2026, 11, 10),
                      comment="по счёту от 25.09")
    path = _csv(tmp_path, _row("IP00-001625", "29.09.2026", "855 876,00",
                              "Смарт Бьюти ООО", "10.11.2026", status="Не согласована"))
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert (report.new, report.adopted) == (0, 1)

    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 1
    taken = store.get_payment(manual.id, db)
    assert taken.doc_number == "IP00-001625"
    assert taken.request_date == date(2026, 9, 29)
    assert taken.origin is PaymentOrigin.IMPORT
    assert taken.amount == pytest.approx(855_876.0)
    assert taken.comment == "по счёту от 25.09"

    again = service.analyze_import(path, today=TODAY, db_path=db)
    assert (again.new, again.adopted, again.updated) == (0, 0, 0)


def test_две_заявки_на_одну_ручную_оплату(tmp_path, db):
    """Одну запись занимает ближайшая по сумме заявка, вторая ложится рядом."""
    manual = _planned(db, 1_500_000.0, "Гранат ООО", pay=date(2026, 10, 9))
    path = _csv(
        tmp_path,
        _row("IP00-001615", "28.09.2026", "1 154 350,00", "Гранат ООО", "09.10.2026"),
        _row("IP00-001617", "28.09.2026", "513 000,00", "Гранат ООО", "09.10.2026"),
    )
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert (report.new, report.adopted) == (1, 1)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 2
    assert store.get_payment(manual.id, db).doc_number == "IP00-001615"


def test_заявка_не_занимает_оплату_другого_дня_или_оплаченную(tmp_path, db):
    _planned(db, 220_849.0, pay=date(2026, 10, 14))
    _planned(db, 220_849.0, status=PaymentStatus.PAID)
    path = _csv(tmp_path, _row("IP00-001623", "28.09.2026", "220 849,00",
                              "АСТЭРА ГК ООО", "13.10.2026"))
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert (report.new, report.adopted) == (1, 0)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.count_payments(db) == 3


def test_занятая_строка_плана_не_возвращается_при_повторной_загрузке(tmp_path, db):
    """Метка плана остаётся на записи: тот же файл плана не заведёт её заново."""
    ref = plan.plan_ref("Когай Анна", 2026, 10)
    row = _planned(db, 401_480.0, "АМАРИСТА ООО", pay=date(2026, 10, 14),
                   origin=PaymentOrigin.PLAN, origin_ref=ref, responsible="Когай Анна")
    path = _csv(tmp_path, _row("IP00-001618", "28.09.2026", "401 480,00",
                              "АМАРИСТА ООО", "14.10.2026"))
    service.apply_import(service.analyze_import(path, today=TODAY, db_path=db),
                         today=TODAY, db_path=db, link=False)
    taken = store.get_payment(row.id, db)
    assert taken.origin_ref == ref and taken.doc_number == "IP00-001618"

    file = plan.PlanFile(manager="Когай Анна", rows=[plan.PlanRow(
        pay_date=date(2026, 10, 14), supplier="АМАРИСТА ООО", amount=401_480.0)])
    current = plan.existing_of(store.list_payments(None, db), "Когай Анна", [(2026, 10)])
    report = plan.compare(file, current)
    assert not report.created and not report.removed and not report.updated
    assert [p.id for p in report.paid] == [row.id]


def test_строка_без_суммы_пропускается_с_объяснением(tmp_path, db):
    path = _csv(
        tmp_path,
        _row("IP00-000001", "09.01.2022", "", "НеваЛайн ООО", "11.01.2022", "Да"),
        _row("IP00-000002", "10.01.2022", "1 000,00", "Сафило СНГ ООО", "12.01.2022", "Да"),
    )
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert report.new == 1
    assert len(report.skipped) == 1 and "сумма" in report.skipped[0]


def test_чужой_файл_не_принимается(tmp_path, db):
    path = tmp_path / "прайс.csv"
    path.write_bytes("Артикул;Наименование;Цена\nA-1;Духи;100\n".encode("cp1251"))
    with pytest.raises(ImportProblem) as error:
        service.analyze_import(str(path), today=TODAY, db_path=db)
    assert "не найдены обязательные колонки" in str(error.value)


def test_пустой_файл_не_принимается(tmp_path, db):
    path = tmp_path / "пусто.csv"
    path.write_bytes(b"")
    with pytest.raises(ImportProblem):
        service.analyze_import(str(path), today=TODAY, db_path=db)


def test_колонки_ищутся_по_названию_а_не_по_номеру(tmp_path, db):
    """Порядок колонок в 1С настраивается и меняется между выгрузками."""
    header = "Получатель;Сумма;Номер;Дата заявки;Дата платежа;Оплачена / Закрыта"
    path = tmp_path / "другой порядок.csv"
    path.write_bytes(
        f"{header}\nНеваЛайн ООО;95 700,00;IP00-000001;09.01.2022;11.01.2022;Да\n".encode("cp1251"))
    report = service.analyze_import(str(path), today=TODAY, db_path=db)
    assert report.new == 1
    assert report.payments[0].amount == pytest.approx(95_700.0)


def test_журнал_импорта_помнит_файл(tmp_path, db):
    path = _csv(tmp_path, _row("IP00-000001", "09.01.2022", "95 700,00", "НеваЛайн ООО",
                              "11.01.2022", "Да"))
    assert service.already_imported(path, db) is None
    service.apply_import(
        service.analyze_import(path, today=TODAY, db_path=db), today=TODAY, db_path=db, link=False)
    assert service.already_imported(path, db) is not None


# --- работа с настоящей выгрузкой ---------------------------------------------

DATA = Path(__file__).resolve().parents[1] / "Для планирования"
HISTORY = DATA / "Оплата поставщикам.csv"

needs_files = pytest.mark.skipif(not HISTORY.exists(), reason="нет выгрузки оплат")


@needs_files
def test_настоящая_выгрузка_разбирается_целиком(db):
    report = service.analyze_import(str(HISTORY), today=TODAY, db_path=db)
    # В выгрузке 6929 строк, две из них без суммы.
    assert report.rows == 6929
    assert report.new == 6927
    assert len(report.skipped) == 2
    assert report.total == pytest.approx(2_439_920_620.91, abs=0.5)


@needs_files
def test_настоящая_выгрузка_переносит_повторный_импорт(db):
    first = service.analyze_import(str(HISTORY), today=TODAY, db_path=db)
    service.apply_import(first, today=TODAY, db_path=db, link=False)
    second = service.analyze_import(str(HISTORY), today=TODAY, db_path=db)
    assert (second.new, second.updated) == (0, 0)
    assert second.same == 6927


# --- подписи осей графиков ----------------------------------------------------

def test_подписи_оси_остаются_уникальными():
    """`QBarCategoryAxis` склеивает одинаковые подписи и теряет вместе с ними столбцы.

    Прежняя версия ставила пустую строку каждой непоказываемой подписи: сорок
    пять дней превращались в девять категорий, и две трети данных исчезали с
    графика молча.
    """
    from app.ui.widgets.charts import _thin

    labels = [f"{day:02d}.07" for day in range(1, 46)]
    thinned = _thin(labels, 5)
    assert len(thinned) == 45
    assert len(set(thinned)) == 45


def test_подписи_оси_показываются_через_шаг():
    from app.ui.widgets.charts import _INVISIBLE, _thin

    thinned = _thin([f"{day:02d}" for day in range(1, 13)], 3)
    visible = [text.replace(_INVISIBLE, "") for text in thinned]
    assert [text for text in visible if text] == ["01", "04", "07", "10"]


def test_повторяющиеся_подписи_не_склеиваются():
    from app.ui.widgets.charts import _thin

    assert len(set(_thin(["янв", "янв", "фев", "фев"], 1))) == 4


def _status_after(tmp_path, db, *files: str) -> str:
    for name, row in enumerate(files):
        path = _csv(tmp_path, row, name=f"выгрузка{name}.csv")
        service.apply_import(
            service.analyze_import(path, today=TODAY, db_path=db),
            today=TODAY, db_path=db, link=False)
    return store.list_payments(None, db)[0].status.value


def test_просроченная_заявка_становится_оплаченной_после_новой_выгрузки(tmp_path, db):
    """Заявка легла просроченной, потом её оплатили: в 1С стоит «Оплачена»."""
    unpaid = _row("IP00-000001", "01.01.2022", "95 700,00", "НеваЛайн ООО", "02.01.2022", "Нет")
    paid = _row("IP00-000001", "01.01.2022", "95 700,00", "НеваЛайн ООО", "02.01.2022", "Да")
    assert _status_after(tmp_path, db, unpaid) == "overdue"
    assert _status_after(tmp_path, db, paid) == "paid"


def test_повторная_выгрузка_лечит_оплаченную_заявку_с_просрочкой(tmp_path, db):
    """База уже испорчена: отметка об оплате есть, а статус остался «Просрочено»."""
    unpaid = _row("IP00-000001", "01.01.2022", "95 700,00", "НеваЛайн ООО", "02.01.2022", "Нет")
    paid = _row("IP00-000001", "01.01.2022", "95 700,00", "НеваЛайн ООО", "02.01.2022", "Да")
    _status_after(tmp_path, db, unpaid)
    with store.connect(db) as connection:
        connection.execute("UPDATE payment SET paid_flag = 1")
        connection.commit()
    assert store.list_payments(None, db)[0].status.value == "overdue"
    path = _csv(tmp_path, paid, name="повтор.csv")
    report = service.analyze_import(path, today=TODAY, db_path=db)
    assert report.updated == 1
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.list_payments(None, db)[0].status.value == "paid"


def test_перенесённая_заявка_не_сбрасывается_импортом(tmp_path, db):
    """Перенос назначил человек — неоплаченная выгрузка его не откатывает."""
    unpaid = _row("IP00-000001", "01.01.2022", "95 700,00", "НеваЛайн ООО", "02.01.2022", "Нет")
    _status_after(tmp_path, db, unpaid)
    with store.connect(db) as connection:
        connection.execute("UPDATE payment SET status = 'moved'")
        connection.commit()
    changed = _csv(tmp_path, _row("IP00-000001", "01.01.2022", "99 000,00", "НеваЛайн ООО",
                                 "02.01.2022", "Нет"), name="сумма.csv")
    service.apply_import(service.analyze_import(changed, today=TODAY, db_path=db),
                         today=TODAY, db_path=db, link=False)
    assert store.list_payments(None, db)[0].status.value == "moved"


def _plan(amount: float, ident: int) -> Payment:
    return Payment(id=ident, amount=amount, recipient="АСТЭРА ГК ООО", pay_date=date(2026, 8, 3),
                   status=PaymentStatus.PLANNED, origin=PaymentOrigin.MANUAL)


def _request(amount: float, number: str) -> Payment:
    return Payment(doc_number=number, request_date=date(2026, 8, 1), amount=amount,
                   recipient="АСТЭРА ГК ООО", pay_date=date(2026, 8, 3),
                   origin=PaymentOrigin.IMPORT)


def _taken(plans, requests) -> dict[int, str]:
    found = importer.adoptions(requests, plans)
    return {ident: payment.doc_number for ident, payment in found.items()}


def test_заявки_делят_записи_дня_по_наименьшему_расхождению_а_не_по_порядку_строк():
    plans = [_plan(800_000, 1), _plan(500_000, 2), _plan(100_000, 3)]
    # Первой в файле идёт 520 000: жадный перебор отдал бы ей 500 000 и оставил
    # 450 000 без близкой записи.
    requests = [_request(520_000, "A"), _request(450_000, "B"), _request(790_000, "C")]
    assert _taken(plans, requests) == {1: "C", 2: "A", 3: "B"}


def test_порядок_строк_не_меняет_сопоставление():
    plans = [_plan(800_000, 1), _plan(500_000, 2), _plan(100_000, 3)]
    requests = [_request(520_000, "A"), _request(450_000, "B"), _request(790_000, "C")]
    assert _taken(plans, list(reversed(requests))) == _taken(plans, requests)


def test_лишняя_заявка_остаётся_без_пары_и_берётся_наиболее_далёкая():
    plans = [_plan(800_000, 1), _plan(100_000, 2)]
    requests = [_request(790_000, "A"), _request(105_000, "B"), _request(2_000_000, "C")]
    assert _taken(plans, requests) == {1: "A", 2: "B"}


def test_лишняя_запись_остаётся_свободной():
    plans = [_plan(800_000, 1), _plan(500_000, 2), _plan(100_000, 3)]
    assert _taken(plans, [_request(95_000, "A")]) == {3: "A"}


def test_равные_суммы_разводятся_детерминированно():
    plans = [_plan(100.0, 7), _plan(100.0, 5)]
    requests = [_request(100.0, "A"), _request(100.0, "B")]
    assert _taken(plans, requests) == {5: "A", 7: "B"}


def _analyzed(tmp_path, db, *rows: str):
    return service.analyze_import(_csv(tmp_path, *rows), today=TODAY, db_path=db)


def test_сумму_новой_заявки_можно_поправить_до_записи(tmp_path, db):
    report = _analyzed(tmp_path, db, _row("IP00-000001", "09.01.2026", "95 700,00",
                                           "НеваЛайн ООО", "11.08.2026"))
    key = report.details[0].key
    assert report.set_amount(key, 90_000.0)
    assert report.details[0].amount_after == pytest.approx(90_000.0)
    assert report.details[0].edited and "поправлена вручную" in report.details[0].fields_text
    assert report.total == pytest.approx(90_000.0)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    saved = store.list_payments(None, db)[0]
    assert saved.amount == pytest.approx(90_000.0)


def test_правка_суммы_пересчитывает_ндс_по_ставке(tmp_path, db):
    line = _row("IP00-000001", "09.01.2026", "120 000,00", "НеваЛайн ООО", "11.08.2026")
    cells = line.split(";")
    cells[4] = "20 000,00"          # НДС 20 000 с 120 000
    report = _analyzed(tmp_path, db, ";".join(cells))
    report.set_amount(report.details[0].key, 60_000.0)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.list_payments(None, db)[0].vat == pytest.approx(10_000.0)


def test_возврат_к_предложенной_сумме_снимает_правку(tmp_path, db):
    report = _analyzed(tmp_path, db, _row("IP00-000001", "09.01.2026", "95 700,00",
                                           "НеваЛайн ООО", "11.08.2026"))
    key = report.details[0].key
    report.set_amount(key, 1.0)
    report.set_amount(key, 95_700.0)
    assert not report.overrides and not report.details[0].edited
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.list_payments(None, db)[0].amount == pytest.approx(95_700.0)


def test_нулевая_и_чужая_сумма_не_принимаются(tmp_path, db):
    report = _analyzed(tmp_path, db, _row("IP00-000001", "09.01.2026", "95 700,00",
                                           "НеваЛайн ООО", "11.08.2026"))
    assert not report.set_amount(report.details[0].key, 0.0)
    assert not report.set_amount(("нет", "такой"), 10.0)


def test_правка_суммы_существующей_заявки_и_счётчик_направления(tmp_path, db):
    first = _row("IP00-000001", "09.01.2026", "100 000,00", "НеваЛайн ООО", "11.08.2026")
    service.apply_import(_analyzed(tmp_path, db, first), today=TODAY, db_path=db, link=False)
    second = _row("IP00-000001", "09.01.2026", "120 000,00", "НеваЛайн ООО", "11.08.2026")
    report = _analyzed(tmp_path, db, second)
    assert report.raised == 1
    report.set_amount(report.details[0].key, 90_000.0)
    assert (report.raised, report.lowered) == (0, 1)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert store.list_payments(None, db)[0].amount == pytest.approx(90_000.0)


def test_правка_суммы_не_меняет_подбор_пар_с_планом(tmp_path, db):
    store.save_payment(_plan(800_000, 0), db)
    store.save_payment(_plan(100_000, 0), db)
    report = _analyzed(
        tmp_path, db,
        _row("IP00-000001", "01.08.2026", "790 000,00", "АСТЭРА ГК ООО", "03.08.2026"),
        _row("IP00-000002", "01.08.2026", "105 000,00", "АСТЭРА ГК ООО", "03.08.2026"))
    big = next(r for r in report.details if r.doc_number == "IP00-000001")
    # Правка сделала бы большую заявку «ближе» к 100 000 — пары остаются прежними.
    report.set_amount(big.key, 110_000.0)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    amounts = sorted(p.amount for p in store.list_payments(None, db))
    assert amounts == [pytest.approx(105_000.0), pytest.approx(110_000.0)]
    assert len(amounts) == 2
