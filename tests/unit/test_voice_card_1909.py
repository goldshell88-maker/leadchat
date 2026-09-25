"""Голосовое → карточка: телефон и адрес из расшифровки поднимаются сами (19.09).

ЧТО ОХРАНЯЕТСЯ. Владелец 19.09: «не распознал номер из аудио». Замер боя за
30 дней: 708 расшифровок `done`, тело у всех пусто, строк адреса от голосовых —
0. Разрыв структурный: весь разбор карточки читал `msg.body`, а речь
голосового приезжает минуты спустя в `voice_transcript`, и после
`_записать_исход(ГОТОВО)` её не читал никто.

Механизм в двух частях, и файл разложен по ним:

* ОДИН ИСТОЧНИК РЕЧИ — `voice.speech_of` (Python, одна реплика) и
  `voice.speech_sql` (SQL, выборки соседей). Расходиться им нельзя: соседи
  читали бы одно, сама реплика — другое (класс dva-puti-raznyi-schet).
* ТРИГГЕР — `transcribe_voice` ставит отдельную задачу `voice_card_extract`
  строго ПОСЛЕ commit'а ГОТОВО; повтор расшифровки после ГОТОВО
  перепоставляет разбор (самопочинка окна «воркер умер между commit'ом и
  постановкой»).

⚠ КАЖДЫЙ СТОРОЖ НИЖЕ ПРОВЕРЕН ДИВЕРСИЕЙ — сломано ровно то, что он стережёт,
и он покраснел. Разбор каждой — в самом тесте.

Все номера вымышленные (`+7 900 111-22-44`). Фикстуры голосового — копия из
`test_voice_transcript_0509.py`, фабрика-журнал — по образцу
`test_voice_backfill_0609.py`, но журнал ОБЩИЙ для commit'ов и постановок:
без этого диверсия «поставить задачу до commit'а» осталась бы невидимой
(постановка идёт в Redis, фабрика сессий её не видит).

Вторая половина файла (6–15, 16, 18, 21–22) — сама задача `voice_card_extract`
(`workers/voice_card.py`), замок `transcribing` (`workers/address_ask.py`),
пункт и подсказка карте из голосовой реплики, контракт догона. Стенд задачи:
диалог с голосовым в базе, разбор без сети (карты и модель не зовутся —
проверяются только ПОСТАНОВКИ в fakeredis), кадры собираются подменой
`inbound.publish_event`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
from arq import Retry
from arq.connections import ArqRedis
from sqlalchemy.exc import OperationalError

from app.integrations import gateway, openrouter
from app.models import (
    AuditLog,
    Client,
    ClientAddressCandidate,
    ClientPhoneCandidate,
    Conversation,
    Message,
)
from app.services import address_ask, app_settings, inbound, phone_parse, voice
from app.services import clients as clients_svc
from app.services.address_ask import InboundRow
from app.services.messages import as_arq
from app.services.voice import Speech
from app.workers import address_ask as ask_worker
from app.workers import address_llm as llm_worker
from app.workers import cards_catchup as catchup_worker
from app.workers import geocode as geo_worker
from app.workers import transcribe
from app.workers import voice_card as card_worker

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 111222333
T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

#: Так голосовое приходит из разбора вебхука (`adapter._extract_attachments`).
ГОЛОСОВОЕ = [
    {
        "media_id": "avito_voice_2229d5a7",
        "kind": "file",
        "name": "Голосовое сообщение",
        "size": None,
        "avito_type": "voice",
    }
]

#: Форма владельца 19.09 — как Whisper пишет продиктованный номер: цифрами,
#: с дефисами, точкой на паузе. Номер вымышленный.
РЕЧЬ_С_НОМЕРОМ = "Здравствуйте, вот мой номер 900-111-22-44. Если хотите, позвоните."
#: Адрес голосом — с пунктуацией Whisper на паузах.
РЕЧЬ_С_АДРЕСОМ = "Здравствуйте, адрес улица Ленина, дом 5, квартира 3. Приезжайте после обеда."


# --- стенд: голосовое в базе, Авито отдаёт запись, модель не считает ----------


class ФальшивоеРаспознавание:
    """Модель, которая ничего не считает: отдаёт заданный текст и длительность.
    Качество распознавания проверено замером, здесь — что мы делаем с текстом."""

    def __init__(self, текст: str = РЕЧЬ_С_НОМЕРОМ, длительность: float = 19.4) -> None:
        self.текст = текст
        self.длительность = длительность
        self.звали = 0

    def transcribe(self, путь: str, **kw: Any) -> tuple[Any, Any]:
        self.звали += 1
        return iter([SimpleNamespace(text=" " + self.текст)]), SimpleNamespace(
            duration=self.длительность
        )


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def авито_отдаёт_запись(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Ссылка от Авито и сама запись по ней — без единого похода в сеть."""
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    состояние: dict[str, Any] = {"спрашивали": 0}

    async def ссылки(self: Any, token: Any, user_id: Any, voice_ids: Any) -> dict[str, str]:
        состояние["спрашивали"] += 1
        return {voice_ids[0]: f"https://cdn.avito.example/{voice_ids[0]}.opus"}

    async def скачать(url: str, куда: str) -> int:
        with open(куда, "wb") as f:
            f.write(b"OggS" + b"\x00" * 4096)
        return 4100

    monkeypatch.setattr(AvitoClient, "get_voice_urls", ссылки)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    monkeypatch.setattr(transcribe, "_скачать", скачать)
    return состояние


