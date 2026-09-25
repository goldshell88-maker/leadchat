"""Удалённый сотрудник остаётся удалённым (01 §3.5 + требование от 7 августа).

`activate_user` не смотрел на `deleted_at`, и «Включить» на удалённой строке
срабатывало: `is_active` возвращался в true, а вместе с ним — вход, REST и
сокет, потому что и логин, и `api/deps.py` проверяют только `is_active`. При
этом в «Команде» человека нет, в «кому передать» нет, в пуле операторов нет:
выданный доступ становился невидимым, и отобрать его кнопкой было нечем.

Попасть туда легко без всякого злого умысла: у второго администратора экран
«Команда» открыт со вчера, строка удалённого сотрудника на нём ещё висит, и
кнопка «Включить» на ней живая.
"""

import pytest

from app.models import User
from tests.unit.conftest import DEFAULT_PASSWORD


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def deleted(client, tokens, make_user):
    """Сотрудник, удалённый администратором через ручку."""
    user = await make_user("fired@leadchat.test", role="manager", full_name="Ушедший")
    gone = await client.delete(f"/api/v1/users/{user.id}", headers=auth(tokens, "admin"))
    assert gone.status_code == 200, gone.text
    return user


async def test_deleted_employee_cannot_be_switched_back_on(
    client, tokens, deleted, db_sessionmaker
):
    """Главный замок: «Включить» удалённого — отказ, а не тихая выдача доступа."""
    back = await client.post(f"/api/v1/users/{deleted.id}/activate", headers=auth(tokens, "admin"))
    assert back.status_code == 409, back.text
    error = back.json()["error"]
    assert error["details"]["reason"] == "user_deleted"
    # Текст для человека: что делать, а не только почему нельзя.
    assert "заново" in error["message"].lower()

    # Строка не тронута — ни флагом, ни отметкой удаления.
    async with db_sessionmaker() as db:
        row = await db.get(User, deleted.id)
        assert row is not None
        assert row.is_active is False
        assert row.deleted_at is not None

    # И доступа по-прежнему нет: пароль у него был установлен и работал.
    login = await client.post(
        "/api/v1/auth/login", json={"email": deleted.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 403, login.text

    # Вторая половина дефекта: в списках его нет, то есть включённого
    # удалённого никто бы и не увидел.
    listed = await client.get("/api/v1/users", headers=auth(tokens, "admin"))
    assert str(deleted.id) not in {i["id"] for i in listed.json()["items"]}


MANAGE_ENDPOINTS = (
    ("POST", "/api/v1/users/{target}/deactivate", None),
    ("POST", "/api/v1/users/{target}/reset-password", None),
    ("POST", "/api/v1/users/{target}/resend-invite", None),
    ("POST", "/api/v1/users/{target}/set-password", {"password": "пароль подлиннее"}),
    ("PATCH", "/api/v1/users/{target}", {"full_name": "Новое имя"}),
)


@pytest.mark.parametrize(
    "method,path,body", MANAGE_ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in MANAGE_ENDPOINTS]
)
async def test_deleted_employee_is_not_managed_at_all(client, tokens, deleted, method, path, body):
    """Остальные ручки управления — 404: снаружи такого сотрудника нет.

    Проверка одна на все точки входа (`_get_managed_user`), поэтому и ответ
    один: перевыпуск ссылки, сброс и прямая установка пароля удалённому — это
    выдача доступа тому, кого в системе больше нет.
    """
    r = await client.request(
        method, path.format(target=deleted.id), headers=auth(tokens, "admin"), json=body
    )
    assert r.status_code == 404, r.text


async def test_repeated_delete_is_still_idempotent(client, tokens, deleted):
    """Кнопка «Удалить» не должна отвечать 404 на собственный результат:
    состояние конечное, повтор — не ошибка."""
    again = await client.delete(f"/api/v1/users/{deleted.id}", headers=auth(tokens, "admin"))
    assert again.status_code == 200, again.text
    assert again.json()["user"]["is_active"] is False
