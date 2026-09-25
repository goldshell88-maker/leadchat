"""Воронка адресов: сколько адресов из переписки дошло до карточки (18.09).

ЗАЧЕМ. С 18.09 адрес привязывается к карточке без человека — оператор
ничего не подтверждает. Единственный способ узнать, что автоматика делает
это хуже, чем вчера, — считать: у скольких диалогов клиент назвал что-то
похожее на адрес, у скольких из них разбор завёл строку, у скольких адрес лёг
в карточку и с какой степенью, сколько раз человек правил автоадрес руками.
Раз в неделю, снимком на ISO-неделю; падение доли — тревога в центр
уведомлений и красная строка в мониторе «Внешние сервисы».

ОДНО ОПРЕДЕЛЕНИЕ НА ВСЕХ: задача планировщика, команда `address-funnel` и
строка монитора зовут `measure`/`compare` отсюда и ничего не считают сами.

ЧТО СЧИТАЕТСЯ И КАК (партиция карточки сходится по построению):
`card = card_auto_exact + card_auto_approx + card_auto_text + card_person + card_unknown`.
Степень источника — только `geocode.card_grade` (контракт п.3): в SQL нет
ни `~approx`, ни `kind`, строки-источники читаются и судятся в Python.

БАЗА НЕИЗМЕНЯЕМАЯ, КАРТОЧКИ — СНИМОК. Реплики (`created_at`) и строки
(`detected_at`) за неделю не меняются, а карточку могут заполнить и через
месяц; повторный пересчёт недели меняет `card_*` и делается только руками.

НИ ОДНОЙ КЛИЕНТСКОЙ ПД: сюда попадают только счётчики; текст реплик
остаётся в запросе и наружу не выходит.

ПО ПРАВИЛАМ (пакет 6.0а, I-5). Второй замер — `measure_rules`: НАКОПИТЕЛЬНО за
`RULE_WINDOW_WEEKS` недель по каждому правилу `trace.rule` — сколько решений,
сколько из них осудил судья (`trace.judge`, I-4) и как, сколько приняли или
отвергли люди. По нему задача `rule_policy_weekly` двигает политику правила по
лестнице §0.3 интервалом Уилсона (`wilson_bounds`). Снимком недели не хранится:
окно скользящее, а не недельное.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message
from app.models.address_funnel import AddressFunnelWeek
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_REJECTED
from app.services import address_parse, dialect, geocode
from app.services.audit import MSK  # единственное определение МСК в проекте

#: Грубое сито «в реплике похоже на адрес» — НЕЗАВИСИМЫЙ от разбора невод:
#: его смысл — увидеть «адрес есть, строки нет». Без `\m`/`\M` и `\b` (на
#: кириллице у SQLite и Python они ведут себя по-разному): границы — не-буквы,
#: чтобы тот же образец гонялся и на PostgreSQL (`~`, ARE с `(?i)`), и на
#: SQLite в стенде (`REGEXP` через функцию фикстуры).
ADDRESS_LIKE_PATTERN = (
    r"(?i)(^|[^а-яё])(ул|улица|проспект|пр-т|пер|переулок|бульвар|б-р|шоссе|проезд|пл|"
    r"площадь|набережная|наб|квартал|кв-л|мкр|микрорайон|снт|днт|днп|кп|деревня|дер|село|"
    r"пос|посёлок|поселок|пгт|хутор|станица|аул|тракт|линия|аллея|тупик)([^а-яё]|$)"
)
DIGIT_PATTERN = r"[0-9]"
#: Служебная подпись Авито в теле входящего (память avito-file-video-net):
#: «Вот подробности…» — не слова клиента.
SERVICE_TEXT_MARK = "Вот подробности"
#: Действия журнала, по которым считаются «вопрос задан» и «правка руками».
#: `conversation.address_asked` пишет отправитель вопроса (участок C).
ASKED_ACTION = "conversation.address_asked"
EDIT_ACTION = "client.address_edited"
CAPTURE_ACTION = "client.address_captured"

WEEK = timedelta(days=7)
#: Окно замера по правилам (§0.3): пороги считаются по накоплению за восемь
#: недель, а не по одной неделе — объёмы правил 5–19 решений в месяц.
RULE_WINDOW_WEEKS = 8

#: Вердикты судьи (I-4) в `trace.judge.verdict` — договор для задачи судьи:
#: `true` — карточка верна; `false` — ложная (§0.4: ложная карточка, ложное
#: место, ложная строка, другой регион); `place_swap` — «подмена места при
#: точной точке» (другой объект с точкой самого дома: тип улицы, названный
#: клиентом явно, улица той же основы, тёзка-пункт, регион; для правил из
#: `OFF_ON_PLACE_SWAP` — `off` с первого случая, для остальных — ложное);
#: `disputed` — спорное (только литера дома).
#: Тень судится тем же словарём под `trace.shadow.judge.verdict`.
JUDGE_TRUE = "true"
JUDGE_FALSE = "false"
JUDGE_PLACE_SWAP = "place_swap"
JUDGE_DISPUTED = "disputed"
JUDGE_VERDICTS: tuple[str, ...] = (JUDGE_TRUE, JUDGE_FALSE, JUDGE_PLACE_SWAP, JUDGE_DISPUTED)
#: Вердикты, которые идут в счёт ложных у воронки по правилам.
FALSE_VERDICTS: tuple[str, ...] = (JUDGE_FALSE, JUDGE_PLACE_SWAP)
#: Строка заводится по реплике, но `detected_at` может отстать от `created_at`
#: (догрузка истории, сверка) — окно строк шире окна реплик на сутки в обе
#: стороны.
ROW_WINDOW_PAD = timedelta(days=1)

#: Пороги тревоги. Меньше ста диалогов — шум: одна неделя отпуска даёт ±20 п.п.
MIN_DIALOGS_FOR_ALARM = 100
CARD_DROP_PP = 10  # падение доли карточек, п.п., неделя к неделе
ROW_DROP_PP = 10  # падение доли строк — регресс разбора
EDIT_SHARE_ALARM = 0.10  # правок руками поверх автоадреса ТОЙ ЖЕ недели от автокарточек недели
MIN_AUTO_CARDS_FOR_EDIT_ALARM = 50

REASON_CARD_SHARE = "card_share"
REASON_ROW_SHARE = "row_share"
REASON_EDITS = "edits"
REASON_WORDS: dict[str, str] = {
    REASON_CARD_SHARE: f"доля карточек упала на {CARD_DROP_PP} п.п. и больше",
    REASON_ROW_SHARE: f"доля строк разбора упала на {ROW_DROP_PP} п.п. и больше",
    REASON_EDITS: f"правок руками поверх автоадреса — {round(EDIT_SHARE_ALARM * 100)} % и больше",
}


@dataclass(frozen=True, slots=True)
class FunnelCounts:
    """Счётчики одной недели. Поля — и есть форма `counts` в таблице."""

    dialogs: int = 0  # диалоги с адресоподобной репликой клиента в окне
    with_row: int = 0  # из них — со строкой-кандидатом (detected_at в окне ± сутки)
    card: int = 0  # из них — адрес в карточке (снимок на момент расчёта)
    card_auto_exact: int = 0  # автоматика, степень «точная точка»
    card_auto_approx: int = 0  # автоматика, «приблизительная» (в т.ч. места)
    card_auto_text: int = 0  # автоматика, «без точки» (строка улицы)
    card_person: int = 0  # руками (address_set_at) или кнопкой (resolved_by_id у источника)
    card_unknown: int = 0  # источника нет (каскад, адрес до 11.09) или у него нет степени
    edited_after_auto: int = 0  # правок руками поверх адреса, записанного автоматикой
    asked: int = 0  # вопросов об адресе задала система
    # Пакет 6.0а (I-5). Оба — СРЕЗЫ автокарточек, не члены партиции:
    # `card_auto_place` входит в `card_auto_approx` (место — всегда центр),
    # `house_after_place` — в `card_auto_*` дома.
    card_auto_place: int = 0  # автоматика, источник — место (kind=place)
    house_after_place: int = 0  # в карточке дом, а раньше автоматика ставила место того же клиента

    @property
    def card_auto(self) -> int:
        return self.card_auto_exact + self.card_auto_approx + self.card_auto_text

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FunnelCounts:
        """Нет ключа → 0 (старый снимок без нового поля), лишний → мимо."""
        известные = {f.name for f in fields(cls)}
        return cls(**{k: int(v or 0) for k, v in d.items() if k in известные})


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Сохранённая неделя рядом с прошлой — для монитора и команды."""

    week_start: date
    computed_at: datetime
    counts: FunnelCounts
    prev: FunnelCounts | None


