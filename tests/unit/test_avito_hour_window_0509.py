"""ЧАС В ТРЕВОГЕ «АВИТО НЕ ОТВЕЧАЕТ» — НАСТОЯЩИЙ ЧАС, А НЕ «ПОКА ИДЁТ ТРАФИК».

⚠ БОЕВОЙ ЗАМЕР 05.09. Тревога не могла сработать никогда. В счётчиках стояло
`incr` и безусловный `expire`, то есть срок обновлялся на КАЖДОМ обращении к
Авито, а обращений идёт около 158 за полминуты. Замер на живом сервере: TTL
ключа попыток за 30 секунд вырос 3599 → 3600 (при честном окне упал бы до
3569), значение накопило 13 280 855.

ПОЧЕМУ ЭТО ХУЖЕ, ЧЕМ ПРОСТО «НЕТОЧНО». Сторож сравнивает отказы за час с
попытками за тот же час. Когда ни один счётчик не истекает, «за час» означает
«за всё время работы», и настоящая часовая авария растворяется в месячной
статистике: сто отказов на тринадцать миллионов обращений — доля 0,0008 при
пороге, который ждёт заметной величины. Разбор 28.08 добавил знаменатель, чтобы
рябь не поднимала ложную тревогу, — и вместо ложных тревог получил их полное
отсутствие.

⚠ ПОЧЕМУ ПРЕЖНИЙ СТОРОЖ ЭТО ПРОПУСКАЛ. В `test_avito_tries_counter_2808.py`
есть проверка срока: `assert 0 < ttl <= 3600`. Она зелёная и при скользящем
сроке, и при фиксированном — то есть проверяет форму («срок вообще стоит»), а
не поведение («срок не отодвигается»). Тесты ниже отличают одно от другого:
сдвигают срок и смотрят, вернёт ли его следующее обращение обратно.
"""

import httpx
import pytest
import respx

from app.core import redis as redis_mod
from app.core.config import settings
from app.integrations.avito.client import AVITO_DOWN_KEY, AVITO_TRIES_KEY, AvitoClient
from app.integrations.avito.ratelimit import WINDOW_TTL, AvitoRateLimiter

pytestmark = pytest.mark.anyio

CHATS_URL = f"{settings.avito_api_base}/messenger/v2/accounts/770777/chats"


@pytest.fixture
def подменённый_redis(monkeypatch, redis):  # noqa: ANN001, ANN201 — фикстуры типизированы у себя
    monkeypatch.setattr(redis_mod, "get_client", lambda: redis)
    return redis


@respx.mock
async def test_час_не_отодвигается_следующим_обращением(подменённый_redis) -> None:  # noqa: ANN001
    """⚠ ДИВЕРСИЯ: вернуть `pipe.expire(ключ, AVITO_DOWN_TTL)` вместо
    `set(..., nx=True)` — тест краснеет.

    Это и есть боевая поломка. Проверка устроена так: ключ уже прожил
    большую часть часа (осталось 100 секунд), приходит новое обращение — и
    оставшийся срок обязан ПРОДОЛЖАТЬ убывать, а не откатиться к часу.
    Иначе окно не закроется, пока люди работают, — то есть никогда.
    """
    respx.get(CHATS_URL).mock(return_value=httpx.Response(200, json={"chats": []}))
    await AvitoClient().get_chats("tok", 770777)

    # Ключ прожил почти весь свой час: до конца окна осталось сто секунд.
    await подменённый_redis.expire(AVITO_TRIES_KEY, 100)
    await AvitoClient().get_chats("tok", 770777)

    ttl = await подменённый_redis.ttl(AVITO_TRIES_KEY)
    assert 0 < ttl <= 100, (
        f"срок откатился к {ttl}: окно скользит вместе с трафиком, и час, за "
        "который сторож считает долю отказов, не кончится никогда"
    )
    assert int(await подменённый_redis.get(AVITO_TRIES_KEY)) == 2, "счёт при этом обязан идти"


@respx.mock
async def test_окно_отказов_тоже_фиксировано(подменённый_redis) -> None:  # noqa: ANN001
    """Оба счётчика обязаны жить по одним часам.

    Разъедься окна — и доля считалась бы от разных отрезков времени: свежие
    отказы против накопленных за неделю попыток дают долю, стремящуюся к нулю
    ровно тогда, когда авария настоящая.
    """
    respx.get(CHATS_URL).mock(side_effect=httpx.ConnectError("сеть"))
    with pytest.raises(Exception):  # noqa: B017, PT011 — важен побочный эффект, не тип
        await AvitoClient().get_chats("tok", 770777)

    await подменённый_redis.expire(AVITO_DOWN_KEY, 100)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await AvitoClient().get_chats("tok", 770777)

    assert 0 < await подменённый_redis.ttl(AVITO_DOWN_KEY) <= 100
    assert int(await подменённый_redis.get(AVITO_DOWN_KEY)) == 2


@respx.mock
async def test_причина_остаётся_последней(подменённый_redis) -> None:  # noqa: ANN001
    """А вот причина свой срок обновляет, и это не оплошность.

    Счётчику нужно фиксированное окно, иначе «за час» ничего не значит. Причина
    же всегда одна — последняя: её называют человеку в тревоге, и вчерашняя
    причина при сегодняшних отказах была бы прямым враньём.
    """
    respx.get(CHATS_URL).mock(side_effect=httpx.ConnectError("сеть недоступна"))
    with pytest.raises(Exception):  # noqa: B017, PT011
        await AvitoClient().get_chats("tok", 770777)

    await подменённый_redis.expire(f"{AVITO_DOWN_KEY}:why", 10)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await AvitoClient().get_chats("tok", 770777)

    assert await подменённый_redis.ttl(f"{AVITO_DOWN_KEY}:why") > 10, (
        "причина обязана обновляться: тревога называет её человеку"
    )


async def test_ограничитель_не_оставляет_ключей_без_срока(redis) -> None:  # noqa: ANN001
    """⚠ ДИВЕРСИЯ: вернуть `incr` + `if n == 1: expire` — тест НЕ покраснеет,
    и это сказано здесь честно.

    Щель между двумя командами открывается только при смерти процесса ровно
    между ними, а такое в тесте не воспроизвести без подмены самого Redis.
    Проверяемое здесь — что срок стоит с первой же секунды жизни ключа, то есть
    что щели больше нет по построению, а не что она закрыта удачей.

    Улика, ради которой это переписано, боевая: `ratelimit:avito:85ea8246-…`
    с TTL = -1, значением 87 и номером окна на 8,8 суток старше текущего.
    """
    await AvitoRateLimiter(redis).acquire("acc-1")
    ключи = list(await redis.keys("ratelimit:avito:acc-1:*"))
    assert len(ключи) == 1
    ttl = await redis.ttl(ключи[0])
    assert 0 < ttl <= WINDOW_TTL, f"ключ ограничителя без срока (ttl={ttl}) остаётся навсегда"


async def test_ограничитель_считает_как_прежде(redis) -> None:  # noqa: ANN001
    """Смена способа поставить срок не имеет права изменить сам счёт."""
    ограничитель = AvitoRateLimiter(redis)
    for _ in range(3):
        await ограничитель.acquire("acc-2")
    ключ = (await redis.keys("ratelimit:avito:acc-2:*"))[0]
    assert int(await redis.get(ключ)) == 3
