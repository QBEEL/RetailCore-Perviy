"""Тесты истории данных: создание снимков, чтение, удаление, сравнение версий."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Column, FieldRole, Record, Sheet
from app.core.snapshots import compare, schema, store
from app.core.snapshots.models import SnapshotProduct
from app.core.workbook import prepare_record

_HEADER = ["Артикул", "Штрихкод", "Номенклатура", "РРЦ", "Бренд", "Комментарий"]
_ROLES = (FieldRole.ARTICLE, FieldRole.EAN, FieldRole.NAME,
          FieldRole.PRICE, FieldRole.BRAND, FieldRole.OTHER)


def _sheet(path: Path, rows: list[list[object]], sheet_name: str = "Прайс") -> Sheet:
    """Собирает Sheet так же, как это делает workbook.load_sheet."""
    columns = [Column(index=i, title=title, role=role)
               for i, (title, role) in enumerate(zip(_HEADER, _ROLES))]
    records = []
    for offset, values in enumerate(rows):
        by_role = {role: value for (_, role), value in zip(zip(_HEADER, _ROLES), values)
                   if role is not FieldRole.OTHER and value is not None}
        record = Record(row=offset + 2, values=list(values), by_role=by_role)
        prepare_record(record, frozenset())
        records.append(record)
    return Sheet(path=str(path), sheet_name=sheet_name, header_row=0,
                 columns=columns, records=records)


def _catalog_file(tmp_path: Path, name: str, content: str) -> Path:
    """Файл нужен настоящий: снимок считает sha256 его содержимого."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "snapshots.db")


# --- схема и миграции ----------------------------------------------------------

def test_migrate_is_idempotent(db: str) -> None:
    with store.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION
    with store.connect(db) as connection:  # повторное открытие ничего не ломает
        assert schema.migrate(connection) == schema.VERSION
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"snapshot", "product"} <= tables


def test_newer_database_is_left_alone(db: str) -> None:
    """Базу от более новой версии нельзя «понижать»: та версия применила бы
    свои миграции повторно и упала на уже существующей колонке."""
    with store.connect(db) as connection:
        connection.execute(f"PRAGMA user_version = {schema.VERSION + 5}")
        connection.commit()

    with store.connect(db) as connection:
        assert schema.migrate(connection) == schema.VERSION + 5
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION + 5


def test_half_applied_migration_recovers(db: str) -> None:
    """Колонка уже добавлена, а номер версии откатился — так выглядела база
    после того, как её открыл exe предыдущей версии."""
    with store.connect(db) as connection:
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    with store.connect(db) as connection:          # не должно падать
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION
        columns = {row[1] for row in connection.execute("PRAGMA table_info(snapshot)")}
        assert "layout" in columns
        assert len([c for c in columns if c == "layout"]) == 1


def test_missing_database_is_created(db: str) -> None:
    assert store.list_snapshots(db) == []
    assert Path(db).exists()


# --- создание снимков ------------------------------------------------------------

def test_create_stores_products_and_metadata(tmp_path: Path, db: str) -> None:
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    sheet = _sheet(path, [
        ["zrp0050per", "4603720459040", "Духи Апельсин (50мл)", 7990, "Z&R", "хит"],
        ["zrp0010per", "4603720459057", "Духи Апельсин (10мл)", 2790, "Z&R", None],
    ])

    snapshot = store.create(sheet, db)

    assert snapshot is not None
    assert snapshot.total_products == 2
    assert snapshot.source_file_name == "catalog.xlsx"
    assert snapshot.sheet_name == "Прайс"
    assert snapshot.brand == "Z&R"          # преобладающее значение по файлу
    assert snapshot.source_file_hash

    products = store.products(snapshot.id, db)
    assert [p.article for p in products] == ["zrp0050per", "zrp0010per"]
    assert products[0].price == 7990
    assert products[0].name == "Духи Апельсин (50мл)"
    # Колонка, у которой нет роли, сохраняется в payload и не теряется.
    assert products[0].values[-1] == "хит"


def test_identical_content_does_not_create_second_snapshot(tmp_path: Path, db: str) -> None:
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    sheet = _sheet(path, [["a1", "1", "Товар", 100, "Z&R", None]])

    first = store.create(sheet, db)
    second = store.create(sheet, db)

    assert first is not None
    assert second is None                      # повтор той же выгрузки
    assert len(store.list_snapshots(db)) == 1


