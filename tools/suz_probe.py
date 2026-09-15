"""Выясняет, какие методы СУЗ существуют, — вашим сохранённым токеном.

Запуск: двойной клик по «Проверка СУЗ.bat» в папке программы. Из командной
строки — `.venv\\Scripts\\python.exe tools\\suz_probe.py`.

Ответ пишется и на экран, и в файл рядом с программой: пересылать файл проще,
чем выделять текст в консоли, а окно консоли к тому же закрывают не глядя.

Зачем. Без токена коды ответов СУЗ бесполезны: проверка учётных данных идёт
раньше маршрутизации, и несуществующий путь отвечает тем же 1110, что и
настоящий. С действующим токеном запрос доходит до маршрутизации, и ответ
наконец различает: 200 — метод есть и работает, 405 — такого пути нет, 400 с
разбором — путь есть, а данные в запросе не те. Отсюда и этот сценарий: один
прогон вместо десяти догадок в коде.

Что он делает. Только читает и только тем, что заведомо ничего не расходует.
Получения кодов (`/codes`) здесь нет и не будет: коды выдаются из буфера
безвозвратно, и «просто посмотреть» на них нельзя. Ни одного POST — создание
заказа не проверяют наугад.

Токен нигде не печатается: в выводе только пути и ответы. Ответы приводятся
как есть, чтобы по ним можно было поправить разбор.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import suz, transport                      # noqa: E402
from app.core.marking.models import Contour                      # noqa: E402

# Выдуманные значения для методов, которым нужен номер заказа или товар. Ответ
# «заказ не найден» — такой же полезный результат, как и любой другой: он
# означает, что метод есть и разбирает запрос.
FAKE_ORDER = "00000000-0000-0000-0000-000000000000"
FAKE_GTIN = "04600000000000"

# Пути-кандидаты. Только чтение. `/codes` отсутствует намеренно.
CANDIDATES: tuple[tuple[str, dict], ...] = (
    ("/ping", {}),
    ("/orders", {}),
    ("/order/list", {}),
    ("/orders/list", {}),
    ("/order", {"orderId": FAKE_ORDER}),
    ("/order/status", {"orderId": FAKE_ORDER}),
    ("/orders/status", {"orderId": FAKE_ORDER}),
    ("/buffer/status", {"orderId": FAKE_ORDER, "gtin": FAKE_GTIN}),
    ("/report/info", {}),
    ("/version", {}),
)


def probe(credentials: suz.Credentials, contour: Contour,
          path: str, params: dict) -> str:
    address = transport.url("suz", path, contour,
                            {"omsId": credentials.oms_id, **params})
    request = urllib.request.Request(
        address, headers={suz.TOKEN_HEADER: credentials.token,
                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return f"200 {_short(answer.read())}"
    except urllib.error.HTTPError as error:
        return f"{error.code} {_short(error.read())}"
    except OSError as error:
        return f"нет ответа: {error}"


def _short(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False)
    except ValueError:
        pass
    return text[:400] if text else "(пусто)"


REPORT = Path(__file__).resolve().parents[1] / "Ответ СУЗ.txt"


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        """Показывает и запоминает: файл потом пересылают, экран — закрывают."""
        print(text)
        lines.append(text)

    found = False
    for contour in (Contour.SANDBOX, Contour.PRODUCTION):
        credentials = suz.load(contour)
        if not credentials.filled:
            continue
        found = True
        suz.apply(credentials, contour)
        say(f"\n=== {contour.title} · ОМС {credentials.oms_id} ===")
        for path, params in CANDIDATES:
            say(f"\nGET {path}")
            say(f"  {probe(credentials, contour, path, params)}")
    if not found:
        print("Реквизиты СУЗ не сохранены. Настройте соединение на вкладке "
              "«Маркировка» и запустите заново.")
        return 1

    say("\nЭтот файл целиком можно отдать разработчику: токена в нём нет.")
    try:
        REPORT.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nОтвет сохранён: {REPORT}")
    except OSError as error:
        # Не беда: на экране всё то же самое. Молчать о неудаче всё же нельзя —
        # иначе человек пойдёт искать файл, которого нет.
        print(f"\nСохранить файл не удалось ({error}). Скопируйте вывод с экрана.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
