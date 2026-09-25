"""Имя клиента, введённое руками (требование заказчика 13 августа).

ЗАЧЕМ ЭТИ ТЕСТЫ. Имя приходило только из профиля Авито, а профиль человек заводил один
раз и мог назвать как угодно: «Ак», «Продам всё», пусто. Диспетчер узнаёт настоящее имя
в разговоре — и деть его было некуда.

Половина проверок здесь не про запись, а про то, что записанное НЕ ПРОПАДЁТ. Имя
затирается тремя способами, и все три — обычная работа, а не крайний случай:
объединение карточек, разъединение и дозагрузка из Авито. Каждый затирает молча, и
заметить это можно только по следующему разговору с клиентом.
"""

import uuid

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client
from app.services import clients as clients_svc


@pytest.fixture
async def seeded(seed_conversation, db_sessionmaker):
    async with db_sessionmaker() as session:
        client = await session.get(Client, seed_conversation.client_id)
        assert client is not None
        client.name = None
        client.name_set_by_id = None
        client.name_set_at = None
        await session.commit()
    return seed_conversation


async def _put_name(client_http, token: str, client_id, name: str):
    return await client_http.put(
        f"/api/v1/clients/{client_id}/name",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}"},
    )


# --- запись -------------------------------------------------------------------


async def test_name_is_saved(client, tokens, seeded, db_sessionmaker):
    resp = await _put_name(client, tokens["manager"], seeded.client_id, "Ольга Никитина")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Ольга Никитина"
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None and row.name == "Ольга Никитина"


async def test_spaces_are_collapsed(client, tokens, seeded, db_sessionmaker):
    """Имя — одна строка: она стоит в списке чатов, в шапке диалога и в заявке."""
    await _put_name(client, tokens["manager"], seeded.client_id, "  Ольга   Никитина \n")
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None and row.name == "Ольга Никитина"


async def test_name_records_who_and_when(client, tokens, users_by_role, seeded, db_sessionmaker):
    """Без автора нельзя отличить имя из Авито от набранного человеком.

    А отличать обязательно: на этом признаке держатся все три защиты ниже.
    """
    await _put_name(client, tokens["manager"], seeded.client_id, "Ольга")
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        assert row.name_set_by_id == users_by_role["manager"].id
        assert row.name_set_at is not None


async def test_empty_name_clears_it(client, tokens, seeded, db_sessionmaker):
    """⚠ ОЧИСТКА РАЗРЕШЕНА НАМЕРЕННО. «Ак» из профиля хуже, чем ничего: под пустым
    именем карточка показывается как «Клиент», и это честно. Запрети очистку — и
    диспетчер обязан оставить заведомо неверное имя."""
    await _put_name(client, tokens["manager"], seeded.client_id, "Ак")
    await _put_name(client, tokens["manager"], seeded.client_id, "")
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        assert row.name is None
        # ⚠ Отметка ОСТАЁТСЯ. Она и означает «человек это трогал»; без неё Авито вернёт
        # стёртое имя обратно первой же дозагрузкой.
        assert row.name_set_at is not None


async def test_too_long_name_is_refused(client, tokens, seeded):
    """Длину режет схема запроса — до всякой работы с базой.

    Ответ 400, а не 422: это отказ разбора тела, общий для всего API. Свой понятный
    отказ сервис тоже умеет (см. тест ниже) — он страхует вызовы мимо ручки.
    """
    resp = await _put_name(client, tokens["manager"], seeded.client_id, "я" * 300)
    assert resp.status_code == 400


async def test_service_explains_the_limit_in_words(db_sessionmaker, seeded, users_by_role):
    """⚠ ВТОРОЙ СЛОЙ НЕ ЛИШНИЙ. Схема отбивает длину у ручки, но сервис зовут и мимо неё
    (импорт, будущие ручки), а «имя на триста знаков» ломает и список чатов, и заявку.
    Здесь же лежит текст, который человек сможет прочитать."""
    from app.core.errors import ApiError

    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        with pytest.raises(ApiError) as поймано:
            await clients_svc.set_name(session, row, "я" * 300, actor=users_by_role["manager"])
    assert "не поместится" in str(поймано.value.message)


