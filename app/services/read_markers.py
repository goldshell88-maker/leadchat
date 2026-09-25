"""Пер-юзерные read-маркеры (01 §5.1/§5.3).

Что это. «Прочитано» — состояние ЧЕЛОВЕКА, а не диалога: руководитель, открыв
чужую переписку, не должен гасить бейдж менеджеру, который её ещё не читал.
Поэтому маркеры живут в Redis-хэше ``read:{user_id}`` →
``{conversation_id: last_read_created_at}`` (TTL 90 дней, продлевается на
каждой записи — за отпуск не протухнет), а не в колонке
``conversations.unread_count``.

Как считается ``unread_count`` для ответа (01 §5.1): входящие сообщения
(``direction='in'``) с ``created_at`` строго новее маркера. Заметки, исходящие
и системные записи непрочитанными не бывают по определению.

Обратная совместимость. Если маркера у пользователя нет (не открывал диалог
ни разу / Redis потеряли / диалог из старых данных) — счётчик берётся как
раньше, из ``conversations.unread_count``. То есть переезд не «обнуляет» ничьи
бейджи и не требует миграции данных.

Стоимость. Один HMGET на страницу списка + один GROUP BY по messages, в
котором ЕСТЬ глобальная нижняя граница по ``created_at`` (минимум из маркеров
страницы) — партиции messages отсекаются по ключу партиционирования (06 §0.2),
а не сканируются все.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from redis.exceptions import WatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import aw
from app.models import Conversation, Message  # Conversation — только для типизации fallback'а

log = structlog.get_logger("app.read_markers")

KEY = "read:{user_id}"


def key_for(user_id: uuid.UUID | str) -> str:
    return KEY.format(user_id=user_id)


def ttl_seconds() -> int:
    return settings.read_marker_ttl_days * 86400


def ensure_aware(dt: datetime) -> datetime:
    """SQLite отдаёт naive-время; в БД и в Redis у нас везде UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _parse(raw: str | bytes | None) -> datetime | None:
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return ensure_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:  # мусор в хэше не должен ронять список диалогов
        log.warning("read_markers.bad_value")
        return None


def _dump(dt: datetime) -> str:
    return ensure_aware(dt).isoformat()


# ------------------------------------------------------------------ маркеры


