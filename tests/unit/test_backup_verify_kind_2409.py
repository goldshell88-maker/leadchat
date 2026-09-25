"""Невосстановимая копия — критичная тревога, а не важная (проверка 24.09).

Ежемесячная проверка слала любой провал видом `restore_check.failed`
(важное): «дамп не восстанавливается» тонуло среди важных и не поднимало
красную плашку, а критичный `backup.verify_failed` не слал никто. Гоняется
настоящий backup-verify.sh; docker и curl подставные.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from tests.unit.test_backup_encryption import _stub

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def test_an_unrestorable_dump_is_reported_as_critical(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "docker",
        'case "$*" in\n'
        '  *" pg_restore -U "*)\n'
        '    echo "pg_restore: error: could not read from input file: end of file" >&2\n'
        "    exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _stub(
        bin_dir / "curl",
        'for a in "$@"; do case "$a" in {*) echo "$a" >> "$CALLS" ;; esac; done\n',
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text("INTERNAL_SERVICE_TOKEN=t\n", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "db_20260924_0030.dump").write_bytes(b"PGDMP" + b"\0" * 4096)
    calls = tmp_path / "curl-calls"

    proc = subprocess.run(
        ["bash", str(ROOT / "deploy/backup-verify.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            DEPLOY_DIR=str(deploy),
            BACKUP_DIR=str(backups),
            CALLS=str(calls),
        ),
    )

    assert proc.returncode != 0
    sent = calls.read_text(encoding="utf-8")
    assert '"kind":"backup.verify_failed"' in sent, sent
    assert "дамп не восстанавливается" in sent
