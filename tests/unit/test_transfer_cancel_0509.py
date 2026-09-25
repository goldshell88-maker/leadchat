"""Отмена передачи тем, кто её начал (просьба владельца 04.09).

«Нужно будет сделать, чтобы можно было отменять передачу, если я случайно
начал передавать не тому человеку».

Обратная половина — отказ получателя — работает с 7 августа. А забрать
ошибку назад было нечем: предложение висит пятнадцать минут, и всё это время
передавший либо ждёт, пока посторонний человек откажется, либо идёт просить
его об этом в мессенджере. Диалог при этом числится за передавшим, и клиент
ждёт ответа именно от него.

Проверяется здесь не «есть ли ручка». Проверяются свойства, ради которых она
написана:

* отмена возвращает ровно то состояние, что было до нажатия «Передать», —
  ответственный прежний, предложения нет;
* получатель об отмене УЗНАЁТ. Ему уже приходило «Вам передали диалог», а
  диалога нет ни в его «Моих», ни в очереди: без закрывающей вести он остаётся
  с долгом, который негде проверить;
* отменить чужое предложение нельзя никому, кроме обладателя
  `conversations:manage`, — тем же правом уже открыт `/assign`, который
  предложение перезаписывает.
"""

import pytest
import sqlalchemy as sa

from app.core.security import create_access_token
from app.models import AuditLog, Client, Conversation, Message
from app.models.notification import Notification
from app.services import conversations as convs
from tests.unit.conftest import drain_events

pytestmark = pytest.mark.anyio


@pytest.fixture
def передача(db_sessionmaker, make_avito_account, make_user, users_by_role, client, tokens, в_сети):
    """Заводит диалог менеджера, предложенный коллеге и ждущий ответа.

    Предложение создаётся ЧЕРЕЗ РУЧКУ `/assign`, а не записью в базу: отмена
    обязана снимать то же состояние, которое создаёт боевой путь, вместе со
    всеми его следами — ⚑ в списке получателя и уведомлением ему же.

    Фикстура СИНХРОННАЯ и отдаёт корутину, а не готовые данные: асинхронная
    исполняется в своём цикле событий, и открытое в ней подключение fakeredis
    в теле теста падает с «bound to a different event loop».
    """

    async def _создать() -> dict:
        giver = users_by_role["manager"]
        taker = await make_user("taker0509@leadchat.test", role="manager", full_name="Борис Чужой")
        account = await make_avito_account()
        async with db_sessionmaker() as s:
            cl = Client(channel="avito", external_id="cancel-0509", name="Клиент Отмены")
            s.add(cl)
            await s.flush()
            row = Conversation(
                channel="avito",
                external_chat_id="chat-cancel-0509",
                account_id=account.id,
                client_id=cl.id,
                status="in_progress",
                assignee_id=giver.id,
            )
            s.add(row)
            await s.commit()
            conv_id = row.id

        # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
        await в_сети(taker)
        r = await client.post(
            f"/api/v1/conversations/{conv_id}/assign",
            headers={"Authorization": f"Bearer {tokens['manager']}"},
            json={"assignee_id": str(taker.id), "comment": "Твой район"},
        )
        assert r.status_code == 200, r.text
        return {
            "conv_id": conv_id,
            "giver": giver,
            "taker": taker,
            "giver_token": tokens["manager"],
        }

    return _создать


async def _cancel(client, conv_id, token: str):
    return await client.post(
        f"/api/v1/conversations/{conv_id}/transfer/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_the_giver_takes_his_offer_back(client, db_sessionmaker, передача):
    """ГЛАВНОЕ: после отмены всё как до нажатия «Передать».

    Ответственный не меняется — он и не менялся: пока предложение висит,
    диалог числится за передавшим. Отмена трогает только предложение.
    """
    дано = await передача()

    r = await _cancel(client, дано["conv_id"], дано["giver_token"])
    assert r.status_code == 200, r.text
    assert r.json()["transfer"] is None, "в ответе полосы уже нет"

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, дано["conv_id"])
        assert row.transfer_to_id is None, "предложение снято"
        assert row.transfer_by_id is None
        assert row.transfer_comment is None, "и объяснение к нему тоже"
        assert row.assignee_id == дано["giver"].id, "диалог остался за передававшим"


async def test_the_recipient_is_told_the_offer_is_gone(client, db_sessionmaker, передача):
    """Получатель узнаёт об отмене из центра уведомлений.

    Ему уже пришло «Вам передали диалог». Диалога при этом нет ни в его
    «Моих», ни в очереди — проверить, ждут ли ещё от него решения, негде.
    Молчаливая отмена оставила бы человека с долгом, которого больше нет.
    """
    дано = await передача()

    r = await _cancel(client, дано["conv_id"], дано["giver_token"])
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.kind == "conversation.transfer_cancelled"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "об отмене обязаны сказать получателю"
    assert rows[0].recipient_id == дано["taker"].id
    assert "отменил(а) передачу" in (rows[0].body or "")


async def test_the_flag_in_the_recipients_list_goes_out(client, redis, передача):
    """⚑ «вам передали» гаснет вместе с предложением.

    Флажок снимается открытием диалога (11 §2.1), и отказ с принятием через
    это открытие и проходят. Отмена — нет: получатель в ней не участвует
    вовсе. Оставь мы флажок — на диалоге, предложения по которому больше не
    существует, до конца смены висела бы пометка «вам передали».
    """
    дано = await передача()
    conv_id = дано["conv_id"]

    до = await convs.transferred_ids(redis, дано["taker"].id)
    assert str(conv_id) in до, "проверка бессмысленна, если флажка не было"

    r = await _cancel(client, conv_id, дано["giver_token"])
    assert r.status_code == 200, r.text

    после = await convs.transferred_ids(redis, дано["taker"].id)
    assert str(conv_id) not in после


