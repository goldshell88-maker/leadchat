"""Пакет 5 (20.09): доля Яндекса починке, монотонность пересуда, города в падеже.

Решение владельца 20.09 — «всё автоматически и точно, точность должна только
расти» — здесь держится тремя участками:

* Q1 — хвост починки получает Яндекс и Саджест ДОЛЕЙ суточного потолка
  (`address_geo.repair_share_yandex`, 30 % по умолчанию), а не «ничего»; ветка
  возраста (реплика старше недели) остаётся только у живой задачи;
* Q2 — пересуд не заменяет улики прежнего вердикта приговором послабее,
  вынесенным без части карт (`geo_prev`, `geocode.keeps_previous`); слепо
  судимая строка остаётся в пересуде и возвращается обходом `kept_blind`, когда
  доля свободна; строки со снимком ставятся в очередь только в квоту Яндекса;
* Q7 — города справочника без дефиса и в предложном падеже («в Салавате»,
  «г Горно Алтайск») попадают в `locality`, а не в чужой пункт.

ДИВЕРСИИ (каждая обязана краснеть): вернуть `ЗАПАС_ЖИВЫМ["yandex"] = 300` —
падает `test_доля_починки_проценты` (270 ≠ 600); поменять ветки origin и
возраста местами — падает `test_починка_старой_строки_идёт_к_яндексу_в_доле`;
`keeps_previous` → всегда False — падает `test_воркер_удерживает_улики_без_яндекса`
(строка станет `not_found/nominatim`); считать «неполный набор» по имени
провайдера снимка — падает `test_воркер_пишет_слабее_при_полном_наборе`; брать
флаг `without_dadata` из снимка в вызове `_пометить` — падает
`test_флаг_без_dadata_по_этому_суду`; стирать `geo_prev` при слепом суде
(`return итог, None` всегда) — падают `test_воркер_удерживает_улики_без_яндекса`,
`test_живая_задача_по_старой_реплике_без_яндекса`, `test_починка_за_долей…`;
не сверять `content` снимка — падает `test_снимок_про_другую_строку_стирается`;
убрать `geo_checked_at IS NOT DISTINCT FROM` из `перепривязать` — падает
`test_перепривязать_оптимистично_пропускает_изменённую_строку`; разрешить
согласным «-ом» — падает `test_инъективность_по_справочнику_и_районам`
(Подольском → Подольск); убрать `replace("-", " ")` из `_норм_имени` — падает
`test_нормализация_дефиса_и_ё`.

Ревью 20.09 (C1–C7), каждая правка со своей диверсией: не ставить отказ
Яндекса при сетевом сбое в `yandex_fallback_failed`/`region_fallback_failed`/
`suggest_recheck_failed`/`place_yandex_failed` или Саджеста в `suggest_failed`
— падают `test_воркер_удерживает_улики_при_сбое_сети_яндекса` (по обработчику),
`test_сбой_яндекса_на_перепроверке_подсказки…`, `test_место_удерживает_точку_при_сбое…`,
`test_сбой_саджеста…` (C1); квота 0 вместо `None` при доле 0 — падают
`test_квота_яндекса_таблица`, `test_обход_при_доле_0…` (C2); счётчики за
сегодня вместо вчера — `test_суточные_счётчики…` (C3); `улики=None` в ручке
провайдера — `test_смена_провайдера_не_стирает_снимок_у_blocked` (C4); сравнение
с дефисом в `_ПУНКТ_ИМЯ_ВПЛОТНУЮ`/`parse_geopoint` — `test_дефисный_город…`,
`test_геоточка…`, `test_все_дефисные_города…` (C5); «-ь» → только «-и», без
ветки «-ое/-ее» или без исключений — `test_склонение_таблица`,
`test_предложные_формы_покрывают_справочник` (C6); захардкодить 30 % в воркере
— `test_воркер_читает_repair_share_из_настроек` (C7).

Адреса — из стендов (Орск/Заречный, Кострома/Малиновка); ПД клиентов нет.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
import typer

from app.core.errors import ApiError
from app.integrations import gateway
from app.integrations.avito import listing_url
from app.integrations.avito.listing_url import CITIES, city_by_name
from app.models import Client, ClientAddressCandidate
from app.models.client import CANDIDATE_ACCEPTED
from app.scheduler.jobs import geo_repair
from app.services import address_parse as ap
from app.services import app_settings, inbound
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_bez_api_1309 as без_api
from tests.unit import test_paket2_requeue_1909 as п2

pytestmark = pytest.mark.anyio

ЧАС = timedelta(hours=1)
ДОМ = без_api.ДОМ
ctx = без_api.ctx
_строка = без_api._строка
_row = без_api._row
# Фикстуры соседних стендов — присваиванием, не импортом имени (ruff F811).
dadata_дом = без_api.dadata_дом
osm_пусто = без_api.osm_пусто
оснастка = п2.оснастка
_строка_обхода = п2._строка_обхода
_ключи_починки = п2._ключи_починки
ВАРИАНТЫ = п2.ВАРИАНТЫ_ПО_ОБЛАСТИ
ТЕКСТ_УЛИЦЫ = "Звенигородская улица, 1, Орск"


# ── оснастка ─────────────────────────────────────────────────────────────────


@pytest.fixture
def яндекс(monkeypatch: Any) -> list[g.Query]:
    """Ключ Яндекса есть; отвечает записанным домом; походы считаются."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    вызовы: list[g.Query] = []

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        вызовы.append(query)
        # Дом с номером клиента: две строки одного стенда — «1» и «3».
        return [dataclasses.replace(ДОМ, house=query.house or ДОМ.house)]

    monkeypatch.setattr(worker.yandex_geocoder, "search", search)
    return вызовы


