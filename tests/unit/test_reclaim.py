"""Возврат розданных и нетронутых диалогов (план 7.7).

Проверяется не «возвращает ли» — это просто. Проверяется ГРАНИЦА: что сторож
НЕ забирает диалоги, взятые руками, не забирает у того, кто уже ответил, не
забирает у сидящего на месте и не срабатывает на секундном обрыве связи.

Каждая из этих ошибок хуже, чем невозвращённый диалог: она отнимает у
оператора переписку, которую он ведёт, — и клиент видит, как разговор
обрывается на середине.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation, Message, User
from app.scheduler.jobs import reclaim
from app.services import inbox as inbox_svc
from app.ws.presence import _status_key

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


async def make_conv(
    db_sessionmaker,
    account_id,
    *,
    key: str,
    assignee_id=None,
    auto_assigned_at: datetime | None = None,
    claimed_by_id=None,
    status: str = "in_progress",
    awaiting_since: datetime | None = None,
    offered_at: datetime | None = None,
) -> Conversation:
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"rc-{key}", name=f"Клиент {key}")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"rc-chat-{key}",
            account_id=account_id,
            client_id=cl.id,
            status=status,
            assignee_id=assignee_id,
            claimed_by_id=claimed_by_id,
            auto_assigned_at=auto_assigned_at,
            awaiting_since=awaiting_since,
            offered_at=offered_at,
            last_message_at=T0,
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)
        return conv


async def reload(db_sessionmaker, conv_id) -> Conversation:
    async with db_sessionmaker() as s:
        return await s.get(Conversation, conv_id)


async def run_reclaim(db_sessionmaker, redis) -> list[dict]:
    """Прогон сторожа на сессии теста — та же логика, что в планировщике.

    Возвращает кадры: их число и есть число возвращённых диалогов.
    """
    async with db_sessionmaker() as s:
        frames = await reclaim.reclaim_in_session(s, redis)
        await s.commit()
        return frames


async def test_an_abandoned_dialog_goes_back_to_the_queue(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Раздали, человек ушёл не притронувшись — диалог возвращается всем.

    Клиент «у оператора», которого нет за столом, не ждёт никого конкретно и не
    виден остальным двенадцати. В очереди его хотя бы заметно.
    """
    account = await make_avito_account()
    gone = await make_user("gone@leadchat.test", role="manager", full_name="Ушедший")
    # presence не выставляем — человека нет в сети
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="abandoned",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 1

    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id is None
    assert inbox_svc.is_waiting(after), "диалог обязан снова стоять в очереди"
    assert after.auto_assigned_at is None, "отметка снята — второй раз не заберут"


async def test_a_dialog_taken_by_hand_is_never_touched(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Взятое руками не отбирается никогда.

    У оператора есть диалоги, которые он ведёт неделю. Забрать их, когда
    человек ушёл домой, значит оборвать клиенту переписку на середине.
    """
    account = await make_avito_account()
    gone = await make_user("hand@leadchat.test", role="manager")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="byhand",
        assignee_id=gone.id,
        claimed_by_id=gone.id,
        auto_assigned_at=None,  # система его не раздавала
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0
    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id == gone.id


async def test_an_operator_who_already_replied_keeps_the_dialog(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Ответил клиенту — значит ведёт диалог, и забирать его нельзя.

    Это страховка против пропущенной точки снятия отметки: точек пять в четырёх
    модулях, и цена ошибки — отобранный у работающего человека разговор.
    Поэтому сторож смотрит не только на отметку, но и на саму переписку.
    """
    account = await make_avito_account()
    working = await make_user("working@leadchat.test", role="manager")
    given_at = datetime.now(UTC) - timedelta(minutes=10)
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="replied",
        assignee_id=working.id,
        auto_assigned_at=given_at,  # отметку «забыли» снять
    )
    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                sender_user_id=working.id,
                body="Здравствуйте, сейчас посмотрю",
                delivery_status="delivered",
                created_at=given_at + timedelta(minutes=1),
            )
        )
        await s.commit()

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0
    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id == working.id
    # Заодно сторож чинит забытую отметку, чтобы не проверять переписку вечно.
    assert after.auto_assigned_at is None


