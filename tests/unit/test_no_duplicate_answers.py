"""Клиент не получает один и тот же ответ дважды (#25).

ЧТО БЫЛО НАПИСАНО И ОКАЗАЛОСЬ НЕПРАВДОЙ. В трёх местах кода стоял комментарий
«идемпотентность исходящих — второй эшелон в БД»: уникальный индекс по
(conversation_id, client_message_id, created_at). Он не ловил ни одного дубля
и не мог: `created_at` входит в ключ по требованию партиционирования, а у
повторной попытки время ДРУГОЕ — значит тройка снова уникальна.

ГДЕ БЫЛА ДЫРА В ЕДИНСТВЕННОЙ РАБОТАВШЕЙ ЗАЩИТЕ. Ключ в Redis имеет ветку
восстановления: «ключ есть, а сообщения по нему нет — значит прошлая попытка
умерла». Она не умеет отличить «умерла» от «ещё летит». Две одновременные
отправки с одним идентификатором дают ровно её.

Здесь проверяется не наличие механизма, а его поведение в гонке.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models import Client, Conversation, Message, MessageIdempotency
from app.services import messages as msgs

pytestmark = pytest.mark.anyio


@pytest.fixture
async def conversation(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client_row = Client(channel="avito", external_id=f"dup-{uuid.uuid4()}", name="Иван")
        s.add(client_row)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-dup-{uuid.uuid4()}",
            account_id=account.id,
            client_id=client_row.id,
            status="in_progress",
        )
        s.add(conv)
        await s.commit()
        return conv.id


async def _count_messages(db_sessionmaker, conv_id) -> int:
    async with db_sessionmaker() as s:
        return await s.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == conv_id)
        )


async def test_duplicate_is_blocked_when_the_first_line_fails(
    db_sessionmaker, redis, conversation, users_by_role, monkeypatch
) -> None:
    """ГЛАВНАЯ ПРОВЕРКА ФАЙЛА: дубль не проходит, когда первый рубеж промахнулся.

    ЧЕСТНО О ТОМ, ЧТО ЗДЕСЬ ВОСПРОИЗВОДИТСЯ. Настоящую гонку двух запросов —
    когда вторая попытка стартует до коммита первой — в юнит-тесте не собрать:
    создание сообщения коммитит транзакцию внутри себя. Поэтому мы
    воспроизводим не сам забег, а ЕГО ИСХОД: ветка восстановления ключа решила,
    что прошлая попытка умерла, и выдала новый идентификатор. Именно это она и
    делает в гонке, и именно здесь раньше рождался второй ответ клиенту.

    Первая версия этого теста проходила и на коде БЕЗ второго эшелона — то
    есть не проверяла ничего. Проверено обратным прогоном: без эшелона этот
    вариант падает.
    """
    cmid = "one-and-the-same"
    user = users_by_role["manager"]

    async with db_sessionmaker() as s:
        first = await msgs.create_outbound_message(
            s,
            redis,
            conversation_id=conversation,
            user=user,
            text="Перезвоню в течение часа",
            client_message_id=cmid,
        )

    # Ключ «потерялся»: ровно так ведёт себя ветка восстановления, когда
    # сообщения по ключу ещё не видно.
    real = msgs.acquire_idempotency

    async def as_if_previous_attempt_died(redis_, db_, conv_id, cmid_, text_):
        return msgs.IdempotencyOutcome(message_id=uuid.uuid4(), existing=None)

    monkeypatch.setattr(msgs, "acquire_idempotency", as_if_previous_attempt_died)

    async with db_sessionmaker() as s:
        again = await msgs.create_outbound_message(
            s,
            redis,
            conversation_id=conversation,
            user=user,
            text="Перезвоню в течение часа",
            client_message_id=cmid,
        )

    monkeypatch.setattr(msgs, "acquire_idempotency", real)

    assert again.replay is True, "вторая попытка обязана вернуть уже созданное"
    assert again.message.id == first.message.id
    assert await _count_messages(db_sessionmaker, conversation) == 1


async def test_second_echelon_holds_without_redis(
    db_sessionmaker, redis, conversation, users_by_role
) -> None:
    """Дубль не проходит даже когда Redis потерял ключ.

    Это и есть смысл «второго эшелона»: первый рубеж живёт в памяти, которая
    может быть очищена, перезапущена или недоступна. Прежний эшелон такого
    не выдерживал — он не срабатывал вообще никогда.
    """
    cmid = "redis-lost-the-key"
    user = users_by_role["manager"]

    async with db_sessionmaker() as s:
        await msgs.create_outbound_message(
            s,
            redis,
            conversation_id=conversation,
            user=user,
            text="Здравствуйте",
            client_message_id=cmid,
        )

    await redis.flushdb()  # ключа больше нет — первый рубеж пуст

    async with db_sessionmaker() as s:
        again = await msgs.create_outbound_message(
            s,
            redis,
            conversation_id=conversation,
            user=user,
            text="Здравствуйте",
            client_message_id=cmid,
        )

    assert again.replay is True
    assert await _count_messages(db_sessionmaker, conversation) == 1


async def test_echelon_row_is_written(db_sessionmaker, redis, conversation, users_by_role) -> None:
    """Запись эшелона появляется вместе с сообщением, а не отдельным шагом.

    Отдельный шаг означал бы окно, в котором сообщение уже есть, а защиты ещё
    нет, — то есть ровно тот дубль, от которого защищаемся.
    """
    cmid = "echelon-row"
    async with db_sessionmaker() as s:
        created = await msgs.create_outbound_message(
            s,
            redis,
            conversation_id=conversation,
            user=users_by_role["manager"],
            text="Текст",
            client_message_id=cmid,
        )

    async with db_sessionmaker() as s:
        row = await s.get(MessageIdempotency, (conversation, cmid))
    assert row is not None
    assert row.message_id == created.message.id


async def test_different_ids_are_two_messages(
    db_sessionmaker, redis, conversation, users_by_role
) -> None:
    """Обратная сторона: защита не должна съедать РАЗНЫЕ сообщения.

    Оператор пишет клиенту два сообщения подряд — это норма, а не дубль.
    """
    user = users_by_role["manager"]
    for cmid, text in (("first", "Здравствуйте"), ("second", "Уточните модель")):
        async with db_sessionmaker() as s:
            await msgs.create_outbound_message(
                s,
                redis,
                conversation_id=conversation,
                user=user,
                text=text,
                client_message_id=cmid,
            )

    assert await _count_messages(db_sessionmaker, conversation) == 2
