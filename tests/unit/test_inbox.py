"""Очередь «Входящие» с явным принятием диалога (план 7.1, 15 §2.1).

Юниты держат логику очереди: кто в неё попадает, в каком порядке, что делают
принятие, отказ и возврат, и что при этом остаётся в ленте и в журнале.

Чего здесь нет и быть не может: настоящей гонки за диалог. SQLite юнит-тестов
однопоточен, ``FOR UPDATE`` там молчаливый no-op, и два «одновременных»
принятия здесь просто идут по очереди. Гонка проверяется на настоящем
PostgreSQL — ``tests/integration/test_inbox_race.py``; последовательный второй
клик здесь проверяет ДРУГОЕ: что проигравший видит имя победителя.
"""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.models import AuditLog, Client, Conversation, Message
from app.models.notification import Notification
from app.services import audit as audit_svc
from app.services import conversations as convs
from app.services import inbox
from app.services import notifications as notify_svc
from app.services.notifications import as_utc  # SQLite отдаёт наивные метки (07 §1.1)

T0 = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------- фикстуры


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account()


@pytest.fixture
async def second_account(make_avito_account):
    return await make_avito_account(avito_user_id=444555666, title="LP-Второй")


@pytest.fixture
def make_conv(db_sessionmaker, account):
    """Диалог в заданном состоянии очереди."""
    seq = count(1)

    async def _make(
        *,
        offered_at: datetime | None = T0,
        assignee_id: uuid.UUID | None = None,
        claimed_by_id: uuid.UUID | None = None,
        claimed_at: datetime | None = None,
        status: str = "new",
        declined_by: list[str] | None = None,
        escalated_at: datetime | None = None,
        account_id: uuid.UUID | None = None,
        client_name: str = "Иван Петров",
        bot_active: bool = False,
    ) -> Conversation:
        n = next(seq)
        async with db_sessionmaker() as s:
            client = Client(channel="avito", external_id=f"cl-{n}", name=client_name)
            s.add(client)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{n}",
                account_id=account_id or account.id,
                client_id=client.id,
                status=status,
                assignee_id=assignee_id,
                claimed_by_id=claimed_by_id,
                claimed_at=claimed_at,
                offered_at=offered_at,
                declined_by=declined_by or [],
                escalated_at=escalated_at,
                bot_active=bot_active,
                unread_count=1,
                last_message_at=offered_at or T0,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"am-{n}",
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте! Почём ремонт?",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=offered_at or T0,
                )
            )
            await s.commit()
            return conv

    return _make


async def _ids(db_sessionmaker, user, **kw) -> list[str]:
    async with db_sessionmaker() as s:
        items, _ = await inbox.list_inbox(s, user, inbox.InboxFilters(**kw), now=T0)
    return [i["id"] for i in items]


async def _row(db_sessionmaker, conv_id: uuid.UUID) -> Conversation:
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        assert row is not None
        return row


async def _feed(db_sessionmaker, conv_id: uuid.UUID) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == conv_id, Message.direction == "system"
                )
            )
        ).scalars()
        return [r.body or "" for r in rows]


def details(exc: pytest.ExceptionInfo[ApiError]) -> dict[str, Any]:
    """``ApiError.details`` объявлен необязательным — сужаем в одном месте."""
    return exc.value.details or {}


async def _audit(db_sessionmaker, conv_id: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(select(AuditLog).where(AuditLog.entity_id == str(conv_id)))
        ).scalars()
        return [(r.action, dict(r.details or {})) for r in rows]


# --------------------------------------------------------------- состав очереди


async def test_queue_holds_only_dialogs_nobody_owns(db_sessionmaker, users_by_role, make_conv):
    """Четыре замка условия очереди — каждый на своём примере.

    Это же тест совместимости: диалоги, которые уже ведут, во «Входящих»
    появиться не должны, иначе после деплоя тринадцать операторов увидят там
    всю текущую работу друг друга. Пятый замок (16.08) — диалог, который ведёт
    БОТ: владелец попросил бота-«сотрудника», которому не мешают.
    """
    manager = users_by_role["manager"]
    waiting = await make_conv()
    # уже принят
    await make_conv(claimed_by_id=manager.id, assignee_id=manager.id, status="in_progress")
    # старый диалог «в работе»: принявшего нет, но есть ответственный — так
    # выглядит правило «кто первым ответил» (01 §6.2) и так выглядят строки,
    # заведённые до 7.1
    await make_conv(assignee_id=manager.id, status="in_progress", offered_at=None)
    # в очередь никогда не ставили
    await make_conv(offered_at=None)
    # закрытый
    await make_conv(status="closed")
    # ведёт бот (16.08, «бот — отдельный сотрудник»): в очереди не показывается,
    # операторы не мешают; при handoff бот снимет bot_active — диалог вернётся сам
    await make_conv(bot_active=True)

    assert await _ids(db_sessionmaker, manager) == [str(waiting.id)]


async def test_queue_puts_the_longest_waiting_first(db_sessionmaker, users_by_role, make_conv):
    """Сортировка одна и не настраивается: дольше всех ждущий — первым."""
    fresh = await make_conv(offered_at=T0 - timedelta(minutes=1))
    oldest = await make_conv(offered_at=T0 - timedelta(hours=3))
    middle = await make_conv(offered_at=T0 - timedelta(minutes=20))

    assert await _ids(db_sessionmaker, users_by_role["manager"]) == [
        str(oldest.id),
        str(middle.id),
        str(fresh.id),
    ]


