"""Повторная привязка адреса без человека и без новой команды (N13, 19.09).

Три триггера — один исполнитель, починка `geo_repair`:
* поздняя реплика-место с типом после дома (ветка 1b в `inbound`) — живой
  задачей, под сторожем смысла `place_stated_as_address` и в двух режимах
  (ответ на вопрос оператора / речь);
* возврат DaData — строки, судимые без неё (`geo_without_dadata`);
* смена версии судьи (`geo_verdict_version` ≠ `geocode.VERDICT_VERSION`).

Обход починки берёт только строки не моложе `REQUEUE_MIN_AGE` (окно вопроса
клиенту) и ровно в свободный остаток порции — сброшенное встаёт в очередь в
том же заходе. Сброс — один набор полей на все пути (`clients.сброс_вердикта`).

ДИВЕРСИИ (каждая обязана краснеть): `place_stated_as_address` → всегда True —
стенды 1b с речью («еду из деревни…», «это не посёлок…») падают; убрать
`REQUEUE_MIN_AGE` из условия обхода — падает тест «не моложе 27 часов» и
отрицательная проверка замка вопроса; не ставить ключ `geo:dadata:down` или
считать попытку при вердикте без DaData по сети — падает стенд «DaData лежит
по сети». Адреса — из стендов (Кострома/Малиновка, Саранск/Ялга,
Орск/Заречный); ПД клиентов нет.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.integrations import gateway
from app.models import Client, ClientAddressCandidate
from app.models.client import CANDIDATE_ACCEPTED
from app.scheduler.jobs import geo_repair
from app.services import address_ask, app_settings
from app.services import address_parse as ap
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services.inbound import apply_inbound_event
from app.workers import address_ask as ask_worker
from app.workers import geocode as worker
from tests.unit import test_address_context_1809 as стенд
from tests.unit import test_geo_bez_api_1309 as без_api

pytestmark = pytest.mark.anyio

T0 = стенд.T0
событие = стенд.событие
_строки = стенд._строки
_исходящее = стенд._исходящее
# Фикстуры соседних стендов — присваиванием, не импортом имени (ruff F811).
account = стенд.account
dadata_дом = без_api.dadata_дом

ЧАС = timedelta(hours=1)


# ── оснастка ─────────────────────────────────────────────────────────────────


@pytest.fixture
def оснастка(monkeypatch: Any, db_sessionmaker: Any, redis: Any) -> None:
    monkeypatch.setattr(geo_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(geo_repair.db_mod, "session_scope", db_sessionmaker)


async def _реплика(db, redis, account, текст: str, i: int, **kw) -> None:  # noqa: ANN001, ANN003
    await apply_inbound_event(
        db, redis, account, событие(текст, msg=f"m{i}", when=T0 + timedelta(minutes=i), **kw)
    )


ВАРИАНТЫ_ПО_ОБЛАСТИ = [
    {"formatted": "Тихий переулок, 2, Малиновка", "lat": 57.71, "lon": 40.95, "city": "Кострома"},
    {"formatted": "Тихий переулок, 2, Сущёво", "lat": 57.80, "lon": 40.90, "city": "Кострома"},
    {"formatted": "Тихий переулок, 2, Минское", "lat": 57.65, "lon": 40.80, "city": "Кострома"},
]


async def _вердикт_строке(db_sessionmaker, row_id: uuid.UUID, **поля: Any) -> None:  # noqa: ANN001
    """Записанный ответ карты — как его оставил бы воркер."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, row_id)
        row.geo_provider = "dadata"
        row.geo_checked_at = datetime.now(UTC)
        row.geo_attempts = 1
        row.geo_verdict_version = g.VERDICT_VERSION
        for k, v in поля.items():
            setattr(row, k, v)
        await s.commit()


async def _ambiguous(db_sessionmaker, row_id: uuid.UUID) -> None:  # noqa: ANN001
    await _вердикт_строке(
        db_sessionmaker, row_id, geo_status=g.GEO_AMBIGUOUS, geo_variants=ВАРИАНТЫ_ПО_ОБЛАСТИ
    )


async def _exact(db_sessionmaker, row_id: uuid.UUID) -> None:  # noqa: ANN001
    await _вердикт_строке(
        db_sessionmaker,
        row_id,
        geo_status=g.GEO_EXACT,
        geo_formatted="улица Садовая, 3, Саранск",
        geo_lat=54.17,
        geo_lon=45.18,
    )


async def _дом(db_sessionmaker) -> ClientAddressCandidate:  # noqa: ANN001
    return next(r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_HOUSE)


async def _места(db_sessionmaker) -> list[ClientAddressCandidate]:  # noqa: ANN001
    return [r for r in await _строки(db_sessionmaker) if r.kind == ap.KIND_PLACE]


