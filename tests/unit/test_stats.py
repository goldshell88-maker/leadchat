"""Юнит-тесты статистики (07 §1.1): без БД и без Redis-сервера.

Три группы:

1. **Рабочие часы** — эталон ``business_seconds_between`` на Python. Он
   дословный порт SQL-функции 06 §2.1, поэтому проверяем здесь всю семантику
   (границы 10:00/20:00, переход суток, ответ до начала дня, ``t1 < t0``,
   NULL), а интеграционный тест сверяет SQL с этим же эталоном на одних
   данных — расхождение реализаций не проедет мимо CI.
2. **Чистая арифметика отчёта** — период, предыдущий период, дельты,
   зануление рядов, лимиты экспорта, формат CSV/XLSX.
3. **RBAC и валидация endpoint'ов** — матрица прав из DESIGN §5.1 и коды
   ошибок 06 §4. SQL здесь не выполняется (unit-база — SQLite): сервисные
   функции подменяются, проверяется контур роутера.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.errors import ApiError
from app.services import stats as st

MSK = st.MSK


def msk(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Момент по московским стенным часам (в БД он лежит как UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


# ============================================================ рабочие часы (06 §2.1)


def test_business_seconds_inside_working_hours():
    """Целиком внутри 10:00–20:00 — астрономическое время равно рабочему."""
    assert st.business_seconds_between(msk(2026, 8, 3, 11, 0), msk(2026, 8, 3, 11, 30)) == 1800


def test_business_seconds_clips_to_day_start_and_end():
    """Границы суток: до 10:00 и после 20:00 вырезаются."""
    # 08:00 → 21:00: считается только 10:00–20:00 = 10 часов
    assert st.business_seconds_between(msk(2026, 8, 3, 8, 0), msk(2026, 8, 3, 21, 0)) == 10 * 3600
    # 09:00 → 10:05: пять минут рабочего времени
    assert st.business_seconds_between(msk(2026, 8, 3, 9, 0), msk(2026, 8, 3, 10, 5)) == 300


def test_business_seconds_answer_before_working_day_is_zero():
    """Нормативная семантика 06 §1.1: ответ до начала дня → рабочий FRT = 0
    («клиент не ждал в рабочее время нисколько»)."""
    assert st.business_seconds_between(msk(2026, 8, 3, 7, 0), msk(2026, 8, 3, 9, 30)) == 0
    # ...и после конца дня — тоже 0
    assert st.business_seconds_between(msk(2026, 8, 3, 20, 30), msk(2026, 8, 3, 23, 0)) == 0


def test_business_seconds_across_midnight():
    """Пример из 06 §1.1: клиент в 23:00, оператор в 10:05 → 5 минут рабочих."""
    assert st.business_seconds_between(msk(2026, 8, 3, 23, 0), msk(2026, 8, 4, 10, 5)) == 300


def test_business_seconds_multiday_counts_every_day():
    """Выходных нет — 7 дней в неделю (DESIGN §4.2), только часы.
    Суббота 1 августа 2026 и воскресенье 2-е считаются как рабочие."""
    saturday, sunday = date(2026, 8, 1), date(2026, 8, 2)
    assert saturday.isoweekday() == 6 and sunday.isoweekday() == 7
    # пт 19:00 → вс 11:00: 1 ч (пт) + 10 ч (сб) + 1 ч (вс)
    assert (
        st.business_seconds_between(msk(2026, 7, 31, 19, 0), msk(2026, 8, 2, 11, 0))
        == (1 + 10 + 1) * 3600
    )


def test_business_seconds_reversed_interval_is_zero():
    """``t1 < t0`` — generate_series пуст, сумма NULL → COALESCE 0."""
    assert st.business_seconds_between(msk(2026, 8, 3, 15, 0), msk(2026, 8, 3, 11, 0)) == 0


def test_business_seconds_is_strict_on_null():
    """STRICT: диалог без ответа даёт None, а не 0 — иначе он испортит среднее."""
    assert st.business_seconds_between(None, msk(2026, 8, 3, 11, 0)) is None
    assert st.business_seconds_between(msk(2026, 8, 3, 11, 0), None) is None


def test_business_seconds_custom_window():
    """Границы окна — параметры функции (06 §2.1), а не константы."""
    assert (
        st.business_seconds_between(
            msk(2026, 8, 3, 0, 0),
            msk(2026, 8, 3, 23, 59),
            timedelta(hours=9),
            timedelta(hours=18),
        )
        == 9 * 3600
    )


def test_business_seconds_accepts_utc_input():
    """На вход приходят UTC-таймстемпы из БД; конвертация в МСК — внутри."""
    t0 = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)  # 09:00 МСК
    t1 = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)  # 11:00 МСК
    assert st.business_seconds_between(t0, t1) == 3600


# ==================================================================== период (06 §4)


def test_period_bounds_are_msk_days_in_utc():
    """`[ts_from, ts_to)` — полуинтервал по московским суткам (06 §0.1)."""
    period = st.Period(date(2026, 8, 1), date(2026, 8, 4))
    assert period.days == 4
    assert period.ts_from == datetime(2026, 7, 31, 21, 0, tzinfo=UTC)  # 01.08 00:00 МСК
    assert period.ts_to == datetime(2026, 8, 4, 21, 0, tzinfo=UTC)  # 05.08 00:00 МСК


def test_previous_period_matches_doc_example():
    """06 §4.1: 2026-08-01..04 → 2026-07-28..31 (та же длина, вплотную)."""
    prev = st.Period(date(2026, 8, 1), date(2026, 8, 4)).previous()
    assert prev.date_from == date(2026, 7, 28)
    assert prev.date_to == date(2026, 7, 31)
    assert prev.days == 4


def test_parse_period_defaults_to_last_30_days():
    period = st.parse_period(None, None)
    assert period.days == 30
    assert period.date_to == st.today_msk()


def test_parse_period_clips_future_dates():
    """Будущие даты обрезаются до сегодня (06 §4)."""
    future = st.today_msk() + timedelta(days=10)
    period = st.parse_period(st.today_msk(), future)
    assert period.date_to == st.today_msk()


def test_parse_period_clips_both_ends_of_a_future_range():
    """Диапазон целиком в будущем — пустой отчёт за сегодня, а не 400: в
    календаре фронта такой выбор делается одним промахом мыши (06 §4)."""
    today = st.today_msk()
    period = st.parse_period(today + timedelta(days=3), today + timedelta(days=10))
    assert (period.date_from, period.date_to) == (today, today)
    assert period.days == 1


def test_parse_period_still_rejects_reversed_future_range():
    """Обрезка не должна «чинить» перевёрнутый диапазон: пользователь получил
    бы молча не тот период вместо ошибки."""
    today = st.today_msk()
    with pytest.raises(ApiError) as exc:
        st.parse_period(today + timedelta(days=10), today + timedelta(days=3))
    assert exc.value.status == 400


def test_parse_period_rejects_reversed_range():
    with pytest.raises(ApiError) as exc:
        st.parse_period(date(2026, 8, 4), date(2026, 8, 1))
    assert exc.value.status == 400


def test_parse_period_rejects_too_long_range():
    with pytest.raises(ApiError) as exc:
        st.parse_period(date(2025, 1, 1), date(2026, 6, 1))
    assert exc.value.code == "period_too_long"
    assert exc.value.status == 400


def test_parse_period_allows_exactly_366_days():
    start = st.today_msk() - timedelta(days=365)
    assert st.parse_period(start, st.today_msk()).days == 366


# ==================================================================== дельты (06 §4.1)


@pytest.mark.parametrize(
    "value,prev,expected",
    [
        (312, 280, 11.4),
        (290, 301, -3.7),
        (10, 0, None),  # деления на ноль нет — «не с чем сравнивать»
        (10, None, None),
        (None, 10, None),
        (0, 5, -100.0),
    ],
)
def test_delta_pct(value, prev, expected):
    assert st.delta_pct(value, prev) == expected


def test_delta_pp_is_percentage_points():
    """У процентных метрик дельта — разница в п.п., а не относительный рост."""
    assert st.delta_pp(18.6, 15.2) == 3.4
    assert st.delta_pp(18.6, None) is None


def test_card_shape_and_snapshot_has_no_prev():
    assert st.card(5, 4) == {"value": 5, "prev": 4, "delta_pct": 25.0}
    assert st.card(47, comparable=False) == {"value": 47, "prev": None, "delta_pct": None}


# ================================================================ ряды (06 §4.2)


def test_buckets_day_grid_is_dense():
    period = st.Period(date(2026, 8, 1), date(2026, 8, 4))
    labels = [st._bucket_label(b, "day") for b in st._buckets(period, "day")]
    assert labels == ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]


def test_buckets_hour_grid_covers_whole_days():
    period = st.Period(date(2026, 8, 1), date(2026, 8, 1))
    buckets = st._buckets(period, "hour")
    assert len(buckets) == 24
    assert st._bucket_label(buckets[13], "hour") == "2026-08-01T13:00"


# ============================================================ экспорт: лимиты и файлы


def test_seconds_to_msk_midnight_is_positive_and_under_a_day():
    ttl = st.seconds_to_msk_midnight(datetime(2026, 8, 4, 21, 30, tzinfo=UTC))
    assert 0 < ttl <= 86_400


def test_seconds_to_msk_midnight_counts_to_moscow_midnight():
    # 20:00 UTC = 23:00 МСК → до полуночи МСК ровно час
    assert st.seconds_to_msk_midnight(datetime(2026, 8, 4, 20, 0, tzinfo=UTC)) == 3600


def test_export_filename_carries_period_and_job_prefix():
    name = st.export_filename(st.Period(date(2026, 8, 1), date(2026, 8, 4)), "xlsx", "b8c4d1e2f3")
    assert name == "leadchat-stats_2026-08-01_2026-08-04_b8c4d1e2.xlsx"


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, "—"), (0, "0 с"), (35, "35 с"), (95, "1 мин 35 с"), (3900, "1 ч 5 мин")],
)
def test_humanize_seconds(seconds, expected):
    assert st.humanize_seconds(seconds) == expected


async def _arows(rows: Sequence[Sequence[Any]]) -> AsyncIterator[Sequence[Any]]:
    for row in rows:
        yield row


async def test_write_csv_is_russian_excel_friendly(tmp_path):
    """utf-8-sig (BOM), разделитель ';', CRLF — иначе русский Excel ломается."""
    path = tmp_path / "export.csv"
    written = await st.write_csv(path, ["Клиент", "Телефон"], _arows([["Иван", None]]))
    assert written == 1
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM
    assert b";" in raw and b"\r\n" in raw
    text = raw.decode("utf-8-sig")
    assert text.splitlines()[1] == "Иван;"  # None → пустая ячейка, а не 'None'


async def test_write_csv_enforces_row_limit(tmp_path):
    with pytest.raises(st.ExportTooLarge):
        await st.write_csv(tmp_path / "big.csv", ["a"], _arows([[1], [2], [3]]), limit=2)


async def test_write_xlsx_write_only_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "export.xlsx"
    sheets = [
        st.Sheet("Сводка", ["Показатель", "Значение"], _arows([["Диалогов новых", 312]])),
        st.Sheet("Диалоги", list(st.CONV_HEADERS), _arows([[1] * len(st.CONV_HEADERS)]), True),
    ]
    counted = await st.write_xlsx(path, sheets)
    assert counted == 1  # считается только лимитируемый лист
    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == ["Сводка", "Диалоги"]


async def test_xlsx_never_turns_a_clients_name_into_a_formula(tmp_path):
    """Имя клиента «=1+1» обязано остаться текстом, а не стать вычислением.

    `openpyxl` разбирает значение при записи: строка, начинающаяся с «=»,
    попадает в файл ФОРМУЛОЙ. Имя клиента и название объявления приходят из
    Авито — их набирает посторонний человек, и в отчёте владельца ячейка
    «Клиент» показывала бы результат вычисления вместо того, о ком строка.
    `=HYPERLINK(...)` в имени делал бы из него ссылку.

    Проверяется `data_type`, а не текст: значение-то openpyxl вернёт то же
    самое («=1+1» — это и есть исходник формулы), разница видна только в типе
    ячейки — и только она определяет, что покажет Excel.
    """
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "attack.xlsx"
    rows = [
        ["=1+1", '=HYPERLINK("http://evil","счёт")', "+79161234567", "Иван", None, 42],
    ]
    await st.write_xlsx(path, [st.Sheet("Диалоги", ["a", "b", "c", "d", "e", "f"], _arows(rows))])

    sheet = openpyxl.load_workbook(path)["Диалоги"]
    cells = list(sheet.iter_rows())[1]
    assert [c.data_type for c in cells[:4]] == ["s", "s", "s", "s"]
    assert cells[0].value == "=1+1"  # текст сохранён буква в букву
    assert cells[5].value == 42  # числа остались числами
    assert cells[5].data_type == "n"


async def test_mv_freshness_label_never_pretends_data_is_fresh():
    """Нет метки — так и написано. Тихо подставить «сейчас» здесь нельзя:
    возраст чисел неизвестен, и владелец должен это прочитать."""
    assert st.mv_freshness_label("2026-08-11T17:05:00+00:00") == "2026-08-11 20:05 (МСК)"
    assert "неизвестно" in st.mv_freshness_label(None)
    assert "неизвестно" in st.mv_freshness_label("не дата")


async def test_write_xlsx_enforces_conversation_limit(tmp_path):
    pytest.importorskip("openpyxl")
    sheets = [st.Sheet("Диалоги", ["a"], _arows([[1], [2], [3]]), True)]
    with pytest.raises(st.ExportTooLarge):
        await st.write_xlsx(tmp_path / "big.xlsx", sheets, limit=2)


async def test_scheduler_registers_hourly_refresh_and_daily_cleanup():
    """06 §3.2: refresh MV — ежечасно в HH:05; уборка — раз в сутки.

    Уборок теперь ДВЕ: выгрузки статистики и брошенные вложения (#22). Обе
    ходят по одному каталогу, и разведены по времени намеренно — работы у них
    на секунды, мешать друг другу незачем.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.scheduler.jobs import stats as stats_job

    scheduler = AsyncIOScheduler(timezone="UTC")
    stats_job.register(scheduler)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        stats_job.REFRESH_JOB_ID,
        stats_job.CLEANUP_JOB_ID,
        "media_orphan_cleanup",
    }
    assert "minute='5'" in str(jobs[stats_job.REFRESH_JOB_ID].trigger)
    assert "hour='3'" in str(jobs[stats_job.CLEANUP_JOB_ID].trigger)
    # Уборка вложений — своим часом, а не тем же: см. докстроку выше.
    assert "hour='4'" in str(jobs["media_orphan_cleanup"].trigger)
    # оба job'а не должны наслаиваться сами на себя
    for job in jobs.values():
        assert job.max_instances == 1
        assert job.coalesce is True


