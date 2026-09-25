"""Ленивое обогащение диалога: имя клиента и карточка объявления.

Вебхук Мессенджера Авито v3 не несёт НИ ИМЕНИ КЛИЕНТА, НИ ОБЪЯВЛЕНИЯ — только
числовой ``item_id``, которого разбор не знает вовсе. В ленте из-за этого
пустая карточка, а в разборе диалогов колонка «Объявление» — сплошные прочерки
(критичный дефект аудита, docs/33 §14а). Тянуть это синхронно в
inbound-конвейере нельзя: доставка сообщения не должна ждать похода в чужой
API (08 §8.1 п.3). Поэтому — отдельная ARQ-задача:

    apply_inbound_event создал диалог без имени клиента ИЛИ без объявления
      -> enqueue enrich_client(conversation_id)   (после commit'а)
      -> воркер: GET чата в Авито -> clients.name + conversations.item_*
         -> WS conversation:updated

ПОЧЕМУ ОДНА ЗАДАЧА, А НЕ ДВЕ. Оба поля приезжают ОДНИМ запросом за карточку
чата, и парсер уже разбирает их вместе (``AvitoAdapter.parse_chat``): до
11 августа объявление он честно доставал и молча выбрасывал. Вторая задача
означала бы второй поход в чужой API за теми же данными.

Задача идемпотентна и безопасна к повторам: всё, что уже известно, не
перезаписывается; ошибку Авито логирует и молчит (следующий вебхук или сверка
попробуют снова).

ГОРОД (требование владельца от 11 августа). Города Авито не отдаёт ни в
вебхуке, ни в карточке чата — он лежит в ССЫЛКЕ на объявление. Поэтому здесь
же, разбором ссылки, без единого лишнего запроса: разбор чистый и работает
даже когда Авито недоступен, лишь бы ссылка уже была известна.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from arq import Retry
from redis.asyncio import Redis

from app.core.observability import with_job_scope
from app.integrations.avito.errors import AvitoApiError, AvitoAuthError, RateLimited
from app.integrations.avito.listing_url import city_by_slug, city_fields, parse_listing_url
from app.integrations.avito.ratelimit import AvitoRateLimiter
from app.models import AvitoAccount, Client, ClientAddressCandidate, Conversation
from app.models.client import CANDIDATE_PENDING
from app.services.geocode_queue import enqueue_geocode
from app.ws.events import publish_event

log = structlog.get_logger("app.client_enrich")

ENRICH_JOB = "enrich_client"
CHATS_SCAN_PAGES = 2  # свежий чат всегда в начале списка (сортировка по updated)
CHATS_PAGE = 100


def client_profile_url(client: Client | None) -> str | None:
    """Ссылка на публичный профиль клиента — то, что прислал Авито, или ``None``.

    ⚠ ЗДЕСЬ БЫЛА ЗАГЛУШКА, И ЕЁ ДОВОД СТОИТ СОХРАНИТЬ ЦЕЛИКОМ. Она возвращала
    `None` всегда, намеренно: собрать адрес было не из чего. Единственный
    идентификатор клиента у Мессенджера — числовой ``author_id`` (он же
    ``clients.external_id``), а публичная страница живёт по другому,
    непрозрачному ключу, и сопоставления «число → ключ» нет ни в каталоге
    методов (docs/26), ни в наших сырых вебхуках. Ссылка, собранная догадкой,
    ведёт в 404, а по ней жмут при клиенте, с телефона.

    ⚠ ЧТО ИЗМЕНИЛОСЬ 02.09 И ПОЧЕМУ ЭТО НЕ ДОГАДКА. Заглушка оставила после себя
    сторожа ``_log_peer_fields``, который писал в лог ИМЕНА полей собеседника, и
    он свою работу сделал: в бою у собеседника приходят
    ``["id", "name", "parsing_allowed", "public_user_profile"]``. Блок
    ``public_user_profile`` мы уже читаем — из него берётся аватар клиента
    (``adapter._peer_avatar_url``), и аватары в бою работают, значит блок
    приходит и заполнен.

    Адрес больше не ВЫВОДИТСЯ, а ЗАПОМИНАЕТСЯ: обогащение кладёт в
    ``clients.profile_url`` то, что дал Авито, — и только если это прошло
    проверку «https на домене Авито» (``adapter._peer_profile_url``). Здесь
    остаётся чтение сохранённого.

    Функция сохранена, хотя стала однострочной: она — единственное место, где
    записано, ПОЧЕМУ ссылки не было пять месяцев и на каком основании она
    появилась. Убрать её значит потерять этот довод вместе с ней.
    """
    return client.profile_url if client is not None else None


async def enqueue_enrich_client(redis: Redis, conversation_id: uuid.UUID) -> None:
    """Постановка задачи. Дедуп по ``_job_id`` — на один диалог одна задача."""
    from arq.connections import ArqRedis

    from app.services.messages import as_arq  # ARQ поверх уже открытого клиента Redis

    try:
        await ArqRedis.enqueue_job(
            as_arq(redis),
            ENRICH_JOB,
            conversation_id,
            # Имя дедуплицирует постановку: иначе поход в чужой API случался бы
            # на каждое входящее. Блокировку «на час после отказа» снимает не
            # имя, а `keep_result=0` при регистрации задачи — разбор там.
            _job_id=f"enrich:{conversation_id}",
        )
    except Exception:  # noqa: BLE001 — имя клиента не стоит упавшего вебхука
        log.warning("client_enrich.enqueue_failed", conversation_id=str(conversation_id))


def _log_peer_fields(raw: Any, account_user_id: int) -> None:
    """ИМЕНА полей собеседника из ``users[]`` — в лог, без единого значения.

    ЗАЧЕМ ЭТО ВООБЩЕ. Владелец просит ссылку на профиль клиента, а собрать её
    не из чего (см. ``client_profile_url``). Схемы массива ``users[]`` нет ни в
    каталоге docs/26, ни в спецификации Авито — там только пути и права. Гадать
    запрещено, спросить некого; значит система должна узнать сама: она уже
    ходит за карточкой чата, и достаточно записать, ЧТО в ответе лежит. Через
    сутки боевого трафика в логе будет точный список полей, и вопрос «есть ли
    там ссылка или хеш профиля» закроется фактом, а не мнением.

    ТОЛЬКО ИМЕНА, БЕЗ ЗНАЧЕНИЙ. В ``users[]`` лежат персональные данные живых
    людей — имя, аватар, возможно телефон. Логи хранятся и читаются глазами;
    складывать туда данные клиентов ради нашего любопытства нельзя. Имя поля
    отвечает на вопрос полностью, значение не добавляет ничего.

    Не бросает: диагностика не имеет права уронить обогащение.
    """
    try:
        if not isinstance(raw, dict):
            return
        users = raw.get("users")
        if not isinstance(users, list):
            log.info("client_enrich.peer_fields", users="нет массива users", chat=sorted(raw))
            return
        for user in users:
            if not isinstance(user, dict):
                continue
            uid = user.get("id")
            # Собеседник — тот, кто не мы. Ровно тот же признак, по которому
            # AvitoAdapter.parse_chat выбирает клиента.
            if isinstance(uid, int) and not isinstance(uid, bool) and uid != account_user_id:
                # ⚠ КЛЮЧИ БЛОКА ПРОФИЛЯ — ВТОРОЙ ВОПРОС, КОТОРЫЙ СТОРОЖ ЗАКРЫВАЕТ.
                #
                # Первый он уже закрыл: 02.09 в бою видно, что `public_user_profile`
                # у собеседника ЕСТЬ. Но форма адреса внутри взята из разбора
                # docs/44 §6 со ссылкой на спецификацию Авито, которой в
                # репозитории нет (docs/30 §B1), — то есть из источника, который
                # нечем проверить и который наш код однажды уже опроверг.
                #
                # Ключи отвечают на это фактом и стоят ровно столько же, сколько
                # имена полей выше: ни одного значения наружу. Разбор
                # (`adapter._peer_profile_url`) на форму не полагается, но знать
                # её нужно — иначе, если ссылка не появится, чинить будут вслепую.
                профиль = user.get("public_user_profile")
                ключи = sorted(профиль) if isinstance(профиль, dict) else type(профиль).__name__
                log.info("client_enrich.peer_fields", fields=sorted(user), profile_keys=ключи)
                return
    except Exception:  # noqa: BLE001 — запись в лог не стоит упавшей задачи
        log.debug("client_enrich.peer_fields_failed")


async def _fetch_chat_info(
    avito_user_id: int,
    chat_id: str,
    token_enc: bytes,
    limiter: AvitoRateLimiter,
    account_id: str,
) -> Any | None:
    """Карточка чата из API Авито: имя собеседника и объявление.

    Основной путь — ``GET /messenger/v2/accounts/{uid}/chats/{chat_id}``
    (контракт задачи). fake-avito такой ручки не отдаёт (в нём есть только
    список чатов), поэтому есть fallback на первые страницы списка — тот же
    парсер, тот же результат.
    """
    from app.integrations.avito.adapter import AvitoAdapter
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    client = AvitoClient()
    token = crypto.decrypt_token(token_enc)

    try:
        # ⚠ БЮДЖЕТ БЕРЁМ ПЕРЕД КАЖДЫМ ОБРАЩЕНИЕМ. Обогащение ходило в Авито мимо
        # лимитера — единственный боевой путь, который это делал, — и до трёх
        # раз на диалог. На выгрузке истории это десятки запросов в минуту сверх
        # бюджета, а платит за них доставка ответов клиентам: она делит с нами
        # то же окно (08 §4.3).
        #
        # Ведро `bulk`, а не `interactive`: интерактивная доля — это ответы
        # клиентам, и фоновому обогащению её есть нельзя.
        await limiter.acquire(account_id, bucket="bulk")
        get_chat = getattr(client, "get_chat", None)
        if get_chat is not None:  # появится в клиенте (чужая зона) — используем сразу
            raw = await get_chat(token, avito_user_id, chat_id)
        else:
            resp = await client._request(  # noqa: SLF001 — метода get_chat пока нет
                "GET",
                f"/messenger/v2/accounts/{avito_user_id}/chats/{chat_id}",
                token=token,
            )
            raw = resp.json() if resp.status_code < 400 else None
        if isinstance(raw, dict):
            _log_peer_fields(raw, avito_user_id)
            info = AvitoAdapter.parse_chat(raw, account_user_id=avito_user_id)
            if info.client_name or info.item_title:
                return info
    except (AvitoApiError, ValueError) as exc:
        log.debug("client_enrich.direct_chat_failed", error=str(exc))

    offset = 0
    for _ in range(CHATS_SCAN_PAGES):
        await limiter.acquire(account_id, bucket="bulk")
        chats = await client.get_chats(token, avito_user_id, offset=offset, limit=CHATS_PAGE)
        if not chats:
            return None
        for raw in chats:
            if not isinstance(raw, dict) or raw.get("id") != chat_id:
                continue
            _log_peer_fields(raw, avito_user_id)
            return AvitoAdapter.parse_chat(raw, account_user_id=avito_user_id)
        offset += len(chats)
    return None


def city_slug_of(item_url: str | None) -> str | None:
    """Слаг города из ссылки на объявление — для точек ЗАПИСИ ссылки.

    Отличается от :func:`_apply_city` тем, что не трогает объект: её зовут там,
    где строка диалога ещё только собирается (вставка из вебхука, дозапись
    сверкой). Одна функция на все точки записи — чтобы «где разбирается город»
    имело один ответ, а не четыре.
    """
    if not item_url:
        return None
    return parse_listing_url(item_url).city_slug


def _apply_city(conv: Conversation) -> dict[str, Any]:
    """Город из уже известной ссылки на объявление. Ни сети, ни запросов.

    ПОЧЕМУ ЗДЕСЬ, А НЕ В КОНВЕЙЕРЕ ВЕБХУКА. Ссылка приезжает вместе с
    карточкой чата, то есть в этой самой задаче; разбирать её где-то ещё
    значило бы завести второе место, знающее про города.

    ПИШЕМ ТОЛЬКО ПУСТОЕ. Заполненный слаг — снимок на момент обращения, и
    переписывать его нельзя даже если объявление переехало в другой город:
    иначе отчёт за прошлый квартал меняется задним числом (docs/32).

    Возвращает часть WS-патча (пустую, если менять нечего) — вызывающий
    доклеивает её к остальным полям объявления.
    """
    if conv.item_city_slug is not None or not conv.item_url:
        return {}
    slug = parse_listing_url(conv.item_url).city_slug
    if slug is None:
        return {}
    conv.item_city_slug = slug
    if city_by_slug(slug) is None:
        # СПРАВОЧНИК РАСТЁТ ПО ФАКТУ, А НЕ ПО НАШИМ ПРЕДСТАВЛЕНИЯМ О ГЕОГРАФИИ
        # ЗАКАЗЧИКА: списка городов, где он даёт объявления, у нас нет
        # (docs/32, unknown №8). Слаг сохраняем и показываем как есть, а эта
        # строка раз в неделю читается глазами и пополняет CITIES. Пишется
        # ровно один раз на диалог — на выдаче списка её нет, иначе лог
        # утонул бы в повторах.
        log.info(
            "listing.unknown_city_slug",
            slug=slug,
            conversation_id=str(conv.id),
            url=conv.item_url,
        )
    return city_fields(slug)


@with_job_scope
async def enrich_client(ctx: dict[str, Any], conversation_id: uuid.UUID) -> None:
    """ARQ-задача: дотянуть имя клиента, объявление и город, разослать обновление."""
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]

    # Фаза 1: что нужно для запроса — простыми значениями, сессия закрывается
    # ДО похода в Авито (08 §8.1 п.3: держать соединение пула на время
    # 15-секундного HTTP-вызова нельзя).
    async with factory() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            return
        client_row = await db.get(Client, conv.client_id)
        if client_row is None:
            return
        need_name = not client_row.name
        need_item = not conv.item_title
        # Фото добирается тем же походом, что имя: отдельного запроса у Авито
        # под него нет, оно лежит в той же карточке чата.
        need_avatar = not client_row.avatar_url
        # ⚠ ССЫЛКА НА ПРОФИЛЬ — ПОВОД СХОДИТЬ, ДАЖЕ ЕСЛИ ВСЁ ОСТАЛЬНОЕ ИЗВЕСТНО
        # (просьба владельца 02.09). У всех клиентов, заведённых до этой
        # правки, поля нет, а имя, объявление и аватар у них давно на месте —
        # то есть без этого условия ранний выход ниже сработал бы у КАЖДОГО из
        # них, и ссылка не появилась бы никогда. Это буквально та же беда, что
        # была с городом, и починена она так же.
        #
        # ⚠ И СПРАШИВАЕМ ОДИН РАЗ. Без отметки о попытке это условие означало бы
        # поход в чужой API на КАЖДОЕ входящее сообщение, вечно, — если Авито
        # ссылку не даёт. А он может не дать: форма поля взята из разбора и
        # первым лицом не проверена. Пустой ответ — тоже знание, и он
        # записывается наравне с найденной ссылкой.
        need_profile = not client_row.profile_url and client_row.profile_checked_at is None
        # ГОРОД ДОБИРАЕТСЯ ДАЖЕ КОГДА ХОДИТЬ В АВИТО НЕ ЗА ЧЕМ. Диалогов со
        # ссылкой, но без города, будет ровно два вида: заведённые до этой
        # выкатки и те, у кого объявление приехало прямо в вебхуке. Выйди мы
        # здесь по «всё уже известно», как раньше, — город у них не появился
        # бы никогда. Это буквально дефект 2 из docs/32, повторённый заново.
        city_patch = _apply_city(conv)
        if city_patch:
            await db.commit()
        if not need_name and not need_item and not need_avatar and not need_profile:
            if city_patch:
                await publish_event(
                    redis,
                    "conversation:updated",
                    {"conversation_id": str(conversation_id), "patch": {"item": city_patch}},
                )
                log.info("client_enrich.city_only", conversation_id=str(conversation_id))
            return  # остальное уже известно — задача идемпотентна
        account = await db.get(AvitoAccount, conv.account_id)
        if account is None or account.status != "active":
            return
        client_id = conv.client_id
        chat_id = conv.external_chat_id
        avito_user_id = account.avito_user_id
        token_enc = bytes(account.access_token_enc)
        # Идентификатор канала нужен лимитеру: бюджет к API Авито считается
        # ПОАККАУНТНО, и одного `avito_user_id` для ключа недостаточно.
        account_id = str(account.id)

    # Фаза 2: поход в Авито вне транзакции и вне сессии
    try:
        info = await _fetch_chat_info(
            avito_user_id, chat_id, token_enc, AvitoRateLimiter(redis), account_id
        )
    except RateLimited as exc:
        # ⚠ 429 БОЛЬШЕ НЕ ХОРОНИТ ЗАДАЧУ. `RateLimited` — подкласс
        # `AvitoApiError`, поэтому он попадал в общую ветку ниже и заканчивался
        # строкой в журнале: ни сна по `Retry-After`, ни повтора. Имя клиента и
        # объявление не появлялись никогда, а выглядело это на карточке как
        # «Авито не отдаёт данные», хотя Авито просто просил подождать.
        #
        # Ровно так же поступает доставка ответов (`workers/deliver`): ждём
        # столько, сколько попросили, и пробуем снова.
        log.warning(
            "client_enrich.rate_limited",
            conversation_id=str(conversation_id),
            retry_in_sec=exc.retry_after,
        )
        raise Retry(defer=exc.retry_after) from exc
    except (AvitoApiError, AvitoAuthError) as exc:
        log.warning("client_enrich.failed", conversation_id=str(conversation_id), error=str(exc))
        return
    if info is None:
        return

    # Фаза 3: записать (пока ходили, поля мог проставить кто-то другой)
    patch: dict[str, Any] = {}
    # ⚠ «ЧТО ЗАПИСАТЬ» И «ЧТО ПОКАЗАТЬ» — РАЗНЫЕ ВОПРОСЫ, И ЭТО СТОИЛО ДЕФЕКТА.
    #
    # Раньше их решал один `patch`: пусто — выходим не коммитя. Пока всё, что мы
    # пишем, было и содержимым карточки, это совпадало. Отметка «про профиль
    # спрашивали» первой нарушила совпадение: показывать её людям нечего, а
    # записать обязательно — иначе клиент без профиля гоняет нас в чужой API на
    # каждое своё сообщение, то есть ограничитель цены написан и не действует.
    записали = False
    # Город, найденный в фазе 1, едет ТЕМ ЖЕ кадром, что и всё остальное:
    # оператор не должен видеть, как карточка достраивается по частям.
    item_patch: dict[str, Any] = dict(city_patch)
    async with factory() as db:
        if need_name and info.client_name:
            fresh_client = await db.get(Client, client_id)
            # ⚠ `name_set_at` — «имя трогал человек». Проверка пустоты одна этого не
            # различает: очищенное диспетчером имя вернулось бы отсюда само, причём
            # молча и с задержкой в минуты, когда связать это с чем-либо уже нельзя.
            if (
                fresh_client is not None
                and not fresh_client.name
                and fresh_client.name_set_at is None
            ):
                fresh_client.name = info.client_name
                patch["client"] = {"name": info.client_name}
        if need_avatar and info.client_avatar_url:
            fresh_client = await db.get(Client, client_id)
            # Только в пустоту — как импорт истории: перезапись непустого
            # мигала бы картинкой при каждом обогащении. Отметки «трогал
            # человек» у фото нет, потому что руками его не ставят вовсе.
            if fresh_client is not None and not fresh_client.avatar_url:
                fresh_client.avatar_url = info.client_avatar_url
                patch.setdefault("client", {})["avatar_url"] = info.client_avatar_url
        if need_profile:
            fresh_client = await db.get(Client, client_id)
            if fresh_client is not None and fresh_client.profile_checked_at is None:
                # ⚠ ОТМЕТКА СТАВИТСЯ ДАЖЕ ПРИ ПУСТОМ ОТВЕТЕ, И ЭТО ГЛАВНОЕ
                # ЗДЕСЬ. Она означает «спрашивали», а не «нашли»: без неё
                # клиент, у которого профиля нет, гонял бы нас в Авито на каждое
                # своё сообщение до конца времён.
                fresh_client.profile_checked_at = datetime.now(UTC)
                записали = True
                # Саму ссылку — только в пустоту, как аватар: руками её не
                # ставят, а перезапись мигала бы ей при каждом обогащении.
                if info.client_profile_url and not fresh_client.profile_url:
                    fresh_client.profile_url = info.client_profile_url
                    patch.setdefault("client", {})["profile_url"] = info.client_profile_url
        if need_item and info.item_title:
            fresh_conv = await db.get(Conversation, conversation_id)
            # ТОЛЬКО ПУСТОЕ. Известное объявление не перезаписываем: у чата оно
            # одно и не меняется, а вот наши данные могли уточнить руками.
            if fresh_conv is not None and not fresh_conv.item_title:
                fresh_conv.item_title = info.item_title
                fresh_conv.item_url = info.item_url
                fresh_conv.item_price = info.item_price
                # ОБЪЯВЛЕНИЕ ЕДЕТ ВЛОЖЕННЫМ ОБЪЕКТОМ `item`, а не тремя полями
                # рядом. Так объявлен патч строки диалога (01 §11.3, на фронте
                # — ConversationPatch и mergeConversationPatch): вложенные
                # `client`, `account`, `item` сливаются частично, остальные
                # ключи кладутся в строку как есть. Плоские `item_title`,
                # `item_url`, `item_price`, стоявшие здесь раньше, тихо
                # оседали В КОРНЕ строки, а `item` оставался пустым: карточка
                # объявления не появлялась до перезагрузки страницы. Имя
                # клиента рядом уезжало правильно — оно с самого начала было
                # вложенным, и на его фоне беда выглядела как «объявление
                # приходит медленнее имени».
                item_patch.update(
                    {
                        "title": info.item_title,
                        "url": info.item_url,
                        "price": info.item_price,
                        **_apply_city(fresh_conv),
                    }
                )
        if item_patch:
            patch["item"] = item_patch
        if patch:
            записали = True
        if not записали:
            return
        # ГОРОД ПОЯВИЛСЯ — АДРЕСА ЭТОГО ДИАЛОГА ЖДУТ КАРТУ (бой 12.09: строка
        # проверялась в ту же секунду, что распознана, объявление приезжало
        # сюда мгновением позже — и «город объявления неизвестен» оставалось
        # навсегда, хотя в шапке уже стояло «Хабаровск»). Список — до commit'а,
        # постановка — после (класс «кадр до commit'а»).
        на_карту: list[uuid.UUID] = []
        if item_patch.get("url"):
            на_карту = list(
                (
                    await db.execute(
                        sa.select(ClientAddressCandidate.id).where(
                            ClientAddressCandidate.conversation_id == conversation_id,
                            ClientAddressCandidate.status == CANDIDATE_PENDING,
                            ClientAddressCandidate.geo_status.in_(("pending", "no_city")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        await db.commit()

    for candidate_id in на_карту:
        await enqueue_geocode(redis, candidate_id, suffix="city")

    # Кадр — только когда есть что показать. Отметка о вопросе к Авито людям
    # ничего не говорит, и лишний кадр заставил бы каждый открытый экран
    # перерисовать строку впустую.
    if not patch:
        return

    await publish_event(
        redis,
        "conversation:updated",
        {"conversation_id": str(conversation_id), "patch": patch},
    )
    log.info("client_enrich.done", conversation_id=str(conversation_id), filled=sorted(patch))
