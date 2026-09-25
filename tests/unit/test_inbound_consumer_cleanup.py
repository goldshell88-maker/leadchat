"""Потребитель потока убирает своё имя из группы при остановке (03.09).

⚠ ЧИСЛА, ИЗ-ЗА КОТОРЫХ ЭТО НАЙДЕНО. Замер боя 03.09: в группе `workers`
потока `webhooks:avito` — 162 потребителя при одном живом, простой самого
старого 7,6 суток. Имя потребителя это `<hostname>:<pid>`, а контейнер
пересоздаётся на каждой выкатке, то есть 10-20 раз в сутки: за год таких
записей набралось бы несколько тысяч. Каждую из них просматривает
`XAUTOCLAIM` на каждом заходе цикла.

⚠ ПОЧЕМУ НЕЛЬЗЯ ПРОСТО УДАЛЯТЬ. `XGROUP DELCONSUMER` уносит вместе с именем
его PEL — обращения, взятые в работу и не подтверждённые. Их после этого не
перехватит никто: `XAUTOCLAIM` ищет ИМЕННО в чужих PEL. Потерянное обращение
Авито — это потерянный клиент, и цена ошибки здесь несимметрична: лишнее имя
в группе стоит микросекунды, потерянное обращение — деньги.
"""

import asyncio
import contextlib

import fakeredis
import pytest

from app.workers import inbound

pytestmark = pytest.mark.anyio


async def _остановить_как_в_бою(
    redis: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Остановка воркера ровно так, как её делает ARQ.

    ⚠ ЭТА ФУНКЦИЯ — ГЛАВНОЕ В ФАЙЛЕ, И ПЕРВАЯ ЕЁ РЕДАКЦИЯ БЫЛА НЕВЕРНОЙ.
    Она просто ставила флаг и звала цикл — цикл выходил сам, уборка в его
    конце срабатывала, тест зеленел. А в бою `shutdown` ставит флаг И ТУТ ЖЕ
    делает `cancel()`, когда цикл висит на чтении из потока: отмена прилетает
    внутрь ожидания и выходит наружу, и код после `while` не исполняется
    НИКОГДА. Правка была выкачена и не работала — увидел по счётчику на бою:
    потребителей стало 163 вместо 162.

    Поэтому здесь воспроизводится настоящая последовательность: задача
    создаётся, отменяется, и только потом идёт уборка — там, где она теперь и
    живёт, в `shutdown`.
    """
    провалы: list[str] = []
    настоящий = inbound.log.warning

    def ловим(event: str, **kw: object) -> None:
        if event == "inbound.consumer_cleanup_failed":
            провалы.append(event)
        настоящий(event, **kw)

    monkeypatch.setattr(inbound.log, "warning", ловим)

    стоп = asyncio.Event()
    ctx = {"redis": redis, "shutdown": стоп}
    задача = asyncio.create_task(inbound.inbound_consumer_loop(ctx))
    await asyncio.sleep(0)  # дать циклу дойти до ожидания в потоке
    стоп.set()
    задача.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await задача
    await inbound.remove_consumer(redis)

    assert not провалы, "уборка потребителя упала — ошибку проглотил except"


async def _имена(redis: fakeredis.aioredis.FakeRedis) -> set[str]:
    строки = await redis.xinfo_consumers(inbound.STREAM, inbound.GROUP)
    return {c["name"] for c in строки}


async def test_пустой_потребитель_уходит_из_группы(redis, monkeypatch):
    я = inbound._consumer_name()
    await inbound._ensure_group(redis)
    await redis.xadd(inbound.STREAM, {"body": "{}"})
    # Читаем и подтверждаем: имя в группе появилось, необработанного не осталось.
    прочитано = await redis.xreadgroup(inbound.GROUP, я, {inbound.STREAM: ">"}, count=10)
    for _, записи in прочитано or []:
        for eid, _ in записи:
            await redis.xack(inbound.STREAM, inbound.GROUP, eid)
    assert я in await _имена(redis), "имя не завелось — тест ниже ничего не проверит"

    await _остановить_как_в_бою(redis, monkeypatch)

    assert я not in await _имена(redis), (
        "потребитель не убрал себя: имена копятся с каждой выкаткой"
    )


async def test_потребитель_с_незакрытыми_обращениями_остаётся(redis, monkeypatch):
    """⚠ ГЛАВНАЯ ПРОВЕРКА ФАЙЛА: уборка не имеет права уносить работу.

    Здесь запись ПРОЧИТАНА, но НЕ подтверждена — то есть лежит в PEL этого
    имени. Удали его — и обращение исчезнет молча, мимо `XAUTOCLAIM`, мимо
    отстойника, мимо всякого следа.
    """
    я = inbound._consumer_name()
    await inbound._ensure_group(redis)
    await redis.xadd(inbound.STREAM, {"body": "{}"})
    await redis.xreadgroup(inbound.GROUP, я, {inbound.STREAM: ">"}, count=10)

    висит = await redis.xpending(inbound.STREAM, inbound.GROUP)
    assert висит["pending"] == 1, "запись не повисла — случай не смоделирован"

    await _остановить_как_в_бою(redis, monkeypatch)

    assert я in await _имена(redis), (
        "имя удалено вместе с неподтверждённым обращением — оно потеряно навсегда"
    )
    после = await redis.xpending(inbound.STREAM, inbound.GROUP)
    assert после["pending"] == 1, "необработанное обращение исчезло из группы"


async def test_остановка_воркера_действительно_зовёт_уборку(redis, monkeypatch):
    """⚠ ВТОРАЯ ПОЛОВИНА, И СЛОМАНА БЫЛА ИМЕННО ОНА.

    Проверки выше говорят, что уборка РАБОТАЕТ. Они ничего не говорят о том,
    что её ЗОВУТ. В первой редакции она стояла в конце цикла, куда исполнение
    при отмене не доходит, — работающий код, который никто не вызывает. Это
    самый частый дефект в этом проекте, и он же случился здесь.

    Тест ведёт настоящий `shutdown` воркера и требует, чтобы вызов состоялся.
    """
    from app.workers import main as worker_main

    звали: list[object] = []

    async def подмена(r: object) -> None:
        звали.append(r)

    monkeypatch.setattr(worker_main.inbound_module, "remove_consumer", подмена)

    class ЗаглушкаARQ:
        async def aclose(self) -> None: ...

    async def ничего(*_a: object, **_k: object) -> None: ...

    monkeypatch.setattr(worker_main.redis_mod, "close_client", ничего)
    monkeypatch.setattr(worker_main.db_mod, "dispose_engine", ничего)

    задача = asyncio.create_task(asyncio.sleep(3600))
    ctx = {
        "shutdown": asyncio.Event(),
        "inbound_task": задача,
        "arq": ЗаглушкаARQ(),
        "redis": redis,
    }
    await worker_main.shutdown(ctx)

    assert звали == [redis], (
        "остановка воркера не зовёт уборку потребителя — "
        "имена в группе снова начнут копиться с каждой выкаткой"
    )
