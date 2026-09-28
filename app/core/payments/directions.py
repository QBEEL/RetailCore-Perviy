"""Направление оплаты: отдел (Beauty, Fashion), статья расхода или прочие.

Направление — свойство поставщика, и хранится оно на сервере, в закреплениях
(`suppliers.directory`). Направления бывают двух видов. Отдел — Beauty, Fashion
— говорит, чьи это деньги. Расход — маркетинг, налоги, аренда — говорит, на
что они ушли, и назначается только поставщику, а не человеку.

Порядок проверки, и почему он такой:

1. Расход поставщика. Администратор отметил поставщика «Арендой» — значит,
   аренда, кто бы её ни оформил. Иначе аренда магазина Fashion утекала бы в
   Fashion, и отбор «Аренда» показывал бы не всю аренду.
2. Операция 1С. Налоги и лизинг 1С проводит своими операциями, и размечать
   налоговые инспекции поставщиками незачем. Работает, только если такое
   направление есть на сервере: иначе оплата попала бы в отбор, которого нет
   в списке, и пропала бы из «остальных».
3. Ответственный. У Fashion поставщики размечены не все, а оплаты отдела
   ведут три человека без учётных записей в программе: всё, что оформили они,
   — Fashion, кому бы ни платили. Иначе платёж Fashion поставщику, которого
   Beauty тоже возит, попадал бы в Beauty.
4. Отдел поставщика.

У каждой оплаты ровно одно направление, и суммы всех отборов складываются в
итог выборки без остатка.
"""
from __future__ import annotations

from typing import Collection, Iterable, Mapping, Sequence

from .models import Payment
from .recipients import recipient_key

# Отбор «прочие»: ни ответственный, ни поставщик направления не дают.
NO_DIRECTION = "none"

# Операция из выгрузки 1С → направление расхода. Остальные операции — оплата
# поставщику, кредиты, зарплата — о назначении расхода ничего не говорят.
OPERATION_DIRECTIONS: dict[str, str] = {
    "Перечисление налогов и взносов": "taxes",
    "Оплата арендодателю": "rent",
}

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
    expense: Collection[str] = frozenset(),
    people: Mapping[str, str] = RESPONSIBLE_DIRECTIONS,
    keys: dict[str, str] | None = None,
) -> str:
    """Направление одной оплаты — по порядку из описания модуля.

    `suppliers` — ключ получателя → его направления в порядке справочника
    (Beauty раньше Fashion). Поставщик в двух отделах относится к первому:
    делить одну оплату пополам нечем. `expense` — коды направлений расхода,
    известные серверу.

    `keys` — кэш ключей получателей: на семи тысячах строк одни и те же
    полтысячи имён нормализуются снова и снова.
    """
    name = payment.recipient
    if keys is None:
        key = recipient_key(name)
    elif (key := keys.get(name)) is None:
        key = keys[name] = recipient_key(name)
    codes = suppliers.get(key) or ()
    if spent := next((code for code in codes if code in expense), ""):
        return spent
    if (code := OPERATION_DIRECTIONS.get(payment.operation.strip(), "")) in expense:
        return code
    if code := people.get(payment.responsible.strip()):
        return code
    return next((code for code in codes if code not in expense), NO_DIRECTION)


def by_direction(
    rows: Iterable[Payment],
    direction: str,
    suppliers: Mapping[str, Sequence[str]],
    expense: Collection[str] = frozenset(),
    people: Mapping[str, str] = RESPONSIBLE_DIRECTIONS,
) -> list[Payment]:
    """Оплаты одного направления. Пустое направление ничего не отбирает."""
    if not direction:
        return list(rows)
    keys: dict[str, str] = {}
    return [payment for payment in rows
            if direction_of(payment, suppliers, expense, people, keys) == direction]
