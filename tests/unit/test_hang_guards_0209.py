"""Предохранители от «висит навсегда» (замеры 02.09).

⚠ ОБЩЕЕ ПРАВИЛО, РАДИ КОТОРОГО ЭТОТ ФАЙЛ. Ресурс без потолка — это не «иногда
медленно», а «однажды встанет всё». Обработчик запроса держит и соединение к БД,
и соединение к Redis; повисни любое из них — занятыми останутся оба, а пул к БД
всего десять на процесс. Дальше очередь: ждут все, включая приём сообщений от
клиентов. Так 02.09 и потерялось 105 вебхуков Авито из 566.
"""

from __future__ import annotations

import pytest

from app.core.config import settings


def test_у_redis_есть_потолки() -> None:
    """`await` к Redis обязан однажды вернуться, даже если Redis замолчал."""
    import app.core.redis as redis_mod

    прежний = redis_mod.client
    redis_mod.client = None
    try:
        c = redis_mod.init_client("redis://localhost:6379/0")
        kw = c.connection_pool.connection_kwargs
        assert kw.get("socket_timeout") == settings.redis_socket_timeout_seconds, (
            "без потолка на команду молчащий Redis держит и соединение к БД"
        )
        assert kw.get("socket_connect_timeout") == settings.redis_connect_timeout_seconds
        assert kw.get("health_check_interval") == 30
    finally:
        redis_mod.client = прежний


@pytest.mark.parametrize(
    ("имя", "значение", "низ", "верх"),
    [
        ("команда", settings.redis_socket_timeout_seconds, 1.0, 10.0),
        ("соединение", settings.redis_connect_timeout_seconds, 0.5, 10.0),
    ],
)
def test_потолки_redis_разумны(имя: str, значение: float, низ: float, верх: float) -> None:
    """Наши команды к Redis — миллисекунды по существу; секунды тут с запасом."""
    assert низ <= значение <= верх, f"{имя}: {значение} с — вне разумного"


def test_воркер_отмечается_живым_часто() -> None:
    """⚠ ЗАВИСШИЙ ВОРКЕР ДОЛЖЕН БЫТЬ ЗАМЕЧЕН, А НЕ ЖДАТЬ ЧАСА.

    По умолчанию ARQ обновляет отметку здоровья раз в `job_timeout` — у нас 300
    секунд, а в бою значение не менялось с момента запуска вовсе. Всё это время
    docker считает воркер живым и не перезапускает: сообщения копятся в очереди,
    сторож молчит.
    """
    from app.workers.main import WorkerSettings

    интервал = getattr(WorkerSettings, "health_check_interval", None)
    assert интервал is not None, "отметка здоровья обновляется раз в job_timeout — слишком редко"
    assert интервал <= 60, f"{интервал} с — зависший воркер будет считаться живым слишком долго"
