"""Auth endpoints (01 §2): login / refresh / logout / me / invite."""

import hashlib
import uuid

import structlog
from fastapi import APIRouter, Depends, Request, Response
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_redis
from app.core.config import settings
from app.core.errors import ACCOUNT_DISABLED_MESSAGE, ApiError
from app.core.rbac import permissions_for
from app.core.security import (
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    RefreshTokenInvalid,
    consume_invite,
    create_access_token,
    dummy_verify,
    hash_password,
    is_password_set,
    issue_refresh_token,
    peek_invite,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from app.models import User
from app.schemas.auth import (
    ChangeNameRequest,
    ChangePasswordRequest,
    HotkeysRequest,
    InviteAcceptRequest,
    InviteInfoOut,
    LoginRequest,
    LoginResponse,
    MeOut,
    UserOut,
)
from app.services.audit import write_audit
from app.services.login_guard import (
    lockout_retry_after,
    record_login_failure,
    reset_login_failures,
)
from app.services.notifications import notify_now
from app.services.sessions import (
    WS_CLOSE_RELOGIN,
    clear_revocation,
    revoke_sessions,
)

router = APIRouter()
log = structlog.get_logger("app.auth")


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(response: Response, token: str, remember: bool) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.refresh_ttl_days * 86400 if remember else None,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        # ⚠ STRICT, А НЕ LAX (01.09). Разница ровно одна: `lax` отправляет куку
        # при ПЕРЕХОДЕ ПО ССЫЛКЕ с чужого сайта, `strict` — никогда.
        #
        # Нашей куке первое не нужно вовсе: её читает единственный запрос —
        # обновление токена, и он всегда идёт из уже открытого приложения. То
        # есть послабление мы платили ни за что.
        #
        # CSRF закрыт был и раньше: на межсайтовый POST `lax` куку тоже не
        # отдаёт. Это сужение оставшейся щели, а не починка дыры.
        #
        # Десктоп от этого не страдает: он грузит фронт локально, его origin
        # чужой нашему домену, и при `lax` кука ему тоже не доставалась бы.
        samesite="strict",
    )


def _login_response(user: User) -> LoginResponse:
    return LoginResponse(
        access_token=create_access_token(user_id=str(user.id), role=user.role),
        expires_in=settings.jwt_access_ttl_seconds,
        user=UserOut.model_validate(user),
    )


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.lower())
    return (await db.execute(stmt)).scalar_one_or_none()


async def _audit_login_failure(
    db: AsyncSession, *, user: User | None, ip: str, reason: str
) -> None:
    """Неудачный вход в журнал (06 §0.3, 07 §A6).

    ``user_id``/``entity_id`` пустые, когда такого пользователя нет: строка
    журнала всё равно нужна — она про попытку, а не про сотрудника.
    Транзакция здесь своя: вызывающий сразу после этого делает raise.
    """
    await write_audit(
        db,
        user_id=user.id if user else None,
        action="auth.login",
        entity="user",
        entity_id=str(user.id) if user else None,
        details={"ip": ip, "result": "failure", "reason": reason},
    )
    await db.commit()


