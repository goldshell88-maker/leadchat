"""Подключение аккаунта Авито по ключам — без согласия и переходов.

РЕШЕНИЕ ЗАКАЗЧИКА ОТ 8 АВГУСТА: «давай так, чтобы для привязки нам был нужен
только Client Secret и ID».

ПОЧЕМУ ЭТО ВООБЩЕ ВОЗМОЖНО. У Авито есть вход `client_credentials`, и все
нужные методы мессенджера его принимают — проверено по официальной
спецификации (docs/26). Значит согласие пользователя не требуется: пара ключей
сама по себе даёт доступ к аккаунту, которому она принадлежит.

ПОЧЕМУ ЭТО НУЖНО ИМЕННО ЗДЕСЬ. Аккаунтов девять, пароли от них у разных людей,
и владелец системы их у себя не держит. «Пришлите ключи» — просьба, которую
можно выполнить, не отдавая пароль; «пришлите пароль» — нельзя.

ЧЕМ ПЛАТИМ. При этом входе refresh-токена нет: доступ переполучается теми же
ключами, значит ключи приходится хранить. Здесь заперто, что они шифруются и
наружу не возвращаются.
"""

import uuid

import pytest

from app.integrations.avito.errors import AvitoApiError, AvitoAuthError
from app.models import AvitoAccount
from app.services import avito_accounts as svc
from app.services import crypto

pytestmark = pytest.mark.anyio

KEYS = {"client_id": "app-42", "client_secret": "секрет-42"}


def _as(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
def avito_ok(monkeypatch):
    """Авито принимает ключи и рассказывает, чей это аккаунт.

    Подписок на аккаунте нет: это «чистый» случай, когда забирать не у кого.
    Проверка чужой подписки (SCEN-32) живёт в своём наборе ниже — здесь она
    подменена намеренно, иначе каждый тест этого файла ходил бы в сеть за
    списком подписок и молча получал бы «Авито недоступен».
    """
    seen: dict[str, object] = {}

    async def token(self, client_id=None, client_secret=None):
        seen["keys"] = (client_id, client_secret)
        return {"access_token": "AT", "expires_in": 3600}

    async def profile(self, access_token):
        seen["token"] = access_token
        return {"id": 990100, "name": "Парт - 7 / Ист"}

    async def webhook(*_a, **_kw):
        return True

    async def no_subscriptions(self, access_token):
        return []

    from app.integrations.avito.client import AvitoClient

    monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
    monkeypatch.setattr(AvitoClient, "get_self", profile)
    monkeypatch.setattr(AvitoClient, "list_subscriptions", no_subscriptions)
    monkeypatch.setattr(svc, "register_webhook", webhook)
    return seen


# ------------------------------------------------------------- сам путь


async def test_two_fields_are_enough(client, tokens, db, avito_ok) -> None:
    """Главная проверка файла: ключи — и аккаунт подключён."""
    response = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["avito_user_id"] == 990100
    assert body["title"] == "Парт - 7 / Ист"
    assert body["status"] == "active"
    # Ключи ушли в Авито ровно те, что прислали.
    assert avito_ok["keys"] == ("app-42", "секрет-42")


async def test_secret_is_stored_encrypted_and_never_returned(client, tokens, db, avito_ok) -> None:
    """Ключи хранятся: без них нечем переполучить доступ. Значит — шифрованно.

    И наружу они не уходят никогда — как пароль пользователя: менять можно,
    читать нельзя.
    """
    response = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
    )
    assert KEYS["client_secret"] not in response.text

    account = await db.get(AvitoAccount, uuid.UUID(response.json()["id"]))
    assert account is not None
    assert account.client_id == "app-42"
    assert account.client_secret_enc is not None
    # В базе лежит шифртекст, а не сам секрет.
    assert KEYS["client_secret"].encode() not in bytes(account.client_secret_enc)
    assert crypto.decrypt_token(bytes(account.client_secret_enc)) == "секрет-42"

    listed = await client.get("/api/v1/avito-accounts", headers=_as("admin", tokens))
    assert KEYS["client_secret"] not in listed.text


async def test_same_account_twice_does_not_duplicate(client, tokens, avito_ok) -> None:
    """Повторное подключение того же аккаунта обновляет ключи, а не плодит строки."""
    first = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
    )
    second = await client.post(
        "/api/v1/avito-accounts/connect",
        json={"client_id": "app-99", "client_secret": "новый-секрет"},
        headers=_as("admin", tokens),
    )

    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["avito_user_id"] == 990100


# --------------------------------------------------- понятные отказы


