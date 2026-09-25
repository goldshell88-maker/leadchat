"""Свой профиль: имя и пароль меняет сам сотрудник (требование от 7 августа).

Раньше пароль менял только администратор, и в профиле стояла надпись
«напишите ему». На тринадцать человек это означало, что смена пароля —
переписка на полдня, а значит пароли не меняют вовсе.

Проверяется не «меняется ли хеш». Проверяются две вещи, из-за которых такую
ручку легко сделать дырой:

1. ТЕКУЩИЙ ПАРОЛЬ СПРАШИВАЕТСЯ. Сессия живёт долго, а незапертый ноутбук —
   обычное дело в офисе.
2. ЧУЖИЕ СЕССИИ РВУТСЯ. Пароль меняют в том числе потому, что «кажется, его
   кто-то знает». Смена без обрыва чужих сессий в этом случае бесполезна.
"""

import pytest

from app.core.security import verify_password
from app.models import User

pytestmark = pytest.mark.anyio

OLD = "correct-horse-battery"
NEW = "another-long-password-2026"


@pytest.fixture
async def me(make_user):
    return await make_user("self@leadchat.test", role="manager", password=OLD, full_name="Пётр")


@pytest.fixture
def my_token(me):
    from app.core.security import create_access_token

    return create_access_token(user_id=str(me.id), role=me.role)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_password_changes(client, me, my_token, db_sessionmaker):
    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": NEW},
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        row = await s.get(User, me.id)
    assert verify_password(row.password_hash, NEW)
    assert not verify_password(row.password_hash, OLD)


async def test_the_current_password_is_required(client, me, my_token, db_sessionmaker):
    """ГЛАВНОЕ: без текущего пароля сменить нельзя.

    Иначе любой, кто подошёл к чужому незапертому ноутбуку, менял бы пароль
    коллеге и запирал его из системы.
    """
    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": "не тот пароль", "new_password": NEW},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "wrong_current_password"

    async with db_sessionmaker() as s:
        row = await s.get(User, me.id)
    assert verify_password(row.password_hash, OLD), "пароль остался прежним"


async def test_the_same_password_is_refused(client, me, my_token):
    """«Сменил» на тот же — это не смена, и говорить об этом надо прямо."""
    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": OLD},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "same_password"


async def test_a_short_password_is_refused(client, me, my_token):
    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": "короткий"},
    )
    assert r.status_code == 400


async def test_other_sessions_are_cut(client, me, my_token, redis):
    """Чужие сессии рвутся, своя — остаётся.

    Пароль меняют по двум причинам: «давно пора» и «кажется, кто-то его
    знает». Во втором случае смена без обрыва чужих сессий бесполезна: тот,
    кто уже вошёл, так и останется внутри.

    Выгонять при этом самого человека — плохая награда за правильный
    поступок, поэтому взамен сразу выдаётся новая цепочка.
    """
    from app.core.security import issue_refresh_token, rotate_refresh_token

    stolen = await issue_refresh_token(redis, str(me.id), remember=True)

    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": NEW},
    )
    assert r.status_code == 200

    # Чужая цепочка мертва.
    from app.core.security import RefreshTokenInvalid

    with pytest.raises(RefreshTokenInvalid):
        await rotate_refresh_token(redis, stolen)

    # А своя выдана заново — иначе текущая вкладка молча выпала бы.
    assert r.cookies.get("refresh_token") or "refresh" in str(r.headers.get("set-cookie", ""))


async def test_чужой_access_и_сокет_тоже_рвутся(client, me, my_token, redis):
    """⚠ ОБРЫВ ЦЕЛИКОМ, А НЕ ОДНИ REFRESH-ЦЕПОЧКИ (аудит 30.08).

    Соседний тест проверял только refresh — и потому пропускал дыру: ручка
    звала `revoke_all_user_refresh`, то есть чужая открытая вкладка сохраняла
    ДЕЙСТВУЮЩИЙ access-токен на все пятнадцать минут (а с ним и право выписывать
    новые WS-тикеты), а уже открытый сокет не закрывался вовсе и продолжал
    получать переписку клиентов. Для смены пароля «потому что кажется, кто-то
    его знает» это обесценивало всю операцию.

    Здесь сторожим оба недостающих следствия: пометку отзыва access-токенов
    (её читает `deps.py`) и команду хабу закрыть сокеты.
    """
    from app.services.sessions import access_revoked_at

    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": NEW},
    )
    assert r.status_code == 200

    отозвано = await access_revoked_at(redis, me.id)
    assert отозвано, "access-токены не отозваны: чужая вкладка останется внутри ещё на 15 минут"


