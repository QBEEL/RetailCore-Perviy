"""Создание карточек и проверка отправок в Национальном каталоге."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from . import session, transport
from .codes import gtin_valid
from .models import CodeInfo, Contour, PermitState

SET_GROUP_ATTRIBUTE = "23768"
FULL_TNVED_ATTRIBUTE = 13933
LIGHT_INDUSTRY_GROUP = "1"
# Коды и обозначения из справочника API НК. Состав списка — инструкция НКМТ,
# раздел 7.1.3 «Создание карточки набора вручную».
SET_GROUPS = {
    "48": ("carparts", "Автозапчасти и комплектующие транспортных средств"),
    "19": ("antiseptic", "Антисептики и дезинфицирующие средства"),
    "37": ("grocery", "Бакалейная продукция"),
    "22": ("nabeer", "Безалкогольное пиво"),
    "4": ("perfumery", "Духи и туалетная вода"),
    "27": ("toys", "Игры и игрушки для детей"),
    "32": ("conserve", "Консервированная продукция"),
    "20": ("petfood", "Корма для животных"),
    "35": ("chemistry", "Косметика, бытовая химия и товары личной гигиены"),
    LIGHT_INDUSTRY_GROUP: ("lp", "Лёгкая промышленность"),
    "10": ("wheelchairs", "Медицинские изделия"),
    "8": ("milk", "Молочная продукция"),
    "21": ("seafood", "Морепродукты"),
    "43": ("autofluids", "Моторные масла"),
    "25": ("meat", "Мясные изделия"),
    "49": ("furslp", "Натуральный мех"),
    "16": ("ncp", "Никотиносодержащая продукция"),
    "36": ("books", "Печатная продукция"),
    "59": ("pyrotechnics", "Пиротехнические изделия"),
    "40": ("fire", "Пожарная безопасность"),
    "44": ("polymer", "Полимерные трубы"),
    "28": ("radio", "Радиоэлектронная продукция"),
    "51": ("gadgets", "Радиоэлектронная продукция. Ноутбуки и смартфоны"),
    "33": ("vegetableoil", "Растительные масла"),
    "45": ("sweets", "Сладости и кондитерские изделия"),
    "23": ("softdrinks", "Соковая продукция и безалкогольные напитки"),
    "17": ("bio", "Специализированная пищевая продукция и БАД к пище"),
    "54": ("homeware", "Товары для дома и интерьера"),
    "53": ("fertilizers", "Удобрения в потребительской упаковке"),
    "6": ("electronics", "Фотокамеры, фотовспышки и лампы-вспышки"),
}

FEED_STATUSES = {
    "Received": "Получен",
    "Moderated": "Модерация завершена",
    "Signed": "Одобренные карточки подписаны",
    "Rejected": "Отклонён",
    "Processing": "Обрабатывается",
}


class CardKind(Enum):
    UNIT = "Единица товара"
    SET = "Набор"
    TECHNICAL = "Техническая карточка"


def positive_id(value: object) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,19}", text):
        return ""
    number = int(text)
    return str(number) if 0 < number <= 2**63 - 1 else ""


def validate_tnved(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{4}|[0-9]{10}", value):
        raise ValueError("Введите четырёхзначный или десятизначный код ТН ВЭД.")
    return value


def _gtin(value: str) -> str:
    value = value.strip()
    if (not re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", value)
            or not gtin_valid(value.zfill(14))):
        raise ValueError(f"Проверьте длину и контрольную цифру GTIN «{value}».")
    return value


@dataclass(frozen=True)
class CardDraft:
    kind: CardKind
    name: str
    brand: str
    tnved: str
    gtin: str = ""
    category: str = ""
    set_group: str = ""
    components: tuple[tuple[str, str], ...] = ()
    attributes: tuple[tuple[str, str, str], ...] = ()
    moderation: bool = False
    source_gtin: str = ""

    def payload(self) -> dict[str, Any]:
        name, brand = self.name.strip(), self.brand.strip()
        if not name or not brand:
            raise ValueError("Укажите полное наименование товара и товарный знак.")
        body: dict[str, Any] = {
            "good_name": name, "brand": brand,
            "tnved": validate_tnved(self.tnved),
        }
        if self.moderation or self.kind is not CardKind.TECHNICAL:
            body["moderation"] = int(self.moderation)
        if self.category.strip():
            if not (category := positive_id(self.category)):
                raise ValueError("Введите числовой идентификатор категории.")
            body["categories"] = [{"cat_id": int(category)}]

        if self.kind is CardKind.TECHNICAL:
            # В JSON-примере технической карточки ЦРПТ флаг и ID атрибутов числовые.
            body["is_tech_gtin"] = 1
        else:
            gtin = _gtin(self.gtin)
            body["gtin"] = gtin
            body["identified_by"] = [
                {"type": "gtin", "value": gtin, "level": "trade-unit", "multiplier": 1}
            ]

        attributes = []
        for raw_id, raw_value, raw_type in self.attributes:
            attribute_id, value_type = positive_id(raw_id), raw_type.strip()
            value = raw_value.strip()
            if not attribute_id or not value:
                raise ValueError("Для каждого атрибута укажите числовой ID и значение.")
            if self.kind is CardKind.SET and attribute_id == SET_GROUP_ATTRIBUTE:
                raise ValueError("Товарную группу набора укажите в отдельном поле.")
            attribute: dict[str, str | int] = {
                "attr_id": int(attribute_id) if self.kind is CardKind.TECHNICAL else attribute_id,
                "attr_value": value,
            }
            if value_type:
                attribute["attr_value_type"] = value_type
            attributes.append(attribute)

        if self.kind is CardKind.SET:
            group = positive_id(self.set_group)
            if not group:
                raise ValueError("Выберите товарную группу набора.")
            body["is_set"] = True
            attributes.append({"attr_id": SET_GROUP_ATTRIBUTE, "attr_value": group})
            components = self._set_gtins(gtin, group)
            if components:
                body["set_gtins"] = components
        if attributes:
            body["good_attrs"] = attributes
        return body

    def _set_gtins(self, set_gtin: str, group: str) -> list[dict[str, str | int]]:
        if not self.components and group != LIGHT_INDUSTRY_GROUP:
            raise ValueError("Укажите хотя бы одно вложение набора.")
        if len(self.components) > 1000:
            raise ValueError("В наборе допускается до 1000 кодов товаров.")
        entries = []
        seen = {set_gtin.zfill(14)}
        for raw_gtin, raw_quantity in self.components:
            gtin = _gtin(raw_gtin)
            if not (quantity := positive_id(raw_quantity)):
                raise ValueError(f"Укажите целое положительное количество для GTIN {gtin}.")
            key = gtin.zfill(14)
            if key in seen:
                raise ValueError(f"Вложение {gtin} повторяется или совпадает с GTIN набора.")
            seen.add(key)
            entries.append({"gtin": gtin, "quantity": int(quantity)})
        return entries


class SubmissionUnknown(transport.MarkingError):
    """Каталог мог принять запрос, но подтверждение не получено."""


@dataclass(frozen=True)
class CatalogAccount:
    contour: Contour
    inn: str
    thumbprint: str

    @classmethod
    def current(cls, contour: Contour) -> CatalogAccount:
        current = session.session
        if current.contour is not contour or not current.thumbprint or not current.inn:
            raise transport.AuthRequired(
                "Войдите по сертификату на вкладке «Проверка кодов» в выбранном контуре.")
        return cls(contour, current.inn, current.thumbprint)

    def _request(self, method: str, path: str, *, system: str = "catalog", **kwargs: Any) -> Any:
        def send(token: str) -> Any:
            current = session.session
            if self != self.current(self.contour) or current.token != token:
                raise transport.MarkingError("Организация или сеанс входа изменились.")
            return transport.request(method, system, path, contour=self.contour,
                                     token=token, **kwargs)

        if self != self.current(self.contour):
            raise transport.MarkingError("Организация или сеанс входа изменились.")
        return session.authorized(send)

    def _write(self, method: str, path: str, unknown: str, **kwargs: Any) -> Any:
        # Даже GET /generate-gtins резервирует код и расходует лимит.
        try:
            return self._request(method, path, retries=0, **kwargs)
        except (transport.Offline, transport.InvalidResponse) as error:
            raise SubmissionUnknown(unknown) from error
        except transport.MarkingError as error:
            if error.status >= 500:
                raise SubmissionUnknown(unknown) from error
            if error.status:
                service_name = ("Национальный каталог через True API"
                                if kwargs.get("system") == "trueapi" else "Национальный каталог")
                raise transport.MarkingError(
                    f"{service_name}, HTTP {error.status}.\n{error}", error.status
                ) from error
            raise

    def generate_gtin(self) -> str:
        answer = self._write("GET", "/generate-gtins", _UNKNOWN_GENERATION, params={"quantity": 1})
        result = answer.get("result") if isinstance(answer, dict) else None
        drafts = result.get("drafts") if isinstance(result, dict) else None
        if isinstance(drafts, list) and len(drafts) == 1 and isinstance(drafts[0], dict):
            gtin = drafts[0].get("gtin")
            if isinstance(gtin, str):
                try:
                    return _gtin(gtin)
                except ValueError:
                    pass
        raise SubmissionUnknown(_UNKNOWN_GENERATION)

    def create(self, draft: CardDraft) -> str:
        body = draft.payload()
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if draft.kind is CardKind.TECHNICAL:
            self._use_registered_tnved(body)
            # True API, § 10.17: массив карточек и массив числовых ID категорий.
            if "categories" in body:
                body["categories"] = [category["cat_id"] for category in body["categories"]]
            answer = self._write("POST", "/nk/feed", _UNKNOWN_SUBMISSION, system="trueapi",
                                 params={"format": "json"}, body=[body], headers=headers)
        else:
            answer = self._write("POST", "/feed", _UNKNOWN_SUBMISSION, body=body, headers=headers)
        result = answer.get("result") if isinstance(answer, dict) else None
        if isinstance(result, dict) and (feed_id := positive_id(result.get("feed_id"))):
            return feed_id
        raise SubmissionUnknown(_UNKNOWN_SUBMISSION)

    def _use_registered_tnved(self, body: dict[str, Any]) -> None:
        full_code = body["tnved"]
        attributes = body.get("good_attrs", [])
        if len(full_code) != 10 or any(
                item["attr_id"] == FULL_TNVED_ATTRIBUTE for item in attributes):
            return
        group = full_code[:4]
        try:
            answer = self._request("POST", "/mark-check",
                                   body={"tnveds": [full_code, group]}, retries=0)
        except transport.MarkingError:
            # Проверка справочника вспомогательная. При недоступности метода
            # используем введённый код, который проверит сам /feed.
            return
        result = answer.get("result") if isinstance(answer, dict) else None
        entries = result.get("tnveds") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            return
        statuses = {item["tnved"]: str(item.get("is_marked_code"))
                    for item in entries if isinstance(item, dict)
                    and isinstance(item.get("tnved"), str)}
        if statuses.get(full_code) == "2" and statuses.get(group) == "1":
            body["tnved"] = group
            body["good_attrs"] = [*attributes, {
                "attr_id": FULL_TNVED_ATTRIBUTE, "attr_value": full_code,
            }]

    def feed_status(self, feed_id: str) -> dict[str, Any]:
        requested = positive_id(feed_id)
        if not requested:
            raise ValueError("Введите числовой номер отправки.")
        result = self._result(self._request("GET", "/nk/feed-status", system="trueapi",
                                           params={"feed_id": requested, "format": "json"}), dict)
        if positive_id(result.get("feed_id")) != requested:
            raise transport.MarkingError("Каталог вернул данные другой отправки.")
        if not isinstance(result.get("status"), str) or not result["status"].strip():
            raise transport.MarkingError("Каталог не вернул статус отправки.")
        return result

    def search_tnved(self, prefix: str, group: str = "") -> list[tuple[str, str]]:
        prefix = prefix.strip()
        if prefix and not re.fullmatch(r"[0-9]{4,10}", prefix):
            raise ValueError("Для поиска ТН ВЭД введите от 4 до 10 цифр.")
        if not prefix and not group:
            raise ValueError("Введите хотя бы первые четыре цифры ТН ВЭД.")
        limit = 1000
        body: dict[str, Any] = {"page": 0, "limit": limit}
        if prefix:
            body["tnveds"] = [prefix]
        if group:
            body["pg"] = [group]
        codes: dict[str, str] = {}
        while True:
            answer = self._request("POST", "/tn-ved/search", system="trueapi4", body=body)
            if isinstance(answer, dict) and answer.get("errorMessage"):
                raise transport.MarkingError(str(answer["errorMessage"]))
            records = answer.get("tnveds") if isinstance(answer, dict) else None
            if not isinstance(records, list):
                raise transport.MarkingError("ГИС МТ вернула неожиданный ответ на поиск ТН ВЭД.")
            for item in records:
                if not isinstance(item, dict):
                    continue
                code, description = item.get("tnved"), item.get("description")
                if (isinstance(code, str) and re.fullmatch(r"[0-9]{10}", code)
                        and code.startswith(prefix) and (not group or item.get("pg") == group)):
                    codes.setdefault(code, description if isinstance(description, str) else "")
            total = positive_id(answer.get("total"))
            if (not records or answer.get("last") is True
                    or (total and (body["page"] + 1) * limit >= int(total))
                    or (not total and len(records) < limit)):
                return sorted(codes.items())
            body["page"] += 1

    def categories(self, tnved: str) -> list[dict]:
        params = {"tnved": validate_tnved(tnved)}
        return self._result(self._request("GET", "/categories", params=params), list)

    def attributes(self, tnved: str, category: str, kind: CardKind) -> list[dict]:
        if category.strip():
            if not (category_id := positive_id(category)):
                raise ValueError("Введите числовой идентификатор категории.")
            params = {"cat_id": category_id}
        else:
            params = {"tnved": validate_tnved(tnved)}
        if kind is CardKind.SET:
            params["is_set"] = "1"
        return self._result(self._request("GET", "/attributes", params=params), list)

    def missing_attribute_fields(
        self, draft: CardDraft, ids: tuple[str, ...],
    ) -> tuple[list[dict], dict[str, tuple[str, str]]]:
        schema = self.attributes(draft.tnved, draft.category, draft.kind)
        fields = {positive_id(item.get("attr_id")): item
                  for item in schema if isinstance(item, dict)}
        suggestions = {}
        if draft.source_gtin:
            try:
                suggestions = self._source_attribute_values(draft.source_gtin, ids)
            except (ValueError, transport.MarkingError):
                # Опубликованная карточка может быть недоступна этой организации.
                pass
        return [fields.get(attr_id, {"attr_id": attr_id}) for attr_id in ids], suggestions

    def _source_attribute_values(self, gtin: str, ids: tuple[str, ...]) -> dict[str, tuple[str, str]]:
        gtin = _gtin(gtin)
        records = self._result(self._request("GET", "/product", params={"gtin": gtin}, retries=0), list)
        matching = []
        for entry in records:
            if not isinstance(entry, dict):
                continue
            identifiers = entry.get("identified_by")
            if isinstance(identifiers, list) and any(
                    isinstance(item, dict) and item.get("type") == "gtin"
                    and isinstance(value := item.get("value"), str)
                    and value.zfill(14) == gtin.zfill(14) for item in identifiers):
                matching.append(entry)
        if len(matching) != 1:
            return {}
        attrs = matching[0].get("good_attrs")
        if not isinstance(attrs, list):
            return {}
        values: dict[str, set[tuple[str, str]]] = {}
        for item in attrs:
            if not isinstance(item, dict):
                continue
            attr_id = positive_id(item.get("attr_id"))
            value = item.get("attr_value")
            if attr_id in ids and isinstance(value, str) and value.strip():
                # Атрибуты другой упаковки могут иметь тот же ID.
                packaging = item.get("gtin")
                if packaging and str(packaging).zfill(14) != gtin.zfill(14):
                    continue
                values.setdefault(attr_id, set()).add((value, str(item.get("attr_value_type") or "")))
        return {attr_id: next(iter(options)) for attr_id, options in values.items() if len(options) == 1}

    @staticmethod
    def _result(answer: Any, expected: type) -> Any:
        result = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(result, expected):
            raise transport.MarkingError("Национальный каталог вернул неожиданный ответ.")
        return result


_UNKNOWN_SUBMISSION = ("Подтверждение отправки не получено. Проверьте карточки "
                       "в личном кабинете перед повторной отправкой.")
_UNKNOWN_GENERATION = ("Каталог не подтвердил генерацию GTIN. Проверьте черновики кодов "
                       "в личном кабинете перед повторной генерацией.")

_MISSING_ATTRIBUTE = re.compile(
    r"отсутствует\s+обязательный\s+(?:параметр|атрибут)\s+[#№]?\s*([0-9]+)\b",
    re.IGNORECASE,
)


def missing_attribute_ids(messages: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        attr_id for message in messages for match in _MISSING_ATTRIBUTE.finditer(message)
        if (attr_id := positive_id(match[1]))
    ))

# Атрибуты разрешительных документов из описания /v3/feed.
_PERMIT_ATTRIBUTES = {
    "CONFORMITY_DECLARATION": "23557",
    "CONFORMITY_CERTIFICATE": "23561",
    "REGISTRATION_CERTIFICATE": "23555",
}


def technical_card_blocked(info: CodeInfo) -> str:
    if not info.found:
        return "Сведения о товаре не получены при проверке кода."
    if info.package_type not in ("", "UNDEFINED", "UNIT"):
        return "Для технической карточки выберите код единицы товара."
    return ""


def technical_card_from_code(info: CodeInfo) -> CardDraft:
    """Поля карточки из ответа проверки кода единицы товара."""
    if blocked := technical_card_blocked(info):
        raise ValueError(blocked)
    nested = info.raw.get("cisInfo")
    source = {**info.raw, **nested} if isinstance(nested, dict) else info.raw
    brand = str(source.get("brand") or "").strip()
    tnved = str(source.get("tnVedEaes") or "").strip()
    if not re.fullmatch(r"[0-9]{10}", tnved):
        group = str(source.get("tnVedEaesGroup") or "").strip()
        tnved = group if re.fullmatch(r"[0-9]{4}", group) else ""
    attributes = []
    country = str(source.get("country") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", country):
        attributes.append(("2630", country, ""))
    for permit in info.permits or ():
        attr_id = _PERMIT_ATTRIBUTES.get(permit.kind.strip().upper())
        number = permit.number.strip()
        if not attr_id or not number or permit.state is not PermitState.VALID:
            continue
        try:
            issued = date.fromisoformat(permit.date.strip()[:10])
        except ValueError:
            continue
        attributes.append((attr_id, f"{number}:::{issued.isoformat()}", ""))
    return CardDraft(CardKind.TECHNICAL, info.product_name.strip(), brand, tnved,
                     attributes=tuple(dict.fromkeys(attributes)), source_gtin=info.gtin)
