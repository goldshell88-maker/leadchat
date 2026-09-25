"""Статистика: словарь метрик, SQL, экспорт (06-STATS-REPORTS целиком).

Слой-исполнитель для ``app/api/routes/stats.py``: роутер занимается только
правами, валидацией query и формой ответа, все определения метрик живут
здесь и совпадают с 06 §1 дословно.

Соглашения, без которых цифры не сходятся (06 §0):

* всё в БД — UTC; все бизнес-определения («день», «час», рабочие часы
  10:00–20:00) — в ``Europe/Moscow``. Параметры API приходят датами по
  Москве и превращаются в полуинтервал UTC ``[ts_from, ts_to)``
  (:class:`Period`) — это же даёт partition pruning по ``messages.created_at``;
* любой запрос к партиционированной ``messages`` несёт либо диапазон
  ``created_at``, либо ``conversation_id`` (06 §0.2) — ни одного запроса
  «по всей таблице» здесь нет;
* опциональные фильтры — паттерн ``(CAST(:x AS uuid) IS NULL OR ...)``;
  типы биндов проставляются явно (:func:`_sql`), иначе asyncpg не выведет
  тип NULL. Мультивыбор менеджеров — ``= ANY(CAST(:manager_ids AS uuid[]))``.
  Именно ``CAST(...)``, а не ``::uuid[]``: SQLAlchemy не распознаёт бинд
  перед ``::`` и запрос остаётся без параметров.

Источники (06 §3.3): диалоговые метрики (FRT, «новых за период», медианы в
таблице менеджеров, экспорт «Диалоги») читают ``mv_conversation_stats``
(свежесть ≤ 1 ч, метка ``refreshed_at`` в каждом ответе); всё событийное
(«закрыто», «принято», телефоны, переоткрытия), сообщения, snapshot-счётчики
и виджет «моя статистика» — live.

Два отступления от буквы 06 (осознанные, оба в пользу инвариантов документа):

1. «% закрытых ботом» считается live по эталонному SQL 06 §2.4, а не из MV
   (§3.3 относит его к MV-части). Причина: карточка обязана показывать тот же
   ``closed_total``, что и карточка «закрыто за период» (06 §4.1), а та —
   live из ``audit_log``; из MV она бы отставала на час и разъезжалась.
   Тяжёлой части в §2.4 нет: ``NOT EXISTS`` идёт точечно по
   ``(conversation_id, created_at)`` — легальная вторая ветка правила §0.2.
2. «Повторные клиенты» считаются из MV, а не из ``conv_started`` (06 §2.7б).
   Это ровно та же выборка «диалоги, начавшиеся в периоде», что и
   «новых за период»; брать её из двух разных источников — гарантированный
   рассинхрон карточек в пределах одного ответа.

Эталонный (live) вариант FRT-агрегата из 06 §2.2 тоже реализован —
:data:`_FRT_LIVE_SQL`; интеграционный тест сверяет его с MV-вариантом.
"""

from __future__ import annotations

import contextlib
import csv
import json
import math
import re
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.core.observability import with_job_scope
from app.models.account import AvitoAccount
from app.models.conversation import Conversation
from app.models.user import User
from app.services import app_settings, inbox
from app.services import conversation_status as status_dict
from app.services.audit import write_audit
from app.services.conversation_table import STATUS_RU
from app.services.csv_cells import csv_cell
from app.services.media import READ_ONLY_ERRNOS, signed_media_url

log = structlog.get_logger("app.stats")

MSK = ZoneInfo("Europe/Moscow")

# Рабочие часы (06 §2.1) — УМОЛЧАНИЯ, а не действующие значения (#41).
#
# Настоящие часы живут в `app_settings` и читаются самой SQL-функцией:
# витрина считается в базе, и питоновская реализация здесь — эталон для
# тестов, а не рабочий путь. Числа взяты из объявления настройки, чтобы
# умолчание было в одном месте: разъедься они — эталон начал бы проверять
# арифметику по часам, которых в базе нет.
WORK_START = timedelta(hours=int(app_settings.SPECS[app_settings.STATS_WORK_START_HOUR].default))
WORK_END = timedelta(hours=int(app_settings.SPECS[app_settings.STATS_WORK_END_HOUR].default))

MAX_PERIOD_DAYS = 366  # 06 §4, 01 §9
DEFAULT_PERIOD_DAYS = 30  # 01 §9: «default — последние 30 дней»
MAX_HOUR_GROUP_DAYS = 7  # group=hour разрешён только на коротком периоде

STATS_REFRESHED_KEY = "stats:refreshed_at"  # пишет job refresh_stats_mv (06 §3.2)
HEATMAP_TTL_SECONDS = 600  # 06 §3.4
# ⚠ 120, А НЕ 30 ИЗ 06 §6: ВИДЖЕТ ОПРАШИВАЕТ РАЗ В 60 С, И КЭШ НА 30 С НЕ
# ПОПАДАЛ НИКОГДА. `MyTodayWidget.tsx` (`refetchInterval: 60_000`) приходил
# через минуту в пустой ключ, и каждый опрос каждого из тринадцати шёл в базу:
# GET /stats/my/today p50 49 / p95 130 мс (замер боя 06.09). С двумя минутами
# каждый второй опрос — из Redis, и обновление по фокусу окна тоже. Цена —
# цифры виджета отстают не больше чем на две минуты; это счётчики «за сегодня»,
# а не очередь.
MY_TODAY_TTL_SECONDS = 120

MV = "mv_conversation_stats"


# =============================================================== период и фильтры


def today_msk() -> date:
    return datetime.now(UTC).astimezone(MSK).date()


def msk_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Границы московских суток в UTC — ``[00:00 МСК, 00:00 МСК следующего)``."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=MSK)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Period:
    """Период отчёта: даты по Москве (обе включительно) + UTC-полуинтервал."""

    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    @property
    def ts_from(self) -> datetime:
        return msk_day_bounds(self.date_from)[0]

    @property
    def ts_to(self) -> datetime:
        return msk_day_bounds(self.date_to)[1]

    def previous(self) -> Period:
        """Предыдущий период той же длины, вплотную к текущему (06 §4.1)."""
        prev_to = self.date_from - timedelta(days=1)
        return Period(prev_to - timedelta(days=self.days - 1), prev_to)

    def as_dict(self) -> dict[str, str]:
        return {"date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat()}


def parse_period(date_from: date | None, date_to: date | None) -> Period:
    """Валидация периода (06 §4): ``date_from ≤ date_to``, ≤ 366 дней,
    будущие даты обрезаются до сегодня, дефолт — последние 30 дней."""
    today = today_msk()
    raw_end = date_to or today
    raw_start = date_from or (min(raw_end, today) - timedelta(days=DEFAULT_PERIOD_DAYS - 1))
    # «Перевёрнутость» проверяется по тому, что прислал клиент: иначе диапазон
    # 10–12 августа при «сегодня» 5 августа сначала схлопнулся бы обрезкой и
    # прошёл бы валидацию, а пользователь получил бы молча не тот период.
    if raw_start > raw_end:
        raise ApiError(
            "validation_error",
            # По-русски, как две другие такие же проверки в этом же модуле
            # (TEXT-29). Имена полей API в тексте для человека — это отладочный
            # вывод, случайно оставленный на экране.
            "Начало периода не может быть позже его конца",
            status=400,
            details={"fields": [{"field": "date_from", "rule": "range", "message": ""}]},
        )
    # Обе границы обрезаются до сегодня (06 §4): выбранный в календаре
    # «завтра» — это пустой отчёт за сегодня, а не 400 в лицо.
    end = min(raw_end, today)
    start = min(raw_start, today)
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ApiError(
            "period_too_long",
            f"Период больше {MAX_PERIOD_DAYS} дней — сузьте диапазон",
            status=400,
            details={"limit_days": MAX_PERIOD_DAYS},
        )
    return Period(start, end)


@dataclass(frozen=True, slots=True)
class Filters:
    """Общие фильтры (06 §4): аккаунт + мультивыбор менеджеров."""

    account_id: uuid.UUID | None = None
    manager_ids: tuple[uuid.UUID, ...] = ()

    @property
    def managers(self) -> list[uuid.UUID] | None:
        """None — «фильтра нет» (ветка ``CAST(:manager_ids AS uuid[]) IS NULL``)."""
        return list(self.manager_ids) or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": str(self.account_id) if self.account_id else None,
            "manager_ids": [str(m) for m in self.manager_ids] or None,
        }


# ================================================= рабочие часы (эталон на Python)


def business_seconds_between(
    t0: datetime | None,
    t1: datetime | None,
    work_start: timedelta = WORK_START,
    work_end: timedelta = WORK_END,
) -> int | None:
    """Секунды интервала ``[t0, t1]``, попавшие в рабочие часы МСК.

    Дословный порт SQL-функции ``business_seconds_between`` (06 §2.1) —
    включая её краевые случаи, и это главное: юнит-тесты проверяют семантику
    здесь, интеграционный тест сверяет обе реализации на одних и тех же
    данных, поэтому расхождение SQL и Python не проедет мимо CI.

    * ``NULL`` на входе → ``None`` (SQL-функция объявлена ``STRICT``): диалог
      без ответа не должен попасть в среднее нулём;
    * ``t1 < t0`` → 0 (``generate_series`` даёт пустое множество);
    * выходных нет — 7 дней в неделю (DESIGN §4.2), только часы;
    * **окно не задано → считаем по календарю**, см. ниже.

    ЗАПАСНОЙ ВАРИАНТ: ОКНО НЕ ЗАДАНО. Если конец окна не позже начала, часы
    не сужают ничего — и раньше это давало ноль секунд на любом интервале.
    На боевой системе окно было сохранено как 0:00–0:00, и медиана «первый
    ответ в рабочее время» стала нулём у ВСЕХ менеджеров: отчёт, по которому
    оценивают людей, показывал ноль независимо от их работы. Сохранить такое
    окно теперь нельзя (`app_settings._assert_work_window`), но уже
    сохранённое лежит в базе и продолжает врать до первой правки руками —
    поэтому запасной вариант нужен именно в расчёте, а не только в форме.

    Календарное время — единственный честный ответ на «часы не заданы»:
    ноль означал бы «ответили мгновенно», а это неправда, которую нечем
    отличить от правды. Цифра при этом совпадает с обычным FRT, и расхождение
    двух колонок отчёта исчезает — что само по себе видно и подталкивает
    поправить настройку.

    То же правило дословно повторено в SQL-функции (миграция 0028): считает
    отчёт она, а этот код — эталон для тестов.
    """
    if t0 is None or t1 is None:
        return None
    local0 = _to_msk_naive(t0)
    local1 = _to_msk_naive(t1)
    if work_end <= work_start:
        return max(0, math.floor((local1 - local0).total_seconds() + 0.5))
    day = local0.replace(hour=0, minute=0, second=0, microsecond=0)
    last = local1.replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    while day <= last:  # generate_series(date_trunc(t0), date_trunc(t1), '1 day')
        window_start = max(local0, day + work_start)
        window_end = min(local1, day + work_end)
        total += max(0.0, (window_end - window_start).total_seconds())
        day += timedelta(days=1)
    return math.floor(total + 0.5)  # ::bigint округляет, а total ≥ 0


def _to_msk_naive(value: datetime) -> datetime:
    """``ts AT TIME ZONE 'Europe/Moscow'`` — стенные часы Москвы без tz."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(MSK).replace(tzinfo=None)


# ======================================================================== дельты


def delta_pct(value: float | None, prev: float | None) -> float | None:
    """``(value - prev) / prev * 100``; None при ``prev IN (0, null)`` (06 §4.1)."""
    if value is None or prev in (None, 0):
        return None
    assert prev is not None
    return round((value - prev) / prev * 100, 1)


def delta_pp(value: float | None, prev: float | None) -> float | None:
    """Дельта процентной метрики — в процентных пунктах (06 §4.1)."""
    if value is None or prev is None:
        return None
    return round(value - prev, 1)


def card(value: Any, prev: Any = None, *, comparable: bool = True) -> dict[str, Any]:
    """Карточка «значение + прошлый период». Snapshot-метрики — ``comparable=False``
    («сейчас» не с чем сравнивать, 06 §4.1)."""
    if not comparable:
        return {"value": value, "prev": None, "delta_pct": None}
    return {"value": value, "prev": prev, "delta_pct": delta_pct(value, prev)}


# ===================================================================== SQL-хелперы

_BIND_RE = re.compile(r"(?<![:\w])(?::)([a-z_][a-z0-9_]*)", re.IGNORECASE)

_PARAM_TYPES: dict[str, Any] = {
    "account_id": sa.Uuid(),
    "manager_ids": postgresql.ARRAY(sa.Uuid()),
    "user_id": sa.Uuid(),
    "user_ids": postgresql.ARRAY(sa.Uuid()),
}


def _sql(query: str) -> sa.TextClause:
    """``text()`` с явными типами uuid/uuid[]-биндов (06 §0.4)."""
    names = {m.group(1) for m in _BIND_RE.finditer(query)}
    binds = [
        sa.bindparam(name, type_=type_) for name, type_ in _PARAM_TYPES.items() if name in names
    ]
    stmt = sa.text(query)
    return stmt.bindparams(*binds) if binds else stmt


def _params(period: Period, filters: Filters, **extra: Any) -> dict[str, Any]:
    return {
        "ts_from": period.ts_from,
        "ts_to": period.ts_to,
        "account_id": filters.account_id,
        "manager_ids": filters.managers,
        **extra,
    }


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


async def _row(db: AsyncSession, query: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await db.execute(_sql(query), params)
    row = result.mappings().first()
    return dict(row) if row is not None else {}


async def _scalar(db: AsyncSession, query: str, params: dict[str, Any]) -> Any:
    return (await db.execute(_sql(query), params)).scalar()


# Фильтры, повторяющиеся в каждом запросе (06 §0.4).
_F_MV = (
    "  AND (CAST(:account_id AS uuid) IS NULL OR s.account_id = :account_id)\n"
    "  AND (CAST(:manager_ids AS uuid[]) IS NULL\n"
    "       OR s.assignee_id = ANY(CAST(:manager_ids AS uuid[])))"
)
_F_CONV = (
    "  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)\n"
    "  AND (CAST(:manager_ids AS uuid[]) IS NULL\n"
    "       OR c.assignee_id = ANY(CAST(:manager_ids AS uuid[])))"
)


# =========================================================== FRT + «новых за период»

# Набор колонок один и тот же у обоих источников — это контракт: карточки
# собираются из словаря, а не из позиций.
_FRT_COLUMNS = """
    count(*)                                          AS conversations_started,
    count({t}.frt_operator_sec)                       AS answered_by_operator,
    count({t}.frt_bot_sec)                            AS answered_by_bot,
    count(*) FILTER (WHERE {t}.first_operator_at IS NULL
                       AND {t}.first_bot_at IS NULL)  AS unanswered,
    round(avg({t}.frt_operator_sec))::int             AS frt_operator_avg_sec,
    round((percentile_cont(0.5) WITHIN GROUP
        (ORDER BY {t}.frt_operator_sec))::numeric)::int      AS frt_operator_median_sec,
    round(avg({t}.frt_operator_biz_sec))::int         AS frt_operator_avg_biz_sec,
    round((percentile_cont(0.5) WITHIN GROUP
        (ORDER BY {t}.frt_operator_biz_sec))::numeric)::int  AS frt_operator_median_biz_sec,
    round(avg({t}.frt_bot_sec))::int                  AS frt_bot_avg_sec,
    round((percentile_cont(0.5) WITHIN GROUP
        (ORDER BY {t}.frt_bot_sec))::numeric)::int           AS frt_bot_median_sec
