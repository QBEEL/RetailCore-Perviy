"""Тесты автообновления: сравнение версий, HTTPS, целостность файла, подмена exe."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import updater


# --- версии -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("remote", "local", "newer"),
    [
        ("1.2.0", "1.1.9", True),
        ("v1.2.0", "1.1.9", True),      # тег с "v" в начале
        ("2.0.0", "2.0.0", False),      # та же версия — не новее
        ("1.9.9", "2.0.0", False),
        ("1.10.0", "1.9.0", True),      # 10 > 9, не строковое сравнение
    ],
)
def test_is_newer(remote: str, local: str, newer: bool) -> None:
    assert updater.is_newer(remote, local) is newer


def test_parse_version_unrecognized() -> None:
    assert updater.parse_version("не версия") == (0, 0, 0)


# --- https ---------------------------------------------------------------------

def test_is_https() -> None:
    assert updater.is_https("https://github.com/owner/repo/releases/download/v1/x.exe")
    assert not updater.is_https("http://github.com/owner/repo/releases/download/v1/x.exe")
    assert not updater.is_https("ftp://example.com/x.exe")


# --- целостность файла -----------------------------------------------------------

def test_verify_sha256_match(tmp_path: Path) -> None:
    file_path = tmp_path / "update.exe"
    file_path.write_bytes("новая версия приложения".encode("utf-8"))
    expected = hashlib.sha256(file_path.read_bytes()).hexdigest()
    assert updater.verify_sha256(str(file_path), expected)
    assert updater.verify_sha256(str(file_path), expected.upper())  # регистр не важен


def test_verify_sha256_mismatch(tmp_path: Path) -> None:
    file_path = tmp_path / "update.exe"
    file_path.write_bytes("повреждённый файл".encode("utf-8"))
    assert not updater.verify_sha256(str(file_path), "0" * 64)


def test_verify_sha256_empty_expected_skips_check(tmp_path: Path) -> None:
    file_path = tmp_path / "update.exe"
    file_path.write_bytes("без опубликованного хеша".encode("utf-8"))
    assert updater.verify_sha256(str(file_path), "")


# --- подмена exe и очистка ---------------------------------------------------------

def test_apply_update_renames_and_replaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = tmp_path / "RetailCore.exe"
    current.write_bytes("старая версия".encode("utf-8"))
    new_exe = tmp_path / "downloaded.exe"
    new_exe.write_bytes("новая версия".encode("utf-8"))

    captured: dict[str, object] = {}
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kwargs: captured.setdefault("args", args))

    updater.apply_update(str(new_exe), current_path=str(current))

    assert current.read_bytes() == "новая версия".encode("utf-8")
    old_exe = tmp_path / "RetailCore.old.exe"
    assert old_exe.read_bytes() == "старая версия".encode("utf-8")
    assert captured["args"] == [str(current)]


def test_cleanup_leftover_removes_old_exe(tmp_path: Path) -> None:
    current = tmp_path / "RetailCore.exe"
    current.write_bytes("текущая версия".encode("utf-8"))
    old_exe = tmp_path / "RetailCore.old.exe"
    old_exe.write_bytes("мусор от прошлого обновления".encode("utf-8"))

    updater.cleanup_leftover(current_path=str(current))

    assert not old_exe.exists()


def test_cleanup_leftover_noop_without_old_exe(tmp_path: Path) -> None:
    current = tmp_path / "RetailCore.exe"
    current.write_bytes("текущая версия".encode("utf-8"))
    updater.cleanup_leftover(current_path=str(current))  # не должно падать
