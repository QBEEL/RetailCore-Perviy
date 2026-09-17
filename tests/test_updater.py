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

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    updater.apply_update(str(new_exe), current_path=str(current))

    assert current.read_bytes() == "новая версия".encode("utf-8")
    old_exe = tmp_path / "RetailCore.old.exe"
    assert old_exe.read_bytes() == "старая версия".encode("utf-8")
    assert captured["args"] == [str(current)]
    # Новую версию нельзя запускать с окружением старой (см. child_environment).
    assert captured["env"] is not None
    assert not [name for name in captured["env"] if name.startswith(("_PYI", "_MEIPASS"))]


def test_child_environment_drops_pyinstaller_variables() -> None:
    """Унаследованный _PYI_APPLICATION_HOME_DIR уводил новый процесс в чужую
    временную папку, которую старый удалял при выходе."""
    source = {
        "PATH": "C:/Windows",
        "APPDATA": "C:/Users/u/AppData/Roaming",
        "_PYI_APPLICATION_HOME_DIR": "C:/Temp/_MEI111162",
        "_PYI_ARCHIVE_FILE": "C:/App/RetailCore.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "0",
        "_MEIPASS2": "C:/Temp/_MEI111162",
    }

    result = updater.child_environment(source)

    assert result == {"PATH": "C:/Windows", "APPDATA": "C:/Users/u/AppData/Roaming"}


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


# --- своя сборка на каждую платформу -------------------------------------------------

def test_release_asset_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS скачивал RetailCore.exe: сумма сходилась, а установка срывалась на
    запуске чужого бинарника — уже после подмены, и копия оставалась битой."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    assert updater.release_asset() == "RetailCore.dmg"

    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater.release_asset() == "RetailCore.exe"


def test_manifest_hash_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {"sha256": "windows-сумма", "sha256_macos": "macos-сумма"}

    monkeypatch.setattr(updater.sys, "platform", "darwin")
    assert updater.manifest_hash(manifest) == "macos-сумма"

    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater.manifest_hash(manifest) == "windows-сумма"


def test_manifest_hash_keeps_old_key_for_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ `sha256` читают уже выпущенные версии — переносить их нельзя."""
    monkeypatch.setattr(updater.sys, "platform", "win32")

    assert updater.manifest_hash({"sha256": "прежний ключ"}) == "прежний ключ"


def test_app_bundle_from_executable() -> None:
    # Сравнение путями, а не строками: тест идёт и на Windows, где разделитель
    # свой, а проверяется разбор пути, не его написание.
    assert Path(updater.app_bundle(
        "/Applications/RetailCore.app/Contents/MacOS/RetailCore"
    )) == Path("/Applications/RetailCore.app")
    # Запуск не из бандла: подменять нечего, и путь пустой.
    assert updater.app_bundle("/usr/local/bin/retailcore") == ""


# --- установка на macOS ---------------------------------------------------------------

@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch):
    """macOS с подменённым hdiutil: настоящий образ в тестах не монтируется."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    calls: list[tuple] = []

    def fake_run(*command: str, check: bool = True) -> None:
        calls.append(command)

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda args, **kwargs: calls.append(("popen", *args)))
    return calls


def _bundle(root: Path, name: str = "RetailCore.app") -> Path:
    """Бандл с бинарником внутри — так приложение и лежит на диске."""
    binary = root / name / "Contents" / "MacOS" / "RetailCore"
    binary.parent.mkdir(parents=True)
    binary.write_text("старая версия", encoding="utf-8")
    return binary


def test_macos_install_replaces_bundle(tmp_path: Path, macos, monkeypatch) -> None:
    """Подменяется папка целиком: бандл — не файл, и заменить его как файл
    нельзя, иначе новая версия окажется бинарником без ресурсов."""
    binary = _bundle(tmp_path)
    image = tmp_path / "RetailCore.dmg"
    image.write_bytes(b"dmg")

    # hdiutil подменён, поэтому «смонтированное» готовим сами: копия образа,
    # какой её увидит установка.
    def stage_mount(*command: str, check: bool = True) -> None:
        macos.append(command)
        if command[1] == "attach":
            mount = Path(command[command.index("-mountpoint") + 1])
            new_binary = mount / "RetailCore.app" / "Contents" / "MacOS" / "RetailCore"
            new_binary.parent.mkdir(parents=True, exist_ok=True)
            new_binary.write_text("новая версия", encoding="utf-8")

    monkeypatch.setattr(updater, "_run", stage_mount)

    updater.apply_update(str(image), current_path=str(binary))

    assert binary.read_text(encoding="utf-8") == "новая версия"
    # Промежуточные копии убраны: рядом с программой не должно остаться мусора.
    assert not (tmp_path / "RetailCore.app.new").exists()
    assert not (tmp_path / "RetailCore.app.old").exists()
    assert ("popen", "open", "-n", str(tmp_path / "RetailCore.app")) in macos


def test_macos_install_detaches_image(tmp_path: Path, macos, monkeypatch) -> None:
    """Образ отмонтируется даже если копирование не удалось — иначе он висит
    в Finder до перезагрузки."""
    binary = _bundle(tmp_path)
    image = tmp_path / "RetailCore.dmg"
    image.write_bytes(b"dmg")

    with pytest.raises(updater.ManualInstallRequired):
        # В «образе» нет RetailCore.app — hdiutil подменён и ничего не создал.
        updater.apply_update(str(image), current_path=str(binary))

    assert [c[1] for c in macos if c and c[0] == "hdiutil"] == ["attach", "detach"]


def test_macos_install_without_write_access(tmp_path: Path, macos, monkeypatch) -> None:
    """Бандл в /Applications у не-администратора: подменять нечем, и человеку
    нужно сказать, что делать, а не показать след исключения."""
    binary = _bundle(tmp_path)
    monkeypatch.setattr(updater.os, "access", lambda path, mode: False)

    with pytest.raises(updater.ManualInstallRequired) as failure:
        updater.apply_update(str(tmp_path / "RetailCore.dmg"),
                             current_path=str(binary))

    assert "Программы" in str(failure.value)
    # Образ не трогали: нечего монтировать, если ставить всё равно некуда.
    assert macos == []


def test_macos_install_outside_bundle(tmp_path: Path, macos) -> None:
    with pytest.raises(updater.ManualInstallRequired):
        updater.apply_update(str(tmp_path / "RetailCore.dmg"),
                             current_path=str(tmp_path / "retailcore"))
