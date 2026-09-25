"""Разделение «оператор / права» (план 7.4).

До этого признака роль означала сразу две вещи: что человеку позволено и
грузить ли его диалогами. У заказчика в Jivo так настроен целый отдел
«СТАРШИЕ - ЧАТЫ» — администрируют систему, обращения не ведут, — и завести
такого сотрудника было невозможно.

Проверяется здесь не «сохраняется ли галочка». Проверяется, что признак
действует ВО ВСЕХ ЧЕТЫРЁХ выборках, где система решает, кому дать диалог.
Их четыре, и раньше каждая держала свою копию условия; забыть признак в
одной — значит получить дефект, который никак себя не проявит, кроме как
диалогом, уехавшим не туда.
"""

import pathlib

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.core.security import create_access_token
from app.models import AccountOperator, Client, Conversation, User
from app.services import account_operators as acc_ops
from app.services import conversations as convs
from app.services import distribution, inbox
from app.ws.presence import _status_key

pytestmark = pytest.mark.anyio


@pytest.fixture
async def senior(db_sessionmaker, make_user):
    """«СТАРШИЕ - ЧАТЫ»: администратор, который диалоги не ведёт."""
    user = await make_user("senior@leadchat.test", role="admin", full_name="Старший Чатов")
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(User)
            .where(User.id == user.id)
            .values(handles_conversations=False, department="СТАРШИЕ - ЧАТЫ")
        )
        await s.commit()
    return user


@pytest.fixture
async def conv(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id="pool-1", name="Клиент")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="pool-chat-1",
            account_id=account.id,
            client_id=client.id,
            status="new",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def test_by_default_everyone_who_can_answer_still_works(db_sessionmaker, make_user):
    """До 7.4 диалоги вели все, кто умеет отвечать, — и миграция это сохраняет.

    Поставь мы `false` по умолчанию — наутро после выкатки очередь перестала
    бы раздаваться никому, молча: ошибки нет, исключения нет, обращения просто
    висят.
    """
    operator = await make_user("plain@leadchat.test", role="manager")
    async with db_sessionmaker() as s:
        row = await s.get(User, operator.id)
        assert row is not None
        assert row.handles_conversations is True


