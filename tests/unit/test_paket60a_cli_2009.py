"""Пакет 6.0а (20.09), исполнитель C — CLI и задачи: I-5, I-8, лестница политик.

Что стережётся:
- `address_funnel.measure_rules` — счётчики по правилу за 8 недель: решения,
  вердикты судьи (`trace.judge`), предложения, принятые руками
  (`trace.suggest` + `resolved_by_id`), отказы руками — по статусу строки И по
  журналу (замена автоадреса другим текстом через `candidate_id` автозаписи),
  дописанная квартира — правка, но не отказ (§0.4); одна строка — один отказ;
  тень (`trace.shadow.rule`); окно режет по `geo_checked_at`;
- `measure` — срезы `card_auto_place` и `house_after_place`;
- `wilson_bounds` — «1 из 50» не понижает, 0 из 60 не поднимает при ≤ 5 %;
- `address-audit-sample --trace k=v` (через `as_string()`, вложенный ключ через
  точку, булево) и `--degree suggest`; мусорный `--trace` → 2;
- `address-unfill`: только карточки автоматики; карточка руками и строка с
  `resolved_by_id` не трогаются; сухой прогон ничего не пишет; после `--no-dry-run`
  карточка пуста, строка `pending` в очереди карты с `trace.unfilled_at` и
  прежним судом в `prev`, журнал `client.address_unfilled`, кадр после commit'а;
- задача `rule_policy_weekly`: чистая лестница `decide` по §0.3 (понижение по
  нижней границе, подъёмы по верхней, потолок, вето человека, объявление exact с
  окном 7 дней ПО КАЛЕНДАРЮ, подмена места → off; выход из тени двумя путями:
  Уилсон от 73 осуждённых или «осуждены все» ≥ 20 без ложных), запись настройки
  `set_many(user_id=None)` + журнал + уведомление; commit до уведомления;
  регистрация в планировщике — через `build_scheduler()`, не по тексту исходника;
- вето по факту сохранения: строка журнала человека с `submitted=["rule_policy"]`
  делает правила её строки человеческими и при неизменном значении; тумблер без
  `rule_policy` — нет; после вето подъём до exact не наступает;
- нестрогое чтение политики: одна устаревшая пара в настройке или в журнале не
  сбрасывает соседние (`parse_rule_policy(strict=False)`), на записи — 400.

ДИВЕРСИИ (каждая обязана краснеть): снять `Client.address_set_at.is_(None)` из
WHERE `address-unfill` → карточка руками пустеет (тест «карточка руками не
трогается»); снять `a.resolved_by_id.is_(None)` → строка кнопки; заменить
`as_string()` на `==` в `_фильтр_следа` → SQLite не сравнит JSON; снять
`посчитаны` в `_правки_руками_по_правилам` → отказ считается дважды; убрать
`await db.commit()` до `notify` в задаче → тест «commit до уведомления»;
перенести `publish_client_updated` выше `db.commit()` → кадр в транзакции.

Телефоны — только +7 900 111-22-44, имён клиентов нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
import typer

from app.models import AuditLog, Client, ClientAddressCandidate, Notification
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING, CANDIDATE_REJECTED
from app.scheduler.jobs import rule_policy_weekly as job
from app.services import address_funnel, app_settings, clients_events, geocode
from app.services import notifications as notify_svc
from app.services.address_funnel import RuleCounts
from tests.unit.test_autobind_ops_1809 import (
    SINCE,
    UNTIL,
    В_ОКНЕ,
    НЕДЕЛЯ,
    _диалог,
    _журнал,
    regexp,
)

pytestmark = pytest.mark.anyio

__all__ = ["regexp"]  # фикстура REGEXP для SQLite — переиспользуется из обвязки 18.09

NOW = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
ДАВНО = NOW - timedelta(days=20)
#: Окно `address-unfill` от стенда, не от настоящих часов: без `--since` команда
#: режет `days` назад от `datetime.now(UTC)`, и с 01.10.2026 строки `ДАВНО`
#: выпали бы из окна сами собой (ревью 20.09, #16).
С_ОКНОМ_СТЕНДА = (ДАВНО - timedelta(days=1)).date().isoformat()
ОКНО = NOW - timedelta(weeks=address_funnel.RULE_WINDOW_WEEKS)
СУД_УЛИЦЫ = {"rule": "street_point", "policy": "exact", "query_form": "house"}


async def _строка(
    s: Any,
    account_id: uuid.UUID,
    *,
    trace: dict[str, Any] | None,
    status: str = CANDIDATE_ACCEPTED,
    resolved_by_id: uuid.UUID | None = None,
    checked_at: datetime = ДАВНО,
    карточка: bool = True,
    address_set_at: datetime | None = None,
    geo_provider: str = "dadata~approx",
    kind: str = "house",
) -> SimpleNamespace:
    """Клиент + диалог + строка со следом; `карточка=True` — адрес из этой строки
    (автоматика: `address_set_at` пуст, `address_candidate_id` — строка)."""
    client, conv, row = await _диалог(
        s,
        account_id,
        текст="ул Ленина 5",
        когда=checked_at,
        строка={
            "status": status,
            "resolved_at": checked_at if status != CANDIDATE_PENDING else None,
            "resolved_by_id": resolved_by_id,
            "geo_provider": geo_provider,
            "kind": kind,
        },
        карточка={"address_set_at": address_set_at} if карточка else None,
    )
    assert row is not None
    row.trace = trace
    row.geo_checked_at = checked_at
    await s.flush()
    return SimpleNamespace(client_id=client.id, conversation_id=conv.id, row_id=row.id)


async def _автозапись_и_правка(
    s: Any, сид: SimpleNamespace, *, новое: str | None, когда: datetime = ДАВНО
) -> None:
    """Журнал как в бою: автозапись без человека с `candidate_id`, затем правка
    руками того же клиента поверх этого текста."""
    await _журнал(
        s,
        action="client.address_captured",
        entity_id=str(сид.client_id),
        user_id=None,
        details={
            "source": "geocoder",
            "address": "улица Ленина, 5, Город",
            "candidate_id": str(сид.row_id),
        },
        когда=когда,
    )
    await _журнал(
        s,
        action="client.address_edited",
        entity_id=str(сид.client_id),
        user_id=uuid.uuid4(),
        details={"source": "manual", "previous": "улица Ленина, 5, Город", "address": новое},
        когда=когда + timedelta(hours=1),
    )


# ── воронка по правилам ─────────────────────────────────────────────────────


async def test_measure_rules_считает_решения_судью_и_руки(
    db_sessionmaker, make_avito_account
) -> None:  # noqa: ANN001
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        # street_point: семь боевых решений в окне. Вердикт судьи привязан к
        # суду `checked_at` (6.0б): без привязки воронка его не считает.
        суд = {"checked_at": address_funnel.checked_at_iso(ДАВНО)}
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "false", **суд}})
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "true", **суд}})
        await _строка(  # предложение, принятое оператором
            s,
            account.id,
            trace={**СУД_УЛИЦЫ, "policy": "suggest", "suggest": True},
            resolved_by_id=uuid.uuid4(),
        )
        await _строка(  # «Не адрес» / «Адрес неверный»
            s, account.id, trace=СУД_УЛИЦЫ, status=CANDIDATE_REJECTED, resolved_by_id=uuid.uuid4()
        )
        заменили = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await _автозапись_и_правка(s, заменили, новое="проспект Мира, 10")
        дописали = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await _автозапись_и_правка(s, дописали, новое="улица Ленина, 5, Город, кв 3")
        стёрли = await _строка(  # стирание руками: строка отказана статусом И правка в журнале
            s, account.id, trace=СУД_УЛИЦЫ, status=CANDIDATE_REJECTED, resolved_by_id=uuid.uuid4()
        )
        await _автозапись_и_правка(s, стёрли, новое=None)
        # Тень street_point у строки, решённой другим правилом.
        await _строка(
            s,
            account.id,
            trace={
                "rule": "house_family",
                "policy": "exact",
                "judge": {"verdict": "place_swap", **суд},
                "shadow": {
                    "rule": "street_point",
                    "status": "exact",
                    "key": "k",
                    "instead": "house_family",
                    "judge": {"verdict": "false", **суд},
                },
            },
        )
        # Вне окна — не в счёт.
        await _строка(s, account.id, trace=СУД_УЛИЦЫ, checked_at=ОКНО - timedelta(days=1))
        # Без следа — не в счёт.
        await _строка(s, account.id, trace=None)
        await s.commit()
    async with db_sessionmaker() as s:
        по_правилам = await address_funnel.measure_rules(s, until=NOW)
    assert set(по_правилам) == {"street_point", "house_family"}
    sp = по_правилам["street_point"]
    assert (sp.n, sp.judged, sp.false, sp.disputed, sp.place_swap) == (7, 2, 1, 0, 0)
    assert (sp.accepted_by_hand, sp.rejected_by_hand, sp.edited_by_hand) == (1, 3, 3)
    assert (sp.shadow_n, sp.shadow_judged, sp.shadow_false) == (1, 1, 1)
    assert sp.first_at is not None and sp.first_at.replace(tzinfo=UTC) == ДАВНО
    assert (sp.signals, sp.false_total) == (2 + 1 + 3, 1 + 3)
    hf = по_правилам["house_family"]
    assert (hf.n, hf.judged, hf.false, hf.place_swap, hf.shadow_n) == (1, 1, 1, 1, 0)
    assert hf.as_dict()["first_at"] == ДАВНО.isoformat()


def test_address_changed_дописывание_не_отказ() -> None:
    assert not address_funnel.address_changed("улица Ленина, 5", "Улица Ленина,  5, кв 3")
    assert address_funnel.address_changed("улица Ленина, 5", "проспект Мира, 10")
    assert address_funnel.address_changed("улица Ленина, 5", None)
    assert address_funnel.address_changed("улица Ленина, 5", "")


async def test_measure_срезы_места_и_дома_после_места(
    db_sessionmaker, make_avito_account, regexp
) -> None:  # noqa: ANN001
    """`card_auto_place` — автокарточка из строки-места (внутри approx);
    `house_after_place` — в карточке дом автоматики, а у клиента есть другая
    строка-место, принятая автоматикой (место уступило дому и осталось
    `accepted`, как делает воркер)."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        await _диалог(
            s,
            account.id,
            текст="снт Солнечный 15",
            строка={"kind": "place", "geo_provider": "dadata~approx"},
            карточка={},
        )
        client, conv, дом = await _диалог(
            s, account.id, текст="ул Ленина 5", строка={}, карточка={}
        )
        assert дом is not None
        s.add(
            ClientAddressCandidate(
                client_id=client.id,
                conversation_id=conv.id,
                value="снт Радуга",
                street="снт Радуга",
                house="",
                raw="снт Радуга",
                level="A",
                status=CANDIDATE_ACCEPTED,
                detected_at=В_ОКНЕ - timedelta(days=1),
                resolved_at=В_ОКНЕ - timedelta(days=1),
                kind="place",
                geo_status="exact",
                geo_provider="dadata~approx",
                geo_formatted="СНТ Радуга, Город",
                geo_lat=55.1,
                geo_lon=37.1,
            )
        )
        # Дом руками с местом рядом — не считается: срез только автоматики.
        await _диалог(
            s, account.id, текст="ул Мира 7", строка={}, карточка={"address_set_at": В_ОКНЕ}
        )
        # Дом автоматики, а место рядом принято КНОПКОЙ или отказано — лестница
        # `place → house` тут ни при чём (ДИВЕРСИЯ: снять `resolved_by_id IS NULL`
        # или `status == accepted` из `было_место` — станет 3).
        for место in (
            {"status": CANDIDATE_ACCEPTED, "resolved_by_id": uuid.uuid4()},
            {"status": CANDIDATE_REJECTED, "resolved_by_id": None},
        ):
            client3, conv3, _ = await _диалог(
                s, account.id, текст="ул Победы 9", строка={}, карточка={}
            )
            s.add(
                ClientAddressCandidate(
                    client_id=client3.id,
                    conversation_id=conv3.id,
                    value="снт Заря",
                    street="снт Заря",
                    house="",
                    raw="снт Заря",
                    level="A",
                    detected_at=В_ОКНЕ - timedelta(days=1),
                    resolved_at=В_ОКНЕ - timedelta(days=1),
                    kind="place",
                    geo_status="exact",
                    geo_provider="dadata~approx",
                    geo_formatted="СНТ Заря, Город",
                    geo_lat=55.2,
                    geo_lon=37.2,
                    **место,
                )
            )
        await s.commit()
    async with db_sessionmaker() as s:
        c = await address_funnel.measure(s, since=SINCE, until=UNTIL)
    assert (c.card, c.card_auto_exact, c.card_auto_approx, c.card_person) == (5, 3, 1, 1)
    assert (c.card_auto_place, c.house_after_place) == (1, 1)
    assert c.card == c.card_auto + c.card_person + c.card_unknown


