"""Проверка «дамп не пустышка» не должна врать на большом оглавлении.

ЧТО СЛУЧИЛОСЬ 8 августа. `deploy/backup.sh` убеждается, что в дампе есть
данные ключевых таблиц, и делал это так:

    printf '%s\\n' "$TOC" | grep -q "TABLE DATA public ${_t}" || fail ...

`grep -q` выходит на ПЕРВОМ совпадении и закрывает канал. `printf`, если ещё
не дописал, получает SIGPIPE и возвращает 141 — а при `set -o pipefail` это
статус всей цепочки. То есть таблица найдена, а проверка кричит «в дампе нет
данных, восстанавливать будет нечего», и снимок базы объявляется провальным.

ПОЧЕМУ НЕ ЛОВИЛОСЬ РАНЬШЕ. Пока оглавление было коротким (~160 записей, 12 КБ),
оно целиком влезало в буфер канала: printf успевал завершиться до выхода grep,
и SIGPIPE неоткуда было взяться. Расширение партиций до 26 месяцев (#24) подняло
оглавление до 45 КБ — и проверка начала валить каждый прогон. Причём падала она
на таблицах, чьи строки в НАЧАЛЕ списка: `messages` «находился», потому что его
партиции в самом конце и grep дочитывал почти всё.

То есть ошибка жила в проверке резервного копирования с самого начала и ждала
роста базы, чтобы сработать. Хуже места для скрытой ошибки трудно придумать:
она либо роняет ночной бэкап, либо приучает не верить его жалобам.

ЗДЕСЬ ПРОВЕРЯЕТСЯ САМА КОНСТРУКЦИЯ, а не текст скрипта: тест гоняет настоящий
bash на оглавлении, заведомо большем буфера канала.
"""

import shutil
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


#: Оглавление ЗАВЕДОМО больше буфера канала (64 КБ на Linux). Совпадение стоит
#: в самом начале — так grep выходит раньше всего, и гонка наиболее вероятна.
def _big_toc() -> str:
    head = "4431; 0 16499 TABLE DATA public users leadchat"
    filler = "\n".join(
        f"{4500 + i}; 0 {17000 + i} TABLE DATA public messages_y2024m{i % 12 + 1:02d} leadchat"
        for i in range(3000)
    )
    return f"{head}\n{filler}"


def _run(snippet: str) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        f"""
        set -euo pipefail
        TOC=$(cat)
        {snippet}
        echo НАЙДЕНО
        """
    )
    return subprocess.run(
        ["bash", "-c", script],
        input=_big_toc(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_old_pipe_really_was_broken() -> None:
    """Обратная проверка: старая конструкция действительно врёт.

    Без неё тест ниже был бы пустым обрядом — «работает» без доказательства,
    что было сломано. Здесь видно своими глазами: совпадение есть, а код
    возврата 141 (SIGPIPE).
    """
    result = _run('printf \'%s\\n\' "$TOC" | grep -q "TABLE DATA public users"')

    assert result.returncode != 0, "старая конструкция обязана падать — иначе тест ниже бессмыслен"
    assert "НАЙДЕНО" not in result.stdout


def test_the_case_construct_survives_a_big_table_of_contents() -> None:
    """Как в скрипте сейчас: сравнение строки в памяти, без канала."""
    result = _run('case "$TOC" in *"TABLE DATA public users"*) : ;; *) exit 1 ;; esac')

    assert result.returncode == 0, result.stderr
    assert "НАЙДЕНО" in result.stdout


def test_missing_table_is_still_caught() -> None:
    """Починка не должна превратить проверку в вечное «всё хорошо»."""
    result = _run('case "$TOC" in *"TABLE DATA public clients"*) : ;; *) exit 1 ;; esac')

    assert result.returncode == 1
    assert "НАЙДЕНО" not in result.stdout


def test_backup_script_does_not_pipe_the_toc_into_grep() -> None:
    """Сторож на возврат приёма — в самом скрипте.

    Конструкция выглядит естественной и напрашивается при первой же правке;
    цена возврата — ложный провал ночного бэкапа.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "deploy" / "backup.sh").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if '"$TOC"' in line and "grep -q" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, "оглавление снова уходит в канал с grep -q:\n" + "\n".join(offenders)
