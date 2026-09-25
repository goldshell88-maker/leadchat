"""Понижение правила действует и на решения, вынесенные им раньше (проверка 24.09).

Лестница `rule_policy_weekly` понижает правило до suggest или shadow позже,
чем оно решило строку. Решение ждёт (карточка была занята), и после «Адрес
неверный» или `address-unfill` автозапись клала его в пустую карточку: степень
смотрела только на хвост провайдера дня суда. Теперь автозапись применяет
сегодняшнюю политику к правилу из следа (`trace.rule`).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models import ClientAddressCandidate
from app.services import app_settings
from app.services import geocode as g
from tests.unit import test_autobind_card_1809 as autobind

seeded = autobind.seeded

pytestmark = pytest.mark.anyio


async def _decided_by_suburb(db_sessionmaker: Any, seeded: Any, policy: str) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        # Как оставил бы воркер ДО понижения: пригород решил строку.
        row.geo_status = g.GEO_EXACT
        row.geo_provider = "dadata~approx"
        row.geo_lat, row.geo_lon = 51.2101234, 58.5012345
        row.geo_formatted = "ул Звенигородская, 1, посёлок Заречный, Орск"
        row.trace = {"rule": g.RULE_SUBURB, "policy": g.POLICY_EXACT}
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_RULE_POLICY: policy}, user_id=None)
        await s.commit()


@pytest.mark.parametrize("policy", ["suburb=suggest", "suburb=shadow", "suburb=off"])
async def test_a_demoted_rule_no_longer_writes_its_old_decision(
    policy: str, seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    await _decided_by_suburb(db_sessionmaker, seeded, policy)

    outcome = await autobind._авто(db_sessionmaker, redis, seeded)

    assert outcome == "skip"
    assert (await autobind._card(db_sessionmaker, seeded.client_id)).address is None


async def test_the_rule_at_its_usual_policy_still_writes(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    await _decided_by_suburb(db_sessionmaker, seeded, "suburb=approx")

    assert await autobind._авто(db_sessionmaker, redis, seeded) == "filled"


def test_a_decision_without_a_rule_is_the_map_s_own() -> None:
    assert g.rule_writes_card({"suburb": g.POLICY_OFF}, None) is True
    assert g.rule_writes_card({}, g.RULE_SUBURB) is (
        g.rule_policy({}, g.RULE_SUBURB) in (g.POLICY_APPROX, g.POLICY_EXACT)
    )
