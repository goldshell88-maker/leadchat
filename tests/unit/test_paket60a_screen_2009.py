"""Пакет 6.0а, экран (20.09, исполнитель D): «Адрес неверный» и след правила.

Программа «автоматически и точно» §1.1 I-9: под записанным автоадресом экран
показывает подпись правила (`RULE_LABEL[trace.rule]`) и варианты источника, а
кнопка «Адрес неверный» одним нажатием делает то же, что «изменить → стереть»:
строка-источник → `rejected` + `resolved_by_id`, карточка → пусто, в журнале
`client.address_edited source=manual reason=wrong candidate_id=…` — по этой
причине воронка (I-5) считает `rejected_by_hand` по правилу.

Здесь стережётся:
* ручка `POST /clients/{id}/address/wrong` — карточка, строка, журнал, кадр
  `client:updated reason=address_wrong` после commit'а;
* граница «только автоматика»: набранный руками и принятый человеком (`replace`)
  адрес ручка не трогает — 409, карточка и строка целы;
* простое стирание через PUT пустой строкой причины НЕ пишет: правка остаётся
  правкой, иначе воронка сосчитала бы каждое «клиент передумал» отказом от
  правила;
* личность (`/identity`) отдаёт у источника карточки `rule`, `rule_label`,
  `suggest`, `variants` — по контракту ядра (contract_60a.md §6).

ДИВЕРСИИ (каждая обязана краснеть; прогнаны 20.09 «правка → тест → откат»):
снять проверку `resolved_by_id` в `reject_auto_address` — принятый человеком
адрес стирается (`test_принятый_человеком_адрес_ручка_не_трогает`); снять
`client.address is None`/`источник is None` — пустая карточка отвечает 200
(`test_пустая_карточка_409`); писать `reason` всегда в `set_address` —
`test_обычное_стирание_причины_не_пишет`; не класть `candidate_id` в детали —
`test_адрес_неверный_снимает_автоадрес_и_пишет_причину`; слать кадр до commit'а
или не слать — тот же тест (кадр с причиной `address_wrong`).

Адреса — стенд 18.09 (Орск, Заречный); имён и телефонов клиентов нет.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientAddressCandidate
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_REJECTED
from app.services import clients as clients_svc
from app.services import geocode as g
from tests.unit import test_autobind_card_1809 as ав
from tests.unit.conftest import drain_events

# Фикстуры соседнего стенда — присваиванием, не импортом имени (ruff F811).
seeded = ав.seeded
ФОРМАТ = ав.ФОРМАТ

pytestmark = pytest.mark.anyio


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def url_неверный(client_id: uuid.UUID) -> str:
    return f"/api/v1/clients/{client_id}/address/wrong"


def url_адрес(client_id: uuid.UUID) -> str:
    return f"/api/v1/clients/{client_id}/address"


async def _автоадрес(
    db_sessionmaker: Any,
    redis: Any,
    seed: Any,
    *,
    trace: dict[str, Any] | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> None:
    """Точка дома у строки + автозапись боевым путём (воркер), след — руками:
    в бою его пишет суд воркера, здесь важен только его вид на экране."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seed.candidate_id)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata"
        row.geo_lat, row.geo_lon, row.geo_formatted = 51.2101234, 58.5012345, ФОРМАТ
        row.trace = trace
        row.geo_variants = variants
        await s.commit()
    assert await ав._авто(db_sessionmaker, redis, seed) == "filled"
    card = await ав._card(db_sessionmaker, seed.client_id)
    assert card.address_candidate_id == seed.candidate_id and card.address_set_at is None


