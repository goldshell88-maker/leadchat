"""Webhook gateway (01 §10, DESIGN §8.3, 08 §2.2).

POST /api/hooks/avito/{account_id}?secret=... — вне /api/v1, без Bearer.
The gateway never writes to PostgreSQL and never calls Avito (SLA p99 < 50 ms):
one PK read of avito_accounts, compare_digest of the secret, XADD into the
``webhooks:avito`` stream, instant ``200 {"ok": true}`` — even for garbage
payloads (не-200 только 403 / 413 / 429).
"""

import secrets
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.core import trace
from app.core.config import settings
from app.core.errors import ApiError
from app.models import AvitoAccount
from app.ws.events import utcnow_iso

router = APIRouter()
log = structlog.get_logger("app.webhooks")

STREAM = "webhooks:avito"


async def _check_rate_limit(redis: Redis, account_id: uuid.UUID) -> None:
    """Storm guard (01 §1.7): 100/sec per account, fixed 1-second window."""
    import time

    key = f"ratelimit:webhook:{account_id}:{int(time.time())}"
    n = await redis.incr(key)
    if n == 1:
        await redis.expire(key, 2)
    if n > settings.webhook_rate_limit_per_second:
        raise ApiError("rate_limited", status=429)


#: Секрет вебхука по аккаунту, в памяти процесса: ``{account_id: (секрет, годен_до)}``.
#:
#: ⚠ ЗАЧЕМ ОН ПОЯВИЛСЯ. Шлюз делал одно чтение по первичному ключу — казалось бы,
#: даром. Но чтение берёт соединение из пула (десять на процесс), а пул общий с
#: тяжёлыми чтениями интерфейса. Замер боя 02.09: за восемь минут медленные
#: выборки списка выбрали пул целиком (166 ошибок «QueuePool limit reached»), и в
#: ту же воронку утянуло приём сообщений — 105 вебхуков Авито из 566 (18,6 %)
#: закрылись по таймауту клиента со стороны Авито. Это потерянные сообщения живых
#: клиентов, и восстановить их нечем: стрим до них не дошёл.
#:
#: Причину (медленный поиск и отсутствие потолка на запрос) правим отдельно. Но
#: приём сообщений не должен зависеть от неё ВООБЩЕ: это самое ценное, что делает
#: система, и у него не должно быть общих ресурсов с показом экрана.
_СЕКРЕТЫ: dict[uuid.UUID, tuple[str, float]] = {}

#: Пять минут. Величина не про безопасность, а про то, как часто мы готовы
#: платить за поход в базу на ровном месте: смена секрета подхватывается СРАЗУ
#: (см. ниже), а не по истечении срока.
_СЕКРЕТ_ЖИВЁТ_СЕК = 300.0


def _совпал(дано: str, наш: str) -> bool:
    """Сравнение секретов за постоянное время — и без падения на чужом вводе.

    ⚠ ЗДЕСЬ СРАВНИВАЛИСЬ СТРОКИ, И ЭТО БЫЛ ЛАТЕНТНЫЙ ДЕФЕКТ. `compare_digest` на
    строках требует, чтобы обе были ASCII, и на не-ASCII бросает `TypeError`.
    Секрет приходит из адреса, то есть его пишет кто угодно: `?secret=привет`
    давал 500 и запись `unhandled_error` в журнале вместо честного 403. Наши
    собственные секреты ASCII, поэтому в бою это не всплывало — всплыло бы в
    первый же день чужого внимания.

    Байты сравнимы всегда, а постоянство времени сохраняется.
    """
    return secrets.compare_digest(дано.encode("utf-8"), наш.encode("utf-8"))


