"""Консольные команды загрузки истории (docs/41 §11).

ЗАЧЕМ ОНИ ЕСТЬ. Загрузка истории запускается при подключении канала, но два
боевых аккаунта подключены 8 августа — под отменённым решением «история до
подключения не нужна». Выполнить новое требование для них можно единственным
способом: запустить загрузку ещё раз, руками, с сервера.

Проверяется то, что делает команду пригодной в этот момент: канал находится
как его называет человек, глубина не принимается «на глаз», а постановка
задачи не отбрасывается дедупом — иначе «продолжить» молча не делало бы
ничего.
"""

import uuid
from typing import Any

import pytest

from app import cli
from app.services import avito_accounts as svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def accounts(make_avito_account):
    live = await make_avito_account(880001, title="Мастер СПб")
    off = await make_avito_account(880002, title="Старый канал", status="disabled")
    return live, off


async def test_channel_is_found_the_way_a_human_names_it(db_sessionmaker, accounts) -> None:
    """UUID из логов, номер из кабинета Авито, название из головы — все три.

    Заставлять человека искать идентификатор ради одной команды — способ
    сделать команду невызываемой в тот момент, когда она нужна.
    """
    live, _ = accounts
    async with db_sessionmaker() as db:
        by_uuid = await cli._resolve_accounts(db, str(live.id))
        by_number = await cli._resolve_accounts(db, "880001")
        by_title = await cli._resolve_accounts(db, "  мастер спб ")

    assert [a.id for a in by_uuid] == [live.id]
    assert [a.id for a in by_number] == [live.id]
    assert [a.id for a in by_title] == [live.id]


async def test_without_a_name_only_live_channels_are_taken(db_sessionmaker, accounts) -> None:
    """Без аргумента — все активные. Отключённый канал в Авито не ходит."""
    live, off = accounts
    async with db_sessionmaker() as db:
        found = await cli._resolve_accounts(db, None)

    ids = [a.id for a in found]
    assert live.id in ids
    assert off.id not in ids


async def test_unknown_channel_gives_nothing_rather_than_everything(
    db_sessionmaker, accounts
) -> None:
    """Опечатка в имени не должна означать «сделай это со всеми каналами»."""
    async with db_sessionmaker() as db:
        assert await cli._resolve_accounts(db, "мастер спб!") == []


async def test_repeated_run_is_not_swallowed_by_the_queue(monkeypatch) -> None:
    """«Продолжить» обязано доходить до очереди.

    ARQ держит ключ выполненной задачи ещё час и повторную постановку с тем же
    идентификатором молча отбрасывает. С постоянным `_job_id` продолжение
    загрузки в течение часа не делало бы РОВНО НИЧЕГО — без ошибки, без
    сообщения, с застывшим ходом работы на карточке.
    """
    enqueued: list[dict[str, Any]] = []

    class FakePool:
        async def enqueue_job(self, name, *args, **kwargs):
            enqueued.append({"name": name, "args": args, "job_id": kwargs.get("_job_id")})

        async def aclose(self):
            return None

    import arq

    monkeypatch.setattr(arq, "create_pool", lambda *_a, **_kw: _resolved(FakePool()))
    account_id = uuid.uuid4()

    await svc.enqueue_backfill(account_id, svc.HISTORY_ALL, dedupe=False)
    await svc.enqueue_backfill(account_id, svc.HISTORY_ALL, dedupe=False)
    await svc.enqueue_backfill(account_id)  # подключение канала — дедуп нужен

    assert enqueued[0]["job_id"] != enqueued[1]["job_id"], "два ручных запуска — две задачи"
    assert enqueued[2]["job_id"] == f"backfill:{account_id}", "подключение дедуплицируется как было"
    # Глубина едет в задачу: умолчание — вся история (требование владельца).
    assert enqueued[2]["args"] == (account_id, svc.HISTORY_ALL)


async def _resolved(value):
    return value


async def test_умершая_загрузка_перестаёт_притворяться_идущей(make_avito_account, redis) -> None:
    """«Загружаем историю…» у загрузки, которой нет, — ложь на экране.

    БОЕВОЙ СЛУЧАЙ 19.08. Владелец: «походу загрузка диалогов зависла». Так и
    было: восемь каналов стояли на 850 из 1100 с утра, а карточки показывали
    «идёт». Задачи умерли при перезапуске воркера (выкатка) и пометить себя не
    успели — ключ остался, `phase` остался «loading».

    Живой прогон обновляет отметку каждые несколько секунд. Молчание дольше
    пяти минут означает, что обновлять её больше некому: показываем
    «прервалась» и кнопку «Повторить». Загруженное на месте, продолжение —
    с той же точки.
    """
    import json
    from datetime import UTC, datetime, timedelta

    account = await make_avito_account()
    ключ = svc._progress_key(account.id)

    живое = {
        "phase": "loading",
        "depth": "all",
        "total": 1100,
        "loaded": 850,
        "chats_offset": 850,
        "queued": 0,
        "failed_chats": 0,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await redis.set(ключ, json.dumps(живое))
    состояние = await svc.get_backfill_state(redis, account.id)
    assert состояние["status"] == "running", "свежая отметка — прогон идёт"

    мёртвое = dict(живое, updated_at=(datetime.now(UTC) - timedelta(hours=10)).isoformat())
    await redis.set(ключ, json.dumps(мёртвое))
    состояние = await svc.get_backfill_state(redis, account.id)

    assert состояние["status"] == "failed", "десять часов молчания — это не «идёт»"
    assert состояние["stale"] is True
    assert состояние["loaded"] == 850, "загруженное остаётся видно: повтор продолжит с него"
    assert состояние["chats_offset"] == 850
