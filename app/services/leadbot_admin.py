"""Лид-бот как отдельный раздел настроек: связь, режим, каналы, проверка.

РЕШЕНИЕ ВЛАДЕЛЬЦА (12 августа): «сам LeadBot это не совсем бот, я хочу чтобы у
него была своя собственная вкладка и настройки». До сих пор он настраивался
выбором «мозга» у обычного бота — и это правда неверно по смыслу: лид-бот
отдельный продукт на своём сервере, со своим регламентом и своей ценой вызова.

ЧТО СНАРУЖИ И ЧТО ВНУТРИ. Снаружи — свой раздел; в списке ботов лид-бота нет.
Внутри он остаётся записью в `bots` с признаком `is_system`, потому что
отвечает он на шаге `ai_answer` сценария, а в движке живут расписание,
замолкание при ответе менеджера, handoff, лимиты и правило «недоступность ИИ не
блокирует доставку». Вынести его оттуда «ради чистоты» значило бы написать всё
это заново — и каждая потерянная мелочь означала бы сообщение, которого клиент
не дождался.

Этот модуль и есть тот единственный слой, который знает про подмену. Дальше по
коду лид-бот — обычный бот; выше по коду — отдельная сущность.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import leadbot
from app.bots.provider import LEADBOT
from app.models.account import AvitoAccount
from app.models.bot import Bot

log = structlog.get_logger("app.leadbot_admin")

#: Имя системной записи. Видно оно только в аудите и в базе: в списке ботов её
#: нет, а на своём экране заголовок написан словами, а не взят отсюда.
SYSTEM_BOT_NAME = "Лид-бот"

#: Идентификатор шага, на котором лид-бота и спрашивают. Нужен снаружи: экран
#: правит глубину контекста, а она лежит в параметрах именно этого шага.
ANSWER_STEP = "ask_leadbot"
WAIT_STEP = "wait_client"
#: Молчащий сутки диалог уходит человеку в очередь — см. `default_scenario`.
NO_REPLY_STEP = "handoff_no_reply"

#: Сколько последних реплик уходит на ту сторону. Тридцать, а не десять как у
#: нашего ИИ: лид-бот ведёт разговор целиком — от приветствия до цены и окна
#: визита, — и на десяти репликах он теряет начало собственной воронки.
DEFAULT_CONTEXT_MESSAGES = 30
MAX_CONTEXT_MESSAGES = 100
#: ⚠ НИЖНЯЯ ГРАНИЦА — НЕ ЕДИНИЦА. Здесь стоял `max(1, ...)`, и это молчаливое
#: приведение стоило боя. Экран настроек отправляет число по уходу фокуса, а
#: `Number("")` — это НОЛЬ: стоило очистить поле, чтобы набрать заново, и уезжал
#: ноль, который превращался в единицу. Лид-бот получал контекст из ОДНОЙ реплики
#: и терял весь разговор — ровно то, на что жаловался владелец 23.08
#: («context_messages сбит с 12 на 1»), и никто не связал это с полем ввода:
#: подмена была тихой, ошибки не было, в журнале — обычное сохранение.
#: Две реплики — минимум, при котором «контекст» вообще имеет смысл: вопрос и
#: ответ. Та же граница стоит в схеме сценария (`bots/validator.py`), и держать
#: два разных представления о минимуме нельзя.
MIN_CONTEXT_MESSAGES = 2


def default_scenario(context_messages: int = DEFAULT_CONTEXT_MESSAGES) -> dict[str, Any]:
    """Сценарий системной записи — предельно короткий, и это главное в нём.

    У обычного бота сценарий здоровается, задаёт вопросы, разводит день и ночь.
    Здесь ничего этого быть не должно: разговор целиком ведёт лид-бот по своему
    регламенту, и любая наша реплика вклинилась бы в его воронку чужим голосом.

    Поэтому шагов ровно три: спросить лид-бота, дождаться следующей реплики
    клиента, спросить снова. Сутки ожидания и тихое закрытие по их истечении —
    те же значения, что у обычного бота: молчащий сутки диалог закрывать тихо,
    без прощальной фразы, которая читается как «мы вас больше не ждём».
    """
    return {
        "version": 1,
        "revision": 1,
        "entry": ANSWER_STEP,
        # Потолок реплик подряд поднят: у обычного бота пять, но там ответы
        # наши. Здесь каждая реплика — ответ лид-бота по регламенту, и обрывать
        # его воронку на пятой значит отдать человеку диалог, который бот довёл
        # бы до заявки.
        "settings": {"max_bot_messages_row": 10, "max_offscript_messages": 3},
        "steps": [
            {
                "id": ANSWER_STEP,
                "type": "ai_answer",
                "params": {
                    # Порог тот же, что у всех: ниже него ответ выбрасывается
                    # целиком. Лид-бот отдаёт 1.0 у роутеров регламента и 0.9 у
                    # модели — то есть в норму попадает всегда, а до порога
                    # опускается ровно тогда, когда сам считает, что не тянет.
                    "confidence_threshold": 0.6,
                    "max_reply_len": 800,
                    "context_messages": context_messages,
                },
                "next": WAIT_STEP,
                "on_low_confidence": None,
            },
            {
                "id": WAIT_STEP,
                "type": "ask",
                "params": {
                    # `text: null` — ждать молча. Свой вопрос лид-бот уже задал.
                    "text": None,
                    "var": "_client_said",
                    "validate": "any",
                    "retry_text": None,
                    "max_attempts": 1,
                    "timeout": "24h",
                },
                "next": ANSWER_STEP,
                # ⚠ МОЛЧАЩИЙ СУТКИ ДИАЛОГ УХОДИТ ЧЕЛОВЕКУ, А НЕ ЗАКРЫВАЕТСЯ САМ. Здесь
                # стояло тихое закрытие, а боевая запись давно жила с передачей в очередь
                # («автозакрытия по таймауту нет, закрывает менеджер вручную»). Два решения
                # разошлись, и код проигрывал бою — сводим к боевому, оно осторожнее:
                # закрытый диалог из очереди не вернёшь, а лишняя строка в очереди дёшева.
                "on_timeout": NO_REPLY_STEP,
                "on_invalid": None,
            },
            {
                # ⚠ ДОЖИМА ЗДЕСЬ НЕТ, И ЭТО НАМЕРЕННО (31.08). Соблазн велик: по тайм-ауту
                # снова спросить лид-бота, чтобы напоминание звучало ЕГО словами, а у панели
                # не было ни одной своей фразы. Так и было сделано, и так делать нельзя без
                # правки на той стороне: бот получит ленту, где последняя реплика клиента —
                # та самая, на которую он уже ответил, и ответит на неё второй раз. Удержала
                # бы только дедупликация в панели, то есть ровно та логика, которую решение
                # владельца 31.08 отсюда и убирает.
                #
                # Дожим вернётся, когда бот научится отличать «клиент молчит» от «клиент
                # написал»: панель передаст признак, бот сам решит, напоминать или молчать,
                # и текст будет его. До тех пор пауза сутки и передача человеку — честнее,
                # чем чужой голос в диалоге.
                "id": NO_REPLY_STEP,
                "type": "handoff",
                "params": {
                    "reason": "ask_timeout",
                    "tags": ["без ответа"],
                    "comment": (
                        "Клиент не ответил сутки. Диалог остаётся в общей очереди — "
                        "автозакрытия по таймауту нет, закрывает менеджер вручную"
                    ),
                },
            },
        ],
    }


async def system_bot(db: AsyncSession) -> Bot | None:
    """Запись лид-бота. `None` — её ещё не создавали."""
    return (
        await db.execute(select(Bot).where(Bot.is_system.is_(True)).limit(1))
    ).scalar_one_or_none()


async def ensure_system_bot(db: AsyncSession) -> Bot:
    """Запись лид-бота, создавая её при первом обращении.

    ПОЧЕМУ СОЗДАЁТСЯ САМА, А НЕ КНОПКОЙ «СОЗДАТЬ». Кнопка означала бы, что
    открытый раздел настроек бывает пустым и требует нажатия, смысл которого
    неочевиден: запись эта служебная, человек про неё не знает и знать не
    должен. Ему нужно ввести адрес и включить — остальное наше дело.

    ⚠ ВЫКЛЮЧЕНА ПРИ СОЗДАНИИ И РАБОТАЕТ ПОДСКАЗКОЙ. Иначе первое же сохранение
    адреса пустило бы чужой регламент к живым клиентам девяти аккаунтов.
    Включение — отдельное осознанное действие человека.
    """
    existing = await system_bot(db)
    if existing is not None:
        return existing

    bot = Bot(
        id=uuid.uuid4(),
        name=SYSTEM_BOT_NAME,
        is_system=True,
        is_enabled=False,
        ai_provider=LEADBOT,
        mode="suggest",
        schedule={"always": True},
        scenario=default_scenario(),
        knowledge_base=None,
    )
    db.add(bot)
    await db.flush()
    log.info("leadbot.system_bot_created", bot_id=str(bot.id))
    return bot


def context_messages_of(bot: Bot) -> int:
    """Глубина контекста из параметров шага — единственное место, где она есть."""
    scenario = bot.scenario if isinstance(bot.scenario, dict) else {}
    for step in scenario.get("steps") or []:
        if isinstance(step, dict) and step.get("id") == ANSWER_STEP:
            params = step.get("params")
            if isinstance(params, dict):
                try:
                    return int(params.get("context_messages", DEFAULT_CONTEXT_MESSAGES))
                except (TypeError, ValueError):
                    return DEFAULT_CONTEXT_MESSAGES
    return DEFAULT_CONTEXT_MESSAGES


def _with_context_messages(bot: Bot, value: int) -> dict[str, Any]:
    """Сценарий с новой глубиной контекста.

    Пересобирается ЦЕЛИКОМ, а не правится на месте: `scenario` — JSONB, и
    SQLAlchemy не заметит изменения вложенного словаря без замены объекта.
    На этом уже спотыкались с `bot_vars`.
    """
    scenario = dict(bot.scenario) if isinstance(bot.scenario, dict) else default_scenario()
    steps = []
    for step in scenario.get("steps") or []:
        if isinstance(step, dict) and step.get("id") == ANSWER_STEP:
            params = dict(step.get("params") or {})
            params["context_messages"] = value
            step = {**step, "params": params}
        steps.append(step)
    scenario["steps"] = steps
    return scenario


async def bound_accounts(db: AsyncSession, bot: Bot) -> list[AvitoAccount]:
    """Каналы, обращения которых обслуживает лид-бот."""
    rows = await db.execute(
        select(AvitoAccount)
        .where(AvitoAccount.bot_id == bot.id, AvitoAccount.is_service.is_(False))
        .order_by(AvitoAccount.title)
    )
    return list(rows.scalars().all())


async def overview(db: AsyncSession) -> dict[str, Any]:
    """Всё, что нужно экрану, одним ответом.

    ⚠ ТОКЕНА ЗДЕСЬ НЕТ И НЕ БУДЕТ — только «задан или нет». Он открывает чужому
    сервису всю переписку клиентов; отдать его в браузер значит положить его в
    историю запросов, в кэш и в чужие снимки экрана.
    """
    config = await leadbot.load(db)
    bot = await system_bot(db)
    accounts = await bound_accounts(db, bot) if bot is not None else []
    all_accounts = (
        (
            await db.execute(
                select(AvitoAccount)
                .where(AvitoAccount.is_service.is_(False))
                .order_by(AvitoAccount.title)
            )
        )
        .scalars()
        .all()
    )

    return {
        "connection": leadbot.safe_dump(config),
        "is_ready": config.is_ready,
        "enabled": bool(bot.is_enabled) if bot is not None else False,
        # «suggest» — ответ ложится заметкой оператору, клиент не получает
        # ничего. «auto» — уходит клиенту. Умолчание безопасное.
        "mode": (bot.mode if bot is not None else "suggest"),
        "context_messages": (
            context_messages_of(bot) if bot is not None else DEFAULT_CONTEXT_MESSAGES
        ),
        "account_ids": [str(a.id) for a in accounts],
        "accounts": [
            {
                "id": str(a.id),
                "title": a.title,
                # Источник рядом с названием — просьба владельца 03.09
                # («источники должны стоять везде»). Названия каналов
                # различаются одним словом («Александр КП» / «Александр МНЧ»),
                # а подключают лид-бота именно к источнику.
                "lead_origin": a.lead_origin,
                # Занят ли канал ДРУГИМ ботом: подключить лид-бота к нему —
                # значит отобрать канал у того бота, и человек обязан увидеть
                # это до нажатия, а не по изменившимся ответам клиентам.
                "busy_with_other_bot": a.bot_id is not None and (bot is None or a.bot_id != bot.id),
            }
            for a in all_accounts
        ],
    }


async def apply(
    db: AsyncSession,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    context_messages: int | None = None,
    account_ids: list[uuid.UUID] | None = None,
) -> Bot:
    """Изменить настройки лид-бота. Пустые поля означают «не трогали»."""
    bot = await ensure_system_bot(db)

    if mode is not None:
        if mode not in ("suggest", "auto"):
            raise ValueError("mode")
        bot.mode = mode
    if context_messages is not None:
        # Значение вне границ — ОТКАЗ, а не тихое приведение. Приведение здесь и
        # было бедой: ноль с экрана становился единицей, и человек видел успех.
        value = int(context_messages)
        if not MIN_CONTEXT_MESSAGES <= value <= MAX_CONTEXT_MESSAGES:
            raise ValueError("context_messages")
        bot.scenario = _with_context_messages(bot, value)
    if account_ids is not None:
        await _bind_accounts(db, bot, account_ids)
    if enabled is not None:
        bot.is_enabled = bool(enabled)

    bot.updated_at = datetime.now(UTC)
    await db.flush()
    return bot


async def _bind_accounts(db: AsyncSession, bot: Bot, wanted: list[uuid.UUID]) -> None:
    """Привязать лид-бота ровно к перечисленным каналам.

    ОТВЯЗЫВАЕМ ТОЛЬКО СВОИ. Канал, занятый другим ботом, здесь не трогается
    вовсе: снять чужого бота с канала — это решение про того бота, и приниматься
    оно обязано на его экране. Иначе список каналов лид-бота стал бы способом
    молча выключить чужую автоматику.
    """
    current = await bound_accounts(db, bot)
    keep = set(wanted)
    for account in current:
        if account.id not in keep:
            account.bot_id = None
    if not keep:
        return
    rows = await db.execute(select(AvitoAccount).where(AvitoAccount.id.in_(keep)))
    for account in rows.scalars().all():
        if account.bot_id is not None and account.bot_id != bot.id:
            # Занят другим — пропускаем молча? Нет: молчание здесь читалось бы
            # как «подключили», а канал остался бы у чужого бота.
            raise ValueError(f"busy:{account.id}")
        account.bot_id = bot.id


# --------------------------------------------------------------- проверка

#: Реплика для проверки связи. Обычная фраза клиента, а не «ping»: лид-бот
#: отвечает по регламенту, и на «ping» он честно ответит отказом — то есть
#: проверка связи выглядела бы как поломка регламента.
PROBE_TEXT = "Здравствуйте! Ремонтируете стиральные машины?"


async def probe(db: AsyncSession) -> dict[str, Any]:
    """Проверить связь: дозвонились ли, за сколько, что ответил.

    Никого не задевает: диалога нет, сообщений никому не уходит, в журнал
    работы это не пишется — журнал про живых клиентов.
    """
    config = await leadbot.load(db)
    if not config.is_ready:
        return {
            "ok": False,
            "error": "Не задан адрес или токен",
            "ms": 0,
            "reply": None,
            "layer": None,
        }
    client = leadbot.LeadbotAI(config.url, config.token, budget_seconds=10.0)
    answer = await client.call_raw(
        [{"role": "user", "content": PROBE_TEXT}],
        None,
        request_id=f"probe-{uuid.uuid4().hex[:16]}",
    )
    return {
        "ok": answer.ok,
        "error": answer.error or None,
        "ms": answer.ms,
        "status": answer.status,
        "reply": (answer.answer or {}).get("reply") if answer.ok else None,
        "layer": answer.meta.get("layer"),
    }


async def test_chat(
    db: AsyncSession,
    *,
    dialog: list[dict[str, str]],
    item_title: str | None = None,
) -> dict[str, Any]:
    """Тестовый разговор: спросить лид-бота и показать ВЕСЬ его ответ.

    ⚠ НИ ОДНО СООБЩЕНИЕ ОТСЮДА НЕ УХОДИТ КЛИЕНТУ. Здесь нет диалога, нет
    аккаунта и нет доставки — только вызов чужой ручки и показ того, что она
    вернула. Ради этого свойства тестовый разговор и заводится: посмотреть, что
    бот отвечает, ДО того как пустить его к живым людям.

    Показывается больше, чем видит движок: слой (регламент или модель),
    уверенность, эскалация с причиной и сроком, готовность заявки,
    предупреждения, время. Вопрос владельца к любому ответу — «это регламент
    или он сам придумал», и ответ на него в `meta.layer`.
    """
    config = await leadbot.load(db)
    if not config.is_ready:
        return {"ok": False, "error": "Не задан адрес или токен", "ms": 0}

    clean = [
        {
            "role": ("user" if line.get("role") != "assistant" else "assistant"),
            "content": str(line.get("content") or "").strip(),
        }
        for line in dialog
        if str(line.get("content") or "").strip()
    ]
    if not clean:
        return {"ok": False, "error": "Пустой разговор", "ms": 0}

    client = leadbot.LeadbotAI(config.url, config.token, budget_seconds=15.0)
    answer = await client.call_raw(
        clean,
        item_title,
        # Свой префикс, чтобы обращение из проверки нельзя было спутать с
        # боевым ни в наших журналах, ни в журналах той стороны.
        request_id=f"test-{uuid.uuid4().hex[:16]}",
    )
    meta = answer.meta
    escalation = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else {}
    parsed = answer.answer or {}
    return {
        "ok": answer.ok,
        "error": answer.error or None,
        "ms": answer.ms,
        "status": answer.status,
        "reply": parsed.get("reply"),
        "confidence": parsed.get("confidence"),
        "needs_operator": parsed.get("needs_operator"),
        "layer": meta.get("layer"),
        "flag": meta.get("flag"),
        "escalation": {
            "reason": escalation.get("reason"),
            "label": escalation.get("label"),
            "deadline_min": escalation.get("deadline_min"),
        }
        if escalation
        else None,
        "lead_ready": bool(meta.get("lead_ready")),
        "warnings": meta.get("warnings") if isinstance(meta.get("warnings"), list) else [],
    }
