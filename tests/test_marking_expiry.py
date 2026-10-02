"""Срок годности из QR: разбор карточки и вкладка склада."""
from __future__ import annotations

import os
from datetime import date

import pytest

from app.core.marking import expiry
from app.core.marking.codes import GS
from app.core.marking.expiry import ExpiryLevel, days_left, level_of, parse_card

# Содержимое настоящего QR с лосьона, ровно как его отдаёт сканер. Это не JSON,
# а текст «Ключ: значение», и часть полей стоит в одной строке через запятую.
CARD = """Дата нанесения: 2026-02-13T10:26:31.642Z
Дата ввода в оборот: 2026-02-13T10:27:48.405Z
"country": RU
"isMultipleSales":false,"isTracking":false,"manufacturerInn": 5018188659
"manufacturerName": ООО "ТЕХНОЛАБ-К"
КиЗ: 0104681886812442215G'.tT
КиЗ: 0104681886812442215G'.tT
Код товара: 04681886812442
Код ТН ВЭД: 3304990000
Группа ТН ВЭД: 3304
Наименование: Лосьон для тела 300 мл, Жасмин и Кедровое дерево
Код ТГ:35,Наименование ТГ: chemistry
Бренд: hi,dear
Дата производства: 2026-02-04T00:00:00.000Z
Дата эмиссии: 2026-02-06T09:54:38.188Z
Тип эмиссии: LOCAL
Вид упаковки: UNIT
Вид упаковки: UNIT
ИНН владельца товара: 5017137002
Наименование владельца товара: ООО "БИГДИЛС"
Статус: INTRODUCED
Особое состояние: EMPTY
Список дочерних КИ в агрегате:[]
ИНН производителя: 5018188659
Наименование производителя: ООО "ТЕХНОЛАБ-К"
Признак выбытия от не владельца:false,Дата срока годности: 2029-01-31T00:00:00.000Z
Сведения о разрешительной документации:[{"number": ЕАЭС N RU Д-RU.РА07.В.72643/25
"type": CONFORMITY_DECLARATION
"date": 2025-09-04
"statusGroup":1,"indx":273595212}]"""

TODAY = date(2026, 10, 2)


# --- разбор карточки ------------------------------------------------------------------

def test_срок_годности_берётся_по_подписи_из_общей_строки():
    """Поле стоит после запятой в одной строке с другим — построчно его не найти."""
    card = parse_card(CARD)
    assert card.expires == date(2029, 1, 31)
    assert card.problems == []


def test_срок_не_путается_с_другими_датами_карточки():
    card = parse_card(CARD)
    assert card.produced == date(2026, 2, 4)
    assert card.expires != date(2026, 2, 6), "дата эмиссии — не срок годности"
    assert card.expires != date(2025, 9, 4), "дата документа — не срок годности"


def test_поля_товара_читаются_и_не_цепляются_за_соседние():
    card = parse_card(CARD)
    assert card.name == "Лосьон для тела 300 мл, Жасмин и Кедровое дерево"
    assert card.brand == "hi,dear"
    assert card.gtin == "04681886812442"
    assert card.status == "INTRODUCED"
    assert card.owner == 'ООО "БИГДИЛС"'
    assert card.manufacturer == 'ООО "ТЕХНОЛАБ-К"'


def test_код_товара_разобран_и_служит_ключом_повтора():
    card = parse_card(CARD)
    assert card.kiz == "0104681886812442215G'.tT"
    assert card.key == "0104681886812442" + "215G'.tT"
    assert card.serial == "5G'.tT"


def test_дата_берётся_как_написана_без_перевода_часового_пояса():
    """Полночь по Гринвичу — календарный день, а не момент: он не должен съезжать."""
    assert parse_card("Дата срока годности: 2029-01-31T00:00:00.000Z").expires == date(2029, 1, 31)
    assert parse_card("Дата срока годности: 2029-01-31T23:59:59.000Z").expires == date(2029, 1, 31)


