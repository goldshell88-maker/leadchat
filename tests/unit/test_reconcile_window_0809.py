"""Сверка не перебирает всю переписку каждые пять минут (замер боя 08.09).

⚠ ЧТО ИЗМЕРЕНО НА БОЮ. За 25 минут журнала воркера: 142 прогона сверки,
83 813 обойдённых чатов, 601 с работы воркера (39 % его времени) и
`messages_recovered=0`. 71 прогон из 142 упёрся в потолок в 1000 чатов.

ПОЧЕМУ ОБХОД НЕ СУЖАЛСЯ САМ. `unread_only=true` не сужает список: мы нигде не
помечаем чаты прочитанными у Авито, поэтому «непрочитан» там навсегда. Замер
двух боевых каналов по 1000 чатов: моложе суток — 18 и 63 чата, старше
тридцати дней — 569 и 179, самый старый 63 дня. Обход был отсортирован от
свежего к старому (0 нарушений убывания на 2000 чатах) — то есть 94–98 %
работы приходилось на хвост, где заведомо ничего не менялось.

ПОЧЕМУ ЭТО БИЛО ПО БАЗЕ. Отсев «чат не менялся» стоял ПОСЛЕ выборки диалога и
`max(created_at)` по входящим — то есть не экономил ни одного запроса:
167 626 обращений к базе и 83 813 взятий сессии из пула за те же 25 минут. У
проекта уже была история, когда выеденный пул уронил приём вебхуков.

ЧТО СТОРОЖИМ ЗДЕСЬ. Что окно сузилось — и что страховка при этом цела: после
простоя окно раздвигается само, а неизменившийся чат не ходит в базу.

ДИВЕРСИИ (каждая проверена: сломай — краснеет, восстанови — зеленеет):
  • `ХВОСТ_ПОДРЯД = 100` → `1` — краснеют
    `test_свежий_чат_после_старых_не_теряется` и
    `test_обход_обрывается_на_странице_старых`;
  • `СВЕРКА_МИН_ОКНО = timedelta(hours=24)` → `timedelta(days=400)` — краснеет
    `test_старый_чат_после_прохода_не_перечитывается`;
  • отметка прохода без условия `достаточно` (ставить всегда) — краснеет
    `test_оборванный_потолком_проход_окна_не_сужает`;
  • отсев «чат не менялся» обратно ПОСЛЕ блока с базой — краснеет
    `test_неизменившийся_чат_в_базу_не_ходит`;
  • `min(граница, прошлый - СВЕРКА_НАХЛЁСТ)` → `граница` — краснеет
    `test_после_простоя_окно_раздвигается`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.integrations.avito.adapter import InboundEvent
from app.workers import reconciliation as mod

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 880001
CLIENT_UID = 880002
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


def _chat(chat_id: str, последнее: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        external_chat_id=chat_id,
        has_unread=True,
        last_message_at=последнее,
        item_title="Ремонт стиральных машин",
        item_url=None,
        item_price=None,
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
    )


class Список:
    """Адаптер с заданным списком чатов: помнит, у кого просили историю."""

    def __init__(self, чаты: list[SimpleNamespace]) -> None:
        self.чаты = чаты
        self.выдано: list[str] = []
        self.истории: list[str] = []
        self.падать_на: set[str] = set()

    async def fetch_chats(self, _account, *, unread_only=True):  # noqa: ANN001
        for chat in self.чаты:
            self.выдано.append(chat.external_chat_id)
            yield chat

    async def fetch_history(self, _account, chat, *, since=None):  # noqa: ANN001
        self.истории.append(chat.external_chat_id)
        if chat.external_chat_id in self.падать_на:
            raise RuntimeError("Авито: история чата -> HTTP 503")
        yield InboundEvent(
            external_chat_id=chat.external_chat_id,
            external_message_id=f"m-{chat.external_chat_id}-{len(self.истории)}",
            author_id=CLIENT_UID,
            account_user_id=ACCOUNT_UID,
            text="сообщение",
            created_at=chat.last_message_at or NOW,
            client_name="Клиент",
        )


async def _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account):  # noqa: ANN001
    monkeypatch.setattr(mod, "get_adapter", lambda _ctx: адаптер)
    return await mod.reconcile_account(
        {"db_session_factory": db_sessionmaker, "redis": redis}, account.id
    )


async def test_первый_прогон_идёт_до_самого_дна(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Отметки прохода нет — окна нет: сверка обходит список как до правки.

    Это и есть та единственная глубокая ходка, которой окно потом опирается:
    всё, что было в списке на момент выкатки, проверено хотя бы раз.
    """
    старый = _chat("u2i-древний", NOW - timedelta(days=40))
    адаптер = Список([старый])

    итог = await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.истории == ["u2i-древний"], (
        "первый прогон обязан прочитать даже сорокадневный чат: иначе диалоги, "
        "которых сверка не видела ни разу, останутся непроверенными навсегда"
    )
    assert итог["chats_old"] == 0


