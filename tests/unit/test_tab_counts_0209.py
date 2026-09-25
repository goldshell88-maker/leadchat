"""GET /conversations/counts — числа для вкладок «Мои» и «Все» (жалоба 02.09).

⚠ ЖАЛОБА ВЛАДЕЛЬЦА: «нет интуитивно понятной навигации, сколько у меня диалогов
взято, сколько диалогов не отвечено».

⚠ ЧТО ИМЕННО СТЕРЕЖЁТ ЭТОТ ФАЙЛ. Не «ручка отвечает 200», а СХОДИМОСТЬ: число
на вкладке обязано равняться тому, что в этой вкладке лежит. Бейдж у «Моих» уже
однажды снимали именно за расхождение — он считался по загруженным страницам и
рос от прокрутки. Поэтому каждое число здесь сверяется с ответом самого списка,
а не с ожидаемой константой: константу можно подогнать под любую ошибку, ответ
списка — нельзя.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Client, Conversation

T0 = datetime(2026, 9, 2, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def набор(db_sessionmaker, make_avito_account, users_by_role):
    """Мои: 3 ждут ответа + 2 отвечено. Чужой: 1 ждёт. Закрытый мой: 1."""
    account = await make_avito_account()
    manager = users_by_role["manager"]
    async with db_sessionmaker() as s:
        созданы = []

        def conv(n, *, status, awaiting, assignee):
            cl = Client(channel="avito", external_id=f"c{n}", name=f"Клиент {n}")
            s.add(cl)
            созданы.append((cl, n, status, awaiting, assignee))
            return cl

        план = [
            ("ждёт-1", "in_progress", True, manager.id),
            ("ждёт-2", "new", True, manager.id),
            ("ждёт-3", "waiting_client", True, manager.id),
            ("отвечен-1", "in_progress", False, manager.id),
            ("отвечен-2", "waiting_client", False, manager.id),
            ("чужой-ждёт", "in_progress", True, None),
            # Закрытый не считается нигде: у закрытого ждать некого — то же
            # правило, что и у `conversation_status.waiting_since`.
            ("мой-закрытый", "closed", True, manager.id),
        ]
        for имя, статус, ждёт, кому in план:
            cl = conv(имя, status=статус, awaiting=ждёт, assignee=кому)
            await s.flush()
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id=f"chat-{имя}",
                    account_id=account.id,
                    client_id=cl.id,
                    status=статус,
                    unread_count=0,
                    tags=[],
                    last_message_at=T0 + timedelta(minutes=len(имя)),
                    assignee_id=кому,
                    awaiting_since=T0 if ждёт else None,
                    item_title="Ремонт iPhone 13",
                )
            )
        await s.commit()
    return SimpleNamespace(account=account, manager=manager)


async def список(client, tokens, tab):
    r = await client.get(f"/api/v1/conversations?tab={tab}&limit=200", headers=auth(tokens))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_числа_сходятся_со_списком(client, tokens, набор):
    """Главная проверка: счётчик равен тому, что во вкладке."""
    r = await client.get("/api/v1/conversations/counts", headers=auth(tokens))
    assert r.status_code == 200, r.text
    числа = r.json()

    мои = await список(client, tokens, "my")

    assert числа["mine"] == len(мои)
    # «Не отвечено» — по тому же полю, которое строка показывает шкалой.
    assert числа["mine_waiting"] == sum(1 for i in мои if i.get("waiting_since") is not None)


async def test_состав_набора_такой_как_задуман(client, tokens, набор):
    """Страховка от зелени на пустом месте: сойтись можно и на нулях."""
    числа = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()
    assert числа == {"mine": 5, "mine_waiting": 3}


async def test_закрытый_не_попадает_в_счёт(client, tokens, набор):
    """У закрытого ждать некого — правило `waiting_since`, не отдельное здесь."""
    числа = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()
    всё = await список(client, tokens, "any")
    мои_закрытые = [i for i in всё if i["status"] == "closed"]
    assert мои_закрытые, "набор обязан содержать закрытый диалог, иначе проверка пуста"
    assert числа["mine"] == 5, "закрытый попал в «взято»"
    assert числа["mine_waiting"] == 3, "закрытый попал в «не отвечено»"


async def test_число_не_зависит_от_страницы(client, tokens, набор):
    """⚠ ИМЕННО ЗА ЭТО СНИМАЛИ ПРЕЖНИЙ БЕЙДЖ.

    Он считался по загруженным строкам и рос от прокрутки: «Мои 3» → «Мои 17».
    Ручка обязана отдавать одно и то же число независимо от того, сколько
    строк успел забрать клиент.
    """
    страница = await client.get("/api/v1/conversations?tab=my&limit=2", headers=auth(tokens))
    assert len(страница.json()["items"]) == 2

    числа = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()
    assert числа["mine"] == 5, "счётчик посчитал страницу, а не вкладку"


async def test_нужен_вход(client, набор):
    r = await client.get("/api/v1/conversations/counts")
    assert r.status_code == 401


async def test_фильтр_ждут_отдаёт_ровно_то_что_насчитано(client, tokens, набор):
    """⚠ ПАРА «ЧИСЛО ↔ ПЕРЕХОД». Нажал на «не отвечено: 3» — увидел ровно три.

    Число и фильтр обязаны считаться ОДНИМ предикатом (`_waiting_condition`).
    Разъедься они — человек нажал бы на 3 и увидел 6, и перестал бы верить
    обоим числам сразу.
    """
    числа = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()

    r = await client.get(
        "/api/v1/conversations?tab=my&waiting_only=true&limit=200", headers=auth(tokens)
    )
    assert r.status_code == 200, r.text
    строки = r.json()["items"]

    assert len(строки) == числа["mine_waiting"]
    assert строки, "проверка пуста, если фильтр не вернул ничего"
    assert all(i.get("waiting_since") is not None for i in строки)

    # На «Всех» фильтр тоже осмыслен, хотя счётчика там нет: закрытые
    # отсеивает сам предикат ожидания, а не вкладка.
    все = await client.get(
        "/api/v1/conversations?tab=any&waiting_only=true&limit=200", headers=auth(tokens)
    )
    assert all(i.get("waiting_since") is not None for i in все.json()["items"])
    assert len(все.json()["items"]) == 4


async def test_фильтр_ждут_неприменим_во_входящих(client, tokens, набор):
    """В очереди ждут все: молча проигнорировать значит показать больше."""
    r = await client.get("/api/v1/conversations?tab=inbox&waiting_only=true", headers=auth(tokens))
    assert r.status_code == 400
    поля = [f["field"] for f in r.json()["error"]["details"]["fields"]]
    assert "waiting_only" in поля
