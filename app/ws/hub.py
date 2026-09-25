"""WebSocket Hub (08 §5): per-process socket registry + Pub/Sub fan-out.

Process model at WEB_CONCURRENCY=2 (08 §5.1): every uvicorn process holds
its own Hub subscribed to the same Redis Pub/Sub channel ``events``; the
publisher never knows where a socket lives. All cross-process state
(presence, tickets) is in Redis.

Rights filtering (08 §5.3/§5.4): ``direction='note'`` is never delivered
to observer sessions (заложено на будущее — заметок в спринте 2 ещё нет),
``account:needs_reauth`` / ``meta.audience='admin'`` — admins only,
``typing`` — only sessions subscribed to the conversation.

События очереди «Входящие» (15 §2.1) фильтра ПО РОЛЯМ не имеют: очередь
видна всем, кто видит диалоги, а разделение проходит по персональному
``can_claim`` — см. каталог ниже. Фильтр ПО КАНАЛУ у них есть, и он не
косметический: список допущенных операторов едет в самом кадре, см.
:data:`INBOX_ELIGIBLE_KEY`.
"""

import asyncio
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from redis.asyncio import Redis

from app.core.rbac import ROLE_PERMISSIONS
from app.ws import viewers as viewers_registry
from app.ws.events import EVENTS_CHANNEL, publish_event, utcnow_iso
from app.ws.presence import presence_heartbeat

log = structlog.get_logger("app.ws")


# --- каталог событий очереди «Входящие» (15 §2.1, план 7.1) ------------------
#
# Конверт как у всех (01 §11.2): {type, ts, data}. Правило доставки у событий
# диалога общее — уезжают всем подключённым, потому что читать диалоги может
# любая роль; кто из них может ПРИНЯТЬ, решает персональный `can_claim`,
# который подставляет Hub._personalize. У кадров очереди правило уже: диалог
# ждёт принятия, а принять его может не всякий — см. `INBOX_ELIGIBLE_KEY`.
#
# Состав `data` подчинён одному требованию 7.1: браузер обязан перерисовать
# список без дополнительного запроса. Отсюда несимметричность —
#   `inbox:new` / `inbox:released` несут диалог ЦЕЛИКОМ (строку нужно
#       вставить в список, а у только что подключившегося её нет вовсе),
#   `inbox:claimed` — только патч (строку нужно убрать из очереди и пометить
#       занятой в «Все»; гонять ради этого весь объект незачем).
#
# Абсолютного счётчика в широковещательных кадрах нет намеренно: «Отклонить» —
# личное решение оператора, поэтому размер очереди у каждого свой, и одно
# число на всех было бы враньём для отказавшегося. Свой счётчик фронт двигает
# сам (+1/−1), сверяя его с `GET /inbox/count`; точное значение приходит
# только в персональных ответах ручек и в `inbox:declined` (only_user).
INBOX_NEW = "inbox:new"
INBOX_CLAIMED = "inbox:claimed"
INBOX_RELEASED = "inbox:released"
INBOX_DECLINED = "inbox:declined"

# Принимать диалоги может тот, кто отвечает клиентам (01 §12): admin и
# manager. Руководитель ведёт команду и не пишет клиентам, наблюдатель только
# смотрит — очередь им видна, кнопка нет.
CLAIM_PERMISSION = "messages:send"


def can_claim(role: str) -> bool:
    """Может ли роль принять диалог — по матрице прав, а не по имени роли."""
    return CLAIM_PERMISSION in ROLE_PERMISSIONS.get(role, frozenset())


