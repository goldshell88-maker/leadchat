"""Auth flow tests (07 §1.1): success/401, lockout after 10 failures,
refresh rotation + reuse detection, logout revocation, invite flow."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import REFRESH_COOKIE_NAME, issue_invite
from app.models import AuditLog, User
from app.models.notification import Notification
from app.services.login_guard import LOGIN_FAIL_WINDOW_SECONDS
from app.services.login_guard import _email_key as _email_key
from tests.unit.conftest import DEFAULT_PASSWORD

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


async def _login(
    client: httpx.AsyncClient, email: str, password: str = DEFAULT_PASSWORD, **extra
) -> httpx.Response:
    return await client.post(LOGIN, json={"email": email, "password": password, **extra})


def _set_refresh_cookie(client: httpx.AsyncClient, value: str) -> None:
    client.cookies.clear()  # the jar may still hold a newer rotation of the cookie
    client.cookies.set(REFRESH_COOKIE_NAME, value, domain="testserver", path="/api/v1/auth")


class TestLogin:
    async def test_success_returns_token_user_and_cookie(self, client, make_user):
        user = await make_user("anna@leadchat.test", role="manager", full_name="Анна")
        r = await _login(client, "anna@leadchat.test", remember=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        assert body["user"]["id"] == str(user.id)
        assert body["user"]["role"] == "manager"
        assert "refresh" not in {k.lower() for k in body}  # refresh never in the body

        set_cookie = r.headers["set-cookie"]
        assert REFRESH_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Path=/api/v1/auth" in set_cookie
        assert "Max-Age" in set_cookie  # remember: true -> persistent cookie

        me = await client.get(ME, headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200

    async def test_email_is_case_insensitive(self, client, make_user):
        await make_user("case@leadchat.test")
        r = await _login(client, "Case@Leadchat.Test")
        assert r.status_code == 200

    async def test_remember_false_gives_session_cookie(self, client, make_user):
        await make_user("s@leadchat.test")
        r = await _login(client, "s@leadchat.test", remember=False)
        assert r.status_code == 200
        assert "Max-Age" not in r.headers["set-cookie"]

    async def test_wrong_password_401_invalid_credentials(self, client, make_user):
        await make_user("bob@leadchat.test")
        r = await _login(client, "bob@leadchat.test", password="wrong-password-123")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"

    async def test_unknown_email_same_401(self, client):
        r = await _login(client, "ghost@leadchat.test")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"

    async def test_inactive_user_403(self, client, make_user):
        await make_user("off@leadchat.test", is_active=False)
        r = await _login(client, "off@leadchat.test")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"

    async def test_login_writes_audit(self, client, make_user, db: AsyncSession):
        user = await make_user("audit@leadchat.test")
        await _login(client, "audit@leadchat.test")
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "auth.login")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == user.id

    async def test_error_envelope_has_request_id(self, client):
        r = await _login(client, "ghost@leadchat.test")
        err = r.json()["error"]
        assert err["request_id"].startswith("req_")
        assert r.headers["x-request-id"] == err["request_id"]


class TestLockout:
    async def test_locked_after_10_failures_even_with_correct_password(self, client, make_user):
        await make_user("brute@leadchat.test")
        for _ in range(10):
            r = await _login(client, "brute@leadchat.test", password="wrong-password-123")
            assert r.status_code == 401
        r = await _login(client, "brute@leadchat.test")  # correct password now
        assert r.status_code == 403
        err = r.json()["error"]
        assert err["code"] == "account_locked"
        assert err["details"]["retry_after_sec"] >= 1

    async def test_failures_below_limit_do_not_lock(self, client, make_user):
        await make_user("fine@leadchat.test")
        for _ in range(9):
            await _login(client, "fine@leadchat.test", password="wrong-password-123")
        r = await _login(client, "fine@leadchat.test")
        assert r.status_code == 200

    async def test_window_slides_on_every_failure(self, client, make_user, redis):
        """Регрессия 07 §4.1 A6: окно счётчика продлевается на КАЖДОЙ неудаче.

        С фиксированным окном (expire только на первой попытке) блокировка на
        проде не срабатывала вовсе: nginx-зона `login` (10r/m, 05 §3.1) держит
        темп 1 попытка в 6 с, счётчик успевал истечь до 11-й. Проверяем не
        «10 подряд» (так баг не виден), а именно продление TTL.
        """
        email = "slow-brute@leadchat.test"
        await make_user(email)
        key = _email_key(email)

        await _login(client, email, password="wrong-password-123")
        assert await redis.ttl(key) == LOGIN_FAIL_WINDOW_SECONDS

        # имитируем «прошло почти всё окно» перед следующей попыткой
        await redis.expire(key, 3)
        await _login(client, email, password="wrong-password-123")
        assert await redis.ttl(key) == LOGIN_FAIL_WINDOW_SECONDS
        assert int(await redis.get(key)) == 2  # счётчик при этом не сбросился


class TestOneOfficeOneAddress:
    """Тринадцать человек за одним внешним адресом (блокер Б3).

    Раньше защита считала сырые промахи с адреса и блокировала по ним. Десять
    опечаток разных людей за минуту — и не входит НИКТО, включая тех, кто
    набирает пароль верно. Успешный вход счётчик адреса не сбрасывал, а каждая
    новая опечатка продлевала окно: офис мог не войти вовсе.

    Считаем не промахи, а РАЗНЫЕ почты: это подпись перебора учёток, а не
    рабочего утра.
    """

    async def test_a_dozen_colleagues_mistyping_do_not_lock_each_other(self, client, make_user):
        """Двенадцать человек ошиблись — тринадцатый входит.

        Промахов тут больше прежнего порога в десять раз, и именно так
        выглядит понедельник в офисе.
        """
        for i in range(12):
            await make_user(f"office{i}@leadchat.test")
            for _ in range(9):  # каждый ошибается, но не до своего личного лимита
                await _login(client, f"office{i}@leadchat.test", password="wrong-password-123")

        await make_user("newcomer@leadchat.test")
        r = await _login(client, "newcomer@leadchat.test")
        assert r.status_code == 200, r.text

    async def test_one_persons_typos_never_touch_the_others(self, client, make_user):
        """Свой пароль можно забыть хоть двадцать раз — соседям всё равно."""
        await make_user("forgetful@leadchat.test")
        await make_user("colleague@leadchat.test")
        for _ in range(20):
            await _login(client, "forgetful@leadchat.test", password="wrong-password-123")

        r = await _login(client, "colleague@leadchat.test")
        assert r.status_code == 200, r.text

    async def test_spraying_many_accounts_still_gets_blocked(self, client, make_user):
        """А вот перебор по списку учёток адрес блокирует — ради этого счётчик и есть."""
        from app.services.login_guard import LOGIN_SPRAY_LIMIT

        for i in range(LOGIN_SPRAY_LIMIT):
            await _login(client, f"victim{i}@leadchat.test", password="wrong-password-123")

        await make_user("innocent@leadchat.test")
        r = await _login(client, "innocent@leadchat.test")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "account_locked"

    async def test_a_successful_login_clears_the_suspicion(self, client, make_user, redis):
        """Вошёл — значит пароль знает, и его промах перестаёт числиться уликой.

        Без этого счётчик адреса только рос: сбрасывалась одна почта, а
        множество промахнувшихся копилось до самого истечения окна.
        """
        from app.services.login_guard import _email_digest, _ip_key

        email = "proved@leadchat.test"
        await make_user(email)
        await _login(client, email, password="wrong-password-123")

        keys = [k async for k in redis.scan_iter("login_fail:ip_emails:*")]
        assert keys, "промах должен попасть в множество адреса"
        assert await redis.sismember(keys[0], _email_digest(email))

        assert (await _login(client, email)).status_code == 200
        assert not await redis.sismember(keys[0], _email_digest(email))
        assert _ip_key  # ключ строится одной функцией — проверяем, что она та же


class TestLockoutNotifiesTheAdmin:
    """14 §2.2: «Учётная запись заблокирована подбором» обязана дойти до админа.

    Иначе сотрудник просто перестаёт работать и молчит, а администратор узнаёт
    об этом от него же — через час, лично.
    """

    @staticmethod
    async def _lock_out(client, email: str) -> None:
        for _ in range(10):
            await _login(client, email, password="wrong-password-123")
        r = await _login(client, email)  # 11-я попытка — уже блокировка
        assert r.status_code == 403, r.text

    @staticmethod
    async def _notifications(db: AsyncSession) -> list[Notification]:
        rows = await db.execute(
            select(Notification).where(Notification.kind == "auth.account_locked")
        )
        return list(rows.scalars())

    async def test_a_locked_out_employee_reaches_the_admin_with_a_button(
        self, client, make_user, db: AsyncSession
    ):
        user = await make_user("locked@leadchat.test", full_name="Пётр Ковалёв")
        await self._lock_out(client, "locked@leadchat.test")

        rows = await self._notifications(db)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.audience == "admin"  # рассылка администраторам, не адресное
        assert row.severity == "warning"
        assert "Пётр Ковалёв" in row.title  # заголовок человеческий, без кодов
        assert row.entity_type == "user" and row.entity_id == str(user.id)
        # Кнопка берётся из каталога по виду события — «Выслать новую ссылку».
        from app.services import notifications as center

        assert center.action_for(row.kind) is not None

    async def test_the_center_is_not_flooded_while_the_lock_holds(
        self, client, make_user, db: AsyncSession
    ):
        """Одна строка на окно блокировки, а не на каждую попытку.

        Уведомление сидит под той же защитой, что и запись в журнал
        (`_should_log_lockout`): иначе неаутентифицированная ручка стала бы
        генератором строк в центре уведомлений.
        """
        await make_user("flood@leadchat.test")
        await self._lock_out(client, "flood@leadchat.test")
        for _ in range(5):
            await _login(client, "flood@leadchat.test")

        rows = await self._notifications(db)
        assert len(rows) == 1, rows
        assert rows[0].repeat_count == 1

    async def test_a_made_up_email_writes_nothing_at_all(self, client, db: AsyncSession):
        """Иначе центр уведомлений засоряется с улицы, без единого пароля."""
        await self._lock_out(client, "nobody@leadchat.test")
        assert await self._notifications(db) == []


class TestRefresh:
    async def test_refresh_rotates_and_returns_new_access(self, client, make_user):
        await make_user("ref@leadchat.test")
        await _login(client, "ref@leadchat.test", remember=True)
        old_cookie = client.cookies.get(REFRESH_COOKIE_NAME)

        r = await client.post(REFRESH)
        assert r.status_code == 200, r.text
        assert r.json()["expires_in"] == 900
        new_cookie = client.cookies.get(REFRESH_COOKIE_NAME)
        assert new_cookie and new_cookie != old_cookie

    async def test_old_cookie_is_invalid_after_rotation(self, client, make_user):
        await make_user("rot@leadchat.test")
        await _login(client, "rot@leadchat.test")
        old_cookie = client.cookies.get(REFRESH_COOKIE_NAME)
        assert (await client.post(REFRESH)).status_code == 200

        _set_refresh_cookie(client, old_cookie)
        r = await client.post(REFRESH)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

    async def test_reuse_revokes_whole_chain(self, client, make_user):
        await make_user("chain@leadchat.test")
        await _login(client, "chain@leadchat.test")
        first = client.cookies.get(REFRESH_COOKIE_NAME)
        assert (await client.post(REFRESH)).status_code == 200
        current = client.cookies.get(REFRESH_COOKIE_NAME)

        _set_refresh_cookie(client, first)
        assert (await client.post(REFRESH)).status_code == 401  # reuse detected

        _set_refresh_cookie(client, current)  # the fresh token died with the chain
        assert (await client.post(REFRESH)).status_code == 401

    async def test_refresh_without_cookie_401(self, client):
        r = await client.post(REFRESH)
        assert r.status_code == 401


class TestLogout:
    async def test_logout_revokes_refresh_and_clears_cookie(self, client, make_user):
        await make_user("out@leadchat.test")
        login = await _login(client, "out@leadchat.test")
        access = login.json()["access_token"]
        cookie = client.cookies.get(REFRESH_COOKIE_NAME)

        r = await client.post(LOGOUT, headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 204
        assert 'lc_refresh=""' in r.headers["set-cookie"]

        _set_refresh_cookie(client, cookie)
        assert (await client.post(REFRESH)).status_code == 401

    async def test_logout_requires_auth(self, client):
        assert (await client.post(LOGOUT)).status_code == 401


class TestMe:
    async def test_me_returns_permissions(self, client, make_user):
        await make_user("perm@leadchat.test", role="manager")
        login = await _login(client, "perm@leadchat.test")
        r = await client.get(
            ME, headers={"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "manager"
        assert body["permissions"] == [
            "conversations:read",
            "messages:send",
            "conversations:manage",
            "notes:read",
            "notes:write",
            "templates:own",
            "stats:own",
            # Разбор диалогов открыт всем ролям с 27.08 (решение владельца).
            # Порядок — каталожный, из `PERMISSIONS`: /auth/me отдаёт список
            # стабильно отсортированным, и фронт на это опирается.
            "dialogs:read",
        ]

    async def test_me_without_token_401(self, client):
        r = await client.get(ME)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

    async def test_me_with_garbage_token_401(self, client):
        r = await client.get(ME, headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


class TestLockoutNotice:
    async def test_a_failed_notice_neither_breaks_the_answer_nor_eats_the_budget(
        self, client, redis, make_user, monkeypatch
    ):
        """⚠ Сбой уведомления не превращает 403 в 500 и не съедает бюджет окна.

        Бюджет «одно уведомление на окно блокировки» занимается через SET NX ДО
        отправки. Раньше `notify_now` шёл без обёртки, и падение давало сразу два
        последствия: человек получал 500 вместо честного «заблокировано, подождите»,
        а маркер оставался занятым — то есть администратор не узнавал НИЧЕГО до конца
        окна. При действующем локе счётчик попыток не растёт, повторные запросы
        попадают в ту же ветку и молча упираются в занятый маркер.

        Смысл механизма (14 §2.2) ровно в том, чтобы администратор узнал, что человек
        не может работать. Тут он не узнавал именно тогда, когда что-то сломалось.
        """
        from app.api.routes import auth as auth_routes

        user = await make_user("locked@leadchat.test", role="manager", full_name="Иван")

        # Загоняем в блокировку: попытки с неверным паролем.
        for _ in range(20):
            r = await client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": "wrong-password-x"}
            )
            if r.status_code == 403:
                break
        assert r.status_code == 403, "учётка не заблокировалась — тест проверяет не то"

        # Ломаем отправку и снимаем маркер, чтобы попасть в ветку уведомления.
        await redis.delete(f"auth:lock_audited:{auth_routes._email_hash(user.email)}")

        async def взрыв(*a, **k):
            raise RuntimeError("центр уведомлений недоступен")

        monkeypatch.setattr(auth_routes, "notify_now", взрыв)

        r = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "wrong-password-x"}
        )
        assert r.status_code == 403, "сбой уведомления превратил честный отказ в 500"
        assert r.json()["error"]["code"] == "account_locked"

        # ⚠ ГЛАВНОЕ: бюджет освобождён — следующая попытка уведомит заново.
        assert (
            await redis.get(f"auth:lock_audited:{auth_routes._email_hash(user.email)}") is None
        ), "маркер остался занятым — администратор не узнает до конца окна блокировки"


class TestInviteFlow:
    async def test_a_refused_invite_does_not_burn_the_link(self, client, redis, make_user):
        """⚠ ОТКАЗ НЕ ГАСИТ ССЫЛКУ. Самый важный тест этого класса.

        ЧТО БЫЛО. `consume_invite` (redis GETDEL, необратимо) стоял ПЕРВЫМ, до всех
        проверок. Приглашение, выписанное на учётку с уже заданным паролем, отвечало
        422 «пароль уже задан» — и в тот же миг самоуничтожалось. Повтор давал 404,
        страница приглашения — 404, и КАЖДОЕ новое приглашение умирало так же с
        первого нажатия. Выйти из этого изнутри было нельзя.

        Поймано на живом стенде 14 августа, когда владелец не мог войти: в логе
        подряд `POST /invite/accept 422`, `POST /invite/accept 404`,
        `GET /invite/<токен> 404`.

        ⚠ ПОЧЕМУ ОБЫЧНЫЙ ТЕСТ ВЫШЕ ЭТОГО НЕ ЛОВИЛ. Он проверяет слабый пароль, а тот
        отсеивается pydantic'ом ДО тела ручки — до `consume_invite` дело не доходит
        вовсе. Ловится только отказ, который выносит САМА ручка.
        """
        user = await make_user("burned@leadchat.test", role="manager", full_name="Ольга")
        # У пользователя пароль уже есть — это и есть условие отказа 422.
        token, _ = await issue_invite(redis, str(user.id))

        r = await client.post(
            "/api/v1/auth/invite/accept",
            json={"token": token, "password": "very-strong-password"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"]["reason"] == "password_already_set"

        # Ссылка ЖИВА: страница приглашения по-прежнему открывается…
        r = await client.get(f"/api/v1/auth/invite/{token}")
        assert r.status_code == 200, "отказ сжёг ссылку — человек остался без пути назад"
        # …и повторная попытка получает тот же честный отказ, а не 404 «просрочено».
        r = await client.post(
            "/api/v1/auth/invite/accept",
            json={"token": token, "password": "very-strong-password"},
        )
        assert r.status_code == 422, "второй отказ превратился в 404 — токен всё-таки сгорел"

    async def test_invite_url_points_at_the_spa_not_at_the_bare_domain(self):
        """⚠ Ссылка ведёт туда, где живёт экран, а не всегда на боевой домен.

        Стояло f"https://{settings.domain}/invite/…" — то есть боевой адрес даже со
        стенда. 14 августа владелец получил на локальной машине ссылку на
        chat.partner-lead-centre.ru с токеном из локальной базы; открыть её нельзя
        нигде. Со staging-машины так же выписалось бы приглашение сотруднику — письмом,
        на боевой адрес, с токеном, которого там нет.

        `frontend_base` заведён ровно для этого и до сих пор не имел потребителей.
        """
        from app.core.config import settings
        from app.core.security import invite_url

        assert invite_url("inv_x").startswith(settings.frontend_base), (
            "ссылка снова собирается мимо frontend_base"
        )
        assert invite_url("inv_x").endswith("/invite/inv_x")

    async def test_full_invite_flow(self, client, redis, make_user, db_sessionmaker):
        user = await make_user("new@leadchat.test", invited=True, role="manager", full_name="Пётр")
        token, _expires = await issue_invite(redis, str(user.id))

        # validate before showing the form
        r = await client.get(f"/api/v1/auth/invite/{token}")
        assert r.status_code == 200
        assert r.json() == {"email": "new@leadchat.test", "full_name": "Пётр"}

        # weak password -> 400 validation_error, token NOT consumed
        r = await client.post(
            "/api/v1/auth/invite/accept", json={"token": token, "password": "short"}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "validation_error"
        assert r.json()["error"]["details"]["fields"][0]["field"] == "password"

        # accept -> logged in immediately (login-shaped body + cookie)
        r = await client.post(
            "/api/v1/auth/invite/accept",
            json={"token": token, "password": "very-strong-password"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == "new@leadchat.test"
        assert REFRESH_COOKIE_NAME in r.headers["set-cookie"]

        # one-time: same token again -> 404 invite_expired
        r = await client.post(
            "/api/v1/auth/invite/accept",
            json={"token": token, "password": "very-strong-password"},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "invite_expired"

        # ⚠ ССЫЛКА НЕ СГОРАЕТ НА ОТКАЗЕ — отдельный тест ниже, самый важный в классе.

        # the new password works via normal login
        r = await _login(client, "new@leadchat.test", password="very-strong-password")
        assert r.status_code == 200

        # audit trail recorded
        async with db_sessionmaker() as session:
            actions = (
                (await session.execute(select(AuditLog.action).where(AuditLog.user_id == user.id)))
                .scalars()
                .all()
            )
        assert "user.invite_accepted" in actions

    async def test_unknown_invite_token_404(self, client):
        r = await client.get("/api/v1/auth/invite/inv_nonexistent")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "invite_expired"

    async def test_accept_when_password_already_set_422(self, client, redis, make_user):
        user = await make_user("haspass@leadchat.test")  # real argon2 hash
        token, _ = await issue_invite(redis, str(user.id))
        r = await client.post(
            "/api/v1/auth/invite/accept",
            json={"token": token, "password": "irrelevant-pass-123"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "unprocessable"

    async def test_login_before_accept_fails(self, client, redis, make_user, db_sessionmaker):
        """The unreachable placeholder hash never matches any password."""
        user = await make_user("pending@leadchat.test", invited=True)
        async with db_sessionmaker() as session:
            stored = (await session.get(User, user.id)).password_hash
        r = await _login(client, "pending@leadchat.test", password=stored)
        assert r.status_code == 401