async def _should_log_lockout(redis: Redis, email: str, ttl: int) -> bool:
    """Залоченный вход пишем в журнал один раз за окно блокировки.

    Иначе неаутентифицированная ручка становится способом лить строки в
    audit_log: пока лок держится, счётчик попыток уже не растёт, и любое
    число запросов дало бы столько же записей (DESIGN §9).
    """
    marker = f"auth:lock_audited:{_email_hash(email)}"
    return bool(await redis.set(marker, "1", nx=True, ex=max(ttl, 1)))


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LoginResponse:
    email = body.email.lower()
    ip = _client_ip(request)

    retry_after = await lockout_retry_after(redis, email, ip)
    if retry_after is not None:
        if await _should_log_lockout(redis, email, retry_after):
            user = await _get_user_by_email(db, email)
            await _audit_login_failure(db, user=user, ip=ip, reason="locked")
            # 14 §2.2: администратор обязан узнать, что человек не может войти —
            # иначе сотрудник просто перестаёт работать и молчит. Уведомление
            # ровно одно на окно блокировки: мы уже под `_should_log_lockout`.
            #
            # Только для существующей учётки. Для выдуманного адреса уведомлять
            # нечего и НЕЛЬЗЯ: неаутентифицированная ручка превратилась бы в
            # способ засорить центр уведомлений с улицы.
            if user is not None:
                # ⚠ СБОЙ УВЕДОМЛЕНИЯ НЕ ДОЛЖЕН СТОИТЬ НИ ОТВЕТА, НИ САМОГО УВЕДОМЛЕНИЯ.
                #
                # Было без обёртки, и это давало сразу две беды. Первая: `notify_now`
                # ходит в базу и публикует событие, а любое его исключение
                # превращало честный 403 «заблокировано, подождите N секунд» в 500 —
                # человек переставал понимать, что происходит, ровно в тот момент,
                # когда ему и так тяжело.
                #
                # Вторая тише и хуже. Бюджет «одно уведомление на окно» занимается
                # ВЫШЕ, в `_should_log_lockout`, через SET NX. Если отправка упала
                # после этого, маркер остаётся занятым до конца окна: счётчик попыток
                # при действующем локе не растёт, повторные запросы попадают в ту же
                # ветку и молча упираются в занятый маркер. Администратор не узнаёт
                # НИЧЕГО — при том что весь смысл механизма (14 §2.2) в том, чтобы он
                # узнал, что сотрудник не может работать.
                #
                # Поэтому: провал не роняет ответ и освобождает маркер, чтобы
                # следующая попытка человека попробовала уведомить заново.
                try:
                    await notify_now(
                        db,
                        redis,
                        kind="auth.account_locked",
                        audience="admin",
                        title=f"Вход заблокирован: {user.full_name}",
                        body=(
                            f"Учётная запись {user.email} временно заблокирована после "
                            "серии неудачных попыток входа. Если это сам сотрудник — "
                            "вышлите ему новую ссылку для входа."
                        ),
                        entity_type="user",
                        entity_id=str(user.id),
                    )
                except Exception:  # noqa: BLE001 — причина любая, реакция одна
                    log.warning("auth.lock_notify_failed", email_hash=_email_hash(email))
                    await redis.delete(f"auth:lock_audited:{_email_hash(email)}")
        log.info("auth.failed", email_hash=_email_hash(email), remote_addr=ip, reason="locked")
        raise ApiError("account_locked", status=403, details={"retry_after_sec": retry_after})

    user = await _get_user_by_email(db, email)
    if user is None:
        dummy_verify(body.password)  # same timing as a real check
        await record_login_failure(redis, email, ip)
        await _audit_login_failure(db, user=None, ip=ip, reason="no_user")
        log.info("auth.failed", email_hash=_email_hash(email), remote_addr=ip, reason="no_user")
        raise ApiError("invalid_credentials", status=401)

    if not verify_password(user.password_hash, body.password):
        await record_login_failure(redis, email, ip)
        await _audit_login_failure(db, user=user, ip=ip, reason="bad_password")
        log.info(
            "auth.failed", email_hash=_email_hash(email), remote_addr=ip, reason="bad_password"
        )
        raise ApiError("invalid_credentials", status=401)

    if not user.is_active:
        await _audit_login_failure(db, user=user, ip=ip, reason="inactive")
        log.info("auth.failed", email_hash=_email_hash(email), remote_addr=ip, reason="inactive")
        raise ApiError("forbidden", status=403, message=ACCOUNT_DISABLED_MESSAGE)

    # Адрес передаём обязательно: успешный вход убирает почту из множества
    # промахнувшихся с этого адреса, иначе утренняя опечатка одного человека
    # так и висела бы уликой против всего офиса за тем же адресом.
    await reset_login_failures(redis, email, ip)
    await write_audit(
        db,
        user_id=user.id,
        action="auth.login",
        entity="user",
        entity_id=str(user.id),
        details={"ip": ip, "result": "success"},
    )
    await db.commit()

    refresh_token = await issue_refresh_token(redis, str(user.id), body.remember)
    _set_refresh_cookie(response, refresh_token, body.remember)
    log.info("auth.login", user_id=str(user.id), remote_addr=ip)
    return _login_response(user)


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LoginResponse:
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise ApiError("unauthorized", status=401)
    try:
        user_id, remember = await rotate_refresh_token(redis, token)
    except RefreshTokenInvalid:
        raise ApiError("unauthorized", status=401) from None

    user = await db.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise ApiError("unauthorized", status=401)

    new_token = await issue_refresh_token(redis, user_id, remember)
    _set_refresh_cookie(response, new_token, remember)
    return _login_response(user)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if token:
        await revoke_refresh_token(redis, token)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    await write_audit(
        db, user_id=user.id, action="auth.logout", entity="user", entity_id=str(user.id)
    )
    await db.commit()


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        department=user.department,
        permissions=permissions_for(user.role),
        hotkeys=(user.ui_settings or {}).get("hotkeys") or {},
    )