def test_other_price_column_creates_new_snapshot(tmp_path: Path, db: str) -> None:
    """У дистрибьютора цена есть в долларах и в рублях. Смена колонки в
    настройках обязана дать новый снимок, а не отказ по дублю."""
    path = _catalog_file(tmp_path, "distributor.xlsx", "июнь")
    rows = [["a1", "1", "Товар", 10.7, "AHC", 916.68]]

    usd = _sheet(path, rows)
    first = store.create(usd, db)

    # Роль «цена» переставлена на колонку с рублями — как это делает
    # пользователь в «Настройках» → «Колонки файла».
    rub = _sheet(path, rows)
    rub.columns[3].role = FieldRole.OTHER
    rub.columns[5].role = FieldRole.PRICE
    for record in rub.records:
        record.by_role[FieldRole.PRICE] = record.values[5]
    second = store.create(rub, db)

    assert first is not None and second is not None
    assert second.id != first.id
    assert store.products(first.id, db)[0].price == 10.7
    assert store.products(second.id, db)[0].price == 916.68


def test_snapshot_records_price_column(tmp_path: Path, db: str) -> None:
    """Из какой колонки взята цена — видно в истории, иначе цифры необъяснимы."""
    path = _catalog_file(tmp_path, "distributor.xlsx", "июнь")
    snapshot = store.create(_sheet(path, [["a1", "1", "Товар", 10.7, "AHC", None]]), db)

    assert snapshot is not None
    assert snapshot.description == "РРЦ"


def test_changed_file_creates_new_version(tmp_path: Path, db: str) -> None:
    august = _catalog_file(tmp_path, "catalog.xlsx", "август")
    store.create(_sheet(august, [["a1", "1", "Товар", 100, "Z&R", None]]), db)

    september = _catalog_file(tmp_path, "catalog_new.xlsx", "сентябрь")
    store.create(_sheet(september, [
        ["a1", "1", "Товар", 120, "Z&R", None],
        ["a2", "2", "Новый товар", 300, "Z&R", None],
    ]), db)

    snapshots = store.list_snapshots(db)
    assert len(snapshots) == 2
    assert [s.total_products for s in snapshots] == [2, 1]   # новые сверху


def test_delete_removes_products_too(tmp_path: Path, db: str) -> None:
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    snapshot = store.create(_sheet(path, [["a1", "1", "Товар", 100, "Z&R", None]]), db)
    assert snapshot is not None

    assert store.delete(snapshot.id, db) is True
    assert store.list_snapshots(db) == []
    with store.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 0
    assert store.delete(snapshot.id, db) is False           # удалять больше нечего


def test_log_stays_next_to_its_database(tmp_path: Path, db: str) -> None:
    """Работа с отдельной базой не должна писать в журнал пользователя."""
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    store.create(_sheet(path, [["a1", "1", "Товар", 100, "Z&R", None]]), db)

    log = Path(db).parent / "snapshot.log"
    assert log.exists()
    assert "Snapshot created" in log.read_text(encoding="utf-8")


def test_delete_frees_disk_space(tmp_path: Path, db: str) -> None:
    """Чистка истории должна уменьшать файл базы, а не только прятать строки."""
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    rows = [[f"a{i}", str(i), f"Товар {i} " + "х" * 200, 100, "Z&R", None] for i in range(3000)]
    snapshot = store.create(_sheet(path, rows), db)
    assert snapshot is not None
    filled = store.database_size(db)

    store.delete(snapshot.id, db)

    assert store.database_size(db) < filled / 2


def test_progress_reports_every_record(tmp_path: Path, db: str) -> None:
    path = _catalog_file(tmp_path, "catalog.xlsx", "август")
    rows = [[f"a{i}", str(i), f"Товар {i}", 100 + i, "Z&R", None] for i in range(5)]
    seen: list[tuple[int, int]] = []

    store.create(_sheet(path, rows), db, progress=lambda done, total: seen.append((done, total)))

    assert seen[-1] == (5, 5)


# --- сравнение версий ---------------------------------------------------------------

def _product(article: str, name: str = "Товар", price: float | None = 100.0,
             volume: str = "", ean: str = "") -> SnapshotProduct:
    return SnapshotProduct(row=2, article=article, ean=ean, name=name,
                           price=price, volume=volume)


