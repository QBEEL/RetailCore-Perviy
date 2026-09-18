"""Вкладка «Маркировка»: вход по сертификату и проверка кодов «Честного ЗНАКа».

Порядок на странице повторяет порядок работы: войти сертификатом, набрать коды
сканером, посмотреть, что о них знает система. Разбор кодов идёт на месте и без
сети — состав кода, повторы в пачке и испорченные коды видны ещё до входа, и
этим закрывается половина вопросов «что за коробка приехала».

Входов на странице два, и это не дублирование. Сертификатом входят в ГИС МТ и
True API — оттуда сведения о кодах. В СУЗ, которая коды выдаёт, сертификатом не
входят вовсе: она узнаёт устройство по трём постоянным реквизитам из личного
кабинета. Отсюда отдельная карточка рядом с картой входа, а не строка в
настройках.

Третья работа — сверка с документом — не требует ни того, ни другого входа.
Коды из УПД и коды с этикеток сравниваются на этом компьютере, и ни сеть, ни
сертификат для этого не нужны. Стоит она отдельной подвкладкой потому, что
отвечает на свой вопрос: проверка говорит, чей код и в обороте ли он, а сверка
— то ли приехало, что написано в документе.

Из операций доступна одна проверка: она ничего не меняет в ГИС МТ. Приёмки,
отгрузки и вывода из оборота здесь нет намеренно — ядро их не отправляет (см.
`STATUS_POLLING_READY` в `core/marking/service.py`), а кнопка, которой нечего
сделать, хуже её отсутствия.

Обращения к закрытому ключу уходят в фоновую задачу: подпись делает КриптоПро,
до окна ввода пароля проходят секунды, и всё это время окно не должно казаться
повисшим.
"""
from __future__ import annotations

import os
from typing import Callable, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.marking import (
    CodeInfo,
    Contour,
    Credentials,
    GROUPS,
    Operation,
    Organisation,
    codes as codes_module,
)
from ..core.marking import orders as orders_module, service
from ..core.marking import onec as onec_module
from ..core.marking import reconcile as reconcile_module, upd as upd_module
from ..core.marking.onec import Catalog, LineMatch, MatchKind, OnecProblem
from ..core.marking.orders import ReleaseMethod
from ..core.marking.reconcile import Reconciliation, Scan, Verdict
from ..core.marking.upd import Document, Line, UpdProblem
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Card, Hint, MetricTile, SectionTitle, Subtitle, Title
from .widgets.marking_dialogs import (
    NomenclatureDialog,
    OrderConfirmDialog,
    OrganisationDialog,
)
from .widgets.toast import ToastKind

# Сколько строк результата показывать. Проверяют тысячами, а глазами смотрят на
# несоответствия — их отбирает переключатель «только замечания».
RESULT_LIMIT = 500

# Пауза перед пересчётом сводки по кодам. Сканер вводит код посимвольно, и
# разбор на каждое нажатие превратил бы одну пачку в тысячу разборов.
PARSE_DELAY = 250

# Сколько последних операций показывать в журнале.
JOURNAL_LIMIT = 50

# Подвкладки по порядку работы: сначала узнать, что за коды, потом сверить с
# документом, и отдельно — заказать свои.
CHECK_TAB = 0
RECONCILE_TAB = 1

