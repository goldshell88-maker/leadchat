"""Поведение статусной модели: переходы, автопереходы, отложка (docs/38 §3–§7).

Каждый тест здесь охраняет решение, а не строку кода: если правило можно
сломать так, что тест останется зелёным, тест написан зря. Правило проекта
(HANDOFF-2026-08-09): написал охрану — сломай исправление и убедись, что тест
краснеет. Что именно ломалось, записано в ответе задачи.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import Client, Conversation, Message
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services import distribution, inbox

try:  # настоящий тип события адаптера, когда зона OAuth на месте
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

T0 = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def conv(db_sessionmaker, make_avito_account, users_by_role):
    """Диалог «в работе» у менеджера, с одним ответом оператора в ленте.

    Ответ нужен почти каждому тесту: без него `waiting_client` запрещён
    (`no_reply_yet`), и половина проверок упиралась бы в этот отказ вместо
    того, что они проверяют.
    """
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id="9100", name="Ольга Никитина")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="chat-status",
            account_id=account.id,
            client_id=client.id,
            status="in_progress",
            status_since=T0,
            assignee_id=users_by_role["manager"].id,
            unread_count=0,
            last_message_at=T0,
        )
        s.add(row)
        await s.flush()
        s.add(
            Message(
                conversation_id=row.id,
                external_message_id="m-out-1",
                direction="out",
                sender_type="operator",
                sender_user_id=users_by_role["manager"].id,
                body="Здравствуйте! Мастер приедет завтра.",
                attachments=[],
                delivery_status="delivered",
                created_at=T0,
            )
        )
        await s.commit()
        return SimpleNamespace(
            id=row.id,
            external_chat_id=row.external_chat_id,
            account=account,
            client=client,
        )


async def _get(db_sessionmaker, conv_id) -> Conversation:
    async with db_sessionmaker() as s:
        return (
            await s.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()


# ------------------------------------------------------------------ матрица


async def test_new_cannot_go_to_waiting_client(client, tokens, conv, db_sessionmaker):
    """`new → waiting_client` — 422 `no_reply_yet`, а не 200.

    «Ждёт клиента» означает «ход за клиентом», а ход не может быть за тем, кому
    ещё ничего не сказали. Без этого запрета диспетчер мог бы «поставить на
    ожидание» обращение, которое никто не читал, — и оно исчезло бы из
    активных, не получив ни одного ответа.
    """
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv.id)
            .values(status="new", assignee_id=None)
        )
        # И сносим ответ оператора: иначе запрет проверялся бы только матрицей.
        await s.execute(sa.delete(Message).where(Message.conversation_id == conv.id))
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "waiting_client"},
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "no_reply_yet"


async def test_waiting_client_needs_a_delivered_operator_reply(
    client, tokens, conv, db_sessionmaker
):
    """Неотправленный ответ оператора «Ждёт клиента» не разрешает.

    Клиент не видел наших слов — ход не переходил к нему. Ровно ту же ошибку
    (`direction='out'` без проверки доставки) в этом проекте уже ловили в
    статистике: неотправленный ответ гасил ожидание.
    """
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Message)
            .where(Message.conversation_id == conv.id)
            .values(delivery_status="failed")
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "waiting_client"},
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "no_reply_yet"


async def test_closed_reopens_only_to_in_progress(client, tokens, conv, db_sessionmaker):
    """Закрытый нельзя вернуть сразу в «Ждёт клиента».

    Иначе диалог выходит из закрытых и не попадает ни в чью работу: ход
    объявлен за клиентом, а взяться за него никто не взялся.

    Вторым значением в этом цикле был «Отложен» — он снят 12 августа вместе со
    всей отложкой, и теперь его отвергает схема запроса (см. соседний тест).
    """
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation).where(Conversation.id == conv.id).values(status="closed")
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "waiting_client"},
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "reopen_to_progress_only"


async def test_the_snoozed_status_is_refused_by_the_request_schema(client, tokens, conv):
    """«Отложен» отвергается на входе — и вместе с ним срок отложки.

    ОХРАНА РЕШЕНИЯ ВЛАДЕЛЬЦА ОТ 12 АВГУСТА. Отложка снята целиком, и вернуться
    она может только осознанной правкой словаря — а не тем, что кто-то шлёт
    старое тело со старой вкладки.

    Проверяются ОБА пути возврата по отдельности, потому что они независимы:
    можно вернуть значение в `Literal`, забыв про поле срока, и наоборот. Оба
    дают 400 — отказ разбора тела, который человек увидит, а не тихое «ок» на
    запрос, который система не исполнила.

    Коды полей проверяются поимённо: без этого тест зеленел бы на любой чужой
    ошибке валидации и перестал бы охранять то, ради чего написан.
    """
    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "snoozed"},
        headers=auth(tokens),
    )
    assert r.status_code == 400, r.text
    assert [f["field"] for f in r.json()["error"]["details"]["fields"]] == ["status"]

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "in_progress", "snooze_until": "2026-08-20T10:00:00+00:00"},
        headers=auth(tokens),
    )
    assert r.status_code == 400, r.text
    fields = r.json()["error"]["details"]["fields"]
    assert [f["field"] for f in fields] == ["snooze_until"]
    assert fields[0]["rule"] == "extra_forbidden"


# -------------------------------------------------------------- status_since


async def test_status_since_moves_only_on_a_real_change(client, tokens, conv, db_sessionmaker):
    """Отметка входа в статус пишется при смене — и НЕ двигается без неё.

    Это и есть смысл поля: «когда вошёл», а не «когда последний раз про него
    вспомнили». Если бы её двигал каждый вызов, строка «В работе у Иванова ·
    12 мин» показывала бы время последнего действия — другую величину, и
    показывала бы правдоподобно.
    """
    before = await _get(db_sessionmaker, conv.id)
    assert before.status_since == T0.replace(tzinfo=None) or before.status_since == T0

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "waiting_client"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    after = await _get(db_sessionmaker, conv.id)
    assert after.status_since is not None
    assert after.status_since != before.status_since, "status_since не сдвинулся на смене статуса"

    # Повторный вызов той же функции с тем же значением отметку не трогает.
    frozen = after.status_since
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        status_dict.set_status(row, "waiting_client")
        await s.commit()
    assert (await _get(db_sessionmaker, conv.id)).status_since == frozen


async def test_status_since_is_exposed_in_the_row_and_in_the_patch(client, tokens, conv):
    """`status_since` едет и в детали, и в кадре WS.

    Без него строка контекста у второго оператора продолжала бы считать от
    прошлого состояния до ближайшего перезапроса.
    """
    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "waiting_client"},
        headers=auth(tokens),
    )
    assert r.status_code == 200
    assert r.json()["status_since"], "деталь диалога не отдаёт status_since"


# ------------------------------------------------------------- автопереходы


async def test_client_reply_wakes_a_waiting_dialog(db_sessionmaker, conv, users_by_role):
    """Клиент написал в «Ждёт клиента» — статус сам вернулся в «В работе».

    Оба состояния означают «ход за клиентом». Клиент сходил — ждать больше
    нечего. Ответственный при этом НЕ меняется: диалог остаётся у того, кто
    его вёл.
    """
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        status_dict.set_status(row, "waiting_client")
        await s.commit()

    await _inbound_text(db_sessionmaker, conv, "Здравствуйте, а во сколько мастер?")

    fresh = await _get(db_sessionmaker, conv.id)
    assert fresh.status == "in_progress"
    assert fresh.assignee_id == users_by_role["manager"].id, "пробуждение отобрало диалог"


# ЗДЕСЬ БЫЛ `test_client_reply_wakes_a_snoozed_dialog_and_clears_the_deadline`:
# клиент написал в отложенный диалог — отложка снята, срок обнулён. Отложки
# больше нет, и завести диалог в статус `snoozed` теперь не даст сама база
# (CHECK из миграции 0033). Живая половина того же правила — пробуждение
# «Нового» — проверяется тестом выше.


async def test_operator_reply_never_touches_waiting_client(db_sessionmaker, conv, users_by_role):
    """Наш ответ НЕ трогает «Ждёт клиента». Это главное решение §3.

    Написать клиенту, не забирая ход себе, — законное действие («напоминаю про
    завтра»); сбрасывай мы статус на каждом нашем сообщении, «Ждёт клиента»
    обесценился бы за один день: в нём не осталось бы ничего, кроме диалогов,
    в которые никто не заходил.

    Первой половиной этого теста было «наш ответ будит „Отложен“» — ушла
    вместе с отложкой 12 августа.
    """
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        status_dict.set_status(row, "waiting_client")
        await s.commit()
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        changed = await convs.ensure_in_progress(
            s, row, source="reply", actor_id=users_by_role["manager"].id
        )
        await s.commit()
    assert changed is False, "ответ оператора сбросил «Ждёт клиента»"
    assert (await _get(db_sessionmaker, conv.id)).status == "waiting_client"


async def test_return_to_queue_clears_the_leftover_snooze(db_sessionmaker, conv, users_by_role):
    """Возврат в очередь гасит ОСТАВШИЕСЯ ОТ ОТЛОЖКИ поля.

    ЗАЧЕМ ЭТО ПРОВЕРЯТЬ, КОГДА ОТЛОЖКИ НЕТ. Колонки `snoozed_until`,
    `snoozed_by_id` и `snooze_reason` в базе остались вместе со значениями —
    снос необратим, а решение владельца может измениться (миграция 0033).
    Значит на бою есть строки со сроком, который никто уже не исполнит, и
    `clear_snooze` в шести местах кода — единственное, что их убирает.

    Убери кто-нибудь вызов «за ненадобностью» — данные останутся навсегда, и
    вернувшаяся когда-нибудь отложка увидит чужие сроки годичной давности.
    """
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        status_dict.set_status(row, "snoozed")
        row.snoozed_until = T0 + timedelta(days=1)
        row.snoozed_by_id = users_by_role["manager"].id
        row.snooze_reason = "клиент перезвонит"
        inbox.return_to_queue(row, now=T0)
        await s.commit()

    fresh = await _get(db_sessionmaker, conv.id)
    assert fresh.status == "new"
    assert (fresh.snoozed_until, fresh.snoozed_by_id, fresh.snooze_reason) == (None, None, None)


# ------------------------------------------------- ожидание и непрочитанные


async def test_closing_zeroes_unread_and_clears_awaiting(client, tokens, conv, db_sessionmaker):
    """Закрытие обнуляет `unread_count` и гасит ожидание (дефект №3).

    Пока счётчик переживал закрытие, закрытый диалог показывал в списке шкалу
    «ждёт 3 ч»: она считается по непрочитанным. Оператор видел строку,
    требующую внимания, открывал — и находил закрытое обращение.

    Обнуляем НА СЕРВЕРЕ, а не прячем шкалу на клиенте: у счётчика есть второй
    потребитель — бейдж в трее десктопа.
    """
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv.id)
            .values(unread_count=4, awaiting_since=T0)
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{conv.id}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    fresh = await _get(db_sessionmaker, conv.id)
    assert fresh.unread_count == 0, "закрытый диалог остался с непрочитанными"
    assert fresh.awaiting_since is None


# ЗДЕСЬ БЫЛ `test_snooze_kills_the_waiting_mark`: перевод в «Отложен» гасил
# `awaiting_since`. Он охранял ещё и цветовое решение — янтарь занят полосой
# срочности «клиент ждёт», и янтарный чип «Отложен» был взят того же цвета
# ровно потому, что вдвоём на одной карточке появиться не могли. Спорить за
# янтарь стало некому: чип снят 12 августа вместе со статусом (переменные
# `--lc-status-snoozed-*` убраны из lc-vars.css). Живая половина правила —
# гашение ожидания при закрытии — проверяется тестом выше.


# ------------------------------------------------------------------ нагрузка


async def test_waiting_client_is_not_a_load(db_sessionmaker, conv, users_by_role, redis):
    """«Ждёт клиента» не считается нагрузкой автораздачи.

    Ход не за нами: оператор с двадцатью такими диалогами внимания на них
    сейчас не тратит, и следующее обращение должно доставаться ему наравне с
    остальными. Пока список был «всё, кроме закрытых», система обходила бы его
    стороной — и раздавала бы работу тем, у кого её и так больше.

    Здесь же проверялся «Отложен» — по тому же доводу и с тем же исходом. Он
    снят 12 августа; `waiting_client` остался единственным значением, которое
    открыто и при этом не нагрузка, то есть единственным, ради которого
    `ACTIVE_STATUSES` вообще отличается от `OPEN_STATUSES`.
    """
    async with db_sessionmaker() as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == conv.id))).scalar_one()
        status_dict.set_status(row, "waiting_client")
        await s.commit()

    async with db_sessionmaker() as s:
        load = await distribution._load_by_user(s, {users_by_role["manager"].id})
    assert load.get(users_by_role["manager"].id, 0) == 0, (
        "диалог, ждущий клиента, посчитан нагрузкой"
    )


# --------------------------------------------------------------- вспомогательное


def _in_hours(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


async def _inbound_text(db_sessionmaker, conv, text: str) -> None:
    """Входящее от клиента через НАСТОЯЩИЙ конвейер `apply_inbound_event`.

    Именно через него, а не прямым присваиванием статуса: правило «клиент
    написал — диалог проснулся» живёт в конвейере, и проверять надо его, а не
    свою копию этого правила в тесте.
    """
    from app.services import inbound

    event = InboundEvent(
        external_chat_id=conv.external_chat_id,
        external_message_id=f"in-{uuid.uuid4().hex[:8]}",
        author_id=int(conv.client.external_id),
        account_user_id=conv.account.avito_user_id,
        text=text,
        created_at=datetime.now(UTC),
        client_name=conv.client.name,
    )
    # Сессия отдаётся конвейеру НЕТРОНУТОЙ: он открывает транзакцию сам
    # (`async with db.begin()`), и любое чтение до вызова её уже начнёт.
    # Аккаунт передаётся отсоединённым объектом — его атрибуты загружены
    # (`expire_on_commit=False` у фабрики сессий), а конвейеру нужны только они.
    async with db_sessionmaker() as s:
        await inbound.apply_inbound_event(s, None, conv.account, event, publish=False)
