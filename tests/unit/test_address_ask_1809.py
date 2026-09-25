"""Один вопрос клиенту об адресе без человека (владелец 18.09).

Клиент описал проблему, адреса в диалоге нет, никто его не спросил и никто не
ответил клиенту за `address_ask.delay_sec` — система отправляет ОДНО исходящее
`('out','system')` с текстом-настройкой, раз на диалог навсегда.

⚠ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТ ЭТОТ ФАЙЛ, — ЗАМКИ. Реплика клиенту от имени компании
по счётчику букв обязана НЕ уходить: когда выключено (умолчание False),
когда ведёт человек или бот, когда клиент в чёрном списке, когда адрес уже
есть (в карточке или строкой СО СТЕПЕНЬЮ в диалоге) или о нём уже спрашивали,
когда клиент отказался. И обратное (ревью 19.09, №7): окончательный отказ
карты без степени (`not_found`, `no_city`, `house_missing` без строки улицы)
вопрос НЕ глушит — по контракту п.2 это и есть «вопрос клиенту»; строку,
которую карта ещё проверяет, задача ждёт самоповтором. Каждый замок — свой
тест и диверсия «без замка вопрос уходит» ('sent'). Вторая половина — что вопрос НЕ ломает соседей:
не гасит «клиент ждёт» (доставка через `restore_awaiting`), не становится
красным долгом диалога (`undelivered_status`), не выключает бота эхом своей
отправки, а ответ клиента «ленина 5» поднимается адресом уровня B.

Стенд: SQLite + fakeredis, сид `seed_conversation` (одно входящее «Здравствуйте!
Экран разбит, почём?» = 28 знаков ≥ 25), сдвинутое на 620 с назад. Номера и
адреса вымышленные.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import sqlalchemy as sa
import structlog
from arq.connections import ArqRedis
from sqlalchemy import select

from app.models import (
    AuditLog,
    AvitoAccount,
    Bot,
    Client,
    ClientAddressCandidate,
    Conversation,
    Message,
)
from app.services import address_ask, address_parse, app_settings, crypto
from app.services import clients as clients_svc
from app.services import messages as messages_svc
from app.services.address_ask import InboundRow
from app.services.inbound import _ВОПРОС_ОБ_АДРЕСЕ, _ВОПРОС_ОБ_АДРЕСЕ_СЛОВА, apply_inbound_event
from app.services.messages import as_arq
from app.workers import address_ask as worker
from app.workers import deliver

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ЗАДЕРЖКА = 600
SCENARIO = {
    "version": 1,
    "revision": 1,
    "entry": "greet",
    "steps": [{"id": "greet", "type": "send", "params": {"text": "Здравствуйте!"}}],
}


# ---------------------------------------------------------------- помощники


def ctx(db_sessionmaker, redis) -> dict[str, Any]:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def настроить(db_sessionmaker, values: dict[str, Any]) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, values, user_id=None)
        await s.commit()


async def сдвинуть(db_sessionmaker, message_id: uuid.UUID, *, секунд: int) -> datetime:
    """Входящее сида — «сейчас»; задача требует, чтобы оно было старше задержки."""
    когда = datetime.now(UTC) - timedelta(seconds=секунд)
    async with db_sessionmaker() as s:
        await s.execute(sa.update(Message).where(Message.id == message_id).values(created_at=когда))
        await s.commit()
    return когда


async def реплика(
    db_sessionmaker,
    сид,
    *,
    direction: str = "in",
    sender_type: str = "client",
    body: str | None = "",
    секунд_назад: int = 0,
    delivery_status: str = "delivered",
    attachments: list[Any] | None = None,
    voice_transcript: str | None = None,
) -> Message:
    async with db_sessionmaker() as s:
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=сид.conversation_id,
            direction=direction,
            sender_type=sender_type,
            body=body,
            attachments=attachments or [],
            voice_transcript=voice_transcript,
            delivery_status=delivery_status,
            created_at=datetime.now(UTC) - timedelta(seconds=секунд_назад),
        )
        s.add(msg)
        await s.commit()
        return msg


async def диалог(db_sessionmaker, conversation_id: uuid.UUID) -> Conversation:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conversation_id)
        assert conv is not None
        return conv


async def править_диалог(db_sessionmaker, conversation_id: uuid.UUID, **values: Any) -> None:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conversation_id)
        assert conv is not None
        for k, v in values.items():
            setattr(conv, k, v)
        await s.commit()


async def вопросы_системы(db_sessionmaker, conversation_id: uuid.UUID) -> list[Message]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.direction == "out",
                        Message.sender_type == "system",
                    )
                )
            )
            .scalars()
            .all()
        )


async def прогон(сид, db_sessionmaker, redis, *, attempt: int = 0) -> str:
    return await worker.address_ask_run(
        ctx(db_sessionmaker, redis),
        conversation_id=str(сид.conversation_id),
        message_id=str(сид.message_id),
        attempt=attempt,
    )


async def бот_на_аккаунте(db_sessionmaker, account_id: uuid.UUID, *, mode: str = "auto") -> Bot:
    """Образец `bot_account` из test_bot_wiring: включённый круглосуточный бот."""
    async with db_sessionmaker() as s:
        bot = Bot(
            id=uuid.uuid4(),
            name="Первичный приём",
            is_enabled=True,
            schedule={"always": True},
            scenario=SCENARIO,
            knowledge_base=None,
            mode=mode,
        )
        s.add(bot)
        await s.flush()
        row = await s.get(AvitoAccount, account_id)
        assert row is not None
        row.bot_id = bot.id
        await s.commit()
        return bot


async def поставить_задачу(redis, name: str, job_id: str, **kwargs: Any) -> None:
    await ArqRedis.enqueue_job(as_arq(redis), name, _job_id=job_id, **kwargs)


async def строка_адреса(db_sessionmaker, сид, **поля: Any) -> ClientAddressCandidate:
    """Строка адреса этого диалога с нужным состоянием карты; по умолчанию —
    дом уровня A, которого карта ещё не смотрела (`pending`)."""
    значения: dict[str, Any] = {
        "value": "ул Ленина, 5",
        "street": "ул Ленина",
        "house": "5",
        "raw": "ул Ленина 5",
        "level": address_parse.LEVEL_A,
        "status": "pending",
        "kind": address_parse.KIND_HOUSE,
        "geo_status": "pending",
        "detected_at": datetime.now(UTC),
    }
    значения.update(поля)
    async with db_sessionmaker() as s:
        row = ClientAddressCandidate(
            client_id=сид.client_id, conversation_id=сид.conversation_id, **значения
        )
        s.add(row)
        await s.commit()
        return row


async def пометить(db_sessionmaker, client_id: uuid.UUID, *, blocked: bool) -> None:
    async with db_sessionmaker() as s:
        card = await s.get(Client, client_id)
        assert card is not None
        card.blocked_at = datetime.now(UTC) if blocked else None
        await s.commit()


# ------------------------------------------------------------------ фикстуры


@pytest.fixture
async def включено(db_sessionmaker):
    """Умолчание — ВЫКЛЮЧЕНО; включаем явно, как включит владелец."""
    await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_ENABLED: True})


@pytest.fixture
async def сид(seed_conversation, db_sessionmaker, включено) -> SimpleNamespace:
    """Диалог `new` без хозяина, одно входящее 620 с назад, вопрос включён."""
    seed_conversation.in_at = await сдвинуть(
        db_sessionmaker, seed_conversation.message_id, секунд=ЗАДЕРЖКА + 20
    )
    return seed_conversation


# ------------------------------------------------ 1–2: выключатель и текст


async def test_умолчание_выключено_и_разбор_адресов_главнее(
    seed_conversation, db_sessionmaker, redis
):
    """`Spec(ADDRESS_ASK_ENABLED, False)` — до сухого прогона вопрос не уходит.

    ⚠ ДИВЕРСИЯ: поставить умолчание True — первая проверка краснеет, и это та
    самая беда: реплика клиенту ушла бы выкаткой, а не решением владельца.
    """
    await сдвинуть(db_sessionmaker, seed_conversation.message_id, секунд=ЗАДЕРЖКА + 20)
    assert app_settings.SPECS[app_settings.ADDRESS_ASK_ENABLED].default is False
    assert await прогон(seed_conversation, db_sessionmaker, redis) == "disabled"

    await настроить(
        db_sessionmaker,
        {app_settings.ADDRESS_ASK_ENABLED: True, app_settings.ADDRESS_DETECT_ENABLED: False},
    )
    assert await прогон(seed_conversation, db_sessionmaker, redis) == "disabled"

    await настроить(db_sessionmaker, {app_settings.ADDRESS_DETECT_ENABLED: True})
    assert await прогон(seed_conversation, db_sessionmaker, redis) == "sent"
    assert len(await вопросы_системы(db_sessionmaker, seed_conversation.conversation_id)) == 1


async def test_текст_без_слова_об_адресе_не_уходит(сид, db_sessionmaker, redis):
    """Ответ «Ленина 5» на «Здравствуйте!» разбор адресом не прочтёт —
    спрашивать таким текстом бессмысленно."""
    await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_TEXT: "Здравствуйте!"})
    with structlog.testing.capture_logs() as логи:
        assert await прогон(сид, db_sessionmaker, redis) == "text_unrecognizable"
    assert any(л["event"] == "address_ask.text_unrecognizable" for л in логи)
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []

    # Умолчание — с тремя словами из семи групп.
    assert address_ask.text_is_recognizable(
        app_settings.SPECS[app_settings.ADDRESS_ASK_TEXT].default
    )


# ------------------------------------------------- 3–5: замки по полям


async def test_закрытый_диалог_и_уже_спрошенный(сид, db_sessionmaker, redis):
    await править_диалог(db_sessionmaker, сид.conversation_id, status="closed")
    assert await прогон(сид, db_sessionmaker, redis) == "closed"
    await править_диалог(db_sessionmaker, сид.conversation_id, status="new")

    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    # Раз на диалог навсегда: второй прогон — замок, строка одна.
    assert await прогон(сид, db_sessionmaker, redis) == "already_asked"
    assert len(await вопросы_системы(db_sessionmaker, сид.conversation_id)) == 1


async def test_принятый_человеком_диалог(сид, db_sessionmaker, redis, make_user):
    """Принял человек — ведёт человек: и `claimed_by_id`, и `assignee_id`."""
    человек = await make_user("op@example.com")
    await править_диалог(db_sessionmaker, сид.conversation_id, claimed_by_id=человек.id)
    assert await прогон(сид, db_sessionmaker, redis) == "claimed_by_human"
    await править_диалог(
        db_sessionmaker, сид.conversation_id, claimed_by_id=None, assignee_id=человек.id
    )
    assert await прогон(сид, db_sessionmaker, redis) == "claimed_by_human"
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []


async def test_диалог_ведёт_бот_или_войдёт_следующим_ходом(сид, db_sessionmaker, redis):
    """`bot_active` — замок по полю; auto-бот без замка входа — замок, потому
    что заговорит следующим ходом; suggest-бот и бот после передачи — нет."""
    await править_диалог(db_sessionmaker, сид.conversation_id, bot_active=True)
    assert await прогон(сид, db_sessionmaker, redis) == "bot_leads"
    await править_диалог(db_sessionmaker, сид.conversation_id, bot_active=False)

    бот = await бот_на_аккаунте(db_sessionmaker, сид.account.id, mode="auto")
    assert await прогон(сид, db_sessionmaker, redis) == "bot_leads"

    async with db_sessionmaker() as s:
        row = await s.get(Bot, бот.id)
        assert row is not None
        row.mode = "suggest"  # подсказка — заметка сотруднику, клиенту не говорит
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


async def test_бот_после_передачи_не_замок(сид, db_sessionmaker, redis):
    await бот_на_аккаунте(db_sessionmaker, сид.account.id, mode="auto")
    await править_диалог(
        db_sessionmaker,
        сид.conversation_id,
        bot_vars={"handoff": {"reason": "not_understood", "at": "2026-09-18T10:00:00+00:00"}},
    )
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


# ------------------------------------ 6–7: последнее слово, возраст, аккаунт


async def test_замки_ленты_стоят_до_аккаунта(сид, db_sessionmaker, redis):
    """Сторож порядка: `needs_reauth` + реплика 10 с назад → `too_soon`, а не
    `account_inactive` — серия реплик выходит одним SELECT'ом."""
    async with db_sessionmaker() as s:
        acc = await s.get(AvitoAccount, сид.account.id)
        assert acc is not None
        acc.status = "needs_reauth"
        await s.commit()
    await реплика(db_sessionmaker, сид, body="и ещё зарядка не работает", секунд_назад=10)
    assert await прогон(сид, db_sessionmaker, redis) == "too_soon"


