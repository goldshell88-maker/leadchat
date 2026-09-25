"""Назначение операторов на каналы Авито (план 7.2, разбор Jivo 15 §2.2).

Что здесь держится. Блок 7.1 сделал очередь «Входящие» — общую на всех. У
заказчика девять каналов и тринадцать операторов, у каждого канала свой набор
людей, и без фильтрации оператор листает чужие обращения и берёт не свои.
Юниты закрывают три вопроса: кто что видит в очереди, кого ждёт эскалация
«отказались все» и что делает экран назначения с галочками.

**Главный тест файла — не про фильтрацию, а про её отсутствие**:
``test_a_channel_without_operators_is_open_to_everyone``. Пустой набор
означает «канал доступен ВСЕМ», а не «никому». Обратное прочтение стоило бы
одного деплоя: таблица назначений пуста, фильтрация включается, очередь у
всех тринадцати пустеет, и обращения виснут молча — без ошибки в логах и без
единого исключения. Поэтому правило проверяется и здесь, и на настоящем
PostgreSQL (``tests/integration/test_channel_filter.py``).

Чего здесь нет: каскадного удаления связей. SQLite юнит-тестов по умолчанию
не исполняет ``ON DELETE CASCADE`` вовсе, так что «проверка» каскада здесь
проверяла бы не тот код, который поедет в бой, — она в интеграционном файле.
"""

import uuid
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.models import AccountOperator, AuditLog, Client, Conversation, Message, User
from app.models.notification import Notification
from app.services import account_operators as acc_ops
from app.services import inbox

T0 = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------- фикстуры


@pytest.fixture
async def account(make_avito_account):
    """«! Парт - 7 / Ист - В43 МНЧ !» — канал, на который назначают."""
    return await make_avito_account(title="Парт-7")


@pytest.fixture
async def other_account(make_avito_account):
    """«Парт - 723 БЕЛЫЙ» — соседний канал с другим набором людей."""
    return await make_avito_account(avito_user_id=444555666, title="Парт-723 БЕЛЫЙ")


@pytest.fixture
async def ops(make_user) -> dict[str, User]:
    """Трое операторов: свой канала, свой соседнего и ничей."""
    return {
        "mine": await make_user("op-mine@leadchat.test", role="manager", full_name="Пётр Свой"),
        "other": await make_user("op-other@leadchat.test", role="manager", full_name="Сидор Чужой"),
        "free": await make_user("op-free@leadchat.test", role="manager", full_name="Иван Ничей"),
    }


@pytest.fixture
def make_conv(db_sessionmaker, account):
    """Диалог, ждущий принятия, на заданном канале."""
    seq = count(1)

    async def _make(
        *,
        account_id: uuid.UUID | None = None,
        offered_at: datetime | None = T0,
        client_name: str = "Иван Петров",
    ) -> Conversation:
        n = next(seq)
        async with db_sessionmaker() as s:
            client = Client(channel="avito", external_id=f"cl-{n}", name=client_name)
            s.add(client)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{n}",
                account_id=account_id or account.id,
                client_id=client.id,
                status="new",
                offered_at=offered_at,
                declined_by=[],
                unread_count=1,
                last_message_at=offered_at or T0,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"am-{n}",
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте! Почём ремонт?",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=offered_at or T0,
                )
            )
            await s.commit()
            return conv

    return _make


@pytest.fixture
def assign(db_sessionmaker):
    """Назначить людей на канал мимо сервиса — чтобы тесты фильтрации не
    зависели от валидации формы назначения."""

    async def _assign(account_id: uuid.UUID, *users: User) -> None:
        async with db_sessionmaker() as s:
            for u in users:
                s.add(AccountOperator(account_id=account_id, user_id=u.id))
            await s.commit()

    return _assign


async def _queue(db_sessionmaker, user: User) -> list[str]:
    async with db_sessionmaker() as s:
        items, _ = await inbox.list_inbox(s, user, now=T0)
    return [i["id"] for i in items]


def details(exc: pytest.ExceptionInfo[ApiError]) -> dict[str, Any]:
    return exc.value.details or {}


# ------------------------------------------- ПРАВИЛО СОВМЕСТИМОСТИ: пустой набор


async def test_a_channel_without_operators_is_open_to_everyone(db_sessionmaker, ops, make_conv):
    """Ключевое правило блока: нет назначенных — канал доступен ВСЕМ.

    Не «никому». Разница между двумя прочтениями — это разница между «7.2
    включили, всё работает как раньше, каналы подключаем по одному» и «7.2
    включили, очередь у тринадцати человек пуста, обращения висят, и никто не
    понимает почему»: ошибка тихая, без исключений и без строчки в логе.
    """
    conv = await make_conv()
    for who in ops.values():
        assert await _queue(db_sessionmaker, who) == [str(conv.id)], who.full_name


