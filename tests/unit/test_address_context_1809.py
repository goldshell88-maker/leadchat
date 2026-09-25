"""Адрес с контекстом (владелец 18.09: «определение адресов должно работать
с контекстом»): пункт из соседней реплики клиента — до улицы и после неё,
части следующей репликой — в адрес карточки, а не только в цитату, пара
«комплекс/дом» Набережных Челнов на живом пути и уступка авто-адреса новой
подтверждённой строке той же реплики (Ангарск, «85-й квартал, 17 / улица
Гагрина, 12»).

Реплики — с экранов владельца 18.09; улицы, дома и квартиры в них заменены
вымышленными той же формы. Телефонов и имён клиентов нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING
from app.services import address_parse as ap
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services import inbound
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 111222918
T0 = datetime(2026, 9, 18, 9, 0, 0, tzinfo=UTC)


def событие(
    текст: str, *, msg: str, when: datetime, город: str = "saransk", имя: str = "Клиент"
) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-1809",
        external_message_id=msg,
        author_id=999018,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name=имя,
        item_title="Ремонт сантехники",
        item_url=f"https://www.avito.ru/{город}/predlozheniya_uslug/remont_123456789",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):
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


async def _исходящее(db_sessionmaker, текст: str, when: datetime) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"out-{when.timestamp()}",
                direction="out",
                sender_type="user",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def _диалог(db, redis, account, реплики: list[str], **kw) -> None:  # noqa: ANN001, ANN003
    for i, текст in enumerate(реплики):
        await apply_inbound_event(
            db, redis, account, событие(текст, msg=f"m{i}", when=T0 + timedelta(minutes=i), **kw)
        )


# ── 1. пункт из соседней реплики ─────────────────────────────────────────────


async def test_пункт_до_улицы_из_речи_в_сукко(db, redis, account, db_sessionmaker):
    """«…нам нужен сантехник в Сукко…» → (через реплику) улица без пункта:
    пункт из ранней реплики — в строку до карты, цитата — обе реплики."""
    await _диалог(
        db,
        redis,
        account,
        [
            "Здравствуйте, пишу по объявлению, нам нужен сантехник в Сукко, "
            "в однокомнатную квартиру",
            "Да, завтра удобно",
            "ул. Солнечная 5, только напишите когда сможете",
        ],
        город="anapa",
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house) == ("ул. Солнечная", "5")
    assert (строка.settlement, строка.settlement_type) == ("Сукко", None)
    assert строка.raw == "в Сукко; ул. Солнечная 5"
    assert строка.geo_status == g.GEO_PENDING


async def test_ялга_до_и_после_улицы(db, redis, account, db_sessionmaker):
    """Скрин владельца: «Ялга ул Мичурина д 3 кв 27 2 подъезд 4 этаж!» →
    «Ялга! Мы вам звонили!» → «Адрес ещё спросили ! Мичурина 3 кв 27».
    Одна строка, пункт Ялга, части все, цитата показывает, откуда пункт."""
    await _диалог(
        db,
        redis,
        account,
        [
            "Ялга ул Мичурина д 3 кв 27 2 подъезд 4 этаж! Домофона нет!",
            "Ялга! Мы вам звонили!",
            "Адрес ещё спросили ! Мичурина 3 кв 27",
        ],
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house) == ("ул Мичурина", "3")
    assert строка.settlement == "Ялга"
    assert (строка.office, строка.entrance, строка.floor) == ("27", "2", "4")
    assert "Ялга" in (строка.raw or "")
    assert ap.quote_holds(
        ap.Found(
            street=строка.street,
            house=строка.house,
            raw=строка.raw,
            start=0,
            end=0,
            level=строка.level,
            settlement=строка.settlement,
        ),
        строка.raw,
    )


async def test_пункт_после_улицы_одним_словом(db, redis, account, db_sessionmaker):
    """«Мичурина 3 кв 27» → «Ялга»: пункт дописан в строку, цитата
    дополнена спереди («Ялга; …»), вердикт карты сброшен — строка идёт к
    карте заново, и на этот раз с пунктом в запросе."""
    await _диалог(db, redis, account, ["Мичурина 3 кв 27"])
    (до,) = await _строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, до.id)
        row.geo_status = g.GEO_EXACT
        row.geo_formatted = "ул Мичурина, 3, Саранск"
        row.geo_attempts = 1
        await s.commit()
    # Голое название — только в ответ на вопрос об адресе: «Планшет» на
    # «что чиним?» тем же словом-репликой пунктом не станет.
    await _исходящее(db_sessionmaker, "Что за техника?", T0 + timedelta(seconds=20))
    await apply_inbound_event(
        db, redis, account, событие("Планшет", msg="m0p", when=T0 + timedelta(seconds=40))
    )
    (между,) = await _строки(db_sessionmaker)
    assert (между.settlement, между.geo_status) == (None, g.GEO_EXACT)
    await _исходящее(db_sessionmaker, "Уточните адрес: какой посёлок?", T0 + timedelta(seconds=50))
    await apply_inbound_event(
        db, redis, account, событие("Ялга", msg="m1", when=T0 + timedelta(minutes=1))
    )
    (после,) = await _строки(db_sessionmaker)
    assert после.id == до.id
    assert (после.settlement, после.settlement_type) == ("Ялга", None)
    assert после.raw == "Ялга; Мичурина 3"
    assert после.geo_status == g.GEO_PENDING and после.geo_attempts == 0
    assert await redis.exists(f"arq:job:geocode:{после.id}")
    # «Дом 5» после склеенной строки — к той же улице и тому же пункту; в
    # цитате — и пункт, и улица (сторож сверяет оба), и новый дом.
    await apply_inbound_event(
        db, redis, account, событие("дом 5", msg="m2", when=T0 + timedelta(minutes=2))
    )
    дома = [(r.value, r.settlement, r.raw) for r in await _строки(db_sessionmaker)]
    assert ("Мичурина, 5", "Ялга", "Ялга; Мичурина 3; дом 5") in дома


async def test_имя_после_вопроса_оператора_не_пункт(db, redis, account, db_sessionmaker):
    """«Как вас зовут?» → «Наталья.» → «Ленина 5»: слово после вопроса об
    имени — имя человека, не пункт; и назад, и вперёд."""
    await _диалог(db, redis, account, ["Здравствуйте"])
    await _исходящее(db_sessionmaker, "Как вас зовут?", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db, redis, account, событие("Наталья.", msg="m1", when=T0 + timedelta(minutes=2))
    )
    await apply_inbound_event(
        db, redis, account, событие("ул Ленина 5", msg="m2", when=T0 + timedelta(minutes=3))
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.locality, строка.raw) == (None, None, "ул Ленина 5")
    await _исходящее(db_sessionmaker, "Как к вам обращаться?", T0 + timedelta(minutes=4))
    await apply_inbound_event(
        db, redis, account, событие("Ольга", msg="m3", when=T0 + timedelta(minutes=5))
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.raw) == (None, "ул Ленина 5")


@pytest.mark.parametrize(
    "реплика",
    [
        "Спасибо! Ждём вас",
        "Хорошо. Во сколько приедете?",
        "Договорились.",
        "Мы в Саранске, недалеко от центра",
        "Напишу в Авито, когда буду дома",
        "Приеду в Октябре",
    ],
)
async def test_речь_с_заглавной_не_пункт(db, redis, account, db_sessionmaker, реплика: str):
    """Первая фраза одним словом и «в <Слово>» — речь, а не пункт: стоп-список,
    город объявления в любом падеже."""
    await _диалог(db, redis, account, [реплика, "ул Ленина 5"])
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.locality, строка.raw) == (None, None, "ул Ленина 5")


async def test_пункт_перед_чужим_адресом_не_переходит_дальше(db, redis, account, db_sessionmaker):
    """«Ялга» → «ул Ленина 5» → «кв 3» → «ул Пушкина 7»: пункт — у Ленина 5,
    а Пушкина 7 без пункта (граница — реплика с адресом; как у стенда 15.09
    с «СНТ Ромашка»)."""
    await _диалог(db, redis, account, ["Здравствуйте"])
    await _исходящее(db_sessionmaker, "Куда подъехать?", T0 + timedelta(seconds=30))
    for i, текст in enumerate(["Ялга", "ул Ленина 5", "кв 3", "ул Пушкина 7"], start=1):
        await apply_inbound_event(
            db, redis, account, событие(текст, msg=f"b{i}", when=T0 + timedelta(minutes=i))
        )
    строки = {r.value: (r.settlement, r.office) for r in await _строки(db_sessionmaker)}
    assert строки == {"ул Ленина, 5": ("Ялга", "3"), "ул Пушкина, 7": (None, None)}


async def test_город_из_справочника_соседней_репликой_это_город_клиента(
    db, redis, account, db_sessionmaker
):
    """«Люберцы» отдельной репликой → «ул Ленина 5»: имя из справочника
    городов — город клиента (`locality`), карта ищет дом в нём."""
    await _диалог(db, redis, account, ["Люберцы", "ул Ленина 5"], город="tomilino")
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.locality) == (None, "Люберцы")
    assert строка.raw == "Люберцы; ул Ленина 5"


async def test_анкета_и_реплика_с_адресом_не_источник_пункта(db, redis, account, db_sessionmaker):
    """Поля анкеты Авито — не речь о месте; в реплике с адресом пункт —
    дело её собственной строки."""
    анкета = (
        "Вот подробности 👇\n\nКомментарий:\nЯлга\n\nКогда нужна услуга:\nзавтра\n\n"
        "✨Задача составлена по ответам клиента в анкете"
    )
    await _диалог(db, redis, account, [анкета, "ул Ленина 5"])
    строки = {r.value: r.settlement for r in await _строки(db_sessionmaker)}
    assert строки.get("ул Ленина, 5", "нет") is None


def test_пункт_в_реплике_формы() -> None:
    assert inbound._пункт_в_реплике("Ялга") == inbound.ПунктКонтекста(
        "Ялга", None, "Ялга", inbound.ФОРМА_РЕПЛИКА
    )
    assert inbound._пункт_в_реплике("Ялга! Мы вам звонили!") == inbound.ПунктКонтекста(
        "Ялга", None, "Ялга", inbound.ФОРМА_ФРАЗА
    )
    в_речи = inbound._пункт_в_реплике("нам нужен сантехник в Сукко, в однокомнатную квартиру")
    assert в_речи == inbound.ПунктКонтекста("Сукко", None, "в Сукко", inbound.ФОРМА_В)
    for речь in [
        "Здравствуйте! Нужен мастер",
        "Ок.",
        "в Ватсапе напишу",
        "В целом, всё понятно",
        "Планшет. Не включается",  # предмет ремонта с точкой — не восклицание
        "Ура! Заработало",
        "Приеду в Октябре",
    ]:
        assert inbound._пункт_в_реплике(речь) is None, речь
    # Голое название требует вопроса об адресе; город из справочника — нет.
    планшет, люберцы = inbound._пункт_в_реплике("Планшет"), inbound._пункт_в_реплике("Люберцы")
    assert планшет is not None and планшет.нужен_вопрос
    assert люберцы is not None and not люберцы.нужен_вопрос


# ── 2. части следующей репликой — в адрес карточки ───────────────────────────


async def _карточка_с_авто_адресом(
    db_sessionmaker, row_id: uuid.UUID, текст: str, *, кнопкой: bool = False
) -> None:  # noqa: ANN001
    """Состояние после автозаписи: строка подтверждена картой и принята без
    человека, адрес карточки собран из неё."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, row_id)
        row.geo_status = g.GEO_EXACT
        row.geo_formatted = текст
        row.geo_lat, row.geo_lon = 48.48, 135.07  # инвариант exact ⇒ lat/lon (контракт 18.09)
        row.status = CANDIDATE_ACCEPTED
        row.resolved_at = datetime.now(UTC)
        row.resolved_by_id = uuid.uuid4() if кнопкой else None
        card = await s.get(Client, row.client_id)
        card.address = текст
        card.address_candidate_id = row.id
        card.address_value = row.value
        card.address_set_by_id = None
        card.address_set_at = None
        await s.commit()


