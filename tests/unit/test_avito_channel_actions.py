"""Управление каналом из интерфейса: подписка заново и удаление.

ЗАЧЕМ ЭТО ПОЯВИЛОСЬ. Заказчик сказал прямо: «я не могу сам нормально добавлять
аккаунты». Сервер умел отключать, включать и снимать подписку с самого начала —
но кнопок не было ни одной, и любое из этих действий требовало разработчика с
доступом к серверу. Ошибиться при подключении можно за секунду, а исправить —
только через меня. Ровно это и делало самостоятельное подключение невозможным.

Здесь заперты новые ручки и то, что удаление НЕ обещает лишнего. Правило
«канал с перепиской удалить нельзя» действовало до 8 августа и снято решением
заказчика: аккаунты Авито у компании меняются постоянно, история ушедшего не
нужна никому. Эта шапка ещё в августе продолжала обещать отказ, которого в
коде уже не было, — и ровно то же самое обещали докстрока ручки и подсказка на
карточке. Взамен заперт размер потери: ``GET /avito-accounts/{id}/history-size``,
из которого подтверждение берёт числа.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AvitoAccount, Client, Conversation, Message

pytestmark = pytest.mark.anyio


def _as(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def _make_conversation(db, account: AvitoAccount) -> Conversation:
    client = Client(channel="avito", external_id=f"c-{uuid.uuid4()}", name="Клиент")
    db.add(client)
    await db.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=f"chat-{uuid.uuid4()}",
        account_id=account.id,
        client_id=client.id,
        status="closed",
    )
    db.add(conv)
    await db.commit()
    return conv


# ------------------------------------------------------------------ удаление


async def test_empty_channel_can_be_deleted(client, tokens, db, make_avito_account) -> None:
    """Главный случай: промахнулись аккаунтом при подключении.

    Диалогов ещё нет, а строка висит в списке навсегда — потому что убрать её
    нечем. Именно эта мелочь и мешала подключать каналы самостоятельно:
    ошибка становилась вечной.
    """
    account = await make_avito_account(880100)

    response = await client.delete(
        f"/api/v1/avito-accounts/{account.id}", headers=_as("admin", tokens)
    )

    assert response.status_code == 204
    left = await db.scalar(
        sa.select(sa.func.count()).select_from(AvitoAccount).where(AvitoAccount.id == account.id)
    )
    assert left == 0


async def test_deleting_a_channel_takes_its_history_along(
    client, tokens, db, make_avito_account
) -> None:
    """Переписка уходит вместе с каналом (решение заказчика от 8 августа).

    Здесь стоял отказ «в канале N диалогов — отключите вместо удаления», и он
    был верен, пока история считалась ценностью. Аккаунты Авито у компании
    меняются постоянно, и переписка ушедшего не нужна никому.
    """
    account = await make_avito_account(880200)
    conv = await _make_conversation(db, account)

    response = await client.delete(
        f"/api/v1/avito-accounts/{account.id}", headers=_as("admin", tokens)
    )

    assert response.status_code == 204
    assert await db.get(AvitoAccount, account.id) is None
    left = await db.scalar(
        sa.select(sa.func.count()).select_from(Conversation).where(Conversation.id == conv.id)
    )
    assert left == 0


# ------------------------------------------------- размер потери для подтверждения


async def _add_messages(db, conv: Conversation, count: int) -> None:
    for i in range(count):
        db.add(
            Message(
                conversation_id=conv.id,
                direction="in",
                sender_type="client",
                body=f"сообщение {i}",
                attachments=[],
                created_at=datetime.now(UTC),
            )
        )
    await db.commit()


async def test_history_size_names_what_will_be_wiped(
    client, tokens, db, make_avito_account
) -> None:
    """Подтверждение обязано называть числа, а не «все диалоги и сообщения».

    За общей формулировкой одинаково прячутся пустая ошибочно подключённая
    строка и год работы живого канала — а необратимы оба нажатия. Комментарий
    рядом с удалением обещал, что окно «называет число диалогов», и три месяца
    в окне не было ни одного числа: обещание закрывается этой ручкой.
    """
    account = await make_avito_account(880700)
    first = await _make_conversation(db, account)
    second = await _make_conversation(db, account)
    await _add_messages(db, first, 3)
    await _add_messages(db, second, 2)

    response = await client.get(
        f"/api/v1/avito-accounts/{account.id}/history-size", headers=_as("admin", tokens)
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"conversations": 2, "messages": 5}


async def test_history_size_counts_only_its_own_channel(
    client, tokens, db, make_avito_account
) -> None:
    """Соседний канал в числа не попадает.

    Числа показывают перед необратимым действием: чужая переписка в них
    означала бы, что человек испугается и не нажмёт там, где стирать нечего,
    — или, наоборот, недооценит потерю на канале рядом.
    """
    mine = await make_avito_account(880800)
    neighbour = await make_avito_account(880900)
    await _add_messages(db, await _make_conversation(db, neighbour), 7)
    await _add_messages(db, await _make_conversation(db, mine), 1)

    response = await client.get(
        f"/api/v1/avito-accounts/{mine.id}/history-size", headers=_as("admin", tokens)
    )

    assert response.json() == {"conversations": 1, "messages": 1}


async def test_history_size_of_an_empty_channel_is_zeroes(
    client, tokens, make_avito_account
) -> None:
    """Пустой канал называется пустым — это половина смысла подсказки."""
    account = await make_avito_account(881000)
    response = await client.get(
        f"/api/v1/avito-accounts/{account.id}/history-size", headers=_as("admin", tokens)
    )
    assert response.json() == {"conversations": 0, "messages": 0}


async def test_history_size_is_admin_only(client, tokens, make_avito_account) -> None:
    """Числа спрашивает тот, кто собрался стирать: право то же, что у удаления."""
    account = await make_avito_account(881100)
    response = await client.get(
        f"/api/v1/avito-accounts/{account.id}/history-size", headers=_as("manager", tokens)
    )
    assert response.status_code == 403


async def test_history_size_of_an_unknown_channel_is_404(client, tokens) -> None:
    response = await client.get(
        f"/api/v1/avito-accounts/{uuid.uuid4()}/history-size", headers=_as("admin", tokens)
    )
    assert response.status_code == 404


async def test_deleting_an_unknown_channel_is_404(client, tokens) -> None:
    response = await client.delete(
        f"/api/v1/avito-accounts/{uuid.uuid4()}", headers=_as("admin", tokens)
    )
    assert response.status_code == 404


# --------------------------------------------------------------- права


async def test_deleting_is_admin_only(client, tokens, make_avito_account) -> None:
    account = await make_avito_account(880300)
    response = await client.delete(
        f"/api/v1/avito-accounts/{account.id}", headers=_as("manager", tokens)
    )
    assert response.status_code == 403


async def test_registering_webhook_is_admin_only(client, tokens, make_avito_account) -> None:
    account = await make_avito_account(880400)
    response = await client.post(
        f"/api/v1/avito-accounts/{account.id}/register-webhook", headers=_as("manager", tokens)
    )
    assert response.status_code == 403


async def test_deleting_needs_a_session(client, make_avito_account) -> None:
    account = await make_avito_account(880500)
    response = await client.delete(f"/api/v1/avito-accounts/{account.id}")
    assert response.status_code == 401


# ------------------------------------------------------- подписка заново


async def test_webhook_cannot_be_registered_on_a_disabled_channel(
    client, tokens, db, make_avito_account
) -> None:
    """Подписка на выключенном канале — это включение через чёрный ход.

    Отказ здесь бережёт от неочевидного состояния «канал выключен, но
    сообщения идут»: разбирать такое пришлось бы по журналу сервера.
    """
    account = await make_avito_account(880600)
    await db.execute(
        sa.update(AvitoAccount).where(AvitoAccount.id == account.id).values(status="disabled")
    )
    await db.commit()

    response = await client.post(
        f"/api/v1/avito-accounts/{account.id}/register-webhook", headers=_as("admin", tokens)
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "not_active"


# ---------------------------------------------------------- переименование


async def test_channel_can_be_renamed(client, tokens, db, make_avito_account) -> None:
    """Имя из Авито читается плохо: «! Парт - 7 / Ист - В43 МНЧ !».

    У Jivo переименования нет вовсе, и заказчик живёт с такими строками в
    списках, фильтрах и статистике. Повторять чужое отсутствие функции незачем.
    """
    account = await make_avito_account(890100)

    response = await client.patch(
        f"/api/v1/avito-accounts/{account.id}",
        json={"title": "Москва · Центр"},
        headers=_as("admin", tokens),
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Москва · Центр"
    stored = await db.scalar(sa.select(AvitoAccount.title).where(AvitoAccount.id == account.id))
    assert stored == "Москва · Центр"


async def test_rename_survives_reconnect(db, make_avito_account) -> None:
    """Переподключение канала не должно возвращать сырое имя из Авито.

    Иначе человек, давший каналу понятное название, потерял бы его при первой
    же починке доступа — и не понял бы, почему.
    """
    from app.services import avito_accounts as svc

    account = await make_avito_account(890200)
    await db.execute(
        sa.update(AvitoAccount).where(AvitoAccount.id == account.id).values(title="Москва · Центр")
    )
    await db.commit()

    same, created = await svc.upsert_account(
        db,
        {"id": 890200, "name": "! Парт - 7 / Ист - В43 МНЧ !"},
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    )

    assert created is False
    assert same.title == "Москва · Центр"


async def test_renaming_is_admin_only(client, tokens, make_avito_account) -> None:
    account = await make_avito_account(890300)
    response = await client.patch(
        f"/api/v1/avito-accounts/{account.id}",
        json={"title": "Что-нибудь"},
        headers=_as("manager", tokens),
    )
    assert response.status_code == 403


# ------------------------------------------- отключение стирает переписку


async def test_disabling_wipes_the_history(client, tokens, db, make_avito_account) -> None:
    """«Как аккаунт отключаем — сразу же удаляем историю».

    Раньше карточка обещала «отключён вручную, история доступна для чтения».
    Компания меняет учётные записи Авито постоянно, и переписка отключённого
    канала не нужна никому: она занимает диск и мешается в поиске.
    """
    account = await make_avito_account(890400)
    conv = await _make_conversation(db, account)

    response = await client.post(
        f"/api/v1/avito-accounts/{account.id}/disable", headers=_as("admin", tokens)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    left = await db.scalar(
        sa.select(sa.func.count()).select_from(Conversation).where(Conversation.id == conv.id)
    )
    assert left == 0


async def test_disabling_does_not_touch_other_channels(
    client, tokens, db, make_avito_account
) -> None:
    """Стираем переписку ОДНОГО канала, а не всё подряд.

    Ошибка здесь стоила бы переписки восьми работающих каналов, и заметили бы
    её не сразу.
    """
    victim = await make_avito_account(890500)
    bystander = await make_avito_account(890600)
    await _make_conversation(db, victim)
    survivor = await _make_conversation(db, bystander)

    await client.post(f"/api/v1/avito-accounts/{victim.id}/disable", headers=_as("admin", tokens))

    left = await db.scalar(
        sa.select(sa.func.count()).select_from(Conversation).where(Conversation.id == survivor.id)
    )
    assert left == 1