async def test_same_name_twice_does_not_grow_the_journal(client, tokens, seeded, db_sessionmaker):
    """Повторное «Сохранить» с тем же текстом — не событие."""
    await _put_name(client, tokens["manager"], seeded.client_id, "Ольга")
    resp = await _put_name(client, tokens["manager"], seeded.client_id, "Ольга")
    assert resp.json()["changed"] is False
    async with db_sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.entity_id == str(seeded.client_id))
                )
            )
            .scalars()
            .all()
        )
    assert len([r for r in rows if r.action.startswith("client.name")]) == 1


async def test_first_entry_and_fix_are_different_events(client, tokens, seeded, db_sessionmaker):
    """«Вписали впервые» и «исправили» — разные вопросы через месяц, и разные строки."""
    await _put_name(client, tokens["manager"], seeded.client_id, "Оля")
    await _put_name(client, tokens["manager"], seeded.client_id, "Ольга Никитина")
    async with db_sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.entity_id == str(seeded.client_id))
                )
            )
            .scalars()
            .all()
        )
    actions = sorted(r.action for r in rows if r.action.startswith("client.name"))
    assert actions == ["client.name_captured", "client.name_edited"]


async def test_observer_cannot_rename(client, tokens, seeded):
    """Карточку правит тот, кто с этим клиентом разговаривает (право `conversations:manage`)."""
    resp = await _put_name(client, tokens["observer"], seeded.client_id, "Ольга")
    assert resp.status_code == 403


# --- имя не пропадает ---------------------------------------------------------


def _client(name=None, phone=None, set_at=None):
    c = Client(id=uuid.uuid4(), channel="avito", external_id=str(uuid.uuid4()))
    c.name, c.phone, c.name_set_at = name, phone, set_at
    return c


def test_merge_keeps_the_name_a_human_typed():
    """⚠ ЖИВОЙ СЛУЧАЙ, РАДИ КОТОРОГО ЗАВЕДЕНА ОТМЕТКА.

    Диспетчер вписал имя со слов клиента в карточку БЕЗ телефона, затем объединил её с
    карточкой того же человека с другого аккаунта, где телефон есть. Правило выбора имени
    («берём из карточки с телефоном») рассчитано на два имени из Авито и вернуло бы
    авитошное «Ак». Правка пропала бы молча.
    """
    from datetime import UTC, datetime

    победитель = _client(name="Ак", phone="+79125550177")
    проигравший = _client(name="Ольга Никитина", set_at=datetime.now(UTC))
    assert clients_svc._pick_name(победитель, проигравший) == "Ольга Никитина"


def test_merge_still_prefers_the_card_with_a_phone_when_both_came_from_avito():
    """Обратная сторона: старое правило обязано работать как работало.

    Оба имени из профиля — выбираем то, за которым стоит разговор, то есть телефон.
    """
    победитель = _client(name="Ак")
    проигравший = _client(name="Ольга", phone="+79125550177")
    assert clients_svc._pick_name(победитель, проигравший) == "Ольга"


def test_snapshot_carries_the_handmade_mark():
    """Разъединение обязано вернуть ТОЧНО исходное состояние.

    Верни оно имя, но потеряй отметку «ручное» — и следующее объединение затрёт имя
    снова, причём уже необъяснимо: в карточке всё выглядит правильно.
    """
    from datetime import UTC, datetime

    момент = datetime.now(UTC)
    снимок = clients_svc._snapshot(_client(name="Ольга", set_at=момент))
    assert снимок["name"] == "Ольга"
    assert снимок["name_set_at"] == момент.isoformat()
