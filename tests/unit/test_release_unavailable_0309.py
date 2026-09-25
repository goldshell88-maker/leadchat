"""Диалоги того, кого нет за столом, освобождаются сами (просьба 03.09).

⚠ ЖАЛОБА ВЛАДЕЛЬЦА ДОСЛОВНО: «диалоги не уходят из моих, когда я не в сети, и
если бы клиент ответил, то мы бы потеряли диалог, так как я не в сети и не могу
отвечать».

Правило: человек недоступен дольше пятнадцати минут — его диалоги освобождаются.
Ждёт КЛИЕНТ — диалог уходит во «Входящие», к тринадцати парам глаз. Ждём не мы
(ответили, клиент молчит) — диалог закрывается; напишет клиент снова, диалог сам
переоткроется и попадёт во «Входящие».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Client, Conversation
from app.scheduler.jobs import reclaim
from app.services import inbox as inbox_svc
from app.ws import presence
from app.ws.presence import _status_key

pytestmark = pytest.mark.anyio

ДАВНО = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


async def сделать_диалог(
    db_sessionmaker,
    account_id,
    *,
    key: str,
    assignee_id,
    status: str = "in_progress",
    awaiting_since: datetime | None = None,
    bot_active: bool = False,
    transfer_to_id=None,
) -> Conversation:
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"ru-{key}", name=f"Клиент {key}")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"ru-chat-{key}",
            account_id=account_id,
            client_id=cl.id,
            status=status,
            assignee_id=assignee_id,
            awaiting_since=awaiting_since,
            bot_active=bot_active,
            transfer_to_id=transfer_to_id,
            last_message_at=ДАВНО,
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)
        return conv


async def перечитать(db_sessionmaker, conv_id) -> Conversation:
    async with db_sessionmaker() as s:
        return await s.get(Conversation, conv_id)


async def прогон(db_sessionmaker, redis, *, now: datetime | None = None) -> list[dict]:
    async with db_sessionmaker() as s:
        frames = await reclaim.release_in_session(s, redis, now=now)
        await s.commit()
        return frames


async def _пометить_недоступным(redis, user_id, *, минут_назад: int, now: datetime) -> None:
    """Сторож увидел человека недоступным столько-то минут назад."""
    await redis.set(
        reclaim._UNAVAILABLE_KEY.format(user_id=user_id),
        (now - timedelta(minutes=минут_назад)).isoformat(),
    )


# ============================================================ два исхода


async def test_неотвеченный_диалог_уходит_во_входящие(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ГЛАВНОЕ В ЖАЛОБЕ: клиент ждёт, а видит это только отсутствующий."""
    account = await make_avito_account()
    ушёл = await make_user("gone1@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="waiting",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    кадры = await прогон(db_sessionmaker, redis, now=сейчас)
    assert len(кадры) == 1
    assert кадры[0]["inbox"] is not None, (
        "кадр очереди обязателен: без него строка не появится ни у кого"
    )

    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id is None
    assert после.status != inbox_svc.CLOSED, "клиента, который ждёт, закрывать нельзя"
    assert inbox_svc.is_waiting(после), "диалог обязан стоять во «Входящих»"


async def test_отвеченный_диалог_просто_закрывается(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ЗАКРЫТИЕ БЕЗОПАСНО ИМЕННО ПОТОМУ, ЧТО ОБРАТИМО.

    Клиенту не происходит ничего: закрытие внутреннее. Напишет он снова —
    `services/inbound` снимет ответственного, поставит статус `new` и положит
    диалог во «Входящие», а бывшему владельцу уйдёт «клиент вернулся».
    """
    account = await make_avito_account()
    ушёл = await make_user("gone2@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="answered",
        assignee_id=ушёл.id,
        awaiting_since=None,  # мы ответили, клиент молчит
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    кадры = await прогон(db_sessionmaker, redis, now=сейчас)
    assert len(кадры) == 1
    assert кадры[0]["inbox"] is None, "закрытый диалог во «Входящие» не кладут"

    после = await перечитать(db_sessionmaker, conv.id)
    assert после.status == inbox_svc.CLOSED
    assert после.assignee_id == ушёл.id, (
        "ответственный снят: обнулился бы отчёт по сотруднику, а из рабочего "
        "вида диалог и так уходит — вкладка «Мои» закрытые не показывает"
    )


# ============================================================ когда НЕ трогать


async def test_отлучка_короче_порога_ничего_не_стоит(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ОБЕД НЕ ДОЛЖЕН СТОИТЬ ЧЕЛОВЕКУ ЕГО РАБОТЫ. Пятнадцать минут — это уже
    не отлучка к принтеру; всё, что меньше, сторож пропускает."""
    account = await make_avito_account()
    вышел = await make_user("lunch@leadchat.test", role="manager", full_name="На обеде")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(db_sessionmaker, account.id, key="lunch", assignee_id=вышел.id)
    await _пометить_недоступным(redis, вышел.id, минут_назад=14, now=сейчас)

    assert await прогон(db_sessionmaker, redis, now=сейчас) == []
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id == вышел.id and после.status != inbox_svc.CLOSED


async def test_человек_на_месте_диалоги_свои(db_sessionmaker, redis, make_user, make_avito_account):
    account = await make_avito_account()
    на_месте = await make_user("here@leadchat.test", role="manager", full_name="На месте")
    await redis.set(_status_key(на_месте.id), presence.ONLINE)
    conv = await сделать_диалог(db_sessionmaker, account.id, key="online", assignee_id=на_месте.id)

    assert await прогон(db_sessionmaker, redis) == []
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id == на_месте.id


async def test_возвращение_сбрасывает_отсчёт(db_sessionmaker, redis, make_user, make_avito_account):
    """⚠ ВЕРНУЛСЯ — ОТСЧЁТ НАЧИНАЕТСЯ ЗАНОВО. Иначе человек, отлучавшийся утром,
    вечером терял бы диалоги за отлучку, которой уже не было."""
    account = await make_avito_account()
    вернулся = await make_user("back@leadchat.test", role="manager", full_name="Вернулся")
    сейчас = datetime.now(UTC)
    await сделать_диалог(db_sessionmaker, account.id, key="back", assignee_id=вернулся.id)
    await _пометить_недоступным(redis, вернулся.id, минут_назад=20, now=сейчас)

    await redis.set(_status_key(вернулся.id), presence.ONLINE)
    assert await прогон(db_sessionmaker, redis, now=сейчас) == []
    assert await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=вернулся.id)) is None, (
        "отметка не снята: следующий уход зачтётся вместе с этим и диалоги уедут сразу же"
    )