def test_wilson_bounds_по_порогам_программы() -> None:
    нижняя, верхняя = address_funnel.wilson_bounds(1, 50)
    assert нижняя < 0.02 < 0.05 < верхняя, "«1 из 50» не понижает exact, но и не поднимает"
    assert address_funnel.wilson_bounds(0, 0) == (0.0, 1.0)
    _, верх60 = address_funnel.wilson_bounds(0, 60)
    _, верх73 = address_funnel.wilson_bounds(0, 73)
    assert верх60 > 0.05 >= верх73, "0 ложных: верхняя граница ≤ 5 % только с 73 осуждённых"
    нижняя30, _ = address_funnel.wilson_bounds(30, 30)
    assert нижняя30 > 0.08
    assert address_funnel.wilson_bounds(5, 3) == address_funnel.wilson_bounds(3, 3)


async def test_address_funnel_печатает_блок_по_правилам(
    db_sessionmaker, make_avito_account, regexp, capsys
) -> None:  # noqa: ANN001
    """`address-funnel` с `rules=True` — блок `by_rule` за 8 недель до конца
    недели: имя, n, judged, false, …, верхняя граница Уилсона."""
    from app.cli import run_address_funnel

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        await _строка(
            s,
            account.id,
            trace={
                **СУД_УЛИЦЫ,
                "judge": {
                    "verdict": "false",
                    "checked_at": address_funnel.checked_at_iso(UNTIL - timedelta(days=2)),
                },
            },
            checked_at=UNTIL - timedelta(days=2),
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_funnel(s, week=НЕДЕЛЯ.isoformat(), store=False)
    assert "by_rule" not in capsys.readouterr().out, "из кода — без блока, если не просили"
    async with db_sessionmaker() as s:
        await run_address_funnel(s, week=НЕДЕЛЯ.isoformat(), store=False, rules=True)
    вывод = capsys.readouterr().out
    assert "by_rule за 8 нед." in вывод and "card_auto_place\t0" in вывод
    строка = next(х for х in вывод.splitlines() if х.startswith("street_point\t"))
    assert строка.split("\t")[:4] == ["street_point", "1", "1", "1"]
    assert строка.split("\t")[10] == "100.0"


# ── address-audit-sample --trace / --degree suggest ────────────────────────


def _таблица(out: str) -> list[list[str]]:
    return [х.split("\t") for х in out.splitlines() if "\t" in х and not х.startswith(("{", "№"))]


async def test_audit_sample_фильтр_следа_и_предложения(
    db_sessionmaker, make_avito_account, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_audit_sample

    account = await make_avito_account()
    свежо = datetime.now(UTC) - timedelta(days=1)
    async with db_sessionmaker() as s:
        улица = await _строка(s, account.id, trace=СУД_УЛИЦЫ, checked_at=свежо)
        await _строка(
            s,
            account.id,
            trace={"rule": "house_family", "shadow": {"rule": "street_point", "status": "exact"}},
            checked_at=свежо,
        )
        предложение = await _строка(
            s,
            account.id,
            trace={**СУД_УЛИЦЫ, "policy": "suggest", "suggest": True},
            status=CANDIDATE_PENDING,
            карточка=False,
            geo_provider="dadata~approx~suggest",
            checked_at=свежо,
        )
        await s.commit()
    async with db_sessionmaker() as s:
        await run_address_audit_sample(
            s, days=7, limit=50, degree="all", trace=["rule=street_point"]
        )
    строки = _таблица(capsys.readouterr().out)
    assert [(х[1], х[8]) for х in строки] == [(str(улица.conversation_id), "street_point")]
    async with db_sessionmaker() as s:
        await run_address_audit_sample(
            s, days=7, limit=50, degree="all", trace=["shadow.rule=street_point"]
        )
    строки = _таблица(capsys.readouterr().out)
    assert [х[8] for х in строки] == ["house_family"], "вложенный ключ через точку"
    async with db_sessionmaker() as s:
        await run_address_audit_sample(
            s, days=7, limit=50, degree="suggest", trace=["suggest=true"]
        )
    вывод = capsys.readouterr().out
    строки = _таблица(вывод)
    assert "предложение правила" in вывод
    assert [(х[1], х[2], х[8]) for х in строки] == [
        (str(предложение.conversation_id), "suggest", "street_point")
    ]
    assert "улица Ленина, 5, Город" in строки[0][6], "вместо адреса карточки — строка карты правила"
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_audit_sample(s, days=7, limit=50, degree="all", trace=["rule"])
    assert exc.value.exit_code == 2
    async with db_sessionmaker() as s:  # ключ не латиницей — 2; значение — любая строка
        with pytest.raises(typer.Exit) as exc:
            await run_address_audit_sample(s, days=7, limit=50, degree="all", trace=["правило=x"])
    assert exc.value.exit_code == 2


def test_фильтр_следа_строится_через_as_string_а_не_включение() -> None:
    """Стенд — SQLite: `@>` там нет, `as_string()` компилируется в JSON_EXTRACT."""
    from sqlalchemy.dialects import sqlite

    from app.cli import _фильтр_следа

    (условие,) = _фильтр_следа(ClientAddressCandidate.trace, ["shadow.rule=street_point"])
    sql = str(условие.compile(dialect=sqlite.dialect()))
    assert "JSON_EXTRACT" in sql.upper() and "@>" not in sql


# ── address-unfill ───────────────────────────────────────────────────────────


@pytest.fixture
def кадры(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    записи: list[dict[str, Any]] = []

    async def публикация(_redis: Any, kind: str, payload: dict[str, Any], **_kw: Any) -> None:
        записи.append({"kind": kind, **payload})

    monkeypatch.setattr(clients_events, "publish_event", публикация)
    return записи


async def _стенд_снятия(db_sessionmaker, account) -> dict[str, SimpleNamespace]:  # noqa: ANN001
    async with db_sessionmaker() as s:
        сиды = {
            "авто": await _строка(s, account.id, trace=СУД_УЛИЦЫ),
            "руками": await _строка(s, account.id, trace=СУД_УЛИЦЫ, address_set_at=ДАВНО),
            "кнопкой": await _строка(s, account.id, trace=СУД_УЛИЦЫ, resolved_by_id=uuid.uuid4()),
            "другое_правило": await _строка(s, account.id, trace={"rule": "house_family"}),
        }
        await s.commit()
    return сиды


async def test_unfill_сухой_прогон_считает_и_ничего_не_пишет(
    db_sessionmaker, make_avito_account, redis, capsys, кадры
) -> None:  # noqa: ANN001
    from app.cli import run_address_unfill

    сиды = await _стенд_снятия(db_sessionmaker, await make_avito_account())
    async with db_sessionmaker() as s:
        commit = s.commit
        счёт = {"commit": 0}

        async def commit_шпион() -> None:
            счёт["commit"] += 1
            await commit()

        s.commit = commit_шпион  # type: ignore[method-assign]
        n = await run_address_unfill(
            s, trace=["rule=street_point"], since=С_ОКНОМ_СТЕНДА, days=30, redis=redis
        )
    assert n == 1 and счёт["commit"] == 0
    вывод = capsys.readouterr().out
    assert "карточек автоматики под rule=street_point" in вывод and ": 1" in вывод
    assert "снято: 0 сухой_прогон=True" in вывод
    async with db_sessionmaker() as s:
        for сид in сиды.values():
            assert (await s.get(Client, сид.client_id)).address is not None
    assert кадры == []


async def test_unfill_снимает_только_карточку_автоматики(
    db_sessionmaker, make_avito_account, redis, capsys, кадры, monkeypatch
) -> None:  # noqa: ANN001
    """Карточка руками, строка кнопки и чужое правило не трогаются; у снятой —
    карточка пуста, строка `pending` в очереди карты с `trace.unfilled_at` и
    прежним судом в `prev`, журнал `client.address_unfilled`, кадр после commit'а.
    ДИВЕРСИЯ: снять `Client.address_set_at.is_(None)` из WHERE — «руками» пустеет."""
    from app.cli import run_address_unfill

    сиды = await _стенд_снятия(db_sessionmaker, await make_avito_account())
    в_транзакции: list[bool] = []
    async with db_sessionmaker() as s:
        публикация = clients_events.publish_event

        async def публикация_со_снимком(*args: Any, **kwargs: Any) -> None:
            в_транзакции.append(s.in_transaction())
            await публикация(*args, **kwargs)

        monkeypatch.setattr(clients_events, "publish_event", публикация_со_снимком)
        n = await run_address_unfill(
            s,
            trace=["rule=street_point"],
            since=С_ОКНОМ_СТЕНДА,
            days=30,
            dry_run=False,
            redis=redis,
        )
    assert n == 1 and "снято: 1 сухой_прогон=False" in capsys.readouterr().out
    assert в_транзакции == [False], "кадр ушёл до commit'а"
    assert [(к["kind"], к["client_id"], к["reason"]) for к in кадры] == [
        ("client:updated", str(сиды["авто"].client_id), "rule_unfilled")
    ]
    async with db_sessionmaker() as s:
        снятая = await s.get(Client, сиды["авто"].client_id)
        assert (снятая.address, снятая.address_candidate_id, снятая.address_value) == (
            None,
            None,
            None,
        )
        строка = await s.get(ClientAddressCandidate, сиды["авто"].row_id)
        assert (строка.status, строка.resolved_at, строка.resolved_by_id) == (
            CANDIDATE_PENDING,
            None,
            None,
        )
        assert (строка.geo_status, строка.geo_checked_at, строка.geo_attempts) == (
            geocode.GEO_PENDING,
            None,
            0,
        )
        assert строка.geo_lat is None and строка.geo_prev is None
        assert строка.trace["prev"]["rule"] == "street_point" and "rule" not in строка.trace
        datetime.fromisoformat(строка.trace["unfilled_at"])
        for имя in ("руками", "кнопкой", "другое_правило"):
            card = await s.get(Client, сиды[имя].client_id)
            assert card.address is not None and card.address_candidate_id == сиды[имя].row_id, имя
            row = await s.get(ClientAddressCandidate, сиды[имя].row_id)
            assert row.status == CANDIDATE_ACCEPTED and row.geo_status == "exact", имя
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "client.address_unfilled")
                )
            )
            .scalars()
            .all()
        )
    assert len(журнал) == 1
    d = журнал[0].details
    assert журнал[0].user_id is None and журнал[0].entity_id == str(сиды["авто"].client_id)
    assert (d["rule"], d["candidate_id"], d["address"]) == (
        "street_point",
        str(сиды["авто"].row_id),
        None,
    )
    assert d["previous"] == "улица Ленина, 5, Город"


