"""Графики дашборда на QtCharts: наведение, подсказки, переход по щелчку.

QtCharts входит в PySide6, поэтому новых зависимостей не появляется и отрисовка
остаётся нативной. Библиотека может отсутствовать в урезанной сборке — тогда
вместо графика показывается подпись, а страница продолжает работать.

Три решения по оформлению стоит пояснить.

Значения на осях сокращаются до миллионов или тысяч, а единица уходит в подпись
оси. Подписи вида «12147620» занимают половину графика и всё равно не читаются;
точное число показывается в подсказке при наведении.

Столбец и сектор отвечают на щелчок: график — не картинка, а способ добраться до
строк. Щелчок по поставщику отбирает его платежи, щелчок по дню или месяцу
открывает календарь на этой дате.

У круговой диаграммы подписи вынесены в легенду. Внешние подписи накладывались
друг на друга, как только у двух-трёх поставщиков доля падала до пары процентов.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from PySide6.QtCore import QMargins, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..theme import Palette

try:
    from PySide6.QtCharts import (
        QBarCategoryAxis,
        QBarSeries,
        QBarSet,
        QChart,
        QChartView,
        QHorizontalBarSeries,
        QPieSeries,
        QValueAxis,
    )
except ImportError:  # pragma: no cover — сборка без QtCharts
    QChart = None  # type: ignore[assignment]

# Палитра серий: первый цвет — основной, остальные для круговой диаграммы.
SERIES_COLORS: tuple[str, ...] = (
    Palette.PRIMARY, "#0891b2", "#16a34a", "#d97706", "#dc2626",
    "#7c3aed", "#db2777", "#0d9488", "#65a30d", "#9333ea",
)

# Больше десяти секторов круговая диаграмма не различает: остальное сводится
# в «Прочие», иначе легенда занимает больше места, чем сам график.
MAX_SLICES = 8


def available() -> bool:
    return QChart is not None


def money(value: float) -> str:
    """Полная сумма с разделителями — для подсказок."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _scale(top: float) -> tuple[float, str]:
    """Делитель и единица для осей: «12 147 620» превращается в «12,1» и «млн ₽»."""
    if top >= 1_000_000:
        return 1_000_000.0, "млн ₽"
    if top >= 1_000:
        return 1_000.0, "тыс ₽"
    return 1.0, "₽"


def _lighten(color: str, factor: float = 0.62) -> QColor:
    """Осветлённый цвет для второй серии — план рядом с фактом.

    Осветление сильное намеренно: при слабом план и факт на узких столбцах
    сливаются в один оттенок, и разделение перестаёт что-либо значить.
    """
    base = QColor(color)
    return QColor(
        int(base.red() + (255 - base.red()) * factor),
        int(base.green() + (255 - base.green()) * factor),
        int(base.blue() + (255 - base.blue()) * factor),
    )


def _darken(color: str, factor: float = 0.32) -> QColor:
    """Затемнённый оттенок для подсветки под курсором.

    Почти чёрный выделенный столбец выглядит как ошибка отрисовки; оттенок
    самого цвета читается как «вот этот».
    """
    base = QColor(color)
    return QColor(
        int(base.red() * (1 - factor)),
        int(base.green() * (1 - factor)),
        int(base.blue() * (1 - factor)),
    )


# Символ нулевой ширины. Служит уникализатором подписей оси: он ничего не
# занимает на экране, но делает строки различными для Qt.
_INVISIBLE = "​"


def _thin(labels: Sequence[str], step: int) -> list[str]:
    """Оставляет каждую n-ю подпись, сохраняя все категории.

    Здесь две ловушки `QBarCategoryAxis`, и обе стоили по одному дефекту.

    Ось молча склеивает одинаковые названия: сорок пять дней с повторяющейся
    пустой подписью превращались в девять категорий, и вместе с подписями с
    графика исчезали сами столбцы. Поэтому каждая подпись обязана быть
    уникальной — даже пустая.

    Уникализировать обычными пробелами нельзя: сорок пятая подпись выходит
    шириной в пол-графика, подписи начинают перекрываться, и Qt прячет их все.
    Поэтому добавляется символ нулевой ширины — невидимый и не занимающий места.

    Само прореживание помогает только на широких слотах. Когда категорий много,
    Qt решает вопрос перекрытия по ширине слота, а не текста, и скрывает подписи
    целиком независимо от прореживания — там нужен поворот подписей.
    """
    if step <= 1:
        return [label + _INVISIBLE * index for index, label in enumerate(labels)]
    return [
        (label if index % step == 0 else "") + _INVISIBLE * (index + 1)
        for index, label in enumerate(labels)
    ]