async def test_части_следующей_репликой_обновляют_авто_адрес(db, redis, account, db_sessionmaker):
    """Скрин владельца (Хабаровск): «Попова 7б» → авто «ул Попова, 7б,
    Хабаровск» → «3 подъезд, квартира 52.» — части были только в цитате."""
    await _диалог(db, redis, account, ["Попова 7б"], город="habarovsk")
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, строка.id, "ул Попова, 7б, Хабаровск")
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("3 подъезд, квартира 52.", msg="m1", when=T0 + timedelta(minutes=1)),
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, строка.client_id)
        assert card.address == "ул Попова, 7б, Хабаровск, кв 52, подъезд 3"
        assert card.address_candidate_id == строка.id and card.address_set_at is None
        row = await s.get(ClientAddressCandidate, строка.id)
        assert (row.office, row.entrance) == ("52", "3")
    # Повтор адреса с квартирой — тот же путь (ключ без квартиры, ревью 11.09).
    await apply_inbound_event(
        db, redis, account, событие("Попова 7б кв 53", msg="m2", when=T0 + timedelta(minutes=2))
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, строка.client_id)
        assert card.address == "ул Попова, 7б, Хабаровск, кв 53, подъезд 3"


async def test_сброшенный_вердикт_источника_держит_строку_карты(
    db, redis, account, db_sessionmaker
):
    """Пункт дописан в строку-источник, вердикт сброшен — в поле остаётся
    строка карты, а не слова клиента; части ждут нового вердикта."""
    await _диалог(db, redis, account, ["Попова 7б"], город="habarovsk")
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, строка.id, "ул Попова, 7б, Хабаровск")
    async with db_sessionmaker() as s:
        clients_svc.сбросить_вердикт(
            await s.get(ClientAddressCandidate, строка.id), reason="settlement_named"
        )
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("кв 70", msg="m1", when=T0 + timedelta(minutes=1))
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Client, строка.client_id)).address == "ул Попова, 7б, Хабаровск"
        assert (await s.get(ClientAddressCandidate, строка.id)).office == "70"


