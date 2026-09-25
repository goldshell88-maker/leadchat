"""Лента: «есть ли ещё» — через `LIMIT + 1`, а не через два EXISTS.

ЗАМЕР БОЯ 06.09. Страница ленты делала два EXISTS ради двух флагов
(`has_more_before` / `has_more_after`), и каждый планировался по 28 партициям
`messages` — 1,4–16 мс на штуку. Это на первом открытии диалога, то есть на
каждом клике по строке списка.

ЧТО ПРОВЕРЯЕТСЯ. Флаги ПРЕЖНИЕ на трёх краях — ровно `limit` сообщений,
`limit + 1`, ноль — и на страницах по курсору в обе стороны. Плюс проводка:
первое открытие — один запрос к `messages`, без EXISTS; страница по курсору —
один EXISTS (в противоположную листанию сторону), а не два.

ДИВЕРСИИ (каждая дала красный, восстановлено байт в байт):
  - `has_more_before = len(rows) > limit` → `>= limit` в ветке первого
    открытия — упал `test_ровно_limit_сообщений_дальше_нет`;
  - `.limit(ещё)` → `.limit(limit)` в той же ветке — упал
    `test_limit_плюс_один_дальше_есть`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation, Message

T0 = datetime(2026, 9, 6, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
def лента(db_sessionmaker, make_avito_account):
    """Диалог с `n` входящими: «сообщение 1» … «сообщение n», по минуте между."""

    async def _make(n: int) -> SimpleNamespace:
        account = await make_avito_account(555000200 + n)
        async with db_sessionmaker() as s:
            cl = Client(channel="avito", external_id=f"feed-{n}", name="Клиент Ленты")
            s.add(cl)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-feed-{n}",
                account_id=account.id,
                client_id=cl.id,
                status="new",
                unread_count=n,
                last_message_at=T0 + timedelta(minutes=n),
            )
            s.add(conv)
            await s.flush()
            for k in range(1, n + 1):
                s.add(
                    Message(
                        conversation_id=conv.id,
                        external_message_id=f"am-{n}-{k}",
                        direction="in",
                        sender_type="client",
                        body=f"сообщение {k}",
                        attachments=[],
                        delivery_status="delivered",
                        created_at=T0 + timedelta(minutes=k),
                    )
                )
            await s.commit()
            return SimpleNamespace(id=conv.id, url=f"/api/v1/conversations/{conv.id}/messages")

    return _make


def _тела(page):
    return [m["body"] for m in page["items"]]


async def _страница(client, tokens, url, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    r = await client.get(f"{url}?{query}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    return r.json()


class _Счётчик:
    def __init__(self, engine) -> None:
        self.statements: list[str] = []
        sa.event.listen(engine.sync_engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(statement)

    def сброс(self) -> None:
        self.statements.clear()

    @property
    def к_сообщениям(self) -> list[str]:
        return [s for s in self.statements if "FROM messages" in s]

    @property
    def exists(self) -> int:
        return sum(1 for s in self.statements if "EXISTS" in s)


async def test_ровно_limit_сообщений_дальше_нет(client, tokens, лента):
    conv = await лента(3)
    page = await _страница(client, tokens, conv.url, limit=3)
    assert _тела(page) == ["сообщение 1", "сообщение 2", "сообщение 3"]
    assert page["page"]["has_more_before"] is False
    assert page["page"]["has_more_after"] is False
    assert page["page"]["prev_cursor"] and page["page"]["next_cursor"]


async def test_limit_плюс_один_дальше_есть(client, tokens, лента):
    conv = await лента(4)
    page = await _страница(client, tokens, conv.url, limit=3)
    # Отдаём ровно `limit`, самые новые; лишняя строка — только признак.
    assert _тела(page) == ["сообщение 2", "сообщение 3", "сообщение 4"]
    assert page["page"]["has_more_before"] is True
    assert page["page"]["has_more_after"] is False


async def test_пустая_лента(client, tokens, лента):
    conv = await лента(0)
    page = await _страница(client, tokens, conv.url, limit=3)
    assert page["items"] == []
    assert page["page"] == {
        "prev_cursor": None,
        "next_cursor": None,
        "has_more_before": False,
        "has_more_after": False,
    }


async def test_страницы_по_курсору_в_обе_стороны(client, tokens, лента):
    conv = await лента(5)
    p1 = await _страница(client, tokens, conv.url, limit=2)
    assert _тела(p1) == ["сообщение 4", "сообщение 5"]

    # Вверх: ровно limit+1 строк за курсором → «есть ещё» истинно.
    p2 = await _страница(client, tokens, conv.url, limit=2, before=p1["page"]["prev_cursor"])
    assert _тела(p2) == ["сообщение 2", "сообщение 3"]
    assert p2["page"]["has_more_before"] is True
    assert p2["page"]["has_more_after"] is True

    # Вверх до конца: одна строка при limit=2 → дальше нет.
    p3 = await _страница(client, tokens, conv.url, limit=2, before=p2["page"]["prev_cursor"])
    assert _тела(p3) == ["сообщение 1"]
    assert p3["page"]["has_more_before"] is False
    assert p3["page"]["has_more_after"] is True

    # Вниз от p2: ровно limit строк → дальше нет.
    p4 = await _страница(client, tokens, conv.url, limit=2, after=p2["page"]["next_cursor"])
    assert _тела(p4) == ["сообщение 4", "сообщение 5"]
    assert p4["page"]["has_more_after"] is False
    assert p4["page"]["has_more_before"] is True

    # Вниз от p2 по одной: limit+1 строк → дальше есть.
    p5 = await _страница(client, tokens, conv.url, limit=1, after=p2["page"]["next_cursor"])
    assert _тела(p5) == ["сообщение 4"]
    assert p5["page"]["has_more_after"] is True

    # Курсор в самом конце: пустая страница, флаги погашены.
    p6 = await _страница(client, tokens, conv.url, limit=2, after=p1["page"]["next_cursor"])
    assert p6["items"] == []
    assert p6["page"]["has_more_after"] is False
    assert p6["page"]["has_more_before"] is False


async def test_первое_открытие_одним_запросом_без_exists(client, tokens, лента, engine):
    """⚠ Проводка: EXISTS легко вернуть «для надёжности» — и снова 28 партиций."""
    conv = await лента(4)
    счёт = _Счётчик(engine)

    p1 = await _страница(client, tokens, conv.url, limit=3)
    assert len(счёт.к_сообщениям) == 1, счёт.к_сообщениям
    assert счёт.exists == 0

    # Страница по курсору: сама выборка + ОДИН EXISTS в противоположную сторону.
    счёт.сброс()
    await _страница(client, tokens, conv.url, limit=3, before=p1["page"]["prev_cursor"])
    assert len(счёт.к_сообщениям) == 2, счёт.к_сообщениям
    assert счёт.exists == 1
