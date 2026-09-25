"""Назначение операторов на каналы Авито — программный интерфейс (15 §2.2, 7.2).

Блок закрывает беду масштаба: девять каналов, тринадцать операторов, и до сих
пор очередь «Входящие» показывала всем всё. Здесь проверяется ровно то, из-за
чего экран назначения появился в Jivo и переехал сюда:

* **правило совместимости** — канал без назначенных операторов доступен ВСЕМ,
  а не никому (`test_channel_without_operators_is_open_to_everyone` и соседи).
  Если бы пустой набор означал «никому», первое же включение фильтрации
  оставило бы девять каналов без очереди, и обращения повисли бы молча — без
  ошибки, без исключения, просто ничего не приходит;
* **набор меняет только администратор** — состав решает, кому попадут
  обращения и чью переписку человек увидит; руководителю оставлено чтение,
  чтобы разбирать заторы;
* **отказ вместо тихого игнорирования** — несуществующий id, отключённый
  сотрудник и роль без права отвечать клиентам дают 422 с машиночитаемым
  `reason`. Молчаливое отбрасывание выглядело бы для админа как «сохранено»,
  а на канале не оказалось бы половины смены;
* **журнал** — кто, какой канал, кого добавил и кого убрал: без этого вопрос
  смены «почему я перестал видеть канал» остаётся без ответа.

Матрица RBAC этих ручек живёт здесь, а не в tests/unit/test_rbac.py: у путей
плейсхолдеры `{account_id}`/`{user_id}`, и ALLOW-ветка общей матрицы упёрлась
бы в 422 на `account_id="x"`, то есть проверяла бы разбор пути вместо отказа
доступа (та же причина, что у `/assign` и ручек очереди — см.
COVERED_ELSEWHERE в test_rbac.py).
"""

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.security import create_access_token
from app.models import AuditLog, AvitoAccount, User

ALL_ROLES = ("admin", "head", "manager", "observer")
# Роли с правом `messages:send` (01 §12) — только их можно назначить на канал.
OPERATOR_ROLES = ("admin", "manager")

OPERATORS_PATH = "/api/v1/avito-accounts/{account_id}/operators"
MY_CHANNELS_PATH = "/api/v1/me/channels"
USER_CHANNELS_PATH = "/api/v1/users/{user_id}/accounts"


def auth(tokens: dict[str, str], role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


def token_for(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=str(user.id), role=user.role)}"}


@pytest.fixture
async def channel(make_avito_account: Callable[..., Awaitable[AvitoAccount]]) -> AvitoAccount:
    """Канал заказчика — с настоящим именем из Jivo (15 §2.2)."""
    return await make_avito_account(770100701, title="! Парт - 7 / Ист - В43 МНЧ !")


@pytest.fixture
async def second_channel(
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> AvitoAccount:
    return await make_avito_account(770100702, title="Парт - 723 БЕЛЫЙ")


@pytest.fixture
async def crew(make_user: Callable[..., Awaitable[User]]) -> SimpleNamespace:
    """Трое операторов сверх ролевых фикстур — набор должен быть заменяемым."""
    return SimpleNamespace(
        first=await make_user("op1@leadchat.test", role="manager", full_name="Анна Быкова"),
        second=await make_user("op2@leadchat.test", role="manager", full_name="Борис Гаврилов"),
        third=await make_user("op3@leadchat.test", role="manager", full_name="Вера Демина"),
    )


async def put_operators(client, channel: AvitoAccount, ids, tokens, role: str = "admin"):
    return await client.put(
        OPERATORS_PATH.format(account_id=channel.id),
        headers=auth(tokens, role),
        json={"operator_ids": [str(i) for i in ids]},
    )


async def get_operators(client, channel: AvitoAccount, tokens, role: str = "admin"):
    return await client.get(
        OPERATORS_PATH.format(account_id=channel.id), headers=auth(tokens, role)
    )


async def assigned(client, channel: AvitoAccount, tokens) -> set[str]:
    r = await get_operators(client, channel, tokens)
    assert r.status_code == 200, r.text
    return set(r.json()["assigned_ids"])


# --- монтирование ------------------------------------------------------------


def test_the_assignment_screen_is_mounted_in_the_application(app: FastAPI) -> None:
    """Страж вместо моста: пути обязаны быть в самом приложении.

    Роутер аккаунтов подключается в `app/main.py` динамическим импортом с
    молчаливым `except ModuleNotFoundError` — зона писалась параллельно.
    Опечатка в имени модуля не уронила бы ничего: экран назначения просто
    исчез бы из API, а тесты, монтирующие роутер сами, остались бы зелёными.
    """
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/avito-accounts/{account_id}/operators",
        "/api/v1/me/channels",
        "/api/v1/users/{user_id}/accounts",
    ):
        assert path in paths, f"{path} нет в приложении — ручка смонтирована только в тестах"
    assert set(paths["/api/v1/avito-accounts/{account_id}/operators"]) >= {"get", "put"}


