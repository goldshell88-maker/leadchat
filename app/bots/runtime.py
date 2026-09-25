"""Рантайм бота: ARQ-задачи, вход в диалог и глушение оператором (02 §2.2–2.6).

Здесь всё, что связывает чистый :class:`~app.bots.engine.ScenarioEngine` с
инфраструктурой:

* :func:`bot_entry_block` / :func:`should_run_bot` — пускать ли бота в диалог
  (02 §2.2). Вызывается воркером входящих после записи сообщения клиента; той
  же функцией закрыт вход в песочнице, чтобы отказы совпадали с боевыми.
* :func:`bot_step` — тик движка: пер-диалоговый лок в Redis, `SELECT … FOR
  UPDATE` на диалоге, перепроверка `muted`/вмешательства оператора, исполнение
  и атомарная фиксация `bot_vars` вместе с сообщениями.
* :func:`bot_ask_timeout` — отложенная задача дедлайна `ask`/`menu`,
  самоаннулирующаяся по токену (02 §2.4).
* :func:`mute_bot` — «оператор написал → бот замолкает НАВСЕГДА» (решение
  владельца №3, 02 §2.6).
* :func:`flush_outbox` — публикация событий и постановка задач строго ПОСЛЕ
  commit'а (08 §8.1).

Порядок в тике жёсткий и повторяет 08 §8.1: сначала транзакция (БД), потом
события и очереди. Обратный порядок не чинится ничем — событие о
незакоммиченных данных даёт 404 у фронта, а джоба доставки отправит то, чего
ещё нет.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from arq import Retry
from arq.connections import ArqRedis
from redis.asyncio import Redis
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import handoff as handoff_mod
from app.bots import leadbot
from app.bots.engine import AiAnswer, AiRequest, ScenarioEngine, classify_texts
from app.bots.handoff import conversation_updated_event
from app.bots.schedule import is_bot_scheduled_now
from app.bots.state import BotState, Outbox, parse_iso, utcnow
from app.core import trace
from app.core.observability import with_job_scope
from app.models import AvitoAccount, Bot, Conversation, Message, User
from app.services import messages as messages_service
from app.services import notifications
from app.services.audit import write_audit
from app.services.messages import as_arq
from app.ws.events import publish_event

log = structlog.get_logger("app.bots.runtime")

BOT_STEP_JOB = "bot_step"
BOT_TIMEOUT_JOB = "bot_ask_timeout"

LOCK_KEY = "lock:bot:{conversation_id}"
LOCK_TTL_SECONDS = 30
RETRY_DELAY = timedelta(seconds=1)
#: Сколько раз повторять сорвавшийся дедлайн бота, прежде чем признать
#: потерю. Три попытки с нарастающей паузой перекрывают обычную заминку
#: базы; дальше держать задачу бессмысленно — пусть отказ будет виден.
ЛИМИТ_ПОВТОРОВ_ДЕДЛАЙНА = 3

#: ⚠ ПОДСКАЗКА ВСЛЕПУЮ ХУЖЕ ПОДСКАЗКИ С ЗАДЕРЖКОЙ (боевой замер 19.08).
#: Пока идёт подгрузка истории Авито, диалог в базе состоит из одной свежей
#: реплики: остальные 10-19 сообщений приедут через минуты. Бот, вызванный в это
#: окно, отвечает вслепую — здоровается на девятнадцатом сообщении и переспрашивает
#: адрес, который клиент уже назвал. По журналу это каждый седьмой вызов
#: (21 из 148 за день), и владелец видит ровно это как «качество хромает».
#: Поэтому тик откладывается, пока история едет.
BACKFILL_WAIT = timedelta(seconds=45)
#: ⚠ НО НЕ БЕСКОНЕЧНО. Подгрузка идёт часами, а требование владельца — «подсказки
#: бот может давать всегда». Ждём не дольше десяти минут, дальше отвечаем тем
#: контекстом, что есть: поздняя подсказка лучше, чем никакой.
BACKFILL_MAX_WAITS = 13
BACKFILL_WAIT_KEY = "bot:backfill_wait:{conversation_id}"
# Отложенная задача сработала раньше дедлайна (перезапуск воркера, перекос
# часов): не режем ожидание, а переставляем себя на остаток.
MIN_REDEFER_SECONDS = 5
#: Серия, на которую отвечает тик: сообщения клиента после последней реплики
#: бота или оператора, не больше стольких и не старше окна от последнего. Окно
#: с запасом на ожидание подгрузки истории (до ~10 минут), но отсекает старую
#: переписку свежего диалога: «позовите менеджера» полугодовой давности не повод
#: передавать новое обращение.
SERIES_MAX_MESSAGES = 10
SERIES_LOOKBACK = timedelta(minutes=15)


# ------------------------------------------------------------------ выборки


async def get_bot_for_conversation(
    db: AsyncSession, conv: Conversation, *, account: AvitoAccount | None = None
) -> Bot | None:
    """Бот диалога по `avito_accounts.bot_id` (привязка живёт на аккаунте)."""
    if account is not None:
        if account.bot_id is None:
            return None
        return await db.get(Bot, account.bot_id)
    return (
        await db.execute(
            select(Bot)
            .join(AvitoAccount, AvitoAccount.bot_id == Bot.id)
            .where(AvitoAccount.id == conv.account_id)
        )
    ).scalar_one_or_none()


async def has_operator_messages(db: AsyncSession, conv: Conversation) -> bool:
    """«Менеджер ещё не отвечал» из триггера DESIGN §4.2.

    Заметки (`direction='note'`) не считаются: внутренний комментарий не
    отбирает диалог у бота (02 §2.6).
    """
    return bool(
        (
            await db.execute(
                select(
                    exists().where(
                        Message.conversation_id == conv.id,
                        Message.sender_type == "operator",
                        Message.direction == "out",
                    )
                )
            )
        ).scalar()
    )


async def _conversation_for_update(
    db: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    """Строка диалога под `FOR UPDATE` (08 §8.4). В воркере отсутствие строки —
    не 404, а штатный no-op."""
    return (
        await db.execute(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
    ).scalar_one_or_none()


# ------------------------------------------------------------ вход в диалог


#: Причины, по которым бот НЕ входит в диалог, человеческими словами.
#: Словарь ОДИН: песочница отдаёт эту подпись редактору готовой (как каталог
#: уведомлений, 14 §3), а экран лид-бота «Почему бот молчит» берёт её отсюда
#: же — чтобы объяснение не расходилось между сервером и фронтом. Ключи
#: возвращает :func:`bot_entry_block`; ключ без подписи экран показал бы
#: машинным кодом (так было с `claimed_by_human`, проверка 24.09).
ENTRY_BLOCKS: dict[str, str] = {
    "no_conversation": "диалога нет",
    "claimed_by_human": "диалог принял человек — бот, отвечающий клиенту, не входит",
    "muted": "человек вмешивался — бот в этом диалоге молчит навсегда",
    "handoff_done": "бот уже отдал диалог человеку и второй раз не входит",
    "not_new": "диалог не новый: его уже ведёт человек",
    "no_bot": "к каналу не привязан бот",
    "bot_disabled": "бот выключен",
    "operator_replied": "в диалоге уже отвечал человек",
    "not_scheduled": "бот вне расписания",
}


async def bot_entry_block(
    db: AsyncSession,
    conv: Conversation,
    *,
    account: AvitoAccount | None = None,
    bot: Bot | None = None,
    now: datetime | None = None,
) -> str | None:
    """Почему бот НЕ входит в диалог; `None` — входит (02 §2.2).

    Расписание проверяется только на **старте**: уже начатый диалог
    (`bot_active=true`) бот доводит до конца и вне расписания — иначе клиент,
    ответивший в 10:01 на ночной вопрос, останется без реакции.

    Причина, а не просто «нет», нужна песочнице: она обязана отказывать ровно
    там же, где прод, И называть админу причину. Пока проверка была булевой,
    песочница имела собственную (только расписание, только на первом
    сообщении) — и показывала поведение, которого в проде не бывает.
    """
    if conv is None:
        return "no_conversation"
    state = BotState.from_conv(conv)
    # ⚠ ДИАЛОГ ПРИНЯТ ЧЕЛОВЕКОМ — АВТО-БОТ НЕ ВХОДИТ И НЕ ПРОДОЛЖАЕТ (боевой
    # дефект, слово владельца 30.08: «бот иногда входит в диалоги, которые приняли
    # операторы, но не успели ответить»). Дыра была двойная: проверки ниже смотрели
    # статус и «оператор ОТВЕТИЛ», а само ПРИНЯТИЕ (claim/assign) не проверял
    # никто — и ранний выход `bot_active` пускал бота продолжать сценарий даже в
    # принятом диалоге. Принял человек — ведёт человек. Режим ПОДСКАЗОК под заслон
    # не попадает: подсказка — заметка сотруднику, в принятом диалоге она и нужна
    # (лестница запретов подсказок ниже, решение 21.08). Проверка режима — лениво:
    # бот здесь ещё не загружен, а тянуть его из базы на каждый вход дорого.
    _принят = (
        getattr(conv, "assignee_id", None) is not None
        or getattr(conv, "claimed_by_id", None) is not None
    )
    if _принят:
        _b = bot if bot is not None else await get_bot_for_conversation(db, conv, account=account)
        if _b is None or str(getattr(_b, "mode", "") or "") != "suggest":
            return "claimed_by_human"
        bot = _b
    if conv.bot_active and not _принят:
        return None  # бот ждёт ответа (ask/menu) — продолжаем сценарий

    # ⚠ РЕЖИМ ПОДСКАЗОК — ОТДЕЛЬНАЯ ЛЕСТНИЦА ЗАПРЕТОВ (решение владельца 21.08).
    #
    # Подсказка это ЗАМЕТКА: её видит только сотрудник, клиенту не уходит ничего.
    # Значит запреты, которые берегут клиента от бота (оператор уже ответил, диалог
    # не новый, менеджер вмешался, диалог передан человеку), к подсказке отношения
    # не имеют — они лишь лишают диспетчера помощи там, где она нужнее всего:
    # в середине живого разговора. Замер 20.08: из 445 диалогов с подсказками бот
    # замолкал после первой же реплики оператора, дальше человек работал один.
    #
    # Остаются в силе и здесь: выключенный бот, канал без бота, отсутствие диалога
    # и РАСПИСАНИЕ — каждый ход это платный вызов шлюза, окно работы решает владелец.
    #
    # В АВТО-режиме не меняется ничего: там говорит клиенту сам бот, и заговорить
    # поверх оператора он не должен ни при каких условиях.
    if bot is None:
        bot = await get_bot_for_conversation(db, conv, account=account)
    if bot is None:
        return "no_bot"  # к аккаунту не привязан бот
    if not bot.is_enabled:
        return "bot_disabled"
    подсказки = str(getattr(bot, "mode", "") or "") == "suggest"
    if подсказки:
        if not is_bot_scheduled_now(bot, now):
            return "not_scheduled"
        return None

    if state.muted:
        return "muted"  # менеджер вмешивался — бот молчит навсегда (условие №6)
    if state.handoff is not None:
        return "handoff_done"  # бот уже отдал диалог человеку — второй раз не входим
    # ТОЛЬКО СВЕЖИЙ ДИАЛОГ, и правило перечитано осознанно вместе с docs/38.
    #
    # С появлением «Ждёт клиента» и «Отложен» условие не изменилось ни на
    # букву, и это верно: клиент, написавший в отложенный человеком диалог,
    # должен получить человека, а не бота, который начнёт знакомиться заново.
    # Написано отрицанием от `new`, а не перечислением остальных, ровно затем,
    # чтобы следующее значение статуса попало сюда само и правильно.
    if conv.status != "new":
        return "not_new"
    if await has_operator_messages(db, conv):
        return "operator_replied"
    if not is_bot_scheduled_now(bot, now):
        return "not_scheduled"
    return None


async def should_run_bot(
    db: AsyncSession,
    conv: Conversation,
    *,
    account: AvitoAccount | None = None,
    bot: Bot | None = None,
    now: datetime | None = None,
) -> bool:
    """Пускать ли бота в этот диалог (02 §2.2) — фасад над `bot_entry_block`."""
    return await bot_entry_block(db, conv, account=account, bot=bot, now=now) is None


#: ⚠ СЕРИЯ СООБЩЕНИЙ КЛИЕНТА — ОДИН ТИК (21.08). В живой выгрузке подсказок клиент
#: пишет 2+ сообщения подряд в 370 случаях на 445 диалогов. Тик на каждое означал:
#: бот отвечает на половину мысли (вторая половина приходит, когда ответ уже сочинён,
#: и главное обычно именно в ней), один разговор оплачивается моделью столько раз,
#: сколько клиент нажал Enter, а диспетчер получает столько же подсказок подряд.
#:
#: ⚠ ОКНО ПОДНЯТО ДО 90 СЕКУНД 26.08, И ЭТО ЗАМЕР. Было 15 в подсказке и 35 в авто —
#: числа взяты «на глаз», и владелец прислал снимок: клиент пишет в 03:10 и в 03:11, бот
#: отвечает дважды и почти одним текстом. Замер по боевой базе (19 077 пар подряд идущих
#: реплик клиента): пауза внутри серии p50 = 13 с, p75 = 32 с, p90 = 88 с, p95 = 214 с.
#: Сколько продолжений серии ловит окно:
#:      15 с → 56 %      35 с → 77 %      60 с → 86 %
#:      90 с → 90 %     120 с → 92 %     180 с → 94 %
#: После 90 секунд отдача падает: каждые следующие полминуты приносят 1–2 пункта.
#: Серия вообще не редкость — 34 % реплик клиента приходят пачкой из двух и больше.
#:
#: ⚠ ЦЕНА ОЖИДАНИЯ РАЗНАЯ, А ЧИСЛО ОДНО. В подсказке ждёт оператор — но он ждёт ОДНУ
#: внятную подсказку вместо трёх спорящих, и диалог всё равно лежит в очереди. В авто
#: ждёт клиент — и там пауза даже полезна: мгновенный ответ выдаёт автомат сам по себе
#: (ДИАЛОГ-И-ПАУЗЫ, правила 51 и 63). Разводить два числа не за что.
#: ⚠ 26.08, ВТОРОЙ ПЕРЕСМОТР ЗА ДЕНЬ — И ТЕПЕРЬ ОКНО СЛУЖИТ ДВУМ ДЕЛАМ СРАЗУ.
#: Владелец: «бот слишком быстро пишет ответы, ещё замедли его». Первое дело у окна
#: прежнее — дождаться конца серии (90 секунд ловят 90 % продолжений). Второе новое:
#: пауза должна быть ЧЕЛОВЕЧЕСКОЙ. Мгновенный ответ выдаёт автомат вернее любой
#: формулировки.
#:
#: ЗАМЕР ПО БОЕВОЙ БАЗЕ, 13 464 пары «реплика клиента → ответ оператора» за 30 дней:
#:      p10 = 17 с   p25 = 45 с   p50 = 205 с   p75 = 1014 с   p90 = 2507 с
#: Живой мастер думает 3,4 минуты. Бот на 90 секундах отвечал быстрее большинства людей.
#:
#: ⚠ ЦЕНА ПРОВЕРЕНА, А НЕ ПРЕДПОЛОЖЕНА. Доля диалогов, где клиент оставил телефон,
#: по скорости первого ответа (60 дней):
#:      быстрее минуты 30,7 %   ·   1-3 мин 23,9 %   ·   3-10 мин 24,1 %
#:      10-60 мин      21,4 %   ·   больше часа 22,5 %
#: Между «1-3» и «3-10» разницы нет: переезд с 90 секунд на три минуты не стоит заявок.
#: (Оговорка честная: у самой быстрой корзины преимущество есть, но туда бот попасть не
#: может — окно серии короче минуты не бывает, иначе он снова ответит на половину мысли.)
#:
#: ⚠⚠ И ГЛАВНОЕ, ЧТО ВЫЯСНИЛОСЬ ТЕМ ЖЕ ВЕЧЕРОМ: ЭТО ЧИСЛО В БОЮ НЕ РАБОТАЛО НИКОГДА.
#: Окно жило здесь, а боевой путь (вебхук Авито, `services.inbound.apply_inbound_event`)
#: звал `enqueue_bot_step` НАПРЯМУЮ — без окна и без отсрочки. Обёртку с окном
#: (`maybe_enqueue_bot_step`) дёргали только проверки. Отсюда и «бот секунда в секунду
#: отвечает», и снимок с двумя почти одинаковыми подсказками подряд: гасить серию было
#: нечем. Правки числа 15 → 35 → 90 не меняли в бою ровно ничего.
#: Теперь боевой путь подключён, и правило «нигде без отсрочки» держит отдельная проверка
#: (tests/unit/test_debounce_wired_in_prod.py), обходящая ВСЕ вызовы в app/.
#:
#: ⚠ ЧИСЛО ВЫБРАЛ ВЛАДЕЛЕЦ, ЗНАЯ ЦЕНУ. Он просил 30 секунд, увидел замер и выбрал 90:
#: это минимум, при котором бот не отвечает дважды на одну мысль (30 секунд ловят 77 %
#: продолжений серии, 90 — 90 %). Человеческая медиана в 205 секунд осталась в стороне
#: сознательно: претензия и срыв визита не должны ждать три минуты до передачи человеку.
#:
#: ⚠ РАЗБРОС ОБЯЗАТЕЛЕН. Фиксированное окно значит, что ответ приходит РОВНО через N
#: секунд после клиента, каждый раз. Это метроном, и по нему бота видно так же хорошо,
#: как по мгновенному ответу. Сдвиг считается от номера диалога: внутри диалога он
#: постоянный (иначе TTL ключа и отложенная задача разъедутся), между диалогами — разный.
# Маркер сообщения без текста. Живёт ЗДЕСЬ, а не в `inbound`: по нему считается форма
# сообщения (фото — самая частая и самая быстрая серия), и разъехавшись с отправителем
# он молча вернул бы фото в ветку «без знака» — вдвое более долгое окно.
МАРКЕР_ВЛОЖЕНИЯ = "🖼 фото"

# ⚠ ОКНО ГИБРИДНОЕ: 30–90 СЕКУНД, И ВЫБИРАЕТ ЕГО ФОРМА СООБЩЕНИЯ, А НЕ МОНЕТКА.
# Владелец 26.08: «мне не нравится таймаут 90 секунд, сделай его гибридным от 30 до 90».
# Ровные 90 на всё — это метроном: по нему бота видно так же хорошо, как по мгновенному ответу,
# и на фото, после которого клиент почти наверняка допишет через 5 секунд, он ждёт впустую.
#
# ЗАМЕР ПО БОЕВОЙ БАЗЕ (19 806 пар «сообщение клиента → следующее событие», окно 10 минут):
# продолжит ли клиент серию — зависит от того, ЧЕМ он закончил, и очень сильно.
#
#   форма последнего сообщения   пар    продолжат   p50   p90 паузы
#   вложение (фото без текста)   1522     65,9 %      5 с    40 с
#   обрывок (до 3 слов)          5869     43,5 %     13 с    67 с
#   без знака в конце            6807     41,5 %     13 с    67 с
#   кончается точкой             1740     38,7 %     19 с   108 с
#   кончается вопросом           3868     29,5 %     10 с   104 с
#
# ⚠ 27.08 У ЭТОГО ЗАМЕРА НАШЛАСЬ ВТОРАЯ СТОРОНА, И ОНА ДОРОЖЕ ПЕРВОЙ. Выше считалось только
# одно: как поймать 90 % дописок. Поэтому брался p90 формы — отсюда 67 и 90 секунд. Чего этот
# счёт не знал: сколько стоит само ожидание. Замер по 28 159 диалогам с отметками времени
# (архив Jivo — в корпусе Авито отметок нет) считает исход от паузы до ПЕРВОГО ответа мастера:
#
#   пауза      диалогов   клиент вернулся   заявка        пауза       заявка
#   0–10 с          259       67,6 %        19,3 %        ≤30 с       20,1 %  (сообщение-вопрос)
#   10–30 с        1289       68,0 %        18,7 %        30–90 с     15,5 %
#   30–60 с        1352       63,7 %        17,3 %        > 90 с       9,5 %
#   60–90 с         877       62,5 %        14,1 %
#   1–3 часа       3962       28,4 %         4,6 %
#
# Скорость оказалась сильнейшим фактором из всех замеренных: ни один приём в тексте реплики
# не давал больше +10 п.п., а разница между «до минуты» и «через час» — 37 п.п. по возврату
# и вчетверо по заявкам. Мгновенность при этом ничего НЕ добавляет: 0–10 с не лучше 10–30 с.
#
# Медиана дописки — 5–25 секунд по форме, то есть окно в 20–30 с ловит большинство серий, а
# прежние 67 и 90 были чистым ожиданием. Берём p50 формы, удвоенный (запас на медленных), и
# зажимаем в новую полосу. Часть дописок при этом теряется: для «обрывка» ловим около 72 %
# серий вместо 90 %. Это осознанный размен — в потерянных случаях бот ответит на неполное
# сообщение и получит дописку следующим ходом, а сэкономленные секунды стоят заявок.
# ⚠ Полоса 15–30 с — решение владельца от 27.08 по этому замеру (прежняя полоса 30–90 была
# его же решением от 26.08, когда цены ожидания ещё никто не считал).
DEBOUNCE_MIN_S = 15
DEBOUNCE_MAX_S = 30
# ⚠ ДЖИТТЕР ЗДЕСЬ МЕНЬШЕ ПОЛОСЫ НЕ ПРОСТО ТАК. Разброс вычитается из базы, а результат
# зажимается снизу в DEBOUNCE_MIN_S. При широком разбросе в узкой полосе формы схлопываются:
# с джиттером 8 «вложение» (база 15) давало ровно 15 всегда, а «вопрос» (база 20) — от 15,
# и различать формы становилось нечем. Базы разведены так, чтобы полосы форм не пересекались
# даже на краю разброса: вложение 15–17, вопрос 18–22, обрывок 23–27, точка 26–30.
DEBOUNCE_JITTER_S = 4
DEBOUNCE_ПО_ФОРМЕ = {
    "вложение": 17,  # p50 = 5 с, самая быстрая серия
    "обрывок": 27,  # p50 = 13 с
    "без знака": 27,  # p50 = 13 с
    "точка": 30,  # p50 = 19 с, верхняя граница полосы
    "вопрос": 22,  # p50 = 10 с, и клиент уже ждёт ответа
}
# оставлены под именем «потолок»: разницы между режимами замер не показал (в подсказке оператор
# ждёт одну внятную реплику, в авто пауза тем более уместна), но имена импортируют проверки
DEBOUNCE_SUGGEST = timedelta(seconds=DEBOUNCE_MAX_S)
DEBOUNCE_AUTO = timedelta(seconds=DEBOUNCE_MAX_S)
DEBOUNCE_KEY = "bot:debounce:{conversation_id}"


def форма_сообщения(текст: str | None) -> str:
    """Чем клиент закончил — от этого зависит, дописывает он сейчас или ждёт ответа."""
    т = (текст or "").strip()
    if not т or т == МАРКЕР_ВЛОЖЕНИЯ:
        return "вложение"
    if т.endswith("?"):
        return "вопрос"
    if т[-1] in ".!":
        return "точка"
    if len(т.split()) <= 3:
        return "обрывок"
    return "без знака"


def debounce_window(
    bot: Bot | None,
    conversation_id: uuid.UUID | None = None,
    *,
    текст: str | None = None,
) -> timedelta:
    """Сколько ждать конца серии — по форме последнего сообщения клиента.

    ⚠ ОДНА СЕРИЯ ОБЯЗАНА ДАТЬ ОДНО И ТО ЖЕ ЧИСЛО: окно ставится дважды — сроком жизни ключа
    в Redis и отсрочкой задачи, и разойтись им нельзя. Поэтому боевой путь считает его ОДИН
    раз и передаёт обеим сторонам (`debounce_claim(окно=…)` и `defer_by=…`), а не зовёт эту
    функцию дважды: со вторым вызовом достаточно потерять `текст`, чтобы числа разъехались.

    `bot` больше ни на что не влияет — оставлен в подписи, потому что режим бота здесь
    спрашивают все вызывающие, и молчаливая смена подписи сломала бы их незаметно.
    """
    база = DEBOUNCE_ПО_ФОРМЕ[форма_сообщения(текст)]
    if conversation_id is None:
        return timedelta(seconds=база)
    зерно = hashlib.sha256(f"{conversation_id}|{текст or ''}".encode()).hexdigest()
    окно = база - int(зерно, 16) % (DEBOUNCE_JITTER_S + 1)
    return timedelta(seconds=max(DEBOUNCE_MIN_S, min(DEBOUNCE_MAX_S, окно)))


async def debounce_claim(
    redis: Redis,
    conversation_id: uuid.UUID,
    bot: Bot | None,
    *,
    окно: timedelta | None = None,
) -> bool:
    """Завести окно серии. `False` — тик на эту серию уже поставлен.

    Ключ ВСЕГДА с TTL: очередь может лечь, и без срока диалог залип бы навсегда.
    `окно` передаёт боевой путь — тем же числом, каким он отложит задачу.
    """
    окно = окно if окно is not None else debounce_window(bot, conversation_id)
    поставили = await redis.set(
        DEBOUNCE_KEY.format(conversation_id=conversation_id),
        "1",
        nx=True,
        ex=int(окно.total_seconds()),
    )
    return bool(поставили)


async def enqueue_bot_step(
    redis: Redis,
    conversation_id: uuid.UUID,
    incoming_text: str | None,
    *,
    arq: ArqRedis | None = None,
    defer_by: timedelta | None = None,
) -> None:
    """Поставить тик бота. Вызывается воркером входящих ПОСЛЕ commit'а."""
    await _enqueue(
        {"redis": redis, "arq": arq},
        BOT_STEP_JOB,
        conversation_id,
        incoming_text,
        defer_by=defer_by,
    )


