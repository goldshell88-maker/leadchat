"""Пакет 3 (19.09, день): три случая владельца с фактами по бою.

C3 «по адресу 12б-73 на 10.30» — объявление в Саранске, клиент по другому
   диалогу из Нефтеюганска: пара «микрорайон-дом» разбирается по городу
   другого диалога клиента, он же — город клиента; воркер принимает город
   клиента из другого региона, если это город его диалога.
C4 «Веселого, 5» в Старом Осколе: слово-улица отдельной репликой — не
   пункт (та же улица; родительный падеж — фамилия улицы); Яндекс и OSM
   отдают дом с «микрорайон Весёлого» и пустой улицей — улица сошлась.
C5 «Камера 4G» — автопринятая строка старого разбора в карточке: один
   предикат «реплика без адреса по нынешнему разбору», `address-reparse
   --auto`, сторож автозаписи, номер клиента у точки улицы/массива.

ДИВЕРСИИ (каждая обязана краснеть): убрать ветку `else` пары в
`_maybe_extract_address` → C3 без строки; убрать `or await
_город_клиента_по_другим_диалогам` → город карты Саранск; убрать
`_та_же_улица` → «Гагарина» пунктом; убрать `_фамилия_улицы` → «Толстого»
пунктом; убрать ветку `_улица_как_массив` → street_mismatch; убрать
`_похож_на_дом` → точка улицы с «4G»; убрать `_отсеять_речь` → «Камера 4G»
в карточке; убрать `--auto` → строка остаётся принятой; убрать `if dry_run:
continue` в `_reparse_auto_accepted` → сухой прогон шлёт UPDATE/INSERT;
потерять `auto=auto` в обёртке typer → флаг не доходит до `run_*`.

Ревью 19.09 (C5, C9–C13, D2–D4), диверсии: вернуть в `_похож_на_дом`
`re.fullmatch(_ДОМ)` → «12, корпус 2» без точки улицы; вернуть модели свой
регэксп дома → «4G»/«1000» приняты; убрать `elif отклонённые` в
`autofill_address` → отклонение без кадра; убрать `voice.load_message` из
`_отсеять_речь` → чтение по одному id; вернуть `про_адрес=True` форме города
в `разбор_реплики_сейчас` → «10-12» адрес без вопроса; убрать
`строка.source = source` при переносе → строка модели отклонена как речь;
убрать `_фамилия_улицы` у формы «фраза» или `is_street_surname` из
`settlement_hints` → «Веселого» пунктом/подсказкой; вернуть `_слово_есть` в
`_место_улицей` → «Северная» сошлась с «мкр Северный»; убрать разбор пары из
`_город_пары_из_других_диалогов` → «2-3 дня» ходит в базу.

Телефонов и имён клиентов в тестах нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.integrations.avito.listing_url import City
from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING, CANDIDATE_REJECTED
from app.services import address_parse as ap
from app.services import app_settings, inbound
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services.conversations import other_conversation_cities
from app.services.inbound import apply_inbound_event
from app.workers import geocode as worker

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 111222919
AUTHOR_ID = 999019
T0 = datetime(2026, 9, 19, 9, 0, 0, tzinfo=UTC)
ОРСК = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")
СТАРЫЙ_ОСКОЛ = City("Старый Оскол", "Белгородская область", "Europe/Moscow")


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


async def _исходящее(db_sessionmaker, chat: str, текст: str, when: datetime) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == chat))
        ).scalar_one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"out-{chat}-{when.timestamp()}",
                direction="out",
                sender_type="user",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def _другой_диалог(
    db, redis, account, db_sessionmaker, *, город: str, chat: str, when: datetime
) -> None:  # noqa: ANN001
    """Диалог того же клиента по объявлению в другом городе — и закрыт."""
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Здравствуйте, нужен мастер", msg=f"m-{chat}", when=when, город=город, chat=chat),
    )
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == chat))
        ).scalar_one()
        conv.status = "closed"
        await s.commit()


# ── C3. пара по городу другого диалога клиента ──────────────────────────────


async def test_пара_разбирается_по_квартальному_городу_другого_диалога(
    db, redis, account, db_sessionmaker
):
    """Бой 19.09: «по адресу 12б-73 на 10.30» в диалоге из Саранска; у того же
    клиента закрытый диалог из Нефтеюганска → строка «12б мкр, 73», город
    клиента — Нефтеюганск, цитата — пара."""
    await _другой_диалог(
        db,
        redis,
        account,
        db_sessionmaker,
        город="nefteyugansk",
        chat="c-neft",
        when=T0 - timedelta(days=2),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("по адресу 12б-73 на 10.30", msg="m1", when=T0, город="saransk", chat="c-sar"),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.value) == ("12б мкр", "73", "12б мкр, 73")
    assert (строка.locality, строка.settlement, строка.level) == ("Нефтеюганск", None, "B")
    assert строка.raw == "12б-73"
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == "c-sar"))
        ).scalar_one()
        assert conv.item_city_slug == "saransk"
        assert строка.conversation_id == conv.id


@pytest.mark.parametrize(
    ("другой_город", "реплика"),
    [
        (None, "по адресу 12б-73 на 10.30"),  # других диалогов нет
        ("kazan", "по адресу 12б-73 на 10.30"),  # другой диалог не в квартальном городе
        ("nefteyugansk", "10-12"),  # пара «как время» без вопроса об адресе
    ],
    ids=["нет_диалогов", "неквартальный", "время"],
)
async def test_пара_без_квартального_города_клиента_не_адрес(
    db, redis, account, db_sessionmaker, другой_город: str | None, реплика: str
):
    if другой_город is not None:
        await _другой_диалог(
            db,
            redis,
            account,
            db_sessionmaker,
            город=другой_город,
            chat="c-other",
            when=T0 - timedelta(days=2),
        )
    await apply_inbound_event(
        db, redis, account, событие(реплика, msg="m1", when=T0, город="saransk", chat="c-sar")
    )
    assert await _строки(db_sessionmaker) == []


async def test_первый_подходящий_город_по_свежести(db, redis, account, db_sessionmaker):
    """Два квартальных города у клиента: пара берётся по более свежему
    диалогу — Нефтеюганск (мкр) свежее Ангарска (квартал)."""
    await _другой_диалог(
        db,
        redis,
        account,
        db_sessionmaker,
        город="angarsk",
        chat="c-ang",
        when=T0 - timedelta(days=3),
    )
    await _другой_диалог(
        db,
        redis,
        account,
        db_sessionmaker,
        город="nefteyugansk",
        chat="c-neft",
        when=T0 - timedelta(days=1),
    )
    await apply_inbound_event(
        db, redis, account, событие("12б-73", msg="m1", when=T0, город="saransk", chat="c-sar")
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.locality) == ("12б мкр", "Нефтеюганск")
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == "c-sar"))
        ).scalar_one()
        города = await other_conversation_cities(s, conv.client_id, conv.id)
    assert [c.name for c in города] == ["Нефтеюганск", "Ангарск"]


async def test_города_других_диалогов_без_чужих_слагов_и_повторов(
    seed_conversation, db_sessionmaker
):
    """Слаг, которого справочник не знает, — не город; один город дважды — один раз."""
    async with db_sessionmaker() as s:
        for слаг, когда in (("orsk", 1), ("net_takogo_goroda", 2), ("orsk", 3), ("saransk", 4)):
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id=f"chat-{слаг}-{когда}",
                    account_id=seed_conversation.account.id,
                    client_id=seed_conversation.client_id,
                    status="closed",
                    item_city_slug=слаг,
                    last_message_at=T0 - timedelta(days=когда),
                )
            )
        await s.commit()
        города = await other_conversation_cities(
            s, seed_conversation.client_id, seed_conversation.conversation_id
        )
    assert [c.name for c in города] == ["Орск", "Саранск"]


@pytest.fixture
def карта(monkeypatch):  # noqa: ANN001
    """Карта отвечает домом в Орске; запросы считаются — важен город запроса."""
    вызовы: list[g.Query] = []

    async def search(query, wait=None, **kw):  # noqa: ANN001
        вызовы.append(query)
        if wait is not None:
            await wait()
        return [
            g.GeoHit(
                street="улица Ленина",
                house="5",
                settlement=None,
                city="Орск",
                region="Оренбургская область",
                lat=51.2,
                lon=58.5,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


def ctx(db_sessionmaker, redis) -> dict:  # noqa: ANN001
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def _строка_клиента(
    seed, db_sessionmaker, *, слаг: str, тело: str, found: ap.Found, **поля: Any
) -> uuid.UUID:  # noqa: ANN001
    """Строка адреса из реплики с этим телом — реплика и есть адрес."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        conv.item_city_slug = слаг
        card = await s.get(Client, seed.client_id)
        card.address = None
        msg = Message(
            conversation_id=conv.id,
            external_message_id=f"in-{uuid.uuid4().hex[:8]}",
            direction="in",
            sender_type="client",
            body=тело,
            attachments=[],
            delivery_status="delivered",
            created_at=T0,
        )
        s.add(msg)
        await s.flush()
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=msg.id,
            message_at=msg.created_at,
            found=found,
            now=T0,
        )
        assert записано.candidate_id is not None
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
        for k, v in поля.items():
            setattr(row, k, v)
        await s.commit()
        return записано.candidate_id


