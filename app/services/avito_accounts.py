"""Жизненный цикл аккаунтов Авито (DESIGN §1.5, 08 §4.2, §6.1).

- refresh_tokens: одноразовый refresh под Redis-локом ``lock:token:{id}``;
  отзыв доступа -> status='needs_reauth' + WS-событие account:needs_reauth.
- refresh_due_accounts: выборка для scheduler'а (истечение < 2 часов).
- backfill_account: ARQ-задача догрузки истории — исторические диалоги
  создаются status='closed'; в очередь операторам поднимаются только чаты с
  непрочитанным и СВЕЖИМ последним сообщением (:data:`QUEUE_FRESH_WINDOW`),
  остальное уходит в архив; эхо-исходящие из истории сохраняются
  direction='out', sender_type='operator', sender_user_id=NULL (это контекст
  переписки, не live-эхо); ботов и по-сообщенческий WS не трогает.

ГЛУБИНА ИСТОРИИ — ТРЕБОВАНИЕ ВЛАДЕЛЬЦА ОТ 11 АВГУСТА (docs/41 §11).
«При подключении аккаунта он должен подгрузить все диалоги, которые были и
есть». Это ОТМЕНА решения от 8 августа («история до подключения не нужна,
аккаунты меняются»). Умолчание теперь :data:`HISTORY_ALL`; прежнее поведение
осталось выбором :data:`HISTORY_SINCE_CONNECT`.

Что важно знать, читая этот файл после аудита: нижней границы по
``account.created_at`` В САМОЙ ЗАГРУЗКЕ ИСТОРИИ НИКОГДА НЕ БЫЛО — она стояла
только в сверке (``app/workers/reconciliation.py``). То есть сама выкачка и до
11 августа брала всё, а «истории до подключения нет» держалось на том, что
выкачка запускается ровно один раз, в момент подключения. Поэтому здесь
появился не снос границы, а три вещи, без которых «грузить всё» опасно:
видимый ход работы с остановкой и продолжением, осознанное решение про
очередь и выбор глубины.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core.config import settings
from app.core.errors import ApiError
from app.core.observability import with_job_scope
from app.core.redis import aw
from app.db import session as db_mod
from app.integrations.avito.adapter import AvitoAdapter, ChatInfo, InboundEvent
from app.integrations.avito.client import AvitoClient, build_authorize_url
from app.integrations.avito.errors import (
    AvitoApiError,
    AvitoAuthError,
    AvitoUnavailable,
    RateLimited,
    TokenRevokedError,
    WebhookParseError,
)
from app.integrations.avito.ratelimit import AvitoRateLimiter
from app.models import AvitoAccount, Client, Conversation, Message, User

# Партиции живут в scheduler/ по историческим причинам — это забота о схеме, а
# не о расписании. Модуль тянет за собой только движок БД, цикла импортов нет.
from app.scheduler.partitions import PartitionCoverage
from app.services import avito_app, cards_catchup, crypto, dialect
from app.services.audit import write_audit
from app.services.inbound import AVITO_SYSTEM_DIRECTION, AVITO_SYSTEM_SENDER, avito_system_prefixed

log = structlog.get_logger("app.avito_accounts")

TOKEN_EXPIRES_SKEW_SECONDS = 300  # запас 5 минут (DESIGN §8.1)
REFRESH_AHEAD = timedelta(hours=2)  # scheduler: обновлять с истечением < 2 ч (08 §6.1)
LOCK_TTL_SECONDS = 30  # lock:token:{id} (08 §8.5)
# Сколько ждать чужое обновление, прежде чем признать токен несвежим (#28).
# Обновление — это один поход в Авито; пяти секунд хватает с запасом, а
# держать человека дольше нельзя.
COMPETITOR_WAIT_SECONDS = 5.0
COMPETITOR_POLL_SECONDS = 0.05
BACKFILL_PROGRESS_TTL = 7 * 24 * 3600  # backfill:{id} (08 §4.2)

#: Отдельный ключ «загрузка сорвалась».
#:
#: ЗАЧЕМ ОТДЕЛЬНЫЙ, А НЕ ЗНАЧЕНИЕ В ТОМ ЖЕ. Ключ прогресса — это ещё и точка
#: возобновления: воркер читает из него смещение числом и продолжает с него.
#: Положи туда слово «failed» — и продолжение упрётся в разбор числа, то есть
#: починка показа сломала бы саму загрузку.
BACKFILL_FAILED_TTL = 7 * 24 * 3600
CHATS_PAGE = 100

#: Сколько секунд один заход загрузки истории работает, прежде чем добровольно
#: уступить место и поставить продолжение.
#:
#: ⚠ ЭТО НЕ НАСТРОЙКА ВКУСА, А ЛЕКАРСТВО ОТ БОЕВОЙ ПОЛОМКИ (28.08).
#:
#: У воркера `job_timeout = 300` (app/workers/main.py) — на ВСЕ задачи, включая
#: эту. Загрузка тысячи с лишним чатов в пятиминутный бюджет не помещается ни
#: при какой скорости, и ARQ убивал её на 300-й секунде. Убивал `CancelledError`,
#: а он наследник BaseException — то есть мимо `except Exception` в конце задачи:
#: ни пометки о срыве, ни отчёта, ни повторной постановки. Ключ хода работы
#: оставался в фазе «loading» с застывшим временем, карточка канала показывала
#: «Загрузка сорвалась», и НИЧТО её не возобновляло.
#:
#: Замер боя 28.08 по ключу `backfill:{id}`: started_at 17:11:15,
#: updated_at 17:16:15 — ровно 300 секунд, loaded 290 из 1100. И так на десяти
#: каналах разом; ещё на двадцати загрузка не начиналась ни разу.
#:
#: Поэтому заход теперь КОРОЧЕ таймаута и заканчивается сам: на границе чата
#: сохраняет ход работы и ставит следующий заход. Запас в минуту — на то, чтобы
#: успеть дописать текущий чат и отчитаться, даже если Авито отвечает медленно.
BACKFILL_SLICE_SECONDS = 240

#: Сколько раз повторяем запрос СТРАНИЦЫ СПИСКА ЧАТОВ, прежде чем сдаться.
#:
#: ⚠ ЗАЧЕМ (боевой замер 28.08, после выкатки нарезки). За сорок минут активной
#: загрузки Авито не ответил 58 раз. Из них 57 пришлись на историю ОТДЕЛЬНЫХ
#: чатов — там отказ изолирован и стоит одного чата из тысячи. А ОДИН пришёлся
#: на страницу списка, и вот он уносил весь прогон канала: исключение выходило
#: из цикла страниц наружу, в общий `except`, тот ставил пометку «сорвалась» и
#: бросал дальше. Работа за полчаса оставалась в базе, но канал вставал до
#: следующего тика сторожа.
#:
#: Один отказ на 58 — это примерно раз в сорок минут при полной загрузке. Ровно
#: то, на что жалуется владелец: «сгрузка аккаунтов иногда срывается».
#:
#: Три попытки, потому что ConnectTimeout мгновенный и обычно одиночный: сеть
#: моргнула. Больше трёх — это уже не рябь, и упираться незачем: заход кончится
#: честно, а продолжение придёт своим чередом.
CHATS_PAGE_TRIES = 3

#: Пауза между повторами страницы. Растёт линейно: 2 с, 4 с.
CHATS_PAGE_RETRY_PAUSE = 2.0


async def _chats_page(
    client: AvitoClient,
    account: AvitoAccount,
    db: AsyncSession,
    redis: Redis,
    limiter: AvitoRateLimiter,
    *,
    offset: int,
    where: str,
) -> list[dict[str, Any]] | None:
    """Страница списка чатов с повторами. `None` — «сейчас не вышло, зайдём позже».

    ЧТО ЗДЕСЬ РАЗЛИЧАЕТСЯ, И ЭТО ГЛАВНОЕ:

    * СЕТЕВОЙ ОТКАЗ И 5xx — рябь. Повторяем; не вышло — возвращаем `None`, и
      вызывающий заканчивает ЗАХОД, а не прогон. Всё загруженное остаётся,
      продолжение поставится само.
    * 400 НА ДАЛЬНЕЙ СТРАНИЦЕ — потолок площадки, а не поломка (боевой случай
      19.08: у владельца восемь каналов по тысяче с лишним чатов, и каждый
      прогон падал на дальней странице). Отдаём наверх — там своя ветка.
    * ОТКАЗ В ДОСТУПЕ — настоящая беда: токен не приняли даже после рефреша.
      Отдельной ветки под него здесь НЕТ намеренно: `AvitoAuthError` наследует
      `AvitoApiError` и приходит со статусом 401/403, то есть правило «любой
      4xx — наверх» его уже накрывает. Отдельный `except` был бы мёртвым кодом,
      который читается как работающий заслон (диверсия по нему не краснела).

    `RateLimited` сюда не доходит: `_call` адаптера сам спит на 429 по Retry-After.
    """
    for попытка in range(1, CHATS_PAGE_TRIES + 1):
        try:
            return await _avito_call(
                client.get_chats,
                account,
                db,
                redis,
                limiter,
                account.avito_user_id,
                offset=offset,
                limit=CHATS_PAGE,
            )
        except AvitoApiError as exc:
            статус = getattr(exc, "status", None)
            # Потолок площадки и любой другой 4xx — не наше дело, наверх.
            if статус is not None and 400 <= статус < 500:
                raise
            if попытка == CHATS_PAGE_TRIES:
                log.warning(
                    "backfill.chats_page_unavailable",
                    account_id=str(account.id),
                    where=where,
                    offset=offset,
                    tries=попытка,
                    error=str(exc),
                )
                return None
            await asyncio.sleep(CHATS_PAGE_RETRY_PAUSE * попытка)
    return None


def _elapsed_clock() -> float:
    """Часы захода. Шов: тесты подменяют его, чтобы не спать четыре минуты.

    `monotonic`, а не стенные часы: перевод времени и подкрутка NTP не должны
    ни продлевать заход за таймаут воркера, ни обрывать его на первой секунде.

    ОТДЕЛЬНАЯ ФУНКЦИЯ, А НЕ `time.monotonic` НА МЕСТЕ. Подменить в тесте сам
    `time.monotonic` нельзя: это модуль, общий на весь процесс, и фальшивые
    часы достались бы заодно asyncio и клиенту Redis — прогон разваливался бы
    по причинам, к проверяемому не относящимся. Первая редакция теста именно
    на это и напоролась.
    """
    return time.monotonic()


MESSAGES_PAGE = 100

# --- глубина истории (требование владельца 11 августа, docs/41 §11) ----------

#: «Вся история»: берём переписку за всё время, что её отдаёт Авито.
HISTORY_ALL = "all"

#: «С момента подключения»: прежнее решение от 8 августа. Осталось выбором —
#: аккаунты у компании и правда меняются, и для подменного канала выкачивать
#: чужой год незачем.
HISTORY_SINCE_CONNECT = "since_connect"

HISTORY_DEPTHS = (HISTORY_ALL, HISTORY_SINCE_CONNECT)

#: Умолчание — вся история. Так просит владелец, и так честнее по отношению к
#: клиенту: оператор видит, о чём с этим человеком говорили в прошлый раз.
DEFAULT_HISTORY_DEPTH = HISTORY_ALL

#: НАСКОЛЬКО СВЕЖИМ ДОЛЖНО БЫТЬ НЕПРОЧИТАННОЕ, ЧТОБЫ ПОПАСТЬ В ОЧЕРЕДЬ.
#:
#: Импорт истории — не входящий поток. Пометка «непрочитано» на чате годовой
#: давности не значит «клиент ждёт ответа»; она значит «в вебе Авито этот чат
#: никто не открывал». Свалить такие диалоги в «Входящие» — это тринадцать
#: операторов, разгребающих прошлогоднюю переписку вместо сегодняшних заявок.
#:
#: Но и отправлять в архив ВСЁ нельзя. Переход с Jivo делается на живом
#: канале: в момент подключения там лежат чаты, где человек написал вчера
#: вечером и ответа не получил. Они и есть работа на сегодня.
#:
#: Двое суток — граница между этими двумя случаями. Заявка на ремонт техники
#: старше двух дней мертва: человек уже вызвал другого мастера. Величина
#: управленческая, а не техническая — если владелец скажет «неделя», меняется
#: одно число.
QUEUE_FRESH_WINDOW = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """Две границы одного прогона загрузки. Считается один раз, в начале.

    ``floor`` — ЧТО ГРУЗИМ: сообщения старше него не берём вовсе (``None`` —
    берём всё). ``queue_floor`` — ЧТО СЧИТАЕМ РАБОТОЙ НА СЕГОДНЯ: диалог с
    непрочитанным и последним сообщением новее этой отметки встаёт в очередь,
    остальное ложится в архив.

    Границы РАЗНЫЕ по сути, и путать их нельзя: «загрузить весь год» и
    «раздать весь год операторам» — разные требования, и владелец просил
    первое.
    """

    depth: str
    floor: datetime | None
    queue_floor: datetime
    #: ЧЕГО НЕ ТРОГАЕМ: сообщения с этой отметки и новее — живой поток, его
    #: несут вебхук и сверка. Ставит только догрузка одного чата
    #: (`backfill_conversation`): там диалог уже создан живым сообщением.
    ceiling: datetime | None = None


def make_plan(
    account: AvitoAccount, depth: str = DEFAULT_HISTORY_DEPTH, *, now: datetime | None = None
) -> BackfillPlan:
    """Границы прогона по выбранной глубине."""
    now = now or utcnow()
    if depth not in HISTORY_DEPTHS:
        # Неизвестная глубина — грузим всё: это умолчание владельца, и оно
        # безопаснее обратного. Недогруженную историю замечают через неделю
        # и словами клиента, лишнюю — сразу и своими глазами.
        log.warning("backfill.unknown_depth", depth=depth, account_id=str(account.id))
        depth = DEFAULT_HISTORY_DEPTH
    connected_at = ensure_aware(account.created_at) or now
    return BackfillPlan(
        depth=depth,
        floor=None if depth == HISTORY_ALL else connected_at,
        queue_floor=now - QUEUE_FRESH_WINDOW,
    )


def ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime — нормализуем в aware UTC для сравнений."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- WS-события -------------------------------------------------------------


async def publish_event(
    redis: Redis, type_: str, data: dict[str, Any], *, audience: str | None = None
) -> None:
    """Публикация в Pub/Sub 'events' в формате Hub'а (08 §5.3).

    Каноническая точка — app/services/events.py (зона конвейера); пока её
    нет, публикуем тот же envelope локально — формат совпадает байт в байт.
    """
    try:
        from app.services.events import publish_event as _publish  # type: ignore
    except ImportError:
        evt: dict[str, Any] = {
            "type": type_,
            "ts": utcnow().isoformat().replace("+00:00", "Z"),
            "data": data,
        }
        if audience:
            evt["meta"] = {"audience": audience, "exclude_user": None}
        await redis.publish("events", json.dumps(evt, ensure_ascii=False))
    else:
        await _publish(redis, type_, data, audience=audience)


# --- вебхук: URL и состояние регистрации ------------------------------------


def public_base_url() -> str:
    """База публичных URL (вебхук). PUBLIC_BASE_URL из env, иначе https://DOMAIN."""
    configured = str(getattr(settings, "public_base_url", "") or "")
    return (configured or f"https://{settings.domain}").rstrip("/")


def webhook_url_for(account: AvitoAccount) -> str:
    """URL вида DESIGN §1.2: {base}/api/hooks/avito/{id}?secret=..."""
    return f"{public_base_url()}/api/hooks/avito/{account.id}?secret={account.webhook_secret}"


def _webhook_state_key(account_id: uuid.UUID) -> str:
    return f"webhook:state:{account_id}"


async def set_webhook_state(
    redis: Redis, account_id: uuid.UUID, status: str, url: str | None
) -> None:
    """ok | failed | not_registered — читается GET /avito-accounts (01 §4.1)."""
    await redis.set(_webhook_state_key(account_id), json.dumps({"status": status, "url": url}))


async def get_webhook_state(redis: Redis, account_id: uuid.UUID) -> dict[str, Any]:
    raw = await redis.get(_webhook_state_key(account_id))
    if not raw:
        return {"status": "not_registered", "url": None}
    try:
        state = json.loads(raw)
    except ValueError:
        return {"status": "not_registered", "url": None}
    return state if isinstance(state, dict) else {"status": "not_registered", "url": None}


async def register_webhook(account: AvitoAccount, redis: Redis) -> bool:
    """Регистрирует вебхук на стороне Авито; итог пишет в webhook:state.
    Возвращает успех — провал не роняет подключение (аккаунт сохранён,
    кнопка перепроверки в UI дёрнет заново)."""
    client = AvitoClient()
    url = webhook_url_for(account)
    try:
        токен = crypto.decrypt_token(account.access_token_enc)
        # ⚠ ЧУЖУЮ ПОДПИСКУ МЫ ВЫТЕСНЯЕМ МОЛЧА — И ЭТО ЕДИНСТВЕННОЕ МЕСТО, ГДЕ
        # МОЖНО ХОТЯ БЫ СКАЗАТЬ ОБ ЭТОМ ВСЛУХ (28.08).
        #
        # Авито держит на аккаунт РОВНО ОДНУ подписку, и регистрация нашей
        # снимает чужую — а чужая сегодня и есть работающий JivoChat.
        # Спрашивал об этом ровно один путь из четырёх: `connect_with_keys`
        # («Подключить»), с подтверждением `takeover_confirmed`. Три остальных —
        # возврат OAuth, «Обновить подписку» и «Включить» — звали регистрацию
        # напрямую, и тринадцать диспетчеров переставали получать обращения без
        # единой строки где-либо.
        #
        # ⚠ ЗДЕСЬ МЫ НЕ БЛОКИРУЕМ, И ЭТО ОСОЗНАННО. Подтверждение перехвата
        # живёт только на сервере: во фронте `takeover_confirmed` не передаёт
        # никто (проверено грепом по `frontend/src`). Отказ с 409 на трёх новых
        # путях стал бы тупиком — человек не смог бы ни включить канал, ни
        # обновить подписку. Пока подтверждения нет в интерфейсе, делаем
        # вытеснение ГРОМКИМ: строка уровня error попадает в тревогу, и «канал
        # молчит со вторника» перестаёт быть загадкой. Блокировку с
        # подтверждением ставить владельцу — это его решение о переезде.
        # Своя обработка отказа: сама проверка вспомогательная, и её неудача не
        # имеет права отменить регистрацию. Не смогли спросить — подписываемся
        # молча, как и раньше; хуже прежнего не станет.
        try:
            чужие = await foreign_subscriptions(client, токен)
        except (AvitoApiError, OSError) as exc:
            log.warning("webhook.foreign_check_failed", account_id=str(account.id), error=str(exc))
            чужие = []
        if чужие:
            log.error(
                "webhook.foreign_subscription_replaced",
                account_id=str(account.id),
                avito_user_id=account.avito_user_id,
                subscriptions=чужие,
            )
        await client.register_webhook(токен, url)
    # DecryptError ловим НАРАВНЕ с отказом Авито и обрывом сети. Токен может
    # оказаться нечитаемым — испорчена запись, сменили ключ шифрования при
    # восстановлении из копии. Раньше это давало 500 и «Внутренняя ошибка»
    # вместо честного «подписаться не удалось»: с виду поломка системы, а на
    # деле — понятная беда одного канала.
    except (AvitoApiError, OSError, crypto.DecryptError) as exc:
        log.warning("webhook.register_failed", account_id=str(account.id), error=str(exc))
        await set_webhook_state(redis, account.id, "failed", url)
        return False
    await set_webhook_state(redis, account.id, "ok", url)
    return True


def is_our_webhook_url(url: str | None) -> bool:
    """Этот адрес подписки — наш?

    Сравниваем по НАЧАЛУ, а не целиком: полный адрес несёт `?secret=...`, свой
    у каждого канала, и у ещё не заведённого канала его просто нет. Общее у
    всех наших подписок ровно одно — куда они ведут.
    """
    return bool(url) and str(url).startswith(f"{public_base_url()}/api/hooks/avito/")


async def foreign_subscriptions(client: AvitoClient, access_token: str) -> list[str]:
    """Чьи ЕЩЁ подписки стоят на этом аккаунте Авито. Пусто — только наши или ничьих.

    ЗАЧЕМ ЭТО СПРАШИВАТЬ ПЕРЕД ПОДКЛЮЧЕНИЕМ. Авито держит на аккаунт РОВНО ОДНУ
    подписку на события. Регистрация нашей молча вытесняет чужую — а чужая
    сегодня и есть работающий JivoChat: тринадцать диспетчеров перестают
    получать обращения в ту же секунду, и снаружи это выглядит просто как
    затишье. Во время переезда это один клик до потери боевого канала.

    ОШИБКУ НЕ ПРЕВРАЩАЕМ В ЗАПРЕТ, и это осознанный выбор. Глагол у метода
    подписок в спецификации Авито записан двояко (см. `AvitoClient.
    list_subscriptions`), права на него могут быть не выданы, Авито может
    просто не ответить. Считать любую из этих причин доказательством чужой
    подписки — значит сделать подключение каналов невозможным по догадке о
    чужом API. Поэтому не смогли спросить — говорим об этом в журнал и
    пропускаем; отвечает за шаг всё равно человек, который его затеял.
    """
    try:
        items = await client.list_subscriptions(access_token)
    except (AvitoApiError, OSError) as exc:
        # ЗАМЕТНО В ЖУРНАЛЕ: это единственный след того, что защита не
        # сработала, и при разборе «почему Jivo ослеп» искать будут его.
        log.warning("subscriptions.check_skipped", error=str(exc))
        return []
    return [
        str(item.get("url"))
        for item in items
        if item.get("url") and not is_our_webhook_url(str(item.get("url")))
    ]


