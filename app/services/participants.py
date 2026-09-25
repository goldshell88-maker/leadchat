"""Позвать коллегу в диалог, не отдавая его (docs/19).

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПЕРЕДАЧИ. Передача меняет ответственного — диалог уходит
целиком, и спрос переходит. Приглашение не меняет ничего: ответственный
прежний, отвечает за клиента он же. Позвали затем, что нужен второй человек.

ЧТО ПРИГЛАШЕНИЕ ДАЁТ СВЕРХ ОБЩЕЙ ВИДИМОСТИ. Вкладка «Все» показывает любой
диалог любому оператору. Но «может открыть, если знает адрес» и «увидит» —
разные вещи при четырёхстах тысячах диалогов. Приглашение добавляет две:

* коллега УЗНАЁТ — приходит уведомление с причиной;
* диалог попадает в его «Мои» — список, который он смотрит каждый день.

Без первого зовут в пустоту, без второго диалог тонет на второй день.

ПОЧЕМУ ПОЗВАННЫЙ МОЖЕТ ОТВЕЧАТЬ. Отдельного запрета нет, и это осознанно:
право писать клиенту даёт роль, а не участие в конкретном диалоге. Позвали
мастера — он и напишет клиенту, что деталь снята с производства, вместо
того чтобы пересказывать это диспетчеру, который перепишет своими словами.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Conversation, ConversationParticipant, User
from app.services.user_ref import user_ref


async def participant_ids(
    db: AsyncSession, conv_id: uuid.UUID, *, kind: str | None = None
) -> list[uuid.UUID]:
    """Кто в диалоге кроме ответственного. `kind` сужает до вида участия."""
    условия = [ConversationParticipant.conversation_id == conv_id]
    if kind is not None:
        условия.append(ConversationParticipant.kind == kind)
    rows = await db.execute(sa.select(ConversationParticipant.user_id).where(*условия))
    return list(rows.scalars())


async def invited_ids(db: AsyncSession, conv_id: uuid.UUID) -> list[uuid.UUID]:
    """Только ПОЗВАННЫЕ — те, кому участие даёт права на чужой диалог.

    ⚠ ЗАШЕДШИЙ САМ ПРАВ НЕ ПОЛУЧАЕТ. Участие открывает, например, закрепление
    чужого диалога: позвали помочь — законный повод, «открыл посмотреть» —
    нет. До появления `kind` эти два случая были неразличимы, потому что
    второго попросту не существовало.
    """
    return await participant_ids(db, conv_id, kind="invited")


async def is_guest(db: AsyncSession, conv_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Человек ЗАШЁЛ в этот диалог сам, не будучи ни хозяином, ни позванным.

    ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ВОПРОС (жалоба владельца 04.09: «я закрываю диалог у
    себя — он закрывается и у Петрова»). Гость видит чужой диалог в «Моих»
    наравне со своими, и «Закрыть» в этом списке читается как «убрать у себя».
    А закрытие одно на всех: статус — поле диалога, а не мнение читателя.
    Поэтому спрашивать «а он тут гость?» приходится в момент закрытия.

    Различение `self` от `invited` не новое: позванному участие ДАЁТ права на
    чужой диалог (см. `invited_ids`), зашедшему самому — нет. Закрытие ровно
    такое право.
    """
    row = await db.get(ConversationParticipant, (conv_id, user_id))
    return row is not None and row.kind == "self"


def mine_condition(user_id: uuid.UUID) -> sa.ColumnElement[bool]:
    """«Мои» — это мои как ответственного ИЛИ те, куда меня позвали.

    Одно условие на все выборки «Моих»: разъедься они, и позванный видел бы
    диалог в списке, но не находил бы его фильтром — или наоборот.
    """
    return sa.or_(
        Conversation.assignee_id == user_id,
        # ⚠ И ТЕ, КОТОРЫЕ МНЕ ПЕРЕДАЮТ ПРЯМО СЕЙЧАС (жалоба владельца 28.08:
        # «непонятно, когда и как тебе передают диалог»).
        #
        # Передача двухфазная: пока получатель не принял, ответственный НЕ
        # меняется. До сегодняшнего дня это значило, что увидеть предложенный
        # диалог получателю было НЕГДЕ: в «Моих» его нет (ответственный чужой),
        # в очереди нет (она про ничьи), в «Разборе» он теряется среди сотен.
        # Единственным следом оставался колокольчик — а его закрывают не глядя.
        #
        # Теперь диалог стоит там, куда человек и так смотрит, и открывается с
        # плашкой «Коллега передаёт вам диалог» и кнопками. Ответственный при
        # этом по-прежнему прежний: список — не решение, а приглашение решить.
        Conversation.transfer_to_id == user_id,
        sa.exists().where(
            sa.and_(
                ConversationParticipant.conversation_id == Conversation.id,
                ConversationParticipant.user_id == user_id,
                # ⚠ «ЗАШЁЛ САМ» ДЕЙСТВУЕТ, ПОКА ДИАЛОГ КЕМ-ТО ВЕДЁТСЯ (03.09).
                #
                # Смысл такого участия — «я рядом, пока с клиентом работает
                # коллега». Ушёл ответственный (сторож отнял, диалог вернулся в
                # очередь) — смысла больше нет, и диалог обязан исчезнуть из
                # «Моих» у заглянувших. Иначе он оказался бы одновременно
                # ничьим и «моим» у каждого: зашедший не взял бы его из
                # очереди, считая своим, а очередь звала бы остальных — ровно
                # та пара «двое пишут одному клиенту», против которой сделана
                # вся очередь.
                #
                # ⚠ УСЛОВИЕМ, А НЕ УБОРКОЙ СТРОК. Снимать участие крючками
                # пришлось бы в пяти местах, где диалог теряет ответственного
                # (сторож, возврат в очередь, передача, увольнение, бот), и
                # пятиточечный инвариант сгнил бы на первой же новой ветке.
                # Здесь он один и по построению не расходится.
                #
                # Приглашение так не гаснет: позвали — значит позвали, и
                # человек уходит сам, когда закончил.
                sa.or_(
                    ConversationParticipant.kind != "self",
                    Conversation.assignee_id.is_not(None),
                ),
            )
        ),
    )