@pytest.mark.parametrize("другой_диалог", [True, False], ids=["город_клиента", "чужой_город"])
async def test_воркер_принимает_город_клиента_из_другого_региона_по_его_диалогу(
    seed_conversation, db_sessionmaker, redis, карта, другой_диалог: bool
):
    """Строка с городом клиента Нефтеюганск при объявлении в Саранске: карту
    спрашивают по Нефтеюганску, если у клиента есть диалог оттуда; без него
    другой регион не подменяет город объявления (как до 19.09)."""
    cid = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="saransk",
        тело="12б-73",
        found=ap.Found(street="12б мкр", house="73", raw="12б-73", start=0, end=6, level="B"),
        locality="Нефтеюганск",
    )
    if другой_диалог:
        async with db_sessionmaker() as s:
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id="chat-neft",
                    account_id=seed_conversation.account.id,
                    client_id=seed_conversation.client_id,
                    status="closed",
                    item_city_slug="nefteyugansk",
                    last_message_at=T0 - timedelta(days=2),
                )
            )
            await s.commit()
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert карта and карта[0].city == ("Нефтеюганск" if другой_диалог else "Саранск")
    assert карта[0].region == ("ХМАО — Югра" if другой_диалог else "Республика Мордовия")


# ── C4. «Веселого» одним словом — улица, не пункт ──────────────────────────


async def test_слово_улицы_перед_адресом_не_пункт(db, redis, account, db_sessionmaker):
    """Бой 19.09, Старый Оскол: вопрос → «Веселого» → вопрос → «Веселого 5
    кв 41 2 подъезд». Одна строка, пункта нет, цитата — только адрес."""
    chat = "c-oskol"
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Здравствуйте", msg="m0", when=T0, город="staryy_oskol", chat=chat),
    )
    await _исходящее(db_sessionmaker, chat, "Подскажите адрес", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Веселого", msg="m1", when=T0 + timedelta(minutes=2), город="staryy_oskol", chat=chat
        ),
    )
    await _исходящее(
        db_sessionmaker, chat, "Напишите полный адрес: улица, дом", T0 + timedelta(minutes=3)
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Веселого 5 кв 41 2 подъезд",
            msg="m2",
            when=T0 + timedelta(minutes=4),
            город="staryy_oskol",
            chat=chat,
        ),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.settlement, строка.locality) == (
        "Веселого",
        "5",
        None,
        None,
    )
    assert строка.raw == "Веселого 5"
    assert (строка.office, строка.entrance) == ("41", "2")


async def test_та_же_улица_одним_словом_не_пункт_без_родительного_падежа(
    db, redis, account, db_sessionmaker
):
    """«Гагарина» — не родительный на -ого/-его: пунктом его не пускает
    только сторож «та же улица, что у найденного адреса»."""
    chat = "c-gag"
    await apply_inbound_event(
        db, redis, account, событие("Здравствуйте", msg="m0", when=T0, город="orsk", chat=chat)
    )
    await _исходящее(db_sessionmaker, chat, "Подскажите адрес", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Гагарина", msg="m1", when=T0 + timedelta(minutes=2), город="orsk", chat=chat),
    )
    await _исходящее(db_sessionmaker, chat, "Дом какой?", T0 + timedelta(minutes=3))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Гагарина 12", msg="m2", when=T0 + timedelta(minutes=4), город="orsk", chat=chat),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.house, строка.settlement, строка.raw) == (
        "Гагарина",
        "12",
        None,
        "Гагарина 12",
    )


