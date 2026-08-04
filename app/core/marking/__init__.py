"""Маркировка: разбор кодов DataMatrix, подпись и работа с Честным ЗНАКом.

Систем на той стороне две: документами оборота заведует ГИС МТ, а кодами —
заказом, эмиссией, проверкой — True API. Адреса и токены у них разные, поэтому
`transport` выбирает систему явно на каждом запросе.

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
    "GROUPS",
    "Operation",
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
