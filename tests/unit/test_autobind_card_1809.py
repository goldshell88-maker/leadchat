"""Автопривязка адреса без человека — карточка, ворота, API (18.09, участок B).

Решение владельца: адрес привязывается к карточке автоматически, оператор
ничего не подтверждает — у него остаётся только «изменить» и «Не адрес».
Здесь стережётся всё, что решает СТЕПЕНЬ строки (`clients.candidate_grade`
→ `geocode.card_grade`, единственный судья): кто ложится в пустую карточку,
кому уступает заполненная (лестница text → approx → exact внутри одного
места, между местами — никогда, руками — никогда), как помнится отказ
(человек — целиком, автоматика — по степени), как решаются два адреса в
одном диалоге (ответ на вопрос об адресе > последний названный) и что видит
экран (`precision`).

Карта подменена записанными ответами; текст без точки (`geo_formatted` при
окончательном отказе) в этом дереве пишется руками теста: в бою его пишет
воркер участка A при известной карте улице (STREET_KNOWN).

ДИВЕРСИИ (каждая обязана краснеть): убрать координаты из проверки `exact`
в `card_grade` → строка без точки идёт как «точка дома»; заменить
`GRADE_RANK[…] >` на `>=` в `auto_address_yields_to` → карточка дёргается
между двумя картами об одном доме; снять `resolved_by_id` в памяти об
отказе → стёртый человеком дом возвращается точной строкой; снять
`_оператор_спросил_адрес` в `_выбрать_из_нескольких` → ответ на вопрос
проигрывает последнему названному; вернуть `endswith` в `point_is_approx`
→ «dadata~approx~picked» читается точкой дома.

Адреса — с экранов владельца и вымышленные; имён и телефонов клиентов нет.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message
from app.models.client import (
    CANDIDATE_ACCEPTED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CANDIDATE_SOURCE_LLM,
)
from app.services import address_parse as ap
from app.services import app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 9, 18, 9, 0, 0, tzinfo=UTC)
ФОРМАТ = "Звенигородская улица, 1, посёлок Заречный, Орск"
ДОМ = g.GeoHit(
    street="Звенигородская улица",
    house="1",
    settlement="Заречный",
    city="Орск",
    region="Оренбургская область",
    lat=51.2101234,
    lon=58.5012345,
    house_level=True,
)


def ctx(db_sessionmaker: Any, redis: Any) -> dict[str, Any]:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


def _строка(
    *,
    street: str = "ул Ленина",
    house: str = "5",
    kind: str = ap.KIND_HOUSE,
    level: str = "A",
    geo_status: str | None = g.GEO_EXACT,
    geo_provider: str | None = "dadata",
    geo_lat: float | None = 54.18,
    geo_lon: float | None = 45.18,
    geo_formatted: str | None = "ул Ленина, 5, Саранск",
) -> ClientAddressCandidate:
    """Строка для чистых проверок — без базы."""
    return ClientAddressCandidate(
        kind=kind,
        street=street,
        house=house,
        value=f"{street}, {house}" if kind == ap.KIND_HOUSE else street,
        level=level,
        geo_status=geo_status,
        geo_provider=geo_provider,
        geo_lat=geo_lat,
        geo_lon=geo_lon,
        geo_formatted=geo_formatted,
        detected_at=T0,
        status=CANDIDATE_PENDING,
    )


# ── 1. единственный судья степени ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("поля", "степень"),
    [
        ({}, g.GRADE_EXACT),
        ({"geo_provider": "dadata~approx"}, g.GRADE_APPROX),
        # Хвост — подстрока, не конец: участок A ставит свои хвосты.
        ({"geo_provider": "dadata~approx~picked"}, g.GRADE_APPROX),
        ({"geo_provider": "speller+dadata~approx"}, g.GRADE_APPROX),
        # Место — всегда центр пункта или массива.
        ({"kind": ap.KIND_PLACE, "street": "деревня Ивановка", "house": ""}, g.GRADE_APPROX),
        # `exact` без координат — инвариант нарушен, в карточку не идёт.
        ({"geo_lat": None, "geo_lon": None}, None),
        ({"geo_lat": None}, None),
        # Окончательный отказ ПО СОДЕРЖАНИЮ с текстом карты (улица известна)
        # — слова без точки.
        ({"geo_status": g.GEO_HOUSE_MISSING, "geo_lat": None, "geo_lon": None}, g.GRADE_TEXT),
        ({"geo_status": g.GEO_HOUSE_MISMATCH, "geo_lat": None, "geo_lon": None}, g.GRADE_TEXT),
        ({"geo_status": g.GEO_STREET_MISMATCH, "geo_lat": None, "geo_lon": None}, g.GRADE_TEXT),
        ({"geo_status": g.GEO_CITY_MISMATCH, "geo_lat": None, "geo_lon": None}, g.GRADE_TEXT),
        ({"geo_status": g.GEO_OTHER_CITY, "geo_lat": None, "geo_lon": None}, g.GRADE_TEXT),
        # Выбор из нескольких — не отказ по содержанию: точку даёт автоматика
        # участка A или строка остаётся показом; текстом — никогда.
        ({"geo_status": g.GEO_AMBIGUOUS, "geo_lat": None, "geo_lon": None}, None),
        ({"geo_status": g.GEO_ELSEWHERE, "geo_lat": None, "geo_lon": None}, None),
        # Отказ без текста — не годна. `not_found` с текстом здесь не
        # проверяется нарочно: улица карте неизвестна по определению статуса,
        # участок A `geo_formatted` при нём не пишет (контракт п.2).
        ({"geo_status": g.GEO_STREET_MISMATCH, "geo_formatted": None}, None),
        ({"geo_status": g.GEO_NOT_FOUND, "geo_formatted": None}, None),
        # Ещё проверяется — не годна, даже с текстом.
        ({"geo_status": g.GEO_PENDING}, None),
        ({"geo_status": g.GEO_ERROR}, None),
        ({"geo_status": g.GEO_BLOCKED}, None),
        ({"geo_status": g.GEO_NO_CITY}, None),
        ({"geo_status": None}, None),
        ({"geo_status": None, "geo_formatted": None}, None),
        # Место без точки — не годна.
        (
            {
                "kind": ap.KIND_PLACE,
                "street": "деревня Ивановка",
                "house": "",
                "geo_status": g.GEO_NOT_FOUND,
                "geo_formatted": None,
            },
            None,
        ),
    ],
)
def test_степень_строки_один_судья(поля: dict[str, Any], степень: str | None) -> None:
    row = _строка(**поля)
    assert clients_svc.candidate_grade(row) == степень
    # Обёртка ничего не добавляет к судье: те же поля — тот же ответ.
    assert степень == g.card_grade(
        row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
    )


@pytest.mark.parametrize(
    ("поля", "точность"),
    [
        ({}, "exact"),
        ({"geo_provider": "dadata~approx"}, "approx"),
        ({"kind": ap.KIND_PLACE, "street": "деревня Ивановка", "house": ""}, "approx"),
        ({"geo_status": g.GEO_HOUSE_MISSING, "geo_lat": None, "geo_lon": None}, "none"),
        ({"geo_status": g.GEO_PENDING}, "none"),
        ({"geo_status": None}, "none"),
        ({"geo_lat": None, "geo_lon": None}, "none"),
    ],
)
def test_степень_точки_для_экрана(поля: dict[str, Any], точность: str) -> None:
    assert clients_svc.candidate_precision(_строка(**поля)) == точность


def test_лестница_читается_в_одном_месте() -> None:
    """Направление `geocode.GRADE_RANK` (у участка A меньше — лучше) читает
    только `grade_weight`; все ворота сравнивают через `grade_beats`. Сторож на
    слияние: перевернись ранг — лестница обязана остаться text → approx →
    exact, иначе краснеет здесь, а не в бою."""
    assert clients_svc.grade_beats(g.GRADE_EXACT, g.GRADE_APPROX)
    assert clients_svc.grade_beats(g.GRADE_APPROX, g.GRADE_TEXT)
    assert clients_svc.grade_beats(g.GRADE_EXACT, g.GRADE_TEXT)
    assert clients_svc.grade_beats(g.GRADE_TEXT, None)
    assert not clients_svc.grade_beats(g.GRADE_TEXT, g.GRADE_APPROX)
    assert not clients_svc.grade_beats(g.GRADE_EXACT, g.GRADE_EXACT)
    assert not clients_svc.grade_beats(None, None)
    assert sorted(
        (g.GRADE_TEXT, None, g.GRADE_EXACT, g.GRADE_APPROX), key=clients_svc.grade_weight
    ) == [None, g.GRADE_TEXT, g.GRADE_APPROX, g.GRADE_EXACT]


def test_хвост_приблизительной_точки_подстрокой() -> None:
    assert g.point_is_approx("dadata~approx~picked") is True
    assert g.point_is_approx("dadata+yandex") is False
    assert g.point_is_approx(None) is False
    assert g.mark_approx("dadata~approx~picked") == "dadata~approx~picked"
    assert g.mark_approx("dadata") == "dadata~approx"


def test_текст_в_карточку_по_степени() -> None:
    """Ворота «есть ли строка карты» — степень: `candidate_address_text`
    передаёт сборщику `geo_formatted` только у годной строки."""
    части = {"office": "21", "entrance": None, "floor": None, "intercom": None}
    текст = clients_svc.candidate_address_text
    # Текст без точки — строка карты, части при ней.
    assert (
        текст(
            _строка(
                street="ул Садовая",
                house="3",
                geo_status=g.GEO_HOUSE_MISSING,
                geo_lat=None,
                geo_lon=None,
                geo_formatted="ул Садовая, 3, Ялга, Саранск",
            ),
            parts=части,
        )
        == "ул Садовая, 3, Ялга, Саранск, кв 21"
    )
    # Место с точкой — строка карты места.
    assert (
        текст(
            _строка(
                kind=ap.KIND_PLACE,
                street="деревня Ивановка",
                house="",
                geo_lat=59.5,
                geo_lon=30.1,
                geo_formatted="деревня Ивановка, Гатчинский район",
            ),
            parts={},
        )
        == "деревня Ивановка, Гатчинский район"
    )
    # Проверяется — слова клиента, даже если старый текст карты остался.
    assert (
        текст(
            _строка(
                street="ул Садовая",
                house="3",
                geo_status=g.GEO_PENDING,
                geo_formatted="ул Садовая, 3, Саранск",
            ),
            parts={},
        )
        == "ул Садовая, 3"
    )
    # `exact` без координат — инвариант нарушен, степени нет: слова клиента.
    assert текст(_строка(geo_lat=None, geo_lon=None), parts={"office": "3"}) == "ул Ленина, 5, кв 3"
    # Отказ без текста карты — слова клиента с частями.
    assert (
        текст(
            _строка(
                geo_status=g.GEO_STREET_MISMATCH, geo_lat=None, geo_lon=None, geo_formatted=None
            ),
            parts={"entrance": "2"},
        )
        == "ул Ленина, 5, подъезд 2"
    )


def test_слова_клиента_без_степени_несут_пункт() -> None:
    """Ревью 19.09: у строки без степени в поле идут слова клиента — и
    названный им пункт (тот самый, из-за которого вердикт сбросили), а не
    голое «улица, дом». Строка со степенью пункт не дописывает: он уже в
    строке карты."""
    текст = clients_svc.candidate_address_text
    отказ = _строка(
        street="ул Садовая",
        house="3",
        geo_status=g.GEO_NOT_FOUND,
        geo_lat=None,
        geo_lon=None,
        geo_formatted=None,
    )
    отказ.settlement, отказ.settlement_type = "Ялга", "посёлок"
    assert текст(отказ, parts={"office": "21"}) == "ул Садовая, 3, посёлок Ялга, кв 21"
    отказ.settlement_type = None
    assert текст(отказ, parts={}) == "ул Садовая, 3, Ялга"
    # Город клиента (`locality`) — когда пункта нет; оба — пункт главнее.
    отказ.settlement = None
    отказ.locality = "Гай"
    assert текст(отказ, parts={}) == "ул Садовая, 3, Гай"
    отказ.settlement, отказ.settlement_type = "Ялга", "рп"
    assert текст(отказ, parts={}) == "ул Садовая, 3, рп Ялга"
    # Пункт уже в словах клиента — второй раз не пишем.
    отказ.value = "Ялга, ул Садовая, 3"
    assert текст(отказ, parts={}) == "Ялга, ул Садовая, 3"
    # Со степенью — строка карты, пункт клиента не дописывается.
    со_степенью = _строка(
        street="ул Садовая",
        house="3",
        geo_status=g.GEO_HOUSE_MISSING,
        geo_lat=None,
        geo_lon=None,
        geo_formatted="ул Садовая, 3, рп Ялга, Саранск",
    )
    со_степенью.settlement, со_степенью.settlement_type = "Ялга", "посёлок"
    assert текст(со_степенью, parts={}) == "ул Садовая, 3, рп Ялга, Саранск"


# ── 2. ворота в воркере ───────────────────────────────────────────────────────


@pytest.fixture
async def seeded(seed_conversation: Any, db_sessionmaker: Any) -> Any:
    """Диалог из Орска с одной строкой уровня A без вердикта, карточка пуста."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        # Реплика строки — сам адрес, как на живом пути: автозапись с 19.09
        # перечитывает реплику строки нынешним разбором, и речь seed'а («Экран
        # разбит, почём?») с домом из другого текста в карточку не пошла бы.
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed_conversation.message_id))
        ).scalar_one()
        сообщение.body = "п заречный ул звенигородская д 1, кв 3"
        found = ap.parse(сообщение.body)
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=T0,
            found=found,
            now=T0,
        )
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
        seed_conversation.candidate_id = записано.candidate_id
    return seed_conversation