@pytest.mark.parametrize(
    ("слово", "пункт"),
    [("Толстого", None), ("Достоевского", None), ("Кедровка", "Кедровка"), ("Ялга", "Ялга")],
)
async def test_родительный_падеж_отдельной_репликой_не_пункт(
    db, redis, account, db_sessionmaker, слово: str, пункт: str | None
):
    """Голое слово на -ого/-его в ответ на вопрос об адресе — фамилия улицы,
    не пункт, и для ДРУГОЙ улицы тоже; «Ялга», «Кедровка» — пункты как были.
    Проверяются обе стороны: слово до адреса и слово после него."""
    chat = "c-gen"
    await apply_inbound_event(
        db, redis, account, событие("Здравствуйте", msg="m0", when=T0, город="saransk", chat=chat)
    )
    await _исходящее(db_sessionmaker, chat, "Подскажите адрес", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(слово, msg="m1", when=T0 + timedelta(minutes=2), город="saransk", chat=chat),
    )
    await _исходящее(db_sessionmaker, chat, "Улица и дом?", T0 + timedelta(minutes=3))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "ул Ленина 5", msg="m2", when=T0 + timedelta(minutes=4), город="saransk", chat=chat
        ),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.street, строка.settlement) == ("ул Ленина", пункт)
    # После адреса — тот же сторож: пункт дописывается, фамилия улицы — нет.
    await _исходящее(
        db_sessionmaker, chat, "Уточните адрес: какой посёлок?", T0 + timedelta(minutes=5)
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(слово, msg="m3", when=T0 + timedelta(minutes=6), город="saransk", chat=chat),
    )
    (после,) = await _строки(db_sessionmaker)
    assert после.settlement == пункт


def test_фамилия_улицы_и_та_же_улица_как_сторожа() -> None:
    assert inbound._фамилия_улицы("Веселого") and inbound._фамилия_улицы("Весёлого")
    assert inbound._фамилия_улицы("Достоевского")
    assert not inbound._фамилия_улицы("Ялга") and not inbound._фамилия_улицы("Гагарина")
    assert not inbound._фамилия_улицы("Ново-Переделкино")
    assert inbound._реплика_целиком_пункт("Веселого") is None
    assert inbound._реплика_целиком_пункт("Ялга") is not None
    # С типом — пункт остаётся: тип назвал сам клиент.
    assert inbound._реплика_целиком_пункт("п. Веселого") is not None
    assert inbound._та_же_улица("Веселого", "ул. Весёлого")
    assert inbound._та_же_улица("Лесная", "ул Лесная")
    assert not inbound._та_же_улица("Ялга", "ул Садовая")
    assert not inbound._та_же_улица("Ленина", None)


# ── C4б. вердикт: улица клиента — микрорайон у карты ────────────────────────


def _оскол(**over: Any) -> g.GeoHit:
    """Ответ Яндекса/OSM на «Веселого 5» — как записан 19.09."""
    база: dict[str, Any] = {
        "street": None,
        "house": "5",
        "settlement": "микрорайон Весёлого",
        "city": "Старый Оскол",
        "region": "Белгородская область",
        "lat": 51.327295,
        "lon": 37.869643,
        "house_level": True,
    }
    база.update(over)
    return g.GeoHit(**база)


def test_улица_клиента_сошлась_с_микрорайоном_карты_без_улицы() -> None:
    parsed = g.Parsed(street="Веселого", house="5")
    статус, hit = g.verdict(parsed, СТАРЫЙ_ОСКОЛ, [_оскол()])
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, parsed) == "микрорайон Весёлого, 5, Старый Оскол"
    # Массив вместо пункта — то же.
    статус, hit = g.verdict(
        parsed, СТАРЫЙ_ОСКОЛ, [_оскол(settlement=None, area="микрорайон Весёлого")]
    )
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, parsed) == "микрорайон Весёлого, 5, Старый Оскол"
    # С типом улицы у клиента — тоже (люди пишут «ул» ко всему).
    assert (
        g.verdict(g.Parsed(street="ул Веселого", house="5"), СТАРЫЙ_ОСКОЛ, [_оскол()])[0]
        == g.GEO_EXACT
    )


@pytest.mark.parametrize(
    ("parsed", "hit"),
    [
        # Пункт твёрдого типа при улице клиента с тем же словом — как раньше.
        (
            g.Parsed(street="Сосново", house="9"),
            _оскол(house="9", settlement="посёлок Сосново"),
        ),
        # Микрорайон с другими словами.
        (g.Parsed(street="Веселого", house="5"), _оскол(settlement="микрорайон Северный")),
        # Улица у карты есть и другая — микрорайон её не заменяет.
        (
            g.Parsed(street="Веселого", house="5"),
            _оскол(street="улица Ленина"),
        ),
        # Слово клиента длиннее слов места.
        (g.Parsed(street="Маршала Веселого", house="5"), _оскол()),
    ],
    ids=["посёлок", "другой_мкр", "улица_есть", "лишнее_слово"],
)
def test_микрорайон_не_улица_когда_слова_не_те(parsed: g.Parsed, hit: g.GeoHit) -> None:
    assert g.verdict(parsed, СТАРЫЙ_ОСКОЛ, [hit]) == (g.GEO_STREET_MISMATCH, None)


def test_дом_по_пункту_без_улицы_как_раньше() -> None:
    """«посёлок Сосново, 9» — улицы нет ни у клиента, ни у карты: пункт сверен, exact."""
    parsed = g.Parsed(street="", house="9", settlement="Сосново", settlement_type="посёлок")
    статус, _ = g.verdict(parsed, СТАРЫЙ_ОСКОЛ, [_оскол(house="9", settlement="посёлок Сосново")])
    assert статус == g.GEO_EXACT


# ── C5. «реплика без адреса по нынешнему разбору» ───────────────────────────


def _msg(тело: str | None, *, attachments: list[Any] | None = None, when: datetime = T0) -> Message:
    return Message(
        conversation_id=uuid.uuid4(),
        external_message_id="x",
        direction="in",
        sender_type="client",
        body=тело,
        attachments=attachments or [],
        delivery_status="delivered",
        created_at=when,
    )


ГЕОТОЧКА = {"avito_type": "location", "address": "Орск, улица Ленина, 5", "lat": 51.2, "lon": 58.5}


@pytest.mark.parametrize(
    ("тело", "вложения", "без_адреса"),
    [
        ("Камера 4G", None, True),
        ("сделать 130", None, True),
        ("Здравствуйте! Экран разбит, почём?", None, True),
        ("ул Ленина 5", None, False),
        ("дом 9", None, False),  # продолжение места из прошлой реплики
        ("д.16, кв 3", None, False),
        ("", None, False),  # тела нет — судить нечего
        (None, None, False),
        ("", [ГЕОТОЧКА], False),  # геоточка — адрес во вложении
        ("Камера 4G", [ГЕОТОЧКА], False),
    ],
)
async def test_предикат_реплика_без_адреса(
    seed_conversation,
    db_sessionmaker,
    тело: str | None,
    вложения: list[Any] | None,
    без_адреса: bool,
):
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        msg = _msg(тело, attachments=вложения)
        found = await inbound.разбор_реплики_сейчас(
            s, conv, msg, client_id=seed_conversation.client_id
        )
        assert inbound.реплика_без_адреса(msg, found) is без_адреса


