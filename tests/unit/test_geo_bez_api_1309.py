"""Работа без API (владелец 13.09: «у нас сейчас не работают Яндекс и DaData»).

Что случилось в бою: догон хвоста выел суточный потолок DaData к половине
одиннадцатого утра, Яндекс — к пяти; до полуночи каждая новая реплика шла по
одному OSM, а тот на половину домов России отвечает «не найдено» — и это
ложилось в строку окончательным приговором. Здесь закрыты четыре щели:

* отказ карты без DaData — не приговор: строка остаётся `pending`, попытка
  не тратится, починка вернётся к ней, когда DaData снова в потолке;
* место без DaData на сегодня — ждёт, а не «не найдено» (без ключа вовсе —
  как раньше, «не найдено»);
* починка не заходит в запас живого потока: её задачи считают потолок как
  `потолок − запас`, а сама она молчит, когда счётчик дошёл до доли починки;
* наш потолок — `limit`, не `blocked`: строку не осуждает, счётчик честный,
  режим `yandex` за потолком уходит в OSM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations import gateway
from app.models import Client, ClientAddressCandidate, Conversation
from app.scheduler.jobs import geo_repair
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

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


def ctx(db_sessionmaker: Any, redis: Any) -> dict:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def _строка(seed: Any, db_sessionmaker: Any, текст: str, *, место: bool = False) -> Any:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed.client_id)
        card.address = None
        found = address_parse.parse_place(текст) if место else address_parse.parse(текст)
        assert found is not None
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
        return записано.candidate_id


async def _row(db_sessionmaker: Any, cid: Any) -> ClientAddressCandidate:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        return row


@pytest.fixture
def osm_пусто(monkeypatch: Any) -> list[g.Query]:
    вызовы: list[g.Query] = []

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        вызовы.append(query)
        if wait is not None:
            await wait()
        return []

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


@pytest.fixture
def dadata_дом(monkeypatch: Any) -> list[g.Query]:
    """Ключ DaData есть; она отвечает записанным домом и зовёт счётчик."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    вызовы: list[g.Query] = []

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        # Как настоящая интеграция: место в потолке занимается ДО похода, и
        # за потолком поход не состоится — в `вызовы` попадают только походы.
        if kw.get("on_request"):
            await kw["on_request"]()
        вызовы.append(query)
        return [ДОМ]

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(worker.dadata, "search", search)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    return вызовы


async def _потолок_dadata_выбран(redis: Any, потолок: int = 9000) -> None:
    await redis.set(worker.dadata_calls_key(), потолок)


# ── отказ без DaData — не приговор ─────────────────────────────────────────────


