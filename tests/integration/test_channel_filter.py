"""Назначение операторов на каналы на настоящем PostgreSQL (07 §1.2, план 7.2).

Ради чего этот файл существует. Юниты блока 7.2 идут на SQLite, а весь блок
держится на двух утверждениях, которые SQLite проверить не может в принципе:

* **``ON DELETE CASCADE`` с обеих сторон связи.** SQLite по умолчанию внешние
  ключи не исполняет вовсе, так что «тест каскада» на нём проверял бы не тот
  код, который поедет в бой. А цена ошибки здесь максимальная и тихая: строка
  уволенного, оставшаяся в ``account_operators``, держит канал
  «назначенным» — и правило «канал без операторов доступен всем» не
  срабатывает. Обращения этого канала не увидит НИКТО, и ни одной ошибки в
  логах при этом не будет;
* **фильтр очереди как SQL.** Видимость канала — коррелированный ``EXISTS``
  поверх частичного индекса очереди (0007), а «не отклонён мной» на PostgreSQL
  это ``declined_by @> ARRAY[id]``, тогда как на SQLite — LIKE по JSON-строке.
  Оба сужения работают в одном ``WHERE``, и проверять их надо там, где они
  выполняются по-настоящему.

Плюс то, что положено проверять на живой базе всегда: миграция 0008 (ключ,
индекс, тип и правила внешних ключей), составной первичный ключ как настоящее
ограничение целостности и план запроса очереди — фильтрация не должна стоить
чтения архива.

ГЛАВНЫЙ ТЕСТ ФАЙЛА — про отсутствие фильтрации, а не про неё:
``test_a_channel_without_operators_is_open_to_everyone``. Пустая таблица
назначений означает «все каналы открыты всем», а не «никому». Обратное
прочтение стоило бы ровно одного деплоя: фильтрация включается, очередь у всех
тринадцати пустеет, обращения виснут молча.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AccountOperator, AvitoAccount, Client, Conversation, Message, User
from app.models.notification import Notification
from app.services import account_operators as acc_ops
from app.services import inbox
from tests.integration.conftest import REPO_ROOT, requires_docker

pytestmark = requires_docker

T0 = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)

# Масштаб заказчика (15 §1): девять каналов Авито и тринадцать операторов.
CHANNELS = 9
OPERATORS = 13


# ------------------------------------------------------------------ инфраструктура


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE account_operators, messages, conversations, clients, "
                "avito_accounts, users, audit_log, notifications, notification_reads CASCADE"
            )
        )


@pytest.fixture
async def channels(sessionmaker) -> list[AvitoAccount]:
    """Девять каналов заказчика — столько же, сколько в жизни."""
    async with sessionmaker() as s:
        rows = [
            AvitoAccount(
                title=f"Канал-{i}",
                avito_user_id=880000000 + i,
                access_token_enc=b"enc",
                refresh_token_enc=b"enc",
                token_expires_at=T0 + timedelta(days=1),
                status="active",
                webhook_secret=f"whsec-{i}",
            )
            for i in range(CHANNELS)
        ]
        s.add_all(rows)
        await s.commit()
        for row in rows:
            await s.refresh(row)
        return rows


@pytest.fixture
async def crew(sessionmaker) -> list[User]:
    """Тринадцать операторов плюс админ — ровно смена заказчика."""
    async with sessionmaker() as s:
        rows = [
            User(
                email=f"op{i:02d}@channels.test",
                password_hash="x",
                full_name=f"Оператор {i:02d}",
                role="manager",
                is_active=True,
            )
            for i in range(OPERATORS)
        ]
        for row in rows:
            s.add(row)
        s.add(
            User(
                email="admin@channels.test",
                password_hash="x",
                full_name="Администратор",
                role="admin",
                is_active=True,
            )
        )
        await s.commit()
        for row in rows:
            await s.refresh(row)
        return rows


@pytest.fixture
async def admin(sessionmaker, crew) -> User:
    async with sessionmaker() as s:
        return (
            (await s.execute(select(User).where(User.email == "admin@channels.test")))
            .scalars()
            .one()
        )


@pytest.fixture
def make_conv(sessionmaker):
    """Диалог, ждущий принятия, на заданном канале."""

    async def _make(account_id: uuid.UUID, *, suffix: str | None = None) -> Conversation:
        tag = suffix or uuid.uuid4().hex[:8]
        async with sessionmaker() as s:
            client = Client(channel="avito", external_id=f"cl-{tag}", name="Иван Петров")
            s.add(client)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{tag}",
                account_id=account_id,
                client_id=client.id,
                status="new",
                offered_at=T0 - timedelta(minutes=5),
                declined_by=[],
                unread_count=1,
                last_message_at=T0 - timedelta(minutes=5),
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"am-{tag}",
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте! Почём ремонт?",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=T0 - timedelta(minutes=5),
                )
            )
            await s.commit()
            return conv

    return _make


@pytest.fixture
def assign(sessionmaker):
    """Назначить людей на канал напрямую — тесты фильтрации не должны зависеть
    от валидации формы назначения."""

    async def _assign(account_id: uuid.UUID, *users: User) -> None:
        async with sessionmaker() as s:
            for u in users:
                s.add(AccountOperator(account_id=account_id, user_id=u.id))
            await s.commit()

    return _assign


async def queue_of(sessionmaker, user: User) -> list[str]:
    async with sessionmaker() as s:
        items, _ = await inbox.list_inbox(s, user, now=T0)
    return [i["id"] for i in items]


# ------------------------------------------- ПРАВИЛО СОВМЕСТИМОСТИ на настоящей базе


async def test_a_channel_without_operators_is_open_to_everyone(
    sessionmaker, crew, channels, make_conv
):
    """Главный тест блока: пустая таблица назначений = «видно ВСЕМ».

    Именно это состояние будет в базе в день деплоя 7.2 — миграция 0008
    создаёт таблицу и НИЧЕГО в неё не пишет. Если бы пустой набор читался как
    «никому», очередь у всех тринадцати операторов оказалась бы пустой, а
    обращения клиентов повисли бы молча: ни ошибки, ни исключения, ни строки
    в логе — просто ничего не приходит.
    """
    conv = await make_conv(channels[0].id)

    async with sessionmaker() as s:
        assert (await s.execute(select(AccountOperator))).scalars().all() == [], (
            "миграция 0008 не должна была ничего назначать"
        )

    for who in crew:
        assert await queue_of(sessionmaker, who) == [str(conv.id)], who.full_name


async def test_the_operator_sees_his_channels_plus_the_common_ones(
    sessionmaker, crew, channels, make_conv, assign
):
    """Очередь = свои каналы ПЛЮС ничьи. Именно плюс, а не только свои.

    Иначе канал, который забыли раздать (девятый из девяти, подключённый в
    пятницу), исчезает из очереди у всех сразу.
    """
    await assign(channels[0].id, crew[0])
    mine = await make_conv(channels[0].id, suffix="mine")
    common = await make_conv(channels[8].id, suffix="common")  # на него не назначен никто

    assert set(await queue_of(sessionmaker, crew[0])) == {str(mine.id), str(common.id)}
    assert await queue_of(sessionmaker, crew[1]) == [str(common.id)]


async def test_the_two_narrowings_work_together_in_one_where(
    sessionmaker, crew, channels, make_conv, assign
):
    """«Мой канал» и «не отклонён мной» — в одном ``WHERE`` на настоящих типах.

    На PostgreSQL отказ ищется оператором массива (``declined_by @> ARRAY[id]``),
    на SQLite юнитов — LIKE по JSON-строке. Проверять их совместную работу надо
    там, где выполняется настоящий предикат: очередь обязана сузиться дважды,
    а не «или по каналу, или по отказу».
    """
    await assign(channels[0].id, crew[0], crew[1])
    first = await make_conv(channels[0].id, suffix="first")
    second = await make_conv(channels[0].id, suffix="second")

    async with sessionmaker() as s:
        await inbox.decline(s, first.id, crew[0], now=T0)
        await s.commit()

    assert await queue_of(sessionmaker, crew[0]) == [str(second.id)]
    assert set(await queue_of(sessionmaker, crew[1])) == {str(first.id), str(second.id)}
    async with sessionmaker() as s:
        # ⚠ `now=T0` ОБЯЗАТЕЛЕН, И ЗАБЫТ ОН БЫЛ НЕ СЛУЧАЙНО. Пока отказ был вечным,
        # момент проверки ничего не решал, и счётчик спрашивали без него. С трёхминутным
        # окном (правка 13.08) список считался на T0, а счётчик — на настоящее «сейчас»:
        # отказу к тому времени неделя, окно давно истекло, и диалог возвращался в
        # счёт. Тест сравнивал список и счётчик в РАЗНЫЕ моменты времени и краснел
        # на верном коде.
        assert await inbox.inbox_count(s, crew[0], now=T0) == 1, (
            "счётчик обязан совпадать со списком"
        )


async def test_the_admin_keeps_the_whole_queue(
    sessionmaker, admin, crew, channels, make_conv, assign
):
    """Администратору очередь не сужается — он разбирает эскалацию (01 §5.5)."""
    await assign(channels[0].id, crew[0])
    await assign(channels[1].id, crew[1])
    first = await make_conv(channels[0].id, suffix="a")
    second = await make_conv(channels[1].id, suffix="b")

    assert set(await queue_of(sessionmaker, admin)) == {str(first.id), str(second.id)}


# --------------------------------------------------------------- каскады (только PG)


async def test_firing_the_last_operator_returns_the_channel_to_everyone(
    sessionmaker, pg_engine, crew, channels, make_conv, assign
):
    """Удалили сотрудника — канал снова общий. Это делает база, а не код.

    Худшее из возможных состояний выглядело бы так: строка уволенного осталась,
    канал считается «назначенным», живые операторы его не видят, а тот, кто
    видит, в системе больше не появится. Обращения канала не увидел бы никто —
    молча. ``ON DELETE CASCADE`` закрывает это на уровне базы, а не на
    добросовестности кода, удаляющего пользователя. SQLite юнитов внешние ключи
    не исполняет, поэтому проверка живёт здесь.
    """
    await assign(channels[0].id, crew[0])
    conv = await make_conv(channels[0].id)
    assert await queue_of(sessionmaker, crew[1]) == [], "пока канал назначен — чужим он не виден"

    async with pg_engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(crew[0].id)})

    async with sessionmaker() as s:
        assert (await s.execute(select(AccountOperator))).scalars().all() == [], (
            "связь пережила удаление сотрудника — канал остался «назначенным призраку»"
        )
    assert await queue_of(sessionmaker, crew[1]) == [str(conv.id)]


async def test_deleting_a_channel_takes_its_assignments_with_it(
    sessionmaker, pg_engine, crew, channels, assign
):
    """Назначения на несуществующий канал ловить потом в фильтре очереди нечем."""
    await assign(channels[0].id, crew[0], crew[1])

    async with pg_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM avito_accounts WHERE id = :id"), {"id": str(channels[0].id)}
        )

    async with sessionmaker() as s:
        assert (await s.execute(select(AccountOperator))).scalars().all() == []


async def test_the_composite_key_forbids_a_duplicate_assignment(sessionmaker, crew, channels):
    """Составной ключ — настоящее ограничение, а не соглашение.

    Ради него от поля-массива и отказались: два администратора, сохраняющие
    один экран одновременно, не могут оставить человека на канале дважды.
    """
    async with sessionmaker() as s:
        s.add(AccountOperator(account_id=channels[0].id, user_id=crew[0].id))
        await s.commit()

    with pytest.raises(IntegrityError):
        async with sessionmaker() as s:
            s.add(AccountOperator(account_id=channels[0].id, user_id=crew[0].id))
            await s.commit()


# ------------------------------------------------------- эскалация на масштабе смены


async def test_escalation_waits_only_for_the_crew_of_this_channel(
    sessionmaker, crew, channels, make_conv, assign
):
    """Девять каналов, тринадцать операторов: отказ ДВОИХ даёт эскалацию.

    До 7.2 «отказались все» означало «отказались все тринадцать». На девяти
    каналах такая эскалация не наступает никогда: одиннадцать человек диалога
    не видят и отказаться от него физически не могут. То есть эскалации нет —
    обращение висит до конца смены, и никто об этом не узнаёт.
    """
    await assign(channels[0].id, crew[0], crew[1])
    for i, operator in enumerate(crew[2:]):
        await assign(channels[1 + i % 8].id, operator)
    conv = await make_conv(channels[0].id)

    assert await queue_of(sessionmaker, crew[0]) == [str(conv.id)]
    assert await queue_of(sessionmaker, crew[5]) == [], "чужой канал не должен попадать в очередь"

    async with sessionmaker() as s:
        first = await inbox.decline(s, conv.id, crew[0], now=T0)
        await s.commit()
    assert first.escalated is False, "один из двух операторов канала — ещё не «все»"

    async with sessionmaker() as s:
        second = await inbox.decline(s, conv.id, crew[1], now=T0)
        await s.commit()
    assert second.escalated is True, (
        "отказались оба оператора канала — администратора зовут сейчас, "
        "а не после одиннадцати человек, которые диалога не видят"
    )

    async with sessionmaker() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert len(rows) == 1 and rows[0].audience == "admin"


async def test_on_a_common_channel_the_escalation_still_waits_for_everyone(
    sessionmaker, admin, crew, channels, make_conv
):
    """Канал без назначений — состав «кому доступен» ровно как до 7.2.

    Без явного fallback'а множество операторов канала было бы пустым, пустое
    вложено в любое — и «отказались все» срабатывало бы после ПЕРВОГО отказа,
    то есть администратора звали бы на каждый отказ подряд.

    Обратная сторона той же монеты — вторая половина теста: тринадцати отказов
    НЕ хватает, потому что диалог доступен ещё и администратору (он тоже
    отвечает клиентам, 01 §12). Состав «кому доступен» здесь — вся компания,
    а не «все менеджеры».
    """
    conv = await make_conv(channels[0].id)
    everyone = [*crew, admin]
    results = []
    for who in everyone:
        async with sessionmaker() as s:
            results.append(await inbox.decline(s, conv.id, who, now=T0))
            await s.commit()

    assert [r.escalated for r in results[:-1]] == [False] * OPERATORS
    assert results[-1].escalated is True


async def test_taking_a_dialog_of_a_foreign_channel_is_refused(
    sessionmaker, crew, channels, make_conv, assign
):
    """Фильтр очереди без серверной проверки принятия — оформление, а не правило."""
    from app.core.errors import ApiError

    await assign(channels[0].id, crew[0])
    await make_conv(channels[0].id)

    async with sessionmaker() as s:
        await acc_ops.assert_can_take_account(s, crew[0], channels[0].id)  # свой — можно
        await acc_ops.assert_can_take_account(s, crew[1], channels[8].id)  # ничей — тоже
        with pytest.raises(ApiError) as exc:
            await acc_ops.assert_can_take_account(s, crew[1], channels[0].id)
    assert exc.value.status == 403
    assert (exc.value.details or {})["reason"] == "channel_not_assigned"


# ------------------------------------------------------------------- миграция 0008


async def test_migration_created_the_link_table_with_its_key_and_index(pg_engine):
    async with pg_engine.connect() as conn:
        cols = {
            r[0]: (r[1], r[2])
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                        " WHERE table_name = 'account_operators'"
                    )
                )
            ).all()
        }
        indexes = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'account_operators'"
                    )
                )
            ).all()
        }
    assert cols["account_id"] == ("uuid", "NO")
    assert cols["user_id"] == ("uuid", "NO")
    # created_at — не украшение: «когда человека сняли с канала» и есть ответ
    # на вопрос разбора «почему обращение висело».
    assert cols["created_at"][0] == "timestamp with time zone"

    assert "pk_account_operators" in indexes
    pk = indexes["pk_account_operators"]
    assert pk.index("account_id") < pk.index("user_id"), (
        "порядок колонок ключа обратный: сторона канала перестанет быть префиксом"
    )
    # Сторона оператора: «мои каналы» читаются, не заглядывая в кучу.
    assert "ix_account_operators_user" in indexes
    user_idx = indexes["ix_account_operators_user"]
    assert user_idx.index("user_id") < user_idx.index("account_id")


async def test_both_foreign_keys_cascade_on_delete(pg_engine):
    """Правило удаления читаем из каталога, а не из текста миграции.

    ``NO ACTION`` вместо ``CASCADE`` не сломает ни одного теста, который не
    удаляет строки, — зато в бою оставит канал назначенным призраку.
    """
    async with pg_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conname, c.confdeltype, t.relname "
                    "FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                    "JOIN pg_class t ON t.oid = c.confrelid "
                    "WHERE r.relname = 'account_operators' AND c.contype = 'f'"
                )
            )
        ).all()
    # confdeltype приезжает как "char" — драйвер отдаёт его байтом.
    by_target = {
        target: (deltype.decode() if isinstance(deltype, bytes) else deltype)
        for _, deltype, target in rows
    }
    assert by_target == {"avito_accounts": "c", "users": "c"}, (
        f"внешние ключи связи не CASCADE: {rows}"
    )


async def test_the_migration_is_neutral_and_reversible(pg_async_url, pg_engine):
    """0008 → 0007 → head: таблица уходит и возвращается пустой.

    Побочная выгода отсутствия бэкофилла: до первого назначения система ведёт
    себя ровно как до 7.2, и девять каналов заказчика включаются по одному, а
    не «все сразу в день деплоя».
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

    async def table_exists() -> bool:
        async with pg_engine.connect() as conn:
            return bool(
                (
                    await conn.execute(text("SELECT to_regclass('public.account_operators')"))
                ).scalar()
            )

    await alembic(command.downgrade, "0007")
    try:
        assert await table_exists() is False, "downgrade оставил таблицу связи"
    finally:
        await alembic(command.upgrade, "head")

    assert await table_exists() is True
    async with pg_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM account_operators"))).scalar() == 0


