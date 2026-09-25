"""Очередь «Входящие» — программный интерфейс и реалтайм (15 §2.1, план 7.1).

Здесь проверяется ровно то, ради чего блок затевался при тринадцати
операторах:

* **принятие атомарно** — двое, нажавшие «Принять» одновременно, получают
  РАЗНЫЙ ответ. Это не рассуждение о `FOR UPDATE`, а два реальных запроса
  через `asyncio.gather` (см. `test_two_operators_claiming_at_once...`);
* **права** — принимать может тот, кто отвечает клиентам; руководитель и
  наблюдатель получают разные 403, потому что фронт рисует по ним разное;
* **события несут достаточно данных**, чтобы браузер перерисовал список без
  второго запроса — иначе тринадцать вкладок после каждого принятия пойдут
  за `GET /conversations` разом;
* **счётчик персональный** — отказ одного не гасит очередь остальным.

Матрица RBAC этого модуля живёт здесь, а не в tests/unit/test_rbac.py: там
ALLOW-ветка упёрлась бы в 409 «уже принят» или 404, то есть проверяла бы
состояние диалога вместо отказа доступа (та же причина, что у `/assign` и
`/status` — см. COVERED_ELSEWHERE в test_rbac.py).
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.core.security import create_access_token
from app.models import Client, Conversation, Message
from app.ws.hub import (
    INBOX_CLAIMED,
    INBOX_DECLINED,
    INBOX_NEW,
    INBOX_RELEASED,
    Hub,
    can_claim,
    publish_inbox_new,
)
from tests.unit.conftest import drain_events

T0 = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)

ALL_ROLES = ("admin", "head", "manager", "observer")
OPERATORS = ("admin", "manager")  # есть messages:send (01 §12)

#: Действия очереди, доступные ЛЮБОМУ оператору.
#:
#: ⚠ `release` ОТСЮДА УБРАН 28.08 (решение владельца: «вернуть в очередь мог
#: только бот или администратор, у менеджеров эту функцию отключи»). У него
#: теперь своё право `conversations:release`, и оно только у администратора —
#: разбор ниже, в `test_release_is_admin_only`.
ACTIONS = ("claim", "decline")


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


def test_the_queue_is_mounted_in_the_application(app: FastAPI) -> None:
    """Страж вместо моста: пути очереди обязаны быть в самом приложении.

    Раньше здесь стояла фикстура, монтировавшая роутер очереди самостоятельно —
    и она врала: в бою `include_router` в main.py не было, то есть очередь
    показывалась списком, а кнопки «Принять» отвечали 404. Мост снят, вместо
    него проверка: путей пять, и приходят они из `create_app`, а не из теста.
    """
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/inbox",
        "/api/v1/inbox/count",
        "/api/v1/conversations/{conversation_id}/claim",
        "/api/v1/conversations/{conversation_id}/decline",
        "/api/v1/conversations/{conversation_id}/release",
    ):
        assert path in paths, f"{path} нет в приложении — очередь смонтирована только в тестах"


class TestTheQueueIsNotUnloadedInBulkAnyMore:
    """Разгрузка очереди убрана целиком (требование владельца от 11 августа, №8).

    ЧТО БЫЛО. Кнопка «Разгрузить…» над очередью, окно с предпросмотром, две
    ручки (`GET /inbox/stale`, `POST /inbox/close-stale`), ночное задание
    планировщика в 04:20 UTC и уведомление администраторам о каждом его
    прогоне. Всё это одним нажатием (или одной ночью) закрывало пачку
    непринятых диалогов, в которых клиент молчал дольше выбранного срока.

    ПОЧЕМУ УБРАНО, А НЕ ВЫКЛЮЧЕНО. Решение владельца: «функция не нужна вообще».
    В живой переписке с настоящими клиентами массовое закрытие необратимо — и
    отменять его пришлось бы руками по одному диалогу.

    ЗАЧЕМ ПРОВЕРКА НА ОТСУТСТВИЕ. Удаление возможности ничем не защищено: чужая
    ветка вернёт ручку или строку планировщика, всё позеленеет, и на боевом
    сервере снова заработает ночное закрытие диалогов, от которого отказались.
    Тест держит именно это — не «код удалён», а «поведения нет».
    """

    async def test_the_endpoints_are_gone(self, client, tokens) -> None:
        for method, path in (
            ("get", "/api/v1/inbox/stale?days=30"),
            ("post", "/api/v1/inbox/close-stale?days=30"),
        ):
            r = await getattr(client, method)(path, headers=auth(tokens, "admin"))
            assert r.status_code == 404, f"{method.upper()} {path} → {r.status_code}, ждали 404"

    def test_the_nightly_run_is_not_scheduled(self) -> None:
        from app.scheduler.main import build_scheduler

        jobs = sorted(job.id for job in build_scheduler().get_jobs())
        assert "queue_cleanup" not in jobs, jobs

    def test_the_notification_kind_is_gone(self) -> None:
        """Уведомления о разгрузке больше нет ни на сервере, ни в интерфейсе.

        Вторую половину (иконку и подпись в каталоге фронта) сторожит
        tests/unit/test_notification_catalog.py: вид, оставшийся на одной
        стороне, уронит его.
        """
        from app.services.notifications import KINDS

        assert "inbox.cleaned" not in KINDS


@pytest.fixture
async def queued(db_sessionmaker, make_avito_account) -> SimpleNamespace:
    """Диалог, ждущий принятия: предложен очереди и никем не принят."""
    account = await make_avito_account(777100200)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="9101", name="Иван Петров", phone="+79261234567")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-inbox-1",
            account_id=account.id,
            client_id=cl.id,
            status="new",
            unread_count=1,
            last_message_at=T0,
            offered_at=T0,
            item_title="Ремонт iPhone 13",
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="am-inbox-1",
                direction="in",
                sender_type="client",
                body="Здравствуйте! Сколько стоит замена экрана?",
                attachments=[],
                delivery_status="delivered",
                created_at=T0,
            )
        )
        await s.commit()
        return SimpleNamespace(account=account, client_id=cl.id, conversation_id=conv.id)


@pytest.fixture
async def queued_pair(db_sessionmaker, make_avito_account) -> SimpleNamespace:
    """Две очереди подряд — чтобы счётчик мог измениться, а не остаться нулём."""
    account = await make_avito_account(777100201)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="9102", name="Мария Соколова")
        s.add(cl)
        await s.flush()
        ids = []
        for n in (1, 2):
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-inbox-pair-{n}",
                account_id=account.id,
                client_id=cl.id,
                status="new",
                unread_count=1,
                last_message_at=T0 + timedelta(minutes=n),
                offered_at=T0 + timedelta(minutes=n),
            )
            s.add(conv)
            await s.flush()
            ids.append(conv.id)
        await s.commit()
        return SimpleNamespace(account=account, ids=ids)


@pytest.fixture
async def colleague(make_user) -> SimpleNamespace:
    """Второй менеджер со своим токеном — «такой же оператор, как я».

    В базовом стенде на каждую роль по одному человеку, и разницу между «мой
    диалог» и «диалог коллеги» приходилось бы проверять на админе — а у него
    прав заведомо больше, и проверка получилась бы про роль, а не про хозяина.
    """
    user = await make_user("manager2@leadchat.test", role="manager", full_name="Пётр Второй")
    token = create_access_token(user_id=str(user.id), role=user.role)
    return SimpleNamespace(user=user, headers={"Authorization": f"Bearer {token}"})


def _url(action: str, conversation_id) -> str:
    return f"/api/v1/conversations/{conversation_id}/{action}"


async def act(client, tokens, action: str, conversation_id, role: str = "manager", **kw):
    """Действие очереди, которое ОБЯЗАНО пройти: 200 или падение с телом ответа."""
    r = await client.post(_url(action, conversation_id), headers=auth(tokens, role), **kw)
    assert r.status_code == 200, r.text
    return r


async def count_for(client, tokens, role: str = "manager") -> int:
    r = await client.get("/api/v1/inbox/count", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    return r.json()["count"]


async def queue_ids(client, tokens, role: str = "manager") -> list[str]:
    r = await client.get("/api/v1/conversations?tab=inbox", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


# --- права (01 §12, DESIGN §5.1) ---------------------------------------------


@pytest.mark.parametrize("action", ACTIONS)
async def test_head_gets_read_only_role_on_every_queue_action(client, tokens, queued, action):
    """Руководитель не отвечает клиентам, значит и диалогов не берёт.

    Код именно `read_only_role`, а не общий `forbidden`: по нему фронт рисует
    плашку «Режим просмотра» вместо «Недостаточно прав» (01 §12).
    """
    r = await client.post(_url(action, queued.conversation_id), headers=auth(tokens, "head"))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "read_only_role"


@pytest.mark.parametrize("action", ACTIONS)
async def test_observer_is_forbidden_on_every_queue_action(client, tokens, queued, action):
    r = await client.post(_url(action, queued.conversation_id), headers=auth(tokens, "observer"))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("role", OPERATORS)
async def test_operators_are_not_stopped_by_rbac(client, tokens, queued, action, role):
    """Админ и менеджер проходят фильтр прав.

    Утверждаем «не 401/403», а не «200»: release на непринятом диалоге честно
    отвечает 409 — это конфликт состояния, а не отказ доступа.
    """
    r = await client.post(_url(action, queued.conversation_id), headers=auth(tokens, role))
    assert r.status_code not in (401, 403), r.text


@pytest.mark.parametrize("action", ACTIONS)
async def test_anonymous_is_rejected(client, queued, action):
    r = await client.post(_url(action, queued.conversation_id))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_both_entrances_return_the_same_queue(client, tokens, queued_pair):
    """`GET /inbox` и `?tab=inbox` — один список, не две реализации.

    Расхождение здесь означало бы ровно ту беду, ради которой выборка вынесена
    в одну функцию: вкладка показывает одно, рабочий экран очереди — другое.
    """
    direct = await client.get("/api/v1/inbox", headers=auth(tokens))
    via_tab = await client.get("/api/v1/conversations?tab=inbox", headers=auth(tokens))
    assert direct.status_code == 200, direct.text
    assert via_tab.status_code == 200, via_tab.text

    # ⚠ ТИКАЮЩИЕ ПОЛЯ СРАВНИВАЕМ ОТДЕЛЬНО (28.08, поймано на полном прогоне).
    #
    # `waiting_seconds` и `waiting_human` считаются от `datetime.now()` в момент
    # ответа. Два запроса подряд обычно попадают в одну секунду — и проверка
    # зеленела месяцами, — но стоит второму перешагнуть границу секунды, как
    # сравнение целиком краснеет на ровном месте. Мигающая проверка в воротах
    # выкатки хуже отсутствующей: она либо блокирует выкатку без причины, либо
    # приучает перезапускать до зелени, и тогда настоящую поломку тоже
    # перезапустят.
    #
    # Сами поля из сравнения НЕ выбрасываем: ниже проверено, что они есть в
    # обоих ответах и совпадают с точностью до секунды. Иначе разъехавшийся
    # сериализатор прошёл бы мимо.
    ТИКАЮЩИЕ = ("waiting_seconds", "waiting_human")

    def без_часов(payload: dict) -> dict:
        снимок = dict(payload)
        снимок["items"] = [
            {k: v for k, v in i.items() if k not in ТИКАЮЩИЕ} for i in payload["items"]
        ]
        return снимок

    assert без_часов(direct.json()) == без_часов(via_tab.json())

    for левый, правый in zip(direct.json()["items"], via_tab.json()["items"], strict=True):
        assert "waiting_seconds" in левый and "waiting_seconds" in правый, (
            "счётчик ожидания пропал из одного из ответов — списки разъехались"
        )
        assert abs((левый["waiting_seconds"] or 0) - (правый["waiting_seconds"] or 0)) <= 1

    # И порядок тот же: дольше всех ждущий — первым (в списке диалогов обратный).
    assert [i["id"] for i in direct.json()["items"]] == [str(i) for i in queued_pair.ids]


async def test_inbox_page_envelope_matches_the_conversation_list(client, tokens, queued_pair):
    """Конверт `{items, page}` общий — второй формы у фронта быть не должно."""
    r = await client.get("/api/v1/inbox?limit=1&offset=1", headers=auth(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page"] == {"limit": 1, "offset": 1, "total": 2}
    assert [i["id"] for i in body["items"]] == [str(queued_pair.ids[1])]


async def test_inbox_filters_by_account(client, tokens, queued, queued_pair):
    r = await client.get(f"/api/v1/inbox?account_id={queued.account.id}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == [str(queued.conversation_id)]


@pytest.mark.parametrize("role", ALL_ROLES)
async def test_every_role_may_watch_the_queue(client, tokens, queued, role):
    """Смотреть очередь может любой — право `conversations:read` есть у всех.

    Руководителю это нужно по делу: растущая очередь — его сигнал. Кнопку
    «Принять» ему не рисуют, но и прятать от него размер очереди незачем.
    """
    assert await count_for(client, tokens, role) == 1
    assert str(queued.conversation_id) in await queue_ids(client, tokens, role)

    direct = await client.get("/api/v1/inbox", headers=auth(tokens, role))
    assert direct.status_code == 200, direct.text
    assert str(queued.conversation_id) in [i["id"] for i in direct.json()["items"]]


@pytest.mark.parametrize("path", ("/api/v1/inbox", "/api/v1/inbox/count"))
async def test_reading_the_queue_requires_a_token(client, path):
    r = await client.get(path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# --- атомарность принятия ----------------------------------------------------


async def test_two_operators_claiming_at_once_get_different_answers(client, tokens, queued):
    """Гонка, ради которой написан блок: победитель ровно один.

    Оба запроса уходят в приложение одновременно (`asyncio.gather`), каждый
    со своим токеном и своей сессией БД. Если принятие где-нибудь распадётся
    на «прочитал → проверил → записал», планировщик asyncio встанет в разрыв
    и оба получат 200 — то есть двое напишут одному клиенту, ровно то, из-за
    чего команда не может работать по-старому.
    """
    url = _url("claim", queued.conversation_id)
    first, second = await asyncio.gather(
        client.post(url, headers=auth(tokens, "manager")),
        client.post(url, headers=auth(tokens, "admin")),
        return_exceptions=True,
    )
    for r in (first, second):
        assert not isinstance(r, BaseException), f"запрос упал вместо ответа: {r!r}"

    codes = sorted(r.status_code for r in (first, second))
    assert codes == [200, 409], (first.status_code, first.text, second.status_code, second.text)

    loser = first if first.status_code == 409 else second
    winner = first if first.status_code == 200 else second
    assert loser.json()["error"]["code"] == "already_claimed"
    # Проигравшему называют победителя — иначе он жмёт «Принять» ещё трижды.
    holder = loser.json()["error"]["details"]["claimed_by"]
    assert holder["full_name"]
    assert holder["id"] == winner.json()["conversation"]["assignee"]["id"]
    assert winner.json()["conversation"]["id"] == str(queued.conversation_id)


async def test_second_claim_is_already_claimed(client, tokens, queued):
    url = _url("claim", queued.conversation_id)
    first = await client.post(url, headers=auth(tokens, "manager"))
    assert first.status_code == 200, first.text

    second = await client.post(url, headers=auth(tokens, "admin"))
    assert second.status_code == 409, second.text
    body = second.json()["error"]
    assert body["code"] == "already_claimed"
    assert body["details"]["claimed_by"]["id"] == first.json()["conversation"]["assignee"]["id"]
    assert body["details"]["mine"] is False


async def test_claim_is_idempotent_for_its_own_owner_only_as_a_conflict(client, tokens, queued):
    """Повторное «Принять» тем же человеком — тоже 409, а не тихий 200.

    Диалог уже у него: второй ответ 200 означал бы, что кнопка «Принять»
    осталась активной на принятом диалоге, и фронт не заметил бы ошибки.
    Флаг `mine` отличает «уже ваш» от «увёл коллега» — тексты разные.
    """
    url = _url("claim", queued.conversation_id)
    assert (await client.post(url, headers=auth(tokens, "manager"))).status_code == 200
    again = await client.post(url, headers=auth(tokens, "manager"))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_claimed"
    assert again.json()["error"]["details"]["mine"] is True


# Чего в этом файле НЕТ и почему. Состояние базы ПОСЛЕ гонки здесь не
# проверяется, и это не забывчивость: юниты ходят в SQLite, где `StaticPool`
# держит ОДНО соединение на все сессии сразу. Транзакции двух одновременных
# запросов сидят на нём вперемешку — откат проигравшего сбрасывает ещё не
# зафиксированную запись победителя, и «200, а диалог ничей» на этом стенде
# получается стабильно. Свойство стенда, не продукта: в бою у каждого запроса
# своё соединение. Поэтому здесь проверяются ОТВЕТЫ (их арбитр — рассинхрону
# не подверженный `rowcount`), а состояние базы после залпа из десяти
# операторов — в tests/integration/test_inbox_race.py на настоящем PostgreSQL.


async def test_claim_of_an_unknown_conversation_is_404(client, tokens):
    r = await client.post(_url("claim", uuid.uuid4()), headers=auth(tokens))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_claiming_a_closed_dialog_is_refused_by_state_not_by_rights(
    client, tokens, db_sessionmaker, queued
):
    """Диалог закрыли, пока он ждал: 422, а не 403 и не тихий 200.

    Разница важна фронту: 403 он рисует как «недостаточно прав» и предлагает
    позвать администратора, а здесь звать некого — принимать просто нечего.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, queued.conversation_id)
        conv.status = "closed"
        await s.commit()

    r = await client.post(_url("claim", queued.conversation_id), headers=auth(tokens))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "conversation_closed"