async def _row(db_sessionmaker: Any, cid: uuid.UUID) -> ClientAddressCandidate:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        return row


async def _card(db_sessionmaker: Any, client_id: uuid.UUID) -> Client:
    async with db_sessionmaker() as s:
        card = await s.get(Client, client_id)
        assert card is not None
        return card


async def _добавить(
    db_sessionmaker: Any,
    seed: Any,
    *,
    street: str,
    house: str,
    raw: str | None = None,
    message_id: uuid.UUID | None = None,
    message_at: datetime | None = None,
    geo_status: str | None = g.GEO_EXACT,
    geo_provider: str | None = "dadata",
    точка: tuple[float, float] | None = (51.23, 58.47),
    geo_formatted: str | None = None,
    source: str = "inbound",
    kind: str = ap.KIND_HOUSE,
    level: str = "A",
    office: str | None = None,
    detected_at: datetime | None = None,
    settlement: str | None = None,
    locality: str | None = None,
) -> uuid.UUID:
    """Строка с готовым вердиктом карты — как её оставил бы воркер."""
    async with db_sessionmaker() as s:
        row = ClientAddressCandidate(
            client_id=seed.client_id,
            conversation_id=seed.conversation_id,
            message_id=message_id,
            message_at=message_at,
            value=f"{street}, {house}" if kind == ap.KIND_HOUSE else street,
            street=street,
            house=house,
            raw=raw or f"{street} {house}",
            level=level,
            source=source,
            kind=kind,
            office=office,
            settlement=settlement,
            locality=locality,
            status=CANDIDATE_PENDING,
            detected_at=detected_at or datetime.now(UTC),
            geo_status=geo_status,
            geo_provider=geo_provider,
            geo_lat=точка[0] if точка else None,
            geo_lon=точка[1] if точка else None,
            geo_formatted=geo_formatted,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _авто(db_sessionmaker: Any, redis: Any, seed: Any) -> str:
    return await worker.autofill_address(ctx(db_sessionmaker, redis), seed.conversation_id)


async def _реплика(
    db_sessionmaker: Any, seed: Any, текст: str, when: datetime, *, direction: str = "out"
) -> None:
    """Сообщение ленты: `_оператор_спросил_адрес` считает по ленте, а не по
    строкам адреса — реплики клиента после вопроса закрывают его."""
    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=seed.conversation_id,
                external_message_id=f"{direction}-{when.timestamp()}",
                direction=direction,
                sender_type="user" if direction == "out" else "client",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def test_текст_без_точки_ставит_автозапись_и_пишется_словами_карты(
    seeded: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    """Карта улицу знает, дом — нет: воркер участка A кладёт текст без точки в
    `geo_formatted`; автозапись пишет его в пустую карточку с частями, экран
    видит `precision none`. Следом «подъезд 2» — карточка пересобрана."""

    async def улица_без_дома(query: Any, **kw: Any) -> list[g.GeoHit]:
        return [
            g.GeoHit(
                street="улица 40 лет Октября",
                house="1",
                settlement="Заречный",
                city="Орск",
                region="Оренбургская область",
                lat=51.0,
                lon=58.0,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", улица_без_дома)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_STREET_MISMATCH
    )
    # Без текста степени нет: задача не ставится, автозапись молчит.
    assert not await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    # Участок A нашёл улицу клиента в его пункте и записал текст без точки.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.geo_formatted = "ул Звенигородская, 1, посёлок Заречный, Орск"
        await s.commit()
    assert clients_svc.candidate_grade(await _row(db_sessionmaker, seeded.candidate_id)) == "text"
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address == "ул Звенигородская, 1, посёлок Заречный, Орск, кв 3"
    assert card.address_candidate_id == seeded.candidate_id and card.address_set_at is None
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "client.address_captured")
                )
            )
            .scalars()
            .all()
        )
    assert личность["address_source"] == "auto"
    assert личность["address_geo"]["status"] == g.GEO_STREET_MISMATCH
    assert личность["address_geo"]["precision"] == "none"
    assert журнал[-1].details["precision"] == "none"
    # Части следующей репликой — в текст карточки, а не только в цитату.
    async with db_sessionmaker() as s:
        assert await clients_svc.refine_address_parts(
            s,
            client_id=seeded.client_id,
            conversation_id=seeded.conversation_id,
            parts={"entrance": "2"},
        )
        await s.commit()
    assert (await _card(db_sessionmaker, seeded.client_id)).address == (
        "ул Звенигородская, 1, посёлок Заречный, Орск, кв 3, подъезд 2"
    )


