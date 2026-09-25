"""Вложения: загрузка и dev-раздача (01 §6.5, 05 §3.2/§3.3).

``POST /media`` — multipart, поле ``file``, право ``messages:send``
(admin + manager; head/observer — 403). Тело читается стримом с жёстким
потолком MEDIA_MAX_SIZE_MB, тип определяется по magic-байтам, не по
Content-Type. Ответ — ``{media_id, kind, name, size, url}``.

``GET /media/{relpath}`` — раздача с проверкой подписи. **В проде этот путь
до Python не доходит**: nginx отдаёт файлы сам через ``secure_link``
(05 §3.2), поэтому здесь ровно та же проверка теми же статусами (403 битая
подпись, 410 истёкшая) — dev/тесты ходят по тем же правилам, что и прод.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from app.api.deps import get_redis, require_permission
from app.core.errors import ApiError
from app.models import User
from app.services import media

router = APIRouter()

send_perm = require_permission("messages:send")

# Заголовки уже прочитаны фреймворком, поэтому потолок на тело — с запасом
# на multipart-обвязку (boundary + Content-Disposition каждой части).
_BODY_SLACK_BYTES = 8 * 1024

_CONTENT_TYPE_BY_EXT = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
    # выгрузки статистики лежат под тем же alias (06 §4.5)
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


async def _read_body_limited(request: Request) -> bytes:
    """Тело целиком, но не больше лимита: превышение — 413 без чтения хвоста.

    Текст отказа берём у сервиса (``media.too_large_error``), а не пишем свой:
    беда одна и та же — «файл больше лимита», — и человек обязан прочитать про
    неё одно и то же независимо от того, оборвали мы закачку на середине или
    домерили размер после разбора.
    """
    limit = media.max_size_bytes() + _BODY_SLACK_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise media.too_large_error()
        except ValueError:
            pass  # кривой Content-Length — считаем сами по потоку ниже
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise media.too_large_error()
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/media", status_code=201)
async def upload_media(
    request: Request,
    user: User = Depends(send_perm),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    body = await _read_body_limited(request)
    upload = media.parse_multipart_file(body, request.headers.get("content-type", ""))
    meta = media.store_upload(upload)  # 413 / 415 внутри
    await media.register_media(redis, meta)  # media_id живёт 24 ч (01 §6.5)
    return media.attachment_out(meta)


@router.get("/media/{relpath:path}")
async def download_media(relpath: str, request: Request) -> FileResponse:
    """Раздача по подписанной ссылке. Аутентификации нет намеренно: доступ
    даёт подпись с TTL 1 ч, выданная API уже после RBAC-проверки диалога
    (DESIGN §1.4) — тот же контракт, что у nginx в проде."""
    if not media.MEDIA_RELPATH_RE.match(relpath):
        raise ApiError("not_found", media.FILE_MISSING_MESSAGE, status=404)
    media.check_signature(relpath, request.query_params.get("sig"), request.query_params.get("exp"))
    path = media.media_root() / relpath
    if not path.is_file():
        raise ApiError("not_found", media.FILE_MISSING_MESSAGE, status=404)
    ext = relpath.rsplit(".", 1)[-1].lower()
    return FileResponse(
        path,
        media_type=_CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )
