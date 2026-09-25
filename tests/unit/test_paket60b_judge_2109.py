"""Пакет 6.0б (21.09), исполнитель B — судья карточек (I-4) и базовый замер (I-7).

Что стережётся:
- `address_llm.parse_judgement` берёт только слова из `JUDGE_VERDICTS`
  (регистр и пробелы прощает), чужое слово → None; `build_judge_message`
  несёт адрес карточки и погашенную речь;
- квота судьи: запас читателю в общем потолке (`llm_calls_today + 1 <=
  потолок − 50`), своя доля по ключу `geo:llm:judge:<день>` занимается ДО
  похода атомарно (перебор → DECR), `on_request` пополняет оба счётчика, при
  умолчании потолка 50 судья не ходит по построению;
- `judge_row` пишет `trace.judge` с `checked_at` = `geo_checked_at` строки и
  `source=judge`; условный UPDATE: суд сменился за время похода — строка не
  переписана (`written=False`); строка без `geo_checked_at` судится, но не
  пишется (`reason=no_checked_at`); тень судится по `shadow.key` в
  `trace.shadow.judge`; без речи — `skipped_no_speech`; отказ читателей —
  `failed`, несостоявшийся поход место в доле возвращает;
- окно речи судьи — вокруг реплики-источника: до пяти реплик до неё
  включительно и не больше `РЕПЛИК_ПОСЛЕ` (3) за сутки после, длинные реплики
  «после» обрезаны, источник во входе гарантирован; калибровочная выгрузка
  видит то же окно; якорь без `message_at` — реплика `message_id`;
- `pick_rows`: пересуженная строка (`judge.checked_at ≠ geo_checked_at`) и
  вердикт без `checked_at` снова в очереди, свежеосуждённая — нет,
  `resolved_by_id` не берётся, правила по кругу; `rule=` — все неосуждённые
  решения правила, боевые и теневые, тень — не дожидаясь суда над чужим боем
  (`rule_target`), тень без ключа — нет;
- `measure_rules` не считает `judge` с чужим `checked_at`, без `checked_at` и с
  чужим словом — только с совпавшим;
- словарь промпта судьи по §0.4: тип улицы, названный клиентом явно, и регион
  — в пункте `place_swap`, `disputed` — только литера; примеры «пер Кирова 7»
  → place_swap, «Кирова 7» → true, «пр-т Мира 10» → true;
- `rule_policy_weekly.judge_calibrated(db)` — по настройке
  `address_llm.judge_agreement` ≥ 95; `decide` без `judge_ok` — не откалиброван;
- задача `address_judge_daily` зарегистрирована (`build_scheduler`), 00:40 UTC
  ежедневно; `judge_daily` судит очередь транзакцией на строку и
  останавливается на первом `skipped_quota`;
- CLI `address-judge`: `--calibrate` считает согласие, печатает матрицу и
  пишет настройку + журнал `source=judge_calibration` только при ≥ 100
  отвеченных пар (`JUDGE_CALIBRATION_N`); обрыв по квоте → настройка не
  тронута, выход 1; повтор тем же файлом берёт свежие вердикты из следа без
  похода (`from_trace`); `--force` пишет и при недоборе с `forced: true` в
  журнале; `--grade` печатает `false_pct`; `--sample --out` пишет TSV масками;
  `--in` пишет вердикт только когда правило файла И строка карты из файла
  совпали с решением строки (боевое `geo_formatted` → `judge`, тень
  `shadow.key` → `shadow`), иначе только печатает и считает `key_mismatch`;
  два режима разом → 2; `address-funnel` печатает строку `judge_agreement=`.

ДИВЕРСИИ (каждая обязана краснеть, прогнаны 21.09):
D1 `parse_judgement`: снять проверку `вердикт not in JUDGE_VERDICTS` →
   `test_parse_judgement_только_словарь`;
D2 `judge_row`: убрать `a.geo_checked_at.is_not_distinct_from(checked_at)` из
   WHERE → `test_judge_row_не_переписывает_сменившийся_суд`;
D3 `Quota.reader_reserved`: убрать `− ЗАПАС_ЧИТАТЕЛЮ` → `test_квота_запас_читателю`;
D4 `_занять`: всегда True (не сравнивать с долей) → `test_квота_доля_судьи_атомарна`;
D5 `address_funnel.judge_is_fresh`: не сравнивать `checked_at` →
   `test_measure_rules_не_считает_чужой_checked_at` и
   `test_pick_rows_пересуженная_снова_в_очереди`;
D6 `pick_rows`: снять `a.resolved_by_id.is_(None)` → `test_pick_rows_не_берёт_решённые_человеком`;
D7 `judge_calibrated`: `>=` → `>` → `test_judge_calibrated_по_настройке`;
D8 `scheduler/main.py`: убрать `address_judge_jobs.register` → `test_задача_в_планировщике`;
D9 CLI `--calibrate`: не звать `set_many` → `test_cli_calibrate_пишет_настройку`;
D10 `_речь_диалогов`: снять счётчик `РЕПЛИК_ПОСЛЕ` → `test_речь_судьи_окно_после_источника`
    и `test_калибровка_видит_то_же_окно`;
D11 `SYSTEM_PROMPT_JUDGE`: вернуть «тип улицы» в `disputed` → `test_словарь_промпта_по_0_4`;
D12 CLI `--calibrate`: снять `всего >= JUDGE_CALIBRATION_N` →
    `test_cli_calibrate_обрыв_не_пишет_и_добирает_из_следа`;
D13 `_слот_по_ключу`: не сравнивать ключ → `test_cli_in_пишет_только_под_своё_правило`;
D14 `judge_row`: не обнулять `target` при `checked_at is None` →
    `test_judge_row_не_переписывает_сменившийся_суд`;
D15 `pick_rows`: вернуть фильтр через `next_target` при `rule=` →
    `test_pick_rows_rule_берёт_тень_поверх_неосуждённого_боя`.

Телефоны — только +7 900 111-22-45, имён клиентов нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
import typer

from app.integrations import openrouter
from app.models import AuditLog, ClientAddressCandidate, Message
from app.models.client import CANDIDATE_ACCEPTED
from app.scheduler.jobs import address_judge as job
from app.scheduler.jobs import rule_policy_weekly
from app.services import address_funnel, address_judge, address_llm, app_settings, geocode
from app.services.address_funnel import RuleCounts
from app.workers.address_llm import llm_calls_key
from tests.unit.test_autobind_ops_1809 import _диалог

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
ДАВНО = NOW - timedelta(days=20)
СУД_УЛИЦЫ = {"rule": "street_point", "policy": "exact", "query_form": "house"}
ТЕЛЕФОН = "+7 900 111-22-45"


async def _строка(
    s: Any,
    account_id: uuid.UUID,
    *,
    trace: dict[str, Any] | None = None,
    текст: str | None = "ул Ленина 5",
    checked_at: datetime | None = ДАВНО,
    resolved_by_id: uuid.UUID | None = None,
    geo_formatted: str = "улица Ленина, 5, Бердск",
    geo_provider: str = "dadata",
    карточка: bool = True,
) -> SimpleNamespace:
    """Клиент + диалог + реплика + строка со следом; `карточка=True` — адрес
    карточки из этой строки (автоматика)."""
    client, conv, row = await _диалог(
        s,
        account_id,
        текст=текст,
        когда=checked_at or ДАВНО,
        строка={
            "status": CANDIDATE_ACCEPTED,
            "resolved_at": checked_at or ДАВНО,
            "resolved_by_id": resolved_by_id,
            "geo_provider": geo_provider,
            "geo_formatted": geo_formatted,
        },
        карточка={"address": geo_formatted, "address_set_at": None} if карточка else None,
    )
    assert row is not None
    row.trace = trace
    row.geo_checked_at = checked_at
    await s.flush()
    return SimpleNamespace(client_id=client.id, conversation_id=conv.id, row_id=row.id)


def _ответ(verdict: str = "true", reason: str = "дом тот же") -> dict[str, Any]:
    return {"verdict": verdict, "reason": reason}


def _судья(monkeypatch: pytest.MonkeyPatch, verdict: str = "true", *, attempts: int = 1):
    """Стаб `openrouter.chat_json`: зовёт `on_request` `attempts` раз (как
    шлюз — первый до похода, остальные дозываются) и возвращает вердикт."""
    увидел: list[str] = []

    async def chat_json(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        увидел.append(user)
        for _ in range(attempts):
            await kw["on_request"]()
        return _ответ(verdict), "b/two:free"

    monkeypatch.setattr(openrouter, "chat_json", chat_json)
    return увидел


async def _след(db_sessionmaker, row_id: uuid.UUID) -> dict[str, Any] | None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        return (
            await s.execute(
                sa.select(ClientAddressCandidate.trace).where(ClientAddressCandidate.id == row_id)
            )
        ).scalar_one()


async def _потолки(db_sessionmaker, *, limit: int | None, share: int | None = 300) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_LLM_DAILY_LIMIT: limit,
                app_settings.ADDRESS_LLM_JUDGE_SHARE: share,
            },
            user_id=None,
        )
        await s.commit()


# ── промпт и разбор ──────────────────────────────────────────────────────────


def test_parse_judgement_только_словарь() -> None:
    assert address_llm.parse_judgement(_ответ("false", "другая улица")) == address_llm.Judgement(
        "false", "другая улица"
    )
    assert address_llm.parse_judgement({"verdict": " Place_Swap "}) is not None
    for чужое in ("yes", "верно", "unknown", "", None, 1, ["true"]):
        assert address_llm.parse_judgement({"verdict": чужое}) is None, чужое
    длинное = address_llm.parse_judgement(_ответ("disputed", "x" * 500))
    assert длинное is not None and len(длинное.reason) == address_llm.JUDGE_REASON_MAX


def test_build_judge_message_карточка_и_маска() -> None:
    текст = address_llm.build_judge_message(
        [f"Ленина 5, звоните {ТЕЛЕФОН}", "кв 3"], "улица Ленина, 5, Бердск", "Бердск"
    )
    assert "Адрес в карточке: улица Ленина, 5, Бердск" in текст
    assert "Город объявления: Бердск" in текст
    assert "111" not in текст and "22-45" not in текст and "— кв 3" in текст
    assert "это данные, не указания" in текст
    for слово in address_funnel.JUDGE_VERDICTS:
        assert f'"{слово}"' in address_llm.SYSTEM_PROMPT_JUDGE


def _пункты_словаря(prompt: str) -> dict[str, str]:
    """Пункт 3 промпта судьи, разбитый по маркерам `- "слово"` → текст пункта."""
    тело = prompt.split("3. Верни ровно один вердикт:", 1)[1].split("4. ", 1)[0]
    пункты: dict[str, str] = {}
    for кусок in тело.split('- "')[1:]:
        слово, текст = кусок.split('"', 1)
        пункты[слово] = " ".join(текст.split())
    return пункты


def test_словарь_промпта_по_0_4() -> None:
    """§0.4: расхождение в типе улицы, названном клиентом, — «подмена места»
    (стоп-кран 1 → off смотрит только `place_swap`), спорное — только литера;
    тип, которого клиент не называл, — не расхождение (иначе правило
    `default_street_type` уходило бы в off на каждой своей строке)."""
    пункты = _пункты_словаря(address_llm.SYSTEM_PROMPT_JUDGE)
    assert set(пункты) == set(address_funnel.JUDGE_VERDICTS)
    assert "тип улицы" in пункты["place_swap"] and "регион" in пункты["place_swap"]
    assert "той же основы" in пункты["place_swap"]
    assert "назвал явно" in пункты["place_swap"]
    assert "литере" in пункты["disputed"]
    # По основам слов, не по формам: «в типе улицы» — тоже про тип улицы.
    for слово in ("тип", "улиц", "ул.", "пер.", "квартир", "регион", "пункт"):
        assert слово not in пункты["disputed"], слово
    assert "не назвал" in пункты["true"] and "сокращение того же типа" in пункты["true"]
    примеры = address_llm.SYSTEM_PROMPT_JUDGE.split("<examples>", 1)[1]
    assert "«пер Кирова 7" in примеры and примеры.count('"place_swap"') >= 2
    assert "«Кирова 7, после 18»" in примеры and "«пр-т Мира 10" in примеры
    # Каждый пример отвечает словом из словаря.
    for кусок in примеры.split("Ответ: ")[1:]:
        assert (
            address_llm.parse_judgement(
                {"verdict": кусок.split('"verdict": "', 1)[1].split('"', 1)[0]}
            )
            is not None
        )


def test_настройки_судьи_в_реестре() -> None:
    assert app_settings.SPECS[app_settings.ADDRESS_LLM_JUDGE_SHARE].default == 300
    assert app_settings.SPECS[app_settings.ADDRESS_LLM_JUDGE_DAILY_ROWS].default == 80
    assert app_settings.SPECS[app_settings.ADDRESS_LLM_JUDGE_AGREEMENT].default is None
    assert app_settings.SPECS[app_settings.ADDRESS_LLM_JUDGE_CALIBRATED_AT].kind == "ts"
    assert app_settings.judge_agreement_pct(97) == 97
    assert app_settings.judge_agreement_pct(0) == 0
    for мусор in (None, 101, -1, "97", True):
        assert app_settings.judge_agreement_pct(мусор) is None


# ── квота ────────────────────────────────────────────────────────────────────


async def test_квота_запас_читателю(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    """Потолок 900, читатель выел 850 → судье остаётся ноль (запас 50);
    при 849 — идёт. Умолчание потолка 50 → не идёт по построению."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    _судья(monkeypatch)
    await _потолки(db_sessionmaker, limit=900)
    await redis.set(llm_calls_key(), 850)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
    assert итог.outcome == address_judge.OUTCOME_SKIPPED_QUOTA
    assert await address_judge.judge_calls_today(redis) == 0, "место в доле не занималось"
    await redis.set(llm_calls_key(), 849)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
        await s.commit()
    assert итог.outcome == address_judge.OUTCOME_JUDGED
    assert int(await redis.get(llm_calls_key())) == 850, "общий счётчик пополнен судьёй"
    assert await address_judge.judge_calls_today(redis) == 1
    # Умолчание потолка (50) минус запас читателю — судье не остаётся ничего.
    await _потолки(db_sessionmaker, limit=50)
    await redis.delete(llm_calls_key())
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
    assert итог.outcome == address_judge.OUTCOME_SKIPPED_QUOTA


