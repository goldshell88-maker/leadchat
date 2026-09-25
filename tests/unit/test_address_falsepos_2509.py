"""Ложный адрес и голый дом (владелец 25.09): три случая из переписки.

G «<прилагательное> гарнитура N» после вопроса оператора — улица «…
  гарнитура», дом N (уровень B). Строчный невод после вопроса (12.09) берёт
  слово на «-а/-и/-ы» именем улицы («ленина», «победы»), а стоп-список
  `_НЕ_УЛИЦА` знал мебель и технику только в именительном («шкаф»,
  «холодильник»): родительный («гарнитура», «холодильника») проходил.
P «Телевизор <Марка> N» — улица «<Марка>», дом N. Марка кириллицей
  («Панасоник») есть в стороже пары (`_НЕ_АДРЕС_ПЕРЕД_ПАРОЙ`), но не в
  `_НЕ_УЛИЦА`; а «телевизор» перед маркой срезается служебным словом раньше,
  чем голову судит стоп-список, — соседство «телевизор → марка → число»
  терялось, и любая марка вне списка («Ортекс», «Сони») становилась улицей.
J «41/7б» целиком репликой после улицы без дома — строки не было: голый дом
  к месту приводит только `house_only` (`_ТОЛЬКО_ДОМ`), а он знал лишь «дом
  N», «N дом», «номер N». Дробь без литеры («14/10») — дата, домом не
  становится.

Данные выдуманы: улицы, дома и марки — не из переписки.

ДИВЕРСИИ (каждая обязана краснеть): убрать родительный мебели и техники из
`_НЕ_УЛИЦА` → G; убрать марки кириллицей → P по марке; убрать соседство
«телевизор» в `_годные_головы` → P с маркой вне списка; убрать ветку голой
дроби с литерой из `_ТОЛЬКО_ДОМ` → J.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import ClientAddressCandidate, Conversation, Message
from app.services import address_parse as ap
from app.services import inbound
from app.services.inbound import apply_inbound_event
from tests.unit.test_address_inbound_0909 import AVITO_USER_ID, T0, событие

pytestmark = pytest.mark.anyio


def _исходящее(conversation_id: uuid.UUID, текст: str, ext: str, когда: datetime) -> Message:
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


async def _строки(db_sessionmaker) -> list[ClientAddressCandidate]:  # noqa: ANN001
    async with db_sessionmaker() as s:
        строки = list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())
    return sorted(строки, key=lambda r: r.detected_at)


# ── G. мебель и техника в родительном ─────────────────────────────────────


@pytest.mark.parametrize(
    "текст",
    [
        "спального гарнитура 3",
        "сборка спального гарнитура 3",
        "сборка углового дивана 2",
        "сборка мебели 3",
        "ремонт холодильника 2",
        "установка кондиционера 4",
        "замена смесителя 1",
        "подключение духовки 2",
    ],
)
def test_мебель_и_техника_в_родительном_не_улица(текст: str) -> None:
    """После вопроса оператора строчный невод берёт родительный за улицу."""
    assert ap.parse(текст, про_адрес=True) is None, текст
    assert ap.parse(текст) is None, текст
    assert ap.gate(текст, про_адрес=True) is None, текст


@pytest.mark.parametrize(
    ("текст", "улица", "дом"),
    [
        ("маршала жукова 7", "маршала жукова", "7"),
        ("красного октября 3", "красного октября", "3"),
        ("победы 12", "победы", "12"),
        ("горького 15", "горького", "15"),
        # Набережная реки Мойки: «мойки» в стоп-список не берётся.
        ("наб. реки Мойки 12", "наб. реки Мойки", "12"),
    ],
)
def test_улица_в_родительном_после_вопроса_остаётся(текст: str, улица: str, дом: str) -> None:
    f = ap.parse(текст, про_адрес=True)
    assert f is not None and (f.street, f.house) == (улица, дом)


async def test_гарнитур_в_ответ_на_вопрос_не_заводит_строку(
    db, redis, db_sessionmaker, make_avito_account
):
    """Живой путь: «Подскажите адрес» → «…гарнитура 3» — строки адреса нет."""
    account = await make_avito_account(AVITO_USER_ID)
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="g0"))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(_исходящее(conv.id, "Подскажите адрес", "out-g1", T0 + timedelta(minutes=1)))
        await s.commit()
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("нужна сборка спального гарнитура 3", msg="g1", when=T0 + timedelta(minutes=2)),
    )
    assert await _строки(db_sessionmaker) == []


# ── P. марка телевизора кириллицей и соседство «телевизор» ───────────────


@pytest.mark.parametrize(
    "текст",
    [
        # Форма случая; её держит и соседство «телевизор» — марку отдельно
        # стережёт строка ниже, без него.
        "Телевизор Панасоник 43",
        "Панасоник 43 не включается",
        "Филипс 42 не показывает",
        "Элджи 32",
        "Хисенс 50",
    ],
)
def test_марка_кириллицей_не_улица(текст: str) -> None:
    """Те же марки, что знает сторож пары `_НЕ_АДРЕС_ПЕРЕД_ПАРОЙ`."""
    assert ap.parse(текст) is None, текст
    assert ap.parse(текст, про_адрес=True) is None, текст


@pytest.mark.parametrize(
    "текст",
    [
        "Телевизор Ортекс 24",  # марка выдумана: в списке её нет и не будет
        "телевизор Сони 40",  # «сони» в список нельзя — улица Сони Кривой
        "Телевизор Ортекс, 24",
        "Не работает телевизор Ортекс 32",
    ],
)
def test_телевизор_перед_названием_это_модель(текст: str) -> None:
    assert ap.parse(текст) is None, текст
    assert ap.parse(текст, про_адрес=True) is None, текст


@pytest.mark.parametrize(
    ("текст", "улица", "дом"),
    [
        ("Телевизор не включается, Ленина 5", "Ленина", "5"),
        ("Телевизор, Гагарина 15", "Гагарина", "15"),
        ("Телевизор\nГагарина 15", "Гагарина", "15"),
        ("Привезу телевизор на Ленина 5", "Ленина", "5"),
        ("Телевизор Самсунг 55, привезите на Ленина 5", "Ленина", "5"),
        ("ул. Сони Кривой 5", "ул. Сони Кривой", "5"),
        ("Сони Кривой 5", "Сони Кривой", "5"),
    ],
)
def test_улица_рядом_с_телевизором_остаётся(текст: str, улица: str, дом: str) -> None:
    f = ap.parse(текст)
    assert f is not None and (f.street, f.house) == (улица, дом), текст
    assert ap.quote_holds(f, текст)


def test_телевизор_в_родительном_не_прилипает_к_улице() -> None:
    """«Ремонт телевизора Гагарина 15»: родительный «телевизора» — не часть
    улицы (было «телевизора Гагарина») и не соседство марки (соседство —
    только «телевизор» в именительном): улица Гагарина, дом 15."""
    f = ap.parse("Ремонт телевизора Гагарина 15")
    assert f is not None and (f.street, f.house) == ("Гагарина", "15")


async def test_марка_телевизора_не_заводит_строку(db, redis, db_sessionmaker, make_avito_account):
    account = await make_avito_account(AVITO_USER_ID)
    await apply_inbound_event(db, redis, account, событие("Телевизор Панасоник 43, не включается"))
    assert await _строки(db_sessionmaker) == []


# ── J. голый дом дробью с литерой ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("текст", "дом", "фраза"),
    [
        ("41/7б", "41/7б", "41/7б"),
        ("41/7Б", "41/7Б", "41/7Б"),
        ("41/7б.", "41/7б", "41/7б"),
        ("41/7б, кв 12", "41/7б", "41/7б"),
        ("дом 41/7б", "41/7б", "дом 41/7б"),  # как было
    ],
)
def test_дом_дробью_с_литерой_целиком_репликой(текст: str, дом: str, фраза: str) -> None:
    assert ap.house_only(текст) == дом
    assert ap.house_phrase(текст) == фраза


@pytest.mark.parametrize(
    "текст",
    [
        "14/10",  # дата
        "1/2",  # дробь
        "41/7",  # без литеры не отличить от даты
        "14/10 в 12",
        "41/7б и 41/8б",
        "в 41/7б",
        "41/7бис",
    ],
)
def test_дробь_без_литеры_не_дом(текст: str) -> None:
    assert ap.house_only(текст) is None, текст
    assert ap.house_phrase(текст) is None, текст


async def test_голый_дом_дробью_продолжает_улицу(db, redis, db_sessionmaker, make_avito_account):
    """«ул. Садовая» → «Номер дома?» → «41/7б»: дом к улице прошлой реплики,
    как «дом 9» (13.09); цитата — обе реплики клиента."""
    account = await make_avito_account(AVITO_USER_ID)
    await apply_inbound_event(db, redis, account, событие("ул. Садовая"))
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(_исходящее(conv.id, "Номер дома подскажите", "out-j1", T0 + timedelta(minutes=1)))
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("41/7б", msg="am-2", when=T0 + timedelta(minutes=2))
    )
    строки = await _строки(db_sessionmaker)
    assert [(r.kind, r.value) for r in строки] == [
        ("place", "ул. Садовая"),
        ("house", "ул. Садовая, 41/7б"),
    ]
    assert строки[1].raw == "ул. Садовая; 41/7б"


async def test_дата_дробью_не_дом_к_улице(db, redis, db_sessionmaker, make_avito_account):
    account = await make_avito_account(AVITO_USER_ID)
    await apply_inbound_event(db, redis, account, событие("ул. Садовая"))
    await apply_inbound_event(
        db, redis, account, событие("14/10", msg="am-2", when=T0 + timedelta(minutes=2))
    )
    assert [(r.kind, r.value) for r in await _строки(db_sessionmaker)] == [("place", "ул. Садовая")]


async def test_голый_дом_без_улицы_строки_не_заводит(
    db, redis, db_sessionmaker, make_avito_account
):
    account = await make_avito_account(AVITO_USER_ID)
    await apply_inbound_event(db, redis, account, событие("41/7б"))
    assert await _строки(db_sessionmaker) == []


@pytest.mark.parametrize(("тело", "без_адреса"), [("41/7б", False), ("14/10", True)])
async def test_предикат_реплика_без_адреса_и_голый_дом(
    seed_conversation, db_sessionmaker, тело: str, без_адреса: bool
):
    """Один предикат на догон и сторож автозаписи: «41/7б» — продолжение места,
    как «дом 9»; «14/10» — речь."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        msg = Message(
            conversation_id=conv.id,
            external_message_id="x",
            direction="in",
            sender_type="client",
            body=тело,
            attachments=[],
            delivery_status="delivered",
            created_at=T0,
        )
        found = await inbound.разбор_реплики_сейчас(
            s, conv, msg, client_id=seed_conversation.client_id
        )
        assert inbound.реплика_без_адреса(msg, found) is без_адреса
