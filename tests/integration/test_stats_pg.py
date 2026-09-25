"""Статистика на настоящем PostgreSQL (07 §1.2): миграция 0004, MV, каждая
метрика словаря 06 §1 на известных данных.

Юнит-тесты проверяют арифметику отчёта, здесь проверяется SQL: работает ли
``business_seconds_between`` так же, как её питоновский эталон; сходятся ли
MV-вариант FRT и live-эталон 06 §2.2; считает ли каждая метрика ровно то, что
описано в словаре, включая краевые случаи (заметка, ``failed``, переоткрытие,
диалог без ответа, закрытие ботом); проходит ли ``REFRESH ... CONCURRENTLY``;
режутся ли партиции ``messages``.

Фикстура наливает один сценарий, ожидания в тестах посчитаны от него руками —
это и есть «известные данные».
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, User
from app.services import stats as st
from app.services.conversation_table import STATUS_RU as TABLE_STATUS_RU
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

MSK = st.MSK


# ------------------------------------------------------------------ инфраструктура


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )


async def refresh_mv(engine: AsyncEngine) -> None:
    """То же, что делает job ``stats_mv_refresh`` (06 §3.2).

    AUTOCOMMIT обязателен: ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` нельзя
    выполнить внутри транзакционного блока, а ``engine.connect()`` открывает
    транзакцию на первом же ``execute``.
    """
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(st.REFRESH_MV_SQL))


async def ensure_partitions(
    engine: AsyncEngine, months_back: int = 4, months_ahead: int = 1
) -> None:
    """Партиции ``messages`` вокруг сегодняшнего дня — сценарий трогает и
    прошлые месяцы (старый диалог клиента, предыдущий период)."""
    today = datetime.now(UTC).date().replace(day=1)
    cursor = today
    for _ in range(months_back):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    async with engine.begin() as conn:
        for _ in range(months_back + months_ahead + 1):
            end = (cursor + timedelta(days=32)).replace(day=1)
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS messages_y{cursor.year:04d}m{cursor.month:02d} "
                    f"PARTITION OF messages FOR VALUES FROM ('{cursor}') TO ('{end}')"
                )
            )
            cursor = end


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Московские стенные часы указанной даты → UTC-таймстемп для БД."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=MSK).astimezone(UTC)


# ---------------------------------------------------------------------- сценарий


@pytest.fixture
async def seed(pg_engine: AsyncEngine, sessionmaker) -> SimpleNamespace:
    """Один разобранный сценарий: 6 диалогов в периоде + старый + предыдущий период.

    Периоды привязаны к «сегодня» (а не к календарной дате), чтобы прогон в
    любой день был одинаковым: D0 = сегодня − 10 дней, период D0..D0+3.
    """
    await ensure_partitions(pg_engine)

    d0 = st.today_msk() - timedelta(days=10)
    period = st.Period(d0, d0 + timedelta(days=3))
    d1, d2, d3 = d0 + timedelta(days=1), d0 + timedelta(days=2), d0 + timedelta(days=3)
    old_day = d0 - timedelta(days=12)
    prev_day = d0 - timedelta(days=3)

    async with sessionmaker() as s:
        users = {}
        for key, name, role, active in (
            ("u1", "Анна Смирнова", "manager", True),
            ("u2", "Борис Ким", "manager", True),
            ("u3", "Виктор Уволенный", "manager", False),
            ("u4", "Галина Без Активности", "manager", True),
            ("head", "Дмитрий Руководитель", "head", True),
            ("admin", "Администратор", "admin", True),
        ):
            row = User(
                email=f"{key}@stats.test",
                password_hash="x",
                full_name=name,
                role=role,
                is_active=active,
            )
            s.add(row)
            users[key] = row
        await s.flush()

        accounts = {}
        for key, title, avito_id in (
            ("acc1", "LP-Основной", 700100),
            ("acc2", "LP-Второй", 700200),
        ):
            account_row = AvitoAccount(
                title=title,
                avito_user_id=avito_id,
                access_token_enc=b"a",
                refresh_token_enc=b"r",
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
                status="active",
                webhook_secret="s",
            )
            s.add(account_row)
            accounts[key] = account_row
        await s.flush()

        clients = {}
        for idx in range(1, 8):
            client_row = Client(
                channel="avito", external_id=f"stats-cli-{idx}", name=f"Клиент {idx}"
            )
            s.add(client_row)
            clients[idx] = client_row
        await s.flush()

        def conversation(
            key: str,
            account: str,
            client: int,
            assignee,
            status: str,
            last_at: datetime,
            *,
            awaiting: datetime | None = None,
            offered: datetime | None = None,
        ) -> Conversation:
            row = Conversation(
                channel="avito",
                external_chat_id=f"stats-{key}",
                account_id=accounts[account].id,
                client_id=clients[client].id,
                assignee_id=assignee.id if assignee is not None else None,
                status=status,
                last_message_at=last_at,
                # «клиент написал и ответа ещё не получил» — по этой отметке
                # считается карточка «Ждут ответа» (06 §1.2, STATS-01)
                awaiting_since=awaiting,
                # «Диалог реально ставили в очередь» — третье требование
                # `inbox.queue_condition()` наряду с «нет принявшего» и «нет
                # ответственного». Без отметки диалог в очереди не числится,
                # каким бы новым он ни был.
                offered_at=offered,
            )
            s.add(row)
            return row

        convs = {
            # старый диалог клиента 6 — делает его «повторным» в периоде
            "old": conversation("old", "acc1", 6, users["u1"], "closed", at(old_day, 12, 10)),
            # предыдущий период — база для дельт
            "prev": conversation("prev", "acc1", 7, users["u1"], "closed", at(prev_day, 12, 5)),
            # ⚠ «В ОЧЕРЕДИ» — ЭТО СОСТОЯНИЕ, А НЕ СТАТУС «НОВЫЙ» (28.08).
            #
            # Проверка снимка ниже обещает «никем не взяты», а в фикстуре у
            # новых диалогов стоял ответственный и не было `offered_at`. Пока
            # карточка считала `count(*) WHERE status='new'`, это сходилось; с
            # переходом на `inbox.queue_condition()` (нет принявшего, нет
            # ответственного, диалог реально ставили в очередь) — перестало, и
            # обе проверки снимка стали красными. Заметили не сразу: набор идёт
            # только при поднятом Docker.
            #
            # Правим фикстуру, а не ожидание: «в очереди» — это состояние, и
            # тест обязан его создавать, а не подгонять число под то, что
            # получилось. Ответственных у c1 и c5 при этом НЕ трогаем: на них
            # висят метрики менеджеров («начато диалогов»), и обнулив их, мы
            # починили бы одну проверку, сломав соседнюю. Диалоги очереди —
            # отдельные, ничьи по определению (см. c7 и c8 ниже).
            "c1": conversation("c1", "acc1", 1, users["u1"], "new", at(d3, 10)),
            # c2 — единственный, где клиент ждёт ответа прямо сейчас. Три
            # snapshot-величины в сценарии намеренно разные (1 / 2 / 3): равные
            # числа пропустили бы подмену одной карточки другой, а именно так
            # и выглядел STATS-01.
            "c2": conversation(
                "c2", "acc1", 2, users["u2"], "in_progress", at(d1, 10, 5), awaiting=at(d1, 10, 30)
            ),
            "c3": conversation("c3", "acc1", 3, None, "closed", at(d1, 12, 0, 5)),
            "c4": conversation("c4", "acc2", 4, users["u1"], "in_progress", at(d2, 9, 20)),
            "c5": conversation("c5", "acc1", 5, users["u2"], "new", at(d2, 15)),
            "c6": conversation("c6", "acc1", 6, users["u3"], "in_progress", at(d3, 9, 30)),
            # Второй диалог очереди. Числа снимка подобраны разными (1 / 2 / 3)
            # намеренно: равные пропустили бы подмену одной карточки другой —
            # ровно так и выглядел STATS-01.
            "c7": conversation("c7", "acc1", 7, None, "new", at(d2, 16), offered=at(d2, 16)),
            # Клиент тот же, что у c7: восьмой клиент сдвинул бы метрику
            # «повторных», к очереди отношения не имеющую.
            "c8": conversation("c8", "acc1", 7, None, "new", at(d2, 17), offered=at(d2, 17)),
        }
        await s.flush()

        def message(
            conv: str,
            when: datetime,
            direction: str,
            sender_type: str,
            *,
            user=None,
            delivery: str = "delivered",
        ) -> None:
            s.add(
                Message(
                    conversation_id=convs[conv].id,
                    direction=direction,
                    sender_type=sender_type,
                    sender_user_id=user.id if user is not None else None,
                    body="текст",
                    attachments=[],
                    delivery_status=delivery,
                    created_at=when,
                )
            )

        # старый диалог и предыдущий период
        message("old", at(old_day, 12), "in", "client")
        message("old", at(old_day, 12, 10), "out", "operator", user=users["u1"])
        message("prev", at(prev_day, 12), "in", "client")
        message("prev", at(prev_day, 12, 5), "out", "operator", user=users["u1"])

        # c1: ответ за 5 минут в рабочее время; закрыт оператором; клиент вернулся
        message("c1", at(d0, 11), "in", "client")
        message("c1", at(d0, 11, 5), "out", "operator", user=users["u1"])
        message("c1", at(d3, 10), "in", "client")  # возврат — FRT второго эпизода нет
        # c2: ночное обращение — астрономический FRT 11 ч 5 мин, рабочий 5 минут
        message("c2", at(d0, 23), "in", "client")
        message("c2", at(d0, 23, 0, 3), "out", "bot")
        message("c2", at(d1, 10, 5), "out", "operator", user=users["u2"])
        # c3: бот ответил и сам закрыл — оператора в диалоге нет вообще
        message("c3", at(d1, 12), "in", "client")
        message("c3", at(d1, 12, 0, 5), "out", "bot")
        # c4: заметка и failed ответом не считаются, pending — считается
        message("c4", at(d2, 9), "in", "client")
        message("c4", at(d2, 9, 10), "note", "operator", user=users["u1"])
        message("c4", at(d2, 9, 15), "out", "operator", user=users["u1"], delivery="failed")
        message("c4", at(d2, 9, 20), "out", "operator", user=users["u1"], delivery="pending")
        # c5: без ответа вообще
        message("c5", at(d2, 15), "in", "client")
        # c6: отвечает отключённый сотрудник — он обязан остаться в отчёте
        message("c6", at(d3, 9), "in", "client")
        message("c6", at(d3, 9, 30), "out", "operator", user=users["u3"])

        def audit(action: str, when: datetime, *, conv=None, user=None, details=None) -> None:
            s.add(
                AuditLog(
                    user_id=user.id if user is not None else None,
                    action=action,
                    entity="conversation" if conv else "client",
                    entity_id=str(convs[conv].id) if conv else str(uuid.uuid4()),
                    details=details,
                    created_at=when,
                )
            )

        assigned = "conversation.assigned"
        status_changed = "conversation.status_changed"
        audit(
            assigned,
            at(d0, 11, 1),
            conv="c1",
            user=users["u1"],
            details={"assignee_id": str(users["u1"].id), "prev_assignee_id": None, "by": "self"},
        )
        audit(
            assigned,
            at(d0, 23, 10),
            conv="c2",
            user=users["u2"],
            details={"assignee_id": str(users["u2"].id), "prev_assignee_id": None, "by": "self"},
        )
        audit(
            assigned,
            at(d2, 9, 1),
            conv="c4",
            user=users["u1"],
            details={"assignee_id": str(users["u1"].id), "prev_assignee_id": None, "by": "self"},
        )
        audit(
            assigned,
            at(d2, 15, 1),
            conv="c5",
            user=users["u2"],
            details={"assignee_id": str(users["u2"].id), "prev_assignee_id": None, "by": "self"},
        )
        audit(
            assigned,
            at(d3, 9, 5),
            conv="c6",
            user=users["u3"],
            details={"assignee_id": str(users["u3"].id), "prev_assignee_id": None, "by": "self"},
        )
        # закрытия в периоде: оператором и ботом
        audit(
            status_changed,
            at(d1, 12),
            conv="c1",
            user=users["u1"],
            details={
                "from": "in_progress",
                "to": "closed",
                "by": "operator",
                "assignee_id": str(users["u1"].id),
            },
        )
        audit(
            status_changed,
            at(d1, 12, 10),
            conv="c3",
            details={"from": "new", "to": "closed", "by": "bot", "assignee_id": None},
        )
        # возврат клиента: ДВА события (06 §0.3)
        audit(
            status_changed,
            at(d3, 10),
            conv="c1",
            details={"from": "closed", "to": "new", "by": "system", "assignee_id": None},
        )
        audit(
            "conversation.reopened",
            at(d3, 10),
            conv="c1",
            details={"client_id": str(clients[1].id)},
        )
        # закрытие в предыдущем периоде — база для дельты
        audit(
            status_changed,
            at(prev_day, 13),
            conv="prev",
            user=users["u1"],
            details={
                "from": "in_progress",
                "to": "closed",
                "by": "operator",
                "assignee_id": str(users["u1"].id),
            },
        )

        def phone(when: datetime, source: str, conv: str, user=None) -> None:
            s.add(
                AuditLog(
                    user_id=user.id if user is not None else None,
                    action="client.phone_captured",
                    entity="client",
                    entity_id=str(uuid.uuid4()),
                    details={"conversation_id": str(convs[conv].id), "source": source},
                    created_at=when,
                )
            )

        phone(at(d1, 12, 5), "regex", "c3")
        phone(at(d2, 9, 30), "bot", "c4")  # acc2
        phone(at(d2, 10), "manual", "c1", user=users["u1"])
        phone(at(prev_day, 12, 30), "regex", "prev")

        await s.commit()

        namespace = SimpleNamespace(
            period=period,
            days=[d0, d1, d2, d3],
            users={key: row.id for key, row in users.items()},
            accounts={key: row.id for key, row in accounts.items()},
            clients={key: row.id for key, row in clients.items()},
            conversations={key: row.id for key, row in convs.items()},
        )

    await refresh_mv(pg_engine)
    return namespace


# ================================================================ объекты миграции


async def test_migration_created_function_mv_and_indexes(pg_engine):
    async with pg_engine.connect() as conn:
        fn = await conn.scalar(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'business_seconds_between'")
        )
        mv = await conn.scalar(
            text("SELECT count(*) FROM pg_matviews WHERE matviewname = :name"),
            {"name": st.MV},
        )
        # уникальный индекс — без него невозможен REFRESH ... CONCURRENTLY
        unique_idx = await conn.scalar(
            text(
                "SELECT indisunique FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'mv_conversation_stats_pk'"
            )
        )
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_%'")
                )
            ).scalars()
        )
    assert fn == 1
    assert mv == 1
    assert unique_idx is True
    assert {
        "idx_messages_operator_out",
        "idx_messages_client_in",
        "idx_conversations_account_status",
        "idx_conversations_client",
        "idx_audit_action_created",
        "idx_audit_entity",
    } <= indexes


# ======================================================= business_seconds_between


@pytest.mark.parametrize(
    "t0,t1",
    [
        (at(date(2026, 8, 3), 11), at(date(2026, 8, 3), 11, 30)),  # внутри окна
        (at(date(2026, 8, 3), 8), at(date(2026, 8, 3), 21)),  # шире окна
        (at(date(2026, 8, 3), 7), at(date(2026, 8, 3), 9, 30)),  # весь интервал до 10:00
        (at(date(2026, 8, 3), 23), at(date(2026, 8, 4), 10, 5)),  # через полночь
        (at(date(2026, 7, 31), 19), at(date(2026, 8, 2), 11)),  # несколько суток
        (at(date(2026, 8, 3), 15), at(date(2026, 8, 3), 11)),  # t1 < t0
        (at(date(2026, 8, 3), 12), at(date(2026, 8, 3), 12)),  # нулевой интервал
    ],
)
async def test_business_seconds_sql_matches_python_reference(pg_engine, t0, t1):
    """SQL-функция и питоновский эталон обязаны совпадать до секунды."""
    async with pg_engine.connect() as conn:
        value = await conn.scalar(
            text("SELECT business_seconds_between(:t0, :t1)"), {"t0": t0, "t1": t1}
        )
    assert value == st.business_seconds_between(t0, t1)


async def test_business_seconds_is_strict(pg_engine):
    """STRICT: NULL на входе → NULL, а не 0 (иначе диалог без ответа испортит avg)."""
    async with pg_engine.connect() as conn:
        value = await conn.scalar(text("SELECT business_seconds_between(NULL::timestamptz, now())"))
    assert value is None


@pytest.mark.parametrize(
    "start,end",
    [(0, 0), (10, 10), (20, 8)],
)
@pytest.mark.parametrize(
    "t0,t1",
    [
        (at(date(2026, 8, 3), 9), at(date(2026, 8, 3), 12)),  # три часа внутри суток
        (at(date(2026, 8, 3), 23), at(date(2026, 8, 4), 10, 5)),  # через полночь
        (at(date(2026, 8, 3), 15), at(date(2026, 8, 3), 11)),  # t1 < t0
    ],
)
async def test_an_unset_window_falls_back_to_calendar_in_sql_too(pg_engine, start, end, t0, t1):
    """Окно не задано → календарное время, и SQL с эталоном обязаны совпасть.

    Это та самая правка: на боевой системе окно сохранено как 0:00–0:00, и
    медиана «первый ответ в рабочее время» показывала ноль у всех менеджеров.
    Считает отчёт SQL-функция, поэтому запасной вариант проверяется здесь, а
    не только на питоновском эталоне — разъедься они, юнит-тесты остались бы
    зелёными, а отчёт продолжил бы врать.
    """
    async with pg_engine.connect() as conn:
        value = await conn.scalar(
            text(
                "SELECT business_seconds_between(:t0, :t1, "
                "make_interval(hours => :start), make_interval(hours => :end))"
            ),
            {"t0": t0, "t1": t1, "start": start, "end": end},
        )
    expected = st.business_seconds_between(t0, t1, timedelta(hours=start), timedelta(hours=end))
    assert value == expected
    # И это НЕ ноль там, где ответ реально был: ноль означал бы «ответили
    # мгновенно» — ровно ту неправду, ради которой всё и делалось.
    assert value == max(0, int((t1 - t0).total_seconds()))


async def test_business_seconds_window_is_parameterised(pg_engine):
    async with pg_engine.connect() as conn:
        value = await conn.scalar(
            text(
                "SELECT business_seconds_between(:t0, :t1, interval '9 hours', interval '18 hours')"
            ),
            {"t0": at(date(2026, 8, 3), 0), "t1": at(date(2026, 8, 3), 23, 59)},
        )
    assert value == 9 * 3600


# ================================================================== FRT (06 §1.1)


async def test_frt_metrics_on_known_data(seed, sessionmaker):
    async with sessionmaker() as db:
        agg = await st.frt_aggregate(db, seed.period, st.Filters())

    assert agg["conversations_started"] == 6  # c1..c6, старый и prev не в периоде
    assert agg["answered_by_operator"] == 4  # c1, c2, c4, c6
    assert agg["answered_by_bot"] == 2  # c2, c3
    assert agg["unanswered"] == 1  # c5 — ни оператора, ни бота

    # астрономический FRT: 300 (c1), 39900 (c2), 1200 (c4), 1800 (c6)
    assert agg["frt_operator_median_sec"] == 1500
    assert agg["frt_operator_avg_sec"] == 10800
    # рабочий FRT: 300, 300, 0 (ответ до 10:00), 0 (ответ до 10:00)
    assert agg["frt_operator_median_biz_sec"] == 150
    assert agg["frt_operator_avg_biz_sec"] == 150
    # бот: 3 с (c2) и 5 с (c3)
    assert agg["frt_bot_median_sec"] == 4
    assert agg["frt_bot_avg_sec"] == 4


async def test_mv_and_live_reference_agree(seed, sessionmaker):
    """MV-вариант и эталонный SQL 06 §2.2 обязаны давать одинаковые цифры."""
    async with sessionmaker() as db:
        from_mv = await st.frt_aggregate(db, seed.period, st.Filters())
        live = await st.frt_aggregate(db, seed.period, st.Filters(), live=True)
    assert from_mv == live


@pytest.mark.parametrize("сдвиг", [0, 1, 2, 3, 4])
async def test_split_equals_full_live_at_every_boundary(seed, sessionmaker, сдвиг):
    """Раскол «витрина + сегодняшний хвост» обязан давать ТЕ ЖЕ цифры.

    Ради этой оптимизации `/stats/summary` перестал считать живьём весь
    период: живьём остаётся только то, чего в витрине заведомо нет. Замер на
    бою 29.08 — 6,77 с против 0,010 с, и вся страница висела на этом.

    Сложение половин законно ровно потому, что складываются СТРОКИ, а не
    агрегаты: медиана из двух медиан была бы неправдой. Тест двигает границу
    по всем дням периода, включая оба края (сдвиг 0 — всё живьём, сдвиг 4 —
    всё с витрины), и требует побайтового совпадения. Разъедется определение
    любой из половин — здесь и упадёт.
    """
    граница = st.msk_day_bounds(seed.period.date_from + timedelta(days=сдвиг))[0]
    async with sessionmaker() as db:
        полный = await st.frt_aggregate(db, seed.period, st.Filters(), live=True)
        раскол = await st.frt_aggregate(db, seed.period, st.Filters(), live=True, split_at=граница)
    assert раскол == полный


async def test_split_holds_under_filters(seed, sessionmaker):
    """Фильтры действуют на ОБЕ половины, иначе цифра поедет только у среза.

    Половины фильтруются разными выражениями — витринная по `s.account_id`,
    живая по `c.account_id`. Пропущенный фильтр в одной из них виден только
    тогда, когда фильтр вообще задан, — общий прогон выше его не поймает.
    """
    граница = st.msk_day_bounds(seed.period.date_from + timedelta(days=2))[0]
    срезы = (
        st.Filters(account_id=seed.accounts["acc2"]),
        st.Filters(manager_ids=(seed.users["u1"],)),
    )
    async with sessionmaker() as db:
        for фильтры in срезы:
            полный = await st.frt_aggregate(db, seed.period, фильтры, live=True)
            раскол = await st.frt_aggregate(db, seed.period, фильтры, live=True, split_at=граница)
            assert раскол == полный, фильтры


async def test_split_does_not_double_count_a_dialog_on_the_seam(seed, sessionmaker, pg_engine):
    """Диалог ровно на стыке половин обязан попасть в ОДНУ из них.

    Половины разделены `< :ts_split` и `>= :ts_split`. Ошибись здесь на один
    знак — и диалог, чьё первое сообщение клиента пришло ровно в московскую
    полночь, попадёт в обе: «новых диалогов» станет на один больше, а медиана
    сместится. Диверсия `<` → `<=` проходила мимо предыдущих тестов насквозь:
    в сценарии не было ни одного диалога на границе суток. Теперь есть.
    """
    стык = st.msk_day_bounds(seed.period.date_from + timedelta(days=2))[0]
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id="boundary-midnight",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[7],
            assignee_id=seed.users["u1"],
            status="closed",
            last_message_at=стык,
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="ровно в полночь",
                attachments=[],
                created_at=стык,
            )
        )
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="принял",
                attachments=[],
                created_at=стык + timedelta(seconds=60),
            )
        )
        await s.commit()
    await refresh_mv(pg_engine)

    async with sessionmaker() as db:
        полный = await st.frt_aggregate(db, seed.period, st.Filters(), live=True)
        раскол = await st.frt_aggregate(db, seed.period, st.Filters(), live=True, split_at=стык)
    assert раскол == полный


async def test_frt_ignores_note_and_failed_but_counts_pending(seed, sessionmaker):
    """Краевые случаи 06 §1.1: заметка и failed ответом не считаются, pending —
    считается. У c4 первый «настоящий» ответ в 09:20 → FRT 1200 с."""
    async with sessionmaker() as db:
        row = await st._row(
            db,
            f"SELECT frt_operator_sec, first_operator_user_id FROM {st.MV} "
            "WHERE conversation_id = :conv",
            {"conv": seed.conversations["c4"]},
        )
    assert row["frt_operator_sec"] == 1200
    assert row["first_operator_user_id"] == seed.users["u1"]


async def test_reopened_conversation_keeps_first_episode_frt(seed, sessionmaker):
    """Переоткрытие не порождает второй FRT (06 §1.1.6): считается первый эпизод."""
    async with sessionmaker() as db:
        row = await st._row(
            db,
            f"SELECT first_client_at, frt_operator_sec FROM {st.MV} WHERE conversation_id = :conv",
            {"conv": seed.conversations["c1"]},
        )
    assert row["frt_operator_sec"] == 300


# ============================================== новые / закрыто / snapshot (06 §1.2)


async def test_conversations_closed_counts_unique_dialogs(seed, sessionmaker):
    async with sessionmaker() as db:
        closed = await st.conversations_closed(db, seed.period, st.Filters())
    assert closed == 2  # c1 (оператором) и c3 (ботом); перевод в 'new' не считается


async def test_snapshot_is_now_not_period(seed, sessionmaker):
    async with sessionmaker() as db:
        snapshot = await st.snapshot_now(db, st.Filters())
    assert snapshot["queue_now"] == 2  # c1 (переоткрыт) и c5 — никем не взяты
    assert snapshot["in_progress_now"] == 3  # c2, c4, c6


async def test_in_progress_names_the_dialogs_nobody_leads(seed, sessionmaker, pg_engine):
    """«В работе» без ответственного — отдельным числом (проверка 24.09).

    Подсказка карточки обещала «взятые кем-то из сотрудников», а в числе были
    и ничьи: на бою 51 из 305 висели в «В работе» без ответственного.
    """
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conversations SET assignee_id = NULL WHERE id = :id"),
            {"id": seed.conversations["c2"]},
        )
    async with sessionmaker() as db:
        snapshot = await st.snapshot_now(db, st.Filters())
    assert snapshot["in_progress_now"] == 3, "число карточки не меняется — оно входит в сумму"
    assert snapshot["in_progress_unassigned"] == 1


async def test_waiting_now_is_not_the_inbox_queue(seed, sessionmaker):
    """«Ждут ответа» ≠ длина очереди «Входящие» (STATS-01, FUNC-114).

    Карточка брала `count(*) WHERE status='new'` — очередь непринятых, — а
    подписана была «диалоги в работе, где клиент не получил ответа». Пока
    очередь пустая, подмена не видна; на боевом потоке это два разных числа,
    и руководитель принимал неразобранную очередь за невыполненную работу.

    Числа сценария подобраны разными (1 / 2 / 3) специально: верни кто-нибудь
    сюда `new_now`, и тест упадёт, а не совпадёт случайно.
    """
    async with sessionmaker() as db:
        snapshot = await st.snapshot_now(db, st.Filters())
    assert snapshot["waiting_now"] == 1  # только c2: у него стоит awaiting_since
    assert snapshot["waiting_now"] != snapshot["queue_now"]
    # «Ждут ответа» — подмножество «В работе»: очередь в него не входит.
    assert snapshot["waiting_now"] <= snapshot["in_progress_now"]


async def test_the_numbers_of_right_now_add_up_to_every_open_dialog(seed, sessionmaker):
    """Сумма разбивки по статусам равна `count(*)` с теми же фильтрами.

    ЗАЧЕМ ЭТО ПРОВЕРЯТЬ. До docs/38 группа «Прямо сейчас» состояла из двух
    чисел, посчитанных двумя `FILTER`-ами. Диалог в любом новом статусе не
    попадал бы НИ В ОДНО из них — и руководитель видел бы, что открытых
    диалогов стало меньше, хотя их столько же. По этой цифре решают, звать ли
    смену.

    Теперь снимок считается группировкой, и пропустить значение технически
    нельзя; тест сторожит именно это свойство, а не конкретные числа сценария.
    """
    async with sessionmaker() as db:
        snapshot = await st.snapshot_now(db, st.Filters())
        total = (await db.execute(text("SELECT count(*) FROM conversations"))).scalar_one()
    assert sum(snapshot["by_status"].values()) == total, (
        "часть диалогов не попала ни в одну карточку группы «Прямо сейчас»"
    )
    assert snapshot["open_now"] == total - snapshot["by_status"]["closed"]


async def test_waiting_now_respects_manager_filter(seed, sessionmaker):
    """Фильтр по менеджеру сужает и снимок: c2 ведёт u2, у u1 ждущих нет."""
    async with sessionmaker() as db:
        u2 = await st.snapshot_now(db, st.Filters(manager_ids=(seed.users["u2"],)))
        u1 = await st.snapshot_now(db, st.Filters(manager_ids=(seed.users["u1"],)))
    assert u2["waiting_now"] == 1
    assert u1["waiting_now"] == 0


async def test_todays_period_does_not_wait_for_the_hourly_refresh(seed, sessionmaker):
    """«Новые диалоги» и «Закрыто» за период с сегодня считаются из одного времени.

    Что было. «Новые» приходили из витрины (пересчёт раз в час в HH:05), а
    стоящее вплотную «Закрыто» — живьём из ``audit_log``. Диалог, пришедший в
    10:20 и закрытый в 10:40, попадал в «Закрыто» и не попадал в «Новые», и на
    экране «Закрыто» оказывалось БОЛЬШЕ «Новых» — отчёт, противоречащий сам
    себе.

    Сценарий воспроизводит это буквально: диалог заводится ПОСЛЕ последнего
    пересчёта витрины (фикстура обновила её в конце), и `refresh_mv` здесь
    намеренно не вызывается — иначе проверять было бы нечего.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    today = st.today_msk()
    # И обращение, и закрытие держим внутри СЕГОДНЯШНЕГО московского дня.
    # «Двадцать минут назад» в первые двадцать минут после московской полуночи
    # — это уже вчера: диалог уезжал за границу периода, и тест падал не на
    # дефекте, который сторожит, а на календаре.
    midnight = datetime.combine(today, time(), tzinfo=st.MSK).astimezone(UTC)
    first_at = max(now - timedelta(minutes=20), midnight)
    closed_at = max(now - timedelta(minutes=1), first_at)
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id="fresh-today",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[7],
            assignee_id=seed.users["u1"],
            status="closed",
            last_message_at=now,
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="сломалась стиралка",
                attachments=[],
                created_at=first_at,
            )
        )
        s.add(
            AuditLog(
                user_id=seed.users["u1"],
                action="conversation.status_changed",
                entity="conversation",
                entity_id=str(conv.id),
                details={
                    "from": "in_progress",
                    "to": "closed",
                    "by": "operator",
                    "assignee_id": str(seed.users["u1"]),
                },
                created_at=closed_at,
            )
        )
        await s.commit()

    period = st.Period(today, today)
    async with sessionmaker() as db:
        payload = await st.summary(db, period, st.Filters())

    cards = payload["cards"]
    assert payload["period_live"] is True
    assert cards["conversations_new"]["value"] == 1
    assert cards["conversations_closed"]["value"] == 1
    # Суть дефекта одной строкой: закрыть можно только то, что уже пришло.
    assert cards["conversations_closed"]["value"] <= cards["conversations_new"]["value"]


async def test_summary_reports_the_configured_work_hours(seed, sessionmaker):
    """Ответ несёт ДЕЙСТВУЮЩИЕ рабочие часы, а не умолчания модуля (FUNC-42).

    Подпись FRT печатала «10:00–20:00» числами в разметке, хотя окно —
    настройка (#41). Сменил владелец часы — цифра поехала по новым, подпись
    осталась старой. Часы читаются из `app_settings`, а не из WORK_START /
    WORK_END: те объявлены умолчаниями и на подмену в базе не реагируют.
    """
    from app.services import app_settings

    async with sessionmaker() as db:
        default = await st.summary(db, seed.period, st.Filters())
        assert default["work_hours"] == {"start_hour": 10, "end_hour": 20}

        await app_settings.set_many(
            db,
            {app_settings.STATS_WORK_START_HOUR: 9, app_settings.STATS_WORK_END_HOUR: 21},
            user_id=seed.users["admin"],
        )
        await db.commit()

    async with sessionmaker() as db:
        changed = await st.summary(db, seed.period, st.Filters())
    assert changed["work_hours"] == {"start_hour": 9, "end_hour": 21}


async def test_past_period_still_reads_the_materialized_view(seed, sessionmaker):
    """Период целиком в прошлом остаётся на витрине — платить за live там не за что."""
    async with sessionmaker() as db:
        payload = await st.summary(db, seed.period, st.Filters())
    assert payload["period_live"] is False


async def test_repeat_clients_live_and_mv_agree(seed, sessionmaker, pg_engine):
    """Live-вариант «повторных клиентов» обязан сойтись с витринным.

    Второй способ счёта того же слова — это и есть механизм, которым карточки
    расходятся между собой; сторож держит оба варианта на одном определении.

    Сценарию добавлен РАЗЛИЧАЮЩИЙ случай: у клиента c5 появляется второй
    диалог, но ПОЗЖЕ периодного. «Повторный» — это тот, кто обращался РАНЬШЕ,
    поэтому оба варианта обязаны его не засчитать. Без такой строки сравнение
    проходило бы и на определении «есть любой другой диалог» — проверено:
    подменил условие, тест остался зелёным.
    """
    async with sessionmaker() as s:
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="later-not-repeat",
                account_id=seed.accounts["acc1"],
                client_id=seed.clients[5],
                status="new",
                last_message_at=datetime.now(UTC),
            )
        )
        await s.commit()
    await refresh_mv(pg_engine)

    async with sessionmaker() as db:
        from_mv = await st.repeat_contacts(db, seed.period, st.Filters())
        live = await st.repeat_contacts(db, seed.period, st.Filters(), live=True)
    assert live == from_mv
    assert from_mv["repeat_clients"] > 0  # иначе сравнивались бы два нуля


# ==================================================== % закрытых ботом (06 §1.3)


async def test_bot_closed_share(seed, sessionmaker):
    async with sessionmaker() as db:
        bot = await st.bot_closed(db, seed.period, st.Filters())
    assert bot["closed_total"] == 2
    assert bot["closed_by_bot"] == 1  # только c3 — в c1 отвечал оператор
    assert bot["pct"] == 50.0


async def test_bot_closed_total_matches_closed_card(seed, sessionmaker):
    """Инвариант 06 §4.1: closed_total карточки бота == «закрыто за период»."""
    async with sessionmaker() as db:
        bot = await st.bot_closed(db, seed.period, st.Filters())
        closed = await st.conversations_closed(db, seed.period, st.Filters())
    assert bot["closed_total"] == closed


async def test_bot_closed_uses_assignee_snapshot_after_transfer(seed, sessionmaker, pg_engine):
    """Тот же инвариант при фильтре по менеджеру — после передачи диалога.

    c1 закрыт, когда его вёл u1 (снимок в ``details->>'assignee_id'``), после
    чего диалог передан u2. По 06 §2.3 отчёт за прошлое обязан считать его за
    u1: ``conversations.assignee_id`` мутабелен, снимок — нет. Если карточка
    «% закрытых ботом» фильтрует по текущему ответственному, в одном ответе
    /stats/summary «закрыто: 1» соседствует с «закрыто всего: 0».
    """
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conversations SET assignee_id = :new WHERE id = :conv"),
            {"new": seed.users["u2"], "conv": seed.conversations["c1"]},
        )

    by_u1 = st.Filters(manager_ids=(seed.users["u1"],))
    async with sessionmaker() as db:
        closed = await st.conversations_closed(db, seed.period, by_u1)
        bot = await st.bot_closed(db, seed.period, by_u1)
    assert closed == 1  # c1 — по снимку, а не по новому ответственному
    assert bot["closed_total"] == closed
    assert bot["closed_by_bot"] == 0  # c1 закрыл оператор
    assert bot["pct"] == 0.0


# ========================================================== телефоны (06 §1.4)


async def test_phones_collected_by_source(seed, sessionmaker):
    async with sessionmaker() as db:
        phones = await st.phones_collected(db, seed.period, st.Filters())
    assert phones["value"] == 3
    assert phones["by_source"] == {"bot": 1, "regex": 1, "manual": 1}


async def test_phones_filtered_by_account(seed, sessionmaker):
    """Фильтр по аккаунту — через ``details->>'conversation_id'`` (06 §1.4)."""
    async with sessionmaker() as db:
        phones = await st.phones_collected(
            db, seed.period, st.Filters(account_id=seed.accounts["acc2"])
        )
    assert phones["value"] == 1
    assert phones["by_source"]["bot"] == 1


async def test_the_phone_chart_skips_history_imports_like_the_card(seed, sessionmaker):
    """Номера из догона истории не считают ни карточка, ни график (проверка 24.09).

    Условие стояло только в карточке: в день подключения канала на одном
    экране было 250 в карточке и 2 000+ на графике за тот же день.
    """
    async with sessionmaker() as db:
        db.add(
            AuditLog(
                user_id=None,
                action="client.phone_captured",
                entity="client",
                entity_id=str(uuid.uuid4()),
                details={
                    "conversation_id": str(seed.conversations["c1"]),
                    "source": "regex",
                    "history": True,
                },
                created_at=at(seed.days[3], 11),
            )
        )
        await db.commit()

    async with sessionmaker() as db:
        card = await st.phones_collected(db, seed.period, st.Filters())
        series = await st.timeseries(
            db, seed.period, st.Filters(), metric="phones_collected", group="day"
        )
    assert card["value"] == 3
    assert sum(point["value"] for point in series["points"]) == card["value"]


# ================================================ повторные обращения (06 §1.6)


async def test_repeat_contacts(seed, sessionmaker):
    async with sessionmaker() as db:
        repeat = await st.repeat_contacts(db, seed.period, st.Filters())
    assert repeat["reopened"] == 1  # событие conversation.reopened у c1
    assert repeat["repeat_clients"] == 1  # клиент 6: старый диалог был раньше


async def test_repeat_clients_frozen_after_period_ends(seed, sessionmaker, pg_engine):
    """Закрытый период не меняется задним числом (09.09).

    До правки «повторным» считался клиент, чей ПРОШЛЫЙ диалог УСПЕЛ ЗАМОЛЧАТЬ
    до начала нового: признак опирался на живую колонку
    ``conversations.last_message_at``. Клиент писал в старый диалог сегодня — и
    число за давно закрытый месяц уменьшалось. Здесь проверяется именно это:
    сообщение приходит В СТАРЫЙ диалог после конца периода, и обе величины
    обязаны остаться прежними.

    Оба пути сразу — витринный и живой: до правки условие стояло в каждом из
    них своей копией, и починка одного молча оставила бы второй.
    """
    async with sessionmaker() as db:
        было = await st.repeat_contacts(db, seed.period, st.Filters())
        было_live = await st.repeat_contacts(db, seed.period, st.Filters(), live=True)
    assert было["repeat_clients"] == 1  # клиент 6: старый диалог начался раньше
    assert было_live["repeat_clients"] == 1

    сейчас = datetime.now(UTC)
    async with sessionmaker() as s:
        s.add(
            Message(
                conversation_id=seed.conversations["old"],
                direction="in",
                sender_type="client",
                body="текст",
                attachments=[],
                delivery_status="delivered",
                created_at=сейчас,
            )
        )
        await s.execute(
            text("UPDATE conversations SET last_message_at = :t WHERE id = :id"),
            {"t": сейчас, "id": seed.conversations["old"]},
        )
        await s.commit()
    await refresh_mv(pg_engine)

    async with sessionmaker() as db:
        стало = await st.repeat_contacts(db, seed.period, st.Filters())
        стало_live = await st.repeat_contacts(db, seed.period, st.Filters(), live=True)
    assert стало == было
    assert стало_live == было_live


# ==================================================================== фильтры


async def test_account_filter_narrows_period_metrics(seed, sessionmaker):
    async with sessionmaker() as db:
        agg = await st.frt_aggregate(db, seed.period, st.Filters(account_id=seed.accounts["acc2"]))
    assert agg["conversations_started"] == 1  # только c4
    assert agg["frt_operator_median_sec"] == 1200


async def test_manager_filter_is_repeatable(seed, sessionmaker):
    """``manager_id=a&manager_id=b`` → ``= ANY(uuid[])`` (06 §0.4)."""
    async with sessionmaker() as db:
        one = await st.frt_aggregate(db, seed.period, st.Filters(manager_ids=(seed.users["u1"],)))
        two = await st.frt_aggregate(
            db, seed.period, st.Filters(manager_ids=(seed.users["u1"], seed.users["u2"]))
        )
    assert one["conversations_started"] == 2  # c1, c4
    assert one["frt_operator_median_sec"] == 750  # (300 + 1200) / 2
    assert two["conversations_started"] == 4  # + c2, c5


# ================================================================ summary (06 §4.1)


async def test_summary_cards_and_deltas(seed, sessionmaker):
    async with sessionmaker() as db:
        payload = await st.summary(db, seed.period, st.Filters())
    cards = payload["cards"]

    assert payload["prev_period"] == seed.period.previous().as_dict()
    assert cards["conversations_new"]["value"] == 6
    assert cards["conversations_new"]["prev"] == 1  # диалог предыдущего периода
    assert cards["conversations_new"]["delta_pct"] == 500.0
    assert cards["conversations_closed"] == {"value": 2, "prev": 1, "delta_pct": 100.0}
    # snapshot-карточки не сравниваются с прошлым
    assert cards["in_progress_now"] == {
        "value": 3,
        "prev": None,
        "delta_pct": None,
        "unassigned": 0,
    }
    # Три разные величины — три разные карточки (STATS-01)
    assert cards["waiting_now"]["value"] == 1
    assert cards["queue_now"]["value"] == 2

    frt = cards["frt_operator"]
    assert frt["median_sec"] == 1500
    assert frt["median_biz_sec"] == 150
    assert frt["answered"] == 4
    assert frt["unanswered"] == 1
    assert frt["prev_median_sec"] == 300

    assert cards["frt_bot"]["median_sec"] == 4
    assert cards["bot_closed"]["pct"] == 50.0
    assert cards["bot_closed"]["closed_total"] == 2
    assert cards["phones_collected"]["value"] == 3
    assert cards["phones_collected"]["prev"] == 1
    assert cards["repeat_contacts"] == {
        "reopened": 1,
        "repeat_clients": 1,
        "prev_reopened": 0,
        "delta_pct": None,  # prev = 0 → сравнивать не с чем
    }


# ============================================================= timeseries (06 §4.2)


async def test_timeseries_is_zero_filled_by_day(seed, sessionmaker):
    async with sessionmaker() as db:
        series = await st.timeseries(
            db, seed.period, st.Filters(), metric="conversations_new", group="day"
        )
    assert [point["value"] for point in series["points"]] == [2, 1, 2, 1]
    assert [point["ts"] for point in series["points"]] == [day.isoformat() for day in seed.days]


async def test_timeseries_median_empty_day_is_null_not_zero(seed, sessionmaker):
    """06 §4.2: нулевая медиана и «нет данных» — разные вещи."""
    async with sessionmaker() as db:
        series = await st.timeseries(
            db, seed.period, st.Filters(), metric="frt_operator_median", group="day"
        )
    assert [point["value"] for point in series["points"]] == [20100, None, 1200, 1800]


@pytest.mark.parametrize(
    "metric,expected",
    [
        ("conversations_closed", [0, 2, 0, 0]),
        ("messages_in", [2, 1, 2, 2]),
        ("messages_out", [1, 1, 1, 1]),
        ("phones_collected", [0, 1, 2, 0]),
    ],
)
async def test_timeseries_metrics(seed, sessionmaker, metric, expected):
    async with sessionmaker() as db:
        series = await st.timeseries(db, seed.period, st.Filters(), metric=metric, group="day")
    assert [point["value"] for point in series["points"]] == expected


async def test_timeseries_hour_group_covers_every_hour(seed, sessionmaker):
    one_day = st.Period(seed.days[0], seed.days[0])
    async with sessionmaker() as db:
        series = await st.timeseries(db, one_day, st.Filters(), metric="messages_in", group="hour")
    assert len(series["points"]) == 24
    hours = {point["ts"][-5:]: point["value"] for point in series["points"]}
    assert hours["11:00"] == 1  # c1
    assert hours["23:00"] == 1  # c2
    assert hours["05:00"] == 0


# =============================================================== heatmap (06 §4.3)


async def test_heatmap_is_always_168_cells(seed, sessionmaker, redis):
    async with sessionmaker() as db:
        payload = await st.heatmap(db, redis, seed.period, st.Filters())
    cells = payload["cells"]
    assert len(cells) == 168
    assert {cell["dow"] for cell in cells} == set(range(1, 8))

    inbound = [
        at(seed.days[0], 11),
        at(seed.days[0], 23),
        at(seed.days[1], 12),
        at(seed.days[2], 9),
        at(seed.days[2], 15),
        at(seed.days[3], 9),
        at(seed.days[3], 10),
    ]
    expected: dict[tuple[int, int], int] = {}
    for moment in inbound:
        local = moment.astimezone(MSK)
        key = (local.isoweekday(), local.hour)
        expected[key] = expected.get(key, 0) + 1
    actual = {(c["dow"], c["hour"]): c["value"] for c in cells if c["value"]}
    assert actual == expected


async def test_heatmap_is_cached_in_redis(seed, sessionmaker, redis):
    async with sessionmaker() as db:
        await st.heatmap(db, redis, seed.period, st.Filters())
    assert await redis.exists(st.heatmap_cache_key(seed.period, st.Filters()))


# ============================================================== managers (06 §4.4)


async def test_manager_table_rows_and_totals(seed, sessionmaker):
    async with sessionmaker() as db:
        payload = await st.managers(db, seed.period, st.Filters())
    rows = {row["full_name"]: row for row in payload["rows"]}

    # head в таблице не участвует — он не пишет клиентам (DESIGN §5.1)
    assert "Дмитрий Руководитель" not in rows

    anna = rows["Анна Смирнова"]
    assert (anna["taken"], anna["answered"], anna["closed"]) == (2, 2, 1)
    assert anna["messages_sent"] == 2  # failed не считается, pending — считается
    assert anna["frt_median_sec"] == 750
    assert anna["frt_median_biz_sec"] == 150

    boris = rows["Борис Ким"]
    assert (boris["taken"], boris["answered"], boris["closed"]) == (2, 1, 0)
    assert boris["frt_median_sec"] == 39900

    # отключённый сотрудник с активностью остаётся в отчёте (06 §2.8)
    victor = rows["Виктор Уволенный"]
    assert victor["is_active"] is False
    assert victor["answered"] == 1

    # активный сотрудник без активности — нули, а не пропуск строки
    assert rows["Галина Без Активности"]["messages_sent"] == 0
    assert rows["Галина Без Активности"]["frt_median_sec"] is None

    totals = payload["totals"]
    assert totals["taken"] == 5
    assert totals["answered"] == 4
    assert totals["closed"] == 1
    assert totals["messages_sent"] == 4
    # медиана считается по всей выборке заново, а не как сумма медиан
    assert totals["frt_median_sec"] == 1500
    assert totals["frt_median_biz_sec"] == 150


async def test_taken_counts_dialogs_not_assignment_events(seed, sessionmaker):
    """«Принято» — это диалоги, а не события назначения (жалоба заказчика №9).

    `conversation.assigned` пишется на КАЖДЫЙ шаг: приём из очереди,
    автоподхват первым ответом, самоназначение и каждую передачу. Пока колонка
    считала события, диалог, взятый из очереди и дважды переданный, давал
    тройку, и на экране статистики рядом стояли «Новых диалогов: 5» и «ИТОГО
    Принято: 7» за один и тот же период — объяснить это число было нечем.

    Здесь c1 уходит Борису и возвращается Анне: три события назначения на один
    диалог. Анне он обязан посчитаться один раз, Борису — один раз, и ни разу
    дважды.
    """
    d3 = seed.days[3]
    async with sessionmaker() as s:
        for when, who in ((at(d3, 11), "u2"), (at(d3, 12), "u1")):
            s.add(
                AuditLog(
                    user_id=seed.users["admin"],
                    action="conversation.assigned",
                    entity="conversation",
                    entity_id=str(seed.conversations["c1"]),
                    details={"assignee_id": str(seed.users[who]), "by": "transfer"},
                    created_at=when,
                )
            )
        await s.commit()

    async with sessionmaker() as db:
        payload = await st.managers(db, seed.period, st.Filters())
    rows = {row["full_name"]: row for row in payload["rows"]}
    assert rows["Анна Смирнова"]["taken"] == 2  # c1 (дважды назначен ей) и c4
    assert rows["Борис Ким"]["taken"] == 3  # c2, c5 и полученный передачей c1
    # ⚠ ИТОГО — УНИКАЛЬНЫЕ ДИАЛОГИ ВЫБОРКИ, А НЕ СУММА СТРОК (15 августа).
    # c1 честно стоит и у Анны, и у Бориса — каждый его принимал, — но диалог
    # один, и в ИТОГО он один: c1, c2, c4, c5, c6 → 5. Сумма строк давала 6,
    # то есть механизм жалобы №9 этажом выше: строки починили DISTINCT'ом
    # 12 августа, а итог продолжал складывать. При счёте событий было бы 7.
    assert payload["totals"]["taken"] == 5
    assert sum(r["taken"] for r in payload["rows"]) == 6, (
        "сумма строк ОБЯЗАНА быть больше итога: в ней передача видна дважды"
    )


async def test_smoke_robot_is_absent_from_the_manager_table(seed, sessionmaker):
    """Служебный smoke-пользователь (07 §6) — не менеджер.

    `seed-smoke` гоняется на КАЖДОМ деплое и шлёт сообщение в служебный диалог;
    без фильтра робот всплывал бы в рейтинге менеджеров и в фильтре «по
    менеджеру» рядом с живыми сотрудниками.

    Робот заведён ровно так, как его заводит `seed-smoke`: со ВЗВЕДЁННЫМ
    `is_service`. Прятать его по домену адреса нельзя — см. соседний тест.
    """
    async with sessionmaker() as s:
        robot = User(
            email="smoke@leadpartner.local",
            password_hash="x",
            full_name="Smoke Robot",
            role="manager",
            is_active=True,
            is_service=True,
        )
        s.add(robot)
        await s.commit()

    async with sessionmaker() as db:
        payload = await st.managers(db, seed.period, st.Filters())
    assert "Smoke Robot" not in {row["full_name"] for row in payload["rows"]}
    # живые сотрудники на месте — фильтр не срезал лишнего
    assert "Анна Смирнова" in {row["full_name"] for row in payload["rows"]}


async def test_manager_on_service_domain_is_a_live_employee(seed, sessionmaker):
    """Сотрудник с адресом на `.local` — человек, а не служебная запись.

    Отчёт прятал менеджеров по домену почты (`email NOT LIKE '%.local'`).
    Замер 12 августа: на боевой базе на `.local` живут ДЕЙСТВУЮЩИЕ
    администраторы (`admin@leadpartner.local`, `dev-admin@leadchat.local`), и
    таблица менеджеров не показывала их вовсе — ни строки, ни нуля. Пропуск
    ничем себя не выдаёт: руководитель видит ровную таблицу и считает по ней,
    а работа этих людей не попадает ни в их строку, ни в «ИТОГО».

    Здесь такой администратор берёт на себя диалог `c6` — то есть работает
    ровно так же, как остальные, — и обязан оказаться в отчёте со своим
    «Принято».
    """
    async with sessionmaker() as s:
        human = User(
            email="admin@leadpartner.local",
            password_hash="x",
            full_name="Пётр Настоящий",
            role="admin",
            is_active=True,
            # признак служебности НЕ взведён: запись завёл человек, а не seed-smoke
        )
        s.add(human)
        await s.flush()
        s.add(
            AuditLog(
                user_id=seed.users["admin"],
                action="conversation.assigned",
                entity="conversation",
                entity_id=str(seed.conversations["c6"]),
                details={"assignee_id": str(human.id), "by": "transfer"},
                created_at=at(seed.days[2], 12),
            )
        )
        await s.commit()

    async with sessionmaker() as db:
        payload = await st.managers(db, seed.period, st.Filters())
    rows = {row["full_name"]: row for row in payload["rows"]}
    assert "Пётр Настоящий" in rows, "живой администратор на .local пропал из отчёта"
    assert rows["Пётр Настоящий"]["taken"] == 1


async def test_manager_table_server_side_sort(seed, sessionmaker):
    async with sessionmaker() as db:
        asc = await st.manager_rows(db, seed.period, st.Filters(), sort="answered", order="asc")
        desc = await st.manager_rows(db, seed.period, st.Filters(), sort="answered", order="desc")
    assert [row["answered"] for row in asc] == sorted(row["answered"] for row in asc)
    assert desc[0]["answered"] == 2  # Анна


async def test_manager_frt_attributed_to_first_answer_author(seed, sessionmaker):
    """06 §1.1.7: FRT остаётся у автора первого ответа даже после передачи диалога."""
    async with sessionmaker() as db:
        await db.execute(
            text("UPDATE conversations SET assignee_id = :new WHERE id = :conv"),
            {"new": seed.users["u2"], "conv": seed.conversations["c1"]},
        )
        await db.commit()
    async with sessionmaker() as db:
        table = await st.manager_rows(db, seed.period, st.Filters())
    rows = {row["full_name"]: row for row in table}
    # MV не пересчитывалась, но атрибуция и не зависит от assignee_id
    assert rows["Анна Смирнова"]["answered"] == 2
    assert rows["Борис Ким"]["answered"] == 1


async def _ответ_после_пересчёта(sessionmaker, seed) -> tuple[str, int]:
    """Сегодняшний диалог с ответом оператора — уже ПОСЛЕ обновления витрины.

    Фикстура обновляет витрину в самом конце, поэтому всё, что заведено здесь,
    в ней заведомо отсутствует. Возвращает пару «кто ответил, за сколько
    секунд» — ожидания тестов считаются от неё, а не зашиты числом: в первые
    минуты после московской полуночи отметки упираются в границу суток.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    midnight = datetime.combine(st.today_msk(), time(), tzinfo=st.MSK).astimezone(UTC)
    первое = max(now - timedelta(minutes=20), midnight)
    ответ = min(первое + timedelta(minutes=5), now)
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id="managers-today",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[1],
            assignee_id=seed.users["u1"],
            status="in_progress",
            last_message_at=ответ,
        )
        s.add(conv)
        await s.flush()
        s.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="сломалась стиралка",
                    attachments=[],
                    created_at=первое,
                ),
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=seed.users["u1"],
                    body="ответ",
                    attachments=[],
                    created_at=ответ,
                ),
                AuditLog(
                    user_id=seed.users["u1"],
                    action="conversation.assigned",
                    entity="conversation",
                    entity_id=str(conv.id),
                    details={"assignee_id": str(seed.users["u1"]), "by": "self"},
                    created_at=ответ,
                ),
            ]
        )
        await s.commit()
    return str(seed.users["u1"]), int((ответ - первое).total_seconds())


async def test_manager_table_today_does_not_wait_for_the_refresh(seed, sessionmaker):
    """Соседние колонки ОДНОЙ строки обязаны быть из одного времени (09.09).

    Что было. «Принято», «Закрыто» и «Сообщений» считаются по ``audit_log`` и
    ``messages`` — они живые всегда. «Ответил первым» и обе медианы FRT читали
    витрину, которую пересчитывают раз в час. Диспетчер, ответивший после
    последнего пересчёта, показывал «Принято 1 · Сообщений 1 · Ответил первым
    0» — три числа из одной строки на два разных момента времени.

    Витрина здесь намеренно не пересчитывается: пересчитай её — и проверять
    станет нечего.
    """
    кто, frt = await _ответ_после_пересчёта(sessionmaker, seed)

    today = st.today_msk()
    async with sessionmaker() as db:
        payload = await st.managers(db, st.Period(today, today), st.Filters())

    строка = next(row for row in payload["rows"] if row["manager_id"] == кто)
    assert строка["taken"] == 1  # колонка была живой и раньше
    assert строка["messages_sent"] == 1  # и эта тоже
    assert строка["answered"] == 1  # а эта отставала на час
    assert строка["frt_median_sec"] == frt
    # «Итого» считается отдельным запросом по всей выборке — у него был свой
    # источник и та же беда.
    assert payload["totals"]["answered"] == 1
    assert payload["totals"]["frt_median_sec"] == frt
    # Признак для подписи на экране: «на момент открытия», а не «по витрине».
    assert payload["period_live"] is True


async def test_manager_table_split_equals_full_live(seed, sessionmaker):
    """Раскол «витрина + сегодняшний хвост» даёт ТЕ ЖЕ цифры, что полный live.

    Тот же сторож, что у FRT и «повторных клиентов»: половины складываются
    СТРОКАМИ, поэтому медиана обязана остаться прежней. Разойдись они — и
    ускорение оплачивалось бы неверными числами.
    """
    await _ответ_после_пересчёта(sessionmaker, seed)

    period = st.Period(seed.days[0], st.today_msk())
    async with sessionmaker() as db:
        # метки свежести нет → раскола нет, весь период считается живьём
        полный = await st.managers(db, period, st.Filters())
        # метка свежее московской полуночи → витрина берёт всё до неё
        раскол = await st.managers(
            db, period, st.Filters(), mv_refreshed_at=datetime.now(UTC).isoformat()
        )
    assert раскол == полный
    assert полный["totals"]["answered"] > 0  # иначе сравнивались бы два нуля


# ============================================================== /stats/my/today


async def test_my_today_agrees_with_manager_table(seed, sessionmaker, pg_engine, redis):
    """06 §6.2: виджет менеджера и таблица руководителя обязаны сходиться."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessionmaker() as db:
        conv = Conversation(
            channel="avito",
            external_chat_id="stats-today",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[1],
            assignee_id=seed.users["u1"],
            status="in_progress",
            last_message_at=now,
        )
        db.add(conv)
        await db.flush()
        db.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="сегодня",
                    attachments=[],
                    created_at=now - timedelta(minutes=2),
                ),
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=seed.users["u1"],
                    body="ответ",
                    attachments=[],
                    created_at=now - timedelta(minutes=1),
                ),
                AuditLog(
                    user_id=seed.users["u1"],
                    action="conversation.assigned",
                    entity="conversation",
                    entity_id=str(conv.id),
                    details={"assignee_id": str(seed.users["u1"]), "by": "self"},
                    created_at=now,
                ),
            ]
        )
        await db.commit()
    await refresh_mv(pg_engine)

    today = st.today_msk()
    async with sessionmaker() as db:
        widget = await st.my_today(db, redis, seed.users["u1"])
        rows = await st.manager_rows(
            db, st.Period(today, today), st.Filters(manager_ids=(seed.users["u1"],))
        )

    assert widget["messages_sent_today"] == 1
    assert widget["answered_today"] == 1
    assert widget["frt_median_sec_today"] == 60
    assert widget["taken_today"] == 1
    assert widget["closed_today"] == 0
    assert widget["active_now"] == 2  # c4 из сценария + сегодняшний
    assert widget["waiting_reply_now"] == 0  # оператор ответил — отметка снята

    assert len(rows) == 1
    assert rows[0]["messages_sent"] == widget["messages_sent_today"]
    assert rows[0]["answered"] == widget["answered_today"]
    assert rows[0]["frt_median_sec"] == widget["frt_median_sec_today"]
    assert rows[0]["taken"] == widget["taken_today"]


async def test_my_today_waiting_matches_the_owner_card(seed, sessionmaker, redis):
    """«Ждут моего ответа» у оператора и «Ждут ответа» у руководителя — одно число.

    06 §6.2 требует, чтобы виджет и дашборд сходились цифра в цифру, а считали
    они разное: виджет выводил «последнее видимое сообщение — от клиента»
    подзапросом, дашборд брал длину очереди. Теперь оба читают одну отметку.

    Проверяется на самом дорогом краевом случае — НЕОТПРАВЛЕННОМ ответе.
    Подзапрос смотрел только на `direction`, поэтому `failed`-исходящее гасило
    ожидание: у оператора в виджете стоял ноль, а сторож в это же время слал
    ему напоминание «клиент ждёт». Клиент ответа не получил ни в одном из
    смыслов, и ждущим он остаётся.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id="waiting-undelivered",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[1],
            assignee_id=seed.users["u2"],
            status="in_progress",
            last_message_at=now,
            awaiting_since=now - timedelta(minutes=20),
        )
        s.add(conv)
        await s.flush()
        s.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="а мастер приедет?",
                    attachments=[],
                    created_at=now - timedelta(minutes=20),
                ),
                # последнее видимое сообщение — исходящее, но НЕ доставленное
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=seed.users["u2"],
                    body="перезвоню",
                    attachments=[],
                    delivery_status="failed",
                    created_at=now - timedelta(minutes=1),
                ),
            ]
        )
        await s.commit()

    async with sessionmaker() as db:
        widget = await st.my_today(db, redis, seed.users["u2"])
        snapshot = await st.snapshot_now(db, st.Filters(manager_ids=(seed.users["u2"],)))

    assert widget["waiting_reply_now"] == 2  # c2 из сценария + этот
    assert widget["waiting_reply_now"] == snapshot["waiting_now"]


async def test_my_today_counts_a_transferred_dialog_once(seed, sessionmaker, redis):
    """Виджет «Взято сегодня» — та же ошибка счёта событий, что и в таблице.

    Виджет оператора и отчёт руководителя обязаны сходиться цифра в цифру
    (06 §6.2), поэтому DISTINCT нужен в обоих запросах; починка только одного
    из них развела бы два экрана вместо того, чтобы их свести.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id="taken-once",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[1],
            assignee_id=seed.users["u2"],
            status="in_progress",
            last_message_at=now,
        )
        s.add(conv)
        await s.flush()
        # взял из очереди, отдал, забрал обратно — три события на один диалог
        for shift in (0, 1, 2):
            s.add(
                AuditLog(
                    user_id=seed.users["u2"],
                    action="conversation.assigned",
                    entity="conversation",
                    entity_id=str(conv.id),
                    details={"assignee_id": str(seed.users["u2"]), "by": "transfer"},
                    created_at=now - timedelta(minutes=shift),
                )
            )
        await s.commit()

    async with sessionmaker() as db:
        widget = await st.my_today(db, redis, seed.users["u2"])
    assert widget["taken_today"] == 1


async def test_my_today_is_cached(seed, sessionmaker, redis):
    async with sessionmaker() as db:
        await st.my_today(db, redis, seed.users["u1"])
    assert await redis.exists(f"stats:my:{seed.users['u1']}:{st.today_msk().isoformat()}")


# ================================================================= экспорт (06 §5)


async def test_export_conversation_rows_from_mv(seed, sessionmaker, tmp_path, monkeypatch):
    """Лист «Диалоги» серверным курсором: поля 06 §5.2 и формат CSV для Excel-RU."""
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    path = st.export_dir() / "conversations.csv"
    async with sessionmaker() as db:
        written = await st.write_csv(
            path, st.CONV_HEADERS, st.conversation_rows(db, seed.period, st.Filters())
        )

    assert written == 6  # шесть диалогов периода
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM — иначе русский Excel даст кракозябры
    lines = raw.decode("utf-8-sig").split("\r\n")
    assert lines[0].split(";")[0] == "Первое сообщение (МСК)"
    body = [line for line in lines[1:] if line]
    assert len(body) == 6
    # дата первого сообщения выгружается в московских часах, а не в UTC
    assert body[0].startswith(seed.days[0].isoformat())
    assert "LP-Основной" in body[0]


async def test_export_conversations_sheet_prints_the_status_in_russian(seed, sessionmaker):
    """Лист «Диалоги»: статус переведён, и переведён ОДНИМ словарём (STATS-09).

    В отчёте, который читает владелец, стояло `in_progress`: SQL брал
    `s.status` из представления и клал его в ячейку как есть. При этом
    соседняя выгрузка с экрана /dialogs те же значения переводила — один и
    тот же лист «Диалоги» из двух мест продукта выглядел по-разному.

    Проверка `is` не педантизм: скопированный сюда словарь дал бы зелёный тест
    и разошёлся с оригиналом на первом же новом статусе — именно так дефект и
    появился.
    """
    async with sessionmaker() as db:
        rows = [row async for row in st.conversation_rows(db, seed.period, st.Filters())]

    column = st.CONV_HEADERS.index("Статус")
    assert {row[column] for row in rows} == {"Новый", "В работе", "Закрыт"}
    assert st.STATUS_RU is TABLE_STATUS_RU


async def test_export_conversations_sheet_prints_who_closed_in_russian(seed, sessionmaker):
    """Столбец «Закрыт кем» — по-русски (та же болезнь, что вылечили у «Статуса»).

    В ячейке стояло машинное `operator` / `bot` прямо из `details->>'by'`.
    Правку STATS-09 сделали по букве дефекта — перевели соседний столбец
    «Статус» — и оставили в той же строке того же листа второе машинное поле.
    """
    async with sessionmaker() as db:
        rows = [row async for row in st.conversation_rows(db, seed.period, st.Filters())]

    column = st.CONV_HEADERS.index("Закрыт кем")
    printed = {row[column] for row in rows}
    # c1 закрыл оператор, c3 — бот; остальные диалоги не закрывали ни разу
    assert printed == {"Оператор", "Бот", None}
    assert "operator" not in printed and "bot" not in printed


async def test_export_summary_sheet_dates_the_materialized_half(
    seed, sessionmaker, tmp_path, redis, monkeypatch
):
    """Лист «Сводка» отдельно называет возраст витрины, а не только время сборки.

    «Выгружено» — когда собран файл, а лист «Диалоги» читается из
    `mv_conversation_stats`, которую пересчитывают раз в час. Выгрузка в 20:55
    печатает по нему состояние на 20:05. Пока в файле была одна дата, отличить
    одно от другого было нечем.

    ⚠ ПОДПИСЬ ПЕРЕИМЕНОВАНА 30.08, И ЭТО НЕ КОСМЕТИКА. Стояло «Диалоги и FRT —
    по данным на …», но после STATS-04 (29.08) сводка периода с сегодняшним
    днём считается ЖИВЬЁМ: и «Диалогов новых», и FRT здесь свежие, витринным
    остался только лист «Диалоги». Прежняя подпись обещала витрину половине
    чисел, которые уже не с витрины, — и владелец, сверяя лист «Диалоги» со
    «Сводкой» ТОГО ЖЕ файла, получал разное и не имел ничего, чем это
    объяснить. Теперь подписей две, каждая про своё.
    """
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    await redis.set(st.STATS_REFRESHED_KEY, "2026-08-11T17:05:00+00:00")
    job_id = "cc55dd66ee77ff88"
    params = {
        "format": "xlsx",
        "date_from": seed.period.date_from.isoformat(),
        "date_to": seed.period.date_to.isoformat(),
        "sheets": ["summary"],
        "user_id": str(seed.users["admin"]),
        "account_id": None,
        "manager_ids": None,
    }
    await st.export_stats({"redis": redis, "db_session_factory": sessionmaker}, job_id, params)

    status = await redis.hgetall(st.export_status_key(job_id))
    sheet = openpyxl.load_workbook(st.export_dir() / status["relpath"].split("/", 1)[1])["Сводка"]
    pairs = {row[0].value: row[1].value for row in sheet.iter_rows()}

    assert pairs["Лист «Диалоги» — по данным на"] == "2026-08-11 20:05 (МСК)"
    # Возраст витрины и время сборки — РАЗНЫЕ строки: совпади они, смысл
    # правки пропал бы, а тест этого не заметил.
    assert pairs["Выгружено"] != pairs["Лист «Диалоги» — по данным на"]
    # И сводка честно называет СВОЙ способ счёта: период тестовых данных лежит
    # в прошлом, значит она с витрины. На периоде с сегодняшним днём здесь
    # стояло бы «на момент выгрузки» — это стережёт `test_stats_export.py`.
    assert pairs["Сводка посчитана"] == "по витрине"


async def test_export_managers_sheet_totals_match_the_screen(
    seed, sessionmaker, tmp_path, redis, monkeypatch
):
    """Строка «Итого» в файле — та же, что на экране, включая медианы.

    Лист собирался из `manager_rows()` и складывал столбцы сам, а медиану
    сложить нельзя — в файле на её месте стояла пустота. Владелец видел на
    /stats «Итого · FRT мед.» с числом, открывал тот же отчёт в Excel и
    находил пустую клетку, неотличимую от «данных нет».
    """
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    job_id = "dd77ee88ff99aa00"
    params = {
        "format": "xlsx",
        "date_from": seed.period.date_from.isoformat(),
        "date_to": seed.period.date_to.isoformat(),
        "sheets": ["managers"],
        "user_id": str(seed.users["admin"]),
        "account_id": None,
        "manager_ids": None,
    }
    await st.export_stats({"redis": redis, "db_session_factory": sessionmaker}, job_id, params)

    status = await redis.hgetall(st.export_status_key(job_id))
    book = openpyxl.load_workbook(st.export_dir() / status["relpath"].split("/", 1)[1])
    sheet = book["Менеджеры"]
    last = [cell.value for cell in sheet[sheet.max_row]]

    async with sessionmaker() as db:
        expected = (await st.managers(db, seed.period, st.Filters()))["totals"]

    # Подпись итога несёт оговорку про уникальность (H-02): сумма столбца
    # законно больше итога, потому что переданный диалог считается один раз.
    assert last[0].startswith("Итого")
    assert "один раз" in last[0]
    assert last[MANAGER_COL["Принято"]] == expected["taken"]
    assert last[MANAGER_COL["Отправлено"]] == expected["messages_sent"]
    # Медианы — то самое, чего в файле не было; и они не None на этих данных.
    assert last[MANAGER_COL["FRT мед., с"]] == expected["frt_median_sec"] is not None
    assert last[MANAGER_COL["FRT мед. (раб.), с"]] == expected["frt_median_biz_sec"] is not None


MANAGER_COL = {name: idx for idx, name in enumerate(st.MANAGER_HEADERS)}


async def test_export_job_writes_file_status_and_audit(
    seed, sessionmaker, tmp_path, redis, monkeypatch
):
    """Полный прогон ARQ-задачи: файл на диске, статус в Redis, запись в audit_log
    (выгрузка персональных данных клиентов обязана быть видима в журнале, 06 §5.4)."""
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    job_id = "b8c4d1e2f3a45566"
    params = {
        "format": "csv",
        "date_from": seed.period.date_from.isoformat(),
        "date_to": seed.period.date_to.isoformat(),
        "sheets": ["summary", "managers", "conversations"],
        "user_id": str(seed.users["admin"]),
        "account_id": None,
        "manager_ids": None,
    }
    await redis.set(st.export_active_key(seed.users["admin"]), job_id)

    outcome = await st.export_stats(
        {"redis": redis, "db_session_factory": sessionmaker}, job_id, params
    )

    assert outcome == "done"
    status = await redis.hgetall(st.export_status_key(job_id))
    assert status["status"] == "done"
    assert status["rows"] == "6"
    written = st.export_dir() / status["relpath"].split("/", 1)[1]
    assert written.exists()
    # слот освобождён — следующий экспорт не упрётся в 409
    assert not await redis.exists(st.export_active_key(seed.users["admin"]))

    async with sessionmaker() as db:
        row = await st._row(
            db,
            "SELECT user_id, details FROM audit_log WHERE action = 'stats.exported'",
            {},
        )
    assert row["user_id"] == seed.users["admin"]
    assert row["details"]["rows"] == 6
    assert row["details"]["format"] == "csv"


async def test_export_job_writes_xlsx_with_all_sheets(
    seed, sessionmaker, tmp_path, redis, monkeypatch
):
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    job_id = "aa11bb22cc33dd44"
    params = {
        "format": "xlsx",
        "date_from": seed.period.date_from.isoformat(),
        "date_to": seed.period.date_to.isoformat(),
        "sheets": ["summary", "managers", "conversations"],
        "user_id": str(seed.users["admin"]),
        "account_id": None,
        "manager_ids": None,
    }
    await st.export_stats({"redis": redis, "db_session_factory": sessionmaker}, job_id, params)

    status = await redis.hgetall(st.export_status_key(job_id))
    assert status["status"] == "done"
    workbook = openpyxl.load_workbook(st.export_dir() / status["relpath"].split("/", 1)[1])
    assert workbook.sheetnames == ["Сводка", "Менеджеры", "Диалоги"]
    managers_sheet = workbook["Менеджеры"]
    assert managers_sheet.cell(row=1, column=1).value == "Менеджер"
    last_row = [cell.value for cell in managers_sheet[managers_sheet.max_row]]
    assert last_row[0].startswith("Итого")


async def test_export_job_marks_failure_when_over_limit(
    seed, sessionmaker, tmp_path, redis, monkeypatch
):
    """Больше лимита строк — job падает в ``failed`` с подсказкой, а не молча
    отдаёт обрезанный файл (06 §5.4)."""
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(st, "MAX_CONV_ROWS", 2)
    job_id = "ff00ff00ff00ff00"
    params = {
        "format": "csv",
        "date_from": seed.period.date_from.isoformat(),
        "date_to": seed.period.date_to.isoformat(),
        "sheets": ["conversations"],
        "user_id": str(seed.users["admin"]),
        "account_id": None,
        "manager_ids": None,
    }
    outcome = await st.export_stats(
        {"redis": redis, "db_session_factory": sessionmaker}, job_id, params
    )
    assert outcome == "failed"
    status = await redis.hgetall(st.export_status_key(job_id))
    assert status["status"] == "failed"
    assert "сузьте период" in status["error"]
    # обрезанного файла на диске не осталось
    assert not list(st.export_dir().glob("*.csv"))


# ======================================================= endpoints поверх живой БД


@pytest.fixture
async def api_client(sessionmaker, redis) -> AsyncIterator[httpx.AsyncClient]:
    """Боевое приложение поверх настоящего PostgreSQL и Redis.

    Юнит-тесты проверяют контур роутера с подменёнными сервисами, тесты выше —
    SQL напрямую. Шов между ними виден только здесь: реальная роль из строки
    БД, реальные запросы к MV и партициям, реальная сериализация ответа
    (``numeric`` из ``percentile_cont`` обязан доехать до JSON числом).
    """
    from app.api import deps
    from app.main import create_app

    application = create_app()
    # Монтирование роутера в main.py — чужая зона: если оно уже сделано,
    # проверяем боевую сборку, иначе подключаем локально.
    if not any(getattr(r, "path", "").startswith("/api/v1/stats") for r in application.routes):
        from app.api.routes import stats as stats_routes

        application.include_router(stats_routes.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[deps.get_db] = override_get_db
    application.dependency_overrides[deps.get_redis] = lambda: redis
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


def bearer(user_id: uuid.UUID, role: str) -> dict[str, str]:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id=str(user_id), role=role)}"}


def period_params(period: st.Period) -> dict[str, str]:
    return {"date_from": period.date_from.isoformat(), "date_to": period.date_to.isoformat()}


async def test_stats_endpoints_end_to_end_for_head(seed, api_client):
    """Полный проход руководителя по дашборду на известных данных."""
    headers = bearer(seed.users["head"], "head")
    params = period_params(seed.period)

    summary = await api_client.get("/api/v1/stats/summary", params=params, headers=headers)
    assert summary.status_code == 200, summary.text
    cards = summary.json()["cards"]
    assert cards["conversations_new"]["value"] == 6
    assert cards["conversations_closed"]["value"] == 2
    assert cards["frt_operator"]["median_sec"] == 1500
    assert cards["frt_operator"]["median_biz_sec"] == 150  # рабочие часы, SQL-функция
    assert cards["bot_closed"]["closed_total"] == cards["conversations_closed"]["value"]
    assert cards["phones_collected"]["by_source"] == {"bot": 1, "regex": 1, "manual": 1}

    timeseries = await api_client.get(
        "/api/v1/stats/timeseries",
        params={**params, "metric": "conversations_new", "group": "day"},
        headers=headers,
    )
    assert timeseries.status_code == 200, timeseries.text
    assert len(timeseries.json()["points"]) == seed.period.days

    heatmap = await api_client.get("/api/v1/stats/heatmap", params=params, headers=headers)
    assert heatmap.status_code == 200, heatmap.text
    assert len(heatmap.json()["cells"]) == 168

    managers = await api_client.get(
        "/api/v1/stats/managers",
        params={**params, "sort": "closed", "order": "desc"},
        headers=headers,
    )
    assert managers.status_code == 200, managers.text
    body = managers.json()
    assert body["rows"][0]["manager_id"] == str(seed.users["u1"])  # единственный, кто закрывал
    assert body["totals"]["messages_sent"] == 4


async def test_manager_sees_only_own_widget_over_live_db(seed, api_client):
    """Роль читается из строки БД, а не из JWT: подпись «head» в токене
    менеджера не должна открыть ему дашборд (01 §12)."""
    manager = bearer(seed.users["u1"], "manager")
    forbidden = await api_client.get(
        "/api/v1/stats/summary", params=period_params(seed.period), headers=manager
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    from app.core.security import create_access_token

    forged = {
        "Authorization": f"Bearer {create_access_token(user_id=str(seed.users['u1']), role='head')}"
    }
    assert (await api_client.get("/api/v1/stats/summary", headers=forged)).status_code == 403

    mine = await api_client.get("/api/v1/stats/my/today", headers=manager)
    assert mine.status_code == 200, mine.text
    assert set(mine.json()) == {
        "date",
        "active_now",
        "waiting_reply_now",
        "taken_today",
        "closed_today",
        "messages_sent_today",
        "frt_median_sec_today",
        "answered_today",
        # ЗДЕСЬ БЫЛО `snoozed_now` — «Отложено» отдельным числом (docs/38 §6.1).
        # Снято 12 августа вместе со статусом. Множество сравнивается ТОЧНО
        # (`==`, не `<=`) намеренно: поле, которое сервер продолжает считать и
        # отдавать, а фронт не читает, — это лишний запрос к базе на каждом
        # открытии виджета и обещание функции, которой нет.
    }


# ============================================================ MV refresh и партиции


async def test_scheduler_job_refreshes_mv_and_writes_freshness_mark(
    seed, sessionmaker, pg_engine, redis, monkeypatch
):
    """Сам job планировщика (06 §3.2), а не его копия в тесте.

    Главное, что тут проверяется, — ``REFRESH ... CONCURRENTLY`` действительно
    проходит из кода job'а: в транзакционном блоке PostgreSQL его запрещает,
    поэтому соединение обязано быть в AUTOCOMMIT.
    """
    from app.core import redis as redis_mod
    from app.db import session as db_mod
    from app.scheduler.jobs import stats as stats_job

    monkeypatch.setattr(db_mod, "engine", pg_engine)
    monkeypatch.setattr(redis_mod, "client", redis)

    await stats_job.refresh_stats_mv()

    mark = await redis.get(st.STATS_REFRESHED_KEY)
    assert mark is not None
    async with sessionmaker() as db:
        assert await st.refreshed_at(redis) == mark
        # MV после прогона job'а содержит диалоги сценария
        assert await db.scalar(text(f"SELECT count(*) FROM {st.MV}")) == 8


async def test_refresh_mv_concurrently_picks_up_new_rows(seed, sessionmaker, pg_engine):
    async with sessionmaker() as db:
        before = await db.scalar(text(f"SELECT count(*) FROM {st.MV}"))
        conv = Conversation(
            channel="avito",
            external_chat_id="stats-refresh",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[2],
            status="new",
            last_message_at=datetime.now(UTC),
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="новый",
                attachments=[],
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

    # до refresh MV ещё не знает о диалоге — это и есть «свежесть ≤ 1 ч» (06 §3.3)
    async with sessionmaker() as db:
        assert await db.scalar(text(f"SELECT count(*) FROM {st.MV}")) == before

    await refresh_mv(pg_engine)  # CONCURRENTLY — дашборд не блокируется
    async with sessionmaker() as db:
        assert await db.scalar(text(f"SELECT count(*) FROM {st.MV}")) == before + 1


async def test_messages_queries_prune_partitions(pg_engine):
    """Правило 06 §0.2: диапазон по created_at обязан резать партиции."""
    await ensure_partitions(pg_engine)
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    window_end = month_start + timedelta(days=2)
    current = f"messages_y{month_start.year:04d}m{month_start.month:02d}"
    previous = f"messages_y{previous_month.year:04d}m{previous_month.month:02d}"

    async with pg_engine.connect() as conn:
        plan_rows = (
            await conn.execute(
                text(
                    "EXPLAIN SELECT count(*) FROM messages m "
                    f"WHERE m.created_at >= timestamptz '{month_start.isoformat()}' "
                    f"AND m.created_at < timestamptz '{window_end.isoformat()}' "
                    "AND m.direction = 'in' AND m.sender_type = 'client'"
                )
            )
        ).scalars()
        plan = "\n".join(plan_rows)
    assert current in plan
    assert previous not in plan, plan


# ------------------------------------------------------- сводка недели по каналам


@pytest.fixture
async def week_seed(pg_engine: AsyncEngine, sessionmaker) -> SimpleNamespace:
    """Своя посевная, а не общий ``seed``: тот кладёт диалоги на десять дней
    назад, а окно сводки — семь. Растягивать общий сценарий ради одного теста
    значило бы менять ожидания в двух десятках чужих проверок."""
    await ensure_partitions(pg_engine)
    today = st.today_msk()

    async with sessionmaker() as s:
        operator = User(
            email="week@stats.test",
            password_hash="x",
            full_name="Оператор Недели",
            role="manager",
            is_active=True,
        )
        s.add(operator)
        await s.flush()

        accounts = {}
        for key, title, avito_id in (("a", "Неделя-А", 710100), ("b", "Неделя-Б", 710200)):
            row = AvitoAccount(
                title=title,
                avito_user_id=avito_id,
                access_token_enc=b"a",
                refresh_token_enc=b"r",
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
                status="active",
                webhook_secret="s",
            )
            s.add(row)
            accounts[key] = row
        await s.flush()

        specs = [
            # Канал А: три обращения за неделю, из них одно без ответа. Дни
            # разные — проверяем и раскладку по столбикам, а не только итог.
            ("a1", "a", today - timedelta(days=6), 10, True),
            ("a2", "a", today - timedelta(days=6), 11, False),
            ("a3", "a", today, 9, True),
            # Канал Б: одно обращение, отвечено.
            ("b1", "b", today - timedelta(days=1), 12, True),
            # За границей окна — не должно попасть НИ В ОДНО число.
            ("a-old", "a", today - timedelta(days=30), 10, True),
        ]

        clients = {}
        for key, *_ in specs:
            client = Client(channel="avito", external_id=f"week-{key}", name=f"Клиент {key}")
            s.add(client)
            clients[key] = client
        # Клиентов сбрасываем ДО диалогов: `client.id` появляется только после
        # flush, а до него внешний ключ ушёл бы в базу пустым.
        await s.flush()

        convs = []
        for key, account, day, hour, answered in specs:
            row = Conversation(
                channel="avito",
                external_chat_id=f"week-{key}",
                account_id=accounts[account].id,
                client_id=clients[key].id,
                status="closed" if answered else "new",
                last_message_at=at(day, hour),
            )
            s.add(row)
            convs.append((row, day, hour, answered))
        await s.flush()

        for row, day, hour, answered in convs:
            s.add(
                Message(
                    conversation_id=row.id,
                    direction="in",
                    sender_type="client",
                    body="текст",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=at(day, hour),
                )
            )
            if answered:
                s.add(
                    Message(
                        conversation_id=row.id,
                        direction="out",
                        sender_type="operator",
                        sender_user_id=operator.id,
                        body="ответ",
                        attachments=[],
                        delivery_status="delivered",
                        created_at=at(day, hour, 5),
                    )
                )
        await s.commit()

        return SimpleNamespace(accounts={k: v.id for k, v in accounts.items()}, today=today)


async def test_account_week_summary(week_seed, pg_engine: AsyncEngine, sessionmaker) -> None:
    """Сводка канала за неделю: итог, «без ответа» и форма по дням.

    Единственный тест на этот SQL, и другого места для него нет: запрос
    опирается на материализованный вид и на ``AT TIME ZONE``, которых в SQLite
    не существует, — на юнитах он молча отдаёт пустую сводку.
    """
    from app.services import account_stats

    await refresh_mv(pg_engine)

    async with sessionmaker() as s:
        summaries = await account_stats.summaries(s)

    a = summaries[week_seed.accounts["a"]]
    b = summaries[week_seed.accounts["b"]]

    # Тридцатидневный диалог остался за окном — иначе итог был бы четыре.
    assert (a.total, a.missed) == (3, 1)
    assert (b.total, b.missed) == (1, 0)

    # Форма недели: длина ровно семь, день без обращений — ноль, а не пропуск.
    assert len(a.daily) == 7
    assert sum(a.daily) == a.total
    assert a.daily[0] == 2  # оба обращения шестидневной давности
    assert a.daily[-1] == 1  # сегодняшнее
    assert a.daily[1:-1] == [0] * 5


async def test_account_week_summary_holds_after_moscow_midnight(
    week_seed, pg_engine: AsyncEngine, sessionmaker
) -> None:
    """Ночью график не съезжает на день (НАЙДЕНО 12 августа в 00:31 МСК).

    Начало окна бралось по UTC, а день столбика приходит из запроса по Москве.
    Днём три часа разницы незаметны; с полуночи до трёх по Москве московского
    «сегодня» в списке дней просто НЕ БЫЛО — сегодняшний столбик пропадал, а
    итог его считал. На экране это выглядело как «за сегодня ноль обращений»
    при непустом итоге; ночью у нас работают, так что видел это живой человек.

    Момент задан явно, а не взят из часов: иначе проверка работала бы три часа
    в сутки и молчала остальные двадцать одни.

    Проверка ломанием: вернуть `start_day = moment.date()` в
    ``app/services/account_stats.py`` — тест краснеет на сегодняшнем столбике.
    """
    from app.services import account_stats

    await refresh_mv(pg_engine)

    # 00:30 по Москве сегодняшнего дня — то есть 21:30 UTC ВЧЕРАШНЕГО.
    just_after_midnight = datetime.combine(week_seed.today, time(0, 30), tzinfo=st.MSK)

    async with sessionmaker() as s:
        summaries = await account_stats.summaries(s, now=just_after_midnight.astimezone(UTC))

    a = summaries[week_seed.accounts["a"]]
    assert sum(a.daily) == a.total, "итог и столбики разошлись — окно и подписи в разных поясах"
    assert a.daily[-1] == 1, "сегодняшний столбик потерян"
    assert a.daily[0] == 2, "неделя съехала на день"


async def test_account_week_summary_puts_a_night_dialog_on_its_moscow_day(
    week_seed, pg_engine: AsyncEngine, sessionmaker
) -> None:
    """Обращение в 02:00 по Москве — в столбик своего дня (проверка 24.09).

    День считался двойным `AT TIME ZONE`: наивное UTC-время читалось как
    московское, и всё с 00:00 до 03:00 МСК уезжало во вчерашний столбик. На
    бою так лежали 7,5 % строк недели, а из первого дня окна они выпадали из
    столбиков совсем — сумма столбиков расходилась с итогом.
    """
    from app.services import account_stats

    night = week_seed.today - timedelta(days=3)
    async with sessionmaker() as s:
        client = Client(channel="avito", external_id="week-night", name="Клиент ночь")
        s.add(client)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="week-night",
            account_id=week_seed.accounts["b"],
            client_id=client.id,
            status="new",
            last_message_at=at(night, 2),
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="ночью",
                attachments=[],
                delivery_status="delivered",
                created_at=at(night, 2),
            )
        )
        await s.commit()
    await refresh_mv(pg_engine)

    async with sessionmaker() as s:
        b = (await account_stats.summaries(s))[week_seed.accounts["b"]]

    assert sum(b.daily) == b.total == 2
    assert b.daily[3] == 1, "ночное обращение уехало во вчерашний столбик"


async def test_account_week_summary_survives_missing_view(sessionmaker) -> None:
    """Нет вида — нет сводки, но и падения нет.

    Экран каналов обязан открываться на свежей базе, где миграция статистики
    ещё не отработала: без сводки администратор канал подключит, без списка —
    нет. Транзакция при этом должна остаться пригодной, иначе Postgres
    отвергнет всё, что запрашивается после.
    """
    from app.services import account_stats

    async with sessionmaker() as s:
        await s.execute(text("DROP MATERIALIZED VIEW IF EXISTS mv_conversation_stats"))

        assert await account_stats.summaries(s) == {}

        # Сессия жива: сбой ограничен точкой сохранения.
        assert (await s.execute(text("SELECT 1"))).scalar() == 1
        await s.rollback()


async def test_export_no_longer_carries_the_outcome_or_the_money(
    seed, sessionmaker, tmp_path, monkeypatch
):
    """Ни «Результат», ни «Сумма, ₽» до файла больше не доезжают.

    ЧТО ЗДЕСЬ БЫЛО. Два теста: «результат доезжает до выгрузки словом и
    рублями» (#37) и «пустое поле честно значит „не спрашивали“». Оба сняты
    12 августа вместе с функцией — решением владельца.

    ЦЕНА, ЗАПИСАННАЯ ЗДЕСЬ НАМЕРЕННО. «Сумма, ₽» была единственным столбцом
    всей выгрузки, где стояли деньги. Диалог, дошедший до выезда, и диалог, где
    человек передумал, снова выглядят в отчёте одинаково: закрыт, столько-то
    сообщений, столько-то секунд. На вопрос «какой канал окупается» — первый,
    который задаёт владелец, глядя на девять аккаунтов, — отчёт не отвечает.

    ПРОВЕРЯЕТСЯ ЧЕРЕЗ ФАЙЛ, а не по составу `CONV_HEADERS` (это делает
    `tests/unit/test_stats_export.py`). По дороге стоят материализованное
    представление, серверный курсор и запись CSV; здесь важно, что колонка
    исчезла на ВСЁМ пути, и что запись файла не сломалась по дороге.

    ЗАПОЛНЕННЫЕ КОЛОНКИ В БАЗЕ НАРОЧНО ОСТАВЛЕНЫ НЕПУСТЫМИ: строки с
    результатом на бою есть (их писали до выката), и выгрузка обязана
    проходить мимо них молча, а не спотыкаться.
    """
    import sqlalchemy as sa

    from app.models import Conversation

    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))

    async with sessionmaker() as db:
        await db.execute(sa.update(Conversation).values(outcome="visit", outcome_amount=450_000))
        await db.commit()

    path = st.export_dir() / "outcome-gone.csv"
    async with sessionmaker() as db:
        await st.write_csv(
            path, st.CONV_HEADERS, st.conversation_rows(db, seed.period, st.Filters())
        )

    text = path.read_bytes().decode("utf-8-sig")
    lines = [line for line in text.split("\r\n") if line]
    header = lines[0].split(";")
    assert "Результат" not in header
    assert not [h for h in header if "Сумма" in h]
    assert "Выезд назначен" not in text, "подпись исхода просочилась в файл"
    assert "4500" not in text, "сумма просочилась в файл"

    # И СТОЛБЦЫ НЕ УЕХАЛИ. Проверка ради того самого сдвига на клетку: подписей
    # и значений обязано быть поровну, иначе файл валиден и нечитаем.
    for line in lines[1:]:
        assert len(line.split(";")) == len(header), f"строка разошлась с заголовком: {line}"


async def test_changing_work_hours_changes_the_numbers_without_a_migration(
    seed, sessionmaker, pg_engine
):
    """ГЛАВНАЯ ПРОВЕРКА #41: часы меняются настройкой и доезжают до витрины.

    ЗАЧЕМ. «Скорость первого ответа в рабочие часы» считалась по интервалу
    10:00–20:00, зашитому и в питоновском эталоне, и в умолчаниях SQL-функции.
    Поменять их можно было только правкой файлов с перезапуском — то есть через
    инженера, хотя решение управленческое: меняются смены, меняется окно.

    А цифра при этом врёт по существу: у заказчика двенадцатичасовые смены, и
    клиенту, написавшему в 20:05 и отвеченному в 10:05 утра, засчитывается
    пять минут вместо четырнадцати часов.

    ЧТО ИМЕННО ЗАПЕРТО ЗДЕСЬ. Не «настройка сохраняется» — это проверяется
    юнитом. Здесь проверяется дорога целиком: настройка → SQL-функция →
    материализованное представление → цифра в ответе. И главное — что для
    смены часов НЕ НУЖНА ни миграция, ни пересборка витрины: определение
    представления не меняется, хватает обычного ежечасного обновления.
    """
    import sqlalchemy as sa

    from app.models import AppSetting
    from app.services import app_settings

    async def frt_biz() -> int:
        async with sessionmaker() as db:
            rows = await db.execute(
                sa.text(f"SELECT sum(frt_operator_biz_sec) FROM {st.MV}")  # noqa: S608
            )
            return int(rows.scalar() or 0)

    await refresh_mv(pg_engine)
    before = await frt_biz()

    # Круглосуточное окно: рабочими становятся все секунды, и «рабочая» цифра
    # обязана вырасти до обычной. Берём именно крайний случай — он не зависит
    # от того, в какие часы засеяны диалоги, и потому не станет ложно-зелёным
    # на других данных.
    async with sessionmaker() as db:
        for key, value in (
            (app_settings.STATS_WORK_START_HOUR, 0),
            (app_settings.STATS_WORK_END_HOUR, 23),
        ):
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        await db.commit()

    await refresh_mv(pg_engine)
    after = await frt_biz()

    assert after > before, (
        "расширили рабочее окно почти на сутки, а «рабочая» скорость ответа не "
        "изменилась — значит настройка до витрины не доезжает"
    )


# ======================================================= раскол витрина/живьём 03.09


async def test_repeat_split_counts_a_client_on_both_sides_once(seed, sessionmaker, pg_engine):
    """Клиент с диалогами ПО ОБЕ СТОРОНЫ границы обязан считаться один раз.

    ⚠ РАДИ ЭТОГО ТЕСТА И НАПИСАН `UNION`. Метрика считает РАЗНЫХ клиентов;
    если половины сложить счётчиками (или объединить `UNION ALL`), такой
    клиент даст двойку — и «повторных клиентов» станет больше, чем клиентов.

    ⚠ ПРЕДЫДУЩАЯ ВЕРСИЯ ЭТОГО НАБОРА ДИВЕРСИЮ ПРОПУСКАЛА. Тест двигал границу
    по всем дням периода и требовал совпадения — но в сценарии не нашлось ни
    одного повторного клиента с диалогами в обеих половинах, и `UNION ALL`
    проходил насквозь. Общий прогон по данным «как есть» не проверяет случай,
    которого в данных нет: состояние надо строить.
    """
    стык = st.msk_day_bounds(seed.period.date_from + timedelta(days=2))[0]
    клиент = seed.clients[7]
    async with sessionmaker() as s:
        # Старый диалог — он и делает клиента «повторным»: у обоих диалогов
        # периода найдётся предыдущий, закрывшийся раньше их первого письма.
        предыстория = Conversation(
            channel="avito",
            external_chat_id="repeat-history",
            account_id=seed.accounts["acc1"],
            client_id=клиент,
            assignee_id=seed.users["u1"],
            status="closed",
            last_message_at=стык - timedelta(days=30),
        )
        s.add(предыстория)
        for метка, когда in (
            ("repeat-before-seam", стык - timedelta(hours=6)),
            ("repeat-after-seam", стык + timedelta(hours=6)),
        ):
            conv = Conversation(
                channel="avito",
                external_chat_id=метка,
                account_id=seed.accounts["acc1"],
                client_id=клиент,
                assignee_id=seed.users["u1"],
                status="closed",
                last_message_at=когда + timedelta(minutes=5),
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="снова я",
                    attachments=[],
                    created_at=когда,
                )
            )
        await s.commit()
    await refresh_mv(pg_engine)

    async with sessionmaker() as db:
        полный = await st.repeat_contacts(db, seed.period, st.Filters(), live=True)
        раскол = await st.repeat_contacts(db, seed.period, st.Filters(), live=True, split_at=стык)
    assert раскол == полный


@pytest.mark.parametrize("сдвиг", [0, 1, 2, 3, 4])
async def test_repeat_split_equals_full_live_at_every_boundary(seed, sessionmaker, сдвиг):
    """«Повторные клиенты» с расколом обязаны давать ТО ЖЕ число.

    Метрика считает РАЗНЫХ клиентов, и ровно поэтому раскол сюда раньше не
    пускали: сложение двух счётчиков задвоило бы клиента, написавшего и до
    границы, и после. Половины объединяются `UNION`'ом по client_id — тест
    двигает границу по всем дням периода и требует совпадения на каждой.

    ⚠ ЗАМЕНИ `UNION` НА `UNION ALL` В `_REPEAT_CLIENTS_SPLIT_SQL` — И ЭТОТ
    ТЕСТ ОБЯЗАН ПОКРАСНЕТЬ. Проверено диверсией: падает на границах, где у
    клиента есть диалоги в обеих половинах.
    """
    граница = st.msk_day_bounds(seed.period.date_from + timedelta(days=сдвиг))[0]
    async with sessionmaker() as db:
        полный = await st.repeat_contacts(db, seed.period, st.Filters(), live=True)
        раскол = await st.repeat_contacts(
            db, seed.period, st.Filters(), live=True, split_at=граница
        )
    assert раскол == полный


async def test_repeat_split_holds_under_filters(seed, sessionmaker):
    """Фильтры действуют на ОБЕ половины — витринную и живую.

    Они фильтруются разными выражениями (`s.account_id` против `c.account_id`),
    и пропуск в одной виден только под фильтром: общий прогон выше его не ловит.
    """
    граница = st.msk_day_bounds(seed.period.date_from + timedelta(days=2))[0]
    срезы = (
        st.Filters(account_id=seed.accounts["acc2"]),
        st.Filters(manager_ids=(seed.users["u1"],)),
    )
    async with sessionmaker() as db:
        for фильтры in срезы:
            полный = await st.repeat_contacts(db, seed.period, фильтры, live=True)
            раскол = await st.repeat_contacts(db, seed.period, фильтры, live=True, split_at=граница)
            assert раскол == полный, фильтры


@pytest.mark.parametrize("metric", ["conversations_new", "frt_operator_median", "messages_in"])
@pytest.mark.parametrize("group", ["day", "hour"])
async def test_timeseries_split_equals_full_live(seed, sessionmaker, metric, group):
    """Ряд с расколом обязан совпасть с полным живым — точка в точку.

    Раскол здесь ложится РОВНО НА СТЫК бакетов (московская полночь), поэтому
    половины не складываются, а дополняют друг друга. Тест берёт период,
    доходящий до сегодня (иначе живого пути нет вовсе), и сверяет два прогона:
    с меткой витрины (раскол) и без неё (всё живьём).

    `messages_in` в наборе намеренно: у него живого близнеца нет, и раскол
    обязан его не трогать — иначе правка тихо сменила бы источник метрике,
    которую не собиралась касаться.

    ⚠ СЕГОДНЯШНИЙ ДИАЛОГ ДОБАВЛЯЕТСЯ ЗДЕСЬ ЖЕ, И БЕЗ НЕГО ТЕСТ ПУСТОЙ. Данные
    набора лежат на десять дней в прошлом, то есть живая половина раскола не
    вернула бы НИ ОДНОЙ строки: сверялись бы два одинаковых витринных ответа,
    а живая ветка не исполнялась бы вовсе. Тот же промах, что и у
    «повторных клиентов» выше.
    """
    сегодня_в_полдень = st.msk_day_bounds(st.today_msk())[0] + timedelta(hours=12)
    async with sessionmaker() as s:
        conv = Conversation(
            channel="avito",
            external_chat_id=f"today-{metric}-{group}",
            account_id=seed.accounts["acc1"],
            client_id=seed.clients[7],
            assignee_id=seed.users["u1"],
            status="in_progress",
            last_message_at=сегодня_в_полдень + timedelta(minutes=3),
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="сегодняшнее",
                attachments=[],
                created_at=сегодня_в_полдень,
            )
        )
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="отвечаю",
                attachments=[],
                created_at=сегодня_в_полдень + timedelta(minutes=3),
            )
        )
        await s.commit()

    период = st.Period(seed.period.date_from, st.today_msk())
    if group == "hour" and период.days > st.MAX_HOUR_GROUP_DAYS:
        pytest.skip("почасовая группировка ограничена периодом")
    свежая_витрина = datetime.now(UTC).isoformat()
    async with sessionmaker() as db:
        живьём = await st.timeseries(db, период, st.Filters(), metric=metric, group=group)
        раскол = await st.timeseries(
            db,
            период,
            st.Filters(),
            metric=metric,
            group=group,
            mv_refreshed_at=свежая_витрина,
        )
    assert раскол == живьём