async def test_квота_доля_судьи_атомарна(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    """Доля 1: первый поход занимает место, второй упирается и возвращает
    счётчик (INCR → DECR): доля остаётся ровно 1, а не 2. Две попытки шлюза
    (`attempts=2`) — оба счётчика растут на второй запрос."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    _судья(monkeypatch, attempts=2)
    await _потолки(db_sessionmaker, limit=None, share=2)
    async with db_sessionmaker() as s:
        assert (await address_judge.judge_row(s, redis, сид.row_id, now=NOW)).outcome == "judged"
        await s.commit()
    assert await address_judge.judge_calls_today(redis) == 2, "занято место + вторая попытка"
    assert int(await redis.get(llm_calls_key())) == 2
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
    assert итог.outcome == address_judge.OUTCOME_SKIPPED_QUOTA
    assert await address_judge.judge_calls_today(redis) == 2, "перебор вернул счётчик"
    assert int(await redis.get(llm_calls_key())) == 2, "похода не было"


async def test_отказ_читателей_возвращает_место(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    await _потолки(db_sessionmaker, limit=None)

    async def blocked(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        raise openrouter.OpenRouterError("blocked")  # до `on_request`, как сам клиент

    monkeypatch.setattr(openrouter, "chat_json", blocked)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
    assert (итог.outcome, итог.reason) == (address_judge.OUTCOME_FAILED, "blocked")
    assert await address_judge.judge_calls_today(redis) == 0
    assert await _след(db_sessionmaker, сид.row_id) == СУД_УЛИЦЫ

    async def мусор(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        await kw["on_request"]()
        return {"verdict": "maybe"}, "b/two:free"

    monkeypatch.setattr(openrouter, "chat_json", мусор)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
    assert (итог.outcome, итог.reason) == (address_judge.OUTCOME_FAILED, "bad_verdict")
    assert await address_judge.judge_calls_today(redis) == 1, "поход был — место потрачено"
    assert await _след(db_sessionmaker, сид.row_id) == СУД_УЛИЦЫ


# ── judge_row ────────────────────────────────────────────────────────────────


async def test_judge_row_пишет_след_с_привязкой_к_суду(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст=f"Ленина 5, тел {ТЕЛЕФОН}")
        await s.commit()
    увидел = _судья(monkeypatch, "disputed")
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
        await s.commit()
    assert (итог.outcome, итог.verdict, итог.written, итог.model) == (
        "judged",
        "disputed",
        True,
        "b/two:free",
    )
    assert "Адрес в карточке: улица Ленина, 5, Бердск" in увидел[0]
    assert "Ленина 5" in увидел[0] and "111" not in увидел[0], "речь — с маской телефона"
    след = await _след(db_sessionmaker, сид.row_id)
    assert след is not None and след["rule"] == "street_point", "ключи суда остались"
    judge = след["judge"]
    assert judge["verdict"] == "disputed" and judge["source"] == "judge"
    assert judge["model"] == "b/two:free" and judge["at"] == NOW.isoformat()
    assert judge["checked_at"] == address_funnel.checked_at_iso(ДАВНО)
    assert "reason" not in judge, "слова модели о речи клиента в след не идут"
    assert address_funnel.judge_is_fresh(judge, ДАВНО)
    # Сухой прогон — вердикт есть, следа нет.
    async with db_sessionmaker() as s:
        сид2 = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид2.row_id, dry_run=True, now=NOW)
        await s.commit()
    assert итог.outcome == "judged" and итог.written is False
    assert await _след(db_sessionmaker, сид2.row_id) == СУД_УЛИЦЫ


async def test_judge_row_не_переписывает_сменившийся_суд(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    """Пока модель думала, воркер пересудил строку (новый `geo_checked_at`):
    вердикт — о прежнем решении, в след не ложится."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    await _потолки(db_sessionmaker, limit=None)
    новый_суд = {"rule": "in_named_city", "policy": "approx"}

    async def пересуд(system: str, user: str, **kw: Any) -> tuple[dict[str, Any], str]:
        await kw["on_request"]()
        async with db_sessionmaker() as s:
            await s.execute(
                sa.update(ClientAddressCandidate)
                .where(ClientAddressCandidate.id == сид.row_id)
                .values(geo_checked_at=NOW - timedelta(minutes=1), trace=новый_суд)
            )
            await s.commit()
        return _ответ("false"), "b/two:free"

    monkeypatch.setattr(openrouter, "chat_json", пересуд)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
        await s.commit()
    assert (итог.outcome, итог.verdict, итог.written) == ("judged", "false", False)
    assert await _след(db_sessionmaker, сид.row_id) == новый_суд
    # Строка без `geo_checked_at` (NULL — сброшена `address-recheck` между
    # выборкой и судом) судится, но не пишется: вердикт `checked_at: null`
    # пережил бы пересуд и считался бы новому правилу.
    async with db_sessionmaker() as s:
        сид2 = await _строка(s, account.id, trace=СУД_УЛИЦЫ, checked_at=None)
        await s.commit()
    увидел = _судья(monkeypatch)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, сид2.row_id, now=NOW)
        await s.commit()
    assert (итог.outcome, итог.verdict, итог.written, итог.target, итог.reason) == (
        "judged",
        "true",
        False,
        None,
        "no_checked_at",
    )
    assert len(увидел) == 1, "к модели сходили — вердикт напечатан"
    assert await _след(db_sessionmaker, сид2.row_id) == СУД_УЛИЦЫ
    # Такой вердикт, окажись он в следе, свежим не считается: строка в очереди.
    assert not address_funnel.judge_is_fresh({"verdict": "true", "checked_at": None}, ДАВНО)
    assert address_funnel.judge_is_fresh({"verdict": "true", "checked_at": None}, None)


