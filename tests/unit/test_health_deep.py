"""GET /api/health/deep (05 §7.2): без auth, всегда 200, без секретов."""

from datetime import UTC, datetime, timedelta

import pytest

from app.api import deps
from app.core.config import settings
from app.models import AvitoAccount, Message, WebhookRawLog

DEEP = "/api/health/deep"


@pytest.fixture(autouse=True)
def _stable_thresholds(monkeypatch):
    """Пороги «красного» фиксируем: тест не должен зависеть от прод-настроек."""
    monkeypatch.setattr(settings, "health_queue_len_red", 1000)
    monkeypatch.setattr(settings, "health_oldest_pending_sec_red", 300)
    monkeypatch.setattr(settings, "health_failed_last_hour_red", 10)
    monkeypatch.setattr(settings, "health_deep_check_scheduler", False)
    monkeypatch.setattr(settings, "health_dlq_fresh_sec_red", 86_400)


async def test_deep_ok_on_empty_system(client):
    r = await client.get(DEEP)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks_failed"] == []
    assert body["red"] == []
    assert body["db"] is True and body["redis"] is True
    assert body["queue"] == {
        "len": 0,
        "pending": 0,
        "oldest_pending_sec": 0,
        "stream_len": 0,
        "dlq": 0,
        "dlq_age_sec": None,
    }
    assert body["accounts"]["needs_reauth"] == 0
    assert body["delivery"]["failed_last_hour"] == 0
    assert body["webhooks"]["last_inbound_age_sec"] is None
    assert body["scheduler"]["alive"] is False


async def test_deep_requires_no_auth(client):
    r = await client.get(DEEP)  # ни одного заголовка Authorization
    assert r.status_code == 200


async def test_deep_leaks_no_secrets(client, make_avito_account):
    """Агрегаты не секретны, детали-имена — секретны (05 §7.2)."""
    account = await make_avito_account(webhook_secret="whsec-super-secret", title="LP-Москва")
    raw = (await client.get(DEEP)).text
    assert account.webhook_secret not in raw
    assert account.title not in raw
    assert str(account.id) not in raw
    assert settings.database_url not in raw
    assert settings.jwt_secret not in raw


async def test_deep_counts_accounts_and_expiring_tokens(client, make_avito_account):
    await make_avito_account(avito_user_id=1001, status="active")
    await make_avito_account(avito_user_id=1002, status="disabled")
    body = (await client.get(DEEP)).json()
    assert body["accounts"]["active"] == 1
    assert body["accounts"]["disabled"] == 1
    assert body["accounts"]["tokens_expiring_2h"] == 0  # seed-токены живут сутки


async def test_deep_degrades_on_needs_reauth(client, make_avito_account):
    """needs_reauth > 0 — красное правило 05 §7.2 (аккаунт отвалился)."""
    await make_avito_account(avito_user_id=1003, status="needs_reauth")
    body = (await client.get(DEEP)).json()
    assert body["status"] == "degraded"
    assert body["accounts"]["needs_reauth"] == 1
    # Какое правило покраснело — видно из ответа, без пересчёта по числам.
    assert "accounts.needs_reauth" in body["red"]