async def test_отсчёт_не_начинается_заново_каждый_проход(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ БЕЗ ЭТОГО ПОРОГ НЕ НАСТУПИЛ БЫ НИКОГДА.

    Отметку ставит первый проход, увидевший отсутствие. Сдвигай её каждую
    минуту — и «недоступен пятнадцать минут» не случится ни разу, а сторож
    будет выглядеть работающим.
    """
    account = await make_avito_account()
    ушёл = await make_user("clock@leadchat.test", role="manager", full_name="Ушедший")
    старт = datetime.now(UTC)
    await сделать_диалог(db_sessionmaker, account.id, key="clock", assignee_id=ушёл.id)

    # три прохода подряд с интервалом в минуту — отметка обязана остаться первой
    for минута in (0, 1, 2):
        await прогон(db_sessionmaker, redis, now=старт + timedelta(minutes=минута))
    сырое = await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=ушёл.id))
    assert сырое is not None
    assert datetime.fromisoformat(сырое) == старт, "отметка сдвинулась — отсчёт обнулился"


async def test_бот_ведёт_диалог_сам(db_sessionmaker, redis, make_user, make_avito_account):
    """Бот отвечает клиенту сам — отсутствие человека ничего не решает."""
    account = await make_avito_account()
    ушёл = await make_user("bot@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="bot",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
        bot_active=True,
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    assert await прогон(db_sessionmaker, redis, now=сейчас) == []
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id == ушёл.id


async def test_передача_в_полёте_не_трогается(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ДВА СТОРОЖА НА ОДНУ СТРОКУ — ЭТО ГОНКА. Просроченными предложениями
    передачи владеет `expire_transfers`, и решать за него нельзя."""
    account = await make_avito_account()
    ушёл = await make_user("tr1@leadchat.test", role="manager", full_name="Ушедший")
    кому = await make_user("tr2@leadchat.test", role="manager", full_name="Получатель")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="transfer",
        assignee_id=ушёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
        transfer_to_id=кому.id,
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    assert await прогон(db_sessionmaker, redis, now=сейчас) == []
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id == ушёл.id


async def test_сторож_зарегистрирован_в_планировщике():
    """⚠ НАПИСАННЫЙ И НЕ ПОДКЛЮЧЁННЫЙ СТОРОЖ — САМЫЙ ЧАСТЫЙ ДЕФЕКТ ПРОЕКТА."""

    class Планировщик:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def add_job(self, _fn, _trigger, *, id, **kw):  # noqa: A002, ANN001, ANN003
            self.ids.append(id)

    s = Планировщик()
    reclaim.register(s)
    assert reclaim.RELEASE_JOB_ID in s.ids


async def test_отошёл_считается_недоступностью(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ГЛАВНОЕ ОТЛИЧИЕ ОТ СОСЕДНЕГО СТОРОЖА, И ОНО НАМЕРЕННОЕ.

    `reclaim_in_session` считает «отошёл» присутствием — и правильно делает:
    он отдаёт диалог ДРУГОМУ человеку, а обед не повод менять клиенту
    собеседника посреди разговора.

    Здесь способ другой (во «Входящие» либо закрыть), и «отошёл» обязан
    считаться недоступностью: отошедший не ответит клиенту так же, как и
    закрывший вкладку. Возьми здесь `presence_map` вместо
    `presence_status_map` — и правка перестанет работать для половины случаев,
    оставшись при этом зелёной на всех остальных тестах.
    """
    account = await make_avito_account()
    отошёл = await make_user("away@leadchat.test", role="manager", full_name="Отошёл")
    сейчас = datetime.now(UTC)
    # Приложение открыто и отвечает на пинги — но человек сам сказал «отошёл».
    await redis.set(_status_key(отошёл.id), presence.AWAY)
    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="away",
        assignee_id=отошёл.id,
        awaiting_since=сейчас - timedelta(minutes=5),
    )
    await _пометить_недоступным(redis, отошёл.id, минут_назад=20, now=сейчас)

    кадры = await прогон(db_sessionmaker, redis, now=сейчас)
    assert len(кадры) == 1, "отошедший считается присутствующим — клиент ждёт впустую"

    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id is None
    assert inbox_svc.is_waiting(после)


async def test_автозакрытие_не_зачитывается_менеджеру(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ОТЧЁТ «ЗАКРЫТО» ПО МЕНЕДЖЕРУ СЧИТАЕТ ПО `details->>'assignee_id'`.

    Положи в журнал это имя — и каждое автозакрытие зачтётся человеку как его
    работа, хотя закрыл диалог сторож, а человека за столом не было. Число
    «закрыл за смену» раздулось бы у того, кто раньше всех ушёл.
    """
    import sqlalchemy as sa

    from app.models import AuditLog

    account = await make_avito_account()
    ушёл = await make_user("stats@leadchat.test", role="manager", full_name="Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(db_sessionmaker, account.id, key="stats", assignee_id=ушёл.id)
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)
    await прогон(db_sessionmaker, redis, now=сейчас)

    async with db_sessionmaker() as s:
        (запись,) = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.entity_id == str(conv.id))))
            .scalars()
            .all()
        )

    assert запись.details.get("to") == inbox_svc.CLOSED
    assert "assignee_id" not in запись.details, (
        "автозакрытие зачлось менеджеру как его работа — раздуется «закрыл за смену»"
    )
    assert запись.details.get("previous_assignee_id") == str(ушёл.id), (
        "кто вёл диалог, из журнала пропало — разбирать будет нечего"
    )