async def _строка_обхода(
    db_sessionmaker: Any,
    seed: Any,
    текст: str,
    *,
    возраст: timedelta,
    статус: str = g.GEO_AMBIGUOUS,
    версия: int | None = None,
    без_dadata: bool = False,
    **поля: Any,
) -> uuid.UUID:
    """Строка с окончательным вердиктом заданного возраста — сырьё обхода N13."""
    когда = datetime.now(UTC) - возраст
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed.client_id)
        found = ap.parse(текст)
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seed.conversation_id,
            message_id=None,
            message_at=когда,
            found=found,
            now=когда,
        )
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
        row.geo_status = статус
        row.geo_variants = ВАРИАНТЫ_ПО_ОБЛАСТИ if статус == g.GEO_AMBIGUOUS else None
        row.geo_provider = "nominatim"
        row.geo_checked_at = когда
        row.geo_attempts = 1
        row.geo_verdict_version = версия
        row.geo_without_dadata = без_dadata
        for k, v in поля.items():
            setattr(row, k, v)
        await s.commit()
        return row.id


async def _статус(db_sessionmaker: Any, cid: uuid.UUID) -> tuple[str | None, int | None, bool]:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        return row.geo_status, row.geo_verdict_version, row.geo_without_dadata


async def _ключи_починки(redis: Any) -> set[str]:
    return {k async for k in redis.scan_iter("arq:job:geocode:*:repair")}


# ── сторож смысла ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("текст", "ожидание"),
    [
        ("деревня Малиновка", True),
        ("д. Малиновка", True),
        ("С. Поселье", True),
        ("живу в д. Малиновка", True),
        ("в посёлок Ударник", True),
        ("да, деревня Малиновка", True),
        ("нет, деревня Малиновка", True),
        ("деревня Малиновка, а не Ударник", True),
        ("станица Грушевская", True),
        ("еду из деревни Малиновка", False),
        ("я не из деревни Малиновка", False),
        ("автобус до деревни Малиновка", False),
        ("Приеду из деревни Малиновка завтра", False),
        ("это не посёлок Ударник", False),
        ("из д. Малиновка", False),
        ("до п. Ударник", False),
        ("около д. Малиновка", False),
        ("у деревни Малиновка", False),
        ("я из станицы Грушевской", False),
        ("с деревни Малиновка", False),
    ],
)
def test_place_stated_as_address_таблица(текст: str, ожидание: bool) -> None:
    """Разбор даёт пункт для ВСЕХ фраз (факт по HEAD) — различает их сторож:
    падеж типа, предлог перед пунктом, частица «не»."""
    found = ap.parse_place(текст)
    assert found is not None and found.settlement, текст
    assert ap.place_stated_as_address(текст, found) is ожидание, текст


def test_place_stated_as_address_без_пункта_false() -> None:
    found = ap.parse_place("Гатчинский р-н, массив Южный")
    assert found is not None and not found.settlement
    assert ap.place_stated_as_address("Гатчинский р-н, массив Южный", found) is False


# ── ветка 1b: пункт с типом после дома ───────────────────────────────────────