async def test_wrong_keys_say_so_plainly(client, tokens, monkeypatch) -> None:
    """Сюда приходит владелец бизнеса, и «HTTP 401» ему ничего не говорит."""
    from app.integrations.avito.client import AvitoClient

    async def reject(*_a, **_kw):
        raise AvitoAuthError(status=401)

    monkeypatch.setattr(AvitoClient, "client_credentials_token", reject)

    response = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "bad_keys"
    assert "Client ID" in response.json()["error"]["message"]


async def test_missing_messenger_rights_are_named(client, tokens, monkeypatch) -> None:
    """Ключи верные, а прав на мессенджер приложению не выдали.

    Это отдельный случай и самый обидный: человек уверен, что всё сделал,
    а работать не будет. Отказ обязан назвать, чего именно не хватает.
    """
    from app.integrations.avito.client import AvitoClient

    async def token(*_a, **_kw):
        return {"access_token": "AT", "expires_in": 3600}

    async def forbidden(*_a, **_kw):
        raise AvitoApiError("нет доступа к мессенджеру", status=403)

    monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
    monkeypatch.setattr(AvitoClient, "get_self", forbidden)

    response = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
    )

    assert response.status_code == 502
    assert "messenger:read" in response.json()["error"]["message"]


# ------------------------------------------- обновление доступа теми же ключами


async def test_access_is_renewed_with_the_same_keys(db, redis, make_avito_account, monkeypatch):
    """У этого входа refresh-токена нет — доступ переполучается ключами.

    Вся механика одноразового refresh (лок, разбор «сгорел или отозвали»)
    сюда не относится: ключи постоянные, и повторный запрос безопасен.
    """
    from app.integrations.avito.client import AvitoClient

    account = await make_avito_account(990200)
    account.client_id = "app-42"
    account.client_secret_enc = crypto.encrypt_token("секрет-42")
    await db.commit()

    used: dict[str, object] = {}

    async def token(self, client_id=None, client_secret=None):
        used["keys"] = (client_id, client_secret)
        return {"access_token": "СВЕЖИЙ", "expires_in": 3600}

    async def must_not_be_called(*_a, **_kw):
        raise AssertionError("refresh-токен здесь использовать нельзя — его нет")

    monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
    monkeypatch.setattr(AvitoClient, "refresh_token", must_not_be_called)

    assert await svc.refresh_tokens(account, db, redis) is True
    assert used["keys"] == ("app-42", "секрет-42")
    assert crypto.decrypt_token(bytes(account.access_token_enc)) == "СВЕЖИЙ"


async def test_revoked_keys_ask_for_new_ones(db, redis, make_avito_account, monkeypatch):
    """Ключи отозвали в кабинете Авито — это то же, что отозванный доступ."""
    from app.integrations.avito.client import AvitoClient

    account = await make_avito_account(990300)
    account.client_id = "app-42"
    account.client_secret_enc = crypto.encrypt_token("секрет-42")
    await db.commit()

    async def reject(*_a, **_kw):
        raise AvitoAuthError(status=401)

    monkeypatch.setattr(AvitoClient, "client_credentials_token", reject)

    assert await svc.refresh_tokens(account, db, redis) is False
    assert account.status == "needs_reauth"


# --------------------------------------------------------------- права


async def test_connecting_is_admin_only(client, tokens) -> None:
    response = await client.post(
        "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("manager", tokens)
    )
    assert response.status_code == 403


async def test_anonymous_cannot_connect(client) -> None:
    assert (await client.post("/api/v1/avito-accounts/connect", json=KEYS)).status_code == 401


def _break_only_avito(monkeypatch, error: Exception) -> None:
    """Уронить сеть ТОЛЬКО для запросов в Авито.

    Подменять `AvitoClient._request` нельзя: исправление живёт внутри него, и
    тест зеленел бы, ничего не проверяя. Подменять `httpx.AsyncClient.request`
    целиком тоже нельзя — через тот же класс тест сам стучится в наше API, и
    падал бы запрос теста, а не запрос в Авито.

    Отличаем по адресу: тест зовёт наше API относительным путём
    («/api/v1/…»), а клиент Авито всегда ходит по абсолютному. Значит роняем
    только абсолютные — и только не на `testserver`.
    """
    import httpx

    original = httpx.AsyncClient.request

    async def maybe_fail(self, method, url, *args, **kwargs):  # noqa: ANN001, ANN202
        target = str(url)
        if target.startswith("http") and "testserver" not in target:
            raise error
        return await original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", maybe_fail)


# ------------------------------------------- отказы, которые ловились мимо


