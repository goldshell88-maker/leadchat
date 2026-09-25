"""Снимок перед выкаткой — не ночная копия (проверка 24.09).

Выкатка (ship.sh, шаг 1) гоняет backup.sh с BACKUP_LABEL=deploy. Скрипт доходил
до конца и слал «backup.ok», а сторож свежести копий смотрит только на эту
отметку: пропади ночная строка cron — он молчал бы сколько угодно, ведь
выкатки идут каждый день. А ротация облака ходила в S3 и на снимке: сбой S3
валил выкатку и поднимал ложное «копия не создана».

Гоняется настоящий скрипт; docker, rclone и curl подставные и пишут вызовы в
файлы рядом.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from tests.unit.test_backup_encryption import FAKE_DOCKER, _identity, _stub

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash"),
    pytest.mark.skipif(
        shutil.which("age") is None or shutil.which("age-keygen") is None, reason="нужен age"
    ),
]

FAKE_RCLONE = r"""
echo "$*" >> "$CALLS/rclone"
case "$1" in
  copy) dest="$BUCKET/${3#*:*/}"; mkdir -p "$dest"; cp "$2" "$dest" ;;
  *) : ;;
esac
"""

FAKE_CURL = r"""
for a in "$@"; do case "$a" in {*) echo "$a" >> "$CALLS/curl" ;; esac; done
"""


def _run(tmp_path: pathlib.Path, label: str | None) -> tuple[str, str]:
    _, recipients = _identity(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "docker", FAKE_DOCKER)
    _stub(bin_dir / "rclone", FAKE_RCLONE)
    _stub(bin_dir / "curl", FAKE_CURL)
    calls = tmp_path / "calls"
    calls.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=d\n", encoding="utf-8")
    (tmp_path / "backups").mkdir()
    (tmp_path / "media").mkdir()

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_DIR=str(deploy),
        COMPOSE_FILE="compose.yml",
        BACKUP_DIR=str(tmp_path / "backups"),
        MEDIA_ROOT=str(tmp_path / "media"),
        RCLONE_REMOTE="offsite:bucket",
        BACKUP_AGE_RECIPIENTS=str(recipients),
        BUCKET=str(tmp_path / "bucket"),
        MARKER="marker",
        CALLS=str(calls),
        INTERNAL_SERVICE_TOKEN="test-token",
        INTERNAL_NOTIFY_URL="http://127.0.0.1:1/api/v1/internal/notify",
        COPYFILE_DISABLE="1",
    )
    env.pop("RCLONE_EXTRA_ARGS", None)
    env.pop("BACKUP_LABEL", None)
    if label:
        env["BACKUP_LABEL"] = label
    proc = subprocess.run(
        ["bash", str(ROOT / "deploy" / "backup.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    def read(name: str) -> str:
        path = calls / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return read("rclone"), read("curl")


def test_the_deploy_snapshot_neither_reports_freshness_nor_touches_the_cloud(tmp_path) -> None:
    rclone, curl = _run(tmp_path, "deploy")

    assert rclone == "", "снимок перед выкаткой ходил в облако"
    assert '"kind":"backup.ok"' not in curl


def test_the_nightly_run_reports_freshness_and_rotates_the_cloud(tmp_path) -> None:
    rclone, curl = _run(tmp_path, None)

    assert '"kind":"backup.ok"' in curl
    assert "delete offsite:bucket/db/" in rclone