async def _журнал(db_sessionmaker: Any, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


async def _личность(client: Any, tokens: Any, client_id: uuid.UUID) -> dict[str, Any]:
    res = await client.get(f"/api/v1/clients/{client_id}/identity", headers=hdr(tokens["manager"]))
    assert res.status_code == 200, res.text
    return res.json()


# ── ручка «Адрес неверный» ────────────────────────────────────────────────────


async def test_адрес_неверный_снимает_автоадрес_и_пишет_причину(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    await _автоадрес(
        db_sessionmaker,
        redis,
        seeded,
        trace={"rule": g.RULE_STREET_POINT, "policy": g.POLICY_APPROX, "km": 0.4},
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"address": None, "changed": True}

    # Карточка пуста и не помнит происхождения.
    card = await ав._card(db_sessionmaker, seeded.client_id)
    assert card.address is None and card.address_candidate_id is None
    assert card.address_set_at is None and card.address_set_by_id is None
    # Строка-источник отказана ЧЕЛОВЕКОМ: место закрыто для автоматики.
    row = await ав._row(db_sessionmaker, seeded.candidate_id)
    assert row.status == CANDIDATE_REJECTED and row.resolved_by_id is not None
    # След суда строки не тронут — по нему воронка узнаёт правило.
    assert row.trace["rule"] == g.RULE_STREET_POINT
    # Журнал: правка руками с причиной и строкой-источником.
    правки = await _журнал(db_sessionmaker, "client.address_edited")
    assert len(правки) == 1
    assert правки[0].user_id is not None
    assert правки[0].details["source"] == "manual"
    assert правки[0].details["reason"] == clients_svc.ADDRESS_WRONG_REASON == "wrong"
    assert правки[0].details["candidate_id"] == str(seeded.candidate_id)
    assert правки[0].details["previous"] == f"{ФОРМАТ}, кв 3"
    assert правки[0].details["address"] is None
    # Кадр после commit'а — с причиной, без адреса.
    кадры = [e for e in await drain_events(pubsub) if e.get("type") == "client:updated"]
    assert [e["data"]["reason"] for e in кадры] == ["address_wrong"]
    assert "address" not in кадры[0]["data"]
    # Личность: пусто, источника нет.
    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address"] is None and личность["address_source"] == "none"
    assert личность["address_geo"] is None


async def test_повторное_нажатие_409(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """Второй клик (или коллега с устаревшим экраном) — 409, а не второе
    стирание с второй строкой журнала."""
    await _автоадрес(db_sessionmaker, redis, seeded)
    first = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "address_not_auto"
    assert len(await _журнал(db_sessionmaker, "client.address_edited")) == 1


async def test_набранный_руками_адрес_ручка_не_трогает(
    seeded: Any, db_sessionmaker: Any, tokens: Any, client: Any
) -> None:
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": "Ленина 5", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "address_not_auto"
    card = await ав._card(db_sessionmaker, seeded.client_id)
    assert card.address == "Ленина 5" and card.address_set_at is not None
    assert await _журнал(db_sessionmaker, "client.address_edited") == []


async def test_принятый_человеком_адрес_ручка_не_трогает(
    seeded: Any, db_sessionmaker: Any, tokens: Any, client: Any, make_user: Any
) -> None:
    """Источник принят кнопкой (`replace`, `resolved_by_id` есть): спор двух
    людей нажатием не решается — 409, строка остаётся принятой."""
    actor = await make_user("p60a-screen@test.local", role="admin")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata"
        row.geo_lat, row.geo_lon, row.geo_formatted = 51.2101234, 58.5012345, ФОРМАТ
        card = await s.get(Client, row.client_id)
        await clients_svc.resolve_address_candidate(
            s, candidate=row, client=card, decision="replace", actor=actor
        )
        await s.commit()
    assert (await _личность(client, tokens, seeded.client_id))["address_source"] == "dialog"

    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 409, res.text
    row = await ав._row(db_sessionmaker, seeded.candidate_id)
    assert row.status == CANDIDATE_ACCEPTED and row.resolved_by_id == actor.id
    card = await ав._card(db_sessionmaker, seeded.client_id)
    assert card.address is not None and card.address_candidate_id == seeded.candidate_id


async def test_пустая_карточка_409(seeded: Any, tokens: Any, client: Any) -> None:
    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "address_not_auto"


async def test_чужой_диалог_в_теле_422(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    await _автоадрес(db_sessionmaker, redis, seeded)
    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(uuid.uuid4())},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "conversation_mismatch"
    assert (await ав._card(db_sessionmaker, seeded.client_id)).address is not None


async def test_без_права_403(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """Право то же, что у «изменить» (`conversations:manage`): наблюдатель
    адрес не стирает."""
    await _автоадрес(db_sessionmaker, redis, seeded)
    res = await client.post(
        url_неверный(seeded.client_id),
        json={"conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["observer"]),
    )
    assert res.status_code == 403, res.text
    assert (await ав._card(db_sessionmaker, seeded.client_id)).address is not None


async def test_обычное_стирание_причины_не_пишет(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """«Изменить» → пустая строка: строка-источник отказывается так же, но
    в журнале это правка без причины — иначе каждое «клиент передумал»
    считалось бы отказом от правила."""
    await _автоадрес(db_sessionmaker, redis, seeded, trace={"rule": g.RULE_STREET_POINT})
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    row = await ав._row(db_sessionmaker, seeded.candidate_id)
    assert row.status == CANDIDATE_REJECTED and row.resolved_by_id is not None
    правки = await _журнал(db_sessionmaker, "client.address_edited")
    assert len(правки) == 1
    assert "reason" not in правки[0].details and "candidate_id" not in правки[0].details


# ── личность: след правила у источника карточки ───────────────────────────────


async def test_identity_источник_отдаёт_правило_подпись_и_варианты(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    варианты = [
        {"formatted": "Звенигородская улица, 1, Орск", "lat": 51.23, "lon": 58.47, "city": "Орск"},
        {"formatted": ФОРМАТ, "lat": 51.2101234, "lon": 58.5012345, "city": "Орск"},
    ]
    await _автоадрес(
        db_sessionmaker,
        redis,
        seeded,
        trace={"rule": g.RULE_ONLY_IN_RADIUS, "policy": g.POLICY_EXACT, "km": 12.0},
        variants=варианты,
    )
    гео = (await _личность(client, tokens, seeded.client_id))["address_geo"]
    assert гео["rule"] == g.RULE_ONLY_IN_RADIUS
    assert гео["rule_label"] == g.RULE_LABEL[g.RULE_ONLY_IN_RADIUS]
    assert гео["suggest"] is False
    assert [v["formatted"] for v in гео["variants"]] == [v["formatted"] for v in варианты]
    assert гео["precision"] == g.PRECISION_EXACT


async def test_identity_без_следа_правила_нет(
    seeded: Any, db_sessionmaker: Any, redis: Any, tokens: Any, client: Any
) -> None:
    """Вердикт самой карты: правила нет, подписи нет, вариантов нет — экран
    не рисует пустую ось."""
    await _автоадрес(db_sessionmaker, redis, seeded)
    гео = (await _личность(client, tokens, seeded.client_id))["address_geo"]
    assert (гео["rule"], гео["rule_label"], гео["suggest"], гео["variants"]) == (
        None,
        None,
        False,
        [],
    )
