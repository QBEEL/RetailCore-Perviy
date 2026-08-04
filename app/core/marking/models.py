"""Маркировка: контуры, товарные группы, операции и их состояния.

Операция здесь — не вызов функции, а запись, переживающая перезапуск. ГИС МТ
отвечает «принято», а обработала она документ или отвергла, выясняется позже
опросом. Пока ответа нет, операция висит в состоянии «отправлена» — и это
самое опасное состояние из всех: повторная отправка того же набора кодов
означает второй вывод товара из оборота. Поэтому после перезапуска такую
операцию положено опрашивать, а не отправлять заново.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

# Ограничения API. Не «рекомендации по оптимизации», а потолок, вокруг которого
# строится вся отправка: пачка кодов делится на куски, куски идут с оглядкой на
# секундный лимит.
#
# Тысяча кодов на запрос — проверено ответом сервера: на пяти тысячах он
# отвечает «Слишком много КИ в запросе. Количество не должно превышать 1000».
BATCH_SIZE = 1000

# Полсотни запросов в секунду от одного участника оборота — из описания True API.
REQUESTS_PER_SECOND = 50


class Contour(str, Enum):
    """Где работаем. Песочница полностью повторяет боевой контур, но с
    ненастоящими данными, и ошибка там ничего не стоит."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"

    @property
    def title(self) -> str:
        return "Песочница" if self is Contour.SANDBOX else "Боевой контур"

    @property
    def safe(self) -> bool:
        return self is Contour.SANDBOX


@dataclass(frozen=True, slots=True)
class ProductGroup:
    """Товарная группа.

    Хранится данными, а не константами в коде: групп два десятка, состав
    меняется распоряжениями правительства, и каждая новая не должна требовать
    правки модуля. `code` — то, что уходит в параметр `pg` запроса.
    """

    code: str
    title: str

    def __str__(self) -> str:
        return self.title


# Группы, интересные этому ассортименту. Список неполный намеренно: сюда
# дописывается то, чем действительно торгуют, а не весь перечень ЦРПТ.
GROUPS: tuple[ProductGroup, ...] = (
    ProductGroup("perfumery", "Духи и туалетная вода"),
    ProductGroup("cosmetics", "Косметика и бытовая химия"),
    ProductGroup("lp", "Товары лёгкой промышленности"),
    ProductGroup("shoes", "Обувные товары"),
    ProductGroup("otp", "Табачная продукция"),
    ProductGroup("milk", "Молочная продукция"),
    ProductGroup("water", "Упакованная вода"),
    ProductGroup("biologically_active_food_supplements", "Биологически активные добавки"),
    ProductGroup("antiseptic", "Антисептики"),
)

GROUP_BY_CODE: dict[str, ProductGroup] = {group.code: group for group in GROUPS}


def group_of(code: Any) -> ProductGroup | None:
    if isinstance(code, ProductGroup):
        return code
    return GROUP_BY_CODE.get(str(code or ""))


class OperationKind(str, Enum):
    """Что делаем. Проверка отделена от остальных: она ничего не меняет."""

    CHECK = "check"
    ACCEPT = "accept"
    SHIP = "ship"
    WITHDRAWAL = "withdrawal"
    REMARK = "remark"

    @property
    def title(self) -> str:
        return KIND_TITLES[self]

    @property
    def read_only(self) -> bool:
        """Меняет ли операция что-нибудь в ГИС МТ."""
        return self is OperationKind.CHECK

    @property
    def reversible(self) -> bool:
        """Можно ли отыграть назад штатными средствами.

        У вывода из оборота отмена предусмотрена не для всех причин, а
        перемаркировка порождает новый код взамен старого — оба помечены
        необратимыми, и интерфейс обязан спрашивать подтверждение.
        """
        return self in (OperationKind.CHECK, OperationKind.ACCEPT, OperationKind.SHIP)


KIND_TITLES: dict[OperationKind, str] = {
    OperationKind.CHECK: "Проверка кодов",
    OperationKind.ACCEPT: "Приёмка",
    OperationKind.SHIP: "Отгрузка",
    OperationKind.WITHDRAWAL: "Вывод из оборота",
    OperationKind.REMARK: "Перемаркировка",
}


class OperationStatus(str, Enum):
    """Состояние операции.

    `SENT` — документ принят ГИС МТ, но результат неизвестен. Из этого
    состояния выходят только опросом; повторная отправка запрещена.
    """

    DRAFT = "draft"
    SENDING = "sending"
    SENT = "sent"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def title(self) -> str:
        return STATUS_TITLES[self]

    @property
    def final(self) -> bool:
        return self in (OperationStatus.DONE, OperationStatus.REJECTED)

    @property
    def pending(self) -> bool:
        """Ждёт ответа ГИС МТ — такие операции опрашиваются при открытии вкладки."""
        return self in (OperationStatus.SENDING, OperationStatus.SENT)

    @property
    def resendable(self) -> bool:
        """Можно ли отправить заново.

        Только то, что заведомо не доехало. `SENDING` сюда не входит: обрыв на
        отправке не означает, что сервер её не принял, и повтор вслепую — это
        второй вывод товара из оборота.
        """
        return self in (OperationStatus.DRAFT, OperationStatus.FAILED)


STATUS_TITLES: dict[OperationStatus, str] = {
    OperationStatus.DRAFT: "Черновик",
    OperationStatus.SENDING: "Отправляется",
    OperationStatus.SENT: "Ждёт обработки",
    OperationStatus.DONE: "Выполнена",
    OperationStatus.REJECTED: "Отклонена",
    OperationStatus.FAILED: "Не отправлена",
}


