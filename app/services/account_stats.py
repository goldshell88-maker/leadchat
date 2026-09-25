"""Сводка по каналу прямо в списке каналов (просьба заказчика от 7 августа).

ЗАЧЕМ. На экране каналов сейчас видно только «подключён / токен активен».
Вопрос, который на самом деле задают, — другой: «какой канал приносит
обращения, а какой мы теряем». Ответ на него живёт в разделе статистики, за
двумя фильтрами, и потому его не смотрят. Здесь он попадает туда, где на
каналы и так глядят.

ЧТО СЧИТАЕМ. Две величины и семь столбиков:

* **всего** — обращений за неделю. Не «сообщений»: канал меряется тем,
  сколько людей написали, а не сколько раз;
* **без ответа** — из них те, где НИ ОДИН оператор не ответил ни разу.
  Именно это в Jivo названо «пропущенным обращением». Число рядом с общим
  сразу отвечает, чем канал болен: мало обращений или мало ответов;
* **столбики по дням** — форма недели. Провал в среду виден глазом и без
  чисел; он же обычно объясняется чем-то за пределами системы (выходной,
  сломанный канал, отпуск).

ПОЧЕМУ ПО МАТЕРИАЛИЗОВАННОМУ ВИДУ. `mv_conversation_stats` уже считает
`first_client_at` и `has_operator_reply` по всем диалогам и обновляется раз в
час. Считать то же самое запросом по `messages` значило бы на каждом открытии
экрана каналов пройти таблицу в 2,8 миллиона строк — при девяти каналах
девять раз.

ЦЕНА СВЕЖЕСТИ. Вид обновляется ежечасно, поэтому сводка отстаёт на час, и
это честно указывается рядом. Для вопроса «какой канал теряет обращения»
точность до часа избыточна; для вопроса «что происходит прямо сейчас» есть
очередь «Входящие», и она живая.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


#: Окно сводки. Неделя — не круглое число, а осмысленное: короче не видно
#: недельного ритма (выходные), длиннее — форма перестаёт читаться в столбиках
#: шириной в пару пикселей.
WINDOW_DAYS = 7


@dataclass(frozen=True)
class AccountSummary:
    account_id: uuid.UUID
    total: int
    missed: int
    #: По одному числу на день, от старого к новому. Длина всегда WINDOW_DAYS —
    #: день без обращений это ноль, а не пропуск: иначе столбики «съезжают» и
    #: форма недели врёт.
    daily: list[int]


_SQL = sa.text(
    """
    SELECT account_id,
           -- Один AT TIME ZONE: колонка timestamptz, и он сразу даёт московские
           -- стенные часы. Второй (было до 24.09) читал уже наивное UTC-время
           -- как московское и сдвигал на три часа назад — обращения с 00:00 до
           -- 03:00 МСК уходили во вчерашний столбик, а с первого дня окна
           -- выпадали из столбиков совсем, оставаясь в итоге.
           (first_client_at AT TIME ZONE :tz)::date AS day,
           count(*) AS total,
           count(*) FILTER (WHERE NOT has_operator_reply) AS missed
    FROM mv_conversation_stats
    WHERE first_client_at >= :ts_from
    GROUP BY account_id, day
    """
)


async def summaries(
    db: AsyncSession, *, now: datetime | None = None, tz: str = "Europe/Moscow"
) -> dict[uuid.UUID, AccountSummary]:
    """Сводка по всем каналам разом.

    Один запрос на весь экран, а не по запросу на канал: девять каналов —
    девять походов в базу ради девяти строчек.

    День считается по Москве (06 §0.1): «сегодня» у руководителя и «сегодня»
    в отчёте обязаны совпадать, иначе утренний провал столбика объясняется
    часовым поясом, а не работой.
    """
    # СВОДКА НЕ ОБЯЗАНА РАБОТАТЬ ВСЕГДА, А СПИСОК КАНАЛОВ — ОБЯЗАН.
    #
    # Запрос опирается на материализованный вид и на `AT TIME ZONE`: первого
    # нет, пока не отработала миграция статистики, второго нет в SQLite (на нём
    # идут юнит-тесты). Уронить из-за этого весь экран каналов значило бы
    # поменять полезную мелочь на рабочий инструмент: без сводки администратор
    # канал подключит, без списка — нет.
    if _dialect(db) != "postgresql":
        return {}

    # ОКНО СЧИТАЕТСЯ В ТОМ ЖЕ ПОЯСЕ, В КОТОРОМ ПОДПИСАНЫ СТОЛБИКИ.
    #
    # Раньше начало окна бралось как `moment.date()` — то есть по UTC, — а день
    # у столбика приходит из запроса уже по Москве (`AT TIME ZONE :tz`). Три
    # часа разницы ничего не значат днём и ломают весь график ночью: с полуночи
    # до трёх по Москве московское «сегодня» в списке дней ОТСУТСТВУЕТ, и
    # сегодняшний столбик молча теряется — итог показывает обращения, которых
    # на графике нет. Ночью у нас работают: клиента Ивана отклонили в 03:25.
    zone = ZoneInfo(tz)
    moment = now or datetime.now(UTC)
    start_day = moment.astimezone(zone).date() - timedelta(days=WINDOW_DAYS - 1)
    window_from = datetime.combine(start_day, datetime.min.time(), tzinfo=zone)
    try:
        # Точка сохранения, а не голый запрос: если вида ещё нет, Postgres
        # объявляет сломанной всю транзакцию и отвергает КАЖДЫЙ следующий
        # запрос. Точка сохранения ограничивает сбой сводкой. Откатывать всю
        # сессию нельзя тем более — это гасит уже загруженные каналы, и они
        # полезут перечитывать себя из базы посреди сборки ответа.
        async with db.begin_nested():
            rows = (
                await db.execute(
                    _SQL,
                    {"ts_from": window_from, "tz": tz},
                )
            ).all()
    except (ProgrammingError, OperationalError):
        log.warning("account_stats.unavailable")
        return {}

    days: list[date] = [start_day + timedelta(days=i) for i in range(WINDOW_DAYS)]
    index = {d: i for i, d in enumerate(days)}

    acc: dict[uuid.UUID, dict] = {}
    for row in rows:
        entry = acc.setdefault(
            row.account_id, {"total": 0, "missed": 0, "daily": [0] * WINDOW_DAYS}
        )
        entry["total"] += int(row.total)
        entry["missed"] += int(row.missed)
        slot = index.get(row.day)
        if slot is not None:
            entry["daily"][slot] += int(row.total)

    return {
        account_id: AccountSummary(
            account_id=account_id,
            total=data["total"],
            missed=data["missed"],
            daily=data["daily"],
        )
        for account_id, data in acc.items()
    }


def empty(account_id: uuid.UUID) -> AccountSummary:
    """Канал без обращений за неделю.

    Отдаётся явно, а не как отсутствие ключа: пустая сводка и «не посчитали» —
    разные вещи, и на экране они должны выглядеть по-разному.
    """
    return AccountSummary(account_id=account_id, total=0, missed=0, daily=[0] * WINDOW_DAYS)
