"""HTTP-клиент к API Авито поверх httpx (DESIGN §8.1–8.3).

Все URL строятся от адреса из настроек приложения (``services/avito_app``,
владелец задаёт его из интерфейса) — в dev/тестах это
fake-avito (07 §2), в проде https://api.avito.ru. Никакой логики
«if test» — только конфиг базового URL.

Обработка статусов (одинаковая для всех методов, кроме token-эндпоинта):
- 401 и 403 -> AvitoAuthError — вызывающий контур делает один авто-рефреш
  (у Авито истёкший токен даёт именно 403);
- 429 -> RateLimited(retry_after из заголовка Retry-After, дефолт 5);
- прочие 4xx/5xx -> AvitoApiError.

Токены в логи не попадают (DESIGN §1.5): модуль логирует только метод,
путь и статус.
"""

from __future__ import annotations

import asyncio
from typing import Any, Self
from urllib.parse import urlencode

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.avito.errors import (
    AvitoApiError,
    AvitoAuthError,
    AvitoUnavailable,
    RateLimited,
    TokenRevokedError,
)
from app.services import avito_app

log = structlog.get_logger("app.integrations.avito")

TIMEOUT_SECONDS = 15.0  # DESIGN §8.1/8.2; fake-avito 'slow' (20 c) обязан ронять по таймауту
OAUTH_SCOPE = "messenger:read,messenger:write,user:read"
DEFAULT_RETRY_AFTER = 5
#: Потолок сна по `Retry-After`. Заголовок присылает ЧУЖАЯ сторона, и верить
#: ему без предела нельзя: ARQ обрывает задачу по `job_timeout` в 300 секунд, а
#: спящий обработчик всё это время держит соединение к базе — ровно тот путь, по
#: которому 02.09 выело пул и потерялось 105 вебхуков. Шестьдесят секунд — это
#: бюджет одного захода: не уложились, значит пусть отказ будет виден.
RETRY_AFTER_MAX = 60


def build_authorize_url(state: str) -> str:
    """URL страницы согласия OAuth (DESIGN §8.1).

    Адрес и ключ берём из настроек ПРИЛОЖЕНИЯ (`services/avito_app`), а не из
    `.env` напрямую: владелец задаёт их из интерфейса, и подключение первого
    боевого канала не должно требовать разработчика с доступом по ssh.
    """
    config = avito_app.current()
    # `safe=":,"` — чтобы права остались читаемыми: `messenger:read,messenger:write`,
    # а не `messenger%3Aread%2C…`. Функционально одно и то же, но заказчик
    # прислал настоящую ссылку Авито, и в ней они не закодированы; совпадение
    # с образцом снимает целый класс вопросов «а точно ли так?» в день
    # подключения боевого аккаунта.
    return (
        config.auth_url
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": config.client_id,
                "scope": OAUTH_SCOPE,
                "state": state,  # CSRF: одноразовый, Redis TTL 600 (08 §8.5)
            },
            safe=":,",
        )
    )


#: Ключ счётчика недоступности Авито. Час жизни намеренный: столько нужно,
#: чтобы отличить одиночный сбой сети от лежащей площадки, и не дольше —
#: вчерашняя авария не должна поднимать сегодняшнюю тревогу.
AVITO_DOWN_KEY = "avito:unreachable:1h"
AVITO_DOWN_TTL = 3600

#: Ключ счётчика ПОПЫТОК за тот же час.
#:
#: ⚠ ЗАЧЕМ ВТОРОЕ ЧИСЛО (боевой случай 28.08). Тревога считала только отказы и
#: срабатывала на пятом за час. А в бою за тот же час было 100 отказов ПРИ 285
#: удачных обращениях: канал работал, просто с рябью. Человеку при этом
#: приходило «перестают работать сверка, история, отправка ответов» — то есть
#: тревога описывала лежащую площадку там, где она стояла на ногах.
#:
#: Цена ложной тревоги в этом проекте измерена и записана в соседнем стороже:
#: «112 ложных тревог закрыли собой два реальных провала резервной копии, и
#: заметили их только неделю спустя». Поэтому отказы теперь называются долей от
#: попыток, а порог спрашивает и то и другое.
AVITO_TRIES_KEY = "avito:tries:1h"