async def test_unfill_без_фильтра_и_с_окном_since(
    db_sessionmaker, make_avito_account, redis, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_unfill

    await _стенд_снятия(db_sessionmaker, await make_avito_account())
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_unfill(s, trace=[], days=30, redis=redis)
        assert exc.value.exit_code == 2
        # `--since` позже записи — ноль; раньше — одна.
        assert (
            await run_address_unfill(
                s, trace=["rule=street_point"], since=(ДАВНО + timedelta(days=1)).date().isoformat()
            )
            == 0
        )
        assert (
            await run_address_unfill(
                s, trace=["rule=street_point"], since=(ДАВНО - timedelta(days=1)).date().isoformat()
            )
            == 1
        )
    capsys.readouterr()


def test_команды_и_действия_журнала_заведены() -> None:
    from app.cli import app as cli_app
    from app.services.audit import AUDIT_ACTIONS

    assert "address-unfill" in {c.name for c in cli_app.registered_commands}
    assert "client.address_unfilled" in AUDIT_ACTIONS
    assert job.ANNOUNCE_ACTION in AUDIT_ACTIONS


# ── лестница политик: чистая функция ────────────────────────────────────────


def _c(**kw: Any) -> RuleCounts:
    kw.setdefault("first_at", NOW - timedelta(days=30))
    return RuleCounts(**kw)


def test_decide_понижение_по_нижней_границе_а_не_по_доле() -> None:
    # 1 ложное из 50 известных у exact — нижняя граница < 2 %: не трогаем.
    assert job.decide("street_point", "exact", _c(n=200, judged=50, false=1), now=NOW) is None
    # 30 отказов руками из 30 известных — понижение на одну ступень.
    ход = job.decide("street_point", "exact", _c(n=120, rejected_by_hand=30), now=NOW)
    assert ход is not None and (ход.from_policy, ход.to_policy, ход.up) == (
        "exact",
        "approx",
        False,
    )
    # Меньше 30 известных — молчим, даже если все ложные.
    assert job.decide("street_point", "exact", _c(n=120, rejected_by_hand=29), now=NOW) is None
    # Цель ступени своя: 10 % ложных при approx (цель 5 %) — вниз, при suggest (8 %) — нет…
    c = _c(n=300, judged=200, false=20)
    assert job.decide("street_point", "approx", c, now=NOW) is not None
    assert job.decide("street_point", "suggest", c, now=NOW) is None
    # …а понижение действует и на значение, поставленное человеком: это страховка.
    assert job.decide("street_point", "approx", c, now=NOW, human_set=True) is not None


def test_decide_подъёмы_по_верхней_границе_возрасту_и_потолку() -> None:
    молодое = _c(n=100, judged=80, first_at=NOW - timedelta(days=10))
    assert job.decide("street_point", "suggest", молодое, now=NOW) is None, "моложе двух недель"
    зрелое = _c(n=100, judged=80)
    ход = job.decide("street_point", "suggest", зрелое, now=NOW)
    assert ход is not None and (ход.to_policy, ход.up) == ("approx", True)
    # 0 из 60 — верхняя граница 6 % > 5 %: ещё нет.
    assert job.decide("street_point", "suggest", _c(n=100, judged=60), now=NOW) is None
    # Подмена места при точной точке запирает подъём suggest → approx.
    assert (
        job.decide("street_point", "suggest", _c(n=100, judged=80, place_swap=1), now=NOW) is None
    )
    # Потолок: default_street_type — suggest навсегда; named_place_country — approx.
    assert job.decide("default_street_type", "suggest", зрелое, now=NOW) is None
    assert job.decide("named_place_country", "suggest", зрелое, now=NOW) is not None
    сотни = _c(n=300, judged=250, first_at=NOW - timedelta(days=50))
    assert job.decide("named_place_country", "approx", сотни, now=NOW, judge_ok=True) is None
    # Значение человека вверх не переписывается.
    assert job.decide("street_point", "suggest", зрелое, now=NOW, human_set=True) is None
    # Тень: нужны осуждённые теневые решения, не боевые.
    тень = _c(n=0, shadow_n=90, shadow_judged=80, first_at=NOW - timedelta(days=30))
    ход = job.decide("street_point", "shadow", тень, now=NOW)
    assert ход is not None and ход.to_policy == "suggest"
    assert job.decide("street_point", "shadow", _c(n=0, shadow_n=90), now=NOW) is None


def _тень(n: int, k: int = 0, **kw: Any) -> RuleCounts:
    return _c(n=0, shadow_n=n, shadow_judged=n, shadow_false=k, **kw)


def test_decide_выход_из_тени_двумя_путями() -> None:
    """Решение владельца 20.09 (ревью #7): Уилсон ≤ 5 % достижим с 73 осуждённых
    (не с 60), поэтому ниже 73 работает ветка «осуждены все ≥ 20 без ложных и
    подмен места» — иначе при 15–35 решениях в месяц правило не выходило бы из
    тени никогда. ДИВЕРСИЯ: вернуть `верхняя ≤ 0.05` в ветку «все» — 72/72 и
    20/20 краснеют."""

    def куда(c: RuleCounts) -> str | None:
        ход = job.decide("street_point", "shadow", c, now=NOW)
        return ход.to_policy if ход else None

    # Ветка «осуждены все»: от 20 при нуле ложных, до границы Уилсона.
    assert куда(_тень(72)) == "suggest"
    assert куда(_тень(20)) == "suggest"
    assert куда(_тень(19)) is None, "меньше 20 — слишком мало, чтобы верить нулю"
    assert куда(_тень(20, k=1)) is None, "одно ложное при малом объёме — сидим"
    assert куда(_тень(50, place_swap=1)) is None, "подмена места запирает выход"
    assert куда(_тень(73, place_swap=1)) is None, "…и путь по Уилсону тоже"
    assert куда(_c(n=0, shadow_n=30, shadow_judged=25)) is None, "осуждены не все"
    # От 73 — только по верхней границе Уилсона, ложные допустимы.
    assert куда(_тень(73)) == "suggest"
    assert куда(_тень(109, k=1)) is None
    assert куда(_тень(110, k=1)) == "suggest"
    assert job.SHADOW_MIN_JUDGED == 73
    assert address_funnel.wilson_bounds(0, 73)[1] <= 0.05 < address_funnel.wilson_bounds(0, 72)[1]
    # Возраст и потолок действуют на оба пути.
    assert куда(_тень(72, first_at=NOW - timedelta(days=10))) is None
    assert job.decide("default_street_type", "shadow", _тень(72), now=NOW) is not None
    ход = job.decide("street_point", "shadow", _тень(30), now=NOW)
    assert ход is not None and "осуждены все 30" in ход.why


def test_decide_exact_с_объявлением_и_вето() -> None:
    c = _c(n=400, judged=200, first_at=NOW - timedelta(days=50))
    # Без калибровки судьи (умолчание `judge_calibrated()` = False) — ничего.
    assert job.decide("street_point", "approx", c, now=NOW) is None
    ход = job.decide("street_point", "approx", c, now=NOW, judge_ok=True)
    assert ход is not None and (ход.to_policy, ход.announce) == ("exact", True)
    ещё_рано = NOW - timedelta(days=3)
    assert (
        job.decide("street_point", "approx", c, now=NOW, judge_ok=True, announced_at=ещё_рано)
        is None
    )
    пора = NOW - timedelta(days=8)
    ход = job.decide("street_point", "approx", c, now=NOW, judge_ok=True, announced_at=пора)
    assert ход is not None and (ход.to_policy, ход.announce) == ("exact", False)
    # Окно — по календарю UTC, не по миллисекундам (ревью #20): `created_at`
    # объявления — `func.now()` транзакции, оно на джиттер ПОЗЖЕ `now` прошлого
    # прогона; крон ровно через неделю обязан поднять при любом знаке джиттера.
    for джиттер in (timedelta(seconds=1), timedelta(seconds=-1), timedelta(minutes=50)):
        ход = job.decide(
            "street_point",
            "approx",
            c,
            now=NOW,
            judge_ok=True,
            announced_at=NOW - timedelta(days=job.VETO_DAYS) + джиттер,
        )
        assert ход is not None and not ход.announce, джиттер
    # Шесть суток — ещё рано, даже почти семь.
    почти = NOW - timedelta(days=job.VETO_DAYS - 1)
    assert (
        job.decide("street_point", "approx", c, now=NOW, judge_ok=True, announced_at=почти) is None
    )
    # Человек вписал правило сам — подъёма нет (вето).
    assert (
        job.decide(
            "street_point", "approx", c, now=NOW, judge_ok=True, announced_at=пора, human_set=True
        )
        is None
    )
    # 150 осуждённых с 1 ложным: верхняя граница 3,6 % > 2 % — нет.
    assert (
        job.decide("street_point", "approx", _c(n=400, judged=150, false=1), now=NOW, judge_ok=True)
        is None
    )


def test_decide_подмена_места_выключает_правило_класса() -> None:
    ход = job.decide("street_suggest", "suggest", _c(n=5, judged=5, place_swap=1), now=NOW)
    assert ход is not None and ход.to_policy == "off"
    # У правил вне класса подмена — ложное, но не мгновенный off.
    assert job.decide("street_point", "exact", _c(n=5, judged=5, place_swap=1), now=NOW) is None
    assert job.decide("nope", "exact", _c(), now=NOW) is None  # чужое имя — без падения и без хода
    assert job.decide("street_point", "чушь", _c(), now=NOW) is None


def test_тексты_уведомлений_и_настройки() -> None:
    off = job.Move("street_suggest", "suggest", "off", "подмена места при точной точке: 1")
    текст = job.notification_body(off, _c(n=5, judged=5, place_swap=1))
    assert "suggest → off" in текст and job.unfill_command("street_suggest") in текст
    assert "address-unfill --trace rule=street_suggest --days 30" in текст
    вниз = job.Move("street_point", "exact", "approx", "x")
    текст = job.notification_body(вниз, _c(n=120, rejected_by_hand=30))
    assert "address-unfill" not in текст and "точка улицы, дом не найден" in текст
    объявление = job.Move("street_point", "approx", "exact", "x", announce=True)
    текст = job.notification_body(объявление, _c(), deadline=NOW + timedelta(days=7))
    assert "28.09" in текст and "street_point=approx" in текст and "будет поднято" in текст
    # Вето — сохранение строки кнопкой экрана (проверка 24.09), значение уже стоит.
    assert "«Сохранить политику»" in текст and "Перекрытия политики" in текст
    assert job.policy_text({"b": "off", "a": "suggest"}) == "a=suggest,b=off"
    assert geocode.parse_rule_policy(job.policy_text({"street_point": "approx"})) == {
        "street_point": "approx"
    }


# ── лестница политик: задача ─────────────────────────────────────────────────


@pytest.fixture
def оснастка_задачи(monkeypatch, db_sessionmaker, redis):  # noqa: ANN001
    monkeypatch.setattr(job.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(job.db_mod, "session_scope", db_sessionmaker)


async def _тридцать_отказов(db_sessionmaker, account) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        for _ in range(30):
            await _строка(
                s,
                account.id,
                trace=СУД_УЛИЦЫ,
                status=CANDIDATE_REJECTED,
                resolved_by_id=uuid.uuid4(),
            )
        await s.commit()


async def test_задача_понижает_правило_пишет_настройку_журнал_и_уведомление(
    db_sessionmaker, make_avito_account, оснастка_задачи
) -> None:  # noqa: ANN001
    await _тридцать_отказов(db_sessionmaker, await make_avito_account())
    ходы = await job.review_rule_policies(now=NOW)
    assert [(х.rule, х.from_policy, х.to_policy) for х in ходы] == [
        ("street_point", "exact", "approx")
    ]
    async with db_sessionmaker() as s:  # новая сессия: только закоммиченное
        текст = await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY)
        assert geocode.parse_rule_policy(текст) == {"street_point": "approx"}
        строка = (
            await s.execute(
                sa.select(app_settings.AppSetting).where(
                    app_settings.AppSetting.key == app_settings.ADDRESS_GEO_RULE_POLICY
                )
            )
        ).scalar_one()
        assert строка.updated_by_id is None
        журнал = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == job.SETTINGS_ACTION)))
            .scalars()
            .all()
        )
        assert len(журнал) == 1 and журнал[0].user_id is None
        d = журнал[0].details
        assert d["source"] == job.SOURCE and d["after"]["rule_policy"] == "street_point=approx"
        assert d["moves"][0]["rule"] == "street_point" and d["moves"][0]["to"] == "approx"
        уведомления = (await s.execute(sa.select(Notification))).scalars().all()
        assert [(у.kind, у.entity_id, у.audience) for у in уведомления] == [
            (job.KIND_DEGRADED, "street_point", "admin")
        ]
        assert "exact → approx" in уведомления[0].body
    # Второй прогон: значение уже approx, 30 отказов (100 %) — ещё ступень вниз.
    ходы = await job.review_rule_policies(now=NOW)
    assert [(х.from_policy, х.to_policy) for х in ходы] == [("approx", "suggest")]


