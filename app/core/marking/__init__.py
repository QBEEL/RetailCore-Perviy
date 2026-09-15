"""Маркировка: разбор кодов DataMatrix, подпись и работа с Честным ЗНАКом.

Систем на той стороне три: документами оборота заведует ГИС МТ, проверкой кодов
— True API, а выдачей кодов — СУЗ. Адреса и токены у них разные, поэтому
`transport` выбирает систему явно на каждом запросе. Вход тоже разный: в первые
две входят по сертификату, а СУЗ узнаёт устройство по постоянным реквизитам —
ОМС ID, идентификатору соединения и токену из личного кабинета.

Операция здесь — запись в базе, а не вызов функции: ГИС МТ отвечает «принято»,
а результат выясняется опросом, и операция обязана пережить перезапуск. Пока
ответа нет, повторная отправка запрещена — это был бы второй вывод из оборота.
"""
from __future__ import annotations

from .codes import (
    Batch,
    Code,
    CodeProblem,
    gtin_valid,
    normalize,
    parse,
    parse_many,
    split_lines,
)
from .session import Organisation
from .suz import Credentials
from .models import (
    BATCH_SIZE,
    REQUESTS_PER_SECOND,
    CodeInfo,
    CodeState,
    Contour,
    GROUPS,
    Operation,
    OperationKind,
    OperationStatus,
    ProductGroup,
    fingerprint,
    group_of,
    parse_state,
)

__all__ = [
    "BATCH_SIZE",
    "Batch",
    "Code",
    "CodeInfo",
    "CodeProblem",
    "CodeState",
    "Contour",
    "Credentials",
    "GROUPS",
    "Operation",
    "Organisation",
    "OperationKind",
    "OperationStatus",
    "ProductGroup",
    "REQUESTS_PER_SECOND",
    "fingerprint",
    "group_of",
    "gtin_valid",
    "normalize",
    "parse",
    "parse_many",
    "parse_state",
    "split_lines",
]