# Как выглядит ответ на скан при сверке. Кладовщик смотрит на товар, а на экран
# косится краем глаза, и различать ответы он должен цветом, а не чтением.
VERDICT_COLORS: dict[Verdict, tuple[str, str]] = {
    Verdict.MATCHED: (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    Verdict.REPEAT: (Palette.WARNING, Palette.WARNING_SOFT),
    Verdict.UNKNOWN: (Palette.DANGER, Palette.DANGER_SOFT),
    Verdict.BROKEN: (Palette.DANGER, Palette.DANGER_SOFT),
}


class MarkingPage(QWidget):
    """Вход в систему маркировки, разбор кодов и проверка их в ГИС МТ."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._certificates: list = []
        # До первого чтения хранилища считаем, что КриптоПро есть: обратное
        # утверждение — вывод, а не догадка, и делать его до проверки нельзя.
        self._crypto_available = True
        self._batch = codes_module.Batch()
        self._result: list[CodeInfo] = []
        self._journal: list[Operation] = []
        # Последний ответ СУЗ на проверку связи. Держится отдельно от реквизитов:
        # он относится к тому, что было в полях в момент проверки, и правка поля
        # его обесценивает.
        self._suz_answer = ""
        self._orders: list = []
        # Документ, с которым идёт сверка, и её ход. Живут, пока открыто окно:
        # сверку начинают на приёмке и заканчивают через полчаса, уходя за
        # каждой следующей коробкой, и терять её при переключении вкладки
        # значило бы начинать сначала.
        self._upd: Document | None = None
        self._session: Reconciliation | None = None
        # Номенклатура 1С и то, как на неё легли строки документа. Держится
        # рядом со сверкой: выгрузка в 1С — её последний шаг, и разносить их
        # по разным местам значит заставить искать документ дважды.
        self._catalog: Catalog | None = None
        self._matches: list[LineMatch] = []
        self._busy = False
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(PARSE_DELAY)
        self._parse_timer.timeout.connect(self._reparse)
        self._build()
        self._restore_choices()
        self._restore_catalog()
        self._sync_reconcile()
        self._sync_state()

    # --- разметка -------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6,
                                Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Маркировка", self))
        root.addWidget(Subtitle(
            "Проверка кодов спрашивает «Честный ЗНАК», что он знает о коде, и "
            "требует входа по сертификату. Заказ кодов идёт через СУЗ и требует "
            "её реквизитов — это разные системы и разные способы представиться. "
            "Сверка с УПД не требует ни входа, ни сети: документ и этикетки "
            "сравниваются здесь.",
            self))

        # Подвкладки, а не одна длинная страница: работы здесь три, и они не
        # пересекаются. Проверка кодов идёт через ГИС МТ по сертификату, заказ —
        # через СУЗ по её реквизитам, сверка не идёт никуда вовсе, и держать
        # перед глазами все три значит каждый раз выбирать, какие две трети
        # экрана сейчас не нужны.
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._check_tab(), icons.icon("marking"), "Проверка кодов")
        self.tabs.addTab(self._reconcile_tab(), icons.icon("compare"),
                         "Сверка кодов маркировки")
        self.tabs.addTab(self._order_tab(), icons.icon("order"), "Заказ кодов")
        # Сверку ведут сканером, а сканер печатает туда, где курсор. Ставить его
        # в поле сканирования при открытии вкладки — не удобство, а условие
        # работы: иначе первый же код уедет в поле поиска или в никуда.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

    def _check_tab(self) -> QWidget:
        page = QWidget(self)
        body = QVBoxLayout(page)
        body.setContentsMargins(0, Metrics.GAP, 0, 0)
        body.setSpacing(Metrics.GAP)
        body.addWidget(self._session_card())
        body.addWidget(self._codes_card())
        body.addWidget(self._result_card(), 1)
        body.addWidget(self._journal_card())
        return page

    def _reconcile_tab(self) -> QWidget:
        """Сверка приехавшего товара с кодами из УПД.

        Стоит рядом с проверкой кодов, а не вместо неё: проверка спрашивает
        «Честный ЗНАК», чей код и в обороте ли он, а сверка отвечает на другой
        вопрос — то ли приехало, что написано в документе. Для сверки не нужны
        ни сертификат, ни сеть: обе стороны сравнения лежат на этом компьютере.
        """
        page = QWidget(self)
        body = QVBoxLayout(page)
        body.setContentsMargins(0, Metrics.GAP, 0, 0)
        body.setSpacing(Metrics.GAP)
        body.addWidget(self._upd_card())
        body.addWidget(self._scan_card())
        body.addWidget(self._progress_card(), 1)
        body.addWidget(self._onec_card())
        body.addWidget(self._extra_card())
        return page

    def _order_tab(self) -> QWidget:
        page = QWidget(self)
        body = QVBoxLayout(page)
        body.setContentsMargins(0, Metrics.GAP, 0, 0)
        body.setSpacing(Metrics.GAP)
        body.addWidget(self._suz_card())
        body.addWidget(self._new_order_card())
        body.addWidget(self._orders_card(), 1)
        return page

    def _session_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Вход в «Честный ЗНАК»", card))
        header.addStretch(1)
        self.certificates_button = self._action(card, "Обновить список", "refresh",
                                                self.reload_certificates)
        self.organisation_button = self._action(card, "Организация", "certificate",
                                                self.choose_organisation)
        self.organisation_button.setToolTip(
            "Под какой организацией входить, если сертификат действует "
            "за нескольких")
        self.login_button = self._action(card, "Войти", "key", self.sign_in)
        self.login_button.setObjectName("Primary")
        self.logout_button = self._action(card, "Выйти", "unlink", self.sign_out)
        for button in (self.certificates_button, self.organisation_button,
                       self.login_button, self.logout_button):
            header.addWidget(button)
        body.addLayout(header)

        chooser = QHBoxLayout()
        chooser.setSpacing(9)
        self.contour_box = QComboBox(card)
        for contour in (Contour.SANDBOX, Contour.PRODUCTION):
            self.contour_box.addItem(contour.title, contour.value)
        self.contour_box.currentIndexChanged.connect(self._on_contour_changed)
        chooser.addWidget(self.contour_box)
        self.certificate_box = QComboBox(card)
        self.certificate_box.setMinimumWidth(320)
        self.certificate_box.currentIndexChanged.connect(self._on_certificate_changed)
        chooser.addWidget(self.certificate_box, 1)
        body.addLayout(chooser)

        self.session_hint = Hint("", card)
        body.addWidget(self.session_hint)
        return card

    def _suz_card(self) -> Card:
        """Реквизиты станции управления заказами.

        Отдельной карточкой, а не строкой в настройках: это второй вход,
        независимый от сертификата. Сертификатом входят в ГИС МТ, реквизитами —
        в СУЗ, и путать их нельзя. Реквизиты свои у каждого контура, поэтому
        поля перечитываются при переключении.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Станция управления заказами (СУЗ)", card))
        header.addStretch(1)
        self.suz_token_button = self._action(card, "Получить токен", "key",
                                             self.sign_in_suz)
        self.suz_token_button.setToolTip(
            "Взять токен у СУЗ по сертификату — вместо переноса чужого из "
            "другой программы")
        self.suz_check_button = self._action(card, "Проверить соединение", "run",
                                             self.check_suz)
        self.suz_check_button.setObjectName("Primary")
        self.suz_save_button = self._action(card, "Сохранить", "save", self.save_suz)
        self.suz_forget_button = self._action(card, "Забыть", "clear", self.forget_suz)
        for button in (self.suz_token_button, self.suz_check_button,
                       self.suz_save_button, self.suz_forget_button):
            header.addWidget(button)
        body.addLayout(header)

        form = QFormLayout()
        form.setSpacing(9)
        self.oms_edit = QLineEdit(card)
        self.oms_edit.setPlaceholderText("Идентификатор ОМС из личного кабинета СУЗ")
        form.addRow("ОМС ID", self.oms_edit)

        self.connection_edit = QLineEdit(card)
        self.connection_edit.setPlaceholderText(
            "Необязательно — в запросы не уходит, опознаёт устройство в кабинете")
        form.addRow("Идентификатор соединения", self.connection_edit)

        token_row = QHBoxLayout()
        token_row.setSpacing(9)
        self.token_edit = QLineEdit(card)
        # Токен равносилен доступу к заказу кодов от имени организации, а
        # вписывают его при коллегах и на общем экране. Показать можно, но по
        # своему решению, а не по умолчанию.
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("Токен устройства")
        token_row.addWidget(self.token_edit, 1)
        self.token_shown = QCheckBox("Показать", card)
        self.token_shown.toggled.connect(self._on_token_shown)
        token_row.addWidget(self.token_shown)
        form.addRow("Токен", token_row)

        self.suz_host_edit = QLineEdit(card)
        self.suz_host_edit.setPlaceholderText(
            "Пусто — облачная СУЗ. Адрес нужен только локальной станции")
        form.addRow("Адрес СУЗ", self.suz_host_edit)
        body.addLayout(form)

        for field in (self.oms_edit, self.connection_edit, self.token_edit,
                      self.suz_host_edit):
            field.textChanged.connect(self._sync_suz_hint)

        self.suz_hint = Hint("", card)
        body.addWidget(self.suz_hint)
        return card

    def _new_order_card(self) -> Card:
        """Новый заказ кодов.

        Способ выпуска назван теми же словами, что в ПРИНТМАРКИ: она давно в
        работе, и одно и то же в двух программах должно называться одинаково —
        иначе однажды будет выбрано не то.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Новый заказ", card))
        header.addStretch(1)
        header.addWidget(self._action(card, "Добавить товар", "plus", self.add_line))
        header.addWidget(self._action(card, "Убрать", "trash", self.remove_line))
        self.order_button = self._action(card, "Заказать коды", "run", self.create_order)
        self.order_button.setObjectName("Primary")
        header.addWidget(self.order_button)
        body.addLayout(header)

        chooser = QHBoxLayout()
        chooser.setSpacing(9)
        self.group_box = QComboBox(card)
        for group in GROUPS:
            self.group_box.addItem(group.title, group.code)
        chooser.addWidget(self.group_box, 1)
        self.method_box = QComboBox(card)
        for method in ReleaseMethod:
            self.method_box.addItem(method.title, method.value)
        self.method_box.currentIndexChanged.connect(self._sync_order_hint)
        chooser.addWidget(self.method_box, 1)
        body.addLayout(chooser)

        details = QFormLayout()
        details.setSpacing(9)
        self.contact_edit = QLineEdit(card)
        self.contact_edit.setPlaceholderText(
            "Кому в ЦРПТ писать по этому заказу")
        details.addRow("Контактное лицо", self.contact_edit)

        # Шаблон и оплата — два числа, которых никто не помнит наизусть и
        # которые неоткуда узнать: метода со списком шаблонов у СУЗ нет. Зато
        # они есть в прошлых заказах участника, поэтому подставляются оттуда, а
        # подпись рядом говорит, откуда именно. Править их можно — но не нужно.
        # Шаблон выбирается из тех, что подходят группе, — списком, а не числом.
        # Номер сам по себе не значит для человека ничего, и требовать его
        # значит требовать знания, которого у категорийного менеджера нет.
        # Различаются шаблоны длиной серийного номера и наличием криптохвоста,
        # это и написано в каждой строке.
        self.template_box = QComboBox(card)
        self.template_box.currentIndexChanged.connect(self._sync_order_hint)
        details.addRow("Шаблон кода", self.template_box)

        payment = QHBoxLayout()
        payment.setSpacing(9)
        self.payment_box = QSpinBox(card)
        self.payment_box.setRange(1, 9)
        self.payment_box.setValue(orders_module.DEFAULT_PAYMENT)
        payment.addWidget(self.payment_box)
        self.template_hint = Hint("", card)
        payment.addWidget(self.template_hint, 1)
        details.addRow("Способ оплаты", payment)
        body.addLayout(details)

        self.lines = QTableWidget(0, 2, card)
        self.lines.setHorizontalHeaderLabels(["Код товара (GTIN)", "Количество"])
        self.lines.verticalHeader().setVisible(False)
        self.lines.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lines.setMaximumHeight(140)
        head = self.lines.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.lines.itemChanged.connect(self._sync_order_hint)
        body.addWidget(self.lines)

        # Подсказка отзывается на каждое поле заказа: она отвечает на вопрос
        # «почему кнопка ничего не делает», и отвечать должна сразу.
        self.contact_edit.textChanged.connect(self._sync_order_hint)
        self.group_box.currentIndexChanged.connect(self._on_group_changed)
        self.payment_box.valueChanged.connect(self._sync_order_hint)

        self.order_hint = Hint("", card)
        body.addWidget(self.order_hint)
        return card

    def _orders_card(self) -> Card:
        """Заказы кодов и остаток в буферах.

        Смотрят сюда ради одного числа: сколько кодов ещё можно забрать. Оно и
        отвечает на вопрос «хватит ли», а «сколько заказывали» не отвечает —
        часть кодов давно забрана.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Заказы кодов", card))
        header.addStretch(1)
        self.orders_button = self._action(card, "Обновить", "refresh",
                                          self.reload_orders)
        header.addWidget(self.orders_button)
        body.addLayout(header)

        self.orders = QTableWidget(0, 6, card)
        self.orders.setHorizontalHeaderLabels(
            ["Заказ", "Создан", "Состояние", "Товаров", "Получено",
             "Кодов доступно"])
        self.orders.verticalHeader().setVisible(False)
        self.orders.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.orders.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.orders.setMaximumHeight(170)
        head = self.orders.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.addWidget(self.orders)

        self.orders_hint = Hint("", card)
        body.addWidget(self.orders_hint)
        return card

    def _codes_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Коды", card))
        header.addStretch(1)
        header.addWidget(self._action(card, "Взять из файла", "open", self.load_file))
        header.addWidget(self._action(card, "Очистить", "clear", self.clear_codes))
        body.addLayout(header)

        self.codes_edit = QPlainTextEdit(card)
        self.codes_edit.setPlaceholderText(
            "Сканируйте коды сюда или вставьте списком — по одному в строке")
        self.codes_edit.setMinimumHeight(120)
        self.codes_edit.textChanged.connect(self._parse_timer.start)
        body.addWidget(self.codes_edit)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_codes = MetricTile("Кодов", Palette.PRIMARY, card)
        self.tile_items = MetricTile("Товаров", Palette.INFO, card)
        self.tile_broken = MetricTile("Не разобрано", Palette.DANGER, card)
        self.tile_repeats = MetricTile("Повторов", Palette.WARNING, card)
        for tile in (self.tile_codes, self.tile_items, self.tile_broken,
                     self.tile_repeats):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.codes_hint = Hint("Пока ни одного кода.", card)
        body.addWidget(self.codes_hint)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.check_button = self._action(card, "Проверить (F5)", "run", self.run_check)
        self.check_button.setObjectName("Primary")
        actions.addWidget(self.check_button)
        self.fresh_box = QCheckBox("Спросить заново, не подставляя прошлые ответы",
                                   card)
        self.fresh_box.setToolTip(
            "Состояние кода меняется, и вчерашний ответ «в обороте» ничего не "
            "говорит о сегодняшнем")
        actions.addWidget(self.fresh_box)
        actions.addStretch(1)
        body.addLayout(actions)
        return card

    def _result_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Что знает система", card))
        header.addStretch(1)
        self.problems_box = QCheckBox("Только замечания", card)
        self.problems_box.stateChanged.connect(self._fill_result)
        header.addWidget(self.problems_box)
        body.addLayout(header)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_circulation = MetricTile("В обороте", Palette.SUCCESS, card)
        self.tile_elsewhere = MetricTile("Не в обороте", Palette.WARNING, card)
        self.tile_alien = MetricTile("Чужих", Palette.INFO, card)
        self.tile_missing = MetricTile("Не найдено", Palette.DANGER, card)
        for tile in (self.tile_circulation, self.tile_elsewhere, self.tile_alien,
                     self.tile_missing):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.result = QTableWidget(0, 5, card)
        self.result.setHorizontalHeaderLabels(
            ["Товар", "Код", "Состояние", "Владелец", "Замечания"])
        self.result.verticalHeader().setVisible(False)
        self.result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result.setAlternatingRowColors(True)
        head = self.result.horizontalHeader()
        # Код, состояние и владелец показываются целиком: обрезанное «Выве…»
        # вместо «Выведен из оборота» превращает главную колонку в загадку.
        # Замечание бывает длинным, поэтому ему отведена своя ширина — иначе
        # одна многословная строка сжимает название товара до многоточия.
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.result.setColumnWidth(4, 240)
        body.addWidget(self.result, 1)

        self.result_hint = Hint(
            "Проверка спрашивает «Честный ЗНАК» о каждом коде: чей он, в обороте "
            "ли и не выведен ли уже.", card)
        body.addWidget(self.result_hint)
        return card

    def _journal_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Журнал операций", card))
        header.addStretch(1)
        header.addWidget(self._action(card, "Обновить", "refresh", self.reload_journal))
        body.addLayout(header)

        self.journal = QTableWidget(0, 5, card)
        self.journal.setHorizontalHeaderLabels(
            ["Когда", "Операция", "Кодов", "Контур", "Состояние"])
        self.journal.verticalHeader().setVisible(False)
        self.journal.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.journal.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.journal.setMaximumHeight(170)
        head = self.journal.horizontalHeader()
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.addWidget(self.journal)

        self.journal_hint = Hint("", card)
        body.addWidget(self.journal_hint)
        return card

    # --- карточки сверки ------------------------------------------------------

    def _upd_card(self) -> Card:
        """Документ, с которым сверяемся.

        Берётся тот самый XML, что приходит по ЭДО, — он лежит в папке рядом с
        печатной формой, и в нём перечислен каждый экземпляр. Архив, скачанный
        из Диадока целиком, тоже подходит: распаковывать его ради одного файла
        человеку незачем.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Документ", card))
        header.addStretch(1)
        self.upd_button = self._action(card, "Загрузить УПД", "open", self.load_upd)
        self.upd_button.setObjectName("Primary")
        self.upd_check_button = self._action(card, "Спросить «Честный ЗНАК»",
                                             "marking", self.codes_to_check)
        self.upd_check_button.setToolTip(
            "Перенести коды документа на вкладку «Проверка кодов»: сверка "
            "говорит, то ли приехало, а «Честный ЗНАК» — чьё оно и в обороте ли")
        self.upd_report_button = self._action(card, "Акт сверки", "export",
                                              self.save_report)
        self.upd_forget_button = self._action(card, "Убрать", "clear", self.forget_upd)
        for button in (self.upd_button, self.upd_check_button,
                       self.upd_report_button, self.upd_forget_button):
            header.addWidget(button)
        body.addLayout(header)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_upd_lines = MetricTile("Позиций", Palette.INFO, card)
        self.tile_upd_codes = MetricTile("Кодов в документе", Palette.PRIMARY, card)
        for tile in (self.tile_upd_lines, self.tile_upd_codes):
            tiles.addWidget(tile, 1)
        tiles.addStretch(2)
        body.addLayout(tiles)

        self.upd_hint = Hint("", card)
        body.addWidget(self.upd_hint)
        return card

    def _scan_card(self) -> Card:
        """Поле сканирования и ответ на последний скан.

        Ответ вынесен в отдельную широкую плашку с цветом во всю ширину. Читать
        его некогда: вещь в руках, следующая в коробке, и различаться ответы
        должны раньше, чем прочитаны, — зелёное значит «клади», красное значит
        «остановись».
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Сканирование", card))
        header.addStretch(1)
        self.undo_button = self._action(card, "Отменить скан", "reset",
                                        self.undo_scan)
        self.undo_button.setToolTip(
            "Снять последний скан — если пикнули не ту вещь")
        header.addWidget(self.undo_button)
        self.restart_button = self._action(card, "Начать заново", "refresh",
                                           self.restart_reconcile)
        header.addWidget(self.restart_button)
        body.addLayout(header)

        self.scan_edit = QLineEdit(card)
        self.scan_edit.setPlaceholderText(
            "Наведите сканер на DataMatrix — код придёт сюда сам")
        self.scan_edit.setMinimumHeight(38)
        self.scan_edit.returnPressed.connect(self.accept_scan)
        body.addWidget(self.scan_edit)

        self.verdict_label = QLabel("", card)
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setMinimumHeight(46)
        self.verdict_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        body.addWidget(self.verdict_label)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_scanned = MetricTile("Сверено", Palette.SUCCESS, card)
        self.tile_left = MetricTile("Осталось", Palette.PRIMARY, card)
        self.tile_extra = MetricTile("Лишних", Palette.DANGER, card)
        self.tile_scan_repeats = MetricTile("Повторов", Palette.WARNING, card)
        for tile in (self.tile_scanned, self.tile_left, self.tile_extra,
                     self.tile_scan_repeats):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.sound_box = QCheckBox("Звук при расхождении", card)
        self.sound_box.setToolTip(
            "Смотреть на экран после каждой вещи некогда — о расхождении лучше "
            "услышать")
        self.sound_box.setChecked(self.settings.marking_reconcile_sound)
        self.sound_box.toggled.connect(self._on_sound_changed)
        body.addWidget(self.sound_box)

        self.scan_hint = Hint("", card)
        body.addWidget(self.scan_hint)
        return card

    def _progress_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Позиции документа", card))
        header.addStretch(1)
        self.open_only_box = QCheckBox("Только незакрытые", card)
        self.open_only_box.setToolTip(
            "Оставить строки, по которым сверено не всё")
        self.open_only_box.stateChanged.connect(self._fill_progress)
        header.addWidget(self.open_only_box)
        body.addLayout(header)

        self.progress = QTableWidget(0, 6, card)
        self.progress.setHorizontalHeaderLabels(
            ["Товар", "Код товара", "По документу", "Сверено", "Состояние",
             "Номенклатура 1С"])
        self.progress.verticalHeader().setVisible(False)
        self.progress.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.progress.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.progress.setAlternatingRowColors(True)
        # Двойной щелчок по строке — привязать номенклатуру. Правится оно ровно
        # там, где видно, что не сошлось, а не в отдельном окне со списком.
        self.progress.itemDoubleClicked.connect(self._on_progress_activated)
        head = self.progress.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.progress.setColumnWidth(5, 210)
        body.addWidget(self.progress, 1)

        self.progress_hint = Hint(
            "Сверка идёт без сети и без сертификата: документ и коды на товаре "
            "сравниваются на этом компьютере.", card)
        body.addWidget(self.progress_hint)
        return card

    def _onec_card(self) -> Card:
        """Последний шаг приёмки: завести сверенные коды в 1С.

        Номенклатура берётся из выгрузки 1С — связать название поставщика с
        кодом номенклатуры больше неоткуда. Путь к выгрузке запоминается и
        перечитывается сам: файл один и тот же от поставки к поставке, а
        содержимое меняется, и держать в памяти прошлое значило бы однажды
        не найти заведённый вчера товар.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Выгрузка в 1С", card))
        header.addStretch(1)
        self.catalog_button = self._action(card, "Номенклатура 1С", "catalog",
                                           self.load_catalog)
        self.catalog_button.setToolTip(
            "Выгрузка из 1С: название, код номенклатуры, характеристика и, если "
            "есть, штрихкод")
        header.addWidget(self.catalog_button)
        self.link_button = self._action(card, "Привязать вручную", "link",
                                        self.link_selected)
        self.link_button.setToolTip(
            "Выбрать номенклатуру для строки, которая не сошлась сама "
            "(или двойной щелчок по строке)")
        header.addWidget(self.link_button)
        self.onec_button = self._action(card, "Файл для 1С", "export",
                                        self.save_onec)
        self.onec_button.setObjectName("Primary")
        header.addWidget(self.onec_button)
        body.addLayout(header)

        self.only_scanned_box = QCheckBox("Только сверенные коды", card)
        self.only_scanned_box.setChecked(True)
        self.only_scanned_box.setToolTip(
            "Код из документа, которого не нашлось на товаре, — это вещь, "
            "которая не приехала. Заводить ей штрихкод значит поставить в 1С "
            "на приход то, чего нет")
        self.only_scanned_box.toggled.connect(self._sync_onec_hint)
        body.addWidget(self.only_scanned_box)

        self.onec_hint = Hint("", card)
        body.addWidget(self.onec_hint)
        return card

    def _extra_card(self) -> Card:
        """Коды, которых в документе нет.

        Карточка прячется, пока лишних нет: пустая таблица занимает половину
        экрана и каждый раз заставляет проверить, не пропущено ли в ней что-то.
        """
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Лишние коды", card))
        header.addStretch(1)
        body.addLayout(header)

        self.extra = QTableWidget(0, 3, card)
        self.extra.setHorizontalHeaderLabels(["Когда", "Код", "Что не так"])
        self.extra.verticalHeader().setVisible(False)
        self.extra.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.extra.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.extra.setMaximumHeight(150)
        head = self.extra.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.extra.setColumnWidth(2, 280)
        body.addWidget(self.extra)

        self.extra_card = card
        card.setVisible(False)
        return card

    def _action(self, parent: QWidget, title: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(handler)
        return button

    # --- контур и сертификат ----------------------------------------------------

    @property
    def contour(self) -> Contour:
        value = self.contour_box.currentData()
        return Contour.PRODUCTION if value == Contour.PRODUCTION.value else Contour.SANDBOX

    @property
    def thumbprint(self) -> str:
        return str(self.certificate_box.currentData() or "")

    def _restore_choices(self) -> None:
        """Возвращает выбор прошлого запуска. Сертификаты читаются отдельно."""
        self._sync_template()
        index = self.contour_box.findData(self.settings.marking_contour)
        if index >= 0:
            self.contour_box.blockSignals(True)
            self.contour_box.setCurrentIndex(index)
            self.contour_box.blockSignals(False)
        self._load_suz()
        self._sync_template()
        self._sync_order_hint()

    def _on_contour_changed(self) -> None:
        """Смена контура сбрасывает вход.

        Токен выдан для своего контура, и держать его при переключении нельзя:
        иначе следующий запрос ушёл бы в бой с песочничным входом и ответил бы
        отказом, который никак не связан с тем, что сделал пользователь.

        По той же причине перечитываются реквизиты СУЗ: устройство в песочнице и
        устройство в бою — разные, и оставить на экране чужие значит однажды
        заказать коды не в той системе.
        """
        if service.signed_in():
            service.sign_out()
            self.notify("Контур изменён — вход сброшен", ToastKind.INFO)
        self.settings.marking_contour = self.contour.value
        self.settings.save()
        self._load_suz()
        self.reload_orders()
        self._sync_state()

    def _on_certificate_changed(self) -> None:
        """Смена сертификата забывает организацию: она была выбрана для другого."""
        chosen = self.thumbprint
        if chosen and chosen != self.settings.marking_thumbprint:
            self.settings.marking_thumbprint = chosen
            self.settings.marking_inn = ""
            self.settings.marking_organisation = ""
            self.settings.marking_organisations = []
            self.settings.save()
        # Заказ подписывается выбранным сертификатом, поэтому его готовность
        # меняется вместе с выбором.
        self._sync_order_hint()
        self._sync_state()

    def reload_certificates(self) -> None:
        """Перечитывает личные сертификаты.

        Делается при каждом открытии вкладки: носитель ключа вставляют и
        вынимают, и список, прочитанный при запуске, к обеду может не иметь
        отношения к тому, что подключено сейчас.
        """
        self._set_busy(True)
        self.session_hint.setText("Читаем личные сертификаты…")
        run_task(
            _load_certificates,
            on_result=self._apply_certificates,
            on_error=self._on_certificates_error,
        )

    def _apply_certificates(self, loaded: tuple[bool, list]) -> None:
        available, found = loaded
        self._crypto_available = available
        self._certificates = list(found)
        self.certificate_box.blockSignals(True)
        self.certificate_box.clear()
        for certificate in self._certificates:
            self.certificate_box.addItem(certificate.title, certificate.thumbprint)
        wanted = self.settings.marking_thumbprint
        if wanted and (index := self.certificate_box.findData(wanted)) >= 0:
            self.certificate_box.setCurrentIndex(index)
        self.certificate_box.blockSignals(False)
        # Контактным лицом заказа по умолчанию идёт владелец сертификата: это
        # он и есть, а набирать своё имя руками — лишнее действие.
        if not self.contact_edit.text().strip() and self._certificates:
            self.contact_edit.setText(self._certificates[0].owner)
        self._sync_order_hint()
        self._set_busy(False)

    def _on_certificates_error(self, message: str) -> None:
        self._crypto_available = False
        self._set_busy(False)
        self.notify(f"Не удалось прочитать сертификаты: {message}", ToastKind.ERROR)

    # --- вход -------------------------------------------------------------------

    def sign_in(self) -> None:
        if not self.thumbprint:
            self.notify("Сначала выберите сертификат", ToastKind.WARNING)
            return
        self._start_sign_in(self.settings.marking_inn,
                            self.settings.marking_organisation)

    def _start_sign_in(self, inn: str, organisation: str) -> None:
        self._set_busy(True)
        self.session_hint.setText(
            "Обращаемся к ключу — КриптоПро может спросить пароль к контейнеру…")
        run_task(
            service.sign_in,
            self.thumbprint, inn, self.contour, organisation,
            on_result=self._on_signed_in,
            on_error=self._on_sign_in_error,
        )

    def _on_signed_in(self, _session) -> None:
        self.settings.marking_thumbprint = self.thumbprint
        self.settings.marking_contour = self.contour.value
        self.settings.save()
        self._set_busy(False)
        self.notify(f"Вход выполнен: {service.current().title}", ToastKind.SUCCESS)

    def _on_sign_in_error(self, message: str) -> None:
        self._set_busy(False)
        if service.needs_organisation(message):
            # Сертификат по машиночитаемой доверенности действует за нескольких,
            # и решать за нас система отказывается: «Невозможно однозначно
            # определить под какой организацией выполняется авторизация».
            # Показывать такой текст и останавливаться незачем — спрашиваем.
            self.choose_organisation()
            return
        self.session_hint.setText("Войти не удалось.")
        self.notify(f"Вход в маркировку не выполнен: {message}", ToastKind.ERROR)

    # --- выбор организации ---------------------------------------------------------

    def choose_organisation(self) -> None:
        """Открывает выбор участника оборота, под которым выполняется вход.

        Запомненный список показывается сразу: спрашивать систему заново —
        значит требовать пароль к контейнеру ради того, что уже известно. Кнопка
        «Спросить систему» в окне остаётся на случай, когда список изменился.
        """
        if not self.thumbprint:
            self.notify("Сначала выберите сертификат", ToastKind.WARNING)
            return
        if known := self._saved_organisations():
            self._show_organisations(known)
            return
        self._fetch_organisations()

    def _fetch_organisations(self) -> None:
        self._set_busy(True)
        self.session_hint.setText("Выясняем, за кого действует сертификат…")
        run_task(
            service.organisations,
            self.thumbprint, self.contour,
            on_result=self._show_organisations,
            on_error=self._on_organisations_error,
        )

    def _on_organisations_error(self, message: str) -> None:
        """Список не пришёл — окно всё равно открывается.

        ИНН вводится руками: без него вход невозможен вовсе, а недоступность
        справочника ГИС МТ не должна означать «работать нельзя».
        """
        self._show_organisations([], note=f"Список организаций не пришёл: {message}")

    def _show_organisations(self, found: Sequence[Organisation],
                            note: str = "") -> None:
        self._set_busy(False)
        dialog = OrganisationDialog(
            _merge(found, self._saved_organisations()),
            self.settings.marking_inn, self, note=note)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.session_hint.setText("Вход отменён.")
            self._sync_state()
            return
        self._remember_organisations(dialog.organisations)
        if dialog.refresh_wanted:
            self._fetch_organisations()
            return
        if (chosen := dialog.chosen) is None:
            return
        self.settings.marking_inn = chosen.inn
        self.settings.marking_organisation = chosen.name
        self.settings.save()
        self._start_sign_in(chosen.inn, chosen.name)

    def _saved_organisations(self) -> list[Organisation]:
        return [Organisation(inn=item.get("inn", ""), name=item.get("name", ""))
                for item in self.settings.marking_organisations
                if item.get("inn")]

    def _remember_organisations(self, known: Sequence[Organisation]) -> None:
        self.settings.marking_organisations = [
            {"inn": item.inn, "name": item.name} for item in known if item.inn]
        self.settings.save()

    def sign_out(self) -> None:
        service.sign_out()
        self.notify("Выход из системы маркировки выполнен", ToastKind.INFO)
        self._sync_state()

    # --- СУЗ -----------------------------------------------------------------------

    @property
    def suz_credentials(self) -> Credentials:
        """Что сейчас в полях. Пробелы обрезает ядро — здесь берётся как набрано."""
        return Credentials(
            oms_id=self.oms_edit.text(),
            connection_id=self.connection_edit.text(),
            token=self.token_edit.text(),
            host=self.suz_host_edit.text(),
        )

    def _load_suz(self) -> None:
        """Показывает реквизиты выбранного контура и ставит его адрес.

        Сигналы полей глушатся: подстановка сохранённого — не правка, и
        подсказка «сохраните изменения» после неё была бы неправдой.
        """
        service.restore_suz()
        saved = service.suz_credentials(self.contour)
        for field, value in ((self.oms_edit, saved.oms_id),
                             (self.connection_edit, saved.connection_id),
                             (self.token_edit, saved.token),
                             (self.suz_host_edit, saved.host)):
            field.blockSignals(True)
            field.setText(value)
            field.blockSignals(False)
        self._suz_answer = ""
        self._sync_suz_hint()
        # Заказы относились к прежнему набору реквизитов. Перечитать их здесь
        # нельзя — сюда заходят и при сборке вкладки, до всякой сети, — а
        # оставить чужие на экране тем более: за них отвечала другая станция.
        self._orders = []
        self.orders.setRowCount(0)
        self._sync_orders_hint()

    def _on_token_shown(self, shown: bool) -> None:
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                    else QLineEdit.EchoMode.Password)

    def save_suz(self) -> None:
        saved = service.save_suz(self.suz_credentials, self.contour)
        self._show_suz(saved)
        if gaps := saved.missing:
            self.notify(f"Сохранено, но не хватает: {', '.join(gaps)}",
                        ToastKind.WARNING)
            return
        self.notify(f"Реквизиты СУЗ сохранены · {self.contour.title}",
                    ToastKind.SUCCESS)

    def sign_in_suz(self) -> None:
        """Берёт токен у самой СУЗ по сертификату, выбранному выше.

        Это ответ на «токен из другой программы не подходит»: он и не должен
        подходить — он выдан её устройству. Свой добывается той же подписью,
        которой входят в ГИС МТ.
        """
        if not self.thumbprint:
            self.notify("Сначала выберите сертификат", ToastKind.WARNING)
            return
        self._set_busy(True)
        self.suz_hint.setText(
            "Обращаемся к ключу — КриптоПро может спросить пароль к контейнеру…")
        run_task(
            service.suz_sign_in,
            self.suz_credentials, self.thumbprint, self.settings.marking_inn,
            self.contour,
            on_result=self._on_suz_token,
            on_error=self._on_suz_error,
        )

    def _on_suz_token(self, credentials: Credentials) -> None:
        """Полученный токен сразу сохраняется и сразу проверяется.

        Проверка следом — не лишний запрос: токен, который подошёл к входу,
        мог быть выдан участнику, а не той станции, чей ОМС ID вписан. Узнать
        это лучше сейчас, чем на первом заказе.
        """
        self._suz_answer = ""
        self._set_busy(False)
        self._show_suz(service.save_suz(credentials, self.contour))
        self.notify("Токен СУЗ получен по сертификату", ToastKind.SUCCESS)
        if credentials.filled:
            self.check_suz()

    def check_suz(self) -> None:
        """Спрашивает СУЗ, принимает ли она эти реквизиты.

        Обращение к сети — значит, фоновая задача: до ответа проходят секунды, и
        всё это время окно не должно казаться повисшим.
        """
        self._set_busy(True)
        self.suz_hint.setText("Спрашиваем СУЗ, принимает ли она эти реквизиты…")
        run_task(
            service.check_suz,
            self.suz_credentials, self.contour, self.thumbprint,
            self.settings.marking_inn,
            on_result=self._on_suz_checked,
            on_error=self._on_suz_error,
        )

    def _on_suz_checked(self, outcome: tuple) -> None:
        """Удачная проверка заодно сохраняет: проверяли именно эти значения.

        Заставлять после удачной проверки нажать ещё и «Сохранить» — верный
        способ получить настроенное соединение, которое не переживёт перезапуск.
        """
        answer, credentials = outcome
        # Ответ запоминается до снятия занятости: подсказка перерисовывается
        # уже с ним, иначе на экране осталось бы «спрашиваем СУЗ…».
        self._suz_answer = answer
        self._set_busy(False)
        # Сохраняются те реквизиты, с которыми получилось: токен мог обновиться
        # по дороге, и записать поверх него набранное в поле значило бы вернуть
        # протухший.
        renewed = credentials.token != self.suz_credentials.stripped().token
        self._show_suz(service.save_suz(credentials, self.contour))
        self.notify(
            f"{answer} · токен обновлён по сертификату" if renewed
            else f"{answer} · реквизиты сохранены", ToastKind.SUCCESS)
        # Связь есть — значит и заказы теперь можно прочитать.
        self.reload_orders()

    def _on_suz_error(self, message: str) -> None:
        self._suz_answer = ""
        self._set_busy(False)
        self.suz_hint.setText(f"Связи с СУЗ нет: {message}")
        self.suz_hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.notify(f"СУЗ не ответила: {message}", ToastKind.ERROR)

    def forget_suz(self) -> None:
        service.forget_suz(self.contour)
        self._load_suz()
        self.notify(f"Реквизиты СУЗ забыты · {self.contour.title}", ToastKind.INFO)

    # --- новый заказ ------------------------------------------------------------------

    @property
    def order_request(self) -> "orders_module.Request":
        """Заказ в том виде, в каком он уйдёт в СУЗ."""
        lines = []
        for row in range(self.lines.rowCount()):
            gtin = self.lines.item(row, 0)
            quantity = self.lines.item(row, 1)
            lines.append(orders_module.Line(
                gtin=gtin.text().strip() if gtin else "",
                quantity=_number(quantity.text() if quantity else ""),
                template_id=self._template_id(),
            ))
        return orders_module.Request(
            product_group=str(self.group_box.currentData() or ""),
            method=ReleaseMethod(str(self.method_box.currentData() or "REMAINS")),
            contact=self.contact_edit.text(),
            payment_type=self.payment_box.value(),
            lines=lines,
            seen_templates=self._seen_templates(),
        )

    def _seen_templates(self) -> frozenset[int]:
        """Шаблоны, которыми эту группу уже заказывали.

        Справочник может отстать от системы, а принятый ею шаблон — довод
        сильнее руководства.
        """
        group = str(self.group_box.currentData() or "").strip().lower()
        return frozenset(
            int(buffer.raw.get("templateId") or 0)
            for order in self._orders
            if order.product_group.strip().lower() == group
            for buffer in order.buffers
            if buffer.raw.get("templateId")
        )

    def _on_group_changed(self) -> None:
        self._sync_template()
        self._sync_order_hint()

    def _sync_template(self) -> None:
        """Заполняет список шаблонов группы и выбирает подходящий.

        Основа — справочник из руководства СУЗ: у каждой товарной группы свой
        набор, и чужой шаблон система отвергает. История заказов уточняет выбор
        там, где шаблонов у группы несколько.
        """
        group = str(self.group_box.currentData() or "")
        known = orders_module.defaults_for(group, self._orders)
        allowed = orders_module.templates_for(group)

        self.template_box.blockSignals(True)
        self.template_box.clear()
        for template in allowed:
            self.template_box.addItem(template.title, template.id)
        if not allowed:
            # Группы нет в справочнике — запрещать нечем, но и подсказать
            # нечего: номер придётся взять из личного кабинета.
            self.template_box.setEditable(True)
            self.template_box.addItem(str(known.template_id or ""),
                                      known.template_id)
        else:
            self.template_box.setEditable(False)
            if known.known and self.template_box.findData(known.template_id) < 0:
                # Справочник этого шаблона не знает, а СУЗ его когда-то приняла.
                # Реальность старше документа — показываем оба.
                self.template_box.insertItem(
                    0, f"{known.template_id} · из вашего заказа по этой группе",
                    known.template_id)
            if (index := self.template_box.findData(known.template_id)) >= 0:
                self.template_box.setCurrentIndex(index)
        self.template_box.blockSignals(False)

        self.payment_box.blockSignals(True)
        self.payment_box.setValue(known.payment_type)
        self.payment_box.blockSignals(False)

        if known.known:
            when = f" от {known.since:%d.%m.%Y}" if known.since else ""
            self.template_hint.setStyleSheet("")
            self.template_hint.setText(
                f"Как в вашем заказе{when} по этой группе — менять не нужно")
            return
        self.template_hint.setStyleSheet("" if allowed
                                         else f"color: {Palette.WARNING};")
        self.template_hint.setText(
            "Шаблон — из справочника СУЗ для этой товарной группы"
            if allowed else
            "Этой группы нет в справочнике шаблонов — номер возьмите в личном "
            "кабинете СУЗ")

    def _template_id(self) -> int:
        """Выбранный шаблон. У незнакомой группы список правится руками."""
        chosen = self.template_box.currentData()
        if chosen is not None:
            return int(chosen)
        return _number(self.template_box.currentText())

    def add_line(self) -> None:
        row = self.lines.rowCount()
        self.lines.insertRow(row)
        self.lines.setItem(row, 0, QTableWidgetItem(""))
        self.lines.setItem(row, 1, QTableWidgetItem("0"))
        self.lines.editItem(self.lines.item(row, 0))

    def remove_line(self) -> None:
        if (row := self.lines.currentRow()) >= 0:
            self.lines.removeRow(row)
        self._sync_order_hint()

    def create_order(self) -> None:
        """Заводит заказ — единственная здесь операция, которая тратит деньги.

        Подтверждение спрашивается всегда, даже в песочнице: привычка нажимать
        не глядя вырабатывается там, а срабатывает в бою.
        """
        request = self.order_request
        if problems := request.problems:
            self.notify(f"Заказ не отправлен: {problems[0]}", ToastKind.WARNING)
            self._sync_order_hint()
            return
        if not self.thumbprint:
            # Заказ подписывается, и сказать об этом нужно до подтверждения, а
            # не после отказа СУЗ.
            self.notify("Заказ подписывается — выберите сертификат на вкладке "
                        "«Проверка кодов»", ToastKind.WARNING)
            self._sync_order_hint()
            return
        if OrderConfirmDialog(request, self.contour, self).exec() != \
                QDialog.DialogCode.Accepted:
            return
        self._set_busy(True)
        self.order_hint.setText("Отправляем заказ в «Честный ЗНАК»…")
        run_task(
            service.create_suz_order,
            request, self.suz_credentials, self.contour, self.thumbprint,
            self.settings.marking_inn,
            on_result=self._on_order_created,
            on_error=self._on_order_error,
        )

    def _on_order_created(self, outcome: tuple) -> None:
        order_id, credentials = outcome
        self._set_busy(False)
        self._show_suz(service.save_suz(credentials, self.contour))
        self.lines.setRowCount(0)
        self.order_hint.setText(f"Заказ создан: {order_id}")
        self.notify(f"Заказ кодов создан: {order_id}", ToastKind.SUCCESS)
        self.reload_orders()

    def _on_order_error(self, message: str) -> None:
        self._set_busy(False)
        self.order_hint.setText(message)
        self.order_hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.notify(f"Заказ не создан: {message}", ToastKind.ERROR)
        # Список обновляется в любом случае: если связь оборвалась, заказ мог
        # быть создан, и увидеть это нужно до того, как захочется повторить.
        self.reload_orders()

    def _sync_order_hint(self) -> None:
        request = self.order_request
        problems = list(request.problems)
        if not self.thumbprint:
            problems.append("не выбран сертификат, а заказ подписывается")
        if problems:
            self.order_hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.order_hint.setText("Пока нельзя отправить: " + "; ".join(problems) + ".")
            return
        self.order_hint.setStyleSheet("")
        self.order_hint.setText(
            f"К заказу: {_plural(len(request.lines), 'товар', 'товара', 'товаров')}, "
            f"{_plural(request.total, 'код', 'кода', 'кодов')}. При отправке "
            "КриптоПро попросит пароль к контейнеру — заказ подписывается. "
            "Отменить созданный заказ нельзя.")

    def reload_orders(self) -> None:
        """Перечитывает заказы. Чтение — ничего не создаёт и кодов не тратит."""
        credentials = self.suz_credentials.stripped()
        if not credentials.filled:
            self._orders = []
            self.orders.setRowCount(0)
            self._sync_orders_hint()
            return
        self.orders_button.setEnabled(False)
        self.orders_hint.setText("Спрашиваем СУЗ о заказах…")
        run_task(
            service.suz_orders,
            credentials, self.contour, self.thumbprint, self.settings.marking_inn,
            on_result=self._fill_orders,
            on_error=self._on_orders_error,
        )

    def _fill_orders(self, outcome: tuple) -> None:
        found, credentials = outcome
        if credentials.token != self.suz_credentials.stripped().token:
            # Токен обновился по дороге. Показать старый в поле — значит
            # предложить человеку вернуть протухший, нажав «Сохранить».
            self._show_suz(credentials)
            self.notify("Токен СУЗ обновлён по сертификату", ToastKind.INFO)
        self._orders = list(found)
        self.orders.setRowCount(len(self._orders))
        for row, order in enumerate(self._orders):
            when = f"{order.created_at:%d.%m.%Y}" if order.created_at else "—"
            cells = (order.id, when, order.declined or order.title,
                     str(order.products), str(order.passed), str(order.left))
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column in (3, 4, 5):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.orders.setItem(row, column, cell)
            if not order.known:
                # Незнакомое состояние показано дословно — пусть будет видно,
                # что перевода ему не нашлось.
                self.orders.item(row, 2).setForeground(QColor(Palette.WARNING))
            if order.expiring:
                # Коды в просроченном буфере ещё числятся, а забрать их уже
                # нельзя. Показать их числом без оговорки — обмануть.
                self.orders.item(row, 5).setForeground(QColor(Palette.DANGER))
        self.orders_button.setEnabled(True)
        self._sync_orders_hint()
        # В прочитанных заказах и лежит ответ на вопрос «какой шаблон».
        self._sync_template()

    def _on_orders_error(self, message: str) -> None:
        self.orders_button.setEnabled(True)
        self.orders_hint.setText(f"Заказы не прочитаны: {message}")
        self.orders_hint.setStyleSheet(f"color: {Palette.DANGER};")

    def _sync_orders_hint(self) -> None:
        self.orders_hint.setStyleSheet("")
        # Порядок проверок важен: заказы на экране отменяют совет настроить
        # соединение — оно очевидно настроено, раз они прочитаны.
        if not self._orders and not service.suz_ready(self.contour) \
                and not self.suz_credentials.filled:
            self.orders_hint.setText(
                "Заказы читаются из СУЗ — сначала настройте соединение выше.")
            return
        if not self._orders:
            self.orders_hint.setText(
                "Заказов нет. Чтение ничего не создаёт и кодов не тратит — "
                "обновлять можно сколько угодно.")
            return
        left = sum(order.left for order in self._orders)
        tail = ("" if service.ORDERING_READY else
                " Создание заказа пока не подключено — здесь только чтение.")
        if expired := sum(1 for order in self._orders if order.expiring):
            # Про просроченные буферы молчать нельзя: коды в них числятся, а
            # забрать их уже нельзя.
            self.orders_hint.setStyleSheet(f"color: {Palette.WARNING};")
            tail = (f" Заказов с просроченным буфером: {expired} — числящиеся в "
                    "них коды забрать уже нельзя." + tail)
        self.orders_hint.setText(
            f"Заказов: {len(self._orders)} · кодов доступно к получению: {left}."
            f"{tail}")

    def _show_suz(self, credentials: Credentials) -> None:
        """Возвращает в поля то, что действительно сохранено."""
        self.oms_edit.setText(credentials.oms_id)
        self.connection_edit.setText(credentials.connection_id)
        self.token_edit.setText(credentials.token)
        self.suz_host_edit.setText(credentials.host)
        self._sync_suz_hint()

    def _sync_suz_hint(self) -> None:
        if self._busy:
            return
        self.suz_hint.setStyleSheet("")
        self.suz_hint.setText(self._suz_summary())

    def _suz_summary(self) -> str:
        current = self.suz_credentials.stripped()
        if self._suz_answer:
            tail = ("" if service.ORDERING_READY else
                    " Заказ кодов пока не подключён — соединение настроено заранее.")
            return f"{self._suz_answer}.{tail}"
        if not any((current.oms_id, current.connection_id, current.token)):
            return ("ОМС ID берётся из личного кабинета СУЗ, а токен переносить "
                    "из другой программы не нужно: кнопка «Получить токен» возьмёт "
                    "его у самой СУЗ по сертификату. Реквизиты нужны только для "
                    "заказа кодов — проверка кодов и вход в ГИС МТ от них не зависят.")
        if gaps := current.missing:
            return (f"Не хватает: {', '.join(gaps)}. ОМС ID — из личного кабинета "
                    "СУЗ, токен — кнопкой «Получить токен» по сертификату.")
        return (f"Реквизиты заполнены · токен {current.masked_token} · "
                f"{_life(current)}. Реквизиты свои у каждого контура, и "
                "переключение контура показывает другой набор.")

    # --- коды --------------------------------------------------------------------

    def _reparse(self) -> None:
        """Разбирает то, что набрано в поле. Ни сети, ни входа для этого не нужно."""
        lines = codes_module.split_lines(self.codes_edit.toPlainText())
        self._batch = codes_module.parse_many(lines)
        self.tile_codes.set_value(len(self._batch.codes))
        self.tile_items.set_value(len(self._batch.by_gtin))
        self.tile_broken.set_value(len(self._batch.broken))
        self.tile_repeats.set_value(len(self._batch.duplicates))
        self.codes_hint.setText(self._codes_summary())
        trouble = bool(self._batch.broken or self._batch.duplicates)
        self.codes_hint.setStyleSheet(
            f"color: {Palette.WARNING};" if trouble else "")
        self._sync_state()

    def _codes_summary(self) -> str:
        if not self._batch.codes:
            return "Пока ни одного кода."
        parts = [self._batch.summary]
        if broken := self._batch.broken:
            first = broken[0]
            reason = first.problems[0] if first.problems else "код не разобран"
            parts.append(f"например: «{first.raw[:40]}» — {reason}")
            parts.append("испорченные коды в запрос не уходят")
        if self._batch.duplicates:
            parts.append("повтор — это либо пересчёт, либо копия кода")
        return " · ".join(parts)

    def load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Список кодов", "", "Списки кодов (*.txt *.csv);;Все файлы (*.*)")
        if not path:
            return
        try:
            text = _read_text(path)
        except OSError as error:
            self.notify(f"Не удалось прочитать файл: {error}", ToastKind.ERROR)
            return
        existing = self.codes_edit.toPlainText()
        self.codes_edit.setPlainText(f"{existing}\n{text}" if existing.strip() else text)
        self._reparse()

    def clear_codes(self) -> None:
        self.codes_edit.clear()
        self._result = []
        self._batch = codes_module.Batch()
        self._reparse()
        self._fill_result()
        self.result_hint.setText("Список кодов очищен.")

    # --- проверка -----------------------------------------------------------------

    def run_check(self) -> None:
        if not service.signed_in():
            self.notify("Сначала войдите по сертификату", ToastKind.WARNING)
            return
        wanted = [code.raw for code in self._batch.valid]
        if not wanted:
            self.notify("Нет ни одного разобранного кода", ToastKind.WARNING)
            return
        self._set_busy(True)
        self.result_hint.setText("Спрашиваем «Честный ЗНАК»…")
        run_task(
            _check,
            wanted, self.contour, not self.fresh_box.isChecked(),
            on_result=self._show_result,
            on_error=self._on_check_error,
            on_progress=lambda done, total: self.result_hint.setText(
                f"Спрашиваем «Честный ЗНАК»… {done} из {total}"),
        )

    def _on_check_error(self, message: str) -> None:
        self._set_busy(False)
        self.result_hint.setText("Проверка не выполнена.")
        self.notify(f"Не удалось проверить коды: {message}", ToastKind.ERROR)

    def _show_result(self, found: list[CodeInfo]) -> None:
        self._result = list(found)
        self._set_busy(False)
        self.tile_circulation.set_value(sum(1 for i in found if i.state.sellable))
        self.tile_elsewhere.set_value(
            sum(1 for i in found if i.found and not i.state.sellable))
        self.tile_alien.set_value(sum(1 for i in found if i.owner_inn and not i.ours))
        self.tile_missing.set_value(sum(1 for i in found if not i.found))
        self._fill_result()
        self.reload_journal()

    def _troubled(self, item: CodeInfo) -> bool:
        """Код, на котором стоит остановиться: чужой, не в обороте или неизвестный."""
        return (bool(item.problems) or not item.found or not item.state.sellable
                or (bool(item.owner_inn) and not item.ours))

    def _fill_result(self) -> None:
        shown = [item for item in self._result
                 if not self.problems_box.isChecked() or self._troubled(item)]
        limited = shown[:RESULT_LIMIT]
        self.result.setRowCount(len(limited))
        for row, item in enumerate(limited):
            owner = item.owner_name or item.owner_inn or "—"
            if item.owner_inn and not item.ours:
                owner = f"{owner} · чужой"
            cells = (item.product_name or "—",
                     f"{item.gtin} · {item.serial}" if item.gtin else item.code[:40],
                     item.state.title,
                     owner,
                     "; ".join(item.problems))
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                # Подсказка на каждой ячейке: длинное название и длинное
                # замечание в таблицу целиком не помещаются, а прочитать их
                # нужно — иначе непонятно, что именно не так с кодом.
                cell.setToolTip(text)
                if column == 2:
                    cell.setForeground(QColor(_state_color(item)))
                self.result.setItem(row, column, cell)
        if not self._result:
            self.result_hint.setText(
                "Проверка спрашивает «Честный ЗНАК» о каждом коде: чей он, в "
                "обороте ли и не выведен ли уже.")
            return
        troubled = sum(1 for item in self._result if self._troubled(item))
        tail = (f", показаны первые {RESULT_LIMIT}"
                if len(shown) > RESULT_LIMIT else "")
        self.result_hint.setText(
            f"Проверено кодов: {len(self._result)} · требуют внимания: {troubled}"
            f"{tail}")

    # --- журнал --------------------------------------------------------------------

    def reload_journal(self) -> None:
        run_task(
            service.history,
            JOURNAL_LIMIT,
            on_result=self._fill_journal,
            on_error=lambda message: self.notify(
                f"Не удалось прочитать журнал операций: {message}", ToastKind.ERROR),
        )

    def _fill_journal(self, operations: list[Operation]) -> None:
        self._journal = list(operations)
        self.journal.setRowCount(len(self._journal))
        for row, operation in enumerate(self._journal):
            when = (f"{operation.created_at:%d.%m.%Y %H:%M}"
                    if operation.created_at else "—")
            cells = (when, operation.kind.title, str(operation.size),
                     operation.contour.title, operation.error or operation.status.title)
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column == 2:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.journal.setItem(row, column, cell)
        self._sync_journal_hint()

    def _sync_journal_hint(self) -> None:
        waiting = sum(1 for operation in self._journal if operation.status.pending)
        if waiting and not service.STATUS_POLLING_READY:
            # Молчать об этом нельзя: операция висит не потому, что ГИС МТ
            # думает, а потому, что спросить её приложение пока не умеет.
            self.journal_hint.setText(
                f"Ждут ответа ГИС МТ: {waiting}. Опрос состояния пока не "
                "подключён — состояние таких операций смотрите в личном кабинете.")
            self.journal_hint.setStyleSheet(f"color: {Palette.WARNING};")
            return
        self.journal_hint.setStyleSheet("")
        self.journal_hint.setText(
            "Операции хранятся на этом компьютере: они подписаны личным "
            "сертификатом и через общий сервер не проходят.")

    # --- сверка с документом ----------------------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        if index == RECONCILE_TAB:
            self._focus_scan()

    def _focus_scan(self) -> None:
        """Курсор в поле сканирования. Без этого сканер печатает мимо."""
        if self._session is not None:
            self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def load_upd(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "УПД с кодами маркировки", "",
            "Документы ЭДО (*.xml *.zip);;Все файлы (*.*)")
        if not path:
            return
        # Разбор идёт прямо здесь, без фоновой задачи: документ на сотню строк —
        # это два десятка килобайт, и окно не успевает моргнуть. Фоновая задача
        # ради такого только развела бы обработку ошибок по двум местам.
        try:
            document = upd_module.read(path)
        except UpdProblem as problem:
            self.notify(str(problem), ToastKind.ERROR)
            return
        self._upd = document
        self._session = Reconciliation(document)
        self._clear_verdict()
        self._rematch()
        self._sync_reconcile()
        self.tabs.setCurrentIndex(RECONCILE_TAB)
        self._focus_scan()
        if repeated := self._session.repeated_in_document:
            # Это ошибка поставщика, и заметить её нужно до сверки: строку с
            # таким кодом не закрыть в принципе — второй такой вещи в коробке
            # нет, а недостача покажется нашей.
            self.notify(
                f"В документе {len(repeated)} кодов записаны дважды — "
                "сверить их до конца не выйдет, это ошибка поставщика",
                ToastKind.WARNING)
        else:
            self.notify(f"{document.title}: к сверке {self._session.total} кодов",
                        ToastKind.SUCCESS)

    def forget_upd(self) -> None:
        self._upd = None
        self._session = None
        self._matches = []
        self.scan_edit.clear()
        self._clear_verdict()
        self._sync_reconcile()

    def accept_scan(self) -> None:
        """Принимает один код из поля сканирования.

        Поле очищается всегда — и после удачного скана, и после лишнего кода.
        Оставленный в нём код сканер допишет своим, и следующая вещь окажется
        «не разобрана» по нашей вине.
        """
        text = self.scan_edit.text().strip()
        self.scan_edit.clear()
        if self._session is None:
            if text:
                self.notify("Сначала загрузите УПД, с которым сверяемся",
                            ToastKind.WARNING)
            return
        if not text:
            return
        scan = self._session.scan(text)
        self._show_verdict(scan)
        self._sync_reconcile()
        self._reveal(scan)

    def undo_scan(self) -> None:
        if self._session is None:
            return
        scan = self._session.undo()
        if scan is None:
            self.notify("Отменять нечего: ни одного скана ещё не было",
                        ToastKind.INFO)
            return
        self._clear_verdict(f"Скан снят: {scan.title}")
        self._sync_reconcile()
        self._focus_scan()

    def restart_reconcile(self) -> None:
        if self._session is None:
            return
        self._session.reset()
        self._clear_verdict("Сверка начата заново.")
        self._sync_reconcile()
        self._focus_scan()

    def _on_sound_changed(self, enabled: bool) -> None:
        self.settings.marking_reconcile_sound = enabled
        self.settings.save()

    def _show_verdict(self, scan: Scan) -> None:
        """Ответ на скан: цвет, товар и — если что-то не так — что именно."""
        color, background = VERDICT_COLORS[scan.verdict]
        where = f" · {scan.line.title}" if scan.line else ""
        note = f" — {scan.note}" if scan.note else ""
        self.verdict_label.setText(f"{scan.verdict.title}{where}{note}")
        self.verdict_label.setStyleSheet(
            f"color: {color}; background: {background}; font-size: 15px;"
            f" font-weight: 600; border-radius: {Metrics.RADIUS}px;"
            " padding: 10px 14px;")
        if not scan.verdict.good and self.sound_box.isChecked():
            QApplication.beep()

    def _clear_verdict(self, text: str = "") -> None:
        self.verdict_label.setText(text)
        self.verdict_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED}; padding: 10px 14px;" if text else "")

    def _sync_reconcile(self) -> None:
        """Приводит плитки, таблицы и кнопки сверки к тому, что уже сверено."""
        session = self._session
        document = self._upd
        self.tile_upd_lines.set_value(len(document.marked_lines) if document else 0)
        self.tile_upd_codes.set_value(len(document.marks) if document else 0)
        self.tile_scanned.set_value(session.done if session else 0)
        self.tile_left.set_value(session.left if session else 0)
        self.tile_extra.set_value(len(session.extra) if session else 0)
        self.tile_scan_repeats.set_value(session.repeats if session else 0)

        self.upd_hint.setText(
            document.summary if document else
            "Документ не загружен. Нужен тот же XML, что пришёл по ЭДО, — он "
            "лежит в папке с документом рядом с печатной формой.")
        self.scan_edit.setEnabled(session is not None)
        for button in (self.upd_check_button, self.upd_report_button,
                       self.upd_forget_button, self.undo_button,
                       self.restart_button):
            button.setEnabled(session is not None)

        if session is None:
            self.scan_hint.setText("")
            self.scan_hint.setStyleSheet("")
        else:
            self.scan_hint.setText(session.summary)
            # Цветом помечается только несошедшееся: зелёный на каждой подписи
            # перестаёт что-либо значить к третьей коробке.
            trouble = bool(session.extra) or bool(session.repeats)
            self.scan_hint.setStyleSheet(
                f"color: {Palette.DANGER};" if trouble else "")
        self._fill_progress()
        self._fill_extra()
        self._sync_onec_hint()

    def _reveal(self, scan: Scan) -> None:
        """Подводит таблицу к строке, по которой только что пикнули.

        В документе на полсотни строк нужная почти всегда за пределами экрана,
        а посмотреть на неё хочется ровно после скана: сколько по ней осталось —
        это и есть «доставать ли из коробки ещё такую же».
        """
        if scan.line is None:
            return
        for row in range(self.progress.rowCount()):
            cell = self.progress.item(row, 0)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == scan.line.number:
                self.progress.selectRow(row)
                self.progress.scrollToItem(
                    cell, QAbstractItemView.ScrollHint.PositionAtCenter)
                return

    def _fill_progress(self) -> None:
        items = self._session.progress if self._session else []
        if self.open_only_box.isChecked():
            items = [item for item in items if not item.done]
        self.progress.setRowCount(len(items))
        for row, item in enumerate(items):
            found = self._match_of(item.line.number)
            cells = (item.line.title, item.line.gtin, str(item.expected),
                     str(item.scanned), item.state, _onec_cell(found))
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setToolTip(_onec_tip(found) if column == 5 else text)
                if column == 0:
                    # Номер строки документа держится в самой ячейке: искать её
                    # потом по названию или GTIN значило бы промахнуться на
                    # двух строках одного товара.
                    cell.setData(Qt.ItemDataRole.UserRole, item.line.number)
                if column in (2, 3):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                if column == 4:
                    cell.setForeground(QColor(
                        Palette.SUCCESS if item.done else Palette.WARNING))
                if column == 5 and (colour := _onec_colour(found)):
                    cell.setForeground(QColor(colour))
                self.progress.setItem(row, column, cell)
        if self._session is None:
            self.progress_hint.setText(
                "Сверка идёт без сети и без сертификата: документ и коды на "
                "товаре сравниваются на этом компьютере.")
            return
        if self._session.complete:
            self.progress_hint.setText(
                "Сошлось: всё из документа найдено, лишнего не приехало. "
                "Документ можно подписывать.")
            self.progress_hint.setStyleSheet(f"color: {Palette.SUCCESS};")
            return
        self.progress_hint.setStyleSheet("")
        # «Осталось», а не «не найдено»: посреди сверки непросканированное —
        # это ещё не недостача, а очередь. Недостачей оно станет, когда коробка
        # кончится, и решает это человек, а не программа.
        self.progress_hint.setText(
            f"Осталось просканировать: {self._session.left} · лишних кодов: "
            f"{len(self._session.extra)}. Расхождения стоит закрыть до подписания "
            "УПД: после него товар уже числится за нами.")

    def _fill_extra(self) -> None:
        found = self._session.extra if self._session else []
        self.extra.setRowCount(len(found))
        for row, scan in enumerate(found):
            cells = (f"{scan.at:%H:%M:%S}", scan.raw, scan.note)
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if column == 2:
                    cell.setForeground(QColor(Palette.DANGER))
                self.extra.setItem(row, column, cell)
        self.extra_card.setVisible(bool(found))

    def codes_to_check(self) -> None:
        """Переносит коды документа на вкладку проверки.

        Сверка и проверка отвечают на разные вопросы, и один без другого
        неполон: сверка говорит, что приехало ровно то, что в документе, а
        «Честный ЗНАК» — что эти коды в обороте и принадлежат поставщику.
        """
        if self._upd is None:
            return
        text = "\n".join(mark.value for _, mark in self._upd.marks)
        existing = self.codes_edit.toPlainText().strip()
        self.codes_edit.setPlainText(f"{existing}\n{text}" if existing else text)
        self._reparse()
        self.tabs.setCurrentIndex(CHECK_TAB)
        self.notify(
            f"Перенесено кодов: {len(self._upd.marks)}. "
            "Для проверки нужен вход по сертификату", ToastKind.INFO)

    def save_report(self) -> None:
        if self._session is None:
            return
        folder = os.path.dirname(self._upd.source) if self._upd else ""
        suggested = reconcile_module.default_name(self._session, folder)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить акт сверки", suggested, "Excel (*.xlsx)")
        if not path:
            return
        run_task(
            reconcile_module.save_report,
            self._session, path,
            on_result=lambda saved: self.notify(
                f"Акт сверки сохранён: {os.path.basename(saved)}",
                ToastKind.SUCCESS),
            on_error=lambda message: self.notify(
                f"Не удалось сохранить акт: {message}", ToastKind.ERROR),
        )

    # --- выгрузка в 1С ------------------------------------------------------------

    def load_catalog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выгрузка номенклатуры из 1С",
            self.settings.marking_onec_catalog,
            "Книги Excel (*.xlsx *.xls);;Все файлы (*.*)")
        if not path:
            return
        if not self._read_catalog(path, quiet=False):
            return
        self.settings.marking_onec_catalog = path
        self.settings.save()
        self._rematch()
        self.notify(f"Номенклатура 1С: {len(self._catalog.items)} позиций",
                    ToastKind.SUCCESS)

    def _read_catalog(self, path: str, quiet: bool = True) -> bool:
        """Читает выгрузку. Молча — только когда её никто не просил читать."""
        try:
            self._catalog = onec_module.read_catalog(path)
        except OnecProblem as problem:
            self._catalog = None
            if not quiet:
                self.notify(str(problem), ToastKind.ERROR)
            return False
        return True

    def _restore_catalog(self) -> None:
        """Перечитывает выгрузку прошлого запуска.

        Молча: файл могли переложить или удалить, и упрёк об этом при каждом
        открытии вкладки — не то, ради чего её открывают. Что выгрузки нет,
        видно по подписи под кнопкой.
        """
        path = self.settings.marking_onec_catalog
        if path and os.path.exists(path):
            self._read_catalog(path)

    def _rematch(self) -> None:
        """Заново раскладывает строки документа по номенклатуре 1С."""
        if self._upd is None or self._catalog is None:
            self._matches = []
        else:
            self._matches = onec_module.match(
                self._upd.marked_lines, self._catalog,
                self.settings.marking_onec_links)
        self._fill_progress()
        self._sync_onec_hint()

    def _match_of(self, number: str) -> LineMatch | None:
        return next((item for item in self._matches
                     if item.line.number == number), None)

    def _on_progress_activated(self, cell: QTableWidgetItem) -> None:
        number = self.progress.item(cell.row(), 0)
        if number is not None:
            self._link_line(str(number.data(Qt.ItemDataRole.UserRole) or ""))

    def link_selected(self) -> None:
        row = self.progress.currentRow()
        if row < 0:
            self.notify("Выберите строку, которой нужно назначить номенклатуру",
                        ToastKind.INFO)
            return
        cell = self.progress.item(row, 0)
        if cell is not None:
            self._link_line(str(cell.data(Qt.ItemDataRole.UserRole) or ""))

    def _link_line(self, number: str) -> None:
        """Спрашивает номенклатуру для строки и запоминает ответ по GTIN."""
        if self._catalog is None:
            self.notify("Сначала загрузите выгрузку номенклатуры из 1С",
                        ToastKind.WARNING)
            return
        found = self._match_of(number)
        if found is None:
            return
        current = found.item.key if found.item else ""
        dialog = NomenclatureDialog(found.line, self._catalog, self, current)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        key = onec_module.gtin_key(found.line.gtin)
        if not key:
            # Без кода товара привязку не к чему привязать: на следующей
            # поставке та же строка придёт как новая.
            self.notify("У строки нет кода товара — привязку не запомнить",
                        ToastKind.WARNING)
            return
        if dialog.cleared:
            self.settings.marking_onec_links.pop(key, None)
        else:
            chosen = dialog.chosen
            if chosen is None:
                return
            self.settings.marking_onec_links[key] = {
                "code": chosen.code, "feature": chosen.feature,
                "name": chosen.name}
        self.settings.save()
        self._rematch()

    def save_onec(self) -> None:
        if self._session is None or not self._matches:
            return
        folder = os.path.dirname(self._upd.source) if self._upd else ""
        suggested = onec_module.default_name(self._session, folder)
        path, _ = QFileDialog.getSaveFileName(
            self, "Файл для загрузки в 1С", suggested, "Excel (*.xlsx)")
        if not path:
            return
        try:
            report = onec_module.save(
                self._matches, self._session, path,
                self.only_scanned_box.isChecked())
        except OnecProblem as problem:
            self.notify(str(problem), ToastKind.ERROR)
            return
        except OSError as error:
            self.notify(f"Не удалось сохранить файл: {error}", ToastKind.ERROR)
            return
        self._sync_onec_hint(report)
        if report.clean:
            self.notify(f"Готово к загрузке в 1С: {report.rows} кодов",
                        ToastKind.SUCCESS)
        else:
            # Молчать об этом нельзя: загруженная половина выглядит в 1С точно
            # так же, как загруженное целиком.
            self.notify(
                f"Записано {report.rows} кодов, но {report.skipped_codes} не "
                f"попало: {len(report.skipped_lines)} позиций без номенклатуры",
                ToastKind.WARNING)

    def _sync_onec_hint(self, report: onec_module.Export | None = None) -> None:
        ready = bool(self._session and self._matches
                     and any(item.ready for item in self._matches))
        self.onec_button.setEnabled(ready)
        self.link_button.setEnabled(self._catalog is not None and bool(self._matches))
        self.only_scanned_box.setEnabled(self._session is not None)
        if self._catalog is None:
            self.onec_hint.setStyleSheet("")
            self.onec_hint.setText(
                "Выгрузка номенклатуры не загружена. Нужны название, код "
                "номенклатуры и характеристика — по ним коды и лягут на товар "
                "в 1С.")
            return
        parts = [self._catalog.summary]
        if self._upd is not None:
            parts.append(onec_module.summarize(self._matches))
        if report is not None:
            parts.append(report.summary)
        self.onec_hint.setText(" · ".join(parts))
        waiting = sum(1 for item in self._matches if not item.ready)
        self.onec_hint.setStyleSheet(
            f"color: {Palette.WARNING};" if waiting else "")

    # --- горячие клавиши ----------------------------------------------------------

    def run_current(self) -> None:
        """Что делает F5 на этой странице. Зависит от открытой подвкладки.

        На сверке проверять нечего: она идёт непрерывно, скан за сканом. Зато
        ровно там F5 нужнее всего — вернуть курсор в поле сканирования, если он
        уехал в таблицу или в переключатель. А без документа F5 предлагает его
        выбрать: это и есть первое действие на странице.
        """
        if self.tabs.currentIndex() != RECONCILE_TAB:
            self.run_check()
            return
        if self._session is None:
            self.load_upd()
            return
        self._focus_scan()

    def save_current(self) -> None:
        """Что сохраняет Ctrl+S. На сверке — акт, на остальных подвкладках нечего."""
        if self.tabs.currentIndex() == RECONCILE_TAB and self._session is not None:
            self.save_report()

    # --- состояние страницы ----------------------------------------------------------

    def restore(self) -> None:
        """Читает сертификаты, реквизиты СУЗ, заказы и журнал при открытии вкладки."""
        self.reload_certificates()
        self.reload_journal()
        self._load_suz()
        self.reload_orders()
        self._restore_catalog()
        self._rematch()
        self._sync_state()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_state()

    def _sync_state(self) -> None:
        """Приводит кнопки и подпись входа в соответствие с тем, что происходит."""
        signed = service.signed_in()
        self.login_button.setEnabled(bool(self.thumbprint) and not self._busy
                                     and not signed)
        self.logout_button.setEnabled(signed and not self._busy)
        self.certificates_button.setEnabled(not self._busy)
        self.organisation_button.setEnabled(bool(self.thumbprint) and not self._busy)
        self.certificate_box.setEnabled(not self._busy and not signed)
        self.contour_box.setEnabled(not self._busy)
        self.check_button.setEnabled(
            signed and bool(self._batch.valid) and not self._busy)
        # Проверка связи с СУЗ входа по сертификату не требует: это другая
        # система и другой способ представиться.
        self.suz_check_button.setEnabled(not self._busy)
        self.suz_save_button.setEnabled(not self._busy)
        self.suz_forget_button.setEnabled(not self._busy)
        # А вот получение токена требует ключа — без выбранного сертификата
        # подписывать нечем.
        self.suz_token_button.setEnabled(bool(self.thumbprint) and not self._busy)
        self.order_button.setEnabled(not self._busy)
        if not self._busy:
            self.session_hint.setText(self._session_summary())
        # Боевой контур помечается цветом всегда: в песочнице ошибка ничего не
        # стоит, а здесь — стоит, и знать об этом нужно до нажатия, а не после.
        self.session_hint.setStyleSheet(
            f"color: {Palette.DANGER};"
            if self.contour is Contour.PRODUCTION else "")

    def _session_summary(self) -> str:
        if not self._crypto_available:
            return ("КриптоПро CSP на этом компьютере не найден. Без него подписать "
                    "вход нечем: система маркировки не пускает по паролю.")
        if not self._certificates:
            return ("Личных сертификатов не найдено. Подключите носитель ключа и "
                    "нажмите «Обновить список».")
        if service.signed_in():
            current = service.current()
            who = current.organisation or current.owner
            inn = f" · ИНН {current.inn}" if current.inn else ""
            return f"Вошли: {who}{inn} · {current.contour.title}"
        # Организация показывается до входа: сертификат действует за нескольких,
        # и знать, под кем пойдёт вход, нужно до нажатия, а не после.
        chosen = ""
        if self.settings.marking_inn:
            name = self.settings.marking_organisation or "организация"
            chosen = f" Вход под: {name} · ИНН {self.settings.marking_inn}."
        if self.contour is Contour.PRODUCTION:
            return ("Боевой контур: запросы уходят в настоящую систему. Проверка "
                    "кодов ничего в ней не меняет, но входить нужно рабочим "
                    f"сертификатом.{chosen}")
        return ("Песочница: данные ненастоящие, ошибка ничего не стоит. Для "
                f"настоящих кодов переключите контур на боевой.{chosen}")


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Число со словом в правильном падеже: «1 товар», «2 товара», «5 товаров».

    Сводку перед заказом читают, чтобы поймать ошибку, и «1 товаров» отвлекает
    ровно в тот момент, когда отвлекаться нельзя.
    """
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        word = one
    elif 2 <= tail <= 4 and not 12 <= hundred <= 14:
        word = few
    else:
        word = many
    return f"{count} {word}"


def _number(text: str) -> int:
    """Число из ячейки таблицы. Всё, что не число, — ноль, а не исключение."""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else 0


def _life(credentials: Credentials) -> str:
    """Сколько токену осталось жить.

    Токен СУЗ действует десять часов, и до сих пор его продление означало
    сходить в другую программу. Теперь приложение обновляет его само, но знать,
    что срок на исходе, человек должен заранее — иначе обновление случится
    посреди работы и вызовет неожиданный вопрос о пароле к контейнеру.
    """
    left = credentials.expires_in
    if left is None:
        return ("когда он получен, неизвестно — при первом же обращении будет "
                "заменён своим")
    if left.total_seconds() <= 0:
        return "срок вышел — будет обновлён по сертификату при первом обращении"
    hours, minutes = divmod(int(left.total_seconds()) // 60, 60)
    remains = f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"
    return f"действует ещё {remains}"


def _merge(fetched: Sequence[Organisation],
           saved: Sequence[Organisation]) -> list[Organisation]:
    """Список системы плюс то, что добавляли руками.

    Первым идёт ответ ГИС МТ: он свежий и с полными названиями. Введённое
    руками не пропадает — оно и появилось потому, что справочник был недоступен.
    """
    known = list(fetched)
    seen = {item.inn for item in known}
    known.extend(item for item in saved if item.inn not in seen)
    return known


def _load_certificates() -> tuple[bool, list]:
    """Сертификаты вместе с признаком «есть ли вообще КриптоПро».

    Оба обращения идут к COM, поэтому выполняются в фоновой задаче: первый же
    вызов из потока интерфейса подвесил бы окно на время опроса хранилища.
    """
    if not service.crypto_available():
        return False, []
    return True, service.certificates()


def _check(raw_codes: Sequence[str], contour: Contour, use_cache: bool,
           progress=None) -> list[CodeInfo]:
    """Проверка кодов вместе с записью её в журнал.

    Запись делается здесь, а не после возврата в интерфейс: журнал должен
    отвечать на вопрос «что мы спрашивали», даже если окно закрыли сразу после
    ответа.
    """
    found = service.check(raw_codes, contour=contour, use_cache=use_cache,
                          progress=progress)
    service.record_check(raw_codes, contour=contour)
    return found


def _read_text(path: str) -> str:
    """Список кодов из файла. Кодировка выясняется перебором.

    Такие списки приходят и из блокнота, и выгрузкой из 1С: utf-8 и cp1251
    встречаются одинаково часто, а ошибка кодировки выглядит как «все коды
    испорчены».
    """
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            with open(path, encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _onec_cell(found: LineMatch | None) -> str:
    """Что показать в колонке «Номенклатура 1С»."""
    if found is None:
        return "—"
    if found.ready and found.item is not None:
        return found.item.short
    if found.candidates:
        # У неподтверждённой строки показывается название, а не код: решают
        # «то же это или не то» по названию, а код номенклатуры об этом не
        # говорит человеку ничего.
        return f"похоже: {found.candidates[0].name}"
    return "не найдено"


def _onec_tip(found: LineMatch | None) -> str:
    """Подсказка на ячейке. Короткого «00-00127672 · M» для решения мало."""
    if found is None:
        return "Выгрузка номенклатуры 1С не загружена — сопоставлять не с чем"
    if found.ready and found.item is not None:
        return f"{found.item.name}\n{found.kind.title}"
    if found.candidates:
        names = "\n".join(f"• {item.name} · {item.short}"
                          for item in found.candidates[:5])
        return ("Само не применяется — подтвердите двойным щелчком.\n"
                f"Ближайшее в 1С:\n{names}")
    return ("В выгрузке нет ничего похожего. Заведите товар в 1С и перечитайте "
            "выгрузку либо привяжите вручную")


def _onec_colour(found: LineMatch | None) -> str:
    if found is None:
        return ""
    if found.ready:
        return Palette.SUCCESS if found.kind is not MatchKind.MANUAL else Palette.PRIMARY
    return Palette.WARNING if found.candidates else Palette.DANGER


def _state_color(item: CodeInfo) -> str:
    if not item.found:
        return Palette.DANGER
    if item.state.sellable:
        return Palette.SUCCESS
    return Palette.WARNING
