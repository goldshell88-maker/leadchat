"""Партиции `messages` на границах: год истории и пустой диапазон.

Настоящую вставку в партиционированную таблицу проверяет
`tests/integration/test_partitions_pg.py` — там живой PostgreSQL. Здесь
проверяется логика вокруг неё, которая на PostgreSQL не видна: сколько раз
берётся тяжёлая блокировка, что происходит, когда обеспечивать нечего, и не
падает ли сама задача обеспечения на пустом списке.

Повод — требование владельца от 11 августа грузить ВСЮ историю: до него
старше текущего месяца ничего не приходило, и оба края были умозрительными.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.scheduler import partitions as mod

pytestmark = pytest.mark.anyio


class _PgLike:
    """Движок, который считает себя PostgreSQL: партиции — только про него."""

    dialect = SimpleNamespace(name="postgresql")


@pytest.fixture
def created(monkeypatch) -> list[list]:
    calls: list[list] = []

    async def fake_create(_engine, months):
        months = list(months)
        calls.append(months)
        return [mod.partition_name(m) for m in months]

    monkeypatch.setattr(mod, "_create", fake_create)
    return calls


async def test_year_of_history_covers_every_month_once(created) -> None:
    """Тринадцать месяцев подряд — тринадцать партиций и тринадцать походов.

    Ни одного лишнего: `CREATE TABLE ... PARTITION OF` берёт тяжёлую
    блокировку на родительской таблице, и звать его на каждое из десятков
    тысяч сообщений года значило бы держать приём новых сообщений в очереди
    весь прогон.
    """
    coverage = mod.PartitionCoverage(_PgLike())
    # ⚠ ШАГ ПО КАЛЕНДАРНЫМ МЕСЯЦАМ, А НЕ ПО 30 ДНЕЙ (28.08).
    #
    # Здесь стояло `now - timedelta(days=30 * n)`, и это была бомба с часовым
    # механизмом того же рода, что уже разбирали в ChannelCardFacts. Тринадцать
    # шагов по 30 дней покрывают 390 дней, но НЕ тринадцать месяцев: февраль
    # короче шага, а месяцы по 31 дню ловятся дважды. 28 августа 2026 набор дал
    # одиннадцать месяцев, и тест покраснел, ничего не сломав в продукте.
    #
    # Свойство, которое здесь проверяется, — «один месяц, один поход за DDL», а
    # не «тринадцать произвольных моментов». Берём месяцы прямо.
    now = datetime.now(UTC).replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    moments = []
    год, месяц = now.year, now.month
    for _ in range(13):
        moments.append(now.replace(year=год, month=месяц))
        месяц -= 1
        if месяц == 0:
            месяц, год = 12, год - 1

    await coverage.ensure_all(moments)
    await coverage.ensure_all(moments)  # повтор бесплатен

    months = {(m[0].year, m[0].month) for m in created}
    assert len(months) == 13, f"тринадцать месяцев — тринадцать партиций, а вышло {len(months)}"
    assert len(created) == len(months), "один месяц — один поход за DDL"
    assert coverage.covered_months == len(months)


async def test_empty_range_asks_the_database_for_nothing(created) -> None:
    """Пустой диапазон — не ошибка, а обычный случай: чат без сообщений."""
    coverage = mod.PartitionCoverage(_PgLike())

    await coverage.ensure_all([])
    await coverage.ensure(None)

    assert created == []
    assert coverage.covered_months == 0


async def test_ensure_partitions_survives_an_empty_month_list(monkeypatch) -> None:
    """Ежедневная задача не падает, когда обеспечивать нечего.

    В строке лога стояло `first=ensured[0]` без проверки — на пустом списке
    это IndexError, то есть падение задачи, единственный смысл которой в том,
    чтобы не падало всё остальное.
    """

    async def empty_create(_engine, months):
        list(months)
        return []

    monkeypatch.setattr(mod, "_create", empty_create)

    assert await mod.ensure_message_partitions(_PgLike()) == []


async def test_sqlite_is_left_alone(created) -> None:
    """В SQLite партиций нет вовсе — попытка создать их роняет юнит-тесты."""
    sqlite_like = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    coverage = mod.PartitionCoverage(sqlite_like)
    await coverage.ensure(datetime.now(UTC))

    assert created == []
    assert await mod.ensure_message_partitions(sqlite_like) == []


async def test_live_webhook_covers_the_month_of_an_old_event(created, monkeypatch) -> None:
    """Живой путь вебхука тоже обеспечивает партицию (правка 18.08).

    ЧТО БЫЛО. Загрузка истории и сверка страховались `PartitionCoverage`, а
    вебхук — нет. Событие со старой меткой (архивный чат, переигранное
    событие площадки, сбитые часы на той стороне) роняло вставку с
    «no partition found»: запись не ack'алась, пять доставок подряд бились
    об одну ошибку и уходили в DLQ — сообщение клиента терялось молча.
    """
    from app.workers import inbound as worker

    monkeypatch.setattr(worker._PARTITIONS, "_engine", _PgLike())
    старая_метка = datetime.now(UTC) - timedelta(days=400)

    await worker._PARTITIONS.ensure(старая_метка)
    await worker._PARTITIONS.ensure(старая_метка)  # повтор бесплатен: лок раз на месяц

    assert len(created) == 1, "партиция создаётся один раз на месяц, а не на сообщение"
    assert created[0][0].year == старая_метка.year
    assert created[0][0].month == старая_метка.month