async def test_judge_row_тень_и_без_речи(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    account = await make_avito_account()
    тень = {"rule": "in_named_city", "status": "exact", "key": "улица Мира, 10, Орск", "km": 1.0}
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace={"shadow": тень}, карточка=False)
        немой = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст=None)
        await s.commit()
    увидел = _судья(monkeypatch, "place_swap")
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(
            s, redis, сид.row_id, target=address_judge.TARGET_SHADOW, now=NOW
        )
        await s.commit()
    assert (итог.outcome, итог.verdict, итог.written, итог.target) == (
        "judged",
        "place_swap",
        True,
        "shadow",
    )
    assert "Адрес в карточке: улица Мира, 10, Орск" in увидел[0], "тень судится по shadow.key"
    след = await _след(db_sessionmaker, сид.row_id)
    assert след is not None and "judge" not in след
    assert след["shadow"]["judge"]["verdict"] == "place_swap"
    assert след["shadow"]["key"] == тень["key"], "тень сохранена целиком"
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, немой.row_id, now=NOW)
    assert итог.outcome == address_judge.OUTCOME_SKIPPED_NO_SPEECH
    assert len(увидел) == 1, "без речи к модели не ходили"
    async with db_sessionmaker() as s:
        итог = await address_judge.judge_row(s, redis, uuid.uuid4(), now=NOW)
    assert итог.outcome == address_judge.OUTCOME_GONE


