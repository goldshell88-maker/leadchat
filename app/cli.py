"""Service CLI (08 §7): ``python -m app.cli ...``.

Sprint 1 commands:
- ``create-admin --email EMAIL [--full-name NAME] [--password PASS]`` —
  creates the first admin. Without ``--password`` it prints a one-time
  invite URL (the documented flow — the admin sets the password in the
  browser); ``--password`` sets the hash directly (dev bootstrap).
  If the email is taken, the invite is reissued (access recovery).
- ``invite --email EMAIL --role ROLE [--full-name NAME]`` — invites an
  employee and prints the one-time link (01 §3.2/§3.3 semantics).
- ``set-role --email EMAIL --role ROLE`` — аварийное восстановление доступа.
  Нужна отдельно от ``create-admin``: та существующему сотруднику роль НЕ
  меняет, только перевыпускает приглашение, — то есть в ситуации «админ
  остался без прав админа» не помогает.

Sprint 4 commands:
- ``seed-smoke`` — служебные сущности регрессионного smoke (07 §6): два
  пользователя-робота (``manager`` для SM-2…SM-5 и ``head`` для SM-10),
  аккаунт-заглушка и диалог ``SMOKE-CONV``. Идемпотентно.
- ``sentry-test`` — контрольное исключение в Sentry (чек-лист 05 §8).
"""

import asyncio
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
import structlog
import typer
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.observability import init_sentry
from app.core.rbac import ROLES
from app.core.security import (
    hash_password,
    invite_url,
    is_password_set,
    issue_invite,
    make_unreachable_hash,
)
from app.db import session as db_mod
from app.models import (
    AuditLog,
    AvitoAccount,
    Client,
    ClientPhoneCandidate,
    Conversation,
    Message,
    Notification,
    User,
)
from app.scheduler.jobs.geo_repair import REQUEUE_MIN_AGE
from app.services import read_markers
from app.services.audit import write_audit
from app.services.crypto import encrypt_token

app = typer.Typer(help="Сервисные команды LeadChat (runbook 05 §8)")
log = structlog.get_logger("app.cli")
#: Умолчание `address-recheck --older-than-hours` — граница обхода (27 ч, окно
#: вопроса клиенту), одна на команду и планировщик.
REQUEUE_MIN_AGE_HOURS = REQUEUE_MIN_AGE.total_seconds() / 3600


def _run(main: Callable[[AsyncSession], Awaitable[None]]) -> None:
    configure_logging(component="cli")

    async def wrapper() -> None:
        db_mod.init_engine(component="cli")
        try:
            # session_scope гарантирует rollback незакрытой транзакции и
            # возврат соединения в пул (08 §1.2) — команда может упасть на
            # середине, «idle in transaction» после неё остаться не должно.
            async with db_mod.session_scope() as db:
                await main(db)
        finally:
            await redis_mod.close_client()
            await db_mod.dispose_engine()

    asyncio.run(wrapper())


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.lower())
    return (await db.execute(stmt)).scalar_one_or_none()


async def _print_invite(db: AsyncSession, user: User, role: str) -> None:
    redis = redis_mod.get_client()
    token, expires = await issue_invite(redis, str(user.id))
    await write_audit(
        db,
        user_id=None,
        action="user.invited",
        entity="user",
        entity_id=str(user.id),
        details={"role": role, "by": "cli"},
    )
    await db.commit()
    typer.echo(f"Invite URL (одноразовая, до {expires.isoformat()}):")
    typer.echo(invite_url(token))


@app.command("create-admin")
def create_admin(
    email: str = typer.Option(..., help="Email администратора"),
    full_name: str = typer.Option("Администратор", help="Отображаемое имя"),
    password: str | None = typer.Option(
        None,
        help="Установить пароль сразу (dev). Без флага печатается invite-ссылка.",
        min=10,
    ),
) -> None:
    """Create the first admin (or reissue their invite / reset password)."""

    async def main(db: AsyncSession) -> None:
        user = await _get_user_by_email(db, email)
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=email.lower(),
                password_hash=make_unreachable_hash(),
                full_name=full_name,
                role="admin",
                is_active=True,
            )
            db.add(user)
            await db.flush()
        if password is not None:
            user.password_hash = hash_password(password)
            await write_audit(
                db,
                user_id=None,
                action="user.invited",
                entity="user",
                entity_id=str(user.id),
                details={"role": "admin", "by": "cli", "password_set": True},
            )
            await db.commit()
            typer.echo(f"Admin ready: {user.email} (пароль установлен)")
        else:
            await _print_invite(db, user, "admin")

    _run(main)


@app.command("set-role")
def set_role(
    email: str = typer.Option(..., help="Email сотрудника"),
    role: str = typer.Option(..., help=f"Новая роль: {', '.join(ROLES)}"),
) -> None:
    """Сменить роль сотрудника — аварийное восстановление доступа.

    ЗАЧЕМ ОТДЕЛЬНАЯ КОМАНДА. `create-admin` существующему сотруднику роль НЕ
    меняет: находит его по адресу и лишь перевыпускает приглашение. То есть
    ровно в том случае, ради которого её стали бы звать, — «администратор
    остался без прав администратора» — она бесполезна, и остаётся править
    строку в базе руками.

    Случай не гипотетический: 7 августа 2026 владелец системы снял роль сам с
    себя и потерял доступ к управлению сотрудниками. Вернуть её через
    интерфейс он уже не мог — для этого нужно право, которое он только что
    отдал. Теперь такое снятие запрещено (`self_role_change`), но дверь
    восстановления всё равно обязана существовать: запрет закрывает один
    способ потерять доступ, а способов больше одного.

    Проверки RBAC здесь нет намеренно, и это не упущение. Команда выполняется
    на сервере тем, у кого уже есть доступ к базе, то есть полный контроль над
    системой в любом случае. Требовать от него ещё и права внутри приложения
    значило бы запереть аварийный выход изнутри.
    """

    _run(lambda db: apply_role(db, email=email, role=role))


async def apply_role(db: AsyncSession, *, email: str, role: str) -> None:
    """Тело команды ``set-role`` — отдельно от обёртки, открывающей соединение.

    Разделение то же, что у сторожа возврата (`jobs/reclaim.py`), и по той же
    причине: логика «нашли / отказали / записали» проверяется на обычной сессии
    теста. Слей её с обёрткой — и `asyncio.run` внутри сделает функцию
    невызываемой из тестов вовсе, а единственным «тестом» останется копия её
    тела рядом, которая разъедется с оригиналом на первой же правке.
    """
    if role not in ROLES:
        typer.echo(f"Неизвестная роль: {role}. Доступны: {', '.join(ROLES)}")
        raise typer.Exit(code=2)

    user = await _get_user_by_email(db, email)
    if user is None:
        typer.echo(f"Сотрудник с адресом {email} не найден")
        raise typer.Exit(code=1)

    old_role = user.role
    if old_role == role:
        typer.echo(f"{user.email}: роль уже «{role}», ничего не меняю")
        return

    user.role = role
    # Запись в журнал обязательна: смена роли из консоли не должна быть менее
    # заметной, чем смена из интерфейса. `user_id=None` — действие выполнено на
    # сервере, а не сотрудником через приложение.
    await write_audit(
        db,
        user_id=None,
        action="user.role_changed",
        entity="user",
        entity_id=str(user.id),
        details={"from": old_role, "to": role, "by": "cli"},
    )
    await db.commit()
    typer.echo(f"{user.email}: роль {old_role} → {role}")
    # Живые сессии переживают смену: право проверяется по строке БД на каждом
    # запросе, поэтому новая роль действует со следующего же вызова.
    typer.echo("Новая роль действует сразу — перезаходить не нужно.")


@app.command("invite")
def invite(
    email: str = typer.Option(..., help="Email сотрудника"),
    role: str = typer.Option("manager", help="Роль: admin | head | manager | observer"),
    full_name: str = typer.Option("Сотрудник", help="Отображаемое имя"),
) -> None:
    """Invite an employee and print the one-time link."""
    if role not in ROLES:
        typer.echo(f"Недопустимая роль: {role} (ожидается одна из {', '.join(ROLES)})")
        raise typer.Exit(code=2)

    async def main(db: AsyncSession) -> None:
        user = await _get_user_by_email(db, email)
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=email.lower(),
                password_hash=make_unreachable_hash(),
                full_name=full_name,
                role=role,
                is_active=True,
            )
            db.add(user)
            await db.flush()
        elif is_password_set(user.password_hash):
            # 01 §3.3: reissue only while the password is not set yet
            typer.echo(f"У пользователя {user.email} пароль уже установлен — invite не нужен")
            raise typer.Exit(code=1)
        await _print_invite(db, user, user.role)

    _run(main)


# --------------------------------------------------------- smoke (07 §6)


SMOKE_TOKEN_PLACEHOLDER = "smoke-account-has-no-avito-token"  # noqa: S105 — не секрет


async def _seed_smoke_user(db: AsyncSession, email: str, password: str | None) -> tuple[User, bool]:
    """Пользователь-робот smoke-набора: role=manager, активен, скрыт из UI.

    ЗДЕСЬ И СТАВИТСЯ ПРИЗНАК «СЛУЖЕБНОЕ» — в одном месте на всю систему.

    Раньше служебность выводили из адреса (``*.local``, config.is_service_email):
    отдельной колонки не было, а домен `.local` казался зарезервированным под
    нас. На боевой базе он оказался обычным внутренним доменом заказчика: на нём
    живут ДЕЙСТВУЮЩИЕ администраторы (`admin@leadpartner.local`,
    `dev-admin@leadchat.local`), и правило прятало из списка сотрудников живых
    людей. Признак теперь ставит тот, кто запись создал, а не угадывает тот, кто
    её читает.
    """
    user = await _get_user_by_email(db, email)
    created = user is None
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            password_hash=make_unreachable_hash(),
            full_name="SMOKE (служебный)",
            role="manager",
            is_active=True,
            is_service=True,
        )
        db.add(user)
        await db.flush()
    else:  # идемпотентность: возвращаем в контрактное состояние
        user.role = "manager"
        user.is_active = True
        user.is_service = True
    if password is not None:
        user.password_hash = hash_password(password)
    return user, created


async def _seed_smoke_admin(
    db: AsyncSession, email: str, password: str | None
) -> tuple[User, bool]:
    """Вторая служебная учётка: ею проверка SM-10 читает список каналов Авито.

    ПОЧЕМУ РОЛЬ `head`, ХОТЯ ПЕРЕМЕННЫЕ ЗОВУТСЯ SMOKE_ADMIN_*. Проверке нужно
    ровно одно право — `accounts:read` (01 §12): «ни один боевой канал не ушёл
    в needs_reauth». Оно есть и у `admin`, и у `head`, и берётся МЕНЬШЕЕ из
    двух. Пароль этой учётки лежит в `.env` сервера, то есть цена его утечки
    равна правам роли: `head` не отправит клиенту ни одного сообщения (01 §12,
    read_only_role), не заведёт человека и не тронет каналы, а `admin` сделал
    бы всё это. Имена переменных оставлены прежними — они уже прописаны в
    `deploy/smoke.sh`, в секретах CI и в чужих инструкциях.

    ПОЧЕМУ НЕ ХВАТАЕТ ПЕРВОЙ УЧЁТКИ. У робота-менеджера `accounts:read` нет, и
    список каналов ответил бы ему 403. До 23 августа `deploy/smoke.sh` читал
    такой ответ как «слова needs_reauth в нём нет», то есть красил SM-10
    зелёным, ничего не проверив.
    """
    user = await _get_user_by_email(db, email)
    created = user is None
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            password_hash=make_unreachable_hash(),
            full_name="SMOKE-ADMIN (служебный)",
            role="head",
            is_active=True,
            is_service=True,
        )
        db.add(user)
        await db.flush()
    else:  # идемпотентность: возвращаем в контрактное состояние
        user.role = "head"
        user.is_active = True
        user.is_service = True
    if password is not None:
        user.password_hash = hash_password(password)
    return user, created


async def _seed_smoke_account(
    db: AsyncSession, webhook_secret: str | None
) -> tuple[AvitoAccount, bool]:
    """Аккаунт-заглушка: status='disabled' — из него НИКОГДА не уходит запрос в Авито.

    Он нужен только затем, что ``conversations.account_id`` NOT NULL, и затем,
    что SM-8 шлёт вебхук с его секретом: запись попадает в стрим и умирает там,
    потому что аккаунт отключён (07 §6).
    """
    account = (
        await db.execute(
            select(AvitoAccount).where(AvitoAccount.avito_user_id == settings.smoke_avito_user_id)
        )
    ).scalar_one_or_none()
    created = account is None
    if account is None:
        account = AvitoAccount(
            id=uuid.uuid4(),
            title=settings.smoke_account_title,
            avito_user_id=settings.smoke_avito_user_id,
            access_token_enc=encrypt_token(SMOKE_TOKEN_PLACEHOLDER),
            refresh_token_enc=encrypt_token(SMOKE_TOKEN_PLACEHOLDER),
            token_expires_at=datetime.now(UTC) + timedelta(days=3650),
            status="disabled",
            webhook_secret=webhook_secret or secrets.token_urlsafe(24),
            # Отсюда служебность и наследует ДИАЛОГ `SMOKE-CONV`: своей отметки
            # у него нет и не нужно — он привязан к этому аккаунту, а отчёт
            # спрашивает про канал. Один признак вместо трёх согласованных.
            is_service=True,
        )
        db.add(account)
        await db.flush()
    else:
        account.status = "disabled"  # никто не «включит» заглушку случайно
        account.is_service = True
        if webhook_secret:
            account.webhook_secret = webhook_secret
    return account, created


async def _seed_smoke_conversation(
    db: AsyncSession, account: AvitoAccount
) -> tuple[Conversation, bool]:
    """Диалог SMOKE-CONV: в него пишутся только заметки (наружу не уходят)."""
    external_id = settings.smoke_conversation_external_id
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.channel == "avito",
                Conversation.external_chat_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if conv is not None:
        return conv, False

    client = (
        await db.execute(
            select(Client).where(Client.channel == "avito", Client.external_id == external_id)
        )
    ).scalar_one_or_none()
    if client is None:
        client = Client(channel="avito", external_id=external_id, name="SMOKE (служебный)")
        db.add(client)
        await db.flush()

    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=external_id,
        account_id=account.id,
        client_id=client.id,
        status="new",
        status_since=datetime.now(UTC),
        unread_count=0,
        last_message_at=datetime.now(UTC),
    )
    db.add(conv)
    await db.flush()
    return conv, True


@app.command("seed-smoke")
def seed_smoke(
    email: str | None = typer.Option(
        None, help="Адрес smoke-пользователя (по умолчанию SMOKE_USER_EMAIL)"
    ),
    password: str | None = typer.Option(
        None, help="Пароль smoke-пользователя (CI: секрет SMOKE_USER_PASSWORD)"
    ),
    admin_email: str | None = typer.Option(
        None, help="Адрес учётки для SM-10 (по умолчанию SMOKE_ADMIN_EMAIL)"
    ),
    admin_password: str | None = typer.Option(
        None, help="Пароль учётки для SM-10 (CI: секрет SMOKE_ADMIN_PASSWORD)"
    ),
    webhook_secret: str | None = typer.Option(
        None, help="Секрет вебхука аккаунта-заглушки (иначе будет сгенерирован)"
    ),
    show_secret: bool = typer.Option(False, help="Напечатать webhook-секрет заглушки"),
) -> None:
    """Служебные сущности регрессионного smoke (07 §6). Идемпотентно.

    Создаёт (или приводит к контрактному состоянию) двух пользователей-роботов
    (`manager` для SM-2…SM-5 и `head` для SM-10), аккаунт-заглушку `disabled` и
    диалог `SMOKE-CONV`. Ничего боевого не трогает и в реальный Авито не ходит.
    """
    target_email = (email or settings.smoke_user_email).lower()
    target_admin_email = (admin_email or settings.smoke_admin_email).lower()
    if target_email == target_admin_email:
        # Роли у них разные, и общий адрес означал бы, что каждый прогон
        # перетирает роль предыдущего: SM-3 и SM-10 ломались бы по очереди.
        typer.echo(f"Обе служебные учётки указывают на {target_email} — разведите адреса")
        raise typer.Exit(code=2)

    async def main(db: AsyncSession) -> None:
        user, user_created = await _seed_smoke_user(db, target_email, password)
        admin, admin_created = await _seed_smoke_admin(db, target_admin_email, admin_password)
        account, account_created = await _seed_smoke_account(db, webhook_secret)
        conv, conv_created = await _seed_smoke_conversation(db, account)
        await db.commit()
        # В audit_log не пишем сознательно: реестр действий (06 §0.3 +
        # services/audit.AUDIT_ACTIONS) — про действия людей в продукте, а не
        # про подготовку стенда. След остаётся в структурных логах деплоя.
        log.info(
            "cli.seed_smoke",
            user_created=user_created,
            admin_created=admin_created,
            account_created=account_created,
            conversation_created=conv_created,
        )

        typer.echo(f"smoke-пользователь: {user.email} ({'создан' if user_created else 'обновлён'})")
        if password is None and not is_password_set(user.password_hash):
            typer.echo("  ! пароль не задан — залогиниться нельзя. Повторите с --password")
        typer.echo(
            f"учётка SM-10: {admin.email} role=head ({'создана' if admin_created else 'обновлена'})"
        )
        if admin_password is None and not is_password_set(admin.password_hash):
            typer.echo("  ! пароль не задан — SM-10 проверять нечем. Повторите с --admin-password")
        typer.echo(
            f"аккаунт-заглушка: {account.title} / avito_user_id={account.avito_user_id} "
            f"({'создан' if account_created else 'обновлён'}, status=disabled)"
        )
        if show_secret or account_created:
            typer.echo(f"  webhook_secret: {account.webhook_secret}")
        typer.echo(
            f"служебный диалог: {settings.smoke_conversation_external_id} id={conv.id} "
            f"({'создан' if conv_created else 'уже был'})"
        )

    _run(main)


async def apply_repair_empty(db: AsyncSession, *, dry_run: bool = False) -> tuple[int, int]:
    """Логика ``repair-empty-messages``: подписать пустые + переименовать по сырцу.

    ⚠ ВТОРОЙ ПРОХОД ПОЯВИЛСЯ 15 АВГУСТА, и это не косметика. Замер боя: из 30
    «сообщений неподдерживаемого вида» 14 оказались `appCall` — клиент ЗВОНИЛ
    через приложение Авито, а лента предлагала «открыть диалог в Авито».
    Диспетчер не перезванивал, потому что не знал, что был звонок. Вид лежит в
    сохранённом сырце (`webhook_raw_log`) — задним числом узнать его МОЖНО, но
    ровно для тех строк, чей конверт сохранён. Историю без сырца не трогаем:
    выдуманный вид хуже честного «не знаем».

    Возвращает: (подписано пустых, переименовано по сырцу).
    """
    from app.integrations.avito.adapter import _CONTENT_FREE_KINDS, UNSUPPORTED_MESSAGE_TEXT

    rows = (
        (
            await db.execute(
                select(Message).where(
                    Message.body.is_(None),
                    Message.direction == "in",
                )
            )
        )
        .scalars()
        .all()
    )
    empty = [m for m in rows if not (m.attachments or [])]

    renamed = 0
    for kind, text in _CONTENT_FREE_KINDS.items():
        result = await db.execute(
            sa.text(
                """
                UPDATE messages SET body = :text
                WHERE body = :placeholder
                  AND EXISTS (
                    SELECT 1 FROM webhook_raw_log w
                    WHERE w.payload->'payload'->'value'->>'id'
                          = messages.external_message_id
                      AND w.payload->'payload'->'value'->>'type' = :kind
                  )
                """
            ),
            {"text": text, "placeholder": UNSUPPORTED_MESSAGE_TEXT, "kind": kind},
        )
        # `execute` объявлен как `Result[Any]`, но у DML это всегда
        # `CursorResult` — счётчик строк живёт только у него.
        renamed += cast(sa.CursorResult[Any], result).rowcount or 0

    if dry_run:
        await db.rollback()
        return len(empty), renamed

    for m in empty:
        m.body = UNSUPPORTED_MESSAGE_TEXT
    await db.commit()
    return len(empty), renamed


@app.command("repair-empty-messages")
def repair_empty_messages(
    dry_run: bool = typer.Option(False, help="Показать, сколько и каких, и выйти"),
) -> None:
    """Подписать сообщения, у которых нет ни текста, ни вложений.

    ОТКУДА ОНИ ВЗЯЛИСЬ. Разбор не знал нетекстовых видов Авито (фотография,
    голосовое, звонок, ссылка на объявление), и сообщение сохранялось пустым:
    ни тела, ни вложения, а сырец терялся. В ленте это показывалось как «…», а
    в списке чатов — как «Вложение»: два разных ответа об одном сообщении.
    Разбор починен 12 августа, но уже сохранённые строки об этом не знают.

    ЧТО ДЕЛАЕТ. Ставит честную подпись «Сообщение неподдерживаемого вида»
    вместо пустоты. Дотянуть настоящее содержимое ЗАДНИМ ЧИСЛОМ нельзя: сырец
    не сохранялся, а в истории Авито эти сообщения лежат под теми же
    неизвестными видами. Обещать «дозагрузим» было бы неправдой — вместо этого
    оператор увидит прямую подсказку открыть диалог в Авито.

    Новых пустых строк не появится: разбор теперь подписывает их сам.
    """

    async def main(db: AsyncSession) -> None:
        empty, renamed = await apply_repair_empty(db, dry_run=dry_run)
        typer.echo(f"сообщений без текста и без вложений: {empty}")
        typer.echo(f"переименовано по сырцу (звонки и подобное): {renamed}")
        if dry_run:
            typer.echo("сухой прогон: ничего не изменено")

    # ⚠ ЭТОЙ СТРОКИ ОДНАЖДЫ НЕ СТАЛО, И КОМАНДА ДВА ДНЯ БЫЛА ЗЕЛЁНОЙ ПУСТЫШКОЙ.
    # Рефакторинг 15 августа вынес логику в `apply_repair_empty` и вместе со
    # старым телом случайно снёс вызов: команда определяла работу и молча
    # выходила с кодом 0. Поймано владельцем на бою — вывода нет, значит нет и
    # работы. Сторож: tests/unit/test_cli_wrappers.py.
    _run(main)


@app.command("backfill-cross-account")
def backfill_cross_account(
    dry_run: bool = typer.Option(True, help="Показать, кого нашли, и выйти"),
) -> None:
    """Проставить признак межканального клиента тем, кто стал им ДО выкатки.

    ЗАЧЕМ. Требование владельца №6: «одни и тот же клиент пишет на разные
    аккаунты — не видно, что это он». Склейка по `author_id` работала с
    первого дня (у таблицы клиентов `UNIQUE(channel, external_id)` без
    аккаунта), а видимость появилась 11 августа — и ставится она ТОЛЬКО в
    момент создания нового диалога (`inbound._note_cross_account_link`).

    ЧТО ИЗ ЭТОГО ВЫШЛО. На боевой системе 12 августа нашёлся ровно один такой
    клиент — Пётр Сергеев, два диалога на каналах «Дамир» и «Тимофей». Оба
    диалога от 8 августа, то есть старше выкатки: признак ему не проставился и
    не проставится никогда, потому что третьего диалога может не быть. Оператор
    открывает карточку и не видит ничего — при том что случай ровно тот, ради
    которого всё и делалось. Догона не было; эта команда — он.

    ЧЕГО КОМАНДА НЕ ДЕЛАЕТ. Не склеивает никого заново: склейка уже произошла в
    базе, здесь только подписывается факт. Карточки с запасным ключом
    `chat:<id>` пропускаются — у них личность неизвестна, и подписывать нечем
    (разбор 12 августа: восемь посторонних людей однажды получили подпись
    «возможно, это тот же человек»).

    Мера доверия ставится «предположительно»: совпал ОДИН лишь идентификатор
    Авито. Подтвердить его может телефон — это делает обычный разбор входящих,
    когда клиент напишет снова.

    По умолчанию — сухой прогон.

    ЗАПУСКАТЬ НА СЕРВЕРЕ, А НЕ У СЕБЯ. Контейнеры живут на проде; с рабочей
    машины `docker exec` отвечает «No such container» — на эти грабли уже
    наступили 12 августа, потому что подсказка была написана без `ssh`:

        ssh leadchat 'docker exec leadchat-api-1 \
            python -m app.cli backfill-cross-account --no-dry-run'
    """

    async def main(db: AsyncSession) -> None:
        from app.models.client import LINK_ASSUMED

        rows = (
            await db.execute(
                sa.select(
                    Client.id,
                    Client.external_id,
                    Client.name,
                    sa.func.count(sa.distinct(Conversation.account_id)).label("каналов"),
                )
                .join(Conversation, Conversation.client_id == Client.id)
                .where(
                    Client.cross_account_since.is_(None),
                    ~Client.external_id.like("chat:%"),
                )
                .group_by(Client.id, Client.external_id, Client.name)
                .having(sa.func.count(sa.distinct(Conversation.account_id)) > 1)
            )
        ).all()

        if not rows:
            typer.echo("Межканальных клиентов без подписи нет — догонять нечего.")
            return

        typer.echo(f"Нашлось карточек: {len(rows)}")
        for row in rows:
            typer.echo(f"  {row.name or '—'} (author_id {row.external_id}), каналов: {row.каналов}")

        if dry_run:
            typer.echo("Сухой прогон: ничего не изменено. Повторите с --no-dry-run.")
            return

        сейчас = datetime.now(UTC)
        await db.execute(
            sa.update(Client)
            .where(Client.id.in_([r.id for r in rows]))
            .values(cross_account_since=сейчас, link_confidence=LINK_ASSUMED)
        )
        await db.commit()
        for row in rows:
            log.warning(
                "client.cross_account_linked",
                client_id=str(row.id),
                avito_author_id=row.external_id,
                link_confidence=LINK_ASSUMED,
                source="backfill",
            )
        typer.echo(f"Подписано: {len(rows)}. Мера доверия — «предположительно».")

    _run(main)


