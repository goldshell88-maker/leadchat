"""Сверка не перечитывает чат, в котором ничего не изменилось (замер 31.08).

⚠ БОЕВОЙ СЛУЧАЙ, С КОТОРОГО ВСЁ НАЧАЛОСЬ. Владелец: «вчера грузился на 78 %».
Замер: воркер держал 102 % процессора НЕПРЕРЫВНО — 30 часов процессорного
времени за сутки. В журнале за 10 минут 33 440 «наблюдений» звонков при 3 472
за всю неделю: история одних и тех же чатов вычитывалась примерно десятью
недельными объёмами каждые десять минут. Заодно это било по лимитам Авито
(`ConnectTimeout` в сверке) и по базе.

ПОЧЕМУ ПРЕЖНИЙ ОТСЕВ НЕ РАБОТАЛ. Он сравнивал `last_message_at` чата с
последним ВХОДЯЩИМ сообщением у нас. Последним в чате бывает не входящее:
наш собственный ответ (а мы отвечаем постоянно), звонок `appCall` или
служебное сообщение Авито — последние два мы у себя сообщениями не храним
вовсе. В этих случаях отсечка навсегда остаётся ниже `last_message_at`, и
отсев не срабатывает НИ РАЗУ.

ЧТО СТОРОЖИМ. Помним не отсечку, а САМО СОСТОЯНИЕ чата: не изменился
`last_message_at` — сверять нечего, что бы там ни лежало последним.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.avito.adapter import InboundEvent
from app.workers import reconciliation as mod

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 777001
CLIENT_UID = 777002
NOW = datetime.now(UTC).replace(microsecond=0)
CHAT = "u2i-повторяемый"


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


def _chat(последнее: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        external_chat_id=CHAT,
        has_unread=True,
        last_message_at=последнее,
        item_title="Ремонт холодильников",
        item_url=None,
        item_price=None,
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
    )


def _event(msg_id: str, *, когда: datetime) -> InboundEvent:
    return InboundEvent(
        external_chat_id=CHAT,
        external_message_id=msg_id,
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text=f"текст {msg_id}",
        created_at=когда,
        client_name="Клиент",
    )


class Считающий:
    """Адаптер, который помнит, сколько раз у него просили историю."""

    def __init__(self, последнее: datetime) -> None:
        self.последнее = последнее
        self.вычиток = 0

    async def fetch_chats(self, _account, *, unread_only=True):  # noqa: ANN001
        yield _chat(self.последнее)

    async def fetch_history(self, _account, chat, *, since=None):  # noqa: ANN001
        self.вычиток += 1
        # Время события НЕ выводим из `последнее`: у чата без отметки времени
        # его нет вовсе, а событие обязано остаться настоящим — иначе адаптер
        # упадёт, диалог не создастся, и проверка пройдёт мимо отсева.
        yield _event(f"m-{self.вычиток}", когда=NOW - timedelta(minutes=1))


async def _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account) -> None:
    monkeypatch.setattr(mod, "get_adapter", lambda _ctx: адаптер)
    await mod.reconcile_account({"db_session_factory": db_sessionmaker, "redis": redis}, account.id)


async def test_второй_прогон_историю_не_качает(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: неизменившийся чат вычитывается один раз, а не вечно."""
    адаптер = Считающий(NOW)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    после_первого = адаптер.вычиток
    assert после_первого == 1, "первый прогон обязан вычитать историю"

    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.вычиток == после_первого, (
        "чат перечитан повторно без единого изменения — это и есть сожранное ядро"
    )


async def test_новое_сообщение_снова_поднимает_вычитку(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Отсев не должен превратиться в глухоту: изменился чат — читаем."""
    адаптер = Считающий(NOW)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert адаптер.вычиток == 1

    адаптер.последнее = NOW + timedelta(minutes=7)  # клиент написал
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.вычиток == 2, (
        "чат изменился, а сверка его пропустила — сообщение клиента не догонится"
    )


async def test_сорванный_чат_метку_не_получает(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ИНАЧЕ ОТСЕВ ПРЯЧЕТ ИМЕННО ТО, ЧТО ДОЛЖЕН ДОГОНЯТЬ.

    Отметка ставится ПОСЛЕ успешного прохода. Поставь её раньше — чат,
    сорвавшийся на середине (503 площадки, битое вложение), считался бы
    сверенным, и его пропущенные сообщения не догнались бы никогда.
    """

    class Капризный(Считающий):
        """Первый проход удачный, дальше — отказ площадки."""

        падать = False

        async def fetch_history(self, _account, chat, *, since=None):  # noqa: ANN001
            self.вычиток += 1
            if self.падать:
                raise RuntimeError("Авито: история чата -> HTTP 503")
            yield _event(f"m-{self.вычиток}", когда=NOW - timedelta(minutes=1))

    адаптер = Капризный(NOW)
    # 1. Удачный проход: диалог заводится, состояние запоминается.
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert адаптер.вычиток == 1

    # 2. Клиент написал (состояние чата изменилось), но площадка отвечает 503.
    адаптер.последнее = NOW + timedelta(minutes=5)
    адаптер.падать = True
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert адаптер.вычиток == 2

    # 3. Следующий прогон ОБЯЗАН попробовать снова: сорванный чат сверенным не
    #    считается, иначе его пропущенные сообщения не догонятся никогда.
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.вычиток == 3, (
        "сорвавшийся чат помечен сверенным — его сообщения потеряются навсегда"
    )


async def test_чат_без_отметки_времени_не_отсеивается(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Нет `last_message_at` — сравнивать нечем; лишняя вычитка дешевле пропажи."""
    адаптер = Считающий(NOW)
    адаптер.последнее = None  # type: ignore[assignment]
    # Первый прогон заводит диалог; отсев спрашивается только у знакомых чатов,
    # поэтому проверять надо ВТОРОЙ и ТРЕТИЙ.
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.вычиток == 3, "чат без отметки времени молча перестал сверяться"


async def test_чат_без_диалога_тоже_отсеивается(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ВТОРАЯ ИТЕРАЦИЯ ПОЧИНКИ, И ИМЕННО ОНА ДАЛА ГЛАВНЫЙ ВЫИГРЫШ.

    Первый вариант спрашивал отсев только у ЗНАКОМЫХ чатов (`conv is not
    None`) — и не ловил тех, кто грузил сильнее всех. Чат, в котором лежат
    только звонки (`appCall`) и служебные сообщения Авито, диалогом НЕ
    СТАНОВИТСЯ НИКОГДА (`inbound.avito_system_orphan`), значит `conv` у него
    вечно пуст, и его история перечитывалась целиком каждые пять минут.

    Замер прода это и показал: после первой правки воркер остался на 107 %
    процессора, а «наблюдений» шло 2100 в минуту при нуле прогонов сверки в
    журнале.
    """

    class ПустойЧат(Считающий):
        """История есть, но событий, годных в диалог, в ней нет."""

        async def fetch_history(self, _account, chat, *, since=None):  # noqa: ANN001
            self.вычиток += 1
            return
            yield  # pragma: no cover — делает функцию генератором

    адаптер = ПустойЧат(NOW)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert адаптер.вычиток == 1, "первый проход обязан прочитать чат"

    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.вычиток == 1, (
        "чат без диалога перечитан снова — это и есть сожранное ядро: такие "
        "чаты диалогом не становятся никогда, значит читались бы вечно"
    )
