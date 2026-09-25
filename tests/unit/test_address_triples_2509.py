"""Тройки «мкр-дом-кв» (владелец 25.09): тип массива по городу, слово «адрес»
и слово ответа перед тройкой, части адреса за ней.

Случаи владельца (здесь — вымышленные данные той же формы):

A. «Адрес N-M-K. <речь>» в Ангарске → «N мкр» и «карта нашла улицу, но не
   дом». Тройку брали только после вопроса оператора и всегда с «мкр», а
   карта отдаёт «N-й квартал» — сторож типа улицы (`geocode._улица_не_шире`)
   такой ответ не принимает.
B. «N-M-K» целиком ответом в Ангарске → «N мкр», «карта не подтвердила номер
   дома». `parse` пишет «мкр» для любого города, а форма города объявления
   (`parse_by_city`) дефисной тройки не знала: `quarter_pair_present` её не
   видел, а `parse_quarter_pair` молчал, раз `parse` нашёл свою.
C. «хорошо, N-M-K, домофон …, N этаж, …» → место без дома. Слово ответа
   перед тройкой — и правила молчали; реплика доставалась читателю частей
   (тот видит только этаж) и модели, которые тройку не понимают.
I. «Адрес N-M-KKK <слово>» в Нефтеюганске → ничего: за словом «адрес»
   тройка бралась только всей репликой.

ДИВЕРСИИ (каждая обязана краснеть): убрать дефисную тройку из
`parse_quarter_pair` → «мкр» в Ангарске; убрать тройку из
`quarter_pair_present` → живой путь в Ангарске пишет «мкр»; убрать ветку
«за словом адрес» из `_тройка` → A и I без строки; убрать слова ответа из
`_ПЕРЕД_ТРОЙКОЙ` → C без дома; убрать части остатка из `_найдена_тройка` →
C теряет этаж; убрать «квартал» из сторожа цитаты → строка Ангарска не
заводится; не передать массив города разбору без города → «Ангарск N-M-K»
с «мкр»; убрать «X» из хвоста `_МКР_ДОМ_КВ` → «N-M-K 8900…» в Ангарске
форма города не читает, остаётся «мкр».

Телефонов и имён клиентов в тестах нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.integrations.avito.listing_url import City
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.services import address_parse as ap
from app.services import geocode as g
from app.services import inbound
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 111222925
AUTHOR_ID = 999025
T0 = datetime(2026, 9, 25, 9, 0, 0, tzinfo=UTC)


# --- B: тройка квартального города — его массивом ----------------------------------


@pytest.mark.parametrize(
    ("текст", "город", "улица", "дом", "части", "цитата"),
    [
        ("73-5-8", "Ангарск", "73 квартал", "5", {"office": "8"}, "73-5-8"),
        ("73-5-8", "Шелехов", "73 квартал", "5", {"office": "8"}, "73-5-8"),
        ("73-5-8", "Нефтеюганск", "73 мкр", "5", {"office": "8"}, "73-5-8"),
        ("73-5-8", "Пыть-Ях", "73 мкр", "5", {"office": "8"}, "73-5-8"),
        ("14а-6-21", "Нефтеюганск", "14а мкр", "6", {"office": "21"}, "14а-6-21"),
        # Город объявления впереди погашен, как у пары «Ангарск 86-11».
        ("Ангарск, 73-5-8", "Ангарск", "73 квартал", "5", {"office": "8"}, "73-5-8"),
        # Части за тройкой — её части.
        (
            "73-5-8, 2 подъезд",
            "Ангарск",
            "73 квартал",
            "5",
            {"office": "8", "entrance": "2"},
            "73-5-8",
        ),
        # Номер телефона за тройкой: у формы города он погашен крестиками, у
        # `parse` — цифры; тройку оба пути читают одинаково.
        ("73-5-8 89001112245", "Ангарск", "73 квартал", "5", {"office": "8"}, "73-5-8"),
    ],
)
def test_тройка_квартального_города_несёт_его_массив(
    текст: str, город: str, улица: str, дом: str, части: dict[str, str], цитата: str
) -> None:
    f = ap.parse_by_city(текст, город)
    assert f is not None, текст
    # Уровень — как у той же тройки в `parse`: форма одна, меняется только тип.
    assert (f.street, f.house, f.parts, f.level) == (улица, дом, части, "C")
    # Цитата — сама тройка, без города и знаков вокруг.
    assert f.raw == цитата and текст[f.start : f.end] == цитата
    assert ap.quote_holds(f, текст)
    # Ворота живого пути обязаны видеть ту же форму, что и разбор.
    assert ap.quarter_pair_present(текст)


def test_ворота_формы_города_видят_только_годную_тройку() -> None:
    """В ленту за вопросом оператора — только за тройкой, которую ответ на
    вопрос сделал бы адресом; срок тройкой («10-12-14 часов») туда не водит."""
    assert ap.quarter_pair_present("Хорошо, 12-58-37. Приезжайте")
    assert ap.parse_by_city("Хорошо, 12-58-37. Приезжайте", "Ангарск") is None
    f = ap.parse_by_city("Хорошо, 12-58-37. Приезжайте", "Ангарск", про_адрес=True)
    # 12 — микрорайон: в Ангарске номер до 33 (замер боя 25.09, test_address_angarsk_mkr_2509).
    assert f is not None and (f.street, f.house) == ("12 мкр", "58")
    for текст in ("10-12-14 часов", "Да, 1-2-3 дня", "Приеду 10-12-14"):
        assert not ap.quarter_pair_present(текст), текст


def test_карта_принимает_квартал_тройки_ангарска() -> None:
    """Корень A и B на стороне карты: она отдаёт «N-й квартал, M», и сторож
    типа улицы (`geocode._улица_не_шире`) «N мкр» с ним не сводит — дом не
    подтверждался. Тройка формы города несёт «квартал», и тот же ответ — дом.
    Ответ карты подставлен руками, в сеть тест не ходит."""
    ангарск = City("Ангарск", "Иркутская область", "Asia/Irkutsk")
    ответ = g.GeoHit(
        street="73-й квартал",
        house="5",
        settlement=None,
        city="Ангарск",
        region="Иркутская область",
        lat=52.5,
        lon=103.9,
        house_level=True,
    )

    def вердикт(f: ap.Found | None) -> str:
        assert f is not None
        return g.verdict(g.Parsed(street=f.street, house=f.house), ангарск, [ответ])[0]

    assert вердикт(ap.parse_by_city("73-5-8", "Ангарск")) == g.GEO_EXACT
    assert вердикт(ap.parse("73-5-8")) == g.GEO_STREET_MISMATCH


def test_тройка_без_города_по_прежнему_мкр() -> None:
    """Город неизвестен — тип прежний: «мкр» (Нефтеюганск, Пыть-Ях, 13.09)."""
    f = ap.parse("73-5-8")
    assert f is not None and (f.street, f.house, f.parts, f.level) == (
        "73 мкр",
        "5",
        {"office": "8"},
        "C",
    )
    # Вне квартальных городов формы города нет — решает `parse`.
    assert ap.parse_by_city("73-5-8", "Саранск") is None


@pytest.mark.parametrize(
    ("текст", "улица", "город"),
    [
        ("Ангарск 73-5-8", "73 квартал", "Ангарск"),
        # Запятая за городом раньше упиралась в тройку: «Ангарск, 73» с кв 5.
        ("Ангарск, 73-5-8", "73 квартал", "Ангарск"),
        ("Нефтеюганск 11-27-143", "11 мкр", "Нефтеюганск"),
    ],
)
def test_тройка_за_названным_городом_его_массивом(текст: str, улица: str, город: str) -> None:
    """Город клиент назвал сам — тип тройки по нему, в каком бы городе ни
    висело объявление; город — городом клиента."""
    f = ap.parse(текст)
    assert f is not None and (f.street, f.locality) == (улица, город), текст
    assert ap.quote_holds(f, текст)


# --- A и I: тройка за словом «адрес», речь дальше -----------------------------------


@pytest.mark.parametrize(
    ("текст", "улица", "дом", "квартира"),
    [
        ("Адрес 91-4-17. Звоните после обеда", "91 мкр", "4", "17"),
        ("Адрес 11-27-143 хорошо", "11 мкр", "27", "143"),
        ("адрес: 5-12-40, наберите за час", "5 мкр", "12", "40"),
        ("Хорошо, адрес 5-12-40 жду мастера", "5 мкр", "12", "40"),
    ],
)
def test_тройка_за_словом_адрес_с_речью_дальше(
    текст: str, улица: str, дом: str, квартира: str
) -> None:
    """Слово «адрес» перед тройкой — улика наравне с вопросом оператора (как у
    пары): речь за тройкой её не отменяет, вопрос не нужен."""
    f = ap.parse(текст)
    assert f is not None, текст
    assert (f.street, f.house, f.parts["office"], f.level) == (улица, дом, квартира, "C")
    assert not f.raw.lower().startswith("адрес") and текст[f.start : f.end] == f.raw
    assert ap.quote_holds(f, текст)
    # В Ангарске та же тройка — массивом по номеру: до 33 микрорайон, выше —
    # квартал (замер боя 25.09, test_address_angarsk_mkr_2509).
    в_ангарске = ap.parse_by_city(текст, "Ангарск")
    assert в_ангарске is not None
    в_ангарске_улица = улица if int(улица.split()[0]) <= 33 else улица.replace("мкр", "квартал")
    assert (в_ангарске.street, в_ангарске.house) == (в_ангарске_улица, дом)
    assert ap.quote_holds(в_ангарске, текст)


# --- C: слово ответа перед тройкой, части за ней -----------------------------------


@pytest.mark.parametrize(
    ("текст", "части"),
    [
        (
            "хорошо, 5-3-12, домофон открыт, 2 этаж, позвоните за час",
            {"office": "12", "floor": "2"},
        ),
        ("Да, 5-3-12", {"office": "12"}),
        ("Здравствуйте! 5-3-12, 4 подъезд", {"office": "12", "entrance": "4"}),
        ("Ок 5-3-12 кв 12, этаж 2", {"office": "12", "floor": "2"}),
    ],
)
def test_тройка_после_слова_ответа(текст: str, части: dict[str, str]) -> None:
    """Дом не теряется: «5 мкр, 3», квартира — третьим числом, части реплики
    (этаж, подъезд) — к ней. Слово ответа впереди тройке не мешает, как паре."""
    f = ap.parse(текст)
    assert f is not None, текст
    assert (f.street, f.house, f.parts, f.level) == ("5 мкр", "3", части, "C")
    assert f.raw == "5-3-12" and ap.quote_holds(f, текст)


def test_явная_квартира_за_тройкой_сильнее_третьего_числа() -> None:
    """«кв 9» словами — квартира; третье число тройки ей не спорит (так же у
    дома с квартирой через дефис: явное слово сильнее догадки по форме)."""
    f = ap.parse("73-5-8 кв 9")
    assert f is not None and (f.house, f.parts) == ("5", {"office": "9"})


def test_тройка_с_речью_за_точкой_по_прежнему_только_после_вопроса() -> None:
    """Слово ответа впереди не заменяет вопрос оператора: «Хорошо, N-M-K.
    <речь>» без вопроса — не адрес, после вопроса — адрес (19.09)."""
    текст = "Хорошо, 12-58-37. Приезжайте к обеду"
    assert ap.parse(текст) is None
    f = ap.parse(текст, про_адрес=True)
    assert f is not None and (f.value, f.parts) == ("12 мкр, 58", {"office": "37"})


# --- сторожа 14.09: сроки, размеры, телефоны, речь -----------------------------------


@pytest.mark.parametrize(
    "текст",
    [
        # Срок и размер тройкой (14.09) — и со словом ответа, и за «адрес».
        "10-12-14 часов",
        "1-2-3 дня",
        "20-30-40 см",
        "Хорошо, 10-12-14 часов",
        "Да, 1-2-3 дня",
        "Адрес 10-12-14 часов",
        "Адрес 20-30-40 см",
        # Телефон с дефисами — не тройка и после слова «адрес».
        "Адрес 8-912-3456789",
        "Да, 8-912-345",
        # Продолжение числа за тройкой: литера вплотную, дробь.
        "Адрес 11-27-143а",
        "Адрес 11-27-143/2",
        # Ведущий ноль — дата.
        "Адрес 12-05-24 приезжайте",
    ],
)
def test_тройка_не_срок_не_размер_не_телефон(текст: str) -> None:
    for f in (
        ap.parse(текст),
        ap.parse(текст, про_адрес=True),
        ap.parse_by_city(текст, "Ангарск"),
        ap.parse_by_city(текст, "Нефтеюганск", про_адрес=True),
    ):
        assert f is None or not f.street.endswith((" мкр", " квартал")), (текст, f)


@pytest.mark.parametrize(
    "текст",
    [
        # Речь перед тройкой — не ответ адресом: слово ответа не любое.
        "Приеду 10-12-14, договорились",
        "Завтра 10-12-14 удобно?",
        "Размер 60-60-85 кв",
        # «адрес» не впереди тройки — речь о другом.
        "10-12-14 адрес пришлю позже",
    ],
)
def test_тройка_внутри_речи_не_адрес(текст: str) -> None:
    for f in (ap.parse(текст), ap.parse_by_city(текст, "Ангарск")):
        assert f is None or not f.street.endswith((" мкр", " квартал")), (текст, f)


@pytest.mark.parametrize(
    "текст", ["завтра 10-12", "2-3 дня", "500-700 рублей", "через 30-40 минут"]
)
def test_пары_квартального_города_по_прежнему_с_уликами(текст: str) -> None:
    """Сторожа пары (ревью 14.09) целы: дефисная тройка встала перед ними и
    не открыла дорогу времени, сроку и цене."""
    assert ap.parse_by_city(текст, "Ангарск") is None


# --- живой путь ---------------------------------------------------------------------


def событие(текст: str, *, msg: str, when: datetime, город: str, chat: str) -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat,
        external_message_id=msg,
        author_id=AUTHOR_ID,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Клиент",
        item_title="Ремонт техники",
        item_url=f"https://www.avito.ru/{город}/predlozheniya_uslug/remont_123456789",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):  # noqa: ANN001
    return await make_avito_account(AVITO_USER_ID)


async def _строки(db_sessionmaker) -> list[ClientAddressCandidate]:  # noqa: ANN001
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )


async def test_живой_путь_ангарска_пишет_квартал(db, redis, account, db_sessionmaker) -> None:
    """B: «N-M-K» ответом в Ангарске — строка «N квартал, M», квартира K;
    карта ищет «N квартал», а не «N мкр»."""
    await apply_inbound_event(
        db, redis, account, событие("73-5-8", msg="m1", when=T0, город="angarsk", chat="c-ang")
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.office, строка.value) == (
        "73 квартал",
        "5",
        "8",
        "73 квартал, 5",
    )


async def test_живой_путь_тройка_по_городу_другого_диалога(
    db, redis, account, db_sessionmaker
) -> None:
    """Тройка при объявлении в городе без этой формы — по квартальному городу
    другого диалога клиента, как пара (19.09): он же город клиента."""
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Здравствуйте", msg="m0", when=T0 - timedelta(days=2), город="angarsk", chat="c-a"),
    )
    await apply_inbound_event(
        db, redis, account, событие("73-5-8", msg="m1", when=T0, город="saransk", chat="c-s")
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.locality) == ("73 квартал", "5", "Ангарск")


async def test_живой_путь_тройка_после_слова_ответа_без_модели(
    seed_conversation, db_sessionmaker, monkeypatch
) -> None:
    """C: правила читают тройку сами — дом, квартира и этаж в одной строке, и
    модель-читатель эту реплику не будит (раньше правила молчали, и строку
    собирали читатель частей и модель — место без дома)."""
    monkeypatch.setattr(inbound.address_llm, "enabled", lambda: True)
    текст = "хорошо, 5-3-12, домофон открыт, 2 этаж, позвоните за час"
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "nefteyugansk"
        client = await s.get(Client, seed_conversation.client_id)
        msg = Message(
            conversation_id=conv.id,
            external_message_id=f"m-{uuid.uuid4().hex[:6]}",
            direction="in",
            sender_type="client",
            body=текст,
            attachments=[],
            delivery_status="delivered",
            created_at=T0,
        )
        s.add(msg)
        await s.flush()
        итог = await inbound.replay_card_extraction_full(s, conv, client, msg, now=T0)
        await s.commit()
    assert итог.llm_read_wanted is False
    строки = [r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_HOUSE]
    assert [(r.street, r.house, r.office, r.floor) for r in строки] == [("5 мкр", "3", "12", "2")]
