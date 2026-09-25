"""Замеры 06.09 на настоящем Postgres: индекс, LATERAL, ветка frt.

Три вещи из аудита 06.09, которые SQLite проверить не может:

1. Миграция 0068 ставит `pg_trgm` и три GIN-индекса, и планировщик ДЕЙСТВИТЕЛЬНО
   берёт индекс по `lower(name)` для условия из `_search_condition` — а для
   прежнего `name ILIKE …` не берёт. Плюс кириллица без учёта регистра: в
   SQLite `lower()` сворачивает только латиницу.

2. Превью строки списка через LATERAL даёт ровно те же строки, что окно
   `row_number()`, — на партиционированной `messages`, с сообщениями в разных
   месяцах, при перемешанном порядке вставки, с заметками и служебными
   записями Авито.

3. Виджет «моя статистика» после сужения ветки frt считает ТО ЖЕ, что и до
   правки, на данных, где есть диалоги вне дня: начатый вчера и отвеченный
   сегодня, заведённый вчера звонком без слов, отвеченный ботом раньше
   человека, недоставленный ответ. Эталон — прежний SQL, дословно.

ДИВЕРСИИ (каждая дала красный, восстановлено байт в байт):
  - в `_last_message_stmt` ветка Postgres: `.desc()` → `.asc()` у `created_at`
    — упал `test_lateral_и_окно_дают_одни_строки_на_партициях`;
  - в `_MY_TODAY_SQL` внутрь `f` добавлена `m.created_at >= :day_start` (та
    самая «граница по дате», которую просил аудит) — упал
    `test_виджет_считает_то_же_что_до_правки`: диалог, начатый вчера, стал
    «отвеченным сегодня».

ДИВЕРСИИ РЕВЬЮ 07.09 — два сторожа оказались пустыми, оба закрыты здесь же:
  - в LATERAL `Message.id.desc()` → `.asc()` — ВСЁ ЗЕЛЕНЕЛО: в данных не было
    двух допущенных записей одного диалога с одной секундой, хотя докстринг их
    обещал. Теперь у диалога `e` такая пара с явными `id`, и «последнее» —
    строго большее из них; после этого диверсия даёт красный.
  - в `_load_related` диалект зашит: `_last_message_stmt("sqlite", …)` — ВСЁ
    ЗЕЛЕНЕЛО и здесь, и в юнитах: обе формы дают одни строки, и по строкам не
    отличить, окном пошёл бой или LATERAL'ом. Теперь ловится сам SQL, ушедший
    в базу из `_load_related`; после этого диверсия даёт красный.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AvitoAccount, Client, Conversation, Message, User
from app.services import conversations as convs
from app.services import stats as st
from tests.integration.conftest import requires_docker
from tests.integration.test_stats_pg import ensure_partitions

pytestmark = requires_docker


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
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """Виджету от Redis нужны только get/set — контейнер тут ни к чему."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def мир(pg_engine: AsyncEngine, sessionmaker) -> SimpleNamespace:
    await ensure_partitions(pg_engine)
    async with sessionmaker() as s:
        u1 = User(email="u1@perf.test", password_hash="x", full_name="Анна", role="manager")
        u2 = User(email="u2@perf.test", password_hash="x", full_name="Борис", role="manager")
        acc = AvitoAccount(
            title="LP",
            avito_user_id=700900,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="s",
        )
        s.add_all([u1, u2, acc])
        await s.commit()
        return SimpleNamespace(u1=u1, u2=u2, acc=acc)


def _клиент(n: int, имя: str, телефон: str | None = None) -> Client:
    return Client(channel="avito", external_id=f"perf-{n}", name=имя, phone=телефон)


def _диалог(мир, cl: Client, n: int, когда, **kw) -> Conversation:
    return Conversation(
        channel="avito",
        external_chat_id=f"perf-chat-{n}",
        account_id=мир.acc.id,
        client_id=cl.id,
        status="in_progress",
        last_message_at=когда,
        **kw,
    )


def _сообщение(conv, тело, когда, direction="in", sender="client", user=None, delivered=True):
    return Message(
        conversation_id=conv.id,
        direction=direction,
        sender_type=sender,
        sender_user_id=user,
        body=тело,
        attachments=[],
        delivery_status="delivered" if delivered else "failed",
        created_at=когда,
    )