"""

# Боевой путь: materialized view (06 §3.2/§3.3).
_FRT_MV_SQL = f"""
SELECT {_FRT_COLUMNS.format(t="s")}
FROM {MV} s
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
{_F_MV}
"""


# «Диалоги, начавшиеся в периоде», посчитанные live — ОДНО определение на всех.
#
# Выборка нужна двум live-метрикам сразу: FRT-агрегату (эталон 06 §2.2) и
# «повторным клиентам». Держать её двумя копиями нельзя: разъехавшись, они дали
# бы в одном ответе разное число диалогов периода — ту самую болезнь, ради
# которой «повторные клиенты» вообще считаются из того же источника, что и
# «новых за период» (см. шапку модуля).
#
# `client_id` в списке колонок нужен только «повторным клиентам»; FRT-агрегат
# его не использует, и лишним он не становится — группировки здесь нет.
def _conv_started_live(снизу: str = "ts_from") -> str:
    """Тот же CTE с ПОДВИЖНОЙ нижней границей.

    ``снизу`` — имя параметра, а не значение: у полного live-пути это
    ``ts_from``, у раскола (см. :data:`_FRT_SPLIT_SQL`) — ``ts_split``.
    Текст один на оба пути намеренно: разойдясь, копии дали бы в одном
    ответе разное число диалогов периода.
    """
    return f"""
conv_started AS (
    SELECT c.id, c.account_id, c.assignee_id, c.client_id, f.first_client_at
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    WHERE c.last_message_at >= :{снизу}
      AND f.first_client_at >= :{снизу}
      AND f.first_client_at <  :ts_to
{_F_CONV}
)"""


_CONV_STARTED_LIVE_CTE = _conv_started_live()

# Эталон 06 §2.2: те же цифры, посчитанные live по messages. Используется
# тестами (сверка с MV) и как источник для узких срезов без ожидания refresh.
_REPLIES_CTE = """
replies AS (
    SELECT cs.id,
           cs.first_client_at,
           op.first_operator_at,
           bt.first_bot_at,
           EXTRACT(epoch FROM op.first_operator_at - cs.first_client_at)::int
                                                              AS frt_operator_sec,
           business_seconds_between(cs.first_client_at, op.first_operator_at)
                                                              AS frt_operator_biz_sec,
           EXTRACT(epoch FROM bt.first_bot_at - cs.first_client_at)::int
                                                              AS frt_bot_sec
    FROM conv_started cs
    LEFT JOIN LATERAL (
        SELECT min(m.created_at) AS first_operator_at
        FROM messages m
        WHERE m.conversation_id = cs.id
          AND m.created_at >= cs.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'operator'
          AND m.delivery_status <> 'failed'
    ) op ON true
    LEFT JOIN LATERAL (
        SELECT min(m.created_at) AS first_bot_at
        FROM messages m
        WHERE m.conversation_id = cs.id
          AND m.created_at >= cs.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'bot'
          AND m.delivery_status <> 'failed'
    ) bt ON true
)"""

_FRT_LIVE_SQL = f"""
WITH {_CONV_STARTED_LIVE_CTE},
{_REPLIES_CTE}
SELECT {_FRT_COLUMNS.format(t="r")}
FROM replies r
"""

# Раскол: витрина — до полуночи, live — только сегодняшний хвост (замер 29.08).
#
# ЧТО БЫЛО. Период, захватывающий сегодня, считался живьём ЦЕЛИКОМ. На «30
# днях» это 23 тысячи диалогов, и на каждый — три просмотра `messages`
# (первое сообщение клиента, первый ответ оператора, первый ответ бота).
# Замер на бою: 6,77 с из 7,67 с всего ответа `/stats/summary`; страница
# держала восемь пустых плиток девять секунд при каждом открытии, тогда как
# соседние запросы отвечали за 0,05–1,3 с.
#
# ЧТО СТАЛО. Живьём считается только то, чего в витрине заведомо нет, —
# начавшееся сегодня. Всё, что раньше московской полуночи, витрина уже
# посчитала (тот же расчёт по ней занимает 0,010 с).
#
# ЦИФРЫ ТЕ ЖЕ, А НЕ ПОХОЖИЕ. Складываются не агрегаты, а СТРОКИ: медиана из
# двух медиан была бы неправдой, медиана по объединённым строкам — та же
# самая, что и по одному источнику. Охранный тест сверяет раскол с полным
# live цифра в цифру.
#
# Границы половин не пересекаются: витринная берёт `first_client_at` строго
# ДО полуночи, живая — строго ОТ. Диалог не может попасть в обе.
_FRT_SPLIT_SQL = f"""
WITH {_conv_started_live("ts_split")},
{_REPLIES_CTE}
SELECT {_FRT_COLUMNS.format(t="r")}
FROM (
    SELECT r.first_operator_at, r.first_bot_at,
           r.frt_operator_sec, r.frt_operator_biz_sec, r.frt_bot_sec
    FROM replies r
    UNION ALL
    SELECT s.first_operator_at, s.first_bot_at,
           s.frt_operator_sec, s.frt_operator_biz_sec, s.frt_bot_sec
    FROM {MV} s
    WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_split
{_F_MV}
) r
"""


async def frt_aggregate(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    live: bool = False,
    split_at: datetime | None = None,
) -> dict[str, Any]:
    """FRT оператора и бота (06 §1.1/§2.2) + «диалогов новых за период» (§1.2).

    Оператор и бот — два независимых показателя, не «или»: диалог может иметь
    оба, один или ни одного. Диалоги без ответа выпадают из средних и медиан
    (``avg``/``percentile_cont`` игнорируют NULL), но остаются в счётчиках.
    """
    if live and split_at is not None:
        row = await _row(db, _FRT_SPLIT_SQL, _params(period, filters, ts_split=split_at))
    else:
        row = await _row(db, _FRT_LIVE_SQL if live else _FRT_MV_SQL, _params(period, filters))
    return {key: _int(value) for key, value in row.items()}


# ============================================================ snapshot «сейчас»

#: Снимок «прямо сейчас» — ГРУППИРОВКОЙ, а не перечислением `FILTER`.
#:
#: Пока значений было три, два `FILTER` покрывали всё, что нужно. С пятью
#: каждое новое значение требовало бы третьей и четвёртой ветки — и, что хуже,
#: пропущенная ветка не давала бы ошибки: диалог просто не попадал бы НИ В
#: ОДНУ карточку группы. Руководитель видел бы, что открытых диалогов стало
#: меньше, хотя их столько же. По этой цифре решают, звать ли смену.
#:
#: `waiting_now` считается отдельной строкой и остаётся привязанным к
#: `awaiting_since`: это другая величина («клиент ждёт нас»), и складывать её
#: с разбивкой по статусам нельзя — она пересекается с несколькими из них.
_SNAPSHOT_SQL = f"""
SELECT c.status AS status,
       count(*) AS n,
       count(*) FILTER (WHERE c.awaiting_since IS NOT NULL) AS awaiting,
       count(*) FILTER (WHERE c.assignee_id IS NULL) AS unassigned
FROM conversations c
WHERE true
{_F_CONV}
GROUP BY c.status
"""


async def snapshot_now(db: AsyncSession, filters: Filters) -> dict[str, Any]:
    """Снимки «на сейчас» (06 §1.2) — к периоду не привязаны.

    ЧТО БЫЛО. Карточка «Ждут ответа» показывала `count(*) WHERE status='new'`,
    то есть содержимое очереди «Входящие» — диалоги, которых ещё никто не взял.
    Тултип же обещал «диалоги в работе, где клиент написал и ответа не получил».
    Две разные величины под одной подписью, и та, что на экране, вдобавок
    дублировала бейдж вкладки «Входящие» в чатах (STATS-01, FUNC-114, docs/35
    §1.6). Руководитель считал, что видит невыполненную работу диспетчеров,
    а видел неразобранную очередь.

    ЧТО СТАЛО. Величины разведены: `queue_now` — очередь (та же цифра, что на
    бейдже, но теперь так и подписана), `waiting_now` — клиенты, которые ждут
    ответа в уже взятом диалоге.

    ПОЧЕМУ `awaiting_since`, А НЕ «последнее сообщение — от клиента». Признак
    «клиент ждёт» в системе уже есть и он один: отметка ставится на входящем,
    снимается ответом оператора, закрытием и возвратом в очередь, по ней же
    работают чип «ждёт 12 мин» в списке и сторож с напоминаниями. Вывод
    «последним писал клиент» подзапросом дал бы ВТОРОЕ определение того же
    слова, причём худшее в двух местах: он не смотрит на `delivery_status`,
    поэтому неотправленный ответ оператора закрывал бы ожидание (ровно та
    поломка, ради которой написан `messages.restore_awaiting`), и стоил бы
    LATERAL по `messages` на каждый диалог вместо частичного индекса
    `WHERE awaiting_since IS NOT NULL`.
    """
    rows = (
        await db.execute(
            _sql(_SNAPSHOT_SQL),
            {"account_id": filters.account_id, "manager_ids": filters.managers},
        )
    ).mappings()
    # ⚠ ОЧЕРЕДЬ СЧИТАЕТСЯ ТЕМ ЖЕ ПРЕДИКАТОМ, ЧТО И САМА ОЧЕРЕДЬ.
    #
    # Здесь стояло `queue_now = by_status["new"]` — «сколько диалогов в статусе
    # Новый». Это НЕ очередь: `inbox.queue_condition()` требует ещё трёх вещей —
    # нет принявшего, нет ответственного, диалог реально ставили в очередь
    # (`offered_at`), — и с 16.08 исключает диалоги под ботом. Подпись же
    # обещает равенство прямым текстом: тултип карточки говорит «то же число,
    # что на вкладке „Входящие"».
    #
    # На девяти каналах с работающим квалификатором расхождение постоянное:
    # диалог, который ведёт бот, в карточку попадал, а в бейдж — нет.
    # Руководитель сверял два числа на соседних экранах и не сходился.
    #
    # Отдельным запросом, а не вторым предикатом в SQL, НАМЕРЕННО: правило
    # очереди должно жить в одном месте. Копия в тексте запроса разъехалась бы
    # с оригиналом на первой же правке — так уже вышло с этой карточкой.
    очередь = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Conversation)
            .where(
                inbox.queue_condition(),
                sa.or_(
                    sa.literal(filters.account_id is None),
                    Conversation.account_id == filters.account_id,
                ),
            )
        )
    ).scalar_one()
    # Нули для ВСЕХ значений словаря, а не только для встреченных: карточка,
    # которая то появляется, то исчезает вместе с диалогами, читается как
    # поломка вёрстки. Незнакомое значение (в базе до 0026 ограничения не
    # было) отбрасываем не молча — оно попадёт в сумму `open_now` только если
    # известно, и расхождение будет видно.
    by_status = dict.fromkeys(status_dict.STATUSES, 0)
    awaiting_total = 0
    in_progress_unassigned = 0
    for row in rows:
        code = str(row["status"])
        if code in by_status:
            by_status[code] = int(row["n"] or 0)
        if code == "in_progress":
            in_progress_unassigned = int(row["unassigned"] or 0)
        # «Клиент ждёт нас» считается ТОЛЬКО по диалогам, которые кто-то ведёт.
        #
        # Отметка `awaiting_since` стоит и у диалогов очереди — их никто не
        # брал, и клиент там ждёт не конкретного оператора, а вообще всех.
        # Сложи мы всё подряд, карточка «Ждут ответа» снова стала бы суммой
        # очереди и работы: ровно тот дефект STATS-01, который в этом файле
        # разбирался неделю назад и починен строчкой ниже.
        if code in status_dict.WORKED_STATUSES:
            awaiting_total += int(row["awaiting"] or 0)
    return {
        "by_status": by_status,
        # Старые ключи СОХРАНЕНЫ намеренно — их читает нынешний фронт
        # (`SummaryCards.tsx`) и лист «Сводка» выгрузки. Ломать контракт в
        # одном релизе с миграцией незачем: выкатка идёт четырьмя шагами, и
        # между шагами старый клиент обязан работать (docs/38, expand-contract).
        "queue_now": int(очередь or 0),
        "in_progress_now": by_status["in_progress"],
        # «В работе» без ответственного (проверка 24.09): подсказка карточки
        # обещала «взятые кем-то из сотрудников», а в числе были и ничьи — на
        # бою 51 из 305. Число карточки не трогаем (оно входит в `open_now`),
        # а ничьи называем рядом.
        "in_progress_unassigned": in_progress_unassigned,
        # «Клиент ждёт нас» — по отметке `awaiting_since`, а не по статусу.
        # Отложенный и закрытый диалоги отметку гасят, поэтому в сумму они не
        # попадут; складывать это число с разбивкой по статусам всё равно
        # нельзя — величины пересекаются.
        "waiting_now": awaiting_total,
        # Сумма всех открытых. Заводится ради проверяемости: до docs/38 два
        # числа группы «Прямо сейчас» не давали в сумме ничего, и сверить их
        # было не с чем.
        "open_now": sum(by_status[s] for s in status_dict.OPEN_STATUSES),
    }


# ================================================================ закрыто за период

_CLOSED_SQL = """
SELECT count(DISTINCT a.entity_id) AS conversations_closed
FROM audit_log a
JOIN conversations c ON c.id = a.entity_id::uuid
WHERE a.action = 'conversation.status_changed'
  AND a.entity = 'conversation'
  AND a.details->>'to' = 'closed'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR (a.details->>'assignee_id')::uuid = ANY(CAST(:manager_ids AS uuid[])))
"""


async def conversations_closed(db: AsyncSession, period: Period, filters: Filters) -> int:
    """Уникальных диалогов, закрытых в периоде (06 §1.2/§2.3).

    Фильтр по менеджеру — по снимку ``details->>'assignee_id'`` (кто вёл диалог
    на момент закрытия), а не по мутабельному ``conversations.assignee_id``:
    отчёт за прошлый месяц не должен «переезжать» при переназначениях.
    """
    return int(await _scalar(db, _CLOSED_SQL, _params(period, filters)) or 0)


# =========================================================== % закрытых ботом

_BOT_CLOSED_SQL = """
WITH last_close AS (
    SELECT DISTINCT ON (a.entity_id)
           a.entity_id::uuid   AS conversation_id,
           a.details->>'by'    AS closed_by
    FROM audit_log a
    WHERE a.action = 'conversation.status_changed'
      AND a.entity = 'conversation'
      AND a.details->>'to' = 'closed'
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      -- Фильтр по менеджеру — по СНИМКУ ответственного в событии, ровно как в
      -- «закрыто за период» (06 §2.3): иначе closed_total этой карточки и
      -- карточка «закрыто» в одном ответе /stats/summary разъезжаются на
      -- каждом переданном диалоге (у conversations.assignee_id снимка нет).
      AND (CAST(:manager_ids AS uuid[]) IS NULL
           OR (a.details->>'assignee_id')::uuid = ANY(CAST(:manager_ids AS uuid[])))
    ORDER BY a.entity_id, a.created_at DESC
),
flagged AS (
    SELECT lc.conversation_id,
           (lc.closed_by = 'bot'
            AND NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.conversation_id = lc.conversation_id
                  AND m.direction = 'out' AND m.sender_type = 'operator'
            )) AS bot_only
    FROM last_close lc
    JOIN conversations c ON c.id = lc.conversation_id
    WHERE (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
)
SELECT count(*)                         AS closed_total,
       count(*) FILTER (WHERE bot_only) AS closed_by_bot,
       round(100.0 * count(*) FILTER (WHERE bot_only)
             / NULLIF(count(*), 0), 1)  AS bot_closed_pct
