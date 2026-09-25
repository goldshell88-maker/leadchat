"""ОТДЕЛ РЯДОМ С ИМЕНЕМ — «Петров Иван (Чатер)» (просьба владельца 04.09).

⚠ ЧТО БЫЛО. Отдел лежит в `users.department` с 7.4 и выходил наружу ровно в
одном месте — колонкой на вкладке «Команда». Во всех рабочих экранах человек
приезжал парой `{id, full_name}`: список диалогов, шапка ленты, автор реплики,
позванные, окно передачи, назначение на канал, «Разбор диалогов». В консоли
тринадцать человек из семи с лишним отделов, часть ведёт один и тот же тип
обращений, — и «кто это» по одному имени не читалось.

⚠ ПОЧЕМУ ПРОВЕРЯЕТСЯ СБОРКА, А НЕ КАЖДАЯ РУЧКА ПО ОТДЕЛЬНОСТИ. Ссылка на
человека собирается ОДНИМ помощником (`app/services/user_ref.py`) — ради того,
чтобы точка, добавленная через месяц, не осталась без отдела. Тесты ниже
стерегут именно это: сам помощник, и то, что боевые пути (лента, список,
список «кому передать») действительно ходят через него.

⚠ ЗАМЕР БОЯ, КОТОРЫЙ ЗДЕСЬ ЗАКРЕПЛЁН: отдел заполнен НЕ У ВСЕХ, и значения
разной аккуратности — рядом живут «Диспетчер МНЧ» и «Диспетчер  МНЧ» с двумя
пробелами. Пустой отдел обязан давать `null` (иначе на экране «Иванов ()»), а
двойной пробел — схлопываться (иначе один отдел выглядит как два).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.user_ref import normalize_department, user_ref, user_ref_parts

pytestmark = pytest.mark.anyio


def auth(tokens) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {tokens['admin']}"}


# --------------------------------------------------------------- сам помощник


def test_пустой_отдел_не_даёт_скобок() -> None:
    """`None`, пусто и пробелы — всё это «отдела нет», а не «отдел из пробелов».

    Заполнен он не у всех, и `{"department": ""}` на экране превратилось бы в
    «Иванов ()» — читается как сломанный экран, а не как «отдел не указан».
    """
    assert normalize_department(None) is None
    assert normalize_department("") is None
    assert normalize_department("   ") is None
    # Неразрывный пробел приезжает копипастой в поле ввода и на глаз неотличим.
    assert normalize_department("   ") is None


def test_лишние_пробелы_схлопываются() -> None:
    """«Диспетчер  МНЧ» и «Диспетчер МНЧ» — один отдел, а не два.

    Обе строки лежат в боевой базе рядом. Человек читает их одинаково, и
    подпись обязана выглядеть одинаково — иначе поиск по отделу («ОКК»,
    «Диспетчер МНЧ») находит половину людей.
    """
    assert normalize_department("Диспетчер  МНЧ") == "Диспетчер МНЧ"
    assert normalize_department("  ОКК  ") == "ОКК"
    assert normalize_department("СТАРШИЕ  -  ЧАТЫ") == "СТАРШИЕ - ЧАТЫ"


async def test_ссылка_несёт_отдел(make_user) -> None:  # noqa: ANN001
    человек = await make_user("ref@leadchat.test", full_name="Петров Иван", department=" Чатер ")
    assert user_ref(человек) == {
        "id": str(человек.id),
        "full_name": "Петров Иван",
        "department": "Чатер",
    }


def test_нет_человека_нет_ссылки() -> None:
    """«Ответственного нет» и «ответственный без отдела» — разные новости.

    Склеить их пустым объектом значило бы показать в списке безымянную строку
    вместо честного «Не назначен».
    """
    assert user_ref(None) is None


def test_подпись_без_учётной_записи() -> None:
    """«Автораздача» — автор без строки в `users`; скобок у неё нет и не будет."""
    assert user_ref_parts(None, "Автораздача") == {
        "id": "",
        "full_name": "Автораздача",
        "department": None,
    }


# ------------------------------------------------------ боевые пути наружу


async def test_ответственный_везёт_отдел_в_карточку_и_список(
    client,  # noqa: ANN001
    tokens,  # noqa: ANN001
    seed_conversation,  # noqa: ANN001
    make_user,  # noqa: ANN001
    в_сети,  # noqa: ANN001
) -> None:
    """Шапка ленты и строка списка подписывают человека одинаково.

    ⚠ ДВЕ ТОЧКИ В ОДНОМ ТЕСТЕ, И ЭТО НЕ ЛЕНЬ. Разъедься они — человек увидит
    «Ольга Ковалёва (ОКК)» в шапке и «Ольга Ковалёва» в списке и решит, что это
    двое разных. Ради этого ссылка и собирается одним помощником.
    """
    оператор = await make_user(
        "dept@leadchat.test", role="manager", full_name="Ольга Ковалёва", department="ОКК"
    )
    # Передать можно только тому, кто в сети (решение владельца 28.08).
    await в_сети(оператор)
    conv_id = str(seed_conversation.conversation_id)

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/assign",
        json={"assignee_id": str(оператор.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["assignee"]["department"] == "ОКК", (
        "у ответственного пропал отдел — шапка ленты подпишет его голым именем"
    )

    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert r.status_code == 200, r.text
    строка = next(row for row in r.json()["items"] if row["id"] == conv_id)
    assert строка["assignee"]["department"] == "ОКК", (
        "в списке отдела нет, а в карточке есть — один человек выглядит как двое"
    )


async def test_автор_исходящей_реплики_везёт_отдел(make_user) -> None:  # noqa: ANN001
    """Подпись автора в пузыре ленты — та же сборка, что у ответственного.

    Проверяется сериализатор, а не ручка: отправка реплики уходит в Авито, и
    городить вокруг неё заглушку ради одного поля значило бы проверять
    заглушку. `message_out` — ровно то место, через которое реплика приезжает и
    в ответ ручки, и в кадр `message:new`.
    """
    from app.models import Message
    from app.services.conversations import message_out

    автор = await make_user(
        "author@leadchat.test", full_name="Ольга Ковалёва", department="Диспетчер  МНЧ"
    )
    msg = Message(
        conversation_id=uuid4(),
        direction="out",
        sender_type="operator",
        body="Здравствуйте! Подскажу по ремонту.",
        attachments=[],
        delivery_status="sent",
        created_at=datetime.now(UTC),
    )
    msg.id = uuid4()

    отдано = message_out(msg, автор)

    assert отдано["sender"]["department"] == "Диспетчер МНЧ", (
        "у автора реплики нет отдела (или он с двумя пробелами) — лента подпишет "
        "его иначе, чем шапка того же диалога"
    )


async def test_кому_передать_знает_отдел(client, tokens, make_user) -> None:  # noqa: ANN001
    """Окно «Передать диалог»: выбирают по отделу, а не только по имени.

    Отдел заполнен не у всех — и вторая строка здесь ровно про это: у человека
    без отдела приезжает `null`, а не пустая строка.
    """
    await make_user(
        "okk@leadchat.test", role="manager", full_name="Ольга Ковалёва", department="ОКК"
    )
    await make_user("nodep@leadchat.test", role="manager", full_name="Пётр Ковалёв")

    r = await client.get("/api/v1/users/assignable", headers=auth(tokens))
    assert r.status_code == 200, r.text
    строки = {row["full_name"]: row for row in r.json()["items"]}

    assert строки["Ольга Ковалёва"]["department"] == "ОКК", (
        "в списке «кому передать» пропал отдел — диалог уходит не в тот отдел"
    )
    assert строки["Пётр Ковалёв"]["department"] is None, (
        "у человека без отдела приехала пустая строка — на экране будет «Пётр Ковалёв ()»"
    )


async def test_отдел_чистится_при_записи(client, tokens, make_user) -> None:  # noqa: ANN001
    """Два пробела в поле «Отдел» не должны доживать до базы.

    Показ схлопывает их у себя, но это последняя защита: по отделу ещё
    фильтруют и ищут, а «Диспетчер  МНЧ» и «Диспетчер МНЧ» — два разных
    значения для любого сравнения строк.
    """
    человек = await make_user("wr@leadchat.test", role="manager", full_name="Игорь Т")

    r = await client.patch(
        f"/api/v1/users/{человек.id}",
        json={"department": "Диспетчер  МНЧ"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/users", headers=auth(tokens))
    assert r.status_code == 200, r.text
    строка = next(row for row in r.json()["items"] if row["full_name"] == "Игорь Т")
    assert строка["department"] == "Диспетчер МНЧ", (
        "в базу уехали два пробела — по отделу перестанет находиться половина людей"
    )
