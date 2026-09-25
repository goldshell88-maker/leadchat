"""Отправка, повтор и заметки (01 §6.2–§6.4).

| Endpoint | Право | Роли |
|---|---|---|
| `POST /conversations/{id}/messages` | `messages:send` | admin, manager (head —
  `403 read_only_role`, observer — `403 forbidden`) |
| `POST /messages/{id}/retry` | `messages:send` | admin, manager (свои
  сообщения; админ — любые) |
| `POST /conversations/{id}/notes` | `notes:write` | admin, head, manager
  (observer — 403) |

Ответ на отправку мгновенный, ``delivery_status='pending'`` — доставку
делает ARQ-воркер ``deliver_message`` (08 §3), итог приходит WS-событием
``message:status``. Повтор с тем же ``client_message_id`` возвращает то же
самое сообщение с кодом 200 и заголовком ``X-Idempotent-Replay: true``
(01 §1.6), дубля не создаётся.
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.core.errors import ApiError
from app.models import Conversation, User
from app.services import media
from app.services import messages as msgs

router = APIRouter()
log = structlog.get_logger("app.messages")


def _log_publish_failed(created: msgs.CreatedMessage, exc: RedisError) -> None:
    log.warning(
        "messages.publish_failed",
        conversation_id=str(created.conversation.id),
        message_id=str(created.message.id),
        error=str(exc),
    )


send_perm = require_permission("messages:send")
notes_perm = require_permission("notes:write")


class AttachmentRef(BaseModel):
    media_id: str = Field(min_length=1, max_length=64)


class SendMessageIn(BaseModel):
    text: str = ""
    # UUID v4 с фронта; длину ограничиваем — это часть ключа Redis (08 §8.3)
    client_message_id: str = Field(min_length=1, max_length=64)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    #: На какое сообщение это ответ (просьба владельца 02.09). Наша собственная
    #: связь: у Авито цитирования в API нет, наружу она не уедет.
    #:
    #: ⚠ ПРОВЕРЯЕТСЯ НА СЕРВЕРЕ, А НЕ ЗДЕСЬ. Тип гарантирует только форму;
    #: принадлежность сообщения ЭТОМУ диалогу — `messages.resolve_reply_to`,
    #: иначе в переписку одного человека уехал бы кусок разговора с другим.
    reply_to_id: uuid.UUID | None = None


class NoteIn(BaseModel):
    text: str
    client_message_id: str = Field(min_length=1, max_length=64)
    # ПОЛЯ ЗДЕСЬ НЕ БЫЛО, И ФАЙЛ ПРОПАДАЛ БЕЗ СЛЕДА (#30).
    #
    # Композер прямо советует оператору: «Авито не принимает файлы от нас —
    # приложите файл к заметке». Оператор прикладывал, видел его в ленте
    # (пузырь рисуется до ответа сервера), получал 201 — и файла не было.
    # Лишнее поле pydantic отбрасывает молча, а `create_note` писала пустой
    # список вложений жёстко.
    #
    # То есть интерфейс уверенно направлял человека в единственный сценарий,
    # где вложение теряется, и не подавал ни малейшего признака. Ровно эту
    # болезнь лечила #9 — только для сообщений клиенту.
    attachments: list[AttachmentRef] = Field(default_factory=list)


def _replayed(response: Response, created: msgs.CreatedMessage) -> dict[str, Any]:
    """201 на первую попытку, 200 + X-Idempotent-Replay на повтор (01 §1.6)."""
    response.status_code = 200 if created.replay else 201
    if created.replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return msgs.message_out_full(
        created.message,
        created.sender,
        client_message_id=created.client_message_id,
        # Цитата возвращается и в ответе ручки: без неё пузырь, нарисованный
        # оптимистично, терял бы связь при первой же перерисовке от сервера.
        quoted=created.quoted,
    )


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageIn,
    response: Response,
    user: User = Depends(send_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    attachment_ids = [a.media_id for a in payload.attachments]
    text = msgs.validate_text(payload.text, allow_empty=bool(attachment_ids))
    attachments = await media.resolve_attachments(redis, attachment_ids)

    created = await msgs.create_outbound_message(
        db,
        redis,
        conversation_id=conversation_id,
        user=user,
        text=text,
        client_message_id=payload.client_message_id,
        attachments=attachments,
        reply_to_id=payload.reply_to_id,
    )
    if not created.replay:
        # Строго после commit'а и в этом порядке (08 §8.1 п.4): сначала кадр,
        # потом очередь. Наоборот быстрый воркер (отказ Авито приходит сразу)
        # присылал статус раньше, чем экраны коллег узнавали о сообщении, и у них
        # оно висело «Отправляется». Сбой кадров доставку не останавливает:
        # сообщение уже сохранено, а догон по ?updated_since отдаст правду.
        try:
            await msgs.publish_message_new(redis, created)
            if created.claimed_from_queue:
                # Ответ в непринятый диалог — то же принятие (7.1): у остальных
                # строка обязана уйти из очереди, иначе их «Принять» получит 409.
                await msgs.publish_claimed_by_reply(redis, created.conversation, user)
            if created.conversation_changed:
                await msgs.publish_conversation_updated(redis, created.conversation, user)
        except RedisError as exc:
            _log_publish_failed(created, exc)
        # Задача не встала — сообщение обязано стать `failed`, иначе оно
        # останется «Отправляется» навсегда: `pending` не видит ни «Повторить»,
        # ни повторный POST, ни красная метка диалога.
        if not await msgs.enqueue_deliver(redis, created.message.id):
            await msgs.mark_enqueue_failed(db, created.message.id)
            try:
                await msgs.publish_status(
                    redis,
                    conversation_id=created.conversation.id,
                    message_id=created.message.id,
                    delivery_status="failed",
                    error="Не удалось поставить в очередь доставки",
                )
            except RedisError as exc:
                _log_publish_failed(created, exc)
    return _replayed(response, created)


@router.post("/conversations/{conversation_id}/notes", status_code=201)
async def create_note(
    conversation_id: uuid.UUID,
    payload: NoteIn,
    response: Response,
    user: User = Depends(notes_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Внутренняя заметка: в Авито не уходит, observer её не видит (01 §6.4)."""
    attachment_ids = [a.media_id for a in payload.attachments]
    # Пустой текст при вложении разрешён — как у сообщений. Заметка «вот фото
    # шильдика» без единого слова осмысленна, а требовать подпись к картинке
    # значит заставлять писать «фото» четыреста раз за смену.
    text = msgs.validate_text(payload.text, allow_empty=bool(attachment_ids))
    attachments = await media.resolve_attachments(redis, attachment_ids)

    created = await msgs.create_note(
        db,
        redis,
        conversation_id=conversation_id,
        user=user,
        text=text,
        client_message_id=payload.client_message_id,
        attachments=attachments,
    )
    if not created.replay:
        # message:new с direction='note' — Hub не доставит его observer-сессиям
        await msgs.publish_message_new(redis, created)
    return _replayed(response, created)