async def test_scheduler_process_actually_registers_stats_jobs():
    """Регистрации мало в модуле job'ов — её должен звать процесс планировщика,
    иначе MV не пересчитывается никогда, а refreshed_at остаётся null."""
    from app.scheduler.jobs import stats as stats_job
    from app.scheduler.main import build_scheduler

    ids = {job.id for job in build_scheduler().get_jobs()}
    assert {stats_job.REFRESH_JOB_ID, stats_job.CLEANUP_JOB_ID} <= ids
    # ...и job'ы прежних спринтов никуда не делись
    assert {"heartbeat", "token_refresh", "reconcile", "partitions", "raw_log_cleanup"} <= ids


def test_worker_registers_the_export_job():
    """Без этой строки POST /stats/export отдаёт 202, а job навсегда pending."""
    from app.workers.main import WorkerSettings

    assert st.export_stats in WorkerSettings.functions
    # ARQ берёт имя задачи из __qualname__ — оно обязано совпадать с тем,
    # под которым create_export_job кладёт задачу в очередь.
    assert st.export_stats.__qualname__ == st.EXPORT_JOB


async def test_reserve_export_slot_rejects_second_concurrent_export(redis):
    user_id = uuid.uuid4()
    await st.reserve_export_slot(redis, user_id, "job-1")
    with pytest.raises(ApiError) as exc:
        await st.reserve_export_slot(redis, user_id, "job-2")
    assert exc.value.code == "export_already_running"
    assert exc.value.status == 409
    # квота не должна была израсходоваться на отказанной попытке
    assert int(await redis.get(st.export_quota_key(user_id))) == 1