async def _счётчик_часа(ключ: str, *, ещё: list[Any] | None = None) -> None:
    """+1 к часовому счётчику, СРОК СТАВИТСЯ ОДИН РАЗ — при рождении ключа.

    ⚠ ЭТА ФУНКЦИЯ ЗАМЕНИЛА ДВЕ, И ПРИЧИНА НЕ В ДУБЛИРОВАНИИ. В обеих стояло
    `incr` + безусловный `expire`, а это означает не «ключ живёт час», а «ключ
    живёт вечно, пока идёт трафик»: срок обновлялся на КАЖДОМ обращении. Замер
    в бою 05.09: за 30 секунд TTL ключа попыток вырос 3599 → 3600 (при честном
    часовом окне он падал бы до 3569), а само значение накопило 13 280 855 при
    реальной частоте около 158 обращений за полминуты.

    Цена — тревога «Авито не отвечает», которая не могла сработать НИКОГДА.
    Сторож сравнивает отказы за час с попытками за тот же час; когда ни один из
    двух счётчиков не истекает, «за час» превращается «за всё время работы», и
    доля размазывается по месяцам. Прошлый разбор (28.08) как раз добавил сюда
    знаменатель, чтобы рябь не поднимала ложную тревогу, — и получил вместо
    ложных тревог их полное отсутствие.

    ⚠ ПОЧЕМУ `SET 0 EX NX` + `INCR`, А НЕ `INCR` + `EXPIRE if n == 1`. Второе
    выглядит очевидным решением и оставляет щель: процесс, умерший между двумя
    командами, оставляет ключ БЕЗ СРОКА — навсегда. Это не гипотеза, такой ключ
    найден в бою: `ratelimit:avito:...` с TTL = -1 и номером окна на 8,8 суток
    старше текущего. Здесь обе команды идут одной транзакцией MULTI/EXEC, и
    ключ рождается уже со сроком: щели нет.
    """
    try:
        from app.core import redis as redis_mod

        r = redis_mod.get_client()
        pipe = r.pipeline()
        pipe.set(ключ, 0, ex=AVITO_DOWN_TTL, nx=True)
        pipe.incr(ключ)
        for команда in ещё or []:
            команда(pipe)
        await pipe.execute()
    except Exception:  # noqa: BLE001 — счётчик не стоит ни одного обращения клиента
        log.debug("avito.counter_failed", key=ключ)


async def _отметить_попытку() -> None:
    """Знаменатель доли отказов. Никогда не бросает — довод тот же, что ниже."""
    await _счётчик_часа(AVITO_TRIES_KEY)


async def _отметить_недоступность(причина: str) -> None:
    """Счётчик для сторожа: +1 к числу отказов и последняя причина.

    Никогда не бросает наружу. Этот код стоит на горячем пути каждого запроса
    к Авито, и падение Redis не имеет права превратиться в падение приёма:
    хуже недоступной площадки только недоступная площадь ПЛЮС наша ошибка.

    ⚠ ПРИЧИНА, В ОТЛИЧИЕ ОТ СЧЁТЧИКА, ОБНОВЛЯЕТ СВОЙ СРОК, И ЭТО НАМЕРЕННО.
    Счётчику нужно фиксированное окно — иначе «за час» перестаёт что-либо
    значить. А причина всегда одна, последняя: тревога называет её человеку, и
    вчерашняя причина при сегодняшних отказах была бы прямым враньём.
    """
    await _счётчик_часа(
        AVITO_DOWN_KEY,
        ещё=[lambda pipe: pipe.setex(f"{AVITO_DOWN_KEY}:why", AVITO_DOWN_TTL, причина[:200])],
    )


#: Пределы общего пула соединений к Авито (`http_client`).
#:
#: Двадцать тёплых соединений на процесс: сверка канала — это около восьми
#: запросов подряд, разом идут не больше пяти сверок (разнос по времени в
#: `scheduler/main.py::enqueue_reconcile_all`), плюс доставки и история.
#: Потолок в пятьдесят — по числу задач воркера (`ARQ_MAX_JOBS`, 50): доставка,
#: сверка и догрузка истории идут в одном процессе, и без потолка каждая задача
#: на пике держала бы своё соединение. Срок тишины у тёплого соединения —
#: умолчание httpx (5 с).
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)

