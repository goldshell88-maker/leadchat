"""Полный прогон по чатам за 30 дней (стенд 15.09, 61 828 диалогов): классы
промахов, каждый воспроизведён строкой из боя и закрыт.

1. Анкета Авито («Вот подробности 👇 … Комментарий: …») — адрес читается
   только из полей клиента, не из названий работ («Сделать короб на стены 244»).
2. Станица и село со строчной буквы: «Ст. Романовская школьная 128 а»,
   «с.Красный Яр Ул.Молодежная 53», «Д новая мельница ул Нахимова д 15».
3. «Красногорск, пгт Отрадное майская 4» — пункт и улица разделяются.
4. Улица числом без «д»: «13 проезд 6А», «Тула, 9 проезд, дом 47».
5. Корпус, прилипший к дому: «18дк4»; «8линия» без пробела.
6. Слова разговора, а не адреса: «обычно днём открыта конечно, 17» → ничего.
7. Пункт без типа уступает дому в городе объявления, когда область его не
   знает («на Малинниках, ул. Заводская, 43» — район Калуги).
8. Зеленоград: «К.418» — корпус вместо улицы, по городу объявления.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.models import ClientAddressCandidate
from app.services import address_parse as ap
from app.services import app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit.test_dadata_review_1209 import _строка, ctx

pytestmark = pytest.mark.anyio

КАЛУГА = City("Калуга", "Калужская область", "Europe/Moscow")


def _разбор(text: str, **kw):  # noqa: ANN003, ANN202
    f = ap.parse(text, **kw)
    return f and (f.level, f.street, f.house, f.settlement, f.settlement_type, f.locality)


# --- 1. анкета Авито -----------------------------------------------------------

АНКЕТА = (
    "Мелкие отделочные работы, Другое · Другое · Сделать короб на...\n"
    "Вот подробности 👇\n\n"
    "Какие работы нужны:\nСделать короб на стены 244\n\n"
    "Когда нужна услуга:\nВ течение недели\n\n"
    "Где выполнить работы:\nВ квартире\n\n"
    "Где находится объект:\nВерхняя Пышма\n\n"
    "Комментарий:\nЛенина 12, кв 5\n\n"
    "Скажите, сколько это будет стоить?\n"
    "✨Задача составлена по \xa0ответам клиента в\xa0 анкете"
)


def test_анкета_адрес_только_из_полей_клиента() -> None:
    # Значение поля — до следующего «Поле:» или подписи; речь между ними
    # («Скажите, сколько это будет стоить?») — не адрес и не мешает.
    assert ap.address_text(АНКЕТА) == (
        "Верхняя Пышма\nЛенина 12, кв 5\n\nСкажите, сколько это будет стоить?"
    )
    # Город из поля «Где находится объект» — город клиента.
    assert _разбор(ap.address_text(АНКЕТА)) == ("A", "Ленина", "12", None, None, "Верхняя Пышма")
    # Без полей клиента анкета не адрес: названия работ дома не содержат
    # («стены 244» читалось домом — стенд 15.09).
    без = АНКЕТА.replace("Комментарий:\nЛенина 12, кв 5\n\n", "")
    assert ap.parse(ap.address_text(без)) is None
    assert ap.parse(без) is not None  # без фильтра анкета «давала адрес»
    # Последнее поле перед подписью анкеты не тянет подпись за собой.
    хвост = "Вот подробности 👇\n\nКомментарий:\nЛенина 12\n✨Задача составлена по ответам клиента"
    assert ap.address_text(хвост) == "Ленина 12"
    # Обычная реплика проходит как есть.
    assert ap.address_text("Ленина 12") == "Ленина 12"


# --- 2–3. станица, село, деревня, пгт ------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Ст. Романовская школьная 128 а",
            ("B", "школьная", "128 а", "Романовская", "ст.", None),
        ),
        (
            "с.Красный Яр Ул.Молодежная 53",
            ("A", "Ул Молодежная", "53", "Красный Яр", "село", None),
        ),
        (
            "Д новая мельница ул Нахимова д 15",
            ("A", "ул Нахимова", "15", "новая мельница", "деревня", None),
        ),
        (
            "Красногорск, пгт Отрадное майская 4",
            ("B", "майская", "4", "Отрадное", "посёлок", "Красногорск"),
        ),
        (
            "Договорились, поселок Дороничи улица Весенняя 3г",
            ("A", "улица Весенняя", "3г", "Дороничи", "посёлок", None),
        ),
    ],
)
def test_пункт_с_типом_и_улица(text: str, expected: tuple) -> None:
    assert _разбор(text) == expected


# --- 4–5. улица числом, корпус вплотную ----------------------------------------


@pytest.mark.parametrize(
    ("text", "street", "house"),
    [
        ("13 проезд 6А около Альтаира", "13 проезд", "6А"),
        ("Тула, 9 проезд (за рынком), дом 47", "9 проезд", "47"),
        ("Проспект Строителей 18дк4", "Проспект Строителей", "18дк4"),
        ("Ул 8линия дом 11", "Ул 8 линия", "11"),
        ("Губкин 2 фабричная 36а", "2 фабричная", "36а"),
    ],
)
def test_улица_числом_и_корпус_вплотную(text: str, street: str, house: str) -> None:
    f = ap.parse(text)
    assert f is not None and (f.street, f.house) == (street, house)


def test_солнечный_город_это_место_а_не_город() -> None:
    f = ap.parse("Солнечный город 27 рядом с Тополево")
    assert f is not None and (f.level, f.street, f.house) == ("C", "Солнечный город", "27")


# --- 6. разговор, а не адрес ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Как подъедете позвоните пожалуйста, домофона нет, обычно днём открыта конечно, 17",
        "Хорошо, ждите, 15 минут",
        "Позвоните за 30 минут, встречу, 2 подъезд",
    ],
)
def test_слова_разговора_не_адрес(text: str) -> None:
    assert ap.parse(text) is None


# --- 7. пункт без типа уступает городу объявления ------------------------------


def _заводская(city: str, lat: float, *, settlement: str | None = None) -> g.GeoHit:
    return g.GeoHit(
        street="ул Заводская",
        house="43",
        settlement=settlement,
        city=city,
        region="Калужская обл",
        lat=lat,
        lon=36.2,
        house_level=True,
    )


def test_разбор_голое_имя_перед_запятой_это_пункт_без_типа() -> None:
    assert _разбор("Малинники, ул. Заводская, 43") == (
        "A",
        "ул. Заводская",
        "43",
        "Малинники",
        None,
        None,
    )


def test_область_не_знает_пункт_без_типа_дом_в_городе_годится() -> None:
    p = g.Parsed(street="ул заводская", house="43", settlement="Малинники")
    в_городе = [_заводская("Калуга", 54.5)]
    assert g.verdict(p, КАЛУГА, в_городе)[0] == g.GEO_SETTLEMENT_MISMATCH
    assert not g.settlement_seen("Малинники", в_городе)
    assert g.settlement_seen("Малинники", [_заводская("Калуга", 54.5, settlement="Малинники")])
    assert worker._пункт_уступает_городу(p, в_городе)
    # С типом пункт не уступает; названный областью — тоже.
    assert not worker._пункт_уступает_городу(
        g.Parsed(
            street="ул заводская", house="43", settlement="Малинники", settlement_type="деревня"
        ),
        в_городе,
    )
    assert not worker._пункт_уступает_городу(p, [_заводская("Малинники", 54.6)])


async def test_воркер_пункт_без_типа_уступает_дому_в_калуге(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(
        seed_conversation,
        db_sessionmaker,
        "В городе, на Малинниках, ул. Заводская, 43(частный дом). Сколько будет стоить выезд?",
        "kaluga",
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.settlement = "Малинники"  # так строку разобрал старый разбор
        await s.commit()

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        # И в городе, и по области — один и тот же дом в Калуге: Малинников
        # как пункта карта не знает.
        return [_заводская("Калуга", 54.5)]

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert (row.geo_provider, row.geo_formatted) == ("dadata", "ул Заводская, 43, Калуга")
        assert not row.geo_variants


async def test_воркер_пункт_с_типом_не_уступает(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(
        seed_conversation, db_sessionmaker, "деревня Малинники, ул. Заводская, 43", "kaluga"
    )

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [_заводская("Калуга", 54.5)]

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) != g.GEO_EXACT


# --- 8. Зеленоград: корпус вместо улицы ----------------------------------------


def test_зеленоград_корпус_по_городу() -> None:
    f = ap.parse_by_city("К.418, подъезд 2", "Зеленоград")
    assert f is not None and (f.street, f.house, f.level) == ("корпус", "418", "B")
    assert ap.parse_by_city("К.418", "Калуга") is None


# --- 9. дом одной семьи: «31» и «31 А» — одно место --------------------------


def _строка_дома(street: str, house: str, *, exact: bool = True, ts: float = 0.0):  # noqa: ANN202
    # Координаты при exact — инвариант `exact ⇒ lat/lon` (контракт 18.09 п.3):
    # без них у строки нет степени, и `_по_местам` считал бы её не годной.
    # Точка — своя у каждого дома (шаг больше `_ОДИН_ДОМ_ГРАДУСОВ`): одна на
    # всех сделала бы «31 А» и «31 Б» одним домом по `_рядом`.
    сдвиг = sum(ord(ch) for ch in f"{street}{house}") * 0.001
    return ClientAddressCandidate(
        kind=ap.KIND_HOUSE,
        street=street,
        house=house,
        value=f"{street}, {house}",
        level="A",
        geo_status=g.GEO_EXACT if exact else g.GEO_NOT_FOUND,
        geo_formatted=f"{street}, {house}, Орск" if exact else None,
        geo_lat=51.2 + сдвиг if exact else None,
        geo_lon=58.5 if exact else None,
        detected_at=datetime.fromtimestamp(1_700_000_000 + ts, tz=UTC),
        status="pending",
    )


@pytest.mark.parametrize(
    ("a", "b", "семья"),
    [
        (("Лазурная", "31"), ("ул Лазурная", "31 А"), True),
        (("Ленина", "3"), ("Ленина", "3/1"), True),
        (("Ленина", "2"), ("Ленина", "2 стр 2"), True),
        (("Ленина", "31 А"), ("Ленина", "31 Б"), False),
        (("Ленина", "31"), ("Ленина", "310"), False),
        (("Ленина", "31"), ("Мира", "31 А"), False),
        (("ул Ленина", "31"), ("проспект Ленина", "31 А"), False),
    ],
)
def test_семья_дома(a: tuple[str, str], b: tuple[str, str], семья: bool) -> None:
    from app.services.clients import одно_место

    assert одно_место(_строка_дома(*a), _строка_дома(*b)) is семья
    assert одно_место(_строка_дома(*b), _строка_дома(*a)) is семья


def test_по_местам_оставляет_точный_по_дому_и_ранний() -> None:
    голый = _строка_дома("Лазурная", "31", ts=0)
    с_литерой = _строка_дома("Лазурная", "31 А", ts=10)
    assert worker._по_местам([голый, с_литерой]) == [с_литерой]
    assert worker._по_местам([с_литерой, голый]) == [с_литерой]
    # Не подтверждённый картой дом с литерой уступает подтверждённому голому.
    assert worker._по_местам([голый, _строка_дома("Лазурная", "31 А", exact=False)]) == [голый]


def test_без_дублей_две_строки_семьи_это_одно_предложение() -> None:
    from app.services.clients import _без_дублей

    голый = _строка_дома("Лазурная", "31", ts=0)
    с_литерой = _строка_дома("Лазурная", "31 А", ts=10)
    итог = _без_дублей([голый, с_литерой], None)
    assert [r.house for r, _ in итог] == ["31 А"]
    # Записан голый номер — уточнение с литерой показывается; записана литера —
    # голый номер не показывается (записанное точнее).
    assert [r.house for r, _ in _без_дублей([с_литерой], голый)] == ["31 А"]
    assert _без_дублей([голый], с_литерой) == []


async def test_автозапись_две_строки_одной_семьи_пишет_точную(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from app.models import Client, Conversation
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        ids = []
        for текст, formatted in (
            ("Лазурная 31", "ул Лазурная, 31, Орск"),
            ("ул Лазурная 31 а, кв 5", "ул Лазурная, 31а, Орск"),
        ):
            found = ap.parse(текст, про_адрес=True)
            assert found is not None
            записано = await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=conv.id,
                message_id=None,
                message_at=None,
                found=found,
                now=datetime.now(UTC),
            )
            row = await s.get(ClientAddressCandidate, записано.candidate_id)
            row.geo_status, row.geo_formatted = g.GEO_EXACT, formatted
            row.geo_lat, row.geo_lon = 51.2, 58.5  # инвариант exact ⇒ lat/lon
            ids.append(row.id)
        await s.commit()
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == ids[1]
        assert card.address == "ул Лазурная, 31а, Орск, кв 5"


# --- 10. у объявления города нет — город из другого диалога клиента ----------


async def test_город_из_другого_диалога_клиента(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from datetime import timedelta

    from app.models import Conversation

    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка(seed_conversation, db_sessionmaker, "По адресу Чапаева 46а", "")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug, conv.item_url = None, None
        # Другой диалог того же клиента — с городом объявления.
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="chat-other",
                account_id=conv.account_id,
                client_id=conv.client_id,
                status="closed",
                unread_count=0,
                last_message_at=datetime.now(UTC) - timedelta(days=3),
                item_city_slug="orsk",
            )
        )
        await s.commit()
    запросы: list[g.Query] = []

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        запросы.append(query)
        return [
            g.GeoHit(
                street="ул Чапаева",
                house="46а",
                settlement=None,
                city="Орск",
                region="Оренбургская область",
                lat=51.2,
                lon=58.5,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", osm)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert запросы and запросы[0].city == "Орск"


async def test_без_города_вовсе_по_прежнему_no_city(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from app.models import Conversation

    cid = await _строка(seed_conversation, db_sessionmaker, "По адресу Чапаева 46а", "")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug, conv.item_url = None, None
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY


# --- 11. корпус за цепочкой частей; «находимся» — не улица -------------------


@pytest.mark.parametrize(
    ("text", "house", "parts"),
    [
        ("3 Кедровая 7, кв 41, 1 корпус, 9 этаж", "7 корпус 1", {"office": "41", "floor": "9"}),
        ("Ленина 5, подъезд 2, корпус 3", "5 корпус 3", {"entrance": "2"}),
        ("Ленина 5, 1 подъезд, 3 корп.", "5 корпус 3", {"entrance": "1"}),
        # Корпус уже в доме — второй не дописывается; «3 корпуса мебели» — не корпус.
        ("Ленина 5 к 2, кв 41, 1 корпус", "5 к 2", {"office": "41"}),
        ("Ленина 5, кв 12, 3 корпуса мебели", "5", {"office": "12"}),
        ("Ленина 5, 1 подъезд, 3 этаж", "5", {"entrance": "1", "floor": "3"}),
    ],
)
def test_корпус_за_частями(text: str, house: str, parts: dict[str, str]) -> None:
    f = ap.parse(text)
    assert f is not None and (f.house, f.parts) == (house, parts)
    assert ap.quote_holds(f, text)


def test_корпус_за_частями_не_подменяется_в_чужом_тексте() -> None:
    f = ap.parse("Ленина 5, кв 41, 1 корпус")
    assert f is not None and f.house == "5 корпус 1"
    assert not ap.quote_holds(f, "Ленина 5, кв 41, 2 корпус")


def test_находимся_не_часть_улицы() -> None:
    f = ap.parse("Мы находимся Олимпийский проспект, 31с3")
    assert f is not None and (f.street, f.house) == ("Олимпийский проспект", "31с3")


def test_семья_не_транзитивна_два_корпуса_остаются_двумя() -> None:
    from app.services.clients import _без_дублей

    голый = _строка_дома("Ленина", "31", ts=0)
    к1 = _строка_дома("Ленина", "31 к 1", ts=10)
    к2 = _строка_дома("Ленина", "31 к 2", ts=20)
    for порядок in ([голый, к1, к2], [к1, к2, голый], [к2, голый, к1]):
        дома = sorted(r.house for r, _ in _без_дублей(порядок, None))
        assert дома == ["31 к 1", "31 к 2"], порядок
        assert sorted(r.house for r in worker._по_местам(порядок)) == ["31 к 1", "31 к 2"]


def test_семья_только_если_карта_не_развела() -> None:
    from app.services.clients import одно_место

    a = _строка_дома("Ленина", "5")
    b = _строка_дома("Ленина", "5 А")
    assert одно_место(a, b)
    a.geo_formatted = "улица Ленина, 5, Орск"
    b.geo_formatted = "проспект Ленина, 5А, Орск"
    assert not одно_место(a, b)
    # Тип улицы назвала одна карта — не спор.
    b.geo_formatted = "Ленина, 5А, Орск"
    assert одно_место(a, b)
    b.geo_formatted = "ул Ленина, 5А, Москва"
    assert not одно_место(a, b)
    b.geo_formatted = "улица Ленина, 5А, Орск"
    assert одно_место(a, b)


async def test_запись_берёт_части_всей_семьи(seed_conversation, db_sessionmaker, redis):
    from app.models import Client, Conversation
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        ids = []
        for текст, formatted in (
            ("Лазурная 31, кв 5, подъезд 2", "ул Лазурная, 31, Орск"),
            ("ул Лазурная 31 а", "ул Лазурная, 31а, Орск"),
        ):
            found = ap.parse(текст, про_адрес=True)
            assert found is not None
            записано = await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=conv.id,
                message_id=None,
                message_at=None,
                found=found,
                now=datetime.now(UTC),
            )
            row = await s.get(ClientAddressCandidate, записано.candidate_id)
            row.geo_status, row.geo_formatted = g.GEO_EXACT, formatted
            row.geo_lat, row.geo_lon = 51.2, 58.5  # инвариант exact ⇒ lat/lon
            ids.append(row.id)
        await s.commit()
    conv_id = seed_conversation.conversation_id
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), conv_id) == "filled"
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == ids[1]
        # Части голого «31» — в записи «31 а»: показ их сливает, запись обязана
        # совпасть с показом (ревью 15.09).
        assert card.address == "ул Лазурная, 31а, Орск, кв 5, подъезд 2"


# --- 12. догон по прожитой переписке: строки «из будущего» не видны ------------

T0 = datetime(2026, 9, 14, 7, 0, 0, tzinfo=UTC)
#: Окно догона от даты стенда, а не от настоящей: `days=7` от сегодняшнего
#: числа с 22.09 уже не накрывало реплики 14–15.09, и тесты догона краснели
#: сами собой (замечено 24.09 при подготовке к публикации).
ОКНО_ДНЕЙ = (datetime.now(UTC) - T0).days + 7


def _событие(текст: str, *, msg: str, when: datetime):  # noqa: ANN202
    from tests.unit.test_address_stend_1409 import AVITO_USER_ID, InboundEvent

    return InboundEvent(
        external_chat_id="chat-1509",
        external_message_id=msg,
        author_id=999015,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Галина",
        item_title="Настройка роутера",
        item_url="https://avito.ru/orsk/item/9",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):  # noqa: ANN001
    from tests.unit.test_address_stend_1409 import AVITO_USER_ID

    return await make_avito_account(AVITO_USER_ID)


async def _строки(db_sessionmaker) -> list[tuple[str, str, str | None, str | None]]:  # noqa: ANN001
    import sqlalchemy as sa

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )
        return [(r.value, r.kind, r.office, r.settlement) for r in rows]


async def test_догон_не_клеит_дом_и_части_к_строкам_из_будущего(
    db, redis, account, db_sessionmaker
):
    """Ревью 15.09: живой путь строк «из будущего» не видит, а догон,
    переигрывая историю, клеил «дом 9» к месту, названному позже, и «кв 3» —
    к адресу из следующей реплики."""
    from datetime import timedelta

    from app.cli import run_backfill_cards
    from app.services.inbound import apply_inbound_event

    реплики = [
        ("пос. Сосново", "m1", T0),
        ("дом 9", "m2", T0 + timedelta(minutes=1)),
        ("СНТ Ромашка", "m3", T0 + timedelta(minutes=3)),
        ("ул Ленина 5", "m4", T0 + timedelta(minutes=5)),
        ("кв 3", "m5", T0 + timedelta(minutes=6)),
        ("ул Пушкина 7", "m6", T0 + timedelta(minutes=8)),
    ]
    for текст, msg, when in реплики:
        await apply_inbound_event(db, redis, account, _событие(текст, msg=msg, when=when))
    до = await _строки(db_sessionmaker)
    assert ("посёлок Сосново, 9", "house", None, "Сосново") in до
    # Место «СНТ Ромашка» из прошлой реплики — к дому (правило 13.09).
    assert ("ул Ленина, 5", "house", "3", "Ромашка") in до
    assert ("ул Пушкина, 7", "house", None, None) in до
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=ОКНО_ДНЕЙ, dry_run=False)
    после = await _строки(db_sessionmaker)
    assert после == до, (до, после)


async def test_сухой_прогон_догона_считает_как_живой_и_не_пишет(
    db, redis, account, db_sessionmaker
):
    from datetime import timedelta

    import sqlalchemy as sa

    from app.cli import run_backfill_cards
    from app.services.inbound import apply_inbound_event

    for текст, msg, when in [
        ("ул Ленина 5", "m1", T0),
        ("кв 3", "m2", T0 + timedelta(minutes=1)),
        ("пос. Сосново", "m3", T0 + timedelta(minutes=2)),
        ("дом 9", "m4", T0 + timedelta(minutes=3)),
    ]:
        await apply_inbound_event(db, redis, account, _событие(текст, msg=msg, when=when))
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(ClientAddressCandidate))
        await s.commit()
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=ОКНО_ДНЕЙ, dry_run=True)
    assert await _строки(db_sessionmaker) == []
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=ОКНО_ДНЕЙ, dry_run=False)
    строки = await _строки(db_sessionmaker)
    # «пос. Сосново» после дома без вердикта (`pending`) дописывается в него
    # (N13 1b, 19.09: пункт с типом как адрес → строка дома с
    # `LATE_PLACE_STATUSES`); до N13 строка оставалась без пункта.
    assert ("ул Ленина, 5", "house", "3", "Сосново") in строки
    # «дом 9» после «пос. Сосново» — к улице, названной раньше (правило 13.09).
    assert ("ул Ленина, 9", "house", None, "Сосново") in строки


async def test_reparse_отклоняет_строку_из_анкеты_без_адресных_полей(
    seed_conversation, db_sessionmaker
):
    from app.cli import run_address_reparse
    from tests.unit.test_address_stend_1409 import _строка_из_реплики

    анкета = АНКЕТА.replace("Комментарий:\nЛенина 12, кв 5\n\n", "").replace(
        "Где находится объект:\nВерхняя Пышма\n\n", ""
    )
    cid = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        анкета,
        street="стены",
        house="244",
        level="A",
        geo_status=g.GEO_NOT_FOUND,
    )
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, cid)).status == "rejected"


# --- 13. ревью 15.09: сторожа уступки, слага, отказа, «ст.», «к 2000» --------


def test_город_справочника_голым_именем_не_уступает() -> None:
    p = g.Parsed(street="ул ленина", house="12", settlement="Обнинск")
    assert not worker._пункт_уступает_городу(p, [])
    # Область назвала пункт хотя бы улицей без дома — не уступает.
    улица = g.GeoHit(
        street="ул Ленина",
        house=None,
        settlement=None,
        city="Малинники",
        region="Калужская обл",
        lat=54.6,
        lon=36.3,
        house_level=False,
    )
    p2 = g.Parsed(street="ул заводская", house="43", settlement="Малинники")
    assert not worker._пункт_уступает_городу(p2, [улица])
    assert worker._пункт_уступает_городу(p2, [])


async def test_dadata_search_складывает_ответы_всех_текстов(monkeypatch) -> None:
    from app.integrations import dadata, gateway

    monkeypatch.setattr(gateway, "known_keys", {"dadata": True})
    улица = g.GeoHit(
        street="ул Ленина",
        house=None,
        settlement=None,
        city="Обнинск",
        region="Калужская обл",
        lat=55.1,
        lon=36.6,
        house_level=False,
    )
    дом = _заводская("Калуга", 54.5)
    ответы = {"Обнинск улица Ленина 12": [улица], "улица Ленина 12": [дом]}

    async def _ask(query, текст, client, *, near=None, on_request=None):  # noqa: ANN001
        return ответы[текст]

    monkeypatch.setattr(dadata, "_ask", _ask)
    все: list[g.GeoHit] = []
    hits = await dadata.search(
        g.Query(
            region="Калужская область",
            city=None,
            settlement="Обнинск",
            street="улица Ленина",
            house="12",
        ),
        seen=все,
    )
    assert hits == [дом] and все == [улица, дом]


async def test_слаг_без_справочника_не_берёт_город_из_другого_диалога(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from datetime import timedelta

    from app.models import Conversation

    cid = await _строка(seed_conversation, db_sessionmaker, "По адресу Чапаева 46а", "")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug, conv.item_url = "berezovskiy", None
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="chat-other-2",
                account_id=conv.account_id,
                client_id=conv.client_id,
                status="closed",
                unread_count=0,
                last_message_at=datetime.now(UTC) - timedelta(days=3),
                item_city_slug="orsk",
            )
        )
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY


async def test_отказ_по_голому_номеру_держит_семью(seed_conversation, db_sessionmaker, redis):
    from app.models import Client, Conversation
    from app.models.client import CANDIDATE_REJECTED
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        for текст, formatted, статус in (
            ("Лазурная 31", "ул Лазурная, 31, Орск", CANDIDATE_REJECTED),
            ("ул Лазурная 31 а, кв 5", "ул Лазурная, 31а, Орск", "pending"),
        ):
            found = ap.parse(текст, про_адрес=True)
            assert found is not None
            записано = await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=conv.id,
                message_id=None,
                message_at=None,
                found=found,
                now=datetime.now(UTC),
            )
            row = await s.get(ClientAddressCandidate, записано.candidate_id)
            row.geo_status, row.geo_formatted, row.status = g.GEO_EXACT, formatted, статус
        await s.commit()
    conv_id = seed_conversation.conversation_id
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), conv_id) == "skip"


@pytest.mark.parametrize(
    ("text", "settlement", "settlement_type", "level"),
    [
        ("Ст. Кашпир ул. Энергетиков 9 кв. 14", "Кашпир", "ст.", "A"),
        ("жд ст. Удельная, Ленина 5", None, None, "B"),
        ("Метро ст. Автово, Ленина 5", None, None, "B"),
        ("Ст Ленина 5", None, None, "C"),
    ],
)
def test_ст_словом_клиента_станция_за_жд_не_пункт(
    text: str, settlement: str | None, settlement_type: str | None, level: str
) -> None:
    f = ap.parse(text)
    assert f is not None and (f.settlement, f.settlement_type, f.level) == (
        settlement,
        settlement_type,
        level,
    )
    assert ap.parse_place("ст Парголово") is None


@pytest.mark.parametrize("text", ["Согласен к 2000 рублей", "приеду к 1200", "к 2025 году"])
def test_предлог_к_с_числом_не_корпус_зеленограда(text: str) -> None:
    assert ap.parse_by_city(text, "Зеленоград") is None


def test_за_городом_речь_не_адрес() -> None:
    for text in ["Тула, чистка 2 кондиционеров", "Тула, диагностика 500"]:
        f = ap.parse(text)
        assert f is None or f.level == "C", text


@pytest.mark.parametrize(
    ("text", "house", "parts"),
    [
        ("Ленина 5 (второй подъезд), кв 3", "5", {"entrance": "2", "office": "3"}),
        ("Ленина 5, кв 3, 2 корпус шкафа", "5", {"office": "3"}),
        ("Ленина 5, кв 3. Корпус 2 это соседний дом", "5", {"office": "3"}),
        ("Ленина 5/2, кв 3, 1 корпус", "5/2", {"office": "3"}),
    ],
)
def test_скобки_с_частями_и_корпус_речью(text: str, house: str, parts: dict[str, str]) -> None:
    f = ap.parse(text)
    assert f is not None and (f.house, f.parts) == (house, parts)


def test_анкета_пустое_поле_и_абзацы() -> None:
    h, t = "Вот подробности 👇\n\n", "\n✨Задача составлена по ответам клиента в анкете"
    пустое = h + "Комментарий:\n\nКакие работы нужны:\nСделать короб на стены 244\n\n" + t
    assert ap.address_text(пустое) == ""
    абзацы = h + "Где находится объект:\nТула\n\nКомментарий:\nЛенина 12\n\nкв 5, подъезд 2" + t
    assert ap.address_text(абзацы) == "Тула\nЛенина 12\n\nкв 5, подъезд 2"
    речь = "Вот подробности: приезжайте на Ленина 5 кв 3"
    assert ap.address_text(речь) == речь


# --- 14. Псков (владелец 16.09): деревня строчными после «Деревня», модель
# роняет пункт, автозапись кладёт дом города --------------------------------

ПСКОВ = "Деревня подлесниково улица кленовая дом 8 89001234567 Иван частный дом"


def test_имя_деревни_строчными_после_полного_слова_типа() -> None:
    f = ap.parse(ПСКОВ)
    assert f is not None and (f.street, f.house, f.settlement, f.settlement_type) == (
        "улица кленовая",
        "8",
        "подлесниково",
        "деревня",
    )
    f = ap.parse("село садовое ул Ленина 5")
    assert f is not None and (f.settlement, f.street) == ("садовое", "ул Ленина")
    # Тип улицы в конце остаётся улицей (корпус Jivo: «калининградское шоссе»).
    f = ap.parse("Поселок шоссейное Улица калининградское шоссе 18А к2 кв7")
    assert f is not None and (f.settlement, f.street, f.house) == (
        "шоссейное",
        "Улица калининградское шоссе",
        "18А к2",
    )
    # После предлога-сокращения «с» имя строчными — не пункт.
    f = ap.parse("с мужем ул Ленина 5")
    assert f is not None and f.settlement is None


def test_модель_не_роняет_пункт_который_видят_правила() -> None:
    from app.services import address_llm as al

    без_пункта = al.Reading(
        street="улица кленовая",
        house="8",
        settlement=None,
        settlement_type=None,
        city=None,
        parts={},
        confidence="high",
        note="",
    )
    f = al.to_found(без_пункта, [ПСКОВ])
    assert f is not None and (f.settlement, f.settlement_type) == ("подлесниково", "деревня")
    assert ap.quote_holds(f, ПСКОВ)
    # Другой дом на той же улице — пункт правил не приклеивается.
    другой = al.Reading(
        street="улица кленовая",
        house="10",
        settlement=None,
        settlement_type=None,
        city=None,
        parts={},
        confidence="high",
        note="",
    )
    assert (
        al.to_found(другой, [ПСКОВ, "нет, дом 10"]) is None
        or al.to_found(другой, [ПСКОВ, "нет, дом 10"]).settlement is None
    )


async def test_автозапись_молчит_когда_для_улицы_назван_пункт(
    seed_conversation, db_sessionmaker, redis
):
    from app.models import Client, Conversation
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "pskov"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        # Строка правил — как её разобрал старый разбор (деревня в улице), и
        # строка модели без деревни, подтверждённая в Пскове.
        строка_правил = ap.Found(
            street="Деревня подлесниково улица кленовая",
            house="8",
            raw="Деревня подлесниково улица кленовая дом 8",
            start=0,
            end=41,
            level="A",
            settlement="подлесниково улица кленовая",
            settlement_type="деревня",
        )
        строка_модели = ap.parse("улица кленовая дом 8", про_адрес=True)
        assert строка_модели is not None
        for found, статус, formatted in (
            (строка_правил, g.GEO_SETTLEMENT_MISMATCH, None),
            (строка_модели, g.GEO_EXACT, "ул Кленовая, 8, Псков"),
        ):
            записано = await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=conv.id,
                message_id=None,
                message_at=None,
                found=found,
                now=datetime.now(UTC),
            )
            row = await s.get(ClientAddressCandidate, записано.candidate_id)
            row.geo_status, row.geo_formatted = статус, formatted
        await s.commit()
    conv_id = seed_conversation.conversation_id
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), conv_id) == "skip"
    async with db_sessionmaker() as s:
        assert (await s.get(Client, seed_conversation.client_id)).address is None


# --- 15. улица в названном пункте есть, дома нет — точка улицы, не чужой дом --

ГОРОД_ПСКОВ = City("Псков", "Псковская область", "Europe/Moscow")


def _кленовая(city: str, house: str | None, *, house_level: bool, lat: float) -> g.GeoHit:
    return g.GeoHit(
        street="ул Кленовая",
        house=house,
        settlement=None,
        city=city,
        region="Псковская обл",
        lat=lat,
        lon=28.3,
        house_level=house_level,
    )


def test_улица_в_пункте_без_дома_даёт_вариант_улицы() -> None:
    p = g.Parsed(
        street="улица кленовая", house="8", settlement="Подлесниково", settlement_type="деревня"
    )
    улица = _кленовая("Подлесниково", "8", house_level=False, lat=57.9)
    дом_в_пскове = _кленовая("Псков", "8", house_level=True, lat=57.78)
    assert g.street_in_settlement(p, ГОРОД_ПСКОВ, [дом_в_пскове, улица]) is улица
    assert g.street_in_settlement(p, ГОРОД_ПСКОВ, [дом_в_пскове]) is None
    вариант = g.street_variant(улица, p)
    assert вариант["formatted"] == "ул Кленовая, 8, Подлесниково" and вариант["lat"] == 57.9
    чужие = g.variants(p, ГОРОД_ПСКОВ, [дом_в_пскове])
    assert not g.variants_name_settlement("Подлесниково", чужие)
    assert g.variants_name_settlement(
        "Подлесниково", [{"formatted": "ул Кленовая, 6, Подлесниково", "city": "Подлесниково"}]
    )


async def test_воркер_дом_в_деревне_не_найден_точка_улицы_а_не_дом_в_пскове(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """С 18.09 (автопривязка, правило `street_point`) та же строка при
    включённой настройке `address_geo.auto_decide` становится `exact` с
    точкой улицы и хвостом `~approx` (стенд в `test_autobind_policy_1809.py`);
    здесь — диверсия выключателя: выключено → прежний вердикт «улица есть,
    дома нет» с вариантом улицы для оператора, как до 18.09."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    cid = await _строка(
        seed_conversation, db_sessionmaker, "Деревня Подлесниково улица Кленовая дом 8", "pskov"
    )

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        seen = kw.get("seen")
        if query.settlement:
            улица = _кленовая("Подлесниково", "8", house_level=False, lat=57.9)
            if seen is not None:
                seen.append(улица)
            if kw.get("without_settlement_too", True) is False:
                return [улица]
        дома = [_кленовая("Псков", "8", house_level=True, lat=57.78)]
        if seen is not None:
            seen.extend(дома)
        return дома

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISSING
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.geo_provider == "dadata" and row.geo_formatted is None
        assert [v["formatted"] for v in row.geo_variants] == ["ул Кленовая, 8, Подлесниково"]
        assert row.geo_variants[0]["lat"] == 57.9
    conv_id = seed_conversation.conversation_id
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), conv_id) == "skip"


