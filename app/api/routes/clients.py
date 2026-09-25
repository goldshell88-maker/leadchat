"""Карточка клиента: чёрный список, телефон руками, объединение карточек.

ЧТО ДЕЛАЕТ ПОМЕТКА. Сообщения помеченного приходят, сохраняются и видны —
пометка убирает ровно одно: требование внимания. Диалог не встаёт в очередь
и не звенит. Клиент об этом не знает и ничего не замечает.

Именно поэтому здесь нет «заблокировать отправку» и «удалить»: и то и другое
означало бы потерянные сообщения, а среди них однажды окажется настоящий
заказ от человека, который в прошлый раз был не в духе.

ТЕЛЕФОН И ОБЪЕДИНЕНИЕ (правка 9 от 12 августа). Правила и вся арифметика — в
`app/services/clients.py`; здесь только разбор запроса, права и транзакция.
Право на всё то же, что у пометки, — `conversations:manage`: правит карточку
тот, кто с этим клиентом разговаривает. Объединение при этом обратимо ровно
той же ручкой, поэтому отдельного, более узкого права оно не требует.

РЕШЕНИЕ ПО РАСПОЗНАННОМУ НОМЕРУ (правка 10, там же) живёт под тем же правом и
по той же причине: «заменить» кнопкой и «заменить» руками кладут номер в одно
и то же поле карточки. Отдельное право закрыло бы дверь, оставив окно.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.core.errors import ApiError
from app.models import Client, ClientAddressCandidate, ClientPhoneCandidate, Conversation, User
from app.services import clients as clients_svc
from app.services import clients_events
from app.services import inbox as inbox_svc
from app.services.audit import write_audit
from app.services.merge_queue import enqueue_merge
from app.services.user_ref import user_ref
from app.ws.events import publish_event

log = structlog.get_logger(__name__)

router = APIRouter()

# Право то же, что у работы с диалогами: помечает тот, кто с этим клиентом и
# разговаривает. Отдельное право завело бы ещё одну строку в матрицу ради
# действия, которое по смыслу — часть обычной работы оператора.
block_perm = require_permission("conversations:manage")
# Читать карточку целиком может каждый, кто читает диалоги: наблюдателю нужен
# адрес выезда, а он лежит только здесь. Правки остаются под `block_perm`.
identity_read_perm = require_permission("conversations:read")

MAX_REASON_LEN = 300
#: Длина поля ввода телефона с запасом на скобки, дефисы и «+7 (912) …».
#: Не про безопасность, а про то, чтобы в поле не приехал абзац текста:
#: `normalize_phone` его всё равно отвергнет, но 400 с внятной длиной честнее
#: разбора мегабайта.
MAX_PHONE_LEN = 32
#: Потолок длины адреса — тот же, что в службе: одно определение на оба слоя.
MAX_ADDRESS_LEN = clients_svc.MAX_ADDRESS_LEN


class BlockRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN)


class NameRequest(BaseModel):
    #: Пустая строка РАЗРЕШЕНА и означает очистку. «Ак» из профиля Авито хуже, чем
    #: ничего: под пустым именем карточка показывается как «Клиент», и это честно.
    #: Запрети очистку — и диспетчер вынужден оставить заведомо неверное имя.
    name: str = Field(default="", max_length=clients_svc.MAX_NAME_LEN)


class PhoneRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=MAX_PHONE_LEN)
    #: Из какого диалога нажали. Нужен не для записи телефона, а для метрики
    #: «собрано телефонов»: она берёт аккаунт через `conversations` по
    #: `details.conversation_id` (06 §1.4). Необязателен — карточку однажды
    #: откроют и не из диалога, и терять из-за этого телефон нельзя.
    conversation_id: uuid.UUID | None = None


class PrimaryPhoneRequest(BaseModel):
    """Какой из УЖЕ ИЗВЕСТНЫХ номеров сделать основным.

    `conversation_id` здесь, в отличие от соседнего `PhoneRequest`, ОБЯЗАТЕЛЕН:
    прежний основной номер уезжает в дополнительные строкой кандидата, а у той
    `conversation_id` не может быть пустым — «предложение без „где это было“
    проверить нельзя» (app/models/client.py). Без диалога удержать прежний
    номер нечем, а молча его потерять — ровно то, от чего эта ручка и заведена.
    """

    phone: str = Field(min_length=1, max_length=MAX_PHONE_LEN)
    conversation_id: uuid.UUID


class AddressRequest(BaseModel):
    """Адрес руками. Пустая строка стирает — это законное действие."""

    address: str = Field(max_length=MAX_ADDRESS_LEN)
    conversation_id: uuid.UUID | None = None


class AddressWrongRequest(BaseModel):
    """«Адрес неверный»: из какого диалога нажали — кадр `client:updated` идёт
    по диалогу, а карточка вне диалога сегодня не открывается."""

    conversation_id: uuid.UUID | None = None


class AddressDecisionRequest(BaseModel):
    decision: str
    #: Какой из вариантов карты выбрал оператор (индекс в `geo.variants`).
    variant: int | None = Field(default=None, ge=0, le=10)


class MergeRequest(BaseModel):
    #: Кого объединяем В карточку из пути. Победитель — тот, что в пути:
    #: оператор стоит в его диалоге, и после операции остаётся там же.
    source_id: uuid.UUID


class CandidateDecision(BaseModel):
    """Тело решения по распознанному номеру: «заменить», «добавить», «отклонить».

    Тип поля — обычная строка, а НЕ `Literal`, и это осознанно. Проверку
    допустимых значений делает `clients.resolve_phone_candidate` — она же одна
    держит список слов, — и отвечает на негодное решение 422. Объяви поле
    перечислением, тот же запрос получал бы 400 от разбора тела, то есть один
    и тот же отказ приходил бы двумя разными статусами в зависимости от того,
    какое слово прислали.
    """

    decision: str


def _view(client: Client, by: User | None = None) -> dict[str, Any]:
    return {
        "id": str(client.id),
        "name": client.name,
        "phone": client.phone,
        "blocked": client.blocked_at is not None,
        "blocked_at": client.blocked_at.isoformat().replace("+00:00", "Z")
        if client.blocked_at
        else None,
        "blocked_by": user_ref(by),
        "blocked_reason": client.blocked_reason,
    }


async def _get(db: AsyncSession, client_id: uuid.UUID) -> Client:
    client = await db.get(Client, client_id)
    if client is None:
        raise ApiError("not_found", "Клиент не найден", status=404)
    return client


async def _publish_client_updated(
    redis: Redis, client_id: uuid.UUID, conversation_id: uuid.UUID | None, *, reason: str
) -> None:
    """Кадр `client:updated` — «перезапросите личность этого клиента».

    ⚠ ЭТО ДРУГОЙ КАДР, ЧЕМ `conversation:updated` НИЖЕ, И ОДИН ДРУГОГО НЕ
    ЗАМЕНЯЕТ. Тот правит строку списка и деталь диалога; этот сбрасывает
    семейство `clients` — список номеров, подпись происхождения, кандидатов.
    Образец и разбор — `inbound._известить_о_клиенте`.

    ⚠ САМ НОМЕР В КАДР НЕ КЛАДЁМ. Кадр широковещательный, а у карточки есть
    своя ручка с правами: рассылать персональные данные всем подписчикам ради
    экономии одного запроса нельзя.

    ⚠ КАДР НЕ ИМЕЕТ ПРАВА УРОНИТЬ УЖЕ СДЕЛАННУЮ ПРАВКУ. Номер в базе поменялся
    и зафиксирован; не ушёл кадр — коллега увидит правду при следующем открытии
    карточки. Ловим широко, но НЕ молча.
    """
    try:
        await publish_event(
            redis,
            "client:updated",
            {
                "client_id": str(client_id),
                "conversation_id": str(conversation_id) if conversation_id else None,
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "client.updated_not_published",
            client_id=str(client_id),
            reason=reason,
            error=str(exc),
        )


@router.post("/clients/{client_id}/block")
async def block_client(
    client_id: uuid.UUID,
    body: BlockRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Пометить клиента: его обращения перестают требовать внимания."""
    client = await _get(db, client_id)
    if client.blocked_at is not None:
        # Повторная пометка — не ошибка, но и не событие: журнал не должен
        # обрастать строками от двойного клика.
        return _view(client, user)

    client.blocked_at = datetime.now(UTC)
    client.blocked_by_id = user.id
    client.blocked_reason = (body.reason or "").strip() or None
    # Окно пометки обещает «не встанет в очередь и не будет звенеть у команды» —
    # и про уже ждущее обращение тоже: оно уходит из «Входящих» у всех, а
    # закрыть необработанное менеджер не может.
    left_queue = []
    for conv in (
        await db.execute(
            sa.select(Conversation).where(
                Conversation.client_id == client.id, Conversation.status != "closed"
            )
        )
    ).scalars():
        if inbox_svc.is_waiting(conv):
            inbox_svc.leave_queue(conv)
            inbox_svc.clear_awaiting(conv)
            left_queue.append(conv)
    await write_audit(
        db,
        user_id=user.id,
        action="client.blocked",
        entity="client",
        entity_id=str(client.id),
        details={"reason": client.blocked_reason},
    )
    await db.commit()
    await clients_events.publish_client_patch(
        db,
        redis,
        client.id,
        {"blocked": True, "blocked_reason": client.blocked_reason},
    )
    for conv in left_queue:
        await clients_events.publish_left_queue(redis, conv)
    return _view(client, user)


