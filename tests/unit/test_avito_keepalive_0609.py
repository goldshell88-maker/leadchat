"""Один httpx-клиент на процесс: поход в Авито не платит за рукопожатие.

ЗАМЕР 06.09. `_request` открывал `httpx.AsyncClient` на каждый запрос и тут
же закрывал: новое TCP-соединение и TLS-рукопожатие каждый раз. С боевого
хоста первый запрос к api.avito.ru шёл 0,168–0,180 с, по уже открытому
соединению 0,041–0,045 с — около 0,13 с на рукопожатие при доставке с p50
0,33 с; сверка всех каналов делала ~276 рукопожатий за порыв.

ЧТО СТЕРЕЖЁМ.
(1) Два запроса подряд идут через ОДИН и тот же клиент, создан он один раз, а
    таймаут у него прежний — 15 с (fake-avito «slow» обязан падать по нему).
(2) При смене цикла событий клиент пересоздаётся: соединения пула — сокеты
    цикла-владельца, и из другого цикла они дают «attached to a different
    loop». В тестах цикл новый на каждый тест, и без этого падали бы соседи.
(3) `close_http_client` закрывает клиент и отпускает его; повтор безвреден;
    следующий поход берёт новый.
(4) Остановка воркера действительно ЗОВЁТ закрытие — работающий код, который
    никто не вызывает, здесь самый частый дефект (см. уборку потребителя в
    `test_inbound_consumer_cleanup.py`).

⚠ ДИВЕРСИИ (все прогнаны, все дали красный):
- вернуть в `_request` `async with httpx.AsyncClient(timeout=...) as http:` —
  краснеет (1): клиентов два, общий пуст;
- убрать `timeout=TIMEOUT_SECONDS` из `http_client` — краснеет сторож таймаута;
- убрать `limits=HTTP_LIMITS` из `http_client` (клиент на умолчаниях httpx:
  100 соединений вместо 50) — краснеет сторож пределов. ⚠ Первая редакция
  сторожа сверяла только константу `HTTP_LIMITS` и на этой диверсии оставалась
  ЗЕЛЁНОЙ: константа на месте, а клиент её не читает. Теперь сверяется пул
  самого клиента;
- убрать `or _http_loop is not loop` из условия в `http_client` — краснеет (2);
- не обнулять `_http` в `close_http_client` — краснеет (3);
- убрать `await avito_client.close_http_client()` из `shutdown` воркера —
  краснеет (4).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from app.core import redis as redis_mod
from app.core.config import settings
from app.integrations.avito import client as client_mod
from app.integrations.avito.client import AvitoClient

CHATS_URL = f"{settings.avito_api_base}/messenger/v2/accounts/770777/chats"


@pytest.fixture(autouse=True)
async def чистый_клиент() -> Any:
    """Каждый тест начинает и кончает без общего клиента — соседи по файлу и по
    прогону не должны видеть чужой."""
    await client_mod.close_http_client()
    yield
    await client_mod.close_http_client()


@pytest.fixture
def подменённый_redis(monkeypatch: pytest.MonkeyPatch, redis: Any) -> Any:
    monkeypatch.setattr(redis_mod, "get_client", lambda: redis)
    return redis


@pytest.fixture
def созданные_клиенты(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Считает, сколько раз модуль построил `httpx.AsyncClient`."""
    созданные: list[httpx.AsyncClient] = []

    class Считающий(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            созданные.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Считающий)
    return созданные


@respx.mock
async def test_два_запроса_подряд_идут_через_один_клиент(
    подменённый_redis: Any, созданные_клиенты: list[httpx.AsyncClient]
) -> None:
    """Один клиент на два похода — и это тот, что лежит в модуле."""
    маршрут = respx.get(CHATS_URL).mock(return_value=httpx.Response(200, json={"chats": []}))

    await AvitoClient().get_chats("tok", 770777)
    первый = client_mod._http
    await AvitoClient().get_chats("tok", 770777)

    assert маршрут.call_count == 2
    assert первый is not None and client_mod._http is первый, (
        "второй поход в Авито взял другой клиент — keep-alive не работает"
    )
    assert созданные_клиенты == [первый], (
        f"клиентов построено {len(созданные_клиенты)}, а обязан быть один на процесс"
    )
    assert not первый.is_closed, (
        "общий клиент закрыт после запроса — следующий снова платит рукопожатие"
    )


