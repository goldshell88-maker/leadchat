"""Величина сна по 429 зажата потолком: заголовок присылает ЧУЖАЯ сторона.

⚠ ЧЕМ ЭТО ОПАСНО. `Retry-After` берётся из ответа Авито и уходит прямо в
`asyncio.sleep`. Спящий обработчик всё это время держит соединение к базе — а
ARQ обрывает задачу по `job_timeout` в 300 секунд, то есть слишком большой сон
не «подождёт и продолжит», а сожжёт заход впустую с занятым соединением. Ровно
этим путём 02.09 выело пул и потерялось 105 вебхуков из 566.

Отрицательное значение так же вредно: `sleep` с ним не спит вовсе, и цикл
`while True` превращается в горячую петлю запросов к площадке.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.avito.client import DEFAULT_RETRY_AFTER, RETRY_AFTER_MAX, AvitoClient
from app.integrations.avito.errors import RateLimited


def _ответ(retry_after: str | None) -> httpx.Response:
    заголовки = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=заголовки, request=httpx.Request("GET", "https://x/y"))


@pytest.mark.parametrize(
    ("прислали", "ожидаем"),
    [
        ("5", 5),
        ("60", RETRY_AFTER_MAX),
        ("86400", RETRY_AFTER_MAX),  # сутки — задача умерла бы по job_timeout
        ("-1", 0),  # горячая петля запросов к площадке
        # Латиницей намеренно: в значении HTTP-заголовка кириллица роняет
        # сам конструктор ответа (эта грабля в проекте уже записана).
        ("soon", DEFAULT_RETRY_AFTER),
        (None, DEFAULT_RETRY_AFTER),
    ],
)
def test_сон_по_лимиту_зажат(прислали: str | None, ожидаем: int) -> None:
    with pytest.raises(RateLimited) as поймали:
        AvitoClient._raise_for_status(_ответ(прислали), "тест")
    assert поймали.value.retry_after == ожидаем


def test_потолок_меньше_бюджета_задачи() -> None:
    """⚠ ПОТОЛОК ОБЯЗАН БЫТЬ МЕНЬШЕ `job_timeout`, ИНАЧЕ ОН НЕ ПОТОЛОК.

    Сон длиннее бюджета задачи означает, что заход гарантированно оборвётся, не
    сделав работы, — и всё это время будет занято соединение к базе.
    """
    from app.workers.main import WorkerSettings

    assert RETRY_AFTER_MAX < WorkerSettings.job_timeout, (
        f"потолок сна {RETRY_AFTER_MAX} с не меньше бюджета задачи "
        f"{WorkerSettings.job_timeout} с — заход сгорит впустую с занятым соединением"
    )
