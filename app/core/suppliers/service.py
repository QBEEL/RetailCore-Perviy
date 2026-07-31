"""Сценарий переоценки с участием базы поставщиков.

Здесь сходятся два слоя: `pricing` умеет сравнивать файлы, а база помнит, чей
это файл, как его читать и что пришлось свести вручную в прошлый раз. Страница
интерфейса работает только с этим модулем и не собирает сценарий сама.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..appdata import log_event
from ..pricing import (
    ComparisonResult,
    MatchOptions,
    OneCTemplate,
    PriceLine,
    SupplierPrice,
    SupplierProfile,
    compare,
    match_lines,
    suggest_price_map,
    suggest_profile_name,
)
from ..pricing.service import LOG_FILE
from . import store
from .identify import identify, suggest_aliases
from .links import LinkBook, apply_links, keys_for, link_from
from .models import Guess, LinkKey, Supplier, SupplierLayout

ProgressCallback = Callable[[int, int], None]


@dataclass(slots=True)
class Session:
    """Опознанный поставщик и всё, что база знает о его прайсе."""

    supplier: Supplier = field(default_factory=Supplier)
    layout: SupplierLayout = field(default_factory=SupplierLayout)
    profile: SupplierProfile = field(default_factory=SupplierProfile)
    book: LinkBook = field(default_factory=LinkBook)
    guess: Guess | None = None
    known: bool = False
    # Поставщика указал пользователь, а не автоматическое узнавание.
    chosen: bool = False

    @property
    def reason(self) -> str:
        """Чем поставщик опознан — это видно в подписи над соответствием колонок."""
        if self.guess is not None:
            return self.guess.reason
        if self.chosen:
            return "выбран вручную"
        return "сохранён в базе" if self.known else "новый поставщик"


def open_session(
    template: OneCTemplate,
    supplier_price: SupplierPrice,
    supplier_id: int = 0,
    path: str | None = None,
) -> Session:
    """Определяет поставщика и готовит соответствие колонок и привязки.

    `supplier_id` — явный выбор пользователя; он всегда сильнее автоматического
    узнавания и никогда им не перебивается.
    """
    titles = supplier_price.titles
    suppliers = store.list_suppliers(path)
    layouts = store.all_layouts(path)

    guess = None
    supplier = None
    layout = None
    if supplier_id:
        supplier = store.get_supplier(supplier_id, path)
        if supplier is not None:
            layout = _layout_for(supplier.id, layouts, supplier_price)
    if supplier is None:
        guess = identify(
            supplier_price.path, titles, supplier_price.sheet_name,
            suppliers, layouts, store.all_aliases(path))
        if guess is not None and guess.confident:
            supplier, layout = guess.supplier, guess.layout

    known = supplier is not None
    if supplier is None:
        supplier = Supplier(name=suggest_profile_name(supplier_price.path))

    profile = _profile(template, supplier_price, supplier, layout)
    book = LinkBook(store.links(supplier.id, path)) if supplier.id else LinkBook()
    return Session(
        supplier=supplier,
        layout=layout or SupplierLayout(supplier_id=supplier.id, profile=profile),
        profile=profile,
        book=book,
        guess=guess,
        known=known,
        chosen=bool(supplier_id) and known,
    )


def _layout_for(
    supplier_id: int,
    layouts: Sequence[SupplierLayout],
    supplier_price: SupplierPrice,
) -> SupplierLayout | None:
    """Структура, подходящая присланному файлу: сначала по сигнатуре, затем по листу."""
    signature = store.signature_of(supplier_price.titles)
    own = [layout for layout in layouts if layout.supplier_id == supplier_id]
    if not own:
        return None
    exact = [layout for layout in own if signature and layout.signature == signature]
    same_sheet = [layout for layout in own if layout.sheet_name == supplier_price.sheet_name]
    return (exact or same_sheet or own)[0]


def _profile(
    template: OneCTemplate,
    supplier_price: SupplierPrice,
    supplier: Supplier,
    layout: SupplierLayout | None,
) -> SupplierProfile:
    """Соответствие колонок: сохранённое, дополненное автоподбором."""
    suggested = suggest_price_map(template.valid_types, supplier_price.price_columns)
    if layout is None:
        return SupplierProfile(
            name=supplier.name, sheet=supplier_price.sheet_name, price_map=suggested)

    saved = layout.profile
    profile = SupplierProfile(
        name=supplier.name,
        sheet=supplier_price.sheet_name,
        price_map=dict(saved.price_map),
        role_map=dict(saved.role_map),
        separators=saved.separators or SupplierProfile().separators,
        modifier_separators=saved.modifier_separators,
    )
    # Вид цены мог появиться в шаблоне уже после того, как профиль сохранили.
    for name, column in suggested.items():
        profile.price_map.setdefault(name, column)
    # Колонка могла исчезнуть из нового файла — тогда её выбор бессмыслен.
    known = {column.title for column in supplier_price.price_columns}
    profile.price_map = {n: c for n, c in profile.price_map.items() if c in known}
    return profile


def run_comparison(
    template: OneCTemplate,
    supplier_price: SupplierPrice,
    session: Session,
    options: MatchOptions | None = None,
    progress: ProgressCallback | None = None,
) -> ComparisonResult:
    """Подбор с учётом сохранённых привязок, затем сравнение цен."""
    options = options or MatchOptions()
    lines = match_lines(template, supplier_price, options, progress)
    keys = keys_for(template, lines)
    applied = apply_links(lines, keys, session.book, supplier_price.records)
    stats = compare(lines, template, supplier_price, session.profile)

    log_event(LOG_FILE, (
        f"Поставщик: {session.supplier.name} ({session.reason}) · "
        f"привязок применено {applied} из {len(session.book)}\n"
        f"Сравнение: {stats.total} строк · найдено {stats.found} ({stats.rate:.1f} %) · "
        f"изменено {stats.changed} · без изменений {stats.unchanged} · "
        f"требует сопоставления {stats.review} · не найдено {stats.not_found}"))
    return ComparisonResult(
        lines=lines, stats=stats, types=template.valid_types, profile=session.profile)


def remember_session(
    session: Session,
    supplier_price: SupplierPrice,
    path: str | None = None,
) -> Session:
    """Сохраняет карточку поставщика и структуру его прайса."""
    supplier = store.save_supplier(session.supplier, path)
    session.supplier = supplier
    if not session.known:
        # Слова из имени файла помогут узнать поставщика в следующий раз,
        # даже если структуру он поменяет.
        patterns = suggest_aliases(supplier_price.path, supplier.name)
        if patterns:
            store.set_aliases(supplier.id, patterns, path)
    session.profile.name = supplier.name
    session.layout = store.save_layout(
        SupplierLayout(
            id=session.layout.id,
            supplier_id=supplier.id,
            profile=session.profile,
            signature=store.signature_of(supplier_price.titles),
            titles=list(supplier_price.titles),
        ),
        path,
    )
    session.known = True
    return session


def remember_link(
    line: PriceLine,
    key: LinkKey,
    session: Session,
    path: str | None = None,
) -> bool:
    """Запоминает ручную привязку строки. Без опознанного поставщика невозможно."""
    if not session.supplier.id or not key or line.source is None:
        return False
    store.save_link(link_from(line, key, session.supplier.id), path)
    session.book = LinkBook(store.links(session.supplier.id, path))
    log_event(LOG_FILE, (
        f"Привязка сохранена · {session.supplier.name} · "
        f"{line.article} → {line.supplier_article}"))
    return True


def forget_link(
    key: LinkKey,
    session: Session,
    path: str | None = None,
) -> bool:
    """Убирает сохранённую привязку — например, если поставщик сменил артикул."""
    link = session.book.find(key)
    if link is None or not link.id:
        return False
    store.delete_link(link.id, path)
    session.book = LinkBook(store.links(session.supplier.id, path))
    return True


def adopt_settings_profiles(profiles, path: str | None = None) -> int:
    """Переносит профили из settings.json при первом запуске с базой."""
    adopted = store.adopt_profiles(profiles, path)
    if adopted:
        log_event(LOG_FILE, f"В базу поставщиков перенесено профилей: {adopted}")
    return adopted


def describe_database(path: str | None = None) -> str:
    size = store.database_size(path) / 1024
    suppliers = len(store.list_suppliers(path))
    return f"{suppliers} поставщиков · {size:.0f} КБ"


def default_supplier_name(path: str) -> str:
    return suggest_profile_name(path) or os.path.splitext(os.path.basename(path))[0]
