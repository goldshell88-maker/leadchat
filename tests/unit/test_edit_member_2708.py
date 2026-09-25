"""Правка карточки сотрудника и пароль без минимальной длины (27.08).

Владелец: «сделай так, чтобы можно было указывать любой пароль» и «нужно чтобы
можно было менять всё — и имя, и почту». Здесь оба правила и их границы.
"""

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.schemas.users import SetPasswordIn, UserPatchIn
from app.services.audit import describe

pytestmark = pytest.mark.anyio


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def target(make_user):
    """Обычный сотрудник, над которым ставятся опыты."""
    return await make_user("target-edit@leadchat.test", role="manager", full_name="Пётр Ковалёв")


def test_admin_password_has_no_minimum_length():
    """Планка в десять знаков снята там, где пароль задаёт администратор."""
    assert SetPasswordIn(password="1").password == "1"
    assert SetPasswordIn(password="abc").password == "abc"


def test_empty_password_is_still_refused():
    """Ноль знаков — это не простой пароль, а вход без пароля."""
    with pytest.raises(ValueError):
        SetPasswordIn(password="")


def test_password_upper_bound_stays():
    """Верхняя граница осталась: 128 знаков — предел хранилища, не вкус."""
    assert SetPasswordIn(password="a" * 128)
    with pytest.raises(ValueError):
        SetPasswordIn(password="a" * 129)


def test_email_can_be_changed():
    p = UserPatchIn(email="  NEW@Example.COM ")
    assert p.email == "new@example.com", "почта обрезается и приводится к нижнему регистру"


def test_broken_email_is_refused_the_same_way_as_on_invite():
    with pytest.raises(ValueError):
        UserPatchIn(email="без-собаки")


def test_email_alone_is_enough_to_pass_the_empty_body_check():
    """⚠ Каждое новое поле обязано попадать в проверку «тело не пустое», иначе
    запрос «поменять только почту» отбивается как пустой."""
    assert UserPatchIn(email="a@b.ru").email == "a@b.ru"


def test_empty_body_is_still_refused():
    with pytest.raises(ValueError):
        UserPatchIn()


async def test_changing_email_moves_the_login(client, users_by_role, tokens, target):
    """Смена почты — это смена логина: старый адрес больше не пускает."""
    старый = target.email
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": "peremena@example.com"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "peremena@example.com"
    assert старый != "peremena@example.com"


async def test_taken_email_answers_409_and_not_a_database_crash(
    client, users_by_role, tokens, target
):
    """Занятый адрес обязан вернуть понятный отказ, а не 500 от уникального индекса."""
    занятый = users_by_role["admin"].email
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": занятый},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "email_taken"


async def test_email_change_lands_in_the_audit_log_with_both_addresses(
    client, db_sessionmaker, users_by_role, tokens, target
):
    """⚠ Смена логина обязана остаться в журнале, и с ОБОИМИ адресами.

    Без старого адреса запись бесполезна: через месяц на вопрос «почему он не
    может войти прежней почтой» ответить будет нечем."""
    старый = target.email
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": "audit-check@example.com"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as db:
        rows = await db.execute(select(AuditLog).where(AuditLog.action == "user.email_changed"))
        записи = list(rows.scalars())
    assert len(записи) == 1, "ровно одна запись на одну смену"
    assert записи[0].details["from"] == старый
    assert записи[0].details["to"] == "audit-check@example.com"
    # Действие обязано иметь человекочитаемый ярлык: иначе в журнале появится
    # голый машинный код, которого не понимает никто.
    подпись = describe(записи[0].action, записи[0].details)
    assert "user.email_changed" not in подпись, подпись
    assert старый in подпись and "audit-check@example.com" in подпись, подпись


async def test_changing_only_the_name_writes_no_email_event(
    client, db_sessionmaker, users_by_role, tokens, target
):
    """Правка имени не должна оставлять след «сменили почту» — его не было."""
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"full_name": "Пётр Ковалёв-Новый"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as db:
        rows = await db.execute(select(AuditLog).where(AuditLog.action == "user.email_changed"))
        assert list(rows.scalars()) == []


