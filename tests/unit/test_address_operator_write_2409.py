"""Кнопка «Записать в карточку» там, где адрес иначе не запишет никто (24.09).

Выключенная автозапись обещала оператору «подтвердить или не адрес», а у
строки была только «Не адрес». Так же ждёт человека решение правила, которое
лестница понизила до `suggest` уже после суда: автозапись его не берёт.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import Client, ClientAddressCandidate
from app.services import app_settings, clients
from app.services import geocode as g
from tests.unit import test_autobind_card_1809 as autobind

seeded = autobind.seeded


def _row(**geo: object) -> ClientAddressCandidate:
    return ClientAddressCandidate(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        kind="house",
        street="ул Мира",
        house="7",
        value="ул Мира, 7",
        raw="Мира 7",
        level="A",
        status="pending",
        detected_at=datetime.now(UTC),
        **geo,
    )


HOUSE = {
    "geo_status": g.GEO_EXACT,
    "geo_provider": "dadata",
    "geo_lat": 51.23,
    "geo_lon": 58.47,
    "geo_formatted": "улица Мира, 7, Орск",
}
TEXT_ONLY = {
    "geo_status": g.GEO_HOUSE_MISSING,
    "geo_provider": "dadata",
    "geo_formatted": "улица Мира, 7, Орск",
}
BY_SUBURB = {
    **HOUSE,
    "geo_provider": "dadata~approx",
    "trace": {"rule": g.RULE_SUBURB, "policy": g.POLICY_EXACT},
}
FRESH_SUGGESTION = {**BY_SUBURB, "geo_provider": "dadata~approx~suggest"}


@pytest.mark.parametrize(
    ("geo", "autofill", "policy", "writable"),
    [
        (HOUSE, False, {}, True),
        (TEXT_ONLY, False, {}, True),
        (HOUSE, True, {}, False),
        # Строку без точки при включённой автозаписи пишет автоматика — у ворот
        # в карточку своего выключателя автопривязки нет.
        (TEXT_ONLY, True, {}, False),
        # Правило понижено до `suggest` после суда: автозапись строку не берёт.
        (BY_SUBURB, True, {g.RULE_SUBURB: g.POLICY_SUGGEST}, True),
        (BY_SUBURB, True, {g.RULE_SUBURB: g.POLICY_APPROX}, False),
        # Тень и выключенное правило — решения нет вовсе, принимать нечего.
        (BY_SUBURB, True, {g.RULE_SUBURB: g.POLICY_SHADOW}, False),
        # Свежее предложение степени не имеет — его кнопка по `geo.suggest`.
        (FRESH_SUGGESTION, True, {}, False),
        ({"geo_status": g.GEO_PENDING}, False, {}, False),
    ],
)
def test_the_operator_writes_what_automation_will_not(
    geo: dict[str, object], autofill: bool, policy: dict[str, str], writable: bool
) -> None:
    row = _row(**geo)
    assert clients._operator_writes(row, autofill=autofill, policy=policy) is writable


async def _house_found(db_sessionmaker: Any, seeded: Any, *, autofill: bool) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        row.geo_status = g.GEO_EXACT
        row.geo_provider = "dadata"
        row.geo_lat, row.geo_lon = 51.2101234, 58.5012345
        row.geo_formatted = "ул Звенигородская, 1, посёлок Заречный, Орск"
        await app_settings.set_many(
            s, {app_settings.ADDRESS_DETECT_AUTOFILL: autofill}, user_id=None
        )
        await s.commit()


async def _writable(db_sessionmaker: Any, seeded: Any) -> list[bool]:
    async with db_sessionmaker() as s:
        view = await clients.identity_view(s, await s.get(Client, seeded.client_id))
    return [c["writable"] for c in view["address_candidates"]]


@pytest.mark.anyio
async def test_with_autofill_off_the_card_offers_the_write_button(
    seeded: Any, db_sessionmaker: Any
) -> None:
    await _house_found(db_sessionmaker, seeded, autofill=False)

    assert await _writable(db_sessionmaker, seeded) == [True]


@pytest.mark.anyio
async def test_with_autofill_on_the_card_leaves_writing_to_automation(
    seeded: Any, db_sessionmaker: Any
) -> None:
    await _house_found(db_sessionmaker, seeded, autofill=True)

    assert await _writable(db_sessionmaker, seeded) == [False]


@pytest.mark.anyio
async def test_a_rule_demoted_to_suggest_after_judging_offers_the_button(
    seeded: Any, db_sessionmaker: Any, redis: Any
) -> None:
    await _house_found(db_sessionmaker, seeded, autofill=True)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        row.geo_provider = "dadata~approx"
        row.trace = {"rule": g.RULE_SUBURB, "policy": g.POLICY_EXACT}
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_RULE_POLICY: "suburb=suggest"}, user_id=None
        )
        await s.commit()

    # Автозапись не берёт, но и оператор не остаётся без кнопки.
    assert await autobind._авто(db_sessionmaker, redis, seeded) == "skip"
    assert await _writable(db_sessionmaker, seeded) == [True]
