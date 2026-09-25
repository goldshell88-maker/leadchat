"""Стенд 14.09 (владелец: «проанализируй все диалоги, где карта не подтвердила,
где id без адреса, где не то распозналось»). Тринадцать скринов и 38 заявок с
id без адреса за трое суток — в основном строчные названия в ответ на вопрос
оператора, опечатка «подьезд» без признака адреса, тройка «мкр-дом-кв» после
слова «адрес», «15/ 3й подъезд» как корпус, «в Хабаровске улица Чкалова» как
имя перед типом, «Просто 3» с вариантами по области.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from leadchat_gateway.providers import dadata as gw_dadata

from app.models import ClientAddressCandidate, Conversation, Message
from app.services import address_parse as ap
from app.services import geocode as g
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

СТЕНД = [
    ("2 молодежная 36 1 подьезд", "A", "2 молодежная", "36", {"entrance": "1"}),
    ("Заречная 6,1 подьезд, тел.XXXXXXXXXXX", "A", "Заречная", "6", {"entrance": "1"}),
    ("Щёлково центральная 15/ 3й подъезд", "A", "центральная", "15", {"entrance": "3"}),
    ("Жукова 14/6 этаж 3 подъезд 1", "A", "Жукова", "14/6", {"floor": "3", "entrance": "1"}),
    # Части за тройкой — её части (владелец 25.09): до того подъезд и этаж
    # этой же реплики терялись — правила адрес нашли, читатель частей её не видел.
    ("14А-76-118 3 подъезд На 10:30", "C", "14а мкр", "76", {"office": "118", "entrance": "3"}),
    ("12-17-158 шестой этаж", "C", "12 мкр", "17", {"office": "158", "floor": "6"}),
    ("Адрес 3-24-71", "C", "3 мкр", "24", {"office": "71"}),
    ("Микрорайон 4 дом 11 кв76", "A", "4 Микрорайон", "11", {"office": "76"}),
    ("XXXXXXXXXXX победы 137 2 подьезд", "A", "победы", "137", {"entrance": "2"}),
    ("Кузнецова 3/1 кв 17", "A", "Кузнецова", "3/1", {"office": "17"}),
]


@pytest.mark.parametrize(
    ("текст", "уровень", "улица", "дом", "части"), СТЕНД, ids=[t for t, *_ in СТЕНД]
)
def test_стенд_1409_дом(
    текст: str, уровень: str, улица: str, дом: str, части: dict[str, str]
) -> None:
    f = ap.parse(текст)
    assert f is not None, текст
    assert (f.level, f.street, f.house, f.parts) == (уровень, улица, дом, части)


ПОСЛЕ_ВОПРОСА = [
    ("анисимова 23", "анисимова", "23"),
    ("партизанская 58", "партизанская", "58"),
    ("2 сормовская 59", "2 сормовская", "59"),
    ("Я вам только звонил юность 3", "юность", "3"),
    ("По адресу кораблестроителей 8 вызывали, развивающий центр", "кораблестроителей", "8"),
    ("Когда смогли бы заехать по адресу мира 86, ресторан yoshi ?", "мира", "86"),
    ("Напишу заранее. Адрес ново-вокзальная 26", "ново-вокзальная", "26"),
]


@pytest.mark.parametrize(
    ("текст", "улица", "дом"), ПОСЛЕ_ВОПРОСА, ids=[t for t, *_ in ПОСЛЕ_ВОПРОСА]
)
def test_строчные_после_вопроса_оператора(текст: str, улица: str, дом: str) -> None:
    f = ap.parse(текст, про_адрес=True)
    assert f is not None and (f.level, f.street, f.house) == ("B", улица, дом)


def test_город_в_предложном_падеже_не_имя_улицы() -> None:
    p = ap.parse_place("Живу в Хабаровске улица Чкалова, У вас в объявлении другой город")
    assert p is not None and p.street == "улица Чкалова"


def test_просто_3_не_адрес() -> None:
    assert ap.parse("Просто 3") is None


def test_телефон_не_тройка_мкр_дом_кв() -> None:
    assert ap.parse("8-912-345") is None
    assert ap.parse("Адрес 8-912-3456789") is None


def test_dadata_москва_и_область_ищутся_разом() -> None:
    assert gw_dadata.locations_for(region="Москва", city=None) == [
        {"region_iso_code": "RU-MOW"},
        {"region_iso_code": "RU-MOS"},
    ]


# ── контекст вопроса, когда правила молчат ────────────────────────────────────

AVITO_USER_ID = 111222777
T0 = datetime(2026, 9, 14, 7, 0, 0, tzinfo=UTC)
#: Окно догона от даты стенда, а не от настоящей: `days=7` от сегодняшнего
#: числа с 22.09 уже не накрывало реплики 14–15.09, и тесты догона краснели
#: сами собой (замечено 24.09 при подготовке к публикации).
ОКНО_ДНЕЙ = (datetime.now(UTC) - T0).days + 7


def событие(текст: str, *, msg: str, when: datetime) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-1409",
        external_message_id=msg,
        author_id=999014,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Галина",
        item_title="Настройка роутера",
        item_url="https://avito.ru/vladivostok/item/9",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


async def test_строчная_улица_в_ответ_на_вопрос_становится_строкой(
    db, redis, account, db_sessionmaker
):
    await apply_inbound_event(db, redis, account, событие("Около 18-18.30", msg="m1", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="out-1",
                direction="out",
                sender_type="user",
                body="по какому адресу и можно ваш номер телефон",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=1),
            )
        )
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("анисимова 23", msg="m2", when=T0 + timedelta(minutes=2))
    )
    async with db_sessionmaker() as s:
        rows = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
    assert [(r.level, r.value) for r in rows] == [("B", "анисимова, 23")]


# ── старые строки: reparse отклоняет речь и правит уровень, показ невода C ──


async def _строка_из_реплики(
    seed_conversation,
    db_sessionmaker,
    текст: str,
    *,
    street: str,
    house: str,
    level: str,
    geo_status: str,
):
    from app.models import Client
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        msg = Message(
            conversation_id=conv.id,
            external_message_id=f"in-{house}-{level}",
            direction="in",
            sender_type="client",
            body=текст,
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(msg)
        await s.flush()
        card = await s.get(Client, seed_conversation.client_id)
        found = ap.Found(
            street=street, house=house, raw=f"{street} {house}", start=0, end=1, level=level
        )
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=msg.id,
            message_at=msg.created_at,
            found=found,
            now=datetime.now(UTC),
        )
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
        row.geo_status = geo_status
        await s.commit()
        return row.id


async def test_reparse_отклоняет_речь_старого_разбора_и_правит_уровень(
    seed_conversation, db_sessionmaker
):
    from app.cli import run_address_reparse

    речь = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        "Здравствуйте однушку надо сделать квартиру 32 квадрата сколько будет стоить",
        street="надо сделать квартиру",
        house="32",
        level="A",
        geo_status=g.GEO_HOUSE_MISSING,
    )
    адрес = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        "Красноармейская 19",
        street="Красноармейская",
        house="19",
        level="A",
        geo_status=g.GEO_NOT_FOUND,
    )
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == "rejected"
        строка = await s.get(ClientAddressCandidate, адрес)
        assert строка.status == "pending" and строка.level == "C"


async def test_невод_C_с_вариантами_по_области_не_показывается(seed_conversation, db_sessionmaker):
    from app.models import Client
    from app.services import clients as clients_svc

    cid = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        "Просто 3",
        street="Просто",
        house="3",
        level="C",
        geo_status=g.GEO_ELSEWHERE,
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_variants = [{"formatted": "ГСК Простор, 3, Норильск", "lat": 69.3, "lon": 88.2}]
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, seed_conversation.client_id))
    assert view["address_candidates"] == []
    # А точный дом у невода — показывается, как и раньше.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_variants = g.GEO_EXACT, None
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, seed_conversation.client_id))
    assert len(view["address_candidates"]) == 1


# ── находки ревью 14.09 ───────────────────────────────────────────────────────

РЕВЬЮ = [
    ("Ленина 17/4 квартира 5", "17/4", {"office": "5"}),
    ("Гагарина 5/2 подъезда 1", "5/2", {"entrance": "1"}),
    # «17/4 подъезд» — дом 17, подъезд 4 (владелец 18.09: «дом 64/5 подъезд»);
    # ревью 14.09 читало дробь домом, но слово части без числа — про хвост.
    ("Ленина 17/4 подъезд", "17", {"entrance": "4"}),
    ("Ленина 17/ 4 подъезд", "17", {"entrance": "4"}),
    ("Ленина 5к 2 квартира 7", "5к 2", {"office": "7"}),
]


@pytest.mark.parametrize(("текст", "дом", "части"), РЕВЬЮ, ids=[t for t, *_ in РЕВЬЮ])
def test_дробь_и_корпус_перед_частью_со_своим_числом(
    текст: str, дом: str, части: dict[str, str]
) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.house, f.parts) == (дом, части)


@pytest.mark.parametrize("текст", ["10-12-14 часов", "1-2-3 дня", "20-30-40 см", "12-14-16 часов"])
def test_тройка_не_срок_и_не_размер(текст: str) -> None:
    assert ap.parse(текст) is None


@pytest.mark.parametrize(
    ("текст", "улица", "части"),
    [
        ("квартира 12 ленина 5", "ленина", {"office": "12"}),
        ("кв. 5 садовая 7", "садовая", {"office": "5"}),
        ("этаж 5 садовая 7", "садовая", {"floor": "5"}),
    ],
)
def test_число_части_перед_улицей_не_приставка(
    текст: str, улица: str, части: dict[str, str]
) -> None:
    f = ap.parse(текст, про_адрес=True)
    assert f is not None and (f.street, f.parts) == (улица, части)


@pytest.mark.parametrize(
    "текст", ["холодильник 2", "утром 9", "стиралка 3", "розетки 3", "окна 3", "смеситель 2"]
)
def test_техника_и_время_после_вопроса_не_улица(текст: str) -> None:
    assert ap.parse(текст, про_адрес=True) is None


async def test_reparse_не_отклоняет_дом_из_двух_реплик(seed_conversation, db_sessionmaker):
    """«Поселок Сосново» + «дом 9»: в реплике строки только дом — это не речь."""
    from app.cli import run_address_reparse

    cid = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        "дом 9",
        street="посёлок Сосново",
        house="9",
        level="A",
        geo_status=g.GEO_HOUSE_MISSING,
    )
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, cid)).status == "pending"


# ── адрес по частям: пункт → улица → дом (владелец 14.09) ─────────────────────


async def test_пункт_улица_дом_тремя_репликами(db, redis, account, db_sessionmaker):
    t = T0
    for i, текст in enumerate(["Поселок Сосново", "ул Лесная", "дом 5"]):
        await apply_inbound_event(
            db, redis, account, событие(текст, msg=f"p{i}", when=t + timedelta(minutes=i))
        )
    async with db_sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )
    виды = [(r.kind, r.street, r.house, r.settlement) for r in rows]
    # Улица без дома унаследовала пункт, дом — улицу и пункт.
    assert ("place", "ул Лесная", "", "Сосново") in виды
    assert ("house", "ул Лесная", "5", "Сосново") in виды


async def test_место_улица_прячется_за_домом_той_же_улицы(seed_conversation, db_sessionmaker):
    """«Зареченский район. Ул. Пушкина» → «Ул. Пушкина, д. 47» (Тула):
    два предложения одной улицы в карточке — не два адреса."""
    from app.models import Client
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "tula"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        for текст, место in (
            ("Зареченский район. Ул. Пушкина", True),
            ("Ул. Пушкина, д. 47", False),
        ):
            found = ap.parse_place(текст) if место else ap.parse(текст)
            assert found is not None, текст
            await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=conv.id,
                message_id=seed_conversation.message_id,
                message_at=datetime.now(UTC),
                found=found,
                now=datetime.now(UTC),
            )
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, seed_conversation.client_id))
    assert [c["kind"] for c in view["address_candidates"]] == ["house"]


# ── «86-11» в Ангарске, вопрос среди трёх исходящих (владелец 14.09) ──────────


@pytest.mark.parametrize(
    ("текст", "улица", "дом", "части"),
    [
        ("Через час 86-11", "86 квартал", "11", {}),
        ("Через час 86-11, 1 подъезд, кв 5", "86 квартал", "11", {"entrance": "1", "office": "5"}),
        ("14а-76", "14а квартал", "76", {}),
        ("Ангарск 86-11", "86 квартал", "11", {}),
        ("Ангарск, 86-11, кв 5", "86 квартал", "11", {"office": "5"}),
        ("89501234567, 86-11, кв 5", "86 квартал", "11", {"office": "5"}),
        ("8-950-123-45-67, 86-11", "86 квартал", "11", {}),
        # Пара «как время» — только с частями или словом «адрес» рядом.
        ("Адрес: 9-13, кв 5", "9 квартал", "13", {"office": "5"}),
        ("мой адрес 22-14", "22 квартал", "14", {}),
    ],
)
def test_пара_квартал_дом(текст: str, улица: str, дом: str, части: dict[str, str]) -> None:
    f = ap.parse_quarter_pair(текст, "квартал")
    assert f is not None and (f.street, f.house, f.parts, f.level) == (улица, дом, части, "B")
    assert ap.quote_holds(f, текст)


@pytest.mark.parametrize(
    "текст",
    [
        # время и окна визита (ревью 14.09)
        "с 9-13",
        "в 10-12",
        "в 9-30",
        "к 18-30",
        "приезжайте к 10-30",
        "с 8-30 до 17-30",
        "Могу завтра 10-12",
        "Завтра 10-12 подойдет?",
        "Сегодня 17-19",
        "около 18-19",
        "после 16-30",
        "работаю 9-18",
        "Могу с 10-12 или 15-17",
        "10-12 или 14-16",
        "10-12",
        # сроки, даты, цены, размеры, модели, части
        "2-3 дня",
        "Через 30-40 минут",
        "14-15 сентября",
        "Мне 65-70 лет",
        "Мигает 2-3 раза",
        "500-700 рублей",
        "цена 300-500",
        "1500-2000 руб",
        "55-65 дюймов",
        "телевизор 55-65",
        "Диагональ 40-43",
        "LG 43-55",
        "модель 40-13",
        "ошибка 4-13",
        "этаж 4-5",
        "подъезд 2-3",
        "12.5-13",
        "XXXXXXXXXXX",
        # ревью 14.09, второй круг
        "могу 19-15",
        "завтра 18-10",
        "Давайте 12-12",
        "Давайте 25-26 числа",
        "Давайте 25-26го",
        "Можно 25-26 го",
        "на 30-40 секунд",
        "25-30 дней",
        "60-70 процентов",
        "полоса 30-40 сантиметров",
        "40-50 тыщ",
        "За 300-500 сделаете?",
        "за 500-700",
        "Рублей 500-700",
        "Диагональ где-то 40-43",
        "Телевизор диагональ примерно 40-43",
        "Самсунг 40-43",
        "матрица 40-43",
        "греется до 60-70",
        "Минут 30-40",
        "Скидка 30-40",
        "Мне 65-70",
        "через 30-40",
        # третий круг: дни месяца и «примерно столько»
        "Можно 28-29?",
        "Давайте 27-28",
        "27-28 подойдет?",
        "Звук на 30-40 еле слышно",
        "Мигает 30-40 раз",
        "Нужно 30-40",
        "30-40",
    ],
)
def test_пара_не_время_не_цена_не_размер(текст: str) -> None:
    assert ap.parse_quarter_pair(текст, "квартал") is None


def test_пара_как_время_всей_репликой_только_после_вопроса() -> None:
    assert ap.parse_quarter_pair("10-12", "квартал") is None
    f = ap.parse_quarter_pair("10-12", "квартал", про_адрес=True)
    assert f is not None and (f.street, f.house) == ("10 квартал", "12")
    # …но не в речи: «Могу завтра 10-12» и после вопроса — время.
    assert ap.parse_quarter_pair("Могу завтра 10-12", "квартал", про_адрес=True) is None


async def test_пара_квартал_дом_только_в_квартальном_городе(db, redis, account, db_sessionmaker):
    """Объявление во Владивостоке — «86-11» ничем не считается; в Ангарске — адрес."""
    await apply_inbound_event(db, redis, account, событие("Через час 86-11", msg="q1", when=T0))
    async with db_sessionmaker() as s:
        assert (await s.execute(sa.select(ClientAddressCandidate))).scalars().all() == []
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "angarsk"
        await s.commit()
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Через час 86-11, 1 подъезд", msg="q2", when=T0 + timedelta(minutes=1)),
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert [(r.level, r.value, r.entrance) for r in rows] == [("B", "86 квартал, 11", "1")]


async def test_вопрос_об_адресе_среди_трёх_последних_исходящих(db, redis, account, db_sessionmaker):
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="v1", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        for i, текст in enumerate(
            ["по адресу подскажите полному, дом, кв, подъезд", "могу к 15-15:30 подъехать сегодня"]
        ):
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"out-v{i}",
                    direction="out",
                    sender_type="user",
                    body=текст,
                    attachments=[],
                    delivery_status="delivered",
                    created_at=T0 + timedelta(minutes=1 + i),
                )
            )
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Промышленная 12/3", msg="v2", when=T0 + timedelta(minutes=4))
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert [(r.level, r.value) for r in rows] == [("B", "Промышленная, 12/3")]


# ── «Ангарск 11 микрорайон дом квартира 92» (владелец 14.09, клиентка) ───────


@pytest.mark.parametrize(
    "текст",
    [
        "Ангарск 11 микрорайон дом квартира 92 , телефон XXXXXXXXXXX.",
        "Ангарск 11 микрорайон",
        "Ангарск 11-й микрорайон",
    ],
)
def test_число_перед_микрорайоном_не_дом(текст: str) -> None:
    """Раньше: улица «Ангарск», дом 11. Число перед типом — номер микрорайона."""
    assert ap.parse(текст) is None
    p = ap.parse_place(текст)
    assert p is not None and (p.kind, p.area, p.locality) == (
        ap.KIND_PLACE,
        "микрорайон 11",
        "Ангарск",
    )
    assert ap.quote_holds(p, текст)


def test_квартира_едет_с_местом() -> None:
    p = ap.parse_place("Ангарск 11 микрорайон дом квартира 92 , телефон XXXXXXXXXXX.")
    assert p is not None and p.parts == {"office": "92"}


def test_микрорайон_с_домом_по_прежнему_адрес() -> None:
    f = ap.parse("Ангарск 11 микрорайон дом 5 квартира 92")
    assert f is not None and (f.street, f.house, f.parts, f.locality) == (
        "11 микрорайон",
        "5",
        {"office": "92"},
        "Ангарск",
    )
    f = ap.parse("15 мкр 15")
    assert f is not None and (f.street, f.house) == ("15 мкр", "15")


async def test_место_с_квартирой_потом_дом(db, redis, account, db_sessionmaker):
    """«Ангарск 11 микрорайон дом квартира 92» → «дом 5»: дом к микрорайону, квартира с места."""
    await apply_inbound_event(
        db, redis, account, событие("Ангарск 11 микрорайон дом квартира 92", msg="t1", when=T0)
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert [(r.kind, r.value, r.office, r.locality) for r in rows] == [
            (ap.KIND_PLACE, "микрорайон 11", "92", "Ангарск")
        ]
    await apply_inbound_event(
        db, redis, account, событие("дом 5", msg="t2", when=T0 + timedelta(minutes=2))
    )
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
    assert [(r.kind, r.value, r.office) for r in rows][-1] == (
        ap.KIND_HOUSE,
        "11 микрорайон, 5",
        "92",
    )


# ── ревью 14.09: город-прилагательное, закрытый вопрос, «Имя мкр N» ──────────


@pytest.mark.parametrize(
    ("текст", "улица", "дом"),
    [
        ("Московский проспект 5", "Московский проспект", "5"),
        ("Московский пр. 5 кв 3", "Московский пр.", "5"),
        ("Свердловский проспект 5", "Свердловский проспект", "5"),
        ("Октябрьский проспект 12 кв 3", "Октябрьский проспект", "12"),
        ("Московский мкр 3", "Московский мкр", "3"),
        ("Октябрьский мкр 15", "Октябрьский мкр", "15"),
        # проспект без типа, с частями — как писали и раньше
        ("Октябрьский 5 кв 3", "Октябрьский", "5"),
        ("Московский 220 кв 15", "Московский", "220"),
        ("Свердловский 12 подъезд 2", "Свердловский", "12"),
    ],
)
def test_город_прилагательное_перед_типом_улицы_это_улица(текст: str, улица: str, дом: str) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.house, f.locality) == (улица, дом, None)


@pytest.mark.parametrize(
    ("текст", "улица", "город"),
    [
        ("Бердск ул Ленина 5", "ул Ленина", "Бердск"),
        ("Геленджик ул. Новая 4", "ул. Новая", "Геленджик"),
        ("Жуковский ул Гагарина 7 кв 3", "ул Гагарина", "Жуковский"),
        ("Мурино бульвар Менделеева 5", "бульвар Менделеева", "Мурино"),
        ("Тула проезд Ленина 5", "проезд Ленина", "Тула"),
        ("Нижний Тагил ул Мира 3", "ул Мира", "Нижний Тагил"),
    ],
)
def test_город_перед_типом_с_именем_улицы_остаётся_городом(
    текст: str, улица: str, город: str
) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.locality or f.settlement) == (улица, город)


def test_московский_проспект_местом_без_города() -> None:
    p = ap.parse_place("Московский проспект")
    assert p is not None and (p.street, p.locality) == ("Московский проспект", None)


@pytest.mark.parametrize("текст", ["Ангарск 86-11, кв 5", "Бердск 23 кв 6"])
def test_город_на_месте_улицы_не_адрес(текст: str) -> None:
    assert ap.parse(текст) is None


def test_октябрьский_5_остаётся_улицей_уровня_c() -> None:
    f = ap.parse("Октябрьский 5")
    assert f is not None and (f.level, f.street, f.house) == ("C", "Октябрьский", "5")


@pytest.mark.parametrize(
    "текст", ["Здравствуйте. Ангарск 11 микрорайон", "Здравствуйте, Ангарск, 11 микрорайон"]
)
def test_цитата_места_без_знака_перед_городом(текст: str) -> None:
    p = ap.parse_place(текст)
    assert p is not None and p.raw.startswith("Ангарск") and p.locality == "Ангарск"


@pytest.mark.parametrize(
    ("текст", "улица", "дом"),
    [
        ("Ленина 5 мкр Заря", "Ленина", "5"),
        ("Ленина 5 мкр. Заря", "Ленина", "5"),
        ("ленина 5 мкр северный кв 3", "ленина", "5"),
        ("Ленина д 5 мкр южный", "Ленина", "5"),
        ("Октябрьский мкр 35 дом 3", "35 мкр", "3"),
        ("Волжский мкр 27 д 5 кв 40", "27 мкр", "5"),
        ("Юбилейный мкр 5 дом 3", "5 мкр", "3"),
        ("Ленина 5 мкр-н Северный", "Ленина", "5"),
        ("Ленина 5 мкр.Северный", "Ленина", "5"),
        ("ул Ленина д 5 мкр Заря", "ул Ленина", "5"),
        ("ул Гагарина 7 мкр 3", "ул Гагарина", "7"),
        ("Юбилейный мкр 3", "Юбилейный мкр", "3"),
        ("Северный мкр 7 подъезд 2", "Северный мкр", "7"),
    ],
)
def test_тип_массива_после_дома_не_отнимает_дом(текст: str, улица: str, дом: str) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.house) == (улица, дом)


@pytest.mark.parametrize(
    "текст", ["Анна 11 микрорайон дом квартира 92", "Мегет 11 микрорайон дом квартира 92"]
)
def test_имя_не_из_списка_городов_перед_микрорайоном_не_улица(текст: str) -> None:
    assert ap.parse(текст) is None
    p = ap.parse_place(текст)
    assert p is not None and p.area == "микрорайон 11"


@pytest.mark.parametrize("текст", ["Здравствуйте мкр 15", "Живу мкр 15", "мкр 15"])
def test_речь_перед_мкр_не_имя(текст: str) -> None:
    assert ap.parse(текст) is None
    p = ap.parse_place(текст)
    assert p is not None and p.area == "мкр 15"


async def test_вопрос_об_адресе_закрыт_ответом(db, redis, account, db_sessionmaker):
    """«адрес?» → «Ленина 5» → «во сколько удобно?» → «приезжайте 16»: речь, не адрес."""
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="z0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(исходящее(conv.id, "Подскажите адрес", "out-z1", T0 + timedelta(minutes=1)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Ленина 5", msg="z1", when=T0 + timedelta(minutes=2))
    )
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(исходящее(conv.id, "Принято, спасибо", "out-z2", T0 + timedelta(minutes=3)))
        s.add(исходящее(conv.id, "Во сколько вам удобно?", "out-z3", T0 + timedelta(minutes=4)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("приезжайте 16", msg="z2", when=T0 + timedelta(minutes=5))
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert [(r.level, r.value) for r in rows] == [("B", "Ленина, 5")]


def исходящее(conversation_id, текст: str, ext: str, когда) -> Message:
    return Message(
        conversation_id=conversation_id,
        external_message_id=ext,
        direction="out",
        sender_type="user",
        body=текст,
        attachments=[],
        delivery_status="delivered",
        created_at=когда,
    )


async def test_вопрос_последней_репликой_открыт_и_после_ответа(db, redis, account, db_sessionmaker):
    """«адрес?» → «ленина 5» → «ой, не ленина, а пушкина 5»: поправка — тоже ответ."""
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="p0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(исходящее(conv.id, "Подскажите адрес", "out-p1", T0 + timedelta(minutes=1)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("ленина 5", msg="p1", when=T0 + timedelta(minutes=2))
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("ой, не ленина, а пушкина 5", msg="p2", when=T0 + timedelta(minutes=3)),
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert sorted((r.level, r.value) for r in rows) == [("B", "ленина, 5"), ("B", "пушкина, 5")]


async def test_вопрос_три_реплики_назад_закрыт_повтором_адреса(db, redis, account, db_sessionmaker):
    """«Ленина 5» → «адрес ещё раз?» → «Ленина 5» (повтор, строки нет) →
    «во сколько удобно?» → «приезжайте 16»: вопрос закрыт репликой клиента."""
    await apply_inbound_event(db, redis, account, событие("Ленина 5", msg="r0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(исходящее(conv.id, "Подскажите адрес ещё раз", "out-r1", T0 + timedelta(minutes=1)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Ленина 5", msg="r1", when=T0 + timedelta(minutes=2))
    )
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(исходящее(conv.id, "Во сколько вам удобно?", "out-r2", T0 + timedelta(minutes=3)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("приезжайте 16", msg="r2", when=T0 + timedelta(minutes=4))
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert [(r.level, r.value) for r in rows] == [("B", "Ленина, 5")]


async def test_город_перед_парой_на_живом_пути(db, redis, account, db_sessionmaker):
    """«Ангарск 86-11»: невод даёт строку C «Ангарск, 86», пара точнее."""
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="g0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "angarsk"
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Ангарск 86-11", msg="g1", when=T0 + timedelta(minutes=1))
    )
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert [(r.level, r.value) for r in rows] == [("B", "86 квартал, 11")]


async def test_догон_не_отклоняет_квартальную_пару(db, redis, account, db_sessionmaker):
    """«Через час 86-11» в Ангарске с отказом карты — адрес, а не речь (третий круг)."""
    from app.cli import run_address_reparse

    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="d0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "angarsk"
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("Через час 86-11", msg="d1", when=T0 + timedelta(minutes=1))
    )
    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
        assert row.value == "86 квартал, 11"
        row.geo_status = "not_found"
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
        await s.commit()
    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
    assert row.status == "pending"


# ── «С. Июньское Воткинский район. Советская 64» (владелец 14.09, Ижевск) ────


@pytest.mark.parametrize(
    "текст",
    [
        "С. Июньское Воткинский район. Советская 64",
        "с. Июньское Воткинский район. Советская 64",
        "С. Июньское, Воткинский район, Советская 64",
    ],
)
def test_район_между_пунктом_и_улицей(текст: str) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.house, f.settlement, f.settlement_type, f.district) == (
        "Советская",
        "64",
        "Июньское",
        "село",
        "Воткинский район",
    )
    assert ap.quote_holds(f, текст)


def test_пункт_с_районом_местом() -> None:
    p = ap.parse_place("С. Июньское Воткинский район")
    assert p is not None and (p.settlement, p.settlement_type, p.district) == (
        "Июньское",
        "село",
        "Воткинский район",
    )


@pytest.mark.parametrize(
    ("текст", "улица", "район"),
    [
        ("Ленинский район Муравьева 14", "Муравьева", "Ленинский район"),
        ("Октябрьский район, ул Ленина 5", "ул Ленина", "Октябрьский район"),
        ("Кировский район Ленина 5 кв 3", "Ленина", "Кировский район"),
    ],
)
def test_район_перед_улицей_в_строку(текст: str, улица: str, район: str) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.district) == (улица, район)


def test_с_точкой_и_заглавной_без_типа_улицы_пункт() -> None:
    f = ap.parse("с мужем, ул Ленина 5")
    assert f is not None and f.settlement is None


# ── четыре скриншота владельца 14.09 (день) ───────────────────────────────────


def test_город_словом_и_массив_с_домом() -> None:
    """Иван, Ивантеевка: «Город Щёлково территория комплекса дубково 14 корпус 3»."""
    f = ap.parse("Город Щёлково территория комплекса дубково 14 корпус 3")
    assert f is not None and (f.level, f.street, f.house, f.locality) == (
        "B",
        "дубково",
        "14 корпус 3",
        "Щёлково",
    )
    assert ap.quote_holds(f, "Город Щёлково территория комплекса дубково 14 корпус 3")
    f = ap.parse("г Гай ленина 5")
    assert f is not None and (f.street, f.locality) == ("ленина", "Гай")


@pytest.mark.parametrize(
    "текст",
    ["У нас магазин одежды. Ла Мур 1 этаж отдельный вход украшен сакурой", "Ашан 2 этаж"],
)
def test_число_перед_этажом_без_своего_числа_не_дом(текст: str) -> None:
    """Алина, Новосибирск: «Ла Мур 1 этаж» — этаж магазина, не дом «Мур, 1»."""
    assert ap.parse(текст) is None
    assert ap.parts_only(текст)["floor"] in ("1", "2")


def test_дробь_и_тип_перед_этажом_остаются_домом() -> None:
    # Хвост дроби перед «подъезд» без числа — подъезд (владелец 18.09);
    # больше 12 подъездов не бывает — «17/40 подъезд» остаётся домом.
    assert ap.parse("Ленина 17/4 подъезд").house == "17"  # type: ignore[union-attr]
    assert ap.parse("Ленина 17/40 подъезд").house == "17/40"  # type: ignore[union-attr]
    assert ap.parse("ул Ленина 5 этаж").house == "5"  # type: ignore[union-attr]
    assert ap.parse("Ленина 5 этаж 3").parts == {"floor": "3"}  # type: ignore[union-attr]


def test_ключ_дома_по_строке_карты() -> None:
    """Светлана, Хабаровск: Яндекс и DaData пишут один дом по-разному."""
    assert g.formatted_key("Полевая улица, 54, Хабаровск") == g.formatted_key(
        "Россия, Хабаровский край, г Хабаровск, ул Полевая, д 54"
    )
    assert g.formatted_key("улица Ленина, 5, Казань") != g.formatted_key(
        "проспект Ленина, 5, Казань"
    )


async def test_подтверждённый_дом_другой_картой_не_второе_предложение(
    seed_conversation, db_sessionmaker
):
    from app.models import Client
    from app.services import clients as clients_svc

    cid = await _строка_из_реплики(
        seed_conversation,
        db_sessionmaker,
        "Ул полевая 54",
        street="Ул полевая",
        house="54",
        level="A",
        geo_status=g.GEO_EXACT,
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_formatted, row.geo_provider = "ул Полевая, 54, Хабаровск", "dadata"
        row.geo_lat, row.geo_lon = 48.486335, 135.156496
        card = await s.get(Client, seed_conversation.client_id)
        # Карточка уже записана с той же улицы по Яндексу — другой строкой.
        src = ClientAddressCandidate(
            client_id=card.id,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            kind="house",
            level="B",
            street="Полевая",
            house="54",
            value="Полевая, 54",
            raw="Полевая 54",
            status="accepted",
            geo_status=g.GEO_EXACT,
            geo_formatted="Полевая улица, 54, Хабаровск",
            geo_provider="yandex",
            geo_lat=48.482751,
            geo_lon=135.163638,
            detected_at=row.detected_at - timedelta(days=2),
            message_at=row.detected_at - timedelta(days=2),
        )
        s.add(src)
        await s.flush()
        card.address, card.address_value, card.address_candidate_id = (
            "Полевая улица, 54, Хабаровск",
            "Полевая, 54",
            src.id,
        )
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, seed_conversation.client_id))
    assert view["address_candidates"] == []


async def test_улица_с_опечаткой_уступает_подтверждённому_дому(db, redis, account, db_sessionmaker):
    """Сергей, Балашиха: «ул яречная» → «Речная ул 6» (карта подтвердила)."""
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("территориально я нахожусь в Кучино ул яречная", msg="y1", when=T0),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Речная ул 6 кв 37 11 этаж", msg="y2", when=T0 + timedelta(minutes=1)),
    )
    from app.models import Client
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        assert sorted(r.kind for r in rows) == ["house", "place"]
        for r in rows:
            if r.kind == "house":
                r.geo_status, r.geo_formatted = g.GEO_EXACT, "Речная улица, 6, Балашиха"
            else:
                r.geo_status = g.GEO_NOT_FOUND
        client_id = rows[0].client_id
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, client_id))
    assert [c["value"] for c in view["address_candidates"]] == ["Речная ул, 6"]


# ── четвёртый круг ревью 14.09 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("текст", "город"),
    [
        ("телефон 89001234567 г. Гай ул Ленина 5", "Гай"),
        ('"г Гай, ул Ленина 5"', "Гай"),
        ("г. Нижний Новгород ул Ленина 5", "Нижний Новгород"),
        ("г. Набережные Челны, ул Ленина 5", "Набережные Челны"),
        ("г Гай Ленина 5", "Гай"),
    ],
)
def test_город_словом_после_телефона_и_из_двух_слов(текст: str, город: str) -> None:
    f = ap.parse(текст)
    assert f is not None and f.locality == город and f.street in ("ул Ленина", "Ленина")


def test_район_переживает_ветку_города_объявления() -> None:
    f = ap.parse("Тула, Зареченский район, ул Пушкина 47")
    assert f is not None and (f.locality, f.district) == ("Тула", "Зареченский район")


def test_ключ_карты_бережёт_литеру_и_сводит_корпус() -> None:
    assert g.formatted_key("улица Ленина, 12Е, Казань") != g.formatted_key(
        "улица Ленина, 12, Казань"
    )
    assert g.formatted_key("Полевая улица, 5к2, Хабаровск") == g.formatted_key(
        "ул Полевая, 5 к 2, Хабаровск"
    )
    assert g.formatted_key("посёлок Урожай, улица Луговая, 1") == g.formatted_key(
        "Урожай, ул Луговая, д 1"
    )


async def test_догон_меняет_дом_мусор_на_место(db, redis, account, db_sessionmaker):
    """Ангарск: старая строка «Ангарск, 11» из реплики-места — отклонить, место завести."""
    from app.cli import run_address_reparse

    текст = "Ангарск 11 микрорайон дом квартира 92"
    await apply_inbound_event(db, redis, account, событие(текст, msg="tm1", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "angarsk"
        # Строка старого разбора: улица-город, дом 11, карте отказано.
        msg = (await s.execute(sa.select(Message).where(Message.direction == "in"))).scalars().one()
        s.add(
            ClientAddressCandidate(
                client_id=conv.client_id,
                conversation_id=conv.id,
                message_id=msg.id,
                kind="house",
                level="A",
                street="Ангарск",
                house="11",
                value="Ангарск, 11",
                raw="Ангарск 11",
                status="pending",
                geo_status=g.GEO_STREET_MISMATCH,
                detected_at=T0,
                message_at=T0,
            )
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
        await s.commit()
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert sorted((r.kind, r.value, r.status) for r in rows) == [
        ("house", "Ангарск, 11", "rejected"),
        ("place", "микрорайон 11", "pending"),
    ]


def test_город_клиента_важнее_города_объявления_в_вердикте() -> None:
    """Иван: «Город Щёлково … 14 корпус 3» при объявлении в Ивантеевке — дом в Щёлкове точный."""
    from app.integrations.avito.listing_url import city_by_name

    city = city_by_name("Ивантеевка")
    assert city is not None
    parsed = g.Parsed(
        street="дубково",
        house="14 корпус 3",
        settlement=None,
        settlement_type=None,
        locality="Щёлково",
        level="B",
    )
    hit = g.GeoHit(
        street="Дубково",
        house="14к3",
        settlement=None,
        city="Щёлково",
        region="Московская область",
        lat=55.9,
        lon=38.0,
        house_level=True,
    )
    assert g.verdict(parsed, city, [hit])[0] == g.GEO_EXACT
    чужой = g.GeoHit(
        street="Дубково",
        house="14к3",
        settlement=None,
        city="Ивантеевка",
        region="Московская область",
        lat=55.9,
        lon=38.0,
        house_level=True,
    )
    assert g.verdict(parsed, city, [чужой])[0] == g.GEO_OTHER_CITY


async def test_место_старого_и_нового_разбора_одно(seed_conversation, db_sessionmaker):
    from app.models import Client
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        card = await s.get(Client, seed_conversation.client_id)
        for i, value in enumerate(("11 микрорайон", "микрорайон 11")):
            s.add(
                ClientAddressCandidate(
                    client_id=card.id,
                    conversation_id=conv.id,
                    kind="place",
                    level="A",
                    street="",
                    house="",
                    value=value,
                    raw=value,
                    status="pending",
                    geo_status=g.GEO_PENDING,
                    area=value,
                    locality="Ангарск",
                    detected_at=T0 + timedelta(minutes=i),
                    message_at=T0 + timedelta(minutes=i),
                )
            )
        await s.commit()
    async with db_sessionmaker() as s:
        view = await clients_svc.identity_view(s, await s.get(Client, seed_conversation.client_id))
    assert len(view["address_candidates"]) == 1


def test_двухсловный_город_клиента_сверяется_целиком() -> None:
    """«г. Верхняя Пышма» ≠ дом в Верхней Салде (пятый круг ревью)."""
    from app.integrations.avito.listing_url import city_by_name

    city = city_by_name("Екатеринбург")
    parsed = g.Parsed(street="ул Ленина", house="5", locality="Верхняя Пышма")
    салда = g.GeoHit(
        street="улица Ленина",
        house="5",
        settlement=None,
        city="Верхняя Салда",
        region="Свердловская область",
        lat=57.0,
        lon=60.0,
        house_level=True,
    )
    пышма = g.GeoHit(
        street="улица Ленина",
        house="5",
        settlement=None,
        city="Верхняя Пышма",
        region="Свердловская область",
        lat=57.0,
        lon=60.0,
        house_level=True,
    )
    assert g.verdict(parsed, city, [салда])[0] == g.GEO_OTHER_CITY
    assert g.verdict(parsed, city, [салда, пышма])[0] == g.GEO_EXACT


async def test_догон_не_трогает_живое_место_другого_диалога(db, redis, account, db_sessionmaker):
    """Место уже есть у карточки (другой диалог, кв 7) — догон дом отклоняет, место не переносит."""
    from app.cli import run_address_reparse

    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="tp0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "angarsk"
        msg = (await s.execute(sa.select(Message).where(Message.direction == "in"))).scalars().one()
        s.add(
            ClientAddressCandidate(
                client_id=conv.client_id,
                conversation_id=conv.id,
                message_id=msg.id,
                kind="house",
                level="A",
                street="Ангарск",
                house="11",
                value="Ангарск, 11",
                raw="Ангарск 11",
                status="pending",
                geo_status=g.GEO_STREET_MISMATCH,
                detected_at=T0,
                message_at=T0,
            )
        )
        # Живое место из более позднего диалога с квартирой 7.
        другой = Conversation(
            channel="avito",
            external_chat_id="chat-other",
            account_id=conv.account_id,
            client_id=conv.client_id,
            status="new",
            item_city_slug="angarsk",
        )
        s.add(другой)
        await s.flush()
        s.add(
            ClientAddressCandidate(
                client_id=conv.client_id,
                conversation_id=другой.id,
                kind="place",
                level="A",
                street="",
                house="",
                value="микрорайон 11",
                raw="микрорайон 11",
                status="pending",
                geo_status=g.GEO_EXACT,
                area="микрорайон 11",
                locality="Ангарск",
                office="7",
                detected_at=T0 + timedelta(days=1),
                message_at=T0 + timedelta(days=1),
            )
        )
        # Реплика-место: догон читает её по строке-дому.
        msg.body = "Ангарск 11 микрорайон дом квартира 92"
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=ОКНО_ДНЕЙ, dry_run=False)
        await s.commit()
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    место = next(r for r in rows if r.kind == "place")
    assert (место.office, место.geo_status, место.detected_at.replace(tzinfo=None)) == (
        "7",
        g.GEO_EXACT,
        (T0 + timedelta(days=1)).replace(tzinfo=None),
    )
    assert next(r for r in rows if r.kind == "house").status == "rejected"


# ── 15.09: Калуга/мкр Кошелев, «жк восток», «Гагари на 12», «Дорожная 19,14» ──


def test_микрорайон_города_в_вердикте_подсказка_а_не_сторож() -> None:
    """Калуга: «Мкр Кошелев, Улица Павла Громова д 8» — карта в адресе дома
    микрорайон не пишет; дом в Калуге точный, а не «есть в области»."""
    from app.integrations.avito.listing_url import city_by_name

    city = city_by_name("Калуга")
    parsed = g.Parsed(
        street="Улица Павла Громова",
        house="8",
        settlement="Кошелев",
        settlement_type="микрорайон",
        locality="Калуга",
    )
    hit = g.GeoHit(
        street="улица Павла Громова",
        house="8",
        settlement=None,
        city="Калуга",
        region="Калужская область",
        lat=54.5,
        lon=36.2,
        house_level=True,
    )
    assert g.verdict(parsed, city, [hit])[0] == g.GEO_EXACT
    # А настоящий пункт по-прежнему обязателен.
    посёлок = g.Parsed(
        street="ул Лесная", house="5", settlement="Сосново", settlement_type="посёлок"
    )
    лесная = g.GeoHit(
        street="улица Лесная",
        house="5",
        settlement=None,
        city="Калуга",
        region="Калужская область",
        lat=54.5,
        lon=36.2,
        house_level=True,
    )
    assert g.verdict(посёлок, city, [лесная])[0] == g.GEO_SETTLEMENT_MISMATCH


def test_многострочный_город_микрорайон_улица() -> None:
    f = ap.parse("Калуга\nМкр Кошелев\nУлица Павла Громова д 8 кв 43")
    assert f is not None and (
        f.street,
        f.house,
        f.settlement,
        f.settlement_type,
        f.locality,
        f.parts,
    ) == ("Улица Павла Громова", "8", "Кошелев", "микрорайон", "Калуга", {"office": "43"})


def test_жк_рядом_делает_строчную_улицу_адресом() -> None:
    """Роман, Владимир: «,ольховая 164 жк восток новостройка» ждал следующей реплики."""
    f = ap.parse(",ольховая 164 жк восток новостройка")
    assert f is not None and (f.level, f.street, f.house, f.settlement, f.settlement_type) == (
        "B",
        "ольховая",
        "164",
        "восток",
        "ЖК",
    )
    assert ap.parse("в 17 мкр как скоро сможете") is None


@pytest.mark.parametrize(
    ("текст", "стоит"),
    [
        ("Гагари на 12", True),
        ("Мжк район", True),
        ("Новый Уренгой", True),
        ("Мы в Самаре на Тухачевского", True),
        ("Советский район, Аделя кутуя", True),
        ("19:00", False),
        ("Да", False),
        ("89241234567", False),
        # ревью 15.09: речь, цена, время с предлогом, телефон с именем — не модели
        ("Хорошо", False),
        ("Сколько будет стоить?", False),
        ("Давайте в 19:00", False),
        ("Сегодня можно после 19:30", False),
        ("За 500?", False),
        ("Телефон 89001112253 Татьяна", False),
        ("с 9 до 12", False),
        ("В 18:00 сможете?", False),
        ("Телевизор не включается", False),
    ],
)
def test_ответ_на_вопрос_об_адресе_читает_модель(текст: str, стоит: bool) -> None:
    """Илья, Чита: «куда выезжать?» → «Гагари на 12» (улица Гагарина): правила
    молчат, приметы невода не сработали — модель читает и так."""
    from app.services import address_llm

    assert address_llm.worth_after_question(текст) is стоит


# ── геоточка Авито (владелец 15.09, Иван: «Место … СНТ Берёзово, 27») ──────────


def test_адрес_геоточки_разбирается_по_кускам() -> None:
    f = ap.parse_geopoint(
        "Московская область, городской округ Химки, посёлок Берёзово, "
        "садоводческое некоммерческое товарищество Берёзово, 27"
    )
    assert f is not None and (f.kind, f.house, f.settlement, f.settlement_type, f.district) == (
        ap.KIND_HOUSE,
        "27",
        "Берёзово",
        "посёлок",
        "городской округ Химки",
    )
    assert f.street == "садоводческое некоммерческое товарищество Берёзово"
    f = ap.parse_geopoint("Иркутская область, Ангарск, 11-й микрорайон, 5")
    assert f is not None and (f.street, f.house, f.locality) == ("11-й микрорайон", "5", "Ангарск")
    assert ap.parse_geopoint("Хабаровский край, Хабаровск") is None


async def test_геоточка_становится_подтверждённой_строкой(db, redis, account, db_sessionmaker):
    from dataclasses import replace

    точка = replace(
        событие("", msg="geo1", when=T0),
        text=None,
        attachments=[
            {
                "media_id": "avito_location_1",
                "kind": "file",
                "name": "Геопозиция",
                "size": None,
                "avito_type": "location",
                "lat": 56.0123,
                "lon": 37.1234,
                "address": "Московская область, городской округ Химки, посёлок Берёзово, "
                "садоводческое некоммерческое товарищество Берёзово, 27",
            }
        ],
    )
    await apply_inbound_event(db, redis, account, точка)
    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
    assert (row.kind, row.house, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon) == (
        "house",
        "27",
        g.GEO_EXACT,
        "avito",
        56.0123,
        37.1234,
    )
    assert row.geo_formatted.startswith("Московская область")
    assert row.settlement == "Берёзово"


async def test_догон_карточек_берёт_геоточку(db, redis, account, db_sessionmaker):
    """Иван (владелец 15.09): точка пришла до выкатки — догон заводит строку."""

    from app.cli import run_backfill_cards

    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="bg0", when=T0))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="bg-loc",
                direction="in",
                sender_type="client",
                body=None,
                attachments=[
                    {
                        "media_id": "avito_location_2",
                        "kind": "file",
                        "name": "Геопозиция",
                        "size": None,
                        "avito_type": "location",
                        "lat": 56.0,
                        "lon": 37.0,
                        "address": "Калужская область, Калуга, улица Ленина, 5",
                    }
                ],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=1),
            )
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=ОКНО_ДНЕЙ, dry_run=False)
        await s.commit()
    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
    assert (row.value, row.geo_status, row.geo_provider, row.locality) == (
        "улица Ленина, 5",
        g.GEO_EXACT,
        "avito",
        "Калуга",
    )


async def test_старая_геоточка_без_координат_идёт_к_карте(db, redis, account, db_sessionmaker):
    """Точки до 15.09 хранят только подпись: строка заводится и ждёт карту."""
    from dataclasses import replace

    точка = replace(
        событие("", msg="geo-old", when=T0),
        text=None,
        attachments=[
            {
                "media_id": "avito_location_3",
                "kind": "file",
                "name": "Калужская область, Калуга, улица Ленина, 5",
                "size": None,
                "avito_type": "location",
            }
        ],
    )
    await apply_inbound_event(db, redis, account, точка)
    async with db_sessionmaker() as s:
        row = (await s.execute(sa.select(ClientAddressCandidate))).scalars().one()
    assert (row.value, row.geo_status, row.geo_provider, row.locality) == (
        "улица Ленина, 5",
        g.GEO_PENDING,
        None,
        "Калуга",
    )