@pytest.mark.parametrize("text", [
    "﻿Дата срока годности: 2029-01-31T00:00:00.000Z",
    "]Q1\nДата срока годности: 2029-01-31T00:00:00.000Z",
    "дата срока годности:2029-01-31",
    "ДАТА СРОКА ГОДНОСТИ = 31.01.2029",
    "Срок годности: 2029-01-31",
])
def test_подпись_и_формат_даты_терпимы_к_вариантам(text):
    assert parse_card(text).expires == date(2029, 1, 31)


def test_json_читается_по_ключу_срока():
    text = ('{"Наименование": "Лосьон", "Бренд": "hi,dear", "Код товара": "04681886812442",'
            ' "Срок годности": "2029-01-31T00:00:00.000Z", "Статус": "INTRODUCED"}')
    card = parse_card(text)
    assert card.expires == date(2029, 1, 31)
    assert (card.name, card.brand, card.gtin) == ("Лосьон", "hi,dear", "04681886812442")


def test_json_с_вложенным_полем_и_английскими_ключами():
    card = parse_card('{"product": {"name": "Cream", "expirationDate": "2028-05-01T00:00:00Z"}}')
    assert card.expires == date(2028, 5, 1)
    assert card.name == "Cream"


def test_голый_код_маркировки_отдаёт_срок_из_ai17():
    code = f"010460123456789321Abc123XyZ{GS}17290131{GS}91EE10"
    assert parse_card(code).expires == date(2029, 1, 31)


def test_ai17_с_днём_00_означает_конец_месяца():
    code = f"010460123456789321Abc123XyZ{GS}17280200"
    assert parse_card(code).expires == date(2028, 2, 29)


def test_без_подписи_срок_не_выдумывается():
    """Сканер без кириллицы приносит искажённые подписи: дату угадывать нельзя."""
    garbled = CARD.replace("Дата срока годности", "Lfnf chjrf ujlyjcnb")
    card = parse_card(garbled)
    assert card.expires is None
    assert any("сканер" in problem for problem in card.problems)


def test_пустой_и_чужой_текст_не_роняют_разбор():
    assert parse_card("").problems == ["пустая строка"]
    other = parse_card("https://example.com/item/42")
    assert other.expires is None and other.problems


# --- уровни ---------------------------------------------------------------------------

def test_остаток_дней_и_уровень():
    assert days_left(date(2026, 10, 12), TODAY) == 10
    assert days_left(date(2026, 10, 1), TODAY) == -1
    assert days_left(None, TODAY) is None


def test_граница_просрочки_последний_день_ещё_годен():
    assert level_of(date(2026, 10, 1), 90, TODAY) is ExpiryLevel.EXPIRED
    assert level_of(date(2026, 10, 2), 90, TODAY) is ExpiryLevel.SOON, "сегодня — последний день"


def test_граница_порога_скоро_включительно():
    assert level_of(date(2026, 12, 31), 90, TODAY) is ExpiryLevel.SOON  # ровно 90 дней
    assert level_of(date(2027, 1, 1), 90, TODAY) is ExpiryLevel.OK      # 91 день
    assert level_of(None, 90, TODAY) is ExpiryLevel.UNKNOWN


# --- вкладка ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    return ExpiryTab(AppSettings(_path=tmp_path / "settings.json"))


def _wait(milliseconds: int) -> None:
    from PySide6.QtTest import QTest

    QTest.qWait(milliseconds)


def test_скан_из_многих_строк_даёт_одну_запись(tab):
    """Сканер жмёт Enter после каждой строки: скан кончается тишиной, а не Enter."""
    tab.scan_edit.setPlainText("")
    for line in CARD.splitlines():
        tab.scan_edit.insertPlainText(line + "\n")
    _wait(450)
    assert len(tab.entries) == 1
    assert tab.entries[0].card.expires == date(2029, 1, 31)
    assert tab.scan_edit.toPlainText() == "", "поле очищено под следующий скан"