async def test_queue_orders_by_client_wait_not_by_queue_entry(
    db_sessionmaker, users_by_role, make_conv
):
    """Владелец 14.09: «входящие идут вразнобой — 1 ч, 2 ч, 1 ч, 45 мин».

    Порядок — по ожиданию КЛИЕНТА (тот же якорь, что у плашки в строке: с
    последнего неотвеченного сообщения), а не по тому, когда диалог встал в
    очередь: вернувшийся в очередь час назад с клиентом, написавшим два с
    половиной часа назад, — выше того, кто написал два часа назад.
    """
    returned = await make_conv(offered_at=T0 - timedelta(hours=1))
    fresh = await make_conv(offered_at=T0 - timedelta(hours=2))
    async with db_sessionmaker() as s:
        for conv_id, ждёт_с in (
            (returned.id, timedelta(hours=2, minutes=30)),
            (fresh.id, timedelta(hours=2)),
        ):
            conv = await s.get(Conversation, conv_id)
            conv.awaiting_since = T0 - ждёт_с
            for m in (
                await s.execute(select(Message).where(Message.conversation_id == conv_id))
            ).scalars():
                m.created_at = T0 - ждёт_с
        await s.commit()

    assert await _ids(db_sessionmaker, users_by_role["manager"]) == [
        str(returned.id),
        str(fresh.id),
    ]


async def test_inbox_item_carries_the_wait_and_the_list_shape(
    db_sessionmaker, users_by_role, make_conv, account
):
    """Строка очереди — обычный ConversationOut плюс ожидание."""
    await make_conv(offered_at=T0 - timedelta(minutes=90))
    async with db_sessionmaker() as s:
        items, total = await inbox.list_inbox(s, users_by_role["manager"], now=T0)
    assert total == 1
    item = items[0]
    assert item["client"]["name"] == "Иван Петров"
    assert item["account"]["title"] == account.title
    assert item["last_message"]["direction"] == "in"  # батч-загрузчик списка, без N+1
    assert item["assignee"] is None
    assert item["waiting_seconds"] == 90 * 60
    assert item["waiting_human"] == "1 ч 30 мин"
    assert item["escalated"] is False
    assert item["declined_count"] == 0


async def test_broadcast_frame_makes_no_claim_about_my_unread(db_sessionmaker, make_conv):
    """Кадр `inbox:new` один на всех — пер-юзерного числа он знать не может.

    `conversation_out` кладёт в строку колонку `conversations.unread_count`. В
    ручках поверх неё ложится счёт по маркеру смотрящего, а у кадра такой
    возможности нет: тело одно на всех допущенных операторов. Колонку при этом
    никто не увеличивает с переезда на маркеры — в кадре ехало замёрзшее число,
    и фронт ставил его в строку очереди каждому.

    ЧТО ЛОМАЛИ: убрали обнуление в `inbox_frame` — тест краснеет на 7.
    """
    conv = await make_conv(offered_at=T0 - timedelta(minutes=5))
    async with db_sessionmaker() as s:
        строка = await s.get(Conversation, conv.id)
        assert строка is not None
        строка.unread_count = 7  # как у диалога, пережившего переезд на маркеры
        await s.commit()
    async with db_sessionmaker() as s:
        кадр = await inbox.inbox_frame(s, await s.get(Conversation, conv.id), now=T0)
    assert кадр["unread_count"] == 0, "кадр очереди утверждает пер-юзерное число, которого не знает"


async def test_account_filter_narrows_the_queue(
    db_sessionmaker, users_by_role, make_conv, second_account
):
    """Готовим 7.2 (операторы на каналах): очередь одного канала уже фильтруется."""
    mine = await make_conv(account_id=second_account.id)
    await make_conv()
    assert await _ids(db_sessionmaker, users_by_role["manager"], account_id=second_account.id) == [
        str(mine.id)
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 с"),
        (45, "45 с"),
        (60, "1 мин"),
        (3600, "1 ч"),
        (3900, "1 ч 5 мин"),
        (90000, "1 дн 1 ч"),
    ],
)
def test_format_wait_speaks_human(seconds: int, expected: str):
    assert inbox.format_wait(seconds) == expected


# -------------------------------------------------------------------- принятие


async def test_claim_takes_the_dialog_out_of_the_queue_into_mine(
    db_sessionmaker, users_by_role, make_conv
):
    manager = users_by_role["manager"]
    conv = await make_conv(offered_at=T0 - timedelta(minutes=12))

    async with db_sessionmaker() as s:
        result = await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()

    assert result.waited_seconds == 12 * 60
    row = await _row(db_sessionmaker, conv.id)
    assert row.claimed_by_id == manager.id
    assert row.assignee_id == manager.id  # дальше диалог живёт по обычным правилам
    assert row.status == "in_progress"
    assert row.claimed_at is not None
    assert await _ids(db_sessionmaker, manager) == []

    # Вкладка «Мои» (01 §5.1) продолжает работать без единой правки.
    async with db_sessionmaker() as s:
        mine, _ = await convs.list_conversations(s, manager, tab="my")
    assert [i["id"] for i in mine] == [str(conv.id)]


async def test_claim_writes_the_feed_record_and_the_journal(
    db_sessionmaker, users_by_role, make_conv
):
    manager = users_by_role["manager"]
    conv = await make_conv(offered_at=T0 - timedelta(minutes=12))
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()

    assert await _feed(db_sessionmaker, conv.id) == [
        f"Диалог принят: {manager.full_name}. Ждал 12 мин"
    ]

    journal = dict(await _audit(db_sessionmaker, conv.id))
    assert journal["conversation.assigned"]["by"] == "self"
    assert journal["conversation.assigned"]["source"] == "inbox"
    assert journal["conversation.assigned"]["waited_seconds"] == 12 * 60
    assert journal["conversation.status_changed"]["from"] == "new"
    assert journal["conversation.status_changed"]["to"] == "in_progress"