@pytest.fixture(autouse=True)
def свой_замок_на_тест(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий замок очереди на каждый тест: `asyncio.Lock` привязывается к
    циклу событий при первом ожидании, а у каждого теста свой цикл."""
    monkeypatch.setattr(transcribe, "_замок", asyncio.Lock())
    monkeypatch.setattr(transcribe, "_желающих", 0)


@pytest.fixture(autouse=True)
def whisper_включён(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "whisper_enabled", True)


async def _голосовое_в_базе(
    db_sessionmaker: Any, account: Any, **kw: Any
) -> tuple[uuid.UUID, datetime]:
    async with db_sessionmaker() as db:
        client_row = Client(
            channel="avito", external_id=f"9990{uuid.uuid4().int % 10**6:06d}", name="Клиент"
        )
        db.add(client_row)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            last_message_at=T0,
        )
        db.add(conv)
        await db.flush()
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id="am-voice-1",
            direction="in",
            sender_type="client",
            body=None,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=T0,
            **kw,
        )
        db.add(msg)
        await db.commit()
        return msg.id, msg.created_at


def _ctx(factory: Any, redis: Any, *, попытка: int = 1) -> dict[str, Any]:
    return {"db_session_factory": factory, "redis": redis, "job_try": попытка}


async def _состояние(db_sessionmaker: Any, message_id: uuid.UUID) -> tuple[Any, Any]:
    async with db_sessionmaker() as db:
        msg = (await db.execute(sa.select(Message).where(Message.id == message_id))).scalar_one()
        return msg.voice_transcript, msg.voice_transcript_status


async def задачи_разбора(redis: Any) -> list[str]:
    """Ключи поставленных задач разбора карточки (дедуп по ``_job_id``)."""
    return sorted(k for k in await redis.keys("arq:job:voicecard:*"))


def _фабрика_с_журналом(db_sessionmaker: Any, журнал: list[str]) -> Any:
    """Фабрика сессий, записывающая каждый commit в общий журнал."""

    def фабрика() -> Any:
        сессия = db_sessionmaker()
        настоящий_commit = сессия.commit

        async def commit() -> None:
            await настоящий_commit()
            журнал.append("commit")

        сессия.commit = commit
        return сессия

    return фабрика


@pytest.fixture
def журнал(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ОБЩИЙ журнал: «commit» пишет фабрика, «publish» — кадр, «enqueue» —
    постановка разбора (настоящая постановка при этом идёт в fakeredis)."""
    записи: list[str] = []
    настоящая_постановка = voice.enqueue_voice_card

    async def публикация(redis: Any, **поля: Any) -> None:
        записи.append("publish")

    async def постановка(redis: Any, message_id: uuid.UUID, created_at: datetime) -> bool:
        записи.append("enqueue")
        return await настоящая_постановка(redis, message_id, created_at)

    monkeypatch.setattr(transcribe, "publish_transcript", публикация)
    monkeypatch.setattr(voice, "enqueue_voice_card", постановка)
    return записи


# =============================================================================
# 1–2. Один источник речи: `speech_of` и его SQL-двойник
# =============================================================================


def test_speech_of_тело_иначе_расшифровка_готово() -> None:
    """⚠ ДИВЕРСИЯ: вернуть расшифровку без проверки состояния — краснеет на
    `running`: у него текста в колонке нет по контракту `_записать_исход`, и
    проверка держит контракт явно, а не надеется на него.
    """
    # Тело побеждает: написанное человеком надёжнее машинного.
    assert voice.speech_of("привет", РЕЧЬ_С_НОМЕРОМ, "done") == Speech("привет", False)
    # Расшифровка — только при ГОТОВО, и с пометкой «сказано, не написано».
    assert voice.speech_of(None, РЕЧЬ_С_НОМЕРОМ, "done") == Speech(РЕЧЬ_С_НОМЕРОМ, True)
    for состояние in ("running", "failed", "too_long", None):
        assert voice.speech_of(None, РЕЧЬ_С_НОМЕРОМ, состояние) == Speech(None, False), состояние
    # Пробельное тело — не тело; пустая расшифровка — не речь.
    assert voice.speech_of("   ", РЕЧЬ_С_НОМЕРОМ, "done") == Speech(РЕЧЬ_С_НОМЕРОМ, True)
    assert voice.speech_of(None, "   ", "done") == Speech(None, False)
    assert voice.speech_of("", None, None) == Speech(None, False)
    # Текст отдаётся КАК ЕСТЬ: смещения `find_all` и подсказка `hint_word`
    # ищут по одной и той же строке.
    assert voice.speech_of(" y", "z", "done").text == " y"
    assert voice.speech_of(None, "x ", "done").text == "x "


async def test_speech_sql_совпадает_с_speech_of(seed_conversation: Any, db: Any) -> None:
    """Реплики во всех состояниях И две пробельные (тело «  » + `done`; `done`
    с пустой расшифровкой) — по каждой `select(speech_sql())` равен
    `speech_of(...).text`.

    ⚠ ДИВЕРСИЯ 1: убрать в `speech_sql` ветвь по состоянию — краснеет строка
    `running`. ДИВЕРСИЯ 2: убрать `trim` — краснеет строка с пробельным телом
    (SQL отдал бы «  », Python — расшифровку). Без двух пробельных строк в
    таблице ниже вторая диверсия осталась бы зелёной (скептик 19.09, возр. 8).
    """
    строки: list[tuple[str | None, str | None, str | None]] = [
        ("привет", None, None),
        ("привет", РЕЧЬ_С_НОМЕРОМ, "done"),
        (None, РЕЧЬ_С_НОМЕРОМ, "done"),
        (None, РЕЧЬ_С_НОМЕРОМ, "running"),
        (None, РЕЧЬ_С_НОМЕРОМ, "failed"),
        (None, РЕЧЬ_С_НОМЕРОМ, "too_long"),
        (None, РЕЧЬ_С_НОМЕРОМ, None),
        ("  ", "x ", "done"),
        (" y", "z", "done"),
        (None, "", "done"),
        (None, "   ", "done"),
        (None, None, "done"),
        (None, None, None),
    ]
    ожидание = [
        "привет", "привет", РЕЧЬ_С_НОМЕРОМ, None, None, None, None,
        "x ", " y", None, None, None, None,
    ]  # fmt: skip
    for i, (тело, расшифровка, состояние) in enumerate(строки):
        db.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                external_message_id=f"sp-{i}",
                direction="in",
                sender_type="client",
                body=тело,
                attachments=list(ГОЛОСОВОЕ) if тело is None else [],
                delivery_status="delivered",
                created_at=T0 + timedelta(seconds=i),
                voice_transcript=расшифровка,
                voice_transcript_status=состояние,
            )
        )
    await db.commit()

    выбрано = (
        await db.execute(
            sa.select(
                Message.body,
                Message.voice_transcript,
                Message.voice_transcript_status,
                voice.speech_sql().label("речь"),
            )
            .where(Message.external_message_id.like("sp-%"))
            .order_by(Message.created_at)
        )
    ).all()
    assert len(выбрано) == len(строки)
    for (тело, расшифровка, состояние, речь_sql), ожидаемая in zip(выбрано, ожидание, strict=True):
        речь_py = voice.speech_of(тело, расшифровка, состояние).text
        assert речь_sql == речь_py == ожидаемая, (тело, расшифровка, состояние)


# =============================================================================
# 3–5. Триггер: постановка разбора после commit'а ГОТОВО, и только тогда
# =============================================================================


async def test_готово_ставит_voicecard_после_commit(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    журнал: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ДИВЕРСИЯ: перенести `enqueue_voice_card` на строку ПЕРЕД
    `_записать_исход` в фазе 3 — «enqueue» встаёт между первым и вторым
    commit'ом, индекс меньше, тест краснеет. Разбор до commit'а прочитал бы ещё
    пустую колонку — ровно та слепота, которую он лечит. Журнал фабрики один
    этого не видит: постановка идёт в Redis (скептик 19.09, возр. 7).
    """
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: ФальшивоеРаспознавание())
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(
        _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis), message_id, created_at
    )

    assert журнал.count("enqueue") == 1, журнал
    assert журнал.index("enqueue") > журнал.index("commit", 1), (
        f"разбор поставлен до commit'а фазы 3: {журнал}"
    )
    assert await задачи_разбора(redis) == [f"arq:job:voicecard:{message_id}"]
    assert (await _состояние(db_sessionmaker, message_id)) == (
        РЕЧЬ_С_НОМЕРОМ,
        voice.СОСТОЯНИЕ_ГОТОВО,
    )


@pytest.mark.parametrize("исход", ["failed", "too_long", "queue_null"])
async def test_не_готово_voicecard_не_ставит(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    журнал: list[str],
    monkeypatch: pytest.MonkeyPatch,
    исход: str,
) -> None:
    """`failed`, `too_long`, «замок не дождался» (NULL) — разбирать нечего:
    текста в колонке нет. ⚠ ДИВЕРСИЯ: ставить разбор безусловно (или по
    любому `_записать_исход`) — краснеют все три.
    """
    from app.core.config import settings
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    попытка = 1
    if исход == "failed":

        async def пусто(self: Any, *_a: Any, **_kw: Any) -> dict[str, str]:
            return {}

        monkeypatch.setattr(AvitoClient, "get_voice_urls", пусто)
        ожидаемое: str | None = voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    elif исход == "too_long":

        async def ссылки(self: Any, token: Any, user_id: Any, voice_ids: Any) -> dict[str, str]:
            return {voice_ids[0]: "https://cdn.avito.example/x.opus"}

        async def скачать(url: str, куда: str) -> int:
            with open(куда, "wb") as f:
                f.write(b"OggS" + b"\x00" * 64)
            return 68

        monkeypatch.setattr(AvitoClient, "get_voice_urls", ссылки)
        monkeypatch.setattr(transcribe, "_скачать", скачать)
        monkeypatch.setattr(
            transcribe, "_загрузить_модель", lambda: ФальшивоеРаспознавание(длительность=1800.0)
        )
        ожидаемое = voice.СОСТОЯНИЕ_СЛИШКОМ_ДЛИННАЯ
    else:
        # Соседняя запись «считается», ждать не хотим, попытки кончились →
        # исход «не начинали» (NULL): досчёт возьмёт сам, разбирать нечего.
        monkeypatch.setattr(transcribe, "ЖДАТЬ_ОЧЕРЕДЬ_СЕК", 0.01)
        await transcribe._замок.acquire()
        попытка = settings.whisper_max_tries
        ожидаемое = None

    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)
    try:
        await transcribe.transcribe_voice(
            _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis, попытка=попытка),
            message_id,
            created_at,
        )
    finally:
        if исход == "queue_null":
            transcribe._замок.release()

    assert (await _состояние(db_sessionmaker, message_id))[1] == ожидаемое
    assert "enqueue" not in журнал, журнал
    assert await задачи_разбора(redis) == []


async def test_повтор_transcribe_после_готово_перепоставляет_разбор(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Воркер умер между commit'ом ГОТОВО и постановкой разбора; ARQ повторяет
    расшифровку — фаза 1 видит `done`, в Авито не ходит и ставит разбор сама.
    Без этого расшифровка есть, а номер в карточку не попал бы никогда.

    ⚠ ДИВЕРСИЯ 1: убрать ветку перепостановки в фазе 1 — краснеет на ключе.
    ДИВЕРСИЯ 2: перепоставлять на любое из ЗАКОНЧЕННЫХ — краснеет вторая
    половина: у `too_long` текста нет, разбирать нечего.
    """
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    готовое, когда = await _голосовое_в_базе(
        db_sessionmaker,
        account,
        voice_transcript=РЕЧЬ_С_НОМЕРОМ,
        voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
    )
    длинное, когда_длинное = await _голосовое_в_базе(
        db_sessionmaker, account, voice_transcript_status=voice.СОСТОЯНИЕ_СЛИШКОМ_ДЛИННАЯ
    )

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), готовое, когда)
    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), длинное, когда_длинное)

    assert авито_отдаёт_запись["спрашивали"] == 0 and модель.звали == 0, "готовое не пересчитываем"
    assert await задачи_разбора(redis) == [f"arq:job:voicecard:{готовое}"]
    assert (await _состояние(db_sessionmaker, готовое))[0] == РЕЧЬ_С_НОМЕРОМ, "текст не перетёрт"


