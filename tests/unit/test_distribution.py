"""Автораспределение обращений между операторами (docs/18).

Главное, что проверяется здесь, — НЕ «раздаётся ли диалог». Это как раз просто.
Проверяется, что распределение НЕ раздаёт там, где раздавать нельзя: человеку
не в сети, человеку сверх предела, человеку с чужого канала. Каждый из этих
промахов тише отказа: диалог не висит в очереди, где его видно всем
тринадцати, а лежит «у оператора», которого нет за столом, — и не ждёт никого
конкретно.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Client, Conversation, User
from app.services import app_settings, distribution
from app.ws.presence import _status_key

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


async def online(redis, *users) -> None:
    for u in users:
        await redis.set(_status_key(u.id), "online")


@pytest.fixture
async def enabled(db_sessionmaker):
    """Раздача включена, потолок — три диалога.

    Настройки пишутся В БАЗУ, а не подменяются заглушкой: так тест проходит
    ровно тот путь, что и прод, включая чтение из `app_settings`. Заглушка
    молча пережила бы поломку хранилища настроек — и «раздача не включается»
    выяснилось бы уже на живой смене.
    """
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.DISTRIBUTION_ENABLED: True,
                app_settings.DISTRIBUTION_MAX_ACTIVE: 3,
            },
            user_id=None,
        )
        await s.commit()


@pytest.fixture
async def conv(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id="dist-1", name="Клиент Распределения")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="dist-chat-1",
            account_id=account.id,
            client_id=client.id,
            status="new",
            offered_at=T0,
            last_message_at=T0,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def load_conv(db_sessionmaker, conv_id):
    async with db_sessionmaker() as s:
        return await s.get(Conversation, conv_id)


async def add_open_dialogs(db_sessionmaker, account_id, user, count: int) -> None:
    """Навесить на оператора N открытых диалогов — это и есть его нагрузка."""
    async with db_sessionmaker() as s:
        for i in range(count):
            cl = Client(channel="avito", external_id=f"load-{user.id}-{i}", name="Нагрузка")
            s.add(cl)
            await s.flush()
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id=f"load-chat-{user.id}-{i}",
                    account_id=account_id,
                    client_id=cl.id,
                    status="in_progress",
                    assignee_id=user.id,
                    last_message_at=T0,
                )
            )
        await s.commit()


async def test_the_dialog_goes_to_the_least_loaded_operator(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """Выбирается наименее загруженный, а не первый попавшийся."""
    busy = await make_user("busy@leadchat.test", role="manager", full_name="Загруженный")
    free = await make_user("free@leadchat.test", role="manager", full_name="Свободный")
    await online(redis, busy, free)
    await add_open_dialogs(db_sessionmaker, conv.account_id, busy, 2)

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == free.id, "диалог должен уйти свободному"
    assert picked.load_before == 0


async def test_nobody_online_means_the_dialog_stays_in_the_queue(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """Никого в сети — обращение остаётся в очереди, а не уходит «в никуда».

    Это худший из возможных промахов: диалог, отданный закрывшему ноутбук, не
    висит в общей очереди, где его видно всем, и не ждёт никого конкретно.
    """
    await make_user("sleeping@leadchat.test", role="manager")
    # presence не выставляем — человек не в сети
    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is None
    assert picked.reason == "no_available_operator"


async def test_an_operator_at_the_cap_gets_nothing_more(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """Предел одновременных диалогов соблюдается: четырнадцатый разговор
    одновременно — это не работа, а её видимость."""
    solo = await make_user("solo@leadchat.test", role="manager")
    await online(redis, solo)
    await add_open_dialogs(db_sessionmaker, conv.account_id, solo, 3)  # предел = 3

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is None, "сверх предела раздавать нельзя"


async def test_closed_dialogs_do_not_count_as_load(
    db_sessionmaker, redis, make_user, conv, enabled, make_avito_account
):
    """Закрытые диалоги нагрузкой не считаются.

    Иначе оператор, честно закрывший за смену тридцать обращений, к обеду
    выглядел бы самым занятым и перестал получать новые — наказание за работу.
    """
    worker = await make_user("worker@leadchat.test", role="manager")
    await online(redis, worker)
    await add_open_dialogs(db_sessionmaker, conv.account_id, worker, 3)
    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    __import__("sqlalchemy")
                    .select(Conversation)
                    .where(Conversation.assignee_id == worker.id)
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            r.status = "closed"
        await s.commit()

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == worker.id
    assert picked.load_before == 0


async def test_only_operators_of_this_channel_are_considered(
    db_sessionmaker, redis, make_user, make_avito_account, conv, enabled
):
    """Правило каналов из 7.2 действует и здесь.

    Иначе обращение с «Парт-7» уехало бы человеку, который этот канал никогда
    не вёл, — и он бы даже не понял, откуда оно.
    """
    from app.models import AccountOperator

    ours = await make_user("ours@leadchat.test", role="manager", full_name="Наш")
    stranger = await make_user("stranger@leadchat.test", role="manager", full_name="Чужой")
    await online(redis, ours, stranger)

    # Назначаем на канал ТОЛЬКО «нашего» — тем самым канал перестаёт быть общим.
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=conv.account_id, user_id=ours.id))
        await s.commit()

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == ours.id, "чужому каналу оператора отдавать нельзя"


async def test_switched_off_means_the_old_behaviour(db_sessionmaker, redis, make_user, conv):
    """Выключенное распределение не делает НИЧЕГО.

    Это свойство важнее любого другого: если завтра распределение поведёт себя
    странно, администратор должен уметь вернуть прежний порядок одним
    переключателем, а не откатом системы.
    """
    u = await make_user("anyone@leadchat.test", role="manager")
    await online(redis, u)

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is None
    assert picked.reason == "distribution_off"


async def test_equal_load_goes_to_whoever_waited_longer(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """При равной нагрузке — тому, кому дольше не доставалось.

    В начале смены у всех по нулю открытых диалогов, и без этого признака
    выбор был бы не «случайным», а всегда одним и тем же человеком — первым по
    идентификатору. Один бы получал всё, остальные — ничего.

    ОТМЕТКА ЖИВЁТ У ЧЕЛОВЕКА, а не выводится из диалогов (#35). Прежняя
    постановка этого теста задавала её через `conversations.updated_at` — то
    есть закрепляла механизм, который и считал не то, и стоил перебора всей
    истории на каждое обращение.
    """
    recent = await make_user("recent@leadchat.test", role="manager", full_name="Недавно получал")
    long_ago = await make_user(
        "longago@leadchat.test", role="manager", full_name="Давно не получал"
    )
    await online(redis, recent, long_ago)

    async with db_sessionmaker() as s:
        for user, when in ((recent, T0), (long_ago, T0 - timedelta(hours=5))):
            row = await s.get(User, user.id)
            assert row is not None
            row.last_assigned_at = when
        await s.commit()

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == long_ago.id, (
        "при равной нагрузке выбирается тот, кому дольше не доставалось"
    )


async def test_an_operator_who_is_answering_is_not_pushed_to_the_back(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """Работающий оператор не должен уступать очередь бездельнику (#35).

    ЧТО ЛОМАЛОСЬ. Тай-брейк выводился из `max(updated_at)` по всем диалогам
    человека. Но `updated_at` меняется от ЛЮБОЙ правки диалога — пришло
    сообщение клиента, сменился статус, поставили метку. Поэтому оператор,
    получивший диалог пять часов назад и активно на него отвечающий, выглядел
    «только что получившим» и отправлялся в конец очереди на раздачу. А его
    место занимал тот, кто получил диалог полчаса назад, но с тех пор ничего
    не делал: его старые диалоги никто не трогал, и отметка выглядела старой.

    Признак, заведённый ради справедливости, работал ровно наоборот. Здесь он
    проверяется на том самом расхождении: у «работающего» диалог свежий по
    `updated_at`, но выдан он раньше.
    """
    busy = await make_user("busy@leadchat.test", role="manager", full_name="Отвечает клиентам")
    idle = await make_user("idle@leadchat.test", role="manager", full_name="Получил недавно")
    await online(redis, busy, idle)

    async with db_sessionmaker() as s:
        for user, when in ((busy, T0 - timedelta(hours=5)), (idle, T0 - timedelta(minutes=30))):
            row = await s.get(User, user.id)
            assert row is not None
            row.last_assigned_at = when
        await s.commit()

    # У «работающего» закрытый диалог, тронутый только что: по старому правилу
    # это выбрасывало его в конец очереди.
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="busy-old", name="Старый клиент")
        s.add(cl)
        await s.flush()
        s.add(
            Conversation(
                channel="avito",
                external_chat_id="busy-old-chat",
                account_id=conv.account_id,
                client_id=cl.id,
                status="closed",
                assignee_id=busy.id,
                last_message_at=T0,
                updated_at=T0,
            )
        )
        await s.commit()

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == busy.id, (
        "диалог обязан достаться тому, кому дольше не ВЫДАВАЛИ, а не тому, "
        "чьи диалоги дольше не трогали"
    )


# --- «отошёл» (#34) -----------------------------------------------------------


async def away(redis, *users) -> None:
    for u in users:
        await redis.set(_status_key(u.id), "away")


async def test_an_operator_who_stepped_away_gets_nothing(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """ГЛАВНАЯ ПРОВЕРКА #34: отошедшему новых обращений не дают.

    ЧТО ЛОМАЛОСЬ. Присутствие двоичное: `presence_map` возвращала `bool` и
    выбрасывала само значение ключа. Положи туда «away» — ни один потребитель
    не заметил бы разницы, `bool("away")` истинно так же, как `bool("online")`.
    Сказать системе «я отошёл» было нечем: единственный способ выпасть из
    раздачи — закрыть приложение, то есть перестать видеть свои же диалоги и
    через три минуты потерять розданные сторожу возврата.

    И беда усиливала сама себя: отошедший обычно САМЫЙ СВОБОДНЫЙ по числу
    открытых диалогов, а выбор идёт от самого свободного. Обращения доставались
    в первую очередь тому, кого нет за столом.
    """
    here = await make_user("here@leadchat.test", role="manager", full_name="За столом")
    lunch = await make_user("lunch@leadchat.test", role="manager", full_name="Обедает")
    await online(redis, here)
    await away(redis, lunch)

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is not None
    assert picked.assignee.id == here.id, "обращение ушло тому, кого нет за столом"


async def test_when_everyone_stepped_away_the_dialog_waits_in_the_queue(
    db_sessionmaker, redis, make_user, conv, enabled
):
    """Все отошли — обращение остаётся в очереди, а не назначается наугад.

    Очередь для того и есть: диалог виден всем тринадцати и достаётся тому, кто
    вернётся первым. Назначить его отсутствующему значило бы спрятать клиента
    у человека, которого нет.
    """
    a = await make_user("a-away@leadchat.test", role="manager")
    b = await make_user("b-away@leadchat.test", role="manager")
    await away(redis, a, b)

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))

    assert picked.assignee is None
