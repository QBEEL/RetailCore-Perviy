"""Сопоставление строк УПД с номенклатурой 1С и файл загрузки кодов.

Проверяется главное свойство: программа не подставляет номенклатуру, в которой
не уверена. Коды, заведённые чужому товару, в 1С выглядят точно так же, как
заведённые своему, и находятся не раньше инвентаризации — поэтому похожее
название остаётся вопросом к человеку, а не ответом.

Второе свойство — про молчание. Строка без номенклатуры в файл не попадает, и
сколько кодов не попало, говорится вслух: загруженная половина в 1С выглядит
ровно как загруженное целиком.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import onec, upd
from app.core.marking.onec import MatchKind
from app.core.marking.reconcile import Reconciliation

# Документ на две позиции: у первой два экземпляра, у второй один.
DOCUMENT = """<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="ON_NSCHFDOPPR_test" ВерсФорм="5.03">
  <Документ КНД="1115131" Функция="СЧФДОП">
    <СвСчФакт НомерДок="3349" ДатаДок="09.09.2026">
      <СвПрод>
        <ИдСв><СвЮЛУч НаимОрг="НЭЙТИВ ООО" ИННЮЛ="7724542709"/></ИдСв>
      </СвПрод>
    </СвСчФакт>
    <ТаблСчФакт>
      <СведТов НомСтр="1" НаимТов="Бюстгальтер АТЛ-Эрика, нимфа (M нимфа)"
               НаимЕдИзм="шт" КолТов="2.00" ЦенаТов="3503.27">
        <ДопСведТов КодТов="04680962313750">
          <НомСредИдентТов>
            <КИЗ>0104680962313750215PZaBNk&lt;kh"JG</КИЗ>
            <КИЗ>0104680962313750215US!!UXXqj"CS</КИЗ>
          </НомСредИдентТов>
        </ДопСведТов>
      </СведТов>
      <СведТов НомСтр="2" НаимТов="Трусы стринги АТЛ-Ланж, небо (S небо)"
               НаимЕдИзм="шт" КолТов="1.00" ЦенаТов="1628.50">
        <ДопСведТов КодТов="04680962313217">
          <НомСредИдентТов>
            <КИЗ>0104680962313217215Q,aW'F1rG2KT</КИЗ>
          </НомСредИдентТов>
        </ДопСведТов>
      </СведТов>
    </ТаблСчФакт>
  </Документ>
