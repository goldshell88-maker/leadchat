"""FastAPI dependencies (08 §1.2): get_db, get_redis, get_current_user,
require_permission."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core.errors import ApiError
from app.core.rbac import ROLE_PERMISSIONS
from app.core.security import decode_access_token
from app.db import session as db_mod
from app.models import User
from app.services.sessions import access_revoked_at, выписан_после

bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Сессия на запрос. Единственный путь освобождения — ``db.session``.

    Логику «rollback незакрытой транзакции + close» намеренно НЕ дублируем:
    два пути возврата соединения в пул рано или поздно разъезжаются, а цена
    расхождения — `idle in transaction` на проде (05 §8).
    """
    async for session in db_mod.db_session():
        yield session  # transactions are opened by the calling code (08 §8.1)


def get_redis() -> Redis:
    return redis_mod.get_client()  # process singleton


async def get_current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    if cred is None:
        raise ApiError("unauthorized", status=401)
    payload = decode_access_token(cred.credentials)  # exp/signature -> 401 unauthorized
    # Отзыв сессий (01 §1.2): подписанный токен живёт 15 минут, и без этой
    # проверки «Отключить» оставляло бы REST открытым до его истечения. Сверка
    # по моменту выдачи, а не «пометка есть»: токен, выписанный после отзыва, —
    # честный перезаход. При сомнении — отказ (`выписан_после`).
    отозвано = await access_revoked_at(redis, payload["sub"])
    if отозвано is not None and not выписан_после(payload, отозвано):
        raise ApiError("unauthorized", status=401)  # instant logout (01 §1.2)
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise ApiError("forbidden", status=403)
    # ⚠ ЖУРНАЛ ЗАПРОСОВ УЗНАЁТ, ЧЕЙ ЭТО ЗАПРОС, ТОЛЬКО ОТСЮДА, И ТОЛЬКО ЧЕРЕЗ
    # `scope`. Строку `http.request` пишет middleware снаружи, а токен
    # разбирается здесь. Контекстная переменная сюда не годится: Starlette
    # выполняет обработчик в ОТДЕЛЬНОЙ задаче, и всё, что поставлено внутри,
    # снаружи уже не видно (проверено — строка приезжала без имени). `scope`
    # же один и тот же объект на весь запрос.
    request.scope["lc_user_id"] = str(user.id)
    return user


def has_permission(user: User, perm: str) -> bool:
    """Матрица прав как предикат — для сериализаторов и ветвлений в ручках.

    Нужна там, где право решает не «пустить/не пустить», а «что отдать»:
    напр. лента сообщений без заметок для observer (01 §6.1, `notes:read`).
    Всегда спрашиваем матрицу, а не сравниваем роль со строкой — иначе новая
    роль тихо получит чужие данные.
    """
    return perm in ROLE_PERMISSIONS.get(user.role, frozenset())


def require_permission(perm: str) -> Callable[..., Awaitable[User]]:
    """Dependency factory: require_permission('messages:send') — 01 §12.

    The role is always the DB row's role (via get_current_user), never the JWT.
    """

    async def dep(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, perm):
            if perm == "messages:send" and user.role == "head":
                # "view mode" banner on the frontend (01 §12)
                raise ApiError("read_only_role", status=403)
            raise ApiError("forbidden", status=403)
        return user

    return dep