async def test_the_system_record_does_not_bump_the_list_order(
    db_sessionmaker, users_by_role, make_conv
):
    """`last_message_at` — метка переписки; служебная запись её не двигает."""
    conv = await make_conv(offered_at=T0 - timedelta(hours=5))
    before = (await _row(db_sessionmaker, conv.id)).last_message_at
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, users_by_role["manager"], now=T0)
        await s.commit()
    assert (await _row(db_sessionmaker, conv.id)).last_message_at == before


async def test_the_second_claim_names_the_winner(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Проигравший обязан увидеть ИМЯ, а не «конфликт данных»."""
    winner = users_by_role["manager"]
    loser = await make_user("loser@leadchat.test", role="manager", full_name="Пётр Второй")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, winner, now=T0)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, conv.id, loser, now=T0)
    assert exc.value.code == "already_claimed"
    assert exc.value.status == 409
    assert winner.full_name in exc.value.message
    assert details(exc)["claimed_by"]["id"] == str(winner.id)
    assert details(exc)["mine"] is False


async def test_claiming_my_own_dialog_twice_is_marked_as_mine(
    db_sessionmaker, users_by_role, make_conv
):
    """Двойной клик по своей же кнопке — не ошибка данных, и фронт это видит."""
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, conv.id, manager, now=T0)
    assert details(exc)["mine"] is True


async def test_a_dialog_taken_by_the_old_first_answer_rule_cannot_be_claimed(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Совместимость: диалог, взятый ответом (01 §6.2), уже занят."""
    owner = users_by_role["manager"]
    other = await make_user("other@leadchat.test", role="manager", full_name="Анна Третья")
    conv = await make_conv(assignee_id=owner.id, status="in_progress")
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, conv.id, other, now=T0)
    assert exc.value.code == "already_claimed"
    assert details(exc)["claimed_by"]["full_name"] == owner.full_name


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_head_and_observer_cannot_claim(db_sessionmaker, users_by_role, make_conv, role):
    """Наблюдатель только читает; руководитель клиентам не пишет (DESIGN §5.1)."""
    conv = await make_conv()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, conv.id, users_by_role[role], now=T0)
    assert exc.value.status == 403
    assert details(exc)["reason"] == "cannot_answer_clients"
    assert (await _row(db_sessionmaker, conv.id)).claimed_by_id is None


async def test_admin_can_claim(db_sessionmaker, users_by_role, make_conv):
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, users_by_role["admin"], now=T0)
        await s.commit()
    assert (await _row(db_sessionmaker, conv.id)).claimed_by_id == users_by_role["admin"].id


async def test_claiming_a_closed_dialog_is_rejected(db_sessionmaker, users_by_role, make_conv):
    conv = await make_conv(status="closed")
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, conv.id, users_by_role["manager"], now=T0)
    assert exc.value.status == 422
    assert details(exc)["reason"] == "conversation_closed"


async def test_claim_retries_once_when_the_dialog_is_freed_mid_flight(
    db_sessionmaker, users_by_role, make_conv, monkeypatch
):
    """Третья перестановка гонки: диалог освободили между UPDATE и перечиткой.

    Первый `UPDATE` не берёт ничего (диалог ещё за коллегой), а к моменту
    разбора «почему» коллега уже нажал «Вернуть». Без повтора оператор получал
    бы 422 «диалог не ждёт принятия» — про диалог, который в эту секунду стоит
    в очереди у него на экране. Освобождение эмулируем перехватом перечитки:
    настоящий параллелизм на юнит-стенде невозможен (см. шапку файла), а
    проверяется здесь ветка кода, а не поведение СУБД.
    """
    manager, colleague = users_by_role["manager"], users_by_role["admin"]
    conv = await make_conv(
        assignee_id=colleague.id, claimed_by_id=colleague.id, status="in_progress"
    )
    original_reload = inbox._reload
    freed = False

    async def _reload_and_free(db, conversation_id):
        nonlocal freed
        row = await original_reload(db, conversation_id)
        if row is not None and not freed:
            freed = True  # коллега нажал «Вернуть» ровно сейчас
            inbox.return_to_queue(row, now=T0)
            await db.flush()
        return row

    monkeypatch.setattr(inbox, "_reload", _reload_and_free)

    async with db_sessionmaker() as s:
        result = await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()
    assert result.conversation.claimed_by_id == manager.id

    row = await _row(db_sessionmaker, conv.id)
    assert row.claimed_by_id == manager.id and row.status == "in_progress"


async def test_claiming_a_missing_dialog_is_404(db_sessionmaker, users_by_role):
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.claim(s, uuid.uuid4(), users_by_role["manager"], now=T0)
    assert exc.value.status == 404


# -------------------------------------------------------------------- отклонение


