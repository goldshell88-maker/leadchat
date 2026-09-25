"""Process-wide Redis singleton (08 §1.1: one client per process).

Created lazily / in the app lifespan; tests override the ``get_redis``
dependency with fakeredis instead of touching this module.
"""

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis

from app.core.config import settings

client: Redis | None = None


def aw[T](result: Awaitable[T] | T) -> Awaitable[T]:
    """Сужение типа команд redis-py: ``await aw(redis.sadd(...))``.

    ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. Один и тот же класс redis-py обслуживает и
    синхронного, и асинхронного клиента, поэтому возврат команд объявлен как
    ``Awaitable[T] | T``. Клиент в приложении всегда асинхронный, но проверка
    типов об этом не знает и краснеет на каждом ``await``.

    ЖИВЁТ ЗДЕСЬ, А НЕ У КАЖДОГО ЗВАВШЕГО. Приём завели дважды — в
    ``services/read_markers`` и в ``services/avito_accounts``, — и к третьему
    месту стало ясно, чем это кончится: четыре копии одной строки, каждая
    приватная, и импорт чужого ``_aw`` через подчёркивание. Приём про КЛИЕНТА,
    значит и место ему рядом с клиентом.
    """
    return cast(Awaitable[T], result)


def socket_timeout_for(component: str) -> float:
    """Потолок ожидания ответа Redis — РАЗНЫЙ у веб-процесса и у воркера.

    ⚠ ЭТО РАЗЛИЧИЕ ОПЛАЧЕНО БОЕВОЙ АВАРИЕЙ 03.09, И ВОТ ЧЕМ ОНО ВЫЗВАНО.
    Утром того дня таймауты задали общими, тремя секундами, с доводом «наши
    команды — это XADD, INCR и HGETALL, миллисекунды по существу». Довод был
    неверен: воркер читает поток КОМАНДОЙ, КОТОРАЯ ЖДЁТ НАРОЧНО —
    ``XREADGROUP ... BLOCK 5000``. Пять секунд ожидания против трёх секунд
    сокета: сокет сдаётся раньше, чем Redis отвечает.

    В бою это выглядело так: каждый холостой заход цикла приёма кончался
    ``TimeoutError: Timeout reading from redis:6379``, переподключением и
    секундной паузой — 163 трассировки за двадцать минут. Сообщения клиентов
    доходили (пришедшее в окно чтение возвращает сразу), но пришедшее сразу
    ПОСЛЕ разрыва ждало следующего захода, а журнал заливало так, что
    настоящую ошибку в нём было бы не найти.

    Правило, которое теперь держит :func:`tests.unit.test_redis_timeouts`:
    потолок сокета обязан быть БОЛЬШЕ самой長ой блокирующей команды процесса.
    У веб-процесса блокирующих команд нет вовсе — там остаются три секунды, и
    это важно: за веб-запросом сидит человек. У воркера потолок поднят выше
    ``inbound.READ_BLOCK_MS``; замолчавший Redis он заметит на несколько секунд
    позже, и для фоновой работы это правильный размен.
    """
    return (
        settings.redis_blocking_socket_timeout_seconds
        if component == "worker"
        else settings.redis_socket_timeout_seconds
    )


def init_client(url: str | None = None, *, component: str = "api") -> Redis:
    """Клиент Redis процесса.

    ⚠ ТАЙМАУТЫ ЗАДАНЫ ЯВНО, И ЭТО НЕ УКРАШЕНИЕ. Без них `await` к Redis не
    возвращается НИКОГДА, если Redis не оборвал соединение, а замолчал (своп,
    длинная команда, сетевой чёрный дыр). А вместе с этим `await` навсегда
    остаётся занятым и соединение к БД: обработчик запроса держит оба.

    Замер боя 02.09 показал, чем это кончается на соседнем ресурсе: медленные
    выборки выбрали пул к БД (166 ошибок «QueuePool limit reached»), и приём
    сообщений от Авито потерял 105 вебхуков из 566. С Redis такого ещё не
    случалось — но у него не было и предохранителя.

    Две секунды на соединение, проверка живости раз в полминуты.
    `retry_on_timeout` — потому что разовый таймаут чаще означает заминку, а не
    смерть, и повтор дешевле отказа. Потолок ответа зависит от процесса —
    разбор в :func:`socket_timeout_for`.
    """
    global client
    if client is None:
        client = Redis.from_url(
            url or settings.redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_for(component),
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            health_check_interval=30,
            retry_on_timeout=True,
        )
    return client


def get_client(component: str = "api") -> Redis:
    if client is None:
        return init_client(component=component)
    return client


async def close_client() -> None:
    global client
    if client is not None:
        await client.aclose()
        client = None