async def test_адрес_подтверждённый_человеком_части_не_переписывают(
    db, redis, account, db_sessionmaker
):
    """Подтверждено кнопкой — слова человека: части дописываются в строку,
    адрес карточки не трогается (как и набранный руками)."""
    await _диалог(db, redis, account, ["Попова 7б"], город="habarovsk")
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(
        db_sessionmaker, строка.id, "ул Попова, 7б, Хабаровск", кнопкой=True
    )
    await apply_inbound_event(
        db, redis, account, событие("кв 52", msg="m1", when=T0 + timedelta(minutes=1))
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, строка.client_id)
        assert card.address == "ул Попова, 7б, Хабаровск"
        assert (await s.get(ClientAddressCandidate, строка.id)).office == "52"
        card.address_set_at = datetime.now(UTC)  # набрано руками
        card.address_candidate_id = None
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("кв 54", msg="m2", when=T0 + timedelta(minutes=2))
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, строка.client_id)
        assert card.address == "ул Попова, 7б, Хабаровск"


# ── 3. Набережные Челны: пара «комплекс/дом» на живом пути ───────────────────


async def test_комплексная_пара_на_живом_пути(db, redis, account, db_sessionmaker):
    """Скрин владельца (Набережные Челны): «…Мы пока за городом…» → «38/07» →
    «38/07 17 подъезд 286 кв». Ветка по образцу квартальных городов: в
    комплексном городе при паре зовётся `parse_by_city` (участок P:
    `КОМПЛЕКСНЫЕ_ГОРОДА`, `complex_pair_present`); в другом городе пара —
    не адрес."""
    await _диалог(
        db,
        redis,
        account,
        ["Мы пока за городом", "38/07", "38/07 17 подъезд 286 кв"],
        город="naberezhnye_chelny",
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.entrance, строка.office, строка.level) == (
        "38-й комплекс",
        "7",
        "17",
        "286",
        "B",
    )
    assert строка.raw == "38/07"
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        conv.item_city_slug = "anapa"
        await s.commit()
    await apply_inbound_event(
        db, redis, account, событие("46/12", msg="x1", when=T0 + timedelta(hours=2))
    )
    assert [r.value for r in await _строки(db_sessionmaker)] == ["38-й комплекс, 7"]