async def test_declined_dialog_hides_from_the_decliner_but_stays_for_others(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Главное свойство отказа: он персональный.

    Если прятать диалог только в интерфейсе, оператор будет натыкаться на него
    после каждого обновления списка.
    """
    first = users_by_role["manager"]
    second = await make_user("m2@leadchat.test", role="manager", full_name="Мария Вторая")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, first, "не мой канал", now=T0)
        await s.commit()

    assert await _ids(db_sessionmaker, first) == []
    assert await _ids(db_sessionmaker, second) == [str(conv.id)]
    # Диалог остался в очереди — он не «исчез», он просто не у этого человека.
    assert inbox.is_waiting(await _row(db_sessionmaker, conv.id))


async def test_decline_reason_lands_in_the_feed(db_sessionmaker, users_by_role, make_conv):
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, "  занят другим клиентом  ", now=T0)
        await s.commit()
    assert await _feed(db_sessionmaker, conv.id) == [
        f"Диалог отклонён: {manager.full_name}. Причина: занят другим клиентом"
    ]


async def test_decline_is_journaled_with_its_reason(db_sessionmaker, users_by_role, make_conv):
    """Отказ — событие журнала, а не только строка в ленте одного диалога.

    «Клиент ждал час, потому что от него отказались четверо» — это разбор
    смены (01 §9.7), и увидеть его в ленте диалога нельзя: туда никто не
    ходит специально. Действие обязано быть в реестре AUDIT_ACTIONS — запись
    мимо реестра валит страж в tests/unit/test_audit.py.
    """
    manager = users_by_role["manager"]
    conv = await make_conv(offered_at=T0 - timedelta(minutes=12))
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, "занят другим клиентом", now=T0)
        await s.commit()

    journal = dict(await _audit(db_sessionmaker, conv.id))
    assert "conversation.declined" in journal, "отказ не попал в журнал"
    assert journal["conversation.declined"]["reason"] == "занят другим клиентом"
    assert journal["conversation.declined"]["declined_count"] == 1
    assert journal["conversation.declined"]["waited_seconds"] == 12 * 60
    assert audit_svc.describe("conversation.declined", journal["conversation.declined"]) == (
        "Оператор отказался от диалога (причина: занят другим клиентом)"
    )


async def test_claiming_from_the_queue_reads_differently_in_the_journal(
    db_sessionmaker, users_by_role, make_conv
):
    """«Диалог принят из очереди» ≠ «Диалог взят в работу» (7.1 против 01 §6.2).

    Действие в журнале одно (`conversation.assigned`), а событий смены два, и
    различает их `details.source`: принял по кнопке или просто ответил первым.
    """
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()

    journal = dict(await _audit(db_sessionmaker, conv.id))
    assigned = journal["conversation.assigned"]
    assert assigned["source"] == "inbox" and assigned["by"] == "self"
    assert audit_svc.describe("conversation.assigned", assigned) == "Диалог принят из очереди"
    # А снятие ответственного из очереди — «возвращён во «Входящие»», не «снят».
    assert (
        audit_svc.describe("conversation.assigned", {"assignee_id": None, "source": "inbox"})
        == "Диалог возвращён во «Входящие»"
    )
    assert (
        audit_svc.describe("conversation.assigned", {"assignee_id": None})
        == "С диалога снят ответственный"
    )


async def test_decline_is_idempotent(db_sessionmaker, users_by_role, make_conv):
    """Повторный отказ не плодит записи в ленте и не растит список отказавшихся."""
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        again = await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()
    assert again.already_declined is True
    assert again.system_message is None
    assert len(await _feed(db_sessionmaker, conv.id)) == 1
    assert (await _row(db_sessionmaker, conv.id)).declined_by == [str(manager.id)]


async def test_a_too_long_reason_is_rejected(db_sessionmaker, users_by_role, make_conv):
    conv = await make_conv()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.decline(s, conv.id, users_by_role["manager"], "я" * 501, now=T0)
    assert exc.value.status == 413


async def test_declining_a_claimed_dialog_is_already_claimed(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    owner = users_by_role["manager"]
    other = await make_user("m3@leadchat.test", role="manager", full_name="Олег Третий")
    conv = await make_conv(claimed_by_id=owner.id, assignee_id=owner.id, status="in_progress")
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.decline(s, conv.id, other, now=T0)
    assert exc.value.code == "already_claimed"


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_head_and_observer_cannot_decline(db_sessionmaker, users_by_role, make_conv, role):
    conv = await make_conv()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.decline(s, conv.id, users_by_role[role], now=T0)
    assert exc.value.status == 403


def test_the_escalation_event_is_registered_in_the_catalog():
    """«Диалог никто не принял» обязан быть в каталоге событий (14 §2).

    Вызов передаёт важность и адресата явно, поэтому без записи в каталоге
    уведомление всё равно доходило бы — но каждая эскалация писала бы в лог
    `notification.unknown_kind`, а центр уведомлений не знал бы, как её
    склеивать и кому показывать кнопку. Заодно тест не даёт каталогу и коду
    разойтись в важности и адресате.
    """
    spec = notify_svc.spec_for(inbox.ESCALATION_KIND)
    assert spec is not None, f"{inbox.ESCALATION_KIND} нет в каталоге KINDS"
    assert spec.severity == "warning"
    assert spec.audience == "admin"
    assert spec.dedup == "entity" and spec.entity_type == "conversation"


async def test_when_everyone_declined_admins_are_called_and_the_dialog_stays(
    db_sessionmaker, users_by_role, make_conv
):
    """Эскалация: диалог не исчезает (это был бы брошенный клиент), а помечается.

    Состав «кому доступен» — активные, кто отвечает клиентам: админ и менеджер
    (у head/observer нет `messages:send`). Значит эскалация наступает после
    отказа обоих, а не после первого.
    """
    manager, admin = users_by_role["manager"], users_by_role["admin"]
    conv = await make_conv(offered_at=T0 - timedelta(minutes=40), client_name="Ольга")

    async with db_sessionmaker() as s:
        first = await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()
    assert first.escalated is False

    async with db_sessionmaker() as s:
        last = await inbox.decline(s, conv.id, admin, "нет свободных рук", now=T0)
        await s.commit()
    assert last.escalated is True
    assert last.notification is not None

    row = await _row(db_sessionmaker, conv.id)
    assert row.escalated_at is not None
    assert inbox.is_waiting(row), "эскалированный диалог обязан остаться в очереди"

    async with db_sessionmaker() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].audience == "admin"  # уведомление администраторам (14 §2)
    assert rows[0].severity == "warning"
    assert rows[0].entity_type == "conversation" and rows[0].entity_id == str(conv.id)
    assert "Ольга" in (rows[0].body or "") and "40 мин" in (rows[0].body or "")
    assert "нет свободных рук" in (rows[0].body or "")


async def test_escalation_rings_once_even_if_someone_declines_later(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    manager, admin = users_by_role["manager"], users_by_role["admin"]
    conv = await make_conv()
    for who in (manager, admin):
        async with db_sessionmaker() as s:
            await inbox.decline(s, conv.id, who, now=T0)
            await s.commit()

    latecomer = await make_user("m4@leadchat.test", role="manager", full_name="Поздний")
    async with db_sessionmaker() as s:
        result = await inbox.decline(s, conv.id, latecomer, now=T0)
        await s.commit()
    assert result.escalated is False  # пометка уже стоит — второй раз не звоним

    async with db_sessionmaker() as s:
        assert len((await s.execute(select(Notification))).scalars().all()) == 1


async def test_an_escalated_dialog_shows_its_mark_and_the_counter_splits(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    watcher = await make_user("m5@leadchat.test", role="manager", full_name="Свежий")
    conv = await make_conv(escalated_at=T0)
    async with db_sessionmaker() as s:
        items, _ = await inbox.list_inbox(s, watcher, now=T0)
        counts = await inbox.inbox_counts(s, watcher)
    assert items[0]["escalated"] is True
    assert items[0]["id"] == str(conv.id)
    assert counts == {"waiting": 1, "escalated": 1}


# ------------------------------------------------------------------- возврат


async def test_a_dialog_led_for_hours_returns_with_the_clients_current_wait(
    db_sessionmaker, users_by_role, make_conv
):
    """Диалог приняли утром и вели три часа; клиент написал 20 минут назад.

    Вставал в очередь с утренним `offered_at`: администраторам сразу уходило
    «ждёт в очереди 3 ч», взявший читал «Ждал 3 ч». Ждёт клиент 20 минут.
    """
    manager = users_by_role["manager"]
    conv = await make_conv(offered_at=T0 - timedelta(hours=3))
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0 - timedelta(hours=3))
        await s.commit()
    async with db_sessionmaker() as s:
        (await s.get(Conversation, conv.id)).awaiting_since = T0 - timedelta(minutes=20)
        await s.commit()

    async with db_sessionmaker() as s:
        await inbox.release(s, conv.id, manager, now=T0)
        await s.commit()

    row = await _row(db_sessionmaker, conv.id)
    assert as_utc(row.offered_at) == T0 - timedelta(minutes=20)
    assert inbox.waiting_seconds(row, now=T0) == 20 * 60


async def test_release_returns_the_dialog_to_the_queue_for_everyone(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    manager = users_by_role["manager"]
    colleague = await make_user("m6@leadchat.test", role="manager", full_name="Коллега")
    offered = T0 - timedelta(minutes=30)
    conv = await make_conv(offered_at=offered)
    async with db_sessionmaker() as s:
        # Клиент ждёт с того сообщения, с которым диалог встал в очередь.
        (await s.get(Conversation, conv.id)).awaiting_since = offered
        await s.commit()

    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        await inbox.release(s, conv.id, manager, now=T0 + timedelta(minutes=1))
        await s.commit()

    row = await _row(db_sessionmaker, conv.id)
    assert row.claimed_by_id is None and row.claimed_at is None
    assert row.assignee_id is None
    assert row.status == "new"
    # Передумал сразу: клиент по-прежнему ждёт с первого сообщения, и
    # возвращённый диалог обязан оказаться наверху.
    assert as_utc(row.offered_at) == offered
    assert await _ids(db_sessionmaker, manager) == [str(conv.id)]
    assert await _ids(db_sessionmaker, colleague) == [str(conv.id)]
    assert await _feed(db_sessionmaker, conv.id) == [
        f"Диалог принят: {manager.full_name}. Ждал 30 мин",
        f"Диалог возвращён во «Входящие»: {manager.full_name}",
    ]


async def test_release_journals_the_removal_of_the_owner(db_sessionmaker, users_by_role, make_conv):
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        await inbox.release(s, conv.id, manager, now=T0)
        await s.commit()

    journal = await _audit(db_sessionmaker, conv.id)
    removals = [d for a, d in journal if a == "conversation.assigned" and d["assignee_id"] is None]
    assert removals and removals[0]["prev_assignee_id"] == str(manager.id)
    back = [d for a, d in journal if a == "conversation.status_changed" and d["to"] == "new"]
    assert back and back[0]["from"] == "in_progress"


async def test_a_stranger_cannot_release_but_an_admin_can(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Запрет «только свой» не должен означать «диалог уехавшего не вытащить»."""
    manager = users_by_role["manager"]
    stranger = await make_user("m7@leadchat.test", role="manager", full_name="Чужой")
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.release(s, conv.id, stranger, now=T0)
    assert exc.value.status == 403
    assert details(exc)["claimed_by"]["full_name"] == manager.full_name

    async with db_sessionmaker() as s:
        await inbox.release(s, conv.id, users_by_role["admin"], now=T0)
        await s.commit()
    assert (await _row(db_sessionmaker, conv.id)).claimed_by_id is None


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_release_checks_the_right_in_the_service_too(
    db_sessionmaker, users_by_role, make_conv, role
):
    """Все ТРИ действия очереди спрашивают право в сервисе, а не только в ручке.

    У `claim` и `decline` проверка стоит первой строкой, у `release` её не было:
    наблюдателя и руководителя отсекала одна зависимость роутера. Сервис зовут
    не только из ручки (воркеры, CLI, будущее автораспределение), и «вернуть
    диалог в очередь» — то же действие над клиентом, что принять и отклонить.
    """
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, manager, now=T0)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.release(s, conv.id, users_by_role[role], now=T0)
    assert exc.value.status == 403
    assert details(exc)["reason"] == "cannot_answer_clients"
    assert (await _row(db_sessionmaker, conv.id)).claimed_by_id == manager.id