async def _секрет_верен(db: AsyncSession, account_id: uuid.UUID, дано: str) -> bool:
    """Тот ли секрет — по памяти процесса, с походом в базу только при промахе.

    ⚠ ПОЧЕМУ СМЕНА СЕКРЕТА ПОДХВАТЫВАЕТСЯ МГНОВЕННО, ХОТЯ ЭТО КЭШ. Потому что в
    базу мы идём не по истечении срока, а при ЛЮБОМ несовпадении. Секрет
    поменяли — первый же вебхук с новым значением не сойдётся с памятью,
    перечитает строку и сойдётся с ней. Обратная ошибка (принять старый секрет
    после смены) живёт не дольше срока памяти и требует, чтобы старый секрет
    знали, — а его знает только тот, кому мы его сами выдали.

    ⚠ ПАМЯТЬ НЕ РАСТЁТ ОТ ЧУЖИХ ЗАПРОСОВ: в неё попадают только аккаунты,
    найденные в базе. Незнакомый идентификатор из адреса кэш не заводит.

    Порядок проверок сохранён прежний — сначала подлинность, потом ограничитель
    частоты. Иначе тот, кто знает идентификатор аккаунта, мог бы выбрать чужой
    лимит неверными секретами и глушить настоящие вебхуки.
    """
    now = time.monotonic()
    известное = _СЕКРЕТЫ.get(account_id)
    if известное is not None and известное[1] > now and _совпал(дано, известное[0]):
        return True  # быстрый путь: базу не трогаем вовсе

    account = await db.get(AvitoAccount, account_id)
    if account is None:
        _СЕКРЕТЫ.pop(account_id, None)
        return False
    _СЕКРЕТЫ[account_id] = (account.webhook_secret, now + _СЕКРЕТ_ЖИВЁТ_СЕК)
    return _совпал(дано, account.webhook_secret)


def забыть_секреты() -> None:
    """Сбросить память процесса. Нужна тестам и смене секрета в том же процессе."""
    _СЕКРЕТЫ.clear()


@router.post("/api/hooks/avito/{account_id}")
async def avito_webhook(
    account_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, bool]:
    secret = request.query_params.get("secret", "")
    if not await _секрет_верен(db, account_id, secret):
        raise ApiError("forbidden", status=403)  # unknown account / bad secret — no body details

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.webhook_max_body_bytes:
        raise ApiError("payload_too_large", status=413)
    body = await request.body()
    if len(body) > settings.webhook_max_body_bytes:
        raise ApiError("payload_too_large", status=413)

    await _check_rate_limit(redis, account_id)

    # Payload is NOT validated here — parsing is the worker's job (01 §10);
    # store the raw body as-is so даже мусор доедет до webhook_raw_log.
    # MAXLEN ~ обязателен: стрим не подрезается ничем другим (ни воркером, ни
    # scheduler'ом), а Redis поднят с noeviction — переполнение памяти означало
    # бы отказ XADD и потерю вебхуков. `approximate` — подрезка по границе
    # узла radix-дерева, без затрат на точный счёт.
    await redis.xadd(
        STREAM,
        {"account_id": str(account_id), "payload": body.decode("utf-8", "replace")},
        maxlen=settings.webhook_stream_maxlen,
        approximate=True,
    )
    await redis.set(f"webhook_last:{account_id}", utcnow_iso())  # отдаёт 01 §4.1
    log.info("webhook.received", account_id=str(account_id), bytes=len(body))
    # СРОК СЛЕДА ПЕРЕЧИТЫВАЕТСЯ ЗДЕСЬ — В ТОЧКЕ ВХОДА ВСЕГО ПУТИ.
    #
    # Без этого след не включался бы вовсе в той половине пути, что живёт в
    # процессе API (вебхук и приём входящего): перечитывал его только тик бота,
    # то есть другой процесс. Поймано на стенде: настройка включена, в журнале
    # пусто. Ответ кэшируется на пять секунд (`app/core/trace.py`), так что
    # лишнего запроса на каждый вебхук здесь нет.
    # ⚠ СЛЕД НЕ ИМЕЕТ ПРАВА СТОИТЬ КЛИЕНТСКОГО СООБЩЕНИЯ. Сообщение уже в стриме
    # строкой выше — то есть доедет в любом случае. А `refresh` идёт в БД (пусть
    # и раз в пять секунд на процесс), и в час, когда пул исчерпан, он способен
    # держать ответ до тридцати секунд. Авито столько не ждёт: 02.09 в бою так
    # потерялось 105 вебхуков из 566. Диагностика, стоящая доставки, — не
    # диагностика.
    try:
        await trace.refresh(db)
    except Exception as exc:  # noqa: BLE001 — след не стоит потерянного вебхука
        log.warning(
            "webhook.trace_refresh_failed",
            account_id=str(account_id),
            error=type(exc).__name__,
        )
    # Первая отметка пути: дальше по `message_id` видна вся жизнь сообщения.
    # Тела не пишем — в нём переписка клиента.
    trace.step("trace.webhook_in", account_id=str(account_id), bytes=len(body))
    return {"ok": True}
