"""Один макет и одно задание принтеру для нескольких блоков маркировки."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressDialog

from ...core.marking import issue, print_queue
from .. import label_print
from .issue_dialogs import PrintDialog


class PrintQueueDialog(PrintDialog):
    def __init__(self, batches, parent=None):
        self._sources = list(batches)
        self._entries = print_queue.pending(self._sources)
        first_name = ((self._entries[0].batch.name or self._entries[0].batch.gtin)
                      if self._entries else "Очередь пуста")
        virtual = issue.Batch(name=first_name,
                              codes=[entry.code for entry in self._entries])
        super().__init__(virtual, parent)
        self.setWindowTitle("Печать выбранных блоков")
        self.start.setEnabled(False)
        self.count.setEnabled(False)
        self.hint.setText(f"Ненапечатанных этикеток: {len(self._entries)}. "
                          "Названия товаров сохраняются для каждого GTIN.")

    def _print(self):
        target = self._target()
        if target is None or not self._entries:
            return
        entries = list(self._entries)
        progress = QProgressDialog("Печать очереди…", "Остановить", 0, len(entries), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        completed = saved = 0
        error = ""

        def checkpoint():
            nonlocal saved
            if not target[1] and completed > saved:
                print_queue.record(entries[saved:completed])
                saved = completed

        def step(done, total):
            nonlocal completed
            completed = done
            if done % 25 == 0:
                checkpoint()
            progress.setLabelText(f"Этикетки: {done} из {total}")
            progress.setValue(done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            label_print.print_codes(
                [entry.code for entry in entries], "", self._spec_now(), target[0],
                names=[entry.batch.name or entry.batch.gtin for entry in entries],
                pdf_path=target[1], progress=step)
        except (label_print.PrintError, OSError) as exc:
            error = str(exc)
        finally:
            try:
                checkpoint()
            except OSError as exc:
                error = f"Не сохранён прогресс печати: {exc}. Проверьте очередь принтера перед повтором."
            progress.close()
        self._remember()
        self._entries = print_queue.pending(self._sources)
        self.batch.codes = [entry.code for entry in self._entries]
        self.batch.name = ((self._entries[0].batch.name or self._entries[0].batch.gtin)
                           if self._entries else "")
        self.start.setRange(1, max(1, len(self._entries)))
        self.start.setValue(1)
        self.count.setRange(1, max(1, len(self._entries)))
        self.count.setValue(max(1, len(self._entries)))
        self._refresh()
        if error:
            self._fail(error)
        else:
            self.hint.setText(f"{'Сохранено в PDF' if target[1] else 'Передано принтеру'}: "
                              f"{completed} из {len(entries)}.")