async def test_releasing_an_unclaimed_dialog_is_422(db_sessionmaker, users_by_role, make_conv):
    conv = await make_conv()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await inbox.release(s, conv.id, users_by_role["manager"], now=T0)
    assert exc.value.status == 422
    assert details(exc)["reason"] == "not_claimed"


async def test_release_keeps_the_declines(db_sessionmaker, users_by_role, make_conv, make_user):
    """Отказавшийся не обязан видеть диалог снова только потому, что его
    кто-то подержал и вернул."""
    manager = users_by_role["manager"]
    other = await make_user("m8@leadchat.test", role="manager", full_name="Взявший")
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, other, now=T0)
        await s.commit()
    async with db_sessionmaker() as s:
        await inbox.release(s, conv.id, other, now=T0)
        await s.commit()

    assert await _ids(db_sessionmaker, manager) == []
    assert await _ids(db_sessionmaker, other) == [str(conv.id)]


# ------------------------------------------------- отказ живёт три минуты (13.08)


async def _ids_at(db_sessionmaker, user, moment) -> list[str]:
    """Очередь глазами человека В ЗАДАННЫЙ момент — отказ теперь зависит от времени."""
    async with db_sessionmaker() as s:
        items, _ = await inbox.list_inbox(s, user, inbox.InboxFilters(), now=moment)
    return [i["id"] for i in items]


