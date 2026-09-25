"""Служебные сообщения Авито видно в ленте, и они ничего не будят.

ЧТО БЫЛО. Событие с ``payload.type != "message"`` адаптер размечал
``kind="system"``, а воркер такое ack'ал и выбрасывал: до ленты не доезжало
ничего — ни строки, ни следа, только сырец в ``webhook_raw_log``, куда никто из
тринадцати диспетчеров не ходит. Владелец увидел это как «нет подсветки
системных сообщений от Авито».

ГЛАВНЫЙ РИСК ЭТОЙ РАБОТЫ НЕ В ТОМ, ЧТО ЧИП НЕ ПОКАЖЕТСЯ. Он в том, что
служебная запись начнёт вести себя как сообщение клиента: раздует бейдж
непрочитанных, поставит диалог в «клиент ждёт», попадёт в метрику первого
ответа, разбудит автораздачу и бота. Каждое из этих четырёх проверяется ниже
отдельным тестом, потому что каждое ломается независимо от остальных.

Классификации по видам здесь нет и не проверяется: перечень ``value.type`` у
Авито нам неизвестен (docs/30), а разметка по угаданному перечню — ровно тот
приём, который в этом проекте дважды дал зелёные тесты и 4xx на боевом.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.avito.adapter import AvitoAdapter
from app.models import Conversation, Message
from app.services.inbound import apply_inbound_event
from tests.unit.conftest import drain_events

AVITO_USER_ID = 111222333
CHAT_ID = "u2i-system-1"
T0 = datetime(2026, 8, 11, 9, 0, 0, tzinfo=UTC)


def webhook(
    *,
    ptype: str = "chat_read",
    text: str | None = "Объявление снято с публикации",
    chat_id: str | None = CHAT_ID,
    envelope_id: str = "env-1",
    value_id: str | None = None,
    at: datetime = T0,
) -> dict:
    """Конверт Авито, который сам Авито объявил НЕ сообщением.

    ``ptype`` намеренно взят выдуманный: перечня видов у нас нет, и разбор не
    имеет права опираться на конкретное значение — только на то, что оно не
    ``"message"``.
    """
    value: dict = {}
    if chat_id is not None:
        value["chat_id"] = chat_id
    if value_id is not None:
        value["id"] = value_id
    if text is not None:
        value["content"] = {"text": text}
    return {
        "id": envelope_id,
        "version": "v3.0.0",
        "timestamp": int(at.timestamp()),
        "payload": {"type": ptype, "value": value},
    }


def message_webhook(*, source_type: str = "text", text: str = "Здравствуйте!") -> dict:
    return {
        "id": "env-msg-1",
        "version": "v3.0.0",
        "timestamp": int(T0.timestamp()),
        "payload": {
            "type": "message",
            "value": {
                "id": "am-1",
                "chat_id": CHAT_ID,
                "user_id": AVITO_USER_ID,
                "author_id": 999001,
                "created": int(T0.timestamp()),
                "type": source_type,
                "content": {"text": text},
            },
        },
    }


# ---------------------------------------------------------------- разбор ----


def test_system_event_with_words_travels_further() -> None:
    """Событие со словами больше не отбрасывается воркером.

    Ворота воркера — ``kind != "message" -> игнор``; чтобы служебная запись
    доехала до ленты, разбор обязан отдать ``kind="message"``, а служебность
    донести отдельным признаком.
    """
    event = AvitoAdapter.parse_webhook(webhook())
    assert event.kind == "message"
    assert event.is_system is True
    assert event.text == "Объявление снято с публикации"
    assert event.external_chat_id == CHAT_ID
    # Автора у служебного события нет — иначе ниже по конвейеру завёлся бы
    # клиент с внешним ключом "None".
    assert event.author_id is None


def test_system_event_without_words_is_still_ignored() -> None:
    """Отметки прочтения приходят пачками и слов не несут.

    Пустой чип «Сообщение Авито» — не информация, а шум на каждое открытие
    чата клиентом. Такое событие ведёт себя ровно как до правки.
    """
    event = AvitoAdapter.parse_webhook(webhook(text=None))
    assert event.kind == "system"
    assert event.is_system is False


def test_system_event_keeps_the_kind_avito_sent_verbatim() -> None:
    """Вид сохраняется сырым и не переводится в слова: словаря у нас нет."""
    event = AvitoAdapter.parse_webhook(webhook(ptype="whatever_avito_invents"))
    assert event.source_type == "whatever_avito_invents"


def test_message_value_type_is_carried_but_never_interpreted() -> None:
    """``value.type`` начал доезжать — и НИЧЕГО не меняет.

    Это заготовка для переписи видов, а не классификатор: сообщение с
    незнакомым видом остаётся обычной перепиской с клиентом.
    """
    event = AvitoAdapter.parse_webhook(message_webhook(source_type="unknown_kind"))
    assert event.source_type == "unknown_kind"
    assert event.is_system is False
    assert event.kind == "message"


def test_envelope_id_is_the_fallback_idempotency_key() -> None:
    """Своего id у события может не быть — тогда ключом служит id конверта."""
    assert AvitoAdapter.parse_webhook(webhook()).external_message_id == "env-1"
    assert AvitoAdapter.parse_webhook(webhook(value_id="sys-77")).external_message_id == "sys-77"


# --------------------------------------------------------------- конвейер ----


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def read(db_sessionmaker):
    async def _read(stmt):
        async with db_sessionmaker() as session:
            return list((await session.execute(stmt)).scalars())

    return _read


@pytest.fixture
async def conv(db_sessionmaker, account):
    """Живой диалог, в который придёт служебное сообщение."""
    from app.models import Client

    client_id, conv_id = uuid.uuid4(), uuid.uuid4()
    async with db_sessionmaker() as session, session.begin():
        session.add(Client(id=client_id, channel="avito", external_id="999001", name="Иван Петров"))
        session.add(
            Conversation(
                id=conv_id,
                channel="avito",
                external_chat_id=CHAT_ID,
                account_id=account.id,
                client_id=client_id,
                status="closed",
                bot_active=False,
                bot_vars={},
                tags=[],
                unread_count=0,
                declined_by=[],
                last_message_at=T0 - timedelta(days=30),
            )
        )
    return conv_id


async def _apply(db, redis, account, **kw) -> bool:
    return await apply_inbound_event(db, redis, account, AvitoAdapter.parse_webhook(webhook(**kw)))


async def test_system_message_lands_in_the_thread(db, redis, account, conv, read):
    """Служебное сообщение видно в ленте и отличимо от переписки."""
    assert await _apply(db, redis, account) is True
    (msg,) = await read(select(Message))
    assert msg.direction == "system"
    assert msg.sender_type == "avito"
    assert msg.body == "Объявление снято с публикации"


async def test_system_message_does_not_raise_the_unread_badge(db, redis, account, conv, read):
    """Бейдж непрочитанных зовёт оператора туда, где спросил клиент.

    Уведомление площадки вопроса не задавало.
    """
    await _apply(db, redis, account)
    (row,) = await read(select(Conversation))
    assert row.unread_count == 0


async def test_system_message_does_not_make_the_client_wait(db, redis, account, conv, read):
    """«Клиент ждёт» — про ответ человеку. Сторож не должен торопить с
    ответом на уведомление Авито, а диалог — всплывать наверх списка."""
    await _apply(db, redis, account)
    (row,) = await read(select(Conversation))
    assert row.awaiting_since is None
    assert row.offered_at is None
    assert row.status == "closed"  # закрытый диалог служебным не переоткрывается
    assert row.last_message_at.replace(tzinfo=UTC) == T0 - timedelta(days=30)


async def test_system_message_is_invisible_to_the_first_reply_metric(
    db, redis, account, conv, read
):
    """Метрика первого ответа и сторож приёма считают по паре
    ``direction='in' AND sender_type='client'``.

    Тест зовёт ровно тот предикат, что стоит в
    ``app/scheduler/jobs/watchdog.py`` и в витрине 0004, — если служебная
    запись когда-нибудь начнёт под него подходить, «время первого ответа»
    поедет на всех диалогах разом.
    """
    await _apply(db, redis, account)
    seen = await read(
        select(Message).where(Message.direction == "in", Message.sender_type == "client")
    )
    assert seen == []
    # И для тех потребителей, что смотрят на один только direction
    # (first_client в разборе, restore_awaiting, пересчёт непрочитанных).
    assert await read(select(Message).where(Message.direction == "in")) == []


async def test_the_intake_watchdog_does_not_mistake_it_for_a_client(
    db, redis, account, conv, db_sessionmaker
):
    """Тот же вопрос, но задан НАСТОЯЩЕМУ потребителю, а не его предикату.

    Сторож приёма (`check_inbound_stalled`) отвечает на «клиенты пишут или
    приём встал». Служебная запись Авито, посчитанная за письмо клиента,
    погасила бы этот сторож навсегда: уведомления Авито капают сами по себе, и
    сломанный приём выглядел бы живым.
    """
    from sqlalchemy import update

    from app.models import AvitoAccount
    from app.scheduler.jobs import watchdog

    # Вторник, 14:00 МСК — глубина рабочего дня; аккаунт подключён пять часов
    # назад, и за эти пять часов ни один клиент не написал.
    work_now = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)
    async with db_sessionmaker() as session, session.begin():
        await session.execute(update(AvitoAccount).values(created_at=work_now - timedelta(hours=5)))

    # Служебная запись Авито минуту назад — самая свежая строка в messages.
    await _apply(db, redis, account, at=work_now - timedelta(minutes=1))

    draft = await watchdog.check_inbound_stalled(db, redis, now=work_now)
    assert draft is not None, "служебная запись Авито выдала себя за письмо клиента"


async def test_system_message_wakes_neither_distribution_nor_bot(
    db, redis, account, conv, monkeypatch
):
    """Автораздача и бот не должны узнать о служебной записи.

    Тринадцать операторов получают звук и назначенный диалог из-за
    уведомления площадки — так выглядела бы эта ошибка на боевом.
    """
    from app.services import distribution

    async def explode(*a, **kw):  # pragma: no cover — срабатывание и есть провал
        raise AssertionError("автораздачу звать нельзя")

    monkeypatch.setattr(distribution, "pick_assignee", explode)

    import app.bots.runtime as runtime

    async def explode_bot(*a, **kw):  # pragma: no cover
        raise AssertionError("бота будить нельзя")

    monkeypatch.setattr(runtime, "should_run_bot", explode_bot)
    monkeypatch.setattr(runtime, "enqueue_bot_step", explode_bot)

    assert await _apply(db, redis, account) is True


async def test_system_message_is_silent_on_the_wire(db, redis, account, conv):
    """Кадра `message:new` нет: он переписал бы строку списка «последним
    сообщением» и сбросил бы списки диалогов у всех тринадцати."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await _apply(db, redis, account)
    assert await drain_events(pubsub) == []