@router.put("/me/hotkeys", response_model=MeOut)
async def set_my_hotkeys(
    body: HotkeysRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeOut:
    """Переназначить себе горячие клавиши (требование заказчика 13 августа).

    ПРАВА НЕ СПРАШИВАЕМ: человек правит СВОЁ рабочее место, как и свой пароль. Тот,
    кто вошёл, вправе решать, какой клавишей ему принимать диалог.

    ⚠ ХРАНИМ ТОЛЬКО ОТЛИЧИЯ, а не полную таблицу. Иначе первое же нажатие «Сохранить»
    заморозило бы у человека ВСЕ восемнадцать действий в том виде, в каком они были
    сегодня, — и завтрашняя правка умолчаний (новое действие, исправленное сочетание)
    до него бы не доехала. Пустой объект возвращает всё к умолчаниям.

    ⚠ ОСТАЛЬНЫЕ НАСТРОЙКИ ИНТЕРФЕЙСА НЕ ЗАТИРАЕМ. `ui_settings` — общий объект, и за
    клавишами туда придут тема и плотность списка; запись целиком снесла бы соседей.
    """
    settings = dict(user.ui_settings or {})
    if body.hotkeys:
        settings["hotkeys"] = body.hotkeys
    else:
        settings.pop("hotkeys", None)
    # Новый объект, а не правка на месте: JSON-колонку SQLAlchemy отслеживает по
    # ссылке, и мутация вложенного словаря в UPDATE не поедет.
    user.ui_settings = settings or None
    await write_audit(
        db,
        user_id=user.id,
        action="user.hotkeys_changed",
        entity="user",
        entity_id=str(user.id),
        details={"actions": sorted(body.hotkeys)},
    )
    await db.commit()
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        department=user.department,
        permissions=permissions_for(user.role),
        hotkeys=body.hotkeys,
    )