async def test_reserve_export_slot_enforces_daily_limit(redis):
    user_id = uuid.uuid4()
    for _ in range(st.EXPORT_DAILY_LIMIT):
        await st.reserve_export_slot(redis, user_id, "job")
        await st.release_export_slot(redis, user_id)
    with pytest.raises(ApiError) as exc:
        await st.reserve_export_slot(redis, user_id, "job")
    assert exc.value.status == 429
    # активный лок откатан — иначе пользователь останется заперт до TTL
    assert not await redis.exists(st.export_active_key(user_id))


async def test_failed_enqueue_releases_slot_and_refunds_quota(redis, monkeypatch):
    """Очередь недоступна — пользователь не должен остаться ни запертым (409),
    ни с потраченной попыткой из 20 (06 §5.4 ограничивает выгрузки, а не
    отказы инфраструктуры)."""
    from arq.connections import ArqRedis

    async def boom(*args, **kwargs):
        raise ConnectionError("arq недоступен")

    monkeypatch.setattr(ArqRedis, "enqueue_job", boom)
    user_id = uuid.uuid4()
    period = st.parse_period(date(2026, 8, 1), date(2026, 8, 4))

    with pytest.raises(ApiError) as exc:
        await st.create_export_job(
            redis,
            user_id=user_id,
            period=period,
            filters=st.Filters(),
            fmt="csv",
            sheets=list(st.EXPORT_SHEETS),
        )
    assert exc.value.status == 503
    assert not await redis.exists(st.export_active_key(user_id))
    assert int(await redis.get(st.export_quota_key(user_id)) or 0) == 0


