"""Журнал аудита: контракт событий 06 §0.3, писатель `write_audit`,
человекочитаемые описания и `GET /audit-log` (01 §9.7, экран 11 §4.2).

Тест полноты (`test_contract_actions_are_emitted_or_pending`) — исполняемая
копия таблицы 06 §0.3: событие либо реально пишется кодом, либо стоит в
`PENDING_ACTIONS` с явной причиной. Молча «забыть» событие нельзя — без него
не считается ни одна событийная метрика статистики.
"""

import ast
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.models import AuditLog, User
from app.services.audit import (
    AUDIT_ACTIONS,
    CONTRACT_ACTIONS,
    MSK,
    PENDING_ACTIONS,
    describe,
    msk_day_range,
    write_audit,
)
from app.services.login_guard import LOGIN_FAIL_LIMIT

APP_DIR = Path(__file__).resolve().parents[2] / "app"
# `action="conversation.assigned"` в вызовах write_audit — литералы, а не
# f-строки: реестр событий должен быть виден грепом, а не только в рантайме.
ACTION_NAME = re.compile(r"^[a-z_]+\.[a-z_]+$")


def test_audit_router_is_mounted_by_the_app_factory(app: FastAPI):
    """Роутер журнала подключает `app/main.py` — шима в тестах больше нет."""
    assert "/api/v1/audit-log" in app.openapi()["paths"]