# =============================================================================
# 6–15. Сама задача `voice_card_extract` (`workers/voice_card.py`)
# =============================================================================


async def _диалог_с_голосовым(
    db_sessionmaker: Any,
    account: Any,
    *,
    речь: str | None = РЕЧЬ_С_НОМЕРОМ,
    когда: datetime | None = None,
    состояние: str | None = voice.СОСТОЯНИЕ_ГОТОВО,
    тело: str | None = None,
    status: str = "new",
) -> SimpleNamespace:
    """Диалог с одним голосовым клиента; по умолчанию — свежее (5 минут назад),
    расшифровка ГОТОВО. Возвращает ключи диалога, карточки и сообщения."""
    когда = когда or datetime.now(UTC) - timedelta(minutes=5)
    async with db_sessionmaker() as db:
        client_row = Client(
            channel="avito", external_id=f"9991{uuid.uuid4().int % 10**6:06d}", name="Клиент"
        )
        db.add(client_row)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status=status,
            last_message_at=когда,
        )
        db.add(conv)
        await db.flush()
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id=f"am-voice-{uuid.uuid4().hex[:6]}",
            direction="in",
            sender_type="client",
            body=тело,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=когда,
            voice_transcript=речь,
            voice_transcript_status=состояние,
        )
        db.add(msg)
        await db.commit()
        return SimpleNamespace(
            conversation_id=conv.id,
            client_id=client_row.id,
            message_id=msg.id,
            created_at=когда,
        )


async def _реплика(
    db_sessionmaker: Any,
    диалог: SimpleNamespace,
    *,
    тело: str | None,
    когда: datetime,
    direction: str = "in",
    sender_type: str = "client",
    attachments: list[Any] | None = None,
    речь: str | None = None,
    состояние: str | None = None,
) -> uuid.UUID:
    """Ещё одна реплика того же диалога (текст клиента, реплика оператора или
    второе голосовое)."""
    async with db_sessionmaker() as db:
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=диалог.conversation_id,
            external_message_id=f"am-{uuid.uuid4().hex[:6]}",
            direction=direction,
            sender_type=sender_type,
            body=тело,
            attachments=attachments or [],
            delivery_status="delivered",
            created_at=когда,
            voice_transcript=речь,
            voice_transcript_status=состояние,
        )
        db.add(msg)
        await db.commit()
        return msg.id


async def _разобрать(db_sessionmaker: Any, redis: Any, диалог: SimpleNamespace) -> str:
    return await card_worker.voice_card_extract(
        _ctx(db_sessionmaker, redis), диалог.message_id, диалог.created_at
    )


async def _номера(db_sessionmaker: Any, client_id: uuid.UUID) -> list[ClientPhoneCandidate]:
    async with db_sessionmaker() as db:
        return list(
            (
                await db.execute(
                    sa.select(ClientPhoneCandidate)
                    .where(ClientPhoneCandidate.client_id == client_id)
                    .order_by(ClientPhoneCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )


async def _адреса(db_sessionmaker: Any, client_id: uuid.UUID) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as db:
        return list(
            (
                await db.execute(
                    sa.select(ClientAddressCandidate)
                    .where(ClientAddressCandidate.client_id == client_id)
                    .order_by(ClientAddressCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )


async def _карточка(db_sessionmaker: Any, client_id: uuid.UUID) -> Client:
    async with db_sessionmaker() as db:
        client = await db.get(Client, client_id)
        assert client is not None
        return client


async def _аудит(db_sessionmaker: Any, action: str) -> int:
    async with db_sessionmaker() as db:
        return int(
            (
                await db.execute(
                    sa.select(sa.func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == action)
                )
            ).scalar_one()
        )


async def _ключи(redis: Any, приставка: str) -> list[str]:
    return sorted(await redis.keys(f"arq:job:{приставка}*"))


async def _включить(db_sessionmaker: Any, значения: dict[str, Any]) -> None:
    async with db_sessionmaker() as db:
        await app_settings.set_many(db, значения, user_id=None)
        await db.commit()


@pytest.fixture
def кадры(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Кадры в сокет — списком: единственный писатель кадра карточки живёт в
    `inbound` (`_известить_о_клиенте`), задача голоса зовёт его же."""
    записи: list[tuple[str, dict[str, Any]]] = []

    async def публикация(_redis: Any, kind: str, payload: dict[str, Any], **_kw: Any) -> None:
        записи.append((kind, payload))

    monkeypatch.setattr(inbound, "publish_event", публикация)
    return записи


@pytest.fixture
def модель_с_ключом(monkeypatch: pytest.MonkeyPatch) -> None:
    """Читатель с ключом по снимку шлюза — иначе ворота модели закрыты."""
    assert gateway.known_keys is not None
    for имя in openrouter.READERS:
        monkeypatch.setitem(gateway.known_keys, имя, True)


async def test_номер_из_расшифровки_ложится_строкой_голосового(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """Случай владельца 19.09: продиктованный номер — строкой карточки.

    Строка несёт `message_id` ГОЛОСОВОГО (критерий боя §9.3), `raw` — как
    сказано, `source='voice'`, `detected_at` — время реплики (не «сейчас»:
    иначе сторожа давности считали бы месячную запись свежей), `pending` при
    выключенной автозаписи; кадр `client:updated` с `phone_suggested` — один.

    ⚠ ДИВЕРСИЯ 1: в `_maybe_extract_phone` вернуть `find_all(msg.body)` —
    строки нет. ДИВЕРСИЯ 2: не прокладывать `source` в `absorb_phones` —
    краснеет на `source`.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account)

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (строка,) = await _номера(db_sessionmaker, голос.client_id)
    assert (строка.phone, строка.raw, строка.source, строка.status) == (
        "+79001112244",
        "900-111-22-44",
        "voice",
        "pending",
    )
    assert строка.message_id == голос.message_id
    assert строка.conversation_id == голос.conversation_id
    assert строка.detected_at.replace(tzinfo=UTC) == голос.created_at
    assert (await _карточка(db_sessionmaker, голос.client_id)).phone is None
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "phone_suggested")]
    assert кадры[0][1]["client_id"] == str(голос.client_id)


async def test_автозапись_основного_из_голоса_по_тем_же_правилам(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """`PHONE_DETECT_AUTOFILL=True` → пустой основной заполняется номером из
    голоса (правила 12.09 те же, что у текста), аудит `client.phone_captured`
    один; подпись под основным — «из голосового» (`identity_view.phone_source
    == "voice"`), не «из диалога». Второе голосовое с другим номером
    заполненный основной не меняет никогда — ложится дополнительным, и в
    списке номеров подписано `voice`.

    ⚠ ДИВЕРСИЯ: убрать ветвь `"voice"` в `_phone_source` (или в `запись()`) —
    подпись становится `"dialog"`, тест краснеет: автозаполненный основной из
    ослышки выглядел бы написанным руками клиента.
    """
    await _включить(db_sessionmaker, {app_settings.PHONE_DETECT_AUTOFILL: True})
    голос = await _диалог_с_голосовым(db_sessionmaker, account)

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    карточка = await _карточка(db_sessionmaker, голос.client_id)
    assert карточка.phone == "+79001112244"
    assert карточка.phone_set_at is None, "автоматика, не человек"
    assert await _аудит(db_sessionmaker, "client.phone_captured") == 1
    async with db_sessionmaker() as db:
        вид = await clients_svc.identity_view(db, await db.get(Client, голос.client_id))
    assert вид["phone_source"] == "voice"
    assert [(p["value"], p["primary"], p["source"]) for p in вид["phones"]] == [
        ("+79001112244", True, "voice")
    ]
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "phone_captured")]

    # Второе голосовое — другой номер: основной не трогаем, дополнительный.
    второе = SimpleNamespace(
        conversation_id=голос.conversation_id,
        client_id=голос.client_id,
        message_id=await _реплика(
            db_sessionmaker,
            голос,
            тело=None,
            когда=голос.created_at + timedelta(minutes=1),
            attachments=list(ГОЛОСОВОЕ),
            речь="а ещё жена, 900-111-22-55, если я не отвечу",
            состояние=voice.СОСТОЯНИЕ_ГОТОВО,
        ),
        created_at=голос.created_at + timedelta(minutes=1),
    )
    assert await _разобрать(db_sessionmaker, redis, второе) == "done"
    assert (await _карточка(db_sessionmaker, голос.client_id)).phone == "+79001112244"
    async with db_sessionmaker() as db:
        вид = await clients_svc.identity_view(db, await db.get(Client, голос.client_id))
    assert [(p["value"], p["primary"], p["source"], p["hint"]) for p in вид["phones"]] == [
        ("+79001112244", True, "voice", None),
        # Подсказка «жена» найдена по ТОЙ ЖЕ строке расшифровки, что и номер.
        ("+79001112255", False, "voice", "жена"),
    ]
    assert await _аудит(db_sessionmaker, "client.phone_captured") == 1


async def test_повтор_задачи_ничего_не_меняет(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """ARQ доставляет at-least-once, фаза 1 расшифровки перепоставляет разбор:
    второй прогон — те же строки, тот же аудит, тот же основной. Объединение
    двойников из задачи НЕ ставится (v2): пару найдёт ночной `merge_backlog`
    под своими сторожами (имена, аккаунты, доказательства).

    ⚠ ДИВЕРСИЯ: позвать `enqueue_merge` из задачи при `phone_captured` —
    краснеет на ключах `merge:*`.
    """
    await _включить(db_sessionmaker, {app_settings.PHONE_DETECT_AUTOFILL: True})
    голос = await _диалог_с_голосовым(db_sessionmaker, account)

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"
    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    assert len(await _номера(db_sessionmaker, голос.client_id)) == 1
    assert await _аудит(db_sessionmaker, "client.phone_captured") == 1
    assert (await _карточка(db_sessionmaker, голос.client_id)).phone == "+79001112244"
    assert await _ключи(redis, "merge:") == []
    # Второго кадра нет: номер уже основной (`ALREADY_PRIMARY`) — причины нет.
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "phone_captured")]


async def test_адрес_из_расшифровки_строка_и_карта_сразу(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any], monkeypatch: Any
) -> None:
    """Адрес голосом → строка A `source='voice'` с частями; у СВЕЖЕГО голосового
    карта ставится сразу (`geocode:{cid}`), как у текста, — и строго после
    commit'а: задача, поставленная раньше, прибежала бы к строке, которой в
    базе нет (класс «кадр до commit'а»).

    ⚠ ДИВЕРСИЯ 1: сторож в `replay_card_extraction` обратно на `msg.body` —
    строки нет. ДИВЕРСИЯ 2: своя постановка карты внутри сессии сразу после
    разбора, до commit'а реплики — в общем журнале после «enqueue_geocode»
    остаётся «commit» (и постановок становится две). ДИВЕРСИЯ 3:
    `replay_conversation` не копит `geocode_ids` — ключа `geocode:` нет.
    """
    журнал: list[str] = []
    настоящая = card_worker.enqueue_geocode

    async def постановка(redis_: Any, candidate_id: Any, **kw: Any) -> bool:
        журнал.append("enqueue_geocode")
        return await настоящая(redis_, candidate_id, **kw)

    monkeypatch.setattr(card_worker, "enqueue_geocode", постановка)
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь=РЕЧЬ_С_АДРЕСОМ)

    assert (
        await card_worker.voice_card_extract(
            _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis),
            голос.message_id,
            голос.created_at,
        )
        == "done"
    )

    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.street, строка.house, строка.office, строка.level, строка.source) == (
        "улица Ленина",
        "5",
        "3",
        "A",
        "voice",
    )
    assert строка.message_id == голос.message_id and строка.geo_status == "pending"
    assert await _ключи(redis, "geocode:") == [f"arq:job:geocode:{строка.id}"]
    assert журнал.count("enqueue_geocode") == 1
    assert "commit" not in журнал[журнал.index("enqueue_geocode") :], (
        f"карта поставлена до commit'а: {журнал}"
    )
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "address_suggested")]


