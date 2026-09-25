"""Application factory: /api/v1 routers, /api/health, webhook gateway,
WS Hub lifespan, error envelope, request-id middleware, CORS from settings."""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    audit,
    auth,
    avito_accounts,
    avito_connect,
    bot_dialogs,
    bots,
    clients,
    conversations,
    health,
    inbox,
    internal,
    media,
    messages,
    notifications,
    stats,
    support,
    templates,
    users,
    webhooks,
    ws,
)
from app.api.routes import (
    leadbot as leadbot_routes,
)
from app.api.routes import (
    leads as leads_routes,
)
from app.api.routes import (
    presence as presence_routes,
)

# Псевдоним: модуль маршрутов зовётся `settings`, как и объект конфигурации,
# импортированный ниже. Без переименования один затирал бы другой.
from app.api.routes import settings as settings_routes
from app.core import redis as redis_mod
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, new_request_id, request_id_var
from app.core.observability import init_sentry
from app.db import session as db_mod
from app.integrations.avito import client as avito_client
from app.services import avito_app
from app.ws import hub as hub_mod

log = structlog.get_logger("app.main")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_sentry("api")  # 05 §7.1: без SENTRY_DSN — no-op, приложение живёт как раньше
    db_mod.init_engine(component="api")
    redis = redis_mod.init_client()
    hub_mod.init_hub(redis)  # per-process Hub + Pub/Sub 'events' listener (08 §5.1)
    await avito_app.seed_process(db_mod.get_session_factory(), "api")
    yield
    # Тот же общий httpx-клиент, что у воркера и планировщика: api ходит в
    # Авито за ссылкой на голосовое и за историей чата.
    await avito_client.close_http_client()
    await hub_mod.shutdown_hub()
    await redis_mod.close_client()
    await db_mod.dispose_engine()


