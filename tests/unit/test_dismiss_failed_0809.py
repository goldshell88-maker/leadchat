"""«Снять» неотправленное с учёта (жалоба владельца 08.09).

⚠ ЖАЛОБА ДОСЛОВНО: «статус „не отправлено“ не пропадает».

Красную метку диалога («ответ не ушёл») ставит `refresh_undelivered` по
наличию сообщения со статусом `failed`. Выхода из этого состояния было ровно
два: удачный повтор или ничего. А повтор помогает не всегда — канал Авито мог
отвалиться, диалог закрыться, текст устареть. Живой случай со снимка: оператор
не стал повторять, а набрал тот же текст заново и отправил новым сообщением.
Оно ушло, старое осталось `failed`, и метка повисла на диалоге навсегда.

Метка, которая не гаснет, перестаёт значить хоть что-то: человек перестаёт её
замечать за неделю — и она не сработает в тот единственный раз, ради которого
заведена. Ровно об этом предупреждает шапка самой `refresh_undelivered`.

⚠ ГЛАВНОЕ ЗДЕСЬ — НЕ «ГАСНЕТ ЛИ МЕТКА», А ТО, ЧТО СООБЩЕНИЕ ОСТАЁТСЯ. Снятие
не удаление: клиент ответа не получил, и это факт разговора, который может
понадобиться при разборе. Проверка, смотрящая только на метку, зеленела бы и у
кода, который сообщение просто стирает.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.models import Conversation, Message
from tests.unit.test_send_message import _make_failed_message, auth

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(app: FastAPI):
    """Тот же клиент поверх того же приложения, что и у соседей по отправке."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_dismiss_clears_the_mark_and_keeps_the_message(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    # Метка на диалоге появляется вместе с неотправленным — иначе снимать нечего.
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        from app.services.messages import refresh_undelivered

        await refresh_undelivered(s, conv)
        await s.commit()
        assert conv.undelivered_at is not None

    r = await api.post(f"/api/v1/messages/{msg.id}/dismiss", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "dismissed"

    async with db_sessionmaker() as s:
        строка = await s.get(Message, (msg.id, msg.created_at))
        assert строка is not None, "сообщение удалили — снятие не должно стирать переписку"
        assert строка.delivery_status == "dismissed"
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv.undelivered_at is None, "красная метка диалога не погасла"


async def test_dismiss_of_a_delivered_message_is_422(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    """Снимать можно только неотправленное: иначе кнопка стирала бы историю.

    ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ЗДЕСЬ ОБЯЗАТЕЛЬНА. Условие `direction == "out" and
    delivery_status == "failed"` написать слишком широко ничего не стоит, а
    последствие — доставленное сообщение, помеченное как неотправленное.
    """
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    async with db_sessionmaker() as s:
        строка = await s.get(Message, (msg.id, msg.created_at))
        строка.delivery_status = "delivered"
        await s.commit()

    r = await api.post(f"/api/v1/messages/{msg.id}/dismiss", headers=auth(tokens))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "not_failed"


async def test_dismiss_is_not_allowed_to_a_stranger(
    api, tokens, seed_conversation, db_sessionmaker, users_by_role
):
    """Снять может автор или админ — как и повторить.

    Право то же самое и по той же причине: снятие меняет ЧУЖУЮ отметку о том,
    дошёл ли ответ клиенту, и решать это тому, кто отвечал.
    """
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    # Руководитель — не автор и не админ: снять чужое он не вправе.
    r = await api.post(f"/api/v1/messages/{msg.id}/dismiss", headers=auth(tokens, "head"))
    assert r.status_code in (403, 404), r.text
