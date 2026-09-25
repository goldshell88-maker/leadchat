"""Служебный вход для скриптов вне приложения (14 §2.1, §4).

    POST /internal/notify   — авторизация сервисным токеном, не сессией

Зачем отдельная ручка. Скрипт бэкапа (``deploy/backup.sh``), проверка
восстановления и внешний наблюдатель со второго сервера живут НА ХОСТЕ и рядом
с контейнерами: импортировать код приложения они не могут, а сообщить о
поломке должны — иначе «резервное копирование не выполнилось» опять узнаётся
через неделю (14 §1).

Что здесь намеренно устроено строго:

* **вид события — из закрытого списка.** Скрипт присылает только ``kind`` и
  необязательную подробность; заголовок, важность и кнопку определяет сервер.
  Так уведомление остаётся обращением к человеку, а не пересказом stderr —
  никакая правка в bash не превратит заголовок в «rc=2 at line 118»;
* **токен сравнивается за постоянное время** и берётся только из настроек
  окружения. Не задан — ручка отвечает 401 всем: молча пускать по пустому
  токену хуже, чем не работать;
* **лишние поля в теле запрещены** (``extra="forbid"``): опечатка в скрипте
  обязана быть видна сразу, а не превращаться в тихо потерянный сигнал.

Сеть: ручка отвечает на том же порту, что и остальное API. Ограничение
доступа снаружи (nginx) описано в cross-boundary-заметках — токен обязателен
именно потому, что полагаться на одну лишь сеть нельзя.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.core.config import settings
from app.core.errors import ApiError
from app.scheduler.jobs.watchdog import BACKUP_OK_KEY  # владелец смысла отметки — сторож
from app.services.support import CRITICAL, INFO, WARNING, NotificationDraft, send_notification

router = APIRouter()
log = structlog.get_logger("app.internal")

TOKEN_HEADER = "X-Internal-Token"
# Отметка «бэкап прошёл» живёт неделю: сторож считает её протухшей уже через
# 26 часов, но при разборе инцидента полезно видеть, что копия была вообще.
BACKUP_OK_TTL_SECONDS = 7 * 86400


@dataclass(frozen=True)
class KindSpec:
    """Что скрипт вправе сообщить и как это увидит человек.

    ``kind`` в запросе — словарь СКРИПТОВ, стабильный контракт для bash.
    ``center_kind`` — вид события в каталоге центра уведомлений (он решает,
    какая у события иконка и кнопка). Один вид центра обслуживает несколько
    поводов скрипта, поэтому ключ подавления повторов задаётся отдельно:
    «дамп не создан» и «копия не уехала в облако» — разные новости, схлопывать
    их в одну строку нельзя.
    """

    center_kind: str
    severity: str
    title: str
    body: str


# Закрытый каталог. Добавление вида сюда — сознательное решение: у каждого
# события есть человеческий заголовок, важность и вид в центре уведомлений.
KNOWN_KINDS: dict[str, KindSpec] = {
    "backup.failed": KindSpec(
        center_kind="backup.failed",
        severity=CRITICAL,
        title="Резервное копирование не выполнилось",
        body=(
            "Ночная копия базы не создана. Пока это так, восстанавливать систему "
            "в случае аварии будет не из чего."
        ),
    ),
    "backup.offsite_missing": KindSpec(
        center_kind="backup.failed",
        severity=CRITICAL,
        title="Копия не уехала в облако",
        body=(
            "Копия базы сделана, но осталась только на самом сервере. Если "
            "сервер погибнет, погибнет и она."
        ),
    ),
    "backup.shrunk": KindSpec(
        center_kind="backup.failed",
        # ВАЖНОЕ, а не критичное: копия сделалась и она читается. Подозрителен
        # её размер, и это повод посмотреть, а не поднимать людей ночью.
        # Раньше об этом узнавал только лог, то есть никто.
        severity=WARNING,
        title="Резервная копия вдвое меньше вчерашней",
        body=(
            "Копия создана, но заметно похудела. Так выглядит потеря данных: "
            "стоит убедиться, что диалоги и сообщения на месте."
        ),
    ),
    # Проверка 24.09: отказ второй копии был виден только строкой в логе.
    # Важное, а не критичное: копия в облаке на месте, пропала страховка
    # на случай, когда облако и сервер уходят вместе.
    "backup.second_copy_failed": KindSpec(
        center_kind="backup.failed",
        severity=WARNING,
        title="Вторая копия не уехала в другую страну",
        body=(
            "Ночная копия лежит в облаке, но на второй сервер (Амстердам) не доехала. "
            "Если облако и основной сервер пропадут вместе, восстанавливаться будет не из чего."
        ),
    ),
    "backup.verify_failed": KindSpec(
        center_kind="backup.failed",
        severity=CRITICAL,
        title="Резервная копия не читается",
        body=(
            "Файл копии есть, но проверка его не прочитала. Копия, из которой "
            "нельзя восстановиться, копией не является."
        ),
    ),
    "restore_check.failed": KindSpec(
        center_kind="backup.failed",
        severity=WARNING,
        title="Пробное восстановление не прошло",
        body="Регулярная проверка «поднимется ли система из копии» завершилась неудачей.",
    ),
    "disk.low": KindSpec(
        center_kind="disk.space",
        severity=WARNING,
        title="Диск заполняется",
        body="Место на сервере заканчивается. При 100% система встаёт целиком.",
    ),
    "scheduler.dead": KindSpec(
        center_kind="scheduler.down",
        severity=CRITICAL,
        title="Планировщик не подаёт признаков жизни",
        body=(
            "Внешняя проверка не видит планировщик. Без него встают партиции, "
            "обновление токенов Авито и сверка пропущенных сообщений."
        ),
    ),
    "system.unreachable": KindSpec(
        # Вид заведён в каталоге центра (14 §2.1): critical, аудитория admin,
        # без кнопки — сервер поднимают руками.
        center_kind="system.unreachable",
        severity=CRITICAL,
        title="Система не отвечает снаружи",
        body=(
            "Внешний наблюдатель не смог достучаться до сервера несколько раз "
            "подряд. Сотрудники сейчас не могут работать."
        ),
    ),
    "system.recovered": KindSpec(
        # Возврат — ОТДЕЛЬНЫЙ вид скрипта, хотя лицо в центре у него то же.
        # Ключ склейки берётся из вида скрипта (см. `_draft`), поэтому доклад
        # «система вернулась» больше не падает в строку тревоги о падении: до
        # этой правки он поднимал ей счётчик повторов, возвращал в
        # непрочитанные и выкидывал поверх экрана красную плашку «Система не
        # отвечает снаружи» — в тот момент, когда всё уже работало.
        center_kind="system.unreachable",
        # «Обычное», а не критичное: авария кончилась, будить некого. Понижения
        # уже поднятой тревоги здесь тоже не происходит — это другая строка
        # (см. правило про понижение важности в services/notifications.notify).
        severity=INFO,
        title="Система снова отвечает снаружи",
        body=(
            "Внешний наблюдатель снова видит сервер. Строка нужна затем, чтобы "
            "у ночного простоя остался след: к утру всё работает и выглядит "
            "так, будто ничего не было."
        ),
    ),
}

# Вид без уведомления: сигнал «бэкап прошёл успешно». Уведомлять об успехе
# нечего — отметка нужна сторожу, чтобы заметить ТИШИНУ (14 §2.1: скрипт,
# который вообще не запустился, про себя не сообщит).
KIND_BACKUP_OK = "backup.ok"
ALLOWED_KINDS: tuple[str, ...] = (KIND_BACKUP_OK, *sorted(KNOWN_KINDS))


class InternalNotifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    # Подробность идёт в тело письма отдельной строкой — это единственное, что
    # скрипт вправе написать своими словами.
    detail: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=64)  # backup.sh, restore-check.sh

    @field_validator("detail", "source")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())  # переводы строк из bash — в один пробел
        return value or None


class InternalNotifyOut(BaseModel):
    status: str
    # Честно говорим скрипту, дошло ли до центра уведомлений: 202 значит
    # «приняли», а не «показали человеку».
    notified: bool


def _configured_token() -> str:
    """Сервисный токен из настроек.

    Главный источник — поле ``internal_service_token`` в ``Settings``
    (``INTERNAL_SERVICE_TOKEN`` в окружении). Запасной путь — та же переменная
    напрямую: ``Settings`` читается один раз при импорте, поэтому окружение,
    выставленное позже (тесты, разовый запуск в уже поднятом контейнере), иначе
    не виден. Пустой результат означает «ручка выключена», а не «пускать всех».
    """
    from_settings = str(getattr(settings, "internal_service_token", "") or "")
    return from_settings or os.environ.get("INTERNAL_SERVICE_TOKEN", "")


async def require_service_token(
    x_internal_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    expected = _configured_token()
    if not expected:
        # Ручка выключена, пока токен не задан. Это не молчаливый отказ:
        # строка в логе объясняет админу, почему скрипт получает 401.
        log.warning("internal.token_not_configured", header=TOKEN_HEADER)
        raise ApiError("unauthorized", status=401)
    # Сравниваем БАЙТЫ: `compare_digest` на строках требует чистого ASCII и на
    # заголовке с байтом >127 падает TypeError, то есть 500 вместо 401 —
    # неаутентифицированный способ уронить ручку в пятисотку.
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token.encode("utf-8", "surrogateescape"), expected.encode()
    ):
        log.warning("internal.bad_token", has_header=bool(x_internal_token))
        raise ApiError("unauthorized", status=401)


def _draft(kind: str, spec: KindSpec, body: InternalNotifyIn) -> NotificationDraft:
    text = spec.body
    if body.detail:
        text = f"{text}\n\nПодробность от скрипта: {body.detail}"
    if body.source:
        text = f"{text}\n(сообщил {body.source})"
    return NotificationDraft(
        kind=spec.center_kind,
        severity=spec.severity,
        title=spec.title,
        body=text,
        # Ключ — вид СКРИПТА и он стабилен: ночь падений даёт одну строку со
        # счётчиком повторов, а не сотню (14 §4).
        dedup_key=kind,
    )


@router.post("/internal/notify", response_model=InternalNotifyOut, status_code=202)
async def internal_notify(
    body: InternalNotifyIn,
    _: None = Depends(require_service_token),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InternalNotifyOut:
    """Событие от скрипта на хосте (14 §2.1: «скрипт бэкапа»)."""
    if body.kind == KIND_BACKUP_OK:
        await redis.set(BACKUP_OK_KEY, datetime.now(UTC).isoformat(), ex=BACKUP_OK_TTL_SECONDS)
        log.info("internal.backup_ok", source=body.source)
        return InternalNotifyOut(status="ok", notified=False)

    spec = KNOWN_KINDS.get(body.kind)
    if spec is None:
        log.warning("internal.unknown_kind", kind=body.kind, source=body.source)
        raise ApiError(
            "validation_error",
            status=400,
            message="Неизвестный вид события",
            details={"kind": body.kind, "known": list(ALLOWED_KINDS)},
        )

    # commit и доставку в браузер делает центр уведомлений (notify_now, 14 §4).
    sent = await send_notification(db, redis, _draft(body.kind, spec, body))
    log.info("internal.notify", kind=body.kind, source=body.source, notified=sent)
    return InternalNotifyOut(status="accepted", notified=sent)