async def maybe_enqueue_bot_step(
    db: AsyncSession,
    redis: Redis,
    conv: Conversation,
    *,
    account: AvitoAccount | None = None,
    bot: Bot | None = None,
    text: str | None = None,
    now: datetime | None = None,
    arq: ArqRedis | None = None,
) -> bool:
    """`should_run_bot` + постановка задачи — одна строка для зоны входящих.

    Точка вызова — `app/services/inbound.py` (DESIGN §8.3), СТРОГО после
    commit'а транзакции, записавшей сообщение клиента.
    """
    if not await should_run_bot(db, conv, account=account, bot=bot, now=now):
        return False
    if bot is None:
        bot = await get_bot_for_conversation(db, conv, account=account)
    if not await debounce_claim(redis, conv.id, bot):
        # тик на эту серию уже стоит; он прочитает историю сам и увидит всю серию
        log.info("bot.debounced", conversation_id=str(conv.id))
        return True
    await enqueue_bot_step(redis, conv.id, text, arq=arq, defer_by=debounce_window(bot, conv.id))
    return True


# ------------------------------------------------------------------- mute


async def mute_bot(
    db: AsyncSession,
    conv: Conversation,
    *,
    by_user: User | None = None,
    audit: bool = True,
) -> bool:
    """Оператор написал в диалог — бот замолкает НАВСЕГДА (02 §2.6).

    `muted` живёт в `bot_vars` и не сбрасывается ничем: даже когда клиент
    возвращается в закрытый диалог и статус снова становится `new` (DESIGN
    §8.3), `should_run_bot` вернёт False. Ожидающий `bot_ask_timeout`
    аннулируется тем, что `waiting` (а с ним и токен) обнуляется.

    Возвращает True, если бот в этот момент действительно вёл диалог — только
    тогда пишется `bot.muted`, иначе журнал заполнится событиями о ботах,
    которых в диалоге не было.
    """
    state = BotState.from_conv(conv)
    was_active = bool(conv.bot_active)
    if not was_active and state.muted:
        return False
    state.mute()
    conv.bot_vars = state.dump()
    conv.bot_active = False
    if was_active and audit:
        await write_audit(
            db,
            user_id=by_user.id if by_user is not None else None,
            action="bot.muted",
            entity="conversation",
            entity_id=str(conv.id),
        )
    return was_active


# ------------------------------------------------------------ outbox -> мир


async def _enqueue(
    ctx: dict[str, Any],
    name: str,
    *args: Any,
    defer_by: timedelta | None = None,
    job_id: str | None = None,
) -> bool:
    """ARQ-постановка через пул воркера либо через тот же Redis (как 08 §8.3).

    Упавшая очередь не должна ронять уже закоммиченную транзакцию — логируем.

    ⚠ ВОЗВРАЩАЕТ ПРИЗНАК УСПЕХА, И ЭТО НЕ УКРАШЕНИЕ (разбор 03.09). Раньше
    функция возвращала None: отказ очереди уходил в лог и на этом кончался.
    Для `deliver_message` это значило, что ответ бота остаётся `pending`
    НАВСЕГДА — а `pending` не видит никто: `/retry` берёт только `failed`,
    красную метку диалогу ставит `refresh_undelivered` тоже по `failed`.
    Клиент не получил ответа, оператор об этом не знает, система молчит.

    Ровно эта беда уже разобрана и закрыта на пути ОПЕРАТОРА
    (`services/messages.enqueue_deliver` + `mark_enqueue_failed`); у бота
    закрыта не была. Ответ обрабатывает `flush_outbox` ниже.
    """
    arq = ctx.get("arq") or as_arq(ctx["redis"])
    try:
        await ArqRedis.enqueue_job(arq, name, *args, _defer_by=defer_by, _job_id=job_id)
    except Exception:  # noqa: BLE001 — очередь недоступна, данные уже в БД
        log.exception("bot.enqueue_failed", job=name)
        return False
    return True