# ── 4. авто-адрес уступает новой подтверждённой строке той же реплики ────────


async def _строка_карты(
    s,
    client_id: uuid.UUID,
    conv_id: uuid.UUID,
    *,
    street: str,
    house: str,
    **kw,  # noqa: ANN001, ANN003
) -> ClientAddressCandidate:
    row = ClientAddressCandidate(
        id=uuid.uuid4(),
        client_id=client_id,
        conversation_id=conv_id,
        message_id=kw.pop("message_id", None),
        message_at=T0,
        value=f"{street}, {house}",
        street=street,
        house=house,
        raw=kw.pop("raw", f"{street} {house}"),
        level="A",
        source="inbound",
        status=kw.pop("status", CANDIDATE_PENDING),
        detected_at=kw.pop("detected_at", T0),
        kind=kw.pop("kind", ap.KIND_HOUSE),
        geo_status=kw.pop("geo_status", g.GEO_EXACT),
        geo_formatted=kw.pop("geo_formatted", None),
        resolved_by_id=kw.pop("resolved_by_id", None),
    )
    # Координаты при exact — инвариант `exact ⇒ lat/lon` (контракт 18.09 п.3):
    # без них у строки нет степени и лестница уступок её не рассматривает.
    if row.geo_status == g.GEO_EXACT:
        row.geo_lat = kw.pop("geo_lat", 52.52)
        row.geo_lon = kw.pop("geo_lon", 103.94)
    assert not kw, kw
    s.add(row)
    await s.flush()
    return row


