"""Торг-2: модель заявки, обращения к серверу, страница, окна и уведомления."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import appdata  # noqa: E402
from app.core.claims import (  # noqa: E402
    Claim,
    ClaimFile,
    Inbox,
    InboxItem,
    Manager,
    expiry_label,
    expiry_level,
    remote,
)
from app.core.payments import transport  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui import claims_page, pages  # noqa: E402
from app.ui.claims_page import ClaimsPage  # noqa: E402
from app.ui.claims_watch import ClaimsWatcher, describe  # noqa: E402
from app.ui.widgets.claim_dialogs import CloseDialog, ClaimDialog  # noqa: E402

TODAY = date(2026, 10, 20)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def clean_session():
    transport.session.clear()
    yield
    transport.session.clear()


def sign_in(user_id: int = 7, *, admin: bool = False) -> None:
    transport.session.base_url = "http://server"
    transport.session.token = "t"
    transport.session.user_id = user_id
    transport.session.is_admin = admin
    transport.session.expires_at = datetime.now() + timedelta(hours=8)


def row(**over) -> dict:
    data = {
        "id": 1, "kind": "shortage", "brand": "FLORAIKU", "product": "Парфюмерная вода Spring River",
        "spec": "50 мл + 10 мл", "barcode": "3701123006104", "quantity": "1.00",
        "expiry_date": None, "comment": "", "status": "new", "applicant_id": 3,
        "applicant": "Склад Первый", "manager_id": 7, "manager": "Анна Петрова",
        "manager_seen": False, "resolution": "", "close_note": "",
        "created_at": "2026-10-19T09:30:00", "closed_at": None, "closed_by": "", "files": 0,
        "can_edit": True, "can_accept": True, "can_close": True,
        "can_reopen": False, "can_delete": False,
    }
    data.update(over)
    return data


# --- модель --------------------------------------------------------------------

class TestExpiry:
    def test_нет_даты(self) -> None:
        assert expiry_label(None, TODAY) == "Нет даты"
        assert expiry_level(None, TODAY) == "none"

    def test_истёк(self) -> None:
        assert expiry_label(TODAY - timedelta(days=1), TODAY) == "Истёк"
        assert expiry_level(TODAY - timedelta(days=1), TODAY) == "expired"

    def test_сегодня(self) -> None:
        assert expiry_label(TODAY, TODAY) == "Истекает сегодня"
        assert expiry_level(TODAY, TODAY) == "soon"

    def test_скоро_считается_днями(self) -> None:
        assert expiry_label(TODAY + timedelta(days=45), TODAY) == "45 дн."
        assert expiry_level(TODAY + timedelta(days=60), TODAY) == "soon"

    def test_далеко_считается_месяцами(self) -> None:
        assert expiry_label(TODAY + timedelta(days=120), TODAY) == "4 мес."
        assert expiry_level(TODAY + timedelta(days=61), TODAY) == "ok"


class TestClaim:
    def test_количество_без_лишнего_нуля(self) -> None:
        assert Claim(quantity=1.0).quantity_text() == "1"
        assert Claim(quantity=11.0).quantity_text() == "11"
        assert Claim(quantity=2.5).quantity_text() == "2,5"

    def test_название_для_текста(self) -> None:
        assert Claim(brand="Rink", product="Тональный крем").title == "Rink · Тональный крем"
        assert Claim(barcode="4673754974031").title == "4673754974031"
        assert Claim(id=5).title == "заявка 5"

    def test_подписи_статусов_и_видов(self) -> None:
        assert Claim(status="accepted").status_title == "Принято в работу"
        assert Claim(status="closed").status_title == "Закрыто"
        assert Claim(kind="defect").kind_title == "Брак"
        assert Claim(resolution="refund").resolution_title == "Возместили деньгами"


# --- сервер --------------------------------------------------------------------

class FakeServer:
    """Подменяет транспорт: запоминает запросы и отдаёт заготовленные ответы."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple] = []
        self.answers: dict[str, object] = {}
        for name in ("get", "post", "patch", "delete"):
            monkeypatch.setattr(transport, name, self._make(name))

    def _make(self, verb: str):
        def call(path, *args):
            self.calls.append((verb, path, *args))
            answer = self.answers.get(f"{verb} {path}")
            if isinstance(answer, Exception):
                raise answer
            return answer
        return call