</Файл>
"""

FIRST = '0104680962313750215PZaBNk<kh"JG'
SECOND = '0104680962313750215US!!UXXqj"CS'
THIRD = "0104680962313217215Q,aW'F1rG2KT"

# Выгрузка номенклатуры такая же, какой её отдаёт 1С: к названию дописана
# единица измерения, размер вынесен в характеристику, код на строке свой.
CATALOG_ROWS = [
    ("Бюстгальтер АТЛ-Эрика, нимфа (M нимфа), шт", "00-00127672", "M"),
    ("Бюстгальтер АТЛ-Эрика, нимфа (L нимфа), шт", "00-00127672", "L"),
    ("Трусы стринги АТЛ-Ланж, небо (S небо), шт", "00-00127682", "S"),
    ("Трусы стринги АТЛ-Ланж, небо (M небо), шт", "00-00127682", "M"),
]


@pytest.fixture
def document(tmp_path) -> Path:
    path = tmp_path / "ON_NSCHFDOPPR_2BM-1.xml"
    path.write_bytes(DOCUMENT.encode("cp1251"))
    return path


def _catalog_file(tmp_path, rows=None, headers=None, barcodes=None) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.append(headers or ["Номенклатура", "Код номенклатуры", "Характеристика"]
                 + (["Штрихкод"] if barcodes else []))
    for index, row in enumerate(rows if rows is not None else CATALOG_ROWS):
        sheet.append(list(row) + ([barcodes[index]] if barcodes else []))
    path = tmp_path / "Номенклатура.xlsx"
    book.save(path)
    return path


@pytest.fixture
def catalog(tmp_path) -> onec.Catalog:
    return onec.read_catalog(str(_catalog_file(tmp_path)))


@pytest.fixture
def session(document) -> Reconciliation:
    return Reconciliation(upd.read(str(document)))


# --- чтение выгрузки ------------------------------------------------------------

def test_читает_выгрузку_номенклатуры(catalog):
    assert len(catalog.items) == 4
    assert catalog.items[0].code == "00-00127672"
    assert catalog.items[0].feature == "M"
    assert not catalog.has_barcodes


def test_шапку_ищет_не_только_в_первой_строке(tmp_path):
    """1С ставит над шапкой название отчёта и период — это не повод сдаться."""
    book = Workbook()
    sheet = book.active
    sheet.append(["Номенклатура и характеристики"])
    sheet.append([])
    sheet.append(["Номенклатура", "Код номенклатуры", "Характеристика"])
    for row in CATALOG_ROWS:
        sheet.append(list(row))
    path = tmp_path / "с шапкой.xlsx"
    book.save(path)

    read = onec.read_catalog(str(path))

    assert len(read.items) == 4


def test_файл_без_кода_номенклатуры_отвергается_с_объяснением(tmp_path):
    path = _catalog_file(tmp_path, headers=["Номенклатура", "Цена"],
                         rows=[("Что-то", 100)])

    with pytest.raises(onec.OnecProblem) as problem:
        onec.read_catalog(str(path))

    assert "Код номенклатуры" in str(problem.value)


def test_штрихкод_не_становится_числом(tmp_path):
    """Excel успевает сделать из штрихкода число, и тогда он с хвостом «.0»."""
    path = _catalog_file(tmp_path, barcodes=[4680962313750, "", "", ""])

    read = onec.read_catalog(str(path))

    assert read.has_barcodes
    assert read.items[0].barcode == "4680962313750"


# --- сопоставление ---------------------------------------------------------------

def test_названия_сходятся_после_отбрасывания_единицы(catalog, document):
    """1С дописывает «, шт», поставщик — нет. Без этого не сошлось бы ничего."""
    lines = upd.read(str(document)).marked_lines

    found = onec.match(lines, catalog)

    assert [item.kind for item in found] == [MatchKind.NAME, MatchKind.NAME]
    assert found[0].item.code == "00-00127672"
    assert found[0].item.feature == "M"
    assert found[1].item.code == "00-00127682"
    assert "можно выгружать" in onec.summarize(found)


def test_штрихкод_важнее_названия(tmp_path, document):
    """Название поставщик пишет как хочет, а GTIN у товара один."""
    path = _catalog_file(
        tmp_path,
        rows=[("Совершенно другое название", "00-00999999", "M")] + CATALOG_ROWS[1:],
        barcodes=["04680962313750", "", "", ""])
    catalog = onec.read_catalog(str(path))
    lines = upd.read(str(document)).marked_lines

    found = onec.match(lines, catalog)

    assert found[0].kind is MatchKind.BARCODE
    assert found[0].item.code == "00-00999999"


def test_похожее_название_не_применяется_само(tmp_path, document):
    """Коды, заведённые чужому товару, не находятся до инвентаризации."""
    path = _catalog_file(tmp_path, rows=[
        ("Бюстгальтер АТЛ-Эрика, небо (M небо), шт", "00-00127670", "M"),
        ("Трусы стринги АТЛ-Ланж, небо (S небо), шт", "00-00127682", "S"),
    ])
    catalog = onec.read_catalog(str(path))
    lines = upd.read(str(document)).marked_lines

    found = onec.match(lines, catalog)

    assert found[0].kind is MatchKind.SIMILAR
    assert not found[0].ready
    assert found[0].candidates[0].code == "00-00127670"
    assert "ждут подтверждения 1" in onec.summarize(found)


def test_одно_название_на_две_номенклатуры_остаётся_вопросом(tmp_path, document):
    """Различаются они чем-то, чего в УПД нет, — выбирать за человека нельзя."""
    path = _catalog_file(tmp_path, rows=[
        ("Бюстгальтер АТЛ-Эрика, нимфа (M нимфа), шт", "00-00127672", "M"),
        ("Бюстгальтер АТЛ-Эрика, нимфа (M нимфа), шт", "00-00555555", "M"),
    ])
    catalog = onec.read_catalog(str(path))
    lines = upd.read(str(document)).marked_lines

    found = onec.match(lines, catalog)

    assert found[0].kind is MatchKind.SIMILAR
    assert len(found[0].candidates) == 2


def test_ручная_привязка_сильнее_всего_и_ищется_по_gtin(tmp_path, document):
    path = _catalog_file(tmp_path, rows=[
        ("Совсем не то", "00-00000001", "M"),
        ("Трусы стринги АТЛ-Ланж, небо (S небо), шт", "00-00127682", "S"),
    ])
    catalog = onec.read_catalog(str(path))
    lines = upd.read(str(document)).marked_lines
    links = {"4680962313750": {"code": "00-00127672", "feature": "M",
                               "name": "Бюстгальтер АТЛ-Эрика, нимфа (M нимфа)"}}

    found = onec.match(lines, catalog, links)

    assert found[0].kind is MatchKind.MANUAL
    assert found[0].item.code == "00-00127672"
    assert found[0].ready


def test_привязка_переживает_исчезновение_позиции_из_выгрузки(tmp_path, document):
    """Файлу загрузки нужны код и характеристика, и они в самой привязке."""
    catalog = onec.read_catalog(str(_catalog_file(tmp_path, rows=CATALOG_ROWS[1:])))
    lines = upd.read(str(document)).marked_lines
    links = {"4680962313750": {"code": "00-00127672", "feature": "M", "name": "Эрика"}}

    found = onec.match(lines, catalog, links)

    assert found[0].ready
    assert found[0].item.code == "00-00127672"


# --- файл для 1С ------------------------------------------------------------------

def test_в_файл_идут_только_сверенные_коды(catalog, session, tmp_path):
    """Код, которого не нашлось на товаре, — это вещь, которая не приехала."""
    session.scan(FIRST)
    session.scan(THIRD)
    found = onec.match(session.document.marked_lines, catalog)

    rows, report = onec.build_rows(found, session)

    assert report.rows == 2
    assert rows == [("00-00127672", "M", FIRST), ("00-00127682", "S", THIRD)]
    assert report.clean


def test_можно_выгрузить_весь_документ_не_сверяя(catalog, session):
    found = onec.match(session.document.marked_lines, catalog)

    rows, report = onec.build_rows(found, session, only_scanned=False)

    assert report.rows == 3
    assert [row[2] for row in rows] == [FIRST, SECOND, THIRD]


def test_позиция_без_номенклатуры_не_попадает_в_файл_и_об_этом_говорится(
        tmp_path, session):
    """Загруженная половина в 1С выглядит ровно как загруженное целиком."""
    catalog = onec.read_catalog(str(_catalog_file(tmp_path, rows=CATALOG_ROWS[2:])))
    for code in (FIRST, SECOND, THIRD):
        session.scan(code)
    found = onec.match(session.document.marked_lines, catalog)

    rows, report = onec.build_rows(found, session)

    assert report.rows == 1
    assert not report.clean
    assert report.skipped_codes == 2
    assert [line.number for line in report.skipped_lines] == ["1"]
    assert "не попало кодов: 2" in report.summary


def test_файл_xlsx_повторяет_шаблон_1с(catalog, session, tmp_path):
    session.scan(FIRST)
    found = onec.match(session.document.marked_lines, catalog)

    report = onec.save(found, session, str(tmp_path / "Загрузка.xlsx"))

    book = load_workbook(report.path)
    sheet = book.active
    assert [cell.value for cell in sheet[1]] == list(onec.COLUMNS)
    assert [cell.value for cell in sheet[2]] == ["00-00127672", "M", FIRST]
    # Текстовый формат: числом Excel съел бы ведущий ноль вместе с годностью кода.
    assert sheet.cell(row=2, column=3).number_format == "@"


def test_код_с_точкой_с_запятой_переживает_выгрузку(catalog, session, tmp_path):
    """В наборе GS1 есть и «;», и кавычка — обе встретились в живой поставке."""
    session.scan(FIRST)
    found = onec.match(session.document.marked_lines, catalog)

    report = onec.save(found, session, str(tmp_path / "Загрузка.xlsx"))

    assert load_workbook(report.path).active.cell(row=2, column=3).value == FIRST


def test_выгружать_нечего_говорится_отказом_а_не_пустым_файлом(catalog, session,
                                                               tmp_path):
    found = onec.match(session.document.marked_lines, catalog)
    destination = tmp_path / "Пусто.xlsx"

    with pytest.raises(onec.OnecProblem):
        onec.save(found, session, str(destination))

    assert not destination.exists()


def test_имя_файла_по_умолчанию_называет_документ(session, tmp_path):
    name = os.path.basename(onec.default_name(session, str(tmp_path)))

    assert name == "Загрузка в 1С — УПД 3349.xlsx"


# --- раздел на вкладке ---------------------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, tmp_path, monkeypatch, document):
    from app.core.settings import AppSettings
    from app.ui import marking_page

    picked = {"open": str(document), "save": str(tmp_path / "Загрузка.xlsx")}

    class Dialog:
        @staticmethod
        def getOpenFileName(*_args, **_kwargs):
            return picked["open"], ""

        @staticmethod
        def getSaveFileName(*_args, **_kwargs):
            return picked["save"], ""

    monkeypatch.setattr(marking_page, "QFileDialog", Dialog)
    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    page = marking_page.MarkingPage(settings, lambda *_: None)
    page._picked = picked
    return page


def _load(page, document, catalog_path):
    page._picked["open"] = str(document)
    page.load_upd()
    page._picked["open"] = str(catalog_path)
    page.load_catalog()


def test_вкладка_сопоставляет_и_запоминает_выгрузку(page, document, tmp_path):
    _load(page, document, _catalog_file(tmp_path))

    assert page.progress.item(0, 5).text() == "00-00127672 · M"
    assert page.onec_button.isEnabled()
    assert page.settings.marking_onec_catalog.endswith("Номенклатура.xlsx")


def test_неподтверждённая_строка_не_даёт_кода_но_показывает_похожее(page, document,
                                                                    tmp_path):
    path = _catalog_file(tmp_path, rows=[
        ("Бюстгальтер АТЛ-Эрика, небо (M небо), шт", "00-00127670", "M"),
        ("Трусы стринги АТЛ-Ланж, небо (S небо), шт", "00-00127682", "S"),
    ])
    _load(page, document, path)

    assert page.progress.item(0, 5).text().startswith("похоже:")
    assert page.progress.item(1, 5).text() == "00-00127682 · S"


def test_ручная_привязка_запоминается_и_ложится_в_строку(page, document, tmp_path,
                                                         monkeypatch):
    from app.ui import marking_page

    path = _catalog_file(tmp_path, rows=[
        ("Бюстгальтер АТЛ-Эрика, небо (M небо), шт", "00-00127670", "M"),
        ("Трусы стринги АТЛ-Ланж, небо (S небо), шт", "00-00127682", "S"),
    ])
    _load(page, document, path)

    class Picker:
        def __init__(self, line, catalog, parent=None, current_key=""):
            self._catalog = catalog

        def exec(self):
            return 1

        cleared = False

        @property
        def chosen(self):
            return self._catalog.items[0]

    monkeypatch.setattr(marking_page, "NomenclatureDialog", Picker)
    page.progress.setCurrentCell(0, 0)
    page.link_selected()

    assert page.progress.item(0, 5).text() == "00-00127670 · M"
    assert page.settings.marking_onec_links["4680962313750"]["code"] == "00-00127670"


def test_файл_для_1с_пишется_со_вкладки(page, document, tmp_path):
    _load(page, document, _catalog_file(tmp_path))
    page.scan_edit.setText(FIRST)
    page.accept_scan()

    page.save_onec()

    saved = load_workbook(tmp_path / "Загрузка.xlsx").active
    assert saved.max_row == 2
    assert saved.cell(row=2, column=3).value == FIRST


def test_без_выгрузки_номенклатуры_кнопка_не_работает_и_сказано_почему(page,
                                                                       document):
    page.load_upd()

    assert not page.onec_button.isEnabled()
    assert "Выгрузка номенклатуры не загружена" in page.onec_hint.text()
