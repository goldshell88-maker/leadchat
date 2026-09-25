"""Один вопрос клиенту об адресе без человека (владелец 18.09).

Когда клиент описал проблему, а адреса в диалоге нет и никто (ни оператор,
ни бот, ни система) его не спросил и никто не ответил клиенту за
`address_ask.delay_sec`, система сама отправляет ОДНО исходящее от аккаунта с
текстом-настройкой. Раз на диалог навсегда (`conversations.address_asked_at`).
Ответ клиента поднимается адресом существующим путём: `inbound._оператор_
спросил_адрес` считает вопросом любое исходящее (фильтр только `direction`).

Здесь — то, что импортируют трое: `inbound` (ворота постановки), задача
`workers/address_ask.py` (лестница замков) и `cli.py address-ask-dry-run`
(сухой прогон на боевой переписке ТЕМИ ЖЕ функциями, а не своей копией счёта
букв, стоп-списка и замка по строкам адреса — класс «два пути считают одно
поле по-разному»).

`arq` и `inbound` импортируются лениво: первый — как в `geocode_queue`,
второй — иначе цикл inbound → address_ask → inbound (прецедент чтения
приватного имени — `cli.py` зовёт `inbound._оператор_спросил_адрес`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.models.client import CANDIDATE_REJECTED
from app.services import address_parse, app_settings, geocode, voice
from app.services.clients import candidate_grade

log = structlog.get_logger("app.address_ask")

ADDRESS_ASK_JOB = "address_ask_run"
#: Свежесть на постановке: сверка истории (`workers/reconciliation.py`) заводит
#: пропущенные живые сообщения тем же путём и часами позже — спрашивать адрес
#: по вчерашней реплике нельзя. Тот же порог, что у `_fresh` эха (inbound.py).
FRESH_WINDOW = timedelta(minutes=15)
#: Окно строки адреса и входящих для порога знаков и отказа.
ОКНО = timedelta(hours=24)
#: Сколько исходящих смотреть на «уже спрашивали».
FEED_LIMIT = 50
#: Переходный замок строки адреса: карта ещё не сказала — задача переставляет
#: себя (как `history_loading`), а не выходит.
GEO_PENDING_LOCK = "geo_pending"
#: Статусы «карта ещё проверяет» ДЛЯ ВОПРОСА: `geocode.CHECKING_STATUSES` без
#: `no_city`. Воркер к `no_city` возвращается (город объявления может приехать
#: позже), но для вопроса это не ожидание, а сам повод спросить: текст
#: вопроса просит «город или посёлок» ради этого случая (`geocode.ask_reason`
#: → ASK_CITY), и ждать его — значит не спросить никогда (ревью 19.09, №7/№8).
GEO_CHECKING_FOR_ASK: frozenset[str | None] = geocode.CHECKING_STATUSES - {geocode.GEO_NO_CITY}
#: Уровни строки, ради которой стоит подождать карту: A и B. Невод уровня C
#: (44–70 % ложных по замеру 18.09) вопрос не держит — подтвердит карта, у
#: строки появится степень, и следующая задача выйдет на `candidate_exists`.
_УРОВНИ_ОЖИДАНИЯ = frozenset({address_parse.LEVEL_A, address_parse.LEVEL_B})
#: Вложение, которое само по себе описание (`attachments[].avito_type`): фото
#: блока питания — описание; engine считает фото ответом (runtime.py).
_ОПИСЫВАЮЩИЕ_ВЛОЖЕНИЯ = frozenset({"image", "voice", "video"})
_ЗНАК = re.compile(r"[0-9a-zа-яё]", re.IGNORECASE)
#: Отказ/отмена клиента — вопрос об адресе неуместен (замер месяца: 19/88
#: not_found без адреса — «узнал цену и пропал»/«передумал»). Граница слова перед
#: «не» обязательна: «мне нужно» ≠ «не нужно» (память leadbot-4skrina-3008, 395
#: живых реплик); «я не передумал» — не отказ. Список намеренно короткий и
#: именованный: ложные срабатывания покажет сухой прогон, не догадки.
_ОТКАЗ = re.compile(
    r"(?<![а-яё])(?:"
    r"не\s+(?:нужн|надо|актуальн|треб|интерес)|"
    r"больше\s+не\s+(?:нужн|надо|актуальн)|"
    r"(?<!не )передумал|отбой|отмен(?:а|ите|яю|яем|ил|ила)(?![а-яё])|"
    r"уже\s+(?:решил|сделал|починил|нашл|вызвал|заказал)|"
    r"спасибо,?\s+не\s+(?:надо|нужно)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AskSettings:
    """Четыре ключа `address_ask.*` в виде, пригодном для решения."""

    enabled: bool  # эффективное: address_ask.enabled AND address_detect.enabled
    delay_sec: int
    text: str
    min_chars: int


@dataclass(frozen=True, slots=True)
class InboundRow:
    """То, что нужно текстовым замкам; без модели — чистые функции
    тестируются без базы."""

    id: uuid.UUID
    body: str | None
    attachments: list[Any]
    voice_transcript: str | None
    #: Состояние расшифровки — пара к тексту: без него `client_declined` брал бы
    #: расшифровку и в `running`/`failed`, где текста по контракту нет, а
    #: `voice.speech_of` держит этот контракт явно. Умолчание None — строки
    #: строятся именованно (сервис и тесты), и старым вызовам поле не нужно.
    voice_transcript_status: str | None = None


def _число(value: Any, default: int) -> int:
    """`None`/мусор в таблице (правили руками) → умолчание Spec."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def settings_from(values: Mapping[str, Any]) -> AskSettings:
    """ЕДИНСТВЕННЫЙ читатель четырёх ключей для РЕШЕНИЯ.

    `None`/мусор у чисел → умолчание Spec; `enabled` = address_ask.enabled AND
    address_detect.enabled (выключили разбор — спрашивать незачем: ответ никто
    не прочтёт). Экран настроек читает хранимые значения сам
    (`routes/settings.py`), не отсюда.
    """
    specs = app_settings.SPECS
    text = values.get(app_settings.ADDRESS_ASK_TEXT)
    return AskSettings(
        enabled=bool(values.get(app_settings.ADDRESS_ASK_ENABLED))
        and bool(values.get(app_settings.ADDRESS_DETECT_ENABLED)),
        delay_sec=_число(
            values.get(app_settings.ADDRESS_ASK_DELAY_SEC),
            int(specs[app_settings.ADDRESS_ASK_DELAY_SEC].default),
        ),
        text=str(text) if text else str(specs[app_settings.ADDRESS_ASK_TEXT].default),
        min_chars=_число(
            values.get(app_settings.ADDRESS_ASK_MIN_CHARS),
            int(specs[app_settings.ADDRESS_ASK_MIN_CHARS].default),
        ),
    )


