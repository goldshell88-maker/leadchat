"""Сторож «освободить диалоги того, кого нет за столом» стал выключаемым (06.09).

⚠ ПРОСЬБА ВЛАДЕЛЬЦА ДОСЛОВНО: «у нас есть функция, которая передаёт диалоги,
когда сотрудник не активен, нужно сделать её выключаемой».

Образец — `distribution.enabled`, один в один: ключ в реестре
`app_settings.SPECS`, чтение из базы на каждом проходе без кэша, поле в той же
ручке `/settings/distribution` под тем же правом и с тем же журналом
«было/стало».

Главное здесь не «выключается ли», а ГДЕ стоит проверка: после отметок
присутствия, а не перед выборкой. Отметки «недоступен с» обязаны жить и при
выключенном стороже, иначе первый проход после включения судит по мусору
(см. `test_включение_судит_по_настоящему_времени_отсутствия`).

Соседние сторожа — `check_awaiting` (jobs/awaiting.py) и `reclaim_abandoned` —
этот выключатель НЕ трогает: они забирают диалоги по другим правилам.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import AuditLog
from app.scheduler.jobs import reclaim
from app.services import app_settings
from app.services import inbox as inbox_svc
from app.ws import presence
from app.ws.presence import _status_key
from tests.unit.test_release_unavailable_0309 import (
    _пометить_недоступным,
    перечитать,
    прогон,
    сделать_диалог,
)

pytestmark = pytest.mark.anyio

URL = "/api/v1/settings/distribution"
КЛЮЧ = app_settings.RELEASE_UNAVAILABLE_ENABLED


async def _выставить(db_sessionmaker, включено: bool) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {КЛЮЧ: включено}, user_id=None)
        await s.commit()


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ============================================================ выключатель в стороже


async def test_выключенный_сторож_ничего_не_освобождает(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Оба исхода — и возврат во «Входящие», и закрытие — при выключенном
    стороже не наступают, сколько бы человек ни отсутствовал.

    ⚠ ДИВЕРСИЯ: убрать из `release_in_session` строки
    `if not await app_settings.get(db, RELEASE_UNAVAILABLE_ENABLED): return []`
    — тест краснеет: оба диалога освобождены.
    """
    await _выставить(db_sessionmaker, False)
    account = await make_avito_account()
    ушёл = await make_user("off1@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    ждёт = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="off-wait",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    отвечен = await сделать_диалог(
        db_sessionmaker, account.id, key="off-answered", assignee_id=ушёл.id, awaiting_since=None
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=60, now=сейчас)

    assert await прогон(db_sessionmaker, redis, now=сейчас) == []
    for conv_id in (ждёт.id, отвечен.id):
        после = await перечитать(db_sessionmaker, conv_id)
        assert после.assignee_id == ушёл.id, "сторож выключен, а диалог отобран"
        assert после.status != inbox_svc.CLOSED, "сторож выключен, а диалог закрыт"


async def test_умолчание_включено_сторож_работает_как_до_выключателя(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Строки в `app_settings` нет — сторож работает. В бою он освобождает
    диалоги с 03.09 (к 06.09 — 84 возврата и 73 автозакрытия), и выкатка
    выключателя не должна молча его остановить.

    ⚠ ДИВЕРСИЯ: поменять умолчание `Spec(..., "bool", True)` на `False` — тест
    краснеет на первом же утверждении, а без него — на кадрах.
    """
    assert app_settings.SPECS[КЛЮЧ].default is True

    account = await make_avito_account()
    ушёл = await make_user("dflt@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="dflt",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    кадры = await прогон(db_sessionmaker, redis, now=сейчас)
    assert len(кадры) == 1
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id is None
    assert inbox_svc.is_waiting(после)


async def test_при_выключенном_стороже_отметки_присутствия_живут(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Отметка «недоступен с» ставится ушедшему и снимается вернувшемуся,
    хотя сторож выключен: она — отсчёт, и замирать на время выключения ему
    нельзя (чем это кончается — в следующем тесте).

    ⚠ ДИВЕРСИЯ: перенести проверку настройки в начало `release_in_session`,
    перед выборку кандидатов, — тест краснеет: отметка не поставлена.
    """
    await _выставить(db_sessionmaker, False)
    account = await make_avito_account()
    ушёл = await make_user("mark@leadchat.test", role="manager", full_name="Ушедший")
    старт = datetime.now(UTC)
    await сделать_диалог(db_sessionmaker, account.id, key="mark", assignee_id=ушёл.id)

    assert await прогон(db_sessionmaker, redis, now=старт) == []
    сырое = await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=ушёл.id))
    assert сырое is not None, "сторож выключен — и отсчёт отсутствия не начался"
    assert datetime.fromisoformat(сырое) == старт

    await redis.set(_status_key(ушёл.id), presence.ONLINE)
    assert await прогон(db_sessionmaker, redis, now=старт + timedelta(minutes=1)) == []
    assert await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=ушёл.id)) is None, (
        "вернулся, а отметка осталась: после включения зачтётся отлучка, которой нет"
    )