async def test_the_operator_sees_his_channels_and_the_common_ones(
    db_sessionmaker, ops, account, other_account, make_conv, assign
):
    """Очередь оператора = свои каналы ПЛЮС ничьи. Именно плюс.

    Если бы правило было «только свои», то канал, который забыли раздать
    (девятый из девяти, подключённый в пятницу), пропал бы из очереди у всех.
    """
    await assign(account.id, ops["mine"])
    mine = await make_conv(account_id=account.id)
    common = await make_conv(account_id=other_account.id)  # на него не назначен никто

    assert set(await _queue(db_sessionmaker, ops["mine"])) == {str(mine.id), str(common.id)}


async def test_a_foreign_channel_disappears_from_the_queue(
    db_sessionmaker, ops, account, make_conv, assign
):
    """Ради этого всё и делалось: чужой канал в личной очереди не показывается."""
    await assign(account.id, ops["mine"])
    conv = await make_conv(account_id=account.id)

    assert await _queue(db_sessionmaker, ops["mine"]) == [str(conv.id)]
    assert await _queue(db_sessionmaker, ops["other"]) == []
    assert await _queue(db_sessionmaker, ops["free"]) == []


async def test_each_channel_keeps_its_own_crew(
    db_sessionmaker, ops, account, other_account, make_conv, assign
):
    """Девять каналов — девять наборов: у каждого своя очередь, не пересекаются."""
    await assign(account.id, ops["mine"])
    await assign(other_account.id, ops["other"])
    first = await make_conv(account_id=account.id)
    second = await make_conv(account_id=other_account.id)

    assert await _queue(db_sessionmaker, ops["mine"]) == [str(first.id)]
    assert await _queue(db_sessionmaker, ops["other"]) == [str(second.id)]
    assert await _queue(db_sessionmaker, ops["free"]) == []


async def test_a_channel_left_with_only_a_fired_operator_becomes_common_again(
    db_sessionmaker, ops, account, make_conv, assign, make_user
):
    """Назначенный, но отключённый сотрудник не держит канал за собой.

    Иначе получалось бы худшее из возможных состояний: канал считается
    «назначенным», живые операторы его не видят, а тот, кто видит, в системе
    больше не появится. Обращения этого канала не увидел бы никто.
    """
    fired = await make_user(
        "fired@leadchat.test", role="manager", is_active=False, full_name="Уволенный"
    )
    await assign(account.id, fired)
    conv = await make_conv(account_id=account.id)

    assert await _queue(db_sessionmaker, ops["free"]) == [str(conv.id)]


async def test_an_assigned_observer_does_not_hide_the_channel_either(
    db_sessionmaker, ops, account, make_conv, assign, users_by_role
):
    """Назначенный наблюдатель — тоже не оператор канала.

    Роль без права ``messages:send`` диалог принять не может (это отсекает
    ``inbox._assert_can_take``). Считать её «назначенной» значит спрятать
    канал от тех, кто работать умеет, — тот же тихий тупик, что и с
    уволенным. Через сервис назначения такая строка не пройдёт, но связь
    может достаться из прошлого: роль сотрудника меняют, а связь остаётся.
    """
    await assign(account.id, users_by_role["observer"])
    conv = await make_conv(account_id=account.id)

    assert await _queue(db_sessionmaker, ops["free"]) == [str(conv.id)]


# ---------------------------------------------------------- кто видит всю очередь


async def test_the_admin_sees_the_whole_queue_regardless_of_assignments(
    db_sessionmaker, users_by_role, ops, account, other_account, make_conv, assign
):
    """Администратору очередь не сужается — ему нужно видеть всё.

    Он же получает эскалацию «диалог никто не принял» и назначает
    ответственного руками (01 §5.5): сузить ему очередь значило бы спрятать
    ровно те обращения, ради которых его и зовут.
    """
    await assign(account.id, ops["mine"])
    await assign(other_account.id, ops["other"])
    first = await make_conv(account_id=account.id)
    second = await make_conv(account_id=other_account.id)

    assert set(await _queue(db_sessionmaker, users_by_role["admin"])) == {
        str(first.id),
        str(second.id),
    }


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_the_supervisor_and_the_observer_keep_the_whole_queue(
    db_sessionmaker, users_by_role, ops, account, make_conv, assign, role
):
    """Кто из очереди не берёт — тому и сужать нечего.

    У руководителя и наблюдателя нет ``messages:send``: они не принимают
    диалоги, а смотрят, как разбирается очередь. Личный фильтр им ничего не
    экономит, зато прячет затор от того, кто обязан его заметить. Новых данных
    они при этом не получают: читать все диалоги может любая роль (01 §12), а
    очередь — их подмножество.
    """
    await assign(account.id, ops["mine"])
    conv = await make_conv(account_id=account.id)

    assert await _queue(db_sessionmaker, users_by_role[role]) == [str(conv.id)]