def test_плашка_показывает_дату_и_остаток(tab):
    tab.accept_scan(CARD, today=TODAY)
    text = tab.verdict_label.text()
    assert "годен до 31.01.2029" in text
    assert "осталось 852 дн." in text
    assert "В порядке" in text
    assert "Лосьон для тела" in tab.detail_label.text()


def test_просроченный_товар_красный(tab):
    old = CARD.replace("2029-01-31", "2026-01-31")
    entry = tab.accept_scan(old, today=TODAY)
    assert entry is not None
    assert "Просрочен" in tab.verdict_label.text()
    assert "просрочен на 244 дн." in tab.verdict_label.text()


def test_повторный_скан_того_же_товара_помечается(tab):
    first = tab.accept_scan(CARD, today=TODAY)
    second = tab.accept_scan(CARD, today=TODAY)
    assert first is not None and second is not None
    assert not first.repeat and second.repeat
    assert "Уже сканировали" in tab.verdict_label.text()


def test_журнал_показывает_товар_срок_и_остаток(tab):
    tab.accept_scan(CARD, today=TODAY)
    model = tab.table.model_
    titles = [column.title for column in model.columns]
    assert model.index(0, titles.index("Годен до")).data() == "31.01.2029"
    assert model.index(0, titles.index("Осталось")).data() == "осталось 852 дн."
    assert model.index(0, titles.index("Товар")).data().startswith("Лосьон")


def test_порог_скоро_меняет_уровень_и_запоминается(tab):
    tab.accept_scan(CARD, today=TODAY)
    assert "В порядке" in tab.verdict_label.text()
    tab.soon_box.setValue(1000)
    assert "Скоро истекает" in tab.verdict_label.text()
    assert tab.settings.marking_expiry_soon_days == 1000


def test_нераспознанный_скан_не_прячется(tab):
    tab.accept_scan("https://example.com/item/42", today=TODAY)
    assert "Срок не найден" in tab.verdict_label.text()
    assert tab.entries[0].card.problems


def test_очистка_журнала(tab):
    tab.accept_scan(CARD, today=TODAY)
    tab.clear_journal()
    assert tab.entries == [] and tab.table.model_.rowCount() == 0
    assert tab.verdict_label.text() == ""