# --- канал в кадре очереди (7.2) ---------------------------------------------
#
# КАДР ОЧЕРЕДИ БЕЗ КАНАЛА — ЭТО ЧУЖОЕ ОБРАЩЕНИЕ У ЧУЖОГО ОПЕРАТОРА.
#
# Было так: `inbox:new` и `inbox:released` уезжали ВСЕМ подключённым без
# разбора. У заказчика девять каналов Авито и тринадцать операторов, и каждый
# канал ведут свои люди (7.2, разбор Jivo 15 §2.2). Оператор «Парт - 723
# БЕЛЫЙ» слышал звук и видел в очереди обращения «! Парт - 7 / Ист - В43 МНЧ
# !», к которым не имеет отношения: жал «Принять» и получал 403 от
# `account_operators.assert_can_take_account`, а свою строку в это время
# терял из виду в чужом потоке. Хуже того, разделение каналов при этом
# выглядело сломанным: `GET /inbox` очередь сужает, а живая лента — нет, и
# после перезагрузки страницы половина строк пропадала. Фильтр, который
# работает только до первого кадра, доверия к очереди не оставляет.
#
# Кто допущен — знает ПУБЛИКАТОР: у него есть сессия к базе и там же считается
# `inbox.eligible_operator_ids`. Хаб — фан-аут поверх Pub/Sub, ходить в базу
# на каждый кадр ему нечем и незачем, поэтому список едет вместе с кадром.
#
# Едет он в `data`, а не в `meta`, по причине транспорта: `meta` в
# `app/ws/events.py` умеет ровно три поля (audience/exclude_user/only_user), а
# кадры очереди публикуют семь разных мест — ручки, входящий конвейер, outbox
# бота, две джобы планировщика, — и общий у них только словарь `data`. До
# браузера ключ не доезжает: `_personalize` снимает его перед отправкой, как
# `dispatch` снимает `meta` (01 §11.2).
INBOX_ELIGIBLE_KEY = "eligible_operator_ids"


def sees_every_channel(role: str) -> bool:
    """Кому назначения на каналы очередь НЕ сужают.

    Ролевая половина ``account_operators.sees_all_channels`` — у хаба есть
    только строка роли, объекта пользователя в сессии сокета нет. Условие то
    же самое и по той же причине: администратору нужно видеть всё, включая
    неразобранное (он же получает эскалацию «отказались все»), а руководитель
    и наблюдатель диалоги из очереди не берут вовсе — сузить очередь тому, кто
    не может её разобрать, значит ослепить надзор ради фильтра, который ему
    ничего не экономит.

    Считаем через матрицу прав (`can_claim`), а не сравнением роли со строкой:
    `can_answer_clients` выведена из того же `messages:send`, так что новая
    роль-оператор попадёт под фильтр сама, а новая надзорная — сама останется
    вне его.
    """
    return role == "admin" or not can_claim(role)


def _channel_allows(session: "Session", data: dict[str, Any]) -> bool:
    """Кадр очереди этого канала — этому оператору?

    ПУСТОЙ (или отсутствующий) СПИСОК ОЗНАЧАЕТ «КАНАЛ ОТКРЫТ ВСЕМ» — правило
    совместимости из шапки `app/services/account_operators.py`, и здесь оно
    обязано читаться ровно так же. Обратное прочтение стоило бы одного
    деплоя: на канале никого не назначили, кадры перестали доезжать до всех
    тринадцати, и обращения повисли молча — без ошибки и без звука. По той же
    причине правило покрывает и публикатора, который список ещё не проставил:
    молчащая очередь хуже лишнего кадра.
    """
    eligible = data.get(INBOX_ELIGIBLE_KEY)
    if not eligible:
        return True
    if sees_every_channel(session.role):
        return True
    return str(session.user_id) in {str(uid) for uid in eligible}


