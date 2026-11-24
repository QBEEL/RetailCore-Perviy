"""Карточки Национального каталога. Сеть и сертификат подменены."""
import json
from dataclasses import replace
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest
from PySide6.QtWidgets import (
    QApplication, QDialog, QInputDialog, QMessageBox, QPlainTextEdit, QStyle,
    QStyleOptionComboBox, QWidget,
)

from app.core.marking import catalog, service, session, transport
from app.core.marking.models import Contour
from app.ui.theme import STYLESHEET
from app.ui.widgets import marking_catalog

GTIN = "4600000000008"
COMPONENT = "4600000000015"
DRAFT = catalog.CardDraft(catalog.CardKind.UNIT, "Тестовый товар", "Марка", "3303",
                          gtin=GTIN, category="30120")


@pytest.fixture
def catalog_account(monkeypatch):
    current = session.Session(contour=Contour.SANDBOX, inn="1234567890",
                              thumbprint="test", token="test-token")
    monkeypatch.setattr(session, "session", current)
    monkeypatch.setattr(session, "token", lambda: current.token)
    return catalog.CatalogAccount.current(Contour.SANDBOX)


def test_unit_uses_trade_unit_and_default_draft():
    assert DRAFT.payload() == {
        "good_name": "Тестовый товар", "brand": "Марка", "tnved": "3303",
        "moderation": 0, "gtin": GTIN, "categories": [{"cat_id": 30120}],
        "identified_by": [
            {"type": "gtin", "value": GTIN, "level": "trade-unit", "multiplier": 1},
        ],
    }


def test_technical_card_lets_catalog_assign_gtin():
    body = replace(DRAFT, kind=catalog.CardKind.TECHNICAL, moderation=True).payload()
    assert type(body["is_tech_gtin"]) is int and body["is_tech_gtin"] == 1
    assert body["moderation"] == 1
    assert "gtin" not in body and "identified_by" not in body


def test_set_includes_group_components_and_multiple_values():
    draft = replace(DRAFT, kind=catalog.CardKind.SET, set_group="4",
                    components=((COMPONENT, "2"),),
                    attributes=(("2630", "RU", ""), ("2630", "BY", ""),
                                ("2501", "100", "мл")))
    body = draft.payload()
    assert body["is_set"] is True
    assert body["set_gtins"] == [{"gtin": COMPONENT, "quantity": 2}]
    assert body["good_attrs"] == [
        {"attr_id": "2630", "attr_value": "RU"},
        {"attr_id": "2630", "attr_value": "BY"},
        {"attr_id": "2501", "attr_value": "100", "attr_value_type": "мл"},
        {"attr_id": "23768", "attr_value": "4"},
    ]
    with pytest.raises(ValueError, match="повторяется"):
        replace(draft, components=((COMPONENT, "2"), ("0" + COMPONENT, "3"))).payload()
    assert "set_gtins" not in replace(draft, set_group="1", components=()).payload()


@pytest.mark.parametrize("changes", [
    {"gtin": "4600000000000"},
    {"tnved": "330"},
    {"category": "-1"},
    {"brand": " "},
    {"attributes": (("id", "value", ""),)},
    {"kind": catalog.CardKind.SET, "set_group": "4"},
    {"kind": catalog.CardKind.SET, "set_group": "4", "components": ((COMPONENT, "0"),)},
])
def test_invalid_card_fields(changes):
    with pytest.raises(ValueError):
        replace(DRAFT, **changes).payload()


