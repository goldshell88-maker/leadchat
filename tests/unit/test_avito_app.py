"""Ключи приложения Авито задаются из интерфейса, а не в файле на сервере.

ЗАЧЕМ. Подключение канала идёт через OAuth Авито, и для него нужны `client_id`
и `client_secret` из кабинета разработчика. Пока они жили только в `.env`,
владелец не мог подключить первый боевой аккаунт без разработчика с доступом по
ssh — что он и сказал прямо: «я не могу ввести данные для добавления аккаунта».

Главное, что здесь заперто: СЕКРЕТ НЕ ВОЗВРАЩАЕТСЯ НАРУЖУ. Показать его один
раз — значит показать каждому, кто заглянет через плечо.
"""

import pytest

from app.core.config import settings
from app.services import avito_app

pytestmark = pytest.mark.anyio


def _as(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


BODY = {
    "client_id": "app-777",
    "client_secret": "очень-секретное-значение",
    "api_base": "https://api.avito.ru",
    "auth_url": "https://www.avito.ru/oauth",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш процесса живёт между тестами и подсунул бы чужие значения."""
    avito_app._drop_cache()
    yield
    avito_app._drop_cache()


# ------------------------------------------------------------------ секрет


async def test_secret_never_comes_back(client, tokens) -> None:
    """Главная проверка файла."""
    saved = await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))
    assert saved.status_code == 200

    for response in (
        saved,
        await client.get("/api/v1/avito/app", headers=_as("admin", tokens)),
    ):
        body = response.json()
        assert body["secret_set"] is True
        assert "client_secret" not in body
        assert BODY["client_secret"] not in response.text


async def test_empty_secret_means_do_not_touch_it(client, tokens, db) -> None:
    """Сохранение адреса не должно стирать ключ.

    Экран не показывает секрет, поэтому при сохранении он приходит пустым.
    Без отдельного значения «не меняли» каждая правка адреса обнуляла бы ключ,
    и подключение переставало работать по причине, которую никто не свяжет с
    последним действием.
    """
    await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))

    response = await client.put(
        "/api/v1/avito/app",
        json={**BODY, "client_secret": None, "api_base": "https://api.avito.ru/v2"},
        headers=_as("admin", tokens),
    )

    assert response.status_code == 200
    assert response.json()["secret_set"] is True
    assert (await avito_app.load(db)).client_secret == BODY["client_secret"]


async def test_first_save_requires_a_secret(client, tokens, monkeypatch) -> None:
    monkeypatch.setattr(settings, "avito_client_secret", "", raising=False)

    response = await client.put(
        "/api/v1/avito/app",
        json={**BODY, "client_secret": None},
        headers=_as("admin", tokens),
    )

    assert response.status_code == 400
    assert "Client Secret" in response.json()["error"]["message"]


# ------------------------------------------------------- что видит система


async def test_saved_keys_take_effect_for_the_client(client, tokens, db) -> None:
    """Сохранили — и клиент Авито ходит уже туда.

    Иначе экран был бы обманом: значения сохранились, а система по-прежнему
    смотрит в имитатор.
    """
    from app.integrations.avito.client import AvitoClient, build_authorize_url

    await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))
    await avito_app.ensure_fresh(db)

    assert AvitoClient()._base == "https://api.avito.ru"
    assert build_authorize_url("st").startswith("https://www.avito.ru/oauth?")
    assert "client_id=app-777" in build_authorize_url("st")


async def test_reset_returns_to_server_settings(client, tokens, db) -> None:
    await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))

    response = await client.delete("/api/v1/avito/app", headers=_as("admin", tokens))

    assert response.status_code == 200
    assert response.json()["source"] == "env"
    assert (await avito_app.load(db)).client_id == settings.avito_client_id


async def test_addresses_must_look_like_addresses(client, tokens) -> None:
    response = await client.put(
        "/api/v1/avito/app",
        json={**BODY, "api_base": "avito.ru"},
        headers=_as("admin", tokens),
    )
    assert response.status_code == 400


async def test_live_flag_says_where_we_look(client, tokens) -> None:
    """«Боевой Авито или имитатор» — первый вопрос человека на этом экране."""
    live = await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))
    assert live.json()["live"] is True

    fake = await client.put(
        "/api/v1/avito/app",
        json={**BODY, "api_base": "http://fake-avito:8020", "auth_url": "http://x/fake-oauth"},
        headers=_as("admin", tokens),
    )
    assert fake.json()["live"] is False


async def test_redirect_uri_is_shown(client, tokens) -> None:
    """Адрес возврата надо вписать в кабинете Авито. Не совпадёт — подключение
    будет падать без внятной причины, и человек будет искать её часами."""
    response = await client.get("/api/v1/avito/app", headers=_as("admin", tokens))
    assert response.json()["redirect_uri"].endswith("/api/v1/avito/callback")


# --------------------------------------------------------------- права


@pytest.mark.parametrize("role", ["head", "manager", "observer"])
async def test_only_admin_touches_the_keys(client, tokens, role: str) -> None:
    assert (await client.get("/api/v1/avito/app", headers=_as(role, tokens))).status_code == 403
    assert (
        await client.put("/api/v1/avito/app", json=BODY, headers=_as(role, tokens))
    ).status_code == 403
    assert (await client.delete("/api/v1/avito/app", headers=_as(role, tokens))).status_code == 403


async def test_anonymous_gets_401(client) -> None:
    assert (await client.get("/api/v1/avito/app")).status_code == 401


# ------------------------------------------- ссылка согласия слово в слово


async def test_authorize_url_matches_the_real_avito_one(monkeypatch, db, client, tokens) -> None:
    """Ссылка согласия должна совпадать с настоящей ссылкой Авито.

    Заказчик прислал ту, которой пользовалась прежняя система:

        https://www.avito.ru/oauth?response_type=code&client_id=…
        &scope=messenger:read,messenger:write,user:read&state=…

    Отсюда два исправления, каждое из которых иначе всплыло бы в день
    подключения боевого аккаунта:

    * адрес с `www`. Без него Авито отвечает редиректом, а на редиректе
      параметры согласия теряются не всегда, но иногда — и разбирать такое
      «иногда» пришлось бы в самый неподходящий момент;
    * права в читаемом виде, без %3A и %2C. Функционально одно и то же, но
      совпадение с образцом снимает вопрос «а точно ли так?».
    """
    from app.integrations.avito.client import build_authorize_url

    await client.put(
        "/api/v1/avito/app",
        json={**BODY, "client_id": "AAMwP4K1c-ohLoWuRpHM"},
        headers=_as("admin", tokens),
    )
    await avito_app.ensure_fresh(db)

    url = build_authorize_url("ST")

    assert url.startswith("https://www.avito.ru/oauth?")
    assert "client_id=AAMwP4K1c-ohLoWuRpHM" in url
    assert "scope=messenger:read,messenger:write,user:read" in url
    assert "response_type=code" in url
    assert "state=ST" in url


# ------------------------------------------- истёкший токен даёт 403, не 401


@pytest.mark.parametrize("status", [401, 403])
async def test_expired_token_triggers_refresh_on_both_codes(status: int) -> None:
    """У Авито истёкший access-токен даёт 403, а не 401.

    Мы исходили из общепринятого 401, и 403 попадал в общую ветку ошибок:
    рефреш не запускался, и КАЖДЫЙ следующий запрос падал — до планового
    обновления токена за два часа до истечения. Отправка сообщений могла молча
    встать на часы, а выглядело бы это как «Авито не отвечает».

    Проверено по двум независимым источникам, в том числе по чужой рабочей
    интеграции, где ради этого заведена отдельная настройка
    `tokenExpiredStatusCode: 403`.
    """
    import httpx

    from app.integrations.avito.client import AvitoClient
    from app.integrations.avito.errors import AvitoAuthError

    response = httpx.Response(status, request=httpx.Request("GET", "https://api.avito.ru/x"))

    with pytest.raises(AvitoAuthError) as exc:
        AvitoClient._raise_for_status(response, "проверка")

    assert exc.value.status == status


async def test_other_client_errors_are_not_auth(monkeypatch) -> None:
    """404 и 400 рефрешем не лечатся — и лечить их им нельзя.

    Иначе каждая опечатка в пути тратила бы выдачу токена и пряталась за
    «токен не принят».
    """
    import httpx

    from app.integrations.avito.client import AvitoClient
    from app.integrations.avito.errors import AvitoApiError, AvitoAuthError

    for status in (400, 404, 500):
        response = httpx.Response(status, request=httpx.Request("GET", "https://api.avito.ru/x"))
        with pytest.raises(AvitoApiError) as exc:
            AvitoClient._raise_for_status(response, "проверка")
        assert not isinstance(exc.value, AvitoAuthError), status


# ------------------------------------- выход из имитатора без ключей приложения


class TestSwitchingOutOfTheImitator:
    """«Всё присваивается к фейк-авито» и «я могу подключить только 1 аккаунт».

    Две жалобы заказчика с одной причиной: система смотрела на встроенный
    имитатор, а выйти из него было нечем. Сохранение настроек требует пару
    ключей ПРИЛОЖЕНИЯ — а каналы подключаются своими ключами, и ключей
    приложения на этой системе может не быть вовсе. Интерфейс обходил это,
    подставляя в поля строку "unused": враньё в данных, которое однажды
    кто-нибудь прочитает как настоящий Client ID.
    """

    async def test_switch_to_live_needs_no_keys(self, client, tokens, db, monkeypatch) -> None:
        """Главная проверка: переключение проходит на чистой системе."""
        monkeypatch.setattr(settings, "avito_client_secret", "", raising=False)

        response = await client.post(
            "/api/v1/avito/app/mode", json={"live": True}, headers=_as("admin", tokens)
        )

        assert response.status_code == 200
        assert response.json()["live"] is True
        assert response.json()["api_base"] == "https://api.avito.ru"
        await avito_app.ensure_fresh(db)
        assert (await avito_app.load(db)).auth_url == "https://www.avito.ru/oauth"

    async def test_switching_keeps_the_secret(self, client, tokens, db) -> None:
        """Ключи, если они заданы, переключение адреса не трогает."""
        await client.put("/api/v1/avito/app", json=BODY, headers=_as("admin", tokens))

        await client.post(
            "/api/v1/avito/app/mode", json={"live": False}, headers=_as("admin", tokens)
        )
        await client.post(
            "/api/v1/avito/app/mode", json={"live": True}, headers=_as("admin", tokens)
        )

        config = await avito_app.load(db)
        assert config.client_secret == BODY["client_secret"]
        assert config.client_id == BODY["client_id"]

    async def test_switch_back_returns_to_the_server_setting(self, client, tokens) -> None:
        """Дорога назад обязана быть: «я тут напутал» случается."""
        await client.post(
            "/api/v1/avito/app/mode", json={"live": True}, headers=_as("admin", tokens)
        )

        response = await client.post(
            "/api/v1/avito/app/mode", json={"live": False}, headers=_as("admin", tokens)
        )

        assert response.json()["api_base"] == settings.avito_api_base.rstrip("/")

    @pytest.mark.parametrize("role", ["head", "manager", "observer"])
    async def test_only_admin_switches(self, client, tokens, role: str) -> None:
        response = await client.post(
            "/api/v1/avito/app/mode", json={"live": True}, headers=_as(role, tokens)
        )
        assert response.status_code == 403


class TestScreenAndCodeAgreeOnWhereWeGo:
    """«Ничего не привязывается»: экран говорил одно, код делал другое.

    ЧТО СЛУЧИЛОСЬ. Владелец нажал «Переключить на настоящий Авито». В базе
    api_base стал `https://api.avito.ru`, экран настроек честно показал «боевой
    Авито» — он читает базу. А подключение аккаунта создавало клиента обычным
    конструктором, который читает КЭШ ПРОЦЕССА, и кэш никто не обновил.

    Система продолжала ходить во встроенный имитатор. Тот на любые ключи
    отдавал один и тот же выдуманный аккаунт, поэтому каждая попытка привязки
    переписывала одну и ту же строку, и владелец видел ровно то, что и написал:
    ничего не привязывается.
    """

    async def test_connect_goes_where_the_screen_says(
        self, client, tokens, db, monkeypatch
    ) -> None:
        """Главная проверка: после переключения подключение идёт на БОЕВОЙ адрес."""
        from app.integrations.avito.client import AvitoClient

        # Процесс уже «прогрет» старым значением — ровно как боевой api после
        # перезапуска: кэш заполнен адресом имитатора из .env.
        avito_app._drop_cache()
        monkeypatch.setattr(settings, "avito_api_base", "http://fake-avito:8020", raising=False)
        await avito_app.ensure_fresh(db)
        assert AvitoClient()._base == "http://fake-avito:8020"

        await client.post(
            "/api/v1/avito/app/mode", json={"live": True}, headers=_as("admin", tokens)
        )

        seen: dict[str, str] = {}

        async def token(self, client_id=None, client_secret=None):  # noqa: ANN001, ANN202
            seen["base"] = self._base
            return {"access_token": "AT", "expires_in": 3600}

        async def profile(self, access_token):  # noqa: ANN001, ANN202
            return {"id": 555111222, "name": "Настоящий аккаунт"}

        async def webhook(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
            return True

        async def no_subscriptions(self, access_token):  # noqa: ANN001, ANN202
            # Проверка чужой подписки перед подключением (SCEN-32) — тоже
            # поход в Авито. Без подмены он уходит в сеть по-настоящему: тест
            # ждал бы таймаута, а на машине с поднятым имитатором получил бы
            # живой ответ и стал бы зависеть от того, что там сейчас настроено.
            return []

        from app.services import avito_accounts as svc

        monkeypatch.setattr(AvitoClient, "client_credentials_token", token)
        monkeypatch.setattr(AvitoClient, "get_self", profile)
        monkeypatch.setattr(AvitoClient, "list_subscriptions", no_subscriptions)
        monkeypatch.setattr(svc, "register_webhook", webhook)

        response = await client.post(
            "/api/v1/avito-accounts/connect",
            json={"client_id": "app-1", "client_secret": "s-1"},
            headers=_as("admin", tokens),
        )

        assert response.status_code == 201
        assert seen["base"] == "https://api.avito.ru", (
            f"подключение ушло не туда, куда показывает экран — пошло в {seen.get('base')!r}"
        )
