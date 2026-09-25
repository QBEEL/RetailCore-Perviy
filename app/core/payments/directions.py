"""Направление оплаты: Beauty, Fashion или прочие.

Направление — свойство поставщика, и хранится оно на сервере, в закреплениях
(`suppliers.directory`). Но у Fashion поставщики размечены не все, а оплаты
отдела ведут три человека без учётных записей в программе. Поэтому для них
правило другое: всё, что оформили они, — Fashion, кому бы ни платили.

Ответственный сильнее поставщика. Иначе платёж Fashion поставщику, которого
Beauty тоже возит, попадал бы в Beauty, и отдел Fashion не досчитался бы своих
денег. По той же причине направления не пересекаются: у каждой оплаты ровно
одно, и суммы трёх отборов складываются в итог выборки без остатка.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .models import Payment
from .recipients import recipient_key

# Отбор «прочие»: ни ответственный, ни поставщик направления не дают.
NO_DIRECTION = "none"

# Ответственный из выгрузки 1С → направление. Имена ровно так, как их пишет
# 1С в колонке «Заявитель»: сравнение точное, как у отбора по ответственному.
# Лёвкина записана дважды: выгрузка теряет «ё» при перекодировке и приходит
# как «Л?вкина», но исправленная выгрузка не должна молча выкинуть её из Fashion.
RESPONSIBLE_DIRECTIONS: dict[str, str] = {
    "Баранова Олеся": "fashion",
    "Мамонова Екатерина": "fashion",
    "Л?вкина София": "fashion",
    "Лёвкина София": "fashion",
}


def direction_of(
    payment: Payment,
    suppliers: Mapping[str, Sequence[str]],
    people: Mapping[str, str] = RESPONSIBLE_DIRECTIONS,
    keys: dict[str, str] | None = None,
) -> str:
    """Направление одной оплаты.

    `suppliers` — ключ получателя → его направления в порядке отдела (Beauty
    раньше Fashion). Поставщик в двух направлениях относится к первому: делить
    одну оплату пополам нечем, а ответственный, который решил бы спор, уже
    проверен выше.

    `keys` — кэш ключей получателей: на семи тысячах строк одни и те же
    полтысячи имён нормализуются снова и снова.
    """
    if code := people.get(payment.responsible.strip()):
        return code
    name = payment.recipient
    if keys is None:
        key = recipient_key(name)
    elif (key := keys.get(name)) is None:
        key = keys[name] = recipient_key(name)
    codes = suppliers.get(key)
    return codes[0] if codes else NO_DIRECTION


def by_direction(
    rows: Iterable[Payment],
    direction: str,
    suppliers: Mapping[str, Sequence[str]],
    people: Mapping[str, str] = RESPONSIBLE_DIRECTIONS,
) -> list[Payment]:
    """Оплаты одного направления. Пустое направление ничего не отбирает."""
    if not direction:
        return list(rows)
    keys: dict[str, str] = {}
    return [payment for payment in rows
            if direction_of(payment, suppliers, people, keys) == direction]
