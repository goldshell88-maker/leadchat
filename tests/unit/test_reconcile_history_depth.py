"""Сверка после отмены отсечки по моменту подключения (docs/41 §11).

ЧТО ИЗМЕНИЛОСЬ. В сверке стояло `since = account.created_at`: переписку до
подключения канала не брали (решение 8 августа). Владелец 11 августа
потребовал обратное — «все диалоги, которые были и есть», — и для незнакомого
чата граница снята.

ЧЕМ ЭТО ОПАСНО И ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Сверка ходит каждый час и заводит
диалоги ЖИВЫМ путём: очередь, отметка ожидания, бот, кадры в сокет. Сними
границу наивно — и первый же незнакомый чат с прошлогодней перепиской
разошёлся бы тринадцати операторам во «Входящие», разбудил бота на реплике
годичной давности и испортил бы скорость первого ответа.

Поэтому момент подключения остался, но сменил работу: он больше не граница
ЗАГРУЗКИ, а граница ЖИВОГО ТРАФИКА. Ниже неё — история (архив, без очереди и
без бота), выше — работа. Тесты держат обе двери.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Conversation, Message
from app.services import inbox
from app.workers import reconciliation as mod

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 770300
CLIENT_UID = 999301
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


def _chat(chat_id: str = "c-1", *, last_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        external_chat_id=chat_id,
        has_unread=True,
        last_message_at=last_at,
        item_title="Ремонт холодильников",
        item_url=None,
        item_price=None,
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
    )


def _event(msg_id: str, created: datetime, *, chat_id: str = "c-1") -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat_id,
        external_message_id=msg_id,
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text=f"текст {msg_id}",
        created_at=created,
        client_name="Клиент",
    )


def _adapter(monkeypatch, chat: SimpleNamespace, history: list[InboundEvent]) -> list[Any]:
    """Фейк адаптера + журнал переданных отсечек `since`.

    Отсечку фейк ОБЯЗАН соблюдать: у боевого адаптера она внутри
    `fetch_history`, и именно она решает, что попадёт в базу.
    """
    seen_since: list[Any] = []

    class FakeAdapter:
        async def fetch_chats(self, _account, *, unread_only=True):
            yield chat

        async def fetch_history(self, _account, _chat, *, since=None):
            seen_since.append(since)
            for event in history:
                if since is not None and event.created_at <= mod._aware(since):
                    continue
                yield event

    monkeypatch.setattr(mod, "get_adapter", lambda _ctx: FakeAdapter())
    return seen_since


async def _conv(db_sessionmaker) -> Conversation:
    async with db_sessionmaker() as db:
        return (await db.execute(sa.select(Conversation))).scalars().one()


async def _bodies(db_sessionmaker) -> list[str]:
    async with db_sessionmaker() as db:
        rows = await db.execute(sa.select(Message.external_message_id).order_by(Message.created_at))
        return list(rows.scalars().all())


async def test_unknown_chat_is_taken_from_the_very_beginning(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """«Все диалоги, которые были и есть»: у незнакомого чата отсечки нет."""
    history = [
        _event("год-назад", NOW - timedelta(days=365)),
        _event("вчера", NOW - timedelta(days=1)),
    ]
    seen_since = _adapter(monkeypatch, _chat(), history)

    await mod.reconcile_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert seen_since == [None], "нижней границы у незнакомого чата быть не должно"
    assert await _bodies(db_sessionmaker) == ["год-назад", "вчера"]


async def test_history_below_the_connect_moment_goes_to_the_archive(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Прошлогоднее приезжает исторической дверью: архив, без очереди и без бота.

    Живой путь поставил бы диалог во «Входящие» тринадцати операторам,
    проставил бы отметку ожидания (и сторож начал бы напоминать про
    прошлогоднее) и разбудил бы бота.
    """
    history = [_event("старое", NOW - timedelta(days=200))]
    _adapter(monkeypatch, _chat(last_at=NOW - timedelta(days=200)), history)

    await mod.reconcile_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    conv = await _conv(db_sessionmaker)
    assert conv.status == "closed"
    assert conv.offered_at is None
    assert conv.awaiting_since is None, "ожидание с прошлого года — повод для ложных напоминаний"
    async with db_sessionmaker() as db:
        queue = await db.execute(sa.select(Conversation.id).where(inbox.queue_condition()))
        assert queue.scalars().all() == []


