"""Служебные записи Авито можно убрать из ленты — но не из базы.

Требование владельца от 14 августа: «сделай так, чтобы можно было отключить все
системные сообщения в диалоге, которые отправляет Авито, так как в самой Jivo их
нет». На его снимках это «Ассистент Авито ответил…», «Пользователь создал чат, но
пока ничего не написал».

⚠ ДВА ВИДА, И ЛОВИТЬ НАДО ОБА. Первый система уже различает: `sender_type='avito'` —
Авито сказал о служебности КОНВЕРТОМ, и лента рисует такой пузырь серым чипом.
Второй приезжает ОБЫЧНЫМ СООБЩЕНИЕМ КЛИЕНТА, и признака служебности у него нет
вовсе: `is_system` ставится только по конверту, внутри переписки Авито его не
выставляет никогда. Выдаёт такие записи только приставка, которую пишет сам Авито.

⚠ И ГЛАВНОЕ: ПРЯЧЕМ, А НЕ УДАЛЯЕМ. За частью этих записей стоит действие клиента
(«пользователь создал чат»). Потерять их из-за настройки ПОКАЗА нельзя — выключатель
обязан возвращать ленту целиком.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.message import Message
from app.services import app_settings
from app.services import conversations as convs

pytestmark = pytest.mark.anyio


async def _лента(db, conv, **kw):
    return await convs.list_messages(db, conv, **kw)


def _сообщение(conv_id, *, body, sender_type="client", direction="in", сдвиг=0):
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        direction=direction,
        sender_type=sender_type,
        body=body,
        attachments=[],
        delivery_status="delivered",
        created_at=datetime.now(UTC) + timedelta(seconds=сдвиг),
    )


class TestСлужебныеЗаписиАвито:
    async def test_по_умолчанию_видны_все(self, db_sessionmaker, seed_conversation):
        """Умолчание сохраняет прежнее поведение: настройка меняет ленту у всех разом."""
        conv_id = seed_conversation.conversation_id
        async with db_sessionmaker() as db:
            db.add_all(
                [
                    _сообщение(conv_id, body="Не морозит холодильник", сдвиг=0),
                    _сообщение(
                        conv_id,
                        body="[Системное сообщение] Пользователь создал чат",
                        сдвиг=1,
                    ),
                    _сообщение(
                        conv_id,
                        body="Принято",
                        sender_type="avito",
                        direction="system",
                        сдвиг=2,
                    ),
                ]
            )
            await db.commit()
            свежий = await convs.get_conversation(db, conv_id)
            лента = await _лента(db, свежий)
            # +1 входящее из seed_conversation
            assert len(лента["items"]) == 4

    async def test_выключатель_убирает_оба_вида(self, db_sessionmaker, seed_conversation):
        conv_id = seed_conversation.conversation_id
        async with db_sessionmaker() as db:
            db.add_all(
                [
                    _сообщение(conv_id, body="Не морозит холодильник", сдвиг=0),
                    _сообщение(
                        conv_id,
                        body="[Системное сообщение] Пользователь создал чат",
                        сдвиг=1,
                    ),
                    _сообщение(
                        conv_id, body="Принято", sender_type="avito", direction="system", сдвиг=2
                    ),
                ]
            )
            await db.commit()
            свежий = await convs.get_conversation(db, conv_id)
            лента = await _лента(db, свежий, include_avito_system=False)

        тексты = [i["body"] for i in лента["items"]]
        служебные = [t for t in тексты if t and "Системное сообщение" in t]
        assert not служебные and "Принято" not in тексты, (
            f"в ленте осталось лишнее: {тексты}. Ловить надо ОБА вида — "
            "и помеченный конвертом, и приехавший обычным сообщением с приставкой"
        )

    async def test_записи_остаются_в_базе(self, db_sessionmaker, seed_conversation):
        """⚠ Выключатель возвращает ленту целиком: ничего не удалено."""
        conv_id = seed_conversation.conversation_id
        async with db_sessionmaker() as db:
            db.add(_сообщение(conv_id, body="[Системное сообщение] Пользователь создал чат"))
            await db.commit()
            свежий = await convs.get_conversation(db, conv_id)
            assert len(await _лента(db, свежий, include_avito_system=False)) is not None
            вернули = await _лента(db, свежий, include_avito_system=True)
        assert any("Системное сообщение" in (i["body"] or "") for i in вернули["items"]), (
            "запись пропала из базы, а не спряталась"
        )

    async def test_настройка_объявлена_и_по_умолчанию_показывает(self, db_sessionmaker):
        async with db_sessionmaker() as db:
            assert await app_settings.get(db, app_settings.AVITO_SYSTEM_HIDDEN) is False
