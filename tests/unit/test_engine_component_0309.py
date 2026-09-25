"""Каждый процесс объявляет себя базе — и получает СВОЙ потолок (03.09).

⚠ ФАЙЛ НАПИСАН ПО СЛЕДАМ СОБСТВЕННОЙ РЕГРЕССИИ, ВЫКАЧЕННОЙ УТРОМ ТОГО ЖЕ ДНЯ.

Потолок запроса в 15 секунд завели для веб-процесса и только для него: за
веб-запросом сидит человек, и один застрявший запрос занимает соединение пула,
которого потом не хватает приёму сообщений (02.09 так потеряли 105 вебхуков).

Но `init_engine` имел умолчание `component="api"`, а воркер и планировщик
звали его БЕЗ аргумента. В бою это дало две беды сразу:

* воркеру достался чужой потолок — обратное заполнение истории и выгрузка
  статистики идут минутами и были бы срезаны на пятнадцатой секунде;
* все соединения подписались `leadchat-api`, и регламент «кто держит пул»
  (05 §8) перестал различать процессы: сорок строк с одним именем.

Проверено на бою: воркер отвечал `statement_timeout = 15s` и
`application_name = leadchat-api`.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.db import session as db_session

PG = "postgresql+asyncpg://u:p@h/db"


@pytest.mark.parametrize("component", ["worker", "scheduler", "cli", "unknown"])
def test_потолок_веб_процесса_не_достаётся_остальным(component: str) -> None:
    args = db_session._connect_args(PG, f"leadchat-{component}", component)
    настройки = args["server_settings"]
    assert настройки.get("statement_timeout") != str(settings.db_api_statement_timeout_ms), (
        f"процесс «{component}» получил потолок веб-запроса: долгая законная "
        "работа будет срезана на пятнадцатой секунде"
    )


def test_веб_процесс_потолок_получает() -> None:
    """Вторая половина правила: убери потолок у api — и вернётся забитый пул."""
    args = db_session._connect_args(PG, "leadchat-api", "api")
    assert args["server_settings"]["statement_timeout"] == str(settings.db_api_statement_timeout_ms)


def test_умолчание_безопасно() -> None:
    """⚠ УМОЛЧАНИЕ — «unknown», А НЕ «api», И ЭТО СУТЬ ПРАВКИ.

    Забытый аргумент обязан НЕ резать ничего и быть заметным. Умолчание «api»
    делало ровно наоборот: молча выдавало чужой потолок.
    """
    import inspect

    assert inspect.signature(db_session.init_engine).parameters["component"].default == "unknown"
    args = db_session._connect_args(PG, "leadchat-unknown", "unknown")
    assert "statement_timeout" not in args["server_settings"]


def test_каждая_точка_входа_называет_себя() -> None:
    """⚠ «ПОДКЛЮЧЕНО ЛИ» — ОТДЕЛЬНЫЙ ВОПРОС, И СЛОМАНА БЫЛА ИМЕННО ЭТА ПОЛОВИНА.

    Проверки выше говорят, что потолок раздаётся правильно. Они ничего не
    говорят о том, что процессы себя НАЗЫВАЮТ. Читаем не текст, а дерево
    разбора: вызов `init_engine` в каждой точке входа обязан нести component.
    """
    import ast

    точки = {
        "app/main.py": "api",
        "app/workers/main.py": "worker",
        "app/scheduler/main.py": "scheduler",
        "app/cli.py": "cli",
    }
    for путь, ожидаемый in точки.items():
        дерево = ast.parse(open(путь, encoding="utf-8").read())
        вызовы = [
            узел
            for узел in ast.walk(дерево)
            if isinstance(узел, ast.Call)
            and isinstance(узел.func, ast.Attribute)
            and узел.func.attr == "init_engine"
        ]
        assert вызовы, f"{путь}: движок не поднимается вовсе?"
        for вызов in вызовы:
            имена = {kw.arg: kw.value for kw in вызов.keywords}
            assert "component" in имена, (
                f"{путь}: init_engine без component — процесс возьмёт умолчание "
                "и потеряет себя в pg_stat_activity"
            )
            значение = имена["component"]
            assert isinstance(значение, ast.Constant) and значение.value == ожидаемый, (
                f"{путь}: ожидался component='{ожидаемый}'"
            )
