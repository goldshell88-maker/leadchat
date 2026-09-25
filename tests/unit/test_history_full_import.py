"""Загрузка ВСЕЙ истории при подключении канала (требование владельца, docs/41 §11).

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО.

11 августа владелец отменил решение от 8 августа: «при подключении аккаунта он
должен подгрузить все диалоги, которые были и есть». Само по себе снятие
границы — одна строка. Опасны последствия, и каждое из них закрыто отдельной
проверкой:

* ПАРТИЦИИ. Таблица `messages` разбита помесячно вокруг сегодняшнего дня, и
  одно сообщение годовой давности роняло загрузку целиком (#24). Проверяем
  год истории — партиция обеспечивается под КАЖДЫЙ месяц, который реально
  пришёл, — и пустой чат, где обеспечивать нечего.
* ОЧЕРЕДЬ. Импортированное не должно свалиться операторам лавиной. Старое
  непрочитанное уходит в архив, свежее — по-настоящему в очередь (SCEN-21: до
  этого статус поднимался, а `offered_at` не ставился, и диалог не попадал
  никуда).
* МЕТРИКИ. У импортированного диалога время сообщений — прошлогоднее.
  Статистика первого ответа считается от `messages.created_at`, поэтому год
  переписки обязан лечь в прошлое, а не в «сегодня».
* ОБЪЁМ. Видимый ход работы «загружено N из M», остановка и продолжение с
  необработанных чатов.
* ГЛУБИНА. Умолчание — вся история; «с момента подключения» осталось выбором.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import Conversation, Message
from app.scheduler import partitions as partitions_mod
from app.services import avito_accounts as svc
from app.services import inbox

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 770200
CLIENT_UID = 999201
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


# --------------------------------------------------------------- фейк Авито


def _raw_chat(
    chat_id: str,
    *,
    unread: bool = False,
    last_at: datetime | None = None,
    client_id: int = CLIENT_UID,
) -> dict[str, Any]:
    return {
        "id": chat_id,
        "users": [{"id": ACCOUNT_UID, "name": "Мы"}, {"id": client_id, "name": "Клиент"}],
        "context": {"type": "item", "value": {"title": "Ремонт стиральных машин"}},
        "has_unread": unread,
        "updated": int(last_at.timestamp()) if last_at else None,
    }


def _raw_msg(msg_id: str, *, created: datetime, author_id: int = CLIENT_UID) -> dict[str, Any]:
    return {
        "id": msg_id,
        "author_id": author_id,
        "created": int(created.timestamp()),
        "content": {"text": f"сообщение {msg_id}"},
    }


def _api(
    chats: list[dict[str, Any]],
    messages: dict[str, list[dict[str, Any]]],
    *,
    page_size: int = 100,
):
    """Подмена похода в Авито: список чатов и история каждого чата.

    Отвечает ПО СМЕЩЕНИЮ, а не очередью страниц: прогон обходит список чатов
    дважды — перепись даёт знаменатель «из M», загрузка идёт следом.
    """

    async def fake_call(fn, _account, _db, _redis, _limiter, *args, **kwargs):
        offset = int(kwargs.get("offset", 0))
        limit = min(int(kwargs.get("limit", page_size)), page_size)
        if fn.__name__ == "get_chats":
            return chats[offset : offset + limit]
        chat_id = args[1]
        page = messages.get(chat_id, [])
        return page[offset : offset + limit]

    return fake_call


class _PgLike:
    """Движок, который считает себя PostgreSQL: партиции — только про него."""

    dialect = SimpleNamespace(name="postgresql")


@pytest.fixture
def coverage_calls(monkeypatch) -> list[Any]:
    """НАСТОЯЩИЙ PartitionCoverage поверх поддельного движка.

    Юнит-тесты идут на SQLite, где партиций нет вовсе и покрытие молча
    выходит. Подменять сам класс заглушкой нельзя: тогда проверялась бы
    заглушка. Поэтому подменяем только выполнение DDL и подсовываем движок,
    который представляется постгресом, — работает вся настоящая логика:
    ключи месяцев, память прогона, порядок вызовов.
    """
    calls: list[Any] = []

    async def fake_create(_engine, months):
        months = list(months)
        calls.append(months)
        return [partitions_mod.partition_name(m) for m in months]

    monkeypatch.setattr(partitions_mod, "_create", fake_create)
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda engine=None: partitions_mod.PartitionCoverage(_PgLike())
    )
    return calls


async def _run(db_sessionmaker, redis, account_id, depth=svc.DEFAULT_HISTORY_DEPTH):
    await svc.backfill_account(
        {"db_session_factory": db_sessionmaker, "redis": redis}, account_id, depth
    )


async def _conversations(db_sessionmaker) -> list[Conversation]:
    async with db_sessionmaker() as db:
        return list((await db.execute(sa.select(Conversation))).scalars().all())


async def _messages(db_sessionmaker) -> list[Message]:
    async with db_sessionmaker() as db:
        return list(
            (await db.execute(sa.select(Message).order_by(Message.created_at))).scalars().all()
        )


# ============================================================ 1. партиции


async def test_year_of_history_gets_a_partition_for_every_month(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Год переписки: партиция обеспечивается под каждый пришедший месяц (#24).

    Это главная проверка требования «грузить всё». Раньше история кончалась
    моментом подключения и старше текущего месяца ничего не приходило; теперь
    приходит тринадцать месяцев подряд, и каждый обязан найти, куда лечь.
    """
    # ⚠ ШАГ ПО КАЛЕНДАРНЫМ МЕСЯЦАМ, А НЕ ПО 30 ДНЕЙ (28.08). Тринадцать шагов по
    # 30 дней покрывают 390 дней, но НЕ тринадцать месяцев: февраль короче шага,
    # а месяцы по 31 дню ловятся дважды. 28 августа 2026 набор дал одиннадцать
    # месяцев, и тест покраснел, ничего не сломав в продукте, — та же бомба с
    # часовым механизмом, что разобрали в ChannelCardFacts. Проверяется свойство
    # «под каждый месяц истории есть партиция», поэтому месяцы берём прямо.
    начало = NOW.replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    моменты = []
    год, месяц = начало.year, начало.month
    for _ in range(13):
        моменты.append(начало.replace(year=год, month=месяц))
        месяц -= 1
        if месяц == 0:
            месяц, год = 12, год - 1
    history = [_raw_msg(f"m{n}", created=м) for n, м in enumerate(моменты)]
    monkeypatch.setattr(svc, "_avito_call", _api([_raw_chat("c1")], {"c1": history}))

    await _run(db_sessionmaker, redis, account.id)

    stored = await _messages(db_sessionmaker)
    assert len(stored) == 13, "год истории должен лечь целиком"
    months = {(m[0].year, m[0].month) for m in coverage_calls}
    assert len(months) == 13, f"партиции обеспечены не под все месяцы: {sorted(months)}"
    # Каждый месяц — ровно один поход за DDL: тяжёлая блокировка на
    # родительской таблице не должна браться на каждое сообщение.
    assert len(coverage_calls) == len(months)


