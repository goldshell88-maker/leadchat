"""Передача диалога с подтверждением получателя.

Требование заказчика от 7 августа: «диалог не считается переданным, если
другой сотрудник его не принял».

Главное, что проверяется, — НЕ «меняется ли ответственный после принятия».
Это просто. Проверяется свойство, ради которого всё затевалось:
ОТВЕТСТВЕННОСТЬ НЕ ПОВИСАЕТ В ВОЗДУХЕ. Пока предложение не принято, диалог
числится за передающим — он в его «Моих», он отвечает клиенту, с него и
спрос. Ошибка здесь тише всех остальных: она не даёт ни исключения, ни
пустого экрана, только клиента, которого оба считают чужим.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.core.security import create_access_token
from app.models import AuditLog, Client, Conversation
from app.services import conversations as convs
from app.services import transfer as transfer_svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pair(make_user):
    """Двое: тот, кто передаёт, и тот, кому передают."""
    giver = await make_user("giver@leadchat.test", role="manager", full_name="Анна Отдающая")
    taker = await make_user("taker@leadchat.test", role="manager", full_name="Борис Принимающий")
    return giver, taker


@pytest.fixture
async def conv(db_sessionmaker, make_avito_account, pair):
    giver, _ = pair
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id="tr-1", name="Клиент Передачи")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="tr-chat-1",
            account_id=account.id,
            client_id=client.id,
            status="in_progress",
            assignee_id=giver.id,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def _assign(db_sessionmaker, conv_id, *, actor, assignee, comment=None):
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        msg = await convs.assign_conversation(
            s, row, assignee=assignee, actor=actor, comment=comment
        )
        await s.commit()
        return msg


async def test_the_dialog_stays_with_the_giver_until_accepted(db_sessionmaker, conv, pair):
    """ГЛАВНОЕ СВОЙСТВО: пока не приняли, диалог у передающего.

    Иначе ответственность повисает между двумя людьми: передавший считает,
    что дело сделано, принимающий — что ему ничего не давали, а клиент ждёт.
    """
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == giver.id, "ответственный не меняется до принятия"
        assert row.transfer_to_id == taker.id, "но предложение видно"
        assert row.transfer_by_id == giver.id


async def test_accepting_moves_the_dialog(db_sessionmaker, conv, pair):
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        previous = transfer_svc.accept(row, actor=taker)
        await s.commit()

    assert previous == giver.id
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == taker.id
        assert row.transfer_to_id is None, "предложение снято"
        assert row.transfer_comment is None, "и объяснение к нему тоже"


async def test_declining_leaves_it_with_the_giver(db_sessionmaker, conv, pair):
    """Отказ возвращает всё как было — и передавший об этом узнаёт.

    Молчаливый отказ означал бы клиента, которого оба считают чужим.
    """
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        notify = transfer_svc.decline(row, actor=taker)
        await s.commit()

    assert notify == giver.id, "сообщить надо тому, кто ждал ответа"
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == giver.id
        assert row.transfer_to_id is None


async def test_only_the_recipient_can_answer(db_sessionmaker, conv, pair, make_user):
    """Ответить за предложение может только тот, кому его сделали.

    Иначе коллега «помог» бы принять диалог за отсутствующего — и тот
    вернулся бы к работе, которую не брал.
    """
    giver, taker = pair
    stranger = await make_user("stranger@leadchat.test", role="manager")
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        with pytest.raises(ApiError) as err:
            transfer_svc.accept(row, actor=stranger)
        assert err.value.status == 403


async def test_answering_a_nonexistent_transfer_is_refused(db_sessionmaker, conv, pair):
    giver, taker = pair
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        with pytest.raises(ApiError) as err:
            transfer_svc.accept(row, actor=taker)
        assert err.value.status == 422


async def test_taking_it_yourself_is_still_instant(db_sessionmaker, conv, pair, make_user):
    """«Взять себе» подтверждения не требует: человек уже согласен.

    Сделай мы двухфазным и это — оператор нажимал бы «взять», а потом ещё
    и «принять» у самого себя.
    """
    _, taker = pair
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await convs.assign_conversation(s, row, assignee=taker, actor=taker)
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == taker.id, "взял себе — сразу ведёт"
        assert row.transfer_to_id is None


async def test_assigning_an_unowned_dialog_is_still_instant(
    db_sessionmaker, make_avito_account, pair
):
    """Раздача диалога БЕЗ ответственного — не передача.

    Отнимать не у кого, и ждать подтверждения значило бы оставить клиента без
    ответа ради формальности.
    """
    giver, taker = pair
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id="tr-2", name="Ничей")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="tr-chat-2",
            account_id=account.id,
            client_id=client.id,
            status="new",
        )
        s.add(row)
        await s.commit()
        conv_id = row.id

    await _assign(db_sessionmaker, conv_id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        assert row.assignee_id == taker.id, "ничей диалог назначается сразу"
        assert row.transfer_to_id is None


async def test_the_comment_travels_with_the_offer(db_sessionmaker, conv, pair):
    """«Почему передаю» нужнее самого факта передачи.

    «Клиент из Балашихи, это твой район» объясняет предложение, а голое
    «вам передан диалог» — нет.
    """
    giver, taker = pair
    await _assign(
        db_sessionmaker, conv.id, actor=giver, assignee=taker, comment="Клиент из Балашихи"
    )
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.transfer_comment == "Клиент из Балашихи"


async def test_an_expired_offer_is_found_by_the_watchdog(db_sessionmaker, conv, pair):
    """Предложение, провисевшее слишком долго, находится сторожем.

    «Висит вечно» здесь равно «потеряли»: передавший считает, что отдал, и
    больше на диалог не смотрит.
    """
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv.id)
            .values(
                transfer_at=datetime.now(UTC)
                - timedelta(minutes=transfer_svc.TRANSFER_TIMEOUT_MINUTES + 1)
            )
        )
        await s.commit()

    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(Conversation).where(transfer_svc.expired_condition())))
            .scalars()
            .all()
        )
    assert [r.id for r in rows] == [conv.id]


async def test_the_watchdog_clears_the_expired_offer(
    db_sessionmaker, conv, pair, monkeypatch, redis
):
    """Сторож снимает просроченное предложение и не трогает диалог.

    Ответственный остаётся прежним — он им и был всё это время. Иначе
    получилось бы наказание за чужое молчание: передающий отдал диалог,
    получатель промолчал, а диалог ушёл в никуда.
    """
    from app.scheduler.jobs import reclaim

    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv.id)
            .values(
                transfer_at=datetime.now(UTC)
                - timedelta(minutes=transfer_svc.TRANSFER_TIMEOUT_MINUTES + 1)
            )
        )
        await s.commit()

    published: list[tuple[str, dict]] = []

    async def fake_publish(_redis, event, data):
        published.append((event, data))

    monkeypatch.setattr(reclaim, "publish_event", fake_publish)
    monkeypatch.setattr(reclaim.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(reclaim.redis_mod, "get_client", lambda: redis)

    assert await reclaim.expire_transfers() == 1

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.transfer_to_id is None, "предложение снято"
        assert row.assignee_id == giver.id, "диалог остался у того, за кем числился"

    # Кадр обязателен: без него передающий и через час видел бы «ждёт
    # подтверждения» от человека, которого уже никто не ждёт.
    assert published and published[0][1]["patch"] == {"transfer": None}


async def test_a_fresh_offer_is_left_alone(db_sessionmaker, conv, pair):
    """Свежее предложение сторож не трогает — человек ещё может ответить."""
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(Conversation).where(transfer_svc.expired_condition())))
            .scalars()
            .all()
        )
    assert rows == []


async def test_the_recipient_is_notified(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account, в_сети
):
    """Получатель узнаёт о предложении из центра уведомлений.

    Это не «приятное дополнение». Пока предложение не принято, диалог не
    появляется НИ в «Моих» получателя, НИ в очереди — увидеть его негде.
    Без уведомления человек узнаёт о переданном диалоге, только если случайно
    откроет его, и двухфазная передача превращается в способ терять диалоги
    вместо способа их не терять.

    Вид уведомления был объявлен в каталоге центра с самого начала («Вам
    передали диалог»), но никто его не порождал: строка была мертва.
    """
    from app.models.notification import Notification

    admin = users_by_role["admin"]
    manager = users_by_role["manager"]
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="tr-n", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="tr-chat-n",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=admin.id,
        )
        s.add(row)
        await s.commit()
        conv_id = row.id

    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(manager)
    r = await client.post(
        f"/api/v1/conversations/{conv_id}/assign",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
        json={"assignee_id": str(manager.id), "comment": "Твой район"},
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(Notification).where(Notification.kind == "conversation.assigned")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "получателю обязано прийти уведомление"
    assert rows[0].recipient_id == manager.id
    assert "передаёт вам диалог" in (rows[0].body or "")


async def test_the_offer_is_recorded(db_sessionmaker, conv, pair):
    """Предложение, принятие и отказ — три разных события в журнале.

    Одного «передан» мало: вопрос «почему клиент ждал сорок минут»
    разрешается только тем, видно ли, что предложение висело непринятым.
    """
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "conversation.transfer_offered")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details["to_id"] == str(taker.id)


async def test_the_api_shows_the_pending_offer(db_sessionmaker, conv, pair):
    """Предложение видно в диалоге — иначе получатель не увидит кнопок.

    Едет оно в КАЖДОМ диалоге, а не только в списке: по прямой ссылке
    /chats/{id} список не смонтирован вовсе.
    """
    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker, comment="Твой район")
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        detail = await convs.conversation_detail(s, row)

    assert detail["transfer"] is not None
    assert detail["transfer"]["to"]["id"] == str(taker.id)
    assert detail["transfer"]["to"]["full_name"] == "Борис Принимающий"
    assert detail["transfer"]["by"]["full_name"] == "Анна Отдающая"
    assert detail["transfer"]["comment"] == "Твой район"
    # Ответственный в выдаче — по-прежнему передающий.
    assert detail["assignee"]["id"] == str(giver.id)


async def test_no_offer_means_no_field(db_sessionmaker, conv):
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        detail = await convs.conversation_detail(s, row)
    assert detail["transfer"] is None


async def test_stale_offer_cannot_overwrite_a_new_owner(db_sessionmaker, conv, pair, make_user):
    """Диалог забрали, пока предложение висело, — принять его уже нельзя.

    БОЕВОЙ СЦЕНАРИЙ (аудит 19.08, находка L-009). Предложение живёт 15 минут и
    переживает возврат диалога в очередь. Иван предложил диалог Петру, диалог
    вернулся в очередь и его взял Сергей — а Пётр в это время нажал «Принять».
    Проверялось ровно два условия («ты ли адресат» и «не закрыт ли»), поэтому
    принятие переписывало ответственного ПОВЕРХ Сергея: диалог оказывался у
    двоих по очереди, Сергей молча терял работу, а клиент получал ответы от
    разных людей. На тринадцати диспетчерах это вопрос времени.
    """
    giver, taker = pair
    третий = await make_user("sergey.third@leadchat.local", role="manager")
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    async with db_sessionmaker() as s:  # диалог забрал кто-то другой
        row = await s.get(Conversation, conv.id)
        row.assignee_id = третий.id
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        with pytest.raises(ApiError) as отказ:
            transfer_svc.accept(row, actor=taker)
        assert отказ.value.code == "unprocessable"
        assert отказ.value.details.get("reason") == "owner_changed"
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == третий.id, "диалог обязан остаться у того, кто его взял"
        assert row.transfer_to_id is None, "устаревшее предложение снимается"


async def test_giver_is_told_about_the_refusal(db_sessionmaker, conv, pair, client):
    """Отказался коллега — передавший обязан узнать, и не кадром, а уведомлением.

    ЧТО БЫЛО (аудит 19.08). Сервис отказа честно возвращает, кому сообщить, и в
    его докстринге написано зачем: «он ждёт ответа и должен узнать, что диалог
    по-прежнему его, — иначе решит, что передал, и клиент останется без
    ответа». А сообщать было НЕЧЕМ: ручка публиковала кадр с пометкой
    `for_user_id`, но хаб персонализирует только назначение, и фронт этот
    признак у обновления диалога не читает. Передавший не узнавал ничего.

    Уведомление выбрано намеренно вместо кадра: оно переживает и обрыв связи, и
    закрытую вкладку — а именно в этот момент человек чаще всего смотрит в
    другой диалог.
    """
    from app.models import Notification

    giver, taker = pair
    await _assign(db_sessionmaker, conv.id, actor=giver, assignee=taker)

    ответ = await client.post(
        f"/api/v1/conversations/{conv.id}/transfer/decline",
        headers={
            "Authorization": "Bearer " + create_access_token(user_id=str(taker.id), role=taker.role)
        },
    )
    assert ответ.status_code == 200, ответ.text

    async with db_sessionmaker() as s:
        строки = list(
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.kind == "conversation.transfer_declined"
                    )
                )
            ).scalars()
        )
    assert len(строки) == 1, "передавший не узнал об отказе"
    assert строки[0].recipient_id == giver.id, "весть ушла не тому"
    assert "Борис Принимающий" in (строки[0].body or ""), "в тексте обязано быть имя отказавшегося"
