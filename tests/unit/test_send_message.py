"""Отправка, заметки и повтор — 01 §6.2–§6.4, 08 §8.3 (07 §1.1).

Покрыто: идемпотентность (реплей, mismatch, дыра «SET прошёл, commit нет»),
автоназначение new -> in_progress, mute_bot (02 §2.6), RBAC (head/observer),
валидации (закрытый диалог, needs_reauth, 4000 символов), заметки,
``/retry`` (только failed, только свои/админ), WS-события и постановка
``deliver_message`` в ARQ.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.errors import ApiError
from app.models import AuditLog, Conversation, Message
from app.services import inbox
from app.services import messages as msgs
from app.ws.hub import INBOX_CLAIMED
from tests.unit.conftest import drain_events

ARQ_QUEUE = "arq:queue"


# --------------------------------------------------------------- фикстуры


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Роутер отправки монтирует сама фабрика (app/main.py) — здесь только
    HTTP-клиент поверх того же приложения, что и в проде."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


def body(text: str = "Добрый день! Замена экрана — от 8 900 ₽", cmid: str | None = None) -> dict:
    return {"text": text, "client_message_id": cmid or str(uuid.uuid4())}


async def post_send(api, tokens, seed, *, role: str = "manager", **kw) -> httpx.Response:
    return await api.post(
        f"/api/v1/conversations/{seed.conversation_id}/messages",
        json=body(**kw),
        headers=auth(tokens, role),
    )


async def fetch_messages(db_sessionmaker, conv_id, direction: str = "out") -> list[Message]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == conv_id, Message.direction == direction
                    )
                )
            ).scalars()
        )


async def fetch_audit(db_sessionmaker, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


def details(row: AuditLog) -> dict:
    assert row.details is not None
    return row.details


# ---------------------------------------------------------------- отправка


async def test_send_creates_pending_message_and_enqueues_delivery(
    api, tokens, seed_conversation, db_sessionmaker, redis, users_by_role
):
    """01 §6.2: мгновенный 201 с delivery_status=pending + джоба в ARQ."""
    payload = body()
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json=payload,
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["direction"] == "out"
    assert out["sender_type"] == "operator"
    assert out["delivery_status"] == "pending"
    assert out["client_message_id"] == payload["client_message_id"]
    assert out["sender"]["id"] == str(users_by_role["manager"].id)
    assert out["conversation_id"] == str(seed_conversation.conversation_id)

    rows = await fetch_messages(db_sessionmaker, seed_conversation.conversation_id)
    assert len(rows) == 1
    assert rows[0].delivery_status == "pending"
    assert rows[0].external_message_id is None

    # deliver_message поставлен строго после commit'а (08 §8.1 п.4)
    assert await redis.zcard(ARQ_QUEUE) == 1
    assert await redis.exists(f"arq:job:deliver:{out['id']}")


async def test_send_publishes_message_new(api, tokens, seed_conversation, redis):
    """01 §11.3: свой же пузырь уходит другим вкладкам и коллегам."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await post_send(api, tokens, seed_conversation)
    assert r.status_code == 201

    events = await drain_events(pubsub)
    types = [e["type"] for e in events]
    assert "message:new" in types
    new = next(e for e in events if e["type"] == "message:new")
    assert new["data"]["message"]["delivery_status"] == "pending"
    assert new["data"]["conversation_patch"]["status"] == "in_progress"
    assert new["data"]["conversation_patch"]["unread_delta"] == 0
    await pubsub.aclose()


async def test_reply_puts_out_the_wait_gauge_for_everyone(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """ОТВЕТИЛИ — ОРАНЖЕВАЯ МЕТКА ГАСНЕТ У ВСЕХ, А НЕ ТОЛЬКО В БАЗЕ.

    ⚠ ЖАЛОБА С БОЯ 28.08 дословно: «диалог отвечен, но висит у всех как
    неотвеченный». Замер показал, что база права: 56 диалогов с нашим последним
    сообщением, и ни одного с непогашенным `awaiting_since`. Врал экран — этот
    кадр нёс статус и время, но не нёс ожидание, а строка списка держит его с
    последней полной загрузки. Метка росла у всех тринадцати до перезапроса
    списка; на снимке от диспетчера это видно прямо: внизу серверная сводка
    «2 ждут ответа», а меток в списке три.

    Проверяем именно `None`, а не отсутствие ключа: интерфейс отличает их
    намеренно — `undefined` значит «поле не приезжало, считай по-старому», а
    `null` значит «сервер сказал: не ждёт» (shared/lib/waiting.ts).
    """
    async with db_sessionmaker() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == seed_conversation.conversation_id)
            .values(awaiting_since=datetime.now(UTC) - timedelta(minutes=20))
        )
        await s.commit()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert (await post_send(api, tokens, seed_conversation)).status_code == 201
    new = next(e for e in await drain_events(pubsub) if e["type"] == "message:new")
    await pubsub.aclose()

    assert "waiting_since" in new["data"]["conversation_patch"], (
        "кадр не несёт ожидания — метка продолжит расти у всех до перезапроса списка"
    )
    assert new["data"]["conversation_patch"]["waiting_since"] is None


