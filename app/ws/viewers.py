"""Кто прямо сейчас смотрит диалог (SCEN-48; кадр `subscribe` — 01 §11.4).

ЗАЧЕМ ЭТОТ МОДУЛЬ ПОЯВИЛСЯ.

Двое диспетчеров открывали один и тот же диалог и отвечали клиенту оба —
клиент получал два ответа подряд, иногда разных по смыслу. Признака «диалог
уже открыт коллегой» не было ни на одном конце: сервер кадр `subscribe`
ПРИНИМАЛ и запоминал в сессии (`Session.conversation_id`), но никому о нём не
рассказывал, а браузер этот кадр не слал вовсе. Механизм был объявлен в
контракте и не работал целиком — ни половины.

ПОЧЕМУ REDIS, А НЕ ПАМЯТЬ ПРОЦЕССА. Процессов уже два (WEB_CONCURRENCY=2,
08 §5.1), и каждый Hub видит только свои сокеты. Двое операторов законно
оказываются в разных процессах — список, собранный из `hub.sessions`, показал
бы каждому из них его самого и больше никого, то есть ровно то же молчание,
что и раньше, только с видом работающего механизма.

ХРАНЕНИЕ. ZSET ``viewers:{conversation_id}``: член — соединение (JSON с
conn_id и человеком), вес — время последнего подтверждения жизни. Вес нужен
потому, что сокет умирает не только по закрытию: процесс сняли, вкладку
заморозил мобильный браузер, соединение молча оборвалось за NAT. Просроченные
члены выметаются перед каждым чтением, а на сам ключ стоит TTL — иначе
брошенный диалог остался бы в Redis навсегда.
"""

import json
import time
import uuid
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.ws.events import publish_event

#: Состав зрителей диалога (01 §11.3). Уезжает только тем, кто на этот диалог
#: подписан, — фильтр в `Hub._allowed` тот же, что у `typing`.
VIEWERS_EVENT = "conversation:viewers"


def _key(conversation_id: uuid.UUID | str) -> str:
    return f"viewers:{conversation_id}"


def _member(user_id: uuid.UUID | str, full_name: str, conn_id: str) -> str:
    """Член ZSET'а — одно СОЕДИНЕНИЕ, а не человек.

    Человек открывает вторую вкладку, и это не второй зритель: схлопывание по
    `id` делает `viewers()` при чтении. А вот закрытие ОДНОЙ из двух вкладок
    не должно убирать человека из списка — значит ключ уникальности обязан
    включать conn_id.

    `sort_keys` — страховка на будущее, а не находка: удаление ищет член по
    ТОЧНОМУ совпадению строки, поэтому запись и её удаление обязаны собираться
    здесь и только здесь. Сегодня строитель один и порядок и так постоянен;
    случись второй — расхождение вылезло бы не ошибкой, а зрителем-призраком,
    висящим до истечения веса.
    """
    return json.dumps(
        {"conn_id": conn_id, "full_name": full_name, "id": str(user_id)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _ttl() -> int:
    """Сколько живёт запись зрителя без подтверждения.

    Тот же срок, что у присутствия: подтверждение приходит с прикладным
    ping'ом раз в 25 с (01 §11.5), и 90 с переживают три подряд потерянных.
    """
    return settings.ws_presence_ttl_seconds


async def touch(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    *,
    user_id: uuid.UUID | str,
    full_name: str,
    conn_id: str,
) -> None:
    """Соединение открыло диалог или подтвердило, что всё ещё в нём."""
    key = _key(conversation_id)
    await redis.zadd(key, {_member(user_id, full_name, conn_id): time.time()})
    await redis.expire(key, _ttl())


async def leave(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    *,
    user_id: uuid.UUID | str,
    full_name: str,
    conn_id: str,
) -> None:
    """Соединение ушло из диалога: закрыло его, перешло в другой или умерло."""
    await redis.zrem(_key(conversation_id), _member(user_id, full_name, conn_id))


async def viewers(redis: Redis, conversation_id: uuid.UUID | str) -> list[dict[str, str]]:
    """Кто смотрит диалог — по одному разу на человека, в порядке прихода.

    Порядок прихода (вес ZSET'а) выбран сознательно: первым в списке стоит
    тот, кто открыл диалог раньше, — именно этот вопрос и решает спор о том,
    кому отвечать.
    """
    key = _key(conversation_id)
    # Выметаем молча умерших ПЕРЕД чтением, а не по расписанию: сторожа для
    # этого заводить не за что, а список без подметания врал бы именно в тот
    # момент, когда его читают.
    await redis.zremrangebyscore(key, "-inf", time.time() - _ttl())
    raw: list[Any] = await redis.zrange(key, 0, -1)
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode()
        try:
            parsed = json.loads(item)
        except ValueError:
            continue  # чужая запись в ключе — не повод ронять список
        user_id = str(parsed.get("id") or "")
        if not user_id or user_id in seen:
            continue  # две вкладки одного человека — один зритель
        seen.add(user_id)
        people.append({"id": user_id, "full_name": str(parsed.get("full_name") or "")})
    return people


async def publish_viewers(redis: Redis, conversation_id: uuid.UUID | str) -> list[dict[str, str]]:
    """Разослать состав зрителей подписанным на диалог.

    Состав едет ЦЕЛИКОМ, а не «пришёл такой-то»: кадры теряются при обрыве и
    приходят в чужом порядке после реконнекта, и собранный из приращений
    список разъехался бы с правдой — а врущий признак «вы тут один» хуже
    отсутствующего, потому что на него полагаются.
    """
    people = await viewers(redis, conversation_id)
    await publish_event(
        redis,
        VIEWERS_EVENT,
        {"conversation_id": str(conversation_id), "viewers": people},
    )
    return people
