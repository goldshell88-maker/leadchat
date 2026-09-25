"""Лид-бот как поставщик ответов на шаге `ai_answer` (docs/42).

Три инварианта, ради которых этот файл существует.

**Первый: ни один тест не ходит в сеть.** HTTP подменяется `respx`, ровно как в
`test_avito_oauth.py`. Лид-бот живёт на чужом сервере, и набор, который к нему
стучится, однажды покраснеет не от нашей ошибки.

**Второй: `None` — штатный ответ, а не исключение.** Таймаут, 401, 500, не-JSON,
JSON не той формы — движок обязан получить одинаковый `None` и уйти в handoff.
Это решение владельца №4: недоступность ИИ НИКОГДА не блокирует доставку
сообщений. Последний тест файла проверяет именно доставку, а не форму ответа.

**Третий: токен не течёт.** Ни в `repr`, ни в журнал, ни в то, что отдаётся
наружу. Проверка идёт по всем путям отказа сразу — потому что течёт секрет
обычно не в успешном сценарии, а в трассировке.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
import structlog
from sqlalchemy import select

from app.bots import leadbot, provider
from app.bots.runtime import bot_step
from app.core.config import settings
from app.models import AvitoAccount, Bot, Client, Conversation, Message

URL = "http://10.10.0.2:8788"
ENDPOINT = URL + leadbot.ANSWER_PATH
#: Латиница намеренно: заголовки HTTP — latin-1, и токен с кириллицей роняет
#: запрос ещё до сети (см. `test_a_cyrillic_token_is_refused_at_save_time`).
TOKEN = "s3rv1ce-token-of-the-leadbot-0123456789"

#: Ответ, который лид-бот отдаёт по контракту (docs/42 §3).
GOOD = {
    "reply": "Замена экрана iPhone 13 — ориентировочно от 8900 ₽, точнее после диагностики.",
    "confidence": 0.9,
    "needs_operator": False,
    "meta": {"layer": "модель", "ms": 1200},
}

DIALOG = [{"role": "user", "content": "Разбил экран на айфоне 13, сколько будет?"}]
BOT = SimpleNamespace(id=uuid.uuid4(), knowledge_base="", ai_provider=provider.LEADBOT)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш процесса живёт между тестами и подсунул бы чужие адрес и токен."""
    leadbot._drop_cache()
    yield
    leadbot._drop_cache()


def _ai() -> leadbot.LeadbotAI:
    return leadbot.LeadbotAI(URL, TOKEN)


# =============================================================================
# Провайдер: успешный ответ
# =============================================================================


@respx.mock
async def test_successful_answer_reaches_the_engine_contract() -> None:
    """Живой ответ лид-бота превращается в то, что умеет разбирать движок."""
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    answer = await _ai().ai_answer(BOT, DIALOG, "Ремонт iPhone")

    assert answer == {
        "reply": GOOD["reply"],
        "confidence": 0.9,
        "needs_operator": False,
    }
    # ⚠ 26.08: `meta` отдаётся, но ОДНИМ полем — причиной передачи. Правило «лишних ключей
    # в контракте шага быть не должно» осталось: в `meta` лежит `lead` с телефоном и адресом
    # клиента, и целиком её отдавать нельзя. Здесь передачи нет вовсе, значит нет и ключа.
    assert "meta" not in answer

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    sent = request.read().decode()
    assert "Разбил экран" in sent
    assert "Ремонт iPhone" in sent