async def test_1b_речь_из_деревни_при_ambiguous_не_трогает_дом(db, redis, account, db_sessionmaker):
    """«пер Тихий 2» → карта нашла три по области → «еду из деревни Малиновка»:
    речь, не адрес — строка дома цела (статус, варианты, пункт), место заведено."""
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    # Живая задача первой реплики отработала — ключа нет; новый не должен появиться.
    await redis.delete(f"arq:job:geocode:{дом.id}")
    await _реплика(db, redis, account, "еду из деревни Малиновка", 1, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert дом.geo_status == g.GEO_AMBIGUOUS
    assert дом.geo_variants == ВАРИАНТЫ_ПО_ОБЛАСТИ and дом.settlement is None
    (место,) = await _места(db_sessionmaker)
    assert место.settlement == "Малиновка"
    assert not await redis.exists(f"arq:job:geocode:{дом.id}")
    assert await redis.exists(f"arq:job:geocode:{место.id}")


async def test_1b_это_не_посёлок_при_ambiguous_не_трогает_дом(db, redis, account, db_sessionmaker):
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    await _реплика(db, redis, account, "это не посёлок Ударник", 1, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert (дом.geo_status, дом.settlement) == (g.GEO_AMBIGUOUS, None)
    assert дом.geo_variants == ВАРИАНТЫ_ПО_ОБЛАСТИ


async def test_1b_деревня_после_ambiguous_дописывает_пункт_и_ставит_две_живые_задачи(
    db, redis, account, db_sessionmaker
):
    """Пункт дописан в строку дома, вердикт сброшен, и ОБЕ строки — дом и
    место — идут живыми задачами: вопрос клиенту ждёт `pending` лишь минуты,
    починки он не дождался бы."""
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    # Задача первой реплики отработала — иначе ключ дома был бы «есть» и без 1b.
    await redis.delete(f"arq:job:geocode:{дом.id}")
    with structlog.testing.capture_logs() as логи:
        await _реплика(db, redis, account, "деревня Малиновка", 1, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.settlement_type) == ("Малиновка", "деревня")
    assert дом.geo_status == g.GEO_PENDING and дом.geo_variants is None
    assert дом.geo_attempts == 0 and дом.geo_verdict_version is None
    assert дом.raw == "деревня Малиновка; пер Тихий 2"
    (место,) = await _места(db_sessionmaker)
    assert await redis.exists(f"arq:job:geocode:{дом.id}")
    assert await redis.exists(f"arq:job:geocode:{место.id}")
    # Журнал `geocode.requeue` — единая точка grep по всем путям сброса (docs/47
    # §4.6); ORM-путь `сбросить_вердикт` держится здесь (ревью 19.09, C14).
    повторы = [л for л in логи if л["event"] == "geocode.requeue"]
    assert [(л["reason"], л["was"], л["candidate_id"]) for л in повторы] == [
        ("late_place", g.GEO_AMBIGUOUS, str(дом.id))
    ]


async def test_1b_после_вопроса_оператора_п_Ялга_при_exact_тронута_как_голое_Ялга(
    db, redis, account, db_sessionmaker
):
    """Симметрия с 18.09: голое «Ялга» после «адрес?» переписывает exact — и
    «п. Ялга» после того же вопроса тоже (клиент поправил адрес)."""
    await _реплика(db, redis, account, "ул Садовая 3", 0)
    дом = await _дом(db_sessionmaker)
    await _exact(db_sessionmaker, дом.id)
    await redis.delete(f"arq:job:geocode:{дом.id}")
    await _исходящее(db_sessionmaker, "Подскажите, пожалуйста, адрес?", T0 + timedelta(minutes=1))
    await _реплика(db, redis, account, "п. Ялга", 2)
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.settlement_type) == ("Ялга", "посёлок")
    assert дом.geo_status == g.GEO_PENDING
    assert дом.geo_lat is None and дом.geo_formatted is None and дом.geo_provider is None
    assert await redis.exists(f"arq:job:geocode:{дом.id}")


async def test_1b_речь_деревня_при_exact_не_трогает(db, redis, account, db_sessionmaker):
    """Без вопроса оператора у точного дома в городе «деревня Малиновка» ничего
    не меняет — место живёт своей строкой."""
    await _реплика(db, redis, account, "ул Садовая 3", 0)
    дом = await _дом(db_sessionmaker)
    await _exact(db_sessionmaker, дом.id)
    await _реплика(db, redis, account, "деревня Малиновка", 1)
    дом = await _дом(db_sessionmaker)
    assert (дом.geo_status, дом.settlement) == (g.GEO_EXACT, None)
    assert дом.geo_lat == 54.17
    assert len(await _места(db_sessionmaker)) == 1


async def test_1b_pending_дом_получает_пункт_до_вердикта(db, redis, account, db_sessionmaker):
    """Дом ещё у карты (`pending` ∈ CHECKING ⊂ LATE_PLACE_STATUSES) — пункт
    дописан ДО вердикта; запись без пункта устареет по снимку `_пометить`."""
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    await _реплика(db, redis, account, "деревня Малиновка", 1, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.geo_status) == ("Малиновка", g.GEO_PENDING)
    assert дом.raw.startswith("деревня Малиновка; ")


async def test_1b_пункт_уже_есть_не_трогает(db, redis, account, db_sessionmaker):
    """Названный пункт не спорит с названным (докстринг refine)."""
    await _реплика(db, redis, account, "Ялга ул Садовая д 3", 0)
    дом = await _дом(db_sessionmaker)
    assert дом.settlement == "Ялга"
    await _ambiguous(db_sessionmaker, дом.id)
    await _реплика(db, redis, account, "деревня Малиновка", 1)
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.geo_status) == ("Ялга", g.GEO_AMBIGUOUS)


async def test_1b_решённая_кнопкой_не_трогает(db, redis, account, db_sessionmaker, make_user):
    оператор = await make_user("op-n13@example.com")
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, дом.id)
        row.resolved_by_id = оператор.id
        await s.commit()
    await _реплика(db, redis, account, "деревня Малиновка", 1, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.geo_status) == (None, g.GEO_AMBIGUOUS)


async def test_1b_город_объявления_не_трогает(db, redis, account, db_sessionmaker):
    """«г. Саранск» при объявлении в Саранске — тот же город, не пункт."""
    await _реплика(db, redis, account, "ул Садовая 3", 0)
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    await _реплика(db, redis, account, "г. Саранск", 1)
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.locality, дом.geo_status) == (None, None, g.GEO_AMBIGUOUS)


async def test_1b_через_25_часов_не_трогает(db, redis, account, db_sessionmaker):
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("деревня Малиновка", msg="m-late", when=T0 + 25 * ЧАС, город="kostroma"),
    )
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.geo_status) == (None, g.GEO_AMBIGUOUS)


async def test_1b_из_деревни_после_вопроса_тоже_не_трогает(db, redis, account, db_sessionmaker):
    """Сторож действует в ОБОИХ режимах: «из деревни Малиновка» не становится
    адресом и после «адрес?»."""
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    await _исходящее(db_sessionmaker, "Уточните адрес, пожалуйста", T0 + timedelta(minutes=1))
    await _реплика(db, redis, account, "еду из деревни Малиновка", 2, город="kostroma")
    дом = await _дом(db_sessionmaker)
    assert (дом.settlement, дом.geo_status) == (None, g.GEO_AMBIGUOUS)