@pytest.fixture
def server(monkeypatch):
    return FakeServer(monkeypatch)


class TestRemote:
    def test_список_разбирается_в_заявки(self, server) -> None:
        server.answers["get /api/claims"] = [row(expiry_date="2027-01-31")]

        claims = remote.list_claims(archive=False, mine=True, text="ролл")

        assert server.calls == [("get", "/api/claims",
                                 {"archive": False, "mine": True, "text": "ролл"})]
        claim = claims[0]
        assert claim.quantity == 1.0 and claim.expiry_date == date(2027, 1, 31)
        assert claim.manager == "Анна Петрова" and claim.can_accept

    def test_правка_отправляет_только_переданное(self, server) -> None:
        server.answers["patch /api/claims/4"] = row(id=4)

        remote.update_claim(4, {"manager_id": None})

        assert server.calls == [("patch", "/api/claims/4", {"manager_id": None})]

    def test_закрытие_передаёт_итог_и_примечание(self, server) -> None:
        server.answers["post /api/claims/4/close"] = row(id=4, status="closed")

        remote.close_claim(4, "refund", "кредит-нота 12")

        assert server.calls == [("post", "/api/claims/4/close",
                                 {"resolution": "refund", "note": "кредит-нота 12"})]

    def test_входящие_разбираются(self, server) -> None:
        server.answers["get /api/claims/inbox"] = {
            "unseen": 2, "open": 5,
            "items": [{"id": 9, "kind": "defect", "brand": "Rink",
                       "product": "Крем", "applicant": "Склад"}]}

        inbox = remote.inbox()

        assert (inbox.unseen, inbox.open) == (2, 5)
        assert inbox.items[0].id == 9 and inbox.items[0].kind == "defect"

    def test_сервер_без_раздела_объясняет_что_делать(self, server) -> None:
        server.answers["get /api/claims"] = transport.ServerError("Not Found", 404)

        with pytest.raises(transport.ServerError, match="013_claims.sql"):
            remote.list_claims()

    def test_другая_ошибка_не_маскируется(self, server) -> None:
        server.answers["get /api/claims"] = transport.ServerError("Недостаточно прав", 403)

        with pytest.raises(transport.ServerError, match="Недостаточно прав"):
            remote.list_claims()

    def test_вложение_кладётся_в_папку_своей_заявки(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(appdata, "path_to", lambda name: str(tmp_path / name))
        loaded: list[str] = []
        monkeypatch.setattr(transport, "download",
                            lambda path, target: loaded.append((path, target)) or target)

        path = remote.local_copy(ClaimFile(id=5, claim_id=2, name="..\\..\\акт.pdf", size=10))

        assert Path(path).parent == tmp_path / "claim_files" / "2" / "5"
        assert Path(path).name == "акт.pdf"
        assert loaded[0][0] == "/api/claims/files/5"


# --- уведомления ---------------------------------------------------------------

class TestWatcher:
    def make(self, app) -> tuple[ClaimsWatcher, list, list]:
        watcher = ClaimsWatcher()
        counts: list[int] = []
        arrived: list[tuple] = []
        watcher.unseen_changed.connect(counts.append)
        watcher.arrived.connect(lambda items, unseen: arrived.append((items, unseen)))
        return watcher, counts, arrived

    def item(self, number: int) -> InboxItem:
        return InboxItem(id=number, kind="shortage", brand="Rink", product="Крем",
                         applicant="Склад")

    def test_новая_заявка_сообщается_один_раз(self, app) -> None:
        watcher, counts, arrived = self.make(app)

        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))
        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))

        assert len(arrived) == 1 and arrived[0][1] == 1
        assert counts == [1]

    def test_следующая_заявка_сообщается_отдельно(self, app) -> None:
        watcher, _counts, arrived = self.make(app)

        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))
        watcher._got(Inbox(unseen=2, open=2, items=[self.item(2), self.item(1)]))

        assert [i.id for i in arrived[1][0]] == [2]
        assert arrived[1][1] == 2

    def test_после_прочтения_цифра_падает_без_уведомления(self, app) -> None:
        watcher, counts, arrived = self.make(app)
        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))

        watcher._got(Inbox(unseen=0, open=1, items=[]))

        assert counts == [1, 0] and len(arrived) == 1

    def test_та_же_заявка_после_возврата_сообщается_снова(self, app) -> None:
        # Открыл, потом администратор вернул из архива — база снова ставит
        # «не видел», и об этом нужно сказать ещё раз.
        watcher, _counts, arrived = self.make(app)
        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))
        watcher._got(Inbox(unseen=0, open=1, items=[]))

        watcher._got(Inbox(unseen=1, open=1, items=[self.item(1)]))

        assert len(arrived) == 2

    def test_без_входа_и_с_истёкшим_токеном_сервер_не_спрашивается(
            self, app, monkeypatch) -> None:
        asked: list[bool] = []
        monkeypatch.setattr("app.ui.claims_watch.run_task",
                            lambda *a, **k: asked.append(True))
        watcher, _c, _a = self.make(app)

        watcher.poll()
        assert asked == []

        sign_in()
        transport.session.expires_at = datetime.now() - timedelta(minutes=5)
        watcher.poll()
        assert asked == []

        transport.session.expires_at = datetime.now() + timedelta(hours=8)
        watcher.poll()
        assert asked == [True]

    def test_ошибка_связи_не_блокирует_следующий_опрос(self, app) -> None:
        watcher, _c, _a = self.make(app)
        watcher._busy = True

        watcher._failed("нет сети")

        assert watcher._busy is False

    def test_текст_одной_заявки_подробный(self) -> None:
        text = describe([InboxItem(1, "defect", "Rink", "Крем", "Мария")], 1)
        assert "брак" in text and "Rink · Крем" in text and "Мария" in text

    def test_текст_нескольких_заявок_числом(self) -> None:
        items = [InboxItem(1, "defect", "A", "x", "н"), InboxItem(2, "shortage", "B", "y", "н")]
        assert "заявок: 3" in describe(items, 3)