async def test_empty_chat_asks_for_no_partitions_and_creates_no_dialog(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Пустой диапазон: обеспечивать нечего, заводить нечего, падать не с чего.

    Проверяется ИМЕННО ЧИСТЫЙ ПРОПУСК, а не «в базе ничего не появилось».
    Сорвавшийся чат тоже ничего не оставляет — его откатывает общая защита, —
    но человеку при этом говорят «не удалось загрузить 1», и он идёт искать
    поломку там, где её нет. На девяти аккаунтах по 300+ объявлений пустых
    чатов наберётся столько, что отчёт станет нечитаемым.
    """
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    monkeypatch.setattr(svc, "_avito_call", _api([_raw_chat("пусто")], {"пусто": []}))

    await _run(db_sessionmaker, redis, account.id)

    assert coverage_calls == []
    assert await _conversations(db_sessionmaker) == []
    notice = events[-1]
    assert notice["level"] == "info", "пустой чат — не сбой"
    assert "не удалось" not in notice["text"]


async def test_partitions_are_ensured_before_anything_is_written(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Партиции обеспечиваются ДО первой записи в базу — иначе всё встаёт колом.

    ЧТО СЛУЧАЕТСЯ ПРИ ОБРАТНОМ ПОРЯДКЕ. Обеспечение ходит отдельным
    соединением; `CREATE TABLE ... PARTITION OF messages` просит на
    `conversations` блокировку под внешний ключ, а она конфликтует с
    незакрытой вставкой диалога. DDL ждёт нашего коммита, коммит ждёт DDL, и
    Постгрес такой клубок не разрубает. Хуже: ждущее соединение уже держит
    AccessExclusiveLock на `messages`, то есть приём сообщений всей компании
    останавливается, пока кто-нибудь не убьёт воркер.

    Настоящее доказательство — `tests/integration/test_history_import_pg.py`
    на живом PostgreSQL: в SQLite партиций нет и блокировок этих не бывает.
    Здесь стоит дешёвая проверка порядка: она падает за полсекунды и называет
    причину, вместо восемнадцати секунд ожидания блокировки.
    """
    order: list[str] = []
    original_conv = svc._get_or_create_conversation
    original_client = svc._get_or_create_client

    async def spy_conv(*args, **kwargs):
        order.append("запись")
        return await original_conv(*args, **kwargs)

    async def spy_client(*args, **kwargs):
        order.append("запись")
        return await original_client(*args, **kwargs)

    class OrderedCoverage(partitions_mod.PartitionCoverage):
        async def ensure_all(self, moments):
            order.append("партиции")
            await super().ensure_all(moments)

    monkeypatch.setattr(svc, "_get_or_create_conversation", spy_conv)
    monkeypatch.setattr(svc, "_get_or_create_client", spy_client)
    monkeypatch.setattr(svc, "PartitionCoverage", lambda engine=None: OrderedCoverage(_PgLike()))
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api([_raw_chat("c1")], {"c1": [_raw_msg("m1", created=NOW - timedelta(days=200))]}),
    )

    await _run(db_sessionmaker, redis, account.id)

    assert order[0] == "партиции", f"партиции обязаны быть первыми, а порядок такой: {order}"