async def test_cleanup_export_files_removes_only_old(tmp_path, monkeypatch):
    monkeypatch.setattr(st.settings, "media_root", str(tmp_path))
    directory = st.export_dir()
    directory.mkdir(parents=True)
    fresh, stale = directory / "fresh.csv", directory / "stale.csv"
    fresh.write_text("x")
    stale.write_text("x")
    import os

    old = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    os.utime(stale, (old, old))
    assert st.cleanup_export_files() == 1
    assert fresh.exists() and not stale.exists()


# ======================================================= endpoints: RBAC и валидация


@pytest.fixture
def stats_app(app: FastAPI) -> FastAPI:
    """Приложение с подключённым роутером статистики.

    Регистрация в ``app/main.py`` — чужая зона (см. cross-boundary), поэтому
    фикстура работает в обоих состояниях: если роутер уже смонтирован, берём
    как есть (тогда тесты заодно проверяют боевое монтирование), иначе
    подключаем локально — RBAC-матрица обязана быть зелёной независимо от
    порядка работ.
    """
    mounted = any(getattr(route, "path", "").startswith("/api/v1/stats") for route in app.routes)
    if not mounted:
        from app.api.routes import stats as stats_routes

        app.include_router(stats_routes.router, prefix="/api/v1")
    return app