@router.post("/clients/{client_id}/unblock")
async def unblock_client(
    client_id: uuid.UUID,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Снять пометку. Следующее обращение снова встанет в очередь."""
    client = await _get(db, client_id)
    if client.blocked_at is None:
        return _view(client)

    client.blocked_at = None
    client.blocked_by_id = None
    client.blocked_reason = None
    await write_audit(
        db,
        user_id=user.id,
        action="client.unblocked",
        entity="client",
        entity_id=str(client.id),
    )
    await db.commit()
    await clients_events.publish_client_patch(
        db, redis, client.id, {"blocked": False, "blocked_reason": None}
    )
    return _view(client)


@router.get("/clients/blocked")
async def list_blocked(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Кого пометили — с именами тех, кто пометил.

    Список нужен не ради полноты: пометка ставится один раз и живёт годами,
    и через полгода никто не помнит, кого и почему занесли. Без этого экрана
    снять ошибочную пометку можно было бы, только случайно наткнувшись на
    диалог.
    """
    total = (
        await db.execute(
            sa.select(sa.func.count()).select_from(Client).where(Client.blocked_at.is_not(None))
        )
    ).scalar_one()
    rows = list(
        (
            await db.execute(
                sa.select(Client)
                .where(Client.blocked_at.is_not(None))
                .order_by(Client.blocked_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    # Имена пометивших — одним запросом, а не по строке: помеченных бывает
    # немного, но привычка ходить в базу на каждую строку однажды переедет
    # туда, где строк тысячи.
    ids = {r.blocked_by_id for r in rows if r.blocked_by_id}
    users = {u.id: u for u in (await db.execute(sa.select(User).where(User.id.in_(ids)))).scalars()}
    return {
        "items": [_view(r, users.get(r.blocked_by_id) if r.blocked_by_id else None) for r in rows],
        "page": {"limit": limit, "offset": offset, "total": total},
    }


# ---------------------------------------------------- личность клиента (правка 9)


@router.get("/clients/{client_id}/identity")
async def client_identity(
    client_id: uuid.UUID,
    user: User = Depends(identity_read_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Телефоны, идентификаторы Авито и объединения одной карточки.

    Ключуется КЛИЕНТОМ, а не диалогом: у одного человека диалогов бывает
    девять, и складывать одинаковый список телефонов в деталь каждого значило
    бы считать его девять раз. Подробности — в
    `app/services/clients.py::identity_view`.
    """
    return await clients_svc.identity_view(db, await _get(db, client_id))


@router.put("/clients/{client_id}/name")
async def set_client_name(
    client_id: uuid.UUID,
    body: NameRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Записать имя клиента, узнанное в разговоре (требование заказчика 13 августа).

    ЗАЧЕМ РУКАМИ. Имя приходит из профиля Авито, а профиль человек заводил один раз и
    мог назвать его как угодно: «Ак», «Продам всё», пусто. Диспетчер в разговоре узнаёт
    настоящее имя — и до сегодня карточка на это отвечала неизменяемой строкой.

    ПРАВО ТО ЖЕ, ЧТО У ТЕЛЕФОНА И ПОМЕТКИ «нежелательный»: карточку правит тот, кто с
    этим клиентом разговаривает (см. шапку файла). Отдельного права под имя не заводим —
    это была бы третья запись в каталоге прав ради одного поля.
    """
    client = await _get(db, client_id)
    result = await clients_svc.set_name(db, client, body.name, actor=user)
    await db.commit()
    await clients_events.publish_client_patch(db, redis, client.id, {"name": client.name})
    return result


@router.put("/clients/{client_id}/phone")
async def set_client_phone(
    client_id: uuid.UUID,
    body: PhoneRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Записать телефон, названный клиентом в разговоре или в чужом канале.

    ЗАЧЕМ РУКАМИ. Автомат вычитывает номер из текста сообщения, и на бою это
    даёт 3 телефона из 37 обращений: люди диктуют номер голосом, пишут его в
    Авито-объявлении или называют мастеру. До сегодня карточка на это отвечала
    неизменяемой строкой «телефон не указан» — то есть знала, что номера нет, и
    не давала его вписать.

    ДВОЙНИК НЕ ОТМЕНЯЕТ СОХРАНЕНИЕ, а едет в ответе (`twins`). Отказ «такой
    номер уже есть» заставил бы оператора выбирать между правдой и
    возможностью сохранить, а один человек законно имеет по карточке на каждый
    наш аккаунт. Карточка покажет двойника строкой с кнопкой «Объединить».
    """
    client = await _get(db, client_id)
    if body.conversation_id is not None:
        # Диалог обязан быть ЭТОГО клиента. Иначе метрика «собрано телефонов»
        # посчитала бы номер на чужой аккаунт — достаточно подставить в тело
        # запроса любой известный uuid диалога.
        owner = (
            await db.execute(
                sa.select(Conversation.client_id).where(Conversation.id == body.conversation_id)
            )
        ).scalar_one_or_none()
        if owner != client.id:
            raise ApiError("conversation_mismatch", "Диалог не этого клиента", status=422)
    result = await clients_svc.set_phone(
        db, client, body.phone, actor=user, conversation_id=body.conversation_id
    )
    await db.commit()
    # ⚠ КАДР ОБЯЗАТЕЛЕН, И ЗДЕСЬ ЕГО НЕ БЫЛО (разбор 03.09). У соседней ручки
    # (имя клиента, чуть выше) он есть, у телефона не было: номер, вписанный
    # одним диспетчером, не появлялся у остальных до перезапроса. Телефон —
    # это и есть предмет работы: по нему звонят и по нему сверяют, тот ли
    # клиент. Два пути, делающие одно дело по-разному.
    await clients_events.publish_client_patch(db, redis, client.id, {"phone": client.phone})
    # Второй кадр — как у `make_phone_primary`: `conversation:updated` правит
    # строку и деталь, но не карточку клиента. Без него у коллеги оставались
    # прежние поле телефона, подпись происхождения и «Ещё номера», а его
    # «изменить» подставляло старый номер и откатывало исправление.
    await _publish_client_updated(redis, client.id, body.conversation_id, reason="phone_set")
    # Появился/сменился основной — проверить двойников по нему (12.09). Ставится
    # после commit'а: задача читает карточки из базы.
    if client.phone:
        await enqueue_merge(redis, client.phone)
    return result


@router.put("/clients/{client_id}/address")
async def set_client_address(
    client_id: uuid.UUID,
    body: AddressRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Записать адрес выезда, названный клиентом.

    ⚠ РАЗБОРА ЗДЕСЬ НЕТ, И ЭТО НЕ УПРОЩЕНИЕ. У телефона строка приводится к
    канону, потому что канон один. У адреса его нет: «Ленина 5» и «ул. Ленина,
    д. 5» — одно место, записанное по-разному, и выбирать за диспетчера, какая
    запись верная, значит спорить с тем, кто только что говорил с клиентом.
    """
    client = await _get(db, client_id)
    await _диалог_этого_клиента(db, client, body.conversation_id)
    result = await clients_svc.set_address(
        db, client, body.address, actor=user, conversation_id=body.conversation_id
    )
    await db.commit()
    # Без диалога кадр слать некуда: подписка на семейство `clients` идёт по
    # диалогу. Карточка вне диалога сегодня не открывается, но правка молча
    # пропасть не должна — она уже в базе, и коллега увидит её при открытии.
    if body.conversation_id is not None:
        await _publish_client_updated(redis, client.id, body.conversation_id, reason="address_set")
    return result


async def _диалог_этого_клиента(
    db: AsyncSession, client: Client, conversation_id: uuid.UUID | None
) -> None:
    """Диалог в теле обязан принадлежать карточке из пути: иначе кадр и журнал
    привязали бы правку к чужому разговору. Без диалога проверять нечего."""
    if conversation_id is None:
        return
    owner = (
        await db.execute(
            sa.select(Conversation.client_id).where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if owner != client.id:
        raise ApiError("conversation_mismatch", "Диалог не этого клиента", status=422)


@router.post("/clients/{client_id}/address/wrong")
async def reject_client_address(
    client_id: uuid.UUID,
    body: AddressWrongRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """«Адрес неверный» под адресом, который записала автоматика (пакет 6.0а, I-9).

    Одно нажатие вместо «изменить → стереть → сохранить»: строка-источник →
    `rejected` + `resolved_by_id`, карточка → пусто, в журнале
    `client.address_edited source=manual reason=wrong` — по этой причине
    воронка считает отказы руками по правилу. Правила и 409 на не-автоадрес —
    в `clients.reject_auto_address`; здесь разбор тела, права и транзакция.
    """
    client = await _get(db, client_id)
    await _диалог_этого_клиента(db, client, body.conversation_id)
    result = await clients_svc.reject_auto_address(
        db, client, actor=user, conversation_id=body.conversation_id
    )
    await db.commit()
    # Кадр строго после commit'а — как у PUT выше и по той же причине: коллега,
    # перечитавший карточку по кадру раньше записи, увидел бы старый адрес.
    if body.conversation_id is not None:
        await _publish_client_updated(
            redis, client.id, body.conversation_id, reason="address_wrong"
        )
    return result


@router.post("/clients/{client_id}/address-candidates/{candidate_id}/resolve")
async def resolve_address_candidate(
    client_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: AddressDecisionRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Решение по распознанному адресу: подтвердить, добавить или отклонить.

    С 18.09 адрес пишет автоматика по степени `clients.candidate_grade`
    (владелец: «адрес привязывается автоматически, оператор ничего не
    подтверждает»); с экрана сюда приходят `reject` («Не адрес») и `replace`
    («Записать в карточку») там, где автоматика сама не запишет, `add`
    остаётся для CLI и тестов. Правку человека — «изменить» (PUT) и отказ —
    автоматика не трогает никогда.
    """
    client = await _get(db, client_id)
    candidate = await db.get(ClientAddressCandidate, candidate_id)
    if candidate is None or not await clients_svc.candidate_belongs(db, client, candidate):
        raise ApiError("not_found", "Предложение не найдено", status=404)
    result = await clients_svc.resolve_address_candidate(
        db,
        candidate=candidate,
        client=client,
        decision=body.decision,
        actor=user,
        variant=body.variant,
    )
    await db.commit()
    await _publish_client_updated(
        redis, client.id, candidate.conversation_id, reason="address_resolved"
    )
    return result


@router.post("/clients/{client_id}/phone/primary")
async def make_phone_primary(
    client_id: uuid.UUID,
    body: PrimaryPhoneRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Поменять основной номер на другой известный номер этого человека.

    ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09: «когда клиент даёт 2 номер, то исправить можно
    только основной номер, второй изменить нельзя. Так же нельзя поменять их
    местами». Второй номер жил строкой принятого кандидата и действий не имел
    вовсе — карточка печатала «Ещё номера этого человека: …» обычным текстом.

    ⚠ ПОЧЕМУ НЕ ХВАТИЛО СОСЕДНЕЙ `PUT /phone`. Она затирает прежний номер:
    после обмена руками в карточке остался бы один номер вместо двух, и второй
    нашёлся бы только в журнале аудита. Здесь прежний удерживается
    дополнительным — разбор в `clients_svc.make_phone_primary`.
    """
    client = await _get(db, client_id)
    # Диалог обязан быть ЭТОГО клиента — тот же довод, что у соседней ручки:
    # иначе в строку кандидата уехал бы чужой диалог, и «где это было»
    # перестало бы быть проверяемым.
    owner = (
        await db.execute(
            sa.select(Conversation.client_id).where(Conversation.id == body.conversation_id)
        )
    ).scalar_one_or_none()
    if owner != client.id:
        raise ApiError("conversation_mismatch", "Диалог не этого клиента", status=422)
    result = await clients_svc.make_phone_primary(
        db, client, body.phone, actor=user, conversation_id=body.conversation_id
    )
    await db.commit()
    # Кадр обязателен по тому же доводу, что у `PUT /phone`: номер — предмет
    # работы, и обмен, сделанный одним диспетчером, обязан быть виден остальным
    # без перезапроса.
    await clients_events.publish_client_patch(db, redis, client.id, {"phone": client.phone})
    # ⚠ И ВТОРОЙ КАДР, ИНАЧЕ У КОЛЛЕГИ ОБНОВИТСЯ ТОЛЬКО ЦИФРА (правка после
    # разбора). `conversation:updated` правит строку списка и деталь диалога, но
    # СЕМЕЙСТВО `clients` он не сбрасывает — замер показал ноль вызовов
    # `invalidateQueries`. А обмен меняет ровно личность: список дополнительных
    # номеров, подпись происхождения, набор кандидатов. Перезапрос по фокусу
    # окна в проекте выключен глобально, то есть без этого кадра карточка у
    # коллеги жила бы прежним состоянием до перезагрузки страницы.
    await _publish_client_updated(redis, client.id, body.conversation_id, reason="phone_swapped")
    if client.phone:
        await enqueue_merge(redis, client.phone)
    return result


@router.post("/clients/{client_id}/phone-candidates/{candidate_id}/resolve")
async def resolve_phone_candidate(
    client_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: CandidateDecision,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Решение оператора по номеру, вычитанному из переписки.

    Правила трёх исходов и вся арифметика — в
    `app/services/clients.py::resolve_phone_candidate`; здесь только разбор
    запроса, права и транзакция. Право то же, каким правят телефон руками
    (`conversations:manage`): «заменить» кнопкой и «заменить» руками кладут
    номер в одно и то же поле, и разрешать одно, запрещая другое, значило бы
    закрыть дверь, оставив окно.

    КАНДИДАТ ОБЯЗАН БЫТЬ ИЗ КАРТОЧКИ В ПУТИ. Иначе оператор, открывший карточку
    Ольги, мог бы подставить в адрес чужой идентификатор и вписать в НЕЁ номер
    из чужой переписки — а карточка после этого выглядела бы совершенно
    обычно. Чужой кандидат отвечает 404, а не 403: сообщать, что такой номер
    вообще существует, тому, кто спросил не про свою карточку, незачем.

    ДВОЙНИК ЕДЕТ В ОТВЕТЕ ТЕМ ЖЕ ПОЛЕМ, ЧТО У РУЧНОГО ВВОДА (`twins`). Номер,
    подтверждённый оператором, — ровно тот случай, когда карточек у человека
    оказывается две: он писал с двух наших аккаунтов. Одинаковая форма ответа
    у двух ручек означает, что «Объединить» в карточке рисуется одним и тем же
    кодом, а не двумя разъезжающимися.
    """
    client = await _get(db, client_id)
    candidate = await db.get(ClientPhoneCandidate, candidate_id)
    if candidate is None or not await clients_svc.candidate_belongs(db, client, candidate):
        raise ApiError("not_found", "Распознанный номер не найден", status=404)

    result = await clients_svc.resolve_phone_candidate(
        db, candidate, decision=body.decision, actor=user, card=client
    )
    # Двойников ищем по номеру КАНДИДАТА, а не по телефону карточки: при
    # «добавить» поле карточки не меняется вовсе, но номер уже признан
    # принадлежащим этому человеку — и вторая карточка с ним есть та же
    # находка, что и при «заменить». При «отклонить» искать нечего: оператор
    # только что сказал, что номер не его.
    twins: list[dict[str, Any]] = []
    if body.decision != "reject":
        twins = [
            clients_svc.short_view(row)
            for row in await clients_svc.phone_twins(db, candidate.phone, exclude_id=client.id)
        ]
    await db.commit()
    # «Заменить» сменил основной — двойники по нему проверяются заново (12.09);
    # «добавить» и «отклонить» основной не трогают. Кадр «личность изменилась»
    # нужен всем трём: снятый или добавленный номер обязан уйти с экранов коллег
    # без F5 — раньше здесь кадра не было вовсе.
    if body.decision == "replace" and client.phone:
        await enqueue_merge(redis, client.phone)
    await clients_events.publish_client_updated(
        redis, client.id, candidate.conversation_id, reason=f"phone_candidate_{body.decision}"
    )
    return {**result, "twins": twins}


@router.get("/clients/{client_id}/merge-candidates")
async def client_merge_candidates(
    client_id: uuid.UUID,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Подсказка «может быть, это один человек» — с причиной у каждого пункта.

    Именно ПОДСКАЗКА: ни один пункт не применяется сам, даже совпавший
    телефон. Молчаливая склейка 11 августа собрала под одним именем восемь
    человек из разных городов, и повторять это нечем.
    """
    return {"items": await clients_svc.merge_candidates(db, await _get(db, client_id))}


@router.post("/clients/{client_id}/merge")
async def merge_client(
    client_id: uuid.UUID,
    body: MergeRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Объединить карточку `source_id` в карточку из пути.

    Победитель — тот, что в пути: оператор нажимает кнопку, стоя в его
    диалоге, и после операции обязан остаться там же. Уводить его в чужой
    диалог посреди переписки с клиентом нельзя.
    """
    target = await _get(db, client_id)
    source = await _get(db, body.source_id)
    result = await clients_svc.merge_clients(db, winner=target, loser=source, actor=user)
    await db.commit()
    # Кадры — после commit'а (12.09): до этого объединение было видно только
    # нажавшему, коллега в переехавшем диалоге узнавал о нём после F5.
    moved = [uuid.UUID(v) for v in result["conversation_ids"]]
    await clients_events.publish_merge(
        redis, winner=result["target"], loser_id=source.id, moved=moved, reason="merged"
    )
    # Имя победителя могло смениться: его собственные диалоги у коллег иначе
    # остались бы под прежним именем — один человек под двумя именами до F5.
    await clients_events.publish_client_patch(
        db,
        redis,
        target.id,
        {"id": str(target.id), "name": target.name, "phone": target.phone},
        skip=frozenset(moved),
    )
    return result


@router.post("/clients/{client_id}/unmerge")
async def unmerge_client(
    client_id: uuid.UUID,
    body: MergeRequest,
    user: User = Depends(block_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Разъединить: вернуть карточку `source_id` из объединения с карточкой из пути.

    Тело то же, что у объединения, и путь тот же — потому что это ровно
    обратная операция над той же парой. Две разные формы запроса у прямого и
    обратного действия однажды разъехались бы, и «Разъединить» перестало бы
    попадать в ту пару, которую объединяли.
    """
    target = await _get(db, client_id)
    source = await _get(db, body.source_id)
    if source.merged_into_id != target.id:
        raise ApiError(
            "not_merged_into_target",
            "Эта карточка объединена не с той, из которой разъединяют",
            status=422,
        )
    result = await clients_svc.unmerge_clients(db, loser=source, actor=user)
    await db.commit()
    await clients_events.publish_merge(
        redis,
        winner=result["source"],
        loser_id=target.id,
        moved=[uuid.UUID(v) for v in result["conversation_ids"]],
        reason="unmerged",
    )
    # Имя и номер карточки, из которой разъединили, откатились: её оставшиеся
    # диалоги у коллег иначе показывали бы номер другого человека.
    await clients_events.publish_client_patch(
        db, redis, target.id, {"id": str(target.id), "name": target.name, "phone": target.phone}
    )
    return result