_http: httpx.AsyncClient | None = None
#: Цикл событий, в котором рождён `_http`, — см. `http_client`.
_http_loop: asyncio.AbstractEventLoop | None = None


def http_client() -> httpx.AsyncClient:
    """Один httpx-клиент на процесс — ради keep-alive.

    ⚠ ЧТО БЫЛО. `_request` открывал `httpx.AsyncClient` на КАЖДЫЙ поход в Авито
    и тут же закрывал: новое TCP-соединение и новое TLS-рукопожатие на каждый
    запрос. Замер с боевого хоста 06.09: первый запрос к api.avito.ru
    0,168–0,180 с, по уже открытому соединению 0,041–0,045 с — около 0,13 с
    уходило на рукопожатие. При доставке с p50 0,33 с это больше трети времени
    до галочки «доставлено»; сверка всех каналов делала ~276 рукопожатий за
    порыв.

    ⚠ КЛИЕНТ ПРИВЯЗАН К ЦИКЛУ СОБЫТИЙ. Соединения пула — сокеты того цикла, в
    котором их открыли; из другого цикла они дают «attached to a different
    loop». В бою цикл у процесса один, а в тестах — новый на каждый тест
    (pytest-asyncio, loop_scope=function). Поэтому здесь запоминается
    цикл-владелец, и при смене цикла клиент пересоздаётся. Старый при этом не
    закрываем: его цикл уже мёртв, `aclose()` из чужого цикла упал бы сам, а
    сокеты умерли вместе с циклом.

    Закрытый (`close_http_client`) тоже пересоздаётся: задача, догоняющая
    остановку воркера, получает рабочий клиент, а не `RuntimeError`.
    """
    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http.is_closed or _http_loop is not loop:
        _http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, limits=HTTP_LIMITS)
        _http_loop = loop
    return _http


async def close_http_client() -> None:
    """Закрыть общий клиент при остановке процесса (воркер:
    `workers/main.py::shutdown`). Повтор и вызов без клиента безвредны — как у
    `redis_mod.close_client`.

    Закрываем только из своего цикла: клиент чужого, уже закрытого цикла просто
    отпускается — довод тот же, что в `http_client`.
    """
    global _http, _http_loop
    if _http is None:
        return
    клиент, цикл = _http, _http_loop
    _http, _http_loop = None, None
    if цикл is asyncio.get_running_loop():
        await клиент.aclose()


