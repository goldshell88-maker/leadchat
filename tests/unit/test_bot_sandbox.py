"""Песочница сценария — 02 §5.3, 01 §8.6, экран 11 §5.4.

Смысл песочницы в том, что она исполняет **тот же** `ScenarioEngine`, что и
прод. Поэтому тесты проверяют не «поведение песочницы», а два её собственных
обязательства:

1. **Ничего не пишется в БД** — ни диалога, ни сообщений, ни журнала.
2. **Состояние живёт в Redis** с TTL 1 час и переживает отдельные HTTP-запросы
   (каждый тик здесь идёт в своей сессии БД — как в реальном запросе).

Плюс сквозной прогон «Первичного приёма» в режиме `stub`: он же — чек-лист
админа перед включением бота (02 §5.3).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app.bots import sandbox as sb
from app.bots.runtime import ENTRY_BLOCKS
from app.bots.scenarios import default_scenario
from app.models import AuditLog, Client, Conversation, Message
from app.services.messages import MAX_TEXT_LENGTH

ADMIN = "11111111-1111-1111-1111-111111111111"
OTHER_ADMIN = "22222222-2222-2222-2222-222222222222"
KB = "Замена экрана iPhone 13 — от 8900 ₽, срок 1–2 часа."

NIGHT = "2026-08-04T22:30:00+03:00"  # вторник, ночь по Москве
DAY = "2026-08-04T12:00:00+03:00"


@pytest.fixture
def tick(db_sessionmaker, redis):
    """Один «HTTP-запрос» песочницы: своя сессия БД на каждый тик."""

    async def _message(session_id: str, text: str, admin: str = ADMIN) -> dict[str, Any]:
        async with db_sessionmaker() as db:
            return await sb.message(redis, db, admin, session_id, text)

    return _message


@pytest.fixture
def timeout_tick(db_sessionmaker, redis):
    async def _fire(session_id: str, admin: str = ADMIN) -> dict[str, Any]:
        async with db_sessionmaker() as db:
            return await sb.fire_timeout(redis, db, admin, session_id)

    return _fire


@pytest.fixture
async def night_session(redis) -> AsyncIterator[sb.SandboxSession]:
    session, _ = await sb.start(
        redis,
        ADMIN,
        scenario=default_scenario(),
        knowledge_base=KB,
        client_name="Иван",
        item_title="Ремонт iPhone",
        now_override=NIGHT,
        ai_mode="stub",
    )
    yield session


def kinds(tick: dict[str, Any]) -> list[str]:
    return [e["kind"] for e in tick["events"]]


def texts(tick: dict[str, Any], kind: str = "bot_message") -> list[str]:
    return [e["text"] for e in tick["events"] if e["kind"] == kind]


def skip(reason: str) -> dict[str, Any]:
    """Отказ входа: подпись редактор не сочиняет, она приходит с сервера."""
    return {"kind": "skipped", "reason": reason, "label": ENTRY_BLOCKS[reason]}


# --- сквозной прогон «Первичного приёма» (02 §5.3) ---------------------------


async def test_night_path_of_primary_intake_end_to_end(night_session, tick):
    """Ночь: приветствие → проблема → AI → ветка «ночь» → телефон → handoff."""
    first = await tick(night_session.session_id, "Разбил экран айфона 13")
    assert first["trace"] == ["greet", "ask_problem"]
    assert "Здравствуйте, Иван" in texts(first)[0]  # подстановка {client_name}
    assert first["state"]["waiting"]["var"] == "problem"
    assert first["state"]["bot_active"] is True

    second = await tick(night_session.session_id, "не включается после падения")
    assert second["trace"] == ["ai_draft", "check_hours", "night_msg", "ask_phone"]
    assert sb.STUB_REPLY in texts(second)  # заглушка AI, живых вызовов нет
    ai_call = next(e for e in second["events"] if e["kind"] == "ai_call")
    assert ai_call["confidence"] == sb.STUB_CONFIDENCE
    assert "Оставьте телефон" in texts(second)[-1]
    assert second["state"]["vars"]["problem"] == "не включается после падения"
    assert second["state"]["waiting"]["var"] == "phone"

    invalid = await tick(night_session.session_id, "не скажу")
    assert "Кажется, это не номер" in texts(invalid)[0]  # retry_text
    assert invalid["state"]["waiting"]["attempts"] == 1

    final = await tick(night_session.session_id, "+7 916 123-45-67")
    assert final["trace"] == ["tag_contact", "note_contact", "handoff_night"]
    assert final["state"]["vars"]["phone"] == "+79161234567"  # нормализован
    assert "контакт собран" in final["state"]["tags"]
    assert "🤖 Бот собрал контакт: +79161234567" in texts(final, "note")[0]
    handoff = next(e for e in final["events"] if e["kind"] == "handoff")
    assert handoff["reason"] == "scenario"
    assert final["state"]["handoff"]["step"] == "handoff_night"
    assert final["state"]["bot_active"] is False
    # решение владельца: бот не закрывает диалог — он возвращает его в очередь
    assert final["state"]["status"] == "new"


async def test_day_path_hands_off_right_after_the_ai_answer(redis, tick):
    session, _ = await sb.start(
        redis, ADMIN, scenario=default_scenario(), knowledge_base=KB, now_override=DAY
    )
    await tick(session.session_id, "Здравствуйте")
    day = await tick(session.session_id, "не заряжается ноутбук")
    assert day["trace"] == ["ai_draft", "check_hours", "handoff_day"]
    assert day["state"]["handoff"]["reason"] == "scenario"
    assert "первичный-приём" in day["state"]["tags"]


async def test_now_override_switches_the_work_hours_branch(redis, tick):
    """«Время: задать» — тот самый тумблер, ради которого песочница и нужна."""
    night, _ = await sb.start(
        redis, ADMIN, scenario=default_scenario(), knowledge_base=KB, now_override=NIGHT
    )
    await tick(night.session_id, "привет")
    assert "night_msg" in (await tick(night.session_id, "разбит экран"))["trace"]

    day, _ = await sb.start(
        redis, ADMIN, scenario=default_scenario(), knowledge_base=KB, now_override=DAY
    )
    await tick(day.session_id, "привет")
    assert "handoff_day" in (await tick(day.session_id, "разбит экран"))["trace"]


# --- промотка таймаута (кнопка «⏩», 02 §5.3) --------------------------------


async def test_fire_timeout_follows_the_on_timeout_branch(night_session, tick, timeout_tick):
    """15.08: первый таймаут — ПИНГ молчащему и повторное ожидание (регламент:
    «5 минут тишины → уточняющий вопрос»); второй — прежний handoff."""
    await tick(night_session.session_id, "Здравствуйте")  # бот ждёт `problem`
    fired = await timeout_tick(night_session.session_id)

    assert kinds(fired)[0] == "timeout"
    assert fired["trace"] == ["ping_problem", "ask_problem_2"]

    fired2 = await timeout_tick(night_session.session_id)
    assert fired2["trace"] == ["handoff_no_reply"]
    # автозакрытия по таймауту нет (решение владельца): диалог в очереди
    assert fired2["state"]["status"] == "new"
    assert fired2["state"]["handoff"]["step"] == "handoff_no_reply"
    assert "close" not in kinds(fired)


async def test_fire_timeout_is_a_noop_when_the_bot_is_not_waiting(night_session, timeout_tick):
    idle = await timeout_tick(night_session.session_id)
    assert idle["events"] == [{"kind": "skipped", "reason": "not_waiting"}]
    assert idle["trace"] == []


async def test_second_ask_timeout_goes_to_the_night_queue(night_session, tick, timeout_tick):
    await tick(night_session.session_id, "привет")
    await tick(night_session.session_id, "разбит экран")  # бот ждёт телефон
    fired = await timeout_tick(night_session.session_id)
    assert fired["trace"] == ["ping_phone", "ask_phone_2"]  # 15.08: сначала пинг
    fired2 = await timeout_tick(night_session.session_id)
    assert fired2["trace"] == ["handoff_night"]
    assert "ночной-лид" in fired2["state"]["tags"]


# --- шесть условий handoff за минуту (чек-лист админа, 02 §5.3) --------------


async def test_client_asking_for_a_human_is_handed_off_immediately(night_session, tick):
    await tick(night_session.session_id, "привет")
    asked = await tick(night_session.session_id, "позовите оператора")
    assert {"kind": "detector", "detector": "human_request"} in asked["events"]
    assert asked["state"]["handoff"]["reason"] == "client_request"
    assert asked["trace"] == []  # сценарий дальше не идёт


async def test_negative_message_tags_the_dialog(night_session, tick):
    """Заглушка классификатора реагирует на «ужасно» (02 §5.3)."""
    await tick(night_session.session_id, "привет")
    angry = await tick(night_session.session_id, "ужасный сервис, верну деньги через Авито")
    assert "негатив" in angry["state"]["tags"]
    assert angry["state"]["handoff"] is not None


# --- обязательство №1: в БД не остаётся ничего -------------------------------


async def test_sandbox_writes_nothing_to_the_database(night_session, tick, db_sessionmaker):
    await tick(night_session.session_id, "Разбил экран")
    await tick(night_session.session_id, "iPhone 13, экран")
    await tick(night_session.session_id, "+7 916 123-45-67")

    async with db_sessionmaker() as db:
        for model in (Conversation, Message, Client, AuditLog):
            total = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            assert total == 0, f"песочница оставила строки в {model.__tablename__}"


# --- обязательство №2: состояние в Redis -------------------------------------


async def test_session_lives_in_redis_with_an_hour_ttl(night_session, redis):
    key = sb.session_key(ADMIN, night_session.session_id)
    assert key.startswith("sandbox:")
    assert await redis.exists(key)
    assert 0 < await redis.ttl(key) <= sb.SESSION_TTL_SECONDS


async def test_every_tick_prolongs_the_ttl_and_stores_the_state(night_session, tick, redis):
    key = sb.session_key(ADMIN, night_session.session_id)
    await redis.expire(key, 5)
    await tick(night_session.session_id, "Разбил экран")

    assert await redis.ttl(key) > 5  # сессия «живая», пока админ тестирует
    stored = json.loads(await redis.get(key))
    assert stored["bot_vars"]["step"] == "ask_problem"
    assert stored["bot_active"] is True
    assert stored["dialog"][0] == {
        "direction": "in",
        "sender_type": "client",
        "body": "Разбил экран",
    }
    assert stored["dialog"][1]["sender_type"] == "bot"


async def test_state_panel_matches_the_documented_shape(night_session, tick):
    first = await tick(night_session.session_id, "Разбил экран")
    assert set(first["state"]) >= {"step", "waiting", "vars", "counters"}
    assert first["state"]["counters"]["steps_total"] == 2
    assert first["state"]["counters"]["bot_msgs_row"] == 1


async def test_sessions_of_different_admins_are_isolated(night_session, tick):
    """Ключ включает `admin_id` — чужую сессию не открыть (02 §5.3)."""
    with pytest.raises(sb.SessionNotFound):
        await tick(night_session.session_id, "привет", OTHER_ADMIN)


async def test_stop_deletes_the_session(night_session, redis, tick):
    await sb.stop(redis, ADMIN, night_session.session_id)
    assert not await redis.exists(sb.session_key(ADMIN, night_session.session_id))
    with pytest.raises(sb.SessionNotFound):
        await tick(night_session.session_id, "привет")
    with pytest.raises(sb.SessionNotFound):
        await sb.stop(redis, ADMIN, night_session.session_id)


async def test_expired_session_is_not_found(night_session, redis, tick):
    await redis.delete(sb.session_key(ADMIN, night_session.session_id))
    with pytest.raises(sb.SessionNotFound):
        await tick(night_session.session_id, "привет")


# --- расписание бота (02 §2.5, фильтр на вход) -------------------------------

NIGHT_ONLY = {
    "always": False,
    "timezone": "Europe/Moscow",
    "intervals": [
        {
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "start": "20:00",
            "end": "10:00",
        }
    ],
}


async def test_bot_does_not_start_outside_its_schedule(redis, tick):
    session, _ = await sb.start(
        redis,
        ADMIN,
        scenario=default_scenario(),
        knowledge_base=KB,
        schedule=NIGHT_ONLY,
        now_override=DAY,  # 12:00 — вне интервала 20:00–10:00
    )
    skipped = await tick(session.session_id, "Здравствуйте")
    assert skipped["events"] == [skip("not_scheduled")]
    assert skipped["state"]["bot_active"] is False


async def test_bot_starts_inside_its_schedule(redis, tick):
    session, _ = await sb.start(
        redis,
        ADMIN,
        scenario=default_scenario(),
        knowledge_base=KB,
        schedule=NIGHT_ONLY,
        now_override=NIGHT,
    )
    started = await tick(session.session_id, "Здравствуйте")
    assert started["trace"] == ["greet", "ask_problem"]


async def test_started_dialog_is_finished_even_outside_the_schedule(redis, tick):
    """02 §2.5: расписание — фильтр на ВХОД; начатый диалог доводится до конца."""
    session, _ = await sb.start(
        redis,
        ADMIN,
        scenario=default_scenario(),
        knowledge_base=KB,
        schedule=NIGHT_ONLY,
        now_override=NIGHT,
    )
    await tick(session.session_id, "Здравствуйте")

    stored = await sb.load_session(redis, ADMIN, session.session_id)
    stored.now_override = DAY  # клиент ответил утром, уже вне расписания
    await sb.save_session(redis, stored)

    morning = await tick(session.session_id, "разбит экран")
    assert morning["trace"], "начатый сценарий обязан продолжиться вне расписания"


# --- честность песочницы: вход бота решает та же функция, что в проде --------
#
# Раньше песочница проверяла только расписание и только на первом сообщении
# сессии, поэтому показывала админу поведение, которого в проде не бывает.

CLOSING_SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "bye",
    "steps": [{"id": "bye", "type": "close", "params": {"text": "До свидания", "silent": False}}],
}


async def test_after_handoff_the_bot_does_not_touch_the_dialog_again(redis, tick):
    """В проде `handoff` закрывает вход навсегда — песочница обязана так же."""
    session, _ = await sb.start(
        redis, ADMIN, scenario=default_scenario(), knowledge_base=KB, now_override=DAY
    )
    await tick(session.session_id, "привет")
    handed = await tick(session.session_id, "не заряжается ноутбук")
    assert handed["state"]["handoff"] is not None

    after = await tick(session.session_id, "а когда приедет мастер?")
    assert after["events"] == [skip("handoff_done")]
    assert after["trace"] == []
    # счётчик «мимо сценария» не должен шевелиться: сценарий не исполнялся
    assert after["state"]["counters"]["offscript_msgs"] == 0


async def test_after_close_the_schedule_is_checked_again(redis, tick):
    """Вернувшийся клиент — новый вход бота, а значит снова фильтр расписания."""
    session, _ = await sb.start(
        redis, ADMIN, scenario=CLOSING_SCENARIO, schedule=NIGHT_ONLY, now_override=NIGHT
    )
    closed = await tick(session.session_id, "привет")
    assert closed["state"]["status"] == "closed"

    stored = await sb.load_session(redis, ADMIN, session.session_id)
    stored.now_override = DAY  # вернулся днём, бот работает только ночью
    await sb.save_session(redis, stored)

    daytime = await tick(session.session_id, "я вернулся")
    assert daytime["events"] == [skip("not_scheduled")]
    assert daytime["trace"] == []


async def test_client_returning_inside_the_schedule_restarts_the_scenario(redis, tick):
    """Обратная сторона: в расписании закрытый диалог переоткрывается (02 §1.3)."""
    session, _ = await sb.start(
        redis, ADMIN, scenario=CLOSING_SCENARIO, schedule=NIGHT_ONLY, now_override=NIGHT
    )
    await tick(session.session_id, "привет")
    again = await tick(session.session_id, "я вернулся")
    assert again["trace"] == ["bye"]
    assert texts(again) == ["До свидания"]


async def test_trace_shows_exactly_what_the_client_would_receive(redis, tick):
    """Длинный ответ обрезан одинаково и для клиента, и для панели админа."""
    scenario: dict[str, Any] = {
        "version": 1,
        "revision": 1,
        "entry": "shout",
        "steps": [
            {
                "id": "shout",
                "type": "send",
                "params": {"text": "я" * (MAX_TEXT_LENGTH + 100)},
                "next": "done",
            },
            {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }
    session, _ = await sb.start(redis, ADMIN, scenario=scenario)
    shown = await tick(session.session_id, "привет")
    assert [len(t) for t in texts(shown)] == [MAX_TEXT_LENGTH]


async def test_session_from_a_previous_release_does_not_crash_the_sandbox(redis, tick):
    """Сессия живёт час и переживает выкладку: лишний ключ — не 500."""
    session, _ = await sb.start(redis, ADMIN, scenario=default_scenario(), knowledge_base=KB)
    key = sb.session_key(ADMIN, session.session_id)
    stored = json.loads(await redis.get(key))
    stored["started"] = True  # поле прошлой версии
    await redis.set(key, json.dumps(stored, ensure_ascii=False))

    revived = await tick(session.session_id, "Здравствуйте")
    assert revived["trace"] == ["greet", "ask_problem"]


# --- режимы AI ---------------------------------------------------------------


async def test_stub_ai_is_deterministic_and_offline():
    stub = sb.ai_backend("stub")
    assert isinstance(stub, sb.StubAI)
    answer = await stub.ai_answer(None, [], None)
    assert answer == {
        "reply": sb.STUB_REPLY,
        "confidence": sb.STUB_CONFIDENCE,
        "needs_operator": False,
    }
    assert (await stub.classify_message(["ужасный сервис"]))["sentiment"] == "negative"
    assert (await stub.classify_message(["спасибо!"]))["sentiment"] == "neutral"
    assert (await stub.classify_message(["дайте оператора"]))["wants_human"] is True


async def test_real_mode_without_the_ai_module_is_a_sandbox_error(monkeypatch):
    """`ai_mode="real"` без `app.bots.ai` — честный 503, а не тихая заглушка."""

    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(sb.importlib, "import_module", missing)
    with pytest.raises(sb.AIUnavailable):
        sb.ai_backend("real")


async def test_start_reports_ai_unavailability_before_the_first_message(redis, monkeypatch):
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(sb.importlib, "import_module", missing)
    with pytest.raises(sb.AIUnavailable):
        await sb.start(redis, ADMIN, scenario=default_scenario(), ai_mode="real")


# --- события для ленты (02 §5.3, 11 §5.4) ------------------------------------


async def test_events_cover_the_documented_kinds(night_session, tick):
    seen: set[str] = set()
    for text in ("Разбил экран", "iPhone 13", "не скажу", "+7 916 123-45-67"):
        seen |= set(kinds(await tick(night_session.session_id, text)))
    assert {"step", "waiting", "bot_message", "ai_call", "tags", "note", "handoff"} <= seen