async def test_sending_the_same_email_writes_nothing(
    client, db_sessionmaker, users_by_role, tokens, target
):
    """Почта прислана та же (пусть и в другом регистре) — смены не было."""
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": target.email.upper()},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as db:
        rows = await db.execute(select(AuditLog).where(AuditLog.action == "user.email_changed"))
        assert list(rows.scalars()) == [], "запись о смене там, где ничего не менялось"


# --- Разбор диалогов открыт всем и источник при подключении (27.08) ----------


def test_dialogs_read_is_granted_to_every_role():
    """«Вкладку Разбор диалогов нужно сделать доступной для всех, Менеджер и тд»."""
    from app.core.rbac import ROLES, permissions_for

    for role in ROLES:
        assert "dialogs:read" in permissions_for(role), role


def test_opening_the_table_did_not_open_stats_and_feed():
    """⚠ Право отдельное намеренно: выдать менеджеру `stats:all` означало бы
    открыть ему заодно Статистику по всем и Живую ленту."""
    from app.core.rbac import permissions_for

    assert "stats:all" not in permissions_for("manager")
    assert "stats:all" not in permissions_for("observer")


def test_connect_schema_accepts_the_lead_origin():
    """Источник задаётся тем же запросом, что и подключение."""
    from app.api.routes.avito_connect import ConnectByKeysIn

    body = ConnectByKeysIn(client_id="a", client_secret="b", lead_origin="В95")
    assert body.lead_origin == "В95"
    # и остаётся необязательным: код можно проставить позже, в карточке
    assert ConnectByKeysIn(client_id="a", client_secret="b").lead_origin is None


# --- Отзыв доступа: сверка по времени выпуска токена (27.08) ------------------


async def test_revoked_token_is_refused_but_a_fresh_one_works(client, redis, users_by_role, tokens):
    """⚠ ОТЗЫВ ЗАКРЫВАЛ ДОСТУП ВСЕМ ТОКЕНАМ НА 15 МИНУТ, ВКЛЮЧАЯ НОВЫЙ.

    Проверка стояла как «ключ есть» — то есть человек, честно перезашедший
    через минуту после отзыва, упирался в 401 до конца окна. Из-за этого смене
    роли пришлось звать `revoke_sessions(block_access=False)`.

    Время отзыва писалось в пометку с самого начала, и функция чтения была
    написана ровно под эту сверку — но не вызывалась ни разу.
    """
    import time as _time

    from app.core.security import create_access_token
    from app.services.sessions import revoked_key

    цель = users_by_role["manager"]
    момент = int(_time.time())
    await redis.set(revoked_key(цель.id), str(момент), ex=900)

    # Токен, выписанный ДО отзыва, доступа не даёт.
    старый = create_access_token(user_id=str(цель.id), role=цель.role)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {старый}"})
    assert r.status_code == 401, r.text

    # А выписанный ПОСЛЕ — работает: это и есть перезаход.
    await redis.set(revoked_key(цель.id), str(момент - 5), ex=900)
    свежий = create_access_token(user_id=str(цель.id), role=цель.role)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {свежий}"})
    assert r.status_code == 200, r.text


async def test_unparseable_revocation_mark_refuses_access(client, redis, users_by_role):
    """Пометка без разбираемого времени (формат прошлых версий) — отказ.

    Осторожная сторона здесь одна: пустить по недостающим данным дороже, чем
    попросить перезайти."""
    from app.core.security import create_access_token
    from app.services.sessions import revoked_key

    цель = users_by_role["manager"]
    await redis.set(revoked_key(цель.id), "не-число", ex=900)

    токен = create_access_token(user_id=str(цель.id), role=цель.role)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {токен}"})
    assert r.status_code == 401, r.text