async def test_задача_коммитит_политику_до_уведомления(
    db_sessionmaker, make_avito_account, оснастка_задачи, monkeypatch
) -> None:  # noqa: ANN001
    """Центр уведомлений упал — политика и журнал обязаны остаться.
    ДИВЕРСИЯ: убрать `await db.commit()` перед циклом `notify` — краснеет."""
    await _тридцать_отказов(db_sessionmaker, await make_avito_account())

    async def падает(*a: Any, **k: Any) -> Any:
        raise RuntimeError("центр уведомлений недоступен")

    monkeypatch.setattr(notify_svc, "notify", падает)
    with pytest.raises(RuntimeError):
        await job.review_rule_policies(now=NOW)
    async with db_sessionmaker() as s:
        текст = await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY)
        assert geocode.parse_rule_policy(текст) == {"street_point": "approx"}


async def test_задача_без_ходов_ничего_не_пишет(db_sessionmaker, оснастка_задачи) -> None:  # noqa: ANN001
    assert await job.review_rule_policies(now=NOW) == []
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY) == ""
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(Notification))
        ).scalar_one() == 0


async def test_кто_ставил_последним_по_журналу_настроек(db_sessionmaker) -> None:  # noqa: ANN001
    """Человек поставил `street_point=approx` через ручку (строка с `user_id` и
    видами before/after) — правило его; потом задача понизила `house_family` —
    её строка без человека, `street_point` в ней не менялся."""
    async with db_sessionmaker() as s:
        await _журнал(
            s,
            action=job.SETTINGS_ACTION,
            entity_id=None,
            user_id=uuid.uuid4(),
            details={
                "before": {"rule_policy": ""},
                "after": {"rule_policy": "street_point=approx"},
            },
            когда=NOW - timedelta(days=3),
        )
        await _журнал(
            s,
            action=job.SETTINGS_ACTION,
            entity_id=None,
            user_id=None,
            details={
                "source": job.SOURCE,
                "before": {"rule_policy": "street_point=approx"},
                "after": {"rule_policy": "house_family=approx,street_point=approx"},
            },
            когда=NOW - timedelta(days=2),
        )
        await _журнал(
            s,
            action=job.ANNOUNCE_ACTION,
            entity_id=None,
            user_id=None,
            details={"rule": "country", "from": "approx", "to": "exact"},
            когда=NOW - timedelta(days=8),
        )
        await s.commit()
    async with db_sessionmaker() as s:
        assert await job.human_set_rules(s) == {"street_point"}
        объявлено = await job.announcements(s, since=NOW - timedelta(days=14))
        assert set(объявлено) == {"country"}
        assert await job.announcements(s, since=NOW - timedelta(days=7)) == {}