class TestFailuresReachThePersonIntact:
    """Два отказа, которые доезжали до человека не тем, чем были.

    Оба нашёл разбор пути подключения, и оба воспроизводимы.
    """

    async def test_network_failure_says_so_instead_of_internal_error(
        self, client, tokens, monkeypatch
    ) -> None:
        """«Авито недоступен» — а не «Внутренняя ошибка сервера».

        ЧТО БЫЛО. Ручка ловила `OSError`, рассчитывая поймать сетевой сбой. Но
        исключения httpx от `OSError` НЕ НАСЛЕДУЮТСЯ: у `ConnectError` цепочка
        идёт `NetworkError -> TransportError -> RequestError -> HTTPError`.
        Отказ пролетал мимо ветки и превращался в HTTP 500.

        Для владельца это выглядело как «ничего не привязывается»: красный тост
        без объяснения, окно не закрылось, список не обновился. Он жал ещё раз
        и ещё раз — с тем же результатом.
        """
        import httpx

        _break_only_avito(monkeypatch, httpx.ConnectError("[Errno 61] Connection refused"))

        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 502, response.text
        assert response.json()["error"]["details"]["reason"] == "unreachable"
        assert "связаться с Авито" in response.json()["error"]["message"]

    async def test_unreachable_is_not_mistaken_for_missing_rights(
        self, client, tokens, monkeypatch
    ) -> None:
        """Обрыв сети не должен объясняться правами на мессенджер.

        `AvitoUnavailable` — наследник `AvitoApiError`, поэтому без отдельной
        ветки ВЫШЕ общей человек получил бы совет «проверьте messenger:read»
        там, где просто не было связи, и пошёл бы чинить исправное приложение.
        """
        import httpx

        _break_only_avito(monkeypatch, httpx.ReadTimeout("timed out"))

        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert "messenger:read" not in response.json()["error"]["message"]

    async def test_webhook_failure_does_not_fail_a_saved_connection(
        self, client, tokens, db, monkeypatch
    ) -> None:
        """Подписка на вебхук падает — подключение всё равно состоялось.

        Вызов стоит ПОСЛЕ commit: аккаунт уже в базе. Исключение здесь давало
        человеку 500, и он считал, что подключения нет, — а оно было и
        обнаруживалось само после перезагрузки страницы. Хуже: он подключал
        заново и заново, каждый раз видя ошибку.
        """
        import httpx

        from app.integrations.avito.client import AvitoClient
        from app.services import avito_accounts as svc

        async def token(self, client_id=None, client_secret=None):
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):
            return {"id": 990400, "name": "Канал с капризным вебхуком"}

        async def no_subscriptions(self, access_token):
            return []

        async def boom(*_a, **_kw):
            raise httpx.ReadTimeout("подписка не прошла")

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        # Список подписок подменяется НАМЕРЕННО, хотя проверяется здесь другое:
        # без подмены запрос уходит в сеть по-настоящему (проверка чужой
        # подписки, SCEN-32) и тест ждёт таймаута пятнадцать секунд, а на
        # машине с поднятым имитатором ещё и получает живой ответ.
        monkeypatch.setattr(AvitoClient, "list_subscriptions", no_subscriptions)
        monkeypatch.setattr(svc, "register_webhook", boom)

        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 201, response.text
        assert response.json()["avito_user_id"] == 990400


