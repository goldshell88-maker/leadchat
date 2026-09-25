"""OAuth Авито (07 §1.1.4 + 01 §4): обмен кода, одноразовый refresh под локом,
state-валидация callback'а, RBAC ручек подключения.

HTTP к Авито мокается respx; URL — от settings.avito_api_base
(в тестовом окружении это дефолтный https://api.avito.ru).
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import deps
from app.api.routes import avito_connect
from app.core.config import settings
from app.integrations.avito.client import AvitoClient
from app.integrations.avito.errors import AvitoAuthError, AvitoUnavailable, RateLimited
from app.main import create_app
from app.models import AvitoAccount
from app.services import avito_accounts as accounts_service
from app.services import avito_accounts as svc_accounts
from app.services import crypto
from app.services.avito_accounts import refresh_tokens


def accounts_redirect(query: str) -> str:
    """Куда OAuth-callback возвращает человека (01 §4.3, хвост спринта 2 «б»).

    Location абсолютный и ведёт на ФРОНТ (FRONTEND_BASE_URL), а не на origin
    API — в dev это Vite :5173. Считаем ожидание из настроек, чтобы ассерты
    пережили смену дефолта.
    """
    return f"{settings.frontend_base}/settings/accounts?{query}"


TOKEN_URL = f"{settings.avito_api_base}/token"
SELF_URL = f"{settings.avito_api_base}/core/v1/accounts/self"
WEBHOOK_URL = f"{settings.avito_api_base}/messenger/v3/webhook"
#: Список подписок аккаунта. Спрашивается ПЕРЕД регистрацией нашей (28.08):
#: Авито держит на аккаунт ровно одну, и вытеснение чужой обязано попадать в
#: журнал строкой уровня error, а не проходить молча.
SUBSCRIPTIONS_URL = f"{settings.avito_api_base}/messenger/v1/subscriptions"

AVITO_USER_ID = 111222333


# --- фикстуры ----------------------------------------------------------------


@pytest.fixture
def app(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    redis,  # fakeredis из conftest
) -> FastAPI:
    """То же приложение, что в conftest; router avito_connect регистрирует
    app/main.py (защитный try-import зоны конвейера) — добираем его сами,
    только если он ещё не подключён."""
    application = create_app()
    paths = {getattr(route, "path", None) for route in application.routes}
    if "/api/v1/avito/connect-url" not in paths:
        application.include_router(avito_connect.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    application.dependency_overrides[deps.get_db] = override_get_db
    application.dependency_overrides[deps.get_redis] = lambda: redis
    return application


@pytest.fixture
def make_account(db: AsyncSession) -> Callable[..., Awaitable[AvitoAccount]]:
    async def _make(
        *,
        avito_user_id: int = AVITO_USER_ID,
        access_token: str = "acc-old",
        refresh_token: str = "ref-old",
        status: str = "active",
    ) -> AvitoAccount:
        account = AvitoAccount(
            title="LP-Тест",
            avito_user_id=avito_user_id,
            access_token_enc=crypto.encrypt_token(access_token),
            refresh_token_enc=crypto.encrypt_token(refresh_token),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=status,
            webhook_secret="whsec-test",
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return account

    return _make


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode())
    return {k: v[0] for k, v in parsed.items()}


def _token_json(access: str = "new-a", refresh: str = "new-r") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 86400,
    }


# --- клиент: token exchange и коды ошибок ------------------------------------


@respx.mock
async def test_exchange_code_posts_authorization_code_grant() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_json("a1", "r1"))
    )
    data = await AvitoClient().exchange_code("the-code")
    assert data["access_token"] == "a1" and data["refresh_token"] == "r1"
    form = _form(route.calls.last.request)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == settings.avito_client_id
    assert form["client_secret"] == settings.avito_client_secret


@respx.mock
async def test_client_401_raises_auth_error() -> None:
    respx.get(SELF_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AvitoAuthError):
        await AvitoClient().get_self("expired")


@respx.mock
async def test_client_429_raises_rate_limited_with_retry_after() -> None:
    respx.get(f"{settings.avito_api_base}/messenger/v2/accounts/{AVITO_USER_ID}/chats").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    with pytest.raises(RateLimited) as exc_info:
        await AvitoClient().get_chats("tok", AVITO_USER_ID)
    assert exc_info.value.retry_after == 7


# --- refresh: одноразовость и лок (07 §1.1.4) --------------------------------


@respx.mock
async def test_concurrent_refresh_hits_token_endpoint_once(
    make_account, db, db_sessionmaker, redis
) -> None:
    """Пятеро разом: одноразовый refresh сожжён один раз, свежий токен у всех.

    КАЖДЫЙ УЧАСТНИК — СО СВОЕЙ СЕССИЕЙ, и это не украшение теста. Раньше все
    пятеро звались с одним объектом аккаунта, а конкуренция здесь бывает
    только между процессами: воркер доставки, планировщик, обработчик вебхука.
    Общий объект давал картину, которой в жизни не существует — победитель
    менял поле прямо под проигравшими, и те не могли отличить «мне достался
    свежий токен» от «мой токен подменили в момент чтения».

    РАНЬШЕ ЗДЕСЬ ПРОВЕРЯЛОСЬ `sum(results) == 1` — и это закрепляло беду #28:
    четверо проигравших получали False и уходили со старым, уже мёртвым
    токеном, хотя победитель положил в базу новый.
    """
    account = await make_account()
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))

    async def one_process() -> bool:
        async with db_sessionmaker() as own:
            mine = await own.get(AvitoAccount, account.id)
            assert mine is not None
            return await refresh_tokens(mine, own, redis)

    results = await asyncio.gather(*(one_process() for _ in range(5)))

    assert route.call_count == 1  # лок сработал: refresh одноразовый, сожгли ровно один
    assert all(results)  # и при этом никто не остался с мёртвым токеном

    await db.refresh(account)
    assert crypto.decrypt_token(account.refresh_token_enc) == "new-r"  # новый refresh сохранён
    assert crypto.decrypt_token(account.access_token_enc) == "new-a"
    assert account.status == "active"


@respx.mock
async def test_refresh_400_marks_needs_reauth(make_account, db, redis) -> None:
    account = await make_account()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    assert await refresh_tokens(account, db, redis) is False
    assert account.status == "needs_reauth"


@respx.mock
async def test_refresh_400_after_concurrent_rotation_retries_with_new(
    make_account, db, db_sessionmaker, redis
) -> None:
    """Второй вызов со старым refresh: 400 не значит «отзыв» — в БД уже лежит
    новый токен (ротация конкурентом). needs_reauth НЕ ставится, ретрай с новым."""
    account = await make_account(refresh_token="ref-stale")
    # другой процесс уже обменял refresh: в БД — ref-rotated, у нас в памяти — ref-stale
    async with db_sessionmaker() as other:
        fresh = await other.get(AvitoAccount, account.id)
        assert fresh is not None
        fresh.refresh_token_enc = crypto.encrypt_token("ref-rotated")
        await other.commit()

    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400),  # сгоревший ref-stale
            httpx.Response(200, json=_token_json("a2", "r2")),  # ретрай с ref-rotated
        ]
    )
    assert await refresh_tokens(account, db, redis) is True
    assert route.call_count == 2
    assert _form(route.calls.last.request)["refresh_token"] == "ref-rotated"
    assert account.status == "active"  # needs_reauth не ставился
    assert crypto.decrypt_token(account.refresh_token_enc) == "r2"


async def test_refresh_lock_released_on_exception(make_account, db, redis) -> None:
    """Сеть упала -> лок не завис (иначе аккаунт застрянет на 30 сек+).

    Ждём `AvitoUnavailable`, а не голый `httpx.ConnectError`: клиент теперь
    оборачивает сетевые сбои в свой тип. Смысл проверки прежний — здесь
    заперто освобождение лока, а не имя исключения. Само оборачивание
    появилось потому, что вызывающий код ловил `OSError`, от которого httpx не
    наследуется, и «Авито недоступен» доезжало до человека как «Внутренняя
    ошибка сервера» (tests/unit/test_connect_by_keys.py).
    """
    account = await make_account()
    with respx.mock:
        respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(AvitoUnavailable):
            await refresh_tokens(account, db, redis)
    assert await redis.get(f"lock:token:{account.id}") is None


@respx.mock
async def test_foreign_lock_never_burns_the_one_time_refresh(
    make_account, db, redis, monkeypatch
) -> None:
    """Чужой лок — не наш refresh. Ждём и не трогаем token-эндпоинт.

    Здесь лок никто не отпускает, поэтому проверяется ещё и то, что ожидание
    ограничено: без предела аккаунт вставал бы на все 30 секунд жизни лока.
    """
    monkeypatch.setattr(svc_accounts, "COMPETITOR_WAIT_SECONDS", 0.2)
    account = await make_account()
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))
    await redis.set(f"lock:token:{account.id}", "1", ex=30)

    started = time.monotonic()
    assert await refresh_tokens(account, db, redis) is False
    assert time.monotonic() - started < 2  # ждём предел, а не жизнь лока
    assert route.call_count == 0  # чужой лок — не сжигаем одноразовый refresh


@respx.mock
async def test_loser_waits_and_takes_the_winner_token(
    make_account, db, db_sessionmaker, redis
) -> None:
    """ГЛАВНАЯ ПРОВЕРКА #28: проигравший в гонке не остаётся с мёртвым токеном.

    ЧТО ЛОМАЛОСЬ. Токен протух, и два процесса одновременно получили 401.
    Первый забрал лок и пошёл обновляться. Второму `refresh_tokens` мгновенно
    отвечала False — «занято». Второй перечитывал строку, видел статус
    «active», решал, что аккаунт здоров, и повторял запрос ТЕМ ЖЕ протухшим
    токеном. Второй 401 подряд — и отправка падала с ошибкой доступа на
    совершенно здоровом канале. Клиент не получал ответ, а в интерфейсе
    аккаунт выглядел исправным: он таким и был.

    Корень — в том, что False означала сразу две несовместимые вещи: «сейчас
    обновляет другой, подожди» и «обновить не вышло, нужен повторный вход».
    Вызывающий код не мог их различить и выбирал неверное поведение для обеих.
    """
    account = await make_account()
    lock_key = f"lock:token:{account.id}"
    await redis.set(lock_key, "1", ex=30)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))

    async def winner() -> None:
        await asyncio.sleep(0.1)  # победитель ходит в Авито
        async with db_sessionmaker() as other:
            fresh = await other.get(AvitoAccount, account.id)
            assert fresh is not None
            fresh.access_token_enc = crypto.encrypt_token("acc-from-winner")
            fresh.token_expires_at = datetime.now(UTC) + timedelta(hours=24)
            await other.commit()
        await redis.delete(lock_key)

    running = asyncio.create_task(winner())
    assert await refresh_tokens(account, db, redis) is True
    await running

    assert crypto.decrypt_token(account.access_token_enc) == "acc-from-winner"
    assert route.call_count == 0  # свой refresh не жгли — взяли чужой результат


@respx.mock
async def test_loser_does_not_report_success_when_winner_failed(
    make_account, db, db_sessionmaker, redis
) -> None:
    """Победитель упёрся в отзыв доступа — проигравший не должен бодриться."""
    account = await make_account()
    lock_key = f"lock:token:{account.id}"
    await redis.set(lock_key, "1", ex=30)

    async def winner() -> None:
        await asyncio.sleep(0.1)
        async with db_sessionmaker() as other:
            fresh = await other.get(AvitoAccount, account.id)
            assert fresh is not None
            fresh.status = "needs_reauth"
            await other.commit()
        await redis.delete(lock_key)

    running = asyncio.create_task(winner())
    assert await refresh_tokens(account, db, redis) is False
    await running
    assert account.status == "needs_reauth"


# --- connect-url: RBAC и state -----------------------------------------------


async def test_connect_url_admin_only(client, tokens) -> None:
    for role in ("head", "manager", "observer"):
        r = await client.get(
            "/api/v1/avito/connect-url", headers={"Authorization": f"Bearer {tokens[role]}"}
        )
        assert r.status_code == 403, (role, r.text)

    r = await client.get(
        "/api/v1/avito/connect-url", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(settings.avito_auth_url)
    assert parse_qs(urlparse(url).query)["state"][0]


async def _obtain_state(client, tokens) -> str:
    r = await client.get(
        "/api/v1/avito/connect-url", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    return parse_qs(urlparse(r.json()["url"]).query)["state"][0]


@respx.mock
async def test_callback_full_flow_and_one_time_state(
    client, tokens, db_sessionmaker, redis, monkeypatch
) -> None:
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_json("cb-a", "cb-r"))
    )
    respx.get(SELF_URL).mock(
        return_value=httpx.Response(200, json={"id": AVITO_USER_ID, "name": "LP-Москва"})
    )
    respx.post(SUBSCRIPTIONS_URL).mock(return_value=httpx.Response(200, json={"subscriptions": []}))
    webhook_route = respx.post(WEBHOOK_URL).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    enqueued: list[uuid.UUID] = []

    async def fake_enqueue(account_id: uuid.UUID) -> None:
        enqueued.append(account_id)

    monkeypatch.setattr(accounts_service, "enqueue_backfill", fake_enqueue)

    state = await _obtain_state(client, tokens)
    r = await client.get(f"/api/v1/avito/callback?code=oauth-code&state={state}")
    assert r.status_code == 302
    assert r.headers["location"] == accounts_redirect("connected=1")

    async with db_sessionmaker() as check:
        account = await accounts_service.get_account_by_avito_user_id(check, AVITO_USER_ID)
        assert account is not None
        assert account.title == "LP-Москва"
        assert account.status == "active"
        # токены в БД шифрованные, расшифровываются в исходные
        assert crypto.decrypt_token(account.access_token_enc) == "cb-a"
        assert crypto.decrypt_token(account.refresh_token_enc) == "cb-r"
        assert account.webhook_secret

        # вебхук зарегистрирован на PUBLIC_BASE_URL (дефолт — https://DOMAIN)
        hook = json.loads(webhook_route.calls.last.request.content)
        assert hook["url"] == (
            f"https://{settings.domain}/api/hooks/avito/{account.id}"
            f"?secret={account.webhook_secret}"
        )
        # ИСТОРИЯ ГРУЗИТСЯ ПРИ ПОДКЛЮЧЕНИИ (решение владельца 17.08 — осознанный
        # разворот решения от 8 августа; страж-тест сработал как задумано).
        # Контекст: каналы параллельно работали в Jivo, и при переезде на
        # LeadChat прошлая переписка аккаунта нужна диспетчерам.
        assert len(enqueued) == 1, "история ставится в очередь при подключении"
        assert enqueued[0] == account.id

    # state одноразовый (GETDEL): повторный callback с тем же state — отказ без обмена кода
    calls_before = token_route.call_count
    r2 = await client.get(f"/api/v1/avito/callback?code=oauth-code&state={state}")
    assert r2.status_code == 302
    assert r2.headers["location"] == accounts_redirect("error=oauth_failed")
    assert token_route.call_count == calls_before


@respx.mock
async def test_callback_unknown_state_rejected(client) -> None:
    token_route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))
    r = await client.get("/api/v1/avito/callback?code=x&state=forged-state")
    assert r.status_code == 302
    assert r.headers["location"] == accounts_redirect("error=oauth_failed")
    assert token_route.call_count == 0  # без валидного state кода не обмениваем


@respx.mock
async def test_reconnect_other_avito_account_mismatch(
    client, tokens, make_account, db_sessionmaker, monkeypatch
) -> None:
    """Callback reconnect-флоу: админ авторизовал ДРУГОЙ аккаунт Авито —
    ничего не пишем, 302 ?error=account_mismatch (01 §4.4)."""
    account = await make_account(avito_user_id=999000111, status="needs_reauth")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))
    respx.get(SELF_URL).mock(
        return_value=httpx.Response(200, json={"id": AVITO_USER_ID, "name": "Другой"})
    )

    async def fake_enqueue(account_id: uuid.UUID) -> None:  # pragma: no cover
        raise AssertionError("backfill не должен ставиться при mismatch")

    monkeypatch.setattr(accounts_service, "enqueue_backfill", fake_enqueue)

    r = await client.post(
        f"/api/v1/avito-accounts/{account.id}/reconnect",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]

    r2 = await client.get(f"/api/v1/avito/callback?code=c&state={state}")
    assert r2.status_code == 302
    assert r2.headers["location"] == accounts_redirect("error=account_mismatch")

    async with db_sessionmaker() as check:
        fresh = await check.get(AvitoAccount, account.id)
        assert fresh is not None
        assert fresh.status == "needs_reauth"  # запись не тронута
        assert crypto.decrypt_token(fresh.refresh_token_enc) == "ref-old"


async def test_accounts_list_hides_tokens(client, tokens, make_account) -> None:
    account = await make_account()
    r = await client.get(
        "/api/v1/avito-accounts", headers={"Authorization": f"Bearer {tokens['head']}"}
    )
    assert r.status_code == 200, r.text  # head видит список (accounts:read, 01 §4)
    body = r.json()
    assert body["page"]["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(account.id)
    assert item["webhook"]["status"] == "not_registered"
    assert item["backfill"]["status"] == "idle"
    dumped = json.dumps(body)
    # токены и webhook_secret не отдаются никогда (01 §4.1)
    assert "token_enc" not in dumped
    assert "whsec-test" not in dumped
    assert "acc-old" not in dumped and "ref-old" not in dumped

    r_manager = await client.get(
        "/api/v1/avito-accounts", headers={"Authorization": f"Bearer {tokens['manager']}"}
    )
    assert r_manager.status_code == 403


# --- ручное обновление токена канала (блок 8.3) ------------------------------


class TestManualTokenRefresh:
    """«Обновить токен» вместо полного переподключения.

    ЗАЧЕМ ОТДЕЛЬНАЯ КНОПКА. Переподключение — это поход в Авито, вход под
    учёткой канала и подтверждение доступа: минуты и чужой пароль под рукой.
    Обновление токена не требует ни того, ни другого. Разница видна ровно в
    тот момент, когда канал внезапно замолчал: одно чинится за секунду,
    другое требует человека с доступом к аккаунту Авито.
    """

    @respx.mock
    async def test_it_refreshes_without_going_through_oauth(
        self, client, tokens, make_account
    ) -> None:
        account = await make_account()
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_token_json("man-a", "man-r"))
        )

        r = await client.post(
            f"/api/v1/avito-accounts/{account.id}/refresh-token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert r.status_code == 200, r.text
        assert route.call_count == 1
        assert r.json()["status"] == "active"
        # Ответ — обычная карточка канала, без единого следа токенов (01 §4.1).
        assert "man-a" not in json.dumps(r.json())

    @respx.mock
    async def test_a_revoked_channel_is_told_the_truth(
        self, client, tokens, make_account, db_sessionmaker
    ) -> None:
        """Авито отозвал доступ — обновлять нечего, и это надо сказать прямо.

        Молчаливое «ок» здесь было бы худшим исходом: администратор ушёл бы
        уверенный, что починил, а канал продолжил бы молчать.
        """
        account = await make_account()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))

        r = await client.post(
            f"/api/v1/avito-accounts/{account.id}/refresh-token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["details"]["reason"] == "needs_reauth"

        async with db_sessionmaker() as s:
            row = await s.get(AvitoAccount, account.id)
            assert row is not None and row.status == "needs_reauth"

    async def test_an_already_revoked_channel_is_not_retried(
        self, client, tokens, make_account
    ) -> None:
        """У канала в needs_reauth обновлять нечего — в Авито не ходим вовсе."""
        account = await make_account(status="needs_reauth")
        r = await client.post(
            f"/api/v1/avito-accounts/{account.id}/refresh-token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["details"]["reason"] == "needs_reauth"

    async def test_manual_token_refresh_is_admin_only(self, client, tokens, make_account) -> None:
        """Право `accounts:manage` — только у администратора.

        В общей матрице RBAC этой ручки нет: `_format` подставляет
        account_id="x", а путь объявлен как uuid, и ALLOW-ветка упёрлась бы в
        422 разбора пути. Поэтому право заперто здесь.
        """
        account = await make_account()
        url = f"/api/v1/avito-accounts/{account.id}/refresh-token"
        for role in ("head", "manager", "observer"):
            r = await client.post(url, headers={"Authorization": f"Bearer {tokens[role]}"})
            assert r.status_code == 403, (role, r.text)
        assert (await client.post(url)).status_code == 401

    @respx.mock
    async def test_a_parallel_refresh_is_not_burned_twice(
        self, client, tokens, make_account, redis
    ) -> None:
        """refresh_token одноразовый: пока его меняет планировщик, вторая
        попытка сожгла бы его и оставила канал без доступа."""
        account = await make_account()
        await redis.set(f"lock:token:{account.id}", "1", ex=30)
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_json()))

        r = await client.post(
            f"/api/v1/avito-accounts/{account.id}/refresh-token",
            headers={"Authorization": f"Bearer {tokens['admin']}"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["details"]["reason"] == "refresh_in_progress"
        assert route.call_count == 0  # в Авито не ходили вовсе
