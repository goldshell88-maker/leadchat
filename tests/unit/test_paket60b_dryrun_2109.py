"""Пакет 6.0б, I-3 (21.09): сухой суд карты — `dry_run` воркера и `address-rule-dry-run`.

Программа «автоматически и точно» §1.2 (I-3), контракт 6.0б §A. Один суд на два
пути (класс `dva-puti-raznyi-schet`, М-5): сухой прогон идёт тем же телом
`workers/geocode.geocode_candidate`, что и бой, с флагом `dry_run=True`:

* судятся и решённые строки (`exact`, отказы) — ранний выход `already` не
  срабатывает; запись вердикта (`_пометить`), повтор (`enqueue_geocode`), кадр
  (`_известить`), автозапись (`enqueue_autofill`), модель (`enqueue_llm_read`),
  стирание устаревшего `geo_prev`, тревоги потолка и блокировки — НЕ
  выполняются; строка в базе не меняется ни одним полем;
* остаются походы к картам под счётчиками (сухой ≠ бесплатный, М-6);
* `policy=` — политика словарём вместо чтения `address_geo.rule_policy`
  (настройка тогда не читается вовсе; без `policy` — ровно одно чтение, сторож
  6.0а «политика читается один раз на задачу» остаётся зелёным);
* итог — в копилку `_СУХОЙ_СУД` (`DryVerdict`), наружу — через `dry_judge`;
* решённая строка судится СО СВОИМИ уликами как «прежними» (снимок в памяти,
  ревью 21.09 #3): при неполном наборе карт прежний вердикт удерживается, как
  в боевом пересуде; при удержании копилка несёт правило прежнего суда, а не
  отвергнутого решения (#7), и флаги `kept`/`missing`;
* CLI `address-rule-dry-run`: выборка `house` A/B за N дней без `rejected`,
  умолчание статусов — отказы + неспокойные + `exact` (строки тени правил на
  `elsewhere`/`ambiguous` видны, #1), строки правила (по следу или тени)
  первыми, политика = боевая настройка + `ИМЯ=exact` + `--policy`, счётчики и
  JSON для судьи; свои походы DaData считаются копилкой и упираются в
  `--dadata-budget` (#5); несудимые исходы — поимённо (#6); мусор в опциях → 2.

ДИВЕРСИИ (каждая обязана краснеть; прогнаны 21.09 «правка → тест → откат по
хешу»):
D1 снять `not dry_run and` у раннего выхода `already` в `geocode_candidate` →
   `test_сухой_суд_судит_решённую_строку` (вернётся `already`, копилка пуста);
D2 убрать `if dry_run: return …` перед `_пометить` в хвосте дома →
   `test_сухой_суд_дома_не_пишет_и_не_ставит_задач` (строка изменится, шпионы
   насчитают вызовы);
D3 читать настройку всегда (`политика = parse_rule_policy(...)`, `policy`
   игнорировать) → `test_policy_словарём_меняет_итог_и_настройка_не_читается`;
D4 снять `if not dry_run:` вокруг `UPDATE geo_prev=NULL` →
   `test_устаревший_geo_prev_не_стирается_в_сухом`;
D5 снять `if dry_run:` у отказа карты (`blocked`/`error`) →
   `test_сухой_отказ_карты_без_записи_и_счётчика_бана` (строка `blocked`,
   ключ `geo:blocked:nominatim` появится);
D6 убрать `if dry_run: return …` в `_geocode_place` →
   `test_сухой_суд_места_не_пишет`;
D7 CLI: сортировать только по `detected_at` (без `своё`) →
   `test_cli_строки_правила_первыми_и_json` (порядок JSON);
D8 CLI: собирать политику без `rule: exact` (`{**боевая, **из_опций}`) →
   `test_cli_правило_судится_как_exact_поверх_тени` (при `suburb=shadow` в
   настройке решений правилом 0);
D9 CLI: снять `a.status != CANDIDATE_REJECTED` → `test_cli_строки_правила_первыми_и_json`
   (`строк` станет 3);
D10 CLI: умолчание `--statuses` вернуть к `REFUSAL_STATUSES | {exact}` →
   `test_cli_строка_тени_правила_видна_без_statuses` (`строк` станет 0);
D11 воркер: убрать синтез снимка `прежнее = снимок_улик(row, reason="dry_run")` →
   `test_сухой_суд_решённой_строки_держит_улики_как_пересуд` (`not_found`
   вместо удержанного `exact`, `kept` False);
D12 воркер: в копилке при удержании брать `rule` из `решение` без условия →
   `test_удержание_не_приписывает_правило_чужому_ключу` (`rule` станет
   `street_point` у ключа прежнего суда — краснеет по `rule`, не AttributeError);
D13 CLI: снять проверку бюджета перед строкой →
   `test_cli_бюджет_dadata_останавливает_прогон` (судимо 3 вместо 1);
D14 CLI: вернуть `if итог is None: continue` без счёта →
   `test_cli_несудимые_поимённо` (`пропущено_disabled`/`пропущено_paused` = 0).

Адреса — стенды 18–20.09 (Орск/Заречный, Самара/Смышляевка); ПД нет.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
import typer

from app.integrations import gateway
from app.models import ClientAddressCandidate
from app.models.client import CANDIDATE_REJECTED
from app.services import app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as самара
from tests.unit import test_geo_bez_api_1309 as без_api
from tests.unit import test_paket5_2009 as п5
from tests.unit.test_geo_1809 import САМАРА, СМЫШЛЯЕВКА, _разобрать, ctx

pytestmark = pytest.mark.anyio

# Фикстуры соседних стендов — присваиванием, не импортом имени (ruff F811).
dadata_отвечает = самара.dadata_отвечает
osm_пусто = самара.osm_пусто
точка_города = самара.точка_города
dadata_пусто = п5.dadata_пусто
яндекс = п5.яндекс

#: Поля строки, которые пишет суд карты: сухой прогон не трогает ни одно.
_ПОЛЯ_СУДА = (
    "geo_status",
    "geo_formatted",
    "geo_lat",
    "geo_lon",
    "geo_variants",
    "geo_provider",
    "geo_checked_at",
    "geo_attempts",
    "geo_verdict_version",
    "geo_without_dadata",
    "geo_prev",
    "trace",
    "office",
    "status",
)
_ЭФФЕКТЫ = ("_пометить", "enqueue_geocode", "_известить", "enqueue_autofill", "enqueue_llm_read")


async def _снимок(db_sessionmaker: Any, cid: uuid.UUID) -> dict[str, Any]:
    row = await самара._row(db_sessionmaker, cid)
    return {поле: getattr(row, поле) for поле in _ПОЛЯ_СУДА}


@pytest.fixture
def шпионы(monkeypatch: Any) -> dict[str, list[tuple[Any, ...]]]:
    """Все точки записи и задач воркера — под счёт; сами вызовы проходят."""
    вызовы: dict[str, list[tuple[Any, ...]]] = {имя: [] for имя in _ЭФФЕКТЫ}
    for имя in _ЭФФЕКТЫ:
        исходная = getattr(worker, имя)

        def обёртка(*a: Any, _имя: str = имя, _исходная: Any = исходная, **kw: Any) -> Any:
            вызовы[_имя].append(a)
            return _исходная(*a, **kw)

        monkeypatch.setattr(worker, имя, обёртка)
    return вызовы


async def _строка_самары(seed: Any, db_sessionmaker: Any, текст: str = "ул Ленина 5") -> Any:
    return await самара._строка(seed, db_sessionmaker, "samara", _разобрать(текст))


def _пригород(dadata_отвечает: dict, точка_города: dict) -> None:
    """Стенд пригорода (18.09): по городу пусто, по области один дом в 24 км."""
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]


# ── воркер: сухой суд дома ────────────────────────────────────────────────────


async def test_сухой_суд_дома_не_пишет_и_не_ставит_задач(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    шпионы: dict,
) -> None:
    """Стенд Самары: правило `suburb` решает `exact`. Сухо: статус тот же, копилка
    полна (правило, политика, строка карты, км, автозапись была бы), но
    строка не изменилась ни полем, задачи и кадр не ставились, ключа автозаписи
    нет. Диверсия D2."""
    await самара._режим(db_sessionmaker, "nominatim")
    _пригород(dadata_отвечает, точка_города)
    cid = await _строка_самары(seed_conversation, db_sessionmaker)
    до = await _снимок(db_sessionmaker, cid)

    статус, итог = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)

    assert статус == g.GEO_EXACT and итог is not None
    assert (итог.rule, итог.policy, итог.status) == (g.RULE_SUBURB, g.POLICY_EXACT, g.GEO_EXACT)
    assert итог.formatted == "ул Ленина, 5, Смышляевка" and итог.provider == "dadata"
    assert итог.hit_city == "Смышляевка" and итог.variants_n == 0 and итог.office is None
    assert итог.km == pytest.approx(g.distance_km(САМАРА, (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon)))
    assert итог.would_autofill is True and итог.shadow is None
    assert итог.trace is not None and итог.trace["rule"] == g.RULE_SUBURB
    # Ни записи, ни задач, ни кадра — и строка байт в байт прежняя.
    assert {имя: len(в) for имя, в in шпионы.items()} == dict.fromkeys(_ЭФФЕКТЫ, 0)
    assert await _снимок(db_sessionmaker, cid) == до
    assert до["geo_status"] == g.GEO_PENDING and (до["trace"] or {}).get("rule") is None
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    # Сухой ≠ бесплатный: походы к DaData состоялись и посчитаны.
    assert len(dadata_отвечает["запросы"]) >= 2
    assert await worker.dadata_calls_today(redis) >= 2

    # Тот же стенд в бою — пишет и ставит автозапись (сравнение путей).
    _пригород(dadata_отвечает, точка_города)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    после = await _снимок(db_sessionmaker, cid)
    assert после["geo_status"] == g.GEO_EXACT and после["trace"]["rule"] == g.RULE_SUBURB
    assert после["geo_formatted"] == итог.formatted
    assert len(шпионы["_пометить"]) == 1 and len(шпионы["enqueue_autofill"]) == 1
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_сухой_суд_судит_решённую_строку(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
    шпионы: dict,
) -> None:
    """Строка `exact` после боевого суда: живой повтор — `already` без суда;
    сухой — судится заново (Яндекс сходил), строка прежняя. Диверсия D1."""
    await п5._режим(db_sessionmaker)
    cid = await п5._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert len(яндекс) == 1
    до = await _снимок(db_sessionmaker, cid)
    assert до["geo_status"] == g.GEO_EXACT

    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "already"
    assert len(яндекс) == 1, "живой повтор решённой строки к карте не ходит"

    статус, итог = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)
    assert статус == g.GEO_EXACT and итог is not None and итог.status == g.GEO_EXACT
    assert len(яндекс) == 2, "сухой суд решённой строки ходит к карте заново"
    assert итог.formatted == до["geo_formatted"] and итог.would_autofill is True
    # Прежний суд в следе уходит в `prev` — как записал бы пересуд.
    assert итог.trace is not None and итог.trace.get("prev") == {
        k: v for k, v in до["trace"].items() if k in g.TRACE_VERDICT_KEYS
    }
    assert await _снимок(db_sessionmaker, cid) == до
    assert len(шпионы["_пометить"]) == 1, "запись была только у боевого суда"


async def test_policy_словарём_меняет_итог_и_настройка_не_читается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    monkeypatch: Any,
) -> None:
    """`policy={suburb: off}` → `elsewhere` с вариантом, правила нет, автозаписи
    не было бы; `shadow` → тень в копилке; `exact` → как в бою. С `policy` в
    руках настройка `address_geo.rule_policy` не читается ни разу; без него —
    ровно один раз (сторож 6.0а). Диверсия D3."""
    await самара._режим(db_sessionmaker, "nominatim")
    cid = await _строка_самары(seed_conversation, db_sessionmaker)
    до = await _снимок(db_sessionmaker, cid)
    исходный = app_settings.get
    чтения: list[str] = []

    async def шпион(db: Any, key: str, *a: Any, **kw: Any) -> Any:
        if key == app_settings.ADDRESS_GEO_RULE_POLICY:
            чтения.append(key)
        return await исходный(db, key, *a, **kw)

    monkeypatch.setattr(app_settings, "get", шпион)

    _пригород(dadata_отвечает, точка_города)
    статус, выкл = await worker.dry_judge(
        ctx(db_sessionmaker, redis), cid, policy={g.RULE_SUBURB: g.POLICY_OFF}
    )
    assert статус == g.GEO_ELSEWHERE and выкл is not None
    assert (выкл.rule, выкл.policy, выкл.variants_n, выкл.would_autofill) == (None, None, 1, False)
    assert выкл.shadow is None

    _пригород(dadata_отвечает, точка_города)
    статус, тень = await worker.dry_judge(
        ctx(db_sessionmaker, redis), cid, policy={g.RULE_SUBURB: g.POLICY_SHADOW}
    )
    assert статус == g.GEO_ELSEWHERE and тень is not None and тень.rule is None
    assert тень.shadow is not None and тень.shadow.rule == g.RULE_SUBURB
    assert тень.shadow.key == "ул Ленина, 5, Смышляевка" and тень.shadow.instead is None
    assert тень.trace is not None and тень.trace["shadow"]["rule"] == g.RULE_SUBURB

    _пригород(dadata_отвечает, точка_города)
    статус, вкл = await worker.dry_judge(
        ctx(db_sessionmaker, redis), cid, policy={g.RULE_SUBURB: g.POLICY_EXACT}
    )
    assert статус == g.GEO_EXACT and вкл is not None and вкл.rule == g.RULE_SUBURB
    assert чтения == [], "политика пришла словарём — настройка не читается"

    _пригород(dadata_отвечает, точка_города)
    статус, по_настройке = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)
    assert статус == g.GEO_EXACT and по_настройке is not None
    assert len(чтения) == 1, "без словаря — одно чтение на задачу, как в бою"
    assert await _снимок(db_sessionmaker, cid) == до


async def test_устаревший_geo_prev_не_стирается_в_сухом(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """Снимок `geo_prev` про другой разбор (`content` не совпал): в бою фаза 1
    стирает его коммитом до похода; сухой суд — нет (и не читает). Диверсия D4."""
    await самара._режим(db_sessionmaker, "nominatim")
    cid = await _строка_самары(seed_conversation, db_sessionmaker)
    чужой = {"content": {"street": "ул Другая", "house": "9"}, "status": g.GEO_HOUSE_MISSING}
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_prev = чужой
        await s.commit()

    _пригород(dadata_отвечает, точка_города)
    статус, итог = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)
    assert статус == g.GEO_EXACT and итог is not None
    assert (await самара._row(db_sessionmaker, cid)).geo_prev == чужой

    _пригород(dadata_отвечает, точка_города)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await самара._row(db_sessionmaker, cid)).geo_prev is None, "бой стирает чужой снимок"


async def test_сухой_отказ_карты_без_записи_и_счётчика_бана(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, шпионы: dict
) -> None:
    """OSM отвечает `blocked` (DaData без ключа): в бою строка `blocked` и
    счётчик бана; сухо — статус `blocked` в копилке, строка `pending`, ключа
    `geo:blocked:nominatim` нет. Диверсия D5."""
    monkeypatch.setitem(gateway.known_keys, "dadata", False)

    async def бан(query: Any, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("nominatim", "blocked", status=403)

    monkeypatch.setattr(worker.nominatim, "search", бан)
    await самара._режим(db_sessionmaker, "nominatim")
    cid = await _строка_самары(seed_conversation, db_sessionmaker)
    до = await _снимок(db_sessionmaker, cid)

    статус, итог = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)
    assert статус == g.GEO_BLOCKED and итог is not None
    assert (итог.status, итог.provider, итог.formatted, итог.would_autofill) == (
        g.GEO_BLOCKED,
        "nominatim",
        None,
        False,
    )
    assert await _снимок(db_sessionmaker, cid) == до and до["geo_status"] == g.GEO_PENDING
    assert not await redis.exists("geo:blocked:nominatim")
    assert шпионы["_пометить"] == []

    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_BLOCKED
    assert (await самара._row(db_sessionmaker, cid)).geo_status == g.GEO_BLOCKED
    assert int(await redis.get("geo:blocked:nominatim") or 0) == 1


async def test_сухой_суд_места_не_пишет(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, шпионы: dict
) -> None:
    """Место без улицы (`_geocode_place`): DaData знает пункт → в бою `exact` с
    точкой пункта и автозапись; сухо — копилка (степень approx → автозапись
    была бы), строка прежняя. Диверсия D6."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    пункт = g.PlaceHit(
        name="посёлок Заречный",
        kind="settlement",
        settlement="Заречный",
        area=None,
        city="Орск",
        district=None,
        region="Оренбургская область",
        lat=51.2101234,
        lon=58.5012345,
    )

    async def место(место_: Any, **kw: Any) -> list[g.PlaceHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        return [пункт]

    monkeypatch.setattr(worker.dadata, "search_place", место)
    cid = await без_api._строка(seed_conversation, db_sessionmaker, "посёлок Заречный", место=True)
    до = await _снимок(db_sessionmaker, cid)

    статус, итог = await worker.dry_judge(ctx(db_sessionmaker, redis), cid)
    assert статус == g.GEO_EXACT and итог is not None
    assert (итог.status, итог.provider, итог.rule, итог.variants_n) == (
        g.GEO_EXACT,
        "dadata",
        None,
        0,
    )
    assert итог.formatted == "посёлок Заречный, Орск" and итог.would_autofill is True
    assert итог.trace is not None and итог.trace["query_form"] == g.QUERY_FORM_PLACE
    assert await _снимок(db_sessionmaker, cid) == до and до["geo_status"] == g.GEO_PENDING
    assert {имя: len(в) for имя, в in шпионы.items()} == dict.fromkeys(_ЭФФЕКТЫ, 0)
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")

    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await самара._row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted) == (g.GEO_EXACT, итог.formatted)
    assert len(шпионы["_пометить"]) == 1 and len(шпионы["enqueue_autofill"]) == 1


