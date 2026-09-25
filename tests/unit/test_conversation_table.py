"""Таблица диалогов с метриками (план 7.3).

Проверяется не «отдаются ли строки» — это просто. Проверяется то, на чём
таблица над большой историей ломается (у Jivo того же заказчика — сотни тысяч
диалогов): что метрики считаются только для
видимой страницы, что сортировать можно лишь дешёвыми колонками, и что
«ответа ещё не было» не превращается в «ответили мгновенно».
"""

import pathlib
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app import cli
from app.core.errors import ApiError
from app.models import Client, Conversation, Message, User
from app.services import conversation_table as tbl

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)


@pytest.fixture
async def dataset(db_sessionmaker, make_avito_account, make_user):
    """Три диалога: с ответом, без ответа и закрытый в другом периоде."""
    account = await make_avito_account()
    operator = await make_user(
        "tbl@leadchat.test",
        role="manager",
        full_name="Оператор Таблицы",
        # Два пробела — как на бою: поле свободное, и рядом лежат
        # «Диспетчер МНЧ» и «Диспетчер  МНЧ».
        department="Диспетчер  МНЧ",
    )

    async with db_sessionmaker() as s:
        made = {}
        for key, name, status, minutes_ago, answered in (
            ("answered", "Отвеченный", "in_progress", 30, True),
            ("waiting", "Без ответа", "new", 10, False),
            ("old", "Старый", "closed", 60 * 24 * 40, True),
        ):
            cl = Client(channel="avito", external_id=f"tbl-{key}", name=name)
            s.add(cl)
            await s.flush()
            at = T0 - timedelta(minutes=minutes_ago)
            conv = Conversation(
                channel="avito",
                external_chat_id=f"tbl-chat-{key}",
                account_id=account.id,
                client_id=cl.id,
                status=status,
                assignee_id=operator.id,
                item_title="Ремонт холодильника",
                unread_count=1 if not answered else 0,
                last_message_at=at,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте",
                    delivery_status="delivered",
                    created_at=at,
                )
            )
            if answered:
                # Ответ через 4 минуты — это и есть время первого ответа.
                s.add(
                    Message(
                        conversation_id=conv.id,
                        direction="out",
                        sender_type="operator",
                        sender_user_id=operator.id,
                        body="Добрый день!",
                        delivery_status="delivered",
                        created_at=at + timedelta(minutes=4),
                    )
                )
            # Внутренняя заметка есть у всех — она НЕ должна попадать в счётчик.
            s.add(
                Message(
                    conversation_id=conv.id,
                    direction="note",
                    sender_type="user",
                    sender_user_id=operator.id,
                    body="Постоянный клиент",
                    delivery_status="delivered",
                    created_at=at + timedelta(minutes=5),
                )
            )
            made[key] = conv.id
        await s.commit()
        return {"ids": made, "account": account, "operator": operator}


async def test_the_table_returns_metrics_per_row(db_sessionmaker, dataset):
    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters())

    by_name = {r["client_name"]: r for r in page["items"]}
    assert page["page"]["total"] == 3

    answered = by_name["Отвеченный"]
    assert answered["first_response_sec"] == 4 * 60, "четыре минуты до первого ответа"
    assert answered["messages_count"] == 2, "заметка сообщением диалога не считается"
    assert answered["assignee_name"] == "Оператор Таблицы"
    # Рядом с именем — идентификатор: фильтр «Оператор» на экране добирает из
    # строк тех, кого нет в справочнике «кому можно передать» (уволенные, роль
    # head, снятые с диалогов). Класть их в фильтр по имени нельзя — два
    # однофамильца слились бы в одну опцию, и фильтр показывал бы чужие строки.
    assert answered["assignee_id"] == str(dataset["operator"].id)
    # Отдел — подписью «(Диспетчер МНЧ)» рядом с именем в колонке «Оператор»
    # (просьба владельца 04.09). Строк здесь сотня, имена похожие, и без
    # отдела колонка не отвечает на вопрос «чей это диалог». Два пробела из
    # боевого поля схлопнуты — иначе один отдел выглядел бы как два.
    assert answered["assignee_department"] == "Диспетчер МНЧ"


