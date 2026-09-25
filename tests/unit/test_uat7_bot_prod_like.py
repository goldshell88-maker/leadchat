"""Приёмка спринта 7: AI-шаг при ПОЛНОСТЬЮ отсутствующем ключе (07 §5 п.6–7).

Существующие тесты закрывают «мёртвый API» (SDK бросает исключение). Здесь —
конфигурация боевого стенда: ключа Anthropic на шлюзе нет (снимок ``/status``,
docs/46), ``AI_FAKE=0``, клиент не строится вовсе. Шаг ``ai_answer`` обязан
увести диалог в ``handoff`` к живому
менеджеру и оставить сводку — а не уронить обработку сообщения и не «съесть»
клиента молча.

Отдельно фиксируем поведение песочницы: там при недоступном AI осознанно
отдаётся 503 (01 §8.6), чтобы админ не принял handoff-заглушку за рабочий
сценарий. Это разные контуры, и путать их нельзя.
"""

# Фикстуры стенда бота импортируются из tests/unit/test_bot_engine.py; ruff
# видит в одноимённых параметрах теста переопределение импорта — для pytest это
# штатный способ переиспользовать чужие фикстуры.
# ruff: noqa: F811

import pytest

from app.bots import ai as ai_mod
from app.bots import sandbox as sandbox_mod
from app.bots.scenarios import default_scenario
from app.core.config import settings
from app.integrations import gateway

# Мир бота (аккаунт + бот + диалог + «клиент пишет») уже собран в тестах движка:
# импортируем и хелперы, и сами фикстуры (импорт = регистрация в этом модуле),
# чтобы приёмка гоняла ровно тот же стенд, а не свою копию.
from tests.unit.test_bot_engine import (  # noqa: F401 — chat/make_* нужны как фикстуры
    DAY,
    bot_messages,
    chat,
    handoff_of,
    load_conv,
    make_bot,
    make_world,
    notes,
    world_ctx,
)


@pytest.fixture
def no_ai_key(monkeypatch):
    """Боевой модуль AI без ключа — ровно то, что сейчас на проде."""
    monkeypatch.setattr(settings, "ai_fake", False, raising=False)
    monkeypatch.setitem(gateway.known_keys, "anthropic", False)
    ai_mod.reset_client()
    ai_mod.reset_breaker()
    yield ai_mod
    ai_mod.reset_client()
    ai_mod.reset_breaker()


async def test_ai_module_without_key_reports_itself_unavailable(no_ai_key):
    assert no_ai_key.is_available() is False
    assert no_ai_key.get_client() is None
    # ключевой контракт: None, а не исключение (вызывающий обязан сделать handoff)
    dialog = [{"role": "user", "content": "привет"}]
    assert await no_ai_key.ai_answer(object(), dialog, None) is None
    assert await no_ai_key.classify_message(["всё сломано"]) is None
    assert await no_ai_key.extract_entities(["телефон 89260001122"]) is None


async def test_dialog_without_ai_key_ends_in_handoff_with_summary(
    db_sessionmaker,
    world_ctx,
    make_world,
    chat,
    no_ai_key,
):
    """UAT §5 п.7 без ключа: клиента забирает человек, сводка на месте."""
    world = await make_world(default_scenario())
    client = chat(world_ctx(ai=no_ai_key, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Не морозит холодильник Bosch")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_unavailable"
    assert conv.bot_active is False
    assert conv.status == "new"  # диалог в общей очереди, а не в чёрной дыре
    assert conv.assignee_id is None
    summaries = await notes(db_sessionmaker, world.conversation_id)
    assert any("AI недоступен" in n for n in summaries), summaries
    # бот успел поздороваться и спросить — клиент не остался без ответа
    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) >= 1


async def test_second_message_after_handoff_does_not_wake_the_bot(
    db_sessionmaker,
    world_ctx,
    make_world,
    chat,
    no_ai_key,
):
    """После handoff бот молчит навсегда — повторный вопрос его не будит."""
    world = await make_world(default_scenario())
    client = chat(world_ctx(ai=no_ai_key, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Не морозит холодильник")
    before = len(await bot_messages(db_sessionmaker, world.conversation_id))
    await client.says("Ну так что?")
    after = len(await bot_messages(db_sessionmaker, world.conversation_id))
    assert after == before, "бот заговорил после передачи менеджеру"


async def test_sandbox_refuses_real_mode_without_key(redis, no_ai_key):
    """Песочница в режиме `real` без ключа — честный отказ, а не тихий handoff."""
    with pytest.raises(sandbox_mod.AIUnavailable):
        await sandbox_mod.start(
            redis,
            "uat7-admin",
            scenario=default_scenario(),
            knowledge_base="Ремонт бытовой техники",
            schedule={"always": True},
            client_name="Пётр",
            item_title="Ремонт холодильника",
            now_override="2026-08-05T14:00:00+03:00",
            ai_mode="real",
        )
