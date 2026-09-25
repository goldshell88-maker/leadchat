"""ПЕРЕДАТЬ ДИАЛОГ МОЖНО ТОЛЬКО ТОМУ, КТО В СЕТИ.

Решение владельца 28.08 дословно: «сделай так, чтобы не было возможности
передать диалог человеку в офлайн, и показывались для передачи только в
онлайне».

⚠ ПОЧЕМУ ЭТО НЕ КОСМЕТИКА. Передача — предложение, а не свершившийся факт:
пока получатель её не принял, диалог не появляется НИ в очереди, ни в «Моих»
получателя (см. `transfer.offer`). Отданный ушедшему домой человеку, он не
виден никому и ждёт молча — а отдающий уверен, что дело сделано и уходит к
следующему клиенту. Это ровно тот способ терять диалоги, ради отмены которого
двухфазная передача и написана.

⚠ ЗАПРЕТ НА СЕРВЕРЕ, А НЕ ТОЛЬКО В СПИСКЕ. Список сотрудников грузится один раз
при открытии окна, и человек в нём успевает уйти, пока оператор дописывает
комментарий. Отсеивать только в интерфейсе — договориться, а не запретить.

⚠ «ПОЗВАТЬ» ПОД ЭТО ПРАВИЛО НЕ ПОПАДАЕТ, И ЭТО РЕШЕНИЕ. Приглашение диалог не
отдаёт: за клиента по-прежнему отвечает тот, кто вёл, а позванный прочтёт
уведомление, когда придёт. Запрет там обрезал бы способ спросить совета у того,
кто в теме, — ради правила, придуманного для другого действия.
"""

import pytest

from app.models import Client, Conversation

pytestmark = pytest.mark.anyio


@pytest.fixture
async def seed(db_sessionmaker, make_avito_account):
    """Один живой диалог — больше этим проверкам ничего не нужно."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        клиент = Client(channel="avito", external_id="9001", name="Иван Петров")
        s.add(клиент)
        await s.flush()
        диалог = Conversation(
            channel="avito",
            external_chat_id="chat-online-only",
            account_id=account.id,
            client_id=клиент.id,
            status="new",
        )
        s.add(диалог)
        await s.commit()
        return диалог.id


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def test_transfer_to_an_offline_colleague_is_refused(client, tokens, seed, make_user) -> None:
    ушёл_домой = await make_user("offline@leadchat.test", role="manager", full_name="Олег Иванов")

    r = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": str(ушёл_домой.id), "comment": "разберись"},
        headers=auth(tokens),
    )

    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "assignee_offline"
    assert "Олег Иванов" in r.json()["error"]["message"], (
        "в сообщении нет имени — оператор не поймёт, кого именно нет на месте"
    )


async def test_transfer_to_an_online_colleague_goes_through(
    client, tokens, seed, make_user, в_сети
) -> None:
    на_месте = await make_user("online@leadchat.test", role="manager", full_name="Пётр Ковалёв")
    await в_сети(на_месте)

    r = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": str(на_месте.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text


async def test_away_counts_as_present(client, tokens, seed, make_user, в_сети) -> None:
    """«Отошёл» — это не офлайн.

    Отошедший на обед сидит за столом: приложение открыто, уведомление он
    увидит. Тот же довод записан у `presence_map`. Разойдись окно и запрет,
    человек видел бы в списке того, кому сервер отказывается отдать диалог.

    ⚠ ФРАЗА ПРО ЗЕЛЁНУЮ ТОЧКУ ОТСЮДА УБРАНА 01.09. Здесь стояло «зелёная точка
    в списке стоит по подключению, а не по статусу» — и это было правдой ровно
    до жалобы владельца: точка красила обедающего и работающего одинаково.
    Правило, которое проверяет ЭТОТ тест, не изменилось (отошедшему передать
    можно), изменился показ — см. `test_assignable_presence_0109`.
    """
    отошёл = await make_user("away@leadchat.test", role="manager", full_name="Анна Петрова")
    await в_сети(отошёл, "away")

    r = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": str(отошёл.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text


async def test_taking_the_dialog_for_yourself_never_asks_about_presence(
    client, tokens, seed, users_by_role
) -> None:
    """Себе диалог можно забрать всегда.

    Человек в сети по факту запроса, а отметка присутствия принадлежит WS-хабу и
    живёт с TTL: подвисшее соединение или вкладка, только что открытая, — не
    повод отказать человеку в его же диалоге.
    """
    r = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text


async def test_unassigning_is_not_a_transfer(client, tokens, seed, users_by_role) -> None:
    """Снять ответственного (`assignee_id: null`) — не передача.

    Диалог возвращается в очередь, где его видят все тринадцать. Спрашивать про
    присутствие тут не у кого.
    """
    # Сначала берём диалог себе — иначе снимать нечего и ручка честно скажет
    # «у диалога и так нет ответственного», а проверка пройдёт мимо смысла.
    взял = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    assert взял.status_code == 200, взял.text

    r = await client.post(
        f"/api/v1/conversations/{seed}/assign",
        json={"assignee_id": None},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text


async def test_inviting_an_offline_colleague_is_still_allowed(
    client, tokens, seed, make_user
) -> None:
    """«Позвать» в офлайн — можно, и это решение, а не забытая проверка."""
    ушёл_домой = await make_user("away2@leadchat.test", role="manager", full_name="Олег Иванов")

    r = await client.post(
        f"/api/v1/conversations/{seed}/participants",
        json={"user_id": str(ушёл_домой.id), "reason": "ты вёл этот адрес"},
        headers=auth(tokens),
    )
    assert r.status_code in (200, 201), r.text