async def publish_inbox_new(
    redis: Redis,
    conversation: dict[str, Any],
    *,
    eligible_operator_ids: Iterable[str | uuid.UUID] | None = None,
) -> None:
    """Диалог встал в очередь: у операторов растёт счётчик и звенит звук.

    Единственный кадр очереди, который публикует НЕ ручка: диалог встаёт в
    очередь во входящем конвейере (новый чат, вернувшийся клиент, передача от
    бота), поэтому у него общий дом здесь, а не в routes/inbox.py. Три
    остальных кадра собирают ручки — так же, как `_publish_change` в
    routes/conversations.py.

    Зовётся строго ПОСЛЕ commit'а (08 §8.1). ``conversation`` — строка очереди
    из ``services.inbox.inbox_item``: у только что подключившегося оператора
    этого диалога нет вовсе, вставлять в список нечего, поэтому объект целиком.

    ``eligible_operator_ids`` — ``inbox.eligible_operator_ids`` этого диалога,
    то есть операторы ЕГО канала; кадр уедет только им (см.
    :data:`INBOX_ELIGIBLE_KEY`). Не передали — канал считается открытым всем.
    Список сортируется: кадр обязан быть воспроизводимым, иначе один и тот же
    диалог даёт разный JSON от прогона к прогону и сверять логи двух процессов
    становится нечем.
    """
    data: dict[str, Any] = {
        "conversation_id": conversation["id"],
        "conversation": conversation,
    }
    if eligible_operator_ids is not None:
        data[INBOX_ELIGIBLE_KEY] = sorted(str(uid) for uid in eligible_operator_ids)
    await publish_event(redis, INBOX_NEW, data)


async def publish_inbox_released(
    redis: Redis,
    *,
    conversation: dict[str, Any],
    released_by: dict[str, Any],
    offered_at: str | None,
    conversation_patch: dict[str, Any] | None,
    eligible_operator_ids: Iterable[str | uuid.UUID],
) -> None:
    """Диалог вернулся в очередь — тем, кому этот КАНАЛ доступен.

    ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ПОМОЩНИК, А НЕ ГОЛЫЙ `publish_event` (28.08). Хаб отбирает
    получателей `inbox:released` тем же ключом, что и у `inbox:new`
    (:data:`INBOX_ELIGIBLE_KEY`, см. `_allowed`), а класть его должен публикатор.
    Все семь публикаторов `inbox:new` его кладут — и ни один из трёх
    публикаторов `inbox:released` не клал: они собирали словарь руками. Пустой
    ключ по правилу совместимости означает «канал открыт всем», поэтому строка
    чужого клиента со звонком уезжала каждому подключённому менеджеру, а
    «Принять» упиралось в 403. Оба сторожа в тестах смотрели только на
    `inbox:new` и этого не видели.

    Список здесь ОБЯЗАТЕЛЕН и без умолчания: забыть его теперь нельзя — не
    соберётся вызов. Источник тот же, что у очереди: `inbox.eligible_operator_ids`.
    """
    data: dict[str, Any] = {
        "conversation_id": conversation["id"],
        "conversation": conversation,
        "released_by": released_by,
        "offered_at": offered_at,
        INBOX_ELIGIBLE_KEY: sorted(str(uid) for uid in eligible_operator_ids),
    }
    if conversation_patch is not None:
        data["conversation_patch"] = conversation_patch
    await publish_event(redis, INBOX_RELEASED, data)


class _SocketLike(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass
class Session:
    ws: _SocketLike
    user_id: uuid.UUID
    full_name: str
    role: str  # snapshot at connect; role change closes the socket with 4401 (08 §5.3)
    conn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    conversation_id: uuid.UUID | None = None  # one active subscription (01 §11.4)

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(frame, ensure_ascii=False))