async def test_an_operator_who_is_online_keeps_the_dialog(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Человек на месте — диалог его, даже если он ещё не ответил.

    Оператор мог только что открыть диалог и читать переписку. Отбирать её
    из-под курсора значит наказывать за то, что он не печатает достаточно
    быстро.
    """
    account = await make_avito_account()
    here = await make_user("here@leadchat.test", role="manager")
    await redis.set(_status_key(here.id), "online")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="online",
        assignee_id=here.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0
    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id == here.id


async def test_a_brief_disconnect_does_not_trigger_a_reclaim(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Только что выданный диалог не забирают.

    Обрыв связи на минуту случается постоянно; возврат по нему устроил бы
    карусель — диалог ходил бы по кругу между людьми, ни один из которых не
    успевает за него взяться.
    """
    account = await make_avito_account()
    blink = await make_user("blink@leadchat.test", role="manager")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="fresh",
        assignee_id=blink.id,
        auto_assigned_at=datetime.now(UTC),  # выдан только что
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0
    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id == blink.id


async def test_a_closed_dialog_is_not_reopened(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Закрытый диалог в очередь не возвращается: работа по нему закончена."""
    account = await make_avito_account()
    gone = await make_user("closed@leadchat.test", role="manager")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="closed",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
        status="closed",
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0
    after = await reload(db_sessionmaker, conv.id)
    assert after.status == "closed"


async def test_the_return_is_recorded(db_sessionmaker, redis, make_user, make_avito_account):
    """Возврат — не бесшумное событие.

    «Почему диалог, который мне отдали, оказался снова в очереди» — вопрос,
    который задаст первый же оператор, и ответ должен быть в журнале.
    """
    from app.models import AuditLog

    account = await make_avito_account()
    gone = await make_user("audit@leadchat.test", role="manager")
    await make_conv(
        db_sessionmaker,
        account.id,
        key="audited",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    await run_reclaim(db_sessionmaker, redis)

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "conversation.reclaimed")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details["previous_assignee_id"] == str(gone.id)
    assert rows[0].details["reason"] == "operator_offline"
    # Решение системы, а не сотрудника: приписать его человеку значило бы
    # соврать журналу.
    assert rows[0].user_id is None


async def test_replying_clears_the_mark(db_sessionmaker, redis, make_user, make_avito_account):
    """Ответ оператора снимает отметку — это основной путь, а не страховка.

    Проверяется на настоящей отправке, а не присваиванием поля: пропусти
    `messages.py` эту строчку — и диалог у человека, уже написавшего клиенту,
    останется «выданным и нетронутым».
    """
    from app.services import messages as msg_svc

    account = await make_avito_account()
    op = await make_user("reply@leadchat.test", role="manager")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="reply",
        assignee_id=op.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    async with db_sessionmaker() as s:
        user = await s.get(User, op.id)
        await msg_svc.create_outbound_message(
            s,
            redis,
            conversation_id=conv.id,
            user=user,
            text="Добрый день!",
            client_message_id="tmp-reclaim-1",
        )
        await s.commit()

    after = await reload(db_sessionmaker, conv.id)
    assert after.auto_assigned_at is None
    # И теперь сторож его уже не тронет.
    assert len(await run_reclaim(db_sessionmaker, redis)) == 0


async def test_the_return_is_announced_to_the_browsers(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Возврат обязан приехать кадрами, а не только записаться в базу.

    Без них диалог возвращается в базе, но не на экранах: у бывшего владельца
    он остался бы висеть в «Моих», а во «Входящих» у остальных не появился бы
    до перезагрузки страницы. То есть функция «работает», а человек продолжает
    считать диалог чужим.
    """
    account = await make_avito_account()
    gone = await make_user("frames@leadchat.test", role="manager")
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="frames",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    frames = await run_reclaim(db_sessionmaker, redis)
    assert len(frames) == 1

    # Кадр очереди везёт диалог ЦЕЛИКОМ: у только что подключившегося оператора
    # этой строки нет вовсе, и патчем её не собрать.
    inbox_frame = frames[0]["inbox"]
    assert inbox_frame["conversation"]["id"] == str(conv.id)
    # ⚠ КАДР ТЕПЕРЬ АДРЕСНЫЙ (аудит 19.08): вместе со строкой очереди едет
    # список операторов ЭТОГО канала. Без него кадр уезжал всем менеджерам —
    # чужой клиент звенел у тех, кто не может его взять, и «Принять» отвечало
    # 403. Пустой список сохраняет прежний смысл «канал открыт всем».
    assert "eligible" in inbox_frame, "кадр обязан нести список допущенных"

    # А списку диалогов достаточно дельты — но в ней обязан быть снятый
    # ответственный, иначе строка останется с именем ушедшего.
    patch = frames[0]["patch"]["patch"]
    assert patch["assignee"] is None
    assert patch["in_inbox"] is True


async def test_the_job_is_actually_registered():
    """Сторож обязан быть подключён в планировщике.

    Проверка жёсткая и без пропуска: потеряется строка регистрации — возврат
    замолчит целиком, и заметить это будет нечем. Отсутствие возвратов выглядит
    ровно так же, как «все операторы на месте», то есть как здоровая система.
    """
    from app.scheduler.main import build_scheduler

    ids = {job.id for job in build_scheduler().get_jobs()}
    assert reclaim.JOB_ID in ids, (
        "reclaim.register() не подключён в app/scheduler/main.py — "
        "розданные диалоги не вернутся в очередь никогда"
    )


# --- ожидание клиента переживает возврат (#27) --------------------------------


async def test_the_return_keeps_the_client_wait(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """ГЛАВНАЯ ПРОВЕРКА #27: вернувшийся диалог не выглядит новорождённым.

    ЧТО ЛОМАЛОСЬ И ПОЧЕМУ ЭТО ДОРОГО. Клиент пишет и встаёт в очередь.
    Автораздача отдаёт диалог оператору — и снимает `offered_at`, потому что из
    очереди диалог ушёл. Оператор не притрагивается и уходит из сети. Сторож
    возвращает диалог в очередь, а вернуть ему нечего: `offered_at` пуст, и
    ожидание начинается с нуля.

    Клиент, прождавший двадцать минут, встаёт в конец очереди — ПОЗАДИ тех, кто
    написал минуту назад. Очередь существует ровно затем, чтобы этого не
    случалось: она сортируется по времени ожидания. Ошибка переворачивала её
    именно для тех, кому не повезло попасть под неудачную раздачу.

    В `return_to_queue` при этом стояло, что ожидание сохраняется. Оно и
    сохранялось — но только на пути «принял руками и передумал», где
    `offered_at` не снимается. На пути автораздачи сохранять было уже нечего.
    """
    account = await make_avito_account()
    gone = await make_user("wait-gone@leadchat.test", role="manager", full_name="Ушедший")
    waiting_since = datetime.now(UTC) - timedelta(minutes=20)
    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="keeps-wait",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
        awaiting_since=waiting_since,
        offered_at=None,  # ровно так его оставила автораздача
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 1

    after = await reload(db_sessionmaker, conv.id)
    assert inbox_svc.is_waiting(after)
    waited = inbox_svc.waiting_seconds(after)
    assert waited is not None and waited >= 19 * 60, (
        f"клиент ждёт двадцать минут, а очередь думает, что {waited} с"
    )


async def test_a_returned_dialog_stands_ahead_of_a_newcomer(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Тот же случай глазами очереди: вернувшийся впереди только что пришедшего.

    Проверка отдельная, потому что цифра «ждёт 20 мин» — это ещё полбеды. Беда
    в порядке: очередь сортируется по `offered_at`, и обнулённое ожидание
    отправляло давнего клиента в самый низ списка.
    """
    account = await make_avito_account()
    gone = await make_user("order-gone@leadchat.test", role="manager")
    returned = await make_conv(
        db_sessionmaker,
        account.id,
        key="order-old",
        assignee_id=gone.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
        awaiting_since=datetime.now(UTC) - timedelta(minutes=20),
        offered_at=None,
    )
    newcomer = await make_conv(
        db_sessionmaker,
        account.id,
        key="order-new",
        status="new",
        offered_at=datetime.now(UTC) - timedelta(minutes=1),
        awaiting_since=datetime.now(UTC) - timedelta(minutes=1),
    )

    await run_reclaim(db_sessionmaker, redis)

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(Conversation.id)
                    .where(inbox_svc.queue_condition())
                    .order_by(Conversation.offered_at.asc(), Conversation.id)
                )
            )
            .scalars()
            .all()
        )
    assert rows.index(returned.id) < rows.index(newcomer.id), (
        "давно ждущий клиент обязан стоять впереди только что написавшего"
    )


async def test_a_fresh_wait_starts_when_the_client_is_not_waiting(
    make_avito_account,
):
    """Обратная граница: если клиенту уже ответили, ожидание считается заново.

    Без этой проверки «сохраняй ожидание» легко превратить в «ожидание никогда
    не обнуляется» — и диалог, в котором оператор ответил час назад, вечно
    висел бы наверху очереди как самый заждавшийся.
    """
    conv = Conversation(
        channel="avito",
        external_chat_id="fresh-wait",
        status="in_progress",
        offered_at=None,
        awaiting_since=None,  # оператор ответил — клиент никого не ждёт
    )
    moment = datetime.now(UTC)
    inbox_svc.return_to_queue(conv, now=moment)
    assert conv.offered_at == moment


# --- «отошёл» не значит «ушёл» (#34) -----------------------------------------


async def test_a_dialog_is_not_taken_from_someone_who_stepped_away(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Обед не должен стоить оператору его работы.

    ГРАНИЦА, КОТОРУЮ ЛЕГКО ПЕРЕЙТИ. «Отошёл» — состояние человека, который
    ЗА СТОЛОМ, просто не берёт новых. Сочти сторож его ушедшим — и обед стоил
    бы всей начатой работы: диалоги уехали бы в очередь, клиенты получили бы
    нового собеседника с середины разговора, а в журнале это записалось бы
    как `operator_offline` — неправдой.

    Поэтому автораздача читает СТАТУС (и отошедшего пропускает), а сторожа
    читают «открыто ли приложение» (и отошедшего не трогают). Две разные
    функции присутствия, и это не дублирование, а разные вопросы.
    """
    account = await make_avito_account()
    lunch = await make_user("reclaim-away@leadchat.test", role="manager", full_name="Обедает")
    await redis.set(_status_key(lunch.id), "away")

    conv = await make_conv(
        db_sessionmaker,
        account.id,
        key="away-keeps",
        assignee_id=lunch.id,
        auto_assigned_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert len(await run_reclaim(db_sessionmaker, redis)) == 0

    after = await reload(db_sessionmaker, conv.id)
    assert after.assignee_id == lunch.id, "диалог отобрали у человека, который просто отошёл"