async def test_dry_judge_без_строки_и_копилка_закрывается(db_sessionmaker: Any, redis: Any) -> None:
    """Строки нет → `gone` и пустая копилка; после вызова копилка снята —
    следующий живой суд её не видит."""
    assert worker._СУХОЙ_СУД.get() is None
    assert await worker.dry_judge(ctx(db_sessionmaker, redis), uuid.uuid4()) == ("gone", None)
    assert worker._СУХОЙ_СУД.get() is None


# ── воркер: удержание прежних улик в сухом суде (#3, #7) ─────────────────────


async def test_сухой_суд_решённой_строки_держит_улики_как_пересуд(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
    шпионы: dict,
) -> None:
    """Строка `exact` от Яндекса (DaData и OSM пусты); доля Яндекса починке
    выбрана. Сухой суд без снимка выносил бы `not_found` — бой в том же
    состоянии удерживает улики (`address-recheck` кладёт снимок и судит).
    Теперь сухой = живой пересуд: `exact` удержан, `kept=True`,
    `missing=('yandex',)`, журнал `rejudge_kept` с `reason=dry_run`, правило
    из следа (нет), строка не изменилась; тот же итог даёт живой пересуд со
    снимком. Диверсия D11."""
    await п5._режим(db_sessionmaker)
    cid = await п5._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    до = await _снимок(db_sessionmaker, cid)
    assert до["geo_status"] == g.GEO_EXACT and до["geo_prev"] is None
    await redis.set(worker.yandex_calls_key(), 900)

    with structlog.testing.capture_logs() as логи:
        статус, итог = await worker.dry_judge(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert статус == g.GEO_EXACT and итог is not None
    assert (итог.status, итог.formatted, итог.provider) == (
        g.GEO_EXACT,
        до["geo_formatted"],
        "yandex",
    )
    assert итог.kept is True and итог.missing == ("yandex",)
    assert итог.rule is None and итог.policy is None and итог.would_autofill is True
    assert итог.rule == (итог.trace or {}).get("rule")
    (запись,) = [л for л in логи if л["event"] == "geocode.rejudge_kept"]
    assert запись["missing"] == ["yandex"] and запись["reason"] == "dry_run"
    assert запись["rank_was"] == g.RANK_EXACT and запись["rank_new"] == g.RANK_NONE
    assert await _снимок(db_sessionmaker, cid) == до, "снимок только в памяти"
    assert len(шпионы["_пометить"]) == 1

    # Тот же стенд боевым пересудом (снимок улик → суд починки): `exact` удержан.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        улики = clients_svc.снимок_улик(row, reason="recheck")
        for k, v in clients_svc.сброс_вердикта(с_попытками=True, улики=улики).items():
            setattr(row, k, v)
        await s.commit()
    assert (
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
        == g.GEO_EXACT
    )
    после = await самара._row(db_sessionmaker, cid)
    assert (после.geo_status, после.geo_formatted, после.geo_provider) == (
        итог.status,
        итог.formatted,
        итог.provider,
    )


@pytest.fixture
def osm_улица(monkeypatch: Any) -> list[g.Query]:
    """OSM знает улицу клиента, дома на ней не находит (уровень улицы)."""
    вызовы: list[g.Query] = []

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        вызовы.append(query)
        if wait is not None:
            await wait()
        return [dataclasses.replace(без_api.ДОМ, house=None, house_level=False, precise=False)]

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


async def test_удержание_не_приписывает_правило_чужому_ключу(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_улица: Any,
    dadata_пусто: Any,
    яндекс: Any,
    capsys: Any,
) -> None:
    """Снимок `house_missing/yandex` со строкой улицы (ранг 3) сброшен обходом;
    OSM знает улицу без дома, доля Яндекса выбрана. Правило `street_point`
    под `suggest` РЕШАЕТ (ранг 0) — и удерживается прежнее: копилка несёт
    ключ и правило прежнего суда (`rule` из следа — его нет → None), а не
    `street_point`; `kept=True`, `km`/`office` пусты. Контроль: под `exact`
    точка улицы (ранг 4) сильнее — `rule=street_point`, `kept=False`. CLI на
    той же строке: `решено_правилом=0 удержано=1 слепых=1`. Диверсия D12."""
    from app.cli import run_address_rule_dry_run

    await п5._режим(db_sessionmaker)
    cid = await п5._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await п5._со_снимком(db_sessionmaker, cid)
    await redis.set(worker.yandex_calls_key(), 900)
    до = await _снимок(db_sessionmaker, cid)

    with structlog.testing.capture_logs() as логи:
        статус, итог = await worker.dry_judge(
            ctx(db_sessionmaker, redis),
            cid,
            origin=worker.ORIGIN_REPAIR,
            policy={g.RULE_STREET_POINT: g.POLICY_SUGGEST},
        )
    assert статус == g.GEO_HOUSE_MISSING and итог is not None
    assert any(
        л["event"] == "geocode.auto_decided" and л["rule"] == g.RULE_STREET_POINT for л in логи
    ), "правило решило — и было отвергнуто удержанием"
    (запись,) = [л for л in логи if л["event"] == "geocode.rejudge_kept"]
    assert (запись["rank_was"], запись["rank_new"], запись["missing"]) == (3, 0, ["yandex"])
    assert (итог.status, итог.formatted, итог.provider) == (
        g.GEO_HOUSE_MISSING,
        п5.ТЕКСТ_УЛИЦЫ,
        "yandex",
    )
    assert итог.kept is True and итог.missing == ("yandex",)
    assert итог.rule is None and итог.rule == (итог.trace or {}).get("rule")
    assert (итог.policy, итог.km, итог.office) == (None, None, None)
    assert await _снимок(db_sessionmaker, cid) == до

    статус, контроль = await worker.dry_judge(
        ctx(db_sessionmaker, redis),
        cid,
        origin=worker.ORIGIN_REPAIR,
        policy={g.RULE_STREET_POINT: g.POLICY_EXACT},
    )
    assert статус == g.GEO_EXACT and контроль is not None and контроль.kept is False
    assert (контроль.rule, контроль.policy) == (g.RULE_STREET_POINT, g.POLICY_EXACT)
    assert контроль.trace is not None and контроль.trace["rule"] == g.RULE_STREET_POINT
    assert g.point_is_approx(контроль.provider) and контроль.missing == ("yandex",)

    async with db_sessionmaker() as s:
        сводка = await run_address_rule_dry_run(
            s,
            rule=g.RULE_STREET_POINT,
            policy=["street_point=suggest"],
            statuses="pending",
            factory=db_sessionmaker,
            redis=redis,
        )
    вывод = capsys.readouterr().out
    assert (сводка["строк"], сводка["судимо"], сводка["решено_правилом"]) == (1, 1, 0)
    assert (сводка["удержано"], сводка["слепых"], сводка["отказов"]) == (1, 1, 1)
    assert " удержано=1 слепых=1" in вывод
    (строка,) = сводка["rows"]
    assert (строка["rule"], строка["kept"], строка["missing"]) == (None, True, ["yandex"])
    assert строка["key"] == п5.ТЕКСТ_УЛИЦЫ and строка["status"] == g.GEO_HOUSE_MISSING
    assert await _снимок(db_sessionmaker, cid) == до


@pytest.fixture
def osm_голова(monkeypatch: Any) -> list[g.Query]:
    """OSM знает дом «10» на улице клиента — голову дроби «10/77»."""
    вызовы: list[g.Query] = []

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        вызовы.append(query)
        if wait is not None:
            await wait()
        return [dataclasses.replace(без_api.ДОМ, house="10")]

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


async def test_живой_путь_при_удержании_пишет_квартиру_решения_как_прежде(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_голова: Any,
    dadata_пусто: Any,
    яндекс: Any,
    monkeypatch: Any,
) -> None:
    """Проверка правок 21.09: пакет про сухой прогон бой не меняет. Стенд
    удержания (снимок ранга 3, доля Яндекса выбрана, `fraction_head=suggest`
    решает рангом 0 и отвергается), но `dry_run=False`: `_пометить` получает
    `office` из решения (как до 6.0б — квартира из хвоста дроби пишется и при
    удержании), хотя в копилке сухого суда при удержании `office` пуст.
    Диверсия: в `_пометить` живого пути передать `решено.office` → None."""
    await п5._режим(db_sessionmaker)
    cid = await без_api._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 10/77")
    await п5._со_снимком(db_sessionmaker, cid)
    await redis.set(worker.yandex_calls_key(), 900)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_RULE_POLICY: "fraction_head=suggest"}, user_id=None
        )
        await s.commit()
    вызовы: list[dict[str, Any]] = []
    настоящий = worker._пометить

    async def шпион(*args: Any, **kwargs: Any) -> bool:
        вызовы.append(kwargs)
        return await настоящий(*args, **kwargs)

    monkeypatch.setattr(worker, "_пометить", шпион)
    with structlog.testing.capture_logs() as логи:
        await worker.geocode_candidate(
            ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
        )
    assert any(л["event"] == "geocode.rejudge_kept" for л in логи), "стенд обязан удерживать"
    assert any(
        л["event"] == "geocode.auto_decided" and л["rule"] == g.RULE_FRACTION_HEAD for л in логи
    )
    (запись,) = вызовы
    assert запись["office"] == "77"