async def test_авто_адрес_уступает_новому_разбору_той_же_реплики(
    seed_conversation, db_sessionmaker
):
    """Ангарск: старый разбор «85-й квартал, 17 / улица Гагрина, 12» дал
    «ул Гагарина, 17» (exact, авто в карточке), новый — «85-й квартал, 17»
    (exact): та же реплика — уступает; из другой реплики и оба exact — нет."""
    msg = seed_conversation.message_id
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        conv = seed_conversation.conversation_id
        старая = await _строка_карты(
            s,
            card.id,
            conv,
            street="ул Гагарина",
            house="17",
            message_id=msg,
            status=CANDIDATE_ACCEPTED,
            geo_formatted="ул Гагарина, 17, Байкальск",
        )
        card.address = "ул Гагарина, 17, Байкальск"
        card.address_candidate_id = старая.id
        card.address_value = старая.value
        новая = await _строка_карты(
            s,
            card.id,
            conv,
            street="85-й квартал",
            house="17",
            message_id=msg,
            detected_at=T0 + timedelta(days=1),
        )
        прежняя, причина = await clients_svc.auto_address_yields_to(s, card, новая)
        assert (прежняя.id if прежняя else None, причина) == (
            старая.id,
            clients_svc.YIELD_SAME_REPLY,
        )
        # Из другой реплики, обе подтверждены — два адреса, решает оператор.
        новая.message_id = uuid.uuid4()
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        # Прежнюю карта отказала, новая — ДРУГОЕ место из другой реплики:
        # между местами карточка автоматикой не переезжает (18.09), вторая
        # строка показывается «Также назван».
        старая.geo_status = g.GEO_STREET_MISMATCH
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        # Переходное состояние — вердикт сброшен (дописан пункт), карта не
        # ответила, город неизвестен: строка ещё проверяется, уступать рано —
        # иначе верный авто-адрес заменялся бы и отказывался без человека.
        for переходный in (g.GEO_PENDING, None, g.GEO_ERROR, g.GEO_BLOCKED, g.GEO_NO_CITY):
            старая.geo_status = переходный
            assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None), (
                переходный
            )
        старая.geo_status = g.GEO_STREET_MISMATCH
        # Подтверждено кнопкой — слова человека, не уступает.
        старая.resolved_by_id = uuid.uuid4()
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        старая.resolved_by_id = None
        # Набрано руками — не уступает ничему.
        card.address_set_at = datetime.now(UTC)
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        card.address_set_at = None
        # Место уступает дому — как в автозаписи с 13.09.
        старая.kind = ap.KIND_PLACE
        assert (await clients_svc.auto_address_yields_to(s, card, новая))[1] == (
            clients_svc.YIELD_PLACE_TO_HOUSE
        )
        старая.kind = ap.KIND_HOUSE
        # Семья дома («17» и «17 к 1») — то же место: прежняя без точки
        # (отказ с текстом карты — степень `text`) уступает точному дому по
        # лестнице; обе с точкой дома — уточнение семьи (замер 18.09: 8/60
        # exact — «17» → «17 А»).
        семья = await _строка_карты(
            s, card.id, conv, street="ул Гагарина", house="17 к 1", message_id=uuid.uuid4()
        )
        assert (await clients_svc.auto_address_yields_to(s, card, семья))[1] == (
            clients_svc.YIELD_GRADE_UP
        )
        старая.geo_status = g.GEO_EXACT
        assert (await clients_svc.auto_address_yields_to(s, card, семья))[1] == (
            clients_svc.YIELD_FAMILY_REFINED
        )
        # «17 к 2» при «17 к 1» — спор корпусов, не уточнение: не уступает.
        семья.house = "17 к 2"
        старая.house = "17 к 1"
        assert await clients_svc.auto_address_yields_to(s, card, семья) == (None, None)
        старая.house = "17"
        # Новая строка без подтверждения карты — не уступает.
        новая.geo_status = g.GEO_PENDING
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        await s.rollback()