async def test_предикат_учитывает_вопрос_оператора_город_и_чужой_город(
    seed_conversation, db_sessionmaker
):
    """«петрова 17» строчными — адрес только в ответ на вопрос (уровень C
    невода — уже адрес, как в догоне); «85-11» — в Ангарске; «12б-73» — при
    диалоге клиента из Нефтеюганска (C3)."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "saransk"
        cid = seed_conversation.client_id

        async def без_адреса(тело: str, when: datetime = T0) -> bool:
            msg = _msg(тело, when=when)
            return inbound.реплика_без_адреса(
                msg, await inbound.разбор_реплики_сейчас(s, conv, msg, client_id=cid)
            )

        assert await без_адреса("петрова 17")
        assert not await без_адреса("Садовая 24-31")  # невод C — адрес
        assert await без_адреса("85-11") and await без_адреса("12б-73")
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="q1",
                direction="out",
                sender_type="user",
                body="Куда подъехать, адрес?",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 - timedelta(minutes=5),
            )
        )
        await s.flush()
        assert not await без_адреса("петрова 17")
        conv.item_city_slug = "angarsk"
        assert not await без_адреса("85-11")
        conv.item_city_slug = "saransk"
        assert await без_адреса("12б-73")
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="chat-neft-pred",
                account_id=seed_conversation.account.id,
                client_id=cid,
                status="closed",
                item_city_slug="nefteyugansk",
                last_message_at=T0 - timedelta(days=2),
            )
        )
        await s.flush()
        assert not await без_адреса("12б-73")


# ── C5.2. address-reparse --auto ────────────────────────────────────────────


async def _речь_в_карточке(seed_conversation, db_sessionmaker, *, человеком: bool = False):  # noqa: ANN001
    """Автопринятая строка старого разбора («Камера 4G» → «Камера, 4G») держит
    карточку; рядом нерешённая годная строка «ул Ленина 5» с точкой дома."""
    речь = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="Камера 4G",
        found=ap.Found(street="Камера", house="4G", raw="Камера 4G", start=0, end=9, level="C"),
        status=CANDIDATE_ACCEPTED,
        resolved_at=T0,
        geo_status=g.GEO_EXACT,
        geo_provider="dadata~approx",
        geo_formatted="ул Камера, 4G, Орск",
        geo_lat=51.2,
        geo_lon=58.5,
    )
    годная = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="ул Ленина 5",
        found=ap.Found(
            street="ул Ленина", house="5", raw="ул Ленина 5", start=0, end=11, level="A"
        ),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata",
        geo_formatted="улица Ленина, 5, Орск",
        geo_lat=51.21,
        geo_lon=58.51,
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, речь)
        if человеком:
            row.resolved_by_id = uuid.uuid4()
        card = await s.get(Client, seed_conversation.client_id)
        card.address = "ул Камера, 4G, Орск"
        card.address_candidate_id = речь
        card.address_value = row.value
        card.address_conversation_id = seed_conversation.conversation_id
        await s.commit()
    return речь, годная


async def test_reparse_auto_отклоняет_автопринятую_речь_и_пересобирает_карточку(
    seed_conversation, db_sessionmaker, redis, capsys, engine: AsyncEngine
):
    from app.cli import run_address_reparse

    речь, годная = await _речь_в_карточке(seed_conversation, db_sessionmaker)
    # Без `--auto` и в сухом прогоне — ничего не меняется, но счётчики видны.
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False, redis=redis)
    # Сухой прогон не пишет НИ ОДНОЙ строки — не только не commit'ит: правки
    # в сессии ушли бы в базу автосбросом перед следующим запросом и заперли
    # бы строки и карточки на весь обход (как у остальных `if not dry_run`
    # команды).
    записи: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            записи.append(statement.split()[0].upper())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with db_sessionmaker() as s:
            await run_address_reparse(s, days=30, dry_run=True, auto=True, redis=redis)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert записи == []
    вывод = capsys.readouterr().out
    assert "авто_строк=1 авто_отклонено=1 авто_карточек=1" in вывод
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_ACCEPTED
        assert (await s.get(Client, seed_conversation.client_id)).address == "ул Камера, 4G, Орск"
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    # Боевой прогон: строка отклонена, автозапись снята, воркер поставлен.
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False, auto=True, redis=redis)
    assert "авто_поставлено=1" in capsys.readouterr().out
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, речь)
        assert (row.status, row.resolved_by_id) == (CANDIDATE_REJECTED, None)
        assert row.resolved_at is not None
        card = await s.get(Client, seed_conversation.client_id)
        assert (card.address, card.address_candidate_id, card.address_value) == (None, None, None)
        журнал = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "client.address_edited")))
            .scalars()
            .all()
        )
        assert [ж.details.get("reason") for ж in журнал] == ["speech_by_current_parse"]
        assert журнал[0].user_id is None and журнал[0].details["previous"] == "ул Камера, 4G, Орск"
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    # Следующую годную строку кладёт воркер автозаписи — существующим путём.
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert (card.address, card.address_candidate_id) == ("улица Ленина, 5, Орск", годная)


async def test_обёртка_address_reparse_несёт_auto_и_по_умолчанию_выключена(monkeypatch):  # noqa: ANN001
    """`--auto` доходит от консоли до `run_address_reparse` и без флага False:
    обёртки в этом репозитории зовутся только через `run_*`, и потерянный
    параметр иначе не заметил бы никто (см. test_cli_wrappers)."""
    import typer.main

    from app import cli

    команда = typer.main.get_command(cli.app).commands["address-reparse"]  # type: ignore[attr-defined]
    (авто,) = [p for p in команда.params if p.name == "auto"]
    assert авто.default is False and "--auto" in авто.opts
    перехват: list[Any] = []
    вызовы: list[tuple[int, bool, bool]] = []

    async def _fake(db, *, days, dry_run, auto=False, redis=None):  # noqa: ANN001
        вызовы.append((days, dry_run, auto))

    monkeypatch.setattr(cli, "run_address_reparse", _fake)
    monkeypatch.setattr(cli, "_run", перехват.append)
    cli.address_reparse(days=30, dry_run=True, auto=True)
    cli.address_reparse(days=7, dry_run=False, auto=False)
    for main in перехват:
        await main(None)
    assert вызовы == [(30, True, True), (7, False, False)]


async def test_reparse_auto_не_трогает_принятое_человеком(
    seed_conversation, db_sessionmaker, redis
):
    from app.cli import run_address_reparse

    речь, _ = await _речь_в_карточке(seed_conversation, db_sessionmaker, человеком=True)
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=30, dry_run=False, auto=True, redis=redis)
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_ACCEPTED
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == речь


# ── C5.3. сторож автозаписи ─────────────────────────────────────────────────


async def test_автозапись_не_кладёт_речь_старого_разбора(seed_conversation, db_sessionmaker, redis):
    """Нерешённая строка «Камера, 4G» с точкой улицы: в карточку не идёт,
    отклоняется без человека и в журнале; годная строка следом — пишется."""
    речь = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="Камера 4G",
        found=ap.Found(street="Камера", house="4G", raw="Камера 4G", start=0, end=9, level="A"),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata~approx",
        geo_formatted="ул Камера, 4G, Орск",
        geo_lat=51.2,
        geo_lon=58.5,
    )
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, речь)
        assert (row.status, row.resolved_by_id) == (CANDIDATE_REJECTED, None)
        assert (await s.get(Client, seed_conversation.client_id)).address is None
    годная = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="ул Ленина 5",
        found=ap.Found(
            street="ул Ленина", house="5", raw="ул Ленина 5", start=0, end=11, level="A"
        ),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata",
        geo_formatted="улица Ленина, 5, Орск",
        geo_lat=51.21,
        geo_lon=58.51,
    )
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == годная
        assert (await s.get(ClientAddressCandidate, годная)).status == CANDIDATE_ACCEPTED


@pytest.fixture
def кадры(monkeypatch):  # noqa: ANN001
    """Кадры `client:updated`, которые воркер публикует через `_известить`."""
    вышло: list[dict[str, Any]] = []

    async def publish_event(redis, kind, data):  # noqa: ANN001
        вышло.append({"kind": kind, **data})

    monkeypatch.setattr(worker, "publish_event", publish_event)
    return вышло


async def _речь_строкой(
    seed_conversation,  # noqa: ANN001
    db_sessionmaker,  # noqa: ANN001
    *,
    улица: str = "Камера",
    дом: str = "4G",
    **поля: Any,
) -> uuid.UUID:
    """Нерешённая строка речи старого разбора («Камера, 4G», «сделать, 130») с
    точкой улицы — видна оператору предложением. Улица и дом задают ключ:
    вторая строка с тем же ключом дописала бы первую, а не завелась."""
    return await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело=f"{улица} {дом}",
        found=ap.Found(street=улица, house=дом, raw=f"{улица} {дом}", start=0, end=9, level="A"),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata~approx",
        geo_formatted=f"ул {улица}, {дом}, Орск",
        geo_lat=51.2,
        geo_lon=58.5,
        **поля,
    )


async def _годная_строкой(seed_conversation, db_sessionmaker) -> uuid.UUID:  # noqa: ANN001
    return await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="ул Ленина 5",
        found=ap.Found(
            street="ул Ленина", house="5", raw="ул Ленина 5", start=0, end=11, level="A"
        ),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata",
        geo_formatted="улица Ленина, 5, Орск",
        geo_lat=51.21,
        geo_lon=58.51,
    )


async def _автозапись_включена(db_sessionmaker) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()


async def test_отклонение_речи_даёт_один_кадр_после_сессии(
    seed_conversation, db_sessionmaker, redis, кадры
):
    """Ревью 19.09 (C10/C12): `_отсеять_речь` commit'ит `rejected`, а кадра не
    было — экран держал снятое предложение до F5, клик давал 409. Теперь при
    исходе `skip` — ровно один кадр `client:updated` на диалог с причиной
    `address_candidate_rejected` (фронт перечитывает карточку по любой
    причине: `applyWsEvent` → `сбросКлиента`; реестра причин нет). Кадр —
    после выхода из сессии: `publish_event` подменён, строка к тому моменту
    уже в базе. Две строки речи — по-прежнему один кадр."""
    речь = await _речь_строкой(seed_conversation, db_sessionmaker)
    вторая = await _речь_строкой(seed_conversation, db_sessionmaker, улица="сделать", дом="130")
    assert вторая != речь
    await _автозапись_включена(db_sessionmaker)
    итог = await worker.autofill_address(
        ctx(db_sessionmaker, redis), seed_conversation.conversation_id
    )
    assert итог == "skip"
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_REJECTED
        assert (await s.get(ClientAddressCandidate, вторая)).status == CANDIDATE_REJECTED
    assert кадры == [
        {
            "kind": "client:updated",
            "client_id": str(seed_conversation.client_id),
            "conversation_id": str(seed_conversation.conversation_id),
            "reason": "address_candidate_rejected",
        }
    ]
    # Повторный прогон: отклонять больше нечего — кадра нет.
    кадры.clear()
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )
    assert кадры == []


async def test_запись_после_отклонения_речи_даёт_один_кадр_автозаписи(
    seed_conversation, db_sessionmaker, redis, кадры
):
    """Речь отклонена и годная строка записана одним прогоном: кадр один —
    `address_autofilled` (он и велит перечитать карточку), второго о снятом
    предложении не шлём."""
    речь = await _речь_строкой(seed_conversation, db_sessionmaker)
    годная = await _годная_строкой(seed_conversation, db_sessionmaker)
    await _автозапись_включена(db_sessionmaker)
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_REJECTED
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == годная
    assert [к["reason"] for к in кадры] == ["address_autofilled"]


async def test_сторож_речи_читает_сообщение_по_секции_времени(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Ревью 19.09 (C5): реплика строки читается `voice.load_message(id,
    message_at)` — в одну секцию `messages`, а не обходом всех секций по
    одному id; строка без `message_at` (старые) идёт прежней выборкой и
    судится так же."""
    вызовы: list[tuple[uuid.UUID, datetime]] = []
    настоящий = worker.voice.load_message

    async def load_message(db, message_id, created_at):  # noqa: ANN001
        вызовы.append((message_id, created_at))
        return await настоящий(db, message_id, created_at)

    monkeypatch.setattr(worker.voice, "load_message", load_message)
    с_временем = await _речь_строкой(seed_conversation, db_sessionmaker)
    без_времени = await _речь_строкой(
        seed_conversation, db_sessionmaker, улица="сделать", дом="130", message_at=None
    )
    assert без_времени != с_временем
    await _автозапись_включена(db_sessionmaker)
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )
    async with db_sessionmaker() as s:
        строка = await s.get(ClientAddressCandidate, с_временем)
        assert строка.status == CANDIDATE_REJECTED and строка.message_at is not None
        assert (await s.get(ClientAddressCandidate, без_времени)).status == CANDIDATE_REJECTED
        assert вызовы == [(строка.message_id, строка.message_at)]


