"""Результата обращения БОЛЬШЕ НЕТ: закрытие одношаговое (решение 12 августа).

ЧТО ЗДЕСЬ БЫЛО. Набор проверок функции «Чем закончилось обращение?» (план 21,
C1): справочник из пяти исходов, сумма в копейках, запрет суммы без выезда,
запрет исхода без закрытия, ограничение базы из миграции 0032.

ПОЧЕМУ ФАЙЛ НЕ УДАЛЁН ВМЕСТЕ С ФУНКЦИЕЙ. Снятие функции — тоже поведение, и
оно должно быть охраняемым. Ручка `PATCH /status` принимает JSON, а pydantic по
умолчанию лишние ключи ИГНОРИРУЕТ: без `extra="forbid"` старый клиент (открытая
вкладка, десктоп без обновления, чей-то скрипт) продолжал бы слать `outcome` —
и получал бы 200 в ответ на запрос, который система молча не исполнила. Тихое
«ок» на невыполненное — худший из возможных ответов, и в этом продукте он уже
стоил доверия к числам.

ЦЕНА РЕШЕНИЯ, ЗАПИСАННАЯ ЗДЕСЬ НАМЕРЕННО. Вместе с исходом ушла
`outcome_amount` — единственное место во всей системе, где появлялась сумма
заказа. Пока её не заполняют, продукт не отвечает на вопрос «какой канал
окупается»: считаются обращения и скорость, но не деньги.

КОЛОНКИ В БАЗЕ ОСТАЛИСЬ вместе с накопленными значениями, и ограничение
`ck_conversations_outcome` (миграция 0032) тоже — оно разрешает NULL и потому
ничему не мешает. Это проверяется ниже: вернуть функцию должно быть можно
правкой кода, а не восстановлением из копии.
"""

import re

import pytest

from app.models import Client, Conversation

pytestmark = pytest.mark.anyio


def _as(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def make_conversation(db_sessionmaker, make_avito_account):
    """Диалог в нужном статусе. Своя фикстура: соседние наборы тянут за собой
    историю и второго клиента, а здесь нужен ровно один разговор."""

    async def _make(*, status: str = "in_progress") -> Conversation:
        account = await make_avito_account()
        async with db_sessionmaker() as s:
            client_row = Client(
                channel="avito", external_id=f"out-{status}-{id(account)}", name="Иван"
            )
            s.add(client_row)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-out-{id(client_row)}",
                account_id=account.id,
                client_id=client_row.id,
                status=status,
            )
            s.add(conv)
            await s.commit()
            await s.refresh(conv)
            # Возвращаем ИДЕНТИФИКАТОР, а не объект: он заведён в другой
            # сессии, и обновлять его в тестовой нельзя — SQLAlchemy честно
            # ругается на чужой экземпляр.
            return conv.id

    return _make


# ------------------------------------------------- закрытие стало одношаговым


async def test_closing_needs_nothing_but_the_status(client, tokens, db, make_conversation):
    """Закрыть можно одним полем — и колонки исхода остаются пустыми.

    Главная проверка файла. Раньше здесь же проверялось, что вместе со статусом
    уезжает `outcome`; теперь важно обратное — что закрытие проходит БЕЗ него и
    ничего в колонки не пишет.
    """
    conv_id = await make_conversation(status="in_progress")

    resp = await client.patch(
        f"/api/v1/conversations/{conv_id}/status",
        json={"status": "closed"},
        headers=_as("manager", tokens),
    )
    assert resp.status_code == 200, resp.text

    conv = await db.get(Conversation, conv_id)
    assert conv.status == "closed"
    assert conv.outcome is None
    assert conv.outcome_amount is None
    assert conv.outcome_at is None
    assert conv.outcome_by_id is None


async def test_the_answer_never_mentions_the_outcome(client, tokens, make_conversation):
    """Ни в детали, ни в строке списка полей исхода больше нет.

    Поле в ответе, которое всегда `null`, — это обещание, что функция жива.
    Фронт по такому полю рисует пустой блок, а следующий разработчик тратит
    полдня на поиск места, где оно заполняется.
    """
    conv_id = await make_conversation(status="in_progress")

    detail = await client.get(f"/api/v1/conversations/{conv_id}", headers=_as("manager", tokens))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    for field in ("outcome", "outcome_amount", "snoozed_until", "snoozed_by", "snooze_reason"):
        assert field not in body, f"деталь диалога всё ещё отдаёт {field}"

    listing = await client.get("/api/v1/conversations", headers=_as("manager", tokens))
    assert listing.status_code == 200, listing.text
    for row in listing.json()["items"]:
        for field in ("outcome", "outcome_amount", "snoozed_until"):
            assert field not in row, f"строка списка всё ещё отдаёт {field}"