async def test_кв_текстом_после_голосового_дописывается(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """Пока запись считалась, клиент дописал «кв 7» текстом: живой путь строки
    ещё не нашёл и часть потерял. Задача разбирает ХВОСТ (от голосового до конца
    переписки), и «кв 7» ложится в строку от голоса.

    ⚠ ДИВЕРСИЯ: в задаче `before=когда` (одна реплика вместо хвоста) — офис
    остаётся 3.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь=РЕЧЬ_С_АДРЕСОМ)
    await _реплика(
        db_sessionmaker, голос, тело="кв 7", когда=голос.created_at + timedelta(minutes=1)
    )

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.value, строка.office) == ("улица Ленина, 5", "7")


async def test_поздняя_часть_не_перебивается_ранним_голосом(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """Обратный порядок частей: текст T−1 «Ленина 5» (строка), текст T+1 «кв 7»
    уже применён живым путём (`office=7`), голосовое T «квартира 3» ГОТОВО.
    Разбор T перепишет офис на 3 (`refine_address_parts` сторожа времени не
    имеет), хвост T+1 вернёт 7 — итог верный ТОЛЬКО благодаря хвосту
    (скептик 19.09, возр. 7).

    ⚠ ДИВЕРСИЯ: разбирать одну реплику — `office == "3"`.
    """
    T = datetime.now(UTC) - timedelta(minutes=5)
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь="квартира 3", когда=T)
    раньше = await _реплика(
        db_sessionmaker, голос, тело="ул. Ленина 5", когда=T - timedelta(minutes=1)
    )
    позже = await _реплика(db_sessionmaker, голос, тело="кв 7", когда=T + timedelta(minutes=1))
    # Живой путь уже разобрал обе текстовые реплики — той же обёрткой.
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, голос.conversation_id)
        client = await db.get(Client, голос.client_id)
        for mid in (раньше, позже):
            msg = (await db.execute(sa.select(Message).where(Message.id == mid))).scalar_one()
            await inbound.replay_card_extraction(
                db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
            )
            await db.commit()
    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert строка.office == "7", "предпосылка: часть T+1 уже в строке"

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.value, строка.office) == ("ул. Ленина, 5", "7")


@pytest.mark.parametrize("кв7_в_окне", [False, True], ids=["без_кв7", "кв7_в_окне"])
async def test_старый_голос_не_перебивает_часть_за_окном_суток(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any], кв7_в_окне: bool
) -> None:
    """Голосовое расшифровано с опозданием (досчёт `voice_repair`): T = сейчас − 30 ч,
    «квартира 3». Текст T−1 мин «ул. Ленина 5» (строка) и «кв 9» T+25 ч уже
    разобраны живым путём (`office=9`); во втором случае между ними ещё «кв 7»
    T+1 мин. Разбор T перепишет офис на 3, и вернуть 9 обязан хвост — поэтому он
    до конца переписки, а не сутки: с потолком `когда + 24 ч` «кв 9» осталось бы
    за окном, и итог был бы 3 (или 7 — от «кв 7» из окна). Ревью 19.09, C1/C3.

    ⚠ ДИВЕРСИЯ: в задаче `before=когда + inbound._ОКНО_ВОПРОСА` — office "3"
    без «кв 7» и "7" с ним; оба варианта краснеют.
    """
    T = datetime.now(UTC) - timedelta(hours=30)
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь="квартира 3", когда=T)
    тексты = [("ул. Ленина 5", T - timedelta(minutes=1))]
    if кв7_в_окне:
        тексты.append(("кв 7", T + timedelta(minutes=1)))
    тексты.append(("кв 9", T + timedelta(hours=25)))
    ids = [await _реплика(db_sessionmaker, голос, тело=т, когда=к) for т, к in тексты]
    # Живой путь уже разобрал все текстовые реплики — той же обёрткой.
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, голос.conversation_id)
        client = await db.get(Client, голос.client_id)
        for mid in ids:
            msg = (await db.execute(sa.select(Message).where(Message.id == mid))).scalar_one()
            await inbound.replay_card_extraction(
                db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
            )
            await db.commit()
    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert строка.office == "9", "предпосылка: поздняя часть уже в строке"

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.value, строка.office) == ("ул. Ленина, 5", "9")


async def test_сбой_соединения_в_хвосте_просит_retry(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any], monkeypatch: Any
) -> None:
    """Обрыв соединения ВНУТРИ разбора самого голосового (там и идут все
    запросы): догон не глотает его как «сбой одной реплики» — задача просит
    `Retry`, а не возвращает `done`/`empty` без строки. Иначе номер из
    расшифровки терялся бы молча до ручного `backfill-cards`, а `max_tries=3`
    не значил бы ничего (ревью 19.09, C2/C4).

    ⚠ ДИВЕРСИЯ: убрать `except (OperationalError, InterfaceError): raise` в
    `replay_conversation` — реплика уходит в `message_failed`, задача выходит
    на "empty", `pytest.raises(Retry)` краснеет.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account)

    async def обрыв(db: Any, conv: Any, client: Any, msg: Any, **kw: Any) -> str | None:
        raise OperationalError("SELECT 1", None, ConnectionResetError("обрыв"))

    monkeypatch.setattr(inbound, "_maybe_extract_phone", обрыв)

    with structlog.testing.capture_logs() as логи, pytest.raises(Retry):
        await _разобрать(db_sessionmaker, redis, голос)

    assert [л["event"] for л in логи if л["event"].startswith("cards_catchup.")] == [], (
        "сбой соединения — не «сбой одной реплики»"
    )
    (предупреждение,) = [л for л in логи if л["event"] == "voice_card.db_unavailable"]
    assert предупреждение["error"] == "OperationalError"
    assert await _номера(db_sessionmaker, голос.client_id) == []
    assert кадры == []