# --- страница ------------------------------------------------------------------

class Notices(list):
    def __call__(self, text, kind) -> None:
        self.append((text, kind))


def make_page(app) -> tuple[ClaimsPage, Notices]:
    notices = Notices()
    return ClaimsPage(AppSettings(), notices), notices


def claims(*rows: dict) -> list[Claim]:
    return [remote._claim(r) for r in rows]


class TestPage:
    def test_раздел_зарегистрирован_и_закрывается_админом(self) -> None:
        assert "claims" in {page.code for page in pages.MANAGED}
        assert pages.PAGES[pages.index_of("claims")].title == "Торг-2"

    def test_таблица_повторяет_колонки_таблицы_склада(self, app) -> None:
        page, _ = make_page(app)
        titles = [c.title for c in page.table.model_.columns]
        assert titles[:3] == ["Дата", "Вид", "Бренд"]
        for wanted in ("Срок годности", "До окончания", "Заявитель", "КМ", "Ответ КМ", "ТОРГ"):
            assert wanted in titles
        assert "Итог" not in titles

    def test_в_архиве_появляются_итог_и_кто_закрыл(self, app) -> None:
        page, _ = make_page(app)
        page.tabs.setCurrentIndex(claims_page.TAB_ARCHIVE)
        page._apply((claims_page.TAB_ARCHIVE, claims(row(
            status="closed", resolution="refund", closed_by="Анна",
            closed_at="2026-10-20T10:00:00")), [], []))

        titles = [c.title for c in page.table.model_.columns]
        assert "Итог" in titles and "Закрыл" in titles
        assert page.table.proxy.rowCount() == 1

    def test_ответ_с_чужой_вкладки_не_рисуется(self, app, monkeypatch) -> None:
        page, _ = make_page(app)
        reloaded: list[bool] = []
        monkeypatch.setattr(page, "restore", lambda: reloaded.append(True))

        page._apply((claims_page.TAB_MINE, claims(row()), [], []))

        assert page._items == [] and reloaded == [True]

    def test_кнопки_следуют_правам_заявки(self, app) -> None:
        page, _ = make_page(app)
        page._apply((claims_page.TAB_OPEN, claims(
            row(id=1, can_accept=True, can_delete=False),
            row(id=2, can_accept=False, can_delete=True)), [], []))

        page.table.selectRow(0)
        assert page.accept_button.isEnabled() and not page.delete_button.isEnabled()
        page.table.selectRow(1)
        assert not page.accept_button.isEnabled() and page.delete_button.isEnabled()

    def test_вернуть_из_архива_видно_только_админу_и_только_в_архиве(self, app) -> None:
        page, _ = make_page(app)
        sign_in(admin=False)
        page.tabs.setCurrentIndex(claims_page.TAB_ARCHIVE)
        page._update_actions()
        assert page.reopen_button.isHidden()

        sign_in(admin=True)
        page._update_actions()
        assert not page.reopen_button.isHidden()

        page.tabs.setCurrentIndex(claims_page.TAB_OPEN)
        page._update_actions()
        assert page.reopen_button.isHidden()

    def test_новое_для_меня_подсвечивается(self, app) -> None:
        sign_in(user_id=7)
        mine, other, seen = claims(row(manager_id=7), row(manager_id=8),
                                   row(manager_id=7, manager_seen=True))

        assert ClaimsPage._tint(mine) is not None
        assert ClaimsPage._tint(other) is None
        assert ClaimsPage._tint(seen) is None

    def test_открытие_снимает_новое_и_сообщает_окну(self, app, monkeypatch) -> None:
        sign_in(user_id=7)
        page, _ = make_page(app)
        marked: list[int] = []
        monkeypatch.setattr(claims_page.remote, "mark_seen", lambda cid: marked.append(cid))
        monkeypatch.setattr(claims_page, "run_task",
                            lambda fn, *a, on_result=None, **k: (fn(*a), on_result and on_result(None)))
        signals: list[bool] = []
        page.inbox_changed.connect(lambda: signals.append(True))
        monkeypatch.setattr(claims_page.ClaimDialog, "exec", lambda self: 0)
        claim = claims(row(id=12, manager_id=7))[0]
        page._apply((claims_page.TAB_OPEN, [claim], [], []))

        page._open(claim)

        assert claim.manager_seen is True
        assert marked == [12] and signals == [True]

    def test_чужую_заявку_открытие_не_отмечает(self, app, monkeypatch) -> None:
        sign_in(user_id=7)
        page, _ = make_page(app)
        marked: list[int] = []
        monkeypatch.setattr(claims_page.remote, "mark_seen", lambda cid: marked.append(cid))
        monkeypatch.setattr(claims_page, "run_task", lambda fn, *a, **k: fn(*a))
        monkeypatch.setattr(claims_page.ClaimDialog, "exec", lambda self: 0)
        claim = claims(row(id=12, manager_id=8))[0]

        page._open(claim)

        assert marked == [] and claim.manager_seen is False


