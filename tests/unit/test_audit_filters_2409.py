"""Фильтры журнала аудита знают все действия и всех сотрудников (проверка 24.09).

Экран собирал «Действие» из своего словаря на 23 пункта: из 72 действий в
журнале 50 выбрать было нельзя, в том числе все `settings.*` — «кто выключил
раздачу» не находилось. «Сотрудник» брался из назначаемых операторов, и
удалённых, руководителя и отключённых в нём не было.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import User
from app.services.audit import AUDIT_ACTIONS

pytestmark = pytest.mark.anyio

URL = "/api/v1/audit-log/filters"


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_every_registered_action_is_offered(client, tokens) -> None:
    body = (await client.get(URL, headers=_hdr(tokens["admin"]))).json()

    offered = {a["action"]: a["label"] for a in body["actions"]}
    assert offered == AUDIT_ACTIONS
    assert any(a.startswith("settings.") for a in offered)


async def test_deleted_and_inactive_people_can_be_picked(
    client, tokens, make_user, db_sessionmaker
) -> None:
    gone = await make_user("gone-af@leadchat.test", full_name="Ушедший Сотрудник")
    await make_user("paused-af@leadchat.test", full_name="Отпускник", is_active=False)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(User).where(User.id == gone.id).values(deleted_at=datetime.now(UTC))
        )
        await s.commit()

    body = (await client.get(URL, headers=_hdr(tokens["admin"]))).json()

    states = {a["full_name"]: a["state"] for a in body["actors"]}
    assert states["Ушедший Сотрудник"] == "deleted"
    assert states["Отпускник"] == "inactive"


async def test_the_filters_are_as_closed_as_the_journal(client, tokens) -> None:
    r = await client.get(URL, headers=_hdr(tokens["manager"]))
    assert r.status_code == 403