# ── pick_rows / sample_for_grade ─────────────────────────────────────────────


async def test_pick_rows_пересуженная_снова_в_очереди(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    account = await make_avito_account()
    свежий = {"verdict": "true", "checked_at": address_funnel.checked_at_iso(ДАВНО)}
    старый = {
        "verdict": "true",
        "checked_at": address_funnel.checked_at_iso(NOW - timedelta(days=30)),
    }
    async with db_sessionmaker() as s:
        неосуждённая = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        пересуженная = await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": старый})
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": свежий})
        # Вердикт без привязки к суду — не свежий: строка снова в очереди.
        без_привязки = await _строка(
            s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "true", "checked_at": None}}
        )
        await _строка(s, account.id, trace={"parse_form": "x"})  # без правила — не решение
        await _строка(s, account.id, trace=СУД_УЛИЦЫ, checked_at=NOW - timedelta(weeks=9))
        await s.commit()
    async with db_sessionmaker() as s:
        picks = await address_judge.pick_rows(s, now=NOW, limit=10)
    assert {p.row_id for p in picks} == {
        неосуждённая.row_id,
        пересуженная.row_id,
        без_привязки.row_id,
    }
    assert all(p.rule == "street_point" and p.target == "judge" for p in picks)


async def test_pick_rows_не_берёт_решённые_человеком(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        своя = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await _строка(s, account.id, trace=СУД_УЛИЦЫ, resolved_by_id=uuid.uuid4())
        await s.commit()
    async with db_sessionmaker() as s:
        picks = await address_judge.pick_rows(s, now=NOW, limit=10)
    assert [p.row_id for p in picks] == [своя.row_id]


async def test_pick_rows_по_кругу_правил_и_тень(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    """Три решения `street_point`, два `area_point`, одна тень `in_named_city`:
    при `limit=4` каждому правилу по одному, потом второй круг; `per_rule=1`
    режет; `rule=` — только оно; тень с осуждённым боевым решением идёт в
    очередь как `shadow`."""
    account = await make_avito_account()
    тень = {"rule": "in_named_city", "status": "exact", "key": "улица Мира, 10, Орск"}
    свежий = {"verdict": "true", "checked_at": address_funnel.checked_at_iso(ДАВНО)}
    async with db_sessionmaker() as s:
        for i in range(3):
            await _строка(s, account.id, trace=СУД_УЛИЦЫ, checked_at=ДАВНО + timedelta(hours=i))
        for i in range(2):
            await _строка(
                s, account.id, trace={"rule": "area_point"}, checked_at=ДАВНО + timedelta(days=i)
            )
        теневая = await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": свежий, "shadow": тень})
        await s.commit()
    async with db_sessionmaker() as s:
        четыре = await address_judge.pick_rows(s, now=NOW, limit=4)
        по_одному = await address_judge.pick_rows(s, now=NOW, per_rule=1, limit=10)
        только = await address_judge.pick_rows(s, now=NOW, limit=10, rule="area_point")
    assert [p.rule for p in четыре] == ["area_point", "in_named_city", "street_point", "area_point"]
    assert [p.rule for p in по_одному] == ["area_point", "in_named_city", "street_point"]
    assert [p.rule for p in только] == ["area_point", "area_point"]
    теневой = next(p for p in четыре if p.rule == "in_named_city")
    assert (теневой.row_id, теневой.target) == (теневая.row_id, "shadow")


async def test_pick_rows_rule_берёт_тень_поверх_неосуждённого_боя(
    db_sessionmaker, make_avito_account
) -> None:  # noqa: ANN001
    """Тень нового правила лежит поверх чужого боя почти всегда: `--rule R`
    судит её сразу, не дожидаясь ночного суда над боем; ночная очередь без
    `rule` по-прежнему берёт одну цель — бой; тень без ключа и осуждённая
    тень в очередь `rule=` не идут."""
    account = await make_avito_account()
    тень = {"rule": "in_named_city", "status": "exact", "key": "улица Мира, 10, Орск"}
    свежий = {"verdict": "true", "checked_at": address_funnel.checked_at_iso(ДАВНО)}
    async with db_sessionmaker() as s:
        поверх_боя = await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "shadow": тень})
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "shadow": {**тень, "key": None}})
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "shadow": {**тень, "judge": свежий}})
        await s.commit()
    async with db_sessionmaker() as s:
        тени = await address_judge.pick_rows(s, now=NOW, limit=10, rule="in_named_city")
        бои = await address_judge.pick_rows(s, now=NOW, limit=10, rule="street_point")
        ночная = await address_judge.pick_rows(s, now=NOW, limit=10)
    assert [(p.row_id, p.rule, p.target) for p in тени] == [
        (поверх_боя.row_id, "in_named_city", "shadow")
    ]
    assert len(бои) == 3 and all(p.target == "judge" for p in бои)
    assert all(p.rule == "street_point" and p.target == "judge" for p in ночная)
    assert len(ночная) == 3, "без rule — одна цель на строку, бой прежде тени"
    assert address_judge.rule_target({**СУД_УЛИЦЫ, "shadow": тень}, ДАВНО, "other") is None


