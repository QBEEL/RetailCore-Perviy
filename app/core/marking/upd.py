"""Чтение УПД с кодами маркировки — того XML, что приходит по ЭДО.

Документ поступает в Диадок, а на склад приезжает коробка, и вопрос всегда
один: то ли в коробке, что в документе. Пока коннектор Диадока с 1С не работает,
ответить на него негде — в веб-версии сверка сканером включается отдельной
платной услугой, а в 1С коды попадают только вместе с электронным документом.
Разбор здесь закрывает эту дыру: XML лежит в папке с документом, и всё, что
нужно для сверки, в нём уже есть.

Формат — приказ ФНС ММВ-7-15/155@ и его продолжения (версии 5.01–5.03).
Пространств имён в нём нет, но имена тегов между версиями не менялись, поэтому
читается он одинаково. Данные лежат в атрибутах, а не в тексте узлов: `СведТов`
несёт название, количество и цену, вложенный `ДопСведТов` — код товара, а коды
экземпляров сидят ещё уровнем ниже, в `НомСредИдентТов`.

Кодировка почти всегда windows-1251, и объявлена она в самом файле — разбор
читает её оттуда, а не угадывает. Это тот случай, когда догадка обошлась бы
дороже: cp1251, прочитанный как utf-8, даёт не ошибку, а мусор вместо названий.
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

# Чем в документе бывают записаны средства идентификации. КИЗ — код экземпляра,
# то, что наклеено на товар. НомУпак и ИдентТрансУпак — коды упаковок: их тоже
# сканируют, и молча выбрасывать их значит объявить недостачей целую коробку.
MARK_TAGS = {
    "КИЗ": "КИЗ",
    "НомУпак": "упаковка",
    "ИдентТрансУпак": "транспортная упаковка",
}

# Имя файла УПД внутри архива, скачанного из Диадока. Рядом лежат подписи,
# протокол и печатная форма — среди них нужен один.
ARCHIVE_PREFIX = "ON_NSCHFDOPPR"


class UpdProblem(Exception):
    """Документ не читается. Текст пригоден для показа пользователю."""


@dataclass(slots=True)
class Mark:
    """Одно средство идентификации из документа."""

    value: str
    kind: str = "КИЗ"


@dataclass(slots=True)
class Line:
    """Строка товара вместе с кодами, которые за ней числятся."""

    number: str = ""
    name: str = ""
    gtin: str = ""
    unit: str = ""
    quantity: float = 0.0
    price: float = 0.0
    total: float = 0.0
    marks: list[Mark] = field(default_factory=list)

    @property
    def marked(self) -> bool:
        return bool(self.marks)

    @property
    def shortage(self) -> int:
        """На сколько кодов в документе меньше, чем товара в строке.

        Поставщик обязан перечислить каждый экземпляр, и нехватка кодов — это
        расхождение самого документа, заметное до всякого сканирования. Считать
        его недостачей товара нельзя: товар-то приехал, не хватает как раз
        бумаги, и разговор с поставщиком тут другой.
        """
        if not self.marked:
            return 0
        return max(0, int(round(self.quantity)) - len(self.marks))

    @property
    def title(self) -> str:
        return self.name or f"Строка {self.number}"


@dataclass(slots=True)
class Document:
    """УПД: реквизиты и строки товара."""

    number: str = ""
    date: str = ""
    seller: str = ""
    seller_inn: str = ""
    buyer: str = ""
    buyer_inn: str = ""
    lines: list[Line] = field(default_factory=list)
    source: str = ""

    @property
    def marks(self) -> list[tuple[Line, Mark]]:
        """Все коды документа вместе со строками, к которым они относятся."""
        return [(line, mark) for line in self.lines for mark in line.marks]

    @property
    def marked_lines(self) -> list[Line]:
        return [line for line in self.lines if line.marked]

    @property
    def title(self) -> str:
        number = self.number or "без номера"
        return f"УПД № {number} от {self.date}" if self.date else f"УПД № {number}"

    @property
    def summary(self) -> str:
        """Одна строка о документе — та, что показывается под кнопкой загрузки."""
        parts = [self.title]
        if self.seller:
            inn = f", ИНН {self.seller_inn}" if self.seller_inn else ""
            parts.append(f"поставщик: {self.seller}{inn}")
        parts.append(f"строк с маркировкой: {len(self.marked_lines)}")
        parts.append(f"кодов: {len(self.marks)}")
        if shortage := sum(line.shortage for line in self.lines):
            parts.append(f"кодов не хватает на {shortage} шт. товара")
        return " · ".join(parts)


def read(path: str) -> Document:
    """Читает УПД из XML или из архива, скачанного в Диадоке."""
    if not path or not os.path.exists(path):
        raise UpdProblem("Файл не найден")
    root = _root(path)
    document = _document(root)
    document.source = path
    if not document.lines:
        raise UpdProblem(
            "В документе нет ни одной строки товара — похоже, это не УПД")
    if not document.marks:
        raise UpdProblem(
            "В документе нет кодов маркировки. Сверять нечего: поставщик их "
            "не указал, и это повод вернуть документ на уточнение")
    return document


def _root(path: str) -> ET.Element:
    """Корень XML. Архив из Диадока распознаётся и распаковывается на лету."""
    try:
        if zipfile.is_zipfile(path):
            return _from_archive(path)
        return ET.parse(path).getroot()
    except ET.ParseError as error:
        raise UpdProblem(f"Файл не разбирается как XML: {error}") from error
    except OSError as error:
        raise UpdProblem(f"Не удалось прочитать файл: {error}") from error


def _from_archive(path: str) -> ET.Element:
    """Файл УПД из архива Диадока.

    В архиве лежат ещё подписи, протокол и печатная форма. Нужный отбирается по
    имени, а если имя незнакомо — по содержимому: тот xml, в котором есть
    таблица товаров. Требовать распаковать архив ради одного файла незачем.
    """
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist()
                 if name.lower().endswith(".xml")]
        wanted = [name for name in names
                  if os.path.basename(name).startswith(ARCHIVE_PREFIX)]
        for name in wanted + [name for name in names if name not in wanted]:
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            if _find(root, "ТаблСчФакт") is not None:
                return root
    raise UpdProblem(
        "В архиве нет файла УПД. Выберите сам XML — он лежит в папке рядом "
        "с печатной формой")


def _document(root: ET.Element) -> Document:
    """Реквизиты и строки. Незнакомые узлы пропускаются, а не ломают разбор."""
    document = Document()
    invoice = _find(root, "СвСчФакт")
    if invoice is not None:
        document.number = invoice.get("НомерДок", "")
        document.date = invoice.get("ДатаДок", "")
        seller = _find(invoice, "СвПрод")
        buyer = _find(invoice, "СвПокуп")
        document.seller, document.seller_inn = _party(seller)
        document.buyer, document.buyer_inn = _party(buyer)
    for node in root.iter():
        if _tag(node) == "СведТов":
            document.lines.append(_line(node))
    return document


def _party(node: ET.Element | None) -> tuple[str, str]:
    """Название и ИНН участника. Организация и предприниматель записаны по-разному."""
    if node is None:
        return "", ""
    if (company := _find(node, "СвЮЛУч")) is not None:
        return company.get("НаимОрг", ""), company.get("ИННЮЛ", "")
    if (person := _find(node, "СвИП")) is not None:
        inn = person.get("ИННФЛ", "")
        name = _find(person, "ФИО")
        if name is None:
            return "", inn
        parts = (name.get("Фамилия", ""), name.get("Имя", ""),
                 name.get("Отчество", ""))
        return " ".join(part for part in parts if part).strip(), inn
    return "", ""


def _line(node: ET.Element) -> Line:
    """Строка товара вместе с кодами из вложенных узлов."""
    line = Line(
        number=node.get("НомСтр", ""),
        name=node.get("НаимТов", ""),
        unit=node.get("НаимЕдИзм", ""),
        quantity=_number(node.get("КолТов")),
        price=_number(node.get("ЦенаТов")),
        total=_number(node.get("СтТовУчНал") or node.get("СтТовБезНДС")),
    )
    for child in node.iter():
        tag = _tag(child)
        if tag == "ДопСведТов" and not line.gtin:
            line.gtin = child.get("КодТов", "")
        elif tag in MARK_TAGS and (value := (child.text or "").strip()):
            line.marks.append(Mark(value=value, kind=MARK_TAGS[tag]))
    return line


def _find(node: ET.Element, name: str) -> ET.Element | None:
    """Первый потомок с таким именем, на любой глубине и без учёта пространства имён."""
    for child in node.iter():
        if child is not node and _tag(child) == name:
            return child
    return None


def _tag(node: ET.Element) -> str:
    """Имя тега без пространства имён.

    В документах ФНС его нет, но файл приходит от чужой программы, и один
    префикс превратил бы разбор в пустой документ без единого сообщения.
    """
    return str(node.tag).rpartition("}")[2]


def _number(text: str | None) -> float:
    """Число из атрибута. Разделителем бывает и точка, и запятая."""
    value = (text or "").strip().replace(",", ".").replace(" ", "")
    try:
        return float(value)
    except ValueError:
        return 0.0