async def test_удержанная_строка_не_считается_решением_правила_под_off(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict[str, Any],
    osm_пусто: Any,
    точка_города: dict[str, Any],
    яндекс: Any,
    capsys: Any,
) -> None:
    """Проверка правок 21.09: живой суд (режим `osm_then_yandex`) дал `exact`
    правилом пригорода; сухой прогон под `--policy suburb=off` при выбранной
    доле Яндекса удерживает прежний ключ (набор карт неполный). Правило
    удержанного ключа — решение ПРЕЖНЕГО суда: в сводке `решено_правилом=0
    (по suburb: 0)`, а не «правило решило» при выключенном правиле; в JSON
    `rule` остаётся рядом с `kept` (судье оно нужно). Диверсия: считать
    `решено_правилом` без `not итог.kept`."""
    from app.cli import run_address_rule_dry_run

    await redis.set(worker.yandex_calls_key(), 900)
    await самара._режим(db_sessionmaker, "osm_then_yandex")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]
    cid = await самара._строка(
        seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await самара._row(db_sessionmaker, cid)
    assert (row.trace or {}).get("rule") == g.RULE_SUBURB
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]
    async with db_sessionmaker() as s:
        сводка = await run_address_rule_dry_run(
            s, rule=g.RULE_SUBURB, policy=["suburb=off"], factory=db_sessionmaker, redis=redis
        )
    вывод = capsys.readouterr().out
    (строка,) = сводка["rows"]
    assert строка["kept"] is True and строка["rule"] == g.RULE_SUBURB
    assert (сводка["решено_правилом"], сводка["по_правилу"], сводка["удержано"]) == (0, 0, 1)
    assert "решено_правилом=0 (по suburb: 0)" in вывод and "удержано=1" in вывод