# --- ответы ручек ------------------------------------------------------------


async def test_claim_returns_the_whole_conversation_and_a_fresh_count(
    client, tokens, users_by_role, queued_pair
):
    """Фронт открывает принятый диалог из этого же ответа, без второго запроса."""
    before = await count_for(client, tokens)
    assert before == 2

    body = (await act(client, tokens, "claim", queued_pair.ids[0])).json()

    conv = body["conversation"]
    # Это объект детали (01 §5.2), а не огрызок: у фронта должно хватить его
    # и на шапку, и на правую карточку, и на строку списка «Мои».
    for field in ("id", "status", "channel", "account", "client", "assignee", "last_message_at"):
        assert field in conv, f"в ответе claim нет поля {field}"
    assert "external_chat_id" in conv and "client_conversations_count" in conv
    assert conv["id"] == str(queued_pair.ids[0])
    assert conv["assignee"]["id"] == str(users_by_role["manager"].id)

    assert body["count"] == before - 1, "счётчик очереди должен приехать вместе с диалогом"
    assert await count_for(client, tokens) == 1


async def test_claimed_conversation_leaves_the_queue_for_everyone(client, tokens, queued):
    await act(client, tokens, "claim", queued.conversation_id)

    for role in ALL_ROLES:
        ids = await queue_ids(client, tokens, role)
        assert str(queued.conversation_id) not in ids, f"диалог остался в очереди у {role}"


