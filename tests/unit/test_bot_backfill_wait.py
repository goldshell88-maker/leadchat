"""Пока едет история Авито — бот не подсказывает вслепую.

БОЕВОЙ ПОВОД (19.08). Замер по журналу: в 21 вызове из 148 за день бот получал
1-3 реплики там, где в диалоге лежало 10-19 сообщений, и отвечал, не видя
разговора. Живой случай: диалог из 19 сообщений, где клиент назвал технику, дал
адрес «Октябрьская 11», оставил номер и договорился на четверг к десяти; на его
возражение о цене бот подсказал «Здравствуйте, цену скажу, когда увижу фронт
работ» — то есть поздоровался посреди переписки.

Причина не в боте: тот же диалог целиком он отрабатывает правильно. Причина в
порядке событий — подгрузка истории Авито (56 запусков за двое суток) дописывает
старые сообщения ПОСЛЕ того, как бот уже ответил на свежую реплику.

ЧТО ЗАКРЕПЛЯЕМ:
1. идёт подгрузка → тик возвращает «backfill» и переносится, подсказки нет;
2. ожидание не вечное: после лимита бот отвечает тем контекстом, что есть —
   требование владельца «подсказки бот может давать всегда»;
3. проверка не роняет тик: если состояние подгрузки узнать не удалось, работаем
   как раньше (правило №4 — недоступность чего-либо не блокирует работу).
"""

import uuid

import pytest

from app.bots import runtime


class _Redis:
    def __init__(self):
        self.значения = {}

    async def get(self, k):
        return self.значения.get(k)


@pytest.mark.asyncio
async def test_идёт_подгрузка_true(monkeypatch):
    async def состояние(redis, account_id):
        return {"status": "running", "loaded": 120, "total": 900}

    monkeypatch.setattr("app.services.avito_accounts.get_backfill_state", состояние)
    conv = type("C", (), {"account_id": uuid.uuid4()})()
    assert await runtime._идёт_подгрузка({"redis": _Redis()}, conv) is True


@pytest.mark.asyncio
async def test_подгрузка_кончилась_false(monkeypatch):
    async def состояние(redis, account_id):
        return {"status": "idle"}

    monkeypatch.setattr("app.services.avito_accounts.get_backfill_state", состояние)
    conv = type("C", (), {"account_id": uuid.uuid4()})()
    assert await runtime._идёт_подгрузка({"redis": _Redis()}, conv) is False


@pytest.mark.asyncio
async def test_прервавшаяся_подгрузка_не_держит_бота(monkeypatch):
    """`failed` и `stopped` — это НЕ «идёт»: ждать нечего, отвечаем сразу."""
    for статус in ("failed", "stopped"):

        async def состояние(redis, account_id, _s=статус):
            return {"status": _s}

        monkeypatch.setattr("app.services.avito_accounts.get_backfill_state", состояние)
        conv = type("C", (), {"account_id": uuid.uuid4()})()
        assert await runtime._идёт_подгрузка({"redis": _Redis()}, conv) is False


@pytest.mark.asyncio
async def test_сбой_проверки_не_блокирует(monkeypatch):
    """Не смогли спросить — работаем как раньше, а не молчим."""

    async def падать(redis, account_id):
        raise RuntimeError("redis лёг")

    monkeypatch.setattr("app.services.avito_accounts.get_backfill_state", падать)
    conv = type("C", (), {"account_id": uuid.uuid4()})()
    assert await runtime._идёт_подгрузка({"redis": _Redis()}, conv) is False


@pytest.mark.asyncio
async def test_диалог_без_аккаунта_не_ждёт(monkeypatch):
    """Песочница и тестовый разговор аккаунта не имеют — ждать там нечего."""
    conv = type("C", (), {"account_id": None})()
    assert await runtime._идёт_подгрузка({"redis": _Redis()}, conv) is False


def test_ожидание_ограничено():
    """Требование владельца: подсказки бот может давать ВСЕГДА.

    Значит ожидание истории обязано кончаться: 13 попыток по 45 секунд — это
    около десяти минут, после которых бот отвечает тем контекстом, что есть.
    """
    assert runtime.BACKFILL_MAX_WAITS >= 5
    предел = runtime.BACKFILL_WAIT.total_seconds() * runtime.BACKFILL_MAX_WAITS
    assert 300 <= предел <= 1200, "ждать дольше двадцати минут нельзя: лид остынет"
