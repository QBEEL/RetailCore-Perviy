"""Отправка УПД покупателю через «ЭДО Лайт» и судьба документа.

«ЭДО Лайт» — оператор ЭДО самого ЦРПТ. Он выбран потому, что входит в него тот
же сертификат и тот же токен, что и в «Честный ЗНАК»: `/auth/key` →
подпись → `/auth/simpleSignIn` (документация API ЭДО Лайт, раздел
«Аутентификация»). Ни договора с другим оператором, ни ключа разработчика не
нужно. Покупатель при этом может сидеть в любом ЭДО: документ уходит ему в
роуминг, если между организациями настроена связь.

Как уходит документ (`POST /api/v1/outgoing-documents`, форма
`multipart/form-data`):

- `content` — файл УПД под его собственным именем: имя обязано совпасть с
  `ИдФайл`, а получателя оператор берёт из идентификатора в нём;
- `signature` — открепленная подпись файла в base64. С ней документ сразу
  «Отправлен», без неё остался бы черновиком в личном кабинете;
- заголовок `send_mchd_file: true`, если подписывает представитель: оператор
  найдёт МЧД по полю «МЧД» в `ИнфПолФХЖ1` и приложит её к пакету.

Повтор безопаснее, чем у документов ГИС МТ: при загрузке файла с тем же
именем оператор возвращает идентификатор уже загруженного, а не создаёт второй.
Поэтому оборванный ответ здесь лечится повторной отправкой того же самого
документа (`sale.Sale` с тем же `guid`), а не собранного заново.

Статус узнаётся из списка исходящих документов: отдельного метода «статус по
идентификатору» у оператора нет. Коды выводятся из оборота не при отправке, а
когда покупатель подпишет документ и оператор передаст его в ГИС МТ (статус
61). Поэтому итог продажи проверяется ещё и по самим кодам.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from .. import appdata
from . import crypto, session, transport
from .models import CodeState, Contour, OperationStatus
from .sale import Sale, SaleProblem

UPLOAD_PATH = "/outgoing-documents"
LIST_PATH = "/outgoing-documents"

# Справочник «Статусы документов» документации API ЭДО Лайт — как статус
# исходящего документа звучит для того, кто его отправил.
STATUS_TITLES: dict[int, str] = {
    0: "Черновик — не отправлен",
    1: "Отправлен",
    2: "Доставлен покупателю",
    3: "Доставлен, ждёт подписи покупателя",
    4: "Подписан покупателем",
    5: "Покупатель отказался подписать",
    7: "Уточнён исправленным документом",
    8: "Покупатель просит уточнить документ",
    11: "Уходит в роуминг к оператору покупателя",
    12: "Просмотрен покупателем",
    13: "Просмотрен покупателем, ждёт подписи",
    14: "Запрос на уточнение просмотрен",
    15: "Покупатель отказался подписать",
    16: "Ожидается аннулирование",
    17: "Требуется аннулирование",
    18: "Аннулирован",
    19: "Отказано в аннулировании",
    41: "Ошибка отправки",
    42: "Ошибка отправки на стороне оператора",
    43: "Оператор покупателя не нашёл связанный документ",
    44: "Ошибка у оператора покупателя",
    61: "Подписан покупателем и передан в «Честный ЗНАК»",
    62: "Подписан, но «Честный ЗНАК» его не принял",
    63: "Подписан, передаётся в «Честный ЗНАК»",
    64: "Аннулирован, передано в «Честный ЗНАК»",
    65: "Аннулирован, но «Честный ЗНАК» не принял",
    66: "Аннулирован, передаётся в «Честный ЗНАК»",
}
# Передан в ГИС МТ — дальше коды выбывают на её стороне.
DELIVERED = frozenset({61})# Итог «не вышло»: отказ покупателя, аннулирование, ошибки доставки и отказ ГИС МТ.
FAILED = frozenset({5, 15, 18, 41, 42, 43, 44, 62, 64, 65, 66})

# Чем помечен в журнале УПД, сохранённый для отправки из Диадока: оператора у
# программы нет, и судьба такого документа узнаётся только по кодам.
MANUAL_PREFIX = "diadoc:"

# Насколько раньше отправки искать документ в списке. Время создания пишет
# сервер, а часы компьютера могут убегать.
LOOKBACK = timedelta(days=1)
PAGE = 100


class EdoUnknown(transport.MarkingError):
    """Ответ на отправку не дошёл: документ мог быть создан, а мог и нет.

    Лечится повторной отправкой того же документа — оператор узнаёт файл по
    имени и второго документа не создаёт.
    """


@dataclass(frozen=True, slots=True)
class EdoStatus:
    """Что оператор знает о документе."""

    code: int | None = None
    found: bool = True
    # Отправлен из Диадока руками: статуса оператора нет, есть только коды.
    manual: bool = False

    @property
    def title(self) -> str:
        if self.manual:
            return ("Покупатель подписал, коды выбыли" if self.delivered
                    else "Отправляется из Диадока — ждём подписи покупателя")
        if not self.found:
            return "не найден в «ЭДО Лайт»"
        if self.code is None:
            return "нет данных"
        return STATUS_TITLES.get(self.code, f"статус {self.code}")

    @property
    def delivered(self) -> bool:
        """Покупатель подписал, документ ушёл в ГИС МТ."""
        return self.code in DELIVERED

    @property
    def failed(self) -> bool:
        return self.code in FAILED

    @property
    def pending(self) -> bool:
        return not self.delivered and not self.failed


@dataclass(frozen=True, slots=True)
class Outcome:
    """Итог опроса: статус у оператора и, если он уже передан в ГИС МТ, — выбыли ли коды."""

    status: EdoStatus
    retired: int = 0
    total: int = 0

    @property
    def withdrawn(self) -> bool:
        return self.status.delivered and self.total > 0 and self.retired == self.total

    @property
    def operation_status(self) -> OperationStatus:
        if self.withdrawn:
            return OperationStatus.DONE
        if self.status.failed:
            return OperationStatus.REJECTED
        return OperationStatus.SENT

    @property
    def text(self) -> str:
        if self.status.manual:
            if self.withdrawn:
                return f"Диадок: коды выведены из оборота ({self.total})"
            tail = f"; выведено {self.retired} из {self.total}" if self.retired else ""
            return f"{self.status.title}{tail}"
        if self.withdrawn:
            return f"{self.status.title}; коды выведены из оборота ({self.total})"
        if self.status.delivered and self.total:
            return (f"{self.status.title}; выведено из оборота {self.retired} из "
                    f"{self.total} — «Честный ЗНАК» ещё обрабатывает документ")
        return self.status.title


def multipart(fields: Sequence[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """Тело `multipart/form-data`: имя поля, имя файла (или пусто), тип, байты.

    Собирается руками, а не библиотекой: приложение держится на стандартной
    библиотеке, а ей форма с файлом не по силам. Граница — случайная: в байтах
    файла её быть не может.
    """
    boundary = "----RetailCore" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, filename, content_type, data in fields:
        disposition = f'form-data; name="{name}"'
        if filename:
            # Имя файла УПД — латиница, цифры, дефисы и подчёркивания, кавычек
            # в нём не бывает; кириллица в названии поля и файла не нужна.
            disposition += f'; filename="{filename}"'
        head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
        if content_type:
            head += f"Content-Type: {content_type}\r\n"
        parts.append(head.encode("utf-8") + b"\r\n" + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send(document: Sale, thumbprint: str,
         contour: Contour = Contour.SANDBOX, *, draft: bool = False) -> str:
    """Подписывает УПД и отправляет его покупателю. Возвращает идентификатор у оператора.

    Запрос не повторяется сам: решение о повторе принимает человек, и только
    тем же документом (см. `EdoUnknown`).

    `draft` загружает УПД без подписи: он ложится черновиком в «ЭДО Лайт»,
    покупателю ничего не уходит. Так оператор проверяет файл, а человек видит
    документ у себя и может подписать его там же или удалить.
    """
    data = document.render()
    parts = [("content", document.file_name, "application/xml", data)]
    if not draft:
        if not thumbprint.strip():
            raise SaleProblem(
                "Документ нужно подписать, а сертификат не выбран. Выберите его на "
                "вкладке «Проверка кодов».")
        # Подписываются ровно те байты, что уйдут файлом.
        signature = crypto.sign(data, thumbprint, detached=True)
        parts.append(("signature", "", "", signature.encode("ascii")))
    body = multipart(parts)
    # Подписант по доверенности: оператор приложит МЧД из ГИС МТ к пакету, иначе
    # покупатель не увидит, на каком основании подписан документ.
    headers = {"send_mchd_file": "true"} if document.signer.by_poa else None
    try:
        answer = session.authorized(lambda token: transport.post(
            "edo", UPLOAD_PATH, contour=contour, raw_body=body, token=token,
            headers=headers, retries=0))
    except transport.Offline as error:
        raise EdoUnknown(
            "Связь оборвалась, и неизвестно, дошёл ли документ до покупателя. "
            "Отправьте его ещё раз — «ЭДО Лайт» узнает тот же файл и второго "
            f"документа не создаст.\n\nПодробности: {error}") from None
    doc_id = _document_id(answer)
    if not doc_id:
        _remember("Ответ на отправку УПД без идентификатора", answer)
        raise EdoUnknown(
            "«ЭДО Лайт» принял запрос, но не вернул идентификатор документа. "
            "Посмотрите исходящие документы в «ЭДО Лайт»: документ мог быть создан.")
    return doc_id


def _document_id(answer: Any) -> str:
    if isinstance(answer, dict):
        return str(answer.get("id") or "").strip()
    if isinstance(answer, str):
        return answer.strip().strip('"')
    return ""


def status(doc_id: str, contour: Contour = Contour.SANDBOX, *,
           since: datetime | None = None, partner_inn: str = "") -> EdoStatus:
    """Статус исходящего документа — из списка исходящих. Ничего не меняет."""
    params: dict[str, Any] = {"limit": PAGE, "offset": 0, "folder": 0,
                              "sortBy": "created_at", "asc": False,
                              "partner_inn": partner_inn.strip()}
    if since is not None:
        params["created_from"] = int((since - LOOKBACK).timestamp())
    answer = session.authorized(lambda token: transport.get(
        "edo", LIST_PATH, contour=contour, params=params, token=token))
    items = answer.get("items") if isinstance(answer, dict) else None
    if not isinstance(items, list):
        _remember(f"Незнакомый ответ списка исходящих документов ({doc_id})", answer)
        raise transport.MarkingError(
            "«ЭДО Лайт» ответил на запрос списка документов не так, как ожидалось. "
            "Ответ записан в журнал marking.log.")
    for item in items:
        if not isinstance(item, dict):
            continue
        for document in item.get("documents") or []:
            if isinstance(document, dict) and str(document.get("id") or "") == doc_id:
                return EdoStatus(_code(document.get("status")))
        if str(item.get("id") or "") == doc_id:
            return EdoStatus(_code(item.get("status")))
    return EdoStatus(found=False)


def follow(doc_id: str, codes: Sequence[str], contour: Contour = Contour.SANDBOX, *,
           since: datetime | None = None, partner_inn: str = "",
           check: Callable[..., list] | None = None) -> Outcome:
    """Статус у оператора, а для переданного в ГИС МТ — ещё и состояние кодов.

    Статус 61 значит «передан», а не «коды выбыли»: ГИС МТ обрабатывает документ
    сама и может его не принять. Окончательный ответ — в самих кодах.

    УПД, отправленный из Диадока руками (`MANUAL_PREFIX`), у оператора не
    спрашивается: программа его там не видит. Он считается подписанным, когда
    выбыли все его коды.
    """
    manual = doc_id.startswith(MANUAL_PREFIX)
    current = EdoStatus(manual=True) if manual else status(
        doc_id, contour, since=since, partner_inn=partner_inn)
    if not codes or not (manual or current.delivered):
        return Outcome(current)
    if check is None:
        from . import service

        check = service.check
    infos = check(list(codes), contour=contour, use_cache=False)
    retired = sum(1 for info in infos if info.state is CodeState.RETIRED)
    if manual and retired == len(codes):
        current = EdoStatus(next(iter(DELIVERED)), manual=True)
    return Outcome(current, retired=retired, total=len(codes))


def _code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _remember(title: str, answer: Any) -> None:
    try:
        text = json.dumps(answer, ensure_ascii=False, indent=2)[:20000]
    except (TypeError, ValueError):
        text = repr(answer)[:20000]
    appdata.log_event("marking.log", f"{title}:\n{text}")
