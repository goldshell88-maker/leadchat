"""Адрес постепенно: сначала место, потом улица (владелец 13.09).

Клиент пишет «Гатчинский р-н. Д. Малая Сосновка, массив Южный.» — улицы
ещё нет, а перейти на карту хочется сразу. Такое сообщение — строка-кандидат
`kind=place`: DaData даёт точку массива/пункта, карточка — ссылку на карту,
автозапись кладёт место в пустую карточку. Следующая реплика с улицей и домом
заменяет место, а всё, что писал человек, не трогается.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from leadchat_gateway.providers import dadata as gw_dadata

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

ГАТЧИНА = City("Гатчина", "Ленинградская область", "Europe/Moscow")


def parse_places(ответ: dict) -> list[g.PlaceHit]:
    """Разбор живёт в шлюзе (docs/46); вердикту нужны dataclass-ы LeadChat."""
    return [g.PlaceHit(**h.model_dump()) for h in gw_dadata.parse_places(ответ)]


def parse_response(ответ: dict) -> list[g.GeoHit]:
    return [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ответ)]


СООБЩЕНИЕ = "Гатчинский р-н. Д. Малая Сосновка, массив Южный."

#: Живой ответ DaData 13.09 (поля обрезаны): массив — уровень 65, пункт в скобках.
ОТВЕТ = {
    "suggestions": [
        {
            "value": "Ленинградская обл, Гатчинский р-н, деревня Малая Сосновка, зона Южный",
            "data": {
                "fias_level": "65",
                "region_with_type": "Ленинградская обл",
                "area_with_type": "Гатчинский р-н",
                "city": None,
                "settlement": "Южный",
                "settlement_with_type": "зона Южный (деревня Малая Сосновка)",
                "geo_lat": "59.612347",
                "geo_lon": "30.141862",
                "qc_geo": "3",
            },
        },
        {
            "value": "…, зона Южный, д 4",
            "data": {
                "fias_level": "8",
                "house": "4",
                "settlement_with_type": "зона Южный (деревня Малая Сосновка)",
                "geo_lat": "59.61",
                "geo_lon": "30.14",
                "region_with_type": "Ленинградская обл",
            },
        },
        {
            "value": "Ленинградская обл, Гатчинский р-н, д Малая Сосновка",
            "data": {
                "fias_level": "6",
                "region_with_type": "Ленинградская обл",
                "area_with_type": "Гатчинский р-н",
                "settlement": "Малая Сосновка",
                "settlement_with_type": "д Малая Сосновка",
                "geo_lat": "59.61",
                "geo_lon": "30.14",
                "qc_geo": "4",
            },
        },
    ]
}


@pytest.mark.parametrize(
    ("текст", "значение"),
    [
        (СООБЩЕНИЕ, "деревня Малая Сосновка, массив Южный, Гатчинский р-н"),
        ("д. Малая Сосновка", "деревня Малая Сосновка"),
        ("деревня Ивановка, СНТ Ромашка", "деревня Ивановка, СНТ Ромашка"),
        ("СНТ Ромашка", "СНТ Ромашка"),
        ("г. Гатчина, мкр Аэродром", "город Гатчина, мкр Аэродром"),
        ("п. Сосновка", "посёлок Сосновка"),
        # Не место: один район, речь вокруг, улица с домом.
        ("Гатчинский район", None),
        ("живу в деревне Ивановка, приезжайте завтра к обеду часам к двум", None),
        ("мкр 15", "мкр 15"),
        ("Да, 10 мкр", "мкр 10"),
        ("Гатчинский р-н, д Ивановка, ул Лесная 3", None),
        ("с мужем в деревне", None),
        ("д. 5 кв 3", None),
        ("Телевизор Samsung", None),
    ],
)
def test_разбор_места(текст: str, значение: str | None) -> None:
    found = address_parse.parse_place(текст)
    assert (found.value if found is not None else None) == значение
    if found is not None:
        assert found.kind == address_parse.KIND_PLACE and found.street == "" and found.house == ""
        assert address_parse.quote_holds(found, текст)


def test_dadata_места_и_вердикт() -> None:
    hits = parse_places(ОТВЕТ)
    assert [(h.kind, h.name, h.settlement) for h in hits] == [
        ("area", "зона Южный", "Малая Сосновка"),
        ("settlement", "д Малая Сосновка", "Малая Сосновка"),
    ]
    found = address_parse.parse_place(СООБЩЕНИЕ)
    assert found is not None
    место = g.Place(found.settlement, found.settlement_type, found.area, found.district)
    статус, hit = g.place_verdict(место, ГАТЧИНА, hits)
    assert статус == g.GEO_EXACT and hit is not None and hit.kind == "area"
    assert g.format_place(hit, место) == "зона Южный, деревня Малая Сосновка, Гатчинский р-н"
    # Без массива — точка пункта, не массива.
    только_пункт = g.Place("Малая Сосновка", "деревня", None, None)
    статус, hit = g.place_verdict(только_пункт, ГАТЧИНА, hits)
    assert статус == g.GEO_EXACT and hit is not None and hit.kind == "settlement"
    # Чужая область — отказ.
    череповец = City("Череповец", "Вологодская область", "Europe/Moscow")
    assert g.place_verdict(место, череповец, hits)[0] == g.GEO_REGION_MISMATCH
    assert address_parse.settlement_hints([СООБЩЕНИЕ]) == ["Малая Сосновка"]


async def _строка(seed_conversation, db_sessionmaker, текст: str, *, место: bool):  # noqa: ANN001
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "gatchina"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.parse_place(текст) if место else address_parse.parse(текст)
        assert found is not None
        # Реплика строки — её текст, как на живом пути (сторож автозаписи 19.09
        # перечитывает реплику строки нынешним разбором).
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed_conversation.message_id))
        ).scalar_one()
        сообщение.body = текст
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


def ctx(db_sessionmaker, redis) -> dict:  # noqa: ANN001
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def test_место_получает_точку_ложится_в_карточку_и_уступает_дому(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    # Раньше тест молча ходил в живой OSM (2 с на прогон); карта здесь не при чём.
    async def осм_молчит(query, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.nominatim, "search", осм_молчит)

    async def search_place(place, *, region, **kw):  # noqa: ANN001
        await kw["on_request"]()
        assert region == "Ленинградская область"
        assert place.query_text == "Гатчинский р-н деревня Малая Сосновка массив Южный"
        return parse_places(ОТВЕТ)

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    cid = await _строка(seed_conversation, db_sessionmaker, СООБЩЕНИЕ, место=True)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.kind == "place" and row.geo_provider == "dadata"
        assert row.geo_formatted == "зона Южный, деревня Малая Сосновка, Гатчинский р-н"
        assert (row.geo_lat, row.geo_lon) == (59.612347, 30.141862)
        card = await s.get(Client, seed_conversation.client_id)
        view = await clients_svc.identity_view(s, card)
        (кандидат,) = view["address_candidates"]
        assert кандидат["kind"] == "place" and кандидат["geo"]["lat"] == 59.612347

    # Автозапись: место — в пустую карточку.
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address == "зона Южный, деревня Малая Сосновка, Гатчинский р-н"
        assert card.address_candidate_id == cid and card.address_set_by_id is None

    # Улица и дом следующей репликой — дом заменяет место.
    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [
            g.GeoHit(
                street="ул Лесная",
                house="3",
                settlement="Малая Сосновка",
                city=None,
                region="Ленинградская обл",
                lat=59.62,
                lon=30.15,
                house_level=True,
            )
        ]

    async def без_точки(city, region, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    # Реплика-место в ленте — подсказка пункта для следующей строки с улицей.
    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                external_message_id="in-place",
                direction="in",
                sender_type="client",
                body=СООБЩЕНИЕ,
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await s.commit()
    cid2 = await _строка(seed_conversation, db_sessionmaker, "ул Лесная 3", место=False)
    async with db_sessionmaker() as s:  # _строка обнуляет адрес ради фикстуры — вернём место
        card = await s.get(Client, seed_conversation.client_id)
        card.address = "зона Южный, деревня Малая Сосновка, Гатчинский р-н"
        card.address_candidate_id = cid
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2) == g.GEO_EXACT
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == cid2
        assert card.address.startswith("ул Лесная, 3")

    # А адрес, который ввёл человек, автоматика не трогает.
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        card.address = "мой адрес"
        card.address_candidate_id = cid
        card.address_set_by_id = (
            seed_conversation.user_id if hasattr(seed_conversation, "user_id") else None
        )
        await s.commit()
    if getattr(seed_conversation, "user_id", None):
        assert (
            await worker.autofill_address(
                ctx(db_sessionmaker, redis), seed_conversation.conversation_id
            )
            == "skip"
        )


async def test_без_dadata_место_не_найдено_а_не_вечно_pending(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка(seed_conversation, db_sessionmaker, СООБЩЕНИЕ, место=True)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_ENABLED: False}, user_id=None
        )
        await s.commit()


# ── Улица без дома — тоже место; «дом N» — отдельной репликой (владелец 13.09) ──

КОРОЛЁВ = City("Королёв", "Московская область", "Europe/Moscow")

#: Живой ответ DaData на «улица Горького» в Королёве: уровень 7 — улица без дома.
ОТВЕТ_УЛИЦА = {
    "suggestions": [
        {
            "value": "Московская обл, г Королев, ул Горького",
            "data": {
                "fias_level": "7",
                "region_with_type": "Московская обл",
                "area_with_type": None,
                "city": "Королев",
                "settlement": None,
                "street_with_type": "ул Горького",
                "geo_lat": "55.921",
                "geo_lon": "37.827",
                "qc_geo": "2",
            },
        },
        {
            "value": "Московская обл, г Мытищи, ул Горького",
            "data": {
                "fias_level": "7",
                "region_with_type": "Московская обл",
                "city": "Мытищи",
                "street_with_type": "ул Горького",
                "geo_lat": "55.91",
                "geo_lon": "37.73",
                "qc_geo": "2",
            },
        },
        {
            "value": "Московская обл, г Королев, ул Горького, д 5",
            "data": {
                "fias_level": "8",
                "house": "5",
                "city": "Королев",
                "street_with_type": "ул Горького",
                "geo_lat": "55.92",
                "geo_lon": "37.83",
                "region_with_type": "Московская обл",
            },
        },
    ]
}


@pytest.mark.parametrize(
    ("текст", "улица", "пункт"),
    [
        ("Надо маме в Королёве ул. Горького, она сама встретит", "ул. Горького", None),
        ("Калуга, улица 65 лет Победы", "улица 65 лет Победы", None),
        ("п. Сосновка ул Лесная", "ул Лесная", "Сосновка"),
        ("проспект Мира", "проспект Мира", None),
        ("8 марта улица", "8 марта улица", None),
        # Не улица: вопрос, речь, массив.
        ("Здравствуйте, улица какая у вас?", None, None),
        ("СНТ Горелый лес", None, None),
        ("мы на улице ждём", None, None),
    ],
)
def test_улица_без_дома_как_место(текст: str, улица: str | None, пункт: str | None) -> None:
    found = address_parse.parse_place(текст)
    if улица is None:
        assert found is None or found.street == ""
        return
    assert found is not None and found.kind == address_parse.KIND_PLACE
    assert found.street == улица and found.house == "" and found.settlement == пункт
    assert address_parse.quote_holds(found, текст)
    assert found.value.startswith(улица)


def test_dadata_улица_уровня_7_и_вердикт_по_городу() -> None:
    hits = parse_places(ОТВЕТ_УЛИЦА)
    assert [(h.kind, h.name, h.city) for h in hits] == [
        ("street", "ул Горького", "Королев"),
        ("street", "ул Горького", "Мытищи"),
    ]
    место = g.Place(None, None, None, None, street="ул. Горького")
    assert место.query_text == "улица Горького"
    статус, hit = g.place_verdict(место, КОРОЛЁВ, hits)
    assert статус == g.GEO_EXACT and hit is not None and hit.city == "Королев"
    assert g.format_place(hit, место) == "ул Горького, Королев"
    # Другая улица — отказ по улице, а не «не найдено».
    чужая = g.Place(None, None, None, None, street="ул Ленина")
    assert g.place_verdict(чужая, КОРОЛЁВ, hits)[0] == g.GEO_STREET_MISMATCH
    # Улица есть, но в другом городе области — отказ носит своё имя (ревью 13.09).
    химки = City("Химки", "Московская область", "Europe/Moscow")
    assert g.place_verdict(место, химки, hits)[0] == g.GEO_CITY_MISMATCH


def test_dadata_микрорайон_в_пгт_со_скобками() -> None:
    """«мкр Центральный (пгт Северный)» — пункт в скобках (бой 13.09, Белгород)."""
    ответ = {
        "suggestions": [
            {
                "value": "Белгородская обл, Белгородский р-н, пгт Северный, мкр Центральный, д 17",
                "data": {
                    "fias_level": "8",
                    "qc_geo": "3",
                    "house": "17",
                    "city": None,
                    "settlement": "Центральный (пгт Северный)",
                    "settlement_with_type": "мкр Центральный (пгт Северный)",
                    "street_with_type": None,
                    "geo_lat": "50.683217",
                    "geo_lon": "36.569124",
                    "region_with_type": "Белгородская обл",
                },
            }
        ]
    }
    (hit,) = parse_response(ответ)
    assert (hit.street, hit.house, hit.city) == ("мкр Центральный", "17", "Северный")
    found = address_parse.parse("пгт Северный, мкр-н Центральный, д. 17, кв. 36, подъезд 2")
    assert found is not None and found.settlement == "Северный"
    parsed = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
    )
    белгород = City("Белгород", "Белгородская область", "Europe/Moscow")
    статус, выбран = g.verdict(parsed, белгород, [hit])
    assert статус == g.GEO_EXACT and выбран is hit
    assert g.format_address(hit, parsed, with_settlement=True) == "мкр Центральный, 17, Северный"


def test_число_перед_типом_улицы_не_приставка() -> None:
    """«часов 11 улица Зеленогорская 17/3» — «11» это время (бой 13.09, Благовещенск)."""
    found = address_parse.parse("Можно будет часов 11 улица Зеленогорская 17/3кв 4")
    assert found is not None
    assert (found.street, found.house) == ("улица Зеленогорская", "17/3")
    assert found.parts == {"office": "4"}
    for текст, улица in [
        ("2-я улица Строителей 7", "2-я улица Строителей"),
        ("11 линия 4", "11 линия"),
        ("2 Советская улица 5", "2 Советская улица"),
    ]:
        f = address_parse.parse(текст)
        assert f is not None and f.street == улица, текст


async def test_дом_отдельной_репликой_продолжает_место(
    db, redis, make_avito_account, db_sessionmaker
):
    """«пгт Северный, мкр Центральный» → «дом 17»: одна строка-дом с улицей места."""
    from app.services.inbound import apply_inbound_event
    from tests.unit.test_address_inbound_0909 import T0, событие

    account = await make_avito_account(111222555)
    await apply_inbound_event(db, redis, account, событие("пгт Северный, мкр-н Центральный"))
    await apply_inbound_event(
        db, redis, account, событие("дом 17, кв 36", msg="am-2", when=T0.replace(minute=3))
    )
    async with db_sessionmaker() as s:
        строки = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
    строки.sort(key=lambda r: r.detected_at)
    assert [r.kind for r in строки] == ["place", "house"]
    дом = строки[1]
    assert (дом.street, дом.house, дом.settlement, дом.office) == (
        "мкр-н Центральный",
        "17",
        "Северный",
        "36",
    )
    # Цитата — реплика места и ФРАЗА с домом, а не вся реплика (в ней бывает телефон).
    assert дом.raw == "пгт Северный, мкр-н Центральный; дом 17"
    # Через сутки «дом 17» — уже не продолжение: место могло быть про другой заказ.
    await apply_inbound_event(
        db, redis, account, событие("дом 7", msg="am-3", when=T0 + timedelta(days=2))
    )
    async with db_sessionmaker() as s:
        всего = (await s.execute(sa.select(sa.func.count(ClientAddressCandidate.id)))).scalar()
    assert всего == 2


@pytest.mark.parametrize(
    ("текст", "улица", "дом", "город", "уровень"),
    [
        # Бой 13.09, Бердск: «Бердск» — город объявления, «Микрорайон» — улица
        # без названия (DaData: «ул Микрорайон, д 23»), «23» — дом, не микрорайон.
        (
            "Бердск микрорайон 23 кв6 2 подъезд этаж кв домофон работает",
            "микрорайон",
            "23",
            "Бердск",
            "A",
        ),
        ("Бердск, микрорайон 23, кв 6", "микрорайон", "23", "Бердск", "A"),
        ("Бердск Каштановая 63", "Каштановая", "63", "Бердск", "B"),
        ("Здравствуйте. Бердск, Ленина 5", "Ленина", "5", "Бердск", "B"),
        # Микрорайон с домом после — по-прежнему микрорайон.
        ("Бердск, мкр 18 д 5", "18 мкр", "5", "Бердск", "A"),
        # Не город: улица с таким же именем, город без адреса.
        ("Октябрьский 5", "Октябрьский", "5", None, "C"),
    ],
)
def test_город_объявлений_без_буквы_г(
    текст: str, улица: str, дом: str, город: str | None, уровень: str
) -> None:
    found = address_parse.parse(текст)
    assert found is not None
    assert (found.street, found.house, found.level) == (улица, дом, уровень)
    # С запятой город — пункт перед улицей (как было), без запятой — город клиента.
    assert город in (found.settlement, found.locality)
    assert address_parse.quote_holds(found, текст)
    assert found.house in found.raw


def test_голый_мкр_остаётся_местом() -> None:
    assert address_parse.parse("мкр 15") is None
    found = address_parse.parse_place("мкр 15")
    assert found is not None and found.area == "мкр 15"


@pytest.mark.parametrize(
    "текст",
    [
        # Речь с типом улицы — не место (ревью 13.09).
        "на улице дождь",
        "на улице холодно, приезжайте побыстрее",
        "проезд платный?",
        "нужно поменять розетку и пр. мелочи",
        "трактор сломался",
        "извините, проспали утром",
        "телефонная линия шумит",
        "напишите улицу пожалуйста",
        "рядом м-н Магнит",
    ],
)
def test_речь_с_типом_улицы_не_место(текст: str) -> None:
    assert address_parse.parse_place(текст) is None


@pytest.mark.parametrize(
    ("текст", "улица", "пункт"),
    [
        ("п. Сосновка ул. Лесная", "ул. Лесная", "Сосновка"),
        ("пгт Северный ул. Ленина", "ул. Ленина", "Северный"),
        ("ул. Горького она сама встретит", "ул. Горького", None),
        ("на улице холодно, приезжайте на ул Ленина", "ул Ленина", None),
        ("улица Ленина город Королёв", "улица Ленина", "Королёв"),
        ("3-я улица Строителей", "3-я улица Строителей", None),
        ("улица 8 марта", "улица 8 марта", None),
    ],
)
def test_улица_без_дома_рядом_с_речью_и_пунктом(текст: str, улица: str, пункт: str | None) -> None:
    found = address_parse.parse_place(текст)
    assert found is not None and (found.street, found.settlement) == (улица, пункт)


def test_телефон_с_точкой_перед_хвостом_и_домофон_не_адрес() -> None:
    """«Домофона нету 8 (900) …-46. 1 этаж» (бой 13.09): номер найден, адреса нет."""
    from app.services import phone_parse

    текст = "Домофона нету 8 (900) 111-22-46. 1 этаж"
    (номер,) = phone_parse.find_all(текст)
    assert номер.raw == "8 (900) 111-22-46"
    assert address_parse.parse(текст) is None
    assert address_parse.parts_only(текст) == {"floor": "1"}


def test_микрорайон_прилип_к_числу_с_домом_через_дефис() -> None:
    """«7мик-д64-кв 41» (бой 13.09, Ангарск): 7-й микрорайон, дом 64, кв 41."""
    found = address_parse.parse("7мик-д64-кв 41")
    assert found is not None
    assert (found.street, found.house, found.parts) == ("7 мик", "64", {"office": "41"})
    assert g.expand_street(found.street) == "7 микрорайон"
    assert g._улица_не_шире("7-й мкр", found.street)


async def test_дом_к_пункту_с_сокращённым_типом(db, redis, make_avito_account, db_sessionmaker):
    """«п. Сосновка» → «дом 9»: дом по пункту без улицы, ключ «посёлок Сосновка, 9»."""
    from app.services.inbound import apply_inbound_event
    from tests.unit.test_address_inbound_0909 import T0, событие

    account = await make_avito_account(111222666)
    await apply_inbound_event(db, redis, account, событие("п. Сосновка"))
    await apply_inbound_event(
        db, redis, account, событие("дом 9", msg="am-2", when=T0.replace(minute=2))
    )
    async with db_sessionmaker() as s:
        строки = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
    строки.sort(key=lambda r: r.detected_at)
    assert [(r.kind, r.street, r.house, r.settlement, r.value) for r in строки] == [
        ("place", "", "", "Сосновка", "посёлок Сосновка"),
        ("house", "", "9", "Сосновка", "посёлок Сосновка, 9"),
    ]
    # Поправка «дом 11» после склеенного дома — то же место, новый дом.
    await apply_inbound_event(
        db, redis, account, событие("дом 11", msg="am-3", when=T0.replace(minute=3))
    )
    async with db_sessionmaker() as s:
        значения = sorted(
            (await s.execute(sa.select(ClientAddressCandidate.value))).scalars().all()
        )
    assert "посёлок Сосновка, 11" in значения


def test_число_как_имя_улицы_перед_домом_и_днт() -> None:
    """«Ольхово днт Ромашка 5 ул дом 148» (бой 13.09, Бурятия; владелец
    искал руками 20 минут): «5 ул» — 5-я улица, дом 148, ДНТ Ромашка."""
    found = address_parse.parse("Ольхово днт Ромашка 5 ул дом 148")
    assert found is not None
    assert (found.street, found.house, found.settlement, found.settlement_type) == (
        "5 ул",
        "148",
        "Ромашка",
        "ДНТ",
    )
    assert address_parse.quote_holds(found, "Ольхово днт Ромашка 5 ул дом 148")
    # «3 линия, 12» в СНТ — 3 это улица; без «дом» число перед типом — не улица.
    f2 = address_parse.parse("снт Ромашка, 3 линия, 12")
    assert f2 is not None and (f2.street, f2.house) == ("3 линия", "12")
    assert address_parse.parse("Можно будет часов 11 улица Зеленогорская 17/3").street == (
        "улица Зеленогорская"
    )
    # Живой ответ DaData: дом внутри ДНТ с пунктом в скобках — exact.
    ответ = {
        "suggestions": [
            {
                "value": "Респ Бурятия, Тарбагатайский р-н, днп ДНТ Ромашка, ул 5-я, д 148",
                "data": {
                    "fias_level": "8",
                    "qc_geo": "0",
                    "house": "148",
                    "city": None,
                    "settlement": "ДНТ Ромашка (село Нижнее Заречье)",
                    "settlement_with_type": "днп ДНТ Ромашка (село Нижнее Заречье)",
                    "street_with_type": "ул 5-я",
                    "area_with_type": "Тарбагатайский р-н",
                    "region_with_type": "Респ Бурятия",
                    "geo_lat": "51.731946",
                    "geo_lon": "107.538217",
                },
            }
        ]
    }
    hits = parse_response(ответ)
    parsed = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
    )
    улан_удэ = City("Улан-Удэ", "Республика Бурятия", "Asia/Irkutsk")
    статус, hit = g.verdict(parsed, улан_удэ, hits)
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, parsed, with_settlement=True).startswith("ул 5-я, 148")


def test_время_перед_названием_и_буква_перед_телефоном() -> None:
    """Бой 13.09: «к 11 звягинцева 7/2» — «11» время, улица «звягинцева»;
    «Тургенева 58 т 8950…» — «т» перед номером не литера дома."""
    f = address_parse.parse("Давайте к 11 звягинцева 7/2 4 подъезд 86 кв")
    assert f is not None and (f.street, f.house, f.parts) == (
        "звягинцева",
        "7/2",
        {"office": "86", "entrance": "4"},
    )
    f = address_parse.parse("Да удобно, тепличный, Тургенева 58 т 89500000000.")
    assert f is not None and (f.street, f.house) == ("Тургенева", "58")
    for текст, улица in [("5 Кольцевая 3", "5 Кольцевая"), ("11 линия 4", "11 линия")]:
        f = address_parse.parse(текст)
        assert f is not None and f.street == улица


async def test_место_из_прошлой_реплики_уходит_в_запрос_дома(
    db, redis, make_avito_account, db_sessionmaker
):
    """«СНТ Калинка» → «Лесной проезд 4» (бой 13.09, Иркутск): дом получает пункт
    «Калинка» (СНТ) из места — в запрос к карте, а не только на сверку."""
    from app.services.inbound import apply_inbound_event
    from tests.unit.test_address_inbound_0909 import T0, событие

    account = await make_avito_account(111222888)
    await apply_inbound_event(db, redis, account, событие("СНТ Калинка"))
    await apply_inbound_event(
        db, redis, account, событие("Лесной проезд 4", msg="am-2", when=T0.replace(minute=2))
    )
    async with db_sessionmaker() as s:
        строки = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
    дом = next(r for r in строки if r.kind == "house")
    assert (дом.street, дом.house, дом.settlement, дом.settlement_type, дом.area) == (
        "Лесной проезд",
        "4",
        "Калинка",
        "СНТ",
        "СНТ Калинка",
    )
    assert дом.raw == "СНТ Калинка; Лесной проезд 4"


def test_место_с_городом_клиента_ищется_только_в_нём() -> None:
    """«Хабаровск, Индустриальный» (бой 13.09): тёзка в Комсомольске-на-Амуре
    — не то место; без города — как раньше."""
    hits = parse_places(
        {
            "suggestions": [
                {
                    "value": "Хабаровский край, г Комсомольск-на-Амуре, мкр Индустриальный",
                    "data": {
                        "fias_level": "65",
                        "region_with_type": "Хабаровский край",
                        "city": "Комсомольск-на-Амуре",
                        "settlement": "Индустриальный",
                        "settlement_with_type": "мкр Индустриальный",
                        "geo_lat": "50.5",
                        "geo_lon": "137.0",
                    },
                }
            ]
        }
    )
    хабаровск = City("Хабаровск", "Хабаровский край", "Asia/Vladivostok")
    место = g.Place("Индустриальный", None, None, None, locality="Хабаровск")
    assert g.place_verdict(место, хабаровск, hits)[0] == g.GEO_CITY_MISMATCH
    без_города = g.Place("Индустриальный", None, None, None)
    assert g.place_verdict(без_города, хабаровск, hits)[0] == g.GEO_EXACT


def test_модель_не_заводит_место_из_голого_имени() -> None:
    from app.services import address_llm

    чтение = address_llm.parse_reading(
        {
            "street": "",
            "house": "",
            "settlement": "Индустриальный",
            "city": "Хабаровск",
            "confidence": "high",
        }
    )
    assert чтение is not None
    assert address_llm.to_found(чтение, ["Где находится объект: Хабаровск, Индустриальный"]) is None
    с_типом = address_llm.parse_reading(
        {
            "street": "",
            "house": "",
            "settlement": "Ивановка",
            "settlement_type": "деревня",
            "confidence": "high",
        }
    )
    assert с_типом is not None
    assert address_llm.to_found(с_типом, ["деревня Ивановка"]) is not None


def test_тройка_микрорайон_дом_квартира_и_массив_с_границей() -> None:
    """«14а- 76-21» (бой 13.09, Нефтеюганск) — мкр 14а, дом 76, кв 21, уровень C;
    дата и телефон не подходят. «время не терять» — не массив «тер+ять»."""
    f = address_parse.parse("14а- 76-21")
    assert f is not None and (f.street, f.house, f.parts, f.level) == (
        "14а мкр",
        "76",
        {"office": "21"},
        "C",
    )
    assert address_parse.quote_holds(f, "14а- 76-21")
    assert address_parse.parse("12-09-2026") is None
    assert address_parse.parse_place("Чтобы время не терять") is None
    assert address_parse.parse_place("тер. Ромашка") is not None


def test_жк_как_пункт_и_двухсловное_имя_перед_улицей() -> None:
    """«ЖК большое путилково ул цветочная 9» (бой 13.09): пункт «большое
    путилково» (ЖК) уходит в запрос; карта с «Путилково» — тот же пункт."""
    f = address_parse.parse("ЖК большое путилково ул цветочная 9")
    assert f is not None and (f.settlement, f.settlement_type, f.street) == (
        "большое путилково",
        "ЖК",
        "ул цветочная",
    )
    assert g._пункт_назван("большое путилково", ["путилково", "красногорск"])
    f = address_parse.parse("деревня Новое Заозерье ул Рябиновая 8")
    assert f is not None and f.settlement == "Новое Заозерье"
    f = address_parse.parse("ул Ленина 5 деревня Ивановка приезжайте")
    assert f is not None and f.settlement == "Ивановка"


async def test_отказанное_место_к_дому_не_клеится(db, redis, make_avito_account, db_sessionmaker):
    """«территориально далеко» (место с отказом карты) → «ул цветочная 9»:
    дом не берёт такой пункт."""
    from app.services.inbound import apply_inbound_event
    from tests.unit.test_address_inbound_0909 import T0, событие

    account = await make_avito_account(111222999)
    await apply_inbound_event(db, redis, account, событие("СНТ Калинка"))
    async with db_sessionmaker() as s:
        место = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
        место.geo_status = "not_found"
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Лесной проезд 4", msg="am-2", when=T0.replace(minute=2))
    )
    async with db_sessionmaker() as s:
        дом = next(
            r
            for r in (await s.execute(sa.select(ClientAddressCandidate))).scalars()
            if r.kind == "house"
        )
    assert дом.settlement is None and дом.raw == "Лесной проезд 4"


def test_микрорайон_с_номером_не_дублирует_пункт() -> None:
    """Клиентка, Ангарск (бой 14.09): «мкр 13» у DaData — settlement «13» той же
    структуры, не пункт внутри; строка карты была «мкр 13, 13, Ангарск»."""
    ответ = {
        "suggestions": [
            {
                "value": "Иркутская обл, г Ангарск, мкр 13",
                "data": {
                    "fias_level": "65",
                    "region_with_type": "Иркутская обл",
                    "area_with_type": None,
                    "city": "Ангарск",
                    "settlement": "13",
                    "settlement_with_type": "мкр 13",
                    "geo_lat": "52.516867",
                    "geo_lon": "103.865863",
                },
            }
        ]
    }
    (hit,) = parse_places(ответ)
    assert (hit.kind, hit.name, hit.area, hit.settlement, hit.city) == (
        "area",
        "мкр 13",
        "мкр 13",
        None,
        "Ангарск",
    )