async def test_note_does_not_put_out_the_wait_gauge(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """ЗАМЕТКА — НЕ ОТВЕТ КЛИЕНТУ, И ГАСИТЬ ОЖИДАНИЕ ЕЙ НЕЧЕМ.

    Соблазн был послать в кадре голое `None`: отправка ведь снимает отметку.
    Но тем же кадром уходит и заметка, которой клиент не видел, — погаси она
    метку, и диалог тихо перестал бы требовать ответа ровно оттого, что кто-то
    записал себе «перезвонить». Поэтому в кадре КАНОНИЧЕСКИЙ расчёт по
    состоянию диалога, а не заглушка.
    """
    async with db_sessionmaker() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == seed_conversation.conversation_id)
            .values(awaiting_since=datetime.now(UTC) - timedelta(minutes=20))
        )
        await s.commit()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json=body(text="Перезвонить после 18:00"),
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    new = next(e for e in await drain_events(pubsub) if e["type"] == "message:new")
    await pubsub.aclose()

    assert new["data"]["conversation_patch"]["waiting_since"] is not None, (
        "заметка погасила ожидание — клиент остался ждать молча"
    )


async def test_repeat_with_same_client_message_id_returns_same_message(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """01 §1.6: повтор -> 200 + X-Idempotent-Replay, ТО ЖЕ сообщение, без дубля."""
    payload = body()
    url = f"/api/v1/conversations/{seed_conversation.conversation_id}/messages"
    first = await api.post(url, json=payload, headers=auth(tokens))
    second = await api.post(url, json=payload, headers=auth(tokens))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json()["id"] == first.json()["id"]
    assert len(await fetch_messages(db_sessionmaker, seed_conversation.conversation_id)) == 1
    # повторная джоба не ставится: тело реплея не проходит через enqueue
    assert await redis.zcard(ARQ_QUEUE) == 1


async def test_repeat_with_different_text_is_idempotency_mismatch(api, tokens, seed_conversation):
    """01 §1.6: тот же client_message_id с другим текстом — баг фронта, 409."""
    cmid = str(uuid.uuid4())
    url = f"/api/v1/conversations/{seed_conversation.conversation_id}/messages"
    await api.post(url, json=body("первый", cmid), headers=auth(tokens))
    r = await api.post(url, json=body("другой", cmid), headers=auth(tokens))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_mismatch"


async def test_idempotency_key_without_message_is_reclaimed(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """08 §8.3: «SET прошёл, commit не дошёл» — ключ перезанимается, клиент
    получает 201 и настоящее сообщение, а не вечный реплей в пустоту."""
    cmid = str(uuid.uuid4())
    key = msgs.idempotency_key(seed_conversation.conversation_id, cmid)
    await redis.set(key, str(uuid.uuid4()), ex=msgs.IDEM_TTL_SECONDS)  # id несуществующего msg

    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json=body("после падения", cmid),
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    rows = await fetch_messages(db_sessionmaker, seed_conversation.conversation_id)
    assert len(rows) == 1
    assert str(rows[0].id) == r.json()["id"]
    assert await redis.get(key) == r.json()["id"]


# ------------------------------------------------------------ автоназначение


async def test_first_reply_assigns_and_moves_to_in_progress(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role, redis
):
    """01 §6.2 «кто взял — тот и ведёт» + audit по 06 §0.3 + WS."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert (await post_send(api, tokens, seed_conversation)).status_code == 201

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.status == "in_progress"
        assert conv.assignee_id == users_by_role["manager"].id

    assigned = await fetch_audit(db_sessionmaker, "conversation.assigned")
    assert len(assigned) == 1
    assert details(assigned[0])["assignee_id"] == str(users_by_role["manager"].id)
    assert details(assigned[0])["prev_assignee_id"] is None
    assert details(assigned[0])["by"] == "self"

    changed = await fetch_audit(db_sessionmaker, "conversation.status_changed")
    assert len(changed) == 1
    assert details(changed[0])["from"] == "new"
    assert details(changed[0])["to"] == "in_progress"
    assert details(changed[0])["by"] == "operator"

    events = await drain_events(pubsub)
    updated = [e for e in events if e["type"] == "conversation:updated"]
    assert updated and updated[0]["data"]["patch"]["status"] == "in_progress"
    await pubsub.aclose()


async def test_reply_to_a_queued_dialog_claims_it_and_clears_the_queue_for_everyone(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role, redis
):
    """«Ответил» и есть «принял» (7.1) — иначе строка висит в очереди у коллег.

    Оператор может ответить в непринятый диалог по прямой ссылке или из старого
    клиента, написанного до очереди. Если это не считать принятием, у остальных
    двенадцати строка останется в очереди до перезапроса: они нажмут «Принять»
    и получат 409 по диалогу, в котором уже идёт разговор.
    """
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.offered_at = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert (await post_send(api, tokens, seed_conversation)).status_code == 201

    me = users_by_role["manager"]
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
    assert conv.claimed_by_id == me.id, "ответ не забрал диалог из очереди"
    assert conv.claimed_at is not None
    assert conv.assignee_id == me.id
    assert not inbox.is_waiting(conv)

    # Кадр — тот же, что у кнопки «Принять»: у остальных строка обязана уйти
    # из очереди одинаково, каким бы путём диалог ни забрали.
    claimed = next(e for e in await drain_events(pubsub) if e["type"] == INBOX_CLAIMED)
    # Подпись принявшего — с отделом (04.09), той же сборкой, что у кнопки
    # «Принять»: строка «Диалог принял …» показывается остальным двенадцати.
    assert claimed["data"]["claimed_by"] == {
        "id": str(me.id),
        "full_name": me.full_name,
        "department": me.department,
    }
    assert claimed["data"]["conversation_patch"]["in_inbox"] is False
    await pubsub.aclose()

    # Журнал различает «принял из очереди» и «ответил первым» — это разные
    # события смены, хотя поля у них одни.
    assigned = await fetch_audit(db_sessionmaker, "conversation.assigned")
    assert details(assigned[0])["source"] == "inbox"

    r = await api.get("/api/v1/inbox", headers=auth(tokens, "admin"))
    assert r.json()["items"] == []


async def test_reply_to_a_dialog_that_never_was_in_the_queue_does_not_claim_it(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """Диалог не предлагали очереди — принимать нечего, кадра нет.

    Иначе `inbox:claimed` летел бы на каждый первый ответ во всех старых
    диалогах, и фронт гасил бы строки, которых в очереди не было.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert (await post_send(api, tokens, seed_conversation)).status_code == 201

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
    assert conv.claimed_by_id is None and conv.claimed_at is None

    assert not [e for e in await drain_events(pubsub) if e["type"] == INBOX_CLAIMED]
    assigned = await fetch_audit(db_sessionmaker, "conversation.assigned")
    assert details(assigned[0])["source"] == "reply"
    await pubsub.aclose()


async def test_second_reply_does_not_reassign(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    """Повторная отправка в уже назначенный диалог ничего не переназначает."""
    await post_send(api, tokens, seed_conversation)
    await post_send(api, tokens, seed_conversation, text="второе сообщение")

    assert len(await fetch_audit(db_sessionmaker, "conversation.assigned")) == 1
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.assignee_id == users_by_role["manager"].id


async def test_reply_to_conversation_of_another_manager_keeps_assignee(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    """Диалог уже за коллегой — отправка его не отбирает (передача — §5.5)."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.assignee_id = users_by_role["admin"].id
        conv.status = "in_progress"
        await s.commit()

    assert (await post_send(api, tokens, seed_conversation)).status_code == 201
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.assignee_id == users_by_role["admin"].id
    assert await fetch_audit(db_sessionmaker, "conversation.assigned") == []


# ----------------------------------------------------------------- mute_bot


async def test_operator_message_mutes_bot_forever(api, tokens, seed_conversation, db_sessionmaker):
    """02 §2.6: оператор написал — бот замолкает навсегда + audit bot.muted."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.bot_active = True
        conv.bot_vars = {"problem": "разбит экран"}
        await s.commit()

    assert (await post_send(api, tokens, seed_conversation)).status_code == 201

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.bot_active is False
        assert conv.bot_vars["muted"] is True
        # Спринт 6: глушение делает app.bots.runtime.mute_bot, и оно
        # нормализует легаси-раскладку — скаляр верхнего уровня переезжает в
        # типизированный `vars` (02 §2.1), не теряясь.
        assert conv.bot_vars["vars"]["problem"] == "разбит экран"
        assert conv.bot_vars["waiting"] is None  # ожидающий bot_ask_timeout умрёт по токену

    assert len(await fetch_audit(db_sessionmaker, "bot.muted")) == 1


async def test_note_does_not_mute_bot(api, tokens, seed_conversation, db_sessionmaker):
    """02 §2.6: заметки бота не глушат — менеджер комментирует, бот работает."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.bot_active = True
        await s.commit()

    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json=body("торгуется, дать скидку до 10%"),
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.bot_active is True


# --------------------------------------------------------------------- RBAC


@pytest.mark.parametrize(
    "role,status,code",
    [("head", 403, "read_only_role"), ("observer", 403, "forbidden")],
)
async def test_send_is_denied_for_head_and_observer(
    api, tokens, seed_conversation, role, status, code
):
    """01 §12: messages:send — только admin и manager."""
    r = await post_send(api, tokens, seed_conversation, role=role)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


@pytest.mark.parametrize("role", ["admin", "manager"])
async def test_send_is_allowed_for_admin_and_manager(api, tokens, seed_conversation, role):
    assert (await post_send(api, tokens, seed_conversation, role=role)).status_code == 201


@pytest.mark.parametrize(
    "role,expected", [("admin", 201), ("head", 201), ("manager", 201), ("observer", 403)]
)
async def test_notes_permission_matrix(api, tokens, seed_conversation, role, expected):
    """01 §6.4: notes:write — admin/head/manager; observer — 403."""
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json=body("внутренний комментарий"),
        headers=auth(tokens, role),
    )
    assert r.status_code == expected, r.text


async def test_send_requires_auth(api, seed_conversation):
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages", json=body()
    )
    assert r.status_code == 401


# ----------------------------------------------------------------- валидации


async def test_send_to_closed_conversation_is_422(api, tokens, seed_conversation, db_sessionmaker):
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.status = "closed"
        await s.commit()

    r = await post_send(api, tokens, seed_conversation)
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "conversation_closed"


async def test_send_with_needs_reauth_account_is_409(
    api, tokens, seed_conversation, db_sessionmaker
):
    from app.models import AvitoAccount

    async with db_sessionmaker() as s:
        account = await s.get(AvitoAccount, seed_conversation.account.id)
        account.status = "needs_reauth"
        await s.commit()

    r = await post_send(api, tokens, seed_conversation)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "account_needs_reauth"


async def test_text_over_limit_is_413(api, tokens, seed_conversation):
    r = await post_send(api, tokens, seed_conversation, text="я" * 4001)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


async def test_empty_text_is_rejected(api, tokens, seed_conversation):
    r = await post_send(api, tokens, seed_conversation, text="   ")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_send_to_unknown_conversation_is_404(api, tokens):
    r = await api.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages", json=body(), headers=auth(tokens)
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- заметки


async def test_note_is_delivered_and_not_queued(
    api, tokens, seed_conversation, db_sessionmaker, redis
):
    """01 §6.4: direction=note, delivered сразу, в Авито не уходит."""
    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json=body("торгуется, дать скидку до 10%"),
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["direction"] == "note"
    assert out["sender_type"] == "operator"
    assert out["delivery_status"] == "delivered"

    notes = await fetch_messages(db_sessionmaker, seed_conversation.conversation_id, "note")
    assert len(notes) == 1
    assert await redis.zcard(ARQ_QUEUE) == 0  # доставлять нечего


async def test_note_does_not_assign_conversation(api, tokens, seed_conversation, db_sessionmaker):
    """Заметка — не ответ клиенту: диалог не переходит в in_progress."""
    await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        json=body("внутренний комментарий"),
        headers=auth(tokens),
    )
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.status == "new"
        assert conv.assignee_id is None


async def test_note_is_idempotent(api, tokens, seed_conversation, db_sessionmaker):
    """01 §1.6: заметки тоже уходят из офлайн-очереди — тот же механизм."""
    payload = body("заметка из офлайн-очереди")
    url = f"/api/v1/conversations/{seed_conversation.conversation_id}/notes"
    first = await api.post(url, json=payload, headers=auth(tokens))
    second = await api.post(url, json=payload, headers=auth(tokens))
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers.get("X-Idempotent-Replay") == "true"
    notes = await fetch_messages(db_sessionmaker, seed_conversation.conversation_id, "note")
    assert len(notes) == 1


# ------------------------------------------------------------------- retry


async def _make_failed_message(db_sessionmaker, conv_id, sender_id) -> Message:
    async with db_sessionmaker() as s:
        msg = Message(
            conversation_id=conv_id,
            direction="out",
            sender_type="operator",
            sender_user_id=sender_id,
            body="не доехало",
            attachments=[],
            delivery_status="failed",
            created_at=datetime.now(UTC),
        )
        s.add(msg)
        await s.commit()
        await s.refresh(msg)
        return msg


async def test_retry_resets_failed_to_pending_and_enqueues(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role, redis
):
    """01 §6.3: failed -> pending + новая джоба (счётчик попыток обнуляется)."""
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "pending"

    async with db_sessionmaker() as s:
        assert (await s.get(Message, (msg.id, msg.created_at))).delivery_status == "pending"
    assert await redis.zcard(ARQ_QUEUE) == 1
    assert not await redis.exists(f"arq:job:deliver:{msg.id}")  # id новый, не «основной»


async def test_retry_of_delivered_message_is_422(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    async with db_sessionmaker() as s:
        row = await s.get(Message, (msg.id, msg.created_at))
        row.delivery_status = "delivered"
        await s.commit()

    r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "not_failed"


async def test_retry_of_foreign_message_is_forbidden_for_manager(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    """«Только свои/admin»: чужой пузырь менеджер не перезапускает."""
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["admin"].id
    )
    r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens, "manager"))
    assert r.status_code == 403

    r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens, "admin"))
    assert r.status_code == 200, r.text


async def test_retry_is_denied_for_head_and_observer(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["head"].id
    )
    for role in ("head", "observer"):
        r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens, role))
        assert r.status_code == 403, role


async def test_retry_publishes_pending_status(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role, redis
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens))
    events = await drain_events(pubsub)
    statuses = [e for e in events if e["type"] == "message:status"]
    assert statuses and statuses[0]["data"]["delivery_status"] == "pending"
    await pubsub.aclose()


# ------------------------------------------------------ перечитка под замком
#
# ``FOR UPDATE`` на SQLite компилируется в пустоту — сам замок проверяется на
# настоящем PostgreSQL (tests/integration/test_inbox_race.py). Здесь проверяется
# ДРУГАЯ половина: identity map. Она от диалекта не зависит — сессия отдаёт
# ранее загруженный объект и на SQLite, и на PostgreSQL, — а без перечитки
# ожидание на замке ничего не даёт: решение принимается по копии, снятой ДО
# него. Обновление строки делается мимо ORM (``synchronize_session=False``) —
# ровно так это выглядит для нашей сессии, когда закоммитил сосед.


async def test_conversation_lock_returns_a_fresh_row_not_the_stale_copy(db, seed_conversation):
    """Замок отдаёт то, что в базе, а не то, что сессия прочитала раньше.

    Так выглядит гонка отправки: диалог прочитан через ``db.get`` до ключа
    идемпотентности, дальше запрос стоит на замке, а сосед за это время
    закрывает диалог и коммитит. Со старой копией проверки увидят «new», и
    ответ уедет в закрытую переписку.
    """
    conv_id = seed_conversation.conversation_id
    stale = await db.get(Conversation, conv_id)
    assert stale.status == "new"

    await db.execute(
        update(Conversation)
        .where(Conversation.id == conv_id)
        .values(status="closed")
        .execution_options(synchronize_session=False)
    )

    conv = await msgs.get_conversation_for_update(db, conv_id)
    assert conv.status == "closed"
    with pytest.raises(ApiError) as err:
        await msgs.assert_sendable(db, conv)
    assert err.value.details["reason"] == "conversation_closed"


async def test_retry_lock_returns_a_fresh_message_not_the_stale_copy(
    db, redis, seed_conversation, db_sessionmaker, users_by_role
):
    """Два «Повторить» подряд: второй обязан упереться в not_failed.

    Строка сообщения читается до замка, и без перечитки под замком оба
    запроса увидят ``failed``, оба поставят джобу — клиент получит одно и то
    же сообщение дважды.
    """
    user = users_by_role["manager"]
    msg = await _make_failed_message(db_sessionmaker, seed_conversation.conversation_id, user.id)
    # копия в сессии — тем же запросом, каким её читает сам reset_for_retry
    # (ссылку держим: identity map слабая, иначе копия просто исчезнет)
    stale = (await db.execute(select(Message).where(Message.id == msg.id))).scalar_one()
    assert stale.delivery_status == "failed"

    await db.execute(  # сосед уже увёл сообщение из failed и закоммитил
        update(Message)
        .where(Message.id == msg.id)
        .values(delivery_status="pending")
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(ApiError) as err:
        await msgs.reset_for_retry(db, redis, message_id=msg.id, user=user)
    assert err.value.details["reason"] == "not_failed"