class CodeState(str, Enum):
    """Состояние кода маркировки в ГИС МТ.

    Значения ГИС МТ приходят строками и по группам различаются, поэтому
    перечисление своё, а сопоставление живёт в `parse_state`. Неизвестное
    значение становится `UNKNOWN`, а не ломает разбор ответа: система
    добавляет состояния, и приложение обязано это переживать.
    """

    EMITTED = "emitted"
    APPLIED = "applied"
    INTRODUCED = "introduced"
    WRITTEN_OFF = "written_off"
    RETIRED = "retired"
    DISAGGREGATED = "disaggregated"
    UNKNOWN = "unknown"

    @property
    def title(self) -> str:
        return CODE_STATE_TITLES[self]

    @property
    def sellable(self) -> bool:
        return self is CodeState.INTRODUCED


CODE_STATE_TITLES: dict[CodeState, str] = {
    CodeState.EMITTED: "Эмитирован",
    CodeState.APPLIED: "Нанесён",
    CodeState.INTRODUCED: "В обороте",
    CodeState.WRITTEN_OFF: "Списан",
    CodeState.RETIRED: "Выведен из оборота",
    CodeState.DISAGGREGATED: "Расформирован",
    CodeState.UNKNOWN: "Неизвестно",
}

# Как значения ГИС МТ ложатся на перечисление. Регистр приводится к нижнему:
# в разных группах одно и то же состояние приходит по-разному.
_STATE_ALIASES: dict[str, CodeState] = {
    "emitted": CodeState.EMITTED,
    "applied": CodeState.APPLIED,
    "introduced": CodeState.INTRODUCED,
    "introduced_into_circulation": CodeState.INTRODUCED,
    "in_circulation": CodeState.INTRODUCED,
    "written_off": CodeState.WRITTEN_OFF,
    "retired": CodeState.RETIRED,
    "withdrawn": CodeState.RETIRED,
    "withdrawn_from_circulation": CodeState.RETIRED,
    "disaggregated": CodeState.DISAGGREGATED,
    "disaggregation": CodeState.DISAGGREGATED,
}


def parse_state(value: Any) -> CodeState:
    return _STATE_ALIASES.get(str(value or "").strip().lower(), CodeState.UNKNOWN)


@dataclass(slots=True)
class CodeInfo:
    """Что ГИС МТ знает о коде. Ответ проверки, сложенный в свой тип."""

    code: str = ""
    gtin: str = ""
    serial: str = ""
    state: CodeState = CodeState.UNKNOWN
    valid: bool = False
    found: bool = False
    owner_inn: str = ""
    owner_name: str = ""
    product_name: str = ""
    expires_at: datetime | None = None
    problems: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    # ИНН, с которым сравнивается владелец кода. Берётся из сертификата, под
    # которым выполнен вход, и заполняется при разборе ответа.
    our_inn: str = ""

    @property
    def ours(self) -> bool:
        """Наш ли это код. Чужой в приёмке — повод остановиться и разобраться."""
        return bool(self.owner_inn) and self.our_inn.strip() == self.owner_inn.strip()

    @property
    def title(self) -> str:
        return self.product_name or f"{self.gtin} · {self.serial}"


@dataclass(slots=True)
class Operation:
    """Операция с кодами — то, что переживает перезапуск приложения."""

    id: int = 0
    # Собственный идентификатор, присвоенный до отправки. По нему операция
    # узнаётся, если ответ не доехал: повтор запрещён, состояние выясняется
    # опросом.
    local_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: OperationKind = OperationKind.CHECK
    product_group: str = ""
    contour: Contour = Contour.SANDBOX
    status: OperationStatus = OperationStatus.DRAFT
    # Идентификатор документа в ГИС МТ. Появляется после приёма и служит
    # ключом опроса.
    document_id: str = ""
    codes: list[str] = field(default_factory=list)
    reason: str = ""
    comment: str = ""
    error: str = ""
    author: str = ""
    certificate: str = ""
    created_at: datetime | None = None
    sent_at: datetime | None = None
    checked_at: datetime | None = None

    @property
    def group(self) -> ProductGroup | None:
        return group_of(self.product_group)

    @property
    def size(self) -> int:
        return len(self.codes)

    @property
    def batches(self) -> int:
        """На сколько запросов разойдётся операция при отправке."""
        return (len(self.codes) + BATCH_SIZE - 1) // BATCH_SIZE

    @property
    def title(self) -> str:
        group = self.group
        where = "" if self.contour is Contour.PRODUCTION else " · песочница"
        return (f"{self.kind.title} · {self.size} кодов"
                f"{f' · {group.title}' if group else ''}{where}")

    @property
    def dangerous(self) -> bool:
        """Требует явного подтверждения перед отправкой."""
        return not self.kind.reversible and self.contour is Contour.PRODUCTION


def fingerprint(codes: Iterable[str]) -> str:
    """Отпечаток набора кодов — защита от повторной отправки того же самого.

    Считается по отсортированному множеству: порядок сканирования значения не
    имеет, а вот повтор набора значит, что операцию запускают второй раз.
    Сравнивать списки целиком нельзя — в базе они лежат текстом, и один
    переставленный код сделал бы наборы «разными».
    """
    unique = sorted({str(code).strip() for code in codes if str(code).strip()})
    digest = hashlib.sha256("\n".join(unique).encode("utf-8")).hexdigest()
    return digest[:32]