# ============================================================ 2. глубина


async def test_default_depth_is_the_whole_history() -> None:
    """Умолчание — вся история: так просил владелец 11 августа."""
    account = SimpleNamespace(id=uuid.uuid4(), created_at=NOW - timedelta(days=3))
    plan = svc.make_plan(account)
    assert plan.depth == svc.HISTORY_ALL
    assert plan.floor is None, "у «всей истории» нижней границы быть не должно"


async def test_since_connect_depth_keeps_only_the_new(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Прежнее решение осталось выбором: «с момента подключения» режет старое."""
    # Отсчёт от МОМЕНТА ПОДКЛЮЧЕНИЯ канала, а не от времени импорта модуля:
    # нижняя граница этой глубины — `account.created_at`, и в длинном прогоне
    # набора он оказывается заметно позже, чем NOW.
    connected = svc.ensure_aware(account.created_at)
    history = [
        _raw_msg("старое", created=connected - timedelta(days=300)),
        _raw_msg("свежее", created=connected + timedelta(minutes=5)),
    ]
    monkeypatch.setattr(svc, "_avito_call", _api([_raw_chat("c1")], {"c1": history}))

    await _run(db_sessionmaker, redis, account.id, svc.HISTORY_SINCE_CONNECT)

    stored = await _messages(db_sessionmaker)
    assert [m.external_message_id for m in stored] == ["свежее"]


async def test_whole_history_depth_keeps_the_old_one(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Та же переписка при глубине «вся история» — обе половины на месте."""
    connected = svc.ensure_aware(account.created_at)
    history = [
        _raw_msg("старое", created=connected - timedelta(days=300)),
        _raw_msg("свежее", created=connected + timedelta(minutes=5)),
    ]
    monkeypatch.setattr(svc, "_avito_call", _api([_raw_chat("c1")], {"c1": history}))

    await _run(db_sessionmaker, redis, account.id, svc.HISTORY_ALL)

    stored = await _messages(db_sessionmaker)
    assert [m.external_message_id for m in stored] == ["старое", "свежее"]


# ============================================================ 3. очередь (SCEN-21)


async def _queue_ids(db_sessionmaker) -> list[uuid.UUID]:
    """Диалоги, которые ОПЕРАТОР УВИДИТ во «Входящих» — тем же условием, что и очередь."""
    async with db_sessionmaker() as db:
        rows = await db.execute(sa.select(Conversation.id).where(inbox.queue_condition()))
        return list(rows.scalars().all())


async def test_old_unread_goes_to_the_archive_not_to_thirteen_operators(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Прошлогоднее непрочитанное — архив с честным бейджем, а не «Входящие».

    Пометка «непрочитано» на чате годовой давности значит «в вебе Авито его
    никто не открывал», а не «клиент ждёт ответа». Свалить такие диалоги в
    очередь — это тринадцать операторов, разгребающих прошлый год вместо
    сегодняшних заявок.
    """
    long_ago = NOW - timedelta(days=200)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("старый", unread=True, last_at=long_ago)],
            {"старый": [_raw_msg("m1", created=long_ago)]},
        ),
    )

    await _run(db_sessionmaker, redis, account.id)

    (conv,) = await _conversations(db_sessionmaker)
    assert conv.status == "closed"
    assert conv.offered_at is None
    assert conv.unread_count == 1, "бейдж непрочитанного остаётся: переписку не читали"
    assert await _queue_ids(db_sessionmaker) == []


async def test_fresh_unread_really_enters_the_queue(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """SCEN-21: свежее непрочитанное обязано попасть в очередь ПО-НАСТОЯЩЕМУ.

    Здесь код расходился с собственным комментарием: рядом было написано
    «поднять из архива в очередь», статус поднимался в `new`, а `offered_at`
    не ставился — и диалог не попадал ни во «Входящие», ни в архив, зато
    считался в снимке «ждут ответа сейчас».
    """
    just_now = NOW - timedelta(hours=1)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("свежий", unread=True, last_at=just_now)],
            {"свежий": [_raw_msg("m1", created=just_now)]},
        ),
    )

    await _run(db_sessionmaker, redis, account.id)

    (conv,) = await _conversations(db_sessionmaker)
    assert conv.status == "new"
    assert conv.offered_at is not None
    # Очередь сортируется по ожиданию: импортированный диалог обязан встать
    # по времени сообщения клиента, а не притвориться самым свежим.
    assert svc.ensure_aware(conv.offered_at) == just_now
    assert await _queue_ids(db_sessionmaker) == [conv.id]


async def test_report_tells_how_many_landed_in_the_inbox(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """«Загружено 2» и «из них 1 ждёт ответа» — разные новости, и вторая срочная."""
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    fresh, old = NOW - timedelta(hours=2), NOW - timedelta(days=100)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [
                _raw_chat("свежий", unread=True, last_at=fresh),
                _raw_chat("старый", unread=True, last_at=old, client_id=CLIENT_UID + 1),
            ],
            {"свежий": [_raw_msg("m1", created=fresh)], "старый": [_raw_msg("m2", created=old)]},
        ),
    )

    await _run(db_sessionmaker, redis, account.id)

    notice = events[-1]
    assert "загружено 2 диалогов" in notice["text"]
    assert "из них 1 с непрочитанным — в «Входящих»" in notice["text"]


# ============================================================ 4. метрики


async def test_imported_history_does_not_land_in_today(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Год переписки обязан лечь в прошлое.

    Скорость первого ответа и доля отвеченных считаются от
    `messages.created_at` (материализованное представление `first_client_at`,
    06 §3.2). Проставь импорт «сейчас» — и сегодняшний отчёт наполнится
    диалогами, которых сегодня не было, а первый ответ годичной давности
    посчитается как сегодняшний.
    """
    old = NOW - timedelta(days=180)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("старый", unread=True, last_at=old)],
            {
                "старый": [
                    _raw_msg("вопрос", created=old),
                    _raw_msg("ответ", created=old + timedelta(minutes=3), author_id=ACCOUNT_UID),
                ]
            },
        ),
    )

    await _run(db_sessionmaker, redis, account.id)

    today = NOW.date()
    for msg in await _messages(db_sessionmaker):
        assert svc.ensure_aware(msg.created_at).date() != today, (
            "сообщение истории получило сегодняшнее время — метрики соврут"
        )
    (conv,) = await _conversations(db_sessionmaker)
    assert svc.ensure_aware(conv.last_message_at).date() != today
    # И в «ждут ответа сейчас» (snapshot по status='new') он тоже не попадёт.
    assert conv.status == "closed"


