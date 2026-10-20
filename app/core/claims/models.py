"""Заявки Торг-2 и то, что о них нужно знать интерфейсу.

Здесь нет ни Qt, ни сети: модель и правила показа проверяются обычным pytest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Названия видов, статусов и итогов — единственное место, где они записаны
# словами. Коды те же, что в базе на сервере.
KIND_TITLES = {"shortage": "Недопоставка", "defect": "Брак"}
STATUS_TITLES = {"new": "Новая", "accepted": "Принято в работу", "closed": "Закрыто"}
# Итоги закрытия: по ним потом считается, сколько вернули деньгами, а сколько
# просто довезли. Порядок — как в списке выбора.
RESOLUTION_TITLES = {
    "delivered": "Довезли недостающее",
    "replaced": "Заменили",
    "refund": "Возместили деньгами",
    "written_off": "Списали",
    "rejected": "Поставщик отказал",
}

# Сколько дней до конца срока годности считается «скоро». Для косметики и
# парфюмерии два месяца — это уже мало: на обмен у поставщика остаётся
# считанные недели.
SOON_DAYS = 60


@dataclass(slots=True)
class Claim:
    id: int = 0
    kind: str = "shortage"
    brand: str = ""
    product: str = ""
    spec: str = ""
    barcode: str = ""
    quantity: float = 1.0
    expiry_date: date | None = None
    comment: str = ""
    status: str = "new"
    applicant_id: int = 0
    applicant: str = ""
    manager_id: int | None = None
    manager: str = ""
    manager_seen: bool = True
    resolution: str = ""
    close_note: str = ""
    created_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: str = ""
    files: int = 0
    # Акт ТОРГ-2, составленный по заявке.
    act_id: int | None = None
    act_number: int | None = None
    # Права вошедшего на эту заявку — посчитаны сервером.
    can_edit: bool = False
    can_accept: bool = False
    can_close: bool = False
    can_reopen: bool = False
    can_delete: bool = False

    @property
    def kind_title(self) -> str:
        return KIND_TITLES.get(self.kind, self.kind)

    @property
    def status_title(self) -> str:
        return STATUS_TITLES.get(self.status, self.status)

    @property
    def resolution_title(self) -> str:
        return RESOLUTION_TITLES.get(self.resolution, self.resolution)

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    @property
    def title(self) -> str:
        """Как заявку называть в тексте: бренд и товар."""
        name = " · ".join(part for part in (self.brand, self.product) if part)
        return name or self.barcode or f"заявка {self.id}"

    def quantity_text(self) -> str:
        """«1» и «2,5», а не «1.0»: штуки считают целыми."""
        if float(self.quantity).is_integer():
            return str(int(self.quantity))
        return f"{self.quantity:g}".replace(".", ",")


@dataclass(slots=True)
class ClaimFile:
    id: int
    claim_id: int
    name: str
    size: int = 0
    added_at: datetime | None = None


@dataclass(slots=True)
class Manager:
    id: int
    full_name: str


@dataclass(slots=True)
class Brand:
    id: int
    name: str


def brand_key(name: str) -> str:
    """Ключ сравнения бренда: без регистра, лишних пробелов и «ё».

    Тот же, что считает сервер, — по нему решается, есть ли бренд в списке.
    """
    return " ".join(name.split()).lower().replace("ё", "е")


def has_brand(brands: list[Brand], name: str) -> bool:
    key = brand_key(name)
    return bool(key) and any(brand_key(b.name) == key for b in brands)


@dataclass(slots=True)
class InboxItem:
    id: int
    kind: str
    brand: str
    product: str
    applicant: str


@dataclass(slots=True)
class Inbox:
    """Что адресовано вошедшему: сколько непрочитанного и сколько всего открыто."""

    unseen: int = 0
    open: int = 0
    items: list[InboxItem] = field(default_factory=list)


@dataclass(slots=True)
class ClaimHint:
    brand: str = ""
    product: str = ""
    spec: str = ""
    manager_id: int | None = None


def expiry_label(expiry: date | None, today: date | None = None) -> str:
    """«До окончания» срока годности — словами, как в таблице склада.

    Месяцами — когда дней больше двух месяцев: «118 дн.» читается хуже, чем
    «3 мес.», а разница в неделю для такого срока ничего не меняет.
    """
    if expiry is None:
        return "Нет даты"
    days = (expiry - (today or date.today())).days
    if days < 0:
        return "Истёк"
    if days == 0:
        return "Истекает сегодня"
    if days > SOON_DAYS:
        return f"{days // 30} мес."
    return f"{days} дн."


def expiry_level(expiry: date | None, today: date | None = None) -> str:
    """Насколько срок горит: `none`, `ok`, `soon` или `expired` — для цвета."""
    if expiry is None:
        return "none"
    days = (expiry - (today or date.today())).days
    if days < 0:
        return "expired"
    return "soon" if days <= SOON_DAYS else "ok"
