"""ARQ-задача ``deliver_message`` — доставка исходящих в Авито (08 §3).

Контур: endpoint отвечает ``pending`` мгновенно, доставку делает воркер,
итог уходит WS-событием ``message:status`` (DESIGN §8.2, 01 §6.2).

Три фазы, границы которых — правило 08 §8.1 п.3 «внешний I/O внутри
транзакции запрещён»:

1. прочитать и проверить под ``FOR UPDATE`` (короткая транзакция);
2. поход в Авито — строго вне транзакции;
3. зафиксировать исход (короткая транзакция) и опубликовать событие.

Поведение на ошибках (07 §2.3):

* **429** — Retry-After истина: ждём не меньше него, попытка не сгорает зря;
* **401** — ровно один авто-рефреш токена и повтор; не вышло обновиться —
  хороним доставку, только если аккаунт реально ушёл в ``needs_reauth``,
  иначе это временная недоступность Авито и место ей в ретраях;
* **needs_reauth / disabled** — доставлять некому: сразу ``failed`` +
  уведомление админам;
* **5xx / таймаут** — экспоненциальный backoff 1, 2, 4, 8, 16 с (+джиттер),
  всего 5 попыток; исчерпали — ``failed`` с ``error_code``.

Задача идемпотентна к повторной постановке: доставляются только сообщения
в статусе ``pending`` (двойная джоба гасится на первой же фазе).
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
import structlog
from arq import Retry
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core import trace
from app.core.observability import with_job_scope

# Модуль целиком — ради `http_client` и часовых счётчиков доступности: их
# читает сторож «Авито не отвечает», и заливка картинки обязана попадать в тот
# же знаменатель, что и остальные походы (см. `_OutboundClient._request`).
from app.integrations.avito import client as avito_client_mod
from app.integrations.avito.client import AvitoClient
from app.integrations.avito.errors import (
    AvitoApiError,
    AvitoAuthError,
    AvitoUnavailable,
    RateLimited,
    TokenRevokedError,
)
from app.integrations.avito.ratelimit import AvitoRateLimiter
from app.models import AvitoAccount, Conversation, Message
from app.services import conversation_status as status_dict
from app.services.avito_text import split_text
from app.services.messages import (
    publish_status,
    refresh_undelivered,
    restore_awaiting,
    undelivered_status,
)
from app.services.notifications import NotifyResult, notify
from app.services.notifications import deliver as deliver_notification
from app.ws.events import iso, publish_event

log = structlog.get_logger("app.workers.deliver")

MAX_TRIES = 5  # «5 ретраев экспоненциально» (DESIGN §8.2 / 01 §6.2)
BACKOFF_BASE_SECONDS = 1  # 1, 2, 4, 8, 16 c
BACKOFF_CAP_SECONDS = 300
REAUTH_NOTIFY_TTL = 300  # не спамить админам одним и тем же аккаунтом
# deliver:parts:{message_id} — сколько частей длинного ответа уже у клиента.
# Срок жизни тот же, что у ключа идемпотентности отправки (24 ч): столько же
# живёт право нажать «Повторить» по неотправленному сообщению, а повтор обязан
# помнить, что клиент уже прочитал.
PARTS_PROGRESS_TTL = 86_400

REAUTH_ERROR = "Аккаунт Авито требует переподключения"
EXHAUSTED_ERROR = "Авито: сообщение не доставлено после 5 попыток"
# ⚠ ПРЕЖНИЙ ТЕКСТ ВРАЛ, И ВРАЛ ДОРОГО: «Отправка вложений в Авито пока
# недоступна» читается как «подождите, скоро включат», и оператор прикладывал
# тот же pdf снова и снова. Правда другая и не изменится сама: Авито принимает
# от нас ИЗОБРАЖЕНИЯ (`uploadImages` + `messages/image`), а метода для
# произвольного файла в каталоге нет вовсе
# (docs/26-AVITO-API-CATALOG.md, строки 49 и 63).
ATTACHMENTS_ERROR = "Авито принимает от нас только изображения — PDF отправить нельзя"
#: Строка вложения есть, а файла в хранилище нет (истёк, потерян, том не
#: смонтирован). Повтор здесь бессмысленен: файл повтором не вернётся.
MEDIA_MISSING_ERROR = "Файл вложения не найден в хранилище — приложите его заново"
#: Авито ответил 404 на отправку: чат удалён или клиент заблокировал (проверка
#: 24.09). В бою с 08.09 у 16 из 22 проваленных отправок все пять попыток дали
#: 404 — и оператор читал «не доставлено после 5 попыток», не узнавая причины.
CHAT_GONE_ERROR = (
    "Авито не принимает сообщения в этот чат (404): чат удалён или клиент заблокировал"
)

#: Имя поля формы при заливке картинки — контракт Авито, а не наша выдумка.
IMAGE_FIELD = "uploadfile[]"

ERROR_CODES = {
    REAUTH_ERROR: "account_needs_reauth",
    EXHAUSTED_ERROR: "delivery_exhausted",
    ATTACHMENTS_ERROR: "attachments_unsupported",
    # MEDIA_MISSING_ERROR намеренно не здесь: фронт различает коды, а нового
    # кода под пропавший файл он не знает. Пусть приедет общий
    # `delivery_failed` с честным текстом, чем незнакомый код без обработчика.
}


def backoff(attempt: int) -> float:
    """Экспоненциальный backoff с джиттером ±20%: ~1, 2, 4, 8, 16 c."""
    base = BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1)
    return min(BACKOFF_CAP_SECONDS, base * (0.8 + random.random() * 0.4))


def _refused_for_good(exc: Exception) -> str | None:
    """Текст для оператора, если Авито отказал так, что повтор не поможет.

    Постоянный отказ — ответ 4xx, кроме 401/403 (токен: своя ветка выше) и 429
    (квота: своя ветка выше). Сеть и 5xx — временные, их повторяем. Сгоревший
    refresh-токен (400 обмена) — не отказ сообщению: его разбирает обновление
    токена.
    """
    if not isinstance(exc, AvitoApiError) or isinstance(exc, (AvitoUnavailable, TokenRevokedError)):
        return None
    status = exc.status
    if status is None or not 400 <= status < 500 or status in (401, 403, 429):
        return None
    if status == 404:
        return CHAT_GONE_ERROR
    return f"Авито не принял сообщение ({status}) — повтор не поможет"


# ------------------------------------------------------------- отправка в API


class _OutboundClient(AvitoClient):
    """Ручки Авито, которыми доставка кладёт ответ оператора в чат.

    Текст — ``POST .../chats/{chat_id}/messages``; фото — две ступени,
    ``POST .../uploadImages`` и ``POST .../chats/{chat_id}/messages/image``.
    Произвольного файла в этом списке нет и не будет: метода у Авито нет
    (docs/26-AVITO-API-CATALOG.md, строки 49 и 63).

    Методы живут здесь, а не в ``AvitoClient``/``AvitoAdapter``: те файлы —
    чужая зона (см. cross-boundary). Наследование даёт готовые разбор
    статусов (401/429/4xx -> типизированные ошибки) и таймаут 15 с.
    """

    async def send_message(self, token: str, user_id: int, chat_id: str, text: str) -> str:
        resp = await self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
            token=token,
            json={"message": {"text": text}, "type": "text"},
        )
        self._raise_for_status(resp, "отправка сообщения")
        payload = self._json(resp, "отправка сообщения")
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise AvitoApiError("Авито: ответ на отправку без идентификатора сообщения")
        return external_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """То же, что у родителя, плюс multipart: заливка картинки — форма, не JSON.

        Без `files` уходим в родителя ЦЕЛИКОМ и намеренно. Там живёт разбор
        сетевого сбоя и признак «тело запроса успело уйти в сокет»
        (`after_send`), от которого зависит защита клиента от дублей; второй
        его экземпляр разъехался бы с первым при первой же правке.

        В ветке с `files` тот же поход и те же счётчики доступности (их читает
        сторож «Авито не отвечает»), но сетевой сбой превращается в
        `AvitoUnavailable` БЕЗ `after_send`. Здесь это не упрощение, а правда:
        единственный multipart в системе — заливка картинки, и у неё нет
        наблюдаемого следствия у клиента. Повторная заливка даёт новый
        `image_id` и никому ничего не показывает; дублем в переписке грозит
        только второй шаг, `messages/image`, а он идёт обычным телом через
        родителя со всем его разбором.
        """
        if files is None:
            return await super()._request(
                method, path, token=token, params=params, json=json, data=data
            )
        headers = {"Authorization": f"Bearer {token}"} if token else None
        await avito_client_mod._отметить_попытку()
        try:
            resp = await avito_client_mod.http_client().request(
                method,
                f"{self._base}{path}",
                headers=headers,
                params=params,
                files=files,
            )
        except httpx.HTTPError as exc:
            # Причину называем всегда: у половины сетевых исключений httpx
            # пустой `str(exc)` — см. разбор в `AvitoClient._request`.
            причина = str(exc) or type(exc).__name__
            log.warning(
                "avito.unreachable",
                method=method,
                path=path,
                error=причина,
                error_kind=type(exc).__name__,
            )
            await avito_client_mod._отметить_недоступность(причина)
            raise AvitoUnavailable() from exc
        log.debug("avito.request", method=method, path=path, status=resp.status_code)
        return resp

    async def upload_image(
        self, token: str, user_id: int, data: bytes, filename: str, content_type: str
    ) -> str:
        """``POST /messenger/v1/accounts/{uid}/uploadImages`` -> ``image_id``.

        ⚠ ИДЕНТИФИКАТОР ЛЕЖИТ В КЛЮЧЕ ОТВЕТА, А НЕ В ПОЛЕ. Авито отвечает
        ``{"<image_id>": {"<размер>": "<url>", ...}}``
        (docs/26-AVITO-API-CATALOG.md, стр. 63) — привычный разбор
        ``payload["id"]`` вернул бы пустоту, и фото не ушло бы никогда.
        Имитатор повторяет эту форму намеренно, чтобы ошибка разбора не
        доехала до боя молча (fake_avito/main.py::upload_images).
        """
        resp = await self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/uploadImages",
            token=token,
            files={
                IMAGE_FIELD: (
                    filename or "image",
                    data,
                    content_type or "application/octet-stream",
                )
            },
        )
        self._raise_for_status(resp, "заливка изображения")
        payload = self._json(resp, "заливка изображения")
        for image_id, размеры in payload.items():
            if isinstance(image_id, str) and image_id and isinstance(размеры, dict):
                return image_id
        raise AvitoApiError("Авито: ответ на заливку изображения без идентификатора")

    async def send_image(self, token: str, user_id: int, chat_id: str, image_id: str) -> str:
        """``POST .../chats/{chat_id}/messages/image`` — вторая ступень отправки фото."""
        resp = await self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/image",
            token=token,
            json={"image_id": image_id},
        )
        self._raise_for_status(resp, "отправка изображения")
        payload = self._json(resp, "отправка изображения")
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise AvitoApiError("Авито: ответ на отправку изображения без идентификатора")
        return external_id

    async def _recent_own(
        self, token: str, user_id: int, chat_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        """Хвост чата, только НАШИ сообщения. Свои отличаем по автору."""
        resp = await self._request(
            "GET",
            f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
            token=token,
            params={"limit": limit, "offset": 0},
        )
        self._raise_for_status(resp, "чтение хвоста чата")
        payload = self._json(resp, "чтение хвоста чата")
        строки = payload.get("messages") if isinstance(payload, dict) else None
        return [
            m
            for m in (строки or [])
            if isinstance(m, dict) and str(m.get("author_id") or "") == str(user_id)
        ]

    async def recent_own_image_urls(
        self, token: str, user_id: int, chat_id: str, *, limit: int = 5
    ) -> list[str]:
        """Ссылки на картинки из последних НАШИХ сообщений — сверка после таймаута.

        ⚠ СВЕРЯЕМ ПО ВХОЖДЕНИЮ ``image_id`` В ССЫЛКУ, И ЭТО ВСЁ, ЧЕМ МЫ
        РАСПОЛАГАЕМ. У сообщения-картинки Авито отдаёт только
        ``content.image.sizes`` — набор ссылок; отдельного поля с
        идентификатором заливки там нет, а идентификатор самой отправки мы в
        этом сценарии как раз и не получили (в этом вся беда). Идентификатор
        картинки входит в её ссылку, поэтому подстрока отвечает на вопрос
        «эта ли картинка уже в чате».

        Не сошлось — значит «не знаем», и доставка отправит: правило то же,
        что у текста, потерять хуже, чем повторить.
        """
        свои = await self._recent_own(token, user_id, chat_id, limit=limit)
        ссылки: list[str] = []
        for m in свои:
            content = m.get("content")
            image = content.get("image") if isinstance(content, dict) else None
            sizes = image.get("sizes") if isinstance(image, dict) else None
            if not isinstance(sizes, dict):
                continue
            ссылки.extend(u for u in sizes.values() if isinstance(u, str) and u)
        return ссылки

    async def recent_own_texts(
        self, token: str, user_id: int, chat_id: str, *, limit: int = 5
    ) -> list[str]:
        """Тексты последних НАШИХ сообщений в чате — для сверки после таймаута.

        ⚠ ЗАЧЕМ ЭТО ВООБЩЕ (аудит 19.08, находка L-007). У отправки в Авито нет
        ключа идемпотентности: если запрос ушёл, а ответа мы не дождались,
        узнать «приняли или нет» можно единственным способом — спросить сам чат.
        Без этого повтор отправлял часть вторым разом, и клиент видел одно и то
        же сообщение дважды.

        Берём короткий хвост: сверяем ровно то, что могли отправить только что.
        """
        свои = await self._recent_own(token, user_id, chat_id, limit=limit)
        out: list[str] = []
        for m in свои:
            текст = ((m.get("content") or {}) if isinstance(m.get("content"), dict) else {}).get(
                "text"
            )
            if isinstance(текст, str) and текст:
                out.append(текст)
        return out


def _parts_key(message_id: uuid.UUID) -> str:
    return f"deliver:parts:{message_id}"


@dataclass
class _Прогресс:
    """Что из этого сообщения клиент уже получил.

    ⚠ ОДНА ЗАПИСЬ НА СООБЩЕНИЕ, А НЕ ДВЕ. Ключ ``deliver:parts:{message_id}``
    вёл счёт только частям текста; с картинками у доставки появился второй
    предмет счёта, и отдельный ключ под него означал бы два источника правды
    с раздельным сроком жизни. Порядок отправки — картинки, потом текст, —
    поэтому сохранение текста идёт ПОСЛЕ картинок: пиши оно только свои поля,
    оно стирало бы память о доехавших фото, и повтор после падения фазы 3
    отправил бы их клиенту второй раз.
    """

    #: сколько частей текста уже у клиента
    sent: int = 0
    #: идентификатор последнего принятого Авито сообщения — уедет в строку БД
    external_id: str | None = None
    #: сколько картинок уже у клиента
    images_sent: int = 0
    #: номер картинки (строкой, как требует JSON) -> уже залитый ``image_id``
    image_ids: dict[str, str] = field(default_factory=dict)


async def _load_progress(
    redis: Redis, message_id: uuid.UUID, *, parts_total: int, images_total: int
) -> _Прогресс:
    """Прогресс прошлых попыток по этому сообщению.

    Мусор и рассинхрон с содержимым трактуем как «прогресса нет»: лишний
    повтор куска неприятен, но пропущенный кусок — дыра в ответе, и она хуже.

    ⚠ СТАРАЯ ЗАПИСЬ ОБЯЗАНА ЧИТАТЬСЯ. В момент выкатки в Redis лежат ключи
    вида ``{"sent": N, "external_id": ...}`` — от сообщений, которые сейчас
    между попытками (docs/21-ROADMAP-2026-08-07.md, стр. 246 прямо про это).
    Отсутствие новых полей читается как «картинок не отправлено», и это
    ровно правда: прежняя доставка их и не умела.
    """
    raw = await redis.get(_parts_key(message_id))
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not raw:
        return _Прогресс()
    try:
        state = json.loads(raw)
        sent = int(state["sent"])
    except (ValueError, TypeError, KeyError):
        return _Прогресс()
    if not isinstance(state, dict) or not 0 <= sent <= parts_total:
        return _Прогресс()
    external_id = state.get("external_id")
    прогресс = _Прогресс(
        sent=sent,
        external_id=external_id if isinstance(external_id, str) and external_id else None,
    )
    images_sent = state.get("images_sent")
    # bool — подкласс int, а `True` в этом поле означает испорченную запись.
    if isinstance(images_sent, int) and not isinstance(images_sent, bool):
        if 0 <= images_sent <= images_total:
            прогресс.images_sent = images_sent
    ids = state.get("image_ids")
    if isinstance(ids, dict):
        прогресс.image_ids = {str(k): v for k, v in ids.items() if isinstance(v, str) and v}
    return прогресс


async def _save_progress(redis: Redis, message_id: uuid.UUID, прогресс: _Прогресс) -> None:
    await redis.set(
        _parts_key(message_id),
        json.dumps(
            {
                "sent": прогресс.sent,
                "external_id": прогресс.external_id,
                "images_sent": прогресс.images_sent,
                "image_ids": прогресс.image_ids,
            }
        ),
        ex=PARTS_PROGRESS_TTL,
    )


#: Отметка «часть могла уйти, но ответа мы не дождались». Живёт столько же,
#: сколько прогресс частей: дольше она бессмысленна, короче — не переживёт
#: паузу между попытками (16 секунд на пятой).
def _doubt_key(message_id: uuid.UUID) -> str:
    return f"deliver:doubt:{message_id}"


async def _отметить_сомнение(redis: Redis, message_id: uuid.UUID, part: int | str) -> None:
    """Запомнить, что часть отправляли и ответа не получили.

    Метка — строка, потому что предметов теперь два: у текста это номер части
    (``"0"``), у картинки — ``"img:0"``. Пересечься они не могут, а сомнение в
    один момент времени бывает ровно одно: картинки уходят до текста.
    """
    await redis.setex(_doubt_key(message_id), PARTS_PROGRESS_TTL, str(part))


async def _возможно_ушло(redis: Redis, message_id: uuid.UUID, part: int | str) -> bool:
    raw = await redis.get(_doubt_key(message_id))
    if raw is None:
        return False
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return value == str(part)


async def _забыть_сомнение(redis: Redis, message_id: uuid.UUID) -> None:
    await redis.delete(_doubt_key(message_id))


async def _запомнить_свою_отправку(redis: Redis, account_id: uuid.UUID, external_id: str) -> None:
    """id нашей отправки — в множество «свои» СРАЗУ, до записи в БД.

    Эхо-вебхук Авито приходит мгновенно и на КАЖДУЮ отправку, а в строке
    сообщения сохраняется id только последней: без этого эхо частей и картинок
    вставлялось дублями и глушило бота его же сообщением (аудит свежего слоя
    17.08, №2/№4).
    """
    try:
        ключ = f"echo:own:{account_id}"
        await redis_mod.aw(redis.sadd(ключ, external_id))
        await redis_mod.aw(redis.expire(ключ, 7200))
    except Exception as exc:  # noqa: BLE001 — дедуп по БД подстрахует
        # Молча ронять нельзя: если множество перестанет наполняться, дубли
        # эха вернутся, а видно это будет только по чужому симптому.
        log.warning("message.own_echo_mark_failed", error=f"{type(exc).__name__}: {exc}")


class _ФайлПотерян(Exception):
    """Вложение есть в строке сообщения, а файла в хранилище нет.

    Отдельный тип, потому что исход у этой беды свой: повторять нечего —
    файл повтором не появится, — и доставка обязана сразу стать красной с
    понятным текстом, а не отсчитывать пять попыток.
    """


def _прочитать_вложение(вложение: dict[str, Any]) -> bytes:
    """Тело вложения с диска.

    ⚠ ЧИТАЕМ С ДИСКА, А НЕ ПО ПОДПИСАННОЙ ССЫЛКЕ. Воркер видит том вложений
    на запись (docker-compose.prod.yml, сервис worker), а поход за
    собственным файлом через nginx добавил бы к доставке ещё одну сеть и ещё
    одну подпись, которая умеет протухать (`app/services/media.py`).
    """
    from app.services.media import media_root

    relpath = вложение.get("path")
    if not isinstance(relpath, str) or not relpath:
        raise _ФайлПотерян("у вложения нет пути в хранилище")
    корень = media_root().resolve()
    try:
        файл = (корень / relpath).resolve()
    except OSError as exc:
        raise _ФайлПотерян(f"путь вложения не разбирается: {relpath}") from exc
    # Путь пришёл из нашей же строки в БД — и всё-таки проверяем: чтение файла
    # по значению из данных ровно там и становится дырой, где «своим данным»
    # верят на слово.
    if not файл.is_relative_to(корень):
        raise _ФайлПотерян(f"путь вложения ведёт за пределы хранилища: {relpath}")
    try:
        return файл.read_bytes()
    except OSError as exc:
        raise _ФайлПотерян(f"файл вложения не читается: {relpath} ({exc})") from exc


T = TypeVar("T")


class _ТокенАккаунта:
    """Поход в Авито токеном аккаунта с правом на ОДИН авто-рефреш за доставку.

    Вынесено из ``_send_parts``, когда доставка научилась отправлять картинки:
    разбор 401 здесь стоит на двух боевых бедах сразу (#28 и ложная ночная
    тревога «канал требует переподключения»), и второй его экземпляр рядом
    разъехался бы с первым при первой же правке.
    """

    def __init__(self, db: AsyncSession, redis: Redis, account: AvitoAccount) -> None:
        self._db = db
        self._redis = redis
        self._account = account
        self._refreshed = False

    async def вызвать(self, действие: Callable[[str], Awaitable[T]]) -> T:
        from app.services import crypto  # локально: горячий путь не тянет зависимость зря
        from app.services.avito_accounts import refresh_tokens

        while True:
            try:
                return await действие(crypto.decrypt_token(self._account.access_token_enc))
            except AvitoAuthError as auth_exc:
                if self._refreshed:
                    raise
                self._refreshed = True
                # False здесь значит ровно одно: свежего токена у нас нет — либо
                # доступ отозвали (тогда needs_reauth и событие уже выставлены
                # внутри), либо обновиться не вышло. Повторять ТЕМ ЖЕ мёртвым
                # токеном бессмысленно: это гарантированный второй 401, лишний
                # поход в Авито и лишняя единица лимита.
                #
                # Раньше здесь ждала беда #28. Возврат не проверялся вовсе, а
                # False приходил и в безобидном случае «обновляет соседний
                # процесс». Строку перечитывали, видели «active», считали канал
                # здоровым — и повторяли протухшим токеном. Отправка падала на
                # исправном канале, клиент ответа не получал. Ожидание конкурента
                # живёт теперь внутри refresh_tokens, поэтому здесь достаточно
                # честно спросить: токен свежий или нет.
                if not await refresh_tokens(self._account, self._db, self._redis):
                    # НО «НЕ ОБНОВИЛСЯ» — ЭТО ЕЩЁ НЕ «ДОСТУП ОТОЗВАЛИ».
                    #
                    # False из refresh_tokens приходит и на обычной недоступности
                    # Авито: там это записано прямым текстом («ВРЕМЕННАЯ БЕДА —
                    # СТАТУС НЕ ТРОГАЕМ»), и оттуда же False возвращается, когда
                    # не дождались чужого обновления. А наверху голый
                    # AvitoAuthError читался ровно как отзыв доступа: админам
                    # уходила критичная тревога «канал требует переподключения»,
                    # сообщение хоронилось насовсем без единого повтора. Цена —
                    # ложная ночная тревога на исправном канале и потерянный
                    # ответ клиенту из-за минутной недоступности Авито; человек
                    # шёл переподключать канал, с которым всё было в порядке.
                    #
                    # Отличаем по единственному честному признаку: настоящий
                    # отзыв refresh_tokens уже записал в статус аккаунта. Статус
                    # перечитываем из базы, а не из ORM-объекта: пометить мог
                    # соседний процесс, и его правку наша строка в памяти не
                    # видит. Не needs_reauth — беда временная, отдаём её в общую
                    # ветку повторов: пять попыток с backoff, и только если
                    # Авито не поднялся — честное «не доставлено».
                    revoked = (
                        await self._db.execute(
                            select(AvitoAccount.status).where(AvitoAccount.id == self._account.id)
                        )
                    ).scalar_one_or_none() == "needs_reauth"
                    if revoked:
                        raise
                    raise AvitoUnavailable(
                        "Авито: не удалось обновить токен — повторим"
                    ) from auth_exc
                # повторяем тот же вызов уже свежим токеном


async def _уже_в_чате(client: Any, account: AvitoAccount, chat_id: str, text: str) -> bool:
    """Есть ли этот текст среди последних наших сообщений чата.

    Сравниваем по тексту, а не по идентификатору: идентификатор той отправки мы
    как раз и не получили — в этом вся беда. Смотрим только свежий хвост: тот же
    текст, отправленный неделю назад, к этому повтору отношения не имеет.

    Ошибка чтения — не повод гадать: возвращаем False и отправляем. Потерять
    ответ клиенту хуже, чем повторить его.
    """
    from app.services import crypto  # локально: горячий путь не тянет зависимость зря

    try:
        свои = await client.recent_own_texts(
            crypto.decrypt_token(account.access_token_enc), account.avito_user_id, chat_id
        )
    except Exception:  # noqa: BLE001 — сверка не имеет права ронять доставку
        log.warning("message.echo_probe_failed", chat_id=chat_id)
        return False
    return text.strip() in {t.strip() for t in свои}


async def _картинка_уже_в_чате(
    client: Any, account: AvitoAccount, chat_id: str, image_id: str
) -> bool:
    """Зеркало ``_уже_в_чате`` для фото: ищем ``image_id`` в ссылках хвоста.

    Сверка возможна только по ссылке — отдельного поля с идентификатором у
    сообщения-картинки Авито нет (см. ``recent_own_image_urls``). Не сошлось
    или чтение упало — отправляем: правило то же, что у текста.
    """
    from app.services import crypto

    try:
        ссылки = await client.recent_own_image_urls(
            crypto.decrypt_token(account.access_token_enc), account.avito_user_id, chat_id
        )
    except Exception:  # noqa: BLE001 — сверка не имеет права ронять доставку
        log.warning("message.image_probe_failed", chat_id=chat_id)
        return False
    return any(image_id in ссылка for ссылка in ссылки)


async def _send_images(
    client: _OutboundClient,
    redis: Redis,
    токен: _ТокенАккаунта,
    account: AvitoAccount,
    chat_id: str,
    картинки: list[dict[str, Any]],
    message_id: uuid.UUID,
    прогресс: _Прогресс,
) -> None:
    """Отправка картинок клиенту: заливка, затем отправка. Мутирует ``прогресс``.

    ⚠ ДВА ЗАПРОСА НА ОДНУ КАРТИНКУ, КЛЮЧА ИДЕМПОТЕНТНОСТИ У АВИТО НЕТ.
    Про эту опасность прямо предупреждает docs/21-ROADMAP-2026-08-07.md
    (строка 246): обрыв между ``uploadImages`` и ``messages/image`` при повторе
    джобы заливает фото ЗАНОВО и отправляет второй раз — клиент видит дубль, а
    отозвать отправленное в Авито нельзя. Поэтому ступени отмечаются в
    прогрессе раздельно: залитый, но не отправленный ``image_id`` переживает и
    ретрай, и «Повторить», и на второй попытке используется он, а не новая
    заливка.
    """
    limiter = AvitoRateLimiter(redis)
    # Файлы читаем ДО первого похода в Авито. Пропавший файл — это отказ, и
    # узнать о нём после того, как половина фото уже у клиента, значит
    # оставить сообщение доставленным наполовину.
    тела: dict[int, bytes] = {
        i: _прочитать_вложение(картинки[i])
        for i in range(прогресс.images_sent, len(картинки))
        if str(i) not in прогресс.image_ids
    }
    индекс = прогресс.images_sent
    if индекс:
        log.info(
            "message.images_resumed",
            message_id=str(message_id),
            sent=индекс,
            total=len(картинки),
        )
    while индекс < len(картинки):
        метка = f"img:{индекс}"
        image_id = прогресс.image_ids.get(str(индекс))
        # ⚠ ТАЙМАУТ НЕ ЗНАЧИТ «НЕ ДОШЛО» — то же, что у текста (находка L-007).
        # Спрашиваем чат только после обрыва на ответе и только про эту картинку.
        if image_id is not None and await _возможно_ушло(redis, message_id, метка):
            if await _картинка_уже_в_чате(client, account, chat_id, image_id):
                log.info(
                    "message.image_already_there",  # повтор не нужен: фото дошло
                    message_id=str(message_id),
                    part=индекс,
                )
                индекс += 1
                прогресс.images_sent = индекс
                await _save_progress(redis, message_id, прогресс)
                await _забыть_сомнение(redis, message_id)
                continue
            await _забыть_сомнение(redis, message_id)
        if image_id is None:
            # Заливка — такой же поход в Авито, как отправка: бюджет общий (08 §4.3).
            await limiter.acquire(str(account.id), bucket="interactive")
            вложение = картинки[индекс]

            async def залить(
                t: str, тело: bytes = тела[индекс], вложение: dict[str, Any] = вложение
            ) -> str:
                # Значения связаны через умолчания: замыкание на переменную
                # цикла здесь читалось бы как «залить последнюю картинку».
                return await client.upload_image(
                    t,
                    account.avito_user_id,
                    тело,
                    str(вложение.get("name") or "image"),
                    str(вложение.get("mime") or "application/octet-stream"),
                )

            image_id = await токен.вызвать(залить)
            # Отметка СРАЗУ после заливки, до отправки: ровно этот промежуток и
            # стоит клиенту дубля, если его не запомнить.
            прогресс.image_ids[str(индекс)] = image_id
            await _save_progress(redis, message_id, прогресс)
        await limiter.acquire(str(account.id), bucket="interactive")

        async def отправить(t: str, готовый: str = image_id) -> str:
            return await client.send_image(t, account.avito_user_id, chat_id, готовый)

        try:
            external_id = await токен.вызвать(отправить)
        except AvitoUnavailable as net_exc:
            # «Ушло, ответа не дождались» — единственный случай, когда повтор
            # может дать клиенту дубль. Помечаем картинку, и следующая попытка
            # сперва спросит чат, а не отправит вслепую.
            if getattr(net_exc, "after_send", False):
                await _отметить_сомнение(redis, message_id, метка)
            raise
        await _запомнить_свою_отправку(redis, account.id, external_id)
        индекс += 1
        прогресс.images_sent = индекс
        прогресс.external_id = external_id
        await _save_progress(redis, message_id, прогресс)


async def _send_parts(
    client: _OutboundClient,
    redis: Redis,
    токен: _ТокенАккаунта,
    account: AvitoAccount,
    chat_id: str,
    parts: list[str],
    message_id: uuid.UUID,
    прогресс: _Прогресс,
) -> None:
    """Отправка частей текста. Мутирует ``прогресс`` (в нём и внешний id).

    429 пробрасывается наверх — решение о задержке принимает
    ``deliver_message`` (Retry-After истина).

    ДЛИННЫЙ ОТВЕТ ПРОДОЛЖАЕТСЯ С МЕСТА ОБРЫВА, А НЕ С НАЧАЛА.
    Прайс на 2500 знаков режется на три сообщения; если Авито ответил 500 на
    третьем, повтор джобы начинал заново — клиент получал первые два куска
    ВТОРОЙ раз. При пяти попытках это до девяти лишних сообщений в чате: стена
    дублей, в которой не найти цену, оператор со стороны выглядит сломанным
    ботом, а Авито за это ещё и режет лимит. Поэтому число ушедших частей
    живёт в Redis по ``message_id`` и переживает и ретрай, и «Повторить»:
    номер части — единственное, чего не знает ни база, ни ARQ.
    """
    limiter = AvitoRateLimiter(redis)
    index = прогресс.sent
    if index:
        log.info(
            "message.parts_resumed",
            message_id=str(message_id),
            sent=index,
            total=len(parts),
        )
    while index < len(parts):
        # ⚠ ТАЙМАУТ НЕ ЗНАЧИТ «НЕ ДОШЛО» (аудит 19.08, находка L-007). Если
        # прошлая попытка оборвалась по времени ПОСЛЕ того, как Авито приняло
        # часть, повтор отправлял бы её вторым разом: клиент видит одно и то же
        # сообщение дважды. Ключа идемпотентности у чужого API нет, поэтому
        # единственный честный способ — спросить сам чат, что там последнее.
        #
        # Спрашиваем ТОЛЬКО после таймаута и только про одну часть: один
        # лишний запрос на редкий случай против дубля в переписке клиента.
        # Не ответили — отправляем: потерять ответ хуже, чем повторить.
        if await _возможно_ушло(redis, message_id, index):
            if await _уже_в_чате(client, account, chat_id, parts[index]):
                log.info(
                    "message.part_already_there",  # повтор не нужен: часть дошла
                    message_id=str(message_id),
                    part=index,
                )
                index += 1
                прогресс.sent = index
                await _save_progress(redis, message_id, прогресс)
                await _забыть_сомнение(redis, message_id)
                continue
            await _забыть_сомнение(redis, message_id)
        await limiter.acquire(str(account.id), bucket="interactive")  # 08 §4.3

        async def отправить(t: str, текст: str = parts[index]) -> str:
            # Умолчание, а не замыкание: иначе на второй итерации ушёл бы
            # текст последней части (ruff B023).
            return await client.send_message(t, account.avito_user_id, chat_id, текст)

        try:
            external_id = await токен.вызвать(отправить)
            if external_id:
                await _запомнить_свою_отправку(redis, account.id, external_id)
        except AvitoUnavailable as net_exc:
            # «Ушло, ответа не дождались» — единственный случай, когда повтор
            # может дать клиенту дубль (находка L-007). Помечаем часть, и
            # следующая попытка сперва спросит чат, а не отправит вслепую.
            if getattr(net_exc, "after_send", False):
                await _отметить_сомнение(redis, message_id, index)
            raise
        index += 1
        прогресс.sent = index
        прогресс.external_id = external_id
        # Отметка ставится ПОСЛЕ каждой части, а не в конце: смысл в том,
        # чтобы её пережил именно обрыв на середине. Ключ не удаляется и
        # после успеха — фаза 3 может упасть на базе уже после отправки, и
        # тогда повтор джобы обязан увидеть, что клиенту всё ушло.
        await _save_progress(redis, message_id, прогресс)


# --------------------------------------------------------------- вспомогалки


async def _lock_message(db: AsyncSession, message_id: uuid.UUID) -> Message | None:
    """Строка сообщения под ``FOR UPDATE`` (08 §8.4): двойная постановка
    джобы не должна дать двойную отправку."""
    return (
        await db.execute(select(Message).where(Message.id == message_id).with_for_update())
    ).scalar_one_or_none()


async def _fail(ctx: dict[str, Any], message_id: uuid.UUID, error: str) -> None:
    """`delivery_status` из `undelivered_status` + отметки в диалоге + WS message:status.

    Своя сессия — вызывающий контур может держать открытую транзакцию после
    рефреша токена.

    ЗДЕСЬ ЖЕ ЧИНИТСЯ ДИАЛОГ, А НЕ ТОЛЬКО СООБЩЕНИЕ (#26). Провал доставки —
    единственное место, где система узнаёт, что ответа у клиента нет; всё
    остальное уже произошло: отметка «клиент ждёт» погашена нажатием
    «Отправить», строка в списке выглядит отвеченной. Обе неправды правятся
    ровно тут, одной транзакцией с самим статусом, — иначе между ними
    существует состояние, в котором сообщение уже провалено, а сторож
    «клиент ждёт 15 минут» на диалог всё ещё не сработает.
    """
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    patch: dict[str, Any] = {}
    notified: NotifyResult | None = None
    async with factory() as db:
        async with db.begin():
            msg = await _lock_message(db, message_id)
            if msg is None or msg.delivery_status != "pending":
                return  # уже delivered/failed — второй раз не публикуем
            conversation_id = msg.conversation_id
            author_id = msg.sender_user_id
            # `failed` — долг оператора; вопрос системы об адресе сразу
            # `dismissed` (services/messages.py::undelivered_status).
            итог = undelivered_status(msg)
            msg.delivery_status = итог
            # Порядок захвата 08 §8.4 нарушать нельзя (сначала conversations,
            # потом messages), но здесь строка сообщения уже под замком, а
            # диалог берётся без FOR UPDATE: оба поля вычисляются из переписки
            # заново, поэтому потерянного обновления тут не бывает.
            conv = await db.get(Conversation, conversation_id)
            if conv is not None:
                await refresh_undelivered(db, conv)
                await restore_awaiting(db, conv)
                patch = {
                    "undelivered": conv.undelivered_at is not None,
                    # ⚠ ИМЯ ПОЛЯ БЫЛО МИМО ЭКРАНА (28.08). Здесь годами ехал
                    # `awaiting_since` — серверное имя, которого во фронте нет
                    # ни одного: шкалу считает `waiting_since` (shared/lib/
                    # waiting.ts). То есть восстановленное ожидание не доезжало
                    # до строки НИКОГДА, и сообщение, не ушедшее клиенту,
                    # оставалось на экране без всякой тревоги до полной
                    # перезагрузки списка. Проверка на это была и зеленела —
                    # она сверяла ровно то ненужное имя, которое сервер и слал.
                    "waiting_since": iso(status_dict.waiting_since(conv)),
                }
            # Колокольчик автору (#26). Красная строка в списке помогает
            # только тому, кто на список смотрит; автор к этому моменту уже
            # ушёл в следующий диалог. Уведомление — единственное, что найдёт
            # его на другом экране. Пишется в той же транзакции, что и статус,
            # публикуется после неё: событие о незакоммиченных данных — 404 на
            # детали у фронта (08 §8.1 п.4).
            if author_id is not None:
                notified = await notify(
                    db,
                    kind="message.undelivered",
                    recipient_id=author_id,
                    body=error,
                    entity_type="conversation",
                    entity_id=str(conversation_id),
                )
    await publish_status(
        redis,
        conversation_id=conversation_id,
        message_id=message_id,
        delivery_status=итог,
        error=error,
        error_code=ERROR_CODES.get(error, "delivery_failed"),
        conversation_patch=patch,
    )
    if notified is not None:
        await deliver_notification(redis, notified)
    log.warning("message.deliver", message_id=str(message_id), outcome="failed", error=error)
    # Последний шаг пути. Дальше сообщения нет — есть только ответ Авито,
    # и по этой отметке видно, что путь кончился здесь (`app/core/trace.py`).
    trace.step("trace.delivery_failed", message_id=str(message_id), error=error)


async def _notify_admins_reauth(redis: Redis, account_id: uuid.UUID, title: str) -> None:
    """account:needs_reauth админам (01 §11.3), не чаще раза в 5 минут.

    Принимает простые значения, а не ORM-объект: после rollback'а сессии
    атрибуты истекают, и ленивая подгрузка в async-коде падает MissingGreenlet.
    """
    if not await redis.set(f"notified:reauth:{account_id}", "1", nx=True, ex=REAUTH_NOTIFY_TTL):
        return
    await publish_event(
        redis,
        "account:needs_reauth",
        {"account_id": str(account_id), "title": title},
        audience="admin",
    )


# -------------------------------------------------------------------- задача


@with_job_scope
async def deliver_message(ctx: dict[str, Any], message_id: uuid.UUID) -> None:
    """Доставить одно исходящее сообщение в Авито (08 §3)."""
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    attempt = int(ctx.get("job_try") or 1)

    # --- Фаза 1: прочитать и проверить (короткая транзакция) ---
    # Всё, что нужно дальше, снимаем простыми значениями: rollback после
    # рефреша токена истекает ORM-атрибуты, а ленивая подгрузка в async-коде
    # падает MissingGreenlet.
    # ⚠ ФАЗА 1 ЗАКРЫТА ПОВТОРОМ, ФАЗА 3 — НЕТ, И ЭТО РАЗНИЦА ПО СУЩЕСТВУ.
    #
    # ЧТО БЫЛО (разбор 03.09). Ошибки Авито в фазе 2 разобраны подробно и
    # честно повторяются. А фаза 1 — обычное чтение из базы — не была закрыта
    # ничем: заминка базы на секунду, и ARQ помечал задачу проваленной. Повтора
    # у неё нет: `max_tries` действует только на `Retry`, обычное исключение —
    # это конец (проверено по исходнику arq/worker.py). Сообщение навсегда
    # оставалось `pending`: ответ оператора клиенту не ушёл, и не узнал об этом
    # никто — сторож недоставленных смотрит на `failed`, а сторож «клиент ждёт»
    # молчит, потому что нажатие «Отправить» уже погасило ожидание. Ровно та
    # тихая пропажа, про которую написано ниже в фазе 2.
    #
    # ПОЧЕМУ ФАЗУ 3 ТАК ЗАКРЫВАТЬ НЕЛЬЗЯ. Повтор задачи начинается с фазы 1 и
    # проходит фазу 2 заново — клиент получит сообщение ВТОРОЙ раз. В момент
    # падения фазы 3 `external_message_id` ещё не сохранён, и погасить дубль
    # нечем. Поэтому признак фазы: повторяем только то, что повторять безопасно.
    фаза = 1
    try:
        async with factory() as db:
            async with db.begin():
                msg = await _lock_message(db, message_id)
                if msg is None or msg.delivery_status != "pending":
                    return  # уже delivered/failed — гасим дубль джобы
                conv = await db.get(Conversation, msg.conversation_id)
                if conv is None:
                    return
                conversation_id, chat_id = conv.id, conv.external_chat_id
                account = await db.get(AvitoAccount, conv.account_id)
                blocked = account is None or account.status != "active"
                account_id = account.id if account else None
                account_title = account.title if account else ""
                parts = split_text(msg.body or "")
                attachments = list(msg.attachments or [])

            фаза = 2
            if blocked:
                if account_id is not None:
                    await _notify_admins_reauth(redis, account_id, account_title)
                await _fail(ctx, message_id, REAUTH_ERROR)
                return
            assert account is not None and account_id is not None

            # ⚠ ЧТО АВИТО ПРИНИМАЕТ ОТ НАС. Изображения — да, двумя ручками
            # (`uploadImages` + `messages/image`). Произвольный файл — нет:
            # метода в каталоге нет вовсе (docs/26-AVITO-API-CATALOG.md,
            # строки 49 и 63). Значит правило одно на весь проект: картинку
            # отправляем клиенту, всё прочее — только в заметку внутри команды.
            картинки = [a for a in attachments if isinstance(a, dict) and a.get("kind") == "image"]
            прочие = [
                a for a in attachments if not (isinstance(a, dict) and a.get("kind") == "image")
            ]
            if прочие:
                # РАНЬШЕ ЗДЕСЬ ТЕРЯЛОСЬ. Сообщение без текста честно падало, а
                # «текст + файл» уходило как один текст: файл исчезал с записью в
                # лог, а оператор видел галочку «доставлено». Оператор прикладывал
                # фото детали с подписью «нужна вот такая» — клиент читал подпись
                # без фото и не понимал, о чём речь; оператор был уверен, что всё
                # ушло. Молчаливая потеря хуже отказа: отказ видно сразу.
                #
                # Поэтому решает НАЛИЧИЕ неотправляемого вложения, а не отсутствие
                # текста. Отправить половину сообщения — не доставка.
                log.warning(
                    "message.attachments_not_forwarded",
                    message_id=str(message_id),
                    count=len(прочие),
                    had_text=bool(parts),
                    had_images=bool(картинки),
                )
                await _fail(ctx, message_id, ATTACHMENTS_ERROR)
                return
            if not parts and not картинки:
                # Ни текста, ни вложений — отправлять нечего. Ветка защитная:
                # ручка отправки пустое сообщение не принимает.
                await _fail(ctx, message_id, ATTACHMENTS_ERROR)
                return

            # --- Фаза 2: поход в Авито — строго ВНЕ транзакции (08 §8.1) ---
            try:
                # Из базы, а не из кэша процесса. Воркер — отдельный контейнер: его
                # кэш не обновляется ничем, и после переключения на боевой Авито он
                # продолжал бы отправлять ответы клиентов в имитатор, показывая
                # «доставлено».
                client = await _OutboundClient.fresh(db)
                токен = _ТокенАккаунта(db, redis, account)
                прогресс = await _load_progress(
                    redis, message_id, parts_total=len(parts), images_total=len(картинки)
                )
                # ⚠ ПОРЯДОК: СНАЧАЛА ПРЕДМЕТ, ПОТОМ ПОДПИСЬ. Текст при фото —
                # это подпись («нужна вот такая деталь»), и подпись, пришедшая
                # раньше предмета, заставляет клиента гадать, о чём речь: он
                # читает «нужна вот такая», смотрит в пустой чат и переспрашивает.
                # Обратный порядок ничего не стоит и снимает вопрос целиком.
                if картинки:
                    await _send_images(
                        client, redis, токен, account, chat_id, картинки, message_id, прогресс
                    )
                if parts:
                    await _send_parts(
                        client, redis, токен, account, chat_id, parts, message_id, прогресс
                    )
                external_id = прогресс.external_id
            except _ФайлПотерян as exc:
                # Повторять нечего: файл повтором не появится. Сразу красное с
                # понятным текстом — оператор приложит файл заново.
                log.warning(
                    "message.attachment_file_missing",
                    message_id=str(message_id),
                    error=str(exc),
                )
                await db.rollback()  # `_fail` работает своей сессией
                await _fail(ctx, message_id, MEDIA_MISSING_ERROR)
                return
            except RateLimited as exc:
                # У ЛИМИТА ТОЖЕ ЕСТЬ ДНО, И РАНЬШЕ ЕГО НЕ БЫЛО.
                #
                # Ветка повторяла безусловно, а соседняя (5xx, ниже) считает
                # попытки и на исходе бюджета зовёт `_fail`. Асимметрия стоила
                # дорого: ARQ на `job_try > max_tries` обрывает задачу, НЕ ВЫЗЫВАЯ
                # тело функции, — значит `_fail` не звался никогда. Сообщение
                # оставалось `pending` навсегда: оператор видел его отправленным,
                # клиент не получал ничего, уведомления не было, а отметка «клиент
                # ждёт» к этому времени уже снята.
                #
                # `RateLimited` — подкласс `AvitoApiError`, но перехватывается
                # выше, поэтому в защищённую ветку он не проваливался.
                outcome = "retry" if attempt < MAX_TRIES else "failed"
                delay = max(exc.retry_after, backoff(attempt))
                log.warning(
                    "message.deliver",
                    message_id=str(message_id),
                    attempt=attempt,
                    status_code=429,
                    outcome=outcome,
                    retry_in_sec=delay if outcome == "retry" else None,
                )
                if attempt >= MAX_TRIES:
                    await db.rollback()  # `_fail` работает своей сессией
                    await _fail(ctx, message_id, EXHAUSTED_ERROR)
                    return
                raise Retry(defer=delay) from exc
            except AvitoAuthError:
                # Сюда доходит только НАСТОЯЩИЙ отзыв доступа: аккаунт уже помечен
                # needs_reauth сервисом, и `_ТокенАккаунта` это перепроверил по базе.
                # Временная недоступность Авито при обновлении токена уходит ниже,
                # в ветку повторов, — иначе минутный сбой хоронил бы ответ клиенту
                # и поднимал админам тревогу на исправном канале.
                await db.rollback()  # _fail работает своей сессией — эту не держим
                await _notify_admins_reauth(redis, account_id, account_title)
                await _fail(ctx, message_id, REAUTH_ERROR)
                return
            except (httpx.HTTPError, AvitoApiError) as exc:
                refused = _refused_for_good(exc)
                if refused is not None:
                    # Отказ, который повтор не изменит, — сразу красным, без
                    # пяти попыток впустую (проверка 24.09).
                    log.warning(
                        "message.deliver",
                        message_id=str(message_id),
                        attempt=attempt,
                        status_code=getattr(exc, "status", None),
                        outcome="failed",
                        permanent=True,
                        error=str(exc),
                    )
                    await db.rollback()
                    await _fail(ctx, message_id, refused)
                    return
                outcome = "retry" if attempt < MAX_TRIES else "failed"
                log.warning(
                    "message.deliver",
                    message_id=str(message_id),
                    attempt=attempt,
                    status_code=getattr(exc, "status", None),
                    outcome=outcome,
                    error=str(exc),
                )
                if attempt >= MAX_TRIES:
                    await db.rollback()
                    await _fail(ctx, message_id, EXHAUSTED_ERROR)
                    return
                raise Retry(defer=backoff(attempt)) from exc
            except Retry:
                # Поднята ветками выше — пропускаем к ARQ нетронутой, иначе общий
                # улов ниже превратил бы законный повтор в отказ.
                raise
            except Exception as exc:
                # ⚠ ОБЩИЙ УЛОВ: БЕЗ НЕГО СООБЩЕНИЕ ВИСЛО «ОТПРАВЛЯЕТСЯ» НАВСЕГДА.
                #
                # Задача написана в расчёте на пять попыток с задержкой, но ARQ
                # повторяет ТОЛЬКО по `Retry` и `CancelledError`; всё остальное
                # закрывает job на первой же попытке. Три ветки выше ловят ошибки
                # Авито — а обрыв соединения с Postgres, рестарт Redis, таймаут
                # задачи и ошибка расшифровки токена не наследуются ни от
                # `httpx.HTTPError`, ни от `AvitoApiError`: они проваливались мимо
                # всех трёх, и `_fail` не звался никогда.
                #
                # Дальше это состояние было НЕВИДИМО целиком: красную метку диалогу
                # ставит `refresh_undelivered` по `delivery_status == "failed"`, а
                # тут «pending»; сторож «клиент ждёт» тоже молчит — `awaiting_since`
                # обнулено нажатием «Отправить». То есть ответ клиенту не ушёл, и
                # об этом не знает ни оператор, ни система.
                #
                # Повторяем на тех же условиях, что и ошибки Авито, а на исходе
                # бюджета помечаем неотправленным: пусть оператор увидит красное и
                # нажмёт «Повторить» — это несравнимо лучше тихой пропажи.
                outcome = "retry" if attempt < MAX_TRIES else "failed"
                log.warning(
                    "message.deliver",
                    message_id=str(message_id),
                    attempt=attempt,
                    outcome=outcome,
                    error=f"{type(exc).__name__}: {exc}",
                    unexpected=True,
                )
                if attempt >= MAX_TRIES:
                    await db.rollback()
                    await _fail(ctx, message_id, EXHAUSTED_ERROR)
                    return
                raise Retry(defer=backoff(attempt)) from exc

            # --- Фаза 3: зафиксировать успех ---
            # авто-рефреш токена в фазе 2 мог оставить открытую транзакцию —
            # закрываем её, иначе begin() ниже упадёт «transaction already begun»
            await db.rollback()
            # Заплатка строки собирается ВНУТРИ транзакции, а уезжает после неё —
            # то же правило, что и везде (08 §8.1).
            успех_patch: dict[str, Any] | None = None
            async with db.begin():
                msg = await _lock_message(db, message_id)
                if msg is None or msg.delivery_status != "pending":
                    return
                msg.delivery_status = "delivered"
                # эхо этого сообщения из вебхука отсечётся по author_id (решение
                # владельца №1), external_message_id нужен для reconciliation
                msg.external_message_id = external_id

                # ⚠ УСПЕХ ОБЯЗАН ПЕРЕСЧИТАТЬ СОСТОЯНИЕ ДИАЛОГА — ЗЕРКАЛО ПРОВАЛА.
                #
                # Обратная связь от диспетчеров 02.09: «чат прочитан, отвечен, но
                # тайминг висит 1 мин. Даже при обновлении страницы».
                #
                # Путь такой: нажали «Отправить» — отметка «клиент ждёт» погасла;
                # доставка сорвалась — ветка провала её ВЕРНУЛА (и правильно:
                # клиент ответа не получил); повторили — дошло. А погасить отметку
                # было больше некому: здесь её не трогали вовсе. Диалог оставался
                # вечно ждущим, и обновление страницы не помогало — так стояло на
                # сервере.
                #
                # То же и с пометкой «не доставлено»: провал её ставит, успех
                # обязан снять. Иначе строка остаётся красной после удавшегося
                # повтора.
                успех_conv = await db.get(Conversation, conversation_id)
                if успех_conv is not None:
                    await refresh_undelivered(db, успех_conv)
                    await restore_awaiting(db, успех_conv)
                    успех_patch = {
                        "undelivered": успех_conv.undelivered_at is not None,
                        # Имя поля — экранное (`waiting_since`), а не серверное:
                        # см. разбор у ветки провала ниже. Одна и та же ошибка
                        # здесь стоила бы того же — заплатка мимо строки.
                        "waiting_since": iso(status_dict.waiting_since(успех_conv)),
                    }
    except Retry:
        raise
    except Exception as exc:
        if фаза != 1:
            # Фазы 2 и 3 разбирают себя сами; сюда попадает только то, чего
            # они не ждали, и повторять это нельзя — см. шапку выше.
            raise
        outcome = "retry" if attempt < MAX_TRIES else "failed"
        log.warning(
            "message.deliver",
            message_id=str(message_id),
            attempt=attempt,
            outcome=outcome,
            error=f"{type(exc).__name__}: {exc}",
            phase=1,
            unexpected=True,
        )
        if attempt >= MAX_TRIES:
            await _fail(ctx, message_id, EXHAUSTED_ERROR)
            return
        raise Retry(defer=backoff(attempt)) from exc

    await publish_status(
        redis,
        conversation_id=conversation_id,
        message_id=message_id,
        delivery_status="delivered",
        conversation_patch=успех_patch,
    )
    log.info(
        "message.deliver",
        message_id=str(message_id),
        attempt=attempt,
        parts=len(parts),
        outcome="delivered",
    )
    # КОНЕЦ СЧАСТЛИВОГО ПУТИ, и без этой отметки след обрывался на предпоследнем
    # шаге: были «встало в доставку» и «не доставилось», а «дошло» — не было.
    # Разница читается наоборот: отсутствие строки выглядит как «застряло», и
    # разбор уходил бы искать поломку там, где всё сработало.
    trace.step(
        "trace.delivered",
        message_id=str(message_id),
        conversation_id=str(conversation_id),
        attempt=attempt,
        parts=len(parts),
    )
