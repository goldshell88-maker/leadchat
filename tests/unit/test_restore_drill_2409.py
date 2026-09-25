"""Учение по восстановлению доходит до конца (проверка 24.09).

pg_restore на боевом дампе возвращает 1 из-за одной известной ошибки витрины
mv_conversation_stats (разбор — в backup-verify.sh). Под `set -e` учение
умирало сразу после «[2/4] pg_restore…»: без проверок, без ИТОГ и без пометки
«не засчитано». Гоняется настоящий restore-check.sh; docker подставной.
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

MV_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  relation "app_settings" does not exist\n'
    "Command was: REFRESH MATERIALIZED VIEW public.mv_conversation_stats;\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)
OTHER_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  relation "messages" does not exist\n'
    "pg_restore: warning: errors ignored on restore: 1\n"
)


def _drill(tmp_path: pathlib.Path, restore_stderr: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "restore.err").write_text(restore_stderr, encoding="utf-8")
    _stub(
        bin_dir / "docker",
        'echo "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        # Имя контейнера тоже содержит «pg_restore» — ловим саму команду.
        f'  *" pg_restore -U "*) cat "{tmp_path / "restore.err"}" >&2; exit 1 ;;\n'
        '  *"psql"*) echo "таблиц в public | 42" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "db_20260924_0030.dump").write_bytes(b"PGDMP" + b"\0" * 4096)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_DIR=str(tmp_path / "нет-развёртывания"),
        BACKUP_DIR=str(backups),
        MEDIA_ROOT=str(tmp_path / "media-нет"),
        CALLS=str(tmp_path / "docker-calls"),
    )
    env.pop("RCLONE_REMOTE", None)
    return subprocess.run(
        ["bash", str(ROOT / "deploy/restore-check.sh"), "--local"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_the_known_view_error_does_not_end_the_drill(tmp_path: pathlib.Path) -> None:
    proc = _drill(tmp_path, MV_ERROR)
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "ИТОГ" in out, out
    calls = (tmp_path / "docker-calls").read_text(encoding="utf-8")
    assert "ANALYZE" in calls and "REFRESH MATERIALIZED VIEW mv_conversation_stats" in calls


def test_any_other_restore_error_fails_the_drill_out_loud(tmp_path: pathlib.Path) -> None:
    proc = _drill(tmp_path, OTHER_ERROR)
    out = proc.stdout + proc.stderr

    assert proc.returncode != 0
    assert "УЧЕНИЕ НЕ ЗАСЧИТАНО" in out, out
    assert "ИТОГ" not in out
