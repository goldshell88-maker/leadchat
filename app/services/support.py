"""Заявки сотрудников администратору + мост в центр уведомлений (14 §2.2).

СЛОВО «ЗАЯВКА», А НЕ «ОБРАЩЕНИЕ». Словарь терминов (10 §7.2) запрещает
«обращение» — оно уже занято как неверный синоним «диалога», и именно на этой
подмене интерфейс разъехался в 28 местах. Здесь речь про другое: сотрудник
просит помощи у своего администратора. 14 §4 называет это заявкой, так и
называем — иначе одно слово означало бы две разные сущности сразу.

Две разные вещи в одном модуле — сознательно:

1. **Мост в центр уведомлений.** Сам центр (таблица ``notifications``, запись,
   подавление повторов, рассылка в WS) живёт в ``app/services/notifications.py``
   и пишется в соседней зоне. Здесь — только тонкий адаптер
   :func:`send_notification` и описание того, ЧТО именно мы просим показать
   человеку (:class:`NotificationDraft`). Пока помощника нет, адаптер честно
   пишет всё уведомление в лог и возвращает ``False`` — вызывающая логика от
   этого не падает (тот же приём, что у ``scheduler/main.py`` с
   ``refresh_due_accounts``).

2. **Заявки сотрудников** (14 §2.2): заявка на сброс пароля и сообщение
   администратору. Для сотрудника это единственный способ достучаться до
   администратора — почты в MVP нет, восстановление пароля идёт только через
   человека.

Тексты уведомлений здесь — человеческие. Ни кода ошибки, ни ``kind`` в
заголовке: заголовок читает администратор, а не grep (14 §3).
"""

from __future__ import annotations

import hashlib
import importlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import User

log = structlog.get_logger("app.support")

# --- важность (14 §4, поле severity) -----------------------------------------
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

# --- виды событий каталога 14 §2.2 -------------------------------------------
KIND_PASSWORD_RESET = "support.password_reset"
KIND_ADMIN_MESSAGE = "support.message"


# =============================================================================
# Мост в центр уведомлений
# =============================================================================


@dataclass(frozen=True)
class NotificationDraft:
    """Одно уведомление до того, как оно попало в базу.

    Поля — ровно аргументы ``notify`` из центра уведомлений (14 §4). Кнопки
    здесь НЕТ намеренно: действие у события одно на всю систему и живёт в
    каталоге центра (``notifications.KINDS`` → ``ACTIONS``). Две записи про
    одну и ту же кнопку разъехались бы на первой же правке подписи.
    """

    kind: str
    severity: str
    title: str
    body: str
    # None — политику склейки выбирает каталог центра (например «support.message»
    # не склеивается никогда: каждая заявка сотрудника — своя).
    dedup_key: str | None = None
    audience: str | None = "admin"  # пусто → адресат конкретный (recipient_id)
    recipient_id: uuid.UUID | None = None
    entity_type: str | None = None
    entity_id: str | None = None

    def as_kwargs(self) -> dict[str, Any]:
        return asdict(self)


# Точка входа центра уведомлений (14 §4). Берём именно ``notify_now`` —
# «notify + commit + доставка в браузер»: у всех вызывающих из этой зоны
# уведомление и есть вся работа, отдельной бизнес-транзакции, к которой его
# надо было бы прицепить, нет.
NOTIFY_IMPL_MODULE = "app.services.notifications"
NOTIFY_IMPL_ATTR = "notify_now"


def resolve_notify_impl() -> Any | None:
    """``notify_now`` из центра уведомлений или None, пока модуля нет."""
    try:
        module = importlib.import_module(NOTIFY_IMPL_MODULE)
    except ImportError:
        return None
    impl = getattr(module, NOTIFY_IMPL_ATTR, None)
    return impl if callable(impl) else None


