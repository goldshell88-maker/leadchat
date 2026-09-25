"""Пачка возврата розданных диалогов не должна застревать на тех, кто в сети.

Выборка брала старейшие розданные диалоги всех владельцев, а тех, кто в сети,
пропускала уже в цикле: пять давних диалогов работающего человека занимали
пачку каждую минуту, и диалог ушедшего не возвращался никогда. Та же болезнь,
что у освобождения до 2978a71.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.scheduler.jobs import reclaim
from app.ws.presence import _status_key
from tests.unit.test_reclaim import make_conv, reload, run_reclaim

pytestmark = pytest.mark.anyio


async def test_an_online_owner_does_not_block_the_return_of_an_offline_ones_dialog(
    db_sessionmaker, redis, make_user, make_avito_account, monkeypatch
):
    monkeypatch.setattr(reclaim, "BATCH", 3)
    account = await make_avito_account()
    here = await make_user("here-st@leadchat.test", role="manager", full_name="На месте")
    gone = await make_user("gone-st@leadchat.test", role="manager", full_name="Ушёл")
    await redis.set(_status_key(here.id), "online")
    now = datetime.now(UTC)
    for i in range(5):
        await make_conv(
            db_sessionmaker,
            account.id,
            key=f"here-{i}",
            assignee_id=here.id,
            auto_assigned_at=now - timedelta(hours=2, minutes=i),
        )
    victim = await make_conv(
        db_sessionmaker,
        account.id,
        key="gone",
        assignee_id=gone.id,
        auto_assigned_at=now - timedelta(minutes=30),
    )

    await run_reclaim(db_sessionmaker, redis)

    assert (await reload(db_sessionmaker, victim.id)).assignee_id is None