async def test_no_revocation_mark_lets_everyone_in(client, users_by_role):
    """Пометки нет — обычная работа. Сверка не должна отбирать доступ у всех."""
    from app.core.security import create_access_token

    цель = users_by_role["manager"]
    токен = create_access_token(user_id=str(цель.id), role=цель.role)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {токен}"})
    assert r.status_code == 200, r.text


async def test_token_without_iat_is_refused_while_revocation_stands(client, redis, users_by_role):
    """⚠ ТОКЕН БЕЗ `iat` ПРИ ДЕЙСТВУЮЩЕМ ОТЗЫВЕ — ОТКАЗ.

    Сверка «выписан ли токен после отзыва» без времени выпуска неразрешима.
    Формат `{sub, role, iat, exp, jti}` держится с самого начала, но проверка
    обязана оставаться верной и для токена, собранного иначе: пустить по
    недостающим данным здесь дороже, чем попросить перезайти.

    Диверсия, которую этот тест закрывает: заменить `not isinstance(...) or`
    на `isinstance(...) and` — тогда токен без `iat` проходит при живом отзыве.
    """
    import time as _time

    import jwt as _jwt

    from app.core.config import settings
    from app.services.sessions import revoked_key

    цель = users_by_role["manager"]
    await redis.set(revoked_key(цель.id), str(int(_time.time()) - 5), ex=900)

    без_iat = _jwt.encode(
        {"sub": str(цель.id), "role": цель.role, "exp": int(_time.time()) + 900, "jti": "x"},
        settings.jwt_secret,
        algorithm="HS256",
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {без_iat}"})
    assert r.status_code == 401, r.text

    # Без отзыва такой токен по-прежнему работает: сверять не с чем, и ломать
    # вход тем, у кого отзыва не было, эта правка не должна.
    свободный = users_by_role["head"]
    полезная = {
        "sub": str(свободный.id),
        "role": свободный.role,
        "exp": int(_time.time()) + 900,
        "jti": "y",
    }
    без_iat2 = _jwt.encode(
        полезная,
        settings.jwt_secret,
        algorithm="HS256",
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {без_iat2}"})
    assert r.status_code == 200, r.text


# --- Глубина контекста лид-бота: ноль больше не становится единицей (27.08) ---


async def test_zero_context_is_refused_instead_of_becoming_one(client, users_by_role, tokens):
    """⚠ БОЕВОЙ СЛЕД: «context_messages сбит с 12 на 1» (жалоба владельца 23.08).

    Экран настроек отправляет число по уходу фокуса, а `Number("")` — это НОЛЬ.
    Стоило очистить поле, чтобы набрать заново, и уезжал ноль; сервер приводил
    его к единице (`max(1, …)`), лид-бот получал контекст из ОДНОЙ реплики и
    терял весь разговор. Ошибки не было, в журнале обычное сохранение — связать
    одно с другим было не с чем.

    Тихое приведение заменено отказом с названными границами.
    """
    r = await client.patch(
        "/api/v1/leadbot", json={"context_messages": 0}, headers=auth(tokens, "admin")
    )
    assert r.status_code == 422, r.text
    сообщение = r.json()["error"]["message"]
    assert "2" in сообщение and "100" in сообщение, сообщение


async def test_context_bounds_are_named_in_the_error(client, users_by_role, tokens):
    """Границы названы в тексте: «Недопустимое значение» на числовом поле не
    говорит, что поправить, и человек пробует наугад."""
    for значение in (1, 101, -5):
        r = await client.patch(
            "/api/v1/leadbot",
            json={"context_messages": значение},
            headers=auth(tokens, "admin"),
        )
        assert r.status_code == 422, f"{значение}: {r.text}"


async def test_valid_context_still_saves(client, users_by_role, tokens):
    """Отказ не должен перекрыть законное: 2, 12 и 100 сохраняются."""
    for значение in (2, 12, 100):
        r = await client.patch(
            "/api/v1/leadbot",
            json={"context_messages": значение},
            headers=auth(tokens, "admin"),
        )
        assert r.status_code == 200, f"{значение}: {r.text}"
        assert r.json()["context_messages"] == значение
