"""Исключения из сторожа освобождения (просьба владельца 08.09).

⚠ ПРОСЬБА ДОСЛОВНО: «сейчас если я включу "Освобождать диалоги сотрудника,
который не в сети или «Отошёл» дольше 15 минут", то она работает на всех — я
хочу так, чтобы я мог добавлять исключения, у кого она не будет работать».

ЗАЧЕМ ЭТО ВАЖНО, А НЕ УДОБНО. Без списка выбор был двоичным: правило для всех
или правила нет вовсе. Владелец выбирал второе — сторож в бою стоял
выключенным, и диалоги отсутствующих не возвращались НИКОГДА. У части людей
отсутствие в системе и есть работа: они у клиента, на выезде, у телефона, и
диалог обязан дождаться их.

Главное здесь не «работает ли фильтр», а то, что исключение НЕ ОТМЕНЯЕТ правило
для остальных: в одном проходе один диалог обязан уехать, а соседний — остаться.
Проверка на одном человеке зеленела бы и у кода, который просто выключил
сторожа целиком.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import app_settings
from tests.unit.test_release_unavailable_0309 import (
    _пометить_недоступным,
    перечитать,
    прогон,
    сделать_диалог,
)

pytestmark = pytest.mark.anyio


async def _настроить(db_sessionmaker, *, включено: bool, исключения: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.RELEASE_UNAVAILABLE_ENABLED: включено,
                app_settings.RELEASE_UNAVAILABLE_EXEMPT: исключения,
            },
            user_id=None,
        )
        await s.commit()


async def test_исключённый_держит_диалог_а_соседний_нет(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Один проход, два одинаково отсутствующих человека, разный исход.

    ⚠ ДИВЕРСИЯ: убрать из `release_in_session` проверку
    `if владелец in неприкосновенные: continue` — тест краснеет: диалог
    исключённого тоже уезжает во «Входящие».
    """
    account = await make_avito_account()
    свой = await make_user("exempt@leadchat.test", role="manager", full_name="Выездной")
    чужой = await make_user("plain@leadchat.test", role="manager", full_name="Обычный")
    await _настроить(db_sessionmaker, включено=True, исключения=str(свой.id))

    сейчас = datetime.now(UTC)
    диалог_исключённого = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="exempt-wait",
        assignee_id=свой.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    диалог_обычного = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="plain-wait",
        assignee_id=чужой.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    for u in (свой, чужой):
        await _пометить_недоступным(redis, u.id, минут_назад=40, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)

    остался = await перечитать(db_sessionmaker, диалог_исключённого.id)
    уехал = await перечитать(db_sessionmaker, диалог_обычного.id)
    assert остался.assignee_id == свой.id, "диалог исключённого всё-таки забрали"
    assert уехал.assignee_id is None, (
        "диалог обычного сотрудника остался — значит проверка выключила сторожа целиком, "
        "а не сделала исключение"
    )


async def test_пустой_список_ничего_не_меняет(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Умолчание обязано сохранять прежнее поведение: пусто = исключений нет."""
    account = await make_avito_account()
    ушёл = await make_user("nobody@leadchat.test", role="manager", full_name="Ушедший")
    await _настроить(db_sessionmaker, включено=True, исключения="")

    сейчас = datetime.now(UTC)
    диалог = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="empty-wait",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=40, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)
    assert (await перечитать(db_sessionmaker, диалог.id)).assignee_id is None


async def test_мусор_в_списке_не_роняет_сторожа(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Строку правят руками и удаляют сотрудников — разбор обязан это пережить.

    ⚠ ОТКАЗ РАЗБОРА ЗДЕСЬ СТОИЛ БЫ ДОРОЖЕ САМОГО МУСОРА: сторож упал бы на
    первом же проходе, и диалоги отсутствующих перестали бы возвращаться у
    ВСЕЙ смены — молча, потому что упавшая джоба видна только в журнале.
    """
    account = await make_avito_account()
    свой = await make_user("ok@leadchat.test", role="manager", full_name="Выездной")
    await _настроить(
        db_sessionmaker,
        включено=True,
        исключения=f"  {свой.id} ; не-идентификатор,, ",
    )

    сейчас = datetime.now(UTC)
    диалог = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="dirty-wait",
        assignee_id=свой.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await _пометить_недоступным(redis, свой.id, минут_назад=40, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)
    assert (await перечитать(db_sessionmaker, диалог.id)).assignee_id == свой.id