async def test_repeated_delivery_does_not_duplicate_the_chip(db, redis, account, conv, read):
    """Запись, вернувшаяся из PEL после падения воркера, приходит второй раз с
    тем же конвертом — второго чипа быть не должно."""
    assert await _apply(db, redis, account) is True
    assert await _apply(db, redis, account) is False
    assert len(await read(select(Message))) == 1


async def test_system_message_into_unknown_chat_creates_nothing(db, redis, account, read):
    """Диалог требует клиента, а у служебного события автора нет.

    Заводить выдуманного клиента ради уведомления нельзя: он попадёт в
    справочник, в поиск и в статистику. Сырец лежит в webhook_raw_log, а
    напишет человек сам — сработает обычное создание диалога.
    """
    assert await _apply(db, redis, account, chat_id="u2i-nobody-knows") is False
    assert await read(select(Conversation)) == []
    assert await read(select(Message)) == []


# ------------------------------------------------ след в строке списка ----
#
# ДЕФЕКТ 12. Чип в ленте появился, а строка списка про уведомление молчала:
# превью так и показывало «Вы: Здравствуйте, Ольга!…» от пятого августа. Для
# диспетчера, который в этот диалог сегодня не заходил, «Клиент оформил заказ,
# ожидает подтверждения» не существовало вовсе.
#
# Здесь проверяется размен, на котором построена правка: СЛЕД В СТРОКЕ — да,
# МЕСТО В СОРТИРОВКЕ — нет. Второе стоило бы дороже следа: по
# `last_message_at` режется период в «Разборе диалогов» и считается «тихо N
# дней» у разгрузки очереди.