@router.delete("/messages/{message_id}", status_code=204)
async def delete_note(
    message_id: uuid.UUID,
    user: User = Depends(notes_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """Удалить ЗАМЕТКУ (17.08, просьба владельца).

    Только заметки: переписка с клиентом неприкосновенна — Авито её всё
    равно не забудет, и удалённое там «воскресло» бы сверкой. Только автор:
    чужая заметка — чужая мысль, спор о ней решают люди, а не кнопка.
    """
    import sqlalchemy as sa

    from app.models import Message as MessageModel
    from app.services.audit import write_audit
    from app.ws.events import publish_event

    # ⚠ НЕ `db.get`: у `messages` СОСТАВНОЙ первичный ключ (id, created_at) —
    # таблица секционирована по дате. `db.get(Message, id)` падает с
    # InvalidRequestError, то есть 500 вместо ответа (поймано на бою 18.08).
    msg = (
        await db.execute(sa.select(MessageModel).where(MessageModel.id == message_id).limit(1))
    ).scalar_one_or_none()
    if msg is None:
        raise ApiError("not_found", "Заметка не найдена", status=404)
    if msg.direction != "note":
        raise ApiError(
            "conflict",
            "Удалять можно только заметки — переписка с клиентом неприкосновенна",
            status=409,
        )
    if msg.sender_user_id != user.id:
        raise ApiError("forbidden", "Заметку удаляет её автор", status=403)
    conv_id = msg.conversation_id
    await db.delete(msg)
    await write_audit(
        db,
        user_id=user.id,
        action="note.deleted",
        entity="message",
        entity_id=str(message_id),
        details={"conversation_id": str(conv_id)},
    )
    await db.commit()
    # кадр с direction=note: хаб спрячет его от сессий без права на заметки
    await publish_event(
        redis,
        "message:deleted",
        {
            "conversation_id": str(conv_id),
            "message_id": str(message_id),
            "direction": "note",
        },
    )


@router.post("/messages/{message_id}/retry")
async def retry_message(
    message_id: uuid.UUID,
    user: User = Depends(send_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Повтор неудачной отправки (01 §6.3): failed -> pending, новая джоба."""
    msg = await msgs.reset_for_retry(db, redis, message_id=message_id, user=user)
    conv = await db.get(Conversation, msg.conversation_id)

    # ⚠ ЗАДАЧУ СТАВИМ ПЕРВОЙ И ОТВЕЧАЕМ НА РЕЗУЛЬТАТ (аудит 30.08).
    #
    # `enqueue_deliver` в своей шапке требует: «Вызывающий обязан на это
    # ответить». `send_message` отвечает, повтор — не отвечал. А `reset_for_retry`
    # к этому моменту УЖЕ закоммитил `pending` и снял красную метку: не встань
    # задача (Redis недоступен, очередь переполнена) — сообщение оставалось в
    # состоянии без выхода. Повторный `/retry` даёт 422 «не в отказе», повторный
    # POST уходит в replay, метки нет. Ровно тот тупик, который для пути отправки
    # чинили 27.08, — на пути повтора, которым пользуются как раз тогда, когда
    # инфраструктура и сбоит.
    #
    # Порядок тоже важен: раньше `publish_status` шёл первым, и его исключение
    # давало 500 с тем же исходом — «Отправляется» навсегда.
    # Новый `_job_id`: счётчик попыток ARQ начинается заново (08 §3).
    queued = await msgs.enqueue_deliver(
        redis, msg.id, job_id=f"deliver:{msg.id}:{uuid.uuid4().hex[:8]}"
    )
    if not queued:
        await msgs.mark_enqueue_failed(db, msg.id)
        await msgs.publish_status(
            redis,
            conversation_id=msg.conversation_id,
            message_id=msg.id,
            delivery_status="failed",
            conversation_patch={"undelivered": True},
        )
        raise ApiError(
            "enqueue_failed",
            "Очередь отправки недоступна. Попробуйте ещё раз через минуту",
            status=503,
        )

    await msgs.publish_status(
        redis,
        conversation_id=msg.conversation_id,
        message_id=msg.id,
        delivery_status="pending",
        # Метка «ответ не ушёл» гаснет во ВСЕХ вкладках, а не только в той,
        # где нажали (#26): диалог один на тринадцать человек, и красная
        # строка у остальных значила бы «этим никто не занялся».
        conversation_patch={"undelivered": conv.undelivered_at is not None} if conv else None,
    )
    sender = await db.get(User, msg.sender_user_id) if msg.sender_user_id else None
    return msgs.message_out_full(msg, sender, quoted=await msgs.quoted_of(db, msg))


@router.post("/messages/{message_id}/dismiss")
async def dismiss_message(
    message_id: uuid.UUID,
    user: User = Depends(send_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Снять неотправленное с учёта: `failed` -> `dismissed`, метка гаснет.

    Довод целиком — в `msgs.dismiss_failed`. Коротко: у красной метки диалога
    не было выхода, кроме удачного повтора, а повтор помогает не всегда.

    Событие шлём всем вкладкам по тому же доводу, что и у повтора: диалог один
    на тринадцать человек, и погасшая метка обязана погаснуть у всех.
    """
    msg = await msgs.dismiss_failed(db, message_id=message_id, user=user)
    conv = await db.get(Conversation, msg.conversation_id)
    await msgs.publish_status(
        redis,
        conversation_id=msg.conversation_id,
        message_id=msg.id,
        delivery_status="dismissed",
        conversation_patch={"undelivered": conv.undelivered_at is not None} if conv else None,
    )
    sender = await db.get(User, msg.sender_user_id) if msg.sender_user_id else None
    return msgs.message_out_full(msg, sender, quoted=await msgs.quoted_of(db, msg))


read_perm = require_permission("conversations:read")


@router.get("/messages/{message_id}/voice")
async def voice_url(
    message_id: uuid.UUID,
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Ссылка на голосовое сообщение — спрашиваем у Авито в момент нажатия.

    ⚠ ПОЧЕМУ ССЫЛКУ НЕ ХРАНИМ (жалоба владельца 19.08: «не грузятся голосовые
    сообщения»). В самом сообщении Авито присылает только идентификатор
    записи — звука там нет. Оператор видел строку «Голосовое сообщение» и не
    мог её послушать: клиент говорит, а мы не слышим. На бою таких сообщений
    174, и это не мелочь — в голосовом обычно и есть суть заказа.

    Ссылки у Авито ВРЕМЕННЫЕ. Сохранённая в базе через час превратилась бы в
    битую и врала бы дважды: и про звук, и про то, что он у нас есть. Поэтому
    спрашиваем ровно тогда, когда человек нажал «прослушать».
    """
    import sqlalchemy as sa

    from app.integrations.avito.client import AvitoClient
    from app.integrations.avito.errors import AvitoApiError, AvitoAuthError, AvitoUnavailable
    from app.models import AvitoAccount
    from app.models import Message as MessageModel
    from app.services import crypto, voice

    # Не `db.get`: у `messages` составной первичный ключ (таблица секционирована).
    msg = (
        await db.execute(sa.select(MessageModel).where(MessageModel.id == message_id).limit(1))
    ).scalar_one_or_none()
    if msg is None:
        raise ApiError("not_found", "Сообщение не найдено", status=404)

    # ⚠ РАЗБОР ВЛОЖЕНИЯ ЖИВЁТ В ОДНОМ МЕСТЕ. Тот же разбор нужен расшифровке
    # (`app/workers/transcribe.py`), и раньше он был написан прямо здесь.
    # Разойдись две копии — ручка отдавала бы запись, а расшифровка молчала бы
    # (или наоборот), и виноватого не было бы видно нигде.
    voice_id = voice.voice_id_of(msg.attachments)
    if voice_id is None:
        raise ApiError("not_found", "В этом сообщении нет голосового", status=404)

    conv = await db.get(Conversation, msg.conversation_id)
    account = await db.get(AvitoAccount, conv.account_id) if conv else None
    if account is None or not account.access_token_enc:
        raise ApiError("conflict", "Канал не подключён — спросить запись не у кого", status=409)

    # ⚠ КЛИЕНТ ЧЕРЕЗ `fresh(db)`, А НЕ КОНСТРУКТОРОМ. Обычный конструктор читает
    # кэш ПРОЦЕССА: после переключения на боевой Авито он продолжал бы ходить в
    # имитатор, и голосовое «не находилось» бы по совершенно ложной причине.
    # Страж поймал это сразу — за что ему спасибо.
    client = await AvitoClient.fresh(db)
    # ⚠ ПОХОД В АВИТО БЫЛ ГОЛЫМ — ЕДИНСТВЕННЫЙ ТАКОЙ В ЭТОМ ФАЙЛЕ.
    #
    # `AvitoApiError`, `AvitoUnavailable` и `AvitoAuthError` наследуются от
    # обычного `Exception`, а не от `ApiError`. Значит их ловил только общий
    # перехватчик и отдавал 500 «Сбой на нашей стороне. Повторите через минуту —
    # если не пройдёт, сообщите администратору». Оператор читал это как поломку
    # системы и шёл к администратору, тогда как на деле недоступен был Авито или
    # отозван токен канала — и лечится это совсем другим.
    #
    # Тем же 500 отвечала и ошибка расшифровки токена на несовпавшем ключе.
    try:
        urls = await client.get_voice_urls(
            crypto.decrypt_token(account.access_token_enc), account.avito_user_id, [voice_id]
        )
    except AvitoAuthError as exc:
        raise ApiError(
            "conflict",
            "Канал требует переподключения — записи Авито не отдаёт",
            status=409,
        ) from exc
    except AvitoUnavailable as exc:
        raise ApiError(
            "bad_gateway",
            "Авито сейчас недоступен — попробуйте через минуту",
            status=502,
        ) from exc
    except AvitoApiError as exc:
        raise ApiError(
            "bad_gateway",
            "Авито не отдал запись — попробуйте позже",
            status=502,
        ) from exc
    ссылка = urls.get(voice_id)
    if not ссылка:
        # Авито ответило, но записи не дало: чаще всего она уже удалена у них.
        # Честный отказ лучше пустого проигрывателя, который «просто не играет».
        raise ApiError("not_found", "Авито не отдал запись — возможно, она удалена", status=404)
    return {"url": ссылка}
