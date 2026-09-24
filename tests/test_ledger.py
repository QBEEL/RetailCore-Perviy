"""Разбор ведомости по товарам на складах и выводы по ней.

Файл собирается здесь же, а не берётся с чьего-то рабочего стола: проверять
разбор выгрузки по файлу, которого нет в репозитории, нельзя — тест начнёт
падать на другой машине, и причина будет не в коде.

Проверяется то, на чём такой разбор ломается молча: шапка с одноимёнными
складами, строки групп с итогами вложенных товаров, служебные склады «в пути» и
колонка «Итого» рядом с данными. Каждая из этих ловушек даёт не ошибку, а
неверные числа в отчёте, который выглядит целым.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import ledger
from app.core.ledger import parse

FIELDS = ("Начальный остаток", "Приход", "Расход", "Конечный остаток")


def _book(path: Path, stores: list[str], rows: list[tuple],
          *, unit_label: str = "Ед. изм.", total: list[float] | None = None) -> str:
    """Собирает выгрузку в том виде, в каком её отдаёт 1С.

    `rows` — кортежи (артикул, номенклатура, ед.изм, [числа по складам]).
    Пустая единица измерения означает строку группы.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "Лист_1"
    sheet["A2"] = "Ведомость по товарам на складах"
    sheet["A4"] = "Параметры:"
    sheet["A5"] = "Отбор:"

    sheet.cell(7, 1, "Артикул")
    sheet.cell(7, 2, "Номенклатура")
    sheet.cell(7, 3, unit_label)
    names = list(stores) + (["Итого"] if total is not None else [])
    for index, name in enumerate(names):
        start = 4 + index * 4
        sheet.cell(7, start, name)
        sheet.merge_cells(start_row=7, start_column=start,
                          end_row=7, end_column=start + 3)
        sheet.cell(8, start, "Количество")
        for offset, field in enumerate(FIELDS):
            sheet.cell(9, start + offset, field)

    for number, (article, name, unit, values) in enumerate(rows):
        row = 10 + number
        sheet.cell(row, 1, article)
        sheet.cell(row, 2, name)
        sheet.cell(row, 3, unit)
        for index, value in enumerate(values):
            if value is not None:
                sheet.cell(row, 4 + index, value)
        if total is not None:
            # Итог по строке: сумма складов, если явно не задан другой.
            start = 4 + len(stores) * 4
            for offset in range(4):
                pieces = [values[index * 4 + offset]
                          for index in range(len(stores))
                          if index * 4 + offset < len(values)
                          and values[index * 4 + offset] is not None]
                if unit and pieces:
                    sheet.cell(row, start + offset, sum(pieces))

    target = str(path / "ведомость.xlsx")
    book.save(target)
    return target


def _line(*values) -> list:
    return list(values)


# --- шапка ---------------------------------------------------------------------

def test_склады_читаются_из_шапки(tmp_path):
    path = _book(tmp_path, ["Реми", "ВС - В пути Сэм", "Сити Молл"],
                 [("1", "Мыло", "шт", _line(10, 0, 3, 7, 0, 0, 0, 0, 5, 0, 1, 4))])

    led = ledger.read(path)

    assert [store.title for store in led.stores] == [
        "Реми", "ВС - В пути Сэм", "Сити Молл"]
    assert [store.title for store in led.shops] == ["Реми", "Сити Молл"]
    assert [store.title for store in led.transit] == ["ВС - В пути Сэм"]


def test_одноимённые_склады_разведены_номером(tmp_path):
    """Два склада с одним названием — данные одного не должны затирать другой."""
    path = _book(tmp_path, ["Пирожок", "Пирожок"],
                 [("1", "Мыло", "шт", _line(10, 0, 4, 6, 20, 0, 5, 15))])

    led = ledger.read(path)

    assert [store.title for store in led.stores] == ["Пирожок", "Пирожок (2)"]
    assert [store.doubled for store in led.stores] == [False, True]
    item = led.items[0]
    assert item.move("Пирожок").outgoing == 4
    assert item.move("Пирожок (2)").outgoing == 5
    assert led.total().outgoing == 9, "оба склада должны попасть в сумму"


