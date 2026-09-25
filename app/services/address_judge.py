"""Судья карточек адреса — модель отвечает на вопрос §0.4 (пакет 6.0б, I-4/I-7).

ЗАЧЕМ. С 18.09 адрес ложится в карточку без человека, а лестница политик
(`rule_policy_weekly`) двигает правила по доле ложных. Долю ложных кто-то
должен считать: правка руками — не ошибка (дописанная квартира — тоже правка,
§0.4), нажатий операторов на 5–19 решений правила в месяц не хватает. Судья —
та же бесплатная модель, что читает адреса (`address_llm`), но с другим
вопросом: «совпадают ли улица и дом (или место) клиента с адресом карточки?»
Ответ — одно слово из `address_funnel.JUDGE_VERDICTS`, ложится в
`trace.judge` строки (или в `trace.shadow.judge` — для теневого решения), и
дальше его читает только воронка `measure_rules`.

⚠ СУДЬЯ НЕ РЕШАЕТ И НИЧЕГО В КАРТОЧКЕ НЕ МЕНЯЕТ. Пишет один ключ следа, и
только если суд строки за время похода не сменился: `UPDATE … WHERE
geo_checked_at IS NOT DISTINCT FROM <что читали>` — пересуженная строка
получит вердикт о прежнем решении не под новым `rule`. Привязка вердикта к
суду — `judge.checked_at` (`address_funnel.checked_at_iso`), по ней воронка
отличает свежий вердикт от вердикта о прошлом решении; строка без
`geo_checked_at` судится, но не пишется — привязать не к чему. Вердикт об
адресе ИЗВНЕ (файл сухого прогона, `address-judge --in`) ложится в след,
только если правило файла — то же И строка карты из файла равна `geo_formatted`
(бой) или `shadow.key` (тень): иначе вердикт о чужой карточке считался бы
свежим судом нынешнего решения (`_слот_по_ключу`).

КВОТА. Потолок один на читателя и судью (`ADDRESS_LLM_DAILY_LIMIT`, счётчик
`geo:llm:calls:<UTC-день>`), у судьи — своя доля (`ADDRESS_LLM_JUDGE_SHARE`,
счётчик `geo:llm:judge:<UTC-день>`) и запас читателю `ЗАПАС_ЧИТАТЕЛЮ`: живое
чтение адресов приоритетнее, судья ходит, только пока читателю остаётся
пятьдесят запросов. Место в доле занимается ДО похода атомарно, как у карт
(`workers/geocode._занять`): INCR, при переборе DECR — параллельные порции не
проскочат «меньше 300» разом.

ТРАНЗАКЦИЯ — НА СТРОКУ, И СЕССИЯ НЕ ДЕРЖИТ ЕЁ ВО ВРЕМЯ ПОХОДА. `judge_row`
читает строку и речь, отпускает транзакцию (`rollback` — читали, не писали),
идёт к модели (до 200 с), и только потом открывает новую под условный UPDATE;
`commit` — за вызывающим. Урок стенда 19.09: одна транзакция на 500 диалогов
провисела «idle in transaction» два часа и уронила `ALTER TABLE` выкатки.

ЧТО УХОДИТ МОДЕЛИ: реплики клиента диалога ВОКРУГ реплики-источника — до
пяти до неё включительно и не больше `РЕПЛИК_ПОСЛЕ` (3) в пределах суток после
(речь через `voice.speech_sql`, маска `address_llm.mask`, не больше 8/2000
знаков; источник во входе гарантирован — `_речь_диалогов`), строка карты из
карточки и город объявления. Ни имён, ни исходящих. То же окно видит человек
на калибровочной разметке (`calibration_rows`).
"""

from __future__ import annotations

import hashlib
import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import openrouter
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.services import address_funnel, address_llm, app_settings, geocode, voice
from app.services.address_funnel import JUDGE_VERDICTS, WEEK, checked_at_iso, judge_is_fresh
from app.services.conversations import conversation_city
from app.workers.address_llm import llm_calls_key, llm_calls_today

log = structlog.get_logger()

