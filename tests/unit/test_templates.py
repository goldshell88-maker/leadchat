"""Шаблоны быстрых ответов — 01 §7.

Ключевые инварианты: видимость (свои личные + общие, чужой личный не
существует), общие пишет только `templates:shared` (admin/head), observer
не видит шаблоны вовсе, а тело хранится как есть — `{имя}`/`{менеджер}`
подставляет фронт (01 §7).
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AuditLog, Template


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def seed(db_sessionmaker, users_by_role):
    """Общий шаблон + личный менеджера + личный админа."""
    async with db_sessionmaker() as s:
        shared = Template(
            owner_id=None,
            title="Приветствие",
            body="Здравствуйте, {имя}! Это сервис Lead Partner 👋",
            folder="Приветствия",
        )
        shared_prices = Template(
            owner_id=None, title="Цена на экран", body="Замена экрана — от 8 900 ₽", folder="Цены"
        )
        mine = Template(
            owner_id=users_by_role["manager"].id,
            title="Моя скидка",
            body="Могу предложить скидку 5% при заказе сегодня",
            folder="Мои скидки",
        )
        foreign = Template(
            owner_id=users_by_role["admin"].id,
            title="Личное админа",
            body="Только для админа",
            folder=None,
        )
        s.add_all([shared, shared_prices, mine, foreign])
        await s.commit()
        return SimpleNamespace(
            shared=shared.id,
            shared_prices=shared_prices.id,
            mine=mine.id,
            foreign=foreign.id,
        )


def titles(payload):
    return [t["title"] for t in payload["items"]]


# --- видимость и фильтры (01 §7.1) -------------------------------------------


async def test_manager_sees_own_and_shared_but_not_foreign_personal(client, tokens, seed):
    r = await client.get("/api/v1/templates", headers=auth(tokens))
    assert r.status_code == 200, r.text
    payload = r.json()
    assert set(titles(payload)) == {"Приветствие", "Цена на экран", "Моя скидка"}
    assert "Личное админа" not in titles(payload)
    assert payload["page"]["total"] == 3
    # тело хранится как есть — подстановку переменных делает фронт (01 §7)
    greeting = next(t for t in payload["items"] if t["title"] == "Приветствие")
    assert greeting["body"] == "Здравствуйте, {имя}! Это сервис Lead Partner 👋"
    assert greeting["owner_id"] is None


async def test_scope_filter(client, tokens, seed, users_by_role):
    shared = await client.get("/api/v1/templates", params={"scope": "shared"}, headers=auth(tokens))
    assert set(titles(shared.json())) == {"Приветствие", "Цена на экран"}

    personal = await client.get(
        "/api/v1/templates", params={"scope": "personal"}, headers=auth(tokens)
    )
    assert titles(personal.json()) == ["Моя скидка"]
    assert personal.json()["items"][0]["owner_id"] == str(users_by_role["manager"].id)


async def test_folder_and_text_filters(client, tokens, seed):
    by_folder = await client.get(
        "/api/v1/templates", params={"folder": "Цены"}, headers=auth(tokens)
    )
    assert titles(by_folder.json()) == ["Цена на экран"]

    by_title = await client.get("/api/v1/templates", params={"q": "скидк"}, headers=auth(tokens))
    assert titles(by_title.json()) == ["Моя скидка"]

    by_body = await client.get("/api/v1/templates", params={"q": "8 900"}, headers=auth(tokens))
    assert titles(by_body.json()) == ["Цена на экран"]


async def test_pagination_is_stable(client, tokens, seed):
    first = await client.get(
        "/api/v1/templates", params={"limit": 1, "offset": 0}, headers=auth(tokens)
    )
    second = await client.get(
        "/api/v1/templates", params={"limit": 1, "offset": 1}, headers=auth(tokens)
    )
    assert first.json()["page"]["total"] == second.json()["page"]["total"] == 3
    assert titles(first.json()) != titles(second.json())


async def test_folders_endpoint_splits_shared_and_personal(client, tokens, seed):
    r = await client.get("/api/v1/templates/folders", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert r.json() == {"shared": ["Приветствия", "Цены"], "personal": ["Мои скидки"]}


# --- создание (01 §7.3) ------------------------------------------------------


async def test_manager_creates_personal_template(
    client, tokens, seed, db_sessionmaker, users_by_role
):
    r = await client.post(
        "/api/v1/templates",
        json={"title": "Перезвоню", "body": "Наберу вас в течение часа", "folder": " Мои  "},
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["owner_id"] == str(users_by_role["manager"].id)
    assert body["folder"] == "Мои"  # папка нормализуется от пробелов

    async with db_sessionmaker() as s:
        rows = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "template.created"))
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].details == {"shared": False, "folder": "Мои"}


async def test_manager_cannot_create_shared_template(client, tokens, seed):
    r = await client.post(
        "/api/v1/templates",
        json={"title": "Общий", "body": "текст", "shared": True},
        headers=auth(tokens),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ["admin", "head"])
async def test_shared_template_created_by_admin_and_head(client, tokens, seed, role):
    r = await client.post(
        "/api/v1/templates",
        json={"title": f"Общий от {role}", "body": "текст", "shared": True},
        headers=auth(tokens, role),
    )
    assert r.status_code == 201, r.text
    assert r.json()["owner_id"] is None


async def test_blank_title_is_validation_error(client, tokens, seed):
    r = await client.post(
        "/api/v1/templates", json={"title": "   ", "body": "текст"}, headers=auth(tokens)
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


# --- изменение и удаление (01 §7.4) ------------------------------------------


async def test_owner_edits_own_template(client, tokens, seed):
    r = await client.patch(
        f"/api/v1/templates/{seed.mine}",
        json={"title": "Моя скидка 10%", "folder": None},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Моя скидка 10%"
    assert r.json()["folder"] is None
    assert r.json()["body"] == "Могу предложить скидку 5% при заказе сегодня"  # не тронуто


async def test_foreign_personal_template_is_invisible(client, tokens, seed):
    """Чужой личный — 404, а не 403: факт существования не раскрываем (01 §7.4)."""
    patch = await client.patch(
        f"/api/v1/templates/{seed.foreign}", json={"title": "Взлом"}, headers=auth(tokens)
    )
    assert patch.status_code == 404
    assert patch.json()["error"]["code"] == "not_found"

    delete = await client.delete(f"/api/v1/templates/{seed.foreign}", headers=auth(tokens))
    assert delete.status_code == 404


async def test_manager_cannot_touch_shared_template(client, tokens, seed):
    patch = await client.patch(
        f"/api/v1/templates/{seed.shared}", json={"title": "Правка"}, headers=auth(tokens)
    )
    assert patch.status_code == 403

    delete = await client.delete(f"/api/v1/templates/{seed.shared}", headers=auth(tokens))
    assert delete.status_code == 403


async def test_head_manages_shared_template(client, tokens, seed):
    patch = await client.patch(
        f"/api/v1/templates/{seed.shared}",
        json={"body": "Здравствуйте, {имя}!"},
        headers=auth(tokens, "head"),
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["body"] == "Здравствуйте, {имя}!"
    assert patch.json()["owner_id"] is None  # общий остаётся общим

    delete = await client.delete(
        f"/api/v1/templates/{seed.shared_prices}", headers=auth(tokens, "head")
    )
    assert delete.status_code == 204


async def test_admin_may_edit_foreign_personal(client, tokens, seed):
    r = await client.patch(
        f"/api/v1/templates/{seed.mine}",
        json={"title": "Правка админом"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text


async def test_delete_removes_row_and_writes_audit(client, tokens, seed, db_sessionmaker):
    r = await client.delete(f"/api/v1/templates/{seed.mine}", headers=auth(tokens))
    assert r.status_code == 204
    async with db_sessionmaker() as s:
        assert await s.get(Template, seed.mine) is None
        actions = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "template.deleted"))
            ).scalars()
        )
    assert len(actions) == 1


async def test_unknown_template_is_404(client, tokens, seed):
    r = await client.patch(
        f"/api/v1/templates/{uuid.uuid4()}", json={"title": "x"}, headers=auth(tokens)
    )
    assert r.status_code == 404


# --- RBAC --------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("GET", "/api/v1/templates", None),
        ("GET", "/api/v1/templates/folders", None),
        ("POST", "/api/v1/templates", {"title": "t", "body": "b"}),
        ("PATCH", "/api/v1/templates/{id}", {"title": "t"}),
        ("DELETE", "/api/v1/templates/{id}", None),
    ],
)
async def test_observer_is_forbidden_everywhere(client, tokens, seed, method, path, json_body):
    """У observer нет `templates:own` — шаблонов он не видит вовсе (01 §7)."""
    r = await client.request(
        method,
        path.format(id=seed.shared),
        json=json_body,
        headers=auth(tokens, "observer"),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/v1/templates"), ("GET", "/api/v1/templates/folders")]
)
async def test_anonymous_is_401(client, seed, method, path):
    r = await client.request(method, path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