# --- окна ----------------------------------------------------------------------

MANAGERS = [Manager(7, "Анна Петрова"), Manager(8, "Ольга Сидорова")]


class TestClaimDialog:
    def test_пустую_заявку_не_принимает(self, app) -> None:
        dialog = ClaimDialog(None, MANAGERS)
        dialog._accept()
        assert dialog.result() == 0 and "хотя бы бренд" in dialog.error.text()

    def test_поля_уходят_на_сервер_как_введены(self, app) -> None:
        dialog = ClaimDialog(None, MANAGERS)
        dialog.brand.setText(" Rink ")
        dialog.product.setText("Крем")
        dialog.quantity.setValue(4)
        dialog.expiry.setText("31.01.2027")
        dialog.manager.setCurrentIndex(dialog.manager.findData(8))

        fields = dialog.fields()

        assert fields["brand"] == "Rink" and fields["quantity"] == 4.0
        assert fields["expiry_date"] == date(2027, 1, 31)
        assert fields["manager_id"] == 8 and fields["kind"] == "shortage"

    def test_неверная_дата_не_молча_теряется(self, app) -> None:
        dialog = ClaimDialog(None, MANAGERS)
        dialog.brand.setText("Rink")
        dialog.expiry.setText("31.13.2027")

        dialog._accept()

        assert dialog.result() == 0 and "дд.мм.гггг" in dialog.error.text()

    def test_подсказка_не_затирает_набранное(self, app) -> None:
        from app.core.claims import ClaimHint

        dialog = ClaimDialog(None, MANAGERS)
        dialog.brand.setText("Мой бренд")

        dialog._apply_hint(ClaimHint(brand="Чужой", product="Крем", spec="30 мл",
                                     manager_id=7))

        assert dialog.brand.text() == "Мой бренд"
        assert dialog.product.text() == "Крем" and dialog.spec.text() == "30 мл"
        assert dialog.manager.currentData() == 7

    def test_подсказка_не_меняет_выбранного_кма(self, app) -> None:
        from app.core.claims import ClaimHint

        dialog = ClaimDialog(None, MANAGERS)
        dialog.manager.setCurrentIndex(dialog.manager.findData(8))

        dialog._apply_hint(ClaimHint(manager_id=7))

        assert dialog.manager.currentData() == 8

    def test_правка_отдаёт_только_изменённое(self, app, monkeypatch) -> None:
        monkeypatch.setattr(ClaimDialog, "_reload_files", lambda self: None)
        claim = claims(row(quantity="1.00", manager_id=7))[0]
        dialog = ClaimDialog(claim, MANAGERS)

        assert dialog.changes() == {}

        dialog.quantity.setValue(3)
        dialog.manager.setCurrentIndex(0)
        assert dialog.changes() == {"quantity": 3.0, "manager_id": None}

    def test_закрытая_заявка_открывается_для_чтения(self, app, monkeypatch) -> None:
        monkeypatch.setattr(ClaimDialog, "_reload_files", lambda self: None)
        claim = claims(row(status="closed", can_edit=False, resolution="refund",
                           closed_by="Анна", closed_at="2026-10-20T10:00:00"))[0]

        dialog = ClaimDialog(claim, MANAGERS)

        assert not dialog.brand.isEnabled() and dialog.comment.isReadOnly()
        assert "Возместили деньгами" in dialog.info.text()

    def test_кма_без_направления_не_превращается_в_неназначенного(
            self, app, monkeypatch) -> None:
        monkeypatch.setattr(ClaimDialog, "_reload_files", lambda self: None)
        claim = claims(row(manager_id=99, manager="Уволенный КМ"))[0]

        dialog = ClaimDialog(claim, MANAGERS)

        assert dialog.manager.currentData() == 99
        assert dialog.manager.currentText() == "Уволенный КМ"


class TestCloseDialog:
    def test_без_итога_закрыть_нельзя(self, app) -> None:
        dialog = CloseDialog(claims(row())[0])

        dialog._accept()

        assert dialog.result() == 0 and "Выберите" in dialog.error.text()

    def test_итог_и_примечание_возвращаются(self, app) -> None:
        dialog = CloseDialog(claims(row())[0])
        dialog.resolution.setCurrentIndex(dialog.resolution.findData("refund"))
        dialog.note.setText(" кредит-нота 12 ")

        dialog._accept()

        assert dialog.result() == 1
        assert dialog.value() == ("refund", "кредит-нота 12")