#: Что кладётся в `trace.judge.source` — чтобы отличать вердикт судьи от
#: разметки человеком, если та когда-нибудь ляжет в тот же ключ.
JUDGE_SOURCE = "judge"
#: Запас живому читателю адресов в общем потолке: судья идёт, только если
#: `llm_calls_today + 1 <= потолок − ЗАПАС_ЧИТАТЕЛЮ` (§8.4, E1).
ЗАПАС_ЧИТАТЕЛЮ = 50
#: Реплики клиента ПОСЛЕ реплики-источника, которые ещё видит судья: пункт или
#: поправку («нет, дом 7») называют следующей репликой. Не больше
#: `РЕПЛИК_ПОСЛЕ` непустых в пределах `ОКНО_ПОСЛЕ`, каждая обрезана до
#: `ДЛИНА_РЕПЛИКИ_ПОСЛЕ` знаков — иначе `prepare_messages` (последние 8 реплик,
#: 2000 знаков с новых) вытеснял бы сам источник, и судья говорил «адреса нет».
ОКНО_ПОСЛЕ = timedelta(hours=24)
РЕПЛИК_ПОСЛЕ = 3
ДЛИНА_РЕПЛИКИ_ПОСЛЕ = 300
#: Окно калибровочной выборки: свежие карточки автоматики.
CALIBRATION_DAYS = 90
#: Размеченных И отвеченных судьёй пар, с которых калибровка считается
#: (программа §0.3/I-4: «согласие ≥ 95 % на сотне»); меньше — настройка
#: `judge_agreement` не пишется, сотня добирается следующими запусками по
#: сохранённым вердиктам (`--calibrate` берёт свежий `trace.judge` из следа).
JUDGE_CALIBRATION_N = 100

TARGET_JUDGE = "judge"
TARGET_SHADOW = "shadow"

OUTCOME_JUDGED = "judged"
OUTCOME_SKIPPED_QUOTA = "skipped_quota"
OUTCOME_SKIPPED_NO_SPEECH = "skipped_no_speech"
OUTCOME_FAILED = "failed"
OUTCOME_GONE = "gone"


def judge_calls_key(day: datetime | None = None) -> str:
    """Счётчик доли судьи — сутки UTC, как у общего счётчика читателя
    (`llm_calls_key`): одна доля в одних сутках."""
    return "geo:llm:judge:" + (day or datetime.now(UTC)).strftime("%Y%m%d")


async def judge_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(judge_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("address_judge.counter_read_failed", error=type(exc).__name__)
        return 0


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """Итог суда одной строки. `written` — условный UPDATE прошёл (суд строки
    не сменился за время похода); при `dry_run` и без цели — False."""

    outcome: str
    verdict: str | None = None
    model: str | None = None
    reason: str = ""
    target: str | None = TARGET_JUDGE
    written: bool = False


@dataclass(frozen=True, slots=True)
class Pick:
    """Строка в очереди судьи: что судить (`target` — боевое решение по
    `geo_formatted` или тень по `trace.shadow.key`) и чьё это решение."""

    row_id: uuid.UUID
    rule: str
    target: str = TARGET_JUDGE


@dataclass(frozen=True, slots=True)
class Quota:
    """Снимок квоты для печати: `judge_used/share llm_today/limit`."""

    limit: int | None
    share: int | None
    used_today: int
    judge_used: int

    @property
    def reader_reserved(self) -> bool:
        """Общий потолок с запасом читателю выбран — судья не идёт."""
        return self.limit is not None and self.used_today + 1 > int(self.limit) - ЗАПАС_ЧИТАТЕЛЮ


async def quota(db: AsyncSession, redis: Redis) -> Quota:
    потолок = await app_settings.get(db, app_settings.ADDRESS_LLM_DAILY_LIMIT)
    доля = await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_SHARE)
    return Quota(
        limit=int(потолок) if потолок is not None else None,
        share=int(доля) if доля is not None else None,
        used_today=await llm_calls_today(redis),
        judge_used=await judge_calls_today(redis),
    )