async def test_the_counter_respects_assignments_too(
    db_sessionmaker, ops, account, other_account, make_conv, assign
):
    """Бейдж вкладки обязан совпадать со списком.

    Счётчик, считающий чужие каналы, — это «Входящие (7)» и пустой список под
    ним; оператор идёт спрашивать, что сломалось.
    """
    await assign(account.id, ops["mine"])
    await make_conv(account_id=account.id)
    await make_conv(account_id=other_account.id)  # ничей — виден обоим

    async with db_sessionmaker() as s:
        assert await inbox.inbox_count(s, ops["mine"]) == 2
        assert await inbox.inbox_count(s, ops["other"]) == 1


# ------------------------------------------------------------------- эскалация


async def test_escalation_waits_only_for_the_operators_of_this_channel(
    db_sessionmaker, ops, account, make_conv, assign
):
    """Эскалация — после отказа операторов ЭТОГО канала, а не всех тринадцати.

    До 7.2 «отказались все» означало «отказались все в компании». На девяти
    каналах такая эскалация не наступает никогда: десять человек из тринадцати
    диалога не видят и отказаться от него не могут. То есть её попросту нет.
    """
    await assign(account.id, ops["mine"], ops["other"])
    conv = await make_conv(account_id=account.id)

    async with db_sessionmaker() as s:
        first = await inbox.decline(s, conv.id, ops["mine"], now=T0)
        await s.commit()
    assert first.escalated is False, "один из двух — ещё не «все»"

    async with db_sessionmaker() as s:
        second = await inbox.decline(s, conv.id, ops["other"], now=T0)
        await s.commit()
    assert second.escalated is True, "отказались все операторы канала — зовём администратора"

    async with db_sessionmaker() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert len(rows) == 1 and rows[0].audience == "admin"


async def test_a_stranger_declining_does_not_trigger_the_escalation(
    db_sessionmaker, ops, account, make_conv, assign
):
    """Отказ того, кому канал не назначен, состав «всех» не закрывает.

    Обратное дало бы ложную тревогу: администратора зовут разбирать диалог,
    от которого настоящие операторы канала даже не отказывались.
    """
    await assign(account.id, ops["mine"], ops["other"])
    conv = await make_conv(account_id=account.id)

    async with db_sessionmaker() as s:
        result = await inbox.decline(s, conv.id, ops["free"], now=T0)
        await s.commit()
    assert result.escalated is False

    async with db_sessionmaker() as s:
        assert (await s.execute(select(Notification))).scalars().all() == []


async def test_on_a_common_channel_the_escalation_still_waits_for_everyone(
    db_sessionmaker, users_by_role, ops, make_conv
):
    """Канал без назначений: состав «кому доступен» — ровно как до 7.2.

    Без этой ветки первое же включение фильтрации выключило бы эскалацию на
    всех каналах, где никого не назначили: множество операторов канала пусто,
    пустое множество вложено в любое — и «отказались все» срабатывало бы после
    ПЕРВОГО отказа.
    """
    conv = await make_conv()
    everyone = [users_by_role["admin"], users_by_role["manager"], *ops.values()]
    results = []
    for who in everyone:
        async with db_sessionmaker() as s:
            results.append(await inbox.decline(s, conv.id, who, now=T0))
            await s.commit()

    assert [r.escalated for r in results[:-1]] == [False] * (len(everyone) - 1)
    assert results[-1].escalated is True


async def test_eligible_ids_are_the_channel_crew(db_sessionmaker, ops, account, make_conv, assign):
    """Точка расширения отдаёт именно операторов канала — проверяем составом."""
    await assign(account.id, ops["mine"], ops["other"])
    conv = await make_conv(account_id=account.id)
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row is not None
        eligible = await inbox.eligible_operator_ids(s, row)
    assert eligible == {str(ops["mine"].id), str(ops["other"].id)}


