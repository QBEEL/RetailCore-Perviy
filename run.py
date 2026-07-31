"""Точка входа для сборки и запуска.

Отдельный файл нужен потому, что `app/main.py` использует относительные импорты:
запущенный как скрипт, он не знает о своём пакете.

Диагностика собранного .exe (окно не открывается, отчёт пишется в файл):

    RetailCore.exe --selftest каталог.xlsx цель.xlsx [отчёт.txt]
"""
import sys
from pathlib import Path


def selftest(argv: list[str]) -> int:
    """Прогоняет загрузку и сопоставление без интерфейса и пишет отчёт."""
    from app.core.matching import Matcher, summarize
    from app.core.workbook import load_sheet
    from app.ui.resources import asset_dir

    report: list[str] = []
    ok = True

    for name in ("app.ico", "check.svg", "chevron-up.svg", "chevron-down.svg"):
        exists = (asset_dir() / name).exists()
        ok &= exists
        report.append(f"ресурс {name}: {'найден' if exists else 'ОТСУТСТВУЕТ'}")

    ok &= _check_icons(report)

    catalog = None
    if len(argv) >= 2:
        try:
            catalog = load_sheet(argv[0])
            target = load_sheet(argv[1])
            results = Matcher(catalog).match_all(target.records)
            counts = summarize(results)
            report.append(f"каталог: {len(catalog.records)} строк")
            report.append(f"цель: {len(target.records)} строк")
            report.append("итог: " + ", ".join(f"{k.title}={v}" for k, v in counts.items() if v))
        except Exception as error:  # noqa: BLE001 — отчёт важнее аккуратного типа
            ok = False
            report.append(f"сопоставление: ОШИБКА — {error}")

    # История данных опирается на sqlite3 из стандартной библиотеки: в собранном
    # exe его может не оказаться, и узнать об этом лучше здесь, а не у клиента.
    if catalog is not None:
        ok &= _check_snapshots(catalog, report)

    ok &= _check_charts(report)
    ok &= _check_payments(report)

    report.append("РЕЗУЛЬТАТ: " + ("успешно" if ok else "есть ошибки"))
    text = "\n".join(report)

    destination = Path(argv[2]) if len(argv) >= 3 else Path("selftest.log")
    destination.write_text(text, encoding="utf-8")
    # У собранного приложения консоли нет: вывод в неё — необязательная услуга,
    # и падать или зависать из-за неё отчёт не должен.
    if sys.stdout is not None:
        try:
            print(text, flush=True)
        except (OSError, ValueError):
            pass
    return 0 if ok else 1


def _check_icons(report: list[str]) -> bool:
    """Рисует иконку по-настоящему: шрифты подгружаются только в этот момент.

    Одного `import qtawesome` мало — файлы .ttf открываются лениво, и сборка
    без них проходила бы проверку, а падала уже у пользователя при первом
    построении окна.
    """
    try:
        import qtawesome  # noqa: F401
    except ImportError as error:
        report.append(f"qtawesome: ОШИБКА — {error}")
        return False

    try:
        from PySide6.QtWidgets import QApplication

        from app.ui import icons

        application = QApplication.instance() or QApplication([])
        drawn = [name for name in ("match", "history", "update", "settings")
                 if not icons.icon(name).pixmap(16, 16).isNull()]
        del application
    except Exception as error:  # noqa: BLE001 — отчёт важнее аккуратного типа
        report.append(f"иконки: ОШИБКА — {error}")
        return False

    if len(drawn) < 4:
        report.append(f"иконки: ОШИБКА — отрисовано {len(drawn)} из 4 (нет шрифтов?)")
        return False
    report.append("иконки: шрифты загружены, 4 из 4 отрисованы")
    return True


def _check_snapshots(catalog, report: list[str]) -> bool:
    """Создаёт снимок во временной базе и сверяет прочитанное с исходником."""
    import tempfile

    try:
        from app.core import appdata
        from app.core.snapshots import store

        with tempfile.TemporaryDirectory() as directory:
            # Диагностика не должна оставлять следов в истории и журнале
            # пользователя: и база, и snapshot.log уходят во временную папку.
            original_dir, appdata.data_dir = appdata.data_dir, lambda: directory
            try:
                return _snapshot_roundtrip(store, catalog, directory, report)
            finally:
                appdata.data_dir = original_dir
    except Exception as error:  # noqa: BLE001 — отчёт важнее аккуратного типа
        report.append(f"история данных: ОШИБКА — {error}")
        return False


def _snapshot_roundtrip(store, catalog, directory: str, report: list[str]) -> bool:
    database = str(Path(directory) / "selftest.db")
    snapshot = store.create(catalog, database)
    if snapshot is None:
        report.append("история данных: ОШИБКА — снимок не создан")
        return False

    saved = store.products(snapshot.id, database)
    if len(saved) != len(catalog.records):
        report.append(f"история данных: ОШИБКА — сохранено {len(saved)}"
                      f" из {len(catalog.records)}")
        return False

    report.append(f"история данных: снимок из {len(saved)} товаров сохранён и прочитан")
    return True


def _check_charts(report: list[str]) -> bool:
    """Графики оплат рисует QtCharts, и в сборке его легко потерять.

    Модуль входит в PySide6, но подключается отложенно, а его биндинг
    `QtOpenGL` из сборки исключён. Если QtCharts не поднимется, страница оплат
    молча покажет цифры без графиков — такое лучше увидеть здесь.
    """
    try:
        from PySide6.QtCharts import QChart  # noqa: F401

        report.append("графики: QtCharts доступен")
        return True
    except ImportError as error:
        report.append(f"графики: ОШИБКА — QtCharts не подключился ({error})")
        return False


def _check_payments(report: list[str]) -> bool:
    """Создаёт базу оплат во временной папке и проходит её насквозь.

    Проверяется то, что ломается при сборке: миграция схемы, запись, чтение и
    разбор выгрузки. Следов в данных пользователя не остаётся.
    """
    try:
        import tempfile
        from datetime import date

        from app.core import appdata
        from app.core.payments import Budget, Payment, PaymentStatus, analytics, store

        with tempfile.TemporaryDirectory() as directory:
            original_dir, appdata.data_dir = appdata.data_dir, lambda: directory
            try:
                database = str(Path(directory) / "payments.db")
                store.save_payment(Payment(
                    amount=1000.0, recipient="Проверка ООО",
                    pay_date=date(2026, 1, 15), status=PaymentStatus.PAID), database)
                rows = store.list_payments(None, database)
                stats = analytics.overview(rows)
                if len(rows) != 1 or stats.total != 1000.0:
                    report.append("оплаты: ОШИБКА — запись не прочиталась обратно")
                    return False
                store.save_budget(Budget(year=2026, month=1, amount=5000.0), database)
                if not store.budgets(database):
                    report.append("оплаты: ОШИБКА — бюджет не сохранился")
                    return False
            finally:
                appdata.data_dir = original_dir
        report.append("оплаты: база создана, платёж и бюджет сохранены и прочитаны")
        return True
    except Exception as error:  # noqa: BLE001 — отчёт важнее аккуратного типа
        report.append(f"оплаты: ОШИБКА — {error}")
        return False


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        index = sys.argv.index("--selftest")
        sys.exit(selftest(sys.argv[index + 1:]))

    from app.main import main

    sys.exit(main())
