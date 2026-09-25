"""Свои адреса — еженедельно, из исходящих (пакет 7а, Q24; программа §2.2).

ЗАЧЕМ. Список своих адресов руками (`address_own_addresses`) держится на
памяти владельца; адрес мастерской, который операторы называют клиентам,
виден и без него — в наших же исходящих. Ключ «улица+дом», прозвучавший в
исходящих ≥ `MIN_CONVERSATIONS` разных диалогов (или у ≥ `MIN_ACCOUNTS`
аккаунтов) за `DAYS` дней, — адрес компании: адрес клиента в трёх диалогах
не повторяется.

⚠ ПОРЯДОК В КАЖДОМ ДИАЛОГЕ. Диалог засчитывается, только если в нём нет
входящей реплики с тем же ключом РАНЬШЕ нашей первой исходящей с ним —
иначе повтор оператором адреса клиента «ул. Ленина 5, верно?» в трёх
диалогах одного дома попал бы в реестр и запер бы настоящий адрес всем, кто
там живёт. Упоминание клиента ищется по тексту (`address_own.text_mentions`),
не по строкам-кандидатам.

ЧТО ПИШЕТ. Строку «улица, дом; …» в `address_geo.own_addresses_auto` тем же
`app_settings.set_many(user_id=None)`, что у ручки, + строку журнала
`settings.address_detect_changed` с `source=own_addresses_weekly` — только
если реестр изменился. Читает её сторож эха (`inbound._эхо_мастерской`)
вместе со списком владельца; человек эту настройку не правит.

ЦЕНА. `messages` партиционирована помесячно, индекса «все исходящие по дате»
нет: проход за 90 дней — порциями по `BATCH` по `(created_at, id)` с
серверным ситом «похоже на адрес» (`address_funnel.ADDRESS_LIKE_PATTERN`),
потолок `MAX_ROWS` строк — дальше остановка с предупреждением, а не
бесконечный понедельник. Транзакцией владеет задача (`session_scope`
откатывает на выходе): запись и журнал — явный commit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session as db_mod
from app.models import Conversation, Message
from app.services import address_funnel, address_own, address_parse, app_settings
from app.services.address_own import Key
from app.services.audit import write_audit

log = structlog.get_logger("app.address_own_addresses")

JOB_ID = "address_own_addresses_weekly"
SETTINGS_ACTION = "settings.address_detect_changed"
#: Откуда взялась строка журнала настроек — чтобы отличать от ручки и лестницы.
SOURCE = "own_addresses_weekly"
STATEMENT_TIMEOUT = "180s"
DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600}

#: Окно реестра, дней.
DAYS = 90
#: Порция исходящих за один SELECT и потолок прохода (§2.2, контракт C.3).
BATCH = 2_000
MAX_ROWS = 50_000
#: Порог «адрес компании»: диалогов с ключом ИЛИ аккаунтов.
MIN_CONVERSATIONS = 3
MIN_ACCOUNTS = 2
#: Наши реплики: оператор (из LeadChat и эхо приложения Авито) и бот; вопрос
#: системы об адресе (`out/system`) улицы не содержит и разбирать его незачем.
OUR_SENDERS = ("operator", "bot")
#: Потолок текстовой настройки (`Spec.max_length`, 2000; длиннее запись
#: отвергается): реестр урезается по числу диалогов, а не на полуслове.
TEXT_LIMIT = 2000


@dataclass
class OwnAddress:
    """Один ключ реестра: где встретился и как его показать человеку."""

    sample: str
    #: Диалог → когда впервые прозвучал в нашей исходящей.
    first_out: dict[UUID, datetime] = field(default_factory=dict)
    account_of: dict[UUID, UUID] = field(default_factory=dict)

    def as_dict(self, conversations: set[UUID]) -> dict[str, Any]:
        return {
            "conversations": len(conversations),
            "accounts": len({self.account_of[c] for c in conversations}),
            "sample": self.sample,
        }


async def _исходящие(
    db: AsyncSession, *, since: datetime, now: datetime
) -> tuple[dict[Key, OwnAddress], int]:
    """Проход по исходящим окна: ключ → где и когда прозвучал, и число
    просмотренных строк — для журнала и потолка."""
    по_ключу: dict[Key, OwnAddress] = {}
    просмотрено = 0
    после: tuple[datetime, UUID] | None = None
    while True:
        stmt = (
            sa.select(
                Message.id,
                Message.created_at,
                Message.conversation_id,
                Conversation.account_id,
                Message.body,
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.direction == "out",
                Message.sender_type.in_(OUR_SENDERS),
                Message.body.is_not(None),
                Message.created_at >= since,
                Message.created_at < now,
                Message.body.regexp_match(address_funnel.ADDRESS_LIKE_PATTERN),
                Message.body.regexp_match(address_funnel.DIGIT_PATTERN),
            )
            .order_by(Message.created_at, Message.id)
            .limit(BATCH)
        )
        if после is not None:
            # Курсор по паре (created_at, id): одинаковые метки времени на
            # границе порции не теряются и не читаются дважды.
            stmt = stmt.where(sa.tuple_(Message.created_at, Message.id) > после)
        строки = (await db.execute(stmt)).all()
        for msg_id, когда, conv_id, account_id, тело in строки:
            просмотрено += 1
            после = (когда, msg_id)
            наш = address_parse.parse(тело or "", про_адрес=True)
            ключ = address_own.key_of(наш)
            if наш is None or ключ is None:
                continue
            запись = по_ключу.setdefault(ключ, OwnAddress(sample=наш.value))
            прежде = запись.first_out.get(conv_id)
            if прежде is None or когда < прежде:
                запись.first_out[conv_id] = когда
            запись.account_of[conv_id] = account_id
        if len(строки) < BATCH:
            break
        if просмотрено >= MAX_ROWS:
            log.warning(
                "address_own.scan_capped", scanned=просмотрено, max_rows=MAX_ROWS, days=DAYS
            )
            break
    return по_ключу, просмотрено


async def _входящие_диалога(
    db: AsyncSession, conv_id: UUID, *, since: datetime, before: datetime
) -> list[tuple[datetime, str]]:
    """Тексты входящих диалога в окне (тело и расшифровка голосового)."""
    строки = (
        await db.execute(
            sa.select(Message.created_at, Message.body, Message.voice_transcript)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.created_at >= since,
                Message.created_at < before,
            )
            .order_by(Message.created_at)
        )
    ).all()
    return [(когда, текст) for когда, тело, речь in строки for текст in (тело, речь) if текст]


def _клиент_называл(тексты: list[tuple[datetime, str]], ключ: Key, *, before: datetime) -> bool:
    return any(
        когда < before
        and (
            address_own.key_of(address_parse.parse(текст, про_адрес=True)) == ключ
            or address_own.text_mentions(текст, ключ)
        )
        for когда, текст in тексты
    )


async def collect_own_addresses(
    db: AsyncSession, *, now: datetime, days: int = DAYS
) -> dict[Key, dict[str, Any]]:
    """Реестр своих адресов по исходящим за `days` дней: ключ → `{conversations,
    accounts, sample}`. Только чтение."""
    реестр, _ = await _собрать(db, now=now, days=days)
    return реестр


async def _собрать(
    db: AsyncSession, *, now: datetime, days: int
) -> tuple[dict[Key, dict[str, Any]], int]:
    since = now - timedelta(days=days)
    по_ключу, просмотрено = await _исходящие(db, since=since, now=now)
    # Порядок проверяется только у ключей, которые могут дойти до порога:
    # входящие диалога читаются один раз на диалог (кэш), и не ради ключей,
    # что встретились в одном диалоге у одного аккаунта.
    кэш: dict[UUID, list[tuple[datetime, str]]] = {}
    реестр: dict[Key, dict[str, Any]] = {}
    for ключ, запись in по_ключу.items():
        диалогов = set(запись.first_out)
        аккаунтов = {запись.account_of[c] for c in диалогов}
        if len(диалогов) < MIN_CONVERSATIONS and len(аккаунтов) < MIN_ACCOUNTS:
            continue
        зачтены: set[UUID] = set()
        for conv_id, первая_наша in запись.first_out.items():
            if conv_id not in кэш:
                кэш[conv_id] = await _входящие_диалога(db, conv_id, since=since, before=now)
            if not _клиент_называл(кэш[conv_id], ключ, before=первая_наша):
                зачтены.add(conv_id)
        итог = запись.as_dict(зачтены)
        if итог["conversations"] >= MIN_CONVERSATIONS or итог["accounts"] >= MIN_ACCOUNTS:
            реестр[ключ] = итог
    return реестр, просмотрено


def registry_text(registry: dict[Key, dict[str, Any]]) -> str:
    """Строка настройки «улица, дом; …»: образцы по убыванию диалогов, затем по
    алфавиту; образец, который не разбирается обратно в свой же ключ (форма,
    которую сторож не прочтёт), не пишется — с предупреждением."""
    части: list[str] = []
    длина = 0
    порядок = sorted(registry.items(), key=lambda kv: (-kv[1]["conversations"], kv[1]["sample"]))
    for ключ, данные in порядок:
        образец = str(данные["sample"])
        if address_own.key_of(address_own.parse_part(образец)) != ключ:
            log.warning("address_own.sample_unreadable", sample=образец)
            continue
        добавка = len(образец) + (2 if части else 0)
        if длина + добавка > TEXT_LIMIT:
            log.warning("address_own.registry_truncated", keys=len(registry), kept=len(части))
            break
        части.append(образец)
        длина += добавка
    return "; ".join(части)


async def run_weekly(now: datetime | None = None) -> dict[Key, dict[str, Any]]:
    """Точка входа планировщика: своя сессия, явный commit."""
    now = now or datetime.now(UTC)
    старт = time.monotonic()
    async with db_mod.session_scope() as db:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        реестр, просмотрено = await _собрать(db, now=now, days=DAYS)
        было = await app_settings.get(db, app_settings.ADDRESS_OWN_ADDRESSES_AUTO) or ""
        стало = registry_text(реестр)
        изменилось = стало != было
        if изменилось:
            await app_settings.set_many(
                db, {app_settings.ADDRESS_OWN_ADDRESSES_AUTO: стало}, user_id=None
            )
            await write_audit(
                db,
                user_id=None,
                action=SETTINGS_ACTION,
                entity="settings",
                details={
                    "source": SOURCE,
                    "before": {"own_addresses_auto": было},
                    "after": {"own_addresses_auto": стало},
                    "keys": len(реестр),
                },
            )
            await db.commit()
    log.info(
        "address_own.collected",
        keys=len(реестр),
        scanned=просмотрено,
        changed=изменилось,
        elapsed_s=round(time.monotonic() - старт, 3),
    )
    return реестр


def register(scheduler: Any) -> None:
    """Понедельник 01:10 UTC = 04:10 МСК — до воронки (01:40) и лестницы
    (02:10), в том же ночном окне."""
    scheduler.add_job(
        run_weekly,
        CronTrigger(day_of_week="mon", hour=1, minute=10, timezone="UTC"),
        id=JOB_ID,
        **DEFAULTS,
    )
