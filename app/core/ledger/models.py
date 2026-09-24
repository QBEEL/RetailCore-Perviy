"""Ведомость по товарам на складах: что в ней есть после разбора.

Выгрузка 1С приходит широкой: на каждый склад по четыре колонки, и складов
шестнадцать. Читать её глазами нельзя, а считать по ней — тем более: колонки
повторяются, часть складов служебная, а итог стоит рядом с данными и попадает
в любую сумму, посчитанную по строке целиком.

Здесь та же ведомость, разложенная по складам и товарам: дальше по ней считают
анализ и собирают понятный файл.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Store:
    """Склад из шапки отчёта.

    `title` может отличаться от `name`: в выгрузке встречаются два разных склада
    с одинаковым названием, и группировать их по имени нельзя — данные одного
    затрут данные другого. Различает их `title` с номером, а исходное имя
    остаётся в `name`, чтобы человек узнал свой склад.
    """

    name: str
    title: str
    column: int
    # Склад «в пути»: товар отгружен, но не принят. Продаж там не бывает, и в
    # анализе он только мешает — но остаток в пути видеть нужно.
    transit: bool = False

    @property
    def doubled(self) -> bool:
        return self.title != self.name


@dataclass(slots=True)
class Movement:
    """Движение одного товара по одному складу."""

    opening: float = 0.0
    incoming: float = 0.0
    outgoing: float = 0.0
    closing: float = 0.0

    @property
    def available(self) -> float:
        """Сколько было в распоряжении за период: остаток плюс приход."""
        return self.opening + self.incoming

    @property
    def share(self) -> float:
        """Какая доля запаса ушла. 1.0 — не осталось ничего."""
        return self.outgoing / self.available if self.available else 0.0

    @property
    def empty(self) -> bool:
        return not (self.opening or self.incoming or self.outgoing or self.closing)

    @property
    def ran_out(self) -> bool:
        """Расход был, а остатка не осталось.

        Это факт, а не вывод: без дат неизвестно, когда именно товар кончился —
        в первый день периода или в последний.
        """
        return self.outgoing > 0 and self.closing <= 0


@dataclass(slots=True)
class Item:
    """Товар и его движение по складам."""

    article: str = ""
    name: str = ""
    unit: str = ""
    # Ключ — `title` склада, а не имя: одноимённые склады различаются только им.
    moves: dict[str, Movement] = field(default_factory=dict)

    def move(self, title: str) -> Movement:
        return self.moves.get(title) or Movement()

    def total(self, titles: list[str]) -> Movement:
        """Сумма движения по перечисленным складам."""
        result = Movement()
        for title in titles:
            piece = self.moves.get(title)
            if piece is None:
                continue
            result.opening += piece.opening
            result.incoming += piece.incoming
            result.outgoing += piece.outgoing
            result.closing += piece.closing
        return result


@dataclass(slots=True)
class Ledger:
    """Разобранная ведомость."""

    stores: list[Store] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    # Итоговая колонка самого отчёта — по ней сверяется разбор. Если сумма по
    # складам с ней не сходится, значит шапку прочитали неверно, и молчать об
    # этом нельзя: отчёт с потерянной колонкой выглядит целым.
    reported: Movement | None = None
    title: str = ""

    @property
    def shops(self) -> list[Store]:
        """Склады, по которым считается анализ: без «в пути» и без итога."""
        return [store for store in self.stores if not store.transit]

    @property
    def transit(self) -> list[Store]:
        return [store for store in self.stores if store.transit]

    @property
    def shop_titles(self) -> list[str]:
        return [store.title for store in self.shops]

    def total(self, titles: list[str] | None = None) -> Movement:
        """Движение по складам. По умолчанию — по магазинам, без «в пути»."""
        result = Movement()
        chosen = self.shop_titles if titles is None else titles
        for item in self.items:
            piece = item.total(chosen)
            result.opening += piece.opening
            result.incoming += piece.incoming
            result.outgoing += piece.outgoing
            result.closing += piece.closing
        return result

    @property
    def balanced(self) -> bool:
        """Сходится ли разбор с итоговой колонкой отчёта.

        Сверяется по всем складам, включая «в пути»: итог в файле посчитан по
        ним тоже. Копейки допускаются — числа приходят из Excel как float.
        """
        if self.reported is None:
            return True
        mine = self.total([store.title for store in self.stores])
        return all(abs(getattr(mine, field) - getattr(self.reported, field)) < 0.01
                   for field in ("opening", "incoming", "outgoing", "closing"))