async def release_bot_dialogs(
    db: AsyncSession, *, account_ids: set[uuid.UUID], reason: str = "bot_disabled"
) -> Outbox:
    """Отдать людям диалоги, которые бот ведёт на этих каналах (проверка 24.09).

    Зовут выключение бота, отвязка канала, «Выключить лид-бота» и перевод
    бота в подсказки (`reason="bot_to_suggest"`: подсказка диалог не ведёт, и
    диалоги, которые вёл автоответ, иначе остались бы скрытыми). Раньше
    `bot_active` оставался («ретроактивно не гасим», 01 §8.4 — правило написано
    до 16.08, когда `bot_active` стал прятать диалог из очереди): ответ клиента
    упирался в «no_bot», и диалог висел невидимым, пока его не снимал сторож
    зависших, а молчащего клиента не возвращал никто. Транзакцией и кадрами
    владеет вызывающий: кадры — `flush_outbox` после commit'а.
    """
    outbox = Outbox()
    if not account_ids:
        return outbox
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.account_id.in_(account_ids),
                    Conversation.bot_active.is_(True),
                    Conversation.status != "closed",
                )
                # Ждём, а не пропускаем диалог под тиком (проверка 24.09): тик
                # держит строку миллисекунды — модель думает вне транзакции, — а
                # пропущенный он успевал снова поставить `bot_active` уже после
                # переключения режима, и диалог оставался спрятанным.
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return outbox
    # Режим ведшего бота решает, как диалог встаёт в очередь: после подсказки —
    # с прежним временем ожидания (клиенту никто не отвечал), после автоответа —
    # с этой секунды, как любая передача. Ботов единицы — берём режимы всех
    # одним запросом и сверяем по строке `bot_vars.bot_id`, ничего не разбирая.
    modes = {str(bot_id): mode for bot_id, mode in await db.execute(select(Bot.id, Bot.mode))}
    for conv in rows:
        state = BotState.from_conv(conv)
        await handoff_mod.do_handoff(
            db,
            conv,
            state,
            outbox,
            reason=reason,
            bot_id=state.bot_id,
            suggest=modes.get(state.bot_id or "") == "suggest",
        )
        conv.bot_vars = state.dump()
    log.info("bot.dialogs_released", conversations=len(rows), accounts=len(account_ids))
    return outbox


