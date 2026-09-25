"""Имитатор Авито даёт РАЗНЫЕ аккаунты на разные ключи.

ЖАЛОБА ЗАКАЗЧИКА: «я могу подключить только 1 аккаунт», «всё присваивается к
фейк-авито».

ПРИЧИНА. На вход `client_credentials` имитатор отдавал один и тот же
`DEFAULT_ACCOUNT_UID` для любых ключей. Система получала тот же
`avito_user_id`, узнавала в нём уже подключённый канал и обновляла ту же
строку вместо создания второй. Второй канал подключить было физически нельзя.

ПОЧЕМУ ЭТО ВАЖНО ПРОВЕРЯТЬ, ХОТЯ ЭТО «ВСЕГО ЛИШЬ МОК». У заказчика девять
аккаунтов, и вся раздача диалогов, права по каналам и статистика строятся
вокруг того, что каналов много. Имитатор, умеющий ровно один аккаунт, делает
непроверяемым всё это разом — а выглядит поломка как поломка системы, а не
мока: человек видит «подключено», а канал один.

Проверяется поведение, а не реализация: устойчивость (те же ключи — тот же
аккаунт) и различимость (разные ключи — разные аккаунты).
"""

import pytest

from fake_avito.main import STATE, _uid_for_client_id

pytestmark = pytest.mark.anyio

KEYS_A = {"grant_type": "client_credentials", "client_id": "app-A", "client_secret": "s"}
KEYS_B = {"grant_type": "client_credentials", "client_id": "app-B", "client_secret": "s"}


async def _token(payload: dict[str, str]) -> dict:
    from fake_avito.main import token

    class _Request:
        headers = {"content-type": "application/json"}

        async def json(self) -> dict[str, str]:
            return payload

    return await token(_Request())  # type: ignore[arg-type]


def _uid_of(pair: dict) -> int:
    return STATE["access_tokens"][pair["access_token"]]["account_user_id"]


async def test_different_keys_are_different_accounts() -> None:
    """Главная проверка файла — ровно то, на что наткнулся заказчик."""
    assert _uid_of(await _token(KEYS_A)) != _uid_of(await _token(KEYS_B))


async def test_same_keys_are_always_the_same_account() -> None:
    """Переподключение теми же ключами не должно плодить каналы.

    Если бы номер выдавался случайно, каждое переподключение создавало бы
    новый канал, а старый оставался бы висеть мёртвым — и через неделю в
    списке было бы двадцать строк вместо девяти.
    """
    assert _uid_of(await _token(KEYS_A)) == _uid_of(await _token(KEYS_A))


async def test_the_account_is_ready_to_work_with() -> None:
    """Аккаунт заводится сразу: без записи в `accounts` профиль отдать нечем."""
    uid = _uid_of(await _token(KEYS_B))
    assert STATE["accounts"][uid]["user_id"] == uid
    assert STATE["accounts"][uid]["name"].strip()
    assert uid in STATE["chats"]


async def test_no_refresh_token_for_this_grant() -> None:
    """У `client_credentials` настоящий Авито refresh не выдаёт — и мы не выдаём.

    Иначе система училась бы обновляться способом, которого в бою нет.
    """
    assert "refresh_token" not in await _token(KEYS_A)


def test_the_number_looks_like_an_avito_one() -> None:
    """Девять знаков, как у настоящих идентификаторов, и никогда не ноль."""
    for client_id in ("app-A", "app-B", "", "очень-длинный-client-id-" * 10):
        uid = _uid_for_client_id(client_id)
        assert 100_000_000 <= uid < 1_000_000_000
