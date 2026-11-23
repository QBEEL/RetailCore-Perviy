"""Частичное изъятие кодов единиц товара из транспортной упаковки."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from . import crypto, introduce, service, store, transport
from .models import (
    BATCH_SIZE, CodeInfo, CodeState, Contour, Operation, OperationKind, OperationStatus, same_group,
)

DOCUMENT_TYPE = "REAGGREGATION_DOCUMENT"


def _value(info: CodeInfo, name: str) -> str:
    raw = info.raw if isinstance(info.raw, dict) else {}
    inner = raw.get("cisInfo")
    value = inner.get(name) if isinstance(inner, dict) and name in inner else raw.get(name)
    return str(value or "").strip()


def blocked_items(items: Iterable[CodeInfo]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in items:
        if item.found and item.valid and item.code and (parent := item.parent_code):
            codes = result.setdefault(parent, [])
            if item.code not in codes:
                codes.append(item.code)
    return result


@dataclass(frozen=True, slots=True)
class Removal:
    inn: str
    group: str
    package: str
    codes: tuple[str, ...]

    wire_format = "MANUAL"

    def wire_type(self, group: str) -> str:
        if group != self.group:
            raise introduce.IntroduceProblem("Товарная группа упаковки изменилась")
        return DOCUMENT_TYPE

    def render(self, extension: str = ".json") -> str:
        return json.dumps({
            "participant_inn": self.inn,
            "reaggregation_type": "REMOVING",
            "uitu": self.package,
            "uit_uitu_list": [{"uit_uitu": code} for code in self.codes],
        }, ensure_ascii=False)


def prepare(package: str, codes: tuple[str, ...], inn: str, contour: Contour) -> Removal:
    """Проверяет принадлежность выбранных кодов и состав КИТУ перед подписью."""
    from .sets import SUPPORTED_SET_GROUPS

    selected = set(codes)
    if (
        not package or not codes or package in selected or len(selected) != len(codes)
        or len(codes) > introduce.MAX_PRODUCTS
    ):
        raise ValueError("Для изъятия нужны уникальные коды вложений одной упаковки")
    if not service.signed_in() or service.current().contour is not contour or service.current().inn != inn:
        raise ValueError("Войдите в ЧЗ под нужной организацией и в нужном контуре")
    fresh = {item.code: item for item in service.check_exact((package, *codes), contour)}
    box = fresh.get(package)
    if not box or not box.found or not box.valid:
        raise ValueError("ЧЗ не подтвердил сведения об упаковке")
    if box.package_type != "BOX":
        raise ValueError("Частичное изъятие доступно для транспортной упаковки (КИТУ)")
    if not inn or box.owner_inn.strip() != inn:
        raise ValueError("ЧЗ не подтвердил, что транспортная упаковка принадлежит вашей организации")
    if box.product_group not in SUPPORTED_SET_GROUPS:
        raise ValueError("Товарная группа упаковки не определена или не поддерживается")
    if box.state not in (CodeState.APPLIED, CodeState.INTRODUCED):
        raise ValueError(f"Статус упаковки «{box.state.title}» не подходит для изъятия")

    children = set(box.children)
    if not children.issuperset(selected):
        raise ValueError("ЧЗ не подтвердил, что все выбранные коды есть в составе упаковки")
    remaining = [code for code in box.children if code not in selected]
    if not remaining:
        raise ValueError("Изъятие всех вложений расформирует упаковку. Для этого используйте кабинет ЧЗ")

    for code in codes:
        item = fresh.get(code)
        if not item or not item.found or not item.valid or item.parent_code != package:
            raise ValueError(f"ЧЗ не подтвердил привязку к упаковке для кода {code}")
        if item.package_type not in ("UNIT", "LEVEL0", "CIS"):
            raise ValueError(f"Код {code} не подтверждён как единица товара")
        if item.owner_inn.strip() != inn:
            raise ValueError(f"ЧЗ не подтвердил право вашей организации на изъятие кода {code}")
        special = _value(item, "statusEx") or _value(item, "statusExt")
        if item.state is not box.state or special.upper() not in ("", "EMPTY"):
            raise ValueError(f"Статус или особое состояние кода {code} не подходит для изъятия")
        if item.state is CodeState.APPLIED and item.emission_type in ("", "REMARK", "REAPPLY"):
            raise ValueError(f"Тип эмиссии кода {code} не подходит для изъятия")

    for index in range(0, len(remaining), BATCH_SIZE):
        if any(
            item.found and item.valid and item.parent_code == package
            and same_group(item.product_group, box.product_group)
            for item in service.check_exact(remaining[index:index + BATCH_SIZE], contour)
        ):
            return Removal(inn, box.product_group, package, codes)
    raise ValueError("ЧЗ не подтвердил, что в упаковке останется код её товарной группы")


def send(package: str, codes: tuple[str, ...], inn: str, thumbprint: str,
         contour: Contour) -> Operation:
    document = prepare(package, codes, inn, contour)
    current = service.current()
    if not service.signed_in() or (current.contour, current.inn, current.thumbprint) != (
        contour, inn, thumbprint,
    ):
        raise ValueError("Сеанс ЧЗ сменился. Повторите проверку под нужным сертификатом")
    operation = store.create(Operation(
        kind=OperationKind.REAGGREGATION, status=OperationStatus.SENDING,
        contour=contour, product_group=document.group, reason=package,
        codes=[package, *codes], certificate=current.owner,
        comment=f"Изъятие вложений: {len(codes)}",
    ))

    def created(doc_id: str) -> None:
        operation.document_id = doc_id
        operation.status = OperationStatus.SENT
        operation.sent_at = datetime.now()
        store.update(operation)

    try:
        answer = introduce.send(document, thumbprint, document.group, contour, on_created=created)
    except (introduce.IntroduceProblem, crypto.CryptoUnavailable, crypto.SigningFailed) as error:
        operation.status = OperationStatus.FAILED
        operation.error = str(error)
        return store.update(operation)
    except transport.MarkingError as error:
        if operation.document_id:
            return operation
        if error.status in (400, 401, 403, 404, 422, 429):
            operation.status = OperationStatus.FAILED
        operation.error = str(error)
        return store.update(operation)

    status = answer.status
    state = (
        OperationStatus.DONE if status.done else
        OperationStatus.REJECTED if status.failed else OperationStatus.SENT
    )
    return store.update_document_status(
        operation.id, operation.document_id, state, "" if status.done else status.text, datetime.now()
    ) or operation