async def test_последнее_слово_не_за_клиентом(сид, db_sessionmaker, redis):
    """Исходящее оператора или бота после реплики — замок; заметка — нет."""
    заметка = await реплика(
        db_sessionmaker, сид, direction="note", sender_type="operator", body="перезвонить"
    )
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(Message).where(Message.id == заметка.id))
        await s.execute(
            sa.delete(Message).where(
                Message.conversation_id == сид.conversation_id, Message.sender_type == "system"
            )
        )
        await s.commit()
    await править_диалог(db_sessionmaker, сид.conversation_id, address_asked_at=None)

    ответ = await реплика(
        db_sessionmaker, сид, direction="out", sender_type="operator", body="Сейчас посмотрю"
    )
    assert await прогон(сид, db_sessionmaker, redis) == "answered"
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(Message).where(Message.id == ответ.id))
        await s.commit()
    await реплика(db_sessionmaker, сид, direction="out", sender_type="bot", body="Чем помочь?")
    assert await прогон(сид, db_sessionmaker, redis) == "answered"


async def test_моложе_задержки_и_старше_часа(сид, db_sessionmaker, redis):
    await реплика(db_sessionmaker, сид, body="и ещё зарядка не работает", секунд_назад=10)
    assert await прогон(сид, db_sessionmaker, redis) == "too_soon"

    async with db_sessionmaker() as s:
        await s.execute(
            sa.delete(Message).where(
                Message.conversation_id == сид.conversation_id, Message.id != сид.message_id
            )
        )
        await s.commit()
    await сдвинуть(db_sessionmaker, сид.message_id, секунд=2 * 3600)
    assert await прогон(сид, db_sessionmaker, redis) == "stale"

    # `stale` считается от ПЛАНОВОГО момента (реплика + delay), не от реплики:
    # при delay=3600 (верх ручки) реплика 3700 с назад — в окне, а не «старше
    # часа». ДИВЕРСИЯ: вернуть `возраст > MAX_AGE` — здесь 'stale', и с такой
    # задержкой вопрос не ушёл бы никогда (too_soon < 3600 < stale).
    await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_DELAY_SEC: 3600})
    await сдвинуть(db_sessionmaker, сид.message_id, секунд=3700)
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    await править_диалог(db_sessionmaker, сид.conversation_id, address_asked_at=None)
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(Message).where(Message.sender_type == "system"))
        await s.commit()
    await сдвинуть(db_sessionmaker, сид.message_id, секунд=3600 + 3700)
    assert await прогон(сид, db_sessionmaker, redis) == "stale"

    # Задержка — настройка: 120 с через set_many, реплика 130 с назад проходит.
    await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_DELAY_SEC: 120})
    await сдвинуть(db_sessionmaker, сид.message_id, секунд=130)
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