async def test_the_bar_disappears_without_a_reload(client, redis, передача):
    """Кадр обязателен: полоса у получателя должна погаснуть сама.

    Он в этот момент смотрит в другой диалог или вовсе не смотрит на экран.
    Без кадра «Принять диалог» осталось бы у него до перезагрузки страницы — и
    нажатие вернуло бы 422 по предложению, которого нет.
    """
    дано = await передача()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    r = await _cancel(client, дано["conv_id"], дано["giver_token"])
    assert r.status_code == 200, r.text

    события = await drain_events(pubsub)
    кадры = [e for e in события if e.get("type") == "conversation:updated"]
    assert кадры, f"кадра обновления нет вовсе: {[e.get('type') for e in события]}"
    assert кадры[-1]["data"]["patch"]["transfer"] is None
    # Кому это персонально важно — получателю: полоса с кнопками у него.
    assert кадры[-1]["data"]["for_user_id"] == str(дано["taker"].id)


async def test_the_feed_and_the_journal_say_who_cancelled(client, db_sessionmaker, передача):
    """В ленте — системная запись, в журнале — своё событие.

    Ленту читают оба участника, и «предложение появилось и пропало» без строки
    выглядит как сбой. В журнале отмена отличается от «никто не ответил»
    наличием человека и его решения: разбор «почему диалог полчаса стоял на
    месте» упирается ровно в это.
    """
    дано = await передача()

    r = await _cancel(client, дано["conv_id"], дано["giver_token"])
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        тексты = [
            m.body
            for m in (
                (
                    await s.execute(
                        sa.select(Message).where(
                            Message.conversation_id == дано["conv_id"],
                            Message.sender_type == "system",
                        )
                    )
                )
                .scalars()
                .all()
            )
        ]
        записи = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "conversation.transfer_cancelled")
                )
            )
            .scalars()
            .all()
        )
    assert any("Передача отменена" in (t or "") for t in тексты), тексты
    assert len(записи) == 1
    assert записи[0].details["to_id"] == str(дано["taker"].id)
    assert записи[0].user_id == дано["giver"].id


async def test_an_answered_offer_cannot_be_cancelled(client, передача):
    """Коллега успел ответить — отменять нечего, и причина машиночитаемая.

    Гонка достижима каждый день: человек жмёт «Отменить передачу» ровно
    тогда, когда получатель жмёт «Принять диалог». Победитель один (строка
    заперта `FOR UPDATE`), проигравший обязан получить понятный отказ, а не
    500 и не молчаливое «готово» поверх уже принятого диалога.
    """
    дано = await передача()
    taker_token = create_access_token(user_id=str(дано["taker"].id), role=дано["taker"].role)

    r = await client.post(
        f"/api/v1/conversations/{дано['conv_id']}/transfer/decline",
        headers={"Authorization": f"Bearer {taker_token}"},
    )
    assert r.status_code == 200, r.text

    r = await _cancel(client, дано["conv_id"], дано["giver_token"])
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "no_pending_transfer"


async def test_a_stranger_cannot_cancel_someone_elses_offer(client, tokens, передача):
    """Наблюдатель до правила доходит и получает 403, а не 401.

    Дверь ручки нарочно шире соседних (`conversations:read`): передавать умеет
    и руководитель, которому отправка закрыта, — повесь мы отмену на
    `messages:send`, он не смог бы отменить собственную передачу. Значит
    отсекать посторонних обязано правило внутри.
    """
    дано = await передача()

    r = await _cancel(client, дано["conv_id"], tokens["observer"])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["details"]["reason"] == "not_your_transfer"

    r = await client.post(f"/api/v1/conversations/{дано['conv_id']}/transfer/cancel")
    assert r.status_code == 401, "аноним не должен доходить до правила вовсе"


async def test_an_administrator_can_undo_someone_elses_transfer(client, tokens, передача):
    """Администратор отменяет чужое предложение — и это не поблажка.

    Тем же правом `conversations:manage` открыт `/assign`, а он предложение
    ПЕРЕЗАПИСЫВАЕТ: запрети мы отмену, тот же человек добился бы того же,
    назначив диалог третьему лицу, то есть более грубым способом.
    """
    дано = await передача()

    r = await _cancel(client, дано["conv_id"], tokens["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["transfer"] is None


async def test_cancelling_a_dialog_without_an_offer_is_refused(
    client, tokens, db_sessionmaker, make_avito_account, users_by_role
):
    """Отрицательная проверка: без предложения ручка отвечает 422 по своей причине.

    Данные подобраны так, чтобы диалог БЫЛ и был доступен нажимающему: иначе
    проверка зеленела бы на 404 по несуществующему id и не проверяла бы ничего.
    """
    account = await make_avito_account(avito_user_id=777001)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="cancel-0509-b", name="Без передачи")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="chat-cancel-0509-b",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=users_by_role["manager"].id,
        )
        s.add(row)
        await s.commit()
        conv_id = row.id

    r = await _cancel(client, conv_id, tokens["manager"])
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "no_pending_transfer"