def test_вкладка_стоит_в_маркировке_последней(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.marking_page import EXPIRY_TAB, MarkingPage

    page = MarkingPage(AppSettings(_path=tmp_path / "settings.json"), lambda *_: None)
    assert page.tabs.tabText(EXPIRY_TAB) == "Срок годности"
    assert page.tabs.widget(EXPIRY_TAB) is page.expiry_tab


# --- код с упаковки без срока: ответ «Честного ЗНАКа» ---------------------------------------

# Квадратный DataMatrix на упаковке: товар и серийный номер, срока в нём нет. Именно
# его даёт сканер, когда его наводят на фото маркировки. Код настоящий, с этикетки.
PACK = "0104640470860092215PoDko"
PACK_WITH_TAIL = f"{PACK}{GS}91EE11{GS}92abcdefgh=="

ANSWER = {
    "cis": PACK,
    "cisInfo": {
        "requestedCis": PACK, "gtin": "04640470860092", "status": "INTRODUCED",
        "productName": "Бальзам GROWTH FACTOR для роста и объем волос",
        "brand": "POLUBVI", "ownerName": "ИП САУХ МАРИЯ НИКОЛАЕВНА",
        "producedDate": "2026-01-20T00:00:00.000Z",
        "expirationDate": "2029-01-19T00:00:00.000Z",
    },
}


def _info(raw: dict, **extra):
    from app.core.marking.models import CodeInfo, CodeState

    return CodeInfo(code=PACK, found=True, valid=True, state=CodeState.UNKNOWN,
                    product_name=raw.get("cisInfo", {}).get("productName", ""),
                    raw=raw, **extra)


def test_код_с_упаковки_это_голый_код_без_срока():
    for text in (PACK, PACK_WITH_TAIL):
        card = parse_card(text)
        assert card.bare, "срока в коде нет — его надо спрашивать у системы"
        assert card.expires is None
        assert card.gtin == "04640470860092"


def test_карточка_не_голый_код_а_код_со_сроком_читается_сразу():
    assert not parse_card(CARD).bare
    with_date = parse_card(f"{PACK}{GS}17290119{GS}91EE11")
    assert with_date.expires == date(2029, 1, 19), "срок в самом коде — спрашивать нечего"


def test_ответ_честного_знака_даёт_срок_и_товар():
    card = expiry.from_response(PACK, ANSWER)
    assert card.expires == date(2029, 1, 19)
    assert card.produced == date(2026, 1, 20)
    assert card.name.startswith("Бальзам GROWTH FACTOR")
    assert card.brand == "POLUBVI"
    assert card.kiz == PACK and card.problems == []


def test_ответ_без_срока_называет_поля_которые_пришли():
    card = expiry.from_response(PACK, {"cisInfo": {"gtin": "04640470860092", "status": "INTRODUCED"}})
    assert card.expires is None
    assert "нет срока годности" in card.problems[0]
    assert "gtin" in card.problems[0] and "status" in card.problems[0]


def _wait_until(condition, limit_ms: int = 3000) -> None:
    for _ in range(limit_ms // 20):
        if condition():
            return
        _wait(20)


def test_скан_кода_с_упаковки_спрашивает_систему_и_показывает_срок(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    asked: list[str] = []

    def lookup(code):
        asked.append(code)
        return _info(ANSWER)

    tab = ExpiryTab(AppSettings(_path=tmp_path / "settings.json"), lookup=lookup)
    entry = tab.accept_scan(PACK_WITH_TAIL, today=TODAY)

    assert entry is not None and entry.pending
    assert "Спрашиваем" in tab.verdict_label.text()
    _wait_until(lambda: not entry.pending)

    assert asked == [PACK_WITH_TAIL], "уходит код как отсканирован: приведёт его сервис"
    assert entry.card.expires == date(2029, 1, 19) and entry.source == "ГИС МТ"
    assert "годен до 19.01.2029" in tab.verdict_label.text()
    assert "Бальзам" in tab.detail_label.text()
    titles = [column.title for column in tab.table.model_.columns]
    assert tab.table.model_.index(0, titles.index("Источник")).data() == "ГИС МТ"


def test_повторный_скан_того_же_товара_не_ходит_в_сеть_второй_раз(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    asked: list[str] = []
    tab = ExpiryTab(AppSettings(_path=tmp_path / "settings.json"),
                    lookup=lambda code: asked.append(code) or _info(ANSWER))
    first = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not first.pending)
    second = tab.accept_scan(PACK_WITH_TAIL, today=TODAY)

    assert not second.pending and second.repeat
    assert second.card.expires == date(2029, 1, 19)
    assert len(asked) == 1
    assert "Уже сканировали" in tab.verdict_label.text()


def test_без_входа_вкладка_говорит_что_делать(application, tmp_path):
    from app.core.marking.transport import AuthRequired
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    def lookup(code):
        raise AuthRequired("Вход в систему маркировки не выполнен. Выберите сертификат "
                           "на вкладке «Маркировка».")

    tab = ExpiryTab(AppSettings(_path=tmp_path / "settings.json"), lookup=lookup)
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)

    assert entry.card.expires is None
    assert "Вход в систему маркировки не выполнен" in entry.card.problems[0]
    assert "Проверка кодов" in entry.card.problems[0]
    assert "Срок не найден" in tab.verdict_label.text()
    assert "Проверка кодов" in tab.hint.text()


def test_ответ_без_срока_виден_а_не_прячется(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    tab = ExpiryTab(AppSettings(_path=tmp_path / "settings.json"),
                    lookup=lambda code: _info({"cisInfo": {"status": "INTRODUCED"}}))
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)

    assert entry.card.expires is None
    assert "нет срока годности" in tab.hint.text()
    assert PACK not in tab._resolved, "ответ без срока не запоминается как решённый"


def test_карточка_со_сроком_не_спрашивает_систему(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    def lookup(code):
        raise AssertionError("срок уже в QR — сеть не нужна")

    tab = ExpiryTab(AppSettings(_path=tmp_path / "settings.json"), lookup=lookup)
    entry = tab.accept_scan(CARD, today=TODAY)
    assert entry is not None and not entry.pending and entry.source == "QR"


# --- парфюмерия: в ответе только дата производства ----------------------------------------

# У парфюма «Честный ЗНАК» отдаёт дату производства, а срока нет: он всегда три года,
# у отдельных брендов пять. Расчёт сверен с настоящей карточкой: бальзам произведён
# 2026-01-20, срок в карточке 2029-01-19 — три года без одних суток.
PERFUME = {
    "cisInfo": {
        "requestedCis": PACK, "gtin": "04640470860092", "status": "INTRODUCED",
        "productName": "Парфюмерная вода", "brand": "POLUBVI",
        "producedDate": "2026-01-20T00:00:00.000Z",
    },
}


def test_срок_считается_от_производства_и_совпадает_с_настоящей_карточкой():
    assert expiry.estimate_expiry(date(2026, 1, 20), 3) == date(2029, 1, 19)
    assert expiry.estimate_expiry(date(2026, 2, 4), 3) == date(2029, 2, 3)


def test_29_февраля_не_роняет_расчёт():
    assert expiry.add_years(date(2024, 2, 29), 3) == date(2027, 2, 28)
    assert expiry.estimate_expiry(date(2024, 2, 29), 5) == date(2029, 2, 27)


def test_бренд_с_другим_сроком_находится_без_оглядки_на_регистр_и_кавычки():
    brands = {"POLUBVI": 5, 'Hi, Dear': 5}
    assert expiry.shelf_years(" polubvi ", 3, brands) == 5
    assert expiry.shelf_years('"HI, dear"', 3, brands) == 5
    assert expiry.shelf_years("Другой", 3, brands) == 3
    assert expiry.shelf_years("", 3, brands) == 3


def test_прочитанный_срок_расчётом_не_перебивается():
    card = parse_card(CARD)
    assert not expiry.apply_estimate(card, 3, {"hi,dear": 5})
    assert card.expires == date(2029, 1, 31) and not card.estimated


def test_без_даты_производства_считать_не_из_чего():
    card = expiry.from_response(PACK, {"cisInfo": {"brand": "POLUBVI"}})
    assert not expiry.apply_estimate(card, 3, {})
    assert card.expires is None and card.problems


def test_расчёт_снимает_сообщение_что_срока_нет():
    card = expiry.from_response(PACK, PERFUME)
    assert card.expires is None and card.problems
    assert expiry.apply_estimate(card, 3, {})
    assert card.estimated and card.shelf_years == 3 and card.problems == []


def test_дата_производства_из_самого_кода_ai11():
    code = f"010460123456789321Abc123XyZ{GS}11260120{GS}91EE10"
    assert parse_card(code).produced == date(2026, 1, 20)


def test_список_брендов_разбирается_и_ругается_на_плохие_строки():
    parsed, errors = expiry.parse_brand_years("POLUBVI = 5\nhi,dear: 5 лет\n\nSome\tBrand\t7")
    assert parsed == {"POLUBVI": 5, "hi,dear": 5, "Some\tBrand": 7}
    assert errors == []
    parsed, errors = expiry.parse_brand_years("без числа\nBrand = 99\nOK = 3")
    assert parsed == {"OK": 3}
    assert len(errors) == 2 and "строка 1" in errors[0]
    assert expiry.format_brand_years({"A": 3, "B": 5}) == "A = 3\nB = 5"


def test_настройки_правил_сохраняются_и_читаются(tmp_path):
    from app.core.settings import AppSettings

    path = tmp_path / "settings.json"
    settings = AppSettings(_path=path)
    settings.marking_shelf_years = 4
    settings.marking_shelf_brands = {"POLUBVI": 5}
    settings.save()
    again = AppSettings.load(str(path))
    assert again.marking_shelf_years == 4 and again.marking_shelf_brands == {"POLUBVI": 5}


def test_настройки_с_мусором_не_ломают_правила(tmp_path):
    import json
    from app.core.settings import AppSettings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"marking_shelf_years": "три",
                                "marking_shelf_brands": {"A": "пять", "B": 5, "C": 0}}),
                    encoding="utf-8")
    settings = AppSettings.load(str(path))
    assert settings.marking_shelf_years == 3
    assert settings.marking_shelf_brands == {"B": 5}