# ── CLI: address-rule-dry-run ─────────────────────────────────────────────────


@pytest.fixture
def dadata_область(monkeypatch: Any) -> list[g.Query]:
    """DaData на шлюзе: по городу пусто, по области — единственный дом на улице
    запроса в Смышляевке (пригород, 18 км). Улица берётся из запроса: строки
    одного прогона обязаны быть разными («один адрес — одна строка»), а стенд
    с очередью ответов считал бы походы. Точку города даёт `точка_города`."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    запросы: list[g.Query] = []

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        запросы.append(query)
        ответ = (
            []
            if query.city is not None
            else [dataclasses.replace(СМЫШЛЯЕВКА, street=query.street or СМЫШЛЯЕВКА.street)]
        )
        # Как настоящая интеграция: `seen` собирает все ответы области.
        if kw.get("seen") is not None:
            kw["seen"].extend(ответ)
        return ответ

    monkeypatch.setattr(worker.dadata, "search", search)
    return запросы


async def _политика_настройкой(db_sessionmaker: Any, текст: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_RULE_POLICY: текст}, user_id=None)
        await s.commit()


async def _отказ_карты(db_sessionmaker: Any, cid: Any) -> None:
    """Строка с прежним отказом карты «улица есть, дома нет» — как записал бы
    суд без правила: в выборку сухого прогона по умолчанию (отказы + `exact`)."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status = g.GEO_HOUSE_MISSING
        row.geo_provider = "dadata"
        row.geo_checked_at = datetime.now(UTC)
        row.geo_attempts = 1
        await s.commit()


