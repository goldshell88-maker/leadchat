"""DaData «Подсказки по адресам» — справочник ФИАС/ГАР с координатами.

ЗАЧЕМ (владелец 12.09: «нашёл dadata.ru, 10 000 бесплатных запросов в день,
подвяжи и сравни»). Проба на восьми боевых случаях владельца: DaData находит
всё, на чём спотыкались OSM и Яндекс, — «ДОС 41» (квартал), «Ясная 23/4»
в Комсомольске по области, «Дзержинского 11» как «ул Феликса Дзержинского»,
опечатку «Звенигародская», микрорайон «мкр 9 д 14» — и отдаёт дом с
точными координатами (`qc_geo=0`) и уровнем ФИАС (`fias_level=8`).

⚠ ЭТО ПОДСКАЗКИ, А НЕ СТАНДАРТИЗАЦИЯ — DaData пишет прямо: «подсказки не
подходят для автоматической обработки адресов, окончательное решение — за
человеком». Поэтому DaData здесь на тех же правах, что OSM и Яндекс: она
ПРЕДЛАГАЕТ дома, а подтверждает наш вердикт (`geocode.verdict`) по дому,
улице, городу и области, и в карточку сам ложится только `exact`. Массовой
чистки базы через этот метод не делаем.

С 16.09 (docs/46) это ОБЁРТКА: ключ, тело запроса, каскад статусов и разбор
ответа живут в шлюзе на Амстердаме (`gateway/leadchat_gateway/providers/
dadata.py`), а здесь — ЧТО спрашивать: цикл текстов (пункт + улица, потом
улица, варианты «/»), `seen`, кэш ответов и счётчик тарифа. Один поход в шлюз
= один запрос к DaData, поэтому `on_request` и кэш считают как раньше.

Тариф: бесплатно до 10 000 запросов в сутки, не чаще 30 в секунду;
считаем ключом `geo:dadata:calls:<день>` (потолок в настройках).
Запрос — ТОЛЬКО разобранные части (улица, дом, пункт) и город/область
объявления; текст сообщения клиента наружу не уходит.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError, GeoHit, Place, PlaceHit, Query

PROVIDER = "dadata"
#: Таймаут похода в шлюз: у самой DaData 5 с (connect 3) плюс дорога по мосту.
TIMEOUT_SEC = 8.0


def enabled() -> bool:
    """Шлюз настроен и по его снимку `/status` у DaData есть ключ."""
    return gateway.enabled() and gateway.key_present(PROVIDER)


_КОРПУС = re.compile(r"[\s.]*\b(?:корпус|корп|кор|к)\.?[\s.]*(?=\d)", re.IGNORECASE)


def house_for_query(house: str) -> str:
    """«3.кор5» → «3 к 5»: DaData понимает корпус словом «к» через пробел."""
    return _КОРПУС.sub(" к ", house).strip()


async def city_point(
    city: str,
    region: str | None,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> tuple[float, float] | None:
    """Координаты города объявления — чтобы искать дом сначала в его округе.

    Бой 12.09: «Рябиновая 8» по Вологодской области DaData отдаёт Вологду,
    Бабаево, Ермаково — а клиент в деревне под Череповцом, и её среди десяти
    первых нет. Круг в 60 км вокруг города объявления (`locations_geo`)
    ставит деревни округа впереди областного центра.
    """
    _нужен_ключ()
    аргументы = {"city": city, "region": region}
    payload = {"v": 3, "op": "city", **аргументы}
    из_кэша = await geo_cache.get(PROVIDER, payload)
    # «Города нет» — тоже ответ: в кэше он лежит объектом, а не `null`, иначе
    # промах и находку было бы не отличить.
    if isinstance(из_кэша, dict):
        return _точка(из_кэша.get("point"))
    if on_request is not None:
        await on_request()
    данные = await _вызвать("/geo/dadata/city", аргументы, client)
    точка = _точка(данные.get("point"))
    await geo_cache.put(PROVIDER, payload, {"point": точка}, empty=точка is None)
    return точка


async def search(
    query: Query,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
    without_settlement_too: bool = True,
    near: tuple[float, float] | None = None,
    seen: list[GeoHit] | None = None,
) -> list[GeoHit]:
    """Дома по запросу. Сначала с пунктом клиента, без него — если пусто:
    «заречный улица звенигародская 1» подсказка не разбирает, «улица
    звенигародская 1» в Орске — находит с исправленной опечаткой.
    `without_settlement_too=False` — только с пунктом: так ищут дом по
    подсказке из соседней реплики, когда без пункта уже искали. `near` —
    круг вокруг точки вместо города/области (радиус задаёт шлюз).

    `on_request` зовётся ПЕРЕД каждым походом в шлюз (= запросом к DaData):
    тариф DaData считает запросы, а не вызовы `search` (второй поход без
    пункта — тоже запрос), и отказ 403/429 — тоже потраченный запрос. `seen`
    — куда сложить ответы ВСЕХ текстов, не только последнего: по ним воркер
    узнаёт, знает ли карта пункт клиента хотя бы улицей (ревью 15.09).
    """
    _нужен_ключ()
    основа = f"{query.street_for_map} {house_for_query(query.house)}".strip()
    if query.settlement:
        с_пунктом = f"{query.settlement} {основа}"
        # Без улицы («посёлок Каменка 9») искать голый номер дома незачем.
        тексты = [с_пунктом] + (
            [основа] if without_settlement_too and query.street_for_map.strip() else []
        )
    else:
        тексты = [основа]
    if "/" in query.house:
        # «14/2»: у DaData это либо дробь «14/2», либо корпус «14 к 2» —
        # спрашиваем оба написания (стенд 13.09: Литейная 14/2, Казачья
        # 30/95 уходили в «дом не найден»).
        корпусом = re.sub(r"\s*/\s*", " к ", query.house)
        тексты = тексты + [t.replace(house_for_query(query.house), корпусом) for t in тексты]
    hits: list[GeoHit] = []
    for текст in тексты:
        hits = await _ask(query, текст, client, near=near, on_request=on_request)
        if seen is not None:
            seen.extend(hits)
        if any(h.house_level for h in hits):
            break
    return hits


async def _ask(
    query: Query,
    текст: str,
    client: httpx.AsyncClient | None,
    *,
    near: tuple[float, float] | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> list[GeoHit]:
    """Один текст — один поход в шлюз через кэш: попадание — без похода и без
    счётчика. Точка подмены для тестов цикла текстов (`seen`)."""
    аргументы: dict[str, Any] = {
        "text": текст,
        "region": query.region,
        "city": query.city,
        "near": list(near) if near is not None else None,
    }
    payload = {"v": 3, "op": "ask", **аргументы}
    из_кэша = await geo_cache.get(PROVIDER, payload)
    if isinstance(из_кэша, list):
        return _хиты(из_кэша, GeoHit)
    if on_request is not None:
        await on_request()
    данные = await _вызвать("/geo/dadata", аргументы, client)
    сырые = данные.get("hits")
    hits = _хиты(сырые, GeoHit)
    await geo_cache.put(PROVIDER, payload, сырые, empty=not hits)
    return hits


async def search_place(
    place: Place,
    *,
    region: str | None,
    city: str | None = None,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> list[PlaceHit]:
    """Места по описанию без улицы: пункт, массив/СНТ, город — с точкой.

    Ограничение — область объявления (город здесь мешал бы: деревня под
    Гатчиной в границах Череповца не лежит); `city` — когда место без пункта,
    то есть микрорайон/квартал самого города («10 мкр»).
    """
    _нужен_ключ()
    аргументы = {"query_text": place.query_text, "region": region, "city": city}
    payload = {"v": 3, "op": "place", **аргументы}
    из_кэша = await geo_cache.get(PROVIDER, payload)
    if isinstance(из_кэша, list):
        return _хиты(из_кэша, PlaceHit)
    if on_request is not None:
        await on_request()
    данные = await _вызвать("/geo/dadata/place", аргументы, client)
    сырые = данные.get("hits")
    hits = _хиты(сырые, PlaceHit)
    await geo_cache.put(PROVIDER, payload, сырые, empty=not hits)
    return hits


def _нужен_ключ() -> None:
    """Снимок `/status` уже сказал, что ключа нет, — не ходим: как раньше при
    пустом ключе в окружении."""
    if not gateway.key_present(PROVIDER):
        raise GeocodeError(PROVIDER, "blocked")


async def _вызвать(
    path: str, аргументы: dict[str, Any], client: httpx.AsyncClient | None
) -> dict[str, Any]:
    try:
        return await gateway.call(
            path, аргументы, timeout=TIMEOUT_SEC, client=client, provider=PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc


def _хиты[H: (GeoHit, PlaceHit)](сырые: Any, тип: type[H]) -> list[H]:
    """`hits` шлюза → dataclass-ы LeadChat. Поля — те же имена (docs/46 §2.2);
    лишние поля, если шлюз когда-нибудь добавит, не мешают."""
    if not isinstance(сырые, list):
        raise GeocodeError(PROVIDER, "bad_response")
    имена = {f.name for f in dataclasses.fields(тип)}
    try:
        return [тип(**{k: v for k, v in h.items() if k in имена}) for h in сырые]
    except (AttributeError, TypeError) as exc:
        raise GeocodeError(PROVIDER, "bad_response") from exc


def _точка(значение: Any) -> tuple[float, float] | None:
    if значение is None:
        return None
    try:
        lat, lon = значение
        return float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise GeocodeError(PROVIDER, "bad_response") from exc
