"""Передача диалога с подтверждением получателя.

ТРЕБОВАНИЕ ЗАКАЗЧИКА, 7 АВГУСТА
--------------------------------
«Диалог не считается переданным, если другой сотрудник его не принял».

До этого передача была мгновенной: оператор нажимал «Передать», и
ответственность уходила в ту же секунду — даже если коллега обедал, был не в
сети или просто не заметил. Клиент оставался без ответа, а спросить было не с
кого: передавший считал, что дело сделано, принимающий — что ему ничего не
давали. Это не гипотетический случай, а самый обычный способ потерять клиента
на ровном месте.

КАК УСТРОЕНО
------------
Передача — предложение, а не действие. Пока оно висит:

* ``assignee_id`` НЕ меняется. Диалог числится за передающим, он отвечает
  клиенту, диалог остаётся в его «Моих». Это главное свойство: ответственность
  не может повиснуть в воздухе между двумя людьми.
* у получателя диалог помечен «предложен вам» с кнопками «Принять» и
  «Отказаться»;
* у передающего — «ждёт подтверждения».

Принял — ответственный меняется. Отказался — предложение снимается, диалог
остаётся у передающего, и тот узнаёт об отказе. Промолчал дольше
:data:`TRANSFER_TIMEOUT_MINUTES` — предложение снимается само, потому что
«висит вечно» здесь равно «потеряли».

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ОЧЕРЕДИ «ВХОДЯЩИЕ»
-----------------------------------------
Соблазн переиспользовать очередь велик: там уже есть принятие, отказ и
эскалация. Но очередь означает «диалог ничей, возьмите кто-нибудь», а
передача адресна: она предложена ОДНОМУ человеку и означает «прошу тебя,
потому что ты в теме». Свали их в одно — и адресность потеряется: диалог,
который передавали мастеру по холодильникам, заберёт первый освободившийся.

ЧТО НЕ СТАЛО ДВУХФАЗНЫМ
------------------------
Три случая остались мгновенными, и каждый по своей причине:

* **взять себе** — принимать нечего, человек уже согласен;
* **снять ответственного** — диалог уходит в очередь, а у неё своё принятие;
* **назначение диалога, у которого ответственного НЕТ** — это не передача, а
  раздача: отнимать не у кого, и ждать подтверждения означало бы оставить
  клиента без ответа ради формальности.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from app.core.errors import ApiError
from app.models import Conversation, User
from app.services import conversation_status as status_dict
from app.services.user_ref import user_ref, user_ref_parts

#: Сколько предложение ждёт ответа. Пятнадцать минут — компромисс: меньше, и
#: человек не успеет вернуться с обеда; больше, и клиент ждёт слишком долго
#: у того, кто уже мысленно отдал диалог.
TRANSFER_TIMEOUT_MINUTES = 15


#: Весть получателю «Вам передали диалог».
OFFERED = "conversation.assigned"

#: Весть получателю «принимать нечего»: предложение отменили, переадресовали,
#: оно истекло или устарело. Интерфейс по этому виду замещает карточку
#: предложения с кнопками «Принять / Отклонить».
WITHDRAWN = "conversation.transfer_cancelled"


def is_pending(conv: Conversation) -> bool:
    """Висит ли предложение передачи."""
    return conv.transfer_to_id is not None


def notice_key(kind: str, conv_id: uuid.UUID, offered_at: datetime | None) -> str:
    """Ключ склейки вестей о передаче: одна строка на одно предложение.

    Склейка по диалогу глушила бы повтор: второе «Передачу не приняли» по тому
    же диалогу ложилось в непрочитанную первую строку и не доходило вовсе.
    """
    stamp = offered_at.isoformat() if offered_at else "-"
    return f"{kind}:conversation:{conv_id}:{stamp}"


@dataclass(frozen=True)
class Notice:
    """Одна весть о судьбе предложения — до записи в центр уведомлений."""

    kind: str
    recipient_id: uuid.UUID
    title: str
    body: str
    dedup_key: str


def outcome_notices(
    kind: str,
    *,
    conv_id: uuid.UUID,
    offered_at: datetime | None,
    owner_id: uuid.UUID | None,
    offered_by: uuid.UUID | None,
    owner_name: str,
    what: str,
) -> list[Notice]:
    """Кому сказать, что передача не состоялась (отказ или таймаут).

    Владельцу — «диалог остался у вас»: он ответственный, и клиент ждёт его.
    Предлагавшему, если это не владелец (администратор, руководитель), — у кого
    диалог остался: «у вас» было бы неправдой.
    """
    title = {
        "conversation.transfer_declined": "От передачи отказались",
        "conversation.transfer_expired": "Передачу не приняли",
    }[kind]
    key = notice_key(kind, conv_id, offered_at)
    notices = []
    if owner_id is not None:
        notices.append(
            Notice(
                kind,
                owner_id,
                f"{title} — диалог остался у вас",
                f"{what} Диалог по-прежнему числится за вами: ответьте сами или передайте другому.",
                key,
            )
        )
    if offered_by is not None and offered_by != owner_id:
        notices.append(
            Notice(kind, offered_by, title, f"{what} Диалог остался за {owner_name}.", key)
        )
    return notices


def withdrawn_notice(
    *,
    conv_id: uuid.UUID,
    offered_at: datetime | None,
    recipient_id: uuid.UUID,
    title: str,
    body: str,
) -> Notice:
    """Весть получателю, что предложение больше не действует."""
    return Notice(WITHDRAWN, recipient_id, title, body, notice_key(WITHDRAWN, conv_id, offered_at))


def clear(conv: Conversation) -> None:
    """Снять предложение — общая точка для принятия, отказа и таймаута.

    Отдельной функцией, а не тремя присваиваниями на местах: полей четыре, и
    забытое `transfer_comment` осталось бы висеть в интерфейсе объяснением к
    предложению, которого уже нет.
    """
    conv.transfer_to_id = None
    conv.transfer_by_id = None
    conv.transfer_from_id = None
    conv.transfer_at = None
    conv.transfer_comment = None


def offer(
    conv: Conversation,
    *,
    to: User,
    actor: User,
    comment: str | None = None,
    now: datetime | None = None,
) -> None:
    """Предложить диалог коллеге. Ответственный НЕ меняется."""
    if to.id == conv.assignee_id:
        raise ApiError(
            "unprocessable",
            "Диалог уже ведёт этот сотрудник",
            status=422,
            details={"reason": "same_assignee"},
        )
    if to.id == actor.id:
        # Взять себе — не передача: принимать нечего, человек уже согласен.
        # Такой вызов означает ошибку в коде выше, а не решение человека.
        raise ApiError(
            "unprocessable",
            "Взять диалог себе можно без передачи",
            status=422,
            details={"reason": "self_transfer"},
        )
    conv.transfer_to_id = to.id
    conv.transfer_by_id = actor.id
    conv.transfer_from_id = conv.assignee_id
    conv.transfer_at = now or datetime.now(UTC)
    conv.transfer_comment = comment


def _assert_recipient(conv: Conversation, actor: User) -> None:
    if not is_pending(conv):
        raise ApiError(
            "unprocessable",
            "Этот диалог вам не передавали",
            status=422,
            details={"reason": "no_pending_transfer"},
        )
    if conv.transfer_to_id != actor.id:
        # Отвечать за предложение может только тот, кому его сделали. Иначе
        # коллега «помог» бы принять диалог за отсутствующего — и тот вернулся
        # бы к работе, которую не брал.
        raise ApiError(
            "forbidden",
            "Диалог передан другому сотруднику",
            status=403,
            details={"reason": "not_your_transfer"},
        )


#: Причины, по которым предложение уже нельзя принять. Такое предложение
#: снимается сразу, а не висит до таймаута с кнопкой, которая всегда отказывает.
STALE_REASONS = frozenset({"conversation_closed", "owner_changed"})


def _stale_reason(conv: Conversation) -> str | None:
    if conv.status == "closed":
        # Закрытие снимает предложение само (change_status); этот рубеж — для
        # гонки «закрыли ровно в тот момент, когда коллега жал „Принять“».
        return "conversation_closed"
    # Предложение живёт 15 минут, и за это время диалог могли забрать или
    # вернуть в очередь. Сверяем с владельцем на момент предложения: предлагать
    # может и не он (администратор, руководитель). Предложения, сделанные до
    # появления колонки, её не знают — для них владельцем был предлагавший.
    owner_at_offer = conv.transfer_from_id or conv.transfer_by_id
    if conv.assignee_id is None or conv.assignee_id != owner_at_offer:
        return "owner_changed"
    return None


_STALE_MESSAGES = {
    "conversation_closed": "Диалог уже закрыт — принимать нечего",
    "owner_changed": "Диалог уже у другого человека — предложение устарело",
}


def accept(conv: Conversation, *, actor: User) -> uuid.UUID | None:
    """Принять переданный диалог. Возвращает прежнего ответственного.

    Устаревшее предложение снимается и даёт 422 с причиной из
    :data:`STALE_REASONS`; снятие вызывающий обязан сохранить.
    """
    _assert_recipient(conv, actor)
    reason = _stale_reason(conv)
    if reason is not None:
        clear(conv)
        raise ApiError(
            "unprocessable", _STALE_MESSAGES[reason], status=422, details={"reason": reason}
        )
    previous = conv.assignee_id
    conv.assignee_id = actor.id
    # Диалог взяли руками: отметка автораздачи больше не про него, иначе сторож
    # возврата (7.7) отобрал бы только что принятый диалог.
    from app.services.inbox import clear_auto_assignment

    clear_auto_assignment(conv)
    # ПРИНЯТИЕ СНИМАЕТ ОТЛОЖКУ ПРЕЖНЕГО ВЛАДЕЛЬЦА (docs/38, крайние случаи).
    #
    # Передача двухфазная: пока предложение висит, `assignee_id` не меняется и
    # отложка остаётся у того, кто её ставил, — это верно, диалог всё ещё его.
    # Но принявший про чужой срок не знает и знать не должен; оставь мы
    # `snoozed_until`, сторож через час разбудил бы диалог у нового хозяина,
    # объяснив это решением, которого тот не принимал.
    #
    # Статус здесь НЕ трогается напрямую: `ensure_in_progress` умеет и «новый»,
    # и «отложенный», а «ждёт клиента» намеренно оставляет как есть — передали
    # диалог, в котором ход за клиентом, и ход остался за ним.
    status_dict.clear_snooze(conv)
    if conv.status in ("new", "snoozed"):
        status_dict.set_status(conv, "in_progress")
    clear(conv)
    return previous


def decline(conv: Conversation, *, actor: User) -> uuid.UUID | None:
    """Отказаться. Диалог остаётся у того, кто передавал.

    Возвращает того, кому надо сообщить об отказе: он ждёт ответа и должен
    узнать, что диалог по-прежнему его, — иначе решит, что передал, и клиент
    останется без ответа.
    """
    _assert_recipient(conv, actor)
    offered_by = conv.transfer_by_id
    clear(conv)
    return offered_by


def cancel(
    conv: Conversation, *, actor: User, may_manage_any: bool
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Снять СВОЁ предложение. Возвращает (кому предлагали, кто предлагал).

    ЗАЧЕМ ЭТО ЕСТЬ (просьба владельца 04.09: «чтобы можно было отменять
    передачу, если я случайно начал передавать не тому человеку»). Ошибиться
    адресатом легко: список сотрудников в окне передачи длинный, а имена
    похожи. До этого ошибку нельзя было забрать назад — предложение висит
    пятнадцать минут, и всё это время передающий либо ждёт чужого отказа, либо
    идёт просить коллегу в мессенджере. Диалог при этом за ним, клиент ждёт.

    ОТВЕТСТВЕННЫЙ НЕ МЕНЯЕТСЯ, и менять его тут нечего: пока предложение
    висит, диалог и так числится за передающим (см. шапку модуля). Отмена
    возвращает ровно то состояние, что было до нажатия «Передать».

    КТО МОЖЕТ. Тот, кто предложение сделал, — и обладатель
    `conversations:manage`. Второе не поблажка: этим же правом открывается
    `/assign`, а он предложение ПЕРЕЗАПИСЫВАЕТ (`offer` кладёт нового
    адресата поверх старого). Запрети мы отмену — тот же человек добился бы
    того же назначением диалога третьему лицу, то есть более грубым способом.

    ⚠ ПРАВО СПРАШИВАЕТСЯ АРГУМЕНТОМ, А НЕ ЗДЕСЬ: матрица прав живёт в
    `api/deps`, а этот модуль про сам механизм передачи и в тестах зовётся
    без HTTP вовсе (tests/unit/test_transfer.py). Ручка передаёт готовый ответ
    `has_permission(user, "conversations:manage")`.
    """
    if not is_pending(conv):
        # Принятие и отказ снимают предложение оба, и различить их по строке
        # диалога уже нельзя: полей передачи нет ни в одном из случаев.
        # Причина поэтому одна и честная — «отвечать больше не на что»;
        # что именно случилось, человек увидит в ленте следующим кадром.
        raise ApiError(
            "unprocessable",
            "Предложение уже снято — коллега успел ответить",
            status=422,
            details={"reason": "no_pending_transfer"},
        )
    if conv.transfer_by_id != actor.id and not may_manage_any:
        raise ApiError(
            "forbidden",
            "Отменить передачу может тот, кто её начал",
            status=403,
            details={"reason": "not_your_transfer"},
        )
    offered_to, offered_by = conv.transfer_to_id, conv.transfer_by_id
    clear(conv)
    return offered_to, offered_by