def test_итоговая_колонка_не_склад(tmp_path):
    path = _book(tmp_path, ["Реми"],
                 [("1", "Мыло", "шт", _line(10, 0, 3, 7))], total=[10, 0, 3, 7])

    led = ledger.read(path)

    assert [store.title for store in led.stores] == ["Реми"]
    assert led.reported is not None and led.reported.outgoing == 3


@pytest.mark.parametrize("label", ["Ед. изм.", "Ед.изм.", "Ед.изм", "ЕД. ИЗМ."])
def test_единица_измерения_узнаётся_в_разных_написаниях(tmp_path, label):
    path = _book(tmp_path, ["Реми"], [("1", "Мыло", "шт", _line(10, 0, 3, 7))],
                 unit_label=label)

    assert len(ledger.read(path).items) == 1


def test_чужой_файл_отклоняется_с_объяснением(tmp_path):
    book = Workbook()
    book.active["A1"] = "просто таблица"
    target = str(tmp_path / "чужое.xlsx")
    book.save(target)

    with pytest.raises(parse.ParseError) as error:
        ledger.read(target)

    assert "ведомость" in str(error.value).lower()


# --- строки групп --------------------------------------------------------------

def test_строки_групп_не_складываются_с_товарами(tmp_path):
    """У групп в этой выгрузке стоят итоги вложенных товаров.

    Сложить их вместе с товарами — посчитать всё дважды. Отличает группу
    отсутствие единицы измерения.
    """
    path = _book(tmp_path, ["Реми"], [
        ("", "Красота и уход", "", _line(30, 0, 9, 21)),
        ("1", "Мыло", "шт", _line(10, 0, 4, 6)),
        ("2", "Крем", "шт", _line(20, 0, 5, 15)),
    ])

    led = ledger.read(path)

    assert [item.name for item in led.items] == ["Мыло", "Крем"]
    assert led.total().outgoing == 9


# --- сверка с файлом -----------------------------------------------------------

def test_разбор_сходится_с_итогом_файла(tmp_path):
    path = _book(tmp_path, ["Реми", "Сити Молл"],
                 [("1", "Мыло", "шт", _line(10, 0, 4, 6, 20, 0, 5, 15))],
                 total=[30, 0, 9, 21])

    assert ledger.read(path).balanced


def test_несошедшийся_итог_не_замалчивается(tmp_path):
    """Итог в файле считан по колонке, которую разбор потерял.

    Это главная проверка разбора: если шапку прочитали неверно, числа встанут
    не под тем складом, а отчёт будет выглядеть целым.
    """
    path = _book(tmp_path, ["Реми"], [("1", "Мыло", "шт", _line(10, 0, 4, 6))],
                 total=[10, 0, 4, 6])
    # Портим итог так, будто в отчёте был склад, которого разбор не увидел.
    from openpyxl import load_workbook
    book = load_workbook(path)
    book.active.cell(10, 8 + 2, 99)
    book.save(path)

    assert not ledger.read(path).balanced


# --- выводы --------------------------------------------------------------------

@pytest.fixture
def report(tmp_path):
    """Ведомость, в которой есть каждый разбираемый случай."""
    path = _book(tmp_path, ["Реми", "Сити Молл", "ВС - В пути Сэм"], [
        # ходовой везде, остатки есть
        ("1", "Салфетки", "шт", _line(100, 0, 60, 40, 80, 0, 30, 50, 0, 0, 0, 0)),
        # кончился в «Сити Молл», в «Реми» остаток есть
        ("2", "Паста", "шт", _line(50, 0, 10, 40, 12, 0, 12, 0, 0, 0, 0, 0)),
        # остатка меньше, чем ушло, — кончится следующим
        ("3", "Патчи", "шт", _line(30, 0, 20, 10, 0, 0, 0, 0, 0, 0, 0, 0)),
        # отрицательный остаток: расход больше, чем было
        ("4", "Пакет", "шт", _line(5, 0, 23, -18, 0, 0, 0, 0, 0, 0, 0, 0)),
        # лежит в пути, движения нет
        ("5", "Маска", "шт", _line(0, 0, 0, 0, 0, 0, 0, 0, 400, 0, 0, 400)),
    ])
    return ledger.build(ledger.read(path))