@pytest.fixture
def dadata_пусто(monkeypatch: Any) -> list[g.Query]:
    """Ключ DaData есть; отвечает пустотой (участвовала, дома не знает)."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    вызовы: list[g.Query] = []

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        вызовы.append(query)
        return []

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    async def место_пусто(место: Any, **kw: Any) -> list[Any]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return []

    monkeypatch.setattr(worker.dadata, "search", search)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    monkeypatch.setattr(worker.dadata, "search_place", место_пусто)
    return вызовы


async def _режим(db_sessionmaker: Any, режим: str = "osm_then_yandex", **ещё: Any) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_PROVIDER: режим, **ещё}, user_id=None
        )
        await s.commit()


async def _состарить(db_sessionmaker: Any, cid: uuid.UUID, дней: int = 10) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.message_at = datetime.now(UTC) - timedelta(days=дней)
        row.detected_at = datetime.now(UTC) - timedelta(days=дней)
        await s.commit()


async def _со_снимком(
    db_sessionmaker: Any,
    cid: uuid.UUID,
    *,
    статус: str = g.GEO_HOUSE_MISSING,
    provider: str = "yandex",
    formatted: str | None = ТЕКСТ_УЛИЦЫ,
    variants: list[dict[str, Any]] | None = None,
    without_dadata: bool = False,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Строка с прежним вердиктом, сброшенная обходом: снимок в `geo_prev`,
    сама строка — `pending` без улик (как после `перепривязать`)."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status = статус
        row.geo_provider = provider
        row.geo_formatted = formatted
        row.geo_variants = variants
        row.geo_lat, row.geo_lon = lat, lon
        row.geo_without_dadata = without_dadata
        row.geo_checked_at = datetime.now(UTC) - 2 * ЧАС
        row.geo_verdict_version = 1
        row.geo_attempts = 1
        снимок = clients_svc.снимок_улик(row, reason="verdict_version")
        for k, v in clients_svc.сброс_вердикта(с_попытками=True, улики=снимок).items():
            setattr(row, k, v)
        await s.commit()
        return снимок


# ── Q1: доля Яндекса и Саджеста починке ──────────────────────────────────────


def test_доля_починки_проценты() -> None:
    """Яндекс и Саджест — процентами; DaData — как была; потолок `None` у
    Яндекса считается за тысячу (ревью 20.09, п. 8). Диверсия: вернуть
    `ЗАПАС_ЖИВЫМ["yandex"]` — 600 вместо 270."""
    assert worker.доля_починки(900, "yandex", yandex_pct=30) == 270
    assert worker.доля_починки(1000, "yandex", yandex_pct=30) == 300
    assert worker.доля_починки(900, "yandex_suggest", yandex_pct=30) == 270
    assert worker.доля_починки(900, "yandex") == 270  # умолчание — 30 %
    assert worker.доля_починки(9000, "dadata", yandex_pct=30) == 7500
    assert worker.доля_починки(None, "yandex", yandex_pct=30) == 300
    assert worker.доля_починки(None, "yandex", yandex_pct=0) == 0
    assert worker.доля_починки(None, "dadata") is None
    assert worker.доля_починки(900, "yandex", yandex_pct=0) == 0
    assert worker.доля_починки(900, "yandex", yandex_pct=100) == 900
    assert "yandex" not in worker.ЗАПАС_ЖИВЫМ and "yandex_suggest" not in worker.ЗАПАС_ЖИВЫМ


async def test_починка_старой_строки_идёт_к_яндексу_в_доле(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, яндекс: Any
) -> None:
    """Реплика −10 дней, `origin=repair`, OSM пуст → Яндекс спрошен в доле, дом
    найден. До пакета ветка возраста стояла раньше origin и обнуляла потолок.
    Диверсия: поменять ветки местами — Яндекс не спрошен, `not_found`."""
    await _режим(db_sessionmaker, **{app_settings.ADDRESS_GEO_DADATA_ENABLED: False})
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _состарить(db_sessionmaker, cid)
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "yandex" and len(яндекс) >= 1
    assert int(await redis.get(worker.yandex_calls_key())) >= 1


async def test_починка_за_долей_без_яндекса_а_живая_с_полным_потолком(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, яндекс: Any
) -> None:
    """Счётчик 270 при потолке 900: задача починки Яндекс не спрашивает, живая
    — спрашивает. Диверсия: доля «потолок − 300» — при 270 обе спросили бы."""
    await _режим(db_sessionmaker, **{app_settings.ADDRESS_GEO_DADATA_ENABLED: False})
    await redis.set(worker.yandex_calls_key(), 270)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR)
    assert яндекс == []
    row = await _row(db_sessionmaker, cid)
    # Слепой суд без снимка тоже помечается: строка ждёт полного набора.
    assert row.geo_status == g.GEO_NOT_FOUND
    assert row.geo_prev is not None and row.geo_prev["missing"] == ["yandex"]
    cid2 = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 3")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2) == g.GEO_EXACT
    assert len(яндекс) == 1 and (await _row(db_sessionmaker, cid2)).geo_prev is None


async def test_живая_задача_по_старой_реплике_без_яндекса(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, яндекс: Any
) -> None:
    """Как `_состарить` в test_paket4_kola_1909: без origin реплика старше
    недели Яндекса не получает (поведение 13.09 сохранено); строка помечена
    слепым снимком — обход `kept_blind` доберёт её долей."""
    await _режим(db_sessionmaker, **{app_settings.ADDRESS_GEO_DADATA_ENABLED: False})
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _состарить(db_sessionmaker, cid)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert яндекс == []
    row = await _row(db_sessionmaker, cid)
    assert row.geo_prev["reason"] == "blind" and row.geo_prev["missing"] == ["yandex"]
    assert row.geo_prev["content"]["value"] == row.value


async def test_воркер_читает_repair_share_из_настроек(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, яндекс: Any
) -> None:
    """Ревью 20.09, C7: проводка `ADDRESS_GEO_REPAIR_SHARE_YANDEX → repair_share_pct
    → доля_починки(yandex_pct=…)` в воркере. Доля 0 при пустом счётчике —
    Яндекс починке не спрошен, снимок `missing=["yandex"]`; доля 100 при
    счётчике 270 (за умолчанием 30 %) — спрошен. Диверсия: захардкодить 30 в
    `доля_яндекса_pct` — первая строка спросила бы Яндекс, вторая — нет."""
    await _режим(
        db_sessionmaker,
        **{
            app_settings.ADDRESS_GEO_DADATA_ENABLED: False,
            app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX: 0,
        },
    )
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_NOT_FOUND
    )
    assert яндекс == []
    row = await _row(db_sessionmaker, cid)
    assert row.geo_prev["reason"] == "blind" and row.geo_prev["missing"] == ["yandex"]
    await _режим(db_sessionmaker, **{app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX: 100})
    await redis.set(worker.yandex_calls_key(), 270)
    cid2 = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 3")
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid2, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    assert len(яндекс) == 1 and (await _row(db_sessionmaker, cid2)).geo_prev is None


def test_настройка_repair_share() -> None:
    spec = app_settings.SPECS[app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX]
    assert (spec.kind, spec.default) == ("count_or_null", 30)
    assert app_settings.repair_share_pct(None) == 30
    assert app_settings.repair_share_pct(150) == 30
    assert app_settings.repair_share_pct(-1) == 30
    assert app_settings.repair_share_pct(True) == 30
    assert app_settings.repair_share_pct(0) == 0
    assert app_settings.repair_share_pct(100) == 100
    assert app_settings.repair_share_pct(45) == 45
    with pytest.raises(ApiError):
        app_settings._validate(spec, 0.3)


async def test_cli_address_limits_repair_share(db_sessionmaker: Any, capsys: Any) -> None:
    """Без аргументов печатает долю процентами; 40 пишет и оставляет след
    `via=cli`; 101 — выход 2 (проценты, не потолок)."""
    from app.cli import run_address_limits
    from app.models import AuditLog

    async with db_sessionmaker() as s:
        await run_address_limits(s, llm=None, dadata=None, yandex=None, suggest=None)
    assert "repair_share_yandex=30%" in capsys.readouterr().out
    async with db_sessionmaker() as s:
        await run_address_limits(
            s, llm=None, dadata=None, yandex=None, suggest=None, repair_share_yandex=40
        )
        assert await app_settings.get(s, app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX) == 40
        след = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .all()
        )
        assert след and след[-1].details["via"] == "cli"
        assert след[-1].details["after"] == {"repair_share_yandex": 40}
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_limits(
                s, llm=None, dadata=None, yandex=None, suggest=None, repair_share_yandex=101
            )
        assert exc.value.exit_code == 2


async def test_view_repair_share_только_чтение(client: Any, tokens: Any) -> None:
    from tests.unit.test_settings_address_detect_1109 import hdr

    res = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert res.status_code == 200 and res.json()["repair_share_yandex"] == 30
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"repair_share_yandex": 50, "suggest_enabled": False},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["repair_share_yandex"] == 30  # поле не в теле PATCH — не правится


async def test_смена_провайдера_не_стирает_снимок_у_blocked(
    client: Any, tokens: Any, seed_conversation: Any, db_sessionmaker: Any
) -> None:
    """Ревью 20.09, C4: строка со снимком от обхода упёрлась в бан OSM —
    `blocked` со снимком (воркер при бане снимок не трогает). Смена провайдера
    возвращает её в `pending`, снимок цел; `blocked` без снимка — без снимка.
    Диверсия: вернуть `улики=None` в ручке — снимок стёрт, первый assert падает."""
    from tests.unit.test_settings_address_detect_1109 import hdr

    со_снимком = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    снимок = await _со_снимком(db_sessionmaker, со_снимком)
    без_снимка = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 3")
    async with db_sessionmaker() as s:
        for cid in (со_снимком, без_снимка):
            row = await s.get(ClientAddressCandidate, cid)
            row.geo_status, row.geo_provider = g.GEO_BLOCKED, "nominatim"
            row.geo_attempts = 2
        await s.commit()
    res = await client.patch(
        "/api/v1/settings/address-detect", json={"provider": "yandex"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200, res.text
    row = await _row(db_sessionmaker, со_снимком)
    assert row.geo_prev == снимок
    assert (row.geo_status, row.geo_provider, row.geo_attempts) == (g.GEO_PENDING, None, 0)
    row = await _row(db_sessionmaker, без_снимка)
    assert row.geo_prev is None and row.geo_status == g.GEO_PENDING
    # Строитель: ключ `geo_prev` есть у всех режимов, кроме явного «не трогать».
    assert "geo_prev" not in clients_svc.сброс_вердикта(
        с_попытками=False, улики=clients_svc.СНИМОК_НЕ_ТРОГАТЬ
    )
    assert clients_svc.сброс_вердикта(с_попытками=False, улики=None)["geo_prev"] is None


def test_cli_recheck_граница_возраста_на_ручке() -> None:
    """Умолчание `--older-than-hours` = граница обхода (27 ч); функция без
    границы — для явных вызовов из кода (ревью 20.09, п. 16)."""
    import inspect

    from app import cli

    assert cli.REQUEUE_MIN_AGE_HOURS == geo_repair.REQUEUE_MIN_AGE.total_seconds() / 3600
    ручка = inspect.signature(cli.address_recheck).parameters["older_than_hours"].default
    assert ручка.default == cli.REQUEUE_MIN_AGE_HOURS == 27
    функция = inspect.signature(cli.run_address_recheck).parameters["older_than_hours"].default
    assert функция == 0


async def test_recheck_с_границей_не_трогает_свежую(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    from app.cli import run_address_recheck

    свежая = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=ЧАС, статус=g.GEO_NOT_FOUND
    )
    давняя = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Мира 7", возраст=28 * ЧАС, статус=g.GEO_NOT_FOUND
    )
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="not_found", dry_run=False, older_than_hours=27)
    assert (await п2._статус(db_sessionmaker, свежая))[0] == g.GEO_NOT_FOUND
    assert (await п2._статус(db_sessionmaker, давняя))[0] == g.GEO_PENDING


# ── Q2: ранг, правило, снимок ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "status", "provider", "lat", "lon", "formatted", "variants", "ожидание"),
    [
        (ap.KIND_HOUSE, g.GEO_EXACT, "dadata", 1.0, 2.0, "ул, 1", None, 5),
        (ap.KIND_HOUSE, g.GEO_EXACT, "dadata~approx", 1.0, 2.0, "ул, 1", None, 4),
        (ap.KIND_PLACE, g.GEO_EXACT, "dadata", 1.0, 2.0, "пос", None, 4),
        (ap.KIND_HOUSE, g.GEO_HOUSE_MISSING, "yandex", None, None, ТЕКСТ_УЛИЦЫ, None, 3),
        (ap.KIND_HOUSE, g.GEO_STREET_MISMATCH, "dadata", None, None, ТЕКСТ_УЛИЦЫ, None, 3),
        (ap.KIND_HOUSE, g.GEO_AMBIGUOUS, "nominatim", None, None, None, ВАРИАНТЫ, 2),
        (ap.KIND_HOUSE, g.GEO_ELSEWHERE, "yandex", None, None, None, ВАРИАНТЫ, 2),
        # Варианты — ранг 2 при любом статусе (ревью 20.09, п. 12).
        (ap.KIND_HOUSE, g.GEO_OTHER_CITY, "dadata", None, None, None, ВАРИАНТЫ, 2),
        (ap.KIND_HOUSE, g.GEO_OTHER_CITY, "dadata", None, None, None, None, 1),
        (ap.KIND_HOUSE, g.GEO_HOUSE_MISSING, "dadata", None, None, None, None, 1),
        (ap.KIND_HOUSE, g.GEO_NOT_FOUND, "nominatim", None, None, None, None, 0),
        (ap.KIND_HOUSE, g.GEO_PENDING, None, None, None, None, None, 0),
        (ap.KIND_HOUSE, g.GEO_AMBIGUOUS, "nominatim", None, None, None, [], 0),
        (ap.KIND_HOUSE, g.GEO_EXACT, "dadata", None, None, "ул, 1", None, 0),
    ],
)
def test_verdict_rank_таблица(
    kind: str,
    status: str,
    provider: str | None,
    lat: float | None,
    lon: float | None,
    formatted: str | None,
    variants: list[dict[str, Any]] | None,
    ожидание: int,
) -> None:
    """Диверсия: поменять 3 и 2 местами — падает пара «текст против вариантов»."""
    assert g.verdict_rank(kind, status, provider, lat, lon, formatted, variants) == ожидание
    assert g.RANK_TEXT > g.RANK_VARIANTS > g.RANK_NAMED_REFUSAL > g.RANK_NONE


@pytest.mark.parametrize(
    ("новый", "прежний", "неполный", "ожидание"),
    [
        (0, 3, True, True),
        (2, 3, True, True),
        (3, 3, True, False),  # равные не удерживают
        (5, 3, True, False),  # сильнее — пишется
        (0, 3, False, False),  # полный набор — карта поправила данные
        (0, 0, True, False),
        (1, 2, False, False),
    ],
)
def test_keeps_previous_таблица(новый: int, прежний: int, неполный: bool, ожидание: bool) -> None:
    assert g.keeps_previous(новый, прежний, неполный) is ожидание


def test_снимок_улик_форма(seed_conversation: Any) -> None:
    row = ClientAddressCandidate(
        id=uuid.uuid4(),
        client_id=seed_conversation.client_id,
        conversation_id=seed_conversation.conversation_id,
        value="ул Ленина, 5",
        street="ул Ленина",
        house="5",
        raw="ул Ленина 5",
        level="A",
        kind=ap.KIND_HOUSE,
        geo_status=g.GEO_HOUSE_MISSING,
        geo_formatted=ТЕКСТ_УЛИЦЫ,
        geo_provider="yandex",
        geo_checked_at=datetime(2026, 9, 19, 8, 11, 2, tzinfo=UTC),
        geo_verdict_version=1,
        geo_without_dadata=True,
    )
    снимок = clients_svc.снимок_улик(row, reason="verdict_version", missing=["yandex"])
    assert tuple(снимок) == clients_svc.СНИМОК_УЛИК
    assert снимок["rank"] == 3 and снимок["checked_at"] == "2026-09-19T08:11:02+00:00"
    assert снимок["content"] == {
        "value": "ул Ленина, 5",
        "settlement": None,
        "settlement_type": None,
        "locality": None,
        "area": None,
    }
    assert снимок["missing"] == ["yandex"] and снимок["without_dadata"] is True
    # Ранг 0 — снимок тоже есть: признак «в пересуде», не только «что удерживать».
    row.geo_status, row.geo_formatted = g.GEO_NOT_FOUND, None
    assert clients_svc.снимок_улик(row, reason="cli_recheck")["rank"] == 0
    # Один набор полей у строителя сброса; `geo_prev` — всегда, и None стирает.
    assert clients_svc.сброс_вердикта(с_попытками=True, улики=None)["geo_prev"] is None
    assert clients_svc.сброс_вердикта(с_попытками=False, улики=снимок)["geo_prev"] is снимок


async def test_обход_кладёт_снимок_и_живой_сброс_его_стирает(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any
) -> None:
    """Обход → `geo_prev` заполнен (ранг 2, варианты), сама строка без улик;
    живое изменение (`settlement_named`) → снимок стёрт — и стёрт в базе
    (`flag_modified`, ревью 20.09 п. 3)."""
    cid = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС)
    assert await geo_repair.repair_geocodes() == 1
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_variants is None
    assert row.geo_prev["rank"] == 2 and row.geo_prev["variants"] == ВАРИАНТЫ
    assert row.geo_prev["reason"] == "verdict_version" and row.geo_prev["status"] == g.GEO_AMBIGUOUS
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        clients_svc.сбросить_вердикт(row, reason="settlement_named")
        await s.commit()
    assert (await _row(db_sessionmaker, cid)).geo_prev is None


async def test_orm_сброс_стирает_снимок_положенный_после_загрузки(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    """Строка загружена с `geo_prev = NULL`, обход положил снимок ПОСЛЕ
    загрузки, живой сброс пишет None: без `flag_modified` UPDATE не задел бы
    колонку и чужой снимок остался бы под новым содержанием."""
    cid = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.geo_prev is None
        async with db_sessionmaker() as другая:
            await другая.execute(
                sa.update(ClientAddressCandidate)
                .where(ClientAddressCandidate.id == cid)
                .values(geo_prev={"status": "чужой"})
            )
            await другая.commit()
        clients_svc.сбросить_вердикт(row, reason="settlement_named")
        await s.commit()
    assert (await _row(db_sessionmaker, cid)).geo_prev is None


async def test_перепривязать_оптимистично_пропускает_изменённую_строку(
    seed_conversation: Any, db_sessionmaker: Any, monkeypatch: Any
) -> None:
    """Между выбором и записью оператор принял строку b → сброшена только a.
    Диверсия: убрать условие `geo_checked_at IS NOT DISTINCT FROM` и `общие`
    из UPDATE — b затёрлась бы."""
    a = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС)
    b = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Мира 7", возраст=28 * ЧАС)
    async with db_sessionmaker() as s:
        настоящий = s.execute

        async def execute(stmt: Any, *args: Any, **kw: Any) -> Any:
            if isinstance(stmt, sa.Update):
                await настоящий(
                    sa.update(ClientAddressCandidate)
                    .where(ClientAddressCandidate.id == b)
                    .values(status=CANDIDATE_ACCEPTED, geo_checked_at=datetime.now(UTC))
                )
            return await настоящий(stmt, *args, **kw)

        monkeypatch.setattr(s, "execute", execute)
        with structlog.testing.capture_logs() as логи:
            ids = await geo_repair.перепривязать(
                s, reason="verdict_version", условие=sa.true(), limit=5
            )
        await s.commit()
    assert ids == [a]
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("verdict_version", 1)
    ]
    assert (await _row(db_sessionmaker, b)).geo_prev is None
    assert (await _row(db_sessionmaker, b)).geo_status == g.GEO_AMBIGUOUS


# ── Q2: воркер ───────────────────────────────────────────────────────────────


async def test_воркер_удерживает_улики_без_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
) -> None:
    """Сценарий синтеза §1.2: `house_missing/yandex` со строкой улицы сброшена
    обходом; доля Яндекса выбрана, DaData и OSM пусты → улики возвращены,
    версия текущая, снимок остался с `missing=["yandex"]` (`kept_blind`
    доберёт), журнал `rejudge_kept`. Диверсия: `keeps_previous` → False —
    строка станет `not_found/nominatim`."""
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    await redis.set(worker.yandex_calls_key(), 900)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert итог == g.GEO_HOUSE_MISSING and яндекс == []
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_HOUSE_MISSING,
        "yandex",
        ТЕКСТ_УЛИЦЫ,
    )
    assert row.geo_verdict_version == g.VERDICT_VERSION and row.geo_attempts == 2
    assert row.geo_prev["reason"] == "kept" and row.geo_prev["missing"] == ["yandex"]
    assert row.geo_prev["rank"] == 3 and row.geo_prev["content"]["value"] == row.value
    (запись,) = [л for л in логи if л["event"] == "geocode.rejudge_kept"]
    assert запись["rank_was"] == 3 and запись["rank_new"] == 0
    assert запись["missing"] == ["yandex"] and запись["reason"] == "verdict_version"
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_TEXT
    )


@pytest.fixture
def яндекс_падает(monkeypatch: Any, request: Any) -> list[str]:
    """Ключ Яндекса есть, доля свободна — а шлюз отвечает ошибкой сети на
    походах с номерами из `request.param` (по умолчанию — на всех); остальные
    походы пусты. Так каждый обработчик отказа проверяется в одиночку: у дома
    Яндекс спрашивают дважды (по городу, потом по области), и падение обоих
    скрыло бы пропуск отказа в одном из них."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    падать_на = set(getattr(request, "param", ()))
    вызовы: list[str] = []

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        вызовы.append(query.free_text)
        if not падать_на or len(вызовы) in падать_на:
            raise g.GeocodeError("yandex", "network")
        return []

    monkeypatch.setattr(worker.yandex_geocoder, "search", search)
    return вызовы