# ── 5. ревью 18.09: речь после адреса, имена людей, пункт с частями, pending ──


@pytest.mark.parametrize(
    "реплика",
    [
        "Всё в Порядке, ждём",
        "В Ленте купил запчасть",
        "Работаю в Газпроме, звоните после 18",
        "Мы в Силе?",
    ],
)
async def test_речь_за_в_после_адреса_не_трогает_строку(
    db, redis, account, db_sessionmaker, реплика: str
):
    """«ул Ленина 5» (exact, авто в карточке) → «в <Слово>» в речи без вопроса
    оператора об адресе: пункт не дописывается, вердикт не сбрасывается, к
    карте строка заново не идёт. Стоп-список слов за «в» конечен, а бренды,
    учреждения и обороты — нет; привязка к вопросу об адресе — сторож."""
    await _диалог(db, redis, account, ["ул Ленина 5"])
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, строка.id, "ул Ленина, 5, Саранск")
    await redis.delete(f"arq:job:geocode:{строка.id}")
    await _исходящее(db_sessionmaker, "Хорошо, мастер выезжает", T0 + timedelta(seconds=30))
    await apply_inbound_event(
        db, redis, account, событие(реплика, msg="m1", when=T0 + timedelta(minutes=1))
    )
    (после,) = await _строки(db_sessionmaker)
    assert (после.settlement, после.locality, после.raw) == (None, None, "ул Ленина 5")
    assert (после.geo_status, после.geo_formatted) == (g.GEO_EXACT, "ул Ленина, 5, Саранск")
    assert not await redis.exists(f"arq:job:geocode:{после.id}")


async def test_в_пункте_в_ответ_на_вопрос_об_адресе_дописывается(
    db, redis, account, db_sessionmaker
):
    """А «в <Имя>» в ответ на вопрос оператора об адресе — пункт: «Уточните
    адрес: какой посёлок?» → «Мы в Сукко, кв 5, 2 подъезд» — и пункт, и части
    в ту же строку, вердикт сброшен."""
    await _диалог(db, redis, account, ["ул Ленина 5"], город="anapa")
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, строка.id, "ул Ленина, 5, Анапа")
    await _исходящее(db_sessionmaker, "Уточните адрес: какой посёлок?", T0 + timedelta(seconds=30))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Мы в Сукко, кв 5, 2 подъезд", msg="m1", when=T0 + timedelta(minutes=1), город="anapa"
        ),
    )
    (после,) = await _строки(db_sessionmaker)
    assert (после.settlement, после.raw) == ("Сукко", "в Сукко; ул Ленина 5")
    assert (после.office, после.entrance) == ("5", "2")
    assert после.geo_status == g.GEO_PENDING and после.geo_formatted is None
    assert await redis.exists(f"arq:job:geocode:{после.id}")


