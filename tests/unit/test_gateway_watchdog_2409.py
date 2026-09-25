"""Молчащий шлюз внешних сервисов доходит до людей (проверка 24.09).

Через шлюз в Амстердаме ходят карта, справочники и модель-читатель адресов.
22.09 он молчал 12 минут, и видно это было только строкой журнала скрипта на
хосте. Сторож спрашивает `/status` шлюза сам и зовёт людей после трёх
молчаний подряд: мост между странами моргает, и тревога на каждый чих
приучила бы её не читать.
"""

from __future__ import annotations

import pytest

from app.integrations import gateway
from app.scheduler.jobs import watchdog as wd
from app.services.support import WARNING

pytestmark = pytest.mark.anyio


@pytest.fixture
def gateway_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    state: dict[str, str | None] = {"error": "network"}
    monkeypatch.setattr(gateway, "last_status_error", None)
    monkeypatch.setattr(gateway, "enabled", lambda: True)

    async def refresh(**_kw: object) -> None:
        gateway.last_status_error = state["error"]

    monkeypatch.setattr(gateway, "refresh_status", refresh)
    return state


async def test_three_silent_checks_in_a_row_call_people(redis, gateway_state) -> None:
    assert await wd.check_gateway_unreachable(None, redis) is None  # type: ignore[arg-type]
    assert await wd.check_gateway_unreachable(None, redis) is None  # type: ignore[arg-type]

    draft = await wd.check_gateway_unreachable(None, redis)  # type: ignore[arg-type]

    assert draft is not None
    assert (draft.kind, draft.severity) == ("gateway.down", WARNING)
    assert "15 минут" in draft.body


async def test_one_answer_starts_the_count_again(redis, gateway_state) -> None:
    await wd.check_gateway_unreachable(None, redis)  # type: ignore[arg-type]
    await wd.check_gateway_unreachable(None, redis)  # type: ignore[arg-type]
    gateway_state["error"] = None
    assert await wd.check_gateway_unreachable(None, redis) is None  # type: ignore[arg-type]

    gateway_state["error"] = "network"
    assert await wd.check_gateway_unreachable(None, redis) is None  # type: ignore[arg-type]


def test_the_check_runs_with_the_fast_watchdog() -> None:
    assert "gateway_unreachable" in {c.name for c in wd.FAST_CHECKS}