async def test_sample_for_grade_по_степени(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    account = await make_avito_account()
    свежий = {"verdict": "true", "checked_at": address_funnel.checked_at_iso(ДАВНО)}
    async with db_sessionmaker() as s:
        точная = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        приблизительная = await _строка(
            s, account.id, trace=СУД_УЛИЦЫ, geo_provider="dadata~approx"
        )
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": свежий})  # уже осуждена
        await _строка(s, account.id, trace=СУД_УЛИЦЫ, resolved_by_id=uuid.uuid4())  # кнопка
        await _строка(s, account.id, trace=СУД_УЛИЦЫ, карточка=False)  # не источник карточки
        # Геоточка Авито: точку выбрал клиент, речи у неё нет — по репликам
        # не проверить, судья видел бы окно без адреса (проверка правок 21.09).
        геоточка = await _строка(s, account.id, trace=None, geo_provider=geocode.GEOPOINT_PROVIDER)
        await s.commit()
    async with db_sessionmaker() as s:
        exact = await address_judge.sample_for_grade(s, grade="exact", days=30, limit=10, now=NOW)
        сотня = await address_judge.calibration_rows(s, limit=100, now=NOW)
        approx = await address_judge.sample_for_grade(s, grade="approx", days=30, limit=10, now=NOW)
        with pytest.raises(ValueError):
            await address_judge.sample_for_grade(s, grade="best", days=30, limit=10, now=NOW)
    assert [p.row_id for p in exact] == [точная.row_id]
    assert [p.row_id for p in approx] == [приблизительная.row_id]
    assert all(r.row_id != геоточка.row_id for r in сотня), "геоточка не идёт и на разметку"


# ── воронка и лестница ───────────────────────────────────────────────────────


async def test_measure_rules_не_считает_чужой_checked_at(
    db_sessionmaker, make_avito_account
) -> None:  # noqa: ANN001
    account = await make_avito_account()
    свой = address_funnel.checked_at_iso(ДАВНО)
    чужой = address_funnel.checked_at_iso(NOW - timedelta(days=30))
    async with db_sessionmaker() as s:
        # Без `checked_at` — не привязан к суду: пережил бы пересуд, не в счёт.
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "false"}})
        await _строка(
            s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "false", "checked_at": свой}}
        )
        await _строка(
            s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "false", "checked_at": чужой}}
        )
        await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "judge": {"verdict": "yes"}})
        await s.commit()
    async with db_sessionmaker() as s:
        c = (await address_funnel.measure_rules(s, until=NOW))["street_point"]
    assert (c.n, c.judged, c.false) == (4, 1, 1)


async def test_judge_calibrated_по_настройке(db_sessionmaker) -> None:  # noqa: ANN001
    async with db_sessionmaker() as s:
        assert await rule_policy_weekly.judge_calibrated(s) is False
        for значение, ждём in ((94, False), (95, True), (100, True), (0, False)):
            await app_settings.set_many(
                s, {app_settings.ADDRESS_LLM_JUDGE_AGREEMENT: значение}, user_id=None
            )
            await s.commit()
            assert await rule_policy_weekly.judge_calibrated(s) is ждём, значение
    c = RuleCounts(n=400, judged=200, first_at=NOW - timedelta(days=50))
    assert rule_policy_weekly.decide("street_point", "approx", c, now=NOW) is None
    assert (
        rule_policy_weekly.decide("street_point", "approx", c, now=NOW, judge_ok=True) is not None
    )


# ── задача планировщика ──────────────────────────────────────────────────────


def test_задача_в_планировщике() -> None:
    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler.main import build_scheduler

    задача = build_scheduler().get_job(job.JOB_ID)
    assert задача is not None and задача.func is job.judge_daily
    assert job.JOB_ID == "address_judge_daily"
    trigger = задача.trigger
    assert isinstance(trigger, CronTrigger)
    assert (str(trigger.fields[5]), str(trigger.fields[6])) == ("0", "40")
    assert str(trigger.fields[4]) == "*", "ежедневно, не по понедельникам"
    assert job.DEFAULTS["max_instances"] == 1 and job.DEFAULTS["misfire_grace_time"] >= 3600


