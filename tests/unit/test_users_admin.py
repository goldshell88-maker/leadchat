"""Управление сотрудниками — 01 §3.1–3.5, экран «Команда» 11 §4.2.

Блокер приёмки Б4 состоял из двух половин, и здесь заперты обе:

* **ручек не было вовсе** — сотрудник заводился командой в консоли;
* **отключение не доводилось до конца** — строка в БД менялась, а живые
  сессии человека продолжали работать. Проверяем все три замка сразу:
  refresh-цепочка, денилист access-токенов и `control:revoked` в шину.
  Пропуск любого делает отключение косметическим.

Сквозной сценарий с настоящим сокетом — `tests/integration/test_session_revoke.py`
(там нужен живой Pub/Sub и хаб); здесь — контракт ручек и RBAC.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import ApiError
from app.core.security import create_access_token
from app.models import AuditLog, User
from app.services import users as users_service
from app.services.sessions import revoked_key
from tests.unit.conftest import DEFAULT_PASSWORD, drain_events

ROLES = ("admin", "head", "manager", "observer")

# Все ручки управления людьми: право `users:manage`, только admin (01 §12–13).
MANAGE_ENDPOINTS = (
    ("GET", "/api/v1/users", None),
    ("POST", "/api/v1/users", {"email": "x@leadchat.test", "full_name": "X", "role": "manager"}),
    (
        "POST",
        "/api/v1/users/invite",
        {"email": "y@leadchat.test", "full_name": "Y", "role": "manager"},
    ),
    ("PATCH", "/api/v1/users/{target}", {"full_name": "Новое имя"}),
    ("POST", "/api/v1/users/{target}/invite", None),
    ("POST", "/api/v1/users/{target}/resend-invite", None),
    ("POST", "/api/v1/users/{target}/reset-password", None),
    ("POST", "/api/v1/users/{target}/deactivate", None),
    ("POST", "/api/v1/users/{target}/activate", None),
)


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def _audit_actions(db_sessionmaker, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as db:
        rows = await db.execute(select(AuditLog).where(AuditLog.action == action))
        return list(rows.scalars())


def _token_from(url: str) -> str:
    return url.rsplit("/", 1)[-1]


@pytest.fixture
async def target(make_user):
    """Обычный сотрудник, над которым ставятся опыты."""
    return await make_user("target@leadchat.test", role="manager", full_name="Пётр Ковалёв")


# --- RBAC: четыре роли на каждую ручку (01 §12–13) ---------------------------


@pytest.mark.parametrize(
    "method,path,body", MANAGE_ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in MANAGE_ENDPOINTS]
)
@pytest.mark.parametrize("role", [r for r in ROLES if r != "admin"])
async def test_manage_endpoints_are_admin_only(client, tokens, target, method, path, body, role):
    """head/manager/observer — ровно 403, а не 401 и не 404.

    404 вместо 403 был бы утечкой: «такого сотрудника нет» и «вам не положено»
    — разные ответы, и второй знать не обязательно (01 §1.3).
    """
    url = path.format(target=target.id)
    r = await client.request(method, url, headers=auth(tokens, role), json=body)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "method,path,body", MANAGE_ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in MANAGE_ENDPOINTS]
)
async def test_manage_endpoints_reject_anonymous(client, target, method, path, body):
    url = path.format(target=target.id)
    r = await client.request(method, url, json=body)
    assert r.status_code == 401, r.text


@pytest.mark.parametrize(
    "method,path,body", MANAGE_ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in MANAGE_ENDPOINTS]
)
async def test_admin_passes_rbac_on_every_endpoint(client, tokens, target, method, path, body):
    """Админа матрица пропускает везде: отказ, если он есть, — уже прикладной."""
    url = path.format(target=target.id)
    r = await client.request(method, url, headers=auth(tokens, "admin"), json=body)
    assert r.status_code not in (401, 403), r.text


# --- список (01 §3.1) --------------------------------------------------------


async def test_list_returns_page_with_flags(client, tokens, users_by_role, make_user):
    invited = await make_user("invited@leadchat.test", role="manager", invited=True)
    r = await client.get("/api/v1/users", headers=auth(tokens, "admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page"]["total"] == 5  # четыре роли + приглашённый
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(invited.id)]["invite_pending"] is True
    assert by_id[str(users_by_role["manager"].id)]["invite_pending"] is False
    assert all(item["is_online"] is False for item in body["items"])  # никто не подключён


async def test_list_filters_by_role_and_activity(client, tokens, users_by_role, make_user):
    fired = await make_user("fired@leadchat.test", role="manager", is_active=False)

    only_managers = await client.get(
        "/api/v1/users", headers=auth(tokens, "admin"), params={"role": "manager"}
    )
    assert {i["id"] for i in only_managers.json()["items"]} == {
        str(users_by_role["manager"].id),
        str(fired.id),
    }

    active = await client.get(
        "/api/v1/users", headers=auth(tokens, "admin"), params={"is_active": "false"}
    )
    assert [i["id"] for i in active.json()["items"]] == [str(fired.id)]

    found = await client.get("/api/v1/users", headers=auth(tokens, "admin"), params={"q": "fired@"})
    assert [i["id"] for i in found.json()["items"]] == [str(fired.id)]


async def test_service_user_is_hidden_from_the_team_screen(client, tokens, make_user):
    """Робот smoke-регрессии — не сотрудник. Служебность — КОЛОНКА, не домен.

    ⚠ ТЕСТ ПЕРЕУЧЕН 15 АВГУСТА, и это часть починки, а не подгонка. Раньше он
    заводил робота ГОЛЫМ ДОМЕНОМ `.local` и требовал его спрятать — то есть
    закреплял ровно то правило, которое признали неверным ещё 12 августа:
    `.local` — обычный внутренний домен, и на нём живут настоящие люди,
    которых экран «Команда» молча не показывал (замер: два действующих
    администратора). Признак ставит тот, кто запись создал (`seed-smoke`).
    """
    robot = await make_user("smoke@leadchat.local", role="manager", is_service=True)
    human = await make_user("dispatcher@leadpartner.local", role="manager")
    r = await client.get("/api/v1/users", headers=auth(tokens, "admin"))
    ids = {i["id"] for i in r.json()["items"]}
    assert str(robot.id) not in ids, "помеченный робот виден на экране «Команда»"
    assert str(human.id) in ids, "настоящего человека спрятало по домену почты"
    # и управлять роботом из интерфейса нельзя — 404, а не «отключил робота»
    kill = await client.post(f"/api/v1/users/{robot.id}/deactivate", headers=auth(tokens, "admin"))
    assert kill.status_code == 404, kill.text


async def test_list_pagination_is_honest_about_total(client, tokens, users_by_role):
    r = await client.get(
        "/api/v1/users", headers=auth(tokens, "admin"), params={"limit": 2, "offset": 0}
    )
    body = r.json()
    assert len(body["items"]) == 2
    assert body["page"] == {"limit": 2, "offset": 0, "total": 4}


# --- приглашение (01 §3.2–3.3) ----------------------------------------------


async def test_invite_creates_user_and_returns_one_time_link(
    client, tokens, redis, db_sessionmaker
):
    r = await client.post(
        "/api/v1/users",
        headers=auth(tokens, "admin"),
        json={"email": "Petr@Leadchat.test", "full_name": "  Пётр Ковалёв  ", "role": "head"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "petr@leadchat.test"  # адрес нормализован
    assert body["user"]["full_name"] == "Пётр Ковалёв"
    assert body["user"]["role"] == "head"
    assert body["user"]["invite_pending"] is True
    # ⚠ ПРОВЕРЯЕМ АДРЕС ЭКРАНА, А НЕ СХЕМУ. Стояло `.startswith("https://")` — и это
    # держало дефект: ссылка собиралась из `settings.domain` и вела на БОЕВОЙ домен
    # даже со стенда. Схема при этом всегда сходилась, поэтому тест был зелёным.
    # Теперь адрес берётся из `frontend_base` («там, где живёт экран»), а на стенде
    # это http://localhost:5173 — и требование https стало бы требованием вернуть
    # дефект обратно.
    assert body["invite_url"].startswith(settings.frontend_base)
    assert "/invite/" in body["invite_url"]
    assert body["invite_expires_at"].endswith("Z")

    token = _token_from(body["invite_url"])
    assert await redis.get(f"invite:{token}") == body["user"]["id"]

    rows = await _audit_actions(db_sessionmaker, "user.invited")
    assert [row.entity_id for row in rows] == [body["user"]["id"]]
    assert (rows[0].details or {})["role"] == "head"


async def test_invite_rejects_duplicate_email(client, tokens, target):
    r = await client.post(
        "/api/v1/users",
        headers=auth(tokens, "admin"),
        json={"email": target.email.upper(), "full_name": "Двойник", "role": "manager"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["reason"] == "email_taken"


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "нет-собаки", "full_name": "X", "role": "manager"},
        {"email": "a@b.test", "full_name": "   ", "role": "manager"},
        {"email": "a@b.test", "full_name": "X", "role": "superuser"},
    ],
)
async def test_invite_validates_payload(client, tokens, payload):
    r = await client.post("/api/v1/users", headers=auth(tokens, "admin"), json=payload)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"


async def test_resend_invite_kills_the_previous_link(client, tokens, make_user, redis):
    invited = await make_user("waiting@leadchat.test", invited=True)
    first = await client.post(f"/api/v1/users/{invited.id}/invite", headers=auth(tokens, "admin"))
    second = await client.post(
        f"/api/v1/users/{invited.id}/resend-invite", headers=auth(tokens, "admin")
    )
    assert first.status_code == 200 and second.status_code == 200, second.text
    old, new = _token_from(first.json()["invite_url"]), _token_from(second.json()["invite_url"])
    assert old != new
    assert await redis.get(f"invite:{old}") is None  # старая ссылка погашена (01 §3.3)
    assert await redis.get(f"invite:{new}") == str(invited.id)


async def test_resend_invite_refuses_when_password_is_already_set(client, tokens, target):
    r = await client.post(f"/api/v1/users/{target.id}/invite", headers=auth(tokens, "admin"))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "password_already_set"


async def test_manage_unknown_user_is_404(client, tokens):
    r = await client.post(f"/api/v1/users/{uuid.uuid4()}/deactivate", headers=auth(tokens, "admin"))
    assert r.status_code == 404, r.text


# --- смена роли и имени (01 §3.4) -------------------------------------------


async def test_patch_renames_without_touching_sessions(client, tokens, target, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        headers=auth(tokens, "admin"),
        json={"full_name": "Пётр А. Ковалёв"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["full_name"] == "Пётр А. Ковалёв"
    # переименование — не повод выкидывать человека из системы
    assert await drain_events(pubsub) == []


async def test_patch_requires_at_least_one_field(client, tokens, target):
    r = await client.patch(f"/api/v1/users/{target.id}", headers=auth(tokens, "admin"), json={})
    assert r.status_code == 400, r.text


async def test_role_change_writes_audit_and_revokes_sessions(
    client, tokens, target, redis, db_sessionmaker
):
    """01 §3.4 + 08 §5.4: роль меняется — сокет закрывается кодом 4401.

    4401, а не 4403: человек продолжает работать, но сессия хаба несёт снимок
    роли, сделанный на connect'е, и должна быть пересоздана.
    """
    from app.core.security import issue_refresh_token

    refresh = await issue_refresh_token(redis, str(target.id), remember=False)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    r = await client.patch(
        f"/api/v1/users/{target.id}", headers=auth(tokens, "admin"), json={"role": "observer"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "observer"

    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "control:revoked"
    assert evt["data"] == {"user_id": str(target.id), "code": 4401}
    assert await redis.get(f"refresh:{refresh}") is None  # цепочка refresh'ей отозвана

    rows = await _audit_actions(db_sessionmaker, "user.role_changed")
    assert [row.details for row in rows] == [{"from": "manager", "to": "observer"}]


async def test_role_change_keeps_the_employee_able_to_log_in(client, tokens, target, redis):
    """Понижение — не бан. Пометка `revoked_users` тут не ставится осознанно:
    в её сегодняшнем виде («ключ есть → 401») она закрыла бы человеку и
    повторный вход на 15 минут, а роль бэкенд и так читает из строки БД."""
    await client.patch(
        f"/api/v1/users/{target.id}", headers=auth(tokens, "admin"), json={"role": "observer"}
    )
    assert await redis.exists(revoked_key(target.id)) == 0
    login = await client.post(
        "/api/v1/auth/login", json={"email": target.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"] == "observer"


# --- защита от прострела в ногу (01 §3.4–3.5) -------------------------------


async def test_last_admin_cannot_demote_himself(client, tokens, users_by_role):
    """Отказ приходит с кодом `self_role_change`, а не `last_admin`.

    Раньше единственным способом остаться без администратора было понижение
    себя, и ловил его запрет «последнего администратора». Теперь самопонижение
    закрыто раньше и по своей причине — и это точнее: человеку сообщают, что
    делать («попросите другого администратора»), а не почему нельзя.

    Инвариант «админ в системе есть всегда» держится обеими проверками:
    `_assert_admin_remains` остаётся второй линией на случай, когда матрица
    прав изменится и `users:manage` появится не только у admin'а.
    """
    admin = users_by_role["admin"]
    r = await client.patch(
        f"/api/v1/users/{admin.id}", headers=auth(tokens, "admin"), json={"role": "manager"}
    )
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "self_role_change"
    assert "администратор" in error["message"].lower()  # текст для человека, не код


async def test_admin_can_be_demoted_while_another_admin_remains(
    client, tokens, users_by_role, make_user
):
    second = await make_user("admin2@leadchat.test", role="admin")
    r = await client.patch(
        f"/api/v1/users/{second.id}", headers=auth(tokens, "admin"), json={"role": "head"}
    )
    assert r.status_code == 200, r.text
    # а теперь единственный оставшийся снова заперт
    last = await client.patch(
        f"/api/v1/users/{users_by_role['admin'].id}",
        headers=auth(tokens, "admin"),
        json={"role": "head"},
    )
    assert last.status_code == 409, last.text


async def test_admin_cannot_demote_himself_even_when_another_admin_exists(
    client, tokens, users_by_role, make_user
):
    """Снять роль с самого себя нельзя, даже если админов в системе двое.

    Настоящий случай: владелец системы понизил себя и остался без управления
    сотрудниками — вернуть роль он уже не мог, потому что для этого нужно
    право, которое он только что отдал.

    Запрет «последнего администратора» тут не помогает по построению: при
    втором админе он операцию пропускает, то есть не срабатывает ровно в самом
    частом случае. Поэтому проверка отдельная и стоит раньше.
    """
    await make_user("second-admin@leadchat.test", role="admin")

    r = await client.patch(
        f"/api/v1/users/{users_by_role['admin'].id}",
        headers=auth(tokens, "admin"),
        json={"role": "manager"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["reason"] == "self_role_change"

    # Своё имя менять по-прежнему можно: запрещена смена РОЛИ, а не правка
    # карточки.
    r = await client.patch(
        f"/api/v1/users/{users_by_role['admin'].id}",
        headers=auth(tokens, "admin"),
        json={"full_name": "Новое Имя"},
    )
    assert r.status_code == 200, r.text


async def test_admin_cannot_deactivate_himself(client, tokens, users_by_role):
    r = await client.post(
        f"/api/v1/users/{users_by_role['admin'].id}/deactivate", headers=auth(tokens, "admin")
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["reason"] == "self_deactivation"


async def test_invited_admin_does_not_count_as_the_last_one(db_sessionmaker, make_user):
    """Приглашённый админ, не установивший пароль, войти не может — считать
    его страховкой значит разрешить операцию, после которой не зайдёт никто.

    Проверяется напрямую, а не через ручку, и вот почему. Раньше сюда вело
    единственное действие — понижение админом самого себя, — но оно теперь
    запрещено раньше и отдельным отказом (`self_role_change`). Через API
    состояние «в системе не остаётся администратора» стало недостижимым, и это
    хорошо. Однако сам инвариант остаётся: `users:manage` есть только у admin'а
    СЕГОДНЯ, а матрица прав меняется (DESIGN §5.1). Тест сторожит именно
    инвариант, а не путь к нему.
    """
    real = await make_user("real-admin@leadchat.test", role="admin")
    await make_user("ghost-admin@leadchat.test", role="admin", invited=True)

    async with db_sessionmaker() as s:
        target = await s.get(User, real.id)
        with pytest.raises(ApiError) as err:
            await users_service._assert_admin_remains(s, target, what="понизить роль")
    assert err.value.details["reason"] == "last_admin"


async def _admin_id(client, tokens) -> str:
    me = await client.get("/api/v1/auth/me", headers=auth(tokens, "admin"))
    return me.json()["id"]


async def test_last_admin_guard_holds_for_any_actor(db, redis, users_by_role):
    """Инвариант «админ есть всегда» защищает состояние системы, а не права
    вызывающего: сегодня сюда попадает только сам админ, но матрица прав
    меняется (DESIGN §5.1), а инвариант — нет."""
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        await users_service.deactivate_user(
            db, redis, actor=users_by_role["head"], user_id=users_by_role["admin"].id
        )
    assert exc.value.code == "last_admin"
    assert exc.value.status == 409


# --- отключение и включение (01 §3.5) ---------------------------------------


async def test_deactivate_locks_all_three_doors(client, tokens, target, redis, db_sessionmaker):
    """Отключение = refresh + access + сокет. Пропуск любого замка делает его
    косметическим: (1) вернёт вход через минуту, (2) оставит REST открытым на
    15 минут, (3) оставит открытым поток сообщений навсегда."""
    from app.core.security import issue_refresh_token

    refresh = await issue_refresh_token(redis, str(target.id), remember=False)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    r = await client.post(f"/api/v1/users/{target.id}/deactivate", headers=auth(tokens, "admin"))
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_active"] is False

    # 1. refresh-цепочка отозвана
    assert await redis.get(f"refresh:{refresh}") is None
    assert await redis.smembers(f"user_refresh:{target.id}") == set()
    # 2. действующий access-токен в денилисте — и ключ живёт не дольше токена
    assert await redis.exists(revoked_key(target.id)) == 1
    assert 0 < await redis.ttl(revoked_key(target.id)) <= 900
    # 3. хабу отдана команда закрыть сокеты (потребитель — app/ws/hub.py)
    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "control:revoked"
    assert evt["data"] == {"user_id": str(target.id), "code": 4403}

    rows = await _audit_actions(db_sessionmaker, "user.deactivated")
    assert [row.entity_id for row in rows] == [str(target.id)]


async def test_deactivated_employee_loses_rest_access_immediately(client, tokens, target):
    """Тот самый провал приёмки: отключённый продолжал читать переписку."""
    employee = {
        "Authorization": f"Bearer {create_access_token(user_id=str(target.id), role='manager')}"
    }
    assert (await client.get("/api/v1/auth/me", headers=employee)).status_code == 200

    await client.post(f"/api/v1/users/{target.id}/deactivate", headers=auth(tokens, "admin"))

    for path in ("/api/v1/auth/me", "/api/v1/conversations"):
        after = await client.get(path, headers=employee)
        assert after.status_code in (401, 403), f"{path}: {after.status_code} {after.text}"


async def test_deactivate_is_idempotent_and_still_kicks(
    client, tokens, target, redis, db_sessionmaker
):
    """Повторный вызов журнал не засоряет, но сессии рвёт снова: «отключён, а
    всё ещё сидит» — состояние, из которого нужен выход одной кнопкой."""
    await client.post(f"/api/v1/users/{target.id}/deactivate", headers=auth(tokens, "admin"))
    await redis.delete(revoked_key(target.id))

    again = await client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=auth(tokens, "admin")
    )
    assert again.status_code == 200, again.text
    assert await redis.exists(revoked_key(target.id)) == 1
    assert len(await _audit_actions(db_sessionmaker, "user.deactivated")) == 1


async def test_activate_clears_the_denylist(client, tokens, target, redis, db_sessionmaker):
    await client.post(f"/api/v1/users/{target.id}/deactivate", headers=auth(tokens, "admin"))
    r = await client.post(f"/api/v1/users/{target.id}/activate", headers=auth(tokens, "admin"))
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_active"] is True
    # иначе включённый сотрудник упирался бы в 401 до конца 15-минутного окна
    assert await redis.exists(revoked_key(target.id)) == 0
    assert len(await _audit_actions(db_sessionmaker, "user.activated")) == 1

    login = await client.post(
        "/api/v1/auth/login", json={"email": target.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text


# --- сброс пароля (01 §3.3, 14 §2.2) ----------------------------------------


async def test_reset_password_issues_link_kills_old_password_and_sessions(
    client, tokens, target, redis
):
    from app.core.security import issue_refresh_token

    refresh = await issue_refresh_token(redis, str(target.id), remember=False)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    r = await client.post(
        f"/api/v1/users/{target.id}/reset-password", headers=auth(tokens, "admin")
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["invite_pending"] is True

    old_login = await client.post(
        "/api/v1/auth/login", json={"email": target.email, "password": DEFAULT_PASSWORD}
    )
    assert old_login.status_code == 401  # старый пароль больше не работает
    assert await redis.get(f"refresh:{refresh}") is None
    (evt,) = await drain_events(pubsub)
    assert evt["data"] == {"user_id": str(target.id), "code": 4403}

    accepted = await client.post(
        "/api/v1/auth/invite/accept",
        json={"token": _token_from(r.json()["invite_url"]), "password": "новый пароль 123"},
    )
    assert accepted.status_code == 200, accepted.text


async def test_employee_can_work_right_after_setting_a_new_password(client, tokens, target):
    """Сброс пароля → сотрудник поставил новый → работает СРАЗУ, а не через 15 минут.

    Был xfail: `POST /auth/invite/accept` не снимал денилист access-токенов,
    который ставит сброс пароля. Денилист — «ключ revoked_users:{id} есть →
    401», он не различает старый токен и выданный секунду назад и живёт 15
    минут: сотрудник ставил новый пароль и упирался в «Требуется авторизация»
    до конца окна. Лечение — `clear_revocation(redis, user.id)` после commit'а
    в invite_accept; лазейки отключённому это не даёт, ему та же ручка отвечает
    404 выше по коду.
    """
    r = await client.post(
        f"/api/v1/users/{target.id}/reset-password", headers=auth(tokens, "admin")
    )
    accepted = await client.post(
        "/api/v1/auth/invite/accept",
        json={"token": _token_from(r.json()["invite_url"]), "password": "новый пароль 123"},
    )
    assert accepted.status_code == 200, accepted.text
    fresh = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=fresh)
    assert me.status_code == 200, me.text


async def test_notification_center_action_contract(db, redis, users_by_role, target):
    """Кнопку «Выслать новую ссылку» центра уведомлений обслуживает эта же
    функция сервиса — контракт `(db, redis, *, entity_id, actor) -> dict`
    (14 §3, реестр действий в app/services/notifications.py)."""
    result = await users_service.issue_password_reset_action(
        db, redis, entity_id=str(target.id), actor=users_by_role["admin"]
    )
    # Тот же довод, что и выше: адрес экрана, а не схема.
    assert result["invite_url"].startswith(settings.frontend_base)
    assert result["user"]["id"] == target.id
    assert await redis.exists(revoked_key(target.id)) == 1


async def test_notification_center_action_without_entity(db, redis, users_by_role):
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        await users_service.issue_password_reset_action(
            db, redis, entity_id=None, actor=users_by_role["admin"]
        )
    assert exc.value.status == 422


# --- полный цикл (задание спринта: приглашение → работа → отказ в доступе) ---


async def test_full_lifecycle_invite_work_deactivate_denied(client, tokens, redis, db_sessionmaker):
    # 1. администратор приглашает
    invited = await client.post(
        "/api/v1/users",
        headers=auth(tokens, "admin"),
        json={"email": "newbie@leadchat.test", "full_name": "Новичок", "role": "manager"},
    )
    assert invited.status_code == 201, invited.text
    user_id = invited.json()["user"]["id"]
    token = _token_from(invited.json()["invite_url"])

    # 2. сотрудник открывает ссылку и ставит пароль
    info = await client.get(f"/api/v1/auth/invite/{token}")
    assert info.json() == {"email": "newbie@leadchat.test", "full_name": "Новичок"}
    accepted = await client.post(
        "/api/v1/auth/invite/accept", json={"token": token, "password": "пароль подлиннее"}
    )
    assert accepted.status_code == 200, accepted.text
    employee = {"Authorization": f"Bearer {accepted.json()['access_token']}"}

    # 3. работает
    assert (await client.get("/api/v1/conversations", headers=employee)).status_code == 200

    # 4. администратор отключает
    off = await client.post(f"/api/v1/users/{user_id}/deactivate", headers=auth(tokens, "admin"))
    assert off.status_code == 200, off.text

    # 5. доступа больше нет — ни по действующему access'у, ни через refresh
    assert (await client.get("/api/v1/conversations", headers=employee)).status_code in (401, 403)
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "newbie@leadchat.test", "password": "пароль подлиннее"},
        )
    ).status_code == 403  # «Учётная запись отключена, обратитесь к администратору»

    async with db_sessionmaker() as db:
        row = await db.get(User, uuid.UUID(user_id))
        assert row is not None and row.is_active is False
