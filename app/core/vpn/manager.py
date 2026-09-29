"""Туннель через VPS: запуск sing-box и системного прокси.

Права администратора не нужны, потому что виртуального адаптера нет: sing-box
слушает локальный порт, а программы приходят к нему через системную настройку
прокси в профиле пользователя.

Главное требование здесь — не оставить человека без интернета. Пока туннель
включён, у него в реестре прописан адрес, по которому никто не слушает, если
sing-box упал. Поэтому:
- прежние настройки прокси записываются на диск до включения, и следующий
  запуск возвращает их, даже если приложение завершилось аварийно;
- sing-box привязан к заданию Windows и умирает вместе с приложением;
- `poll` замечает упавший sing-box и снимает прокси.
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

from .. import appdata, net
from . import binary, config, sysproxy
from .vless_uri import VlessLink

STATE_FILE = os.path.join("vpn", "state.json")
LINK_FILE = os.path.join("vpn", "link.txt")
SUBSCRIPTION_FILE = os.path.join("vpn", "subscription.txt")
START_TIMEOUT = 8.0
_CREATE_NO_WINDOW = 0x08000000


class VpnError(RuntimeError):
    """Туннель не включился или остановился; текст показывается человеку."""


def _read(name: str) -> str:
    try:
        with open(appdata.path_to(name), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _write(name: str, text: str) -> None:
    path = appdata.path_to(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.strip())


def load_link() -> str:
    """Ссылка сервера, с которой туннель поднимается при запуске."""
    return _read(LINK_FILE)


def save_link(text: str) -> None:
    """Ссылки и подписка содержат ключ доступа к серверу, поэтому лежат
    отдельными файлами в папке данных, а не в общем `settings.json`."""
    _write(LINK_FILE, text)


def load_subscription() -> str:
    return _read(SUBSCRIPTION_FILE)


def save_subscription(text: str) -> None:
    """Пустой текст удаляет подписку: сервер остаётся выбранным в `link.txt`."""
    if text.strip():
        _write(SUBSCRIPTION_FILE, text)
        return
    try:
        os.remove(appdata.path_to(SUBSCRIPTION_FILE))
    except OSError:
        pass


class VpnManager:
    def __init__(self, registry: sysproxy.ProxyRegistry | None = None) -> None:
        self._registry = registry or sysproxy.ProxyRegistry()
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._job = None
        self._server = ""
        self.last_error = ""

    @property
    def supported(self) -> bool:
        return sysproxy.SUPPORTED

    @property
    def active(self) -> bool:
        # Без блокировки: интерфейс спрашивает об этом из главного потока, а
        # запуск в фоне держит замок до нескольких секунд.
        process = self._process
        return process is not None and process.poll() is None

    @property
    def proxy_address(self) -> str:
        return self._server if self.active else ""

    def start(self, link: VlessLink) -> None:
        if not self.supported:
            raise VpnError("Туннель поддерживается только в Windows.")
        with self._lock:
            self._stop_locked()
            state = _read_state()
            if state and int(state.get("pid", 0)) != os.getpid() and _pid_alive(int(state.get("pid", 0))):
                raise VpnError("Туннель уже включён в другом окне RetailCore.")
            if state:
                # Остаток аварийно завершённого запуска: настройки возвращаются
                # раньше, чем снимается новый снимок, иначе «прежним» стал бы наш же прокси.
                self._restore_from(state)
            try:
                exe = binary.ensure_installed()
            except binary.BinaryMissing as error:
                raise VpnError(str(error)) from error

            port = _free_port()
            work = binary.work_dir()
            os.makedirs(work, exist_ok=True)
            log_file = os.path.join(work, "sing-box.log")
            config_file = os.path.join(work, "config.json")
            _remove(log_file)
            with open(config_file, "w", encoding="utf-8") as handle:
                json.dump(config.build(link, port, log_file), handle, ensure_ascii=False, indent=2)

            try:
                process = subprocess.Popen(
                    [exe, "run", "-c", config_file, "-D", work],
                    cwd=work,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except OSError as error:
                raise VpnError(f"Не удалось запустить sing-box: {error}") from error
            self._process = process
            self._job = _bind_to_job(process, self._job)

            try:
                _wait_ready(process, port, log_file)
                server = f"{config.LISTEN_HOST}:{port}"
                previous = self._registry.snapshot()
                # Снимок пишется до включения: оборвись запуск между двумя
                # действиями, следующий старт всё равно найдёт, что вернуть.
                _write_state({
                    "pid": os.getpid(),
                    "server": server,
                    "previous": sysproxy.encode_snapshot(previous),
                })
                self._registry.enable(server)
            except (VpnError, OSError) as error:
                self._kill_process()
                _remove(_state_path())
                if isinstance(error, VpnError):
                    raise
                raise VpnError(f"Не удалось включить системный прокси: {error}") from error
            self._server = server
            self.last_error = ""

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def poll(self) -> str:
        """Пусто, пока всё в порядке; иначе — почему туннель остановился.

        При остановке прокси снимается: без этого у человека перестали бы
        открываться все сайты сразу. Вызывается из главного потока, поэтому
        занятый замок означает, что запуск или остановка идут прямо сейчас, —
        ждать их нельзя, проверка повторится через несколько секунд.
        """
        if not self._lock.acquire(blocking=False):
            return ""
        try:
            process = self._process
            if process is None or process.poll() is None:
                return ""
            tail = _log_tail(os.path.join(binary.work_dir(), "sing-box.log"))
            self._stop_locked()
            self.last_error = "Туннель остановился." + (f" {tail}" if tail else "")
            return self.last_error
        finally:
            self._lock.release()

    def recover(self) -> bool:
        """Возвращает настройки прокси после аварийного завершения.

        Не трогает состояние, если им владеет другое живое окно приложения.
        """
        with self._lock:
            state = _read_state()
            if not state:
                return False
            pid = int(state.get("pid", 0))
            if pid == os.getpid() and self._process is not None:
                return False
            if pid != os.getpid() and _pid_alive(pid):
                return False
            self._restore_from(state)
            return True

    def exit_ip(self, timeout: float = 10.0) -> str:
        """Внешний адрес, каким видят компьютер через туннель."""
        address = self.proxy_address
        if not address:
            raise VpnError("Туннель выключен.")
        proxy = f"http://{address}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=net.context),
        )
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                with opener.open(url, timeout=timeout) as response:
                    text = response.read(64).decode("ascii", "replace").strip()
            except (OSError, ValueError):
                continue
            if text:
                return text
        raise VpnError("Через туннель не удалось узнать адрес: сервер не отвечает или ссылка неверна.")

    # --- внутреннее -----------------------------------------------------------

    def _stop_locked(self) -> None:
        self._kill_process()
        state = _read_state()
        if state and int(state.get("pid", 0)) == os.getpid():
            self._restore_from(state)
        self._server = ""

    def _kill_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(3)

    def _restore_from(self, state: dict) -> None:
        """Возвращает прежние настройки, если в реестре всё ещё наш адрес.

        Если человек сам сменил прокси, пока туннель работал, его выбор
        главнее нашего снимка и остаётся как есть.
        """
        try:
            if self._registry.server() == state.get("server"):
                self._registry.restore(sysproxy.decode_snapshot(state.get("previous") or {}))
        except OSError:
            return
        _remove(_state_path())


# --- файл состояния ---------------------------------------------------------


def _state_path() -> str:
    return appdata.path_to(STATE_FILE)


def _read_state() -> dict | None:
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(temporary, path)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- процессы и сеть --------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((config.LISTEN_HOST, 0))
        return probe.getsockname()[1]


def _wait_ready(process: subprocess.Popen, port: int, log_file: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = _log_tail(log_file)
            raise VpnError("sing-box завершился сразу после запуска." + (f" {tail}" if tail else ""))
        try:
            with socket.create_connection((config.LISTEN_HOST, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise VpnError("sing-box не начал принимать соединения за отведённое время.")


def _log_tail(path: str, limit: int = 400) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return " ".join(text.split())[-limit:]


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or sys.platform != "win32":
        return False
    import ctypes

    synchronize, wait_timeout = 0x00100000, 0x102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _bind_to_job(process: subprocess.Popen, job):
    """Привязывает sing-box к заданию, которое убивает его при гибели приложения.

    Без этого после аварийного завершения sing-box оставался бы в памяти и
    держал порт. Если привязка не удалась, работа продолжается: возврат
    прокси при следующем запуске от неё не зависит.
    """
    try:
        import win32api
        import win32con
        import win32job

        if job is None:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — привязка вспомогательная, запуск важнее
        pass
    return job
