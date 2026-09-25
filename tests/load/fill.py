"""Наполнение базы объёмом, близким к боевому (план 7.5).

ЗАЧЕМ ОТДЕЛЬНО ОТ `release.js` И `inject.py`
--------------------------------------------
Те два меряют, как система держит ПОТОК: вебхуки в минуту, операторы на
сокетах, задержка доставки. Прошлый прогон дал запас в 10–22 раза — и это
честный результат, но полученный на пустой базе. Между «выдержит поток» и
«выдержит поток НА ЧЕТЫРЁХСТАХ ТЫСЯЧАХ диалогов» разница принципиальная:
второе ломается не пропускной способностью, а планами запросов. Сортировка,
которая на тринадцати строках незаметна, на четырёхстах тысячах становится
Seq Scan + Sort, и экран оператора открывается пять секунд.

Поэтому здесь — не нагрузка, а ОБЪЁМ: наполнить базу так, как она будет
выглядеть через год работы, и замерить на ней те запросы, которыми живут
экраны.

ПОЧЕМУ COPY, А НЕ ORM
---------------------
Четыреста тысяч диалогов и несколько миллионов сообщений через ORM — это часы
и гигабайты питоновских объектов. `COPY FROM STDIN` заливает те же данные за
минуты. Данные при этом не «случайный шум»: распределение статусов, длина
переписки и доля непрочитанных взяты из выгрузки заказчика (docs/15), иначе
замеры мерили бы несуществующую систему.

ВСЁ ПОМЕЧЕНО
------------
Клиенты, диалоги и сообщения несут метку :data:`MARK` — уборка идёт по ней и
только по ней. Это не осторожность ради осторожности: скрипт может быть
случайно запущен на базе с настоящими данными, и он обязан уметь убрать за
собой, не тронув чужое.

    python tests/load/fill.py --conversations 400000       # наполнить
    python tests/load/fill.py --measure                    # замерить
    python tests/load/fill.py --clean                      # убрать
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import random
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

MARK = "LOADFILL"

#: Распределение статусов — из выгрузки заказчика (docs/15 §1): подавляющая
#: часть истории закрыта, в работе единицы процентов.
STATUS_MIX = [("closed", 0.88), ("in_progress", 0.09), ("new", 0.03)]

#: Сообщений в диалоге. Короткие переписки преобладают, но хвост длинный —
#: и именно хвост создаёт нагрузку на ленту.
LENGTH_MIX = [(2, 0.35), (4, 0.30), (8, 0.20), (16, 0.10), (40, 0.05)]

ITEMS = [
    "Ремонт холодильника Bosch",
    "Стиральная машина Indesit",
    "Посудомоечная машина Electrolux",
    "Духовой шкаф Gorenje",
    "Варочная панель Hansa",
    "Морозильник Liebherr",
    "Кофемашина DeLonghi",
    "Вытяжка Krona",
]
BODIES = [
    "Здравствуйте, сколько стоит диагностика?",
    "Добрый день! Выезд бесплатный, диагностика 500 ₽.",
    "А когда мастер может подъехать?",
    "Сегодня после 16:00 удобно?",
    "Да, записывайте",
    "Не включается, мигает индикатор",
    "Спасибо, всё работает",
]


def pick(mix: list[tuple[object, float]]) -> object:
    r = random.random()
    acc = 0.0
    for value, weight in mix:
        acc += weight
        if r <= acc:
            return value
    return mix[-1][0]


async def connect() -> asyncpg.Connection:
    dsn = os.environ.get("FILL_DSN") or os.environ.get("DATABASE_URL", "")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        print("Не задан FILL_DSN или DATABASE_URL", file=sys.stderr)
        raise SystemExit(2)
    return await asyncpg.connect(dsn)


async def ensure_partitions(conn: asyncpg.Connection, months_back: int = 14) -> None:
    """Партиции `messages` за прошедший год.

    Планировщик создаёт их на текущий и следующий месяц (`ensure_message_partitions`)
    — этого хватает живой системе, которая накапливает историю по мере работы.
    Наполнителю нужны и прошлые: он кладёт годовую историю разом, и без
    партиций COPY падает на первом же сообщении «постарше».

    Именование и границы повторяют планировщик один в один — иначе на проде
    получились бы две параллельные схемы разбиения.
    """
    today = datetime.now(UTC).date().replace(day=1)
    made = 0
    for back in range(months_back, -2, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        start = f"{y:04d}-{m:02d}-01"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = f"{ny:04d}-{nm:02d}-01"
        name = f"messages_y{y:04d}m{m:02d}"
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF messages "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )
        made += 1
    print(f"Партиции обеспечены: {made}")


async def fill(conn: asyncpg.Connection, total: int, batch: int = 20_000) -> None:
    account_id = await conn.fetchval("select id from avito_accounts limit 1")
    if account_id is None:
        print("В базе нет ни одного аккаунта Авито — сначала заведите его", file=sys.stderr)
        raise SystemExit(2)
    rows = await conn.fetch("select id from users where role in ('admin','manager')")
    operators = [r["id"] for r in rows]
    if not operators:
        print("В базе нет операторов", file=sys.stderr)
        raise SystemExit(2)

    await ensure_partitions(conn)

    now = datetime.now(UTC)
    made = 0
    started = time.monotonic()

    while made < total:
        n = min(batch, total - made)
        clients = io.StringIO()
        convs = io.StringIO()
        msgs = io.StringIO()

        for i in range(n):
            seq = made + i
            client_id = uuid.uuid4()
            conv_id = uuid.uuid4()
            # Растягиваем историю на год: свежие диалоги сверху, старые внизу —
            # ровно так, как отбирает список и как листает таблица.
            age = timedelta(minutes=random.randint(0, 365 * 24 * 60))
            last_at = now - age
            status = str(pick(STATUS_MIX))
            length = int(pick(LENGTH_MIX))  # type: ignore[arg-type]
            assignee = random.choice(operators) if status != "new" else None
            unread = random.choice([0, 0, 0, 1, 2, 5]) if status != "closed" else 0

            clients.write(
                f"{client_id}\tavito\t{MARK}-{seq}\t{MARK} Клиент {seq}\t"
                f"+7900{seq % 10_000_000:07d}\t\\N\n"
            )
            convs.write(
                f"{conv_id}\tavito\t{MARK}-chat-{seq}\t{account_id}\t{client_id}\t"
                f"{assignee or chr(92) + 'N'}\t{status}\tf\t{{}}\t{{}}\t"
                f"{random.choice(ITEMS)}\t\\N\t\\N\t{last_at.isoformat()}\t"
                f"{last_at.isoformat()}\t{unread}\t\\N\t\\N\t\\N\t{{}}\t\\N\t\\N\n"
            )
            # Сообщения идут назад от последней активности — так же, как их
            # отдаёт лента (курсор от свежих к старым).
            for k in range(length):
                at = last_at - timedelta(minutes=k * 3)
                incoming = k % 2 == 0
                msgs.write(
                    f"{uuid.uuid4()}\t{conv_id}\t\\N\t"
                    f"{'in' if incoming else 'out'}\t{'client' if incoming else 'operator'}\t"
                    f"{(assignee if not incoming else None) or chr(92) + 'N'}\t"
                    f"{random.choice(BODIES)} [{MARK}]\t[]\tdelivered\t{at.isoformat()}\n"
                )

        # Батч заливается ОДНОЙ транзакцией. Иначе сбой на последней таблице
        # оставляет клиентов без диалогов, а повторный запуск падает на
        # дубликате внешнего идентификатора — то есть первая же ошибка делает
        # скрипт неперезапускаемым.
        async with conn.transaction():
            for buf, table, cols in (
                (clients, "clients", "id,channel,external_id,name,phone,avito_rating"),
                (
                    convs,
                    "conversations",
                    "id,channel,external_chat_id,account_id,client_id,assignee_id,status,"
                    "bot_active,bot_vars,tags,item_title,item_url,item_price,"
                    "last_message_at,updated_at,unread_count,offered_at,claimed_by_id,"
                    "claimed_at,declined_by,escalated_at,auto_assigned_at",
                ),
                (
                    msgs,
                    "messages",
                    "id,conversation_id,external_message_id,direction,"
                    "sender_type,sender_user_id,body,attachments,delivery_status,created_at",
                ),
            ):
                # asyncpg ждёт байты, а не текст: COPY идёт по протоколу как есть.
                await conn.copy_to_table(
                    table,
                    source=io.BytesIO(buf.getvalue().encode("utf-8")),
                    columns=cols.split(","),
                    format="text",
                )

        made += n
        rate = made / max(time.monotonic() - started, 0.001)
        print(f"  {made:>7}/{total}  ({rate:.0f} диалогов/с)", flush=True)

    await conn.execute("analyze conversations; analyze messages; analyze clients;")
    print(f"Готово за {time.monotonic() - started:.0f} с")


#: Запросы, которыми живут экраны. Каждый — то, что человек делает руками.
QUERIES: list[tuple[str, str]] = [
    (
        "список диалогов, вкладка «Все» (первая страница)",
        """select c.id from conversations c join clients cl on cl.id = c.client_id
           where c.status <> 'closed'
           order by (c.unread_count > 0) desc, c.last_message_at desc nulls last, c.id
           limit 50""",
    ),
    (
        "список «Мои» у оператора",
        """select c.id from conversations c join clients cl on cl.id = c.client_id
           where c.assignee_id = (select id from users where role='manager' limit 1)
             and c.status <> 'closed'
           order by (c.unread_count > 0) desc, c.last_message_at desc nulls last, c.id
           limit 50""",
    ),
    (
        "очередь «Входящие»",
        """select c.id from conversations c
           where c.offered_at is not null and c.claimed_by_id is null and c.status <> 'closed'
           order by c.offered_at limit 50""",
    ),
    (
        "поиск по имени клиента",
        """select c.id from conversations c join clients cl on cl.id = c.client_id
           where cl.name ilike '%Клиент 12345%' limit 50""",
    ),
    (
        "поиск по телефону",
        """select c.id from conversations c join clients cl on cl.id = c.client_id
           where cl.phone like '%9001234%' limit 50""",
    ),
    (
        "таблица диалогов: страница (7.3)",
        """select id from conversations
           where last_message_at >= now() - interval '30 days'
           order by last_message_at desc nulls last, id limit 50""",
    ),
    (
        "таблица диалогов: метрики страницы (7.3)",
        """select c.id,
             (select min(m.created_at) from messages m
               where m.conversation_id = c.id and m.direction='in') as first_in,
             (select min(m.created_at) from messages m
               where m.conversation_id = c.id and m.direction='out'
                 and m.sender_type='operator') as first_out,
             (select count(*) from messages m
               where m.conversation_id = c.id and m.direction in ('in','out')) as cnt
           from conversations c
           where c.id in (select id from conversations
                          order by last_message_at desc nulls last, id limit 50)""",
    ),
    (
        "сторож возврата диалогов (7.7)",
        """select id from conversations
           where auto_assigned_at is not null and auto_assigned_at < now() - interval '3 minutes'
             and assignee_id is not null and status <> 'closed'
           order by auto_assigned_at limit 100""",
    ),
    (
        "нагрузка операторов для автораздачи (7.6)",
        """select assignee_id, count(*) from conversations
           where assignee_id is not null and status in ('new','in_progress')
           group by assignee_id""",
    ),
    (
        "лента диалога (последние 50 сообщений)",
        """select m.id from messages m
           where m.conversation_id = (select id from conversations
                                      order by last_message_at desc limit 1)
           order by m.created_at desc limit 50""",
    ),
    (
        "счётчик «Найдено» в таблице",
        "select count(*) from conversations where last_message_at >= now() - interval '30 days'",
    ),
]

#: Порог, выше которого экран ощущается медленным. Не выдумка: 200 мс — граница,
#: за которой отклик перестаёт восприниматься как мгновенный.
SLOW_MS = 200.0


async def measure(conn: asyncpg.Connection, runs: int = 5) -> int:
    total = await conn.fetchval("select count(*) from conversations")
    messages = await conn.fetchval("select count(*) from messages")
    size = await conn.fetchval("select pg_size_pretty(pg_database_size(current_database()))")
    print(f"Объём: {total} диалогов, {messages} сообщений, {size}\n")

    failures = 0
    for name, sql in QUERIES:
        times = []
        for _ in range(runs):
            t0 = time.monotonic()
            await conn.fetch(sql)
            times.append((time.monotonic() - t0) * 1000)
        med = statistics.median(times)
        worst = max(times)
        flag = "  ⚠️ МЕДЛЕННО" if med > SLOW_MS else ""
        if med > SLOW_MS:
            failures += 1
        print(f"{med:8.1f} мс (худший {worst:6.1f})  {name}{flag}")

    print()
    if failures:
        print(f"Медленнее {SLOW_MS:.0f} мс: {failures} запрос(ов) — смотрите планы.")
    else:
        print(f"Все запросы укладываются в {SLOW_MS:.0f} мс.")
    return failures


async def clean(conn: asyncpg.Connection) -> None:
    """Уборка строго по метке — чужие данные не трогаем."""
    n = await conn.fetchval(
        f"select count(*) from conversations where external_chat_id like '{MARK}-%'"
    )
    await conn.execute(
        f"""delete from messages where conversation_id in
            (select id from conversations where external_chat_id like '{MARK}-%')"""
    )
    await conn.execute(f"delete from conversations where external_chat_id like '{MARK}-%'")
    await conn.execute(f"delete from clients where external_id like '{MARK}-%'")
    await conn.execute("analyze conversations; analyze messages; analyze clients;")
    print(f"Убрано {n} диалогов с меткой {MARK}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=0)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    conn = await connect()
    try:
        if args.conversations:
            await fill(conn, args.conversations)
        if args.measure:
            return await measure(conn, runs=args.runs)
        if args.clean:
            await clean(conn)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