async def send_notification(
    db: AsyncSession, redis: Redis, draft: NotificationDraft, *, now: datetime | None = None
) -> bool:
    """Отдать уведомление центру. ``True`` — приняли, ``False`` — центра нет.

    Запись, коммит и публикацию в браузер делает центр (``notify_now``).

    ``now`` — момент, которым центр штампует запись. В бою его не передают: там
    «сейчас» одно на всех. Передаёт его тот, кто САМ живёт по переданному
    времени, — сторож планировщика: он решает, пора ли повторять тревогу, по
    возрасту уже существующей строки, и если строку штампует один час, а
    решение принимает другой, лестница повторов считает по разнице между
    настоящим временем и выдуманным. В бою это незаметно (часы совпадают), а
    значит и сломается однажды молча.

    Ошибку центра НЕ пробрасываем: уведомление о поломке не должно ронять
    операцию, из-за которой оно возникло (провалившаяся проверка планировщика
    или заявка сотрудника на сброс пароля важнее самого уведомления).
    """
    impl = resolve_notify_impl()
    if impl is None:
        # Центра ещё нет — уведомление целиком уходит в лог, чтобы оно не
        # исчезло бесследно (это ровно та «неделя молчания» из 14 §1).
        log.warning(
            "notify.center_unavailable",
            kind=draft.kind,
            severity=draft.severity,
            title=draft.title,
            body=draft.body,
            dedup_key=draft.dedup_key,
        )
        return False
    kwargs = draft.as_kwargs()
    if now is not None:
        kwargs["now"] = now
    try:
        await impl(db, redis, **kwargs)
    except Exception:
        log.exception("notify.failed", kind=draft.kind, dedup_key=draft.dedup_key)
        return False
    log.info("notify.sent", kind=draft.kind, severity=draft.severity, dedup_key=draft.dedup_key)
    return True


# =============================================================================
# Ограничение частоты (тот же приём, что в app/services/login_guard.py)
# =============================================================================

# Отличие от login_guard осознанное. Там окно СКОЛЬЗЯЩЕЕ (TTL продлевается на
# каждой неудаче) — это блокировка подбора, и вечно висеть она права имеет.
# Здесь окно ФИКСИРОВАННОЕ: весь офис выходит в интернет с одного адреса
# (приёмка, блокер Б3), и скользящее окно означало бы, что один скрипт лишает
# коллег возможности попросить новый пароль до бесконечности.
#
# Счётчики живут в СВОИХ ключах, а не в `login_fail:*`. Общий счётчик означал
# бы, что запрос «не помню пароль» приближает блокировку входа тому же
# человеку — то есть попытка починиться делает хуже.

PASSWORD_RESET_PER_EMAIL = 3  # за час на учётку
PASSWORD_RESET_PER_IP = 20  # за час на адрес; в офисе адрес общий (Б3)
PASSWORD_RESET_WINDOW_SECONDS = 3600
ADMIN_MESSAGE_PER_USER = 5  # 5 в час, задание §2.2
ADMIN_MESSAGE_WINDOW_SECONDS = 3600


