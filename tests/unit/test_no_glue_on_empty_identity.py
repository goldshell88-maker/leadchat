"""Служебные события Авито не должны склеивать посторонних людей.

ЧТО СЛУЧИЛОСЬ НА БОЕВОЙ СИСТЕМЕ 12 АВГУСТА. 11-го включили две вещи разом:
загрузку ВСЕЙ истории канала и показ служебных событий Авито. У служебного
события автора нет, а путь переписки заводит клиента ПО АВТОРУ — `str(None)`
и `str(0)` давали "None" и "0", и отбор по паре `channel + external_id` находил
первую такую карточку.

Итог: ОДНА карточка с чужим именем держала ВОСЕМЬ диалогов из восьми городов и
с обоих каналов, и вдобавок показывала подпись «возможно, этот же человек писал
и на другой наш канал». Оператор, открыв любой из этих диалогов, видел не того
человека и восемь чужих обращений в истории.

ТРИ РУБЕЖА, И КАЖДЫЙ ПРОВЕРЯЕТСЯ ЗДЕСЬ:
1. разбор истории помечает служебное событие `is_system` — оно вообще не идёт
   путём переписки (до 12 августа помечал только вебхук);
2. клиент по пустому автору не ищется: запасная личность — сам чат;
3. карточка с неизвестной личностью не получает межканальной подписи.

Проверка ломанием: снимите `is_system` в разборе истории — падает
``test_history_system_event_does_not_create_a_person``; верните
``external_id = str(event.author_id)`` — падает
``test_two_authorless_chats_stay_two_people``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.integrations.avito.adapter import AvitoAdapter, ChatInfo, InboundEvent
from app.models import Client, Conversation


class _FakeRedis:
    async def publish(self, *_a: Any, **_kw: Any) -> int:
        return 0

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        return True

    async def get(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def delete(self, *_a: Any) -> int:
        return 1

    async def incr(self, *_a: Any, **_kw: Any) -> int:
        return 1

    async def expire(self, *_a: Any, **_kw: Any) -> bool:
        return True


def _chat(chat_id: str) -> ChatInfo:
    return ChatInfo(
        external_chat_id=chat_id,
        client_external_id=None,
        client_name=None,
        item_title="Ремонт телевизоров",
        item_url="https://avito.ru/abakan/predlozheniya_uslug/remont_1234567",
        item_price=None,
        unread_count=0,
        has_unread=False,
        last_message_at=None,
    )


# --- рубеж 1: разбор истории ---------------------------------------------------


def test_history_system_event_is_marked_system() -> None:
    """Служебное событие из истории обязано нести признак служебного.

    Именно этого не было: вебхук помечал, история — нет, и «Пользователь
    посмотрел номер из объявления» ложилось обычным входящим от клиента.
    """
    event = AvitoAdapter.normalize_history_message(
        {
            "id": "m-1",
            "author_id": 0,
            "created": 1754900000,
            "type": "system",
            "content": {"text": "Пользователь посмотрел номер из объявления"},
        },
        chat=_chat("u2i-abc"),
        account_user_id=111222333,
    )
    assert event.is_system is True


def test_history_real_message_is_not_marked_system() -> None:
    """Обычное сообщение служебным не становится — ошибка в эту сторону дороже:
    сообщение клиента легло бы серым чипом без звука и без очереди."""
    event = AvitoAdapter.normalize_history_message(
        {
            "id": "m-2",
            "author_id": 987654,
            "created": 1754900001,
            "type": "text",
            "content": {"text": "Здравствуйте, сколько стоит ремонт?"},
        },
        chat=_chat("u2i-abc"),
        account_user_id=111222333,
    )
    assert event.is_system is False


def test_unknown_source_type_is_treated_as_conversation() -> None:
    """Незнакомый вид — переписка, а не служебное: перечня видов у нас нет."""
    event = AvitoAdapter.normalize_history_message(
        {
            "id": "m-3",
            "author_id": 987654,
            "created": 1754900002,
            "type": "невиданный_вид",
            "content": {"text": "текст"},
        },
        chat=_chat("u2i-abc"),
        account_user_id=111222333,
    )
    assert event.is_system is False


# --- рубеж 2: клиент не склеивается по пустому автору --------------------------


async def test_two_authorless_chats_stay_two_people(
    db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Два чата без автора — два разных клиента, а не один на всех.

    Это тот самый случай: восемь таких чатов слиплись в одну карточку.
    """
    from app.services import inbound as inbound_svc

    account = await make_avito_account(avito_user_id=661122334)
    for n, chat_id in enumerate(("u2i-one", "u2i-two"), start=1):
        event = InboundEvent(
            external_chat_id=chat_id,
            external_message_id=f"m-{n}",
            author_id=0,  # автора нет — ровно как у служебного события
            account_user_id=account.avito_user_id,
            text=f"Здравствуйте {n}",
            created_at=datetime.now(UTC),
        )
        async with db_sessionmaker() as db:
            await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)

    async with db_sessionmaker() as db:
        clients = (
            (await db.execute(select(Client).where(Client.external_id.like("chat:%"))))
            .scalars()
            .all()
        )
        assert len(clients) == 2, "чаты без автора обязаны остаться разными людьми"
        assert {c.external_id for c in clients} == {"chat:u2i-one", "chat:u2i-two"}
        for c in clients:
            convs = (
                (await db.execute(select(Conversation).where(Conversation.client_id == c.id)))
                .scalars()
                .all()
            )
            assert len(convs) == 1, "на карточке без личности не может быть двух диалогов"


