"""Потолок DaData во вторичных шагах цепочки — отказ, а не молчаливый пропуск (24.09).

Предпроверка `_в_пределах` перед шагами (место пункта, точка массива, голова
дроби, область, точка города) пропускала шаг без флага «без DaData»: строка
оставалась, например, `elsewhere` навсегда — ни один обход её больше не брал.
И неделю лежавший в кэше ответ за потолком не читался вовсе, хотя кэш по
замыслу бесплатен. Теперь шаг идёт в интеграцию: из кэша — даром, на промахе
`_посчитать_dadata` бросает `limit`, и шаг ставит «без DaData».

Сценарии — те же, что в пакете 4 (Кленово, Кола); потолок равен числу
походов до проверяемого шага.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from app.services import app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_paket4_kola_1909 as kola
from tests.unit import test_paket4_neighbour_1909 as neighbour
from tests.unit.test_geo_1809 import _row, _разобрать, _режим, _строка, ctx

# Фикстуры наборов Колы и соседа — присваиванием, как у самого набора Колы:
# импорт под тем же именем, что у параметра теста, ruff читает переопределением.
dadata_очередь = kola.dadata_очередь
osm_отвечает = kola.osm_отвечает
место_отвечает = kola.место_отвечает
яндекс_отвечает = kola.яндекс_отвечает
dadata_отвечает = neighbour.dadata_отвечает
osm_пусто = neighbour.osm_пусто
точка_города = neighbour.точка_города
точка_пункта = neighbour.точка_пункта

pytestmark = pytest.mark.anyio


async def _set_dadata_ceiling(db_sessionmaker: Any, n: int) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT: n}, user_id=None
        )
        await s.commit()


async def _klenovo_row(seed_conversation: Any, db_sessionmaker: Any) -> Any:
    await _режим(db_sessionmaker, "nominatim")
    return await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )


async def test_the_neighbour_step_over_the_ceiling_is_a_refusal_not_a_verdict(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    точка_города["точка"] = neighbour.ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[neighbour.ЛИПОВКА_17]]
    cid = await _klenovo_row(seed_conversation, db_sessionmaker)
    # Город, круг и область выбирают потолок ровно — место «Кленово» уже за ним.
    await _set_dadata_ceiling(db_sessionmaker, 3)

    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)

    row = await _row(db_sessionmaker, cid)
    assert row.geo_without_dadata is True, "шаг за потолком обязан ставить «без DaData»"
    assert row.geo_prev is not None, "прежний вердикт сохранён для пересмотра"


async def test_a_cached_neighbour_answer_works_beyond_the_ceiling(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    точка_города["точка"] = neighbour.ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[neighbour.ЛИПОВКА_17]]
    cid = await _klenovo_row(seed_conversation, db_sessionmaker)
    await _set_dadata_ceiling(db_sessionmaker, 3)

    async def from_cache(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        # Ответ из кэша: интеграция не зовёт `on_request` — поход не состоялся.
        точка_пункта["походы"].append((place, region, kw.get("city")))
        return list(точка_пункта["ответ"])

    monkeypatch.setattr(worker.dadata, "search_place", from_cache)

    with structlog.testing.capture_logs() as logs:
        outcome = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)

    assert outcome == g.GEO_EXACT
    assert [e.get("rule") for e in logs if e["event"] == "geocode.auto_decided"] == [
        "neighbour_settlement"
    ]


async def test_the_place_by_the_first_street_word_over_the_ceiling_is_flagged(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    await _режим(db_sessionmaker, "osm_then_yandex")
    точка_города["точка"] = kola.ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [], [], [kola.КОЛА_4_DADATA]]
    cid = await kola._строка_колы(seed_conversation, db_sessionmaker)
    await kola._состарить(db_sessionmaker, cid)
    # Пять походов (город, голова, круг, область, голова по области) — ровно
    # потолок; место «Кола» по первому слову улицы — уже за ним.
    await _set_dadata_ceiling(db_sessionmaker, 5)

    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)

    row = await _row(db_sessionmaker, cid)
    assert row.geo_without_dadata is True
    assert место_отвечает["походы"] == [], "за потолком поход не состоялся"