def email_digest(email: str) -> str:
    """Email — PII, в ключ Redis кладём хэш (как в login_guard)."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


async def rate_limit_hit(redis: Redis, key: str, *, limit: int, window_seconds: int) -> int | None:
    """Засчитать попытку. Вернуть secs до конца окна, если лимит исчерпан."""
    count = int(await redis.incr(key))
    if count == 1:
        await redis.expire(key, window_seconds)
    if count > limit:
        ttl = int(await redis.ttl(key))
        return max(ttl, 1)
    return None


async def _consume_password_reset_limits(redis: Redis, email: str, ip: str) -> int | None:
    """Оба счётчика тикают ВСЕГДА — и по email, и по адресу.

    Если выходить по первому сработавшему, лимит обходится чередованием: по
    известному email счётчик уже красный, а адресный так и не растёт.
    """
    by_email = await rate_limit_hit(
        redis,
        f"support:pwreset:email:{email_digest(email)}",
        limit=PASSWORD_RESET_PER_EMAIL,
        window_seconds=PASSWORD_RESET_WINDOW_SECONDS,
    )
    by_ip = await rate_limit_hit(
        redis,
        f"support:pwreset:ip:{ip}",
        limit=PASSWORD_RESET_PER_IP,
        window_seconds=PASSWORD_RESET_WINDOW_SECONDS,
    )
    retry = max((v for v in (by_email, by_ip) if v is not None), default=None)
    return retry


# =============================================================================
# Заявка на сброс пароля (14 §2.2)
# =============================================================================


async def _user_by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.strip().lower())
    return (await db.execute(stmt)).scalar_one_or_none()


def password_reset_draft(user: User) -> NotificationDraft:
    """Уведомление администраторам о заявке сотрудника.

    Кнопка «Выслать новую ссылку» приходит из каталога центра (``KINDS``
    → ``ACTIONS``), поэтому текст для ОТКЛЮЧЁННОЙ учётки написан так, чтобы
    администратор не нажал её на автомате: одно нажатие вернуло бы доступ
    уволенному.
    """
    if user.is_active:
        body = (
            f"{user.full_name} ({user.email}) не может войти и просит новую ссылку. "
            "Ссылка одноразовая, действует трое суток."
        )
    else:
        body = (
            f"ОСТОРОЖНО: учётная запись {user.full_name} ({user.email}) отключена, "
            "а доступ просят. Сначала решите, работает ли человек у вас, — "
            "и только потом высылайте ссылку."
        )
    return NotificationDraft(
        kind=KIND_PASSWORD_RESET,
        severity=WARNING,  # человек не может работать, но система цела
        title=f"Не может войти: {user.full_name}",
        body=body,
        # Один ключ на сотрудника: десять нажатий «не помню пароль» — это одна
        # строка со счётчиком повторов, а не десять (14 §4).
        dedup_key=f"{KIND_PASSWORD_RESET}:{user.id}",
        entity_type="user",
        entity_id=str(user.id),
    )


async def request_password_reset(db: AsyncSession, redis: Redis, *, email: str, ip: str) -> None:
    """Заявка со страницы входа. Вызывающий отвечает один и тот же ответ ВСЕГДА.

    Порядок важен: лимиты считаются ДО поиска учётки. Иначе «429 только на
    существующих» само по себе становится способом узнать, кто у нас работает.
    """
    retry_after = await _consume_password_reset_limits(redis, email, ip)
    if retry_after is not None:
        log.info("support.password_reset_limited", email_hash=email_digest(email), remote_addr=ip)
        raise ApiError("rate_limited", status=429, details={"retry_after_sec": retry_after})

    user = await _user_by_email(db, email)
    # Служебность — по колонке: заявка на сброс пароля от настоящего человека
    # с почтой на `.local` раньше молча уходила в никуда.
    known = user is not None and not user.is_service
    if user is not None and known:
        # commit и доставку в браузер делает центр (notify_now, 14 §4).
        await send_notification(db, redis, password_reset_draft(user))
    log.info(
        "support.password_reset_requested",
        email_hash=email_digest(email),
        remote_addr=ip,
        known=known,  # в логе — да, в ответе клиенту — никогда
    )


# =============================================================================
# Сообщение администратору (14 §2.2)
# =============================================================================


def admin_message_draft(user: User, *, subject: str, text: str) -> NotificationDraft:
    """Ключ подавления НЕ задаём: в каталоге центра у ``support.message``
    политика «не склеивать никогда» — каждая заявка сотрудника своя, и
    решает это владелец каталога, а не отправитель."""
    return NotificationDraft(
        kind=KIND_ADMIN_MESSAGE,
        severity=INFO,
        title=f"{user.full_name}: {subject}",
        body=text,
        entity_type="user",
        entity_id=str(user.id),
    )


async def submit_admin_message(
    db: AsyncSession, redis: Redis, *, user: User, subject: str, text: str
) -> None:
    """Форма «Написать администратору» из профиля (14 §2.2)."""
    retry_after = await rate_limit_hit(
        redis,
        f"support:message:user:{user.id}",
        limit=ADMIN_MESSAGE_PER_USER,
        window_seconds=ADMIN_MESSAGE_WINDOW_SECONDS,
    )
    if retry_after is not None:
        log.info("support.message_limited", user_id=str(user.id))
        raise ApiError("rate_limited", status=429, details={"retry_after_sec": retry_after})

    await send_notification(db, redis, admin_message_draft(user, subject=subject, text=text))
    log.info("support.message_sent", user_id=str(user.id), subject_len=len(subject))
