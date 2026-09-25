"""Автообъединение карточек-двойников по телефону: проверки и действие.

ПРОСЬБА ВЛАДЕЛЬЦА 12.09: карточка «сама объединяет». Решение 12.08
(«автообъединения нет») было ответом на склейку ПО ИМЕНИ, собравшую под одним
именем восемь человек из разных городов. Здесь склейка по ТЕЛЕФОНУ, и правило
одно, в одну фразу:

    Карточки склеиваются сами только когда каждая САМА назвала один номер в
    своей переписке, на разных наших аккаунтах, таких карточек не больше
    :data:`MAX_GROUP` за всю историю, имена совпадают точь-в-точь (без
    дописанных номеров заявок) и человек их не разъединял.

Группа (владелец 13.09, «Иван 100101 / 100102 / 1001030» с одним номером):
до 12.09 склеивались ровно две, третья оставалась подсказкой. Теперь все
карточки группы уходят в старшую по истории — по одной, каждая под своим
замком и с повторной проверкой.

Всё остальное — подсказка «Объединить» (как раньше). Замер 12.09: из 300
пар-двойников 281 — один человек, написавший в два наших объявления
(идентификатор Авито между нашими аккаунтами не сквозной: `cross_account_since`
стоит у 2 карточек из 70 000), 189 пар с равными именами, 218 — оба первых
сообщения в пределах суток.

КАЖДАЯ ПРОВЕРКА — ИМЕНОВАННЫЙ ОТКАЗ. Отказ не пишется в журнал (пара остаётся
подсказкой), но логируется с причиной, и сухой прогон (`cli merge-backlog
--dry-run`) считает пары по причинам.

ОБРАТИМО: «Разъединить» возвращает всё и запоминает пару (`client_merge_vetoes`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditLog,
    Client,
    ClientMergeVeto,
    ClientPhoneCandidate,
    Conversation,
    Message,
)
from app.models.app_setting import AppSetting
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_SOURCE_SWAP
from app.services import app_settings, notifications, phone_rules
from app.services.audit import write_audit
from app.services.clients import lock_cards, merge_clients

log = structlog.get_logger("app.client_merge")

RULE = "phone_twins_v2"
#: Разрыв между первыми сообщениями карточек, дальше которого не склеиваем сами:
#: перепроданная SIM, «отдал номер сыну». 218 пар из 300 — в пределах суток.
MAX_GAP_DAYS = 90
#: Больше стольких карточек с одним номером (живых и уже склеенных) — не
#: человек, а номер конторы или мастера: сами не трогаем.
MAX_GROUP = 4
#: Разъединений автоматических склеек за сутки, после которых объединение само
#: переходит в тень: люди говорят «не то» чаще, чем мы готовы ошибаться.
AUTOSTOP_UNMERGES_PER_DAY = 2


class Proof(NamedTuple):
    conversation_id: uuid.UUID
    account_id: uuid.UUID
    message_at: datetime | None


class TwinVerdict(NamedTuple):
    ok: bool
    reason: str
    winner: Client | None = None
    loser: Client | None = None
    proof: dict[str, Any] | None = None
    #: Остальные карточки группы — после `loser`, каждая своим шагом.
    others: list[Client] = []


class Outcome(NamedTuple):
    #: merged | shadow | skipped
    action: str
    reason: str
    winner_id: uuid.UUID | None = None
    loser_id: uuid.UUID | None = None
    moved: list[uuid.UUID] = []


async def check_twins(db: AsyncSession, phone: str, *, own: frozenset[str]) -> TwinVerdict:
    """Все условия правила по свежим данным. Первый отказ — ответ."""
    if phone in own or phone_rules.is_toll_free(phone):
        return TwinVerdict(False, "own_or_toll_free")

    live = list(
        (
            await db.execute(
                sa.select(Client)
                .where(Client.phone == phone, Client.merged_into_id.is_(None))
                .order_by(Client.id)
            )
        )
        .scalars()
        .all()
    )
    if len(live) < 2:
        return TwinVerdict(False, "single")
    ids = [c.id for c in live]

    # ВСЯ ИСТОРИЯ НОМЕРА, А НЕ ТОЛЬКО ЖИВЫЕ. Ушедшие карточки с этим номером
    # считаются в размер группы (иначе группа любого размера собиралась бы по
    # одной), а ушедшие НЕ В ЭТУ ГРУППУ — чужая история номера: SIM переходила
    # из рук в руки, сами не трогаем.
    ушедшие = list(
        (
            await db.execute(
                sa.select(Client.merged_into_id).where(
                    Client.phone == phone, Client.merged_into_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if any(куда not in ids for куда in ушедшие):
        return TwinVerdict(False, "shared_history")
    if len(live) + len(ушедшие) > MAX_GROUP:
        return TwinVerdict(False, "group")
    # Карточка, уже впитавшая кого-то с ДРУГИМ номером, — узел чужих склеек.
    дети = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Client)
            .where(Client.merged_into_id.in_(ids), Client.phone.is_distinct_from(phone))
        )
    ).scalar_one()
    if дети:
        return TwinVerdict(False, "has_children")
    # Строки ушедших карточек группы остаются на них — это своя история, не чужая.
    свои = ids + list(
        (
            await db.execute(
                sa.select(Client.id).where(Client.phone == phone, Client.merged_into_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    чужие_строки = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ClientPhoneCandidate)
            .where(
                ClientPhoneCandidate.phone == phone,
                ClientPhoneCandidate.status == CANDIDATE_ACCEPTED,
                ClientPhoneCandidate.client_id.not_in(свои),
            )
        )
    ).scalar_one()
    if чужие_строки:
        return TwinVerdict(False, "shared_candidate")

    for c in live:
        if c.external_id.startswith("chat:"):
            return TwinVerdict(False, "stub")
        if c.blocked_at is not None:
            return TwinVerdict(False, "blocked")
        if c.link_phone_conflict_at is not None:
            return TwinVerdict(False, "phone_conflict")
        if c.cross_account_since is not None:
            return TwinVerdict(False, "cross_account")

    # Запрет — по ВСЕЙ группе, включая поглощённых: «Разъединить» B от X
    # обязано держать и после того, как X руками увели в A, иначе B→A сводит
    # разъединённых под одной карточкой (ревью 13.09).
    запретов = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ClientMergeVeto)
            .where(ClientMergeVeto.a_id.in_(свои), ClientMergeVeto.b_id.in_(свои))
        )
    ).scalar_one()
    if запретов:
        return TwinVerdict(False, "vetoed")
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if not phone_rules.names_equal(a.name, b.name):
                return TwinVerdict(False, "names_differ")

    firsts: dict[uuid.UUID, datetime] = {}
    for c in live:
        first_c = await _first_message_at(db, c.id)
        if first_c is None:
            return TwinVerdict(False, "no_messages")
        firsts[c.id] = first_c

    # Победитель — карточка со старшей историей; при равенстве — меньший id.
    # Остальные — по старшинству за ним, каждая своим шагом (`auto_merge`).
    по_старшинству = sorted(live, key=lambda c: (firsts[c.id], c.id))
    winner, loser, *others = по_старшинству
    # Ушедшие того же номера — «своя история» только у победителя: проигравшая
    # с детьми — цепочка A→B→C, её `merge_clients` запрещает (loser_has_merged),
    # и без именованного отказа ночной проход возвращался бы к номеру вечно.
    if any(куда != winner.id for куда in ушедшие):
        return TwinVerdict(False, "has_children")
    proofs: dict[uuid.UUID, Proof] = {}
    for c in live:
        proof_c = await _proof(db, c, phone)
        if proof_c is None:
            return TwinVerdict(False, "unproven")
        proofs[c.id] = proof_c
    # «Каждая на своём аккаунте» — и поглощённые тоже: их диалоги переехали к
    # победителю и `_proof` их не видит, а два автора Авито на одном нашем
    # аккаунте — не один человек, даже если один уже впитан (ревью 13.09).
    аккаунты = {p.account_id for p in proofs.values()}
    поглощённые = [cid for cid in свои if cid not in ids]
    for cid in поглощённые:
        acc = await _absorbed_proof_account(db, cid, phone)
        if acc is None:
            return TwinVerdict(False, "unproven")
        аккаунты.add(acc)
    if len(аккаунты) < len(live) + len(поглощённые):
        return TwinVerdict(False, "same_account")

    for c in по_старшинству[1:]:
        if abs(firsts[c.id] - firsts[winner.id]) > timedelta(days=MAX_GAP_DAYS):
            return TwinVerdict(False, "gap_too_wide")
    proof: dict[str, Any] = {
        "winner": _proof_view(proofs[winner.id]),
        "loser": _proof_view(proofs[loser.id]),
        "first_seen": {
            "winner": firsts[winner.id].isoformat(),
            "loser": firsts[loser.id].isoformat(),
        },
    }
    if others:
        proof["group"] = [str(c.id) for c in others]
    return TwinVerdict(True, "ok", winner, loser, proof, others)


async def _proof(db: AsyncSession, card: Client, phone: str) -> Proof | None:
    """Номер назван в СОБСТВЕННОЙ переписке карточки — принятая строка с диалогом,
    который до сих пор под этой карточкой (не переехавший). Номер бота (без
    строки), ручной ввод без совпавшего кандидата, строка обмена — не
    доказательство: подсказка, а не склейка."""
    row = (
        await db.execute(
            sa.select(
                ClientPhoneCandidate.conversation_id,
                Conversation.account_id,
                ClientPhoneCandidate.message_at,
            )
            .join(Conversation, Conversation.id == ClientPhoneCandidate.conversation_id)
            .where(
                ClientPhoneCandidate.client_id == card.id,
                ClientPhoneCandidate.phone == phone,
                ClientPhoneCandidate.status == CANDIDATE_ACCEPTED,
                ClientPhoneCandidate.source != CANDIDATE_SOURCE_SWAP,
                ClientPhoneCandidate.conversation_id.is_not(None),
                Conversation.client_id == card.id,
            )
            .order_by(ClientPhoneCandidate.message_at.asc().nullslast())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return Proof(row[0], row[1], row[2])


async def _absorbed_proof_account(
    db: AsyncSession, client_id: uuid.UUID, phone: str
) -> uuid.UUID | None:
    """Аккаунт, на котором ПОГЛОЩЁННАЯ карточка сама назвала номер: строки
    остались на ней, а диалог переехал к победителю — владение не проверяем."""
    row = (
        await db.execute(
            sa.select(Conversation.account_id)
            .select_from(ClientPhoneCandidate)
            .join(Conversation, Conversation.id == ClientPhoneCandidate.conversation_id)
            .where(
                ClientPhoneCandidate.client_id == client_id,
                ClientPhoneCandidate.phone == phone,
                ClientPhoneCandidate.status == CANDIDATE_ACCEPTED,
                ClientPhoneCandidate.source != CANDIDATE_SOURCE_SWAP,
                ClientPhoneCandidate.conversation_id.is_not(None),
            )
            .order_by(ClientPhoneCandidate.message_at.asc().nullslast())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


def _proof_view(p: Proof) -> dict[str, Any]:
    return {
        "conversation_id": str(p.conversation_id),
        "account_id": str(p.account_id),
        "message_at": p.message_at.isoformat() if p.message_at else None,
    }


async def _first_message_at(db: AsyncSession, client_id: uuid.UUID) -> datetime | None:
    ids = (
        (await db.execute(sa.select(Conversation.id).where(Conversation.client_id == client_id)))
        .scalars()
        .all()
    )
    if not ids:
        return None
    value = (
        await db.execute(
            sa.select(sa.func.min(Message.created_at)).where(Message.conversation_id.in_(ids))
        )
    ).scalar_one_or_none()
    return _aware(value)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def auto_merge(
    db: AsyncSession,
    phone: str,
    *,
    mode: str,
    own: frozenset[str],
    backfill: bool = False,
) -> Outcome:
    """Проверить пару и объединить (`on`) или записать тень (`shadow`).

    Транзакцией владеет вызывающий. Автостоп: если за сутки люди разъединили
    ≥ :data:`AUTOSTOP_UNMERGES_PER_DAY` автоматических склеек, режим сам
    переводится в `shadow` — до решения владельца.
    """
    if mode not in ("shadow", "on"):
        return Outcome("skipped", "off")
    verdict = await check_twins(db, phone, own=own)
    if not verdict.ok or verdict.winner is None or verdict.loser is None:
        log.info("client_merge.skipped", reason=verdict.reason)
        return Outcome("skipped", verdict.reason)
    # ⚠ ПОД ЗАМКОМ — ЕЩЁ РАЗ. Первая проверка шла без `FOR UPDATE`; между ней и
    # замком оператор мог нажать «Разъединить» (появилась строка запрета) или
    # пометить карточку. Сторожа `merge_clients` ловят только своё
    # (объединена/цепочка), правило — здесь.
    под_замком = {verdict.winner.id, verdict.loser.id, *(c.id for c in verdict.others)}
    await lock_cards(db, verdict.winner, verdict.loser, *verdict.others)
    verdict = await check_twins(db, phone, own=own)
    if not verdict.ok or verdict.winner is None or verdict.loser is None:
        log.info("client_merge.skipped", reason=verdict.reason, after_lock=True)
        return Outcome("skipped", verdict.reason)
    # Карточка, появившаяся между первой проверкой и замком (второй вебхук),
    # под замок не попала — без замка её не проверяем: ночной проход вернётся
    # к номеру, когда все будут под замком с самого начала.
    if {verdict.winner.id, verdict.loser.id, *(c.id for c in verdict.others)} - под_замком:
        log.info("client_merge.skipped", reason="group_changed", after_lock=True)
        return Outcome("skipped", "group_changed")
    winner, loser = verdict.winner, verdict.loser
    proof = dict(verdict.proof or {})
    if backfill:
        proof["backfill"] = True

    if mode == "on" and await _people_disagree(db):
        await app_settings.set_many(db, {app_settings.CLIENT_MERGE_AUTO: "shadow"}, user_id=None)
        await write_audit(
            db,
            user_id=None,
            action="settings.phone_detect_changed",
            entity="settings",
            details={
                "before": {"merge_auto": "on"},
                "after": {"merge_auto": "shadow"},
                "reason": "autostop_unmerges",
            },
        )
        await notifications.notify(
            db,
            kind="client_merge.autostopped",
            body=(
                f"За сутки люди разъединили {AUTOSTOP_UNMERGES_PER_DAY} автоматических "
                "склеек — объединение переведено в «только считать». Включить обратно "
                "можно в настройках телефонов; до этого пары остаются подсказками."
            ),
        )
        log.warning("client_merge.autostop", unmerges_per_day=AUTOSTOP_UNMERGES_PER_DAY)
        mode = "shadow"

    if mode == "shadow":
        await write_audit(
            db,
            user_id=None,
            action="client.merge_shadow",
            entity="client",
            entity_id=str(winner.id),
            details={
                "source_id": str(loser.id),
                "target_id": str(winner.id),
                "rule": RULE,
                "proof": proof,
            },
        )
        return Outcome("shadow", "ok", winner.id, loser.id)

    result = await merge_clients(
        db, winner=winner, loser=loser, actor=None, auto=True, rule=RULE, proof=proof
    )
    moved = [uuid.UUID(v) for v in result["conversation_ids"]]
    # ГРУППА: остальные — по одной, каждая с повторной проверкой правила по
    # свежим данным (первая ушедшая уже числится историей номера в группе).
    for следующая in verdict.others:
        снова = await check_twins(db, phone, own=own)
        if not снова.ok or снова.winner is None or снова.loser is None:
            log.info("client_merge.group_stopped", reason=снова.reason)
            break
        if снова.winner.id != winner.id or снова.loser.id != следующая.id:
            log.info("client_merge.group_stopped", reason="group_changed")
            break
        ещё = await merge_clients(
            db,
            winner=winner,
            loser=следующая,
            actor=None,
            auto=True,
            rule=RULE,
            proof={**dict(снова.proof or {}), **({"backfill": True} if backfill else {})},
        )
        moved.extend(uuid.UUID(v) for v in ещё["conversation_ids"])
    return Outcome("merged", "ok", winner.id, loser.id, moved)


async def _people_disagree(db: AsyncSession) -> bool:
    """≥ N разъединений АВТОМАТИЧЕСКИХ склеек за последние сутки.

    Считаются только откаты ПОСЛЕ последнего изменения настройки: владелец,
    включивший объединение обратно после автостопа, принял решение с учётом
    тех откатов — засчитывать их второй раз значило бы перебивать его до конца
    суток.
    """
    since = datetime.now(UTC) - timedelta(days=1)
    row = await db.get(AppSetting, app_settings.CLIENT_MERGE_AUTO)
    if row is not None and row.updated_at is not None:
        менялась = _aware(row.updated_at)
        if менялась is not None and менялась > since:
            since = менялась
    rows = (
        await db.execute(
            sa.select(AuditLog.details).where(
                AuditLog.action == "client.unmerged", AuditLog.created_at >= since
            )
        )
    ).scalars()
    n = sum(1 for d in rows if (d or {}).get("was_auto"))
    return n >= AUTOSTOP_UNMERGES_PER_DAY