async def test_a_real_author_still_glues_his_own_chats(
    db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Настоящий автор по-прежнему собирает свои чаты в одну карточку —
    иначе починка сломала бы то, ради чего склейка и существует."""
    from app.services import inbound as inbound_svc

    account = await make_avito_account(avito_user_id=662233445)
    for n, chat_id in enumerate(("u2i-real-a", "u2i-real-b"), start=1):
        event = InboundEvent(
            external_chat_id=chat_id,
            external_message_id=f"r-{n}",
            author_id=555444333,
            account_user_id=account.avito_user_id,
            text="Здравствуйте",
            created_at=datetime.now(UTC),
            client_name="Иван",
        )
        async with db_sessionmaker() as db:
            await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)

    async with db_sessionmaker() as db:
        client = (
            await db.execute(select(Client).where(Client.external_id == "555444333"))
        ).scalar_one()
        convs = (
            (await db.execute(select(Conversation).where(Conversation.client_id == client.id)))
            .scalars()
            .all()
        )
        assert len(convs) == 2


# --- рубеж 3: неизвестная личность не получает межканальной подписи ------------


async def test_unknown_identity_never_gets_cross_account_note(
    db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Карточка, заведённая по чату, не подписывается «тот же человек».

    На боевом восемь посторонних получили именно такую подпись.
    """
    from app.services import inbound as inbound_svc

    first = await make_avito_account(avito_user_id=663344556, webhook_secret="w1")
    second = await make_avito_account(avito_user_id=664455667, webhook_secret="w2")
    for account, chat_id in ((first, "u2i-x"), (second, "u2i-y")):
        event = InboundEvent(
            external_chat_id=chat_id,
            external_message_id=f"s-{uuid.uuid4().hex[:6]}",
            author_id=0,
            account_user_id=account.avito_user_id,
            text="Здравствуйте",
            created_at=datetime.now(UTC),
        )
        async with db_sessionmaker() as db:
            await inbound_svc.apply_inbound_event(db, _FakeRedis(), account, event)

    async with db_sessionmaker() as db:
        for c in (
            (await db.execute(select(Client).where(Client.external_id.like("chat:%"))))
            .scalars()
            .all()
        ):
            assert c.cross_account_since is None
            assert c.link_confidence is None
