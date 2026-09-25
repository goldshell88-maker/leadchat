"""ЗАГРУЗКА ИСТОРИИ НАРЕЗАНА ПО ВРЕМЕНИ И ПРОДОЛЖАЕТ САМА СЕБЯ.

⚠ БОЕВАЯ ПОЛОМКА 28.08, ЖАЛОБА ВЛАДЕЛЬЦА: «плохо работает сгрузка диалогов…
сейчас у меня всё делается долго и иногда встаёт».

ЧТО БЫЛО. У воркера `job_timeout = 300` — на ВСЕ задачи, включая эту. Тысяча с
лишним чатов в пять минут не помещается, и ARQ убивал загрузку на 300-й
секунде. Убивал `CancelledError`, а он наследник BaseException — то есть мимо
`except Exception` в конце задачи: ни пометки о срыве, ни отчёта, ни повторной
постановки. Ключ хода работы оставался в фазе «loading» с застывшим временем,
карточка канала показывала «Загрузка сорвалась», и НИЧТО её не возобновляло.

Замер боя по ключу `backfill:{id}` канала GLEB: started_at 17:11:15,
updated_at 17:16:15 — ровно 300 секунд, loaded 290 из 1100. И так на ДЕСЯТИ
каналах разом; ещё на двадцати загрузка не начиналась ни разу.

ЧТО ПРОВЕРЯЕМ. Заход кончается САМ, раньше таймаута, на границе чата; ход
работы и множество разобранных чатов остаются; продолжение ставится в очередь
и идёт с того же места, а не с нуля.
"""

from typing import Any

import pytest

from app.services import avito_accounts as svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(770900)


def _chats(*chats: dict[str, Any], page_size: int = 100):
    catalogue = list(chats)

    async def fake_call(fn, *_args, offset: int = 0, limit: int = page_size, **_kwargs):
        return catalogue[offset : offset + min(limit, page_size)]

    return fake_call


