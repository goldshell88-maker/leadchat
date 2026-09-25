"""DaData «Подсказки по адресам» — справочник ФИАС/ГАР с координатами.

Перенесено из `app/integrations/dadata.py` LeadChat 16.09 (docs/46): здесь
ключ, тело запроса, ограничение поиска (`locations`/`locations_geo`), каскад
статусов и разбор чужого JSON. Что спрашивать (пункт + улица, потом улица,
варианты «/»), кэш и суточный счётчик — остались у обёртки LeadChat: один
вызов ручки = один запрос к DaData.

⚠ ЭТО ПОДСКАЗКИ, А НЕ СТАНДАРТИЗАЦИЯ — DaData пишет прямо: «подсказки не
подходят для автоматической обработки адресов, окончательное решение — за
человеком». Поэтому разбор только ПРЕДЛАГАЕТ дома и места, а вердикт по дому,
улице, городу и области выносит LeadChat.

Тариф: бесплатно до 10 000 запросов в сутки, не чаще 30 в секунду. В запрос
уходят ТОЛЬКО разобранные части адреса и город/область объявления; текст
сообщения клиента сюда не попадает никогда.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from leadchat_gateway import http
from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.types import GeoHit, PlaceHit

PROVIDER = "dadata"
BASE_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
COUNT = 10

#: Радиус «рядом с городом объявления»: деревни округа Череповца — до 60 км.
NEAR_RADIUS_M = 60_000

_ТИП_РЕГИОНА = re.compile(
    r"\b(?:область|обл\.?|край|республика|респ\.?|автономный округ|ао|округ)\b", re.IGNORECASE
)


def _регион_для_подсказок(region: str | None) -> str | None:
    """«Хабаровский край» → «Хабаровский»: DaData ждёт регион без типа."""
    if not region:
        return None
    имя = _ТИП_РЕГИОНА.sub(" ", region)
    имя = " ".join(имя.replace(" и ", " ").split())
    return имя or None


#: Регионы, чей фильтр `locations` нельзя получить вырезанием типа из имени
#: (ревью 12.09): «ХМАО — Югра» у DaData «Ханты-Мансийский Автономный округ -
#: Югра», «Северная Осетия» — «Северная Осетия - Алания», «Карачаево-Черкесия»
#: — «…Черкесская». Город федерального значения и область вокруг у DaData — ДВА
#: региона, у объявления — одно место (`locations` — это ИЛИ, даём оба разом).
#: Значение — готовые объекты фильтра, как их ждёт DaData: код ISO 3166-2
#: (`region_iso_code`) или код КЛАДР региона (`kladr_id`, две цифры — форма из
#: документации DaData: `{"kladr_id": "65"}`).
#: Крым и Севастополь — ТОЛЬКО по КЛАДР 91/92 (стенд 19.09): ISO у DaData там
#: UA-43/UA-40 (справочник регионов HFLabs), кодов RU-CR/RU-SEV в справочнике
#: нет, а фильтр по несуществующему коду отдаёт пустоту молча — все места Крыма
#: по области были `not_found`. Код КЛАДР от политики справочника не зависит.


def _по_iso(код: str) -> dict[str, str]:
    return {"region_iso_code": код}


def _по_кладр(код: str) -> dict[str, str]:
    return {"kladr_id": код}


_РЕГИОН_ФИЛЬТР: dict[str, tuple[dict[str, str], ...]] = {
    "Москва и область": (_по_iso("RU-MOW"), _по_iso("RU-MOS")),
    # Зеленоград (регион «Москва») и «деревня Голубое» под ним — область
    # (стенд 14.09); и наоборот, из подмосковного объявления называют Москву.
    "Москва": (_по_iso("RU-MOW"), _по_iso("RU-MOS")),
    "Московская область": (_по_iso("RU-MOS"), _по_iso("RU-MOW")),
    "Санкт-Петербург": (_по_iso("RU-SPE"), _по_iso("RU-LEN")),
    "Ленинградская область": (_по_iso("RU-LEN"), _по_iso("RU-SPE")),
    "Севастополь": (_по_кладр("92"), _по_кладр("91")),
    "Республика Крым": (_по_кладр("91"), _по_кладр("92")),
    "ХМАО — Югра": (_по_iso("RU-KHM"),),
    "Ямало-Ненецкий АО": (_по_iso("RU-YAN"),),
    "Ненецкий АО": (_по_iso("RU-NEN"),),
    "Еврейская АО": (_по_iso("RU-YEV"),),
    "Республика Саха (Якутия)": (_по_iso("RU-SA"),),
    "Чувашская Республика": (_по_iso("RU-CU"),),
    "Удмуртская Республика": (_по_iso("RU-UD"),),
    "Чеченская Республика": (_по_iso("RU-CE"),),
    "Кабардино-Балкария": (_по_iso("RU-KB"),),
    "Карачаево-Черкесия": (_по_iso("RU-KC"),),
    "Северная Осетия": (_по_iso("RU-SE"),),
}


def locations_for(*, region: str | None, city: str | None) -> list[dict[str, str]]:
    """Ограничение поиска: город объявления, а без города — область.

    Город и область разом не даём: `locations` — это ИЛИ, и область поглотила
    бы город. Поиск по области (дом в другом городе области) — отдельный
    запрос вызывающего с `city=None`.
    """
    if city:
        return [{"city": city}]
    if region in _РЕГИОН_ФИЛЬТР:
        # Копии: тело запроса собирается из них, общий словарь не должен уехать.
        return [dict(ф) for ф in _РЕГИОН_ФИЛЬТР[region]]
    регион = _регион_для_подсказок(region)
    return [{"region": регион}] if регион else []


def _ключ() -> str:
    ключ = (settings.dadata_api_key or "").strip()
    if not ключ:
        raise ProviderError("no_key", None, "ключ DaData на шлюзе не задан")
    return ключ


async def _post(body: dict[str, Any]) -> Any:
    """Один запрос к DaData; статусы — общим каскадом `http.check_status`."""
    headers = {
        "Authorization": f"Token {_ключ()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with http.client(TIMEOUT) as own:
            resp = await own.post(BASE_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    http.check_status(resp)
    return http.parse_json(resp)


async def ask(
    text: str,
    *,
    region: str | None,
    city: str | None,
    near: tuple[float, float] | None = None,
) -> list[GeoHit]:
    """Дома по одной строке запроса. `near` — круг NEAR_RADIUS_M вокруг
    точки вместо города/области (`locations_geo`): область проверит вердикт
    LeadChat (`region_matches`)."""
    body: dict[str, Any] = {"query": text[:300], "count": COUNT}
    if near is not None:
        body["locations_geo"] = [{"lat": near[0], "lon": near[1], "radius_meters": NEAR_RADIUS_M}]
    else:
        места = locations_for(region=region, city=city)
        if места:
            body["locations"] = места
    return parse_response(await _post(body))


async def city_point(city: str, region: str | None) -> tuple[float, float] | None:
    """Координаты города объявления — чтобы искать дом сначала в его округе.

    Бой 12.09: «Рябиновая 8» по Вологодской области DaData отдаёт Вологду,
    Бабаево, Ермаково — а клиент в деревне под Череповцом, и её среди десяти
    первых нет. Круг в 60 км вокруг города объявления (`locations_geo`)
    ставит деревни округа впереди областного центра.
    """
    регион = _регион_для_подсказок(region)
    body: dict[str, Any] = {
        "query": city[:100],
        "count": 3,
        "from_bound": {"value": "city"},
        "to_bound": {"value": "settlement"},
    }
    if регион:
        body["locations"] = [{"region": регион}]
    данные = await _post(body)
    for s in данные.get("suggestions", []) if isinstance(данные, dict) else []:
        d = s.get("data") if isinstance(s, dict) else None
        if not isinstance(d, dict):
            continue
        имя = (d.get("city") or d.get("settlement") or "").strip().lower().replace("ё", "е")
        if имя != city.strip().lower().replace("ё", "е"):
            continue
        try:
            return float(d.get("geo_lat") or ""), float(d.get("geo_lon") or "")
        except (TypeError, ValueError):
            continue
    return None


async def search_place(query_text: str, *, region: str | None, city: str | None) -> list[PlaceHit]:
    """Места по описанию без улицы: пункт, массив/СНТ, город — с точкой.

    Ограничение — область объявления (город здесь мешал бы: деревня под
    Гатчиной в границах Череповца не лежит); `city` — когда место без пункта,
    то есть микрорайон/квартал самого города («10 мкр»). Уровни ФИАС: 4 —
    город, 6 — пункт, 65 — планировочная структура (массив, СНТ, микрорайон).
    """
    body: dict[str, Any] = {"query": query_text[:300], "count": COUNT}
    места = locations_for(region=region, city=city)
    if места:
        body["locations"] = места
    return parse_places(await _post(body))


_В_СКОБКАХ = re.compile(r"\s*\(([^)]*)\)")
#: Типы планировочной структуры в `settlement_with_type` у DaData: то, что
#: стоит внутри пункта или города, а не сам пункт («тер. СНТ Рассвет»,
#: «мкр Центральный», «зона Южный», «кв-л 92/93»).
_ТИП_СТРУКТУРЫ = re.compile(
    r"^(?:мкр|кв-л|квартал|тер|зона|массив|жилрайон|ж/р|гск|снт|днт|днп|тсн|кп|промзона)\.?\s",
    re.IGNORECASE,
)
#: Массив с участками, где дома в ФИАС почти никогда нет: СНТ, ДНТ, ДНП, ТСН,
#: коттеджный посёлок. Номер участка называет клиент, справочник знает только
#: сам массив.
_МАССИВ_С_УЧАСТКАМИ = re.compile(
    r"\b(?:снт|днт|днп|тсн|кп|садоводч\w*|садовое|дачн\w*|коттеджн\w*)\b", re.IGNORECASE
)
_ТИП_ПУНКТА_В_СКОБКАХ = re.compile(
    r"^(?:деревня|село|пос[её]лок|поселок|пгт|п\.г\.т\.|рп|станица|ст-ца|хутор|х|сл|аул|город|"
    r"кп|снт|тер|д|с|п|г)\.?\s+",
    re.IGNORECASE,
)


def _имя_пункта(текст: str) -> str | None:
    """«деревня Большая Дубрава» → «Большая Дубрава»."""
    имя = _ТИП_ПУНКТА_В_СКОБКАХ.sub("", текст.strip()).strip()
    return имя or None


def _пункт_и_родитель(пункт: str, с_типом: str) -> tuple[str, str | None]:
    """«Центральный (пгт Восход)» → («Центральный», «Восход»).

    Структура внутри пункта (микрорайон, зона, СНТ) у DaData носит имя
    родителя в скобках, а поля `city`/`settlement` при этом пусты или содержат
    те же скобки (бой 13.09, Белгородская обл.). Имя структуры остаётся
    пунктом — по нему сверяется «СНТ Василёк 6»; родитель уходит в город,
    если города нет, — по нему сверяется «пгт Восход, мкр Центральный».
    """
    в_скобках = _В_СКОБКАХ.search(с_типом) or _В_СКОБКАХ.search(пункт)
    имя = _В_СКОБКАХ.sub("", пункт).strip() or _имя_пункта(_В_СКОБКАХ.sub("", с_типом)) or ""
    родитель = _имя_пункта(в_скобках.group(1)) if в_скобках else None
    return имя, родитель


def _подсказки(данные: Any) -> list[Any]:
    if not isinstance(данные, dict):
        raise ProviderError("bad_response", None, "ответ не объект")
    подсказки = данные.get("suggestions", [])
    if not isinstance(подсказки, list):
        raise ProviderError("bad_response", None, "suggestions не список")
    return подсказки


def parse_places(данные: Any) -> list[PlaceHit]:
    """Ответ подсказок → места (без домов и улиц). Чистая функция."""
    out: list[PlaceHit] = []
    for s in _подсказки(данные):
        d = s.get("data") if isinstance(s, dict) else None
        if not isinstance(d, dict) or (d.get("house") or "").strip():
            continue
        уровень = str(d.get("fias_level") or "")
        try:
            lat, lon = float(d.get("geo_lat") or ""), float(d.get("geo_lon") or "")
        except (TypeError, ValueError):
            continue
        пункт = (d.get("settlement") or "").strip() or None
        город = (d.get("city") or "").strip() or None
        район = (d.get("area_with_type") or "").strip() or None
        регион = (d.get("region_with_type") or "").strip() or None
        улица = (d.get("street_with_type") or "").strip()
        if улица:
            # Улица без дома — место с точкой улицы (уровень ФИАС 7); улица с
            # домом сюда не попадает, а улица глубже (участок) — не место.
            if уровень == "7":
                имя_пункта, родитель = _пункт_и_родитель(
                    пункт or "", (d.get("settlement_with_type") or "").strip()
                )
                out.append(
                    PlaceHit(
                        name=улица,
                        kind="street",
                        settlement=имя_пункта or None,
                        # Улица внутри микрорайона/зоны: структура — массив улицы.
                        area=имя_пункта if родитель or (имя_пункта and город) else None,
                        city=город or родитель,
                        district=район,
                        region=регион,
                        lat=lat,
                        lon=lon,
                    )
                )
            continue
        # Планировочная структура (массив, СНТ, зона) у DaData лежит в
        # `settlement_with_type`, а пункт, к которому она относится, — в
        # скобках: «зона Южный (деревня Большая Дубрава)», «тер. СНТ
        # Василёк(Кобрино)» (бой 13.09, Гатчинский р-н).
        структура = (d.get("settlement_with_type") or пункт or "").strip() or None
        if уровень == "65" and структура:
            в_скобках = _В_СКОБКАХ.search(структура)
            пункт_структуры = _имя_пункта(в_скобках.group(1)) if в_скобках else None
            имя = _В_СКОБКАХ.sub("", структура).strip()
            # «мкр 7» у DaData: settlement «7», settlement_with_type «мкр 7»
            # — это та же структура, а не пункт внутри неё (бой 14.09, Ангарск:
            # строка карты выходила «мкр 7, 7, Ангарск»).
            тот_же = пункт is not None and (пункт == имя or имя.endswith(" " + пункт))
            out.append(
                PlaceHit(
                    name=имя,
                    kind="area",
                    settlement=пункт_структуры or (None if тот_же else пункт),
                    area=имя,
                    city=город,
                    district=район,
                    region=регион,
                    lat=lat,
                    lon=lon,
                )
            )
        elif уровень == "6" and пункт:
            out.append(
                PlaceHit(
                    name=(d.get("settlement_with_type") or пункт).strip(),
                    kind="settlement",
                    settlement=пункт,
                    area=None,
                    city=город,
                    district=район,
                    region=регион,
                    lat=lat,
                    lon=lon,
                )
            )
        elif уровень == "4" and город:
            out.append(
                PlaceHit(
                    name=(d.get("city_with_type") or город).strip(),
                    kind="city",
                    settlement=None,
                    area=None,
                    city=город,
                    district=район,
                    region=регион,
                    lat=lat,
                    lon=lon,
                )
            )
    return out


def parse_response(данные: Any) -> list[GeoHit]:
    """Ответ подсказок → дома. Чистая функция — проверяется на образце.

    Уровень дома — `fias_level` 8 (дом) или 9 (квартира того же дома); всё
    выше улицы — не дом, а `-1` — адрес, которого в справочнике нет. `qc_geo`
    — только про ТОЧКУ: `0` — координаты дома, `1` — ближайшего дома, `2` —
    улицы, `3` — пункта. Дом в справочнике есть при любом из них: у деревень
    координат домов часто нет вовсе (бой 12.09: «ул Рябиновая, 8, деревня
    Новое Заозерье» — `fias_level=8`, `qc_geo=3`, и адрес настоящий).
    Точность точки при этом — отдельный признак `precise` (18.09: «Лучистая
    7к3» в Сертолове — дом ФИАС, а точка `qc_geo=3` стояла в центре мкр
    Чёрная Речка, за полтора километра; LeadChat по `precise=False` спросит у
    Яндекса точку дома).
    Микрорайон у DaData — это `settlement` без улицы («мкр 9»): для вердикта
    он и есть «улица». Массив с участками (СНТ, ДНТ, КП) — тоже «улица», и
    номер участка в нём принимается домом даже без строки ФИАС (уровень 65,
    «тер. СНТ Светлый, д 23» — участков у справочника нет, а мастер едет в
    массив и спрашивает номер): точка — массива, `precise=False`.
    Структура внутри пункта у DaData носит родителя в скобках («ДНТ Василёк
    (село Нижнее Заречье)»): структура уходит в `area`, а пункт и город
    заполняются так, чтобы все три имени остались в ответе.
    """
    out: list[GeoHit] = []
    for s in _подсказки(данные):
        if not isinstance(s, dict):
            continue
        d = s.get("data") or {}
        if not isinstance(d, dict):
            continue
        дом = (d.get("house") or "").strip()
        if not дом:
            continue
        корпус = (d.get("block") or "").strip()
        тип_корпуса = (d.get("block_type") or "").strip()
        # `house` у DaData — только номер, корпус всегда отдельно в `block`:
        # добавляем всегда. Проверка подстрокой («2» in «12») теряла корпус, и
        # «д 12 к 2» становился домом «12» (ревью 12.09).
        if корпус:
            дом = f"{дом} {тип_корпуса} {корпус}".strip() if тип_корпуса else f"{дом} к {корпус}"
        улица = (d.get("street_with_type") or "").strip()
        пункт = (d.get("settlement") or "").strip()
        город = (d.get("city") or "").strip()
        с_типом = (d.get("settlement_with_type") or "").strip()
        структура = _структура(пункт, с_типом)
        if not улица and пункт:
            # Микрорайон, квартал, посёлок без улиц: «мкр 9, д 14», «поселок
            # Каменка, 9» — пункт и есть «улица». Пункт при этом остаётся
            # пунктом: по нему вердикт сверяет то, что назвал клиент.
            улица = _В_СКОБКАХ.sub("", с_типом or пункт).strip()
        if пункт and "(" in (с_типом or пункт):
            # «мкр Центральный (пгт Восход)», «тер. СНТ Василёк(Кобрино)»:
            # пункт — имя структуры, родитель из скобок — город, если города
            # нет. Иначе «Восход» не находился и дом уходил в «пункт не
            # совпал» (бой 13.09, Белгородская обл.); подмена пункта родителем
            # теряла бы «Василёк» у «СНТ Василёк 6» (ревью 13.09).
            # Город есть — родитель становится пунктом (18.09: «г Люберцы,
            # деревня Ивняково, тер. СНТ Рассвет, д 17» — Ивняково терялось,
            # и названная клиентом деревня «не подтверждалась»); структура
            # при этом остаётся в `area`.
            пункт, родитель = _пункт_и_родитель(пункт, с_типом)
            if город:
                пункт = родитель or пункт
            else:
                город = родитель or ""
        if not улица:
            continue
        try:
            lat = float(d.get("geo_lat") or "")
            lon = float(d.get("geo_lon") or "")
        except (TypeError, ValueError):
            continue
        уровень = str(d.get("fias_level") or "")
        qc = str(d.get("qc_geo") if d.get("qc_geo") is not None else "")
        точная = qc in ("0", "1")
        # Дом есть: в справочнике (уровень ФИАС 8/9/75) — при любой точности
        # точки; или НЕ в справочнике (уровень 7 — улица, 65 — структура), но
        # с координатами самого дома (qc_geo 0/1): «ул Маршала Иванова, 12» в
        # Петербурге у ФИАС только как «12 стр 1», а дом с точной точкой DaData
        # знает (стенд 13.09). Уровень 7 с точкой улицы — догадка, домом не
        # считается. Участок в массиве (уровень 65, «тер. СНТ Светлый, д
        # 23», 18.09) — дом: участков у ФИАС нет, номер знает только клиент.
        участок_массива = (
            уровень == "65"
            and not (d.get("street_with_type") or "").strip()
            and bool(_МАССИВ_С_УЧАСТКАМИ.search(с_типом or пункт))
        )
        дом_есть = (
            уровень in ("8", "9", "75") or (уровень in ("7", "65") and точная) or участок_массива
        )
        out.append(
            GeoHit(
                street=улица,
                house=дом,
                settlement=(пункт or None) if город else None,
                city=город or пункт or None,
                region=(d.get("region_with_type") or "").strip() or None,
                lat=lat,
                lon=lon,
                house_level=дом_есть,
                interpolated=False,
                settlement_kind="place",
                precise=точная,
                area=структура,
            )
        )
    return out


def _структура(пункт: str, с_типом: str) -> str | None:
    """Имя планировочной структуры из `settlement_with_type` — без родителя в
    скобках: «тер. СНТ Рассвет (деревня Ивняково)» → «СНТ Рассвет», «мкр
    Центральный (пгт Восход)» → «мкр Центральный». Обычный пункт («д
    Ивняково») структурой не является — None."""
    if not с_типом or not _ТИП_СТРУКТУРЫ.match(с_типом):
        return None
    имя = _В_СКОБКАХ.sub("", пункт).strip()
    # Имя уже несёт тип массива («СНТ Рассвет», «ДНТ Василёк») — тип ФИАС
    # («тер.», «днп») перед ним лишний; иначе тип — часть имени («мкр 9»,
    # «зона Южный»), а голое «тер.» перед типом массива («тер. СНТ
    # Светлый» при имени «Светлый») — нет.
    if имя and _МАССИВ_С_УЧАСТКАМИ.match(имя):
        return имя
    полное = _В_СКОБКАХ.sub("", с_типом).strip()
    return _ТЕР_ПЕРЕД_МАССИВОМ.sub("", полное) or None


_ТЕР_ПЕРЕД_МАССИВОМ = re.compile(r"^тер\.?\s+(?=(?:снт|днт|днп|тсн|кп)\b)", re.IGNORECASE)