async def test_дом_голосом_к_месту_из_текста(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """«пгт Северный, мкр Центральный» текстом, «дом 17» — голосом (владелец
    13.09: адрес постепенно): `_дом_к_месту` читает речь голосового, и дом
    клеится к месту из прошлой реплики.

    ⚠ ДИВЕРСИЯ: в `_дом_к_месту` обратно `msg.body or ""` — строки дома нет.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь="дом 17")
    место = await _реплика(
        db_sessionmaker,
        голос,
        тело="пгт Северный, мкр Центральный",
        когда=голос.created_at - timedelta(minutes=2),
    )
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, голос.conversation_id)
        client = await db.get(Client, голос.client_id)
        msg = (await db.execute(sa.select(Message).where(Message.id == место))).scalar_one()
        итог = await inbound.replay_card_extraction(
            db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
        )
        await db.commit()
    assert итог.address_reason == "address_suggested", "предпосылка: место заведено"

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    дома = [r for r in await _адреса(db_sessionmaker, голос.client_id) if r.kind == "house"]
    assert [(r.house, r.settlement, r.source, r.message_id) for r in дома] == [
        ("17", "Северный", "voice", голос.message_id)
    ]
    assert дома[0].raw.endswith("; дом 17")


async def test_модель_читателю_только_свежему_и_когда_правила_молчат(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any], модель_с_ключом: None
) -> None:
    """«Пушки на 10» голосом после «куда выезжать?» оператора: правила молчат,
    ворота модели (`llm_read_wanted`, одни с живым путём) открыты → `llm-addr:{id}`.
    То же у голосового 3-дневной давности — не ставится (семантика догона);
    правила нашли адрес — не ставится.

    ⚠ ДИВЕРСИЯ: в `llm_read_wanted` убрать условие `правила_промолчали` (или
    `причина_адреса is None`) — третий случай краснеет: модель звалась бы и на
    разобранный адрес.
    """

    async def случай(*, речь: str, назад: timedelta) -> tuple[str, list[str]]:
        когда = datetime.now(UTC) - назад
        голос = await _диалог_с_голосовым(db_sessionmaker, account, речь=речь, когда=когда)
        await _реплика(
            db_sessionmaker,
            голос,
            тело="Подскажите, куда к вам подъехать?",
            когда=когда - timedelta(minutes=2),
            direction="out",
            sender_type="user",
        )
        итог = await _разобрать(db_sessionmaker, redis, голос)
        return итог, [k for k in await _ключи(redis, "llm-addr:") if str(голос.message_id) in k]

    итог, задачи = await случай(речь="Пушки на 10", назад=timedelta(minutes=5))
    assert (итог, len(задачи)) == ("done", 1), "свежее, правила молчат — модель зовём"

    итог, задачи = await случай(речь="Пушки на 10", назад=timedelta(days=3))
    assert (итог, задачи) == ("done", []), "старому — семантика догона, модели нет"

    итог, задачи = await случай(речь=РЕЧЬ_С_АДРЕСОМ, назад=timedelta(minutes=5))
    assert (итог, задачи) == ("done", []), "правила нашли адрес — модели нечего читать"


async def test_старому_голосовому_карту_сразу_не_ставим(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """Голосовое 20-дневной давности (досчёт `voice_repair`): строка есть,
    `geocode:*` пусто — карту сделает `geo_repair` в своём темпе, а не залп
    порции досчёта в DaData.

    ⚠ ДИВЕРСИЯ: убрать условие `свежее` перед постановкой карты — краснеет.
    """
    голос = await _диалог_с_голосовым(
        db_sessionmaker,
        account,
        речь=РЕЧЬ_С_АДРЕСОМ,
        когда=datetime.now(UTC) - timedelta(days=20),
    )

    assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.source, строка.geo_status) == ("voice", "pending")
    assert await _ключи(redis, "geocode:") == []
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "address_suggested")]


async def test_сбой_реплики_хвоста_не_роняет_кадр(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any], monkeypatch: Any
) -> None:
    """Хвост из двух реплик: голосовое с номером и текст, на котором разбор
    падает. Сбой изолирован догоном (`cards_catchup.message_failed` с id
    текстовой), а задача ДОХОДИТ до кадра: простые значения сняты до хвоста.

    ⚠ ДИВЕРСИЯ: в задаче читать `msg.conversation_id` ПОСЛЕ
    `replay_conversation` — после rollback + expunge_all объект протух и
    отвязан, чтение падает DetachedInstanceError; кадра нет, тест краснеет.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account)
    текст = await _реплика(
        db_sessionmaker,
        голос,
        тело="сломай меня",
        когда=голос.created_at + timedelta(minutes=1),
    )
    исходная = inbound._maybe_extract_phone

    async def падает(db: Any, conv: Any, client: Any, msg: Any, **kw: Any) -> str | None:
        if msg.id == текст:
            raise RuntimeError("разбор упал")
        return await исходная(db, conv, client, msg, **kw)

    monkeypatch.setattr(inbound, "_maybe_extract_phone", падает)

    with structlog.testing.capture_logs() as логи:
        assert await _разобрать(db_sessionmaker, redis, голос) == "done"

    (сбой,) = [л for л in логи if л["event"] == "cards_catchup.message_failed"]
    assert сбой["message_id"] == str(текст)
    (строка,) = await _номера(db_sessionmaker, голос.client_id)
    assert строка.message_id == голос.message_id
    assert [(k, p["reason"]) for k, p in кадры] == [("client:updated", "phone_suggested")]
    (итог,) = [л for л in логи if л["event"] == "voice_card.done"]
    assert (итог["tail_messages"], итог["tail_phones"]) == (1, 1)


async def test_историческая_дверь_читает_расшифровки(
    db_sessionmaker: Any, redis: Any, account: Any, monkeypatch: Any, кадры: list[Any]
) -> None:
    """Догон истории (N29) идёт той же обёрткой: у голосового из истории
    расшифровка уже ГОТОВО (досчёт успел) → после `cardcatch` строка номера И
    строка адреса — обе `source='voice'`.

    ⚠ ДИВЕРСИЯ: сторож в `replay_card_extraction` обратно на `msg.body` —
    строки адреса нет (телефон сторожа не проходит, поэтому здесь оба).
    """
    from tests.unit.test_paket2_history_1909 import _догрузить

    T = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    chat_id = f"chat-hist-{uuid.uuid4().hex[:6]}"

    def голосовое(msg_id: str, created: datetime) -> dict[str, Any]:
        return {
            "id": msg_id,
            "author_id": 999901,
            "created": int(created.timestamp()),
            "type": "voice",
            "content": {"voice": {"voice_id": msg_id}},
        }

    await _догрузить(
        monkeypatch,
        db_sessionmaker,
        redis,
        account,
        chat_id,
        [голосовое("v-phone", T), голосовое("v-addr", T + timedelta(minutes=1))],
    )
    async with db_sessionmaker() as db:
        conv = (
            await db.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == chat_id)
            )
        ).scalar_one()
        for ext, речь in (("v-phone", РЕЧЬ_С_НОМЕРОМ), ("v-addr", РЕЧЬ_С_АДРЕСОМ)):
            msg = (
                await db.execute(
                    sa.select(Message).where(
                        Message.conversation_id == conv.id, Message.external_message_id == ext
                    )
                )
            ).scalar_one()
            assert msg.body is None and voice.has_voice(msg.attachments), "историческая дверь"
            msg.voice_transcript, msg.voice_transcript_status = речь, voice.СОСТОЯНИЕ_ГОТОВО
        await db.commit()
        client_id = conv.client_id

    assert (
        await catchup_worker.catchup_conversation_cards(
            _ctx(db_sessionmaker, redis), conv.id, T.isoformat()
        )
        == "done"
    )

    (номер,) = await _номера(db_sessionmaker, client_id)
    (адрес,) = await _адреса(db_sessionmaker, client_id)
    assert (номер.phone, номер.source) == ("+79001112244", "voice")
    assert (адрес.value, адрес.office, адрес.source) == ("улица Ленина, 5", "3", "voice")


async def test_backfill_cards_читает_расшифровки(
    db_sessionmaker: Any, redis: Any, account: Any, capsys: Any
) -> None:
    """`backfill-cards --no-dry-run` (догон владельца после выкатки) на
    голосовом ГОТОВО заводит строку номера; печать `с_номером=1` — счётчик
    читает речь, а не тело (иначе печатал бы 0 при заведённой строке).

    ⚠ ДИВЕРСИЯ: счётчик `с_номером` обратно по `msg.body` — краснеет печать.
    """
    from app.cli import run_backfill_cards

    голос = await _диалог_с_голосовым(db_sessionmaker, account)
    async with db_sessionmaker() as db:
        await run_backfill_cards(db, days=7, dry_run=False)

    (строка,) = await _номера(db_sessionmaker, голос.client_id)
    assert (строка.phone, строка.source, строка.message_id) == (
        "+79001112244",
        "voice",
        голос.message_id,
    )
    печать = capsys.readouterr().out
    assert "сообщений=1" in печать and "с_номером=1" in печать, печать


