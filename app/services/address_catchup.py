"""Догон после включения автозаписи и автопривязки адреса (проверка 24.09).

Автозапись ставится после нового вердикта в том же диалоге, а в закончившемся
диалоге его не будет. Выключили автозапись на сутки и включили обратно —
адреса, подтверждённые картой за это время, лежали без карточки, пока
владелец не запускал `address-autofill-backlog` из консоли, хотя подпись
переключателя обещала «пустая карточка заполнится сама». С автопривязкой так
же: строки, судимые при выключенной политике, обход починки не пересматривал —
версия судьи у них текущая.

Оба догона идут путём живой работы, своего решения здесь нет: автозапись —
`enqueue_autofill` (решает воркер `autofill_address`), пересуд — обход починки
(`geo_repair`, триггер `verdict_version`) в его квотах и границах возраста.
Здесь только выбор строк.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import AuditLog, Client, ClientAddressCandidate
from app.models.client import CANDIDATE_PENDING
from app.scheduler.jobs.geo_repair import REQUEUE_MAX_AGE
from app.services import clients, geocode

#: Действие журнала, которым ручка настроек пишет правку «Адресов в переписке».
SETTINGS_ACTION = "settings.address_detect_changed"


@dataclass(frozen=True, slots=True)
class AutofillBacklog:
    """Диалоги, которым автозапись есть что взять по степени."""

    #: Карточка пуста, в диалоге ждёт строка со степенью.
    empty_cards: tuple[uuid.UUID, ...]
    #: Карточку держит строка автоматики, а в диалоге ждёт строка лучшей степени.
    better_grade: tuple[uuid.UUID, ...]

    @property
    def conversations(self) -> list[uuid.UUID]:
        return list(dict.fromkeys([*self.empty_cards, *self.better_grade]))


async def autofill_backlog(db: AsyncSession) -> AutofillBacklog:
    """Кому ставить автозапись — два случая живого пути.

    ПРЕДФИЛЬТР В SQL, СТЕПЕНЬ — В PYTHON (контракт 18.09, п. 3): в SQL только
    то, что степень заведомо исключает, — не `exact` и не отказ со строкой
    улицы; итог решает `card_grade`.
      (а) карточка пуста — любая строка со степенью;
      (б) карточку держит строка автоматики (не руками, не кнопкой), а в
          диалоге ждёт строка ЛУЧШЕЙ степени — лестница text → approx →
          exact (контракт п. 4); одно ли это место, решит воркер.
    Руками (`address_set_at`) и кнопкой (`resolved_by_id` у источника) — не
    ставим никогда: воркер их всё равно не тронет.
    """
    source = aliased(ClientAddressCandidate)
    rows = (
        await db.execute(
            sa.select(
                ClientAddressCandidate.conversation_id,
                ClientAddressCandidate.kind,
                ClientAddressCandidate.geo_status,
                ClientAddressCandidate.geo_provider,
                ClientAddressCandidate.geo_lat,
                ClientAddressCandidate.geo_lon,
                ClientAddressCandidate.geo_formatted,
                Client.address,
                source.kind.label("src_kind"),
                source.geo_status.label("src_geo_status"),
                source.geo_provider.label("src_geo_provider"),
                source.geo_lat.label("src_geo_lat"),
                source.geo_lon.label("src_geo_lon"),
                source.geo_formatted.label("src_geo_formatted"),
            )
            .join(Client, Client.id == ClientAddressCandidate.client_id)
            .outerjoin(source, source.id == Client.address_candidate_id)
            .where(
                Client.merged_into_id.is_(None),
                Client.address_set_at.is_(None),
                ClientAddressCandidate.status == CANDIDATE_PENDING,
                sa.or_(
                    ClientAddressCandidate.geo_status == geocode.GEO_EXACT,
                    sa.and_(
                        ClientAddressCandidate.geo_status.in_(tuple(geocode.REFUSAL_STATUSES)),
                        ClientAddressCandidate.geo_formatted.is_not(None),
                    ),
                ),
                sa.or_(
                    Client.address.is_(None),
                    sa.and_(source.id.is_not(None), source.resolved_by_id.is_(None)),
                ),
            )
            .order_by(ClientAddressCandidate.detected_at.desc())
        )
    ).all()
    empty: list[uuid.UUID] = []
    better: list[uuid.UUID] = []
    for r in rows:
        grade = geocode.card_grade(
            r.kind, r.geo_status, r.geo_provider, r.geo_lat, r.geo_lon, r.geo_formatted
        )
        if grade is None:
            continue
        if r.address is None:
            empty.append(r.conversation_id)
            continue
        held = geocode.card_grade(
            r.src_kind,
            r.src_geo_status,
            r.src_geo_provider,
            r.src_geo_lat,
            r.src_geo_lon,
            r.src_geo_formatted,
        )
        # Источник без степени (вердикт сброшен) ждёт своего вердикта, а не
        # догона: воркер пересоберёт карточку сам (`refresh_auto_address`).
        # Направление лестницы — только через `clients.grade_beats` (у
        # `geocode.GRADE_RANK` меньше — лучше; знак ранга читается в одном месте).
        if held is not None and clients.grade_beats(grade, held):
            better.append(r.conversation_id)
    return AutofillBacklog(
        empty_cards=tuple(dict.fromkeys(empty)), better_grade=tuple(dict.fromkeys(better))
    )


async def mark_rejudge_after_auto_decide(db: AsyncSession, *, now: datetime) -> int:
    """Строки, судимые при выключенной автопривязке, — на пересуд обходом починки.

    Помечаются снятием версии судьи (`geo_verdict_version = NULL`, «судила
    прежняя версия»): вердикт без политики и правда вынесен другим судьёй, а
    триггер `verdict_version` обхода вернёт такие строки сам — в свободный
    остаток порции, со снимком улик, в квоте доли Яндекса, только строки от
    27 часов до 30 дней. Своего сброса вердикта здесь нет намеренно: путь
    пересуда один (память dva-puti-raznyi-schet).

    Окно — с последнего выключения автопривязки с экрана (строка журнала
    настроек), но не глубже границы обхода: старше он строку всё равно не
    возьмёт. Выключения в журнале нет (выключали мимо экрана) — окно по
    границе обхода. Транзакцией владеет вызывающий.
    """
    since = await _auto_decide_off_since(db, now=now)
    result = await db.execute(
        sa.update(ClientAddressCandidate)
        .where(
            ClientAddressCandidate.geo_status.in_(tuple(geocode.RECHECK_STATUSES)),
            ClientAddressCandidate.status == CANDIDATE_PENDING,
            ClientAddressCandidate.resolved_by_id.is_(None),
            ClientAddressCandidate.geo_checked_at >= since,
            ClientAddressCandidate.geo_verdict_version.is_not(None),
        )
        .values(geo_verdict_version=None)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0))


async def _auto_decide_off_since(db: AsyncSession, *, now: datetime) -> datetime:
    floor = now - REQUEUE_MAX_AGE
    rows = (
        await db.execute(
            sa.select(AuditLog.created_at, AuditLog.details)
            .where(AuditLog.action == SETTINGS_ACTION, AuditLog.created_at >= floor)
            .order_by(AuditLog.created_at.desc())
        )
    ).all()
    for created_at, details in rows:
        if _switched_off(details):
            return created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    return floor


def _switched_off(details: object) -> bool:
    if not isinstance(details, dict):
        return False
    before, after = details.get("before"), details.get("after")
    return (
        isinstance(before, dict)
        and isinstance(after, dict)
        and before.get("auto_decide") is True
        and after.get("auto_decide") is False
    )