async def test_неактивный_аккаунт(сид, db_sessionmaker, redis):
    async with db_sessionmaker() as s:
        acc = await s.get(AvitoAccount, сид.account.id)
        assert acc is not None
        acc.status = "needs_reauth"
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "account_inactive"
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []


# ------------------------------------------- 8, 10, 25: переходные замки


async def test_история_едет_самоповтор_до_трёх_раз(сид, db_sessionmaker, redis, monkeypatch):
    """Подгрузка аккаунта идёт → 'retry' с задачей `addr-ask:{id}:r1`; на
    третьей попытке — замок как известная потеря."""

    async def running(redis_, account_id):
        return {"status": "running"}

    monkeypatch.setattr(worker, "get_backfill_state", running)
    assert await прогон(сид, db_sessionmaker, redis) == "retry"
    assert await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r1")
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []

    assert await прогон(сид, db_sessionmaker, redis, attempt=3) == "history_loading"
    assert not await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r4")


async def test_история_чата_в_очереди_замок_а_результат_нет(сид, db_sessionmaker, redis):
    """`convhist:{conv}` в очереди — замок; ⚠ `arq:result:` — НЕ замок:
    `backfill_conversation` без `keep_result=0` хранит результат час."""
    await поставить_задачу(redis, "backfill_conversation", f"convhist:{сид.conversation_id}")
    assert await прогон(сид, db_sessionmaker, redis) == "retry"

    await redis.delete(f"arq:job:convhist:{сид.conversation_id}")
    await redis.zrem("arq:queue", f"convhist:{сид.conversation_id}")
    await redis.set(f"arq:result:convhist:{сид.conversation_id}", "x")
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


async def test_набор_статусов_в_полёте(redis):
    """`_история_едет`: deferred/queued/in_progress — True; complete/not_found — False."""
    conv = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4())
    assert await worker._история_едет(redis, conv) is False  # not_found

    await поставить_задачу(redis, "backfill_conversation", f"convhist:{conv.id}")  # queued
    assert await worker._история_едет(redis, conv) is True
    await redis.zadd("arq:queue", {f"convhist:{conv.id}": 2_000_000_000_000})  # deferred
    assert await worker._история_едет(redis, conv) is True
    await redis.zrem("arq:queue", f"convhist:{conv.id}")
    await redis.set(f"arq:in-progress:convhist:{conv.id}", "1")
    assert await worker._история_едет(redis, conv) is True
    await redis.delete(f"arq:in-progress:convhist:{conv.id}")
    await redis.set(f"arq:result:convhist:{conv.id}", "x")  # complete
    assert await worker._история_едет(redis, conv) is False


async def test_модель_читает_реплику_самоповтор(сид, db_sessionmaker, redis):
    await поставить_задачу(redis, "llm_address_read", f"llm-addr:{сид.message_id}")
    assert await прогон(сид, db_sessionmaker, redis) == "retry"
    assert await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r1")
    assert await прогон(сид, db_sessionmaker, redis, attempt=3) == "llm_reading"
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []


# -------------------------------------------------- 9: карточка и строка


async def test_адрес_в_карточке_и_строка_адреса(сид, db_sessionmaker, redis):
    async with db_sessionmaker() as s:
        card = await s.get(Client, сид.client_id)
        assert card is not None
        card.address = "ул. Ленина, 5"
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "card_has_address"

    async with db_sessionmaker() as s:
        card = await s.get(Client, сид.client_id)
        assert card is not None
        card.address = None
        found = address_parse.parse("ул. Ленина 5")
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=сид.conversation_id,
            message_id=сид.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        candidate_id = записано.candidate_id
    # Свежая строка живого пути — карта её ещё не смотрела: задача ждёт (retry),
    # а не выходит; степень появится — станет замком.
    assert await прогон(сид, db_sessionmaker, redis) == "retry"
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, candidate_id)
        assert row is not None
        row.geo_status, row.geo_lat, row.geo_lon = "exact", 55.0, 37.0
        row.geo_formatted = "улица Ленина, 5, Город"
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "candidate_exists"

    # Отклонённая строка — не замок; строка старше суток — тоже.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, candidate_id)
        assert row is not None
        row.status = "rejected"
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    await править_диалог(db_sessionmaker, сид.conversation_id, address_asked_at=None)
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(Message).where(Message.sender_type == "system"))
        row = await s.get(ClientAddressCandidate, candidate_id)
        assert row is not None
        row.status = "pending"
        row.detected_at = datetime.now(UTC) - timedelta(hours=25)
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


@pytest.mark.parametrize(
    ("строка", "итог"),
    [
        # Окончательный отказ без степени — «вопрос клиенту» (контракт п.2).
        (
            {
                "kind": "place",
                "street": "",
                "house": "",
                "value": "микрорайон 9",
                "geo_status": "not_found",
                "geo_formatted": None,
            },
            "sent",
        ),
        ({"geo_status": "house_missing", "geo_formatted": None}, "sent"),
        ({"geo_status": "not_found", "geo_formatted": None}, "sent"),
        # `no_city` — воркер к нему вернётся, но текст вопроса просит город
        # именно ради этого случая: не ожидание, а повод спросить.
        ({"geo_status": "no_city", "geo_formatted": None}, "sent"),
        ({"geo_status": "ambiguous", "geo_formatted": None}, "sent"),
        # Степень есть — замок: text, exact, approx, место.
        (
            {"geo_status": "house_missing", "geo_formatted": "улица Ленина, 5, Город"},
            "candidate_exists",
        ),
        ({"geo_status": "exact", "geo_lat": 55.0, "geo_lon": 37.0}, "candidate_exists"),
        (
            {
                "geo_status": "exact",
                "geo_provider": "dadata~approx",
                "geo_lat": 55.0,
                "geo_lon": 37.0,
            },
            "candidate_exists",
        ),
        (
            {
                "kind": "place",
                "street": "",
                "house": "",
                "value": "снт Солнечный",
                "geo_status": "exact",
                "geo_provider": "dadata",
                "geo_lat": 55.0,
                "geo_lon": 37.0,
            },
            "candidate_exists",
        ),
        # `exact` без координат — инвариант нарушен, степени нет — не замок.
        ({"geo_status": "exact", "geo_lat": None, "geo_lon": None}, "sent"),
        # Карта ещё проверяет строку A/B — самоповтор.
        ({"geo_status": "pending"}, "retry"),
        ({"geo_status": None}, "retry"),
        ({"geo_status": "error"}, "retry"),
        ({"geo_status": "blocked"}, "retry"),
        ({"geo_status": "pending", "level": address_parse.LEVEL_B}, "retry"),
        # Невод уровня C вопрос не держит: 44–70 % ложных.
        ({"geo_status": "pending", "level": address_parse.LEVEL_C}, "sent"),
    ],
    ids=lambda v: v if isinstance(v, str) else f"{v.get('level', 'A')}:{v.get('geo_status')}",
)
async def test_замок_по_строке_адреса_решает_степень(сид, db_sessionmaker, redis, строка, итог):
    """Контракт п.5: глушит строка СО СТЕПЕНЬЮ; п.2: `not_found` — вопрос
    клиенту. ДИВЕРСИЯ: вернуть `latest_address_candidate is not None` — все
    `sent` и `retry` здесь станут `candidate_exists`, и сценарий владельца
    «9 микрорайон → not_found → система спрашивает» умрёт."""
    await строка_адреса(db_sessionmaker, сид, **строка)
    assert await прогон(сид, db_sessionmaker, redis) == итог
    if итог == "retry":
        assert await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r1")
        assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []
        # На третьей попытке — замок как известная потеря, с именем из реестра.
        assert await прогон(сид, db_sessionmaker, redis, attempt=3) == "geo_pending"
        assert "geo_pending" in worker.ЗАМКИ
    elif итог == "sent":
        assert len(await вопросы_системы(db_sessionmaker, сид.conversation_id)) == 1
    else:
        assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []


async def test_замок_по_строкам_смотрит_всё_окно_а_не_последнюю(сид, db_sessionmaker, redis):
    """Последняя строка — `not_found`, а часом раньше в окне — `exact`: адрес в
    диалоге есть. ДИВЕРСИЯ: судить одну последнюю строку — здесь `sent`."""
    await строка_адреса(
        db_sessionmaker,
        сид,
        geo_status="exact",
        geo_lat=55.0,
        geo_lon=37.0,
        detected_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await строка_адреса(
        db_sessionmaker,
        сид,
        value="ул Мира, 7",
        street="ул Мира",
        house="7",
        raw="ул Мира 7",
        geo_status="not_found",
    )
    assert await прогон(сид, db_sessionmaker, redis) == "candidate_exists"


def test_замок_по_строкам_чистой_функцией():
    """`candidate_lock` — ОДИН предикат для задачи и сухого прогона; статусы
    ожидания — `geocode.CHECKING_STATUSES` без `no_city`."""
    from app.services import geocode

    def row(**kw: Any) -> ClientAddressCandidate:
        base: dict[str, Any] = {
            "kind": "house",
            "level": "A",
            "geo_status": "pending",
            "geo_provider": None,
            "geo_lat": None,
            "geo_lon": None,
            "geo_formatted": None,
        }
        base.update(kw)
        return ClientAddressCandidate(**base)

    assert address_ask.candidate_lock([]) is None
    assert address_ask.candidate_lock([row(geo_status="not_found")]) is None
    assert address_ask.candidate_lock([row(geo_status="no_city")]) is None
    assert address_ask.candidate_lock([row()]) == address_ask.GEO_PENDING_LOCK == "geo_pending"
    assert address_ask.candidate_lock([row(level="C")]) is None
    assert (
        address_ask.candidate_lock([row(geo_status="exact", geo_lat=1.0, geo_lon=2.0)])
        == "candidate_exists"
    )
    # Степень сильнее ожидания: в списке и pending, и exact — замок степени.
    assert (
        address_ask.candidate_lock([row(), row(geo_status="exact", geo_lat=1.0, geo_lon=2.0)])
        == "candidate_exists"
    )
    assert address_ask.GEO_CHECKING_FOR_ASK == geocode.CHECKING_STATUSES - {geocode.GEO_NO_CITY}
    assert geocode.GEO_NO_CITY in geocode.CHECKING_STATUSES, "стенд: no_city больше не CHECKING"


# ----------------------------------------------- 9а: чёрный список


async def test_помеченному_клиенту_вопрос_не_уходит(сид, db_sessionmaker, redis):
    """Чёрный список (models/client.py) снимает требование внимания: диалог не
    в очереди, никто не назначен, бот не ведёт — все прочие замки помеченного
    пропускают, и «никто не ответил за 600 с» выполняется всегда. Без замка
    вопрос от имени компании уходил бы ровно тем, с кем решили не работать, и
    доставка возвращала бы снятое «ждёт». ДИВЕРСИЯ: `blocked_at=None` → `sent`.
    """
    до = await диалог(db_sessionmaker, сид.conversation_id)
    await пометить(db_sessionmaker, сид.client_id, blocked=True)
    with structlog.testing.capture_logs() as логи:
        assert await прогон(сид, db_sessionmaker, redis) == "client_blocked"
    assert any(
        л["event"] == "address_ask.skipped" and л["reason"] == "client_blocked" for л in логи
    )
    assert await вопросы_системы(db_sessionmaker, сид.conversation_id) == []
    async with db_sessionmaker() as s:
        assert (
            await s.execute(select(AuditLog).where(AuditLog.action == "conversation.address_asked"))
        ).scalar_one_or_none() is None
    после = await диалог(db_sessionmaker, сид.conversation_id)
    assert после.address_asked_at is None
    assert после.awaiting_since == до.awaiting_since

    # Помеченный с адресом в карточке — тоже `client_blocked`, не `card_has_address`:
    # иначе сухой прогон занизил бы долю помеченных.
    async with db_sessionmaker() as s:
        card = await s.get(Client, сид.client_id)
        assert card is not None
        card.address = "ул. Ленина, 5"
        await s.commit()
    assert await прогон(сид, db_sessionmaker, redis) == "client_blocked"
    async with db_sessionmaker() as s:
        card = await s.get(Client, сид.client_id)
        assert card is not None
        card.address = None
        await s.commit()

    await пометить(db_sessionmaker, сид.client_id, blocked=False)
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


def test_замок_карточки_чистой_функцией():
    assert address_ask.card_lock(None) is None
    assert address_ask.card_lock(SimpleNamespace(blocked_at=None, address=None)) is None
    assert (
        address_ask.card_lock(SimpleNamespace(blocked_at=None, address="ул Ленина, 5"))
        == "card_has_address"
    )
    assert (
        address_ask.card_lock(SimpleNamespace(blocked_at=datetime.now(UTC), address=None))
        == "client_blocked"
    )
    assert (
        address_ask.card_lock(SimpleNamespace(blocked_at=datetime.now(UTC), address="ул Ленина, 5"))
        == "client_blocked"
    )


# ------------------------------------------------- 11: уже спрашивали


@pytest.mark.parametrize(
    ("вопрос", "замок"),
    [
        ("Подскажите, куда к вам подъехать?", True),
        ("Диагностика 500 ₽", False),
        # Осознанно консервативно: «этаж» — слово из семи групп, лучше не спросить.
        ("Диагностика на 3 этаже — 500 ₽", True),
    ],
)
async def test_об_адресе_уже_спрашивали_в_ленте(сид, db_sessionmaker, redis, вопрос, замок):
    """Вопрос оператора три дня назад, потом свежее входящее — замок без окна времени."""
    await реплика(
        db_sessionmaker,
        сид,
        direction="out",
        sender_type="operator",
        body=вопрос,
        секунд_назад=3 * 24 * 3600,
    )
    assert await прогон(сид, db_sessionmaker, redis) == ("asked_in_feed" if замок else "sent")


async def test_недоставленный_вопрос_системы_тоже_вопрос(сид, db_sessionmaker, redis):
    await реплика(
        db_sessionmaker,
        сид,
        direction="out",
        sender_type="system",
        body="Подскажите адрес",
        delivery_status="dismissed",
        секунд_назад=3600,
    )
    assert await прогон(сид, db_sessionmaker, redis) == "asked_in_feed"


# ------------------------------------------------------ 12: отказ клиента


@pytest.mark.parametrize(
    ("текст", "отказ"),
    [
        ("Спасибо, не нужно", True),
        ("Передумал, отбой", True),
        ("Уже вызвал мастера", True),
        # Граница слова: «мне нужно» ≠ «не нужно»; «я не передумал» — не отказ.
        ("Мне нужно починить экран, разбит", False),
        ("Я не передумал, приезжайте", False),
        ("Диагностика не нужна, сразу ремонт", True),  # консервативно
    ],
)
def test_отказ_клиента_чистой_функцией(текст, отказ):
    строки = [InboundRow(id=uuid.uuid4(), body=текст, attachments=[], voice_transcript=None)]
    assert address_ask.client_declined(строки) is отказ


async def test_отказавшемуся_не_спрашиваем(сид, db_sessionmaker, redis):
    await реплика(db_sessionmaker, сид, body="Спасибо, не нужно", секунд_назад=ЗАДЕРЖКА + 5)
    assert await прогон(сид, db_sessionmaker, redis) == "declined"


async def test_мне_нужно_не_отказ(сид, db_sessionmaker, redis):
    await реплика(
        db_sessionmaker, сид, body="Мне нужно починить экран, разбит", секунд_назад=ЗАДЕРЖКА + 5
    )
    assert await прогон(сид, db_sessionmaker, redis) == "sent"


# ----------------------------------------------- 13: клиент ничего не описал


def test_описал_ли_чистой_функцией():
    assert address_ask.client_described([], min_chars=25) == (False, 0)
    пунктуация = [
        InboundRow(id=uuid.uuid4(), body="?!.. — …", attachments=[], voice_transcript=None)
    ]
    assert address_ask.client_described(пунктуация, min_chars=1) == (False, 0)
    фото = [
        InboundRow(
            id=uuid.uuid4(), body="", attachments=[{"avito_type": "image"}], voice_transcript=None
        )
    ]
    assert address_ask.client_described(фото, min_chars=25) == (True, 0)
    голос = [
        InboundRow(
            id=uuid.uuid4(),
            body=None,
            attachments=[{"avito_type": "voice"}],
            voice_transcript="разбит экран",
        )
    ]
    assert address_ask.client_described(голос, min_chars=100) == (True, 11)
    расшифровка = [
        InboundRow(
            id=uuid.uuid4(), body="", attachments=[], voice_transcript="разбит экран телефона"
        )
    ]
    assert address_ask.client_described(расшифровка, min_chars=19) == (True, 19)
    assert address_ask.client_described(пунктуация, min_chars=0) == (True, 0)


async def test_ничего_не_описал_и_число_знаков_в_журнале(seed_conversation, db_sessionmaker, redis):
    """«Привет» — замок, в журнале `chars=6`: распределение за первый день и
    есть калибровка порога."""
    await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_ENABLED: True})
    когда = datetime.now(UTC) - timedelta(seconds=ЗАДЕРЖКА + 20)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Message)
            .where(Message.id == seed_conversation.message_id)
            .values(body="Привет", created_at=когда)
        )
        await s.commit()
    with structlog.testing.capture_logs() as логи:
        assert await прогон(seed_conversation, db_sessionmaker, redis) == "not_described"
    пропуск = next(л for л in логи if л["event"] == "address_ask.skipped")
    assert пропуск["reason"] == "not_described" and int(пропуск["chars"]) == 6
    assert "Привет" not in str(логи), "в журнале тело реплики"

    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Message)
            .where(Message.id == seed_conversation.message_id)
            .values(body="", attachments=[{"avito_type": "image", "kind": "image"}])
        )
        await s.commit()
    assert await прогон(seed_conversation, db_sessionmaker, redis) == "sent"


