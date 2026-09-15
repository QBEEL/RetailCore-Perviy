"""Разбор кодов маркировки DataMatrix."""
from __future__ import annotations

import pytest

from app.core.marking import codes
from app.core.marking.codes import GS, for_request, parse, parse_many, split_lines

# Настоящий по строению код парфюмерии: товар, серийный номер, ключ и значение
# проверки. GTIN подобран с верной контрольной цифрой.
PERFUME = f"010460123456789321Abc123XyZ{GS}91EE10{GS}92signature=="

# Настоящий код с пачки духов, снятый сканером: разделителей GS в нём нет
# вовсе — сканер их не выдал.
SCANNED = ("0103770014214423215Xtj9=4WZnWPI91EE1192"
           "9xCxykVs/R8ooelKq+ZgCjs63ysRsnNs2MEKJDa98zs=")
SCANNED_KI = "0103770014214423215Xtj9=4WZnWPI"


def test_разбирает_поля_кода():
    code = parse(PERFUME)
    assert code.valid, code.problems
    assert code.gtin == "04601234567893"
    assert code.serial == "Abc123XyZ"
    assert code.fields["91"] == "EE10"
    assert code.fields["92"] == "signature=="


def test_gtin_постоянной_длины_читается_по_счёту():
    """Между 01 и 21 разделителя нет — поле 01 всегда ровно 14 знаков."""
    code = parse(PERFUME)
    assert code.gtin == "04601234567893"
    assert not code.gtin.startswith("21")


def test_серийный_номер_кончается_разделителем():
    code = parse(f"010460123456789321AB{GS}91XXXX")
    assert code.serial == "AB"
    assert code.fields["91"] == "XXXX"


def test_последнее_поле_без_разделителя():
    code = parse("010460123456789321Serial999")
    assert code.serial == "Serial999"


@pytest.mark.parametrize("prefix", ["]d2", "]D2", ""])
def test_признак_символики_отбрасывается(prefix):
    assert parse(prefix + PERFUME).gtin == "04601234567893"


@pytest.mark.parametrize("alias", ["<GS>", "&#29;", "␝", "{GS}"])
def test_видимая_замена_разделителя_понимается(alias):
    """Из письма и из таблицы GS приходит подставленным текстом."""
    raw = f"010460123456789321AB{alias}91XXXX"
    code = parse(raw)
    assert code.valid, code.problems
    assert code.serial == "AB"
    assert code.fields["91"] == "XXXX"


def test_неверная_контрольная_цифра_замечается():
    code = parse(f"010460123456789221AB{GS}91XXXX")
    assert not code.valid
    assert any("контрольная" in problem for problem in code.problems)


def test_код_без_серийного_номера_неполон():
    code = parse("0104601234567893")
    assert not code.valid
    assert any("серийн" in problem for problem in code.problems)


def test_мусор_не_роняет_разбор():
    code = parse("совсем не код")
    assert not code.valid
    assert code.problems


def test_пустая_строка():
    assert not parse("").valid


def test_контрольная_цифра_gtin():
    assert codes.gtin_valid("04601234567893")
    assert not codes.gtin_valid("04601234567895")
    assert not codes.gtin_valid("0460123456789")   # короткий
    assert not codes.gtin_valid("0460123456789X")  # не цифры


def test_ean13_для_штучного_товара():
    assert parse(PERFUME).ean13 == "4601234567893"


def test_ean13_пуст_для_упаковки():
    """Первая цифра не ноль — это уровень упаковки, а не штука."""
    code = parse(f"011460123456778221AB{GS}91XXXX")
    assert code.ean13 == ""


def test_поле_дважды_замечается():
    code = parse(f"010460123456789321AB{GS}21CD")
    assert any("дважды" in problem for problem in code.problems)


def test_длинный_идентификатор_читается():
    code = parse(f"010460123456789321AB{GS}800512345{GS}91XXXX")
    assert code.fields["8005"] == "12345"
    assert code.fields["91"] == "XXXX"