CLIENT_LINE = "Здравствуйте! Не включается ноутбук, залила чаем."
OPERATOR_LINE = "Здравствуйте, Ольга! Мастер подъедет сегодня после 16:00."


@pytest.fixture
async def conv_with_history(db_sessionmaker, conv):
    """Тот же диалог, но с настоящей перепиской: клиент спросил, мы ответили."""
    talked_at = T0 - timedelta(days=30)
    async with db_sessionmaker() as session, session.begin():
        session.add(
            Message(
                conversation_id=conv,
                external_message_id="am-client-1",
                direction="in",
                sender_type="client",
                body=CLIENT_LINE,
                attachments=[],
                delivery_status="delivered",
                created_at=talked_at,
            )
        )
        session.add(
            Message(
                conversation_id=conv,
                external_message_id="am-operator-1",
                direction="out",
                sender_type="operator",
                body=OPERATOR_LINE,
                attachments=[],
                delivery_status="delivered",
                created_at=talked_at + timedelta(minutes=3),
            )
        )
    return conv


async def _list_row(client, tokens, conv_id):
    """Строка диалога в списке — та самая, что видит диспетчер слева.

    `status=closed`, потому что диалог из фикстуры закрыт, а вкладка «Все» по
    умолчанию закрытые прячет.
    """
    r = await client.get(
        "/api/v1/conversations?tab=all&status=closed",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text
    (row,) = [item for item in r.json()["items"] if item["id"] == str(conv_id)]
    return row


async def test_the_notice_leaves_a_trace_in_the_list_row(
    db, redis, account, conv_with_history, client, tokens
):
    """Главное в дефекте 12: уведомление площадки видно, не открывая диалог."""
    before = await _list_row(client, tokens, conv_with_history)
    assert before["last_message"]["body"] == OPERATOR_LINE  # то, что и было

    assert await _apply(db, redis, account) is True

    after = await _list_row(client, tokens, conv_with_history)
    assert after["last_message"]["body"] == "Объявление снято с публикации"
    # `sender_type` едет наружу ради интерфейса: списку нечем иначе отличить
    # запись Авито от нашей собственной, а подписать её «Авито» он обязан.
    assert after["last_message"]["sender_type"] == "avito"
    assert after["last_message"]["direction"] == "system"


async def test_the_notice_does_not_take_the_row_up_the_list(
    db, redis, account, conv_with_history, client, tokens
):
    """Строка получает след, но НЕ место в сортировке.

    Порядок списка держит `last_message_at`; та же колонка режет период в
    «Разборе диалогов» и считает «тихо N дней» у разгрузки очереди. Сдвинь её
    ради уведомления — и диалог от четвёртого августа поедет в сегодняшний
    отчёт руководителя, а брошенный диалог перестанет считаться брошенным.
    """
    before = await _list_row(client, tokens, conv_with_history)
    await _apply(db, redis, account)
    after = await _list_row(client, tokens, conv_with_history)

    assert after["last_message_at"] == before["last_message_at"]
    # И это ровно то расхождение, ради которого строка списка показывает время
    # ПОКАЗАННОГО сообщения, а не `last_message_at`: иначе свежее уведомление
    # было бы подписано датой месячной давности.
    assert after["last_message"]["created_at"] != after["last_message_at"]


async def test_our_own_system_record_never_reaches_the_list_row(
    db_sessionmaker, conv_with_history, client, tokens
):
    """«Статус: Новый → В работе. Иванов» в превью не лезет.

    В превью допущены ДВА вида записей с `direction='system'`, и отличает их
    только `sender_type`. Ошибись отбор — и список на каждой смене статуса
    показывал бы рассказ системы о самой себе вместо слов клиента, да ещё и
    подписанный «Авито».
    """
    from app.models import Conversation as ConversationModel
    from app.services.conversations import add_system_message

    async with db_sessionmaker() as session, session.begin():
        conv_row = await session.get(ConversationModel, conv_with_history)
        add_system_message(session, conv_row, "Статус: Новый → В работе. Иванов")

    row = await _list_row(client, tokens, conv_with_history)
    assert row["last_message"]["body"] == OPERATOR_LINE
    assert row["last_message"]["sender_type"] == "operator"


async def test_a_private_note_never_reaches_the_list_row(
    db_sessionmaker, conv_with_history, client, tokens
):
    """Заметка внутренняя (01 §6.1): observer не должен прочесть её в списке.

    Отбор превью правился ради дефекта 12 — этот сторож стоит здесь, чтобы
    правка не расширила его заодно и на заметки.
    """
    async with db_sessionmaker() as session, session.begin():
        session.add(
            Message(
                conversation_id=conv_with_history,
                external_message_id=None,
                direction="note",
                sender_type="operator",
                body="Клиент скандальный, звонить только Петру",
                attachments=[],
                delivery_status="delivered",
                created_at=T0,
            )
        )

    row = await _list_row(client, tokens, conv_with_history)
    assert row["last_message"]["body"] == OPERATOR_LINE