async def _строка_настроек(
    s: Any,
    *,
    когда: datetime,
    user_id: uuid.UUID | None,
    было: str,
    стало: str,
    submitted: list[str] | None = None,
    тумблер: tuple[dict[str, Any], dict[str, Any]] = ({}, {}),
) -> None:
    """Строка `settings.address_detect_changed` в форме ручки: виды before/after
    целиком (`тумблер` — прочие поля вида до и после), `submitted` — какие поля
    пришли в теле; строка автоматики несёт `source`."""
    details: dict[str, Any] = {
        "before": {"rule_policy": было, **тумблер[0]},
        "after": {"rule_policy": стало, **тумблер[1]},
    }
    if submitted is not None:
        details["submitted"] = submitted
    if user_id is None:
        details["source"] = job.SOURCE
    await _журнал(
        s, action=job.SETTINGS_ACTION, entity_id=None, user_id=user_id, details=details, когда=когда
    )


async def test_вето_по_факту_сохранения_а_не_по_смене_значения(db_sessionmaker) -> None:  # noqa: ANN001
    """Ревью #6. Значение `approx` вписала автоматика (строка без `user_id`);
    человек в окне объявления сохранил настройку с той же строкой — ручка
    пишет строку журнала с `submitted=["rule_policy"]` и при `after == before`.
    Правило — его. Тумблер, сохранённый без `rule_policy`, правил не
    присваивает, даже если строка политики в его before/after есть.
    ДИВЕРСИЯ: вернуть в `human_set_rules` учёт только по разнице значений —
    второе утверждение краснеет."""
    async with db_sessionmaker() as s:
        await _строка_настроек(
            s, когда=NOW - timedelta(days=30), user_id=None, было="", стало="street_point=approx"
        )
        await _строка_настроек(
            s,
            когда=NOW - timedelta(days=6),
            user_id=uuid.uuid4(),
            было="street_point=approx",
            стало="street_point=approx",
            submitted=["autofill"],
            тумблер=({"autofill": True}, {"autofill": False}),
        )
        await s.commit()
    async with db_sessionmaker() as s:
        assert await job.human_set_rules(s) == set(), "тумблер — не вето"
        await _строка_настроек(
            s,
            когда=NOW - timedelta(days=5),
            user_id=uuid.uuid4(),
            было="street_point=approx",
            стало="street_point=approx",
            submitted=["rule_policy"],
        )
        await s.commit()
    async with db_sessionmaker() as s:
        assert await job.human_set_rules(s) == {"street_point"}
        # Автоматика позже понизила правило — владение снова у неё (страховка
        # вниз действует, а следующий подъём вверх уже не «поверх человека»).
        await _строка_настроек(
            s,
            когда=NOW - timedelta(days=1),
            user_id=None,
            было="street_point=approx",
            стало="street_point=suggest",
        )
        await s.commit()
    async with db_sessionmaker() as s:
        assert await job.human_set_rules(s) == set()