async def test_todays_missed_message_still_goes_the_live_way(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Пропавшее сегодняшнее — работа: очередь, ожидание, всё как было.

    Тот же чат, что и в прошлом тесте: сначала год истории, потом сегодняшнее
    сообщение. Первое обязано лечь в архив, второе — поднять диалог в очередь.
    """
    # Отсчёт от МОМЕНТА ПОДКЛЮЧЕНИЯ канала, а не от времени импорта модуля:
    # граница живого трафика — это `account.created_at`, и в длинном прогоне
    # набора он оказывается заметно позже, чем NOW.
    connected = mod._aware(account.created_at)
    history = [
        _event("старое", connected - timedelta(days=200)),
        _event("сегодняшнее", connected + timedelta(minutes=1)),
    ]
    _adapter(monkeypatch, _chat(), history)

    result = await mod.reconcile_account(
        {"db_session_factory": db_sessionmaker, "redis": redis}, account.id
    )

    assert result["history_imported"] == 1
    assert result["messages_recovered"] == 1
    conv = await _conv(db_sessionmaker)
    assert conv.status == "new"
    assert conv.offered_at is not None
    async with db_sessionmaker() as db:
        queue = await db.execute(sa.select(Conversation.id).where(inbox.queue_condition()))
        assert queue.scalars().all() == [conv.id]


async def test_known_dialog_is_read_from_its_last_incoming(
    monkeypatch, db_sessionmaker, redis, account, seed_conversation
) -> None:
    """У знакомого диалога граница — его последнее входящее, а не начало времён.

    Иначе каждый час перечитывалась бы вся переписка каждого чата с
    непрочитанным: дублей не будет (дедуп по идентификатору сообщения), а
    работа будет.
    """
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        last_in = (
            await db.execute(
                sa.select(sa.func.max(Message.created_at)).where(
                    Message.conversation_id == conv.id, Message.direction == "in"
                )
            )
        ).scalar_one()
        external_chat_id = conv.external_chat_id

    chat = _chat(external_chat_id)
    seen_since = _adapter(monkeypatch, chat, [])

    await mod.reconcile_account(
        {"db_session_factory": db_sessionmaker, "redis": redis},
        seed_conversation.account.id,
    )

    assert seen_since == [mod._aware(last_in)]


async def test_partitions_are_ensured_for_every_imported_month(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Год истории через сверку — та же беда #24, та же защита.

    Раньше глубже момента подключения сверка не заходила, и промах по
    партициям был умозрительным. Теперь она берёт незнакомый чат целиком, и
    одно сообщение годовой давности снова способно уронить прогон — то есть
    сломать догон пропавших сообщений ровно там, где он нужен.
    """
    covered: list[tuple[int, int]] = []

    class RecordingCoverage:
        def __init__(self, *_a, **_kw) -> None:
            pass

        async def ensure(self, moment):
            if moment is not None:
                covered.append((moment.year, moment.month))

        async def ensure_all(self, moments):
            for moment in moments:
                await self.ensure(moment)

    monkeypatch.setattr(mod, "PartitionCoverage", RecordingCoverage)
    # ⚠ ШАГ ПО КАЛЕНДАРНЫМ МЕСЯЦАМ, А НЕ ПО 30 ДНЕЙ (28.08). Тринадцать шагов по
    # 30 дней покрывают 390 дней, но НЕ тринадцать месяцев: февраль короче шага,
    # а месяцы по 31 дню ловятся дважды. 28 августа 2026 набор дал одиннадцать
    # месяцев, и тест покраснел, ничего не сломав в продукте, — та же бомба с
    # часовым механизмом, что уже разбирали в ChannelCardFacts. Проверяется
    # свойство «под каждый месяц истории есть партиция», а не «13 произвольных
    # моментов», поэтому месяцы берём прямо.
    начало = NOW.replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    моменты = []
    год, месяц = начало.year, начало.month
    for _ in range(13):
        моменты.append(начало.replace(year=год, month=месяц))
        месяц -= 1
        if месяц == 0:
            месяц, год = 12, год - 1
    history = [_event(f"m{n}", момент) for n, момент in enumerate(моменты)]
    _adapter(monkeypatch, _chat(), history)

    await mod.reconcile_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert len(set(covered)) == 13, f"партиции обеспечены не под все месяцы: {sorted(set(covered))}"