FROM flagged
"""


async def bot_closed(db: AsyncSession, period: Period, filters: Filters) -> dict[str, Any]:
    """Доля диалогов, закрытых ботом без единого сообщения оператора (06 §1.3).

    Заметка оператора (``direction='note'``) участием не считается — клиент её
    не видел. Диалог, закрытый ботом и переоткрытый, а затем закрытый
    оператором, в числитель не входит: берётся ПОСЛЕДНЕЕ закрытие в периоде.

    При фильтре по менеджеру «последнее закрытие» — последнее из закрытий
    ЭТОГО менеджера (снимок ``details->>'assignee_id'``), поэтому
    ``closed_total`` здесь всегда равен карточке «закрыто за период»
    (:func:`conversations_closed`) при любых фильтрах — инвариант 06 §4.1.
    """
    row = await _row(db, _BOT_CLOSED_SQL, _params(period, filters))
    pct = row.get("bot_closed_pct")
    return {
        "closed_total": int(row.get("closed_total") or 0),
        "closed_by_bot": int(row.get("closed_by_bot") or 0),
        "pct": float(pct) if pct is not None else None,
    }


# ============================================================== собрано телефонов

#: Какие события «получен телефон» считаются собранными (06 §1.4). Одно условие
#: на карточку и на график: номера из догона истории (19.09) отбрасывала только
#: карточка, и в день подключения канала на одном экране стояли 250 в карточке
#: и 2 000+ на графике (проверка 24.09).
#:
#: Номера из догона не считаются: событие случилось, когда клиент назвал номер,
#: а строка легла временем импорта — день подключения канала иначе показывал бы
#: месяц чужих номеров. Признак пишет `clients.absorb_phones` (`ИСТОРИЯ_СТАРШЕ`);
#: у строк до 19.09 ключа нет — они считаются, как считались.
_PHONE_CAPTURED_SQL = """a.action = 'client.phone_captured'
  AND NOT COALESCE((a.details->>'history')::boolean, false)"""

_PHONES_SQL = f"""
SELECT count(*)                                               AS phones_collected,
       count(*) FILTER (WHERE a.details->>'source' = 'bot')    AS by_bot,
       count(*) FILTER (WHERE a.details->>'source' = 'regex')  AS by_regex,
       count(*) FILTER (WHERE a.details->>'source' = 'manual') AS by_manual
FROM audit_log a
LEFT JOIN conversations c ON c.id = (a.details->>'conversation_id')::uuid
WHERE {_PHONE_CAPTURED_SQL}
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  -- Менеджер — это АКТОР события (кто внёс телефон), а не ответственный за
  -- диалог: у «собрано телефонов» смысл «сделал сотрудник», а не «случилось
  -- в его диалоге». Следствие: под фильтром по менеджеру источники bot/regex
  -- дают нули — у них actor NULL (06 §0.3).
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR a.user_id = ANY(CAST(:manager_ids AS uuid[])))
"""


async def phones_collected(db: AsyncSession, period: Period, filters: Filters) -> dict[str, Any]:
    """Событий ``client.phone_captured`` за период с разбивкой по источникам (06 §1.4).

    Событие пишется один раз при ПЕРВОМ заполнении телефона клиента, поэтому
    метрика не накручивается повторами. Фильтр по менеджеру считает автора
    события (см. SQL) — под ним остаются только ``source='manual'``. Захваты из
    догона истории (``details.history``) в отчёт не входят (см. SQL).
    """
    row = await _row(db, _PHONES_SQL, _params(period, filters))
    return {
        "value": int(row.get("phones_collected") or 0),
        "by_source": {
            "bot": int(row.get("by_bot") or 0),
            "regex": int(row.get("by_regex") or 0),
            "manual": int(row.get("by_manual") or 0),
        },
    }


# ============================================================= повторные обращения

_REOPENED_SQL = """
SELECT count(*) AS reopened
FROM audit_log a
JOIN conversations c ON c.id = a.entity_id::uuid
WHERE a.action = 'conversation.reopened'
  AND a.entity = 'conversation'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR c.assignee_id = ANY(CAST(:manager_ids AS uuid[])))
"""

# ⚠ «ПОВТОРНЫЙ» — ЭТО «У КЛИЕНТА БЫЛ ДИАЛОГ РАНЬШЕ», И ДО 09.09 СЧИТАЛОСЬ НЕ ЭТО.
#
# Здесь стояло `c2.last_message_at < s.first_client_at` — то есть требовалось,
# чтобы прошлый диалог УСПЕЛ ЗАМОЛЧАТЬ до начала нового. У живого потока это
# условие почти никогда не выполняется: старый диалог остаётся открытым, в него
# приходят сообщения, и клиент перестаёт считаться повторным задним числом.
#
# Два следствия, и оба плохие:
#   * ЧИСЛО ЗАНИЖАЛОСЬ ВТРОЕ. Замер по бою за август: 172 против 487;
#   * ЗАКРЫТЫЙ ПЕРИОД МЕНЯЛСЯ ЗАДНИМ ЧИСЛОМ. `last_message_at` — живая колонка:
#     пришло сообщение в старый диалог — и августовское число уменьшилось.
#     Отчёт, который меняется после того, как месяц кончился, доверия не имеет.
#
# Теперь спрашивается неизменное: есть ли у клиента диалог, НАЧАВШИЙСЯ раньше.
# `first_client_at` в витрине зафиксирован и задним числом не двигается.
#
# ⚠ ПРОШЛОЕ ИЩЕМ В ВИТРИНЕ ДАЖЕ НА ЖИВОМ ПУТИ. У `conversations` нет колонки
# «когда диалог начался» вовсе, а считать её подзапросом по `messages` дорого:
# именно на этой метрике мерили 1,078 с. Витрина отвечает за 21 мс (замер
# 09.09). Цена — клиент, у которого ОБА диалога начались в последний час и
# витрина их ещё не видела: он станет повторным после ближайшего обновления.
# Для вопроса «обращался ли раньше» час погрешности несуществен.
_REPEAT_CLIENTS_SQL = f"""
SELECT count(DISTINCT s.client_id) AS repeat_clients
FROM {MV} s
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
{_F_MV}
  AND EXISTS (
      SELECT 1 FROM {MV} s2
      WHERE s2.client_id = s.client_id
        AND s2.conversation_id <> s.conversation_id
        AND s2.first_client_at < s.first_client_at
  )
"""


# Тот же счёт по live-выборке диалогов периода. Нужен, когда период включает
# сегодня: «повторные клиенты» обязаны считаться по тому же набору диалогов,
# что и «новых за период» (шапка модуля), а тот в этом случае тоже live.
_REPEAT_CLIENTS_LIVE_SQL = f"""
WITH {_CONV_STARTED_LIVE_CTE}
SELECT count(DISTINCT cs.client_id) AS repeat_clients
FROM conv_started cs
WHERE EXISTS (
    SELECT 1 FROM {MV} s2
    WHERE s2.client_id = cs.client_id
      AND s2.conversation_id <> cs.id
      AND s2.first_client_at < cs.first_client_at
)
"""


# Раскол для «повторных клиентов» — по тому же правилу, что и у FRT выше.
#
# ЧТО БЫЛО. `split_at` намеренно НЕ доходил сюда, и довод в шапке
# `_period_metrics` был верен для сложения: метрика считает РАЗНЫХ клиентов,
# а один и тот же клиент пишет и вчера, и сегодня — сложив два счётчика, его
# посчитали бы дважды. Поэтому весь период считался живьём: 1,078 с на «30
# днях» против 0,071 с по витрине (замер на бою 03.09), то есть эта одна
# метрика съедала больше, чем все остальные карточки вместе.
#
# ЧТО СТАЛО. Складываются не счётчики, а МНОЖЕСТВА клиентов, и складывает их
# `UNION` — он же и убирает повтор. Клиент, попавший в обе половины, остаётся
# одной строкой, и `count(*)` поверх даёт ровно то же число, что
# `count(DISTINCT client_id)` по всему периоду живьём. Тот самый довод про
# двойной счёт закрыт не оговоркой, а операцией.
#
# `UNION`, А НЕ `UNION ALL`. Разница здесь — весь смысл правки: `ALL` вернул
# бы как раз задвоенного клиента.
_REPEAT_CLIENTS_SPLIT_SQL = f"""
WITH {_conv_started_live("ts_split")}
SELECT count(*) AS repeat_clients
FROM (
    SELECT cs.client_id
    FROM conv_started cs
    WHERE EXISTS (
        SELECT 1 FROM {MV} s2
        WHERE s2.client_id = cs.client_id
          AND s2.conversation_id <> cs.id
          AND s2.first_client_at < cs.first_client_at
    )
    UNION
    SELECT s.client_id
    FROM {MV} s
    WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_split
{_F_MV}
      AND EXISTS (
          SELECT 1 FROM {MV} s2
          WHERE s2.client_id = s.client_id
            AND s2.conversation_id <> s.conversation_id
            AND s2.first_client_at < s.first_client_at
      )
) u
"""


async def repeat_contacts(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    live: bool = False,
    split_at: datetime | None = None,
) -> dict[str, int]:
    """Переоткрытия (события) и повторные клиенты (06 §1.6/§2.7).

    Переоткрытия считаются событиями, а не диалогами: каждое — реальный
    возврат клиента, один диалог может дать несколько за период.

    ``split_at`` действует только на «повторных клиентов»: «переоткрытия»
    читают ``audit_log`` и витрины не касаются вовсе.
    """
    params = _params(period, filters)
    reopened = int(await _scalar(db, _REOPENED_SQL, params) or 0)
    if live and split_at is not None:
        sql = _REPEAT_CLIENTS_SPLIT_SQL
        params = _params(period, filters, ts_split=split_at)
    else:
        sql = _REPEAT_CLIENTS_LIVE_SQL if live else _REPEAT_CLIENTS_SQL
    repeat_clients = int(await _scalar(db, sql, params) or 0)
    return {"reopened": reopened, "repeat_clients": repeat_clients}


# ==================================================================== /stats/summary


async def refreshed_at(redis: Redis) -> str | None:
    """Метка свежести MV — ``stats:refreshed_at`` (пишет job, 06 §3.2)."""
    try:
        value = await redis.get(STATS_REFRESHED_KEY)
    except Exception:  # noqa: BLE001 — Redis лежит: отчёт важнее метки свежести
        log.warning("stats.refreshed_at_unavailable")
        return None
    return value.decode() if isinstance(value, bytes) else value


async def refreshed_moment(redis: Redis) -> datetime | None:
    """Метка свежести витрины моментом времени; нет метки или она кривая — None."""
    raw = await refreshed_at(redis)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.warning("stats.refreshed_at_unparsable", value=raw)
        return None


async def _period_metrics(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    live: bool = False,
    split_at: datetime | None = None,
) -> dict[str, Any]:
    """Всё, что считается «за период» — один и тот же набор для текущего и
    предыдущего периода (06 §4.1: сравнение выполняется теми же запросами со
    сдвинутыми границами).

    ``live`` действует на две метрики из пяти — только они и читают витрину;
    «закрыто», «% ботом» и «телефоны» живые всегда (события ``audit_log``).

    ``split_at`` уходит в FRT и в «повторных клиентов» — в обе метрики,
    которые читают витрину.

    ⚠ ЗДЕСЬ СТОЯЛА ОГОВОРКА «ТОЛЬКО В FRT», и она была верна ровно до тех
    пор, пока половины СКЛАДЫВАЛИСЬ: «повторные клиенты» считают РАЗНЫХ
    клиентов, а один и тот же клиент пишет и вчера, и сегодня — сумма двух
    счётчиков задвоила бы его. Оговорка обошлась в 1,078 с из 1,066 с всего
    ответа на «30 днях» (замер на бою 03.09): одна эта метрика стоила больше,
    чем все остальные карточки вместе. Теперь половины не складываются, а
    объединяются `UNION`'ом по client_id — разбор в `_REPEAT_CLIENTS_SPLIT_SQL`.
    """
    return {
        "frt": await frt_aggregate(db, period, filters, live=live, split_at=split_at),
        "closed": await conversations_closed(db, period, filters),
        "bot_closed": await bot_closed(db, period, filters),
        "phones": await phones_collected(db, period, filters),
        "repeat": await repeat_contacts(db, period, filters, live=live, split_at=split_at),
    }


def _раскол_живого(period: Period, mv_refreshed_at: str | None) -> datetime | None:
    """Граница, до которой живой период берётся с витрины. ``None`` — считать
    живьём целиком, как раньше.

    Витрине верим ровно в одном случае: её пересчитали УЖЕ ПОСЛЕ московской
    полуночи, значит всё, что началось вчера и раньше, в ней есть. Если job
    пересчёта отвалился и метка старая — молча потерять сутки нельзя, и мы
    возвращаемся к полному live: медленно, но правда.
    """
    if not mv_refreshed_at:
        return None
    try:
        отметка = datetime.fromisoformat(mv_refreshed_at)
    except ValueError:
        log.warning("stats.mv_refreshed_at_unparsed", value=mv_refreshed_at)
        return None
    if отметка.tzinfo is None:
        отметка = отметка.replace(tzinfo=UTC)
    полночь = msk_day_bounds(today_msk())[0]
    # Период целиком внутри сегодня — витринной половины просто нет.
    if отметка < полночь or period.ts_from >= полночь:
        return None
    return полночь


def _витрина_покрывает(period: Period, mv_refreshed_at: str | None) -> bool:
    """Успела ли витрина вобрать ВЕСЬ этот период.

    ⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА ДЛЯ ПРЕДЫДУЩЕГО ПЕРИОДА (аудит 30.08). Текущий
    период сверяется с витриной расколом (`_раскол_живого`): метка старее
    московской полуночи — считаем живьём, «медленно, но правда». Предыдущий
    же брался с витрины ВСЕГДА, и комментарий «он остаётся на витрине всегда»
    верен ровно до тех пор, пока пересчёт жив.

    А если пересчёт умер (или REFRESH упёрся в `statement_timeout` — порог
    деградации назван в самом задании), выходит худшее из возможного: текущие
    числа правильные (сработал живой запасной путь), вчерашние — обрезанные, и
    ДЕЛЬТА на карточках врёт молча. «−40 %» рядом с верной цифрой человек
    читает как падение и идёт разбираться с людьми, а не с витриной.

    Критерий тот же по сути: витрине верим, если её пересчитали ПОСЛЕ конца
    периода — значит всё, что в него попадает, она уже вобрала.
    """
    if not mv_refreshed_at:
        return False
    try:
        отметка = datetime.fromisoformat(mv_refreshed_at)
    except ValueError:
        log.warning("stats.mv_refreshed_at_unparsed", value=mv_refreshed_at)
        return False
    if отметка.tzinfo is None:
        отметка = отметка.replace(tzinfo=UTC)
    return отметка >= period.ts_to


async def summary(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    mv_refreshed_at: str | None = None,
) -> dict[str, Any]:
    """``GET /stats/summary`` — карточки + дельта к предыдущему периоду (06 §4.1).

    ПЕРИОД, ЗАХВАТЫВАЮЩИЙ СЕГОДНЯ, СЧИТАЕТСЯ ЖИВЬЁМ (STATS-04).

    Что было. В группе «За период» две карточки стоят вплотную: «Новые
    диалоги» приходили из витрины, которую пересчитывают раз в час в HH:05, а
    «Закрыто» считалось живьём по ``audit_log``. На пресете «Сегодня» в 10:50
    это значит, что первая показывает состояние на 10:05, а вторая — на 10:50.

    Диалог, который у мастерской пришёл в 10:20 и закрылся в 10:40, попадал в
    «Закрыто» и не попадал в «Новые». Достаточно нескольких таких, и «Закрыто»
    становится БОЛЬШЕ «Новых» — отчёт, противоречащий сам себе на глазах у
    владельца. Метка «Данные на 10:05» стояла одна на весь экран и объясняла
    это ровно наполовину.

    Что стало. Если период захватывает сегодня, витринные метрики считаются
    по эталонному live-SQL (06 §2.2) — тому самому, который интеграционный
    тест сверяет с витриной цифра в цифру. Обе карточки группы становятся
    «на момент открытия», и разойтись им больше не на чем.

    Почему не всегда live: для периода, целиком лежащего в прошлом, витрина
    полна — новых диалогов с таким ``first_client_at`` уже не появится, — и
    платить за пересчёт по ``messages`` там не за что.

    Предыдущий период остаётся на витрине ВСЕГДА: он по построению кончается
    не позже вчерашнего дня (``Period.previous``), то есть попадает ровно в
    тот случай, где витрина полна.
    """
    prev_period = period.previous()
    # Рабочие часы — НАСТРОЙКА, а не константа (#41), и подпись обязана
    # печатать действующие значения. На экране стояло зашитое «10:00–20:00»
    # (FUNC-42): цифра считалась по новым часам, а подписана была старыми, и
    # отличить одно от другого читателю было нечем. Модульные WORK_START /
    # WORK_END для этого не годятся — они умолчания, а не действующие часы.
    start_hour = int(await app_settings.get(db, app_settings.STATS_WORK_START_HOUR))
    end_hour = int(await app_settings.get(db, app_settings.STATS_WORK_END_HOUR))
    # ВЫРОЖДЕННОЕ ОКНО ЧИСЕЛ НЕ ПОЛУЧАЕТ. Сохранить 0:00–0:00 больше нельзя,
    # но уже сохранённое лежит в базе, и метрика в этом случае считается по
    # календарю (`business_seconds_between`). Напечатай подпись «в рабочие
    # часы 00:00–00:00» рядом с календарной цифрой — и она соврёт про то,
    # как посчитано. `null` фронт читает как «часы не пришли» и печатает
    # нейтральное «в рабочие часы», без чисел, которых не было.
    work_hours = (
        {"start_hour": start_hour, "end_hour": end_hour}
        if app_settings.work_window_is_set(start_hour, end_hour)
        else None
    )
    live = period.date_to >= today_msk()
    раскол = _раскол_живого(period, mv_refreshed_at) if live else None
    now = await _period_metrics(db, period, filters, live=live, split_at=раскол)
    # Предыдущий период — с витрины, только если она его успела вобрать;
    # иначе считаем живьём, как и текущий (разбор — в `_витрина_покрывает`).
    prev_live = not _витрина_покрывает(prev_period, mv_refreshed_at)
    prev = await _period_metrics(db, prev_period, filters, live=prev_live)
    snapshot = await snapshot_now(db, filters)

    frt, prev_frt = now["frt"], prev["frt"]
    bot, prev_bot = now["bot_closed"], prev["bot_closed"]

    cards: dict[str, Any] = {
        "conversations_new": card(
            frt.get("conversations_started") or 0, prev_frt.get("conversations_started") or 0
        ),
        "conversations_closed": card(now["closed"], prev["closed"]),
        # Snapshot-метрики: «сейчас» не с чем сравнивать (06 §4.1).
        "in_progress_now": {
            **card(snapshot["in_progress_now"], comparable=False),
            "unassigned": snapshot["in_progress_unassigned"],
        },
        "waiting_now": card(snapshot["waiting_now"], comparable=False),
        # Очередь «Входящие» — отдельной карточкой, а не подменой предыдущей.
        "queue_now": card(snapshot["queue_now"], comparable=False),
        # КАРТОЧКА ГРУППЫ «ПРЯМО СЕЙЧАС» (docs/38 §6.1). Без неё диалоги,
        # переведённые в «Ждёт клиента», не попадали бы НИ В ОДНУ карточку, и
        # руководитель видел бы, что открытых стало меньше, хотя их столько же.
        # Соседняя карточка «Отложено» убрана 12 августа вместе со статусом.
        "waiting_client_now": card(snapshot["by_status"]["waiting_client"], comparable=False),
        "frt_operator": {
            "median_sec": frt.get("frt_operator_median_sec"),
            "avg_sec": frt.get("frt_operator_avg_sec"),
            "median_biz_sec": frt.get("frt_operator_median_biz_sec"),
            "avg_biz_sec": frt.get("frt_operator_avg_biz_sec"),
            "answered": frt.get("answered_by_operator") or 0,
            "unanswered": frt.get("unanswered") or 0,
            "prev_median_sec": prev_frt.get("frt_operator_median_sec"),
            "delta_pct": delta_pct(
                frt.get("frt_operator_median_sec"), prev_frt.get("frt_operator_median_sec")
            ),
        },
        "frt_bot": {
            "median_sec": frt.get("frt_bot_median_sec"),
            "avg_sec": frt.get("frt_bot_avg_sec"),
            "answered": frt.get("answered_by_bot") or 0,
        },
        "bot_closed": {
            "pct": bot["pct"],
            "closed_by_bot": bot["closed_by_bot"],
            "closed_total": bot["closed_total"],
            "prev_pct": prev_bot["pct"],
            # у процентной метрики дельта — в процентных пунктах (06 §4.1)
            "delta_pct": delta_pp(bot["pct"], prev_bot["pct"]),
        },
        "phones_collected": {
            **card(now["phones"]["value"], prev["phones"]["value"]),
            "by_source": now["phones"]["by_source"],
        },
        "repeat_contacts": {
            "reopened": now["repeat"]["reopened"],
            "repeat_clients": now["repeat"]["repeat_clients"],
            "prev_reopened": prev["repeat"]["reopened"],
            "delta_pct": delta_pct(now["repeat"]["reopened"], prev["repeat"]["reopened"]),
        },
        # Полная разбивка «сколько сейчас в каком статусе» — плоским словарём,
        # рядом с карточками. Отдельного отчёта по статусам в продукте не было
        # ни на одном экране; теперь пять чисел в сумме дают все открытые
        # диалоги, и эту сумму можно проверить (охранный тест).
        "by_status": snapshot["by_status"],
        "open_now": snapshot["open_now"],
    }
    return {
        "period": {**period.as_dict(), "tz": "Europe/Moscow"},
        "prev_period": prev_period.as_dict(),
        # Экран обязан сказать человеку, что он читает. `refreshed_at` в ответе
        # называет возраст ВИТРИНЫ, и к этим карточкам он относится не всегда —
        # признак говорит, относится ли (STATS-04).
        "period_live": live,
        # Действующие рабочие часы — чтобы подпись FRT печатала их, а не
        # зашитые в интерфейс (FUNC-42).
        "work_hours": work_hours,
        "cards": cards,
    }


# ================================================================= /stats/timeseries

TIMESERIES_METRICS: tuple[str, ...] = (
    "conversations_new",
    "conversations_closed",
    "messages_in",
    "messages_out",
    "frt_operator_median",
    "phones_collected",
)
TIMESERIES_GROUPS: tuple[str, ...] = ("day", "hour")

# «Пустой день» у медианы — null, а не 0: нулевая медиана и «нет данных» —
# разные вещи (06 §4.2). У счётчиков пустой бакет — 0.
_NULLABLE_METRICS = frozenset({"frt_operator_median"})

_TS_TEMPLATES: dict[str, str] = {
    "conversations_new": f"""