async def test_автозапись_не_судит_строку_модели_и_дом_из_двух_реплик(
    seed_conversation, db_sessionmaker, redis
):
    """Строка модели-читателя и «дом 9» после места — не речь: кладутся."""
    from app.models.client import CANDIDATE_SOURCE_LLM

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    модель = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="Здравствуйте! Экран разбит, почём?",
        found=ap.Found(
            street="ул Ленина", house="5", raw="ул Ленина 5", start=0, end=11, level="A"
        ),
        source=CANDIDATE_SOURCE_LLM,
        geo_status=g.GEO_EXACT,
        geo_provider="dadata",
        geo_formatted="улица Ленина, 5, Орск",
        geo_lat=51.21,
        geo_lon=58.51,
    )
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == модель
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        card.address_candidate_id = None
        card.address_value = None
        row = await s.get(ClientAddressCandidate, модель)
        row.status = CANDIDATE_REJECTED
        row.resolved_by_id = uuid.uuid4()
        await s.commit()
    дом = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="дом 9",
        found=ap.Found(
            street="посёлок Сосново",
            house="9",
            raw="Поселок Сосново; дом 9",
            start=0,
            end=0,
            level="A",
        ),
        geo_status=g.GEO_EXACT,
        geo_provider="dadata",
        geo_formatted="посёлок Сосново, 9, Орск",
        geo_lat=51.3,
        geo_lon=58.6,
    )
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == дом


