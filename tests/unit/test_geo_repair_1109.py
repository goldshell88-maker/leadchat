"""Починка проверок по карте: что не поставилось и что не ответило — в очередь, в темпе.

Ставит только `pending`/`error` с запасом попыток, новые вперёд, порцией и с
шагом; `blocked` и исчерпавшие попытки не трогает; выключатель молчит.

ДИВЕРСИИ (каждая обязана краснеть): снять потолок попыток → безнадёжная
строка ставится вечно; снять паузу → `error` переспрашивается каждые 10
минут; снять выключатель → починка ходит к карте при выключенной проверке.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations import gateway
from app.models import Client, ClientAddressCandidate
from app.scheduler.jobs import geo_repair
from app.services import address_parse, app_settings
from app.services import clients as clients_svc

pytestmark = pytest.mark.anyio


async def _строка(db_sessionmaker, seed, текст: str, **geo) -> ClientAddressCandidate:
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed.client_id)
        found = address_parse.parse(текст)
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seed.conversation_id,
            message_id=None,
            message_at=None,
            found=found,
            now=datetime.now(UTC),
        )
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
        for k, v in geo.items():
            setattr(row, k, v)
        await s.commit()
        return row


@pytest.fixture
def оснастка(monkeypatch: Any, db_sessionmaker: Any, redis: Any) -> None:
    monkeypatch.setattr(geo_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(geo_repair.db_mod, "session_scope", db_sessionmaker)


async def test_непроверенные_встают_в_очередь_с_шагом(
    seed_conversation, db_sessionmaker, redis, оснастка
):
    a = await _строка(db_sessionmaker, seed_conversation, "ул Ленина 5")
    b = await _строка(db_sessionmaker, seed_conversation, "ул Мира 7")
    assert await geo_repair.repair_geocodes() == 2
    assert await redis.exists(f"arq:job:geocode:{a.id}:repair")
    assert await redis.exists(f"arq:job:geocode:{b.id}:repair")
    # Вторая задача отложена на шаг: у неё в очереди более поздний срок.
    сроки = await redis.zrange("arq:queue", 0, -1, withscores=True)
    assert len(сроки) == 2
    assert abs(сроки[1][1] - сроки[0][1]) >= geo_repair.ШАГ_СЕК * 1000 - 50


async def test_ошибка_ждёт_паузу_а_потолок_попыток_закрывает_дверь(
    seed_conversation, db_sessionmaker, redis, оснастка
):
    только_что = datetime.now(UTC)
    свежая = await _строка(
        db_sessionmaker,
        seed_conversation,
        "ул Ленина 5",
        geo_status="error",
        geo_attempts=1,
        geo_checked_at=только_что,
    )
    давняя = await _строка(
        db_sessionmaker,
        seed_conversation,
        "ул Мира 7",
        geo_status="error",
        geo_attempts=1,
        geo_checked_at=только_что - timedelta(minutes=11),
    )
    безнадёжная = await _строка(
        db_sessionmaker,
        seed_conversation,
        "ул Победы 9",
        geo_status="error",
        geo_attempts=geo_repair.ПОТОЛОК_ПОПЫТОК,
        geo_checked_at=только_что - timedelta(days=3),
    )
    # Бан переспрашивается не раньше чем через шесть часов.
    запрет = await _строка(
        db_sessionmaker,
        seed_conversation,
        "ул Кирова 3",
        geo_status="blocked",
        geo_attempts=1,
        geo_checked_at=только_что - timedelta(hours=2),
    )
    старый_запрет = await _строка(
        db_sessionmaker,
        seed_conversation,
        "ул Мира 12",
        geo_status="blocked",
        geo_attempts=1,
        geo_checked_at=только_что - timedelta(hours=7),
    )
    assert await geo_repair.repair_geocodes() == 2
    assert await redis.exists(f"arq:job:geocode:{давняя.id}:repair")
    assert await redis.exists(f"arq:job:geocode:{старый_запрет.id}:repair")
    assert not await redis.exists(f"arq:job:geocode:{свежая.id}:repair")
    assert not await redis.exists(f"arq:job:geocode:{безнадёжная.id}:repair")
    assert not await redis.exists(f"arq:job:geocode:{запрет.id}:repair")


async def test_пауза_растёт_вдвое() -> None:
    now = datetime.now(UTC)
    assert geo_repair._пора(0, None, now)
    assert not geo_repair._пора(2, now - timedelta(minutes=15), now)
    assert geo_repair._пора(2, now - timedelta(minutes=21), now)
    assert not geo_repair._пора(3, now - timedelta(minutes=39), now)


async def test_выключатель_останавливает_починку(
    seed_conversation, db_sessionmaker, redis, оснастка
):
    a = await _строка(db_sessionmaker, seed_conversation, "ул Ленина 5")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
        await s.commit()
    assert await geo_repair.repair_geocodes() == 0
    assert not await redis.exists(f"arq:job:geocode:{a.id}:repair")


def test_починка_регистрируется_в_планировщике_с_интервалом() -> None:
    """Сторож на `register()`, а не на импорт: импорт без вызова — «написано,
    но не подключено» (ревью 11.09)."""
    from apscheduler.triggers.interval import IntervalTrigger

    вызовы: list[dict] = []

    class Планировщик:
        def add_job(self, func, trigger, **kw):  # noqa: ANN001
            вызовы.append({"func": func, "trigger": trigger, **kw})

    geo_repair.register(Планировщик())
    (вызов,) = вызовы
    assert вызов["func"] is geo_repair.repair_geocodes
    assert вызов["id"] == geo_repair.JOB_ID
    assert isinstance(вызов["trigger"], IntervalTrigger)
    assert вызов["max_instances"] == 1


def test_планировщик_зовёт_register_починки() -> None:
    import ast
    import inspect
    import textwrap

    from app.scheduler import main as scheduler_main

    дерево = ast.parse(textwrap.dedent(inspect.getsource(scheduler_main)))
    зовут = {
        f"{getattr(у.func.value, 'id', '')}.{у.func.attr}"
        for у in ast.walk(дерево)
        if isinstance(у, ast.Call) and isinstance(у.func, ast.Attribute)
    }
    assert "geo_repair_jobs.register" in зовут


async def test_починка_ждёт_пока_dadata_выбрана_и_пока_очередь_не_рассосалась(
    seed_conversation, db_sessionmaker, redis, оснастка, monkeypatch
):
    """Бой 13.09: DaData ответила 403 на суточный лимит — хвост через OSM с
    окончательными отказами не гоним, ждём московской полуночи; и не кладём
    порцию, пока в общей очереди затор (живые реплики вперёд)."""
    from app.workers import geocode as worker

    # Флаг 403 останавливает починку только при настроенной DaData (ревью 13.09).
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    await _строка(db_sessionmaker, seed_conversation, "ул Ленина 5")
    await worker._закрыть_на_сутки(redis, "dadata")
    assert await geo_repair.repair_geocodes() == 0
    await redis.delete("geo:exhausted:dadata")
    for i in range(geo_repair.ЗАТОР + 1):
        await redis.zadd("arq:queue", {f"чужая-{i}": i})
    assert await geo_repair.repair_geocodes() == 0
    await redis.delete("arq:queue")
    assert await geo_repair.repair_geocodes() == 1