# --- недели --------------------------------------------------------------------


def week_bounds(week_start: date) -> tuple[datetime, datetime]:
    """Пн 00:00 МСК → полуоткрытое окно `[since, since + 7 дней)` в UTC."""
    if week_start.weekday() != 0:
        raise ValueError(f"неделя начинается с понедельника, а {week_start} — нет")
    since = datetime.combine(week_start, time.min, tzinfo=MSK).astimezone(UTC)
    return since, since + WEEK


def last_complete_week(now: datetime | None = None) -> date:
    """Понедельник последней ПОЛНОЙ недели по Москве."""
    сегодня = (now or datetime.now(UTC)).astimezone(MSK).date()
    этот_понедельник = сегодня - timedelta(days=сегодня.weekday())
    return этот_понедельник - WEEK


def parse_week(raw: str, now: datetime | None = None) -> date:
    """`YYYY-MM-DD` понедельника; пусто — последняя полная неделя."""
    if not raw.strip():
        return last_complete_week(now)
    неделя = date.fromisoformat(raw.strip())
    week_bounds(неделя)  # не понедельник → ValueError
    return неделя


# --- замер ---------------------------------------------------------------------


def address_like(text: str | None) -> bool:
    """Тот же невод в Python — для стендов и сверки с SQL, один образец."""
    if not text or SERVICE_TEXT_MARK in text:
        return False
    return (
        re.search(ADDRESS_LIKE_PATTERN, text) is not None
        and re.search(DIGIT_PATTERN, text) is not None
    )


