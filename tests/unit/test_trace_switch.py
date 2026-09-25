"""Подробный след пути сообщения: включается на время и никого не ломает.

ГЛАВНОЕ ЗДЕСЬ — НЕ СЛЕД, А ТРАНЗАКЦИЯ. Чтение настройки открывает транзакцию
неявно; поставь его перед кодом, который начинает свою (`async with db.begin()`),
и падает ВЕСЬ приём входящих — каждое сообщение клиента. Эта ловушка сработала
уже трижды: в тике бота, в моём посеве стенда и в конвейере воркера, куда я сам
её и внёс час назад. Ни разу она не была видна глазами: код читается правильно.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import trace
from app.services import app_settings


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    trace.drop_cache()


async def test_trace_is_silent_until_switched_on(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """По умолчанию след молчит.

    Умолчание здесь — не осторожность, а стоимость: на боевом идут десятки
    тысяч сообщений в сутки, и включённый по умолчанию след означал бы журналы
    на порядок больше плюс переписку клиентов, лежащую в логах дольше нужного.
    """
    async with db_sessionmaker() as session:
        assert await trace.refresh(session) is False
    assert trace.is_on() is False


async def test_trace_turns_off_by_itself(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Истёкший срок гасит след без чьего-либо участия.

    Ради этого срок и хранится вместо булева переключателя: включённый «на
    посмотреть» и забытый след — это ровно то, чего мы избегали, отказавшись
    делать его постоянным.
    """
    async with db_sessionmaker() as session:
        await app_settings.set_many(
            session, {app_settings.TRACE_UNTIL: int(time.time()) + 3600}, user_id=None
        )
        await session.commit()
        assert await trace.refresh(session) is True

    # Срок в прошлом — тот же ключ, никаких особых «выключить». Свежий ответ
    # отдаётся из памяти процесса (`CACHE_TTL_SECONDS`), поэтому «прошло пять
    # секунд» изображаем сбросом кэша, а не ожиданием.
    trace.drop_cache()
    async with db_sessionmaker() as session:
        await app_settings.set_many(
            session, {app_settings.TRACE_UNTIL: int(time.time()) - 1}, user_id=None
        )
        await session.commit()
        assert await trace.refresh(session) is False


async def test_stale_cache_reads_as_off(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Протухший кэш означает «выключено», а не «как было».

    Разница видна в единственном случае, который и важен: настройку выключили,
    а процесс её ещё не перечитал. «Как было» означало бы, что след живёт
    дольше, чем ему разрешили, — и никто не знает, насколько дольше.
    """
    trace.set_until(time.time() + 3600)
    assert trace.is_on() is True
    # Отматываем часы вперёд подменой отметки чтения: ждать пять секунд в
    # проверке — это пять секунд в каждом прогоне набора.
    read_at, until = trace._cache  # type: ignore[misc]
    trace._cache = (read_at - trace.CACHE_TTL_SECONDS - 1, until)  # type: ignore[misc]
    assert trace.is_on() is False


async def test_a_fresh_answer_does_not_go_to_the_database(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Ответ моложе `CACHE_TTL_SECONDS` — из памяти процесса (проверка 24.09).

    `refresh` зовётся на каждом вебхуке: поход в базу на каждый — соединение из
    общего пула API на каждый приём сообщения.
    """
    trace.set_until(time.time() + 3600)
    async with db_sessionmaker() as session:
        calls = {"execute": 0}
        real = session.execute

        async def counting(*a: object, **kw: object) -> object:
            calls["execute"] += 1
            return await real(*a, **kw)  # type: ignore[arg-type]

        session.execute = counting  # type: ignore[method-assign]
        assert await trace.refresh(session) is True
        assert calls["execute"] == 0


async def test_refresh_leaves_no_open_transaction_behind(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """⚠ ПОСЛЕ ОБНОВЛЕНИЯ СРОКА СЕССИЯ ДОЛЖНА ПУСКАТЬ `db.begin()`.

    Это и есть та ловушка, ради которой файл написан. Чтение настройки открывает
    транзакцию неявно; следующий код, который начинает СВОЮ (`apply_inbound_event`,
    тик бота), падает с «A transaction is already begun» — то есть перестаёт
    доезжать каждое входящее сообщение клиента.

    Проверка ровно на это: обновили срок — и `db.begin()` после этого обязан
    работать. Место вызова в конвейере выбрано так, чтобы обновление шло ВНУТРИ
    уже открытой транзакции (`app/workers/inbound.py`), но охранять надо не
    место, а свойство: место однажды переставят.
    """
    async with db_sessionmaker() as session:
        await trace.refresh(session)
        # Раньше здесь и падало.
        async with session.begin():
            pass


# ------------------------------------------------------- полнота пути и запись


#: Шаги пути сообщения, каждый из которых обязан оставлять отметку.
#:
#: ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ СПИСКОМ, А НЕ ГЛАЗАМИ. Дыра в следе не падает и не
#: подсвечивается — она читается НАОБОРОТ: отсутствие строки выглядит как
#: «сообщение застряло здесь», и разбор уходит искать поломку туда, где всё
#: сработало. Ровно это и было: `delivered` не писался вовсе, и счастливый путь
#: обрывался на предпоследнем шаге.
PATH_STEPS: dict[str, str] = {
    "trace.webhook_in": "app/api/routes/webhooks.py",
    "trace.inbound_stored": "app/services/inbound.py",
    "trace.bot_asks": "app/bots/engine.py",
    "trace.delivered": "app/workers/deliver.py",
    "trace.delivery_failed": "app/workers/deliver.py",
}


@pytest.mark.parametrize("event_name,module", sorted(PATH_STEPS.items()))
def test_every_step_of_the_path_leaves_a_mark(event_name: str, module: str) -> None:
    """Каждый шаг пути пишет свою отметку, и пишет её там, где происходит.

    Поиск не зависит от форматирования: вызовы стоят на разной глубине, и
    сравнение с отступами буквально уже дало ложный отказ на движке — там
    `trace.step` вложен в метод.
    """
    import pathlib
    import re

    source = pathlib.Path(module).read_text(encoding="utf-8")
    found = re.search(rf'trace\.step\(\s*"{re.escape(event_name)}"', source)
    assert found, f"{event_name} не пишется в {module} — в следе появилась дыра"


def test_step_writes_when_on_and_says_nothing_when_off() -> None:
    """Сама запись: включено — строка есть, выключено — нет ни одной.

    Проверяется механизм, а не место: `is_on` уже проверен выше, но между ним и
    строкой в журнале лежит `step`, и молчащий `step` при включённом следе — это
    ровно тот отказ, который выглядит как «ничего не происходит».
    """
    import structlog

    trace.set_until(time.time() + 60)
    with structlog.testing.capture_logs() as logs:
        trace.step("trace.проба", message_id="m-1")
    assert [entry["event"] for entry in logs] == ["trace.проба"]
    assert logs[0]["trace"] is True
    assert logs[0]["message_id"] == "m-1"

    trace.set_until(0.0)
    with structlog.testing.capture_logs() as logs:
        trace.step("trace.проба", message_id="m-2")
    assert logs == []