# --- криптохвост без разделителей -------------------------------------------------

def test_код_со_сканера_разбирается_без_разделителей():
    """Сканер не выдал ни одного GS — конец серийного номера ищется по хвосту.

    Без этого серийный номер получался длиной в полсотни знаков: он «съедал»
    и ключ проверки, и подпись.
    """
    code = parse(SCANNED)

    assert code.valid, code.problems
    assert code.gtin == "03770014214423"
    assert code.serial == "5Xtj9=4WZnWPI"
    assert code.fields["91"] == "EE11"
    assert code.fields["92"] == "9xCxykVs/R8ooelKq+ZgCjs63ysRsnNs2MEKJDa98zs="


def test_в_запрос_уходит_код_идентификации():
    """С ключом и значением проверки система отвечает отказом, без них — сведениями."""
    assert for_request(SCANNED) == SCANNED_KI
    assert parse(SCANNED).ki == SCANNED_KI


def test_разделители_ничего_не_меняют():
    """Тот же код от правильно настроенного сканера даёт то же самое."""
    with_separators = f"{SCANNED_KI}{GS}91EE11{GS}929xCxykVs/R8ooelKq+ZgCjs63ysRsnNs2MEKJDa98zs="

    assert for_request(with_separators) == SCANNED_KI
    assert parse(with_separators).serial == parse(SCANNED).serial


def test_код_идентификации_остаётся_собой():
    """Усечённый руками код уже готов к запросу — трогать его незачем."""
    assert for_request(SCANNED_KI) == SCANNED_KI


def test_девяносто_один_внутри_серийного_номера_не_режет_код():
    """Сочетание «91» встречается и в серийном номере — резать по нему нельзя."""
    code = parse(f"010460123456789321AB91CD91EE1192{'x' * 44}")

    assert code.serial == "AB91CD"
    assert code.fields["91"] == "EE11"


def test_неразобранный_код_уходит_как_есть():
    """Решать за систему, что она его не поймёт, нельзя: ответ и есть результат."""
    assert for_request("совсем не код") == "совсем не код"


# --- пачка кодов ----------------------------------------------------------------

def test_повторы_в_пачке_находятся():
    """Один серийный номер дважды — это либо пересчёт, либо копия кода."""
    batch = parse_many([PERFUME, PERFUME, "010460123456789321Другой"])
    assert len(batch.duplicates) == 1
    assert len(batch.duplicates[("04601234567893", "Abc123XyZ")]) == 2


def test_разные_серийные_повтором_не_считаются():
    batch = parse_many(["010460123456789321AA", "010460123456789321BB"])
    assert not batch.duplicates


def test_группировка_по_товару():
    batch = parse_many([
        "010460123456789321AA",
        "010460123456789321BB",
        "011234567890128621CC",
    ])
    assert len(batch.by_gtin) == 2
    assert len(batch.by_gtin["04601234567893"]) == 2


def test_испорченные_отделяются_от_целых():
    batch = parse_many([PERFUME, "мусор", ""])
    assert len(batch.valid) == 1
    assert len(batch.broken) == 1  # пустая строка отброшена до разбора


def test_сводка_читается():
    batch = parse_many([PERFUME, PERFUME, "мусор"])
    summary = batch.summary
    assert "кодов: 3" in summary
    assert "повторов: 1" in summary
    assert "не разобрано: 1" in summary


# --- деление вставленного текста ------------------------------------------------

def test_деление_по_строкам():
    assert split_lines("AAA\nBBB\r\nCCC") == ["AAA", "BBB", "CCC"]


def test_пустые_строки_отбрасываются():
    assert split_lines("AAA\n\n\nBBB\n  \n") == ["AAA", "BBB"]


def test_по_пробелам_не_делим():
    """Внутри кода пробел встречается, и деление по нему разорвало бы код."""
    assert split_lines("AAA BBB") == ["AAA BBB"]