async def test_decline_expires_and_the_dialog_comes_back(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """ТРЕБОВАНИЕ ЗАКАЗЧИКА ОТ 13 АВГУСТА, ради которого всё и делалось.

    «При нажатии „Отклонить“ диалог возвращался в течение 3 минут назад во входящие, и
    работало под каждого пользователя отдельно: у того, кто нажал, вернётся в очередь;
    тот, кто не нажимал, так и останется».

    Отказ — это «уберите с глаз, я сейчас занят», а не «никогда больше». Проверяются обе
    границы: до срока диалог спрятан, после — снова в очереди. Проверять только одну
    сторону бессмысленно: тест «спрятан» проходил и на вечном отказе.
    """
    первый = users_by_role["manager"]
    второй = await make_user("m20@leadchat.test", role="manager", full_name="Не нажимавший")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, первый, now=T0)
        await s.commit()

    # Сразу после нажатия — спрятан у нажавшего, виден остальным.
    assert await _ids_at(db_sessionmaker, первый, T0) == []
    assert await _ids_at(db_sessionmaker, второй, T0) == [str(conv.id)]

    # За секунду до срока — всё ещё спрятан.
    почти = T0 + inbox.DECLINE_TTL - timedelta(seconds=1)
    assert await _ids_at(db_sessionmaker, первый, почти) == []

    # Через три минуты — вернулся, и именно тому, кто нажимал.
    после = T0 + inbox.DECLINE_TTL + timedelta(seconds=1)
    assert await _ids_at(db_sessionmaker, первый, после) == [str(conv.id)]
    # ⚠ У НЕ НАЖИМАВШЕГО НИЧЕГО НЕ ИЗМЕНИЛОСЬ, и это половина требования. Диалог у него
    # не «вернулся» — он и не пропадал: отказ персональный, а не «диалог уходит из очереди».
    assert await _ids_at(db_sessionmaker, второй, после) == [str(conv.id)]


async def test_second_decline_hides_it_again(db_sessionmaker, users_by_role, make_conv):
    """Диалог вернулся, человек отказался снова — и он снова прячется.

    ⚠ ЭТО НЕ ОЧЕВИДНО И ЛЕГКО СЛОМАТЬ. Идемпотентность отказа раньше считалась по списку
    отказывавшихся: «я там есть — значит уже отказывался, ничего не делаем». С истекающим
    отказом та же проверка убила бы саму задачу: диалог вернулся, человек жмёт «Отклонить»
    второй раз — а система отвечает «уже отказывался» и не прячет ничего. Дубль — это два
    нажатия подряд, и отличает его свежесть предыдущего отказа, а не факт его наличия.
    """
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()

    позже = T0 + inbox.DECLINE_TTL + timedelta(seconds=1)
    assert await _ids_at(db_sessionmaker, manager, позже) == [str(conv.id)]

    async with db_sessionmaker() as s:
        итог = await inbox.decline(s, conv.id, manager, now=позже)
        await s.commit()
    assert итог.already_declined is False  # это НЕ дубль нажатия
    assert await _ids_at(db_sessionmaker, manager, позже) == []


async def test_double_click_is_still_one_decline(db_sessionmaker, users_by_role, make_conv):
    """А вот два нажатия подряд — по-прежнему один отказ и одна строка в ленте.

    Двойной клик и две открытые вкладки никуда не делись; от них защищала та же проверка,
    которую пришлось переписать под истечение.
    """
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, now=T0)
        итог = await inbox.decline(s, conv.id, manager, now=T0 + timedelta(seconds=2))
        await s.commit()
    assert итог.already_declined is True
    assert await _feed(db_sessionmaker, conv.id) == [f"Диалог отклонён: {manager.full_name}"]


async def test_undo_returns_it_at_once_not_in_three_minutes(
    db_sessionmaker, users_by_role, make_conv
):
    """Отмена отказа возвращает диалог СРАЗУ, а не по истечении срока.

    Иначе кнопка «Вернуть» три минуты не делала бы ничего видимого, и человек нажал бы её
    ещё раз, решив что не сработало.
    """
    manager = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, manager, now=T0)
        await s.commit()
    assert await _ids_at(db_sessionmaker, manager, T0) == []

    async with db_sessionmaker() as s:
        await inbox.undo_decline(s, conv.id, manager, now=T0 + timedelta(seconds=5))
        await s.commit()
    assert await _ids_at(db_sessionmaker, manager, T0 + timedelta(seconds=5)) == [str(conv.id)]


