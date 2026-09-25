"""Отказ второй копии доходит до колокольчика (проверка 24.09).

push-offsite.sh везёт зашифрованную ночную копию на второй сервер. Его fail()
только печатал строку в лог: затяжной отказ — отозванный ключ, лежащий мост,
диск напарника — не заметил бы никто, пока копия не понадобится. Гоняется
настоящий скрипт; ssh, scp и curl подставные.
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


def _push(
    tmp_path: pathlib.Path, *, bridge_up: bool, token_in_env_file: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = 'exec bash -c "${@: -1}"\n' if bridge_up else "exit 255\n"
    _stub(bin_dir / "ssh", ssh)
    _stub(bin_dir / "scp", 'src="${@: -2:1}"; dst="${@: -1}"; cp "$src" "${dst#*:}"\n')
    _stub(
        bin_dir / "curl",
        'for a in "$@"; do case "$a" in {*) echo "$a" >> "$CALLS" ;; esac; done\n',
    )
    key = tmp_path / "backup_push"
    key.write_text("key", encoding="utf-8")
    stage = tmp_path / "backups" / "offsite"
    stage.mkdir(parents=True)
    (stage / "db_20260924_0030.dump.age").write_bytes(b"age-encryption.org/v1" + b"\1" * 20000)
    env_file = tmp_path / ".env"
    env_file.write_text(
        'INTERNAL_SERVICE_TOKEN="from-env-file"\n' if token_in_env_file else "",
        encoding="utf-8",
    )
    calls = tmp_path / "curl-calls"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        BACKUP_DIR=str(tmp_path / "backups"),
        OFFSITE_DIR=str(tmp_path / "remote"),
        SSH_KEY=str(key),
        ENV_FILE=str(env_file),
        CALLS=str(calls),
    )
    env.pop("INTERNAL_SERVICE_TOKEN", None)
    if not token_in_env_file:
        env["INTERNAL_SERVICE_TOKEN"] = "from-cron"
    proc = subprocess.run(
        ["bash", str(ROOT / "deploy/push-offsite.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc, calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_a_dead_bridge_is_reported_not_just_logged(tmp_path: pathlib.Path) -> None:
    proc, sent = _push(tmp_path, bridge_up=False)

    assert proc.returncode != 0
    assert '"kind":"backup.second_copy_failed"' in sent
    assert "нет связи" in sent


def test_the_token_is_taken_from_the_env_file_when_cron_has_none(tmp_path: pathlib.Path) -> None:
    proc, sent = _push(tmp_path, bridge_up=False, token_in_env_file=True)

    assert proc.returncode != 0
    assert '"kind":"backup.second_copy_failed"' in sent


def test_a_delivered_copy_reports_nothing(tmp_path: pathlib.Path) -> None:
    proc, sent = _push(tmp_path, bridge_up=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sent == ""