async def _стенд_cli(seed_conversation: Any, db_sessionmaker: Any, redis: Any) -> dict[str, Any]:
    """Три строки Самары: `решённая` — боевой `exact` со следом `rule=suburb`
    (и старше остальных на час: без сортировки по правилу шла бы последней),
    `отказанная` — прежний отказ карты без следа, `отклонённая` — `rejected`
    (в выборку не идёт). Стенд ждёт `dadata_область` и `точка_города` у теста."""
    await самара._режим(db_sessionmaker, "nominatim")
    решённая = await _строка_самары(seed_conversation, db_sessionmaker, "ул Ленина 5")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), решённая) == g.GEO_EXACT
    await redis.delete(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    отказанная = await _строка_самары(seed_conversation, db_sessionmaker, "ул Пушкина 5")
    await _отказ_карты(db_sessionmaker, отказанная)
    отклонённая = await _строка_самары(seed_conversation, db_sessionmaker, "ул Мира 5")
    await _отказ_карты(db_sessionmaker, отклонённая)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, отклонённая)
        row.status = CANDIDATE_REJECTED
        row_р = await s.get(ClientAddressCandidate, решённая)
        row_р.detected_at = row_р.detected_at - timedelta(hours=1)
        await s.commit()
    return {"решённая": решённая, "отказанная": отказанная, "отклонённая": отклонённая}