# --- права (01 §12, DESIGN §5.1) ---------------------------------------------


@pytest.mark.parametrize(
    "role,expected", [("admin", 200), ("head", 200), ("manager", 403), ("observer", 403)]
)
async def test_reading_the_set_is_for_admin_and_head(client, tokens, channel, role, expected):
    """`accounts:read` — admin и head. Руководителю чтение нужно, чтобы понять,
    почему обращение висит: он видит, кому канал вообще виден."""
    r = await get_operators(client, channel, tokens, role)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize(
    "role,expected", [("admin", 200), ("head", 403), ("manager", 403), ("observer", 403)]
)
async def test_changing_the_set_is_admin_only(client, tokens, channel, crew, role, expected):
    """`accounts:manage` есть только у admin: назначение на канал — это раздача
    доступа к чужой переписке, а не настройка интерфейса."""
    r = await put_operators(client, channel, [crew.first.id], tokens, role)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("method", ["get", "put"])
async def test_anonymous_is_rejected(client, channel, method):
    r = await client.request(
        method, OPERATORS_PATH.format(account_id=channel.id), json={"operator_ids": []}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("role", ALL_ROLES)
async def test_every_role_sees_its_own_channels(client, tokens, channel, role):
    """Свои каналы — любой авторизованной роли.

    Отдельного права здесь быть не должно: `accounts:read` есть у админа и
    руководителя, а вопрос «почему мне приходят одни обращения и не приходят
    другие» задаёт как раз менеджер.
    """
    r = await client.get(MY_CHANNELS_PATH, headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


async def test_someone_elses_channels_need_accounts_read(client, tokens, users_by_role, crew):
    """Чужой список — карта доступа к переписке: только admin и head."""
    stranger = USER_CHANNELS_PATH.format(user_id=crew.first.id)
    denied = await client.get(stranger, headers=auth(tokens, "manager"))
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "forbidden"

    for role in ("admin", "head"):
        allowed = await client.get(stranger, headers=auth(tokens, role))
        assert allowed.status_code == 200, allowed.text

    # ...но про СЕБЯ ручка отвечает кому угодно: 403 на вопрос человека о самом
    # себе заставил бы фронт выбирать путь по роли, а не по смыслу действия.
    me = users_by_role["manager"]
    own = await client.get(
        USER_CHANNELS_PATH.format(user_id=me.id), headers=auth(tokens, "manager")
    )
    assert own.status_code == 200, own.text


async def test_channels_of_a_ghost_user_is_404(client, tokens):
    r = await client.get(USER_CHANNELS_PATH.format(user_id=uuid.uuid4()), headers=auth(tokens))
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("path", [MY_CHANNELS_PATH, USER_CHANNELS_PATH])
async def test_channel_lists_reject_anonymous(client, users_by_role, path):
    r = await client.get(path.format(user_id=users_by_role["manager"].id))
    assert r.status_code == 401


async def test_unknown_channel_is_404(client, tokens):
    r = await client.get(OPERATORS_PATH.format(account_id=uuid.uuid4()), headers=auth(tokens))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"


# --- правило совместимости: канал без операторов доступен ВСЕМ ---------------


async def test_channel_without_operators_is_open_to_everyone(
    client, tokens, channel, users_by_role
):
    """Ни одного назначенного — канал общий, а не ничей.

    Это не деталь ответа, а условие переезда: девять каналов заказчика в момент
    включения фильтрации не назначены никому. Если бы пустой набор означал
    «никому», очередь в тот же миг опустела бы у всех тринадцати операторов, и
    обращения повисли бы без единой ошибки в логах.
    """
    body = (await get_operators(client, channel, tokens)).json()
    assert body["assigned_ids"] == []

    mine = await client.get(MY_CHANNELS_PATH, headers=auth(tokens, "manager"))
    assert mine.status_code == 200, mine.text
    row = next(i for i in mine.json()["items"] if i["id"] == str(channel.id))
    # «open», а не «assigned»: человек видит разницу между «мой канал» и
    # «пока ничей» и не идёт с этим вопросом к администратору.
    assert row["access"] == "open"

    accounts = await client.get("/api/v1/avito-accounts", headers=auth(tokens))
    card = next(i for i in accounts.json()["items"] if i["id"] == str(channel.id))
    assert card["operators"] == {"count": 0, "preview": []}


async def test_emptying_the_set_reopens_the_channel(client, tokens, channel, crew):
    """Снятие всех галочек возвращает канал в общий доступ, а не запирает его."""
    assert (await put_operators(client, channel, [crew.first.id], tokens)).status_code == 200
    assert await assigned(client, channel, tokens) == {str(crew.first.id)}

    reopened = await put_operators(client, channel, [], tokens)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["operators"]["count"] == 0
    assert await assigned(client, channel, tokens) == set()

    # ...и канал снова виден постороннему оператору как общий
    mine = await client.get(MY_CHANNELS_PATH, headers=auth(tokens, "manager"))
    assert {i["id"]: i["access"] for i in mine.json()["items"]}[str(channel.id)] == "open"


async def test_assigned_channel_leaves_other_operators_lists(
    client, tokens, channel, second_channel, crew
):
    """Назначенный канал уходит из списка остальных, общий остаётся у всех."""
    assert (await put_operators(client, channel, [crew.first.id], tokens)).status_code == 200

    mine = await client.get(MY_CHANNELS_PATH, headers=token_for(crew.first))
    assert mine.status_code == 200, mine.text
    access = {i["id"]: i["access"] for i in mine.json()["items"]}
    assert access[str(channel.id)] == "assigned"  # свой канал
    assert access[str(second_channel.id)] == "open"  # общий, никем не занят

    other = await client.get(MY_CHANNELS_PATH, headers=token_for(crew.second))
    assert other.status_code == 200, other.text
    ids = {i["id"] for i in other.json()["items"]}
    assert str(channel.id) not in ids, "чужой канал виден постороннему оператору"
    assert str(second_channel.id) in ids


async def test_a_channel_left_with_only_a_fired_operator_becomes_common_again(
    client, tokens, channel, crew, db_sessionmaker
):
    """Канал, чей единственный оператор уволен, снова общий.

    Иначе строка увольнения тихо запирала бы канал: назначенный есть, работать
    некому — обращения дождались бы только эскалации «отказались все».
    """
    assert (await put_operators(client, channel, [crew.first.id], tokens)).status_code == 200
    async with db_sessionmaker() as s:
        row = await s.get(User, crew.first.id)
        assert row is not None
        row.is_active = False
        await s.commit()

    accounts = await client.get("/api/v1/avito-accounts", headers=auth(tokens))
    card = next(i for i in accounts.json()["items"] if i["id"] == str(channel.id))
    assert card["operators"]["count"] == 0  # живых операторов нет — канал общий

    mine = await client.get(MY_CHANNELS_PATH, headers=token_for(crew.second))
    assert {i["id"]: i["access"] for i in mine.json()["items"]}[str(channel.id)] == "open"

    # галочка при этом на месте: админ снимает её осознанно, а не гадает,
    # почему в списке назначенных пусто, а канал числится за кем-то
    assert await assigned(client, channel, tokens) == {str(crew.first.id)}


# --- полная замена набора ----------------------------------------------------


async def test_put_replaces_the_whole_set(client, tokens, channel, crew):
    """Тело — итоговый список: кого в нём нет, тот с канала снят."""
    first = await put_operators(client, channel, [crew.first.id, crew.second.id], tokens)
    assert first.status_code == 200, first.text
    assert first.json()["operators"]["count"] == 2
    assert await assigned(client, channel, tokens) == {str(crew.first.id), str(crew.second.id)}

    second = await put_operators(client, channel, [crew.second.id, crew.third.id], tokens)
    assert second.status_code == 200, second.text
    assert await assigned(client, channel, tokens) == {str(crew.second.id), str(crew.third.id)}
    # ...и первого канал больше не показывает
    mine = await client.get(MY_CHANNELS_PATH, headers=token_for(crew.first))
    assert str(channel.id) not in {i["id"] for i in mine.json()["items"]}


async def test_saved_summary_carries_names_for_the_card(client, tokens, channel, crew):
    """Ответ PUT перерисовывает карточку без второго запроса: имена — в своде."""
    r = await put_operators(client, channel, [crew.first.id, crew.second.id], tokens)
    assert r.status_code == 200, r.text
    preview = r.json()["operators"]["preview"]
    assert {p["full_name"] for p in preview} == {"Анна Быкова", "Борис Гаврилов"}


async def test_duplicates_in_the_body_collapse(client, tokens, channel, crew):
    """Пять одинаковых галочек — это один оператор, а не пять."""
    r = await put_operators(client, channel, [crew.first.id] * 5, tokens)
    assert r.status_code == 200, r.text
    assert r.json()["operators"]["count"] == 1
    assert await assigned(client, channel, tokens) == {str(crew.first.id)}


async def test_the_whole_staff_is_listed_with_a_grey_mark(client, tokens, channel, users_by_role):
    """Как в Jivo: несотрудники не спрятаны, а показаны серым.

    Спрятать руководителя и наблюдателя было бы хуже отказа: админ решил бы,
    что человека нет в системе, и пошёл бы заводить второго.
    """
    body = (await get_operators(client, channel, tokens)).json()
    by_id = {i["id"]: i for i in body["candidates"]}
    for role in ALL_ROLES:
        row = by_id[str(users_by_role[role].id)]
        assert row["can_be_operator"] is (role in OPERATOR_ROLES), row
        if not row["can_be_operator"]:
            # у серой строки есть объяснение — иначе админ не поймёт, что делать
            assert row["reason"]


# --- валидация набора (та же форма отказа, что у передачи диалога, 01 §5.5) ---


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_a_non_operator_cannot_be_assigned(client, tokens, channel, users_by_role, role):
    """Руководителя и наблюдателя назначить нельзя — они не отвечают клиентам.

    Критерий — право `messages:send`, а не список ролей: новая роль без него не
    должна молча получить чужие обращения в очередь. Отказ внятный: в тексте
    имя человека, чтобы админ не искал его по id среди тринадцати.
    """
    r = await put_operators(client, channel, [users_by_role[role].id], tokens)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["details"]["reason"] == "cannot_answer_clients"
    assert error["details"]["user_ids"] == [str(users_by_role[role].id)]
    assert users_by_role[role].full_name in error["message"]
    assert "не отвечают клиентам" in error["message"]

    assert await assigned(client, channel, tokens) == set()


async def test_unknown_id_is_rejected_not_ignored(client, tokens, channel, crew):
    """Несуществующий id — 422, а не тихое отбрасывание.

    Молча пропустить его значит показать админу «сохранено» и оставить канал
    без половины смены; отказ целиком — единственная честная реакция.
    """
    ghost = uuid.uuid4()
    await put_operators(client, channel, [crew.first.id], tokens)

    r = await put_operators(client, channel, [crew.second.id, ghost], tokens)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "user_not_found"
    assert r.json()["error"]["details"]["user_ids"] == [str(ghost)]

    # набор не тронут: отказ атомарен, частично сохранённого состояния нет
    assert await assigned(client, channel, tokens) == {str(crew.first.id)}


async def test_deactivated_employee_cannot_be_assigned(client, tokens, channel, make_user):
    fired = await make_user("fired@leadchat.test", role="manager", is_active=False)
    r = await put_operators(client, channel, [fired.id], tokens)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "user_inactive"
    assert await assigned(client, channel, tokens) == set()


# --- число операторов в списке аккаунтов (экран 11 §4.1) ---------------------


async def test_accounts_list_shows_the_operator_count(
    client, tokens, channel, second_channel, crew
):
    """На экране аккаунтов состав виден сразу, без захода внутрь канала."""
    assert (
        await put_operators(client, channel, [crew.first.id, crew.second.id], tokens)
    ).status_code == 200

    r = await client.get("/api/v1/avito-accounts", headers=auth(tokens))
    assert r.status_code == 200, r.text
    cards = {i["id"]: i["operators"] for i in r.json()["items"]}
    assert cards[str(channel.id)]["count"] == 2
    assert {p["full_name"] for p in cards[str(channel.id)]["preview"]} == {
        "Анна Быкова",
        "Борис Гаврилов",
    }
    assert cards[str(second_channel.id)]["count"] == 0  # общий канал — назначенных нет


# --- журнал (01 §9.7) --------------------------------------------------------


async def test_audit_records_who_added_and_removed(
    client, tokens, users_by_role, channel, crew, db_sessionmaker
):
    """«Кто, какой канал, кого добавил и кого убрал» — иначе вопрос смены
    «почему я перестал видеть канал» остаётся без ответа."""
    await put_operators(client, channel, [crew.first.id, crew.second.id], tokens)
    await put_operators(client, channel, [crew.second.id, crew.third.id], tokens)

    async with db_sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "account.operators_changed")
                    .order_by(AuditLog.id)
                )
            ).scalars()
        )
    assert len(rows) == 2
    last = rows[-1]
    assert last.user_id == users_by_role["admin"].id
    assert last.entity == "avito_account"
    assert last.entity_id == str(channel.id)
    assert last.details["added"] == [str(crew.third.id)]
    assert last.details["removed"] == [str(crew.first.id)]
    assert set(last.details["operator_ids"]) == {str(crew.second.id), str(crew.third.id)}
    # имя канала в деталях: в журнале «! Парт - 7 …» читается, uuid — нет
    assert last.details["title"] == channel.title


async def test_repeated_put_writes_nothing_to_the_journal(
    client, tokens, channel, crew, db_sessionmaker
):
    """«Открыл, посмотрел, нажал Сохранить» — не событие смены."""
    await put_operators(client, channel, [crew.first.id], tokens)
    again = await put_operators(client, channel, [crew.first.id], tokens)
    assert again.status_code == 200, again.text
    assert again.json()["operators"]["count"] == 1

    async with db_sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "account.operators_changed")
                )
            ).scalars()
        )
    assert len(rows) == 1, "неизменившийся PUT не должен плодить записи журнала"


def test_the_audit_action_is_registered():
    """Действие обязано стоять в реестре (06 §0.3): без названия журнал
    показал бы сырой код и сломал колонку «действие» экрана 11 §4.2."""
    from app.services.audit import AUDIT_ACTIONS

    assert "account.operators_changed" in AUDIT_ACTIONS
