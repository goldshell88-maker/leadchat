"""Сторож освобождения не должен «застревать» на чужих старых диалогах (24.09).

⚠ ЖАЛОБА ВЛАДЕЛЬЦА: «сломалась функция „Освобождать диалоги сотрудника, который
не в сети или «Отошёл» дольше 15 минут“».

ЧТО БЫЛО В БОЮ. Пачка сторожа — `BATCH` самых старых открытых диалогов по ВСЕМ
владельцам, а исключённые и «на месте» отсеивались уже в цикле. Исключённые
держат диалоги сколько угодно, поэтому их старые диалоги заняли всю пачку: 100
из 223 кандидатов — и все 100 у исключённых. Диалоги остальных сторож не видел,
а их владельцам даже не ставил отметку «недоступен с» — отсчёт 15 минут для них
не начинался вовсе.

ДИВЕРСИИ (каждая красит свой тест):
* вернуть выборку «пачка по всем владельцам, фильтр в цикле» — краснеют все три;
* ставить отметки только владельцам из пачки — краснеет
  `test_отметка_ставится_и_тем_кого_нет_в_пачке`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import Conversation
from app.scheduler.jobs import reclaim
from app.services import app_settings
from app.ws import presence
from app.ws.presence import _status_key
from tests.unit.test_release_unavailable_0309 import (
    ДАВНО,
    _пометить_недоступным,
    перечитать,
    прогон,
    сделать_диалог,
)

pytestmark = pytest.mark.anyio

#: Маленькая пачка вместо боевых 100 — чтобы «забить» её тремя диалогами.
ПАЧКА = 3


@pytest.fixture(autouse=True)
def _маленькая_пачка(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reclaim, "BATCH", ПАЧКА)


async def _исключить(db_sessionmaker, *ids) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.RELEASE_UNAVAILABLE_ENABLED: True,
                app_settings.RELEASE_UNAVAILABLE_EXEMPT: ", ".join(str(i) for i in ids),
            },
            user_id=None,
        )
        await s.commit()


async def _состарить(db_sessionmaker, ids, *, на_часов: int) -> None:
    """Эти диалоги старше остальных — встают первыми в порядке `last_message_at`."""
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id.in_(ids))
            .values(last_message_at=ДАВНО - timedelta(hours=на_часов))
        )
        await s.commit()


async def test_исключённый_со_старыми_диалогами_не_заслоняет_ушедшего(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Пачка забита старыми диалогами исключённого — ровно как в бою 24.09. Диалог
    обычного сотрудника, отсутствующего 40 минут, обязан освободиться."""
    account = await make_avito_account()
    выездной = await make_user("field@leadchat.test", role="manager", full_name="Выездной")
    ушёл = await make_user("gone@leadchat.test", role="manager", full_name="Ушедший")
    await _исключить(db_sessionmaker, выездной.id)
    сейчас = datetime.now(UTC)
    старые = [
        await сделать_диалог(
            db_sessionmaker,
            account.id,
            key=f"field-{i}",
            assignee_id=выездной.id,
            awaiting_since=сейчас - timedelta(minutes=5),
        )
        for i in range(ПАЧКА + 2)
    ]
    await _состарить(db_sessionmaker, [c.id for c in старые], на_часов=5)
    диалог_ушедшего = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="gone-wait",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    for u in (выездной, ушёл):
        await _пометить_недоступным(redis, u.id, минут_назад=40, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)

    assert (await перечитать(db_sessionmaker, диалог_ушедшего.id)).assignee_id is None, (
        "диалог ушедшего остался за ним: пачку заняли старые диалоги исключённого"
    )
    for c in старые:
        assert (await перечитать(db_sessionmaker, c.id)).assignee_id == выездной.id


async def test_работающий_со_старыми_диалогами_не_заслоняет_ушедшего(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Тот же затор, но пачку держит тот, кто на месте: его диалоги не трогаются,
    а диалог отсутствующего — освобождается."""
    account = await make_avito_account()
    на_месте = await make_user("here@leadchat.test", role="manager", full_name="На месте")
    ушёл = await make_user("away@leadchat.test", role="manager", full_name="Отошёл")
    await _исключить(db_sessionmaker)
    сейчас = datetime.now(UTC)
    старые = [
        await сделать_диалог(
            db_sessionmaker,
            account.id,
            key=f"here-{i}",
            assignee_id=на_месте.id,
            awaiting_since=сейчас - timedelta(minutes=5),
        )
        for i in range(ПАЧКА + 2)
    ]
    await _состарить(db_sessionmaker, [c.id for c in старые], на_часов=5)
    диалог_ушедшего = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="away-wait",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await redis.set(_status_key(на_месте.id), presence.ONLINE)
    await redis.set(_status_key(ушёл.id), presence.AWAY)
    await _пометить_недоступным(redis, ушёл.id, минут_назад=40, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)

    assert (await перечитать(db_sessionmaker, диалог_ушедшего.id)).assignee_id is None
    for c in старые:
        assert (await перечитать(db_sessionmaker, c.id)).assignee_id == на_месте.id


async def test_отметка_ставится_и_тем_кого_нет_в_пачке(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Отсчёт 15 минут начинается с первого прохода, увидевшего отсутствие, — у
    ВСЕХ владельцев, а не только у тех, чьи диалоги попали в пачку. Иначе
    отсутствующий с «молодыми» диалогами не накопил бы порога никогда."""
    account = await make_avito_account()
    на_месте = await make_user("busy@leadchat.test", role="manager", full_name="Занятой")
    ушёл = await make_user("left@leadchat.test", role="manager", full_name="Ушёл")
    await _исключить(db_sessionmaker)
    сейчас = datetime.now(UTC)
    старые = [
        await сделать_диалог(db_sessionmaker, account.id, key=f"busy-{i}", assignee_id=на_месте.id)
        for i in range(ПАЧКА + 2)
    ]
    await _состарить(db_sessionmaker, [c.id for c in старые], на_часов=5)
    await сделать_диалог(db_sessionmaker, account.id, key="left-1", assignee_id=ушёл.id)
    await redis.set(_status_key(на_месте.id), presence.ONLINE)

    await прогон(db_sessionmaker, redis, now=сейчас)

    отметка = await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=ушёл.id))
    assert отметка is not None, "отсутствующему не поставили отметку: его диалогов нет в пачке"