async def invite(
    db: AsyncSession,
    conv: Conversation,
    *,
    who: User,
    actor: User,
    reason: str | None = None,
) -> ConversationParticipant:
    """Позвать человека в диалог. Ответственный НЕ меняется."""
    if who.id == conv.assignee_id:
        raise ApiError(
            "unprocessable",
            "Этот сотрудник и так ведёт диалог",
            status=422,
            details={"reason": "already_assignee"},
        )
    existing = await db.get(ConversationParticipant, (conv.id, who.id))
    if existing is not None:
        # Повторный вызов — не ошибка: два человека могли позвать одного и
        # того же. Но и не событие: журнал не должен обрастать строками.
        return existing

    row = ConversationParticipant(
        conversation_id=conv.id,
        user_id=who.id,
        invited_by_id=actor.id,
        reason=(reason or "").strip() or None,
        kind="invited",
    )
    db.add(row)
    await db.flush()
    return row


async def enter(db: AsyncSession, conv: Conversation, *, who: User) -> bool:
    """Человек ОТКРЫЛ чужой рабочий диалог. `True` — записали впервые.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 03.09: «чтобы можно было спокойно заходить в чужой
    диалог, который в работе у другого человека, и он появлялся так же у тебя
    в „Мои"». Дальше диалог попадает в «Мои» сам — общим условием
    `mine_condition`, без единой правки в выборках и счётчиках.

    ⚠ ЭТО НЕ ПРИГЛАШЕНИЕ, И ПОЭТОМУ ОТДЕЛЬНАЯ ФУНКЦИЯ. Пройди вход через
    `invite`, и на каждое открытие чужого диалогачеловек получал бы уведомление ОТ
    САМОГО СЕБЯ, в ленте появлялась бы строка «Иванов позвал(а) в диалог:
    Иванов», а кадр о новом сообщении улетал бы всем тринадцати сессиям. Вход
    обязан быть ТИХИМ: ни колокольчика, ни строки в ленте, ни звука.

    ⚠ У ОТВЕТСТВЕННОГО ВХОДА НЕТ. Свой диалог и так в «Моих», а строка
    участника рядом с ответственностью — второе имя одному и тому же.
    """
    if who.id == conv.assignee_id:
        return False
    existing = await db.get(ConversationParticipant, (conv.id, who.id))
    if existing is not None:
        # Уже здесь — хоть позванным, хоть зашедшим. Приглашение НЕ понижаем
        # до «зашёл»: права, данные осознанно, не отбирают открытием экрана.
        return False
    db.add(
        ConversationParticipant(
            conversation_id=conv.id,
            user_id=who.id,
            invited_by_id=None,  # никто не звал — пришёл сам
            reason=None,
            kind="self",
        )
    )
    await db.flush()
    return True


async def leave(db: AsyncSession, conv: Conversation, *, user_id: uuid.UUID) -> bool:
    """Убрать участника. ``False`` — его там и не было."""
    row = await db.get(ConversationParticipant, (conv.id, user_id))
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def view(db: AsyncSession, conv_id: uuid.UUID) -> list[dict[str, Any]]:
    """Кто позван — для карточки диалога.

    Имена нужны сразу: строка «позваны: Борис, Сергей» отвечает на вопрос
    «кто ещё здесь», а список идентификаторов не отвечает ни на что.
    """
    rows = list(
        (
            await db.execute(
                sa.select(ConversationParticipant, User)
                .join(User, User.id == ConversationParticipant.user_id)
                .where(ConversationParticipant.conversation_id == conv_id)
                .order_by(ConversationParticipant.invited_at)
            )
        ).all()
    )
    return [
        {
            # Ссылка на человека — общей сборкой: отдел в скобках у позванного
            # обязан совпасть с тем, что стоит у ответственного и у автора
            # реплики, иначе один человек прочтётся как двое.
            **user_ref(u),
            "reason": p.reason,
            # Вид участия нужен ЭКРАНУ, а не только серверу: пункт «Закрыть
            # диалог» гостю не показывают, потому что сервер ему всё равно
            # откажет (403 `guest_cannot_close`). Пункт, который всегда
            # отвечает отказом, хуже отсутствующего.
            "kind": p.kind,
            "invited_at": p.invited_at.isoformat().replace("+00:00", "Z") if p.invited_at else None,
        }
        for p, u in rows
    ]