# ── C5.4. номер клиента у точки улицы и массива ─────────────────────────────


def _evidence(**kw: Any) -> g.Evidence:
    база: dict[str, Any] = {
        "status": g.GEO_NOT_FOUND,
        "parsed": g.Parsed(street="ул Ленина", house="5"),
        "city": ОРСК,
        "hits": [],
        "city_hits": [],
        "region_hits": [],
        "variants": [],
        "city_point": None,
        "hints": [],
    }
    база.update(kw)
    return g.Evidence(**база)


УЛИЦА = g.GeoHit(
    street="ул Ленина",
    house=None,
    settlement=None,
    city="Орск",
    region="Оренбургская область",
    lat=51.2,
    lon=58.5,
    house_level=False,
    precise=False,
)


@pytest.mark.parametrize(
    ("house", "правило"),
    [
        ("5", g.RULE_STREET_POINT),
        ("5а", g.RULE_STREET_POINT),
        ("12/3", g.RULE_STREET_POINT),
        # Дом из реплики «дом N» (`house_only`, голова `_НОМЕР_ДОМА`) и из
        # модели — те же дома (ревью 19.09: сторож по одной голове невода
        # отбирал у них точку улицы).
        ("12, корпус 2", g.RULE_STREET_POINT),
        ("12/2а", g.RULE_STREET_POINT),
        ("12, стр 3", g.RULE_STREET_POINT),
        ("5 к 2", g.RULE_STREET_POINT),
        ("17/2", g.RULE_STREET_POINT),
        ("3 стр 1", g.RULE_STREET_POINT),
        ("12а", g.RULE_STREET_POINT),
        ("273", g.RULE_STREET_POINT),
        ("4G", None),
        ("4K", None),
        ("3D", None),
        ("1000", None),
    ],
)
def test_точка_улицы_только_с_номером_похожим_на_дом(house: str, правило: str | None) -> None:
    e = _evidence(parsed=g.Parsed(street="ул Ленина", house=house), city_hits=[([УЛИЦА], "dadata")])
    решение = g.auto_decide(e)
    assert (решение.rule if решение is not None else None) == правило
    if решение is not None:
        assert решение.hit.house == house


@pytest.mark.parametrize(("house", "правило"), [("273", g.RULE_AREA_POINT), ("4G", None)])
def test_точка_массива_только_с_номером_похожим_на_дом(house: str, правило: str | None) -> None:
    parsed = g.Parsed(street="СНТ Урожай", house=house, settlement="Урожай", settlement_type="СНТ")
    место = g.Place(
        settlement=None, settlement_type=None, area="СНТ Урожай", district=None, street=""
    )
    массив = g.PlaceHit(
        name="СНТ Урожай",
        kind="area",
        settlement=None,
        area="СНТ Урожай",
        city=None,
        district="Оренбургский р-н",
        region="Оренбургская область",
        lat=51.6,
        lon=55.2,
    )
    e = _evidence(status=g.GEO_HOUSE_MISSING, parsed=parsed, place=место, place_hits=[массив])
    решение = g.auto_decide(e)
    assert (решение.rule if решение is not None else None) == правило


def test_похож_на_дом_читает_регэксп_разбора() -> None:
    """Одно определение дома на проект (`is_house_number`): обе головы разбора,
    латинская литера техники — нет, кириллица — да."""
    assert g._похож_на_дом("4к") and g._похож_на_дом("24дк6") and g._похож_на_дом("12 корпус 2")
    assert not g._похож_на_дом("4G") and not g._похож_на_дом("") and not g._похож_на_дом(None)
    assert ap.parse("Камера 4G") is None and ap.parse("Ленина 4к") is not None
    # Дом, рождённый `house_only`, проходит сторож слово в слово.
    for реплика in ("дом 12, корпус 2", "дом 12/2а", "д. 12, стр 3", "номер 8"):
        дом = ap.house_only(реплика)
        assert дом is not None and ap.is_house_number(дом), реплика
    # Дом, вернувшийся из головы невода, — тоже.
    for реплика in ("ул Ленина 5к2", "ул Ленина 3 стр 1", "ул Ленина 17/2", "Ленина 24дк6"):
        found = ap.parse(реплика)
        assert found is not None and ap.is_house_number(found.house), реплика
    assert not ap.is_house_number("17/2кв 4") and not ap.is_house_number("18 этажей")


def test_модель_читатель_судит_дом_тем_же_определением() -> None:
    """`address_llm.parse_reading` — тем же `is_house_number`: «12, корпус 2»
    и «12/2а» берутся, «4G» и четыре цифры — нет (у модели был свой регэксп,
    он пускал и то и другое)."""
    from app.services import address_llm

    def ответ(house: str) -> dict[str, Any]:
        return {
            "street": "улица Ленина",
            "house": house,
            "settlement": "",
            "settlement_type": "",
            "city": "",
            "apartment": "",
            "entrance": "",
            "floor": "",
            "intercom": "",
            "confidence": "high",
            "note": "",
        }

    for дом in ("12, корпус 2", "12/2а", "5 к 2", "273"):
        чтение = address_llm.parse_reading(ответ(дом))
        assert чтение is not None and чтение.house == дом, дом
    for дом in ("4G", "4K", "3D", "1000", "17/2кв 4"):
        assert address_llm.parse_reading(ответ(дом)) is None, дом