async def test_подтверждённую_кнопкой_строку_пункт_не_сбрасывает(
    db, redis, account, db_sessionmaker
):
    """Подтверждено кнопкой — слова человека: «Ялга» после вопроса об адресе
    в строку не дописывается, вердикт не сбрасывается, координаты и строка
    карты остаются (как части у `refresh_auto_address`)."""
    await _диалог(db, redis, account, ["Мичурина 3 кв 27"])
    (строка,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(
        db_sessionmaker, строка.id, "ул Мичурина, 3, Саранск", кнопкой=True
    )
    await redis.delete(f"arq:job:geocode:{строка.id}")
    await _исходящее(db_sessionmaker, "Уточните адрес: какой посёлок?", T0 + timedelta(seconds=30))
    await apply_inbound_event(
        db, redis, account, событие("Ялга", msg="m1", when=T0 + timedelta(minutes=1))
    )
    (после,) = await _строки(db_sessionmaker)
    assert (после.settlement, после.raw) == (None, "Мичурина 3")
    assert (после.geo_status, после.geo_formatted) == (g.GEO_EXACT, "ул Мичурина, 3, Саранск")
    assert not await redis.exists(f"arq:job:geocode:{после.id}")


async def test_пункт_и_части_в_одной_реплике(db, redis, account, db_sessionmaker):
    """«ул Ленина 5» → «Ялга! кв 27, 2 подъезд»: и пункт, и части — в ту же
    строку. Ветка пункта возвращалась раньше ветки частей, и квартира с
    подъездом терялись (адрес по частям — 23,5 % адресных диалогов)."""
    await _диалог(db, redis, account, ["ул Ленина 5", "Ялга! кв 27, 2 подъезд"])
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.office, строка.entrance) == ("Ялга", "27", "2")
    assert строка.raw == "Ялга; ул Ленина 5"
    assert строка.geo_status == g.GEO_PENDING
    assert await redis.exists(f"arq:job:geocode:{строка.id}")


async def test_имя_из_обращения_оператора_не_пункт(db, redis, account, db_sessionmaker):
    """Оператор: «Здравствуйте, Ольга! Подскажите адрес» → «Ольга, сейчас
    уточню» → «ул Ленина 5» → «Ольга! Спасибо, ждём»: имя перед запятой —
    подсказка карте (`settlement_hints`), а не пункт строки; слово, которым
    оператор обратился к клиенту, — имя человека и восклицанием."""
    await _диалог(db, redis, account, ["Здравствуйте"])
    await _исходящее(
        db_sessionmaker, "Здравствуйте, Ольга! Подскажите адрес", T0 + timedelta(seconds=30)
    )
    for i, текст in enumerate(
        ["Ольга, сейчас уточню", "ул Ленина 5", "Ольга! Спасибо, ждём"], start=1
    ):
        await apply_inbound_event(
            db, redis, account, событие(текст, msg=f"n{i}", when=T0 + timedelta(minutes=i))
        )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.locality, строка.raw) == (None, None, "ул Ленина 5")


