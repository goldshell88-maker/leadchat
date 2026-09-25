"""Эксплуатация второй копии и отката: пять мест, где тишина дороже поломки.

Общее у всех пяти — их нельзя заметить в тот день, когда они ломаются.
Офсайт-копия, откат и доклад о возврате проверяются ровно один раз: в аварию.
Поэтому сторожа стоят здесь, а не в чек-листе.

Часть проверок гоняет НАСТОЯЩИЕ скрипты под bash с подставными `ssh`,
`scp`, `rclone`, `curl`, `age` и `docker`. Проверка текстом слабее и стоит только там, где
поведение недостижимо без сервера (ssh, docker на проде) — тогда она стережёт
конструкцию, а не результат.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess

import pytest

from app.api.routes import internal as internal_routes
from app.services import notifications as center_svc

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _script(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code_lines(rel: str) -> list[str]:
    """Строки без комментариев: `#` — пояснение, а не поведение."""
    return [ln for ln in _script(rel).splitlines() if not ln.strip().startswith("#")]


def _stub(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# =============================================================================
#  1. Во вторую страну уезжают только зашифрованные копии
# =============================================================================


def _run_push_offsite(
    tmp_path: pathlib.Path,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    """Гоняет настоящий push-offsite.sh; «второй сервер» — каталог рядом.

    Подставной ssh исполняет команду здесь же, подставной scp копирует файл.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir / "ssh", 'exec bash -c "${@: -1}"\n')
    _stub(bin_dir / "scp", 'src="${@: -2:1}"; dst="${@: -1}"; cp "$src" "${dst#*:}"\n')
    key = tmp_path / "backup_push"
    key.write_text("key", encoding="utf-8")
    remote_dir = tmp_path / "remote"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        BACKUP_DIR=str(tmp_path / "backups"),
        OFFSITE_DIR=str(remote_dir),
        SSH_KEY=str(key),
    )
    proc = subprocess.run(
        ["bash", str(DEPLOY / "push-offsite.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc, remote_dir


def _stage_with_plain_dump(tmp_path: pathlib.Path) -> pathlib.Path:
    """Каталог бэкапов, где открытый ночной дамп лежит рядом с каталогом отправки."""
    stage = tmp_path / "backups" / "offsite"
    stage.mkdir(parents=True)
    (tmp_path / "backups" / "db_20260924_0030.dump").write_bytes(b"PGDMP-plain" + b"\0" * 20000)
    return stage


def test_only_encrypted_copies_reach_the_second_server(tmp_path: pathlib.Path) -> None:
    stage = _stage_with_plain_dump(tmp_path)
    (stage / "db_20260924_0030.dump.age").write_bytes(b"age-encryption.org/v1" + b"\1" * 20000)
    (stage / "media_20260924_0030.tar.age").write_bytes(b"age-encryption.org/v1" + b"\2" * 500)

    proc, remote = _run_push_offsite(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in remote.iterdir()) == [
        "db_20260924_0030.dump.age",
        "media_20260924_0030.tar.age",
    ]


def test_a_repeated_run_sends_nothing_again(tmp_path: pathlib.Path) -> None:
    stage = _stage_with_plain_dump(tmp_path)
    (stage / "db_20260924_0030.dump.age").write_bytes(b"age-encryption.org/v1" + b"\1" * 20000)
    _run_push_offsite(tmp_path)

    proc, _ = _run_push_offsite(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "уже на месте" in proc.stdout
    assert "отправляю" not in proc.stdout


def test_without_an_encrypted_copy_nothing_leaves_the_server(tmp_path: pathlib.Path) -> None:
    """Открытый дамп лежит рядом, но наружу он не уходит ни при каких условиях."""
    _stage_with_plain_dump(tmp_path)

    proc, remote = _run_push_offsite(tmp_path)

    assert proc.returncode != 0
    assert "нет зашифрованной ночной копии" in proc.stderr
    assert not remote.exists()


# =============================================================================
#  2. Строка офсайта в crontab жила только на сервере
# =============================================================================


def test_the_crontab_in_the_repository_schedules_the_offsite_copy() -> None:
    """Установка crontab по инструкции ЗАМЕНЯЕТ таблицу целиком.

    Строка про push-offsite.sh стояла на сервере, добавленная руками, — и
    первая же переустановка (переезд, чистка, правка соседней строки) стирала
    её начисто. Cron перестаёт запускать скрипт, ошибки нет, лог пуст,
    backup.sh по-прежнему рапортует «backup OK». Узнать об этом можно было бы
    ровно в тот день, когда вторая копия и понадобится.
    """
    lines = [ln for ln in _code_lines("deploy/crontab.leadchat") if ln.strip()]
    scheduled = [ln for ln in lines if "push-offsite.sh" in ln]
    assert scheduled, (
        "в deploy/crontab.leadchat нет строки запуска push-offsite.sh — "
        "установка crontab по инструкции из его шапки затрёт офсайт-копию"
    )
    # Расписание, а не просто упоминание: строка cron начинается с пяти полей.
    for line in scheduled:
        assert re.match(r"^[\d*/,\-]+\s+[\d*/,\-]+\s+\S+\s+\S+\s+\S+\s", line), (
            f"строка офсайта не похожа на задание cron: {line}"
        )


def test_every_script_the_crontab_runs_actually_exists() -> None:
    """Обратная сторона: сторож не должен зеленеть на строку-опечатку."""
    text = _script("deploy/crontab.leadchat")
    named = set(re.findall(r"/srv/leadchat/deploy/([\w.-]+\.sh)", text))
    missing = sorted(n for n in named if not (DEPLOY / n).is_file())
    # media-gc.sh назван в закомментированной строке намеренно (спринт медиа).
    missing = [n for n in missing if n != "media-gc.sh"]
    assert not missing, f"crontab зовёт скрипты, которых нет в репозитории: {missing}"


# =============================================================================
#  3. Офсайтную копию не открывал никто и ни разу
# =============================================================================
#
# Обе автоматические проверки открывали дамп из локального каталога, то есть
# с ТОГО ЖЕ диска, что и боевая база. А восстанавливаться будут из офсайтной
# копии — её достают, когда диска, сервера или аккаунта у поставщика больше
# нет. Про неё было известно ровно одно: `rclone copy` вернул ноль.


def _run_restore_check(
    tmp_path: pathlib.Path,
    *args: str,
    remote: str | None = "offsite:bucket",
    with_key: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Гоняет настоящий restore-check.sh с подставными rclone, age и docker.

    Подставной age «расшифровывает» копированием — само шифрование проверяет
    test_backup_encryption.py настоящим age. Подставной docker падает — до него
    нам дела нет: источник дампа скрипт называет РАНЬШЕ, в шапке отчёта.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fixture = tmp_path / "offsite-copy.dump.age"
    fixture.write_bytes(b"PGDMP-offsite" + b"\0" * 4096)

    _stub(
        bin_dir / "rclone",
        'case "$1" in\n'
        '  lsf) printf "%s\\n" "db_20260101_0300.dump.age" "db_20260812_0330.dump.age" ;;\n'
        f'  copyto) cp "{fixture}" "$3" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _stub(
        bin_dir / "age",
        "while [ $# -gt 1 ]; do\n"
        '  case "$1" in -o) out="$2"; shift 2 ;; -i) shift 2 ;; *) shift ;; esac\n'
        "done\n"
        'cp "$1" "$out"\n',
    )
    _stub(bin_dir / "docker", "exit 1\n")
    identity = tmp_path / "backup-age-key.txt"
    if with_key:
        identity.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "db_20260812_0400.dump").write_bytes(b"PGDMP-local" + b"\0" * 4096)

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_DIR=str(tmp_path / "нет-развёртывания"),
        BACKUP_DIR=str(backups),
        BACKUP_AGE_IDENTITY=str(identity),
    )
    if remote is None:
        env.pop("RCLONE_REMOTE", None)
    else:
        env["RCLONE_REMOTE"] = remote

    return subprocess.run(
        ["bash", str(ROOT / "deploy/restore-check.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_the_quarterly_drill_opens_the_offsite_copy_by_default(tmp_path: pathlib.Path) -> None:
    """Без аргументов учение обязано брать ОФСАЙТНУЮ копию.

    Локальный дамп в BACKUP_DIR лежит и заведомо свежее по имени — если бы
    скрипт по-прежнему брал его, мы увидели бы в отчёте именно его.
    """
    proc = _run_restore_check(tmp_path)
    out = proc.stdout + proc.stderr

    assert "ОФСАЙТ" in out, f"учение опять восстанавливает локальный дамп:\n{out}"
    assert "db_20260812_0330.dump" in out, f"в отчёте не офсайтная копия:\n{out}"
    assert "db_20260812_0400.dump" not in out, (
        f"учение взяло дамп с того же диска, что и боевая база:\n{out}"
    )


def test_the_drill_says_out_loud_when_it_could_not_reach_the_offsite(
    tmp_path: pathlib.Path,
) -> None:
    """Молчаливый откат на локальный дамп вернул бы ту же дыру — с зелёным
    отчётом об учении сверху."""
    proc = _run_restore_check(tmp_path, remote=None)
    out = proc.stdout + proc.stderr

    assert "db_20260812_0400.dump" in out, f"без офсайта учение обязано хотя бы идти дальше:\n{out}"
    assert "офсайт" in out.lower(), f"провал офсайта прошёл незамеченным:\n{out}"


def test_the_drill_says_out_loud_when_it_has_no_key_to_decrypt(tmp_path: pathlib.Path) -> None:
    """Без секретной половины ключа офсайтная копия — нечитаемый набор байтов."""
    proc = _run_restore_check(tmp_path, with_key=False)
    out = proc.stdout + proc.stderr

    assert "нет ключа расшифровки" in out, out
    assert "db_20260812_0400.dump" in out, f"без ключа учение обязано хотя бы идти дальше:\n{out}"


def test_an_explicit_path_still_wins(tmp_path: pathlib.Path) -> None:
    """Совместимость: путь первым аргументом принимался и раньше, им
    пользуются руками при разборе конкретного файла."""
    chosen = tmp_path / "db_20250101_0000.dump"
    chosen.write_bytes(b"PGDMP-manual" + b"\0" * 4096)

    proc = _run_restore_check(tmp_path, str(chosen))
    out = proc.stdout + proc.stderr

    assert str(chosen) in out, f"явно указанный дамп не взят:\n{out}"


# =============================================================================
#  4. «Система вернулась» воскрешала тревогу «Система не отвечает снаружи»
# =============================================================================


def _run_watchdog(tmp_path: pathlib.Path, state: str) -> tuple[str, str]:
    """Гоняет настоящий watchdog-offsite.sh с подставным curl.

    Возвращает (вывод скрипта, всё, что «ушло» в центр уведомлений).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sent = tmp_path / "sent.jsonl"
    _stub(
        bin_dir / "curl",
        'out=""; data=""; url=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    -d) data="$2"; shift 2 ;;\n'
        "    -X|-H|-w|--max-time) shift 2 ;;\n"
        "    -s|-sS) shift ;;\n"
        '    http*) url="$1"; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'if [ -n "$data" ]; then\n'
        f'  printf "%s\\n" "$data" >> {sent}\n'
        '  printf "202"; exit 0\n'
        "fi\n"
        'case "$url" in\n'
        '  *health/deep) printf "%s" "{\\"alive\\":true}" ;;\n'
        '  *) [ -n "$out" ] && printf "%s" "{\\"status\\":\\"ok\\"}" > "$out"; printf "200" ;;\n'
        "esac\n"
        "exit 0\n",
    )

    token = tmp_path / "token"
    token.write_text("служебный-токен\n", encoding="utf-8")
    state_file = tmp_path / "state"
    state_file.write_text(state, encoding="utf-8")

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        TOKEN_FILE=str(token),
        STATE_FILE=str(state_file),
        PROD_URL="http://прод.невидимка",
        NOTIFY_URL="http://10.10.0.1/api/v1/internal/notify",
    )
    proc = subprocess.run(
        ["bash", str(ROOT / "deploy/watchdog-offsite.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    reported = sent.read_text(encoding="utf-8") if sent.exists() else ""
    return proc.stdout + proc.stderr, reported


@pytest.mark.parametrize("reported_before", ["1", "0"])
def test_the_recovery_report_does_not_revive_the_outage_alarm(
    tmp_path: pathlib.Path, reported_before: str
) -> None:
    """Доклад о возврате обязан ехать СВОИМ видом.

    ЧТО БЫЛО. Обе новости уезжали видом `system.unreachable`, а вид — это ключ
    склейки повторов в центре. Доклад «система вернулась» попадал в ту же
    строку, что и тревога о падении: счётчик повторов рос, строка снова
    становилась непрочитанной, красная плашка «Система не отвечает снаружи»
    всплывала поверх экрана — ровно в тот момент, когда всё уже работало.

    Обе ветки проверяются одинаково: и когда о простое доложили в момент
    падения, и когда доклад задержался (прод лежал целиком).
    """
    state = f"3 1000000000 {reported_before}\n"
    out, reported = _run_watchdog(tmp_path, state)

    assert reported, f"о возврате не доложили вовсе:\n{out}"
    assert '"kind":"system.recovered"' in reported, (
        f"возврат уехал не своим видом:\n{reported}\n{out}"
    )
    assert "system.unreachable" not in reported, (
        "возврат по-прежнему едет видом тревоги о падении — он воскрешает её "
        f"красной плашкой:\n{reported}"
    )


def test_the_watchdog_still_reports_an_outage_as_critical(tmp_path: pathlib.Path) -> None:
    """Обратная сторона: тревога о падении обязана остаться тревогой.

    Без этой проверки предыдущая зеленела бы и на стороже, из которого
    `system.unreachable` убрали целиком.
    """
    watchdog = _code_lines("deploy/watchdog-offsite.sh")
    assert any("system.unreachable" in ln for ln in watchdog), (
        "сторож перестал сообщать о недоступности системы вообще"
    )


def test_the_recovery_kind_is_registered_on_the_server(tmp_path: pathlib.Path) -> None:
    """Вид, которого нет в закрытом списке, ручка отвергает с 400.

    То есть незарегистрированный вид — это не «уведомление другого цвета», а
    доклад, потерянный совсем: скрипт получит ошибку и запишет строку в лог на
    сервере, который никто не читает.
    """
    spec = internal_routes.KNOWN_KINDS.get("system.recovered")
    assert spec is not None, (
        "вид `system.recovered` не заведён в KNOWN_KINDS — сторож шлёт его, "
        "а ручка отвечает 400, и доклад о возврате пропадает целиком"
    )
    assert spec.severity == "info", "возврат — не авария, будить им нельзя"
    assert spec.center_kind in center_svc.KINDS, (
        "вид вне каталога центра приходит человеку без иконки и без умолчаний"
    )


def test_the_recovery_and_the_outage_do_not_share_a_dedup_key() -> None:
    """Ключ склейки — то самое, из-за чего доклад попадал в чужую строку."""
    body = internal_routes.InternalNotifyIn(kind="system.recovered", detail="вернулась")
    recovered = internal_routes._draft(
        "system.recovered", internal_routes.KNOWN_KINDS["system.recovered"], body
    )
    outage = internal_routes._draft(
        "system.unreachable", internal_routes.KNOWN_KINDS["system.unreachable"], body
    )

    assert recovered.dedup_key != outage.dedup_key, (
        "возврат склеивается с тревогой о падении: он поднимет ей счётчик повторов "
        "и вернёт красную плашку на работающей системе"
    )
    assert recovered.severity != outage.severity


def test_every_kind_the_scripts_send_is_a_kind_the_handle_accepts() -> None:
    """Сквозная сверка: bash и закрытый список обязаны совпадать.

    Расхождение не видно ниоткуда — скрипт получает 400 и идёт дальше, а
    событие не доезжает ни до кого.
    """
    pattern = re.compile(r'\bnotify(?:_center)?\s+"([a-z_]+\.[a-z_]+)"')
    unknown: list[str] = []
    for path in sorted(DEPLOY.rglob("*.sh")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for kind in pattern.findall(line):
                if kind not in internal_routes.ALLOWED_KINDS:
                    unknown.append(f"{path.relative_to(ROOT)}: {kind}")
    assert not unknown, (
        "скрипты шлют виды, которых нет в закрытом списке ручки (ответ 400, "
        "событие теряется):\n" + "\n".join(unknown)
    )


# =============================================================================
#  5. Автооткат при красной выкатке не мог сработать ни разу
# =============================================================================
#
# Здесь проверка текстом, и это её честный предел: настоящий откат живёт по ssh
# на боевом сервере. Но стережём мы не формулировку, а конструкцию — метку до
# сборки и то, что откат больше не зовёт скрипт, рассчитанный на реестр.

SHIP = "deploy/workstation/ship.sh"


def test_ship_marks_a_point_of_return_before_it_builds() -> None:
    """Точка возврата существует ровно до `docker build -t …:prod`.

    Сборка переставляет тег на новый слой, и предыдущий образ остаётся
    безымянным — вернуться на него после сборки уже нечем.
    """
    text = _script(SHIP)
    assert ":rollback" in text, "в выкатке нет метки отката — откатываться будет не на что"
    mark = text.index("docker tag ${tag} ${tag%:*}:rollback")
    build = text.index("docker build -q -t ${tag}")
    assert mark < build, (
        "метка ставится ПОСЛЕ сборки: к этому моменту предыдущий образ уже "
        "потерял тег, и откат вернёт то же самое сломанное"
    )


def test_ship_does_not_roll_back_through_the_registry_path() -> None:
    """ЧТО БЫЛО. На красном smoke выкатка звала `deploy/rollback.sh` — обёртку
    над `deploy.sh`, который ТЯНЕТ ОБРАЗЫ ИЗ РЕЕСТРА. Реестра у сервера нет
    вовсе (образы собираются на нём, см. docker-compose.override.yml), тега-SHA
    не существует, файла `.state/previous_tag` тоже.

    То есть автооткат не «иногда не срабатывал» — он не мог сработать ни разу.
    Первая же красная выкатка оставляла прод лежать со свежесобранным сломанным
    кодом и печатала «откат тоже не прошёл — нужен человек на сервере».
    """
    guilty = [ln.strip() for ln in _code_lines(SHIP) if "rollback.sh" in ln]
    assert not guilty, (
        "выкатка снова откатывается через путь с реестром, которого у сервера нет:\n"
        + "\n".join(guilty)
    )


def test_both_red_outcomes_roll_back_the_images() -> None:
    """Красных исходов два — здоровье и регрессия, и оба обязаны откатывать."""
    calls = [ln for ln in _code_lines(SHIP) if "rollback_images" in ln and "()" not in ln]
    assert len(calls) >= 2, "откат вызывается не из всех красных исходов выкатки: " + repr(calls)
    assert "rollback_images() {" in _script(SHIP), "функция отката пропала"
