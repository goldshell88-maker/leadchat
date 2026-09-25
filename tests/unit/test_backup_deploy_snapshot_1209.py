"""Снимок перед выкаткой не должен жить как ночная копия (12.09).

ЧТО СЛУЧИЛОСЬ. `ship.sh` шагом 1 зовёт `backup.sh` — тот же скрипт, что и
ночной cron. Снимок получал имя ночного, уезжал в S3 и на второй сервер и
хранился 14 суток. При десятке выкаток в день это 165 дампов на 10 ГБ против
15 ночных — диск на 83 %, сторож «меньше 15 %» в одном шаге от тревоги.

ТЕПЕРЬ снимок помечен (`BACKUP_LABEL=deploy` → `db_<штамп>_deploy.dump`),
офсайт для него пропускается, а ротация оставляет последние три. Здесь
проверяется сама конструкция ротации настоящим bash: шаблон из «?» не
задевает ночные дампы, `ls` без совпадений не роняет скрипт под `pipefail`.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKUP = ROOT / "deploy" / "backup.sh"
SHIP = ROOT / "deploy" / "workstation" / "ship.sh"
OFFSITE = ROOT / "deploy" / "push-offsite.sh"


def _rotation_snippet() -> str:
    """Строки ротации помеченных снимков — из самого скрипта, не копия."""
    text = BACKUP.read_text(encoding="utf-8")
    m = re.search(r"^\{ ls -1t .*?\n.*?xargs -r rm -f --\n", text, re.M | re.S)
    assert m, "в backup.sh нет ротации помеченных снимков"
    return m.group(0)


def _touch(path: pathlib.Path, age_s: int) -> None:
    path.write_bytes(b"x")
    ts = time.time() - age_s
    os.utime(path, (ts, ts))


def test_ротация_оставляет_три_помеченных_и_не_трогает_ночные(tmp_path: pathlib.Path) -> None:
    ночные = [f"db_2026091{i}_0030.dump" for i in range(1, 6)]
    выкатки = [f"db_20260912_0{i}00_deploy.dump" for i in range(1, 8)]
    for i, name in enumerate(ночные):
        _touch(tmp_path / name, age_s=(10 - i) * 3600)
    for i, name in enumerate(выкатки):
        _touch(tmp_path / name, age_s=(20 - i) * 60)
    script = (
        "set -euo pipefail\n"
        f'BACKUP_DIR="{tmp_path}"\nBACKUP_KEEP_LABELLED=3\n' + _rotation_snippet() + "echo ГОТОВО\n"
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and "ГОТОВО" in res.stdout, res.stderr
    осталось = sorted(p.name for p in tmp_path.iterdir())
    assert [n for n in осталось if n.endswith("_0030.dump")] == sorted(ночные)
    assert [n for n in осталось if "_deploy" in n] == sorted(выкатки[-3:])


def test_без_помеченных_снимков_ротация_не_роняет_скрипт(tmp_path: pathlib.Path) -> None:
    _touch(tmp_path / "db_20260912_0030.dump", age_s=60)
    script = (
        "set -euo pipefail\n"
        f'BACKUP_DIR="{tmp_path}"\nBACKUP_KEEP_LABELLED=3\n' + _rotation_snippet() + "echo ГОТОВО\n"
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and "ГОТОВО" in res.stdout, res.stderr
    assert (tmp_path / "db_20260912_0030.dump").exists()


def test_выкатка_помечает_снимок_а_офсайт_берёт_только_ночной() -> None:
    ship = SHIP.read_text(encoding="utf-8")
    assert re.search(r"^on_server .*BACKUP_LABEL=deploy .*backup\.sh", ship, re.M)
    backup = BACKUP.read_text(encoding="utf-8")
    assert 'DUMP="db_${STAMP}${BACKUP_LABEL:+_${BACKUP_LABEL}}.dump"' in backup
    assert 'if [ -n "${BACKUP_LABEL}" ]; then' in backup, (
        "офсайт для снимка выкатки не пропускается"
    )
    offsite = OFFSITE.read_text(encoding="utf-8")
    assert "db_????????_????.dump" in offsite, "во вторую страну поедет снимок выкатки"