@pytest.fixture
def судья_и_сотни(monkeypatch):  # noqa: ANN001
    """Стенд подъёма до exact без 400 строк в базе: судья откалиброван, у
    `street_point` 200 осуждённых без ложных за 50 дней."""

    async def откалиброван(db: Any) -> bool:  # 6.0б: судья читается из настройки в сессии
        return True

    monkeypatch.setattr(job, "judge_calibrated", откалиброван)

    async def замер(db: Any, *, until: datetime, weeks: int = 8) -> dict[str, RuleCounts]:
        return {"street_point": RuleCounts(n=400, judged=200, first_at=until - timedelta(days=50))}

    monkeypatch.setattr(job.address_funnel, "measure_rules", замер)


async def test_задача_после_вето_не_поднимает_а_без_вето_поднимает_без_нового_объявления(
    db_sessionmaker, оснастка_задачи, судья_и_сотни
) -> None:  # noqa: ANN001
    """Сквозной путь #6 и #20: объявление лежит в журнале 14 дней и 1 секунду
    (прогон недели 2 стартовал на джиттер позже недели 0 — объявление обязано
    остаться в окне, иначе задача объявляла бы заново); человек сохранил ту же
    строку `street_point=approx` — подъёма нет, настройка не тронута. Сняли
    строку человека — подъём до exact без второго объявления."""
    объявлено_когда = NOW - timedelta(days=2 * job.VETO_DAYS, seconds=1)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_RULE_POLICY: "street_point=approx"}, user_id=None
        )
        await _строка_настроек(
            s, когда=NOW - timedelta(days=30), user_id=None, было="", стало="street_point=approx"
        )
        await _журнал(
            s,
            action=job.ANNOUNCE_ACTION,
            entity_id=None,
            user_id=None,
            details={"rule": "street_point", "from": "approx", "to": "exact"},
            когда=объявлено_когда,
        )
        await _строка_настроек(
            s,
            когда=NOW - timedelta(days=5),
            user_id=uuid.uuid4(),
            было="street_point=approx",
            стало="street_point=approx",
            submitted=["rule_policy"],
        )
        await s.commit()
    assert await job.review_rule_policies(now=NOW) == []
    async with db_sessionmaker() as s:
        текст = await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY)
        assert текст == "street_point=approx"
        await s.execute(
            sa.delete(AuditLog).where(
                AuditLog.action == job.SETTINGS_ACTION, AuditLog.user_id.is_not(None)
            )
        )
        await s.commit()
    ходы = await job.review_rule_policies(now=NOW)
    assert [(х.rule, х.to_policy, х.announce) for х in ходы] == [("street_point", "exact", False)]
    async with db_sessionmaker() as s:
        assert (
            await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY) == "street_point=exact"
        )
        объявления = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == job.ANNOUNCE_ACTION)))
            .scalars()
            .all()
        )
        assert len(объявления) == 1, "второго объявления быть не должно"
        уведомления = (await s.execute(sa.select(Notification))).scalars().all()
        assert [(у.kind, у.entity_id) for у in уведомления] == [(job.KIND_PROMOTED, "street_point")]