@pytest.mark.parametrize(
    ("яндекс_падает", "обработчик"),
    [
        ({1}, "geocode.yandex_fallback_failed"),  # поход по городу упал, по области пуст
        ({2}, "geocode.region_fallback_failed"),  # по городу пуст, по области упал
    ],
    indirect=["яндекс_падает"],
)
async def test_воркер_удерживает_улики_при_сбое_сети_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс_падает: Any,
    обработчик: str,
) -> None:
    """Ревью 20.09, C1: доля свободна, но Яндекс упал по сети — до правки
    `yandex_fallback_failed`/`region_fallback_failed` отказ не ставили, набор
    считался полным, и `not_found` от OSM стирал улики и снимок. Теперь:
    удержано, снимок `kept` с `missing=["yandex"]` — `kept_blind` доберёт.
    Диверсия: убрать `отказы.яндекс = True` у любого из двух обработчиков —
    его строка станет `not_found/nominatim`, снимок `None`."""
    await _режим(db_sessionmaker)  # ключа Саджеста в снимке шлюза нет — только Яндекс
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert итог == g.GEO_HOUSE_MISSING and len(яндекс_падает) >= 1
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_HOUSE_MISSING,
        "yandex",
        ТЕКСТ_УЛИЦЫ,
    )
    assert row.geo_prev["reason"] == "kept" and row.geo_prev["missing"] == ["yandex"]
    события = [л["event"] for л in логи]
    assert события.count(обработчик) == 1 and "geocode.rejudge_kept" in события
    assert len(яндекс_падает) == 2  # оба похода состоялись, упал ровно один