async def test_decline_removes_it_from_my_queue_only(client, tokens, queued_pair):
    """Отказ — личное решение: у коллеги очередь прежняя.

    Иначе один уставший оператор «отклонил» бы диалог для всей команды, и
    клиент не дождался бы никого.
    """
    conv_id = queued_pair.ids[0]
    body = (await act(client, tokens, "decline", conv_id, json={"reason": "не мой канал"})).json()
    assert body["declined"] is True
    assert body["conversation_id"] == str(conv_id)
    assert body["reason"] == "не мой канал"
    assert body["count"] == 1, "счётчик отказавшегося уменьшился ровно на один"

    assert str(conv_id) not in await queue_ids(client, tokens, "manager")
    assert str(conv_id) in await queue_ids(client, tokens, "admin")
    assert await count_for(client, tokens, "admin") == 2


async def test_decline_needs_no_body_and_does_not_break_the_counter(client, tokens, queued_pair):
    """Кнопка «Отклонить» шлёт запрос без тела — 400 на пустом клике недопустим."""
    conv_id = queued_pair.ids[0]
    body = (await act(client, tokens, "decline", conv_id)).json()
    assert body["reason"] is None
    assert body["already_declined"] is False
    assert body["count"] == 1

    # Повтор (двойной клик, вторая вкладка) — 200 с пометкой, а не ошибка и не
    # второе уменьшение: иначе бейдж уедет в минус на первом же двойном клике.
    again = (await act(client, tokens, "decline", conv_id)).json()
    assert again["already_declined"] is True
    assert again["count"] == 1
    assert await count_for(client, tokens, "manager") == 1