async def test_включение_судит_по_настоящему_времени_отсутствия(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Человек ушёл, пока сторож был выключен. Включили — и решение принимается
    по времени С ЕГО УХОДА, а не с момента включения: кто давно ушёл —
    освобождается, кто отошёл только что — нет.

    ⚠ ДИВЕРСИЯ: перенести проверку настройки перед выборку — отметка встанет
    только первым проходом после включения (T+10), к T+16 «отсутствие» будет
    шесть минут, и тест краснеет на `len(кадры) == 1`.
    """
    await _выставить(db_sessionmaker, False)
    account = await make_avito_account()
    ушёл = await make_user("late@leadchat.test", role="manager", full_name="Ушедший")
    T0 = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="late",
        assignee_id=ушёл.id,
        awaiting_since=T0 - timedelta(minutes=5),
    )

    # Выключен: проходы идут, отметка ставится, диалог на месте.
    assert await прогон(db_sessionmaker, redis, now=T0) == []
    assert await прогон(db_sessionmaker, redis, now=T0 + timedelta(minutes=10)) == []
    assert (await перечитать(db_sessionmaker, conv.id)).assignee_id == ушёл.id

    await _выставить(db_sessionmaker, True)
    # Включили на десятой минуте — порог ещё не наступил, отбирать рано.
    assert await прогон(db_sessionmaker, redis, now=T0 + timedelta(minutes=10)) == []
    # Шестнадцатая минута С УХОДА, а не с включения.
    кадры = await прогон(db_sessionmaker, redis, now=T0 + timedelta(minutes=16))
    assert len(кадры) == 1, "включение обнулило отсчёт: судит с момента включения"
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id is None
    assert inbox_svc.is_waiting(после)


# ============================================================ ручка


async def test_ручка_отдаёт_и_принимает_выключатель(client, tokens, db_sessionmaker):
    """Поле живёт в той же ручке, что автораздача: третье решение о том, у кого
    лежит диалог, а не отдельный раздел с отдельным правом.

    ⚠ ДИВЕРСИЯ: убрать `release_unavailable` из `_view` в `routes/settings.py`
    — тест краснеет на первом утверждении (в ответе нет поля).
    """
    headers = hdr(tokens["admin"])
    res = await client.get(URL, headers=headers)
    assert res.status_code == 200
    assert res.json()["release_unavailable"] is True

    res = await client.patch(URL, json={"release_unavailable": False}, headers=headers)
    assert res.status_code == 200
    assert res.json()["release_unavailable"] is False
    # Соседи не тронуты: пачка пишется по полям, а не целиком.
    assert res.json()["enabled"] is False

    assert (await client.get(URL, headers=headers)).json()["release_unavailable"] is False
    # И сторож прочтёт то же самое — из базы, без кэша.
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, КЛЮЧ) is False


async def test_выключение_не_анонимно_а_повтор_не_засоряет_журнал(
    client, tokens, users_by_role, db_sessionmaker
):
    """«Почему диалоги ушедших со вчера висят за ними» обязан иметь ответ с
    именем и временем; повторное сохранение того же — нет.

    ⚠ ДИВЕРСИЯ: убрать поле из `_view` — «было» и «стало» совпадут, записи в
    журнале не будет, и тест краснеет на `len(rows) == 1`.
    """
    headers = hdr(tokens["admin"])
    await client.patch(URL, json={"release_unavailable": False}, headers=headers)
    # Повтор того же — no-op, без записи.
    await client.patch(URL, json={"release_unavailable": False}, headers=headers)

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.distribution_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == users_by_role["admin"].id
    assert rows[0].details["before"]["release_unavailable"] is True
    assert rows[0].details["after"]["release_unavailable"] is False
