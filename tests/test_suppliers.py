"""Тесты базы поставщиков: хранилище, узнавание, сохранённые привязки."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import suppliers
from app.core.pricing import (
    MatchOptions,
    PriceStatus,
    SupplierProfile,
    load_supplier,
    load_template,
)
from app.core.suppliers import LinkKey, Supplier, SupplierLayout, SupplierLink


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "suppliers.db")


# --- ключ привязки ------------------------------------------------------------

def test_идентификаторы_1с_сильнее_артикула():
    key = LinkKey.of(nomenclature="A-1", characteristic="B-2", article="ZRP01", name="Духи")
    assert key.strong
    assert key.identity.startswith("g:")
    # Слабые ключи остаются запасными — для шаблонов без идентификаторов.
    assert len(key.lookup()) == 3


def test_без_идентификаторов_ключ_артикул_с_объёмом():
    key = LinkKey.of(article="ZRP-01", volume="50 мл", name="Духи")
    assert not key.strong
    assert key.identity == "a:zrp01:50 мл"


def test_пустая_строка_ключа_не_даёт():
    assert not LinkKey.of()


# --- хранилище ----------------------------------------------------------------

def test_поставщик_заводится_один_раз(db):
    first = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    second = suppliers.save_supplier(Supplier(name="  zielinski  "), db)
    assert first.id == second.id
    assert len(suppliers.list_suppliers(db)) == 1


def test_структура_обновляется_а_не_копится(db):
    supplier = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    profile = SupplierProfile(sheet="Лист1", price_map={"Закупочная": "Закупка новая"})
    layout = SupplierLayout(
        supplier_id=supplier.id, profile=profile, signature="abc", titles=["Артикул", "Цена"])
    saved = suppliers.save_layout(layout, db)
    assert saved.uses == 1

    again = suppliers.save_layout(
        SupplierLayout(supplier_id=supplier.id, profile=profile,
                       signature="abc", titles=["Артикул", "Цена"]), db)
    assert again.id == saved.id
    assert again.uses == 2
    assert len(suppliers.layouts(supplier.id, db)) == 1


def test_другая_структура_живёт_рядом(db):
    """Поставщик может присылать файлы в нескольких форматах."""
    supplier = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    for signature in ("abc", "def"):
        suppliers.save_layout(
            SupplierLayout(supplier_id=supplier.id, signature=signature,
                           profile=SupplierProfile(sheet="Лист1")), db)
    assert len(suppliers.layouts(supplier.id, db)) == 2


def test_повторная_привязка_заменяет_прежнюю(db):
    supplier = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    key = LinkKey.of(nomenclature="A", characteristic="B")
    suppliers.save_link(SupplierLink(supplier_id=supplier.id, key=key,
                                     supplier_article="OLD"), db)
    suppliers.save_link(SupplierLink(supplier_id=supplier.id, key=key,
                                     supplier_article="NEW"), db)
    saved = suppliers.links(supplier.id, db)
    assert len(saved) == 1
    assert saved[0].supplier_article == "NEW"


def test_удаление_поставщика_забирает_привязки(db):
    supplier = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    suppliers.save_link(SupplierLink(
        supplier_id=supplier.id, key=LinkKey.of(nomenclature="A"), supplier_article="X"), db)
    suppliers.save_layout(SupplierLayout(supplier_id=supplier.id, signature="s"), db)

    assert suppliers.delete_supplier(supplier.id, db)
    assert suppliers.links(supplier.id, db) == []
    assert suppliers.layouts(supplier.id, db) == []


def test_профили_из_настроек_переносятся_один_раз(db):
    profiles = [SupplierProfile(name="Zielinski", price_map={"Закупочная": "Закупка"})]
    assert suppliers.adopt_profiles(profiles, db) == 1
    # Повторный запуск не воскрешает удалённых поставщиков.
    assert suppliers.adopt_profiles(profiles, db) == 0
    assert len(suppliers.list_suppliers(db)) == 1


# --- узнавание ----------------------------------------------------------------

def test_узнавание_по_структуре_когда_имя_файла_обезличено(db):
    supplier = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    titles = ["Категория", "EAN", "Артикул", "Номенклатура", "РРЦ текущая"]
    suppliers.save_layout(
        SupplierLayout(supplier_id=supplier.id, signature=suppliers.signature_of(titles),
                       titles=titles, profile=SupplierProfile(sheet="Лист1")), db)

    guess = suppliers.identify(
        "C:/цены/переоценка_09_2026.xlsx", titles, "Лист1",
        suppliers.list_suppliers(db), suppliers.all_layouts(db))
    assert guess is not None and guess.confident
    assert guess.supplier.name == "Zielinski"
    assert "структура" in guess.reason


def test_узнавание_по_дополнительному_имени(db):
    supplier = suppliers.save_supplier(Supplier(name="Зелински и Розен"), db)
    suppliers.set_aliases(supplier.id, ["zielinski"], db)
    guess = suppliers.identify(
        "C:/цены/Переоценка Zielinski.xlsx", [], "",
        suppliers.list_suppliers(db), suppliers.all_layouts(db),
        suppliers.all_aliases(db))
    assert guess is not None and guess.supplier.id == supplier.id


def test_чужой_файл_не_опознаётся(db):
    suppliers.save_supplier(Supplier(name="Zielinski"), db)
    guess = suppliers.identify(
        "C:/цены/Прайс Другого Поставщика.xlsx", ["Совсем", "Другие", "Колонки"], "Лист1",
        suppliers.list_suppliers(db), suppliers.all_layouts(db))
    assert guess is None or not guess.confident


def test_виды_цен_собираются_по_всем_структурам(db):
    """Виды цен принадлежат базе 1С, а не поставщику: список общий для всех."""
    assert suppliers.known_price_types(db) == []
    first = suppliers.save_supplier(Supplier(name="Первый"), db)
    second = suppliers.save_supplier(Supplier(name="Второй"), db)
    suppliers.save_layout(SupplierLayout(
        supplier_id=first.id, signature="a",
        profile=SupplierProfile(price_map={"Закупочная": "Закупка", "Розница": "РРЦ"})), db)
    suppliers.save_layout(SupplierLayout(
        supplier_id=second.id, signature="b",
        profile=SupplierProfile(price_map={"Закупочная": "Опт"})), db)
    # Частый вид цены идёт первым — его и предложат в диалоге раньше прочих.
    assert suppliers.known_price_types(db)[0] == "Закупочная"
    assert set(suppliers.known_price_types(db)) == {"Закупочная", "Розница"}


def test_сигнатура_не_зависит_от_порядка_колонок():
    assert suppliers.signature_of(["Артикул", "Цена"]) == suppliers.signature_of(["Цена", "Артикул"])
    assert suppliers.signature_of(["Артикул"]) != suppliers.signature_of(["Цена"])


# --- работа с настоящими файлами ---------------------------------------------

DATA = Path(__file__).resolve().parents[1] / "Для переоценки"
TEMPLATE = DATA / "шаблон выгрузки из 1С.xls"
SUPPLIER = DATA / "Переоценка Zielinski.xlsx"

needs_files = pytest.mark.skipif(
    not (TEMPLATE.exists() and SUPPLIER.exists()),
    reason="нет примеров файлов переоценки")


@pytest.fixture(scope="module")
def files():
    return load_template(str(TEMPLATE)), load_supplier(str(SUPPLIER))


@needs_files
def test_идентификаторы_1с_уникальны_для_каждой_строки(files):
    """На этом держатся привязки: пара идентификаторов опознаёт строку однозначно."""
    template, _ = files
    assert template.has_identifiers
    pairs = {
        (template.value_at(record, template.nomenclature_column),
         template.value_at(record, template.characteristic_column))
        for record in template.records
    }
    assert len(pairs) == len(template.records)


@needs_files
def test_колонки_единиц_измерения_за_идентификаторы_не_приняты(files):
    """У каждого вида цены своя колонка с такой же подписью — их надо исключить."""
    template, _ = files
    reserved = {t.unit_guid_column for t in template.valid_types}
    assert template.nomenclature_column not in reserved
    assert template.characteristic_column not in reserved


@needs_files
def test_привязки_поднимают_долю_сопоставления(files, db):
    template, supplier = files

    session = suppliers.open_session(template, supplier, path=db)
    assert not session.known
    first = suppliers.run_comparison(template, supplier, session, MatchOptions())
    session = suppliers.remember_session(session, supplier, path=db)
    assert session.supplier.id

    keys = suppliers.keys_for(template, first.lines)
    saved = 0
    for line, key in zip(first.lines, keys):
        if line.status is PriceStatus.REVIEW and line.alternatives:
            line.assign(line.alternatives[0])
            saved += bool(suppliers.remember_link(line, key, session, path=db))
    assert saved > 0

    session = suppliers.open_session(template, supplier, path=db)
    assert session.known, "поставщик не опознан во второй раз"
    assert len(session.book) == saved
    second = suppliers.run_comparison(template, supplier, session, MatchOptions())

    applied = sum(1 for line in second.lines if line.linked)
    assert applied == saved, "привязка задела строки, для которых её не сохраняли"
    assert second.stats.found > first.stats.found
    assert second.stats.rate > 98


@needs_files
def test_привязка_не_перетекает_на_соседнюю_строку_с_тем_же_артикулом(files, db):
    """Одна ячейка артикула стоит в нескольких строках — привязка строго своя."""
    template, supplier = files
    session = suppliers.open_session(template, supplier, path=db)
    result = suppliers.run_comparison(template, supplier, session, MatchOptions())
    session = suppliers.remember_session(session, supplier, path=db)
    keys = suppliers.keys_for(template, result.lines)

    by_article: dict[str, list[int]] = {}
    for index, line in enumerate(result.lines):
        if line.article:
            by_article.setdefault(line.article, []).append(index)
    shared = next(idx for idx in by_article.values() if len(idx) > 1)

    line = result.lines[shared[0]]
    line.assign(line.alternatives[0] if line.alternatives else line.candidate)
    assert suppliers.remember_link(line, keys[shared[0]], session, path=db)

    session = suppliers.open_session(template, supplier, path=db)
    again = suppliers.run_comparison(template, supplier, session, MatchOptions())
    linked = [index for index, l in enumerate(again.lines) if l.linked]
    assert linked == [shared[0]]


@needs_files
def test_снятая_привязка_забывается(files, db):
    template, supplier = files
    session = suppliers.open_session(template, supplier, path=db)
    result = suppliers.run_comparison(template, supplier, session, MatchOptions())
    session = suppliers.remember_session(session, supplier, path=db)
    keys = suppliers.keys_for(template, result.lines)

    index = next(i for i, l in enumerate(result.lines)
                 if l.status is PriceStatus.REVIEW and l.alternatives)
    result.lines[index].assign(result.lines[index].alternatives[0])
    suppliers.remember_link(result.lines[index], keys[index], session, path=db)
    assert len(session.book) == 1

    assert suppliers.forget_link(keys[index], session, path=db)
    assert len(session.book) == 0
    assert suppliers.links(session.supplier.id, db) == []


@needs_files
def test_структура_запоминается_и_подхватывается(files, db):
    template, supplier = files
    session = suppliers.open_session(template, supplier, path=db)
    chosen = dict(session.profile.price_map)
    session = suppliers.remember_session(session, supplier, path=db)
    assert session.layout.signature == suppliers.signature_of(supplier.titles)

    again = suppliers.open_session(template, supplier, path=db)
    assert again.known
    assert again.profile.price_map == chosen


@needs_files
def test_ручная_роль_колонки_применяется_при_чтении(files, db):
    """Разметка из карточки поставщика меняет то, по чему ищется товар."""
    from app.core.models import FieldRole

    _, supplier = files
    profile = SupplierProfile(role_map={FieldRole.VOLUME.value: "Категория"})
    overrides = profile.overrides(supplier)
    assert overrides, "колонка «Категория» не найдена в прайсе"

    reread = load_supplier(str(SUPPLIER), None, overrides)
    volume = next((c.title for c in reread.columns if c.role is FieldRole.VOLUME), None)
    assert volume == "Категория"


@needs_files
def test_добавленная_вручную_структура_делает_поставщика_узнаваемым(files, db):
    """Поставщик прислал файл нового формата — карточка не должна раздвоиться."""
    _, supplier = files
    known = suppliers.save_supplier(Supplier(name="Zielinski"), db)
    # До добавления структуры обезличенный файл ни с кем не связан.
    assert suppliers.identify(
        "C:/цены/прайс_08.xlsx", supplier.titles, supplier.sheet_name,
        suppliers.list_suppliers(db), suppliers.all_layouts(db)) is None

    suppliers.save_layout(SupplierLayout(
        supplier_id=known.id,
        signature=suppliers.signature_of(supplier.titles),
        titles=list(supplier.titles),
        profile=SupplierProfile(sheet=supplier.sheet_name)), db)

    guess = suppliers.identify(
        "C:/цены/прайс_08.xlsx", supplier.titles, supplier.sheet_name,
        suppliers.list_suppliers(db), suppliers.all_layouts(db))
    assert guess is not None and guess.confident
    assert guess.supplier.id == known.id


@needs_files
def test_явный_выбор_поставщика_сильнее_узнавания(files, db):
    template, supplier = files
    other = suppliers.save_supplier(Supplier(name="Другой поставщик"), db)
    suppliers.remember_session(suppliers.open_session(template, supplier, path=db), supplier, db)

    session = suppliers.open_session(template, supplier, supplier_id=other.id, path=db)
    assert session.supplier.id == other.id
    assert session.reason == "выбран вручную"
