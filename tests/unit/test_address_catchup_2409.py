"""Включение автозаписи и автопривязки догоняет накопленное (проверка 24.09).

Автозапись ставилась только после нового вердикта в том же диалоге: включили
её обратно — адреса, подтверждённые картой за время выключения, лежали без
карточки до консольной `address-autofill-backlog`. Строки, судимые при
выключенной автопривязке, обход починки не пересматривал: версия судьи у них
текущая.

ДИВЕРСИИ (обязаны краснеть): убрать постановку догона из ручки — краснеет
«включение автозаписи ставит догон»; убрать пометку из ручки или условие
окна `geo_checked_at >= since` — краснеют тесты автопривязки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.routes import settings as settings_routes
from app.models import ClientAddressCandidate
from app.services import app_settings
from app.services import geocode as g
from app.workers.address_catchup import address_autofill_catchup
from app.workers.main import registered_job_names
from tests.unit import test_autobind_card_1809 as autobind
from tests.unit.test_geo_1809 import ctx

seeded = autobind.seeded

pytestmark = pytest.mark.anyio


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _patch(client: Any, tokens: Any, body: dict[str, Any]) -> dict[str, Any]:
    res = await client.patch(
        "/api/v1/settings/address-detect", json=body, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture
def catchups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    queued: list[str] = []

    async def record(redis: Any) -> bool:
        queued.append("catchup")
        return True

    monkeypatch.setattr(settings_routes, "enqueue_autofill_catchup", record)
    return queued


async def test_switching_autofill_on_queues_the_catchup(
    client: Any, tokens: Any, catchups: list[str]
) -> None:
    await _patch(client, tokens, {"autofill": False})
    assert catchups == []

    await _patch(client, tokens, {"autofill": True})
    assert catchups == ["catchup"]

    # Уже включена — догонять нечего.
    await _patch(client, tokens, {"autofill": True})
    assert catchups == ["catchup"]


async def test_the_catchup_job_queues_autofill_for_a_waiting_address(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        row.geo_status = g.GEO_EXACT
        row.geo_provider = "dadata"
        row.geo_lat, row.geo_lon = 51.2101234, 58.5012345
        row.geo_formatted = "ул Звенигородская, 1, посёлок Заречный, Орск"
        await s.commit()

    assert await address_autofill_catchup(ctx(db_sessionmaker, redis)) == 1
    assert await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")


def test_the_catchup_job_is_registered() -> None:
    assert "address_autofill_catchup" in registered_job_names()


async def _judged(db_sessionmaker: Any, seeded: Any, *, status: str, checked_at: datetime) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        row.geo_status = status
        row.geo_checked_at = checked_at
        row.geo_verdict_version = g.VERDICT_VERSION
        await s.commit()


async def _version(db_sessionmaker: Any, seeded: Any) -> int | None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        return row.geo_verdict_version


async def test_a_refusal_judged_while_auto_decide_was_off_goes_to_rejudge(
    client: Any, tokens: Any, catchups: list[str], seeded: Any, db_sessionmaker: Any
) -> None:
    await _patch(client, tokens, {"auto_decide": False})
    await _judged(db_sessionmaker, seeded, status=g.GEO_HOUSE_MISSING, checked_at=datetime.now(UTC))

    await _patch(client, tokens, {"auto_decide": True})

    assert await _version(db_sessionmaker, seeded) is None


async def test_a_refusal_judged_before_switching_off_keeps_its_verdict(
    client: Any, tokens: Any, catchups: list[str], seeded: Any, db_sessionmaker: Any
) -> None:
    await _judged(
        db_sessionmaker,
        seeded,
        status=g.GEO_HOUSE_MISSING,
        checked_at=datetime.now(UTC) - timedelta(days=1),
    )
    await _patch(client, tokens, {"auto_decide": False})

    await _patch(client, tokens, {"auto_decide": True})

    assert await _version(db_sessionmaker, seeded) == g.VERDICT_VERSION


async def test_a_found_house_is_not_rejudged(
    client: Any, tokens: Any, catchups: list[str], seeded: Any, db_sessionmaker: Any
) -> None:
    await _patch(client, tokens, {"auto_decide": False})
    await _judged(db_sessionmaker, seeded, status=g.GEO_EXACT, checked_at=datetime.now(UTC))

    await _patch(client, tokens, {"auto_decide": True})

    assert await _version(db_sessionmaker, seeded) == g.VERDICT_VERSION


async def test_without_a_recorded_switch_off_the_window_is_the_walk_s_bound(
    client: Any, tokens: Any, catchups: list[str], seeded: Any, db_sessionmaker: Any
) -> None:
    # Выключили мимо экрана — строки журнала о выключении нет.
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    await _judged(
        db_sessionmaker,
        seeded,
        status=g.GEO_HOUSE_MISSING,
        checked_at=datetime.now(UTC) - timedelta(days=3),
    )

    await _patch(client, tokens, {"auto_decide": True})

    assert await _version(db_sessionmaker, seeded) is None