async def test_a_dialog_without_an_operator_has_no_id_either(db_sessionmaker, dataset):
    """Ничей диалог отдаёт `None`, а не строку «None».

    Строка «None» доехала бы до фильтра как настоящий идентификатор и завела
    бы в выпадающем списке опцию, по которой не находится ничего.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, dataset["ids"]["waiting"])
        assert conv is not None
        conv.assignee_id = None
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(status="new"))

    row = page["items"][0]
    assert row["assignee_id"] is None
    assert row["assignee_name"] is None
    # И отдела у ничейного диалога тоже нет: пустая строка дала бы в колонке
    # «()» — читается как сломанный экран.
    assert row["assignee_department"] is None


async def test_no_answer_is_null_and_not_zero(db_sessionmaker, dataset):
    """«Ещё не ответили» — это `None`, а не ноль.

    Ноль читался бы как «ответили мгновенно», то есть отчёт показывал бы
    идеальную скорость ровно там, где клиента не обслужили вовсе.
    """
    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters())

    waiting = next(r for r in page["items"] if r["client_name"] == "Без ответа")
    assert waiting["first_response_sec"] is None
    assert waiting["messages_count"] == 1


async def test_our_message_before_the_question_does_not_erase_the_answer(
    db_sessionmaker, dataset, make_user
):
    """ЭТО И БЫЛ ПУСТОЙ СТОЛБЕЦ «ПЕРВЫЙ ОТВЕТ» НА БОЮ (12 августа).

    Переписка, где НАШЕ сообщение стоит раньше первого вопроса клиента, —
    обычное дело: так выглядит вся история, загруженная из Авито, где первым
    писал продавец. Раньше таблица брала самое раннее исходящее оператора
    вообще, разность выходила отрицательной, `_seconds_between` превращала её
    в `None`, и в столбце стоял прочерк — при том, что на вопрос клиента
    ответили через четыре минуты, и «Статистика» эти четыре минуты считала.

    Проверяем ЧИСЛО, а не «не прочерк»: величина обязана мериться от вопроса
    клиента до ближайшего ответа ПОСЛЕ него, а не от чего-нибудь ещё, что тоже
    даёт непустую ячейку.
    """
    operator = await make_user("before@leadchat.test", role="manager", full_name="Ранний")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, dataset["ids"]["waiting"])
        assert conv is not None
        client_asked_at = await s.scalar(
            sa.select(sa.func.min(Message.created_at)).where(
                Message.conversation_id == conv.id, Message.direction == "in"
            )
        )
        assert client_asked_at is not None
        if isinstance(client_asked_at, str):  # SQLite отдаёт даты строками
            client_asked_at = datetime.fromisoformat(client_asked_at)
        s.add_all(
            [
                # Наше сообщение ДО вопроса — «дожали» прошлое обращение.
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Напоминаем про заявку",
                    delivery_status="delivered",
                    created_at=client_asked_at - timedelta(hours=3),
                ),
                # И настоящий ответ — через четыре минуты после вопроса.
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Добрый день! Мастер приедет сегодня",
                    delivery_status="delivered",
                    created_at=client_asked_at + timedelta(minutes=4),
                ),
            ]
        )
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(status="new"))

    row = next(r for r in page["items"] if r["client_name"] == "Без ответа")
    assert row["first_response_sec"] == 4 * 60


async def test_the_countdown_starts_from_the_client_and_not_from_avito(
    db_sessionmaker, dataset, make_user
):
    """Отсчёт начинает ВОПРОС КЛИЕНТА, а не любая входящая запись.

    Служебные строки самого Авито («Клиент оформил заказ…») лежали в базе как
    `direction='in'` до миграции 0026 — она перевела их в `system`, но у
    старых баз и у будущих интеграций гарантии нет никакой. Взять такую
    строку за начало отсчёта значит показать время ответа на уведомление
    площадки вместо времени ответа человеку — то есть аккуратное, правдоподобно
    выглядящее враньё.

    Условие `sender_type = 'client'` — то же, что в статистике (`stats.py`) и в
    витрине (миграция 0004): у одной величины должно быть одно определение.
    """
    operator = await make_user("avito@leadchat.test", role="manager", full_name="Ответчик")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, dataset["ids"]["waiting"])
        assert conv is not None
        client_asked_at = await s.scalar(
            sa.select(sa.func.min(Message.created_at)).where(
                Message.conversation_id == conv.id, Message.sender_type == "client"
            )
        )
        assert client_asked_at is not None
        if isinstance(client_asked_at, str):
            client_asked_at = datetime.fromisoformat(client_asked_at)
        s.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="avito",
                    body="Клиент оформил заказ, ожидает подтверждения",
                    delivery_status="delivered",
                    created_at=client_asked_at - timedelta(hours=2),
                ),
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Добрый день!",
                    delivery_status="delivered",
                    created_at=client_asked_at + timedelta(minutes=7),
                ),
            ]
        )
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(status="new"))

    row = next(r for r in page["items"] if r["client_name"] == "Без ответа")
    assert row["first_response_sec"] == 7 * 60, "семь минут от вопроса, а не два часа от Авито"


async def test_the_metric_sort_sees_the_same_answer_as_the_column(
    db_sessionmaker, dataset, make_user
):
    """Столбец и порядок строк считаются ОДНИМ определением — сверяем вместе.

    Разъезд здесь не виден вовсе: строки же как-то расположены. Поэтому мало
    починить показ — надо убедиться, что сортировка по «Первому ответу»
    поставила эту строку по её НАСТОЯЩЕЙ величине, а не в хвост «без ответа».
    """
    operator = await make_user("sortsame@leadchat.test", role="manager", full_name="Тот же")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, dataset["ids"]["waiting"])
        assert conv is not None
        s.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Напоминаем про заявку",
                    delivery_status="delivered",
                    created_at=T0 - timedelta(hours=5),
                ),
                Message(
                    conversation_id=conv.id,
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Добрый день!",
                    delivery_status="delivered",
                    # Диалог «waiting» заведён за 10 минут до T0; отвечаем
                    # через час после вопроса — дольше всех в наборе.
                    created_at=T0 - timedelta(minutes=10) + timedelta(hours=1),
                ),
            ]
        )
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    rows = page["items"]
    assert rows[0]["client_name"] == "Без ответа", "самый долгий ответ — первой строкой"
    assert rows[0]["first_response_sec"] == 60 * 60


async def test_filters_narrow_the_selection(db_sessionmaker, dataset):
    async with db_sessionmaker() as s:
        only_new = await tbl.query_table(s, tbl.TableFilters(status="new"))
        assert [r["client_name"] for r in only_new["items"]] == ["Без ответа"]

        recent = await tbl.query_table(s, tbl.TableFilters(date_from=T0 - timedelta(hours=2)))
        names = {r["client_name"] for r in recent["items"]}
        assert names == {"Отвеченный", "Без ответа"}, "старый диалог вне периода"
        assert recent["page"]["total"] == 2, "общее число тоже считается с фильтром"


async def test_the_search_actually_searches(db_sessionmaker, dataset):
    """`q=` не должен молча возвращать всё.

    Параметр объявлен в фильтрах и принимается обеими ручками таблицы, а в
    условия не попадал вовсе: запрос с `q=Иванов` отдавал полную выборку,
    сделав вид, что поиск применён. Экран им пока не пользуется — но принятый
    и выброшенный фильтр хуже отсутствующего: он врёт первому же, кто до него
    дойдёт, и врёт правдоподобно.
    """
    async with db_sessionmaker() as s:
        found = await tbl.query_table(s, tbl.TableFilters(q="Отвеченный"))
        assert [r["client_name"] for r in found["items"]] == ["Отвеченный"]
        # Общее число обязано считаться с тем же условием: иначе пейджер
        # обещает страницы, которых нет.
        assert found["page"]["total"] == 1

        nothing = await tbl.query_table(s, tbl.TableFilters(q="таких-нет"))
        assert nothing["items"] == []
        assert nothing["page"]["total"] == 0


async def test_the_search_reaches_the_text_of_messages(db_sessionmaker, dataset):
    """Ищется и по переписке, а не только по имени клиента.

    Условие взято из списка диалогов целиком (`convs._search_condition`), и
    это главное в правке: там уже решено, что внутренние заметки в поиск не
    входят. Своя копия однажды забыла бы про это, и observer нашёл бы диалог
    по тексту, которого ему видеть нельзя.
    """
    async with db_sessionmaker() as s:
        by_body = await tbl.query_table(s, tbl.TableFilters(q="Добрый день"))
        assert {r["client_name"] for r in by_body["items"]} == {"Отвеченный", "Старый"}

        # «Постоянный клиент» — текст ЗАМЕТКИ, она есть у всех трёх диалогов.
        by_note = await tbl.query_table(s, tbl.TableFilters(q="Постоянный клиент"))
        assert by_note["items"] == [], "по заметкам не ищем — это внутренний текст"


async def test_the_search_and_the_export_agree(db_sessionmaker, dataset):
    """Выгрузка обязана повторять экран — в том числе сужение поиском."""
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters(q="Отвеченный"))

    lines = csv_text.strip().split("\r\n")
    assert len(lines) == 2, "заголовок и одна строка"
    assert lines[1].startswith("Отвеченный;")


async def test_sorting_by_a_cheap_column(db_sessionmaker, dataset):
    async with db_sessionmaker() as s:
        ok = await tbl.query_table(s, tbl.TableFilters(), sort="unread_count", direction="asc")
        # Утверждаем только детерминированное: единственный диалог с
        # непрочитанным уходит в конец. Порядок двух нулей между собой решает
        # идентификатор, и закреплять его тестом значило бы закрепить
        # случайность.
        assert [r["client_name"] for r in ok["items"]][-1] == "Без ответа"
        assert [r["unread_count"] for r in ok["items"]] == [0, 0, 1]


async def test_an_unknown_column_is_still_refused(db_sessionmaker, dataset):
    """Список сортируемых колонок закрытый — иначе первая же опечатка в адресе
    превратила бы отчёт в полный перебор по колонке без индекса."""
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await tbl.query_table(s, tbl.TableFilters(), sort="client_name")
        assert err.value.status == 400


@pytest.mark.parametrize(
    ("method", "path", "accepted"),
    [
        ("GET", "/api/v1/conversations/table", 200),
        ("POST", "/api/v1/conversations/table/export", 202),
    ],
)
async def test_an_unknown_status_is_refused_and_not_answered_with_an_empty_page(
    client, tokens, dataset, method, path, accepted
):
    """Неизвестный статус — отказ, а не пустая страница.

    ⚠ ЗАМЕР БОЯ 14 АВГУСТА: `?status=zzz` отвечал 200 и пустым списком. Значение
    уходило в `WHERE status = 'zzz'`, не совпадало ни с чем — и человек получал
    единственно возможный вывод: «диалогов нет». Пустая таблица выглядит как
    факт о деле, а была фактом об опечатке в адресе.

    Тем обиднее, что рядом, в этой же выборке, неизвестное имя КОЛОНКИ
    отбивается словами (тест выше). Одна ошибка называлась вслух, вторая
    проглатывалась молча — при том что вторая приходит из адресной строки
    ровно так же часто.

    Выгрузка проверяется наравне с экраном: расходись они, отчёт на экране и
    файл, отправленный владельцу, отвечали бы на разные вопросы.
    """
    r = await client.request(
        method,
        path,
        params={"status": "zzz"},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"

    # А знакомый статус проходит — сторож не должен закрыть ручку целиком.
    ok = await client.request(
        method,
        path,
        params={"status": "new"},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert ok.status_code == accepted, ok.text


# --- сортировка по «Первому ответу» ----------------------------------------
#
# Экран обещает в собственном описании ответить на вопрос «где первый ответ был
# дольше пятнадцати минут». Пока сортировки по этой колонке не было, ответить
# он на него не мог: оставалось листать страницы и искать красное глазами.


@pytest.fixture
async def response_times(db_sessionmaker, make_avito_account, make_user):
    """Четыре диалога с РАЗНЫМ временем первого ответа, включая два крайних.

    «Не ответили» и «первым написал оператор» — не медленный и не быстрый
    ответ, а отсутствие величины. Обе строки обязаны вести себя одинаково.
    """
    account = await make_avito_account()
    operator = await make_user("frt@leadchat.test", role="manager", full_name="Оператор")

    async with db_sessionmaker() as s:
        made = {}
        # (ключ, имя, через сколько секунд ответил оператор)
        #   None — не ответил вовсе
        #   -600 — ответил ДО того, как клиент написал (дожал старое обращение)
        for key, name, answer_after in (
            ("fast", "Быстрый", 30),
            ("slow", "Медленный", 40 * 60),
            ("never", "Не ответили", None),
            ("backwards", "Оператор первым", -600),
        ):
            cl = Client(channel="avito", external_id=f"frt-{key}", name=name)
            s.add(cl)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"frt-chat-{key}",
                account_id=account.id,
                client_id=cl.id,
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
                    body="Здравствуйте",
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
                        body="Добрый день!",
                        delivery_status="delivered",
                        created_at=T0 + timedelta(seconds=answer_after),
                    )
                )
            made[key] = conv.id
        await s.commit()
        return made


async def test_sorting_by_first_response_answers_the_screens_own_question(
    db_sessionmaker, response_times
):
    """«Где первый ответ был дольше пятнадцати минут» — по убыванию, сверху."""
    async with db_sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    names = [r["client_name"] for r in page["items"]]
    assert names[0] == "Медленный", "самый долгий ответ — первой строкой"
    assert names[1] == "Быстрый"


async def test_ascending_puts_the_quickest_first(db_sessionmaker, response_times):
    async with db_sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction="asc"
        )

    names = [r["client_name"] for r in page["items"]]
    assert names[0] == "Быстрый"
    assert names[1] == "Медленный"


@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_missing_first_response_never_takes_a_place_in_the_ranking(
    db_sessionmaker, response_times, direction
):
    """«Ответа не было» уходит в конец в ОБЕ стороны, а не только в одну.

    Отсутствие величины — не ноль и не бесконечность, места в ряду
    длительностей у него нет. Отправь мы такие строки в начало при
    возрастании — верх отчёта «самые быстрые ответы» заняли бы диалоги, где не
    ответили вовсе, и отчёт хвалил бы ровно за то, за что должен ругать.

    «Оператор первым» здесь же, но причина у него ДРУГАЯ, и путать их нельзя:
    в этом наборе он написал ОДИН раз и до вопроса клиента, то есть ответа
    после вопроса не было вовсе. Прочерк тут честен. А вот переписка, где
    наше сообщение стоит раньше вопроса, но потом мы ответили, обязана
    показывать число — это отдельная проверка,
    :func:`test_our_message_before_the_question_does_not_erase_the_answer`.
    """
    async with db_sessionmaker() as s:
        page = await tbl.query_table(
            s, tbl.TableFilters(), sort="first_response_sec", direction=direction
        )

    rows = page["items"]
    assert [r["client_name"] for r in rows[:2]] == (
        ["Медленный", "Быстрый"] if direction == "desc" else ["Быстрый", "Медленный"]
    )
    # Хвост — обе строки без величины, в любом порядке между собой.
    assert {r["client_name"] for r in rows[2:]} == {"Не ответили", "Оператор первым"}
    assert all(r["first_response_sec"] is None for r in rows[2:])


async def test_the_metric_sort_refuses_a_selection_it_cannot_afford(
    db_sessionmaker, response_times, monkeypatch
):
    """Слишком широкая выборка — отказ словами, а не полминуты ожидания.

    Порядок по метрике считается подзапросами по `messages` на КАЖДУЮ строку
    выборки, а не на пятьдесят видимых. Тот же приём, что у глубины пагинации
    и потолка выгрузки: сказать «сузьте фильтр» честнее, чем молча тормозить.
    """
    monkeypatch.setattr(tbl, "METRIC_SORT_MAX_ROWS", 3)
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await tbl.query_table(s, tbl.TableFilters(), sort="first_response_sec")
    assert err.value.status == 400
    assert err.value.details["total"] == 4
    assert err.value.details["max_rows"] == 3
    # ⚠ ИМЯ ВИНОВНОЙ КОЛОНКИ — ЧАСТЬ ДОГОВОРА, А НЕ УКРАШЕНИЕ (правка 08.09).
    # Экран отказа отличает ЭТОТ отказ от прочих четырёхсотых ровно по ключу
    # `sort` (frontend TablePage.tsx, `отказПоСортировкеЛи`) и по нему решает,
    # предложить «Сбросить сортировку» или «Повторить». Убери ключ отсюда —
    # и кнопка молча превратится обратно в «Повторить», то есть в тупик:
    # повтор пришлёт тот же отказ. Ни один сторож этого не ловил до сегодня.
    assert err.value.details["sort"] == "first_response_sec"

    # Дешёвая сортировка тем же потолком не ограничена — ей он не нужен.
    async with db_sessionmaker() as s:
        assert (await tbl.query_table(s, tbl.TableFilters(), sort="status"))["page"]["total"] == 4


async def test_the_export_repeats_the_metric_order_too(db_sessionmaker, response_times):
    """Выгрузка обязана повторять экран — в том числе его ПОРЯДОК СТРОК.

    Файл, отсортированный иначе, чем экран, — это тот же разлад «экран врёт»,
    ради которого фильтры и сортировка собираются одной функцией.
    """
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    names = [line.split(";")[0] for line in csv_text.strip().split("\r\n")[1:]]
    assert names[:2] == ["Медленный", "Быстрый"]
    assert set(names[2:]) == {"Не ответили", "Оператор первым"}


async def test_the_export_orders_once_no_matter_how_many_chunks(
    db_sessionmaker, response_times, monkeypatch
):
    """Порядок выбирается один раз на всю выгрузку, а не заново на порцию.

    Раньше каждая порция звала `_page`, а тот заново считал COUNT и заново
    сортировал всю выборку ради сотни строк. С сортировкой по метрике это
    означало бы сто проходов подзапросами по `messages` — десятки секунд на
    файле, который экран показывает мгновенно.
    """
    monkeypatch.setattr(tbl, "EXPORT_CHUNK", 1)
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(
            s, tbl.TableFilters(), sort="first_response_sec", direction="desc"
        )

    lines = csv_text.strip().split("\r\n")
    assert len(lines) == 1 + 4, "порции не должны терять строки"
    assert [line.split(";")[0] for line in lines[1:]][:2] == ["Медленный", "Быстрый"]


async def test_paging_too_deep_is_refused_rather_than_slow(db_sessionmaker, dataset):
    """Глубокая страница — отказ с просьбой уточнить фильтр, а не молчаливое
    торможение: OFFSET на четырёхсоттысячной строке честно пролистывает всё
    пропущенное, и «отчёт висит» выглядит как поломка."""
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await tbl.query_table(s, tbl.TableFilters(), offset=tbl.MAX_OFFSET + 1)
        assert err.value.status == 400
        assert err.value.details["max_offset"] == tbl.MAX_OFFSET


async def test_the_export_repeats_the_screen_exactly(db_sessionmaker, dataset):
    """Выгрузка обязана отдавать РОВНО то, что человек видит на экране.

    Расхождение заметят не сразу и решат, что врёт экран, — поэтому фильтр и
    сортировка тут те же, а метрики считает тот же код.
    """
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters(status="new"))

    lines = csv_text.strip().split("\r\n")
    assert lines[0].split(";") == list(tbl.CSV_HEADER)
    assert len(lines) == 2, "фильтр status=new оставляет один диалог"
    cells = lines[1].split(";")
    assert cells[0] == "Без ответа"
    assert cells[3] == "Новый", "статус по-русски: файл читают глазами"
    # Пустая клетка, а не ноль: ноль в Excel посчитался бы как «ответили
    # мгновенно» и занизил бы среднее по столбцу.
    assert cells[8] == ""


async def test_the_export_prints_moscow_time(db_sessionmaker, dataset):
    """Время в файле — московское, как вся отчётность (06 §0.1).

    В базе оно лежит в UTC, и напечатанное как есть отличалось бы от экрана на
    несколько часов. Руководитель, у которого файл и экран показывают разное
    время одного диалога, перестаёт верить обоим — а разобраться, кто прав, ему
    нечем.
    """
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters(status="new"))

    # Диалог «Без ответа» — за 10 минут до T0 (10:00 UTC), то есть 09:50 UTC.
    # По Москве это 12:50.
    assert csv_text.strip().split("\r\n")[1].endswith("2026-08-06 12:50")


async def test_the_export_walks_past_one_chunk(db_sessionmaker, dataset, monkeypatch):
    """Обход порциями не должен терять хвост.

    Порция привязана к потолку страницы: собственное число однажды разъехалось
    бы с ним, и выгрузка молча роняла бы часть каждой порции — самая тихая из
    возможных поломок отчёта.
    """
    monkeypatch.setattr(tbl, "EXPORT_CHUNK", 1)
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters())

    assert len(csv_text.strip().split("\r\n")) == 1 + 3, "три диалога и шапка"


async def test_the_export_refuses_instead_of_hanging(db_sessionmaker, dataset, monkeypatch):
    monkeypatch.setattr(tbl, "EXPORT_MAX_ROWS", 2)
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await tbl.export_csv(s, tbl.TableFilters())
    assert err.value.status == 400
    assert err.value.details["total"] == 3


async def test_channel_and_duration_are_in_the_row(db_sessionmaker, dataset):
    """Канал и длительность переписки — колонки из плана 7.3."""
    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters())

    row = next(r for r in page["items"] if r["client_name"] == "Отвеченный")
    assert row["account_title"], "канал показывается названием, а не uuid"
    # От первого сообщения до последнего. Заметка через 5 минут в счёт не идёт:
    # длительность — про переписку с клиентом, а не про работу с карточкой.
    assert row["duration_sec"] == 4 * 60


async def test_tag_filter_matches_the_whole_tag(db_sessionmaker, dataset):
    """Метка ищется целиком: «срочно» не должно вытаскивать «несрочно»."""
    ids = dataset["ids"]
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == ids["answered"])
            .values(tags=["срочно"])
        )
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == ids["waiting"])
            .values(tags=["несрочно"])
        )
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(tag="срочно"))

    assert [r["client_name"] for r in page["items"]] == ["Отвеченный"]
    assert page["page"]["total"] == 1


def test_frontend_knows_the_same_sortable_columns():
    """Список сортируемых колонок продублирован во фронте — сторожим разъезд.

    Интерфейс обязан знать, у каких заголовков рисовать стрелку: иначе человек
    кликает по «Первому ответу» и получает ошибку вместо сортировки. Дубль
    неизбежен (запрашивать список ради отрисовки шапки — лишний раунд-трип), но
    молчаливый он опаснее самого дубля: добавили колонку на сервере, забыли во
    фронте — и половина таблицы перестаёт сортироваться без единой жалобы.
    """
    ts = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend/src/features/table/api.ts"
    ).read_text(encoding="utf-8")
    m = re.search(r"export const SORTABLE = new Set\(\[(.*?)\]\)", ts, re.S)
    assert m, "во фронте пропал экспорт SORTABLE"
    front = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert front == set(tbl.SORTABLE) | set(tbl.METRIC_SORTABLE), (
        "список сортируемых колонок разъехался"
    )


async def test_an_empty_page_costs_nothing(db_sessionmaker, dataset):
    """Пустая страница не считает метрики вовсе — незачем."""
    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters(assignee_id=uuid.uuid4()))
    assert page["items"] == []
    assert page["page"]["total"] == 0


# ------------------------------------------------------- служебные записи ---


@pytest.fixture
async def smoke_row(db_sessionmaker, dataset):
    """Ровно то, что заводит `app/cli.py seed-smoke` на каждом деплое (07 §6).

    Собирается вызовом самой команды, а не руками: копия seed'а в тесте — это
    второй источник правды, который однажды разойдётся с первым, и тогда тест
    будет охранять несуществующие данные.
    """
    async with db_sessionmaker() as s:
        account, _ = await cli._seed_smoke_account(s, None)
        conv, _ = await cli._seed_smoke_conversation(s, account)
        s.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body="проверка связи",
                delivery_status="delivered",
                created_at=T0,
            )
        )
        await s.commit()
        return conv.id


async def test_the_report_does_not_count_the_smoke_dialog(db_sessionmaker, smoke_row):
    """Служебный диалог не строка отчёта — ни в списке, ни в «Найдено».

    Было на бою 12 августа: в «Разборе диалогов» стояла строка с клиентом
    «SMOKE (служебный)» и чатом SMOKE-CONV. Руководитель считал по ней как по
    обращению — и вместе с ней по счётчику наверху.
    """
    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters())

    assert page["page"]["total"] == 3, "три боевых диалога, служебного среди них нет"
    assert str(smoke_row) not in {r["id"] for r in page["items"]}
    assert "SMOKE (служебный)" not in {r["client_name"] for r in page["items"]}


async def test_the_export_hides_the_smoke_dialog_too(db_sessionmaker, smoke_row):
    """Выгрузка обязана совпадать с экраном — в том числе тем, чего на нём нет.

    Разойдись они здесь — человек решил бы, что врёт экран, и поверил бы файлу
    со служебной строкой внутри.
    """
    async with db_sessionmaker() as s:
        csv_text = await tbl.export_csv(s, tbl.TableFilters())

    assert "SMOKE" not in csv_text
    assert len(csv_text.strip().split("\r\n")) == 1 + 3, "шапка и три боевых диалога"


async def test_a_dialog_of_a_service_operator_stays_out_of_the_report(
    db_sessionmaker, dataset, make_user
):
    """Диалог, который ведёт робот smoke, в разбивке по операторам не нужен.

    Регрессионный прогон входит в систему пользователем-роботом и работает
    диалогом от его имени. Попади такой диалог в отчёт — руководитель увидел бы
    в колонке «Оператор» сотрудника, которого в команде нет.
    """
    robot = await make_user("smoke-robot@leadpartner.local", role="manager")
    async with db_sessionmaker() as s:
        await s.execute(sa.update(User).where(User.id == robot.id).values(is_service=True))
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == dataset["ids"]["waiting"])
            .values(assignee_id=robot.id)
        )
        await s.commit()

    async with db_sessionmaker() as s:
        page = await tbl.query_table(s, tbl.TableFilters())

    assert page["page"]["total"] == 2
    assert "Без ответа" not in {r["client_name"] for r in page["items"]}