async def test_геосостояние_ask_после_1b(db, redis, account, db_sessionmaker, monkeypatch):
    """«пер Тихий 2» ambiguous → «деревня Малиновка» → живая задача судит дом;
    предикат вопроса клиенту (`candidate_lock`, тот же в задаче и сухом
    прогоне) через 600 с не даёт `geo_pending`."""

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return [
            g.GeoHit(
                street="Тихий переулок",
                house="2",
                settlement="Малиновка",
                city="Кострома",
                region="Костромская область",
                lat=57.71,
                lon=40.95,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", search)
    await _реплика(db, redis, account, "пер Тихий 2", 0, город="kostroma")
    дом = await _дом(db_sessionmaker)
    await _ambiguous(db_sessionmaker, дом.id)
    await _реплика(db, redis, account, "деревня Малиновка", 1, город="kostroma")
    assert (await _дом(db_sessionmaker)).geo_status == g.GEO_PENDING
    assert await worker.geocode_candidate(без_api.ctx(db_sessionmaker, redis), дом.id) not in (
        "deferred",
        "stale",
        "paused",
    )
    дом = await _дом(db_sessionmaker)
    assert дом.geo_status not in g.CHECKING_STATUSES
    assert дом.geo_verdict_version == g.VERDICT_VERSION
    t = T0 + timedelta(minutes=1) + timedelta(seconds=600)
    async with db_sessionmaker() as s:
        строки = await address_ask.address_rows(
            s,
            client_id=дом.client_id,
            conversation_id=дом.conversation_id,
            since=t - address_ask.ОКНО,
            before=t,
        )
    assert address_ask.candidate_lock(строки) != address_ask.GEO_PENDING_LOCK


# ── обход починки ────────────────────────────────────────────────────────────


def test_min_age_перекрывает_окно_вопроса() -> None:
    """Граница считается из констант окна вопроса клиенту, а не на глаз:
    `ОКНО` + наибольшая задержка вопроса + `MAX_AGE` + повторы замка."""
    окно = (
        address_ask.ОКНО
        + timedelta(seconds=app_settings.ADDRESS_ASK_DELAY_MAX_SEC)
        + ask_worker.MAX_AGE
        + timedelta(seconds=ask_worker.RETRY_MAX * ask_worker.RETRY_DEFER_SEC)
    )
    assert geo_repair.REQUEUE_MIN_AGE >= окно
    assert geo_repair.REQUEUE_MAX_AGE > geo_repair.REQUEUE_MIN_AGE


async def test_обход_не_моложе_27_часов(seed_conversation, db_sessionmaker, redis, оснастка):
    """ambiguous 30 минут с NULL-версией — не тронута; 28 часов — сброшена и
    поставлена в очередь в том же заходе."""
    свежая = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=timedelta(minutes=30)
    )
    давняя = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Мира 7", возраст=28 * ЧАС)
    assert await geo_repair.repair_geocodes() == 1
    assert (await _статус(db_sessionmaker, свежая))[0] == g.GEO_AMBIGUOUS
    assert await _статус(db_sessionmaker, давняя) == (g.GEO_PENDING, None, False)
    assert await _ключи_починки(redis) == {f"arq:job:geocode:{давняя}:repair"}
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, давняя)
        # Попытки сохраняются: у строки уже был вердикт (`с_попытками=True`).
        assert row.geo_attempts == 1 and row.geo_variants is None
        assert row.geo_provider is None and row.geo_checked_at is None


async def test_обход_ровно_в_свободный_остаток_порции(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Порция 2, десять подходящих строк → сброшено ровно 2 и оба ключа
    `:repair` в этом же заходе; когда воркер их рассудил — ещё 2."""
    monkeypatch.setattr(geo_repair, "ПОРЦИЯ", 2)
    ids = [
        await _строка_обхода(db_sessionmaker, seed_conversation, f"ул Ленина {n}", возраст=28 * ЧАС)
        for n in range(1, 11)
    ]
    assert await geo_repair.repair_geocodes() == 2
    ключи = await _ключи_починки(redis)
    сброшены = [cid for cid in ids if (await _статус(db_sessionmaker, cid))[0] == g.GEO_PENDING]
    assert len(сброшены) == 2
    assert ключи == {f"arq:job:geocode:{cid}:repair" for cid in сброшены}
    # Воркер рассудил обе: версия текущая, флаг снят — в обход они не вернутся.
    for cid in сброшены:
        await _вердикт_строке(db_sessionmaker, cid, geo_status=g.GEO_EXACT)
        await redis.delete(f"arq:job:geocode:{cid}:repair")
    await redis.delete("arq:queue")
    assert await geo_repair.repair_geocodes() == 2
    ещё = [
        cid
        for cid in ids
        if cid not in сброшены and (await _статус(db_sessionmaker, cid))[0] == g.GEO_PENDING
    ]
    assert len(ещё) == 2


async def test_обход_молчит_когда_живой_хвост_заполняет_порцию(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Две живые `pending` при порции 2 — ни одна старая строка не сброшена:
    живые вперёд, обход ждёт следующего захода."""
    monkeypatch.setattr(geo_repair, "ПОРЦИЯ", 2)
    старые = [
        await _строка_обхода(db_sessionmaker, seed_conversation, f"ул Мира {n}", возраст=28 * ЧАС)
        for n in (1, 2, 3)
    ]
    живые = [
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            f"ул Кирова {n}",
            возраст=timedelta(minutes=1),
            статус=g.GEO_PENDING,
            geo_attempts=0,
            geo_checked_at=None,
            geo_provider=None,
        )
        for n in (1, 2)
    ]
    assert await geo_repair.repair_geocodes() == 2
    assert await _ключи_починки(redis) == {f"arq:job:geocode:{cid}:repair" for cid in живые}
    for cid in старые:
        assert (await _статус(db_sessionmaker, cid))[0] == g.GEO_AMBIGUOUS