@respx.mock
async def test_свои_подсказки_и_звонки_уезжают_боту() -> None:
    """Два поля сверх переписки (24.08): что я уже предлагал и сколько раз клиент звонил.

    В режиме подсказки ответ бота уходит заметкой, а история для него собирается только
    из in/out — своих слов он не видел вовсе, и все анти-повторные правила считали по
    репликам ОПЕРАТОРА. Звонок лежит служебной записью и в переписку не входит.
    Оба поля едут ОТДЕЛЬНО от dialog: клиент подсказок не видел, и выдавать их за
    переписку значит убедить бота, что он уже поздоровался.
    """
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    await _ai().ai_answer(
        BOT,
        DIALOG,
        "Ремонт iPhone",
        prior_suggestions=["И номер ваш подскажите", "  ", "Адрес подскажете?"],
        calls=2,
    )

    sent = json.loads(route.calls.last.request.read().decode())
    assert sent["prior_suggestions"] == ["И номер ваш подскажите", "Адрес подскажете?"]
    assert sent["events"] == [{"type": "call"}, {"type": "call"}]
    # ⚠ и НЕ в переписке: dialog остался тем, что клиент видел
    assert all("номер ваш подскажите" not in str(m) for m in sent["dialog"])


@respx.mock
async def test_без_новых_полей_запрос_прежний() -> None:
    """Поля необязательные: панель, которая их не шлёт, работает как раньше."""
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    await _ai().ai_answer(BOT, DIALOG, "Ремонт iPhone")

    sent = json.loads(route.calls.last.request.read().decode())
    assert sent["prior_suggestions"] == []
    assert sent["events"] == []


@respx.mock
async def test_request_id_is_stable_for_a_repeat_and_changes_with_the_dialog() -> None:
    """Ключ идемпотентности: тот же диалог — тот же ключ, новая реплика — новый.

    Лид-бот держит по нему кэш ответов десять минут. Если ключ будет случайным,
    задвоенный балансировщиком POST спишет с владельца второй вызов модели и
    может отправить клиенту второй ответ; если ключ будет вечным — клиент на
    новое сообщение получит старый ответ.
    """
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))
    ai = _ai()

    await ai.ai_answer(BOT, DIALOG, "Ремонт iPhone")
    await ai.ai_answer(BOT, DIALOG, "Ремонт iPhone")
    await ai.ai_answer(BOT, [*DIALOG, {"role": "user", "content": "Так сколько?"}], "Ремонт iPhone")

    first, repeat, after_new_message = (
        json.loads(call.request.read())["request_id"] for call in respx.calls
    )
    assert first == repeat
    assert after_new_message != first


async def test_empty_dialog_never_leaves_the_process() -> None:
    """Спрашивать нечего — не спрашиваем.

    Лид-бот на пустой `dialog` отвечает ошибкой разбора, то есть мы бы заплатили
    сетевым вызовом за заведомо известный результат.
    """
    with respx.mock:
        route = respx.post(ENDPOINT)
        assert await _ai().ai_answer(BOT, [], "Ремонт iPhone") is None
        assert not route.called


# =============================================================================
# Провайдер: отказ. Всё одинаково — `None`
# =============================================================================


@respx.mock
async def test_timeout_is_none_and_not_an_exception() -> None:
    """Главный тест файла: тайм-аут не имеет права выйти наружу исключением.

    Выйди он — упал бы весь тик обработки входящего, а вместе с ним доставка
    сообщений в диалоге (решение владельца №4).
    """
    respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("нет ответа за 10 с"))

    assert await _ai().ai_answer(BOT, DIALOG, None) is None