async def test_the_senior_is_out_of_the_transfer_list(client, tokens, senior):
    """Список «кому передать» его не показывает.

    Иначе руководитель отдал бы диалог руками, а система тут же вернула бы
    его в очередь как непринятый — человек не работает с обращениями.
    """
    r = await client.get(
        "/api/v1/users/assignable", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert r.status_code == 200, r.text
    assert str(senior.id) not in {u["id"] for u in r.json()["items"]}


async def test_the_senior_never_gets_an_auto_assigned_dialog(db_sessionmaker, redis, senior, conv):
    """Автораздача его не выбирает, даже когда он единственный в сети."""
    await redis.set(_status_key(senior.id), "online")
    from app.services import app_settings

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
        await s.commit()

    async with db_sessionmaker() as s:
        picked = await distribution.pick_assignee(s, redis, await s.get(Conversation, conv.id))
    assert picked.assignee is None, "диалог не должен уходить тому, кто их не ведёт"


async def test_the_senior_is_not_counted_as_a_channel_operator(db_sessionmaker, senior, conv):
    """Канал, где назначен только он, считается каналом БЕЗ операторов.

    Это важнее, чем кажется: правило 7.2 гласит, что канал без назначенных
    доступен всем. Считай мы «старшего» оператором — канал выглядел бы
    закрытым за человеком, который в него не заглядывает, и обращения не
    увидел бы никто.
    """
    async with db_sessionmaker() as s:
        s.add(AccountOperator(account_id=conv.account_id, user_id=senior.id))
        await s.commit()

    async with db_sessionmaker() as s:
        ids = await acc_ops.operator_ids_for_account(s, conv.account_id)
    assert str(senior.id) not in ids


async def test_the_senior_is_not_expected_to_decline(db_sessionmaker, senior, conv):
    """Эскалация «отказались все» его не ждёт.

    Иначе диалог висел бы в очереди вечно: система ждала бы решения от
    человека, который эту очередь не открывает.
    """
    async with db_sessionmaker() as s:
        eligible = await inbox.eligible_operator_ids(s, await s.get(Conversation, conv.id))
    assert str(senior.id) not in eligible


class TestTheSeniorCannotTakeFromTheQueue:
    """ПЯТАЯ ТОЧКА, о которой 7.4 забыл: кнопка «Принять» во «Входящих».

    Признак проверяли четыре ВЫБОРКИ («кому система вправе дать диалог»), а
    принятие — ни одна: `inbox._assert_can_take` смотрел только на роль. На
    боевой системе это и случилось: у «Администратора Lead Partner» галка
    «Ведёт диалоги» снята, а в 14:27 он взял из очереди четыре диалога
    (`conversation.assigned`, `by: self`, `source: inbox`). Дальше система
    противоречила сама себе — списка назначаемых он не попадал, и карточка
    диалога писала «Этого сотрудника больше нет среди назначаемых» про
    человека с четырьмя диалогами на руках.
    """

    @staticmethod
    async def _queued(db_sessionmaker, conversation_id):
        async with db_sessionmaker() as s:
            conv = await s.get(Conversation, conversation_id)
            inbox.enter_queue(conv)
            await s.commit()

    async def test_claim_is_refused(self, db_sessionmaker, senior, conv):
        """Очередь — это раздача, просто ручная. Кто не в раздаче, тот не берёт."""
        await self._queued(db_sessionmaker, conv.id)
        async with db_sessionmaker() as s:
            user = await s.get(User, senior.id)
            with pytest.raises(ApiError) as err:
                await inbox.claim(s, conv.id, user)
        assert err.value.status == 403
        assert err.value.details["reason"] == "does_not_handle_conversations"

        # И диалог остался в очереди — то есть отказ произошёл ДО захвата.
        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv.id)
            assert inbox.is_waiting(row), "диалог ушёл из очереди, хотя принятие отбито"

    async def test_the_api_refuses_it_too(self, client, db_sessionmaker, senior, conv):
        """Ручка отвечает тем же 403: сервис зовут не только из неё, но и она
        обязана отбивать — id диалога виден в ссылке и в кадре `inbox:new`."""
        await self._queued(db_sessionmaker, conv.id)
        token = create_access_token(user_id=str(senior.id), role=senior.role)
        r = await client.post(
            f"/api/v1/conversations/{conv.id}/claim",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["details"]["reason"] == "does_not_handle_conversations"

    async def test_an_ordinary_operator_still_takes_it(self, db_sessionmaker, users_by_role, conv):
        """Охранник от чрезмерного запрета: обычный менеджер берёт как раньше.

        Без этой строки правка «запретить всем» выглядела бы такой же зелёной,
        как правка «запретить тем, кто не ведёт диалоги», — а разница между
        ними в том, работает очередь или нет.
        """
        await self._queued(db_sessionmaker, conv.id)
        async with db_sessionmaker() as s:
            user = await s.get(User, users_by_role["manager"].id)
            result = await inbox.claim(s, conv.id, user)
            await s.commit()
        assert result.conversation.assignee_id == users_by_role["manager"].id

    async def test_he_can_still_give_back_what_he_already_holds(
        self, db_sessionmaker, senior, conv
    ):
        """Возврат в очередь ему ОСТАВЛЕН намеренно.

        У человека на боевой системе уже четыре взятых диалога. Запрети мы и
        `release` — их было бы не отдать назад никаким способом, кроме похода
        к администратору: правка заперла бы диалоги у того, кто их не ведёт.
        """
        async with db_sessionmaker() as s:
            row = await s.get(Conversation, conv.id)
            row.assignee_id = senior.id
            row.claimed_by_id = senior.id
            row.status = "in_progress"
            await s.commit()

        async with db_sessionmaker() as s:
            user = await s.get(User, senior.id)
            result = await inbox.release(s, conv.id, user)
            await s.commit()
        assert result.conversation.assignee_id is None
        assert inbox.is_waiting(result.conversation)


async def test_the_right_to_answer_is_not_taken_away(senior):
    """Право отвечать остаётся: роль его даёт, признак — не отнимает.

    Разница существенная. Администратор, вмешавшийся в конкретный диалог,
    должен уметь написать клиенту — просто система не назначает ему новые
    сама. Запрети мы и это, «старший» не смог бы разобрать затор, ради
    которого его и позвали.
    """
    assert convs.can_answer_clients(senior.role) is True


async def test_toggling_it_is_never_anonymous(client, tokens, users_by_role, db_sessionmaker):
    """Вывод человека из работы с диалогами меняет состав смены для всех —
    он исчезает из очереди, из автораздачи и из списка «кому передать».
    Такое не должно происходить без следа."""
    from app.models import AuditLog

    target = users_by_role["manager"]
    r = await client.patch(
        f"/api/v1/users/{target.id}",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
        json={"handles_conversations": False, "department": "ОКК"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["handles_conversations"] is False
    assert r.json()["user"]["department"] == "ОКК"

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "user.conversations_toggled")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].entity_id == str(target.id)


def test_the_frontend_knows_the_same_answering_roles():
    """Список «кто умеет отвечать» продублирован во фронте — сторожим разъезд.

    Дубль неизбежен: интерфейсу нужно решить, показывать ли живой тумблер
    «ведёт диалоги» в каждой строке таблицы, а спрашивать об этом сервер ради
    каждой строки — лишний запрос. Но разъехавшись, дубль соврёт молча:
    тумблер окажется активным у роли, которой диалоги не назначают вовсе, и
    человек будет щёлкать его, не понимая, почему ничего не меняется.
    """
    import re

    ts = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend/src/features/settings/team/roles.ts"
    ).read_text(encoding="utf-8")
    m = re.search(r"export function canAnswer\(role: Role\): boolean \{(.*?)\}", ts, re.S)
    assert m, "во фронте пропала функция canAnswer"
    front = set(re.findall(r'role === "(\w+)"', m.group(1)))
    assert front == set(convs.ASSIGNABLE_ROLES), "списки отвечающих ролей разъехались"


