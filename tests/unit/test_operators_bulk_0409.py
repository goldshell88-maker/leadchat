"""Массовое назначение операторов на каналы (просьба владельца 04.09).

⚠ ДОСЛОВНО: «продумай, как мне быстро можно было подключать или отключать людей
на всех аккаунтах — сейчас я вручную по 30 раз захожу и тыкаю, неудобно».

Замер боя объясняет, почему это болит: 35 каналов, 35 человек, и в среднем один
человек назначен на 29,7 канала из 35. Норма здесь — «почти все на почти всех»,
то есть единица работы ЧЕЛОВЕК, а весь прежний экран построен вокруг канала:
подключить одного ко всем стоило 64 нажатий.
"""

import uuid

import pytest
import sqlalchemy as sa

from app.models import AccountOperator, AuditLog


def auth(tokens, role="admin"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def каналы(make_avito_account):
    первый = await make_avito_account(avito_user_id=700000001, title="Канал А")
    второй = await make_avito_account(avito_user_id=700000002, title="Канал Б")
    return первый, второй


async def назначения(db_sessionmaker) -> set[tuple[uuid.UUID, uuid.UUID]]:
    async with db_sessionmaker() as s:
        rows = (await s.execute(sa.select(AccountOperator))).scalars().all()
        return {(r.account_id, r.user_id) for r in rows}


async def test_one_request_connects_a_person_to_every_channel(
    client, tokens, каналы, db_sessionmaker, users_by_role
):
    """Один запрос вместо похода по каждому каналу."""
    первый, второй = каналы
    менеджер = users_by_role["manager"]

    r = await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True},
                {"account_id": str(второй.id), "user_id": str(менеджер.id), "assigned": True},
            ]
        },
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2
    assert r.json()["accounts_touched"] == 2

    assert await назначения(db_sessionmaker) == {(первый.id, менеджер.id), (второй.id, менеджер.id)}


async def test_repeat_is_not_an_error(client, tokens, каналы, db_sessionmaker, users_by_role):
    """Повтор не роняет запрос и не заводит дубль.

    ⚠ БЕЗ ЭТОГО ДВОЙНОЙ ЩЕЛЧОК ИЛИ ДВА АДМИНИСТРАТОРА ДАЛИ БЫ ПЯТИСОТКУ:
    составной ключ запрещает дубль, а голая вставка на гонке падает. Отсюда
    вставка «при конфликте ничего не делать».
    """
    первый, _ = каналы
    менеджер = users_by_role["manager"]
    тело = {
        "changes": [{"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True}]
    }

    assert (
        await client.post("/api/v1/avito-accounts/operators/bulk", json=тело, headers=auth(tokens))
    ).status_code == 200
    r = await client.post("/api/v1/avito-accounts/operators/bulk", json=тело, headers=auth(tokens))
    assert r.status_code == 200, r.text

    assert len(await назначения(db_sessionmaker)) == 1


async def test_removing_the_last_operator_is_reported_as_opening_the_channel(
    client, tokens, каналы, db_sessionmaker, users_by_role
):
    """Ответ называет каналы, оставшиеся без живых операторов.

    ⚠ ПУСТОЙ НАБОР ОЗНАЧАЕТ «КАНАЛ ОТКРЫТ ВСЕМ», А НЕ «ЗАКРЫТ». Снятие
    последнего человека РАСШИРЯЕТ доступ: администратор думает, что сузил, а на
    деле показал канал всей смене. В поканальной модалке об этом предупреждают
    заранее; массовое действие обязано сказать то же самое по факту.
    """
    первый, _ = каналы
    менеджер = users_by_role["manager"]
    await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True}
            ]
        },
        headers=auth(tokens),
    )

    r = await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": False}
            ]
        },
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["opened_to_all"] == ["Канал А"]


async def test_a_channel_that_still_has_people_is_not_reported(
    client, tokens, каналы, db_sessionmaker, users_by_role
):
    """Обратная половина: пока на канале кто-то остался, тревоги нет."""
    первый, _ = каналы
    менеджер = users_by_role["manager"]
    админ = users_by_role["admin"]
    await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True},
                {"account_id": str(первый.id), "user_id": str(админ.id), "assigned": True},
            ]
        },
        headers=auth(tokens),
    )

    r = await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": False}
            ]
        },
        headers=auth(tokens),
    )
    assert r.json()["opened_to_all"] == []


async def test_a_person_who_cannot_answer_clients_is_refused(
    client, tokens, каналы, db_sessionmaker, users_by_role
):
    """Годность судится тем же правилом, что и на поканальном экране.

    Иначе массовый экран тихо завёл бы назначения, которых поканальный не
    допускает, и число на карточке разошлось бы с галочками.
    """
    первый, _ = каналы
    r = await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {
                    "account_id": str(первый.id),
                    "user_id": str(users_by_role["observer"].id),
                    "assigned": True,
                }
            ]
        },
        headers=auth(tokens),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "cannot_answer_clients"
    assert await назначения(db_sessionmaker) == set(), "часть пачки применилась"


async def test_every_touched_channel_gets_a_journal_line(
    client, tokens, каналы, db_sessionmaker, users_by_role
):
    """След в журнале — по строке на канал, тем же действием, что и правка руками.

    Разбор «почему обращения этого канала перестали приходить Петрову» ищет одну
    строку, а не догадывается по времени.
    """
    первый, второй = каналы
    менеджер = users_by_role["manager"]
    await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True},
                {"account_id": str(второй.id), "user_id": str(менеджер.id), "assigned": True},
            ]
        },
        headers=auth(tokens),
    )

    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "account.operators_changed")
                )
            )
            .scalars()
            .all()
        )
    assert {str(a.entity_id) for a in строки} == {str(первый.id), str(второй.id)}
    assert all(a.details["reason"] == "bulk" for a in строки)


async def test_the_grid_shows_channels_people_and_pairs(client, tokens, каналы, users_by_role):
    """Решётка приезжает одним ответом: каналы, люди и сырые пары назначений."""
    первый, _ = каналы
    менеджер = users_by_role["manager"]
    await client.post(
        "/api/v1/avito-accounts/operators/bulk",
        json={
            "changes": [
                {"account_id": str(первый.id), "user_id": str(менеджер.id), "assigned": True}
            ]
        },
        headers=auth(tokens),
    )

    r = await client.get("/api/v1/avito-accounts/operators/grid", headers=auth(tokens))
    assert r.status_code == 200, r.text
    данные = r.json()
    assert {a["title"] for a in данные["accounts"]} >= {"Канал А", "Канал Б"}
    assert {"account_id": str(первый.id), "user_id": str(менеджер.id)} in данные["assigned"]
    # Правило «кого можно назначить» — то же, что у поканального экрана.
    свои = {u["id"]: u for u in данные["users"]}
    assert свои[str(менеджер.id)]["can_be_operator"] is True
    assert свои[str(users_by_role["observer"].id)]["can_be_operator"] is False


async def test_only_the_admin_may_change_the_grid(client, tokens, каналы, users_by_role):
    """Менять состав вправе только администратор — как и на поканальном экране."""
    первый, _ = каналы
    тело = {
        "changes": [
            {
                "account_id": str(первый.id),
                "user_id": str(users_by_role["manager"].id),
                "assigned": True,
            }
        ]
    }
    for роль in ("head", "manager", "observer"):
        r = await client.post(
            "/api/v1/avito-accounts/operators/bulk", json=тело, headers=auth(tokens, роль)
        )
        assert r.status_code == 403, f"{роль}: {r.text}"
