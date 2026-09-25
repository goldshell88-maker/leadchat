"""Федеральные объявления без города: `avito.ru/all/…`, `avito.ru/rossiya/…` (19.09, B2).

До этой правки `parse_listing_url` считал городом любой первый сегмент,
которого нет среди служебных, и `/all/…` давал `city_slug="all"`: в шапке
«город all», воркер карты считал город известным человеку (`city_known`),
город из других диалогов клиента не брался, замер ASK_CITY молчал. Теперь
такой сегмент — «вся Россия»: не город и не служебный маршрут, слаг None,
категория за ним сохраняется (`listing_url.COUNTRY_SEGMENTS`).

Здесь — разбор ссылки, единый путь чтения (`conversation_city_slug`,
`item_out`), точка записи (`_apply_city`), воркер целиком на записанных
ответах карт и миграция `0080`, снимающая накопленное «all» из колонки.
На каждый гард — контрпример: слаг вне справочника («kotlas», «rossosh»)
по-прежнему город человека, а не повод спрашивать клиента.

Сеть не ходит: карты подменены фикстурами стенда 18.09. Ссылки — формы
боевых, не сами боевые; адреса вымышленные.
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.integrations.avito import listing_url
from app.integrations.avito.listing_url import (
    _REGION_PREFIX_ALIASES,
    _REGION_PREFIXES,
    _SERVICE_FIRST_SEGMENTS,
    CITIES,
    COUNTRY_SEGMENTS,
    parse_listing_url,
)
from app.models import Client, Conversation
from app.services import address_parse, client_enrich
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services.conversations import conversation_city, conversation_city_slug, item_out
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_geo_1809 import _row, _разобрать, _режим, ctx, дом

#: Карты подменены записанными ответами — фикстуры стенда 18.09 под своими
#: именами (присваивание, не импорт: pytest собирает их по имени модуля).
dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто

pytestmark = pytest.mark.anyio

ВСЯ_РОССИЯ = "https://www.avito.ru/all/predlozheniya_uslug/remont_bytovoy_tehniki_1234567890"
МИГРАЦИЯ = (
    pathlib.Path(__file__).resolve().parents[2]
    / "app/db/migrations/versions/0080_country_listing_slug.py"
)


# ── разбор ссылки ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "item_id", "category"),
    [
        (ВСЯ_РОССИЯ, 1234567890, "predlozheniya_uslug"),
        (
            "https://m.avito.ru/rossiya/predlozheniya_uslug/remont_1234567890?context=abc#x",
            1234567890,
            "predlozheniya_uslug",
        ),
        ("avito.ru/all/predlozheniya_uslug/remont_1234567890", 1234567890, "predlozheniya_uslug"),
        ("https://www.avito.ru/rossiya/1234567890", 1234567890, None),
        # unquote посегментно: «%61ll» — это «all», страна, а не город «%61ll».
        (
            "https://www.avito.ru/%61ll/predlozheniya_uslug/remont_1234567890",
            1234567890,
            "predlozheniya_uslug",
        ),
    ],
)
def test_федеральная_ссылка_без_города(url: str, item_id: int, category: str | None) -> None:
    p = parse_listing_url(url)
    assert (p.kind, p.item_id, p.ok, p.city_slug, p.city_name, p.city_tz, p.category_slug) == (
        "item",
        item_id,
        True,
        None,
        None,
        None,
        category,
    )
    # Та же функция, что у точек записи (вставка из вебхука, сверка).
    assert client_enrich.city_slug_of(url) is None


@pytest.mark.parametrize(
    ("slug", "city_name"),
    [
        # Вне справочника — слаг сохранён, имени нет: город виден человеку
        # («kurchatov» с 20.09 в справочнике — 35 диалогов за 90 дней).
        ("kotlas", None),
        # Начинается с «ross», но это не «rossiya»: точное множество, не префикс.
        ("rossosh", None),
        # Начинается с «al», в справочнике — Альметьевск.
        ("almetevsk", "Альметьевск"),
    ],
)
def test_слаг_вне_справочника_по_прежнему_город_человека(slug: str, city_name: str | None) -> None:
    p = parse_listing_url(f"https://www.avito.ru/{slug}/predlozheniya_uslug/remont_1234567890")
    assert (p.kind, p.ok, p.city_slug, p.city_name, p.category_slug) == (
        "item",
        True,
        slug,
        city_name,
        "predlozheniya_uslug",
    )


def test_один_сегмент_страны_остаётся_мусором() -> None:
    assert parse_listing_url("https://www.avito.ru/all").kind == "garbage"


def test_страна_и_служебные_сегменты_не_пересекаются() -> None:
    """Сторож от «кто-то добавил all в справочник»: сегмент страны обязан быть
    ровно одним смыслом — не городом, не служебным маршрутом, не областью."""
    assert COUNTRY_SEGMENTS == {"all", "rossiya"}
    assert COUNTRY_SEGMENTS.isdisjoint(_SERVICE_FIRST_SEGMENTS)
    assert COUNTRY_SEGMENTS.isdisjoint(CITIES)
    assert COUNTRY_SEGMENTS.isdisjoint(_REGION_PREFIXES)
    assert COUNTRY_SEGMENTS.isdisjoint(_REGION_PREFIX_ALIASES)
    for сегмент in COUNTRY_SEGMENTS:
        assert listing_url.city_by_slug(сегмент) is None


# ── единый путь чтения и точка записи ──────────────────────────────────────────


def test_conversation_city_slug_федеральной_ссылки_None_а_старая_колонка_нет() -> None:
    conv = Conversation(item_city_slug=None, item_url=ВСЯ_РОССИЯ, item_title="Ремонт")
    assert conversation_city_slug(conv) is None
    assert conversation_city(conv) is None
    out = item_out(conv)
    assert out is not None and out["city_slug"] is None and out["city_name"] is None
    # Диверсия: колонка читается РАНЬШЕ ссылки, и накопленное «all» пережило бы
    # правку разбора — поэтому обязательна миграция 0080, а не сторож на чтении.
    conv.item_city_slug = "all"
    assert conversation_city_slug(conv) == "all"
    старое = item_out(conv)
    assert старое is not None and старое["city_slug"] == "all"


def test_обогащение_федеральной_ссылки_не_пишет_слаг_и_не_шумит() -> None:
    conv = Conversation(item_city_slug=None, item_url=ВСЯ_РОССИЯ)
    with structlog.testing.capture_logs() as логи:
        assert client_enrich._apply_city(conv) == {}
    assert conv.item_city_slug is None
    assert not [л for л in логи if л["event"] == "listing.unknown_city_slug"]

    # Контрпример: слаг вне справочника пишется и попадает в лог ровно как раньше.
    чужой = Conversation(
        item_city_slug=None, item_url="https://avito.ru/zazerkalye/uslugi/remont_8213779975"
    )
    with structlog.testing.capture_logs() as логи:
        патч = client_enrich._apply_city(чужой)
    assert чужой.item_city_slug == "zazerkalye" and патч["city_slug"] == "zazerkalye"
    assert [л["slug"] for л in логи if л["event"] == "listing.unknown_city_slug"] == ["zazerkalye"]


# ── воркер: федеральное объявление ─────────────────────────────────────────────


async def _строка_по_ссылке(
    seed: Any,
    db_sessionmaker: Any,
    found: address_parse.Found,
    *,
    url: str | None,
    слаг: str | None = None,
) -> Any:
    """Как `test_geo_1809._строка`, но диалог получает ССЫЛКУ, а колонку — по
    умолчанию пустую: так строка приходит из вебхука после этой выкатки."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        conv.item_city_slug, conv.item_url = слаг, url
        card = await s.get(Client, seed.client_id)
        card.address = None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        assert записано.candidate_id is not None
        return записано.candidate_id