async def test_тело_побеждает_расшифровку_в_задаче(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """Реплика с телом «привет» и расшифровкой с номером: задача видит
    `spoken=False` → `"no_speech"`, строк нет, кадра нет — тело разобрал живой
    путь. Без расшифровки (`failed`) — то же."""
    с_телом = await _диалог_с_голосовым(db_sessionmaker, account, тело="привет")
    assert await _разобрать(db_sessionmaker, redis, с_телом) == "no_speech"
    не_вышло = await _диалог_с_голосовым(
        db_sessionmaker, account, состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    )
    assert await _разобрать(db_sessionmaker, redis, не_вышло) == "no_speech"
    assert await _номера(db_sessionmaker, с_телом.client_id) == []
    assert await _номера(db_sessionmaker, не_вышло.client_id) == []
    assert кадры == []


# =============================================================================
# 16. Замок `transcribing` в `workers/address_ask.py` и его предпосылка:
#     имена задач голоса рождаются в одном месте
# =============================================================================


async def _стенд_вопроса(
    db_sessionmaker: Any, seed_conversation: Any, *, голос_назад: int = 610
) -> SimpleNamespace:
    """Стенд задачи вопроса (как `сид` в test_address_ask_1809): вопрос включён,
    входящее сида 620 с назад; плюс голосовое клиента `голос_назад` секунд назад
    без расшифровки (NULL — «не начинали»)."""
    await _включить(db_sessionmaker, {app_settings.ADDRESS_ASK_ENABLED: True})
    сейчас = datetime.now(UTC)
    async with db_sessionmaker() as db:
        await db.execute(
            sa.update(Message)
            .where(Message.id == seed_conversation.message_id)
            .values(created_at=сейчас - timedelta(seconds=620))
        )
        голос = Message(
            id=uuid.uuid4(),
            conversation_id=seed_conversation.conversation_id,
            external_message_id="am-voice-ask",
            direction="in",
            sender_type="client",
            body=None,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=сейчас - timedelta(seconds=голос_назад),
        )
        db.add(голос)
        await db.commit()
        return SimpleNamespace(
            conversation_id=seed_conversation.conversation_id,
            message_id=seed_conversation.message_id,
            voice_id=голос.id,
        )


async def _спросить(сид: Any, db_sessionmaker: Any, redis: Any, *, attempt: int = 0) -> str:
    return await ask_worker.address_ask_run(
        _ctx(db_sessionmaker, redis),
        conversation_id=str(сид.conversation_id),
        message_id=str(сид.message_id),
        attempt=attempt,
    )


async def _вопросы(db_sessionmaker: Any, conversation_id: uuid.UUID) -> int:
    async with db_sessionmaker() as db:
        return len(
            (
                await db.execute(
                    sa.select(Message.id).where(
                        Message.conversation_id == conversation_id,
                        Message.direction == "out",
                        Message.sender_type == "system",
                    )
                )
            ).all()
        )


async def test_вопрос_об_адресе_ждёт_расшифровку_в_полёте(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """На 600-й секунде `client_described` считает голосовое описанием — без
    замка вопрос ушёл бы тому, кто только что наговорил адрес. Замок — по
    задачам ARQ В ПОЛЁТЕ (`voice:{id}`, `voice:{id}:repair`, `voicecard:{id}`),
    не по колонке: `voice:{id}` в очереди → `retry`; разбор `voicecard:{id}` в
    полёте → `retry`; задач нет и состояние NULL → вопрос идёт дальше (`sent`);
    `failed` без задач — не держит. `"transcribing"` — в реестре замков.

    ⚠ ДИВЕРСИЯ: замок по колонке (`status in (NULL, running)`) вместо очереди —
    краснеет случай «NULL без задач → sent»: исторический NULL (никто не ставил,
    Whisper выключен) держал бы вопрос до `RETRY_MAX` и глушил его.
    """
    assert "transcribing" in ask_worker.ЗАМКИ
    сид = await _стенд_вопроса(db_sessionmaker, seed_conversation)
    живое, ремонтное = voice.transcribe_job_ids(сид.voice_id)
    разбор = voice.VOICE_CARD_JOB_ID.format(сид.voice_id)

    await ArqRedis.enqueue_job(as_arq(redis), voice.TRANSCRIBE_JOB, _job_id=живое)
    with structlog.testing.capture_logs() as логи:
        assert await _спросить(сид, db_sessionmaker, redis) == "retry"
    assert [л["reason"] for л in логи if л["event"] == "address_ask.retry"] == ["transcribing"]
    assert await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r1")

    # Расшифровка закончилась (`keep_result=0` — следа нет), разбор в полёте.
    await redis.delete(f"arq:job:{живое}")
    await redis.zrem("arq:queue", живое)
    assert (
        await ask_worker._голос_считается(redis, [InboundRow(сид.voice_id, None, ГОЛОСОВОЕ, None)])
        is False
    )
    await redis.set(f"arq:in-progress:{разбор}", "1")
    assert await _спросить(сид, db_sessionmaker, redis, attempt=1) == "retry"
    await redis.delete(f"arq:in-progress:{разбор}")

    # Ремонтная постановка досчёта — тоже замок.
    await ArqRedis.enqueue_job(as_arq(redis), voice.TRANSCRIBE_JOB, _job_id=ремонтное)
    assert await ask_worker._голос_считается(
        redis, [InboundRow(сид.voice_id, None, ГОЛОСОВОЕ, None)]
    )
    await redis.delete(f"arq:job:{ремонтное}")
    await redis.zrem("arq:queue", ремонтное)

    # Задач нет, состояние в базе NULL — не держим: вопрос уходит.
    assert await _вопросы(db_sessionmaker, сид.conversation_id) == 0
    assert await _спросить(сид, db_sessionmaker, redis, attempt=2) == "sent"
    assert await _вопросы(db_sessionmaker, сид.conversation_id) == 1

    # `failed` без задач и текстовая реплика — замок молчит (по имени задачи, не по колонке).
    assert (
        await ask_worker._голос_считается(
            redis, [InboundRow(uuid.uuid4(), None, ГОЛОСОВОЕ, None, "failed")]
        )
        is False
    )
    assert (
        await ask_worker._голос_считается(redis, [InboundRow(uuid.uuid4(), "Ленина 5", [], None)])
        is False
    )


async def test_transcribing_после_retry_max_это_skip(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Попытки кончились, расшифровка всё ещё в полёте (порция досчёта впереди)
    — честный `skip("transcribing")`: вопрос НЕ уходит, как у `history_loading`.
    Лучше не спросить у того, чья расшифровка ещё в работе, чем спросить у
    назвавшего адрес. Это и счётчик для наблюдения (§9.4).

    ⚠ ДИВЕРСИЯ: после лимита отправлять — краснеет на `sent` и на строке вопроса.
    """
    сид = await _стенд_вопроса(db_sessionmaker, seed_conversation)
    живое, _ = voice.transcribe_job_ids(сид.voice_id)
    await ArqRedis.enqueue_job(as_arq(redis), voice.TRANSCRIBE_JOB, _job_id=живое)

    with structlog.testing.capture_logs() as логи:
        assert (
            await _спросить(сид, db_sessionmaker, redis, attempt=ask_worker.RETRY_MAX)
            == "transcribing"
        )

    (пропуск,) = [л for л in логи if л["event"] == "address_ask.skipped"]
    assert пропуск["reason"] == "transcribing"
    assert await _вопросы(db_sessionmaker, сид.conversation_id) == 0
    assert not await redis.exists(f"arq:job:addr-ask:{сид.message_id}:r{ask_worker.RETRY_MAX + 1}")


async def test_имена_задач_голоса_рождаются_в_одном_месте(redis: Any) -> None:
    """Замок `transcribing` спрашивает у ARQ «в полёте ли расшифровка или её
    разбор» по именам из `voice.transcribe_job_ids` и `voice.VOICE_CARD_JOB_ID`.
    Имена, которые постановщики кладут в Redis на самом деле, обязаны быть
    ТЕМИ ЖЕ — иначе замок молчал бы, и вопрос об адресе ушёл бы тому, кто
    только что наговорил адрес.

    ⚠ ДИВЕРСИЯ: в `enqueue_voice_card` собрать `_job_id` руками f-строкой с
    другим видом (или в `enqueue_transcribe` вернуть прежние литералы с
    опечаткой) — краснеет на наборе ключей.
    """
    ид = uuid.uuid4()
    assert await voice.enqueue_transcribe(redis, ид, T0)
    assert await voice.enqueue_transcribe(redis, ид, T0, повтор=True)
    assert await voice.enqueue_voice_card(redis, ид, T0)

    живое, ремонтное = voice.transcribe_job_ids(ид)
    ожидаемые = {живое, ремонтное, voice.VOICE_CARD_JOB_ID.format(ид)}
    assert {k.removeprefix("arq:job:") for k in await redis.keys("arq:job:*")} == ожидаемые
    # Дедупликация по имени: повтор той же постановки — не вторая задача.
    assert not await voice.enqueue_voice_card(redis, ид, T0)
    assert len(await задачи_разбора(redis)) == 1


# =============================================================================
# 17. Отказ, сказанный голосом, глушит вопрос об адресе
# =============================================================================


def test_отказ_голосом_глушит_вопрос() -> None:
    """⚠ ДИВЕРСИЯ: в `client_declined` читать `voice_transcript` без состояния
    — краснеет на `running`: текста там по контракту нет, и брать его значило
    бы верить недописанной колонке.
    """
    голос = InboundRow(
        id=uuid.uuid4(),
        body=None,
        attachments=[{"avito_type": "voice"}],
        voice_transcript="спасибо, не надо",
        voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
    )
    assert address_ask.client_declined([голос]) is True
    assert address_ask.client_declined([replace(голос, voice_transcript_status="running")]) is False
    assert address_ask.client_declined([replace(голос, voice_transcript_status=None)]) is False
    # Тело побеждает: написанное «мне нужно» главнее машинного «не надо».
    assert address_ask.client_declined([replace(голос, body="Мне нужно починить экран")]) is False
    # Старые вызовы без поля состояния живут как раньше — по телу.
    assert address_ask.client_declined(
        [
            InboundRow(
                id=uuid.uuid4(), body="Спасибо, не нужно", attachments=[], voice_transcript=None
            )
        ]
    )


async def test_inbound_rows_несут_состояние_расшифровки(seed_conversation: Any, db: Any) -> None:
    """Проводка от таблицы к чистой функции: без чтения состояния в
    `inbound_rows` поле осталось бы умолчанием None, и отказ голосом не
    гасил бы вопрос в задаче — при зелёном тесте чистой функции.
    ⚠ ДИВЕРСИЯ: убрать `voice_transcript_status` из выборки — краснеет.
    """
    db.add(
        Message(
            conversation_id=seed_conversation.conversation_id,
            external_message_id="am-voice-decline",
            direction="in",
            sender_type="client",
            body=None,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=datetime.now(UTC),
            voice_transcript="спасибо, не надо",
            voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
        )
    )
    await db.commit()
    строки = await address_ask.inbound_rows(
        db,
        seed_conversation.conversation_id,
        since=datetime.now(UTC) - timedelta(hours=1),
        before=datetime.now(UTC) + timedelta(minutes=1),
    )
    голос = next(r for r in строки if r.voice_transcript is not None)
    assert голос.voice_transcript_status == voice.СОСТОЯНИЕ_ГОТОВО
    assert address_ask.client_declined(строки) is True


# =============================================================================
# 18. Пункт из голосовой реплики (`inbound._пункт_из_соседних_реплик`)
#     и сторож автозаписи по речи голосового
# =============================================================================


async def test_пункт_из_голосовой_реплики(
    db_sessionmaker: Any, redis: Any, account: Any, кадры: list[Any]
) -> None:
    """«Куда к вам подъехать?» → «это посёлок Ударник» голосом (ГОТОВО) →
    «Ленина 5» текстом: пункт из соседней реплики дописывается к дому — строка
    с `settlement` Ударник (реплика-место годится в ответ на вопрос оператора,
    как у текста); `running` соседа пунктом не считается.

    ⚠ ДИВЕРСИЯ: выборка `_пункт_из_соседних_реплик` обратно по `Message.body`
    — `settlement is None`.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь="это посёлок Ударник")
    await _реплика(
        db_sessionmaker,
        голос,
        тело="Куда к вам подъехать?",
        когда=голос.created_at - timedelta(minutes=1),
        direction="out",
        sender_type="user",
    )
    текст = await _реплика(
        db_sessionmaker, голос, тело="Ленина 5", когда=голос.created_at + timedelta(minutes=1)
    )
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, голос.conversation_id)
        client = await db.get(Client, голос.client_id)
        msg = (await db.execute(sa.select(Message).where(Message.id == текст))).scalar_one()
        итог = await inbound.replay_card_extraction(
            db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
        )
        await db.commit()
    assert итог.address_reason == "address_suggested"
    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert (строка.street, строка.house, строка.settlement, строка.settlement_type) == (
        "Ленина",
        "5",
        "Ударник",
        "посёлок",
    )
    assert строка.raw == "посёлок Ударник; Ленина 5"

    # Тот же сосед, но расшифровка ещё считается — пункта нет.
    другой = await _диалог_с_голосовым(
        db_sessionmaker, account, речь="это посёлок Ударник", состояние=voice.СОСТОЯНИЕ_В_РАБОТЕ
    )
    await _реплика(
        db_sessionmaker,
        другой,
        тело="Куда к вам подъехать?",
        когда=другой.created_at - timedelta(minutes=1),
        direction="out",
        sender_type="user",
    )
    текст = await _реплика(
        db_sessionmaker, другой, тело="Ленина 5", когда=другой.created_at + timedelta(minutes=1)
    )
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, другой.conversation_id)
        client = await db.get(Client, другой.client_id)
        msg = (await db.execute(sa.select(Message).where(Message.id == текст))).scalar_one()
        await inbound.replay_card_extraction(
            db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
        )
        await db.commit()
    (строка,) = await _адреса(db_sessionmaker, другой.client_id)
    assert строка.settlement is None