async def test_escalation_on_the_real_scale_nine_channels_thirteen_operators(
    db_sessionmaker, make_avito_account, make_user, make_conv, assign
):
    """Настоящий масштаб заказчика: девять каналов, тринадцать операторов.

    Соседние тесты доказывают правило на трёх людях, и на трёх оно доказуемо
    случайно: разница между «ждём двоих с канала» и «ждём всех тринадцати»
    там укладывается в один лишний отказ. Здесь она — одиннадцать человек, то
    есть разница между эскалацией, которая наступает через две минуты, и
    эскалацией, которой нет вообще: одиннадцать оставшихся диалога не видят
    (их каналы другие) и отказаться от него физически не могут, поэтому
    «отказались все» никогда не станет правдой, а обращение будет висеть в
    очереди до конца смены — молча, без ошибки и без уведомления.

    Проверяем не только факт эскалации, но и то, что она наступает на ВТОРОМ
    отказе, а не раньше: сработавшая на первом означала бы, что состав «кому
    доступен» пуст и администратора зовут вместо второго оператора канала.
    """
    channels = [
        await make_avito_account(avito_user_id=880000000 + i, title=f"Канал-{i}") for i in range(9)
    ]
    crew = [
        await make_user(f"scale{i}@leadchat.test", role="manager", full_name=f"Оператор {i:02d}")
        for i in range(13)
    ]
    # Первый канал ведут двое; остальные одиннадцать человек разобраны по
    # восьми соседним каналам — они и есть «те, кого ждать нельзя».
    await assign(channels[0].id, crew[0], crew[1])
    for i, operator in enumerate(crew[2:]):
        await assign(channels[1 + i % 8].id, operator)

    conv = await make_conv(account_id=channels[0].id)

    # Диалог виден ровно двоим — остальным одиннадцати он в очередь не попал.
    assert await _queue(db_sessionmaker, crew[0]) == [str(conv.id)]
    assert await _queue(db_sessionmaker, crew[5]) == []

    async with db_sessionmaker() as s:
        first = await inbox.decline(s, conv.id, crew[0], now=T0)
        await s.commit()
    assert first.escalated is False, "один из двух операторов канала — ещё не «все»"

    async with db_sessionmaker() as s:
        second = await inbox.decline(s, conv.id, crew[1], now=T0)
        await s.commit()
    assert second.escalated is True, (
        "отказались оба оператора канала — эскалация обязана сработать сейчас, "
        "а не ждать одиннадцати человек, которые диалога не видят"
    )

    async with db_sessionmaker() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert len(rows) == 1 and rows[0].audience == "admin"


# ------------------------------------------------- принятие чужого диалога


async def test_taking_a_dialog_of_a_foreign_channel_is_refused(
    db_sessionmaker, ops, account, make_conv, assign
):
    """Спрятать кнопку мало — принятие обязано проверять канал на сервере.

    Id диалога виден в ссылке, в кадре ``inbox:new`` и во вкладке «Все», а
    принятие — обычный POST. Без этой проверки фильтр очереди остаётся
    оформлением, а не правилом.
    """
    await assign(account.id, ops["mine"])
    conv = await make_conv(account_id=account.id)

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.assert_can_take_account(s, ops["other"], conv.account_id)
    assert exc.value.status == 403
    assert details(exc)["reason"] == "channel_not_assigned"
    # Текст называет причину человеческим языком: «канал не ваш» — не сбой, и
    # оператор должен понять, что делать, а не жать кнопку второй раз.
    assert "Парт-7" in exc.value.message


async def test_taking_a_dialog_of_your_own_or_a_common_channel_is_allowed(
    db_sessionmaker, ops, users_by_role, account, other_account, assign
):
    """Три разрешённых случая: свой канал, ничей канал и администратор."""
    await assign(account.id, ops["mine"])
    async with db_sessionmaker() as s:
        await acc_ops.assert_can_take_account(s, ops["mine"], account.id)
        await acc_ops.assert_can_take_account(s, ops["free"], other_account.id)
        await acc_ops.assert_can_take_account(s, users_by_role["admin"], account.id)


# --------------------------------------------------------- экран назначения


async def test_setting_the_crew_replaces_the_whole_set(
    db_sessionmaker, ops, users_by_role, account
):
    """Галочки экрана — это итоговый набор, а не «добавь/убери».

    При инкрементальных операциях два администратора, правящие один канал
    одновременно, получают набор, которого не хотел ни один из них.
    """
    async with db_sessionmaker() as s:
        await acc_ops.set_operators(
            s, account.id, [ops["mine"].id, ops["other"].id], actor=users_by_role["admin"]
        )
        await s.commit()

    async with db_sessionmaker() as s:
        result = await acc_ops.set_operators(
            s, account.id, [ops["other"].id, ops["free"].id], actor=users_by_role["admin"]
        )
        await s.commit()

    assert result.added == [ops["free"].id]
    assert result.removed == [ops["mine"].id]
    async with db_sessionmaker() as s:
        assert set(await acc_ops.assigned_user_ids(s, account.id)) == {
            ops["other"].id,
            ops["free"].id,
        }


