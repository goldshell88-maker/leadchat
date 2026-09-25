"""Довески 19.09 к пакету автопривязки — скрины владельца утром.

1. Волжский: «9 микрорайон» → «пл Победы 15» и наоборот — микрорайон это часть
   адреса в микрорайонных городах; карта его не знает, в запрос он не идёт, в
   строку дома и в текст карточки — обязан (в обе стороны по времени).
2. Нефтеюганск: «14 мкр; 37» → «кв 18» рождали место «мкр 14» и «дом» «37 мкр,
   18» рядом с карточкой «мкр 14, 37, кв 18» — «сначала думать, потом давать
   адрес»: обрубки записанного адреса на экране не показываются, настоящий
   второй адрес — показывается.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.models import Client, ClientAddressCandidate
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING
from app.services import address_parse as ap
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services import inbound
from app.services.inbound import apply_inbound_event
from app.workers import geocode as worker
from tests.unit import test_address_context_1809 as стенд
from tests.unit import test_geo_1809 as гео

T0 = стенд.T0
_карточка_с_авто_адресом = стенд._карточка_с_авто_адресом
_строки = стенд._строки
событие = стенд.событие
# Фикстура соседнего стенда — присваиванием, не импортом имени (иначе ruff F811
# на параметрах тестов, как в test_autobind_policy_1809).
account = стенд.account

pytestmark = pytest.mark.anyio


async def _реплика(db, redis, account, текст: str, i: int, **kw) -> None:  # noqa: ANN001, ANN003
    await apply_inbound_event(
        db, redis, account, событие(текст, msg=f"m{i}", when=T0 + timedelta(minutes=i), **kw)
    )


# ── 1. микрорайон к дому ──────────────────────────────────────────────────────


def test_внутригородской_массив_по_форме() -> None:
    assert inbound.внутригородской_массив("микрорайон 9")
    assert inbound.внутригородской_массив("9 мкр")
    assert inbound.внутригородской_массив("квартал 92/93")
    assert not inbound.внутригородской_массив("СНТ Малиновка")
    assert not inbound.внутригородской_массив("ЖК Дубровино")
    assert not inbound.внутригородской_массив(None)


async def test_микрорайон_до_дома_клеится_даже_после_отказа_карты(
    db, redis, account, db_sessionmaker
):
    """«9 микрорайон» (карта не нашла) → «пл Победы 15»: area в строке дома,
    пункта в запросе нет, цитата склеена."""
    await _реплика(db, redis, account, "9 микрорайон", 0, город="volzhskiy")
    (место,) = await _строки(db_sessionmaker)
    assert место.kind == ap.KIND_PLACE and место.area == "микрорайон 9"
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, место.id)
        row.geo_status = g.GEO_NOT_FOUND  # DaData микрорайон не знает — как в бою
        await s.commit()
    await _реплика(db, redis, account, "пл Победы 15 кв 104 подъезд 6", 1, город="volzhskiy")
    строки = await _строки(db_sessionmaker)
    дом = next(r for r in строки if r.kind == ap.KIND_HOUSE)
    assert дом.area == "микрорайон 9"
    assert дом.settlement is None and дом.locality is None
    assert дом.raw.startswith("9 микрорайон; ")
    assert (дом.office, дом.entrance) == ("104", "6")


async def test_посёлок_с_микрорайоном_при_отказе_карты_не_клеится(
    db, redis, account, db_sessionmaker
):
    """Ревью 19.09: исключение для микрорайона — только у места БЕЗ пункта.
    «п. Приморский, 3 микрорайон» с отказом карты понёс бы в запрос сам
    посёлок (`с_пунктом_места` идёт веткой с пунктом) — сторож 13.09 держит.
    Положительный контроль — тест ниже."""
    await _реплика(db, redis, account, "п. Приморский, 3 микрорайон", 0, город="volzhskiy")
    (место,) = await _строки(db_sessionmaker)
    assert (место.kind, место.settlement, место.area) == (
        ap.KIND_PLACE,
        "Приморский",
        "микрорайон 3",
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, место.id)
        row.geo_status = g.GEO_NOT_FOUND
        await s.commit()
    await _реплика(db, redis, account, "пл Победы 15", 1, город="volzhskiy")
    дом = next(r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_HOUSE)
    assert дом.settlement is None and дом.area is None
    assert not дом.raw.startswith("п. Приморский")


async def test_посёлок_с_микрорайоном_без_вердикта_клеится_целиком(
    db, redis, account, db_sessionmaker
):
    """Положительный контроль к сторожу выше: пока карта месту не отказала,
    «п. Приморский, 3 микрорайон» → «пл Победы 15» клеится и пунктом, и
    массивом — иначе отрицательная проверка зеленела бы и без сторожа."""
    await _реплика(db, redis, account, "п. Приморский, 3 микрорайон", 0, город="volzhskiy")
    await _реплика(db, redis, account, "пл Победы 15", 1, город="volzhskiy")
    дом = next(r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_HOUSE)
    assert (дом.settlement, дом.area) == ("Приморский", "микрорайон 3")
    assert дом.raw.startswith("п. Приморский, 3 микрорайон; ")


async def test_деревня_с_отказом_карты_по_прежнему_не_клеится(db, redis, account, db_sessionmaker):
    """Сторож 13.09 жив: посёлок, которому карта отказала («территориально
    далеко»), к дому не клеится — он увёл бы запрос в другую область."""
    await _реплика(db, redis, account, "деревня Подлесниково", 0, город="pskov")
    (место,) = await _строки(db_sessionmaker)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, место.id)
        row.geo_status = g.GEO_NOT_FOUND
        await s.commit()
    await _реплика(db, redis, account, "Кленовая 6", 1, город="pskov")
    дом = next(r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_HOUSE)
    assert дом.settlement is None and дом.area is None


async def test_микрорайон_после_дома_дописывается_и_карточка_пересобирается(
    db, redis, account, db_sessionmaker
):
    """«пл Победы 15» → автозапись → «9 микрорайон»: area в строке дома, текст
    карточки с микрорайоном, вердикт карты не сброшен."""
    await _реплика(db, redis, account, "пл Победы 15 кв 104", 0, город="volzhskiy")
    (дом,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, дом.id, "пл Победы, 15, Волжский")
    await _реплика(db, redis, account, "9 микрорайон", 1, город="volzhskiy")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, дом.id)
        assert row.area == "микрорайон 9"
        assert row.geo_status == g.GEO_EXACT  # город тот же — перепроверять нечего
        card = await s.get(Client, дом.client_id)
        assert card.address == "пл Победы, 15, Волжский, кв 104, микрорайон 9"
        assert card.address_candidate_id == дом.id


async def test_микрорайон_после_дома_решённого_человеком_не_трогает(
    db, redis, account, db_sessionmaker
):
    await _реплика(db, redis, account, "пл Победы 15", 0, город="volzhskiy")
    (дом,) = await _строки(db_sessionmaker)
    await _карточка_с_авто_адресом(db_sessionmaker, дом.id, "пл Победы, 15, Волжский", кнопкой=True)
    await _реплика(db, redis, account, "9 микрорайон", 1, город="volzhskiy")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, дом.id)
        assert row.area is None
        card = await s.get(Client, дом.client_id)
        assert card.address == "пл Победы, 15, Волжский"


def test_текст_карточки_с_микрорайоном() -> None:
    assert (
        g.address_text(
            geo_formatted="пл Победы, 15, Волжский",
            geo_status=g.GEO_EXACT,
            value="пл Победы, 15",
            parts={"office": "104", "entrance": "6"},
            area="микрорайон 9",
        )
        == "пл Победы, 15, Волжский, кв 104, подъезд 6, микрорайон 9"
    )
    # Строка карты уже содержит микрорайон — второй раз не пишем.
    assert (
        g.address_text(
            geo_formatted="9-й микрорайон, 15, Волжский",
            geo_status=g.GEO_EXACT,
            value="9 мкр, 15",
            parts={},
            area="9-й микрорайон",
        )
        == "9-й микрорайон, 15, Волжский"
    )


# ── 2. обрубки записанного адреса не показываются ─────────────────────────────


def _строка(
    *,
    kind: str,
    value: str,
    street: str,
    house: str = "",
    settlement: str | None = None,
    area: str | None = None,
    office: str | None = None,
    floor: str | None = None,
    conv: uuid.UUID,
    at: datetime,
    geo_status: str | None = None,
    geo_formatted: str | None = None,
) -> ClientAddressCandidate:
    return ClientAddressCandidate(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        conversation_id=conv,
        value=value,
        street=street,
        house=house,
        settlement=settlement,
        area=area,
        office=office,
        floor=floor,
        raw=value,
        level="A",
        kind=kind,
        status=CANDIDATE_PENDING,
        detected_at=at,
        message_at=at,
        geo_status=geo_status,
        geo_formatted=geo_formatted,
    )


def test_обрубки_нефтеюганска_поглощены_записанным_адресом() -> None:
    conv = uuid.uuid4()
    источник = _строка(
        kind=ap.KIND_HOUSE,
        value="14 мкр, 37",
        street="14 мкр",
        house="37",
        office="18",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="мкр 14, 37, Нефтеюганск",
    )
    источник.status = CANDIDATE_ACCEPTED
    место = _строка(kind=ap.KIND_PLACE, value="мкр 14", street="", area="мкр 14", conv=conv, at=T0)
    обрубок = _строка(
        kind=ap.KIND_HOUSE,
        value="37 мкр, 18",
        street="37 мкр",
        house="18",
        conv=conv,
        at=T0 + timedelta(minutes=3),
    )
    assert clients_svc._поглощена_источником(место, источник)
    assert clients_svc._поглощена_источником(обрубок, источник)
    assert clients_svc._без_дублей([место, обрубок], источник) == []


@pytest.mark.parametrize(
    ("часть", "номер"),
    [("office", "12"), ("floor", "2")],
    ids=["квартира", "этаж"],
)
def test_другой_дом_той_же_улицы_не_обрубок(часть: str, номер: str) -> None:
    """Ревью 19.09: «Ленина 5, кв 12» записан → через час «Ленина 12»
    (поправка или второй адрес). По числам это выглядело обрубком (12 —
    номер квартиры источника, слова улицы те же) — и строка пропадала с
    экрана насовсем. Та же улица, другой номер — другой дом: показывается
    «также назван». Диверсия: обрубок Нефтеюганска с ДРУГОЙ «улицей»
    («37 мкр, 18» при «14 мкр, 37») поглощён по-прежнему."""
    conv = uuid.uuid4()
    источник = _строка(
        kind=ap.KIND_HOUSE,
        value="ул Ленина, 5",
        street="ул Ленина",
        house="5",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="ул Ленина, д 5, Саранск",
        **{часть: номер},
    )
    источник.status = CANDIDATE_ACCEPTED
    другой_дом = _строка(
        kind=ap.KIND_HOUSE,
        value=f"Ленина, {номер}",
        street="Ленина",
        house=номер,
        conv=conv,
        at=T0 + timedelta(hours=1),
    )
    assert not clients_svc.одно_место(другой_дом, источник)
    assert not clients_svc._поглощена_источником(другой_дом, источник)
    assert [r for r, _ in clients_svc._без_дублей([другой_дом], источник)] == [другой_дом]
    # Тот же дом той же улицы — по-прежнему не предложение (его глушит
    # `_то_же_место`, а не этот сторож).
    тот_же = _строка(
        kind=ap.KIND_HOUSE, value="Ленина, 5", street="Ленина", house="5", conv=conv, at=T0
    )
    assert clients_svc._без_дублей([тот_же], источник) == []
    нефтеюганск = _строка(
        kind=ap.KIND_HOUSE,
        value="14 мкр, 37",
        street="14 мкр",
        house="37",
        office="18",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="мкр 14, 37, Нефтеюганск",
    )
    обрубок = _строка(
        kind=ap.KIND_HOUSE, value="37 мкр, 18", street="37 мкр", house="18", conv=conv, at=T0
    )
    assert clients_svc._поглощена_источником(обрубок, нефтеюганск)


def test_второй_адрес_ангарска_остаётся_показанным() -> None:
    """«93 квартал, 31» записан; «Учебная, 17» — школа, другой адрес: свои
    слова и числа, не обрубок — остаётся на экране как «также назван»."""
    conv = uuid.uuid4()
    источник = _строка(
        kind=ap.KIND_HOUSE,
        value="93 квартал, 31",
        street="93 квартал",
        house="31",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="кв-л 93, 31, Ангарск",
    )
    источник.status = CANDIDATE_ACCEPTED
    другой = _строка(
        kind=ap.KIND_HOUSE, value="Учебная, 17", street="Учебная", house="17", conv=conv, at=T0
    )
    assert not clients_svc._поглощена_источником(другой, источник)
    assert [r for r, _ in clients_svc._без_дублей([другой], источник)] == [другой]


def test_обрубок_из_другого_диалога_или_через_сутки_не_поглощается() -> None:
    conv = uuid.uuid4()
    источник = _строка(
        kind=ap.KIND_HOUSE,
        value="14 мкр, 37",
        street="14 мкр",
        house="37",
        office="18",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="мкр 14, 37, Нефтеюганск",
    )
    поздний = _строка(
        kind=ap.KIND_HOUSE,
        value="37 мкр, 18",
        street="37 мкр",
        house="18",
        conv=conv,
        at=T0 + timedelta(days=2),
    )
    чужой = _строка(
        kind=ap.KIND_HOUSE,
        value="37 мкр, 18",
        street="37 мкр",
        house="18",
        conv=uuid.uuid4(),
        at=T0,
    )
    assert not clients_svc._поглощена_источником(поздний, источник)
    assert not clients_svc._поглощена_источником(чужой, источник)


def test_место_микрорайон_поглощено_домом_с_area() -> None:
    conv = uuid.uuid4()
    источник = _строка(
        kind=ap.KIND_HOUSE,
        value="пл Победы, 15",
        street="пл Победы",
        house="15",
        area="микрорайон 9",
        conv=conv,
        at=T0,
        geo_status=g.GEO_EXACT,
        geo_formatted="пл Победы, 15, Волжский",
    )
    место = _строка(
        kind=ap.KIND_PLACE, value="микрорайон 9", street="", area="микрорайон 9", conv=conv, at=T0
    )
    assert clients_svc._поглощена_источником(место, источник)


# ── 3. уточнение точки Яндексом, когда пункт дублирует улицу ──────────────────

dadata_отвечает = гео.dadata_отвечает
osm_пусто = гео.osm_пусто
яндекс_отвечает = гео.яндекс_отвечает

#: Чита, «мкр Кедровка 18» (владелец 19.09): DaData отдаёт микрорайон и
#: пунктом, и «улицей», точка — центр микрорайона (qc_geo 2).
ОСЕТРОВКА_DADATA = гео._dadata(
    {
        "suggestions": [
            {
                "value": "Забайкальский край, г Чита, мкр Кедровка, 18",
                "data": {
                    "region_with_type": "Забайкальский край",
                    "city": "Чита",
                    "settlement": "Кедровка",
                    "settlement_with_type": "мкр Кедровка",
                    "street_with_type": "мкр Кедровка",
                    "house": "18",
                    "geo_lat": "51.987919",
                    "geo_lon": "113.576768",
                    "qc_geo": "2",
                    "fias_level": "8",
                },
            }
        ]
    }
)[0]
#: Яндекс на «Чита, микрорайон Кедровка 18» — дом с точной точкой; улицы у
#: него нет, микрорайон — район.
ОСЕТРОВКА_ЯНДЕКС = g.GeoHit(
    street=None,
    house="18",
    settlement="микрорайон Кедровка",
    city="Чита",
    region="Забайкальский край",
    lat=51.990019,
    lon=113.574868,
    house_level=True,
    settlement_kind="district",
)


def test_пункт_внутри_улицы_по_форме() -> None:
    assert g.settlement_inside_street("Кедровка", "мкр Кедровка")
    assert g.settlement_inside_street("Солнечный", "тер. СНТ Солнечный")
    assert not g.settlement_inside_street("Урожай", "ул Луговая")
    assert not g.settlement_inside_street("Черная Речка", "ул Златоглавая")


async def test_микрорайон_читы_уточняется_яндексом_без_дубля_пункта(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """В бою запрос уходил как «Чита, Кедровка, микрорайон Кедровка 18», и
    Яндекс отдавал район без дома — точка оставалась приблизительной."""
    await гео._режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[ОСЕТРОВКА_DADATA]]
    яндекс_отвечает["ответ"] = [ОСЕТРОВКА_ЯНДЕКС]
    cid = await гео._строка(
        seed_conversation, db_sessionmaker, "chita", гео._разобрать("мкр Кедровка 18")
    )
    assert await worker.geocode_candidate(гео.ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await гео._row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata+yandex"
    assert (row.geo_lat, row.geo_lon) == (ОСЕТРОВКА_ЯНДЕКС.lat, ОСЕТРОВКА_ЯНДЕКС.lon)
    assert яндекс_отвечает["запросы"] == [
        g.Query(
            region="Забайкальский край",
            city="Чита",
            settlement=None,
            street="мкр Кедровка",
            house="18",
        )
    ]


# ── 4. догон переписки порциями: откат не «протухает» объекты ─────────────────


async def test_догон_сухим_прогоном_переживает_порцию_в_500_сообщений(
    db, redis, account, db_sessionmaker
):
    """19.09: `backfill-cards --dry-run` на 30 днях падал MissingGreenlet —
    откат каждые 500 сообщений протухал уже загруженные объекты. Теперь
    объекты грузятся порцией заново; адрес после границы порции считается."""
    from app.cli import run_backfill_cards

    for i in range(501):
        await _реплика(db, redis, account, f"здравствуйте {i}", i, город="volzhskiy")
    await _реплика(db, redis, account, "пл Победы 15 кв 104", 501, город="volzhskiy")
    капчур: list[str] = []
    import typer

    orig = typer.echo
    typer.echo = lambda s, *a, **k: капчур.append(str(s))  # type: ignore[assignment]
    try:
        async with db_sessionmaker() as s:
            await run_backfill_cards(s, days=7, dry_run=True)
    finally:
        typer.echo = orig  # type: ignore[assignment]
    assert капчур and "сообщений=502" in капчур[-1] and "адресов=1" in капчур[-1], капчур


async def test_боевой_догон_коммитит_после_каждой_реплики_сухой_откатывает_порцию(
    db, redis, account, db_sessionmaker, monkeypatch
):
    """Ревью 19.09, D1. Боевой прогон — одна реплика, одна транзакция (как
    `workers/cards_catchup.replay_conversation`): UPDATE clients держит замок
    строки клиента до конца транзакции, а живой приём пишет в те же строки.
    Сухой — без единого commit'а, откат раз на порцию. Диверсия: вернуть
    commit раз на порцию — три реплики дают один commit вместо трёх."""
    from app.cli import run_backfill_cards

    for i, текст in enumerate(("здравствуйте", "пл Победы 15", "кв 104")):
        await _реплика(db, redis, account, текст, i, город="volzhskiy")

    async def прогон(*, dry_run: bool) -> tuple[int, int]:
        async with db_sessionmaker() as s:
            commit, rollback = s.commit, s.rollback
            счёт = {"commit": 0, "rollback": 0}

            async def commit_шпион() -> None:
                счёт["commit"] += 1
                await commit()

            async def rollback_шпион() -> None:
                счёт["rollback"] += 1
                await rollback()

            monkeypatch.setattr(s, "commit", commit_шпион)
            monkeypatch.setattr(s, "rollback", rollback_шпион)
            await run_backfill_cards(s, days=7, dry_run=dry_run)
        return счёт["commit"], счёт["rollback"]

    assert await прогон(dry_run=False) == (3, 0)
    assert await прогон(dry_run=True) == (0, 1)


async def test_порция_догона_читает_messages_парой_id_и_даты(
    db, redis, account, db_sessionmaker, monkeypatch
):
    """Ревью 19.09, D2. `messages` секционирована по дате: порция по одному
    `id IN (…)` обходила бы все секции. Стережём сам запрос к базе, а не текст
    исходника: WHERE порции — кортеж `(messages.id, messages.created_at) IN`,
    голого `messages.id IN` нет. Диверсия: вернуть `Message.id.in_(…)` — падает."""
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    from app.cli import run_backfill_cards

    for i in range(3):
        await _реплика(db, redis, account, f"здравствуйте {i}", i, город="volzhskiy")
    порции: list[str] = []
    async with db_sessionmaker() as s:
        настоящий = s.execute

        async def execute(stmt: Any, *args: Any, **kw: Any) -> Any:
            if isinstance(stmt, sa.Select) and stmt.whereclause is not None:
                sql = str(stmt.compile(dialect=postgresql.dialect()))
                if "JOIN conversations" in sql and "FROM messages" in sql:
                    порции.append(sql)
            return await настоящий(stmt, *args, **kw)

        monkeypatch.setattr(s, "execute", execute)
        await run_backfill_cards(s, days=7, dry_run=True)
    assert len(порции) == 1, порции
    (sql,) = порции
    where = sql.split("WHERE", 1)[1]
    assert "(messages.id, messages.created_at) IN" in where, where
    assert "messages.id IN" not in where.replace("(messages.id, messages.created_at) IN", ""), where


async def test_догон_по_образцу_берёт_только_подходящие_реплики(
    db, redis, account, db_sessionmaker, engine
):
    """21.09: `backfill-cards --match` — точечный догон под новую форму разбора
    («7.10.34» в квартальном городе): у реплики без найденного адреса строки
    нет, `address-reparse` её не увидит. Образец режет выборку в SQL по речи;
    без образца выборка прежняя. Диверсия: убрать условие `regexp_match` —
    первый прогон насчитает 3 сообщения вместо 1."""
    import re

    from app.cli import run_backfill_cards

    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.create_function(
            "regexp", 2, lambda p, s: s is not None and re.search(p, s) is not None
        )
    for i, текст in enumerate(("здравствуйте", "7.10.34", "пл Победы 15")):
        await _реплика(db, redis, account, текст, i, город="nefteyugansk")
    капчур: list[str] = []
    import typer

    orig = typer.echo
    typer.echo = lambda s, *a, **k: капчур.append(str(s))  # type: ignore[assignment]
    try:
        async with db_sessionmaker() as s:
            await run_backfill_cards(
                s, days=7, dry_run=True, match=r"^\s*[1-9]\d?\.[1-9]\d{0,2}\.[1-9]\d{0,2}\s*$"
            )
        async with db_sessionmaker() as s:
            await run_backfill_cards(s, days=7, dry_run=True)
    finally:
        typer.echo = orig  # type: ignore[assignment]
    assert "сообщений=1" in капчур[0] and "адресов=1" in капчур[0], капчур
    # «адресов» считает реплики со строкой, «строк_на_карту» — только НОВЫЕ
    # строки (по ним решается боевой прогон); здесь строки уже завёл живой
    # путь, новых нет.
    assert "строк_на_карту=0" in капчур[0], капчур
    assert "сообщений=3" in капчур[1], капчур
