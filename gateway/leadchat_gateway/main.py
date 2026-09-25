"""Приложение шлюза.

`/health` — без токена (сторожа LeadChat: healthcheck-alert.sh и строка «Шлюз
API» в мониторе). Всё остальное — под `require_token`. Маршруты по группам
(каждая — свой модуль, чтобы правки не толкались):

- `routes.status`       — GET /status
- `routes.geo_dadata`   — POST /geo/dadata, /geo/dadata/place, /geo/dadata/city
- `routes.geo_osm_yandex` — POST /geo/nominatim, /geo/yandex, /geo/yandex-suggest
- `routes.text`         — POST /geo/ahunter, /spell
- `routes.llm`          — POST /llm/chat, /llm/anthropic/tool
- `routes.check`        — POST /check/{provider}, /check/url
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from leadchat_gateway import version
from leadchat_gateway.auth import require_token
from leadchat_gateway.log import configure_logging
from leadchat_gateway.routes import check, geo_dadata, geo_osm_yandex, llm, status, text


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Логи настраиваются при СТАРТЕ сервера, а не при импорте модуля: тесты
    # LeadChat импортируют приложение шлюза в том же процессе, и настройка
    # structlog при импорте перебила бы их собственную (LOG_CACHE=0).
    configure_logging()
    yield


app = FastAPI(
    title="LeadChat gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "version": version()}


for router in (status, geo_dadata, geo_osm_yandex, text, llm, check):
    app.include_router(router.router, dependencies=[Depends(require_token)])