class Hub:
    """One instance per uvicorn process; started from the api lifespan."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.sessions: dict[str, Session] = {}  # conn_id -> Session
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, ws: _SocketLike, user: Any) -> Session:
        s = Session(ws=ws, user_id=user.id, full_name=user.full_name, role=user.role)
        self.sessions[s.conn_id] = s
        return s

    def detach(self, s: Session) -> None:
        self.sessions.pop(s.conn_id, None)
        # УХОД ИЗ ДИАЛОГА ОБЯЗАН РАЗОЙТИСЬ, А НЕ ДОЖДАТЬСЯ ИСТЕЧЕНИЯ ВЕСА.
        #
        # Запись зрителя живёт 90 секунд без подтверждения (см. ws/viewers).
        # Не убери её при закрытии сокета — коллега все эти полторы минуты
        # видел бы «диалог открыт у Петра» после того, как Пётр ушёл, и отложил
        # бы клиента, которого никто не ведёт. Признак, который врёт в обе
        # стороны, не заслуживает доверия вовсе.
        if s.conversation_id is not None:
            self._spawn(self.forget_viewer(s))

    def _spawn(self, coro: Any) -> None:
        """Фоновая задача с УДЕРЖАННОЙ ссылкой.

        `detach` зовут из `finally` обработчика сокета — ждать там уже нечего,
        соединение закрыто. Ссылку держим намеренно: у задачи, на которую никто
        не смотрит, сборщик мусора вправе забрать единственную ссылку и снять
        её на середине; уход зрителя тогда не разошлётся, и вернётся ровно тот
        призрак, ради которого всё это и написано.
        """
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # цикла нет (выключение) — закрывать уже нечего
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def forget_viewer(self, s: Session) -> None:
        """Убрать соединение из зрителей его диалога и разослать новый состав.

        Отказ Redis глушится намеренно: задача фоновая и ждать её некому, а
        необработанное исключение в ней — это трейсбек в логах на КАЖДОЕ
        закрытие вкладки во время недоступности Redis. Потеря уборки не
        бесконечна: запись зрителя всё равно истечёт по весу.
        """
        cid = s.conversation_id
        if cid is None:
            return
        s.conversation_id = None
        try:
            await viewers_registry.leave(
                self.redis, cid, user_id=s.user_id, full_name=s.full_name, conn_id=s.conn_id
            )
            await viewers_registry.publish_viewers(self.redis, cid)
        except Exception:  # noqa: BLE001 — Redis недоступен: запись истечёт сама
            log.exception("ws.viewer_cleanup_failed", conn_id=s.conn_id)

    async def run(self) -> None:
        """Process-wide subscription to Pub/Sub 'events' (08 §5.1)."""
        while True:
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(EVENTS_CHANNEL)
                async for raw in pubsub.listen():
                    if raw["type"] != "message":
                        continue
                    try:
                        evt = json.loads(raw["data"])
                    except ValueError:
                        continue
                    await self.dispatch(evt)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws.hub_listener_error")  # Sentry; keep the loop alive
                await asyncio.sleep(1)

    async def dispatch(self, evt: dict[str, Any]) -> None:
        meta = evt.pop("meta", None) or {}

        if str(evt.get("type", "")).startswith("control:"):  # service events, never sent out
            if evt["type"] == "control:revoked":  # deactivation / logout / role change
                targets = [
                    s
                    for s in list(self.sessions.values())
                    if str(s.user_id) == evt.get("data", {}).get("user_id")
                ]
                for s in targets:
                    code = evt.get("data", {}).get("code", 4403)  # 4403 → /login (01 §11.7)
                    try:
                        await s.ws.close(code=code)
                    except Exception:  # noqa: BLE001 — already-dead socket
                        pass
                    self.detach(s)
            return

        for s in list(self.sessions.values()):
            if not self._allowed(s, evt, meta):
                continue
            try:
                await s.send(self._personalize(s, evt))
            except Exception:  # noqa: BLE001 — dead socket: clean up silently
                self.detach(s)

    def _allowed(self, s: Session, evt: dict[str, Any], meta: dict[str, Any]) -> bool:
        """Rights and addressing filter. Default (01 §11.3): conversation
        events go to ALL connected users — every role can read every dialog."""
        t, d = evt.get("type"), evt.get("data", {})
        if meta.get("exclude_user") == str(s.user_id):
            return False
        only_user = meta.get("only_user")
        if only_user is not None and str(only_user) != str(s.user_id):
            return False  # персональное состояние (read-маркер) — только своим вкладкам
        if meta.get("audience") == "admin" and s.role != "admin":
            return False
        # Заметки уезжают только тем, у кого есть `notes:read` (01 §6.1/§11.3).
        # Критерий — матрица прав, а не сравнение роли со строкой: иначе новая
        # роль без `notes:read` молча получала бы чужие заметки по WS, хотя
        # HTTP-лента (has_permission в routes/conversations.py) её отсекает.
        if (
            t == "message:new"
            and d.get("message", {}).get("direction") == "note"
            and "notes:read" not in ROLE_PERMISSIONS.get(s.role, frozenset())
        ):
            return False
        if (
            t == "message:deleted"
            and d.get("direction") == "note"
            and "notes:read" not in ROLE_PERMISSIONS.get(s.role, frozenset())
        ):
            return False
        if t == "account:needs_reauth" and s.role != "admin":  # admins only (01 §11.3)
            return False
        # Кадры «внутри диалога» — только подписанным на этот диалог (01 §11.3).
        # Состав зрителей попал в это же правило по той же причине, что и
        # `typing`: он бесполезен тому, у кого диалог не открыт, а рассылка
        # состава всем тринадцати на каждое открытие карточки — это 13×13
        # кадров на ровном месте.
        if t in ("typing", viewers_registry.VIEWERS_EVENT):
            cid = str(s.conversation_id) if s.conversation_id else None
            return d.get("conversation_id") == cid
        # Кадры очереди — только операторам канала (7.2). Проверка стоит здесь,
        # а не в `_personalize`: там кадр уже решено отправить, а «чужой канал»
        # — это не «отправить без кнопки», это НЕ ОТПРАВЛЯТЬ. Строка очереди
        # едет целиком, и попав к чужому оператору, она встаёт в его список
        # ровно так же, как своя.
        if t in (INBOX_NEW, INBOX_RELEASED):
            return _channel_allows(s, d)
        return True

    def _personalize(self, s: Session, evt: dict[str, Any]) -> dict[str, Any]:
        """Per-recipient fields: is_for_you in conversation:assigned (01 §11.3),
        can_claim / is_mine в событиях очереди «Входящие» (15 §2.1)."""
        t = evt.get("type")
        if t == "conversation:assigned":
            assignee = (evt.get("data", {}).get("assignee") or {}).get("id")
            return {
                **evt,
                "data": {**evt["data"], "is_for_you": assignee == str(s.user_id)},
            }
        if t in (INBOX_NEW, INBOX_RELEASED):
            # Очередь видят все роли (право на чтение диалогов есть у всех),
            # но принять может не каждый. Флаг решает за фронт сразу два
            # вопроса: рисовать ли кнопку «Принять» и играть ли звук —
            # руководителю и наблюдателю очередь не звенит, они по ней не
            # работают. Считаем по матрице прав, а не сравнением роли со
            # строкой: новая роль-оператор получит кнопку сама.
            #
            # Здесь же список допущенных сходит с кадра: он адресация, а не
            # содержимое строки. Отдать его браузеру значило бы раздать всей
            # смене состав операторов каждого канала — то, что показывает
            # экран назначения, доступный одним администраторам.
            data = {k: v for k, v in evt["data"].items() if k != INBOX_ELIGIBLE_KEY}
            return {**evt, "data": {**data, "can_claim": can_claim(s.role)}}
        if t == INBOX_CLAIMED:
            # «Занят Петром» против «принял я»: у принявшего диалог мог быть
            # принят в другой вкладке — там его надо открыть, а не гасить.
            holder = (evt.get("data", {}).get("claimed_by") or {}).get("id")
            return {**evt, "data": {**evt["data"], "is_mine": holder == str(s.user_id)}}
        return evt


async def _resubscribe(session: Session, target: uuid.UUID | None, redis: Redis) -> None:
    """Перевести сессию на другой диалог, обновив состав зрителей у обоих.

    Повторная подписка на ТОТ ЖЕ диалог — не безделица и не ошибка клиента:
    после реконнекта браузер обязан подписаться заново (сессия на сервере
    новая и про диалог не знает), и приходит он ровно туда же. Рассылать на
    это «состав изменился» незачем — состав тот же; но вес записи обновить
    надо, иначе пережившая обрыв вкладка выпадет из зрителей по времени.
    """
    previous = session.conversation_id
    uid, name, conn = session.user_id, session.full_name, session.conn_id

    if previous == target:
        if target is not None:
            await viewers_registry.touch(redis, target, user_id=uid, full_name=name, conn_id=conn)
        return

    session.conversation_id = target
    if previous is not None:
        await viewers_registry.leave(redis, previous, user_id=uid, full_name=name, conn_id=conn)
        await viewers_registry.publish_viewers(redis, previous)
    if target is not None:
        await viewers_registry.touch(redis, target, user_id=uid, full_name=name, conn_id=conn)
        # Состав рассылается ПОСЛЕ записи себя: пришедший обязан увидеть в
        # ответе и себя, и тех, кто уже сидел в диалоге, — именно этот кадр и
        # предупреждает его, что отвечать он собирается вторым.
        await viewers_registry.publish_viewers(redis, target)


async def handle_client_frame(session: Session, raw: str, redis: Redis) -> None:
    """Client frames (01 §11.4): ping / subscribe / typing; anything broken
    gets an ``error`` frame back — the socket is never dropped for bad input."""
    try:
        frame = json.loads(raw)
        ftype = frame["type"]
    except (ValueError, KeyError, TypeError):
        await session.send({"type": "error", "data": {"code": "bad_frame"}})
        return

    if ftype == "ping":
        await session.send({"type": "pong", "ts": utcnow_iso(), "data": frame.get("data")})
        await presence_heartbeat(redis, session.user_id, session.conn_id)
        # Тем же кадром подтверждаем, что диалог всё ещё открыт. Отдельного
        # кадра для этого нет намеренно: читать переписку можно молча часами,
        # и зритель, чья запись живёт 90 с, исчез бы из списка ровно у того,
        # кто внимательно читает, — а это и есть самый опасный сосед.
        if session.conversation_id is not None:
            await viewers_registry.touch(
                redis,
                session.conversation_id,
                user_id=session.user_id,
                full_name=session.full_name,
                conn_id=session.conn_id,
            )
    elif ftype == "subscribe":
        cid = (frame.get("data") or {}).get("conversation_id")
        try:
            target = uuid.UUID(cid) if cid else None
        except ValueError:
            await session.send({"type": "error", "data": {"code": "bad_frame"}})
        else:
            await _resubscribe(session, target, redis)
    elif ftype == "typing":
        cid = (frame.get("data") or {}).get("conversation_id")
        if cid:
            await publish_event(
                redis,
                "typing",
                {
                    "conversation_id": cid,
                    "source": "operator",
                    "user": {"id": str(session.user_id), "full_name": session.full_name},
                },
                exclude_user=str(session.user_id),
            )
    else:
        await session.send({"type": "error", "data": {"code": "bad_frame"}})


# --- process singleton (created in the api lifespan) ---
hub: Hub | None = None
_listener_task: asyncio.Task[None] | None = None


def init_hub(redis: Redis) -> Hub:
    """Create the Hub and start its Pub/Sub listener as a background task."""
    global hub, _listener_task
    hub = Hub(redis)
    _listener_task = asyncio.create_task(hub.run())
    return hub


def get_hub() -> Hub:
    if hub is None:
        raise RuntimeError("WS Hub is not initialized (app lifespan not started)")
    return hub


async def shutdown_hub() -> None:
    global hub, _listener_task
    if _listener_task is not None:
        _listener_task.cancel()
        try:
            await _listener_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _listener_task = None
    hub = None