async def test_escalation_still_counts_everyone_who_ever_declined(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """⚠ ЭСКАЛАЦИЯ СЧИТАЕТСЯ ПО ВСЕМ ОТКАЗАВШИМСЯ, А НЕ ПО ДЕЙСТВУЮЩИМ ОТКАЗАМ.

    «Отказались ВСЕ, кому диалог доступен» — повод позвать администраторов (14 §2.3).
    Считай круг по истекающим отказам — и через три минуты после последнего отказа круг
    рассыпался бы сам собой, а администраторов не позвали бы никогда: отказы истекают
    быстрее, чем успевает отказаться следующий.

    Поэтому массив `declined_by` остался и ведётся по-прежнему — он отвечает на вопрос
    «кто вообще отказывался», и это другой вопрос.
    """
    первый = users_by_role["manager"]
    conv = await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, conv.id, первый, now=T0)
        await s.commit()
    # Отказ истёк — но след в «кто отказывался» остался.
    строка = await _row(db_sessionmaker, conv.id)
    assert str(первый.id) in [str(x) for x in (строка.declined_by or [])]


# ------------------------------------------------------------------- счётчик


async def test_inbox_count_is_personal(db_sessionmaker, users_by_role, make_conv, make_user):
    manager = users_by_role["manager"]
    colleague = await make_user("m9@leadchat.test", role="manager", full_name="Сосед")
    first = await make_conv()
    await make_conv()
    async with db_sessionmaker() as s:
        await inbox.decline(s, first.id, manager, now=T0)
        await s.commit()

    # ⚠ `now=T0` ОБЯЗАТЕЛЕН С 13 АВГУСТА. Отказ живёт три минуты (`inbox.DECLINE_TTL`),
    # и без явного момента счётчик считал бы «сейчас» — а отказ, сделанный в T0, к этому
    # времени давно истёк. Раньше время не значило ничего, и параметра не требовалось.
    async with db_sessionmaker() as s:
        assert await inbox.inbox_count(s, manager, now=T0) == 1
        assert await inbox.inbox_count(s, colleague, now=T0) == 2
        items, total = await inbox.list_inbox(s, manager, now=T0)
    assert total == len(items) == 1  # счётчик и вкладка считают одно и то же


# ------------------------------------------------------------------- передача