SELECT date_trunc('{{unit}}', s.first_client_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(*)::int AS value
FROM {MV} s
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
{_F_MV}
GROUP BY 1
""",
    "frt_operator_median": f"""
SELECT date_trunc('{{unit}}', s.first_client_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       round((percentile_cont(0.5) WITHIN GROUP
           (ORDER BY s.frt_operator_sec))::numeric)::int AS value
FROM {MV} s
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
{_F_MV}
GROUP BY 1
""",
    "conversations_closed": """
SELECT date_trunc('{unit}', a.created_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(DISTINCT a.entity_id)::int AS value
FROM audit_log a
JOIN conversations c ON c.id = a.entity_id::uuid
WHERE a.action = 'conversation.status_changed'
  AND a.entity = 'conversation'
  AND a.details->>'to' = 'closed'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR (a.details->>'assignee_id')::uuid = ANY(CAST(:manager_ids AS uuid[])))
GROUP BY 1
""",
    "messages_in": """
SELECT date_trunc('{unit}', m.created_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(*)::int AS value
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
  AND m.direction = 'in' AND m.sender_type = 'client'
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR c.assignee_id = ANY(CAST(:manager_ids AS uuid[])))
GROUP BY 1
""",
    # messages_out — исходящие ОПЕРАТОРОВ без failed (06 §1.5).
    #
    # ⚠ ЗДЕСЬ БЫЛО НАПИСАНО «РОВНО ТО ЖЕ ОПРЕДЕЛЕНИЕ, ЧТО И В КОЛОНКЕ
    # „ОТПРАВЛЕНО“ ТАБЛИЦЫ МЕНЕДЖЕРОВ», И ЭТО НЕВЕРНО (разбор 08.09). Условия
    # отбора действительно те же, а собирается результат по-разному: здесь
    # count(*) по всем исходящим, а в таблице — GROUP BY sender_user_id, и
    # сообщения БЕЗ автора (отправленные из приложения Авито) не попадают ни в
    # чью строку, а значит и в ИТОГО. Историческая разница огромна: из 137 735
    # исходящих автор записан у 40 727. Читателю это объясняет подсказка
    # колонки и оговорка под заголовком таблицы (ManagersTable.tsx).
    "messages_out": """
SELECT date_trunc('{unit}', m.created_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(*)::int AS value
FROM messages m
WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
  AND m.direction = 'out' AND m.sender_type = 'operator'
  AND m.delivery_status <> 'failed'
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR m.sender_user_id = ANY(CAST(:manager_ids AS uuid[])))
  AND (CAST(:account_id AS uuid) IS NULL OR EXISTS (
        SELECT 1 FROM conversations c
        WHERE c.id = m.conversation_id AND c.account_id = :account_id))
GROUP BY 1
""",
    "phones_collected": """
SELECT date_trunc('{unit}', a.created_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(*)::int AS value
FROM audit_log a
LEFT JOIN conversations c ON c.id = (a.details->>'conversation_id')::uuid
WHERE """
    + _PHONE_CAPTURED_SQL
    + """
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_ids AS uuid[]) IS NULL
       OR a.user_id = ANY(CAST(:manager_ids AS uuid[])))
GROUP BY 1
""",
}

# ⚠ ЖИВЫЕ БЛИЗНЕЦЫ ДВУХ ШАБЛОНОВ ВЫШЕ (правка 23.08).
#
# `conversations_new` и `frt_operator_median` читались из витрины
# `mv_conversation_stats`, а витрину обновляет задание раз в час, в HH:05.
# Карточка «Новые диалоги» на ТОМ ЖЕ экране считается живьём, когда период
# захватывает сегодня (`summary` → `frt_aggregate(live=True)`). Итог: два числа
# про одну величину рядом друг с другом, и график до часа отстаёт от карточки.
# Руководитель, открывший экран в 10:30, видел в карточке 41, а на графике 36 —
# и не мог знать, какое из них правда.
#
# Живой вариант есть только у этих двух метрик: остальные четыре шаблона и так
# считают по `audit_log`/`messages`, витрины не касаются.
#
# `conv_started` тот же, что у `_FRT_LIVE_SQL`, — второго определения «нового
# диалога» в системе быть не должно. Отсечка `c.last_message_at >= :ts_from`
# внутри него потерь не даёт: у диалога, чьё первое сообщение клиента попало в
# период, последнее сообщение заведомо не раньше первого.
_TS_LIVE_TEMPLATES: dict[str, str] = {
    "conversations_new": f"""
WITH {_CONV_STARTED_LIVE_CTE}
SELECT date_trunc('{{unit}}', cs.first_client_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       count(*)::int AS value
FROM conv_started cs
GROUP BY 1
""",
    "frt_operator_median": f"""
WITH {_CONV_STARTED_LIVE_CTE},
replies AS (
    SELECT cs.first_client_at,
           EXTRACT(epoch FROM op.first_operator_at - cs.first_client_at)::int
                                                              AS frt_operator_sec
    FROM conv_started cs
    LEFT JOIN LATERAL (
        SELECT min(m.created_at) AS first_operator_at
        FROM messages m
        WHERE m.conversation_id = cs.id
          AND m.created_at >= cs.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'operator'
          AND m.delivery_status <> 'failed'
    ) op ON true
)
SELECT date_trunc('{{unit}}', r.first_client_at AT TIME ZONE 'Europe/Moscow') AS bucket,
       round((percentile_cont(0.5) WITHIN GROUP
           (ORDER BY r.frt_operator_sec))::numeric)::int AS value
FROM replies r
GROUP BY 1
""",
}


def _buckets(period: Period, group: str) -> list[datetime]:
    """Сплошная сетка бакетов в московских стенных часах (06 §4.2)."""
    start = datetime.combine(period.date_from, datetime.min.time())
    end = datetime.combine(period.date_to, datetime.min.time()) + timedelta(days=1)
    step = timedelta(days=1) if group == "day" else timedelta(hours=1)
    out: list[datetime] = []
    cursor = start
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out


def _bucket_label(moment: datetime, group: str) -> str:
    return moment.strftime("%Y-%m-%d") if group == "day" else moment.strftime("%Y-%m-%dT%H:00")


async def _ts_rows(
    db: AsyncSession, шаблон: str, group: str, params: dict[str, Any]
) -> dict[str, int | None]:
    """Один прогон шаблона ряда -> {подпись бакета: значение}."""
    rows = (await db.execute(_sql(шаблон.format(unit=group)), params)).all()
    # bucket приходит naive timestamp'ом в московских стенных часах
    return {_bucket_label(row[0], group): _int(row[1]) for row in rows}


async def timeseries(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    metric: str,
    group: str,
    mv_refreshed_at: str | None = None,
) -> dict[str, Any]:
    """``GET /stats/timeseries`` (06 §4.2) — ряд, занулённый до сплошного."""
    if metric not in TIMESERIES_METRICS:
        raise ApiError(
            "validation_error",
            f"Неизвестная метрика: {metric}",
            status=400,
            details={"fields": [{"field": "metric", "rule": "enum", "message": ""}]},
        )
    if group not in TIMESERIES_GROUPS:
        raise ApiError(
            "validation_error",
            f"Неизвестная группировка: {group}",
            status=400,
            details={"fields": [{"field": "group", "rule": "enum", "message": ""}]},
        )
    if group == "hour" and period.days > MAX_HOUR_GROUP_DAYS:
        raise ApiError(
            "validation_error",
            f"Почасовая группировка доступна на периоде до {MAX_HOUR_GROUP_DAYS} дней",
            status=400,
            details={"fields": [{"field": "group", "rule": "period", "message": ""}]},
        )

    # Источник тот же, что у карточки рядом: период захватывает сегодня —
    # считаем живьём, иначе берём витрину (она дешевле и для прошлого точна).
    живьём = period.date_to >= today_msk() and metric in _TS_LIVE_TEMPLATES
    раскол = _раскол_живого(period, mv_refreshed_at) if живьём else None
    if раскол is not None:
        # ⚠ РАСКОЛ ТОТ ЖЕ, ЧТО У КАРТОЧЕК, И ЗДЕСЬ ОН БЕСПЛАТЕН.
        #
        # ЧТО БЫЛО. Период, захватывающий сегодня, считался живьём ЦЕЛИКОМ —
        # ради одного сегодняшнего бакета пересчитывались все тридцать.
        # Замер на бою 03.09: `conversations_new` 0,953 с (по витрине 0,010 с),
        # `frt_operator_median` 4,228 с. График — умолчание экрана `last30`,
        # то есть эту цену платил каждый заход.
        #
        # ПОЧЕМУ ЗДЕСЬ ПРОЩЕ, ЧЕМ У FRT. Там половины пришлось объединять
        # СТРОКАМИ, чтобы медиана осталась медианой. Здесь объединять нечего:
        # раскол — московская полночь (`_раскол_живого`), а бакет у нас сутки
        # или час, то есть граница ложится РОВНО НА СТЫК бакетов. Каждый бакет
        # целиком лежит в одной половине, и словари просто дополняют друг
        # друга. Пересечься ключам не на чем — а если бы могло, `|` взял бы
        # живое значение, то есть более свежее.
        found = await _ts_rows(
            db, _TS_TEMPLATES[metric], group, _params(period, filters, ts_to=раскол)
        )
        found |= await _ts_rows(
            db, _TS_LIVE_TEMPLATES[metric], group, _params(period, filters, ts_from=раскол)
        )
    else:
        found = await _ts_rows(
            db,
            _TS_LIVE_TEMPLATES[metric] if живьём else _TS_TEMPLATES[metric],
            group,
            _params(period, filters),
        )
    empty = None if metric in _NULLABLE_METRICS else 0
    points = [
        {"ts": label, "value": found.get(label, empty)}
        for label in (_bucket_label(b, group) for b in _buckets(period, group))
    ]
    return {"metric": metric, "group": group, "points": points}


# =================================================================== /stats/heatmap

_HEATMAP_SQL = """
SELECT extract(isodow FROM (m.created_at AT TIME ZONE 'Europe/Moscow'))::int AS dow,
       extract(hour   FROM (m.created_at AT TIME ZONE 'Europe/Moscow'))::int AS hour,
       count(*) AS messages_in
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
  AND m.direction = 'in' AND m.sender_type = 'client'
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
GROUP BY 1, 2
ORDER BY 1, 2
"""


def heatmap_cache_key(period: Period, filters: Filters) -> str:
    account = str(filters.account_id) if filters.account_id else "all"
    return f"stats:heatmap:{account}:{period.date_from}:{period.date_to}"


async def heatmap(
    db: AsyncSession, redis: Redis, period: Period, filters: Filters
) -> dict[str, Any]:
    """``GET /stats/heatmap`` — входящие клиентов, 7 × 24, всегда 168 ячеек (06 §3.4/§4.3).

    Фильтр по менеджеру не применяется намеренно: входящие сообщения
    менеджеру не принадлежат (06 §4.3). Кэш Redis 600 с — повторные открытия
    дашборда БД не трогают.
    """
    key = heatmap_cache_key(period, filters)
    cached = await _cache_get(redis, key)
    if cached is not None:
        return cached

    rows = (
        await db.execute(
            _sql(_HEATMAP_SQL),
            {"ts_from": period.ts_from, "ts_to": period.ts_to, "account_id": filters.account_id},
        )
    ).all()
    values = {(int(row[0]), int(row[1])): int(row[2]) for row in rows}
    payload = {
        "tz": "Europe/Moscow",
        "metric": "messages_in",
        # Полная матрица: фронтенду не нужно догадываться о пропусках.
        "cells": [
            {"dow": dow, "hour": hour, "value": values.get((dow, hour), 0)}
            for dow in range(1, 8)
            for hour in range(24)
        ],
    }
    await _cache_set(redis, key, payload, HEATMAP_TTL_SECONDS)
    return payload


async def _cache_get(redis: Redis, key: str) -> dict[str, Any] | None:
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001 — кэш не критичен, считаем заново
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _cache_set(redis: Redis, key: str, payload: dict[str, Any], ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)
    except Exception:  # noqa: BLE001 — не смогли закэшировать: не повод падать
        log.warning("stats.cache_set_failed", key=key)


# ================================================================== /stats/managers

MANAGER_SORTS: tuple[str, ...] = (
    "messages_sent",
    "taken",
    "answered",
    "closed",
    "frt_median_sec",
    "frt_median_biz_sec",
)

# ⚠ ЖИВОЙ ПУТЬ ТАБЛИЦЫ ЛЮДЕЙ — ЧТОБЫ СОСЕДНИЕ КОЛОНКИ ОДНОЙ СТРОКИ НЕ
# РАСХОДИЛИСЬ ВО ВРЕМЕНИ (найдено аудитом 08.09, сделано 09.09).
#
# ЧТО БЫЛО. «Принято», «Закрыто» и «Сообщений» считаются по `audit_log` и
# `messages` — они живые всегда. «Ответил первым» и обе медианы FRT читали
# витрину, которую пересчитывают раз в час. На пресете «Сегодня» в 10:50 это
# значит, что в ОДНОЙ строке одного человека три числа на 10:50 и три на
# 10:05: диспетчер принял диалог и ответил в 10:20 — «Принято» ему
# засчиталось, «Ответил первым» нет. Та же беда, ради которой на живой путь
# перевели карточки «Новые диалоги» и «Закрыто» (STATS-04), только здесь она
# внутри одной строки и потому заметнее.
#
# ЧТО СТАЛО. Тот же приём, что у карточек и графика: период захватывает
# сегодня — считаем живьём, а витрину берём на ту часть периода, которую она
# заведомо вобрала (`_раскол_живого`). Складываются СТРОКИ, а не агрегаты:
# медиана из двух медиан была бы неправдой.
#
# ЦЕНА ЗАМЕРЕНА НА БОЮ 09.09, «30 дней»: витрина 265 мс, раскол 323 мс — за
# правду в шести колонках платим 58 мс. Полный live той же выборки — 6,05 с,
# и он остаётся ЗАПАСНЫМ путём: включается, только если метки свежести нет
# вовсе (пересчёт умер, Redis лёг). Там же и по той же причине оказываются
# карточки — «медленно, но правда» решено ещё 29.08, и второго решения на тот
# же вопрос быть не должно.
#
# ⚠ ФИЛЬТР ПО СОТРУДНИКУ ЗДЕСЬ НЕ ПРИМЕНЯЕТСЯ, И ЭТО НЕ ЗАБЫВЧИВОСТЬ. Витринная
# ветка фильтрует только по каналу, а людей отбирает строка `users` ниже:
# метрика атрибутируется АВТОРУ ПЕРВОГО ОТВЕТА, а не ответственному за диалог
# (06 §1.1.7). Взяв готовый `_conv_started_live`, живая ветка отфильтровала бы
# по `assignee_id` — и считала бы не то же самое, что витринная.


def _frt_rows_mv(*, верх: str, users: bool) -> str:
    """Строки «первый ответ оператора» из витрины: ``[ts_from, верх)``."""
    отбор = "\n      AND s.first_operator_user_id = ANY(CAST(:user_ids AS uuid[]))" if users else ""
    return f"""
    SELECT s.first_operator_user_id AS user_id,
           s.frt_operator_sec, s.frt_operator_biz_sec
    FROM {MV} s
    WHERE s.first_client_at >= :ts_from AND s.first_client_at < :{верх}
      AND s.first_operator_user_id IS NOT NULL{отбор}
      AND (CAST(:account_id AS uuid) IS NULL OR s.account_id = :account_id)"""


def _frt_rows_live(*, снизу: str, users: bool) -> str:
    """Те же строки живьём по ``messages``: ``[снизу, ts_to)``.

    Определение «первого ответа» списано с витрины дословно, включая
    ``ORDER BY created_at LIMIT 1`` и ``delivery_status <> 'failed'``: разойдись
    они — и одно и то же число на одном экране считалось бы двумя способами.
    """
    отбор = (
        "\n      AND op.first_operator_user_id = ANY(CAST(:user_ids AS uuid[]))" if users else ""
    )
    return f"""
    SELECT op.first_operator_user_id AS user_id,
           EXTRACT(epoch FROM op.first_operator_at - f.first_client_at)::int
                                                              AS frt_operator_sec,
           business_seconds_between(f.first_client_at, op.first_operator_at)
                                                              AS frt_operator_biz_sec
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    JOIN LATERAL (
        SELECT m.created_at     AS first_operator_at,
               m.sender_user_id AS first_operator_user_id
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.created_at >= f.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'operator'
          AND m.delivery_status <> 'failed'
        ORDER BY m.created_at
        LIMIT 1
    ) op ON true
    WHERE c.last_message_at >= :{снизу}
      AND f.first_client_at >= :{снизу}
      AND f.first_client_at <  :ts_to
      AND op.first_operator_user_id IS NOT NULL{отбор}
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)"""


def _frt_rows(*, live: bool, split: bool, users: bool) -> str:
    """Источник строк FRT для одного из трёх путей отчёта."""
    if not live:
        return _frt_rows_mv(верх="ts_to", users=users)
    if not split:
        return _frt_rows_live(снизу="ts_from", users=users)
    return (
        _frt_rows_live(снизу="ts_split", users=users)
        + "\n    UNION ALL"
        + _frt_rows_mv(верх="ts_split", users=users)
    )


_MANAGERS_SQL_TEMPLATE = """
WITH frt AS (
    SELECT r.user_id,
           count(*)                                 AS answered,
           round(avg(r.frt_operator_sec))::int      AS frt_avg_sec,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_operator_sec))::numeric)::int      AS frt_median_sec,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_operator_biz_sec))::numeric)::int  AS frt_median_biz_sec
    FROM (@ИСТОЧНИК@
    ) r
    GROUP BY 1
),
taken AS (
    -- DISTINCT по диалогу, а не count(*) по событиям, и это не придирка к
    -- формулировке. `conversation.assigned` пишется на КАЖДЫЙ шаг
    -- назначения: приём из очереди, автоподхват первым ответом,
    -- самоназначение и каждую передачу. Диалог, взятый из очереди и дважды
    -- переданный, давал в колонке «Принято» тройку, и на одном экране
    -- оказывались «Новых диалогов: 5» и «ИТОГО Принято: 7» за тот же период
    -- (жалоба заказчика №9). Соседний CTE `closed` считал DISTINCT с самого
    -- начала — расхождение было именно здесь.
    SELECT (a.details->>'assignee_id')::uuid AS user_id,
           count(DISTINCT a.entity_id) AS taken
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.assigned'
      AND a.entity = 'conversation'
      AND a.details->>'assignee_id' IS NOT NULL
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
    GROUP BY 1
),
closed AS (
    SELECT a.user_id,
           count(DISTINCT a.entity_id) AS closed
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.status_changed'
      AND a.entity = 'conversation'
      AND a.details->>'to' = 'closed'
      AND a.user_id IS NOT NULL
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
    GROUP BY 1
),
sent AS (
    SELECT m.sender_user_id AS user_id,
           count(*) AS messages_sent
    FROM messages m
    WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.delivery_status <> 'failed'
      AND (CAST(:account_id AS uuid) IS NULL OR EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = m.conversation_id AND c.account_id = :account_id))
    GROUP BY 1
)
SELECT u.id AS manager_id, u.full_name, u.is_active,
       COALESCE(t.taken, 0)          AS taken,
       COALESCE(f.answered, 0)       AS answered,
       COALESCE(cl.closed, 0)        AS closed,
       f.frt_avg_sec, f.frt_median_sec, f.frt_median_biz_sec,
       COALESCE(s.messages_sent, 0)  AS messages_sent
