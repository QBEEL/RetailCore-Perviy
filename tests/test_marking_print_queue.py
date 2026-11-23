import pytest
from app.core.marking import issue, print_queue


def test_queue_resumes_each_product_and_saves_correct_ranges():
    a = issue.Batch(id="a", codes=[f"a{i}" for i in range(6)], printed=1)
    b = issue.Batch(id="b", codes=[f"b{i}" for i in range(4)])
    issue.mark_printed_range(a, 3, 5)
    queue = print_queue.pending([a, b])
    assert [e.code for e in queue] == ["a1", "a2", "a5", "b0", "b1", "b2", "b3"]
    print_queue.record(queue[:5])
    a, b = issue.load("a"), issue.load("b")
    assert a.printed_count == 6
    assert b.printed_count == 2
    assert [e.code for e in print_queue.pending([a, b])] == ["b2", "b3"]


def test_queue_rejects_duplicates_and_mixed_contours():
    with pytest.raises(ValueError):
        print_queue.pending([issue.Batch(codes=["same"]), issue.Batch(codes=["same"])])
    with pytest.raises(ValueError):
        print_queue.pending([issue.Batch(contour="prod"), issue.Batch(contour="sandbox")])


@pytest.mark.parametrize("pdf", [False, True])
def test_dialog_submits_one_job_with_names_and_preserves_pdf_progress(monkeypatch, pdf):
    from PySide6.QtWidgets import QApplication
    from app.ui import label_print
    from app.ui.widgets.print_queue_dialog import PrintQueueDialog
    application = QApplication.instance() or QApplication([])
    a = issue.Batch(id="a", name="First", codes=[label_print.SAMPLE_CODE])
    b = issue.Batch(id="b", name="Second", codes=[label_print.SAMPLE_CODE.replace("Xy7", "Ab8")])
    issue.save(a)
    issue.save(b)
    dialog = PrintQueueDialog([a, b])
    monkeypatch.setattr(dialog, "_target", lambda: ("test-printer", "test.pdf" if pdf else ""))
    calls = []

    def fake_print(codes, name, spec, printer, **kwargs):
        calls.append((codes, kwargs["names"]))
        # Остановка после первого товара: второй должен остаться в очереди.
        kwargs["progress"](1, len(codes))
        return 1

    monkeypatch.setattr(label_print, "print_codes", fake_print)
    try:
        dialog._print()
        assert len(calls) == 1
        assert calls[0] == (a.codes + b.codes, ["First", "Second"])
        assert issue.load("a").printed_count == (0 if pdf else 1)
        assert issue.load("b").printed_count == 0
        assert len(dialog._entries) == (2 if pdf else 1)
    finally:
        dialog.close()


def test_print_dialog_keeps_names_aligned_with_selected_range(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from app.ui import label_print, pdf_label_print
    from app.ui.widgets.issue_dialogs import PrintDialog

    application = QApplication.instance() or QApplication([])
    codes = [label_print.SAMPLE_CODE.replace("Xy7", serial) for serial in ("Ab8", "Bc9", "Cd0")]
    calls = []
    print_codes = label_print.print_codes

    def record(selected, name, spec, printer, **kwargs):
        calls.append((selected, kwargs["names"]))
        return print_codes(selected, name, spec, printer, **kwargs)

    monkeypatch.setattr(label_print, "print_codes", record)
    target = str(tmp_path / "labels.pdf")
    dialog = PrintDialog(issue.Batch(codes=codes), names=["Первый", "Второй", "Третий"])
    monkeypatch.setattr(dialog, "_target", lambda: ("", target))
    try:
        dialog.start.setValue(2)
        dialog.count.setValue(2)
        dialog._print()
        assert calls == [(codes[1:], ["Второй", "Третий"])]
        document, _ = pdf_label_print.open_labels(target)
        try:
            assert document.pageCount() == 2
        finally:
            document.close()
    finally:
        dialog.close()
