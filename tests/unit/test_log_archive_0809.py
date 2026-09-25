"""Журналы контейнеров переживают пересоздание — сторожа на четыре свойства.

ЧТО БЫЛО. Журнал json-file лежит в /var/lib/docker/containers/<id>/, где
<id> — id КОНТЕЙНЕРА. Выкатка пересоздаёт контейнеры, docker сносит каталог
вместе с журналом. Разбор «почему поехала вёрстка» и разбор пропажи реплик
бота упирались ровно в это: логов за нужный день уже не было.

Замер боя 08.09 (прод, только чтение): api 1 178 572 Б журнала за
21 минуту жизни контейнера, nginx 1 546 362 Б — то есть 76 и 108 МБ в сутки.
Прежний кольцевой буфер 20 МБ × 5 = 100 МБ давал nginx меньше суток: журнал
за нужный день исчезал сам, даже без выкатки.

Проверяем НАСТОЯЩИЙ deploy/archive-logs.sh под bash с подставным `docker` —
текстовая проверка тут ничего не стоила бы: вся суть в том, что снятое
остаётся на диске, когда контейнера уже нет.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "deploy" / "archive-logs.sh"
SHIP = ROOT / "deploy" / "workstation" / "ship.sh"
COMPOSE = ROOT / "docker-compose.prod.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _docker_stub(bin_dir: pathlib.Path, containers: dict[str, tuple[str, str]]) -> None:
    """Подставной `docker`: id -> (имя сервиса, что отдаёт `docker logs`).

    Настоящий docker в модульных тестах недоступен, а проверять надо ровно то,
    что скрипт делает с его выводом.
    """
    cases = []
    for cid, (svc, payload) in containers.items():
        cases.append(f"    {cid}) svc={svc}; body={payload!r} ;;")
    body = f"""
ids="{" ".join(containers)}"
case "$1" in
  ps)      for i in $ids; do echo "$i"; done ;;
  inspect) cid="${{@: -1}}"
           case "$cid" in
{chr(10).join(cases)}
           esac
           printf '%s\\n' "$svc" ;;
  logs)    cid="${{@: -1}}"
           case "$cid" in
{chr(10).join(cases)}
           esac
           printf '%s\\n' "$body" ;;
  *)       exit 0 ;;