async def test_сторож_автозаписи_судит_речь_голосового(db_sessionmaker: Any, account: Any) -> None:
    """`разбор_реплики_сейчас`/`реплика_без_адреса` (сторож автозаписи и догон
    `address-reparse`) читают речь, как и разбор: строка от голосового судится
    по той же расшифровке, из которой родилась, — не по пустому телу. Речи нет
    вовсе (расшифровки нет) — судить нечего, как и раньше.

    ⚠ ДИВЕРСИЯ: в `реплика_без_адреса` обратно `msg.body` — первый случай
    краснеет (пустое тело → «судить нечего» → False).
    """
    без_адреса = await _диалог_с_голосовым(db_sessionmaker, account, речь="сделать 130")
    с_адресом = await _диалог_с_голосовым(db_sessionmaker, account, речь=РЕЧЬ_С_АДРЕСОМ)
    без_речи = await _диалог_с_голосовым(db_sessionmaker, account, речь=None, состояние=None)
    async with db_sessionmaker() as db:
        for диалог, ждём_found, ждём_без in (
            (без_адреса, False, True),
            (с_адресом, True, False),
            (без_речи, False, False),
        ):
            conv = await db.get(Conversation, диалог.conversation_id)
            msg = (
                await db.execute(sa.select(Message).where(Message.id == диалог.message_id))
            ).scalar_one()
            found = await inbound.разбор_реплики_сейчас(db, conv, msg, client_id=диалог.client_id)
            assert (found is not None) is ждём_found, диалог
            assert inbound.реплика_без_адреса(msg, found) is ждём_без, диалог


# =============================================================================
# 19. Модель-читатель видит расшифровку
# =============================================================================


