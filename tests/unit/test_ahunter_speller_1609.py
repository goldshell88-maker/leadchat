"""Ahunter и Спеллер — два бесплатных подсказчика без ключа (16.09).

1–2. Разбор строки ГАР и выбор варианта Спеллера уехали на шлюз (docs/46):
   `gateway/tests/test_gw_text.py`; обёртки LeadChat (строка запроса, слова
   улицы, кэш, подстановка) — `tests/unit/test_ahunter_speller_шлюз.py`.
3. Воркер: карта сырую строку не нашла → Спеллер поправил → карта подтвердила;
   Ahunter дал нормализованную строку → DaData подтвердила; подсказка с чужим
   пунктом («д Малиновка» при «Ленина 5» в городе) не берётся; Ahunter упал по
   сети — воркер не падает и десять минут его не спрашивает.
4. Настройки: переключатели и расход видны на экране.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from app.integrations import ahunter, gateway
from app.models import Client, ClientAddressCandidate, Conversation
from app.services import address_parse
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit.test_geocode_worker import ctx, строка

pytestmark = pytest.mark.anyio

# --- 3. воркер ---------------------------------------------------------------------

ОРСК_ДОМ = g.GeoHit(
    street="улица Ленина",
    house="5",
    settlement=None,
    city="Орск",
    region="Оренбургская область",
    lat=51.2,
    lon=58.5,
    house_level=True,
)


async def _строка_дома(seed_conversation, db_sessionmaker, текст: str):  # noqa: ANN001, ANN202
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.parse(текст, про_адрес=True)
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        return записано.candidate_id


async def test_спеллер_чинит_опечатку_и_карта_подтверждает(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленена 5")
    запросы: list[str] = []

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        запросы.append(query.street)
        return [ОРСК_ДОМ] if "ленина" in query.street.lower() else []

    async def fix_street(street, **kw):  # noqa: ANN001
        if kw.get("on_request"):
            await kw["on_request"]()
        return street.replace("Ленена", "Ленина")

    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.speller, "fix_street", fix_street)
    monkeypatch.setattr(worker.ahunter, "suggest", _нет)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await строка(db_sessionmaker, cid)
    assert row.geo_provider == "speller+nominatim"
    assert row.geo_formatted == "улица Ленина, 5, Орск"
    assert await worker.speller_calls_today(redis) == 1


async def _нет(query, **kw):  # noqa: ANN001
    return []


async def test_ahunter_нормализует_и_dadata_подтверждает(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """«57 квартал 8» в Ангарске: DaData сырую строку не нашла, Ahunter дал
    «кв-л 57, дом 8» — с ним DaData находит дом."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "angarsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.Found(
            street="57 квартал", house="8", raw="57 квартал 8", start=0, end=12, level="A"
        )
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        cid = записано.candidate_id
    дом = g.GeoHit(
        street="кв-л 57",
        house="8",
        settlement=None,
        city="Ангарск",
        region="Иркутская обл",
        lat=52.5,
        lon=103.9,
        house_level=True,
    )
    тексты: list[str] = []

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        тексты.append(query.street)
        return [дом] if query.street == "кв-л 57" else []

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [
            ahunter.Suggested(
                street="кв-л 57",
                house="8",
                city="Ангарск",
                settlement=None,
                district=None,
                region="обл Иркутская",
                formatted="обл Иркутская, г Ангарск, кв-л 57, дом 8",
            )
        ]

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    monkeypatch.setattr(worker.speller, "fix_street", _нет_улицы)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await строка(db_sessionmaker, cid)
    assert row.geo_provider == "ahunter+dadata"
    assert тексты == ["57 квартал", "кв-л 57"]
    assert await worker.ahunter_calls_today(redis) == 1


async def _нет_улицы(street, **kw):  # noqa: ANN001
    return None


async def test_подсказка_с_чужим_пунктом_не_берётся() -> None:
    q = g.Query(
        region="Костромская область", city="Кострома", settlement=None, street="Ленина", house="5"
    )
    assert not worker._подсказка_о_том_же(q, None, "Малиновка")
    assert worker._подсказка_о_том_же(q, "Кострома", None)
    assert not worker._подсказка_о_том_же(q, "Буй", None)
    с_пунктом = dataclasses.replace(q, settlement="Малиновка")
    assert worker._подсказка_о_том_же(с_пунктом, None, "Малиновка")
    assert not worker._подсказка_о_том_же(с_пунктом, None, "Середняя")