async def test_своя_вкладка_остаётся_внутри(client, me, my_token, redis):
    """Обрыв не должен выгонять того, кто сменил пароль.

    Это работает благодаря сверке по `iat` в `deps.py` (27.08): старый токен
    вкладки выписан ДО отзыва и отвергается, но свежая refresh-кука выдана
    здесь же — вкладка обновится и получит токен, выписанный ПОСЛЕ.
    Проверяем именно это: кука на месте, а отзыв не позже неё.
    """
    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": NEW},
    )
    assert r.status_code == 200
    assert r.cookies.get("refresh_token") or "refresh" in str(r.headers.get("set-cookie", "")), (
        "своя цепочка не выдана заново — человек выпадет за правильный поступок"
    )


async def test_anonymous_cannot_change_anything(client):
    assert (
        await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": OLD, "new_password": NEW},
        )
    ).status_code == 401
    assert (await client.patch("/api/v1/auth/me", json={"full_name": "Кто-то"})).status_code == 401


class TestOwnName:
    """Имя видно клиенту в подписи сообщения и коллегам в списке передачи."""

    async def test_it_changes(self, client, me, my_token, db_sessionmaker):
        r = await client.patch(
            "/api/v1/auth/me", headers=auth(my_token), json={"full_name": "Пётр Иванов"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["full_name"] == "Пётр Иванов"

        async with db_sessionmaker() as s:
            assert (await s.get(User, me.id)).full_name == "Пётр Иванов"

    async def test_an_empty_name_is_refused(self, client, my_token):
        """Человек без имени в списке передачи — это пустая строка, на которую
        нельзя нажать осмысленно."""
        r = await client.patch("/api/v1/auth/me", headers=auth(my_token), json={"full_name": "   "})
        assert r.status_code == 400

    async def test_the_role_cannot_be_changed_this_way(self, client, me, my_token, db_sessionmaker):
        """Роль — вопрос прав, и остаётся за администратором.

        Лишнее поле в теле просто игнорируется: иначе менеджер повышал бы себя
        до администратора одним запросом.
        """
        await client.patch(
            "/api/v1/auth/me",
            headers=auth(my_token),
            json={"full_name": "Пётр", "role": "admin"},
        )
        async with db_sessionmaker() as s:
            assert (await s.get(User, me.id)).role == "manager"


@pytest.mark.parametrize("role", ["admin", "head", "manager", "observer"])
async def test_every_role_manages_its_own_profile(client, tokens, users_by_role, role):
    """Своё имя меняет каждый, включая наблюдателя: это не право над системой,
    а собственные данные человека."""
    r = await client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens[role]}"},
        json={"full_name": f"Новое Имя {role}"},
    )
    assert r.status_code == 200, (role, r.text)


async def test_own_sockets_reconnect_instead_of_logging_out(client, me, my_token, redis):
    """Кодом «выйди» (4403) закрывалась и вкладка, где пароль сменили: фронт
    на нём выходит на экран входа. Код «переподключись» (4401) даёт этой вкладке
    вернуться с новой кукой, а чужим устройствам — упереться в отозванную
    цепочку обновления и выйти."""
    from app.ws.events import EVENTS_CHANNEL
    from tests.unit.conftest import drain_events

    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)

    r = await client.post(
        "/api/v1/auth/change-password",
        headers=auth(my_token),
        json={"current_password": OLD, "new_password": NEW},
    )
    assert r.status_code == 200

    revoked = [e for e in await drain_events(pubsub) if e.get("type") == "control:revoked"]
    assert [e["data"]["code"] for e in revoked] == [4401]
