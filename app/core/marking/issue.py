"""Получение кодов из заказа СУЗ и их сохранность до печати.

Здесь заканчивается заказ и начинается работа с самими кодами. Устроено всё
вокруг одного факта из руководства СУЗ (раздел 4.4.4): **выданный блок кодов
повторно не выдаётся** тем же методом. Забрал, не успел сохранить — потерял
оплаченные коды. Поэтому:

- коды **записываются на диск до того, как функция вернёт управление**, —
  раньше, чем кто-либо успеет их показать, отправить на принтер или уронить
  приложение;
- запрос на получение **не повторяется** автоматически: оборванный ответ не
  означает «не дошло», и вторая попытка забрала бы второй блок;
- потерянный блок можно вернуть — у СУЗ для этого есть отдельные методы
  `/order/codes/blocks` и `/order/codes/retry`, но только пока заказ не закрыт
  (раздел 4.4.5–4.4.6).

Хранится по файлу на блок в `%APPDATA%\\RetailCore\\marking_codes`. Файл — не
кэш, а единственная копия того, что оплачено, поэтому он пишется через
временный файл и не удаляется сам.

Печать и ввод в оборот работают уже с сохранённым блоком: допечатать
прерванное и повторить файл ввода в оборот можно, не обращаясь к СУЗ.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .. import appdata
from . import transport
from .models import Contour
from .suz import Credentials, TOKEN_HEADER, apply

CODES_PATH = "/codes"
BLOCKS_PATH = "/order/codes/blocks"
RETRY_PATH = "/order/codes/retry"
PRODUCT_PATH = "/order/product"

FOLDER = "marking_codes"

# Куда уезжают удалённые блоки. Не стираются: это оплаченные коды.
TRASH = "deleted"

# Предел одного запроса — из руководства СУЗ, таблица 90.
MAX_PER_REQUEST = 150_000

# Какие ключи в ответе `/order/product` говорят о ТН ВЭД. Десятизначный код
# предпочтительнее: в документе ввода в оборот ждут полный.
_TNVED_KEYS = ("tnVedCode10", "tnVedCode", "tnved", "tnvedCode")
_NAME_KEYS = ("name", "fullName", "productName", "shortName")


class CodesUnknown(transport.MarkingError):
    """Запрос на получение отправлен, а что с кодами — неизвестно.

    Отдельным исключением, потому что лечится единственным способом: вернуть
    потерянный блок методом «Восстановить». Повторять получение нельзя.
    """


@dataclass(slots=True)
class Product:
    """Что СУЗ знает о товаре заказа."""

    gtin: str = ""
    name: str = ""
    tnved: str = ""


@dataclass(slots=True)
class Batch:
    """Блок кодов одного товара, выданный СУЗ и сохранённый на диске."""

    id: str = ""
    created: str = ""
    contour: str = ""
    order_id: str = ""
    block_id: str = ""
    gtin: str = ""
    name: str = ""
    tnved: str = ""
    product_group: str = ""
    release_method: str = ""
    codes: list[str] = field(default_factory=list)
    # Сколько кодов от начала уже ушло на принтер. Нужно, чтобы допечатать
    # прерванное с нужного места, а не с первой этикетки.
    printed: int = 0
    # Когда сформирован файл ввода в оборот или отправлен документ. Пусто — ещё нет.
    introduced: str = ""
    # Документ, отправленный в «Честный ЗНАК» из программы: его номер, последний
    # известный статус и сколько кодов блока им покрыто (с начала блока).
    doc_id: str = ""
    doc_status: str = ""
    introduced_count: int = 0
    # Сведения о товаре для перемаркировки. Заполняются один раз на блок: у блока
    # один GTIN, а значит одна страна, один цвет и размер.
    country: str = ""
    color: str = ""
    size: str = ""

    @property
    def total(self) -> int:
        return len(self.codes)

    @property
    def left(self) -> int:
        """Сколько этикеток ещё не напечатано."""
        return max(0, self.total - self.printed)

    @property
    def title(self) -> str:
        stamp = parse_stamp(self.created)
        when = f"{stamp:%d.%m.%Y %H:%M}" if stamp else "—"
        name = self.name or self.gtin
        return f"{when} · {name} · {self.total} шт."

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "created": self.created, "contour": self.contour,
            "order_id": self.order_id, "block_id": self.block_id,
            "gtin": self.gtin, "name": self.name, "tnved": self.tnved,
            "product_group": self.product_group,
            "release_method": self.release_method, "printed": self.printed,
            "introduced": self.introduced, "doc_id": self.doc_id,
            "doc_status": self.doc_status,
            "introduced_count": self.introduced_count, "country": self.country,
            "color": self.color, "size": self.size, "codes": self.codes,
        }


# --- обращение к СУЗ -------------------------------------------------------------------

def product_info(credentials: Credentials, order_id: str,
                 contour: Contour = Contour.SANDBOX) -> dict[str, Product]:
    """Название и ТН ВЭД товаров заказа. Ничего не расходует.

    Нужны для этикетки и для документа ввода в оборот. Отказ здесь не мешает
    получить коды — название и ТН ВЭД можно вписать руками, — поэтому ошибка
    превращается в пустой ответ, а не в остановку работы.
    """
    ready = credentials.stripped()
    apply(ready, contour)
    try:
        answer = transport.get(
            "suz", PRODUCT_PATH, contour=contour,
            params={"omsId": ready.oms_id, "orderId": order_id},
            headers={TOKEN_HEADER: ready.token})
    except transport.MarkingError as error:
        if error.status in (400, 401, 403) and _stale(error):
            raise
        return {}
    found: dict[str, Product] = {}
    if isinstance(answer, dict):
        for gtin, attributes in answer.items():
            if isinstance(attributes, dict):
                found[str(gtin)] = Product(
                    gtin=str(gtin),
                    name=_first(attributes, _NAME_KEYS),
                    tnved=_first(attributes, _TNVED_KEYS))
    return found


def fetch(credentials: Credentials, order_id: str, gtin: str, quantity: int,
          contour: Contour = Contour.SANDBOX, *, product: Product | None = None,
          product_group: str = "", release_method: str = "") -> Batch:
    """Забирает коды из буфера заказа и **сразу сохраняет на диск**.

    Безвозвратная операция: коды списываются из буфера. Запрос без повторов, а
    результат записан до возврата — что бы ни случилось дальше, оплаченные коды
    лежат в файле.
    """
    if not 1 <= quantity <= MAX_PER_REQUEST:
        raise transport.MarkingError(
            f"Количество кодов — от 1 до {MAX_PER_REQUEST} за один раз.")
    ready = credentials.stripped()
    if gaps := ready.missing:
        raise transport.MarkingError(f"Не заполнено: {', '.join(gaps)}.")
    apply(ready, contour)
    try:
        answer = transport.request(
            "GET", "suz", CODES_PATH, contour=contour,
            params={"omsId": ready.oms_id, "orderId": order_id,
                    "gtin": gtin, "quantity": quantity},
            headers={TOKEN_HEADER: ready.token}, retries=0)
    except transport.Offline as error:
        raise CodesUnknown(
            "Связь оборвалась, и неизвестно, выдала ли СУЗ коды. Нажмите "
            "«Восстановить»: если блок выдан, он вернётся оттуда. "
            f"Повторное получение забрало бы второй блок.\n\nПодробности: {error}"
        ) from None

    codes, block_id = _codes(answer)
    if not codes:
        raise transport.MarkingError(
            "СУЗ ответила, но кодов в ответе нет. Если они списались из "
            "буфера, их можно вернуть кнопкой «Восстановить».")
    return _store(codes, block_id, order_id, gtin, contour, product,
                  product_group, release_method)


def recover(credentials: Credentials, order_id: str, gtin: str,
            contour: Contour = Contour.SANDBOX, *, product: Product | None = None,
            product_group: str = "", release_method: str = "") -> list[Batch]:
    """Возвращает блоки, выданные ранее, но не сохранённые на этом компьютере.

    Работает, пока заказ не закрыт и первое получение шло через API
    (руководство СУЗ, 4.4.5). Уже сохранённые блоки не трогаются и не
    дублируются.
    """
    ready = credentials.stripped()
    if gaps := ready.missing:
        raise transport.MarkingError(f"Не заполнено: {', '.join(gaps)}.")
    apply(ready, contour)
    answer = transport.get(
        "suz", BLOCKS_PATH, contour=contour,
        params={"omsId": ready.oms_id, "orderId": order_id, "gtin": gtin},
        headers={TOKEN_HEADER: ready.token})
    blocks = answer.get("blocks") if isinstance(answer, dict) else None
    known = {batch.block_id for batch in saved() if batch.block_id}
    restored: list[Batch] = []
    for block in blocks or []:
        block_id = str(block.get("blockId") or "") if isinstance(block, dict) else ""
        if not block_id or block_id in known:
            continue
        reply = transport.get(
            "suz", RETRY_PATH, contour=contour,
            params={"omsId": ready.oms_id, "blockId": block_id},
            headers={TOKEN_HEADER: ready.token})
        codes, got_id = _codes(reply)
        if codes:
            restored.append(_store(codes, got_id or block_id, order_id, gtin,
                                   contour, product, product_group, release_method))
    return restored


def _codes(answer: Any) -> tuple[list[str], str]:
    if not isinstance(answer, dict):
        return [], ""
    raw = answer.get("codes")
    codes = [str(item) for item in raw if str(item).strip()] \
        if isinstance(raw, list) else []
    return codes, str(answer.get("blockId") or "")


def _stale(error: object) -> bool:
    from . import suz
    return suz.stale(error)


def _first(row: dict, names: Sequence[str]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


# --- хранение -----------------------------------------------------------------------

def folder() -> str:
    return appdata.path_to(FOLDER)


def _file(batch_id: str) -> str:
    return os.path.join(folder(), f"{batch_id}.json")


def _store(codes: list[str], block_id: str, order_id: str, gtin: str,
           contour: Contour, product: Product | None, product_group: str,
           release_method: str) -> Batch:
    now = datetime.now()
    batch = Batch(
        id=f"{now:%Y%m%d-%H%M%S}-{gtin}-{(block_id or 'x')[:8]}",
        created=now.isoformat(timespec="seconds"),
        contour=contour.value, order_id=order_id, block_id=block_id, gtin=gtin,
        name=(product.name if product else ""),
        tnved=(product.tnved if product else ""),
        product_group=product_group, release_method=release_method, codes=codes)
    save(batch)
    return batch


def save(batch: Batch) -> None:
    """Пишет блок атомарно: через временный файл, чтобы обрыв не оставил половину."""
    os.makedirs(folder(), exist_ok=True)
    target = _file(batch.id)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(batch.as_json(), handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def saved() -> list[Batch]:
    """Все сохранённые блоки, свежие первыми. Повреждённый файл пропускается."""
    found: list[Batch] = []
    try:
        names = sorted(os.listdir(folder()), reverse=True)
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        batch = load(name[:-5])
        if batch is not None:
            found.append(batch)
    return found


def load(batch_id: str) -> Batch | None:
    try:
        with open(_file(batch_id), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("codes"), list):
        return None
    return Batch(
        id=str(raw.get("id") or batch_id), created=str(raw.get("created") or ""),
        contour=str(raw.get("contour") or ""), order_id=str(raw.get("order_id") or ""),
        block_id=str(raw.get("block_id") or ""), gtin=str(raw.get("gtin") or ""),
        name=str(raw.get("name") or ""), tnved=str(raw.get("tnved") or ""),
        product_group=str(raw.get("product_group") or ""),
        release_method=str(raw.get("release_method") or ""),
        codes=[str(code) for code in raw["codes"]],
        printed=int(raw.get("printed") or 0), introduced=str(raw.get("introduced") or ""),
        doc_id=str(raw.get("doc_id") or ""), doc_status=str(raw.get("doc_status") or ""),
        introduced_count=int(raw.get("introduced_count") or 0),
        country=str(raw.get("country") or ""), color=str(raw.get("color") or ""),
        size=str(raw.get("size") or ""))


def delete(batch: Batch) -> str:
    """Убирает блок из списка и возвращает, куда его переложили.

    Файл не стирается, а уезжает в подпапку `deleted`: это единственная копия
    оплаченных кодов, и «Удалить», нажатое по ошибке, не должно стоить денег.
    Список подпапку не читает — `saved()` берёт только `*.json` в самой папке.
    """
    source = _file(batch.id)
    trash = os.path.join(folder(), TRASH)
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, os.path.basename(source))
    os.replace(source, target)
    return target


def stored_for(order_id: str, gtin: str = "") -> int:
    """Сколько кодов заказа сохранено на этом компьютере (по товару, если указан)."""
    return sum(batch.total for batch in saved()
               if batch.order_id == order_id and (not gtin or batch.gtin == gtin))


def mark_printed(batch: Batch, printed: int) -> None:
    """Запоминает, сколько этикеток ушло на принтер. Назад не откатывается."""
    batch.printed = max(batch.printed, min(printed, batch.total))
    save(batch)


def mark_introduced(batch: Batch) -> None:
    """Файл ввода в оборот сформирован (но не отправлен из программы)."""
    batch.introduced = datetime.now().isoformat(timespec="seconds")
    save(batch)


def mark_sent(batch: Batch, doc_id: str, covered: int, status: str = "") -> None:
    """Документ ввода в оборот отправлен: запоминается сразу, до опроса статуса.

    Номер документа — единственное, по чему потом можно узнать его судьбу, и
    потерять его из-за сбоя на опросе нельзя.
    """
    batch.doc_id = doc_id
    batch.introduced_count = max(batch.introduced_count, min(covered, batch.total))
    batch.doc_status = status
    batch.introduced = datetime.now().isoformat(timespec="seconds")
    save(batch)


def mark_status(batch: Batch, status: str, failed: bool = False) -> None:
    """Запоминает статус документа. Отказ освобождает коды для новой отправки.

    Отклонённый документ ничего не ввёл в оборот, и считать его коды
    «отправленными» значило бы не дать отправить их исправленным документом.
    """
    batch.doc_status = status
    if failed:
        batch.introduced_count = 0
    save(batch)


def parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