# ------------------------------------------------------------- 1. индекс и поиск


async def test_миграция_поставила_pg_trgm_и_gin(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        ext = set((await conn.execute(text("SELECT extname FROM pg_extension"))).scalars())
        assert "pg_trgm" in ext
        rows = (
            await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE '%trgm'")
            )
        ).all()
    ddl = dict(rows)
    assert ddl == {
        "ix_clients_name_trgm": (
            "CREATE INDEX ix_clients_name_trgm ON public.clients "
            "USING gin (lower(name) gin_trgm_ops)"
        ),
        "ix_clients_phone_trgm": (
            "CREATE INDEX ix_clients_phone_trgm ON public.clients USING gin (phone gin_trgm_ops)"
        ),
        "ix_client_phone_candidates_phone_trgm": (
            "CREATE INDEX ix_client_phone_candidates_phone_trgm "
            "ON public.client_phone_candidates USING gin (phone gin_trgm_ops)"
        ),
    }


async def test_кириллица_без_регистра_и_планировщик_берёт_индекс(мир, sessionmaker) -> None:
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        свой = _клиент(1, "Иннокентий Уникальный", "+79151234567")
        чужой = _клиент(2, "Посторонний")
        s.add_all([свой, чужой])
        await s.flush()
        a = _диалог(мир, свой, 1, now)
        b = _диалог(мир, чужой, 2, now - timedelta(minutes=1))
        s.add_all([a, b])
        await s.commit()

        for q in ("иннокентий", "ИННОКЕНТИЙ", "Уникаль", "34567", "915 123-45-67"):
            items, total = await convs.list_conversations(s, мир.u1, tab="any", q=q)
            assert [i["id"] for i in items] == [str(a.id)], q
            assert total == 1, q

        # --- планировщик ------------------------------------------------
        #
        # ⚠ ПЛАН СВЕРЯЕМ НА ОДНОЙ ТАБЛИЦЕ, А НЕ НА ВСЁМ ПОИСКЕ (правка 07.09).
        # Здесь стоял EXPLAIN всего `_search_condition` — а он ищет ещё и по
        # тексту сообщений, и на двух строках планировщик honestly выбирал
        # ветку сообщений, оставляя индекс клиентов в стороне. Тест краснел не
        # на дефекте, а на объёме данных: в выкатке 07.09 он и упал.
        #
        # Утверждение, ради которого сторож заведён, — другое: ВЫРАЖЕНИЕ в
        # запросе совпадает с ВЫРАЖЕНИЕМ индекса. Оно проверяется двумя частями:
        # (1) прямой запрос по клиентам идёт через триграммный индекс, а тот же
        # запрос в прежней форме `ILIKE` — нет; (2) `_search_condition` эту
        # форму и порождает. Порознь каждая половина пуста: первая не знает,
        # что пишет код, вторая — что из этого выйдет у планировщика.
        await s.execute(text("ANALYZE clients"))
        await s.execute(text("SET LOCAL enable_seqscan = off"))

        async def _план(sql: str) -> str:
            return "\n".join((await s.execute(text("EXPLAIN " + sql))).scalars().all())

        по_выражению = await _план(
            "SELECT id FROM clients WHERE lower(name) LIKE lower('%иннокентий%')"
        )
        assert "ix_clients_name_trgm" in по_выражению, по_выражению

        прежний = await _план("SELECT id FROM clients WHERE name ILIKE '%иннокентий%'")
        assert "ix_clients_name_trgm" not in прежний, прежний

        # Вторая половина: код пишет именно `lower(name) LIKE lower(...)`.
        текст_условия = str(
            select(Conversation.id)
            .where(convs._search_condition(s, "иннокентий"))
            .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "lower(clients.name) LIKE lower(" in текст_условия, текст_условия


# ------------------------------------------------------- 2. LATERAL против окна


async def test_lateral_и_окно_дают_одни_строки_на_партициях(мир, sessionmaker, pg_engine) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessionmaker() as s:
        клиенты = [_клиент(n, f"Клиент {n}") for n in range(1, 6)]
        s.add_all(клиенты)
        await s.flush()
        a, b, c, d, e = (_диалог(мир, cl, n, now) for n, cl in enumerate(клиенты, start=1))
        s.add_all([a, b, c, d, e])
        await s.flush()
        # ⚠ Настоящая ничья по секунде — с ЯВНЫМИ id, чтобы «последнее» было
        # предрешено: при `id DESC` побеждает больший. Без этой пары разрыв по
        # `id` в LATERAL не стерёг никто (диверсия ревью 07.09).
        ничья_меньший = _сообщение(e, "Д: та же секунда, меньший id", now - timedelta(days=3))
        ничья_меньший.id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        ничья_больший = _сообщение(e, "Д: ПОСЛЕДНЕЕ по id", now - timedelta(days=3))
        ничья_больший.id = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
        # Перемешанный порядок вставки, сообщения в трёх разных месяцах,
        # одна и та же секунда у двух записей (ключ `id DESC`), заметка и
        # служебная запись Авито.
        s.add_all(
            [
                _сообщение(
                    a,
                    "А: ПОСЛЕДНЕЕ (прошлый месяц)",
                    now - timedelta(days=40),
                    "out",
                    "operator",
                    мир.u1.id,
                ),
                _сообщение(b, "Б: заметка новее всех", now, "note", "operator", мир.u1.id),
                _сообщение(a, "А: старое (позапрошлый месяц)", now - timedelta(days=70)),
                _сообщение(b, "Б: ПОСЛЕДНЕЕ", now - timedelta(minutes=1)),
                _сообщение(c, "В: слово клиента", now - timedelta(hours=2)),
                _сообщение(
                    c, "В: Клиент оформил заказ", now - timedelta(hours=1), "system", "avito"
                ),
                _сообщение(b, "Б: старое", now - timedelta(days=45)),
                ничья_больший,
                ничья_меньший,
            ]
        )
        await s.commit()

        ids = [a.id, b.id, c.id, d.id, e.id]
        lateral = {
            m.conversation_id: (m.id, m.body)
            for m in (
                await s.execute(
                    convs._last_message_stmt("postgresql", ids, convs.PREVIEW_MESSAGE_CONDITION)
                )
            ).scalars()
        }
        окно = {
            m.conversation_id: (m.id, m.body)
            for m in (
                await s.execute(
                    convs._last_message_stmt("sqlite", ids, convs.PREVIEW_MESSAGE_CONDITION)
                )
            ).scalars()
        }
        assert lateral == окно
        assert {k: v[1] for k, v in lateral.items()} == {
            a.id: "А: ПОСЛЕДНЕЕ (прошлый месяц)",
            b.id: "Б: ПОСЛЕДНЕЕ",
            c.id: "В: Клиент оформил заказ",
            e.id: "Д: ПОСЛЕДНЕЕ по id",
        }
        # И боевой путь на Postgres идёт именно LATERAL'ом. ⚠ Сверять строки
        # мало: окно дало бы те же самые. Ловим SQL, который `_load_related`
        # отправил в базу, — только он и отличает проводку от зашитого
        # диалекта (диверсия ревью 07.09).
        ушло: list[str] = []

        def _ловим(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            ушло.append(statement)

        sa.event.listen(pg_engine.sync_engine, "before_cursor_execute", _ловим)
        try:
            related = await convs._load_related(s, [a, b, c, d, e])
        finally:
            sa.event.remove(pg_engine.sync_engine, "before_cursor_execute", _ловим)
        assert {k: v.id for k, v in related["last_messages"].items()} == {
            k: v[0] for k, v in lateral.items()
        }
        превью_sql = [q for q in ушло if "FROM messages" in q]
        assert len(превью_sql) == 1, превью_sql
        assert "LATERAL" in превью_sql[0] and "row_number" not in превью_sql[0], превью_sql[0]
        план = "\n".join(
            (
                await s.execute(
                    text(
                        "EXPLAIN "
                        + str(
                            convs._last_message_stmt(
                                "postgresql", ids, convs.PREVIEW_MESSAGE_CONDITION
                            ).compile(
                                dialect=postgresql.dialect(),
                                compile_kwargs={"literal_binds": True},
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "Nested Loop" in план and "WindowAgg" not in план, план


# ----------------------------------------------------------------- 3. ветка frt


#: Ветка frt ДО правки — дословно. Эталон того, что виджет обязан считать.
_ЭТАЛОН_FRT_SQL = """
SELECT count(*)                                          AS answered_today,
       round((percentile_cont(0.5) WITHIN GROUP
           (ORDER BY r.frt_sec))::numeric)::int          AS frt_median_sec_today
FROM (
    SELECT EXTRACT(epoch FROM op.first_at - f.first_client_at) AS frt_sec
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    CROSS JOIN LATERAL (
        SELECT m.created_at AS first_at, m.sender_user_id
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.created_at >= f.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'operator'
          AND m.delivery_status <> 'failed'
        ORDER BY m.created_at
        LIMIT 1
    ) op
    WHERE c.last_message_at >= :day_start
      AND f.first_client_at >= :day_start AND f.first_client_at < :day_end
      AND op.sender_user_id = :user_id
) r
"""


async def test_виджет_считает_то_же_что_до_правки(мир, sessionmaker, redis) -> None:
    ds, de = st.msk_day_bounds(st.today_msk())
    u1, u2 = мир.u1.id, мир.u2.id
    async with sessionmaker() as s:
        клиенты = [_клиент(n, f"Клиент {n}") for n in range(1, 7)]
        s.add_all(клиенты)
        await s.flush()
        A, B, C, D, E, F = (
            _диалог(мир, cl, n, ds + timedelta(hours=9 + n), assignee_id=u1)
            for n, cl in enumerate(клиенты, start=1)
        )
        s.add_all([A, B, C, D, E, F])
        await s.flush()
        s.add_all(
            [
                # A — начат и отвечен сегодня Анной: FRT 60.
                _сообщение(A, "a", ds + timedelta(hours=9)),
                _сообщение(A, "ответ", ds + timedelta(hours=9, minutes=1), "out", "operator", u1),
                # B — начат ВЧЕРА, клиент написал ещё и сегодня, Анна ответила
                # сегодня: не «начат сегодня». ⚠ Второе слово клиента здесь —
                # не украшение: без него граница по дате внутри `f` дала бы NULL
                # и диалог отпал бы «случайно правильно»; с ним такая граница
                # объявила бы диалог сегодняшним с FRT в час (диверсия D12).
                _сообщение(B, "b вчера", ds - timedelta(hours=5)),
                _сообщение(B, "b сегодня", ds + timedelta(hours=8)),
                _сообщение(B, "ответ", ds + timedelta(hours=9), "out", "operator", u1),
                # C — первым ответил Борис (FRT 30 у него), Анна позже: не её.
                _сообщение(C, "c", ds + timedelta(hours=10)),
                _сообщение(C, "Борис", ds + timedelta(hours=10, seconds=30), "out", "operator", u2),
                _сообщение(C, "Анна", ds + timedelta(hours=10, minutes=5), "out", "operator", u1),
                # D — без ответа.
                _сообщение(D, "d", ds + timedelta(hours=11)),
                # E — заведён ВЧЕРА звонком без слов; клиент написал сегодня;
                # первый ответ Анны не доставлен, второй — да: FRT 120.
                _сообщение(E, "звонок", ds - timedelta(hours=3), "system", "avito"),
                _сообщение(E, "e", ds + timedelta(hours=12)),
                _сообщение(
                    E,
                    "сорвалось",
                    ds + timedelta(hours=12, minutes=1),
                    "out",
                    "operator",
                    u1,
                    delivered=False,
                ),
                _сообщение(E, "дошло", ds + timedelta(hours=12, minutes=2), "out", "operator", u1),
                # F — бот ответил раньше человека; FRT оператора — по Анне: 60.
                _сообщение(F, "f", ds + timedelta(hours=13)),
                _сообщение(F, "бот", ds + timedelta(hours=13, seconds=5), "out", "bot"),
                _сообщение(F, "Анна", ds + timedelta(hours=13, minutes=1), "out", "operator", u1),
            ]
        )
        await s.commit()

        for user, answered, median in ((u1, 3, 60), (u2, 1, 30)):
            widget = await st.my_today(s, redis, user)
            assert (widget["answered_today"], widget["frt_median_sec_today"]) == (
                answered,
                median,
            ), user
            эталон = await st._row(
                s, _ЭТАЛОН_FRT_SQL, {"day_start": ds, "day_end": de, "user_id": user}
            )
            assert (int(эталон["answered_today"]), st._int(эталон["frt_median_sec_today"])) == (
                answered,
                median,
            ), user
