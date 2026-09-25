"""Закрепить диалог у себя (требование заказчика от 7 августа).

«Каждый может для себя лично закреплять свои взятые диалоги».

Проверяется не «пишется ли строка». Проверяются три границы, каждая из
которых при ослаблении делает отметку бесполезной:

1. ЛИЧНОЕ. Закрепление одного не поднимает диалог у остальных. Иначе через
   неделю наверху списка у всех висит десяток чужих закладок.
2. ТОЛЬКО СВОИ. Чужой диалог закрепить нельзя: отметка полезна тем, что
   показывает СВОЮ работу.
3. ПОТОЛОК. Двадцать закреплённых — это просто ещё один список, только
   сверху.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.models import Client, Conversation, ConversationPin
from app.services import conversations as convs
from app.services import participants, pins

pytestmark = pytest.mark.anyio


@pytest.fixture
async def two(make_user):
    mine = await make_user("pin-mine@leadchat.test", role="manager", full_name="Мой Оператор")
    other = await make_user("pin-other@leadchat.test", role="manager", full_name="Чужой Оператор")
    return mine, other


async def _conv(db_sessionmaker, account, owner, tag: str) -> uuid.UUID:
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"pin-{tag}", name=f"Клиент {tag}")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id=f"pin-chat-{tag}",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=owner.id if owner else None,
        )
        s.add(row)
        await s.commit()
        return row.id


async def test_a_pin_belongs_to_one_person_only(db_sessionmaker, make_avito_account, two):
    """ГЛАВНОЕ: закрепил один — у остальных ничего не изменилось.

    Общее закрепление на тринадцать человек превращается в свалку за неделю:
    закрепляют все, снимает никто, и наверху висит десяток чужих закладок.
    """
    mine, other = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, mine, "own")

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        await pins.pin(s, row, mine)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await pins.pinned_ids(s, mine.id) == {conv_id}
        assert await pins.pinned_ids(s, other.id) == set()


async def test_someone_elses_dialog_cannot_be_pinned(db_sessionmaker, make_avito_account, two):
    """Отметка полезна тем, что показывает СВОЮ работу.

    Закрепить чужой диалог — значит поднять наверх своего списка то, чего ты
    не ведёшь и вести не будешь. Для наблюдения есть поиск и вкладка «Все».
    """
    mine, other = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, other, "alien")

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        with pytest.raises(ApiError) as err:
            await pins.pin(s, row, mine)
        assert err.value.status == 422
        assert err.value.details["reason"] == "not_mine"


async def test_a_dialog_i_was_invited_to_counts_as_mine(db_sessionmaker, make_avito_account, two):
    """Позвали посмотреть — диалог для меня свой настолько, чтобы закрепить.

    Иначе позванный мастер не может отметить у себя то, ради чего его и
    позвали, и через час ищет диалог поиском.
    """
    mine, other = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, other, "invited")

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        await participants.invite(s, row, who=mine, actor=other)
        await pins.pin(s, row, mine)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await pins.pinned_ids(s, mine.id) == {conv_id}


async def test_the_cap_holds(db_sessionmaker, make_avito_account, two):
    """Двадцать закреплённых — это просто ещё один список, только сверху."""
    mine, _ = two
    account = await make_avito_account()
    ids = [await _conv(db_sessionmaker, account, mine, f"cap{i}") for i in range(pins.MAX_PINS + 1)]

    async with db_sessionmaker() as s:
        for conv_id in ids[: pins.MAX_PINS]:
            await pins.pin(s, await s.get(Conversation, conv_id), mine)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as err:
            await pins.pin(s, await s.get(Conversation, ids[-1]), mine)
        assert err.value.status == 422
        assert err.value.details["reason"] == "too_many_pins"


async def test_pinning_twice_is_harmless(db_sessionmaker, make_avito_account, two):
    """Двойное нажатие — не ошибка и не второй закреплённый."""
    mine, _ = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, mine, "twice")

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        await pins.pin(s, row, mine)
        await pins.pin(s, row, mine)
        await s.commit()

    async with db_sessionmaker() as s:
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(ConversationPin)
                .where(ConversationPin.user_id == mine.id)
            )
        ).scalar_one()
    assert n == 1


async def test_pinned_rises_above_everything_in_my_list(db_sessionmaker, make_avito_account, two):
    """Закрепление — ПЕРВЫЙ ключ порядка, выше свежести.

    Человек сам сказал, что это важно, и система не должна с ним спорить.

    Прежде тест назывался «выше даже непрочитанного»: непрочитанное было
    отдельным ключом сортировки. Ключа больше нет (см. докстринг
    `list_conversations`), поэтому «шумный» диалог здесь поднимает не счётчик,
    а то же, что и всех, — последнее сообщение.
    """
    mine, _ = two
    account = await make_avito_account()
    quiet = await _conv(db_sessionmaker, account, mine, "quiet")
    loud = await _conv(db_sessionmaker, account, mine, "loud")

    async with db_sessionmaker() as s:
        # «Шумный» свежее и непрочитан — без закрепления он был бы первым.
        row = await s.get(Conversation, loud)
        row.unread_count = 5
        row.last_message_at = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        quiet_row = await s.get(Conversation, quiet)
        quiet_row.last_message_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        await s.commit()

    async with db_sessionmaker() as s:
        # Без закрепа порядок обратный — иначе тест доказывал бы не то.
        before, _ = await convs.list_conversations(s, mine, tab="all", limit=50, offset=0)
    assert [i["id"] for i in before][:2] == [str(loud), str(quiet)]

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, quiet), mine)
        await s.commit()

    async with db_sessionmaker() as s:
        items, _total = await convs.list_conversations(s, mine, tab="all", limit=50, offset=0)
    assert [i["id"] for i in items][:2] == [str(quiet), str(loud)]
    assert items[0]["pinned"] is True
    assert items[1]["pinned"] is False


async def test_unpinning_puts_it_back(db_sessionmaker, make_avito_account, two):
    mine, _ = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, mine, "off")

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, conv_id), mine)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await pins.unpin(s, conv_id, mine.id) is True
        await s.commit()

    async with db_sessionmaker() as s:
        assert await pins.pinned_ids(s, mine.id) == set()
        # Второй раз — не ошибка: кнопка одна и та же.
        assert await pins.unpin(s, conv_id, mine.id) is False


async def test_pinning_is_for_everyone_who_can_read(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account
):
    """Право — «читать диалоги», то есть у всех четырёх ролей.

    Закрепление ничего не меняет ни в диалоге, ни у коллег: это отметка
    человека в собственном списке, и спрашивать на неё разрешение не у кого.
    В общей матрице RBAC этих ручек нет: ALLOW-ветка упёрлась бы в 422
    «только свои диалоги», то есть проверяла бы правило отметки, а не доступ.
    """
    account = await make_avito_account(avito_user_id=987654321)
    for role in ("admin", "head", "manager", "observer"):
        conv_id = await _conv(db_sessionmaker, account, users_by_role[role], f"api-{role}")
        head = {"Authorization": f"Bearer {tokens[role]}"}

        r = await client.post(f"/api/v1/conversations/{conv_id}/pin", headers=head)
        assert r.status_code == 200, (role, r.text)
        assert r.json()["pinned"] is True

        r = await client.delete(f"/api/v1/conversations/{conv_id}/pin", headers=head)
        assert r.status_code == 200, (role, r.text)
        assert r.json()["pinned"] is False

        assert (await client.post(f"/api/v1/conversations/{conv_id}/pin")).status_code == 401


# --------------------------------- закрытие снимает закрепление (28.08)


async def test_closing_removes_the_pin(db_sessionmaker, make_avito_account, two):
    """⚠ ЗАКРЕПЛЕНИЕ НА ЗАКРЫТОМ ДИАЛОГЕ НЕВИДИМО, НО ПРЕДЕЛ ЗАНИМАЕТ.

    Жалоба оператора 28.08: «можно закрепить максимум 1 диалог». Замер боя: у
    человека семь закреплений — предел, — и ШЕСТЬ из них на закрытых диалогах.
    Закрытые в списке не показываются, поэтому на экране одно закрепление, а
    восьмое поставить нельзя, и тост «Закреплённых уже максимум» выглядит
    враньём.

    Закрепление — рабочая закладка «вернусь к этому», и у закрытого диалога она
    бессмысленна. Закрытие снимает ожидание, непрочитанное и висящую передачу —
    закладку оно обязано снимать по той же причине.

    ЧТО ЛОМАЛИ: убрали `sa.delete(ConversationPin)` из ветки закрытия — тест
    краснеет.
    """
    mine, _ = two
    account = await make_avito_account()
    conv_id = await _conv(db_sessionmaker, account, mine, "close-pin")

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, conv_id), mine)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await s.get(ConversationPin, (mine.id, conv_id)) is not None

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        await convs.change_status(s, conv, new_status="closed", actor=mine)
        await s.commit()

    async with db_sessionmaker() as s:
        assert await s.get(ConversationPin, (mine.id, conv_id)) is None, (
            "закрепление пережило закрытие: оно невидимо, но занимает предел"
        )


async def test_closing_frees_the_cap(db_sessionmaker, make_avito_account, two):
    """Следствие, ради которого всё и делалось: предел освобождается.

    Проверяем не наличие строки, а ПОВЕДЕНИЕ — можно ли закрепить снова. Иначе
    тест зеленел бы и при снятии закрепления «наполовину».
    """
    mine, _ = two
    account = await make_avito_account()
    ids = [
        await _conv(db_sessionmaker, account, mine, f"free{i}") for i in range(pins.MAX_PINS + 1)
    ]

    async with db_sessionmaker() as s:
        for conv_id in ids[: pins.MAX_PINS]:
            await pins.pin(s, await s.get(Conversation, conv_id), mine)
        await s.commit()

    # Один из закреплённых закрыли — место обязано освободиться.
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, ids[0])
        assert conv is not None
        await convs.change_status(s, conv, new_status="closed", actor=mine)
        await s.commit()

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, ids[-1]), mine)
        await s.commit()  # не должно упасть: место освободилось