async def test_порог_ноль_спрашивает_по_любому_слову(seed_conversation, db_sessionmaker, redis):
    await настроить(
        db_sessionmaker,
        {app_settings.ADDRESS_ASK_ENABLED: True, app_settings.ADDRESS_ASK_MIN_CHARS: 0},
    )
    когда = datetime.now(UTC) - timedelta(seconds=ЗАДЕРЖКА + 20)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Message)
            .where(Message.id == seed_conversation.message_id)
            .values(body="Привет", created_at=когда)
        )
        await s.commit()
    assert await прогон(seed_conversation, db_sessionmaker, redis) == "sent"


# -------------------------------------------------------- 14: отправка


async def test_отправка_строка_отметки_кадр_задача_журнал(сид, db_sessionmaker, redis):
    """Что пишется и что НЕ трогается."""
    до = await диалог(db_sessionmaker, сид.conversation_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    assert await прогон(сид, db_sessionmaker, redis) == "sent"

    [msg] = await вопросы_системы(db_sessionmaker, сид.conversation_id)
    assert msg.delivery_status == "pending"
    assert msg.body == app_settings.SPECS[app_settings.ADDRESS_ASK_TEXT].default
    assert msg.sender_user_id is None
    после = await диалог(db_sessionmaker, сид.conversation_id)
    assert после.address_asked_at is not None
    assert _utc(после.last_message_at) == _utc(msg.created_at)
    assert после.awaiting_since == до.awaiting_since
    assert после.unread_count == до.unread_count
    assert после.bot_active == до.bot_active
    assert после.bot_vars == до.bot_vars

    from tests.unit.conftest import drain_events

    события = await drain_events(pubsub)
    кадр = next(e for e in события if e["type"] == "message:new")
    assert кадр["data"]["conversation_patch"]["unread_delta"] == 0
    assert кадр["data"]["message"]["sender_type"] == "system"
    assert await redis.exists(f"arq:job:deliver:{msg.id}"), "задача доставки не встала"

    async with db_sessionmaker() as s:
        запись = (
            await s.execute(select(AuditLog).where(AuditLog.action == "conversation.address_asked"))
        ).scalar_one()
    assert запись.user_id is None
    assert запись.details["chars"] == 28
    assert запись.details["message_id"] == str(msg.id)


class _СессияСМедленнымЗамком:
    """Сессия, у которой `SELECT … FOR UPDATE` ждёт `ждать` секунд — так задача
    простаивает за соседней транзакцией (тик бота, приём). Всё остальное —
    настоящая AsyncSession."""

    def __init__(self, session: Any, *, ждать: float) -> None:
        self._s, self._ждать = session, ждать

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)

    async def __aenter__(self) -> _СессияСМедленнымЗамком:
        await self._s.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._s.__aexit__(*exc)

    async def execute(self, stmt: Any, *a: Any, **kw: Any) -> Any:
        if getattr(stmt, "_for_update_arg", None) is not None:
            await anyio.sleep(self._ждать)
        return await self._s.execute(stmt, *a, **kw)