def test_create_and_check_use_the_same_account_and_do_not_retry_creation(
        monkeypatch, catalog_account):
    calls = []

    def request(method, system, path, **kwargs):
        calls.append((method, system, path, kwargs))
        if path == "/feed":
            return {"result": {"feed_id": "123"}}
        return {"result": {"feed_id": 123, "status": "Processing"}}

    monkeypatch.setattr(transport, "request", request)
    feed_id = catalog_account.create(DRAFT)
    assert catalog_account.feed_status(feed_id)["status"] == "Processing"
    assert calls == [
        ("POST", "catalog", "/feed", {"contour": Contour.SANDBOX, "token": "test-token",
                                     "body": DRAFT.payload(), "retries": 0,
                                     "headers": {"Content-Type": "application/json; charset=utf-8"}}),
        ("GET", "trueapi", "/nk/feed-status", {"contour": Contour.SANDBOX, "token": "test-token",
                                             "params": {"feed_id": "123", "format": "json"}}),
    ]
    assert transport.url("catalog", "/feed", Contour.SANDBOX) == (
        "https://api.nk.sandbox.crptech.ru/v3/feed")
    assert transport.url("catalog", "/feed", Contour.PRODUCTION) == (
        "https://" + "апи.национальный-каталог.рф".encode("idna").decode() + "/v3/feed")


@pytest.mark.parametrize("full_status, group_status, uses_group", [
    (2, 1, True), (1, 1, False), (0, 1, False), (2, 3, False),
])
def test_technical_card_uses_true_api_feed_and_checks_tnved(monkeypatch, catalog_account,
                                                          full_status, group_status, uses_group):
    requests = []

    def send(prepared, tolerate):
        body = json.loads(prepared.data)
        requests.append((prepared, body))
        if prepared.full_url.endswith("/mark-check"):
            return {"result": {"tnveds": [
                {"tnved": "3303", "is_marked_code": group_status},
                {"tnved": "3303001000", "is_marked_code": full_status},
            ]}}
        return {"result": {"feed_id": 123}}

    monkeypatch.setattr(transport, "_send", send)
    draft = replace(DRAFT, kind=catalog.CardKind.TECHNICAL, tnved="3303001000",
                    attributes=(("2630", "RU", ""),))
    assert catalog_account.create(draft) == "123"
    assert len(requests) == 2
    assert requests[0][0].full_url.endswith("/v3/mark-check")
    assert requests[0][1] == {"tnveds": ["3303001000", "3303"]}
    prepared, entries = requests[1]
    assert prepared.full_url == (
        "https://markirovka.sandbox.crptech.ru/api/v3/true-api/nk/feed?format=json"
    )
    assert prepared.get_header("Authorization") == "Bearer test-token"
    assert prepared.get_header("Content-type") == "application/json; charset=utf-8"
    assert b'"is_tech_gtin": 1' in prepared.data and b'"attr_id": 2630' in prepared.data
    assert isinstance(entries, list) and len(entries) == 1
    body = entries[0]
    assert body["categories"] == [30120]
    assert body["tnved"] == ("3303" if uses_group else "3303001000")
    assert body["good_attrs"] == [{"attr_id": 2630, "attr_value": "RU"}] + (
        [{"attr_id": 13933, "attr_value": "3303001000"}] if uses_group else []
    )
    assert not {"gtin", "moderation", "identified_by"} & body.keys()


def test_technical_card_empty_refusal_keeps_request_id_and_does_not_resubmit(
        monkeypatch, catalog_account):
    calls = []

    def refuse(prepared, **kwargs):
        calls.append(prepared.full_url)
        headers = Message()
        headers["x-request-log-id"] = "example/123"
        raise HTTPError(prepared.full_url, 400, "Bad Request", headers, BytesIO())

    monkeypatch.setattr(transport.urllib.request, "urlopen", refuse)
    draft = replace(DRAFT, kind=catalog.CardKind.TECHNICAL)
    with pytest.raises(transport.MarkingError, match="ID обращения: example/123") as failure:
        catalog_account.create(draft)
    assert "через True API, HTTP 400" in str(failure.value)
    assert calls == ["https://markirovka.sandbox.crptech.ru/api/v3/true-api/nk/feed?format=json"]


