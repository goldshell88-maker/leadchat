"""Ручки для расширения «Автозаявки»: отдать лиды и принять результат.

ДВЕ РУЧКИ, И ИХ ФОРМА ЗАДАНА НЕ НАМИ. Расширение уже написано и ходит по
своему протоколу (его README, раздел «Протокол»): `GET {base}/leads?limit=20` и
`POST {base}/leads/{uid}/ack`. Подгонять его под наши вкусы значило бы править
работающий чужой код ради красоты; проще совпасть.

⚠ ВХОД ЗДЕСЬ НЕ ПОЛЬЗОВАТЕЛЬСКИЙ. Расширение — машина, у неё нет сессии и не
может быть: оно ходит с офисного компьютера по расписанию, в том числе когда за
ним никто не сидит. Поэтому свой токен, отдельный от входа сотрудников, и своя
проверка. Токен даёт доступ к телефонам клиентов — обращаться с ним как с
паролем.

ПОЧЕМУ ЭТИ РУЧКИ ЖИВУТ ВНУТРИ `/api/v1`, А НЕ РЯДОМ С ВЕБХУКАМИ. Вебхуки Авито
приходят снаружи от чужой системы и проверяются секретом в адресе; здесь наш
собственный инструмент со своим токеном в заголовке. Разные двери — разные
замки, и путать их не стоит.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_permission
from app.core.errors import ApiError
from app.models.lead import DECISION_LABELS, SRC_LABELS
from app.models.user import User
from app.services import audit as audit_svc
from app.services import leads

router = APIRouter()

#: Управление автозаявками — там же, где остальные настройки системы.
settings_perm = require_permission("settings:manage")

log = structlog.get_logger("app.leads.api")


async def require_lead_token(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Проверить токен расширения.

    СРАВНЕНИЕ ПОСТОЯННОГО ВРЕМЕНИ (`compare_digest`), а не `==`. Обычное
    сравнение выходит на первом несовпавшем символе, и по времени ответа токен
    подбирается посимвольно. Здесь это не теория: ручка открыта в интернет и
    отвечает на каждый запрос.

    Пустой токен в настройках означает «расширение не подключено» — тогда
    ручка закрыта для всех, а не открыта для всех. Разница в одну строку и в
    доступ ко всем телефонам клиентов.
    """
    expected = await leads.get_token(db)
    if not expected:
        raise ApiError("not_configured", "Автозаявки не настроены", status=403)

    prefix = "Bearer "
    header = authorization or ""
    given = header[len(prefix) :] if header.startswith(prefix) else ""
    # ⚠ ПРОВЕРКА НА ЛАТИНИЦУ ОБЯЗАТЕЛЬНА, И ЭТО НЕ ПРИДИРКА К ФОРМАТУ.
    # `compare_digest` на строке с кириллицей БРОСАЕТ TypeError, а не отвечает
    # «не совпало». Поймано на стенде: токен «подделка» давал 500 «Внутренняя
    # ошибка» вместо честного 401 — то есть любой мусор в заголовке выглядел
    # как поломка сервера, и в мониторинге копились бы наши собственные пятисотки.
    if not given or not given.isascii() or not secrets.compare_digest(given, expected):
        raise ApiError("unauthorized", "Неверный токен", status=401)