@router.get("/invite/{token}", response_model=InviteInfoOut)
async def invite_info(
    token: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InviteInfoOut:
    user_id = await peek_invite(redis, token)
    if user_id is None:
        raise ApiError("invite_expired", status=404)
    user = await db.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise ApiError("invite_expired", status=404)
    return InviteInfoOut(email=user.email, full_name=user.full_name)


@router.post("/invite/accept", response_model=LoginResponse)
async def invite_accept(
    body: InviteAcceptRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LoginResponse:
    """
    ⚠ СНАЧАЛА ПРОВЕРКИ, ПОТОМ ГАШЕНИЕ. Порядок был обратный, и он сжигал ссылку на
    отказе: `consume_invite` — это redis GETDEL, необратимо, а стоял он ПЕРВЫМ. Любой
    отказ ниже оставлял человека с мёртвой ссылкой и без пути назад.

    Это не теория: 14 августа владелец получил приглашение на учётку, у которой пароль
    уже был задан, — сервер ответил 422 «пароль уже задан», а токен к тому моменту уже
    сгорел. Повтор дал 404, страница приглашения — 404, и каждое новое приглашение
    умирало ровно так же с первого нажатия. Тупик, из которого не выйти изнутри.

    ⚠ ГАШЕНИЕ ОСТАЁТСЯ ПОСЛЕДНИМ РУБЕЖОМ, И ЭТО НЕ ФОРМАЛЬНОСТЬ. Наивная перестановка
    (прочитать → проверить → погасить) открывает окно, в котором два одновременных
    запроса пройдут проверки и оба поставят пароль. Поэтому `consume_invite` стоит
    вплотную перед мутацией: GETDEL атомарен, и его пустой ответ означает ровно одно —
    кто-то успел раньше. Между ним и записью пароля не должно появиться ни одной
    проверки, иначе дефект вернётся с другой стороны.
    """
    user_id = await peek_invite(redis, body.token)
    if user_id is None:
        raise ApiError("invite_expired", status=404)
    user = await db.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise ApiError("invite_expired", status=404)
    if is_password_set(user.password_hash):
        raise ApiError(
            "unprocessable",
            status=422,
            message="Пароль уже задан — войдите с ним. Забыли — нажмите «Не помню пароль»",
            details={"reason": "password_already_set"},
        )

    # Гасим здесь и только здесь — см. довод выше про гонку.
    if await consume_invite(redis, body.token) is None:
        raise ApiError("invite_expired", status=404)

    user.password_hash = hash_password(body.password)
    await write_audit(
        db,
        user_id=user.id,
        action="user.invite_accepted",
        entity="user",
        entity_id=str(user.id),
    )
    await db.commit()
    # Сброс пароля хоронит действующие access-токены пометкой
    # `revoked_users:{id}` (иначе старый токен читал бы переписку ещё 15 минут).
    # Пометка не различает старый токен и выданный секунду назад, поэтому
    # сотрудник, только что поставивший новый пароль, получал бы 401 на любой
    # REST-запрос до конца окна. Снимаем её здесь: лазейки это не даёт —
    # неактивному эта ручка уже ответила 404 выше, а сессии оборваны до неё.
    await clear_revocation(redis, user.id)

    # Log the user in right away (01 §2.4): body as in /auth/login + cookie.
    refresh_token = await issue_refresh_token(redis, str(user.id), remember=False)
    _set_refresh_cookie(response, refresh_token, remember=False)
    return _login_response(user)


# --- свой профиль (требование заказчика от 7 августа) -----------------------


@router.post("/change-password")
async def change_own_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Сменить СВОЙ пароль.

    Раньше пароль менял только администратор, и в профиле стояла надпись
    «напишите ему». На тринадцать человек это означало, что смена пароля —
    переписка на полдня, а значит пароли не меняют вовсе.

    ТЕКУЩИЙ ПАРОЛЬ СПРАШИВАЕТСЯ ОБЯЗАТЕЛЬНО. Сессия живёт долго, а
    незапертый ноутбук — обычное дело в офисе. Без проверки любой, кто
    подошёл к чужому столу, менял бы пароль коллеге и запирал его из
    системы.

    ОСТАЛЬНЫЕ СЕССИИ ОБРЫВАЮТСЯ. Пароль меняют по двум причинам: «давно
    пора» и «кажется, кто-то его знает». Во втором случае смена без обрыва
    чужих сессий бесполезна — тот, кто уже вошёл, так и останется внутри.
    Своя сессия при этом сохраняется: выгонять человека из системы за то,
    что он сделал правильную вещь, — плохая награда.
    """
    if not verify_password(user.password_hash, body.current_password):
        # Код именной, и он НЕ ``invalid_credentials``. Тот код означает
        # «не пустили в систему»: по нему экран входа чистит поле пароля,
        # трясёт форму и уводит фокус. Здесь человек уже внутри и всего лишь
        # опечатался в текущем пароле — беда другая, и вести себя с ней надо
        # иначе, значит и код обязан быть другим. Общий код здесь и был бы тем
        # разъездом, из-за которого фронт не отличает «повторите» от «нельзя».
        raise ApiError(
            "wrong_current_password",
            "Текущий пароль не подходит — проверьте раскладку и Caps Lock",
            status=422,
            details={"reason": "wrong_current_password"},
        )
    if body.new_password == body.current_password:
        raise ApiError(
            "unprocessable",
            "Новый пароль совпадает с текущим — придумайте другой",
            status=422,
            details={"reason": "same_password"},
        )

    user.password_hash = hash_password(body.new_password)
    await write_audit(
        db,
        user_id=user.id,
        action="user.password_changed",
        entity="user",
        entity_id=str(user.id),
        details={"by": "self"},
    )
    await db.commit()

    # ⚠ РВЁМ СЕССИИ ЦЕЛИКОМ, А НЕ ОДНИ REFRESH-ЦЕПОЧКИ (аудит 30.08).
    #
    # Здесь стоял `revoke_all_user_refresh` — то есть обещание докстринга
    # «остальные сессии обрываются» выполнялось на треть. Пароль меняют в том
    # числе потому, что «кажется, кто-то его знает»; чужая открытая вкладка при
    # этом сохраняла действующий access-токен на все пятнадцать минут (а с ним
    # и право выписывать новые WS-тикеты), а уже открытый сокет не закрывался
    # вовсе и продолжал получать переписку клиентов, пока не оборвётся сам.
    # Контракт в шапке `services/sessions.py` называет обе цены прямо. Все
    # административные пути (сброс пароля, отключение сотрудника) зовут полный
    # `revoke_sessions` — своя смена пароля была единственным исключением.
    #
    # СВОЯ ВКЛАДКА ПРИ ЭТОМ ОСТАЁТСЯ ВНУТРИ, и это не везение: с 27.08 `deps.py`
    # сверяет отзыв по `iat`, а не по факту пометки. Старый access-токен этой
    # вкладки отвергается (он выписан ДО отзыва) — она обновляется по свежей
    # куке ниже и получает новый, выписанный ПОСЛЕ. Ценой одного мгновенного
    # обновления человек не платит за то, что сделал правильную вещь.
    # Сокеты закрываются кодом «переподключись», а не «выйди»: эта вкладка
    # переподключится с новой кукой из этого ответа, а чужие устройства упрутся
    # в отозванную цепочку обновления и выйдут сами.
    await revoke_sessions(redis, user.id, reason="password_changed", close_code=WS_CLOSE_RELOGIN)
    refresh_token = await issue_refresh_token(redis, str(user.id), remember=False)
    _set_refresh_cookie(response, refresh_token, remember=False)
    log.info("auth.password_changed", user_id=str(user.id), remote_addr=_client_ip(request))
    return {"status": "ok"}


@router.patch("/me")
async def change_own_name(
    body: ChangeNameRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Сменить СВОЁ имя.

    Имя видно клиенту в подписи сообщения и коллегам в списке передачи.
    Человек, которого завели как «Сотрудник 3», должен уметь исправить это
    сам, а не просить администратора.

    Роль и почта здесь не меняются намеренно: первое — вопрос прав, второе —
    логин. И то и другое остаётся за администратором.
    """
    name = body.full_name.strip()
    if not name:
        raise ApiError(
            "validation_error",
            "Впишите имя — его видят клиент в подписи и коллеги в списке передачи",
            status=400,
            details={"fields": [{"field": "full_name", "rule": "required"}]},
        )
    previous, user.full_name = user.full_name, name
    await write_audit(
        db,
        user_id=user.id,
        action="user.renamed",
        entity="user",
        entity_id=str(user.id),
        details={"from": previous, "to": name, "by": "self"},
    )
    await db.commit()
    return {"full_name": name}