async def test_сбой_саджеста_считается_неполным_набором(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    monkeypatch: Any,
) -> None:
    """Ревью 20.09, C1: Яндекс в доле и пуст, Саджест упал по сети (не потолок)
    → набор неполный по Саджесту: удержано, `missing=["suggest"]`. Диверсия:
    вернуть `if exc.kind == "limit"` в `suggest_failed` — набор полный,
    `not_found` записан."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)

    async def пусто(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return []

    async def саджест_падает(query: g.Query, **kw: Any) -> list[Any]:
        if kw.get("on_request"):
            await kw["on_request"]()
        raise g.GeocodeError("yandex_suggest", "network")

    monkeypatch.setattr(worker.yandex_geocoder, "search", пусто)
    monkeypatch.setattr(worker.yandex_suggest, "suggest", саджест_падает)
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert итог == g.GEO_HOUSE_MISSING
    row = await _row(db_sessionmaker, cid)
    assert row.geo_prev["reason"] == "kept" and row.geo_prev["missing"] == ["suggest"]
    assert any(л["event"] == "geocode.suggest_failed" for л in логи)


@pytest.mark.parametrize("яндекс_падает", [{2}], indirect=True)
async def test_сбой_яндекса_на_перепроверке_подсказки_неполный_набор(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс_падает: Any,
    monkeypatch: Any,
) -> None:
    """Ревью 20.09, C1, третий обработчик: Саджест исправил улицу, OSM по ней
    пуст, Яндекс на перепроверке упал (поход № 2; по городу и по области он
    пуст) → `suggest_recheck_failed` ставит отказ Яндекса, улики удержаны.
    Диверсия: убрать отказ там — набор полный, `not_found` записан."""
    from app.integrations import yandex_suggest

    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)

    async def suggest(query: g.Query, **kw: Any) -> list[Any]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return [
            yandex_suggest.Suggested(
                street="Звенигородская улица",
                house="1",
                city="Орск",
                settlement=None,
                region="Оренбургская область",
                formatted=None,
            )
        ]

    monkeypatch.setattr(worker.yandex_suggest, "suggest", suggest)
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    события = [л["event"] for л in логи]
    assert события.count("geocode.suggest_recheck_failed") == 1, события
    assert итог == g.GEO_HOUSE_MISSING and len(яндекс_падает) == 3
    row = await _row(db_sessionmaker, cid)
    assert row.geo_prev["reason"] == "kept" and row.geo_prev["missing"] == ["yandex"]


async def test_место_удерживает_точку_при_сбое_яндекса(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_пусто: Any, яндекс_падает: Any
) -> None:
    """Место: прежний `exact` (ранг 4), DaData пуста, Яндекс упал по сети →
    точка возвращена (`place_yandex_failed` ставит отказ, C1). Диверсия: убрать
    `отказы.яндекс = True` там — `not_found` без точки."""
    cid = await _строка(seed_conversation, db_sessionmaker, "посёлок Заречный", место=True)
    await _со_снимком(
        db_sessionmaker,
        cid,
        статус=g.GEO_EXACT,
        provider="dadata",
        formatted="посёлок Заречный, Орск",
        lat=51.22,
        lon=58.47,
    )
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_lat, row.geo_lon) == (51.22, 58.47) and len(яндекс_падает) == 1


async def test_флаг_без_dadata_по_этому_суду(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
) -> None:
    """Снимок сделан без DaData (флаг True); новый суд — с DaData, без Яндекса
    → удержано, но флаг False: DaData участвовала, петли с `dadata_back` нет
    (ревью 20.09, п. 4). Диверсия: брать флаг из снимка — True."""
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(
        db_sessionmaker,
        cid,
        статус=g.GEO_AMBIGUOUS,
        provider="nominatim",
        formatted=None,
        variants=ВАРИАНТЫ,
        without_dadata=True,
    )
    await redis.set(worker.yandex_calls_key(), 900)
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_AMBIGUOUS
    )
    row = await _row(db_sessionmaker, cid)
    assert row.geo_variants == ВАРИАНТЫ and row.geo_without_dadata is False
    assert len(dadata_пусто) >= 1


async def test_воркер_пишет_сильнее_и_стирает_снимок(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_дом: Any,
    яндекс: Any,
) -> None:
    """Тот же снимок ранга 3; DaData находит дом → `exact`, снимок стёрт —
    и стёрт даже при выбранной доле Яндекса: `exact` обход не пересматривает."""
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    await redis.set(worker.yandex_calls_key(), 900)
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider.startswith("dadata") and row.geo_prev is None


async def test_воркер_пишет_слабее_при_полном_наборе(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    monkeypatch: Any,
) -> None:
    """Все карты в деле (Яндекс в доле и пуст), DaData/OSM пусты → `not_found`
    записан, журнал `rejudge_weaker`, снимка нет. Диверсия: считать набор
    неполным по имени провайдера снимка (`yandex`) — удержала бы."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)

    async def пусто(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return []

    monkeypatch.setattr(worker.yandex_geocoder, "search", пусто)
    await _режим(db_sessionmaker)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert итог == g.GEO_NOT_FOUND
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_NOT_FOUND and row.geo_formatted is None
    assert row.geo_prev is None
    события = {л["event"] for л in логи}
    assert "geocode.rejudge_weaker" in события and "geocode.rejudge_kept" not in события


async def test_отложенный_pending_не_трогает_снимок(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_дом: Any
) -> None:
    """DaData за потолком, свежая строка → `pending` (отложено), снимок цел;
    DaData вернулась → пересуд с ним (дом найден, снимок стёрт)."""
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    снимок = await _со_снимком(db_sessionmaker, cid)
    await без_api._потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_prev == снимок
    await redis.delete(worker.dadata_calls_key())
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await _row(db_sessionmaker, cid)).geo_prev is None