async def test_пенding_строки_без_речи_reparse_как_прежде(seed_conversation, db_sessionmaker):
    """Догон без `--auto` через предикат: речь с отказом карты отклоняется,
    «дом 9» — нет (как в стенде 14.09)."""
    from app.cli import run_address_reparse

    речь = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="Здравствуйте однушку надо сделать квартиру 32 квадрата сколько будет стоить",
        found=ap.Found(
            street="надо сделать квартиру",
            house="32",
            raw="надо сделать квартиру 32",
            start=0,
            end=1,
            level="A",
        ),
        geo_status=g.GEO_HOUSE_MISSING,
    )
    дом = await _строка_клиента(
        seed_conversation,
        db_sessionmaker,
        слаг="orsk",
        тело="дом 9",
        found=ap.Found(
            street="посёлок Сосново",
            house="9",
            raw="посёлок Сосново; дом 9",
            start=0,
            end=1,
            level="A",
        ),
        geo_status=g.GEO_HOUSE_MISSING,
    )
    async with db_sessionmaker() as s:
        await run_address_reparse(s, days=7, dry_run=False)
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_REJECTED
        assert (await s.get(ClientAddressCandidate, дом)).status == CANDIDATE_PENDING


# ── Ревью 19.09: C9, C13, D2, D3, D4 ────────────────────────────────────────


async def test_разбор_сейчас_повторяет_порядок_живого_пути_для_формы_города(
    seed_conversation, db_sessionmaker, monkeypatch
):
    """C9: «10-12» целиком репликой в Ангарске — без вопроса оператора не
    адрес (пара «как время»), после вопроса — адрес; `про_адрес` у формы
    города — только после вопроса, как на живом пути. Вопрос считается один
    раз на реплику (флаг переиспользуется), а не на каждую форму."""
    походов = 0
    настоящий = inbound._оператор_спросил_адрес

    async def _спросил(db, conv, *, before):  # noqa: ANN001
        nonlocal походов
        походов += 1
        return await настоящий(db, conv, before=before)

    monkeypatch.setattr(inbound, "_оператор_спросил_адрес", _спросил)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "angarsk"
        cid = seed_conversation.client_id
        msg = _msg("10-12")
        assert await inbound.разбор_реплики_сейчас(s, conv, msg, client_id=cid) is None
        assert походов == 1, "в ленту за вопросом — один раз на реплику"
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="q-ang",
                direction="out",
                sender_type="user",
                body="Подскажите адрес",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 - timedelta(minutes=5),
            )
        )
        await s.flush()
        походов = 0
        found = await inbound.разбор_реплики_сейчас(s, conv, msg, client_id=cid)
        assert found is not None and (found.street, found.house) == ("10 квартал", "12")
        assert походов == 1
        # «85-11» — адрес и без вопроса (85 — не час): в ленту — один раз, за
        # `про_адрес` невода (как на живом пути), а не второй раз за форму города.
        походов = 0
        found = await inbound.разбор_реплики_сейчас(s, conv, _msg("85-11"), client_id=cid)
        assert found is not None and found.house == "11" and походов == 1


@pytest.mark.parametrize(
    ("реплика", "в_базу"),
    [
        ("12б-73", True),
        ("10-12", True),  # «как время» целиком — решит вопрос оператора, после запроса
        ("2-3 дня", False),
        ("завтра 10-12", False),
        ("500-700 рублей", False),
        ("через 30-40 минут", False),
        ("19/09 в 10", False),
        ("38/07", True),
    ],
)
async def test_пара_идёт_в_базу_за_городом_только_если_разбор_пары_её_примет(
    seed_conversation, db_sessionmaker, monkeypatch, реплика: str, в_базу: bool
):
    """D4: `_город_пары_из_других_диалогов` ходит за диалогами клиента, только
    когда пара проходит `parse_quarter_pair`/`parse_complex_pair` (единица,
    цена, диапазон, дата режутся до запроса)."""
    походов: list[Any] = []

    async def other_conversation_cities(db, client_id, conversation_id):  # noqa: ANN001
        походов.append(conversation_id)
        return []

    monkeypatch.setattr(inbound, "other_conversation_cities", other_conversation_cities)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        await inbound._город_пары_из_других_диалогов(
            s,
            реплика,
            client_id=seed_conversation.client_id,
            conversation_id=conv.id,
            имя_города="Саранск",
        )
    assert bool(походов) is в_базу


def test_фамилия_улицы_восклицанием_и_в_подсказках_карте() -> None:
    """D2: одно правило (`address_parse.is_street_surname`) на форму
    «фраза» («Веселого! Жду» — не пункт, «Ялга! Жду» — пункт) и на подсказки
    карте (`settlement_hints` без «Веселого»; с типом — остаётся)."""
    assert inbound._пункт_в_реплике("Веселого! Жду") is None
    assert inbound._пункт_в_реплике("Толстого! Приезжайте") is None
    пункт = inbound._пункт_в_реплике("Ялга! Мы вам звонили!")
    assert пункт is not None and (пункт.имя, пункт.форма) == ("Ялга", inbound.ФОРМА_ФРАЗА)
    assert ap.settlement_hints(["Веселого", "Толстого.", "Ялга", "п. Веселого"]) == [
        "Ялга",
        "Веселого",
    ]
    assert ap.is_street_surname("Весёлого") and not ap.is_street_surname("Гагарина")
    assert inbound._фамилия_улицы("Веселого") is ap.is_street_surname("Веселого")


async def test_фамилия_улицы_восклицанием_после_адреса_не_пункт(
    db, redis, account, db_sessionmaker
):
    """D2, живой путь: «Веселого 5 кв 41 2 подъезд» → реплика оператора →
    «Веселого! Жду» — строка без пункта, вердикт не сброшен (до правки пункт
    «Веселого» дописывался формой «фраза», которой вопрос не нужен)."""
    chat = "c-oskol-fraza"
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Веселого 5 кв 41 2 подъезд", msg="m1", when=T0, город="staryy_oskol", chat=chat),
    )
    (строка,) = await _строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, строка.id)
        row.geo_status = g.GEO_EXACT
        row.geo_provider = "dadata"
        row.geo_formatted = "микрорайон Весёлого, 5, Старый Оскол"
        row.geo_lat, row.geo_lon = 51.32, 37.88
        await s.commit()
    await _исходящее(db_sessionmaker, chat, "Выезжаем, ждите", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Веселого! Жду",
            msg="m2",
            when=T0 + timedelta(minutes=2),
            город="staryy_oskol",
            chat=chat,
        ),
    )
    (после,) = await _строки(db_sessionmaker)
    assert (после.settlement, после.geo_status, после.raw) == (None, g.GEO_EXACT, "Веселого 5")
    # Пункт восклицанием как был: «Ялга! Жду» дописывается.
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "Ялга! Жду", msg="m3", when=T0 + timedelta(minutes=3), город="staryy_oskol", chat=chat
        ),
    )
    (с_пунктом,) = await _строки(db_sessionmaker)
    assert с_пунктом.settlement == "Ялга"