@pytest.fixture
def add_audit(db_sessionmaker):
    async def _add(
        action: str,
        *,
        user_id: uuid.UUID | None = None,
        entity: str | None = None,
        entity_id: str | None = None,
        details: dict | None = None,
        created_at: datetime | None = None,
    ) -> AuditLog:
        async with db_sessionmaker() as session:
            row = AuditLog(
                user_id=user_id,
                action=action,
                entity=entity,
                entity_id=entity_id,
                details=details,
                created_at=created_at or datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    return _add


def _emitted_actions() -> set[str]:
    """Все действия, которые код РЕАЛЬНО пишет, — по дереву разбора, не грепом.

    ⚠ ПОЧЕМУ НЕ РЕГУЛЯРКА, ХОТЯ БЫЛА ОНА. Прежний `action="…"` видел только
    литерал, стоящий вплотную за знаком равенства. А половина вызовов в этом
    проекте выбирает действие выражением — «первый раз» против «исправили»:

        action="client.phone_captured" if previous is None else "client.phone_edited"

    Второе имя грепу не показывалось никогда. Сторож, который не видит половины
    охраняемого, зеленеет не потому, что всё в порядке (09.09: проверено
    диверсией — имя, выброшенное из реестра, набор пропустил).

    Разбор дерева берёт ВСЕ строки внутри значения `action=`, поэтому тернарник,
    скобки и перенос строки его не обманут. Отбор по виду «слово.слово» оставлен:
    f-строка сюда по-прежнему не годится, потому что реестр обязан читаться
    глазами, а не собираться в рантайме.
    """
    найдено: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        дерево = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(дерево):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "action":
                    continue
                найдено |= {
                    n.value
                    for n in ast.walk(kw.value)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and ACTION_NAME.match(n.value)
                }
    return найдено


# --- реестр и контракт 06 §0.3 -----------------------------------------------


def test_every_action_written_by_code_is_in_the_registry():
    """Событие мимо реестра = пустая колонка «действие» в журнале (11 §4.2)."""
    unknown = _emitted_actions() - set(AUDIT_ACTIONS)
    assert not unknown, f"действия без описания в AUDIT_ACTIONS: {sorted(unknown)}"


def test_contract_actions_are_registered():
    missing = [a for a in CONTRACT_ACTIONS if a not in AUDIT_ACTIONS]
    assert not missing, f"события контракта 06 §0.3 вне реестра: {missing}"


def test_contract_actions_are_emitted_or_pending():
    emitted = _emitted_actions()
    for action in CONTRACT_ACTIONS:
        assert action in emitted or action in PENDING_ACTIONS, (
            f"событие контракта 06 §0.3 «{action}» никто не пишет и оно не объявлено "
            "в PENDING_ACTIONS — метрики статистики, опирающиеся на него, мертвы"
        )


def test_pending_actions_stay_honest():
    """PENDING — только про контракт и только про то, чего ещё нет в коде."""
    emitted = _emitted_actions()
    assert set(PENDING_ACTIONS) <= set(CONTRACT_ACTIONS)
    landed = sorted(set(PENDING_ACTIONS) & emitted)
    assert not landed, f"уже пишется кодом — уберите из PENDING_ACTIONS: {landed}"


@pytest.mark.parametrize(
    "action",
    [
        "conversation.status_changed",
        "conversation.assigned",
        "conversation.reopened",
        "client.phone_captured",
        "auth.login",
        "auth.logout",
        "user.invited",
    ],
)
def test_sprint_scope_contract_actions_are_live(action: str):
    """События, точки вызова которых уже существуют, обязаны быть в коде."""
    assert action in _emitted_actions()


# --- человекочитаемые описания -----------------------------------------------


@pytest.mark.parametrize(
    "action,details,expected",
    [
        ("auth.login", {"ip": "10.0.0.1"}, "Вход в систему"),
        ("auth.login", {"result": "failure", "reason": "bad_password"}, "Неудачная попытка входа"),
        ("auth.logout", None, "Выход из системы"),
        (
            "conversation.status_changed",
            {"from": "new", "to": "in_progress", "by": "operator"},
            "Статус диалога: Новый → В работе (оператор)",
        ),
        (
            "conversation.status_changed",
            {"from": "closed", "to": "new", "by": "system"},
            "Статус диалога: Закрыт → Новый (система)",
        ),
        ("conversation.assigned", {"assignee_id": "x", "by": "self"}, "Диалог взят в работу"),
        (
            "conversation.assigned",
            {"assignee_id": "x", "by": "transfer"},
            "Диалог передан коллеге",
        ),
        ("conversation.assigned", {"assignee_id": None}, "С диалога снят ответственный"),
        ("conversation.reopened", {"client_id": "x"}, "Диалог переоткрыт — клиент вернулся"),
        (
            "client.phone_captured",
            {"source": "regex"},
            "Получен телефон клиента (из текста сообщения)",
        ),
        ("user.invited", {"role": "manager"}, "Приглашён сотрудник (менеджер)"),
        (
            "user.role_changed",
            {"from": "manager", "to": "head"},
            "Изменена роль сотрудника: менеджер → руководитель",
        ),
        ("bot.updated", {"revision": 7}, "Обновлён сценарий бота (ревизия 7)"),
        (
            "bot.handoff",
            {"reason": "client_request"},
            "Бот передал диалог оператору (причина: client_request)",
        ),
        (
            "stats.exported",
            {"format": "csv", "date_from": "2026-07-01", "date_to": "2026-07-31"},
            "Выгрузка статистики (csv, 2026-07-01 — 2026-07-31)",
        ),
    ],
)
def test_describe(action: str, details: dict | None, expected: str):
    assert describe(action, details) == expected


def test_describe_unknown_action_is_shown_as_is():
    assert describe("something.new", {}) == "something.new"


# --- писатель ----------------------------------------------------------------


async def test_write_audit_persists_contract_fields(db, db_sessionmaker):
    actor = uuid.uuid4()
    conv_id = uuid.uuid4()
    await write_audit(
        db,
        user_id=actor,
        action="conversation.status_changed",
        entity="conversation",
        entity_id=str(conv_id),
        details={"from": "new", "to": "closed", "by": "operator", "assignee_id": None},
    )
    await db.commit()

    async with db_sessionmaker() as session:
        row = (await session.execute(AuditLog.__table__.select())).mappings().one()
    assert row["user_id"] == actor
    assert row["action"] == "conversation.status_changed"
    assert row["entity"] == "conversation"
    assert row["entity_id"] == str(conv_id)
    assert row["details"]["to"] == "closed"
    assert row["created_at"] is not None


async def test_write_audit_keeps_the_row_even_for_an_unknown_action(db, db_sessionmaker):
    """Опечатка в action не должна ронять бизнес-операцию (её ловит CI)."""
    await write_audit(db, user_id=None, action="typo.action", entity="user")
    await db.commit()
    async with db_sessionmaker() as session:
        rows = (await session.execute(AuditLog.__table__.select())).mappings().all()
    assert [r["action"] for r in rows] == ["typo.action"]


# --- период по Москве (06 §0.1) ----------------------------------------------


def test_msk_day_range_is_a_half_open_utc_interval():
    ts_from, ts_to = msk_day_range(datetime(2026, 8, 4, tzinfo=MSK).date(), None)
    assert ts_from == datetime(2026, 8, 3, 21, 0, tzinfo=UTC)  # 04.08 00:00 МСК
    assert ts_to is None
    _, ts_to = msk_day_range(None, datetime(2026, 8, 4, tzinfo=MSK).date())
    assert ts_to == datetime(2026, 8, 4, 21, 0, tzinfo=UTC)  # 05.08 00:00 МСК


# --- GET /audit-log ----------------------------------------------------------


async def _get(client, token: str, **params):
    return await client.get(
        "/api/v1/audit-log", headers={"Authorization": f"Bearer {token}"}, params=params
    )


@pytest.mark.parametrize("role,expected", [("admin", 200), ("head", 200)])
async def test_audit_log_is_readable_by_admin_and_head(client, tokens, role, expected):
    r = await _get(client, tokens[role])
    assert r.status_code == expected, r.text
    assert r.json() == {"items": [], "page": {"limit": 50, "offset": 0, "total": 0}}


@pytest.mark.parametrize("role", ["manager", "observer"])
async def test_audit_log_is_forbidden_for_manager_and_observer(client, tokens, role):
    r = await _get(client, tokens[role])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


async def test_audit_log_requires_auth(client):
    r = await client.get("/api/v1/audit-log")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_audit_log_is_sorted_newest_first_and_describes_actions(
    client, tokens, users_by_role, add_audit
):
    manager = users_by_role["manager"]
    base = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    await add_audit("auth.login", user_id=manager.id, details={"ip": "10.0.0.1"}, created_at=base)
    await add_audit(
        "conversation.assigned",
        user_id=manager.id,
        entity="conversation",
        entity_id="c-1",
        details={"assignee_id": str(manager.id), "prev_assignee_id": None, "by": "self"},
        created_at=base + timedelta(minutes=1),
    )
    # системное событие: user_id NULL (реопен пишет воркер, 06 §0.3)
    await add_audit(
        "conversation.reopened",
        entity="conversation",
        entity_id="c-1",
        details={"client_id": "cl-1"},
        created_at=base + timedelta(minutes=2),
    )

    r = await _get(client, tokens["head"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["action"] for i in body["items"]] == [
        "conversation.reopened",
        "conversation.assigned",
        "auth.login",
    ]
    assert body["page"]["total"] == 3
    assert body["items"][0]["user"] is None  # система
    assert body["items"][1]["user"]["full_name"] == manager.full_name
    assert body["items"][1]["description"] == "Диалог взят в работу"
    assert body["items"][2]["description"] == "Вход в систему"
    assert body["items"][0]["created_at"].endswith("Z")


async def test_audit_log_filters(client, tokens, users_by_role, add_audit):
    manager, head = users_by_role["manager"], users_by_role["head"]
    day = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    await add_audit("auth.login", user_id=manager.id, entity="user", created_at=day)
    await add_audit("auth.logout", user_id=manager.id, entity="user", created_at=day)
    await add_audit(
        "conversation.assigned",
        user_id=head.id,
        entity="conversation",
        entity_id="c-9",
        details={"assignee_id": str(manager.id), "by": "transfer"},
        created_at=day,
    )

    by_user = (await _get(client, tokens["admin"], user_id=str(manager.id))).json()
    assert by_user["page"]["total"] == 2
    assert {i["action"] for i in by_user["items"]} == {"auth.login", "auth.logout"}

    by_action = (await _get(client, tokens["admin"], action="auth.logout")).json()
    assert [i["action"] for i in by_action["items"]] == ["auth.logout"]

    by_entity = (await _get(client, tokens["admin"], entity="conversation")).json()
    assert [i["entity_id"] for i in by_entity["items"]] == ["c-9"]


async def test_audit_log_period_is_in_moscow_dates(client, tokens, add_audit):
    """`date_from`/`date_to` — даты по Москве, обе включительно (06 §0.1)."""
    # 03.08 22:00 UTC = 04.08 01:00 МСК — событие принадлежит 4 августа
    await add_audit("auth.login", created_at=datetime(2026, 8, 3, 22, 0, tzinfo=UTC))
    # 04.08 22:00 UTC = 05.08 01:00 МСК — уже 5 августа
    await add_audit("auth.logout", created_at=datetime(2026, 8, 4, 22, 0, tzinfo=UTC))

    fourth = (
        await _get(client, tokens["admin"], date_from="2026-08-04", date_to="2026-08-04")
    ).json()
    assert [i["action"] for i in fourth["items"]] == ["auth.login"]

    both = (
        await _get(client, tokens["admin"], date_from="2026-08-04", date_to="2026-08-05")
    ).json()
    assert both["page"]["total"] == 2

    r = await _get(client, tokens["admin"], date_from="2026-08-05", date_to="2026-08-04")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_audit_log_pagination(client, tokens, add_audit):
    base = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    for i in range(5):
        await add_audit("auth.login", entity_id=str(i), created_at=base + timedelta(minutes=i))

    page = (await _get(client, tokens["admin"], limit=2, offset=0)).json()
    assert [i["entity_id"] for i in page["items"]] == ["4", "3"]
    assert page["page"] == {"limit": 2, "offset": 0, "total": 5}

    page2 = (await _get(client, tokens["admin"], limit=2, offset=2)).json()
    assert [i["entity_id"] for i in page2["items"]] == ["2", "1"]

    assert (await _get(client, tokens["admin"], limit=0)).status_code == 400
    assert (await _get(client, tokens["admin"], limit=201)).status_code == 400


async def test_audit_log_survives_a_deleted_actor(client, tokens, add_audit):
    """`audit_log.user_id` без FK (DESIGN §4.4) — автора может уже не быть."""
    await add_audit("auth.login", user_id=uuid.uuid4())
    body = (await _get(client, tokens["admin"])).json()
    assert body["items"][0]["user"] is None


async def test_login_lands_in_the_journal(client, tokens, users_by_role):
    """Сквозная проверка писателя: вход → строка в журнале (01 §2.1)."""
    from tests.unit.conftest import DEFAULT_PASSWORD

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": users_by_role["manager"].email, "password": DEFAULT_PASSWORD},
    )
    assert r.status_code == 200, r.text

    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    assert body["page"]["total"] == 1
    assert body["items"][0]["user"]["id"] == str(users_by_role["manager"].id)
    assert body["items"][0]["description"] == "Вход в систему"


async def test_failed_login_lands_in_the_journal(client, tokens, users_by_role):
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": users_by_role["manager"].email, "password": "wrong-password-here"},
    )
    assert r.status_code == 401

    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    assert body["page"]["total"] == 1
    item = body["items"][0]
    assert item["details"] == {
        "ip": item["details"]["ip"],
        "result": "failure",
        "reason": "bad_password",
    }
    assert item["user"]["id"] == str(users_by_role["manager"].id)
    assert item["entity"] == "user"
    assert item["description"] == "Неудачная попытка входа"


