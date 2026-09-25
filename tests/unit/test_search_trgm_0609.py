"""Поиск по мере набора: индекс по триграммам, итог окном — выдача прежняя.

ЗАМЕР БОЯ 06.09. Каждое нажатие клавиши в поле поиска стоило два полных
прохода по `clients` (62 264 строки): `count(*)` для «Найдено: N» — 43,7–102 мс
и страница — 33–77 мс. 9,7 % всех вызовов списка, около 5 000 запросов в день.

ДВЕ ПРАВКИ, И У КАЖДОЙ СВОЙ СПОСОБ СЛОМАТЬСЯ МОЛЧА.

1. GIN по триграммам (миграция 0068) на `lower(name)` и `phone`. Индекс по
   выражению планировщик берёт только для ТОГО ЖЕ выражения: верни в
   `_search_condition` прежний `name ILIKE …` — выдача не изменится, тесты
   поведения зазеленеют, а индекс останется мёртвым. Поэтому форма условия
   проверяется буквально, компиляцией диалектом Postgres.

2. Итог при поиске — `count(*) OVER ()` в той же выборке, а не второй запрос.
   Число обязано остаться ТОЧНЫМ: фронт рисует по нему «Найдено: N» и решает,
   есть ли ещё страницы (`loaded < page.total`). Здесь проверяется и число, и
   то, что отдельного `count(*)` при поиске больше нет, — включая путь с
   закреплёнными и пустую страницу за концом выборки.

⚠ SQLite сворачивает регистр только у латиницы, поэтому проверка «без учёта
регистра» здесь на латинском имени; кириллица и настоящий индекс — в
`tests/integration/test_perf_0609_pg.py`. И ещё одно про SQLite: его `LIKE`
сам не различает регистр латиницы (`case_sensitive_like` по умолчанию OFF),
то есть условие с `lower()` только слева здесь зеленело бы, а на Postgres —
нет. Поэтому в поведенческом тесте прагма включается: регистр обязан
сворачивать наш `lower()` с обеих сторон, а не база из вежливости.

ДИВЕРСИИ (каждая дала красный, восстановлено байт в байт):
  - `_search_condition`: `sa.func.lower(Client.name).like(...)` →
    `Client.name.ilike(like)` — упал `test_условие_поиска_совпадает_с_индексом`.
  - модель: `postgresql_ops` снят у `ix_clients_name_trgm` — упал
    `test_на_postgres_индексы_gin_по_триграммам`; имя индекса переименовано —
    упал `test_имена_индексов_в_модели_и_миграции_совпадают`.
  - `list_conversations`: возвращён отдельный `count(*)` при поиске — упал
    `test_итог_при_поиске_без_отдельного_count`.

ДИВЕРСИЯ РЕВЬЮ 07.09: `.like(sa.func.lower(like))` → `.like(like)` (lower только
слева). Форма-тест упал, а `test_имя_находится_без_учёта_регистра` ОСТАЛСЯ
ЗЕЛЁНЫМ — SQLite LIKE сворачивал латиницу сам. Закрыто прагмой
`case_sensitive_like = ON` в этом тесте; после неё диверсия даёт красный
на `INNOKENTIY` и `Innok`.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.models import Client, ClientPhoneCandidate, Conversation
from app.services import conversations as convs
from app.services import pins

T0 = datetime(2026, 9, 6, 9, 0, 0, tzinfo=UTC)
МИГРАЦИЯ = Path(__file__).resolve().parents[2] / "app/db/migrations/versions/0068_trgm_search.py"


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


class _ПсевдоPG:
    """Соединение, о котором известно одно: диалект Postgres."""

    class _Bind:
        class dialect:  # noqa: N801 — подражаем форме SQLAlchemy
            name = "postgresql"

    def get_bind(self):  # noqa: ANN201
        return self._Bind()


def _миграция():
    spec = importlib.util.spec_from_file_location("migration_0068", МИГРАЦИЯ)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _триграммные_индексы() -> dict[str, sa.Index]:
    out: dict[str, sa.Index] = {}
    for table in (Client.__table__, ClientPhoneCandidate.__table__):
        for index in table.indexes:
            if index.name and "trgm" in index.name:
                out[index.name] = index
    return out


# ------------------------------------------------------------- проводка индекса


def test_имена_индексов_в_модели_и_миграции_совпадают() -> None:
    """Индекс, названный в модели иначе, при сравнении схем выглядел бы
    одновременно «лишним» и «недостающим»."""
    в_миграции = dict(_миграция().TRGM_INDEXES)
    в_модели = {name: idx.table.name for name, idx in _триграммные_индексы().items()}
    assert в_модели == в_миграции


def test_на_postgres_индексы_gin_по_триграммам() -> None:
    """⚠ Без `postgresql_ops` GIN по тексту не создастся вовсе, а без
    `USING gin` получится B-tree, который на `LIKE '%…%'` не работает."""
    индексы = _триграммные_индексы()
    assert set(индексы) == {
        "ix_clients_name_trgm",
        "ix_clients_phone_trgm",
        "ix_client_phone_candidates_phone_trgm",
    }
    ddl = {
        name: str(CreateIndex(idx).compile(dialect=postgresql.dialect())).strip()
        for name, idx in индексы.items()
    }
    for name, sql in ddl.items():
        assert "USING gin" in sql, f"{name}: не GIN — {sql}"
        assert "gin_trgm_ops" in sql, f"{name}: не по триграммам — {sql}"
    # Выражение — ровно то, что пишет условие поиска.
    assert ddl["ix_clients_name_trgm"].endswith("USING gin (lower(name) gin_trgm_ops)")
    assert ddl["ix_clients_phone_trgm"].endswith("USING gin (phone gin_trgm_ops)")


def test_условие_поиска_совпадает_с_индексом() -> None:
    """⚠ `lower(clients.name) LIKE lower(…)`, а не `ILIKE`: индекс по выражению
    берётся только для того же выражения."""

    def _sql(q: str) -> str:
        compiled = convs._search_condition(_ПсевдоPG(), q).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
        # Диалект экранирует `%` под pyformat (`%%`) — читаем как SQL.
        return str(compiled).replace("%%", "%")

    sql = _sql("Иван")
    assert "lower(clients.name) LIKE lower('%Иван%')" in sql, sql
    assert "ILIKE" not in sql, "вернулся ILIKE — индекс 0068 для него мёртв"

    sql = _sql("+7 (915) 123-45-67")
    assert "clients.phone LIKE '%9151234567%'" in sql, sql
    assert "client_phone_candidates.phone LIKE '%9151234567%'" in sql, sql


# ------------------------------------------------------------------- поведение


@pytest.fixture
async def набор(db_sessionmaker, make_avito_account, users_by_role):
    account = await make_avito_account()
    manager = users_by_role["manager"]
    async with db_sessionmaker() as s:
        сделано: dict[str, uuid.UUID] = {}

        async def диалог(метка, *, имя, телефон=None, сдвиг=0, мой=False):
            cl = Client(
                id=uuid.uuid4(),
                channel="avito",
                external_id=f"ext-{метка}",
                name=имя,
                phone=телефон,
            )
            s.add(cl)
            await s.flush()
            conv = Conversation(
                id=uuid.uuid4(),
                channel="avito",
                external_chat_id=f"chat-{метка}",
                account_id=account.id,
                client_id=cl.id,
                assignee_id=manager.id if мой else None,
                status="in_progress",
                unread_count=0,
                tags=[],
                declined_by=[],
                bot_active=False,
                bot_vars={},
                last_message_at=T0 + timedelta(minutes=сдвиг),
            )
            s.add(conv)
            await s.flush()
            сделано[метка] = conv.id

        await диалог("латиница", имя="Innokentiy Unique", сдвиг=50)
        await диалог("телефон", имя="Кто-то", телефон="+79151234567", сдвиг=40)
        await диалог("ремонт1", имя="Ремонт Первый", сдвиг=30, мой=True)
        await диалог("ремонт2", имя="Ремонт Второй", сдвиг=20)
        await диалог("ремонт3", имя="Ремонт Третий", сдвиг=10)
        await диалог("посторонний", имя="Посторонний", сдвиг=0)
        await s.commit()
    return SimpleNamespace(**сделано, manager=manager)


async def _поиск(client, tokens, q: str, **params) -> dict:
    query = "&".join([f"tab=any&q={q}"] + [f"{k}={v}" for k, v in params.items()])
    r = await client.get(f"/api/v1/conversations?{query}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("q", ["innokentiy", "INNOKENTIY", "Innok", "unique"])
async def test_имя_находится_без_учёта_регистра(client, tokens, набор, engine, q):
    # ⚠ Без прагмы SQLite сворачивает регистр латиницы сам, и тест не отличил
    # бы `lower()` с обеих сторон от `lower()` только слева (см. докстринг).
    # Движок — на тест, соединение одно (StaticPool): прагма никого не заденет.
    async with engine.connect() as conn:
        await conn.execute(sa.text("PRAGMA case_sensitive_like = ON"))
        await conn.commit()
    body = await _поиск(client, tokens, q)
    assert {i["id"] for i in body["items"]} == {str(набор.латиница)}
    assert body["page"]["total"] == 1


@pytest.mark.parametrize("q", ["34567", "915 123-45-67", "%2B7 (915) 123-45-67", "89151234567"])
async def test_хвост_телефона_находится_как_раньше(client, tokens, набор, q):
    body = await _поиск(client, tokens, q)
    assert {i["id"] for i in body["items"]} == {str(набор.телефон)}


class _Счётчик:
    """Все SQL-выражения, ушедшие в базу за время теста."""

    def __init__(self, engine) -> None:
        self.statements: list[str] = []
        sa.event.listen(engine.sync_engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(statement)

    def сброс(self) -> None:
        self.statements.clear()

    @property
    def отдельных_count(self) -> int:
        return sum(1 for s in self.statements if "count(*) AS count_1" in s)

    @property
    def окон(self) -> int:
        return sum(1 for s in self.statements if "count(*) OVER ()" in s)


async def test_итог_при_поиске_без_отдельного_count(client, tokens, набор, engine):
    """Три совпадения, страница по два: число точное, а `count(*)` — в окне."""
    счёт = _Счётчик(engine)
    ремонты = {str(набор.ремонт1), str(набор.ремонт2), str(набор.ремонт3)}

    body = await _поиск(client, tokens, "Ремонт", limit=2)
    assert len(body["items"]) == 2
    assert {i["id"] for i in body["items"]} < ремонты
    assert body["page"]["total"] == 3
    assert счёт.отдельных_count == 0, "при поиске вернулся отдельный count(*)"
    assert счёт.окон == 1

    # Вторая страница: тот же итог, ровно один оставшийся.
    счёт.сброс()
    body = await _поиск(client, tokens, "Ремонт", limit=2, offset=2)
    assert len(body["items"]) == 1
    assert body["page"]["total"] == 3
    assert счёт.отдельных_count == 0

    # Пустая выборка при offset=0 — ноль без запроса.
    счёт.сброс()
    body = await _поиск(client, tokens, "Зазеркалье")
    assert body["items"] == [] and body["page"]["total"] == 0
    assert счёт.отдельных_count == 0

    # За концом выборки окну не на чем ехать — честный count(*), число прежнее.
    счёт.сброс()
    body = await _поиск(client, tokens, "Ремонт", limit=2, offset=10)
    assert body["items"] == [] and body["page"]["total"] == 3
    assert счёт.отдельных_count == 1


async def test_итог_при_поиске_с_закреплёнными(client, tokens, набор, engine, db_sessionmaker):
    """Голова закреплённых + хвост окном — и случай, когда голова заняла всю страницу."""
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, набор.ремонт1)
        await pins.pin(s, conv, набор.manager)
    счёт = _Счётчик(engine)

    body = await _поиск(client, tokens, "Ремонт", limit=2)
    assert body["items"][0]["id"] == str(набор.ремонт1)
    assert body["items"][0]["pinned"] is True
    assert len(body["items"]) == 2
    assert body["page"]["total"] == 3
    assert счёт.отдельных_count == 0 and счёт.окон == 1

    # Страница из одной строки: её целиком заняла голова, хвост не спрашивали,
    # итог — честным count(*), и он тот же.
    счёт.сброс()
    body = await _поиск(client, tokens, "Ремонт", limit=1)
    assert [i["id"] for i in body["items"]] == [str(набор.ремонт1)]
    assert body["page"]["total"] == 3
    assert счёт.отдельных_count == 1

    # Без поиска — прежний путь: отдельный count(*), окна нет.
    счёт.сброс()
    r = await client.get("/api/v1/conversations?tab=any&limit=2", headers=auth(tokens))
    assert r.status_code == 200
    assert r.json()["page"]["total"] == 6
    assert счёт.отдельных_count == 1 and счёт.окон == 0
