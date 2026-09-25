"""ЗНАМЕНАТЕЛЬ ТРЕВОГИ СЧИТАЕТСЯ НА САМОМ ДЕЛЕ.

⚠ БОЕВОЙ СЛУЧАЙ 28.08. Тревога «Авито не отвечает» считала только ОТКАЗЫ и
срабатывала на пятом за час. В бою за час было 100 отказов при 285 удачных
обращениях: канал работал с рябью, а человеку приходило «перестают работать
сверка, история, отправка ответов».

Порог теперь спрашивает и долю — но доля бесполезна, если знаменатель никто не
считает: при нулевых попытках проверка честно откатывается к старому поведению
«решаем по числу», и правка тихо не работает. Диверсия «убрать вызов
`_отметить_попытку`» проверки сторожа НЕ РОНЯЛА — они ставят ключ в Redis сами.

Поэтому здесь проверяется не сторож, а сам клиент: каждое обращение к Авито
обязано увеличить счётчик попыток.
"""

import httpx
import pytest
import respx

from app.core import redis as redis_mod
from app.core.config import settings
from app.integrations.avito.client import AVITO_DOWN_KEY, AVITO_TRIES_KEY, AvitoClient

pytestmark = pytest.mark.anyio

CHATS_URL = f"{settings.avito_api_base}/messenger/v2/accounts/770777/chats"


@pytest.fixture
def подменённый_redis(monkeypatch, redis):
    monkeypatch.setattr(redis_mod, "get_client", lambda: redis)
    return redis


@respx.mock
async def test_a_successful_call_counts_as_an_attempt(подменённый_redis) -> None:
    """Удачное обращение — тоже попытка.

    Без этого знаменатель равнялся бы числу отказов, доля всегда была бы
    единицей, и порог по доле не отсекал бы ничего.
    """
    respx.get(CHATS_URL).mock(return_value=httpx.Response(200, json={"chats": []}))
    await AvitoClient().get_chats("tok", 770777)

    assert int(await подменённый_redis.get(AVITO_TRIES_KEY)) == 1
    assert await подменённый_redis.get(AVITO_DOWN_KEY) is None


@respx.mock
async def test_a_failed_call_counts_in_both(подменённый_redis) -> None:
    """Отказ увеличивает и числитель, и знаменатель.

    Попытку считаем ДО похода: иначе при полностью лежащей площадке доля
    считалась бы от нуля, то есть не считалась бы вовсе.
    """
    from app.integrations.avito.errors import AvitoUnavailable

    respx.get(CHATS_URL).mock(side_effect=httpx.ConnectTimeout("нет связи"))
    with pytest.raises(AvitoUnavailable):
        await AvitoClient().get_chats("tok", 770777)

    assert int(await подменённый_redis.get(AVITO_TRIES_KEY)) == 1
    assert int(await подменённый_redis.get(AVITO_DOWN_KEY)) == 1


@respx.mock
async def test_the_counter_lives_exactly_one_hour(подменённый_redis) -> None:
    """Час — тот же, что у счётчика отказов.

    Разъедься сроки, и доля считалась бы от разных отрезков времени: вчерашние
    попытки против сегодняшних отказов дали бы тревогу тем позже, чем спокойнее
    была ночь.
    """
    respx.get(CHATS_URL).mock(return_value=httpx.Response(200, json={"chats": []}))
    await AvitoClient().get_chats("tok", 770777)

    ttl = await подменённый_redis.ttl(AVITO_TRIES_KEY)
    assert 0 < ttl <= 3600