async def test_an_empty_set_reopens_the_channel(
    db_sessionmaker, ops, users_by_role, account, make_conv, assign
):
    """Снять всех — законная операция: канал возвращается в общий пул.

    Это не «ошибка формы, вы никого не выбрали», а способ вернуть канал всем.
    """
    await assign(account.id, ops["mine"])
    conv = await make_conv(account_id=account.id)
    assert await _queue(db_sessionmaker, ops["other"]) == []

    async with db_sessionmaker() as s:
        await acc_ops.set_operators(s, account.id, [], actor=users_by_role["admin"])
        await s.commit()

    assert await _queue(db_sessionmaker, ops["other"]) == [str(conv.id)]


async def test_repeating_the_same_set_changes_nothing_and_writes_nothing(
    db_sessionmaker, ops, users_by_role, account
):
    """«Открыл, посмотрел, нажал Сохранить» не должен попадать в журнал."""
    async with db_sessionmaker() as s:
        await acc_ops.set_operators(s, account.id, [ops["mine"].id], actor=users_by_role["admin"])
        await s.commit()
    async with db_sessionmaker() as s:
        again = await acc_ops.set_operators(
            s, account.id, [ops["mine"].id], actor=users_by_role["admin"]
        )
        await s.commit()

    assert again.changed is False
    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "account.operators_changed")
                )
            )
            .scalars()
            .all()
        )
        links = (await s.execute(select(AccountOperator))).scalars().all()
    assert len(rows) == 1, "повторное сохранение того же набора попало в журнал"
    assert len(links) == 1, "составной ключ обязан запрещать дубль связи"


async def test_duplicate_checkboxes_collapse(db_sessionmaker, ops, users_by_role, account):
    """Один и тот же id дважды в теле — не повод падать и не повод дублировать."""
    async with db_sessionmaker() as s:
        result = await acc_ops.set_operators(
            s, account.id, [ops["mine"].id, ops["mine"].id], actor=users_by_role["admin"]
        )
        await s.commit()
    assert result.operator_ids == [ops["mine"].id]
    async with db_sessionmaker() as s:
        assert len((await s.execute(select(AccountOperator))).scalars().all()) == 1


@pytest.mark.parametrize("role", ["head", "observer"])
async def test_a_non_operator_cannot_be_assigned(
    db_sessionmaker, users_by_role, account, role, make_conv, ops
):
    """Назначить можно только того, кто отвечает клиентам (право ``messages:send``).

    Проверка на сервере, а не «в интерфейсе такой галочки нет». Назначенный
    наблюдатель канал бы не увидел (роль отсекается при принятии), но
    СЧИТАЛСЯ бы назначенным — и канал спрятался бы от настоящих операторов.
    Тихое исчезновение обращений вместо честной ошибки формы.
    """
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.set_operators(
                s, account.id, [users_by_role[role].id], actor=users_by_role["admin"]
            )
    assert exc.value.status == 422
    assert details(exc)["reason"] == "cannot_answer_clients"


async def test_an_inactive_employee_cannot_be_assigned(
    db_sessionmaker, users_by_role, account, make_user
):
    """Отключённого назначать нельзя: канал достался бы тому, кто не войдёт."""
    fired = await make_user(
        "fired2@leadchat.test", role="manager", is_active=False, full_name="Уволенный"
    )
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.set_operators(s, account.id, [fired.id], actor=users_by_role["admin"])
    assert exc.value.status == 422
    assert details(exc)["reason"] == "user_inactive"


async def test_an_unknown_id_is_rejected_not_silently_dropped(
    db_sessionmaker, users_by_role, account, ops
):
    """Неизвестный id — отказ формы, а не «сохранили что смогли».

    Молча отбросить половину списка значит показать администратору
    «сохранено» и оставить канал без половины смены.
    """
    ghost = uuid.uuid4()
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.set_operators(
                s, account.id, [ops["mine"].id, ghost], actor=users_by_role["admin"]
            )
    assert exc.value.status == 422
    assert details(exc)["reason"] == "user_not_found"
    async with db_sessionmaker() as s:
        assert (await s.execute(select(AccountOperator))).scalars().all() == []


async def test_an_unknown_channel_is_a_404(db_sessionmaker, users_by_role, ops):
    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.set_operators(
                s, uuid.uuid4(), [ops["mine"].id], actor=users_by_role["admin"]
            )
    assert exc.value.status == 404