def _perfume_tab(tmp_path, answer=PERFUME, **brands):
    from app.core.settings import AppSettings
    from app.ui.widgets.expiry_tab import ExpiryTab

    settings = AppSettings(_path=tmp_path / "settings.json")
    settings.marking_shelf_brands = dict(brands)
    return ExpiryTab(settings, lookup=lambda code: _info(answer))


def test_парфюм_показывает_расчётный_срок_и_помечает_его(application, tmp_path):
    tab = _perfume_tab(tmp_path)
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)

    assert entry.card.expires == date(2029, 1, 19) and entry.card.estimated
    assert "годен до 19.01.2029" in tab.verdict_label.text()
    assert "расчёт: 3 г. от производства" in tab.verdict_label.text()
    titles = [column.title for column in tab.table.model_.columns]
    row = tab.table.model_
    assert row.index(0, titles.index("Годен до")).data() == "≈ 19.01.2029"
    assert row.index(0, titles.index("Источник")).data() == "ГИС МТ, расчёт"
    assert "срок расчётный: 3 г." in row.index(0, titles.index("Замечание")).data()


def test_бренд_на_пять_лет_считается_на_пять(application, tmp_path):
    tab = _perfume_tab(tmp_path, polubvi=5)
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)

    assert entry.card.expires == date(2031, 1, 19) and entry.card.shelf_years == 5


