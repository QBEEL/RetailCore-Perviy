"""Первичная разметка закреплений по истории оплат.

Готового справочника «поставщик → менеджер» нет, а размечать полторы тысячи
поставщиков руками никто не станет. Поэтому черновики заводятся по истории:
менеджер получает заявку на поставщика, если на него приходится заметная доля
оплат за последний год.

Черновик — это предложение, а не назначение. Менеджер видит свой список во
вкладке, лишнее отзывает сам, недостающее добавляет; администратор фиксирует.

Считаем по сумме, а не по количеству: количество перекошено мелкими
регулярными платежами, и поставщик с сотней доставок по тысяче рублей
перевесил бы поставщика с одной закупкой на миллион.

Порог низкий намеренно. По одному поставщику черновик получат несколько
человек, и это правильно: лишнее отзывается за минуту, а пропущенного
поставщика в списке из полутора тысяч человек не заметит вовсе. Ошибка в
сторону лишнего дешевле ошибки в сторону недостающего.

Запуск на сервере:

    docker compose exec -T api python - --apply < tools/bootstrap_assignments.py

Без `--apply` ничего не пишет, только показывает, что получится.
"""
from __future__ import annotations

import sys

from app import db

# Доля суммы оплат поставщику за период, начиная с которой менеджер считается
# кандидатом в ведущие. Согласовано: 20 % за 12 месяцев.
SHARE = 0.20
MONTHS = 12

# Кандидатом может быть только категорийный менеджер, а признак этого —
# заданное направление в `user_direction`. Отдельного поля «должность» не
# заводим: направление и так есть у всех, кто ведёт поставщиков, и второй
# признак того же самого рано или поздно разошёлся бы с первым.
#
# Проверка на живых данных показала, зачем это нужно. Больше всего оплат в
# отделе проходит через офис-менеджера и бухгалтера — они платят по заявкам,
# но поставщиков не ведут. По истории оплат им доставалось 40 % всех
# черновиков, и администратору пришлось бы отклонять их поштучно.
#
# Оплаты, где ответственный не сведён к учётной записи, в расчёт не идут: за
# ними нет человека, которому можно что-то предложить. Такие имена — отдельная
# работа администратора, и скрипт показывает их в конце.
_CANDIDATES = """
WITH recent AS (
    SELECT p.recipient_key, p.amount, ur.user_id
    FROM payment p
    JOIN LATERAL (
        SELECT ur.user_id FROM user_responsible ur
        JOIN app_user u ON u.id = ur.user_id
        WHERE ur.responsible = p.responsible AND u.is_active
          AND EXISTS (SELECT 1 FROM user_direction ud WHERE ud.user_id = u.id)
        ORDER BY ur.user_id LIMIT 1
    ) ur ON TRUE
    WHERE p.recipient <> '' AND p.responsible <> ''
      AND p.pay_date >= CURRENT_DATE - interval '%s months'
), by_supplier AS (
    -- Доля считается от оплат категорийных менеджеров, а не от всех вообще.
    -- Иначе поставщик, у которого девять десятых платежей провёл бухгалтер,
    -- не дотянул бы до порога ни у кого, хотя ведёт его вполне конкретный
    -- человек — просто платит за него не он.
    SELECT recipient_key, SUM(amount) AS total FROM recent GROUP BY recipient_key
), by_manager AS (
    SELECT recipient_key, user_id, SUM(amount) AS paid FROM recent
    GROUP BY recipient_key, user_id
)
SELECT m.recipient_key, m.user_id, m.paid, s.total,
       m.paid / NULLIF(s.total, 0) AS share
FROM by_manager m JOIN by_supplier s ON s.recipient_key = m.recipient_key
WHERE s.total > 0 AND m.paid / s.total >= %s
  AND NOT EXISTS (SELECT 1 FROM supplier_assignment a
                  WHERE a.recipient_key = m.recipient_key AND a.user_id = m.user_id)
ORDER BY m.recipient_key, m.paid DESC
"""