async def test_cli_строки_правила_первыми_и_json(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_область: list,
    osm_пусто: Any,
    точка_города: dict,
    шпионы: dict,
    capsys: Any,
    tmp_path: Any,
) -> None:
    """Выборка по умолчанию — отказы и `exact`: две строки (`rejected` не
    берётся), решённая правилом — первой, хотя старше; отказанная под правилом
    стала бы `exact` с автозаписью; счёт `строк=2 судимо=2 решено_правилом=2 (по suburb: 2) exact=2
    would_autofill=2`; JSON — те же две строки с ключами контракта и
    `prev_status`; ни одна строка не изменилась. Диверсии D7, D9."""
    from app.cli import run_address_rule_dry_run

    точка_города["точка"] = САМАРА
    строки = await _стенд_cli(seed_conversation, db_sessionmaker, redis)
    до = {имя: await _снимок(db_sessionmaker, cid) for имя, cid in строки.items()}
    записей_до = len(шпионы["_пометить"])
    dadata_до = await worker.dadata_calls_today(redis)
    файл = tmp_path / "dry.json"
    async with db_sessionmaker() as s:
        итог = await run_address_rule_dry_run(
            s,
            rule=g.RULE_SUBURB,
            days=30,
            limit=10,
            out=str(файл),
            factory=db_sessionmaker,
            redis=redis,
        )
    вывод = capsys.readouterr().out
    assert итог["строк"] == 2 and итог["судимо"] == 2
    assert итог["решено_правилом"] == 2 and итог["по_правилу"] == 2
    assert (итог["exact"], итог["approx"], итог["suggest"], итог["отказов"]) == (2, 0, 0, 0)
    assert итог["would_autofill"] == 2
    assert итог["dadata_за_прогон"] == await worker.dadata_calls_today(redis) - dadata_до > 0
    assert "строк=2 судимо=2 решено_правилом=2 (по suburb: 2) exact=2" in вывод
    assert f"would_autofill=2 dadata_за_прогон={итог['dadata_за_прогон']}" in вывод
    assert "политика:" in вывод and "suburb=exact" in вывод
    # Порядок: строка со следом правила первой, хотя она старше.
    json_строки = json.loads(файл.read_text(encoding="utf-8"))
    assert [r["candidate_id"] for r in json_строки] == [
        str(строки["решённая"]),
        str(строки["отказанная"]),
    ]
    assert json_строки == итог["rows"]
    первая = json_строки[0]
    assert set(первая) >= {
        "candidate_id",
        "conversation_id",
        "rule",
        "policy",
        "status",
        "key",
        "km",
        "would_autofill",
        "prev_status",
    }
    assert (первая["rule"], первая["policy"], первая["status"], первая["prev_status"]) == (
        g.RULE_SUBURB,
        g.POLICY_EXACT,
        g.GEO_EXACT,
        g.GEO_EXACT,
    )
    assert json_строки[1]["prev_status"] == g.GEO_HOUSE_MISSING
    assert json_строки[1]["rule"] == g.RULE_SUBURB and json_строки[1]["would_autofill"]
    assert первая["key"] == "ул Ленина, 5, Смышляевка"
    assert json_строки[1]["key"] == "ул Пушкина, 5, Смышляевка"
    assert первая["km"] == round(g.distance_km(САМАРА, (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon)), 2)
    # Речь клиента в отчёт не идёт — только строки карты.
    assert "ул Ленина 5" not in вывод and "ул Ленина, 5, Смышляевка" in вывод
    # Ничего не записано.
    assert len(шпионы["_пометить"]) == записей_до
    assert {имя: await _снимок(db_sessionmaker, cid) for имя, cid in строки.items()} == до
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_cli_правило_судится_как_exact_поверх_тени(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_область: list,
    osm_пусто: Any,
    точка_города: dict,
    capsys: Any,
) -> None:
    """Боевая настройка держит `suburb=shadow`: без `--policy` прогон судит
    правило «как если бы exact» (решений правилом 2); `--policy suburb=off`
    поверх — решений 0, обе строки `elsewhere` (прочее=2); строки не
    изменились. Строки ещё `pending` — берутся явным `--statuses` (настоящая
    тень — `test_cli_строка_тени_правила_видна_без_statuses`). Диверсия D8."""
    from app.cli import run_address_rule_dry_run

    точка_города["точка"] = САМАРА
    await _политика_настройкой(db_sessionmaker, "suburb=shadow")
    await самара._режим(db_sessionmaker, "nominatim")
    a = await _строка_самары(seed_conversation, db_sessionmaker, "ул Ленина 5")
    b = await _строка_самары(seed_conversation, db_sessionmaker, "ул Пушкина 5")

    async with db_sessionmaker() as s:
        итог = await run_address_rule_dry_run(
            s,
            rule=g.RULE_SUBURB,
            days=30,
            statuses="pending",
            factory=db_sessionmaker,
            redis=redis,
        )
    assert итог["строк"] == 2 and итог["решено_правилом"] == 2 and итог["exact"] == 2
    assert "suburb=exact" in capsys.readouterr().out

    async with db_sessionmaker() as s:
        итог = await run_address_rule_dry_run(
            s,
            rule=g.RULE_SUBURB,
            policy=["suburb=off"],
            days=30,
            statuses="pending",
            factory=db_sessionmaker,
            redis=redis,
        )
    assert (итог["решено_правилом"], итог["exact"], итог["прочее"]) == (0, 0, 2)
    assert {r["status"] for r in итог["rows"]} == {g.GEO_ELSEWHERE}
    assert "suburb=off" in capsys.readouterr().out
    for cid in (a, b):
        assert (await самара._row(db_sessionmaker, cid)).geo_status == g.GEO_PENDING