async def get_marker(
    redis: Redis, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> datetime | None:
    raw = await aw(redis.hget(key_for(user_id), str(conversation_id)))
    return _parse(raw)


async def get_markers(
    redis: Redis, user_id: uuid.UUID, conversation_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Маркеры пользователя для страницы списка — одним HMGET."""
    ids = list(dict.fromkeys(conversation_ids))  # порядок сохраняем, дубли убираем
    if not ids:
        return {}
    values: list[str | bytes | None] = await aw(
        redis.hmget(key_for(user_id), [str(i) for i in ids])
    )
    out: dict[uuid.UUID, datetime] = {}
    for conv_id, raw in zip(ids, values, strict=False):
        parsed = _parse(raw)
        if parsed is not None:
            out[conv_id] = parsed
    return out


#: Сколько раз перечитываем маркер, если между чтением и записью его тронули.
#: Соперник тут ровно один — второй клиент ТОГО ЖЕ человека по ТОМУ ЖЕ диалогу,
#: поэтому пяти попыток хватает с запасом; потолок стоит затем, чтобы отметка
#: прочтения не умела зациклиться ни при каких данных.
_CAS_ATTEMPTS = 5


async def set_marker(
    redis: Redis, user_id: uuid.UUID, conversation_id: uuid.UUID, at: datetime
) -> datetime:
    """Поставить маркер. Назад маркер не ездит — только вперёд.

    ПОЧЕМУ ЗДЕСЬ WATCH, А НЕ ПРОСТЫЕ HGET+HSET, КАК БЫЛО.

    Условие «только вперёд» тут было и раньше — но проверялось оно одним
    запросом, а записывалось другим, и между ними помещалось всё что угодно.
    Гонка настоящая, а не теоретическая, и вот её сценарий целиком:

      1. оператор открывает диалог — уходит /read, сервер читает
         max(messages.created_at) = T1;
      2. пока запрос летит, клиент присылает новое сообщение (время T2 > T1);
      3. открытый диалог получает кадр и обновляется — уходит второй /read,
         он читает уже T2, видит пустой маркер и пишет T2;
      4. первый запрос (медленнее — он же ушёл раньше) добирается до Redis,
         тоже видит «маркера нет» (он прочитал его ДО шага 3) и пишет T1.

    Маркер уехал НАЗАД, и бейдж непрочитанного вернулся сам собой — ровно та
    жалоба «непрочитанные висят без причины», с которой началась эта правка.
    Два клиента одного человека (веб + десктоп) дают то же самое без всякого
    нового сообщения.

    Сравнение и запись обязаны быть одной операцией, поэтому оптимистическая
    транзакция: WATCH на ключ, чтение, MULTI/EXEC. Тронул кто-то ключ между
    ними — EXEC не выполнится, и мы перечитываем.

    Lua-скрипт (EVAL) сделал бы то же за один поход, но fakeredis в юнит-тестах
    исполняет Lua только с `lupa`, которого в зависимостях нет: тогда главное
    свойство маркера проверялось бы не тем кодом, что работает на проде.
    """
    at = ensure_aware(at)
    key = key_for(user_id)
    field = str(conversation_id)

    async with redis.pipeline() as pipe:
        for _ in range(_CAS_ATTEMPTS):
            try:
                await pipe.watch(key)
                current = _parse(await aw(pipe.hget(key, field)))
                keep = current is not None and current >= at
                pipe.multi()
                if not keep:
                    pipe.hset(key, field, _dump(at))
                # TTL продлеваем в любом случае: человек диалог открыл, значит
                # хэш живой, даже если время в нём не сдвинулось.
                pipe.expire(key, ttl_seconds())
                await pipe.execute()
                return current if keep else at  # type: ignore[return-value]
            except WatchError:
                continue

    # Пять раз подряд не сошлось — такого на одном человеке и одном диалоге не
    # бывает, но молчать об этом нельзя. Пишем «в лоб»: потерять секунды в
    # маркере не страшно, а вот не отметить диалог прочитанным вовсе — это
    # висящий бейдж, из-за которого всё и затевалось.
    log.warning("read_markers.cas_gave_up", user_id=str(user_id), conversation_id=field)
    await aw(redis.hset(key, field, _dump(at)))
    await redis.expire(key, ttl_seconds())
    return at


async def clear_marker(redis: Redis, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Снять маркер (диалог снова «непрочитан» для этого пользователя)."""
    await aw(redis.hdel(key_for(user_id), str(conversation_id)))


async def last_message_at(db: AsyncSession, conversation_id: uuid.UUID) -> datetime | None:
    """Время последнего сообщения диалога — куда ставится маркер при /read."""
    value = (
        await db.execute(
            select(sa.func.max(Message.created_at)).where(
                Message.conversation_id == conversation_id
            )
        )
    ).scalar()
    return ensure_aware(value) if value is not None else None


async def mark_read(
    db: AsyncSession, redis: Redis, user_id: uuid.UUID, conv: Conversation
) -> datetime:
    """POST /conversations/{id}/read (01 §5.3) — маркер ТЕКУЩЕГО пользователя.

    Глобальный ``conversations.unread_count`` при этом не трогаем: он остался
    fallback'ом для тех, у кого маркера ещё нет, и обнулять его от лица одного
    человека — ровно тот баг, ради которого маркеры и заводились.
    """
    at = await last_message_at(db, conv.id)
    if at is None:
        at = ensure_aware(conv.last_message_at) if conv.last_message_at else datetime.now(UTC)
    return await set_marker(redis, user_id, conv.id, at)


# ------------------------------------------------------------------ счётчики


async def unread_counts(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
    conversation_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """{conversation_id: непрочитанные} — ТОЛЬКО для диалогов с маркером.

    Отсутствие ключа в ответе означает «маркера нет» — вызывающий берёт
    ``conversations.unread_count`` (обратная совместимость).
    """
    markers = await get_markers(redis, user_id, conversation_ids)
    if not markers:
        return {}

    floor = min(markers.values())  # глобальная нижняя граница → prune партиций
    per_conv = [
        sa.and_(Message.conversation_id == conv_id, Message.created_at > marker)
        for conv_id, marker in markers.items()
    ]
    rows = (
        await db.execute(
            select(Message.conversation_id, sa.func.count().label("cnt"))
            .where(
                Message.direction == "in",  # непрочитанным бывает только входящее
                Message.created_at > floor,
                sa.or_(*per_conv),
            )
            .group_by(Message.conversation_id)
        )
    ).all()

    counts: dict[uuid.UUID, int] = dict.fromkeys(markers, 0)  # маркер есть → 0, а не fallback
    for conv_id, cnt in rows:
        counts[conv_id] = int(cnt)
    return counts


async def unread_for_conversation(
    db: AsyncSession, redis: Redis, user_id: uuid.UUID, conv: Conversation
) -> int:
    """Счётчик одного диалога с fallback'ом на глобальный."""
    counts = await unread_counts(db, redis, user_id, [conv.id])
    if conv.id in counts:
        return counts[conv.id]
    return conv.unread_count or 0


async def apply_unread_counts(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
    items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Проставить пер-юзерный ``unread_count`` в уже сериализованные диалоги.

    Точка интеграции для роутера: ``conversation_out`` остаётся чистым
    сериализатором строки БД, а персонализация — один вызов поверх страницы
    (и один и тот же код для списка и для детали).
    """
    rows = list(items)
    if not rows:
        return rows
    ids: list[uuid.UUID] = []
    for row in rows:
        try:
            ids.append(uuid.UUID(str(row["id"])))
        except (KeyError, ValueError):  # не наш словарь — оставляем как есть
            continue
    counts = await unread_counts(db, redis, user_id, ids)
    if not counts:
        return rows
    for row in rows:
        try:
            conv_id = uuid.UUID(str(row["id"]))
        except (KeyError, ValueError):
            continue
        if conv_id in counts:
            row["unread_count"] = counts[conv_id]
    return rows


async def prune_marker_hash(redis: Redis, user_id: uuid.UUID, keep: int = 5000) -> int:
    """Подрезать хэш маркеров, если он разросся (за годы — тысячи диалогов).

    Оставляем ``keep`` самых свежих маркеров: старые всё равно эквивалентны
    fallback'у по закрытым диалогам. Вызывается по желанию (крон/скрипт), не
    в горячем пути.
    """
    key = key_for(user_id)
    raw: dict[str, str] = await aw(redis.hgetall(key))
    if len(raw) <= keep:
        return 0
    parsed = [(field, _parse(value)) for field, value in raw.items()]
    ordered = sorted(parsed, key=lambda p: p[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
    stale = [field for field, _ in ordered[keep:]]
    if stale:
        await aw(redis.hdel(key, *stale))
        await redis.expire(key, ttl_seconds())
    return len(stale)