FROM users u
LEFT JOIN frt    f  ON f.user_id  = u.id
LEFT JOIN taken  t  ON t.user_id  = u.id
LEFT JOIN closed cl ON cl.user_id = u.id
LEFT JOIN sent   s  ON s.user_id  = u.id
WHERE u.role IN ('admin', 'manager')
  -- Служебная запись (робот seed-smoke, 07 §6) в рейтинге не участвует — но
  -- по ПОСТАВЛЕННОМУ признаку, а не по домену адреса.
  --
  -- ЧТО БЫЛО: `lower(u.email) NOT LIKE '%.local'`. Правило дешёвое и ровно
  -- поэтому неверное — `.local` оказался обычным внутренним доменом
  -- заказчика. Замер 12 августа: `admin@leadpartner.local` и
  -- `dev-admin@leadchat.local` — два ДЕЙСТВУЮЩИХ администратора, которые
  -- ведут диалоги. Отчёт по менеджерам их не показывал вовсе: ни строки, ни
  -- нуля, ни следа. Руководитель видел таблицу без единого пропуска и не имел
  -- повода усомниться — их работа просто не попадала в «ИТОГО», а разбираться
  -- по такому отчёту, кого хвалить и кого подтягивать, нечем.
  --
  -- Миграция 0030 объявила угадывание по строке ошибочным и завела
  -- `users.is_service`: признак ставит тот, кто запись создаёт (seed-smoke), а
  -- отчёт его читает. Ошибка в сторону «не пометили служебное» стоит одной
  -- лишней строки до следующего прогона seed-smoke; ошибка в другую сторону
  -- стирает человека из отчётов молча.
  AND NOT u.is_service
  AND (CAST(:manager_ids AS uuid[]) IS NULL OR u.id = ANY(CAST(:manager_ids AS uuid[])))
  AND (u.is_active
       OR COALESCE(t.taken, f.answered, cl.closed, s.messages_sent) IS NOT NULL)
ORDER BY {sort} {order} NULLS LAST, u.full_name ASC
"""

# «Итого» пересчитывается по всей выборке заново: медиана суммы ≠ сумма медиан
# (06 §4.4).
#
# ⚠ С 15 АВГУСТА ТО ЖЕ ОТНОСИТСЯ К «ПРИНЯТО» И «ЗАКРЫТО»: уникальные диалоги
# суммы ≠ сумма уникальных по людям. Диалог, переданный от Иванова Петрову
# внутри периода, честно даёт по единице каждому (каждый его принимал), но в
# ИТОГО он один — а сумма строк давала двойку. Это ровно механизм жалобы №9,
# только этажом выше: строки починили DISTINCT'ом 12 августа, итог продолжал
# складывать. На нынешних данных не видно (передач мало), поэтому и жило.
#
# Условия CTE обязаны зеркалить одноимённые CTE построчного запроса — вплоть до
# `assignee_id IS NOT NULL`. Разъедутся — и «строка больше итога» вернётся, но
# уже необъяснимой. `answered` и `messages_sent` складываются из строк по
# праву: у ответа один автор первого ответа, у сообщения один отправитель —
# передача диалога их не двоит.
#
# ⚠ «ПЕРЕДАЧ МАЛО» — УСТАРЕЛО, И РОВНО ЭТО ПРИВЕЛО К ЖАЛОБЕ (аудит 08.09,
# H-02). Строка выше говорила «на нынешних данных не видно». Замер по бою
# 08.09: сумма по людям 16 246 против 10 729 уникальных диалогов, а прошли
# через двух и более исполнителей 3 698 диалогов — ТРЕТЬ, при рекорде в десять
# человек на один диалог. Расхождение не редкость, а норма этого потока, и
# читателю отчёта его надо объяснять, а не прятать: оговорка теперь стоит в
# подсказках ОБЕИХ колонок — «Принято» и «Закрыто» (ManagersTable.tsx).
_MANAGERS_TOTALS_TEMPLATE = """
WITH medians AS (
    SELECT round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_operator_sec))::numeric)::int      AS frt_median_sec,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_operator_biz_sec))::numeric)::int  AS frt_median_biz_sec
    FROM (@ИСТОЧНИК@
    ) r
),
taken AS (
    SELECT count(DISTINCT a.entity_id) AS taken
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.assigned'
      AND a.entity = 'conversation'
      AND a.details->>'assignee_id' IS NOT NULL
      AND (a.details->>'assignee_id')::uuid = ANY(CAST(:user_ids AS uuid[]))
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
),
closed AS (
    SELECT count(DISTINCT a.entity_id) AS closed
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.status_changed'
      AND a.entity = 'conversation'
      AND a.details->>'to' = 'closed'
      AND a.user_id = ANY(CAST(:user_ids AS uuid[]))
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
)
SELECT medians.frt_median_sec, medians.frt_median_biz_sec, taken.taken, closed.closed
FROM medians, taken, closed
"""


# Три пути на каждый из двух запросов. Собираются один раз при импорте — текст
# запроса от периода не зависит, зависит только выбор источника строк.
def _по_путям(шаблон: str, *, users: bool) -> dict[str, str]:
    return {
        путь: шаблон.replace("@ИСТОЧНИК@", _frt_rows(live=live, split=split, users=users))
        for путь, (live, split) in (
            ("mv", (False, False)),
            ("live", (True, False)),
            ("split", (True, True)),
        )
    }


_MANAGERS_SQL = _по_путям(_MANAGERS_SQL_TEMPLATE, users=False)
_MANAGERS_TOTALS_SQL = _по_путям(_MANAGERS_TOTALS_TEMPLATE, users=True)


def _путь(live: bool, split_at: datetime | None) -> str:
    if not live:
        return "mv"
    return "split" if split_at is not None else "live"


async def manager_rows(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    sort: str = "messages_sent",
    order: str = "desc",
    live: bool = False,
    split_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Строки таблицы менеджеров (06 §2.8/§4.4).

    ``answered`` и FRT атрибутируются АВТОРУ ПЕРВОГО ОТВЕТА (06 §1.1.7) — при
    передаче диалога метрика не «переезжает» к новому ответственному.
    Отключённые сотрудники остаются в отчёте, если у них была активность в
    периоде: отчёт за прошлый месяц не теряет уволенных.
    """
    if sort not in MANAGER_SORTS:
        raise ApiError(
            "validation_error",
            f"Сортировка по '{sort}' не поддерживается",
            status=400,
            details={"fields": [{"field": "sort", "rule": "enum", "message": ""}]},
        )
    if order.lower() not in ("asc", "desc"):
        raise ApiError(
            "validation_error",
            "order — только asc или desc",
            status=400,
            details={"fields": [{"field": "order", "rule": "enum", "message": ""}]},
        )
    # Обе подстановки — из белого списка выше; в SQL не попадает ничего из query.
    query = _MANAGERS_SQL[_путь(live, split_at)].format(sort=sort, order=order.upper())
    params = _params(period, filters, ts_split=split_at) if split_at else _params(period, filters)
    rows = (await db.execute(_sql(query), params)).mappings().all()
    return [
        {
            "manager_id": str(row["manager_id"]),
            "full_name": row["full_name"],
            "is_active": bool(row["is_active"]),
            "taken": int(row["taken"]),
            "answered": int(row["answered"]),
            "closed": int(row["closed"]),
            "frt_avg_sec": _int(row["frt_avg_sec"]),
            "frt_median_sec": _int(row["frt_median_sec"]),
            "frt_median_biz_sec": _int(row["frt_median_biz_sec"]),
            "messages_sent": int(row["messages_sent"]),
        }
        for row in rows
    ]