async def test_declined_conversation_can_still_be_claimed_by_a_colleague(
    client, tokens, queued_pair
):
    conv_id = queued_pair.ids[0]
    await act(client, tokens, "decline", conv_id, role="manager")
    await act(client, tokens, "claim", conv_id, role="admin")


async def test_declining_a_dialog_a_colleague_already_took_is_a_conflict(
    client, tokens, users_by_role, queued
):
    """Строка ушла из очереди раньше, чем нажали «Отклонить», — 409 с именем.

    Тихий 200 был бы хуже ошибки: оператор решил бы, что убрал диалог из своей
    очереди, а он и так там не лежит — и запись «отклонил» появилась бы у
    диалога, который уже кто-то ведёт.
    """
    await act(client, tokens, "claim", queued.conversation_id, role="admin")

    r = await client.post(_url("decline", queued.conversation_id), headers=auth(tokens, "manager"))
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "already_claimed"
    assert err["details"]["claimed_by"]["id"] == str(users_by_role["admin"].id)
    assert err["details"]["mine"] is False


async def test_when_everyone_declines_the_dialog_stays_and_is_marked(client, tokens, queued_pair):
    """Отказались все — клиент не должен пропасть.

    Диалог остаётся в очереди с пометкой `escalated`, а не исчезает: исчезнуть
    значило бы «никто не берёт и никто об этом не узнает». Операторов в юнит-
    стенде ровно двое (admin и manager — те, у кого messages:send).
    """
    conv_id = queued_pair.ids[0]
    first = (await act(client, tokens, "decline", conv_id, role="manager")).json()
    assert first["escalated_now"] is False
    assert first["escalated"] == 0  # брошенных в очереди пока нет

    last = (await act(client, tokens, "decline", conv_id, role="admin")).json()
    assert last["escalated_now"] is True

    # Из ЛИЧНЫХ очередей он ушёл у обоих, но из системы — нет.
    assert str(conv_id) not in await queue_ids(client, tokens, "manager")
    assert str(conv_id) not in await queue_ids(client, tokens, "admin")
    # Руководитель отказов не делал — у него диалог виден, и это ровно тот
    # человек, который должен его разобрать передачей (01 §5.5).
    assert str(conv_id) in await queue_ids(client, tokens, "head")
    head_badge = (await client.get("/api/v1/inbox/count", headers=auth(tokens, "head"))).json()
    assert head_badge == {"count": 2, "escalated": 1}