# ------------------------------------------------------------------- план запроса


async def test_the_channel_filter_does_not_cost_a_scan_of_the_archive(
    pg_engine, sessionmaker, crew, channels, assign
):
    """Фильтр по каналам обязан оставаться поверх частичного индекса очереди.

    Ради этого предикат написан как коррелированный ``EXISTS``, а не как
    ``account_id IN (SELECT ...)``: ``IN`` со списком каналов даёт планировщику
    повод собрать хэш по ``conversations`` целиком, а это чтение архива ради
    счётчика вкладки. Две тысячи строк, из которых в очереди пять, — тот же
    масштаб в миниатюре, что и у проверки 7.1.
    """
    await assign(channels[0].id, crew[0])
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
            {"account": str(channels[0].id), "owner": str(crew[0].id)},
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
            {"account": str(channels[0].id)},
        )
        await conn.execute(text("ANALYZE conversations"))
        await conn.execute(text("ANALYZE account_operators"))

    # Предикат берём тот самый, что исполняет очередь, а не его пересказ:
    # переписанное здесь условие проверяло бы план выдуманного запроса.
    async with sessionmaker() as s:
        query = (
            select(Conversation.id)
            .where(inbox.visible_queue_condition(s, crew[0]))
            .order_by(Conversation.offered_at.asc())
            .limit(50)
        )
        compiled = query.compile(
            dialect=s.get_bind().dialect, compile_kwargs={"literal_binds": True}
        )
        plan = "\n".join(r[0] for r in (await s.execute(text(f"EXPLAIN {compiled}"))).all())

    assert "ix_conversations_inbox" in plan, (
        f"очередь с фильтром каналов читается мимо частичного индекса:\n{plan}"
    )
    assert "Seq Scan on conversations" not in plan, plan
