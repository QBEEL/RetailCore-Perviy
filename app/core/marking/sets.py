"""Формирование наборов из ОСУ: документ «Формирование упаковки» (`AGGREGATION_DOCUMENT`, JSON).

Для товаров с объёмно-сортовым учётом (ОСУ), включая товарную группу
«Косметика и бытовая химия» (`cosmetics`), при приёмке по УПД передаются GTIN и
количество без поэкземплярных кодов маркировки. Поэтому при сборке набора из
таких товаров у участника на руках физические единицы товара с нанесёнными на
них кодами маркировки (`КИ`) и отдельно заказанный и нанесённый код
идентификации набора (`КИН`).

Программа не придумывает и не генерирует коды ни для набора, ни для вложений:
`КИН` и `КИ` считываются сканером с этикеток или загружаются из файла.

Документ уходит единым методом создания документов True API
(`POST /lk/documents/create?pg=<группа>`):
`{"document_format": "MANUAL", "product_document": <base64 JSON>,
"type": "AGGREGATION_DOCUMENT", "signature": <base64>}`.

По актуальной инструкции Честного ЗНАКа по формированию наборов (редакция от
19.10.2026):
- для товарных групп с ОСУ (без поэкземплярной прослеживаемости, в том числе
  `cosmetics`) проверка владельца вложенных КИ при формировании набора не
  блокирует операцию: после обработки документа вложенные КИ переходят в
  собственность участника, подавшего сведения о формировании набора;
- все вложенные КИ в наборе должны иметь одинаковый статус — либо все
  «В обороте» (`INTRODUCED`), либо все «Нанесён» (`APPLIED`);
- если вложенные КИ находятся в статусе «В обороте», то КИН набора до
  формирования должен быть в статусе «Нанесён» (`APPLIED`) с типом эмиссии
  «Произведён в РФ» (`LOCAL` / `PRODUCTION`), а после успешной обработки
  документа `AGGREGATION_DOCUMENT` набор автоматически переходит в статус
  «В обороте» без отдельного документа ввода в оборот;
- если вложенные КИ находятся в статусе «Нанесён», то КИН набора после
  формирования остаётся в статусе «Нанесён» и требует последующего ввода в
  оборот.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .. import appdata
from . import codes as codes_module
from .introduce import MAX_PRODUCTS, IntroduceProblem, _INN
from .models import CodeInfo, CodeState, group_of

DOCUMENT_TYPE = "AGGREGATION_DOCUMENT"
UNIT_TYPE_SET = "PRODUCT_SET"
AGGREGATION_TYPE = "AGGREGATION"
DEFAULT_PRODUCT_GROUP = "cosmetics"
DRAFT_FILE = "marking_sets_draft.json"

# В этом сценарии проверены правила ОСУ только для выбранной пользователем группы.
OSU_GROUPS = frozenset({"cosmetics"})

# Допустимые значения типа упаковки для КИН набора и для вложений, если ГИС МТ
# вернула это поле в `/cises/info`.
_SET_PACKAGE_TYPES = frozenset({"SET", "PRODUCT_SET", "LEVEL1"})
_ITEM_PACKAGE_TYPES = frozenset({"UNIT", "LEVEL0", "CIS"})

# Обозначения способа выпуска «Произведён в РФ» в ответах `/cises/info`.
_LOCAL_EMISSION_TYPES = frozenset({"LOCAL", "PRODUCTION", "RF"})
_REMAINS_EMISSION_TYPES = frozenset({"REMAINS", "OST"})


def key_of(raw_code: str) -> str:
    """Ключ сравнения кода: КИ без криптохвоста, иначе очищенная строка."""
    parsed = codes_module.parse(raw_code)
    return parsed.ki or codes_module.normalize(raw_code).strip()


@dataclass(frozen=True, slots=True)
class SetUnit:
    """Один формируемый набор: код набора (`КИН`) и коды вложений (`КИ`)."""

    set_code: str
    item_codes: tuple[str, ...]
    capacity: int | None = None

    @property
    def set_ki(self) -> str:
        return codes_module.parse(self.set_code).ki

    @property
    def set_gtin(self) -> str:
        return codes_module.parse(self.set_code).gtin

    @property
    def set_serial(self) -> str:
        return codes_module.parse(self.set_code).serial

    @property
    def item_kis(self) -> tuple[str, ...]:
        return tuple(codes_module.parse(code).ki for code in self.item_codes)

    @property
    def effective_capacity(self) -> int:
        if self.capacity is not None and self.capacity > 0:
            return int(self.capacity)
        return len(self.item_codes)

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.set_code.strip():
            found.append("не указан код идентификации набора (КИН)")
        else:
            parsed_set = codes_module.parse(self.set_code)
            if not parsed_set.ki or not parsed_set.valid:
                reason = parsed_set.problems[0] if parsed_set.problems else "неверный формат GS1"
                found.append(f"код набора (КИН) не разбирается: {reason}")
        if not self.item_codes:
            found.append("в наборе нет ни одного кода вложения (КИ)")
            return found

        broken = [code for code in self.item_codes if not codes_module.parse(code).valid]
        if broken:
            found.append(
                f"кодов вложений с ошибкой формата: {len(broken)} "
                f"(первый: {broken[0][:40]!r})"
            )

        keys = [key_of(code) for code in self.item_codes]
        if len(set(keys)) != len(keys):
            found.append("среди вложений набора есть повторяющиеся коды (КИ)")

        set_key = key_of(self.set_code) if self.set_code.strip() else ""
        if set_key and set_key in set(keys):
            found.append("код набора (КИН) совпадает с одним из кодов вложений")

        if self.capacity is not None and self.capacity > 0 and self.capacity < len(self.item_codes):
            found.append(
                f"вместимость набора ({self.capacity}) меньше числа вложений "
                f"({len(self.item_codes)})"
            )
        return found

    def as_json(self) -> dict[str, Any]:
        """Единица агрегации в формате True API `AGGREGATION_DOCUMENT`."""
        return {
            "aggregationType": AGGREGATION_TYPE,
            "sntins": [codes_module.parse(code).ki for code in self.item_codes],
            "unitSerialNumber": codes_module.parse(self.set_code).ki,
            "unitSerialNumberType": UNIT_TYPE_SET,
        }


@dataclass(frozen=True, slots=True)
class SetDocument:
    """Документ «Формирование упаковки» (`AGGREGATION_DOCUMENT`) для наборов."""

    inn: str
    units: tuple[SetUnit, ...]
    product_group: str = DEFAULT_PRODUCT_GROUP

    kind = "aggregation"
    title = "Формирование упаковки (набора)"
    wire_format = "MANUAL"

    @classmethod
    def single(
        cls,
        inn: str,
        set_code: str,
        item_codes: Sequence[str],
        *,
        product_group: str = DEFAULT_PRODUCT_GROUP,
        capacity: int | None = None,
    ) -> "SetDocument":
        return cls(
            inn=inn,
            units=(SetUnit(set_code=set_code, item_codes=tuple(item_codes), capacity=capacity),),
            product_group=product_group,
        )

    @property
    def all_codes(self) -> list[str]:
        """Все коды документа (сначала КИН каждого набора, затем его вложения)."""
        result: list[str] = []
        for unit in self.units:
            if unit.set_code.strip():
                result.append(unit.set_code)
            result.extend(unit.item_codes)
        return result

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not _INN.match(self.inn.strip()):
            found.append("ИНН участника — 10 цифр для организации, 12 для ИП")
        if not self.product_group.strip():
            found.append("не выбрана товарная группа")
        elif self.product_group.strip().lower() != DEFAULT_PRODUCT_GROUP:
            found.append("формирование наборов подключено только для товарной группы «Косметика и бытовая химия»")
        if not self.units:
            found.append("не добавлено ни одного набора")
            return found

        total_codes = 0
        seen_sets: set[str] = set()
        seen_items: set[str] = set()
        for index, unit in enumerate(self.units, start=1):
            prefix = f"набор №{index}: " if len(self.units) > 1 else ""
            for problem in unit.problems:
                found.append(f"{prefix}{problem}")
            set_key = key_of(unit.set_code) if unit.set_code.strip() else ""
            if set_key:
                if set_key in seen_sets:
                    found.append(f"{prefix}повторяющийся КИН набора {set_key}")
                if set_key in seen_items:
                    found.append(f"{prefix}КИН набора {set_key} уже числится среди вложений")
                seen_sets.add(set_key)
            for code in unit.item_codes:
                item_key = key_of(code)
                if item_key in seen_sets:
                    found.append(f"{prefix}код вложения {item_key} совпадает с КИН набора")
                elif len(self.units) > 1 and item_key in seen_items:
                    found.append(f"{prefix}код вложения {item_key} уже включён в другой набор")
                seen_items.add(item_key)
            total_codes += 1 + len(unit.item_codes)

        if total_codes > MAX_PRODUCTS:
            found.append(
                f"в одном документе не больше {MAX_PRODUCTS} кодов — разделите партию"
            )
        return found

    def wire_type(self, product_group: str = "") -> str:
        group = (product_group or self.product_group).strip()
        if not group:
            raise IntroduceProblem("Не указана товарная группа.")
        if group.lower() != DEFAULT_PRODUCT_GROUP or group.lower() != self.product_group.strip().lower():
            raise IntroduceProblem("Формирование наборов подключено только для группы «Косметика и бытовая химия».")
        return DOCUMENT_TYPE

    def render(self, extension: str = ".json") -> str:
        """Текст JSON `AGGREGATION_DOCUMENT` — он же подписывается и уходит в запрос."""
        if problems := self.problems:
            raise IntroduceProblem("Документ не собран: " + "; ".join(problems) + ".")
        body = {
            "participantId": self.inn.strip(),
            "aggregationUnits": [unit.as_json() for unit in self.units],
        }
        return json.dumps(body, ensure_ascii=False, indent=2) + "\n"

    @property
    def file_filters(self) -> str:
        return "JSON (*.json)"


@dataclass(frozen=True, slots=True)
class SetItemSummary:
    """Строка сводки состава набора по GTIN вложений."""

    gtin: str
    name: str
    quantity: int
    serials: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetComposition:
    """Предпросмотр состава набора перед формированием."""

    set_ki: str
    set_gtin: str
    set_serial: str
    set_name: str
    total_items: int
    distinct_gtins: int
    items: tuple[SetItemSummary, ...]


def composition(
    unit: SetUnit,
    *,
    checks: Mapping[str, CodeInfo] | None = None,
    names: Mapping[str, str] | None = None,
) -> SetComposition:
    """Собирает предпросмотр состава набора по сгруппированным GTIN вложений."""
    checks = checks or {}
    names = names or {}
    parsed_set = codes_module.parse(unit.set_code) if unit.set_code.strip() else None
    set_ki = parsed_set.ki if parsed_set else ""
    set_gtin = parsed_set.gtin if parsed_set else ""
    set_serial = parsed_set.serial if parsed_set else ""
    set_info = checks.get(set_ki) or checks.get(unit.set_code.strip())
    set_name = (
        (set_info.product_name if set_info else "")
        or names.get(set_ki, "")
        or names.get(set_gtin, "")
    )

    by_gtin: dict[str, list[str]] = {}
    gtin_names: dict[str, str] = {}
    for raw in unit.item_codes:
        parsed = codes_module.parse(raw)
        gtin = parsed.gtin or "—"
        ki = parsed.ki or raw.strip()
        by_gtin.setdefault(gtin, []).append(parsed.serial or ki)
        if not gtin_names.get(gtin):
            info = checks.get(ki) or checks.get(raw.strip())
            resolved = (
                (info.product_name if info else "")
                or names.get(ki, "")
                or names.get(gtin, "")
            )
            if resolved:
                gtin_names[gtin] = resolved

    summaries = tuple(
        SetItemSummary(
            gtin=gtin,
            name=gtin_names.get(gtin, ""),
            quantity=len(serials),
            serials=tuple(serials),
        )
        for gtin, serials in by_gtin.items()
    )
    return SetComposition(
        set_ki=set_ki,
        set_gtin=set_gtin,
        set_serial=set_serial,
        set_name=set_name,
        total_items=len(unit.item_codes),
        distinct_gtins=len(summaries),
        items=summaries,
    )


@dataclass(frozen=True, slots=True)
class SetVerification:
    """Результат проверки статусов КИН и вложений в ГИС МТ перед отправкой."""

    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    code_notes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_codes(
    unit: SetUnit,
    checks: Mapping[str, CodeInfo],
    *,
    our_inn: str = "",
    product_group: str = DEFAULT_PRODUCT_GROUP,
) -> SetVerification:
    """Проверяет КИН и КИ вложений по правилам ГИС МТ для формирования наборов."""
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    code_notes: dict[str, str] = {}

    group = product_group.strip().lower()
    group_title = group_of(group).title if group_of(group) else group
    is_osu = group in OSU_GROUPS
    clean_inn = our_inn.strip()

    for problem in unit.problems:
        problems.append(problem)
    if problems:
        return SetVerification(
            problems=tuple(problems),
            warnings=tuple(warnings),
            notes=tuple(notes),
            code_notes=code_notes,
        )

    set_ki = unit.set_ki
    set_info = checks.get(set_ki) or checks.get(unit.set_code.strip())
    if set_info is None:
        problems.append("код набора (КИН) ещё не проверен в «Честном ЗНАКе»")
    else:
        _verify_set_code(
            set_ki,
            set_info,
            clean_inn=clean_inn,
            group=group,
            group_title=group_title,
            problems=problems,
            warnings=warnings,
            code_notes=code_notes,
        )

    item_states: list[CodeState] = []
    item_emissions: set[str] = set()
    foreign_owners: set[str] = set()
    item_gtins: set[str] = set()

    for raw in unit.item_codes:
        parsed = codes_module.parse(raw)
        ki = parsed.ki or raw.strip()
        if parsed.gtin:
            item_gtins.add(parsed.gtin)
        info = checks.get(ki) or checks.get(raw.strip())
        if info is None:
            problems.append(f"код вложения {ki} ещё не проверен в «Честном ЗНАКе»")
            code_notes[ki] = "не проверен в ЧЗ"
            continue
        _verify_item_code(
            ki,
            info,
            clean_inn=clean_inn,
            group=group,
            group_title=group_title,
            is_osu=is_osu,
            problems=problems,
            warnings=warnings,
            code_notes=code_notes,
            item_states=item_states,
            item_emissions=item_emissions,
            foreign_owners=foreign_owners,
        )

    # Сверка сочетания статусов вложений и КИН по правилам ЦРПТ (19.10.2026).
    unique_states = set(item_states)
    if len(unique_states) > 1:
        state_names = ", ".join(sorted(state.title for state in unique_states))
        problems.append(
            f"вложения набора имеют разные статусы ({state_names}) — все КИ "
            "в наборе должны быть либо «В обороте», либо «Нанесён»"
        )
    elif len(unique_states) == 1 and set_info is not None and set_info.found:
        common_state = next(iter(unique_states))
        set_emission = set_info.emission_type.strip().upper()
        if common_state is CodeState.INTRODUCED:
            if not set_emission:
                problems.append("способ выпуска КИН не получен от ЧЗ — нельзя подтвердить возможность формирования набора")
                code_notes[set_ki] = "способ выпуска неизвестен"
            elif set_emission not in _LOCAL_EMISSION_TYPES:
                problems.append(
                    f"при формировании набора из товаров «В обороте» КИН должен "
                    f"иметь способ выпуска «Произведён в РФ» (получено: {set_emission})"
                )
                code_notes[set_ki] = f"нужен выпуск РФ, а не {set_emission}"
            elif set_info.state is CodeState.APPLIED:
                notes.append(
                    "Все вложения в статусе «В обороте», КИН в статусе «Нанесён» — "
                    "после обработки документа набор автоматически перейдёт в статус "
                    "«В обороте»."
                )
        elif common_state is CodeState.APPLIED:
            if len(item_gtins) > 1 and (
                set_emission in _REMAINS_EMISSION_TYPES
                or bool(item_emissions & _REMAINS_EMISSION_TYPES)
            ):
                problems.append(
                    "для мультитоварного набора (разные GTIN) нельзя использовать "
                    "коды со способом выпуска «Маркировка остатков»"
                )
            if len(item_emissions) > 1:
                problems.append(
                    "вложения в статусе «Нанесён» имеют разные способы выпуска: "
                    + ", ".join(sorted(item_emissions))
                )
            elif len(item_emissions) == 1 and set_emission:
                item_emission = next(iter(item_emissions))
                if not _same_emission(set_emission, item_emission):
                    problems.append(
                        f"у КИН набора способ выпуска ({set_emission}) не совпадает "
                        f"со способом выпуска вложений ({item_emission})"
                    )
            if set_info.state is CodeState.APPLIED and not problems:
                notes.append(
                    "Вложения и КИН в статусе «Нанесён» — после формирования набора "
                    "КИН останется в статусе «Нанесён», потребуется отдельный ввод "
                    "в оборот."
                )

    if foreign_owners and is_osu:
        owners_list = ", ".join(sorted(foreign_owners)[:3])
        notes.append(
            f"ОСУ ({group_title}): вложения числятся за поставщиком ({owners_list}) — "
            "при формировании набора они перейдут в собственность вашей организации."
        )

    return SetVerification(
        problems=tuple(dict.fromkeys(problems)),
        warnings=tuple(dict.fromkeys(warnings)),
        notes=tuple(dict.fromkeys(notes)),
        code_notes=code_notes,
    )


def _verify_set_code(
    set_ki: str,
    info: CodeInfo,
    *,
    clean_inn: str,
    group: str,
    group_title: str,
    problems: list[str],
    warnings: list[str],
    code_notes: dict[str, str],
) -> None:
    remarks: list[str] = []
    if not info.found:
        problems.append("код набора (КИН) не найден в «Честном ЗНАКе»")
        remarks.append("не найден в ЧЗ")
    elif not info.valid:
        detail = "; ".join(info.problems) if info.problems else "не признан системой"
        problems.append(f"код набора (КИН) недействителен: {detail}")
        remarks.append(detail)
    else:
        if not info.product_group:
            problems.append("товарная группа кода набора (КИН) не получена от ЧЗ")
            remarks.append("товарная группа неизвестна")
        elif group and info.product_group != group:
            problems.append(
                f"код набора (КИН) относится к группе «{info.product_group}», "
                f"а выбрана «{group_title}»"
            )
            remarks.append(f"группа {info.product_group}")
        if not info.package_type:
            problems.append("тип упаковки кода набора (КИН) не получен от ЧЗ")
            remarks.append("тип упаковки неизвестен")
        elif info.package_type not in _SET_PACKAGE_TYPES:
            problems.append(
                f"вместо кода набора (КИН) отсканирован код с типом упаковки "
                f"«{info.package_type}» (ожидался SET)"
            )
            remarks.append(f"тип упаковки {info.package_type}, нужен SET")
        if clean_inn and not info.owner_inn.strip():
            problems.append("владелец кода набора (КИН) не получен от ЧЗ")
            remarks.append("владелец неизвестен")
        elif clean_inn and info.owner_inn.strip() != clean_inn:
            owner_label = info.owner_name or info.owner_inn
            problems.append(
                f"код набора (КИН) принадлежит другому участнику ({owner_label})"
            )
            remarks.append(f"чужой КИН ({info.owner_inn})")
        if info.state is CodeState.EMITTED:
            problems.append(
                "код набора (КИН) в статусе «Эмитирован» — перед формированием "
                "набора нужен отчёт о нанесении (статус «Нанесён»)"
            )
            remarks.append("нужен отчёт о нанесении")
        elif info.state is CodeState.INTRODUCED:
            if info.children:
                problems.append(
                    f"код набора (КИН) уже в обороте и содержит вложений: "
                    f"{len(info.children)}"
                )
            else:
                problems.append(
                    "код набора (КИН) уже в статусе «В обороте» — для формирования "
                    "нового набора нужен КИН в статусе «Нанесён»"
                )
            remarks.append("уже в обороте")
        elif info.state is not CodeState.APPLIED:
            problems.append(
                f"код набора (КИН) в статусе «{info.state.title}» — для формирования "
                "набора нужен статус «Нанесён»"
            )
            remarks.append(f"статус: {info.state.title}")
        elif info.children:
            warnings.append(
                f"у кода набора (КИН) в ЧЗ уже указаны вложения ({len(info.children)})"
            )
            remarks.append(f"уже есть вложения ({len(info.children)})")
    if remarks:
        code_notes[set_ki] = "; ".join(remarks)


def _verify_item_code(
    ki: str,
    info: CodeInfo,
    *,
    clean_inn: str,
    group: str,
    group_title: str,
    is_osu: bool,
    problems: list[str],
    warnings: list[str],
    code_notes: dict[str, str],
    item_states: list[CodeState],
    item_emissions: set[str],
    foreign_owners: set[str],
) -> None:
    remarks: list[str] = []
    if not info.found:
        problems.append(f"код вложения {ki} не найден в «Честном ЗНАКе»")
        code_notes[ki] = "не найден в ЧЗ"
        return
    if not info.valid:
        detail = "; ".join(info.problems) if info.problems else "не признан системой"
        problems.append(f"код вложения {ki} недействителен: {detail}")
        code_notes[ki] = detail
        return

    raw_info = info.raw.get("cisInfo", info.raw) if isinstance(info.raw, dict) else {}
    if isinstance(raw_info, dict):
        special = str(raw_info.get("statusEx") or raw_info.get("statusExt") or "").strip().upper()
        if special not in ("", "EMPTY", "WAIT_TRANSFER_TO_OWNER"):
            problems.append(f"код вложения {ki} имеет особое состояние «{special}»")
            remarks.append(f"особое состояние {special}")
        parent = str(raw_info.get("parent") or "").strip()
        if parent:
            problems.append(f"код вложения {ki} уже находится в упаковке {parent}")
            remarks.append("уже вложен в упаковку")

    if not info.product_group:
        problems.append(f"товарная группа кода вложения {ki} не получена от ЧЗ")
        remarks.append("товарная группа неизвестна")
    elif group and info.product_group != group:
        problems.append(
            f"код вложения {ki} относится к группе «{info.product_group}», "
            f"а выбрана «{group_title}»"
        )
        remarks.append(f"группа {info.product_group}")

    if not info.package_type:
        problems.append(f"тип упаковки кода вложения {ki} не получен от ЧЗ")
        remarks.append("тип упаковки неизвестен")
    elif info.package_type not in _ITEM_PACKAGE_TYPES:
        problems.append(
            f"код вложения {ki} имеет неподходящий тип упаковки «{info.package_type}»"
        )
        remarks.append(f"тип {info.package_type}")

    if info.state not in (CodeState.INTRODUCED, CodeState.APPLIED):
        problems.append(
            f"код вложения {ki} в статусе «{info.state.title}» — допускаются "
            "только «В обороте» или «Нанесён»"
        )
        remarks.append(f"недопустимый статус: {info.state.title}")
    else:
        item_states.append(info.state)

    if info.emission_type.strip():
        item_emissions.add(info.emission_type.strip().upper())

    owner_inn = info.owner_inn.strip()
    if clean_inn and owner_inn and owner_inn != clean_inn:
        owner_label = f"{info.owner_name} ({owner_inn})" if info.owner_name else owner_inn
        if is_osu:
            foreign_owners.add(owner_label)
            remarks.append(f"ОСУ: переход от {owner_inn}")
        else:
            problems.append(
                f"код вложения {ki} принадлежит другому участнику ({owner_label})"
            )
            remarks.append(f"чужой код ({owner_inn})")

    if info.permit_state.troubled and info.permit_state.remark:
        warnings.append(f"{ki}: {info.permit_state.remark}")
        remarks.append(info.permit_state.remark)

    if remarks:
        code_notes[ki] = "; ".join(remarks)


def _same_emission(first: str, second: str) -> bool:
    if first == second:
        return True
    if first in _LOCAL_EMISSION_TYPES and second in _LOCAL_EMISSION_TYPES:
        return True
    if first in _REMAINS_EMISSION_TYPES and second in _REMAINS_EMISSION_TYPES:
        return True
    return False


# --- черновик набора --------------------------------------------------------------

@dataclass(slots=True)
class SetDraft:
    """Черновик формируемого набора, переживающий закрытие программы."""

    inn: str = ""
    product_group: str = DEFAULT_PRODUCT_GROUP
    set_code: str = ""
    capacity: int = 0
    item_codes: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    operation_id: int = 0
    doc_id: str = ""
    doc_status: str = ""
    updated_at: str = ""

    @property
    def has_pending_doc(self) -> bool:
        return bool(self.doc_id.strip())


def draft_path() -> str:
    return appdata.path_to(DRAFT_FILE)


def save_draft(draft: SetDraft, path: str | None = None) -> SetDraft:
    """Атомарно сохраняет черновик набора на диск."""
    target = path or draft_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    draft.updated_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "inn": draft.inn,
        "product_group": draft.product_group or DEFAULT_PRODUCT_GROUP,
        "set_code": draft.set_code,
        "capacity": int(draft.capacity or 0),
        "item_codes": list(draft.item_codes),
        "names": dict(draft.names),
        "operation_id": int(draft.operation_id or 0),
        "doc_id": draft.doc_id,
        "doc_status": draft.doc_status,
        "updated_at": draft.updated_at,
    }
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return draft


def load_draft(path: str | None = None) -> SetDraft:
    """Читает сохранённый черновик набора. При отсутствии или порче — пустой."""
    target = path or draft_path()
    if not os.path.exists(target):
        return SetDraft()
    try:
        with open(target, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return SetDraft()
        raw_items = data.get("item_codes")
        item_codes = (
            [str(item) for item in raw_items if str(item).strip()]
            if isinstance(raw_items, list) else []
        )
        raw_names = data.get("names")
        names = (
            {str(k): str(v) for k, v in raw_names.items() if str(k).strip() and str(v).strip()}
            if isinstance(raw_names, dict) else {}
        )
        return SetDraft(
            inn=str(data.get("inn") or ""),
            product_group=str(data.get("product_group") or DEFAULT_PRODUCT_GROUP),
            set_code=str(data.get("set_code") or ""),
            capacity=int(data.get("capacity") or 0),
            item_codes=item_codes,
            names=names,
            operation_id=int(data.get("operation_id") or 0),
            doc_id=str(data.get("doc_id") or ""),
            doc_status=str(data.get("doc_status") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )
    except (OSError, ValueError, TypeError):
        return SetDraft()


def clear_draft(path: str | None = None) -> None:
    """Удаляет черновик набора с диска."""
    target = path or draft_path()
    try:
        if os.path.exists(target):
            os.remove(target)
    except OSError:
        pass


def file_name(
    set_gtin: str = "",
    moment: datetime | None = None,
    extension: str = ".json",
) -> str:
    moment = moment or datetime.now()
    middle = f" {set_gtin}" if set_gtin.strip() else ""
    return f"Формирование набора{middle} {moment:%Y-%m-%d %H-%M}{extension}"