class ChartBox(QWidget):
    """Рамка с заголовком: график, а если QtCharts нет — объяснение.

    `activated` отдаёт значение, привязанное к столбцу или сектору: страница
    решает, что с ним делать — отобрать строки или перевести календарь.
    """

    activated = Signal(object)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._payload: list[Any] = []
        self._hints: list[str] = []
        self._selected = -1

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        self.caption = QLabel(title, self)
        self.caption.setStyleSheet("font-size: 13px; font-weight: 600;")
        self._layout.addWidget(self.caption)

        self.empty = QLabel("нет данных за выбранный период", self)
        self.empty.setObjectName("Hint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.empty)

        self.view: QChartView | None = None
        if available():
            chart = QChart()
            chart.setBackgroundVisible(False)
            chart.setMargins(QMargins(0, 0, 0, 0))
            chart.legend().setVisible(False)
            chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)
            chart.setAnimationDuration(320)
            self.chart: QChart | None = chart
            view = QChartView(chart, self)
            view.setRenderHint(QPainter.RenderHint.Antialiasing)
            view.setMinimumHeight(300)
            view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            view.setStyleSheet("background: transparent; border: none;")
            view.setMouseTracking(True)
            self._layout.addWidget(view, 1)
            self.view = view
            view.setVisible(False)
        else:
            self.chart = None
            self.empty.setText(
                "Графики недоступны: в этой сборке нет модуля QtCharts.\n"
                "Цифры по-прежнему видны в плитках и таблице.")
        self.setMinimumHeight(340)

    # --- общее ----------------------------------------------------------------

    def _prepare(self, has_data: bool) -> bool:
        """Готовит область под новый график. False — рисовать нечего."""
        if self.chart is None:
            return False
        self.chart.removeAllSeries()
        for axis in list(self.chart.axes()):
            self.chart.removeAxis(axis)
        self.chart.legend().setVisible(False)
        self._payload = []
        self._hints = []
        self._selected = -1
        if self.view is not None:
            self.view.setVisible(has_data)
            self.view.setCursor(
                Qt.CursorShape.PointingHandCursor if has_data else Qt.CursorShape.ArrowCursor)
        self.empty.setVisible(not has_data)
        return has_data

    def _value_axis(self, top: float, divisor: float, unit: str) -> "QValueAxis":
        axis = QValueAxis()
        axis.setRange(0, (top / divisor) * 1.08 if top else 1.0)
        axis.setLabelsFont(QFont("Segoe UI", 8))
        axis.setLabelsColor(QColor(Palette.TEXT_MUTED))
        axis.setGridLineColor(QColor(Palette.BORDER))
        axis.setTickCount(5)
        axis.setLabelFormat("%.1f" if divisor > 1 else "%d")
        axis.setTitleText(unit)
        axis.setTitleFont(QFont("Segoe UI", 8))
        axis.setTitleBrush(QColor(Palette.TEXT_FAINT))
        return axis

    def _category_axis(
        self,
        labels: Sequence[str],
        *,
        angle: int = 0,
        visible: bool = True,
    ) -> "QBarCategoryAxis":
        """Ось категорий. `visible=False` оставляет деления без подписей.

        Подписи приходится отключать, когда категорий много. `QBarCategoryAxis`
        делит доступную ширину на их число и обрезает текст под получившийся
        слот: на сорока пяти днях от «30.06» остаётся одна точка. Прореживание
        не помогает — ось склеивает одинаковые названия, а поворот подписей она
        обрезает так же. Точки вместо дат хуже, чем честно пустая ось: дату
        показывает подсказка при наведении, а отрезок — заголовок графика.
        """
        axis = QBarCategoryAxis()
        axis.append(list(labels))
        axis.setLabelsFont(QFont("Segoe UI", 8))
        axis.setLabelsColor(QColor(Palette.TEXT_MUTED))
        axis.setGridLineVisible(False)
        axis.setLabelsVisible(visible)
        if angle:
            axis.setLabelsAngle(angle)
        return axis

    def _connect(self, series: Any, sets: Sequence["QBarSet"]) -> None:
        """Подсказка при наведении, выделение и переход по щелчку.

        Выделение живёт на наборе столбцов, а не на серии: у `QBarSeries` нет
        своего переключателя, зато `QBarSet` умеет подсвечивать отдельный столбец.
        """
        series.hovered.connect(lambda status, index, _set=None: self._hover(status, index))
        series.clicked.connect(lambda index, _set=None: self._click(index))
        self._sets = list(sets)

    def _hover(self, status: bool, index: int) -> None:
        if not status or not 0 <= index < len(self._hints):
            QToolTip.hideText()
            self._select(-1)
            return
        QToolTip.showText(QCursor.pos(), self._hints[index], self.view)
        self._select(index)

    def _select(self, index: int) -> None:
        if index == self._selected:
            return
        self._selected = index
        for bars in getattr(self, "_sets", []):
            bars.deselectAllBars()
            if index >= 0:
                bars.selectBar(index)

    def _click(self, index: int) -> None:
        if 0 <= index < len(self._payload):
            self.activated.emit(self._payload[index])

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        QToolTip.hideText()
        self._select(-1)
        super().leaveEvent(event)

    # --- виды графиков --------------------------------------------------------

    def show_bars(
        self,
        labels: Sequence[str],
        values: Sequence[float],
        *,
        color: str = Palette.PRIMARY,
        step: int = 1,
        payload: Sequence[Any] | None = None,
        hints: Sequence[str] | None = None,
        angle: int = 0,
        labels_visible: bool = True,
    ) -> None:
        """Столбцы по категориям: расходы по месяцам."""
        if not self._prepare(bool(values) and any(values)):
            return
        divisor, unit = _scale(max(values))
        bars = QBarSet("")
        bars.append([value / divisor for value in values])
        bars.setColor(QColor(color))
        bars.setBorderColor(QColor(color))
        bars.setSelectedColor(_darken(color))
        series = QBarSeries()
        series.append(bars)
        series.setBarWidth(0.82)
        self.chart.addSeries(series)

        axis_x = self._category_axis(
            _thin(labels, step), angle=angle, visible=labels_visible)
        self.chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)
        axis_y = self._value_axis(max(values), divisor, unit)
        self.chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

        self._payload = list(payload) if payload is not None else list(labels)
        self._hints = list(hints) if hints is not None else [
            f"{label}\n{money(value)} ₽" for label, value in zip(labels, values)]
        self._connect(series, [bars])

    def show_split_bars(
        self,
        labels: Sequence[str],
        paid: Sequence[float],
        planned: Sequence[float],
        *,
        color: str = Palette.PRIMARY,
        step: int = 1,
        angle: int = 0,
        labels_visible: bool = True,
        payload: Sequence[Any] | None = None,
        hints: Sequence[str] | None = None,
    ) -> None:
        """Две серии в одном столбце: оплачено и предстоит.

        Нужно там, где отрезок захватывает и прошлое, и будущее: иначе план
        неотличим от факта, а на дашборде это разные вещи.
        """
        totals = [a + b for a, b in zip(paid, planned)]
        if not self._prepare(bool(totals) and any(totals)):
            return
        divisor, unit = _scale(max(totals))
        done = QBarSet("Оплачено")
        done.append([value / divisor for value in paid])
        done.setColor(QColor(color))
        done.setBorderColor(QColor(color))
        done.setSelectedColor(_darken(color))
        ahead = QBarSet("Предстоит")
        ahead.append([value / divisor for value in planned])
        ahead.setColor(_lighten(color))
        ahead.setBorderColor(_lighten(color))
        ahead.setSelectedColor(_darken(color, 0.12))

        series = QBarSeries()
        series.append(done)
        series.append(ahead)
        series.setBarWidth(0.9)
        self.chart.addSeries(series)

        axis_x = self._category_axis(
            _thin(labels, step), angle=angle, visible=labels_visible)
        self.chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)
        axis_y = self._value_axis(max(totals), divisor, unit)
        self.chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

        legend = self.chart.legend()
        legend.setVisible(True)
        legend.setAlignment(Qt.AlignmentFlag.AlignBottom)
        legend.setFont(QFont("Segoe UI", 8))
        legend.setLabelColor(QColor(Palette.TEXT_MUTED))
        legend.setMarkerShape(legend.MarkerShape.MarkerShapeCircle)

        self._payload = list(payload) if payload is not None else list(labels)
        self._hints = list(hints) if hints is not None else [
            f"{label}\n{money(total)} ₽" for label, total in zip(labels, totals)]
        self._connect(series, [done, ahead])

    def show_horizontal(
        self,
        labels: Sequence[str],
        values: Sequence[float],
        *,
        color: str = Palette.PRIMARY,
        payload: Sequence[Any] | None = None,
        hints: Sequence[str] | None = None,
    ) -> None:
        """Горизонтальные полосы: рейтинг поставщиков — длинные названия читаемы.

        Названия ставятся вдоль полос подписями самих значений, а не на ось:
        левая ось у QtCharts получает мало места и режет всё длиннее двадцати
        знаков в «...», что и делало график бесполезным.
        """
        if not self._prepare(bool(values) and any(values)):
            return
        divisor, unit = _scale(max(values))
        # Категории горизонтальной диаграммы идут снизу вверх.
        order = list(reversed(range(len(labels))))
        bars = QBarSet("")
        bars.append([values[index] / divisor for index in order])
        bars.setColor(QColor(color))
        bars.setBorderColor(QColor(color))
        bars.setSelectedColor(_darken(color))
        bars.setLabelColor(QColor(Palette.TEXT))
        bars.setLabelFont(QFont("Segoe UI", 8))
        series = QHorizontalBarSeries()
        series.append(bars)
        series.setBarWidth(0.72)
        series.setLabelsVisible(True)
        series.setLabelsPosition(
            QHorizontalBarSeries.LabelsPosition.LabelsOutsideEnd)
        # Без ограничения точности подпись печатает всё, что есть после
        # запятой: «95.4672» вместо «95,5». Три значащие цифры дают и
        # «218», и «63,5» — читаемо в обоих случаях.
        series.setLabelsPrecision(3)
        self.chart.addSeries(series)

        axis_y = self._category_axis([_short(labels[index]) for index in order])
        self.chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)
        axis_x = self._value_axis(max(values) * 1.16, divisor, unit)
        self.chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        source_payload = list(payload) if payload is not None else list(labels)
        source_hints = list(hints) if hints is not None else [
            f"{label}\n{money(value)} ₽" for label, value in zip(labels, values)]
        self._payload = [source_payload[index] for index in order]
        self._hints = [source_hints[index] for index in order]
        self._connect(series, [bars])

    def show_pie(
        self,
        labels: Sequence[str],
        values: Sequence[float],
        *,
        payload: Sequence[Any] | None = None,
    ) -> None:
        """Круговая диаграмма с легендой: распределение бюджета."""
        pairs = [(label, value) for label, value in zip(labels, values) if value > 0]
        if not self._prepare(bool(pairs)):
            return
        source_payload = list(payload) if payload is not None else list(labels)
        keys = [source_payload[index] for index, (_, value) in enumerate(zip(labels, values))
                if value > 0] if payload is not None else [label for label, _ in pairs]
        if len(pairs) > MAX_SLICES:
            head, tail = pairs[:MAX_SLICES - 1], pairs[MAX_SLICES - 1:]
            pairs = head + [(f"Прочие ({len(tail)})", sum(value for _, value in tail))]
            keys = keys[:MAX_SLICES - 1] + [None]

        total = sum(value for _, value in pairs) or 1.0
        series = QPieSeries()
        series.setHoleSize(0.44)
        series.setPieSize(0.82)
        for index, (label, value) in enumerate(pairs):
            piece = series.append(f"{_short(label, 22)} · {value / total * 100:.0f} %", value)
            colour = QColor(SERIES_COLORS[index % len(SERIES_COLORS)])
            piece.setColor(colour)
            piece.setBorderColor(QColor(Palette.SURFACE))
            piece.setBorderWidth(2)
            piece.setLabelVisible(False)
        self.chart.addSeries(series)

        legend = self.chart.legend()
        legend.setVisible(True)
        legend.setAlignment(Qt.AlignmentFlag.AlignRight)
        legend.setFont(QFont("Segoe UI", 8))
        legend.setLabelColor(QColor(Palette.TEXT_MUTED))
        legend.setMarkerShape(legend.MarkerShape.MarkerShapeCircle)

        self._payload = keys
        self._hints = [f"{label}\n{money(value)} ₽ · {value / total * 100:.1f} %"
                       for label, value in pairs]
        series.hovered.connect(self._pie_hover)
        series.clicked.connect(self._pie_click)
        self._slices = list(series.slices())

    def _pie_hover(self, piece: Any, status: bool) -> None:
        """Сектор приподнимается и показывает точную сумму."""
        pieces = getattr(self, "_slices", [])
        piece.setExploded(status)
        piece.setExplodeDistanceFactor(0.06)
        if not status:
            QToolTip.hideText()
            return
        try:
            index = pieces.index(piece)
        except ValueError:
            return
        if 0 <= index < len(self._hints):
            QToolTip.showText(QCursor.pos(), self._hints[index], self.view)

    def _pie_click(self, piece: Any) -> None:
        pieces = getattr(self, "_slices", [])
        try:
            index = pieces.index(piece)
        except ValueError:
            return
        if 0 <= index < len(self._payload) and self._payload[index] is not None:
            self.activated.emit(self._payload[index])


def _short(text: str, limit: int = 26) -> str:
    """Обрезка по словам: «ЗЕЛИНСКИЙ И РОЗЕН ПАРФЮМЕРИЯ ООО» → «ЗЕЛИНСКИЙ И РОЗЕН…»."""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0]
    return (cut or clean[:limit]) + "…"
