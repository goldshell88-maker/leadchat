"""GET /conversations, деталь, лента с курсорами, POST /read (01 §5–6).

SQLite-вариант поиска (LIKE) — юнит-уровень; настоящий FTS — INT-9.
Fixed sort проверяется буквально: закреплённые смотрящим, затем
last_message_at DESC, затем id. Подробный разбор порядка и его историю
держит `tests/unit/test_conversations_order.py`.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Client, Conversation, Message
from tests.unit.conftest import drain_events

T0 = datetime(2026, 8, 4, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def dataset(db_sessionmaker, make_avito_account, users_by_role):
    """A(in_progress, мой, прочитан, самый свежий) B(new, негатив, 2 непрочитанных)
    C(new, 1 непрочитанное) D(closed)."""
    account = await make_avito_account()
    manager = users_by_role["manager"]
    async with db_sessionmaker() as s:

        def client_row(external_id, name):
            row = Client(channel="avito", external_id=external_id, name=name)
            s.add(row)
            return row

        cl_a = client_row("1001", "Анна Клиентова")
        cl_b = client_row("1002", "Иван Петров")
        cl_c = client_row("1003", "Пётр Сидоров")
        cl_d = client_row("1004", "Мария Архивная")
        await s.flush()

        def conv_row(client, chat, *, status, unread, tags=(), last_at, assignee_id=None):
            row = Conversation(
                channel="avito",
                external_chat_id=chat,
                account_id=account.id,
                client_id=client.id,
                status=status,
                unread_count=unread,
                tags=list(tags),
                last_message_at=last_at,
                assignee_id=assignee_id,
                item_title="Ремонт iPhone 13",
            )
            s.add(row)
            return row

        conv_a = conv_row(
            cl_a,
            "chat-a",
            status="in_progress",
            unread=0,
            last_at=T0 + timedelta(minutes=30),
            assignee_id=manager.id,
        )
        conv_b = conv_row(
            cl_b,
            "chat-b",
            status="new",
            unread=2,
            tags=["негатив"],
            last_at=T0 + timedelta(minutes=10),
        )
        conv_c = conv_row(
            cl_c, "chat-c", status="new", unread=1, last_at=T0 + timedelta(minutes=20)
        )
        conv_d = conv_row(cl_d, "chat-d", status="closed", unread=0, last_at=T0)
        await s.flush()

        def msg(conv, body, at, direction="in", external_id=None):
            row = Message(
                conversation_id=conv.id,
                external_message_id=external_id or f"am-{uuid.uuid4().hex[:8]}",
                direction=direction,
                sender_type="client" if direction == "in" else "operator",
                body=body,
                attachments=[],
                delivery_status="delivered",
                created_at=at,
            )
            s.add(row)
            return row

        msg(conv_a, "Спасибо, всё получилось", T0 + timedelta(minutes=30))
        msg(conv_b, "Это ужас, а не сервис!", T0 + timedelta(minutes=10))
        msg(conv_c, "Экран разбит, почём замена?", T0 + timedelta(minutes=20))
        msg(conv_d, "Старый диалог", T0)
        await s.commit()
        return SimpleNamespace(
            account=account,
            a=conv_a.id,
            b=conv_b.id,
            c=conv_c.id,
            d=conv_d.id,
            client_b=cl_b.id,
        )


def ids(payload):
    return [item["id"] for item in payload["items"]]


async def test_fixed_sort_is_last_message_desc(client, tokens, dataset):
    """Порядок списка — свежее сверху; строка везёт всё, что рисует карточка.

    Прежде первым ключом стояло непрочитанное, и тест назывался
    `test_fixed_sort_unread_then_negative`. Ключ убран (подробности — в
    докстринге `list_conversations`): сортировали по ГЛОБАЛЬНОЙ колонке
    `conversations.unread_count`, а в ответ уезжает ПЕР-ЮЗЕРНЫЙ счётчик из
    маркеров чтения. Порядок строился по числу, которого смотрящий не видит,
    и на боевом стенде список выглядел неотсортированным вовсе.
    """
    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert r.status_code == 200, r.text
    payload = r.json()
    # A (+30 мин) -> C (+20) -> B (+10); D закрыт и во вкладку не входит.
    assert ids(payload) == [str(dataset.a), str(dataset.c), str(dataset.b)]
    assert payload["page"] == {"limit": 50, "offset": 0, "total": 3}
    negative = payload["items"][2]
    assert negative["tags"] == ["негатив"]
    assert negative["unread_count"] == 2
    assert negative["client"]["name"] == "Иван Петров"
    assert negative["account"]["title"] == "LP-Test"
    assert negative["last_message"]["body"] == "Это ужас, а не сервис!"
    assert negative["item"]["title"] == "Ремонт iPhone 13"


async def test_waiting_longer_does_not_hoist_a_row_in_the_list(
    client, tokens, db_sessionmaker, dataset
):
    """«Кто дольше ждёт — выше» в ЭТОМ списке больше нет, и это осознанно.

    Правило пришло из UX-аудита (docs/17 §Т1) и решало настоящую беду:
    оператор идёт списком сверху вниз, и «свежее выше» подсовывало ему
    написавшего полминуты назад вместо ждущего двадцать пять минут.

    Но держалось оно на `conversations.unread_count > 0` — колонке, которая
    ГЛОБАЛЬНА и смотрящему не показывается: в ответ уезжает пер-юзерное число
    из маркеров чтения (`read_markers.apply_unread_counts`). То есть «дольше
    ждёт» считалось по чужому непрочитанному. На боевом стенде это выглядело
    так: диалог с `unread_count: 0` в выдаче стоит выше диалога, который на
    два часа свежее, — и объяснить это, глядя в экран, невозможно.

    САМА ЗАДАЧА НЕ ПОТЕРЯНА. «Кто дольше ждёт, тот выше» живёт там, где
    оператор берёт работу, — в очереди «Входящие»: `services.inbox.list_inbox`
    сортирует по `offered_at ASC, id`, и это проверяется отдельно
    (`tests/unit/test_inbox.py`). Список же — про диалоги, которые уже ведут,
    и в нём честнее «где последнее движение».
    """
    async with db_sessionmaker() as s:
        extra_client = Client(channel="avito", external_id="1005", name="Забытый Клиент")
        s.add(extra_client)
        await s.flush()
        forgotten = Conversation(
            channel="avito",
            external_chat_id="chat-e",
            account_id=dataset.account.id,
            client_id=extra_client.id,
            status="new",
            unread_count=1,
            tags=[],
            last_message_at=T0 - timedelta(minutes=40),
            item_title="Ремонт холодильника",
        )
        s.add(forgotten)
        await s.flush()
        forgotten_id = forgotten.id
        await s.commit()

    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert r.status_code == 200, r.text
    order = ids(r.json())

    # Строго по последнему сообщению: +30, +20, +10, −40.
    assert order == [str(dataset.a), str(dataset.c), str(dataset.b), str(forgotten_id)], order


@pytest.mark.parametrize(
    "tab,expected_attr",
    [("my", ["a"]), ("mine", ["a"]), ("new", ["c", "b"]), ("closed", ["d"])],
)
async def test_tabs(client, tokens, dataset, tab, expected_attr):
    r = await client.get(f"/api/v1/conversations?tab={tab}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    expected = {str(getattr(dataset, attr)) for attr in expected_attr}
    assert set(ids(r.json())) == expected


async def test_status_is_a_filter_of_its_own_not_a_tab(client, tokens, dataset):
    """Состояние диалога — измерение, ортогональное вкладке.

    Раньше «Новые» и «Закрытые» стояли вкладками в одном ряду с «Моими» и
    «Всеми», хотя отвечали на другой вопрос: не «чей диалог», а «в каком он
    виде». Заказчик заметил это первым — «кажется, они друг друга дублируют».
    Признаки разведены, и `status` обязан давать ровно то же множество, что
    давала одноимённая вкладка.
    """
    by_tab = await client.get("/api/v1/conversations?tab=new", headers=auth(tokens))
    by_filter = await client.get("/api/v1/conversations?tab=all&status=new", headers=auth(tokens))
    assert by_filter.status_code == 200, by_filter.text
    assert set(ids(by_filter.json())) == set(ids(by_tab.json()))


async def test_an_explicit_status_beats_the_tab_default(client, tokens, dataset):
    """«Мои + Закрытые» не должно давать пустоту.

    Вкладка «Мои» сама по себе означает «мои и ещё не закрытые». Складывались
    бы условия — вышло бы «closed AND NOT closed», то есть пустой список на
    совершенно осмысленный запрос «мои закрытые за сегодня». Выбранный руками
    статус переопределяет умолчание вкладки, а не дополняет его.
    """
    r = await client.get("/api/v1/conversations?tab=all&status=closed", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert set(ids(r.json())) == {str(dataset.d)}, "закрытый диалог обязан найтись"


async def test_an_unknown_status_is_refused(client, tokens, dataset):
    """Опечатка в статусе — отказ, а не молча показанный полный список."""
    r = await client.get("/api/v1/conversations?status=bogus", headers=auth(tokens))
    assert r.status_code in (400, 422), r.text


async def test_tab_validation(client, tokens, dataset):
    r = await client.get("/api/v1/conversations?tab=bogus", headers=auth(tokens))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_search_by_client_name_and_text(client, tokens, dataset):
    r = await client.get("/api/v1/conversations?q=Иван", headers=auth(tokens))
    assert set(ids(r.json())) == {str(dataset.b)}
    # текст сообщения (sqlite: LIKE; кейс-чувствительно для кириллицы, поэтому
    # ищем слово в исходном регистре — настоящий FTS проверяет INT-9 на Postgres)
    r2 = await client.get("/api/v1/conversations?q=разбит", headers=auth(tokens))
    assert str(dataset.c) in ids(r2.json())


async def test_search_looks_everywhere_not_only_in_the_open_tab(client, tokens, dataset):
    """Поиск идёт по всему, включая закрытые (UX-аудит, docs/17 §Т3).

    Раньше условие вкладки применялось и при поиске. У менеджера вкладка по
    умолчанию «Мои» — значит диалог коллеги не находился никогда, а закрытый
    не находился ни из одной вкладки вовсе. Оператор получал честное «ничего
    не найдено» про клиента, который в системе есть, и говорил ему «вы к нам
    не обращались».

    Починка — отдельное значение `tab=any`, а не «игнорировать вкладку при
    непустом запросе»: сочетание вкладки с запросом остаётся законным (поиск
    внутри закрытых проверяется соседним тестом), интерфейс просто получает
    чем выразить то, что он и так обещает, приглушая вкладки на время поиска.

    Проверяем оба прежних провала: закрытый диалог (D, «Мария Архивная») и
    чужой непринятый (B, «Иван Петров») — ни того, ни другого не было видно
    из вкладки «Мои», которая у менеджера стоит по умолчанию.
    """
    for name, expected in (("Мария", dataset.d), ("Иван", dataset.b)):
        r = await client.get(f"/api/v1/conversations?tab=any&q={name}", headers=auth(tokens))
        assert r.status_code == 200, r.text
        assert set(ids(r.json())) == {str(expected)}, (
            f"«{name}» должен находиться из вкладки «Мои», получили {ids(r.json())}"
        )


async def test_the_ordinary_tabs_keep_narrowing(client, tokens, dataset):
    """Обычные вкладки сужают как прежде — `any` их не отменяет."""
    r = await client.get("/api/v1/conversations?tab=mine", headers=auth(tokens))
    assert set(ids(r.json())) == {str(dataset.a)}
    r = await client.get("/api/v1/conversations?tab=mine&q=Мария", headers=auth(tokens))
    assert r.json()["items"] == [], "во вкладке «Мои» чужого закрытого быть не должно"


async def test_filters_tag_unread_updated_since(client, tokens, dataset):
    r = await client.get("/api/v1/conversations?tag=негатив", headers=auth(tokens))
    assert ids(r.json()) == [str(dataset.b)]

    r2 = await client.get("/api/v1/conversations?unread_only=true", headers=auth(tokens))
    assert set(ids(r2.json())) == {str(dataset.b), str(dataset.c)}

    # ISO с Z-суффиксом (01 §1.5): "+00:00" в query превратился бы в пробел
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    r3 = await client.get(f"/api/v1/conversations?updated_since={future}", headers=auth(tokens))
    assert ids(r3.json()) == []

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    r4 = await client.get(f"/api/v1/conversations?updated_since={past}", headers=auth(tokens))
    assert len(ids(r4.json())) == 3  # все не-closed диалоги менялись «после» метки


async def test_detail_fields_and_404(client, tokens, dataset):
    r = await client.get(f"/api/v1/conversations/{dataset.b}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["external_chat_id"] == "chat-b"
    assert body["bot_vars"] == {}
    assert body["first_client_at"] is not None
    assert body["client_conversations_count"] == 1
    assert body["unread_count"] == 2

    r404 = await client.get(f"/api/v1/conversations/{uuid.uuid4()}", headers=auth(tokens))
    assert r404.status_code == 404
    assert r404.json()["error"]["code"] == "not_found"


async def test_detail_says_whether_the_dialog_still_waits_in_the_queue(
    client, tokens, dataset, db_sessionmaker
):
    """`in_inbox` приходит с ДЕТАЛЬЮ, а не только со строкой очереди (7.1).

    Холодный переход по прямой ссылке /chats/{id} — F5, ссылка из уведомления,
    мобильный стек — списка не монтирует вовсе. Без этого поля браузер покажет
    непринятый диалог с обычным полем ввода вместо кнопок «Принять/Отклонить»,
    и оператор напишет в диалог, которого не брал.
    """
    r = await client.get(f"/api/v1/conversations/{dataset.b}", headers=auth(tokens))
    assert r.json()["in_inbox"] is False  # в очередь не ставили
    assert r.json()["offered_at"] is None
    assert r.json()["escalated"] is False

    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, dataset.b)
        conv.offered_at = T0
        conv.assignee_id = None

    r2 = await client.get(f"/api/v1/conversations/{dataset.b}", headers=auth(tokens))
    assert r2.json()["in_inbox"] is True
    assert r2.json()["offered_at"] == "2026-08-04T09:00:00.000Z"

    # И то же самое в строке списка — форма у списка и детали одна (01 §5.1).
    r3 = await client.get("/api/v1/conversations", headers=auth(tokens))
    row = next(i for i in r3.json()["items"] if i["id"] == str(dataset.b))
    assert row["in_inbox"] is True


# --- лента сообщений (01 §6.1, курсоры §1.4) --------------------------------


@pytest.fixture
async def feed(db_sessionmaker, make_avito_account):
    account = await make_avito_account(555000111)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="2001", name="Фидовый Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-feed",
            account_id=account.id,
            client_id=cl.id,
            status="new",
            unread_count=5,
            last_message_at=T0 + timedelta(minutes=5),
        )
        s.add(conv)
        await s.flush()
        message_ids = []
        for n in range(1, 6):
            m = Message(
                conversation_id=conv.id,
                external_message_id=f"am-feed-{n}",
                direction="in",
                sender_type="client",
                body=f"сообщение {n}",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=n),
            )
            s.add(m)
            await s.flush()
            message_ids.append(m.id)
        await s.commit()
        return SimpleNamespace(conversation_id=conv.id, message_ids=message_ids)


def bodies(payload):
    return [m["body"] for m in payload["items"]]


async def test_messages_cursor_walk(client, tokens, feed):
    url = f"/api/v1/conversations/{feed.conversation_id}/messages"
    # первое открытие: последние limit сообщений, порядок ASC
    r = await client.get(f"{url}?limit=2", headers=auth(tokens))
    assert r.status_code == 200, r.text
    p1 = r.json()
    assert bodies(p1) == ["сообщение 4", "сообщение 5"]
    assert p1["page"]["has_more_before"] is True
    assert p1["page"]["has_more_after"] is False

    # скролл вверх
    r2 = await client.get(f"{url}?limit=2&before={p1['page']['prev_cursor']}", headers=auth(tokens))
    p2 = r2.json()
    assert bodies(p2) == ["сообщение 2", "сообщение 3"]
    assert p2["page"]["has_more_before"] is True
    assert p2["page"]["has_more_after"] is True

    r3 = await client.get(f"{url}?limit=2&before={p2['page']['prev_cursor']}", headers=auth(tokens))
    p3 = r3.json()
    assert bodies(p3) == ["сообщение 1"]
    assert p3["page"]["has_more_before"] is False

    # догон вниз (after) от страницы p2
    r4 = await client.get(f"{url}?limit=2&after={p2['page']['next_cursor']}", headers=auth(tokens))
    p4 = r4.json()
    assert bodies(p4) == ["сообщение 4", "сообщение 5"]
    assert p4["page"]["has_more_after"] is False


async def test_messages_both_cursors_rejected(client, tokens, feed):
    cursor = base64.urlsafe_b64encode(f"{T0.isoformat()}|{uuid.uuid4()}".encode()).decode()
    r = await client.get(
        f"/api/v1/conversations/{feed.conversation_id}/messages?before={cursor}&after={cursor}",
        headers=auth(tokens),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_observer_does_not_see_notes(client, tokens, feed, db_sessionmaker):
    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=feed.conversation_id,
                direction="note",
                sender_type="operator",
                body="торгуется, дать скидку до 10%",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=10),
            )
        )
        await s.commit()
    url = f"/api/v1/conversations/{feed.conversation_id}/messages"
    manager_view = (await client.get(url, headers=auth(tokens))).json()
    assert "note" in {m["direction"] for m in manager_view["items"]}
    observer_view = (await client.get(url, headers=auth(tokens, "observer"))).json()
    assert "note" not in {m["direction"] for m in observer_view["items"]}


async def test_mark_read_resets_counter_and_publishes(client, tokens, dataset, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await client.post(f"/api/v1/conversations/{dataset.b}/read", headers=auth(tokens))
    assert r.status_code == 204

    detail = (await client.get(f"/api/v1/conversations/{dataset.b}", headers=auth(tokens))).json()
    assert detail["unread_count"] == 0

    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "conversation:updated"
    assert evt["data"] == {"conversation_id": str(dataset.b), "patch": {"unread_count": 0}}

    # идемпотентность: повторный read — 204 и БЕЗ повторного события
    r2 = await client.post(f"/api/v1/conversations/{dataset.b}/read", headers=auth(tokens))
    assert r2.status_code == 204
    assert await drain_events(pubsub) == []


# --- курсор пагинации: микросекунды (регрессия «догрузка дублирует хвост») ----

T_MICRO = datetime(2026, 8, 4, 10, 0, 0, 123456, tzinfo=UTC)


@pytest.fixture
async def micro_feed(db_sessionmaker, make_avito_account):
    """Лента, где у `created_at` есть микросекунды — как на PostgreSQL."""
    account = await make_avito_account(avito_user_id=555444333)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="1777", name="Микро Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-micro",
            account_id=account.id,
            client_id=cl.id,
            status="new",
            unread_count=2,
            last_message_at=T_MICRO,
        )
        s.add(conv)
        await s.flush()
        for n in range(1, 4):
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"am-micro-{n}",
                    direction="in",
                    sender_type="client",
                    body=f"микро {n}",
                    attachments=[],
                    delivery_status="delivered",
                    # доли секунды НЕ кратны миллисекунде — округление курсора
                    # вниз возвращало бы пограничное сообщение ещё раз
                    created_at=T_MICRO + timedelta(seconds=n, microseconds=n * 111),
                )
            )
        await s.commit()
        return SimpleNamespace(conversation_id=conv.id)


async def test_after_cursor_does_not_repeat_the_boundary_message(client, tokens, micro_feed):
    """`GET /messages?after=<next_cursor>` — догрузка после обрыва WS (01 §6.1).

    Курсор кодируется с микросекундами, а сравнение в ветке `after` строгое:
    последнее известное клиенту сообщение приезжать повторно не должно.
    """
    url = f"/api/v1/conversations/{micro_feed.conversation_id}/messages"
    page = (await client.get(f"{url}?limit=3", headers=auth(tokens))).json()
    assert bodies(page) == ["микро 1", "микро 2", "микро 3"]

    tail = (
        await client.get(f"{url}?after={page['page']['next_cursor']}", headers=auth(tokens))
    ).json()
    assert bodies(tail) == []  # хвоста нет — и дубля последнего тоже
    assert tail["page"]["has_more_after"] is False


async def test_after_cursor_walks_a_microsecond_feed_without_duplicates(client, tokens, micro_feed):
    url = f"/api/v1/conversations/{micro_feed.conversation_id}/messages"
    seen: list[str] = []
    page = (await client.get(f"{url}?limit=1", headers=auth(tokens))).json()
    cursor = page["page"]["prev_cursor"]
    first = (await client.get(f"{url}?limit=1&before={cursor}", headers=auth(tokens))).json()
    cursor = first["page"]["prev_cursor"]
    start = (await client.get(f"{url}?limit=1&before={cursor}", headers=auth(tokens))).json()
    seen += bodies(start)
    cursor = start["page"]["next_cursor"]
    for _ in range(2):
        step = (await client.get(f"{url}?limit=1&after={cursor}", headers=auth(tokens))).json()
        seen += bodies(step)
        cursor = step["page"]["next_cursor"]
    assert seen == ["микро 1", "микро 2", "микро 3"]  # ни одного повтора


async def test_cursor_keeps_microsecond_precision():
    from app.services import conversations as convs

    at = datetime(2026, 8, 4, 10, 0, 0, 123456, tzinfo=UTC)
    mid = uuid.uuid4()
    decoded_at, decoded_id = convs.decode_cursor(convs.encode_cursor(at, mid))
    assert decoded_at == at  # не 10:00:00.123000
    assert decoded_id == mid


async def test_decode_cursor_still_accepts_millisecond_cursors():
    """Курсоры, выданные до правки, обязаны продолжать работать."""
    from app.services import conversations as convs

    mid = uuid.uuid4()
    legacy = base64.urlsafe_b64encode(f"2026-08-04T10:00:00.123Z|{mid}".encode()).decode()
    decoded_at, decoded_id = convs.decode_cursor(legacy)
    assert decoded_at == datetime(2026, 8, 4, 10, 0, 0, 123000, tzinfo=UTC)
    assert decoded_id == mid


# --- фильтр «Без ответственного» (11 §2.5.2) ---------------------------------


async def test_unassigned_filter_returns_only_conversations_without_assignee(
    client, tokens, dataset
):
    r = await client.get("/api/v1/conversations?unassigned=true", headers=auth(tokens, "head"))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert all(item["assignee"] is None for item in body["items"])
    assert str(dataset.a) not in ids  # A назначен на менеджера
    assert str(dataset.b) in ids
    assert body["page"]["total"] == len(body["items"])


async def test_unassigned_is_not_the_same_as_the_new_tab(client, tokens, dataset):
    """Закрытый диалог без ответственного — тоже «без ответственного».

    Пункт фильтра нельзя подменить вкладкой «Новые»: диалог, закрытый ботом,
    ответственного не имеет, но в «Новые» не попадает (11 §2.5.2).
    """
    closed = (
        await client.get("/api/v1/conversations?unassigned=true&tab=closed", headers=auth(tokens))
    ).json()
    new_tab = (await client.get("/api/v1/conversations?tab=new", headers=auth(tokens))).json()
    assert str(dataset.d) in {i["id"] for i in closed["items"]}
    assert str(dataset.d) not in {i["id"] for i in new_tab["items"]}


async def test_unassigned_false_is_the_default(client, tokens, dataset):
    default = (await client.get("/api/v1/conversations", headers=auth(tokens))).json()
    explicit = (
        await client.get("/api/v1/conversations?unassigned=false", headers=auth(tokens))
    ).json()
    assert [i["id"] for i in default["items"]] == [i["id"] for i in explicit["items"]]