async def test_declining_by_mistake_can_be_taken_back(client, tokens, queued_pair):
    """Отказ обратим (UX-аудит, docs/17 §Т7).

    Отказ стал одним нажатием Ctrl+Backspace, значит промахнуться теперь
    легко, а последствие было необратимым: обращение навсегда уходило из
    очереди отказавшегося. Клиент при этом не брошен — коллегам диалог
    виден, — но конкретный оператор терял то, что собирался взять.
    """
    conv_id = queued_pair.ids[0]
    await act(client, tokens, "decline", conv_id)
    assert str(conv_id) not in await queue_ids(client, tokens, "manager")

    body = (await act(client, tokens, "decline/undo", conv_id)).json()
    assert body["declined"] is False
    assert body["count"] == 2, "диалог вернулся в мою очередь"
    assert str(conv_id) in await queue_ids(client, tokens, "manager")

    # Повторная отмена — не ошибка и не второй возврат (двойной клик, вторая вкладка).
    again = (await act(client, tokens, "decline/undo", conv_id)).json()
    assert again["count"] == 2
    assert str(conv_id) in await queue_ids(client, tokens, "manager")


async def test_taking_back_the_last_refusal_clears_nobody_takes_it(client, tokens, queued_pair):
    """Возврат последнего отказа снимает пометку «никто не берёт».

    Утверждение «отказались все» перестало быть правдой, а держать на диалоге
    неверную пометку хуже, чем не ставить её вовсе. Уже ушедшее админам
    уведомление, разумеется, не отзывается — но состояние диалога обязано
    соответствовать действительности.
    """
    conv_id = queued_pair.ids[0]
    await act(client, tokens, "decline", conv_id, role="manager")
    last = (await act(client, tokens, "decline", conv_id, role="admin")).json()
    assert last["escalated_now"] is True

    head_badge = (await client.get("/api/v1/inbox/count", headers=auth(tokens, "head"))).json()
    assert head_badge["escalated"] == 1

    await act(client, tokens, "decline/undo", conv_id, role="admin")

    head_after = (await client.get("/api/v1/inbox/count", headers=auth(tokens, "head"))).json()
    assert head_after["escalated"] == 0, "пометка снята — отказались уже не все"
    assert str(conv_id) in await queue_ids(client, tokens, "admin")


async def test_you_cannot_take_back_a_refusal_after_someone_took_the_dialog(
    client, tokens, queued_pair
):
    """Диалог мог уйти из очереди, пока висел тост: его приняли.

    Тогда возвращать нечего, и сказать об этом надо прямо, а не молча сделать
    вид, что получилось.
    """
    conv_id = queued_pair.ids[0]
    await act(client, tokens, "decline", conv_id, role="manager")
    await act(client, tokens, "claim", conv_id, role="admin")

    # Прямой запрос, а не `act`: тот ОБЯЗЫВАЕТ ответ быть 200, здесь же
    # проверяется именно отказ.
    r = await client.post(_url("decline/undo", conv_id), headers=auth(tokens, "manager"))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "already_claimed"


async def test_release_is_admin_only(client, tokens, queued):
    """ВЕРНУТЬ В ОЧЕРЕДЬ МОЖЕТ ТОЛЬКО АДМИНИСТРАТОР (решение владельца 28.08).

    Дословно: «сделай, чтобы вернуть в очередь мог только бот или
    администратор, у менеджеров эту функцию отключи и удали, чтобы её не было».

    ⚠ ЧТО БЫЛО. Ручка висела на `messages:send` — то есть была у каждого, кто
    умеет отвечать клиенту. Возврат снимает ответственного и отдаёт тринадцати
    диалог, с которым человек уже поговорил: клиент получает второго
    собеседника с нуля, а история разговора остаётся за прежним. Оператору для
    «я сейчас занят» есть «Отклонить» — оно про диалог, ЕЩЁ не начатый.

    ⚠ БОТА И СТОРОЖЕЙ ЭТО НЕ КАСАЕТСЯ: они зовут `inbox.return_to_queue` в
    сервисном слое, мимо HTTP и мимо прав. Правило запирает ручку, а не саму
    операцию, — иначе вместе с кнопкой встали бы передача от бота и оба
    сторожа возврата.
    """
    r = await client.post(_url("release", queued.conversation_id), headers=auth(tokens, "manager"))
    assert r.status_code == 403, r.text

    # Админ фильтр прав проходит: 409 здесь — конфликт состояния (диалог никем
    # не принят), а не отказ доступа.
    r = await client.post(_url("release", queued.conversation_id), headers=auth(tokens, "admin"))
    assert r.status_code not in (401, 403), r.text