def test_timeout_leaves_submission_unknown_without_repeat(monkeypatch, catalog_account):
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        raise transport.Offline("Время ожидания истекло")

    monkeypatch.setattr(transport, "request", request)
    with pytest.raises(catalog.SubmissionUnknown):
        catalog_account.create(DRAFT)
    assert calls == [("POST", "catalog", "/feed")]


def test_create_reports_http_status_and_catalog_detail(monkeypatch, catalog_account):
    def request(*args, **kwargs):
        raise transport._failure(400, b'{"result":{"errors":[{"text":"Invalid category"}]}}')

    monkeypatch.setattr(transport, "request", request)
    with pytest.raises(transport.MarkingError, match=r"HTTP 400.*\nInvalid category") as failure:
        catalog_account.create(DRAFT)
    assert failure.value.status == 400


def test_mismatched_feed_and_changed_account_are_rejected(monkeypatch, catalog_account):
    monkeypatch.setattr(transport, "request",
                        lambda *args, **kw: {"result": {"feed_id": 321, "status": "Signed"}})
    with pytest.raises(transport.MarkingError, match="другой отправки"):
        catalog_account.feed_status("123")
    monkeypatch.setattr(session.session, "inn", "0987654321")
    with pytest.raises(transport.MarkingError, match="сеанс входа изменились"):
        catalog_account.create(DRAFT)


def test_generate_gtin_reserves_one_code_without_retry(monkeypatch, catalog_account):
    calls = []

    def request(method, system, path, **kwargs):
        calls.append((method, system, path, kwargs))
        return {"result": {"drafts": [{"gtin": GTIN}]}}

    monkeypatch.setattr(transport, "request", request)
    assert catalog_account.generate_gtin() == GTIN
    assert calls == [("GET", "catalog", "/generate-gtins", {
        "contour": Contour.SANDBOX, "token": "test-token", "retries": 0,
        "params": {"quantity": 1},
    })]


def test_search_tnved_filters_group_and_reads_all_pages(monkeypatch, catalog_account):
    requests = []

    def request(method, system, path, **kwargs):
        assert (method, system, path) == ("POST", "trueapi4", "/tn-ved/search")
        body = kwargs["body"].copy()
        requests.append(body)
        if body["page"] == 0:
            return {"total": 1001, "last": False, "tnveds": [
                {"tnved": "3303001000", "description": "Духи", "pg": "perfumery"},
                {"tnved": "3303001000", "description": "Другая группа", "pg": "chemistry"},
            ]}
        return {"total": 1001, "last": True, "tnveds": [
            {"tnved": "3303009000", "description": "Туалетная вода", "pg": "perfumery"},
            {"tnved": "3401001000", "description": "Другой префикс", "pg": "perfumery"},
        ]}

    monkeypatch.setattr(transport, "request", request)
    assert catalog_account.search_tnved("3303", "perfumery") == [
        ("3303001000", "Духи"), ("3303009000", "Туалетная вода"),
    ]
    assert requests == [
        {"page": page, "limit": 1000, "tnveds": ["3303"], "pg": ["perfumery"]}
        for page in (0, 1)
    ]