async def test_приблизительная_точка_ставит_автозапись_и_подписана(
    seeded: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    """Дом в справочнике есть, точка — пункта (`precise=False`), Яндекса нет:
    строка помечена `~approx`, автозапись поставлена и пишет с `approx`."""

    async def дом_с_точкой_пункта(query: Any, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        return [dataclasses.replace(ДОМ, precise=False)]

    monkeypatch.setattr(worker.nominatim, "search", дом_с_точкой_пункта)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    row = await _row(db_sessionmaker, seeded.candidate_id)
    assert g.point_is_approx(row.geo_provider) and row.geo_lat is not None
    assert clients_svc.candidate_grade(row) == g.GRADE_APPROX
    assert await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert личность["address"] == f"{ФОРМАТ}, кв 3"
    assert личность["address_geo"]["precision"] == "approx"


async def test_инвариант_записи_вердикта_exact_с_координатами(
    seeded: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    """`_пометить` пишет `exact` только вместе с точкой дома: степень читает
    координаты, и строка без них в карточку не пошла бы."""

    async def карта(query: Any, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        return [ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", карта)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    row = await _row(db_sessionmaker, seeded.candidate_id)
    assert row.geo_status == g.GEO_EXACT and (row.geo_lat, row.geo_lon) == (ДОМ.lat, ДОМ.lon)
    assert clients_svc.candidate_grade(row) == g.GRADE_EXACT
    assert await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")


# ── 3. лестница уступок ───────────────────────────────────────────────────────


async def _текст_в_карточке(
    seeded: Any, db_sessionmaker: Any, redis: Any, *, formatted: str = "ул Ленина, 5, Саранск"
) -> uuid.UUID:
    """Карточка заполнена текстом без точки из строки «ул Ленина, 5»."""
    cid = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="5",
        raw="ул Ленина 5",
        message_id=uuid.uuid4(),
        message_at=T0,
        geo_status=g.GEO_HOUSE_MISSING,
        точка=None,
        geo_formatted=formatted,
    )
    async with db_sessionmaker() as s:
        # Сеяная строка Орска здесь не участвует.
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status = CANDIDATE_ACCEPTED
        row.resolved_by_id = uuid.uuid4()
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == cid and card.address == formatted
    return cid


async def test_тот_же_источник_переверdictован_exact_пересобирает_карточку(
    seeded: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    """Текст без точки в карточке → пункт дописан, вердикт сброшен (в поле
    остаётся прежний текст) → карта подтвердила дом → `_пометить` пересобрал
    карточку строкой карты с точкой."""

    async def улица_без_дома(query: Any, **kw: Any) -> list[g.GeoHit]:
        return [dataclasses.replace(ДОМ, street="улица 40 лет Октября")]

    monkeypatch.setattr(worker.nominatim, "search", улица_без_дома)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row.geo_status == g.GEO_STREET_MISMATCH
        row.geo_formatted = "ул Звенигородская, 1, посёлок Заречный, Орск"
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    async with db_sessionmaker() as s:
        clients_svc.сбросить_вердикт(
            await s.get(ClientAddressCandidate, seeded.candidate_id), reason="settlement_named"
        )
        await s.commit()
    # Проверяется — в поле прежний текст, трогать рано.
    assert (await _card(db_sessionmaker, seeded.client_id)).address == (
        "ул Звенигородская, 1, посёлок Заречный, Орск, кв 3"
    )

    async def дом(query: Any, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        return [ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", дом)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address == f"{ФОРМАТ}, кв 3" and card.address_candidate_id == seeded.candidate_id


async def test_текст_уступает_точному_дому_того_же_места(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    прежняя = await _текст_в_карточке(seeded, db_sessionmaker, redis)
    новая = await _добавить(
        db_sessionmaker,
        seeded,
        street="Ленина",
        house="5",
        raw="Ленина 5 подъезд 2",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=5),
        geo_formatted="ул Ленина, 5, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == новая and card.address_set_at is None
    старая = await _row(db_sessionmaker, прежняя)
    assert старая.status == CANDIDATE_REJECTED and старая.resolved_by_id is None
    async with db_sessionmaker() as s:
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog)
                    .where(AuditLog.action == "client.address_captured")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    # Автозапись пишет условным UPDATE и передаёт «было пусто» явно: обе
    # записи — «собран», и у последней степень точки уже «точка дома».
    assert [з.details["precision"] for з in журнал] == ["none", "exact"]


async def test_между_местами_карточка_автоматикой_не_переезжает(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Текст «Ленина 5» в карточке, из другой реплики — точный «Мира 7»:
    законно два адреса, вторая строка остаётся показом «Также назван»."""
    прежняя = await _текст_в_карточке(seeded, db_sessionmaker, redis)
    другая = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Мира",
        house="7",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=5),
        geo_formatted="ул Мира, 7, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == прежняя
    assert (await _row(db_sessionmaker, другая)).status == CANDIDATE_PENDING
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert [c["id"] for c in личность["address_candidates"]] == [str(другая)]


async def test_понижения_нет_и_семья_уточняется(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Точный «29» с «кв 5» в карточке: «~approx» того же дома — skip;
    «29 А» с точкой — уточнение семьи, «кв 5» едет с карточкой, «подъезд 2»
    следом — в тексте обе части; «29 к 1» при «29 к 2» — skip."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status, row.resolved_by_id = CANDIDATE_ACCEPTED, uuid.uuid4()
        await s.commit()
    первая = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="29",
        raw="Ленина 29 кв 5",
        message_id=uuid.uuid4(),
        message_at=T0,
        geo_formatted="ул Ленина, 29, Саранск",
        office="5",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (
        await _card(db_sessionmaker, seeded.client_id)
    ).address == "ул Ленина, 29, Саранск, кв 5"
    # Приблизительная точка того же дома — не выше по лестнице.
    хуже = await _добавить(
        db_sessionmaker,
        seeded,
        street="Ленина",
        house="29",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=1),
        geo_provider="dadata~approx",
        geo_formatted="ул Ленина, 29, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, хуже)
        row.status = CANDIDATE_REJECTED
        await s.commit()
    # Уточнение семьи: «29» → «29 А», обе с точкой дома.
    литера = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="29 А",
        raw="Ленина 29 А",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=2),
        точка=(51.2301, 58.4702),
        geo_formatted="ул Ленина, 29А, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == литера
    assert card.address == "ул Ленина, 29А, Саранск, кв 5"
    assert (await _row(db_sessionmaker, первая)).status == CANDIDATE_REJECTED
    async with db_sessionmaker() as s:
        assert await clients_svc.refine_address_parts(
            s,
            client_id=seeded.client_id,
            conversation_id=seeded.conversation_id,
            parts={"entrance": "2"},
        )
        await s.commit()
    assert (await _card(db_sessionmaker, seeded.client_id)).address == (
        "ул Ленина, 29А, Саранск, кв 5, подъезд 2"
    )
    # Спор корпусов — не уточнение.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, литера)
        row.house, row.value = "29 к 2", "ул Ленина, 29 к 2"
        card = await s.get(Client, seeded.client_id)
        card.address_value = row.value
        await s.commit()
    await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="29 к 1",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=3),
        точка=(51.2305, 58.4705),
        geo_formatted="ул Ленина, 29 к 1, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    assert (await _card(db_sessionmaker, seeded.client_id)).address_candidate_id == литера


async def test_руками_и_кнопкой_не_уступает(seeded: Any, db_sessionmaker: Any) -> None:
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        прежняя = await s.get(ClientAddressCandidate, seeded.candidate_id)
        прежняя.geo_status, прежняя.geo_formatted = g.GEO_HOUSE_MISSING, "ул Звенигородская, 1"
        прежняя.status = CANDIDATE_ACCEPTED
        card.address, card.address_candidate_id = "ул Звенигородская, 1", прежняя.id
        новая = ClientAddressCandidate(
            client_id=seeded.client_id,
            conversation_id=seeded.conversation_id,
            value="ул Звенигородская, 1",
            street="ул Звенигородская",
            house="1",
            raw="Звенигородская 1",
            level="A",
            detected_at=datetime.now(UTC),
            geo_status=g.GEO_EXACT,
            geo_provider="dadata",
            geo_lat=51.22,
            geo_lon=58.48,
            geo_formatted=ФОРМАТ,
        )
        s.add(новая)
        await s.flush()
        assert (await clients_svc.auto_address_yields_to(s, card, новая))[1] == (
            clients_svc.YIELD_GRADE_UP
        )
        прежняя.resolved_by_id = uuid.uuid4()  # подтверждено кнопкой
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        прежняя.resolved_by_id = None
        card.address_set_at = datetime.now(UTC)  # набрано руками
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        await s.rollback()


async def _место_в_карточке(
    seeded: Any, db_sessionmaker: Any, redis: Any, **пункт: str | None
) -> uuid.UUID:
    """Карточка заполнена местом «деревня Ивановка» (approx, центр пункта)."""
    cid = await _добавить(
        db_sessionmaker,
        seeded,
        street="деревня Ивановка",
        house="",
        kind=ap.KIND_PLACE,
        raw="деревня Ивановка",
        message_id=uuid.uuid4(),
        message_at=T0,
        точка=(59.5, 30.1),
        geo_formatted="деревня Ивановка, Гатчинский район",
        **пункт,
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status = CANDIDATE_ACCEPTED
        row.resolved_by_id = uuid.uuid4()
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == cid
    return cid


@pytest.mark.parametrize(
    ("поле", "у_места", "у_дома"),
    [("settlement", "Ивановка", "Петровка"), ("locality", "Гатчина", "Луга")],
    ids=["другой пункт", "другой город"],
)
async def test_место_не_уступает_дому_из_другого_пункта(
    seeded: Any, db_sessionmaker: Any, redis: Any, поле: str, у_места: str, у_дома: str
) -> None:
    """Ревью 19.09: «деревня Ивановка» в карточке, из другой реплики точный
    «д Петровка, ул Лесная 3» — два места; между местами карточка
    автоматикой не переезжает (контракт п.4), вторая строка — «Также назван».
    Дом того же пункта и дом без пункта место по-прежнему вытесняют."""
    место = await _место_в_карточке(seeded, db_sessionmaker, redis, **{поле: у_места})
    чужой = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Лесная",
        house="3",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=5),
        geo_formatted="ул Лесная, 3, д Петровка",
        **{поле: у_дома},
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == место
    assert (await _row(db_sessionmaker, чужой)).status == CANDIDATE_PENDING
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        новая = await s.get(ClientAddressCandidate, чужой)
        assert await clients_svc.auto_address_yields_to(s, card, новая) == (None, None)
        # Тот же пункт — уступает; дом без пункта (после «место; дом» он
        # несёт пункт места) — уступает.
        setattr(новая, поле, у_места)
        assert (await clients_svc.auto_address_yields_to(s, card, новая))[1] == (
            clients_svc.YIELD_PLACE_TO_HOUSE
        )
        setattr(новая, поле, None)
        assert (await clients_svc.auto_address_yields_to(s, card, новая))[1] == (
            clients_svc.YIELD_PLACE_TO_HOUSE
        )
        await s.rollback()


async def test_после_отказа_карты_пересборка_держит_пункт_клиента(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Ревью 19.09: «Садовая 3 кв 21» → авто «ул Садовая, 3, Саранск,
    кв 21» → «Ялга!» (пункт дописан, вердикт сброшен) → карта окончательно
    отказала без текста. Раньше пересборка собирала «ул Садовая, 3, кв
    21» — без города и без пункта, из-за которого вердикт сбросили; теперь —
    слова клиента с пунктом, а в журнале своя причина `verdict_refused`."""
    cid = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Садовая",
        house="3",
        raw="Садовая 3 кв 21",
        message_id=uuid.uuid4(),
        message_at=T0,
        geo_formatted="ул Садовая, 3, Саранск",
        office="21",
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status, row.resolved_by_id = CANDIDATE_ACCEPTED, uuid.uuid4()
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (await _card(db_sessionmaker, seeded.client_id)).address == (
        "ул Садовая, 3, Саранск, кв 21"
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        card = await s.get(Client, seeded.client_id)
        row.settlement, row.settlement_type = "Ялга", "посёлок"
        clients_svc.сбросить_вердикт(row, reason="settlement_named")
        # Проверяется — в поле прежний текст.
        assert await clients_svc.refresh_auto_address(s, card, row) is False
        assert card.address == "ул Садовая, 3, Саранск, кв 21"
        row.geo_status, row.geo_provider = g.GEO_NOT_FOUND, "dadata"
        assert await clients_svc.refresh_auto_address(s, card, row) is True
        assert card.address == "ул Садовая, 3, посёлок Ялга, кв 21"
        assert card.address_candidate_id == cid and card.address_set_at is None
        await s.commit()
    async with db_sessionmaker() as s:
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog)
                    .where(AuditLog.action == "client.address_edited")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [з.details["reason"] for з in журнал] == ["verdict_refused"]
    assert журнал[-1].details["previous"] == "ул Садовая, 3, Саранск, кв 21"
    # Диверсия: та же строка снова подтверждена картой (с пунктом) — причина
    # прежняя, `parts_refined`, текст — строкой карты.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        card = await s.get(Client, seeded.client_id)
        row.geo_status, row.geo_formatted = g.GEO_EXACT, "ул Садовая, 3, рп Ялга, Саранск"
        row.geo_lat, row.geo_lon = 54.13, 45.23
        assert await clients_svc.refresh_auto_address(s, card, row) is True
        assert card.address == "ул Садовая, 3, рп Ялга, Саранск, кв 21"
        await s.commit()
    async with db_sessionmaker() as s:
        причины = (
            (
                await s.execute(
                    sa.select(AuditLog.details["reason"].as_string())
                    .where(AuditLog.action == "client.address_edited")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert причины == ["verdict_refused", "parts_refined"]


# ── 4. память об отказе ───────────────────────────────────────────────────────


async def test_стёр_человек_место_закрыто_любой_степенью(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """Приблизительная точка записана → оператор стёр → тот же дом с точкой
    дома → skip: стирание значит «это не его адрес», а не «точка неверна»."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata~approx"
        row.geo_lat, row.geo_lon, row.geo_formatted = 51.22, 58.48, ФОРМАТ
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    res = await client.put(
        f"/api/v1/clients/{seeded.client_id}/address",
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text
    assert (await _row(db_sessionmaker, seeded.candidate_id)).resolved_by_id is not None
    await _добавить(
        db_sessionmaker,
        seeded,
        street="Звенигородская",
        house="1",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=5),
        точка=(51.2101234, 58.5012345),
        geo_formatted=ФОРМАТ,
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    assert (await _card(db_sessionmaker, seeded.client_id)).address is None


async def test_не_адрес_с_экрана_закрывает_место(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """Единственное решение с экрана — «Не адрес»: строка отказана человеком,
    и точная строка того же дома автоматикой не пишется."""
    res = await client.post(
        f"/api/v1/clients/{seeded.client_id}/address-candidates/{seeded.candidate_id}/resolve",
        json={"decision": "reject"},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["candidate"]["geo"]["precision"] == "none"
    await _добавить(
        db_sessionmaker,
        seeded,
        street="Звенигородская",
        house="1",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=5),
        точка=(51.2101234, 58.5012345),
        geo_formatted=ФОРМАТ,
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"


async def test_цепочка_автоматики_text_approx_exact_одного_места(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Отклонённые автоматикой строки низшей степени не запирают высшую;
    та же или низшая — запирают."""
    text = await _текст_в_карточке(seeded, db_sessionmaker, redis)
    approx = await _добавить(
        db_sessionmaker,
        seeded,
        street="Ленина",
        house="5",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=1),
        geo_provider="dadata~approx",
        geo_formatted="ул Ленина, 5, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (await _row(db_sessionmaker, text)).status == CANDIDATE_REJECTED
    exact = await _добавить(
        db_sessionmaker,
        seeded,
        street="улица Ленина",
        house="5",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=2),
        geo_formatted="ул Ленина, 5, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == exact
    assert (await _row(db_sessionmaker, approx)).status == CANDIDATE_REJECTED
    # Ещё одна точная того же места — не выше отклонённой автоматикой approx?
    # Выше; но карточка уже точная — лестница молчит, строка остаётся показом.
    ещё = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул. Ленина",
        house="5",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=3),
        geo_formatted="ул Ленина, 5, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    assert (await _row(db_sessionmaker, ещё)).status == CANDIDATE_PENDING
    # Память автоматики по степени: стёртая автоматикой approx запирает approx
    # того же места в пустой карточке, но не точный дом.
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        card.address, card.address_candidate_id, card.address_value = None, None, None
        for cid in (exact, ещё):
            row = await s.get(ClientAddressCandidate, cid)
            row.status = CANDIDATE_REJECTED
            row.resolved_by_id = None
        await s.commit()
    снова_approx = await _добавить(
        db_sessionmaker,
        seeded,
        street="Улица Ленина",
        house="5",
        raw="Ленина 5 ещё раз",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=4),
        geo_provider="dadata~approx",
        geo_formatted="ул Ленина, 5, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    assert (await _row(db_sessionmaker, снова_approx)).status == CANDIDATE_PENDING
    assert (await _card(db_sessionmaker, seeded.client_id)).address is None


# ── 5. два адреса в одном диалоге ─────────────────────────────────────────────


async def test_ответ_на_вопрос_об_адресе_важнее_последнего_названного(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status, row.resolved_by_id = CANDIDATE_ACCEPTED, uuid.uuid4()
        await s.commit()
    ленина = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="5",
        message_id=uuid.uuid4(),
        message_at=T0,
        geo_formatted="ул Ленина, 5, Саранск",
    )
    await _реплика(
        db_sessionmaker, seeded, "Подскажите адрес, куда подъехать?", T0 + timedelta(minutes=1)
    )
    await _реплика(db_sessionmaker, seeded, "ул Мира 7", T0 + timedelta(minutes=2), direction="in")
    мира = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Мира",
        house="7",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=2),
        точка=(51.24, 58.46),
        geo_formatted="ул Мира, 7, Саранск",
    )
    # И ещё один, названный ПОСЛЕ ответа: последний, но не ответ на вопрос.
    await _реплика(db_sessionmaker, seeded, "Хорошо, во сколько удобно?", T0 + timedelta(minutes=3))
    await _реплика(
        db_sessionmaker, seeded, "ул Пушкина 9", T0 + timedelta(minutes=4), direction="in"
    )
    третий = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Пушкина",
        house="9",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=4),
        точка=(51.25, 58.45),
        geo_formatted="ул Пушкина, 9, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    card = await _card(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == мира and card.address == "ул Мира, 7, Саранск"
    for cid in (ленина, третий):
        assert (await _row(db_sessionmaker, cid)).status == CANDIDATE_PENDING
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert {c["id"] for c in личность["address_candidates"]} == {str(ленина), str(третий)}
    # Поверх заполненной карточки третье место не переезжает: «Также назван».
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    assert (await _card(db_sessionmaker, seeded.client_id)).address_candidate_id == мира


async def test_без_вопроса_последний_названный(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status, row.resolved_by_id = CANDIDATE_ACCEPTED, uuid.uuid4()
        await s.commit()
    ленина = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="5",
        message_id=uuid.uuid4(),
        message_at=T0,
        geo_formatted="ул Ленина, 5, Саранск",
    )
    мира = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Мира",
        house="7",
        message_id=uuid.uuid4(),
        message_at=T0 + timedelta(minutes=2),
        точка=(51.24, 58.46),
        geo_formatted="ул Мира, 7, Саранск",
    )
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seeded.conversation_id)
        годные = [await s.get(ClientAddressCandidate, cid) for cid in (ленина, мира)]
        выбор, причина = await worker._выбрать_из_нескольких(s, conv, годные)
    assert (выбор.id if выбор else None, причина) == (мира, worker.PICK_LAST_NAMED)
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (await _card(db_sessionmaker, seeded.client_id)).address_candidate_id == мира


async def test_две_головы_одной_реплики(seeded: Any, db_sessionmaker: Any, redis: Any) -> None:
    """Равной степени — перечисление, не выбираем; text против exact той же
    реплики (строка модели-читателя) — карта рассудила: пишется exact."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status, row.resolved_by_id = CANDIDATE_ACCEPTED, uuid.uuid4()
        await s.commit()
    реплика = uuid.uuid4()
    a = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Ленина",
        house="5",
        raw="три клуба: Ленина 5, Мира 7",
        message_id=реплика,
        message_at=T0,
        geo_formatted="ул Ленина, 5, Саранск",
    )
    b = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Мира",
        house="7",
        raw="три клуба: Ленина 5, Мира 7",
        message_id=реплика,
        message_at=T0,
        точка=(51.24, 58.46),
        geo_formatted="ул Мира, 7, Саранск",
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "skip"
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seeded.conversation_id)
        годные = [await s.get(ClientAddressCandidate, cid) for cid in (a, b)]
        assert await worker._выбрать_из_нескольких(s, conv, годные) == (
            None,
            worker.PICK_TWO_IN_ONE_REPLY,
        )
        for cid in (a, b):
            (await s.get(ClientAddressCandidate, cid)).status = CANDIDATE_REJECTED
        await s.commit()
    # «11 улица Озёрная» правил (street_mismatch, текст без точки) против
    # «Озёрная 11» модели с тем же message_id (exact).
    реплика2 = uuid.uuid4()
    await _добавить(
        db_sessionmaker,
        seeded,
        street="11 улица Озёрная",
        house="11",
        raw="часов 11 улица Озёрная 11",
        message_id=реплика2,
        message_at=T0 + timedelta(minutes=10),
        geo_status=g.GEO_STREET_MISMATCH,
        точка=None,
        geo_formatted="ул Озёрная, 11, Саранск",
    )
    модель = await _добавить(
        db_sessionmaker,
        seeded,
        street="ул Озёрная",
        house="11",
        raw="часов 11 улица Озёрная 11",
        message_id=реплика2,
        message_at=T0 + timedelta(minutes=10),
        точка=(51.26, 58.44),
        geo_formatted="ул Озёрная, 11, Саранск",
        source=CANDIDATE_SOURCE_LLM,
    )
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (await _card(db_sessionmaker, seeded.client_id)).address_candidate_id == модель


# ── 6. API: степень точки на экране ───────────────────────────────────────────


async def test_identity_отдаёт_precision(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    async def identity() -> dict[str, Any]:
        res = await client.get(
            f"/api/v1/clients/{seeded.client_id}/identity",
            headers={"Authorization": f"Bearer {tokens['manager']}"},
        )
        assert res.status_code == 200, res.text
        return res.json()

    # Строка с отказом и вариантами — показ, `precision none`.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.geo_status = g.GEO_OTHER_CITY
        row.geo_variants = [
            {"formatted": "ул Мира, 7, Гай", "lat": 51.4, "lon": 58.4, "city": "Гай"}
        ]
        await s.commit()
    личность = await identity()
    assert личность["address_candidates"][0]["geo"]["precision"] == "none"
    assert личность["address_candidates"][0]["geo"]["variants"][0]["formatted"] == "ул Мира, 7, Гай"
    # Точка дома.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.geo_status, row.geo_variants = g.GEO_EXACT, None
        row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted = (
            "dadata",
            51.22,
            58.48,
            ФОРМАТ,
        )
        await s.commit()
    assert await _авто(db_sessionmaker, redis, seeded) == "filled"
    assert (await identity())["address_geo"]["precision"] == "exact"
    # Приблизительная точка — по хвосту провайдера, экран хвост не читает.
    async with db_sessionmaker() as s:
        (await s.get(ClientAddressCandidate, seeded.candidate_id)).geo_provider = "dadata~approx"
        await s.commit()
    assert (await identity())["address_geo"]["precision"] == "approx"
    # Записанное место — «approx» без чтения `kind` на экране.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.kind, row.geo_provider = ap.KIND_PLACE, "dadata"
        await s.commit()
    assert (await identity())["address_geo"]["precision"] == "approx"
    # Текст без точки под записанным адресом.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.kind, row.geo_status, row.geo_lat, row.geo_lon = (
            ap.KIND_HOUSE,
            g.GEO_HOUSE_MISSING,
            None,
            None,
        )
        await s.commit()
    геo = (await identity())["address_geo"]
    assert (геo["status"], геo["precision"]) == (g.GEO_HOUSE_MISSING, "none")
