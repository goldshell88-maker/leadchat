"""Обёртки Ahunter и Спеллера поверх шлюза (docs/46, 16.09).

Поход к провайдеру и разбор его ответа уехали на шлюз (`gateway/tests/test_gw_text.py`);
здесь — то, что осталось в LeadChat: строка запроса и слова улицы, кэш (один
поход на один вопрос), `on_request` только перед настоящим походом, сборка
`Suggested` из JSON шлюза, подстановка замен, маппинг отказов в `GeocodeError`
без адреса в логах.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import structlog

from app.integrations import ahunter, gateway, speller
from app.services import geo_cache
from app.services import geocode as g

pytestmark = pytest.mark.anyio

GW = "http://gw.test"

#: Настоящие обёртки — до того, как autouse-заглушка conftest
#: (`_без_сети_подсказчиков`) подменит их на «ничего не нашли».
_SUGGEST = ahunter.suggest
_FIX_STREET = speller.fix_street


@pytest.fixture(autouse=True)
def _настоящие_обёртки(monkeypatch, _без_сети_подсказчиков):  # noqa: ANN001
    """Здесь проверяются сами обёртки, а не воркер: заглушку снимаем, сеть —
    respx на адрес шлюза."""
    monkeypatch.setattr(ahunter, "suggest", _SUGGEST)
    monkeypatch.setattr(speller, "fix_street", _FIX_STREET)


ЗАПРОС = g.Query(
    region="Иркутская область", city="Ангарск", settlement=None, street="57 квартал", house="8"
)
ПОДСКАЗКА = {
    "street": "кв-л 57",
    "house": "8",
    "city": "Ангарск",
    "settlement": None,
    "district": "р-н Ангарский",
    "region": "обл Иркутская",
    "formatted": "обл Иркутская, р-н Ангарский, г Ангарск, кв-л 57, дом 8",
}


# --- то, что осталось чистым в LeadChat ------------------------------------------


def test_ahunter_текст_запроса_без_области() -> None:
    assert ahunter.query_text(ЗАПРОС) == "Ангарск 57 квартал 8"
    # Улица — как для карты, с раскрытым типом; региона нет.
    assert (
        ahunter.query_text(g.Query(None, None, "Нахабино", "ул Мира", "3"))
        == "Нахабино улица Мира 3"
    )


def test_speller_слова_и_подстановка() -> None:
    assert speller.words_to_check("ул Ленена") == ["Ленена"]
    assert speller.words_to_check("пр-кт Мира") == []
    # Пять букв — уже спрашиваем («Новая», «Южная»), четыре — нет; дефисные — нет.
    assert speller.words_to_check("ул Новая") == ["Новая"]
    assert speller.words_to_check("ул Ново-Садовая 5") == []
    assert speller.apply("ул Ленена", {"Ленена": "Ленина"}) == "ул Ленина"
    # Заглавная сохраняется; обратная косая в ответе словаря — текст, не группа.
    assert speller.apply("ул Ленена", {"Ленена": "ленина"}) == "ул Ленина"
    assert speller.apply("ул Ленена", {"Ленена": "Ле\\1нина"}) == "ул Ле\\1нина"
    # Часть слова не трогаем.
    assert speller.apply("ул Ленена Ленената", {"Ленена": "Ленина"}) == "ул Ленина Ленената"


# --- Ahunter -----------------------------------------------------------------------


@respx.mock
async def test_ahunter_собирает_подсказку_из_json_шлюза() -> None:
    маршрут = respx.post(f"{GW}/geo/ahunter").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [ПОДСКАЗКА]})
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    подсказки = await ahunter.suggest(ЗАПРОС, on_request=посчитать)
    # Все семь полей — по именам из JSON шлюза.
    assert подсказки == [
        ahunter.Suggested(
            street="кв-л 57",
            house="8",
            city="Ангарск",
            settlement=None,
            district="р-н Ангарский",
            region="обл Иркутская",
            formatted="обл Иркутская, р-н Ангарский, г Ангарск, кв-л 57, дом 8",
        )
    ]
    assert счёт == 1
    запрос = маршрут.calls.last.request
    assert json.loads(запрос.content) == {"text": "Ангарск 57 квартал 8"}
    assert запрос.headers["Authorization"] == "Bearer test-token"
    # Лишнее поле шлюза не мешает, недостающее — форма не та.
    respx.post(f"{GW}/geo/ahunter").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [{**ПОДСКАЗКА, "zip": "1"}]})
    )
    assert (await ahunter.suggest(ЗАПРОС))[0].house == "8"
    respx.post(f"{GW}/geo/ahunter").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [{"street": "x"}]})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert exc.value.kind == "bad_response"


@respx.mock
async def test_ahunter_кэш_один_поход_и_счётчик_только_при_походе(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/geo/ahunter").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [ПОДСКАЗКА]})
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    первый = await ahunter.suggest(ЗАПРОС, on_request=посчитать)
    второй = await ahunter.suggest(ЗАПРОС, on_request=посчитать)
    assert первый == второй and первый[0].street == "кв-л 57"
    assert маршрут.call_count == 1 and счёт == 1
    # Другой дом — другой ключ кэша.
    await ahunter.suggest(g.Query(None, "Ангарск", None, "57 квартал", "15"), on_request=посчитать)
    assert маршрут.call_count == 2 and счёт == 2
    ключи = [k async for k in redis.scan_iter("geo:cache:ahunter:*")]
    assert len(ключи) == 2


@respx.mock
async def test_ahunter_отказы_шлюза_и_провайдера_без_адреса_в_логе() -> None:
    маршрут = respx.post(f"{GW}/geo/ahunter")
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    маршрут.mock(side_effect=httpx.ConnectTimeout("boom"))
    with structlog.testing.capture_logs() as логи:
        with pytest.raises(g.GeocodeError) as exc:
            await ahunter.suggest(ЗАПРОС, on_request=посчитать)
    assert (exc.value.provider, exc.value.kind, exc.value.status) == ("ahunter", "network", None)
    assert счёт == 1  # счётчик — за попытку, как и раньше
    текст_логов = json.dumps(логи, ensure_ascii=False)
    assert "Ангарск" not in текст_логов and "квартал" not in текст_логов
    assert "Ангарск" not in str(exc.value)
    # Отказ провайдера за шлюзом — kind как есть, со статусом провайдера.
    маршрут.mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "blocked", "status": 403})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert (exc.value.kind, exc.value.status) == ("blocked", 403)
    маршрут.mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "network", "status": 503})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert (exc.value.kind, exc.value.status) == ("network", 503)
    # Шлюз не принял токен — для воркера это «сеть», не бан провайдера.
    маршрут.mock(return_value=httpx.Response(401, json={"detail": "x"}))
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert exc.value.kind == "network"
    # Ответ шлюза не той формы.
    маршрут.mock(return_value=httpx.Response(200, json={"ok": True}))
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert exc.value.kind == "bad_response"


async def test_ahunter_клиент_из_теста_идёт_в_шлюз() -> None:
    def обработчик(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{GW}/geo/ahunter"
        return httpx.Response(200, json={"ok": True, "hits": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        assert await ahunter.suggest(ЗАПРОС, client=client) == []


# --- Спеллер -----------------------------------------------------------------------


@respx.mock
async def test_speller_замены_подставляются_с_заглавной() -> None:
    маршрут = respx.post(f"{GW}/spell").mock(
        return_value=httpx.Response(200, json={"ok": True, "fixes": {"Ленена": "ленина"}})
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    assert await speller.fix_street("ул Ленена 5", on_request=посчитать) == "ул Ленина 5"
    assert счёт == 1
    assert json.loads(маршрут.calls.last.request.content) == {
        "text": "Ленена",
        "words": ["Ленена"],
    }
    # Несколько слов — одной строкой через пробел, без типов и коротких.
    await speller.fix_street("пр-кт Звенигародская Ленена", on_request=посчитать)
    assert json.loads(маршрут.calls.last.request.content) == {
        "text": "Звенигародская Ленена",
        "words": ["Звенигародская", "Ленена"],
    }


@respx.mock
async def test_speller_пусто_это_none_а_нечего_спрашивать_без_похода() -> None:
    маршрут = respx.post(f"{GW}/spell").mock(
        return_value=httpx.Response(200, json={"ok": True, "fixes": {}})
    )
    assert await speller.fix_street("ул Ленена") is None
    assert маршрут.call_count == 1
    # Только тип улицы и число — спрашивать нечего, в шлюз не идём.
    assert await speller.fix_street("ул 5") is None
    assert await speller.fix_street("") is None
    assert маршрут.call_count == 1
    # `fixes` не той формы — сбой, не молчание.
    маршрут.mock(return_value=httpx.Response(200, json={"ok": True, "fixes": ["Ленина"]}))
    with pytest.raises(g.GeocodeError) as exc:
        await speller.fix_street("ул Ленена")
    assert exc.value.kind == "bad_response"


@respx.mock
async def test_speller_кэш_один_поход(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/spell").mock(
        return_value=httpx.Response(200, json={"ok": True, "fixes": {"Ленена": "Ленина"}})
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    assert await speller.fix_street("ул Ленена", on_request=посчитать) == "ул Ленина"
    assert await speller.fix_street("ул Ленена 7", on_request=посчитать) == "ул Ленина 7"
    assert маршрут.call_count == 1 and счёт == 1
    # Пустой ответ тоже запоминается (на сутки) — второй раз не спрашиваем.
    маршрут.mock(return_value=httpx.Response(200, json={"ok": True, "fixes": {}}))
    assert await speller.fix_street("ул Новая", on_request=посчитать) is None
    assert await speller.fix_street("ул Новая", on_request=посчитать) is None
    assert маршрут.call_count == 2 and счёт == 2


@respx.mock
async def test_speller_отказ_шлюза_это_network() -> None:
    маршрут = respx.post(f"{GW}/spell")
    маршрут.mock(side_effect=httpx.ConnectError("x"))
    with structlog.testing.capture_logs() as логи:
        with pytest.raises(g.GeocodeError) as exc:
            await speller.fix_street("ул Ленена")
    assert (exc.value.provider, exc.value.kind) == ("speller", "network")
    assert "Ленена" not in json.dumps(логи, ensure_ascii=False)
    маршрут.mock(return_value=httpx.Response(502))
    with pytest.raises(g.GeocodeError) as exc:
        await speller.fix_street("ул Ленена")
    assert exc.value.kind == "network"
    маршрут.mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "blocked", "status": 403})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await speller.fix_street("ул Ленена")
    assert (exc.value.kind, exc.value.status) == ("blocked", 403)


@respx.mock
async def test_без_ключа_на_шлюзе_это_blocked_и_снимок_помечен() -> None:
    """Ahunter ключа не требует, но договор общий: `no_key` → blocked, как
    пустой ключ раньше, и снимок ключей помечает провайдера."""
    respx.post(f"{GW}/geo/ahunter").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    gateway.known_keys = {"ahunter": None}  # ключ не нужен — в снимке None
    assert gateway.key_present("ahunter")
    with pytest.raises(g.GeocodeError) as exc:
        await ahunter.suggest(ЗАПРОС)
    assert exc.value.kind == "blocked"
    assert not gateway.key_present("ahunter")