async def test_отрицательная_без_min_age_вопрос_клиенту_skip_geo_pending(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Без границы возраста обход сбросил бы строку A/B пятичасовой давности в
    `pending`, и замок вопроса клиенту (`candidate_lock`) дал бы `geo_pending`
    — вопрос не ушёл бы. С границей 27 ч замка нет."""
    cid = await _строка_обхода(db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=5 * ЧАС)

    async def замок() -> str | None:
        async with db_sessionmaker() as s:
            row = await s.get(ClientAddressCandidate, cid)
            t = datetime.now(UTC)
            строки = await address_ask.address_rows(
                s,
                client_id=row.client_id,
                conversation_id=row.conversation_id,
                since=t - address_ask.ОКНО,
                before=t,
            )
        return address_ask.candidate_lock(строки)

    assert await geo_repair.repair_geocodes() == 0
    assert (await _статус(db_sessionmaker, cid))[0] == g.GEO_AMBIGUOUS
    assert await замок() is None  # отказ без степени — это и есть «вопрос клиенту»

    # Диверсия в самом тесте: без сторожа на экране ЕСТЬ замок.
    monkeypatch.setattr(geo_repair, "REQUEUE_MIN_AGE", timedelta(0))
    assert await geo_repair.repair_geocodes() == 1
    assert (await _статус(db_sessionmaker, cid))[0] == g.GEO_PENDING
    assert await замок() == address_ask.GEO_PENDING_LOCK


async def test_dadata_back_и_verdict_version_не_считают_строку_дважды(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Строка с флагом И NULL-версией подходит обоим триггерам — сброшена один
    раз: `pending` не в RECHECK_STATUSES, второму триггеру она не достаётся."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС, без_dadata=True
    )
    with structlog.testing.capture_logs() as логи:
        assert await geo_repair.repair_geocodes() == 1
    assert await _статус(db_sessionmaker, cid) == (g.GEO_PENDING, None, False)
    повторы = [л for л in логи if л["event"] == "geocode.requeue"]
    assert [(л["reason"], л["rows"]) for л in повторы] == [("dadata_back", 1)]
    (заход,) = [л for л in логи if л["event"] == "geo.repair"]
    assert (заход["requeued"], заход["enqueued"]) == (1, 1)


async def test_сходимость_повторно_судимая_не_попадает_в_обход(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Воркер записал версию = текущая и флаг False — второй заход не трогает
    строку, каким бы неспокойным ни был вердикт."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    судимые = [
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            текст,
            возраст=28 * ЧАС,
            статус=статус,
            версия=g.VERDICT_VERSION,
        )
        for текст, статус in (
            ("ул Ленина 5", g.GEO_AMBIGUOUS),
            ("ул Мира 7", g.GEO_ELSEWHERE),
            ("ул Кирова 9", g.GEO_NOT_FOUND),
        )
    ]
    assert await geo_repair.repair_geocodes() == 0
    for cid, ожидание in zip(
        судимые, (g.GEO_AMBIGUOUS, g.GEO_ELSEWHERE, g.GEO_NOT_FOUND), strict=True
    ):
        assert (await _статус(db_sessionmaker, cid))[0] == ожидание
    assert await _ключи_починки(redis) == set()


async def test_возврат_dadata_пересматривает_только_с_флагом(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Флаг False при NULL-версии — сброс по `verdict_version`; флаг True при
    текущей версии — по `dadata_back`, и только когда DaData в деле."""
    без_версии = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС, версия=None
    )
    без_dadata = await _строка_обхода(
        db_sessionmaker,
        seed_conversation,
        "ул Мира 7",
        возраст=28 * ЧАС,
        версия=g.VERDICT_VERSION,
        без_dadata=True,
    )
    # DaData не настроена (ключа нет): только смена версии.
    with structlog.testing.capture_logs() as логи:
        assert await geo_repair.repair_geocodes() == 1
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("verdict_version", 1)
    ]
    assert (await _статус(db_sessionmaker, без_версии))[0] == g.GEO_PENDING
    assert (await _статус(db_sessionmaker, без_dadata))[0] == g.GEO_AMBIGUOUS

    # DaData вернулась — строка с флагом пересматривается.
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    await redis.delete("arq:queue", *await _ключи_починки(redis))
    with structlog.testing.capture_logs() as логи:
        # Сброшенная выше `pending` тоже встаёт (живой хвост) — считаем причины.
        assert await geo_repair.repair_geocodes() == 2
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("dadata_back", 1)
    ]
    assert await _статус(db_sessionmaker, без_dadata) == (g.GEO_PENDING, None, False)


