"""Кто отвечает на шаге `ai_answer`: наш Claude или лид-бот владельца.

ЗАЧЕМ ОТДЕЛЬНЫЙ СЛОЙ. Движку всё равно, кто за него думает: он зовёт
`ai_answer(bot, dialog, item_title)` и разбирает ответ. Значит выбор поставщика
некуда девать, кроме как в сам вызов, — и лучше, чтобы движок не знал ни слова
«claude», ни слова «лид-бот». Здесь ровно одна развилка и ничего больше.

ПОЧЕМУ ВЫБОР У БОТА, А НЕ У КАНАЛА. Всё, что определяет ответ, уже лежит на
боте: сценарий с шагом `ai_answer`, база знаний, расписание. Канал (аккаунт
Авито) отвечает за доставку и сам ссылается на бота (`avito_accounts.bot_id`).
Поставь переключатель на канал — и два аккаунта с одним ботом стали бы отвечать
разными мозгами по одной и той же базе знаний, причём в редакторе бота эта
разница не видна вообще: человек правит базу знаний и не понимает, почему на
одном аккаунте правки работают, а на другом нет. Обратная плата честная: чтобы
включить лид-бота только на одном канале, придётся завести отдельного бота. Это
видимое действие с видимым результатом.

ПО УМОЛЧАНИЮ — КАК БЫЛО. `bots.ai_provider = 'claude'`, то есть подключение
лид-бота ничего не меняет само по себе; его включают явно, боту за ботом.

ЧТО ПОДМЕНЯЕТСЯ, А ЧТО НЕТ. Подменяется ТОЛЬКО `ai_answer`. Классификатор
(негатив, просьба позвать человека) и извлечение сущностей перед передачей
остаются нашими: у лид-бота таких ручек нет, и если бы мы просто перестали их
звать, диспетчеры молча потеряли бы метку «негатив» и подсказки в карточке
передачи — потерю, которую никто бы не связал с включением автоответа.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.bots import ai as claude
from app.bots import leadbot

log = structlog.get_logger("app.bots.provider")

#: Значения `bots.ai_provider`. Список — контракт с CHECK в `models/bot.py` и в
#: миграции 0035; там значения выписаны литералами, а здесь живёт код, и
#: разъехаться им не даёт охранный тест в `tests/unit/test_bot_leadbot.py`.
CLAUDE = "claude"
LEADBOT = "leadbot"
PROVIDERS = (CLAUDE, LEADBOT)
DEFAULT = CLAUDE


def provider_of(bot: Any) -> str:
    """Какой поставщик выбран у бота. Неизвестное значение = прежнее поведение.

    Неизвестное сюда попасть не должно (CHECK в базе), но если попадёт — при
    восстановлении копии из будущей версии, например, — отвечать Claude лучше,
    чем не отвечать вовсе.
    """
    value = getattr(bot, "ai_provider", None)
    return value if value in PROVIDERS else DEFAULT


async def ai_answer(
    bot: Any,
    dialog: list[dict[str, Any]],
    item_title: str | None,
    *,
    client_name: str = "",
    city: str = "",
    channel: dict[str, str] | None = None,
    conv_key: str = "",
    prior_suggestions: list[str] | None = None,
    calls: int = 0,
) -> dict[str, Any] | None:
    """Шаг `ai_answer` (02 §3.2). `None` — вызывающий обязан сделать handoff.

    ⚠ `prior_suggestions` и `calls` уходят ТОЛЬКО лид-боту (24.08): это его
    контракт, у нашего Claude полей под них нет. Смысл — в `engine.load_bot_context`.
    """
    if provider_of(bot) != LEADBOT:
        # `dict(...)`, а не сам объект: у Claude ответ типизирован как `AIAnswer`
        # (TypedDict), и отдавать наружу разные типы для двух поставщиков значит
        # завести два контракта там, где договаривались об одном.
        answer = await claude.ai_answer(bot, dialog, item_title, client_name=client_name, city=city)
        return dict(answer) if answer is not None else None

    config = leadbot.current()
    if not config.is_ready:
        # НЕ откатываемся на Claude молча. Владелец выбрал другой мозг с другим
        # регламентом — ответить вместо него нашим значит выдать клиенту цены и
        # формулировки, которых он не согласовывал, и никто этого не заметит.
        # Передать диалог человеку — заметно, и чинится настройкой.
        log.warning("bot.leadbot_not_configured", bot_id=str(getattr(bot, "id", "")))
        return None

    return await leadbot.LeadbotAI(config.url, config.token).ai_answer(
        bot,
        dialog,
        item_title,
        prior_suggestions=prior_suggestions or [],
        calls=calls,
        client_name=client_name,
        city=city,
        channel=channel,
        conv_key=conv_key,
    )


async def classify_message(texts: list[str]) -> dict[str, Any] | None:
    """Классификатор всегда наш — см. шапку файла."""
    return await claude.classify_message(texts)


async def extract_entities(texts: list[str]) -> dict[str, Any] | None:
    """Извлечение сущностей перед передачей — тоже наше."""
    return await claude.extract_entities(texts)
