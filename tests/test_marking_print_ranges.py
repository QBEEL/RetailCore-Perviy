from app.core.marking import issue


def test_disjoint_ranges_survive_reload_without_skipping_gaps():
    batch = issue.Batch(id="ranges", codes=[str(i) for i in range(100)])
    issue.mark_printed_range(batch, 50, 60)
    restored = issue.load(batch.id)
    assert restored.printed == 0
    assert restored.printed_count == 10
    assert restored.next_print_count == 50
    assert restored.left == 90
    issue.mark_printed_range(restored, 0, 50)
    assert restored.printed == 60
    assert restored.next_print_count == 40
    issue.mark_printed_range(restored, 45, 65)
    assert restored.printed_count == 65
    assert issue.load(batch.id).printed_count == 65


def test_legacy_prefix_is_preserved_and_ranges_are_bounded():
    batch = issue.Batch(codes=[str(i) for i in range(10)], printed=3)
    issue.mark_printed_range(batch, 7, 100)
    assert batch.printed == 3
    assert batch.printed_count == 6
    assert batch.next_print_count == 4
    issue.mark_printed_range(batch, 3, 7)
    assert batch.printed == 10
    assert batch.left == 0