async def managers(
    db: AsyncSession,
    period: Period,
    filters: Filters,
    *,
    sort: str = "messages_sent",
    order: str = "desc",
    mv_refreshed_at: str | None = None,
) -> dict[str, Any]:
    """``GET /stats/managers`` — строки + «Итого» (06 §4.4).

    Период, захватывающий сегодня, считается живьём — теми же правилами, что
    карточки и график (``_раскол_живого``, разбор у ``_frt_rows``).
    """
    live = period.date_to >= today_msk()
    раскол = _раскол_живого(period, mv_refreshed_at) if live else None
    rows = await manager_rows(
        db, period, filters, sort=sort, order=order, live=live, split_at=раскол
    )
    user_ids = [uuid.UUID(row["manager_id"]) for row in rows]
    # Имя честное: с 15 августа здесь не только медианы, но и уникальные
    # счётчики «Принято»/«Закрыто» по всей выборке.
    recount: dict[str, Any] = {}
    if user_ids:
        итог_params: dict[str, Any] = {
            "ts_from": period.ts_from,
            "ts_to": period.ts_to,
            "account_id": filters.account_id,
            "user_ids": user_ids,
        }
        if раскол is not None:
            итог_params["ts_split"] = раскол
        recount = await _row(db, _MANAGERS_TOTALS_SQL[_путь(live, раскол)], итог_params)
    totals = {
        # Уникальные диалоги по ВСЕЙ выборке, а не сумма строк: переданный
        # диалог даёт по единице каждому участнику, а в итоге он один
        # (разбор у _MANAGERS_TOTALS_SQL). Пустая таблица — честные нули.
        "taken": _int(recount.get("taken")) or 0,
        "answered": sum(row["answered"] for row in rows),
        "closed": _int(recount.get("closed")) or 0,
        "messages_sent": sum(row["messages_sent"] for row in rows),
        "frt_median_sec": _int(recount.get("frt_median_sec")),
        "frt_median_biz_sec": _int(recount.get("frt_median_biz_sec")),
    }
    # Экран обязан сказать, что он читает: у групп карточек это подписано
    # с 29.08 (STATS-04), у таблицы подписывать было нечего — она приходила
    # с витрины всегда.
    return {"period": period.as_dict(), "period_live": live, "rows": rows, "totals": totals}


# ================================================================ /stats/my/today

_MY_TODAY_SQL = """
WITH sent AS (
    SELECT count(*) AS messages_sent_today
    FROM messages m
    WHERE m.created_at >= :day_start AND m.created_at < :day_end
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.sender_user_id = :user_id
      AND m.delivery_status <> 'failed'
),
taken AS (
    -- Тот же DISTINCT по диалогу, что и в таблице менеджеров: виджет
    -- оператора и отчёт руководителя обязаны сходиться цифра в цифру
    -- (06 §6.2), а два раза переданный диалог — это одно «принято», а не три.
    SELECT count(DISTINCT a.entity_id) AS taken_today
    FROM audit_log a
    WHERE a.action = 'conversation.assigned'
      AND (a.details->>'assignee_id')::uuid = :user_id
      AND a.created_at >= :day_start AND a.created_at < :day_end
),
closed AS (
    SELECT count(DISTINCT a.entity_id) AS closed_today
    FROM audit_log a
    WHERE a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
      AND a.user_id = :user_id
      AND a.created_at >= :day_start AND a.created_at < :day_end
),
active AS (
    -- «Ждут моего ответа» — по той же отметке `awaiting_since`, что и карточка
    -- «Ждут ответа» на /stats (см. `snapshot_now`): виджет оператора и отчёт
    -- руководителя обязаны сходиться цифра в цифру (06 §6.2).
    --
    -- Здесь стоял LATERAL «последнее видимое сообщение — от клиента». Он врал
    -- ровно там, где ошибка дороже всего: `direction='out'` без проверки
    -- доставки, поэтому НЕОТПРАВЛЕННЫЙ ответ оператора гасил ожидание. У
    -- оператора в виджете стоял ноль, сторож в это же время слал ему
    -- напоминание «клиент ждёт 15 минут», и правы были оба — просто считали
    -- разное.
    --
    -- «Активные» — это `in_progress` И `waiting_client` (docs/38 §6.1).
    -- Диалог, в котором мы ждём клиента, из работы оператора не выпал: он его
    -- ведёт, он за него отвечает, и клиент может ответить в любую секунду.
    -- Третьим числом здесь считались отложенные («что вернётся завтра») —
    -- снято 12 августа вместе со статусом.
    SELECT count(*) FILTER (WHERE c.status IN ('in_progress', 'waiting_client'))
                                                                AS active_now,
           count(*) FILTER (WHERE c.awaiting_since IS NOT NULL) AS waiting_reply_now
    FROM conversations c
    WHERE c.assignee_id = :user_id
      AND c.status IN ('in_progress', 'waiting_client')
),
frt AS (
    -- ⚠ ГРАНИЦА ПО ДАТЕ ЗДЕСЬ БЫЛА С 05.08 (`c.last_message_at >= :day_start`),
    -- И ДОРОГО СТОИЛА НЕ ОНА. Она оставляла все диалоги, в которых сегодня
    -- хоть что-то было, — у всей компании, а не у этого человека: 677 штук
    -- (замер боя 06.09). И для КАЖДОГО из них LATERAL `f` искал первое слово
    -- клиента по всем 28 партициям `messages`, потому что первое слово могло
    -- быть когда угодно. 48 671 буфер по замеру 06.09, 81 881 — 07.09; чей это
    -- диалог, выяснялось только в самом конце, по `op.sender_user_id`.
    --
    -- ПОЭТОМУ СНАЧАЛА — ЧЬИ ДИАЛОГИ, И ТОЛЬКО ПОТОМ — ЛАТЕРАЛЫ. Условие
    -- «этот человек сегодня отправил сюда хоть один доставленный ответ»
    -- ищется в одной сегодняшней партиции и оставляет от 677 диалогов
    -- несколько десятков. Смысла оно не меняет: у любого диалога, который
    -- прошёл бы прежний фильтр, `op` — как раз такое сообщение (сегодняшнее,
    -- этого человека, не failed), то есть EXISTS для него истинен; а диалог, у
    -- которого такого сообщения нет, прежний фильтр отбрасывал по
    -- `op.sender_user_id`. Замер боя 07.09 на самом занятом операторе дня:
    -- 81 881 → 12 451 буфер, 56 → 9,2 мс, оба числа виджета совпали (116 / 529).
    --
    -- `m.created_at >= :day_start` внутри `op` — из того же ряда: раз
    -- `f.first_client_at >= :day_start` обязателен, граница ничего не отсекает,
    -- зато позволяет планировщику не заглядывать в прошлые партиции.
    --
    -- ⚠ ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ: границы по дате ВНУТРИ `f`. Первое слово
    -- клиента обязано искаться по всей истории, иначе диалог, начатый вчера и
    -- отвеченный сегодня, стал бы «начатым сегодня» с ложным FRT. Именно это и
    -- стережёт `tests/integration/test_perf_0609_pg.py`. `c.created_at` тоже
    -- не годится: диалог заводится и по звонку без единого слова
    -- (`inbound.py`), и клиент может написать в него назавтра.
    SELECT count(*)                                          AS answered_today,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_sec))::numeric)::int          AS frt_median_sec_today
    FROM (
        SELECT EXTRACT(epoch FROM op.first_at - f.first_client_at) AS frt_sec
        FROM conversations c
        CROSS JOIN LATERAL (
            SELECT min(m.created_at) AS first_client_at
            FROM messages m
            WHERE m.conversation_id = c.id
              AND m.direction = 'in' AND m.sender_type = 'client'
        ) f
        CROSS JOIN LATERAL (
            SELECT m.created_at AS first_at, m.sender_user_id
            FROM messages m
            WHERE m.conversation_id = c.id
              AND m.created_at >= :day_start
              AND m.created_at >= f.first_client_at
              AND m.direction = 'out' AND m.sender_type = 'operator'
              AND m.delivery_status <> 'failed'
            ORDER BY m.created_at
            LIMIT 1
        ) op
        WHERE c.last_message_at >= :day_start
          AND EXISTS (
              SELECT 1 FROM messages m
              WHERE m.conversation_id = c.id
                AND m.created_at >= :day_start
                AND m.direction = 'out' AND m.sender_type = 'operator'
                AND m.sender_user_id = :user_id
                AND m.delivery_status <> 'failed'
          )
          AND f.first_client_at >= :day_start AND f.first_client_at < :day_end
          AND op.sender_user_id = :user_id
    ) r
)
SELECT * FROM sent, taken, closed, active, frt
"""


async def my_today(db: AsyncSession, redis: Redis, user_id: uuid.UUID) -> dict[str, Any]:
    """``GET /stats/my/today`` — виджет «моя статистика за сегодня» (06 §6).

    Без MV (нужна live-свежесть), узкий скоуп (один пользователь, одна
    партиция текущего месяца), кэш Redis на ``MY_TODAY_TTL_SECONDS`` — дольше
    интервала опроса виджета, иначе кэш не попадает никогда (см. константу).
    Определения — те же, что в словаре §1: виджет менеджера и ``/stats``
    руководителя обязаны сходиться цифра в цифру при одинаковых фильтрах.
    """
    day = today_msk()
    key = f"stats:my:{user_id}:{day.isoformat()}"
    cached = await _cache_get(redis, key)
    if cached is not None:
        return cached

    day_start, day_end = msk_day_bounds(day)
    row = await _row(
        db,
        _MY_TODAY_SQL,
        {"day_start": day_start, "day_end": day_end, "user_id": user_id},
    )
    payload = {
        "date": day.isoformat(),
        "active_now": int(row.get("active_now") or 0),
        # «мои активные, где клиент написал и ответа ещё не получил»
        "waiting_reply_now": int(row.get("waiting_reply_now") or 0),
        "taken_today": int(row.get("taken_today") or 0),
        "closed_today": int(row.get("closed_today") or 0),
        "messages_sent_today": int(row.get("messages_sent_today") or 0),
        "frt_median_sec_today": _int(row.get("frt_median_sec_today")),
        "answered_today": int(row.get("answered_today") or 0),
    }
    await _cache_set(redis, key, payload, MY_TODAY_TTL_SECONDS)
    return payload


# ========================================================================= экспорт

EXPORT_FORMATS: tuple[str, ...] = ("csv", "xlsx")
EXPORT_SHEETS: tuple[str, ...] = ("summary", "managers", "conversations")
EXPORT_DIRNAME = "exports"
EXPORT_JOB = "export_stats"
MAX_CONV_ROWS = 100_000  # 06 §5.4
EXPORT_DAILY_LIMIT = 20  # 06 §5.4
EXPORT_URL_TTL_SECONDS = 86_400  # подписанная ссылка живёт сутки
EXPORT_RETENTION_DAYS = 7  # хранение файлов (06 §5.4)
EXPORT_ACTIVE_TTL_SECONDS = 3_600  # предохранитель: залипший лок снимется сам

CONV_HEADERS: tuple[str, ...] = (
    "Первое сообщение (МСК)",
    "Аккаунт",
    "Клиент",
    "Телефон",
    "Объявление",
    "Статус",
    "Ответственный",
    "Автор первого ответа",
    "FRT оператора, с",
    "FRT оператора (раб.), с",
    "FRT бота, с",
    "Сообщений вх.",
    "Сообщений исх. (оператор)",
    "Сообщений исх. (бот)",
    "Закрыт кем",
    "Закрыт когда (МСК)",
    # ЗДЕСЬ СТОЯЛИ КОЛОНКИ «Результат» И «Сумма, ₽» (#37). Убраны 12 августа
    # решением владельца вместе с окном «Чем закончилось обращение?».
    #
    # ЧТО ЭТО ЗНАЧИТ ДЛЯ ОТЧЁТА, ЧЕСТНО. «Сумма, ₽» была единственным столбцом
    # во всей выгрузке, где стояли деньги. Без неё диалог, дошедший до выезда,
    # и диалог, где человек передумал, снова выглядят одинаково: закрыт,
    # столько-то сообщений, столько-то секунд. На вопрос «какой канал
    # окупается» — первый, который задаёт владелец, глядя на девять
    # аккаунтов, — отчёт больше не отвечает.
    "conversation_id",
)

#: «Закрыт кем» — расшифровка `audit_log.details->>'by'` события закрытия.
#:
#: В столбце стояло машинное `operator` / `bot`: SQL брал `s.closed_by` из
#: представления и клал в ячейку как есть. Ровно та же болезнь, что вылечили у
#: столбца «Статус» по соседству (STATS-09), только на другой колонке — правку
#: тогда сделали по букве дефекта, а не по всей строке выгрузки.
#:
#: Закрывают оператор (`"by": "operator"` в `conversations.py`, `inbox.py`,
#: `messages.py`), бот (`bots/engine.py`) и сама система — массовый разбор
#: очереди и сторож освобождения (`"by": "system"`). До 24.09 сторож закрывал
#: без ключа `by` вовсе: у таких строк «Закрыт когда» заполнено, а «Закрыт кем»
#: пусто, и это читалось как «никто» — см. `closed_by_label`. Незнакомое
#: значение отдаётся как есть: превратить его в пустоту значит спрятать
#: строку, которую как раз и надо заметить.
CLOSED_BY_RU: dict[str, str] = {"operator": "Оператор", "bot": "Бот", "system": "Автоматически"}