def test_топ_упорядочен_по_расходу(report):
    # Салфетки 90, Пакет 23, Паста 22, Патчи 20.
    assert [item.name for item in report.top] == [
        "Салфетки", "Пакет", "Паста", "Патчи", "Маска"]
    assert report.top[0].move.outgoing == 90


def test_доля_расхода_считается_от_всех_магазинов(report):
    # 90 + 22 + 20 + 23 = 155
    assert report.total.outgoing == 155
    assert report.top[0].share == pytest.approx(90 / 155)


def test_товар_ходовой_везде_отличим_от_одного_магазина(report):
    """«В скольких магазинах был расход» — разные решения при равном расходе."""
    by_name = {item.name: item for item in report.items}

    assert by_name["Салфетки"].active == ["Реми", "Сити Молл"]
    assert by_name["Патчи"].active == ["Реми"]


def test_кончилось_а_расход_был(report):
    """Остатка нет, а расход был — то, что нужно смотреть первым."""
    found = {(line.name, line.store) for line in report.shortages}

    assert ("Паста", "Сити Молл") in found
    assert ("Паста", "Реми") not in found, "в «Реми» остаток есть"
    assert ("Салфетки", "Реми") not in found


def test_остатка_меньше_чем_ушло(report):
    found = {(line.name, line.store) for line in report.tight}

    # Патчи: ушло 20, осталось 10. Салфетки: ушло 60, осталось 40 — тоже меньше
    # ушедшего, и попасть в список должны обе.
    assert ("Патчи", "Реми") in found
    assert ("Салфетки", "Реми") in found
    assert ("Паста", "Реми") not in found, "осталось 40 при расходе 10"


def test_отрицательный_остаток_выделен_отдельно(report):
    """Минус — ошибка учёта: завозом не лечится, и причина другая.

    В список кончившихся он попадает тоже: товара на полке действительно нет.
    Но искать по нему нужно непроведённый приход, а не поставщика.
    """
    assert [line.name for line in report.negative] == ["Пакет"]
    assert ("Пакет", "Реми") in {(l.name, l.store) for l in report.shortages}


def test_склады_в_пути_не_попадают_в_расход(report):
    """Продаж в пути не бывает, а остаток видеть нужно."""
    assert all(line.store != "ВС - В пути Сэм" for line in report.shortages)
    assert [(line.name, line.move.closing) for line in report.transit] == [
        ("Маска", 400)]
    # Остаток магазинов: Реми 40+40+10-18=72, Сити Молл 50. Четыреста в пути
    # сюда не входят.
    assert report.total.closing == 122, "в пути в остаток магазинов не входит"


def test_магазины_упорядочены_по_расходу(report):
    assert [total.store.title for total in report.stores] == ["Реми", "Сити Молл"]
    assert report.stores[0].move.outgoing == 113, "60+10+20+23"
    assert report.stores[0].ran_out == 1, "«Пакет» ушёл в минус"


# --- выгрузка ------------------------------------------------------------------

def test_выгрузка_раскладывает_по_листам(report, tmp_path):
    from openpyxl import load_workbook

    target = ledger.save(report, str(tmp_path / "разбор.xlsx"))
    book = load_workbook(target)

    assert book.sheetnames == ["Главное", "Не хватило", "Топ товаров",
                               "По магазинам", "Свод"]
    # Свод: на каждый магазин две колонки, склады «в пути» в него не входят.
    head = [book["Свод"].cell(4, column).value
            for column in range(1, book["Свод"].max_column + 1)]
    assert head[:3] == ["Артикул", "Товар", "Ед."]
    assert "ВС - В пути Сэм" not in head


def test_имя_файла_по_умолчанию_с_датой(tmp_path):
    name = ledger.default_name(str(tmp_path))

    assert name.endswith(".xlsx")
    assert "Ведомость" in name
