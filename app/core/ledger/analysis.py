"""Что видно из ведомости: где расходится, что кончилось, чего не хватило.

Считается только по магазинам: склады «в пути» в расходе не участвуют, и их
остаток к продажам отношения не имеет — он показывается отдельной строкой,
чтобы товар в пути не выглядел пропавшим.

Слово «продажи» здесь не используется намеренно. В расход попадают и списания,
и перемещения между складами, а отличить их в этой выгрузке нечем: колонка одна.
Отчёт, который назвал бы расход продажами, читался бы точнее, чем есть.

Дни тоже не считаются: период выгрузки в файле не указан, и «хватит на неделю»
было бы выдумкой. Всё в штуках и долях.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Item, Ledger, Movement, Store

# Сколько товаров показывать в топе по умолчанию.
TOP_LIMIT = 15

# Сколько лучших показывать по каждому магазину: пятнадцать строк на магазин
# при восьми магазинах — это сто двадцать строк, которые никто не читает.
STORE_TOP = 5


@dataclass(slots=True)
class Line:
    """Товар на одном складе."""

    item: Item
    store: str
    move: Movement

    @property
    def article(self) -> str:
        return self.item.article

    @property
    def name(self) -> str:
        return self.item.name


@dataclass(slots=True)
class ItemTotal:
    """Товар в сумме по всем магазинам."""

    item: Item
    move: Movement
    # Магазины, где расход был: по нему видно, товар ходовой везде или в одном.
    active: list[str] = field(default_factory=list)
    # Магазины, где расход был, а остатка не осталось.
    ran_out: list[str] = field(default_factory=list)
    share: float = 0.0

    @property
    def article(self) -> str:
        return self.item.article

    @property
    def name(self) -> str:
        return self.item.name


@dataclass(slots=True)
class StoreTotal:
    """Магазин в сумме по всем товарам."""

    store: Store
    move: Movement
    items: int = 0
    ran_out: int = 0
    top: list[Line] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    """Всё, что показывает вкладка и уходит в файл."""

    ledger: Ledger
    total: Movement
    items: list[ItemTotal] = field(default_factory=list)
    stores: list[StoreTotal] = field(default_factory=list)
    shortages: list[Line] = field(default_factory=list)
    tight: list[Line] = field(default_factory=list)
    transit: list[Line] = field(default_factory=list)
    # Отрицательный остаток: расход больше, чем было. Это не нехватка товара, а
    # ошибка учёта — непроведённая приходная или пересорт, — и лечится она не
    # завозом, поэтому стоит отдельно от кончившихся.
    negative: list[Line] = field(default_factory=list)
    limit: int = TOP_LIMIT

    @property
    def top(self) -> list[ItemTotal]:
        """Топ для показа. `items` остаётся целым: в файл уходит всё."""
        return self.items[:self.limit]

    @property
    def doubled(self) -> list[Store]:
        """Склады с повторяющимся названием — о них стоит предупредить."""
        return [store for store in self.ledger.stores if store.doubled]


def build(ledger: Ledger, *, limit: int = TOP_LIMIT,
          store_top: int = STORE_TOP) -> Report:
    """Считает всё разом: страница показывает сразу несколько срезов."""
    titles = ledger.shop_titles
    total = ledger.total(titles)
    report = Report(ledger=ledger, total=total, limit=limit)

    report.items = _items(ledger, titles, total.outgoing)
    report.stores = _stores(ledger, store_top)
    report.shortages = _shortages(ledger, titles)
    report.tight = _tight(ledger, titles)
    report.transit = _transit(ledger)
    report.negative = _negative(ledger, titles)
    return report


def _items(ledger: Ledger, titles: list[str], outgoing: float) -> list[ItemTotal]:
    """Товары по убыванию расхода. Список полный — топ отрезают позже."""
    rows: list[ItemTotal] = []
    for item in ledger.items:
        move = item.total(titles)
        active = [title for title in titles
                  if item.move(title).outgoing > 0]
        ran_out = [title for title in titles if item.move(title).ran_out]
        rows.append(ItemTotal(
            item=item, move=move, active=active, ran_out=ran_out,
            share=move.outgoing / outgoing if outgoing else 0.0))
    rows.sort(key=lambda row: (-row.move.outgoing, row.name))
    return rows


def _stores(ledger: Ledger, store_top: int) -> list[StoreTotal]:
    """Магазины по убыванию расхода, у каждого — его лучшие товары."""
    rows: list[StoreTotal] = []
    for store in ledger.shops:
        move = Movement()
        lines: list[Line] = []
        ran_out = 0
        for item in ledger.items:
            piece = item.moves.get(store.title)
            if piece is None:
                continue
            move.opening += piece.opening
            move.incoming += piece.incoming
            move.outgoing += piece.outgoing
            move.closing += piece.closing
            if piece.ran_out:
                ran_out += 1
            if piece.outgoing > 0:
                lines.append(Line(item=item, store=store.title, move=piece))
        lines.sort(key=lambda line: (-line.move.outgoing, line.name))
        rows.append(StoreTotal(store=store, move=move, items=len(lines),
                               ran_out=ran_out, top=lines[:store_top]))
    rows.sort(key=lambda row: -row.move.outgoing)
    return rows


def _shortages(ledger: Ledger, titles: list[str]) -> list[Line]:
    """Расход был, остатка не осталось — по убыванию расхода.

    Это и есть «хорошо расходился, но кончился». Утверждать, что продажи
    потеряны, нельзя: в выгрузке нет дат, и товар мог кончиться в последний день
    периода. Но смотреть такой список нужно первым.
    """
    lines = [Line(item=item, store=title, move=item.move(title))
             for item in ledger.items for title in titles
             if item.move(title).ran_out]
    lines.sort(key=lambda line: (-line.move.outgoing, line.name))
    return lines


def _tight(ledger: Ledger, titles: list[str]) -> list[Line]:
    """Остаток есть, но меньше, чем ушло за период — кончится следующим.

    Отдельно от кончившихся: там нужен завоз, здесь — внимание.
    """
    lines = []
    for item in ledger.items:
        for title in titles:
            move = item.move(title)
            if move.outgoing > 0 and 0 < move.closing < move.outgoing:
                lines.append(Line(item=item, store=title, move=move))
    lines.sort(key=lambda line: (-line.move.outgoing, line.name))
    return lines


def _negative(ledger: Ledger, titles: list[str]) -> list[Line]:
    """Остаток ушёл в минус — по возрастанию, самый глубокий минус первым.

    Проверять такое нужно раньше выводов о продажах: минус означает, что расход
    посчитан по товару, которого на складе не было, и остальные числа по этой
    строке тоже под вопросом.
    """
    lines = [Line(item=item, store=title, move=item.move(title))
             for item in ledger.items for title in titles
             if item.move(title).closing < 0]
    lines.sort(key=lambda line: (line.move.closing, line.name))
    return lines


def _transit(ledger: Ledger) -> list[Line]:
    """Что лежит в пути: остаток есть, а движения нет."""
    lines = [Line(item=item, store=store.title, move=item.move(store.title))
             for item in ledger.items for store in ledger.transit
             if item.move(store.title).closing > 0]
    lines.sort(key=lambda line: (-line.move.closing, line.name))
    return lines
