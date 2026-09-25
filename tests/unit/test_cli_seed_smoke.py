"""CLI ``seed-smoke`` (07 §6): служебные сущности регрессионного smoke.

Команда гоняется на КАЖДОМ деплое, поэтому проверяем ровно два свойства:
она идемпотентна и не создаёт ничего, что могло бы уехать в реальный Авито.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from app import cli
from app.core.config import is_service_email, settings
from app.core.rbac import ROLE_PERMISSIONS
from app.core.security import is_password_set, verify_password
from app.models import AvitoAccount, Conversation, User

SMOKE_PASSWORD = "smoke-password-123"
SMOKE_ADMIN_PASSWORD = "smoke-admin-password-123"


@pytest.fixture
def seed(db_sessionmaker, monkeypatch):
    """Прогон команды на тестовой БД: подменяем только фабрику сессий."""

    async def _seed(
        *,
        password: str | None = SMOKE_PASSWORD,
        admin_password: str | None = SMOKE_ADMIN_PASSWORD,
    ) -> None:
        async with db_sessionmaker() as db:
            user, _ = await cli._seed_smoke_user(db, settings.smoke_user_email, password)
            await cli._seed_smoke_admin(db, settings.smoke_admin_email, admin_password)
            account, _ = await cli._seed_smoke_account(db, None)
            await cli._seed_smoke_conversation(db, account)
            await db.commit()

    return _seed


async def _counts(db_sessionmaker) -> tuple[int, int, int]:
    async with db_sessionmaker() as session:
        users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        accounts = (
            await session.execute(select(func.count()).select_from(AvitoAccount))
        ).scalar_one()
        convs = (await session.execute(select(func.count()).select_from(Conversation))).scalar_one()
        return users, accounts, convs


async def _user(db_sessionmaker, email: str) -> User:
    async with db_sessionmaker() as session:
        return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def test_seed_is_idempotent(db_sessionmaker, seed):
    await seed()
    first = await _counts(db_sessionmaker)
    await seed()
    await seed()
    assert await _counts(db_sessionmaker) == first == (2, 1, 1)


async def test_seed_user_shape(db_sessionmaker, seed):
    await seed()
    user = await _user(db_sessionmaker, settings.smoke_user_email)
    assert user.role == "manager"  # SM-3/SM-5 работают правами менеджера
    assert user.is_active is True
    assert is_service_email(user.email)  # скрыт из UI-списков по домену .local
    assert verify_password(user.password_hash, SMOKE_PASSWORD)


async def test_sm10_user_has_exactly_the_right_it_needs(db_sessionmaker, seed):
    """Роль второй учётки привязана к праву, ради которого она заведена.

    Проверка SM-10 читает список каналов Авито, а это `accounts:read`. Пароль
    учётки лежит в `.env` сервера, поэтому роль взята МЕНЬШАЯ из двух, где это
    право есть: `head`, а не `admin`. Здесь это и закреплено — понизят роль,
    и красным станет тест, а не выкатка на проде.
    """
    await seed()
    admin = await _user(db_sessionmaker, settings.smoke_admin_email)
    assert admin.role == "head"
    assert admin.is_active is True
    assert verify_password(admin.password_hash, SMOKE_ADMIN_PASSWORD)

    права = ROLE_PERMISSIONS[admin.role]
    assert "accounts:read" in права  # ради этого учётка и заведена
    assert "messages:send" not in права  # клиенту она не напишет ничего


async def test_two_service_accounts_never_share_an_address(db_sessionmaker, seed):
    """Роли у роботов разные, значит общий адрес означал бы, что seed сам себя
    перетирает: после каждого прогона ломалась бы то SM-3, то SM-10."""
    assert settings.smoke_user_email != settings.smoke_admin_email


async def test_seed_without_password_leaves_login_disabled(db_sessionmaker, seed):
    await seed(password=None, admin_password=None)
    for email in (settings.smoke_user_email, settings.smoke_admin_email):
        user = await _user(db_sessionmaker, email)
        assert is_password_set(user.password_hash) is False


async def test_stub_account_can_never_talk_to_avito(db_sessionmaker, seed):
    """Аккаунт-заглушка всегда disabled — воркер такие события не обрабатывает."""
    await seed()
    async with db_sessionmaker() as session:
        account = (await session.execute(select(AvitoAccount))).scalar_one()
        account.status = "active"  # кто-то «починил» заглушку руками
        await session.commit()

    await seed()  # повторный прогон возвращает контрактное состояние
    async with db_sessionmaker() as session:
        account = (await session.execute(select(AvitoAccount))).scalar_one()
    assert account.status == "disabled"
    assert account.webhook_secret  # SM-8 шлёт вебхук именно с ним


async def test_smoke_conversation_is_detached_from_real_accounts(db_sessionmaker, seed):
    await seed()
    async with db_sessionmaker() as session:
        conv = (await session.execute(select(Conversation))).scalar_one()
        account = (await session.execute(select(AvitoAccount))).scalar_one()
    assert conv.external_chat_id == settings.smoke_conversation_external_id
    assert conv.account_id == account.id  # заглушка, а не боевой аккаунт
    assert account.avito_user_id == settings.smoke_avito_user_id


async def test_existing_user_is_returned_to_contract_state(db_sessionmaker, seed, make_user):
    """Кто-то понизил робота в observer / отключил — seed чинит.

    Второго робота это касается ровно так же и по более дорогой причине: с
    ролью `manager` он логинится, но список каналов ему отвечает 403, и SM-10
    проверяет не отказ токенов, а собственные права.
    """
    await make_user(settings.smoke_user_email, role="observer", is_active=False)
    await make_user(settings.smoke_admin_email, role="manager", is_active=False)
    await seed()

    user = await _user(db_sessionmaker, settings.smoke_user_email)
    admin = await _user(db_sessionmaker, settings.smoke_admin_email)
    assert (user.role, user.is_active) == ("manager", True)
    assert (admin.role, admin.is_active) == ("head", True)
    assert isinstance(user.id, uuid.UUID)


async def test_seed_marks_its_own_entities_as_service(db_sessionmaker, seed):
    """Признак «служебное» ставит тот, кто запись создаёт, — здесь и нигде больше.

    До этого служебность УГАДЫВАЛИ по адресу (`.local`), и на боевой базе
    правило било мимо в обе стороны: аккаунт-заглушку не прятало вовсе (её
    диалог стоял строкой в «Разборе диалогов»), а живых администраторов на
    внутреннем домене `@leadpartner.local` прятало из списка сотрудников.
    """
    await seed()
    async with db_sessionmaker() as session:
        users = (await session.execute(select(User))).scalars().all()
        account = (await session.execute(select(AvitoAccount))).scalar_one()

    assert [u.is_service for u in users] == [True, True]
    assert account.is_service is True


async def test_seed_restores_the_service_flag_if_someone_cleared_it(db_sessionmaker, seed):
    """Идемпотентность распространяется и на признак: снятый — возвращается.

    Снять его может миграция без заполнения, ручной UPDATE или восстановление
    базы из копии, снятой до этой правки. Прогон seed-smoke идёт на каждом
    деплое и обязан возвращать служебные записи в контрактное состояние
    целиком, а не частично.
    """
    await seed()
    async with db_sessionmaker() as session:
        await session.execute(sa.update(User).values(is_service=False))
        await session.execute(sa.update(AvitoAccount).values(is_service=False))
        await session.commit()

    await seed()
    async with db_sessionmaker() as session:
        users = (await session.execute(select(User))).scalars().all()
        account = (await session.execute(select(AvitoAccount))).scalar_one()

    assert [u.is_service for u in users] == [True, True]
    assert account.is_service is True