async def test_имя_клиента_и_подпись_оператора_не_пункт(db, redis, account, db_sessionmaker):
    """Клиент по карточке — «Ольга»: «Ольга! Спасибо» — имя, не пункт. Подпись
    оператора («Наталья, сервис») — тоже: «Наталья! Спасибо!» после адреса
    пунктом не становится. А слово из вопроса оператора, которое есть в
    справочнике городов («Люберцы или Томилино?» → «Люберцы»), — город."""
    await _диалог(db, redis, account, ["ул Ленина 5"], имя="Ольга")
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Ольга! Спасибо", msg="m1", when=T0 + timedelta(minutes=1), имя="Ольга"),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.raw) == (None, "ул Ленина 5")
    await _исходящее(db_sessionmaker, "Наталья, сервис. Когда удобно?", T0 + timedelta(minutes=2))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Наталья! Спасибо!", msg="m2", when=T0 + timedelta(minutes=3), имя="Ольга"),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.raw) == (None, "ул Ленина 5")
    await _исходящее(
        db_sessionmaker, "Уточните адрес: Люберцы или Томилино?", T0 + timedelta(minutes=4)
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Люберцы", msg="m3", when=T0 + timedelta(minutes=5), имя="Ольга"),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.locality, строка.raw) == ("Люберцы", "Люберцы; ул Ленина 5")


def test_имя_перед_запятой_не_пункт_реплики() -> None:
    """`settlement_hints` отдаёт «Ольга» из «Ольга, сейчас уточню» подсказкой
    для сверки с картой; в строку адреса такое имя идти не должно."""
    assert inbound._пункт_в_реплике("Ольга, сейчас уточню") is None
    assert inbound._пункт_в_реплике("Пашковка, Ореховая") is None
    assert inbound._пункт_в_реплике("Новое заозерье") == inbound.ПунктКонтекста(
        "Новое заозерье", None, "Новое заозерье", inbound.ФОРМА_РЕПЛИКА
    )
    assert inbound._пункт_в_реплике("д. Малая Сосновка") == inbound.ПунктКонтекста(
        "Малая Сосновка", None, "Малая Сосновка", inbound.ФОРМА_РЕПЛИКА
    )
    # Реплика-место с районом и массивом — пункт с типом из `parse_place`.
    место = inbound._пункт_в_реплике("Гатчинский р-н. Д. Малая Сосновка, массив Южный")
    assert место is not None and (место.имя, место.тип, место.форма) == (
        "Малая Сосновка",
        "деревня",
        inbound.ФОРМА_РЕПЛИКА,
    )


@pytest.mark.parametrize(
    ("реплика", "город"),
    [
        ("Мы в Туле, район центра", "tula"),
        ("Живём в Уфе, рядом с вокзалом", "ufa"),
        ("Мы в Чите", "chita"),
        ("В Орле, центр", "orel"),
        ("Мы в Ельце", "elets"),
        ("В Бору, у моста", "bor"),
        ("Мы в Перми", "perm"),
        ("В Челнах, новый город", "naberezhnye_chelny"),
    ],
)
async def test_короткий_город_объявления_в_падеже_не_пункт(
    db, redis, account, db_sessionmaker, реплика: str, город: str
):
    """«в Туле» при объявлении в Туле — тот же город, а не пункт: у имён из
    3–4 букв падеж меняет последнюю букву, и сравнение первых четырёх
    сравнивало слово целиком («туле» ≠ «тула»)."""
    await _диалог(db, redis, account, [реплика, "ул Ленина 5"], город=город)
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.settlement, строка.locality, строка.raw) == (None, None, "ул Ленина 5")


def test_город_объявления_в_любом_падеже() -> None:
    assert inbound._тот_же_город("Туле", "Тула")
    assert inbound._тот_же_город("Саранске", "Саранск")
    assert inbound._тот_же_город("Перми", "Пермь")
    assert inbound._тот_же_город("Орле", "Орёл")
    assert inbound._тот_же_город("Ельце", "Елец")
    assert inbound._тот_же_город("Бору", "Бор")
    assert inbound._тот_же_город("Новгороде", "Великий Новгород")
    assert inbound._тот_же_город("Ростове", "Ростов-на-Дону")
    assert inbound._тот_же_город("Грозном", "Грозный")
    assert inbound._тот_же_город("Гае", "Гай")
    assert inbound._тот_же_город("Посаде", "Сергиев Посад")
    # Другой пункт с тем же началом — не город объявления.
    assert not inbound._тот_же_город("Борисоглебске", "Бор")
    assert not inbound._тот_же_город("Уфимске", "Уфа")
    assert not inbound._тот_же_город("Ялга", "Саранск")
    assert not inbound._тот_же_город("Сукко", "Анапа")
