"""Снимок журналов не теряет вторую реплику сервиса (проверка 24.09).

Отметка «снято до» была одна на сервис. При двух контейнерах одного сервиса
(`--scale worker=3` или одноразовый `docker compose run api …`) первый
снимался от старой отметки и сдвигал её на «сейчас», а второй — уже от
«сейчас»: всё, что он написал с прошлого снимка, в архив не попадало.

Гоняется настоящий archive-logs.sh; подставной docker записывает, с какой
отметки у него просили журнал каждого контейнера.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

# id -> (сервис, номер реплики, одноразовый ли)
CONTAINERS = {
    "w1": ("worker", "1", "False"),
    "w2": ("worker", "2", "False"),
    "run1": ("api", "1", "True"),
}


def _docker(bin_dir: pathlib.Path, calls: pathlib.Path) -> None:
    cases = "\n".join(
        f"    {cid}) svc={svc}; num={num}; oneoff={oneoff} ;;"
        for cid, (svc, num, oneoff) in CONTAINERS.items()
    )
    body = f"""
case "$1" in
  ps)
    for cid in {" ".join(CONTAINERS)}; do
      case "$cid" in
{cases}
      esac
      if [ "$oneoff" = "True" ] && [[ "$*" == *"oneoff=False"* ]]; then continue; fi
      echo "$cid"
    done ;;
  inspect)
    cid="${{@: -1}}"
    case "$cid" in
{cases}
    esac
    printf '%s %s\\n' "$svc" "$num" ;;
  logs)
    cid="${{@: -1}}"
    since=""
    prev=""
    for a in "$@"; do [ "$prev" = "--since" ] && since="$a"; prev="$a"; done
    echo "$cid since=$since" >> "{calls}"
    echo "2026-09-24T10:00:00Z строка $cid" ;;
  *) exit 0 ;;
esac
"""
    p = bin_dir / "docker"
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(archive: pathlib.Path, bin_dir: pathlib.Path) -> None:
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        LOG_ARCHIVE_DIR=str(archive),
        LOG_ARCHIVE_PROJECT="leadchat",
        LOG_ARCHIVE_MIN_FREE_PCT="0",
    )
    proc = subprocess.run(
        ["bash", str(ROOT / "deploy/archive-logs.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_each_replica_is_read_from_its_own_last_snapshot(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    archive = tmp_path / "logs"
    _docker(bin_dir, calls)

    _run(archive, bin_dir)
    first = (archive / ".since-worker-1").read_text(encoding="utf-8")
    calls.write_text("", encoding="utf-8")
    _run(archive, bin_dir)

    asked = dict(line.split(" since=") for line in calls.read_text(encoding="utf-8").splitlines())
    assert asked == {"w1": first, "w2": first}


def test_one_off_compose_runs_are_not_archived(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    _docker(bin_dir, calls)

    _run(tmp_path / "logs", bin_dir)

    assert "run1" not in calls.read_text(encoding="utf-8")
