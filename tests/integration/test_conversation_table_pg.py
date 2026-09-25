"""Таблица разбора на настоящем PostgreSQL (07 §1.2).

ЗАЧЕМ ОТДЕЛЬНО ОТ ЮНИТОВ. У этого модуля две ветки, которых юниты не видят
вовсе, потому что идут на SQLite:

1. Порядок сортировки по «первому ответу». На SQLite разность меток времени
   считается через ``julianday``, на PostgreSQL — через ``extract(epoch ...)``
   от интервала. Это разный SQL, и зелёный юнит не говорит о проде ничего.
   Цена ошибки высокая: экран заводили ради вопроса «где ответили дольше
   пятнадцати минут», и неверный порядок отвечает на него неправильно молча —
   строки же как-то расположены.

2. Поиск ``q=``. На SQLite он вырождается в ``LIKE`` по телу сообщения, на
   PostgreSQL идёт через полнотекстовый индекс
   (``messages.search @@ websearch_to_tsquery('russian', …)``) — то есть по
   ЛЕММАМ, а не по подстроке. Здесь же проверяется соединение с ``clients``:
   без него счётчик и страница считались бы по разным множествам.

Данные наливаются руками и ожидания посчитаны от них — «известные данные»,
как в test_stats_pg.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import ApiError
from app.models import AvitoAccount, Client, Conversation, Message, User
from app.services import conversation_table as tbl
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

#: Опорный момент — вчера: и партиция уже есть, и «за 30 дней» его застаёт.
T0 = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )


async def ensure_partitions(engine: AsyncEngine) -> None:
    """Партиции ``messages`` на прошлый, текущий и следующий месяц."""
    first = datetime.now(UTC).date().replace(day=1)
    cursor = (first - timedelta(days=1)).replace(day=1)
    async with engine.begin() as conn:
        for _ in range(3):
            end = (cursor + timedelta(days=32)).replace(day=1)
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS messages_y{cursor.year:04d}m{cursor.month:02d} "
                    f"PARTITION OF messages FOR VALUES FROM ('{cursor}') TO ('{end}')"
                )
            )
            cursor = end


@pytest.fixture
async def seed(pg_engine: AsyncEngine, sessionmaker) -> dict[str, uuid.UUID]:
    """Четыре диалога с разным временем первого ответа, включая оба крайних.

    «Не ответили» и «оператор написал первым» — не медленный и не быстрый
    ответ, а отсутствие величины: обе строки обязаны вести себя одинаково.
    """
    await ensure_partitions(pg_engine)

    async with sessionmaker() as s:
        operator = User(
            email="frt@table.test",
            password_hash="x",
            full_name="Оператор Таблицы",
            role="manager",
            is_active=True,
        )
        s.add(operator)
        account = AvitoAccount(
            title="LP-Разбор",
            avito_user_id=770100,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="s",
        )
        s.add(account)
        await s.flush()

        made: dict[str, uuid.UUID] = {}
        # (ключ, имя клиента, через сколько ответил оператор, текст клиента)
        #   None  — не ответил вовсе
        #   -600  — ответил ДО того, как клиент написал (дожал старое обращение)
        rows = (
            ("fast", "Быстрый", 30, "Здравствуйте, нужен ремонт холодильника"),
            ("slow", "Медленный", 40 * 60, "Сломалась стиральная машина"),
            ("never", "Не ответили", None, "Добрый день"),
            ("backwards", "Оператор первым", -600, "Спасибо"),
        )
        for key, name, answer_after, body in rows:
            client = Client(channel="avito", external_id=f"pgt-{key}", name=name)
            s.add(client)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"pgt-chat-{key}",
                account_id=account.id,
                client_id=client.id,
                status="in_progress",
                assignee_id=operator.id,
                last_message_at=T0,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body=body,
                    delivery_status="delivered",
                    created_at=T0,
                )
            )
            if answer_after is not None:
                s.add(
                    Message(
                        conversation_id=conv.id,
                        direction="out",
                        sender_type="operator",
                        sender_user_id=operator.id,
                        body="Приедем сегодня",
                        delivery_status="delivered",
                        created_at=T0 + timedelta(seconds=answer_after),
                    )
                )
            # Заметка есть у всех — внутренний текст, в поиск не входит.
            s.add(
                Message(
                    conversation_id=conv.id,
                    direction="note",
                    sender_type="user",
                    sender_user_id=operator.id,
                    body="Внутренняя пометка про скидку",
                    delivery_status="delivered",
                    created_at=T0 + timedelta(minutes=5),
                )
            )
            made[key] = conv.id
        await s.commit()
        return made


# ------------------------------------------------- сортировка по «первому ответу»


@pytest.mark.parametrize(
    ("direction", "head"),
    [("desc", ["Медленный", "Быстрый"]), ("asc", ["Быстрый", "Медленный"])],
)
async def test_the_metric_sort_orders_the_same_way_on_postgres(sessionmaker, seed, direction, head):
    """`extract(epoch ...)` даёт тот же порядок, что `julianday` на юнитах.

    А «величины нет» уходит в хвост в ОБЕ стороны: отправь мы NULL в начало
    при возрастании — верх отчёта «самые быстрые ответы» заняли бы диалоги,
    где не ответили вовсе, и отчёт хвалил бы ровно за то, за что должен ругать.
    """
    async with sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction=direction
        )

    names = [r["client_name"] for r in page["items"]]
    assert names[:2] == head
    assert set(names[2:]) == {"Не ответили", "Оператор первым"}
    assert all(r["first_response_sec"] is None for r in page["items"][2:])


async def test_the_metric_values_match_the_order(sessionmaker, seed):
    """Столбец и порядок считаются одним определением — сверяем числа.

    Разъезд здесь не виден вовсе: строки же как-то расположены. Заметили бы
    его не раньше, чем кто-нибудь начал сверять глазами.
    """
    async with sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    by_name = {r["client_name"]: r["first_response_sec"] for r in page["items"]}
    assert by_name["Медленный"] == 40 * 60
    assert by_name["Быстрый"] == 30


async def test_our_message_before_the_question_does_not_erase_the_answer_on_postgres(
    pg_engine, sessionmaker, seed
):
    """ПУСТОЙ СТОЛБЕЦ «ПЕРВЫЙ ОТВЕТ» НА БОЮ — та же проверка, но на PostgreSQL.

    Юнит идёт на SQLite и про прод не говорит ничего: условие «ответ не раньше
    вопроса» сравнивает метку времени с коррелированным подзапросом внутри
    другого коррелированного подзапроса, а это ровно то место, где диалекты
    расходятся. Цена ошибки прежняя: столбец, ради которого экран и заводили,
    молча показывает прочерк, и проверить его нечем.

    Переписка заведена ОТДЕЛЬНАЯ и закрытая: набор `seed` целиком проверяется
    соседними тестами по именам и по счёту, и пятая строка в нём сломала бы их
    не по делу.
    """
    await ensure_partitions(pg_engine)
    async with sessionmaker() as s:
        operator = (
            await s.execute(text("SELECT id FROM users WHERE email = 'frt@table.test'"))
        ).scalar_one()
        account = (await s.execute(text("SELECT id FROM avito_accounts LIMIT 1"))).scalar_one()
        client = Client(channel="avito", external_id="pgt-before", name="Мы писали первыми")
        s.add(client)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="pgt-chat-before",
            account_id=account,
            client_id=client.id,
            status="closed",
            assignee_id=operator,
            last_message_at=T0,
        )
        s.add(conv)
        await s.flush()
        s.add_all(
            [
                # Наше сообщение ДО первого вопроса — так выглядит история из
                # Авито, где первым писал продавец.
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator,
                    body="Напоминаем про заявку",
                    delivery_status="delivered",
                    created_at=T0 - timedelta(hours=3),
                ),
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте, а сколько будет стоить?",
                    delivery_status="delivered",
                    created_at=T0,
                ),
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator,
                    body="Три тысячи с выездом",
                    delivery_status="delivered",
                    created_at=T0 + timedelta(seconds=5 * 60),
                ),
            ]
        )
        await s.commit()

    async with sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(status="closed"), sort="first_response_sec", direction="desc"
        )

    assert [r["client_name"] for r in page["items"]] == ["Мы писали первыми"]
    assert page["items"][0]["first_response_sec"] == 5 * 60


async def test_the_export_repeats_the_metric_order(sessionmaker, seed):
    """Файл и экран обязаны совпадать, в том числе порядком строк."""
    async with sessionmaker() as s:
        csv_text = await tbl.export_csv(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    names = [line.split(";")[0] for line in csv_text.strip().split("\r\n")[1:]]
    assert names[:2] == ["Медленный", "Быстрый"]


async def test_a_selection_too_wide_for_the_metric_sort_is_refused(sessionmaker, seed, monkeypatch):
    """Потолок дорогой сортировки работает и на настоящей базе."""
    monkeypatch.setattr(tbl, "METRIC_SORT_MAX_ROWS", 3)
    async with sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await tbl.query_table(s, tbl.TableFilters(), sort="first_response_sec")
    assert err.value.status == 400
    assert err.value.details["total"] == 4


# ------------------------------------ сортировка по «первому ответу» с витрины


async def _refresh_view(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("REFRESH MATERIALIZED VIEW mv_conversation_stats"))
    return datetime.now(UTC)


async def test_the_view_sort_orders_like_the_live_one(sessionmaker, seed, pg_engine):
    """Ключ сортировки из витрины даёт тот же порядок, что живой счёт (24.09).

    Живой счёт по всей выборке стоил на бою 16 с на «30 днях», поэтому потолок
    в 20 000 строк отказывал на умолчательном периоде.
    """
    refreshed = await _refresh_view(pg_engine)
    async with sessionmaker() as s:
        page = await tbl.query_table(
            s,
            tbl.TableFilters(),
            sort="first_response_sec",
            direction="desc",
            mv_refreshed_at=refreshed,
        )
    names = [r["client_name"] for r in page["items"]]
    assert names[:2] == ["Медленный", "Быстрый"]
    assert set(names[2:]) == {"Не ответили", "Оператор первым"}


async def test_a_dialog_answered_after_the_refresh_is_counted_live(sessionmaker, seed, pg_engine):
    """Витрина не знает об ответе, пришедшем после пересчёта, — считаем живьём.

    «Не ответили» получает ответ через 20 минут уже после пересчёта: в витрине
    у него пусто, но место ему — между медленным (40 мин) и быстрым (30 с).
    """
    refreshed = await _refresh_view(pg_engine)
    answered_at = refreshed + timedelta(minutes=1)
    async with sessionmaker() as s:
        conv = await s.get(Conversation, seed["never"])
        assert conv is not None
        conv.last_message_at = answered_at
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="Ответили позже",
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=20),
            )
        )
        await s.commit()

    async with sessionmaker() as s:
        page = await tbl.query_table(
            s,
            tbl.TableFilters(),
            sort="first_response_sec",
            direction="desc",
            mv_refreshed_at=refreshed,
        )
    names = [r["client_name"] for r in page["items"]]
    assert names[:3] == ["Медленный", "Не ответили", "Быстрый"]


async def test_the_table_query_runs_without_jit(sessionmaker, seed):
    """Компиляция JIT стоила таблице 11–12 с на запрос при четверти секунды работы."""
    async with sessionmaker() as s:
        await tbl.query_table(s, tbl.TableFilters(), sort="first_response_sec")
        assert (await s.execute(text("SHOW jit"))).scalar_one() == "off"
    async with sessionmaker() as s:
        assert (await s.execute(text("SHOW jit"))).scalar_one() == "on", "SET LOCAL протёк"


async def test_a_fresh_view_lifts_the_metric_sort_ceiling(
    sessionmaker, seed, pg_engine, monkeypatch
):
    monkeypatch.setattr(tbl, "METRIC_SORT_MAX_ROWS", 3)
    refreshed = await _refresh_view(pg_engine)
    async with sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", mv_refreshed_at=refreshed
        )
    assert page["page"]["total"] == 4


# ------------------------------------------------------------------------- поиск


async def test_the_search_goes_through_the_fulltext_index(sessionmaker, seed):
    """На PostgreSQL поиск идёт по ЛЕММАМ, а не по подстроке.

    Юнит на SQLite вырождается в LIKE и такого не показал бы: «холодильник» в
    именительном падеже не является подстрокой «холодильника».
    """
    async with sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(q="холодильник"))

    assert [r["client_name"] for r in page["items"]] == ["Быстрый"]
    assert page["page"]["total"] == 1, "счётчик считается тем же условием, что и строки"


async def test_the_search_finds_by_client_name_too(sessionmaker, seed):
    """Имя клиента живёт в другой таблице — выборка присоединяет её.

    Забыть соединение значило бы получить счётчик и страницу по разным
    множествам: «Найдено: 12» над тремя строками.
    """
    async with sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(q="Медленный"))

    assert [r["client_name"] for r in page["items"]] == ["Медленный"]
    assert page["page"]["total"] == 1


async def test_the_search_ignores_internal_notes(sessionmaker, seed):
    """По заметкам не ищем: это внутренний текст.

    Условие взято из списка диалогов целиком именно ради этого — своя копия
    однажды забыла бы про заметки, и observer нашёл бы диалог по тексту,
    которого ему видеть нельзя.
    """
    async with sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(q="пометка про скидку"))

    assert page["items"] == []
    assert page["page"]["total"] == 0


async def test_the_search_narrows_the_export_as_well(sessionmaker, seed):
    async with sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters(q="холодильник"))

    lines = csv_text.strip().split("\r\n")
    assert len(lines) == 2, "заголовок и одна строка"
    assert lines[1].startswith("Быстрый;")
