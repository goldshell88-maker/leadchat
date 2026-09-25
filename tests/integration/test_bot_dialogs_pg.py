"""Экран «Диалоги бота» (docs/45) на настоящем PostgreSQL.

Здесь проверяется SQL, а не арифметика: отбирает ли фильтр ровно те диалоги,
где бот писал, и берётся ли исход по ПОСЛЕДНЕЙ записи журнала, а не по любой.

Второе — главное. Диалог может вернуться в очередь и бот вступит снова: тогда
записей о нём несколько. Отбор «есть хоть одна запись с такой причиной» показал
бы диалог в плитке позапрошлого захода — в списке «бот закрыл сам» стояли бы
диалоги, которые бот потом благополучно передал человеку. На глаз такую ошибку
не видно: список выглядит правдоподобно, просто он врёт.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AuditLog, AvitoAccount, Client, Conversation, Message
from app.scheduler.partitions import ensure_message_partitions
from app.services import bot_dialogs
from app.services import conversation_table as table
from tests.integration.conftest import requires_docker

pytestmark = requires_docker


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


ЧИСТКА = (
    "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
    "clients, avito_accounts, users CASCADE"
)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Чисто и ДО, и ПОСЛЕ — иначе страдает следующий файл, а не этот.

    ⚠ ПОЙМАНО ЗАСЛОНОМ ВЫКАТКИ 29.08. Уборка стояла только «до», и после
    последнего теста в базе оставались сообщения этого сценария. Файлы
    интеграционных тестов идут по алфавиту, `test_bot_dialogs_pg` — раньше
    `test_bots_api`, а тот чистит `DELETE FROM conversations` без каскада: он
    падал с нарушением внешнего ключа на строках, которых сам не создавал.

    Отсюда правило: набор обязан вернуть базу такой, какой взял. Чистить «до»
    защищает только себя, а от себя защищать надо соседа.
    """
    async with pg_engine.begin() as conn:
        await conn.execute(text(ЧИСТКА))
    yield
    async with pg_engine.begin() as conn:
        await conn.execute(text(ЧИСТКА))


@pytest.fixture
async def сцена(pg_engine: AsyncEngine, sessionmaker) -> dict[str, uuid.UUID]:
    """Пять диалогов, каждый под свою проверку.

    Времена разнесены на минуты, чтобы «последняя запись» определялась
    однозначно: одинаковый `created_at` у двух записей сделал бы порядок
    зависящим от плана запроса, и тест ловил бы то себя, то дефект.
    """
    await ensure_message_partitions(pg_engine)
    сейчас = datetime.now(UTC).replace(microsecond=0)
    ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as s:
        acc = AvitoAccount(
            title="Канал",
            avito_user_id=777_001,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=сейчас + timedelta(days=1),
            status="active",
            webhook_secret="s",
        )
        s.add(acc)
        cli = Client(channel="avito", external_id="bot-cli-1", name="Клиент")
        s.add(cli)
        await s.flush()

        async def диалог(ключ: str, статус: str, *, бот_писал: bool) -> Conversation:
            c = Conversation(
                channel="avito",
                external_chat_id=f"chat-{ключ}",
                account_id=acc.id,
                client_id=cli.id,
                status=статус,
                last_message_at=сейчас,
            )
            s.add(c)
            await s.flush()
            s.add(
                Message(
                    conversation_id=c.id,
                    direction="in",
                    sender_type="client",
                    body="здравствуйте",
                    attachments=[],
                    created_at=сейчас - timedelta(minutes=30),
                )
            )
            if бот_писал:
                s.add(
                    Message(
                        conversation_id=c.id,
                        direction="out",
                        sender_type="bot",
                        body="слушаю вас",
                        attachments=[],
                        created_at=сейчас - timedelta(minutes=29),
                    )
                )
            ids[ключ] = c.id
            return c

        def запись(c: Conversation, action: str, reason: str | None, минут: int) -> None:
            s.add(
                AuditLog(
                    user_id=None,
                    action=action,
                    entity="conversation",
                    entity_id=str(c.id),
                    details={"reason": reason} if reason else None,
                    created_at=сейчас - timedelta(minutes=минут),
                )
            )

        a = await диалог("отказал", "closed", бот_писал=True)
        запись(a, "bot.handoff", "refused", 20)

        # Бот отказал, а потом вступил снова и честно позвал человека.
        b = await диалог("передумал", "closed", бот_писал=True)
        запись(b, "bot.handoff", "refused", 20)
        запись(b, "bot.handoff", "client_request", 5)

        c = await диалог("завис", "new", бот_писал=True)
        запись(c, "conversation.bot_stuck_released", None, 10)

        # Записи о боте есть, а сам бот не писал: в выборку попасть не должен.
        d = await диалог("чужой", "closed", бот_писал=False)
        запись(d, "bot.handoff", "refused", 20)

        await диалог("ведёт", "in_progress", бот_писал=True)

        # С 29.08 бот ЗАКРЫВАЕТ отказ сам, и пишется `bot.closed`, а не
        # `bot.handoff` (проверка 24.09). Прежние записи «отказал» — это бой до
        # 29.08, их плитка считает по-прежнему.
        e = await диалог("закрыл", "closed", бот_писал=True)
        запись(e, "bot.closed", "refused", 15)
        f = await диалог("заявка", "closed", бот_писал=True)
        запись(f, "bot.closed", "lead_ready", 15)

        await s.commit()
    return ids


async def _страница(sessionmaker, **kw) -> dict:
    async with sessionmaker() as db:
        return await bot_dialogs.page(db, table.TableFilters(**kw))