async def test_задача_объявляет_если_объявление_старше_окна_с_запасом(
    db_sessionmaker, оснастка_задачи, судья_и_сотни
) -> None:  # noqa: ANN001
    """Объявление старше `2·VETO_DAYS + 1` суток — не в счёт: задача объявляет
    заново, а не поднимает по прошлогодней строке."""
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_RULE_POLICY: "street_point=approx"}, user_id=None
        )
        await _журнал(
            s,
            action=job.ANNOUNCE_ACTION,
            entity_id=None,
            user_id=None,
            details={"rule": "street_point", "from": "approx", "to": "exact"},
            когда=NOW - timedelta(days=2 * job.VETO_DAYS + 2),
        )
        await s.commit()
    ходы = await job.review_rule_policies(now=NOW)
    assert [(х.to_policy, х.announce) for х in ходы] == [("exact", True)]
    async with db_sessionmaker() as s:
        assert (
            await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY) == "street_point=approx"
        )


# ── нестрогое чтение политики (#5/#14) ──────────────────────────────────────


def test_parse_rule_policy_нестрого_пропускает_пару_и_оставляет_соседей(monkeypatch) -> None:  # noqa: ANN001
    """Одна устаревшая пара (имя, снятое из реестра, или чужое значение) не
    сбрасывает соседей на чтении; на записи — по-прежнему ValueError (400).
    ДИВЕРСИЯ: вернуть `raise` без `strict` — второй assert краснеет."""
    import structlog

    from app.services import address_parse

    # Память «уже предупреждали» — на процесс; стенд начинает с чистой.
    monkeypatch.setattr(geocode, "_ПРЕДУПРЕЖДЕНО", set())
    monkeypatch.setattr(address_parse, "_ПРЕДУПРЕЖДЕНО", set())
    текст = "street_point=shadow, house_famly=suggest, suburb=maybe"
    with pytest.raises(ValueError, match="неизвестное правило"):
        geocode.parse_rule_policy(текст)
    with structlog.testing.capture_logs() as логи:
        assert geocode.parse_rule_policy(текст, strict=False) == {"street_point": "shadow"}
        # Читается на каждую задачу карты: повтор той же пары — без второй
        # строки в журнале (проверка правок 21.09).
        assert geocode.parse_rule_policy(текст, strict=False) == {"street_point": "shadow"}
    assert [(л["event"], л["rule"]) for л in логи] == [
        ("rule_policy.unknown_rule", "house_famly"),
        ("rule_policy.unknown_rule", "suburb"),
    ]
    # Слой разбора — тот же договор.
    monkeypatch.setitem(address_parse.PARSE_RULES, "stop_probe_2009", "off")
    with pytest.raises(ValueError):
        address_parse.rules_from_setting("stop_probe_2009=on, stop_gone=on")
    with structlog.testing.capture_logs() as логи:
        for _ in range(2):
            assert address_parse.rules_from_setting(
                "stop_probe_2009=on, stop_gone=on", strict=False
            ) == {
                **address_parse.PARSE_RULES,
                "stop_probe_2009": "on",
            }
    assert [(л["event"], л["layer"], л["rule"]) for л in логи] == [
        ("rule_policy.unknown_rule", "parse", "stop_gone")
    ]