class TestTakingTheChannelFromJivoNeedsAnAnswer:
    """Чужая подписка на аккаунте — остановка, а не примечание (SCEN-32).

    ЧТО ЭТО ЗА БЕДА. Авито держит на аккаунт РОВНО ОДНУ подписку на события.
    Подключение регистрирует нашу сразу и без вопросов — значит вытесняет ту,
    что стоит сейчас. На боевых аккаунтах заказчика сейчас стоит JivoChat с
    тринадцатью диспетчерами: они перестают получать обращения в ту же
    секунду, сообщения об этом нет нигде, а снаружи это выглядит как затишье.
    Во время переезда «Подключить» был одним кликом до потери боевого канала.

    Проверок здесь четыре, и каждая закрывает свой способ всё испортить:
    отказ есть, отказ ничего не пишет в базу, подтверждение работает, и
    неудачная проверка не превращается в запрет подключать каналы вообще.
    """

    @pytest.fixture
    def jivo_is_subscribed(self, monkeypatch):
        """Авито отвечает: на аккаунте стоит подписка чужой системы."""
        from app.integrations.avito.client import AvitoClient

        async def token(self, client_id=None, client_secret=None):
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):
            return {"id": 990500, "name": "Боевой канал под Jivo"}

        async def subscriptions(self, access_token):
            return [{"url": "https://webhook.jivosite.com/avito/12345", "version": "3.0.0"}]

        registered: list[str] = []

        async def webhook(account, redis):
            registered.append(str(account.id))
            return True

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        monkeypatch.setattr(AvitoClient, "list_subscriptions", subscriptions)
        monkeypatch.setattr(svc, "register_webhook", webhook)
        return registered

    async def test_connecting_over_a_stranger_is_refused(
        self, client, tokens, jivo_is_subscribed
    ) -> None:
        """Главная проверка файла: молча канал не отбирается.

        Отказ обязан назвать и причину, и чужой адрес: выбор «забирать или
        нет» иначе делается вслепую.
        """
        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["details"]["reason"] == "subscription_taken"
        assert error["details"]["subscriptions"] == ["https://webhook.jivosite.com/avito/12345"]
        assert "одну подписку" in error["message"]
        assert jivo_is_subscribed == [], "подписка не должна была регистрироваться"

    async def test_the_refusal_leaves_no_half_connected_channel(
        self, client, tokens, db, jivo_is_subscribed
    ) -> None:
        """Отказ не оставляет следов в базе.

        Человек не соглашался ни на что; строка в списке каналов после отказа
        — это «а я думал, не подключилось», из которого вырастает второе
        нажатие уже вслепую.
        """
        await client.post("/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens))

        from sqlalchemy import select

        accounts = list((await db.execute(select(AvitoAccount))).scalars())
        assert accounts == []

    async def test_confirmation_lets_the_channel_be_taken(
        self, client, tokens, jivo_is_subscribed
    ) -> None:
        """Подтвердил — забираем. Отказ обязан быть проходимым, иначе переезд встанет."""
        response = await client.post(
            "/api/v1/avito-accounts/connect",
            json={**KEYS, "takeover_confirmed": True},
            headers=_as("admin", tokens),
        )

        assert response.status_code == 201, response.text
        assert response.json()["avito_user_id"] == 990500
        assert len(jivo_is_subscribed) == 1, "канал забрали — подписка обязана встать"

    async def test_our_own_subscription_is_not_a_stranger(
        self, client, tokens, monkeypatch
    ) -> None:
        """Наша же подписка не повод спрашивать разрешения.

        Иначе повторное подключение уже работающего канала (обновление ключей)
        требовало бы подтверждения «отобрать у самих себя» — и человек привык
        бы жать «да» не читая, ровно к тому дню, когда за этим будет стоять
        живой Jivo.
        """
        from app.integrations.avito.client import AvitoClient

        ours = f"{svc.public_base_url()}/api/hooks/avito/{uuid.uuid4()}?secret=s"

        async def token(self, client_id=None, client_secret=None):
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):
            return {"id": 990600, "name": "Наш же канал"}

        async def subscriptions(self, access_token):
            return [{"url": ours, "version": "3.0.0"}]

        async def webhook(*_a, **_kw):
            return True

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        monkeypatch.setattr(AvitoClient, "list_subscriptions", subscriptions)
        monkeypatch.setattr(svc, "register_webhook", webhook)

        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 201, response.text

    async def test_an_unanswered_check_does_not_block_connecting(
        self, client, tokens, monkeypatch
    ) -> None:
        """Не смогли спросить — не запрещаем.

        Глагол у метода подписок в спецификации Авито записан двояко, прав на
        него может не быть, Авито может не ответить. Считать любую из этих
        причин доказательством чужой подписки — значит сделать подключение
        каналов невозможным по догадке о чужом API.
        """
        from app.integrations.avito.client import AvitoClient

        async def token(self, client_id=None, client_secret=None):
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):
            return {"id": 990700, "name": "Канал, про подписки которого не спросить"}

        async def unavailable(self, access_token):
            raise AvitoApiError("метод недоступен", status=404)

        async def webhook(*_a, **_kw):
            return True

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        monkeypatch.setattr(AvitoClient, "list_subscriptions", unavailable)
        monkeypatch.setattr(svc, "register_webhook", webhook)

        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 201, response.text


