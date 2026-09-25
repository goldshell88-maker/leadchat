"""Отказ очереди у ответа бота обязан стать видимым (03.09).

⚠ ЧТО БЫЛО. Реплика бота коммитится в базу со статусом `pending`, и только
потом ставится задача доставки. `_enqueue` при отказе очереди писал строку в
лог и возвращал None — то есть для вызывающего ничего не происходило.

`pending` не видит НИКТО: `/retry` берёт только `failed`, красную метку диалогу
ставит `refresh_undelivered` тоже по `failed`, а сторож «клиент ждёт» молчит.
Ответ клиенту оставался в подвешенном состоянии навсегда.

Ровно эта беда уже разобрана и закрыта на пути ОПЕРАТОРА
(`services/messages.enqueue_deliver` + `mark_enqueue_failed`) — у бота закрыта
не была. Два пути, делающие одно дело по-разному: класс, который в этом
проекте ловится чаще всего.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.bots import runtime

pytestmark = pytest.mark.anyio


class ПадающаяОчередь:
    async def enqueue_job(self, *_a: object, **_k: object) -> None:
        raise ConnectionError("redis недоступен")


class Сессия:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


def _ctx(monkeypatch, *, очередь_падает: bool) -> tuple[dict, list]:
    помечено: list[uuid.UUID] = []

    async def пометка(_db: object, mid: uuid.UUID) -> bool:
        помечено.append(mid)
        return True

    monkeypatch.setattr(runtime.messages_service, "mark_enqueue_failed", пометка)

    async def постановка(_arq, name, *args, **kw):  # noqa: ANN001, ANN202
        if очередь_падает:
            raise ConnectionError("redis недоступен")
        return None

    monkeypatch.setattr(runtime.ArqRedis, "enqueue_job", постановка)
    monkeypatch.setattr(runtime.log, "exception", lambda *a, **k: None)
    return {"redis": object(), "arq": object(), "db_session_factory": Сессия}, помечено


def _outbox(mid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        events=[],
        notifications=[],
        jobs=[SimpleNamespace(name="deliver_message", args=(mid,), defer_by=None, job_id=None)],
        clear=lambda: None,
    )


async def test_не_вставшая_доставка_помечается_красным(monkeypatch) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: иначе ответ бота висит pending и его не видит никто."""
    ctx, помечено = _ctx(monkeypatch, очередь_падает=True)
    mid = uuid.uuid4()

    await runtime.flush_outbox(ctx, _outbox(mid))

    assert помечено == [mid], (
        "задача доставки не встала, а сообщение осталось pending — "
        "клиент без ответа, и об этом не знает ни оператор, ни система"
    )


async def test_успешная_постановка_ничего_не_помечает(monkeypatch) -> None:
    """Вторая половина: красная метка на удавшейся отправке — ложная тревога."""
    ctx, помечено = _ctx(monkeypatch, очередь_падает=False)

    await runtime.flush_outbox(ctx, _outbox(uuid.uuid4()))

    assert помечено == []


async def test_отказ_пометки_не_роняет_рассылку(monkeypatch) -> None:
    """Пометка — не главное в кадре: остальные события обязаны уехать."""
    ctx, _ = _ctx(monkeypatch, очередь_падает=True)

    async def падает(*_a: object, **_k: object) -> bool:
        raise RuntimeError("база тоже недоступна")

    monkeypatch.setattr(runtime.messages_service, "mark_enqueue_failed", падает)

    await runtime.flush_outbox(ctx, _outbox(uuid.uuid4()))  # не должно бросить


async def test_прочие_задачи_метки_не_получают(monkeypatch) -> None:
    """`bot_ask_timeout` — не отправка клиенту: помечать нечего и незачем."""
    ctx, помечено = _ctx(monkeypatch, очередь_падает=True)
    outbox = SimpleNamespace(
        events=[],
        notifications=[],
        jobs=[
            SimpleNamespace(
                name="bot_ask_timeout",
                args=(uuid.uuid4(), "t"),
                defer_by=timedelta(minutes=25),
                job_id=None,
            )
        ],
        clear=lambda: None,
    )

    await runtime.flush_outbox(ctx, outbox)

    assert помечено == []