async def test_the_change_lands_in_the_journal_with_both_sides_of_the_diff(
    db_sessionmaker, ops, users_by_role, account, assign
):
    """Журнал отвечает на вопрос разбора: почему канал перестал приходить Петрову.

    Поэтому пишем и дельту («кого сняли»), и состав целиком — иначе состояние
    канала пришлось бы склеивать из десяти строк истории.
    """
    await assign(account.id, ops["mine"])
    async with db_sessionmaker() as s:
        await acc_ops.set_operators(s, account.id, [ops["other"].id], actor=users_by_role["admin"])
        await s.commit()

    async with db_sessionmaker() as s:
        row = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "account.operators_changed")
                )
            )
            .scalars()
            .one()
        )
    assert row.user_id == users_by_role["admin"].id
    assert row.entity == "avito_account" and row.entity_id == str(account.id)
    d = dict(row.details or {})
    assert d["added"] == [str(ops["other"].id)]
    assert d["removed"] == [str(ops["mine"].id)]
    assert d["operator_ids"] == [str(ops["other"].id)]
    assert d["title"] == "Парт-7"


def test_the_audit_action_is_registered_in_the_catalog():
    """Событие мимо реестра — пустая колонка «действие» в журнале (01 §9.7)."""
    from app.services.audit import AUDIT_ACTIONS, describe

    assert acc_ops.AUDIT_ACTION in AUDIT_ACTIONS
    assert describe(acc_ops.AUDIT_ACTION) == "Изменён состав операторов канала"


# ------------------------------------------------------ списки для интерфейса


async def test_the_assignment_screen_lists_the_whole_staff_with_a_reason(
    db_sessionmaker, ops, users_by_role, account, assign, make_user
):
    """Экран приезжает одним ответом: галочки и список — из одного запроса.

    Двумя запросами, выполненными в разном порядке, получаются галочки на
    людях, которых нет в списке. Несотрудники показаны, но помечены — как в
    Jivo серым; ``reason`` объясняет, почему галочка недоступна.
    """
    await assign(account.id, ops["mine"])
    # Робот помечен КОЛОНКОЙ — домен больше ничего не значит (15 августа):
    # по домену этот экран прятал и настоящих людей с почтой на `.local`.
    await make_user(
        "smoke@leadchat.local", role="manager", full_name="Робот регрессии", is_service=True
    )
    human_local = await make_user(
        "dispatcher@leadpartner.local", role="manager", full_name="Человек На Локале"
    )

    async with db_sessionmaker() as s:
        data = await acc_ops.channel_operators(s, None, account.id)

    by_id = {c["id"]: c for c in data["candidates"]}
    assert data["assigned_ids"] == [str(ops["mine"].id)]
    assert by_id[str(ops["mine"].id)]["can_be_operator"] is True
    assert by_id[str(users_by_role["observer"].id)]["can_be_operator"] is False
    assert by_id[str(users_by_role["observer"].id)]["reason"] == "Роль не отвечает клиентам"
    # Служебный smoke-пользователь (колонка `is_service`, 07 §6) в интерфейсе
    # не живёт: назначить робота на канал заказчика можно только по ошибке.
    assert not any("Робот" in c["full_name"] for c in data["candidates"])
    # А настоящий человек с почтой на `.local` — живёт: его прятал старый
    # фильтр по домену, и оператора нельзя было назначить на канал.
    assert str(human_local.id) in by_id


async def test_the_card_summary_counts_and_previews(
    db_sessionmaker, ops, account, other_account, assign, make_user
):
    """Свод для карточки канала: «+N» рядом с аватарками, как в Jivo.

    Батчем на весь список каналов: девять карточек — это девять запросов
    ровно того вида, который в списке диалогов запрещён явно (01 §5.1).
    ``count = 0`` читается экраном как «открыт всем» — то же правило, что и в
    фильтре очереди.
    """
    extra = [
        await make_user(f"bulk{i}@leadchat.test", role="manager", full_name=f"Оператор {i}")
        for i in range(4)
    ]
    await assign(account.id, ops["mine"], *extra)

    async with db_sessionmaker() as s:
        summary = await acc_ops.operators_summary(s, [account.id, other_account.id])

    assert summary[account.id]["count"] == 5
    assert len(summary[account.id]["preview"]) == acc_ops.PREVIEW_LIMIT
    assert summary[other_account.id] == {"count": 0, "preview": []}


