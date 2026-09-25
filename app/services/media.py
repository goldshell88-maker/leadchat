"""Вложения: приём, раскладка на диске, подписанные ссылки (01 §6.5, 05 §3.3).

Три независимых куска:

1. **Разбор multipart** — ``parse_multipart_file``. В зависимостях проекта
   нет ``python-multipart``, поэтому ``UploadFile``/``File()`` из FastAPI
   недоступны (FastAPI падает на импорте роутера). Парсер по RFC 7578 живёт
   здесь: он нужен ровно для одного случая — одно поле ``file`` в теле
   ``POST /media``. Как только python-multipart появится в pyproject,
   роутер можно переписать на ``UploadFile`` без изменений остального.

2. **Определение типа по magic-байтам** — ``sniff``. ``Content-Type`` из
   браузера не доказательство (07 §5): .exe с ``Content-Type: image/png``
   должен получить 415. Разрешены jpeg/png/webp + pdf (01 §6.5).

3. **Хранение и подпись** — файл ложится в
   ``{MEDIA_ROOT}/{yyyy}/{mm}/{2hex}/{uuid}.{ext}`` (05 §3.3: fan-out по
   первым двум hex-символам uuid), ссылка подписывается ровно тем форматом,
   который проверяет ``secure_link_md5`` в nginx (05 §3.2/§3.3) — в проде
   файлы отдаёт nginx, в dev тем же ключом их проверяет ``GET /media/{path}``.

``media_id`` живёт 24 ч неприкреплённым в Redis (``media:{id}``) — прикрепление
происходит через ``attachments`` в ``POST /conversations/{id}/messages``;
осиротевшие файлы собирает :func:`collect_orphans` в конце этого модуля,
задачей планировщика раз в сутки.

ЗДЕСЬ БЫЛО НАПИСАНО «собирает ``bin/media-gc.sh``». Такого файла в
репозитории нет — ни по этому пути, ни по ``deploy/media-gc.sh``, который
называет расписание; строка в cron оставлена закомментированной, а руководство
по эксплуатации честно пишет «скрипта пока нет». Три места ссылались на
уборку, которой не существовало, и одно из них — комментарий ровно там, где её
стали бы искать.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.models import Message

log = structlog.get_logger("app.media")

MEDIA_URL_PREFIX = "/api/v1/media"
MEDIA_TTL_SECONDS = 3600  # TTL подписанной ссылки — 1 ч (01 §6.1)
UNATTACHED_TTL_SECONDS = 24 * 3600  # media_id живёт сутки неприкреплённым (01 §6.5)
MAX_ATTACHMENTS = 5  # «до 5 шт» (01 §6.2)

# --------------------------------------------------------------- тексты отказов
#
# Тексты вынесены в константы не для красоты, а потому что каждый из них —
# ОДНА беда, у которой несколько мест обнаружения. Пока текст писался по месту,
# три соседние проверки подписи объясняли одно и то же тремя способами
# («Ссылка без подписи», «Ссылка с некорректной подписью» дважды), а разбор
# multipart отвечал человеку заголовками HTTP («Ожидается multipart/form-data
# с полем 'file'», «В Content-Type не найден boundary»). Диспетчер не знает,
# что такое boundary, и сделать с ним ничего не может — ему нужно одно:
# приложить файл заново.

#: Тело запроса не похоже на файл: не тот Content-Type, нет boundary, нет части
#: с именем ``file``. Для человека это одна беда — файл не доехал.
UPLOAD_BROKEN_MESSAGE = "Файл не дошёл до сервера. Попробуйте приложить его заново"

#: Подпись ссылки отсутствует или не сходится. Человек её не подделывал: почти
#: всегда это старая вкладка или ссылка, скопированная из чужого письма.
LINK_INVALID_MESSAGE = "Ссылка на файл больше не действует — обновите диалог"

#: Срок подписи вышел (TTL 1 ч). Отдельный текст от предыдущего намеренно:
#: беда другая, и она чинится ровно обновлением диалога.
LINK_EXPIRED_MESSAGE = "Ссылка истекла, обновите диалог"

#: Подпись сошлась, а файла на диске нет — его убрали или потеряли. Обещать
#: здесь действие («обновите диалог») нельзя: обновление файл не вернёт, и
#: человек только потратит время, прежде чем поймёт это сам.
FILE_MISSING_MESSAGE = "Файл не найден — возможно, его уже удалили"

# Разрешённые типы (01 §6.5): изображения + pdf. mime -> (расширение, kind).
ALLOWED_TYPES: dict[str, tuple[str, str]] = {
    "image/jpeg": ("jpg", "image"),
    "image/png": ("png", "image"),
    "image/webp": ("webp", "image"),
    "application/pdf": ("pdf", "file"),
}

# Путь раздачи — строго то, что мы сами сгенерировали (05 §3.3). Регулярка
# заодно закрывает traversal: ни '..', ни абсолютных путей она не пропускает.
#
# Вторая альтернатива — файлы выгрузок статистики (06 §4.5): они лежат в
# ``MEDIA_ROOT/exports`` под тем же alias и подписываются тем же ключом.
# Имя описывает ровно то, что собирает ``stats.export_filename``
# (``leadchat-stats_{date_from}_{date_to}_{job8}.{csv|xlsx}``) — тест
# ``test_export_relpath_is_servable`` держит эти два места вместе.
MEDIA_RELPATH_RE = re.compile(
    r"^(?:"
    r"\d{4}/\d{2}/[0-9a-f]{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\.[a-z0-9]{2,5}"
    r"|"
    r"exports/leadchat-stats_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_[0-9a-f]{8}\.(?:csv|xlsx)"
    r")$"
)

_MEDIA_ID_RE = re.compile(r"^m_[0-9a-f]{32}$")


def max_size_bytes() -> int:
    """MEDIA_MAX_SIZE_MB (05 §4); читается на каждый вызов — тесты его двигают."""
    return settings.media_max_size_mb * 1024 * 1024


# --------------------------------------------------------------- magic-байты


def sniff(data: bytes) -> tuple[str, str, str] | None:
    """(mime, ext, kind) по сигнатуре файла или None, если тип не разрешён.

    Content-Type запроса намеренно НЕ участвует: единственный источник
    истины — содержимое (01 §6.5, 07 §5).
    """
    if data[:3] == b"\xff\xd8\xff":  # JPEG SOI + marker
        mime = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data[:5] == b"%PDF-":
        mime = "application/pdf"
    else:
        return None
    ext, kind = ALLOWED_TYPES[mime]
    return mime, ext, kind


# ------------------------------------------------------------------ multipart


@dataclass(slots=True)
class UploadedFile:
    """Одна файловая часть multipart-тела."""

    field: str
    filename: str
    content_type: str
    data: bytes


def upload_broken_error(reason: str) -> ApiError:
    """Отказ «файл не доехал». Текст один на все три причины, ``reason`` — в details.

    Человеку про Content-Type и boundary сказать нечего: это не его ошибка и не
    его словарь. Разбираться по причине будем в логе и в Sentry, для чего она и
    лежит в ``details.reason``.
    """
    return ApiError(
        "validation_error",
        UPLOAD_BROKEN_MESSAGE,
        status=400,
        details={
            "reason": reason,
            "fields": [{"field": "file", "rule": reason, "message": UPLOAD_BROKEN_MESSAGE}],
        },
    )


def _multipart_boundary(content_type: str) -> bytes:
    """boundary из заголовка Content-Type; иначе — 400 validation_error."""
    if "multipart/form-data" not in content_type.lower():
        raise upload_broken_error("not_multipart")
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "boundary":
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            if value:
                return value.encode()
    raise upload_broken_error("no_boundary")


def _parse_part_headers(head: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n"):
        if not line:
            continue
        name, sep, value = line.partition(b":")
        if sep:
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def _disposition_param(disposition: str, key: str) -> str | None:
    """name= / filename= из Content-Disposition (без RFC 2231 — его не шлёт
    ни один браузер для этих полей)."""
    for chunk in disposition.split(";")[1:]:
        param, _, value = chunk.strip().partition("=")
        if param.strip().lower() != key:
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        # latin-1 на проводе, но браузеры кладут туда utf-8 as-is (RFC 7578 §5.1)
        try:
            return value.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return value
    return None


def parse_multipart_file(body: bytes, content_type: str, *, field: str = "file") -> UploadedFile:
    """Первая файловая часть с именем ``field``. Ничего не нашли — 400."""
    delimiter = b"--" + _multipart_boundary(content_type)
    for raw in body.split(delimiter):
        chunk = raw[2:] if raw.startswith(b"\r\n") else raw
        if not chunk or chunk.startswith(b"--"):  # преамбула и финальный "--"
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = _parse_part_headers(head)
        disposition = headers.get("content-disposition", "")
        if _disposition_param(disposition, "name") != field:
            continue
        filename = _disposition_param(disposition, "filename")
        if filename is None:
            continue
        if data.endswith(b"\r\n"):  # CRLF перед следующим delimiter — не часть файла
            data = data[:-2]
        return UploadedFile(
            field=field,
            filename=Path(filename).name or "file",
            content_type=headers.get("content-type", "application/octet-stream"),
            data=data,
        )
    raise upload_broken_error("no_file_part")


# ------------------------------------------------------------------ хранение


def media_root() -> Path:
    return Path(settings.media_root)


def build_relpath(file_uuid: uuid.UUID, ext: str, *, now: datetime | None = None) -> str:
    """``{yyyy}/{mm}/{2hex}/{uuid}.{ext}`` — раскладка 05 §3.3."""
    now = now or datetime.now(UTC)
    return f"{now:%Y}/{now:%m}/{file_uuid.hex[:2]}/{file_uuid}.{ext}"


def save_file(data: bytes, relpath: str) -> Path:
    """Запись в MEDIA_ROOT. Недоступное хранилище — понятная 500, а не трейс:
    типовая причина — не смонтирован /var/leadchat/media (05 §2.2/§8.4)."""
    path = media_root() / relpath
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # запись через .part: недокачанный файл не увидит GC как «сироту»
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        log.exception("media.storage_unavailable", media_root=str(media_root()))
        raise ApiError(
            "internal_error",
            "Хранилище вложений недоступно — сообщите администратору",
            status=500,
        ) from exc
    return path


def too_large_error(size: int | None = None) -> ApiError:
    """Отказ по размеру. ОДИН на весь проект — и для потока, и для разбора.

    Беда одна: файл больше лимита. Мест обнаружения два — роутер обрывает
    чтение тела, не дожидаясь конца закачки, а этот модуль проверяет уже
    разобранную часть. Пока текст писался в обоих местах отдельно, он и жил
    двумя копиями: разъезд в лимите или в формулировке заметить было нечем.
    """
    limit = max_size_bytes()
    details: dict[str, Any] = {"limit_bytes": limit, "limit_mb": settings.media_max_size_mb}
    if size is not None:
        details["size"] = size
    return ApiError(
        "payload_too_large",
        f"Файл больше {settings.media_max_size_mb} МБ — уменьшите его или отправьте по частям",
        status=413,
        details=details,
    )


def validate_upload(upload: UploadedFile) -> tuple[str, str, str]:
    """Размер + magic-байты. -> (mime, ext, kind); иначе 413 / 415."""
    if not upload.data:
        raise ApiError(
            "validation_error",
            "Файл пустой — выберите другой",
            status=400,
            details={
                "reason": "empty_file",
                "fields": [{"field": "file", "rule": "empty", "message": "Файл пустой"}],
            },
        )
    if len(upload.data) > max_size_bytes():
        raise too_large_error(len(upload.data))
    sniffed = sniff(upload.data)
    if sniffed is None:
        raise ApiError(
            "unsupported_media_type",
            # Расширения — строчными и через запятую: так их пишет проводник,
            # а не спецификация MIME.
            "Такой файл отправить нельзя. Подойдут фото (jpg, png, webp) и pdf",
            status=415,
            details={"declared_content_type": upload.content_type},
        )
    return sniffed


def store_upload(upload: UploadedFile) -> dict[str, Any]:
    """Валидация + запись на диск. -> метаданные вложения (без подписи URL)."""
    mime, ext, kind = validate_upload(upload)
    file_uuid = uuid.uuid4()
    relpath = build_relpath(file_uuid, ext)
    save_file(upload.data, relpath)
    meta = {
        "media_id": f"m_{uuid.uuid4().hex}",
        "kind": kind,
        "name": upload.filename,
        "size": len(upload.data),
        "mime": mime,
        "path": relpath,
    }
    log.info("media.stored", media_id=meta["media_id"], kind=kind, size=meta["size"])
    return meta


# ------------------------------------------------------------------- подпись


def signed_media_url(relpath: str, ttl: int = MEDIA_TTL_SECONDS) -> str:
    """Подписанная ссылка формата ``secure_link_md5`` из nginx (05 §3.3).

    Формат строки подписи — контракт с nginx, менять нельзя:
    ``"{exp}{uri} {MEDIA_SIGN_KEY}"``.
    """
    return _signed_uri(f"{MEDIA_URL_PREFIX}/{relpath}", ttl)


def _signed_uri(uri: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    raw = f"{exp}{uri} {settings.media_sign_key}"
    sig = base64.urlsafe_b64encode(hashlib.md5(raw.encode()).digest()).rstrip(b"=").decode()  # noqa: S324
    return f"{uri}?sig={sig}&exp={exp}"


#: Фотографии клиентов лежат на CDN Авито (`NN.img.avito.st`). Из офиса и
#: из-за VPN они грузятся по 3–6 с на снимок (замер 12.09 с ноутбука
#: владельца: «фото не грузятся»), с сервера — мгновенно. Поэтому ссылка
#: подменяется на наш адрес: nginx забирает снимок у Авито и держит в кэше
#: (`location /api/v1/avito-img/`), а подпись та же, что у своих вложений.
#: Хост — только двузначный номер перед `.img.avito.st`: чужой хост через
#: nginx не пройдёт по построению (шаблон в location), а не по проверке.
#: Ссылки Авито кончаются пустым «?» (все 3 из 3 свежих в бою) — его
#: отбрасываем; ссылку с настоящей строкой запроса не трогаем: подпись
#: nginx считает по пути, и параметры пришлось бы возить отдельно.
AVITO_IMG_PREFIX = "/api/v1/avito-img"
_AVITO_IMG = re.compile(r"^https://(\d{2}\.img\.avito\.st)/([^?#\s]+)\??$")


def proxied_avito_image_url(url: Any, ttl: int = MEDIA_TTL_SECONDS) -> str | None:
    """Наш подписанный адрес для снимка с CDN Авито — или None, если это не он."""
    m = _AVITO_IMG.match(url) if isinstance(url, str) else None
    if m is None:
        return None
    return _signed_uri(f"{AVITO_IMG_PREFIX}/{m.group(1)}/{m.group(2)}", ttl)


def _link_invalid_error() -> ApiError:
    """Подпись не сходится или её нет — беда одна, текст один.

    БЫЛО ТРИ ТЕКСТА НА ОДНУ БЕДУ: «Ссылка без подписи», «Ссылка с некорректной
    подписью» (дважды) и рядом «Ссылка истекла». Человек, открывший вчерашнюю
    вкладку, читал любой из трёх и не понимал, чем они отличаются, — а не
    отличаются они ничем: во всех трёх случаях делать надо одно и то же.

    КОД ИМЕННОЙ, А НЕ ``forbidden``. ``forbidden`` в этой системе означает «у
    вас нет прав»: по нему экран входа рисует «Учётная запись отключена», а
    интерфейс — отказ роли. К сломанной ссылке на файл права сотрудника
    отношения не имеют, и путать эти две беды одним кодом нельзя.
    """
    return ApiError("media_link_invalid", LINK_INVALID_MESSAGE, status=403)


def check_signature(relpath: str, sig: str | None, exp: str | int | None) -> None:
    """Проверка подписи для dev-раздачи. Статусы — как у nginx (05 §3.2):
    403 на битой подписи, 410 на истёкшей. Коды именные: истёкшую ссылку фронт
    чинит сам (перезапросил диалог — получил свежую подпись), с битой делать
    нечего."""
    if not sig or exp is None:
        raise _link_invalid_error()
    try:
        exp_ts = int(exp)
    except (TypeError, ValueError) as exc:
        raise _link_invalid_error() from exc
    raw = f"{exp_ts}{MEDIA_URL_PREFIX}/{relpath} {settings.media_sign_key}"
    expected = base64.urlsafe_b64encode(hashlib.md5(raw.encode()).digest()).rstrip(b"=").decode()  # noqa: S324
    # hmac.compare_digest: подпись сравнивается за постоянное время.
    #
    # Сравниваем в БАЙТАХ, а не в строках. На строках `compare_digest` бросает
    # TypeError, если в любой из них есть символ вне ASCII, — и ссылка с
    # кириллицей в `sig` (письмо, прошедшее через почтовый клиент; ссылка,
    # скопированная с переносом) улетала в 500 «Сбой на нашей стороне» вместо
    # честного «ссылка не действует». Проверено на стенде: 500 с трейсом в
    # логе. Наша подпись — base64url, то есть заведомо ASCII, поэтому
    # неудачное кодирование само по себе означает «подпись не наша».
    import hmac

    try:
        candidate = sig.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _link_invalid_error() from exc
    if not hmac.compare_digest(expected.encode("ascii"), candidate):
        raise _link_invalid_error()
    if exp_ts < int(time.time()):
        raise ApiError("media_link_expired", LINK_EXPIRED_MESSAGE, status=410)


def attachment_out(meta: dict[str, Any]) -> dict[str, Any]:
    """Элемент ``attachments[]`` в MessageOut (01 §6.1) со свежей подписью."""
    out = {
        "media_id": meta.get("media_id"),
        "kind": meta.get("kind"),
        "name": meta.get("name"),
        "size": meta.get("size"),
    }
    relpath = meta.get("path")
    if isinstance(relpath, str) and relpath:
        out["url"] = signed_media_url(relpath)
    elif meta.get("url"):
        out["url"] = meta["url"]
    return out


def sign_attachments(attachments: list[Any] | None) -> list[dict[str, Any]]:
    """Пересобрать attachments из БД со свежими подписями (TTL 1 ч, 01 §6.1).

    Вложения ВХОДЯЩИХ приходят из Авито в своём формате (adapter
    ``_extract_attachments``) — у них нет ``path``, их отдаём как есть.
    """
    result: list[dict[str, Any]] = []
    for item in attachments or []:
        if isinstance(item, dict) and item.get("path"):
            result.append(attachment_out(item))
        elif isinstance(item, dict):
            через_нас = (
                proxied_avito_image_url(item.get("url")) if item.get("kind") == "image" else None
            )
            result.append({**item, "url": через_нас} if через_нас else item)
    return result


# ------------------------------------------- реестр неприкреплённых media_id


def _media_key(media_id: str) -> str:
    return f"media:{media_id}"


async def register_media(redis: Redis, meta: dict[str, Any]) -> None:
    """media_id → метаданные на 24 ч (01 §6.5)."""
    await redis.set(
        _media_key(meta["media_id"]),
        json.dumps(meta, ensure_ascii=False),
        ex=UNATTACHED_TTL_SECONDS,
    )


async def resolve_media(redis: Redis, media_id: str) -> dict[str, Any] | None:
    """Метаданные загруженного файла или None (истёк / неизвестен / подделан)."""
    if not isinstance(media_id, str) or not _MEDIA_ID_RE.match(media_id):
        return None
    raw = await redis.get(_media_key(media_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        meta = json.loads(raw)
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


async def resolve_attachments(redis: Redis, media_ids: list[str]) -> list[dict[str, Any]]:
    """media_id[] из тела запроса -> метаданные для messages.attachments.

    Неизвестный/протухший id — 422: молча ронять вложение нельзя, менеджер
    должен увидеть, что картинка не ушла.
    """
    if len(media_ids) > MAX_ATTACHMENTS:
        raise ApiError(
            "unprocessable",
            f"В одно сообщение помещается {MAX_ATTACHMENTS} файлов — "
            "уберите лишние или отправьте вторым сообщением",
            status=422,
            details={"reason": "too_many_attachments", "limit": MAX_ATTACHMENTS},
        )
    resolved: list[dict[str, Any]] = []
    for media_id in media_ids:
        meta = await resolve_media(redis, media_id)
        if meta is None:
            raise ApiError(
                "unprocessable",
                "Вложение не найдено или устарело — загрузите файл заново",
                status=422,
                details={"reason": "media_not_found", "media_id": media_id},
            )
        resolved.append(meta)
    return resolved


# ------------------------------------------------------ уборка сирот (#22)


#: Насколько файл должен быть старше самого долгого законного «висения»,
#: прежде чем его можно считать брошенным. Неприкреплённый `media_id` живёт
#: сутки; берём двое — запас на часы, разъехавшиеся между контейнерами, и на
#: человека, который загрузил файл и ушёл на обед, не отправив.
ORPHAN_AFTER_SECONDS = 2 * UNATTACHED_TTL_SECONDS

#: Каталоги внутри MEDIA_ROOT, до которых уборке вложений дела нет.
#:
#: ПОЧЕМУ ЭТО ВАЖНО. В `MEDIA_ROOT/exports` лежат выгрузки статистики, и у них
#: СВОЙ владелец и СВОЙ срок — семь суток (`stats.cleanup_export_files`,
#: 06 §5.4). На выгрузку не ссылается ни одно сообщение, поэтому для уборки
#: вложений она выглядела ровно как брошенный файл и удалялась через двое
#: суток. Два задания планировщика ходят по одному каталогу с разницей в
#: полчаса (03:30 и 04:00, `app/scheduler/jobs/stats.py`), и более злое молча
#: побеждало: обещанные семь дней на деле были двумя, а `cleanup_export_files`
#: доставался уже пустой каталог. Проверено тестом
#: `test_orphan_cleanup_keeps_statistics_exports`.
#:
#: Имя каталога дублирует `stats.EXPORT_DIRNAME` намеренно: импортировать сюда
#: весь модуль статистики ради одной строки нельзя — он сам импортирует этот
#: модуль (`from app.services.media import signed_media_url`), и получилось бы
#: кольцо. Два имени держит вместе тест `test_export_dirname_matches_stats`.
FOREIGN_DIRNAMES = frozenset({"exports"})

#: Ошибки, при которых уборке удалять нечем: том без права записи. Такая беда не
#: «файл забрали параллельно» — она повторится на каждом файле и каждую ночь, и
#: заметить её надо сразу, а не по сотням предупреждений (том планировщика был
#: `:ro` с 05.08 по 24.09, и уборки не удалили ни одного файла).
READ_ONLY_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


async def collect_orphans(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Удалить файлы, которые ни к чему не прикреплены. -> сколько удалено.

    ЧТО ЗДЕСЬ БЫЛО. В шапке этого модуля написано «осиротевшие файлы собирает
    ``bin/media-gc.sh``». Такого файла в репозитории нет — ни по этому пути, ни
    по `deploy/media-gc.sh`, который называет расписание. Строку в cron
    оставили закомментированной, а руководство по эксплуатации честно пишет
    «скрипта пока нет». То есть три места ссылались на уборку, которой не
    существовало, и одно из них — комментарий рядом с кодом, где её ищут.

    ОТКУДА БЕРУТСЯ СИРОТЫ. Человек прикладывает файл к сообщению: файл
    улетает на диск сразу, а `media_id` живёт сутки в Redis и прикрепляется
    только при отправке. Передумал, закрыл вкладку, перезагрузил страницу —
    файл остался на диске навсегда. Ключ в Redis истечёт, а байты нет.

    ПОЧЕМУ ЗАДАЧЕЙ, А НЕ СКРИПТОМ. Решение «прикреплён или нет» требует
    заглянуть в базу: путь файла лежит внутри JSONB сообщения. Shell-скрипту
    пришлось бы ходить в PostgreSQL самому, дублируя знание о раскладке и
    формате поля. Здесь же всё рядом, и рядом же тесты.

    ПОЧЕМУ СНАЧАЛА ЧИТАЕМ БАЗУ, А ПОТОМ ДИСК. Обратный порядок дал бы гонку с
    отправкой: файл записан, сообщение ещё не создано — и уборка удалила бы
    вложение из-под человека, нажавшего «Отправить». Порог в двое суток от
    этого и страхует, но порядок дешевле и надёжнее.
    """
    root = media_root()
    if not root.is_dir():
        return 0

    cutoff = ((now or datetime.now(UTC)) - timedelta(seconds=ORPHAN_AFTER_SECONDS)).timestamp()

    # Все пути, на которые ссылается хоть одно сообщение. Выборка идёт по
    # сообщениям С вложениями — их доля мала, и полный перебор таблицы здесь не
    # нужен.
    rows = await db.execute(
        sa.select(Message.attachments).where(
            Message.attachments.is_not(None),
            sa.func.jsonb_array_length(sa.cast(Message.attachments, JSONB)) > 0,
        )
    )
    referenced: set[str] = set()
    for (items,) in rows:
        for item in items or []:
            path = item.get("path") if isinstance(item, dict) else None
            if isinstance(path, str) and path:
                referenced.add(path)

    removed = 0
    for entry in root.rglob("*"):
        if not entry.is_file() or entry.suffix == ".part":
            continue
        relative = entry.relative_to(root)
        # Чужой каталог — не наша забота: у выгрузок статистики свой срок
        # хранения и своя уборка (см. FOREIGN_DIRNAMES).
        if relative.parts and relative.parts[0] in FOREIGN_DIRNAMES:
            continue
        relpath = str(relative)
        if relpath in referenced:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue  # слишком свежий — может быть прямо сейчас отправляется
            entry.unlink()
            removed += 1
        except OSError as exc:
            if exc.errno in READ_ONLY_ERRNOS:
                log.error("media.orphan_cleanup_readonly", root=str(root), error=str(exc))
                return removed
            log.warning("media.orphan_cleanup_failed", path=relpath)  # забрали параллельно
    return removed