async def test_failed_login_for_an_unknown_email_is_a_system_row(client, tokens):
    """Пользователя нет — строка всё равно нужна: она про попытку (06 §0.3)."""
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever"},
    )
    assert r.status_code == 401

    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    assert body["page"]["total"] == 1
    item = body["items"][0]
    assert item["user"] is None and item["entity_id"] is None
    assert item["details"]["reason"] == "no_user"


async def test_successful_login_is_marked_as_success(client, tokens, users_by_role):
    from tests.unit.conftest import DEFAULT_PASSWORD

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": users_by_role["manager"].email, "password": DEFAULT_PASSWORD},
    )
    assert r.status_code == 200, r.text
    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    assert body["items"][0]["details"]["result"] == "success"


async def test_inactive_user_login_is_journaled(client, tokens, users_by_role, db_sessionmaker):
    from tests.unit.conftest import DEFAULT_PASSWORD

    manager = users_by_role["manager"]
    async with db_sessionmaker() as session:
        user = await session.get(User, manager.id)
        assert user is not None
        user.is_active = False
        await session.commit()

    r = await client.post(
        "/api/v1/auth/login", json={"email": manager.email, "password": DEFAULT_PASSWORD}
    )
    assert r.status_code == 403

    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    assert body["page"]["total"] == 1
    assert body["items"][0]["details"]["reason"] == "inactive"


async def test_locked_login_is_journaled_once_per_lock_window(client, tokens, users_by_role):
    """Лок не должен превращать неаутентифицированную ручку в способ
    наливать строки в audit_log: одна запись на окно блокировки."""
    email = users_by_role["manager"].email
    for _ in range(LOGIN_FAIL_LIMIT):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "nope"})
        assert r.status_code == 401

    for _ in range(3):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "nope"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "account_locked"

    body = (await _get(client, tokens["admin"], action="auth.login")).json()
    reasons = [i["details"]["reason"] for i in body["items"]]
    assert reasons.count("locked") == 1
    assert reasons.count("bad_password") == LOGIN_FAIL_LIMIT
