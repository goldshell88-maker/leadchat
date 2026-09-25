"""Счёт есть, реплики нет: инвариант ``bot_msgs_row`` (замер боя 08.09).

ЧТО СЛУЧИЛОСЬ НА БОЮ. У 299 диалогов ``bot_vars.counters.bot_msgs_row > 0``, и у
127 из них в переписке НЕТ реплик бота вовсе — ни исходящих, ни заметок, при
живых входящих (707) и ответах операторов (653). Все 127 укладываются в окно
19–23 августа по ``last_step_at``, после 23.08 — ни одного.

ПОЧЕМУ ЭТО ДОРОГО. ``bot_msgs_row`` — предохранитель «не заваливать клиента»:
фантомный счёт закрывает боту рот за реплики, которых он не говорил. Ни в
интерфейсе, ни в журнале это ничем не отличается от исправной работы — потерю
видно только сличкой счётчика с лентой руками.

ЗДЕСЬ ДВА СТОРОЖА, И ОНИ ПРО РАЗНОЕ:

1. движок обязан считать ТОЛЬКО состоявшуюся запись — в любом режиме;
2. планировщик обязан находить свежие нарушения инварианта и звать человека,
   а исторические 127 — оставлять в покое.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.bots.engine import SUGGEST_PREFIX, ScenarioEngine
from app.bots.state import BotState, utcnow_iso
from app.models import Bot, Client, Conversation, Message
from app.models.notification import Notification
from app.scheduler.jobs import bot_phantom
from app.services import notifications as notify_svc

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


# --------------------------------------------------------- сторож №1: движок


async def _engine(db, *, mode: str) -> ScenarioEngine:
    """Движок на живых строках базы: бот, канал не нужен, диалог свой."""
    bot = Bot(
        name="Фантомный счёт",
        schedule={"always": True},
        scenario={"version": 1, "entry": "s1", "steps": []},
        mode=mode,
    )
    client = Client(channel="avito", external_id=uuid.uuid4().hex[:10], name="Клиент")
    db.add_all([bot, client])
    await db.flush()
    from app.models import AvitoAccount

    account = AvitoAccount(
        title="LP",
        avito_user_id=100_000 + uuid.uuid4().int % 100_000,
        access_token_enc=b"a",
        refresh_token_enc=b"r",
        token_expires_at=NOW + timedelta(days=1),
        status="active",
        webhook_secret="whsec",
        bot_id=bot.id,
    )
    db.add(account)
    await db.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=uuid.uuid4().hex[:10],
        account_id=account.id,
        client_id=client.id,
        status="in_progress",
        bot_active=True,
        bot_vars={},
    )
    db.add(conv)
    await db.flush()
    return ScenarioEngine(bot=bot, conv=conv, state=BotState.from_conv(conv), db=db, now=NOW)


async def test_suggestion_that_was_not_written_is_not_counted(db, monkeypatch):
    """Заметка не легла — счёта нет. Ровно та асимметрия, что жила в коде.

    В режиме подсказки счётчик рос ДО записи, а `add_note` возвращает None,
    если такая заметка уже лежит (`_заметка_уже_есть`): реплики нет, счёт есть.

    Ветку «такая заметка уже лежит» подменяем прямо, а не выстраиваем к ней
    дорогу из двадцати служебных заметок: проверяется договор «счёт по факту
    записи», а не маршрут к одному из его нарушений. Сам `add_note` при этом
    работает настоящий — вместе со своей записью в журнал.

    ДИВЕРСИЯ: вернуть в `send_bot_message` прежний порядок — `+= 1` перед
    `add_note` (или заменить `if msg is not None:` на `if True:`) — тест
    краснеет: счётчик 1 при нуле заметок. Проверено; после диверсии в .py
    чистить `__pycache__`.
    """
    eng = await _engine(db, mode="suggest")

    async def уже_лежит(body: str) -> bool:
        return True

    monkeypatch.setattr(eng, "_заметка_уже_есть", уже_лежит)

    assert await eng.send_bot_message("Подскажите модель техники?") is None
    assert eng.state.counters.bot_msgs_row == 0, "счёт без записи — это фантом"
    notes = (
        await db.execute(sa.select(sa.func.count()).select_from(Message).where(Message.body != ""))
    ).scalar_one()
    assert notes == 0, "в ленте не появилось ничего — считать нечего"


async def test_suggestion_that_was_written_is_counted_once(db):
    """Обратная сторона: заметка легла — счёт вырос ровно на единицу."""
    eng = await _engine(db, mode="suggest")
    msg = await eng.send_bot_message("Подскажите модель техники?")
    assert msg is not None and msg.body.startswith(SUGGEST_PREFIX)
    assert eng.state.counters.bot_msgs_row == 1


async def test_auto_mode_counts_the_message_it_wrote(db):
    """Обычный режим считает так же — и это теперь ОДНА точка на оба режима.

    Точка одна намеренно: «подсказка» когда-то была новым режимом и завела себе
    собственный счёт со своим порядком. Следующий режим правило получит сам.
    """
    eng = await _engine(db, mode="auto")
    msg = await eng.send_bot_message("Здравствуйте! Что случилось с техникой?")
    assert msg is not None and msg.sender_type == "bot"
    assert eng.state.counters.bot_msgs_row == 1
    assert eng.conv.last_message_at == NOW


async def test_repeat_guard_does_not_count_either(db):
    """Гард повторов молчит — значит и счёта нет: реплики-то не было."""
    eng = await _engine(db, mode="auto")
    await eng.send_bot_message("Здравствуйте! Что случилось с техникой?")
    await eng.send_bot_message("здравствуйте!  что случилось  с техникой?")
    assert eng.state.counters.bot_msgs_row == 1, "второй раз бот ничего не сказал"


# --------------------------------------------------- сторож №2: планировщик


async def _conv_with_counter(
    db_sessionmaker,
    account,
    *,
    last_step_at: datetime | None,
    msgs_row: int = 1,
    bot_reply: bool = False,
    updated_at: datetime = NOW,
    tag: str = "x",
) -> uuid.UUID:
    """Диалог с заданным счётчиком, шагом и наличием (или нет) реплики бота."""
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"ph-{tag}", name="Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"ph-chat-{tag}",
            account_id=account.id,
            client_id=cl.id,
            status="closed",
            bot_active=False,
            bot_vars={
                "counters": {"steps_total": 7, "bot_msgs_row": msgs_row, "ai_calls": 4},
                "last_step_at": utcnow_iso(last_step_at) if last_step_at else None,
            },
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="здравствуйте",
                attachments=[],
                delivery_status="delivered",
                created_at=NOW - timedelta(minutes=5),
            )
        )
        if bot_reply:
            s.add(
                Message(
                    id=uuid.uuid4(),
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="bot",
                    body="Здравствуйте! Что случилось?",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=NOW - timedelta(minutes=4),
                )
            )
        await s.commit()
        # `updated_at` ставится ORM'ом на любое изменение строки — задаём его
        # ПОСЛЕ вставки, иначе onupdate перепишет наше значение своим now().
        await s.execute(
            sa.update(Conversation).where(Conversation.id == conv.id).values(updated_at=updated_at)
        )
        await s.commit()
        return conv.id


async def test_fresh_phantom_is_found(db, db_sessionmaker, make_avito_account):
    """Свежее нарушение инварианта видно: счёт есть, реплики бота нет."""
    account = await make_avito_account()
    conv_id = await _conv_with_counter(
        db_sessionmaker, account, last_step_at=NOW - timedelta(minutes=20), tag="fresh"
    )
    found = await bot_phantom.find_phantoms(db, now=NOW)
    assert [pair[0] for pair in found] == [conv_id]


async def test_old_phantoms_stay_silent_even_when_the_row_was_touched_today(
    db, db_sessionmaker, make_avito_account
):
    """ГЛАВНОЕ: исторические 127 не звенят, даже если строку тронули сегодня.

    У тех 127 диалогов `last_step_at` стоит в окне 19–23.08, а `updated_at`
    живёт своей жизнью: на бою один из них тронут за последние сутки, два — за
    неделю, самый свежий 07.09 (открыли, переназначили, закрыли). Проверка по
    `updated_at` подняла бы тревогу о беде месячной давности и обвинила бы
    сегодняшний код.

    ДИВЕРСИЯ: убрать из `find_phantoms` разбор `last_step_at` (условие
    `step_at is None or step_at < cutoff`) — тест краснеет, находится диалог
    трёхнедельной давности. Проверено. Убрать только строковое условие в SQL
    мало: тест останется зелёным, потому что решение принимает Python — так и
    задумано, строковое сравнение там лишь бережёт порцию LIMIT.
    """
    account = await make_avito_account()
    await _conv_with_counter(
        db_sessionmaker,
        account,
        last_step_at=datetime(2026, 8, 21, 6, 28, tzinfo=UTC),
        updated_at=NOW - timedelta(hours=2),
        tag="old",
    )
    assert await bot_phantom.find_phantoms(db, now=NOW) == []


async def test_a_dialog_with_a_bot_reply_is_not_a_violation(
    db, db_sessionmaker, make_avito_account
):
    """Счёт при живой реплике бота — это норма, а не находка."""
    account = await make_avito_account()
    await _conv_with_counter(
        db_sessionmaker,
        account,
        last_step_at=NOW - timedelta(minutes=20),
        bot_reply=True,
        tag="ok",
    )
    assert await bot_phantom.find_phantoms(db, now=NOW) == []


async def test_zero_counter_is_not_a_violation(db, db_sessionmaker, make_avito_account):
    """Нулевой счётчик без реплик бота — обычный диалог, который бот не вёл."""
    account = await make_avito_account()
    await _conv_with_counter(
        db_sessionmaker,
        account,
        last_step_at=NOW - timedelta(minutes=20),
        msgs_row=0,
        tag="zero",
    )
    assert await bot_phantom.find_phantoms(db, now=NOW) == []


async def test_alarm_reaches_the_bell(db_sessionmaker, make_avito_account, redis, monkeypatch):
    """Находка становится строкой в колокольчике администратора.

    ДИВЕРСИЯ: убрать вид `bot.phantom_reply` из каталога
    `services/notifications.KINDS` — тест краснеет на заголовке (черновик
    получает машинное имя вида вместо русской подписи), а `test_notification_
    catalog` — на расхождении с интерфейсом. Проверено.
    """
    #: ⚠ ЗДЕСЬ ЧАСЫ НАСТОЯЩИЕ, А НЕ `NOW`, И ЭТО ЕДИНСТВЕННО ВЕРНО (правка 09.09).
    #:
    #: Проверка зовёт ТОЧКУ ВХОДА `check_phantom_counters`, а та берёт время
    #: сама (`datetime.now(UTC)`) — параметра `now` у неё нет и не должно быть:
    #: планировщик зовёт её без аргументов. Данные же сеялись от зашитой
    #: константы `NOW = 2026-09-08 12:00`, то есть проверка работала ровно
    #: сутки — восьмого сентября. Девятого окно поиска уехало вперёд, диалог
    #: оказался за его краем, и `check_phantom_counters` честно вернула 0.
    #:
    #: Тест не «сломался» — он ПРОТУХ по календарю и с этого дня валил ЛЮБУЮ
    #: выкатку: `ship.sh` гоняет юниты шагом «0г» и на красном прекращает
    #: работу. Соседняя отрицательная проверка (`test_silence_...`) при этом
    #: зеленела по неверной причине: она ждёт 0 и получала его не потому, что
    #: сторож молчит по делу, а потому что находить было нечего.
    #:
    #: `NOW` остаётся у тех проверок, которые зовут `find_phantoms(db, now=NOW)`
    #: — там время передаётся явно, и зашитая дата уместна.
    сейчас = datetime.now(UTC)
    account = await make_avito_account()
    await _conv_with_counter(
        db_sessionmaker,
        account,
        last_step_at=сейчас - timedelta(minutes=20),
        # ⚠ И `updated_at` ТОЖЕ. Запрос отбирает по нему («свежесть проверяется
        # ДВАЖДЫ и намеренно»), а умолчание помощника — та же зашитая `NOW`.
        # Передать одно время и забыть второе значит починить проверку ровно до
        # завтра.
        updated_at=сейчас,
        tag="bell",
    )
    monkeypatch.setattr(bot_phantom.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(bot_phantom.redis_mod, "get_client", lambda: redis)

    assert await bot_phantom.check_phantom_counters() == 1

    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(Notification))).scalars().one()
    assert row.kind == "bot.phantom_reply"
    assert row.audience == "admin"
    assert row.severity == "warning"
    assert row.title == notify_svc.KINDS["bot.phantom_reply"].title
    assert "1" in (row.body or "")


async def test_silence_when_the_invariant_holds(
    db_sessionmaker, make_avito_account, redis, monkeypatch
):
    """Тишина, когда всё в порядке: ни строки, ни повода её читать.

    ⚠ Проверка ОТРИЦАТЕЛЬНАЯ, поэтому данные подобраны так, чтобы без сторожа
    на экране что-то БЫЛО: диалог с ненулевым счётчиком в базе лежит, и от
    находки его отделяет ровно одна живая реплика бота.

    ⚠ И ЧАСЫ ЗДЕСЬ НАСТОЯЩИЕ ПО ТОЙ ЖЕ ПРИЧИНЕ, ЧТО У СОСЕДА ВЫШЕ. С зашитой
    датой этот ноль приходил бы не от сторожа, а от пустого окна поиска — то
    есть отрицательная проверка зеленела бы, даже если сторож сломан.
    """
    сейчас = datetime.now(UTC)
    account = await make_avito_account()
    await _conv_with_counter(
        db_sessionmaker,
        account,
        last_step_at=сейчас - timedelta(minutes=20),
        updated_at=сейчас,  # см. довод у соседа выше: свежесть отбирается дважды
        bot_reply=True,
        tag="quiet",
    )
    monkeypatch.setattr(bot_phantom.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(bot_phantom.redis_mod, "get_client", lambda: redis)

    assert await bot_phantom.check_phantom_counters() == 0
    async with db_sessionmaker() as s:
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(Notification))
        ).scalar_one() == 0


async def test_the_job_is_registered_in_the_scheduler():
    """Задача без строки в расписании не запускается никогда."""
    from app.scheduler.main import build_scheduler

    scheduler = build_scheduler()
    ids = {job.id for job in scheduler.get_jobs()}
    assert bot_phantom.JOB_ID in ids
