"""Клиент написал в рабочий диалог и ждёт ответа (требование от 7 августа).

Проверяется не «шлётся ли уведомление». Проверяется ГРАНИЦА: где система
помогает, а где начинает вредить.

Отнять диалог у человека, который им занимается, — худшее, что здесь можно
сделать: переписка обрывается на середине, клиент получает нового
собеседника, не знающего, о чём шла речь. Поэтому основная часть тестов ниже
про то, у кого отбирать НЕЛЬЗЯ.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation, Message
from app.models.notification import Notification

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
async def job(monkeypatch, db_sessionmaker, redis):
    """Сторож, подключённый к тестовой сессии и Redis."""
    from app.scheduler.jobs import awaiting as mod

    monkeypatch.setattr(mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(mod.redis_mod, "get_client", lambda: redis)
    return mod


async def _taken(db_sessionmaker, account, owner, *, waiting_minutes: float, tag: str) -> uuid.UUID:
    """Диалог, который взяли руками и в котором клиент ждёт ответа."""
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"aw-{tag}", name=f"Клиент {tag}")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"aw-chat-{tag}",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=owner.id,
            claimed_by_id=owner.id,
            awaiting_since=NOW - timedelta(minutes=waiting_minutes),
            last_message_at=NOW - timedelta(minutes=waiting_minutes),
        )
        s.add(conv)
        await s.commit()
        return conv.id


async def _reply(db_sessionmaker, conv_id, owner, *, minutes_ago: float) -> None:
    """Оператор написал клиенту — значит диалог он ведёт."""
    async with db_sessionmaker() as s:
        s.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv_id,
                direction="out",
                sender_type="operator",
                sender_user_id=owner.id,
                body="сейчас уточню",
                attachments=[],
                delivery_status="delivered",
                created_at=NOW - timedelta(minutes=minutes_ago),
            )
        )
        await s.commit()


@pytest.fixture
async def owner(make_user):
    return await make_user("owner-aw@leadchat.test", role="manager", full_name="Иван Ведущий")


async def test_the_operator_gets_a_personal_reminder(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """Первая ступень — напоминание тому, кто ведёт.

    Самая частая причина молчания — человек отвлёкся, и одного напоминания
    достаточно. Поднимать из-за этого руководителя значит приучить его не
    смотреть на уведомления.

    ЧЕЛОВЕК ЗДЕСЬ В СЕТИ, И ЭТО НЕ ДЕТАЛЬ ОБСТАНОВКИ. Напоминают только тому,
    у кого диалог остался. Прежняя версия этого теста присутствия не ставила —
    то есть проверяла напоминание тому, у кого тем же проходом диалог
    отбирали: `touched: 1, returned: 1` в её же выводе.
    """
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=20, tag="remind")
    await presence_connected(redis, owner.id, "conn-remind")

    assert await job.check_awaiting(now=NOW) == 1

    async with db_sessionmaker() as s:
        notes = list(
            (
                await s.execute(
                    sa.select(Notification).where(Notification.kind == "conversation.awaiting_you")
                )
            )
            .scalars()
            .all()
        )
    assert len(notes) == 1
    assert notes[0].recipient_id == owner.id, "напоминание личное, а не всем"
    assert notes[0].entity_id == str(conv_id)
    assert "20 мин" in (notes[0].body or "")


async def test_a_fresh_wait_bothers_nobody(db_sessionmaker, make_avito_account, owner, job):
    """Пять минут — это ещё «сейчас допишу», а не молчание."""
    account = await make_avito_account()
    await _taken(db_sessionmaker, account, owner, waiting_minutes=5, tag="fresh")

    assert await job.check_awaiting(now=NOW) == 0


async def test_after_half_an_hour_the_head_is_told(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """Вторая ступень. Оператор не среагировал — разбирать это руководителю.

    ОПЕРАТОР ЗДЕСЬ В СЕТИ, И ЭТО НЕ ДЕТАЛЬ ОБСТАНОВКИ. Прежняя версия теста
    присутствия не ставила — то есть строила диалог, который тем же проходом
    ОТБИРАЛИ (её собственный вывод: `touched: 1, returned: 1`), и требовала,
    чтобы в тексте стояло имя бывшего владельца. Так она и закрепляла дефект
    №4 боевого разбора: «Иванов не отвечает клиенту 403 мин» про диалог, у
    которого ответственного нет вообще. Ступень «человек на месте, но молчит»
    проверяется на человеке НА МЕСТЕ; ничей диалог — соседним тестом.
    """
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    await _taken(db_sessionmaker, account, owner, waiting_minutes=40, tag="escalate")
    await presence_connected(redis, owner.id, "conn-escalate")

    await job.check_awaiting(now=NOW)

    async with db_sessionmaker() as s:
        note = (
            await s.execute(
                sa.select(Notification).where(Notification.kind == "conversation.no_reply")
            )
        ).scalar_one()
    assert "Иван Ведущий" in (note.body or ""), "руководителю важно, КТО не отвечает"
    assert note.audience == "head"


class TestWhoseDialogIsNeverTaken:
    """ГЛАВНОЕ: у кого отбирать нельзя.

    Забрать диалог посреди разговора хуже, чем не забрать вовсе.
    """

    async def test_an_operator_who_is_online_keeps_his_dialog(
        self, db_sessionmaker, make_avito_account, owner, job, redis
    ):
        """Человек за столом — диалог его.

        «Не отвечает» по времени — признак ненадёжный: оператор мог позвонить
        клиенту и договориться о выезде, и по переписке этого не видно вовсе.
        """
        from app.ws.presence import presence_connected

        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=90, tag="online")
        await presence_connected(redis, owner.id, "conn-1")

        await job.check_awaiting(now=NOW)

        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv_id)
        assert row.assignee_id == owner.id, "у работающего диалог не отбирают"
        assert row.offered_at is None, "и в очередь он не возвращается"

    async def test_an_operator_who_already_answered_keeps_it_even_offline(
        self, db_sessionmaker, make_avito_account, owner, job
    ):
        """Написал — значит ведёт, даже если сейчас вышел.

        Клиент задал второй вопрос после ответа оператора; тот ушёл на обед.
        Отдать диалог другому — значит начать разговор заново.
        """
        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=90, tag="replied")
        await _reply(db_sessionmaker, conv_id, owner, minutes_ago=60)

        await job.check_awaiting(now=NOW)

        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv_id)
        assert row.assignee_id == owner.id

    async def test_an_absent_operator_who_never_answered_keeps_it(
        self, db_sessionmaker, make_avito_account, owner, job
    ):
        """Сторож только напоминает. Возврат диалога отсутствующего — работа
        release_unavailable: у неё выключатель, исключения и выдержка, которые
        обещает экран «Распределение»."""
        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=90, tag="lost")

        await job.check_awaiting(now=NOW)

        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv_id)
            kinds = [
                n.kind
                for n in (
                    await s.execute(
                        sa.select(Notification).where(Notification.entity_id == str(conv_id))
                    )
                ).scalars()
            ]
        assert row.assignee_id == owner.id
        assert row.offered_at is None
        assert "conversation.awaiting_you" in kinds, "напоминание ждёт его возвращения"

    async def test_a_note_is_not_an_answer(self, db_sessionmaker, make_avito_account, owner, job):
        """Заметка для своих клиенту не видна — он всё ещё ждёт, и напоминание идёт."""
        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=90, tag="note")
        async with db_sessionmaker() as s:
            s.add(
                Message(
                    id=uuid.uuid4(),
                    conversation_id=conv_id,
                    direction="note",
                    sender_type="operator",
                    sender_user_id=owner.id,
                    body="перезвонить после обеда",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=NOW - timedelta(minutes=60),
                )
            )
            await s.commit()

        await job.check_awaiting(now=NOW)

        async with db_sessionmaker() as s:
            reminders = (
                (
                    await s.execute(
                        sa.select(Notification).where(
                            Notification.kind == "conversation.awaiting_you",
                            Notification.entity_id == str(conv_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(reminders) == 1


class TestTheMarkItself:
    """Отметка «клиент ждёт» ставится и снимается там, где надо."""

    async def test_a_persistent_client_waits_from_his_first_message(
        self, db_sessionmaker, make_avito_account, owner
    ):
        """Пять сообщений подряд — ждёт с ПЕРВОГО.

        Обновляй мы отметку на каждом, самый настойчивый клиент выглядел бы
        самым свежим — ровно наоборот.
        """
        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=50, tag="many")
        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv_id)
            first = row.awaiting_since
            # Второе сообщение того же клиента — отметку не двигаем.
            if row.awaiting_since is None:
                row.awaiting_since = NOW
            await s.commit()

        async with db_sessionmaker() as s:
            assert (await s.get(Conversation, conv_id)).awaiting_since == first

    async def test_closing_ends_the_wait(self, db_sessionmaker, make_avito_account, owner):
        """Закрыли — ждать нечего. Иначе диалог напоминал бы о себе вечно."""
        from app.services import conversations as convs

        account = await make_avito_account()
        conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=50, tag="closed")

        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv_id)
            await convs.change_status(s, row, new_status="closed", actor=owner)
            await s.commit()

        async with db_sessionmaker() as s:
            assert (await s.get(Conversation, conv_id)).awaiting_since is None


def test_the_guard_is_actually_scheduled():
    """Сторож без регистрации не запускается НИКОГДА, а ошибка бесшумна."""
    from app.scheduler.main import build_scheduler

    assert "awaiting_reply" in {j.id for j in build_scheduler().get_jobs()}


# =============================================================================
# Один человек, один диалог — одна строка (найдено разбором боевой системы)
# =============================================================================


async def _rows_for(db_sessionmaker, conv_id):
    async with db_sessionmaker() as s:
        return list(
            (await s.execute(sa.select(Notification).where(Notification.entity_id == str(conv_id))))
            .scalars()
            .all()
        )


def _visible_to(rows, user):
    """Строки, которые этот человек увидит: свои адресные плюс его рассылки."""
    from app.services.notifications import visible_audiences

    seen = set(visible_audiences(user.role))
    return [r for r in rows if r.recipient_id == user.id or (r.audience in seen)]


async def test_a_head_who_leads_the_dialog_is_not_told_about_himself_twice(
    db_sessionmaker, make_avito_account, make_user, job, redis
):
    """Диалог ведёт руководитель — и обе ступени сходятся на нём одном.

    В колокольчик приходило две строки про один диалог: личное «Клиент ждёт
    вашего ответа» и следом руководительское «Пётр Руководящий не отвечает
    клиенту 40 мин» — то есть он сам, про себя. Гасится рассылка, а не личное
    напоминание: личное адресовано единственному, кто может ответить, и
    приходит на пятнадцать минут раньше.

    Проверка ломанием: уберите ``owner_id in head_readers`` из условия пропуска
    рассылки — падает этот тест.
    """
    from app.ws.presence import presence_connected

    head = await make_user("head-dup@leadchat.test", role="head", full_name="Пётр Руководящий")
    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, head, waiting_minutes=40, tag="head-dup")
    await presence_connected(redis, head.id, "conn-head-dup")

    await job.check_awaiting(now=NOW)

    seen = _visible_to(await _rows_for(db_sessionmaker, conv_id), head)
    assert [r.kind for r in seen] == ["conversation.awaiting_you"], (
        f"один человек, один диалог, а строк {len(seen)}: {[r.kind for r in seen]}"
    )


async def test_a_manager_still_gets_the_reminder_and_the_head_still_gets_the_escalation(
    db_sessionmaker, make_avito_account, make_user, owner, job, redis
):
    """Обычный расклад не тронут: разным людям — разные строки.

    Гасить рассылку целиком было бы куда хуже дубля: тогда о молчащем диалоге
    не узнал бы никто, кроме самого молчащего.
    """
    from app.ws.presence import presence_connected

    boss = await make_user("boss-ok@leadchat.test", role="head", full_name="Глава Смены")
    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=40, tag="normal")
    await presence_connected(redis, owner.id, "conn-normal")

    await job.check_awaiting(now=NOW)

    rows = await _rows_for(db_sessionmaker, conv_id)
    assert [r.kind for r in _visible_to(rows, owner)] == ["conversation.awaiting_you"]
    assert [r.kind for r in _visible_to(rows, boss)] == ["conversation.no_reply"]


async def test_an_absent_head_leading_the_dialog_gets_one_line(
    db_sessionmaker, make_avito_account, make_user, job
):
    """Руководитель не в сети и ведёт диалог сам: личное напоминание есть, рассылки
    про него же нет, и диалог остаётся за ним."""
    head = await make_user("head-gone@leadchat.test", role="head", full_name="Пётр Ушедший")
    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, head, waiting_minutes=40, tag="head-gone")

    await job.check_awaiting(now=NOW)

    rows = await _rows_for(db_sessionmaker, conv_id)
    assert [r.kind for r in _visible_to(rows, head)] == ["conversation.awaiting_you"]
    async with db_sessionmaker() as s:
        assert (await s.get(Conversation, conv_id)).assignee_id == head.id


async def test_a_head_already_reminded_is_not_told_a_second_time_when_he_leaves(
    db_sessionmaker, make_avito_account, make_user, job, redis
):
    """Руководитель получил личное напоминание и ушёл, не ответив: второй строки
    про тот же диалог нет, и диалог остаётся за ним."""
    from app.ws.presence import _status_key, presence_connected

    head = await make_user("head-late@leadchat.test", role="head", full_name="Пётр Уставший")
    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, head, waiting_minutes=0, tag="head-late")
    await presence_connected(redis, head.id, "conn-head-late")

    # За столом: получает личное напоминание на пятнадцатой минуте.
    await job.check_awaiting(now=NOW + timedelta(minutes=20))
    assert [r.kind for r in await _rows_for(db_sessionmaker, conv_id)] == [
        "conversation.awaiting_you"
    ], "первая ступень обязана сработать, иначе тест дальше проверяет пустоту"

    # Закрыл ноутбук, так и не ответив. Присутствие гасим ключом: разрыв сокета
    # выжидает тридцать секунд на переподключение, а проверяется здесь не он.
    await redis.delete(_status_key(head.id))
    await job.check_awaiting(now=NOW + timedelta(minutes=45))

    rows = await _rows_for(db_sessionmaker, conv_id)
    assert [r.kind for r in _visible_to(rows, head)] == ["conversation.awaiting_you"], (
        f"один человек, один диалог, а строк {len(_visible_to(rows, head))}: "
        f"{[r.kind for r in _visible_to(rows, head)]}"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Conversation, conv_id)).assignee_id == head.id


# =============================================================================
# Разбор боевой системы 12 августа: дубликаты, счётчики, минуты, ложные имена
# =============================================================================


async def test_two_dialogs_are_not_two_identical_lines(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """«ПО ДВЕ ИДЕНТИЧНЫЕ ЗАПИСИ» — ЭТО ДВА РАЗНЫХ ДИАЛОГА, А НЕ ПРОМАХ СКЛЕЙКИ.

    В колокольчике 10 августа висели по две строки «Клиент ждёт ответа 58 мин ·
    повторялось 44 раза». Склейка при этом работала как задумано: она идёт по
    СУЩНОСТИ, и два разных диалога разводить обязана. Совпадал текст — он не
    называл ни клиента, ни диалог, а число минут у двух клиентов, написавших в
    одну минуту, одинаково по определению. Вплоть до счётчика повторов, потому
    что и он тикал по часам (см. соседний тест про лестницу).

    Отличить такие строки было нечем даже теоретически — и читалось это как
    поломка центра уведомлений, то есть подрывало доверие ко всем остальным
    его строкам разом.

    Проверка ломанием: верните тело к `f"Клиент ждёт ответа {...}"` — оба
    ассерта ниже падают.
    """
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    await _taken(db_sessionmaker, account, owner, waiting_minutes=58, tag="Первый")
    await _taken(db_sessionmaker, account, owner, waiting_minutes=58, tag="Второй")
    await presence_connected(redis, owner.id, "conn-two")

    await job.check_awaiting(now=NOW)

    async with db_sessionmaker() as s:
        bodies = [
            r.body
            for r in (
                await s.execute(
                    sa.select(Notification).where(Notification.kind == "conversation.awaiting_you")
                )
            )
            .scalars()
            .all()
        ]
    assert len(bodies) == 2, "диалога два — строк тоже две, склеивать их нечего"
    assert len(set(bodies)) == 2, f"две строки читаются одинаково: {bodies}"
    assert any("Первый" in (b or "") for b in bodies), "в строке должно быть видно, КТО ждёт"


async def test_a_nameless_client_is_named_by_his_advert(db_sessionmaker):
    """У клиента без имени подпись берётся из объявления.

    Авито отдаёт имя не всегда (см. `client_enrich`), а «Клиент» вместо имени
    возвращает ровно ту неразличимость, ради которой всё переписано. Заголовок
    объявления в этом деле и есть суть разговора — «что сломалось».
    """
    from app.scheduler.jobs.awaiting import _client_label

    assert _client_label("Пётр Иванов", "Ремонт плиты") == "Пётр Иванов"
    assert _client_label("  ", "Ремонт плиты") == "Клиент по объявлению «Ремонт плиты»"
    assert _client_label(None, None) == "Клиент"


async def test_the_repeat_counter_is_a_ladder_and_not_a_clock(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """СЧЁТЧИК ПОВТОРОВ ДОХОДИЛ ДО 142, И ЭТО БЫЛА НЕ ЭСКАЛАЦИЯ.

    Задача бежит раз в минуту, уведомление уходило на каждом проходе. Склейка
    честно складывала их в одну строку — шторма в колокольчике не было, но был
    счётчик, растущий сам по себе, ровно по часам. Строка, меняющаяся каждую
    минуту, перестаёт быть новостью через час; «142» и «143» человек не
    различает вовсе.

    Теперь ступени удваивают ожидание: 15 → 30 → 60 → 120 → 240. За два часа
    это четыре напоминания вместо ста шести, и каждое означает ровно одно и
    притом новое: клиент ждёт ВДВОЕ дольше, чем в прошлый раз.

    Прогон именно поминутный, а не «два вызова по краям»: беда была ровно в
    том, что задача бежит каждую минуту, и проверять надо это.

    Проверка ломанием: снимите условие ступени (`> remind_sent`) — счётчик
    станет 106, и тест упадёт.
    """
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=0, tag="ladder")
    await presence_connected(redis, owner.id, "conn-ladder")

    for minute in range(1, 121):  # два часа поминутных прогонов
        await job.check_awaiting(now=NOW + timedelta(minutes=minute))

    rows = {r.kind: r for r in await _rows_for(db_sessionmaker, conv_id)}
    assert rows["conversation.awaiting_you"].repeat_count == 4, (
        "ступени личного напоминания: 15, 30, 60, 120 минут"
    )
    assert rows["conversation.no_reply"].repeat_count == 3, (
        "ступени рассылки руководителям: 30, 60, 120 минут"
    )


def test_the_ladder_doubles_and_then_flattens_at_the_cap():
    """Сама лестница — отдельно от базы: где ступени и где кончается удвоение.

    Через сутки ожидания счётчик обязан быть двузначным, а не четырёхзначным:
    в этом вся правка. Потолок шага держит напоминания и после того, как
    удваивать стало некуда, — диалог, о котором перестали напоминать вовсе,
    тихо исчезает с радаров, а клиент продолжает ждать.
    """
    from app.scheduler.jobs.awaiting import ESCALATE_AFTER, REMIND_AFTER, _reminders_due

    due = lambda minutes: _reminders_due(timedelta(minutes=minutes), REMIND_AFTER)  # noqa: E731
    assert due(14) == 0, "до первого порога не напоминаем вовсе"
    assert [due(m) for m in (15, 29, 30, 59, 60, 119, 120, 239, 240)] == [
        1,
        1,
        2,
        2,
        3,
        3,
        4,
        4,
        5,
    ]
    # Дальше удваивать некуда — шаг фиксируется на четырёх часах.
    assert [due(m) for m in (479, 480, 719, 720)] == [5, 6, 6, 7]
    assert due(24 * 60) == 10, "за сутки десять ступеней, а не тысяча четыреста"

    boss = lambda minutes: _reminders_due(timedelta(minutes=minutes), ESCALATE_AFTER)  # noqa: E731
    assert [boss(m) for m in (29, 30, 60, 120, 240)] == [0, 1, 2, 3, 4]


def test_the_ladder_step_stays_inside_the_dedup_window():
    """Потолок шага обязан быть МЕНЬШЕ окна склейки повторов.

    Иначе лестница ломается тихо и незаметно: строка выпадает из окна раньше,
    чем приходит следующая ступень, `notify` заводит НОВУЮ запись — и вместо
    одной строки со счётчиком в колокольчике снова копится куча, только реже.
    Счётчик при этом всегда показывает «1», то есть выглядит здоровым.
    """
    from app.scheduler.jobs.awaiting import STEP_CAP
    from app.services.notifications import DEDUP_WINDOWS, KINDS

    for kind in ("conversation.awaiting_you", "conversation.no_reply"):
        window = DEDUP_WINDOWS[KINDS[kind].severity]
        assert STEP_CAP < window, f"{kind}: шаг {STEP_CAP} не помещается в окно {window}"


async def test_the_explicit_dedup_key_matches_the_one_the_centre_picks_itself(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """Ключ склейки сторож теперь ПЕРЕДАЁТ явно — и он обязан совпасть с тем,
    который центр уведомлений выбрал бы сам.

    Зачем явный. Лестница ступеней спрашивает базу «сколько раз это уже
    показывали», и её запрос обязан находить ровно ту строку, которую обновит
    `notify`. Один ключ на оба обращения — единственный способ это гарантировать.

    Зачем эта проверка. Разъезд ключа не ломает ничего громко: строки просто
    перестают склеиваться со СТАРЫМИ, уже лежащими в боевой таблице, — а
    лестница начинает считать с нуля и напоминать заново. Выглядит здоровым.
    """
    from app.services import notifications as notify_svc
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=20, tag="key")
    await presence_connected(redis, owner.id, "conn-key")

    await job.check_awaiting(now=NOW)

    async with db_sessionmaker() as s:
        ours = (
            await s.execute(
                sa.select(Notification).where(Notification.kind == "conversation.awaiting_you")
            )
        ).scalar_one()
        # Тот же вид и та же сущность, но ключ выбирает центр — по каталогу.
        theirs = await notify_svc.notify(
            s,
            kind="conversation.awaiting_you",
            recipient_id=owner.id,
            body="проверка ключа",
            entity_type="conversation",
            entity_id=str(conv_id),
            now=NOW,
        )
    assert ours.dedup_key == theirs.notification.dedup_key
    assert theirs.created is False, "совпал ключ — значит центр склеил, а не завёл вторую строку"


async def test_the_wait_is_written_the_same_way_as_the_rest_of_the_interface(
    db_sessionmaker, make_avito_account, owner, job, redis
):
    """«454 мин» ПРОТИВ «3 ч 43 мин» НА СОСЕДНЕМ ЭКРАНЕ.

    Ожидание в уведомлениях печаталось голыми минутами, хотя весь остальной
    интерфейс говорит «3 ч 43 мин». Семичасовое ожидание в виде «454 мин»
    человек не читает — он его пересчитывает, и пересчитывает каждый раз.

    Формат берётся у той же функции, что печатает ожидание в системной записи
    ленты (`inbox.format_wait`). Своя вторая означала бы третий вид одного и
    того же числа на экране — ровно то, из-за чего эта функция и появилась.

    Проверка ломанием: верните `f"... {minutes} мин"` — падает первый ассерт.
    """
    from app.services.inbox import format_wait
    from app.ws.presence import presence_connected

    account = await make_avito_account()
    await _taken(db_sessionmaker, account, owner, waiting_minutes=223, tag="hours")
    await presence_connected(redis, owner.id, "conn-hours")

    await job.check_awaiting(now=NOW)

    async with db_sessionmaker() as s:
        note = (
            await s.execute(
                sa.select(Notification).where(Notification.kind == "conversation.awaiting_you")
            )
        ).scalar_one()
    assert "3 ч 43 мин" in (note.body or ""), f"часы, а не минуты: {note.body!r}"
    assert "223 мин" not in (note.body or "")
    assert format_wait(223 * 60) == "3 ч 43 мин", (
        "формат обязан быть общим с лентой, а не своим здесь"
    )


# --- очередь: диалог, который никто не взял (12 августа) ----------------------
#
# Дыра между двумя сторожами. `check_awaiting` по построению смотрит ВЗЯТЫЕ
# диалоги, а `conversation.unclaimed` из `inbox._escalate` срабатывает только на
# ОТКАЗ всех, кому диалог предложили. Диалог, на который просто никто не
# посмотрел, не покрывал никто — и на боевой системе 12 августа это выглядело так:
# два диалога в очереди 7 и 4 часа, девять непрочитанных, ноль уведомлений.


async def _queued(db_sessionmaker, account, *, waiting_minutes: float, tag: str) -> uuid.UUID:
    """Диалог, который предложен очереди и не взят никем."""
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"q-{tag}", name=f"Клиент {tag}")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"q-chat-{tag}",
            account_id=account.id,
            client_id=cl.id,
            status="new",
            offered_at=NOW - timedelta(minutes=waiting_minutes),
            awaiting_since=NOW - timedelta(minutes=waiting_minutes),
            last_message_at=NOW - timedelta(minutes=waiting_minutes),
        )
        s.add(conv)
        await s.commit()
        return conv.id


async def _unclaimed_notes(db_sessionmaker, conv_id) -> list[Notification]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.kind == "conversation.unclaimed",
                        Notification.entity_id == str(conv_id),
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_nobody_took_the_dialog_and_the_system_says_so(
    db_sessionmaker, make_avito_account, job
):
    """Главное свойство: молчания больше нет."""
    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=25, tag="alone")

    assert await job.check_queue(now=NOW) == 1

    notes = await _unclaimed_notes(db_sessionmaker, conv_id)
    assert len(notes) == 1
    assert notes[0].audience == "admin"
    assert notes[0].severity == "warning"
    body = notes[0].body or ""
    assert "Клиент alone" in body
    assert "25 мин" in body


async def test_fresh_dialog_is_not_worth_a_word(db_sessionmaker, make_avito_account, job):
    """Порог существует, чтобы уведомление не приходило на каждое сообщение."""
    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=3, tag="fresh")

    assert await job.check_queue(now=NOW) == 0
    assert await _unclaimed_notes(db_sessionmaker, conv_id) == []


async def test_taken_dialog_is_not_this_guards_business(
    db_sessionmaker, make_avito_account, owner, job
):
    """Взятый диалог ведёт первый сторож; здесь он дал бы вторую строку об одном."""
    account = await make_avito_account()
    conv_id = await _taken(db_sessionmaker, account, owner, waiting_minutes=90, tag="taken-q")

    assert await job.check_queue(now=NOW) == 0
    assert await _unclaimed_notes(db_sessionmaker, conv_id) == []


async def test_the_line_does_not_repeat_every_minute(db_sessionmaker, make_avito_account, job):
    """Ступени с удвоением: 10, 20, 40 минут — а не шестьдесят строк за час.

    Ровный такт научил бы ровно тому, чему учит всякий шум: пролистывать. Повтор
    обязан означать новое — «ждёт вдвое дольше»."""
    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=11, tag="ladder")

    assert await job.check_queue(now=NOW) == 1
    # минутой позже — то же ожидание, ступень не пройдена
    assert await job.check_queue(now=NOW + timedelta(minutes=1)) == 0
    # на двадцатой минуте ожидания — вторая ступень
    assert await job.check_queue(now=NOW + timedelta(minutes=10)) == 1

    notes = await _unclaimed_notes(db_sessionmaker, conv_id)
    assert len(notes) == 1, "одна строка с растущим счётчиком, а не три"
    assert notes[0].repeat_count >= 2


async def test_closed_dialog_is_left_alone(db_sessionmaker, make_avito_account, job):
    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=200, tag="closed-q")
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        row.status = "closed"
        await s.commit()

    assert await job.check_queue(now=NOW) == 0


def test_the_queue_guard_is_actually_scheduled():
    """Без регистрации сторож не запускается никогда, и ошибка бесшумна."""
    from app.scheduler.main import build_scheduler

    assert "queue_unclaimed" in {j.id for j in build_scheduler().get_jobs()}


# --- тревога гаснет, когда повод исчез --------------------------------------
#
# ⚠ ДО 14 АВГУСТА В КОДЕ НЕ БЫЛО НИ ОДНОГО МЕСТА, ГДЕ УВЕДОМЛЕНИЕ ГАСНЕТ ПО
# СМЕНЕ СОСТОЯНИЯ СУЩНОСТИ. Снять его мог только человек нажатием или чистка по
# сроку в 90 дней. Замер боя: «Диалог никто не принял» висит 57 минут при уже
# ЗАКРЫТОМ диалоге и пустой очереди — а в теле у него «Откройте „Входящие“ и
# возьмите его». Администратор открывает очередь, не находит там ничего и
# делает единственный доступный вывод: колокольчик врёт.


async def _unread_for(db_sessionmaker, user_id, conv_id) -> int:
    """Сколько строк про этот диалог этот человек ВИДИТ как непрочитанные.

    Считается ровно тем предикатом, что и счётчик колокольчика: у рассылки по
    роли непрочитанность живёт не в `read_at`, а в ОТСУТСТВИИ строки в
    `notification_reads` у каждого получателя. Проверяй мы `read_at`, тест
    прошёл бы и на половинчатой починке — той самой, что до сих пор живёт в
    `cli.dismiss-stale-alerts`.
    """
    from app.models import User
    from app.services import notifications as svc

    async with db_sessionmaker() as s:
        user = await s.get(User, user_id)
        rows = (
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.entity_id == str(conv_id),
                        svc.visibility_condition(user),
                        Notification.expires_at > NOW,
                    )
                )
            )
            .scalars()
            .all()
        )
        read = await svc.read_ids(s, user, [r.id for r in rows])
        return len([r for r in rows if r.id not in read])


async def test_taking_the_dialog_puts_out_the_alarm_about_it(
    db_sessionmaker, make_avito_account, make_user, job, redis
):
    """Диалог приняли — «его никто не принял» перестало быть правдой."""
    from app.models import User
    from app.services import inbox

    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=25, tag="resolve-claim")
    admin = await make_user("resolve-admin@leadchat.test", role="admin")
    operator = await make_user("resolve-op@leadchat.test", role="manager")

    assert await job.check_queue(now=NOW) == 1
    assert await _unread_for(db_sessionmaker, admin.id, conv_id) == 1, "тревога зажглась"

    async with db_sessionmaker() as s:
        who = await s.get(User, operator.id)
        await inbox.claim(s, conv_id, who)
        await s.commit()

    assert await _unread_for(db_sessionmaker, admin.id, conv_id) == 0

    # ⚠ ИЗ ЖУРНАЛА СТРОКА НЕ ИСЧЕЗАЕТ. «Диалог висел непринятым двадцать пять
    # минут» — факт о работе, и он обязан пережить приём диалога. Ровно поэтому
    # гасим прочтением, а не сроком жизни: `expires_at` выкинул бы строку и из
    # журнала тоже, потому что выборка журнала стоит на том же предикате.
    assert len(await _unclaimed_notes(db_sessionmaker, conv_id)) == 1


async def test_closing_the_dialog_puts_out_the_alarm_too(
    db_sessionmaker, make_avito_account, make_user, job
):
    """Закрытие — второй способ сделать тревогу неправдой.

    Это и был случай с боя: диалог закрыт, очередь пуста, а строка висит.
    """
    from app.models import Conversation as Conv
    from app.models import User
    from app.services import conversations as convs

    account = await make_avito_account()
    conv_id = await _queued(db_sessionmaker, account, waiting_minutes=200, tag="resolve-close")
    admin = await make_user("resolve-admin2@leadchat.test", role="admin")

    assert await job.check_queue(now=NOW) == 1
    assert await _unread_for(db_sessionmaker, admin.id, conv_id) == 1

    async with db_sessionmaker() as s:
        conv = await s.get(Conv, conv_id)
        actor = await s.get(User, admin.id)
        await convs.change_status(s, conv, new_status="closed", actor=actor)
        await s.commit()

    assert await _unread_for(db_sessionmaker, admin.id, conv_id) == 0


async def test_alarms_of_other_dialogs_are_left_alone(
    db_sessionmaker, make_avito_account, make_user, job
):
    """Гасим ровно свой диалог.

    Случай не выдуманный: гасить по ВИДУ, а не по сущности, — первое, что
    приходит в голову, и оно молча стёрло бы очередь тревог по всем остальным
    диалогам при первом же принятом.
    """
    from app.models import User
    from app.services import inbox

    account = await make_avito_account()
    mine = await _queued(db_sessionmaker, account, waiting_minutes=25, tag="resolve-mine")
    other = await _queued(db_sessionmaker, account, waiting_minutes=30, tag="resolve-other")
    admin = await make_user("resolve-admin3@leadchat.test", role="admin")
    operator = await make_user("resolve-op3@leadchat.test", role="manager")

    assert await job.check_queue(now=NOW) == 2

    async with db_sessionmaker() as s:
        who = await s.get(User, operator.id)
        await inbox.claim(s, mine, who)
        await s.commit()

    assert await _unread_for(db_sessionmaker, admin.id, mine) == 0
    assert await _unread_for(db_sessionmaker, admin.id, other) == 1, "чужая тревога цела"