def test_tab_creates_technical_card_and_checks_status(monkeypatch, catalog_account):
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    tab = marking_catalog.MarkingCatalogTab(lambda: Contour.SANDBOX, parent)
    tab.kind.setCurrentIndex(tab.kind.findData(catalog.CardKind.TECHNICAL))
    assert tab.gtin_row.isHidden() and tab.components.isHidden()
    assert not tab.technical_gtin.isHidden()
    tab.name.setText(DRAFT.name)
    tab.brand.setText(DRAFT.brand)
    tab.tnved.setText(DRAFT.tnved)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    scheduled = []

    def run(function, *args, on_result, on_error):
        scheduled.append((function, args, on_result, on_error))

    monkeypatch.setattr(marking_catalog, "run_task", run)
    tab.create_button.click()
    function, args, on_result, _ = scheduled.pop()
    assert function.__self__ == catalog_account
    assert function.__func__ is catalog.CatalogAccount.create
    assert args[0].kind is catalog.CardKind.TECHNICAL
    assert not tab.create_button.isEnabled()
    on_result("123")
    assert tab.create_button.isEnabled() and tab.feed_id.text() == "123"
    tab.status_button.click()
    function, args, on_result, _ = scheduled.pop()
    assert function.__self__ == catalog_account
    assert function.__func__ is catalog.CatalogAccount.feed_status and args == ("123",)
    on_result({"status": "Rejected", "result": {"0": ["Уточните ТН ВЭД"], "totalErrors": "1"},
               "error_details": {"items": [
                   {"attribute_name": "Вид товара", "message": "Заполните значение"},
               ]}})
    assert "Отклонён" in tab.result.toPlainText()
    assert "Уточните ТН ВЭД" in tab.result.toPlainText()
    assert "Вид товара · Заполните значение" in tab.result.toPlainText()
    application.processEvents()


