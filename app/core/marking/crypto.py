"""Подпись данных сертификатом через КриптоПро.

Честный ЗНАК не пускает по паролю: сервер выдаёт случайную строку, её нужно
подписать усиленной квалифицированной подписью и вернуть — в обмен на токен.
Поэтому подпись здесь не «дополнительная возможность», а единственный способ
вообще получить доступ.

Работа идёт через COM-объекты КриптоПро (`CAdESCOM`), те же самые, что
использует плагин для браузера. Своей реализации ГОСТ-подписи здесь нет и быть
не может: закрытый ключ живёт в защищённом хранилище и наружу не выдаётся —
это и есть смысл всей конструкции.

Модуль работает только на Windows с установленным КриптоПро CSP. На других
системах он честно не запускается, а не притворяется работающим.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Константы CAPICOM и CAdESCOM. В COM они доступны только числами — имена
# существуют лишь в документации, поэтому вынесены сюда с пояснениями.
_CURRENT_USER_STORE = 2
_MY_STORE = "My"
_OPEN_READ_ONLY = 0
_CADES_BES = 1
_BASE64_TO_BINARY = 1


class CryptoUnavailable(RuntimeError):
    """КриптоПро не установлен или недоступен. Текст пригоден для показа."""


class SigningFailed(RuntimeError):
    """Подписать не удалось: нет ключа, отменён ввод пароля, истёк сертификат."""


@dataclass(slots=True)
class Certificate:
    """Сертификат из хранилища пользователя."""

    thumbprint: str = ""
    subject: str = ""
    issuer: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    has_private_key: bool = False

    @property
    def owner(self) -> str:
        """ФИО или название организации — то, что стоит показать в списке."""
        return _field(self.subject, "CN") or self.subject

    @property
    def inn(self) -> str:
        return _field(self.subject, "ИНН") or _field(self.subject, "ИННЮЛ")

    @property
    def expired(self) -> bool:
        return bool(self.valid_to and self.valid_to.timestamp()
                    < datetime.now().timestamp())

    @property
    def usable(self) -> bool:
        return self.has_private_key and not self.expired

    @property
    def title(self) -> str:
        until = f" · до {self.valid_to:%d.%m.%Y}" if self.valid_to else ""
        mark = "" if self.usable else " · непригоден"
        return f"{self.owner}{until}{mark}"


def _field(subject: str, name: str) -> str:
    """Достаёт поле из строки вида `CN=Иванов, ИНН=1234567890`."""
    for part in subject.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().upper() == name.upper():
            return value.strip().strip('"')
    return ""


def _dispatch(name: str) -> Any:
    try:
        import win32com.client
    except ImportError as error:
        raise CryptoUnavailable(
            "Не установлен пакет pywin32 — без него подпись недоступна."
        ) from error
    try:
        return win32com.client.Dispatch(name)
    except Exception as error:  # noqa: BLE001 — COM бросает что угодно
        raise CryptoUnavailable(
            "КриптоПро CSP не найден. Установите его и перезапустите "
            f"приложение.\n\nПодробности: {error}") from error


def available() -> bool:
    """Есть ли на машине КриптоПро. Проверяется без обращения к ключам."""
    try:
        _dispatch("CAdESCOM.Store")
        return True
    except CryptoUnavailable:
        return False


def certificates() -> list[Certificate]:
    """Личные сертификаты пользователя, пригодные и нет.

    Непригодные тоже возвращаются: человеку понятнее увидеть свой просроченный
    сертификат с пометкой, чем пустой список и вопрос «а куда он делся».
    """
    store = _dispatch("CAdESCOM.Store")
    store.Open(_CURRENT_USER_STORE, _MY_STORE, _OPEN_READ_ONLY)
    try:
        found = store.Certificates
        return [_certificate(found.Item(index))
                for index in range(1, found.Count + 1)]
    finally:
        store.Close()


def _certificate(item: Any) -> Certificate:
    return Certificate(
        thumbprint=str(item.Thumbprint),
        subject=str(item.SubjectName),
        issuer=str(item.IssuerName),
        valid_from=_moment(item.ValidFromDate),
        valid_to=_moment(item.ValidToDate),
        has_private_key=bool(item.HasPrivateKey()),
    )


def _moment(value: Any) -> datetime | None:
    try:
        # COM отдаёт время с зоной; приложение живёт в местном и о зонах не знает.
        moment = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def find(thumbprint: str) -> Certificate | None:
    """Сертификат по отпечатку — им он и запоминается в настройках."""
    wanted = (thumbprint or "").replace(" ", "").upper()
    for certificate in certificates():
        if certificate.thumbprint.replace(" ", "").upper() == wanted:
            return certificate
    return None


def sign(data: str, thumbprint: str) -> str:
    """Отсоединённая подпись CAdES-BES над строкой. Возвращает base64.

    `data` — то, что прислал сервер: строка, которую он ждёт подписанной.
    Подписывается её содержимое, а не base64-представление, поэтому перед
    подписью оно кодируется, а COM-объекту сообщается, что вход — base64.
    """
    certificate = find(thumbprint)
    if certificate is None:
        raise SigningFailed(
            "Сертификат не найден. Возможно, его удалили или подключён другой "
            "носитель ключа.")
    if certificate.expired:
        raise SigningFailed(
            f"Сертификат {certificate.owner} действовал до "
            f"{certificate.valid_to:%d.%m.%Y} и больше не годится.")
    if not certificate.has_private_key:
        raise SigningFailed(
            f"У сертификата {certificate.owner} нет закрытого ключа — "
            "подписать им нельзя. Подключите носитель.")

    store = _dispatch("CAdESCOM.Store")
    store.Open(_CURRENT_USER_STORE, _MY_STORE, _OPEN_READ_ONLY)
    try:
        found = store.Certificates.Find(0, certificate.thumbprint)
        if found.Count == 0:
            raise SigningFailed("Сертификат исчез из хранилища во время работы.")
        signer = _dispatch("CAdESCOM.CPSigner")
        signer.Certificate = found.Item(1)
        # Ноль означает «не вкладывать цепочку сертификатов». ГИС МТ ждёт
        # минимальную подпись; лишние вложения она отвергает.
        signer.Options = 0

        signed = _dispatch("CAdESCOM.CadesSignedData")
        signed.ContentEncoding = _BASE64_TO_BINARY
        signed.Content = base64.b64encode(data.encode("utf-8")).decode()

        try:
            signature = signed.SignCades(signer, _CADES_BES, True)
        except Exception as error:  # noqa: BLE001 — COM бросает что угодно
            raise SigningFailed(_explain(error)) from error
    finally:
        store.Close()

    # Переводы строк в base64 добавляет сама библиотека; в заголовке запроса
    # они недопустимы.
    return signature.replace("\r", "").replace("\n", "")


def _explain(error: Exception) -> str:
    """Ошибка COM → текст, из которого понятно, что делать."""
    text = str(error)
    if "0x800704C7" in text or "отменен" in text.lower():
        return "Ввод пароля к ключу отменён — подпись не создана."
    if "0x8009000D" in text:
        return ("Закрытый ключ недоступен. Проверьте, вставлен ли носитель "
                "(Рутокен, флешка) и не сменился ли он.")
    if "0x80090010" in text:
        return "Доступ к ключу запрещён — неверный пароль к контейнеру."
    return f"Не удалось подписать: {text}"