def test_diff_finds_added_removed_and_price_change() -> None:
    before = [_product("a1", price=100.0), _product("a2"), _product("a3")]
    after = [_product("a1", price=120.0), _product("a2"), _product("a4")]

    result = compare.diff(before, after)

    assert [p.article for p in result.added] == ["a4"]
    assert [p.article for p in result.removed] == ["a3"]
    assert [c.after.article for c in result.price_changes] == ["a1"]
    assert result.price_changes[0].price_delta == 20.0
    assert result.total == 3


def test_diff_finds_characteristic_change() -> None:
    before = [_product("a1", name="Духи Апельсин", volume="50 мл")]
    after = [_product("a1", name="Духи Апельсин и Жасмин", volume="55 мл")]

    change = compare.diff(before, after).changed[0]

    assert set(change.fields) == {"name", "volume"}
    assert not change.price_changed


def test_diff_ignores_price_rounding_noise() -> None:
    assert compare.diff([_product("a1", price=2790.0)], [_product("a1", price=2790.0)]).total == 0
    assert compare.diff([_product("a1", price=None)], [_product("a1", price=None)]).total == 0


def test_price_direction_and_percent() -> None:
    """Направление обязано отличать подорожание от подешевения."""
    up = compare.diff([_product("a1", price=100.0)], [_product("a1", price=125.0)]).changed[0]
    assert up.price_rose is True
    assert up.price_percent == pytest.approx(25.0)

    down = compare.diff([_product("a1", price=100.0)], [_product("a1", price=75.0)]).changed[0]
    assert down.price_rose is False
    assert down.price_percent == pytest.approx(-25.0)


def test_price_direction_unknown_when_one_side_missing() -> None:
    """Появившаяся цена — не подешевение: направление считать не от чего."""
    appeared = compare.diff([_product("a1", price=None)], [_product("a1", price=50.0)]).changed[0]
    assert appeared.price_rose is None
    assert appeared.price_percent is None

    gone = compare.diff([_product("a1", price=50.0)], [_product("a1", price=None)]).changed[0]
    assert gone.price_rose is None


# --- описание файла и пригодность листа -------------------------------------------

def test_brand_of_multibrand_file_is_not_a_single_name(tmp_path: Path, db: str) -> None:
    """У дистрибьютора десятки брендов — подписывать файл одним из них нельзя."""
    path = _catalog_file(tmp_path, "distributor.xlsx", "июнь")
    rows = [[f"a{i}", str(i), f"Товар {i}", 100, f"Бренд {i % 12}", None] for i in range(60)]

    snapshot = store.create(_sheet(path, rows), db)

    assert snapshot is not None
    assert snapshot.brand == "12 брендов"


def test_brand_of_single_brand_file_is_its_name(tmp_path: Path, db: str) -> None:
    path = _catalog_file(tmp_path, "brand.xlsx", "июнь")
    rows = [[f"a{i}", str(i), f"Товар {i}", 100, "Z&R", None] for i in range(30)]

    snapshot = store.create(_sheet(path, rows), db)

    assert snapshot is not None
    assert snapshot.brand == "Z&R"


def test_service_sheet_is_not_trackable(tmp_path: Path) -> None:
    """Лист-обложка «Главная» не должен попадать в историю."""
    path = _catalog_file(tmp_path, "supplier.xlsx", "июнь")
    cover = _sheet(path, [
        [None, None, "Контактная информация", None, None, None],
        [None, None, "Дата заказа", None, None, None],
        [None, None, "ФИО получателя", None, None, None],
        [None, None, "Город получателя", None, None, None],
        [None, None, "Контактный телефон", None, None, None],
        [None, None, "Комментарий", None, None, None],
    ])
    assert store.is_trackable(cover) is False

    catalog = _sheet(path, [[f"a{i}", str(i), f"Товар {i}", 100, "Z&R", None] for i in range(30)])
    assert store.is_trackable(catalog) is True


def test_diff_matches_by_ean_when_article_missing() -> None:
    before = [_product("", ean="4603720459040", price=100.0)]
    after = [_product("", ean="4603720459040", price=150.0)]

    result = compare.diff(before, after)

    assert not result.added and not result.removed
    assert result.price_changes[0].price_delta == 50.0