async def flush_outbox(ctx: dict[str, Any], outbox: Outbox) -> None:
    """События в Pub/Sub и задачи в ARQ — строго ПОСЛЕ commit'а (08 §8.1)."""
    redis = ctx["redis"]
    for event in outbox.events:
        try:
            await publish_event(redis, event.type, event.data)
        except Exception:  # noqa: BLE001 — WS best effort, догон даёт ?updated_since=
            log.exception("bot.publish_failed", event=event.type)
    for job in outbox.jobs:
        встала = await _enqueue(ctx, job.name, *job.args, defer_by=job.defer_by, job_id=job.job_id)
        # ⚠ ОТКАЗ ОЧЕРЕДИ У ОТПРАВКИ КЛИЕНТУ ОБЯЗАН СТАТЬ ВИДИМЫМ.
        #
        # Реплика бота уже в базе со статусом `pending`. Не встала задача
        # доставки — и её никто не подберёт: `/retry` берёт только `failed`,
        # красная метка диалога считается по `failed`. Клиент остаётся без
        # ответа молча.
        #
        # Помечаем «не доставлено» своей короткой сессией — основная
        # транзакция тика к этому моменту уже закрыта. Отказ пометки не имеет
        # права уронить рассылку: остальные события кадра обязаны уехать.
        if not встала and job.name == "deliver_message" and job.args:
            factory = ctx.get("db_session_factory")
            if factory is not None:
                try:
                    async with factory() as db:
                        await messages_service.mark_enqueue_failed(db, job.args[0])
                except Exception:  # noqa: BLE001
                    log.exception("bot.mark_enqueue_failed", job=job.name)
    # Уведомления центра — тем же порядком, что и события: запись уже в базе,
    # кадр уходит только сейчас. Отказ Pub/Sub не роняет тик: строка в базе
    # осталась, и колокольчик подберёт её следующим запросом списка.
    for result in outbox.notifications:
        try:
            await notifications.deliver(redis, result)
        except Exception:  # noqa: BLE001 — WS best effort, строка в базе уже есть
            log.exception("bot.notify_publish_failed")
    outbox.clear()


def _engine_kwargs(ctx: dict[str, Any]) -> dict[str, Any]:
    """Инъекции для тестов и песочницы: свой AI-бэкенд и своё «сейчас»."""
    kwargs: dict[str, Any] = {}
    if "bot_ai" in ctx:
        kwargs["ai"] = ctx["bot_ai"]
    if "bot_now" in ctx:
        kwargs["now"] = ctx["bot_now"]
    return kwargs


# --------------------------------------------------------------- ARQ-задачи


@with_job_scope
async def bot_step(
    ctx: dict[str, Any],
    conversation_id: uuid.UUID,
    incoming_text: str | None = None,
    token: str | None = None,
) -> str:
    """Тик движка (02 §2.3). `incoming_text=None` — срабатывание по таймауту.

    `token` (сверх сигнатуры DESIGN §8.3) передаёт `bot_ask_timeout`: между
    его проверкой и этим тиком клиент мог ответить, и «слепой» таймаут срезал
    бы уже новое ожидание.
    """
    redis: Redis = ctx["redis"]
    lock_key = LOCK_KEY.format(conversation_id=conversation_id)
    # Пер-диалоговый лок: клиент может прислать три сообщения за секунду, и
    # consumer group отдаст их разным воркерам — сериализуемся.
    if not await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS):
        await _enqueue(
            ctx, BOT_STEP_JOB, conversation_id, incoming_text, token, defer_by=RETRY_DELAY
        )
        return "locked"
    try:
        result, classify_text = await _tick(ctx, conversation_id, incoming_text, token)
        if result == "backfill":
            # История ещё едет — вернёмся с полным контекстом. Счётчик ожиданий живёт
            # в Redis и сам истекает: диалог, зависший в ожидании, не будет ждать вечно.
            ключ = BACKFILL_WAIT_KEY.format(conversation_id=conversation_id)
            ждали = await redis.incr(ключ)
            await redis.expire(ключ, int(BACKFILL_WAIT.total_seconds()) * (BACKFILL_MAX_WAITS + 2))
            if ждали <= BACKFILL_MAX_WAITS:
                await _enqueue(
                    ctx,
                    BOT_STEP_JOB,
                    conversation_id,
                    incoming_text,
                    token,
                    defer_by=BACKFILL_WAIT,
                )
                return "backfill_wait"
            # ждали достаточно — отвечаем тем, что есть, и помечаем это в журнале
            log.warning(
                "bot.backfill_wait_exhausted", conversation_id=str(conversation_id), waits=ждали
            )
            await redis.delete(ключ)
            # Под тем же пер-диалоговым локом (проверка 24.09): повторный тик шёл
            # уже после его снятия и мог разойтись с параллельным.
            result, classify_text = await _tick(
                ctx, conversation_id, incoming_text, token, ignore_backfill=True
            )
    finally:
        await redis.delete(lock_key)

    if classify_text:
        # Условие №3 (негатив) и второй эшелон условия №1 — своя короткая
        # транзакция, тик к этому моменту закоммичен (02 §3.4).
        await _classify_after_commit(ctx, conversation_id, classify_text)
    return result


async def _идёт_подгрузка(ctx: dict[str, Any], conv: Any) -> bool:
    """Идёт ли подгрузка истории для аккаунта этого диалога.

    Возвращает False при любой беде: не смогли спросить — отвечаем как раньше.
    Задерживать подсказку из-за сбоя проверки хуже, чем ответить с коротким
    контекстом (правило владельца №4: недоступность чего-либо не блокирует работу).
    """
    try:
        from app.services.avito_accounts import get_backfill_state

        account_id = getattr(conv, "account_id", None)
        if account_id is None:
            return False
        state = await get_backfill_state(ctx["redis"], account_id)
        return (state or {}).get("status") == "running"
    except Exception:  # noqa: BLE001
        return False


@dataclass
class TickOutcome:
    """Итог одной транзакции тика."""

    result: str
    outbox: Outbox
    classify_text: str | None = None
    #: Шаг ИИ остановил сценарий до вызова модели (см. `engine.AiRequest`).
    deferred: AiRequest | None = None
    #: Ответ модели отброшен: клиент дописал, пока она думала. Текст этого
    #: последнего сообщения — чтобы тик на него точно стоял.
    stale_text: str | None = None


#: Сколько раз один тик ходит в модель. Сценарий «ИИ → условие → ИИ» без
#: вопроса клиенту — законный, но бесконечным он быть не может.
MAX_AI_CALLS_PER_TICK = 3


