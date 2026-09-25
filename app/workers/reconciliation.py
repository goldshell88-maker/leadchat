"""Reconciliation — страховка недоставленных вебхуков (08 §4.1, DESIGN §1.3).

Scheduler enqueues one ARQ job per active account (dedup by _job_id);
the worker polls fake-avito/Avito chats with unread_only=true and pushes
missing messages through the SAME ``apply_inbound_event`` — идемпотентно
(контракт INT-3): повторный прогон не создаёт ни дублей, ни повторных
событий.

The adapter comes from the OAuth zone (app.integrations.avito.adapter);
``get_adapter`` is the seam tests monkeypatch with a fake.

ГЛУБИНА И ДВЕ ДВЕРИ (требование владельца 11 августа, docs/41 §11). Нижней
границы по моменту подключения канала здесь больше нет: незнакомый чат
берётся с самого начала — «все диалоги, которые были и есть». Момент
подключения при этом никуда не делся, у него другая работа: сообщения новее
него идут ЖИВОЙ дверью (очередь, ожидание, бот, кадры в сокет), старше —
ИСТОРИЧЕСКОЙ (``apply_inbound_event(..., backfill=True)``: закрытый диалог,
без очереди и без бота). Без этой развилки снятая отсечка означала бы лавину
прошлогодних чатов у тринадцати операторов.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select

from app.core.observability import with_job_scope
from app.models import AvitoAccount, Conversation, Message
from app.scheduler.partitions import PartitionCoverage
from app.services.client_enrich import city_slug_of
from app.services.inbound import apply_inbound_event
from app.workers.inbound import ping_canary

log = structlog.get_logger("app.workers.reconciliation")


class _LiveAdapter:
    """Thin fetch layer over the OAuth zone's AvitoClient + static parsers
    (08 §4.1): pages of chats/history -> normalized ChatInfo/InboundEvent.

    ХОДИТ ЧЕРЕЗ ``AvitoAdapter._call``, А НЕ НАПРЯМУЮ — И ЭТО НЕ СТИЛЬ.
    ==================================================================
    Здесь стояло `self._client.get_chats(token, ...)` с токеном, расшифрованным
    ОДИН РАЗ на весь прогон. Что это дало на боевой системе (найдено в логах
    12 августа):

        AvitoAuthError: Авито: токен не принят (403)
        app/workers/reconciliation.py:137 -> fetch_chats -> get_chats

    Сверка — это страховка от НЕДОШЕДШИХ вебхуков. Она падала целиком, как
    только истекал access-токен: то есть переставала работать ровно тогда,
    когда единственная её задача — догнать потерянное. Прогон умирал, ARQ
    записывал исключение, и никто ничего не догонял до следующего часа (а если
    токен так и не обновился — и дальше).

    `_call` из адаптера делает ровно то, чего здесь не хватало: берёт бюджет
    ограничителя, спит на 429 по Retry-After и делает РОВНО ОДИН авто-рефреш на
    401/403 с повтором (DESIGN §8.2). Токен он расшифровывает на каждой попытке
    сам — поэтому обновлённый подхватывается сразу, а не остаётся протухшим до
    конца прогона.

    Отдельно про 403: у Авито истёкший access-токен даёт именно 403, а не 401
    (см. `AvitoAuthError` в integrations/avito/errors.py — там это разобрано и
    проверено). Так что ветка авто-рефреша здесь не теоретическая: это ровно
    тот случай, что мы поймали.
    """

    PAGE = 100

    def __init__(self, db_session_factory=None, redis=None) -> None:  # noqa: ANN001
        from app.integrations.avito.adapter import AvitoAdapter
        from app.integrations.avito.client import AvitoClient

        self._client = AvitoClient()
        # Адаптер нужен только ради `_call` — бюджета, сна на 429 и авто-рефреша.
        # Сессия и Redis обязательны: без них `_refresh_detached` не сможет
        # сохранить обновлённый токен и честно бросит AvitoAuthError — то есть
        # авто-рефреш будет объявлен, но работать не будет. Ровно такую
        # «объявленную, но не работающую» механику этот проект уже ловил.
        self._adapter = AvitoAdapter(db_session_factory, redis)

    async def _call(self, account, fn, /, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return await self._adapter._call(account, fn, *args, **kwargs)  # noqa: SLF001

    async def fetch_chats(self, account, *, unread_only: bool = True):  # noqa: ANN001, ANN201
        from app.integrations.avito.adapter import CHATS_MAX_OFFSET, AvitoAdapter
        from app.integrations.avito.errors import AvitoApiError, WebhookParseError

        offset = 0
        while True:
            try:
                chats = await self._call(
                    account,
                    self._client.get_chats,
                    account.avito_user_id,
                    offset=offset,
                    limit=self.PAGE,
                    unread_only=unread_only,
                )
            except AvitoApiError as exc:
                # Та же страховка, что у загрузки истории: 400 на дальней
                # странице — конец списка, а не поломка. На первой странице
                # 400 остаётся настоящей бедой и уходит наверх.
                if getattr(exc, "status", None) == 400 and offset > 0:
                    log.warning(
                        "avito.chats_page_refused",
                        account_id=str(account.id),
                        taken=offset,
                        error=str(exc),
                    )
                    return
                raise
            if not chats:
                return
            for raw in chats:
                # КРИВОЙ ЧАТ ПРОПУСКАЕТСЯ, ПРОГОН НЕ РВЁТСЯ.
                #
                # Здесь `parse_chat` звался голым. Разбор бросает
                # WebhookParseError на любой непривычной форме — например на
                # служебном сообщении без автора: какой у него author_id, проект
                # НЕ проверял (docs/30 §«Чего мы не знаем»), это признанная
                # догадка. Одна такая запись уносила ВЕСЬ прогон сверки, и это
                # не «пропустили один чат»:
                #
                #   * offset каждый час начинается с нуля, а чат остаётся в
                #     выборке unread_only — следующий прогон спотыкается о то же
                #     самое. Не «до перезапуска», а НАВСЕГДА;
                #   * сверка — единственная страховка от НЕДОШЕДШИХ вебхуков.
                #     Пока она лежит, пропавшее сообщение клиента не появится у
                #     оператора никогда, и никто об этом не узнает: в журнале
                #     только исключение ARQ;
                #   * заодно молчит `ping_canary` в конце прогона — ночью
                #     вебхуков нет, и канарейку кормит только сверка. Мёртвый
                #     канал в это время перестаёт быть заметен.
                #
                # Штатный `AvitoAdapter.fetch_chats` и первичная загрузка
                # истории пропускают кривой чат с warning ровно так же; сверка
                # была единственным местом, где эта конвенция нарушена.
                try:
                    chat = AvitoAdapter.parse_chat(raw, account_user_id=account.avito_user_id)
                except WebhookParseError as exc:
                    log.warning(
                        "reconcile.chat_skipped", account_id=str(account.id), error=str(exc)
                    )
                    continue
                yield chat
            if len(chats) < self.PAGE:
                return
            offset += len(chats)
            if offset >= CHATS_MAX_OFFSET:
                # Тот же потолок, что и у загрузки истории: глубже Авито отдаёт
                # HTTP 400, и сверка падала на нём КАЖДЫЙ прогон — то есть
                # страховки от недошедших вебхуков не было вовсе, пока в канале
                # больше тысячи чатов. Свежее лежит в начале списка, поэтому
                # обрыв на тысяче страховку почти не ослабляет.
                #
                # ⚠ INFO, А НЕ WARNING (замер боя 05.09). Строка описывает
                # СОСТОЯНИЕ канала — «непрочитанных больше тысячи», — а не
                # событие: у 17 каналов она повторялась КАЖДЫЙ прогон, раз в
                # пять минут, с одними и теми же полями taken=1000 limit=1000.
                # Это ~4 900 одинаковых предупреждений в сутки, и настоящие
                # (voice.failed, 3 % потока) в них тонули. Действия у человека
                # по ней нет — потолок наш и намеренный, — а предупреждение без
                # действия это шум. Не «раз в час через Redis»: 17 × 24 = 408
                # одинаковых строк в сутки всё равно втрое больше настоящих, и
                # ради одной строки журнала не стоит заводить ключ и лишний
                # заход в Redis. Факт из журнала не пропадает: те же поля, тот
                # же event, уровень INFO в бою включён (.env.prod.example).
                log.info(
                    "avito.chats_offset_limit",
                    account_id=str(account.id),
                    taken=offset,
                    limit=CHATS_MAX_OFFSET,
                )
                return

    async def fetch_history(self, account, chat, *, since=None):  # noqa: ANN001, ANN201
        from app.integrations.avito.adapter import AvitoAdapter
        from app.integrations.avito.errors import WebhookParseError

        offset = 0
        while True:
            messages = await self._call(
                account,
                self._client.get_chat_messages,
                account.avito_user_id,
                chat.external_chat_id,
                offset=offset,
                limit=self.PAGE,
            )
            if not messages:
                return
            for raw in messages:
                # ТА ЖЕ БЕДА НА СООБЩЕНИИ, И ОНА ДОРОЖЕ ЧАТА.
                #
                # Сверка теперь берёт незнакомый чат С САМОГО НАЧАЛА (отсечка по
                # моменту подключения снята, docs/41 §11) — то есть читает
                # переписку за годы, где вероятность встретить непривычную форму
                # тем выше, чем длиннее история. Одно такое сообщение роняло
                # прогон целиком: терялся не только этот чат, но и все
                # НЕДОШЕДШИЕ вебхуками сообщения остальных чатов — то, ради чего
                # сверка вообще существует.
                #
                # Пропускаем ровно сообщение: остальная история чата и сам
                # прогон продолжаются. Так же поступают штатный
                # `AvitoAdapter.fetch_history` и первичная загрузка истории.
                try:
                    event = AvitoAdapter.normalize_history_message(
                        raw, chat=chat, account_user_id=account.avito_user_id
                    )
                except WebhookParseError as exc:
                    log.warning(
                        "reconcile.message_skipped",
                        account_id=str(account.id),
                        chat_id=chat.external_chat_id,
                        error=str(exc),
                    )
                    continue
                if since is not None and event.created_at <= _aware(since):
                    continue
                yield event
            if len(messages) < self.PAGE:
                return
            offset += len(messages)


def _aware(dt):  # noqa: ANN001, ANN202
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def get_adapter(ctx: dict):  # noqa: ANN201 — seam: тесты подменяют фейком (INT-3)
    """Adapter factory. Raises ImportError while the OAuth zone is absent.

    Сессия и Redis берутся из контекста задачи и передаются адаптеру: без них
    авто-рефреш токена внутри `_call` не работает (см. шапку `_LiveAdapter`).
    """
    return _LiveAdapter(ctx.get("db_session_factory"), ctx.get("redis"))


@with_job_scope
async def reconcile_account(ctx: dict, account_id: UUID) -> dict:
    """ARQ job. Догоняет сообщения, не дошедшие вебхуками. Идемпотентна."""
    factory, redis = ctx["db_session_factory"], ctx["redis"]
    async with factory() as db:
        account = await db.get(AvitoAccount, account_id)
    if account is None or account.status != "active":
        return {"skipped": True}  # disabled/needs_reauth не опрашиваем (01 §4.5)

    try:
        adapter = get_adapter(ctx)
    except ImportError:
        log.warning("reconcile.adapter_unavailable", account_id=str(account_id))
        return {"skipped": True}

    chats_checked = recovered = imported = chats_failed = chats_old = 0
    # Один экземпляр на прогон: помнит уже обеспеченные месяцы, чтобы не
    # брать тяжёлую блокировку на каждое догнанное сообщение.
    coverage = PartitionCoverage()
    # ГРАНИЦА ЖИВОГО ТРАФИКА — момент подключения канала. Выше неё сообщение
    # «пропало и его ждут», ниже — «это история». Разница не косметическая:
    # см. развилку в конце цикла.
    live_since = _aware(account.created_at) if account.created_at else None
    # ⚠ НИЖНЯЯ ГРАНИЦА ОБХОДА — ПОЧЕМУ ЕЁ ЗДЕСЬ НЕ ХВАТАЛО (замер боя 08.09).
    #
    # За 25 минут журнала: 142 прогона, 83 813 обойдённых чатов, 601 с работы
    # воркера (39 % его времени) и messages_recovered=0. Из 142 прогонов 71
    # упёрся в потолок в 1000 чатов — то есть канал каждые пять минут
    # перебирал ВСЮ доступную переписку. Отсев «чат не менялся» стоял ПОСЛЕ
    # двух запросов в базу, поэтому от базы он не спасал вовсе: 167 626
    # запросов и 83 813 взятий сессии из пула за те же 25 минут. У проекта уже
    # была история, когда выеденный пул уронил приём вебхуков (105 потеряно).
    #
    # Страховка при этом не ослабевает: окно раздвигается само, если прошлый
    # полный проход был давно (см. `_нижняя_граница`).
    начало = datetime.now(UTC)
    граница_обхода = await _нижняя_граница(redis, account_id, начало)
    хвост = 0  # сколько чатов подряд оказались старше границы
    прервано_хвостом = False
    дно = None  # самая старая метка, до которой обход реально дошёл
    # GET /messenger/v2/accounts/{uid}/chats?unread_only=true (DESIGN §1.3)
    async for chat in adapter.fetch_chats(account, unread_only=True):
        chats_checked += 1
        chat_last = getattr(chat, "last_message_at", None)
        if chat_last is not None:
            дно = _aware(chat_last)
        # Чат без отметки времени судить нечем: он проходит дальше как обычный
        # и обнуляет счёт хвоста — иначе такой чат посреди старых копил бы
        # хвост, сам при этом ничего не подтверждая.
        if граница_обхода is not None and chat_last is not None:
            if _aware(chat_last) < граница_обхода:
                chats_old += 1
                хвост += 1
                if хвост >= ХВОСТ_ПОДРЯД:
                    прервано_хвостом = True
                    break
                continue
            хвост = 0
        else:
            хвост = 0
        # ⚠ ОТСЕВ «ЧАТ НЕ МЕНЯЛСЯ» — ДО ПОХОДА В БАЗУ, А НЕ ПОСЛЕ.
        #
        # Разбор самого отсева — в описании `_чат_без_изменений`. Здесь важно
        # только одно: раньше он стоял после выборки диалога и после
        # `max(created_at)` по входящим, и потому не экономил ни одного
        # запроса. Ровно эти два запроса и составляли 167 626 обращений к базе
        # за 25 минут при нуле находок. Дописывание объявления (оно было в том
        # же блоке) не страдает: неизменившийся чат уже проходил через него в
        # тот прогон, когда отметка ставилась, а любое новое входящее двигает
        # `last_message_at` и снова открывает дорогу в базу.
        if await _чат_без_изменений(redis, chat.external_chat_id, chat_last):
            continue
        async with factory() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.channel == "avito",
                        Conversation.external_chat_id == str(chat.external_chat_id),
                    )
                )
            ).scalar_one_or_none()
            # Отсечка — по последнему сообщению КЛИЕНТА, а не по
            # `conv.last_message_at`. Последнее двигает и наш исходящий
            # (services/messages.py), поэтому ответ менеджера, отправленный
            # после потерянного вебхука, задирал `since` выше пропущенного
            # входящего и прятал его от reconciliation НАВСЕГДА: клиент писал,
            # а сообщение не появлялось никогда (07 §5 сценарий 34).
            # Догоняем мы только входящие, эхо своих отбрасывает
            # apply_inbound_event, дедуп — по external_message_id, поэтому
            # лишний перезахват истории безопасен.
            # ОТСЕЧКИ ПО МОМЕНТУ ПОДКЛЮЧЕНИЯ БОЛЬШЕ НЕТ — ТРЕБОВАНИЕ ВЛАДЕЛЬЦА
            # ОТ 11 АВГУСТА (docs/41 §11).
            #
            # Здесь стояло `since = account.created_at`: переписку до
            # подключения канала решили не брать (8 августа: «аккаунты
            # меняются, старое никому не пригодится»). Требование обратное:
            # «подгрузить все диалоги, которые были и есть». Для незнакомого
            # чата граница снята — берём его с самого начала.
            #
            # ЧТО ОСТАЛОСЬ ОТ `created_at` И ПОЧЕМУ. Момент подключения из
            # границы ЗАГРУЗКИ стал границей ЖИВОГО ТРАФИКА (`live_since`
            # ниже): что было до него — история, что после — работа. Иначе
            # снятая отсечка превратила бы часовую сверку в раздачу
            # прошлогодней переписки тринадцати операторам, а метрики первого
            # ответа — в отчёт о том, как мы год не отвечали.
            since = None
            if conv is not None:
                last_in = (
                    await db.execute(
                        select(func.max(Message.created_at)).where(
                            Message.conversation_id == conv.id,
                            Message.direction == "in",
                        )
                    )
                ).scalar_one_or_none()
                # У знакомого диалога граница — его последнее входящее: всё
                # раньше уже лежит у нас, и перечитывать его незачем.
                if last_in is not None:
                    since = _aware(last_in)

                # ДОПИСЫВАЕМ ПУСТОЕ ОБЪЯВЛЕНИЕ СУЩЕСТВУЮЩЕМУ ДИАЛОГУ.
                #
                # Здесь была вторая половина критичного дефекта (docs/33 §14а):
                # сверка каждый час тянет чаты, парсер честно достаёт из них
                # название, ссылку и цену объявления — и всё это выбрасывалось,
                # потому что строка диалога уже существует. Во всём коде было
                # четыре присваивания item_title и НИ ОДНОГО обновления, так что
                # диалог, созданный вебхуком без объявления, оставался с
                # прочерком навсегда.
                #
                # Только там, где пусто: известное руками не затираем, и
                # идемпотентность прогона сохраняется.
                if conv.item_title is None and getattr(chat, "item_title", None):
                    # id забираем ДО commit'а: он гасит атрибуты ORM, и
                    # обращение к conv.id после него уходит в ленивую
                    # подгрузку, а в async-коде это MissingGreenlet. На этом
                    # уже спотыкались в services/avito_accounts (#28).
                    conv_id = conv.id
                    conv.item_title = chat.item_title
                    conv.item_url = getattr(chat, "item_url", None)
                    conv.item_price = getattr(chat, "item_price", None)
                    # Город — той же функцией, что и при вставке из вебхука:
                    # точка записи ссылки обязана проставлять и город, иначе
                    # он не появится вовсе (задача обогащения сюда не ходит).
                    conv.item_city_slug = city_slug_of(conv.item_url)
                    await db.commit()
                    log.info(
                        "reconcile.item_filled",
                        conversation_id=str(conv_id),
                        item_title=chat.item_title,
                    )
        # Быстрый отсев: последнее сообщение чата уже у нас — историю не качаем.
        # ChatInfo.last_message_at — всегда aware UTC; since из SQLite может быть
        # naive -> выравниваем, иначе TypeError в unit-окружении (на Postgres ок)
        #
        # ⚠ И ЗАПОМИНАЕМ СОСТОЯНИЕ ПРЯМО ЗДЕСЬ. Раньше этот `continue` уходил
        # мимо отметки, и такой чат каждый прогон снова стоил двух запросов в
        # базу — вечно, потому что отметке взяться было неоткуда. Ставить её
        # честно: сверять в чате нечего, мы только что это и установили.
        if (
            conv is not None
            and since is not None
            and chat_last is not None
            and chat_last <= _aware(since)
        ):
            await _запомнить_состояние(redis, chat.external_chat_id, chat_last)
            continue
        # ⚠ ОДИН ПЛОХОЙ ЧАТ НЕ УНОСИТ ПРОГОН КАНАЛА (28.08, боевой случай).
        #
        # В журнале прода: `102.51s ! reconcile:...failed, AvitoApiError: Авито:
        # история чата -> HTTP 503`. Отказ на ОДНОМ чате поднимался наружу и
        # убивал задачу целиком — вместе со всеми чатами, до которых она ещё не
        # дошла. А поскольку следующий прогон начинает список сначала и в том же
        # порядке, чаты за сбойным могли не проверяться НИКОГДА: страховка от
        # потерянных вебхуков переставала работать ровно там, где нужна.
        #
        # За сутки таких смертей 22 из 291 прогонов — почти каждый тринадцатый.
        #
        # Ловим широко и намеренно: причина у сорвавшегося чата бывает любая —
        # 503 площадки, вложение неизвестного вида, битая карточка. Ни одна из
        # них не повод бросить остальные девятьсот девяносто девять. Ровно та же
        # защита и теми же словами стоит в массовой загрузке (`_backfill_chat`);
        # здесь её просто не было.
        try:
            recovered, imported = await _reconcile_chat_history(
                factory,
                redis,
                adapter,
                account,
                chat,
                since=since,
                live_since=live_since,
                coverage=coverage,
                recovered=recovered,
                imported=imported,
            )
            # Прошли чат целиком и без отказа — запоминаем состояние. Ставим
            # ЗДЕСЬ, а не до вызова: иначе сорвавшийся на середине чат считался
            # бы сверенным и его пропущенные сообщения не догнались бы никогда.
            await _запомнить_состояние(redis, chat.external_chat_id, chat_last)
        except Exception:
            chats_failed += 1
            log.warning(
                "reconcile.chat_failed",
                account_id=str(account_id),
                external_chat_id=str(getattr(chat, "external_chat_id", "?")),
                exc_info=True,
            )

    # ⚠ ОТМЕТКА СТАВИТСЯ, ТОЛЬКО ЕСЛИ ПРОХОД ДЕЙСТВИТЕЛЬНО ВСЁ ЗАКРЫЛ.
    #
    # Она разрешает следующему прогону сузить обход до суток, поэтому цена
    # неверной отметки — молча непроверенный кусок списка. Достаточным считаем
    # ровно два случая:
    #   • обход оборван хвостом — значит до границы мы дошли и перешагнули её
    #     на целую страницу;
    #   • обход ушёл глубже суток (`дно`) — значит суточное окно следующего
    #     прогона заведомо внутри проверенного. Сюда попадает и первый прогон
    #     после выкатки: границы у него нет, он идёт до потолка Авито и на бою
    #     достаёт 44–63 дня назад.
    # Список короче суток и не оборванный хвостом отметки не даёт — и не надо:
    # такой канал обходится целиком и стоит копейки.
    #
    # Сорванный чат (`chats_failed`) отметку запрещает: он не сверен, его
    # состояние не запомнено, и окно обязано оставаться широким, пока он не
    # пройдёт.
    достаточно = прервано_хвостом or дно is None or дно <= начало - СВЕРКА_МИН_ОКНО
    if chats_failed == 0 and достаточно:
        await _запомнить_проход(redis, account_id, начало)

    log.info(
        "reconcile.run",  # контракт логов 05 §7.3
        account_id=str(account_id),
        chats_checked=chats_checked,
        chats_failed=chats_failed,
        chats_old=chats_old,
        window_from=граница_обхода.isoformat() if граница_обхода else None,
        messages_recovered=recovered,
        history_imported=imported,
    )
    await ping_canary(redis)  # ночью вебхуков нет — канарейку кормит reconciliation
    return {
        "chats_checked": chats_checked,
        "chats_failed": chats_failed,
        "chats_old": chats_old,
        "messages_recovered": recovered,
        "history_imported": imported,
    }


#: Насколько глубоко сверка заходит в список чатов при здоровой работе.
#:
#: ⚠ ЗАЧЕМ ГРАНИЦА ВООБЩЕ (замер боя 08.09). `unread_only=true` не сужает
#: список: мы нигде не помечаем чаты прочитанными у Авито, поэтому «непрочитан»
#: там навсегда. Замер двух боевых каналов по 1000 чатов: моложе суток 18 и 63
#: чата, старше тридцати дней — 569 и 179, самый старый 63 дня. То есть 94-98 %
#: обхода приходилось на переписку, в которой ничего не менялось неделями, и
#: каждый такой чат стоил двух запросов в базу КАЖДЫЕ ПЯТЬ МИНУТ.
#:
#: Сутки — не «на глаз»: прогон идёт каждые 300 с, значит внутри окна у сверки
#: 288 попыток догнать потерянный вебхук. Чтобы пропажа уехала за границу,
#: сверка обязана молчать сутки подряд.
СВЕРКА_МИН_ОКНО = timedelta(hours=24)

#: Нахлёст к границе, когда её задаёт прошлый успешный проход.
#:
#: Часы у нас и у Авито свои; метки чатов приходят с их стороны. Час запаса
#: стоит нескольких лишних чатов и снимает вопрос о расхождении часов.
СВЕРКА_НАХЛЁСТ = timedelta(hours=1)

#: Сколько чатов подряд ниже границы надо увидеть, прежде чем оборвать обход.
#:
#: ⚠ НЕ ОБРЫВ НА ПЕРВОМ ЖЕ СТАРОМ ЧАТЕ, И ЭТО НАМЕРЕННО. Обрыв держится на
#: том, что Авито отдаёт список от свежего к старому. На бою 08.09 это верно
#: (2000 чатов двух каналов, 0 нарушений убывания), но это ЧУЖОЙ порядок, и
#: молчаливая его смена выключила бы страховку целиком. Страница — 100 чатов,
#: так что терпимость в целую страницу стоит максимум одного лишнего GET и
#: ни одного запроса в базу: чат ниже границы отсеивается до похода в БД.
ХВОСТ_ПОДРЯД = 100

#: Сколько живёт отметка «канал сверен целиком по такое-то время».
#:
#: Дольше `СВЕРКА_МИН_ОКНО`: пока отметка жива, она РАСШИРЯЕТ окно после
#: простоя. Истекла — окно просто становится полным (обход до потолка), то
#: есть поведение до этой правки. Ошибка в эту сторону дешевле.
ПРОХОД_TTL = 30 * 24 * 3600


def _ключ_прохода(account_id: UUID) -> str:
    return f"reconcile:pass:{account_id}"


async def _нижняя_граница(redis, account_id: UUID, сейчас: datetime):  # noqa: ANN001, ANN202
    """До какого времени назад имеет смысл идти по списку чатов.

    Сутки — при здоровой работе. Если прошлый ПОЛНЫЙ проход был давно (воркер
    лежал, канал не отвечал, выкатка затянулась), окно раздвигается ровно на
    величину простоя: граница опускается до момента того прохода минус нахлёст.
    Отметки нет вовсе — границы нет, обход идёт как до правки, до потолка.
    """
    граница = сейчас - СВЕРКА_МИН_ОКНО
    try:
        было = await redis.get(_ключ_прохода(account_id))
    except Exception:  # noqa: BLE001 — Redis не повод сузить страховку
        return None
    if not было:
        return None
    if isinstance(было, bytes):
        было = было.decode()
    try:
        прошлый = _aware(datetime.fromisoformat(было))
    except ValueError:
        # Мусор в ключе — ведём себя как при его отсутствии, но не молча.
        log.warning("reconcile.pass_mark_broken", account_id=str(account_id))
        return None
    return min(граница, прошлый - СВЕРКА_НАХЛЁСТ)


async def _запомнить_проход(redis, account_id: UUID, начало: datetime) -> None:  # noqa: ANN001
    """Отметить, что канал сверен целиком по состоянию на `начало` прогона.

    ⚠ ИМЕННО НАЧАЛО, А НЕ КОНЕЦ. Прогон длится секунды, и сообщение, пришедшее
    во время него, могло не попасть в уже прочитанную страницу. Отметка по
    концу выкинула бы его из следующего окна.
    """
    try:
        await redis.set(_ключ_прохода(account_id), начало.isoformat(), ex=ПРОХОД_TTL)
    except Exception:  # noqa: BLE001 — не записали, значит следующее окно будет полным
        log.debug("reconcile.pass_not_stored", account_id=str(account_id))


#: Сколько живёт отметка «этот чат сверен в таком состоянии».
#:
#: Тридцать дней — с запасом: метка нужна ровно до следующего изменения чата, а
#: истёкшая означает одну лишнюю вычитку, не пропущенное сообщение. Дешевле
#: ошибиться в эту сторону.
СВЕРЕНО_TTL = 30 * 24 * 3600


def _ключ_сверки(chat_id: str) -> str:
    return f"reconcile:seen:{chat_id}"


async def _чат_без_изменений(redis, chat_id: str, chat_last) -> bool:  # noqa: ANN001
    """Сверяли ли мы этот чат ровно в этом состоянии.

    Сравниваем `last_message_at` чата с тем, что запомнили в прошлый успешный
    проход. Совпало — в чате с тех пор ничего не появилось, и вычитывать его
    историю незачем.

    ПОЧЕМУ ПОМНИМ СОСТОЯНИЕ, А НЕ ОТСЕЧКУ (замер боя 31.08). Отсев по
    последнему ВХОДЯЩЕМУ у нас не срабатывал НИ РАЗУ там, где последним в чате
    лежит не входящее: наш собственный ответ (а мы отвечаем постоянно), звонок
    `appCall` или служебное сообщение Авито — последние два мы у себя
    сообщениями не храним вовсе. `last_message_at` навсегда оставался выше
    отсечки, и чат перечитывался целиком каждые пять минут. Замер: воркер
    держал 102 % процессора непрерывно, за 10 минут журнала 33 440
    «наблюдений» звонков при 3 472 за всю неделю.

    СПРАШИВАЕМ У ЛЮБОГО ЧАТА, А НЕ ТОЛЬКО У ЗНАКОМОГО (вторая итерация той же
    починки). Чат, где лежат только звонки и служебные сообщения Авито,
    диалогом НЕ СТАНОВИТСЯ НИКОГДА (`inbound.avito_system_orphan`) — значит
    условие «есть диалог» вечно ложно, и именно такие чаты грузили сильнее
    всех.

    Отметки нет или Redis недоступен — отвечаем «изменился»: лишняя вычитка
    дешевле пропущенного сообщения клиента.
    """
    if chat_last is None:
        return False
    try:
        было = await redis.get(_ключ_сверки(chat_id))
    except Exception:  # noqa: BLE001 — Redis не повод не сверять
        return False
    if not было:
        return False
    if isinstance(было, bytes):
        было = было.decode()
    return было == chat_last.isoformat()


async def _запомнить_состояние(redis, chat_id: str, chat_last) -> None:  # noqa: ANN001
    """Запомнить состояние чата после успешной сверки."""
    if chat_last is None:
        return
    try:
        await redis.set(_ключ_сверки(chat_id), chat_last.isoformat(), ex=СВЕРЕНО_TTL)
    except Exception:  # noqa: BLE001 — не записали, значит сверим ещё раз
        log.debug("reconcile.seen_not_stored", chat_id=chat_id)


async def _reconcile_chat_history(
    factory,  # noqa: ANN001 — фабрика сессий из ctx, тип задаётся вызывающим
    redis,  # noqa: ANN001
    adapter,  # noqa: ANN001
    account: AvitoAccount,
    chat,  # noqa: ANN001
    *,
    since,  # noqa: ANN001
    live_since,  # noqa: ANN001
    coverage: PartitionCoverage,
    recovered: int,
    imported: int,
) -> tuple[int, int]:
    """История одного чата. Отдельной функцией — чтобы её отказ ловился
    поштучно, а не уносил прогон канала (см. развёрнутый довод у вызова)."""
    async for event in adapter.fetch_history(account, chat, since=since):
        # Та же беда, что и у загрузки истории (#24): для НОВОГО диалога
        # отсечки нет, и сверка читает переписку с самого начала — а
        # помесячные партиции `messages` живут вокруг сегодняшнего дня.
        # Одно сообщение годовой давности роняло сверку целиком, и тогда
        # переставали догоняться свежие пропущенные сообщения — то есть
        # чинилка ломалась ровно там, где нужна. Теперь, когда границы по
        # моменту подключения нет, это единственная защита.
        await coverage.ensure(event.created_at)
        # ДВЕ ДВЕРИ, А НЕ ОДНА.
        #
        # Сверка теперь берёт незнакомый чат целиком, с самого начала — и
        # заводить прошлогоднюю переписку тем же путём, что сегодняшнее
        # пропавшее сообщение, нельзя. Живая дверь ставит диалог в
        # очередь, ставит отметку ожидания, будит бота и звенит
        # операторам. Тринадцать человек получили бы лавину чужих
        # прошлогодних чатов, сторож разослал бы напоминания про давно
        # разобранное, а скорость первого ответа посчиталась бы по
        # переписке, которой мы не вели.
        #
        # Поэтому всё, что старше момента подключения, идёт исторической
        # дверью (`backfill=True`): диалог создаётся закрытым, в очередь
        # не встаёт, бот не запускается. И без публикации: кадр на каждое
        # сообщение годовой давности — это шторм в сокете ради того, чего
        # никто не ждёт.
        historic = live_since is not None and event.created_at < live_since
        # эхо отбрасывает apply_inbound_event (решение №1)
        async with factory() as db:
            if await apply_inbound_event(
                db, redis, account, event, backfill=historic, publish=not historic
            ):
                if historic:
                    imported += 1
                else:
                    recovered += 1
    return recovered, imported