def test_tab_fills_generated_code_and_selects_tnved_and_set_group(monkeypatch, catalog_account):
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    tab = marking_catalog.MarkingCatalogTab(lambda: Contour.SANDBOX, parent)
    scheduled = []

    def run(function, *args, on_result, on_error):
        scheduled.append((function, args, on_result))

    monkeypatch.setattr(marking_catalog, "run_task", run)
    tab._generate_gtin()
    function, args, on_result = scheduled.pop()
    assert function.__func__ is catalog.CatalogAccount.generate_gtin and not args
    on_result(GTIN)
    assert tab.gtin.text() == GTIN

    tab.kind.setCurrentIndex(tab.kind.findData(catalog.CardKind.SET))
    tab.set_group.setCurrentIndex(tab.set_group.findData("4"))
    monkeypatch.setattr(marking_catalog.TnvedDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    tab._find_tnved()
    function, args, on_result = scheduled.pop()
    assert function.__func__ is catalog.CatalogAccount.search_tnved
    assert args == ("", "perfumery")
    on_result([("3303001000", "Духи")])
    assert tab.tnved.text() == "3303001000"
    assert tab._draft().set_group == "4"
    application.processEvents()


def test_tnved_dialog_fits_screen_with_long_description():
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    description = "Длинное описание товарной позиции " * 300
    dialog = marking_catalog.TnvedDialog([("3401300000", description), ("3402900000", "Другое")],
                                         parent)
    dialog.show()
    application.processEvents()
    assert parent.screen().availableGeometry().contains(dialog.frameGeometry())
    assert dialog.description.toPlainText() == description
    assert dialog.selected_code() == "3401300000"
    dialog.codes.setCurrentRow(1)
    assert dialog.description.toPlainText() == "Другое"
    assert dialog.selected_code() == "3402900000"
    dialog.close()


def test_technical_card_from_code_prefills_only_applicable_data():
    info = service._info({"cisInfo": {
        "productName": "Тестовая пена для умывания", "brand": "Тестовая марка",
        "gtin": GTIN, "country": "RU", "tnVedEaes": "3401300000", "tnVedEaesGroup": "3401",
        "packageType": "UNIT", "status": "INTRODUCED",
        "certDoc": [
            {"type": "CONFORMITY_DECLARATION", "number": "Тест № 1", "date": "2026-04-03",
             "statusGroup": 1},
            {"type": "CONFORMITY_CERTIFICATE", "number": "Тест № 2", "date": "2026-04-03",
             "statusGroup": 11},
        ],
    }})
    draft = catalog.technical_card_from_code(info)
    assert draft.source_gtin == GTIN
    body = draft.payload()
    assert body == {
        "is_tech_gtin": 1, "good_name": "Тестовая пена для умывания", "brand": "Тестовая марка",
        "tnved": "3401300000",
        "good_attrs": [
            {"attr_id": 2630, "attr_value": "RU"},
            {"attr_id": 23557, "attr_value": "Тест № 1:::2026-04-03"},
        ],
    }
    info.raw["cisInfo"]["tnVedEaes"] = ""
    assert catalog.technical_card_from_code(info).tnved == "3401"


def test_missing_attribute_ids_are_unique_and_ignore_unrelated_numbers():
    assert catalog.missing_attribute_ids([
        "отсутствует обязательный параметр 15448",
        "Отсутствует обязательный атрибут №12",
        "отсутствует обязательный параметр 15448",
        "Отправка № 123",
    ]) == ("15448", "12")


def test_missing_attribute_fields_suggest_only_matching_product_and_packaging(monkeypatch,
                                                                             catalog_account):
    def request(method, system, path, **kwargs):
        if path == "/attributes":
            assert kwargs["params"] == {"cat_id": "30120"}
            return {"result": [{"attr_id": 15448, "attr_name": "Объём"}]}
        assert path == "/product" and kwargs["params"] == {"gtin": GTIN}
        return {"result": [
            {"identified_by": [{"type": "gtin", "value": COMPONENT}],
             "good_attrs": [{"attr_id": 15448, "attr_value": "200"}]},
            {"identified_by": [{"type": "gtin", "value": "0" + GTIN}],
             "good_attrs": [
                 {"attr_id": 15448, "attr_value": "150", "attr_value_type": "мл"},
                 {"attr_id": 15448, "attr_value": "300", "gtin": COMPONENT},
                 {"attr_id": 12, "attr_value": "Гель"},
                 {"attr_id": 12, "attr_value": "Крем"},
             ]},
        ]}

    monkeypatch.setattr(transport, "request", request)
    draft = replace(DRAFT, kind=catalog.CardKind.TECHNICAL, source_gtin=GTIN)
    fields, suggestions = catalog_account.missing_attribute_fields(draft, ("15448", "12"))
    assert fields == [{"attr_id": 15448, "attr_name": "Объём"}, {"attr_id": "12"}]
    assert suggestions == {"15448": ("150", "мл")}


def test_fill_missing_status_attributes_adds_values_to_draft_without_sending(
        monkeypatch, catalog_account):
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    tab = marking_catalog.MarkingCatalogTab(lambda: Contour.SANDBOX, parent)
    draft = replace(DRAFT, kind=catalog.CardKind.TECHNICAL, source_gtin=GTIN,
                    attributes=(("2630", "RU", ""),))
    assert tab.load_technical_card(draft)
    tab.feed_id.setText("123")
    tab._show_status(catalog_account, "123", {"status": "Rejected", "result": {
        "0": ["отсутствует обязательный параметр 15448", "отсутствует обязательный параметр 12"],
    }})
    assert not tab.fill_missing_button.isHidden()

    def run(function, form, ids, *, on_result, on_error):
        assert function.__func__ is catalog.CatalogAccount.missing_attribute_fields
        assert form.source_gtin == GTIN
        assert ids == ("15448", "12")
        on_result(([
            {"attr_id": 15448, "attr_name": "Объём", "attr_value_type": ["мл", "л"]},
            {"attr_id": 12, "attr_name": "Вид товара", "attr_preset": ["ГЕЛЬ", "КРЕМ"],
             "attr_preset_only": True},
        ], {"15448": ("150", "мл"), "12": ("ГЕЛЬ", "")}))

    monkeypatch.setattr(marking_catalog, "run_task", run)
    monkeypatch.setattr(marking_catalog.MissingAttributesDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    tab.fill_missing_button.click()
    assert tab._draft().attributes == (("2630", "RU", ""), ("15448", "150", "мл"), ("12", "ГЕЛЬ", ""))
    tab.feed_id.setText("456")
    assert tab.fill_missing_button.isHidden()
    application.processEvents()


def test_missing_attribute_unit_is_readable_and_placeholder_is_not_submitted():
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    dialog = marking_catalog.MissingAttributesDialog(
        [{"attr_id": 23820, "attr_name": "Номинальное количество",
          "attr_value_type": ["мл", "л", "см³"]}],
        {"23820": ("50", "МЛ")}, parent,
    )
    dialog.setStyleSheet(STYLESHEET)
    dialog.show()
    application.processEvents()
    _, units = dialog._inputs["23820"]
    assert units.currentText() == "мл"
    option = QStyleOptionComboBox()
    units.initStyleOption(option)
    text_area = units.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, units,
    )
    assert text_area.width() >= units.fontMetrics().horizontalAdvance("Ед. изм.")
    units.setCurrentText("л")
    assert dialog.attributes() == [("23820", "50", "л")]
    units.setCurrentIndex(0)
    assert units.currentText() == "Ед. изм."
    assert dialog.attributes() == [("23820", "50", "")]
    dialog.close()


@pytest.mark.parametrize("group_fields", [
    {"productGroup": "chemistry"}, {"productGroup": "cosmetics"}, {"productGroupId": 35},
])
def test_technical_card_for_cosmetics_can_be_prefilled(group_fields):
    info = service._info({"cisInfo": {
        "packageType": "UNIT", "status": "INTRODUCED", "productName": "Тестовый крем",
        "brand": "Тестовая марка", "tnVedEaes": "3304990000", **group_fields,
    }})
    draft = catalog.technical_card_from_code(info)
    assert draft.payload()["is_tech_gtin"] == 1
    assert draft.tnved == "3304990000"


def test_technical_card_for_cosmetics_category_reaches_catalog(monkeypatch, catalog_account):
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    tab = marking_catalog.MarkingCatalogTab(lambda: Contour.SANDBOX, parent)
    tab.kind.setCurrentIndex(tab.kind.findData(catalog.CardKind.TECHNICAL))
    tab.name.setText(DRAFT.name)
    tab.brand.setText(DRAFT.brand)
    tab.tnved.setText("3304990000")
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("Тест · 123", True))
    tab._choose_category([{"cat_id": 123, "cat_name": "Тест", "category_active": True,
                           "gismt_codes": [35]}])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    sent = []

    def run(function, draft, *, on_result, on_error):
        assert function.__self__ == catalog_account
        assert function.__func__ is catalog.CatalogAccount.create
        sent.append(draft.payload())
        on_result("123")

    monkeypatch.setattr(marking_catalog, "run_task", run)
    tab.create_button.click()
    assert len(sent) == 1
    assert sent[0]["is_tech_gtin"] == 1 and "moderation" not in sent[0]
    assert sent[0]["categories"] == [{"cat_id": 123}]
    assert "GTIN" not in sent[0] and "gtin" not in sent[0]
    assert tab.feed_id.text() == "123"
    application.processEvents()


def test_attribute_reference_prioritizes_required_registration_fields(monkeypatch):
    application = QApplication.instance() or QApplication([])
    parent = QWidget()
    tab = marking_catalog.MarkingCatalogTab(lambda: Contour.SANDBOX, parent)
    lines = []

    def read_dialog(dialog):
        lines.extend(dialog.findChild(QPlainTextEdit).toPlainText().splitlines())

    monkeypatch.setattr(QDialog, "exec", read_dialog)
    tab._attribute_reference([
        {"attr_id": 1, "attr_name": "Описание", "attr_type": "o"},
        {"attr_id": 2, "attr_name": "Масса", "attr_type": "m", "first_layer": False},
        {"attr_id": 3, "attr_name": "Вид товара", "attr_type": "m", "first_layer": True,
         "attr_preset": ["Крем", "Гель"]},
    ])
    assert lines == [
        "3 · Вид товара · обязательный · для заказа кодов маркировки",
        "  Значения: Крем, Гель",
        "2 · Масса · обязательный",
        "1 · Описание",
    ]
    application.processEvents()