async def _tick(
    ctx: dict[str, Any],
    conversation_id: uuid.UUID,
    incoming_text: str | None,
    token: str | None,
    *,
    ignore_backfill: bool = False,
) -> tuple[str, str | None]:
    """Тик целиком: транзакция до шага ИИ, модель без блокировки, продолжение.

    ⚠ МОДЕЛЬ ДУМАЕТ ВНЕ ТРАНЗАКЦИИ (проверка 24.09). Тик держал `SELECT … FOR
    UPDATE` строки диалога всё время похода в модель — до 15 секунд. Вебхук
    нового сообщения клиента ждал этой строки и не мог записать сообщение, так
    что маркер свежести (30.08) в бою не срабатывал никогда: ответ по неполной
    картине уходил клиенту. «Принять», «Забрать себе» и отправка оператора по
    этому диалогу висели те же 15 секунд. Теперь первая транзакция доходит до
    шага ИИ и коммитится, модель отвечает без блокировки, вторая транзакция
    сверяет, что за это время ничего не изменилось, и продолжает сценарий.
    """
    outcome = await _run_tick(
        ctx, conversation_id, incoming_text, token, ignore_backfill=ignore_backfill
    )
    await flush_outbox(ctx, outcome.outbox)
    # Текст для классификатора собирает первая транзакция; вторая, упёршись в
    # ворота (приняли, закрыли, заглушили), его не вернёт — а негатив клиента
    # обязан дойти до классификатора и тогда (проверка 24.09).
    classify_text = outcome.classify_text
    for _ in range(MAX_AI_CALLS_PER_TICK):
        if outcome.deferred is None:
            break
        answer = AiAnswer(outcome.deferred, await outcome.deferred.ask())
        outcome = await _apply_ai_answer(ctx, conversation_id, answer)
        await flush_outbox(ctx, outcome.outbox)
        classify_text = outcome.classify_text or classify_text
    if outcome.deferred is not None:
        outcome = await _stop_ai_chain(ctx, conversation_id, outcome.deferred)
        await flush_outbox(ctx, outcome.outbox)
    if outcome.stale_text is not None:
        await _keep_series_answered(ctx, conversation_id, outcome.stale_text)
    return outcome.result, classify_text


async def _run_tick(
    ctx: dict[str, Any],
    conversation_id: uuid.UUID,
    incoming_text: str | None,
    token: str | None,
    *,
    ignore_backfill: bool = False,
) -> TickOutcome:
    """Первая транзакция тика: ворота, входящее или таймаут, сценарий до шага ИИ."""
    factory = ctx["db_session_factory"]
    outbox = Outbox()
    async with factory() as db:
        async with db.begin():
            # Адрес и токен лид-бота живут в базе (`app/bots/leadbot.py`), а шаг
            # `ai_answer` читает их из кэша процесса — своей сессии у него нет.
            # Обновляем здесь, ДО взятия блокировки строки диалога: запрос под
            # блокировкой — это лишние миллисекунды на каждом тике. Кэш живёт
            # полминуты, так что к базе мы за этим почти не ходим.
            #
            # Внутри транзакции, а не перед ней, и это не мелочь: `db.get` сам
            # открывает транзакцию, после чего `db.begin()` падает с «A
            # transaction is already begun». Ровно так этот тик и падал —
            # целиком, вместе с доставкой сообщений, то есть нарушая то самое
            # решение №4, ради которого всё остальное здесь и написано.
            await leadbot.ensure_fresh(db)
            # Срок подробного следа перечитывается здесь же и по той же
            # причине: тик — единственное место конвейера, где сессия уже
            # есть, а идти в базу за настройкой из середины пути нельзя.
            await trace.refresh(db)
            conv = await _conversation_for_update(db, conversation_id)
            if conv is None:
                return TickOutcome("no_conversation", outbox)
            bot = await get_bot_for_conversation(db, conv)
            state = BotState.from_conv(conv)
            blocked = await _tick_guards(
                ctx, db, conv, bot, state, outbox, check_backfill=not ignore_backfill
            )
            if blocked is not None:
                return TickOutcome(blocked, outbox)
            assert bot is not None  # ворота выше отсекают диалог без бота

            engine = ScenarioEngine(
                bot=bot,
                conv=conv,
                state=state,
                db=db,
                outbox=outbox,
                defer_ai=True,
                **_engine_kwargs(ctx),
            )
            if incoming_text is not None:
                # ⚠ ВСЯ СЕРИЯ, А НЕ ПЕРВОЕ СООБЩЕНИЕ (проверка 24.09). Тик ставится
                # по первому сообщению серии, следующие уходят в `bot.debounced` —
                # и проверки сценария их не видели: «сейчас» + номер следом давали
                # «Не вижу номера», «позовите оператора» вторым сообщением терялось.
                series, all_seen = await _client_series(db, conv, state)
                if all_seen:
                    # Серию уже разобрал другой тик — отвечать второй раз не на что.
                    return TickOutcome("seen", outbox)
                texts = [text for _, text in series if text.strip()]
                await engine.on_incoming("\n".join(texts) if texts else incoming_text, series=texts)
                state.remember_seen(message_id for message_id, _ in series)
            else:
                await engine.on_timeout(token)
            await engine.run()
            conv.bot_vars = state.dump()  # атомарно вместе с сообщениями
            return TickOutcome("ok", outbox, engine.pending_classification, engine.deferred_ai)


async def _apply_ai_answer(
    ctx: dict[str, Any], conversation_id: uuid.UUID, answer: AiAnswer
) -> TickOutcome:
    """Вторая транзакция тика: ответ модели — в сценарий, если он ещё к месту.

    Пока модель думала, диалог жил: клиент мог дописать, оператор — принять,
    руководитель — закрыть, админ — выключить бота. Ворота те же, что у первой
    транзакции; сверх них — сценарий обязан стоять на том же шаге ИИ, а
    последнее входящее — быть тем, по которому собран контекст.
    """
    factory = ctx["db_session_factory"]
    outbox = Outbox()
    request = answer.request
    async with factory() as db:
        async with db.begin():
            conv = await _conversation_for_update(db, conversation_id)
            if conv is None:
                return TickOutcome("no_conversation", outbox)
            bot = await get_bot_for_conversation(db, conv)
            state = BotState.from_conv(conv)
            blocked = await _tick_guards(ctx, db, conv, bot, state, outbox, check_backfill=False)
            if blocked is not None:
                return TickOutcome(blocked, outbox)
            assert bot is not None
            if state.step != request.step_id or state.waiting is not None:
                return TickOutcome("stale", outbox)
            if await _latest_out_id(db, conversation_id) != request.last_out_id:
                # Пока модель думала, клиенту уже ответили (оператор — в режиме
                # подсказки его ответ бота не глушит): подсказка к отвеченному
                # вопросу легла бы в ленту после ответа (проверка 24.09).
                return TickOutcome("stale", outbox)
            latest_id, latest_text = await _latest_incoming(db, conversation_id)
            if latest_id != request.last_in_id:
                # Маркер свежести (30.08): ответ сочинён по неполной картине.
                # Шаг стоит на месте без ожидания — тик на новое сообщение
                # ответит по всей переписке (`engine.on_incoming`).
                log.info(
                    "bot.reply_stale_dropped",
                    conversation_id=str(conversation_id),
                    reason="client_wrote_while_thinking",
                )
                return TickOutcome("stale", outbox, stale_text=latest_text or "")
            engine = ScenarioEngine(
                bot=bot,
                conv=conv,
                state=state,
                db=db,
                outbox=outbox,
                defer_ai=True,
                **_engine_kwargs(ctx),
            )
            engine.resume_ai(answer)
            await engine.run()
            conv.bot_vars = state.dump()
            return TickOutcome("ok", outbox, engine.pending_classification, engine.deferred_ai)