async def _другой_диалог_клиента(seed: Any, db_sessionmaker: Any, слаг: str) -> None:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="chat-other",
                account_id=conv.account_id,
                client_id=conv.client_id,
                status="closed",
                unread_count=0,
                last_message_at=datetime.now(UTC) - timedelta(days=3),
                item_city_slug=слаг,
            )
        )
        await s.commit()


async def test_страна_федеральное_объявление_вопрос_о_городе_в_журнале(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[]]
    cid = await _строка_по_ссылке(
        seed_conversation, db_sessionmaker, _разобрать("ул Кедровская 4"), url=ВСЯ_РОССИЯ
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    # Ровно один поход — по стране: ни города, ни области у объявления нет.
    assert dadata_отвечает["запросы"] == [
        g.Query(region=None, city=None, settlement=None, street="ул Кедровская", house="4")
    ]
    assert [л["reason"] for л in логи if л["event"] == "geocode.ask_client"] == [g.ASK_CITY]
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_NO_CITY and row.geo_formatted is None
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_страна_федеральное_объявление_город_из_другого_диалога(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    await _другой_диалог_клиента(seed_conversation, db_sessionmaker, "orsk")
    dadata_отвечает["ответы"] = [[дом(street="ул Кедровская", house="4")]]
    cid = await _строка_по_ссылке(
        seed_conversation, db_sessionmaker, _разобрать("ул Кедровская 4"), url=ВСЯ_РОССИЯ
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    запрос = dadata_отвечает["запросы"][0]
    assert (запрос.city, запрос.region) == ("Орск", "Оренбургская область")
    assert not [л for л in логи if л["event"] == "geocode.ask_client"]
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_EXACT and row.geo_formatted == "ул Кедровская, 4, Орск"


async def test_слаг_вне_справочника_другой_диалог_не_читается(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """Контрпример: город в ссылке назван, просто справочник его не знает —
    чужой город сюда не нужен (ревью 15.09), вопроса клиенту нет."""
    await _режим(db_sessionmaker, "nominatim")
    await _другой_диалог_клиента(seed_conversation, db_sessionmaker, "orsk")
    dadata_отвечает["ответы"] = [[]]
    cid = await _строка_по_ссылке(
        seed_conversation,
        db_sessionmaker,
        _разобрать("ул Кедровская 4"),
        url="https://www.avito.ru/kotlas/predlozheniya_uslug/remont_1234567890",
        слаг="kotlas",
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    assert len(dadata_отвечает["запросы"]) == 1
    assert (dadata_отвечает["запросы"][0].region, dadata_отвечает["запросы"][0].city) == (
        None,
        None,
    )
    assert not [л for л in логи if л["event"] == "geocode.ask_client"]


async def test_старое_all_в_колонке_глушит_вопрос_потому_миграция(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """Состояние до 0080 — явно: колонка «all» читается раньше ссылки, воркер
    считает город известным человеку, вопроса нет, город из других диалогов
    не берётся. Это НЕ чинится сторожем на чтении — только миграцией."""
    await _режим(db_sessionmaker, "nominatim")
    await _другой_диалог_клиента(seed_conversation, db_sessionmaker, "orsk")
    dadata_отвечает["ответы"] = [[]]
    cid = await _строка_по_ссылке(
        seed_conversation,
        db_sessionmaker,
        _разобрать("ул Кедровская 4"),
        url=ВСЯ_РОССИЯ,
        слаг="all",
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    assert len(dadata_отвечает["запросы"]) == 1 and dadata_отвечает["запросы"][0].city is None
    assert not [л for л in логи if л["event"] == "geocode.ask_client"]


# ── миграция 0080 ──────────────────────────────────────────────────────────────


def _модуль_миграции() -> Any:
    spec = importlib.util.spec_from_file_location("m0080", МИГРАЦИЯ)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_миграция_снимает_те_же_слаги_что_разбор(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    """Не по тексту исходника: модуль загружается, SQL выполняется на sqlite.
    Без `skipif` намеренно (ревью 19.09): пропажа или перенумерация файла при
    слиянии с соседней сессией обязана краснеть, а не тихо пропускаться."""
    mod = _модуль_миграции()
    assert set(mod.СЛАГИ_СТРАНЫ) == COUNTRY_SEGMENTS
    assert (mod.revision, mod.down_revision) == ("0080", "0079")

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug, conv.item_url = "all", ВСЯ_РОССИЯ
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="chat-orsk",
                account_id=conv.account_id,
                client_id=conv.client_id,
                status="closed",
                unread_count=0,
                last_message_at=datetime.now(UTC),
                item_city_slug="orsk",
            )
        )
        await s.commit()
        await s.execute(sa.text(mod.upgrade_sql()))
        await s.commit()
    async with db_sessionmaker() as s:
        федеральный = await s.get(Conversation, seed_conversation.conversation_id)
        орский = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "chat-orsk")
            )
        ).scalar_one()
        assert (федеральный.item_city_slug, орский.item_city_slug) == (None, "orsk")
