"""Проверка shell-бэкапа: отказ любого шага не публикует готовую копию."""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
if BASH is None and shutil.which("git"):
    candidate = Path(shutil.which("git")).parents[1] / "bin" / "bash.exe"
    if candidate.is_file():
        BASH = str(candidate)
pytestmark = pytest.mark.skipif(BASH is None, reason="нужен bash (на Windows входит в Git)")


@pytest.fixture
def backup(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    scripts = {
        "docker": "printf '%s\\n' '-- test dump'; [ \"$FAIL_STEP\" != dump ] || exit 11\n",
        "gzip": '[ "$FAIL_STEP" != gzip ] || exit 12\nexec /usr/bin/gzip "$@"\n',
        "tar": '[ "$FAIL_STEP" != tar ] || exit 13\nexec /usr/bin/tar "$@"\n',
    }
    for name, body in scripts.items():
        file = commands / name
        file.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        file.chmod(0o755)
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "attachment.txt").write_text("keep attachment", encoding="utf-8")
    # Уже готовая копия должна пережить неудачу следующего запуска.
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "previous.sql.gz").write_bytes(b"previous snapshot")

    def run(failure=""):
        directory = tmp_path.as_posix()
        if os.name == "nt":
            directory = "/" + directory[0].lower() + directory[2:]
        env = dict(os.environ, RETAILCORE_ROOT=directory, FAIL_STEP=failure)
        env["BACKUP_TEST_BIN"] = directory + "/bin"
        env["BACKUP_TEST_SCRIPT"] = (ROOT / "server" / "tools" / "backup.sh").as_posix()
        paths = [str(commands)]
        if os.name == "nt":
            paths.append(str(Path(BASH).parents[1] / "usr" / "bin"))
        env["PATH"] = os.pathsep.join([*paths, env["PATH"]])
        # Git Bash добавляет свои каталоги перед унаследованным PATH;
        # подмены команд ставим первыми уже внутри shell.
        return subprocess.run(
            [BASH, "-c", 'export PATH="$BACKUP_TEST_BIN:$PATH"; exec sh "$BACKUP_TEST_SCRIPT"'],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

    return tmp_path, run


@pytest.mark.parametrize("step", ["dump", "gzip", "tar"])
def test_failed_backup_is_not_published(backup, step):
    root, run = backup
    result = run(step)
    assert result.returncode != 0, result.stdout + result.stderr
    assert sorted(file.name for file in (root / "backups").glob("*.gz")) == ["previous.sql.gz"]
    assert (root / "backups" / "previous.sql.gz").read_bytes() == b"previous snapshot"


def test_successful_backup_contains_dump_and_attachments(backup):
    root, run = backup
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    dump, = (root / "backups").glob("retailcore-*.sql.gz")
    assert gzip.decompress(dump.read_bytes()) == b"-- test dump\n"
    archive, = (root / "backups").glob("files-*.tar.gz")
    with tarfile.open(archive) as files:
        assert files.extractfile("files/attachment.txt").read() == b"keep attachment"
    assert list((root / "backups").glob("*.part")) == []