async def test_cli_строка_тени_правила_видна_без_statuses(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """НАСТОЯЩАЯ тень (§0.3): настройка `suburb=shadow`, живой суд кладёт
    `elsewhere` с `trace.shadow.rule=suburb`. Прогон БЕЗ `--statuses` обязан её
    видеть: `строк=1 решено_правилом=1 exact=1`, `prev_status=elsewhere`, строка
    первой и не изменилась. Явный `--statuses exact` остаётся строгим фильтром
    (строк=0). Диверсия D10."""
    from app.cli import run_address_rule_dry_run

    await _политика_настройкой(db_sessionmaker, "suburb=shadow")
    await самара._режим(db_sessionmaker, "nominatim")
    _пригород(dadata_отвечает, точка_города)
    cid = await _строка_самары(seed_conversation, db_sessionmaker)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    до = await _снимок(db_sessionmaker, cid)
    assert до["geo_status"] == g.GEO_ELSEWHERE and до["trace"]["shadow"]["rule"] == g.RULE_SUBURB

    async def прогон(**kw: Any) -> dict[str, Any]:
        _пригород(dadata_отвечает, точка_города)
        async with db_sessionmaker() as s:
            return await run_address_rule_dry_run(
                s, rule=g.RULE_SUBURB, days=30, factory=db_sessionmaker, redis=redis, **kw
            )

    итог = await прогон()
    assert (итог["строк"], итог["судимо"], итог["решено_правилом"]) == (1, 1, 1)
    assert (итог["по_правилу"], итог["exact"], итог["удержано"]) == (1, 1, 0)
    (строка,) = итог["rows"]
    assert (строка["prev_status"], строка["status"], строка["rule"]) == (
        g.GEO_ELSEWHERE,
        g.GEO_EXACT,
        g.RULE_SUBURB,
    )
    assert строка["key"] == "ул Ленина, 5, Смышляевка" and строка["kept"] is False
    assert await _снимок(db_sessionmaker, cid) == до
    assert (await прогон(statuses="exact"))["строк"] == 0, "явный --statuses режет"
    assert (await прогон(statuses="elsewhere"))["строк"] == 1


async def _три_отказанные(seed_conversation: Any, db_sessionmaker: Any) -> list[Any]:
    """Три свежие строки Самары с прежним отказом карты: ни одна не судилась,
    каждая стоит DaData три похода (город, круг, область) на стенде
    `dadata_область`."""
    await самара._режим(db_sessionmaker, "nominatim")
    строки = []
    for текст in ("ул Ленина 5", "ул Пушкина 5", "ул Мира 5"):
        cid = await _строка_самары(seed_conversation, db_sessionmaker, текст)
        await _отказ_карты(db_sessionmaker, cid)
        строки.append(cid)
    return строки


async def test_cli_бюджет_dadata_останавливает_прогон(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_область: list,
    osm_пусто: Any,
    точка_города: dict,
    capsys: Any,
    tmp_path: Any,
) -> None:
    """Три отказанные строки по три похода DaData каждая. `--dadata-budget 3`:
    первая судится (3 похода), перед второй бюджет выбран — `судимо=1
    несудимо=2 остановлен_по_бюджету=1`, отчёт и `--out` вышли по судимым.
    Бюджет считает СВОИ походы: чужой прирост суточного ключа на 1000 прогон не
    останавливает, а `dadata_за_прогон` равен числу настоящих походов стенда.
    Диверсия D13."""
    from app.cli import run_address_rule_dry_run

    точка_города["точка"] = САМАРА
    строки = await _три_отказанные(seed_conversation, db_sessionmaker)
    файл = tmp_path / "budget.json"
    async with db_sessionmaker() as s:
        итог = await run_address_rule_dry_run(
            s,
            rule=g.RULE_SUBURB,
            dadata_budget=3,
            out=str(файл),
            factory=db_sessionmaker,
            redis=redis,
        )
    вывод = capsys.readouterr().out
    assert (итог["строк"], итог["судимо"], итог["несудимо"]) == (3, 1, 2)
    assert итог["остановлен_по_бюджету"] == 1 and итог["решено_правилом"] == 1
    assert итог["dadata_за_прогон"] == len(dadata_область) == 3
    assert "остановлен перед строкой 2, несудимо=2" in вывод
    assert "dadata_за_прогон=3 остановлен_по_бюджету=1 несудимо=2" in вывод
    (в_файле,) = json.loads(файл.read_text(encoding="utf-8"))
    assert в_файле["candidate_id"] in {str(cid) for cid in строки}

    # Чужие походы (живой поток, починка) в суточном ключе бюджет не выедают.
    await redis.incrby(worker.dadata_calls_key(), 1000)
    del dadata_область[:]
    async with db_sessionmaker() as s:
        итог = await run_address_rule_dry_run(
            s, rule=g.RULE_SUBURB, dadata_budget=500, factory=db_sessionmaker, redis=redis
        )
    assert (итог["судимо"], итог["остановлен_по_бюджету"], итог["несудимо"]) == (3, 0, 0)
    assert итог["dadata_за_прогон"] == len(dadata_область) == 9
    assert "остановлен_по_бюджету" not in capsys.readouterr().out


async def test_cli_несудимые_поимённо(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_область: list,
    osm_пусто: Any,
    точка_города: dict,
    capsys: Any,
) -> None:
    """Карта выключена настройкой → первая строка `disabled`, печать причины и
    останов (`пропущено_disabled=1` при `строк=3`, походов 0). Бан OSM при
    DaData без дома по городу → каждая строка `paused`: `пропущено_paused=3`,
    судимо 0, предупреждение о смещённом итоге — при этом DaData уже сходила
    (походы посчитаны). Диверсия D14."""
    from app.cli import run_address_rule_dry_run

    точка_города["точка"] = САМАРА
    await _три_отказанные(seed_conversation, db_sessionmaker)

    async def прогон() -> dict[str, Any]:
        async with db_sessionmaker() as s:
            return await run_address_rule_dry_run(
                s, rule=g.RULE_SUBURB, factory=db_sessionmaker, redis=redis
            )

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
        await s.commit()
    итог = await прогон()
    вывод = capsys.readouterr().out
    assert (итог["строк"], итог["судимо"], итог["пропущено_disabled"]) == (3, 0, 1)
    assert итог["dadata_за_прогон"] == 0 and dadata_область == []
    assert "карта выключена настройкой address_geo.enabled" in вывод
    assert "dadata_за_прогон=0 пропущено_disabled=1" in вывод

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_ENABLED: True}, user_id=None)
        await s.commit()
    await redis.set("geo:blocked:nominatim", worker.BLOCKED_THRESHOLD)
    итог = await прогон()
    вывод = capsys.readouterr().out
    assert (итог["строк"], итог["судимо"], итог["пропущено_paused"]) == (3, 0, 3)
    assert итог["dadata_за_прогон"] == len(dadata_область) == 3, "DaData сходила до бана"
    assert "пропущено_paused=3" in вывод and "провайдер под баном" in вывод
    assert итог["rows"] == []