def _адресные_диалоги(since: datetime, until: datetime) -> Any:
    """CTE: диалоги, где клиент в окне написал что-то похожее на адрес."""
    return (
        sa.select(Message.conversation_id)
        .where(
            Message.direction == "in",
            Message.sender_type == "client",
            Message.created_at >= since,
            Message.created_at < until,
            Message.body.is_not(None),
            Message.body.regexp_match(ADDRESS_LIKE_PATTERN),
            Message.body.regexp_match(DIGIT_PATTERN),
            sa.not_(Message.body.contains(SERVICE_TEXT_MARK)),
        )
        .distinct()
        .cte("адресные")
    )


async def measure(db: AsyncSession, *, since: datetime, until: datetime) -> FunnelCounts:
    """Счётчики окна `[since, until)`. Только чтение.

    Присоединённая карточка (`merged_into_id`) считается по своей строке без
    перехода к выжившей — осознанное упрощение: адрес при объединении
    переезжает, а диалог остаётся у прежней.
    """
    адресные = _адресные_диалоги(since, until)
    src = aliased(ClientAddressCandidate)
    есть_строка = (
        sa.select(sa.literal(1))
        .where(
            ClientAddressCandidate.conversation_id == адресные.c.conversation_id,
            ClientAddressCandidate.detected_at >= since - ROW_WINDOW_PAD,
            ClientAddressCandidate.detected_at < until + ROW_WINDOW_PAD,
        )
        .exists()
    )
    # «Дом после места» (I-5): у того же клиента есть ДРУГАЯ строка-место,
    # принятая автоматикой (`accepted` без человека). Место, уступившее дому,
    # остаётся `accepted` (воркер отклоняет при уступке только дома) — по
    # этому и видно, что лестница `place → house` сработала.
    было_место = (
        sa.select(sa.literal(1))
        .where(
            ClientAddressCandidate.client_id == Client.id,
            ClientAddressCandidate.id != src.id,
            ClientAddressCandidate.kind == address_parse.KIND_PLACE,
            ClientAddressCandidate.status == CANDIDATE_ACCEPTED,
            ClientAddressCandidate.resolved_by_id.is_(None),
        )
        .exists()
    )
    stmt = (
        sa.select(
            есть_строка.label("with_row"),
            Client.address,
            Client.address_set_at,
            src.id,
            src.resolved_by_id,
            src.kind,
            src.geo_status,
            src.geo_provider,
            src.geo_lat,
            src.geo_lon,
            src.geo_formatted,
            было_место.label("house_after_place"),
        )
        .select_from(адресные)
        .join(Conversation, Conversation.id == адресные.c.conversation_id)
        .join(Client, Client.id == Conversation.client_id)
        .outerjoin(src, src.id == Client.address_candidate_id)
    )
    counts = dict.fromkeys(FunnelCounts().as_dict(), 0)
    for row in (await db.execute(stmt)).all():
        counts["dialogs"] += 1
        if row.with_row:
            counts["with_row"] += 1
        if row.address is None:
            continue
        counts["card"] += 1
        источник_есть = row.id is not None
        if row.address_set_at is not None or (источник_есть and row.resolved_by_id is not None):
            counts["card_person"] += 1
            continue
        степень = (
            geocode.card_grade(
                row.kind,
                row.geo_status,
                row.geo_provider,
                row.geo_lat,
                row.geo_lon,
                row.geo_formatted,
            )
            if источник_есть
            else None
        )
        if степень == geocode.GRADE_EXACT:
            counts["card_auto_exact"] += 1
        elif степень == geocode.GRADE_APPROX:
            counts["card_auto_approx"] += 1
        elif степень == geocode.GRADE_TEXT:
            counts["card_auto_text"] += 1
        else:
            counts["card_unknown"] += 1
            continue
        # Срезы автокарточки со степенью: место и «дом после места».
        if row.kind == address_parse.KIND_PLACE:
            counts["card_auto_place"] += 1
        elif row.house_after_place:
            counts["house_after_place"] += 1
    counts["edited_after_auto"] = await _правок_поверх_авто(db, since, until)
    counts["asked"] = await _вопросов(db, since, until)
    return FunnelCounts(**counts)