def create_app() -> FastAPI:
    configure_logging(component="api")
    app = FastAPI(
        title="LeadChat API",
        version=settings.version,
        lifespan=_lifespan,
        docs_url="/api/docs" if settings.env != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.env != "production" else None,
    )

    # CORS from CORS_ORIGINS (csv). Empty in prod: SPA and API share an origin
    # (01 §1.1). allow_credentials — the refresh cookie.
    if settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = new_request_id()
        token = request_id_var.set(rid)
        structlog.contextvars.bind_contextvars(request_id=rid)
        начало = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)
        мс = (time.perf_counter() - начало) * 1000
        # ⚠ БЕЗ ЭТОЙ СТРОКИ ДИАГНОСТИКА СЛЕПА (инцидент 02.09).
        #
        # В журнале nginx 4 320 записей `GET /api/v1/conversations` неразличимы
        # между собой, при медиане 141 мс и максимуме 59,97 с. Какой именно
        # запрос вставал — по логам было не узнать вовсе; на поиск ушли часы
        # ручных замеров.
        #
        # ⚠ ПИШЕМ ШАБЛОН МАРШРУТА, А НЕ ПУТЬ. Путь несёт идентификаторы, и
        # `/conversations/<uuid>` в журнале не сгруппировать. Шаблон
        # (`/conversations/{conversation_id}`) даёт готовую группировку.
        #
        # ⚠ И НИ В КОЕМ СЛУЧАЕ НЕ СТРОКУ ЗАПРОСА. В ней `q=<телефон клиента>` —
        # персональные данные живых людей, а журнал читают глазами и хранят.
        # Ровно поэтому маршрут пишется здесь, а не в nginx: там доступен только
        # сырой `$request_uri`.
        route = request.scope.get("route")
        медленно = мс >= settings.slow_request_ms
        log.log(
            logging.WARNING if медленно else logging.INFO,
            "http.request",
            route=getattr(route, "path", None) or request.url.path,
            method=request.method,
            status=response.status_code,
            ms=round(мс, 1),
            # ⚠ ЧЕЙ ЭТО ЗАПРОС (разбор 03.09). Сотрудник сообщил: «в 20:46 по
            # Владивостоку сайт работал в пять раз медленнее». Сервер в ту
            # минуту отвечал за 25 мс по медиане, то есть жалоба была про
            # конкретного человека — а проверить это было нечем: строки
            # запросов обезличены, и «медленно у меня» ничем не отличалось от
            # «медленно у всех». Пустая строка — запрос без токена (вход,
            # health, вебхук).
            user_id=request.scope.get("lc_user_id"),
        )
        response.headers["X-Request-Id"] = rid
        return response

    install_error_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(auth.router, prefix="/auth", tags=["auth"])
    api_v1.include_router(conversations.router, tags=["conversations"])
    # Спринт 3: отправка/заметки/повтор (01 §6.2–§6.4), вложения (§6.5),
    # шаблоны (§7) и список исполнителей (§3.1). Собственного prefix у роутеров
    # нет — пути складываются из /api/v1 и путей внутри модулей.
    api_v1.include_router(messages.router, tags=["messages"])
    api_v1.include_router(media.router, tags=["media"])
    api_v1.include_router(templates.router, tags=["templates"])
    api_v1.include_router(users.router, tags=["users"])
    # PUT /presence — своё состояние «на месте» / «отошёл» (#34). Ручка была
    # описана в контракте 01 §11.6, в дереве файлов 08 §и в требованиях к
    # десктопу, но не написана: трей переключал галки, а серверу об этом никто
    # не сообщал.
    api_v1.include_router(presence_routes.router, tags=["presence"])
    # Спринт 6: боты (01 §8). Свой prefix не нужен — пути внутри модуля уже
    # начинаются с /bots, иначе получится /api/v1/bots/bots.
    api_v1.include_router(bots.router, tags=["bots"])
    # Надзор за ботом (docs/45) — админский экран «Диалоги бота». Отдельно от
    # `bots`, потому что там настройка, а здесь разбор уже случившегося.
    api_v1.include_router(bot_dialogs.router, tags=["bot-dialogs"])
    # Лид-бот — СВОЙ раздел, не часть ботов (решение владельца от 12 августа):
    # это отдельный продукт на своём сервере, а не один из наших ботов.
    api_v1.include_router(leadbot_routes.router, tags=["leadbot"])
    # Автозаявки: расширение владельца забирает лиды и подтверждает результат.
    # Вход по своему токену, не по сессии сотрудника (`routes/leads.py`).
    api_v1.include_router(leads_routes.router, tags=["leads"])
    api_v1.include_router(ws.router, tags=["ws"])
    # Спринт 7: центр уведомлений (14 §4). Без этой строки раздела просто нет —
    # в OpenAPI ноль путей /api/v1/notifications, а тесты зеленеют лишь потому,
    # что монтируют роутер собственной фикстурой.
    api_v1.include_router(notifications.router, tags=["notifications"])
    # Очередь «Входящие» (15 §2.1, план 7.1): GET /inbox, /inbox/count и
    # действия claim/decline/release над диалогом. Без этой строки очередь
    # читается (`?tab=inbox` даёт роутер диалогов), но принять из неё диалог
    # нельзя ни одной кнопкой — путей в приложении просто нет.
    api_v1.include_router(inbox.router, tags=["inbox"])
    # Обращения сотрудников (14 §2.2) и служебный вход для скриптов на хосте
    # (14 §2.1): POST /support/password-reset, /support/message,
    # /internal/notify — последний зовёт deploy/backup.sh.
    api_v1.include_router(support.router, tags=["support"])
    api_v1.include_router(internal.router, tags=["internal"])
    # Настройки работы команды: автораспределение диалогов. Без этой строки
    # движок раздачи существует, но включить его можно только правкой .env на
    # сервере — то есть не руководителем, а инженером.
    api_v1.include_router(settings_routes.router, tags=["settings"])
    # Чёрный список (docs/19): пометка убирает требование внимания, но не
    # теряет сообщения — подробности в шапке модуля.
    api_v1.include_router(clients.router, tags=["clients"])

    # ⚠ ЭТИ ЧЕТЫРЕ МОНТИРУЮТСЯ КАК ВСЕ ОСТАЛЬНЫЕ — И ЭТО ПРАВКА, А НЕ УПРОЩЕНИЕ.
    #
    # Здесь стояли `try/except ImportError` и `importlib`: «зоны пишутся
    # параллельно, отсутствие модуля не должно мешать подняться API». Спринты
    # давно закончились, все четыре модуля лежат в дереве, а защита пережила
    # свою причину и превратилась в глушитель.
    #
    # Цена была несимметричной. Ошибка импорта в `stats` или `avito_connect` —
    # опечатка, забытая зависимость, битая миграция кода — не роняла выкат, а
    # поднимала боевой API БЕЗ целого раздела: 404 на /avito-accounts,
    # /me/channels, /stats/*, /audit-log. Контейнер при этом healthy,
    # `/api/health` зелёный, smoke зелёный — а у диспетчеров пропали каналы.
    # В логах оставалась одна строка warning, а у ветки avito_accounts и вовсе
    # `pass`: тишина.
    #
    # Быстрый громкий отказ лучше тихого отсутствия ручек: контейнер не станет
    # healthy, выкатка сама вернёт прежние образы (`ship.sh`), и человек узнает
    # о беде за минуту, а не по жалобе «каналы исчезли».
    api_v1.include_router(avito_connect.router, tags=["avito"])
    api_v1.include_router(avito_accounts.router, tags=["avito-accounts"])
    api_v1.include_router(stats.router, tags=["stats"])
    api_v1.include_router(audit.router, tags=["audit"])

    app.include_router(api_v1)
    app.include_router(health.router, tags=["health"])  # /api/health, outside /api/v1
    app.include_router(webhooks.router, tags=["webhooks"])  # /api/hooks/*, outside /api/v1
    # Канонический upgrade-путь по 01 §11 — /api/ws (nginx location из §11.1);
    # /api/v1/ws оставлен как рабочий алиас (его использует текущий фронт).
    app.websocket("/api/ws")(ws.ws_endpoint)
    return app


app = create_app()