@respx.mock
async def test_slow_answer_is_cut_by_our_own_budget() -> None:
    """Лид-бот молчит дольше бюджета шага — ждём не дольше своего секундомера.

    Бюджет здесь искусственно короткий: настоящие десять секунд в наборе юнитов
    означали бы десять секунд на прогон.
    """

    async def _too_slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=GOOD)

    respx.post(ENDPOINT).mock(side_effect=_too_slow)

    ai = leadbot.LeadbotAI(URL, TOKEN, budget_seconds=0.05)
    started = asyncio.get_running_loop().time()
    assert await ai.ai_answer(BOT, DIALOG, None) is None
    assert asyncio.get_running_loop().time() - started < 2.0


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404, 500, 502])
async def test_any_bad_status_is_none(status: int) -> None:
    """401 «не пустили» и 500 «упал» для движка одно и то же — человек."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(status, json={"error": "нет"}))

    assert await _ai().ai_answer(BOT, DIALOG, None) is None


@respx.mock
@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("не объект", [1, 2, 3]),
        ("пустой объект", {}),
        ("reply не строка", {"reply": 123, "confidence": 0.9, "needs_operator": False}),
        ("reply отсутствует", {"confidence": 0.9, "needs_operator": False}),
        ("confidence словами", {"reply": "о", "confidence": "высокая", "needs_operator": False}),
        # `bool` — подкласс `int`, и без отдельной проверки `true` проехал бы как 1.0.
        ("confidence булев", {"reply": "ответ", "confidence": True, "needs_operator": False}),
        ("needs_operator строкой", {"reply": "ответ", "confidence": 0.9, "needs_operator": "yes"}),
        ("needs_operator отсутствует", {"reply": "ответ", "confidence": 0.9}),
    ],
)
async def test_garbage_in_the_answer_is_none(name: str, payload: Any) -> None:
    """Мусор не проходит. Снисходительность здесь дороже строгости.

    Пропустив `reply=None`, мы отправили бы клиенту пустое сообщение; пропустив
    `confidence="высокая"` и посчитав её нулём — отдали бы человеку готовый
    хороший ответ. Оба исхода выглядят как «бот сломался», а искали бы их в
    движке, до которого мусор и доехал.
    """
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    assert await _ai().ai_answer(BOT, DIALOG, None) is None, name


@respx.mock
async def test_not_json_at_all_is_none() -> None:
    """Впереди встал прокси и отдал HTML — это не ответ бота, а его отсутствие."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, text="<html>502 Bad Gateway</html>"))

    assert await _ai().ai_answer(BOT, DIALOG, None) is None


@respx.mock
async def test_empty_reply_with_needs_operator_is_a_valid_answer() -> None:
    """«Молча человеку» — это ОТВЕТ лид-бота, а не поломка.

    Его `unavailable()` отдаёт ровно такую форму, когда сам не успел. Считать её
    мусором значило бы стереть разницу между «бот решил позвать человека» и
    «бот недоступен» — а именно эта разница объясняет диспетчеру, почему диалог
    у него на руках.
    """
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"reply": "", "confidence": 0.0, "needs_operator": True, "meta": {}},
        )
    )

    assert await _ai().ai_answer(BOT, DIALOG, None) == {
        "reply": "",
        "confidence": 0.0,
        "needs_operator": True,
    }


@respx.mock
async def test_confidence_is_clamped_to_the_engine_scale() -> None:
    """Шкала уверенности у движка 0..1: чужие 5.0 не должны её ломать."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"reply": "ответ", "confidence": 5.0, "needs_operator": False}
        )
    )

    answer = await _ai().ai_answer(BOT, DIALOG, None)
    assert answer is not None and answer["confidence"] == 1.0


# =============================================================================
# Секрет: не в журнале, не в repr, не наружу
# =============================================================================


@respx.mock
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(200, json=GOOD),
        httpx.Response(401, json={"error": "нужен служебный токен"}),
        httpx.Response(500, text="упал"),
        httpx.Response(200, text="не json"),
        httpx.Response(200, json={"reply": None}),
    ],
)
async def test_token_never_reaches_the_log(outcome: httpx.Response) -> None:
    """Токен не пишется НИ НА ОДНОМ пути, включая пути отказа.

    Течёт секрет обычно не в успешном сценарии: там его просто не за чем
    печатать. Течёт он в диагностике, которую дописывают в три часа ночи, — в
    `exc_info`, в «на всякий случай залогируем payload», в `repr` объекта.
    """
    if isinstance(outcome, httpx.Response):
        respx.post(ENDPOINT).mock(return_value=outcome)

    with structlog.testing.capture_logs() as captured:
        await _ai().ai_answer(BOT, DIALOG, "Ремонт iPhone")

    assert TOKEN not in repr(captured)


@respx.mock
async def test_token_never_reaches_the_log_on_a_network_error() -> None:
    """То же самое, когда исключение поднял сам httpx: в тексте бывает адрес."""
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("connection refused"))

    with structlog.testing.capture_logs() as captured:
        assert await _ai().ai_answer(BOT, DIALOG, None) is None

    assert TOKEN not in repr(captured)


def test_repr_hides_the_token() -> None:
    """`repr` уезжает в трассировку, трассировка — в Sentry."""
    text = repr(_ai())

    assert TOKEN not in text
    assert "задан" in text  # но факт наличия видно — иначе отладка вслепую


def test_config_repr_hides_the_token() -> None:
    """У dataclass'а `repr` печатается сам, и по умолчанию — со всеми полями."""
    text = repr(leadbot.LeadbotConfig(url=URL, token=TOKEN, source="db"))

    assert TOKEN not in text
    assert URL in text


