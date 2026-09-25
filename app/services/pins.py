"""Закрепить диалог у себя (требование заказчика от 7 августа).

«Каждый может для себя лично закреплять свои взятые диалоги».

ЧТО ЭТО ДАЁТ. Закреплённые поднимаются в начало ЕГО списка и не тонут, когда
приходят новые обращения. Отметка «я к этому вернусь»: клиент обещал
перезвонить, ждём деталь, договорились на четверг.

ТРИ ГРАНИЦЫ, И КАЖДАЯ ОПЛАЧЕНА
-------------------------------
1. **Личное.** Общее закрепление на тринадцать человек превращается в свалку
   за неделю: закрепляют все, снимает никто. Плюс спор «зачем ты открепил
   мой», у которого нет правильного ответа.
2. **Только свои.** Закрепить чужой диалог — значит поднять наверх своего
   списка то, чего ты не ведёшь. Для наблюдения есть поиск и вкладка «Все».
3. **Потолок.** Закрепить можно немного. Отметка работает, пока их единицы:
   двадцать закреплённых — это просто ещё один список, только сверху.

ГДЕ ЗАКРЕПЛЕНИЕ НЕ ДЕЙСТВУЕТ. В очереди «Входящие». Там порядок — кто дольше
ждёт, и он не украшение, а суть: оператор жмёт «Принять» на верхней строке.
Пустить туда личные отметки значило бы разрешить поднимать себе удобное
поверх клиента, который ждёт дольше всех.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Conversation, ConversationPin, User
from app.services import participants

#: Сколько диалогов можно держать закреплёнными. Семь — не круглое число:
#: столько строк списка помещается на экране ноутбука выше сгиба, и закрепив
#: больше, человек начинает прокручивать собственные закладки.
MAX_PINS = 7


async def pinned_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(
        sa.select(ConversationPin.conversation_id).where(ConversationPin.user_id == user_id)
    )
    return set(rows.scalars())


def _assert_mine(conv: Conversation, user: User) -> None:
    """Свой диалог — тот, что за мной, или тот, куда меня позвали."""
    if conv.assignee_id == user.id:
        return
    raise ApiError(
        "unprocessable",
        "Закреплять можно только свои диалоги",
        status=422,
        details={"reason": "not_mine"},
    )


async def pin(db: AsyncSession, conv: Conversation, user: User) -> ConversationPin:
    """Закрепить у себя. Повтор — не ошибка и не дубль."""
    existing = await db.get(ConversationPin, (user.id, conv.id))
    if existing is not None:
        return existing

    # ⚠ ТОЛЬКО ПОЗВАННЫЕ, А НЕ ВСЕ УЧАСТНИКИ (03.09). С появлением свободного
    # входа в чужой диалог участником становится и тот, кто просто открыл
    # посмотреть. Права «своего» ему не полагаются: закрепление чужого диалога
    # — законный повод для позванного помогать, а не для случайного зрителя.
    if conv.assignee_id != user.id and user.id not in set(
        await participants.invited_ids(db, conv.id)
    ):
        _assert_mine(conv, user)

    count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(ConversationPin)
            .where(ConversationPin.user_id == user.id)
        )
    ).scalar_one()
    if count >= MAX_PINS:
        raise ApiError(
            "unprocessable",
            f"Больше {MAX_PINS} закреплённых — открепите что-нибудь",
            status=422,
            details={"reason": "too_many_pins", "limit": MAX_PINS},
        )

    row = ConversationPin(user_id=user.id, conversation_id=conv.id)
    db.add(row)
    await db.flush()
    return row


async def unpin(db: AsyncSession, conv_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Открепить. ``False`` — закреплено и не было."""
    row = await db.get(ConversationPin, (user_id, conv_id))
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True
