"""Помесячные партиции ``messages`` (08 §6.3; 05 §5.3 правило 5).

ЧТО ЗДЕСЬ БЫЛО СЛОМАНО (аудит 7 августа, задача #24). Партиции создавались
ровно на два месяца — текущий и следующий. Для приёма новых сообщений этого
достаточно: они всегда «сегодняшние». Но при подключении боевого аккаунта
Авито система выкачивает переписку ЗА ГОД, и первое же сообщение старше
текущего месяца упиралось в `no partition of relation "messages" found for
row`. Загрузка истории падала на первом старом сообщении, в настройках канала
навсегда оставалось «идёт загрузка», а тем же местом ломалась сверка
пропущенных сообщений: новый чат она читает с самого начала.

ТРИ СЛОЯ ЗАЩИТЫ, И КАЖДЫЙ ЗАКРЫВАЕТ СВОЁ

1. **Широкое окно вперёд и назад** (``ensure_message_partitions``). Ежедневная
   задача обеспечивает два года назад и месяц вперёд. Пустая партиция ничего
   не стоит, а два десятка `CREATE TABLE IF NOT EXISTS` раз в сутки не стоят
   ничего тем более. Этого хватает на обычную загрузку истории, и хватает БЕЗ
   участия кода вставки — то есть работает даже там, где про партиции забыли.
2. **Точное покрытие по факту данных** (``PartitionCoverage``). Год — не
   предел: попадётся чат пятилетней давности, и первый слой промахнётся.
   Поэтому загрузка истории перед вставкой обеспечивает партицию под тот
   месяц, который реально пришёл. Повторные обращения к уже обеспеченному
   месяцу бесплатны — помнит множество в памяти прогона.
3. **Один плохой чат не уносит весь прогон** — это уже в
   ``services/avito_accounts.py``. Здесь важно, что второй слой ошибку НЕ
   глушит: если партицию создать не удалось, звать вставку бессмысленно.

ПОЧЕМУ DDL ОТДЕЛЬНЫМ СОЕДИНЕНИЕМ. ``CREATE TABLE ... PARTITION OF`` берёт на
родительской таблице тяжёлую блокировку. В общей транзакции со вставками она
держалась бы до конца этой транзакции; отдельным соединением — доли секунды.
Плюс независимость: откат вставки не отменяет созданную партицию, а она
пригодится следующему чату.

ЧЕМ ЗА ЭТО ПЛАТИМ, И ЭТО НЕ ТЕОРИЯ. Отдельное соединение не видит транзакций
вызывающего, а блокировки — видит. Новая партиция получает внешний ключ на
``conversations``, то есть DDL просит на ней ``ShareRowExclusiveLock``; он
конфликтует с ``RowExclusiveLock`` любой НЕЗАКРЫТОЙ вставки диалога. Позови
обеспечение изнутри такой транзакции — и DDL будет ждать коммита, а коммит
ждать DDL. Постгрес этот клубок не разрубит: он видит не взаимную блокировку,
а честное ожидание. При этом ждущее соединение уже держит
``AccessExclusiveLock`` на ``messages``, то есть встаёт приём сообщений ВСЕЙ
компании. Ровно так вставала загрузка всей истории 11 августа.

Отсюда два правила. Обеспечивать партиции ДО открытия записывающей транзакции
(порядок вызова — забота вызывающего, см. ``services/avito_accounts.py``), и
не ждать блокировку вечно (:data:`PARTITION_LOCK_TIMEOUT`): отказ виден в
отчёте и лечится повтором, зависание не видно никак.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import session as db_mod

log = structlog.get_logger("app.scheduler.partitions")

#: Насколько глубоко в прошлое держим партиции. Два года — не круглое число, а
#: ответ на вопрос «какую историю мы реально грузим»: Авито отдаёт переписку за
#: всё время, но обращения по ремонту техники старше двух лет мертвы для дела.
#: Дальше работает второй слой — точное покрытие по факту данных.
MONTHS_BACK = 24

#: Месяц вперёд обязателен: партиция следующего месяца должна существовать до
#: наступления первого числа, иначе приём сообщений встанет в полночь.
MONTHS_AHEAD = 1


def _month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _prev_month(d: date) -> date:
    first = d.replace(day=1)
    return (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )


def partition_name(d: date) -> str:
    return f"messages_y{d.year:04d}m{d.month:02d}"


def _partitioned(engine: AsyncEngine) -> bool:
    """Партиции — это про PostgreSQL.

    В SQLite (юнит-тесты) ``messages`` — обычная таблица, и попытка создать
    партицию не просто бесполезна, а роняет тест ошибкой синтаксиса. Тихо
    выходим: отсутствие партиций там не ошибка, а свойство окружения.
    """
    return engine.dialect.name == "postgresql"


#: Сколько ждать блокировку на создание партиции, прежде чем сдаться.
#:
#: ПОЧЕМУ ОЖИДАНИЕ ВООБЩЕ ОГРАНИЧЕНО. `CREATE TABLE ... PARTITION OF messages`
#: берёт AccessExclusiveLock на `messages` и ShareRowExclusiveLock на
#: `conversations` (внешний ключ новой партиции). Если в этот момент кто-то
#: держит на `conversations` незакрытую вставку, DDL встаёт в очередь — а
#: `messages` он к этому моменту УЖЕ занял, и приём сообщений всей компании
#: останавливается. Ждать он при этом может вечно: Постгрес видит здесь не
#: взаимную блокировку, а честное ожидание.
#:
#: Так и случилось при загрузке всей истории (11 августа): вставка диалога
#: открывала транзакцию, обеспечение партиции ждало её коммита, коммит ждал
#: обеспечения. Порядок вызовов починен в `services/avito_accounts.py`, но
#: полагаться на порядок в трёх местах вызова нельзя — здесь стоит замок.
#:
#: Пятнадцать секунд — заведомо больше любой честной очереди на `messages`
#: (вставки живут миллисекунды) и заведомо меньше терпения человека. Отказ
#: виден: чат считается несостоявшимся, попадает в отчёт и берётся повтором.
PARTITION_LOCK_TIMEOUT = "15s"


async def _create(engine: AsyncEngine, months: Iterable[date]) -> list[str]:
    ensured: list[str] = []
    async with engine.begin() as conn:
        # LOCAL — действует до конца этой транзакции и никому больше.
        await conn.execute(text(f"SET LOCAL lock_timeout = '{PARTITION_LOCK_TIMEOUT}'"))
        for first in months:
            start, end = _month_bounds(first)
            name = partition_name(start)
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF messages "
                    f"FOR VALUES FROM ('{start}') TO ('{end}')"
                )
            )
            ensured.append(name)
    return ensured


async def ensure_message_partitions(
    engine: AsyncEngine | None = None,
    *,
    months_back: int = MONTHS_BACK,
    months_ahead: int = MONTHS_AHEAD,
) -> list[str]:
    """Партиции вокруг сегодняшнего дня. Идемпотентно.

    Возвращает имена обеспеченных партиций — для логов и тестов. Запускается
    при старте процесса и ежедневно: выкатка не является условием
    работоспособности записи (INT-10).
    """
    engine = engine or db_mod.init_engine()
    if not _partitioned(engine):
        return []

    today = datetime.now(UTC).date().replace(day=1)
    months: list[date] = []
    cursor = today
    for _ in range(months_back):
        cursor = _prev_month(cursor)
    for _ in range(months_back + months_ahead + 1):
        months.append(cursor)
        cursor = _month_bounds(cursor)[1]

    ensured = await _create(engine, months)
    # ПУСТОЙ ДИАПАЗОН НЕ ДОЛЖЕН РОНЯТЬ ОБЕСПЕЧЕНИЕ ПАРТИЦИЙ. Здесь стояло
    # `first=ensured[0]` без проверки, и на пустом списке это IndexError —
    # то есть падение в задаче, единственный смысл которой в том, чтобы всё
    # остальное не падало. Сегодня список пуст только при бессмысленных
    # аргументах (`months_ahead=-1`), но цена защиты — одна строка, а цена
    # промаха — ежедневная задача, умирающая до создания партиций.
    log.info(
        "partitions.ensured",
        count=len(ensured),
        first=ensured[0] if ensured else None,
        last=ensured[-1] if ensured else None,
    )
    return ensured


class PartitionCoverage:
    """Обеспечивает партицию под КОНКРЕТНУЮ дату, помня уже сделанное.

    Живёт один прогон загрузки истории. Смысл памяти — не в экономии запросов,
    а в блокировке: `CREATE TABLE ... PARTITION OF` берёт тяжёлый лок на
    родительской таблице, и звать его на каждое из десятков тысяч сообщений
    значило бы держать приём новых сообщений в очереди весь прогон.

    Ошибку создания НЕ глушим: если партиции нет и создать её не удалось,
    вставка всё равно упадёт — пусть падает с понятной причиной, а не с
    «no partition found» на десять уровней ниже.
    """

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine
        self._done: set[tuple[int, int]] = set()

    @property
    def covered_months(self) -> int:
        """Сколько месяцев обеспечено за прогон.

        Число попадает в отчёт о загрузке истории и в лог: при загрузке «всей
        истории» именно оно отличает выкачанный год от выкачанной недели, и
        именно по нему видно, что второй слой защиты (#24) реально работал, а
        не молчал потому, что старых сообщений не пришло вовсе.
        """
        return len(self._done)

    @property
    def _resolved(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = db_mod.init_engine()
        return self._engine

    async def ensure(self, moment: datetime | None) -> None:
        """Партиция под месяц этой отметки времени. Повторный вызов бесплатен."""
        if moment is None or not _partitioned(self._resolved):
            return
        key = (moment.year, moment.month)
        if key in self._done:
            return
        first = date(moment.year, moment.month, 1)
        await _create(self._resolved, [first])
        self._done.add(key)
        log.info("partitions.covered", partition=partition_name(first))

    async def ensure_all(self, moments: Iterable[datetime | None]) -> None:
        for moment in moments:
            await self.ensure(moment)