@respx.mock
async def test_lead_values_do_not_reach_the_log_only_field_names() -> None:
    """В `meta.lead` лежит телефон клиента, а журнал читают шире переписки."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                **GOOD,
                "meta": {"lead_ready": True, "lead": {"phone": "+79161234567", "name": "Иван"}},
            },
        )
    )

    with structlog.testing.capture_logs() as captured:
        await _ai().ai_answer(BOT, DIALOG, None)

    dumped = repr(captured)
    assert "+79161234567" not in dumped
    assert "phone" in dumped  # имя поля видно: по нему понятно, что телефон собран


# =============================================================================
# Настройка: база, запас из окружения, секрет наружу не отдаётся
# =============================================================================


async def test_env_is_the_fallback_until_the_db_has_a_row(db, monkeypatch) -> None:
    """Пока строки в базе нет, действует то, что задал инженер при развёртывании."""
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)

    config = await leadbot.load(db)

    assert (config.url, config.token, config.source) == (URL, TOKEN, "env")
    assert config.is_ready


async def test_db_overrides_env(db, monkeypatch) -> None:
    """Владелец переопределяет инженера, не заходя на сервер."""
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", "stale-token-from-the-env-file", raising=False)

    await leadbot.save(
        db, actor_id=None, url="https://bot.example.org/", token="fresh-token-from-the-screen"
    )
    await db.commit()

    config = await leadbot.load(db)
    assert config.url == "https://bot.example.org"  # хвостовой слэш снят
    assert config.token == "fresh-token-from-the-screen"
    assert config.source == "db"


async def test_saving_only_the_url_keeps_the_token(db) -> None:
    """Экран не показывает токен, поэтому при правке адреса он приходит пустым.

    Без отдельного значения «не меняли» каждая правка адреса обнуляла бы токен,
    и автоответ переставал бы работать по причине, которую никто не свяжет с
    последним действием.
    """
    await leadbot.save(db, actor_id=None, url=URL, token=TOKEN)
    await leadbot.save(db, actor_id=None, url="https://bot.example.org", token=None)
    await db.commit()

    assert (await leadbot.load(db)).token == TOKEN


async def test_a_bad_address_is_refused_before_it_reaches_the_dialogs(db) -> None:
    """Адрес без схемы — это отказ на каждом диалоге, и узнать о нём надо сразу."""
    with pytest.raises(ValueError, match="http"):
        await leadbot.save(db, actor_id=None, url="10.10.0.2:8788", token=TOKEN)


async def test_a_cyrillic_token_is_refused_at_save_time(db) -> None:
    """Найдено этим же набором, а не придумано.

    Заголовки HTTP — latin-1: токен с кириллицей роняет запрос ещё до сети, и
    провайдер честно отдаёт `None`. Снаружи это выглядит как «лид-бот
    недоступен» на КАЖДОМ диалоге, и искать причину человек пойдёт в сеть и в
    чужой сервер. Между тем повод бытовой: русская «с» вместо латинской.
    """
    with pytest.raises(ValueError, match="латиниц"):
        await leadbot.save(db, actor_id=None, url=URL, token="секретный-токен")


@respx.mock
async def test_a_cyrillic_token_that_slipped_through_still_only_costs_a_handoff() -> None:
    """Через `.env` проверка не проходит — там её ставить некому.

    Значит провайдер обязан пережить и это: `None`, а не исключение наружу.
    """
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    assert await leadbot.LeadbotAI(URL, "секретный-токен").ai_answer(BOT, DIALOG, None) is None


async def test_reset_returns_to_what_the_engineer_set(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)

    await leadbot.save(db, actor_id=None, url="https://bot.example.org", token="another-token")
    await db.commit()
    await leadbot.reset(db)
    await db.commit()

    assert (await leadbot.load(db)).source == "env"


async def test_safe_dump_has_no_token(db) -> None:
    """Это то, что уходит на экран и в журнал. Токена здесь нет — никогда."""
    await leadbot.save(db, actor_id=None, url=URL, token=TOKEN)
    await db.commit()

    dumped = leadbot.safe_dump(await leadbot.load(db))

    assert TOKEN not in repr(dumped)
    assert dumped == {"url": URL, "token_set": True, "source": "db", "ready": True}


async def test_unreadable_token_is_not_silent(db, monkeypatch) -> None:
    """Ключ шифрования сменился при восстановлении копии — токен нечитаем.

    Молчать нельзя: снаружи это выглядит как «все диалоги вдруг уходят людям», а
    причина ровно здесь и ни в одном другом месте не видна.
    """
    monkeypatch.setattr(settings, "leadbot_token", "", raising=False)
    await leadbot.save(db, actor_id=None, url=URL, token=TOKEN)
    await db.commit()

    def _unreadable(_: bytes) -> str:
        raise leadbot.crypto.DecryptError

    monkeypatch.setattr(leadbot.crypto, "decrypt_token", _unreadable)

    with structlog.testing.capture_logs() as captured:
        config = await leadbot.load(db)

    assert config.token == ""
    assert not config.is_ready
    assert any(entry["event"] == "leadbot.token_unreadable" for entry in captured)


async def test_url_without_a_token_is_not_ready(db, monkeypatch) -> None:
    """Сеть — это транспорт, а не аутентификация: без токена нас не пустят."""
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", "", raising=False)

    assert not (await leadbot.load(db)).is_ready


# =============================================================================
# Маршрутизатор: кто отвечает
# =============================================================================


async def test_by_default_a_bot_still_answers_with_claude(monkeypatch) -> None:
    """Подключение лид-бота само по себе не меняет ничего.

    Это и есть цена вопроса: у лид-бота другой регламент, другие цены и другие
    формулировки, и переезд работающего бота на него обязан быть чьим-то
    решением, а не следствием того, что кто-то заполнил переменную окружения.
    """
    seen: list[str] = []

    async def _claude(bot, dialog, item_title, **_kw):  # noqa: ANN001, ANN202
        seen.append("claude")
        return {"reply": "наш ответ", "confidence": 0.9, "needs_operator": False}

    monkeypatch.setattr(provider.claude, "ai_answer", _claude)
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)

    default_bot = SimpleNamespace(id=uuid.uuid4())  # поля `ai_provider` нет вовсе
    plain_bot = SimpleNamespace(id=uuid.uuid4(), ai_provider=provider.CLAUDE)

    for bot in (default_bot, plain_bot):
        assert await provider.ai_answer(bot, DIALOG, None) is not None
    assert seen == ["claude", "claude"]


@respx.mock
async def test_leadbot_answers_only_when_it_is_chosen(monkeypatch) -> None:
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)

    async def _claude(bot, dialog, item_title):  # noqa: ANN001, ANN202
        raise AssertionError("бот переведён на лид-бота, Claude звать нельзя")

    monkeypatch.setattr(provider.claude, "ai_answer", _claude)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    answer = await provider.ai_answer(BOT, DIALOG, None)

    assert answer is not None and answer["reply"] == GOOD["reply"]
    assert route.called


async def test_chosen_but_not_configured_hands_over_instead_of_falling_back(monkeypatch) -> None:
    """Молчаливого отката на Claude нет, и это осознанно.

    Ответить вместо лид-бота нашим мозгом значит выдать клиенту цены и
    формулировки, которых владелец не согласовывал, — и не заметить этого.
    Передача человеку заметна и чинится настройкой.
    """
    monkeypatch.setattr(settings, "leadbot_url", "", raising=False)
    monkeypatch.setattr(settings, "leadbot_token", "", raising=False)

    async def _claude(bot, dialog, item_title):  # noqa: ANN001, ANN202
        raise AssertionError("лид-бот не настроен — это не повод отвечать за него")

    monkeypatch.setattr(provider.claude, "ai_answer", _claude)

    assert await provider.ai_answer(BOT, DIALOG, None) is None


def test_the_code_list_and_the_db_constraint_cannot_drift() -> None:
    """Охранный тест: `PROVIDERS`, CHECK в модели и CHECK в миграции — одно и то же.

    Литералы в ограничении выписаны руками намеренно (иначе содержимое уже
    применённой миграции зависело бы от версии приложения). Плата за это —
    возможность разъезда, и вот она закрыта: добавит кто-нибудь третьего
    поставщика в код и забудет миграцию — вставка будет падать в бою, а не здесь.
    """
    checks = [c for c in Bot.__table__.constraints if hasattr(c, "sqltext")]
    provider_check = [c for c in checks if "ai_provider IN" in str(c.sqltext)]
    assert provider_check, "с bots.ai_provider пропал CheckConstraint"

    assert set(re.findall(r"'(\w+)'", str(provider_check[0].sqltext))) == set(provider.PROVIDERS)
    assert provider.DEFAULT in provider.PROVIDERS
    # Значение по умолчанию — прежнее поведение, и это половина всей задачи.
    assert Bot.__table__.c.ai_provider.server_default.arg == provider.CLAUDE


async def test_the_classifier_stays_ours(monkeypatch) -> None:
    """Подключение автоответа не должно отнимать метку «негатив».

    У лид-бота ручки классификации нет. Перестань мы звать свою — диспетчеры
    молча потеряли бы и метку, и распознавание просьбы позвать человека, и
    связать потерю с включением автоответа было бы нечем.
    """
    called: list[list[str]] = []

    async def _classify(texts):  # noqa: ANN001, ANN202
        called.append(texts)
        return {"sentiment": "negative", "wants_human": False, "reason": "тест"}

    monkeypatch.setattr(provider.claude, "classify_message", _classify)

    assert await provider.classify_message(["вы издеваетесь"]) is not None
    assert called == [["вы издеваетесь"]]


# =============================================================================
# Сквозной: молчание лид-бота не блокирует доставку (решение владельца №4)
# =============================================================================

#: Сценарий с отправкой ДО шага `ai_answer` — чтобы было чему потеряться.
SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "greet",
    "settings": {"max_bot_messages_row": 5},
    "steps": [
        {
            "id": "greet",
            "type": "send",
            "params": {"text": "Здравствуйте! Опишите, что случилось с техникой."},
            "next": "ai_draft",
        },
        {
            "id": "ai_draft",
            "type": "ai_answer",
            "params": {"confidence_threshold": 0.6, "max_reply_len": 800, "context_messages": 10},
            "next": "done",
            "on_low_confidence": None,
        },
        {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}


@pytest.fixture
async def world(db_sessionmaker):
    """Бот на лид-боте + аккаунт + клиент + свежий диалог."""
    async with db_sessionmaker() as s:
        bot = Bot(
            name="Первичный приём",
            is_enabled=True,
            schedule={"always": True},
            scenario=SCENARIO,
            knowledge_base="",
            ai_provider=provider.LEADBOT,
            # ⚠ В БАЗЕ умолчание — «подсказка» (миграция 0036): бот выходит на живой
            # аккаунт молча. Здесь проверяется, что ответ лид-бота ДОХОДИТ ДО КЛИЕНТА,
            # поэтому автоответ задан явно; поведение подсказки — в test_bot_mode.py.
            mode="auto",
        )
        s.add(bot)
        await s.flush()
        account = AvitoAccount(
            title="LP-Москва",
            avito_user_id=100_000 + uuid.uuid4().int % 100_000,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="whsec",
            bot_id=bot.id,
        )
        client = Client(channel="avito", external_id=uuid.uuid4().hex[:10], name="Иван")
        s.add_all([account, client])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            item_title="Ремонт iPhone",
            item_price="от 8 900 ₽",
        )
        s.add(conv)
        await s.commit()
        return SimpleNamespace(bot_id=bot.id, conversation_id=conv.id)


async def _client_says(db_sessionmaker, redis, world, text: str) -> str:
    async with db_sessionmaker() as s, s.begin():
        s.add(
            Message(
                conversation_id=world.conversation_id,
                external_message_id=f"am-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body=text,
                attachments=[],
                delivery_status="delivered",
            )
        )
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis}
    return await bot_step(ctx, world.conversation_id, text)


async def _rows(db_sessionmaker, conv_id, direction: str) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(Message)
                .where(Message.conversation_id == conv_id, Message.direction == direction)
                .order_by(Message.created_at, Message.id)
            )
        ).scalars()
        return [r.body or "" for r in rows]


@respx.mock
async def test_a_silent_leadbot_never_blocks_delivery(
    db_sessionmaker, redis, world, monkeypatch
) -> None:
    """Решение владельца №4 целиком, на живом тике обработки входящего.

    Лид-бот не отвечает вовсе. Проверяем не форму ответа провайдера, а то, ради
    чего вся эта осторожность: приветствие клиенту УШЛО, тик не упал, диалог
    лежит у человека с внятной причиной, а не в чёрной дыре.
    """
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)
    respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("нет ответа"))

    result = await _client_says(db_sessionmaker, redis, world, "Разбил экран на айфоне")

    assert result == "ok"  # тик отработал, а не свалился

    sent = await _rows(db_sessionmaker, world.conversation_id, "out")
    assert any("Здравствуйте" in text for text in sent), "приветствие обязано было уйти клиенту"

    async with db_sessionmaker() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.id == world.conversation_id))
        ).scalar_one()
    assert conv.bot_active is False
    assert conv.status == "new"  # диалог у людей, а не потерян
    assert (conv.bot_vars or {}).get("handoff", {}).get("reason") == "ai_unavailable"

    notes = await _rows(db_sessionmaker, world.conversation_id, "note")
    assert any("AI недоступен" in note for note in notes)


@respx.mock
async def test_a_live_leadbot_answers_the_client(
    db_sessionmaker, redis, world, monkeypatch
) -> None:
    """Обратная сторона: когда лид-бот жив, клиент получает ЕГО ответ.

    Без этого теста предыдущий проходил бы и на провайдере, который не работает
    никогда.
    """
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    await _client_says(db_sessionmaker, redis, world, "Разбил экран на айфоне")

    sent = await _rows(db_sessionmaker, world.conversation_id, "out")
    assert any(GOOD["reply"] in text for text in sent)


# --- причина передачи и «лид готов» доезжают до человека (12 августа) ---------
#
# До этого `meta` уходила только в журнал сервера. Человек в диалоге видел «бот
# передал» — и ни слова о том, почему и насколько это срочно. Причина, срок и
# собранная заявка живут в `meta`, и место им в ленте диалога, а не в логе.

ESCALATED = {
    "reply": "Уточню по времени и вернусь к вам",
    "confidence": 1.0,
    "needs_operator": True,
    "meta": {
        "layer": "локальный роутер (эскалация: status)",
        "escalation": {
            "reason": "status",
            "label": "вопрос о статусе визита",
            "deadline_min": 10,
            "note": "Вас ждать?",
        },
    },
}

LEAD = {
    "reply": "Могу завтра к 14:00-14:30, подтвержу ближе к делу",
    "confidence": 0.9,
    "needs_operator": False,
    "meta": {"lead_ready": True, "lead": {"окно": "к 14", "телефон": True}},
}


def test_the_reason_and_the_deadline_become_words():
    answer = leadbot._parse(ESCALATED)
    assert answer is not None
    comment = str(answer["handoff_comment"])
    assert "вопрос о статусе визита" in comment
    assert "10 мин" in comment
    assert "Вас ждать?" in comment


def test_a_plain_answer_carries_no_extra_fields():
    """Пустой ключ означал бы, что бэкенд про это поле что-то сказал."""
    answer = leadbot._parse(GOOD)
    assert answer is not None
    assert "handoff_comment" not in answer and "note" not in answer


def test_a_ready_lead_asks_to_confirm_the_time_and_hides_the_phone():
    """Календаря у бота нет, слот ничем не подтверждён — и телефон в ленту не пишем."""
    answer = leadbot._parse(LEAD)
    assert answer is not None
    note = str(answer["note"])
    assert "подтвердите время" in note.lower()
    assert "окно" in note and "телефон" in note
    assert "к 14" not in note, "значения полей лида в заметку не идут"


def test_curly_braces_from_the_other_side_cannot_eat_a_sentence():
    """Комментарий у нас проходит через подстановку; чужая `{` съела бы предложение."""
    answer = leadbot._parse(
        {
            **ESCALATED,
            "meta": {"escalation": {"label": "клиент пишет {адрес} не тот", "deadline_min": 5}},
        }
    )
    assert answer is not None
    comment = str(answer["handoff_comment"])
    assert "{" not in comment and "}" not in comment


def test_a_long_meta_cannot_flood_the_thread():
    answer = leadbot._parse(
        {**ESCALATED, "meta": {"escalation": {"label": "ы" * 5000, "deadline_min": 5}}}
    )
    assert answer is not None
    assert len(str(answer["handoff_comment"])) < 400


def test_nonsense_in_meta_is_ignored_not_printed():
    metas: list[dict[str, object]] = [
        {"escalation": "строка"},
        {"escalation": {}},
        {"lead_ready": True, "lead": 5},
    ]
    for meta in metas:
        answer = leadbot._parse({**GOOD, "meta": meta})
        assert answer is not None
        assert "handoff_comment" not in answer or answer["handoff_comment"]


@respx.mock
async def test_the_operator_sees_why_the_dialog_landed_on_them(
    db_sessionmaker, redis, world, monkeypatch
) -> None:
    """Сквозная проверка: причина доезжает до ленты диалога, а не до лога."""
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=ESCALATED))

    await _client_says(db_sessionmaker, redis, world, "Вас ждать сегодня?")

    notes = " ".join(await _rows(db_sessionmaker, world.conversation_id, "note"))
    assert "вопрос о статусе визита" in notes
    assert "10 мин" in notes


@respx.mock
async def test_имя_и_город_доезжают_до_лид_бота(db_sessionmaker, redis, world, monkeypatch) -> None:
    """Протокол 15.08: `client_name` и `city` больше не пустые.

    Город решает у лид-бота филиал, колонку прайса и окно приезда; имя — живое
    обращение. Дефолт карточки «Клиент» именем не считается и уходит пустым.
    """
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.item_city_slug = "bryansk"
        await s.commit()

    await _client_says(db_sessionmaker, redis, world, "Разбил экран на айфоне")

    body = json.loads(respx.calls.last.request.content)
    assert body["client_name"] == "Иван"
    assert body["city"] == "Брянск"


@respx.mock
async def test_лид_бот_видит_телефон_сырым(db_sessionmaker, redis, world, monkeypatch) -> None:
    """Воронка лид-бота ищет номер в репликах, чтобы не просить его повторно.

    С маской `{PHONE}` бот не видел данный клиентом номер и переспрашивал —
    сам лид-бот шлёт об этом warning (brain/leadchat.py::parse_request).
    Своему Claude маска остаётся — тот запрос уходит за границу.
    """
    monkeypatch.setattr(settings, "leadbot_url", URL, raising=False)
    monkeypatch.setattr(settings, "leadbot_token", TOKEN, raising=False)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=GOOD))

    await _client_says(db_sessionmaker, redis, world, "Запишите: 8 926 123-45-67, Ленина 5")

    body = json.loads(respx.calls.last.request.content)
    texts = " ".join(m["content"] for m in body["dialog"])
    assert "{PHONE}" not in texts
    assert "8 926 123-45-67" in texts