async def test_в_выборку_попадают_только_диалоги_бота(сцена, sessionmaker) -> None:
    """Признак — сообщение бота, а не запись в журнале.

    Диалог «чужой» имеет запись `bot.handoff`, но бот в нём не писал. Отбирай
    мы по журналу, он бы попал в надзор за ботом, хотя бот к нему не
    прикасался.
    """
    стр = await _страница(sessionmaker)
    вошли = {str(r["id"]) for r in стр["items"]}
    assert str(сцена["чужой"]) not in вошли
    assert вошли == {
        str(сцена[k]) for k in ("отказал", "передумал", "завис", "ведёт", "закрыл", "заявка")
    }


async def test_исход_берётся_по_последней_записи(сцена, sessionmaker) -> None:
    """У «передумал» два `bot.handoff`: сначала отказ, потом зов человека."""
    стр = await _страница(sessionmaker)
    по_id = {str(r["id"]): r for r in стр["items"]}
    assert по_id[str(сцена["передумал"])]["outcome"]["label"] == ("клиент попросил живого человека")
    # И группы у него нет вовсе: зов человека — штатная работа, не сбой.
    assert по_id[str(сцена["передумал"])]["outcome"]["group"] is None


async def test_плитка_не_считает_позапрошлый_заход(сцена, sessionmaker) -> None:
    """«Бот закрыл сам» — только «отказал». «Передумал» отказывал тоже, но
    последним словом позвал человека, и в этой плитке ему не место."""
    стр = await _страница(sessionmaker)
    плитки = {p["group"]: p["count"] for p in стр["counters"]}
    assert плитки["closed_itself"] == 2

    отобрано = await _страница(sessionmaker, bot_outcome="closed_itself")
    assert {str(r["id"]) for r in отобрано["items"]} == {
        str(сцена["отказал"]),
        str(сцена["закрыл"]),
    }


async def test_закрытие_ботом_видно_на_экране(сцена, sessionmaker) -> None:
    """`bot.closed` — своими словами: отказ в плитке, собранная заявка — не сбой."""
    стр = await _страница(sessionmaker)
    по_id = {str(r["id"]): r for r in стр["items"]}
    assert по_id[str(сцена["закрыл"])]["outcome"]["group"] == "closed_itself"
    assert по_id[str(сцена["заявка"])]["outcome"] == {
        "group": None,
        "label": "заявка собрана — бот закрыл",
    }


async def test_завис_виден_отдельным_событием(сцена, sessionmaker) -> None:
    """«Бот завис» приходит не из причин передачи, а из события сторожа."""
    стр = await _страница(sessionmaker)
    плитки = {p["group"]: p["count"] for p in стр["counters"]}
    assert плитки["stuck"] == 1

    по_id = {str(r["id"]): r for r in стр["items"]}
    assert по_id[str(сцена["завис"])]["outcome"] == {"group": "stuck", "label": "бот завис"}


async def test_без_записей_экран_не_придумывает_исход(сцена, sessionmaker) -> None:
    """Открытый диалог без записей — «ещё ведёт», а не «закрыл сам»."""
    стр = await _страница(sessionmaker)
    по_id = {str(r["id"]): r for r in стр["items"]}
    assert по_id[str(сцена["ведёт"])]["outcome"]["label"] == "ещё ведёт"


async def test_счётчики_не_зависят_от_выбранной_плитки(сцена, sessionmaker) -> None:
    """Иначе выбранная плитка показывала бы своё число, остальные — нули, и
    вернуться из отбора было бы некуда."""
    отобрано = await _страница(sessionmaker, bot_outcome="closed_itself")
    плитки = {p["group"]: p["count"] for p in отобрано["counters"]}
    assert плитки["closed_itself"] == 2
    assert плитки["stuck"] == 1


async def test_живая_полоса_считает_только_ведомые_сейчас(сцена, sessionmaker) -> None:
    """«Сейчас» отвечает на другой вопрос, чем таблица под ним.

    Таблица разбирает СЛУЧИВШЕЕСЯ — там признак «бот здесь писал». Живая полоса
    показывает, кто у бота в руках СИЮ МИНУТУ, — там `bot_active`. Спутать их
    легко и незаметно: в сценарии бот писал в четырёх диалогах, а ведёт один.
    """
    async with sessionmaker() as s:
        c = await s.get(Conversation, сцена["ведёт"])
        assert c is not None
        c.bot_active = True
        await s.commit()

    async with sessionmaker() as db:
        живое = await bot_dialogs.live(db)

    assert живое["count"] == 1
    assert [i["id"] for i in живое["items"]] == [str(сцена["ведёт"])]
    строка = живое["items"][0]
    assert строка["bot_replies"] == 1
    assert строка["last_bot_text"] == "слушаю вас"


async def test_живая_полоса_пуста_когда_бот_никого_не_ведёт(сцена, sessionmaker) -> None:
    """Ноль — это ноль, а не пустой список рядом с ненулевым счётчиком."""
    async with sessionmaker() as db:
        живое = await bot_dialogs.live(db)
    assert живое["count"] == 0
    assert живое["items"] == []


async def test_ёмкость_считается_отдельно_от_сбоев(сцена, sessionmaker) -> None:
    """«Не смог сам» приходит своим видом, чтобы экран не назвал это сбоем."""
    async with sessionmaker() as s:
        c = await s.get(Conversation, сцена["ведёт"])
        assert c is not None
        s.add(
            AuditLog(
                user_id=None,
                action="bot.handoff",
                entity="conversation",
                entity_id=str(c.id),
                details={"reason": "ai_low_confidence"},
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()

    стр = await _страница(sessionmaker)
    виды = {p["group"]: p["kind"] for p in стр["counters"]}
    assert виды["not_capable"] == "capacity"
    assert виды["closed_itself"] == "failure"

    числа = {p["group"]: p["count"] for p in стр["counters"]}
    assert числа["not_capable"] == 1