@app.command("scan-phones")
def scan_phones(
    limit: int = typer.Option(2000, help="Сколько последних входящих просмотреть"),
    dry_run: bool = typer.Option(True, help="Показать, что нашли, и выйти"),
) -> None:
    """Найти телефоны в УЖЕ НАКОПЛЕННОЙ переписке — списком на подтверждение.

    ЗАЧЕМ. Распознавание номеров в тексте появилось 12 августа и работает
    только на новых сообщениях. Вся прежняя переписка про него не знает: номер,
    написанный клиентом в июле, так и лежит в ленте, а карточка показывает
    кнопку «указать телефон». Догона не было; эта команда — он.

    ЧЕГО КОМАНДА НЕ ДЕЛАЕТ, И ЭТО ГЛАВНОЕ. Она НЕ ПИШЕТ НОМЕРА В КАРТОЧКИ. Ни
    одного, ни при каком сочетании флагов. Разбор ошибается на цифрах, которые
    телефоном не являются (код домофона, артикул, номер квартиры), а номер из
    карточки набирают и диктуют мастеру вслух. Разом переписать сотни карточек
    догадками — это сотни звонков посторонним людям, и заметить их можно будет
    только по жалобам. Команда формирует СПИСОК КАНДИДАТОВ: они появятся в
    карточках предложением «заменить / добавить / отклонить», и решит человек.

    Побочно (и сразу) чинится поиск: диалог начинает находиться по телефону,
    написанному в тексте, — и по «1112240», и по «+7 900 111-22-40».

    Повторный запуск безопасен: `client_phone_candidates` держит один номер на
    карточку, поэтому уже предложенное не задваивается, а ОТКЛОНЁННОЕ не
    возвращается — иначе люди перестали бы читать подсказки за неделю.

    По умолчанию — сухой прогон.

    ЗАПУСКАТЬ НА СЕРВЕРЕ, А НЕ У СЕБЯ. Контейнеры живут на проде; с рабочей
    машины `docker exec` отвечает «No such container» — на эти грабли уже
    наступали 12 августа:

        ssh leadchat 'docker exec leadchat-api-1 \
            python -m app.cli scan-phones --no-dry-run'
    """
    _run(lambda db: run_scan_phones(db, limit=limit, dry_run=dry_run))