class TestAFailedSubscriptionIsNotSilent:
    """Провал подписки перестал выглядеть успехом (SCEN-35).

    ЧТО БЫЛО. Ответ 201, зелёное «Аккаунт подключён · Новые обращения пойдут в
    Чаты» — и ни одного обращения. Канал подключён по-настоящему: токены на
    месте, карточка в списке. Но подписки на события у него нет, и Авито нам
    не шлёт ничего. Худшее из сочетаний: система уверяет, что работает, а
    клиенты пишут в пустоту; узнают об этом по звонку клиента через сутки.
    """

    @pytest.fixture
    def avito_refuses_the_subscription(self, monkeypatch):
        from app.integrations.avito.client import AvitoClient

        async def token(self, client_id=None, client_secret=None):
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):
            return {"id": 990800, "name": "Канал без подписки"}

        async def no_subscriptions(self, access_token):
            return []

        async def refuse(*_a, **_kw):
            raise AvitoApiError("подписка не принята", status=502)

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        monkeypatch.setattr(AvitoClient, "list_subscriptions", no_subscriptions)
        monkeypatch.setattr(AvitoClient, "register_webhook", refuse)

    async def test_the_answer_says_the_channel_is_deaf(
        self, client, tokens, avito_refuses_the_subscription
    ) -> None:
        """В ответе состояние подписки — `failed`, а не «не зарегистрирован».

        Разница для человека решающая: одно читается как «ещё не делали»,
        другое — как «сделали и не вышло».
        """
        response = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert response.status_code == 201, response.text
        assert response.json()["webhook"]["status"] == "failed"

    async def test_admins_are_called_with_a_repair_button(
        self, client, tokens, db, avito_refuses_the_subscription
    ) -> None:
        """Главная проверка: о беде говорят тогда же, когда она случилась.

        Карточка канала правду показывает и без этого, но мастер закрывается
        зелёным тостом, и на карточку человек посмотрит не раньше, чем что-то
        заподозрит. Уведомление `webhook.lost` критично, поднимает красную
        плашку поверх экрана и несёт кнопку «Перерегистрировать».
        """
        from sqlalchemy import select

        from app.models import Notification
        from app.services.notifications import action_for

        await client.post("/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens))

        rows = list(
            (
                await db.execute(select(Notification).where(Notification.kind == "webhook.lost"))
            ).scalars()
        )
        assert rows, "подписка не встала, а администраторов никто не позвал"
        (row,) = rows
        assert row.severity == "critical"
        assert row.audience == "admin"
        assert "Канал без подписки" in (row.body or "")
        assert action_for("webhook.lost") is not None, "уведомление без кнопки починки"

    async def test_a_working_subscription_calls_nobody(self, client, tokens, db, avito_ok) -> None:
        """Обратная сторона: удачное подключение тревоги не поднимает.

        Без этой проверки «позвать всегда» прошло бы все остальные и приучило
        бы администраторов гасить красную плашку после каждого подключения.
        """
        from sqlalchemy import select

        from app.models import Notification

        await client.post("/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens))

        assert list((await db.execute(select(Notification))).scalars()) == []


class TestSameKeysTwiceAreNotTwoAccounts:
    """«Я могу подключить только 1 аккаунт» — вторая, неочевидная причина.

    При входе `client_credentials` пара ключей принадлежит ТОМУ аккаунту, где
    заведено приложение. Значит одни ключи на девять аккаунтов девять раз
    попадут в один и тот же канал: система найдёт совпадение по
    `avito_user_id` и обновит существующую строку.

    Само поведение правильное — переподключение не должно плодить дубликаты и
    не должно сбрасывать переименование канала. Неправильным было МОЛЧАНИЕ:
    оба исхода выглядели одинаково успешными.
    """

    async def test_answer_says_whether_a_channel_was_created(
        self, client, tokens, avito_ok
    ) -> None:
        """Главная проверка: ответ различает новый канал и обновление ключей."""
        first = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )
        second = await client.post(
            "/api/v1/avito-accounts/connect",
            json={"client_id": "app-99", "client_secret": "другие-ключи"},
            headers=_as("admin", tokens),
        )

        assert first.json()["created"] is True
        assert second.json()["created"] is False, (
            "второе подключение попало в тот же канал, но ответ об этом молчит"
        )
        assert second.json()["id"] == first.json()["id"]

    async def test_the_channel_keeps_its_name(self, client, tokens, avito_ok) -> None:
        """Переименование канала не сбрасывается повторным подключением.

        Это и есть причина, по которой человек видел ЧУЖОЕ название и решал,
        что подключился не тот аккаунт.
        """
        created = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )
        await client.patch(
            f"/api/v1/avito-accounts/{created.json()['id']}",
            json={"title": "Парт - 7 / Ист"},
            headers=_as("admin", tokens),
        )

        again = await client.post(
            "/api/v1/avito-accounts/connect", json=KEYS, headers=_as("admin", tokens)
        )

        assert again.json()["title"] == "Парт - 7 / Ист"
        assert again.json()["created"] is False