class AvitoClient:
    """Тонкий клиент: HTTP и коды ошибок. Rate-limit бюджет (08 §4.3)
    и авто-рефреш на 401/403 — забота вызывающих контуров."""

    def __init__(self, base_url: str | None = None) -> None:
        # Адрес берём из настроек приложения: переключение «имитатор ↔ боевой
        # Авито» — это ровно смена этого адреса, и делать её должен владелец
        # из интерфейса, а не инженер в файле на сервере.
        #
        # ЗДЕСЬ ЧИТАЕТСЯ КЭШ ПРОЦЕССА, А НЕ БАЗА. Если рядом есть сессия —
        # пользуйтесь :meth:`fresh`, а не этим конструктором (см. её описание:
        # именно на этом расхождении система сутки ходила в имитатор, пока
        # экран показывал «настоящий Авито»).
        self._base = (base_url or avito_app.current().api_base).rstrip("/")

    @classmethod
    async def fresh(cls, db: AsyncSession, base_url: str | None = None) -> Self:
        """Клиент с настройками, ПЕРЕЧИТАННЫМИ ИЗ БАЗЫ.

        ЧТО СЛУЧИЛОСЬ БЕЗ ЭТОГО. Владелец нажал «Переключить на настоящий
        Авито» — строка в базе сменилась, экран настроек честно показал
        «боевой Авито». Но подключение аккаунта, доставка сообщений и обновление
        токена создавали клиент обычным конструктором, а он читает кэш ПРОЦЕССА,
        который никто не обновил. Система продолжала ходить в имитатор: каждая
        попытка привязки возвращала один и тот же выдуманный аккаунт, и владелец
        писал «ничего не привязывается». Хуже всего было то, что интерфейс при
        этом не врал по своим данным — он читал базу, а код читал кэш.

        ПРАВИЛО, КОТОРОЕ ИЗ ЭТОГО СЛЕДУЕТ: есть сессия — есть `fresh(db)`.
        Оно проверяется тестом-стражем (tests/unit/test_avito_client_freshness.py),
        а не только этим текстом.

        Кэш при этом остаётся: он нужен фоновым местам, где сессии нет вовсе,
        и живёт полминуты (:data:`avito_app.CACHE_TTL_SECONDS`).
        """
        await avito_app.ensure_fresh(db)
        return cls(base_url)

    # --- низкоуровневое ---

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        # Попытку считаем ДО похода, а не после удачного ответа: знаменатель
        # обязан включать и то обращение, которое сейчас провалится, иначе доля
        # отказов при полностью лежащей площадке считалась бы от нуля.
        await _отметить_попытку()
        try:
            # Общий клиент процесса, а не новый на запрос: разбор и замер — в
            # `http_client`.
            resp = await http_client().request(
                method,
                f"{self._base}{path}",
                headers=headers,
                params=params,
                json=json,
                data=data,
            )
        except httpx.HTTPError as exc:
            # ЕДИНСТВЕННОЕ место, где сетевой сбой превращается в нашу ошибку.
            # Раньше он выходил наружу голым httpx-исключением, а вызывающий
            # код ловил `OSError` — от которого httpx не наследуется. В итоге
            # «Авито недоступен» приезжало человеку как «Внутренняя ошибка
            # сервера». Подробности — в AvitoUnavailable.
            # ⚠ ПРИЧИНУ НАЗЫВАЕМ ВСЕГДА (аудит 19.08, находка L-002). У половины
            # сетевых исключений httpx пустой `str(exc)`: ConnectError, ReadError и
            # ReadTimeout приходят без аргумента. В журнале боя за сутки лежало
            # восемь строк `"error": ""` — авария названа, причина нет, и разбирать
            # её было нечем. Имя класса отвечает на главный вопрос: не дозвонились,
            # оборвалось на чтении или вышло время.
            причина = str(exc) or type(exc).__name__
            log.warning(
                "avito.unreachable",
                method=method,
                path=path,
                error=причина,
                error_kind=type(exc).__name__,
            )
            # Счётчик для сторожа: журнал контейнера читает человек, а тревогу
            # обязана поднимать машина. Ключ живёт час — этого хватает, чтобы
            # отличить одиночный сбой от лежащего Авито, и он не переживает
            # перезапуск дольше собственного смысла.
            await _отметить_недоступность(причина)
            # ⚠ «ЗАПРОС УШЁЛ» — ЭТО НЕ ТОЛЬКО ТАЙМАУТ (аудит 30.08).
            #
            # Признак решает, поставит ли доставка ключ сомнения: с ним
            # следующая попытка сверяет хвост чата и не шлёт клиенту второй раз
            # то, что уже дошло (L-007). Здесь стояло «таймаут, кроме таймаута
            # соединения» — и мимо проходили `ReadError` и `RemoteProtocolError`:
            # это обрыв соединения ПОСЛЕ того, как тело запроса ушло, на чтении
            # ответа. Прокси Авито рвёт так регулярно. Сомнение не ставилось,
            # повтор шёл вслепую — и клиент получал одно и то же сообщение
            # дважды. Отозвать его в Авито нельзя.
            #
            # Правило теперь по сути, а не по имени класса: «тело запроса
            # успело уйти в сокет». Не ушло — соединение не установилось
            # (`ConnectError`, `ConnectTimeout`) или оборвалось на записи
            # (`WriteError`, `WriteTimeout`); всё прочее из транспортных сбоев
            # приходится на время, когда запрос уже у Авито.
            #
            # ⚠ ОСТОРОЖНАЯ СТОРОНА — СЧИТАТЬ, ЧТО УШЁЛ. Лишняя сверка хвоста
            # стоит одного запроса; пропущенная — второго сообщения клиенту.
            НЕ_УШЁЛ = (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.WriteError,
                httpx.WriteTimeout,
                httpx.UnsupportedProtocol,
                httpx.InvalidURL,
            )
            ушёл = not isinstance(exc, НЕ_УШЁЛ)
            raise AvitoUnavailable(after_send=ушёл) from exc
        log.debug("avito.request", method=method, path=path, status=resp.status_code)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.status_code < 400:
            return
        # 403 — тоже «токен истёк»: у Авито это его код, а не 401 (см. разбор
        # в AvitoAuthError). Без этой строки каждый запрос после истечения
        # токена падал бы до планового обновления.
        if resp.status_code in (401, 403):
            raise AvitoAuthError(status=resp.status_code)
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", DEFAULT_RETRY_AFTER))
            except ValueError:
                retry_after = DEFAULT_RETRY_AFTER
            # Отрицательное и запредельное — не ошибка чужой стороны, а наша
            # уязвимость: см. RETRY_AFTER_MAX.
            raise RateLimited(max(0, min(retry_after, RETRY_AFTER_MAX)))
        raise AvitoApiError(f"Авито: {context} -> HTTP {resp.status_code}", status=resp.status_code)

    @staticmethod
    def _json(resp: httpx.Response, context: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AvitoApiError(f"Авито: {context} — ответ не JSON") from exc
        if not isinstance(payload, dict):
            raise AvitoApiError(f"Авито: {context} — ожидался JSON-объект")
        return payload

    # --- OAuth (DESIGN §8.1) ---

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """authorization_code -> первая пара токенов
        {access_token, refresh_token, expires_in, ...}."""
        resp = await self._request(
            "POST",
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": avito_app.current().client_id,
                "client_secret": avito_app.current().client_secret,
            },
        )
        self._raise_for_status(resp, "обмен кода")
        return self._validate_token_pair(self._json(resp, "обмен кода"), "обмен кода")

    async def client_credentials_token(
        self, client_id: str | None = None, client_secret: str | None = None
    ) -> dict[str, Any]:
        """Токен приложения — самая дешёвая проверка «ключи верны».

        Отвечает на пару client_id/client_secret и не требует ни согласия
        пользователя, ни подключённого аккаунта. Нужен ровно для кнопки
        «Проверить связь» на экране настроек: пусть ошибка в ключах всплывёт
        там, а не посреди OAuth, где человек уже ушёл на сайт Авито и вернулся
        с отказом без объяснения.
        """
        # Ключи можно передать явно — так подключается аккаунт СВОЕЙ парой,
        # ещё до того как он существует у нас в базе. Без аргументов берутся
        # общие: ими же проверяется связь на экране настроек.
        config = avito_app.current()
        resp = await self._request(
            "POST",
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id or config.client_id,
                "client_secret": client_secret or config.client_secret,
            },
        )
        self._raise_for_status(resp, "проверка ключей приложения")
        return self._json(resp, "проверка ключей приложения")

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """grant_type=refresh_token. Refresh у Авито одноразовый: 400 ->
        TokenRevokedError (сгорел или доступ отозван) — решает сервис."""
        resp = await self._request(
            "POST",
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": avito_app.current().client_id,
                "client_secret": avito_app.current().client_secret,
            },
        )
        if resp.status_code == 400:
            raise TokenRevokedError()
        self._raise_for_status(resp, "refresh токена")
        return self._validate_token_pair(self._json(resp, "refresh токена"), "refresh токена")

    @staticmethod
    def _validate_token_pair(payload: dict[str, Any], context: str) -> dict[str, Any]:
        for field in ("access_token", "refresh_token", "expires_in"):
            if not payload.get(field):
                raise AvitoApiError(f"Авито: {context} — в ответе нет поля {field!r}")
        return payload

    # --- профиль / вебхук ---

    async def get_self(self, access_token: str) -> dict[str, Any]:
        """GET /core/v1/accounts/self -> {id, name, ...}; id обязателен."""
        resp = await self._request("GET", "/core/v1/accounts/self", token=access_token)
        self._raise_for_status(resp, "профиль аккаунта")
        profile = self._json(resp, "профиль аккаунта")
        if not isinstance(profile.get("id"), int):
            raise AvitoApiError("Авито: профиль аккаунта без числового id")
        return profile

    async def register_webhook(self, access_token: str, url: str) -> None:
        """POST /messenger/v3/webhook — подписка на входящие (DESIGN §8.3)."""
        resp = await self._request(
            "POST", "/messenger/v3/webhook", token=access_token, json={"url": url}
        )
        self._raise_for_status(resp, "регистрация вебхука")

    async def list_subscriptions(self, access_token: str) -> list[dict[str, Any]]:
        """Кто СЕЙЧАС подписан на события этого аккаунта (#39).

        ЗАЧЕМ. На аккаунтах заказчика сегодня работает JivoChat, и главный
        неотвеченный вопрос переезда — что случится с её подпиской, когда
        подпишемся мы. Вариантов три: подписки сосуществуют, наша перебивает
        чужую, чужая перебивает нашу. От ответа зависит, можно ли вести пилот
        на двух-трёх диспетчерах параллельно с работающим Jivo.

        Метод ТОЛЬКО ЧИТАЕТ. Он и есть способ узнать ответ, ничего не сломав:
        видно и сколько подписок у аккаунта, и какие у них адреса.

        ПРО ГЛАГОЛ. В нашем каталоге методов он записан дважды и по-разному:
        в разделе «чего мы не используем» — как GET, в таблице, взятой из
        спецификации Авито, — как POST. Имитатор реализовал GET, то есть
        подтвердил нашу же догадку — ровно та беда, из-за которой мы уже
        дважды попали на боевом Авито (путь v2 вместо v3, 403 вместо 401).

        Поэтому здесь пробуются оба, начиная со СПЕЦИФИКАЦИИ: сперва POST, и
        только на «нет такого метода» — GET. Угадывать не приходится, а лишний
        запрос случается один раз и только если спецификация врёт.
        """
        resp = await self._request("POST", "/messenger/v1/subscriptions", token=access_token)
        if resp.status_code in (404, 405):
            resp = await self._request("GET", "/messenger/v1/subscriptions", token=access_token)
        self._raise_for_status(resp, "список подписок")
        payload = self._json(resp, "список подписок")
        items = payload.get("subscriptions")
        if not isinstance(items, list):
            raise AvitoApiError("Авито: ответ о подписках без списка `subscriptions`")
        return [item for item in items if isinstance(item, dict)]

    async def unregister_webhook(self, access_token: str, url: str) -> None:
        """Снятие вебхука при disable аккаунта (01 §4.5)."""
        resp = await self._request(
            "POST", "/messenger/v1/webhook/unsubscribe", token=access_token, json={"url": url}
        )
        self._raise_for_status(resp, "снятие вебхука")

    async def get_voice_urls(
        self, access_token: str, user_id: int, voice_ids: list[str]
    ) -> dict[str, str]:
        """GET /messenger/v1/accounts/{uid}/getVoiceFiles — ссылки на голосовые.

        ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ПОХОД. В сообщении Авито присылает только
        идентификатор голосового, самой записи там нет. Без этого запроса
        оператор видел строку «Голосовое сообщение» и не мог её послушать —
        клиент говорит, а мы не слышим (жалоба владельца 19.08). На бою таких
        сообщений 174.

        Ссылки ВРЕМЕННЫЕ и потому не хранятся: спрашиваем в момент, когда
        человек нажал «прослушать». Сохранённая в базе ссылка через час
        превратилась бы в битую и врала бы дважды — и про звук, и про то, что
        он у нас есть.
        """
        if not voice_ids:
            return {}
        resp = await self._request(
            "GET",
            f"/messenger/v1/accounts/{user_id}/getVoiceFiles",
            token=access_token,
            params={"voice_ids": ",".join(voice_ids)},
        )
        self._raise_for_status(resp, "ссылки на голосовые")
        payload = self._json(resp, "ссылки на голосовые")
        urls = payload.get("voices_urls")
        if not isinstance(urls, dict):
            raise AvitoApiError("Авито: ссылки на голосовые — поле 'voices_urls' не объект")
        return {str(k): str(v) for k, v in urls.items() if isinstance(v, str) and v}

    # --- чаты / история (backfill 08 §4.2, reconciliation 08 §4.1) ---

    async def get_chats(
        self,
        access_token: str,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 100,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /messenger/v2/accounts/{uid}/chats — страница сырых чатов."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if unread_only:
            params["unread_only"] = "true"
        resp = await self._request(
            "GET", f"/messenger/v2/accounts/{user_id}/chats", token=access_token, params=params
        )
        self._raise_for_status(resp, "список чатов")
        payload = self._json(resp, "список чатов")
        chats = payload.get("chats", [])
        if not isinstance(chats, list):
            raise AvitoApiError("Авито: список чатов — поле 'chats' не массив")
        return chats

    async def get_chat(self, access_token: str, user_id: int, chat_id: str) -> dict[str, Any]:
        """GET /messenger/v2/accounts/{uid}/chats/{chat_id} — один чат.

        ЗАЧЕМ ОТДЕЛЬНО ОТ ``get_chats``. Вебхук не несёт ни имени клиента, ни
        объявления — только числовой ``item_id``. Дотягивать карточку
        постраничным перебором всех чатов ради одного — дорого и ненадёжно:
        нужный уедет за вторую страницу, как только поток вырастет.

        Право то же, что у списка (``messenger:read``) — новых разрешений у
        Авито просить не нужно (каталог docs/26).
        """
        resp = await self._request(
            "GET",
            f"/messenger/v2/accounts/{user_id}/chats/{chat_id}",
            token=access_token,
        )
        self._raise_for_status(resp, "чат")
        return self._json(resp, "чат")

    async def get_chat_messages(
        self,
        access_token: str,
        user_id: int,
        chat_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /messenger/v3/accounts/{uid}/chats/{chat_id}/messages/ — страница
        сырых сообщений истории (07 §2.1).

        ВЕРСИЯ V3, А НЕ V2. Здесь стояла v2, и на боевом Авито это был бы 404:
        в каталоге API эндпоинта v2 для сообщений чата НЕТ вовсе — есть v1 на
        отправку и v3 на чтение. Не всплывало потому, что встроенный имитатор
        отвечал по тому пути, который мы у него же и спросили: имитатор,
        написанный по нашим догадкам, подтверждает наши догадки.

        Сломалось бы это на сверке пропущенных — то есть на механизме, который
        чинит потерянные сообщения. Причём молча: сверка ловит ошибки и идёт
        дальше, так что «клиент написал, а сообщение не появилось» осталось бы
        без объяснения.
        """
        resp = await self._request(
            "GET",
            f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
            token=access_token,
            params={"offset": offset, "limit": limit},
        )
        self._raise_for_status(resp, "история чата")
        # Импорт локальный: адаптер импортирует этот модуль, и обратная связь на
        # уровне модуля дала бы круговой импорт.
        from app.integrations.avito.adapter import _observe_message_shape

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AvitoApiError("Авито: история чата — ответ не JSON") from exc
        if isinstance(payload, list):  # Авито исторически отдаёт и голый массив
            _observe_message_shape(payload, откуда="v3.messages")
            return payload
        if isinstance(payload, dict):
            messages = payload.get("messages", [])
            if isinstance(messages, list):
                # ⚠ СТОРОЖ СТОИТ ЗДЕСЬ, А НЕ У ПОТРЕБИТЕЛЕЙ, И ЭТО ВАЖНО.
                #
                # Сначала он стоял в `normalize_history_message` — то есть у
                # ОДНОГО из читателей этой ручки. За полчаса боя он не записал
                # ни строки: разбор истории зовётся только при загрузке
                # переписки, а сверка и досылка ходят сюда своими путями.
                # Наблюдение, которое молчит, ничем не отличается от
                # отсутствующего.
                #
                # Здесь мимо не пройдёт никто: это единственное место, где
                # ответ ручки превращается в список сообщений.
                _observe_message_shape(messages, откуда="v3.messages")
                return messages
        raise AvitoApiError("Авито: история чата — не удалось найти массив сообщений")
