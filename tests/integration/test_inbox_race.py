"""Очередь «Входящие» на настоящем PostgreSQL (07 §1.2): гонка за диалог,
миграция 0007 и её бэкофилл, массивы отказов.

Ради чего этот файл существует. Вся 7.1 держится на одном утверждении: двое
операторов, нажавшие «Принять» одновременно, обязаны получить РАЗНЫЙ результат.
Проверить это рассуждением нельзя, и юнитами на SQLite тоже нельзя — там один
поток, одно соединение и ``FOR UPDATE``, скомпилированный в пустоту. Поэтому
здесь настоящая база, настоящие отдельные соединения и настоящий
``asyncio.gather``: тринадцать операторов на девяти каналах устраивают эту
гонку по несколько раз в день.

Что ещё проверяется только здесь:

* **миграция 0007 целиком** — колонки, частичные предикаты трёх индексов и
  БЭКОФИЛЛ. Бэкофилл прогоняется по-настоящему: ``downgrade`` до 0006, строки
  «как до 7.1», ``upgrade`` обратно. Ошибка в нём означает, что в день деплоя
  тринадцать операторов увидят во «Входящих» всю текущую работу друг друга;
* **``text[]`` и оператор ``@>``** — «не отклонён мной» на SQLite вырождается в
  LIKE по JSON-строке, то есть проверяет не тот код, который поедет в бой;
* **потерянное обновление списка отказов**: без ``FOR UPDATE`` два
  одновременных отказа затирают друг друга, и SQLite этого не покажет;
* **частичный индекс очереди реально выбирается планировщиком** на таблице,
  где очередь — доли процента строк. Ровно так выглядит рабочая база: 454 000
  диалогов (15 §1) и десятки ждущих.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import ApiError
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, User
from app.scheduler.partitions import PartitionCoverage
from app.services import inbox
from app.services.inbound import apply_inbound_event
from tests.integration.conftest import REPO_ROOT, requires_docker

pytestmark = requires_docker

T0 = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)
OPERATORS = 10  # столько же, сколько людей в реальной смене (15 §1)


# ------------------------------------------------------------------ инфраструктура


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    # Сообщения этих тестов датированы T0: партиция под его месяц заводится
    # здесь, а не достаётся от соседнего набора — иначе файл, запущенный
    # отдельно, падал на «no partition of relation messages».
    await PartitionCoverage(engine).ensure(T0)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    """Настоящий Redis — его требует подпись конвейера входящих (07 §1.2)."""
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE messages, conversations, clients, avito_accounts, users, "
                "audit_log, notifications, notification_reads CASCADE"
            )
        )


@pytest.fixture
async def operators(sessionmaker) -> list[User]:
    """Смена операторов плюс наблюдатель и руководитель — как в жизни."""
    made: list[User] = []
    async with sessionmaker() as s:
        for i in range(OPERATORS):
            row = User(
                email=f"op{i}@inbox.test",
                password_hash="x",
                full_name=f"Оператор {i}",
                role="manager",
                is_active=True,
            )
            s.add(row)
            made.append(row)
        for key, role in (("head", "head"), ("obs", "observer")):
            s.add(
                User(
                    email=f"{key}@inbox.test",
                    password_hash="x",
                    full_name=key,
                    role=role,
                    is_active=True,
                )
            )
        await s.commit()
    return made


@pytest.fixture
async def account(sessionmaker) -> AvitoAccount:
    async with sessionmaker() as s:
        row = AvitoAccount(
            title="LP-Гонка",
            avito_user_id=987654321,
            access_token_enc=b"enc",
            refresh_token_enc=b"enc",
            token_expires_at=T0 + timedelta(days=1),
            status="active",
            webhook_secret="whsec",
        )
        s.add(row)
        await s.commit()
        return row


@pytest.fixture
def make_conv(sessionmaker, account):
    async def _make(
        *,
        offered_at: datetime | None = T0 - timedelta(minutes=5),
        awaiting_since: datetime | None = None,
        assignee_id: uuid.UUID | None = None,
        claimed_by_id: uuid.UUID | None = None,
        status: str = "new",
        suffix: str | None = None,
    ) -> Conversation:
        tag = suffix or uuid.uuid4().hex[:8]
        async with sessionmaker() as s:
            client = Client(channel="avito", external_id=f"cl-{tag}", name="Иван Петров")
            s.add(client)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{tag}",
                account_id=account.id,
                client_id=client.id,
                status=status,
                assignee_id=assignee_id,
                claimed_by_id=claimed_by_id,
                offered_at=offered_at,
                awaiting_since=awaiting_since,
                declined_by=[],
                unread_count=1,
                last_message_at=offered_at or T0,
            )
            s.add(conv)
            await s.commit()
            return conv

    return _make


async def _row(sessionmaker, conv_id: uuid.UUID) -> Conversation:
    async with sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        assert row is not None
        return row


# --------------------------------------------------------------------- ГОНКА


async def _claim_attempt(sessionmaker, conv_id: uuid.UUID, user: User) -> tuple[bool, str]:
    """Одна попытка принятия в СВОЁЙ сессии и своём соединении."""
    async with sessionmaker() as db:
        try:
            await inbox.claim(db, conv_id, user, now=T0)
            await db.commit()
            return True, ""
        except ApiError as exc:
            await db.rollback()
            return False, exc.code


async def test_ten_operators_press_accept_at_once_and_exactly_one_wins(
    sessionmaker, operators, make_conv
):
    """Ядро блока 7.1: победитель ровно один, остальные видят «уже принят».

    Именно ради этой строчки принятие сделано одним условным ``UPDATE``. Если
    бы оно было «SELECT, проверил, UPDATE», здесь бы прошли несколько — и в
    жизни это означало бы двух операторов, пишущих одному клиенту.
    """
    conv = await make_conv()

    outcomes = await asyncio.gather(
        *(_claim_attempt(sessionmaker, conv.id, op) for op in operators)
    )

    winners = [ok for ok, _ in outcomes if ok]
    codes = {code for ok, code in outcomes if not ok}
    assert len(winners) == 1, f"диалог взяли {len(winners)} раз — гонка не закрыта"
    assert codes == {"already_claimed"}, f"проигравшие получили не тот ответ: {codes}"

    row = await _row(sessionmaker, conv.id)
    assert row.claimed_by_id is not None
    assert row.assignee_id == row.claimed_by_id
    assert row.status == "in_progress"
    assert row.claimed_at is not None


async def test_the_race_leaves_exactly_one_feed_record_and_one_journal_row(
    sessionmaker, operators, make_conv
):
    """Побочные эффекты тоже одни: девять откатов не должны оставить следов."""
    conv = await make_conv()
    await asyncio.gather(*(_claim_attempt(sessionmaker, conv.id, op) for op in operators))

    async with sessionmaker() as s:
        feed = (
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == conv.id, Message.direction == "system"
                    )
                )
            )
            .scalars()
            .all()
        )
        journal = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == str(conv.id),
                        AuditLog.action == "conversation.assigned",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(feed) == 1, "проигравшие оставили записи в ленте"
    assert feed[0].body is not None and feed[0].body.startswith("Диалог принят:")
    assert len(journal) == 1, "в журнале больше одного принятия"
    assert journal[0].details is not None and journal[0].details["source"] == "inbox"


async def test_the_loser_is_told_who_took_it(sessionmaker, operators, make_conv):
    """Проигравший видит ИМЯ победителя, а не «конфликт данных».

    Отдельным тестом, потому что ошибку легко испортить незаметно: если
    диагностический ``SELECT`` возьмёт объект из identity map своей сессии, он
    вернёт устаревшую копию — и сообщение назовёт победителем «никого».
    """
    conv = await make_conv()
    winner, loser = operators[0], operators[1]

    async with sessionmaker() as losing_db:
        # Сессия проигравшего сначала ЧИТАЕТ диалог свободным — так и бывает:
        # он открыл очередь до того, как коллега нажал «Принять». С этого
        # момента у него в identity map лежит устаревшая копия строки.
        stale = await losing_db.get(Conversation, conv.id)
        assert stale is not None and stale.claimed_by_id is None

        async with sessionmaker() as db:
            await inbox.claim(db, conv.id, winner, now=T0)
            await db.commit()

        with pytest.raises(ApiError) as exc:
            await inbox.claim(losing_db, conv.id, loser, now=T0)
        await losing_db.rollback()

    assert exc.value.code == "already_claimed"
    assert winner.full_name in exc.value.message
    assert (exc.value.details or {})["claimed_by"]["id"] == str(winner.id)


async def test_two_operators_taking_two_dialogs_do_not_block_each_other(
    sessionmaker, operators, make_conv
):
    """Замок обязан быть на строке, а не на очереди целиком."""
    first = await make_conv(suffix="a")
    second = await make_conv(suffix="b")
    results = await asyncio.gather(
        _claim_attempt(sessionmaker, first.id, operators[0]),
        _claim_attempt(sessionmaker, second.id, operators[1]),
    )
    assert [ok for ok, _ in results] == [True, True]


async def test_a_dialog_already_in_work_survives_the_storm(sessionmaker, operators, make_conv):
    """Совместимость под нагрузкой: чужой диалог не отнимается ни одним из десяти."""
    owner = operators[0]
    conv = await make_conv(assignee_id=owner.id, claimed_by_id=owner.id, status="in_progress")
    outcomes = await asyncio.gather(
        *(_claim_attempt(sessionmaker, conv.id, op) for op in operators[1:])
    )
    assert not any(ok for ok, _ in outcomes)
    row = await _row(sessionmaker, conv.id)
    assert row.assignee_id == owner.id


# ------------------------------------------------------- отказы: массив text[]


async def test_declined_is_invisible_to_the_decliner_and_visible_to_the_rest(
    sessionmaker, operators, make_conv
):
    """``NOT (declined_by @> ARRAY[id])`` на настоящем массиве PostgreSQL."""
    conv = await make_conv()
    me, colleague = operators[0], operators[1]

    async with sessionmaker() as db:
        await inbox.decline(db, conv.id, me, "не мой канал", now=T0)
        await db.commit()

    async with sessionmaker() as db:
        mine, _ = await inbox.list_inbox(db, me, now=T0)
        theirs, _ = await inbox.list_inbox(db, colleague, now=T0)
        # ⚠ ТОТ ЖЕ `now=T0`, ЧТО И У СПИСКА ВЫШЕ. Без него список считается на T0, а
        # счётчик — на настоящее «сейчас»: отказу к моменту прогона неделя, трёхминутное
        # окно (правка 13.08) давно истекло, и диалог возвращается в счёт. Пока отказ был
        # вечным, момент проверки ничего не решал, и аргумент был не нужен.
        assert await inbox.inbox_count(db, me, now=T0) == 0
        assert await inbox.inbox_count(db, colleague, now=T0) == 1
    assert mine == []
    assert [i["id"] for i in theirs] == [str(conv.id)]


async def test_parallel_declines_do_not_lose_each_other(sessionmaker, operators, make_conv):
    """Потерянное обновление списка отказов — то, чего SQLite показать не может.

    Читать-менять-писать список без блокировки строки означает, что из десяти
    одновременных отказов в базе останется один-два, и диалог будет вечно
    всплывать у людей, которые от него уже отказались.
    """
    conv = await make_conv()

    async def decline(op: User) -> None:
        async with sessionmaker() as db:
            await inbox.decline(db, conv.id, op, now=T0)
            await db.commit()

    await asyncio.gather(*(decline(op) for op in operators))

    row = await _row(sessionmaker, conv.id)
    assert sorted(row.declined_by) == sorted(str(op.id) for op in operators)


async def test_when_all_who_can_answer_declined_the_dialog_stays_and_admins_are_called(
    sessionmaker, operators, make_conv
):
    """Эскалация на настоящей базе: диалог остаётся в очереди с пометкой."""
    async with sessionmaker() as s:
        s.add(
            User(
                email="boss@inbox.test",
                password_hash="x",
                full_name="Админ",
                role="admin",
                is_active=True,
            )
        )
        await s.commit()
        admin = (
            (await s.execute(select(User).where(User.email == "boss@inbox.test"))).scalars().one()
        )

    conv = await make_conv()
    for op in [*operators, admin]:
        async with sessionmaker() as db:
            result = await inbox.decline(db, conv.id, op, now=T0)
            await db.commit()

    assert result.escalated is True
    row = await _row(sessionmaker, conv.id)
    assert row.escalated_at is not None
    assert inbox.is_waiting(row), "брошенный диалог обязан остаться в очереди"

    async with sessionmaker() as db:
        notifications = (
            await db.execute(text("SELECT audience, kind, entity_id FROM notifications"))
        ).all()
    assert len(notifications) == 1
    assert notifications[0][0] == "admin"
    assert notifications[0][2] == str(conv.id)


# ------------------------------------------------- наполнение очереди конвейером
#
# Гонка проверяет, что диалог нельзя взять дважды. Эти два теста проверяют то,
# без чего гонки не случится вовсе: что диалог в очереди ВООБЩЕ появляется —
# настоящим трактом входящих, на настоящем PostgreSQL с настоящим `text[]`.


async def test_the_inbound_pipeline_puts_a_new_dialog_into_the_queue(
    sessionmaker, operators, account, redis
):
    """Вебхук → `apply_inbound_event` → строка в очереди у каждого оператора.

    Юниты этот путь проверяют на SQLite; здесь важно, что колонки 0007 и
    предикат частичного индекса работают на живой базе — то есть очередь после
    деплоя наполняется, а не остаётся с тем, что засеял бэкофилл.
    """
    event = SimpleNamespace(
        chat_id="chat-pipeline-1",
        message_id="am-pipeline-1",
        author_id=999321,
        text="Здравствуйте! Почём замена экрана?",
        created_at=T0 - timedelta(minutes=7),
        client_name="Иван Петров",
        item_title="Ремонт iPhone 13",
        item_url=None,
        item_price=None,
        attachments=[],
    )
    async with sessionmaker() as db:
        assert await apply_inbound_event(db, redis, account, event, publish=False) is True

    async with sessionmaker() as db:
        row = (
            (
                await db.execute(
                    select(Conversation).where(Conversation.external_chat_id == "chat-pipeline-1")
                )
            )
            .scalars()
            .one()
        )
        items, total = await inbox.list_inbox(db, operators[0], now=T0)
    assert row.offered_at is not None and inbox.is_waiting(row)
    assert total == 1 and items[0]["id"] == str(row.id)
    assert items[0]["waiting_seconds"] == 7 * 60
    assert items[0]["client"]["name"] == "Иван Петров"


async def test_a_returning_client_wipes_the_declines_in_a_real_text_array(
    sessionmaker, operators, account, redis, make_conv
):
    """Клиент вернулся — `declined_by` обнуляется в НАСТОЯЩЕМ массиве text[].

    На SQLite список отказов — JSON-строка, и «обнулили» там проверяет не тот
    код, который поедет в бой. Цена ошибки конкретная: вернувшегося клиента не
    увидит ровно тот, кто однажды от него отказался.
    """
    async with sessionmaker() as db:
        client = Client(channel="avito", external_id="ret-1", name="Мария Соколова")
        db.add(client)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-return-1",
            account_id=account.id,
            client_id=client.id,
            status="closed",
            assignee_id=operators[0].id,
            claimed_by_id=operators[0].id,
            claimed_at=T0 - timedelta(days=1),
            offered_at=T0 - timedelta(days=1),
            declined_by=[str(op.id) for op in operators],
            escalated_at=T0 - timedelta(days=1),
            last_message_at=T0 - timedelta(days=1),
        )
        db.add(conv)
        await db.commit()

    back = T0 + timedelta(minutes=5)
    event = SimpleNamespace(
        chat_id="chat-return-1",
        message_id="am-return-2",
        author_id=999322,
        text="Я всё-таки решился, когда сможете принять?",
        created_at=back,
        client_name="Мария Соколова",
        item_title=None,
        item_url=None,
        item_price=None,
        attachments=[],
    )
    async with sessionmaker() as db:
        assert await apply_inbound_event(db, redis, account, event, publish=False) is True

    row = await _row(sessionmaker, conv.id)
    assert row.status == "new"
    assert row.declined_by == [], "вчерашние отказы прячут вернувшегося клиента"
    assert row.escalated_at is None
    assert row.claimed_by_id is None and row.assignee_id is None
    assert row.offered_at == back  # ожидание считается с возврата

    async with sessionmaker() as db:
        for op in operators:  # видят ВСЕ, включая отказавшихся вчера
            assert await inbox.inbox_count(db, op) == 1


# -------------------------------------------------------------------- возврат


async def test_release_puts_the_dialog_back_for_everyone(sessionmaker, operators, make_conv):
    offered = T0 - timedelta(minutes=45)
    # Клиент ждёт ответа с того же сообщения, с которого диалог встал в очередь.
    conv = await make_conv(offered_at=offered, awaiting_since=offered)
    owner, colleague = operators[0], operators[1]

    async with sessionmaker() as db:
        await inbox.claim(db, conv.id, owner, now=T0)
        await db.commit()
    async with sessionmaker() as db:
        assert await inbox.inbox_count(db, colleague) == 0
    async with sessionmaker() as db:
        await inbox.release(db, conv.id, owner, now=T0 + timedelta(minutes=2))
        await db.commit()

    row = await _row(sessionmaker, conv.id)
    assert row.claimed_by_id is None and row.assignee_id is None and row.status == "new"
    assert row.offered_at == offered  # ожидание клиента не обнуляется
    async with sessionmaker() as db:
        assert await inbox.inbox_count(db, colleague) == 1
        assert await inbox.inbox_count(db, owner) == 1


async def test_release_of_an_answered_dialog_starts_waiting_from_the_release(
    sessionmaker, operators, make_conv
):
    """Место в очереди — от того, как давно клиент ждёт СЕЙЧАС (a9f46fc, 24.09).

    Клиенту уже ответили (`awaiting_since` пуст): диалог, который вели три
    часа, не должен вставать в очередь с утренним временем и тут же звать
    администраторов «ждёт в очереди 3 ч».
    """
    conv = await make_conv(offered_at=T0 - timedelta(hours=3), awaiting_since=None)
    released_at = T0 + timedelta(minutes=2)
    async with sessionmaker() as db:
        await inbox.claim(db, conv.id, operators[0], now=T0)
        await db.commit()
    async with sessionmaker() as db:
        await inbox.release(db, conv.id, operators[0], now=released_at)
        await db.commit()

    assert (await _row(sessionmaker, conv.id)).offered_at == released_at


async def test_claim_bumps_updated_at_so_polling_sees_it(sessionmaker, operators, make_conv):
    """``?updated_since=`` (01 §5.1) обязан заметить принятие: строка изменилась."""
    conv = await make_conv()
    before = (await _row(sessionmaker, conv.id)).updated_at
    async with sessionmaker() as db:
        await inbox.claim(db, conv.id, operators[0], now=T0)
        await db.commit()
    assert (await _row(sessionmaker, conv.id)).updated_at > before


# ------------------------------------------------------------------ миграция 0007


async def test_migration_added_the_queue_columns(pg_engine):
    async with pg_engine.connect() as conn:
        cols = {
            r[0]: (r[1], r[2])
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns WHERE table_name = 'conversations'"
                    )
                )
            ).all()
        }
    assert cols["offered_at"][0] == "timestamp with time zone"
    assert cols["claimed_at"][0] == "timestamp with time zone"
    assert cols["escalated_at"][0] == "timestamp with time zone"
    assert cols["claimed_by_id"][0] == "uuid"
    # Список отказавшихся не бывает «неизвестен» — пустой массив, а не NULL:
    # иначе предикат «не отклонён мной» пришлось бы писать с оглядкой на NULL.
    assert cols["declined_by"] == ("ARRAY", "NO")


async def test_the_queue_indexes_are_partial_by_the_queue_predicate(pg_engine):
    """Индекс без своего ``WHERE`` — уже не тот индекс.

    Частичность здесь и есть смысл: очередь — это десятки строк на фоне сотен
    тысяч закрытых и разобранных, и индекс обязан быть размером с очередь.
    """
    async with pg_engine.connect() as conn:
        defs = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'conversations'"
                    )
                )
            ).all()
        }
    for name in ("ix_conversations_inbox_wait", "ix_conversations_inbox_account"):
        assert name in defs, name
        predicate = defs[name]
        assert "claimed_by_id IS NULL" in predicate, name
        assert "assignee_id IS NULL" in predicate, name
        assert "offered_at IS NOT NULL" in predicate, name
        assert "closed" in predicate, name
    assert "offered_at" in defs["ix_conversations_inbox_wait"]
    assert "account_id" in defs["ix_conversations_inbox_account"]
    assert "WHERE (claimed_by_id IS NOT NULL)" in defs["ix_conversations_claimed_by"]


async def test_the_queue_query_uses_the_partial_index_not_a_seq_scan(
    pg_engine, sessionmaker, operators, account
):
    """Проверка на масштабе в миниатюре (готовим 7.5).

    Две тысячи строк, из которых в очереди пять. Если планировщик всё равно
    выбирает seq scan, значит частичный индекс не подходит выборке — и на
    четырёхстах тысячах диалогов вкладка «Входящие» будет читать архив.
    """
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO clients (id, channel, external_id, name) "
                "VALUES (gen_random_uuid(), 'avito', 'bulk', 'Массовый')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, channel, external_chat_id, account_id, client_id, status, assignee_id, "
                " claimed_by_id, offered_at, declined_by, last_message_at, updated_at) "
                "SELECT gen_random_uuid(), 'avito', 'bulk-' || i, :account, "
                "       (SELECT id FROM clients WHERE external_id='bulk'), "
                "       CASE WHEN i % 2 = 0 THEN 'closed' ELSE 'in_progress' END, "
                "       :owner, :owner, now() - (i || ' minutes')::interval, '{}'::text[], "
                "       now(), now() "
                "FROM generate_series(1, 2000) AS i"
            ),
            {"account": str(account.id), "owner": str(operators[0].id)},
        )
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, channel, external_chat_id, account_id, client_id, status, offered_at, "
                " declined_by, last_message_at, updated_at) "
                "SELECT gen_random_uuid(), 'avito', 'wait-' || i, :account, "
                "       (SELECT id FROM clients WHERE external_id='bulk'), 'new', "
                "       now() - (i || ' minutes')::interval, '{}'::text[], now(), now() "
                "FROM generate_series(1, 5) AS i"
            ),
            {"account": str(account.id)},
        )
        await conn.execute(text("ANALYZE conversations"))

    async with pg_engine.connect() as conn:
        plan = "\n".join(
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "EXPLAIN SELECT id FROM conversations "
                        "WHERE claimed_by_id IS NULL AND assignee_id IS NULL "
                        "  AND offered_at IS NOT NULL AND status <> 'closed' "
                        "ORDER BY offered_at ASC LIMIT 50"
                    )
                )
            ).all()
        )
    assert "ix_conversations_inbox" in plan, f"очередь читается мимо индекса:\n{plan}"


async def test_the_backfill_keeps_dialogs_in_work_out_of_the_queue(pg_async_url, sessionmaker):
    """Настоящий прогон бэкофилла: 0006 → строки «как до 7.1» → 0007.

    День деплоя выглядит именно так. Ошибка здесь означает, что тринадцать
    операторов увидят во «Входящих» всю текущую работу друг друга, а
    исторический архив (backfill из Авито, статус ``closed``) засыплет очередь.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", pg_async_url)

    # env.py поднимает свой цикл (`asyncio.run`) — из работающего цикла теста
    # его надо звать в отдельном потоке, иначе RuntimeError вместо миграции.
    async def alembic(action, revision: str) -> None:
        await asyncio.to_thread(action, cfg, revision)

    await alembic(command.downgrade, "0006")
    try:
        from sqlalchemy.ext.asyncio import create_async_engine as _engine

        engine = _engine(pg_async_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, full_name, role, is_active) "
                    "VALUES (:id, 'legacy@inbox.test', 'x', 'Старожил', 'manager', true)"
                ),
                {"id": str(uuid.uuid4())},
            )
            await conn.execute(
                text(
                    "INSERT INTO avito_accounts (id, title, avito_user_id, access_token_enc, "
                    " refresh_token_enc, token_expires_at, status, webhook_secret) "
                    "VALUES (gen_random_uuid(), 'LP-Старый', 424242, 'x', 'x', now(), "
                    "        'active', 'w')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO clients (id, channel, external_id, name) "
                    "VALUES (gen_random_uuid(), 'avito', 'legacy', 'Клиент')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, channel, external_chat_id, account_id, client_id, status, assignee_id, "
                    " last_message_at, updated_at) "
                    "SELECT gen_random_uuid(), 'avito', v.chat, a.id, c.id, v.status, "
                    "       CASE WHEN v.owned THEN u.id ELSE NULL END, v.last_at, now() "
                    "FROM (VALUES "
                    "        ('in-work', 'in_progress', true,  now() - interval '3 hours'), "
                    "        ('waiting', 'new',         false, now() - interval '2 hours'), "
                    "        ('archive', 'closed',      false, now() - interval '90 days') "
                    "     ) AS v(chat, status, owned, last_at), "
                    "     avito_accounts a, clients c, users u "
                    "WHERE a.avito_user_id = 424242 AND c.external_id = 'legacy' "
                    "  AND u.email = 'legacy@inbox.test'"
                )
            )
        await engine.dispose()
    finally:
        await alembic(command.upgrade, "head")

    async with sessionmaker() as db:
        rows = {
            c.external_chat_id: c for c in (await db.execute(select(Conversation))).scalars().all()
        }
        operator = (
            (await db.execute(select(User).where(User.email == "legacy@inbox.test")))
            .scalars()
            .one()
        )

    in_work = rows["in-work"]
    assert in_work.claimed_by_id == in_work.assignee_id, "диалог в работе не считается принятым"
    assert in_work.claimed_at is not None
    assert not inbox.is_waiting(in_work), "чужая работа всплыла во «Входящих»"

    waiting = rows["waiting"]
    assert waiting.offered_at is not None
    assert inbox.is_waiting(waiting)
    # Время ожидания честное — от последнего сообщения клиента, а не от миграции:
    # иначе после деплоя вся очередь встала бы «в одну секунду» и порядок
    # «дольше всех ждущий первым» потерялся бы.
    assert waiting.offered_at == waiting.last_message_at

    archive = rows["archive"]
    assert archive.offered_at is None, "исторический архив засыпал очередь"
    assert not inbox.is_waiting(archive)

    async with sessionmaker() as db:
        items, total = await inbox.list_inbox(db, operator, now=T0)
    assert total == 1
    assert [i["id"] for i in items] == [str(waiting.id)]
