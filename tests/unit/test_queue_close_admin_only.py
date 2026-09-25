"""Диалог из очереди закрывает только администратор (правка владельца 22.08).

ЧТО СЛУЧИЛОСЬ. Владелец закрывал диалоги пачкой и обнаружил: диалог, который
висит во «Входящих» и ещё никем не взят, закрытие УБИРАЕТ ИЗ ОЧЕРЕДИ насовсем.
Условие очереди (`inbox.queue_condition`) требует `status != closed`, а матрица
переходов разрешает `new → closed` — то есть один случайный «Закрыть» тихо
уносит обращение, которому никто не ответил. Клиент при этом ждёт.

ЧТО РЕШИЛ ВЛАДЕЛЕЦ. «Во входящих можно только "Отклонить" — так они
возвращаются в очередь. Закрыть диалог из очереди тоже хорошая идея, но такое
нужно оставить только админам».

РАЗНИЦА МЕЖДУ ДВУМЯ ДЕЙСТВИЯМИ, и она принципиальная:
  • «Отклонить» — «я сейчас занят»: диалог уходит с глаз ТОГО, кто нажал, на
    три минуты и возвращается в общую очередь (`inbox.DECLINE_TTL`);
  • «Закрыть» — «с этим обращением покончено»: диалог исчезает у всех и не
    возвращается, пока клиент не напишет снова.

Оператору нужно первое. Второе оставлено администратору: он разбирает спам и
явный мусор, и он же отвечает за то, что обращение действительно не нужно.

ЗАПРЕТ ЖИВЁТ НА СЕРВЕРЕ, а не только в интерфейсе: спрятанная кнопка защищает
от случайного нажатия, но не от горячей клавиши, повторного запроса и чужого
клиента.
"""

from __future__ import annotations

import pytest

from app.core import rbac


def test_pravo_est_v_kataloge():
    assert "conversations:close_queued" in rbac.PERMISSIONS


def test_pravo_tolko_u_admina():
    """Именно «только админам» — слова владельца."""
    for роль in rbac.ROLES:
        есть = "conversations:close_queued" in rbac.ROLE_PERMISSIONS.get(роль, frozenset())
        assert есть == (роль == "admin"), f"{роль}: право закрывать из очереди — {есть}"


def test_ostalnye_prava_ne_tronuty():
    """Правка не должна попутно раздать или отнять что-то ещё."""
    # Оператор по-прежнему ведёт диалоги и пишет клиенту.
    assert "messages:send" in rbac.ROLE_PERMISSIONS["manager"]
    assert "conversations:manage" in rbac.ROLE_PERMISSIONS["manager"]
    # Руководитель по-прежнему не пишет клиенту.
    assert "messages:send" not in rbac.ROLE_PERMISSIONS["head"]
    # Наблюдатель по-прежнему только смотрит.
    assert "conversations:manage" not in rbac.ROLE_PERMISSIONS["observer"]


def test_zakrytie_ne_iz_ocheredi_ostayotsya_vsem():
    """Взятый в работу диалог закрывает тот, кто его ведёт, — как и раньше.

    Ограничение касается ТОЛЬКО очереди: обращения, которое никто не взял.
    Иначе оператор не смог бы закончить собственную работу.
    """
    assert "conversations:manage" in rbac.ROLE_PERMISSIONS["manager"]


# ── поведение: сервер действительно не даёт закрыть ожидающий диалог ─────────

import uuid  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.core.errors import ApiError  # noqa: E402
from app.models import AvitoAccount, Client, Conversation, User  # noqa: E402
from app.services import conversations as convs  # noqa: E402

T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


async def _мир(db, роль: str):
    acc = AvitoAccount(
        id=uuid.uuid4(),
        title="Канал",
        avito_user_id=1,
        status="active",
        webhook_secret="s",
        access_token_enc=b"e",
        refresh_token_enc=b"e",
        token_expires_at=T0 + timedelta(days=1),
    )
    cl = Client(id=uuid.uuid4(), name="Клиент", external_id="u-1")
    who = User(
        id=uuid.uuid4(),
        email=f"{роль}@x.ru",
        full_name=роль.title(),
        password_hash="x",
        role=роль,
        is_active=True,
    )
    db.add_all([acc, cl, who])
    await db.flush()
    # ждущий диалог: предложен очереди, никем не взят
    conv = Conversation(
        id=uuid.uuid4(),
        account_id=acc.id,
        client_id=cl.id,
        external_chat_id="c-1",
        status="new",
        offered_at=T0,
        bot_active=False,
        last_message_at=T0,
    )
    db.add(conv)
    await db.flush()
    return conv, who


@pytest.mark.asyncio
async def test_operator_ne_zakryvayet_ozhidayushchiy(db):
    """Главный случай владельца: обращение не должно исчезать из очереди."""
    conv, кто = await _мир(db, "manager")
    with pytest.raises(ApiError) as отказ:
        await convs.change_status(db, conv, new_status="closed", actor=кто)
    assert отказ.value.status == 403
    assert отказ.value.details.get("reason") == "queued_close_forbidden"
    assert "Отклонить" in отказ.value.message, "человеку не сказали, что делать вместо"


@pytest.mark.asyncio
async def test_administrator_zakryvayet(db):
    """Разбор спама и мусора остаётся возможным — это и была вторая половина
    решения владельца."""
    conv, кто = await _мир(db, "admin")
    await convs.change_status(db, conv, new_status="closed", actor=кто)
    assert conv.status == "closed"


@pytest.mark.asyncio
async def test_vzyatyy_v_rabotu_zakryvayet_kto_vedyot(db):
    """Ограничение касается ТОЛЬКО очереди: свою работу оператор заканчивает сам."""
    conv, кто = await _мир(db, "manager")
    conv.claimed_by_id = кто.id
    conv.status = "in_progress"
    await db.flush()
    await convs.change_status(db, conv, new_status="closed", actor=кто)
    assert conv.status == "closed"
