"""WebSocket endpoints (01 §11, 08 §5.2).

POST /api/v1/ws/ticket — одноразовый тикет (Redis, TTL 60 с, GETDEL);
WS   /api/v1/ws?ticket=... — сокет: 4401 невалидный тикет, 4403 неактивный
пользователь, 4408 «60 секунд ни одного кадра» (прикладной heartbeat 01 §11.5).
"""

import asyncio
import secrets
import time
import uuid
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.api.deps import get_current_user, get_redis
from app.core.config import settings
from app.db import session as db_mod
from app.models import User
from app.services.sessions import access_revoked_at
from app.ws.hub import get_hub, handle_client_frame
from app.ws.presence import presence_connected, presence_disconnected

router = APIRouter()
log = structlog.get_logger("app.ws")


def свой_источник(ws: WebSocket) -> bool:
    """Пришло ли рукопожатие со страницы, которой мы доверяем.

    ⚠ ВТОРАЯ ЛИНИЯ, А НЕ ПЕРВАЯ. Захват сокета с чужого сайта (CSWSH) уже
    закрыт одноразовым тикетом: чтобы его получить, нужен авторизованный запрос
    с токеном, которого у чужой страницы нет. Эта проверка стоит на случай, если
    выдачу тикета когда-нибудь смягчат, — и стоит она три строки.

    ⚠ ОТСУТСТВУЮЩИЙ `Origin` — ЭТО «НЕ БРАУЗЕР», И ЕГО МЫ ПРОПУСКАЕМ. Браузер
    шлёт заголовок при рукопожатии ВСЕГДА, поэтому чужая страница обойти
    проверку молчанием не может. А вот проверки выкатки, наблюдатель и любой
    служебный клиент ходят без него — отвергни мы их, и красным станет smoke, а
    не нападающий.

    ⚠ TAURI. Десктоп грузит фронт локально, его origin не наш домен
    (`WsClient.ts`: «в Tauri origin = tauri://localhost»). Забудь про это — и
    первый же релиз десктопа останется без реального времени, а выглядеть будет
    как «у них не работает интернет». Список — в `ws_extra_origins`.
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    origin = origin.rstrip("/")
    if origin in settings.ws_extra_origins_list:
        return True
    host = ws.headers.get("host")
    # nginx передаёт настоящий Host (`proxy_set_header Host $host`), поэтому
    # сравниваем с ним, а не с настройкой: домен меняется, код — нет.
    return bool(host) and urlsplit(origin).netloc == host


@router.post("/ws/ticket")
async def ws_ticket(
    user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Right: any authenticated (01 §11.1). Ticket is strictly one-time."""
    ticket = "wst_" + secrets.token_urlsafe(36)
    # Время выдачи едет в билете: подключение сверяет его с отзывом сессий так
    # же, как REST сверяет `iat` токена.
    await redis.set(
        f"ws_ticket:{ticket}",
        f"{user.id}|{int(time.time() * 1000)}",
        ex=settings.ws_ticket_ttl_seconds,
    )
    return {"ticket": ticket, "expires_in": settings.ws_ticket_ttl_seconds}


@router.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket,
    ticket: str | None = None,
    redis: Redis = Depends(get_redis),
) -> None:
    """Сокет живёт часами — сессии БД в сигнатуре быть НЕ ДОЛЖНО.

    Зависимость ``get_db`` закрывается только при выходе из обработчика, а
    первое же обращение к сессии неявно открывает транзакцию: каждый открытый
    сокет держал бы слот пула в состоянии `idle in transaction` всё время
    жизни вкладки (05 §8). Пользователя читаем в короткоживущем скоупе — до
    ``accept()``, после чего соединение сразу возвращается в пул.
    """
    if not свой_источник(ws):
        # Проверяем ДО тикета: иначе чужая страница смогла бы сжечь чужой
        # одноразовый тикет, даже не открыв соединение.
        log.warning("ws.foreign_origin", origin=ws.headers.get("origin"))
        await ws.close(code=4403)
        return
    user_id = await redis.getdel(f"ws_ticket:{ticket}") if ticket else None
    if not user_id:
        await ws.close(code=4401)  # invalid ticket — before the first frame (01 §11.1)
        return
    if isinstance(user_id, bytes):
        user_id = user_id.decode()
    user_id, _, issued = user_id.partition("|")
    async with db_mod.session_scope() as db:
        user = await db.get(User, uuid.UUID(user_id))
        if user is not None:
            # Отцепляем ДО выхода из скоупа: `_release` делает rollback, а он
            # экспайрит объекты сессии — у detached-инстанса чтение поля
            # ушло бы в DetachedInstanceError.
            db.expunge(user)
    if user is None or not user.is_active:
        await ws.close(code=4403)
        return
    # Вторая дверь того же отзыва сессии (01 §1.2, §3.3). `is_active` закрывает
    # только отключение; сброс пароля учётную запись активной оставляет, и от
    # здоровой её отличает ровно пометка `revoked_users:{id}`. Без этой проверки
    # обрыв сессии обходится за один шаг: хаб закрыл старые сокеты, а тикет,
    # выписанный до отзыва, свои 60 секунд (§11.1) ещё годен — окно открывает
    # НОВЫЙ сокет и продолжает читать ленту переписки. Ключ строит владелец
    # формата (`app/services/sessions.py`), f-строка здесь молча разъехалась бы.
    #
    # Сверка по времени выдачи билета, как у REST по `iat`: билет, выписанный
    # после отзыва, — это честный перезаход (например, новым паролем, который
    # администратор только что задал), и его пускаем. Без времени (билет
    # прежнего формата) — отказ, как раньше.
    отозвано = await access_revoked_at(redis, user.id)
    if отозвано is not None and (not issued.isdigit() or int(issued) <= отозвано):  # мс
        await ws.close(code=4403)
        return

    await ws.accept()
    hub = get_hub()
    session = hub.attach(ws, user)
    await presence_connected(redis, user.id, session.conn_id)
    log.info("ws.connected", user_id=str(user.id), conn_id=session.conn_id)
    открыт = time.monotonic()
    код: int | str = "eof"
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    ws.receive_text(), timeout=settings.ws_heartbeat_timeout_seconds
                )
            except TimeoutError:  # ни одного кадра за 60 с (01 §11.5)
                код = 4408
                await ws.close(code=4408)
                return
            await handle_client_frame(session, raw, redis)
    except WebSocketDisconnect as обрыв:
        код = обрыв.code
    finally:
        hub.detach(session)
        # grace-таймер offline не должен держать обработчик соединения
        asyncio.get_running_loop().create_task(
            presence_disconnected(redis, user.id, session.conn_id)
        )
        # ⚠ ПОЧЕМУ И СКОЛЬКО ПРОЖИЛ (разбор 03.09). Запись об обрыве была без
        # причины и без длительности, и «связь рвётся» отличалось от «человек
        # закрыл вкладку» только догадкой. Сотрудник пожаловался на медленный
        # сайт; сервер отвечал за 25 мс, а у него самого сокет перезакрывался
        # шесть раз за час — но чтобы это увидеть, пришлось считать пары
        # «подключился/отключился» руками по всему журналу.
        #
        # Код 1001 — вкладку закрыли, 1006 — связь оборвалась без прощания
        # (сеть), 4408 — наш же таймаут тишины, 4401 — выход из системы.
        log.info(
            "ws.disconnected",
            user_id=str(user.id),
            conn_id=session.conn_id,
            code=код,
            lived_s=round(time.monotonic() - открыт, 1),
        )
