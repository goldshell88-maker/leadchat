"""Управление сотрудниками — 01 §3.1–3.5, экран «Команда» 11 §4.2.

До этого модуля завести сотрудника, сменить ему роль или отключить его можно
было только командой в консоли (`app/cli.py`), а отключение вообще не
доводилось до конца: строка в БД менялась, но живые сессии человека
продолжали работать. Здесь обе дыры закрыты.

**Транзакцией владеет сервис, а не ручка** — сознательное отступление от
конвенции остального бэкенда (08 §8.1, `write_audit`). Причина: у каждой
мутации здесь есть обязательный ХВОСТ после commit'а — обрыв сессий
(`services/sessions.py`) и выпуск одноразовой ссылки. Забыть хвост в одной из
шести точек вызова — значит получить «отключён, но всё ещё читает переписку»,
то есть ровно тот дефект, ради которого модуль написан. Поэтому commit и
хвост живут вместе, в одной функции, а ручка остаётся тонкой. Порядок внутри
неизменен: БД → commit → Redis/Pub-Sub (публикация события, которое потом
откатится, выкинула бы человека из системы без причины).

**Служебный smoke-пользователь** (домен `.local`, 07 §6) не виден в списке и
не управляется этими ручками (404): робот регрессии заводится и гасится
CLI-командой, а не кнопкой в интерфейсе — случайно отключить его из UI
означало бы уронить проверку прода.

**Последний администратор.** «Последний» считается строго: активный, не
служебный и **с установленным паролем**. Админ, который приглашён, но ссылкой
не воспользовался, войти не может — считать его страховкой значит разрешить
операцию, после которой в систему не зайдёт никто.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import (
    hash_password,
    invite_url,
    is_password_set,
    issue_invite,
    make_unreachable_hash,
)
from app.models import Conversation, User
from app.schemas.users import DEFAULT_LIMIT, MAX_LIMIT
from app.services.audit import write_audit
from app.services.sessions import (
    WS_CLOSE_LOGOUT,
    WS_CLOSE_RELOGIN,
    clear_revocation,
    revoke_sessions,
)
from app.services.user_ref import normalize_department
from app.ws.events import iso, publish_event
from app.ws.hub import publish_inbox_new
from app.ws.presence import presence_status_map

log = structlog.get_logger("app.users")

ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class InviteIssued:
    """Пользователь + одноразовая ссылка установки пароля (01 §3.2)."""

    user: User
    url: str
    expires_at: datetime


# --- сериализация ------------------------------------------------------------


def serialize_user(
    user: User, *, is_online: bool = False, presence_status: str | None = None
) -> dict[str, Any]:
    """Строка таблицы «Сотрудники» (01 §3.1).

    `is_online` у отключённого всегда false: ключ presence живёт с TTL и ещё
    минуту после обрыва сокета показывал бы уволенного «в сети» (11 §4.2 —
    ⚫ у деактивированного, а не 🟢).
    """
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_online": bool(is_online) and user.is_active,
        # «На месте» / «отошёл» — ОТДЕЛЬНО от «в сети» (#34).
        #
        # Одним булевым признаком тут не обойтись: отошедший обязан оставаться
        # ВИДИМЫМ (он за столом, ему можно написать и передать диалог), но
        # отличимым — иначе руководитель отдаст диалог обедающему и будет
        # ждать ответа. Поэтому зелёная точка остаётся, а рядом появляется
        # состояние.
        #
        # У отключённого — None по той же причине, что и `is_online=false`:
        # ключ присутствия живёт с TTL и ещё минуту показывал бы уволенного.
        "presence": (presence_status if user.is_active else None),
        "invite_pending": not is_password_set(user.password_hash),
        # 7.4: ведёт ли человек диалоги — отдельно от роли, и отдел.
        "handles_conversations": user.handles_conversations,
        # Как есть, БЕЗ чистки: строка редактируется прямо в этой таблице, и
        # показать одно, а хранить другое значит подсунуть человеку правку,
        # которой он не делал. Чистка живёт на записи (см. `update_user`).
        "department": user.department,
        "color": user.color,
        "created_at": iso(user.created_at) or "",
    }


# --- выборки -----------------------------------------------------------------


async def _by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.strip().lower())
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_managed_user(
    db: AsyncSession, user_id: uuid.UUID, *, allow_deleted: bool = False
) -> User:
    """Сотрудник, которым можно управлять с экрана «Команда».

    УДАЛЁННОГО ЗДЕСЬ НЕТ — по той же причине, по которой его нет в списках
    (`list_users`, `/users/assignable`, пул операторов, 01 §3.5): снаружи
    такого сотрудника не существует. Раньше проверки не было ни здесь, ни в
    вызовах, и `activate_user` включал удалённого обратно: строка получала
    `is_active = true`, а вместе с ней — вход, REST и сокет, потому что и
    логин, и `api/deps.py` смотрят только на `is_active`. Получался доступ,
    которого не видно: в «Команде» человека нет, в «кому передать» нет,
    отключить его кнопкой нельзя — кнопки для невидимой строки не существует,
    а переписка клиентов ему уже открыта.

    `allow_deleted=True` берут ровно две операции, которым удалённая строка
    нужна именно как удалённая: повторное удаление (оно обязано остаться
    идемпотентным) и включение — оно объясняет отказ своими словами вместо
    общего «сотрудник не найден».
    """
    user = await db.get(User, user_id)
    # Служебность — колонка, а не домен почты: на `.local` живут и настоящие
    # люди, и по домену их прятало от управления (довод у `User.is_service`).
    if user is None or user.is_service:
        raise ApiError("not_found", status=404, message="Сотрудник не найден")
    if user.deleted_at is not None and not allow_deleted:
        raise ApiError("not_found", status=404, message="Сотрудник не найден")
    return user


async def _reachable_admin_ids(db: AsyncSession) -> set[uuid.UUID]:
    """Администраторы, которые прямо сейчас могут войти в систему."""
    stmt = select(User).where(User.role == ADMIN_ROLE, User.is_active.is_(True))
    return {
        u.id
        for u in (await db.execute(stmt)).scalars()
        if not u.is_service and is_password_set(u.password_hash)
    }


async def _assert_admin_remains(db: AsyncSession, target: User, *, what: str) -> None:
    """Запретить операцию, после которой в системе не останется админа.

    Проверка живёт в сервисе, а не в ручке, потому что она про состояние
    системы, а не про права вызывающего: сегодня `users:manage` есть только у
    admin'а и «понизить себя» — единственный способ сюда попасть, но матрица
    прав меняется (DESIGN §5.1), а инвариант «админ есть всегда» — нет.
    """
    if target.role != ADMIN_ROLE:
        return
    if await _reachable_admin_ids(db) - {target.id}:
        return
    raise ApiError(
        "last_admin",
        status=409,
        message=(
            f"Нельзя {what}: это последний администратор системы. "
            "Сначала назначьте администратором кого-то ещё."
        ),
        details={"reason": "last_admin"},
    )


async def list_users(
    db: AsyncSession,
    redis: Redis,
    *,
    q: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Список сотрудников с фильтрами (01 §3.1). Возвращает (строки, total).

    Пагинация считается в Python, потому что служебных пользователей
    отсеивает Python (правило — домен адреса, 07 §6): SQL-LIMIT поверх такого
    списка врал бы в `total` и оставлял дырки на страницах. Сотрудников
    десятки — выборка целиком дешевле, чем неверная арифметика.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    # Удалённых не показываем НИКОГДА, включая галочку «показывать
    # отключённых»: отключённый вернётся, удалённый ушёл, и держать его в
    # списке значит заново отвечать на вопрос «а это кто» каждый раз.
    stmt = stmt.where(User.deleted_at.is_(None))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(User.full_name.ilike(pattern) | User.email.ilike(pattern))
    stmt = stmt.order_by(User.full_name, User.id)

    rows = [u for u in (await db.execute(stmt)).scalars() if not u.is_service]
    total = len(rows)
    page = rows[offset : offset + limit]
    # Один MGET на страницу — presence принадлежит WS-хабу (08 §5.5).
    status = await presence_status_map(redis, [u.id for u in page])
    return [
        serialize_user(u, is_online=bool(status.get(u.id)), presence_status=status.get(u.id))
        for u in page
    ], total


# --- мутации -----------------------------------------------------------------


async def invite_user(
    db: AsyncSession,
    redis: Redis,
    *,
    actor: User,
    email: str,
    full_name: str,
    role: str,
) -> InviteIssued:
    """Завести сотрудника и выдать одноразовую ссылку установки пароля (01 §3.2).

    Пароля у новой строки нет: в `password_hash` лежит заведомо недостижимая
    заглушка, войти по ней невозможно (`is_password_set` → false).
    """
    email = email.strip().lower()
    if await _by_email(db, email) is not None:
        raise ApiError(
            "conflict",
            status=409,
            message="Сотрудник с таким email уже существует",
            details={"reason": "email_taken"},
        )

    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=make_unreachable_hash(),
        full_name=full_name.strip(),
        role=role,
        is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # Гонка двух приглашений на один адрес: уникальный индекс — истина,
        # проверка выше — вежливость.
        await db.rollback()
        raise ApiError(
            "conflict",
            status=409,
            message="Сотрудник с таким email уже существует",
            details={"reason": "email_taken"},
        ) from None

    await write_audit(
        db,
        user_id=actor.id,
        action="user.invited",
        entity="user",
        entity_id=str(user.id),
        details={"role": role, "by": "admin", "kind": "invite"},
    )
    await db.commit()
    await db.refresh(user)  # created_at приходит из server_default
    return await _issue_link(redis, user)


async def resend_invite(
    db: AsyncSession, redis: Redis, *, actor: User, user_id: uuid.UUID
) -> InviteIssued:
    """Перевыпустить ссылку приглашения (01 §3.3); старая гаснет.

    Только пока пароль не установлен. Если установлен — это уже не
    «потерялась ссылка», а сброс пароля, у него своя ручка и свои последствия
    (обрыв сессий), молча подменять одно другим нельзя.
    """
    user = await _get_managed_user(db, user_id)
    if is_password_set(user.password_hash):
        raise ApiError(
            "unprocessable",
            status=422,
            message="Пароль уже установлен — нужна не новая ссылка, а сброс пароля",
            details={"reason": "password_already_set"},
        )
    _assert_active(user, what="перевыпустить ссылку")

    await write_audit(
        db,
        user_id=actor.id,
        action="user.invited",
        entity="user",
        entity_id=str(user.id),
        details={"role": user.role, "by": "admin", "kind": "resend"},
    )
    await db.commit()
    return await _issue_link(redis, user)


async def reset_password(
    db: AsyncSession, redis: Redis, *, user_id: uuid.UUID, actor: User | None = None
) -> InviteIssued:
    """Выслать сотруднику новую ссылку установки пароля (01 §3.3, 14 §2.2).

    Публичная функция сервиса: её же зовёт действие «Выслать новую ссылку» из
    центра уведомлений по заявке сотрудника «Не помню пароль» — там
    администратор нажимает одну кнопку, а не ходит в консоль.

    Старый пароль перестаёт работать сразу: `password_hash` заменяется
    недостижимой заглушкой, иначе `POST /auth/invite/accept` отказал бы со
    словами «пароль уже установлен» и ссылка была бы бесполезной. Учётная
    запись возвращается в состояние «ждёт пароля», и все её сессии рвутся —
    держать открытой сессию учётки, у которой больше нет пароля, нельзя.

    ``actor=None`` — действие системы (журнал получит `user_id = NULL`).
    """
    user = await _get_managed_user(db, user_id)
    _assert_active(user, what="сбросить пароль")

    user.password_hash = make_unreachable_hash()
    await write_audit(
        db,
        user_id=actor.id if actor else None,
        action="user.invited",
        entity="user",
        entity_id=str(user.id),
        details={"role": user.role, "by": "admin" if actor else "system", "kind": "password_reset"},
    )
    await db.commit()
    link = await _issue_link(redis, user)
    await revoke_sessions(redis, user.id, reason="password_reset", close_code=WS_CLOSE_LOGOUT)
    return link


async def update_user(
    db: AsyncSession,
    redis: Redis,
    *,
    actor: User,
    user_id: uuid.UUID,
    full_name: str | None = None,
    email: str | None = None,
    role: str | None = None,
    handles_conversations: bool | None = None,
    department: str | None = None,
    color: str | None = None,
) -> User:
    """Сменить имя и/или роль (01 §3.4). Смена роли действует немедленно.

    «Немедленно» — про бэкенд буквально: право проверяется по строке БД на
    каждом запросе (`api/deps.py`), поэтому понижённый админ получает 403 уже
    на следующем вызове. А вот открытый WebSocket несёт снимок роли, сделанный
    на connect'е (08 §5.3), и сам по себе про понижение не узнает — поэтому
    сокет закрывается кодом 4401: клиент возьмёт новый тикет и переподключится
    уже с новой ролью.
    """
    user = await _get_managed_user(db, user_id)
    old_role = user.role
    role_changed = role is not None and role != old_role
    if role_changed and user.id == actor.id:
        # Симметрично запрету отключить самого себя. Обе операции лишают
        # человека доступа, и обе — необратимо своими руками: сняв с себя роль
        # администратора, вернуть её он уже не может, потому что для этого
        # нужно право, которое он только что отдал. Настоящий случай: владелец
        # системы понизил себя и остался без управления сотрудниками.
        #
        # Проверка стоит ДО `_assert_admin_remains` намеренно: тому важно,
        # останется ли в системе хоть один админ, и при втором администраторе
        # он бы это понижение пропустил — то есть ровно в самом частом случае
        # не сработал бы.
        raise ApiError(
            "self_role_change",
            status=409,
            message="Нельзя сменить роль самому себе — попросите другого администратора",
            details={"reason": "self_role_change"},
        )
    if role_changed:
        await _assert_admin_remains(db, user, what="понизить роль")

    if full_name is not None:
        user.full_name = full_name.strip()
    if email is not None:
        # ⚠ ПОЧТА — ЛОГИН. Проверка занятости здесь обязательна: без неё запрос
        # уходит в БД и падает уникальным индексом, а человек видит «внутренняя
        # ошибка» вместо «адрес занят». Сравниваем так же, как ищет вход, —
        # регистронезависимо, иначе Ivan@ и ivan@ окажутся разными адресами на
        # правке и одним на входе.
        новый = email.strip().lower()
        if новый != (user.email or "").lower():
            занят = await _by_email(db, новый)
            if занят is not None and занят.id != user.id:
                raise ApiError(
                    "email_taken",
                    status=409,
                    message="Сотрудник с таким email уже существует",
                    details={"reason": "email_taken"},
                )
            старый = user.email
            user.email = новый
            # Смена логина — не косметика: после неё прежний адрес перестаёт
            # пускать в систему. Такое обязано остаться в журнале с обоими
            # адресами, иначе на вопрос «почему он не может войти» ответить
            # нечем.
            await write_audit(
                db,
                user_id=actor.id,
                action="user.email_changed",
                entity="user",
                entity_id=str(user.id),
                details={"from": старый, "to": новый},
            )
    if handles_conversations is not None and handles_conversations != user.handles_conversations:
        user.handles_conversations = handles_conversations
        # Вывод человека из работы с диалогами меняет состав смены для всех:
        # он исчезает из очереди, из автораздачи и из списка «кому передать».
        # Такое не должно происходить анонимно.
        await write_audit(
            db,
            user_id=actor.id,
            action="user.conversations_toggled",
            entity="user",
            entity_id=str(user.id),
            details={"handles_conversations": handles_conversations},
        )
    if color is not None:
        # пустая строка = снять цвет (симметрично отделу)
        user.color = color or None
    if department is not None:
        # Пустая строка — «убрать отдел»: снять его человек должен уметь так
        # же, как поставить.
        #
        # ⚠ ЧИСТИМ ПРИ ЗАПИСИ, А НЕ ПРИ ПОКАЗЕ (04.09). С этого дня отдел
        # виден рядом с именем на всех экранах, и на боевых данных рядом
        # живут «Диспетчер МНЧ» и «Диспетчер  МНЧ» с двумя пробелами: для
        # человека это один отдел, для строки — два. Показ схлопывает
        # пробелы у себя (`user_ref`), но там это последняя защита; чинить
        # надо источник, иначе поле так и останется с двумя написаниями
        # одного отдела, а по нему ещё будут искать.
        user.department = normalize_department(department)
    if role_changed and role is not None:
        user.role = role
        await write_audit(
            db,
            user_id=actor.id,
            action="user.role_changed",
            entity="user",
            entity_id=str(user.id),
            details={"from": old_role, "to": role},
        )
    await db.commit()

    if role_changed:
        # block_access=False — см. docstring revoke_sessions: пометка
        # `revoked_users` в её сегодняшнем виде (проверка «ключ есть») закрыла
        # бы человеку и повторный вход на 15 минут, а роль на бэкенде и так
        # читается из БД. Сокет закрываем, refresh-цепочку рвём.
        await revoke_sessions(
            redis,
            user.id,
            reason="role_changed",
            close_code=WS_CLOSE_RELOGIN,
            block_access=False,
        )
    return user


async def deactivate_user(
    db: AsyncSession, redis: Redis, *, actor: User, user_id: uuid.UUID
) -> User:
    """Отключить сотрудника (01 §3.5) — со сносом всех его живых сессий.

    Диалоги НЕ переназначаются: руководитель находит их фильтром «Менеджер» и
    передаёт руками, чтобы не потерять контекст (01 §3.5, 11 §2.5.2).

    Повторный вызов на уже отключённом — не ошибка: журнал второй записью не
    засоряется, а сессии рвутся снова. Это осознанно: «отключён, но всё ещё
    сидит» — состояние, из которого администратору нужен выход одной кнопкой.
    """
    user = await _get_managed_user(db, user_id)
    if user.id == actor.id:
        raise ApiError(
            "self_deactivation",
            status=409,
            message="Нельзя отключить самого себя — попросите другого администратора",
            details={"reason": "self_deactivation"},
        )

    if user.is_active:
        await _assert_admin_remains(db, user, what="отключить сотрудника")
        user.is_active = False
        await write_audit(
            db,
            user_id=actor.id,
            action="user.deactivated",
            entity="user",
            entity_id=str(user.id),
            details={"role": user.role},
        )
        await db.commit()

    await revoke_sessions(redis, user.id, reason="deactivated", close_code=WS_CLOSE_LOGOUT)
    return user


async def set_password(
    db: AsyncSession, redis: Redis, *, actor: User, user_id: uuid.UUID, password: str
) -> User:
    """Администратор задаёт сотруднику пароль НАПРЯМУЮ.

    Рядом уже есть «выслать ссылку» (:func:`reset_password`), и она лучше по
    безопасности: пароль знает только сам сотрудник. Но она требует, чтобы
    человек открыл почту и прошёл по ссылке, а на практике администратор
    заводит людей рядом с собой и говорит пароль вслух: «вот твой вход,
    поменяешь потом». Ссылка в этот сценарий не укладывается, и без прямой
    установки заведение сотрудника превращается в переписку.

    ЧТО ДЕЛАЕМ ВЗАМЕН БЕЗОПАСНОСТИ ССЫЛКИ:

    * пароль не короче десяти символов — как и при приёме приглашения;
    * все живые сессии сотрудника рвутся: если пароль меняют потому, что
      «кажется, кто-то его знает», смена без обрыва сессий бесполезна;
    * в журнал пишется, что пароль поставил администратор, а не сам человек.
      Через полгода вопрос «кто знал этот пароль» разрешается только этим.

    Себе так поставить нельзя: для своего пароля есть смена со вводом
    текущего, и она надёжнее — не оставляет способа сменить пароль, зная
    только чужой незапертый ноутбук.
    """
    user = await _get_managed_user(db, user_id)
    if user.id == actor.id:
        raise ApiError(
            "unprocessable",
            "Свой пароль меняют в профиле — с вводом текущего",
            status=422,
            details={"reason": "use_own_profile"},
        )

    user.password_hash = hash_password(password)
    await write_audit(
        db,
        user_id=actor.id,
        action="user.password_changed",
        entity="user",
        entity_id=str(user.id),
        details={"by": "admin"},
    )
    await db.commit()

    await revoke_sessions(redis, user.id, reason="password_set", close_code=WS_CLOSE_LOGOUT)
    return user


async def delete_user(db: AsyncSession, redis: Redis, *, actor: User, user_id: uuid.UUID) -> User:
    """Удалить сотрудника (требование заказчика от 7 августа).

    УДАЛЕНИЕ — ОТМЕТКА, А НЕ ``DELETE``. Настоящее удаление снесло бы вместе
    с человеком его сообщения — переписку с клиентами. Годовой отчёт стал бы
    анонимным, а в старых диалогах вместо имени появилось бы «Сотрудник»:
    клиент видит подпись менеджера, и она обязана остаться правдой и через
    год. Поэтому строка остаётся, а из всех списков человек исчезает.

    ЧЕМ ОТЛИЧАЕТСЯ ОТ ОТКЛЮЧЕНИЯ. Отключённый — временное состояние: отпуск,
    разбираемся с доступом, вернётся. Он виден под галочкой «показывать
    отключённых» и включается одной кнопкой. Удалённый не виден нигде и не
    включается.

    ЕГО ДИАЛОГИ ВОЗВРАЩАЮТСЯ В ОЧЕРЕДЬ — и это главное отличие от
    отключения, где их сознательно оставляют для ручной передачи. Отключённый
    вернётся и продолжит; удалённый не вернётся никогда, и оставить клиента
    «за ним» значит потерять клиента. Закрытые не трогаем: там уже ничего не
    ждут.
    """
    from app.services import inbox as inbox_svc

    # allow_deleted=True: повторное удаление обязано остаться идемпотентным
    # (ниже оно вернёт строку как есть), иначе кнопка «Удалить» отвечала бы
    # 404 на собственный результат.
    user = await _get_managed_user(db, user_id, allow_deleted=True)
    if user.id == actor.id:
        raise ApiError(
            "self_deletion",
            status=409,
            message="Нельзя удалить самого себя — попросите другого администратора",
            details={"reason": "self_deletion"},
        )
    if user.deleted_at is not None:
        return user  # повтор — не ошибка: кнопка одна, состояние конечное

    await _assert_admin_remains(db, user, what="удалить сотрудника")

    open_conversations = list(
        (
            await db.execute(
                sa.select(Conversation).where(
                    Conversation.assignee_id == user.id,
                    Conversation.status != "closed",
                )
            )
        )
        .scalars()
        .all()
    )
    for conv in open_conversations:
        inbox_svc.return_to_queue(conv, keep_declines=False)
        inbox_svc.clear_auto_assignment(conv)
    # ⚠ КАДРЫ — ПАЧКОЙ, А НЕ ПО ОДНОМУ (28.08).
    #
    # Здесь стоял поштучный `inbox_frame_addressed` внутри цикла, а он делает
    # свой `_load_related` и свой запрос допущенных — пять-шесть рейсов до базы
    # на КАЖДЫЙ диалог. Предела у выборки выше нет: у диспетчера с тремя сотнями
    # открытых диалогов удаление превращалось в полторы тысячи запросов внутри
    # одного запроса ручки. Ни ошибки, ни строчки в журнале — администратор
    # смотрит на крутящуюся кнопку и жмёт её второй раз.
    #
    # Кадры собираются ДО commit'а, публикуются ПОСЛЕ (08 §8.1) — без них
    # диалоги удалённого висели невидимыми до перезагрузки страниц (аудит
    # синхронизации 16.08; образец — reclaim.py). Список допущенных внутри
    # обязателен (аудит 19.08): без него строка очереди чужого канала уезжает
    # всем менеджерам.
    кадры_очереди = await inbox_svc.inbox_frames_addressed(db, open_conversations)
    frames: list[dict[str, Any]] = [
        {
            "inbox": кадр,
            "patch": {
                "conversation_id": str(conv.id),
                "patch": {"status": conv.status, "assignee": None, "in_inbox": True},
            },
        }
        for conv, кадр in zip(open_conversations, кадры_очереди, strict=True)
    ]

    # ⚠ И СНИМАЕМ СО ВСЕХ КАНАЛОВ, В ТОЙ ЖЕ ТРАНЗАКЦИИ. Без этого строка связи
    # переживала человека: назначить его заново нельзя, а форма назначения
    # операторов из-за него переставала сохраняться целиком — 14 каналов из 14
    # оказались заперты (жалоба владельца 03.09).
    from app.services import account_operators as ops_svc  # noqa: PLC0415 — цикл импортов

    освобождённые = await ops_svc.release_user_from_channels(db, user, actor=actor)

    user.deleted_at = datetime.now(UTC)
    user.is_active = False
    await write_audit(
        db,
        user_id=actor.id,
        action="user.deleted",
        entity="user",
        entity_id=str(user.id),
        details={
            "role": user.role,
            "returned_conversations": len(open_conversations),
            "released_channels": len(освобождённые),
        },
    )
    await db.commit()

    for frame in frames:
        await publish_inbox_new(
            redis,
            frame["inbox"]["conversation"],
            eligible_operator_ids=frame["inbox"].get("eligible"),
        )
        await publish_event(redis, "conversation:updated", frame["patch"])

    await revoke_sessions(redis, user.id, reason="deleted", close_code=WS_CLOSE_LOGOUT)
    return user


async def activate_user(db: AsyncSession, redis: Redis, *, actor: User, user_id: uuid.UUID) -> User:
    """Включить сотрудника обратно (01 §3.5).

    УДАЛЁННОГО НЕ ВКЛЮЧАЕМ. Отключение — пауза, удаление — конец: диалоги
    удалённого уже вернулись в очередь, а сам он исчез из всех списков. Раньше
    эта ручка про `deleted_at` не знала и честно ставила `is_active = true` —
    человек снова заходил в систему и читал переписку клиентов, оставаясь
    невидимым: в «Команде» его нет, значит и отключить его обратно нечем.
    Попасть сюда просто — открытый экран «Команда» у второго администратора
    ещё показывает строку, удалённую минуту назад, и кнопка «Включить» на ней
    живая. Отказ отдельный, а не общий 404: администратор должен понять, что
    сотрудника надо заводить заново, а не что он «куда-то делся».

    Денилист access-токенов снимается сразу: иначе включённый в ту же минуту
    сотрудник упирался бы в 401 до конца 15-минутного окна.
    """
    user = await _get_managed_user(db, user_id, allow_deleted=True)
    if user.deleted_at is not None:
        raise ApiError(
            "user_deleted",
            status=409,
            message=(
                "Сотрудник удалён — включить его нельзя. "
                "Заведите учётную запись заново приглашением."
            ),
            details={"reason": "user_deleted"},
        )
    if not user.is_active:
        user.is_active = True
        await write_audit(
            db,
            user_id=actor.id,
            action="user.activated",
            entity="user",
            entity_id=str(user.id),
            details={"role": user.role},
        )
        await db.commit()

    await clear_revocation(redis, user.id)
    return user


# --- действие центра уведомлений (14 §3) -------------------------------------


async def issue_password_reset_action(
    db: AsyncSession, redis: Redis, *, entity_id: str | None, actor: User
) -> dict[str, Any]:
    """Кнопка «Выслать новую ссылку» под заявкой сотрудника (14 §2.2, §3).

    Контракт действий центра уведомлений (`app/services/notifications.py`):
    `(db, redis, *, entity_id, actor) -> dict`, результат уезжает клиенту в
    поле `result`. `entity_id` — идентификатор сотрудника из уведомления.

    Право проверяет вызывающий (`users:manage`), но валидность цели — наша:
    уведомление живёт 90 дней и вполне может пережить сотрудника.
    """
    try:
        user_id = uuid.UUID(entity_id) if entity_id else None
    except ValueError:
        user_id = None
    if user_id is None:
        raise ApiError(
            "unprocessable",
            status=422,
            message="Заявка не привязана к сотруднику — сбросьте пароль на экране «Команда»",
            details={"reason": "entity_missing"},
        )

    issued = await reset_password(db, redis, user_id=user_id, actor=actor)
    return {
        "user": serialize_user(issued.user),
        "invite_url": issued.url,
        "invite_expires_at": iso(issued.expires_at),
    }


# --- вспомогательное ---------------------------------------------------------


def _assert_active(user: User, *, what: str) -> None:
    if user.is_active:
        return
    raise ApiError(
        "unprocessable",
        status=422,
        message=f"Нельзя {what}: сотрудник отключён. Сначала включите учётную запись.",
        details={"reason": "user_inactive"},
    )


async def _issue_link(redis: Redis, user: User) -> InviteIssued:
    """Одноразовый токен + ссылка. Выпускается ПОСЛЕ commit'а: токен, ведущий
    на несуществующего пользователя, — мусор в Redis и 404 у сотрудника."""
    token, expires_at = await issue_invite(redis, str(user.id))
    return InviteIssued(user=user, url=invite_url(token), expires_at=expires_at)