async def test_an_empty_department_clears_it(client, tokens, users_by_role):
    """Снять отдел человек должен уметь так же, как поставить."""
    target = users_by_role["manager"]
    headers = {"Authorization": f"Bearer {tokens['admin']}"}
    await client.patch(f"/api/v1/users/{target.id}", headers=headers, json={"department": "Дисп 3"})
    r = await client.patch(f"/api/v1/users/{target.id}", headers=headers, json={"department": ""})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["department"] is None


# --- служебный робот: колонка, а не домен почты (15 августа) ------------------
#
# ⚠ РОБОТ БЫЛ В ПУЛЕ, И ЭТОГО НЕ ВИДЕЛ НИКТО. Smoke-пользователь — manager,
# активен, ведёт диалоги, то есть проходил ВСЕ условия `operator_pool_conditions`.
# Из списка «кому передать» его убирал фильтр по домену почты В САМОЙ РУЧКЕ — а
# автораздачу, операторов канала и эскалацию не прикрывало ничто: включи
# владелец автораздачу, и клиентский диалог мог уехать роботу. Заодно домен
# прятал и настоящих людей: `.local` — обычный внутренний домен, и действующие
# администраторы на нём просто исчезали из списков.


async def test_the_robot_is_out_of_the_pool_itself_not_just_the_list(
    db_sessionmaker, redis, make_user, make_avito_account
):
    """Автораздача не выбирает робота, даже когда он единственный кандидат.

    Проверяется ПУЛ, а не ручка: список «кому передать» робота и раньше не
    показывал, но раздача ходит мимо ручки — прямо по условиям пула.
    """
    from app.services import app_settings, distribution
    from app.ws.presence import _status_key

    # Раздача включается по-настоящему, через базу: с выключенной тест зеленел
    # бы по чужой причине (`distribution_off`) и пул не проверял бы вовсе.
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.DISTRIBUTION_ENABLED: True}, user_id=None)
        await s.commit()

    robot = await make_user("smoke2@leadchat.test", role="manager", is_service=True)
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client_row = Client(channel="avito", external_id="pool-svc", name="Клиент")
        s.add(client_row)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="pool-svc-chat",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)

        # Робот «в сети» — присутствие ему не мешает быть выбранным.
        await redis.set(_status_key(robot.id), "online")

        picked = await distribution.pick_assignee(s, redis, conv)
        assert picked.assignee is None, "клиентский диалог уехал служебному роботу"
        assert picked.reason == "no_available_operator"


async def test_a_real_person_on_a_local_domain_is_visible(client, tokens, make_user):
    """Настоящий человек с почтой на `.local` виден в списке «кому передать».

    Ровно эти люди и пропадали: домен `.local` — обычный внутренний, и замер
    12 августа нашёл на нём двух действующих администраторов, которых списки
    не показывали вовсе. Служебность обязана быть КОЛОНКОЙ, которую ставит
    тот, кто запись создал, — а не догадкой по строке адреса.
    """
    human = await make_user("dispatcher@leadpartner.local", role="manager")
    r = await client.get(
        "/api/v1/users/assignable", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert r.status_code == 200, r.text
    assert str(human.id) in {u["id"] for u in r.json()["items"]}


async def test_the_marked_robot_is_out_of_the_transfer_list_too(client, tokens, make_user):
    """А помеченный колонкой робот — не виден, на каком бы домене ни жил."""
    robot = await make_user("bot@leadchat.test", role="manager", is_service=True)
    r = await client.get(
        "/api/v1/users/assignable", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert r.status_code == 200, r.text
    assert str(robot.id) not in {u["id"] for u in r.json()["items"]}
