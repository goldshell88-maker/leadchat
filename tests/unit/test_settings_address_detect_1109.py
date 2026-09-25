"""Ручка настроек адреса: пять переключателей на одном экране и след в журнале.

До 11.09 ключей `address_detect.*` нельзя было достичь ни из API, ни из
панели — выключатель автозаписи существовал только как SQL. Раз автоматика
пишет в карточку сама, снять её обязано быть одним нажатием руководителя.
"""

import pytest
import sqlalchemy as sa

from app.models import AuditLog
from app.services import address_parse, app_settings, geocode

pytestmark = pytest.mark.anyio


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_умолчания_читаются(client, tokens, monkeypatch):
    # Ключи провайдеров — на шлюзе (docs/46); экрану «есть ли ключ» приходит
    # из снимка `/status`. Здесь снимок явный: ни у кого ключа нет.
    from app.integrations import gateway

    monkeypatch.setattr(
        gateway,
        "known_keys",
        {
            "dadata": False,
            "yandex_geocoder": False,
            "yandex_suggest": False,
            "openrouter": False,
            "groq": False,
            "mistral": False,
        },
    )
    res = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert res.status_code == 200, res.text
    assert res.json() == {
        "enabled": True,
        "autofill": True,
        "levels": "AB",
        "geo_enabled": True,
        "provider": "nominatim",
        # Шлюз в тестах настроен (conftest), ключей у него нет — снимок выше.
        "gateway_configured": True,
        "yandex_key_present": False,
        "yandex_daily_limit": 900,
        "yandex_used_today": 0,
        # Доля починки — только на чтение (пакет 5, 20.09); правится из консоли.
        "repair_share_yandex": 30,
        "suggest_enabled": True,
        "suggest_key_present": False,
        "suggest_daily_limit": 900,
        "suggest_used_today": 0,
        "dadata_enabled": True,
        "dadata_key_present": False,
        "dadata_daily_limit": 9000,
        "dadata_used_today": 0,
        "llm_enabled": True,
        "llm_key_present": False,
        "llm_daily_limit": 50,
        "llm_used_today": 0,
        "ahunter_enabled": True,
        "ahunter_used_today": 0,
        "speller_enabled": True,
        "speller_daily_limit": 9000,
        "speller_used_today": 0,
        # Автопривязка (18.09): включена решением владельца; вопрос клиенту —
        # выключен до сухого прогона, включает владелец тумблером.
        "auto_decide": True,
        # Политики правил (пакет 6.0а): пусто — одни умолчания реестров, и
        # действующая лестница по правилам отдаётся явно, с подписями.
        "rule_policy": "",
        "rule_policy_effective": [
            {"rule": rule, "label": geocode.RULE_LABEL[rule], "policy": policy}
            for rule, policy in geocode.RULE_DEFAULT_POLICY.items()
        ],
        "parse_rules": "",
        # Правила разбора (проверка 24.09) — с подписями и состоянием, все
        # выключены по умолчанию реестра.
        "parse_rules_effective": [
            {"rule": rule, "label": address_parse.PARSE_RULE_LABEL[rule], "state": state}
            for rule, state in address_parse.PARSE_RULES.items()
        ],
        # Свои адреса (пакет 7а, Q24): список владельца пуст, вывод задачи пуст.
        "own_addresses": "",
        "own_addresses_auto": "",
        "ask_enabled": False,
        "ask_delay_sec": 600,
        "ask_text": app_settings.ADDRESS_ASK_DEFAULT_TEXT,
        "ask_min_chars": 25,
    }


async def test_ключи_видны_по_снимку_шлюза(client, tokens, monkeypatch):
    from app.core.config import settings
    from app.integrations import gateway

    monkeypatch.setattr(
        gateway, "known_keys", {"dadata": True, "yandex_geocoder": False, "groq": True}
    )
    res = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert res.json()["dadata_key_present"] is True
    assert res.json()["yandex_key_present"] is False
    assert res.json()["llm_key_present"] is True  # хоть один читатель с ключом
    # Шлюз не настроен — ключей нет, что бы ни говорил старый снимок.
    monkeypatch.setattr(settings, "gateway_url", "")
    res = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert res.json()["dadata_key_present"] is False
    assert res.json()["llm_key_present"] is False
    # И экран знает почему: чинится адрес шлюза, а не ключ у него.
    assert res.json()["gateway_configured"] is False