def expired_condition(now: datetime | None = None) -> sa.ColumnElement[bool]:
    """Предложения, провисевшие дольше отведённого."""
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=TRANSFER_TIMEOUT_MINUTES)
    return sa.and_(
        Conversation.transfer_to_id.is_not(None),
        Conversation.transfer_at < cutoff,
    )


def view(conv: Conversation, users: dict[uuid.UUID, User]) -> dict[str, Any] | None:
    """Как предложение выглядит в API. ``None`` — предложения нет.

    Имена подставляются из готового словаря, а не догружаются здесь: строк
    списка пятьдесят, и запрос на каждую превратил бы открытие экрана в
    полсотни походов в базу.
    """
    if not is_pending(conv):
        return None
    to = users.get(conv.transfer_to_id) if conv.transfer_to_id else None
    by = users.get(conv.transfer_by_id) if conv.transfer_by_id else None
    return {
        # «—» вместо имени — не украшение: строка сотрудника могла уехать
        # (удаление), а предложение осталось. Окно передачи обязано
        # нарисоваться и в этом случае, иначе диалог зависает без кнопок.
        "to": user_ref(to) or user_ref_parts(conv.transfer_to_id, "—"),
        "by": (
            (user_ref(by) or user_ref_parts(conv.transfer_by_id, "—"))
            if conv.transfer_by_id
            else None
        ),
        "at": conv.transfer_at.isoformat().replace("+00:00", "Z") if conv.transfer_at else None,
        "comment": conv.transfer_comment,
    }
