"""INT: `/api/v1/bots/*` против настоящего Postgres — 01 §8, §12–13.

Почему integration, а не unit: сценарий лежит в `jsonb`, привязка бота — в
`avito_accounts.bot_id` с внешним ключом, а `409` при удалении зависит от
того, что этот ключ действительно есть. SQLite всё это «прощает», Postgres —
нет, и именно его поведение уезжает в прод.

Проверяем три вещи, которые ломаются тише всего:
* права — `bots:manage` только у admin (01 §12), включая песочницу;
* `PUT` заменяет сценарий ЦЕЛИКОМ и поднимает `revision` (01 §8.3);
* удаление привязанного бота — `409`, а не каскад (01 §8.7).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import deps
from app.bots.scenarios import default_scenario
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models import AuditLog, AvitoAccount, Bot, Client, Conversation, User
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

ROLES = ("admin", "head", "manager", "observer")
KB = "Замена экрана iPhone 13 — от 8900 ₽, срок 1–2 часа."


@pytest.fixture
async def pg_sessionmaker(pg_async_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NullPool: pytest-asyncio даёт свой event loop на каждый тест.
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_bots(pg_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """База переиспользуется всей сессией тестов — чистим за собой."""
    yield
    async with pg_sessionmaker() as db:
        await db.execute(update(AvitoAccount).values(bot_id=None))
        await db.execute(delete(Conversation))
        await db.execute(delete(Client))
        await db.execute(delete(AvitoAccount))
        await db.execute(delete(Bot))
        await db.execute(delete(AuditLog))
        await db.commit()


@pytest.fixture
async def client(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    # Роутер ботов монтирует сама фабрика `app/main.py`; сторож монтажа —
    # tests/unit/test_bot_wiring.py::test_bots_router_is_mounted.
    app = create_app()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pg_sessionmaker() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c
    await redis.aclose()


@pytest.fixture
async def tokens(pg_sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    out: dict[str, str] = {}
    async with pg_sessionmaker() as db:
        for role in ROLES:
            email = f"bots-{role}@leadchat.test"
            user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                user = User(
                    email=email,
                    password_hash=hash_password("integration-pass-123"),
                    full_name=f"Бот-тест {role}",
                    role=role,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
            out[role] = create_access_token(user_id=str(user.id), role=user.role)
    return out


def auth(tokens: dict[str, str], role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def account_factory(pg_sessionmaker: async_sessionmaker[AsyncSession]):
    async def _make(title: str) -> uuid.UUID:
        async with pg_sessionmaker() as db:
            account = AvitoAccount(
                id=uuid.uuid4(),
                title=title,
                avito_user_id=int(uuid.uuid4().int % 10**12) + 10**12,
                access_token_enc=b"enc",
                refresh_token_enc=b"enc",
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
                status="active",
                webhook_secret="whsec",
            )
            db.add(account)
            await db.commit()
            return account.id

    return _make


@pytest.fixture
async def conversation_factory(pg_sessionmaker: async_sessionmaker[AsyncSession]):
    """Диалог со следом бота в `bot_vars` и заданным возрастом `updated_at`."""

    async def _make(account_id: uuid.UUID, *, bot_id: str | None, days_ago: int) -> uuid.UUID:
        moment = datetime.now(UTC) - timedelta(days=days_ago)
        async with pg_sessionmaker() as db:
            client_row = Client(
                id=uuid.uuid4(), channel="avito", external_id=uuid.uuid4().hex, name="Клиент"
            )
            db.add(client_row)
            await db.flush()
            conv = Conversation(
                id=uuid.uuid4(),
                channel="avito",
                external_chat_id=uuid.uuid4().hex,
                account_id=account_id,
                client_id=client_row.id,
                status="new",
                bot_active=False,
                bot_vars={"bot_id": bot_id} if bot_id else {},
                tags=[],
                unread_count=0,
                updated_at=moment,
            )
            db.add(conv)
            await db.commit()
            # onupdate/server_default могли переписать метку — ставим явно.
            await db.execute(
                update(Conversation).where(Conversation.id == conv.id).values(updated_at=moment)
            )
            await db.commit()
            return conv.id

    return _make


async def create_bot(client: httpx.AsyncClient, tokens: dict[str, str], **body: Any) -> dict:
    payload = {"name": "Первичный приём", "knowledge_base": KB, **body}
    r = await client.post("/api/v1/bots", json=payload, headers=auth(tokens))
    assert r.status_code == 201, r.text
    return r.json()


# --- права (01 §12–13): bots:manage — только admin ---------------------------


@pytest.mark.parametrize("role", ["head", "manager", "observer"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/bots"),
        ("GET", "/api/v1/bots/{id}"),
        ("POST", "/api/v1/bots"),
        ("PUT", "/api/v1/bots/{id}"),
        ("POST", "/api/v1/bots/{id}/enable"),
        ("POST", "/api/v1/bots/{id}/disable"),
        ("PUT", "/api/v1/bots/{id}/accounts"),
        ("DELETE", "/api/v1/bots/{id}"),
        ("POST", "/api/v1/bots/sandbox/start"),
        ("POST", "/api/v1/bots/sandbox/xyz/message"),
        ("POST", "/api/v1/bots/sandbox/xyz/fire-timeout"),
        ("DELETE", "/api/v1/bots/sandbox/xyz"),
    ],
)
async def test_every_bot_endpoint_is_admin_only(client, tokens, role, method, path):
    bot = await create_bot(client, tokens)
    url = path.format(id=bot["id"])
    r = await client.request(method, url, json={}, headers=auth(tokens, role))
    assert r.status_code == 403, f"{method} {url} для {role}: {r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "forbidden"


async def test_bot_endpoints_require_authentication(client):
    r = await client.get("/api/v1/bots")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_admin_reads_the_list(client, tokens, account_factory):
    account_id = await account_factory("LP-Москва")
    bot = await create_bot(client, tokens)
    await client.put(
        f"/api/v1/bots/{bot['id']}/accounts",
        json={"account_ids": [str(account_id)]},
        headers=auth(tokens),
    )

    r = await client.get("/api/v1/bots", headers=auth(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page"] == {"limit": 50, "offset": 0, "total": 1}
    item = body["items"][0]
    assert item["name"] == "Первичный приём"
    assert item["scenario_steps_count"] == len(default_scenario()["steps"])
    assert item["knowledge_base_present"] is True
    assert item["accounts"] == [{"id": str(account_id), "title": "LP-Москва"}]
    assert item["conversations_7d"] == 0  # диалогов ещё не было


async def test_conversations_7d_counts_only_this_bots_last_week(
    client, tokens, account_factory, pg_sessionmaker, conversation_factory
):
    """Колонка «Диалогов/7д» (11 §5.1) — по следу бота в bot_vars, не по аккаунту.

    Считаем именно `bot_vars.bot_id`: привязка аккаунта могла смениться вчера,
    а диалоги за неделю вёл предыдущий бот — по аккаунту счёт был бы враньём.
    """
    account_id = await account_factory("LP-Москва")
    mine = await create_bot(client, tokens, name="Мой")
    other = await create_bot(client, tokens, name="Чужой")

    await conversation_factory(account_id, bot_id=mine["id"], days_ago=1)
    await conversation_factory(account_id, bot_id=mine["id"], days_ago=6)
    await conversation_factory(account_id, bot_id=mine["id"], days_ago=8)  # старше недели
    await conversation_factory(account_id, bot_id=other["id"], days_ago=1)  # чужой бот
    await conversation_factory(account_id, bot_id=None, days_ago=1)  # бот не заходил

    r = await client.get("/api/v1/bots", headers=auth(tokens))
    assert r.status_code == 200, r.text
    # Не сравниваем весь список: в чистой базе живёт ещё сид «Первичного
    # приёма» из миграции 0005, и порядок тестов на него влиять не должен.
    counts = {i["name"]: i["conversations_7d"] for i in r.json()["items"]}
    assert counts["Мой"] == 2, "чужие и просроченные диалоги попали в счёт"
    assert counts["Чужой"] == 1


# --- создание (01 §8.3) ------------------------------------------------------


async def test_new_bot_gets_a_copy_of_the_default_scenario(client, tokens):
    """«Создать бота» = копия «Первичного приёма» (02 §5.1, 11 §5.1)."""
    bot = await create_bot(client, tokens, name="Ночной дежурный")
    ids = [s["id"] for s in bot["scenario"]["steps"]]
    assert ids == [s["id"] for s in default_scenario()["steps"]]
    assert bot["scenario"]["revision"] == 1
    assert bot["is_enabled"] is True
    assert bot["schedule"] == {"always": True, "timezone": "Europe/Moscow", "intervals": []}


async def test_invalid_scenario_is_rejected_with_the_editor_contract(client, tokens):
    scenario = default_scenario()
    scenario["steps"][0]["next"] = "tag_contct"
    r = await client.post(
        "/api/v1/bots",
        json={"name": "Кривой", "scenario": scenario, "knowledge_base": KB},
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "bot_scenario_invalid"
    assert error["details"]["step_id"] == "greet"
    assert error["details"]["reason"] == "broken_ref"
    assert error["details"]["ref"] == "tag_contct"
    assert {"step_id", "field", "code", "level", "message"} <= set(error["details"]["issues"][0])

    listing = await client.get("/api/v1/bots", headers=auth(tokens))
    assert listing.json()["page"]["total"] == 0  # ничего не сохранилось


async def test_warnings_do_not_block_saving(client, tokens):
    """`ai_without_kb` — предупреждение: «Сохранить с предупреждениями» (02 §5.2)."""
    r = await client.post(
        "/api/v1/bots",
        json={"name": "Без базы знаний", "knowledge_base": ""},
        headers=auth(tokens),
    )
    assert r.status_code == 201, r.text
    assert r.json()["knowledge_base"] is None


# --- PUT: полная замена (01 §8.3) -------------------------------------------


async def test_put_replaces_the_whole_scenario_and_bumps_the_revision(
    client, tokens, pg_sessionmaker
):
    bot = await create_bot(client, tokens)
    assert bot["scenario"]["revision"] == 1

    replacement = {
        "version": 1,
        "revision": 999,  # ревизией владеет сервер, а не клиент
        "entry": "hi",
        "steps": [
            {"id": "hi", "type": "send", "params": {"text": "Здравствуйте!"}, "next": "bye"},
            {"id": "bye", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }
    r = await client.put(
        f"/api/v1/bots/{bot['id']}",
        json={
            "name": "Переписанный",
            "is_enabled": False,
            "scenario": replacement,
            "knowledge_base": "новая база",
            "schedule": {"always": True},
        },
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["id"] for s in body["scenario"]["steps"]] == ["hi", "bye"]  # старых шагов нет
    assert body["scenario"]["revision"] == 2
    assert body["name"] == "Переписанный"
    # Включение PUT не меняет (проверка 24.09): для него `enable`/`disable`, а
    # черновик из устаревшего кэша включал выключенного бота обратно.
    assert body["is_enabled"] is bot["is_enabled"] is True
    assert body["knowledge_base"] == "новая база"

    async with pg_sessionmaker() as db:
        stored = (await db.execute(select(Bot))).scalar_one()
        assert len(stored.scenario["steps"]) == 2

    # audit: bot.updated с ревизией и списком изменённых шагов (02 §5.2)
    async with pg_sessionmaker() as db:
        rows = (
            await db.execute(select(AuditLog).where(AuditLog.action == "bot.updated"))
        ).scalars()
        updates = [r for r in rows if r.details.get("revision") == 2]
    assert updates, "PUT обязан писать bot.updated"
    assert "greet" in updates[0].details["diff_steps"]
    assert updates[0].entity == "bot" and updates[0].entity_id == bot["id"]


async def test_put_without_a_scenario_is_rejected(client, tokens):
    bot = await create_bot(client, tokens)
    r = await client.put(
        f"/api/v1/bots/{bot['id']}", json={"name": "Только имя"}, headers=auth(tokens)
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"


async def test_patch_is_not_offered(client, tokens):
    """PATCH у ботов нет намеренно: сценарий — атомарный документ (01 §8.3)."""
    bot = await create_bot(client, tokens)
    r = await client.patch(f"/api/v1/bots/{bot['id']}", json={"name": "х"}, headers=auth(tokens))
    assert r.status_code == 405


async def test_unknown_bot_is_404(client, tokens):
    r = await client.get(f"/api/v1/bots/{uuid.uuid4()}", headers=auth(tokens))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# --- enable/disable (01 §8.4) ------------------------------------------------


async def test_enable_and_disable(client, tokens):
    bot = await create_bot(client, tokens)
    off = await client.post(f"/api/v1/bots/{bot['id']}/disable", headers=auth(tokens))
    assert off.status_code == 200 and off.json()["is_enabled"] is False
    on = await client.post(f"/api/v1/bots/{bot['id']}/enable", headers=auth(tokens))
    assert on.status_code == 200 and on.json()["is_enabled"] is True


# --- привязка аккаунтов (01 §8.5) -------------------------------------------


async def test_accounts_are_rebound_and_unbound(client, tokens, account_factory):
    first = await account_factory("LP-Москва")
    second = await account_factory("LP-Химки")
    bot_a = await create_bot(client, tokens, name="Бот А")
    bot_b = await create_bot(client, tokens, name="Бот Б")

    r = await client.put(
        f"/api/v1/bots/{bot_a['id']}/accounts",
        json={"account_ids": [str(first), str(second)]},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert {a["title"] for a in r.json()["accounts"]} == {"LP-Москва", "LP-Химки"}

    # аккаунт может принадлежать только одному боту — молча перепривязываем
    r = await client.put(
        f"/api/v1/bots/{bot_b['id']}/accounts",
        json={"account_ids": [str(second)]},
        headers=auth(tokens),
    )
    assert [a["title"] for a in r.json()["accounts"]] == ["LP-Химки"]

    left = await client.get(f"/api/v1/bots/{bot_a['id']}", headers=auth(tokens))
    assert [a["title"] for a in left.json()["accounts"]] == ["LP-Москва"]

    # пустой список = отвязать все
    r = await client.put(
        f"/api/v1/bots/{bot_a['id']}/accounts", json={"account_ids": []}, headers=auth(tokens)
    )
    assert r.json()["accounts"] == []


async def test_binding_an_unknown_account_is_404(client, tokens):
    bot = await create_bot(client, tokens)
    r = await client.put(
        f"/api/v1/bots/{bot['id']}/accounts",
        json={"account_ids": [str(uuid.uuid4())]},
        headers=auth(tokens),
    )
    assert r.status_code == 404, r.text


# --- удаление (01 §8.7) ------------------------------------------------------


async def test_bound_bot_cannot_be_deleted(client, tokens, account_factory):
    account_id = await account_factory("LP-Москва")
    bot = await create_bot(client, tokens)
    await client.put(
        f"/api/v1/bots/{bot['id']}/accounts",
        json={"account_ids": [str(account_id)]},
        headers=auth(tokens),
    )

    conflict = await client.delete(f"/api/v1/bots/{bot['id']}", headers=auth(tokens))
    assert conflict.status_code == 409, conflict.text
    error = conflict.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"]["reason"] == "bot_in_use"
    assert error["details"]["accounts"][0]["title"] == "LP-Москва"

    # осознанное действие: сначала отвязать, потом удалить
    await client.put(
        f"/api/v1/bots/{bot['id']}/accounts", json={"account_ids": []}, headers=auth(tokens)
    )
    gone = await client.delete(f"/api/v1/bots/{bot['id']}", headers=auth(tokens))
    assert gone.status_code == 204
    assert (await client.get(f"/api/v1/bots/{bot['id']}", headers=auth(tokens))).status_code == 404


# --- песочница по HTTP (01 §8.6) --------------------------------------------


async def test_sandbox_runs_the_draft_without_touching_the_database(
    client, tokens, pg_sessionmaker
):
    start = await client.post(
        "/api/v1/bots/sandbox/start",
        json={
            "scenario": default_scenario(),
            "knowledge_base": KB,
            "client_name": "Иван",
            "item_title": "Ремонт iPhone",
            "now_override": "2026-08-04T22:30:00+03:00",
            "ai_mode": "stub",
        },
        headers=auth(tokens),
    )
    assert start.status_code == 200, start.text
    session_id = start.json()["session_id"]
    assert start.json()["events"] == []  # бот ждёт первого сообщения клиента

    first = await client.post(
        f"/api/v1/bots/sandbox/{session_id}/message",
        json={"text": "Разбил экран айфона"},
        headers=auth(tokens),
    )
    assert first.status_code == 200, first.text
    assert first.json()["trace"] == ["greet", "ask_problem"]

    # ⚠ СТРАЖ ОБНОВЛЁН ПОД ДЕЙСТВУЮЩИЙ СЦЕНАРИЙ (аудит 19.08). Он ждал, что
    # молчание клиента СРАЗУ отдаёт диалог человеку, — так было до правки
    # fe62f93 «молчащий клиент получает пинг через 5 минут». Теперь первый
    # таймаут шлёт живой пинг («Ну что, подскажете?») и снова ждёт, и только
    # ВТОРОЕ молчание уводит к человеку. Тест был красным на main и этого
    # никто не видел: интеграционный набор без Docker молча пропускается,
    # а ship.sh его не гоняет (находка L-011).
    fired = await client.post(
        f"/api/v1/bots/sandbox/{session_id}/fire-timeout", headers=auth(tokens)
    )
    assert fired.status_code == 200, fired.text
    assert fired.json()["trace"] == ["ping_problem", "ask_problem_2"], (
        "первое молчание — пинг, а не передача человеку"
    )

    второй = await client.post(
        f"/api/v1/bots/sandbox/{session_id}/fire-timeout", headers=auth(tokens)
    )
    assert второй.status_code == 200, второй.text
    assert второй.json()["trace"] == ["handoff_no_reply"], (
        "второе молчание обязано отдать диалог человеку"
    )
    assert fired.json()["state"]["status"] == "new"  # автозакрытия по таймауту нет

    closed = await client.delete(f"/api/v1/bots/sandbox/{session_id}", headers=auth(tokens))
    assert closed.status_code == 204
    gone = await client.post(
        f"/api/v1/bots/sandbox/{session_id}/message", json={"text": "ещё"}, headers=auth(tokens)
    )
    assert gone.status_code == 404

    # ни диалога, ни бота, ни журнала — песочница чиста (02 §5.3)
    async with pg_sessionmaker() as db:
        assert (await db.execute(select(Bot))).scalars().all() == []
        assert (await db.execute(select(AuditLog))).scalars().all() == []


async def test_sandbox_rejects_an_invalid_draft(client, tokens):
    scenario = default_scenario()
    scenario["entry"] = "nowhere"
    r = await client.post(
        "/api/v1/bots/sandbox/start",
        json={"scenario": scenario, "knowledge_base": KB},
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "bot_scenario_invalid"
    assert r.json()["error"]["details"]["reason"] == "entry_missing"


# --- режим работы: подсказка или автоответ (миграция 0035) -------------------


async def test_new_bot_is_created_in_suggest_mode(client, tokens, pg_sessionmaker):
    """Умолчание на всём пути «нет поля в запросе → база → ответ» — «подсказка».

    Проверяем именно против Postgres: ограничение `ck_bots_mode` и `server_default`
    живут в базе, и SQLite их «прощает» иначе."""
    bot = await create_bot(client, tokens)
    assert bot["mode"] == "suggest"

    async with pg_sessionmaker() as db:
        assert (await db.execute(select(Bot))).scalar_one().mode == "suggest"

    listed = (await client.get("/api/v1/bots", headers=auth(tokens))).json()
    assert listed["items"][0]["mode"] == "suggest", "режим обязан быть виден в списке"


async def test_switch_to_auto_persists_and_lands_in_audit(client, tokens, pg_sessionmaker):
    """Выпуск бота к клиентам — событие для аудита, а не строка в diff'е шагов."""
    bot = await create_bot(client, tokens)

    r = await client.put(
        f"/api/v1/bots/{bot['id']}",
        json={
            "name": bot["name"],
            "is_enabled": True,
            "scenario": bot["scenario"],
            "knowledge_base": KB,
            "schedule": {"always": True},
            "mode": "auto",
        },
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "auto"

    async with pg_sessionmaker() as db:
        assert (await db.execute(select(Bot))).scalar_one().mode == "auto"
        audit = select(AuditLog).where(AuditLog.action == "bot.updated")
        rows = (await db.execute(audit)).scalars()
        switches = [r.details.get("mode") for r in rows if r.details.get("mode")]
    assert switches == [{"from": "suggest", "to": "auto"}]


async def test_unknown_mode_is_rejected_by_the_api(client, tokens):
    """Опечатка не должна доехать до базы: отказ на входе, а не молчащий бот в бою."""
    r = await client.post(
        "/api/v1/bots",
        json={"name": "Кривой", "knowledge_base": KB, "mode": "подсказка"},
        headers=auth(tokens),
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert [f["field"] for f in body["error"]["details"]["fields"]] == ["mode"]
