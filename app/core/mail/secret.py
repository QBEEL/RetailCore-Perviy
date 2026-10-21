"""Пароль почты: на диске он лежит только зашифрованным средствами системы.

В `settings.json` пароль не попадает: этот файл копируют, пересылают в
поддержку и кладут в архивы. Поэтому:

- Windows — DPAPI. Шифрует ключом учётной записи Windows: файл с паролем не
  расшифровать ни на другом компьютере, ни под другим пользователем;
- macOS — связка ключей через `security`, как у любой программы с паролем;
- прочие системы — пароль живёт только в памяти процесса и спрашивается при
  каждом запуске. Класть его на диск открытым текстом не станем.

Пароль здесь — «пароль для внешнего приложения», который создают в настройках
почты. Он даёт доступ только к ящику, и его можно отозвать, не меняя основной.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from .. import appdata

SECRET_FILE = os.path.join("mail", "secret.json")
_SERVICE = "RetailCore mail"

# Пароль текущего запуска. Единственное хранилище там, где системного нет, и
# страховка от сбоя чтения: ввёл — значит, до закрытия программы он есть.
_memory: dict[str, str] = {}


def persistent() -> bool:
    """Переживёт ли пароль закрытие программы на этой системе."""
    return sys.platform.startswith("win") or sys.platform == "darwin"


def save(address: str, password: str) -> None:
    """Запоминает пароль. Сбой записи на диск не мешает работать в этом запуске."""
    key = _key(address)
    _memory[key] = password
    if sys.platform.startswith("win"):
        _save_windows(key, password)
    elif sys.platform == "darwin":
        _save_macos(key, password)


def load(address: str) -> str:
    key = _key(address)
    if key in _memory:
        return _memory[key]
    if sys.platform.startswith("win"):
        found = _load_windows(key)
    elif sys.platform == "darwin":
        found = _load_macos(key)
    else:
        found = ""
    if found:
        _memory[key] = found
    return found


def forget(address: str) -> None:
    key = _key(address)
    _memory.pop(key, None)
    if sys.platform.startswith("win"):
        _forget_windows(key)
    elif sys.platform == "darwin":
        _forget_macos(key)


def _key(address: str) -> str:
    return address.strip().lower()


# --- Windows ------------------------------------------------------------------

def _file_path() -> str:
    return appdata.path_to(SECRET_FILE)


def _read_table() -> dict[str, str]:
    try:
        with open(_file_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_table(table: dict[str, str]) -> None:
    path = _file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(table, handle)
    os.replace(temporary, path)


def _save_windows(key: str, password: str) -> None:
    table = _read_table()
    table[key] = base64.b64encode(_protect(password.encode("utf-8"))).decode("ascii")
    _write_table(table)


def _load_windows(key: str) -> str:
    stored = _read_table().get(key)
    if not stored:
        return ""
    try:
        return _unprotect(base64.b64decode(stored)).decode("utf-8")
    except (OSError, ValueError):
        # Файл скопирован с другого компьютера или учётки: пароль не открыть,
        # а значит, его надо ввести заново.
        return ""


def _forget_windows(key: str) -> None:
    table = _read_table()
    if table.pop(key, None) is not None:
        _write_table(table)


def _blob_functions():  # type: ignore[no-untyped-def]
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(Blob)
    crypt32.CryptProtectData.argtypes = [
        pointer, wintypes.LPCWSTR, pointer, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, pointer]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        pointer, ctypes.c_void_p, pointer, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, pointer]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return ctypes, Blob, crypt32, kernel32


def _transform(data: bytes, protect: bool) -> bytes:
    ctypes, Blob, crypt32, kernel32 = _blob_functions()
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = Blob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), "RetailCore mail", None, None, None, 0,
            ctypes.byref(result))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI отказала")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _protect(data: bytes) -> bytes:
    return _transform(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    return _transform(data, protect=False)


# --- macOS --------------------------------------------------------------------

def _security(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["security", *arguments], capture_output=True, text=True,
                          timeout=15)


def _save_macos(key: str, password: str) -> None:
    # `-U` обновляет запись, если она уже есть. Пароль виден в списке процессов
    # на доли секунды — для связки ключей это обычная цена `security`.
    _security("add-generic-password", "-U", "-s", _SERVICE, "-a", key, "-w", password)


def _load_macos(key: str) -> str:
    try:
        result = _security("find-generic-password", "-s", _SERVICE, "-a", key, "-w")
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _forget_macos(key: str) -> None:
    try:
        _security("delete-generic-password", "-s", _SERVICE, "-a", key)
    except (OSError, subprocess.SubprocessError):
        pass
