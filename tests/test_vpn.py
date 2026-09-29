"""Туннель через свой сервер: ссылка, конфиг, системный прокси, запуск.

Реестр проверяется на своём временном разделе, а не на настройках интернета той
машины, где идёт прогон: тест, сломавший прокси разработчику, хуже любого
пропущенного дефекта. Запуск проверяется настоящим sing-box, если он скачан
(`tools/fetch_singbox.py`), и пропускается, если нет.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.vpn import VlessError, VpnError, VpnManager, config, manager, parse, sysproxy
from app.core.vpn import binary

USER = "11111111-2222-3333-4444-555555555555"
TLS_LINK = (
    f"vless://{USER}@vpn.example.com:443?encryption=none&security=tls&sni=vpn.example.com"
    "&fp=chrome&alpn=h2%2Chttp%2F1.1&type=ws&host=cdn.example.com&path=%2Fws%2Fabc#Finland")
PLAIN_LINK = f"vless://{USER}@203.0.113.7:8080?encryption=none&security=none&type=ws&path=%2Fabc"

windows_only = pytest.mark.skipif(not sysproxy.SUPPORTED, reason="системный прокси — только Windows")
needs_sing_box = pytest.mark.skipif(
    binary.bundled_path() is None, reason="sing-box не скачан: tools/fetch_singbox.py")


# --- ссылка --------------------------------------------------------------------


def test_ссылка_с_tls_разбирается_целиком():
    link = parse(TLS_LINK)

    assert (link.user_id, link.host, link.port, link.tls) == (USER, "vpn.example.com", 443, True)
    assert link.path == "/ws/abc"
    assert link.ws_host == "cdn.example.com"
    assert link.fingerprint == "chrome"
    assert link.alpn == ("h2", "http/1.1")
    assert link.name == "Finland"


def test_ссылка_без_tls_и_с_адресом_числом():
    link = parse(PLAIN_LINK)

    assert (link.host, link.port, link.tls) == ("203.0.113.7", 8080, False)


def test_ipv6_адрес_в_скобках():
    link = parse(f"vless://{USER}@[2001:db8::1]:443?security=tls&type=ws&path=/")

    assert link.host == "2001:db8::1"
    assert link.port == 443


def test_путь_без_косой_черты_и_пустой_путь():
    assert parse(f"vless://{USER}@h.example:1?type=ws&path=ws").path == "/ws"
    assert parse(f"vless://{USER}@h.example:1?type=ws").path == "/"


def test_имя_для_сертификата_берётся_из_sni_потом_из_host():
    only_host = parse(f"vless://{USER}@1.2.3.4:443?security=tls&type=ws&host=site.example")

    assert only_host.server_name == "site.example"
    assert parse(TLS_LINK).server_name == "vpn.example.com"


@pytest.mark.parametrize("text, fragment", [
    ("", "Вставьте ссылку"),
    ("https://example.com", "vless://"),
    (f"vless://не-uuid@h.example:443?type=ws", "UUID"),
    (f"vless://{USER}@h.example?type=ws", "порт"),
    (f"vless://{USER}@:443?type=ws", "адрес"),
    (f"vless://{USER}@h.example:443?type=tcp", "WebSocket"),
    (f"vless://{USER}@h.example:443", "WebSocket"),
    (f"vless://{USER}@h.example:443?type=grpc&security=tls", "WebSocket"),
    (f"vless://{USER}@h.example:443?type=ws&security=reality", "Защита"),
    (f"vless://{USER}@h.example:443?type=ws&flow=xtls-rprx-vision", "flow"),
    (f"vless://{USER}@h.example:443?type=ws&encryption=mlkem", "encryption"),
])
def test_непригодная_ссылка_отклоняется_с_понятным_текстом(text, fragment):
    with pytest.raises(VlessError, match=fragment):
        parse(text)


def test_отключение_проверки_сертификата_не_принимается():
    """Такая ссылка отдала бы трафик тому, кто встал между компьютером и сервером."""
    with pytest.raises(VlessError, match="allowInsecure"):
        parse(f"vless://{USER}@h.example:443?type=ws&security=tls&allowInsecure=1")


# --- конфигурация ----------------------------------------------------------------


def test_конфиг_слушает_только_локальный_адрес():
    cfg = config.build(parse(TLS_LINK), 18080, "sb.log")

    inbound = cfg["inbounds"][0]
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["listen_port"] == 18080
    assert inbound["type"] == "mixed"


def test_конфиг_tls_несёт_имя_отпечаток_и_заголовок_host():
    cfg = config.build(parse(TLS_LINK), 18080, "sb.log")

    proxy = cfg["outbounds"][0]
    assert proxy["type"] == "vless"
    assert (proxy["server"], proxy["server_port"], proxy["uuid"]) == ("vpn.example.com", 443, USER)
    assert proxy["transport"] == {"type": "ws", "path": "/ws/abc", "headers": {"Host": "cdn.example.com"}}
    assert proxy["tls"]["enabled"] is True
    assert proxy["tls"]["server_name"] == "vpn.example.com"
    assert proxy["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}
    assert proxy["tls"]["alpn"] == ["h2", "http/1.1"]
    assert "insecure" not in proxy["tls"]


def test_конфиг_без_tls_не_содержит_блока_tls():
    proxy = config.build(parse(PLAIN_LINK), 18080, "sb.log")["outbounds"][0]

    assert "tls" not in proxy


def test_адрес_числом_не_становится_именем_сертификата():
    link = parse(f"vless://{USER}@203.0.113.7:443?type=ws&security=tls")

    assert "server_name" not in config.build(link, 1, "sb.log")["outbounds"][0]["tls"]


def test_весь_трафик_идёт_в_туннель():
    assert config.build(parse(PLAIN_LINK), 1, "sb.log")["route"]["final"] == "proxy"


# --- системный прокси ------------------------------------------------------------


@pytest.fixture
def scratch_key():
    """Временный раздел реестра, который убирается после теста."""
    import winreg

    path = rf"Software\RetailCoreTest\{uuid.uuid4().hex}"
    yield path
    for sub in (path, r"Software\RetailCoreTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def _set(path: str, **values):
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        for name, (value, kind) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)


@windows_only
def test_включение_прописывает_адрес_исключения_и_включает_прокси(scratch_key):
    import winreg

    registry = sysproxy.ProxyRegistry(scratch_key)
    registry.enable("127.0.0.1:18080")

    assert registry.server() == "127.0.0.1:18080"
    snapshot = registry.snapshot()
    assert snapshot["ProxyEnable"] == (1, winreg.REG_DWORD)
    override = snapshot["ProxyOverride"][0].split(";")
    for bypass in ("<local>", "*.ru", "retail.qbeely.ru", "*.crpt.ru", "127.*"):
        assert bypass in override


@windows_only
def test_выключение_возвращает_прежние_значения_как_были(scratch_key):
    import winreg

    _set(scratch_key,
         ProxyEnable=(1, winreg.REG_DWORD),
         ProxyServer=("corp-proxy:3128", winreg.REG_SZ),
         ProxyOverride=("*.corp.local", winreg.REG_SZ),
         AutoConfigURL=("http://corp/proxy.pac", winreg.REG_SZ))
    registry = sysproxy.ProxyRegistry(scratch_key)
    before = registry.snapshot()

    registry.enable("127.0.0.1:18080")
    assert "AutoConfigURL" in registry.snapshot() and registry.snapshot()["AutoConfigURL"] is None
    registry.restore(before)

    assert registry.snapshot() == before


@windows_only
def test_записей_не_было_значит_после_выключения_их_снова_нет(scratch_key):
    registry = sysproxy.ProxyRegistry(scratch_key)
    before = registry.snapshot()
    assert all(entry is None for entry in before.values())

    registry.enable("127.0.0.1:18080")
    registry.restore(before)

    assert registry.server() == ""
    assert registry.snapshot() == before


def test_снимок_переживает_запись_на_диск():
    snapshot = {"ProxyEnable": (1, 4), "ProxyServer": ("a:1", 1), "ProxyOverride": None, "AutoConfigURL": None}

    encoded = json.loads(json.dumps(sysproxy.encode_snapshot(snapshot)))

    assert sysproxy.decode_snapshot(encoded) == snapshot


# --- запуск ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"порт {port} не открылся")


@pytest.fixture
def registry(scratch_key):
    return sysproxy.ProxyRegistry(scratch_key)


@windows_only
@needs_sing_box
def test_запуск_включает_прокси_а_остановка_возвращает_прежний(registry):
    import winreg

    _set(registry._key_path,
         ProxyEnable=(1, winreg.REG_DWORD), ProxyServer=("corp-proxy:3128", winreg.REG_SZ))
    before = registry.snapshot()
    vpn = VpnManager(registry)

    vpn.start(parse(PLAIN_LINK))
    try:
        assert vpn.active
        assert registry.server() == vpn.proxy_address
        assert Path(manager._state_path()).is_file()
    finally:
        vpn.stop()

    assert not vpn.active
    assert registry.snapshot() == before
    assert not Path(manager._state_path()).exists()


@windows_only
@needs_sing_box
def test_после_аварии_следующий_запуск_возвращает_прокси(registry):
    """Приложение убили, не дав выключить туннель: реестр не должен остаться
    с адресом, по которому никто не слушает."""
    before = registry.snapshot()
    crashed = VpnManager(registry)
    crashed.start(parse(PLAIN_LINK))
    process = crashed._process
    assert registry.server()

    process.kill()
    process.wait(5)
    crashed._process = None  # приложение «умерло»: его памяти больше нет

    assert VpnManager(registry).recover() is True
    assert registry.snapshot() == before
    assert not Path(manager._state_path()).exists()


@windows_only
@needs_sing_box
def test_упавший_sing_box_снимает_прокси(registry):
    before = registry.snapshot()
    vpn = VpnManager(registry)
    vpn.start(parse(PLAIN_LINK))

    vpn._process.kill()
    vpn._process.wait(5)
    problem = vpn.poll()

    assert "остановился" in problem
    assert not vpn.active
    assert registry.snapshot() == before


@windows_only
@needs_sing_box
def test_выбранный_человеком_прокси_не_затирается_возвратом(registry):
    """Пока туннель работал, человек сам сменил прокси: его выбор главнее снимка."""
    import winreg

    vpn = VpnManager(registry)
    vpn.start(parse(PLAIN_LINK))
    _set(registry._key_path, ProxyServer=("mine:8888", winreg.REG_SZ))

    vpn.stop()

    assert registry.server() == "mine:8888"


@windows_only
def test_чужое_живое_окно_туннель_не_отбирается(registry):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        manager._write_state({"pid": other.pid, "server": "127.0.0.1:1", "previous": {}})
        vpn = VpnManager(registry)

        assert vpn.recover() is False
        with pytest.raises(VpnError, match="другом окне"):
            vpn.start(parse(PLAIN_LINK))
        assert Path(manager._state_path()).is_file()
    finally:
        other.kill()


@windows_only
def test_без_sing_box_ошибка_понятна(registry, monkeypatch):
    monkeypatch.setattr(binary, "bundled_path", lambda: None)

    with pytest.raises(VpnError, match="sing-box"):
        VpnManager(registry).start(parse(PLAIN_LINK))
    assert registry.server() == ""


def test_ссылка_хранится_в_папке_данных_отдельным_файлом():
    manager.save_link("  vless://x  ")

    assert manager.load_link() == "vless://x"
    assert manager.load_link.__module__ == manager.__name__
    assert Path(manager.appdata.path_to(manager.LINK_FILE)).parent.name == "vpn"


# --- сквозная проверка: клиент → свой сервер → сайт ---------------------------------


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = "через туннель".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@windows_only
@needs_sing_box
def test_трафик_доходит_до_сайта_через_туннель(registry, tmp_path):
    """Второй sing-box играет роль сервера с 3X-UI: VLESS по WebSocket.

    Проверяется вся цепочка на одной машине — то, чего не даёт ни разбор
    ссылки, ни проверка конфигурации: клиент действительно соединяется с
    сервером по параметрам из ссылки и передаёт по ним запрос.
    """
    server_port, site_port = _free_port(), _free_port()
    site = HTTPServer(("127.0.0.1", site_port), _Ok)
    threading.Thread(target=site.serve_forever, daemon=True).start()

    server_config = tmp_path / "server.json"
    server_config.write_text(json.dumps({
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "vless", "listen": "127.0.0.1", "listen_port": server_port,
            "users": [{"uuid": USER}],
            "transport": {"type": "ws", "path": "/ws"},
        }],
        "outbounds": [{"type": "direct"}],
    }), encoding="utf-8")
    server = subprocess.Popen(
        [str(binary.bundled_path()), "run", "-c", str(server_config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
    vpn = VpnManager(registry)
    try:
        _wait_port(server_port)
        vpn.start(parse(f"vless://{USER}@127.0.0.1:{server_port}?encryption=none&security=none&type=ws&path=%2Fws"))

        proxy = f"http://{vpn.proxy_address}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy}))
        with opener.open(f"http://127.0.0.1:{site_port}/", timeout=10) as response:
            assert response.read().decode("utf-8") == "через туннель"
    finally:
        vpn.stop()
        server.kill()
        server.wait(5)
        site.shutdown()


# --- sing-box в сборке ---------------------------------------------------------------


def test_в_собранном_приложении_sing_box_ищется_во_временной_папке_сборки(tmp_path, monkeypatch):
    bundle = tmp_path / "_MEI" / "vpn"
    bundle.mkdir(parents=True)
    (bundle / "sing-box.exe").write_bytes(b"binary")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI"), raising=False)

    assert binary.bundled_path() == bundle / "sing-box.exe"


def test_рабочая_копия_ставится_в_папку_данных_и_не_копируется_заново(tmp_path):
    source = tmp_path / "sing-box.exe"
    source.write_bytes(b"binary")

    installed = binary.ensure_installed(source)

    assert installed == binary.installed_path()
    assert Path(installed).read_bytes() == b"binary"
    stamp = os.path.getmtime(installed)
    assert binary.ensure_installed(source) == installed
    assert os.path.getmtime(installed) == stamp


def test_другой_размер_в_сборке_заменяет_рабочую_копию(tmp_path):
    source = tmp_path / "sing-box.exe"
    source.write_bytes(b"old")
    binary.ensure_installed(source)

    source.write_bytes(b"newer version")
    binary.ensure_installed(source)

    assert Path(binary.installed_path()).read_bytes() == b"newer version"


# --- подписка ------------------------------------------------------------------------


def _subscription_text() -> str:
    """Структура настоящей подписки 3X-UI: серверы разных протоколов вперемешку."""
    return "\n".join([
        f"hy2://{USER}@hy.example.test:8443?sni=hy.example.test&insecure=0#NL",
        f"vless://{USER}@fi.example.test:8443?type=ws&security=tls&sni=fi.example.test"
        "&host=fi.example.test&path=%2Fa&fp=firefox&alpn=http%2F1.1&encryption=none"
        "#%F0%9F%87%AB%F0%9F%87%AE%20NET%20%7C%20FI-4",
        f"hy2://{USER}@150.0.2.1:8443?sni=150.0.2.1&insecure=0#SE-3",
        f"vless://{USER}@se.example.test:8443?type=ws&security=tls&sni=se.example.test"
        "&host=se.example.test&path=%2Fb&fp=chrome&alpn=http%2F1.1&encryption=none#SE-4",
    ])


def test_подписка_в_base64_отдаёт_только_поддерживаемые_серверы():
    import base64

    from app.core.vpn import subscription

    links = subscription.decode(base64.b64encode(_subscription_text().encode("utf-8")))

    assert [link.host for link in links] == ["fi.example.test", "se.example.test"]
    assert links[0].name == "🇫🇮 NET | FI-4"


def test_подписка_открытым_текстом_тоже_читается():
    from app.core.vpn import subscription

    assert len(subscription.decode(_subscription_text().encode("utf-8"))) == 2


def test_подписка_без_подходящих_серверов_объясняет_почему():
    from app.core.vpn import subscription

    with pytest.raises(VlessError, match="Пропущено других: 1"):
        subscription.decode(f"hy2://{USER}@h.example:1#x".encode("utf-8"))


@pytest.mark.parametrize("url", ["http://sub.example.test/x", "ftp://x", "sub.example.test", "https://"])
def test_подписка_только_по_https(url):
    from app.core.vpn import subscription

    with pytest.raises(VlessError, match="https://"):
        subscription.fetch(url)


def test_сервер_по_умолчанию_финский_а_прежний_выбор_главнее():
    from app.core.vpn import subscription

    links = subscription.decode(_subscription_text().encode("utf-8"))

    assert subscription.preferred(links).host == "fi.example.test"
    assert subscription.preferred(links, "SE-4").host == "se.example.test"
    assert subscription.preferred(links, "нет такого").host == "fi.example.test"
    assert subscription.preferred([links[1]]).host == "se.example.test"


def test_подписка_хранится_отдельным_файлом_и_удаляется_пустым_текстом():
    manager.save_subscription("https://sub.example.test/x")
    assert manager.load_subscription() == "https://sub.example.test/x"

    manager.save_subscription("")

    assert manager.load_subscription() == ""
