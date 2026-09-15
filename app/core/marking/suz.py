"""СУЗ — станция управления заказами: реквизиты соединения и проверка связи.

Третья система «Честного ЗНАКа» и единственная, которая выдаёт коды. ГИС МТ
ведёт документы оборота, True API отвечает на вопросы о кодах, а заказать их
можно только здесь. Отсюда и отдельный модуль: у СУЗ ни адрес, ни способ
авторизации не совпадают с остальными.

Реквизитов три, и выдаются они в личном кабинете при регистрации устройства:

- **ОМС ID** — какая именно станция обслуживает участника оборота;
- **токен** — чем устройство подтверждает, что оно и есть оно;
- **идентификатор соединения** — какое устройство обращается.

Первые два уходят в каждый запрос: ОМС ID параметром, токен заголовком. Третий
не уходит ни в один рабочий метод, но нужен раньше — при запросе самого токена.

**Токен динамический и живёт десять часов.** Это главное про него, и это не
предположение: так написано в инструкции ЦРПТ к настройке обмена с СУЗ. Отсюда
всё остальное устройство модуля. Хранить токен между запусками можно, полагаться
на то, что он ещё жив, — нельзя, а продлевать его походом в другую программу
человек не должен: сертификат есть, вход по нему устроен, и на отказ по учётным
данным приложение добывает новый токен само.

Хранятся реквизиты отдельно от `settings.json`: там недавние файлы и размеры
окон, этот файл копируют и пересылают, а токен СУЗ равносилен доступу к заказу
кодов от имени организации. Наборов два, по одному на контур: песочница и бой —
разные станции с разными устройствами, и путать их нельзя.

Что проверено обращением к живой системе, а не взято из описания: оба адреса
отвечают на `/api/v3/ping` и на `/api/v3/auth/key`; непринятые реквизиты
возвращаются кодом **400**, а не 401, и текст отказа лежит списком под
`globalErrors` — «Проверка учетных данных УОТ не пройдена», код 1110.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .. import appdata
from . import crypto, session, transport
from .models import Contour

FILE_NAME = "marking_suz.json"

# Проверка связи. Метод ничего не меняет и не тратит квоту заказов — его и
# спрашивают, чтобы убедиться, что реквизиты приняты.
PING_PATH = "/ping"

# Вход по сертификату. Устроен так же, как в True API: строка для подписи →
# подпись → токен. Проверено обращением: `/auth/key` отвечает на обоих адресах
# СУЗ, а `simpleSignIn` с непригодной подписью отвечает «Найденные данные по
# UUID … не совпадают с подписанными» — то есть метод существует и проверяет
# именно подпись.
KEY_PATH = "/auth/key"
SIGN_IN_PATH = "/auth/simpleSignIn"

# Заголовок, которым СУЗ принимает токен. Не `Authorization: Bearer`, как две
# другие системы, — отсюда `headers` в транспорте.
TOKEN_HEADER = "clientToken"

# Сколько живёт выданный токен. Не наша оценка: так написано в инструкции ЦРПТ
# к настройке обмена с СУЗ. В самом ответе срок не приходит, поэтому считается
# от времени выдачи.
LIFETIME = timedelta(hours=10)

# За сколько до конца срока обновлять, не дожидаясь отказа. Обновление стоит
# одного обращения к ключу, а протухший посреди работы токен — прерванной
# приёмки.
RENEW_MARGIN = timedelta(minutes=10)

# Как СУЗ говорит, что токен её не устраивает. Код ответа при этом обычный
# четырёхсотый, поэтому различать приходится по тексту и коду ошибки.
STALE_MARKS: tuple[str, ...] = ("учетных данных", "учётных данных", "1110")


def stale(error: object) -> bool:
    """Отказ ли это из-за протухшего или непринятого токена.

    Нужно для того, чтобы обновить токен самим, а не звать человека в другую
    программу за новым.
    """
    if getattr(error, "status", 0) not in (400, 401, 403):
        return False
    text = str(error).lower()
    return any(mark in text for mark in STALE_MARKS)


@dataclass(frozen=True, slots=True)
class Credentials:
    """Чем приложение представляется станции управления заказами.

    `host` пустой означает встроенный адрес облачной СУЗ. Поле существует ради
    локальной станции: она стоит в сети предприятия, и адрес у неё свой — знать
    его заранее приложение не может.
    """

    oms_id: str = ""
    connection_id: str = ""
    token: str = ""
    host: str = ""
    # Когда токен получен. По ней и считается срок: сам токен приходит без него,
    # а живёт десять часов.
    issued_at: str = ""

    @property
    def filled(self) -> bool:
        return not self.missing

    @property
    def issued(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.issued_at) if self.issued_at else None
        except ValueError:
            return None

    @property
    def expiring(self) -> bool:
        """Пора обновлять. Неизвестное время выдачи считается «пора».

        Токен из другой программы приходит без отметки времени, и относиться к
        нему нужно как к просроченному: он мог быть выдан вчера.
        """
        moment = self.issued
        if moment is None:
            return bool(self.token)
        return datetime.now() >= moment + LIFETIME - RENEW_MARGIN

    @property
    def expires_in(self) -> timedelta | None:
        """Сколько токену осталось. `None` — время выдачи неизвестно."""
        moment = self.issued
        return moment + LIFETIME - datetime.now() if moment else None

    @property
    def missing(self) -> tuple[str, ...]:
        """Чего не хватает для обращения к СУЗ — человеческими названиями.

        Идентификатора соединения здесь нет, но по другой причине, чем казалось
        сначала. В сами запросы он действительно не уходит — с ним и без него
        `ping` отвечает одинаково. Нужен он раньше: при запросе токена. Поэтому
        мешать работе с уже полученным токеном он не должен, а его отсутствие
        всплывёт там, где важно, — в `sign_in`.
        """
        gaps = []
        if not self.oms_id.strip():
            gaps.append("ОМС ID")
        if not self.token.strip():
            gaps.append("токен")
        return tuple(gaps)

    def stripped(self) -> "Credentials":
        """Те же реквизиты без случайных пробелов.

        Значения переносят из личного кабинета через буфер обмена, и хвостовой
        пробел или перенос строки приезжает вместе с ними. В заголовке запроса
        такой пробел превращается в отказ, объяснить который невозможно.
        """
        return Credentials(
            oms_id=self.oms_id.strip(),
            connection_id=self.connection_id.strip(),
            token=self.token.strip(),
            host=self.host.strip().rstrip("/"),
            issued_at=self.issued_at.strip(),
        )

    @property
    def masked_token(self) -> str:
        return mask(self.token)


def renewed(credentials: Credentials, token: str) -> Credentials:
    """Те же реквизиты с только что полученным токеном и отметкой времени."""
    return Credentials(
        oms_id=credentials.oms_id,
        connection_id=credentials.connection_id,
        token=token,
        host=credentials.host,
        issued_at=datetime.now().isoformat(timespec="minutes"),
    ).stripped()


def mask(token: str) -> str:
    """Токен в виде, пригодном для показа и для письма в поддержку.

    Начало и конец оставляются: по ним человек узнаёт, тот ли токен вписан, а
    воспользоваться показанным нельзя.
    """
    value = token.strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


# --- хранение ------------------------------------------------------------------

def path() -> str:
    return appdata.path_to(FILE_NAME)


def load(contour: Contour = Contour.SANDBOX) -> Credentials:
    """Реквизиты контура. Испорченный файл — то же, что его отсутствие."""
    return _all().get(contour.value, Credentials())


def save(credentials: Credentials, contour: Contour = Contour.SANDBOX) -> Credentials:
    """Запоминает реквизиты контура, не трогая второй.

    Возвращает то, что действительно сохранено: пробелы обрезаны, адрес без
    хвостовой косой черты.
    """
    cleaned = credentials.stripped()
    saved = _all()
    saved[contour.value] = cleaned
    _write(saved)
    apply(cleaned, contour)
    return cleaned


def forget(contour: Contour = Contour.SANDBOX) -> None:
    """Убирает реквизиты контура — вместе с токеном из файла."""
    saved = _all()
    saved.pop(contour.value, None)
    _write(saved)
    apply(Credentials(), contour)


def known(contour: Contour = Contour.SANDBOX) -> bool:
    return load(contour).filled


def restore() -> None:
    """Применяет сохранённые адреса при запуске приложения.

    Сами реквизиты читаются вкладкой по мере надобности, а вот адрес обязан
    попасть в транспорт до первого запроса: иначе первый же уйдёт по
    встроенному адресу, который пользователь как раз и заменил.
    """
    for contour in (Contour.SANDBOX, Contour.PRODUCTION):
        apply(load(contour), contour)


def apply(credentials: Credentials, contour: Contour = Contour.SANDBOX) -> None:
    """Ставит адрес СУЗ для контура. Пустой возвращает встроенный."""
    if credentials.host.strip():
        transport.configure("suz", contour, credentials.host)
    else:
        transport.configure("suz", contour, DEFAULT_HOSTS[contour])


# Встроенные адреса запоминаются до того, как их кто-нибудь переопределит:
# `apply` с пустым адресом обязан вернуть именно их, а не то, что стояло в
# транспорте на момент вызова.
DEFAULT_HOSTS: dict[Contour, str] = {
    contour: transport.HOSTS.get(("suz", contour), "")
    for contour in (Contour.SANDBOX, Contour.PRODUCTION)
}


def _all() -> dict[str, Credentials]:
    try:
        with open(path(), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found: dict[str, Credentials] = {}
    for contour in (Contour.SANDBOX, Contour.PRODUCTION):
        item = raw.get(contour.value)
        if isinstance(item, dict):
            found[contour.value] = Credentials(
                oms_id=str(item.get("oms_id") or ""),
                connection_id=str(item.get("connection_id") or ""),
                token=str(item.get("token") or ""),
                host=str(item.get("host") or ""),
                issued_at=str(item.get("issued_at") or ""),
            ).stripped()
    return found


def _write(everything: dict[str, Credentials]) -> None:
    payload = {
        name: {
            "oms_id": item.oms_id,
            "connection_id": item.connection_id,
            "token": item.token,
            "host": item.host,
            "issued_at": item.issued_at,
        }
        for name, item in everything.items()
    }
    target = path()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if directory := os.path.dirname(target):
        os.makedirs(directory, exist_ok=True)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, target)
    # В файле лежит действующий токен: читать его посторонним на общей машине
    # незачем. На Windows ограничение приблизительное, но лучше, чем ничего, и
    # ошибку прав игнорируем — не ради неё всё.
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


# --- вход по сертификату ----------------------------------------------------------

def sign_in(thumbprint: str, inn: str = "",
            contour: Contour = Contour.SANDBOX,
            connection_id: str = "") -> str:
    """Получает токен СУЗ подписью, а не выпиской из чужой программы.

    Устроено так же, как вход в True API: сервер выдаёт случайную строку, её
    подписывают усиленной квалифицированной подписью и возвращают в обмен на
    токен. Значит, обращение к закрытому ключу — носитель должен быть вставлен,
    а КриптоПро может спросить пароль к контейнеру.

    Токен **динамический и живёт десять часов** — это не предположение, а то, что
    написано в инструкции ЦРПТ к настройке обмена с СУЗ. Отсюда и всё остальное
    устройство: хранить его между запусками можно, а полагаться на то, что он
    ещё жив, — нельзя.

    Идентификатор соединения нужен именно здесь. В сами запросы он не уходит —
    `ping` отвечает одинаково с ним и без него, — а вот токен выдаётся под
    конкретное устройство, и без него запрос не о чем.
    """
    certificate = crypto.find(thumbprint)
    if certificate is None:
        raise crypto.SigningFailed(
            "Сертификат не найден. Возможно, подключён другой носитель ключа.")

    challenge = transport.get("suz", KEY_PATH, contour=contour)
    uuid_value, data = session.parse_challenge(challenge)
    signature = crypto.sign(data, thumbprint)

    body: dict[str, Any] = {"uuid": uuid_value, "data": signature}
    if inn.strip():
        body["inn"] = inn.strip()
    # Идентификатор соединения уходит и полем, и параметром. Выглядит
    # избыточно, но каждая попытка стоит человеку ввода пароля к контейнеру, а
    # какое из двух мест верное, из строк чужой библиотеки не видно: там
    # `omsConnection` лежит отдельным словом, без `?` и `=`. Лишний неизвестный
    # параметр сервер игнорирует, лишний вопрос пользователю — нет.
    params: dict[str, Any] = {}
    if connection_id.strip():
        body["omsConnection"] = connection_id.strip()
        params["omsConnection"] = connection_id.strip()
    granted = transport.post("suz", SIGN_IN_PATH, contour=contour,
                             params=params or None, body=body)

    token = _token(granted)
    if not token:
        raise transport.MarkingError(
            "СУЗ приняла подпись, но токен не выдала. Проверьте, что этот "
            "сертификат заведён в личном кабинете СУЗ как устройство.")
    return token


def _token(answer: Any) -> str:
    if not isinstance(answer, dict):
        return ""
    for name in ("token", "clientToken", "client_token"):
        if value := answer.get(name):
            return str(value).strip()
    return ""


# --- проверка соединения ---------------------------------------------------------

def check(credentials: Credentials,
          contour: Contour = Contour.SANDBOX) -> str:
    """Спрашивает СУЗ, принимает ли она эти реквизиты. Вернёт текст для показа.

    Ничего не заказывает и не меняет: `ping` для того и существует, чтобы
    убедиться в связи, не потратив ни одного кода.

    Незаполненные реквизиты до сети не доходят: сказать «не хватает токена»
    можно и здесь, а ответ сервера на пустой заголовок объяснит это хуже.
    """
    ready = credentials.stripped()
    if gaps := ready.missing:
        raise transport.MarkingError(f"Не заполнено: {', '.join(gaps)}.")

    apply(ready, contour)
    try:
        answer = transport.get(
            "suz", PING_PATH, contour=contour,
            params={"omsId": ready.oms_id},
            headers={TOKEN_HEADER: ready.token})
    except transport.MarkingError as error:
        # Непринятые реквизиты СУЗ отвечает четырёхсотым, а не 401 — проверено
        # обращением: `ping` без токена возвращает 400 и «Проверка учетных
        # данных УОТ не пройдена». Ждать здесь 401, как от True API, значит
        # показать общий текст про сертификат — которого в СУЗ нет вовсе.
        if error.status in (400, 401, 403):
            raise _refused(error, contour) from None
        raise

    echoed = _oms(answer)
    if echoed and echoed != ready.oms_id:
        # Станция ответила про другой ОМС — значит, токен выдан не той, чей
        # идентификатор вписан. Промолчать нельзя: заказ уйдёт не туда.
        raise transport.MarkingError(
            f"СУЗ ответила про другой ОМС ID: {echoed}. Вписанный "
            f"{ready.oms_id} этой станции не принадлежит.")
    return f"Соединение с СУЗ установлено · ОМС {ready.oms_id} · {contour.title}"


def _refused(error: transport.MarkingError,
             contour: Contour) -> transport.MarkingError:
    """Отказ СУЗ → объяснение, что с этим делать.

    Ответ системы приводится дословно и целиком: в нём код ошибки, по которому
    в поддержке и разбираются, а пересказ его потерял бы.
    """
    return transport.MarkingError(
        "СУЗ не приняла реквизиты. Проверьте, что ОМС ID, идентификатор "
        "соединения и токен взяты из личного кабинета того же контура "
        f"({contour.title.lower()}) и что устройство в нём не отключено."
        f"\n\nОтвет системы: {error}", error.status)


def _oms(answer: Any) -> str:
    if isinstance(answer, dict):
        for name in ("omsId", "oms_id", "omsID"):
            if value := answer.get(name):
                return str(value).strip()
    return ""