@pytest.fixture
def тихо(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def fake_publish(_redis, _kind, payload, **_kw):
        events.append(payload)

    monkeypatch.setattr(svc, "publish_event", fake_publish)
    return events


@pytest.fixture
def поставленные(monkeypatch) -> list[tuple]:
    """Перехват постановки продолжения: очереди в юнит-окружении нет."""
    ставили: list[tuple] = []

    async def fake_enqueue(account_id, depth=svc.DEFAULT_HISTORY_DEPTH, *, dedupe=True):
        ставили.append((account_id, depth, dedupe))

    monkeypatch.setattr(svc, "enqueue_backfill", fake_enqueue)
    return ставили


def _часы(monkeypatch, шаг: float) -> None:
    """Часы, которые прыгают на `шаг` секунд при каждом взгляде.

    Настоящую задержку не изображаем: тест, который спит четыре минуты, никто
    не станет гонять, а именно он и должен краснеть при поломке.

    ⚠ ПОДМЕНЯЕМ ШОВ `_elapsed_clock`, А НЕ `time.monotonic`. Первая редакция
    подменяла сам `time.monotonic` — а это модуль, общий на весь процесс:
    фальшивые часы доставались заодно asyncio и клиенту Redis, и прогон
    разваливался по причинам, к проверяемому не относящимся.
    """
    состояние = {"t": 0.0}

    def fake_clock() -> float:
        текущее = состояние["t"]
        состояние["t"] += шаг
        return текущее

    monkeypatch.setattr(svc, "_elapsed_clock", fake_clock)


async def test_slice_ends_itself_and_queues_the_rest(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Бюджет вышел — заход кончается на границе чата и ставит продолжение."""
    разобрано: list[str] = []

    async def fake_chat(_db, _redis, _client, _limiter, _account, raw_chat, _coverage, _plan):
        разобрано.append(raw_chat["id"])
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(
        svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"})
    )
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)
    # Каждый взгляд на часы двигает их на треть бюджета: первый чат успевает,
    # на следующей границе бюджет уже выбран.
    _часы(monkeypatch, svc.BACKFILL_SLICE_SECONDS / 3)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert разобрано and len(разобрано) < 4, (
        "заход прошёл все чаты — значит бюджет времени не проверяется вовсе"
    )
    assert поставленные, "продолжение не поставлено: загрузка встала бы навсегда"
    _, _, dedupe = поставленные[-1]
    assert dedupe is False, (
        "продолжение поставлено с постоянным идентификатором — ARQ отбросит его "
        "как повтор выполненной задачи, и загрузка встанет тише прежнего"
    )


async def test_slice_keeps_the_resume_point(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Ход работы и разобранные чаты переживают конец захода.

    Сотрись они — продолжение пошло бы с нуля, и загрузка ходила бы по кругу
    по первым чатам, никогда не добираясь до последних.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}, {"id": "c"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)
    _часы(monkeypatch, svc.BACKFILL_SLICE_SECONDS / 3)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    ход = await redis.get(svc._progress_key(account.id))
    assert ход is not None, "ход работы стёрт — продолжение начнёт с нуля"
    состояние = svc._parse_progress(ход)
    assert состояние is not None and состояние["phase"] == "loading"
    assert await redis.scard(svc._seen_key(account.id)) > 0, (
        "множество разобранных чатов стёрто — продолжение перекачает их заново"
    )


async def test_slice_end_is_silent(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Конец захода — не событие для человека.

    Загрузка идёт, число на карточке растёт. Сообщать «заход №7 закончился»
    значит превратить исправную работу в поток тревог, а среди них потеряется
    единственное сообщение, которое читать обязательно, — про настоящий срыв.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}, {"id": "c"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)
    _часы(monkeypatch, svc.BACKFILL_SLICE_SECONDS / 3)

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert тихо == [], f"конец захода разбудил человека: {тихо}"


async def test_short_run_still_finishes_and_reports(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Канал, который влезает в один заход, заканчивается КАК РАНЬШЕ.

    Нарезка не должна превратить каждую загрузку в бесконечную цепочку: мелкий
    канал обязан дойти до конца, отчитаться и стереть ход работы.
    """

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)
    _часы(monkeypatch, 0.0)  # время не идёт — бюджет не выбирается никогда

    await svc.backfill_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)

    assert поставленные == [], "мелкий канал поставил продолжение — цепочка не кончится"
    assert await redis.get(svc._progress_key(account.id)) is None, (
        "ход работы остался — карточка канала будет вечно показывать загрузку"
    )
    assert тихо and тихо[-1]["title"] == "История загружена"


async def test_second_slice_skips_the_census(
    monkeypatch, db_sessionmaker, redis, account, тихо, поставленные
) -> None:
    """Перепись — один раз на загрузку, а не в начале каждого захода.

    Она обходит весь список чатов страницами по сотне: на канале с тысячей
    чатов это одиннадцать запросов к Авито. Гонять их заново в начале каждого
    захода значило бы отдать переписи заметную часть того самого бюджета, ради
    которого заход и нарезан.
    """
    переписей = {"n": 0}

    async def fake_census(*_args, **_kw):
        переписей["n"] += 1
        return 3

    async def fake_chat(*_args, **_kw):
        return svc.ChatResult(loaded=True)

    monkeypatch.setattr(svc, "_census_chats", fake_census)
    monkeypatch.setattr(svc, "_avito_call", _chats({"id": "a"}, {"id": "b"}, {"id": "c"}))
    monkeypatch.setattr(svc, "_backfill_chat", fake_chat)
    _часы(monkeypatch, svc.BACKFILL_SLICE_SECONDS / 3)

    ctx = {"db_session_factory": db_sessionmaker, "redis": redis}
    await svc.backfill_account(ctx, account.id)
    assert переписей["n"] == 1
    await svc.backfill_account(ctx, account.id)
    assert переписей["n"] == 1, "второй заход пересчитал чаты заново"