async def test_старый_чат_после_прохода_не_перечитывается(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: 94–98 % обхода уходит, страховка остаётся."""
    старый = _chat("u2i-древний", NOW - timedelta(days=40))
    адаптер = Список([старый])
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert len(адаптер.истории) == 1

    # Второй прогон: проход уже отмечен, значит окно — сутки.
    итог = await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert len(адаптер.истории) == 1, "сорокадневный чат снова пошёл в обход"
    assert итог["chats_old"] == 1


async def test_свежее_в_окне_проверяется_всегда(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Сужение окна не должно превратиться в глухоту на живом трафике."""
    свежий = _chat("u2i-свежий", NOW - timedelta(minutes=3))
    адаптер = Список([свежий, _chat("u2i-древний", NOW - timedelta(days=40))])
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    адаптер.истории.clear()

    # Клиент написал ещё раз — состояние чата изменилось.
    свежий.last_message_at = NOW + timedelta(minutes=1)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.истории == ["u2i-свежий"], (
        "новое сообщение в окне не догнано — сверка перестала быть страховкой"
    )


async def test_потерянное_входящее_старше_границы_догоняется(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ТОТ САМЫЙ СЛУЧАЙ, РАДИ КОТОРОГО СВЕРКА И ЖИВЁТ.

    Диалог, у которого последнее ВХОДЯЩЕЕ у нас — недельной давности (дальше
    отвечал только менеджер), а вебхук о новом сообщении клиента потерян.
    Граница обхода смотрит на состояние чата у Авито, а не на нашу отсечку,
    поэтому такой чат в отбор попадает: `last_message_at` у него свежий.
    """
    старая_переписка = _chat("u2i-молчун", NOW - timedelta(days=9))
    адаптер = Список([старая_переписка])
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    адаптер.истории.clear()

    # Вебхук потерян: у нас последнее входящее девятидневной давности,
    # а у Авито в чате уже есть новое сообщение клиента.
    старая_переписка.last_message_at = NOW - timedelta(minutes=2)
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.истории == ["u2i-молчун"], (
        "потерянное вебхуком сообщение не догнано — это отказ страховки"
    )


async def test_после_простоя_окно_раздвигается(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Сверка молчала пять суток — окно обязано накрыть весь простой.

    Иначе сутки становятся жёстким потолком: всё, что потерялось за время
    простоя и с тех пор не шевелилось, не догонится никогда.
    """
    молчавший = _chat("u2i-простой", NOW - timedelta(days=3))
    адаптер = Список([молчавший])
    # Прошлый полный проход был пять суток назад.
    await redis.set(mod._ключ_прохода(account.id), (NOW - timedelta(days=5)).isoformat())

    итог = await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.истории == ["u2i-простой"], (
        "чат, изменившийся во время простоя сверки, выпал из окна — "
        "потерянное за эти дни не догонится уже никогда"
    )
    assert итог["chats_old"] == 0


async def test_свежий_чат_после_старых_не_теряется(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ОБРЫВ ОБХОДА ДЕРЖИТСЯ НА ЧУЖОМ ПОРЯДКЕ — ТЕРПИМ ЦЕЛУЮ СТРАНИЦУ.

    На бою 08.09 список приходит строго от свежего к старому (0 нарушений
    убывания на 2000 чатах), но это порядок Авито, а не наш. Молчаливая его
    смена не должна выключать страховку, поэтому обход обрывается только после
    `ХВОСТ_ПОДРЯД` старых чатов подряд.
    """
    # ⚠ ЧИСЛО ЗДЕСЬ ЖЁСТКОЕ, А НЕ `mod.ХВОСТ_ПОДРЯД - 1`. Проверка, которая
    # берёт размер терпимости из самого кода, подстраивается под его порчу и
    # зеленеет при любом значении — то есть не проверяет ничего.
    старые = [_chat(f"u2i-старый-{i}", NOW - timedelta(days=10 + i)) for i in range(99)]
    свежий = _chat("u2i-затерявшийся", NOW - timedelta(minutes=4))
    адаптер = Список([*старые, свежий, _chat("u2i-хвост", NOW - timedelta(days=99))])
    # Отметка прохода уже есть — окно сузилось до суток.
    await redis.set(mod._ключ_прохода(account.id), (NOW - timedelta(minutes=1)).isoformat())

    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert адаптер.истории == ["u2i-затерявшийся"], (
        "свежий чат за 99 старыми пропущен — при смене порядка у Авито "
        "сверка тихо перестанет находить потери"
    )


async def test_обход_обрывается_на_странице_старых(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Хвост списка не дочитывается: ради этого правка и делалась."""
    чаты = [_chat(f"u2i-{i}", NOW - timedelta(days=10 + i)) for i in range(500)]
    адаптер = Список(чаты)
    await redis.set(mod._ключ_прохода(account.id), (NOW - timedelta(minutes=1)).isoformat())

    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    # Число жёсткое по той же причине, что и в проверке выше.
    assert len(адаптер.выдано) == 100, (
        f"обойдено {len(адаптер.выдано)} чатов из 500 — обрыв по хвосту не работает"
    )


async def test_неизменившийся_чат_в_базу_не_ходит(
    monkeypatch, db_sessionmaker, redis, account, engine
) -> None:
    """⚠ ОТСЕВ «ЧАТ НЕ МЕНЯЛСЯ» ОБЯЗАН СТОЯТЬ ДО БАЗЫ, А НЕ ПОСЛЕ.

    Пока он стоял после выборки диалога и `max(created_at)` по входящим, он не
    экономил ни одного запроса: на бою это 167 626 обращений к базе за 25
    минут при нуле находок.
    """
    свежий = _chat("u2i-свежий", NOW - timedelta(minutes=5))
    адаптер = Список([свежий])
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    запросы: list[str] = []

    def _считать(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        запросы.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _считать)
    try:
        await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _считать)

    по_чату = [q for q in запросы if "conversations" in q or "messages" in q]
    assert по_чату == [], "неизменившийся чат всё ещё стоит запросов в базу: " + "; ".join(
        по_чату[:2]
    )


async def test_сорванный_чат_окно_не_сужает(monkeypatch, db_sessionmaker, redis, account) -> None:
    """Проход с отказом отметки не даёт: непроверенный чат обязан остаться в окне."""
    старый = _chat("u2i-древний", NOW - timedelta(days=40))
    адаптер = Список([старый])
    адаптер.падать_на = {"u2i-древний"}

    итог = await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert итог["chats_failed"] == 1
    assert await redis.get(mod._ключ_прохода(account.id)) is None

    # Следующий прогон обязан снова его достать, а не отсечь по суточному окну.
    адаптер.падать_на = set()
    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)
    assert адаптер.истории == ["u2i-древний", "u2i-древний"], (
        "сорвавшийся чат выпал из окна — его пропущенные сообщения не догонятся"
    )


async def test_оборванный_потолком_проход_окна_не_сужает(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """⚠ ОТМЕТКА ТОЛЬКО ЗА ПРОХОД, КОТОРЫЙ ДЕЙСТВИТЕЛЬНО ВСЁ ЗАКРЫЛ.

    Канал, у которого за сутки шевельнулось больше чатов, чем отдаёт Авито
    (потолок в 1000): обход обрывается ВЫШЕ границы, часть списка не
    просмотрена. Отметить такой проход значило бы разрешить следующему сузить
    окно до суток поверх дыры.
    """
    # Все чаты моложе суток, обход не оборван хвостом — покрытие неполное.
    чаты = [_chat(f"u2i-{i}", NOW - timedelta(minutes=i + 1)) for i in range(5)]
    адаптер = Список(чаты)

    await _прогон(адаптер, monkeypatch, db_sessionmaker, redis, account)

    assert await redis.get(mod._ключ_прохода(account.id)) is None, (
        "проход, не дошедший до границы, отмечен полным — следующее окно "
        "сузится поверх непросмотренного хвоста"
    )