def test_правило_меняется_и_уже_отсканированное_пересчитывается(application, tmp_path, monkeypatch):
    from app.ui.widgets import expiry_tab as module

    tab = _perfume_tab(tmp_path)
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)
    assert entry.card.expires == date(2029, 1, 19)

    class Dialog:
        """Подставной диалог: человек выставил «5 лет» для POLUBVI."""
        class DialogCode:
            Accepted = module.QDialog.DialogCode.Accepted

        def __init__(self, years, brands, parent=None):
            self.years = type("Spin", (), {"value": staticmethod(lambda: 3)})()
            self.result_brands = {"POLUBVI": 5}

        def exec(self):
            return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module, "ShelfLifeDialog", Dialog)
    assert tab.edit_rules()

    assert entry.card.expires == date(2031, 1, 19)
    assert tab.settings.marking_shelf_brands == {"POLUBVI": 5}
    assert "годен до 19.01.2031" in tab.verdict_label.text()


def test_повторный_парфюм_считается_по_действующим_правилам(application, tmp_path):
    tab = _perfume_tab(tmp_path)
    first = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not first.pending)
    tab.settings.marking_shelf_brands = {"POLUBVI": 5}
    second = tab.accept_scan(PACK, today=TODAY)

    assert not second.pending and second.card.expires == date(2031, 1, 19)
    assert first.card.expires == date(2029, 1, 19), "прежняя запись остаётся как была"


def test_настоящий_срок_из_ответа_не_перебивается_расчётом(application, tmp_path):
    tab = _perfume_tab(tmp_path, answer=ANSWER, polubvi=5)
    entry = tab.accept_scan(PACK, today=TODAY)
    _wait_until(lambda: not entry.pending)

    assert entry.card.expires == date(2029, 1, 19) and not entry.card.estimated
    assert "расчёт" not in tab.verdict_label.text()