async def test_таймаут_и_пределы_прежние() -> None:
    """Общий клиент не имеет права потерять потолок в 15 с: fake-avito «slow»
    (20 с) обязан ронять запрос по таймауту, как и раньше.

    Пределы сверяются по ПУЛУ КЛИЕНТА, а не по константе: константа может стоять
    в модуле и не попасть в конструктор — тогда клиент живёт на умолчаниях httpx
    (100 соединений), и потолок «по числу задач воркера» существует только в
    комментарии.
    """
    клиент = client_mod.http_client()
    assert клиент.timeout == httpx.Timeout(client_mod.TIMEOUT_SECONDS)
    assert client_mod.HTTP_LIMITS == httpx.Limits(max_keepalive_connections=20, max_connections=50)
    # Пул транспорта — единственное место, где пределы действуют (httpcore).
    пул = клиент._transport._pool  # type: ignore[attr-defined]
    assert (пул._max_keepalive_connections, пул._max_connections) == (20, 50), (
        "клиент построен не по HTTP_LIMITS — пределы стоят в модуле, а не в пуле"
    )


async def test_при_смене_цикла_событий_клиент_пересоздаётся() -> None:
    """Клиент чужого цикла обязан уступить, в обе стороны.

    Второй цикл живёт в другом потоке (`asyncio.run` там — новый цикл): из него
    модуль обязан отдать другой клиент, а по возвращении в свой цикл — снова
    другой, потому что тот, что остался, принадлежит уже мёртвому циклу.
    """
    первый = client_mod.http_client()
    assert client_mod.http_client() is первый, "в своём цикле клиент обязан быть тем же"

    async def взять() -> httpx.AsyncClient:
        return client_mod.http_client()

    второй = await asyncio.to_thread(lambda: asyncio.run(взять()))
    assert второй is not первый, (
        "клиент прежнего цикла отдан в чужой: «attached to a different loop»"
    )

    третий = client_mod.http_client()
    assert третий is not второй, "вернулись в свой цикл, а клиент остался от мёртвого"


async def test_закрытие_отпускает_клиент_и_следующий_поход_берёт_новый() -> None:
    первый = client_mod.http_client()

    await client_mod.close_http_client()

    assert первый.is_closed, "закрытие не закрыло: соединения остаются висеть до смерти процесса"
    assert client_mod._http is None, "закрытый клиент остался общим — следующий поход упадёт на нём"
    await client_mod.close_http_client()  # повтор безвреден
    assert client_mod.http_client() is not первый


async def test_закрытый_снаружи_клиент_пересоздаётся() -> None:
    """Ветка `_http.is_closed` в `http_client`: клиент закрыли мимо
    `close_http_client` (тест-сосед, отладка) — следующий поход обязан получить
    живой, а не `RuntimeError: client has been closed`."""
    первый = client_mod.http_client()
    await первый.aclose()

    второй = client_mod.http_client()
    assert второй is not первый and not второй.is_closed, (
        "закрытый мимо close_http_client клиент остался общим"
    )


async def test_остановка_воркера_зовёт_закрытие(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящий `shutdown` воркера с заглушками всего остального."""
    from app.workers import main as worker_main

    звали: list[str] = []

    async def подмена() -> None:
        звали.append("http")

    async def ничего(*_a: object, **_k: object) -> None: ...

    class ЗаглушкаARQ:
        async def aclose(self) -> None: ...

    monkeypatch.setattr(worker_main.avito_client, "close_http_client", подмена)
    monkeypatch.setattr(worker_main.inbound_module, "remove_consumer", ничего)
    monkeypatch.setattr(worker_main.redis_mod, "close_client", ничего)
    monkeypatch.setattr(worker_main.db_mod, "dispose_engine", ничего)

    ctx = {
        "shutdown": asyncio.Event(),
        "inbound_task": asyncio.create_task(asyncio.sleep(3600)),
        "arq": ЗаглушкаARQ(),
        "redis": redis,
    }
    await worker_main.shutdown(ctx)

    assert звали == ["http"], "остановка воркера не закрывает общий клиент к Авито"