async def test_dadata_лежит_по_сети_обход_ждёт_а_попытка_не_сгорает(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Ревью 19.09, C2. DaData настроена, но не отвечает по сети: обход
    `dadata_back` сбросил строку, воркер снова осудил её без DaData (OSM даёт
    `ambiguous`) — попытка НЕ тратится (иначе за час строка упёрлась бы в
    `ПОТОЛОК_ПОПЫТОК` и выпала из обхода навсегда), а следующий заход, пока
    жив ключ `geo:dadata:down`, обход не включает. Ключ умер — обход снова
    сбрасывает, и попытка по-прежнему не растёт.

    Диверсии: убрать правило «попытка не тратится» в воркере → 2 вместо 1;
    не ставить ключ / не смотреть его в починке → второй заход сбрасывает."""
    from app.models import Conversation

    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def сеть(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "network", 503)

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    async def osm_два_дома(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return [
            без_api.ДОМ,
            dataclasses.replace(без_api.ДОМ, settlement=None, lat=51.23, lon=58.47),
        ]

    monkeypatch.setattr(worker.dadata, "search", сеть)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    monkeypatch.setattr(worker.nominatim, "search", osm_два_дома)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        await s.commit()
    cid = await _строка_обхода(
        db_sessionmaker,
        seed_conversation,
        "ул Звенигородская 1",
        возраст=28 * ЧАС,
        версия=g.VERDICT_VERSION,
        без_dadata=True,
    )
    ctx = без_api.ctx(db_sessionmaker, redis)

    async def попыток() -> int:
        return (await без_api._row(db_sessionmaker, cid)).geo_attempts

    async def заход() -> tuple[int, list[tuple[str, int]], list[str]]:
        await redis.delete("arq:queue", *(await _ключи_починки(redis) or ["-"]))
        with structlog.testing.capture_logs() as логи:
            поставлено = await geo_repair.repair_geocodes()
        return (
            поставлено,
            [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"],
            [л["paused"] for л in логи if л["event"] == "geo.repair" and "paused" in л],
        )

    # 1. Заход: DaData считается живой — строка с флагом сброшена и поставлена.
    assert await заход() == (1, [("dadata_back", 1)], [])
    assert await _статус(db_sessionmaker, cid) == (g.GEO_PENDING, None, False)
    assert await попыток() == 1
    # 2. Воркер: DaData лежит по сети, OSM — два дома → ambiguous без DaData.
    assert await worker.geocode_candidate(ctx, cid, origin=worker.ORIGIN_REPAIR) == g.GEO_AMBIGUOUS
    assert await _статус(db_sessionmaker, cid) == (g.GEO_AMBIGUOUS, g.VERDICT_VERSION, True)
    assert await попыток() == 1, "попытка без DaData по сети не тратится"
    assert await worker.dadata_down(redis)
    assert 0 < await redis.ttl(worker.DADATA_DOWN_KEY) <= worker.DADATA_DOWN_SEC
    # 3. Заход при живом ключе: обход dadata_back не включён, строка не тронута.
    assert await заход() == (0, [], ["dadata_back"])
    assert (await _статус(db_sessionmaker, cid))[0] == g.GEO_AMBIGUOUS
    # 4. Ключ протух — обход снова сбрасывает; второй суд без DaData попытку
    #    тоже не тратит: два захода подряд не дали 2 попыток.
    await redis.delete(worker.DADATA_DOWN_KEY)
    assert await заход() == (1, [("dadata_back", 1)], [])
    assert await worker.geocode_candidate(ctx, cid, origin=worker.ORIGIN_REPAIR) == g.GEO_AMBIGUOUS
    assert await попыток() == 1
    assert await worker.dadata_down(redis)


async def test_сеть_dadata_у_отложенного_отказа_попытку_тратит(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Граница правила C2: отказ по содержанию без DaData откладывается
    `pending` — строка остаётся в CHECKING, пауза починки на ней работает, и
    попытка тратится, как до 19.09 (test_geo_bez_api_1309::test_сеть_dadata_
    откладывает_но_считает_попытку). Здесь — задачей починки (`origin=repair`):
    отложено в любом возрасте, попытка +1, ключ «лежит» поставлен."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def сеть(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "bad_response")

    async def пусто(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return []

    monkeypatch.setattr(worker.dadata, "search", сеть)
    monkeypatch.setattr(worker.nominatim, "search", пусто)
    cid = await без_api._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    ctx = без_api.ctx(db_sessionmaker, redis)
    assert await worker.geocode_candidate(ctx, cid, origin=worker.ORIGIN_REPAIR) == "deferred"
    row = await без_api._row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_attempts, row.geo_without_dadata) == (g.GEO_PENDING, 1, True)
    assert await worker.dadata_down(redis)


async def test_обход_не_трогает_решённые_кнопкой_и_вне_30_дней_и_с_потолком_попыток(
    seed_conversation, db_sessionmaker, redis, оснастка, make_user
):
    оператор = await make_user("op-n13-repair@example.com")
    нетронутые = [
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            "ул Ленина 5",
            возраст=28 * ЧАС,
            resolved_by_id=оператор.id,
        ),
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            "ул Мира 7",
            возраст=28 * ЧАС,
            status=CANDIDATE_ACCEPTED,
        ),
        await _строка_обхода(
            db_sessionmaker, seed_conversation, "ул Кирова 9", возраст=timedelta(days=31)
        ),
        await _строка_обхода(
            db_sessionmaker,
            seed_conversation,
            "ул Садовая 11",
            возраст=28 * ЧАС,
            geo_attempts=geo_repair.ПОТОЛОК_ПОПЫТОК,
        ),
        # `exact` — никогда: он в карточке.
        await _строка_обхода(
            db_sessionmaker, seed_conversation, "ул Победы 15", возраст=28 * ЧАС, статус=g.GEO_EXACT
        ),
    ]
    годная = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Гагарина 15", возраст=28 * ЧАС
    )
    assert await geo_repair.repair_geocodes() == 1
    for cid in нетронутые:
        assert (await _статус(db_sessionmaker, cid))[0] != g.GEO_PENDING
    assert (await _статус(db_sessionmaker, годная))[0] == g.GEO_PENDING


# ── воркер и общий строитель сброса ──────────────────────────────────────────


async def test_воркер_пишет_версию_и_флаг(
    seed_conversation, db_sessionmaker, redis, dadata_дом, monkeypatch
):
    """Записанный ответ карт без DaData (потолок выбран, дом дал OSM) →
    `geo_without_dadata=True`, версия = VERDICT_VERSION; с DaData → False."""

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return [без_api.ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", search)
    cid = await без_api._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await без_api._потолок_dadata_выбран(redis)
    ctx = без_api.ctx(db_sessionmaker, redis)
    assert await worker.geocode_candidate(ctx, cid) == g.GEO_EXACT
    row = await без_api._row(db_sessionmaker, cid)
    assert row.geo_provider == "nominatim"
    assert (row.geo_without_dadata, row.geo_verdict_version) == (True, g.VERDICT_VERSION)

    # Новые сутки: DaData в деле — та же строка после сброса судится с ней.
    await redis.delete(worker.dadata_calls_key())
    async with db_sessionmaker() as s:
        clients_svc.сбросить_вердикт(
            await s.get(ClientAddressCandidate, cid), reason="settlement_named"
        )
        await s.commit()
    assert (await без_api._row(db_sessionmaker, cid)).geo_verdict_version is None
    assert await worker.geocode_candidate(ctx, cid) == g.GEO_EXACT
    row = await без_api._row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata"
    assert (row.geo_without_dadata, row.geo_verdict_version) == (False, g.VERDICT_VERSION)


async def test_воркер_пишет_флаг_и_на_отложенный_отказ(
    seed_conversation, db_sessionmaker, redis, dadata_дом, monkeypatch
):
    """Отказ OSM без DaData откладывается `pending` — флаг ложится и на него:
    потребитель смотрит только RECHECK_STATUSES, один код на все статусы."""

    async def пусто(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return []

    monkeypatch.setattr(worker.nominatim, "search", пусто)
    cid = await без_api._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await без_api._потолок_dadata_выбран(redis)
    assert await worker.geocode_candidate(без_api.ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await без_api._row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_without_dadata) == (g.GEO_PENDING, True)
    assert row.geo_verdict_version == g.VERDICT_VERSION


def test_сброс_вердикта_ключи_и_orm_сброс_совпадают(
    seed_conversation: Any,
) -> None:
    с_попытками = clients_svc.сброс_вердикта(с_попытками=True, улики=None)
    с_нуля = clients_svc.сброс_вердикта(с_попытками=False, улики=None)
    assert set(с_нуля) - set(с_попытками) == {"geo_attempts"}
    assert "geo_attempts" not in с_попытками and с_нуля["geo_attempts"] == 0
    assert с_попытками["geo_checked_at"] is None and с_попытками["geo_status"] == g.GEO_PENDING
    row = ClientAddressCandidate(
        id=uuid.uuid4(),
        client_id=seed_conversation.client_id,
        conversation_id=seed_conversation.conversation_id,
        value="ул Ленина, 5",
        street="ул Ленина",
        house="5",
        raw="ул Ленина 5",
        level="A",
        geo_status=g.GEO_AMBIGUOUS,
        geo_attempts=3,
        geo_lat=1.0,
        geo_lon=2.0,
        geo_provider="dadata",
        geo_verdict_version=g.VERDICT_VERSION,
        geo_without_dadata=True,
    )
    clients_svc.сбросить_вердикт(row, reason="settlement_named")
    assert {k: getattr(row, k) for k in с_нуля} == с_нуля


async def test_recheck_пишет_в_журнал_rowcount_а_не_предварительный_count(
    seed_conversation, db_sessionmaker, monkeypatch, capsys
):
    """Ревью 19.09, C3. Между COUNT и UPDATE оператор принял одну из двух
    строк: на экране «строк … : 2» (COUNT), а в `geocode.requeue rows=` и в
    «возвращено в очередь» — 1, сколько UPDATE задел на самом деле. Диверсия:
    вернуть в журнал `всего` — падает."""
    from app.cli import run_address_recheck

    a = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=ЧАС, статус=g.GEO_NOT_FOUND
    )
    b = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Мира 7", возраст=ЧАС, статус=g.GEO_NOT_FOUND
    )
    async with db_sessionmaker() as s:
        настоящий = s.execute

        async def execute(stmt: Any, *args: Any, **kw: Any) -> Any:
            if isinstance(stmt, sa.Update):
                # Оператор принял строку b, пока команда шла от COUNT к UPDATE.
                await настоящий(
                    sa.update(ClientAddressCandidate)
                    .where(ClientAddressCandidate.id == b)
                    .values(status=CANDIDATE_ACCEPTED)
                )
            return await настоящий(stmt, *args, **kw)

        monkeypatch.setattr(s, "execute", execute)
        with structlog.testing.capture_logs() as логи:
            await run_address_recheck(s, statuses="not_found", dry_run=False)
    экран = capsys.readouterr().out
    assert "['not_found']: 2;" in экран and "возвращено в очередь: 1" in экран, экран
    assert [(л["reason"], л["rows"]) for л in логи if л["event"] == "geocode.requeue"] == [
        ("cli_recheck", 1)
    ]
    assert (await _статус(db_sessionmaker, a))[0] == g.GEO_PENDING
    assert (await _статус(db_sessionmaker, b))[0] == g.GEO_NOT_FOUND


async def test_сброс_вердикта_один_набор_на_все_пути(
    client, tokens, seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Обход починки, `address-recheck` и ручка смены провайдера берут поля у
    одного строителя — сторож по коду (шпион на функции), не грепом; и каждая
    из трёх строк после сброса несёт ровно его значения."""
    from app.cli import run_address_recheck
    from tests.unit.test_settings_address_detect_1109 import hdr

    вызовы: list[bool] = []
    настоящий = clients_svc.сброс_вердикта

    def шпион(*, с_попытками: bool, улики: dict[str, Any] | None) -> dict[str, Any]:
        вызовы.append(с_попытками)
        return настоящий(с_попытками=с_попытками, улики=улики)

    monkeypatch.setattr(clients_svc, "сброс_вердикта", шпион)

    async def как_у_строителя(cid: uuid.UUID, *, с_попытками: bool) -> None:
        async with db_sessionmaker() as s:
            row = await s.get(ClientAddressCandidate, cid)
            for k, v in настоящий(с_попытками=с_попытками, улики=None).items():
                if k == "geo_prev":
                    # Снимок улик — свой у каждого пути (пакет 5), его держит
                    # test_paket5_2009; здесь — остальные поля одного строителя.
                    continue
                assert getattr(row, k) == v, (k, getattr(row, k))

    # 1. обход починки — попытки сохраняются.
    обход = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Ленина 5", возраст=28 * ЧАС
    )
    async with db_sessionmaker() as s:
        ids = await geo_repair.перепривязать(
            s, reason="verdict_version", условие=sa.true(), limit=5
        )
        await s.commit()
    assert ids == [обход] and вызовы == [True]
    await как_у_строителя(обход, с_попытками=True)
    assert (await без_api._row(db_sessionmaker, обход)).geo_attempts == 1

    # 2. address-recheck — с нуля (и теперь без provider/lat/lon).
    recheck = await _строка_обхода(
        db_sessionmaker,
        seed_conversation,
        "ул Мира 7",
        возраст=ЧАС,
        статус=g.GEO_NOT_FOUND,
        geo_lat=1.0,
        geo_lon=2.0,
    )
    async with db_sessionmaker() as s:
        await run_address_recheck(s, statuses="not_found", dry_run=False)
    assert вызовы == [True, False]
    await как_у_строителя(recheck, с_попытками=False)

    # 3. ручка смены провайдера — снимает бан тем же набором.
    бан = await _строка_обхода(
        db_sessionmaker, seed_conversation, "ул Кирова 9", возраст=ЧАС, статус=g.GEO_BLOCKED
    )
    res = await client.patch(
        "/api/v1/settings/address-detect", json={"provider": "yandex"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200, res.text
    assert вызовы == [True, False, False]
    await как_у_строителя(бан, с_попытками=False)