async def test_снимок_про_другую_строку_стирается(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_пусто: Any
) -> None:
    """Пункт дописан без сброса (старая реплика): ключ содержания снимка не
    совпал → снимок стёрт и не читается (`rejudge_prev_stale`); при полном
    наборе слабый вердикт записан как есть. Диверсия: не сверять `content` —
    удержался бы текст улицы про строку без пункта."""
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.settlement, row.settlement_type = "Ударник", "посёлок"
        await s.commit()
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог == g.GEO_NOT_FOUND
    row = await _row(db_sessionmaker, cid)
    assert row.geo_prev is None and row.geo_formatted is None
    assert any(л["event"] == "geocode.rejudge_prev_stale" for л in логи)
    assert not any(л["event"] == "geocode.rejudge_kept" for л in логи)


async def test_место_удерживает_точку_без_яндекса(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_пусто: Any, яндекс: Any
) -> None:
    """Место: прежний `exact` (ранг 4), DaData пуста, доля Яндекса выбрана →
    точка возвращена. Место вариантов не пишет (ревью 20.09, п. 12) — держится
    ранг точки, не вариантов."""
    cid = await _строка(seed_conversation, db_sessionmaker, "посёлок Заречный", место=True)
    await _со_снимком(
        db_sessionmaker,
        cid,
        статус=g.GEO_EXACT,
        provider="dadata",
        formatted="посёлок Заречный, Орск",
        lat=51.22,
        lon=58.47,
    )
    await redis.set(worker.yandex_calls_key(), 900)
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_lat, row.geo_lon, row.geo_formatted) == (
        51.22,
        58.47,
        "посёлок Заречный, Орск",
    )
    assert row.geo_prev is None  # `exact` обход не пересматривает — снимок не нужен
    assert яндекс == []


async def test_geo_view_и_card_grade_не_видят_снимок(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    row = await _row(db_sessionmaker, cid)
    вид = clients_svc.geo_view(row)
    assert вид["status"] == g.GEO_PENDING and вид["formatted"] is None
    assert вид["variants"] == [] and clients_svc.candidate_grade(row) is None


async def test_resolve_стирает_снимок(
    seed_conversation: Any, db_sessionmaker: Any, make_user: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    actor = await make_user("p5-admin@test.local", role="admin")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        card = await s.get(Client, row.client_id)
        await clients_svc.resolve_address_candidate(
            s, candidate=row, client=card, decision="reject", actor=actor
        )
        await s.commit()
    assert (await _row(db_sessionmaker, cid)).geo_prev is None


async def test_recheck_кладёт_снимок_и_коммитит_пачками(
    seed_conversation: Any, db_sessionmaker: Any, monkeypatch: Any
) -> None:
    from app import cli

    monkeypatch.setattr(cli, "_ПАЧКА_СБРОСА", 2)
    ids = [
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            f"ул Ленина {n}",
            возраст=28 * ЧАС,
            статус=g.GEO_HOUSE_MISSING,
            geo_formatted=f"улица Ленина, {n}, Саранск",
        )
        for n in range(1, 6)
    ]
    async with db_sessionmaker() as s:
        коммиты: list[int] = []
        настоящий = s.commit

        async def commit() -> None:
            коммиты.append(1)
            await настоящий()

        monkeypatch.setattr(s, "commit", commit)
        with structlog.testing.capture_logs() as логи:
            await cli.run_address_recheck(s, statuses="house_missing", dry_run=False)
    assert len(коммиты) >= 3
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("cli_recheck", 5)
    ]
    for cid in ids:
        row = await _row(db_sessionmaker, cid)
        assert row.geo_status == g.GEO_PENDING and row.geo_formatted is None
        assert row.geo_prev["rank"] == 3 and row.geo_prev["reason"] == "cli_recheck"


# ── Q1/Q2: обход — квота Яндекса и слепые строки ─────────────────────────────


def test_квота_яндекса_таблица(monkeypatch: Any) -> None:
    """Доля 0 (`repair_share_yandex=0` или потолок 0) при ключе — квоты нет
    (`None`), как и без ключа: ждать нечего (ревью 20.09, C2). Диверсия:
    вернуть `max(0, …)` без ветки `доля <= 0` — 0 вместо None."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    assert geo_repair.квота_яндекса(900, 0, yandex_pct=30) == 90
    assert geo_repair.квота_яндекса(900, 269, yandex_pct=30) == 0
    assert geo_repair.квота_яндекса(900, 270, yandex_pct=30) == 0
    assert geo_repair.квота_яндекса(900, 900, yandex_pct=30) == 0
    assert geo_repair.квота_яндекса(None, 0, yandex_pct=30) == 100
    assert geo_repair.квота_яндекса(900, 0, yandex_pct=0) is None
    assert geo_repair.квота_яндекса(0, 0, yandex_pct=30) is None
    # Доля меньше походов одной строки (3) — та же «квота 0 навсегда»: None.
    assert geo_repair.квота_яндекса(9, 0, yandex_pct=30) is None  # доля 2
    assert geo_repair.квота_яндекса(10, 0, yandex_pct=30) == 1  # доля 3
    assert geo_repair.квота_яндекса(10, 1, yandex_pct=30) == 0  # доля есть, выбрана
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", False)
    assert geo_repair.квота_яндекса(900, 0, yandex_pct=30) is None


async def test_обход_ставит_строки_со_снимком_в_квоту_яндекса(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    """Доля выбрана (270/900): `pending` со снимком не ставится, без снимка —
    ставится, триггерных сбросов нет, `kept_blind` на паузе; новые сутки —
    всё ставится. Диверсия: не считать снимок в `найти_непроверенные` — обе
    `pending` встали бы при выбранной доле."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    со_снимком = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, со_снимком)
    без_снимка = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 3")
    триггерная = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Мира 7", возраст=28 * ЧАС
    )
    await redis.set(worker.yandex_calls_key(), 270)
    with structlog.testing.capture_logs() as логи:
        assert await geo_repair.repair_geocodes() == 1
    assert await _ключи_починки(redis) == {f"arq:job:geocode:{без_снимка}:repair"}
    assert (await п2._статус(db_sessionmaker, триггерная))[0] == g.GEO_AMBIGUOUS
    assert any(л.get("paused") == "kept_blind" for л in логи if л["event"] == "geo.repair")
    # Новые московские сутки: счётчик пуст — квота 90, остальные в очередь
    # (ключ первой строки ещё стоит — дедуп, в счёт не идёт).
    await redis.delete(worker.yandex_calls_key(), "arq:queue")
    assert await geo_repair.repair_geocodes() == 2
    assert await _ключи_починки(redis) == {
        f"arq:job:geocode:{cid}:repair" for cid in (без_снимка, со_снимком, триггерная)
    }
    assert (await п2._статус(db_sessionmaker, триггерная))[0] == g.GEO_PENDING