async def _tick_guards(
    ctx: dict[str, Any],
    db: AsyncSession,
    conv: Conversation,
    bot: Bot | None,
    state: BotState,
    outbox: Outbox,
    *,
    check_backfill: bool,
) -> str | None:
    """Повторные ворота тика: между постановкой и выполнением всё могло измениться.

    `None` — сценарий идёт; иначе — итог тика. Одни и те же для обеих
    транзакций: пока модель думала, диалог могли принять, закрыть, бота —
    выключить.
    """
    if bot is None or not bot.is_enabled:
        if conv.bot_active:
            # ⚠ БОТА ВЫКЛЮЧИЛИ, ПОКА ОН ВЁЛ ДИАЛОГ (проверка 24.09). Здесь
            # стоял голый «no_bot»: `bot_active` оставался, и диалог
            # висел скрытым из «Входящих» — вернуть его мог только
            # сторож зависших, а молчащего клиента не возвращал никто.
            await handoff_mod.do_handoff(
                db,
                conv,
                state,
                outbox,
                reason="bot_disabled",
                bot_id=state.bot_id,
                suggest=bot is not None and bot.mode == "suggest",
            )
            conv.bot_vars = state.dump()
        return "no_bot"
    # ⚠ ЗАКРЫТЫЙ ДИАЛОГ БОТ НЕ ТРОГАЕТ НИ В КАКОМ РЕЖИМЕ (проверка 24.09):
    # тик, отложенный окном серии или подгрузкой истории, мог проснуться
    # уже после закрытия человеком — и подсказка с передачей переоткрывала
    # закрытый как спам диалог в «Новые».
    if conv.status == "closed":
        _step_aside(conv, state, outbox)
        return "closed"
    подсказки = str(getattr(bot, "mode", "") or "") == "suggest"
    # ⚠ РЕЖИМ ПОДСКАЗОК ЖИВЁТ ПО ЛЕСТНИЦЕ ВОРОТ, А НЕ ПО ЭТОЙ.
    #
    # `bot_entry_block` с 21.08 намеренно пропускает подсказки мимо
    # запретов, которые берегут КЛИЕНТА от бота: подсказка это заметка,
    # клиенту она не уходит. А здесь стоял повторный guard, который про
    # режим не знал вовсе, — и глушил ровно то, ради чего правку делали.
    #
    # Цена была не «иногда»: `muted` не сбрасывается никогда, а
    # `has_operator_messages` считает ЛЮБОЕ исходящее оператора, включая
    # приехавшее подгрузкой истории Авито. То есть диалог с чужой
    # историей получал «muted» на первом же тике и не оживал до конца
    # жизни. Замер 20.08, ради которого и правили ворота, — «из 445
    # диалогов бот замолкал после первой реплики оператора» — в бою
    # оставался в силе, потому что чинили только первый рубеж из двух.
    #
    # Двух лестниц запретов быть не должно; пока их две, эта обязана
    # быть зеркалом ворот.
    if подсказки:
        # Подсказка диалог не ведёт (проверка 24.09): `bot_active` и
        # ожидание, оставшиеся от прошлых тиков или от автоответа до
        # переключения, снимаем — иначе диалог скрыт из «Входящих».
        # Ожидание без срока — законное: сценарий без шага ИИ помнит заданный
        # в подсказке вопрос (`engine._begin_waiting`). Раньше расписания: вне окна
        # работы тик уходит сразу, а диалог остался бы спрятанным.
        if conv.bot_active or (state.waiting is not None and state.waiting.deadline):
            _step_aside(conv, state, outbox)
        # Расписание — единственный запрет, который у подсказок остаётся:
        # каждый ход это платный вызов шлюза, окно работы решает владелец.
        # Проверяем повторно, потому что между постановкой в очередь и
        # выполнением тика окно могло закрыться.
        if not is_bot_scheduled_now(bot):
            return "not_scheduled"
    elif conv.assignee_id is not None or conv.claimed_by_id is not None:
        # ⚠ ДИАЛОГ ПРИНЯТ ИЛИ НАЗНАЧЕН ЧЕЛОВЕКУ (проверка 24.09). Ворота
        # (`bot_entry_block`) судят в момент постановки, а тик просыпается
        # через окно серии: оператор успевал «Принять», и бот всё равно
        # здоровался с клиентом, снова ставя `bot_active` на чужом
        # диалоге; дедлайн `ask` так же дожимал клиента у назначенного.
        _step_aside(conv, state, outbox)
        return "claimed"
    elif state.muted or await has_operator_messages(db, conv):
        await mute_bot(db, conv)  # 02 §2.6
        return "muted"
    elif state.handoff_done():
        # ⚠ ДИАЛОГ УЖЕ ОТДАН ЛЮДЯМ (проверка 24.09). Ворота не пускают бота после
        # передачи, но тик, поставленный раньше неё, или передача, закоммиченная
        # классификатором, пока модель думала, доходили до сценария: бот отвечал
        # клиенту после «передаю мастеру» и снова прятал диалог из очереди.
        return "handoff_done"
    # ⚠ ПОКА ЕДЕТ ИСТОРИЯ — НЕ ОТВЕЧАЕМ (19.08). В окне подгрузки диалог в
    # базе состоит из одной свежей реплики, и подсказка выходит вслепую:
    # приветствие посреди переписки, повторный вопрос про адрес. Ждём
    # (вызывающий перепланирует тик) — кроме случая, когда ждать уже хватит.
    if check_backfill and await _идёт_подгрузка(ctx, conv):
        return "backfill"
    return None


