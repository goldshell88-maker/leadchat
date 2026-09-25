"""Сторожа, которых не хватило 2 сентября.

⚠ ГЛАВНЫЙ УРОК ТОГО ДНЯ. Сторож писал «OK: health/deep зелёные» ровно в те
минуты, когда пул соединений был выбран целиком: 166 ошибок «QueuePool limit
reached», 1 100 брошенных запросов и 105 ПОТЕРЯННЫХ ВЕБХУКОВ Авито — сообщений
живых людей, которых теперь нет.

Числа пула сторож при этом отдавал: `body["pool"]` был на месте. Просто ни одно
правило на них не смотрело. Зелёный сигнал означал не «всё хорошо», а «никто не
смотрел туда, где плохо».
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.routes.health import _red_flags
from app.core.config import settings


def тело(**over: Any) -> dict[str, Any]:
    основа: dict[str, Any] = {"pool": {"size": 10, "overflow": 5, "checkedout": 0}, "queue": {}}
    основа.update(over)
    return основа


def флаги(**over: Any) -> list[str]:
    return _red_flags(тело(**over), db_ok=True, redis_ok=True, failed=[])


def test_свободный_пул_не_тревожит() -> None:
    assert "pool.checkedout" not in флаги()


@pytest.mark.parametrize("занято", [12, 13, 15])
def test_выбранный_пул_краснеет(занято: int) -> None:
    """⚠ ИМЕННО ЭТОГО ПРАВИЛА НЕ ХВАТИЛО 02.09."""
    флаг = флаги(pool={"size": 10, "overflow": 5, "checkedout": занято})
    assert "pool.checkedout" in флаг, (
        f"занято {занято} из 15 — сторож молчит, как молчал в день потери 105 вебхуков"
    )


def test_порог_срабатывает_до_полного_исчерпания() -> None:
    """0.8, а не 1.0: к моменту исчерпания запросы уже стоят по 30 секунд.

    Сторож обязан звать ДО того, как станет больно, иначе он лишь подтверждает
    уже случившееся.
    """
    assert settings.health_pool_busy_red < 1.0
    # 11 из 15 — это 73 %, ещё не порог; 12 из 15 — 80 %, уже порог.
    assert "pool.checkedout" not in флаги(pool={"size": 10, "overflow": 5, "checkedout": 11})
    assert "pool.checkedout" in флаги(pool={"size": 10, "overflow": 5, "checkedout": 12})


def test_правило_считает_долю_а_не_число(monkeypatch: pytest.MonkeyPatch) -> None:
    """Порог в абсолютных единицах разъехался бы с настройкой пула молча."""
    # Пул вдвое больше — та же доля обязана давать тот же ответ.
    monkeypatch.setattr(settings, "db_pool_size", 20)
    monkeypatch.setattr(settings, "db_max_overflow", 10)
    assert "pool.checkedout" in флаги(pool={"size": 20, "overflow": 0, "checkedout": 24})
    assert "pool.checkedout" not in флаги(pool={"size": 20, "overflow": -2, "checkedout": 18})


@pytest.mark.parametrize(("занято", "сверх_пула"), [(3, -7), (8, -2), (9, -1)])
def test_недобранный_пул_не_краснеет(занято: int, сверх_пула: int) -> None:
    """`pool.overflow()` — ТЕКУЩЕЕ число сверх пула, а не допустимое (24.09).

    Пока пул не наполнен, оно отрицательное, и потолок «размер + overflow»
    превращался в «уже открытые соединения»: 8 и 9 занятых из 15 давали ложное
    degraded — четыре из шести тревог пула на бою с 15.09.
    """
    assert "pool.checkedout" not in флаги(
        pool={"size": 10, "overflow": сверх_пула, "checkedout": занято}
    )


def test_без_чисел_пула_не_выдумываем_тревогу() -> None:
    """Пул без счётчиков (StaticPool в тестах) — не повод краснеть."""
    assert "pool.checkedout" not in флаги(pool={})


class Шпион:
    """Ловит записи нашего логгера — structlog мимо caplog не проходит."""

    def __init__(self) -> None:
        self.записи: list[dict[str, Any]] = []

    def log(self, _level: int, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})

    def info(self, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})

    def warning(self, event: str, **kw: Any) -> None:
        self.записи.append({"event": event, **kw})


async def test_в_журнале_есть_длительность_и_маршрут(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ БЕЗ ЭТОГО ДИАГНОСТИКА СЛЕПА.

    В журнале nginx 4 320 записей `GET /api/v1/conversations` неразличимы между
    собой при медиане 141 мс и максимуме 59,97 с. Какой запрос вставал — по
    логам было не узнать, и на поиск ушли часы ручных замеров.

    ⚠ ПРОВЕРЯЕТСЯ НАСТОЯЩИМ ЗАПРОСОМ, А НЕ ГРЕПОМ ПО ИСХОДНИКУ. Здесь стоял
    тест, искавший строки в тексте `create_app`. Он падал на СОБСТВЕННОМ
    комментарии, объясняющем, чего мы не делаем, — то есть проверял написанное,
    а не работающее. За один день это третий случай подряд.
    """
    from app import main

    шпион = Шпион()
    monkeypatch.setattr(main, "log", шпион)
    await client.get("/api/health")

    записи = [з for з in шпион.записи if з["event"] == "http.request"]
    assert записи, "запросы не логируются вовсе"
    assert записи[-1]["route"] == "/api/health", "нет маршрута — записи не сгруппировать"
    assert isinstance(записи[-1]["ms"], float), "нет длительности — нечего сравнивать"
    assert записи[-1]["status"] == 200


async def test_строка_запроса_в_журнал_не_попадает(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ В НЕЙ `q=<телефон клиента>` — ПЕРСОНАЛЬНЫЕ ДАННЫЕ.

    Журнал читают глазами и хранят. Ровно поэтому маршрут пишется приложением,
    а не nginx: там доступен только сырой `$request_uri` со строкой запроса.
    """
    from app import main

    шпион = Шпион()
    monkeypatch.setattr(main, "log", шпион)
    await client.get("/api/health?q=79151234567&secret=hunter2")

    целиком = json.dumps(шпион.записи, ensure_ascii=False, default=str)
    assert "79151234567" not in целиком, "телефон из строки запроса утёк в журнал"
    assert "hunter2" not in целиком, "секрет из строки запроса утёк в журнал"