async def test_сейчас_берётся_после_захвата_замка_строки(сид, db_sessionmaker, redis):
    """`now` — ПОСЛЕ `FOR UPDATE`, не до транзакции: простояв за соседней,
    задача считала бы возраст реплики по времени до ожидания (ложный
    `too_soon`), а время вопроса отставало бы от настоящего. Стенд: замок ждёт
    0,8 с; `created_at` вопроса (= `now`) обязан быть позже старта на эти 0,8 с.
    ДИВЕРСИЯ: вернуть `now = datetime.now(UTC)` в начало задачи — разница
    становится миллисекундами."""
    ждать = 0.8

    def фабрика() -> _СессияСМедленнымЗамком:
        return _СессияСМедленнымЗамком(db_sessionmaker(), ждать=ждать)

    старт = datetime.now(UTC)
    assert (
        await worker.address_ask_run(
            ctx(фабрика, redis),
            conversation_id=str(сид.conversation_id),
            message_id=str(сид.message_id),
        )
        == "sent"
    )
    [msg] = await вопросы_системы(db_sessionmaker, сид.conversation_id)
    conv = await диалог(db_sessionmaker, сид.conversation_id)
    assert (_utc(msg.created_at) - старт).total_seconds() >= ждать
    assert _utc(conv.address_asked_at) == _utc(msg.created_at)


# ------------------------------------ 15: доставка не гасит «клиент ждёт»


class ФейкАвито:
    """Отправка текста — единственная ручка, которая тут нужна (образец
    tests/unit/test_deliver_images.py::ФейкАвито)."""

    def __init__(self) -> None:
        self.тексты: list[str] = []

    async def send_message(self, token: str, user_id: int, chat_id: str, text: str) -> str:
        self.тексты.append(text)
        return f"ext-txt-{len(self.тексты)}"


@pytest.fixture
def авито(monkeypatch) -> ФейкАвито:
    фейк = ФейкАвито()
    monkeypatch.setattr(deliver._OutboundClient, "send_message", фейк.send_message)
    return фейк


@pytest.fixture
async def канал(db_sessionmaker, включено) -> SimpleNamespace:
    """Аккаунт с НАСТОЯЩИМИ шифрованными токенами (доставка их расшифровывает;
    заглушка `seed_conversation` до Авито не доехала бы) + диалог с входящим
    620 с назад и `awaiting_since` на нём. Образец test_deliver_images.py::канал."""
    now = datetime.now(UTC)
    in_at = now - timedelta(seconds=ЗАДЕРЖКА + 20)
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Вопрос",
            avito_user_id=111222555,
            access_token_enc=crypto.encrypt_token("access-token"),
            refresh_token_enc=crypto.encrypt_token("refresh-token"),
            token_expires_at=now + timedelta(days=1),
            status="active",
            webhook_secret="whsec-ask",
        )
        клиент = Client(channel="avito", external_id="999003", name="Пётр Адресов")
        s.add_all([account, клиент])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-ask",
            account_id=account.id,
            client_id=клиент.id,
            status="new",
            unread_count=1,
            last_message_at=in_at,
            awaiting_since=in_at,
            offered_at=in_at,
        )
        s.add(conv)
        await s.flush()
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id="am-ask-1",
            direction="in",
            sender_type="client",
            body="Здравствуйте! Экран разбит, почём?",
            attachments=[],
            delivery_status="delivered",
            created_at=in_at,
        )
        s.add(msg)
        await s.commit()
        return SimpleNamespace(
            account=account,
            client_id=клиент.id,
            conversation_id=conv.id,
            message_id=msg.id,
            in_at=in_at,
        )