@pytest.mark.parametrize(
    ("улица", "место", "статус"),
    [
        ("Северная", "микрорайон Северный", g.GEO_STREET_MISMATCH),
        ("Строителей", "микрорайон Строитель", g.GEO_STREET_MISMATCH),
        ("Солнечная", "микрорайон Солнечный", g.GEO_STREET_MISMATCH),
        ("Веселого", "микрорайон Весёлого", g.GEO_EXACT),  # ё — та же буква
        ("ул Весёлого", "мкр Веселого", g.GEO_EXACT),
        ("Веселова", "микрорайон Весёлого", g.GEO_STREET_MISMATCH),  # опечатку не прощаем
    ],
)
def test_микрорайон_на_месте_улицы_сверяется_строго(улица: str, место: str, статус: str) -> None:
    """D3: между улицей клиента и микрорайоном карты слова сравниваются
    строго после `_норм` — без основ и опечаток («ул Северная» и «мкр
    Северный» — разные объекты одного города)."""
    parsed = g.Parsed(street=улица, house="5")
    assert g.verdict(parsed, СТАРЫЙ_ОСКОЛ, [_оскол(settlement=место)])[0] == статус
    assert g.verdict(parsed, СТАРЫЙ_ОСКОЛ, [_оскол(settlement=None, area=место)])[0] == статус


def _читатели(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations import gateway, openrouter

    assert gateway.known_keys is not None
    for имя in openrouter.READERS:
        monkeypatch.setitem(gateway.known_keys, имя, True)


async def _модель_прочла(
    monkeypatch, db_sessionmaker, redis, *, chat: str, msg: str, **чтение: str
) -> str:  # noqa: ANN001
    """Задача модели-читателя на реплике `msg` диалога `chat`; ответ модели —
    `чтение` (улица, дом, квартира…), карта и OpenRouter подменены."""
    from app.workers import address_llm as llm_worker

    async def chat_json(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        await kw["on_request"]()
        ответ = {
            "street": "",
            "house": "",
            "settlement": "",
            "settlement_type": "",
            "city": "",
            "apartment": "",
            "entrance": "",
            "floor": "",
            "intercom": "",
            "confidence": "high",
            "note": "",
        }
        ответ.update(чтение)
        return ответ, "b/two:free"

    async def enqueue_geocode(redis_, candidate_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(llm_worker.openrouter, "chat_json", chat_json)
    monkeypatch.setattr(llm_worker, "enqueue_geocode", enqueue_geocode)
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == chat))
        ).scalar_one()
        message = (
            await s.execute(sa.select(Message).where(Message.external_message_id == msg))
        ).scalar_one()
    return await llm_worker.llm_address_read(
        ctx(db_sessionmaker, redis), conversation_id=str(conv.id), message_id=str(message.id)
    )


async def _подтверждена_картой(db_sessionmaker, row_id: uuid.UUID) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, row_id)
        row.geo_status = g.GEO_EXACT
        row.geo_provider = "dadata"
        row.geo_formatted = "улица Ленина, 5, Орск"
        row.geo_lat, row.geo_lon = 51.21, 58.51
        await s.commit()


async def test_повтор_адреса_моделью_в_другом_диалоге_переносит_и_источник(
    db, redis, account, db_sessionmaker, monkeypatch, кадры
):
    """C13: строка правил из D1 («ул Ленина 5»), повтор в D2 прочитан моделью
    («тот же адрес ленина 5» — правила молчат): привязка переехала в D2 вместе
    с источником `llm`, и автозапись в D2 кладёт её в карточку, а не отклоняет
    как речь (со старым `inbound` сторож перечитал бы реплику D2 правилами)."""
    _читатели(monkeypatch)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "ул Ленина 5", msg="d1-m1", when=T0 - timedelta(hours=2), город="orsk", chat="c-d1"
        ),
    )
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.source, строка.raw) == ("inbound", "ул Ленина 5")
    await _подтверждена_картой(db_sessionmaker, строка.id)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("тот же адрес ленина 5", msg="d2-m1", when=T0, город="orsk", chat="c-d2"),
    )
    assert len(await _строки(db_sessionmaker)) == 1  # правила промолчали
    assert (
        await _модель_прочла(
            monkeypatch,
            db_sessionmaker,
            redis,
            chat="c-d2",
            msg="d2-m1",
            street="ленина",
            house="5",
        )
        == "recorded"
    )
    (перенесённая,) = await _строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        d2 = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == "c-d2"))
        ).scalar_one()
        m2 = (
            await s.execute(sa.select(Message).where(Message.external_message_id == "d2-m1"))
        ).scalar_one()
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    assert перенесённая.id == строка.id
    assert (перенесённая.conversation_id, перенесённая.message_id) == (d2.id, m2.id)
    assert (перенесённая.source, перенесённая.geo_status) == ("llm", g.GEO_EXACT)
    кадры.clear()
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), d2.id) == "filled"
    async with db_sessionmaker() as s:
        card = await s.get(Client, d2.client_id)
        assert card.address_candidate_id == строка.id and card.address is not None
        assert (await s.get(ClientAddressCandidate, строка.id)).status == CANDIDATE_ACCEPTED
    assert [к["reason"] for к in кадры] == ["address_autofilled"]


async def test_повтор_адреса_текстом_после_модели_возвращает_источник_правил(
    db, redis, account, db_sessionmaker, monkeypatch
):
    """C13, обратная сторона одного правила «источник едет за ссылкой»: строка
    модели из D1 («тот же адрес ленина 5»), повтор текстом в D2 («ул Ленина 5») прочли
    правила — привязка в D2, источник `inbound`; сторож речи такую строку
    перечитывает правилами и адрес видит."""
    _читатели(monkeypatch)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие(
            "тот же адрес ленина 5",
            msg="d1-m1",
            when=T0 - timedelta(hours=2),
            город="orsk",
            chat="c-d1",
        ),
    )
    assert await _строки(db_sessionmaker) == []
    assert (
        await _модель_прочла(
            monkeypatch,
            db_sessionmaker,
            redis,
            chat="c-d1",
            msg="d1-m1",
            street="ленина",
            house="5",
        )
        == "recorded"
    )
    (строка,) = await _строки(db_sessionmaker)
    assert строка.source == "llm"
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("ул Ленина 5", msg="d2-m1", when=T0, город="orsk", chat="c-d2"),
    )
    (после,) = await _строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        d2 = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == "c-d2"))
        ).scalar_one()
    assert после.id == строка.id and после.conversation_id == d2.id
    assert после.source == "inbound"
