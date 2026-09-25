"""Пер-юзерные read-маркеры (01 §5.1/§5.3).

Главное свойство, ради которого маркеры и заводились: «прочитано» у каждого
своё. Руководитель, открывший чужой диалог, не гасит бейдж менеджеру.
"""

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Conversation, Message
from app.services import read_markers as rm
from tests.unit.conftest import drain_events


async def _add_message(
    db_sessionmaker,
    conversation_id: uuid.UUID,
    *,
    direction: str = "in",
    body: str = "текст",
    created_at: datetime | None = None,
) -> Message:
    async with db_sessionmaker() as session:
        msg = Message(
            conversation_id=conversation_id,
            external_message_id=f"am-{uuid.uuid4().hex[:8]}" if direction == "in" else None,
            direction=direction,
            sender_type="client" if direction == "in" else "operator",
            body=body,
            attachments=[],
            delivery_status="delivered",
            created_at=created_at or datetime.now(UTC),
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def _conversation(db_sessionmaker, conversation_id: uuid.UUID) -> Conversation:
    async with db_sessionmaker() as session:
        return (
            await session.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one()


@pytest.fixture
async def two_users(make_user):
    manager = await make_user("marker-manager@leadchat.test", role="manager")
    head = await make_user("marker-head@leadchat.test", role="head")
    return manager, head


# --------------------------------------------------------------- счётчики


async def test_no_marker_means_fallback(db, redis, seed_conversation, two_users):
    """Маркера нет — счётчик не считается вовсе (вызывающий берёт глобальный)."""
    manager, _ = two_users
    counts = await rm.unread_counts(db, redis, manager.id, [seed_conversation.conversation_id])
    assert counts == {}


async def test_two_users_have_independent_counters(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    """Один прочитал — второму бейдж остался (тот самый баг спринта 2)."""
    manager, head = two_users
    conv_id = seed_conversation.conversation_id
    conv = await _conversation(db_sessionmaker, conv_id)

    # менеджер открыл диалог: маркер уехал на последнее сообщение
    await rm.mark_read(db, redis, manager.id, conv)

    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 0}
    assert await rm.unread_counts(db, redis, head.id, [conv_id]) == {}  # у head маркера нет

    # прилетело новое входящее — непрочитанным оно стало у ОБОИХ
    await _add_message(db_sessionmaker, conv_id, body="ещё вопрос")
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 1}

    # head тоже открыл — у него 0, у менеджера по-прежнему 1
    await rm.mark_read(db, redis, head.id, await _conversation(db_sessionmaker, conv_id))
    assert await rm.unread_counts(db, redis, head.id, [conv_id]) == {conv_id: 0}
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 1}


async def test_only_inbound_messages_count(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    """Исходящие, заметки и системные записи непрочитанными не бывают."""
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    await rm.mark_read(db, redis, manager.id, await _conversation(db_sessionmaker, conv_id))

    await _add_message(db_sessionmaker, conv_id, direction="out", body="ответ")
    await _add_message(db_sessionmaker, conv_id, direction="note", body="внутренняя заметка")
    await _add_message(db_sessionmaker, conv_id, direction="system", body="Статус: ...")
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 0}

    await _add_message(db_sessionmaker, conv_id, direction="in", body="а можно ещё?")
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 1}


async def test_counts_only_messages_newer_than_marker(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    base = datetime.now(UTC)
    await rm.set_marker(redis, manager.id, conv_id, base)

    await _add_message(db_sessionmaker, conv_id, created_at=base - timedelta(minutes=5))
    await _add_message(db_sessionmaker, conv_id, created_at=base + timedelta(minutes=1))
    await _add_message(db_sessionmaker, conv_id, created_at=base + timedelta(minutes=2))

    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 2}