def enqueue_delay_sec(values: Mapping[str, Any]) -> int | None:
    """Через сколько секунд ставить проверку; None — не ставить (выключено)."""
    s = settings_from(values)
    return s.delay_sec if s.enabled else None


def text_is_recognizable(text: str) -> bool:
    """Ответ на этот текст разбор прочтёт адресом: в нём есть слово из
    `inbound._ВОПРОС_ОБ_АДРЕСЕ` — один словарь на вопрос и ответ."""
    from app.services.inbound import _ВОПРОС_ОБ_АДРЕСЕ

    return bool(text.strip()) and _ВОПРОС_ОБ_АДРЕСЕ.search(text) is not None


def recognizable_words() -> str:
    """«адрес, подъезд, этаж, квартира, улица, где находитесь, куда подъехать» —
    для текста отказа ручки и подписи замка; источник —
    `inbound._ВОПРОС_ОБ_АДРЕСЕ_СЛОВА`."""
    from app.services.inbound import _ВОПРОС_ОБ_АДРЕСЕ_СЛОВА

    return ", ".join(_ВОПРОС_ОБ_АДРЕСЕ_СЛОВА)


def _aware(dt: datetime) -> datetime:
    """SQLite отдаёт naive; как `inbound._aware_utc`."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def is_fresh(created_at: datetime, *, now: datetime) -> bool:
    return now - _aware(created_at) < FRESH_WINDOW


def field_lock(conv: Conversation) -> str | None:
    """Замки по ПОЛЯМ диалога — без запросов.

    ОДНА функция на два вызова: ворота постановки в `inbound` (снимок без
    замка, чтобы не плодить задач) и первая ступень лестницы в задаче (под
    FOR UPDATE — там решает). Порядок — реестр `workers/address_ask.ЗАМКИ`.
    """
    if conv.status == "closed":
        return "closed"
    if conv.address_asked_at is not None:
        return "already_asked"
    if conv.claimed_by_id is not None or conv.assignee_id is not None:
        return "claimed_by_human"
    if conv.bot_active:
        return "bot_leads"
    return None


def card_lock(client: Client | None) -> str | None:
    """Замки по КАРТОЧКЕ: чёрный список и адрес.

    Чёрный список — ПЕРВЫМ: пометка (`Client.blocked_at`, models/client.py)
    снимает требование внимания — `inbound.leave_queue` уводит диалог из
    очереди, никто не назначен, бот не ведёт, — и без этого замка «никто не
    ответил за delay_sec» для помеченного выполнялось бы всегда: вопрос от
    имени компании уходил бы ровно тем, с кем решили не работать, а доставка
    возвращала бы снятое «ждёт». Порядок важен для журнала и сухого прогона:
    помеченный клиент с адресом в карточке — `client_blocked`, не
    `card_has_address`. `client is None` — по карточке не блокируем (диалог
    без карточки — не наш случай).
    """
    if client is None:
        return None
    if client.blocked_at is not None:
        return "client_blocked"
    return "card_has_address" if client.address else None


def candidate_lock(rows: Sequence[ClientAddressCandidate]) -> str | None:
    """Замок по СТРОКАМ адреса диалога за окно — один на задачу и сухой прогон.

    Контракт 18.09 п.5: вопрос глушит строка СО СТЕПЕНЬЮ (единственный судья
    — `clients.candidate_grade`, свой предикат не заводим). Строка уровня A/B,
    которую карта ещё проверяет (`GEO_CHECKING_FOR_ASK`), — переходный замок
    `GEO_PENDING_LOCK`: задача подождёт вердикта, как ждёт историю. Окончательный
    отказ без степени (`not_found`, `house_missing` без строки улицы, `no_city`,
    `ambiguous`…) — НЕ замок: по контракту п.2 это и есть «вопрос клиенту».
    Смотрим ВСЕ строки окна, не последнюю: последняя `not_found`, а раньше
    `exact` — адрес в диалоге есть.
    """
    if any(candidate_grade(r) is not None for r in rows):
        return "candidate_exists"
    if any(r.level in _УРОВНИ_ОЖИДАНИЯ and r.geo_status in GEO_CHECKING_FOR_ASK for r in rows):
        return GEO_PENDING_LOCK
    return None


def _знаков(text: str | None) -> int:
    return len(_ЗНАК.findall(text or ""))


def _описывающее_вложение(attachments: Any) -> bool:
    if not isinstance(attachments, list):
        return False
    return any(
        isinstance(a, dict) and a.get("avito_type") in _ОПИСЫВАЮЩИЕ_ВЛОЖЕНИЯ for a in attachments
    )


def client_described(rows: Sequence[InboundRow], *, min_chars: int) -> tuple[bool, int]:
    """(описал ли, сколько знаков насчитали).

    Любое вложение с `avito_type` из `_ОПИСЫВАЮЩИЕ_ВЛОЖЕНИЯ` → (True, n).
    Иначе сумма букв/цифр `body` и `voice_transcript` ≥ min_chars.
    `min_chars=0` → True. Пустой список → (False, 0). Число возвращается
    ради журнала `address_ask.skipped chars=` — распределение и есть
    калибровка порога.
    """
    if not rows:
        return False, 0
    знаков = sum(_знаков(r.body) + _знаков(r.voice_transcript) for r in rows)
    if any(_описывающее_вложение(r.attachments) for r in rows):
        return True, знаков
    return знаков >= min_chars, знаков


def client_declined(rows: Sequence[InboundRow]) -> bool:
    """Хоть одно входящее за окно попало в `_ОТКАЗ`.

    Консервативно: лучше не спросить у того, кто написал «диагностика не
    нужна, сразу ремонт», чем спросить у отказавшегося. Речь — через
    `voice.speech_of`: «спасибо, не надо», сказанное голосом, — тот же отказ,
    и та же консервативность.
    """
    for r in rows:
        речь = voice.speech_of(r.body, r.voice_transcript, r.voice_transcript_status).text
        if речь and _ОТКАЗ.search(речь) is not None:
            return True
    return False


async def address_rows(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    since: datetime,
    before: datetime | None = None,
) -> list[ClientAddressCandidate]:
    """Не отклонённые строки адреса ЭТОГО диалога не старше `since` (и не
    моложе `before`, если дан), новые первыми — тот же отбор, что у
    `clients.latest_address_candidate`, без `.limit(1)`: `candidate_lock` судит
    все строки окна. `before` нужен сухому прогону (состояние на момент T);
    задача верхней границы не ставит — строка, заведённая мгновением позже
    её «сейчас», тоже строка этого диалога."""
    условия = [
        ClientAddressCandidate.client_id == client_id,
        ClientAddressCandidate.conversation_id == conversation_id,
        ClientAddressCandidate.status != CANDIDATE_REJECTED,
        ClientAddressCandidate.detected_at >= since,
    ]
    if before is not None:
        условия.append(ClientAddressCandidate.detected_at <= before)
    return list(
        (
            await db.execute(
                sa.select(ClientAddressCandidate)
                .where(*условия)
                .order_by(ClientAddressCandidate.detected_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def inbound_rows(
    db: AsyncSession, conversation_id: uuid.UUID, *, since: datetime, before: datetime
) -> list[InboundRow]:
    """`direction=='in'`, `since <= created_at <= before`, свежие первыми,
    не больше FEED_LIMIT. `before` нужен сухому прогону (состояние на момент
    T), в задаче — `now`."""
    rows = (
        await db.execute(
            sa.select(
                Message.id,
                Message.body,
                Message.attachments,
                Message.voice_transcript,
                Message.voice_transcript_status,
            )
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == "in",
                Message.created_at >= since,
                Message.created_at <= before,
            )
            .order_by(Message.created_at.desc())
            .limit(FEED_LIMIT)
        )
    ).all()
    return [
        InboundRow(
            id=id_,
            body=body,
            attachments=attachments or [],
            voice_transcript=transcript,
            voice_transcript_status=status,
        )
        for id_, body, attachments, transcript, status in rows
    ]


async def asked_in_feed(db: AsyncSession, conversation_id: uuid.UUID, *, before: datetime) -> bool:
    """Последние FEED_LIMIT исходящих (`direction=='out'`, `body IS NOT NULL`,
    `created_at <= before`) — хоть одно попало в `inbound._ВОПРОС_ОБ_АДРЕСЕ`.

    Без окна времени (оператор мог спросить вчера) и без фильтра `sender_type`
    (вопрос бота — тоже вопрос; недоставленный вопрос системы — тоже). ⚠ Правило
    шире слова «адрес»: гасят и «этаж», «квартир», «где находитесь», «улиц»,
    «куда подъехать/приехать/ехать/нужно» — «Диагностика на 3 этаже — 500 ₽»
    тоже замок. Осознанно консервативно: лучше не спросить, чем спросить
    дважды. Список слов — `inbound._ВОПРОС_ОБ_АДРЕСЕ_СЛОВА`.
    """
    from app.services.inbound import _ВОПРОС_ОБ_АДРЕСЕ

    тексты = (
        await db.execute(
            sa.select(Message.body)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == "out",
                Message.body.is_not(None),
                Message.created_at <= before,
            )
            .order_by(Message.created_at.desc())
            .limit(FEED_LIMIT)
        )
    ).scalars()
    return any(текст and _ВОПРОС_ОБ_АДРЕСЕ.search(текст) for текст in тексты)


async def last_in_feed(
    db: AsyncSession, conversation_id: uuid.UUID, *, before: datetime
) -> tuple[str, datetime] | None:
    """(direction, created_at) последнего среди 'in'/'out' не позже `before`
    (образец `runtime.bot_ask_timeout`). Заметки и системные записи не
    считаются ответом. Время — в UTC (в SQLite приходит naive)."""
    row = (
        await db.execute(
            sa.select(Message.direction, Message.created_at)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction.in_(("in", "out")),
                Message.created_at <= before,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    направление, когда = row
    return str(направление), _aware(когда)


def job_id(message_id: uuid.UUID | str, attempt: int = 0) -> str:
    """Имя задачи: по входящему (дедуп повторов вебхука); самоповтор — своё
    имя, потому что первое занято до конца выполнения (прецедент `suffix`
    у `geocode_queue.enqueue_geocode`)."""
    base = f"addr-ask:{message_id}"
    return base if attempt <= 0 else f"{base}:r{attempt}"


async def enqueue_address_ask(
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    defer_sec: int,
    attempt: int = 0,
) -> bool:
    """Поставить проверку на ВХОДЯЩЕЕ через `defer_sec` секунд.

    `attempt>0` — самоповтор задачи (история едет / модель читает): своё имя и
    `attempt=` именованным аргументом (как `origin` у geocode_queue). Строковые
    id, как `enqueue_llm_read`. Сбой очереди — warning
    `address_ask.enqueue_failed`, False: вопрос не стоит упавшего вебхука.
    """
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    kwargs: dict[str, Any] = {"attempt": attempt} if attempt > 0 else {}
    try:
        job = await ArqRedis.enqueue_job(
            as_arq(redis),
            ADDRESS_ASK_JOB,
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            _job_id=job_id(message_id, attempt),
            _defer_by=defer_sec or None,
            **kwargs,
        )
        return job is not None
    except Exception:  # noqa: BLE001 — вопрос не стоит упавшего вебхука
        log.warning(
            "address_ask.enqueue_failed",
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            attempt=attempt,
        )
        return False