async def test_работающий_человек_держит_диалог_сколько_бы_клиент_ни_ждал(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """⚠ ВОПРОС ВЛАДЕЛЬЦА 03.09 ДОСЛОВНО: «когда человек работает и у него
    появится диалог, на который не будет ответа 15 минут, они же не появятся во
    входящих?»

    Не появятся, и вот почему это не совпадение: отсчёт здесь идёт по
    НЕДОСТУПНОСТИ ЧЕЛОВЕКА, а не по времени ожидания клиента. Человек в сети —
    отметка снимается на каждом проходе, и порог не наступает никогда, сколько
    бы клиент ни ждал.

    За «клиент ждёт слишком долго» отвечает другой сторож (`jobs/awaiting`), и
    он работающему человеку шлёт НАПОМИНАНИЕ, а в очередь возвращает только
    если сотрудника нет в сети И он в этом диалоге не написал ни слова.

    Перепутать эти два отсчёта — значит отобрать диалог у человека, который
    сидит за столом и как раз собирается ответить.
    """
    account = await make_avito_account()
    работает = await make_user("busy@leadchat.test", role="manager", full_name="Работает")
    сейчас = datetime.now(UTC)
    await redis.set(_status_key(работает.id), presence.ONLINE)

    conv = await сделать_диалог(
        db_sessionmaker,
        account.id,
        key="busy",
        assignee_id=работает.id,
        # клиент ждёт СОРОК минут — куда дольше порога сторожа
        awaiting_since=сейчас - timedelta(minutes=40),
    )
    # И даже если от прошлой отлучки осталась старая отметка — она обязана
    # сняться, а не сработать.
    await _пометить_недоступным(redis, работает.id, минут_назад=60, now=сейчас)

    assert await прогон(db_sessionmaker, redis, now=сейчас) == [], (
        "диалог отобрали у человека, который в сети: отсчёт перепутан с ожиданием клиента"
    )
    после = await перечитать(db_sessionmaker, conv.id)
    assert после.assignee_id == работает.id
    assert после.status != inbox_svc.CLOSED
    assert await redis.get(reclaim._UNAVAILABLE_KEY.format(user_id=работает.id)) is None