async def test_задача_читает_политику_нестрого_и_переписывает_без_устаревшей_пары(
    db_sessionmaker, make_avito_account, оснастка_задачи
) -> None:  # noqa: ANN001
    """В таблице «house_famly=suggest,street_point=approx» (имя снято из реестра
    после записи). Задача видит `street_point=approx`, а не пустую политику;
    30 отказов опускают его до suggest; в перезаписи устаревшей пары нет
    (`set_many` строг). Строка журнала «кто ставил» с устаревшим именем тоже
    читается по соседям."""
    await _тридцать_отказов(db_sessionmaker, await make_avito_account())
    async with db_sessionmaker() as s:
        s.add(
            app_settings.AppSetting(
                key=app_settings.ADDRESS_GEO_RULE_POLICY,
                value="house_famly=suggest,street_point=approx",
            )
        )
        await _строка_настроек(
            s,
            когда=NOW - timedelta(days=3),
            user_id=uuid.uuid4(),
            было="",
            стало="house_famly=suggest,street_point=approx",
            submitted=["rule_policy"],
        )
        await s.commit()
    async with db_sessionmaker() as s:
        assert await job.human_set_rules(s) == {"street_point"}
    ходы = await job.review_rule_policies(now=NOW)
    assert [(х.rule, х.from_policy, х.to_policy) for х in ходы] == [
        ("street_point", "approx", "suggest")
    ]
    async with db_sessionmaker() as s:
        assert (
            await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY)
            == "street_point=suggest"
        )


def test_задача_регистрируется_по_понедельникам_после_воронки() -> None:
    from apscheduler.triggers.cron import CronTrigger

    вызовы: list[dict] = []

    class Планировщик:
        def add_job(self, func, trigger, **kw):  # noqa: ANN001
            вызовы.append({"func": func, "trigger": trigger, **kw})

    job.register(Планировщик())
    (вызов,) = вызовы
    assert вызов["func"] is job.review_rule_policies
    assert вызов["id"] == job.JOB_ID == "address_rule_policy_weekly"
    assert isinstance(вызов["trigger"], CronTrigger)
    assert str(вызов["trigger"].fields[4]) == "mon"
    assert (str(вызов["trigger"].fields[5]), str(вызов["trigger"].fields[6])) == ("2", "10")
    assert вызов["max_instances"] == 1 and вызов["misfire_grace_time"] >= 3600


def test_планировщик_зовёт_register_лестницы() -> None:
    """⚠ ДИВЕРСИЯ: убрать `rule_policy_jobs.register(scheduler)` из
    `build_scheduler` (или спрятать под `if False:`) — краснеет. По образцу
    `test_voice_repair_0509`: задача ищется в собранном планировщике, а не в
    тексте исходника."""
    from app.scheduler.main import build_scheduler

    задача = build_scheduler().get_job(job.JOB_ID)
    assert задача is not None and задача.func is job.review_rule_policies


def test_виды_уведомлений_лестницы_заведены() -> None:
    for kind in (job.KIND_DEGRADED, job.KIND_PROMOTED):
        spec = notify_svc.KINDS[kind]
        assert spec.audience == "admin" and spec.dedup == "entity"
    assert notify_svc.KINDS[job.KIND_DEGRADED].severity == "warning"
    assert notify_svc.KINDS[job.KIND_PROMOTED].severity == "info"