def closed_by_label(closed_by: str | None, closed_at: Any) -> str | None:
    """«Закрыт кем» по-русски; закрытие без автора — автоматическое."""
    if closed_by is None:
        return CLOSED_BY_RU["system"] if closed_at is not None else None
    return CLOSED_BY_RU.get(closed_by, closed_by)


# ⚠ ЗАГОЛОВКИ ЛИСТА ОБЯЗАНЫ СОВПАДАТЬ С ПОДПИСЯМИ НА ЭКРАНЕ (правка 08.09).
# Колонку «Отвечено» на экране переименовали в «Ответил первым», потому что тем
# же словом называется карточка сводки, а числа за ними разные: карточка
# считает любой ответ оператора (замер по бою — 29 757), колонка — только те,
# у кого известен автор (10 006). В выгрузке оба листа лежат в ОДНОМ файле:
# «Сводка» со строкой «Отвечено оператором» и «Менеджеры» со столбцом
# «Отвечено». Оставить их разными значило бы перенести исправленное
# противоречие с экрана в файл, который уходит руководителю.
MANAGER_HEADERS: tuple[str, ...] = (
    "Менеджер",
    "Принято",
    "Ответил первым",
    "Закрыто",
    "FRT ср., с",
    "FRT мед., с",
    "FRT мед. (раб.), с",
    "Отправлено",
)


#: Что видит человек, когда файл не собрался. В статусе лежала строка
#: `internal`, и фронт показывал её как есть — тост «Выгрузка не удалась:
#: internal» (проверка 24.09). Подробности — в журнале воркера.
EXPORT_FAILED_TEXT = "Не получилось собрать файл — повторите выгрузку позже"
EXPORT_QUEUE_DOWN_TEXT = "Очередь задач недоступна — повторите выгрузку позже"


class ExportTooLarge(Exception):
    """Строк «Диалогов» больше лимита — job завершается ``failed`` с подсказкой."""


def export_dir() -> Path:
    """``MEDIA_ROOT/exports`` — под тем же alias, что и вложения (06 §4.5)."""
    return Path(settings.media_root) / EXPORT_DIRNAME


def export_filename(period: Period, fmt: str, job_id: str) -> str:
    return f"leadchat-stats_{period.date_from}_{period.date_to}_{job_id[:8]}.{fmt}"


def export_status_key(job_id: str) -> str:
    return f"stats:export:{job_id}"


def export_active_key(user_id: uuid.UUID | str) -> str:
    return f"stats:export:active:{user_id}"


def export_quota_key(user_id: uuid.UUID | str, day: date | None = None) -> str:
    return f"stats:export:quota:{user_id}:{(day or today_msk()).isoformat()}"


def seconds_to_msk_midnight(now: datetime | None = None) -> int:
    """TTL счётчика экспортов — до полуночи МСК (06 §5.4)."""
    moment = (now or datetime.now(UTC)).astimezone(MSK)
    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - moment).total_seconds()))


async def reserve_export_slot(redis: Redis, user_id: uuid.UUID, job_id: str) -> None:
    """Лимиты 06 §5.4: один активный экспорт на пользователя (409) и 20 в
    сутки (429). Счётчик откатывается, если слот занять не удалось."""
    acquired = await redis.set(
        export_active_key(user_id), job_id, nx=True, ex=EXPORT_ACTIVE_TTL_SECONDS
    )
    if not acquired:
        raise ApiError(
            "export_already_running",
            "У вас уже выполняется экспорт — дождитесь его завершения",
            status=409,
        )
    used = int(await redis.incr(export_quota_key(user_id)))
    if used == 1:
        await redis.expire(export_quota_key(user_id), seconds_to_msk_midnight())
    if used > EXPORT_DAILY_LIMIT:
        await redis.decr(export_quota_key(user_id))
        await redis.delete(export_active_key(user_id))
        raise ApiError(
            "rate_limited",
            f"Исчерпан дневной лимит выгрузок ({EXPORT_DAILY_LIMIT})",
            status=429,
            details={"limit": EXPORT_DAILY_LIMIT},
        )


async def release_export_slot(redis: Redis, user_id: uuid.UUID | str) -> None:
    await redis.delete(export_active_key(user_id))