async def test_cli_фильтры_выборки(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_область: list,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """`--statuses`, `--levels`, `--limit` и окно дней режут выборку."""
    from app.cli import run_address_rule_dry_run

    точка_города["точка"] = САМАРА
    строки = await _стенд_cli(seed_conversation, db_sessionmaker, redis)

    async def прогон(**kw: Any) -> dict[str, Any]:
        async with db_sessionmaker() as s:
            return await run_address_rule_dry_run(
                s, rule=g.RULE_SUBURB, factory=db_sessionmaker, redis=redis, **kw
            )

    assert (await прогон())["строк"] == 2
    assert (await прогон(statuses="exact"))["строк"] == 1
    assert (await прогон(statuses="house_missing"))["строк"] == 1
    assert (await прогон(statuses="pending"))["строк"] == 0
    assert (await прогон(levels="B"))["строк"] == 0
    assert (await прогон(limit=1))["строк"] == 1
    assert (await прогон(days=0))["строк"] == 0
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, строки["отказанная"])
        row.detected_at = row.detected_at - timedelta(days=40)
        await s.commit()
    assert (await прогон(days=30))["строк"] == 1


@pytest.mark.parametrize(
    "опции",
    [
        {"rule": "no_such_rule"},
        {"rule": g.RULE_SUBURB, "policy": ["suburb=on"]},
        {"rule": g.RULE_SUBURB, "policy": ["no_such=off"]},
        {"rule": g.RULE_SUBURB, "statuses": "foo"},
        {"rule": g.RULE_SUBURB, "levels": "Z"},
    ],
)
async def test_cli_мусор_в_опциях_2(db_sessionmaker: Any, redis: Any, опции: dict) -> None:
    """Опечатка в правиле, политике (в том числе `on` — политики разбора, не
    карты), статусе или уровне — выход 2 до выборки, не пустой отчёт."""
    from app.cli import run_address_rule_dry_run

    async with db_sessionmaker() as s:
        with pytest.raises(typer.Exit) as exc:
            await run_address_rule_dry_run(s, factory=db_sessionmaker, redis=redis, **опции)
    assert exc.value.exit_code == 2