@router.get("/leads", dependencies=[Depends(require_lead_token)])
async def get_leads(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Отдать порцию лидов и запомнить, что отдали.

    ЗАПИСЬ О ВЫДАЧЕ ДЕЛАЕТСЯ ЗДЕСЬ ЖЕ, а не после подтверждения. Иначе при
    любой заминке расширение получило бы тот же диалог на следующем опросе, и
    заявка завелась бы дважды — то есть мастер поехал бы к человеку два раза.
    """
    batch = await leads.collect(db, limit=limit)
    if batch.handouts:
        await leads.hand_out(db, batch)
        await db.commit()

    # Придержанные — в лог, с причиной и числом. Молчание здесь означало бы, что
    # две трети заявок теряются незаметно (разбор — в шапке `services/leads.py`).
    if batch.held_back:
        причины: dict[str, int] = {}
        for held in batch.held_back:
            причины[held.reason] = причины.get(held.reason, 0) + 1
        log.info("leads.held_back", total=len(batch.held_back), reasons=причины)

    return {"leads": batch.leads}


class AckBody(BaseModel):
    """Тело подтверждения — поля расширения, как оно их шлёт."""

    uid: str = Field(default="", max_length=200)
    decision: str = Field(default="error", max_length=40)
    #: Номер заявки в лид-центре. Приходит числом, принимаем и строкой:
    #: это чужой идентификатор, и его тип — не наше дело.
    requestId: int | str | None = None
    srcKey: str | None = Field(default=None, max_length=10)
    message: str = Field(default="", max_length=2000)


@router.post("/leads/{uid}/ack", dependencies=[Depends(require_lead_token)])
async def ack_lead(
    uid: str,
    body: AckBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Принять результат: завелась заявка или нет и почему.

    ⚠ ОТВЕЧАЕМ 200 ДАЖЕ НА НЕИЗВЕСТНЫЙ ЛИД. Расширение подтверждает уже ПОСЛЕ
    того, как заявка создана, и ошибку подтверждения оно намеренно глотает.
    Ответь мы 404 — оно бы промолчало, а лид остался бы неподтверждённым и ушёл
    в повторную выдачу. То есть строгость здесь стоила бы второй заявки.
    """
    row = await leads.ack(
        db,
        uid=uid or body.uid,
        decision=body.decision,
        request_id=str(body.requestId) if body.requestId is not None else None,
        message=body.message,
    )
    if row is None:
        log.warning("leads.ack_unknown", uid=uid, decision=body.decision)
        return {"ok": False, "known": False}
    await db.commit()
    return {
        "ok": True,
        "known": True,
        "decision": row.decision,
        "label": DECISION_LABELS.get(row.decision or "", row.decision),
    }


# ---------------------------------------------------- управление (для админа)


class TokenOut(BaseModel):
    """Состояние подключения. ⚠ Самого токена здесь нет — только «задан»."""

    configured: bool
    #: Сводка выдач: сколько отдано, создано, отклонено, ждёт подтверждения.
    stats: dict[str, int]


@router.get("/settings/leads")
async def leads_settings(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Состояние автозаявок для экрана настроек."""
    token = await leads.get_token(db)
    return {"configured": bool(token), "stats": await leads.stats(db)}


@router.post("/settings/leads/token")
async def issue_token(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Выпустить новый токен и показать его ОДИН раз.

    ПОЧЕМУ ОДИН РАЗ. Хранится он зашифрованным и обратно не отдаётся: ручка
    состояния говорит только «задан». Значит увидеть значение можно ровно
    здесь, в ответ на осознанное нажатие, — и человек обязан сразу перенести
    его в расширение.

    Выпуск нового немедленно закрывает старый: расширение со старым токеном
    получит 401 и перестанет забирать лиды, пока его не перенастроят. Это и
    есть способ отозвать доступ, если токен куда-то утёк.

    ⚠ ПУТЬ ЗАКРЫТ ПРЕДОХРАНИТЕЛЕМ (решение владельца 16.08). Разбор — у самого
    предохранителя, `leads.SECOND_PATH_FUSE`: он один на обе двери, потому что
    вторая дверь (команда `app.cli leads-token`) была открыта нараспашку.
    """
    if leads.SECOND_PATH_FUSE:
        raise ApiError("leads_disabled", leads.SECOND_PATH_FUSE, status=409)

    # Ниже — то, что произойдёт в день, когда поток осознанно переведут на
    # LeadChat. Держим код живым, а не в истории git: восстанавливать выдачу
    # токена по памяти в тот день — лишний способ ошибиться.
    token = await leads.set_token(db, leads.new_token())
    await audit_svc.write_audit(
        db, user_id=user.id, action="settings.leads_token_issued", details={}
    )
    await db.commit()
    return {"token": token}


@router.delete("/settings/leads/token")
async def revoke_token(
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Отозвать токен: автозаявки выключаются целиком."""
    await leads.set_token(db, "")
    await audit_svc.write_audit(
        db, user_id=user.id, action="settings.leads_token_revoked", details={}
    )
    await db.commit()
    return {"configured": False}


@router.get("/settings/leads/handouts")
async def handouts(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(settings_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Что отдавали и чем кончилось — для экрана. И что придержано.

    ⚠ ПРИДЕРЖАННЫЕ НЕ БЫЛИ ВИДНЫ НИГДЕ, ХОТЯ ЭКРАН ОБЕЩАЛ ОБРАТНОЕ (28.08).
    `collect` складывает непригодные диалоги в `Batch.held_back` с причиной на
    каждый, но ручка выдачи писала их только в structlog агрегатом, а наружу
    отдавала одни лиды. Строк `LeadHandout` придержанный не создаёт, поэтому и в
    журнале ниже его быть не могло. При этом подпись на экране утверждает: «Если
    не сработали оба пути, заявка придерживается — и это видно в журнале ниже, а
    не пропадает молча».

    По замеру в шапке `services/leads.py` из шести диалогов с итогом «Выезд»
    телефон есть у двух — то есть две трети заявок висят придержанными, и
    владелец, пришедший разбираться «почему в лид-центрах мало заявок», не видел
    ни одной из них.

    Считаем на месте: `collect` НИЧЕГО НЕ ЗАПИСЫВАЕТ (её собственная докстрока),
    так что показать придержанные — это только чтение. Предел тот же, что у
    выдачи: экран показывает ближайшую порцию, а не всю историю.
    """
    rows = await leads.recent(db, limit=limit)
    порция = await leads.collect(db, limit=min(limit, 100))
    return {
        "held_back": [
            {
                "conversation_id": str(h.conversation_id),
                "client_name": h.client_name,
                "account_title": h.account_title,
                "reason": h.reason,
            }
            for h in порция.held_back
        ],
        "items": [
            {
                "conversation_id": str(row.conversation_id),
                "handed_at": row.handed_at.isoformat(),
                "src_key": row.src_key,
                "src_label": SRC_LABELS.get(row.src_key, row.src_key),
                "acked": row.acked_at is not None,
                "decision": row.decision,
                "decision_label": DECISION_LABELS.get(row.decision or "", None),
                "created": row.created,
                "request_id": row.request_id,
                "message": row.message,
            }
            for row in rows
        ],
    }
