"""Отчёт о нанесении КИН набора через API СУЗ.

Руководство СУЗ 3.0, разделы 4.4.11 и 4.4.13. Отчёт отправляется один раз;
последующие проверки используют только идентификатор полученного отчёта.
"""
from __future__ import annotations

import json
from uuid import UUID

from . import codes, crypto, transport
from .issue import Batch
from .models import Contour, same_group
from .suz import Credentials, TOKEN_HEADER, apply

SEND_PATH = "/utilisation"
STATUS_PATH = "/report/info"
MAX_CODES = 30_000
PENDING = frozenset({"DRAFT", "PENDING", "READY_TO_SEND", "SENT", "CHECK"})
FAILED = frozenset({"REJECTED", "FAILED", "PARTIALLY"})
DONE = "SUCCESS"
STATUS_TITLES = {
    "SENDING": "Отправляется",
    "UNKNOWN": "Исход неизвестен",
    "DRAFT": "Черновик в СУЗ",
    "PENDING": "Ожидает обработки",
    "READY_TO_SEND": "Готов к отправке",
    "SENT": "Отправлен",
    "CHECK": "Проверяется",
    "SUCCESS": "Нанесён",
    "REJECTED": "Отклонён",
    "FAILED": "Ошибка обработки",
    "PARTIALLY": "Частично обработан",
    "APPLIED_EXTERNAL": "Нанесён (проверено в ЧЗ)",
}


def _uuid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return ""


def payload(batch: Batch) -> dict:
    """Проверяет блок до подписи и собирает тело отчёта только для КИН набора."""
    if not same_group(batch.product_group, "cosmetics"):
        raise ValueError("Отчёт о нанесении наборов поддержан для косметики и бытовой химии.")
    if batch.cis_type.upper() != "SET":
        raise ValueError("Тип кодов в сохранённом блоке не подтверждён как SET.")
    if not 1 <= batch.total <= MAX_CODES:
        raise ValueError(f"В одном отчёте должно быть от 1 до {MAX_CODES} кодов.")
    full: list[str] = []
    seen: set[str] = set()
    for raw in batch.codes:
        item = codes.parse(raw)
        if not item.valid or not item.full or item.gtin != batch.gtin:
            raise ValueError("В блоке есть КИН без полного кода с проверочной частью.")
        if item.ki in seen:
            raise ValueError("В блоке повторяются КИН набора.")
        seen.add(item.ki)
        full.append(item.full)
    return {"productGroup": "chemistry", "sntins": full}


def send(batch: Batch, credentials: Credentials, thumbprint: str,
         contour: Contour) -> str:
    """Подписывает и однократно отправляет отчёт; возвращает только проверенный reportId."""
    if batch.application_status != "SENDING" or batch.application_report_id:
        raise ValueError("Отчёт не подготовлен к первой отправке или уже отправлен.")
    body = payload(batch)
    if batch.contour != contour.value:
        raise ValueError("Контур блока не совпадает с выбранным контуром СУЗ.")
    if not credentials.stripped().filled or not thumbprint.strip():
        raise ValueError("Нужны реквизиты СУЗ и сертификат для подписи отчёта.")
    ready = credentials.stripped()
    apply(ready, contour)
    signed = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    signature = crypto.sign(signed, thumbprint, detached=True)
    answer = transport.post(
        "suz", SEND_PATH, contour=contour, params={"omsId": ready.oms_id},
        headers={TOKEN_HEADER: ready.token, "X-Signature": signature},
        text_body=signed, retries=0,
    )
    report_id = _uuid(answer.get("reportId")) if isinstance(answer, dict) else ""
    if not report_id or _uuid(answer.get("omsId")) != _uuid(ready.oms_id):
        raise transport.MarkingError(
            "СУЗ ответила на отчёт, но не подтвердила его идентификатор. "
            "Проверьте отчёты в СУЗ перед любым повтором."
        )
    return report_id


def status(report_id: str, credentials: Credentials,
           contour: Contour) -> tuple[str, str]:
    """Читает только статус указанного отчёта, не отправляя его повторно."""
    wanted = _uuid(report_id)
    if not wanted:
        raise ValueError("Не сохранён идентификатор отчёта о нанесении.")
    ready = credentials.stripped()
    if not ready.filled:
        raise ValueError("Не заполнены реквизиты СУЗ.")
    apply(ready, contour)
    answer = transport.get(
        "suz", STATUS_PATH, contour=contour,
        params={"omsId": ready.oms_id, "reportId": wanted},
        headers={TOKEN_HEADER: ready.token},
    )
    if not isinstance(answer, dict) or _uuid(answer.get("reportId")) != wanted \
            or _uuid(answer.get("omsId")) != _uuid(ready.oms_id):
        raise transport.MarkingError("СУЗ не вернула статус запрошенного отчёта.")
    state = str(answer.get("reportStatus") or "").upper()
    if state not in PENDING | FAILED | {DONE}:
        raise transport.MarkingError("СУЗ вернула незнакомый статус отчёта о нанесении.")
    return state, str(answer.get("errorReason") or "")