async def create_export_job(
    redis: Redis,
    *,
    user_id: uuid.UUID,
    period: Period,
    filters: Filters,
    fmt: str,
    sheets: list[str],
) -> str:
    """Резервирует слот, кладёт статус в Redis и ставит ARQ-задачу (06 §5.1)."""
    from app.services.messages import as_arq  # локально: избегаем цикла импорта

    job_id = uuid.uuid4().hex
    await reserve_export_slot(redis, user_id, job_id)
    params: dict[str, Any] = {
        "format": fmt,
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
        "sheets": sheets,
        "user_id": str(user_id),
        **filters.as_dict(),
    }
    await redis.hset(  # type: ignore[misc]
        export_status_key(job_id),
        mapping={
            "status": "pending",
            "format": fmt,
            "user_id": str(user_id),
            "date_from": params["date_from"],
            "date_to": params["date_to"],
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    await redis.expire(export_status_key(job_id), EXPORT_URL_TTL_SECONDS)
    try:
        from arq.connections import ArqRedis

        await ArqRedis.enqueue_job(as_arq(redis), EXPORT_JOB, job_id, params, _job_id=job_id)
    except Exception:
        # Очередь недоступна — слот держать нельзя, иначе пользователь
        # получит 409 на следующую попытку до истечения TTL. Дневную квоту
        # тоже возвращаем: несостоявшаяся выгрузка не должна съедать одну из
        # 20 попыток (06 §5.4 ограничивает выгрузки, а не отказы очереди).
        await release_export_slot(redis, user_id)
        with contextlib.suppress(Exception):
            await redis.decr(export_quota_key(user_id))
        await redis.hset(  # type: ignore[misc]
            export_status_key(job_id), mapping={"status": "failed", "error": EXPORT_QUEUE_DOWN_TEXT}
        )
        log.exception("stats.export_enqueue_failed", job_id=job_id)
        raise ApiError(
            "upstream_unavailable",
            "Очередь задач недоступна — повторите позже",
            status=503,
        ) from None
    log.info("stats.export_enqueued", job_id=job_id, user_id=str(user_id), format=fmt)
    return job_id


async def export_status(redis: Redis, job_id: str, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Статус job'а — только автору (06 §4.5). Чужой/несуществующий → 404."""
    raw = await redis.hgetall(export_status_key(job_id))  # type: ignore[misc]
    data = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in (raw or {}).items()
    }
    if not data or data.get("user_id") != str(user_id):
        raise ApiError("not_found", "Задача выгрузки не найдена", status=404)
    relpath = data.get("relpath")
    url = signed_media_url(relpath, ttl=EXPORT_URL_TTL_SECONDS) if relpath else None
    expires = (
        (datetime.now(UTC) + timedelta(seconds=EXPORT_URL_TTL_SECONDS)).isoformat() if url else None
    )
    return {
        "job_id": job_id,
        "status": data.get("status", "pending"),
        "format": data.get("format"),
        "rows": _int(data.get("rows")) if data.get("rows") is not None else None,
        "url": url,
        "expires_at": expires,
        "error": data.get("error"),
    }


# ----------------------------------------------------------------- строки экспорта


def humanize_seconds(value: int | None) -> str:
    """«1 мин 35 с» — только для листа «Сводка» (в API длительности целые секунды)."""
    if value is None:
        return "—"
    seconds = int(value)
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def mv_freshness_label(iso: str | None) -> str:
    """Метка свежести витрины для листа «Сводка» — «2026-08-11 20:05 (МСК)»."""
    if not iso:
        # Метку пишет ежечасный job в Redis. Нет метки — значит Redis не ответил
        # или витрину ещё ни разу не пересчитывали; и то и другое означает
        # «возраст этих чисел неизвестен», а не «данные свежие».
        return "неизвестно — метка свежести недоступна"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return "неизвестно — метка свежести недоступна"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{moment.astimezone(MSK).strftime('%Y-%m-%d %H:%M')} (МСК)"


@dataclass(frozen=True)
class FilterNames:
    """Подписи фильтров для листа «Сводка»: название канала и имена сотрудников."""

    account: str | None = None
    managers: tuple[str, ...] = ()


async def filter_names(db: AsyncSession, filters: Filters) -> FilterNames:
    """Названия вместо идентификаторов (проверка 24.09).

    Лист «Сводка» печатал «Аккаунт: 3f2a…», «Менеджеры: 9c1e…, 51b0…» — по
    файлу, который уходит владельцу, нельзя было понять, чей это отчёт.
    Незнакомый идентификатор печатается с пометкой, а не пропадает: фильтр
    действовал, и промолчать о нём значило бы выдать срез за полный отчёт.
    """
    account = None
    if filters.account_id is not None:
        title = await db.scalar(
            sa.select(AvitoAccount.title).where(AvitoAccount.id == filters.account_id)
        )
        account = title or f"неизвестный канал ({filters.account_id})"
    managers: tuple[str, ...] = ()
    if filters.manager_ids:
        rows = await db.execute(
            sa.select(User.id, User.full_name).where(User.id.in_(filters.manager_ids))
        )
        known: dict[uuid.UUID, str] = {row.id: row.full_name for row in rows}
        managers = tuple(
            known.get(m) or f"неизвестный сотрудник ({m})" for m in filters.manager_ids
        )
    return FilterNames(account=account, managers=managers)


def summary_pairs(
    payload: dict[str, Any],
    period: Period,
    filters: Filters,
    *,
    refreshed_at: str | None = None,
    names: FilterNames | None = None,
) -> list[list[Any]]:
    """Лист «Сводка»: пары «показатель / значение» + период и фильтры (06 §5.2)."""
    names = names or FilterNames()
    # Период, захватывающий сегодня, сводка считает живьём (STATS-04) — значит
    # и подписать её надо иначе, чем витринный лист «Диалоги».
    живая_сводка = period.date_to >= today_msk()
    cards = payload["cards"]
    frt = cards["frt_operator"]
    bot = cards["bot_closed"]
    phones = cards["phones_collected"]
    repeat = cards["repeat_contacts"]
    return [
        ["Период", f"{period.date_from} — {period.date_to} (Europe/Moscow)"],
        # «Аккаунт» — тем же словом, что колонка листа «Диалоги» в этом же файле.
        ["Аккаунт", (names.account or str(filters.account_id)) if filters.account_id else "все"],
        [
            "Менеджеры",
            ", ".join(names.managers or tuple(str(m) for m in filters.manager_ids))
            if filters.manager_ids
            else "все",
        ],
        ["Выгружено", datetime.now(UTC).astimezone(MSK).strftime("%Y-%m-%d %H:%M")],
        # ДВЕ РАЗНЫЕ ДАТЫ, И ЭТО НЕ ИЗБЫТОЧНОСТЬ.
        #
        # «Выгружено» — когда собран файл. Но собрано под ним не всё сразу:
        # лист «Диалоги» читается из витрины `mv_conversation_stats`, а её
        # пересчитывает job раз в час в HH:05 (06 §3.2/§3.3). Выгрузка в 20:55
        # печатает по нему состояние на 20:05 — до пятидесяти минут разницы.
        #
        # Пока даты не было, отличить одно от другого в файле было НЕЧЕМ:
        # «Выгружено 20:55» читалось как «всё здесь на 20:55». Владелец сверял
        # отчёт с экраном чатов, не сходилось, и виноватой оказывалась система,
        # а не сорок минут возраста.
        #
        # ⚠ ПОДПИСЬ ИСПРАВЛЕНА 30.08: ОНА ВРАЛА В ОБРАТНУЮ СТОРОНУ. Стояло
        # «Диалоги и FRT — по данным на HH:05», но после STATS-04 (29.08)
        # сводка периода, захватывающего сегодня, считается ЖИВЬЁМ: и
        # «Диалогов новых», и FRT здесь свежие, а старым остаётся только лист
        # «Диалоги». Подпись обещала витрину половине чисел, которые уже не с
        # витрины, — и владелец, сверяя лист «Диалоги» со «Сводкой» ТОГО ЖЕ
        # файла, получал 36 строк против 41 и не имел ничего, чем это
        # объяснить. Разница настоящая (сегодняшний хвост в витрину ещё не
        # попал), и убрать её можно только живым близнецом листа — а это
        # пересчёт всех предрассчитанных колонок на каждую выгрузку. Пока
        # честнее назвать вещи своими именами: числа тогда сходятся не по
        # величине, а по объяснению.
        [
            "Сводка посчитана",
            "на момент выгрузки" if живая_сводка else "по витрине",
        ],
        ["Лист «Диалоги» — по данным на", mv_freshness_label(refreshed_at)],
        ["Диалогов новых", cards["conversations_new"]["value"]],
        ["Диалогов закрыто", cards["conversations_closed"]["value"]],
        ["В работе сейчас", cards["in_progress_now"]["value"]],
        ["из них без ответственного", cards["in_progress_now"].get("unassigned", 0)],
        # Обе строки — снимки на момент выгрузки, а не «за период»; подписи
        # называют разное и считают разное (см. `snapshot_now`). До правки
        # «Ждут ответа сейчас» печатало длину очереди — в отчёте владельца
        # стояла неразобранная очередь под видом невыполненной работы.
        ["Ждут ответа сейчас", cards["waiting_now"]["value"]],
        ["В очереди сейчас (никем не взяты)", cards["queue_now"]["value"]],
        # «Ждут клиента» — в отчёте владельца, а не только на экране. Четыре
        # числа «сейчас» в сумме дают все открытые диалоги; до docs/38 два
        # числа не давали в сумме ничего, и сверить отчёт было не с чем.
        # Пятой строкой стояло «Отложено сейчас» — снято 12 августа вместе со
        # статусом; сумма от этого сходиться не перестала.
        ["Ждут клиента сейчас", cards["waiting_client_now"]["value"]],
        ["FRT оператора, медиана, с", frt["median_sec"]],
        ["FRT оператора, медиана", humanize_seconds(frt["median_sec"])],
        ["FRT оператора, среднее, с", frt["avg_sec"]],
        ["FRT оператора (раб. часы), медиана, с", frt["median_biz_sec"]],
        ["FRT оператора (раб. часы), медиана", humanize_seconds(frt["median_biz_sec"])],
        ["Отвечено оператором", frt["answered"]],
        ["Без ответа", frt["unanswered"]],
        ["FRT бота, медиана, с", cards["frt_bot"]["median_sec"]],
        ["Закрыто ботом без оператора, %", bot["pct"]],
        ["Закрыто ботом без оператора", bot["closed_by_bot"]],
        ["Собрано телефонов", phones["value"]],
        ["  из них ботом", phones["by_source"]["bot"]],
        ["  из них автоизвлечением", phones["by_source"]["regex"]],
        ["  из них вручную", phones["by_source"]["manual"]],
        ["Переоткрытий", repeat["reopened"]],
        ["Повторных клиентов", repeat["repeat_clients"]],
    ]


_CONV_ROWS_SQL = f"""
SELECT to_char(s.first_client_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI')
                                              AS first_client_at_msk,
       acc.title                              AS account_title,
       cl.name                                AS client_name,
       cl.phone                               AS client_phone,
       c.item_title                           AS item_title,
       s.status                               AS status,
       assignee.full_name                     AS assignee_name,
       author.full_name                       AS first_operator_name,
       s.frt_operator_sec, s.frt_operator_biz_sec, s.frt_bot_sec,
       s.msgs_in, s.msgs_out_operator, s.msgs_out_bot,
       s.closed_by,
       to_char(s.closed_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI')
                                              AS closed_at_msk,
       -- Отсюда брались `c.outcome` и `c.outcome_amount` — результат обращения
       -- и сумма. Сняты 12 августа; колонки в базе остались, читать их больше
       -- некому. JOIN на `conversations` НЕ убран: он нужен и без них, ради
       -- названия объявления.
       s.conversation_id::text                AS conversation_id
FROM {MV} s
JOIN conversations c   ON c.id = s.conversation_id
JOIN avito_accounts acc ON acc.id = s.account_id
JOIN clients cl        ON cl.id = s.client_id
LEFT JOIN users assignee ON assignee.id = s.assignee_id
LEFT JOIN users author   ON author.id = s.first_operator_user_id
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
{_F_MV}
ORDER BY s.first_client_at
"""


def conv_row(row: Any) -> list[Any]:
    """Одна строка листа «Диалоги» из строки выборки.

    ОТДЕЛЬНОЙ ФУНКЦИЕЙ, А НЕ ТЕЛОМ ГЕНЕРАТОРА, — чтобы её можно было проверить
    без базы. Пока разбор жил внутри `conversation_rows`, согласие этого списка
    с `CONV_HEADERS` не проверялось НИЧЕМ: заголовков могло стать на два больше,
    чем значений, и файл уехал бы владельцу со сдвигом — «Закрыт когда» под
    подписью «Результат». Никакой ошибки при этом не возникает, а прочитать
    такой отчёт нельзя вовсе. Ровно так и вышло при снятии колонок исхода
    12 августа: заголовки убрали, значения убрали, и убедиться, что тронуты
    оба места, было нечем. Теперь сверку делает
    `tests/unit/test_stats_export.py`.
    """
    return [
        row["first_client_at_msk"],
        row["account_title"],
        row["client_name"],
        row["client_phone"],
        row["item_title"],
        # Статус — по-русски, тем же словарём, что и выгрузка с /dialogs.
        # Здесь печаталось машинное значение, и в отчёте, который читает
        # владелец, стояло `in_progress`; тот же столбец того же листа,
        # выгруженный с соседнего экрана, показывал «В работе». Словарь
        # берётся из conversation_table, а не копируется сюда: две копии
        # разъедутся на первом же новом статусе — из-за этого расхождения
        # дефект и появился.
        STATUS_RU.get(row["status"], row["status"]),
        row["assignee_name"],
        row["first_operator_name"],
        row["frt_operator_sec"],
        row["frt_operator_biz_sec"],
        row["frt_bot_sec"],
        row["msgs_in"],
        row["msgs_out_operator"],
        row["msgs_out_bot"],
        # «operator» / «bot» — машинные значения; в отчёте владельца стоят
        # по-русски, как и соседний столбец «Статус» (см. CLOSED_BY_RU).
        closed_by_label(row["closed_by"], row["closed_at_msk"]),
        row["closed_at_msk"],
        row["conversation_id"],
    ]


async def conversation_rows(
    db: AsyncSession, period: Period, filters: Filters
) -> AsyncIterator[list[Any]]:
    """Строки листа «Диалоги» из MV серверным курсором (``yield_per=1000``)."""
    stream = await db.stream(
        _sql(_CONV_ROWS_SQL).execution_options(yield_per=1000), _params(period, filters)
    )
    async for row in stream.mappings():
        yield conv_row(row)


# ------------------------------------------------------------------- запись файлов


@dataclass(slots=True)
class Sheet:
    """Лист выгрузки. ``limited`` — считается против ``MAX_CONV_ROWS``."""

    title: str
    header: Sequence[Any] | None
    rows: AsyncIterator[Sequence[Any]]
    limited: bool = False


def _row_limit(limit: int | None) -> int:
    """Лимит строк читается на каждый вызов, а не защёлкивается в дефолте
    сигнатуры: иначе его нельзя ни переопределить, ни подменить в тесте."""
    return MAX_CONV_ROWS if limit is None else limit


async def write_csv(
    path: Path,
    header: Sequence[Any],
    rows: AsyncIterator[Sequence[Any]],
    *,
    limit: int | None = None,
) -> int:
    """CSV для русского Excel (06 §5.2): ``utf-8-sig`` (BOM), ``;``, CRLF."""
    limit = _row_limit(limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";", lineterminator="\r\n")
            writer.writerow(list(header))
            async for row in rows:
                written += 1
                if written > limit:
                    raise ExportTooLarge(f"Больше {limit} диалогов — сузьте период или фильтры")
                writer.writerow(["" if cell is None else csv_cell(cell) for cell in row])
    except BaseException:
        # Обрезанный файл на диске опаснее отсутствующего: по подписанной
        # ссылке его не отличить от полной выгрузки.
        path.unlink(missing_ok=True)
        raise
    return written


def _text_stays_text(worksheet: Any, value: Any) -> Any:
    """Строку — строкой, даже если она начинается с «=».

    ПОЧЕМУ. ``openpyxl`` разбирает значение при записи и текст, начинающийся с
    «=», кладёт в файл ФОРМУЛОЙ (проверено на живой библиотеке: у такой ячейки
    ``data_type == 'f'``, у обычной — ``'s'``). В выгрузке статистики текст
    берётся из полей, которые пишет не наш продукт: имя клиента и название
    объявления приходят из Авито, а туда их набирает посторонний человек.

    Чем это кончается в отчёте владельца: клиент по имени «=1+1» превращает
    ячейку в вычисление, и в столбце «Клиент» владелец видит «2» — строка
    отчёта молча теряет того, о ком она. Excel показывает `#ИМЯ?` на любом
    тексте, похожем на функцию, а `=HYPERLINK(...)` и вовсе делает из имени
    клиента ссылку. Отчёт, по которому считают деньги, обязан показывать то,
    что лежит в базе, буква в букву.

    Тип проставляется ЯВНО, а не подбором «опасных» первых символов: список
    таких символов у Excel свой, у openpyxl свой, и держать здесь третью копию
    значит однажды разойтись со всеми. Правило простое и без исключений —
    текстовая ячейка выгрузки всегда текст.
    """
    if not isinstance(value, str):
        return value
    from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]

    cell = WriteOnlyCell(worksheet, value=value)
    cell.data_type = "s"
    return cell


async def write_xlsx(path: Path, sheets: Iterable[Sheet], *, limit: int | None = None) -> int:
    """XLSX через ``openpyxl`` в режиме ``write_only`` — память O(1) от строк."""
    limit = _row_limit(limit)
    try:
        # У openpyxl нет типов в дереве проекта — стабы не входят в dev-группу.
        from openpyxl import Workbook  # type: ignore[import-untyped]
    except ImportError as exc:  # зависимость ставится отдельно (см. cross-boundary)
        log.error("stats.export_xlsx_unavailable", reason="openpyxl_not_installed")
        raise ApiError(
            "internal_error",
            # Имя библиотеки — для журнала, а не для человека (TEXT-30): по
            # слову «openpyxl» руководитель не сделает ничего, а «выгрузите в
            # CSV» — сделает прямо сейчас. Диагностика уходит в лог.
            "Выгрузка в XLSX сейчас недоступна — выгрузите в CSV или напишите администратору",
            status=500,
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook(write_only=True)
    worksheet: Any = None
    counted = 0
    try:
        for sheet in sheets:
            worksheet = workbook.create_sheet(sheet.title)
            if sheet.header is not None:
                worksheet.append([_text_stays_text(worksheet, v) for v in sheet.header])
            async for row in sheet.rows:
                if sheet.limited:
                    counted += 1
                    if counted > limit:
                        raise ExportTooLarge(f"Больше {limit} диалогов — сузьте период или фильтры")
                worksheet.append([_text_stays_text(worksheet, value) for value in row])
        workbook.save(str(path))
    except BaseException:
        # Порядок важен: сначала доигрываем генератор текущего листа, потом
        # закрываем архив — иначе openpyxl пишет в уже закрытый файл при GC.
        with contextlib.suppress(Exception):
            if worksheet is not None:
                worksheet.close()
        with contextlib.suppress(Exception):
            workbook.close()
        path.unlink(missing_ok=True)
        raise
    return counted


async def _iter_rows(rows: Iterable[Sequence[Any]]) -> AsyncIterator[Sequence[Any]]:
    for row in rows:
        yield row


# ------------------------------------------------------------------- ARQ-задача


def _params_to_period(params: dict[str, Any]) -> Period:
    return Period(date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"]))


def _params_to_filters(params: dict[str, Any]) -> Filters:
    account_id = params.get("account_id")
    manager_ids = params.get("manager_ids") or []
    return Filters(
        account_id=uuid.UUID(account_id) if account_id else None,
        manager_ids=tuple(uuid.UUID(m) for m in manager_ids),
    )


@with_job_scope
async def export_stats(ctx: dict[str, Any], job_id: str, params: dict[str, Any]) -> str:
    """ARQ-задача выгрузки (06 §5.1/§5.3).

    Живёт в сервисном слое, а не в ``app/workers/exports.py``: тело задачи —
    те же метрики, что и у endpoint'ов, и дублировать их запросы в воркере
    нельзя. Регистрация в ``WorkerSettings.functions`` — см. cross-boundary.
    """
    redis: Redis = ctx["redis"]
    key = export_status_key(job_id)
    user_id = params["user_id"]
    period = _params_to_period(params)
    filters = _params_to_filters(params)
    fmt = params.get("format", "csv")
    sheets = params.get("sheets") or list(EXPORT_SHEETS)
    path = export_dir() / export_filename(period, fmt, job_id)

    await redis.hset(key, mapping={"status": "running"})  # type: ignore[misc]
    try:
        factory = ctx["db_session_factory"]
        async with factory() as db:
            rows = await _write_export(
                db,
                path,
                fmt=fmt,
                sheets=sheets,
                period=period,
                filters=filters,
                # Возраст витрины читается ЗДЕСЬ, до сборки файла: лист «Сводка»
                # обязан назвать его сам (см. `summary_pairs`).
                refreshed_at=await refreshed_at(redis),
            )
            await write_audit(
                db,
                user_id=uuid.UUID(user_id),
                action="stats.exported",
                entity="stats",
                entity_id=job_id,
                details={
                    "format": fmt,
                    "date_from": params["date_from"],
                    "date_to": params["date_to"],
                    "rows": rows,
                },
            )
            await db.commit()
        await redis.hset(  # type: ignore[misc]
            key,
            mapping={
                "status": "done",
                "rows": rows,
                "relpath": f"{EXPORT_DIRNAME}/{path.name}",
            },
        )
        log.info("stats.export_done", job_id=job_id, rows=rows, format=fmt)
        return "done"
    except ExportTooLarge as exc:
        await redis.hset(key, mapping={"status": "failed", "error": str(exc)})  # type: ignore[misc]
        log.warning("stats.export_too_large", job_id=job_id)
        return "failed"
    except Exception:
        await redis.hset(key, mapping={"status": "failed", "error": EXPORT_FAILED_TEXT})  # type: ignore[misc]
        log.exception("stats.export_failed", job_id=job_id)
        raise
    finally:
        await redis.expire(key, EXPORT_URL_TTL_SECONDS)
        await release_export_slot(redis, user_id)


async def _write_export(
    db: AsyncSession,
    path: Path,
    *,
    fmt: str,
    sheets: list[str],
    period: Period,
    filters: Filters,
    refreshed_at: str | None = None,
) -> int:
    if fmt == "csv":
        # CSV — плоский формат: те же данные, что на листе «Диалоги» (06 §5.2).
        return await write_csv(path, CONV_HEADERS, conversation_rows(db, period, filters))

    prepared: list[Sheet] = []
    if "summary" in sheets:
        pairs = summary_pairs(
            await summary(db, period, filters, mv_refreshed_at=refreshed_at),
            period,
            filters,
            refreshed_at=refreshed_at,
            names=await filter_names(db, filters),
        )
        prepared.append(Sheet("Сводка", ["Показатель", "Значение"], _iter_rows(pairs)))
    if "managers" in sheets:
        # Через `managers()`, а не через `manager_rows()`: строку «Итого» надо
        # взять ту же, что видна на экране.
        #
        # Здесь она собиралась заново, и медианы в ней стояли пустыми — сложить
        # медианы нельзя, а посчитать их по всей выборке эта ветка не умела.
        # Владелец видел на /stats «Итого · FRT мед. 4 м 12 с», открывал тот же
        # отчёт в Excel и находил на том же месте пустоту; догадаться, что это
        # не «данных нет», а «не посчитали», по файлу невозможно. `managers()`
        # считает медиану «Итого» отдельным запросом по всей выборке (06 §4.4)
        # — берём готовое, а не повторяем расчёт третьим способом.
        # Метку свежести передаём и сюда: лист «Сводка» рядом считается по
        # ней живьём, и разойтись двум листам одной выгрузки не на чем.
        payload = await managers(db, period, filters, mv_refreshed_at=refreshed_at)
        rows = payload["rows"]
        totals = payload["totals"]
        table = [
            [
                row["full_name"],
                row["taken"],
                row["answered"],
                row["closed"],
                row["frt_avg_sec"],
                row["frt_median_sec"],
                row["frt_median_biz_sec"],
                row["messages_sent"],
            ]
            for row in rows
        ]
        # ⚠ «ИТОГО» ОБЪЯСНЯЕТ СЕБЯ ПРЯМО В ЯЧЕЙКЕ (правка 08.09, H-02). На
        # экране оговорка живёт в подсказке колонки, а в файле подсказок нет
        # вовсе: руководитель складывает столбец, получает больше итога и
        # читает это как ошибку счёта. Причина — передачи: по замеру боя через
        # двух и более исполнителей проходит треть диалогов, и каждому из них
        # диалог засчитан по праву, а в итоге он один.
        table.append(
            [
                "Итого (диалог считается один раз)",
                totals["taken"],
                totals["answered"],
                totals["closed"],
                # Среднего по всем у API нет, и выдумывать его здесь нельзя:
                # среднее средних — не среднее. Пустая клетка честнее.
                None,
                totals["frt_median_sec"],
                totals["frt_median_biz_sec"],
                totals["messages_sent"],
            ]
        )
        prepared.append(Sheet("Менеджеры", MANAGER_HEADERS, _iter_rows(table)))
    if "conversations" in sheets:
        prepared.append(
            Sheet(
                "Диалоги",
                CONV_HEADERS,
                conversation_rows(db, period, filters),
                limited=True,
            )
        )
    return await write_xlsx(path, prepared)


# ============================================================== обслуживание (job'ы)

REFRESH_MV_SQL = f"REFRESH MATERIALIZED VIEW CONCURRENTLY {MV}"
REFRESH_STATEMENT_TIMEOUT = "120s"


def cleanup_export_files(now: datetime | None = None, *, days: int = EXPORT_RETENTION_DAYS) -> int:
    """Удаляет файлы выгрузок старше ``days`` суток (06 §5.4). -> сколько удалено."""
    directory = export_dir()
    if not directory.is_dir():
        return 0
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).timestamp()
    removed = 0
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError as exc:
            if exc.errno in READ_ONLY_ERRNOS:
                log.error("stats.export_cleanup_readonly", path=str(directory), error=str(exc))
                return removed
            log.warning("stats.export_cleanup_failed", path=str(entry))  # забрали параллельно
    return removed


__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_SHEETS",
    "MANAGER_SORTS",
    "TIMESERIES_GROUPS",
    "TIMESERIES_METRICS",
    "Filters",
    "Period",
    "business_seconds_between",
    "cleanup_export_files",
    "create_export_job",
    "delta_pct",
    "export_stats",
    "export_status",
    "heatmap",
    "managers",
    "my_today",
    "parse_period",
    "refreshed_at",
    "summary",
    "timeseries",
]