def main(apply: bool) -> int:
    db.pool.open(wait=True, timeout=30)
    rows = db.fetch_all(_CANDIDATES % (MONTHS, SHARE))
    if not rows:
        print("Кандидатов нет: либо всё уже закреплено, либо оплат за период "
              "не нашлось.")
        return 0

    names = {row["id"]: row["full_name"] for row in db.fetch_all(
        "SELECT id, full_name FROM app_user")}
    per_manager: dict[int, int] = {}
    suppliers: set[str] = set()
    for row in rows:
        per_manager[row["user_id"]] = per_manager.get(row["user_id"], 0) + 1
        suppliers.add(row["recipient_key"])

    print(f"Порог: {SHARE:.0%} суммы за {MONTHS} мес.")
    print(f"Черновиков: {len(rows)} по {len(suppliers)} поставщикам\n")
    for user_id, count in sorted(per_manager.items(), key=lambda p: -p[1]):
        print(f"  {names.get(user_id, '?'):<28} {count}")

    # Поставщики, попавшие сразу к нескольким: это не сбой, а обычное дело —
    # одного поставщика ведут по разным брендам. Но цифру полезно видеть.
    shared = len(rows) - len(suppliers)
    if shared:
        print(f"\nу {shared} закреплений поставщик общий с коллегой")

    if not apply:
        print("\nЧерновики не записаны. Повторите с --apply.")
        return 0

    with db.cursor() as handle:
        handle.execute(
            "INSERT INTO supplier_assignment (recipient_key, user_id, claimed_by)"
            " SELECT k, u, NULL FROM unnest(%s::text[], %s::bigint[]) AS t(k, u)"
            " ON CONFLICT (recipient_key, user_id) DO NOTHING"
            " RETURNING recipient_key",
            ([row["recipient_key"] for row in rows],
             [row["user_id"] for row in rows]))
        added = [r["recipient_key"] for r in handle.fetchall()]

        # Автор — не человек, а расчёт. В журнале это должно быть видно:
        # иначе менеджер, увидевший чужую заявку от своего имени, решит, что
        # её кто-то подделал.
        handle.execute(
            "INSERT INTO supplier_assignment_log"
            " (recipient_key, user_id, action, payload, actor_id)"
            " SELECT k, u, 'bootstrap', %s::jsonb, NULL"
            " FROM unnest(%s::text[], %s::bigint[]) AS t(k, u)",
            (f'{{"share": {SHARE}, "months": {MONTHS}}}',
             [row["recipient_key"] for row in rows],
             [row["user_id"] for row in rows]))

        _sync_directions(handle, sorted(set(added)))

    print(f"\nЗаписано черновиков: {len(added)}")
    _report_unmapped()
    return 0


def _sync_directions(handle, keys: list[str]) -> None:
    """Направления поставщиков по направлениям заявленных менеджеров."""
    if not keys:
        return
    handle.execute(
        "INSERT INTO supplier_direction (recipient_key, direction_id, source)"
        " SELECT DISTINCT a.recipient_key, ud.direction_id, 'auto'"
        " FROM supplier_assignment a"
        " JOIN user_direction ud ON ud.user_id = a.user_id"
        " WHERE a.recipient_key = ANY(%s)"
        "   AND NOT EXISTS (SELECT 1 FROM supplier_direction m"
        "                   WHERE m.recipient_key = a.recipient_key"
        "                     AND m.source = 'manual')"
        " ON CONFLICT (recipient_key, direction_id) DO NOTHING",
        (keys,))


def _report_unmapped() -> None:
    """Имена из 1С, за которыми нет учётной записи.

    Их оплаты в расчёт не попали, а поставщики остались без кандидатов. Пока
    имя не сведено к учётке, эти поставщики не появятся ни у кого в «моих».
    """
    rows = db.fetch_all(
        "SELECT p.responsible, COUNT(*) AS n,"
        "       COUNT(DISTINCT p.recipient_key) AS suppliers"
        " FROM payment p"
        " WHERE p.responsible <> '' AND p.recipient <> ''"
        "   AND NOT EXISTS (SELECT 1 FROM user_responsible ur"
        "                   WHERE ur.responsible = p.responsible)"
        " GROUP BY p.responsible ORDER BY n DESC LIMIT 10")
    if not rows:
        return
    print("\nБез учётной записи (их поставщики не попадут ни к кому):")
    for row in rows:
        print(f"  {row['responsible']:<44} оплат {row['n']:>5}"
              f"   поставщиков {row['suppliers']}")


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