async def test_my_channels_explain_why_the_queue_looks_like_it_does(
    db_sessionmaker, ops, users_by_role, account, other_account, assign
):
    """Блок «Мои каналы»: ``assigned`` — назначили, ``open`` — канал ничей.

    Это ответ оператору на вопрос «почему мне приходят одни обращения и не
    приходят другие», который иначе задают администратору.
    """
    await assign(account.id, ops["mine"])

    async with db_sessionmaker() as s:
        mine = await acc_ops.channels_of(s, ops["mine"])
        stranger = await acc_ops.channels_of(s, ops["other"])
        admin = await acc_ops.channels_of(s, users_by_role["admin"])

    assert {c["title"]: c["access"] for c in mine} == {
        "Парт-7": "assigned",
        "Парт-723 БЕЛЫЙ": "open",
    }
    # Чужой канал в списке не появляется вовсе — иначе «мои каналы» врут.
    assert [c["title"] for c in stranger] == ["Парт-723 БЕЛЫЙ"]
    # Администратор видит очередь целиком, и его список каналов честно об этом
    # говорит: все каналы открыты ему, а не «назначены».
    assert {c["access"] for c in admin} == {"open"} and len(admin) == 2


async def test_a_disabled_channel_is_not_in_my_channels(
    db_sessionmaker, ops, make_avito_account, assign
):
    """Отключённый канал в списке выглядел бы рабочим, а обращений по нему нет."""
    dead = await make_avito_account(avito_user_id=777888999, title="Парт-Архив", status="disabled")
    await assign(dead.id, ops["mine"])
    async with db_sessionmaker() as s:
        titles = [c["title"] for c in await acc_ops.channels_of(s, ops["mine"])]
    assert "Парт-Архив" not in titles


# ------------------------------------------------------------------ мелочи API


async def test_the_predicates_are_sql_not_python(db_sessionmaker, ops, account, assign):
    """Фильтрация обязана уезжать в базу: очередь бывает длинной.

    Постфильтрация страницы в Python отдала бы неполную страницу и неверный
    счётчик под ней. Проверяем не текст запроса, а то, что предикат вообще
    исполняется базой как условие выборки.
    """
    await assign(account.id, ops["mine"])
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(Conversation.id).where(
                    acc_ops.visible_accounts_condition(Conversation.account_id, ops["other"].id)
                )
            )
        ).all()
    assert rows == []


# ------------------------------------------- удалённый сотрудник запирал канал


async def test_a_deleted_operator_still_has_a_row_on_screen(
    db_sessionmaker, users_by_role, account, make_user
):
    """Кто в наборе — тот и в списке. Иначе галочку не с чего снять.

    ⚠ БОЕВОЙ ТУПИК 03.09, ЖАЛОБА ВЛАДЕЛЬЦА: «не могу подключать сотрудников,
    сломалось — пишут, что отключён, хотя на деле нет».

    Удалённых вычеркнули из списка кандидатов, а `assigned_ids` отдавали как
    есть. Получался идентификатор БЕЗ СТРОКИ: галочка стоит, снять её не с
    чего, форма шлёт набор целиком — и сервер отвечает 422. Канал становился
    несохраняемым навсегда. Так заперло 14 каналов из 14.
    """
    ушёл = await make_user("gone@leadchat.test", role="manager", full_name="Ушедший Сотрудник")
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=account.id, user_id=ушёл.id))
        row = await s.get(User, ушёл.id)
        row.deleted_at = T0
        row.is_active = False
        await s.commit()

    async with db_sessionmaker() as s:
        экран = await acc_ops.channel_operators(s, None, account.id)

    assert str(ушёл.id) in экран["assigned_ids"]
    строки = {c["id"]: c for c in экран["candidates"]}
    assert str(ушёл.id) in строки, "назначенного нет строкой — галочку не снять"
    # ⚠ ПРИЧИНА ИМЕННО «УДАЛЁН», А НЕ «ОТКЛЮЧЁН». Удаление ставит оба флага
    # соседними строками, и при обратном порядке проверок владелец читал совет
    # «сначала включите его» — выполнить его нельзя.
    assert строки[str(ушёл.id)]["reason"] == "Сотрудник удалён"
    assert строки[str(ушёл.id)]["can_be_operator"] is False