async def test_release_returns_the_dialog_to_the_queue(client, tokens, queued):
    # Берёт и возвращает АДМИНИСТРАТОР: с 28.08 возврат — только его право
    # («у менеджеров эту функцию отключи»), а вернуть можно лишь то, что взял сам.
    conv_id = queued.conversation_id
    await act(client, tokens, "claim", conv_id, role="admin")
    assert await count_for(client, tokens, "admin") == 0

    body = (await act(client, tokens, "release", conv_id, role="admin")).json()
    assert body["conversation"]["id"] == str(conv_id)

    assert str(conv_id) in await queue_ids(client, tokens, "admin")
    assert await count_for(client, tokens, "admin") == 1


async def test_admin_can_pull_out_a_dialog_taken_by_someone_who_left(
    client, tokens, users_by_role, queued
):
    """Администратор возвращает в очередь и ЧУЖОЙ диалог — это решение.

    Довод записан в `inbox.release`: «запрет тут означал бы, что диалог
    уехавшего сотрудника не вытащить никак».

    ⚠ ЗДЕСЬ БЫЛА ПРОВЕРКА «вернуть может только хозяин» — с менеджером-коллегой
    в роли чужака. С 28.08 она проверяет несуществующий случай: право на возврат
    осталось ТОЛЬКО у администратора, а администратор замок хозяина проходит
    насквозь. То есть ветка `not_holder` по HTTP теперь недостижима, и тест,
    который делал вид, что достижима, зеленел бы на выдумке.

    Ветку в сервисе оставляем: она второй замок на случай, если право однажды
    выдадут кому-то ещё. А проверяем то, что правда сейчас.
    """
    conv_id = queued.conversation_id
    await act(client, tokens, "claim", conv_id, role="manager")

    r = await client.post(_url("release", conv_id), headers=auth(tokens, "admin"))
    assert r.status_code == 200, r.text
    assert str(conv_id) in await queue_ids(client, tokens, "admin")


async def test_manager_cannot_pull_out_a_colleagues_dialog(client, tokens, colleague, queued):
    """А менеджер — не может, и упирается именно в ПРАВО.

    Раньше он упирался в замок хозяина (`not_holder`), то есть свой диалог
    вернуть мог, а чужой нет. Теперь ни своего, ни чужого: «у менеджеров эту
    функцию отключи и удали, чтобы её не было».
    """
    conv_id = queued.conversation_id
    await act(client, tokens, "claim", conv_id, role="manager")

    r = await client.post(_url("release", conv_id), headers=colleague.headers)
    assert r.status_code == 403, r.text
    # И он никуда не уехал: чужая попытка очередь не тронула.
    assert str(conv_id) not in await queue_ids(client, tokens, "admin")


async def test_admin_can_return_a_dialog_of_an_operator_who_left(client, tokens, queued):
    """Диалог уехавшего сотрудника обязан быть вытаскиваемым.

    Администратор и так может передать его (01 §5.5); запрет на возврат
    означал бы, что диалог человека, у которого отобрали доступ, вернуть в
    общую очередь нельзя вовсе.
    """
    conv_id = queued.conversation_id
    await act(client, tokens, "claim", conv_id, role="manager")
    await act(client, tokens, "release", conv_id, role="admin")
    assert str(conv_id) in await queue_ids(client, tokens, "manager")


# --- события WebSocket (01 §11.3) --------------------------------------------