esac
"""
    p = bin_dir / "docker"
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(archive_dir: pathlib.Path, bin_dir: pathlib.Path, **extra: str):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["LOG_ARCHIVE_DIR"] = str(archive_dir)
    env["LOG_ARCHIVE_PROJECT"] = "leadchat"
    # Порог свободного места снимаем: на машине проверяющего диск может быть
    # забит, и сторож краснел бы не по делу.
    env["LOG_ARCHIVE_MIN_FREE_PCT"] = "0"
    env.update(extra)
    return subprocess.run(
        ["bash", str(ARCHIVE)], env=env, capture_output=True, text=True, timeout=60
    )


def _archived_text(archive_dir: pathlib.Path, svc: str) -> str:
    """Всё, что скрипт сложил по сервису, — распакованным и слитым в одну строку."""
    import gzip

    out = []
    for f in sorted((archive_dir / svc).glob("*.log.gz")):
        out.append(gzip.decompress(f.read_bytes()).decode("utf-8", "replace"))
    return "".join(out)


# =============================================================================
#  1. Снятое переживает пересоздание контейнера
# =============================================================================
#
# ДИВЕРСИЯ. В deploy/archive-logs.sh убрать вызов `gzip -6 -c "${out}.tmp"`
# (или строку `printf '%s' "$NOW" >"$stamp_file"` вместе с ним) — на диске не
# остаётся ничего, и проверка краснеет на первой же строке `assert "старое"`.
def test_stroki_perezhivayut_peresozdanie(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arch = tmp_path / "logs"

    # Первый контейнер: id c1, в журнале строка «старое».
    _docker_stub(bin_dir, {"c1": ("api", "2026-09-08T04:00:00Z старое")})
    r1 = _run(arch, bin_dir)
    assert r1.returncode == 0, r1.stderr

    # Выкатка: контейнер пересоздан, новый id, СТАРОГО ЖУРНАЛА У НЕГО НЕТ —
    # ровно то, что делает docker.
    _docker_stub(bin_dir, {"c2": ("api", "2026-09-08T05:00:00Z новое")})
    r2 = _run(arch, bin_dir)
    assert r2.returncode == 0, r2.stderr

    text = _archived_text(arch, "api")
    assert "старое" in text, "журнал прежнего контейнера потерян — вся затея впустую"
    assert "новое" in text


# =============================================================================
#  2. Неудачное снятие НЕ двигает отметку времени
# =============================================================================
#
# ПОЧЕМУ ОТДЕЛЬНЫЙ СТОРОЖ. Если отметку сдвинуть до успешной записи, провалившийся
# прогон объявит снятым то, чего на диске нет: следующий возьмёт `--since` уже
# после пропущенного куска, и дыра в журнале останется навсегда — незаметная,
# потому что и файлы есть, и ошибок больше нет.
#
# ДИВЕРСИЯ. Перенести `printf '%s' "$NOW" >"$stamp_file"` ВЫШЕ вызова gzip
# (то есть до проверки успеха) — отметка появится и проверка покраснеет.
def test_proval_ne_dvigaet_otmetku(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arch = tmp_path / "logs"
    arch.mkdir()

    # `docker logs` падает: id есть, имя сервиса есть, а чтения журнала нет.
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  ps) echo c1 ;;\n"
        "  inspect) echo api ;;\n"
        "  logs) echo 'Error: No such container' >&2; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    r = _run(arch, bin_dir)

    assert r.returncode == 0, "провал снятия не имеет права ронять выкатку"
    assert "!!" in r.stdout, "провал должен быть ГРОМКИМ, а не молчаливым"
    assert not (arch / ".since-api").exists(), (
        "отметка сдвинута после провала — следующий прогон перескочит пропущенный кусок"
    )


# =============================================================================
#  3. Кончается место — снимок пропускается, старое не трогается
# =============================================================================
#
# ДИВЕРСИЯ. Убрать в скрипте блок `--- место на диске ---` (проверку FREE_PCT) —
# скрипт начнёт снимать журнал и на переполненном разделе, строки «пропущен» не
# будет, проверка покраснеет.
def test_malo_mesta_snimok_propuskaetsya(tmp_path: pathlib.Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arch = tmp_path / "logs"
    _docker_stub(bin_dir, {"c1": ("api", "строка")})

    # Подставной df: занято 99%, свободно 1% — ниже порога по умолчанию (10%).
    df = bin_dir / "df"
    df.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
        "echo '/dev/sda1 1000 990 10 99% /'\n",
        encoding="utf-8",
    )
    df.chmod(df.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["LOG_ARCHIVE_DIR"] = str(arch)
    env["LOG_ARCHIVE_PROJECT"] = "leadchat"
    r = subprocess.run(["bash", str(ARCHIVE)], env=env, capture_output=True, text=True, timeout=60)

    assert r.returncode == 0
    assert "пропущен" in r.stdout, "молчаливый пропуск по месту — это тот же потерянный журнал"
    assert not (arch / "api").exists()


# =============================================================================
#  4. Выкатка снимает журнал ДО пересоздания и не падает из-за него
# =============================================================================
#
# ПОРЯДОК — ЭТО ВСЁ. Снимок после `compose up -d` снимает пустоту: старого
# журнала к тому моменту уже нет. А `die` на этом шаге останавливал бы выкатку
# всей команде из-за несобранного архива.
#
# ДИВЕРСИЯ. Перенести блок «4б» ниже цикла `for svc in worker scheduler api
# nginx` — краснеет проверка порядка. Дописать `|| die …` к вызову скрипта —
# краснеет проверка на die.
def test_ship_snimaet_zhurnal_do_peresozdaniya() -> None:
    lines = SHIP.read_text(encoding="utf-8").splitlines()
    code = [(i, ln) for i, ln in enumerate(lines) if not ln.strip().startswith("#")]

    call = [i for i, ln in code if "archive-logs.sh" in ln]
    assert call, "ship.sh больше не снимает журналы перед пересозданием"

    # Опорой берём объявление шага 5, а не первый попавшийся `up -d`: выше по
    # файлу такой же вызов стоит внутри `rollback_images()` — функции, которая
    # выполняется только при провале smoke и к порядку шагов отношения не имеет.
    step5 = [i for i, ln in code if 'say "5.' in ln]
    assert step5, "не нашёл в ship.sh объявления шага перезапуска"
    assert min(call) < min(step5), (
        "снимок журналов идёт ПОСЛЕ пересоздания — снимать будет уже нечего"
    )

    for i in call:
        assert "die" not in lines[i], "провал снятия журнала не имеет права остановить выкатку"


# =============================================================================
#  5. Кольцевой буфер контейнера покрывает больше суток
# =============================================================================
#
# ЗАЧЕМ ЧИСЛО. Снимок снимается раз в сутки (cron) и на каждой выкатке. Между
# ними всё держит буфер json-file. При замеренных 108 МБ строк в сутки у nginx
# буфер меньше 108 МБ означает потерю начала суток ещё до снимка.
#
# ДИВЕРСИЯ. Вернуть в docker-compose.prod.yml `max-size: "20m"` / `max-file: "5"`
# (100 МБ) — проверка краснеет: 100 < 108.
def test_bufer_zhurnala_pokryvaet_sutki() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    block = re.search(
        r'x-logging:\s*&default-logging.*?max-size:\s*"(\d+)m".*?max-file:\s*"(\d+)"',
        text,
        re.S,
    )
    assert block, "исчез якорь x-logging — журналы контейнеров настраиваются где-то ещё"
    total_mb = int(block.group(1)) * int(block.group(2))
    assert total_mb >= 108, (
        f"буфер журнала {total_mb} МБ меньше суточного потока nginx (108 МБ, замер 08.09)"
    )

    # Сервис без `logging: *default-logging` молча уезжает на умолчание демона —
    # и его буфер перестаёт быть тем, что здесь посчитано.
    for svc in ("postgres:", "redis:", "nginx:", "certbot:"):
        tail = text.split(f"\n  {svc}", 1)
        assert len(tail) == 2, f"сервис {svc} исчез из compose"
        assert "logging: *default-logging" in tail[1][:1200], (
            f"{svc} потерял общий якорь журналирования"
        )
