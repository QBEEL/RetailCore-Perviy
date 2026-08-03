"""Формы запросов и ответов.

Имена полей повторяют dataclass Payment из приложения: клиент раскладывает
ответ в тот же объект, что раньше приходил из SQLite, и остальной код —
таблица, календарь, аналитика — ничего не замечает.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Полный набор из app/core/payments/models.py. «moved» — перенос, назначенный
# человеком: у него есть новая дата, и в просрочку он не превращается.
Status = Literal["planned", "paid", "overdue", "moved", "cancelled"]
Origin = Literal["import", "manual", "order", "pricing"]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    login: str
    full_name: str
    is_admin: bool
    responsible: list[str]
    # Пароль выдан администратором и известен не только владельцу учётки.
    # Работать под ним можно, но приложение потребует заменить его сразу.
    must_change_password: bool = False


class PaymentIn(BaseModel):
    """Платёж, пришедший от клиента. Поля из выгрузки 1С сюда не входят."""

    pay_date: date | None = None
    amount: float = 0.0
    vat: float = 0.0
    currency: str = "руб."
    supplier_id: int = 0
    recipient: str = ""
    status: Status = "planned"
    comment: str = ""
    responsible: str = ""
    operation: str = ""
    priority: str = ""


class PaymentOut(BaseModel):
    id: int
    doc_number: str
    request_date: date | None
    pay_date: date | None
    amount: float
    vat: float
    currency: str
    supplier_id: int
    recipient: str
    recipient_key: str
    status: Status
    source_status: str
    paid_flag: bool
    operation: str
    over_limit: bool
    priority: str
    edo_state: str
    responsible: str
    author: str
    comment: str
    had_files: bool
    origin: Origin
    origin_ref: str
    created_at: datetime
    updated_at: datetime
    files: int = 0
    # Может ли текущий пользователь править эту запись. Считается на сервере,
    # чтобы клиент не повторял правило и не разошёлся с ним при изменении.
    editable: bool = False


class PaymentPatch(BaseModel):
    """Точечная правка. Не переданное поле не меняется."""

    pay_date: date | None = None
    status: Status | None = None
    comment: str | None = None
    supplier_id: int | None = None
    amount: float | None = None
    priority: str | None = None
    # Отдельный признак: иначе нельзя отличить «убрать дату» от «не трогать».
    clear_pay_date: bool = False


class BulkPatch(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    patch: PaymentPatch


class BulkResult(BaseModel):
    changed: int
    denied: list[int] = []


class BudgetIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    amount: float = 0.0
    note: str = ""


class BudgetOut(BudgetIn):
    updated_at: datetime


class RecipientLinkIn(BaseModel):
    recipient: str
    supplier_id: int


class RecipientLinkOut(BaseModel):
    recipient_key: str
    recipient: str
    supplier_id: int
    linked_by: str
    updated_at: datetime


class SupplierRow(BaseModel):
    """Поставщик, каким его видно из оплат."""

    recipient_key: str
    recipient: str
    supplier_id: int = 0
    payments: int = 0
    amount: float = 0.0
    last_pay: date | None = None
    # Все, кто платил этому поставщику, от частого к редкому. Список, а не одно
    # имя: почти половина получателей оплачивается несколькими менеджерами.
    managers: list[str] = []


class UnlinkedRecipient(BaseModel):
    recipient: str
    payments: int
    amount: float


class FileOut(BaseModel):
    id: int
    payment_id: int
    name: str
    size: int
    added_at: datetime


class UserIn(BaseModel):
    login: str
    full_name: str = ""
    responsible: list[str] = []
    is_admin: bool = False
    is_active: bool = True


class UserOut(BaseModel):
    id: int
    login: str
    full_name: str
    responsible: list[str]
    is_admin: bool
    is_active: bool
    created_at: datetime


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=10)


class KnownValues(BaseModel):
    """Значения для выпадающих списков отбора.

    Имена ключей повторяют store.known_values из приложения — интерфейс
    разбирает ответ тем же кодом, что раньше читал локальную базу.
    """

    recipients: list[str]
    responsible: list[str]
    operations: list[str]


# --- отчётность для поставщиков ---------------------------------------------------

class ReportProfileIn(BaseModel):
    """Формат отчёта, пришедший от клиента.

    Всё, кроме имени и поставщика, лежит в `payload` нетипизированным: состав
    полей, метрик и фильтров задаётся приложением и будет меняться чаще, чем
    выкатывается сервер. Проверять его здесь заново — значит выпускать новую
    версию API на каждую новую метрику.
    """

    name: str
    supplier: str = ""
    supplier_id: int = 0
    payload: dict = Field(default_factory=dict)


class ReportProfileOut(BaseModel):
    id: int
    name: str
    supplier: str
    supplier_id: int
    payload: dict
    updated_at: datetime
    updated_by: str = ""


class StoreRuleIn(BaseModel):
    """Правило «продажи источника учитывать за приёмником»."""

    source: str
    target: str
    enabled: bool = True
    comment: str = ""


class StoreRuleOut(BaseModel):
    id: int
    source: str
    target: str
    enabled: bool
    comment: str
    updated_at: datetime
    updated_by: str = ""
