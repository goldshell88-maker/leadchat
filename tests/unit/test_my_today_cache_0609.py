"""`GET /stats/my/today`: кэш переживает интервал опроса, ветка frt сужена по дате.

ЗАМЕР БОЯ 06.09. p50 49 / p95 130 мс на каждый опрос, и опросов — по одному в
минуту от каждого из тринадцати: кэш жил 30 с, виджет приходил через 60 с
(`MyTodayWidget.tsx`, `refetchInterval: 60_000`), то есть промах ВСЕГДА.
В SQL ветка frt шла LATERAL'ами по всем диалогам, где сегодня хоть что-то
было (677 у всей компании), и по 28 партициям для каждого — 48 671 буфер.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ, НА SQLite. Сам SQL виджета — Postgres (percentile_cont,
FILTER, EXTRACT), поэтому база подменена счётчиком: второй вызов в пределах
TTL в базу не идёт, ключ живёт дольше интервала опроса. Форма ветки frt
проверяется по тексту: предфильтр «этот человек сегодня отвечал здесь» стоит,
а ВНУТРИ поиска первого слова клиента границы по дате нет — она сделала бы
вчерашний диалог сегодняшним. Числа на данных вне дня — в
`tests/integration/test_perf_0609_pg.py`.

ДИВЕРСИИ (каждая дала красный, восстановлено байт в байт):
  - `MY_TODAY_TTL_SECONDS = 120` → `30` — упал `test_ttl_дольше_интервала_опроса`;
  - из `_MY_TODAY_SQL` убран EXISTS-предфильтр — упал
    `test_ветка_frt_сужена_предфильтром_а_не_границей_внутри_f`.
"""

from __future__ import annotations

import uuid

from app.services import stats as st

ИНТЕРВАЛ_ОПРОСА_ВИДЖЕТА_С = 60  # MyTodayWidget.tsx: refetchInterval 60_000


async def test_второй_вызов_в_пределах_ttl_не_идёт_в_базу(db, redis, monkeypatch):
    вызовов = 0

    async def _row(db_, query, params):  # noqa: ANN001
        nonlocal вызовов
        вызовов += 1
        return {"answered_today": 3, "frt_median_sec_today": 42, "taken_today": 1}

    monkeypatch.setattr(st, "_row", _row)
    user_id = uuid.uuid4()

    первый = await st.my_today(db, redis, user_id)
    второй = await st.my_today(db, redis, user_id)

    assert вызовов == 1, "второй вызов внутри TTL пошёл в базу"
    assert первый == второй
    assert первый["answered_today"] == 3 and первый["frt_median_sec_today"] == 42
    assert первый["active_now"] == 0  # чего в строке нет — ноль, а не падение

    # Чужой ключ — свой счёт: кэш пер-пользовательский.
    await st.my_today(db, redis, uuid.uuid4())
    assert вызовов == 2


async def test_ttl_дольше_интервала_опроса(db, redis, monkeypatch):
    """⚠ TTL короче интервала опроса — это кэш, который не попадает никогда."""
    assert st.MY_TODAY_TTL_SECONDS >= 120
    assert st.MY_TODAY_TTL_SECONDS > ИНТЕРВАЛ_ОПРОСА_ВИДЖЕТА_С

    async def _row(db_, query, params):  # noqa: ANN001
        return {}

    monkeypatch.setattr(st, "_row", _row)
    user_id = uuid.uuid4()
    await st.my_today(db, redis, user_id)
    key = f"stats:my:{user_id}:{st.today_msk().isoformat()}"
    ttl = await redis.ttl(key)
    assert ИНТЕРВАЛ_ОПРОСА_ВИДЖЕТА_С < ttl <= st.MY_TODAY_TTL_SECONDS


def test_ветка_frt_сужена_предфильтром_а_не_границей_внутри_f() -> None:
    """Проводка: где граница по дате стоять обязана, а где — не имеет права."""
    sql = st._MY_TODAY_SQL
    # Комментарии в SQL цитируют те же условия — режем их, чтобы искать по коду.
    код = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    frt = код[код.index("frt AS (") :]

    # Предфильтр: сегодняшние доставленные ответы ЭТОГО человека — до латералов.
    exists = frt[frt.index("EXISTS (") : frt.index("f.first_client_at >= :day_start")]
    assert "m.created_at >= :day_start" in exists
    assert "m.sender_user_id = :user_id" in exists
    assert "m.delivery_status <> 'failed'" in exists

    # Внутри `f` даты нет: первое слово клиента ищется по всей истории.
    f = frt[frt.index("SELECT min(m.created_at)") : frt.index(") f")]
    assert ":day_start" not in f, "граница внутри f делает вчерашний диалог сегодняшним"

    # А в `op` — есть: она ничего не отсекает и даёт отсечь прошлые партиции.
    op = frt[frt.index(") f") : frt.index(") op")]
    assert "m.created_at >= :day_start" in op

    # Прежняя граница по диалогу на месте.
    assert "c.last_message_at >= :day_start" in frt