async def test_обход_при_доле_0_ставит_снимок_и_не_ждёт_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    оснастка: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
) -> None:
    """Ревью 20.09, C2/C7: ключ Яндекса есть, `repair_share_yandex=0`. Квота
    `None` — `pending` со снимком ставится (не ждёт долю, которой нет), триггер
    `verdict_version` не ограничен пределом 0, `kept_blind` на паузе с причиной
    `yandex_share_zero`; воркер судит строку без Яндекса и удерживает улики
    (снимок `kept`, `missing=["yandex"]`). Диверсия: квота 0 при доле 0 — обе
    строки остались бы `pending`, а сброс версии не случился бы."""
    await _режим(db_sessionmaker, **{app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX: 0})
    со_снимком = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, со_снимком)
    триггерная = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Мира 7", возраст=28 * ЧАС
    )
    with structlog.testing.capture_logs() as логи:
        assert await geo_repair.repair_geocodes() == 2
    assert await _ключи_починки(redis) == {
        f"arq:job:geocode:{cid}:repair" for cid in (со_снимком, триггерная)
    }
    assert (await п2._статус(db_sessionmaker, триггерная))[0] == g.GEO_PENDING
    паузы = [л.get("reason") for л in логи if л["event"] == "geo.repair" and "paused" in л]
    assert паузы == ["yandex_share_zero"]
    # Воркер: суд без Яндекса, улики удержаны, строка помечена слепой.
    итог = await worker.geocode_candidate(
        ctx(db_sessionmaker, redis), со_снимком, origin=worker.ORIGIN_REPAIR
    )
    assert итог == g.GEO_HOUSE_MISSING and яндекс == []
    row = await _row(db_sessionmaker, со_снимком)
    assert (row.geo_status, row.geo_formatted) == (g.GEO_HOUSE_MISSING, ТЕКСТ_УЛИЦЫ)
    assert row.geo_prev["reason"] == "kept" and row.geo_prev["missing"] == ["yandex"]