async def test_dadata_выключается_одним_нажатием(client, tokens):
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"dadata_enabled": False},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["dadata_enabled"] is False
    again = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert again.json()["dadata_enabled"] is False


async def test_правка_пишется_и_оставляет_след(client, tokens, db_sessionmaker):
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"autofill": False, "levels": "BA", "provider": "yandex"},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["autofill"] is False
    assert res.json()["levels"] == "AB"
    assert res.json()["provider"] == "yandex"
    again = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert again.json()["autofill"] is False
    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(строки) == 1
    assert строки[0].details["before"]["autofill"] is True
    assert строки[0].details["after"]["autofill"] is False


async def test_негодный_уровень_и_провайдер_отвергаются(client, tokens):
    res = await client.patch(
        "/api/v1/settings/address-detect", json={"levels": "AX"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code in (400, 422), res.text
    res = await client.patch(
        "/api/v1/settings/address-detect", json={"provider": "google"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code in (400, 422), res.text


async def test_пустое_тело_ничего_не_меняет_и_не_пишет_журнал(client, tokens, db_sessionmaker):
    res = await client.patch(
        "/api/v1/settings/address-detect", json={}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200
    async with db_sessionmaker() as s:
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "settings.address_detect_changed")
            )
        ).scalar_one()
    assert n == 0


async def test_смена_провайдера_снимает_бан_со_строк(
    client, tokens, db_sessionmaker, seed_conversation
):
    """Бан — свойство провайдера, а не адреса: сменили карту — строки снова в очереди."""
    from datetime import UTC, datetime

    from app.models import Client, ClientAddressCandidate
    from app.services import address_parse
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seed_conversation.conversation_id,
            message_id=None,
            message_at=None,
            found=address_parse.parse("ул Ленина 5"),
            now=datetime.now(UTC),
        )
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
        row.geo_status = "blocked"
        row.geo_attempts = 3
        await s.commit()
    res = await client.patch(
        "/api/v1/settings/address-detect", json={"provider": "yandex"}, headers=hdr(tokens["admin"])
    )
    assert res.status_code == 200, res.text
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, записано.candidate_id)
    assert (row.geo_status, row.geo_attempts) == ("pending", 0)


async def test_режим_osm_then_yandex_и_потолок_яндекса(client, tokens, redis):
    """Владелец: «тарифы Яндекса дороги — OSM первым, Яндекс вторым». Потолок
    держит бесплатную тысячу, расход за сегодня виден рядом с выключателем."""
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"provider": "osm_then_yandex", "yandex_daily_limit": 500},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["provider"] == "osm_then_yandex"
    assert res.json()["yandex_daily_limit"] == 500
    # Расход читается из того же счётчика, что ведёт задача.
    from app.workers.geocode import yandex_calls_key

    await redis.set(yandex_calls_key(), 42)
    again = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert again.json()["yandex_used_today"] == 42
    # Снять потолок — только явно, для платного тарифа.
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"unlimited_yandex": True},
        headers=hdr(tokens["admin"]),
    )
    assert res.json()["yandex_daily_limit"] is None


# ── политика правил: вето по факту сохранения и нестрогое чтение (ревью 20.09) ──


