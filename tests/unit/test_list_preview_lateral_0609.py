"""Превью строки списка: на Postgres — LATERAL, на SQLite — окно; строки те же.

ЗАМЕР БОЯ. Аудит 06.09: превью последнего сообщения для 50 строк списка —
окно `row_number()` по 28 партициям, планирование 11–62 мс + исполнение
8–24 мс, 1 300 буферов; та же выдача через `CROSS JOIN LATERAL (… ORDER BY
created_at DESC LIMIT 1)` — 4,7 + 1,2 мс, −85 % буферов. Повторный замер
07.09 в свежем соединении: буферы 3 067 → 512, исполнение 8,7 → 2,9 мс, при
одинаковом дайджесте 50 строк.

⚠ ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, А ЧТО НЕТ. LATERAL в SQLite не существует, поэтому
поведение (какое сообщение становится превью) проверяется на запасной ветке с
окном — она обязана давать ровно то же, что боевая. Форма Postgres проверяется
компиляцией: LATERAL, порядок `created_at DESC, id DESC`, `LIMIT 1` и
отсутствие окна. Равенство двух форм на настоящем Postgres с партициями — в
`tests/integration/test_perf_0609_pg.py`.

ДИВЕРСИИ (каждая дала красный, восстановлено байт в байт):
  - в ветке Postgres `Message.created_at.desc()` → `.asc()` — упал
    `test_форма_postgres_lateral_с_нужным_порядком`;
  - в запасной ветке `Message.created_at.desc()` → `.asc()` — упали
    `test_превью_это_последнее_при_перемешанной_вставке` и
    `test_превью_различает_диалоги`.

ДИВЕРСИЯ РЕВЬЮ 07.09: в `_load_related` диалект зашит —
`_last_message_stmt("sqlite", …)` вместо `_dialect(db)` — и ВСЕ ПЯТЬ ТЕСТОВ
ОСТАЛИСЬ ЗЕЛЁНЫМИ (как и PG-тест на равенство строк): обе формы дают одни
строки, по ним не видно, окном пошёл бой или LATERAL'ом. Закрыто
`test_проводка_load_related_берёт_диалект_соединения`: ловится сам аргумент.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.models import AvitoAccount, Client, Conversation, Message
from app.services import conversations as convs

T0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


async def _мир(db):
    acc = AvitoAccount(
        id=uuid.uuid4(),
        title="Канал",
        avito_user_id=1,
        status="active",
        webhook_secret="s",
        access_token_enc=b"enc",
        refresh_token_enc=b"enc",
        token_expires_at=T0 + timedelta(days=1),
    )
    cl = Client(id=uuid.uuid4(), name="Клиент", external_id="u-1")
    db.add_all([acc, cl])
    await db.flush()
    return acc, cl


def _диалог(acc, cl, n):
    return Conversation(
        id=uuid.uuid4(),
        account_id=acc.id,
        client_id=cl.id,
        external_chat_id=f"chat-{n}",
        status="new",
        last_message_at=T0 - timedelta(minutes=n),
    )


def _сообщение(conv, тело, когда, direction="in", sender="client"):
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        direction=direction,
        sender_type=sender,
        body=тело,
        attachments=[],
        delivery_status="delivered",
        created_at=когда,
    )


# ------------------------------------------------------------------ поведение


@pytest.mark.asyncio
async def test_превью_это_последнее_при_перемешанной_вставке(db):
    """Порядок вставки — не порядок времени: последнее по `created_at`, не по id."""
    acc, cl = await _мир(db)
    a = _диалог(acc, cl, 1)
    db.add(a)
    await db.flush()
    # Сначала вставляем самое новое, потом старые — как приезжает догон истории.
    for тело, минут, direction, sender in (
        ("ответ оператора — ПОСЛЕДНЕЕ", 1, "out", "operator"),
        ("первое слово клиента", 60, "in", "client"),
        ("второе слово клиента", 30, "in", "client"),
    ):
        db.add(_сообщение(a, тело, T0 - timedelta(minutes=минут), direction, sender))
        await db.flush()

    related = await convs._load_related(db, [a])
    превью = related["last_messages"][a.id]
    assert превью.body == "ответ оператора — ПОСЛЕДНЕЕ"
    assert превью.direction == "out"


@pytest.mark.asyncio
async def test_превью_различает_диалоги(db):
    """У каждого диалога — своё последнее, а не общее самое новое на странице."""
    acc, cl = await _мир(db)
    a, b, c = _диалог(acc, cl, 1), _диалог(acc, cl, 2), _диалог(acc, cl, 3)
    db.add_all([a, b, c])
    await db.flush()
    db.add_all(
        [
            _сообщение(b, "Б: новее всех на странице", T0 - timedelta(minutes=1)),
            _сообщение(a, "А: старое", T0 - timedelta(hours=5)),
            _сообщение(a, "А: последнее", T0 - timedelta(hours=2)),
            _сообщение(b, "Б: старое", T0 - timedelta(hours=1)),
        ]
    )
    await db.flush()

    related = await convs._load_related(db, [a, b, c])
    assert related["last_messages"][a.id].body == "А: последнее"
    assert related["last_messages"][b.id].body == "Б: новее всех на странице"
    assert c.id not in related["last_messages"]


@pytest.mark.asyncio
async def test_допуск_в_превью_прежний(db):
    """Заметка в превью не идёт; служебная запись Авито — идёт (дефект 12)."""
    acc, cl = await _мир(db)
    a = _диалог(acc, cl, 1)
    db.add(a)
    await db.flush()
    db.add_all(
        [
            _сообщение(a, "слово клиента", T0 - timedelta(minutes=30)),
            _сообщение(a, "Клиент оформил заказ", T0 - timedelta(minutes=10), "system", "avito"),
            _сообщение(a, "заметка", T0 - timedelta(minutes=1), "note", "operator"),
        ]
    )
    await db.flush()

    related = await convs._load_related(db, [a])
    assert related["last_messages"][a.id].body == "Клиент оформил заказ"


@pytest.mark.asyncio
async def test_проводка_load_related_берёт_диалект_соединения(db, monkeypatch):
    """⚠ Проводка: диалект приходит от соединения, а не зашит. Бой — Postgres,
    тесты — SQLite, и только этот аргумент решает, какая форма уйдёт в базу."""
    acc, cl = await _мир(db)
    a = _диалог(acc, cl, 1)
    db.add(a)
    await db.flush()
    db.add(_сообщение(a, "слово", T0))
    await db.flush()

    увидено: list[str] = []
    подлинная = convs._last_message_stmt

    def _шпион(dialect, conv_ids, допуск):  # noqa: ANN001, ANN202
        увидено.append(dialect)
        # SQLite LATERAL не исполнит — подменяем только форму, не проводку.
        return подлинная("sqlite", conv_ids, допуск)

    monkeypatch.setattr(convs, "_dialect", lambda db_: "postgresql")
    monkeypatch.setattr(convs, "_last_message_stmt", _шпион)
    related = await convs._load_related(db, [a])
    assert увидено == ["postgresql"], "диалект в _load_related не от соединения"
    assert related["last_messages"][a.id].body == "слово"


# --------------------------------------------------------------------- форма


def test_форма_postgres_lateral_с_нужным_порядком() -> None:
    """⚠ Форма и есть предмет правки: окно выдало бы те же строки в 6 раз дороже."""
    sql = str(
        convs._last_message_stmt(
            "postgresql", [uuid.uuid4()], convs.PREVIEW_MESSAGE_CONDITION
        ).compile(dialect=postgresql.dialect())
    )
    assert "LATERAL" in sql, sql
    assert "ORDER BY messages.created_at DESC, messages.id DESC" in sql, sql
    assert "LIMIT" in sql, sql
    assert "row_number" not in sql, "вернулось окно по партициям"
    assert "messages.conversation_id = page.id" in sql, "LATERAL не привязан к строке страницы"
    assert "messages.direction IN" in sql, "допуск в превью потерян"


def test_запасная_ветка_sqlite_остаётся_окном() -> None:
    sql = str(
        convs._last_message_stmt("sqlite", [uuid.uuid4()], convs.PREVIEW_MESSAGE_CONDITION).compile(
            dialect=sqlite.dialect()
        )
    )
    assert "row_number() OVER (PARTITION BY messages.conversation_id" in sql, sql
    assert "ORDER BY messages.created_at DESC, messages.id DESC" in sql, sql
    assert "LATERAL" not in sql