async def test_переданный_диалог_виден_во_входящих_получателя(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """⚠ ПРОСЬБА ВЛАДЕЛЬЦА 31.08: «когда передаёшь диалог, пусть он показывается
    во Входящих у того, кому передаю».

    До этого переданный диалог попадал только во вкладку «Мои»
    (`participants.mine_condition`), а туда человек заглядывает, чтобы
    ПРОДОЛЖИТЬ работу, а не чтобы взять новую. Предложение о передаче — это
    ровно «вот работа, решите», то есть смысл «Входящих». Пока его там не было,
    диалог ждал, пока получатель случайно посмотрит в другую вкладку.
    """
    from app.services import transfer as transfer_svc

    отдал = users_by_role["manager"]
    получатель = await make_user("recv@leadchat.test", role="manager", full_name="Получатель")
    третий = await make_user("third@leadchat.test", role="manager", full_name="Третий")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        строка = await s.get(Conversation, conv.id)
        # Диалог ведёт отдавший — в общей очереди его быть не может.
        строка.assignee_id = отдал.id
        строка.claimed_by_id = отдал.id
        строка.status = "in_progress"
        transfer_svc.offer(строка, to=получатель, actor=отдал, comment=None, now=T0)
        await s.commit()

    async with db_sessionmaker() as s:
        мои, _ = await inbox.list_inbox(s, получатель, now=T0)
        чужие, _ = await inbox.list_inbox(s, третий, now=T0)

    assert [str(x["id"]) for x in мои] == [str(conv.id)], (
        "переданный диалог не появился во «Входящих» получателя"
    )
    assert [str(x["id"]) for x in чужие] == [], (
        "предложение передачи видно постороннему — его заберёт не тот, кому передавали"
    )


async def test_счётчик_входящих_учитывает_переданное(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Бейдж и список обязаны считать одно и то же — иначе бейдж врёт."""
    from app.services import transfer as transfer_svc

    отдал = users_by_role["manager"]
    получатель = await make_user("recv2@leadchat.test", role="manager", full_name="Получатель")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        строка = await s.get(Conversation, conv.id)
        строка.assignee_id = отдал.id
        строка.claimed_by_id = отдал.id
        строка.status = "in_progress"
        transfer_svc.offer(строка, to=получатель, actor=отдал, comment=None, now=T0)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await inbox.inbox_count(s, получатель, now=T0) == 1


async def test_закрытое_предложение_во_входящие_не_лезет(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """Закрытый диалог — не работа: он не должен всплывать предложением."""
    from app.services import transfer as transfer_svc

    отдал = users_by_role["manager"]
    получатель = await make_user("recv3@leadchat.test", role="manager", full_name="Получатель")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        строка = await s.get(Conversation, conv.id)
        строка.assignee_id = отдал.id
        строка.claimed_by_id = отдал.id
        transfer_svc.offer(строка, to=получатель, actor=отдал, comment=None, now=T0)
        строка.status = "closed"
        await s.commit()

    async with db_sessionmaker() as s:
        assert await inbox.inbox_count(s, получатель, now=T0) == 0


async def test_автораздача_переданное_не_забирает(
    db_sessionmaker, users_by_role, make_conv, make_user
):
    """⚠ ГРАНИЦА: личная ветка НЕ попадает в общее `queue_condition`.

    Им пользуются автораздача, сторожа возврата и эскалация. Попади туда
    предложение передачи — диалог, который передали Ивану, забрал бы Пётр, и
    передача перестала бы что-либо значить.
    """
    from app.services import transfer as transfer_svc

    отдал = users_by_role["manager"]
    получатель = await make_user("recv4@leadchat.test", role="manager", full_name="Получатель")
    conv = await make_conv()

    async with db_sessionmaker() as s:
        строка = await s.get(Conversation, conv.id)
        строка.assignee_id = отдал.id
        строка.claimed_by_id = отдал.id
        строка.status = "in_progress"
        transfer_svc.offer(строка, to=получатель, actor=отдал, comment=None, now=T0)
        await s.commit()
        обновлённая = await s.get(Conversation, conv.id)
        assert inbox.is_waiting(обновлённая) is False, (
            "переданный диалог считается ничьим — его заберёт автораздача"
        )


# --------------------------------------------------- помощники для чужих зон


async def test_enter_queue_starts_a_fresh_wait(db_sessionmaker, users_by_role, make_conv):
    """Клиент написал снова — это новое ожидание: отказы обнуляются."""
    manager = users_by_role["manager"]
    conv = await make_conv(declined_by=[str(manager.id)], escalated_at=T0)
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row is not None
        inbox.enter_queue(row, now=T0 + timedelta(hours=1))
        await s.commit()

    row = await _row(db_sessionmaker, conv.id)
    assert row.declined_by == [] and row.escalated_at is None
    assert as_utc(row.offered_at) == T0 + timedelta(hours=1)
    assert await _ids(db_sessionmaker, manager) == [str(conv.id)]


async def test_return_to_queue_can_drop_the_declines_on_demand(
    db_sessionmaker, users_by_role, make_conv
):
    manager = users_by_role["manager"]
    conv = await make_conv(
        assignee_id=manager.id, status="in_progress", declined_by=[str(manager.id)]
    )
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row is not None
        inbox.return_to_queue(row, now=T0, keep_declines=False)
        await s.commit()

    row = await _row(db_sessionmaker, conv.id)
    assert row.status == "new" and row.assignee_id is None
    assert row.declined_by == []
    assert as_utc(row.offered_at) == T0  # строки до 7.1 приходят без offered_at — ставим сейчас
    assert await _ids(db_sessionmaker, manager) == [str(conv.id)]


async def test_return_to_queue_never_reopens_a_closed_dialog(
    db_sessionmaker, users_by_role, make_conv
):
    """Закрытый диалог руками в «Новые» не возвращают (01 §5.4)."""
    conv = await make_conv(status="closed", assignee_id=users_by_role["manager"].id)
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row is not None
        inbox.return_to_queue(row, now=T0)
        await s.commit()
    row = await _row(db_sessionmaker, conv.id)
    assert row.status == "closed"
    assert not inbox.is_waiting(row)


# --------------------------------------------------------------- патчи для кадров
#
# Сами кадры собирает и публикует роутер (`tests/unit/test_inbox_api.py`), здесь
# — только дельты, которые в них едут: это то, что читает фронт, и то, что
# обязано совпадать у HTTP-ответа и у WS-события.
#
# Три функции `publish_claimed/declined/released` из этого модуля удалены
# вместе со своими тестами: они публиковали `conversation:claimed` там, где
# каталог (app/ws/hub.py) и фронт работают с `inbox:claimed`, и не вызывались
# ниоткуда. Зелёный тест на них означал только одно — что неправильные имена
# событий кто-то охраняет.


def test_queue_patch_says_the_dialog_is_waiting_again(users_by_role):
    """`in_inbox` в патче — то же, что предикат выборки: один источник правды."""
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id="patch-1",
        account_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        status="new",
        offered_at=T0,
        declined_by=[],
    )
    patch = inbox.queue_patch(conv)
    assert patch == {
        "status": "new",
        "assignee": None,
        "in_inbox": True,
        "offered_at": "2026-08-06T09:00:00.000Z",
        "escalated": False,
    }

    conv.escalated_at = T0
    assert inbox.queue_patch(conv)["escalated"] is True


def test_claimed_patch_marks_the_dialog_busy_for_everyone_else(users_by_role):
    """Главное поле кадра «принят» — `in_inbox: false`: строка уходит у всех."""
    manager = users_by_role["manager"]
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id="patch-2",
        account_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        status="in_progress",
        offered_at=T0,
        claimed_by_id=manager.id,
        claimed_at=T0,
        assignee_id=manager.id,
        declined_by=[],
    )
    patch = inbox.claimed_patch(conv, manager)
    assert patch["in_inbox"] is False
    assert patch["status"] == "in_progress"
    # Отдел едет вместе с именем (04.09): кадр обновляет строку списка у всех
    # тринадцати, и подпись там обязана совпасть с шапкой ленты.
    assert patch["assignee"] == {
        "id": str(manager.id),
        "full_name": manager.full_name,
        "department": manager.department,
    }
    assert patch["claimed_by"] == patch["assignee"]
    assert patch["claimed_at"] == "2026-08-06T09:00:00.000Z"


async def test_claiming_an_escalated_dialog_clears_the_mark(
    db_sessionmaker, users_by_role, make_conv
):
    """«Никто не берёт» на принятом диалоге висело навсегда и прятало счётчик
    непрочитанного в его строке."""
    conv = await make_conv(escalated_at=T0 - timedelta(minutes=5))

    async with db_sessionmaker() as s:
        await inbox.claim(s, conv.id, users_by_role["manager"], now=T0)
        await s.commit()

    assert (await _row(db_sessionmaker, conv.id)).escalated_at is None
