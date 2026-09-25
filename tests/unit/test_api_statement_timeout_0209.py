"""Потолок на один запрос — у веб-процесса, и только у него.

⚠ ЗАЧЕМ (замер боя 02.09). Пул к базе — десять соединений на рабочий процесс.
Поиск по телефону шёл 27-93 секунды и всё это время держал одно соединение из
десяти. При двух-трёх таких запросах API вставал целиком, и ждали ВСЕ: p95 у
карточки диалога и у ленты сообщений — 17,7 секунды, хотя сами по себе они
отвечают за 130-160 мс.

Потолок не ускоряет ни одного запроса. Он превращает «встало у всех» в «не
получилось у одного» — а это и есть надёжность.

⚠ И ПОЧЕМУ НЕ ГЛОБАЛЬНО. Миграции, обратное заполнение и пересчёт витрин бывают
долгими по делу, и они идут другим процессом. Общий потолок остаётся выключенным
намеренно; ограничен ровно тот путь, за которым сидит человек.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.db.session import _connect_args

PG = "postgresql+asyncpg://u:p@h/db"


def настройки(component: str) -> dict[str, str]:
    args = _connect_args(PG, f"leadchat-{component}", component)
    return args.get("server_settings", {})


def test_у_веб_процесса_потолок_есть() -> None:
    сервер = настройки("api")
    assert "statement_timeout" in сервер, (
        "без потолка один застрявший запрос занимает соединение из пула в десять "
        "и морит голодом всех остальных"
    )
    assert int(сервер["statement_timeout"]) == settings.db_api_statement_timeout_ms


@pytest.mark.parametrize("component", ["worker", "scheduler", "cli"])
def test_у_фоновых_потолка_нет(component: str) -> None:
    """Миграции и обратное заполнение бывают долгими по делу."""
    assert "statement_timeout" not in настройки(component), (
        f"{component}: потолок веб-запроса не должен резать фоновую работу"
    )


def test_потолок_не_бесконечный_и_не_издевательский() -> None:
    """Число должно быть терпимым для человека и достаточным для тяжёлой выборки.

    Границы взяты из замеров: самая тяжёлая законная выборка (список «Все» с
    архивом в 42 тысячи строк) укладывается в сотни миллисекунд, а человек за
    экраном считает приложение зависшим уже через несколько секунд.
    """
    assert 5_000 <= settings.db_api_statement_timeout_ms <= 30_000


def test_у_sqlite_настроек_сервера_нет() -> None:
    """Юнит-тесты ходят на SQLite: там таких настроек не существует."""
    assert _connect_args("sqlite+aiosqlite:///:memory:", "leadchat-api", "api") == {}


@pytest.mark.parametrize("component", ["api", "worker", "scheduler", "cli"])
def test_проводка_component_доходит_до_настроек(
    component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ПРОВОДКА, БЕЗ КОТОРОЙ ПРОВЕРКИ ВЫШЕ ЗЕЛЕНЕЮТ ВПУСТУЮ.

    У `_connect_args` параметр `component` имеет значение по умолчанию «api».
    Забудь передать его в `init_engine` — и потолок веб-запроса молча накроет
    воркер, планировщик и командную строку, то есть ровно те процессы, которым
    долгие запросы положены по делу: миграции, обратное заполнение, пересчёт
    витрин. Ни один тест выше этого не заметит: они зовут `_connect_args`
    напрямую и аргумент передают сами.

    Поэтому здесь проверяется НАСТОЯЩИЙ путь: с чем `init_engine` эту функцию
    зовёт.
    """
    import app.db.session as db_mod

    видел: list[tuple[str, str]] = []

    def шпион(url: str, application_name: str, комп: str = "api") -> dict[str, object]:
        видел.append((application_name, комп))
        return {}

    monkeypatch.setattr(db_mod, "_connect_args", шпион)
    прежний, прежняя_фабрика = db_mod.engine, db_mod.session_factory
    db_mod.engine, db_mod.session_factory = None, None
    try:
        db_mod.init_engine(PG, component=component)
    finally:
        db_mod.engine, db_mod.session_factory = прежний, прежняя_фабрика

    assert видел == [(f"leadchat-{component}", component)], (
        f"init_engine не передал component в настройки соединения: {видел}"
    )