async def test_claim_publishes_inbox_claimed_with_enough_to_redraw_the_row(
    client, tokens, users_by_role, redis, queued
):
    """Кадр обязан хватать на перерисовку без запроса.

    Тринадцать вкладок, каждая из которых после чужого принятия идёт за
    `GET /conversations`, — это тринадцать запросов на каждый принятый
    диалог. Поэтому в кадре и id, и кто принял, и патч строки.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await act(client, tokens, "claim", queued.conversation_id)

    by_type = {e["type"]: e for e in await drain_events(pubsub)}
    assert INBOX_CLAIMED in by_type, f"события {INBOX_CLAIMED} нет: {sorted(by_type)}"
    evt = by_type[INBOX_CLAIMED]
    assert set(evt) == {"type", "ts", "data"}, "конверт кадра — {type, ts, data} (01 §11.2)"

    data = evt["data"]
    assert data["conversation_id"] == str(queued.conversation_id)
    # `claimed_by` — то же слово, что в колонке и в details ошибки 409.
    assert data["claimed_by"]["id"] == str(users_by_role["manager"].id)
    assert data["claimed_by"]["full_name"]
    assert data["claimed_at"]
    patch = data["conversation_patch"]
    assert patch["assignee"]["id"] == str(users_by_role["manager"].id)
    assert patch["status"] == "in_progress"
    # Ради этого поля вся 7.1 и делалась: строка обязана уйти из очереди у
    # остальных двенадцати, а не остаться «свободной» до перезагрузки.
    assert patch["in_inbox"] is False
    # Абсолютного счётчика в широковещательном кадре быть не должно: у
    # отказавшегося оператора очередь своя, одно число на всех — враньё.
    assert "count" not in data

    # Компания кадру та же, что у назначения (01 §11.3): системная запись в
    # открытой ленте и починка строки списка у тех, кто очередь не смотрит.
    assert "message:new" in by_type
    assert "Диалог принят" in by_type["message:new"]["data"]["message"]["body"]
    assert by_type["conversation:updated"]["data"]["patch"] == patch


async def test_release_publishes_the_whole_conversation(client, tokens, redis, queued):
    """`inbox:released` несёт диалог целиком.

    Патча тут мало: у оператора, подключившегося после принятия, этой строки
    нет вообще — вставлять в список нечего.
    """
    conv_id = queued.conversation_id
    await act(client, tokens, "claim", conv_id, role="admin")

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await act(client, tokens, "release", conv_id, role="admin")

    by_type = {e["type"]: e for e in await drain_events(pubsub)}
    assert INBOX_RELEASED in by_type, f"события {INBOX_RELEASED} нет: {sorted(by_type)}"
    data = by_type[INBOX_RELEASED]["data"]
    assert data["conversation"]["id"] == str(conv_id)
    assert data["conversation"]["client"]["name"] == "Иван Петров"
    assert data["released_by"]["full_name"]
    assert data["conversation_patch"]["in_inbox"] is True
    assert data["conversation_patch"]["assignee"] is None


async def test_decline_frame_is_addressed_to_its_author_only(
    client, tokens, users_by_role, redis, queued_pair
):
    """Отказ синхронизирует ВТОРУЮ вкладку того же человека, а не команду."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await act(client, tokens, "decline", queued_pair.ids[0])

    frames = await drain_events(pubsub)
    by_type = {e["type"]: e for e in frames}
    assert INBOX_CLAIMED not in by_type, "отказ не занимает диалог — кадра о занятии быть не может"
    assert INBOX_RELEASED not in by_type, "диалог и не покидал очередь остальных"

    assert INBOX_DECLINED in by_type, f"события {INBOX_DECLINED} нет: {sorted(by_type)}"
    evt = by_type[INBOX_DECLINED]
    assert evt["meta"]["only_user"] == str(users_by_role["manager"].id)
    assert evt["data"]["count"] == 1  # адресный кадр — абсолютное число честно
    assert evt["data"]["escalated"] is False
    assert evt["data"]["declined_by"]["id"] == str(users_by_role["manager"].id)

    # А вот системная запись — всем: коллеги должны видеть, кто отказался.
    assert "Диалог отклонён" in by_type["message:new"]["data"]["message"]["body"]
    assert by_type["message:new"].get("meta") is None


