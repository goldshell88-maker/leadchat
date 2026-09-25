"""CLI ``set-role`` — аварийное восстановление доступа.

Инструмент этого рода зовут ровно один раз и в худший момент: когда войти в
управление уже нельзя. Поэтому проверяется не «меняется ли роль», а что
команда не подведёт именно тогда — не промолчит на опечатке в адресе, не
съест неизвестную роль и не сменит роль тихо, без следа в журнале.
"""

import pytest
import sqlalchemy as sa
import typer

from app import cli
from app.models import AuditLog, User

pytestmark = pytest.mark.anyio


@pytest.fixture
def run(db_sessionmaker):
    """Вызов НАСТОЯЩЕЙ логики команды на тестовой базе.

    Зовётся `cli.apply_role` — та самая функция, которую исполняет команда, а
    не её копия в тесте. Обёртку `set-role` покрывает отдельная проверка
    регистрации: всё, что она делает, — открывает соединение и передаёт
    управление сюда.
    """

    async def _invoke(email: str, role: str) -> None:
        async with db_sessionmaker() as db:
            await cli.apply_role(db, email=email, role=role)

    return _invoke


async def test_the_locked_out_owner_gets_his_role_back(db_sessionmaker, make_user, run):
    """Ровно тот случай, ради которого команда написана.

    Владелец снял роль администратора сам с себя и потерял доступ к управлению
    сотрудниками: вернуть её через интерфейс нельзя — для этого нужно право,
    которое он только что отдал.
    """
    owner = await make_user("owner@leadchat.test", role="manager")

    await run("owner@leadchat.test", "admin")

    async with db_sessionmaker() as s:
        assert (await s.get(User, owner.id)).role == "admin"


async def test_the_address_is_matched_case_insensitively(db_sessionmaker, make_user, run):
    """Адрес в консоли наберут как придётся — «Ivan@» и «ivan@» один человек.

    Промах здесь означал бы «сотрудник не найден» у того, кто в базе есть, и
    поиск причины в самый неподходящий момент.
    """
    user = await make_user("ivan@leadchat.test", role="observer")

    await run("IVAN@LeadChat.TEST", "admin")

    async with db_sessionmaker() as s:
        assert (await s.get(User, user.id)).role == "admin"


async def test_an_unknown_address_fails_loudly(run):
    """Опечатка в адресе — отказ с кодом, а не молчаливое «готово».

    Тихий успех здесь хуже всего: человек уходит уверенным, что доступ
    восстановлен, и обнаруживает обратное на следующем входе.
    """
    with pytest.raises(typer.Exit) as err:
        await run("nobody@leadchat.test", "admin")
    assert err.value.exit_code == 1


async def test_an_unknown_role_is_refused(db_sessionmaker, make_user, run):
    """Роль проверяется по реестру, а не пишется в базу как есть.

    Строка «administrator» вместо «admin» создала бы пользователя с ролью, у
    которой нет ни одного права, — то есть заперла бы человека ещё надёжнее.
    """
    user = await make_user("typo@leadchat.test", role="manager")

    with pytest.raises(typer.Exit) as err:
        await run("typo@leadchat.test", "administrator")
    assert err.value.exit_code == 2

    async with db_sessionmaker() as s:
        assert (await s.get(User, user.id)).role == "manager", "роль не тронута"


async def test_the_change_leaves_a_trace(db_sessionmaker, make_user, run):
    """Смена роли из консоли не должна быть незаметнее смены из интерфейса."""
    await make_user("traced@leadchat.test", role="manager")

    await run("traced@leadchat.test", "admin")

    async with db_sessionmaker() as s:
        rows = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "user.role_changed")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details == {"from": "manager", "to": "admin", "by": "cli"}
    # Действие выполнено на сервере, а не сотрудником через приложение.
    assert rows[0].user_id is None


async def test_setting_the_same_role_changes_nothing(db_sessionmaker, make_user, run):
    """Повтор не засоряет журнал: строка «сменил роль с admin на admin» только
    мешает искать настоящую."""
    await make_user("same@leadchat.test", role="admin")

    await run("same@leadchat.test", "admin")

    async with db_sessionmaker() as s:
        count = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "user.role_changed")
            )
        ).scalar_one()
    assert count == 0


def test_the_command_is_registered():
    """Команда должна быть видна в `--help`: аварийный инструмент, о котором
    нельзя узнать, не существует."""
    names = {c.name for c in cli.app.registered_commands}
    assert "set-role" in names