async def _latest_incoming(db: AsyncSession, conversation_id: uuid.UUID) -> tuple[Any, str | None]:
    """Последнее входящее диалога: id для маркера свежести и текст."""
    row = (
        await db.execute(
            select(Message.id, Message.body)
            .where(Message.conversation_id == conversation_id, Message.direction == "in")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).first()
    return (row.id, row.body) if row is not None else (None, None)


async def _latest_out_id(db: AsyncSession, conversation_id: uuid.UUID) -> Any:
    """Последняя реплика клиенту — любого автора."""
    return (
        await db.execute(
            select(Message.id)
            .where(Message.conversation_id == conversation_id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _stop_ai_chain(
    ctx: dict[str, Any], conversation_id: uuid.UUID, request: AiRequest
) -> TickOutcome:
    """Шагов ИИ подряд больше :data:`MAX_AI_CALLS_PER_TICK` — это зацикливание.

    Шаг стоял бы без ожидания, с `bot_active`, и сторожа его не видят: последняя
    реплика за ботом. Отдаём людям с той же причиной, что и защита от
    зацикливания движка (02 §2.7).
    """
    log.error("bot.ai_calls_exhausted", conversation_id=str(conversation_id))
    factory = ctx["db_session_factory"]
    outbox = Outbox()
    async with factory() as db:
        async with db.begin():
            conv = await _conversation_for_update(db, conversation_id)
            if conv is None:
                return TickOutcome("no_conversation", outbox)
            state = BotState.from_conv(conv)
            if state.step != request.step_id or state.waiting is not None:
                return TickOutcome("stale", outbox)
            await handoff_mod.do_handoff(
                db, conv, state, outbox, reason="loop_protection", bot_id=state.bot_id
            )
            conv.bot_vars = state.dump()
            return TickOutcome("loop_protection", outbox)


async def _keep_series_answered(ctx: dict[str, Any], conversation_id: uuid.UUID, text: str) -> None:
    """Ответ отброшен из-за нового сообщения — тик на это сообщение обязан стоять.

    Обычно его уже поставил вебхук: окно серии первого сообщения истекло к
    началу тика. Ставим сами, только если окна нет (`debounce_claim` удался) —
    иначе ответов на одну серию стало бы два.
    """
    redis = ctx["redis"]
    окно = debounce_window(None, conversation_id, текст=text)
    if await debounce_claim(redis, conversation_id, None, окно=окно):
        await enqueue_bot_step(redis, conversation_id, text, arq=ctx.get("arq"), defer_by=окно)


async def _client_series(
    db: AsyncSession, conv: Conversation, state: BotState
) -> tuple[list[tuple[str, str]], bool]:
    """Серия клиента, на которую отвечает тик: `[(id, текст)]` по порядку.

    Входящие, ещё не разобранные ботом (`state.seen_in`), не старше окна от
    последнего и после последнего ответа ОПЕРАТОРА: человек ответил — прежние
    сообщения клиента уже не к боту. Второе значение — «в окне есть сообщения
    клиента, но все разобраны»: тик пришёл на серию, которую уже закрыл другой.
    """
    last_operator_out = (
        select(func.max(Message.created_at))
        .where(
            Message.conversation_id == conv.id,
            Message.direction == "out",
            Message.sender_type == "operator",
        )
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Message.id, Message.body, Message.created_at)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == "in",
                Message.sender_type == "client",
                or_(last_operator_out.is_(None), Message.created_at > last_operator_out),
            )
            .order_by(Message.created_at.desc())
            .limit(SERIES_MAX_MESSAGES)
        )
    ).all()
    if not rows:
        return [], False
    newest = rows[0].created_at
    window = [r for r in reversed(rows) if newest - r.created_at <= SERIES_LOOKBACK]
    fresh = [(str(r.id), r.body or "") for r in window if str(r.id) not in state.seen_in]
    return fresh, not fresh


def _step_aside(conv: Conversation, state: BotState, outbox: Outbox) -> None:
    """Бот отходит без заглушения: ожидание снято, диалог больше не «ведёт бот».

    Не `mute_bot`: человек мог принять диалог на минуту и вернуть его в
    очередь — навсегда глушить бота за это незачем, ворота решат заново.
    """
    state.stop_waiting()
    conv.bot_vars = state.dump()
    if conv.bot_active:
        conv.bot_active = False
        outbox.event(
            "conversation:updated", conversation_updated_event(conv, {"bot_active": False})
        )


async def _classify_after_commit(
    ctx: dict[str, Any], conversation_id: uuid.UUID, text: str
) -> None:
    """Классификатор негатива и просьбы человека — после commit'а тика (02 §3.4).

    Тексты читаем без блокировки, классификатор зовём вне транзакции, реакцию
    пишем под `FOR UPDATE` (проверка 24.09): поход в модель под блокировкой
    строки заставлял ждать вебхуки и действия оператора по этому диалогу.
    """
    factory = ctx["db_session_factory"]
    outbox = Outbox()
    try:
        async with factory() as db:
            conv = await db.get(Conversation, conversation_id)
            bot = await get_bot_for_conversation(db, conv) if conv is not None else None
            if conv is None or bot is None:
                return
            reader = ScenarioEngine(
                bot=bot,
                conv=conv,
                state=BotState.from_conv(conv),
                db=db,
                **_engine_kwargs(ctx),
            )
            reader.last_incoming = text
            texts = await reader.client_texts()
            backend = reader.ai
        result = await classify_texts(backend, texts)
        if result is None:
            return
        async with factory() as db:
            async with db.begin():
                conv = await _conversation_for_update(db, conversation_id)
                if conv is None:
                    return
                bot = await get_bot_for_conversation(db, conv)
                if bot is None:
                    return
                state = BotState.from_conv(conv)
                engine = ScenarioEngine(
                    bot=bot, conv=conv, state=state, db=db, outbox=outbox, **_engine_kwargs(ctx)
                )
                await engine.react_to_classification(result)
                conv.bot_vars = state.dump()
        await flush_outbox(ctx, outbox)
    except Exception:  # noqa: BLE001 — вспомогательный контур, тик уже закоммичен
        log.exception("bot.classify_tick_failed", conversation_id=str(conversation_id))


@with_job_scope
async def bot_ask_timeout(ctx: dict[str, Any], conversation_id: uuid.UUID, token: str) -> str:
    """Дедлайн `ask`/`menu` (02 §2.4). No-op, если ответ уже пришёл.

    Отменять задачу при ответе клиента не нужно — она самоаннулируется по
    токену: каждый `exec_ask` генерирует новый, а истина о том, чего мы ждём,
    живёт только в `bot_vars.waiting`. Это дешевле `Job.abort()` и устойчиво к
    рестартам воркеров.
    """
    factory = ctx["db_session_factory"]
    # ⚠ ПОТЕРЯННЫЙ ДЕДЛАЙН ЗАМОРАЖИВАЕТ ДИАЛОГ, И НЕ ВИДИТ ЭТОГО НИКТО
    # (разбор 03.09).
    #
    # Тело — одна транзакция к базе. Заминка на секунду, и ARQ помечает задачу
    # проваленной без повтора: `max_tries` действует только на `Retry`, обычное
    # исключение это конец (проверено по исходнику arq/worker.py). Дедлайн
    # исчезает, диалог остаётся с `bot_active=True` и ждёт вечно — бот не
    # дожимает и не передаёт человеку.
    #
    # Сторож зависших бота сюда не смотрит: он ловит «клиент написал, бот не
    # ответил», а здесь последнее слово как раз за ботом. Потерянный дедлайн
    # подбирает свой сторож — `scheduler/jobs/bot_deadlines.py` (24.09).
    #
    # ⚠ ПОВТОР ЗДЕСЬ БЕЗОПАСЕН, В ОТЛИЧИЕ ОТ `bot_step`. Задача
    # самоаннулируется по токену (см. докстринг): ответил клиент — `waiting`
    # пересоздан, и повтор вернёт `stale`, ничего не сделав. Транзакция при
    # ошибке откатывается целиком, так что повторять нечего дважды.
    try:
        async with factory() as db, db.begin():
            conv = await _conversation_for_update(db, conversation_id)
            if conv is None or not conv.bot_active:
                return "inactive"
            # ⚠ ВТОРОЙ ЗАМОК: ЗАКРЫТЫЙ ДИАЛОГ ДОЖИМАТЬ НЕЛЬЗЯ (аудит 30.08).
            #
            # Первый замок — гашение бота при закрытии (`services/conversations`).
            # Этого достаточно для будущих закрытий, но задачи, поставленные ДО
            # выкатки, уже лежат в очереди со своими токенами и проснутся как есть.
            # Да и цена промаха несимметрична: клиент закрытого обращения получает
            # от нас «Ну что, подскажете?» — то есть мы пишем человеку, разговор с
            # которым сами же завершили.
            if conv.status == "closed":
                return "closed"
            waiting = BotState.from_conv(conv).waiting
            if waiting is None or waiting.token != token:
                return "stale"  # клиент уже ответил: waiting сброшен/пересоздан
            # ⚠ КЛИЕНТ МОГ ОТВЕТИТЬ, А ТИК ЕЩЁ НЕ ОТРАБОТАТЬ (находка 26.08).
            # `waiting` снимает шаг бота, а он теперь отложен окном серии на полторы-две с
            # половиной минуты. Всё это время сообщение клиента уже в базе, а `waiting` ещё
            # стоит — и дедлайн, сработав первым, передаёт диалог с причиной «клиент не
            # ответил в отведённое время». Владелец прислал ровно такой снимок: клиент в
            # 21:12 прислал фото блока питания, в 21:14 бот отдал диалог как молчание.
            # Спрашиваем прямо у ленты: последнее слово чьё. Если клиента — дедлайну здесь
            # делать нечего, отложенный тик разберётся сам.
            последнее = (
                await db.execute(
                    select(Message.direction)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if последнее == "in":
                return "answered"
            now = ctx["bot_now"]() if callable(ctx.get("bot_now")) else None
            if not waiting.expired(now):
                remaining = _remaining(waiting.deadline, now)
                if remaining is not None and remaining.total_seconds() > MIN_REDEFER_SECONDS:
                    # job_id не задаём: у ARQ он уникален и дедуплицирует
                    # постановку, а переставить себя задача может не один раз.
                    await _enqueue(ctx, BOT_TIMEOUT_JOB, conversation_id, token, defer_by=remaining)
                    return "early"
        # токен совпал, дедлайн истёк — продолжаем сценарий веткой on_timeout
        await _enqueue(ctx, BOT_STEP_JOB, conversation_id, None, token)
        return "fired"
    except Retry:
        raise
    except Exception as exc:
        попытка = int(ctx.get("job_try") or 1)
        if попытка >= ЛИМИТ_ПОВТОРОВ_ДЕДЛАЙНА:
            # Дальше повторять бессмысленно: пусть отказ будет виден, а дедлайн
            # поставит заново сторож `scheduler/jobs/bot_deadlines.py`. Сторож
            # зависших этот диалог не видит: последнее слово за ботом.
            log.error(
                "bot.ask_timeout_lost",
                conversation_id=str(conversation_id),
                attempt=попытка,
                error=f"{type(exc).__name__}: {exc}",
            )
            return "failed"
        log.warning(
            "bot.ask_timeout_retry",
            conversation_id=str(conversation_id),
            attempt=попытка,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise Retry(defer=RETRY_DELAY * попытка) from exc


def _remaining(deadline: str | None, now: datetime | None) -> timedelta | None:
    parsed = parse_iso(deadline)
    if parsed is None:
        return None
    return parsed - (now or utcnow())