async def test_без_dadata_отказ_osm_остаётся_pending_и_попытка_не_тратится(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_дом: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING
    assert row.geo_provider == "nominatim"  # кто отказал — видно
    assert row.geo_attempts == 0 and row.geo_checked_at is not None
    assert dadata_дом == []  # поход к DaData не состоялся: потолок
    # Модель не зовётся по отложенному отказу, автозапись — тем более:
    # в очереди воркера пусто.
    assert not await redis.exists(f"arq:job:llm-addr:{seed_conversation.message_id}")
    assert (await redis.zcard("arq:queue")) == 0
    # Тревога о потолке — один раз в сутки.
    assert await redis.exists(f"geo:limit_alerted:dadata:{worker._день_яндекса()}")

    # Новые сутки — DaData снова в потолке, та же строка получает дом.
    await redis.delete(worker.dadata_calls_key())
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata" and row.geo_attempts == 1


async def test_dadata_ответила_403_отказ_тоже_откладывается(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def лимит(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "blocked", 403)

    monkeypatch.setattr(worker.dadata, "search", лимит)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    assert (await _row(db_sessionmaker, cid)).geo_status == g.GEO_PENDING
    assert await worker.dadata_exhausted(redis)


async def test_dadata_доступна_отказ_окончателен(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    """DaData спросили, и она дом не знает — это приговор, а не ожидание."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def пусто(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return []

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(worker.dadata, "search", пусто)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_NOT_FOUND and row.geo_attempts == 1


async def test_без_ключа_dadata_osm_приговаривает_как_раньше(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert (await _row(db_sessionmaker, cid)).geo_status == g.GEO_NOT_FOUND


async def test_найденное_osm_без_dadata_записывается(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_дом: Any, monkeypatch: Any
) -> None:
    """Откладывается только отказ: точный дом от OSM — это дом."""

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return [ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", search)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await _row(db_sessionmaker, cid)).geo_provider == "nominatim"


# ── место ──────────────────────────────────────────────────────────────────────


async def test_место_без_dadata_на_сегодня_ждёт_а_без_ключа_не_найдено(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(seed_conversation, db_sessionmaker, "СНТ Ромашка", место=True)
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 0

    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND


# ── наш потолок — limit, не blocked ───────────────────────────────────────────


async def test_потолок_это_limit_и_счётчик_честный(redis: Any) -> None:
    ключ = worker.yandex_calls_key()
    await worker._занять(redis, ключ, 2, "yandex")
    await worker._занять(redis, ключ, 2, "yandex")
    with pytest.raises(g.GeocodeError) as exc:
        await worker._занять(redis, ключ, 2, "yandex")
    assert exc.value.kind == "limit"
    assert int(await redis.get(ключ)) == 2  # несостоявшийся поход не считается


async def test_режим_яндекс_за_потолком_уходит_в_osm(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:

    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "dadata", False)

    async def яндекс(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        await kw["on_request"]()  # занять место — а потолок уже выбран гонкой
        raise AssertionError("до Яндекса дойти не должны")

    osm: list[g.Query] = []

    async def osm_дом(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        osm.append(query)
        return [ДОМ]

    monkeypatch.setattr(worker.yandex_geocoder, "search", яндекс)
    monkeypatch.setattr(worker.nominatim, "search", osm_дом)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_PROVIDER: "yandex",
                app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT: 1,
            },
            user_id=None,
        )
        await s.commit()
    # Счётчик ниже потолка (проверка пропускает), занятие переваливает.
    await redis.set(worker.yandex_calls_key(), 1)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert osm and (await _row(db_sessionmaker, cid)).geo_provider == "nominatim"
    assert int(await redis.get(worker.yandex_calls_key())) == 1


# ── запас живому потоку ───────────────────────────────────────────────────────


def test_доля_починки() -> None:
    assert worker.доля_починки(9000, "dadata") == 7500
    # Яндекс — долей 30 % по умолчанию (пакет 5, 20.09), не «потолок − 300».
    assert worker.доля_починки(900, "yandex") == 270
    assert worker.доля_починки(1000, "dadata") == 500  # не меньше половины
    assert worker.доля_починки(None, "dadata") is None


async def test_задача_починки_не_заходит_в_запас_а_живая_заходит(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_дом: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await redis.set(worker.dadata_calls_key(), 7500)  # доля починки выбрана, запас цел
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid, origin="repair")
        == "deferred"
    )
    assert dadata_дом == []
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert len(dadata_дом) == 1


@pytest.fixture
def оснастка(monkeypatch: Any, db_sessionmaker: Any, redis: Any) -> None:
    monkeypatch.setattr(geo_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(geo_repair.db_mod, "session_scope", db_sessionmaker)


async def test_починка_молчит_в_запасе_и_ставит_задачи_как_починка(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    await redis.set(worker.dadata_calls_key(), 7500)
    assert await geo_repair.repair_geocodes() == 0
    await redis.set(worker.dadata_calls_key(), 7499)

    поставлено: list[dict[str, Any]] = []

    async def enqueue(redis_: Any, candidate_id: Any, **kw: Any) -> bool:
        поставлено.append(kw)
        return True

    monkeypatch.setattr(geo_repair, "enqueue_geocode", enqueue)
    assert await geo_repair.repair_geocodes() == 1
    assert поставлено[0]["origin"] == "repair"


async def test_починка_без_ключа_dadata_идёт_как_раньше(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    await redis.set(worker.dadata_calls_key(), 9000)
    assert await geo_repair.repair_geocodes() == 1


# ── возврат в очередь того, что осудили без DaData ────────────────────────────


async def test_recheck_по_карте_и_времени(seed_conversation: Any, db_sessionmaker: Any) -> None:
    from app.cli import run_address_recheck

    a = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    b = await _строка(seed_conversation, db_sessionmaker, "ул Мира 7")
    c = await _строка(seed_conversation, db_sessionmaker, "ул Кирова 9")
    d = await _строка(seed_conversation, db_sessionmaker, "ул Садовая 11")
    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        for cid, provider, when in (
            (a, "nominatim", now),  # осуждён OSM сегодня — вернуть
            (b, "dadata", now),  # осуждён DaData — приговор
            (c, None, now),  # место без карты — вернуть
            (d, "nominatim", now - timedelta(days=3)),  # до окна — не трогать
        ):
            row = await s.get(ClientAddressCandidate, cid)
            row.geo_status = g.GEO_NOT_FOUND
            row.geo_provider = provider
            row.geo_checked_at = when
            row.geo_attempts = 3
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_recheck(
            s,
            statuses="not_found",
            providers="nominatim,none",
            checked_since=(now - timedelta(hours=1)).isoformat(),
            dry_run=False,
        )
    async with db_sessionmaker() as s:
        статусы = {
            cid: (await s.get(ClientAddressCandidate, cid)).geo_status for cid in (a, b, c, d)
        }
        assert статусы == {
            a: g.GEO_PENDING,
            b: g.GEO_NOT_FOUND,
            c: g.GEO_PENDING,
            d: g.GEO_NOT_FOUND,
        }
        assert (await s.get(ClientAddressCandidate, a)).geo_attempts == 0


# ── находки ревью 13.09 ───────────────────────────────────────────────────────


async def test_потолок_ноль_это_выключенная_dadata_а_не_вечное_ожидание(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_дом: Any,
    оснастка: Any,
) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT: 0}, user_id=None
        )
        await s.commit()
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    assert dadata_дом == []
    # И починка при потолке 0 не молчит.
    await _строка(seed_conversation, db_sessionmaker, "ул Мира 7")
    assert await geo_repair.repair_geocodes() >= 1


async def test_старая_строка_без_dadata_принимает_вердикт_osm(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_дом: Any
) -> None:
    """Мёртвый ключ не должен замораживать проверку навсегда: строка старше
    трёх суток принимает отказ карт послабее."""
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.message_at = datetime.now(UTC) - worker.ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ - timedelta(hours=1)
        await s.commit()
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND


async def test_сеть_dadata_откладывает_но_считает_попытку(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def сеть(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "network", 503)

    monkeypatch.setattr(worker.dadata, "search", сеть)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 1
    assert not await worker.dadata_exhausted(redis)


async def test_кэш_dadata_работает_и_за_потолком(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    """Ответ из кэша не стоит места в потолке: за потолком DaData отвечает из кэша."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def из_кэша(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        return [ДОМ]  # как _post при попадании: on_request не зовётся

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(worker.dadata, "search", из_кэша)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await _row(db_sessionmaker, cid)).geo_provider == "dadata"


async def test_место_403_закрывает_сутки_и_ждёт(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def лимит(place: Any, **kw: Any) -> list[Any]:
        raise g.GeocodeError("dadata", "blocked", 403)

    monkeypatch.setattr(worker.dadata, "search_place", лимит)
    cid = await _строка(seed_conversation, db_sessionmaker, "СНТ Ромашка", место=True)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    assert await worker.dadata_exhausted(redis)
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 0


async def test_тревога_потолка_один_раз_в_сутки_и_не_от_починки(redis: Any) -> None:
    ключ = f"geo:limit_alerted:dadata:{worker._день_яндекса()}"
    await worker._тревога_потолка(redis, "dadata")
    await worker._тревога_потолка(redis, "dadata")
    assert await redis.ttl(ключ) > 0


async def test_задача_починки_не_гасит_живую_постановку(redis: Any) -> None:
    import uuid

    from app.services.geocode_queue import enqueue_geocode

    cid = uuid.uuid4()
    assert await enqueue_geocode(redis, cid, defer_sec=500, suffix="repair", origin="repair")
    assert await enqueue_geocode(redis, cid)  # живая — своим именем, встаёт
    assert await redis.exists(f"arq:job:geocode:{cid}:repair")
    assert await redis.exists(f"arq:job:geocode:{cid}")


async def test_recheck_none_строкой(seed_conversation: Any, db_sessionmaker: Any) -> None:
    from app.cli import run_address_recheck

    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_provider, row.geo_checked_at = (
            g.GEO_NOT_FOUND,
            "none",
            datetime.now(UTC),
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="not_found", providers="none", dry_run=False)
    assert (await _row(db_sessionmaker, cid)).geo_status == g.GEO_PENDING


# ── второй круг ревью 13.09 ───────────────────────────────────────────────────


async def test_старое_место_без_dadata_ждёт_с_попыткой_а_не_not_found(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(seed_conversation, db_sessionmaker, "СНТ Ромашка", место=True)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.message_at = datetime.now(UTC) - worker.ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ - timedelta(hours=1)
        await s.commit()
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 1
    assert row.geo_provider == "none"  # DaData не отвечала — её имени в строке нет


async def test_строка_починки_откладывается_в_любом_возрасте(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, dadata_дом: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.message_at = datetime.now(UTC) - timedelta(days=30)
        await s.commit()
    await redis.set(worker.dadata_calls_key(), 7500)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid, origin="repair")
        == "deferred"
    )


async def test_403_в_поиске_по_области_закрывает_сутки(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    """Основной ответ DaData — из кэша (пусто), 403 приходит в поиске по области."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    вызовов = {"n": 0}

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        вызовов["n"] += 1
        if вызовов["n"] == 1:
            return []  # как попадание в кэш: on_request не зовётся
        raise g.GeocodeError("dadata", "blocked", 403)

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    async def osm_дом_не_тот(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        return []

    monkeypatch.setattr(worker.dadata, "search", search)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    monkeypatch.setattr(worker.nominatim, "search", osm_дом_не_тот)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    assert await worker.dadata_exhausted(redis)


async def test_recheck_сбрасывает_время_проверки(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    from app.cli import run_address_recheck

    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_provider = g.GEO_NOT_FOUND, "nominatim"
        row.geo_checked_at, row.geo_attempts = datetime.now(UTC), 2
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="not_found", dry_run=False)
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 0
    assert row.geo_checked_at is None


async def test_recheck_levels(seed_conversation: Any, db_sessionmaker: Any, capsys: Any) -> None:
    """W9 (N24, 19.09): `--levels A,B` возвращает в очередь только строки уровней
    улики улицы (`STREET_EVIDENCE_LEVELS`); невод C остаётся с прежним
    вердиктом — сторожа его всё равно не пропустят, а запросы карт он потратил бы.
    Регистр и пробелы прощаются, чужое имя уровня — печать и выход без записи,
    без опции — все уровни, как раньше."""
    from app.cli import run_address_recheck

    a = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    b = await _строка(seed_conversation, db_sessionmaker, "ул Мира 7")
    c = await _строка(seed_conversation, db_sessionmaker, "ул Кирова 9")
    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        for cid, level in ((a, "A"), (b, "B"), (c, "C")):
            row = await s.get(ClientAddressCandidate, cid)
            row.level = level
            row.geo_status = g.GEO_STREET_MISMATCH
            row.geo_provider = "dadata"
            row.geo_checked_at = now
        await s.commit()
    assert set(g.STREET_EVIDENCE_LEVELS) == {"A", "B"}, "тест собран под уровни A/B"

    async def статусы() -> dict[Any, str]:
        async with db_sessionmaker() as s:
            return {cid: (await s.get(ClientAddressCandidate, cid)).geo_status for cid in (a, b, c)}

    # Чужое имя уровня («AB» без запятой) — ничего не сброшено, сказано словами.
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="street_mismatch", levels="AB", dry_run=False)
    assert "неизвестные уровни ['AB']" in capsys.readouterr().out
    assert set((await статусы()).values()) == {g.GEO_STREET_MISMATCH}

    # Сухой прогон печатает уровни и ничего не пишет.
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="street_mismatch", levels=" a, b ", dry_run=True)
    вывод = capsys.readouterr().out
    assert "уровней ['A', 'B']: 2; сухой прогон: True" in вывод
    assert set((await статусы()).values()) == {g.GEO_STREET_MISMATCH}

    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="street_mismatch", levels="A,B", dry_run=False)
    assert await статусы() == {a: g.GEO_PENDING, b: g.GEO_PENDING, c: g.GEO_STREET_MISMATCH}

    # Без опции — все уровни, как до 19.09.
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="street_mismatch", dry_run=False)
    assert (await статусы())[c] == g.GEO_PENDING


async def test_порция_починки_не_больше_остатка_доли(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, оснастка: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    for улица in ("ул Ленина 5", "ул Мира 7", "ул Кирова 9", "ул Садовая 11"):
        await _строка(seed_conversation, db_sessionmaker, улица)
    await redis.set(worker.dadata_calls_key(), 7497)  # до доли 7 500 — три запроса
    assert await geo_repair.repair_geocodes() == 1  # 3 // 2 = 1 строка


async def test_cli_address_limits_меняет_потолки_с_журналом(db_sessionmaker: Any) -> None:
    from app.cli import run_address_limits
    from app.models import AuditLog

    async with db_sessionmaker() as s:
        await run_address_limits(s, llm=900, dadata=None, yandex=None, suggest=None)
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_DAILY_LIMIT) == 900
        assert await app_settings.get(s, app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT) == 9000
        import sqlalchemy as sa

        row = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .first()
        )
        assert row is not None and row.details["after"] == {"llm": 900}


# ── третий круг ревью 13.09 ───────────────────────────────────────────────────


async def test_сброс_вердикта_чистит_координаты_и_попытки(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    from app.services import clients as clients_svc

    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_attempts = g.GEO_NOT_FOUND, 8
        row.geo_lat, row.geo_lon, row.geo_formatted = 1.0, 2.0, "где-то"
        row.geo_variants, row.geo_checked_at = [{"x": 1}], datetime.now(UTC)
        clients_svc.сбросить_вердикт(row, reason="settlement_named")
        await s.commit()
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 0
    assert row.geo_lat is None and row.geo_formatted is None and row.geo_variants is None
    assert row.geo_checked_at is None and row.geo_provider is None


async def test_посёлок_следом_сбрасывает_попытки_и_координаты(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    from app.services import clients as clients_svc

    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_attempts, row.geo_lat = g.GEO_NOT_FOUND, 8, 1.0
        await s.commit()
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        found = address_parse.parse("пос. Ударник ул Звенигородская 1")
        assert found is not None and found.settlement
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seed_conversation.conversation_id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        assert записано.перепроверить
        await s.commit()
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.geo_attempts == 0 and row.geo_lat is None


async def test_старое_место_в_починке_не_тратит_попытку_на_потолок(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(seed_conversation, db_sessionmaker, "СНТ Ромашка", место=True)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.message_at = datetime.now(UTC) - timedelta(days=30)
        await s.commit()
    await _потолок_dadata_выбран(redis)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid, origin="repair")
        == "deferred"
    )
    assert (await _row(db_sessionmaker, cid)).geo_attempts == 0


async def test_потолок_между_основным_запросом_и_областью_откладывает(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, osm_пусто: Any, monkeypatch: Any
) -> None:
    """Основной ответ DaData пуст (из кэша), потолок выбран до поиска по
    области — без области вердикт не окончателен."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def пусто_из_кэша(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        return []

    monkeypatch.setattr(worker.dadata, "search", пусто_из_кэша)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await _потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
