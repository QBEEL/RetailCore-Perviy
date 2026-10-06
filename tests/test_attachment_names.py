"""Вложения и выгрузки: имена файлов, открытие, текст в ячейках, вход после смены пароля."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import workbook
from app.core.payments import PaymentFile, remote, transport
from app.core.payments.safe_files import is_risky, safe_name


@pytest.mark.parametrize("raw, expected", [
    ("счёт.pdf", "счёт.pdf"),
    ("..\\..\\Docs\\x.bat", "x.bat"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\a\\акт.xlsx", "акт.xlsx"),
    ('a"b<c>.pdf', "abc.pdf"),
    ("x\r\ny.pdf", "xy.pdf"),
    ("   ...   ", "file"),
    ("", "file"),
    (None, "file"),
])
def test_имя_вложения_очищается(raw, expected):
    assert safe_name(raw) == expected


def test_длинное_имя_укорачивается_с_сохранением_расширения():
    name = safe_name("а" * 400 + ".pdf")
    assert len(name) <= 150 and name.endswith(".pdf")


@pytest.mark.parametrize("name, risky", [
    ("счёт.pdf", False), ("акт.xlsx", False), ("архив.zip", False),
    ("setup.exe", True), ("run.BAT", True), ("счёт.pdf.exe", True),
    ("счёт.exe.pdf", False), ("ярлык.lnk", True), ("s.ps1", True),
    ("www.ozon.com.xlsx", False), ("отчёт.js.xlsx", False), ("ООО Альфа.com", True),
])
def test_запускаемые_типы_распознаются(name, risky):
    assert is_risky(name) is risky


def test_вложение_с_путём_в_имени_остаётся_в_своей_папке(tmp_path, monkeypatch):
    """Имя `..\\..\\x.bat` прислал клиент, а скачивает его чужой компьютер."""
    monkeypatch.setattr(remote.appdata, "path_to", lambda name: str(tmp_path / name))
    saved: list[str] = []
    monkeypatch.setattr(transport, "download",
                        lambda path, target: saved.append(target) or target)
    attachment = PaymentFile(id=7, payment_id=3, name="..\\..\\..\\Docs\\x.bat", size=1)

    target = Path(remote.local_copy(attachment)).resolve()

    assert target == Path(saved[0]).resolve()
    assert (tmp_path / remote.CACHE_DIR / "3" / "7").resolve() in target.parents
    assert target.name == "x.bat"


def _sheet_xml(path: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_формула_в_ячейке_выгрузки_остаётся_текстом(tmp_path):
    path = str(tmp_path / "выгрузка.xlsx")
    workbook.write_sheet(path, "Лист", ["Получатель", "Сумма"],
                         [['=СУММ(A1:A2)', 100], ["Обычный", 5]])
    assert "<f>" not in _sheet_xml(path)
    book = openpyxl.load_workbook(path)
    assert book.active["A2"].value == '=СУММ(A1:A2)'
    assert book.active["B2"].value == 100
    book.close()


def test_при_записи_значений_своя_формула_файла_остаётся_а_присланный_текст_нет(tmp_path):
    source = tmp_path / "исходный.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "=1+1"
    book.save(source)
    book.close()
    destination = str(tmp_path / "копия.xlsx")

    workbook.write_values(str(source), destination, [(2, 1, "=cmd"), (1, 2, "ok")])

    assert _sheet_xml(destination).count("<f>") == 1


# --- вход после смены пароля ---------------------------------------------------

def _answer(token: str) -> dict:
    return {"access_token": token, "login": "ivanov", "full_name": "Иванов",
            "is_admin": False, "expires_in": 3600, "user_id": 5}


def test_после_смены_пароля_клиент_берёт_новый_токен(monkeypatch):
    from app.core.payments import session_store

    saved: list[str] = []
    monkeypatch.setattr(session_store, "save", lambda session: saved.append(session.token))
    monkeypatch.setattr(transport, "post", lambda path, body=None: _answer("новый"))
    # Своя сессия: `_adopt` переписывает не только токен, и общая протекла бы в другие тесты.
    monkeypatch.setattr(transport, "session", transport.Session(
        token="старый", base_url="http://сервер"))

    transport.change_password("a", "b")

    assert transport.session.token == "новый"
    assert saved == ["новый"]


def test_сервер_прежней_версии_отвечает_пусто_и_токен_остаётся(monkeypatch):
    monkeypatch.setattr(transport, "post", lambda path, body=None: None)
    # Своя сессия: `_adopt` переписывает не только токен, и общая протекла бы в другие тесты.
    monkeypatch.setattr(transport, "session", transport.Session(
        token="старый", base_url="http://сервер"))

    transport.change_password("a", "b")

    assert transport.session.token == "старый"
