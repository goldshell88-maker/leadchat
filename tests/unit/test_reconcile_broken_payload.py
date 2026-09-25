"""Сверка не должна умирать от одной неразбираемой записи.

ЧТО БЫЛО. `_LiveAdapter` звал `AvitoAdapter.parse_chat` и
`normalize_history_message` НАПРЯМУЮ, без обработки. Разбор бросает
WebhookParseError на любой непривычной форме — например на служебном сообщении
без автора: какой у него `author_id`, проект не проверял (docs/30, «Чего мы не
знаем»), это признанная догадка. Одна такая запись уносила ВЕСЬ прогон сверки.

ЧЕМ ЭТО ГРОЗИЛО ЧЕЛОВЕКУ. Сверка — единственная страховка от НЕДОШЕДШИХ
вебхуков: пока она лежит, пропавшее сообщение клиента не появится у оператора
вовсе. И лежит она не до перезапуска, а навсегда: offset каждый час начинается
с нуля, а чат остаётся в выборке unread_only — следующий прогон спотыкается о
ту же запись. Побочно молчит и `ping_canary` в конце прогона: ночью вебхуков
нет, канарейку кормит только сверка, и мёртвый канал перестаёт быть заметен.

Штатный `AvitoAdapter.fetch_chats/fetch_history` (adapter.py) и первичная
загрузка истории (services/avito_accounts.py) давно пропускают кривое с
warning — сверка была единственным местом, где эта конвенция нарушена.

ПРОВЕРКА ЛОМАНИЕМ: уберите в `app/workers/reconciliation.py` перехват
WebhookParseError — краснеют все четыре теста этого файла.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.models import AvitoAccount, Message
from app.workers import reconciliation as rec

ACCOUNT_UID = 772400
CLIENT_UID = 999401

# Чат непривычной формы: у Авито id чата — строка, здесь число. Форма взята
# нарочно безобидная: дело не в конкретном поле, а в том, что разбор чужого
# API имеет право не узнать запись, и это не повод бросать остальные чаты.
BROKEN_CHAT: dict[str, Any] = {"id": 12345}

# СЛУЖЕБНОЕ СООБЩЕНИЕ БЕЗ АВТОРА — ровно тот случай из docs/30: формы таких
# сообщений мы не видели, гипотеза «author_id = 0 или id платформы» ничем не
# подтверждена. `normalize_history_message` на нём падает.
BROKEN_MESSAGE: dict[str, Any] = {
    "id": "m-служебное",
    "created": 1_754_000_000,
    "type": "system",
    "content": {},
}


def _account() -> AvitoAccount:
    return AvitoAccount(
        id=uuid.uuid4(),
        title="LP-Сверка",
        avito_user_id=ACCOUNT_UID,
        access_token_enc=b"enc",
        refresh_token_enc=b"enc",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
        webhook_secret="whsec",
    )


def _raw_chat(chat_id: str = "u2i-хороший") -> dict[str, Any]:
    return {
        "id": chat_id,
        "users": [
            {"id": ACCOUNT_UID, "name": "Мы"},
            {"id": CLIENT_UID, "name": "Клиент"},
        ],
    }


def _raw_message(msg_id: str, moment: datetime) -> dict[str, Any]:
    return {
        "id": msg_id,
        "author_id": CLIENT_UID,
        "created": int(moment.timestamp()),
        "type": "text",
        "content": {"text": f"текст {msg_id}"},
    }


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    adapter: rec._LiveAdapter,
    *,
    chats: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    """Фейк транспорта: страницы сырых чатов и сообщений по смещению.

    Подменяются методы клиента, а не `_call`, — маршрут вызова (бюджет
    ограничителя, авто-рефреш) остаётся боевым.
    """
    chat_pages = list(chats or [])
    message_pages = list(messages or [])

    async def _get_chats(
        _token: str, _uid: int, *, offset: int = 0, limit: int = 100, **_kw: Any
    ) -> list[dict[str, Any]]:
        return chat_pages[offset : offset + limit]

    async def _get_messages(
        _token: str, _uid: int, _chat_id: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return message_pages[offset : offset + limit]

    monkeypatch.setattr(adapter._client, "get_chats", _get_chats)
    monkeypatch.setattr(adapter._client, "get_chat_messages", _get_messages)
    # Расшифровка токена здесь не при чём — важен разбор ответа.
    monkeypatch.setattr("app.services.crypto.decrypt_token", lambda _b: "token")


async def test_broken_chat_is_skipped_and_the_page_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Кривой чат пропускается, следующий за ним разбирается как обычно."""
    account = _account()
    adapter = rec._LiveAdapter()
    _transport(monkeypatch, adapter, chats=[BROKEN_CHAT, _raw_chat()])

    chats = [chat async for chat in adapter.fetch_chats(account)]

    assert [chat.external_chat_id for chat in chats] == ["u2i-хороший"]


async def test_the_skipped_chat_leaves_a_trace_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Молчаливый пропуск хуже падения: пропущенное обязано быть в журнале.

    Иначе диалог не появляется у оператора, а в логах — ни строчки о том,
    почему.
    """
    account = _account()
    adapter = rec._LiveAdapter()
    _transport(monkeypatch, adapter, chats=[BROKEN_CHAT])

    with structlog.testing.capture_logs() as logs:
        assert [chat async for chat in adapter.fetch_chats(account)] == []

    skipped = [entry for entry in logs if entry["event"] == "reconcile.chat_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"


async def test_broken_message_does_not_take_the_rest_of_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Служебное без автора пропускается, соседние сообщения доезжают.

    Незнакомый чат сверка теперь берёт с самого начала — чем длиннее история,
    тем вернее в ней попадётся форма, которой мы не видели.
    """
    account = _account()
    adapter = rec._LiveAdapter()
    now = datetime.now(UTC)
    _transport(
        monkeypatch,
        adapter,
        messages=[
            _raw_message("до", now - timedelta(minutes=2)),
            BROKEN_MESSAGE,
            _raw_message("после", now - timedelta(minutes=1)),
        ],
    )
    chat = type("Chat", (), {"external_chat_id": "u2i-1"})()
    for field in ("item_title", "item_url", "item_price", "client_name"):
        setattr(chat, field, None)

    events = [event async for event in adapter.fetch_history(account, chat)]

    assert [event.external_message_id for event in events] == ["до", "после"]


async def test_the_run_still_catches_up_the_missed_message(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker, redis, make_avito_account
) -> None:
    """Главное: прогон доходит до конца и догоняет пропавшее сообщение.

    В выборке — кривой чат, а за ним настоящий, и в его истории кривое
    служебное. Раньше на первой же такой записи прогон умирал, и сообщение
    клиента, потерянное вебхуком, не появлялось у оператора никогда.
    """
    account = await make_avito_account(ACCOUNT_UID)
    adapter = rec._LiveAdapter(db_sessionmaker, redis)
    connected = rec._aware(account.created_at)
    _transport(
        monkeypatch,
        adapter,
        chats=[BROKEN_CHAT, _raw_chat()],
        messages=[BROKEN_MESSAGE, _raw_message("пропавшее", connected + timedelta(minutes=1))],
    )
    monkeypatch.setattr(rec, "get_adapter", lambda _ctx: adapter)

    result = await rec.reconcile_account(
        {"db_session_factory": db_sessionmaker, "redis": redis}, account.id
    )

    assert result["chats_checked"] == 1, "кривой чат пропущен, настоящий — разобран"
    assert result["messages_recovered"] == 1
    async with db_sessionmaker() as db:
        rows = await db.execute(sa.select(Message.external_message_id))
        assert list(rows.scalars().all()) == ["пропавшее"]
