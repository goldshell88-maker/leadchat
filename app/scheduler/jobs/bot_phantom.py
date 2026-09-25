"""Счёт есть, реплики нет: инвариант ``bot_msgs_row`` (замер боя 08.09).

ПРАВИЛО, КОТОРОЕ ЗДЕСЬ ОХРАНЯЕТСЯ. ``bot_vars.counters.bot_msgs_row > 0``
означает «бот сказал в этом диалоге хотя бы одну реплику». Реплика бота — это
строка ``messages`` с ``sender_type='bot'``: так пишутся ОБА вида, и обычное
исходящее, и заметка-подсказка (``bots/handoff.add_bot_message`` и
``add_note``). Других способов у движка нет.

ЧТО БЫЛО. На бою 299 диалогов с ненулевым счётчиком, и у 127 из них в переписке
нет ни одной реплики бота — при живых входящих (707) и ответах операторов (653).
Все 127 укладываются в окно 19–23 августа по ``bot_vars.last_step_at`` (первый
06:28 19.08, последний 11:28 23.08), после 23.08 — ни одного, хотя бот работал
25, 26, 29, 30 августа и 1 сентября. Беда закрылась сама, вероятнее всего
выкаткой 23.08 (трогала ``bots/state.py``, ``jobs/bot_stuck.py``,
``workers/inbound.py``); точная строка не найдена.

ПОЧЕМУ ЭТО НАДО СЛЫШАТЬ. ``bot_msgs_row`` — предохранитель «не заваливать
клиента» (``limits.max_bot_messages_row``): фантомный счёт закрывает боту рот за
реплики, которых он не говорил. Клиент при этом молчания не понимает, а в
интерфейсе всё выглядит исправным — потеря невидима, пока кто-нибудь не сравнит
счётчик с перепиской вручную. Ровно это и делает проверка, но каждые полчаса.

ПОЧЕМУ ПРОВЕРКА, А НЕ ПОЧИНКА. Причина фантома неизвестна и, судя по окну, уже
устранена. Сторож, который молча правит счётчики, спрятал бы возврат беды —
а он и есть новость. Данные не трогаем вовсе (см. `find_phantoms`: только
SELECT), решение по историческим 127 принимает владелец.

ПОРОГ СВЕЖЕСТИ — :data:`FRESH_WINDOW`, шесть часов по ``last_step_at``.

* ИСТОРИЧЕСКИЕ 127 ЗВЕНЕТЬ НЕ ДОЛЖНЫ: они закрыты, ``bot_active`` снят у всех.
  Отсекать их «по факту закрытости» нельзя — возврат беды выглядел бы так же.
  Отсекаем по времени последнего шага, и ТОЛЬКО по нему: шесть часов против
  шестнадцати суток до края окна 23.08 — запас в 64 раза.
* ⚠ И ИМЕННО ``last_step_at``, А НЕ ``updated_at``. У тех же 127 диалогов
  ``updated_at`` живёт своей жизнью: один тронут за последние сутки, два за
  неделю, самый свежий — 07.09 (человек открыл, переназначил, закрыл). Проверка
  по ``updated_at`` подняла бы тревогу о беде месячной давности и обвинила бы
  сегодняшний код.
* Окно вдвенадцатеро длиннее такта (:data:`INTERVAL_MINUTES`): пропущенный
  прогон, перезапуск планировщика и даже шестичасовой простой не уносят находку
  из виду. Шире — начнёт цеплять хвост уже показанного; ýже — находка успеет
  выпасть между прогонами.

ЦЕНА ПРОВЕРКИ (замер на бою 08.09, 50 841 диалог, EXPLAIN ANALYZE этого самого
запроса). Один запрос на прогон: индексное чтение ``ix_conversations_updated_at``
поднимает 215 строк за шесть часов, условие по счётчику не оставляет из них ни
одной — до ``messages`` дело не доходит вовсе, 253 буфера и все из кэша. 1,6 мс
исполнения; ~5 мс на прогон вместе с разбором и планированием (десять прогонов
подряд), из них первый холодный план 40 мс — 28 партиций ``messages``. При такте
в полчаса это четверть секунды в сутки.

Полного обхода 50 841 диалога здесь нет намеренно: соседняя сверка, которая
гоняет 151 тыс. проверок и находит ноль, — пример того, чего эта проверка себе
не позволяет.

ЧТО ЗАПРОС ВООБЩЕ УМЕЕТ НАХОДИТЬ (положительная проверка на бою, тот же день):
он же с окном в 30 суток вместо шести часов возвращает ровно 127 — те самые
исторические диалоги. То есть тишина в шестичасовом окне — это тишина, а не
неработающий запрос.

ПОЧЕМУ ``updated_at`` ВСЁ-ТАКИ В ЗАПРОСЕ. Он и есть та дешёвая индексная
отсечка, ради которой всё это влезает в миллисекунду: строка не может измениться
раньше, чем в неё записали шаг (``onupdate=now()`` на любое изменение), поэтому
``updated_at >= порог`` не теряет ни одной свежей находки, а лишнее отбрасывает
условие по ``last_step_at`` — оно здесь главное.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.state import BotState, parse_iso, utcnow_iso
from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import Conversation, Message
from app.services import notifications as notify_svc
from app.services.audit import MSK  # все человеческие времена в проекте — по Москве

log = structlog.get_logger("app.bot_phantom")

#: Вид события в каталоге центра уведомлений (14 §2.1).
KIND = "bot.phantom_reply"

#: Ключ склейки — по виду: беда системная, а не про конкретный диалог. Повторы
#: складывает центр (14 §4), как у сторожевых проверок watchdog.
DEDUP_KEY = KIND

#: Свежесть находки по ``bot_vars.last_step_at``. Разбор — в шапке модуля.
FRESH_WINDOW = timedelta(hours=6)

#: Такт проверки. Полчаса, а не пять минут: чинить тут нечего, тревога
#: одинаково полезна и через полчаса, а окно в шесть часов даёт при этом не
#: больше двенадцати повторов на одну беду.
INTERVAL_MINUTES = 30

#: Сколько находок забираем за прогон. Тревога не поимённая — для текста хватает
#: количества и самого свежего шага, а сотня строк в память не нужна.
BATCH = 50

JOB_ID = "bot_phantom"


def _fresh_since(now: datetime) -> datetime:
    return now - FRESH_WINDOW


async def find_phantoms(
    db: AsyncSession, *, now: datetime | None = None
) -> list[tuple[uuid.UUID, datetime]]:
    """Диалоги со свежим фантомным счётом: ``(id, момент последнего шага)``.

    Только чтение: правка боевых счётчиков — решение владельца, а не сторожа.

    Свежесть проверяется ДВАЖДЫ и намеренно. В запросе — сравнением строк
    (``last_step_at`` пишется одним форматом, ``utcnow_iso``: секунды и всегда
    ``+00:00``), чтобы порция ``LIMIT`` не забилась старьём и свежая находка не
    осталась за её краем. В Python — разбором даты, и это решение окончательное:
    строку в ``bot_vars`` пишем не только мы, а сравнение строк на чужом формате
    ошибается молча.
    """
    moment = now or datetime.now(UTC)
    cutoff = _fresh_since(moment)
    counter = Conversation.bot_vars["counters"]["bot_msgs_row"].as_string()
    last_step = Conversation.bot_vars["last_step_at"].as_string()

    сказал_хоть_раз = (
        sa.select(sa.literal(1))
        .select_from(Message)
        .where(Message.conversation_id == Conversation.id, Message.sender_type == "bot")
        .exists()
    )
    rows = (
        await db.execute(
            sa.select(Conversation.id, Conversation.bot_vars)
            .where(
                Conversation.updated_at >= cutoff,
                counter.is_not(None),
                counter != "0",
                last_step >= utcnow_iso(cutoff),
                ~сказал_хоть_раз,
            )
            .limit(BATCH)
        )
    ).all()

    found: list[tuple[uuid.UUID, datetime]] = []
    for conv_id, bot_vars in rows:
        state = BotState.from_dict(bot_vars)
        step_at = parse_iso(state.last_step_at)
        # ⚠ Счётчик читаем ТЕМ ЖЕ разбором, что и движок: `Counters.from_dict`
        # считает нулём и мусор, и отрицательное. Условие в запросе — только
        # отсечка, здесь — решение.
        if state.counters.bot_msgs_row <= 0 or step_at is None or step_at < cutoff:
            continue
        found.append((conv_id, step_at))
    return found


async def check_phantom_counters() -> int:
    """Точка входа планировщика: своя сессия, свой Redis (стиль bot_stuck)."""
    redis = redis_mod.get_client()
    moment = datetime.now(UTC)
    async with db_mod.session_scope() as db:
        found = await find_phantoms(db, now=moment)
        if not found:
            return 0
        newest_id, newest_at = max(found, key=lambda pair: pair[1])
        result = await notify_svc.notify(
            db,
            kind=KIND,
            body=(
                f"Диалогов со счётом без реплики: {len(found)}; "
                f"последний шаг в {newest_at.astimezone(MSK):%H:%M %d.%m}. "
                "Движок считает, что бот ответил, а в переписке реплики нет — "
                "предохранитель «не заваливать клиента» закрывает боту рот за "
                "несказанное. Откройте диалог и сверьте ленту со счётчиком."
            ),
            entity_type="conversation",
            entity_id=str(newest_id),
            dedup_key=DEDUP_KEY,
            now=moment,
        )
        await db.commit()

    await notify_svc.deliver(redis, result)
    log.warning("bot_phantom.found", conversations=len(found), newest_step_at=newest_at.isoformat())
    return len(found)


def register(scheduler: Any) -> None:
    """Раз в полчаса: запрос стоит миллисекунду, а находка означает возврат беды,
    из-за которой бот молчит там, где считает, что говорил."""
    scheduler.add_job(
        check_phantom_counters,
        IntervalTrigger(minutes=INTERVAL_MINUTES),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
