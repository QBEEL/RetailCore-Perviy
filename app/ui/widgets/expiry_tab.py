"""Вкладка «Срок годности»: склад пикает QR и видит, до какого числа товар годен.

Работа та же, что и при сверке с УПД: вещь в руках, следующая в коробке, и ответ
должен быть виден раньше, чем прочитан. Поэтому он вынесен в широкую плашку с
цветом во всю ширину — красное значит «просрочен», жёлтое «скоро», зелёное
«годен». Журнал ниже нужен не для чтения по ходу дела, а чтобы в конце смены
отсортировать и увидеть, что лежало с самым коротким сроком.

Главное отличие от сверки в том, что QR несёт целую карточку из десятков строк, а
сканер печатает её как клавиатура и после каждой строки жмёт Enter. Поле, которое
принимает код по Enter, приняло бы по скану на каждую строку. Поэтому ввод
копится, и скан считается законченным, когда сканер замолчал: он печатает
сотни знаков в секунду, а между сканами проходят секунды.

У парфюмерии срока в ответе системы нет — только дата производства. Тогда срок
считается от неё: три года, у отдельных брендов пять (список — кнопкой «Сроки по
брендам»). Расчётный срок помечен «≈» и словом «расчёт», чтобы его не принимали за
данные системы.

Бывает и так, что на упаковке не карточка, а обычный код маркировки — квадратный
DataMatrix. Срока в нём нет: в коде лежат только товар и серийный номер. Тогда срок
спрашивается у «Честного ЗНАКа» по этому коду (так делают и другие программы,
показывающие карточку), и для этого нужен вход по сертификату на вкладке
«Проверка кодов». Если он не выполнен, вкладка говорит об этом прямо.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import codes as codes_module, expiry, service, upd as upd_module
from ...core.marking.expiry import Card as ProductCard, ExpiryLevel
from ...core.marking.models import CodeInfo
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, SectionTitle
from .table import Column, DataTable
from .toast import ToastKind

# Сколько сканер должен молчать, чтобы скан считался законченным. Он печатает
# знак за единицы миллисекунд, а человеку между двумя сканами нужны секунды.
SCAN_IDLE_MS = 300

# Цвет и фон ответа. Отдельно от `Verdict` сверки: там вопрос «есть ли в
# документе», здесь — «годен ли», и различаются они не названиями, а срочностью.
LEVEL_COLORS: dict[ExpiryLevel, tuple[str, str]] = {
    ExpiryLevel.OK: (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    ExpiryLevel.SOON: (Palette.WARNING, Palette.WARNING_SOFT),
    ExpiryLevel.EXPIRED: (Palette.DANGER, Palette.DANGER_SOFT),
    ExpiryLevel.UNKNOWN: (Palette.TEXT_MUTED, Palette.SURFACE_ALT),
}

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
FAR = 10 ** 9


class ScanBuffer(QPlainTextEdit):
    """Поле, собирающее скан из любого числа строк в одно сообщение."""

    scanned = Signal(str)

    def __init__(self, parent: QWidget | None = None, idle_ms: int = SCAN_IDLE_MS) -> None:
        super().__init__(parent)
        self.setPlaceholderText(
            "Наведите сканер на QR или на код маркировки (DataMatrix) — содержимое "
            "придёт сюда само. Можно и вставить текст из буфера (Ctrl+V).")
        self.setFixedHeight(64)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(idle_ms)
        self._timer.timeout.connect(self.flush)
        self.textChanged.connect(self._timer.start)

    def flush(self) -> None:
        """Отдаёт накопленное как один скан и очищает поле.

        Очистка нужна всегда: оставленный текст сканер допишет следующим, и
        следующая вещь окажется «не разобрана» по нашей вине.
        """
        self._timer.stop()
        text = self.toPlainText().strip()
        self.blockSignals(True)
        self.clear()
        self.blockSignals(False)
        if text:
            self.scanned.emit(text)


@dataclass(slots=True)
class Entry:
    """Одна строка журнала: что отсканировали и когда."""

    card: ProductCard
    day: date
    at: datetime = field(default_factory=datetime.now)
    repeat: bool = False
    # Срока в самом коде нет, ответ «Честного ЗНАКа» ещё идёт.
    pending: bool = False
    source: str = "QR"
    # Откуда код попал в журнал, если не со сканера: «УПД № 3349 от 09.09.2026».
    origin: str = ""

    @property
    def days(self) -> int | None:
        return expiry.days_left(self.card.expires, self.day)


class ShelfLifeDialog(QDialog):
    """Правила расчёта срока: сколько лет по умолчанию и бренды с другим сроком."""

    def __init__(self, years: int, brands: dict[str, int],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Сроки по брендам")
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(
            "Если «Честный ЗНАК» не отдаёт срок годности (так у парфюмерии), он "
            "считается от даты производства. Такой срок помечается «≈» и «расчёт».",
            self))
        form = QFormLayout()
        self.years = QSpinBox(self)
        self.years.setRange(1, 30)
        self.years.setSuffix(" л.")
        self.years.setValue(years)
        form.addRow("Срок по умолчанию", self.years)
        root.addLayout(form)
        root.addWidget(QLabel("Бренды с другим сроком — по одному в строке: «Бренд = лет»", self))
        self.brands = QPlainTextEdit(self)
        self.brands.setPlaceholderText("POLUBVI = 5\nНазвание бренда = 5")
        self.brands.setPlainText(expiry.format_brand_years(brands))
        self.brands.setMinimumHeight(140)
        root.addWidget(self.brands)
        self.error = QLabel("", self)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {Palette.DANGER};")
        root.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.result_brands: dict[str, int] = dict(brands)

    def _accept(self) -> None:
        parsed, errors = expiry.parse_brand_years(self.brands.toPlainText())
        if errors:
            # Молча пропустить строку нельзя: бренд остался бы со сроком по
            # умолчанию, и склад получил бы неверную дату без единого сигнала.
            self.error.setText("\n".join(errors))
            return
        self.result_brands = parsed
        self.accept()


def ask_gis(code: str) -> CodeInfo:
    """Спрашивает «Честный ЗНАК» о коде. Свежий ответ, а не из кэша: срок читается
    из самого ответа, а в кэше проверки его не хранится."""
    return service.check([code], use_cache=False)[0]


def ask_gis_many(codes: list[str]) -> dict[str, CodeInfo]:
    """Спрашивает «Честный ЗНАК» о многих кодах разом — пачками по пятьдесят.

    Возвращает ответы по исходным кодам: система отвечает по очищенному от
    криптохвоста, и сопоставить их приходится здесь, а не вызывающему.
    """
    answered = {item.code: item for item in service.check(codes, use_cache=False)}
    found: dict[str, CodeInfo] = {}
    for code in codes:
        if (item := answered.get(codes_module.for_request(code))) is not None:
            found[code] = item
    return found


def left_text(days: int | None) -> str:
    """«осталось 849 дн.», «сегодня последний день», «просрочен на 5 дн.»."""
    if days is None:
        return ""
    if days < 0:
        return f"просрочен на {-days} дн."
    if days == 0:
        return "сегодня последний день"
    return f"осталось {days} дн."


class ExpiryTab(QWidget):
    """Скан QR → срок годности: плашка с ответом и журнал сканов."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, object], None] | None = None,
        parent: QWidget | None = None,
        lookup: Callable[[str], CodeInfo] | None = None,
        lookup_many: Callable[[list[str]], dict[str, CodeInfo]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.entries: list[Entry] = []
        # Кто отвечает за код без срока. Подменяется в тестах: сеть и КриптоПро
        # там не нужны.
        self._lookup = lookup or ask_gis
        # Пачка кодов спрашивается разом. Если подменён только одиночный ответ,
        # пачка спрашивается им же по одному — для тестов этого достаточно.
        self._lookup_many = lookup_many or (
            (lambda codes: {code: lookup(code) for code in codes})
            if lookup else ask_gis_many)
        # Уже полученные в этой сессии ответы по ключу экземпляра: повторный скан
        # того же товара не должен снова ходить в сеть.
        self._resolved: dict[str, ProductCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, Metrics.GAP, 0, 0)
        root.setSpacing(Metrics.GAP)
        root.addWidget(self._scan_card())
        root.addWidget(self._journal_card(), 1)

    # --- построение ----------------------------------------------------------------

    def _scan_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Сканирование QR", card))
        header.addStretch(1)
        self.upd_button = QPushButton("Загрузить УПД…", card)
        self.upd_button.setIcon(icons.icon("open"))
        self.upd_button.setToolTip(
            "Взять все коды из УПД и показать срок годности каждого: срок "
            "спросится у «Честного ЗНАКа» разом на весь документ. Нужен вход "
            "по сертификату")
        self.upd_button.clicked.connect(lambda _checked=False: self.load_upd())
        header.addWidget(self.upd_button)
        self.rules_button = QPushButton("Сроки по брендам…", card)
        self.rules_button.setIcon(icons.icon("calendar"))
        self.rules_button.setToolTip(
            "Как считать срок, если система его не отдаёт: по умолчанию и по брендам")
        self.rules_button.clicked.connect(self.edit_rules)
        header.addWidget(self.rules_button)
        header.addWidget(QLabel("Скоро — менее", card))
        self.soon_box = QSpinBox(card)
        self.soon_box.setRange(1, 3650)
        self.soon_box.setSuffix(" дн.")
        self.soon_box.setValue(self.settings.marking_expiry_soon_days)
        self.soon_box.setToolTip(
            "За сколько дней до конца срока товар помечается жёлтым")
        self.soon_box.valueChanged.connect(self._on_soon_changed)
        header.addWidget(self.soon_box)
        body.addLayout(header)

        self.scan_edit = ScanBuffer(card)
        self.scan_edit.scanned.connect(self.accept_scan)
        body.addWidget(self.scan_edit)

        self.verdict_label = QLabel("", card)
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setMinimumHeight(64)
        self.verdict_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        body.addWidget(self.verdict_label)

        self.detail_label = QLabel("", card)
        self.detail_label.setWordWrap(True)
        body.addWidget(self.detail_label)

        self.hint = Hint(
            "Срок читается из самого QR. Для кода маркировки с упаковки (DataMatrix) "
            "срок спрашивается у «Честного ЗНАКа» — для этого нужен вход на вкладке "
            "«Проверка кодов».", card)
        body.addWidget(self.hint)
        return card

    def _journal_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Журнал сканов", card))
        header.addStretch(1)
        self.clear_button = QPushButton("Очистить журнал", card)
        self.clear_button.setIcon(icons.icon("reset"))
        self.clear_button.clicked.connect(self.clear_journal)
        header.addWidget(self.clear_button)
        body.addLayout(header)

        self.table = DataTable([
            Column("Время", lambda e: f"{e.at:%H:%M:%S}", 84),
            Column("Товар", lambda e: e.card.title, 300, highlight=True),
            Column("GTIN", lambda e: e.card.gtin, 130),
            Column("Серийный", lambda e: e.card.serial, 120),
            Column("Произведён", lambda e: _day(e.card.produced), 112,
                   sort_key=lambda e: e.card.produced or date.min),
            Column("Годен до", lambda e: _expires(e), 128,
                   color=self._level_color,
                   sort_key=lambda e: e.card.expires or date.max),
            Column("Осталось", lambda e: left_text(e.days), 210,
                   color=self._level_color, align=RIGHT,
                   sort_key=lambda e: e.days if e.days is not None else FAR),
            Column("Источник", lambda e: e.source + (", расчёт" if e.card.estimated else ""),
                   120),
            Column("Статус КМ", lambda e: e.card.status, 110),
            Column("Замечание", self._remark, 360),
        ], card)
        self.table.setMinimumHeight(180)
        body.addWidget(self.table, 1)
        return card

    # --- скан ----------------------------------------------------------------------

    def focus_scan(self) -> None:
        """Курсор в поле скана. Без этого сканер печатает мимо."""
        self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept_scan(self, text: str, today: date | None = None) -> Entry | None:
        """Принимает один скан целиком, разбирает и показывает ответ."""
        if not text or not text.strip():
            return None
        entry = self._make_entry(text, today)
        card = entry.card
        self.entries.append(entry)
        self._show(entry)
        self._refresh_table()
        if entry.pending:
            run_task(
                self._lookup, card.kiz,
                on_result=lambda info, e=entry: self._lookup_done(e, info),
                on_error=lambda message, e=entry: self._lookup_failed(e, message))
        return entry

    def _make_entry(self, text: str, today: date | None = None) -> Entry:
        """Разбирает скан в запись журнала; нужен ли ответ ГИС МТ — в `pending`."""
        card = expiry.parse_card(text)
        key = card.key
        repeat = any(entry.card.key == key for entry in self.entries)
        entry = Entry(card, today or date.today(), repeat=repeat)
        if card.expires is None and card.bare:
            # Срока в коде нет. Известный экземпляр берётся из памяти сессии, а
            # неизвестный спрашивается у «Честного ЗНАКа» в фоне — ожидание
            # ответа не должно держать окно и следующий скан.
            if known := self._resolved.get(key):
                # В памяти — ответ как пришёл, без расчёта: правила могли
                # смениться, и считать надо по действующим.
                entry.card, entry.source = copy.deepcopy(known), "ГИС МТ"
                self._estimate(entry.card)
            else:
                entry.pending = True
        else:
            # Срок в самом QR или хотя бы дата производства: считать можно сразу.
            self._estimate(card)
        return entry

    def add_codes(self, texts: list[str], today: date | None = None, *,
                  origin: str = "",
                  names: dict[str, str] | None = None) -> tuple[int, int]:
        """Добавляет в журнал готовые коды — например, из сверки с УПД.

        Возвращает «добавлено и пропущено». Пропускаются пустые и те, что уже в
        журнале: повтор здесь не отметка «уже сканировали», как у живого скана,
        а лишняя строка и лишний запрос. Срок спрашивается у «Честного ЗНАКа»
        одной пачкой в фоне — по запросу на код тысяча вещей ждала бы минуты.
        """
        added: list[Entry] = []
        skipped = 0
        known = {entry.card.key for entry in self.entries}
        for text in texts:
            if not text or not text.strip():
                continue
            entry = self._make_entry(text, today)
            if entry.card.key in known:
                skipped += 1
                continue
            entry.origin = origin
            # Название из документа — запасное: ответ системы его заменит, а
            # если ответа не будет, строка не останется безымянной.
            if names and not entry.card.name:
                entry.card.name = names.get(text, "")
            known.add(entry.card.key)
            self.entries.append(entry)
            added.append(entry)
        self._refresh_table()
        waiting = [entry for entry in added if entry.pending]
        if added and not waiting:
            self._show(added[-1], sound=False)
        if waiting:
            self.hint.setStyleSheet("")
            self.hint.setText(
                f"Спрашиваем «Честный ЗНАК» о сроке: {len(waiting)} из {len(added)} "
                "кодов — в самом коде срока нет.")
            run_task(
                self._lookup_many, [entry.card.kiz for entry in waiting],
                on_result=lambda found, w=waiting: self._lookup_many_done(w, found),
                on_error=lambda message, w=waiting: self._lookup_many_failed(w, message))
        return len(added), skipped

    def load_upd(self, path: str = "") -> int:
        """Берёт все коды УПД и показывает срок годности каждого.

        Сверка отвечает, то ли приехало, а здесь вопрос другой — до какого
        числа это годно, и спрашивать его удобнее по документу, чем сканируя
        каждую вещь. Срок спрашивается у «Честного ЗНАКа» одной пачкой, поэтому
        нужен вход по сертификату. Возвращает, сколько кодов добавлено.
        """
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "УПД с кодами маркировки", "",
                "Документы ЭДО (*.xml *.zip);;Все файлы (*.*)")
        if not path:
            return 0
        try:
            document = upd_module.read(path)
        except upd_module.UpdProblem as problem:
            self._say(str(problem), ToastKind.ERROR)
            return 0
        if not service.signed_in():
            self._say("Срок спрашивается у «Честного ЗНАКа» — войдите по "
                      "сертификату на вкладке «Проверка кодов»", ToastKind.WARNING)
            return 0
        pairs = document.marks
        added, skipped = self.add_codes(
            [mark.value for _, mark in pairs], origin=document.title,
            names={mark.value: line.name for line, mark in pairs})
        tail = f" (уже были в журнале: {skipped})" if skipped else ""
        self._say(f"{document.title}: в журнал сроков добавлено кодов: {added}{tail}",
                  ToastKind.SUCCESS if added else ToastKind.WARNING)
        return added

    def _say(self, text: str, kind: ToastKind) -> None:
        if self.notify:
            self.notify(text, kind)

    def _lookup_many_done(self, waiting: list[Entry], found: dict[str, CodeInfo]) -> None:
        for entry in waiting:
            info = found.get(entry.card.kiz)
            if info is None:
                self._lookup_failed(entry, "«Честный ЗНАК» не ответил по этому коду.")
            else:
                self._lookup_done(entry, info)
        got = sum(1 for entry in waiting if entry.card.expires is not None)
        self.hint.setText(
            f"Ответ получен: срок известен у {got} из {len(waiting)} кодов"
            + ("." if got == len(waiting) else
               "; у остальных причина — в колонке «Замечание»."))

    def _lookup_many_failed(self, waiting: list[Entry], message: str) -> None:
        for entry in waiting:
            self._lookup_failed(entry, message)
        self.hint.setText(message)

    def _lookup_done(self, entry: Entry, info: CodeInfo) -> None:
        """Ответ «Честного ЗНАКа» пришёл: срок и сведения о товаре — в запись."""
        card = expiry.from_response(entry.card.kiz, info.raw)
        card.name = card.name or info.product_name or entry.card.name
        card.owner = card.owner or info.owner_name
        card.status = card.status or (info.state.title if info.found else "")
        if info.problems:
            card.problems = [*info.problems, *card.problems]
        if card.expires is not None or card.produced is not None:
            self._resolved[card.key] = copy.deepcopy(card)
        self._estimate(card)
        entry.card, entry.pending, entry.source = card, False, "ГИС МТ"
        self._finished(entry)

    def _lookup_failed(self, entry: Entry, message: str) -> None:
        """Спросить не удалось: вход не выполнен, нет связи, отказ. Говорим как есть."""
        hint = ""
        if "вход" in message.lower():
            hint = " Войдите на вкладке «Проверка кодов» и отсканируйте ещё раз."
        entry.card.problems = [message.rstrip() + hint]
        entry.pending = False
        self._finished(entry)

    def _finished(self, entry: Entry) -> None:
        self._refresh_table()
        # Плашка отвечает на последний скан; ответ на давний обновляет только журнал.
        if self.entries and self.entries[-1] is entry:
            self._show(entry)

    def _estimate(self, card: ProductCard) -> None:
        """Срок по дате производства — когда прочитать его негде."""
        expiry.apply_estimate(card, self.settings.marking_shelf_years,
                              self.settings.marking_shelf_brands)

    def edit_rules(self) -> bool:
        """Правила расчёта срока. После правки пересчитываются уже отсканированные."""
        dialog = ShelfLifeDialog(self.settings.marking_shelf_years,
                                 self.settings.marking_shelf_brands, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.settings.marking_shelf_years = dialog.years.value()
        self.settings.marking_shelf_brands = dialog.result_brands
        self.settings.save()
        for entry in self.entries:
            expiry.clear_estimate(entry.card)
            self._estimate(entry.card)
        self._refresh_table()
        if self.entries:
            self._show(self.entries[-1], sound=False)
        return True

    def _level(self, entry: Entry) -> ExpiryLevel:
        return expiry.level_of(entry.card.expires, self.soon_box.value(), entry.day)

    def _level_color(self, entry: Entry) -> QColor:
        return QColor(LEVEL_COLORS[self._level(entry)][0])

    def _remark(self, entry: Entry) -> str:
        parts = []
        if entry.pending:
            parts.append("спрашиваем «Честный ЗНАК»…")
        if entry.card.estimated:
            parts.append(f"срок расчётный: {entry.card.shelf_years} г. от даты производства")
        if entry.repeat:
            parts.append("повтор")
        if entry.origin:
            parts.append(entry.origin)
        parts.extend(entry.card.problems)
        return "; ".join(parts)

    def _show(self, entry: Entry, *, sound: bool = True) -> None:
        """Ответ на скан: цвет, дата, остаток дней и что за товар."""
        level = self._level(entry)
        color, background = LEVEL_COLORS[level]
        card = entry.card
        if entry.pending:
            text = "Спрашиваем «Честный ЗНАК»…"
            sound = False
        elif card.expires is None:
            text = level.title
        else:
            text = (f"{level.title} · годен до {card.expires:%d.%m.%Y} · "
                    f"{left_text(entry.days)}")
            if card.estimated:
                text += f" · расчёт: {card.shelf_years} г. от производства"
        if entry.repeat:
            text = "Уже сканировали · " + text
        self.verdict_label.setText(text)
        self.verdict_label.setStyleSheet(
            f"color: {color}; background: {background}; font-size: 20px;"
            f" font-weight: 700; border-radius: {Metrics.RADIUS}px;"
            " padding: 12px 16px;")

        facts = [card.title]
        if card.brand:
            facts.append(card.brand)
        if card.gtin:
            facts.append(f"GTIN {card.gtin}")
        if card.produced:
            facts.append(f"произведён {card.produced:%d.%m.%Y}")
        self.detail_label.setText(" · ".join(facts) if card.name or card.gtin else "")
        self.hint.setText(" ".join(card.problems)
                          if card.problems else
                          "Срок читается из самого QR. Для кода маркировки с упаковки "
                          "(DataMatrix) срок спрашивается у «Честного ЗНАКа» — нужен вход "
                          "на вкладке «Проверка кодов».")
        # Просрочку и непрочитанный срок надо услышать: на экран никто не смотрит.
        if sound and level in (ExpiryLevel.EXPIRED, ExpiryLevel.UNKNOWN):
            QApplication.beep()

    # --- журнал и порог ---------------------------------------------------------------

    def _refresh_table(self) -> None:
        # Новые сверху: смотрят на последний скан, а не на первый за смену.
        self.table.set_items(list(reversed(self.entries)))
        self.clear_button.setEnabled(bool(self.entries))

    def clear_journal(self) -> None:
        self.entries.clear()
        self.verdict_label.setText("")
        self.verdict_label.setStyleSheet("")
        self.detail_label.setText("")
        self._refresh_table()
        self.focus_scan()

    def _on_soon_changed(self, value: int) -> None:
        self.settings.marking_expiry_soon_days = value
        self.settings.save()
        # Цвета журнала пересчитываются по новому порогу; плашка — по последнему скану.
        self._refresh_table()
        if self.entries:
            self._show(self.entries[-1], sound=False)


def _day(value: date | None) -> str:
    return f"{value:%d.%m.%Y}" if value else ""


def _expires(entry: Entry) -> str:
    """Срок для журнала; расчётный — со знаком «≈», чтобы не спутать с данными."""
    text = _day(entry.card.expires)
    return f"≈ {text}" if text and entry.card.estimated else text
