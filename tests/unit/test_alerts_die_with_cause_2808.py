"""ТРЕВОГА ГАСНЕТ ВМЕСТЕ С ПОВОДОМ — НА ВСЕХ ДОРОГАХ, А НЕ НА ТРЁХ ИЗ ЧЕТЫРЁХ.

⚠ ЖАЛОБА ВЛАДЕЛЬЦА 28.08: «сделай так, чтобы история уведомлений чистилась, а то
приходят все за всё время».

Замер боя в тот же день: 5193 уведомления, из них 2650 непрочитанных, и 2466 из
этих 2650 — «Диалог никто не принял». То есть 93% колокольчика составляли
тревоги о диалогах, которыми давно занялись.

ПОЧЕМУ ОНИ НЕ ГАСЛИ. Механизм гашения (`notifications.resolve_conversation`)
написан давно и позван из трёх мест: принятие из очереди, смена статуса руками,
назначение ответственного. А `conversations.ensure_in_progress` — вход для всех
АВТОМАТИЧЕСКИХ переходов «кто-то взялся за диалог», и среди них тот, которым
сегодня отвечают чаще всего: ответ, написанный в приложении Авито. Мимо гашения
проходила ровно та дорога, по которой идёт большинство.

Цена вранья записана в `cli.dismiss-stale-alerts`: «пока завал не разобран,
колокольчик бесполезен — его перестают открывать, и следующая настоящая тревога
опоздает ровно настолько, насколько человек привык не смотреть».
"""

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation
from app.models.notification import Notification
from app.services import conversations as convs
from app.services import notifications as notify_svc

pytestmark = pytest.mark.anyio


@pytest.fixture
async def диалог(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        клиент = Client(channel="avito", external_id="9401", name="Иван Петров")
        s.add(клиент)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-alerts",
            account_id=account.id,
            client_id=клиент.id,
            status="new",
        )
        s.add(conv)
        await s.commit()
        return conv.id


async def _тревога(db_sessionmaker, conv_id) -> None:
    async with db_sessionmaker() as s:
        await notify_svc.notify(
            s,
            kind="conversation.unclaimed",
            body="Откройте «Входящие» и возьмите его",
            entity_type="conversation",
            entity_id=str(conv_id),
        )
        await s.commit()


async def _живых(db_sessionmaker, conv_id) -> int:
    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.kind == "conversation.unclaimed",
                        Notification.entity_id == str(conv_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        return sum(1 for r in rows if r.read_at is None)


async def test_answering_from_the_avito_app_puts_out_the_alert(db_sessionmaker, диалог) -> None:
    """Ответ из приложения Авито — тоже «кто-то занялся».

    Это самая частая дорога в бою и единственная, которая мимо гашения и шла.
    """
    await _тревога(db_sessionmaker, диалог)
    assert await _живых(db_sessionmaker, диалог) == 1

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, диалог)
        assert await convs.ensure_in_progress(s, conv, source="external_reply", actor_id=None)
        await s.commit()

    assert await _живых(db_sessionmaker, диалог) == 0, (
        "тревога «Диалог никто не принял» пережила ответ клиенту — колокольчик "
        "снова копит новости о том, что давно сделано"
    )


async def test_a_dialog_nobody_touched_keeps_its_alert(db_sessionmaker, диалог) -> None:
    """Обратная граница: пока диалогом не занялись, тревога обязана висеть.

    Гасить её «за компанию» значило бы прятать живую беду — клиент ждёт, и
    никто про него не знает.
    """
    await _тревога(db_sessionmaker, диалог)

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, диалог)
        conv.status = "closed"  # мимо `ensure_in_progress`: тот трогает только `new`
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, диалог)
        # Диалог уже не `new` — переход не случается, гасить нечего.
        assert (
            await convs.ensure_in_progress(s, conv, source="external_reply", actor_id=None) is False
        )
        await s.commit()

    assert await _живых(db_sessionmaker, диалог) == 1


def test_every_road_into_in_progress_puts_out_the_alerts() -> None:
    """ПРОВОДКА: гашение позвано из ВСЕХ четырёх мест, а не из трёх.

    Без этой проверки следующая дорога «кто-то взялся за диалог» снова пройдёт
    мимо — так и получились 2466 висящих тревог.
    """
    import inspect
    import re

    from app.services import inbox as inbox_svc

    места = {
        "ensure_in_progress": inspect.getsource(convs.ensure_in_progress),
        "change_status": inspect.getsource(convs.change_status),
        "assign_conversation": inspect.getsource(convs.assign_conversation),
        "claim": inspect.getsource(inbox_svc.claim),
    }
    немые = [
        имя
        for имя, src in места.items()
        if "resolve_conversation" not in re.sub(r"#[^\n]*", " ", src)
    ]
    assert немые == [], f"дороги в «в работе», которые не гасят тревоги: {немые}"


async def test_the_client_returned_notice_goes_out_once_someone_answers(
    db_sessionmaker, диалог, users_by_role
) -> None:
    """«Клиент вернулся в закрытый диалог» не гас никогда: у менеджеров их
    копились тысячи, и весть о передаче тонула среди них."""
    manager = users_by_role["manager"]
    async with db_sessionmaker() as s:
        await notify_svc.notify(
            s,
            kind="conversation.reopened",
            recipient_id=manager.id,
            body="Клиент вернулся",
            entity_type="conversation",
            entity_id=str(диалог),
        )
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, диалог)
        assert await convs.ensure_in_progress(s, conv, source="external_reply", actor_id=None)
        await s.commit()

    async with db_sessionmaker() as s:
        (row,) = (
            await s.execute(
                sa.select(Notification).where(Notification.kind == "conversation.reopened")
            )
        ).scalars()
    assert row.read_at is not None
