"""Users — управление сотрудниками (01 §3, экран «Команда» 11 §4.2).

Два разных права в одном модуле, и это не небрежность:

* `GET /users/assignable` — право `conversations:manage` (admin/head/manager).
  Менеджеру нужен список коллег для модалки «Передать коллеге» (11 §2.4), но
  не нужен доступ к управлению персоналом.
* всё остальное — право `users:manage`, а оно есть **только у admin**
  (01 §12, DESIGN §5.1). Заведение людей, роли и отключение — это раздача
  доступа к чужой переписке, у руководителя такого права нет.

До спринта 8 «всего остального» здесь не было вовсе: сотрудник заводился
командой в консоли, а отключение не доводилось до конца — строка в БД
менялась, но открытые сессии человека продолжали жить. Вся логика (включая
обрыв сессий) лежит в `app/services/users.py` и `app/services/sessions.py`,
ручки остаются тонкими.

Пути продублированы намеренно: 01 §3.2–3.3 называют их `POST /users` и
`POST /users/{id}/invite`, задание спринта — `POST /users/invite` и
`POST /users/{id}/resend-invite`. Обе пары ведут в одну функцию; лишний
синоним дешевле, чем 404 у фронта из-за расхождения в документе.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.models import User
from app.schemas.users import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InviteIssuedOut,
    PageOut,
    Role,
    SetPasswordIn,
    UserAdminOut,
    UserEnvelopeOut,
    UserInviteIn,
    UserPatchIn,
    UsersPageOut,
)
from app.services import users as users_service
from app.services.conversations import operator_pool_conditions
from app.services.user_ref import normalize_department
from app.ws.events import iso
from app.ws.presence import presence_map, presence_status_map

router = APIRouter()


class AssignableUserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    role: str
    is_online: bool
    #: «на месте» | «отошёл» | None (не в сети).
    #:
    #: ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 01.09: «показывается, что люди онлайн в работе, а у
    #: кого-то просто вкладка открыта, у кого-то просто ПК включён».
    #:
    #: Причина была здесь. `is_online` отвечает на вопрос «приложение открыто и
    #: отвечает на пинги» — и у ОТОШЕДШЕГО он тоже true. То есть авто-«отошёл»
    #: (`frontend/.../автоОтошёл.ts`) исправно ставил статус, а этот список
    #: красил отошедшего тем же зелёным, что и работающего: замер на бою 01.09
    #: показал троих `away` и двоих `online` — в модалке все пятеро зелёные.
    #:
    #: Отошедшего НЕ ПРЯЧЕМ: он за столом, видит диалоги и отвечает, ему можно
    #: передать (тот же довод, что у `TeamUserDto.presence`). Врёт не наличие
    #: строки, а её цвет.
    presence: str | None = None
    # Всегда true в выдаче по умолчанию; смысл появляется при include_inactive=true —
    # фронт помечает такого сотрудника «отключён» и не даёт его выбрать целью.
    is_active: bool = True
    #: Отдел — подписью «(ОКК)» рядом с именем в окне «Передать диалог».
    #:
    #: ПРОСЬБА ВЛАДЕЛЬЦА 04.09. В списке тринадцать человек, половина из
    #: которых сидит в разных отделах на одном и том же типе обращений;
    #: выбирать «кому передать» по одному имени — значит регулярно попадать
    #: не в тот отдел. `None` — отдел не заполнен, скобок не будет.
    department: str | None = None


class AssignableUsersOut(BaseModel):
    items: list[AssignableUserOut]


@router.get("/users/assignable", response_model=AssignableUsersOut)
async def list_assignable_users(
    include_inactive: bool = Query(False),
    user: User = Depends(require_permission("conversations:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AssignableUsersOut:
    """Активные сотрудники, которым можно назначить диалог (01 §3.1).

    Только роли, умеющие отвечать клиенту (admin/manager) — head читает всё и
    так, но отвечать не может, observer тем более; назначение на них
    отбивается `422 assignee_cannot_chat` (01 §5.5), поэтому и в списке их нет.
    Без пагинации: сотрудников десятки, модалка фильтрует поиском на фронте.

    ``include_inactive=true`` (11 §2.5.2) добавляет отключённых: деактивация
    диалоги не переназначает (01 §3.5), и руководителю нужно фильтровать
    список именно по уволенным, чтобы найти осиротевшие диалоги. Целью
    назначения такой сотрудник всё равно не станет — `422 assignee_inactive`.

    Служебный smoke-пользователь (колонка ``is_service``, 07 §6) не попадает
    в выдачу никогда: робот в выпадашке «Ответственный» — это баг. Признак
    берётся из колонки, а не из домена почты: на `.local` живут и настоящие
    люди.
    """
    # Тот же пул, что у очереди и автораздачи (7.4): человек, выведенный из
    # работы с диалогами, не должен появляться и в списке «кому передать» —
    # иначе руководитель отдаст ему диалог руками, а система тут же вернёт его
    # в очередь как непринятый.
    # Служебного робота отсекает сам пул (`User.is_service`), а не догадка по
    # домену почты. Фильтр `is_service_email` стоял здесь с 12 августа как
    # признанная времянка: домен `.local` — обычный внутренний домен, и на нём
    # живут настоящие люди, которых этот список молча прятал.
    conds = operator_pool_conditions(include_inactive=include_inactive)
    rows = list(
        (await db.execute(select(User).where(*conds).order_by(User.full_name, User.id))).scalars()
    )
    # Ключ presence'а принадлежит WS-хабу (08 §5.5) — читаем его хелпером
    # оттуда же, одним MGET на всю модалку (01 §11.6).
    # Спрашиваем СТАТУС, а не «подключён ли»: `presence_map` возвращает bool и
    # «away» от «online» не отличает — на этом список и врал.
    статусы = await presence_status_map(redis, [u.id for u in rows])
    return AssignableUsersOut(
        items=[
            AssignableUserOut(
                id=u.id,
                full_name=u.full_name,
                role=u.role,
                # `is_online` оставлен со ПРЕЖНИМ смыслом — «приложение
                # открыто». Его читают и другие места; сузить его тихо значило
                # бы поменять смысл поля под теми, кто о правке не знает.
                is_online=статусы.get(u.id) is not None,
                presence=статусы.get(u.id),
                is_active=u.is_active,
                department=normalize_department(u.department),
            )
            for u in rows
        ]
    )


# --- управление сотрудниками (право `users:manage`, только admin) -------------


async def _envelope(redis: Redis, user: User) -> UserEnvelopeOut:
    online = await presence_map(redis, [user.id])
    row = users_service.serialize_user(user, is_online=online.get(user.id, False))
    return UserEnvelopeOut(user=UserAdminOut.model_validate(row))


def _invite_response(issued: users_service.InviteIssued) -> InviteIssuedOut:
    return InviteIssuedOut(
        user=UserAdminOut.model_validate(users_service.serialize_user(issued.user)),
        invite_url=issued.url,
        invite_expires_at=iso(issued.expires_at) or "",
    )


@router.get("/users", response_model=UsersPageOut)
async def list_users(
    q: str | None = Query(None, max_length=200, description="Поиск по имени или email"),
    role: Role | None = Query(None),
    is_active: bool | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UsersPageOut:
    """Список сотрудников с фильтрами «роль» и «активность» (01 §3.1)."""
    rows, total = await users_service.list_users(
        db, redis, q=q, role=role, is_active=is_active, limit=limit, offset=offset
    )
    return UsersPageOut(
        items=[UserAdminOut.model_validate(r) for r in rows],
        page=PageOut(limit=limit, offset=offset, total=total),
    )


@router.post("/users", response_model=InviteIssuedOut, status_code=201)
@router.post("/users/invite", response_model=InviteIssuedOut, status_code=201)
async def invite_user(
    body: UserInviteIn,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InviteIssuedOut:
    """Пригласить сотрудника: строка в БД + одноразовая ссылка (01 §3.2).

    Ссылка возвращается один раз и живёт 72 часа — почтового сервиса в MVP
    нет, администратор передаёт её человеку сам (11 §4.2). Потерялась —
    `POST /users/{id}/resend-invite`.
    """
    issued = await users_service.invite_user(
        db, redis, actor=user, email=body.email, full_name=body.full_name, role=body.role
    )
    return _invite_response(issued)


@router.post("/users/{user_id}/invite", response_model=InviteIssuedOut)
@router.post("/users/{user_id}/resend-invite", response_model=InviteIssuedOut)
async def resend_invite(
    user_id: uuid.UUID,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InviteIssuedOut:
    """Перевыпустить ссылку установки пароля; старая гаснет (01 §3.3)."""
    issued = await users_service.resend_invite(db, redis, actor=user, user_id=user_id)
    return _invite_response(issued)


@router.post("/users/{user_id}/reset-password", response_model=InviteIssuedOut)
async def reset_password(
    user_id: uuid.UUID,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InviteIssuedOut:
    """Выслать сотруднику новую ссылку установки пароля (01 §3.3, 14 §2.2).

    Эту же функцию сервиса зовёт действие «Выслать новую ссылку» в центре
    уведомлений — по заявке сотрудника «Не помню пароль». Старый пароль
    перестаёт работать сразу, сессии сотрудника рвутся.
    """
    issued = await users_service.reset_password(db, redis, user_id=user_id, actor=user)
    return _invite_response(issued)


@router.post("/users/{user_id}/set-password", response_model=UserEnvelopeOut)
async def set_user_password(
    user_id: uuid.UUID,
    body: SetPasswordIn,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserEnvelopeOut:
    """Задать сотруднику пароль напрямую (требование заказчика от 7 августа).

    Рядом есть «выслать ссылку», и она надёжнее — пароль знает только сам
    сотрудник. Но она требует, чтобы человек открыл почту, а администратор
    заводит людей рядом с собой и говорит пароль вслух. Без прямой установки
    заведение сотрудника превращается в переписку.

    Ответ — обычная карточка сотрудника, без пароля: он в системе больше
    нигде не появляется, включая ответ той ручки, которая его поставила.
    """
    updated = await users_service.set_password(
        db, redis, actor=user, user_id=user_id, password=body.password
    )
    return await _envelope(redis, updated)


@router.delete("/users/{user_id}", response_model=UserEnvelopeOut)
async def delete_user(
    user_id: uuid.UUID,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserEnvelopeOut:
    """Удалить сотрудника (требование заказчика от 7 августа).

    Не ``DELETE`` в базе: строка остаётся, чтобы его имя сохранилось в старых
    диалогах — клиент видит подпись менеджера, и она обязана остаться правдой
    и через год. Из всех списков человек исчезает, доступ закрывается, а его
    незакрытые диалоги возвращаются в очередь.
    """
    updated = await users_service.delete_user(db, redis, actor=user, user_id=user_id)
    return await _envelope(redis, updated)


@router.patch("/users/{user_id}", response_model=UserEnvelopeOut)
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatchIn,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserEnvelopeOut:
    """Имя, почта и/или роль (01 §3.4).

    Понижение последнего администратора — `409 last_admin`: система без
    админа не управляется вообще, включая возврат этой роли обратно.
    Занятая почта — `409 email_taken`, как и при приглашении.
    """
    updated = await users_service.update_user(
        db,
        redis,
        actor=user,
        user_id=user_id,
        full_name=body.full_name,
        email=body.email,
        role=body.role,
        handles_conversations=body.handles_conversations,
        department=body.department,
        color=body.color,
    )
    return await _envelope(redis, updated)


@router.post("/users/{user_id}/deactivate", response_model=UserEnvelopeOut)
async def deactivate_user(
    user_id: uuid.UUID,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserEnvelopeOut:
    """Отключить сотрудника — со сносом всех живых сессий (01 §3.5).

    Отказы: `409 self_deactivation` — себя, `409 last_admin` — последнего
    администратора.
    """
    updated = await users_service.deactivate_user(db, redis, actor=user, user_id=user_id)
    return await _envelope(redis, updated)


@router.post("/users/{user_id}/activate", response_model=UserEnvelopeOut)
async def activate_user(
    user_id: uuid.UUID,
    user: User = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserEnvelopeOut:
    """Включить сотрудника обратно (01 §3.5). Пароль остаётся прежним."""
    updated = await users_service.activate_user(db, redis, actor=user, user_id=user_id)
    return await _envelope(redis, updated)