async def _правок_поверх_авто(db: AsyncSession, since: datetime, until: datetime) -> int:
    """Ручная правка адреса, который до неё — ТОЙ ЖЕ неделей — записала автоматика.

    Признак «записала автоматика» — строка журнала о том же тексте без
    человека (`user_id IS NULL`: автозапись, пересборка частей); кнопка
    «Подтвердить» пишет с человеком и сюда не попадает. Автозапись берётся из
    того же окна `[since, until)`, что и правка: тревога `edits` делит это
    число на автокарточки недели (`card_auto`), и правка над автоадресом
    прошлого месяца в числителе завышала бы долю — одна дата-рамка в числителе
    и знаменателе. Индекс — `idx_audit_entity (entity, entity_id, created_at)`.
    """
    e = aliased(AuditLog)
    a = aliased(AuditLog)
    было_автоматикой = (
        sa.select(sa.literal(1))
        .where(
            a.entity == "client",
            a.entity_id == e.entity_id,
            a.created_at >= since,
            a.created_at < e.created_at,
            a.action.in_((CAPTURE_ACTION, EDIT_ACTION)),
            a.user_id.is_(None),
            a.details["address"].as_string() == e.details["previous"].as_string(),
        )
        .exists()
    )
    stmt = (
        sa.select(sa.func.count())
        .select_from(e)
        .where(
            e.action == EDIT_ACTION,
            e.created_at >= since,
            e.created_at < until,
            e.details["source"].as_string() == "manual",
            было_автоматикой,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def _вопросов(db: AsyncSession, since: datetime, until: datetime) -> int:
    stmt = (
        sa.select(sa.func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == ASKED_ACTION,
            AuditLog.created_at >= since,
            AuditLog.created_at < until,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


# --- по правилам (пакет 6.0а, I-5) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleCounts:
    """Счётчики одного правила `trace.rule` за скользящее окно.

    `n` — боевые решения (строка судима правилом: `trace.rule`, время —
    `geo_checked_at`); `judged/false/disputed/place_swap` — вердикты судьи по
    ним (`trace.judge.verdict`); `accepted_by_hand` — предложения правила
    (`trace.suggest`), принятые оператором; `rejected_by_hand` — отказ человека
    от карточки правила: «Не адрес»/«Адрес неверный»/стёр (строка `rejected` с
    `resolved_by_id`) или заменил адрес ДРУГИМ (правка руками по журналу, у
    которой новый текст не содержит прежнего); `edited_by_hand` — любая правка
    руками поверх карточки правила, в том числе дописанная квартира (§0.4:
    правка ≠ ошибка — в счёт ложных не идёт, только в отчёт); `shadow_*` — то
    же для тени (`trace.shadow.rule`, `trace.shadow.judge`); `first_at` —
    первое решение правила в окне, боевое или теневое (по нему — «≥ 2 недели»).
    """

    n: int = 0
    judged: int = 0
    false: int = 0
    disputed: int = 0
    place_swap: int = 0
    accepted_by_hand: int = 0
    rejected_by_hand: int = 0
    edited_by_hand: int = 0
    shadow_n: int = 0
    shadow_judged: int = 0
    shadow_false: int = 0
    first_at: datetime | None = None

    @property
    def signals(self) -> int:
        """Решения, о которых что-то известно: судья + нажатия людей (§0.3)."""
        return self.judged + self.accepted_by_hand + self.rejected_by_hand

    @property
    def false_total(self) -> int:
        """Ложных в счёт Уилсона: судья + отказы людей (правка руками — улика)."""
        return self.false + self.rejected_by_hand

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        d["first_at"] = self.first_at.isoformat() if self.first_at else None
        return d


def wilson_bounds(k: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Интервал Уилсона 95 % для доли `k/n`; `n = 0` — ничего не известно,
    `(0, 1)`. Пороги §0.3 читают верхнюю границу на повышение и нижнюю на
    понижение: при целевых 2 % «1 из 50» иначе понижало бы правило в 64 %
    недель (скептик С-1)."""
    if n <= 0:
        return 0.0, 1.0
    k = max(0, min(k, n))
    p = k / n
    z2 = z * z
    центр = (p + z2 / (2 * n)) / (1 + z2 / n)
    разброс = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, центр - разброс), min(1.0, центр + разброс)


def checked_at_iso(когда: datetime | None) -> str | None:
    """`geo_checked_at` строки в том виде, в каком судья кладёт его в
    `trace.judge.checked_at`, — ОДНО определение на писателя и читателей: у
    стенда SQLite время приходит наивным, у PostgreSQL — с поясом, и сравнивать
    их можно только через одну и ту же нормализацию в UTC."""
    if когда is None:
        return None
    if когда.tzinfo is None:
        когда = когда.replace(tzinfo=UTC)
    return когда.astimezone(UTC).isoformat()


def judge_is_fresh(judge: Any, geo_checked_at: datetime | None) -> bool:
    """Вердикт судьи относится к НЫНЕШНЕМУ суду строки (пакет 6.0б, B.5).

    Суд оставляет `judge` на месте при пересуде (`verdict_trace` не трогает
    ключи судьи), поэтому вердикт привязан к суду временем: `checked_at`
    строго равен `geo_checked_at` строки (`None == None` — только у строки, у
    которой суда не было и нет). Иначе судили другое решение — строка снова в
    очереди судьи, а в воронке не считается осуждённой. Шортката «`checked_at`
    пуст — свежий» нет нарочно: такой вердикт пережил бы пересуд и считался
    бы новому правилу (ревью #4); строк старше поля нет — `geo_checked_at` и
    судья родились с записями `checked_at`. Слово вне словаря вердиктов свежим
    не бывает: воронка не должна считать его за «верно».
    """
    if not isinstance(judge, Mapping) or judge.get("verdict") not in JUDGE_VERDICTS:
        return False
    return judge.get("checked_at") == checked_at_iso(geo_checked_at)


def _вердикт(
    trace: Mapping[str, Any] | None, *путь: str, checked_at: datetime | None
) -> str | None:
    """`trace.judge.verdict` (или `trace.shadow.judge.verdict`) словом — только
    из словаря вердиктов и только свежий (`judge_is_fresh`)."""
    узел: Any = trace
    for ключ in путь:
        if not isinstance(узел, Mapping):
            return None
        узел = узел.get(ключ)
    if not judge_is_fresh(узел, checked_at):
        return None
    return str(узел["verdict"])


def address_changed(previous: str | None, new: str | None) -> bool:
    """Правка руками — замена, а не дописывание: новый текст не содержит
    прежнего («улица Ленина, 5» → «улица Ленина, 5, кв 3» — дописали, не
    ложное; → «проспект Мира, 10» или пусто — отказ)."""
    было = " ".join((previous or "").lower().split())
    стало = " ".join((new or "").lower().split())
    return not стало or было not in стало


def _пусто() -> dict[str, Any]:
    return {f.name: 0 for f in fields(RuleCounts)} | {"first_at": None}


async def measure_rules(
    db: AsyncSession, *, until: datetime, weeks: int = RULE_WINDOW_WEEKS
) -> dict[str, RuleCounts]:
    """Счётчики по правилам за `[until − weeks, until)`. Только чтение.

    Строки читаются с `trace` целиком и разбираются в Python: имя правила
    лежит в двух местах (`rule` и `shadow.rule`), вердикт судьи — в третьем,
    а строк со следом за восемь недель — тысячи, не миллионы. Отбор в SQL —
    по времени суда и по наличию имени правила (`trace["rule"].as_string()`,
    не `@>`: на стенде SQLite).
    """
    since = until - weeks * WEEK
    a = ClientAddressCandidate
    rows = (
        await db.execute(
            sa.select(a.id, a.trace, a.status, a.resolved_by_id, a.geo_checked_at).where(
                a.geo_checked_at >= since,
                a.geo_checked_at < until,
                a.trace.is_not(None),
                sa.or_(
                    a.trace["rule"].as_string().is_not(None),
                    a.trace["shadow"]["rule"].as_string().is_not(None),
                ),
            )
        )
    ).all()
    счёт: dict[str, dict[str, Any]] = {}
    # Правило боевого решения по id строки (id — строкой: в журнале он строкой).
    правило_строки: dict[str, str] = {}
    # Строки, отказанные человеком САМИ (статус): стирание адреса руками
    # отказывает строку-источник, и та же правка есть в журнале — считать один раз.
    отказаны_статусом: set[str] = set()

    def _раньше(c: dict[str, Any], когда: datetime | None) -> None:
        if когда is None:
            return
        когда = когда.replace(tzinfo=UTC) if когда.tzinfo is None else когда
        if c["first_at"] is None or когда < c["first_at"]:
            c["first_at"] = когда

    for r in rows:
        trace = r.trace if isinstance(r.trace, Mapping) else {}
        имя = trace.get("rule")
        if isinstance(имя, str) and имя:
            c = счёт.setdefault(имя, _пусто())
            c["n"] += 1
            _раньше(c, r.geo_checked_at)
            правило_строки[str(r.id)] = имя
            вердикт = _вердикт(trace, "judge", checked_at=r.geo_checked_at)
            if вердикт is not None:
                c["judged"] += 1
                c["false"] += вердикт in FALSE_VERDICTS
                c["place_swap"] += вердикт == JUDGE_PLACE_SWAP
                c["disputed"] += вердикт == JUDGE_DISPUTED
            if r.resolved_by_id is not None:
                if r.status == CANDIDATE_ACCEPTED and trace.get("suggest") is True:
                    c["accepted_by_hand"] += 1
                elif r.status == CANDIDATE_REJECTED:
                    c["rejected_by_hand"] += 1
                    отказаны_статусом.add(str(r.id))
        тень = trace.get("shadow")
        if isinstance(тень, Mapping) and isinstance(тень.get("rule"), str) and тень["rule"]:
            c = счёт.setdefault(тень["rule"], _пусто())
            c["shadow_n"] += 1
            _раньше(c, r.geo_checked_at)
            вердикт = _вердикт(тень, "judge", checked_at=r.geo_checked_at)
            if вердикт is not None:
                c["shadow_judged"] += 1
                c["shadow_false"] += вердикт in FALSE_VERDICTS
    if правило_строки:
        await _правки_руками_по_правилам(db, since, until, правило_строки, отказаны_статусом, счёт)
    return {имя: RuleCounts(**c) for имя, c in sorted(счёт.items())}


async def _правки_руками_по_правилам(
    db: AsyncSession,
    since: datetime,
    until: datetime,
    правило_строки: Mapping[str, str],
    отказаны_статусом: set[str],
    счёт: dict[str, dict[str, Any]],
) -> None:
    """Правка руками поверх карточки правила — через журнал (I-5): автозапись
    `client.address_captured`/`address_edited` без человека несёт
    `details.candidate_id`; правка `client.address_edited source=manual` того же
    клиента с `previous`, равным тексту автозаписи, — правка ПОВЕРХ неё. После
    правки `address_candidate_id` карточки пуст, поэтому связь только так.
    Замена другим текстом — в `rejected_by_hand` (одна строка — один отказ,
    даже если её уже отказал статус), дописывание — только в `edited_by_hand`.
    Автозаписи вне окна не в счёт: их решение не входит в `n`, и отказ по ним
    завышал бы долю."""
    e = AuditLog
    правки = (
        await db.execute(
            sa.select(e.entity_id, e.created_at, e.details).where(
                e.action == EDIT_ACTION,
                e.created_at >= since,
                e.created_at < until,
                e.details["source"].as_string() == "manual",
                e.details["previous"].as_string().is_not(None),
            )
        )
    ).all()
    if not правки:
        return
    a = AuditLog
    автозаписи = (
        await db.execute(
            sa.select(a.entity_id, a.created_at, a.details)
            .where(
                a.entity == "client",
                a.entity_id.in_(sorted({п.entity_id for п in правки if п.entity_id})),
                a.action.in_((CAPTURE_ACTION, EDIT_ACTION)),
                a.user_id.is_(None),
                a.details["candidate_id"].as_string().is_not(None),
                a.created_at < until,
            )
            .order_by(a.created_at)
        )
    ).all()
    по_клиенту: dict[str, list[Any]] = {}
    for з in автозаписи:
        по_клиенту.setdefault(з.entity_id, []).append(з)
    посчитаны: set[str] = set(отказаны_статусом)
    for п in правки:
        d = п.details or {}
        previous = d.get("previous")
        источник = None
        for з in по_клиенту.get(п.entity_id, []):
            if з.created_at < п.created_at and (з.details or {}).get("address") == previous:
                источник = з  # последняя автозапись того же текста до правки
        if источник is None:
            continue
        candidate_id = str((источник.details or {}).get("candidate_id"))
        имя = правило_строки.get(candidate_id)
        if имя is None:
            continue
        c = счёт[имя]
        c["edited_by_hand"] += 1
        if address_changed(previous, d.get("address")) and candidate_id not in посчитаны:
            посчитаны.add(candidate_id)
            c["rejected_by_hand"] += 1


# --- сравнение -------------------------------------------------------------------


def _доля(часть: int, целое: int) -> float:
    return 100.0 * часть / целое if целое else 0.0


def share_pct(часть: int, целое: int) -> int:
    """Доля в процентах для человека; ноль в знаменателе — ноль."""
    return round(_доля(часть, целое))


def compare(prev: FunnelCounts | None, cur: FunnelCounts) -> list[str]:
    """Причины тревоги; пусто — всё ровно. Обе стороны (задача и монитор)
    считают по нему же, чтобы красная строка и уведомление не разошлись."""
    причины: list[str] = []
    if (
        prev is not None
        and prev.dialogs >= MIN_DIALOGS_FOR_ALARM
        and cur.dialogs >= MIN_DIALOGS_FOR_ALARM
    ):
        if _доля(prev.card, prev.dialogs) - _доля(cur.card, cur.dialogs) >= CARD_DROP_PP:
            причины.append(REASON_CARD_SHARE)
        if _доля(prev.with_row, prev.dialogs) - _доля(cur.with_row, cur.dialogs) >= ROW_DROP_PP:
            причины.append(REASON_ROW_SHARE)
    if (
        cur.card_auto >= MIN_AUTO_CARDS_FOR_EDIT_ALARM
        and cur.edited_after_auto / cur.card_auto >= EDIT_SHARE_ALARM
    ):
        причины.append(REASON_EDITS)
    return причины


def reason_words(reasons: list[str]) -> str:
    return ", ".join(REASON_WORDS.get(r, r) for r in reasons)


# --- хранение ----------------------------------------------------------------------


async def store(
    db: AsyncSession, *, week_start: date, counts: FunnelCounts, computed_at: datetime
) -> None:
    """Записать (или перезаписать) снимок недели. БЕЗ commit — транзакцией
    владеет вызывающий (`session_scope` на выходе откатывает)."""
    week_bounds(week_start)
    stmt = (
        dialect.insert(db)(AddressFunnelWeek)
        .values(week_start=week_start, computed_at=computed_at, counts=counts.as_dict())
        .on_conflict_do_update(
            index_elements=[AddressFunnelWeek.week_start],
            set_={"computed_at": computed_at, "counts": counts.as_dict()},
        )
    )
    await db.execute(stmt)


async def stored(db: AsyncSession, week_start: date) -> FunnelCounts | None:
    row = await db.get(AddressFunnelWeek, week_start)
    return None if row is None else FunnelCounts.from_dict(row.counts)


async def latest(db: AsyncSession) -> Snapshot | None:
    """Самая свежая сохранённая неделя рядом с РОВНО предыдущей (`−7 дней`),
    а не «следующей строкой»: при пропущенной неделе соседняя строка —
    позапрошлая, и монитор считал бы падение по разрыву в две недели."""
    row = (
        await db.execute(
            sa.select(AddressFunnelWeek).order_by(AddressFunnelWeek.week_start.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return Snapshot(
        week_start=row.week_start,
        computed_at=row.computed_at,
        counts=FunnelCounts.from_dict(row.counts),
        prev=await stored(db, row.week_start - WEEK),
    )


async def recent(db: AsyncSession, *, weeks: int = 8) -> list[Snapshot]:
    """Последние `weeks` недель, новые сверху; у каждой — прошлая по правилу
    `−7 дней` (лишняя строка читается ради самой старой показанной)."""
    rows = (
        (
            await db.execute(
                sa.select(AddressFunnelWeek)
                .order_by(AddressFunnelWeek.week_start.desc())
                .limit(weeks + 1)
            )
        )
        .scalars()
        .all()
    )
    по_неделе = {r.week_start: FunnelCounts.from_dict(r.counts) for r in rows}
    return [
        Snapshot(
            week_start=r.week_start,
            computed_at=r.computed_at,
            counts=по_неделе[r.week_start],
            prev=по_неделе.get(r.week_start - WEEK),
        )
        for r in rows[:weeks]
    ]