async def test_kept_blind_возвращает_слепую_строку(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    """Слепо судимая строка (снимок с `missing`, версия текущая) — не видна
    ни `verdict_version`, ни `dadata_back`; `kept_blind` берёт её через 20 ч,
    свежую (1 ч) — нет. Диверсия: стирать `geo_prev` при удержании — строка
    не вернулась бы никогда."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    давняя = await _строка_обхода(
        db_sessionmaker,
        seed_conversation,
        "ул Ленина 5",
        возраст=28 * ЧАС,
        статус=g.GEO_HOUSE_MISSING,
        версия=g.VERDICT_VERSION,
        geo_formatted="улица Ленина, 5, Саранск",
        geo_checked_at=datetime.now(UTC) - 21 * ЧАС,
    )
    свежая = await _строка_обхода(
        db_sessionmaker,
        seed_conversation,
        "ул Мира 7",
        возраст=28 * ЧАС,
        статус=g.GEO_HOUSE_MISSING,
        версия=g.VERDICT_VERSION,
        geo_formatted="улица Мира, 7, Саранск",
        geo_checked_at=datetime.now(UTC) - ЧАС,
    )
    async with db_sessionmaker() as s:
        for cid in (давняя, свежая):
            row = await s.get(ClientAddressCandidate, cid)
            row.geo_prev = clients_svc.снимок_улик(row, reason="blind", missing=["yandex"])
        await s.commit()
    with structlog.testing.capture_logs() as логи:
        assert await geo_repair.repair_geocodes() == 1
    assert (await п2._статус(db_sessionmaker, давняя))[0] == g.GEO_PENDING
    assert (await п2._статус(db_sessionmaker, свежая))[0] == g.GEO_HOUSE_MISSING
    row = await _row(db_sessionmaker, давняя)
    assert row.geo_prev["reason"] == "kept_blind" and row.geo_prev["rank"] == 3
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("kept_blind", 1)
    ]


async def test_суточные_счётчики_раз_в_сутки_и_за_вчера(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any
) -> None:
    """Строка расхода — одна на сутки и ЗА ВЧЕРА (ревью 20.09, C3): первый заход
    после полуночи сегодняшних ключей не видит, а вчерашние ещё живы. Диверсия:
    вернуть ключи сегодняшнего дня — `yandex=0`, `day` сегодняшний."""
    вчера = datetime.now(UTC) - timedelta(days=1)
    await redis.set(worker.yandex_calls_key(вчера), 500)
    await redis.set(worker.dadata_calls_key(вчера), 1200)
    await redis.set(worker.suggest_calls_key(вчера), 7)
    await redis.set(worker.yandex_calls_key(), 3)  # сегодняшний — не в строку
    with structlog.testing.capture_logs() as логи:
        await geo_repair.repair_geocodes()
        await geo_repair.repair_geocodes()
    строки = [л for л in логи if л["event"] == "geocode.daily_counters"]
    assert len(строки) == 1
    (строка,) = строки
    assert (строка["dadata"], строка["yandex"], строка["suggest"]) == (1200, 500, 7)
    assert строка["day"] == worker._день_яндекса(вчера) != worker._день_яндекса()
    assert строка["repair_share_yandex"] == 30
    assert await redis.get(f"geo:daily_counters_logged:{worker._день_яндекса(вчера)}") is not None


# ── Q7: города справочника без дефиса и в падеже ─────────────────────────────


def test_нормализация_дефиса_и_ё() -> None:
    """Диверсия: убрать `replace("-", " ")` — «Горно Алтайск» не найдётся."""
    for имя in ("Горно Алтайск", "горно-алтайск", "Горно — Алтайск", "ГОРНО-АЛТАЙСК"):
        assert city_by_name(имя) is not None and city_by_name(имя).name == "Горно-Алтайск", имя
    assert city_by_name("Орел") is city_by_name("Орёл") is not None
    assert "горно алтайск" in ap._имена_городов()
    assert "горно-алтайск" not in ap._имена_городов()


def test_дефисный_город_в_голове_идёт_в_locality() -> None:
    """Ревью 20.09, C5: `_имена_городов()` без дефиса, а читатель
    `_ПУНКТ_ИМЯ_ВПЛОТНУЮ` в рекурсии `_без_города` сравнивал с дефисом —
    «Санкт-Петербург ул Ленина 5» уходил пунктом, `locality` пуст. Диверсия:
    вернуть `.lower().replace("ё", "е")` на строке 1862 — падает."""
    found = ap.parse("Санкт-Петербург ул Ленина 5")
    assert found is not None
    assert (found.locality, found.settlement) == ("Санкт-Петербург", None)
    found = ap.parse("Горно-Алтайск ул Ленина 5")
    assert found is not None
    assert (found.locality, found.settlement) == ("Горно-Алтайск", None)


def test_геоточка_дефисный_город() -> None:
    """Ревью 20.09, C5: `parse_geopoint` сравнивал кусок с дефисом — «Санкт-
    Петербург, 5» рождал строку дома с улицей «Санкт-Петербург». Диверсия:
    вернуть сравнение без `_норм_города` — первый assert падает."""
    assert ap.parse_geopoint("Санкт-Петербург, 5") is None
    found = ap.parse_geopoint("Россия, Санкт-Петербург, Невский проспект, 28")
    assert found is not None
    assert (found.locality, found.settlement, found.street, found.house) == (
        "Санкт-Петербург",
        None,
        "Невский проспект",
        "28",
    )


#: Дефисные имена, которых образец головы реплики (`_ГОРОД_ВНАЧАЛЕ`: два слова
#: с заглавной) не видит: три части через строчную «на», цифра. Они и до
#: пакета городом в голове не читались — сторож требует лишь, чтобы имя не
#: стало пунктом или улицей.
_ДЕФИСНЫЕ_ВНЕ_ОБРАЗЦА_ГОЛОВЫ = frozenset(
    {"Ростов-на-Дону", "Комсомольск-на-Амуре", "Славянск-на-Кубани", "Янино-1"}
)


def test_все_дефисные_города_через_оба_читателя() -> None:
    """Прогон всех дефисных имён справочника через `parse` и `parse_geopoint`:
    имя города никогда не пункт и не улица; где образец головы его видит —
    оно `locality`; у геоточки — всегда `locality`."""
    дефисные = sorted({c.name for c in CITIES.values() if "-" in c.name and " и " not in c.name})
    assert len(дефисные) >= 15
    for имя in дефисные:
        found = ap.parse(f"{имя} ул Ленина 5")
        assert found is not None, имя
        assert found.settlement is None and found.street == "ул Ленина", (имя, found)
        if имя in _ДЕФИСНЫЕ_ВНЕ_ОБРАЗЦА_ГОЛОВЫ:
            assert found.locality is None, (имя, found.locality)
        else:
            assert found.locality == имя, (имя, found.locality)
        assert ap.parse_geopoint(f"{имя}, 5") is None, имя
        точка = ap.parse_geopoint(f"Россия, {имя}, Невский проспект, 28")
        assert точка is not None and точка.locality == имя, (имя, точка)
        assert точка.settlement is None and точка.street == "Невский проспект", (имя, точка)


@pytest.mark.parametrize(
    ("форма", "ожидание"),
    [
        ("Орске", "Орск"),
        ("Орле", None),  # беглая гласная — вне пакета, и это не Орск
        ("Салавате", "Салават"),
        ("Салаватом", None),  # творительный не принимается
        ("Уфе", "Уфа"),
        ("Твери", "Тверь"),
        ("Казани", "Казань"),
        ("Грозном", "Грозный"),
        ("Клину", "Клин"),
        ("Химках", "Химки"),
        ("Евпатории", "Евпатория"),
        ("Набережных Челнах", "Набережные Челны"),
        ("Нижнем Новгороде", "Нижний Новгород"),
        ("Ростове на Дону", "Ростов-на-Дону"),
        ("Горно Алтайске", "Горно-Алтайск"),
        ("Дзержинске", "Дзержинск"),
        ("Дзержинском", "Дзержинский"),  # разводятся сами: предложный разный
        ("Новгороде", None),  # два города, предыдущее слово обязательно
        ("Кировске", None),  # Кировска в справочнике нет — и не Киров
        ("Кирова", None),  # родительный = фамилия улицы: не принимается
        ("Кировская", None),
        ("Подольском", None),  # существительное на «-ск» + «ом» — не предложный
        ("Хабаровском", None),
        ("Ногинском", None),
        ("Сочи", "Сочи"),
        ("Иванове", None),  # «-о» не склоняем
        # Ревью 20.09, C6 — мужской род на «-ь», средний род «-ое/-ее»,
        # существительное на «-ой», короткое множественное.
        ("Ярославле", "Ярославль"),
        ("Ставрополе", "Ставрополь"),
        ("Симферополе", "Симферополь"),
        ("Севастополе", "Севастополь"),
        ("Анадыре", "Анадырь"),
        ("Перми", "Пермь"),
        ("Новом Уренгое", "Новый Уренгой"),
        ("Новом Уренгом", None),  # прилагательное из «Уренгоя» не делаем
        ("Раменском", "Раменское"),
        ("Видном", "Видное"),
        ("Новом Девяткино", "Новое Девяткино"),
        ("Великих Луках", "Великие Луки"),
        ("Луках", None),  # без «Великих» — не город
    ],
)
def test_склонение_таблица(форма: str, ожидание: str | None) -> None:
    """Диверсия: добавить согласным «-ом» — «Подольском → Подольск» ловит;
    убрать «-е» у «-ь» — «Ярославле» → None; убрать ветку «-ое/-ее» —
    «Раменском» → None; убрать исключения — «Новом Уренгое», «Великих Луках»."""
    город = city_by_name(форма, declined=True)
    assert (город.name if город is not None else None) == ожидание


#: Имена, у которых предложной формы по правилам нет и не будет: «-о/-е»
#: несклоняемые (Иваново, Туапсе), «Сочи», имя с цифрой. Любое другое имя
#: справочника обязано находиться хотя бы одной своей формой.
def _без_предложной_формы(имя: str) -> bool:
    последнее = имя.split()[-1].lower().replace("ё", "е")
    return последнее[-1] in "ое" or имя == "Сочи" or any(ch.isdigit() for ch in имя)


def test_предложные_формы_покрывают_справочник() -> None:
    """Каждый склоняемый город справочника находится в предложном падеже
    (ревью 20.09, C6: девять из 251 — Ярославль, Новый Уренгой, Великие Луки,
    Раменское… — не находились, «заявленное в падеже» для них не работало).
    Диверсия: вернуть «-ь» → только «-и» — Ярославль выпадает."""
    именительные = listing_url._по_имени()
    формы = listing_url._по_форме()
    находятся = {id(c) for c in формы.values() if c is not None}
    пропуски = [
        город.name
        for город in именительные.values()
        if id(город) not in находятся and not _без_предложной_формы(город.name)
    ]
    assert пропуски == []
    # И сами исключённые — действительно без форм (список не шире нужного).
    исключённые = [c.name for c in именительные.values() if _без_предложной_формы(c.name)]
    assert 15 <= len(исключённые) <= 30 and "Иваново" in исключённые


#: Прилагательные районов, округов, краёв и областей (ревью 20.09, п. 2): ни
#: одно не должно находить город справочника в предложном падеже.
_РАЙОНЫ_ПРИЛАГАТЕЛЬНЫЕ = """Подольском Ногинском Красногорском Всеволожском Солнечногорском
Наро-Фоминском Троицком Новомосковском Красноармейском Хабаровском Красноярском Иркутском
Омском Томском Новосибирском Брянском Смоленском Ленинском Энгельсском Одинцовском Кировском
Калининском Советском Центральном Первомайском Пролетарском Заводском Индустриальном
Промышленном Железнодорожном Фрунзенском Куйбышевском Дзержинском Приморском Невском
Выборгском Курортном Петроградском Адмиралтейском Василеостровском Калужском Тульском
Рязанском Тверском Ярославском Владимирском Ивановском Костромском Вологодском Псковском
Новгородском Мурманском Архангельском Пермском Свердловском Челябинском Курганском Тюменском
Алтайском Кемеровском Забайкальском Амурском Сахалинском Камчатском Магаданском Чукотском
Якутском Бурятском Тывинском Хакасском Ставропольском Краснодарском Ростовском Волгоградском
Астраханском Саратовском Пензенском Ульяновском Самарском Оренбургском Башкирском Татарском
Чувашском Марийском Мордовском Удмуртском Кировском Нижегородском Липецком Тамбовском
Воронежском Белгородском Курском Орловском""".split()


def test_инъективность_по_справочнику_и_районам() -> None:
    """Каждая порождённая форма ведёт ровно к своему городу либо к None; ни
    одна не равна именительному другого города; прилагательные районов
    находят город только если он сам — прилагательное (Дзержинский,
    Кировский…, и их страхует сторож следующего слова в `inbound`).
    Диверсия: вернуть `лок[:-2] == w` или «-ом» согласным — падает."""
    именительные = listing_url._по_имени()
    формы = listing_url._по_форме()
    assert set(формы) & set(именительные) == set()
    for имя, город in именительные.items():
        for слово in имя.split():
            for ф in listing_url._предложные_формы(слово):
                assert ф not in именительные or именительные[ф] is город, (имя, ф)
    # Все формы однозначны (после отказа от творительного неоднозначных нет).
    assert [ф for ф, c in формы.items() if c is None] == []
    прилагательные = {
        c.name for c in именительные.values() if ap._ГОРОД_ПРИЛАГАТЕЛЬНОЕ.search(c.name.lower())
    }
    for район in _РАЙОНЫ_ПРИЛАГАТЕЛЬНЫЕ:
        город = city_by_name(район, declined=True)
        # Только город-прилагательное (Дзержинский, Кировский…) — и того
        # страхует сторож следующего слова в `inbound`; существительное на
        # «-ск» (Подольск, Хабаровск, Ногинск) не находится никогда.
        assert город is None or город.name in прилагательные, (район, город)
    assert all(
        city_by_name(р, declined=True) is None
        for р in ("Подольском", "Хабаровском", "Ногинском", "Красногорском", "Иркутском")
    )
    assert len(CITIES) >= 250  # сторож живёт на всём справочнике, не на выборке


def test_declined_false_по_умолчанию_точный() -> None:
    """Без `declined` — точное имя: первое слово улицы («Орске Победы») и
    имя человека городом не становятся."""
    assert city_by_name("Салавате") is None and city_by_name("Салават") is not None
    assert city_by_name("Орске") is None
    # `street_head_as_settlement` зовёт точный режим: «Орск Победы» — город
    # справочника, не пункт (край Б0); «Орске Победы» точным режимом городом
    # не считается — читается пунктом, как до пакета (склонение туда не идёт).
    точный = g.Parsed(street="Орск Победы", house="5", settlement=None, settlement_type=None)
    assert g.street_head_as_settlement(точный, city_by_name("Уфа")) is None
    в_падеже = g.Parsed(street="Орске Победы", house="5", settlement=None, settlement_type=None)
    assert g.street_head_as_settlement(в_падеже, city_by_name("Уфа")) == ("Орске", "Победы")


def test_в_салавате_становится_пунктом_салават() -> None:
    """«в Салавате» → `ПунктКонтекста(имя="Салават")`; строка дома получает
    `locality="Салават"`, `settlement` пуст, цитата — как написал клиент.
    Диверсия: не канонизировать — `settlement="Салавате"`, класс «два пути»."""
    пункт = inbound._пункт_в_реплике("я в Салавате живу")
    assert пункт is not None and (пункт.имя, пункт.цитата) == ("Салават", "в Салавате")
    found = ap.parse("ул Ленина 5")
    assert found is not None
    с_пунктом = inbound.с_пунктом_контекста(found, пункт)
    assert (с_пунктом.locality, с_пунктом.settlement) == ("Салават", None)
    assert с_пунктом.raw == "в Салавате; ул Ленина 5"
    assert с_пунктом.value == found.value  # ключ строки не меняется


@pytest.mark.parametrize(
    ("текст", "имя"),
    [
        ("живу в Горно Алтайске", "Горно-Алтайск"),
        ("мы в Набережных Челнах", "Набережные Челны"),
        ("в Нижнем Новгороде, на Ленина", "Нижний Новгород"),
        ("в Сукко, в однокомнатную квартиру", "Сукко"),
        ("в Дзержинском", "Дзержинском"),  # город-прилагательное — как написано
    ],
)
def test_пункт_в_реплике_формы(текст: str, имя: str) -> None:
    пункт = inbound._пункт_в_реплике(текст)
    assert пункт is not None and пункт.имя == имя


@pytest.mark.parametrize(
    "текст",
    [
        "мы в Подольском районе",
        "в Ногинском районе, Ленина 5",
        "в Октябрьском районе",
        "в Московском округе",
        "в Красноярском крае",
        "в Ленинском р-не",
        "приеду в Октябре",
        "в Целом всё ок",
    ],
)
def test_район_за_в_не_пункт(текст: str) -> None:
    """Сторож следующего слова: «в <Имя>ском районе/крае/округе» — не пункт
    вовсе (сегодня был ложный `settlement`). Диверсия: убрать
    `_ЗА_ПУНКТОМ_НЕ_ПУНКТ` — «Ногинском» пошло бы пунктом."""
    assert inbound._пункт_в_реплике(текст) is None


def test_г_салавате_locality() -> None:
    """«г Салавате, Ленина 5» → `locality="Салават"`, `value` прежний."""
    found = ap.parse("г Салавате, Ленина 5")
    assert found is not None and found.locality == "Салават"
    assert found.value == "Ленина, 5"
    found2 = ap.parse("г Горно Алтайск, Ленина 5")
    assert found2 is not None and found2.locality == "Горно-Алтайск"
    # Не из справочника — как написано (поведение до пакета).
    found3 = ap.parse("г Цимлянск, ул Ленина 5")
    assert found3 is not None and found3.locality == "Цимлянск"


def test_кирова_в_начале_реплики_улица_не_город() -> None:
    """`_город_объявлений` не склоняет: «Кирова 5» — улица (родительный =
    фамилия), город Киров не подставляется."""
    found = ap.parse("Кирова 5, подъезд 2")
    assert found is not None and found.locality is None and found.street.lower() == "кирова"


async def test_воркер_находит_город_у_старой_строки(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_дом: Any, osm_пусто: Any
) -> None:
    """Строка до пакета с `locality="Салавате"` при объявлении без города:
    воркер берёт город Салават (запрос к карте с его регионом), а не `no_city`."""
    from app.models import Conversation

    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.locality = "Салавате"
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = None
        conv.item_url = None
        await s.commit()
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    # Город найден: запрос ушёл в регион Салавата, а не в «город неизвестен»
    # (ответ стенда — дом в Орске, потому вердикт — спор региона, не `exact`).
    assert итог != g.GEO_NO_CITY
    салават = city_by_name("Салават")
    assert салават is not None and dadata_дом and dadata_дом[0].region == салават.region
    assert dadata_дом[0].city == "Салават"


# ── Бой 20.09: JSON `null` в geo_prev ≠ «снимка нет» ─────────────────────────


async def test_сброс_без_улик_даёт_sql_null_а_не_json_null(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    """Бой 20.09: после `address-reparse` восемь строк лежали `pending` с
    `geo_prev = 'null'::jsonb` — обычный `JSON` пишет Python `None` как JSON
    `null`, `IS NOT NULL` по нему истинен, обход считал строку «со снимком»,
    ставил её в квоту Яндекса (доля выбрана) и не заводил в очередь. Колонка —
    `JSONBNullable`: `None` → SQL NULL. Диверсия: вернуть `JSONB` у `geo_prev`
    — `найти_непроверенные` отдаст снимок True, и при выбранной доле строка не
    встанет в очередь."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _со_снимком(db_sessionmaker, cid)
    # Сброс без улик — как reparse/живое изменение содержания.
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(ClientAddressCandidate)
            .where(ClientAddressCandidate.id == cid)
            .values(**clients_svc.сброс_вердикта(с_попытками=False, улики=None))
        )
        await s.commit()
    async with db_sessionmaker() as s:
        есть_снимок = (
            await s.execute(
                sa.select(ClientAddressCandidate.geo_prev.is_not(None)).where(
                    ClientAddressCandidate.id == cid
                )
            )
        ).scalar()
    assert есть_снимок is False
    строки = {r[0]: r[4] for r in await геo_repair_найти(db_sessionmaker)}
    assert строки[cid] is False
    # Доля Яндекса выбрана — строка без снимка всё равно ставится.
    await redis.set(worker.yandex_calls_key(), 270)
    assert await geo_repair.repair_geocodes() == 1
    assert await _ключи_починки(redis) == {f"arq:job:geocode:{cid}:repair"}


async def геo_repair_найти(db_sessionmaker: Any) -> list[Any]:
    async with db_sessionmaker() as s:
        return await geo_repair.найти_непроверенные(s)