async def test_контекст_модели_включает_расшифровку(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Постановка на голосовое (из разбора или после отказа карты) без этого
    отдавала бы модели ПУСТУЮ реплику. Речь ГОТОВО попадает в сообщение
    пользователя — и соседняя (пункт назван голосом раньше), и сама читаемая;
    `running` — нет; сторож цитаты (`to_found`) тоже читает расшифровку —
    иначе улица из неё считалась бы выдуманной.

    ⚠ ДИВЕРСИЯ 1: вернуть в выборке контекста `select(Message.body)` —
    краснеет на соседней голосовой («Ударник» не доехал). ДИВЕРСИЯ 2: вернуть
    и `msg.body` для самой реплики — краснеет на адресе (реплика пуста).
    ДИВЕРСИЯ 3: в `speech_sql` брать расшифровку без состояния — краснеет на
    строке `running`. Сама реплика попадает в выборку контекста и без второй
    правки (окно `created_at <= msg.created_at` включает её), поэтому диверсия
    2 видна только вместе с первой — это не пробел сторожа, а запас в коде.
    """
    assert gateway.known_keys is not None  # снимок шлюза задаёт autouse-фикстура
    for имя in openrouter.READERS:
        monkeypatch.setitem(gateway.known_keys, имя, True)
    сейчас = datetime.now(UTC)
    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                external_message_id="am-voice-settlement",
                direction="in",
                sender_type="client",
                body=None,
                attachments=list(ГОЛОСОВОЕ),
                delivery_status="delivered",
                created_at=сейчас - timedelta(minutes=5),
                voice_transcript="мы в посёлке Ударник",
                voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
            )
        )
        s.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                external_message_id="am-voice-running",
                direction="in",
                sender_type="client",
                body=None,
                attachments=list(ГОЛОСОВОЕ),
                delivery_status="delivered",
                created_at=сейчас - timedelta(minutes=3),
                voice_transcript="ещё считается, читать нельзя",
                voice_transcript_status=voice.СОСТОЯНИЕ_В_РАБОТЕ,
            )
        )
        голос = Message(
            conversation_id=seed_conversation.conversation_id,
            external_message_id="am-voice-address",
            direction="in",
            sender_type="client",
            body=None,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=сейчас - timedelta(minutes=1),
            voice_transcript=РЕЧЬ_С_АДРЕСОМ,
            voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
        )
        s.add(голос)
        await s.commit()
        message_id = голос.id

    увидела: dict[str, str] = {}

    async def chat_json(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        увидела["user"] = user
        await kw["on_request"]()
        return {
            "street": "улица Ленина",
            "house": "5",
            "settlement": "",
            "settlement_type": "",
            "city": "",
            "apartment": "3",
            "entrance": "",
            "floor": "",
            "intercom": "",
            "confidence": "high",
            "note": "",
        }, "b/two:free"

    async def enqueue_geocode(redis_: Any, candidate_id: Any) -> None:
        pass

    monkeypatch.setattr(llm_worker.openrouter, "chat_json", chat_json)
    monkeypatch.setattr(llm_worker, "enqueue_geocode", enqueue_geocode)

    итог = await llm_worker.llm_address_read(
        _ctx(db_sessionmaker, redis),
        conversation_id=str(seed_conversation.conversation_id),
        message_id=str(message_id),
    )

    assert "улица Ленина, дом 5, квартира 3" in увидела["user"], увидела
    assert "посёлке Ударник" in увидела["user"], "соседняя голосовая реплика — тоже речь"
    assert "читать нельзя" not in увидела["user"], "`running` — не речь"
    assert итог == "recorded", "сторож цитаты обязан видеть ту же речь, что и модель"
    async with db_sessionmaker() as s:
        (row,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert (row.street, row.house, row.office, row.message_id) == (
        "улица Ленина",
        "5",
        "3",
        message_id,
    )


# =============================================================================
# 20. Тело побеждает расшифровку
# =============================================================================


def test_тело_побеждает_расшифровку() -> None:
    """Реплика с телом «привет» и расшифровкой с номером — речь это тело:
    разбор телефона по ней ничего не найдёт, и это верно (тело разобрал живой
    путь, расшифровку такой реплике Авито не присылает вовсе).

    V2 дописывает вторую половину на задаче: `voice_card_extract(...)` →
    `"no_speech"`, строк `client_phone_candidates` нет.
    """
    речь = voice.speech_of("привет", РЕЧЬ_С_НОМЕРОМ, voice.СОСТОЯНИЕ_ГОТОВО)
    assert речь == Speech("привет", False)
    assert phone_parse.find_all(речь.text) == []
    # А без тела тот же номер из речи разбирается — форма владельца 19.09.
    голос = voice.speech_of(None, РЕЧЬ_С_НОМЕРОМ, voice.СОСТОЯНИЕ_ГОТОВО)
    assert голос.spoken and [f.value for f in phone_parse.find_all(голос.text)] == ["+79001112244"]


# =============================================================================
# 21–22. Подсказка пункта карте (`workers/geocode._соседние_реплики`) и
#        контракт догона (`replay_card_extraction` / `_full`)
# =============================================================================


async def test_подсказка_пункта_карте_из_голосовой_реплики(
    db_sessionmaker: Any, account: Any
) -> None:
    """Строка от голоса «мы в Пашковке, Ковыльная 214»: `_соседние_реплики`
    первым элементом отдаёт РЕЧЬ (по телу было бы `None`), соседнее голосовое
    ГОТОВО видно, `running` — нет; исходящее оператора в соседи не попадает.

    ⚠ ДИВЕРСИЯ: `своё` обратно по `Message.body` — первый элемент `None`.
    """
    речь = "мы в Пашковке, Ковыльная 214"
    голос = await _диалог_с_голосовым(db_sessionmaker, account, речь=речь)
    await _реплика(
        db_sessionmaker,
        голос,
        тело=None,
        когда=голос.created_at - timedelta(minutes=3),
        attachments=list(ГОЛОСОВОЕ),
        речь="нам в Пашковку нужен мастер",
        состояние=voice.СОСТОЯНИЕ_ГОТОВО,
    )
    await _реплика(
        db_sessionmaker,
        голос,
        тело=None,
        когда=голос.created_at - timedelta(minutes=2),
        attachments=list(ГОЛОСОВОЕ),
        речь="ещё считается",
        состояние=voice.СОСТОЯНИЕ_В_РАБОТЕ,
    )
    await _реплика(
        db_sessionmaker,
        голос,
        тело="Куда подъехать?",
        когда=голос.created_at - timedelta(minutes=1),
        direction="out",
        sender_type="user",
    )
    async with db_sessionmaker() as db:
        строка = ClientAddressCandidate(
            client_id=голос.client_id,
            conversation_id=голос.conversation_id,
            message_id=голос.message_id,
            message_at=голос.created_at,
            value="Ковыльная, 214",
            street="Ковыльная",
            house="214",
            raw="Ковыльная 214",
            level="A",
            status="pending",
            kind="house",
            source="voice",
            geo_status="pending",
            detected_at=голос.created_at,
        )
        db.add(строка)
        await db.commit()
        соседи = await geo_worker._соседние_реплики(db, строка)
    assert соседи == [речь, None, "нам в Пашковку нужен мастер"]


async def test_contract_догона_не_изменился(
    db_sessionmaker: Any, account: Any, monkeypatch: Any
) -> None:
    """`replay_card_extraction(...) == ("phone_suggested", None, False)` — тот же
    трёхкортеж, что сравнивает `test_paket2_history_1909`; полная форма
    `replay_card_extraction_full(...).card` — он же, плюс следствия (`geocode_ids`
    строки на карту, `llm_read_wanted`). Полная форма идёт ЧЕРЕЗ трёхполевой шов:
    шпион на `replay_card_extraction` видит реплику и при вызове `_full`, а
    следствия при этом не теряются (иначе диверсии догона — сбой одной
    реплики, чужая запись между репликами — стали бы слепыми).

    ⚠ ДИВЕРСИЯ 1: вернуть `CardReplayFull` из старого имени — первое равенство
    краснеет (дата-класс кортежу не равен). ДИВЕРСИЯ 2: `_full` зовёт снимок
    функции (`_ШОВ = replay_card_extraction` на уровне модуля) вместо имени
    модуля — шпион не видит реплику.
    """
    голос = await _диалог_с_голосовым(db_sessionmaker, account)
    номер = await _реплика(
        db_sessionmaker, голос, тело="мой номер 900-111-22-44", когда=голос.created_at
    )
    адрес = await _реплика(
        db_sessionmaker,
        голос,
        тело="ул. Ленина 5",
        когда=голос.created_at + timedelta(seconds=30),
    )
    увидел: list[uuid.UUID] = []
    исходная = inbound.replay_card_extraction

    async def шпион(db: Any, conv: Any, client: Any, msg: Any, *, now: datetime) -> Any:
        увидел.append(msg.id)
        return await исходная(db, conv, client, msg, now=now)

    monkeypatch.setattr(inbound, "replay_card_extraction", шпион)
    async with db_sessionmaker() as db, app_settings.one_pass():
        conv = await db.get(Conversation, голос.conversation_id)
        client = await db.get(Client, голос.client_id)
        msg = (await db.execute(sa.select(Message).where(Message.id == номер))).scalar_one()
        когда = msg.created_at.replace(tzinfo=UTC)
        assert await inbound.replay_card_extraction(db, conv, client, msg, now=когда) == (
            "phone_suggested",
            None,
            False,
        )
        полный = await inbound.replay_card_extraction_full(db, conv, client, msg, now=когда)
        assert isinstance(полный, inbound.CardReplayFull)
        assert полный.card == ("phone_suggested", None, False)
        assert (полный.geocode_ids, полный.llm_read_wanted) == ((), False)

        msg = (await db.execute(sa.select(Message).where(Message.id == адрес))).scalar_one()
        полный = await inbound.replay_card_extraction_full(
            db, conv, client, msg, now=msg.created_at.replace(tzinfo=UTC)
        )
        await db.commit()
    assert полный.card == (None, "address_suggested", False)
    (строка,) = await _адреса(db_sessionmaker, голос.client_id)
    assert полный.geocode_ids == (строка.id,), "строка на карту — следствие полной формы"
    assert увидел == [номер, номер, адрес], "полная форма идёт через трёхполевой шов"