async def test_deep_reports_expiring_tokens(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        session.add(
            AvitoAccount(
                title="LP-Expiring",
                avito_user_id=1004,
                access_token_enc=b"enc",
                refresh_token_enc=b"enc",
                token_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                status="active",
                webhook_secret="whsec-x",
            )
        )
        await session.commit()
    body = (await client.get(DEEP)).json()
    assert body["accounts"]["tokens_expiring_2h"] == 1
    assert body["status"] == "ok"  # само по себе не авария — это работа планировщика


async def test_deep_counts_failed_deliveries_last_hour(client, db_sessionmaker, seed_conversation):
    conv_id = seed_conversation.conversation_id
    async with db_sessionmaker() as session:
        for offset, status in ((5, "failed"), (30, "failed"), (5, "delivered")):
            session.add(
                Message(
                    conversation_id=conv_id,
                    direction="out",
                    sender_type="operator",
                    body="…",
                    attachments=[],
                    delivery_status=status,
                    created_at=datetime.now(UTC) - timedelta(minutes=offset),
                )
            )
        session.add(  # старше часа — в окно не попадает
            Message(
                conversation_id=conv_id,
                direction="out",
                sender_type="operator",
                body="…",
                attachments=[],
                delivery_status="failed",
                created_at=datetime.now(UTC) - timedelta(hours=3),
            )
        )
        await session.commit()

    body = (await client.get(DEEP)).json()
    assert body["delivery"]["failed_last_hour"] == 2
    assert body["status"] == "ok"  # порог 10/час ещё не пробит


async def test_deep_degrades_when_delivery_over_threshold(
    client, db_sessionmaker, seed_conversation, monkeypatch
):
    monkeypatch.setattr(settings, "health_failed_last_hour_red", 1)
    async with db_sessionmaker() as session:
        for _ in range(2):
            session.add(
                Message(
                    conversation_id=seed_conversation.conversation_id,
                    direction="out",
                    sender_type="operator",
                    body="…",
                    attachments=[],
                    delivery_status="failed",
                    created_at=datetime.now(UTC),
                )
            )
        await session.commit()
    body = (await client.get(DEEP)).json()
    assert body["status"] == "degraded"


async def test_deep_reports_last_inbound_age(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        session.add(
            WebhookRawLog(
                id=1,  # в PG колонка IDENTITY, в SQLite её надо задать руками
                account_id=None,
                stream_id="1-1",
                payload={"ok": True},
                received_at=datetime.now(UTC) - timedelta(seconds=42),
                processed=True,
            )
        )
        await session.commit()
    body = (await client.get(DEEP)).json()
    age = body["webhooks"]["last_inbound_age_sec"]
    assert age is not None and 30 <= age <= 120


async def test_deep_reads_queue_length(client, redis):
    """`len` — отставание группы, а не длина стрима."""
    await redis.xgroup_create("webhooks:avito", "workers", id="0", mkstream=True)
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    body = (await client.get(DEEP)).json()
    assert body["queue"]["len"] == 2
    assert body["queue"]["stream_len"] == 2
    assert body["queue"]["pending"] == 0  # ещё не выданы консьюмеру
    assert body["status"] == "ok"


async def test_deep_queue_len_is_backlog_not_stream_length(client, redis):
    """Разобранные записи из стрима не удаляются — и не должны считаться лагом.

    Redis Streams не подрезаются сами: XLEN растёт всё время жизни сервиса, и
    порог `health_queue_len_red` на нём срабатывал бы навсегда после первой
    тысячи вебхуков (найдено нагрузочным прогоном 07 §3).
    """
    await redis.xgroup_create("webhooks:avito", "workers", id="0", mkstream=True)
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    batches = await redis.xreadgroup("workers", "c1", {"webhooks:avito": ">"}, count=10)
    for _stream, entries in batches:
        for entry_id, _fields in entries:
            await redis.xack("webhooks:avito", "workers", entry_id)

    body = (await client.get(DEEP)).json()
    assert body["queue"]["len"] == 0  # всё разобрано
    assert body["queue"]["pending"] == 0
    assert body["queue"]["stream_len"] == 2  # записи физически остались в стриме
    assert body["status"] == "ok"


async def test_deep_reports_pending_entries(client, redis):
    """Запись взята консьюмером и не подтверждена — это лаг воркера (PEL)."""
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    await redis.xgroup_create("webhooks:avito", "workers", id="0")
    await redis.xreadgroup("workers", "c1", {"webhooks:avito": ">"}, count=10)

    body = (await client.get(DEEP)).json()
    assert body["queue"]["pending"] == 1
    assert body["queue"]["oldest_pending_sec"] is not None
    assert body["queue"]["oldest_pending_sec"] < 60
    assert body["status"] == "ok"  # порог — 5 минут, свежий pending нормален


async def test_deep_degrades_on_stuck_pending(client, redis):
    """oldest_pending_sec > 5 минут — воркер не разбирает очередь (05 §7.2)."""
    hour_ago_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    await redis.xadd("webhooks:avito", {"payload": "{}"}, id=f"{hour_ago_ms}-0")
    await redis.xgroup_create("webhooks:avito", "workers", id="0")
    await redis.xreadgroup("workers", "c1", {"webhooks:avito": ">"}, count=10)

    body = (await client.get(DEEP)).json()
    assert body["queue"]["pending"] == 1
    assert body["queue"]["oldest_pending_sec"] > 3000  # возраст берётся из id записи
    assert body["status"] == "degraded"


async def test_deep_degrades_on_long_queue(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "health_queue_len_red", 1)
    await redis.xgroup_create("webhooks:avito", "workers", id="0", mkstream=True)
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    await redis.xadd("webhooks:avito", {"payload": "{}"})
    body = (await client.get(DEEP)).json()
    assert body["status"] == "degraded"


async def test_deep_reports_dlq_and_degrades(client, redis):
    """Брошенный вебхук — потерянное обращение клиента, и его должно быть видно.

    До 11 августа поток `webhooks:avito:dlq` был односторонним: воркер туда
    писал (`app/workers/inbound.py:_reclaim_stuck`), читателя не было ни одного,
    и сообщения копились невидимо — ни в интерфейсе, ни в мониторинге.
    """
    await redis.xadd("webhooks:avito:dlq", {"payload": "{}", "orig_id": "1-1"})
    body = (await client.get(DEEP)).json()
    assert body["queue"]["dlq"] == 1
    assert body["queue"]["dlq_age_sec"] is not None
    assert body["queue"]["dlq_age_sec"] < 60
    assert body["status"] == "degraded"  # порога нет: одна потеря — уже беда


async def test_deep_dlq_alarm_expires_but_count_stays(client, redis, monkeypatch):
    """Старая потеря гасит тревогу, но не исчезает из ответа.

    DLQ никто не подрезает, счётчик только растёт. Красное «пока dlq > 0»
    означало бы вечно деградированный /deep — и Kuma, которая после первой
    потери больше не отличает упавшую БД от прошлогодней записи.
    """
    week_ago_ms = int((datetime.now(UTC) - timedelta(days=7)).timestamp() * 1000)
    await redis.xadd("webhooks:avito:dlq", {"payload": "{}"}, id=f"{week_ago_ms}-0")
    body = (await client.get(DEEP)).json()
    assert body["queue"]["dlq"] == 1  # число никуда не делось
    assert body["queue"]["dlq_age_sec"] > 6 * 24 * 3600
    assert body["status"] == "ok"  # но тревога истекла

    monkeypatch.setattr(settings, "health_dlq_fresh_sec_red", 30 * 24 * 3600)
    assert (await client.get(DEEP)).json()["status"] == "degraded"


async def test_deep_ok_when_dlq_stream_absent(client, redis):
    """Пустого потока в Redis не существует — это норма, а не сбой датчика."""
    body = (await client.get(DEEP)).json()
    assert body["queue"]["dlq"] == 0
    assert body["queue"]["dlq_age_sec"] is None
    assert body["checks_failed"] == []
    assert body["status"] == "ok"


async def test_deep_sees_scheduler_heartbeat(client, redis):
    await redis.set("scheduler:alive", datetime.now(UTC).isoformat(), ex=120)
    body = (await client.get(DEEP)).json()
    assert body["scheduler"]["alive"] is True
    assert body["scheduler"]["heartbeat_age_sec"] is not None
    assert body["scheduler"]["heartbeat_age_sec"] < 5


async def test_deep_stale_scheduler_degrades_only_when_enabled(client, redis, monkeypatch):
    await redis.set("scheduler:alive", (datetime.now(UTC) - timedelta(hours=1)).isoformat(), ex=120)
    assert (await client.get(DEEP)).json()["status"] == "ok"  # выключено по умолчанию

    monkeypatch.setattr(settings, "health_deep_check_scheduler", True)
    body = (await client.get(DEEP)).json()
    assert body["scheduler"]["alive"] is False
    assert body["status"] == "degraded"


class _BrokenSession:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("db is down")


class _BrokenRedis:
    async def ping(self):
        raise ConnectionError("redis is down")

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise ConnectionError("redis is down")

        return _boom


async def test_deep_survives_db_down(app, client):
    async def broken_db():
        yield _BrokenSession()

    app.dependency_overrides[deps.get_db] = broken_db
    r = await client.get(DEEP)
    assert r.status_code == 200  # деградация, не 500 — иначе мониторинг слепнет
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] is False
    assert set(body["checks_failed"]) >= {"accounts", "delivery", "webhooks"}


async def test_deep_survives_redis_down(app, client):
    app.dependency_overrides[deps.get_redis] = lambda: _BrokenRedis()
    r = await client.get(DEEP)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["redis"] is False


async def test_plain_health_still_ok(client):
    """Спринт 4 не трогает контракт лёгкого /api/health (SM-1)."""
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "db": True,
        "redis": True,
        "version": "test-version",
    }
