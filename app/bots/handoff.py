"""Передача диалога оператору + служебные записи бота в ленту (02 §4).

`do_handoff` — единая идемпотентная процедура для ВСЕХ шести условий DESIGN
§4.3: и для шага `handoff` сценария, и для дефолтных веток
(`on_timeout`/`on_invalid`/`on_no_match`/`on_low_confidence` = null), и для
предохранителей зацикливания, и для недоступности AI. Реестр причин —
:data:`HANDOFF_REASONS` (он же словарь `details.reason` в контракте журнала
06 §0.3).

Условие №6 (оператор вмешался) сюда НЕ входит: диалог уже в руках человека —
статус не трогаем, заметок не пишем, только `bot.muted`
(:func:`app.bots.runtime.mute_bot`).

Публикация событий и постановка задач не делаются здесь: движок копит их в
:class:`~app.bots.state.Outbox`, а отправляет их вызывающий строго ПОСЛЕ
commit'а (08 §8.1).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.state import BotState, Outbox, utcnow
from app.models import Conversation, Message
from app.services import conversation_status as status_dict
from app.services import inbox, notifications
from app.services.audit import write_audit
from app.services.conversations import message_out
from app.ws.events import iso
from app.ws.hub import INBOX_ELIGIBLE_KEY, INBOX_NEW

log = structlog.get_logger("app.bots.handoff")

NEGATIVE_TAG = "негатив"

# Полный реестр причин (02 §4; словарь `details.reason` в 06 §0.3).
HANDOFF_REASONS: dict[str, str] = {
    "client_request": "клиент попросил живого человека",
    "ai_low_confidence": "AI не уверен в ответе",
    "negative": "негатив или жалоба клиента",
    "offscript": "клиент пишет мимо сценария",
    "scenario": "сценарий дошёл до передачи менеджеру",
    "ask_timeout": "клиент не ответил в отведённое время",
    "ask_invalid": "не удалось получить корректный ответ",
    "loop_protection": "сработала защита от зацикливания",
    "ai_unavailable": "AI недоступен",
    "scenario_changed": "сценарий изменился во время диалога",
    # ⚠ ОКОНЧАТЕЛЬНЫЙ ОТКАЗ ЗАКАНЧИВАЕТ РАБОТУ БОТА (снимок владельца 26.08). Клиент
    # спросил замену матрицы, бот верно ответил «с битой матрицей не помогу» — и через
    # пять минут сам же дожал «Ну что, расскажете, что случилось?». Владелец: «зачем бот
    # ведёт дальше диалог, когда понятно, что тут матрица». Спрашивать после отказа
    # нечего: тема закрыта, а сценарий шёл на следующий шаг и досиживал его таймаут.
    "refused": "отказ по регламенту — тема закрыта",
    # ⚠ ПРИЧИНЫ БЭКЕНДА. До 26.08 ЛЮБАЯ передача от `ai_answer` называлась «AI не уверен
    # в ответе» — даже когда бэкенд прямо сказал, почему зовёт человека. Снимки владельца:
    # в очереди висит «AI не уверен в ответе», а рядом, заметкой, честное «клиент звонил»
    # и «тема по согласованию». Оператор сортирует очередь по первому и не читает второе.
    # Лид-бот отдаёт причину в `meta.escalation.reason` — теперь она и становится причиной
    # передачи. Список ниже — его словарь (brain/escalation.py::REASONS), слово в слово.
    "claim": "претензия к работе или деньгам",
    "prior_master": "сомнение в предыдущем мастере — проверить нельзя",
    "cancel": "отказ от заявки",
    "status": "вопрос о статусе визита",
    "reschedule": "перенос или сдвиг времени",
    "no_show": "срыв визита",
    "discontent": "недовольство с агрессией или после работы",
    "unknown_topic": "тема не распознана",
    "soglasovanie": "тема по согласованию — обещали вернуться с ответом",
    "call_request": "клиент просит позвонить",
    "call_made": "клиент звонил — о чём говорили, знает только оператор",
    "model_silent": "модель не ответила — клиенту не ушло ничего",
    "blocklist": "профиль из чёрного списка — отвечать не нужно",
    "multi_account": "один профиль более чем на 5 наших аккаунтов — сливаем",
    "troll_exit": "три хода не по делу — разговор закрыт",
    "offtopic": "тема вне ремонта — бот отказал сам",
    "model_handoff": "модель не справилась и позвала человека",
    "voice": "голосовое сообщение — бот его не слышит",
    "not_primary": "диалог уже ведёт человек — бот не вмешивается",
    "self_repeat": "бот повторил бы себя — нового сказать нечего",
    # Бота выключили или сняли с канала, пока он вёл диалог (проверка 24.09):
    # диалог возвращается людям, а не остаётся скрытым от очереди.
    "bot_disabled": "бот выключен или снят с канала — диалог отдан людям",
    "bot_to_suggest": "бот переведён в подсказки — диалог отдан людям",
    "bot_stalled": "бот остановился посреди сценария — диалог отдан людям",
}
DEFAULT_REASON = "scenario"

#: Событие журнала, которым сторож помечает зависшего бота (`bot_stuck.py`).
#: Живёт здесь, рядом с причинами, потому что на экране «Диалоги бота» оно
#: стоит в одном ряду с ними и отвечает на тот же вопрос — чем кончилось.
STUCK_ACTION = "conversation.bot_stuck_released"
#: Бот сам закрыл диалог: отказ по регламенту, собранная заявка, шаг `close`
#: (проверка 24.09). Передачей это не является, и экран «Диалоги бота» до
#: этого события закрытий бота не видел вовсе — плитка «Бот закрыл сам» была 0.
BOT_CLOSED_ACTION = "bot.closed"
#: События журнала, по которым виден путь бота в диалоге, — одни на экран
#: «Диалоги бота» и на отбор таблицы по плитке.
BOT_EVENTS: tuple[str, ...] = (STUCK_ACTION, "bot.handoff", BOT_CLOSED_ACTION)
#: Закрытия ботом, которые сбоем не являются, — своими словами. Отказ по
#: регламенту (`refused`) — не здесь: он и есть плитка «Бот закрыл сам».
CLOSE_REASON_LABELS: dict[str, str] = {
    "lead_ready": "заявка собрана — бот закрыл",
    "client_closed": "клиент завершил разговор — бот закрыл",
    "scenario": "закрыт шагом сценария",
}

#: Разбор причин по видам сбоя — для экрана «Диалоги бота» (docs/45).
#:
#: ⚠ СЮДА ВХОДЯТ НЕ ВСЕ ПРИЧИНЫ, И ЭТО НАМЕРЕННО. `client_request`,
#: `scenario`, `call_made` и им подобные — штатная работа: бот сделал что
#: должен и позвал человека. Сбоем это не является, и в плитки такое не
#: попадает. Экран отвечает на вопрос «где бот навредил», а не «сколько раз
#: он передавал диалог».
#:
#: ⚠ ГРУППА `closed_itself` — САМАЯ ДОРОГАЯ, ХОТЯ ОБЫЧНО САМАЯ МАЛЕНЬКАЯ.
#: Остальные пять — это «бот не справился и позвал человека»: плохо, но
#: клиент остаётся у нас. Здесь бот НЕ позвал: решил сам, что помогать не
#: нужно, и разговор кончился. Ровно этот случай владелец разбирал 26 августа
#: («зачем бот ведёт дальше диалог, когда понятно, что тут матрица»), и ровно
#: поэтому группа стоит первой на экране независимо от величины счётчика.
OUTCOME_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "closed_itself": ("Бот закрыл сам", ("refused", "offtopic", "troll_exit")),
    "misunderstood": (
        "Не понял",
        ("unknown_topic", "offscript", "ask_invalid", "ask_timeout"),
    ),
    "looped": ("Зациклился", ("loop_protection", "self_repeat")),
    "stuck": ("Бот завис", ()),  # не причина передачи, а событие STUCK_ACTION
    "unhappy": ("Клиент недоволен", ("negative", "discontent", "claim")),
    "silent": ("Промолчал", ("model_silent", "ai_unavailable")),
}

#: ⚠ ОТДЕЛЬНО ОТ СБОЕВ, И ЭТО ГЛАВНОЕ В ЭТОЙ ЗАПИСИ.
#:
#: `ai_low_confidence` — 210 передач из 325 на боевом трафике за 30 дней
#: (замер 29.08). Это САМАЯ ЧАСТАЯ причина и при этом НЕ сбой: бот честно
#: зовёт человека вместо того, чтобы гадать, — ровно то поведение, которого от
#: него и хотят.
#:
#: Поставь мы её в один ряд с зацикливанием и отказами — экран сказал бы
#: «210 сбоев» про правильную работу, и на его фоне единственный настоящий
#: отказ по регламенту потерялся бы совсем.
#:
#: Но и прятать нельзя: каждый такой случай — диалог, который бот не довёл.
#: Это не дефект, а потолок его возможностей, и по нему видно, куда бота
#: дорабатывать. Поэтому — своя группа, отдельная строка на экране, другая
#: подпись.
OUTCOME_CAPACITY: dict[str, tuple[str, tuple[str, ...]]] = {
    "not_capable": ("Не смог сам", ("ai_low_confidence", "model_handoff")),
}

OUTCOME_GROUPS = {**OUTCOME_GROUPS, **OUTCOME_CAPACITY}

#: Порядок плиток на экране. НЕ по величине счётчика: сортировка по числу
#: подняла бы наверх самый частый сбой вместо самого дорогого.
OUTCOME_ORDER: tuple[str, ...] = (
    "closed_itself",
    "misunderstood",
    "looped",
    "stuck",
    "unhappy",
    "silent",
)

#: Что рисуется отдельной строкой под плитками сбоев.
CAPACITY_ORDER: tuple[str, ...] = ("not_capable",)

#: Все группы, которые считает экран, в порядке показа.
ALL_OUTCOMES: tuple[str, ...] = OUTCOME_ORDER + CAPACITY_ORDER


def outcome_label(group: str) -> str:
    return OUTCOME_GROUPS[group][0] if group in OUTCOME_GROUPS else group


def reasons_of(group: str) -> tuple[str, ...]:
    return OUTCOME_GROUPS[group][1] if group in OUTCOME_GROUPS else ()


# Человеческие названия собранных переменных для сводки менеджеру.
VAR_LABELS: dict[str, str] = {
    "phone": "Телефон",
    "problem": "Проблема",
    "device_kind": "Тип техники",
    "device_brand": "Бренд",
    "device_model": "Модель",
}


def reason_label(reason: str) -> str:
    return HANDOFF_REASONS.get(reason, reason)


def reason_tags(reason: str) -> list[str]:
    """Автотеги причины: негатив поднимает диалог в топ списка (01 §5.1)."""
    return [NEGATIVE_TAG] if reason == "negative" else []


async def notify_negative(db: AsyncSession, conv: Conversation, outbox: Any) -> None:
    """Поднять «Клиент недоволен» (14 §2.3) — из передачи и из классификатора.

    ⚠ УСЛОВИЕ БЫЛО НЕ ТЕМ, ЧТО ИМЕЛИ В ВИДУ. Раньше здесь стояло
    `if NEGATIVE_TAG in added_tags`, то есть «тег дописан ИМЕННО этой строкой».
    А `BotEngine.classify` вешает тег САМ, до передачи (engine.py:1580) — и к
    моменту передачи дописывать уже нечего: `add_conversation_tags` возвращает
    пустой список, потому что тег на месте. Условие не выполнялось никогда, ни
    в автоответе, ни в подсказке: строка в каталоге была, уведомления не было.
    Спрашиваем то, что имели в виду, — причину передачи.

    Второй вызов — из классификатора, когда диалог уже отдан человеку по другой
    причине и повторная передача не нужна: недовольство клиента от этого никуда
    не девается. Повтор по одному диалогу безопасен — `notify` склеивает его по
    сущности (`dedup="entity"`) в ту же строку, поднимая `repeat_count`.
    """
    outbox.notification(
        await notifications.notify(
            db,
            kind="conversation.negative",
            title="Клиент недоволен",
            body=(
                "Бот распознал негатив и передал диалог человеку. "
                "Такое лучше посмотреть сразу: клиент уже недоволен, и время здесь "
                "работает против нас."
            ),
            entity_type="conversation",
            entity_id=str(conv.id),
        )
    )


# ------------------------------------------------- записи бота в ленту диалога


def _new_message(conv: Conversation, *, now: datetime, **kwargs: Any) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        external_message_id=None,
        sender_user_id=None,
        attachments=[],
        created_at=now,
        **kwargs,
    )


def add_bot_message(
    db: AsyncSession, conv: Conversation, body: str, *, now: datetime | None = None
) -> Message:
    """Исходящее бота: `sender_type='bot'`, доставка общим контуром (08 §3)."""
    msg = _new_message(
        conv,
        now=now or utcnow(),
        direction="out",
        sender_type="bot",
        body=body,
        delivery_status="pending",
    )
    db.add(msg)
    return msg


def add_note(
    db: AsyncSession, conv: Conversation, body: str, *, now: datetime | None = None
) -> Message:
    """Внутренняя заметка бота (02 §1.3 `note`): видна только сотрудникам."""
    msg = _new_message(
        conv,
        now=now or utcnow(),
        direction="note",
        sender_type="bot",
        body=body,
        delivery_status="delivered",
    )
    db.add(msg)
    return msg


def add_system_message(
    db: AsyncSession, conv: Conversation, body: str, *, now: datetime | None = None
) -> Message:
    """Системная запись в ленту — клиенту не уходит (как в 01 §5.4)."""
    msg = _new_message(
        conv,
        now=now or utcnow(),
        direction="system",
        sender_type="system",
        body=body,
        delivery_status="delivered",
    )
    db.add(msg)
    return msg


def message_new_event(conv: Conversation, msg: Message) -> dict[str, Any]:
    """Кадр `message:new` (01 §11.3) без дельты непрочитанных: служебные
    записи и сообщения бота бейдж не двигают."""
    return {
        "conversation_id": str(conv.id),
        "message": message_out(msg),
        "conversation_patch": {"unread_delta": 0, "status": conv.status},
    }


def conversation_updated_event(conv: Conversation, patch: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": str(conv.id), "patch": patch}


def add_conversation_tags(conv: Conversation, tags: Iterable[str]) -> list[str]:
    """Дописать теги без дублей, сохранив порядок. Возвращает добавленные.

    Строка диалога уже взята `FOR UPDATE` тиком (02 §2.3), поэтому read-modify-write
    здесь безопасен и не требует `array(SELECT DISTINCT unnest(...))` на стороне БД —
    заодно работает на SQLite в юнит-тестах.
    """
    current = list(conv.tags or [])
    added: list[str] = []
    for tag in tags:
        clean = str(tag).strip()
        if not clean or clean in current:
            continue
        current.append(clean)
        added.append(clean)
    if added:
        conv.tags = current  # новый список — иначе ORM не увидит изменения
    return added


# ------------------------------------------------------------------- сводка


def handoff_summary(
    reason: str,
    comment: str | None,
    values: Mapping[str, Any],
    entities: Mapping[str, Any] | None = None,
    *,
    step: str | None = None,
) -> str:
    """Заметка-сводка менеджеру: причина + всё, что собрал бот (02 §4).

    Именно она закрывает требование DESIGN §4.3 «менеджер видит собранные
    переменные». Технические переменные (с `_` в начале, напр. `_ai_confidence`)
    в сводку не идут.
    """
    lines = [f"🤖 Бот передал диалог менеджеру. Причина: {reason_label(reason)} ({reason})"]
    if step:
        lines.append(f"Шаг сценария: {step}")
    if comment:
        lines.append(comment)

    collected: dict[str, Any] = {}
    for key, value in values.items():
        if key.startswith("_") or value is None or str(value).strip() == "":
            continue
        collected[key] = value
    for key, value in (entities or {}).items():
        # AI-извлечение не перетирает собранное шагами `ask` (02 §3.5).
        if value is None or str(value).strip() == "" or key in collected:
            continue
        collected[key] = value

    if collected:
        lines.append("Собрано ботом:")
        lines += [f"• {VAR_LABELS.get(k, k)}: {v}" for k, v in collected.items()]
    return "\n".join(lines)


def handoff_system_text(reason: str) -> str:
    return f"🤖 Бот передал диалог менеджеру (причина: {reason_label(reason)})"


# ------------------------------------------------------------------ handoff


async def do_handoff(
    db: AsyncSession,
    conv: Conversation,
    state: BotState,
    outbox: Outbox,
    *,
    reason: str,
    bot_id: Any = None,
    comment: str | None = None,
    tags: Iterable[str] = (),
    entities: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    suggest: bool = False,
) -> bool:
    """Передать диалог оператору. Идемпотентно: повторный вызов — no-op.

    `suggest` — передача из режима подсказки (проверка 24.09): подсказка ничего
    не решает за человека, поэтому статус и место в очереди не трогаются. Было —
    `set_status('new')` и `enter_queue`: ничейный диалог, ждавший 20 минут,
    уходил в конец очереди с «0 мин», а закрытый человеком — снова в «Новые».

    Порядок ровно как в 02 §4: снимаем бота, возвращаем диалог в общую очередь
    (`status='new'`, `assignee_id=NULL` — MVP «кто взял, тот и ведёт»), пишем
    сводку-заметку и системную запись, вешаем теги, журналируем `bot.handoff`
    и кладём в outbox `conversation:updated`.

    «Общая очередь» с 7.1 — это вкладка «Входящие», а не только статус
    «Новый»: `enter_queue` ставит новое время ожидания и обнуляет прошлые
    отказы. Без него диалог, который бот отдал человеку, лежал бы в «Новых»
    без `offered_at` — то есть не показывался бы ни в чьей очереди, и кнопку
    «Принять» на нём никто бы не нажал. Время ожидания считается от передачи,
    а не от первого сообщения клиента: до этой секунды с клиентом РАЗГОВАРИВАЛИ,
    и час бота — не час молчания.
    """
    if state.handoff_done():
        return False
    if reason not in HANDOFF_REASONS:  # опечатка не должна ломать передачу
        log.warning("bot.unknown_handoff_reason", reason=reason)

    moment = now or utcnow()
    step = state.step

    conv.bot_active = False
    unowned = conv.assignee_id is None and conv.claimed_by_id is None
    # Ушёл ли диалог в общую очередь. Запоминаем РЕШЕНИЕ ветки, а не пересчитываем
    # его ниже: кадры о передаче обязаны описывать то, что случилось на самом деле.
    to_queue = unowned and not suggest
    # Подсказка место в очереди не трогает, но диалог, который бот держал
    # скрытым (`bot_active`), снова виден там с прежним временем ожидания.
    in_queue = to_queue or (unowned and conv.offered_at is not None and conv.status != "closed")
    if to_queue:
        # Бот отдаёт диалог людям — он снова ничей и снова новый. Отложку снимаем
        # тем же движением: диалог уходит в общую очередь, и срок возврата,
        # поставленный кем-то до бота, разбудил бы его там второй раз.
        status_dict.clear_snooze(conv)
        status_dict.set_status(conv, "new", now=moment)
        conv.assignee_id = None
        inbox.enter_queue(conv, now=moment)  # 7.1: диалог ждёт человека — с этой секунды
    # диалог уже У ЧЕЛОВЕКА (взял по ссылке, пока бот работал) — бот просто
    # отходит: стирать владельца значило бы отобрать диалог у взявшего (аудит 16.08)
    state.set_handoff(reason=reason, step=step, now=moment)
    state.stop_waiting()
    state.next_step = None  # остановить run()

    added_tags = add_conversation_tags(conv, [*tags, *reason_tags(reason)])

    note = add_note(
        db,
        conv,
        handoff_summary(reason, comment, state.vars, entities, step=step),
        now=moment,
    )
    system = add_system_message(db, conv, handoff_system_text(reason), now=moment)
    conv.updated_at = moment

    await write_audit(
        db,
        user_id=None,  # действие бота, а не человека (06 §0.3)
        action="bot.handoff",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "reason": reason,
            "step": step,
            "bot_id": str(bot_id) if bot_id else state.bot_id,
        },
    )

    # ⚠ ДВА ВИДА УВЕДОМЛЕНИЙ, КОТОРЫЕ ОБЕЩАЛИ И НЕ СДЕЛАЛИ (аудит 19.08,
    # находка L-012). Каталог центра объявлял «AI временно недоступен» и
    # «Клиент недоволен», а создавать их было некому: оба живут ровно здесь,
    # в передаче диалога человеку, и больше нигде взяться не могут.
    #
    # Кладём в ТУ ЖЕ транзакцию (`notify`, а не `notify_now`): передача и
    # уведомление о ней обязаны попасть в базу вместе, иначе однажды человек
    # получит тревогу о диалоге, которого нет.
    # ⚠ ИТОГ `notify` УЕЗЖАЕТ В OUTBOX, А НЕ В МУСОР. Запись ложится в ту же
    # транзакцию, что и передача, а кадр в браузер публикует `flush_outbox`
    # строго после commit'а. Раньше итог выбрасывался: строка в базе была, а
    # тоста и обновления колокольчика не было ни у кого — список уведомлений
    # запрашивается без `refetchInterval`, и `refetchOnWindowFocus` выключен.
    if reason == "ai_unavailable":
        outbox.notification(
            await notifications.notify(
                db,
                kind="ai.unavailable",
                title="AI временно недоступен",
                body=(
                    "Бот не смог получить ответ модели и отдал диалог людям. "
                    "Клиенты не теряются, но бот сейчас не отвечает: проверьте ключ "
                    "доступа к модели, баланс и связь между серверами."
                ),
            )
        )
    if reason == "negative":
        await notify_negative(db, conv, outbox)

    # Строка очереди для кадра `inbox:new` — пока транзакция открыта: после
    # commit'а за клиентом и аккаунтом пришлось бы идти в базу второй раз.
    # ⚠ КАДР СО СПИСКОМ ДОПУЩЕННЫХ (аудит 19.08): бот отдаёт диалог людям, и
    # строка обязана появиться только у операторов ЭТОГО канала — иначе на
    # девяти аккаунтах чужой клиент звенит у всех тринадцати.
    queue_row = await inbox.inbox_frame_addressed(db, conv, now=moment) if in_queue else None

    # События `conversation:handoff` в каталоге WS нет (01 §11.3): менеджеру
    # звук и ⚑ даёт `message:new` системной записи, а карточку в списке
    # обновляет `conversation:updated` с патчем.
    outbox.event("message:new", message_new_event(conv, note))
    outbox.event("message:new", message_new_event(conv, system))
    # ⚠ ПАТЧ СОБИРАЕТСЯ ИЗ ДИАЛОГА, А НЕ ИЗ ЛИТЕРАЛОВ.
    #
    # Здесь стояли `"status": "new"`, `"assignee": None`, `"in_inbox": True`
    # безусловно — то есть кадр описывал ветку «диалог был ничей», даже когда
    # отработала вторая: диалог УЖЕ у человека, и ветка выше его владельца
    # намеренно не трогает. Экран верил кадру буквально: у ведущего диспетчера
    # его собственный открытый диалог превращался в ничей «Новый» с кнопкой
    # «Принять» вместо поля ввода, а у остальных во «Входящих» появлялась строка
    # на диалог, которого в очереди нет.
    #
    # `assignee` в патч не кладём ВОВСЕ: передача бота владельца не меняет, а
    # ключа нет — значит фронт оставит прежнего (`mergeConversationPatch`
    # трогает поле только при `!== undefined`). Это дешевле и честнее, чем идти
    # в базу за именем ради поля, которое не менялось.
    патч = {
        "status": conv.status,
        "bot_active": False,
        "tags": list(conv.tags or []),
        # 7.1: строка списка обязана узнать, что диалог теперь ждёт принятия —
        # иначе у тех, кто очередь не смотрит, он останется обычным «Новым» с
        # полем ввода вместо кнопки «Принять».
        "in_inbox": in_queue,
        "offered_at": iso(conv.offered_at),
    }
    if to_queue:
        патч["assignee"] = None
    outbox.event("conversation:updated", conversation_updated_event(conv, патч))
    # Строка очереди — целиком: у оператора, подключившегося минуту назад, этого
    # диалога в списке нет вовсе, вставлять нечего. Кадр не «дубль» даже для
    # тех, у кого строка уже была: время ожидания и список отказов только что
    # обнулились, и очередь обязана пересортироваться. Собираем до commit'а,
    # публикует outbox после (08 §8.1).
    # ⚠ ТОЛЬКО ЕСЛИ ДИАЛОГ ДЕЙСТВИТЕЛЬНО ВСТАЛ В ОЧЕРЕДЬ. Раньше кадр уходил
    # безусловно — и на диалоге, оставшемся за человеком, рисовал во «Входящих»
    # строку, которой там нет: `enter_queue` в этой ветке не вызывался, а время
    # ожидания в строке считалось от пустого `offered_at`.
    if queue_row is not None:
        outbox.event(
            INBOX_NEW,
            {
                "conversation_id": str(conv.id),
                "conversation": queue_row["conversation"],
                INBOX_ELIGIBLE_KEY: queue_row.get("eligible") or [],
            },
        )
    outbox.note("handoff", reason=reason, step=step, comment=comment, tags=added_tags)
    log.info(
        "bot.handoff",
        conversation_id=str(conv.id),
        reason=reason,
        step=step,
        bot_id=str(bot_id) if bot_id else state.bot_id,
    )
    return True