async def test_a_stale_client_sending_an_outcome_is_refused_not_ignored(
    client, tokens, db, make_conversation
):
    """Старый клиент со своим `outcome` получает отказ, а не тихое «ок».

    ЗАЧЕМ ЭТО ОТДЕЛЬНЫМ ТЕСТОМ. Открытые вкладки и десктопы обновляются не в
    момент выката, и запросы прежней формы будут идти ещё сутки. У pydantic по
    умолчанию лишние ключи просто отбрасываются — то есть диспетчер выбрал бы
    «Выезд назначен», получил 200 и ушёл, а в базе не осталось бы ничего.
    Отказ здесь честнее: он виден, и по нему обновляют вкладку.

    400, а не 422: это отказ РАЗБОРА тела (`extra="forbid"` у `StatusPatch`), а
    не отказ по состоянию диалога. Проверяется и код поля — иначе тест прошёл
    бы на любой чужой ошибке валидации, например на опечатке в статусе.
    """
    conv_id = await make_conversation(status="in_progress")

    resp = await client.patch(
        f"/api/v1/conversations/{conv_id}/status",
        json={"status": "closed", "outcome": "visit", "outcome_amount_rub": 4500},
        headers=_as("manager", tokens),
    )
    assert resp.status_code == 400, resp.text
    fields = resp.json()["error"]["details"]["fields"]
    assert {f["field"] for f in fields} == {"outcome", "outcome_amount_rub"}
    assert all(f["rule"] == "extra_forbidden" for f in fields), fields

    # И диалог остался НЕЗАКРЫТЫМ: отвергнутый запрос не исполняется наполовину.
    conv = await db.get(Conversation, conv_id)
    assert conv.status == "in_progress"
    assert conv.outcome is None


# --------------------------------------------- данные и ограничение остались


def test_the_columns_are_still_there_so_the_feature_can_come_back() -> None:
    """Колонки исхода на месте — снос был бы необратим.

    Владелец отказался от функции, а не от накопленного. Если эти четыре
    колонки кто-нибудь «приберёт» следующей миграцией, вернуть «Чем
    закончилось» можно будет только из резервной копии.
    """
    columns = set(Conversation.__table__.columns.keys())
    assert {"outcome", "outcome_amount", "outcome_at", "outcome_by_id"} <= columns


def test_the_catalog_constraint_survives_and_still_allows_null() -> None:
    """`ck_conversations_outcome` (0032) остался и не мешает.

    Ограничение перечисляет пять исходов и разрешает NULL. Вторая половина
    здесь ключевая: без неё оно запретило бы все диалоги подряд, потому что
    заполнять колонку теперь некому.
    """
    checks = [c for c in Conversation.__table__.constraints if hasattr(c, "sqltext")]
    outcome_check = [c for c in checks if "outcome IN" in str(c.sqltext)]
    assert outcome_check, "с conversations.outcome пропал CheckConstraint"

    sqltext = str(outcome_check[0].sqltext)
    assert "outcome IS NULL" in sqltext
    assert set(re.findall(r"'(\w+)'", sqltext)) == {
        "visit",
        "declined",
        "not_our_profile",
        "spam",
        "no_reply",
    }


def test_no_outcome_catalog_is_left_in_the_code() -> None:
    """Справочника исходов в коде не осталось ни одного экземпляра.

    Он жил в `services/conversations.py` (`OUTCOMES` / `OUTCOME_LABELS`) и во
    фронтовом `features/chats/outcomes.ts`. Пока значения перечислены где-то в
    коде, «вернуть на минутку одно поле» выглядит безобидной правкой — и
    функция просачивается обратно по частям.
    """
    from app.services import conversations as convs

    assert not hasattr(convs, "OUTCOMES")
    assert not hasattr(convs, "OUTCOME_LABELS")