@pytest.fixture
async def stats_client(stats_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=stats_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


STATS_ALL_PATHS = [
    "/api/v1/stats/summary",
    "/api/v1/stats/timeseries",
    "/api/v1/stats/heatmap",
    "/api/v1/stats/managers",
]


@pytest.mark.parametrize("path", STATS_ALL_PATHS)
@pytest.mark.parametrize("role", ["manager", "observer"])
async def test_stats_all_endpoints_forbidden_for_manager_and_observer(
    stats_client, tokens, path, role
):
    """DESIGN §5.1: stats:all — только admin и head. Утечка чужих цифр
    менеджеру/наблюдателю — это утечка данных, а не косметика."""
    r = await stats_client.get(path, headers=auth(tokens, role))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("path", STATS_ALL_PATHS)
@pytest.mark.parametrize("role", ["manager", "observer"])
async def test_rbac_wins_over_query_validation(stats_client, tokens, path, role):
    """Отказ по правам обязан выигрывать у отказа по валидации: иначе по кодам
    ответов просматривается контур API, закрытый для роли."""
    r = await stats_client.get(
        path,
        params={"date_from": "2024-01-01", "date_to": "2026-01-01"},  # период > 366 дней
        headers=auth(tokens, role),
    )
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("path", [*STATS_ALL_PATHS, "/api/v1/stats/my/today"])
async def test_stats_endpoints_reject_anonymous(stats_client, path):
    r = await stats_client.get(path)
    assert r.status_code == 401


async def test_my_today_forbidden_for_observer(stats_client, tokens):
    r = await stats_client.get("/api/v1/stats/my/today", headers=auth(tokens, "observer"))
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["admin", "head", "manager"])
async def test_my_today_allowed_for_stats_own_roles(
    stats_client, tokens, users_by_role, monkeypatch, role
):
    """`user_id` берётся из JWT: каждый видит ровно свои цифры (06 §6.1)."""
    seen: dict[str, Any] = {}

    async def fake_my_today(db, redis, user_id):
        seen["user_id"] = user_id
        return {"date": "2026-08-04", "messages_sent_today": 7}

    monkeypatch.setattr(st, "my_today", fake_my_today)
    r = await stats_client.get("/api/v1/stats/my/today", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    assert r.json()["messages_sent_today"] == 7
    assert seen["user_id"] == users_by_role[role].id


@pytest.mark.parametrize("role", ["admin", "head"])
async def test_summary_allowed_for_stats_all_roles(stats_client, tokens, monkeypatch, role):
    async def fake_summary(db, period, filters, *, mv_refreshed_at=None):
        return {"period": period.as_dict(), "cards": {}}

    monkeypatch.setattr(st, "summary", fake_summary)
    r = await stats_client.get("/api/v1/stats/summary", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    # метка свежести MV есть в каждом ответе (06 §4)
    assert "refreshed_at" in r.json()


async def test_summary_reports_refreshed_at_from_redis(stats_client, tokens, redis, monkeypatch):
    """Метка свежести витрины и уходит в ответ, И доезжает до расчёта.

    Второе важнее первого: по ней `summary` решает, брать ли прошлые сутки с
    витрины вместо пересчёта всего периода живьём. Ручка, которая метку
    прочитала, но в расчёт не передала, выглядела бы полностью исправной —
    ответ тот же, просто на семь секунд медленнее.
    """
    дошло = {}

    async def fake_summary(db, period, filters, *, mv_refreshed_at=None):
        дошло["метка"] = mv_refreshed_at
        return {"cards": {}}

    monkeypatch.setattr(st, "summary", fake_summary)
    await redis.set(st.STATS_REFRESHED_KEY, "2026-08-04T11:05:12+00:00")
    r = await stats_client.get("/api/v1/stats/summary", headers=auth(tokens, "admin"))
    assert r.json()["refreshed_at"] == "2026-08-04T11:05:12+00:00"
    assert дошло["метка"] == "2026-08-04T11:05:12+00:00"


async def test_period_too_long_is_400(stats_client, tokens):
    r = await stats_client.get(
        "/api/v1/stats/summary",
        params={"date_from": "2024-01-01", "date_to": "2026-01-01"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "period_too_long"


async def test_timeseries_hour_group_requires_short_period(stats_client, tokens):
    """`group=hour` разрешён только на периоде ≤ 7 дней (06 §4.2)."""
    today = st.today_msk()
    r = await stats_client.get(
        "/api/v1/stats/timeseries",
        params={
            "date_from": (today - timedelta(days=8)).isoformat(),
            "date_to": today.isoformat(),
            "group": "hour",
        },
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_timeseries_rejects_unknown_metric(stats_client, tokens):
    r = await stats_client.get(
        "/api/v1/stats/timeseries",
        params={"metric": "profit"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 400


async def test_managers_rejects_unknown_sort(stats_client, tokens):
    r = await stats_client.get(
        "/api/v1/stats/managers",
        params={"sort": "salary"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 400


async def test_managers_accepts_whitelisted_sort(stats_client, tokens, monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_managers(db, period, filters, *, sort, order, mv_refreshed_at=None):
        captured.update(
            sort=sort, order=order, managers=filters.manager_ids, refreshed=mv_refreshed_at
        )
        return {"rows": [], "totals": {}}

    monkeypatch.setattr(st, "managers", fake_managers)
    manager_id = str(uuid.uuid4())
    r = await stats_client.get(
        "/api/v1/stats/managers",
        params={"sort": "frt_median_sec", "order": "asc", "manager_id": manager_id},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text
    assert captured["sort"] == "frt_median_sec"
    assert captured["order"] == "asc"
    # повторяемый manager_id доезжает до сервиса списком (06 §4)
    assert captured["managers"] == (uuid.UUID(manager_id),)
    # Метка свежести доезжает тоже: по ней таблица решает, брать ли витрину
    # или считать живьём (09.09). Без неё живой путь работал бы, но потерял бы
    # раскол — и период с сегодня считался бы живьём целиком.
    assert "refreshed" in captured


# ---------------------------------------------------------------- экспорт: endpoint


async def test_export_enqueues_job_and_returns_202(stats_client, tokens, redis):
    r = await stats_client.post(
        "/api/v1/stats/export",
        json={"format": "csv", "date_from": "2026-08-01", "date_to": "2026-08-04"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert await redis.exists(f"arq:job:{job_id}")
    assert await redis.hget(st.export_status_key(job_id), "status") == "pending"


async def test_export_second_request_is_409(stats_client, tokens):
    body = {"format": "csv", "date_from": "2026-08-01", "date_to": "2026-08-04"}
    first = await stats_client.post(
        "/api/v1/stats/export", json=body, headers=auth(tokens, "admin")
    )
    assert first.status_code == 202
    second = await stats_client.post(
        "/api/v1/stats/export", json=body, headers=auth(tokens, "admin")
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "export_already_running"


async def test_export_rejects_too_long_period(stats_client, tokens):
    r = await stats_client.post(
        "/api/v1/stats/export",
        json={"format": "csv", "date_from": "2024-01-01", "date_to": "2026-01-01"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "period_too_long"


@pytest.mark.parametrize("role", ["manager", "observer"])
async def test_export_forbidden_without_stats_all(stats_client, tokens, role):
    """Обе ручки экспорта стоят в COVERED_ELSEWHERE матрицы RBAC — DENY-сторона
    проверяется здесь (07 §1.1.3)."""
    r = await stats_client.post(
        "/api/v1/stats/export", json={"format": "csv"}, headers=auth(tokens, role)
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"

    status = await stats_client.get(
        f"/api/v1/stats/export/{uuid.uuid4().hex}", headers=auth(tokens, role)
    )
    # 403, а не 404: отказ по правам обязан выигрывать у «job не найден»
    assert status.status_code == 403
    assert status.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v1/stats/export", {"format": "csv"}),
        ("GET", "/api/v1/stats/export/deadbeef", None),
    ],
)
async def test_export_endpoints_reject_anonymous(stats_client, method, path, body):
    r = await stats_client.request(method, path, json=body)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_export_status_visible_only_to_author(stats_client, tokens, redis, users_by_role):
    post = await stats_client.post(
        "/api/v1/stats/export", json={"format": "csv"}, headers=auth(tokens, "admin")
    )
    job_id = post.json()["job_id"]

    mine = await stats_client.get(f"/api/v1/stats/export/{job_id}", headers=auth(tokens, "admin"))
    assert mine.status_code == 200
    assert mine.json()["status"] == "pending"

    # head тоже имеет stats:all, но чужой job для него не существует
    someone_else = await stats_client.get(
        f"/api/v1/stats/export/{job_id}", headers=auth(tokens, "head")
    )
    assert someone_else.status_code == 404


async def test_export_status_returns_signed_url_when_done(stats_client, tokens, redis):
    post = await stats_client.post(
        "/api/v1/stats/export", json={"format": "csv"}, headers=auth(tokens, "admin")
    )
    job_id = post.json()["job_id"]
    await redis.hset(
        st.export_status_key(job_id),
        mapping={"status": "done", "rows": "4211", "relpath": "exports/leadchat-stats.csv"},
    )
    r = await stats_client.get(f"/api/v1/stats/export/{job_id}", headers=auth(tokens, "admin"))
    body = r.json()
    assert body["status"] == "done"
    assert body["rows"] == 4211
    assert body["url"].startswith("/api/v1/media/exports/leadchat-stats.csv?sig=")
    assert body["expires_at"] is not None


async def test_export_status_unknown_job_is_404(stats_client, tokens):
    r = await stats_client.get("/api/v1/stats/export/deadbeef", headers=auth(tokens, "admin"))
    assert r.status_code == 404


# ------------------------------------------------- граница раскола живого периода


async def _пустой_снимок() -> dict[str, int]:
    return {"in_progress_now": 0, "waiting_now": 0, "inbox_now": 0}


class TestРасколЖивого:
    """`_раскол_живого` — единственный замок между «быстро» и «правда».

    Он решает, можно ли прошлые сутки живого периода взять с витрины вместо
    того, чтобы пересчитывать весь период по `messages` (замер 29.08: 6,77 с
    против 0,010 с). Ошибётся в одну сторону — страница снова висит девять
    секунд; ошибётся в другую — молча теряются сутки, и заметить это на глаз
    нельзя: цифра просто станет меньше.
    """

    @staticmethod
    def _период(дней: int) -> st.Period:
        сегодня = st.today_msk()
        return st.Period(сегодня - timedelta(days=дней - 1), сегодня)

    @staticmethod
    def _полночь() -> datetime:
        return st.msk_day_bounds(st.today_msk())[0]

    def test_витрина_покрывает_только_завершённый_период(self) -> None:
        """⚠ АУДИТ 30.08: предыдущий период брался с витрины ВСЕГДА.

        Текущий период сверяется расколом — метка старее полуночи, считаем
        живьём. Предыдущий этой проверки не имел, и при умершем пересчёте
        выходило худшее: сегодняшние числа верные (сработал живой путь),
        вчерашние обрезанные, а ДЕЛЬТА на карточках врёт молча. «−40 %» рядом
        с верной цифрой человек читает как падение и идёт разбираться с
        людьми, а не с витриной.
        """
        прошлый = self._период(7).previous()
        # Пересчёт прошёл ПОСЛЕ конца прошлого периода — витрина его вобрала.
        свежая = (прошлый.ts_to + timedelta(minutes=5)).isoformat()
        assert st._витрина_покрывает(прошлый, свежая) is True

        # Пересчёт остановился ДО конца периода — часть суток в витрину не попала.
        отставшая = (прошлый.ts_to - timedelta(hours=1)).isoformat()
        assert st._витрина_покрывает(прошлый, отставшая) is False, (
            "отставшей витрине верят — дельта на карточках соврёт молча"
        )

    async def test_проверка_действительно_подключена_к_summary(self, monkeypatch) -> None:
        """⚠ БЕЗ ЭТОГО ПРОВЕРКИ ВЫШЕ СТОРОЖИЛИ БЫ ФУНКЦИЮ, КОТОРУЮ НИКТО НЕ
        ЗОВЁТ.

        Они дёргают `_витрина_покрывает` напрямую и остались бы зелёными, забудь
        кто-нибудь применить её к предыдущему периоду. Механизм, написанный и не
        подключённый, — самая частая поломка в этом проекте; поймано диверсией.

        Здесь смотрим на ФАКТ вызова: с отставшей витриной предыдущий период
        обязан считаться живьём.
        """
        звонки: list[dict[str, object]] = []

        async def перехват(db, period, filters, *, live=False, split_at=None):  # noqa: ANN001
            звонки.append({"period": period, "live": live})
            return {
                "frt": {},
                "closed": 0,
                "bot_closed": {},
                "messages": {},
                "reaction": {},
            }

        async def настройка(db, key):  # noqa: ANN001 — рабочие часы читаются из базы
            return 0

        monkeypatch.setattr(st, "_period_metrics", перехват)
        monkeypatch.setattr(st, "snapshot_now", lambda db, filters: _пустой_снимок())
        monkeypatch.setattr(st.app_settings, "get", настройка)

        период = self._период(7)
        # Витрина отстала на сутки: конца прошлого периода она не вобрала.
        отставшая = (период.previous().ts_to - timedelta(hours=2)).isoformat()
        try:
            await st.summary(None, период, st.Filters(), mv_refreshed_at=отставшая)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — дальше идут расчёты по пустым словарям
            pass

        прошлые = [з for з in звонки if з["period"] == период.previous()]
        assert прошлые, "предыдущий период вообще не считался"
        assert прошлые[0]["live"] is True, (
            "предыдущий период взят с отставшей витрины — дельта на карточках соврёт"
        )

    def test_без_метки_витрине_не_верим(self) -> None:
        """Метки нет — значит и знания о свежести нет; считаем живьём."""
        assert st._витрина_покрывает(self._период(7).previous(), None) is False
        assert st._витрина_покрывает(self._период(7).previous(), "не-дата") is False

    def test_свежая_витрина_даёт_полночь(self) -> None:
        отметка = (self._полночь() + timedelta(minutes=5)).isoformat()
        assert st._раскол_живого(self._период(30), отметка) == self._полночь()

    def test_вчерашняя_витрина_отключает_раскол(self) -> None:
        """Пересчёт не прошёл после полуночи — вчерашних диалогов в витрине
        может не быть, и взять её значит потерять сутки."""
        отметка = (self._полночь() - timedelta(minutes=1)).isoformat()
        assert st._раскол_живого(self._период(30), отметка) is None

    def test_период_внутри_сегодня_расколу_не_подлежит(self) -> None:
        """Витринной половины просто нет — раскол был бы лишним запросом."""
        отметка = (self._полночь() + timedelta(minutes=5)).isoformat()
        assert st._раскол_живого(self._период(1), отметка) is None

    def test_без_метки_считаем_живьём(self) -> None:
        assert st._раскол_живого(self._период(30), None) is None

    def test_битая_метка_не_роняет_отчёт(self) -> None:
        """Redis отдал мусор — отчёт обязан выйти, пусть и медленно."""
        assert st._раскол_живого(self._период(30), "позавчера") is None

    def test_наивная_метка_читается_как_utc(self) -> None:
        """Метку пишет job; если она без зоны, сравнение с полуночью упало бы
        на `can't compare offset-naive and offset-aware`."""
        отметка = (self._полночь() + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        assert st._раскол_живого(self._период(30), отметка) == self._полночь()