def judge_daily_rows(value: Any) -> int:
    """Строк за ночной прогон — как читает задача: `None`/мусор → умолчание."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        if value is not None:
            log.warning("address_judge.daily_rows_invalid", value=value)
        return app_settings.JUDGE_DAILY_ROWS_DEFAULT
    return int(value)


# --- очередь ------------------------------------------------------------------------


def _тень(trace: Any) -> dict[str, Any] | None:
    тень = trace.get("shadow") if isinstance(trace, dict) else None
    return тень if isinstance(тень, dict) and isinstance(тень.get("rule"), str) else None


def next_target(trace: Any, geo_checked_at: datetime | None) -> tuple[str, str] | None:
    """Что у строки ещё не осуждено: `(цель, правило)` или None.

    Сначала боевое решение (`trace.rule` → `trace.judge`), потом тень
    (`trace.shadow.rule` → `trace.shadow.judge`, по `shadow.key`). Свежесть —
    `judge_is_fresh`: вердикт о прошлом суде (`checked_at` ≠ `geo_checked_at`)
    не в счёт, строка судится снова.
    """
    if not isinstance(trace, dict):
        return None
    правило = trace.get("rule")
    if (
        isinstance(правило, str)
        and правило
        and not judge_is_fresh(trace.get("judge"), geo_checked_at)
    ):
        return TARGET_JUDGE, правило
    тень = _тень(trace)
    if (
        тень is not None
        and тень.get("key")
        and not judge_is_fresh(тень.get("judge"), geo_checked_at)
    ):
        return TARGET_SHADOW, str(тень["rule"])
    return None


def target_for_rule(trace: Any, rule: str) -> str | None:
    """Куда класть вердикт о решении правила `rule` (вход `--in` из сухого
    прогона): боевое решение этим правилом — `judge`, теневое — `shadow`;
    правило строку не решало ни так, ни так — писать некуда (None), вердикт
    только печатается."""
    if not isinstance(trace, dict):
        return None
    if trace.get("rule") == rule:
        return TARGET_JUDGE
    тень = _тень(trace)
    if тень is not None and тень.get("rule") == rule:
        return TARGET_SHADOW
    return None


def rule_target(trace: Any, geo_checked_at: datetime | None, rule: str) -> str | None:
    """Куда судить решение именно `rule`, если оно ещё не осуждено (свежесть —
    `judge_is_fresh`, тень — только с ключом, как у `next_target`); None —
    решения этим правилом у строки нет или оно уже осуждено. В отличие от
    `next_target` не ждёт суда над боевым решением другого правила: тень
    нового правила лежит поверх чужого боя почти всегда (`Shadow.instead`)."""
    target = target_for_rule(trace, rule)
    if target == TARGET_JUDGE:
        return None if judge_is_fresh(trace.get("judge"), geo_checked_at) else target
    if target == TARGET_SHADOW:
        тень = _тень(trace)
        if (
            тень is not None
            and тень.get("key")
            and not judge_is_fresh(тень.get("judge"), geo_checked_at)
        ):
            return target
    return None


def _ключ(строка_карты: Any) -> str:
    """Строка карты для сравнения: пробелы схлопнуты, как в TSV/JSON выгрузок."""
    return " ".join(str(строка_карты or "").split())


def _слот_по_ключу(
    trace: dict[str, Any],
    geo_formatted: str | None,
    card_address: str,
    *,
    for_rule: str | None,
    target: str | None,
) -> str | None:
    """Куда писать вердикт об адресе, пришедшем ИЗВНЕ (`card_address`, файл
    сухого прогона): только в слот, чья строка карты равна этому адресу — иначе
    вердикт о чужой карточке лёг бы свежим судом нынешнего решения, и воронка
    считала бы правило по адресу, которого в карточке нет (ревью #2/#12). С
    `for_rule` слот ищется по имени И ключу (бой прежде тени: `shadow_of`
    допускает одно имя с другим ключом); без него — ключ сверяется со слотом
    `target`. None — писать некуда, вердикт только печатается."""
    ключ = _ключ(card_address)
    тень = _тень(trace)
    if for_rule is not None:
        if trace.get("rule") == for_rule and ключ == _ключ(geo_formatted):
            return TARGET_JUDGE
        if тень is not None and тень.get("rule") == for_rule and ключ == _ключ(тень.get("key")):
            return TARGET_SHADOW
        return None
    if target == TARGET_SHADOW:
        return target if тень is not None and ключ == _ключ(тень.get("key")) else None
    if target == TARGET_JUDGE:
        return target if ключ == _ключ(geo_formatted) else None
    return None


async def pick_rows(
    db: AsyncSession,
    *,
    now: datetime,
    per_rule: int = 50,
    weeks: float = address_funnel.RULE_WINDOW_WEEKS,
    limit: int,
    rule: str | None = None,
) -> list[Pick]:
    """Очередь ночной задачи: решения правил за окно воронки, ещё не осуждённые
    (или пересуженные после суда), без решения человека. По кругу правил —
    каждому правилу достаётся поровну, свежие вперёд, не больше `per_rule` на
    правило за окно и `limit` на прогон. Без `rule` у строки берётся ОДНА цель
    за прогон — сначала бой, потом тень (`next_target`); с `rule`
    (`address-judge --rule`) — все неосуждённые решения этого правила, боевые и
    теневые, не дожидаясь суда над чужим боем (`rule_target`). Одна выборка
    следов за окно: строк со следом за восемь недель — тысячи, разбор в Python
    (имя правила лежит в двух местах, как у `measure_rules`)."""
    a = ClientAddressCandidate
    rows = (
        await db.execute(
            sa.select(a.id, a.trace, a.geo_checked_at)
            .where(
                a.geo_checked_at >= now - weeks * WEEK,
                a.geo_checked_at < now,
                a.trace.is_not(None),
                sa.or_(
                    a.trace["rule"].as_string().is_not(None),
                    a.trace["shadow"]["rule"].as_string().is_not(None),
                ),
                a.resolved_by_id.is_(None),
            )
            .order_by(a.geo_checked_at.desc())
        )
    ).all()
    по_правилам: dict[str, list[Pick]] = {}
    for r in rows:
        if rule is not None:
            target = rule_target(r.trace, r.geo_checked_at, rule)
            if target is None:
                continue
            имя = rule
        else:
            цель = next_target(r.trace, r.geo_checked_at)
            if цель is None:
                continue
            target, имя = цель
        корзина = по_правилам.setdefault(имя, [])
        if len(корзина) < per_rule:
            корзина.append(Pick(row_id=r.id, rule=имя, target=target))
    очереди = [deque(v) for _, v in sorted(по_правилам.items())]
    выбор: list[Pick] = []
    while очереди and len(выбор) < limit:
        for очередь in list(очереди):
            if not очередь:
                очереди.remove(очередь)
                continue
            выбор.append(очередь.popleft())
            if len(выбор) >= limit:
                break
    return выбор


def _карточки_автоматики(now: datetime, *, days: int) -> list[Any]:
    """Условия «карточка записана автоматикой» — те же три, что у аудита и
    воронки (`address-audit-sample`, docs/47 §2)."""
    a = ClientAddressCandidate
    return [
        Client.address.is_not(None),
        Client.address_set_at.is_(None),
        Client.merged_into_id.is_(None),
        a.id == Client.address_candidate_id,
        a.resolved_by_id.is_(None),
        a.resolved_at >= now - timedelta(days=days),
        # Геоточку Авито выбрал сам клиент, речи у неё нет — по репликам её
        # не проверить: судья видел бы окно без адреса и говорил бы «false»
        # (проверка правок 21.09).
        sa.or_(a.geo_provider.is_(None), a.geo_provider != geocode.GEOPOINT_PROVIDER),
    ]


async def sample_for_grade(
    db: AsyncSession,
    *,
    grade: str,
    days: int,
    limit: int,
    now: datetime | None = None,
) -> list[Pick]:
    """Базовый замер I-7: источники карточек автоматики степени `grade`
    (`exact`/`approx`/`text` — судит `geocode.card_grade`, как всюду), свежие
    вперёд по `resolved_at`, ещё не осуждённые нынешним судом. Степень — в
    Python, поэтому потолок режется после фильтра (образец аудита)."""
    if grade not in (geocode.GRADE_EXACT, geocode.GRADE_APPROX, geocode.GRADE_TEXT):
        raise ValueError(f"степень: одна из exact, approx, text, получено «{grade}»")
    now = now or datetime.now(UTC)
    a = ClientAddressCandidate
    условия = _карточки_автоматики(now, days=days)
    # Предфильтр по данным строки — только чтобы не тащить лишнее.
    условия.append(a.geo_lat.is_(None) if grade == geocode.GRADE_TEXT else a.geo_lat.is_not(None))
    rows = (
        await db.execute(
            sa.select(
                a.id,
                a.kind,
                a.geo_status,
                a.geo_provider,
                a.geo_lat,
                a.geo_lon,
                a.geo_formatted,
                a.geo_checked_at,
                a.trace,
            )
            .join(Client, Client.id == a.client_id)
            .where(*условия)
            .order_by(a.resolved_at.desc())
        )
    ).all()
    выбор: list[Pick] = []
    for r in rows:
        степень = geocode.card_grade(
            r.kind, r.geo_status, r.geo_provider, r.geo_lat, r.geo_lon, r.geo_formatted
        )
        if степень != grade:
            continue
        trace = r.trace if isinstance(r.trace, dict) else {}
        if judge_is_fresh(trace.get("judge"), r.geo_checked_at):
            continue  # уже осуждена нынешним судом — второй поход ничего не добавит
        правило = trace.get("rule")
        выбор.append(Pick(row_id=r.id, rule=правило if isinstance(правило, str) else ""))
        if len(выбор) >= limit:
            break
    return выбор


# --- калибровка ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    """Строка на разметку человеком: речь клиента (маска `address_llm.mask`,
    те же реплики, что увидит судья) и адрес карточки (строка карты)."""

    row_id: uuid.UUID
    speech: tuple[str, ...]
    card: str


async def calibration_rows(
    db: AsyncSession, *, limit: int = 100, now: datetime | None = None
) -> list[CalibrationRow]:
    """Случайные карточки автоматики за `CALIBRATION_DAYS` дней с речью —
    выгрузка на разметку (`address-judge --sample`). Случайные, а не свежие:
    калибруют судью на разнообразии, а не на одной неделе одного правила."""
    now = now or datetime.now(UTC)
    a = ClientAddressCandidate
    rows = (
        await db.execute(
            sa.select(a.id, a.conversation_id, a.message_at, a.message_id, a.geo_formatted)
            .join(Client, Client.id == a.client_id)
            .where(*_карточки_автоматики(now, days=CALIBRATION_DAYS), a.geo_formatted.is_not(None))
            .order_by(sa.func.random())
            .limit(limit * 2)
        )
    ).all()
    речь = await _речь_диалогов(db, [(r.conversation_id, r.message_at, r.message_id) for r in rows])
    выбор: list[CalibrationRow] = []
    for r in rows:
        реплики = address_llm.prepare_messages(речь.get(r.conversation_id, []))
        if not реплики:
            continue
        выбор.append(CalibrationRow(row_id=r.id, speech=tuple(реплики), card=r.geo_formatted or ""))
        if len(выбор) >= limit:
            break
    return выбор


async def saved_verdicts(db: AsyncSession, row_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Свежие вердикты судьи из следа (`trace.judge`, `source=judge`,
    `judge_is_fresh`) по строкам — калибровка добирает сотню между запусками
    без второго похода к модели: обрыв по квоте или отказ читателей не
    заставляют пережигать долю заново (ревью #10)."""
    ids = list(row_ids)
    if not ids:
        return {}
    a = ClientAddressCandidate
    rows = (await db.execute(sa.select(a.id, a.trace, a.geo_checked_at).where(a.id.in_(ids)))).all()
    итог: dict[uuid.UUID, str] = {}
    for r in rows:
        судья = r.trace.get("judge") if isinstance(r.trace, dict) else None
        if (
            isinstance(судья, dict)
            and судья.get("source") == JUDGE_SOURCE
            # Вердикт другого промпта — не этот судья: после правки словаря
            # калибровка обязана сходить заново, а не набрать сотню из старых.
            and судья.get("prompt") == prompt_fingerprint()
            and judge_is_fresh(судья, r.geo_checked_at)
        ):
            итог[r.id] = str(судья["verdict"])
    return итог


def prompt_fingerprint() -> str:
    """Отпечаток вопроса судьи в `trace.judge.prompt`: по нему калибровка и
    воронка отличают вердикты нынешнего словаря от прежних."""
    return hashlib.sha1(address_llm.SYSTEM_PROMPT_JUDGE.encode()).hexdigest()[:8]


def agreement(pairs: Iterable[tuple[str | None, str | None]]) -> tuple[int, int]:
    """`(совпало, всего)` по парам «человек, судья»: считаются только пары, где
    оба слова из словаря вердиктов — строка без разметки или без ответа модели
    в знаменатель не идёт."""
    всего = совпало = 0
    for человек, судья in pairs:
        if человек not in JUDGE_VERDICTS or судья not in JUDGE_VERDICTS:
            continue
        всего += 1
        совпало += человек == судья
    return совпало, всего


# --- суд одной строки ---------------------------------------------------------------


async def _речь_диалогов(
    db: AsyncSession,
    окна: Sequence[tuple[uuid.UUID, datetime | None, uuid.UUID | None]],
) -> dict[uuid.UUID, list[str]]:
    """Речь клиента по диалогам одним запросом — окно ВОКРУГ реплики-источника,
    одно на судью и на калибровочную выгрузку (иначе человек размечал бы не
    тот вход, что видит судья). Якорь — `message_at` строки, без него —
    `created_at` реплики `message_id`. «До» — реплики `created_at <= якорь`
    (источник — последняя, как у читателя `workers/address_llm`), «после» — не
    больше `РЕПЛИК_ПОСЛЕ` непустых в пределах `ОКНО_ПОСЛЕ`, каждая обрезана до
    `ДЛИНА_РЕПЛИКИ_ПОСЛЕ`; итог `до[-(MAX_MESSAGES − РЕПЛИК_ПОСЛЕ):] + после`,
    и хвост «после» сбрасывается, пока он вместе с источником не влезает в
    `MAX_CHARS`, — так `prepare_messages` (последние 8, 2000 знаков с новых)
    источник уже не вытеснит. Строка без якоря (ни `message_at`, ни реплики по
    `message_id`) — вся речь, последние `MAX_MESSAGES`, как раньше."""
    if not окна:
        return {}
    якоря = {conv_id: (когда, message_id) for conv_id, когда, message_id in окна}
    rows = (
        await db.execute(
            sa.select(Message.conversation_id, Message.id, Message.created_at, voice.speech_sql())
            .where(
                Message.conversation_id.in_(list(якоря)),
                Message.direction == "in",
            )
            .order_by(Message.created_at.asc())
        )
    ).all()
    по_диалогам: dict[uuid.UUID, list[tuple[uuid.UUID, datetime, str | None]]] = {}
    for conv_id, message_id, когда, текст in rows:
        по_диалогам.setdefault(conv_id, []).append((message_id, когда, текст))
    речь: dict[uuid.UUID, list[str]] = {}
    for conv_id, реплики in по_диалогам.items():
        якорь, message_id = якоря.get(conv_id, (None, None))
        if якорь is None and message_id is not None:
            # Источник ищется среди всех входящих: у голосового без расшифровки
            # речи нет, а якорем он быть обязан.
            якорь = next((когда for mid, когда, _ in реплики if mid == message_id), None)
        непустые = [(когда, т) for _, когда, т in реплики if т and т.strip()]
        if якорь is None:
            if непустые:
                речь[conv_id] = [т for _, т in непустые][-address_llm.MAX_MESSAGES :]
            continue
        до: list[str] = []
        после: list[str] = []
        for когда, текст in непустые:
            if _utc(когда) <= _utc(якорь):
                до.append(текст)
            elif _utc(когда) <= _utc(якорь) + ОКНО_ПОСЛЕ and len(после) < РЕПЛИК_ПОСЛЕ:
                после.append(текст[:ДЛИНА_РЕПЛИКИ_ПОСЛЕ])
        до = до[-(address_llm.MAX_MESSAGES - РЕПЛИК_ПОСЛЕ) :]
        # Потолок знаков `prepare_messages` набирается с новых: источник
        # (последняя из «до») обязан влезть вместе со всем хвостом «после».
        while (
            после
            and до
            and len(_ключ(до[-1])) + sum(len(_ключ(т)) for т in после) > (address_llm.MAX_CHARS)
        ):
            после.pop()
        if до or после:
            речь[conv_id] = до + после
    return речь


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


async def _занять(redis: Redis, доля: int | None) -> bool:
    """Место в доле судьи ДО похода (образец `workers/geocode._занять`).
    Redis упал — идём, как идёт читатель: потолок — наш, не провайдера."""
    ключ = judge_calls_key()
    try:
        await redis.set(ключ, 0, nx=True, ex=2 * 24 * 3600)
        n = int(await redis.incr(ключ))
    except Exception as exc:  # noqa: BLE001
        log.warning("address_judge.counter_failed", error=type(exc).__name__)
        return True
    if доля is None or n <= int(доля):
        return True
    await _вернуть(redis)
    return False


async def _вернуть(redis: Redis) -> None:
    try:
        await redis.decr(judge_calls_key())
    except Exception as exc:  # noqa: BLE001
        log.warning("address_judge.counter_failed", error=type(exc).__name__)


async def judge_row(
    db: AsyncSession,
    redis: Redis,
    row_id: uuid.UUID,
    *,
    target: str | None = TARGET_JUDGE,
    card_address: str | None = None,
    for_rule: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> JudgeResult:
    """Осудить одну строку: вердикт в `trace.judge` (`target=judge`, адрес —
    `geo_formatted`) или в `trace.shadow.judge` (`target=shadow`, адрес —
    `trace.shadow.key`); `card_address` — адрес ИЗВНЕ вместо строки карты
    (файл сухого прогона I-3): судится он, а пишется только в слот, чья строка
    карты ему равна (`_слот_по_ключу`: с `for_rule` — правило файла то же И
    ключ совпал с боем или тенью; иначе `reason="key_mismatch"`, вердикт только
    печатается); `for_rule` без `card_address` — цель по имени правила
    (`target_for_rule`); `target=None` или `dry_run` — судить, но не писать.
    Строка без `geo_checked_at` тоже судится, но не пишется (`reason=
    "no_checked_at"`): вердикт не к чему привязать, а `checked_at: null`
    пережил бы пересуд. Транзакция чтения отпускается до похода; UPDATE
    условный по `geo_checked_at`; `commit` — за вызывающим.

    Итоги: `judged` (вердикт получен; `written` — записан), `skipped_quota`
    (потолок с запасом читателю или доля судьи выбраны), `skipped_no_speech`
    (речи клиента в окне нет), `failed` (читатели не ответили или ответ не из
    словаря), `gone` (строки нет или судить нечего — ни адреса, ни тени).
    """
    now = now or datetime.now(UTC)
    a = ClientAddressCandidate
    строка = (
        await db.execute(
            sa.select(
                a.conversation_id,
                a.message_at,
                a.message_id,
                a.geo_formatted,
                a.geo_checked_at,
                a.trace,
                Conversation.item_city_slug,
                Conversation.item_url,
            )
            .join(Conversation, Conversation.id == a.conversation_id)
            .where(a.id == row_id)
        )
    ).one_or_none()
    if строка is None:
        return JudgeResult(OUTCOME_GONE, target=target, reason="no_row")
    trace = dict(строка.trace) if isinstance(строка.trace, dict) else {}
    checked_at = строка.geo_checked_at
    без_записи: str | None = None  # почему вердикт только печатается
    if card_address is not None and (target is not None or for_rule is not None):
        слот = _слот_по_ключу(
            trace, строка.geo_formatted, card_address, for_rule=for_rule, target=target
        )
        if слот is None:
            без_записи = "key_mismatch"
            log.info(
                "address_judge.key_mismatch",
                candidate_id=str(row_id),
                rule=for_rule,
                target_was=target,
                trace_rule=trace.get("rule"),
            )
        target = слот
    elif for_rule is not None:
        target = target_for_rule(trace, for_rule)
    if target == TARGET_SHADOW:
        тень = _тень(trace)
        if тень is None:
            return JudgeResult(OUTCOME_GONE, target=target, reason="no_shadow")
        адрес = card_address or str(тень.get("key") or "")
    else:
        адрес = card_address or (строка.geo_formatted or "")
    if not адрес.strip():
        return JudgeResult(OUTCOME_GONE, target=target, reason="no_card")
    if target is not None and checked_at is None:
        # Суда, к которому привязать вердикт, нет: писать `checked_at: null`
        # нельзя — пересуд его не снял бы (ревью #4).
        без_записи = "no_checked_at"
        target = None
    # Город объявления — как у читателя: справочник по слагу/ссылке диалога.
    city = conversation_city(
        _Объявление(item_city_slug=строка.item_city_slug, item_url=строка.item_url)
    )
    речь = (
        await _речь_диалогов(db, [(строка.conversation_id, строка.message_at, строка.message_id)])
    ).get(строка.conversation_id, [])
    квота = await quota(db, redis)
    # Читали, не писали: транзакция отпускается ДО похода (шапка модуля).
    await db.rollback()
    if not address_llm.prepare_messages(речь):
        return JudgeResult(OUTCOME_SKIPPED_NO_SPEECH, target=target)
    if квота.reader_reserved or not await _занять(redis, квота.share):
        return JudgeResult(OUTCOME_SKIPPED_QUOTA, target=target)

    запросов = 0

    async def _посчитать() -> None:
        # Общий счётчик — на каждый запрос; доля судьи за первый уже занята
        # `_занять`, дальше — попытки шлюза со следующими моделями.
        nonlocal запросов
        запросов += 1
        try:
            await redis.set(llm_calls_key(), 0, nx=True, ex=2 * 24 * 3600)
            await redis.incr(llm_calls_key())
            if запросов > 1:
                await redis.set(judge_calls_key(), 0, nx=True, ex=2 * 24 * 3600)
                await redis.incr(judge_calls_key())
        except Exception as exc:  # noqa: BLE001
            log.warning("address_judge.counter_failed", error=type(exc).__name__)

    try:
        данные, модель = await openrouter.chat_json(
            address_llm.SYSTEM_PROMPT_JUDGE,
            address_llm.build_judge_message(речь, адрес, city.name if city else None),
            on_request=_посчитать,
        )
    except openrouter.OpenRouterError as exc:
        if запросов == 0:
            await _вернуть(redis)  # похода не было — место в доле не потрачено
        log.warning("address_judge.failed", kind=exc.kind, status=exc.status)
        return JudgeResult(OUTCOME_FAILED, target=target, reason=exc.kind)
    суждение = address_llm.parse_judgement(данные)
    if суждение is None:
        log.warning("address_judge.bad_verdict", model=модель)
        return JudgeResult(OUTCOME_FAILED, model=модель, target=target, reason="bad_verdict")
    log.info(
        "address_judge.judged",
        candidate_id=str(row_id),
        target=target,
        verdict=суждение.verdict,
        model=модель,
    )
    if dry_run or target is None:
        return JudgeResult(
            OUTCOME_JUDGED,
            verdict=суждение.verdict,
            model=модель,
            reason=без_записи or суждение.reason,
            target=target,
        )
    запись = {
        "verdict": суждение.verdict,
        "at": now.isoformat(),
        "model": модель,
        "checked_at": checked_at_iso(checked_at),
        "source": JUDGE_SOURCE,
        "prompt": prompt_fingerprint(),
    }
    if target == TARGET_SHADOW:
        trace["shadow"] = {**trace["shadow"], "judge": запись}
    else:
        trace["judge"] = запись
    result = await db.execute(
        sa.update(a)
        .where(a.id == row_id, a.geo_checked_at.is_not_distinct_from(checked_at))
        .values(trace=trace)
        .execution_options(synchronize_session=False)
    )
    записано = bool(getattr(result, "rowcount", 0))
    if not записано:
        log.info("address_judge.stale", candidate_id=str(row_id), target=target)
    return JudgeResult(
        OUTCOME_JUDGED,
        verdict=суждение.verdict,
        model=модель,
        reason=суждение.reason,
        target=target,
        written=записано,
    )


@dataclass(frozen=True, slots=True)
class _Объявление:
    """Ровно то, что читает `conversation_city`: слаг города и ссылка объявления."""

    item_city_slug: str | None
    item_url: str | None