@pytest.fixture
def оснастка_задачи(monkeypatch, db_sessionmaker, redis):  # noqa: ANN001
    monkeypatch.setattr(job.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(job.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(job, "PAUSE_SEC", 0)


async def test_judge_daily_судит_очередь_и_стоит_на_квоте(
    db_sessionmaker, make_avito_account, redis, monkeypatch, оснастка_задачи
) -> None:  # noqa: ANN001
    """Три строки, доля 2: две осуждены и записаны (transaction на строку —
    проверяется новой сессией), третья — `skipped_quota`, прогон остановлен."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сиды = [await _строка(s, account.id, trace=СУД_УЛИЦЫ) for _ in range(3)]
        await app_settings.set_many(
            s, {app_settings.ADDRESS_LLM_JUDGE_DAILY_ROWS: 10}, user_id=None
        )
        await s.commit()
    _судья(monkeypatch, "false")
    await _потолки(db_sessionmaker, limit=None, share=2)
    прогон = await job.judge_daily(now=NOW)
    assert (прогон.picked, прогон.judged, прогон.skipped_quota, прогон.failed) == (3, 2, 1, 0)
    следы = [await _след(db_sessionmaker, с.row_id) for с in сиды]
    осуждены = [сл for сл in следы if сл and "judge" in сл]
    assert len(осуждены) == 2 and all(сл["judge"]["verdict"] == "false" for сл in осуждены)  # type: ignore[index]
    async with db_sessionmaker() as s:
        c = (await address_funnel.measure_rules(s, until=NOW))["street_point"]
    assert (c.n, c.judged, c.false) == (3, 2, 2)
    # Второй прогон: осуждённые в очередь не попадают.
    await _потолки(db_sessionmaker, limit=None, share=300)
    прогон = await job.judge_daily(now=NOW)
    assert (прогон.picked, прогон.judged) == (1, 1)


# ── CLI ──────────────────────────────────────────────────────────────────────


async def test_cli_режимы_взаимоисключающи(db_sessionmaker) -> None:  # noqa: ANN001
    from app.cli import run_address_judge

    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(s, rule="street_point", grade="exact")
        assert exc.value.exit_code == 2
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(s)
        assert exc.value.exit_code == 2
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(s, sample=5)  # без --out
        assert exc.value.exit_code == 2


async def test_cli_grade_печатает_false_pct(
    db_sessionmaker, make_avito_account, redis, monkeypatch, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        for _ in range(2):
            await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    _судья(monkeypatch, "false")
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(
            s,
            grade="exact",
            redis=redis,
            session_factory=db_sessionmaker,
            pause=0,
            now=NOW,
        )
    вывод = capsys.readouterr().out
    assert счёт["judged"] == 2 and счёт["false_pct"] == 100.0
    assert "judged=2 true=0 false=2" in вывод and "false_pct=100.0 (2 из 2 exact)" in вывод
    assert "тревога: ложных среди exact 100.0 % > 2.0 %" in вывод
    assert "judge_used=2/share=300 llm_today=2/limit=нет" in вывод


async def test_cli_sample_пишет_tsv_масками(
    db_sessionmaker, make_avito_account, tmp_path, capsys
) -> None:  # noqa: ANN001
    from app.cli import run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст=f"Ленина 5, тел {ТЕЛЕФОН}")
        await s.commit()
    файл = tmp_path / "judge.tsv"
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(s, sample=10, out=str(файл), now=NOW)
    assert счёт == {"sample": 1}
    строки = файл.read_text(encoding="utf-8").splitlines()
    assert строки[0] == "id\tреплики\tкарточка\thuman"
    поля = строки[1].split("\t")
    assert поля[0] == str(сид.row_id) and поля[3] == ""
    assert "Ленина 5" in поля[1] and "111" not in поля[1], "речь — маской"
    assert поля[2] == "улица Ленина, 5, Бердск"
    assert "выгружено 1 строк" in capsys.readouterr().out


async def test_cli_calibrate_пишет_настройку(
    db_sessionmaker, make_avito_account, redis, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    """Четыре размеченные строки, судья всем говорит `true`: согласие 3/4 = 75,
    матрица расхождений; четырёх строк для калибровки мало (сотня), поэтому
    настройка и журнал с `source=judge_calibration` пишутся только под
    `--force` — с пометкой `forced`; после этого `address-funnel` печатает
    `judge_agreement=75 %`, а лестница — не откалиброван (< 95)."""
    from app.cli import run_address_funnel, run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сиды = [await _строка(s, account.id, trace=СУД_УЛИЦЫ) for _ in range(4)]
        await s.commit()
    метки = ["true", "true", "true", "false"]
    файл = tmp_path / "labeled.tsv"
    файл.write_text(
        "id\tреплики\tкарточка\thuman\n"
        + "\n".join(
            f"{с.row_id}\tЛенина 5\tулица Ленина, 5\t{м}" for с, м in zip(сиды, метки, strict=True)
        )
        + f"\n{uuid.uuid4()}\tмусор\t—\t\n",
        encoding="utf-8",
    )
    _судья(monkeypatch, "true")
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(
            s,
            calibrate=str(файл),
            redis=redis,
            session_factory=db_sessionmaker,
            pause=0,
            now=NOW,
            force=True,
        )
    вывод = capsys.readouterr().out
    assert счёт["agreement"] == [3, 4] and счёт["agreement_pct"] == 75
    assert "agreement=3/4 (75 %)" in вывод and "расхождение human=false judge=true: 1" in вывод
    assert "калибровка на 4 строках — программа велит 100" in вывод
    assert "записано по 4 строкам принудительно" in вывод
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT) == 75
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_JUDGE_CALIBRATED_AT) == int(
            NOW.timestamp()
        )
        assert await rule_policy_weekly.judge_calibrated(s) is False
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .all()
        )
    assert [з.details["source"] for з in журнал] == ["judge_calibration"]
    assert журнал[0].user_id is None and журнал[0].details["after"]["judge_agreement"] == 75
    assert журнал[0].details["forced"] is True and журнал[0].details["agreement"] == [3, 4]
    for с in сиды:
        след = await _след(db_sessionmaker, с.row_id)
        assert след is not None and след["judge"]["verdict"] == "true", "калибровка пишет след"
    async with db_sessionmaker() as s:
        await run_address_funnel(s, week="2026-09-07", store=False, rules=True)
    assert "judge_agreement=75 % (2026-09-21)" in capsys.readouterr().out


async def test_cli_calibrate_обрыв_не_пишет_и_добирает_из_следа(
    db_sessionmaker, make_avito_account, redis, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    """Шесть размеченных, доля судьи 1: осуждена одна, вторая — `skipped_quota`,
    прогон оборван; согласие 1/1 = 100 %, но настройка НЕ пишется (ворота
    лестницы не открываются с одной строки), журнала нет, выход 1. Повтор тем
    же файлом с квотой: первая строка берётся из следа без похода
    (`from_trace=1`, к модели — пять раз), сотни всё равно нет → снова выход 1
    и настройка пуста."""
    from app.cli import run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сиды = [await _строка(s, account.id, trace=СУД_УЛИЦЫ) for _ in range(6)]
        await s.commit()
    файл = tmp_path / "labeled.tsv"
    файл.write_text(
        "id\tреплики\tкарточка\thuman\n"
        + "\n".join(f"{с.row_id}\tЛенина 5\tулица Ленина, 5\ttrue" for с in сиды),
        encoding="utf-8",
    )
    увидел = _судья(monkeypatch, "true")
    await _потолки(db_sessionmaker, limit=None, share=1)
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(
                s,
                calibrate=str(файл),
                redis=redis,
                session_factory=db_sessionmaker,
                pause=0,
                now=NOW,
            )
    assert exc.value.exit_code == 1
    вывод = capsys.readouterr().out
    assert "agreement=1/1 (100 %)" in вывод
    assert "калибровка на 1 строках — программа велит 100 (skipped_quota=1" in вывод
    assert "настройка не записана" in вывод and len(увидел) == 1
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT) is None
        assert await rule_policy_weekly.judge_calibrated(s) is False
        assert (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "settings.address_detect_changed")
            )
        ).scalar_one() == 0
    # Повтор: осуждённая строка — из следа, остальные пять — к модели.
    await _потолки(db_sessionmaker, limit=None, share=300)
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(
                s,
                calibrate=str(файл),
                redis=redis,
                session_factory=db_sessionmaker,
                pause=0,
                now=NOW,
            )
    assert exc.value.exit_code == 1
    вывод = capsys.readouterr().out
    assert "agreement=6/6 (100 %)" in вывод and "from_trace=1" in вывод
    assert "judged=5" in вывод and len(увидел) == 6, "к модели сходили только за пятью"
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT) is None
    # Сухой прогон при недоборе — без выхода 1 и без записи.
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(
            s,
            calibrate=str(файл),
            redis=redis,
            session_factory=db_sessionmaker,
            pause=0,
            now=NOW,
            dry_run=True,
        )
    assert счёт["agreement"] == [6, 6] and счёт["from_trace"] == 6


async def test_cli_calibrate_порция_и_отпечаток_промпта(
    db_sessionmaker, make_avito_account, redis, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    """Проверка правок 21.09. (1) Порция ≤ `--max` и у калибровки: пять строк,
    `max_rows=2` → два похода, остаток — следующим запуском из следа + ещё
    два. (2) Вердикт в следе от ДРУГОГО промпта не берётся из следа: после
    правки словаря калибровка ходит заново, `from_trace=0`. Диверсии: снять
    `break` по порции в режиме calibrate; убрать сверку `prompt` в
    `saved_verdicts`."""
    from app.cli import run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сиды = [await _строка(s, account.id, trace=СУД_УЛИЦЫ) for _ in range(5)]
        await s.commit()
    файл = tmp_path / "labeled.tsv"
    файл.write_text(
        "id\tреплики\tкарточка\thuman\n"
        + "\n".join(f"{с.row_id}\tЛенина 5\tулица Ленина, 5\ttrue" for с in сиды),
        encoding="utf-8",
    )
    увидел = _судья(monkeypatch, "true")
    await _потолки(db_sessionmaker, limit=None, share=300)

    async def прогон() -> str:
        async with db_sessionmaker() as s:
            with pytest.raises(typer.Exit):
                await run_address_judge(
                    s,
                    calibrate=str(файл),
                    max_rows=2,
                    redis=redis,
                    session_factory=db_sessionmaker,
                    pause=0,
                    now=NOW,
                )
        return capsys.readouterr().out

    вывод = await прогон()
    assert len(увидел) == 2 and "порция 2 исчерпана" in вывод
    вывод = await прогон()
    assert len(увидел) == 4 and "from_trace=2" in вывод
    # Смена промпта — отпечаток в следе чужой, к модели идут заново.
    monkeypatch.setattr(address_llm, "SYSTEM_PROMPT_JUDGE", address_llm.SYSTEM_PROMPT_JUDGE + "\n")
    вывод = await прогон()
    assert "from_trace=0" in вывод and len(увидел) == 6
    след = await _след(db_sessionmaker, сиды[0].row_id)
    assert след is not None and след["judge"]["prompt"] == address_judge.prompt_fingerprint()


async def test_cli_calibrate_сотня_пишет_без_force(
    db_sessionmaker, make_avito_account, redis, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    """101 размеченная строка, одна `gone` (строки нет): отвеченных 100 —
    настройка пишется без `--force` и без пометки `forced`. Порция ≤ 60 на
    запуск (проверка правок 21.09): первый запуск судит 60 и выходит с кодом 1,
    второй добирает 60 из следа + 40 к модели и пишет настройку."""
    from app.cli import run_address_judge

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сиды = [
            await _строка(s, account.id, trace=СУД_УЛИЦЫ)
            for _ in range(address_judge.JUDGE_CALIBRATION_N)
        ]
        await s.commit()
    файл = tmp_path / "labeled.tsv"
    файл.write_text(
        "id\tреплики\tкарточка\thuman\n"
        + "\n".join(f"{с.row_id}\tЛенина 5\tулица Ленина, 5\ttrue" for с in сиды)
        + f"\n{uuid.uuid4()}\tЛенина 5\tулица Ленина, 5\ttrue",
        encoding="utf-8",
    )
    увидел = _судья(monkeypatch, "true")
    await _потолки(db_sessionmaker, limit=None, share=None)
    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_judge(
                s,
                calibrate=str(файл),
                redis=redis,
                session_factory=db_sessionmaker,
                pause=0,
                now=NOW,
            )
    assert exc.value.exit_code == 1 and len(увидел) == 60
    assert "порция 60 исчерпана" in capsys.readouterr().out
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(
            s,
            calibrate=str(файл),
            redis=redis,
            session_factory=db_sessionmaker,
            pause=0,
            now=NOW,
        )
    вывод = capsys.readouterr().out
    assert счёт["agreement"] == [100, 100] and счёт["gone"] == 1
    assert счёт["from_trace"] == 60 and len(увидел) == 100
    assert "настройка judge_agreement: None → 100" in вывод and "принудительно" not in вывод
    async with db_sessionmaker() as s:
        assert await app_settings.get(s, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT) == 100
        assert await rule_policy_weekly.judge_calibrated(s) is True
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "settings.address_detect_changed")
                )
            )
            .scalars()
            .all()
        )
    assert len(журнал) == 1 and "forced" not in журнал[0].details


async def test_cli_in_пишет_только_под_своё_правило(
    db_sessionmaker, make_avito_account, redis, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    """JSON сухого прогона (форма `address-rule-dry-run --out`): вердикт
    пишется, только когда правило файла И строка карты из файла совпали с
    решением строки — боевое (`geo_formatted`) → `trace.judge`, тень
    (`shadow.key`) → `trace.shadow.judge`; правило то же, но карта другая
    (правило поправили, карты дрейфнули, строку пересудили) → вердикт о чужой
    карточке напечатан, след не тронут, `key_mismatch`; правило строку не
    решало → то же; `trace.rule == shadow.rule`, ключ совпал только с тенью →
    запись в тень; запись без `key` пропускается. Судится всегда строка карты
    из файла (пробелы в ключе схлопываются)."""
    import json

    from app.cli import run_address_judge

    account = await make_avito_account()
    ключ = "улица Мира, 10, Орск"
    тень = {"rule": "in_named_city", "status": "exact", "key": ключ}
    async with db_sessionmaker() as s:
        боевая = await _строка(s, account.id, trace={"rule": "in_named_city"}, geo_formatted=ключ)
        другая_карта = await _строка(s, account.id, trace={"rule": "in_named_city"})
        теневая = await _строка(s, account.id, trace={**СУД_УЛИЦЫ, "shadow": тень})
        другая_тень = await _строка(
            s, account.id, trace={**СУД_УЛИЦЫ, "shadow": {**тень, "key": "улица Мира, 12, Орск"}}
        )
        одно_имя = await _строка(s, account.id, trace={"rule": "in_named_city", "shadow": тень})
        чужая = await _строка(s, account.id, trace=СУД_УЛИЦЫ)
        await s.commit()
    записи = [
        {
            "candidate_id": str(с.row_id),
            "rule": "in_named_city",
            "key": "улица  Мира, 10,  Орск",
            "status": "exact",
            "km": 1.0,
            "would_autofill": True,
            "prev_status": "exact",
        }
        for с in (боевая, другая_карта, теневая, другая_тень, одно_имя, чужая)
    ] + [{"candidate_id": str(uuid.uuid4()), "rule": None, "key": None, "status": "not_found"}]
    файл = tmp_path / "dry.json"
    файл.write_text(json.dumps(записи, ensure_ascii=False), encoding="utf-8")
    увидел = _судья(monkeypatch, "true")
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        счёт = await run_address_judge(
            s,
            in_file=str(файл),
            redis=redis,
            session_factory=db_sessionmaker,
            pause=0,
            now=NOW,
        )
    вывод = capsys.readouterr().out
    assert счёт["judged"] == 6 and счёт["key_mismatch"] == 3 and len(увидел) == 6
    assert "judged=6 true=6" in вывод and "key_mismatch=3" in вывод
    assert all(f"Адрес в карточке: {ключ}" in u for u in увидел)
    боевой_след = await _след(db_sessionmaker, боевая.row_id)
    assert боевой_след is not None and боевой_след["judge"]["verdict"] == "true"
    assert await _след(db_sessionmaker, другая_карта.row_id) == {"rule": "in_named_city"}
    теневой_след = await _след(db_sessionmaker, теневая.row_id)
    assert теневой_след is not None and "judge" not in теневой_след
    assert теневой_след["shadow"]["judge"]["verdict"] == "true"
    другая_тень_след = await _след(db_sessionmaker, другая_тень.row_id)
    assert другая_тень_след is not None and "judge" not in другая_тень_след["shadow"]
    одно_имя_след = await _след(db_sessionmaker, одно_имя.row_id)
    assert одно_имя_след is not None and "judge" not in одно_имя_след
    assert одно_имя_след["shadow"]["judge"]["verdict"] == "true", "ключ совпал только с тенью"
    assert await _след(db_sessionmaker, чужая.row_id) == СУД_УЛИЦЫ
    # Несовпавшие строки для ночной очереди по-прежнему не осуждены.
    async with db_sessionmaker() as s:
        picks = await address_judge.pick_rows(s, now=NOW, limit=10)
    assert {другая_карта.row_id, чужая.row_id} <= {p.row_id for p in picks}
    assert боевая.row_id not in {p.row_id for p in picks}


async def _реплики(
    s: Any, conversation_id: uuid.UUID, реплики: list[tuple[str, timedelta]]
) -> None:
    """Входящие реплики диалога со сдвигом от `ДАВНО` (реплика-источник)."""
    for текст, сдвиг in реплики:
        s.add(
            Message(
                conversation_id=conversation_id,
                external_message_id=uuid.uuid4().hex[:12],
                direction="in",
                sender_type="client",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=ДАВНО + сдвиг,
            )
        )


def _речь_из_входа(вход: str) -> list[str]:
    return [с[2:] for с in вход.splitlines() if с.startswith("— ")]


async def test_речь_судьи_окно_после_источника(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    """Окно вокруг реплики-источника («Ленина 5», `message_at`): семь реплик
    до неё → в окне четыре последних до плюс источник; девять коротких за
    сутки после → в окне только три первых («это в Искитиме» среди них), а
    источник не вытеснен «последними восемью»; реплика через двое суток не
    видна. Длинная реплика после (2 200 знаков) обрезана до 300 — по знакам
    источник тоже остаётся."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст="Ленина 5")
        row = await s.get(ClientAddressCandidate, сид.row_id)
        assert row is not None
        row.message_at = ДАВНО
        await _реплики(
            s,
            сид.conversation_id,
            [(f"реплика до {i}", -timedelta(hours=8 - i)) for i in range(1, 8)]
            + [("это в Искитиме", timedelta(minutes=5))]
            + [(f"реплика после {i}", timedelta(hours=i)) for i in range(1, 9)]
            + [("спасибо, приехали", timedelta(days=2))],
        )
        await s.commit()
    увидел = _судья(monkeypatch)
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
        await s.commit()
    речь = _речь_из_входа(увидел[0])
    assert речь == [
        "реплика до 4",
        "реплика до 5",
        "реплика до 6",
        "реплика до 7",
        "Ленина 5",
        "это в Искитиме",
        "реплика после 1",
        "реплика после 2",
    ]
    assert "спасибо, приехали" not in увидел[0]
    # Одна длинная реплика после источника: обрезана, источник на месте.
    async with db_sessionmaker() as s:
        сид2 = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст="Ленина 5")
        row = await s.get(ClientAddressCandidate, сид2.row_id)
        assert row is not None
        row.message_at = ДАВНО
        await _реплики(s, сид2.conversation_id, [("ж" * 2200, timedelta(hours=1))])
        await s.commit()
    async with db_sessionmaker() as s:
        await address_judge.judge_row(s, redis, сид2.row_id, now=NOW)
        await s.commit()
    речь = _речь_из_входа(увидел[1])
    assert речь[0] == "Ленина 5" and len(речь) == 2
    assert len(речь[1]) == address_judge.ДЛИНА_РЕПЛИКИ_ПОСЛЕ


async def test_речь_судьи_якорь_по_message_id(
    db_sessionmaker, make_avito_account, redis, monkeypatch
) -> None:  # noqa: ANN001
    """Строка без `message_at`, но с `message_id`: якорь — время реплики
    источника, окно то же; без обоих — вся речь (последние восемь)."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст="Ленина 5")
        row = await s.get(ClientAddressCandidate, сид.row_id)
        assert row is not None
        row.message_at = None
        row.message_id = (
            await s.execute(
                sa.select(Message.id).where(Message.conversation_id == сид.conversation_id)
            )
        ).scalar_one()
        await _реплики(
            s,
            сид.conversation_id,
            [(f"реплика после {i}", timedelta(hours=i)) for i in range(1, 10)],
        )
        без_якоря = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст="Ленина 5")
        row = await s.get(ClientAddressCandidate, без_якоря.row_id)
        assert row is not None
        row.message_at = None
        await _реплики(
            s,
            без_якоря.conversation_id,
            [(f"реплика после {i}", timedelta(hours=i)) for i in range(1, 10)],
        )
        await s.commit()
    увидел = _судья(monkeypatch)
    await _потолки(db_sessionmaker, limit=None)
    async with db_sessionmaker() as s:
        await address_judge.judge_row(s, redis, сид.row_id, now=NOW)
        await s.commit()
        await address_judge.judge_row(s, redis, без_якоря.row_id, now=NOW)
        await s.commit()
    assert _речь_из_входа(увидел[0]) == [
        "Ленина 5",
        "реплика после 1",
        "реплика после 2",
        "реплика после 3",
    ]
    assert _речь_из_входа(увидел[1]) == [f"реплика после {i}" for i in range(2, 10)]


async def test_калибровка_видит_то_же_окно(db_sessionmaker, make_avito_account) -> None:  # noqa: ANN001
    """`calibration_rows` строит вход тем же окном, что судья: девять реплик
    после источника не вытесняют «Ленина 5» из TSV на разметку."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        сид = await _строка(s, account.id, trace=СУД_УЛИЦЫ, текст="Ленина 5")
        row = await s.get(ClientAddressCandidate, сид.row_id)
        assert row is not None
        row.message_at = ДАВНО
        await _реплики(
            s,
            сид.conversation_id,
            [(f"реплика после {i}", timedelta(hours=i)) for i in range(1, 10)],
        )
        await s.commit()
    async with db_sessionmaker() as s:
        строки = await address_judge.calibration_rows(s, limit=10, now=NOW)
    assert [с.row_id for с in строки] == [сид.row_id]
    assert строки[0].speech == (
        "Ленина 5",
        "реплика после 1",
        "реплика после 2",
        "реплика после 3",
    )