async def purge_history(db: AsyncSession, account_id: uuid.UUID) -> int:
    """Стереть переписку канала. Возвращает число удалённых диалогов.

    РЕШЕНИЕ ЗАКАЗЧИКА ОТ 8 АВГУСТА: «как аккаунт отключаем — сразу же удаляем
    историю, так как аккаунты мы постоянно меняем». Компания меняет учётные
    записи Авито регулярно, и переписка ушедшего аккаунта не нужна никому: она
    только занимает диск и мешается в поиске.

    ЭТО НЕОБРАТИМО, и потому вызывается ровно из двух мест — отключения и
    удаления канала, — оба с подтверждением, называющим число диалогов.

    СООБЩЕНИЯ УДАЛЯЮТСЯ ЯВНО, И ЭТО НЕ ПЕДАНТИЗМ. Здесь стояло «уходят
    каскадом за диалогами» — я это предположил и не проверил. Каскада нет:
    у `conversation_participants` и `conversation_pins` он есть, а у
    `messages` правило `NO ACTION`. На проде отключение канала падало с
    «Внутренняя ошибка сервера», и человек не мог ни отключить канал, ни
    удалить его.

    Юнит-тесты этого поймать не могли: они идут на SQLite, а он по умолчанию
    внешние ключи не проверяет вовсе. Поэтому рядом живёт ИНТЕГРАЦИОННЫЙ тест
    на настоящем PostgreSQL — единственное место, где такая ошибка видна.

    Клиентов НЕ трогаем: один и тот же человек мог писать в несколько каналов,
    и стереть его карточку значило бы обрубить историю соседнему.

    СПИСОК ID В ПАМЯТЬ НЕ СОБИРАЕМ, И ЭТО НЕ ВКУСОВЩИНА. Здесь сначала
    вычитывались ВСЕ id диалогов канала и подставлялись в `IN (...)` двумя
    запросами — по одному параметру на диалог. У протокола PostgreSQL потолок
    32767 параметров на запрос, и он жёсткий: канал, где диалогов больше,
    отключить со стиранием было НЕЛЬЗЯ ВООБЩЕ. Запрос падал, транзакция
    откатывалась — и человек оставался с каналом в промежуточном состоянии:
    вебхук на стороне Авито уже снят (мы зовём `unregister_webhook` ДО
    стирания), а канал числится работающим и переписка на месте. Кнопка
    «Отключить» — это кнопка отката, и ломается она ровно на тех каналах,
    ради которых её жмут: на больших и боевых.

    Поэтому граница «какие диалоги стирать» живёт в самой базе — подзапросом
    по `account_id`. Параметр в запросе теперь один, сколько бы диалогов ни
    было, и потолок недостижим по построению. Потолок этот, как и внешний ключ
    выше, на SQLite не воспроизводится вовсе, поэтому сам факт держит
    `tests/integration/test_purge_history_scale_pg.py`, а форму запроса —
    `tests/unit/test_purge_history_scale.py`.

    `synchronize_session=False` — потому что это чистка, а не правка объектов
    в сессии: без него SQLAlchemy на подзапрос переключается со стратегии
    `evaluate` на `fetch` и вычитывает id удаляемых сообщений обратно в
    память. Мы бы убрали потолок протокола и получили взамен потолок
    оперативной памяти на том же самом большом канале.
    """
    # Сообщения ПЕРВЫМИ: без них удаление диалогов нарушает внешний ключ.
    # `messages` партиционирована по `created_at`; DELETE по родительской
    # таблице PostgreSQL разводит по партициям сам, перечислять их не нужно.
    await db.execute(
        sa.delete(Message)
        .where(
            Message.conversation_id.in_(
                sa.select(Conversation.id).where(Conversation.account_id == account_id)
            )
        )
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(
        sa.delete(Conversation)
        .where(Conversation.account_id == account_id)
        .execution_options(synchronize_session=False)
    )
    # rowcount живёт на CursorResult; типизированный Result его не обещает.
    removed = int(getattr(result, "rowcount", 0))
    if not removed:
        return 0
    log.info("account.history_purged", account_id=str(account_id), conversations=removed)
    return removed


async def unregister_webhook(account: AvitoAccount, redis: Redis) -> bool:
    """Снимает вебхук при disable (01 §4.5). ``True`` — Авито подтвердил снятие.

    Выключение канала не блокируем: недоступность Авито не повод оставить
    канал включённым. Но и МОЛЧАТЬ о неудаче нельзя, и вот почему.

    «Отключить» — это кнопка отката. Ей пользуются в тот момент, когда
    подключение к боевому аккаунту что-то сломало и надо вернуть как было:
    например, наша подписка перебила подписку прежней системы, и та ослепла.
    Человек жмёт «Отключить», видит «канал выключен» и уходит успокоенный — а
    вебхук на стороне Авито остался наш, и прежняя система по-прежнему не
    получает ничего. Откат, который врёт о своём результате, хуже отсутствия
    отката: на отсутствие хотя бы не полагаются.

    Поэтому неудача отдельно записывается в состояние вебхука и видна на
    карточке канала — «вебхук не снят, Авито не ответил».
    """
    client = AvitoClient()
    url = webhook_url_for(account)
    try:
        await client.unregister_webhook(crypto.decrypt_token(account.access_token_enc), url)
    # DecryptError — здесь ещё важнее, чем при регистрации. Отписка вызывается
    # из «Отключить» и из удаления канала, и нечитаемый токен запирал БЕЗ ТОГО
    # И БЕЗ ДРУГОГО: канал с испорченной записью нельзя было ни выключить, ни
    # убрать из списка — только через базу. Ровно тот случай, когда система
    # держит человека в заложниках у собственной ошибки.
    except (AvitoApiError, OSError, crypto.DecryptError) as exc:
        log.warning("webhook.unregister_failed", account_id=str(account.id), error=str(exc))
        await set_webhook_state(redis, account.id, "unregister_failed", url)
        return False
    await set_webhook_state(redis, account.id, "not_registered", None)
    return True


# --- upsert аккаунта (OAuth callback) ---------------------------------------


def apply_token_response(account: AvitoAccount, data: dict[str, Any]) -> None:
    account.access_token_enc = crypto.encrypt_token(data["access_token"])
    # REFRESH-ТОКЕНА МОЖЕТ НЕ БЫТЬ, и это не поломка.
    #
    # У входа `client_credentials` его нет вовсе: доступ живёт недолго и
    # переполучается теми же ключами приложения. У входа через согласие он
    # есть и одноразовый. Раньше строка была безусловной, и подключение по
    # ключам падало бы на KeyError — с сообщением, по которому причину не
    # угадать.
    refresh = data.get("refresh_token")
    if refresh:
        account.refresh_token_enc = crypto.encrypt_token(refresh)
    account.token_expires_at = utcnow() + timedelta(
        seconds=int(data["expires_in"]) - TOKEN_EXPIRES_SKEW_SECONDS
    )
    # ВЫКЛЮЧЕННЫЙ КАНАЛ СВЕЖИЙ ТОКЕН НЕ ВОСКРЕШАЕТ.
    #
    # Строка была безусловной, и «Обновить токен» тихо включала канал, который
    # выключили НАМЕРЕННО. Поймано 11 августа на боевом: аккаунт-заглушка
    # smoke, создаваемый строго выключенным («никто не „включит“ заглушку
    # случайно» — app/cli.py), оказался в списке работающих после нескольких
    # нажатий кнопки. Для настоящего канала цена выше: «Отключить» у нас
    # необратимо стирает переписку, и человек, выключивший канал, увидел бы,
    # что тот снова принимает обращения.
    #
    # Обновление токена и включение канала — разные действия, и у второго есть
    # своя кнопка.
    if account.status != "disabled":
        account.status = "active"


def has_own_keys(account: AvitoAccount) -> bool:
    """Канал подключён своими ключами (а не через согласие)?

    Отличается от :func:`account_credentials` тем, что не расшифровывает
    секрет: вопрос «каким способом подключён канал» задаётся в местах, где
    ключи не нужны, — в выдаче карточки и при выборе способа починки.
    """
    return bool(account.client_id and account.client_secret_enc)


def account_credentials(account: AvitoAccount) -> tuple[str, str] | None:
    """Свои ключи аккаунта или ``None``, если он подключён через согласие."""
    if not account.client_id or not account.client_secret_enc:
        return None
    try:
        return account.client_id, crypto.decrypt_token(bytes(account.client_secret_enc))
    except crypto.DecryptError:
        # Ключ шифрования сменился при восстановлении из копии. Молчать нельзя:
        # аккаунт перестанет обновлять доступ, а причина будет не видна.
        log.warning("account.secret_unreadable", account_id=str(account.id))
        return None


async def connect_with_keys(
    db: AsyncSession,
    redis: Redis,
    *,
    client_id: str,
    client_secret: str,
    takeover_confirmed: bool = False,
) -> tuple[AvitoAccount, bool]:
    """Подключить аккаунт ПО КЛЮЧАМ, без согласия и переходов.

    РЕШЕНИЕ ЗАКАЗЧИКА: «давай так, чтобы для привязки нам был нужен только
    Client Secret и ID». Аккаунтов девять, пароли от них у разных людей, и
    «пришлите ключи» — просьба, которую можно выполнить, не отдавая пароль.

    Как это работает: у Авито есть вход `client_credentials`, и все нужные
    методы мессенджера его принимают (проверено по спецификации, docs/26).
    Просим по ключам токен, спрашиваем у Авито, чей это аккаунт, и заводим
    его у себя. Ни адреса возврата, ни страницы согласия.

    Ключи сохраняем: refresh-токена в этом входе нет, и доступ придётся
    переполучать ими же. Шифруем тем же ключом, что и токены.

    ``takeover_confirmed`` — «да, я знаю, что отбираю канал у чужой подписки».
    Без него подключение аккаунта, который сейчас обслуживает другая система,
    ОТКАЗЫВАЕТСЯ с 409: см. ниже.
    """
    # Настройки перечитываются ИЗ БАЗЫ, а не из кэша процесса.
    # Владелец переключил систему на боевой Авито, экран это показал, а
    # подключение продолжало ходить в имитатор и возвращать один и тот же
    # выдуманный аккаунт — «ничего не привязывается».
    client = await AvitoClient.fresh(db)
    tokens = await client.client_credentials_token(client_id, client_secret)
    profile = await client.get_self(tokens["access_token"])

    # ЧУЖАЯ ПОДПИСКА — ОСТАНОВКА, А НЕ ПРИМЕЧАНИЕ (SCEN-32).
    #
    # Авито держит на аккаунт РОВНО ОДНУ подписку на события. Подключение
    # регистрирует нашу сразу и без вопросов, а значит вытесняет ту, что стоит
    # сейчас, — и сегодня это работающий JivoChat на боевых аккаунтах
    # заказчика. Тринадцать диспетчеров перестают получать обращения в ту же
    # секунду, никакого сообщения об этом ни у нас, ни у них нет, и снаружи
    # это выглядит просто как затишье. Во время переезда «Подключить» был
    # одним кликом до потери боевого канала.
    #
    # СПРАШИВАЕМ ДО ЕДИНОЙ ЗАПИСИ В БАЗУ. Отказ обязан не оставлять следов:
    # человек не давал согласия ни на что, и полузаведённый канал в списке
    # был бы ровно тем «а я думал, не подключилось», из которого потом
    # вырастает второе нажатие.
    #
    # ПОДТВЕРЖДЕНИЕ НЕ ХРАНИМ. Оно про ОДНО нажатие, а не про аккаунт:
    # следующее подключение того же канала снова спросит, если чужая подписка
    # снова окажется на месте. Забыть согласие дешевле, чем однажды применить
    # прошлогоднее.
    if not takeover_confirmed:
        strangers = await foreign_subscriptions(client, tokens["access_token"])
        if strangers:
            log.warning(
                "connect.foreign_subscription",
                avito_user_id=profile.get("id"),
                subscriptions=strangers,
            )
            raise ApiError(
                "conflict",
                status=409,
                message=(
                    "На этом аккаунте Авито уже стоит подписка на события — сейчас его "
                    "обслуживает другая система (обычно это Jivo). Авито держит на аккаунт "
                    "ровно одну подписку: подключение заберёт канал себе, и та система "
                    "перестанет получать обращения от клиентов. Подтвердите, что канал "
                    "нужно забрать."
                ),
                details={
                    "reason": "subscription_taken",
                    # Адреса чужих подписок — чтобы человек мог опознать, чья
                    # она, и не гадать. Иначе выбор «забирать или нет» делается
                    # вслепую.
                    "subscriptions": strangers,
                    # Поле, которое надо прислать повторным запросом.
                    "confirm_field": "takeover_confirmed",
                },
            )

    account, created = await upsert_account(db, profile, tokens)
    account.client_id = client_id
    account.client_secret_enc = crypto.encrypt_token(client_secret)
    await db.flush()
    log.info(
        "account.connected_with_keys",
        account_id=str(account.id),
        avito_user_id=account.avito_user_id,
        created=created,
    )
    return account, created


async def get_account_by_avito_user_id(db: AsyncSession, avito_user_id: int) -> AvitoAccount | None:
    stmt = select(AvitoAccount).where(AvitoAccount.avito_user_id == avito_user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def upsert_account(
    db: AsyncSession, profile: dict[str, Any], tokens: dict[str, Any]
) -> tuple[AvitoAccount, bool]:
    """Создать/обновить avito_accounts по профилю get_self (DESIGN §8.1).
    Возвращает (account, created). Commit — за вызывающим кодом."""
    avito_user_id = int(profile["id"])
    account = await get_account_by_avito_user_id(db, avito_user_id)
    created = account is None
    if account is None:
        account = AvitoAccount(
            title=str(profile.get("name") or f"Avito {avito_user_id}"),
            avito_user_id=avito_user_id,
            access_token_enc=b"",  # заполняется apply_token_response ниже
            refresh_token_enc=b"",
            token_expires_at=utcnow(),
            webhook_secret=secrets.token_urlsafe(32),
        )
        db.add(account)
    apply_token_response(account, tokens)
    await db.flush()  # получить account.id для URL вебхука/аудита
    return account, created


# --- refresh токенов (DESIGN §1.5 / §8.1, 07 §1.1.4) ------------------------


async def _take_competitor_result(
    account: AvitoAccount, db: AsyncSession, redis: Redis, lock_key: str
) -> bool:
    """Дождаться чужого обновления и забрать его результат.

    ЗАЧЕМ ЖДАТЬ, А НЕ ВЕРНУТЬ «ЗАНЯТО» СРАЗУ. Раньше возвращалось False, и это
    калечило здоровый канал (#28). Токен протух, два процесса получили 401
    одновременно; первый ушёл обновляться, второму отвечали «занято». Второй
    перечитывал строку, видел статус «active» — аккаунт же исправен, — и
    повторял запрос ТЕМ ЖЕ мёртвым токеном. Второй 401 подряд, и отправка
    падала с ошибкой доступа на канале, с которым всё было в порядке.

    Беда была в том, что False означала две несовместимые вещи: «подожди,
    обновляет другой» и «не вышло, нужен повторный вход». Различить их
    вызывающий код не мог и для обеих выбирал неправильное поведение. Теперь
    ожидание съедает первый случай целиком, и False снова значит ровно одно.

    ЧУЖОЙ ОДНОРАЗОВЫЙ REFRESH НЕ ТРОГАЕМ — в этом весь смысл лока: сжечь его
    дважды значит потерять доступ к аккаунту по-настоящему.

    ПОЧЕМУ ОЖИДАНИЕ ОГРАНИЧЕНО. Держатель лока может умереть, не сняв его, —
    тогда ключ провисит все 30 секунд TTL. Ждать столько нельзя: на том конце
    человек ждёт ответа. По истечении предела честно отвечаем «не свежие».
    """
    #  Признак чужой удачи — переписанная строка токена. Сравниваем именно
    #  шифротекст: он меняется только тогда, когда обновление реально
    #  состоялось и было записано.
    before = bytes(account.access_token_enc)
    deadline = time.monotonic() + COMPETITOR_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(COMPETITOR_POLL_SECONDS)
        if await redis.get(lock_key) is None:
            break

    await db.refresh(account)
    if account.status != "active":
        return False  # победитель упёрся в отзыв доступа — бодриться не о чем
    return bytes(account.access_token_enc) != before


def _keys_rejected(exc: BaseException) -> bool:
    """Отказ Авито означает «ключи не приняты», а не «Авито сейчас не отвечает»?

    Ключи постоянные, поэтому НАСТОЯЩИЙ отказ — только явный ответ Авито из
    четырёхсотых: приложение удалили или отключили в кабинете. Всё остальное
    временно и лечится повтором:

    * сеть, DNS, таймаут (``AvitoUnavailable``, статуса нет вовсе);
    * 5xx — беда на стороне Авито;
    * 429 — превышен лимит запросов, канал тут ни при чём.
    """
    if isinstance(exc, AvitoUnavailable) or isinstance(exc, OSError):
        return False
    status = getattr(exc, "status", None)
    if status is None:
        return False
    return 400 <= status < 500 and status != 429


async def _announce_needs_reauth(
    db: AsyncSession, redis: Redis, account: AvitoAccount, *, reason: str
) -> None:
    """Сказать администраторам, что канал больше не может отвечать.

    ДВА КАНАЛА, И ОБА НУЖНЫ. Кадр в сокет — чтобы карточка канала покраснела у
    того, кто сейчас смотрит на экран. Запись в центр уведомлений — чтобы
    новость пережила ночь: до 11 августа её не было вовсе, вид
    ``account.needs_reauth`` жил в каталоге, был покрыт двадцатью тестами и не
    создавался НИ ОДНОЙ строкой боевого кода. Канал отваливался в тишине, и
    узнавал об этом только тот, кто пробовал ответить клиенту.
    """
    await publish_event(
        redis,
        "account:needs_reauth",
        {"account_id": str(account.id), "title": account.title},
        audience="admin",
    )
    # Импорт внутри функции: support тянет за собой центр уведомлений, а он —
    # модели и события. На уровне модуля это замкнуло бы круг импортов.
    from app.services.support import CRITICAL, NotificationDraft, send_notification

    await send_notification(
        db,
        redis,
        NotificationDraft(
            kind="account.needs_reauth",
            severity=CRITICAL,
            title="Аккаунт Авито требует переподключения",
            body=(
                f"Канал «{account.title}» не может отправлять ответы: {reason}. "
                "Обращения от клиентов при этом продолжают приходить."
            ),
            entity_type="account",
            entity_id=str(account.id),
        ),
    )


async def announce_webhook_lost(db: AsyncSession, redis: Redis, account: AvitoAccount) -> None:
    """Сказать администраторам, что канал подключён, а обращения не пойдут.

    ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ У ОДНОГО ВЫЗЫВАЮЩЕГО. Провал подписки — это
    состояние «канал есть, событий нет», и оно уже описано видом
    `webhook.lost` со своей кнопкой «Перерегистрировать» (14 §2.1). Второй вид
    под то же состояние означал бы две разные красные плашки про одну беду и
    две кнопки, делающие одно и то же.

    ТЕКСТ СВОЙ, А НЕ КАТАЛОЖНЫЙ. Заголовок вида — «Канал отобрали: подписка на
    события пропала», и он верен для сторожа, который поймал пропажу у
    работающего канала. Здесь случай другой и человеку надо сказать именно
    его: подписка не пропала, она НЕ ВСТАЛА, и произошло это только что, у
    него на глазах, в ответ на его же нажатие.

    Склейка у вида — по ВИДУ (`dedup="kind"`), поэтому подключение девяти
    каналов подряд с недоступным Авито даст одну строку со счётчиком, а не
    девять плашек. Кнопка «Перерегистрировать» чинит все затронутые каналы
    разом (`rewebhook_action`), так что склейка ничего не теряет.
    """
    # Импорт внутри функции: support тянет за собой центр уведомлений, а он —
    # модели и события. На уровне модуля это замкнуло бы круг импортов (тот же
    # приём, что в `_announce_needs_reauth` ниже).
    from app.services.support import CRITICAL, NotificationDraft, send_notification

    await send_notification(
        db,
        redis,
        NotificationDraft(
            kind="webhook.lost",
            severity=CRITICAL,
            title="Канал подключён, но обращения не пойдут",
            body=(
                f"Авито не принял подписку на события канала «{account.title}». "
                "Канал сохранён и токены на месте, но сообщения клиентов к нам не "
                "поступают. Нажмите «Перерегистрировать» — это чинится одним запросом."
            ),
        ),
    )


#: Исходы обновления токена (проверка 24.09). Раньше наружу выходил один
#: bool, и ручки гадали: у активного канала False читалось «уже обновляется»,
#: у канала в needs_reauth — «Авито отозвал доступ», хотя на своих ключах это
#: была обычная недоступность Авито, и человек шёл чинить то, что не сломано.
REFRESH_OK = "ok"
REFRESH_BUSY = "busy"
REFRESH_UNAVAILABLE = "unavailable"
REFRESH_REVOKED = "revoked"


async def refresh_tokens(
    account: AvitoAccount,
    db: AsyncSession,
    redis: Redis,
    *,
    wait_for_competitor: bool = True,
) -> bool:
    """True, если токены в БД свежие. Исход подробнее — :func:`refresh_outcome`."""
    outcome = await refresh_outcome(account, db, redis, wait_for_competitor=wait_for_competitor)
    return outcome == REFRESH_OK


async def refresh_outcome(
    account: AvitoAccount,
    db: AsyncSession,
    redis: Redis,
    *,
    wait_for_competitor: bool = True,
) -> str:
    """Refresh с локом: refresh_token одноразовый, двойное использование
    сжигает его. Исход — `REFRESH_OK` (токены в БД свежие), `REFRESH_BUSY`
    (обновляет другой процесс и не дождались), `REFRESH_UNAVAILABLE` (Авито
    не ответил; ключи или refresh целы — повторить) или `REFRESH_REVOKED`
    (доступ отозван, канал в needs_reauth). Сбой сети на обмене refresh
    (подключение через согласие) поднимается исключением, как и раньше.

    ``wait_for_competitor=False`` — для планировщика: ему нечего повторять,
    он просто обходит аккаунты, и ждать чужой работы ему незачем.

    400 от token-эндпоинта — два разных случая:
    - конкурентная ротация: другой процесс уже обменял refresh, в БД лежит
      новый — перечитываем строку и повторяем с ним (needs_reauth НЕ ставим);
    - реальный отзыв доступа: перечитанный refresh совпадает со сгоревшим ->
      status='needs_reauth' + событие account:needs_reauth админам (08 §6.1).

    КАЖДЫЙ ИСХОД ЗАПИСЫВАЕТСЯ В ЖУРНАЛ ОБНОВЛЕНИЯ (12 августа,
    ``app/services/channel_health.py``). До этого наблюдаемым фактом был
    только срок истечения токена, и карточка канала не могла ответить ни на
    «когда обновление в последний раз получилось», ни на «а оно вообще
    получалось». Отсюда и жёлтая строка на всех каналах круглосуточно: любой
    срок без доказательства работающей автоматики выглядит угрозой.

    Конкурентную попытку (лок занят) в журнал НЕ пишем: там ничего не
    произошло — ни успеха, ни неудачи, — а «неудача подряд» из-за чужого лока
    покрасила бы карточку красным на ровном месте.
    """
    # Импорт внутри функции: ``channel_health`` читает этот модуль (пороги,
    # адрес вебхука), и на уровне модуля это замкнуло бы круг импортов. Тот же
    # приём, что с центром уведомлений в ``_announce_needs_reauth``.
    from app.services import channel_health

    lock_key = f"lock:token:{account.id}"
    if not await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS):
        # Обновляет другой — ждём его и забираем результат (#28).
        if not wait_for_competitor:
            return REFRESH_BUSY
        if await _take_competitor_result(account, db, redis, lock_key):
            return REFRESH_OK
        # Строку уже перечитал `_take_competitor_result`: отзыв победитель записал.
        return REFRESH_REVOKED if account.status == "needs_reauth" else REFRESH_BUSY
    try:
        # Из базы, а не из кэша процесса: иначе обновление токена ушло бы в
        # имитатор после того, как владелец переключил систему на боевой Авито.
        client = await AvitoClient.fresh(db)

        # АККАУНТ СО СВОИМИ КЛЮЧАМИ ОБНОВЛЯЕТСЯ ИНАЧЕ.
        #
        # У входа `client_credentials` refresh-токена нет: доступ просто
        # переполучается той же парой ключей. Вся сложная механика ниже —
        # одноразовость refresh, лок от двойного использования, разбор «сгорел
        # или отозвали» — к нему не относится. Здесь нечему сгорать: ключи
        # постоянные, и повторный запрос токена безопасен.
        credentials = account_credentials(account)
        if credentials is not None:
            try:
                data = await client.client_credentials_token(*credentials)
            except (AvitoApiError, OSError) as exc:
                if not _keys_rejected(exc):
                    # ВРЕМЕННАЯ БЕДА — СТАТУС НЕ ТРОГАЕМ.
                    #
                    # Здесь стояло `needs_reauth` на любую ошибку, и это был
                    # неверный диагноз. `client_id`/`client_secret` Авито выдаёт
                    # ОДИН РАЗ и не меняет (подтверждено владельцем 11 августа):
                    # «ключи отозвали» — не сценарий, отозвать их можно только
                    # удалив приложение в кабинете. Значит в эту ветку попадала
                    # почти всегда обычная недоступность Авито, а канал за неё
                    # выключался насовсем: обновление токенов, сверка и оба
                    # сторожа перебирают только активные аккаунты, и обратно
                    # система не выходила.
                    #
                    # Правильное поведение с постоянными ключами одно: повторить.
                    # Следующий обход планировщика (каждые 30 минут) попробует
                    # снова теми же рабочими ключами.
                    log.warning(
                        "token.client_credentials_unavailable",
                        account_id=str(account.id),
                        error=str(exc),
                        status=getattr(exc, "status", None),
                    )
                    await channel_health.note_refresh_failed(
                        redis, account.id, reason="Авито не ответил"
                    )
                    return REFRESH_UNAVAILABLE
                # Ключи не приняты по-настоящему: приложение удалили или
                # отключили в кабинете Авито. Других ключей не существует,
                # поэтому «переподключение» здесь означает включить приложение
                # обратно, а не ввести новую пару.
                account.status = "needs_reauth"
                await db.commit()
                log.warning(
                    "token.client_credentials_failed",
                    account_id=str(account.id),
                    error=str(exc),
                )
                await _announce_needs_reauth(
                    db,
                    redis,
                    account,
                    reason="ключи приложения больше не принимаются — проверьте, "
                    "не удалено ли и не отключено ли приложение в кабинете Авито",
                )
                await channel_health.note_refresh_failed(
                    redis, account.id, reason="Авито не принял ключи приложения"
                )
                return REFRESH_REVOKED
            apply_token_response(account, data)
            await db.commit()
            log.info("token.refreshed_with_keys", account_id=str(account.id))
            await channel_health.note_refresh_ok(redis, account.id)
            return REFRESH_OK

        for attempt in (1, 2):
            used_refresh_enc = bytes(account.refresh_token_enc)
            try:
                data = await client.refresh_token(crypto.decrypt_token(used_refresh_enc))
            except TokenRevokedError:
                await db.refresh(account)  # перечитать: не ротировал ли кто-то параллельно
                if bytes(account.refresh_token_enc) != used_refresh_enc and attempt == 1:
                    log.info(
                        "token.refresh_retry_with_rotated",
                        account_id=str(account.id),
                    )
                    continue  # в БД уже новый refresh — пробуем его
                account.status = "needs_reauth"
                await db.commit()
                log.warning("token.refresh_revoked", account_id=str(account.id))
                await _announce_needs_reauth(
                    db,
                    redis,
                    account,
                    reason="Авито отозвал доступ, нужно пройти подключение заново",
                )
                await channel_health.note_refresh_failed(
                    redis, account.id, reason="Авито отозвал доступ"
                )
                return REFRESH_REVOKED
            apply_token_response(account, data)
            await db.commit()
            log.info("token.refreshed", account_id=str(account.id))
            await channel_health.note_refresh_ok(redis, account.id)
            return REFRESH_OK
        return REFRESH_REVOKED  # недостижимо: вторая итерация всегда завершает выше
    finally:
        await redis.delete(lock_key)


async def refresh_due_accounts(db: AsyncSession | None = None, redis: Redis | None = None) -> int:
    """Для scheduler'а (08 §6.1): обновить активные аккаунты с истечением
    < 2 часов. Ошибка одного аккаунта не прерывает остальные.

    Scheduler зовёт без аргументов (app/scheduler/main.py) — тогда сессия
    и Redis берутся из процессных синглтонов."""
    if db is None:
        # session_scope, а не factory(): освобождение соединения (rollback+close)
        # должно жить в ОДНОМ месте — иначе инвариант пула разъедется (05 §8).
        async with db_mod.session_scope() as own_db:
            return await refresh_due_accounts(own_db, redis)
    if redis is None:
        redis = redis_mod.get_client()

    # КАНАЛ НА КЛЮЧАХ БЕРЁМ И ИЗ `needs_reauth` — ИНАЧЕ ОН НЕ ВЫЙДЕТ ОТТУДА САМ.
    #
    # Раньше здесь стояло только `status == "active"`, и это замыкало круг:
    # канал попадал в `needs_reauth` от сетевой ошибки и переставал обновляться,
    # потому что обновляются только активные. Выйти можно было лишь кнопкой,
    # которая для канала на ключах ещё и отказывала.
    #
    # Ключи постоянные, поэтому повторная попытка ими безопасна и почти всегда
    # успешна: `apply_token_response` вернёт каналу `active` сам. Канал через
    # согласие сюда не попадает намеренно — там refresh одноразовый, отозванный
    # не оживёт, и лишний поход в Авито ничего не починит.
    accounts = (
        (
            await db.execute(
                select(AvitoAccount).where(
                    # ⚠ СЛУЖЕБНАЯ ЗАГЛУШКА СЮДА НЕ ПОПАДАЕТ (аудит 19.08,
                    # находка L-003). Все сторожа её отсеивают предикатом
                    # `is_service`, а этот обход — единственный боевой — не
                    # отсеивал: заглушка с мёртвым токеном и без ключей шла в
                    # обновление наравне с боевыми каналами. Вреда на бою не
                    # было (ошибок токенов за сутки ноль), но обход, который
                    # ходит в Авито за несуществующим токеном, — это шум,
                    # который однажды прочтут как настоящую беду.
                    AvitoAccount.is_service.is_(False),
                    sa.or_(
                        AvitoAccount.status == "active",
                        sa.and_(
                            AvitoAccount.status == "needs_reauth",
                            AvitoAccount.client_id.is_not(None),
                            AvitoAccount.client_secret_enc.is_not(None),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    deadline = utcnow() + REFRESH_AHEAD
    refreshed = 0
    for account in accounts:
        expires_at = ensure_aware(account.token_expires_at)
        if expires_at is not None and expires_at >= deadline:
            continue
        try:
            if await refresh_tokens(account, db, redis, wait_for_competitor=False):
                refreshed += 1
        except Exception:
            log.exception("token.refresh", account_id=str(account.id), outcome="error")
    return refreshed


# --- постановка backfill-задачи ---------------------------------------------


async def enqueue_backfill(
    account_id: uuid.UUID,
    depth: str = DEFAULT_HISTORY_DEPTH,
    *,
    dedupe: bool = True,
) -> None:
    """Ставит ARQ-задачу 'backfill_account'.

    ``dedupe=True`` — постоянный ``_job_id``: двойное подключение не породит
    два прогона (08 §4.2). Так зовёт подключение канала.

    ``dedupe=False`` — уникальный ``_job_id``, и это НЕ мелочь. ARQ держит
    ключ выполненной задачи ещё час (``keep_result``) и повторную постановку с
    тем же идентификатором молча отбрасывает. То есть «продолжить загрузку»
    после остановки в течение часа не делало бы РОВНО НИЧЕГО, и человек видел
    бы застывший ход работы без единой ошибки. Осознанный повторный запуск
    (консоль) дедупу не подлежит: его смысл в том, чтобы прогон начался.
    """
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
    except ImportError:
        # Воркер-процесс (зона конвейера) приносит arq в зависимости; до его
        # появления подключение аккаунта не должно падать из-за очереди.
        log.warning(
            "backfill.enqueue_skipped", account_id=str(account_id), reason="arq_not_installed"
        )
        return
    job_id = f"backfill:{account_id}" if dedupe else f"backfill:{account_id}:{uuid.uuid4().hex}"
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("backfill_account", account_id, depth, _job_id=job_id)
    finally:
        await pool.aclose()


# --- ход загрузки: ключи, состояние, остановка -------------------------------


def _progress_key(account_id: uuid.UUID) -> str:
    return f"backfill:{account_id}"


def _seen_key(account_id: uuid.UUID) -> str:
    return f"backfill:seen:{account_id}"


def _failed_key(account_id: uuid.UUID) -> str:
    return f"backfill:failed:{account_id}"


def _stop_key(account_id: uuid.UUID) -> str:
    return f"backfill:stop:{account_id}"


async def request_backfill_stop(redis: Redis, account_id: uuid.UUID) -> None:
    """Попросить прогон остановиться (кнопка/команда «Остановить»).

    Флаг, а не отмена задачи: прогон обязан остановиться НА ГРАНИЦЕ ЧАТА, уже
    записав в базу то, что успел, и оставив точку возобновления. Убитая
    посреди чата задача оставила бы наполовину загруженный диалог и ключ
    прогресса, который врёт.
    """
    await redis.set(_stop_key(account_id), "1", ex=BACKFILL_PROGRESS_TTL)
    log.info("backfill.stop_requested", account_id=str(account_id))


class BackfillProgress:
    """Ход загрузки истории — то, что человек видит на карточке канала.

    ЗАЧЕМ ЭТО СЛОЖНЕЕ ОДНОГО ЧИСЛА. Раньше в ключе лежало смещение страницы
    чатов, и карточка писала «Загружаем историю… 300 чатов». На вопрос
    «сколько осталось» такая строка не отвечает, а при девяти аккаунтах по
    300+ объявлений он единственный, который задают. Поэтому сначала перепись
    (сколько чатов всего), потом загрузка (сколько из них позади).

    ЧТО В КЛЮЧЕ ЛЕЖИТ JSON, А НЕ ЧИСЛО. Прежний формат — голое число —
    читается и сейчас: прогон, начатый до выкатки, доживёт свой век и покажет
    хотя бы смещение, а не «Загрузка сорвалась». Разбор старого формата стоит
    трёх строк, а его отсутствие стоило бы ложной тревоги на боевом канале в
    день выкатки.
    """

    def __init__(
        self,
        redis: Redis,
        account_id: uuid.UUID,
        *,
        depth: str,
        processed: int = 0,
        loaded: int = 0,
        queued: int = 0,
    ) -> None:
        self._redis = redis
        self._account_id = account_id
        self.depth = depth
        self.phase = "census"
        self.total: int | None = None
        # РАЗОБРАННЫЕ и ЗАГРУЖЕННЫЕ — разные числа, и оба нужны. Чат может
        # оказаться пустым или старее выбранной глубины: он позади, но
        # диалогом не стал. Полоса хода идёт по разобранным (иначе она
        # застрянет, не дойдя до конца), а человеку называется загруженное —
        # он спрашивает про диалоги, а не про чаты.
        self.processed = processed
        self.loaded = loaded
        self.queued = queued
        self.failed_chats = 0
        self.started_at = utcnow()

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "depth": self.depth,
            "total": self.total,
            "loaded": self.loaded,
            "queued": self.queued,
            "failed_chats": self.failed_chats,
            # Совместимость: карточка и ручка знали это поле как «сколько
            # чатов позади». Смысл сохранён, поэтому имя оставлено.
            "chats_offset": self.processed + self.failed_chats,
            "started_at": self.started_at.isoformat(),
            "updated_at": utcnow().isoformat(),
        }

    async def flush(self) -> None:
        await self._redis.set(
            _progress_key(self._account_id),
            json.dumps(self.as_dict(), ensure_ascii=False),
            ex=BACKFILL_PROGRESS_TTL,
        )
        # 17.08 «история появляется в реальном времени»: лёгкий кадр всем —
        # карточка канала живёт без F5, открытый список чатов подтягивает
        # новые строки. Кадр — НЕ ЧАЩЕ РАЗА В 3 СЕКУНДЫ: в фазе переписи flush
        # зовётся на каждую быструю страницу, и без дросселя у операторов
        # «всё начинало скакать» (жалоба владельца 17.08) — плюс финальный
        # кадр по завершении фазы публикуется всегда.
        import time as _time

        _now = _time.monotonic()
        _last = getattr(self, "_last_frame_at", 0.0)
        # реальные финальные фазы — stopped (и кадр idle шлётся отдельно при
        # завершении задачи, см. _announce_backfill_final): их не глотаем
        if self.phase != "stopped" and _now - _last < 3.0:
            return
        self._last_frame_at = _now
        try:
            from app.ws.events import publish_event

            await publish_event(
                self._redis,
                "account:backfill",
                {"account_id": str(self._account_id), **self.as_dict()},
            )
        except Exception:  # noqa: BLE001 — кадр не стоит упавшей загрузки
            pass

    async def stop_requested(self) -> bool:
        return bool(await self._redis.get(_stop_key(self._account_id)))


# --- backfill истории (08 §4.2 + решения владельца №1/№3) --------------------


async def _avito_call(
    fn: Any,
    account: AvitoAccount,
    db: AsyncSession,
    redis: Redis,
    limiter: AvitoRateLimiter,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Вызов API из bulk-контура: бюджет лимитера, сон на 429 (Retry-After —
    истина), ровно один авто-рефреш на 401/403 (DESIGN §8.2)."""
    # Ключи и адреса Авито владелец меняет из интерфейса, а клиент читает их
    # из кэша процесса. Обновляем кэш здесь: это единственное место, через
    # которое проходят почти все обращения к Авито, и здесь есть сессия базы.
    await avito_app.ensure_fresh(db)
    refreshed = False
    ограничений = 0
    while True:
        await limiter.acquire(str(account.id), bucket="bulk")
        token = crypto.decrypt_token(account.access_token_enc)
        try:
            return await fn(token, *args, **kwargs)
        except RateLimited as exc:
            # ⚠ 429 БОЛЬШЕ НЕ ПРОХОДИТ БЕССЛЕДНО (разбор 03.09).
            #
            # Здесь стоял `while True` со сном и без единой строки в журнал:
            # площадка могла тормозить нас часами, а узнать об этом было
            # неоткуда — ни счётчика попыток, ни следа. Соседняя ветка про
            # токен счётчик как раз держит.
            #
            # Уровень warning, а не error, и НЕ в центр уведомлений: 429 —
            # штатная реакция площадки на массовую загрузку, и алерт на
            # каждый превратил бы центр в ленту одинаковых строк (тот же
            # довод уже записан в watchdog.py).
            ограничений += 1
            log.warning(
                "avito.rate_limited",
                account_id=str(account.id),
                retry_after=exc.retry_after,
                attempt=ограничений,
            )
            await asyncio.sleep(exc.retry_after)
        except AvitoAuthError:
            if refreshed:
                raise
            refreshed = True
            # Ожидание конкурента — внутри refresh_tokens (#28). Здесь False
            # означает «свежего токена нет», и повторять тем же незачем.
            if not await refresh_tokens(account, db, redis):
                raise


async def _get_or_create_client(
    db: AsyncSession, external_id: str, name: str | None, avatar_url: str | None = None
) -> Client:
    stmt = select(Client).where(Client.channel == "avito", Client.external_id == external_id)
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        client = Client(channel="avito", external_id=external_id, name=name, avatar_url=avatar_url)
        db.add(client)
        await db.flush()
        return client
    # ⚠ `name_set_at` — «имя трогал человек»: импорт истории не должен возвращать имя,
    # которое диспетчер стёр как неверное (см. `clients.set_name`).
    if name and not client.name and client.name_set_at is None:
        client.name = name
    # Аватар — только в пустоту: руками его никто не ставит, а перезапись
    # непустого устроила бы мигание картинки при каждом проходе импорта.
    if avatar_url and not client.avatar_url:
        client.avatar_url = avatar_url
    return client


async def _get_or_create_conversation(
    db: AsyncSession, account: AvitoAccount, client: Client, chat: ChatInfo
) -> tuple[Conversation, bool]:
    stmt = select(Conversation).where(
        Conversation.channel == "avito",
        Conversation.external_chat_id == chat.external_chat_id,
    )
    conv = (await db.execute(stmt)).scalar_one_or_none()
    created = conv is None
    if conv is None:
        conv = Conversation(
            channel="avito",
            external_chat_id=chat.external_chat_id,
            account_id=account.id,
            client_id=client.id,
            status="closed",  # решение №3: история — в архив, не в очередь «Новые»
            # Импортированный диалог вошёл в «Закрыт» в момент импорта, а не
            # тогда, когда шла переписка. Поле означает «когда вошёл в НЫНЕШНИЙ
            # статус», и импорт — это ровно тот момент; выдумывать сюда дату
            # последнего сообщения значило бы соврать точной цифрой.
            status_since=datetime.now(UTC),
            item_title=chat.item_title,
            item_url=chat.item_url,
            item_price=chat.item_price,
        )
        db.add(conv)
        await db.flush()
    return conv, created


async def _insert_history_message(
    db: AsyncSession, conv: Conversation, account: AvitoAccount, event: InboundEvent
) -> bool:
    """Идемпотентная вставка сообщения истории. Эхо-исходящие сохраняются
    как direction='out', sender_type='operator', sender_user_id=NULL —
    отправлены до LeadChat, автора не знаем (08 §4.2). Служебные записи Авито
    («[Системное сообщение] …») — той же парой, что кладёт живой путь
    (`inbound.AVITO_SYSTEM_DIRECTION/SENDER`, пакет 6.0а I-10): до этого дверь
    писала их как `in/client`, и «Ассистент Авито ответил…» из истории
    считался словами клиента у стенда адресов и `client_described`."""
    exists = await db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conv.id,
            Message.external_message_id == event.external_message_id,
        )
        .limit(1)
    )
    if exists is not None:
        return False
    own = event.author_id == account.avito_user_id
    # Только по приставке: `is_system` у истории носят и геоточка с адресом, и
    # ссылка клиента (`adapter._SYSTEM_SOURCE_TYPES`) — им место в переписке.
    служебное = avito_system_prefixed(event.text)
    # ON CONFLICT, а не только проверка выше: между ней и вставкой то же
    # сообщение успевал записать живой приём, и уникальный ключ ронял всю
    # историю чата с откатом (8 раз в журнале воркера с 10.09).
    result = await db.execute(
        dialect.insert(db)(Message)
        .values(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id=event.external_message_id,
            direction=AVITO_SYSTEM_DIRECTION if служебное else ("out" if own else "in"),
            sender_type=AVITO_SYSTEM_SENDER if служебное else ("operator" if own else "client"),
            sender_user_id=None,
            body=event.text,
            attachments=event.attachments,
            delivery_status="delivered",
            created_at=event.created_at,
        )
        .on_conflict_do_nothing(
            index_elements=["conversation_id", "external_message_id", "created_at"],
            index_where=sa.text("external_message_id IS NOT NULL"),
        )
    )
    return getattr(result, "rowcount", 0) == 1


async def _reopen_conversation(
    db: AsyncSession, conv: Conversation, client: Client, *, now: datetime
) -> None:
    """closed -> new штатной процедурой реопена: audit-пара 08 §2.5.

    ЗДЕСЬ ЖИЛ SCEN-21, И ЭТО БЫЛ РАЗРЫВ МЕЖДУ КОДОМ И ЕГО СОБСТВЕННЫМ
    КОММЕНТАРИЕМ. Рядом было написано «поднять из архива в очередь», статус
    поднимался в ``new`` — а ``offered_at`` не ставился. Очередь же собрана
    условием ``offered_at IS NOT NULL`` (``inbox.queue_condition``), и диалог
    оставался в невидимом промежутке: во «Входящие» не попадал, звука не
    давал, но в снимке «ждут ответа сейчас» считался наравне с настоящими.
    Худший из трёх возможных исходов: работы не даёт, а цифру портит.

    Теперь исходов два, и оба честные: либо диалог ПО-НАСТОЯЩЕМУ в очереди —
    ``enter_queue`` ставит ``offered_at`` и снимает прошлые отказы, — либо он
    остаётся в архиве. Промежутка нет.

    ``now`` — время последнего сообщения чата, а не «сейчас». Очередь
    сортируется по ожиданию, и импортированный вчерашний диалог обязан встать
    среди вчерашних, а не притвориться самым свежим.
    """
    # Импорт локальный: ``inbox`` тянет за собой уведомления и распределение,
    # а те — обратно в сервисы аккаунта. На уровне модуля это круг импортов
    # (тем же способом здесь подключён центр уведомлений).
    from app.services import conversation_status as status_dict
    from app.services import inbox

    status_dict.clear_snooze(conv)
    status_dict.set_status(conv, "new", now=now)
    conv.assignee_id = None
    inbox.enter_queue(conv, now=now)
    await write_audit(
        db,
        user_id=None,
        action="conversation.status_changed",
        entity="conversation",
        entity_id=str(conv.id),
        details={"from": "closed", "to": "new", "by": "system", "assignee_id": None},
    )
    await write_audit(
        db,
        user_id=None,
        action="conversation.reopened",
        entity="conversation",
        entity_id=str(conv.id),
        details={"client_id": str(client.id)},
    )


async def _fetch_chat_messages(
    client: AvitoClient,
    account: AvitoAccount,
    db: AsyncSession,
    redis: Redis,
    limiter: AvitoRateLimiter,
    chat_id: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await _avito_call(
            client.get_chat_messages,
            account,
            db,
            redis,
            limiter,
            account.avito_user_id,
            chat_id,
            offset=offset,
            limit=MESSAGES_PAGE,
        )
        if not page:
            break
        messages.extend(page)
        if len(page) < MESSAGES_PAGE:
            break
        offset += len(page)
    return messages


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Итог одного чата: обработан ли он и досталась ли работа операторам."""

    loaded: bool
    queued: bool = False
    messages: int = 0


CHAT_SKIPPED = ChatResult(loaded=False)


async def _backfill_chat(
    db: AsyncSession,
    redis: Redis,
    client: AvitoClient,
    limiter: AvitoRateLimiter,
    account: AvitoAccount,
    raw_chat: dict[str, Any],
    coverage: PartitionCoverage,
    plan: BackfillPlan,
) -> ChatResult:
    """Один чат: клиент + диалог (closed) + сообщения идемпотентно.

    ``plan`` задаёт обе границы прогона — см. :class:`BackfillPlan`.
    """
    try:
        chat = AvitoAdapter.parse_chat(raw_chat, account_user_id=account.avito_user_id)
    except WebhookParseError as exc:
        log.warning("backfill.chat_skipped", account_id=str(account.id), error=str(exc))
        return CHAT_SKIPPED

    # Чат целиком старше нижней границы — не тратим на него запросы истории.
    # Отсев только по ЯВНО известной отметке: нет её — качаем и разбираемся по
    # сообщениям. Пропустить чат из-за отсутствующего поля значило бы потерять
    # переписку по причине, к делу не относящейся.
    if (
        plan.floor is not None
        and chat.last_message_at is not None
        and chat.last_message_at < plan.floor
    ):
        return CHAT_SKIPPED

    raw_messages = await _fetch_chat_messages(
        client, account, db, redis, limiter, chat.external_chat_id
    )

    events: list[InboundEvent] = []
    for raw_msg in raw_messages:
        try:
            events.append(
                AvitoAdapter.normalize_history_message(
                    raw_msg, chat=chat, account_user_id=account.avito_user_id
                )
            )
        except WebhookParseError as exc:
            log.warning(
                "backfill.message_skipped",
                account_id=str(account.id),
                chat_id=chat.external_chat_id,
                error=str(exc),
            )

    # Нижняя граница — ПО ФАКТУ ОТМЕТКИ КАЖДОГО СООБЩЕНИЯ, а не по порядку
    # страниц. Порядок выдачи сообщений Авито нигде не обещан, и «дочитали до
    # старого — остановились» было бы догадкой о чужом API. В этом проекте
    # угаданный контракт дважды давал зелёные тесты и 4xx на боевом.
    if plan.floor is not None:
        events = [event for event in events if event.created_at >= plan.floor]
    # ⚠ ВЕРХНЯЯ ГРАНИЦА — ПРОТИВ ГОНКИ С ВЕБХУКОМ (проверка 24.09). Догрузка
    # чата забирала и сообщение, которое клиент написал через секунду после
    # первого, и клала его исторической дверью: без непрочитанного, без
    # ожидания ответа, без кадра и без бота. Живой вебхук того же сообщения
    # получал «дубль» и пропускал всё живое — вопрос клиента выглядел
    # отвеченным. Всё, что новее первого живого сообщения, — работа вебхука.
    if plan.ceiling is not None:
        events = [event for event in events if event.created_at < plan.ceiling]

    if not events:
        # Пустой диапазон: чат целиком старше границы либо в нём нечего
        # разбирать. Заводить под это диалог без единого сообщения нельзя —
        # он встанет в списки пустой строкой без времени и без текста.
        log.info(
            "backfill.chat_empty",
            account_id=str(account.id),
            chat_id=chat.external_chat_id,
            depth=plan.depth,
        )
        return CHAT_SKIPPED

    # Участник-клиент: из карточки чата, иначе — первый «не наш» автор истории.
    client_external_id = chat.client_external_id
    client_name = chat.client_name
    if client_external_id is None:
        for event in events:
            if event.author_id is not None and event.author_id != account.avito_user_id:
                client_external_id = str(event.author_id)
                client_name = client_name or event.client_name
                break
    if client_external_id is None:
        log.warning(
            "backfill.chat_skipped",
            account_id=str(account.id),
            chat_id=chat.external_chat_id,
            error="не удалось определить клиента чата",
        )
        return CHAT_SKIPPED

    # ПАРТИЦИИ ОБЕСПЕЧИВАЮТСЯ ДО ЕДИНОЙ ЗАПИСИ В БАЗУ. Здесь два требования, и
    # порядок между ними не косметический.
    #
    # 1. ДО ВСТАВКИ, А НЕ ПОСЛЕ ОШИБКИ. Сообщения истории приходят годовой
    #    давности, а помесячные партиции `messages` живут вокруг сегодняшнего
    #    дня — первое же старое сообщение упиралось в «no partition found» и
    #    уносило весь прогон (#24).
    #
    # 2. ДО ОТКРЫТИЯ ЗАПИСЫВАЮЩЕЙ ТРАНЗАКЦИИ — иначе ВЗАИМНАЯ БЛОКИРОВКА, и
    #    это не теория: на настоящем PostgreSQL прогон вставал намертво, а
    #    поймал это интеграционный тест, потому что в SQLite партиций нет и
    #    юнит-тесты о ней знать не могут.
    #
    #    Здесь стояло: создать клиента и диалог (INSERT -> RowExclusiveLock на
    #    `conversations`, транзакция открыта), а потом позвать обеспечение
    #    партиций. Обеспечение ходит ОТДЕЛЬНЫМ соединением, и `CREATE TABLE
    #    ... PARTITION OF messages` берёт на новой партиции внешний ключ на
    #    `conversations`, а для этого — ShareRowExclusiveLock на неё же. Он
    #    конфликтует с RowExclusiveLock нашей же незакрытой вставки. DDL ждёт
    #    коммита, коммит ждёт DDL, и Постгрес такой клубок не разрубает: наша
    #    сессия не ждёт блокировку, она ждёт нас.
    #
    #    Хуже того, ожидающее соединение к этому моменту УЖЕ держит
    #    AccessExclusiveLock на `messages` — то есть висящая загрузка истории
    #    останавливает приём сообщений всей компании, пока кто-нибудь не убьёт
    #    воркер.
    #
    #    Коммит перед DDL закрывает читающую транзакцию, оставшуюся от похода
    #    в Авито (`avito_app.ensure_fresh`), и гарантирует, что в этот момент
    #    сессия не держит ничего. Гасить объекты он не может: фабрика сессий
    #    живёт с `expire_on_commit=False`.
    await db.commit()
    await coverage.ensure_all(event.created_at for event in events)

    client_row = await _get_or_create_client(
        db, client_external_id, client_name, chat.client_avatar_url
    )
    conv, conv_created = await _get_or_create_conversation(db, account, client_row, chat)

    added = 0
    real_client_words = 0  # настоящие входящие, не «[Системное сообщение]…»
    # ДОГОН КАРТОЧКИ (N29, 19.09). Историческая дверь вставляет реплики без разбора
    # адреса и телефона — живой путь через неё не идёт, и диалоги теряли адрес,
    # названный до первого дошедшего вебхука (корзина H_no_rows, 47/мес по
    # гипотезе разбора). Запоминаем время каждой ВСТАВЛЕННОЙ реплики клиента;
    # после commit'а ставим задачу `workers/cards_catchup` от самой ранней в окне
    # — она перечитает хвост диалога тем же путём, что живой приём. Только
    # вставленные: повторный заход по разобранному чату ничего не вставляет — и
    # ничего не ставит.
    вставленные_клиентские: list[datetime] = []
    last_event_at = events[0].created_at
    # ⚠ ЧЬЁ ПОСЛЕДНЕЕ СЛОВО — И ЕСТЬ ВОПРОС ОЧЕРЕДИ (19.08, разбор потока).
    # Свежесть считалась по ЛЮБОМУ последнему событию, включая наш собственный
    # ответ. Отсюда 129 диалогов в очереди, где мы уже ответили и ход давно за
    # клиентом: работа сделана, а строка висит и требует внимания. Очередь — это
    # «клиент написал и ждёт», и решает это направление последнего сообщения.
    последнее_слово_клиента = False
    for event in events:
        вставлено = await _insert_history_message(db, conv, account, event)
        if вставлено:
            added += 1
        свой = bool(event.author_id) and event.author_id == account.avito_user_id
        служебное = avito_system_prefixed(event.text)
        if event.author_id and not свой and not служебное:
            real_client_words += 1
            if вставлено:
                вставленные_клиентские.append(cast(datetime, ensure_aware(event.created_at)))
        if event.created_at >= last_event_at:
            last_event_at = event.created_at
            # Служебные записи ход не передают: «клиент посмотрел номер» — не
            # обращение, отвечать на него нечего.
            if not служебное:
                последнее_слово_клиента = bool(event.author_id) and not свой
        last = ensure_aware(conv.last_message_at)
        if last is None or event.created_at > last:
            conv.last_message_at = event.created_at

    # ЧТО ИМПОРТ ДЕЛАЕТ С НЕПРОЧИТАННЫМ — РЕШЕНИЕ, А НЕ УМОЛЧАНИЕ.
    #
    # До 11 августа здесь было написано «поднять из архива в очередь», а
    # делалось другое (SCEN-21): статус поднимался, `offered_at` — нет, и
    # диалог не попадал ни в очередь, ни в архив. Теперь, когда грузится ВСЯ
    # история, цена этого промаха выросла: девять аккаунтов по 300+
    # объявлений — это годы переписки, и половина старых чатов на стороне
    # Авито помечена непрочитанной просто потому, что их никто не открывал в
    # вебе.
    #
    # Правило: непрочитанное СВЕЖЕЕ (см. QUEUE_FRESH_WINDOW) — это работа,
    # диалог идёт в очередь по-настоящему. Непрочитанное СТАРОЕ — это архив с
    # честным бейджем: переписка на месте, поиск её находит, карточка клиента
    # её показывает, но тринадцать операторов сегодня разгребают сегодняшнее.
    queued = False
    queue_frame: dict[str, Any] | None = None
    # ⚠ только для диалогов, СОЗДАННЫХ этим прогоном: на живом канале (кнопка
    # 17.08) существующий диалог уже отработан людьми — Авито держит has_unread
    # почти на всём (LeadChat mark-read туда не шлёт), и без этого условия
    # прогон реопенил закрытые операторами диалоги и перетирал их unread
    # (аудит свежего слоя, находка №9)
    # пустышки («создал чат и молчит», одни системки) в очередь не идут:
    # «Сообщений пока нет» с кнопкой «Принять» — мусор для операторов (17.08)
    if chat.has_unread and conv_created and real_client_words > 0 and последнее_слово_клиента:
        conv.unread_count = chat.unread_count or 1  # бейдж: клиента никто не читал
        if conv.status == "closed" and last_event_at >= plan.queue_floor:
            await _reopen_conversation(db, conv, client_row, now=last_event_at)
            queued = True
            # Кадр собирается ДО commit'а (связанные сущности ещё видны в
            # транзакции), публикуется — строго после (08 §8.1). Без него
            # диалог лежал бы в очереди молча и появился бы у операторов
            # только после перезагрузки страницы: ровно та половинчатость,
            # из-за которой SCEN-21 и остался незамеченным.
            from app.services import inbox

            queue_frame = await inbox.inbox_frame_addressed(db, conv, now=last_event_at)

    await db.commit()  # чат — атомарная единица прогресса: упали — продолжаем с него же

    if queue_frame is not None:
        from app.ws.hub import publish_inbox_new

        # Список допущенных обязателен: без него кадр очереди уезжает всем
        # операторам, включая тех, кому канал не назначен (разбор — в
        # `inbound._inbox_frame_for` и `inbox.inbox_frame_addressed`).
        await publish_inbox_new(
            redis,
            queue_frame["conversation"],
            eligible_operator_ids=queue_frame.get("eligible"),
        )

    догнать_с = cards_catchup.earliest_in_window(вставленные_клиентские, now=utcnow())
    if догнать_с is not None:
        # После commit'а: задача читает уже видимые строки ленты.
        await cards_catchup.enqueue_cards_catchup(redis, conv.id, since=догнать_с)

    return ChatResult(loaded=True, queued=queued, messages=added)


async def _census_chats(
    db: AsyncSession,
    redis: Redis,
    client: AvitoClient,
    limiter: AvitoRateLimiter,
    account: AvitoAccount,
    *,
    progress: BackfillProgress,
) -> int | None:
    """Перепись: сколько всего чатов у аккаунта. ``None`` — сосчитать не вышло.

    ПОЧЕМУ СЧИТАЕМ САМИ, А НЕ СПРАШИВАЕМ У АВИТО. Ответ списка чатов кладётся
    в ``AvitoClient.get_chats`` и разбирается там же: наружу отдаётся только
    массив ``chats``. Есть ли в ответе общее число — по документации Авито
    неизвестно, а придумывать поле нельзя: в этом проекте угаданный формат
    дважды дал зелёные тесты и 4xx на боевом. Свой счёт по страницам верен при
    любом ответе; если у Авито найдётся честный ``total``, отсюда уйдёт весь
    цикл, и это будет правкой одной функции.

    Ничего не запоминаем, кроме числа: страницы чатов при девяти аккаунтах —
    это десятки тысяч словарей, и держать их в памяти воркера незачем.

    Остановку слушаем и здесь: перепись сама по себе может идти минуты, и
    кнопка «Остановить» обязана работать всё это время.
    """
    counted = 0
    offset = 0
    while True:
        if await progress.stop_requested():
            log.info("backfill.census_stopped", account_id=str(account.id), counted=counted)
            return None
        try:
            chats = await _chats_page(
                client, account, db, redis, limiter, offset=offset, where="census"
            )
        except AvitoApiError as exc:
            # Тот же потолок площадки, что и в самом прогоне: перепись доходит
            # до дальней страницы и получает 400. Считать это поломкой нельзя —
            # именно здесь прогон падал ПЕРВЫМ, ещё до единого загруженного
            # диалога, и владелец видел «сорвалась на 0 из 1100».
            if getattr(exc, "status", None) == 400 and offset > 0:
                log.warning(
                    "avito.chats_page_refused",
                    account_id=str(account.id),
                    taken=offset,
                    where="census",
                    error=str(exc),
                )
                break
            raise
        if chats is None:
            # Сеть не дала страницу даже с повторами. Перепись — не работа, а
            # знаменатель строки «загружено N из M»: оборвать её не страшно,
            # число просто будет по тому, что успели сосчитать. Ронять из-за
            # неё прогон, который ещё не загрузил ни одного диалога, нельзя.
            log.warning("backfill.census_incomplete", account_id=str(account.id), counted=counted)
            break
        if not chats:
            break
        counted += len(chats)
        offset += len(chats)
        progress.total = counted
        await progress.flush()
    log.info("backfill.census", account_id=str(account.id), chats=counted)
    return counted


@with_job_scope
async def backfill_conversation(
    ctx: dict[str, Any],
    account_id: uuid.UUID,
    external_chat_id: str,
    live_since: str | None = None,
) -> None:
    """ИСТОРИЯ ОДНОГО ЧАТА — КАК ТОЛЬКО ОН ПОЯВИЛСЯ ВО «ВХОДЯЩИХ» (28.08).

    Просьба владельца дословно: «чтобы если чат приходит во входящие, он
    автоматом подтягивал историю сообщений».

    ⚠ ПОЧЕМУ ЭТОГО НЕ ДЕЛАЛОСЬ РАНЬШЕ И ПОЧЕМУ СВЕРКА НЕ ПОМОГАЛА. Сверка
    (`workers/reconciliation.py`) берёт границу истории по ПОСЛЕДНЕМУ ВХОДЯЩЕМУ
    диалога: `since = max(created_at) where direction = 'in'`. У диалога,
    только что созданного вебхуком, это ровно то самое сообщение, которым он и
    создан. Дальше в сверке стоит быстрый отсев «последнее сообщение чата уже у
    нас — историю не качаем», и он срабатывает на таком диалоге ВСЕГДА.

    То есть переписку, которая была в этом чате ДО первого дошедшего до нас
    вебхука, не подтягивал никто и никогда. Оператор открывал диалог и видел
    одну строку без всякого «что было раньше»: ни прежней цены, ни адреса, ни
    того, что клиенту уже отказали в прошлом месяце.

    ЧТО ДЕЛАЕТ ЭТА ЗАДАЧА. Спрашивает у Авито карточку чата и его сообщения и
    заводит недостающие ИСТОРИЧЕСКОЙ дверью — `_backfill_chat`, тот же код, что
    и у массовой загрузки. Живого диалога это не трогает: очередь, статус и
    бейдж непрочитанного меняются в `_backfill_chat` только у диалогов,
    СОЗДАННЫХ прогоном, а наш уже существует.

    ОДИН ЧАТ, А НЕ КАНАЛ. Дешёвая задача (два запроса), ставится на создание
    диалога, дедуплицируется по идентификатору диалога. Массовую догрузку
    канала ведёт сторож (`scheduler.enqueue_backfill_supervise`) — здесь мы
    закрываем не её, а дыру в конкретном диалоге, который человек открывает
    прямо сейчас.

    МОЛЧА. Ни уведомлений, ни событий в сокет: для человека это не событие, а
    та самая лента, которую он и ожидал увидеть, открывая диалог.
    """
    session_factory = ctx.get("db_session_factory") or db_mod.get_session_factory()
    redis: Redis = ctx.get("redis") or redis_mod.get_client()

    async with session_factory() as db:
        account = await db.get(AvitoAccount, account_id)
        if account is None or account.status != "active":
            return  # disabled/needs_reauth не опрашиваем (01 §4.5)

        client = AvitoClient()
        limiter = AvitoRateLimiter(redis)
        # Глубина ВСЕГДА полная, независимо от настройки канала. Настройка
        # `history_depth` — про массовую загрузку, где выбор «всё или с момента
        # подключения» экономит часы. Здесь речь об одном диалоге, который
        # оператор открывает сию секунду, и обрезать ему контекст ради
        # экономии двух запросов нечестно.
        plan = make_plan(account, HISTORY_ALL)
        # `live_since` — отметка сообщения, которым приём создал диалог: всё с
        # неё и новее несут вебхук и сверка (см. `BackfillPlan.ceiling`).
        if live_since:
            plan = replace(plan, ceiling=datetime.fromisoformat(live_since))
        coverage = PartitionCoverage()
        try:
            raw_chat = await _avito_call(
                client.get_chat,
                account,
                db,
                redis,
                limiter,
                account.avito_user_id,
                str(external_chat_id),
            )
        except Exception:
            # Ловим широко: отказ Авито, обрыв, неизвестный вид ответа. Диалог
            # уже виден оператору с тем сообщением, ради которого он и создан, —
            # неудачная догрузка ухудшает контекст, но не ломает работу. Молча
            # проглотить, однако, нельзя: без записи в журнале «почему у этого
            # диалога нет истории» ответа не будет ни у кого.
            log.warning(
                "convhist.chat_unavailable",
                account_id=str(account_id),
                external_chat_id=str(external_chat_id),
                exc_info=True,
            )
            return
        if not isinstance(raw_chat, dict) or not raw_chat:
            log.warning(
                "convhist.chat_empty",
                account_id=str(account_id),
                external_chat_id=str(external_chat_id),
            )
            return
        try:
            result = await _backfill_chat(
                db, redis, client, limiter, account, raw_chat, coverage, plan
            )
        except Exception:
            await db.rollback()
            log.exception(
                "convhist.failed",
                account_id=str(account_id),
                external_chat_id=str(external_chat_id),
            )
            return
        log.info(
            "convhist.done",
            account_id=str(account_id),
            external_chat_id=str(external_chat_id),
            messages=result.messages,
        )


async def enqueue_conversation_history(
    account_id: uuid.UUID,
    conversation_id: uuid.UUID,
    external_chat_id: str,
    *,
    live_since: datetime | None = None,
) -> None:
    """Поставить догрузку истории одного чата. Молча, если очереди нет.

    ⚠ ДЕДУП ПО ДИАЛОГУ, А НЕ ПО МОМЕНТУ. Диалог создаётся ровно один раз, но
    вебхук Авито доставляется «хотя бы один раз»: повтор придёт в тот же чат.
    Постоянный `_job_id` делает повтор бесплатным.

    ⚠ ОШИБКА ЗДЕСЬ НЕ ДОЛЖНА УРОНИТЬ ПРИЁМ СООБЩЕНИЯ. Эта функция зовётся с
    горячего пути вебхука: недоступный Redis очереди — повод не подтянуть
    историю, а не повод потерять сообщение клиента.
    """
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
    except ImportError:
        return
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:
        log.warning("convhist.enqueue_failed", conversation_id=str(conversation_id), exc_info=True)
        return
    try:
        await pool.enqueue_job(
            "backfill_conversation",
            account_id,
            str(external_chat_id),
            live_since.isoformat() if live_since else None,
            _job_id=f"convhist:{conversation_id}",
        )
    except Exception:
        log.warning("convhist.enqueue_failed", conversation_id=str(conversation_id), exc_info=True)
    finally:
        await pool.aclose()


@with_job_scope
async def backfill_account(
    ctx: dict[str, Any], account_id: uuid.UUID, depth: str = DEFAULT_HISTORY_DEPTH
) -> None:
    """ARQ-задача: выкачивает существующие чаты аккаунта (DESIGN §1.2).

    Резюмируема: ход работы — в Redis ``backfill:{account_id}``, множество уже
    обработанных чатов — в ``backfill:seen:{account_id}``; после падения
    воркера, после остановки человеком и после полного повторного запуска
    прогон продолжает с необработанных чатов и дублей не создаёт. Боты не
    запускаются, по-сообщенческие WS-события не публикуются — по завершении
    один notify админам (08 §4.2).

    ``depth`` — глубина истории (:data:`HISTORY_ALL` по умолчанию, требование
    владельца от 11 августа). Аргумент со значением по умолчанию, а не
    обязательный: в очереди ARQ могут лежать задачи, поставленные ДО выкатки,
    с одним аргументом, и падать на них нельзя.

    ПОЧЕМУ СНАЧАЛА ПЕРЕПИСЬ. Первый проход только считает чаты — по странице
    на сотню, — и лишь потом начинается загрузка. Один лишний обход списка
    против единственного вопроса, который задают про эту загрузку: «сколько
    осталось». Без числа M строка «загружено 300 чатов» не отличает начало от
    конца, а на девяти аккаунтах по 300+ объявлений разница между ними —
    часы.
    """
    session_factory = ctx.get("db_session_factory") or db_mod.get_session_factory()
    redis: Redis = ctx.get("redis") or redis_mod.get_client()

    async with session_factory() as db:
        account = await db.get(AvitoAccount, account_id)
        if account is None or account.status != "active":
            return  # disabled/needs_reauth не качаем (01 §4.5)

        client = AvitoClient()
        limiter = AvitoRateLimiter(redis)
        progress_key = _progress_key(account_id)
        seen_key = _seen_key(account_id)
        failed_key = _failed_key(account_id)
        # Новый заход снимает прежнюю пометку о срыве и прежнюю просьбу
        # остановиться: обе про прошлую попытку.
        await redis.delete(failed_key)
        await redis.delete(_stop_key(account_id))
        # ТОЧКА ВОЗОБНОВЛЕНИЯ — МНОЖЕСТВО ОБРАБОТАННЫХ ЧАТОВ, А НЕ СМЕЩЕНИЕ.
        # Смещение врёт: список чатов Авито отсортирован по свежести, и любое
        # новое сообщение во время прогона сдвигает страницы — продолжение «с
        # трёхсотого» перепрыгивало бы через чаты, которые уехали вниз. Чаты
        # же, которые мы уже разобрали, названы поимённо и не зависят от
        # порядка выдачи.
        seen: set[str] = {
            item.decode() if isinstance(item, bytes) else str(item)
            for item in await aw(redis.smembers(seen_key))
        }
        plan = make_plan(account, depth)
        # Счётчики продолжения берутся из ПРЕЖНЕГО хода работы, а не считаются
        # заново по множеству разобранных чатов: в множестве лежат и
        # пропущенные (пустой чат, чат старее выбранной глубины), и назвать их
        # загруженными диалогами значило бы соврать в отчёте после каждой
        # остановки.
        previous = _parse_progress(await redis.get(progress_key)) or {}
        progress = BackfillProgress(
            redis,
            account_id,
            depth=plan.depth,
            processed=len(seen),
            loaded=int(previous.get("loaded") or 0),
            queued=int(previous.get("queued") or 0),
        )
        # ЗАЧЕМ ЭТОТ try. Всё, что ниже, ходит в Авито и в базу; любое
        # неожиданное исключение раньше уносило задачу целиком, и ключ
        # прогресса ОСТАВАЛСЯ. А карточка канала читает его как «идёт
        # загрузка» — и семь суток, до истечения ключа, показывала «Загружаем
        # историю…» у загрузки, которой давно нет. Человек ждал, вместо того
        # чтобы нажать «Повторить».
        try:
            coverage = PartitionCoverage()
            # Название забираем СЕЙЧАС, а не в конце. Откат сессии после сбойного
            # чата гасит загруженный объект, и обращение к `account.title` полезло
            # бы перечитывать его из базы посреди сборки отчёта.
            account_title = account.title
            # «Дальше идти незачем»: канал отключили прямо во время прогона,
            # сессия окончательно сломалась или человек нажал «Остановить».
            # Флаг, а не break, потому что выходить надо из ДВУХ циклов — по
            # чатам и по страницам.
            stop = False
            stopped_by_human = False
            # Заход кончился по бюджету времени — не сбой и не конец работы:
            # ход работы остаётся, продолжение ставится в очередь ниже.
            slice_exhausted = False
            slice_started = _elapsed_clock()

            def slice_over() -> bool:
                return _elapsed_clock() - slice_started >= BACKFILL_SLICE_SECONDS

            await progress.flush()
            # ПЕРЕПИСЬ — ОДИН РАЗ НА ВСЮ ЗАГРУЗКУ, А НЕ НА КАЖДЫЙ ЗАХОД.
            #
            # Она обходит весь список чатов страницами по сотне: на канале с
            # тысячей чатов это одиннадцать запросов к Авито. Гонять их заново
            # в начале каждого захода значило бы отдать переписи заметную часть
            # бюджета — ровно того бюджета, ради которого заход и нарезан.
            # Продолжение берёт число из прежнего хода работы: оно про тот же
            # канал и ту же глубину.
            _прежний_total = previous.get("total")
            if isinstance(_прежний_total, int) and _прежний_total > 0:
                progress.total = _прежний_total
            else:
                progress.total = await _census_chats(
                    db, redis, client, limiter, account, progress=progress
                )
            # Остановили на переписи — дальше не идём. Без этой проверки
            # прогон вошёл бы в загрузку, тут же вышел по тому же флагу и
            # отчитался «загружено 0 диалогов», стерев ход работы: остановка
            # выглядела бы как успешно законченная пустая загрузка.
            if await progress.stop_requested():
                stop = stopped_by_human = True
            progress.phase = "loading"
            await progress.flush()

            offset = 0
            обрыв_площадки = False
            while not stop:
                try:
                    chats = await _chats_page(
                        client, account, db, redis, limiter, offset=offset, where="loading"
                    )
                except AvitoApiError as exc:
                    # ⚠ 400 НА ДАЛЬНЕЙ СТРАНИЦЕ — ПОТОЛОК ПЛОЩАДКИ, А НЕ ПОЛОМКА
                    # (боевой случай 19.08). У владельца восемь каналов по тысяче
                    # с лишним чатов, и КАЖДЫЙ прогон падал на дальней странице:
                    # «Загрузка истории сорвалась» по всем каналам подряд, хотя
                    # тысяча диалогов уже лежала в базе. Хоронить прогон целиком
                    # из-за того, что площадка не пускает глубже, — значит
                    # выбрасывать всю проделанную работу и пугать человека.
                    #
                    # На ПЕРВОЙ странице 400 остаётся настоящей бедой: там дело не
                    # в глубине, а в самом запросе или в правах, и молчать нельзя.
                    if getattr(exc, "status", None) == 400 and offset > 0:
                        log.warning(
                            "avito.chats_page_refused",
                            account_id=str(account.id),
                            taken=offset,
                            error=str(exc),
                        )
                        обрыв_площадки = True
                        break
                    raise
                if chats is None:
                    # ⚠ СЕТЬ НЕ ДАЛА СТРАНИЦУ — ЭТО КОНЕЦ ЗАХОДА, А НЕ ПРОГОНА
                    # (боевой замер 28.08).
                    #
                    # За сорок минут загрузки Авито не ответил 58 раз: 57 отказов
                    # пришлись на историю отдельных чатов — там они изолированы и
                    # стоят одного чата из тысячи, — а ОДИН на страницу списка. И
                    # этот один уносил весь прогон канала: исключение выходило
                    # наружу, общий `except` ставил «сорвалась», и канал стоял до
                    # следующего тика сторожа. Примерно раз в сорок минут при
                    # полной загрузке — ровно то, на что жалуется владелец.
                    #
                    # Теперь это ровно то же, что исчерпанный бюджет времени:
                    # загруженное остаётся, ход работы сохраняется, продолжение
                    # ставится само и пойдёт с необработанных чатов.
                    slice_exhausted = True
                    break
                if not chats:
                    break
                for raw_chat in chats:
                    chat_id = str(raw_chat.get("id") or "")
                    if chat_id and chat_id in seen:
                        continue  # разобран в прошлом заходе — второй раз незачем
                    # ОСТАНОВКА — НА ГРАНИЦЕ ЧАТА. Проверяем перед чатом, а не
                    # посреди: чат — атомарная единица прогресса, и оборвать
                    # его на середине значило бы оставить полудиалог.
                    if await progress.stop_requested():
                        stop = stopped_by_human = True
                        break
                    # ⚠ БЮДЖЕТ ЗАХОДА — ТАМ ЖЕ, ГДЕ ОСТАНОВКА ЧЕЛОВЕКОМ, И ПО
                    # ТОЙ ЖЕ ПРИЧИНЕ: чат — атомарная единица прогресса. Оборви
                    # мы его посреди, остался бы полудиалог, а точка
                    # возобновления считала бы его разобранным.
                    if slice_over():
                        stop = slice_exhausted = True
                        break
                    # ОДИН ПЛОХОЙ ЧАТ НЕ УНОСИТ ОСТАЛЬНЫЕ. Раньше любая ошибка на
                    # любом из тысяч чатов роняла задачу целиком: загружено было,
                    # скажем, 900 диалогов из 1000, но пользователь видел только
                    # вечное «идёт загрузка», а девятьсот успешных чатов при
                    # повторном запуске выкачивались заново.
                    #
                    # Ловим широко и намеренно: причина у сорвавшегося чата бывает
                    # любая — битая карточка, вложение неизвестного вида, отказ
                    # Авито на середине. Ни одна из них не является поводом бросить
                    # оставшиеся девятьсот девяносто девять.
                    try:
                        result = await _backfill_chat(
                            db, redis, client, limiter, account, raw_chat, coverage, plan
                        )
                        # Пропущенный чат тоже помечаем разобранным: причина
                        # пропуска (старее границы, пустой, без клиента) при
                        # повторном заходе будет ровно той же, а вот запросы
                        # его истории повторятся.
                        if chat_id:
                            seen.add(chat_id)
                            await aw(redis.sadd(seen_key, chat_id))
                            await redis.expire(seen_key, BACKFILL_PROGRESS_TTL)
                        progress.processed += 1
                        if result.loaded:
                            progress.loaded += 1
                            if result.queued:
                                progress.queued += 1
                        await progress.flush()
                    except Exception:
                        progress.failed_chats += 1
                        await progress.flush()
                        log.exception(
                            "backfill.chat_failed",
                            account_id=str(account_id),
                            chat_id=chat_id or "?",
                        )
                        # Сессия после сбоя непригодна: Postgres отвергнет всё
                        # следующее в этой транзакции.
                        await db.rollback()
                        # Откат гасит ЗАГРУЖЕННЫЕ объекты, и следующий виток цикла
                        # полез бы перечитывать аккаунт посреди обращения к Авито —
                        # то есть падал бы уже по второму разу и по другой причине.
                        # Перечитываем сами и здесь же узнаём, не отключили ли
                        # канал прямо во время прогона.
                        try:
                            await db.refresh(account)
                        except Exception:
                            log.exception("backfill.account_gone", account_id=str(account_id))
                            stop = True
                            break
                        if account.status != "active":
                            log.info("backfill.stopped_inactive", account_id=str(account_id))
                            stop = True
                            break
                offset += len(chats)

            if slice_exhausted:
                # ⚠ ЗАХОД КОНЧИЛСЯ, РАБОТА — НЕТ. Ход работы и множество
                # разобранных чатов остаются нетронутыми: продолжение поднимет
                # их и пойдёт дальше ровно с того места.
                #
                # Молчим намеренно — ни notify, ни отчёта. Для человека это не
                # событие: загрузка идёт, на карточке канала растёт то же
                # число. Сообщать «заход №7 закончился» значит превратить
                # исправную работу в поток тревог.
                #
                # `dedupe=False` обязателен: ARQ держит ключ выполненной задачи
                # ещё час (`keep_result`), и постоянный идентификатор молча
                # отбросил бы продолжение — загрузка встала бы навсегда, ровно
                # так же, как вставала от таймаута, только тише.
                progress.phase = "loading"
                await progress.flush()
                await enqueue_backfill(account_id, plan.depth, dedupe=False)
                log.info(
                    "backfill.slice_done",
                    account_id=str(account_id),
                    loaded=progress.loaded,
                    processed=progress.processed,
                    total=progress.total,
                )
                return

            if stopped_by_human:
                # Останов — не сбой и не конец: ключи прогресса и разобранных
                # чатов остаются, продолжение поднимет их и пойдёт дальше.
                progress.phase = "stopped"
                await progress.flush()
                await redis.delete(_stop_key(account_id))
                log.info(
                    "backfill.stopped",
                    account_id=str(account_id),
                    loaded=progress.loaded,
                    total=progress.total,
                )
                await publish_event(
                    redis,
                    "notify",
                    {
                        "level": "info",
                        "title": "Загрузка истории остановлена",
                        "text": (
                            f"Аккаунт «{account_title}»: загружено {progress.loaded} "
                            f"из {progress.total if progress.total is not None else '?'} "
                            "диалогов. Продолжение пойдёт с необработанных."
                        ),
                        "audience_hint": "admin",
                    },
                    audience="admin",
                )
                return

            await redis.delete(progress_key)
            await redis.delete(seen_key)
            # ⚠ ОТМЕТКА «ЗАГРУЖЕНО» — В БАЗУ, А НЕ В REDIS (28.08).
            #
            # Ключи выше только что стёрты, а `backfill:seen` и без того живёт
            # неделю: по Redis «канал загружен» и «канал не загружался ни разу»
            # выглядят одинаково — ключей нет ни там, ни там. Сторожу догрузки
            # различать их обязательно, иначе он либо крутит законченную
            # загрузку по кругу, либо не трогает незапущенную.
            account.history_loaded_at = utcnow()
            account.history_loaded_depth = plan.depth
            await db.commit()
            await _announce_backfill_final(redis, account_id, "idle")
            log.info(
                "backfill.done",
                account_id=str(account_id),
                depth=plan.depth,
                loaded_convs=progress.loaded,
                failed_convs=progress.failed_chats,
                queued_convs=progress.queued,
                total_chats=progress.total,
                covered_months=coverage.covered_months,
            )
            # Отчёт называет и то, что НЕ загрузилось. Молчаливая половина хуже
            # честного «загружено 900, не удалось 100»: в первом случае человек
            # уверен, что вся переписка на месте, и узнаёт обратное от клиента.
            text = f"Аккаунт «{account_title}»: загружено {progress.loaded} диалогов"
            # Про очередь говорим ОТДЕЛЬНОЙ фразой: «загружено 900» и «из них 4
            # ждут ответа» — разные новости, и вторая требует действия сегодня.
            if progress.queued:
                text += f", из них {progress.queued} с непрочитанным — в «Входящих»"
            if progress.failed_chats:
                text += (
                    f", не удалось загрузить {progress.failed_chats}"
                    " — подробности в журнале сервера"
                )
            if обрыв_площадки:
                # Человек обязан знать, что список кончился НЕ У НАС. Иначе
                # «загружено 1000» читается как «вся переписка на месте», а
                # глубже просто не пустили — и старые чаты не появятся никогда,
                # сколько ни жми «Загрузить историю».
                text += (
                    ". Глубже Авито не отдаёт: площадка обрывает список примерно"
                    " на тысяче чатов, и старее этого загрузить нечем"
                )
            await publish_event(
                redis,
                "notify",
                {
                    "level": "warning" if progress.failed_chats else "info",
                    "title": (
                        "История загружена частично"
                        if progress.failed_chats
                        else "История загружена"
                    ),
                    "text": text,
                    "audience_hint": "admin",
                },
                audience="admin",
            )

        except Exception:
            # Пометка живёт столько же, сколько жил бы ключ прогресса: неделю.
            # Карточка канала покажет «Загрузка сорвалась» и кнопку повтора, а
            # не бесконечное «Загружаем историю…».
            #
            # Ключ ПРОГРЕССА при этом НЕ трогаем: он точка возобновления, и
            # повторный запуск продолжит с последней страницы, а не с нуля.
            await redis.set(failed_key, "1", ex=BACKFILL_FAILED_TTL)
            log.exception("backfill.crashed", account_id=str(account_id))
            await _announce_backfill_final(redis, account_id, "failed")
            await publish_event(
                redis,
                "notify",
                {
                    "level": "error",
                    "title": "Загрузка истории сорвалась",
                    "text": (
                        f"Аккаунт «{account_title}»: загрузка прервалась. "
                        "Уже загруженное на месте, повтор продолжит с того же места."
                    ),
                    "audience_hint": "admin",
                },
                audience="admin",
            )
            raise


def _parse_progress(raw: str | bytes | None) -> dict[str, Any] | None:
    """Разбор ключа хода работы. Понимает и старый формат — голое число.

    Прогон, начатый ДО выкатки нового формата, доживает свой век: в ключе у
    него лежит смещение страницы чатов. Уронить на нём разбор значило бы в
    день выкатки показать «Загрузка сорвалась» на канале, где всё в порядке.
    """
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        state = json.loads(text)
    except ValueError:
        state = None
    if isinstance(state, dict):
        return state
    try:  # старый формат: смещение числом
        return {"phase": "loading", "chats_offset": int(text), "loaded": int(text)}
    except ValueError:
        return {"phase": "loading"}


async def clear_backfill_state(redis: Redis, account_id: uuid.UUID) -> None:
    """Стереть следы прогона. ОБЯЗАТЕЛЬНО при «Отключить и стереть»: иначе
    backfill:seen переживает отключение (TTL 7 суток), и повторная загрузка
    молча пропускает уже стёртые из БД чаты — потеря переписки без единой
    ошибки (аудит свежего слоя 17.08, находка №7)."""
    for key in (
        _progress_key(account_id),
        _seen_key(account_id),
        _failed_key(account_id),
        _stop_key(account_id),
    ):
        try:
            await redis.delete(key)
        except Exception:  # noqa: BLE001
            pass


async def mark_backfill_queued(redis: Redis, account_id: uuid.UUID) -> None:
    """Фаза «queued» ДО enqueue: кнопка гаснет сразу, двойной клик отсечён
    409-ом, а окно «idle до первого flush воркера» закрыто (находки №8/№10)."""
    await redis.set(
        _progress_key(account_id),
        json.dumps({"phase": "queued", "total": None, "loaded": 0}, ensure_ascii=False),
        ex=BACKFILL_PROGRESS_TTL,
    )


async def _announce_backfill_final(redis: Redis, account_id: uuid.UUID, phase: str) -> None:
    """Финальный кадр загрузки: без него карточка застревала на «Загружаем…»
    до F5 — завершение удаляет ключ прогресса, и очередного flush не бывает
    (аудит свежего слоя 17.08). Фронт финальные кадры не дросселирует."""
    try:
        from app.ws.events import publish_event

        await publish_event(
            redis, "account:backfill", {"account_id": str(account_id), "phase": phase}
        )
    except Exception:  # noqa: BLE001
        pass


#: Сколько молчания состояния считать смертью прогона. Живая загрузка
#: обновляет отметку каждые несколько секунд (дроссель flush — 3 с), так что
#: пять минут — это заведомо мёртвая задача, а не медленный чат.
BACKFILL_STALE_AFTER = timedelta(minutes=5)


def _as_dt(value: Any) -> datetime | None:
    """Отметка времени из состояния. Мусор — не повод падать при показе."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def get_backfill_state(redis: Redis, account_id: uuid.UUID) -> dict[str, Any]:
    """Ход загрузки истории для UI (01 §4.1 + 11-SCREENS §4.1).

    ЧЕТЫРЕ СОСТОЯНИЯ. Их было два — «нет ключа» и «есть ключ», — и второе
    называлось «идёт загрузка». Но ключ остаётся и когда задача упала:
    карточка семь суток показывала «Загружаем историю…» у загрузки, которой
    давно нет, и человек ждал вместо того, чтобы нажать «Повторить». Третьим
    стал `failed`, четвёртым — `stopped`: остановку человек делает сам, и
    отвечать ему «идёт загрузка» после этого нельзя.

    Числа отдаём при любом исходе: по ним видно, сколько успели, и что
    продолжение пойдёт дальше, а не начнёт заново.
    """
    state = _parse_progress(await redis.get(_progress_key(account_id)))
    failed = await redis.get(_failed_key(account_id))
    if state is None:
        return {"status": "idle", "chats_offset": None}

    loaded = state.get("loaded")
    total = state.get("total")
    # Перепись делается один раз в начале, а чаты во время загрузки
    # прибывают: за час прогона их станет больше, чем насчитали. «Загружено
    # 1002 из 1000» — цифра, после которой не верят и остальным, поэтому
    # потолок поднимаем до факта, а не показываем невозможное.
    if isinstance(total, int) and isinstance(loaded, int) and loaded > total:
        total = loaded
    out = {
        "chats_offset": state.get("chats_offset"),
        "loaded": loaded,
        "total": total,
        "failed_chats": state.get("failed_chats"),
        "queued": state.get("queued"),
        "depth": state.get("depth"),
        "phase": state.get("phase"),
    }
    if failed:
        return {"status": "failed", **out}
    if state.get("phase") == "stopped":
        return {"status": "stopped", **out}
    # ⚠ ПЯТОЕ СОСТОЯНИЕ: «ИДЁТ», КОТОРОЕ УЖЕ НЕ ИДЁТ (боевой случай 19.08).
    #
    # Четырёх состояний оказалось мало. Задача может умереть так, что пометить
    # себя не успеет: перезапуск воркера при выкатке, падение контейнера,
    # перезагрузка сервера. Ключ остаётся, `phase` остаётся «loading» — и
    # карточка честно, но неверно показывает «Загружаем историю…». Владелец
    # написал прямо: «походу загрузка диалогов зависла». Загрузки не было уже
    # десять часов: восемь каналов стояли на 850 из 1100 с утра.
    #
    # Отличаем живую от мёртвой по отметке времени: живой прогон обновляет
    # состояние каждые несколько секунд (`BackfillProgress.flush`), поэтому
    # молчание дольше STALE_AFTER означает, что обновлять его больше некому.
    # Возвращаем `failed`: карточка покажет «прервалась» и кнопку «Повторить»,
    # а загруженное останется на месте — продолжение пойдёт с той же точки.
    отметка = _as_dt(state.get("updated_at"))
    if отметка is not None and datetime.now(UTC) - отметка > BACKFILL_STALE_AFTER:
        return {"status": "failed", "stale": True, **out}
    return {"status": "running", **out}


async def count_accounts(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AvitoAccount)) or 0)


# --- OAuth-state и кнопка центра уведомлений (14 §3) -------------------------

OAUTH_STATE_TTL_SECONDS = 600  # oauth_state:{state} (08 §8.5)


async def issue_oauth_state(
    redis: Redis,
    user_id: uuid.UUID | str,
    *,
    reconnect_account_id: uuid.UUID | None = None,
    connect_link_token: str | None = None,
) -> str:
    """Одноразовый ``state`` для страницы согласия Авито (01 §4.2, 08 §8.5).

    Живёт в сервисе, а не в ручке: тот же state выписывает кнопка
    «Переподключить» из центра уведомлений, а сервис не имеет права
    импортировать модуль ручек (импорт идёт ровно в обратную сторону).
    """
    state = secrets.token_urlsafe(32)
    payload = {
        "user_id": str(user_id),
        "reconnect_account_id": str(reconnect_account_id) if reconnect_account_id else None,
        # Токен ссылки-приглашения едет ВНУТРИ state, чтобы погасить его в callback'е —
        # вплотную к записи аккаунта, а не на публичном GET (см. `peek_connect_link`).
        "connect_link_token": connect_link_token,
    }
    await redis.set(f"oauth_state:{state}", json.dumps(payload), ex=OAUTH_STATE_TTL_SECONDS)
    return state


#: Сколько живёт ссылка-приглашение на подключение аккаунта. Сутки — не
#: круглое число: за это время человек, которому её отправили, успевает дойти
#: до компьютера, а забытая в переписке ссылка не работает вечно.
CONNECT_LINK_TTL_SECONDS = 24 * 3600


async def issue_connect_link(redis: Redis, *, actor_id: uuid.UUID) -> tuple[str, int]:
    """Одноразовая ссылка «подключите свой аккаунт Авито». Возвращает (токен, TTL).

    ЗАЧЕМ. Подключить аккаунт может только тот, кто вошёл в LeadChat
    администратором. Но пароли от аккаунтов Авито у разных людей: девять
    учётных записей — это девять человек, и владелец системы не держит их
    пароли у себя (и правильно делает). До сих пор это означало «пришлите мне
    пароль» — то есть ровно то, чего делать нельзя.

    В Jivo это решалось ссылкой, и заказчик сказал прямо: «мы просто давали
    ссылку и всё». Ссылка открывает страницу согласия Авито и приводит
    подключённый аккаунт к нам; в LeadChat при этом заходить не нужно и
    доступа к нему не требуется.

    ЧТО ССЫЛКА ПОЗВОЛЯЕТ И ЧЕГО НЕТ. Она позволяет ровно одно: привязать
    аккаунт Авито к системе. Ни войти, ни увидеть переписку, ни что-либо
    поменять по ней нельзя. Живёт сутки и сгорает при первом использовании —
    иначе пересланная в общий чат ссылка осталась бы рабочей навсегда.
    """
    token = secrets.token_urlsafe(32)
    await redis.set(
        f"connect_link:{token}",
        json.dumps({"issued_by": str(actor_id)}),
        ex=CONNECT_LINK_TTL_SECONDS,
    )
    log.info("connect_link.issued", actor_id=str(actor_id))
    return token, CONNECT_LINK_TTL_SECONDS


async def peek_connect_link(redis: Redis, token: str) -> str | None:
    """Прочитать ссылку, НЕ гася её. Возвращает id выписавшего или ``None``.

    ⚠ ЗАЧЕМ ПОНАДОБИЛОСЬ ЧТЕНИЕ БЕЗ ГАШЕНИЯ (14.08). Переход по ссылке — это GET, и
    гасился он первой же строкой ручки. А ссылку ПО ЗАМЫСЛУ отправляют человеку в
    мессенджер: «мы просто давали ссылку и всё». Значит по этому GET первым приходит
    не человек, а превью-бот Telegram, сканер почты или префетч браузера — и ссылка
    сгорает ДО того, как её кто-нибудь нажмёт.

    Цена высокая именно здесь: на той стороне человек, у которого есть пароль от
    аккаунта Авито и НЕТ доступа в LeadChat. Перевыпустить ссылку он не может — только
    администратор, который о случившемся не знает. У заказчика девять аккаунтов Авито
    и девять таких людей.

    Одноразовость никуда не делась: гашение переехало в callback, вплотную к записи
    аккаунта. Ссылка умирает от СОСТОЯВШЕГОСЯ подключения, а не от чужого GET.
    """
    raw = await redis.get(f"connect_link:{token}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return cast(str | None, payload.get("issued_by"))


async def consume_connect_link(redis: Redis, token: str) -> str | None:
    """Погасить ссылку. Возвращает id выписавшего или ``None``, если её нет.

    GETDEL, а не GET: одноразовость обеспечивается атомарно, иначе два
    одновременных перехода по одной ссылке оба сочлись бы действительными.

    ⚠ ЗОВЁТСЯ ТОЛЬКО ИЗ callback'а И ТОЛЬКО ПЕРЕД ЗАПИСЬЮ АККАУНТА. Это последний
    рубеж от гонки: пустой ответ означает «кто-то успел раньше», и тогда мы не пишем
    ничего. Перенести вызов раньше — вернуть дефект, разобранный у `peek_connect_link`.
    """
    raw = await redis.getdel(f"connect_link:{token}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    issued_by = payload.get("issued_by")
    return str(issued_by) if issued_by else None


async def reconnect_account_action(
    db: AsyncSession, redis: Redis, *, entity_id: str | None, actor: User
) -> dict[str, Any]:
    """Кнопка «Переподключить» под событием ``account.needs_reauth`` (14 §3).

    Контракт действий центра уведомлений
    (:mod:`app.services.notifications`): ``(db, redis, *, entity_id, actor)
    -> dict``; возврат уезжает клиенту в поле ``result``. Здесь это ссылка на
    страницу согласия Авито — открыть её должен браузер администратора, и
    только он: без человека переподключение невозможно в принципе (Авито
    спрашивает согласие владельца аккаунта).

    Право (``accounts:manage``) проверил вызывающий; наше дело — цель:
    уведомление живёт 90 дней и переживает удалённый аккаунт. Ошибку
    прикладного уровня поднимаем своим ``ApiError`` — центр отдаст её как есть.
    """
    try:
        account_id = uuid.UUID(entity_id) if entity_id else None
    except ValueError:
        account_id = None
    if account_id is None:
        raise ApiError(
            "unprocessable",
            status=422,
            message="Уведомление не привязано к аккаунту — переподключите его в настройках",
            details={"reason": "entity_missing"},
        )

    account = await db.get(AvitoAccount, account_id)
    if account is None:
        raise ApiError(
            "not_found",
            status=404,
            message="Аккаунт Авито не найден — возможно, он уже удалён",
        )

    # КАНАЛ НА КЛЮЧАХ ЧИНИТСЯ ЗДЕСЬ ЖЕ, БЕЗ ПОХОДА В АВИТО.
    #
    # `client_id` и `client_secret` выдаются один раз и не меняются, поэтому
    # согласие владельца тут ни при чём: те же ключи, которыми канал работал
    # вчера, действительны и сегодня. Отправлять человека на страницу согласия
    # значило бы просить его подтвердить доступ, который и не терялся, — а
    # чинит канал один запрос токена.
    if has_own_keys(account):
        outcome = await refresh_outcome(account, db, redis)
        await db.refresh(account)
        log.info(
            "account.reconnect_action_keys",
            account_id=str(account.id),
            actor_id=str(actor.id),
            outcome=outcome,
        )
        # Причина — по исходу (проверка 24.09): совет «проверьте, не удалено ли
        # приложение» уместен только когда Авито ключи ОТВЕРГ, а не когда он
        # просто не ответил.
        messages = {
            REFRESH_OK: f"Канал «{account.title}» снова на связи",
            REFRESH_REVOKED: "Авито не принял ключи приложения. Проверьте в кабинете Авито, "
            "не удалено ли и не отключено ли приложение",
            REFRESH_BUSY: "Токен уже обновляется — подождите несколько секунд",
        }
        return {
            "account": {"id": str(account.id), "title": account.title, "status": account.status},
            "message": messages.get(
                outcome, "Авито не ответил — повторите через минуту. Ключи канала целы"
            ),
        }

    state = await issue_oauth_state(redis, actor.id, reconnect_account_id=account.id)
    log.info("account.reconnect_action", account_id=str(account.id), actor_id=str(actor.id))
    return {
        "url": build_authorize_url(state),
        "account": {"id": str(account.id), "title": account.title, "status": account.status},
        "message": "Откройте страницу Авито и подтвердите доступ — ссылка действует 10 минут",
    }


async def rewebhook_action(
    db: AsyncSession, redis: Redis, *, entity_id: str | None, actor: User
) -> dict[str, Any]:
    """Кнопка «Перерегистрировать» под событием ``webhook.lost``.

    ЧЕМ ОТЛИЧАЕТСЯ ОТ «ПЕРЕПОДКЛЮЧИТЬ». Та ведёт на страницу согласия Авито и
    нужна, когда доступ ОТОЗВАН: без человека и без согласия владельца её не
    обойти. Здесь доступ цел — жив токен, работают ключи, — пропала только
    подписка на события: кто-то подписался после нас, а Авито держит на
    аккаунт ровно одну. Чинится это одним запросом, без согласий и переходов.

    ЧИНИТ ВСЕ ЗАТРОНУТЫЕ КАНАЛЫ СРАЗУ, а не один. Уведомление склеено по виду
    и называет несколько аккаунтов: заставлять человека жать кнопку по разу на
    канал, разбирая, какой из них уже починен, значит выдавать ему работу
    вместо решения.
    """
    del entity_id  # событие склеено по виду: конкретного аккаунта у него нет

    accounts = (
        (await db.execute(select(AvitoAccount).where(AvitoAccount.status == "active")))
        .scalars()
        .all()
    )
    restored: list[str] = []
    failed: list[str] = []
    for account in accounts:
        if await register_webhook(account, redis):
            restored.append(account.title)
        else:
            failed.append(account.title)

    await write_audit(
        db,
        user_id=actor.id,
        action="account.webhook_reregistered",
        entity="avito_account",
        details={"restored": restored, "failed": failed},
    )
    await db.commit()

    # Отчёт называет и неудачи. Молчаливая половина хуже честного «два из трёх»:
    # человек уйдёт уверенным, что починил всё.
    return {"restored": restored, "failed": failed}