# --- 16. живой путь и догон: проводка inbound/cli (ревью тестов 15.09) --------


async def test_зеленоград_корпус_заводится_на_живом_пути(db, redis, account, db_sessionmaker):
    from datetime import timedelta

    import sqlalchemy as sa

    from app.models import Conversation
    from app.services.inbound import apply_inbound_event

    await apply_inbound_event(db, redis, account, _событие("Здравствуйте", msg="z0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "moskva_zelenograd"
        await s.commit()
    await apply_inbound_event(
        db,
        redis,
        account,
        _событие("К.418, 1 под, 7 эт, кв.52", msg="z1", when=T0 + timedelta(minutes=1)),
    )
    assert await _строки(db_sessionmaker) == [("корпус, 418", "house", "52", None)]


async def test_анкета_на_живом_пути_и_в_догоне(db, redis, account, db_sessionmaker):
    import sqlalchemy as sa

    from app.cli import run_backfill_cards
    from app.services.inbound import apply_inbound_event

    без_адреса = АНКЕТА.replace("Комментарий:\nЛенина 12, кв 5\n\n", "").replace(
        "Где находится объект:\nВерхняя Пышма\n\n", ""
    )
    assert ap.parse(без_адреса) is not None  # без фильтра анкета «давала адрес»
    await apply_inbound_event(db, redis, account, _событие(без_адреса, msg="a1", when=T0))
    assert await _строки(db_sessionmaker) == []
    await apply_inbound_event(db, redis, account, _событие(АНКЕТА, msg="a2", when=T0))
    assert await _строки(db_sessionmaker) == [("Ленина, 12", "house", "5", None)]
    async with db_sessionmaker() as s:
        await s.execute(sa.delete(ClientAddressCandidate))
        await s.commit()
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=ОКНО_ДНЕЙ, dry_run=False)
    assert await _строки(db_sessionmaker) == [("Ленина, 12", "house", "5", None)]


async def test_город_из_ссылки_другого_диалога_и_самый_свежий(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from datetime import timedelta

    from app.models import Conversation

    cid = await _строка(seed_conversation, db_sessionmaker, "По адресу Чапаева 46а", "")
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug, conv.item_url = None, None
        for i, (slug, url, дней) in enumerate(
            (
                ("orsk", None, 10),
                (None, "https://www.avito.ru/kaluga/predlozheniya_uslug/remont_1234567890", 1),
                ("neizvestno", None, 0.5),
            )
        ):
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id=f"chat-other-{i}",
                    account_id=conv.account_id,
                    client_id=conv.client_id,
                    status="closed",
                    unread_count=0,
                    last_message_at=datetime.now(UTC) - timedelta(days=дней),
                    item_city_slug=slug,
                    item_url=url,
                )
            )
        await s.commit()
    запросы: list[g.Query] = []

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        запросы.append(query)
        return []

    monkeypatch.setattr(worker.nominatim, "search", osm)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    # Самый свежий диалог с городом справочника — калужский, по ссылке без слага.
    assert запросы and запросы[0].city == "Калуга"