async def test_unread_for_conversation_falls_back_to_column(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    """Обратная совместимость: без маркера отдаём conversations.unread_count."""
    manager, _ = two_users
    conv = await _conversation(db_sessionmaker, seed_conversation.conversation_id)
    assert conv.unread_count == 1  # seed
    assert await rm.unread_for_conversation(db, redis, manager.id, conv) == 1

    await rm.mark_read(db, redis, manager.id, conv)
    assert await rm.unread_for_conversation(db, redis, manager.id, conv) == 0


# ------------------------------------------------------------ сериализация


async def test_apply_unread_counts_patches_items_per_user(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    """Точка интеграции роутера: правим уже сериализованные ConversationOut."""
    manager, head = two_users
    conv_id = seed_conversation.conversation_id
    conv = await _conversation(db_sessionmaker, conv_id)
    await rm.mark_read(db, redis, manager.id, conv)

    def items() -> list[dict]:
        return [{"id": str(conv_id), "unread_count": conv.unread_count}]

    for_manager = await rm.apply_unread_counts(db, redis, manager.id, items())
    for_head = await rm.apply_unread_counts(db, redis, head.id, items())

    assert for_manager[0]["unread_count"] == 0  # прочитал
    assert for_head[0]["unread_count"] == 1  # маркера нет → глобальный счётчик


async def test_apply_unread_counts_survives_foreign_dicts(db, redis, two_users):
    manager, _ = two_users
    items = [{"no_id_here": True}, {"id": "не-uuid", "unread_count": 7}]
    assert await rm.apply_unread_counts(db, redis, manager.id, items) == items


# ------------------------------------------------------------------ Redis


async def test_marker_never_moves_backwards(redis, seed_conversation, two_users):
    """Гонка «веб ↔ десктоп» не должна возвращать бейдж."""
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    later = datetime.now(UTC)
    earlier = later - timedelta(hours=1)

    await rm.set_marker(redis, manager.id, conv_id, later)
    stored = await rm.set_marker(redis, manager.id, conv_id, earlier)
    assert stored == later
    assert await rm.get_marker(redis, manager.id, conv_id) == later


class _LaggyPipeline:
    """Конвейер, у которого ОТВЕТ на чтение приезжает с задержкой."""

    def __init__(self, inner: Any, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def __aenter__(self) -> "_LaggyPipeline":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)

    async def hget(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.hget(*args, **kwargs)
        if not inspect.isawaitable(result):
            return result  # буферный режим (после MULTI) — команда только копится
        value = await result
        await asyncio.sleep(self._delay)
        return value


class _LaggyRead:
    """Redis, у которого чтение отвечает МЕДЛЕННО, а запись — сразу.

    Задержка стоит ПОСЛЕ настоящего чтения, а не до него: так воспроизводится
    то, что и происходит в сети, — значение снято с сервера рано, а до клиента
    доехало поздно, и решение принимается по устаревшему снимку. Задержка до
    чтения ничего бы не проверяла: второй запрос просто увидел бы уже
    записанное соседом.
    """

    def __init__(self, inner: Any, delay: float = 0.02) -> None:
        self._inner = inner
        self._delay = delay

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def hget(self, *args: Any, **kwargs: Any) -> Any:
        value = await self._inner.hget(*args, **kwargs)
        await asyncio.sleep(self._delay)
        return value

    def pipeline(self, *args: Any, **kwargs: Any) -> _LaggyPipeline:
        return _LaggyPipeline(self._inner.pipeline(*args, **kwargs), self._delay)


async def test_two_simultaneous_reads_never_move_the_marker_backwards(
    redis, seed_conversation, two_users
):
    """ДВЕ отметки прочтения ОДНОВРЕМЕННО — бейдж не имеет права вернуться.

    Жалоба с боевой: «непрочитанные висят без причины». Вот как это выходило.
    Оператор открывает диалог — уходит /read с временем T1. Пока запрос летит,
    клиент присылает сообщение (T2), открытый диалог обновляется и шлёт второй
    /read — уже с T2. Второй успевает записать T2, первый (он медленнее)
    дописывает поверх свой T1 — и непрочитанное возвращается само.

    То же самое даёт пара «веб + десктоп» одного человека вообще без нового
    сообщения: два клиента, два запроса, разные снимки.

    Проверка идёт через Redis с медленным ЧТЕНИЕМ: именно окно между «прочитал
    маркер» и «записал маркер» и есть вся беда. Проверить это без задержки
    нельзя — обе корутины успевали бы отработать целиком по очереди.
    """
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    later = datetime.now(UTC)
    earlier = later - timedelta(minutes=5)
    laggy = _LaggyRead(redis)

    await asyncio.gather(
        rm.set_marker(laggy, manager.id, conv_id, later),
        rm.set_marker(laggy, manager.id, conv_id, earlier),
    )

    assert await rm.get_marker(redis, manager.id, conv_id) == later


async def test_marker_key_shape_and_ttl(redis, seed_conversation, two_users):
    """`read:{user_id}` — хэш с TTL 90 дней (01 §5.1)."""
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    await rm.set_marker(redis, manager.id, conv_id, datetime.now(UTC))

    key = f"read:{manager.id}"
    assert await redis.type(key) == "hash"
    assert set(await redis.hkeys(key)) == {str(conv_id)}
    ttl = await redis.ttl(key)
    assert 0 < ttl <= settings.read_marker_ttl_days * 86400
    assert ttl > 89 * 86400  # ровно 90 дней, а не «на всякий случай час»


async def test_clear_marker_makes_conversation_unread_again(
    db, db_sessionmaker, redis, seed_conversation, two_users
):
    manager, _ = two_users
    conv_id = seed_conversation.conversation_id
    await rm.mark_read(db, redis, manager.id, await _conversation(db_sessionmaker, conv_id))
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {conv_id: 0}

    await rm.clear_marker(redis, manager.id, conv_id)
    assert await rm.unread_counts(db, redis, manager.id, [conv_id]) == {}


async def test_broken_marker_value_does_not_break_counting(redis, seed_conversation, two_users):
    """Мусор в хэше не должен ронять список диалогов."""
    manager, _ = two_users
    await redis.hset(f"read:{manager.id}", str(seed_conversation.conversation_id), "не дата")
    assert await rm.get_marker(redis, manager.id, seed_conversation.conversation_id) is None


async def test_prune_marker_hash_keeps_freshest(redis, two_users):
    manager, _ = two_users
    now = datetime.now(UTC)
    ids = [uuid.uuid4() for _ in range(5)]
    for offset, conv_id in enumerate(ids):
        await rm.set_marker(redis, manager.id, conv_id, now - timedelta(days=offset))

    removed = await rm.prune_marker_hash(redis, manager.id, keep=2)
    assert removed == 3
    assert set(await redis.hkeys(f"read:{manager.id}")) == {str(ids[0]), str(ids[1])}


async def test_mark_read_without_messages_uses_now(db, db_sessionmaker, redis, two_users):
    """Диалог без сообщений (служебный SMOKE-CONV) тоже отмечается прочитанным."""
    manager, _ = two_users
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=f"empty-{uuid.uuid4().hex[:8]}",
        account_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        status="new",
        unread_count=0,
        last_message_at=None,
    )
    at = await rm.mark_read(db, redis, manager.id, conv)
    assert at is not None
    assert await rm.get_marker(redis, manager.id, conv.id) == at


# ------------------------------------------------- проводка в HTTP-ручках
#
# Сервис маркеров бесполезен, пока его никто не зовёт: до этой проводки
# `POST /read` обнулял ГЛОБАЛЬНЫЙ счётчик и рассылал кадр всем — руководитель,
# заглянувший в чужую переписку, гасил бейдж менеджеру (01 §5.1/§5.3).


def _auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


async def test_read_by_one_user_does_not_clear_another_users_badge(
    client, tokens, redis, seed_conversation
):
    conv_id = seed_conversation.conversation_id
    assert (
        await client.post(f"/api/v1/conversations/{conv_id}/read", headers=_auth(tokens, "head"))
    ).status_code == 204

    head_list = (await client.get("/api/v1/conversations", headers=_auth(tokens, "head"))).json()
    head_row = next(i for i in head_list["items"] if i["id"] == str(conv_id))
    assert head_row["unread_count"] == 0  # руководитель прочитал — у него ноль

    mgr_list = (await client.get("/api/v1/conversations", headers=_auth(tokens))).json()
    mgr_row = next(i for i in mgr_list["items"] if i["id"] == str(conv_id))
    assert mgr_row["unread_count"] == 1  # менеджер диалог не открывал


async def test_read_does_not_zero_the_global_column(
    client, tokens, db_sessionmaker, seed_conversation
):
    """Колонка остаётся fallback'ом для тех, у кого маркера ещё нет."""
    conv_id = seed_conversation.conversation_id
    await client.post(f"/api/v1/conversations/{conv_id}/read", headers=_auth(tokens, "head"))
    async with db_sessionmaker() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None
        assert conv.unread_count == 1


async def test_read_publishes_only_to_the_reader(client, tokens, redis, seed_conversation):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    conv_id = seed_conversation.conversation_id
    await client.post(f"/api/v1/conversations/{conv_id}/read", headers=_auth(tokens, "head"))

    (frame,) = await drain_events(pubsub)
    assert frame["type"] == "conversation:updated"
    # адресный кадр: бейдж — состояние человека, гасим только его вкладки
    assert frame["meta"]["only_user"] is not None


async def test_new_incoming_message_returns_the_badge_after_read(
    client, tokens, db_sessionmaker, seed_conversation
):
    conv_id = seed_conversation.conversation_id
    await client.post(f"/api/v1/conversations/{conv_id}/read", headers=_auth(tokens))
    row = next(
        i
        for i in (await client.get("/api/v1/conversations", headers=_auth(tokens))).json()["items"]
        if i["id"] == str(conv_id)
    )
    assert row["unread_count"] == 0

    await _add_message(
        db_sessionmaker,
        conv_id,
        body="а ещё вопрос",
        created_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    row = next(
        i
        for i in (await client.get("/api/v1/conversations", headers=_auth(tokens))).json()["items"]
        if i["id"] == str(conv_id)
    )
    assert row["unread_count"] == 1


async def test_detail_reports_the_personal_unread_count(client, tokens, seed_conversation):
    conv_id = seed_conversation.conversation_id
    await client.post(f"/api/v1/conversations/{conv_id}/read", headers=_auth(tokens, "head"))
    head_detail = (
        await client.get(f"/api/v1/conversations/{conv_id}", headers=_auth(tokens, "head"))
    ).json()
    mgr_detail = (
        await client.get(f"/api/v1/conversations/{conv_id}", headers=_auth(tokens))
    ).json()
    assert head_detail["unread_count"] == 0
    assert mgr_detail["unread_count"] == 1
