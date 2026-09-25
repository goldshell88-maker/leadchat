"""Лестница политик правил адреса — еженедельно, без человека (пакет 6.0а, §0.3).

ЗАЧЕМ. Решение владельца 20.09: автоматика без человека, точность только
растёт. Правило слоя вердикта рождается в тени, а в карточку начинает писать
лишь когда его решения осуждены (судья I-4) и приняты людьми в достаточном
числе; выходит за цель ступени — само опускается. Порог — не «доля», а
интервал Уилсона 95 % (`address_funnel.wilson_bounds`): при объёмах 5–19
решений в месяц «1 из 50» иначе понижало бы правило в 64 % недель (С-1).

ЧТО ДЕЛАЕТ. По понедельникам после воронки: `address_funnel.measure_rules`
(накопительно за 8 недель) → по каждому правилу `decide` (чистая функция,
ниже) → запись `address_geo.rule_policy` тем же `app_settings.set_many`
(`user_id=None`), что у ручки настроек, — проверка та же; строка журнала
`settings.address_detect_changed` с `source=rule_policy_weekly`; уведомление
администраторам: понижение — `address.rule_degraded` (при `off` — с текстом
команды `address-unfill`, чинить прошлое автоматика сама не берётся, docs/47
§2), повышение — `address.rule_promoted`.

ПОДЪЁМ ДО `exact` — С ПРАВОМ ОСТАНОВИТЬ (§0.3): сначала объявление (журнал
`settings.address_rule_promotion_announced` — память о дате, второй таблицы
нет) и уведомление с окном `VETO_DAYS`; следующий прогон после окна (по
календарю UTC, не по миллисекундам — ревью 20.09 #20) поднимает, если пороги
держатся и человек не сохранил правило в настройке сам. Вето — сохранить
настройку со строкой «rule=approx»: значение уже стоит (его вписала сама
автоматика при подъёме до approx), поэтому ручка пишет строку журнала и при
неизменном значении, с `details.submitted=["rule_policy", …]`, а задача
считает человеческими все правила из сохранённой строки (`human_set_rules`).
Значение, поставленное человеком, автоматика вверх не переписывает никогда;
вниз — да, это страховка.

ВЫХОД ИЗ ТЕНИ (решение владельца 20.09 по ревью #7): при 0 ложных верхняя
граница Уилсона ≤ 5 % достижима только с 73 осуждёнными (не 60) — по объёмам
§10 (15–35 решений/мес) правило сидело бы в тени бессрочно. Поэтому
`shadow → suggest` двумя путями: ≥ 73 осуждённых и верхняя ≤ 5 %, либо
осуждены ВСЕ теневые решения (≥ 20) без единого ложного и без подмен места.
`suggest` в карточку не пишет — только предложение оператору, риск ограничен
нажатием человека.

СУДЬЯ (I-4, 6.0б): `judged`/`shadow_judged` наполняет ночная задача
`address_judge_daily`, а `judge_calibrated(db)` читает согласие судьи с
человеком из настройки `address_llm.judge_agreement` (`address-judge
--calibrate`). Пока калибровки нет — подъём до `exact` не наступает по
построению; понижение возможно по отказам людей (`rejected_by_hand`).

⚠ ТРАНЗАКЦИЕЙ ВЛАДЕЕТ ЗАДАЧА (тот же урок, что у воронки): `session_scope`
на выходе откатывает, `notify` не коммитит; настройка и журнал — commit ДО
уведомлений, чтобы упавший центр уведомлений не откатил политику.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import AuditLog
from app.services import address_funnel, app_settings, geocode
from app.services import notifications as notify_svc
from app.services.address_funnel import RuleCounts, wilson_bounds
from app.services.audit import write_audit
from app.services.geocode import (
    POLICIES,
    POLICY_APPROX,
    POLICY_EXACT,
    POLICY_OFF,
    POLICY_SHADOW,
    POLICY_SUGGEST,
)

log = structlog.get_logger("app.rule_policy_weekly")

JOB_ID = "address_rule_policy_weekly"
KIND_DEGRADED = "address.rule_degraded"
KIND_PROMOTED = "address.rule_promoted"
SETTINGS_ACTION = "settings.address_detect_changed"
ANNOUNCE_ACTION = "settings.address_rule_promotion_announced"
#: Откуда взялась строка журнала настроек — чтобы отличать от ручки.
SOURCE = "rule_policy_weekly"
STATEMENT_TIMEOUT = "180s"
DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600}

# --- пороги §0.3 ---------------------------------------------------------------------
#: Минимальный возраст правила в окне для подъёма («≥ 2 недели»).
MIN_AGE = timedelta(days=14)
#: `shadow → suggest` по Уилсону: осуждено решений в тени — не меньше стольких.
#: 73 — фактическая граница, не круглое число: `wilson_bounds(0, 73)[1] ≤ 0.05`,
#: а `wilson_bounds(0, 72)[1] > 0.05` (при одном ложном — с 110).
SHADOW_MIN_JUDGED = 73
#: …или осуждены все (при меньшем объёме, но не меньше стольких) — тогда без
#: Уилсона: ни одного ложного и ни одной подмены места.
SHADOW_MIN_ALL = 20
#: `suggest → approx`: судья + нажатия людей.
SUGGEST_MIN_SIGNALS = 60
#: `approx → exact`: осуждённых за окно.
EXACT_MIN_SIGNALS = 150
#: Верхняя граница Уилсона доли ложных для подъёма НА ступень.
UPPER_LIMIT: dict[str, float] = {POLICY_SUGGEST: 0.05, POLICY_APPROX: 0.05, POLICY_EXACT: 0.02}
#: Цель ступени: нижняя граница выше — понижение на одну.
TARGET: dict[str, float] = {POLICY_EXACT: 0.02, POLICY_APPROX: 0.05, POLICY_SUGGEST: 0.08}
DEMOTE_MIN_SIGNALS = 30
#: Окно вето на подъём до `exact`.
VETO_DAYS = 7
#: Потолок лестницы по правилам (§10): `exact` — только дому справочника.
#: Правил без записи потолок не касается (по умолчанию — `exact`).
POLICY_CEILING: dict[str, str] = {
    "settlement_point": POLICY_APPROX,  # Q9
    "nearest_house": POLICY_APPROX,  # Q9
    "numbered_place": POLICY_APPROX,  # Q11
    "default_street_type": POLICY_SUGGEST,  # Q12 — suggest навсегда
    "named_place_country": POLICY_APPROX,  # Q13 — exact только рукой владельца
    "street_suggest": POLICY_APPROX,  # Q15
    "city_over_far_variants": POLICY_APPROX,  # Q8
}
#: Одна «подмена места при точной точке» (§0.4) → `off` немедленно. Имена
#: правил Q17/Q18 (формы разбора и усечение) появятся с пакетом 7 — добавить
#: сюда при рождении правила.
OFF_ON_PLACE_SWAP: frozenset[str] = frozenset(
    {"default_street_type", "named_place_country", "street_suggest"}
)

_РАНГ = {p: i for i, p in enumerate(POLICIES)}


@dataclass(frozen=True, slots=True)
class Move:
    """Решение лестницы по одному правилу. `announce` — не сам подъём до
    `exact`, а его объявление (окно вето)."""

    rule: str
    from_policy: str
    to_policy: str
    why: str
    announce: bool = False

    @property
    def up(self) -> bool:
        return _РАНГ[self.to_policy] > _РАНГ[self.from_policy]


#: Согласие судьи с человеком на размеченной сотне (§0.3 `approx → exact`):
#: ниже — судья не ворота, правило стоит на `approx`, спорное — человеку.
JUDGE_MIN_AGREEMENT = 95


async def judge_calibrated(db: AsyncSession) -> bool:
    """Судья откалиброван: настройка `address_llm.judge_agreement` (пишет
    `address-judge --calibrate`, пакет 6.0б) не меньше `JUDGE_MIN_AGREEMENT`.
    Пусто или мусор — не откалиброван: подъём до `exact` не наступает."""
    согласие = app_settings.judge_agreement_pct(
        await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT)
    )
    return согласие is not None and согласие >= JUDGE_MIN_AGREEMENT


def _ниже(policy: str) -> str:
    return POLICIES[max(0, _РАНГ[policy] - 1)]


def _в_потолке(rule: str, to_policy: str) -> bool:
    return _РАНГ[to_policy] <= _РАНГ[POLICY_CEILING.get(rule, POLICY_EXACT)]


def _pct(x: float) -> str:
    return f"{x * 100:.1f} %"


def decide(
    rule: str,
    current: str,
    c: RuleCounts,
    *,
    now: datetime,
    announced_at: datetime | None = None,
    human_set: bool = False,
    judge_ok: bool | None = None,
) -> Move | None:
    """Чистая лестница §0.3 для одного правила. `human_set` — текущее значение
    поставил человек: вверх не трогаем. `judge_ok` — калибровка судьи; задача
    читает её из настройки (`judge_calibrated(db)`), у чистой функции базы
    нет, поэтому не сказано — значит, не откалиброван."""
    if current not in _РАНГ:
        return None
    judge_ok = bool(judge_ok)
    # 1. Подмена места при точной точке — `off` с первого случая.
    if current != POLICY_OFF and rule in OFF_ON_PLACE_SWAP and c.place_swap >= 1:
        return Move(rule, current, POLICY_OFF, f"подмена места при точной точке: {c.place_swap}")
    # 2. Понижение на ступень: нижняя граница Уилсона выше цели ступени.
    if current in TARGET and c.signals >= DEMOTE_MIN_SIGNALS:
        нижняя, _ = wilson_bounds(c.false_total, c.signals)
        if нижняя > TARGET[current]:
            return Move(
                rule,
                current,
                _ниже(current),
                f"нижняя граница доли ложных {_pct(нижняя)} > цели {_pct(TARGET[current])} "
                f"при {c.signals} известных из {c.n}",
            )
    # 3. Подъёмы — только не поверх значения человека и не выше потолка.
    if human_set:
        return None
    взрослое = c.first_at is not None and now - c.first_at >= MIN_AGE
    if current == POLICY_SHADOW:
        # Два пути из тени (шапка модуля): по Уилсону от 73 осуждённых, либо
        # «осуждены все» (≥ 20) — без ложных и без подмен места. Подмена места
        # запирает оба пути одинаково: больший объём улик не повод судить
        # признак мягче (проверка правок 21.09).
        if c.shadow_judged >= SHADOW_MIN_JUDGED:
            _, верхняя = wilson_bounds(c.shadow_false, c.shadow_judged)
            готово = верхняя <= UPPER_LIMIT[POLICY_SUGGEST] and c.place_swap == 0
            почему = (
                f"тень: осуждено {c.shadow_judged} из {c.shadow_n}, верхняя граница {_pct(верхняя)}"
            )
        else:
            все = c.shadow_judged >= c.shadow_n and c.shadow_judged >= SHADOW_MIN_ALL
            готово = все and c.shadow_false == 0 and c.place_swap == 0
            почему = f"тень: осуждены все {c.shadow_judged}, ложных и подмен места нет"
        if взрослое and готово and _в_потолке(rule, POLICY_SUGGEST):
            return Move(rule, current, POLICY_SUGGEST, почему)
        return None
    if current == POLICY_SUGGEST:
        _, верхняя = wilson_bounds(c.false_total, c.signals)
        if (
            взрослое
            and c.signals >= SUGGEST_MIN_SIGNALS
            and верхняя <= UPPER_LIMIT[POLICY_APPROX]
            and c.place_swap == 0
            and _в_потолке(rule, POLICY_APPROX)
        ):
            return Move(
                rule,
                current,
                POLICY_APPROX,
                f"известно о {c.signals} решениях (принято руками {c.accepted_by_hand}), "
                f"верхняя граница {_pct(верхняя)}",
            )
        return None
    if current == POLICY_APPROX:
        _, верхняя = wilson_bounds(c.false_total, c.signals)
        if not (
            c.signals >= EXACT_MIN_SIGNALS
            and верхняя <= UPPER_LIMIT[POLICY_EXACT]
            and judge_ok
            and _в_потолке(rule, POLICY_EXACT)
        ):
            return None
        почему = f"осуждено {c.signals}, верхняя граница {_pct(верхняя)}, судья откалиброван"
        if announced_at is None:
            return Move(rule, current, POLICY_EXACT, почему, announce=True)
        # По календарю UTC, а не по разности времён: `created_at` объявления —
        # `func.now()` транзакции, оно на миллисекунды позже `now` прошлого
        # прогона, и крон недели спустя давал бы «6 дн 23:59:59.99» — подъём
        # откладывался бы монетой на неделю-две (ревью 20.09, #20).
        if (_день_utc(now) - _день_utc(announced_at)).days >= VETO_DAYS:
            return Move(rule, current, POLICY_EXACT, почему)
    return None


def _день_utc(t: datetime) -> date:
    return (t.astimezone(UTC) if t.tzinfo is not None else t).date()


# --- тексты -----------------------------------------------------------------------


def unfill_command(rule: str) -> str:
    """Команда владельцу из уведомления об `off` (программа §7 п. 5)."""
    return f"address-unfill --trace rule={rule} --days 30"


def notification_body(move: Move, c: RuleCounts, *, deadline: datetime | None = None) -> str:
    подпись = geocode.rule_label(move.rule) or move.rule
    шапка = f"Правило «{подпись}» ({move.rule})"
    цифры = (
        f"за {address_funnel.RULE_WINDOW_WEEKS} нед. решений {c.n}, известно о {c.signals}, "
        f"ложных {c.false_total}, принято руками {c.accepted_by_hand}, "
        f"отвергнуто руками {c.rejected_by_hand}"
    )
    if move.announce and deadline is not None:
        return (
            f"{шапка} будет поднято до exact {deadline:%d.%m}: {move.why}; {цифры}. "
            f"Остановить — «Аккаунты Авито → Адреса в переписке → Перекрытия политики»: "
            f"в строке уже стоит «{move.rule}=approx», нажмите «Сохранить политику» — "
            f"важно само сохранение (настройка «{app_settings.ADDRESS_GEO_RULE_POLICY}»)."
        )
    текст = f"{шапка}: {move.from_policy} → {move.to_policy}. Причина: {move.why}; {цифры}."
    if move.to_policy == POLICY_OFF:
        текст += (
            f" Снять уже записанное этим правилом: «{unfill_command(move.rule)}» "
            "(сухо, печатает число), затем то же с --no-dry-run."
        )
    return текст


def policy_text(policy: Mapping[str, str]) -> str:
    """Настройка `имя=значение,…` из словаря — обратный `parse_rule_policy`."""
    return ",".join(f"{k}={v}" for k, v in sorted(policy.items()))


# --- журнал: кто ставил и что объявлено -------------------------------------------


def _политика_из(details: Any, ключ: str) -> dict[str, str]:
    """`details.before/after.rule_policy` строки `settings.address_detect_changed`."""
    if not isinstance(details, Mapping):
        return {}
    блок = details.get(ключ)
    текст = блок.get("rule_policy") if isinstance(блок, Mapping) else None
    # Нестрого: имя правила, которого уже нет в реестре (переименовано,
    # снято), пропускается, соседние правила той же строки остаются — иначе
    # одна старая пара стирала бы «кто ставил последним» у всех.
    return geocode.parse_rule_policy(текст if isinstance(текст, str) else "", strict=False)


def _подано_rule_policy(details: Any) -> bool:
    """Ручка пометила строку явной подачей `rule_policy` (`details.submitted`)."""
    подано = details.get("submitted") if isinstance(details, Mapping) else None
    return isinstance(подано, list) and "rule_policy" in подано


async def human_set_rules(db: AsyncSession) -> set[str]:
    """Правила, чьё ТЕКУЩЕЕ значение в настройке поставил человек. Такие
    автоматика вверх не переписывает (вето §0.3 — частный случай).

    Две формы человеческой строки журнала: (а) человек явно подал
    `rule_policy` (`details.submitted`) — его все правила из сохранённой
    строки, менялось значение или нет: так вписывается вето «rule=approx»
    поверх того же `approx`, что уже поставила автоматика; (б) иначе — как у
    строк автоматики, по разнице значений: тумблер, сохранённый без строки
    политики, правил не присваивает. Последняя строка по правилу решает."""
    rows = (
        await db.execute(
            sa.select(AuditLog.user_id, AuditLog.details)
            .where(AuditLog.action == SETTINGS_ACTION)
            .order_by(AuditLog.created_at)
        )
    ).all()
    последний_человек: dict[str, bool] = {}
    for r in rows:
        было, стало = _политика_из(r.details, "before"), _политика_из(r.details, "after")
        человек = r.user_id is not None
        подал_строку = человек and _подано_rule_policy(r.details)
        for rule in set(было) | set(стало):
            if подал_строку or было.get(rule) != стало.get(rule):
                последний_человек[rule] = человек
    return {rule for rule, человек in последний_человек.items() if человек}


async def announcements(db: AsyncSession, *, since: datetime) -> dict[str, datetime]:
    """Объявленные подъёмы до `exact`: правило → время последнего объявления
    (не старше `since`, чтобы прошлогоднее объявление не подняло правило сегодня)."""
    rows = (
        await db.execute(
            sa.select(AuditLog.created_at, AuditLog.details)
            .where(AuditLog.action == ANNOUNCE_ACTION, AuditLog.created_at >= since)
            .order_by(AuditLog.created_at)
        )
    ).all()
    итог: dict[str, datetime] = {}
    for r in rows:
        rule = r.details.get("rule") if isinstance(r.details, Mapping) else None
        if isinstance(rule, str):
            когда = r.created_at
            итог[rule] = когда.replace(tzinfo=UTC) if когда.tzinfo is None else когда
    return итог


# --- задача -----------------------------------------------------------------------


async def review_rule_policies(now: datetime | None = None) -> list[Move]:
    """Точка входа планировщика: своя сессия, свой Redis (стиль воронки)."""
    now = now or datetime.now(UTC)
    redis = redis_mod.get_client()
    результаты: list[notify_svc.NotifyResult] = []
    async with db_mod.session_scope() as db:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        текст = await app_settings.get(db, app_settings.ADDRESS_GEO_RULE_POLICY)
        # Нестрого, как воркер: пара с именем, снятым из реестра, пропускается,
        # остальные перекрытия действуют и переписываются обратно как есть.
        политика = geocode.parse_rule_policy(текст, strict=False)
        по_правилам = await address_funnel.measure_rules(db, until=now)
        человеком = await human_set_rules(db)
        судья = await judge_calibrated(db)
        # Запас в сутки над двумя окнами: объявление недели 0 обязано попасть
        # в прогон недели 2, даже если тот стартовал на миллисекунды позже
        # прошлого (иначе задача объявляла бы заново, #20); старее — не в счёт.
        объявлено = await announcements(db, since=now - timedelta(days=2 * VETO_DAYS + 1))
        ходы: list[Move] = []
        # Только правила реестра: имя из следа, которого в `RULE_DEFAULT_POLICY`
        # нет (переименовано, снято), в настройку не запишется — `set_many`
        # проверяет имена той же `parse_rule_policy`, что и ручка.
        чужие = sorted(set(по_правилам) - set(geocode.RULE_DEFAULT_POLICY))
        if чужие:
            log.warning("rule_policy.unknown_rules_in_trace", rules=чужие)
        for rule in sorted(geocode.RULE_DEFAULT_POLICY):
            c = по_правилам.get(rule, RuleCounts())
            ход = decide(
                rule,
                geocode.rule_policy(политика, rule),
                c,
                now=now,
                announced_at=объявлено.get(rule),
                human_set=rule in человеком,
                judge_ok=судья,
            )
            if ход is not None:
                ходы.append(ход)
        if not ходы:
            log.info("rule_policy.reviewed", rules=len(по_правилам), moves=0)
            return []
        новая = dict(политика)
        for ход in ходы:
            if not ход.announce:
                новая[ход.rule] = ход.to_policy
        if новая != политика:
            новый_текст = policy_text(новая)
            # Та же проверка, что у ручки: имена и значения из реестров.
            await app_settings.set_many(
                db, {app_settings.ADDRESS_GEO_RULE_POLICY: новый_текст}, user_id=None
            )
            await write_audit(
                db,
                user_id=None,
                action=SETTINGS_ACTION,
                entity="settings",
                details={
                    "source": SOURCE,
                    "before": {"rule_policy": текст},
                    "after": {"rule_policy": новый_текст},
                    "moves": [
                        {"rule": х.rule, "from": х.from_policy, "to": х.to_policy, "why": х.why}
                        for х in ходы
                        if not х.announce
                    ],
                },
            )
        for ход in ходы:
            if ход.announce:
                await write_audit(
                    db,
                    user_id=None,
                    action=ANNOUNCE_ACTION,
                    entity="settings",
                    details={
                        "rule": ход.rule,
                        "from": ход.from_policy,
                        "to": ход.to_policy,
                        "why": ход.why,
                        "deadline": (now + timedelta(days=VETO_DAYS)).isoformat(),
                    },
                )
        # COMMIT ДО УВЕДОМЛЕНИЙ: политика и журнал обязаны остаться, даже если
        # центр уведомлений ниже упадёт (шапка модуля).
        await db.commit()
        for ход in ходы:
            c = по_правилам.get(ход.rule, RuleCounts())
            результаты.append(
                await notify_svc.notify(
                    db,
                    kind=KIND_PROMOTED if ход.up else KIND_DEGRADED,
                    body=notification_body(
                        ход, c, deadline=now + timedelta(days=VETO_DAYS) if ход.announce else None
                    ),
                    entity_id=ход.rule,
                )
            )
            log.info(
                "rule_policy.moved",
                rule=ход.rule,
                from_policy=ход.from_policy,
                to_policy=ход.to_policy,
                announce=ход.announce,
                why=ход.why,
            )
        await db.commit()
    for r in результаты:
        await notify_svc.deliver(redis, r)
    return ходы


def register(scheduler: Any) -> None:
    """Понедельник 02:10 UTC = 05:10 МСК — после воронки (01:40), чтобы читать
    те же строки, что она уже посчитала, и до смены."""
    scheduler.add_job(
        review_rule_policies,
        CronTrigger(day_of_week="mon", hour=2, minute=10, timezone="UTC"),
        id=JOB_ID,
        **DEFAULTS,
    )