async def test_the_frame_and_the_answer_agree_on_what_escalation_means(
    client, tokens, redis, queued_pair
):
    """Слово `escalated` живёт в системе в двух смыслах — замок на их стыке.

    В кадре о диалоге и в строке очереди `escalated` — ФЛАГ («этого не берёт
    никто»), в счётчиках (`InboxCountOut`) — ЧИСЛО («сколько таких в очереди»).
    В теле ответа рядом стоят оба, поэтому флаг там зовётся `escalated_now`.
    Пара имён держится на договорённости, а договорённость без теста живёт до
    первого рефакторинга: переименуют одну сторону — фронт молча начнёт красить
    строку по счётчику чужих брошенных диалогов.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    body = (await act(client, tokens, "decline", queued_pair.ids[0])).json()
    frame = next(e for e in await drain_events(pubsub) if e["type"] == INBOX_DECLINED)["data"]

    # Один и тот же факт под двумя именами — и оба на месте.
    assert frame["escalated"] == body["escalated_now"]
    assert isinstance(frame["escalated"], bool), "в кадре о диалоге `escalated` — флаг"
    # `bool` — подкласс `int`, поэтому «число» проверяем как «не флаг».
    assert not isinstance(body["escalated"], bool), "в счётчиках `escalated` — число"

    # Счётчик очереди — тот же, что в ответе: вторая вкладка не ходит за ним.
    assert frame["count"] == body["count"]
    # А счётчика брошенных в кадре нет: от своего же отказа он не меняется.
    assert "escalated_count" not in frame


# --- персонализация кадров в Hub (08 §5.3) -----------------------------------


class StubWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:  # pragma: no cover — не нужен
        pass


def _attach(hub: Hub, role: str, user_id: uuid.UUID | None = None):
    ws = StubWS()
    user = SimpleNamespace(id=user_id or uuid.uuid4(), full_name=role, role=role)
    return ws, hub.attach(ws, user)


@pytest.mark.parametrize("role", ALL_ROLES)
@pytest.mark.parametrize("event_type", (INBOX_NEW, INBOX_RELEASED))
async def test_queue_frames_carry_can_claim_per_recipient(redis, role, event_type):
    """Очередь видят все, кнопку «Принять» — операторы.

    Флаг считается по матрице прав, а не сравнением роли со строкой: новая
    роль-оператор получит кнопку сама, без правки хаба.
    """
    hub = Hub(redis)
    ws, _ = _attach(hub, role)
    await hub.dispatch(
        {"type": event_type, "ts": "2026-08-06T09:00:00.000Z", "data": {"conversation_id": "c1"}}
    )
    assert len(ws.sent) == 1, f"кадр {event_type} не доехал до роли {role}"
    assert ws.sent[0]["data"]["can_claim"] is can_claim(role)
    assert ws.sent[0]["data"]["can_claim"] is (role in OPERATORS)


async def test_inbox_new_carries_the_whole_row(client, tokens, redis, queued):
    """Кадр «встал в очередь» публикует не ручка, а входящий конвейер.

    Проверяем сам публикатор на настоящей строке очереди: у оператора,
    который подключился только что, этого диалога нет вовсе — патчем его в
    список не вставить, поэтому объект едет целиком.
    """
    row = (await client.get("/api/v1/conversations?tab=inbox", headers=auth(tokens))).json()[
        "items"
    ][0]

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await publish_inbox_new(redis, row)

    frames = await drain_events(pubsub)
    assert [f["type"] for f in frames] == [INBOX_NEW]
    data = frames[0]["data"]
    assert data["conversation_id"] == str(queued.conversation_id)
    assert data["conversation"]["client"]["name"] == "Иван Петров"
    assert data["conversation"]["waiting_seconds"] is not None


async def test_claimed_frame_tells_the_claimer_from_everyone_else(redis):
    hub = Hub(redis)
    me = uuid.uuid4()
    mine_ws, _ = _attach(hub, "manager", me)
    other_ws, _ = _attach(hub, "manager")

    await hub.dispatch(
        {
            "type": INBOX_CLAIMED,
            "ts": "2026-08-06T09:00:00.000Z",
            "data": {"conversation_id": "c1", "claimed_by": {"id": str(me), "full_name": "Я"}},
        }
    )
    assert mine_ws.sent[0]["data"]["is_mine"] is True
    assert other_ws.sent[0]["data"]["is_mine"] is False


async def test_declined_frame_never_leaks_to_a_colleague(redis):
    """`only_user` уже умеет хаб — проверяем, что новый тип им пользуется."""
    hub = Hub(redis)
    me = uuid.uuid4()
    mine_ws, _ = _attach(hub, "manager", me)
    other_ws, _ = _attach(hub, "manager")

    await hub.dispatch(
        {
            "type": INBOX_DECLINED,
            "ts": "2026-08-06T09:00:00.000Z",
            "data": {"conversation_id": "c1", "count": 3},
            "meta": {"only_user": str(me), "audience": None, "exclude_user": None},
        }
    )
    assert len(mine_ws.sent) == 1
    assert other_ws.sent == []


# --- совместимость: старые вкладки не должны пострадать ----------------------


@pytest.mark.parametrize("tab", ("my", "all", "new", "closed"))
async def test_existing_tabs_keep_working(client, tokens, queued, tab):
    r = await client.get(f"/api/v1/conversations?tab={tab}", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert "items" in r.json() and "page" in r.json()


async def test_unknown_tab_still_gives_a_readable_400(client, tokens):
    r = await client.get("/api/v1/conversations?tab=zzz", headers=auth(tokens))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "param,query",
    [
        ("assignee_id", f"assignee_id={uuid.uuid4()}"),
        ("unassigned", "unassigned=true"),
        ("q", "q=экран"),
        ("unread_only", "unread_only=true"),
        ("tag", "tag=негатив"),
    ],
)
async def test_unsupported_filters_are_refused_on_the_queue(client, tokens, param, query):
    """Фильтр, которого у очереди нет, — отказ, а не тихое игнорирование.

    Проигнорировать значит показать БОЛЬШЕ, чем человек попросил: выбрал в
    «Менеджер ▾» Анну — увидел полную очередь и решил, что это очередь Анны.
    """
    r = await client.get(f"/api/v1/conversations?tab=inbox&{query}", headers=auth(tokens))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert [f["field"] for f in r.json()["error"]["details"]["fields"]] == [param]


async def test_updated_since_is_tolerated_on_the_queue(client, tokens, queued):
    """`updated_since` не отвергаем: это догон после reconnect'а (01 §11.7).

    Очередь его пока не сужает — это стоит лишнего трафика, но не врёт;
    отказ же сломал бы догон вкладки «Входящие» на живом фронте.
    """
    past = (T0 - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    r = await client.get(
        f"/api/v1/conversations?tab=inbox&updated_since={past}", headers=auth(tokens)
    )
    assert r.status_code == 200, r.text
    assert str(queued.conversation_id) in [i["id"] for i in r.json()["items"]]


async def test_queue_shares_the_common_filters(client, tokens, queued, queued_pair):
    """Фильтры и пагинация у очереди общие — второй механизм не заведён."""
    by_account = await client.get(
        f"/api/v1/conversations?tab=inbox&account_id={queued.account.id}", headers=auth(tokens)
    )
    assert by_account.status_code == 200, by_account.text
    assert [i["id"] for i in by_account.json()["items"]] == [str(queued.conversation_id)]

    paged = await client.get("/api/v1/conversations?tab=inbox&limit=1", headers=auth(tokens))
    assert paged.status_code == 200, paged.text
    assert len(paged.json()["items"]) == 1
    # Тот же конверт `page`, что у остальных вкладок: второй пагинации нет.
    assert paged.json()["page"] == {"limit": 1, "offset": 0, "total": 3}


async def test_queue_rows_have_the_waiting_fields_the_screen_needs(client, tokens, queued):
    """Строка очереди — обычный элемент списка плюс ожидание.

    «Ждёт 43 минуты» — то, по чему оператор решает, что брать первым; без
    этих полей вкладка «Входящие» ничем не отличается от «Новых».
    """
    r = await client.get("/api/v1/conversations?tab=inbox", headers=auth(tokens))
    row = next(i for i in r.json()["items"] if i["id"] == str(queued.conversation_id))
    assert row["assignee"] is None, "в очереди ответственного нет по определению"
    assert row["client"]["name"] == "Иван Петров"  # форма общая с 01 §5.1
    assert row["offered_at"] and isinstance(row["waiting_seconds"], int)
    assert row["waiting_human"] and row["escalated"] is False
    assert row["declined_count"] == 0
