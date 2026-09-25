"""Сорвавшийся дедлайн бота не замораживает диалог навсегда (03.09).

⚠ ЧТО БЫЛО. Тело `bot_ask_timeout` — одна транзакция к базе. Заминка на
секунду, и ARQ помечает задачу проваленной БЕЗ ПОВТОРА: `max_tries` действует
только на `Retry`, обычное исключение — это конец.

Дедлайн при этом исчезает. Диалог остаётся с `bot_active=True` и ждёт вечно:
бот не дожимает клиента и не передаёт человеку. Сторож зависших ботов сюда не
смотрит — он ловит «клиент написал, бот не ответил», а здесь последнее слово
как раз за ботом.

⚠ ПОЧЕМУ ПОВТОР ЗДЕСЬ БЕЗОПАСЕН, В ОТЛИЧИЕ ОТ `bot_step`. Задача
самоаннулируется по токену: ответил клиент — `waiting` пересоздан, и повтор
вернёт `stale`, ничего не сделав. Транзакция при ошибке откатывается целиком.
У `bot_step` иначе: там повтор после коммита тика сгенерировал бы ВТОРУЮ
реплику клиенту.
"""

from __future__ import annotations

import uuid

import pytest
from arq import Retry

from app.bots import runtime

pytestmark = pytest.mark.anyio


class ПадающаяФабрика:
    def __call__(self):  # noqa: ANN204
        raise ConnectionError("база недоступна")


async def test_заминка_базы_даёт_повтор(monkeypatch) -> None:
    monkeypatch.setattr(runtime.log, "warning", lambda *a, **k: None)
    ctx = {"db_session_factory": ПадающаяФабрика(), "redis": object(), "job_try": 1}

    with pytest.raises(Retry):
        await runtime.bot_ask_timeout(ctx, uuid.uuid4(), "tok")


async def test_на_исходе_попыток_отказ_становится_видимым(monkeypatch) -> None:
    """⚠ ВЕЧНЫЙ ПОВТОР НЕ ЛУЧШЕ ПОТЕРИ: у отказа должен быть конец и след."""
    записи: list[str] = []
    monkeypatch.setattr(runtime.log, "warning", lambda *a, **k: None)
    monkeypatch.setattr(runtime.log, "error", lambda e, **k: записи.append(e))
    ctx = {
        "db_session_factory": ПадающаяФабрика(),
        "redis": object(),
        "job_try": runtime.ЛИМИТ_ПОВТОРОВ_ДЕДЛАЙНА,
    }

    исход = await runtime.bot_ask_timeout(ctx, uuid.uuid4(), "tok")

    assert исход == "failed"
    assert "bot.ask_timeout_lost" in записи, (
        "дедлайн потерян молча — диалог замёрзнет с bot_active=True, и не узнает никто"
    )