async def test_a_deleted_operator_does_not_block_saving_the_channel(
    db_sessionmaker, users_by_role, account, make_user, ops
):
    """Унаследованное не пересуживается: сохранение канала проходит.

    Сервер судит только ДОБАВЛЯЕМЫХ. Пока негодный человек лежит в наборе,
    администратор обязан иметь возможность и добавить кого-то, и снять его
    самого — ради этого он сюда и пришёл.
    """
    ушёл = await make_user("gone2@leadchat.test", role="manager", full_name="Ушедший Второй")
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=account.id, user_id=ушёл.id))
        row = await s.get(User, ушёл.id)
        row.deleted_at = T0
        row.is_active = False
        await s.commit()

    # Набор целиком, как его шлёт форма: унаследованный негодный + новый живой.
    async with db_sessionmaker() as s:
        await acc_ops.set_operators(
            s, account.id, [ушёл.id, ops["mine"].id], actor=users_by_role["admin"]
        )
        await s.commit()

    async with db_sessionmaker() as s:
        assert set(await acc_ops.assigned_user_ids(s, account.id)) == {ушёл.id, ops["mine"].id}

    # А снять его — проходит и подавно: это и есть выход из тупика.
    async with db_sessionmaker() as s:
        await acc_ops.set_operators(s, account.id, [ops["mine"].id], actor=users_by_role["admin"])
        await s.commit()
    async with db_sessionmaker() as s:
        assert set(await acc_ops.assigned_user_ids(s, account.id)) == {ops["mine"].id}


async def test_a_deleted_employee_cannot_be_added_and_hears_why(
    db_sessionmaker, users_by_role, account, make_user
):
    """Добавить удалённого с нуля по-прежнему нельзя — и причина честная.

    ⚠ ОТДЕЛЬНАЯ ПРИЧИНА, А НЕ «ОТКЛЮЧЁН». Прежний отказ советовал «сначала
    включите его, потом назначайте» — совет, который выполнить невозможно:
    включение удалённого отвечает отказом. Ровно это владелец и прочитал как
    «пишут, что отключён, хотя на деле нет».
    """
    ушёл = await make_user("gone3@leadchat.test", role="manager", full_name="Ушедший Третий")
    async with db_sessionmaker() as s:
        row = await s.get(User, ушёл.id)
        row.deleted_at = T0
        row.is_active = False
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(ApiError) as exc:
            await acc_ops.set_operators(s, account.id, [ушёл.id], actor=users_by_role["admin"])
    assert exc.value.status == 422
    assert details(exc)["reason"] == "user_deleted"
    assert "включите" not in exc.value.message.lower()


async def test_deleting_an_employee_releases_their_channels(
    db_sessionmaker, users_by_role, account, other_account, make_user, redis
):
    """Причина тупика: связь переживала человека.

    ⚠ ТОЛЬКО УДАЛЕНИЕ. Отключение обратимо (отпуск, болезнь), и связь его
    переживает намеренно — иначе возвращение сотрудника молча оставило бы
    канал общим. Проверка обратной половины — соседним тестом.
    """
    from app.services import users as users_svc

    ушёл = await make_user("gone4@leadchat.test", role="manager", full_name="Ушедший Четвёртый")
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=account.id, user_id=ушёл.id))
        s.add(AccountOperator(account_id=other_account.id, user_id=ушёл.id))
        await s.commit()

    async with db_sessionmaker() as s:
        await users_svc.delete_user(s, redis, actor=users_by_role["admin"], user_id=ушёл.id)

    async with db_sessionmaker() as s:
        осталось = (
            (await s.execute(select(AccountOperator).where(AccountOperator.user_id == ушёл.id)))
            .scalars()
            .all()
        )
        assert осталось == [], "удалённый сотрудник остался числиться оператором"
        # След в журнале по каждому каналу: разбор «почему канал стал общим»
        # ищет одну строку, а не догадывается по времени удаления.
        строки = (
            (await s.execute(select(AuditLog).where(AuditLog.action == acc_ops.AUDIT_ACTION)))
            .scalars()
            .all()
        )
        assert {str(a.entity_id) for a in строки} == {str(account.id), str(other_account.id)}
        assert all(a.details["reason"] == "user_deleted" for a in строки)


async def test_deactivating_an_employee_keeps_their_channels(
    db_sessionmaker, users_by_role, account, make_user, redis
):
    """Обратная половина: отключение связь НЕ рвёт.

    Отключение — пауза, а не конец. Снеси мы связь здесь, включение сотрудника
    обратно перестало бы возвращать ему каналы: администратор считает, что
    вернул человека в строй, а канал остался открытым всей смене.
    """
    from app.services import users as users_svc

    отпуск = await make_user("vacation@leadchat.test", role="manager", full_name="В Отпуске")
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=account.id, user_id=отпуск.id))
        await s.commit()

    async with db_sessionmaker() as s:
        await users_svc.deactivate_user(s, redis, actor=users_by_role["admin"], user_id=отпуск.id)

    async with db_sessionmaker() as s:
        assert set(await acc_ops.assigned_user_ids(s, account.id)) == {отпуск.id}