# ============================================================ 5. ход, остановка, продолжение


async def test_progress_says_how_many_of_how_many(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """«Загружено N из M»: без знаменателя строка не отличает начало от конца."""
    seen_states: list[dict[str, Any]] = []
    original = svc._backfill_chat

    async def spy(*args, **kwargs):
        seen_states.append(await svc.get_backfill_state(redis, account.id))
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc, "_backfill_chat", spy)
    chats = [_raw_chat(f"c{n}", client_id=CLIENT_UID + n) for n in range(3)]
    history = {f"c{n}": [_raw_msg(f"m{n}", created=NOW - timedelta(days=n))] for n in range(3)}
    monkeypatch.setattr(svc, "_avito_call", _api(chats, history))

    await _run(db_sessionmaker, redis, account.id)

    assert [s["loaded"] for s in seen_states] == [0, 1, 2]
    assert [s["total"] for s in seen_states] == [3, 3, 3]
    assert [s["status"] for s in seen_states] == ["running"] * 3
    assert seen_states[0]["depth"] == svc.HISTORY_ALL
    # Прогон закончился — ход работы снят, карточка не врёт «идёт загрузка».
    assert (await svc.get_backfill_state(redis, account.id))["status"] == "idle"


async def test_stop_keeps_what_is_loaded_and_continues_from_there(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Остановка человеком: не сбой, не конец — точка возобновления.

    Проверяем обе половины требования «с возможностью остановить и
    продолжить»: прогон выходит на границе чата, а следующий заход НЕ
    перезагружает уже разобранное.
    """
    processed: list[str] = []
    original = svc._backfill_chat

    async def stop_after_first(*args, **kwargs):
        raw_chat = args[5]
        processed.append(raw_chat["id"])
        result = await original(*args, **kwargs)
        await svc.request_backfill_stop(redis, account.id)
        return result

    chats = [_raw_chat(f"c{n}", client_id=CLIENT_UID + n) for n in range(3)]
    history = {f"c{n}": [_raw_msg(f"m{n}", created=NOW - timedelta(days=n))] for n in range(3)}
    monkeypatch.setattr(svc, "_avito_call", _api(chats, history))
    monkeypatch.setattr(svc, "_backfill_chat", stop_after_first)

    await _run(db_sessionmaker, redis, account.id)

    assert processed == ["c0"], "остановка обязана сработать на границе чата"
    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "stopped"
    assert state["loaded"] == 1 and state["total"] == 3
    assert len(await _conversations(db_sessionmaker)) == 1

    # Продолжение: разобранный чат второй раз не трогаем, остальные грузим.
    processed.clear()
    monkeypatch.setattr(svc, "_backfill_chat", original)
    await _run(db_sessionmaker, redis, account.id)

    assert len(await _conversations(db_sessionmaker)) == 3
    assert (await svc.get_backfill_state(redis, account.id))["status"] == "idle"


async def test_skipped_chats_move_the_bar_but_not_the_count(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Пустой чат — позади, но диалогом не стал. И после остановки тоже.

    Разница между «разобрано» и «загружено» кажется буквоедством ровно до
    первой остановки: продолжение считало загруженные диалоги по множеству
    разобранных чатов, и каждый пустой чат прибавлял к отчёту диалог, которого
    нет. На девяти аккаунтах, где пустых чатов сотни, это отчёт, которому
    нельзя верить.
    """
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    chats = [_raw_chat("пусто"), _raw_chat("живой", client_id=CLIENT_UID + 1)]
    history = {"пусто": [], "живой": [_raw_msg("m1", created=NOW - timedelta(days=5))]}
    monkeypatch.setattr(svc, "_avito_call", _api(chats, history))

    original = svc._backfill_chat

    async def stop_after_first(*args, **kwargs):
        result = await original(*args, **kwargs)
        await svc.request_backfill_stop(redis, account.id)
        return result

    monkeypatch.setattr(svc, "_backfill_chat", stop_after_first)
    await _run(db_sessionmaker, redis, account.id)

    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "stopped"
    assert state["loaded"] == 0, "пустой чат не диалог"
    assert state["chats_offset"] == 1, "но он позади, и полоса хода это показывает"

    monkeypatch.setattr(svc, "_backfill_chat", original)
    await _run(db_sessionmaker, redis, account.id)

    assert "загружено 1 диалогов" in events[-1]["text"]
    assert len(await _conversations(db_sessionmaker)) == 1


async def test_second_run_does_not_refetch_finished_chats(
    monkeypatch, db_sessionmaker, redis, account, coverage_calls
) -> None:
    """Продолжение стоит дёшево: историю разобранного чата заново не качаем.

    Возобновление по СМЕЩЕНИЮ здесь и ломалось: список чатов Авито
    отсортирован по свежести, любое новое сообщение сдвигает страницы, и
    «продолжить с третьего» перепрыгивало бы через чат, уехавший вниз.
    Поэтому точка возобновления — имена разобранных чатов.
    """
    fetched: list[str] = []
    chats = [_raw_chat(f"c{n}", client_id=CLIENT_UID + n) for n in range(2)]
    history = {f"c{n}": [_raw_msg(f"m{n}", created=NOW - timedelta(days=n))] for n in range(2)}
    api = _api(chats, history)

    async def counting_api(fn, *args, **kwargs):
        if fn.__name__ != "get_chats":
            fetched.append(args[5] if len(args) > 5 else args[-1])
        return await api(fn, *args, **kwargs)

    monkeypatch.setattr(svc, "_avito_call", counting_api)

    # Первый заход остановлен после первого чата.
    original = svc._backfill_chat

    async def stop_after_first(*args, **kwargs):
        result = await original(*args, **kwargs)
        await svc.request_backfill_stop(redis, account.id)
        return result

    monkeypatch.setattr(svc, "_backfill_chat", stop_after_first)
    await _run(db_sessionmaker, redis, account.id)
    assert fetched == ["c0"]

    monkeypatch.setattr(svc, "_backfill_chat", original)
    fetched.clear()
    await _run(db_sessionmaker, redis, account.id)
    assert fetched == ["c1"], "второй заход обязан взять только необработанный чат"


async def test_old_progress_format_is_still_readable(redis, account) -> None:
    """Прогон, начатый ДО выкатки, не должен выглядеть сорвавшимся.

    В его ключе лежит голое число — смещение страницы чатов. Уронить на нём
    разбор значило бы в день выкатки показать «Загрузка сорвалась» на канале,
    где всё в порядке.
    """
    await redis.set(f"backfill:{account.id}", 300)

    state = await svc.get_backfill_state(redis, account.id)

    assert state["status"] == "running"
    assert state["chats_offset"] == 300
    assert state["total"] is None


async def test_progress_never_shows_more_than_the_total(redis, account) -> None:
    """«Загружено 1002 из 1000» — цифра, после которой не верят и остальным.

    Перепись делается один раз в начале, а чаты во время часового прогона
    прибывают: числитель обгонит знаменатель на любом живом канале.
    """
    await redis.set(
        f"backfill:{account.id}",
        '{"phase": "loading", "loaded": 1002, "total": 1000, "chats_offset": 1002}',
    )

    state = await svc.get_backfill_state(redis, account.id)

    assert state["loaded"] == 1002
    assert state["total"] == 1002
