"""Системный прокси Windows в профиле пользователя.

Настройки лежат в `HKEY_CURRENT_USER`, поэтому права администратора не нужны.
Их читают Chrome, Edge, `urllib` и большинство прочих программ; не читают те,
у кого свои настройки (Firefox по умолчанию, часть мессенджеров и игр).

Прежние значения снимаются целиком, вместе с типом записи, и возвращаются как
были: у человека мог стоять корпоративный прокси или файл автонастройки, и
после выключения он должен получить именно их, а не пустоту.
"""
from __future__ import annotations

import sys
from typing import Any

SUPPORTED = sys.platform == "win32"

KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# Записи, которые меняет включение. AutoConfigURL сбрасывается на время работы:
# файл автонастройки главнее ProxyServer и увёл бы трафик мимо туннеля.
_ENABLE = "ProxyEnable"
_SERVER = "ProxyServer"
_OVERRIDE = "ProxyOverride"
_AUTOCONFIG = "AutoConfigURL"
_NAMES = (_ENABLE, _SERVER, _OVERRIDE, _AUTOCONFIG)

# Что идёт мимо туннеля: своя сеть, российские зоны, сервер оплат и «Честный
# ЗНАК». Маркировка ходит с российского адреса, а через зарубежный её могут не
# пустить; сервер оплат — наш собственный и в обходе не нуждается.
DEFAULT_BYPASS = (
    "<local>",
    "localhost",
    "127.*",
    "10.*",
    "192.168.*",
    "172.16.*", "172.17.*", "172.18.*", "172.19.*", "172.20.*", "172.21.*", "172.22.*",
    "172.23.*", "172.24.*", "172.25.*", "172.26.*", "172.27.*", "172.28.*", "172.29.*",
    "172.30.*", "172.31.*",
    "*.ru", "*.su", "*.рф", "*.xn--p1ai",
    "retail.qbeely.ru",
    "*.crpt.ru", "*.crptech.ru",
)

# Снимок записи: (значение, тип) или None, если записи не было.
Snapshot = dict[str, "tuple[Any, int] | None"]


class ProxyRegistry:
    """Чтение и запись записей прокси в одном разделе реестра.

    Раздел задаётся параметром, чтобы проверки работали на своём временном
    разделе, а не на настройках интернета той машины, где идёт прогон.
    """

    def __init__(self, key_path: str = KEY_PATH, *, notify: bool = True) -> None:
        self._key_path = key_path
        self._notify = notify and key_path == KEY_PATH

    def snapshot(self) -> Snapshot:
        import winreg

        result: Snapshot = {}
        with self._open(winreg.KEY_READ) as key:
            for name in _NAMES:
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    result[name] = None
                else:
                    result[name] = (value, kind)
        return result

    def server(self) -> str:
        """Адрес прокси, который сейчас записан, и пусто, если прокси выключен."""
        import winreg

        with self._open(winreg.KEY_READ) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, _ENABLE)
                server, _ = winreg.QueryValueEx(key, _SERVER)
            except FileNotFoundError:
                return ""
        return str(server) if enabled else ""

    def enable(self, server: str, bypass: tuple[str, ...] = DEFAULT_BYPASS) -> None:
        import winreg

        with self._open(winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _SERVER, 0, winreg.REG_SZ, server)
            winreg.SetValueEx(key, _OVERRIDE, 0, winreg.REG_SZ, ";".join(bypass))
            try:
                winreg.DeleteValue(key, _AUTOCONFIG)
            except FileNotFoundError:
                pass
            # Включение — последним: пока адрес и исключения не записаны,
            # включённый прокси указывал бы на прежнее или пустое значение.
            winreg.SetValueEx(key, _ENABLE, 0, winreg.REG_DWORD, 1)
        self._announce()

    def restore(self, snapshot: Snapshot) -> None:
        import winreg

        with self._open(winreg.KEY_SET_VALUE) as key:
            # Выключение — первым, по той же причине, что и включение — последним.
            saved = snapshot.get(_ENABLE)
            winreg.SetValueEx(key, _ENABLE, 0, winreg.REG_DWORD,
                              int(saved[0]) if saved else 0)
            for name in (_SERVER, _OVERRIDE, _AUTOCONFIG):
                _put(key, name, snapshot.get(name))
            # Записи, которой не было, не должно быть и после: 0 в ней и её
            # отсутствие для системы одно и то же, но «как было» — это точно.
            if saved is None:
                _put(key, _ENABLE, None)
        self._announce()

    def _open(self, access: int):
        import winreg

        return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self._key_path, 0, access)

    def _announce(self) -> None:
        """Сообщает системе, что настройки сменились: без этого браузеры
        подхватывают их только после перезапуска."""
        if not self._notify:
            return
        try:
            import ctypes

            internet_set_option = ctypes.windll.wininet.InternetSetOptionW
            internet_set_option(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
            internet_set_option(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
        except (AttributeError, OSError):
            pass


def _put(key, name: str, entry: "tuple[Any, int] | None") -> None:
    """Записывает значение с прежним типом или удаляет запись, которой не было."""
    import winreg

    if entry is None:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    else:
        winreg.SetValueEx(key, name, 0, entry[1], entry[0])


def encode_snapshot(snapshot: Snapshot) -> dict[str, list[Any] | None]:
    return {name: None if entry is None else [entry[0], entry[1]] for name, entry in snapshot.items()}


def decode_snapshot(data: dict[str, Any]) -> Snapshot:
    result: Snapshot = {}
    for name in _NAMES:
        entry = data.get(name)
        result[name] = (entry[0], int(entry[1])) if isinstance(entry, list) and len(entry) == 2 else None
    return result
