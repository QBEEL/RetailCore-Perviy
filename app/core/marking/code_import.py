"""Пакетный импорт списка проверки из PDF, CSV и текстовых файлов."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import codes, pdf_codes


@dataclass
class ImportedFile:
    name: str
    batch: codes.Batch = field(default_factory=codes.Batch)
    pdf: pdf_codes.FullScan | None = None
    error: str = ""


def read_files(paths: list[str], progress: Callable[[int, int], None] | None = None
               ) -> list[ImportedFile]:
    imported = []
    for index, path in enumerate(paths, 1):
        source = Path(path)
        result = ImportedFile(source.name)
        try:
            if source.suffix.lower() == ".pdf":
                result.pdf = pdf_codes.scan_full(path)
                lines = result.pdf.marks
            else:
                lines = codes.split_lines(codes.read_codes_file(path))
            result.batch = codes.parse_many(lines)
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            result.error = str(error)
        imported.append(result)
        if progress:
            progress(index, len(paths))
    return imported