async def _строки_журнала(db_sessionmaker) -> list:  # noqa: ANN001
    async with db_sessionmaker() as s:
        return (
            (
                await s.execute(
                    sa.select(AuditLog)
                    .where(AuditLog.action == "settings.address_detect_changed")
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


async def test_rule_policy_повторное_сохранение_пишет_журнал_с_пометкой(
    client, tokens, db_sessionmaker
):
    """Ревью #6: вето на объявленный подъём — сохранить настройку со строкой
    «rule=approx», а значение там уже стоит. Ручка обязана оставить след и при
    `after == before`, с `submitted=["rule_policy"]`; тумблер без строки
    политики помечается своим именем, а без изменений — не пишется вовсе.
    ДИВЕРСИЯ: вернуть `if after != before:` — второй PATCH не оставит строки."""
    url = "/api/v1/settings/address-detect"
    for _ in range(2):
        res = await client.patch(
            url, json={"rule_policy": "street_point=approx"}, headers=hdr(tokens["admin"])
        )
        assert res.status_code == 200, res.text
        assert res.json()["rule_policy"] == "street_point=approx"
    строки = await _строки_журнала(db_sessionmaker)
    assert len(строки) == 2
    assert [s.details["submitted"] for s in строки] == [["rule_policy"], ["rule_policy"]]
    assert строки[1].details["before"] == строки[1].details["after"], "значение не менялось"
    assert строки[1].user_id is not None
    # Тумблер: строка есть, но `rule_policy` в `submitted` нет.
    res = await client.patch(url, json={"autofill": False}, headers=hdr(tokens["admin"]))
    assert res.status_code == 200, res.text
    строки = await _строки_журнала(db_sessionmaker)
    assert len(строки) == 3 and строки[2].details["submitted"] == ["autofill"]
    # Тот же тумблер тем же значением — без строки: журнал не растёт впустую.
    await client.patch(url, json={"autofill": False}, headers=hdr(tokens["admin"]))
    assert len(await _строки_журнала(db_sessionmaker)) == 3


async def test_устаревшая_пара_в_таблице_не_сбрасывает_соседей(client, tokens, db_sessionmaker):
    """Ревью #5/#14: в таблице «street_point=shadow,house_famly=suggest» (имя
    снято из реестра после записи). Читатель отдаёт строку как есть, экран
    показывает `street_point=shadow` — то же, что видит воркер; на записи та же
    строка — по-прежнему 400. Мусор не того типа — умолчание с предупреждением,
    а не молча."""
    import structlog

    from app.models import AppSetting

    текст = "street_point=shadow,house_famly=suggest"
    async with db_sessionmaker() as s:
        s.add(AppSetting(key=app_settings.ADDRESS_GEO_RULE_POLICY, value=текст))
        await s.commit()
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY) == текст
        assert (await app_settings.get_all(s))[app_settings.ADDRESS_GEO_RULE_POLICY] == текст
    res = await client.get("/api/v1/settings/address-detect", headers=hdr(tokens["admin"]))
    assert res.status_code == 200, res.text
    assert res.json()["rule_policy"] == текст
    действующая = {r["rule"]: r["policy"] for r in res.json()["rule_policy_effective"]}
    assert действующая["street_point"] == "shadow"
    assert действующая["house_family"] == geocode.RULE_DEFAULT_POLICY["house_family"]
    плохо = await client.patch(
        "/api/v1/settings/address-detect", json={"rule_policy": текст}, headers=hdr(tokens["admin"])
    )
    assert плохо.status_code == 400, плохо.text
    # Не строка в текстовой настройке — умолчание, и об этом есть запись.
    async with db_sessionmaker() as s:
        row = await s.get(AppSetting, app_settings.ADDRESS_GEO_RULE_POLICY)
        row.value = 123
        await s.commit()
    async with db_sessionmaker() as s:
        with structlog.testing.capture_logs() as логи:
            assert await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY) == ""
            assert (await app_settings.get_all(s))[app_settings.ADDRESS_GEO_RULE_POLICY] == ""
    assert [(л["event"], л["key"]) for л in логи] == [
        ("app_settings.stored_invalid", app_settings.ADDRESS_GEO_RULE_POLICY)
    ] * 2


def test_every_parse_rule_has_a_label():
    """Правило без подписи экран показал бы голым именем (проверка 24.09)."""
    assert set(address_parse.PARSE_RULE_LABEL) == set(address_parse.PARSE_RULES)
    assert all(label.strip() for label in address_parse.PARSE_RULE_LABEL.values())


async def test_an_enabled_parse_rule_shows_on_screen(client, tokens):
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"parse_rules": "STOP_LATIN_BRAND=on, workshop_echo=shadow"},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    state = {r["rule"]: r["state"] for r in res.json()["parse_rules_effective"]}
    assert state["STOP_LATIN_BRAND"] == "on"
    assert state["workshop_echo"] == "shadow"
    assert state["STOP_PRONOUN"] == "off"