async def test_ahunter_упал_по_сети_воркер_не_падает_и_ждёт(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Звенигародская 1")

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        raise g.GeocodeError("ahunter", "network")

    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    monkeypatch.setattr(worker.speller, "fix_street", _нет_улицы)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert await worker._ahunter_лежит(redis)


async def test_чужая_улица_от_ahunter_вердикт_не_пускает(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Ahunter предложил «Советскую» на «Звенигародскую» — вердикт по разбору
    клиента отказывает, как и Саджесту."""
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Звенигародская 1")

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        return [
            ahunter.Suggested(
                street="ул Советская",
                house="1",
                city="Орск",
                settlement=None,
                district=None,
                region=None,
                formatted="г Орск, ул Советская, дом 1",
            )
        ]

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        return (
            [dataclasses.replace(ОРСК_ДОМ, street="Советская улица", house="1")]
            if "совет" in query.street.lower()
            else []
        )

    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    monkeypatch.setattr(worker.speller, "fix_street", _нет_улицы)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND


# --- 4. настройки ------------------------------------------------------------------


async def test_переключатели_ahunter_и_спеллера(client, tokens):  # noqa: ANN001
    hdr = {"Authorization": f"Bearer {tokens['admin']}"}
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"ahunter_enabled": False, "speller_enabled": False},
        headers=hdr,
    )
    assert res.status_code == 200, res.text
    assert (res.json()["ahunter_enabled"], res.json()["speller_enabled"]) == (False, False)
    again = await client.get("/api/v1/settings/address-detect", headers=hdr)
    assert again.json()["speller_daily_limit"] == 9000


async def test_выключенные_подсказчики_не_спрашиваются(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from app.services import app_settings

    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленена 5")
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_AHUNTER_ENABLED: False,
                app_settings.ADDRESS_GEO_SPELLER_ENABLED: False,
            },
            user_id=None,
        )
        await s.commit()
    звали: list[str] = []

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        звали.append("ahunter")
        return []

    async def fix_street(street, **kw):  # noqa: ANN001
        звали.append("speller")
        return None

    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    monkeypatch.setattr(worker.speller, "fix_street", fix_street)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert звали == []
    assert (await строка(db_sessionmaker, cid)).id == cid


# --- 6. ревью 16.09: кулдаун, потолки, чужой дом, границы -----------------------


async def test_кулдаун_ahunter_не_пускает_второй_поход(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Звенигародская 1")
    походы = 0

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        nonlocal походы
        походы += 1
        raise g.GeocodeError("ahunter", "bad_response", 200)

    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    # Вторая проверка той же строки в те же десять минут — Ahunter не зовётся.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status = g.GEO_PENDING
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert походы == 1


async def test_потолок_dadata_внутри_подсказок_не_роняет_кандидата(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """DaData выбрана в потолок на первом же кандидате — карты режима всё
    равно проверяют исправленную улицу."""
    from app.services import app_settings

    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленена 5")
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT: 1}, user_id=None
        )
        await s.commit()
    dadata_походы: list[str] = []

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()  # второй поход — за потолком: GeocodeError(limit)
        dadata_походы.append(query.street)
        return []

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        return [ОРСК_ДОМ] if "ленина" in query.street.lower() else []

    async def fix_street(street, **kw):  # noqa: ANN001
        return street.replace("Ленена", "Ленина")

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.speller, "fix_street", fix_street)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert dadata_походы == ["ул Ленена"]
    assert (await строка(db_sessionmaker, cid)).geo_provider == "speller+nominatim"


async def test_подсказчики_зовутся_и_после_улица_не_та(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленена 5")

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        # На сырую улицу карта отдаёт дом на другой улице → «улица не та».
        if "ленена" in query.street.lower():
            return [dataclasses.replace(ОРСК_ДОМ, street="улица Мира")]
        return [ОРСК_ДОМ]

    async def fix_street(street, **kw):  # noqa: ANN001
        return street.replace("Ленена", "Ленина")

    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.speller, "fix_street", fix_street)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT


async def test_ahunter_с_другим_номером_дома_не_берётся(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленина 13")
    запросы: list[str] = []

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        return [
            ahunter.Suggested(
                street="ул Ленина",
                house=h,
                city="Орск",
                settlement=None,
                district=None,
                region=None,
                formatted=f"г Орск, ул Ленина, дом {h}",
            )
            for h in ("130", "13а", "13")
        ]

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        запросы.append(query.house)
        return []

    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    # Сырой «13» плюс ни одного чужого номера: «13» Ahunter совпал с исходным
    # ключом и повторно не проверялся.
    assert запросы == ["13"]


async def test_переключатели_по_одному(seed_conversation, db_sessionmaker, redis, monkeypatch):
    from app.services import app_settings

    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка_дома(seed_conversation, db_sessionmaker, "ул Ленена 5")
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_SPELLER_ENABLED: False}, user_id=None
        )
        await s.commit()
    звали: list[str] = []

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        звали.append("ahunter")
        return []

    async def fix_street(street, **kw):  # noqa: ANN001
        звали.append("speller")
        return None

    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    monkeypatch.setattr(worker.speller, "fix_street", fix_street)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert звали == ["ahunter"]


# --- 7. хвосты ревью 16.09: формы дома ГАР, микрорайон в улице подсказки ----------


async def test_микрорайон_клиента_в_улице_подсказки(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """«мкр Байкальск, ул Боткина 5» в Ангарске: справочник пишет микрорайон
    в улице, а не пунктом — подсказка про тот же микрорайон берётся."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "angarsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.Found(
            street="ул Боткина",
            house="5",
            raw="мкр Байкальск ул Боткина 5",
            start=0,
            end=26,
            level="A",
            settlement="Байкальск",
            settlement_type="микрорайон",
        )
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        cid = записано.candidate_id
    дом = g.GeoHit(
        street="мкр Байкальск ул Боткина",
        house="5",
        settlement="Байкальск",
        city="Ангарск",
        region="Иркутская обл",
        lat=52.5,
        lon=103.9,
        house_level=True,
    )

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [дом] if query.street.startswith("мкр") else []

    async def ahunter_suggest(query, **kw):  # noqa: ANN001
        return [
            ahunter.Suggested(
                street="мкр Байкальск ул Боткина",
                house="5",
                city="Ангарск",
                settlement=None,
                district=None,
                region="обл Иркутская",
                formatted="обл Иркутская, г Ангарск, мкр Байкальск, ул Боткина, дом 5",
            )
        ]

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", _нет)
    monkeypatch.setattr(worker.ahunter, "suggest", ahunter_suggest)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await строка(db_sessionmaker, cid)).geo_provider == "ahunter+dadata"