async def test_доставка_вопроса_не_гасит_ожидание(канал, db_sessionmaker, redis, авито):
    """⚠ ПОСЛЕ доставки, не до: успех зовёт `restore_awaiting`, чья граница —
    последний доставленный исходящий. Без `sender_type != 'system'` вопрос стал
    бы границей, и сторож «клиент ждёт 15 минут» потерял бы диалог.

    ДИВЕРСИЯ: убрать фильтр в `restore_awaiting` — `awaiting_since` гаснет.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    assert await прогон(канал, db_sessionmaker, redis) == "sent"
    [msg] = await вопросы_системы(db_sessionmaker, канал.conversation_id)

    await deliver.deliver_message(ctx(db_sessionmaker, redis), msg.id)

    assert авито.тексты == [msg.body]
    [msg] = await вопросы_системы(db_sessionmaker, канал.conversation_id)
    assert msg.delivery_status == "delivered"
    conv = await диалог(db_sessionmaker, канал.conversation_id)
    assert conv.awaiting_since is not None
    assert _utc(conv.awaiting_since) == канал.in_at

    from tests.unit.conftest import drain_events

    кадры = [e for e in await drain_events(pubsub) if e["type"] == "message:status"]
    assert кадры and кадры[-1]["data"]["conversation_patch"]["waiting_since"] is not None


async def test_контроль_ответ_бота_гасит_ожидание_как_было(канал, db_sessionmaker, redis, авито):
    """Бот ответил — клиент отвечен: граница для `('out','bot')` осталась."""
    async with db_sessionmaker() as s:
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=канал.conversation_id,
            direction="out",
            sender_type="bot",
            body="Здравствуйте! Чем помочь?",
            attachments=[],
            delivery_status="pending",
            created_at=datetime.now(UTC),
        )
        s.add(msg)
        await s.commit()
    await deliver.deliver_message(ctx(db_sessionmaker, redis), msg.id)
    conv = await диалог(db_sessionmaker, канал.conversation_id)
    assert conv.awaiting_since is None


# ----------------------------------------- 16–17: недоставка — не долг


def test_статус_недоставленного_чистой_функцией():
    def m(direction: str, sender_type: str) -> Message:
        return Message(direction=direction, sender_type=sender_type)

    assert messages_svc.undelivered_status(m("out", "system")) == "dismissed"
    assert messages_svc.undelivered_status(m("out", "operator")) == "failed"
    assert messages_svc.undelivered_status(m("out", "bot")) == "failed"
    assert messages_svc.undelivered_status(m("note", "operator")) == "failed"


async def test_недоставленный_вопрос_не_долг(канал, db_sessionmaker, redis):
    """`_fail` для системной строки: `dismissed`, `undelivered_at` пуст,
    ожидание не тронуто, кадр `message:status` со статусом `dismissed`,
    уведомления нет (автора нет). Для строки бота — `failed` и `undelivered_at`.
    """
    assert await прогон(канал, db_sessionmaker, redis) == "sent"
    [msg] = await вопросы_системы(db_sessionmaker, канал.conversation_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await deliver._fail(ctx(db_sessionmaker, redis), msg.id, deliver.EXHAUSTED_ERROR)

    [msg] = await вопросы_системы(db_sessionmaker, канал.conversation_id)
    assert msg.delivery_status == "dismissed"
    conv = await диалог(db_sessionmaker, канал.conversation_id)
    assert conv.undelivered_at is None
    assert _utc(conv.awaiting_since) == канал.in_at
    from tests.unit.conftest import drain_events

    события = await drain_events(pubsub)
    кадр = next(e for e in события if e["type"] == "message:status")
    assert кадр["data"]["delivery_status"] == "dismissed"
    assert not [e for e in события if e["type"].startswith("notification")]

    async with db_sessionmaker() as s:
        бот = Message(
            id=uuid.uuid4(),
            conversation_id=канал.conversation_id,
            direction="out",
            sender_type="bot",
            body="Чем помочь?",
            attachments=[],
            delivery_status="pending",
            created_at=datetime.now(UTC),
        )
        s.add(бот)
        await s.commit()
    await deliver._fail(ctx(db_sessionmaker, redis), бот.id, deliver.EXHAUSTED_ERROR)
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Message).where(Message.id == бот.id))).scalar_one()
        assert row.delivery_status == "failed"
    conv = await диалог(db_sessionmaker, канал.conversation_id)
    assert conv.undelivered_at is not None


async def test_отказ_очереди_кладёт_dismissed_и_не_роняет_задачу(
    сид, db_sessionmaker, redis, monkeypatch
):
    async def не_встала(redis_, message_id, *, job_id=None):
        return False

    monkeypatch.setattr(messages_svc, "enqueue_deliver", не_встала)
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    [msg] = await вопросы_системы(db_sessionmaker, сид.conversation_id)
    assert msg.delivery_status == "dismissed"
    conv = await диалог(db_sessionmaker, сид.conversation_id)
    assert conv.undelivered_at is None


# --------------------------------------- 18–19: порядок commit → постановка


async def test_задача_доставки_ставится_после_commit(сид, db_sessionmaker, redis, monkeypatch):
    """Шпион в СВОЕЙ сессии находит строку — иначе воркер доставки прочёл бы пустоту."""
    видел: dict[str, Any] = {}

    async def шпион(redis_, message_id, *, job_id=None):
        async with db_sessionmaker() as s:
            row = (
                await s.execute(select(Message).where(Message.id == message_id))
            ).scalar_one_or_none()
        видел["найдена"] = row is not None
        return True

    monkeypatch.setattr(messages_svc, "enqueue_deliver", шпион)
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    assert видел == {"найдена": True}


AVITO_USER_ID = 111222444
T_LIVE = datetime.now(UTC)


def событие(
    текст: str, *, msg: str = "am-1", when: datetime | None = None, **kw: Any
) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-ask-in",
        external_message_id=msg,
        author_id=999002,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when or datetime.now(UTC),
        client_name="Пётр Адресов",
        item_title="Ремонт телевизора",
        item_url="https://avito.ru/item/2",
        item_price="от 1500 ₽",
        **kw,
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def шпион_постановки(monkeypatch) -> list[dict[str, Any]]:
    """Перехват `enqueue_address_ask` на месте вызова: в шпионе — своя сессия
    и проверка, что транзакция приёма уже закрыта."""
    вызовы: list[dict[str, Any]] = []

    async def spy(redis_, *, conversation_id, message_id, defer_sec, attempt=0):
        вызовы.append(
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "defer_sec": defer_sec,
                "attempt": attempt,
            }
        )
        return True

    monkeypatch.setattr(address_ask, "enqueue_address_ask", spy)
    return вызовы


async def test_постановка_из_inbound_после_commit(
    db, redis, account, db_sessionmaker, включено, monkeypatch
):
    """Шпион видит входящее своей сессией и закрытую транзакцию приёма;
    задержка — из настроек (600)."""
    видел: dict[str, Any] = {}

    async def spy(redis_, *, conversation_id, message_id, defer_sec, attempt=0):
        видел["in_transaction"] = db.in_transaction()
        async with db_sessionmaker() as s:
            row = (
                await s.execute(select(Message).where(Message.id == message_id))
            ).scalar_one_or_none()
        видел["найдено"] = row is not None
        видел["defer_sec"] = defer_sec
        return True

    monkeypatch.setattr(address_ask, "enqueue_address_ask", spy)
    assert await apply_inbound_event(db, redis, account, событие("Экран разбит, почём?"))
    assert видел == {"in_transaction": False, "найдено": True, "defer_sec": ЗАДЕРЖКА}


@pytest.mark.parametrize(
    "случай",
    [
        "backfill",
        "выключено",
        "несвежее",
        "принят",
        "адрес_в_карточке",
        "чёрный_список",
        "bot_active",
    ],
)
async def test_ворота_постановки(
    db, redis, account, db_sessionmaker, включено, шпион_постановки, make_user, случай
):
    """Ворота `field_lock`/`card_lock` на постановке: иначе с задержкой 600 с
    очередь держала бы десятки отложенных задач и `geo_repair` молчал бы."""
    kwargs: dict[str, Any] = {}
    текст = "Экран разбит, почём?"
    when = None
    if случай == "backfill":
        kwargs = {"backfill": True, "publish": False}
    elif случай == "выключено":
        await настроить(db_sessionmaker, {app_settings.ADDRESS_ASK_ENABLED: False})
    elif случай == "несвежее":
        when = datetime.now(UTC) - timedelta(minutes=20)
    else:
        # Диалог заводится первым сообщением, потом правится, потом второе.
        assert await apply_inbound_event(db, redis, account, событие("Здравствуйте"))
        шпион_постановки.clear()
        async with db_sessionmaker() as s:
            conv = (await s.execute(select(Conversation))).scalar_one()
            if случай == "принят":
                человек = await make_user("op2@example.com")
                conv.claimed_by_id = человек.id
            elif случай == "bot_active":
                conv.bot_active = True
            elif случай == "адрес_в_карточке":
                card = await s.get(Client, conv.client_id)
                assert card is not None
                card.address = "ул. Ленина, 5"
            elif случай == "чёрный_список":
                card = await s.get(Client, conv.client_id)
                assert card is not None
                card.blocked_at = datetime.now(UTC)
            await s.commit()
    assert await apply_inbound_event(
        db, redis, account, событие(текст, msg="am-2", when=when), **kwargs
    )
    assert шпион_постановки == [], f"{случай}: задача поставлена мимо ворот"


async def test_реплика_с_адресом_задачу_ставит_а_решает_карта(
    db, redis, account, db_sessionmaker, включено, шпион_постановки
):
    """Ревью 19.09: «Хаер 75» заводит строку, карта её отвергнет — вопрос
    нужен; настоящий адрес к сроку задачи станет замком `candidate_exists`
    (см. candidate_lock). Ворота постановки по найденному адресу сняты."""
    assert await apply_inbound_event(
        db, redis, account, событие("Приезжайте на ул. Ленина 5, экран разбит")
    )
    assert len(шпион_постановки) == 1


async def test_постановка_до_раннего_выхода_блока_бота(
    db, redis, db_sessionmaker, включено, шпион_постановки, make_avito_account
):
    """Серия с auto-ботом: вторая реплика в окне серии даёт `debounced` и ранний
    `return True` в блоке бота — постановка стоит ДО него, иначе терялась бы на
    каждой серии. Первая реплика — задача есть; вторая, пока тик бота ещё не
    прошёл (`bot_active` False) — тоже есть, и в журнале `bot.debounced`; третья,
    когда бот уже ведёт, — ворота `field_lock`, задачи нет.

    ДИВЕРСИЯ: перенести `enqueue_address_ask` ниже блока бота — на второй
    реплике шпион останется с одним вызовом.
    """
    account = await make_avito_account(AVITO_USER_ID)
    await бот_на_аккаунте(db_sessionmaker, account.id, mode="auto")
    # Приём читает `account.bot_id` с переданного объекта: перечитать после
    # привязки, иначе бот не просыпается вовсе и серия не схлопывается.
    async with db_sessionmaker() as s:
        account = await s.get(AvitoAccount, account.id)
        assert account is not None and account.bot_id is not None
    assert await apply_inbound_event(db, redis, account, событие("Экран разбит, почём?"))
    assert len(шпион_постановки) == 1

    with structlog.testing.capture_logs() as логи:
        assert await apply_inbound_event(db, redis, account, событие("и зарядка", msg="am-2"))
    assert any(л["event"] == "bot.debounced" for л in логи), "серия не схлопнулась — стенд не тот"
    assert len(шпион_постановки) == 2, "постановка потерялась за ранним return блока бота"

    async with db_sessionmaker() as s:
        conv = (await s.execute(select(Conversation))).scalar_one()
        conv.bot_active = True
        await s.commit()
    assert await apply_inbound_event(db, redis, account, событие("и корпус", msg="am-3"))
    assert len(шпион_постановки) == 2


# ------------------------------------------- 20–21: регистрация и реестр


def test_задача_зарегистрирована_в_воркере():
    from app.workers.main import registered_job_names

    assert address_ask.ADDRESS_ASK_JOB in registered_job_names()


_ЗАМКИ_СЕРВИСА = {"field_lock", "card_lock", "candidate_lock"}


def _причины_из_кода() -> set[str]:
    """Все литералы из `_skip("…")`/`skip("…")`/`повтор("…")` задачи и из
    `field_lock`/`card_lock`/`candidate_lock` сервиса — по дереву разбора, как
    в test_audit; имя-константа в `return` (`GEO_PENDING_LOCK`) разворачивается
    через сам модуль."""
    найдено: set[str] = set()
    for путь in (
        КОРЕНЬ / "app" / "workers" / "address_ask.py",
        КОРЕНЬ / "app" / "services" / "address_ask.py",
    ):
        дерево = ast.parse(путь.read_text(encoding="utf-8"), filename=str(путь))
        for node in ast.walk(дерево):
            if isinstance(node, ast.Call):
                имя = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if имя in {"_skip", "skip", "повтор"} and node.args:
                    первый = node.args[0]
                    if isinstance(первый, ast.Constant) and isinstance(первый.value, str):
                        найдено.add(первый.value)
            if isinstance(node, ast.FunctionDef) and node.name in _ЗАМКИ_СЕРВИСА:
                for под in ast.walk(node):
                    if isinstance(под, ast.Return) and под.value is not None:
                        найдено |= {
                            c.value
                            for c in ast.walk(под.value)
                            if isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                            and c.value.replace("_", "").isalpha()
                        }
                        найдено |= {
                            getattr(address_ask, n.id)
                            for n in ast.walk(под.value)
                            if isinstance(n, ast.Name)
                            and n.id.isupper()
                            and isinstance(getattr(address_ask, n.id, None), str)
                        }
    return найдено


def test_каждая_причина_есть_в_реестре_замков():
    причины = _причины_из_кода()
    assert причины, "причин не найдено вовсе — сторож ослеп"
    assert {"client_blocked", "geo_pending", "candidate_exists"} <= причины, (
        "сторож не видит сервис"
    )
    assert причины - set(worker.ЗАМКИ) == set(), "замок без подписи для людей"


# ------------------------------------------ 22–23: ответ клиента и эхо


async def test_ответ_клиента_на_вопрос_системы_поднимается_адресом(сид, db, redis, db_sessionmaker):
    """«ленина 5» после вопроса системы → строка адреса (уровень B через
    `_оператор_спросил_адрес`); диверсия: без вопроса строки нет."""
    conv = await диалог(db_sessionmaker, сид.conversation_id)
    ответ = InboundEvent(
        external_chat_id=conv.external_chat_id,
        external_message_id="am-answer",
        author_id=999001,
        account_user_id=сид.account.avito_user_id,
        text="ленина 5",
        created_at=datetime.now(UTC) + timedelta(seconds=5),
        client_name="Иван Петров",
        item_title="Ремонт телефона",
        item_url="https://avito.ru/item/3",
        item_price="от 1500 ₽",
    )
    assert await apply_inbound_event(db, redis, сид.account, ответ)
    async with db_sessionmaker() as s:
        assert (await s.execute(select(ClientAddressCandidate))).scalars().all() == []

    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    ответ2 = InboundEvent(
        external_chat_id=conv.external_chat_id,
        external_message_id="am-answer-2",
        author_id=999001,
        account_user_id=сид.account.avito_user_id,
        text="ленина 5",
        created_at=datetime.now(UTC) + timedelta(seconds=5),
        client_name="Иван Петров",
        item_title="Ремонт телефона",
        item_url="https://avito.ru/item/3",
        item_price="от 1500 ₽",
    )
    assert await apply_inbound_event(db, redis, сид.account, ответ2)
    async with db_sessionmaker() as s:
        строки = (await s.execute(select(ClientAddressCandidate))).scalars().all()
    assert len(строки) == 1
    assert строки[0].street and строки[0].house == "5"


async def test_эхо_своего_вопроса_не_выключает_бота(сид, db, redis, db_sessionmaker):
    """Эхо с аккаунта тем же текстом в окне 2 мин отбрасывается, `external_message_id`
    дописывается; записи «Ответили из другого приложения» нет."""
    assert await прогон(сид, db_sessionmaker, redis) == "sent"
    [msg] = await вопросы_системы(db_sessionmaker, сид.conversation_id)
    conv = await диалог(db_sessionmaker, сид.conversation_id)
    эхо = InboundEvent(
        external_chat_id=conv.external_chat_id,
        external_message_id="am-echo",
        author_id=сид.account.avito_user_id,
        account_user_id=сид.account.avito_user_id,
        text=msg.body,
        created_at=datetime.now(UTC) + timedelta(seconds=3),
        client_name=None,
        item_title=None,
        item_url=None,
        item_price=None,
    )
    assert await apply_inbound_event(db, redis, сид.account, эхо) is False
    async with db_sessionmaker() as s:
        исходящие = (
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == сид.conversation_id, Message.direction == "out"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(исходящие) == 1
        assert исходящие[0].external_message_id == "am-echo"
        системные = (
            (
                await s.execute(
                    select(Message.body).where(
                        Message.conversation_id == сид.conversation_id,
                        Message.direction == "system",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert not any("Ответили из другого приложения" in (т or "") for т in системные)


# ------------------------------------------------ 24: слова и правило


def test_слова_и_правило_один_словарь():
    for слово in _ВОПРОС_ОБ_АДРЕСЕ_СЛОВА:
        assert _ВОПРОС_ОБ_АДРЕСЕ.search(слово), слово
    for контроль in ("диагностика", "цена", "спасибо"):
        assert not _ВОПРОС_ОБ_АДРЕСЕ.search(контроль), контроль
    слова = address_ask.recognizable_words()
    assert "этаж" in слова and "квартира" in слова
    assert not address_ask.text_is_recognizable("   ")


def test_настройки_читаются_одним_читателем():
    """`None`/мусор у чисел → умолчание Spec; `enabled` = AND с разбором адресов."""
    s = address_ask.settings_from(
        {
            app_settings.ADDRESS_ASK_ENABLED: True,
            app_settings.ADDRESS_DETECT_ENABLED: True,
            app_settings.ADDRESS_ASK_DELAY_SEC: None,
            app_settings.ADDRESS_ASK_MIN_CHARS: "мусор",
            app_settings.ADDRESS_ASK_TEXT: "",
        }
    )
    assert s.enabled and s.delay_sec == 600 and s.min_chars == 25
    assert s.text == app_settings.SPECS[app_settings.ADDRESS_ASK_TEXT].default
    assert address_ask.enqueue_delay_sec({app_settings.ADDRESS_ASK_ENABLED: True}) is None