async def run_scan_phones(db: AsyncSession, *, limit: int, dry_run: bool) -> None:
    """Тело команды ``scan-phones`` — отдельно от обёртки, открывающей соединение.

    Разделение то же, что у ``set-role``, и по той же причине: логика «нашли /
    показали / записали» проверяется на обычной сессии теста. Слей её с
    обёрткой — и `asyncio.run` внутри сделает функцию невызываемой из тестов
    вовсе, а единственным «тестом» осталась бы копия её тела рядом, которая
    разъедется с оригиналом на первой же правке.
    """
    from app.models.client import CANDIDATE_SOURCE_RESCAN
    from app.services import clients as clients_svc
    from app.services import phone_parse

    rows = (
        await db.execute(
            sa.select(
                Message.id,
                Message.body,
                Message.created_at,
                Conversation.id.label("conversation_id"),
                Conversation.client_id,
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                # ТОЛЬКО ВХОДЯЩИЕ ОТ КЛИЕНТА. Исходящие оператора отсекаются по
                # `direction`, служебные записи Авито — по нему же
                # (`direction='system'`), и оба отсечения не косметические: в
                # исходящих стоит НАШ собственный телефон, и записать его
                # клиенту значило бы позвонить самому себе.
                Message.direction == "in",
                Message.sender_type == "client",
                Message.body.is_not(None),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()

    # Разбор чистый и без базы, поэтому весь отбор делается в памяти одним
    # проходом: ни одного запроса на сообщение. Берём ПЕРВЫЙ номер сообщения —
    # тот, который человек назвал главным (см. `phone_parse.find_first`).
    found = [(row, hit) for row in rows for hit in phone_parse.find_all(row.body)[:1]]
    typer.echo(f"просмотрено сообщений: {len(rows)}, номеров распознано: {len(found)}")
    if not found:
        return

    typer.echo(f"карточек затронуто: {len({row.client_id for row, _ in found})}")
    for row, hit in found[:20]:
        typer.echo(f"  {hit.value}  ←  «{hit.raw}»  (диалог {row.conversation_id})")
    if len(found) > 20:
        typer.echo(f"  … и ещё {len(found) - 20}")

    if dry_run:
        typer.echo("Сухой прогон: ничего не изменено. Повторите с --no-dry-run.")
        return

    сейчас = datetime.now(UTC)
    добавлено = 0
    for row, hit in found:
        client = await db.get(Client, row.client_id)
        if client is None:  # карточку успели удалить между выборкой и записью
            continue
        добавлено += await clients_svc.record_phone_candidate(
            db,
            client=client,
            conversation_id=row.conversation_id,
            message_id=row.id,
            message_at=row.created_at,
            found=hit,
            source=CANDIDATE_SOURCE_RESCAN,
            now=сейчас,
        )
    await db.commit()
    typer.echo(
        f"добавлено предложений: {добавлено} (остальные уже были предложены или отклонены раньше)"
    )
    typer.echo("В карточки НИЧЕГО не записано — решает оператор.")


@app.command("backfill-phones")
def backfill_phones(
    limit: int = typer.Option(500, help="Сколько карточек заполнить за прогон"),
    dry_run: bool = typer.Option(True, help="Показать, что будет сделано, и выйти"),
) -> None:
    """Заполнить ПУСТЫЕ карточки телефонами из уже накопленной переписки.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «проанализируй все чаты и добавь везде номера,
    где есть номера и не привязаны, так как часто не привязывается».

    ⚠ ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ `scan-phones`, КОТОРАЯ РЯДОМ. Та формирует
    ПРЕДЛОЖЕНИЯ и в карточки не пишет ничего: «решает оператор». Эта пишет — но
    только в ПУСТУЮ карточку и только тем правилом, которое уже действует в бою.

    ⚠ ПОЧЕМУ ЭТО НЕ НОВАЯ ПОЛИТИКА, А ПРОИГРЫВАНИЕ СТАРОЙ. В настройках
    `phone_detect.autofill` включён (проверено на бою 02.09): распознанный номер
    УЖЕ заполняет пустую карточку сам, на каждом входящем. Правило выбрано
    владельцем, и здесь оно просто применяется к тем сообщениям, которые пришли
    до его появления. Ни одного собственного решения команда не принимает.

    ПРАВИЛО, СЛОВО В СЛОВО КАК В `inbound._maybe_extract_phone`:
      * только входящие сообщения САМОГО КЛИЕНТА. В исходящих стоит наш телефон
        или телефон мастера — записать его клиенту значит позвонить самому себе.
        Замер 02.09: у семи карточек номер написали только мы, и они пропущены;
        у трёх наш и клиентский номера РАЗНЫЕ;
      * карточка пуста. Непустую не трогаем никогда — ни своей записью, ни
        чужой правкой руками;
      * по времени, от старого к новому: карточку заполняет ПЕРВЫЙ названный
        номер, как и было бы в бою;
      * `record_phone_candidate` вернул «строка не новая» — значит решение по
        этому номеру уже принимал человек (принял, отклонил, добавил вторым).
        Автозапись не спорит с человеком и пропускает такую карточку.

    ЧТО БУДЕТ В ЖУРНАЛЕ. `client.phone_captured` с `source='regex'` и пустым
    `user_id` — ровно то, что пишет автозапись в бою. Это же означает всплеск в
    отчёте «собрано телефонов» (06 §1.4) за день прогона: числа настоящие, но
    собраны они не в этот день. Стоит сказать об этом тому, кто смотрит отчёт.

    ⚠ КАК ОТМЕНИТЬ, ЕСЛИ ЧТО-ТО ПОЙДЁТ НЕ ТАК. Все записи этого прогона видны в
    журнале аудита: `action='client.phone_captured' AND user_id IS NULL` за
    период прогона. По ним же и откатывается.

    По умолчанию — сухой прогон.

    ЗАПУСКАТЬ НА СЕРВЕРЕ, А НЕ У СЕБЯ:

        ssh leadchat 'docker exec leadchat-api-1 \
            python -m app.cli backfill-phones --no-dry-run'
    """
    _run(lambda db: run_backfill_phones(db, limit=limit, dry_run=dry_run))


async def run_backfill_phones(db: AsyncSession, *, limit: int, dry_run: bool) -> None:
    """Тело команды ``backfill-phones`` — отдельно от обёртки, ради проверяемости."""
    from app.models.client import CANDIDATE_SOURCE_RESCAN
    from app.services import app_settings, phone_parse
    from app.services import clients as clients_svc
    from app.services.audit import write_audit

    if not await app_settings.get(db, app_settings.PHONE_DETECT_ENABLED):
        typer.echo("Распознавание телефонов выключено в настройках — прогон отменён.")
        return
    if not await app_settings.get(db, app_settings.PHONE_DETECT_AUTOFILL):
        # ⚠ НАСТРОЙКА ГЛАВНЕЕ КОМАНДЫ. Выключенная автозапись означает решение
        # владельца «в карточку пишет только человек». Массовая запись мимо неё
        # была бы подменой его решения нашим.
        typer.echo(
            "Автозаполнение карточки выключено (phone_detect.autofill=false) — прогон отменён.\n"
            "Эта команда лишь применяет к истории то правило, что уже действует в бою."
        )
        return

    # Входящие клиента ПО ВОЗРАСТАНИЮ времени: карточку обязан заполнить первый
    # названный номер — тот же, что заполнил бы её в бою.
    rows = (
        await db.execute(
            sa.select(
                Message.id,
                Message.body,
                Message.created_at,
                Conversation.id.label("conversation_id"),
                Conversation.account_id,
                Conversation.client_id,
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Client, Client.id == Conversation.client_id)
            .where(
                Message.direction == "in",
                Message.sender_type == "client",
                Message.body.is_not(None),
                Client.phone.is_(None),
                # Человек уже трогал поле руками — не наше дело.
                Client.phone_set_at.is_(None),
            )
            .order_by(Message.created_at.asc())
        )
    ).all()

    # Первый номер каждой карточки. Разбор чистый и без базы — один проход в
    # памяти, ни одного запроса на сообщение.
    первый: dict[uuid.UUID, Any] = {}
    for row in rows:
        if row.client_id in первый:
            continue
        hit = phone_parse.find_first(row.body)
        if hit is not None:
            первый[row.client_id] = (row, hit)

    # ⚠ НОМЕР, КОТОРЫЙ МЫ САМИ ПИСАЛИ ЭТОМУ ЧЕЛОВЕКУ, — НЕ ЕГО НОМЕР.
    #
    # Клиент цитирует то, что дали ему мы: «вы прислали 8-90…, он не отвечает».
    # Разбор видит цифры во ВХОДЯЩЕМ и по букве правила прав, а записать это в
    # карточку значит подставить телефон мастера вместо заказчика. Замер боя
    # 02.09: таких три из 2040.
    #
    # ⚠ ПОЧЕМУ ЗДЕСЬ ПРАВИЛО СТРОЖЕ, ЧЕМ В БОЮ. В бою карточка меняется на
    # глазах у диспетчера: он видит подставленный номер и поправит его в ту же
    # минуту. Массовую заливку не смотрит НИКТО — две тысячи карточек меняются
    # молча. Одинаковая строгость там, где разная цена ошибки, — не
    # последовательность, а невнимательность.
    исходящие = (
        await db.execute(
            sa.select(Conversation.client_id, Message.body)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.direction == "out",
                Message.body.is_not(None),
                Conversation.client_id.in_(list(первый)),
            )
        )
    ).all()
    наши: dict[uuid.UUID, set[str]] = {}
    for client_id, body in исходящие:
        for hit in phone_parse.find_all(body):
            наши.setdefault(client_id, set()).add(hit.value)

    свои_же = [cid for cid, (_, hit) in первый.items() if hit.value in наши.get(cid, ())]
    for cid in свои_же:
        первый.pop(cid, None)
    if свои_же:
        typer.echo(f"пропущено (номер писали ему мы сами): {len(свои_же)}")

    очередь = list(первый.values())[:limit]
    typer.echo(f"просмотрено входящих: {len(rows)}")
    typer.echo(f"пустых карточек с номером в переписке: {len(первый)}")
    typer.echo(f"будет заполнено за этот прогон: {len(очередь)} (--limit {limit})")
    for row, hit in очередь[:10]:
        typer.echo(f"  {hit.value}  ←  «{hit.raw}»  (диалог {row.conversation_id})")
    if len(очередь) > 10:
        typer.echo(f"  … и ещё {len(очередь) - 10}")

    if dry_run:
        typer.echo("Сухой прогон: ничего не изменено. Повторите с --no-dry-run.")
        return

    сейчас = datetime.now(UTC)
    заполнено = 0
    пропущено = 0
    for row, hit in очередь:
        client = await db.get(Client, row.client_id)
        if client is None or client.phone is not None or client.phone_set_at is not None:
            пропущено += 1
            continue
        новая = await clients_svc.record_phone_candidate(
            db,
            client=client,
            conversation_id=row.conversation_id,
            message_id=row.id,
            message_at=row.created_at,
            found=hit,
            source=CANDIDATE_SOURCE_RESCAN,
            accepted=True,
            now=сейчас,
        )
        if not новая:
            # По этому номеру решение уже принимал человек — принял, отклонил
            # или добавил вторым. Автозапись с человеком не спорит: память об
            # отказе лежит не в пустом поле `clients.phone`, и заполнить его
            # значило бы молча вернуть отклонённое.
            пропущено += 1
            continue
        client.phone = hit.value
        client.phone_account_id = row.account_id
        client.phone_conversation_id = row.conversation_id
        await write_audit(
            db,
            user_id=None,
            action="client.phone_captured",
            entity="client",
            entity_id=str(client.id),
            details={"conversation_id": str(row.conversation_id), "source": "regex"},
        )
        заполнено += 1
    await db.commit()
    typer.echo(f"заполнено карточек: {заполнено}, пропущено: {пропущено}")
    typer.echo("Непустые карточки не тронуты; решения людей не переписаны.")


@app.command("prune-read-markers")
def prune_read_markers(
    keep: int = typer.Option(5000, help="Сколько самых свежих маркеров оставить каждому"),
    dry_run: bool = typer.Option(True, help="Показать размеры и выйти, ничего не удаляя"),
) -> None:
    """Подрезать разросшиеся хэши отметок прочтения.

    ЗАЧЕМ КОМАНДА, ЕСЛИ ФУНКЦИЯ БЫЛА. `read_markers.prune_marker_hash` написана
    давно и в докстроке обещает «вызывается по желанию (крон/скрипт)». Скрипта
    при этом не существовало: позвать её было неоткуда, кроме собственного
    теста. Обещание без исполнения — та же неправда, что и настройка без
    потребителя, поэтому здесь ровно то, что было обещано, и ничего сверх.

    ⚠ СЕГОДНЯ ЕЙ НЕЧЕГО ДЕЛАТЬ, И ЭТО ИЗМЕРЕНО, А НЕ ПРЕДПОЛОЖЕНО. На бою
    23.08 самый большой хэш — 1545 отметок при пороге 5000; подрезка вернула бы
    ноль. Команда заведена как рычаг на случай, когда порог будет пройден: за
    годы работы диалогов у диспетчера накапливаются тысячи, а хэш живёт до
    истечения своего TTL.

    По умолчанию — сухой прогон: печатает размеры и не трогает ничего.
    """

    async def main(db: AsyncSession) -> None:  # noqa: ARG001 — база здесь не нужна
        redis = redis_mod.get_client()
        всего_снято = 0
        # Ключи перебираем сканом, а не `keys`: на боевом Redis блокирующий
        # обход — это остановка всего, что через него ходит.
        async for key in redis.scan_iter(match=read_markers.KEY.format(user_id="*")):
            имя = key.decode() if isinstance(key, bytes) else str(key)
            # `aw` сужает тип команды redis-py (живёт у самого клиента,
            # `core/redis.py`): без него проверка типов видит
            # `Awaitable[int] | int` и справедливо ругается на await.
            размер = await redis_mod.aw(redis.hlen(имя))
            if размер <= keep:
                continue
            user_id = имя.rsplit(":", 1)[-1]
            if dry_run:
                typer.echo(f"{имя}: {размер} отметок — снялось бы {размер - keep}")
                continue
            снято = await read_markers.prune_marker_hash(redis, uuid.UUID(user_id), keep=keep)
            всего_снято += снято
            typer.echo(f"{имя}: было {размер}, снято {снято}")
        if dry_run:
            typer.echo("Сухой прогон: ничего не изменено. Повторите с --no-dry-run.")
        else:
            typer.echo(f"Снято отметок всего: {всего_снято}")

    _run(main)


@app.command("seed-templates")
def seed_templates(
    dry_run: bool = typer.Option(True, help="Показать, что появится, и выйти"),
) -> None:
    """Завести ОБЩИЕ быстрые ответы — те, что видны всем без настройки.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 28.08: «быстрые ответы должны автоматически появляться
    у людей, чтобы им не обязательно было самому их вручную прописывать».

    ЧТО БЫЛО. Правило видимости давно верное: человек видит СВОИ личные плюс
    ВСЕ общие (`owner_id IS NULL`). Только общих в базе было РОВНО НОЛЬ — замер
    28.08: двадцать заготовок, все личные, и восемнадцать из них принадлежат
    одному человеку. Остальные двенадцать диспетчеров открывали пустую полосу и
    набирали «Здравствуйте» руками — шесть с половиной тысяч раз за месяц.

    Завести общие через интерфейс менеджер не мог: право `templates:shared`
    есть только у администратора и руководителя. То есть у большинства не было
    ни заготовок, ни способа их получить.

    ЧТО ДЕЛАЕТ. Создаёт недостающие общие заготовки из набора выше. ПО
    ЗАГОЛОВКУ и только недостающие: повторный запуск ничего не задваивает и не
    трогает исправленный кем-то текст — общая заготовка после создания
    принадлежит команде, и переписывать её из кода значило бы отменять чужую
    правку при каждой выкатке.

    ЧЕГО НЕ ДЕЛАЕТ. Не трогает ЛИЧНЫЕ заготовки: они отражают манеру
    конкретного человека, и превращать их в общие без его ведома нельзя.
    """

    async def main(db: AsyncSession) -> None:
        from app.data.base_templates import БАЗОВЫЕ_ЗАГОТОВКИ
        from app.models.template import Template

        существуют = {
            имя
            for (имя,) in (
                await db.execute(sa.select(Template.title).where(Template.owner_id.is_(None)))
            ).all()
        }
        нехватает = [z for z in БАЗОВЫЕ_ЗАГОТОВКИ if z[1] not in существуют]

        # ⚠ И ДОСТАВИТЬ СТАРТОВЫЙ СЧЁТ ТЕМ, У КОГО ЕГО НЕТ (29.08).
        #
        # Заготовки завели 28.08, а колонка `used_count` появилась 29-го — то
        # есть все тридцать девять получили ноль, и подсказка сортировала их по
        # алфавиту. Отбор по заголовку при повторном запуске честно отвечал
        # «добавлять нечего» и мимо этого проходил.
        #
        # Трогаем ТОЛЬКО нулевые: ноль значит «ни разу не применяли», и
        # измеренное число там лучше пустоты. У заготовки с живым счётом свой
        # счёт уже правдивее нашего замера — его не перебиваем.
        измеренное = {имя: раз for _, имя, _, раз in БАЗОВЫЕ_ЗАГОТОВКИ}
        без_счёта = (
            (
                await db.execute(
                    sa.select(Template).where(
                        Template.owner_id.is_(None),
                        Template.used_count == 0,
                        Template.title.in_(list(измеренное)),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not нехватает and not без_счёта:
            typer.echo("Все базовые заготовки уже есть, счётчики проставлены — делать нечего.")
            return

        typer.echo(f"Появится общих заготовок: {len(нехватает)} (уже есть {len(существуют)})")
        папка_была = ""
        for папка, имя, тело, раз in нехватает:
            if папка != папка_была:
                typer.echo(f"  [{папка}]")
                папка_была = папка
            typer.echo(f"    · {имя}: {тело}  ({раз} раз набирали руками)")

        if без_счёта:
            typer.echo(f"Проставим стартовый счёт: {len(без_счёта)} заготовкам")

        if dry_run:
            typer.echo("Сухой прогон: ничего не создано. Повторите с --no-dry-run.")
            return

        for row in без_счёта:
            row.used_count = измеренное[row.title]

        for папка, имя, тело, раз in нехватает:
            db.add(
                Template(
                    owner_id=None,
                    title=имя,
                    body=тело,
                    folder=папка,
                    # ⚠ СЧЁТЧИК СТАРТУЕТ НЕ С НУЛЯ (29.08). Пустой у всех
                    # означает, что первую неделю подсказка сортирует по
                    # алфавиту — то есть бесполезна ровно тогда, когда человек
                    # к ней привыкает. Число — сколько раз фразу набрали
                    # руками до появления заготовки; это не выдумка, а тот же
                    # живой сигнал, только собранный раньше.
                    used_count=раз,
                )
            )
        await db.commit()
        typer.echo(
            f"Создано: {len(нехватает)}, проставлен счёт: {len(без_счёта)}. "
            "Их видят все — личные заготовки не тронуты."
        )

    _run(main)


@app.command("dismiss-stale-alerts")
def dismiss_stale_alerts(
    kind: str = typer.Option(..., help="Вид уведомления, например inbound.stalled"),
    before: str = typer.Option(..., help="Только созданные ДО этой даты, ГГГГ-ММ-ДД"),
    dry_run: bool = typer.Option(True, help="Показать, сколько и чьих, и выйти"),
) -> None:
    """Погасить завал непрочитанных тревог одного вида.

    ЗАЧЕМ ЭТО ОТДЕЛЬНАЯ КОМАНДА, А НЕ ЗАПРОС РУКАМИ. Разбор 11 августа нашёл на
    боевой системе 112 непрочитанных `inbound.stalled` за шесть дней — все
    ложные: порог молчания стоял 30 минут вместо четырёх часов, и сторож будил
    по любому затишью. Порог починен, новых не появляется. Но СТАРЫЕ никуда не
    делись, и они закрыли собой два настоящих отказа резервной копии от
    8 августа: в колокольчике сто с лишним одинаковых строк, среди которых два
    важных сообщения нашлись только запросом в базу.

    Пока завал не разобран, колокольчик бесполезен: его перестают открывать, и
    следующая настоящая тревога опоздает ровно настолько, насколько человек
    привык не смотреть.

    ЧТО ДЕЛАЕТ. Ставит `read_at` уведомлениям названного вида, созданным раньше
    указанной даты. Не удаляет: тревога остаётся в журнале и в выгрузке, из
    колокольчика уходит только счётчик непрочитанного.

    ЧЕГО НЕ ДЕЛАЕТ. Не трогает свежие — граница задаётся руками и намеренно:
    «погасить всё» пряталo бы и то, что появилось минуту назад.

    По умолчанию — сухой прогон: сначала посмотреть, потом гасить.

    ЗАПУСКАТЬ НА СЕРВЕРЕ, А НЕ У СЕБЯ:

        ssh leadchat 'docker exec leadchat-api-1 \
            python -m app.cli dismiss-stale-alerts \
            --kind inbound.stalled --before 2026-08-12 --no-dry-run'
    """
    граница = datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=UTC)

    async def main(db: AsyncSession) -> None:
        rows = (
            await db.execute(
                sa.select(Notification.recipient_id, sa.func.count())
                .where(
                    Notification.kind == kind,
                    Notification.read_at.is_(None),
                    Notification.created_at < граница,
                )
                .group_by(Notification.recipient_id)
            )
        ).all()
        всего = sum(int(n) for _, n in rows)
        if not всего:
            typer.echo(f"Непрочитанных «{kind}» раньше {before} нет — гасить нечего.")
            return

        typer.echo(f"Непрочитанных «{kind}» раньше {before}: {всего} (получателей: {len(rows)}).")
        if dry_run:
            typer.echo("Сухой прогон: ничего не изменено. Повторите с --no-dry-run.")
            return

        await db.execute(
            sa.update(Notification)
            .where(
                Notification.kind == kind,
                Notification.read_at.is_(None),
                Notification.created_at < граница,
            )
            .values(read_at=datetime.now(UTC))
        )
        await db.commit()
        typer.echo(f"Погашено: {всего}. В журнале они остались, из колокольчика ушли.")

    _run(main)


@app.command("backfill-items")
def backfill_items(
    limit: int = typer.Option(500, help="Сколько диалогов взять за прогон"),
    dry_run: bool = typer.Option(False, help="Показать, скольким нужно объявление, и выйти"),
) -> None:
    """Догнать объявления у диалогов, где колонка пуста (docs/33 §14а).

    ЗАЧЕМ РАЗОВАЯ КОМАНДА, ЕСЛИ ЕСТЬ СВЕРКА. Сверка ходит только по чатам с
    непрочитанным (``unread_only=true``) — накопленные закрытые и отвеченные
    диалоги она не увидит никогда. Их и добирает эта команда: по одному
    запросу карточки чата на диалог, через ту же ARQ-задачу, что и ленивое
    обогащение, поэтому дедуп и идемпотентность достаются даром.

    Гоняется столько раз, сколько нужно: уже заполненные диалоги задача
    пропускает сама.
    """

    async def main(db: AsyncSession) -> None:
        from app.integrations.avito.listing_url import parse_listing_url
        from app.services.client_enrich import enqueue_enrich_client

        # СНАЧАЛА ГОРОД — ОН БЕРЁТСЯ ИЗ УЖЕ ИЗВЕСТНОЙ ССЫЛКИ, БЕЗ АВИТО.
        #
        # Показывается город и без этого: сборка ответа разбирает ссылку на
        # лету, когда колонка пуста. Но снимок нужен сам по себе — по нему
        # можно будет отбирать и считать, а разбор на лету отвечает только на
        # вопрос «что показать сейчас». Один проход по базе, ни одного запроса
        # в чужой API.
        city_rows = (
            (
                await db.execute(
                    select(Conversation).where(
                        Conversation.item_url.is_not(None),
                        Conversation.item_city_slug.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        filled_city = 0
        for conv in city_rows:
            slug = parse_listing_url(conv.item_url).city_slug
            if slug:
                conv.item_city_slug = slug
                filled_city += 1
        if filled_city and not dry_run:
            await db.commit()
        typer.echo(
            f"город из ссылки: {'проставили бы' if dry_run else 'проставлено'} "
            f"{filled_city} из {len(city_rows)} (у остальных города в ссылке нет)"
        )

        rows = (
            (
                await db.execute(
                    select(Conversation)
                    .join(AvitoAccount, AvitoAccount.id == Conversation.account_id)
                    .where(
                        Conversation.item_title.is_(None),
                        AvitoAccount.status == "active",
                    )
                    .order_by(Conversation.last_message_at.desc().nullslast())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await db.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.item_title.is_(None))
            )
        ).scalar_one()

        typer.echo(f"диалогов без объявления всего: {total}")
        if dry_run:
            typer.echo(f"взяли бы за прогон: {len(rows)} (--limit {limit})")
            return

        redis = redis_mod.get_client()
        for conv in rows:
            await enqueue_enrich_client(redis, conv.id)
        typer.echo(f"поставлено задач: {len(rows)}")
        typer.echo("  выполняются воркером; повторите команду, пока total не перестанет падать")

    _run(main)


# ------------------------------------------- история канала (docs/41 §11)


async def _resolve_accounts(db: AsyncSession, account: str | None) -> list[AvitoAccount]:
    """Аккаунты по номеру, названию или идентификатору; без аргумента — все живые.

    Три способа назвать канал, потому что все три встречаются в жизни: в
    журналах сервера лежит UUID, в кабинете Авито — числовой номер, а человек
    помнит название. Заставлять его искать идентификатор ради одной команды —
    способ сделать команду невызываемой.
    """
    stmt = select(AvitoAccount).order_by(AvitoAccount.created_at)
    rows = list((await db.execute(stmt)).scalars().all())
    if account is None:
        return [row for row in rows if row.status == "active"]
    needle = account.strip().lower()
    found = [
        row
        for row in rows
        if str(row.id) == needle
        or str(row.avito_user_id) == needle
        or row.title.strip().lower() == needle
    ]
    return found


@app.command("load-history")
def load_history(
    account: str | None = typer.Option(
        None, help="UUID, номер Авито или название канала. Без него — все активные."
    ),
    depth: str = typer.Option(
        "all", help="Глубина: all — вся история, since-connect — только с момента подключения"
    ),
    restart: bool = typer.Option(
        False, help="Начать заново: забыть, какие чаты уже разобраны в прошлом заходе"
    ),
) -> None:
    """Загрузить историю канала — «все диалоги, которые были и есть».

    ЗАЧЕМ КОМАНДА, ЕСЛИ ЗАГРУЗКА ИДЁТ ПРИ ПОДКЛЮЧЕНИИ. Два боевых аккаунта
    подключены 8 августа — под решением, что история до подключения не нужна.
    Требование от 11 августа обратное, и для уже подключённых каналов
    выполнить его можно ровно одним способом: запустить загрузку ещё раз.
    Второй повод — продолжить прогон после остановки или после срыва.

    Повторный запуск безопасен: сообщения дедуплицируются по идентификатору
    Авито, диалоги — по идентификатору чата. Уже разобранные в прошлом заходе
    чаты пропускаются, поэтому продолжение стоит дёшево; ``--restart`` эту
    память сбрасывает.
    """
    from app.services import avito_accounts as accounts_service

    chosen = depth.strip().lower().replace("-", "_")
    if chosen not in accounts_service.HISTORY_DEPTHS:
        typer.echo(
            f"Неизвестная глубина: {depth}. Ожидается all или since-connect "
            "(all — вся история, since-connect — только с момента подключения)"
        )
        raise typer.Exit(code=2)

    async def main(db: AsyncSession) -> None:
        accounts = await _resolve_accounts(db, account)
        if not accounts:
            typer.echo("Каналов не нашлось — проверьте имя или номер")
            raise typer.Exit(code=1)

        redis = redis_mod.get_client()
        for row in accounts:
            if row.status != "active":
                typer.echo(f"{row.title}: канал {row.status} — история не грузится")
                continue
            if restart:
                # Сбрасываем ТОЛЬКО память о разобранных чатах и пометку
                # срыва. Сама переписка не трогается: «начать заново» — про
                # обход чатов, а не про удаление того, что уже загружено.
                await redis.delete(f"backfill:seen:{row.id}")
                await redis.delete(f"backfill:{row.id}")
                await redis.delete(f"backfill:failed:{row.id}")
            # Дедуп по идентификатору задачи здесь ВЫКЛЮЧЕН намеренно: ARQ
            # держит ключ выполненной задачи час и повторную постановку молча
            # отбрасывает. Для команды, которую человек запускает руками,
            # «ничего не произошло и никто не сказал почему» — худший исход.
            await accounts_service.enqueue_backfill(row.id, chosen, dedupe=False)
            human_depth = (
                "вся история" if chosen == accounts_service.HISTORY_ALL else "с момента подключения"
            )
            typer.echo(f"{row.title}: загрузка поставлена в очередь (глубина: {human_depth})")
        typer.echo("Ход работы — на карточке канала в настройках и в `history-status`")

    _run(main)


@app.command("stop-history")
def stop_history(
    account: str | None = typer.Option(None, help="UUID, номер Авито или название канала"),
) -> None:
    """Остановить загрузку истории. Загруженное остаётся, продолжение — с того же места.

    Останов просьбой, а не убийством задачи: прогон дочитывает текущий чат,
    записывает его целиком и только тогда выходит. Оборванный на середине чат
    оставил бы полудиалог и точку возобновления, которая врёт.
    """
    from app.services import avito_accounts as accounts_service

    async def main(db: AsyncSession) -> None:
        accounts = await _resolve_accounts(db, account)
        if not accounts:
            typer.echo("Каналов не нашлось — проверьте имя или номер")
            raise typer.Exit(code=1)
        redis = redis_mod.get_client()
        for row in accounts:
            await accounts_service.request_backfill_stop(redis, row.id)
            typer.echo(f"{row.title}: попросили остановиться (дочитает текущий чат)")

    _run(main)


@app.command("history-status")
def history_status(
    account: str | None = typer.Option(None, help="UUID, номер Авито или название канала"),
) -> None:
    """Показать ход загрузки истории по каналам."""
    from app.services import avito_accounts as accounts_service

    labels = {
        "idle": "не идёт",
        "running": "идёт",
        "stopped": "остановлена",
        "failed": "сорвалась",
    }

    async def main(db: AsyncSession) -> None:
        accounts = await _resolve_accounts(db, account)
        if not accounts:
            typer.echo("Каналов не нашлось — проверьте имя или номер")
            raise typer.Exit(code=1)
        redis = redis_mod.get_client()
        for row in accounts:
            state = await accounts_service.get_backfill_state(redis, row.id)
            status = str(state.get("status"))
            line = f"{row.title}: {labels.get(status, status)}"
            loaded, total = state.get("loaded"), state.get("total")
            if loaded is not None:
                line += f", загружено {loaded}" + (f" из {total}" if total is not None else "")
            if state.get("queued"):
                line += f", в очередь {state['queued']}"
            if state.get("failed_chats"):
                line += f", не удалось {state['failed_chats']}"
            typer.echo(line)

    _run(main)


@app.command("sentry-test")
def sentry_test() -> None:
    """Отправить контрольное исключение в Sentry (чек-лист запуска 05 §8)."""
    configure_logging(component="cli")
    if not init_sentry("cli"):
        typer.echo("Sentry не сконфигурирован: пуст SENTRY_DSN или не установлен sentry-sdk")
        raise typer.Exit(code=1)
    try:
        raise RuntimeError("LeadChat sentry-test: контрольное исключение")
    except RuntimeError as exc:
        import sentry_sdk  # см. core/observability

        sentry_sdk.capture_exception(exc)
        sentry_sdk.flush(timeout=5)
    typer.echo("Событие отправлено — проверьте проект leadchat-backend в Sentry")


@app.command("leads-token")
def leads_token(
    revoke: bool = typer.Option(False, "--revoke", help="Отозвать токен, выключив автозаявки"),
) -> None:
    """Выпустить токен для расширения «Автозаявки» и показать его ОДИН раз.

    ЗАЧЕМ КОМАНДА, ЕСЛИ ЕСТЬ РУЧКА. Экран настроек автозаявок ещё не сделан, а
    подключить расширение нужно уже сейчас. Команда делает ровно то же, что
    сделает кнопка: выпускает новый токен и печатает его — второго раза не
    будет, дальше он хранится только зашифрованным.

    Выпуск нового немедленно закрывает старый: расширение со старым получит 401
    и перестанет забирать лиды. Это и есть способ отозвать доступ.

    ⚠ ВЫПУСК ЗАКРЫТ ТЕМ ЖЕ ПРЕДОХРАНИТЕЛЕМ, ЧТО И РУЧКА (`leads.SECOND_PATH_FUSE`).
    Написана она была ДО решения владельца от 16.08 и про него не знала: ручку
    закрыли, а команду — нет. Дверь на сервере — как раз та, куда пойдёт человек,
    которому «надо просто подключить расширение», и открывалась она молча.

    ОТЗЫВ (`--revoke`) предохранителем НЕ закрыт и не должен быть: он выключает
    путь, а не включает. Закрытая дверь, которую нельзя закрыть ещё плотнее, —
    это ловушка, а не защита.
    """
    from app.services import leads as leads_svc

    if not revoke and leads_svc.SECOND_PATH_FUSE:
        typer.echo(leads_svc.SECOND_PATH_FUSE)
        raise typer.Exit(code=2)

    async def main(db: AsyncSession) -> None:

        if revoke:
            await leads_svc.set_token(db, "")
            await db.commit()
            typer.echo("Токен отозван — автозаявки выключены.")
            return

        token = await leads_svc.set_token(db, leads_svc.new_token())
        await db.commit()
        typer.echo("")
        typer.echo("Токен для расширения «Автозаявки» (показывается один раз):")
        typer.echo("")
        typer.echo(f"    {token}")
        typer.echo("")
        typer.echo("В расширении: Настройки → источник LeadChat →")
        typer.echo("    адрес  https://188-225-34-82.sslip.io/api/v1")
        typer.echo("    токен  (строка выше)")
        typer.echo("")
        typer.echo("⚠ Токен открывает доступ к телефонам клиентов. Не пересылайте")
        typer.echo("  его в чат и не храните в файле — впишите сразу в расширение.")

    _run(main)


@app.command("triage-queue")
def triage_queue(
    apply: bool = typer.Option(False, "--apply", help="Выполнить, а не только показать"),
    stale_days: int = typer.Option(14, help="Сколько дней молчания клиента считать протухшим"),
) -> None:
    """Разобрать очередь по правилу «чьё последнее слово» (боевой случай 19.08).

    ЗАЧЕМ. Очередь — это «клиент написал и ждёт НАС». Загрузка истории считала
    свежесть по любому последнему событию, включая наш собственный ответ, и в
    очередь попали диалоги, где ход давно за клиентом: владелец увидел это как
    «некоторые диалоги висят 47 дней». Источник починен, но накопленное надо
    разобрать, а закрывать всё подряд нельзя — среди них есть живая работа.

    ТРИ КУЧИ, КАЖДАЯ ПО СВОЕМУ СМЫСЛУ:
      * последнее слово НАШЕ — «Ждёт клиента». Не закрываем: разговор жив,
        просто ход не наш. Клиент ответит — диалог вернётся в работу сам;
      * последнее слово клиента, но он молчит дольше `--stale-days` — закрыть:
        ответ на позавчерашний вопрос уместен, на месячный уже нет;
      * последнее слово клиента и оно свежее — ОСТАЁТСЯ в очереди. Это работа.

    Без `--apply` только показывает числа: команда, которая молча меняет
    боевые данные, — не сервисная команда, а ловушка.
    """
    import sqlalchemy as sa

    from app.services import conversation_status as status_dict

    async def main(db: AsyncSession) -> None:
        порог = datetime.now(UTC) - timedelta(days=stale_days)
        последнее_направление = (
            sa.select(Message.direction)
            .where(Message.conversation_id == Conversation.id, Message.direction.in_(("in", "out")))
            .order_by(Message.created_at.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        последнее_клиента = (
            sa.select(sa.func.max(Message.created_at))
            .where(
                Message.conversation_id == Conversation.id,
                Message.direction == "in",
                Message.sender_type == "client",
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        строки = (
            await db.execute(
                sa.select(Conversation, последнее_направление, последнее_клиента).where(
                    Conversation.status == "new",
                    Conversation.assignee_id.is_(None),
                    Conversation.claimed_by_id.is_(None),
                )
            )
        ).all()

        ждут_клиента: list[Conversation] = []
        закрыть: list[Conversation] = []
        останутся = 0
        for conv, направление, когда in строки:
            # SQLite (тестовый стек) отдаёт время без зоны — сравнение с
            # порогом падало бы «can't compare offset-naive and offset-aware».
            # Та же мина уже ловилась в выдаче лидов; чиним приведением.
            if когда is not None and когда.tzinfo is None:
                когда = когда.replace(tzinfo=UTC)
            if направление == "out":
                ждут_клиента.append(conv)
            elif когда is None or когда < порог:
                закрыть.append(conv)
            else:
                останутся += 1

        typer.echo(f"В очереди сейчас: {len(строки)}")
        typer.echo(f"  ход за клиентом -> «Ждёт клиента»: {len(ждут_клиента)}")
        typer.echo(f"  клиент молчит дольше {stale_days} дней -> закрыть: {len(закрыть)}")
        typer.echo(f"  останется настоящей работы: {останутся}")
        if not apply:
            typer.echo("\nЭто показ. Чтобы выполнить: triage-queue --apply")
            return

        сейчас = datetime.now(UTC)
        for conv in ждут_клиента:
            status_dict.set_status(conv, "waiting_client", now=сейчас)
            conv.offered_at = None
            conv.claimed_at = None
            conv.awaiting_since = None  # ждём КЛИЕНТА, а не мы его
            conv.escalated_at = None
            conv.updated_at = сейчас
        for conv in закрыть:
            status_dict.set_status(conv, "closed", now=сейчас)
            conv.offered_at = None
            conv.claimed_at = None
            conv.awaiting_since = None
            conv.escalated_at = None
            conv.unread_count = 0
            conv.transfer_to_id = None
            conv.transfer_by_id = None
            conv.transfer_at = None
            conv.transfer_comment = None
            conv.updated_at = сейчас
        db.add(
            AuditLog(
                user_id=None,
                action="conversation.bulk_triaged",
                entity="conversation",
                entity_id=None,
                details={
                    "rule": "очередь = клиент написал последним и не позже порога",
                    "stale_days": stale_days,
                    "waiting_client": len(ждут_клиента),
                    "closed": len(закрыть),
                    "left": останутся,
                },
            )
        )
        await db.commit()
        typer.echo("\nГотово. Обновите страницу: счётчик очереди живёт на событиях.")

    _run(main)


# --- автоматика карточки (12.09) ---------------------------------------------


@app.command("phone-backlog")
def phone_backlog(
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не записывать"),
) -> None:
    """Старые предложения «второй номер» — по правилам 12.09: дополнительными.

    До 12.09 второй номер на заполненной карточке ложился ВОПРОСОМ оператору
    (`pending`), и 70 % вопросов оставались без ответа. Теперь такие номера
    ложатся дополнительными сами. Команда доводит накопленные `pending` до
    того же состояния, что даёт живой путь: принятая строка без автора и
    строка журнала с `backfill:true`. Основной номер НЕ ТРОГАЕТСЯ ни у одной
    карточки; наши номера и 8-800 отклоняются с причиной.

    Печатает только числа. По умолчанию — сухой прогон.
    """
    _run(lambda db: run_phone_backlog(db, dry_run=dry_run))


async def run_phone_backlog(db: AsyncSession, *, dry_run: bool) -> None:
    from app.services import app_settings, phone_rules
    from app.services import clients as clients_svc

    own = phone_rules.parse_own_numbers(await app_settings.get(db, app_settings.PHONE_OWN_NUMBERS))
    rows = list(
        (
            await db.execute(
                select(ClientPhoneCandidate, Client.phone)
                .join(Client, Client.id == ClientPhoneCandidate.client_id)
                .where(ClientPhoneCandidate.status == "pending")
                .order_by(ClientPhoneCandidate.detected_at)
            )
        ).all()
    )
    итог = {
        "accepted": 0,
        "filled_primary": 0,
        "rejected_own": 0,
        "rejected_toll_free": 0,
        "near_primary": 0,
        "raced": 0,
    }
    now = datetime.now(UTC)
    for row, primary in rows:
        # На ПУСТОЙ карточке старое предложение — это первый годный номер: он
        # идёт в основной, как на живом пути, а не «дополнительным без основного».
        verdict = phone_rules.classify(row.phone, index=0, primary=primary, own=own, autofill=True)
        if verdict.kind == phone_rules.SKIP_OWN:
            status, ключ = "rejected", "rejected_own"
        elif verdict.kind == phone_rules.SKIP_TOLL_FREE:
            status, ключ = "rejected", "rejected_toll_free"
        elif verdict.kind == phone_rules.FILL_PRIMARY:
            status, ключ = "accepted", "filled_primary"
        else:
            status, ключ = "accepted", "accepted"
        похож = phone_rules.near_duplicate(primary, row.phone)
        if похож:
            итог["near_primary"] += 1
        if dry_run:
            итог[ключ] += 1
            continue
        result = await db.execute(
            sa.update(ClientPhoneCandidate)
            .where(ClientPhoneCandidate.id == row.id, ClientPhoneCandidate.status == "pending")
            .values(status=status, resolved_at=now, resolved_by_id=None)
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            итог["raced"] += 1  # оператор нажал раньше — его решение старше
            continue
        if ключ == "filled_primary":
            card = await db.get(Client, row.client_id)
            account_id = (
                await db.execute(
                    select(Conversation.account_id).where(Conversation.id == row.conversation_id)
                )
            ).scalar_one_or_none()
            if card is not None and row.conversation_id is not None and account_id is not None:
                занято = await clients_svc.заполнить_основной(
                    db,
                    card,
                    row.phone,
                    conversation_id=row.conversation_id,
                    account_id=account_id,
                )
                if занято:
                    итог[ключ] += 1
                    await write_audit(
                        db,
                        user_id=None,
                        action="client.phone_captured",
                        entity="client",
                        entity_id=str(row.client_id),
                        details={
                            "conversation_id": str(row.conversation_id),
                            "source": "regex",
                            "backfill": True,
                        },
                    )
                    continue
            ключ = "accepted"  # основной успели заполнить — строка дополнительная
        итог[ключ] += 1
        await write_audit(
            db,
            user_id=None,
            action=(
                "client.phone_candidate_accepted"
                if status == "accepted"
                else "client.phone_candidate_rejected"
            ),
            entity="client",
            entity_id=str(row.client_id),
            details={
                "phone": row.phone,
                "conversation_id": str(row.conversation_id) if row.conversation_id else None,
                "message_id": str(row.message_id) if row.message_id else None,
                "primary": False,
                "auto": True,
                "rule": "extra_v2" if status == "accepted" else verdict.reason,
                "near_primary": похож,
                "backfill": True,
            },
        )
    if not dry_run:
        await db.commit()
    typer.echo(f"pending всего: {len(rows)}; сухой прогон: {dry_run}")
    for k, v in итог.items():
        typer.echo(f"  {k}: {v}")


@app.command("address-recheck")
def address_recheck(
    statuses: str = typer.Option(
        "ambiguous", help="Какие вердикты карты вернуть в очередь, через запятую"
    ),
    providers: str = typer.Option(
        "",
        help="Только строки, осуждённые этими картами (nominatim,yandex,none), через запятую",
    ),
    checked_since: str = typer.Option(
        "", help="Только строки, проверенные не раньше (ISO-время, UTC если без пояса)"
    ),
    regions: str = typer.Option(
        "",
        help="Только диалоги объявлений этих регионов, как в справочнике городов "
        "(«Республика Крым,Севастополь»), через запятую",
    ),
    levels: str = typer.Option(
        "", help="Только строки этих уровней разбора (A,B,C), через запятую"
    ),
    older_than_hours: float = typer.Option(
        REQUEUE_MIN_AGE_HOURS,
        help="Только строки старше стольких часов (окно вопроса клиенту, как у обхода); 0 — все",
    ),
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не записывать"),
) -> None:
    """Вернуть строки адреса с указанными вердиктами в очередь проверки по карте.

    Нужно после правки вердикта (12.09: четыре строения одного дома перестали
    быть «неоднозначностью», появились варианты и поиск по области): строки,
    осуждённые старым правилом, иначе так и висели бы с «уточните посёлок».
    И после суток без DaData (13.09): `--providers nominatim,yandex,none
    --checked-since …` возвращает всё, что за это время осудили карты
    послабее. Проверку ставит починка планировщика (`geo_repair`) в своём
    темпе — когда DaData снова в потолке.

    Это и есть ЕДИНСТВЕННЫЙ путь догона автопривязки (18.09): сброс →
    починка → воркер → степень → автозапись. Второго пути с расширенным
    набором статусов нет намеренно (память dva-puti-raznyi-schet). `no_city`
    сюда включать явно: у него тратится попытка на каждый проход, и после
    восьми починка строку не берёт — сброс обнуляет счётчик.

    `--levels A,B` (N24, 19.09) — только строки этих уровней разбора: улика
    «карта знает улицу, дома нет» годится лишь A/B (`STREET_EVIDENCE_LEVELS`),
    и догон `street_mismatch` без уровней вернул бы и невод C — сторожа его не
    пропустят, но запросы карт он потратит.

    С пакетом 5 (20.09) сброс кладёт строке снимок прежних улик (`geo_prev`):
    воркер не заменит их приговором послабее без части карт, а обход ставит
    такие строки в квоту доли Яндекса — залп любого размера растягивается на
    дни сам. `--older-than-hours` — граница возраста как у обхода (27 ч):
    строка моложе — окно вопроса клиенту.
    """
    _run(
        lambda db: run_address_recheck(
            db,
            statuses=statuses,
            providers=providers,
            checked_since=checked_since,
            regions=regions,
            levels=levels,
            older_than_hours=older_than_hours,
            dry_run=dry_run,
        )
    )


#: `--regions` отбирает строки в Python и отдаёт id списком в `IN (…)`: asyncpg
#: передаёт параметры счётчиком int16 — 32 767 на запрос, по одному на id.
#: Региональный срез узкий по замыслу (Крым — десятки строк); шире потолка —
#: это общий сброс, он делается без `--regions`.
_ПОТОЛОК_РЕГИОНАЛЬНОГО_СРЕЗА = 30_000
#: Сброс `address-recheck` — построчно со снимком улик, commit пачками: не
#: держать замки строк минутами при десятках тысяч (тот же довод, что у
#: `_ПАЧКА_АВТО`).
_ПАЧКА_СБРОСА = 500


async def run_address_recheck(
    db: AsyncSession,
    *,
    statuses: str,
    dry_run: bool,
    providers: str = "",
    checked_since: str = "",
    regions: str = "",
    levels: str = "",
    older_than_hours: float = 0,
) -> None:
    """Граница возраста по умолчанию стоит на РУЧКЕ (`address-recheck`), не
    здесь: команда — руки владельца, функция — вызов из кода и проверок с
    явным параметром."""
    from app.models import ClientAddressCandidate
    from app.services import address_parse
    from app.services import clients as clients_svc

    выбранные = [s.strip() for s in statuses.split(",") if s.strip()]
    условия = [
        ClientAddressCandidate.geo_status.in_(выбранные),
        ClientAddressCandidate.status == "pending",
    ]
    if older_than_hours and older_than_hours > 0:
        # Окно вопроса клиенту (ревью 20.09, п. 16): строка A/B моложе 27 ч в
        # `pending` запирала бы вопрос (`candidate_lock` → `geo_pending`).
        условия.append(
            ClientAddressCandidate.detected_at
            <= datetime.now(UTC) - timedelta(hours=float(older_than_hours))
        )
    уровни = [lvl.strip().upper() for lvl in levels.split(",") if lvl.strip()]
    if уровни:
        # Уровень — колонка строки (`level IN ('A','B','C')`), тот же признак,
        # по которому `STREET_EVIDENCE_LEVELS` пускает улику улицы в карточку.
        # Чужое имя уровня («AB» без запятой) — печать и выход, а не молчаливый
        # ноль строк: с `--no-dry-run` это выглядело бы как «нечего возвращать».
        чужие = sorted(
            set(уровни) - {address_parse.LEVEL_A, address_parse.LEVEL_B, address_parse.LEVEL_C}
        )
        if чужие:
            typer.echo(f"неизвестные уровни {чужие}: допустимы A, B, C через запятую")
            return
        условия.append(ClientAddressCandidate.level.in_(уровни))
    карты = [p.strip() for p in providers.split(",") if p.strip()]
    if карты:
        # «none» — строка без карты вовсе: место без DaData пишется строкой
        # «none», строки старее колонки — NULL (ревью 13.09). Имя карты бывает
        # с хвостом («dadata+yandex» — точку дал Яндекс, «dadata~approx» —
        # точка приблизительная, 18.09) и с приставкой подсказчика
        # («speller+dadata»): ищем по вхождению слова, а не по равенству.
        столбец = ClientAddressCandidate.geo_provider
        по_имени = sa.or_(
            столбец.in_(карты),
            *(
                образец
                for k in карты
                if k != "none"
                for образец in (
                    столбец.like(f"{k}+%"),
                    столбец.like(f"%+{k}"),
                    столбец.like(f"{k}~%"),
                    столбец.like(f"%+{k}~%"),
                    столбец.like(f"%+{k}+%"),
                )
            ),
        )
        условия.append(
            sa.or_(по_имени, ClientAddressCandidate.geo_provider.is_(None))
            if "none" in карты
            else по_имени
        )
    if checked_since:
        с = datetime.fromisoformat(checked_since)
        if с.tzinfo is None:
            с = с.replace(tzinfo=UTC)
        условия.append(ClientAddressCandidate.geo_checked_at >= с)
    области = {r.strip() for r in regions.split(",") if r.strip()}
    if области:
        # Регион объявления — производная города объявления, а город считает
        # ОДНА функция на всех: `conversation_city` (колонка `item_city_slug`,
        # без неё — ссылка) — та же, что у воркера и у `address-reparse`.
        # Голая колонка завела бы второй путь (память dva-puti-raznyi-schet):
        # у строк до задачи обогащения она пуста, а слаг в ссылке есть.
        # Отбираем в Python, id — в условие. Слаг вне справочника → региона не
        # знаем → строка не берётся; сколько таких — печатаем, а не молчим.
        from app.services.conversations import conversation_city, conversation_city_slug

        пары = (
            await db.execute(
                sa.select(ClientAddressCandidate.id, Conversation)
                .join(Conversation, Conversation.id == ClientAddressCandidate.conversation_id)
                .where(*условия)
            )
        ).all()
        ids: list[uuid.UUID] = []
        вне_справочника = без_слага = 0
        for cid, conv in пары:
            город = conversation_city(conv)
            if город is not None:
                if город.region in области:
                    ids.append(cid)
            elif conversation_city_slug(conv) is not None:
                вне_справочника += 1
            else:
                без_слага += 1
        typer.echo(
            f"регионы {sorted(области)}: {len(ids)} строк из {len(пары)}; "
            f"слаг вне справочника: {вне_справочника}; без слага: {без_слага} "
            "(город воркер берёт из реплики или другого диалога клиента — "
            "в региональный отбор не входят)"
        )
        if not ids:
            return
        if len(ids) > _ПОТОЛОК_РЕГИОНАЛЬНОГО_СРЕЗА:
            typer.echo(
                f"срез шире потолка {_ПОТОЛОК_РЕГИОНАЛЬНОГО_СРЕЗА}: "
                "это общий сброс, уберите --regions"
            )
            return
        условия.append(ClientAddressCandidate.id.in_(ids))
    stmt = sa.select(sa.func.count()).select_from(ClientAddressCandidate).where(*условия)
    всего = (await db.execute(stmt)).scalar_one()
    typer.echo(
        f"строк с вердиктом {выбранные}"
        + (f" от карт {карты}" if карты else "")
        + (f" проверенных с {checked_since}" if checked_since else "")
        + (f" регионов {sorted(области)}" if области else "")
        + (f" уровней {уровни}" if уровни else "")
        + (f" старше {older_than_hours:g} ч" if older_than_hours and older_than_hours > 0 else "")
        + f": {всего}; сухой прогон: {dry_run}"
    )
    if dry_run or not всего:
        return
    # Построчно со снимком прежних улик (пакет 5): один набор полей сброса на
    # все пути (N13, 19.09) — без времени проверки, строки карты и координат
    # под `pending`; улики уезжают в `geo_prev` и вернутся в строку, если
    # пересуд без части карт окажется слабее. Оптимистическое условие
    # `geo_checked_at IS NOT DISTINCT FROM` — как у обхода: чужая запись между
    # чтением и сбросом даёт rowcount 0. Commit — пачками.
    сброшено = 0
    строки = (await db.execute(sa.select(ClientAddressCandidate).where(*условия))).scalars().all()
    for i, row in enumerate(строки, 1):
        result = await db.execute(
            sa.update(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.id == row.id,
                *условия,
                ClientAddressCandidate.geo_checked_at.is_not_distinct_from(row.geo_checked_at),
            )
            .values(
                **clients_svc.сброс_вердикта(
                    с_попытками=False,
                    улики=clients_svc.снимок_улик(row, reason="cli_recheck"),
                )
            )
            .execution_options(synchronize_session=False)
        )
        сброшено += int(getattr(result, "rowcount", 0))
        if i % _ПАЧКА_СБРОСА == 0:
            await db.commit()
    await db.commit()
    # В журнал и на экран — сколько строк ЗАДЕЛ UPDATE, а не предварительный
    # COUNT (ревью 19.09, C3): между ними оператор принимает строку или воркер
    # меняет статус, и `geocode.requeue rows=` — единая точка grep по всем путям
    # сброса — иначе врала бы. COUNT остаётся сухому прогону и строке выше.
    log.info("geocode.requeue", reason="cli_recheck", rows=сброшено)
    typer.echo(f"возвращено в очередь: {сброшено}")


@app.command("address-reparse")
def address_reparse(
    days: int = typer.Option(60, help="Строки моложе стольких дней"),
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не записывать"),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="И принятые автоматикой строки: речь по нынешнему разбору — отклонить, "
        "карточку пересчитать",
    ),
    golden: bool = typer.Option(
        False,
        "--golden",
        help="Золотой корпус: принятые строки A/B перечитать сухо и посчитать, у скольких "
        "нынешний разбор меняет ключ или молчит (код выхода 1); только с сухим прогоном",
    ),
    rules: str | None = typer.Option(
        None,
        "--rules",
        help="Политика правил разбора «имя=on,…» поверх настройки — только в сухом прогоне",
    ),
    scope: str = typer.Option(
        "accepted",
        "--scope",
        help="Выборка золотого корпуса: accepted — принятые A/B (как всегда); all — все "
        "строки-дома за окно по статусам и уровням, чтобы видеть, сколько снимет правило; "
        "код выхода по-прежнему только по принятым A/B",
    ),
) -> None:
    """Перечитать сообщения нерешённых строк адреса новым разбором.

    Разбор учится (13.09: «Поселок Сосново дом 9» — посёлок, «Д. Сорокино
    улица Полевая 37» — деревня, «…, район Бугор»), а строки хранят разбор
    того дня, когда пришло сообщение: пункт пуст, и карта ищет дом не там.
    Команда дописывает пункт, город и район в строки с тем же ключом «улица,
    дом» и возвращает их в очередь проверки. Ключ строки не меняется никогда.

    `--auto` (19.09): строки, ПРИНЯТЫЕ АВТОМАТИКОЙ (`accepted` без человека),
    чья реплика по нынешнему разбору без адреса («Камера 4G» — дом «4G»
    старого разбора попал в карточку точкой улицы; замер боя 30 дн: 32 из
    5 928), отклоняются; если такая строка держит карточку — автозапись
    снимается, и карточку заново собирает воркер автозаписи из следующей
    годной строки (существующий путь, свой не пишется). Принятые человеком
    не трогаются никогда.

    `--golden` (21.09, программа §1.2 I-6): золотой корпус боя — принятые
    строки A/B за окно перечитываются нынешним разбором (с `--rules` — как
    если бы правило уже включили), и команда печатает, у скольких ключ
    «улица, дом» сменился или разбор замолчал; любое расхождение — код выхода
    1, шаг выкатки перед `--no-dry-run` падает. Ничего не пишет: с
    `--no-dry-run`/`--auto` несовместим (код 2).

    `--golden --scope all` (21.09, пакет 7a §B.2): те же принятые A/B в
    воротах, а сверх них — все строки-дома за окно (`pending`, `rejected`,
    любой уровень), счёт «снято / ключ изменился» по каждой паре статус/
    уровень. Смысл — сколько мусора снимет стоп-класс против того, сколько
    принятых он бы потерял; код выхода от нерешённых и отклонённых не зависит.
    """
    # Новые ключи — только когда заданы: подмены `run_address_reparse` со
    # старой сигнатурой (сторож обёртки пакета 3) иначе не примут вызов. Тот
    # же сторож зовёт обёртку напрямую без новых параметров — тогда вместо
    # умолчания приходит `OptionInfo`, и проверка строгая по типу, не «истинно».
    доп: dict[str, Any] = {}
    if golden is True:
        доп["golden"] = True
    if isinstance(rules, str) and rules:
        доп["rules_text"] = rules
    if isinstance(scope, str) and scope != "accepted":
        доп["scope"] = scope
    _run(lambda db: run_address_reparse(db, days=days, dry_run=dry_run, auto=auto, **доп))


#: Выборки `address-reparse --golden --scope`: принятые A/B (ворота, как
#: всегда) или все строки-дома за окно по статусам и уровням (пакет 7a §B.2).
_GOLDEN_SCOPE_ACCEPTED = "accepted"
_GOLDEN_SCOPE_ALL = "all"
_GOLDEN_SCOPES: tuple[str, ...] = (_GOLDEN_SCOPE_ACCEPTED, _GOLDEN_SCOPE_ALL)

#: Отказы карты, у которых новый разбор вправе сменить и дом.
_ОТКАЗЫ_КАРТЫ = frozenset(
    {
        "not_found",
        "house_missing",
        "house_mismatch",
        "street_mismatch",
        "settlement_mismatch",
        "city_mismatch",
        "region_mismatch",
    }
)


async def run_address_reparse(
    db: AsyncSession,
    *,
    days: int,
    dry_run: bool,
    auto: bool = False,
    redis: Any | None = None,
    golden: bool = False,
    rules_text: str | None = None,
    scope: str = _GOLDEN_SCOPE_ACCEPTED,
) -> None:
    from app.models import Client, ClientAddressCandidate
    from app.models.client import CANDIDATE_SOURCE_LLM
    from app.services import address_parse, inbound
    from app.services import clients as clients_svc

    if golden and (not dry_run or auto):
        # Золотой корпус — прибор, а не догон: его итог решает, идти ли в
        # `--no-dry-run`, и записи под ним быть не должно ни своей, ни `--auto`.
        typer.echo("--golden несовместим с --no-dry-run и --auto: золотой корпус только сухой")
        raise typer.Exit(code=2)
    if scope not in _GOLDEN_SCOPES:
        # Опечатка в выборке не имеет права тихо превратиться в «как всегда»:
        # человек читал бы ворота, думая, что смотрит на весь корпус.
        typer.echo(
            f"--scope: неизвестная выборка «{scope}»; допустимы: {', '.join(_GOLDEN_SCOPES)}"
        )
        raise typer.Exit(code=2)
    if scope != _GOLDEN_SCOPE_ACCEPTED and not golden:
        typer.echo("--scope действует только с --golden")
        raise typer.Exit(code=2)
    if rules_text and not dry_run:
        # Политика с консоли — для примерки. Боевой догон под правилом, которого
        # нет в настройке, разошёлся бы с живым путём (класс dva-puti-raznyi-schet)
        # и обошёл бы журнал `settings.address_detect_changed`.
        typer.echo("--rules действует только в сухом прогоне: включайте правило настройкой")
        raise typer.Exit(code=2)
    since = datetime.now(UTC) - timedelta(days=days)
    # Политика правил разбора — один раз на прогон (контракт 6.0а §5.2): без
    # неё `разбор_реплики_сейчас` читал бы настройку на каждую строку, а вне
    # `one_pass` это SELECT на строку — тысячи лишних запросов в догоне. В
    # `one_pass` прогон не заворачиваем: снимок рассчитан на миллисекунды
    # одной записи, а догон длится минуты (ревью 20.09, #13).
    rules = await inbound.parse_rules(db)
    if rules_text:
        # ПОВЕРХ настройки, не вместо: `rules_from_setting` отдаёт реестр
        # целиком, и словарь из одной строки консоли стёр бы перекрытия
        # настройки у остальных правил. Пары идут по порядку, поздняя
        # побеждает — прочитанная политика первой, консоль второй; чужое имя
        # или значение — та же строгая проверка, что у ручки настроек.
        try:
            rules = address_parse.rules_from_setting(
                ",".join([*(f"{k}={v}" for k, v in rules.items()), rules_text]), strict=True
            )
        except ValueError as e:
            typer.echo(f"--rules: {e}")
            raise typer.Exit(code=2) from None
    if golden:
        await _golden(db, since=since, rules=rules, scope=scope)
        return
    rows = (
        await db.execute(
            select(ClientAddressCandidate, Message, Conversation)
            .join(Message, Message.id == ClientAddressCandidate.message_id)
            .join(Conversation, Conversation.id == ClientAddressCandidate.conversation_id)
            .where(
                ClientAddressCandidate.status == "pending",
                ClientAddressCandidate.kind == "house",
                ClientAddressCandidate.detected_at >= since,
            )
        )
    ).all()
    итог = {"строк": len(rows), "дописано": 0, "другой_ключ": 0, "отклонено": 0, "уровень": 0}
    источники = set(
        (
            await db.execute(
                select(Client.address_candidate_id).where(Client.address_candidate_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    for row, сообщение, conv in rows:
        when = row.message_at or row.detected_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        # Анкета Авито — только её поля с адресом, как на живом пути (15.09);
        # речь, не тело: строка от голосового судится по расшифровке (19.09).
        body = address_parse.address_text(inbound.client_speech(сообщение).text)
        # Разбор нынешними правилами — тем же порядком, что живой путь и
        # сторож автозаписи (`inbound.разбор_реплики_сейчас`, 19.09): «86-11»
        # в Ангарске — адрес по правилу квартальных городов (ревью 14.09),
        # пара по городу другого диалога клиента — тоже.
        found = await inbound.разбор_реплики_сейчас(
            db, conv, сообщение, client_id=row.client_id, rules=rules
        )
        if found is None:
            # НОВЫЙ РАЗБОР В РЕПЛИКЕ АДРЕСА НЕ ВИДИТ (стенд 14.09: «сделать 130»,
            # «работает 1», «спальное место 200» — старый разбор брал речь
            # головой, карта отказала, а оператор видит «карта не подтвердила»).
            # Строку не удаляем — отклоняем: карте она уже не нужна, человеку
            # тоже; источник карточки и подтверждённые не трогаем. «Реплика
            # без адреса» — один предикат с автозаписью
            # (`inbound.реплика_без_адреса`): дом из двух реплик («дом 9»),
            # геоточка и пустое тело речью не считаются.
            if (
                row.geo_status in _ОТКАЗЫ_КАРТЫ
                and row.id not in источники
                and row.source != CANDIDATE_SOURCE_LLM
                and inbound.реплика_без_адреса(сообщение, found)
            ):
                место_реплики = address_parse.parse_place(body, rules=rules)
                # «Ангарск 12 микрорайон дом квартира 58» (владелец 14.09):
                # старый разбор дал дом «Ангарск, 12», новый видит место —
                # дом отклоняем, место заводим (повтор по ключу безвреден).
                if место_реплики is not None and row.kind != address_parse.KIND_HOUSE:
                    continue
                итог["отклонено"] += 1
                if not dry_run:
                    row.status = "rejected"
                    row.resolved_at = datetime.now(UTC)
                    # Строка выбыла из пересуда — снимок улик ей не нужен (пакет 5).
                    row.geo_prev = None
                    if место_реплики is not None:
                        # Только если такого места у карточки ещё нет: запись
                        # «к последнему упоминанию» перенесла бы живую строку
                        # места в старую реплику и перебила бы её части
                        # (ревью 14.09).
                        есть = (
                            await db.execute(
                                select(ClientAddressCandidate.id).where(
                                    ClientAddressCandidate.client_id == row.client_id,
                                    ClientAddressCandidate.value == место_реплики.value,
                                    ClientAddressCandidate.status != "rejected",
                                )
                            )
                        ).first()
                        client = None if есть else await db.get(Client, row.client_id)
                        if client is not None:
                            await clients_svc.record_address_candidate(
                                db,
                                client=client,
                                conversation_id=row.conversation_id,
                                message_id=row.message_id,
                                message_at=row.message_at or row.detected_at,
                                found=место_реплики,
                                now=when,
                                overwrite_parts=False,
                            )
            continue
        if found.value == row.value and found.level != row.level and row.level != "C":
            # Уровень — по новому разбору: «ориентировочный диаметр 15» был A из-за
            # слова «квартира» рядом, новый разбор такой головы не даёт вовсе
            # (выше), а «Садовая 13» без признака — C, не A.
            итог["уровень"] += 1
            if not dry_run:
                row.level = found.level
        if (
            found.kind == address_parse.KIND_HOUSE
            and found.settlement is None
            and found.area is None
            and not found.locality
        ):
            # Место из прошлой реплики того же диалога — в запрос дома
            # («СНТ Берёзка» → «Садовый проезд 4», бой 13.09).
            место = await clients_svc.latest_address_candidate(
                db,
                client_id=row.client_id,
                conversation_id=row.conversation_id,
                since=when - timedelta(hours=24),
            )
            if (
                место is not None
                and место.kind == address_parse.KIND_PLACE
                and место.id != row.id
                and (место.settlement or место.area)
            ):
                found = inbound.с_пунктом_места(found, место) or found
        if found.value != row.value:
            # Новый разбор выделил пункт из «улицы» («Д. Сорокино улица
            # Полевая, 37» → «улица Полевая, 37» + деревня Сорокино):
            # нерешённой строке ключ можно сменить, если такого у карточки нет.
            занят = (
                await db.execute(
                    select(sa.func.count())
                    .select_from(ClientAddressCandidate)
                    .where(
                        ClientAddressCandidate.client_id == row.client_id,
                        ClientAddressCandidate.value == found.value,
                    )
                )
            ).scalar_one()
            # Дом менять нельзя — кроме строки, которой карта уже отказала:
            # «днт Ромашка, 5» → «5 ул, 147» (бой 13.09, Бурятия) — у отказа
            # терять нечего, а новый разбор проверит та же карта.
            if занят or (found.house != row.house and row.geo_status not in _ОТКАЗЫ_КАРТЫ):
                итог["другой_ключ"] += 1
                continue
            итог["ключ_сменён"] = итог.get("ключ_сменён", 0) + 1
            if not dry_run:
                row.value, row.street, row.house = found.value, found.street, found.house
                row.settlement, row.settlement_type = found.settlement, found.settlement_type
                row.locality, row.district = found.locality, found.district
                clients_svc.сбросить_вердикт(row, reason="reparse")
            continue
        новое = {
            "settlement": found.settlement if not row.settlement else None,
            "settlement_type": found.settlement_type if not row.settlement else None,
            "locality": found.locality if not row.locality else None,
            "district": found.district if not row.district else None,
        }
        if not any(v for v in новое.values()):
            continue
        итог["дописано"] += 1
        if dry_run:
            continue
        for k, v in новое.items():
            if v:
                setattr(row, k, v)
        clients_svc.сбросить_вердикт(row, reason="reparse")
    if not dry_run:
        # Основной обход — своей транзакцией, ДО `--auto`: тот пишет в `clients`
        # и коммитит пачками, и правки нерешённых строк не должны ни ждать его
        # первой пачки, ни уезжать в базу вместе с ней (ревью 19.09, C8).
        await db.commit()
    if auto:
        итог.update(
            await _reparse_auto_accepted(db, since=since, dry_run=dry_run, redis=redis, rules=rules)
        )
    typer.echo(" ".join(f"{k}={v}" for k, v in итог.items()) + f" сухой_прогон={dry_run}")


#: Порция `address-reparse --golden`: строки грузятся и откатываются по столько.
#: Откат протухает всё загруженное в сессии, и второй проход по старым объектам
#: падал бы MissingGreenlet (как у `run_backfill_cards`, 19.09) — поэтому
#: сначала одни ключи, объекты — порциями заново.
_ПОРЦИЯ_GOLDEN = 200
#: Сколько расхождений золотого корпуса печатать масками.
_ПРИМЕРОВ_GOLDEN = 10
#: Уровень строки по убыванию уверенности — для счёта «уровень_упал».
_РАНГ_УРОВНЯ = {"A": 0, "B": 1, "C": 2}


async def _golden(
    db: AsyncSession,
    *,
    since: datetime,
    rules: dict[str, str],
    scope: str = _GOLDEN_SCOPE_ACCEPTED,
) -> None:
    """`address-reparse --golden` (программа §1.2 I-6): золотой корпус боя.

    Принятые строки-дома A/B за окно — и автоматикой, и человеком (обе группы
    считаются отдельно: `авто`/`руками`), кроме строк модели-читателя —
    перечитываются нынешним разбором (`inbound.разбор_реплики_сейчас`, тем
    же путём, что живой приём и догон) под политикой `rules`. Счёт:
    `строки_нет` — разбор в реплике адреса больше не видит; `ключ_изменился`
    — ключ «улица, дом» другой. Сравнение ключа СЕМАНТИЧЕСКОЕ
    (`address_parse.same_address`, как при рождении строки в
    `clients._та_же_строка`): строка могла быть склеена в существующую под
    другим написанием («Ленина, 5» ↔ «ул Ленина, 5»), и строковое `value`
    законно расходится с разбором собственной реплики. `уровень_упал` —
    уверенность ниже прежней (A→B→C) — отдельно, в ключ не входит.
    `не_судимо` — разбор молчит, но реплика и не была речью об адресе
    (`inbound.реплика_без_адреса`: геоточка Авито, «дом 9» к месту прошлой
    реплики, вложение без речи) — строка родилась не из разбора одной
    реплики, и «больше не видит» к ней неприменимо; в ворота не входит.

    Ничего не пишет: откат порциями (`_ПОРЦИЯ_GOLDEN`). Печатает ВСЕГДА все
    ключи, и нули тоже: шаг выкатки читает `0/0` глазами и грепом. Примеры
    масками до `_ПРИМЕРОВ_GOLDEN`: сначала фатальные (смена ключа, строки
    нет), уровень — в остаток. Смена ключа или `строки_нет` — код выхода 1:
    правило разбора не включается, пока корпус не молчит (§0.3 «правило
    разбора»).

    `scope=all` (пакет 7a §B.2): выборка шире — все строки-дома за окно
    (`pending`, `accepted`, `rejected`; уровни A/B/C; не модель), но ворота и
    первая строка печати — те же принятые A/B, слово в слово. Сверх них:
    `golden scope=all строк=N` и по каждой паре статус/уровень
    `строк[status/level]=N снято[…]=N ключ_изменился[…]=N` (два последних —
    только ненулевые; `снято` — тот же предикат, что `строки_нет`), затем до
    `_ПРИМЕРОВ_GOLDEN` снятых ВНЕ ворот масками — отдельным списком от
    фатальных. Смысл: сколько мусора (`pending`/`rejected`) снимет стоп-класс
    против того, сколько принятых он потеряет; код выхода от строк вне ворот
    не зависит.
    """
    from app.models import ClientAddressCandidate
    from app.models.client import (
        CANDIDATE_ACCEPTED,
        CANDIDATE_PENDING,
        CANDIDATE_REJECTED,
        CANDIDATE_SOURCE_LLM,
    )
    from app.services import address_parse, inbound

    a = ClientAddressCandidate
    ворота_уровни = (address_parse.LEVEL_A, address_parse.LEVEL_B)
    if scope == _GOLDEN_SCOPE_ALL:
        статусы: tuple[str, ...] = (CANDIDATE_PENDING, CANDIDATE_ACCEPTED, CANDIDATE_REJECTED)
        уровни: tuple[str, ...] = (*ворота_уровни, address_parse.LEVEL_C)
    else:
        статусы, уровни = (CANDIDATE_ACCEPTED,), ворота_уровни

    def _в_воротах(status: str, level: str) -> bool:
        return status == CANDIDATE_ACCEPTED and level in ворота_уровни

    # Статус и уровень — уже здесь, а не из объекта порции: `строк` и знаменатели
    # групп считаются по ключам, как всегда считался `строк` (строка, чью реплику
    # соединение не нашло, остаётся в счёте, а не пропадает молча).
    ключи: list[tuple[uuid.UUID, str, str]] = [
        (row_id, status, level)
        for row_id, status, level in (
            await db.execute(
                select(a.id, a.status, a.level)
                .where(
                    a.status.in_(статусы),
                    a.kind == address_parse.KIND_HOUSE,
                    a.level.in_(уровни),
                    a.source != CANDIDATE_SOURCE_LLM,
                    a.detected_at >= since,
                )
                .order_by(a.detected_at.asc(), a.id)
            )
        ).all()
    ]
    итог = {
        "строк": sum(1 for _, status, level in ключи if _в_воротах(status, level)),
        "авто": 0,
        "руками": 0,
        "ключ_изменился": 0,
        "строки_нет": 0,
        "уровень_упал": 0,
        "не_судимо": 0,
    }
    группы: dict[tuple[str, str], dict[str, int]] = {}
    for _, status, level in ключи:
        группа = группы.setdefault((status, level), {"строк": 0, "снято": 0, "ключ_изменился": 0})
        группа["строк"] += 1
    # Два списка (ревью 21.09, #14): падение уровня в бою массово и невинно
    # (`clients` поднимает уровень строки уверенной репликой, `message_id`
    # остаётся на первой), и в порядке `detected_at` оно забирало бы все слоты
    # печати у смены ключа — единственного, ради чего ворота падают.
    фатальные: list[str] = []
    уровень: list[str] = []
    # Снятые вне ворот (`scope=all`) — своим списком: это мусор, который класс
    # и должен снять, и он не смеет вытеснять из печати то, ради чего ворота падают.
    вне_ворот: list[str] = []

    def _пример(куда: list[str], row: ClientAddressCandidate, стало: str) -> None:
        if len(куда) < _ПРИМЕРОВ_GOLDEN:
            куда.append(f"{_маска_адреса(row.value)} → {стало}")

    for start in range(0, len(ключи), _ПОРЦИЯ_GOLDEN):
        порция = [row_id for row_id, _, _ in ключи[start : start + _ПОРЦИЯ_GOLDEN]]
        rows = (
            await db.execute(
                select(a, Message, Conversation)
                .join(Message, Message.id == a.message_id)
                .join(Conversation, Conversation.id == a.conversation_id)
                .where(a.id.in_(порция))
                .order_by(a.detected_at.asc(), a.id)
            )
        ).all()
        for row, сообщение, conv in rows:
            в_воротах = _в_воротах(row.status, row.level)
            группа = группы[(row.status, row.level)]
            if в_воротах:
                итог["авто" if row.resolved_by_id is None else "руками"] += 1
            found = await inbound.разбор_реплики_сейчас(
                db, conv, сообщение, client_id=row.client_id, rules=rules
            )
            if found is None:
                # Единственный предикат «реплика без адреса» — тот же, что у
                # догона `--auto` и сторожа автозаписи (ревью 21.09, #13):
                # геоточку Авито, «дом 9» к месту прошлой реплики и вложение
                # без речи разбор одной реплики не видел никогда — это не
                # «больше не видит», судить нечего.
                if not inbound.реплика_без_адреса(сообщение, found):
                    if в_воротах:
                        итог["не_судимо"] += 1
                    continue
                группа["снято"] += 1
                if в_воротах:
                    итог["строки_нет"] += 1
                    _пример(фатальные, row, "—")
                elif len(вне_ворот) < _ПРИМЕРОВ_GOLDEN:
                    вне_ворот.append(f"снято[{row.status}/{row.level}] {_маска_адреса(row.value)}")
                continue
            # Равные строки — тот же адрес и без ядра улицы («посёлок
            # Сосново, 9»: у дома без улицы `same_address` сравнивать нечего).
            тот_же = found.value == row.value or address_parse.same_address(
                row.street, row.house, found.street, found.house
            )
            if not тот_же:
                группа["ключ_изменился"] += 1
            if not в_воротах:
                continue
            if not тот_же:
                итог["ключ_изменился"] += 1
                _пример(фатальные, row, _маска_адреса(found.value))
            if _РАНГ_УРОВНЯ.get(found.level, 2) > _РАНГ_УРОВНЯ.get(row.level, 2):
                итог["уровень_упал"] += 1
                if тот_же:
                    _пример(
                        уровень,
                        row,
                        f"{_маска_адреса(found.value)} (уровень {row.level}→{found.level})",
                    )
        # Разбор ходит в базу (вопрос оператора, города других диалогов) и
        # копит объекты в сессии; своих правок нет — откат только чистит её.
        await db.rollback()
    typer.echo("golden " + " ".join(f"{k}={v}" for k, v in итог.items()) + " сухой_прогон=True")
    for строка in (фатальные + уровень)[:_ПРИМЕРОВ_GOLDEN]:
        typer.echo(f"  {строка}")
    if scope == _GOLDEN_SCOPE_ALL:
        typer.echo(f"golden scope=all строк={len(ключи)}")
        for (status, level), группа in sorted(группы.items()):
            метка = f"[{status}/{level}]"
            typer.echo(
                "  " + " ".join(f"{k}{метка}={v}" for k, v in группа.items() if v or k == "строк")
            )
        for строка in вне_ворот:
            typer.echo(f"  {строка}")
    if итог["ключ_изменился"] + итог["строки_нет"] > 0:
        raise typer.Exit(code=1)


#: Пачка `address-reparse --auto`: commit каждые столько отклонённых строк.
#: Замок строки `clients` живёт до commit'а, а живой приём и ручка карточки
#: пишут в те же строки (statement_timeout ручки 15 с): одна транзакция на
#: все автопринятые строки за 60 дней (~12 тыс. по замеру боя 5 928/30 дн)
#: держала бы карточки минуты (ревью 19.09, C8).
_ПАЧКА_АВТО = 200


async def _reparse_auto_accepted(
    db: AsyncSession,
    *,
    since: datetime,
    dry_run: bool,
    redis: Any | None,
    rules: dict[str, str] | None = None,
) -> dict[str, int]:
    """`address-reparse --auto`: строки, принятые АВТОМАТИКОЙ (`accepted`,
    `resolved_by_id` пуст), чья реплика по нынешнему разбору без адреса
    (`inbound.реплика_без_адреса`) — в отклонённые. Строка держит карточку —
    автозапись снимается (поле пустеет, связь со строкой рвётся, в журнале
    причина `speech_by_current_parse`), и карточку заново собирает ВОРКЕР
    автозаписи (`autofill_address`) из следующей годной строки
    клиента по своим же правилам: задача ставится на каждый диалог клиента, где
    есть нерешённая строка со степенью, — тем же `enqueue_autofill`, что у
    живого пути и у `address-autofill-backlog`. Своего выбора «следующей
    строки» здесь нет намеренно (контракт 18.09: судья степени один).

    Принятые человеком (`resolved_by_id`) и адрес, набранный руками
    (`address_set_at`), не трогаются: это слова человека. Строки модели-
    читателя (`llm`) — тоже: их адрес прочитан там, где правила молчали.

    Снятие автозаписи — УСЛОВНЫЙ UPDATE, как у `autofill_address`: гонку с
    человеком решает база, а не порядок чтения. Коммит — пачками
    (`_ПАЧКА_АВТО`); кадр `client:updated` на каждую снятую карточку и задачи
    воркеру — после commit'а своей пачки. Возвращает счётчики для строки
    итога: `авто_карточек` — сколько автозаписей СНЯТО (в сухом прогоне —
    сколько строк держит карточку по прочитанному). `rules` — политика правил
    разбора на весь прогон (даёт `run_address_reparse`); без неё читается
    здесь один раз, а не на каждую строку.
    """
    from app.models import Client, ClientAddressCandidate
    from app.models.client import CANDIDATE_SOURCE_LLM
    from app.services import clients as clients_svc
    from app.services import inbound
    from app.services.audit import write_audit
    from app.services.clients_events import publish_client_updated
    from app.services.geocode_queue import enqueue_autofill

    if rules is None:
        rules = await inbound.parse_rules(db)
    rows = (
        await db.execute(
            select(ClientAddressCandidate, Message, Conversation)
            .join(Message, Message.id == ClientAddressCandidate.message_id)
            .join(Conversation, Conversation.id == ClientAddressCandidate.conversation_id)
            .where(
                ClientAddressCandidate.status == "accepted",
                ClientAddressCandidate.resolved_by_id.is_(None),
                ClientAddressCandidate.kind == "house",
                ClientAddressCandidate.source != CANDIDATE_SOURCE_LLM,
                ClientAddressCandidate.detected_at >= since,
            )
        )
    ).all()
    итог = {"авто_строк": len(rows), "авто_отклонено": 0, "авто_карточек": 0}
    # Снятые карточки (клиент, диалог-источник) и диалоги под пересборку — ТЕКУЩЕЙ
    # пачки: и кадр, и задача уходят только после её commit'а.
    кадры: list[tuple[uuid.UUID, uuid.UUID]] = []
    пересобрать: list[uuid.UUID] = []
    поставлено: set[uuid.UUID] = set()
    now = datetime.now(UTC)

    async def _закрыть_пачку() -> None:
        # Кадры и задачи — СТРОГО ПОСЛЕ commit'а (08 §8.1): кадр до него заставил
        # бы экран перечитать карточку ещё с речью; воркер, прибежавший раньше,
        # увидел бы её ещё занятой отклоняемой строкой (окно тишины 90 с это
        # скрывает, но полагаться на него нельзя). Redis — только здесь: сухой
        # прогон пачек не закрывает, и клиент ему не нужен.
        await db.commit()
        очередь = redis if redis is not None else redis_mod.get_client()
        for client_id, conversation_id in кадры:
            await publish_client_updated(
                очередь, client_id, conversation_id, reason="address_reparsed"
            )
        кадры.clear()
        for cid in пересобрать:
            await enqueue_autofill(очередь, cid)
            поставлено.add(cid)
        пересобрать.clear()

    async def _снять_автозапись(row: ClientAddressCandidate, client: Client) -> bool:
        # Условная запись: между чтением карточки и этой строкой оператор мог
        # вписать адрес руками (`address_set_at`), а воркер — переставить
        # карточку на другую строку; тогда rowcount 0 — без журнала и кадра.
        previous = client.address
        result = await db.execute(
            sa.update(Client)
            .where(
                Client.id == client.id,
                Client.address_candidate_id == row.id,
                Client.address_set_at.is_(None),
            )
            .values(
                address=None,
                address_candidate_id=None,
                address_value=None,
                address_conversation_id=None,
                address_set_by_id=None,
            )
        )
        if not getattr(result, "rowcount", 0):
            return False
        await write_audit(
            db,
            user_id=None,
            action="client.address_edited",
            entity="client",
            entity_id=str(client.id),
            details={
                "source": "reparse",
                "reason": "speech_by_current_parse",
                "previous": previous,
                "address": None,
                "conversation_id": str(row.conversation_id),
                "candidate_id": str(row.id),
            },
        )
        кадры.append((client.id, row.conversation_id))
        # Следующую годную строку выбирает воркер автозаписи — по каждому
        # диалогу клиента, где есть нерешённая строка со степенью.
        # Присоединённые карточки не в счёт: их диалоги воркер и так пропустит
        # (`merged_into_id`), а карточка-победитель читает их строки сама.
        свои = await clients_svc.address_candidates(
            db, client.id, only_pending=True, include_merged=False
        )
        for r in свои:
            if (
                clients_svc.candidate_grade(r) is not None
                and r.conversation_id not in пересобрать
                and r.conversation_id not in поставлено
            ):
                пересобрать.append(r.conversation_id)
        return True

    в_пачке = 0
    for row, сообщение, conv in rows:
        found = await inbound.разбор_реплики_сейчас(
            db, conv, сообщение, client_id=row.client_id, rules=rules
        )
        if not inbound.реплика_без_адреса(сообщение, found):
            continue
        итог["авто_отклонено"] += 1
        client = await db.get(Client, row.client_id)
        держит_карточку = (
            client is not None
            and client.address_candidate_id == row.id
            and client.address_set_at is None
        )
        if dry_run:
            # Прогноз по прочитанному: сухой прогон не шлёт ни одного UPDATE.
            if держит_карточку:
                итог["авто_карточек"] += 1
            continue
        row.status = "rejected"
        row.resolved_at = now
        if client is not None and держит_карточку and await _снять_автозапись(row, client):
            итог["авто_карточек"] += 1
        в_пачке += 1
        if в_пачке >= _ПАЧКА_АВТО:
            await _закрыть_пачку()
            в_пачке = 0
    if dry_run:
        return итог
    if в_пачке:
        await _закрыть_пачку()
    if поставлено:
        итог["авто_поставлено"] = len(поставлено)
    return итог


@app.command("address-autofill-backlog")
def address_autofill_backlog(
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не ставить"),
) -> None:
    """Поставить автозапись для карточек, которым есть что взять по степени.

    Автозапись живёт на пути «воркер вынес вердикт» и раньше брала только
    строки моложе суток; 13.09 правило для пустой карточки снято, 18.09 у
    строк появились степени (точка, приблизительная точка, строка улицы) —
    накопленное само в очередь не встанет, эта команда ставит его разом.
    Решает дальше сам воркер (`autofill_address`): команда только ставит
    задачи, тем же `enqueue_autofill`, что и живой путь.
    """
    _run(lambda db: run_address_autofill_backlog(db, dry_run=dry_run))


def _степень(row: Any) -> str | None:
    """Степень строки — только через единственного судью (`geocode.card_grade`)."""
    from app.services import geocode

    return geocode.card_grade(
        row.kind,
        row.geo_status,
        row.geo_provider,
        row.geo_lat,
        row.geo_lon,
        row.geo_formatted,
    )


async def run_address_autofill_backlog(db: AsyncSession, *, dry_run: bool) -> None:
    from app.core import redis as redis_mod
    from app.services import address_catchup
    from app.services.geocode_queue import enqueue_autofill

    # Выбор диалогов — один на команду и на догон после включения автозаписи
    # с экрана (`address_catchup.autofill_backlog`, проверка 24.09).
    backlog = await address_catchup.autofill_backlog(db)
    conversations = backlog.conversations
    typer.echo(
        f"диалогов со строкой по степени и пустой карточкой: {len(backlog.empty_cards)}; "
        f"карточку держит худшая степень, лучшая ждёт: {len(backlog.better_grade)}; "
        f"всего к постановке: {len(conversations)}; сухой прогон: {dry_run}"
    )
    if dry_run:
        return
    redis = redis_mod.get_client()
    for cid in conversations:
        await enqueue_autofill(redis, cid)
    # `enqueue_autofill` молчит при сбое Redis (пишет `geocode.autofill_enqueue_failed`):
    # это число попыток, не подтверждений очереди.
    typer.echo(f"поставлено (без подтверждения от очереди): {len(conversations)}")


@app.command("address-dedup")
def address_dedup(
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не записывать"),
) -> None:
    """Свести дубли адреса в карточках: одно место — одна строка (13.09).

    До правки ключом была «улица как написана + дом», и «ул. Некрасова 6» с
    «Некрасова 6» жили двумя строками. Остаётся принятая, затем подтверждённая
    картой, затем с частями, затем ранняя; части остальных переезжают в неё,
    остальные удаляются. Отклонённые не трогаем.
    """
    _run(lambda db: run_address_dedup(db, dry_run=dry_run))


async def run_address_dedup(db: AsyncSession, *, dry_run: bool) -> None:
    from app.models import ClientAddressCandidate
    from app.services import clients as clients_svc
    from app.services.clients import _то_же_место

    rows = (
        await db.execute(
            select(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.status != "rejected",
                ClientAddressCandidate.kind == "house",
            )
            .order_by(ClientAddressCandidate.client_id, ClientAddressCandidate.detected_at)
        )
    ).scalars()
    по_клиентам: dict[uuid.UUID, list[ClientAddressCandidate]] = {}
    for row in rows:
        по_клиентам.setdefault(row.client_id, []).append(row)
    части = ("office", "entrance", "floor", "intercom")

    # Строки-источники карточек: их удалять нельзя — карточка потеряет цитату и
    # подпись «из диалога» (ревью 13.09).
    источники = set(
        (
            await db.execute(
                select(Client.address_candidate_id).where(Client.address_candidate_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    def вес(r: ClientAddressCandidate) -> tuple[int, int, int, int, float]:
        return (
            1 if r.id in источники else 0,
            1 if r.status == "accepted" else 0,
            1 if r.geo_status == "exact" else 0,
            sum(1 for ч in части if getattr(r, ч)),
            -r.detected_at.timestamp(),
        )

    итог = {"карточек": 0, "лишних": 0}
    for строки in по_клиентам.values():
        группы: list[list[ClientAddressCandidate]] = []
        for row in строки:
            for группа in группы:
                # Карта главнее слов: два подтверждённых разных дома — не дубль.
                if all(_то_же_место(row, r) for r in группа):
                    группа.append(row)
                    break
            else:
                группы.append([row])
        for группа in группы:
            if len(группа) < 2:
                continue
            итог["карточек"] += 1
            лучшая = max(группа, key=вес)
            # Части — хронологически по всей группе: позднее побеждает раннее.
            слитые: dict[str, str] = {}
            for r in sorted(группа, key=lambda r: r.detected_at):
                for ч in части:
                    if getattr(r, ч) is not None:
                        слитые[ч] = getattr(r, ч)
            for другая in группа:
                if другая is лучшая or другая.id in источники:
                    continue
                итог["лишних"] += 1
                if dry_run:
                    continue
                if другая.settlement and not лучшая.settlement:
                    лучшая.settlement, лучшая.settlement_type = (
                        другая.settlement,
                        другая.settlement_type,
                    )
                    if лучшая.geo_status != "exact":
                        clients_svc.сбросить_вердикт(лучшая, reason="dedup")
                await db.delete(другая)
            if not dry_run:
                for ч, з in слитые.items():
                    setattr(лучшая, ч, з)
    if not dry_run:
        await db.commit()
    typer.echo(
        f"карточек с дублями={итог['карточек']} лишних строк={итог['лишних']} "
        f"сухой_прогон={dry_run}"
    )


@app.command("backfill-cards")
def backfill_cards(
    days: int = typer.Option(60, help="Сколько дней переписки просмотреть"),
    dry_run: bool = typer.Option(True, help="Только посчитать, ничего не записывать"),
    match: str | None = typer.Option(
        None,
        "--match",
        help="Только реплики, чья речь подходит под регулярное выражение Postgres "
        "(`~`, регистр важен; `(?i)` в начале — без регистра)",
    ),
) -> None:
    """Догон карточек по накопленной переписке правилами 12.09.

    ЗАЧЕМ. Номера дополнительными (12.09) и адреса из переписки (09.09) работают
    только на новых сообщениях. Владелец 13.09: «не распознал адрес и 2-й номер
    из сообщения» — сообщение от 30.08, до обоих правил. Здесь те же правила
    применяются к прошлому:

    * номера и адреса — `inbound.replay_card_extraction`, слово в слово живой
      путь и задача N29 (историческая дверь, 19.09): сначала телефон
      (`_maybe_extract_phone` — межканальная проверка, выключатель
      `phone_detect.enabled`, первый годный в ПУСТОЙ основной, остальные
      дополнительными; заполненный основной не трогается, наши и 8-800 не
      пишутся, отклонённое человеком не возвращается), затем адрес
      (`_maybe_extract_address`: строки любого уровня, как и там — показ
      отбирает `ADDRESS_DETECT_LEVELS`, «дом N» к месту, части к строке, ответы
      на вопрос оператора, правила по городу, геоточки); проверку по карте
      ставит починка планировщика в своём темпе;
    * геоточки Авито с координатами — строка сразу `exact`, карта к ней не
      придёт: одна автозапись на диалог (`enqueue_autofill`), только в боевом
      прогоне и после commit'а.

    Боевой прогон коммитит после каждой реплики (замки строк `clients` не
    держатся дольше миллисекунд — живой приём пишет в те же строки), сухой
    откатывает порциями по 500.

    Объединение двойников по найденным номерам не ставится: его делает ночной
    проход по всем номерам. Печатает только числа: `телефон_записан` —
    сообщений, из которых номер лёг в карточку (основным или дополнительным).
    По умолчанию — сухой прогон: тот же путь, что живой, но ничего не
    записывается (откат вместо commit'а) и ничего не ставится.

    `--match` (21.09): каждая новая форма в разборе («7.10.34» в квартальном
    городе) требует догона по прошлому, а у реплики без найденного адреса нет
    строки — `address-reparse` её не увидит. Образец режет выборку в SQL по
    речи реплики (тело, иначе готовая расшифровка голосового); геоточки без
    текста под образец не попадают.
    """
    _run(
        lambda db: run_backfill_cards(
            db,
            days=days,
            dry_run=dry_run,
            match=match,
            # Redis — только боевому прогону: сухой откатывает порции, ставить
            # автозапись по строкам, которых после отката нет, — незачем.
            redis=None if dry_run else redis_mod.get_client(),
        )
    )


async def run_backfill_cards(
    db: AsyncSession,
    *,
    days: int,
    dry_run: bool,
    redis: Redis | None = None,
    match: str | None = None,
) -> None:
    from app.services import inbound, phone_parse, voice
    from app.services.geocode_queue import enqueue_autofill

    since = datetime.now(UTC) - timedelta(days=days)
    # Сначала — только ключи в хронологии, объекты — порциями по 500 заново:
    # откат сухого прогона «протухает» всё загруженное в сессии, и второй
    # проход по старым объектам падал MissingGreenlet (19.09, 30 дней — 5 000+
    # сообщений в одной сессии).
    #
    # ⚠ ПАРА (id, created_at), А НЕ ОДИН id (ревью 19.09, D2): `messages`
    # секционирована по дате с составным ключом; порция по одному `id IN (…)`
    # читала бы все секции за всю историю — с датой планировщик берёт только
    # свои (как в `workers/cards_catchup.replay_conversation`).
    пары: list[tuple[uuid.UUID, datetime]] = [
        (mid, at)
        for mid, at in (
            await db.execute(
                select(Message.id, Message.created_at)
                .where(
                    Message.direction == "in",
                    Message.sender_type == "client",
                    # Геоточка Авито приходит без тела — только вложением.
                    sa.or_(Message.body.is_not(None), Message.attachments != []),
                    Message.created_at >= since,
                    # Образец — по речи (`speech_sql`: тело, иначе готовая
                    # расшифровка), как и разбор ниже; без образца — условие
                    # пустое, выборка прежняя.
                    *([voice.speech_sql().regexp_match(match)] if match else []),
                )
                .order_by(Message.created_at.asc(), Message.id)
            )
        ).all()
    ]
    итог = {
        "сообщений": len(пары),
        "с_номером": 0,
        "телефон_записан": 0,
        "адресов": 0,
        "строк_на_карту": 0,
        "геоточек": 0,
    }
    карточки: dict[uuid.UUID, Client] = {}
    точки_порции: set[uuid.UUID] = set()

    async def _порция(
        часть: list[tuple[uuid.UUID, datetime]],
    ) -> list[tuple[Message, Conversation]]:
        rows = (
            await db.execute(
                select(Message, Conversation)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(sa.tuple_(Message.id, Message.created_at).in_(часть))
            )
        ).all()
        порядок = {mid: n for n, (mid, _) in enumerate(часть)}
        return sorted(((m, c) for m, c in rows), key=lambda mc: порядок[mc[0].id])

    ПОРЦИЯ = 500
    for start in range(0, len(пары), ПОРЦИЯ):
        for msg, conv in await _порция(пары[start : start + ПОРЦИЯ]):
            # Как живой путь (`now` = время сообщения): у строки догона `detected_at`
            # — время сообщения, иначе сторож автозаписи (сутки по detected_at)
            # счёл бы 60-дневный адрес свежим и записал его в карточку.
            when = msg.created_at if msg.created_at.tzinfo else msg.created_at.replace(tzinfo=UTC)
            # Сухой прогон идёт ТЕМ ЖЕ путём (ревью 15.09: своя копия разбора
            # считала 1 адрес там, где живой путь заводил 4) — пишет в сессию, а
            # в базу не попадает: откат вместо commit'а.
            client = карточки.get(conv.client_id) or await db.get(Client, conv.client_id)
            if client is None:
                continue
            карточки[conv.client_id] = client
            # Речь, не тело (19.09): у голосового номер лежит в расшифровке — и
            # обёртка ниже читает её же; счётчик обязан считать то, что разбирается.
            if phone_parse.find_all(inbound.client_speech(msg).text):
                итог["с_номером"] += 1
            # ОДИН ПУТЬ ДОГОНА (N29, 19.09): та же обёртка, что у задачи по вставленной
            # истории, — телефон (`_maybe_extract_phone`, с межканальной проверкой и
            # выключателем `phone_detect.enabled`), затем адрес; `now` = время сообщения.
            # Своя копия разбора (до 15.09 — адреса, до 19.09 — телефоны прямым
            # `absorb_phones`) расходилась с живым путём там, где он уже брал
            # (класс dva-puti-raznyi-schet). Хронология прогона — хронология
            # переписки, поэтому «позднее побеждает» держится само.
            # Полная форма ради `geocode_ids`: «адресов» считает и реплики, чья
            # строка уже есть, а решение о боевом прогоне принимается по числу
            # НОВЫХ строк, которые уйдут карте (сухой прогон 21.09: 147 536
            # сообщений, «адресов» 9 846 — сколько из них новых, было не видно).
            полный = await inbound.replay_card_extraction_full(db, conv, client, msg, now=when)
            разбор = полный.card
            итог["строк_на_карту"] += len(полный.geocode_ids)
            if not dry_run:
                # ОДНА РЕПЛИКА — ОДНА ТРАНЗАКЦИЯ (ревью 19.09, D1), как у задачи
                # догона: UPDATE clients (основной номер, адрес карточки) держит
                # замок строки клиента до конца транзакции, и одна транзакция на
                # 500 сообщений блокировала бы живой приём того же клиента на
                # секунды (у ручки карточки statement_timeout 15 с — ошибка
                # сохранения). Перечитывать conv/client после commit'а не надо:
                # фабрика — `expire_on_commit=False`.
                await db.commit()
            if разбор.phone_reason in ("phone_captured", "phone_extra_added"):
                итог["телефон_записан"] += 1
            if разбор.address_reason is not None:
                итог["адресов"] += 1
            if разбор.geopoint_ready:
                итог["геоточек"] += 1
                точки_порции.add(conv.id)
        if dry_run:
            # Сухой прогон откатывает порциями: 30 дней переписки в одной сессии
            # не удержать; счёт от этого чуть ниже живого (части и «дом N» к
            # строке из прошлой порции не находят). Следующая порция грузится
            # заново — откат протухает всё загруженное.
            await db.rollback()
        elif redis is not None:
            # Строки `exact` от точек Авито карта не возьмёт (`geo_repair` берёт только
            # непроверенные) — автозапись ставим сами, одну на диалог, после commit'а
            # (каждая реплика уже закоммичена): задача, поставленная раньше, прибежала
            # бы к строке, которой в базе нет.
            for cid in точки_порции:
                await enqueue_autofill(redis, cid)
        точки_порции.clear()
        карточки.clear()
    typer.echo(" ".join(f"{k}={v}" for k, v in итог.items()) + f" сухой_прогон={dry_run}")


@app.command("merge-auto")
def merge_auto_cmd(
    mode: str = typer.Option(..., help="off | shadow | on — как на экране настроек телефонов"),
) -> None:
    """Режим автообъединения двойников по телефону (`client_merge.auto`).

    То же, что переключатель «Объединять карточки-двойники» на экране
    настроек, но из консоли: с записью в журнал действий и тем же сбросом
    отсчёта автостопа (он считает откаты после ПОСЛЕДНЕГО изменения
    настройки). Владелец 13.09: «почему не объединилось» — режим на бою
    был «off» с самого появления.
    """
    _run(lambda db: run_merge_auto(db, mode=mode))


async def run_merge_auto(db: AsyncSession, *, mode: str) -> None:
    from app.services import app_settings
    from app.services.audit import write_audit

    if mode not in app_settings.MERGE_AUTO_MODES:
        typer.echo(
            f"Неизвестный режим: {mode}. Доступны: {', '.join(app_settings.MERGE_AUTO_MODES)}"
        )
        raise typer.Exit(code=2)
    было = await app_settings.get(db, app_settings.CLIENT_MERGE_AUTO)
    if было == mode:
        typer.echo(f"client_merge.auto уже «{mode}», ничего не меняю")
        return
    await app_settings.set_many(db, {app_settings.CLIENT_MERGE_AUTO: mode}, user_id=None)
    await write_audit(
        db,
        user_id=None,
        action="settings.phone_detect_changed",
        entity="settings",
        details={"before": {"merge_auto": было}, "after": {"merge_auto": mode}, "via": "cli"},
    )
    await db.commit()
    typer.echo(f"client_merge.auto: {было} → {mode}")


@app.command("merge-backlog")
def merge_backlog_cmd(
    limit: int = typer.Option(1000, help="Сколько номеров-двойников проверить"),
) -> None:
    """Сухой прогон правила объединения по всем парам-двойникам — числа по причинам.

    Ничего не пишет: каждая пара прогоняется через `client_merge.check_twins`,
    и печатается, сколько пар склеилось бы и сколько отпало по какой причине.
    Само объединение делает ночной проход планировщика при включённой
    настройке — этой командой оно не запускается намеренно.
    """
    _run(lambda db: run_merge_backlog(db, limit=limit))


async def run_merge_backlog(db: AsyncSession, *, limit: int) -> None:
    from collections import Counter

    from app.services import app_settings, client_merge, phone_rules

    own = phone_rules.parse_own_numbers(await app_settings.get(db, app_settings.PHONE_OWN_NUMBERS))
    номера = [
        r[0]
        for r in (
            await db.execute(
                select(Client.phone)
                .where(Client.phone.is_not(None), Client.merged_into_id.is_(None))
                .group_by(Client.phone)
                .having(func.count() >= 2)
                .order_by(Client.phone)
                .limit(limit)
            )
        ).all()
        if r[0]
    ]
    причины: Counter[str] = Counter()
    for phone in номера:
        verdict = await client_merge.check_twins(db, phone, own=own)
        причины["merge" if verdict.ok else verdict.reason] += 1
    await db.rollback()
    typer.echo(f"номеров с двумя и более живыми карточками: {len(номера)}")
    for reason, n in причины.most_common():
        typer.echo(f"  {reason}: {n}")


@app.command("address-limits")
def address_limits(
    llm: int | None = typer.Option(None, help="Потолок модели-читателя в сутки"),
    dadata: int | None = typer.Option(None, help="Потолок DaData в сутки"),
    yandex: int | None = typer.Option(None, help="Потолок Яндекс-геокодера в сутки"),
    suggest: int | None = typer.Option(None, help="Потолок Яндекс-Саджеста в сутки"),
    repair_share_yandex: int | None = typer.Option(
        None, help="Доля Яндекса и Саджеста для починки, процентов суточного потолка (0..100)"
    ),
) -> None:
    """Суточные потолки карт и модели — из консоли, с записью в журнал.

    В экране настроек правится только потолок Яндекса; DaData и модель
    задаются здесь (13.09: владелец пополнил OpenRouter до $10 — бесплатные
    модели дают 1 000 запросов в сутки вместо 50, потолок поднят до 900).
    `--repair-share-yandex` (пакет 5, 20.09) — процент потолка Яндекса и
    Саджеста, который получает починка (умолчание 30: «300 из 1 000»); 0 —
    как до пакета, 100 — починка может выесть потолок целиком. Без аргументов
    — показать текущие.
    """
    _run(
        lambda db: run_address_limits(
            db,
            llm=llm,
            dadata=dadata,
            yandex=yandex,
            suggest=suggest,
            repair_share_yandex=repair_share_yandex,
        )
    )


async def run_address_limits(
    db: AsyncSession,
    *,
    llm: int | None,
    dadata: int | None,
    yandex: int | None,
    suggest: int | None,
    repair_share_yandex: int | None = None,
) -> None:
    from app.services import app_settings
    from app.services.audit import write_audit

    ключи = {
        "llm": app_settings.ADDRESS_LLM_DAILY_LIMIT,
        "dadata": app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT,
        "yandex": app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT,
        "suggest": app_settings.ADDRESS_GEO_SUGGEST_DAILY_LIMIT,
        "repair_share_yandex": app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX,
    }
    новые = {
        имя: значение
        for имя, значение in (
            ("llm", llm),
            ("dadata", dadata),
            ("yandex", yandex),
            ("suggest", suggest),
            ("repair_share_yandex", repair_share_yandex),
        )
        if значение is not None
    }
    было = {имя: await app_settings.get(db, ключ) for имя, ключ in ключи.items()}
    if not новые:
        typer.echo(
            " ".join(
                f"{k}={v}%" if k == "repair_share_yandex" else f"{k}={v}" for k, v in было.items()
            )
        )
        return
    for имя, значение in новые.items():
        if значение < 0:
            typer.echo(f"{имя}: потолок не бывает отрицательным")
            raise typer.Exit(code=2)
    доля = новые.get("repair_share_yandex")
    if доля is not None and доля > app_settings.REPAIR_SHARE_MAX:
        typer.echo(f"repair_share_yandex: доля — проценты, 0..{app_settings.REPAIR_SHARE_MAX}")
        raise typer.Exit(code=2)
    if доля == app_settings.REPAIR_SHARE_MAX:
        typer.echo("repair_share_yandex=100: починка может выесть весь суточный потолок Яндекса")
    await app_settings.set_many(
        db, {ключи[имя]: значение for имя, значение in новые.items()}, user_id=None
    )
    await write_audit(
        db,
        user_id=None,
        action="settings.address_detect_changed",
        entity="settings",
        details={
            "before": {k: было[k] for k in новые},
            "after": новые,
            "via": "cli",
        },
    )
    await db.commit()
    typer.echo(" ".join(f"{k}: {было[k]} → {v}" for k, v in новые.items()))


#: Замки, которые сухой прогон проверяет сам (без задачи): печатаются и с нулём.
_ЗАМКИ_ПРОГОНА = (
    "client_blocked",
    "card_has_address",
    "candidate_exists",
    "geo_pending",
    "asked_in_feed",
    "declined",
    "not_described",
    "would_send",
)


@app.command("address-ask-dry-run")
def address_ask_dry_run(
    days: int = typer.Option(30, help="Диалоги, чьё первое входящее не старше N дней"),
    delay: int | None = typer.Option(None, help="Задержка, с; пусто — из настроек"),
    min_chars: int | None = typer.Option(None, help="Порог знаков; пусто — из настроек"),
    limit: int = typer.Option(5000, help="Потолок диалогов"),
    sample: int = typer.Option(40, help="Сколько «спросили бы» показать глазами"),
) -> None:
    """Кому система задала бы вопрос об адресе — на боевой переписке, без записи.

    Ворота включения `address_ask.enabled` (18.09): реплика клиенту от имени
    компании по счётчику букв не должна уйти, пока порог, стоп-список и
    задержка не посмотрены на боевом корпусе. Прогон зовёт ТЕ ЖЕ функции
    `services/address_ask.py`, что решают в бою, а не свой SQL — иначе два
    пути считали бы одно поле по-разному. Печатает распределение замков
    (в том числе `client_blocked` — сколько отправок ушло бы помеченным,
    и `geo_pending` — строка, которую карта ещё проверяла на момент T; в бою
    это самоповтор, на прожитой переписке статус строки — сегодняшний, и он
    считается замком), «спросили бы» с корзинами по тому, через сколько человек
    всё же взял диалог («пересечение» — два голоса), и выборку для глаз
    владельца — маской `_маска_адреса` (телефоны, ссылки, номера квартир и
    имя клиента скрыты).
    """

    async def main(db: AsyncSession) -> None:
        await run_address_ask_dry_run(
            db, days=days, delay=delay, min_chars=min_chars, limit=limit, sample=sample
        )

    _run(main)


async def run_address_ask_dry_run(
    db: AsyncSession,
    *,
    days: int,
    delay: int | None,
    min_chars: int | None,
    limit: int,
    sample: int,
) -> dict[str, Any]:
    """Тело сухого прогона; возвращает счётчики (для проверок), печатает отчёт."""
    import random
    from collections import Counter

    from app.services import address_ask, app_settings

    s = address_ask.settings_from(await app_settings.get_all(db))
    задержка = timedelta(seconds=delay if delay is not None else s.delay_sec)
    порог = min_chars if min_chars is not None else s.min_chars
    now = datetime.now(UTC)
    от = now - timedelta(days=days)
    # Кандидаты — диалоги с перепиской за окно (у диалога нет `created_at`).
    ids = list(
        (
            await db.execute(
                select(Conversation.id)
                .where(Conversation.last_message_at >= от)
                .order_by(Conversation.last_message_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    замки: Counter[str] = Counter()
    корзины: Counter[str] = Counter()
    #: та же корзина (180, 600] независимо от задержки — число для комментария Spec
    пересечение_180_600 = 0
    answered_in_time = 0
    без_первого = 0
    выборка: list[tuple[str, datetime, float | None, str]] = []

    def _utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    for conv_id in ids:
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            continue
        t0 = await db.scalar(
            select(func.min(Message.created_at)).where(
                Message.conversation_id == conv_id, Message.direction == "in"
            )
        )
        if t0 is None or _utc(t0) < от:
            без_первого += 1
            continue
        t0 = _utc(t0)
        # Первое «человек взял»: исходящее оператора/бота после T0 или принятие.
        первый_ответ = await db.scalar(
            select(func.min(Message.created_at)).where(
                Message.conversation_id == conv_id,
                Message.direction == "out",
                Message.sender_type.in_(("operator", "bot")),
                Message.delivery_status != "failed",
                Message.created_at > t0,
            )
        )
        # Серия открытия: входящие до первого ответа и не позже T0 + 10 мин.
        конец_серии_условия = [
            Message.conversation_id == conv_id,
            Message.direction == "in",
            Message.created_at <= t0 + timedelta(minutes=10),
        ]
        if первый_ответ is not None:
            конец_серии_условия.append(Message.created_at < первый_ответ)
        конец_серии = await db.scalar(
            select(func.max(Message.created_at)).where(*конец_серии_условия)
        )
        конец_серии = _utc(конец_серии) if конец_серии is not None else t0
        моменты = [_utc(первый_ответ)] if первый_ответ is not None else []
        for отметка in (conv.claimed_at, conv.auto_assigned_at):
            if отметка is not None and _utc(отметка) > t0:
                моменты.append(_utc(отметка))
        взял = (min(моменты) - конец_серии).total_seconds() if моменты else None
        if взял is not None and взял <= задержка.total_seconds():
            answered_in_time += 1
            continue
        t = конец_серии + задержка
        client = await db.get(Client, conv.client_id)
        замок = address_ask.card_lock(client)
        if замок is None:
            # Тот же предикат, что в задаче: степень глушит, ожидание карты —
            # здесь замок (повтора у прогона нет), отказ без степени — нет.
            замок = address_ask.candidate_lock(
                await address_ask.address_rows(
                    db,
                    client_id=conv.client_id,
                    conversation_id=conv_id,
                    since=t - address_ask.ОКНО,
                    before=t,
                )
            )
        if замок is None and await address_ask.asked_in_feed(db, conv_id, before=t):
            замок = "asked_in_feed"
        if замок is None:
            входящие = await address_ask.inbound_rows(
                db, conv_id, since=t - address_ask.ОКНО, before=t
            )
            if address_ask.client_declined(входящие):
                замок = "declined"
            else:
                описал, _ = address_ask.client_described(входящие, min_chars=порог)
                if not описал:
                    замок = "not_described"
        if замок is not None:
            замки[замок] += 1
            continue
        замки["would_send"] += 1
        if взял is None:
            корзины["never"] += 1
        elif взял <= задержка.total_seconds() + 420:
            корзины["overlap_7min"] += 1
        elif взял <= 1800:
            корзины["within_30min"] += 1
        else:
            корзины["later"] += 1
        if взял is not None and 180 < взял <= 600:
            пересечение_180_600 += 1
        последняя = await db.scalar(
            select(Message.body)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.created_at <= конец_серии,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        # Только маской: телефон, ссылка, номер квартиры и имя клиента наружу
        # не идут даже в терминал (та же маска, что у address-audit-sample).
        выборка.append(
            (
                str(conv_id),
                t0,
                взял,
                _маска_адреса(последняя, имя=client.name if client else None)[:120],
            )
        )

    всего = sum(замки.values())
    typer.echo(
        f"диалогов: {len(ids)}, без первого входящего в окне: {без_первого}, "
        f"человек ответил за задержку ({int(задержка.total_seconds())} с): {answered_in_time}"
    )
    typer.echo("замок → число → % среди не-answered:")
    # Нули тоже печатаются: «помеченных 0» владелец должен увидеть, а не искать.
    for имя, n in [*замки.most_common(), *((з, 0) for з in _ЗАМКИ_ПРОГОНА if з not in замки)]:
        typer.echo(f"  {имя}: {n} ({n * 100 // всего if всего else 0}%)")
    typer.echo(
        "would_send по тому, когда человек всё же взял: "
        + ", ".join(f"{k}={корзины[k]}" for k in ("overlap_7min", "within_30min", "later", "never"))
    )
    typer.echo(f"взяли в окне (180, 600] с — для комментария Spec: {пересечение_180_600}")
    if sample > 0 and выборка:
        # Только в терминал контейнера, для глаз владельца; в журнал не пишется.
        typer.echo(f"выборка would_send ({min(sample, len(выборка))} из {len(выборка)}):")
        for метка, t0, взял, текст in random.sample(выборка, min(sample, len(выборка))):
            typer.echo(f"  {метка} {t0:%d.%m %H:%M} взял={взял} | {текст}")
    return {
        "conversations": len(ids),
        "answered_in_time": answered_in_time,
        "locks": dict(замки),
        "buckets": dict(корзины),
        "overlap_180_600": пересечение_180_600,
    }


@app.command("address-funnel")
def address_funnel_cmd(
    week: str = typer.Option(
        "", help="Понедельник недели (YYYY-MM-DD, МСК); пусто — последняя полная"
    ),
    store: bool = typer.Option(False, help="Записать снимок недели (перезапишет существующий)"),
    history: int = typer.Option(0, help="Показать N последних сохранённых недель"),
    rules: bool = typer.Option(
        True, help="Печатать блок по правилам (накопительно за 8 недель до конца недели)"
    ),
) -> None:
    """Воронка адресов за неделю: сколько адресов из переписки дошло до карточки.

    Те же определения, что у понедельничной задачи планировщика и у строки
    монитора «Внешние сервисы» (`services/address_funnel`). Без `--store`
    только считает и печатает; с `--store` — кладёт снимок недели, чтобы
    следующий замер сравнивал с ним. После догона (docs/47) запускается
    руками: догон меняет карточки задним числом, а задача считает раз в
    неделю. Блок `by_rule` (пакет 6.0а) — по каждому правилу `trace.rule`
    накопительно за восемь недель: решений, осуждено судьёй, ложных, спорных,
    принято/отвергнуто руками, тень — те же числа, по которым задача
    `rule_policy_weekly` двигает политику правила.
    """
    _run(lambda db: run_address_funnel(db, week=week, store=store, history=history, rules=rules))


async def run_address_funnel(
    db: AsyncSession, *, week: str, store: bool, history: int = 0, rules: bool = False
) -> None:
    from app.services import address_funnel

    try:
        неделя = address_funnel.parse_week(week)
    except ValueError as exc:
        typer.echo(f"неделя: {exc}")
        raise typer.Exit(code=2) from exc
    since, until = address_funnel.week_bounds(неделя)
    counts = await address_funnel.measure(db, since=since, until=until)
    prev = await address_funnel.stored(db, неделя - address_funnel.WEEK)
    typer.echo(f"неделя с {неделя.isoformat()} ({since.isoformat()} → {until.isoformat()})")
    for имя, значение in counts.as_dict().items():
        typer.echo(f"{имя}\t{значение}")
    typer.echo(
        f"доля строк {address_funnel.share_pct(counts.with_row, counts.dialogs)} %, "
        f"доля карточек {address_funnel.share_pct(counts.card, counts.dialogs)} %"
        + (
            f" (неделей раньше {address_funnel.share_pct(prev.card, prev.dialogs)} %)"
            if prev
            else " (прошлой недели в базе нет)"
        )
    )
    причины = address_funnel.compare(prev, counts)
    typer.echo(f"тревога: {address_funnel.reason_words(причины)}" if причины else "тревоги нет")
    if rules:
        по_правилам = await address_funnel.measure_rules(db, until=until)
        typer.echo(
            f"by_rule за {address_funnel.RULE_WINDOW_WEEKS} нед. до {until.date().isoformat()}: "
            f"{len(по_правилам)} правил"
        )
        # Согласие судьи с человеком (пакет 6.0б): по нему лестница решает,
        # ворота ли судья для подъёма до exact (`JUDGE_MIN_AGREEMENT`).
        typer.echo(await _строка_согласия_судьи(db))
        if по_правилам:
            typer.echo(
                "правило\tn\tjudged\tfalse\tdisputed\tplace_swap\taccepted_by_hand\t"
                "rejected_by_hand\tedited_by_hand\tshadow_n\tfalse_upper_pct\tfirst_at"
            )
        for имя, rc in по_правилам.items():
            _, верх = address_funnel.wilson_bounds(rc.false_total, rc.signals)
            typer.echo(
                "\t".join(
                    [
                        имя,
                        str(rc.n),
                        str(rc.judged),
                        str(rc.false),
                        str(rc.disputed),
                        str(rc.place_swap),
                        str(rc.accepted_by_hand),
                        str(rc.rejected_by_hand),
                        str(rc.edited_by_hand),
                        str(rc.shadow_n),
                        f"{верх * 100:.1f}" if rc.signals else "—",
                        rc.first_at.isoformat() if rc.first_at else "",
                    ]
                )
            )
    if store:
        await address_funnel.store(
            db, week_start=неделя, counts=counts, computed_at=datetime.now(UTC)
        )
        # `_run` идёт через `session_scope`, а она на выходе откатывает.
        await db.commit()
        typer.echo("снимок недели записан")
    if history > 0:
        for снимок in await address_funnel.recent(db, weeks=history):
            c = снимок.counts
            слова = address_funnel.reason_words(address_funnel.compare(снимок.prev, c))
            typer.echo(
                f"{снимок.week_start.isoformat()}\tдиалогов {c.dialogs}\tстрока {c.with_row}\t"
                f"карточка {c.card}\tточных {c.card_auto_exact}\t≈ {c.card_auto_approx}\t"
                f"без точки {c.card_auto_text}\tруками {c.card_person}\t"
                f"без источника {c.card_unknown}\tправок {c.edited_after_auto}\t"
                f"вопросов {c.asked}\t{слова or '—'}"
            )
    log.info("address.funnel_cli", week=str(неделя), stored=store, **counts.as_dict())


#: Степени для `--degree` команды аудита; `all` — без фильтра. `suggest` —
#: не степень карточки, а предложения правил (хвост `~suggest`, пакет 6.0а):
#: строки ещё `pending`, в карточке их нет, показываем строку карты правила.
_СТЕПЕНИ_АУДИТА = ("all", "exact", "approx", "text", "suggest")
_СТЕПЕНЬ_ПРЕДЛОЖЕНИЯ = "suggest"
#: Ключ `trace` в `--trace k=v`: латиница, цифры, подчёркивание, точка для
#: вложенности (`shadow.rule`). Иначе — 2: опечатка молча дала бы пустую выборку.
_КЛЮЧ_СЛЕДА = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


def _фильтр_следа(столбец: Any, пары: list[str]) -> list[Any]:
    """`--trace k=v` → условия SQL по `trace[k].as_string() == v`.

    Не `@>` (JSONB-включение): стенд проверок — SQLite, а `as_string()` у
    SQLAlchemy компилируется и там (`JSON_EXTRACT`), и на PostgreSQL (`->>`).
    Вложенный ключ — через точку (`shadow.rule=street_point`); `true`/`false`
    сравниваются как булево (`suggest=true`), остальное — строкой. Пустые ключи
    суд не пишет (контракт 6.0а п. 2.1), так что «ключ есть» = «ключ равен».
    """
    условия: list[Any] = []
    for пара in пары:
        ключ, знак, значение = пара.partition("=")
        ключ, значение = ключ.strip(), значение.strip()
        if not знак or not значение or not _КЛЮЧ_СЛЕДА.match(ключ):
            typer.echo(f"--trace: ожидается «ключ=значение» латиницей, получено «{пара}»")
            raise typer.Exit(code=2)
        узел = столбец
        for часть in ключ.split("."):
            узел = узел[часть]
        if значение in ("true", "false"):
            условия.append(узел.as_boolean() == (значение == "true"))
        else:
            условия.append(узел.as_string() == значение)
    return условия


def _правило_следа(trace: Any) -> str:
    return str(trace.get("rule") or "") if isinstance(trace, dict) else ""


_АУДИТ_ПОТОЛОК = 500
_ЧАСТЬ = re.compile(r"(?i)(?<![а-яё])(кв|квартира|подъезд|под|этаж|эт|домофон|код)\.?\s*[\d#*]+")
#: Слово имени короче трёх букв («Ли») скрывать нельзя: вырезалось бы из
#: любого текста.
_МИН_СЛОВО_ИМЕНИ = 3


def _маска_адреса(text: str | None, *, имя: str | None = None) -> str:
    """Текст клиента для глаз владельца (сверка «улица-дом ↔ карточка»,
    выборка сухого прогона) без личного.

    Телефоны, ссылки, почта и длинные числа — крестиками через
    `address_llm.mask`: один разборщик номеров на проект (`phone_parse.find_all`
    — надстрочные цифры, номер словами), а не вторая регулярка рядом; номер
    квартиры, подъезда, этажа, домофона → слово и `**`; слова имени клиента из
    карточки → `[имя]`. Улица, дом и пункт остаются — иначе сверять нечего.
    """
    from app.services import address_llm

    if not text:
        return ""
    т = address_llm.mask(text)
    т = _ЧАСТЬ.sub(lambda m: f"{m.group(1)} **", т)
    for слово in (имя or "").split():
        if len(слово) >= _МИН_СЛОВО_ИМЕНИ:
            т = re.sub(rf"(?i)(?<!\w){re.escape(слово)}(?!\w)", "[имя]", т)
    return " ".join(т.split())


@app.command("address-audit-sample")
def address_audit_sample(
    days: int = typer.Option(7, help="Карточки, записанные автоматикой за столько дней"),
    limit: int = typer.Option(50, help=f"Сколько строк (не больше {_АУДИТ_ПОТОЛОК})"),
    degree: str = typer.Option(
        "all", help="all | exact | approx | text — степень точки; suggest — предложения правил"
    ),
    trace: list[str] = typer.Option(  # noqa: B008 — так задаётся повторяемая опция typer
        [], help="Фильтр по следу строки: ключ=значение (rule=street_point, shadow.rule=…)"
    ),
) -> None:
    """Выборка карточек «текст клиента ↔ адрес карточки» для проверки глазами.

    Случайные N карточек, которые за последние `days` дней записала автоматика
    (не руками и не кнопкой), со степенью каждой. Печатает TSV масками: без
    телефонов и номеров квартир. В журнал уходит только число строк. Фильтр
    `--degree` применяется ДО потолка N: степень судит `card_grade` в Python,
    поэтому строки читаются все за окно, а потолок режет уже отфильтрованные.
    `--degree suggest` (пакет 6.0а) — не карточки, а предложения правил (хвост
    `~suggest`): строка ещё ждёт нажатия оператора, вместо адреса карточки —
    строка карты, которую предложило правило. `--trace k=v` — по следу строки
    (`trace[k]`), можно несколько.
    """
    _run(
        lambda db: run_address_audit_sample(
            db, days=days, limit=limit, degree=degree, trace=list(trace)
        )
    )


async def run_address_audit_sample(
    db: AsyncSession, *, days: int, limit: int, degree: str, trace: list[str] | None = None
) -> None:
    from app.models import ClientAddressCandidate
    from app.models.client import CANDIDATE_PENDING
    from app.services import geocode

    if degree not in _СТЕПЕНИ_АУДИТА:
        typer.echo(f"степень: одна из {', '.join(_СТЕПЕНИ_АУДИТА)}")
        raise typer.Exit(code=2)
    limit = max(1, min(limit, _АУДИТ_ПОТОЛОК))
    a = ClientAddressCandidate
    с = datetime.now(UTC) - timedelta(days=days)
    предложения = degree == _СТЕПЕНЬ_ПРЕДЛОЖЕНИЯ
    if предложения:
        # Предложение в карточку не ходило: строка `pending`, без человека,
        # с хвостом `~suggest`; окно — по времени суда, `resolved_at` пуст.
        условия = [
            a.status == CANDIDATE_PENDING,
            a.resolved_by_id.is_(None),
            a.geo_provider.like(f"%{geocode.POINT_SUGGEST_SUFFIX}%"),
            a.geo_checked_at >= с,
            Client.merged_into_id.is_(None),
        ]
    else:
        условия = [
            Client.address.is_not(None),
            Client.address_set_at.is_(None),
            Client.merged_into_id.is_(None),
            a.resolved_by_id.is_(None),
            a.resolved_at >= с,
            a.id == Client.address_candidate_id,
        ]
        # Предфильтр по данным строки — только чтобы не тащить лишнее; степень
        # ниже всё равно судит `card_grade` (контракт п.3).
        if degree == "text":
            условия.append(a.geo_lat.is_(None))
        elif degree in ("exact", "approx"):
            условия.append(a.geo_lat.is_not(None))
    условия.extend(_фильтр_следа(a.trace, trace or []))
    rows = (
        await db.execute(
            select(
                a.conversation_id,
                a.kind,
                a.geo_status,
                a.geo_provider,
                a.geo_lat,
                a.geo_lon,
                a.geo_formatted,
                a.raw,
                a.resolved_at,
                a.geo_checked_at,
                a.trace,
                Client.address,
            )
            .join(Client, Client.id == a.client_id)
            .where(*условия)
            .order_by(sa.func.random())
        )
    ).all()
    # Степень — в Python (контракт п.3), значит и фильтр `--degree` здесь, ДО
    # потолка: с `LIMIT` в SQL «approx» из 50 случайных дал бы меньше 50 строк.
    строки: list[tuple[Any, str]] = []
    for r in rows:
        степень: str | None
        if предложения:
            if not geocode.point_is_suggest(r.geo_provider):
                continue
            степень = _СТЕПЕНЬ_ПРЕДЛОЖЕНИЯ
        else:
            степень = _степень(r)
            if степень is None or (degree != "all" and степень != degree):
                continue
        строки.append((r, степень))
        if len(строки) >= limit:
            break
    if not строки:
        typer.echo(
            f"предложений правил за {days} дн., нет"
            if предложения
            else f"карточек, записанных автоматикой за {days} дн., нет"
        )
        log.info("address.audit_sample", rows=0, days=days, degree=degree, trace=trace or [])
        return
    # Колонка правила — последней: прежние колонки читают сторожа и глаза.
    typer.echo(
        "№\tдиалог\tстепень\tвид\tкарта\tтекст клиента\t"
        + ("предложение правила" if предложения else "адрес карточки")
        + "\tзаписано\tправило"
    )
    for i, (r, степень) in enumerate(строки, start=1):
        когда = r.geo_checked_at if предложения else r.resolved_at
        typer.echo(
            "\t".join(
                [
                    str(i),
                    str(r.conversation_id),
                    степень,
                    r.kind,
                    r.geo_provider or "",
                    _маска_адреса(r.raw),
                    _маска_адреса(r.geo_formatted if предложения else r.address),
                    когда.isoformat() if когда else "",
                    _правило_следа(r.trace),
                ]
            )
        )
    log.info("address.audit_sample", rows=len(строки), days=days, degree=degree, trace=trace or [])


#: Причина в журнале `client.address_unfilled` и в кадре `client:updated`.
_ПРИЧИНА_СНЯТИЯ = "rule_unfilled"


@app.command("address-unfill")
def address_unfill(
    trace: list[str] = typer.Option(  # noqa: B008 — так задаётся повторяемая опция typer
        ..., help="Какие строки: ключ=значение по следу, обычно rule=<имя правила>"
    ),
    since: str = typer.Option("", help="Карточки, записанные с этой даты (YYYY-MM-DD, UTC)"),
    days: int = typer.Option(30, help="…или за столько последних дней (если --since пуст)"),
    dry_run: bool = typer.Option(True, help="Только посчитать; --no-dry-run применяет"),
) -> None:
    """Снять автоадреса, записанные правилом, которое ушло в `off` (пакет 6.0а, I-8).

    Автоматика чинит прошлое только этой командой (docs/47 §2: свой адрес она
    уточняет лишь лучшей степенью того же места) — задача `rule_policy_weekly`
    печатает её в уведомлении об откате правила. Берутся ТОЛЬКО карточки
    автоматики: `address_set_at IS NULL`, источник `address_candidate_id` — строка
    с подходящим следом (`trace.rule=X`), у строки `resolved_by_id IS NULL`.
    Набранное руками и подтверждённое кнопкой не трогается по построению.
    Карточка → пусто (условный UPDATE — гонку с человеком решает база), строка →
    `pending` и обратно в очередь карты с `trace.unfilled_at` (прежний суд — в
    `trace.prev`): её пересудит воркер уже под новой политикой, и если другой
    путь подтвердит дом — автозапись вернёт адрес сама; журнал
    `client.address_unfilled`, кадр `client:updated` — после commit'а пачки.
    Печатает число; без `--no-dry-run` ничего не пишет.
    """

    async def _тело(db: AsyncSession) -> None:
        await run_address_unfill(db, trace=list(trace), since=since, days=days, dry_run=dry_run)

    _run(_тело)


async def run_address_unfill(
    db: AsyncSession,
    *,
    trace: list[str],
    since: str = "",
    days: int = 30,
    dry_run: bool = True,
    redis: Any | None = None,
) -> int:
    """Тело `address-unfill`; возвращает число снятых карточек (в сухом прогоне
    — сколько подходит). Отдельно от команды — ради проверок."""
    from app.models import ClientAddressCandidate
    from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING
    from app.services import clients as clients_svc
    from app.services import geocode
    from app.services.clients_events import publish_client_updated

    if not trace:
        typer.echo("--trace обязателен: без фильтра команда сняла бы все автоадреса")
        raise typer.Exit(code=2)
    a = ClientAddressCandidate
    фильтр = _фильтр_следа(a.trace, trace)
    if since.strip():
        с = datetime.fromisoformat(since.strip())
        с = с.replace(tzinfo=UTC) if с.tzinfo is None else с
    else:
        с = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(a, Client)
            .join(Client, Client.id == a.client_id)
            .where(
                # Карточка автоматики — три условия docs/47 §2, все в WHERE.
                Client.address_candidate_id == a.id,
                Client.address_set_at.is_(None),
                Client.address.is_not(None),
                a.resolved_by_id.is_(None),
                a.status == CANDIDATE_ACCEPTED,
                a.resolved_at >= с,
                *фильтр,
            )
            .order_by(a.resolved_at)
        )
    ).all()
    typer.echo(f"карточек автоматики под {' '.join(trace)} с {с.date().isoformat()}: {len(rows)}")
    if dry_run or not rows:
        typer.echo(f"снято: 0 сухой_прогон={dry_run}")
        return len(rows)
    очередь = redis if redis is not None else redis_mod.get_client()
    now = datetime.now(UTC)
    снято = 0
    кадры: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _закрыть_пачку() -> None:
        # Кадр — СТРОГО после commit'а (08 §8.1): до него экран перечитал бы
        # карточку ещё с адресом.
        await db.commit()
        for client_id, conversation_id in кадры:
            await publish_client_updated(
                очередь, client_id, conversation_id, reason=_ПРИЧИНА_СНЯТИЯ
            )
        кадры.clear()

    for row, client in rows:
        previous = client.address
        result = await db.execute(
            sa.update(Client)
            .where(
                Client.id == client.id,
                Client.address_candidate_id == row.id,
                Client.address_set_at.is_(None),
            )
            .values(
                address=None,
                address_candidate_id=None,
                address_value=None,
                address_conversation_id=None,
                address_set_by_id=None,
            )
            .execution_options(synchronize_session=False)
        )
        if not getattr(result, "rowcount", 0):
            continue  # оператор успел раньше — ни журнала, ни кадра
        след = dict(
            geocode.trace_after_reset(row.trace if isinstance(row.trace, dict) else None) or {}
        )
        след["unfilled_at"] = now.isoformat()
        # Строка — обратно в кандидаты и в очередь карты: содержание то же, но
        # прежний вердикт вынесен правилом, которому больше не верят, —
        # снимка улик нет, попытки с нуля (пересуд полным набором карт).
        await db.execute(
            sa.update(a)
            .where(a.id == row.id, a.resolved_by_id.is_(None))
            .values(
                status=CANDIDATE_PENDING,
                resolved_at=None,
                **clients_svc.сброс_вердикта(с_попытками=False, улики=None, след=след),
            )
            .execution_options(synchronize_session=False)
        )
        await write_audit(
            db,
            user_id=None,
            action="client.address_unfilled",
            entity="client",
            entity_id=str(client.id),
            details={
                "source": "unfill",
                "reason": _ПРИЧИНА_СНЯТИЯ,
                "trace": trace,
                "rule": _правило_следа(row.trace) or None,
                "previous": previous,
                "address": None,
                "conversation_id": str(row.conversation_id),
                "candidate_id": str(row.id),
            },
        )
        кадры.append((client.id, row.conversation_id))
        снято += 1
        if len(кадры) >= _ПАЧКА_АВТО:
            await _закрыть_пачку()
    if кадры:
        await _закрыть_пачку()
    log.info("address.unfilled", rows=снято, trace=trace, since=с.isoformat())
    typer.echo(f"снято: {снято} сухой_прогон={dry_run}")
    return снято


# ── address-rule-dry-run (пакет 6.0б, I-3) ───────────────────────────────────

#: Потолок СТРОК одного сухого прогона — верхняя граница, не бюджет. Бюджет
#: считается ПОХОДАМИ (`--dadata-budget`, умолчание ниже): отказная строка
#: стоит DaData 3–8 запросов (город → круг → область; ×2 при дроби «/», +1–2
#: при пункте; замер ревью 21.09, #5: до ~14 при пункте с дробью), а не «до
#: двух»; бесплатен только повтор ключа в кэше (пустые ответы — 24 ч,
#: `geo_cache.TTL_EMPTY`). Строки сверх бюджета остаются несудимыми.
_СУХОЙ_ПОТОЛОК = 500
#: Бюджет СВОИХ походов DaData на прогон (программа §8.4: ≤ 500 на пакет).
#: Считается по копилке воркера (`DrySpill.dadata_requests`), а не по дельте
#: суточного ключа: тот общий с живым потоком и починкой. Останов — перед
#: строкой, перерасход не больше цены одной строки.
_СУХОЙ_БЮДЖЕТ_DADATA = 500
#: Сколько примеров показать глазами после счётчиков.
_СУХИХ_ПРИМЕРОВ = 10
#: Уровни строк разбора для `--levels` (`address_parse.LEVEL_*`).
_УРОВНИ = ("A", "B", "C")


@app.command("address-rule-dry-run")
def address_rule_dry_run(
    rule: str = typer.Option(
        ..., "--rule", help="Имя правила (RULE_*): его строки первыми, политика «как если бы exact»"
    ),
    policy: list[str] = typer.Option(  # noqa: B008 — так задаётся повторяемая опция typer
        [],
        "--policy",
        help="имя=off|shadow|suggest|approx|exact поверх боевой настройки; можно несколько",
    ),
    days: int = typer.Option(30, help="Строки, обнаруженные не раньше N дней назад"),
    statuses: str = typer.Option(
        "",
        help=(
            "geo_status через запятую (строгий фильтр); пусто — отказы карты по "
            "содержанию, неспокойные (ambiguous, elsewhere) и exact"
        ),
    ),
    levels: str = typer.Option("A,B", help="Уровни строк через запятую"),
    limit: int = typer.Option(100, help=f"Сколько строк судить (не больше {_СУХОЙ_ПОТОЛОК})"),
    dadata_budget: int = typer.Option(
        _СУХОЙ_БЮДЖЕТ_DADATA,
        "--dadata-budget",
        help="Потолок своих походов DaData за прогон; достигнут — остаток строк не судится",
    ),
    out: str = typer.Option("", help="Файл: JSON-массив итогов для address-judge --in"),
) -> None:
    """Сухой суд карты по правилу: что решило бы правило на боевых строках, без записи.

    Всегда сухой (флага «в бой» нет): каждая строка судится тем же
    `workers/geocode.geocode_candidate` с `dry_run=True` — те же карты, тот же
    порядок, та же политика правил, — но вердикт не пишется, задачи и кадры не
    ставятся. Сухой ≠ бесплатный (программа М-6): DaData и Яндекс тратятся в
    доле починки, повтор того же адреса берётся из кэша ответов; свои походы
    считаются (`dadata_за_прогон`) и упираются в `--dadata-budget`: достигнут —
    прогон останавливается перед следующей строкой (`остановлен_по_бюджету=1
    несудимо=K`), отчёт и `--out` выходят по судимым. Судятся и решённые строки
    (`exact`, отказы, неспокойные): вопрос — «что решило бы правило сегодня»;
    решённая строка судится со своими уликами как «прежними» — при неполном
    наборе карт удерживается прежний вердикт, как в боевом пересуде
    (`удержано=N`, `слепых=N` — суды без части карт). Политика — боевая
    настройка `address_geo.rule_policy` плюс `ИМЯ=exact` («как если бы
    включили»), а `--policy` перекрывает поверх (`--policy ИМЯ=off` — сравнить
    с выключенным). В отчёте — строки карты без ПД; речь клиента в отчёт не идёт.
    """

    async def main(db: AsyncSession) -> None:
        await run_address_rule_dry_run(
            db,
            rule=rule,
            policy=list(policy),
            days=days,
            statuses=statuses,
            levels=levels,
            limit=limit,
            dadata_budget=dadata_budget,
            out=out or None,
        )

    _run(main)


def _список_опции(текст: str) -> list[str]:
    return [часть.strip() for часть in (текст or "").split(",") if часть.strip()]


def _степень_сухого(итог: Any) -> str:
    """Корзина итога для счётчиков: `suggest` (хвост предложения) → `approx`
    (точка приблизительная) → `exact` → `refused` (отказ по содержанию) →
    `other` (ещё проверяется, выбор из нескольких, город неизвестен)."""
    from app.services import geocode

    if geocode.point_is_suggest(итог.provider):
        return "suggest"
    if итог.status == geocode.GEO_EXACT:
        return "approx" if geocode.point_is_approx(итог.provider) else "exact"
    if итог.status in geocode.REFUSAL_STATUSES:
        return "refused"
    return "other"


async def run_address_rule_dry_run(
    db: AsyncSession,
    *,
    rule: str,
    policy: list[str] | None = None,
    days: int = 30,
    statuses: str = "",
    levels: str = "A,B",
    limit: int = 100,
    dadata_budget: int = _СУХОЙ_БЮДЖЕТ_DADATA,
    out: str | None = None,
    factory: Any | None = None,
    redis: Any | None = None,
) -> dict[str, Any]:
    """Тело сухого прогона; печатает отчёт, возвращает счётчики и строки итога
    (для проверок и `--out`). `factory`/`redis` — для стенда; в бою — фабрика
    сессий и клиент Redis процесса. `dadata_budget` — потолок своих походов
    DaData (0 — без потолка)."""
    import json
    from pathlib import Path

    from arq import Retry

    from app.models import ClientAddressCandidate
    from app.models.client import CANDIDATE_REJECTED
    from app.services import address_parse, app_settings, geocode
    from app.workers import geocode as worker

    if rule not in geocode.RULE_DEFAULT_POLICY:
        typer.echo(
            f"--rule: неизвестное правило «{rule}»; известны: "
            f"{', '.join(sorted(geocode.RULE_DEFAULT_POLICY))}"
        )
        raise typer.Exit(code=2)
    # Перекрытия из опций — СТРОГО (это запись человеком, как в ручке настроек):
    # опечатка в имени или значении молча судила бы не то правило.
    try:
        из_опций = geocode.parse_rule_policy(",".join(policy or []), strict=True)
    except ValueError as exc:
        typer.echo(f"--policy: {exc}")
        raise typer.Exit(code=2) from exc
    уровни = _список_опции(levels)
    if not уровни or any(у not in _УРОВНИ for у in уровни):
        typer.echo(f"--levels: ожидаются уровни из {', '.join(_УРОВНИ)}")
        raise typer.Exit(code=2)
    известные_статусы = (
        geocode.FINAL_STATUSES
        | geocode.REQUEUE_STATUSES
        | {s for s in geocode.CHECKING_STATUSES if s}
    )
    # Умолчание — любой окончательный вердикт: отказы по содержанию, НЕСПОКОЙНЫЕ
    # (`ambiguous`/`elsewhere`/`city_mismatch`) и `exact`. Без неспокойных
    # прибор слеп к половине правил (ревью 21.09, #1): «вердикт без правила» у
    # suburb/only_in_radius/neighbour_settlement — `elsewhere`, у fullest_street/
    # one_spot/best_of — `ambiguous`, и строки их собственной тени (§0.3
    # `shadow`) в выборку не попадали бы вовсе. Явный `--statuses` — строгий
    # фильтр, «своё» из-под него не вынимается.
    статусы = _список_опции(statuses) or sorted(
        geocode.REFUSAL_STATUSES | geocode.REQUEUE_STATUSES | {geocode.GEO_EXACT}
    )
    if any(s not in известные_статусы for s in статусы):
        typer.echo(f"--statuses: ожидаются статусы из {', '.join(sorted(известные_статусы))}")
        raise typer.Exit(code=2)
    limit = max(1, min(limit, _СУХОЙ_ПОТОЛОК))
    бюджет = max(0, dadata_budget)
    # Политика прогона: боевая настройка (нестрого, как читает воркер) →
    # правило «как если бы включили» → перекрытия человека.
    боевая = geocode.parse_rule_policy(
        str(await app_settings.get(db, app_settings.ADDRESS_GEO_RULE_POLICY) or ""),
        strict=False,
    )
    политика: dict[str, str] = {**боевая, rule: geocode.POLICY_EXACT, **из_опций}

    a = ClientAddressCandidate
    с = datetime.now(UTC) - timedelta(days=days)
    # Строки правила — первыми: по следу нынешнего суда или его тени; остальные
    # — свежие вперёд. Сравнение через `as_string()` — одинаково на SQLite стенда
    # и PostgreSQL боя (как `_фильтр_следа`).
    своё = sa.or_(
        a.trace["rule"].as_string() == rule,
        a.trace["shadow"]["rule"].as_string() == rule,
    )
    rows = (
        await db.execute(
            select(a.id, a.conversation_id, a.geo_status, a.geo_formatted)
            .where(
                a.kind == address_parse.KIND_HOUSE,
                a.detected_at >= с,
                a.level.in_(уровни),
                a.geo_status.in_(статусы),
                a.status != CANDIDATE_REJECTED,
            )
            .order_by(sa.case((своё, 0), else_=1), a.detected_at.desc())
            .limit(limit)
        )
    ).all()
    # Выборка снята — транзакцию CLI закрываем: воркер ходит к картам минутами
    # и открывает свои сессии, а соединение CLI «idle in transaction» всё это
    # время держало бы место в пуле (урок «пул-голодание»).
    await db.rollback()

    очередь = redis if redis is not None else redis_mod.get_client()
    ctx = {
        "db_session_factory": factory if factory is not None else db_mod.get_session_factory(),
        "redis": очередь,
        "job_try": 1,
    }
    # Несудимые — поимённо (ревью 21.09, #6): `paused` (провайдер под баном,
    # DaData при этом уже сходила), `disabled` (карта выключена настройкой),
    # `gone` (строка удалена). Ключи заведены заранее: обычный словарь.
    счёт: dict[str, int] = {
        "строк": len(rows),
        "судимо": 0,
        "решено_правилом": 0,
        "по_правилу": 0,
        "exact": 0,
        "approx": 0,
        "suggest": 0,
        "отказов": 0,
        "прочее": 0,
        "would_autofill": 0,
        "удержано": 0,
        "слепых": 0,
        "dadata_за_прогон": 0,
        "повтор_сети": 0,
        "пропущено_paused": 0,
        "пропущено_disabled": 0,
        "пропущено_gone": 0,
        "остановлен_по_бюджету": 0,
        "несудимо": 0,
    }
    итоги: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        # Бюджет — ПЕРЕД строкой, по своим походам; перерасход не больше цены
        # одной строки. `break`, а не выход: отчёт и `--out` — по судимым.
        if бюджет and счёт["dadata_за_прогон"] >= бюджет:
            счёт["остановлен_по_бюджету"] = 1
            счёт["несудимо"] = len(rows) - i
            typer.echo(
                f"бюджет DaData выбран ({счёт['dadata_за_прогон']} ≥ {бюджет}): "
                f"остановлен перед строкой {i + 1}, несудимо={счёт['несудимо']}"
            )
            break
        копилка = worker.DrySpill()
        try:
            статус, итог = await worker.dry_judge(
                ctx, r.id, origin=worker.ORIGIN_REPAIR, policy=политика, spill=копилка
            )
        except Retry:
            # Сеть: в бою задача повторилась бы через ARQ; у сухого прогона
            # повтора нет — строка считается несудимой, прогон идёт дальше.
            счёт["повтор_сети"] += 1
            счёт["dadata_за_прогон"] += копилка.dadata_requests
            continue
        счёт["dadata_за_прогон"] += копилка.dadata_requests
        if итог is None:
            ключ = f"пропущено_{статус}"
            счёт[ключ] = счёт.get(ключ, 0) + 1
            if статус == "disabled":
                # Настройка глобальная и читается заново на каждую строку:
                # остальные дадут то же, судить нечего.
                typer.echo(
                    "карта выключена настройкой address_geo.enabled — судить нечего, "
                    "прогон остановлен"
                )
                break
            continue
        счёт["судимо"] += 1
        корзина = _степень_сухого(итог)
        счёт[{"refused": "отказов", "other": "прочее"}.get(корзина, корзина)] += 1
        # Удержанный ключ — решение прежнего суда, не этого прогона: под
        # `--policy ИМЯ=off` он не должен выглядеть как «правило решило»
        # (в JSON правило остаётся рядом с `kept` — судье оно нужно).
        if итог.rule is not None and not итог.kept:
            счёт["решено_правилом"] += 1
            if итог.rule == rule:
                счёт["по_правилу"] += 1
        if итог.would_autofill:
            счёт["would_autofill"] += 1
        # Удержан прежний вердикт (набор карт неполный, новое слабее) — правило
        # и ключ в итоге прежнего суда; слепой — суд без части карт (#3, #7).
        if итог.kept:
            счёт["удержано"] += 1
        if итог.missing:
            счёт["слепых"] += 1
        итоги.append(
            {
                "candidate_id": str(r.id),
                "conversation_id": str(r.conversation_id),
                "rule": итог.rule,
                "policy": итог.policy,
                "status": итог.status,
                "key": итог.formatted,
                "km": round(итог.km, 2) if итог.km is not None else None,
                "would_autofill": итог.would_autofill,
                "prev_status": r.geo_status,
                "shadow": итог.shadow.as_trace() if итог.shadow is not None else None,
                "kept": итог.kept,
                "missing": list(итог.missing),
            }
        )
    # Хвосты сводки — только ненулевые: несудимые исходы и останов по бюджету.
    хвосты = (
        "удержано",
        "слепых",
        "повтор_сети",
        "пропущено_paused",
        "пропущено_disabled",
        "пропущено_gone",
        "остановлен_по_бюджету",
        "несудимо",
    )
    typer.echo(
        f"строк={счёт['строк']} судимо={счёт['судимо']} "
        f"решено_правилом={счёт['решено_правилом']} (по {rule}: {счёт['по_правилу']}) "
        f"exact={счёт['exact']} approx={счёт['approx']} suggest={счёт['suggest']} "
        f"отказов={счёт['отказов']} прочее={счёт['прочее']} "
        f"would_autofill={счёт['would_autofill']} dadata_за_прогон={счёт['dadata_за_прогон']}"
        + "".join(f" {k}={счёт[k]}" for k in хвосты if счёт[k])
    )
    if счёт["пропущено_paused"]:
        typer.echo(
            f"провайдер под баном (geo:blocked:<p>): {счёт['пропущено_paused']} строк не "
            "судимы, итог неполный и смещён (выпадают строки, где DaData дом не "
            "подтвердила) — повторить через час"
        )
    typer.echo(f"политика: {', '.join(f'{k}={v}' for k, v in sorted(политика.items()))}")
    # Примеры — строки карты (ПД нет), маска поверх как страховка; речь клиента
    # в отчёт не идёт.
    for i, итог_строки in enumerate(итоги[:_СУХИХ_ПРИМЕРОВ], start=1):
        typer.echo(
            "\t".join(
                [
                    str(i),
                    итог_строки["conversation_id"],
                    f"{итог_строки['prev_status']}→{итог_строки['status']}",
                    итог_строки["rule"] or "—",
                    f"{итог_строки['km']} км" if итог_строки["km"] is not None else "",
                    _маска_адреса(итог_строки["key"]),
                ]
            )
        )
    if out:
        Path(out).write_text(json.dumps(итоги, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"итоги записаны: {out} ({len(итоги)})")
    log.info(
        "address.rule_dry_run",
        rule=rule,
        policy=политика,
        days=days,
        **{k: v for k, v in счёт.items() if k != "по_правилу"},
        by_rule=счёт["по_правилу"],
    )
    return {**счёт, "rows": итоги}


# ── address-judge (пакет 6.0б, I-4 судья, I-7 базовый замер) ─────────────────

#: Порция одного запуска: ≈ 60 × (пауза + ответ) — до получаса на худшей
#: модели; стенд 19.09 упал на 175-й строке одной транзакцией (EXIT 137).
_СУДЬЯ_ПОРЦИЯ = 60
_СУДЬЯ_ПАУЗА = 1.5
_СУДЬЯ_ШАГ_ПЕЧАТИ = 10
#: Окна по умолчанию: строки правила — окно воронки (8 недель), базовый
#: замер карточек — месяц.
_СУДЬЯ_ДНИ_ПРАВИЛА = 56
_СУДЬЯ_ДНИ_СТЕПЕНИ = 30
#: Порог тревоги базового замера I-7 (§8.4): доля ложных среди exact и approx.
#: Только печать — решение о классе правил принимает владелец.
_ТРЕВОГА_ЛОЖНЫХ_PCT = {"exact": 2.0, "approx": 10.0}
#: Откуда взялась строка журнала настроек при калибровке.
_ИСТОЧНИК_КАЛИБРОВКИ = "judge_calibration"
_TSV_ШАПКА = ("id", "реплики", "карточка", "human")


async def _строка_согласия_судьи(db: AsyncSession) -> str:
    """`judge_agreement=… (дата)` для отчёта воронки: настройка калибровки."""
    from app.services import app_settings

    согласие = app_settings.judge_agreement_pct(
        await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT)
    )
    когда = await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_CALIBRATED_AT)
    if согласие is None:
        return "judge_agreement=— (калибровки не было)"
    дата = (
        datetime.fromtimestamp(int(когда), tz=UTC).date().isoformat()
        if isinstance(когда, int)
        else "дата неизвестна"
    )
    return f"judge_agreement={согласие} % ({дата})"


@app.command("address-judge")
def address_judge_cmd(
    rule: str = typer.Option(
        "", "--rule", help="Имя правила: его решения за окно, ещё не осуждённые"
    ),
    grade: str = typer.Option(
        "", "--grade", help="exact | approx | text — базовый замер карточек автоматики (I-7)"
    ),
    in_file: str = typer.Option(
        "", "--in", help="JSON сухого прогона (address-rule-dry-run --out)"
    ),
    sample: int = typer.Option(
        0, "--sample", help="Выгрузить N строк на разметку человеком (с --out)"
    ),
    out: str = typer.Option("", "--out", help="Файл TSV для --sample"),
    calibrate: str = typer.Option(
        "", "--calibrate", help="Размеченный TSV (колонка human): согласие судьи и запись настройки"
    ),
    max_rows: int = typer.Option(
        _СУДЬЯ_ПОРЦИЯ, "--max", help=f"Строк за запуск, не больше {_СУДЬЯ_ПОРЦИЯ}"
    ),
    days: int | None = typer.Option(
        None, help=f"Окно в днях: --rule {_СУДЬЯ_ДНИ_ПРАВИЛА}, --grade {_СУДЬЯ_ДНИ_СТЕПЕНИ}"
    ),
    pause: float = typer.Option(_СУДЬЯ_ПАУЗА, help="Пауза между походами к модели, с"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Судить, но след строки не писать"),
    force: bool = typer.Option(
        False, "--force", help="--calibrate: записать настройку и при недоборе сотни"
    ),
) -> None:
    """Судья карточек адреса: модель сверяет адрес карточки с речью клиента (§0.4).

    Один из режимов: `--rule ИМЯ` — все неосуждённые решения этого правила за
    окно, боевые и теневые (ночная задача `address_judge_daily` берёт у строки
    одну цель за прогон, бой прежде тени; `--rule` судит тень правила, не
    дожидаясь суда над чужим боем); `--grade exact|approx|text` — базовый
    замер I-7: карточки автоматики этой степени, печатает долю ложных и порог
    тревоги; `--in ФАЙЛ` — строки сухого прогона `address-rule-dry-run --out`:
    судится строка карты из файла, вердикт пишется в
    `trace.judge`/`trace.shadow.judge` только когда правило файла — то же, каким
    строка решена в бою или в тени, И строка карты из файла равна
    `geo_formatted`/`shadow.key` строки (иначе вердикт напечатан, след не
    тронут, счётчик `key_mismatch`); `--sample N --out ФАЙЛ.tsv` — выгрузка на
    разметку человеком масками (берите с запасом на `failed`/`gone`, например
    120); `--calibrate ФАЙЛ.tsv` — прогон судьи по размеченным строкам, печать
    согласия и запись `address_llm.judge_agreement` (лестница пускает судью в
    ворота при ≥ 95) — только когда отвеченных пар ≥ 100 (программа §0.3:
    «на сотне»); меньше — согласие печатается, настройка не пишется, выход 1;
    повтор тем же файлом добирает сотню по сохранённым вердиктам
    (`trace.judge`, без второго похода); `--force` пишет и при недоборе, с
    пометкой `forced` в журнале.

    Порции не больше 60 строк, пауза между походами, транзакция на строку:
    сессия не держится, пока модель думает. Квота — общий потолок модели минус
    запас живому читателю плюс своя доля судьи (`address-limits --llm`,
    `address_llm.judge_share`); печатается `judge_used/share llm_today/limit`.
    """

    async def main(db: AsyncSession) -> None:
        await run_address_judge(
            db,
            rule=rule,
            grade=grade,
            in_file=in_file,
            sample=sample,
            out=out,
            calibrate=calibrate,
            max_rows=max_rows,
            days=days,
            pause=pause,
            dry_run=dry_run,
            force=force,
        )

    _run(main)


async def run_address_judge(
    db: AsyncSession,
    *,
    rule: str = "",
    grade: str = "",
    in_file: str = "",
    sample: int = 0,
    out: str = "",
    calibrate: str = "",
    max_rows: int = _СУДЬЯ_ПОРЦИЯ,
    days: int | None = None,
    pause: float = _СУДЬЯ_ПАУЗА,
    dry_run: bool = False,
    force: bool = False,
    redis: Any | None = None,
    session_factory: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Тело `address-judge`; возвращает счётчики прогона — для проверок.
    `session_factory` — фабрика сессий на строку (в бою — `db_mod`); `db` —
    сессия команды: выборка и запись настройки, на время походов отпускается.
    `--calibrate` при недоборе `JUDGE_CALIBRATION_N` отвеченных пар настройку
    не пишет и выходит с кодом 1 (`force` — пишет с пометкой в журнале)."""
    import csv
    import json
    from collections import Counter
    from pathlib import Path

    from app.integrations import gateway
    from app.services import address_judge, app_settings
    from app.services.address_funnel import FALSE_VERDICTS, JUDGE_VERDICTS

    режимы = [
        имя
        for имя, есть in (
            ("rule", rule),
            ("grade", grade),
            ("in", in_file),
            ("sample", sample),
            ("calibrate", calibrate),
        )
        if есть
    ]
    if len(режимы) != 1:
        typer.echo("нужен ровно один режим: --rule, --grade, --in, --sample или --calibrate")
        raise typer.Exit(code=2)
    режим = режимы[0]
    now = now or datetime.now(UTC)
    порция = max(1, min(max_rows, _СУДЬЯ_ПОРЦИЯ))
    factory = session_factory or db_mod.get_session_factory()

    if режим == "sample":
        if not out:
            typer.echo("--sample пишет TSV: укажите --out ФАЙЛ.tsv")
            raise typer.Exit(code=2)
        строки = await address_judge.calibration_rows(db, limit=max(1, sample), now=now)
        with Path(out).open("w", encoding="utf-8", newline="") as f:
            писарь = csv.writer(f, delimiter="\t", lineterminator="\n")
            писарь.writerow(_TSV_ШАПКА)
            for с in строки:
                писарь.writerow([str(с.row_id), " | ".join(с.speech), " ".join(с.card.split()), ""])
        typer.echo(
            f"выгружено {len(строки)} строк на разметку → {out} "
            f"(human ∈ {', '.join(JUDGE_VERDICTS)})"
        )
        log.info("address_judge.sample", rows=len(строки), out=out)
        return {"sample": len(строки)}

    # Что судить: очередь (строка, адрес вместо строки карты, правило файла).
    очередь: list[tuple[address_judge.Pick, str | None, str | None]] = []
    человек: dict[uuid.UUID, str] = {}
    if режим == "rule":
        picks = await address_judge.pick_rows(
            db,
            now=now,
            per_rule=порция,
            weeks=(days or _СУДЬЯ_ДНИ_ПРАВИЛА) / 7,
            limit=порция,
            rule=rule,
        )
        очередь = [(p, None, None) for p in picks]
    elif режим == "grade":
        try:
            picks = await address_judge.sample_for_grade(
                db, grade=grade, days=days or _СУДЬЯ_ДНИ_СТЕПЕНИ, limit=порция, now=now
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=2) from exc
        очередь = [(p, None, None) for p in picks]
    elif режим == "in":
        try:
            записи = json.loads(Path(in_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            typer.echo(f"--in: файл не читается: {exc}")
            raise typer.Exit(code=2) from exc
        for з in записи if isinstance(записи, list) else []:
            if not isinstance(з, dict) or not з.get("candidate_id") or not з.get("key"):
                continue  # правило строку не решило — судить нечего
            try:
                row_id = uuid.UUID(str(з["candidate_id"]))
            except ValueError:
                continue
            правило = str(з["rule"]) if з.get("rule") else ""
            очередь.append(
                (address_judge.Pick(row_id=row_id, rule=правило), str(з["key"]), правило)
            )
            if len(очередь) >= порция:
                break
    else:  # calibrate
        try:
            with Path(calibrate).open(encoding="utf-8", newline="") as f:
                чтец = csv.DictReader(f, delimiter="\t")
                for з in чтец:
                    метка = (з.get("human") or "").strip().lower()
                    if метка not in JUDGE_VERDICTS:
                        continue
                    try:
                        row_id = uuid.UUID((з.get("id") or "").strip())
                    except ValueError:
                        continue
                    человек[row_id] = метка
                    очередь.append((address_judge.Pick(row_id=row_id, rule=""), None, None))
        except OSError as exc:
            typer.echo(f"--calibrate: файл не читается: {exc}")
            raise typer.Exit(code=2) from exc
        if not очередь:
            typer.echo("--calibrate: размеченных строк (human ∈ словарь вердиктов) нет")
            raise typer.Exit(code=2)
    # Калибровка добирает сотню между запусками: свежий вердикт судьи из следа
    # берётся без похода — обрыв по квоте или отказ читателей не заставляют
    # пережигать долю заново.
    из_следа: dict[uuid.UUID, str] = (
        await address_judge.saved_verdicts(db, [p.row_id for p, _, _ in очередь])
        if режим == "calibrate"
        else {}
    )
    # Выборка прочитана — транзакция команды отпускается на время походов.
    await db.rollback()
    typer.echo(f"строк в очереди: {len(очередь)} режим={режим} сухой_прогон={dry_run}")

    очередь_redis = redis if redis is not None else redis_mod.get_client()
    await gateway.refresh_status()  # ключи читателей знает только шлюз (16.09)
    исходы: Counter[str] = Counter()
    вердикты: Counter[str] = Counter()
    пары: list[tuple[str | None, str | None]] = []
    походов = 0
    for pick, адрес, правило_файла in очередь:
        if pick.row_id in из_следа:
            исходы["from_trace"] += 1
            вердикты[из_следа[pick.row_id]] += 1
            пары.append((человек.get(pick.row_id), из_следа[pick.row_id]))
            continue
        if режим == "calibrate" and походов >= порция:
            # Порция ≤ 60 походов и у калибровки: остаток доберётся следующим
            # запуском тем же файлом — свежие вердикты возьмутся из следа.
            typer.echo(
                f"порция {порция} исчерпана — повторите тем же файлом, "
                "осуждённое возьмётся из следа"
            )
            break
        if походов:
            await asyncio.sleep(pause)
        походов += 1
        async with factory() as s:
            итог = await address_judge.judge_row(
                s,
                очередь_redis,
                pick.row_id,
                target=pick.target,
                card_address=адрес,
                for_rule=правило_файла,
                dry_run=dry_run,
                now=now,
            )
            await s.commit()
        исходы[итог.outcome] += 1
        if итог.outcome == address_judge.OUTCOME_JUDGED and итог.reason == "key_mismatch":
            # Вердикт получен и напечатан, но след не тронут: правило файла или
            # его строка карты не совпали с решением строки.
            исходы["key_mismatch"] += 1
        if итог.verdict:
            вердикты[итог.verdict] += 1
        if режим == "calibrate":
            пары.append((человек.get(pick.row_id), итог.verdict))
        if походов % _СУДЬЯ_ШАГ_ПЕЧАТИ == 0:
            typer.echo(f"… {походов}/{len(очередь)} judged={исходы[address_judge.OUTCOME_JUDGED]}")
        if итог.outcome == address_judge.OUTCOME_SKIPPED_QUOTA:
            typer.echo("квота выбрана — остальное в следующей порции")
            break
    квота = await address_judge.quota(db, очередь_redis)
    judged = исходы[address_judge.OUTCOME_JUDGED]
    # Счётчики только своего режима: `key_mismatch` — вердикт напечатан, след
    # не тронут (`--in`); `from_trace` — пара взята из следа без похода.
    свои = (f"key_mismatch={исходы['key_mismatch']} " if режим == "in" else "") + (
        f"from_trace={исходы['from_trace']} " if режим == "calibrate" else ""
    )
    typer.echo(
        f"judged={judged} true={вердикты['true']} false={вердикты['false']} "
        f"place_swap={вердикты['place_swap']} disputed={вердикты['disputed']} "
        f"skipped_quota={исходы[address_judge.OUTCOME_SKIPPED_QUOTA]} "
        f"skipped_no_speech={исходы[address_judge.OUTCOME_SKIPPED_NO_SPEECH]} "
        f"failed={исходы[address_judge.OUTCOME_FAILED]} gone={исходы[address_judge.OUTCOME_GONE]} "
        f"{свои}"
        f"judge_used={квота.judge_used}/share={квота.share if квота.share is not None else 'нет'} "
        f"llm_today={квота.used_today}/limit={квота.limit if квота.limit is not None else 'нет'}"
    )
    счёт: dict[str, Any] = {"mode": режим, "judged": judged, **исходы, "verdicts": dict(вердикты)}
    if режим == "grade":
        ложных = sum(вердикты[v] for v in FALSE_VERDICTS)
        false_pct = round(ложных * 100 / judged, 1) if judged else None
        порог = _ТРЕВОГА_ЛОЖНЫХ_PCT.get(grade)
        typer.echo(
            f"false_pct={false_pct if false_pct is not None else '—'} "
            f"({ложных} из {judged} {grade})"
        )
        if порог is not None and false_pct is not None and false_pct > порог:
            typer.echo(
                f"тревога: ложных среди {grade} {false_pct} % > {порог} % (§8.4 — только печать)"
            )
        счёт["false_pct"] = false_pct
    if режим == "calibrate":
        совпало, всего = address_judge.agreement(пары)
        pct = round(совпало * 100 / всего) if всего else None
        typer.echo(f"agreement={совпало}/{всего} ({pct if pct is not None else '—'} %)")
        расхождения = Counter(
            (чел, суд)
            for чел, суд in пары
            if чел in JUDGE_VERDICTS and суд in JUDGE_VERDICTS and чел != суд
        )
        for (чел, суд), n in sorted(расхождения.items()):
            typer.echo(f"расхождение human={чел} judge={суд}: {n}")
        счёт.update({"agreement": [совпало, всего], "agreement_pct": pct})
        # Ворота лестницы открываются только с сотни отвеченных пар (программа
        # §0.3): по обрывку прогона (квота, отказы читателей) `judge_agreement`
        # не пишется — иначе одна строка давала бы «100 %» (ревью #10).
        недобор = всего < address_judge.JUDGE_CALIBRATION_N
        if недобор:
            typer.echo(
                f"калибровка на {всего} строках — программа велит "
                f"{address_judge.JUDGE_CALIBRATION_N} "
                f"(skipped_quota={исходы[address_judge.OUTCOME_SKIPPED_QUOTA]} "
                f"failed={исходы[address_judge.OUTCOME_FAILED]} "
                f"gone={исходы[address_judge.OUTCOME_GONE]} "
                f"skipped_no_speech={исходы[address_judge.OUTCOME_SKIPPED_NO_SPEECH]})"
            )
        if pct is not None and not dry_run and (not недобор or force):
            было = await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_AGREEMENT)
            await app_settings.set_many(
                db,
                {
                    app_settings.ADDRESS_LLM_JUDGE_AGREEMENT: pct,
                    app_settings.ADDRESS_LLM_JUDGE_CALIBRATED_AT: int(now.timestamp()),
                },
                user_id=None,
            )
            подробности: dict[str, Any] = {
                "source": _ИСТОЧНИК_КАЛИБРОВКИ,
                "before": {"judge_agreement": было},
                "after": {"judge_agreement": pct, "judge_calibrated_at": int(now.timestamp())},
                "agreement": [совпало, всего],
            }
            if недобор:
                подробности["forced"] = True
            await write_audit(
                db,
                user_id=None,
                action="settings.address_detect_changed",
                entity="settings",
                details=подробности,
            )
            await db.commit()
            typer.echo(
                f"настройка judge_agreement: {было} → {pct}"
                + (f" (записано по {всего} строкам принудительно, --force)" if недобор else "")
            )
        elif недобор and not dry_run:
            typer.echo(
                "настройка не записана: доберите квоту и повторите тем же файлом "
                "(свежие вердикты берутся из следа без похода) или --force"
            )
            log.info(
                "address_judge.cli",
                **{k: v for k, v in счёт.items() if k != "verdicts"},
                verdicts=dict(вердикты),
            )
            raise typer.Exit(code=1)
    log.info(
        "address_judge.cli",
        **{k: v for k, v in счёт.items() if k != "verdicts"},
        verdicts=dict(вердикты),
    )
    return счёт


if __name__ == "__main__":
    app()
