"""apply_inbound_event — the single write path for every inbound source
(webhook, reconciliation, backfill later) — 08 §2.5.

Owner decisions enforced here:
1. echo messages (event.author_id == account.avito_user_id) are DROPPED;
2. no auto-closing of conversations anywhere;
3. backfill creates historic conversations with status='closed' and never
   reopens them here (reopen — только настоящие входящие).

The whole write is ONE transaction; Pub/Sub publish goes strictly AFTER
commit (08 §8.1). Idempotency backstop — the partial unique index on
messages (conversation_id, external_message_id, created_at).

Очередь «Входящие» (план 7.1) наполняется ЗДЕСЬ и только здесь — это
единственный путь, которым диалог из Авито попадает в систему. Новый диалог
получает ``offered_at`` на вставке, вернувшийся клиент — новым ожиданием
через :func:`app.services.inbox.enter_queue`. Без этих двух строк вкладка
«Входящие» после деплоя показывала бы только то, что засеял бэкофилл
миграции 0007, и пустела бы навсегда.

Диалог встаёт в очередь и тогда, когда его подхватит бот, и это осознанно.
До 7.1 такой диалог лежал во вкладке «Новые», где ответить в него мог любой
(правило «кто первым ответил», 01 §6.2) — прятать его от очереди значило бы
УБАВИТЬ видимость: сломавшийся или замолчавший бот уносил бы клиента туда,
где его никто не ищет. Оператор, принявший диалог посреди сценария, бота не
ломает: первое же его сообщение глушит бота навсегда (02 §2.6), а передача
от бота ставит диалог в очередь заново с честным временем ожидания
(``app/bots/handoff.py``).
"""

import contextvars
import dataclasses
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, Protocol

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core import trace
from app.integrations.avito.listing_url import city_by_name
from app.models import (
    AvitoAccount,
    Client,
    ClientAddressCandidate,
    ClientPhoneCandidate,
    Conversation,
    Message,
)
from app.models.client import (
    CANDIDATE_REJECTED,
    CANDIDATE_SOURCE_INBOUND,
    CANDIDATE_SOURCE_VOICE,
    LINK_ASSUMED,
    LINK_CONFIRMED,
)
from app.services import (
    address_ask,
    address_funnel,
    address_llm,
    address_own,
    address_parse,
    app_settings,
    clients,
    dialect,
    distribution,
    geocode,
    inbox,
    notifications,
    phone_parse,
    phone_rules,
)
from app.services import conversation_status as status_dict
from app.services import voice as voice_service
from app.services.audit import write_audit
from app.services.avito_text import AVITO_TEXT_LIMIT, split_text
from app.services.client_enrich import city_slug_of, enqueue_enrich_client
from app.services.conversations import (
    AVITO_SYSTEM_PREFIX,
    conversation_city,
    message_out,
    other_conversation_cities,
)
from app.services.conversations import add_system_message as convs_add_system_message
from app.services.conversations import ensure_in_progress as convs_ensure_in_progress
from app.services.geocode_queue import enqueue_autofill, enqueue_geocode, enqueue_llm_read
from app.services.merge_queue import enqueue_merge
from app.services.user_ref import user_ref, user_ref_parts
from app.ws.events import iso, publish_event
from app.ws.hub import publish_inbox_new

log = structlog.get_logger("app.inbound")


class InboundEventLike(Protocol):
    """Normalized inbound event — contract of AvitoAdapter.parse_webhook
    (DESIGN §8.3; модуль пишется параллельно в OAuth-зоне, здесь только
    duck-typing по атрибутам). Для kind='message' идентификаторы непустые.

    Необязательные поля читаются через ``getattr`` и в протокол не входят —
    у запасного разборщика воркера (``FallbackInboundEvent``) их нет:
    ``is_system`` («служебная запись Авито, а не переписка») и ``source_type``
    (сырой вид от Авито, как пришёл)."""

    @property
    def chat_id(self) -> str | None: ...
    @property
    def message_id(self) -> str | None: ...

    author_id: int | None
    text: str | None
    created_at: datetime


def extract_phone(text: str | None) -> str | None:
    """Первый российский номер в тексте, приведённый к ``+7XXXXXXXXXX``.

    Разбор переехал в :mod:`app.services.phone_parse` — здесь остался вход, по
    которому его зовут четыре чужих места (`app/bots/steps.py` для валидатора
    «телефон» и маскирования перед отправкой в модель, `app/bots/engine.py` для
    шага «Вопрос»). Имя и подпись сохранены намеренно: менять их значило бы
    править файлы в зоне ботов ради переезда, который их не касается.

    ЧТО ИЗМЕНИЛОСЬ ВНУТРИ. Прежняя регулярка брала ЛЮБЫЕ 10–11 цифр подряд, не
    глядя на соседей и не считая длину цепочки целиком: «Телефон 89001112240abc»
    давала номер, «8-900-111-22-40-77» — тоже (первые одиннадцать цифр из
    тринадцати). Теперь и то и другое остаётся нераспознанным; почему именно —
    в шапке `phone_parse`.
    """
    found = phone_parse.find_first(text)
    return found.value if found else None


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _client_name(event: Any) -> str | None:
    """Adapter zone's InboundEvent carries ``client_name`` (fallback: author_name)."""
    return getattr(event, "client_name", None) or getattr(event, "author_name", None)


async def _upsert_client(
    db: AsyncSession, event: InboundEventLike
) -> tuple[Client, bool, Client | None]:
    """clients UNIQUE (channel, external_id); the race loser reads the winner's row.

    Второй элемент — «строку вставили мы и имени в событии не было»: вебхук
    Мессенджера v3 имени не несёт, поэтому его дотягивает отдельная ARQ-задача
    (хвост спринта 2, пункт «а»). Проигравший гонку не ставит задачу — её уже
    поставил победитель.

    Третий — ИСХОДНАЯ карточка, если по цепочке `merged_into_id` ушли к
    победителю (иначе ``None``): новый диалог запишет её в
    `conversations.origin_client_id`, и «Разъединить» сможет вернуть его.

    ЗДЕСЬ И ПРОИСХОДИТ МЕЖКАНАЛЬНАЯ СКЛЕЙКА, о которой спрашивал владелец.
    В отборе (`sel` ниже) НЕТ аккаунта: клиент ищется по паре
    `channel + external_id`, где `external_id` — это `author_id` Авито. Значит
    сообщение с ВТОРОГО аккаунта, у которого тот же `author_id`, не заводит
    вторую карточку, а переиспользует первую, и диалог второго аккаунта встаёт
    под тот же `client_id`. Так работает с первого дня, и держится это на
    ограничении `UNIQUE(channel, external_id)` в `app/models/client.py` —
    тоже без аккаунта.

    Проверять здесь нечего и менять здесь нечего: связывание уже есть. Чего у
    него не было — видимости и меры доверия; их добавляет
    :func:`_note_cross_account_link` ниже.
    """
    # ПУСТОЙ ИДЕНТИФИКАТОР — НЕ ИДЕНТИФИКАТОР, И СКЛЕИВАТЬ ПО НЕМУ НЕЛЬЗЯ.
    #
    # Что случилось 12 августа на боевой системе. У служебных событий Авито
    # автора нет, `str(event.author_id)` давал "0" или "None", и отбор по паре
    # `channel + external_id` находил ПЕРВУЮ такую карточку. К ней прицепился
    # каждый следующий такой чат: восемь диалогов из восьми городов и двух
    # каналов слиплись в одного человека с чужим именем — и вдобавок получили
    # подпись «возможно, тот же человек писал и на другой канал». Совпало при
    # этом ровно ничего.
    #
    # Настоящая личность у такого чата одна — сам чат. Поэтому запасной
    # идентификатор строится из `external_chat_id`: карточки остаются
    # раздельными, а когда придёт настоящее сообщение с автором, оно заведёт
    # нормальную карточку по автору.
    #
    # Форма `chat:<id>` выбрана намеренно: она не может совпасть с числовым
    # `author_id` Авито ни при каких данных, и по ней сразу видно, что личность
    # неизвестна, — в базе, в логах и в выгрузке.
    author_id = getattr(event, "author_id", None)
    if author_id in (None, 0):
        chat_key = str(getattr(event, "chat_id", None) or "")
        if not chat_key:
            # Ни автора, ни чата — привязать событие не к чему. Отдельная
            # карточка «ничего не знаем» была бы свалкой ровно того же рода,
            # ради которой всё это и переписано.
            raise ValueError("событие без автора и без чата: привязать его не к чему")
        external_id = f"chat:{chat_key}"
    else:
        external_id = str(author_id)
    author_name = _client_name(event)
    sel = select(Client).where(Client.channel == "avito", Client.external_id == external_id)
    client = (await db.execute(sel)).scalar_one_or_none()
    if client is None:
        insert = dialect.insert(db)
        result = await db.execute(
            insert(Client)
            .values(id=uuid.uuid4(), channel="avito", external_id=external_id, name=author_name)
            .on_conflict_do_nothing(index_elements=["channel", "external_id"])
        )
        client = (await db.execute(sel)).scalar_one()
        inserted = getattr(result, "rowcount", 0) == 1
        return client, inserted and not author_name, None
    # ⚠ `name_set_at` — ЭТО «ИМЯ ТРОГАЛ ЧЕЛОВЕК». Пустое поле у нас значит две разные
    # вещи: «ещё не узнали» и «диспетчер стёр неверное». Различить их можно только по
    # отметке. Без неё очищенное человеком имя вернулось бы следующим же сообщением
    # клиента, и диспетчер решил бы, что кнопка не работает.
    if author_name and not client.name and client.name_set_at is None:
        client.name = author_name
    # ⚠ ОБЪЕДИНЁННАЯ КАРТОЧКА — ИДЁМ ДО КОНЕЧНОЙ (решение владельца 19.08,
    # находка аудита L-014). Приём находил карточку по внешнему ключу и на
    # признак объединения не смотрел ни разу: сообщение клиента, чью карточку
    # уже слили с другой, садилось на проигравшую — переписка одного человека
    # расползалась надвое, а диспетчер видел половину. На бою объединений 16,
    # объединённых карточек сейчас две, то есть случай не теоретический.
    #
    # Потолок глубины — защита от кольца в данных: цепочка «а в б, б в в»
    # законна, кольцо «а в б, б в а» означало бы порчу, и вешать на ней приём
    # клиентских сообщений нельзя. Дошли до потолка — работаем с тем, что есть.
    исходная = client
    for _ in range(5):
        if client.merged_into_id is None:
            break
        следующая = await db.get(Client, client.merged_into_id)
        if следующая is None or следующая.id == client.id:
            break
        client = следующая
    return client, False, (исходная if исходная.id != client.id else None)


async def _upsert_conversation(
    db: AsyncSession,
    account: AvitoAccount,
    client: Client,
    event: InboundEventLike,
    *,
    initial_status: str,
    offered_at: datetime | None,
    origin_client_id: uuid.UUID | None = None,
) -> tuple[Conversation, bool]:
    """conversations UNIQUE (channel, external_chat_id); existing row FOR UPDATE (08 §8.4).

    Второй элемент — «строку вставили мы»: у проигравшего гонку создания диалог
    уже создан кем-то другим, и второй раз ставить его в очередь (кадр
    ``inbox:new``, звук у тринадцати операторов) нельзя.

    ``offered_at`` — момент входа в очередь «Входящие» (план 7.1). Ставится
    ЗДЕСЬ, на создании: это и есть точка, где у клиента начинается ожидание. У
    бэкофилла он None — история в очередь не идёт (решение владельца №3), иначе
    в день включения 7.1 архив похоронит под собой живых клиентов.
    """
    external_chat_id = str(event.chat_id)
    sel = (
        select(Conversation)
        .where(
            Conversation.channel == "avito",
            Conversation.external_chat_id == external_chat_id,
        )
        .with_for_update()
    )
    conv = (await db.execute(sel)).scalar_one_or_none()
    if conv is not None:
        return conv, False
    insert = dialect.insert(db)
    result = await db.execute(
        insert(Conversation)
        .values(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id=external_chat_id,
            account_id=account.id,
            client_id=client.id,
            origin_client_id=origin_client_id,
            status=initial_status,
            # Диалог вошёл в свой первый статус в момент создания. Без этой
            # строки у всего, что заводит вебхук, `status_since` осталась бы
            # пустой до первой смены статуса — то есть строка контекста в
            # карточке молчала бы ровно у самых свежих обращений, где она
            # нужнее всего.
            status_since=datetime.now(UTC),
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            offered_at=offered_at,
            declined_by=[],
            item_title=getattr(event, "item_title", None),
            item_url=getattr(event, "item_url", None),
            item_price=getattr(event, "item_price", None),
            # ГОРОД РАЗБИРАЕТСЯ ЗДЕСЬ ЖЕ, А НЕ ТОЛЬКО В ЗАДАЧЕ ОБОГАЩЕНИЯ.
            #
            # Задача ставится, лишь когда объявления НЕТ (см. ниже по коду), —
            # а сверка и загрузка истории приносят событие уже С объявлением.
            # Такой диалог задачу не получал, и город у него не появлялся
            # никогда: 11 августа на боевой базе так набралось 21 диалог со
            # ссылкой вида /bryansk/... и пустым городом.
            #
            # Разбор чист и дёшев (ни сети, ни базы), поэтому дешевле звать его
            # в каждой точке записи ссылки, чем помнить про исключения.
            item_city_slug=city_slug_of(getattr(event, "item_url", None)),
        )
        .on_conflict_do_nothing(index_elements=["channel", "external_chat_id"])
    )
    inserted = getattr(result, "rowcount", 0) == 1
    return (await db.execute(sel)).scalar_one(), inserted


async def _insert_message_idempotent(
    db: AsyncSession, conv: Conversation, event: InboundEventLike
) -> Message | None:
    """INSERT ... ON CONFLICT DO NOTHING against the partial unique index
    (08 §8.2). None → duplicate (webhook retry / reconciliation replay)."""
    insert = dialect.insert(db)
    values = {
        "id": uuid.uuid4(),
        "conversation_id": conv.id,
        "external_message_id": str(event.message_id),
        "direction": "in",
        "sender_type": "client",
        "sender_user_id": None,
        "body": event.text,
        "attachments": getattr(event, "attachments", None) or [],
        "delivery_status": "delivered",
        "created_at": _aware_utc(event.created_at),
    }
    result = await db.execute(
        insert(Message)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["conversation_id", "external_message_id", "created_at"],
            index_where=sa.text("external_message_id IS NOT NULL"),
        )
    )
    if getattr(result, "rowcount", 0) != 1:  # CursorResult; 0 => конфликт (дубль)
        return None
    return Message(**values)


#: Служебная запись Авито в ленте: `direction='system'`, `sender_type='avito'`.
#:
#: ПОЧЕМУ ИМЕННО ЭТА ПАРА, А НЕ НОВАЯ КОЛОНКА. Все двенадцать потребителей
#: сообщений отсекают такую строку САМИ, без единой правки на их стороне:
#: `direction='in' AND sender_type='client'` — витрина статистики (миграция
#: 0004), живой расчёт первого ответа (`stats.py`), сторож приёма
#: (`scheduler/jobs/watchdog.py`), звук на фронте (`platform/toast.ts`);
#: только `direction='in'` — `first_client` разбора (`conversation_table.py`),
#: восстановление ожидания (`messages.py restore_awaiting`), пересчёт
#: непрочитанных (`read_markers.py`); `direction IN ('in','out')` — превью
#: строки списка и счётчик сообщений диалога. Проверено чтением каждого места,
#: а не доверием к комментариям рядом с ними.
#:
#: `sender_type='avito'` отличает служебную запись Авито от НАШЕЙ собственной
#: (`sender_type='system'` — «Статус: Новый → В работе. Иванов»). Разница не
#: косметическая: наша запись рассказывает про нашу же работу, запись Авито —
#: чужие слова, за которыми может стоять действие. CHECK'а на колонке нет
#: (`0001_init.py`), поэтому новое значение не требует миграции.
#:
#: Пара одна на оба входа: живой путь (`_apply_avito_system_event`) и
#: историческая дверь (`avito_accounts._insert_history_message`, пакет 6.0а
#: I-10) — раньше дверь клала ту же запись как `in/client`, и «Ассистент
#: Авито ответил…» считался буквами клиента у стенда и `client_described`.
AVITO_SYSTEM_DIRECTION = "system"
AVITO_SYSTEM_SENDER = "avito"


def avito_system_prefixed(text: str | None) -> bool:
    """Служебная запись Авито ПО ПРИСТАВКЕ в тексте (`AVITO_SYSTEM_PREFIX`) —
    ОДИН признак на живой путь и историческую дверь (пакет 6.0а, I-10).
    Внутри переписки Авито служебности не объявляет, выдаёт её только эта
    приставка; конверт (`is_system`) — второй, независимый признак, и у истории
    он шире (там `is_system` носят и геоточка, и ссылка, и объявление клиента
    — `adapter._SYSTEM_SOURCE_TYPES`), поэтому дверь истории судит только по
    приставке. Разойдись два пути в признаке — одна и та же запись лежала бы в
    ленте под разными парами `direction/sender_type`."""
    return (text or "").startswith(AVITO_SYSTEM_PREFIX)


async def _inbox_frame_for(db: AsyncSession, conv: Conversation) -> dict[str, Any]:
    """Строка очереди ВМЕСТЕ со списком допущенных — собирается ВНУТРИ транзакции.

    ⚠ СПИСОК ЗДЕСЬ ОБЯЗАТЕЛЕН, И ЕГО НЕ БЫЛО (найдено 27.08). Хаб отбирает
    получателей кадра `inbox:new` по ключу `eligible`, который кладёт ТОЛЬКО
    публикатор; пустой список по правилу совместимости означает «канал открыт
    всем» (`_channel_allows`, ws/hub.py). Боевой конвейер вебхука список не
    передавал ни в одной из трёх своих веток — и кадр очереди уезжал ВСЕМ
    тринадцати диспетчерам: чужой клиент звенел, строка появлялась в очереди с
    живой кнопкой «Принять», а нажатие упиралось в 403 «Диалог канала «…» ведут
    другие операторы». После перезагрузки строка исчезала, потому что GET /inbox
    выборку сужает, — счётчик расходился со списком.

    Ровно от этого заведена `inbox.inbox_frame_addressed` (аудит 19.08): «собирать
    кадр и список раздельно в шести местах — верный способ снова разъехаться,
    поэтому здесь они собираются вместе и всегда». Три джобы планировщика её
    зовут, конвейер — не звал.

    Возвращаем ту же пару `{"conversation": …, "eligible": […]}`, что и она.
    """
    from app.services import inbox as _inbox

    пара = await _inbox.inbox_frame_addressed(db, conv)
    пара["conversation"].setdefault("id", str(conv.id))
    return пара


async def _apply_avito_system_event(
    db: AsyncSession,
    account: AvitoAccount,
    event: InboundEventLike,
    redis: Redis | None = None,
    *,
    backfill: bool = False,
) -> bool:
    """Служебное сообщение Авито -> серый чип в ленте, и БОЛЬШЕ НИЧЕГО.

    Четыре вещи, которых здесь намеренно нет, — и это главный смысл функции:

    * не растёт `unread_count` — иначе бейдж зовёт оператора туда, где клиент
      ничего не спрашивал;
    * не ставится `awaiting_since` — иначе диалог попадает в «клиент ждёт», и
      сторож начинает торопить с ответом на уведомление Авито;
    * не двигается `last_message_at` и не трогается `status` — иначе диалог
      всплывает наверх списка и выходит из «тихо N дней» без участия человека;
    * не зовётся ни автораздача, ни бот, ни разбор телефона — служебная запись
      не повод будить тринадцать человек и не повод сочинять клиенту ответ.

    В метрики она не попадает сама (см. `AVITO_SYSTEM_DIRECTION`).

    ЧУЖОГО ДИАЛОГА НЕ СОЗДАЁМ. Событие в неизвестный чат — выходим ни с чем:
    диалог требует клиента (`conversations.client_id NOT NULL`), а клиента у
    служебного события нет, и завести пришлось бы выдуманного. Сырец такого
    события лежит в `webhook_raw_log`, а когда человек напишет сам —
    сработает обычное создание диалога со всей его обвязкой.
    """
    chat_id = event.chat_id
    if not chat_id:
        return False
    call_lead_frame: dict[str, Any] | None = None
    async with db.begin():
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.channel == "avito",
                    Conversation.external_chat_id == str(chat_id),
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            if getattr(event, "source_type", None) in ("appCall", "call"):
                # ЗВОНОК В НОВЫЙ ЧАТ — ЭТО ЛИД (аудит 17.08, №1 critical):
                # клиент нашёл объявление и позвонил. Раньше событие
                # выбрасывалось «сиротой» — обращение исчезало без следа.
                # Клиент-заглушка «сам чат», диалог НОВЫМ в очередь, чип
                # звонка в ленту — операторы увидят и перезвонят.
                stub_ext = f"chat:{chat_id}"
                client = (
                    await db.execute(
                        select(Client).where(
                            Client.channel == "avito", Client.external_id == stub_ext
                        )
                    )
                ).scalar_one_or_none()
                if client is None:
                    client = Client(id=uuid.uuid4(), channel="avito", external_id=stub_ext)
                    db.add(client)
                    await db.flush()
                # ⚠ ИСТОРИЧЕСКИЙ ЗВОНОК — НЕ РАБОТА (19.08, разбор потока у
                # владельца). Живой звонок это лид: человек нашёл объявление и
                # позвонил, ему надо перезвонить сейчас. А загрузка истории
                # поднимает ВСЕ звонки за всё время существования кабинета — и
                # каждый ставила в очередь как новое обращение. На восьми
                # каналах это дало 4611 диалогов, в которых нет ни одного слова
                # клиента: одни служебные записи «Клиент звонил через
                # приложение Авито». Очередь перестаёт быть очередью, а
                # настоящие 172 обращения тонут в этом потоке.
                #
                # Историю заводим ЗАКРЫТОЙ: диалог виден в «Все», находится
                # поиском, открывается — он просто не требует ответа сегодня.
                # Позвонит снова — придёт живым событием и встанет в очередь.
                # ⚠⚠ РЕШЕНИЕ ВЛАДЕЛЬЦА 19.08, ОТМЕНЯЕТ МОЁ ОТ 17.08: «Пусть во
                # входящие не приходят такие чаты». Он показал экран, где в
                # очереди висит диалог с тремя строками «Клиент звонил через
                # приложение Авито» и НИ ОДНИМ словом клиента — и ждёт 42
                # минуты, требуя внимания.
                #
                # Прежнее правило («звонок в новый чат — это лид») звучало
                # разумно: человек нашёл объявление и позвонил. На деле у
                # владельца звонки идут В ТЕЛЕФОН и там же обрабатываются, а в
                # LeadChat от них остаётся только след — работать с ним некому и
                # нечем: ни имени, ни вопроса, ни номера в самом чате.
                #
                # Диалог по-прежнему создаётся и виден в «Все», чип звонка в
                # ленте на месте, поиск его находит. Он просто не требует хода.
                # Напишет — встанет в очередь обычным путём, со словами, на
                # которые есть что ответить.
                _без_очереди = True
                conv = Conversation(
                    id=uuid.uuid4(),
                    channel="avito",
                    external_chat_id=str(chat_id),
                    account_id=account.id,
                    client_id=client.id,
                    status="closed" if _без_очереди else "new",
                    status_since=_aware_utc(event.created_at),
                    bot_active=False,
                    bot_vars={},
                    tags=[],
                    unread_count=0 if _без_очереди else 1,
                    offered_at=None if _без_очереди else _aware_utc(event.created_at),
                    declined_by=[],
                    last_message_at=_aware_utc(event.created_at),
                    # клиент ЖДЁТ ответа с момента звонка: без этой отметки
                    # сторож «ждёт 15 минут» на звонок не сработал бы никогда.
                    # У истории ждать нечего — звонку может быть год.
                    awaiting_since=None if _без_очереди else _aware_utc(event.created_at),
                )
                db.add(conv)
                await db.flush()
                # кадр очереди — звонок это лид, и он обязан появиться у всех
                # операторов сразу, а не после перезагрузки (аудит 18.08).
                # У истории кадра нет: она никому не звенит.
                if not _без_очереди:
                    call_lead_frame = await _inbox_frame_for(db, conv)
            else:
                log.info(
                    "inbound.avito_system_orphan",  # видно, сколько таких мимо ленты
                    account_id=str(account.id),
                    external_chat_id=str(chat_id),
                    source_type=getattr(event, "source_type", None),
                )
                return False

        # Своя вставка, а не `_insert_message_idempotent`: у той жёстко зашита
        # пара `in/client`, и параметризовать её значило бы тронуть горячий путь
        # настоящих клиентских сообщений ради служебной строки. ON CONFLICT тот
        # же и по тому же частичному индексу — повтор вебхука (или запись,
        # вернувшаяся из PEL) не должен давать второй чип.
        insert = dialect.insert(db)
        result = await db.execute(
            insert(Message)
            .values(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                external_message_id=str(event.message_id) if event.message_id else None,
                direction=AVITO_SYSTEM_DIRECTION,
                sender_type=AVITO_SYSTEM_SENDER,
                sender_user_id=None,
                body=event.text,
                attachments=[],
                delivery_status="delivered",
                created_at=_aware_utc(event.created_at),
            )
            .on_conflict_do_nothing(
                index_elements=["conversation_id", "external_message_id", "created_at"],
                index_where=sa.text("external_message_id IS NOT NULL"),
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            return False
        conversation_id = conv.id

    # Кадра `message:new` НЕТ, и это решение, а не забывчивость. Фронт на такой
    # кадр перепишет строке списка «последнее сообщение» и переставит её наверх
    # (`applyWsEvent.ts`, безусловная запись `last_message`), а если строки в
    # кэше нет — сбросит списки диалогов ЦЕЛИКОМ у всех тринадцати. Платить
    # рефетчем у всех за серый чип, который ничего в списке не меняет, нельзя.
    # Чип появляется при открытии диалога. Чтобы он приезжал вживую, нужна
    # правка `applyWsEvent.ts` (не наш файл в этой работе) — тогда сюда
    # вернётся publish.
    if call_lead_frame is not None and redis is not None:
        # звонок в новый чат — это ЛИД: строка очереди у всех операторов сразу
        # ⚠ publish_inbox_new ЖДЁТ САМУ СТРОКУ очереди и заворачивает её сам
        # (app/ws/hub.py). Готовый конверт сюда передавать нельзя: ключа "id"
        # у него нет, и звонок-лид падал `KeyError: 'id'` прямо на бою (два
        # клиента 18.08, находка L-001). Диалог оставался в базе, а живой
        # строки очереди у диспетчеров не появлялось ни разу: при повторе
        # вебхука диалог уже существовал и ветка звонка не срабатывала.
        await publish_inbox_new(
            redis,
            call_lead_frame["conversation"],
            eligible_operator_ids=call_lead_frame.get("eligible"),
        )
    log.info(
        "inbound.avito_system_stored",
        conversation_id=str(conversation_id),
        source_type=getattr(event, "source_type", None),
    )
    return True


async def _есть_присоединённые(db: AsyncSession, client_id: uuid.UUID) -> bool:
    return bool(
        (
            await db.execute(select(Client.id).where(Client.merged_into_id == client_id).limit(1))
        ).scalar_one_or_none()
    )


async def _note_cross_account_link(
    db: AsyncSession, account: AvitoAccount, client: Client, *, now: datetime
) -> None:
    """Первый раз, когда карточка клиента собрала диалоги с ДВУХ аккаунтов.

    ЗАЧЕМ ОТДЕЛЬНОЕ СОБЫТИЕ. Склейка по `author_id` работает молча с первого
    дня (см. :func:`_upsert_client`), и до 11 августа она НИ РАЗУ не
    сработала: в боевой базе 52 клиента и ни одного, писавшего больше чем на
    один аккаунт. Значит первый настоящий случай — это единственная
    возможность проверить допущение «author_id Авито сквозной», которое
    никогда не проверялось и которое спецификация Авито не подтверждает
    («Обратите внимание на хэширование» у `Chat.users[].id`, и ни слова у
    `author_id` вебхука). Раствориться в потоке этот случай не должен:
    отсюда и строка журнала с ОБОИМИ аккаунтами, и отметка в базе.

    ЖУРНАЛ, А НЕ `audit_log`. `audit_log` — это «кто из сотрудников что
    сделал» (реестр `AUDIT_ACTIONS`, за полнотой которого следит
    `tests/unit/test_audit.py`); здесь сотрудник ни при чём. Поэтому событие
    идёт в общий журнал приложения `structlog`, где его видно поиском по
    `client.cross_account_linked`, а НЕИСЧЕЗАЮЩИЙ след остаётся в базе:
    журналы ротируются, `clients.cross_account_since` — нет.

    ЗОВЁТСЯ ТОЛЬКО НА СОЗДАНИИ ДИАЛОГА. Межканальной карточка становится
    ровно в тот момент, когда у неё появляется диалог на новом аккаунте;
    следующие сообщения в него ничего не меняют. Поэтому запрос ниже
    выполняется не на каждое сообщение, а на каждый новый диалог — и первым
    делом отсекается уже отмеченная карточка, чтобы у постоянного
    межканального клиента запроса не было вовсе.

    ИМПОРТ ИСТОРИИ (`backfill=True`) ТОЖЕ СЮДА ЗАХОДИТ, И ЭТО НАРОЧНО. Залив
    переписки со второго аккаунта может высыпать пачку таких строк разом —
    выглядит как всплеск предупреждений, но это ровно тот же факт: у карточки
    появился диалог на другом аккаунте. Не различать эти два случая честнее,
    чем промолчать на импорте: если склейка ложная, импорт покажет её первым.
    """
    if client.cross_account_since is not None:
        return
    if client.external_id.startswith("chat:"):
        # ЛИЧНОСТЬ НЕИЗВЕСТНА — СКЛЕИВАТЬ НЕЧЕГО И ПОДПИСЫВАТЬ НЕЧЕМ.
        #
        # Такая карточка заведена по чату, потому что у события не было автора
        # (служебные события Авито). Двух диалогов на разных аккаунтах у неё
        # быть не может по построению — но если запасной ключ когда-нибудь
        # изменится, лучше промолчать, чем повторить 12 августа: тогда восемь
        # посторонних людей получили подпись «возможно, это тот же человек».
        return
    other_account_id = (
        await db.execute(
            select(Conversation.account_id)
            .where(
                Conversation.client_id == client.id,
                Conversation.account_id != account.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_account_id is None:
        return
    if client.merged_into_id is not None or await _есть_присоединённые(db, client.id):
        # Диалог на другом аккаунте пришёл сюда через ОБЪЕДИНЕНИЕ карточек
        # (ручное или по телефону, 12.09) — связь уже объяснена строкой
        # `client.merged`, и «идентификатор Авито сквозной» тут ни при чём.
        # Отметка поставила бы `link_confidence=assumed`, а первый же второй
        # номер («звоните жене») — спор, которого нет. Проверка стоит ПОСЛЕ
        # поиска второго аккаунта: у обычной карточки это лишний запрос на
        # каждый новый диалог (бюджет SQL входящего).
        return
    client.cross_account_since = now
    # Только предположение: совпал ОДИН лишь идентификатор Авито. Подтвердить
    # или опровергнуть может телефон — см. `_apply_phone_evidence`.
    client.link_confidence = LINK_ASSUMED
    log.warning(
        "client.cross_account_linked",
        client_id=str(client.id),
        # Оба аккаунта — в одной строке: без них случай нельзя ни
        # воспроизвести, ни проверить руками в кабинете Авито.
        account_id=str(account.id),
        other_account_id=str(other_account_id),
        # Тот самый `author_id`, на котором держится склейка. По нему владелец
        # сверяет две карточки чата в Авито и отвечает на вопрос, ради
        # которого всё это и заведено.
        avito_author_id=client.external_id,
        link_confidence=LINK_ASSUMED,
        at=iso(now),
    )


def _apply_phone_evidence(
    client: Client,
    phone: str,
    account_id: uuid.UUID,
    now: datetime,
    *,
    known_phone: str | None,
    known_account_id: uuid.UUID | None,
) -> None:
    """Телефон как проверка межканальной склейки — подтверждение или спор.

    ЧТО ИМЕННО ЗДЕСЬ ДОКАЗЫВАЕТСЯ. Опасность склейки по `author_id` — это
    СТОЛКНОВЕНИЕ: если идентификатор Авито свой у каждого аккаунта, то
    одинаковое число с двух каналов принадлежит двум РАЗНЫМ людям, и они
    съезжаются в одну карточку вместе с телефонами и перепиской. Телефон
    разводит эти два случая:

    * тот же номер пришёл с ДРУГОГО канала — совпасть у столкнувшихся
      посторонних и идентификатору, и номеру нереально, значит это один
      человек: `confirmed`;
    * номер с другого канала ДРУГОЙ — прямой довод в пользу того, что
      склеены разные люди. Уверенность остаётся `assumed`, и карточка
      получает отметку спора.

    ПОЧЕМУ ВАЖНО «С ДРУГОГО КАНАЛА». Номер, совпавший с номером, взятым с
    ТОГО ЖЕ аккаунта, не доказывает ничего: он подтверждает лишь то, что
    клиент дважды написал один и тот же телефон в один и тот же канал. Отсюда
    `clients.phone_account_id`; когда происхождение номера неизвестно (строки
    до миграции 0025 и второй писатель телефона — `app/bots/engine.py`), мы
    молчим, а не додумываем.

    ПОЧЕМУ СПОР НЕ ОТМЕНЯЕТ ПОДТВЕРЖДЁННУЮ СКЛЕЙКУ. В ремонте техники второй
    номер у одного человека — обычное дело («звоните жене»). После того как
    один и тот же номер пришёл с обоих каналов, следующий несовпадающий — это
    второй телефон, а не другой человек. Обратный порядок (сначала спор,
    потом совпадение) снимает отметку спора: совпадение сильнее.

    ЧТО ЗА `known_phone`. Это «номер, который система уже знала об этом
    человеке, и канал, откуда он пришёл». Раньше он читался прямо из карточки
    (`client.phone` / `client.phone_account_id`) — и после правки 10 это
    сломало бы проверку у ВСЕХ: по умолчанию распознанный номер в карточку
    больше не пишется, то есть сравнивать было бы не с чем и склейка молча
    перестала бы проверяться. Теперь «уже знали» считает
    :func:`_known_phone_of` — из карточки, а если её нет, из первого
    распознанного номера. Сама логика ниже не изменилась ни на строку.
    """
    if client.cross_account_since is None:
        return  # склейки нет — подтверждать и опровергать нечего
    if known_account_id is None or known_account_id == account_id:
        return  # тот же канал либо неизвестное происхождение — не доказательство
    if phone == known_phone:
        client.link_confidence = LINK_CONFIRMED
        client.link_phone_conflict_at = None
        return
    if client.link_confidence != LINK_CONFIRMED and client.link_phone_conflict_at is None:
        client.link_phone_conflict_at = now


async def _known_phone_of(db: AsyncSession, client: Client) -> tuple[str | None, uuid.UUID | None]:
    """Номер, который система уже знала об этом человеке, и канал, откуда он был.

    Порядок источников — от твёрдого к мягкому. Карточка первая: там номер уже
    прошёл через человека либо через прежнюю автозапись. Если карточка пуста,
    берётся ПЕРВЫЙ распознанный в переписке номер — тот, который система
    увидела раньше остальных; его канал вычисляется через диалог, в котором он
    написан.

    ОТКЛОНЁННЫЕ НЕ УЧАСТВУЮТ. Оператор сказал «это не его телефон» — строить на
    этом вывод «в карточке два разных человека» значило бы спорить с ним же.

    ЗАПРОС ВЫПОЛНЯЕТСЯ ТОЛЬКО У МЕЖКАНАЛЬНЫХ КАРТОЧЕК. Их на боевой системе
    12 августа был ровно один клиент из 52; у всех остальных функция выходит на
    первой строке, не тронув базу.
    """
    if client.cross_account_since is None:
        return None, None
    if client.phone is not None:
        return client.phone, client.phone_account_id
    row = (
        await db.execute(
            select(ClientPhoneCandidate.phone, Conversation.account_id)
            .join(Conversation, Conversation.id == ClientPhoneCandidate.conversation_id)
            .where(
                ClientPhoneCandidate.client_id == client.id,
                ClientPhoneCandidate.status != CANDIDATE_REJECTED,
            )
            .order_by(ClientPhoneCandidate.detected_at, ClientPhoneCandidate.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row.phone, row.account_id


async def _известить_о_клиенте(
    redis: Redis, conv: Conversation, client: Client, *, reason: str
) -> None:
    """Кадр «карточка клиента изменилась» (жалоба владельца 31.08).

    ⚠ REDIS ПЕРЕДАЁТСЯ, А НЕ БЕРЁТСЯ ИЗ ГЛОБАЛЬНОГО (02.09). Здесь стоял
    `redis_mod.get_client()` — тот же клиент, что и у вызывающего, но добытый в
    обход него. В бою разницы нет, а в проверках она решающая: тест публикует в
    свой Redis и НИЧЕГО не видит, то есть проверить этот кадр было нельзя вовсе.
    Так дыра «кадр уходит до commit'а» и дожила до боя.

    ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ КАДР. Жалоба дословно: «когда клиент скинул номер, то
    чтобы он привязался, приходится обновлять страницу». Так и было: телефон
    извлекается из текста входящего и пишется клиенту, а наружу об этом не
    сообщалось НИЧЕГО. Кадр `message:new` несёт заплатку диалога, полей клиента
    в ней нет; карточка живёт на своём ключе кэша, который не сверяет ни один
    кадр и ни одна тихая сверка. Оператор видел сообщение с номером — и пустую
    карточку рядом, пока не нажмёт F5.

    Кадр широковещательный намеренно: карточку клиента видит не только тот, кто
    ведёт диалог (руководитель смотрит «Все», коллега подхватывает). Данных в
    нём ровно столько, чтобы фронт понял, ЧТО перезапросить, — сам телефон
    сюда не кладём: у карточки есть своя ручка с правами, и рассылать
    персональные данные всем подписчикам ради экономии запроса нельзя.
    """
    # ⚠ КАДР НЕ ИМЕЕТ ПРАВА УРОНИТЬ ПРИЁМ СООБЩЕНИЯ. Он подсказка для экрана, а
    # сообщение клиента — работа: не ушёл кадр — оператор увидит номер сверкой
    # или при следующем открытии карточки, а вот потерянное входящее не
    # вернётся ничем. Ловим широко и намеренно (Redis недоступен, чужой цикл
    # событий в тестовом окружении), но НЕ молча: систематическая пропажа
    # кадров должна быть видна в журнале.
    try:
        await publish_event(
            redis,
            "client:updated",
            {
                "client_id": str(client.id),
                "conversation_id": str(conv.id),
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "client.updated_not_published",
            client_id=str(client.id),
            conversation_id=str(conv.id),
            reason=reason,
            error=str(exc),
        )


#: Вопрос оператора об адресе — по словам самого вопроса, не по смыслу.
_ВОПРОС_ОБ_АДРЕСЕ = re.compile(
    r"куда\s+(?:к\s+вам\s+)?(?:подъех|приех|ехать|нужно)|адрес|подъезд|этаж|квартир|"
    r"где\s+вы\s+наход|где\s+наход|улиц",
    re.IGNORECASE,
)
#: Те же семь групп словами для людей. Отсюда собираются текст отказа ручки
#: настроек (`address_ask.recognizable_words`) и подпись замка `asked_in_feed`:
#: слова и правило не должны разъехаться. Сторож в test_address_ask_1809:
#: каждое слово ловится регэкспом, контрольные («диагностика», «цена») — нет.
_ВОПРОС_ОБ_АДРЕСЕ_СЛОВА: tuple[str, ...] = (
    "адрес",
    "подъезд",
    "этаж",
    "квартира",
    "улица",
    "где находитесь",
    "куда подъехать",
)
#: «слово число» или «число слово» — только тогда стоит лезть в ленту за
#: вопросом оператора (стенд 14.09), а не на каждую реплику.
_ПОХОЖЕ_НА_ОТВЕТ_АДРЕСОМ = re.compile(
    r"[а-яё]{4,}[\s,.\-]*\d{1,3}(?![\d])|(?<![\d])\d{1,3}(?![\d])[\s,.\-]*[а-яё]{4,}", re.IGNORECASE
)
#: Сутки, а не два часа (бой 13.09, Елец): оператор спросил адрес вечером,
#: клиент ответил «Садовая д 27» утром через семь часов — уровень C,
#: и подтверждённый картой дом ждал кнопки оператора. Ответ на вопрос об
#: адресе — это ответ, даже назавтра.
_ОКНО_ВОПРОСА = timedelta(hours=24)


async def записать_геоточку(
    db: AsyncSession, *, client: Client, conv: Conversation, msg: Message, now: datetime
) -> clients.RecordedAddress | None:
    """ГЕОТОЧКА АВИТО (владелец 15.09): клиент выбрал точку на карте сам — адрес
    и координаты у нас уже есть, разбор переписки и поход в карту не нужны;
    строка сразу «подтверждена картой» (карта — Авито), и автозапись берёт её
    тем же порядком, что подтверждённый дом. Зовут живой путь и догон
    (`backfill-cards`): у сообщения-точки тела нет, разбором его не взять.
    """
    геоточка = _геоточка(msg.attachments)
    if геоточка is None:
        return None
    found = address_parse.parse_geopoint(геоточка["address"])
    if found is None or not address_parse.quote_holds(found, геоточка["address"]):
        return None
    записано = await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv.id,
        message_id=msg.id,
        message_at=msg.created_at,
        found=found,
        now=now,
    )
    if записано.candidate_id is None:
        return записано
    row = await db.get(ClientAddressCandidate, записано.candidate_id)
    if геоточка["lat"] is None or геоточка["lon"] is None:
        return записано  # подпись без координат — карта проверит сама
    if row is not None and row.geo_status != geocode.GEO_EXACT:
        row.geo_status = geocode.GEO_EXACT
        row.geo_provider = geocode.GEOPOINT_PROVIDER
        row.geo_formatted = геоточка["address"]
        row.geo_lat, row.geo_lon = геоточка["lat"], геоточка["lon"]
        row.geo_checked_at = now
        row.geo_variants = None
    return записано


def _геоточка(attachments: list[Any] | None) -> dict[str, Any] | None:
    """Геоточка Авито с адресом среди вложений — или None.

    Координаты есть у точек с 15.09 (адаптер их сохраняет); у прежних —
    только подпись `name` с тем же адресом, тогда `lat`/`lon` — None.
    """
    for a in attachments or []:
        if not isinstance(a, dict) or a.get("avito_type") != "location":
            continue
        адрес = a.get("address") or a.get("name")
        if not isinstance(адрес, str) or "," not in адрес:
            continue
        lat, lon = a.get("lat"), a.get("lon")
        if isinstance(lat, int | float) and isinstance(lon, int | float):
            return {"address": адрес, "lat": float(lat), "lon": float(lon)}
        return {"address": адрес, "lat": None, "lon": None}
    return None


def _точка_подтверждена_авито(msg: Message, причина_адреса: str | None) -> bool:
    """Геоточка Авито с координатами записана строкой `exact` (`записать_геоточку`):
    карту звать незачем, автозапись можно ставить сразу. ОДИН предикат для живого
    пути (`apply_inbound_event`) и догона (`replay_card_extraction`, N29): починка
    карты (`geo_repair`) строки `exact` не берёт, и второй, разошедшийся расчёт
    признака оставил бы карточку пустой навсегда (класс dva-puti-raznyi-schet)."""
    return причина_адреса is not None and (_геоточка(msg.attachments) or {}).get("lat") is not None


async def _оператор_спросил_адрес(
    db: AsyncSession, conv: Conversation, *, before: datetime
) -> bool:
    """Это сообщение — ответ на вопрос оператора об адресе?

    Вопрос — последняя реплика оператора (как с 12.09) ИЛИ одна из трёх
    последних, если клиент после неё ещё ничего не писал (владелец 14.09,
    Новочеркасск: «по адресу подскажите полному» → «могу к 15-15:30
    подъехать» → «Лесная 12/1»). Вопрос, на который клиент уже что-то
    ответил, а оператор пошёл дальше («адрес?» → «Ленина 5» → «во сколько
    удобно?» → «приезжайте 16»), закрыт — иначе речь после него читалась бы
    как адрес (ревью 14.09). Считаем по ленте, а не по строкам адреса: повтор
    адреса строки не рождает, а строка места или речи — не ответ.
    """
    последние = (
        await db.execute(
            select(Message.body, Message.created_at)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == "out",
                Message.created_at < before,
                Message.created_at >= before - _ОКНО_ВОПРОСА,
            )
            .order_by(Message.created_at.desc())
            .limit(3)
        )
    ).all()
    for номер, (текст, когда) in enumerate(последние):
        if not (текст and _ВОПРОС_ОБ_АДРЕСЕ.search(текст)):
            continue
        if номер == 0:
            return True
        между = await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == "in",
                Message.created_at > когда,
                Message.created_at < before,
            )
            .limit(1)
        )
        return между.first() is None
    return False


async def _место_к_дому(
    db: AsyncSession,
    *,
    client: Client,
    conv: Conversation,
    msg: Message,
    found: address_parse.Found,
    now: datetime,
) -> address_parse.Found | None:
    """Дом без пункта после места того же диалога (за сутки): пункт, массив и
    район места — к дому, цитата — склейка обеих реплик."""
    когда = _aware_utc(msg.created_at)
    место = await clients.latest_address_candidate(
        db, client_id=client.id, conversation_id=conv.id, since=когда - _ОКНО_ВОПРОСА, before=now
    )
    if место is None or место.kind != address_parse.KIND_PLACE:
        return None
    if место.geo_status not in (
        None,
        geocode.GEO_PENDING,
        geocode.GEO_EXACT,
    ) and not (not место.settlement and внутригородской_массив(место.area)):
        # Место, которому карта отказала («территориально далеко», бой 13.09),
        # к дому не клеится: с ним запрос уехал бы в другую область. Микрорайон
        # и квартал (владелец 19.09, Волжский: «9 микрорайон» → «пл Мира 11»)
        # — внутри города: карта их не знает, а запрос они не уводят. Но
        # только БЕЗ пункта: у «п. Лесной, 3 микрорайон» с отказом карты
        # `с_пунктом_места` понёс бы в запрос сам посёлок — ровно то, от чего
        # сторож 13.09 (ревью 19.09).
        return None
    return с_пунктом_места(found, место)


#: Внутригородской массив: город не меняет, в запрос к карте не идёт (дом
#: ищется по улице), но в строку и в текст карточки — обязан.
_ВНУТРИГОРОДСКОЙ_МАССИВ = frozenset({"микрорайон", "мкр", "мкрн", "мкр-н", "квартал", "кв-л"})


def внутригородской_массив(area: str | None) -> bool:
    слова = (area or "").lower().replace(".", " ").split()
    return bool(слова) and any(w in _ВНУТРИГОРОДСКОЙ_МАССИВ for w in слова)


def с_пунктом_места(
    found: address_parse.Found, место: ClientAddressCandidate
) -> address_parse.Found | None:
    """Пункт, массив и район места — к дому без пункта; цитата — склейка."""
    if not (место.settlement or место.area):
        return None
    if not место.settlement and внутригородской_массив(место.area):
        # «9 микрорайон» → «пл Мира 11» (владелец 19.09): массив внутри
        # города — в строку дома как `area` (в текст карточки), пунктом в
        # запрос к карте не идёт: DaData дом по улице находит и так, а «пункт
        # 9» его бы сломал.
        return dataclasses.replace(
            found,
            raw=f"{место.raw}; {found.raw}",
            area=место.area,
            district=found.district or место.district,
        )
    пункт, тип = место.settlement, место.settlement_type
    if not пункт and место.area:
        # «СНТ Берёзка» — массив без пункта: карте он нужен как пункт («Берёзка»,
        # СНТ) — так же, как «СНТ Василёк 6» в одной реплике.
        слова = место.area.split()
        имя = [w for w in слова if w.lower() not in _ТИПЫ_МАССИВА_РЕЧИ]
        типы = [w for w in слова if w.lower() in address_parse._ПУНКТ_КАНОН]
        if имя:
            пункт = " ".join(имя)
            тип = address_parse._ПУНКТ_КАНОН[типы[0].lower()] if типы else None
    if not пункт:
        return None
    return dataclasses.replace(
        found,
        raw=f"{место.raw}; {found.raw}",
        settlement=пункт,
        settlement_type=тип,
        locality=место.locality,
        district=место.district,
        area=место.area,
    )


_ТИПЫ_МАССИВА_РЕЧИ = frozenset(
    "массив мкр мкрн микрорайон квартал снт днп днт тсн кп тер территория садоводство".split()
)


# --- пункт из соседней реплики (владелец 18.09: «определение адресов должно
# работать с контекстом») ----------------------------------------------------
#
# «Здравствуйте, … нам нужен сантехник в Сукко, в однокомнатную квартиру» →
# (через две реплики) «В Сукко, на ул. Солнечную»; «Ялга ул
# Садовая д 3 кв 21» → «Ялга! Мы вам звонили!» → «Адрес ещё спросили !
# Садовая 3 кв 21». Пункт назван и до улицы, и после — отдельной репликой,
# в речи, одним словом. Без него карта подтвердила «Садовая 3» в самом
# Саранске, а дом — в рабочем посёлке Ялга. Пункт внутри ОДНОЙ реплики берёт
# разбор (`address_parse.parse`); здесь — межрепличное: пункт из соседней
# реплики клиента подставляется в строку ДО карты, с цитатой обеих реплик.

#: Сколько входящих назад смотрим — как у подсказок карты
#: (`workers/geocode.HINT_MESSAGES`); окно — сутки (`_ОКНО_ВОПРОСА`).
_КОНТЕКСТ_РЕПЛИК = 8

#: Пункт восклицанием в начале реплики: «Ялга! Мы вам звонили!» — одно
#: слово с заглавной, «!», дальше речь. С точкой («Планшет. Не включается»)
#: не берём: так называют предмет ремонта. Реплика из одного названия
#: («Ялга», «п. Ударник») — у `_реплика_целиком_пункт`.
_ПУНКТ_ПЕРВОЙ_ФРАЗОЙ = re.compile(r"^\s*(?P<name>[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?)\s*!+\s")
#: «нам нужен сантехник в Сукко, в однокомнатную квартиру»: «в/во <Имя>» с
#: заглавной внутри речи. Тип впереди («в деревне Ивановке») не берётся:
#: падеж меняет имя, а карта сверяет слова.
_В_ПУНКТЕ = re.compile(
    r"(?<![а-яёА-ЯЁ])[вВ]о?\s+(?P<name>[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?)(?![а-яё])"
)
#: «в Горно Алтайске», «в Набережных Челнах», «в Нижнем Новгороде» — два слова с
#: заглавной за «в»: берутся пунктом только когда двусловная предложная форма
#: есть в справочнике городов (пакет 5); иначе второе слово — речь или улица,
#: и работает однословный образец выше.
_В_ПУНКТЕ_ДВА = re.compile(
    r"(?<![а-яёА-ЯЁ])[вВ]о?\s+(?P<name>[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?"
    r"\s+[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?)(?![а-яё])"
)
#: Слово за именем, при котором «в <Имя>» — не пункт, а прилагательное района,
#: округа, края или дороги: «в Подольском районе», «в Красноярском крае», «в
#: Московском шоссе» (ревью 20.09). Без сторожа «Ногинском» шло бы пунктом, а
#: город-прилагательное справочника («Московский», «Октябрьский») — городом
#: клиента, и дом искался бы не там.
_ГОРОД_ПРИЛАГАТЕЛЬНОЕ = address_parse._ГОРОД_ПРИЛАГАТЕЛЬНОЕ
_ЗА_ПУНКТОМ_НЕ_ПУНКТ = re.compile(
    r"\s+(?:район|р-н|округ|кра[йе]|област|микрорайон|мкр|шоссе|проспект|пр-т|тракт|направлен)",
    re.IGNORECASE,
)
#: Слова с заглавной, которыми начинают фразу или которые стоят за «в», — не
#: пункты. Стоп-списки разбора (`_годное_имя_пункта`) знают речь, что стоит
#: на месте улицы; тут — то, что в бою пишется отдельной фразой или за
#: предлогом: «Спасибо!», «в Авито», «в Целом», «в Октябре».
_НЕ_ПУНКТ_В_РЕЧИ = frozenset(
    """жду ждем ждут ждите супер класс ясно согласен согласна благодарю алло извините
простите подскажите скажите пока возможно точно верно правильно именно отправил
отправила перезвоните перезвоню готово готов готова приехали приехал приехала
диагностика срочно удачи всё все прекрасно замечательно норм нормально ага угу
естественно обязательно отбой отмена помогите помогло помогли получилось заработало
заработал сделали сделал сделала посмотрите смотрите ответьте напишите позвонил
позвонила звоню пишу ответил ответила хочу хотим хотел хотела нужно нужны надо
авито ватсап вотсап вацап телеграм телеграме телеграмм вайбер вайбере вк сбер сбербанк
тинькофф целом общем принципе итоге наличии течение смысле основном любом другом этом
том районе доме машине сервисе магазине центре спб питере москве области крае
январе феврале марте апреле мае июне июле августе сентябре октябре ноябре декабре
выходные выходных будни будний рабочие первой второй третьей половине обед обеда
ремонте работе гараже квартиру комнату кухню ванную прихожей коридоре зале спальне
сети интернете личке личку личные личных сообщениях сообщении чате чат
ура стоп люди ребята девушка мужчина женщина мастер внимание важно спасите здорово круто
ужас кошмар блин черт ого вау наконец бегу еду иду выхожу дома нету""".split()
)
#: Оператор спросил имя — ответ одним словом («Наталья.») это имя, не пункт.
_СПРОСИЛ_ИМЯ = re.compile(
    r"как\s+(?:вас|к\s+вам|тебя)\s+(?:зовут|обращаться)|ваше\s+имя|как\s+зовут|представьтесь",
    re.IGNORECASE,
)


#: Формы, в которых пункт назван вне адреса, — и чего каждая требует.
#: «реплика» — реплика целиком одно название («Ялга»): в ремонтном чате так
#: же отвечают «Планшет» на «что чиним?» — годится только в ответ на вопрос
#: об адресе (или имя из справочника городов). «фраза» — восклицание в
#: начале («Ялга! Мы вам звонили!»): так подчёркивают поправку, предмет
#: ремонта восклицанием не называют. «в» — «в Сукко» внутри речи.
ФОРМА_РЕПЛИКА = "реплика"
ФОРМА_ФРАЗА = "фраза"
ФОРМА_В = "в"


@dataclasses.dataclass(frozen=True, slots=True)
class ПунктКонтекста:
    """Пункт, названный клиентом вне строки адреса: имя, канонический тип
    (None у голого имени — карта вправе его уступить дому в городе), цитата
    (слова клиента ровно как написаны — в `raw`, чтобы сторож цитаты и
    оператор видели, откуда пункт) и форма (`ФОРМА_*`)."""

    имя: str
    тип: str | None
    цитата: str
    форма: str

    @property
    def одним_словом(self) -> bool:
        """Такой ответ после «как вас зовут?» — имя человека, не пункт."""
        return self.форма != ФОРМА_В

    @property
    def нужен_вопрос(self) -> bool:
        """Годится только в ответ на вопрос оператора об адресе."""
        return self.форма == ФОРМА_РЕПЛИКА and city_by_name(self.имя) is None


def _пункт_в_реплике(текст: str, *, rules: address_parse.Rules = None) -> ПунктКонтекста | None:
    """Пункт в реплике, где нет ни адреса, ни места с типом (те — свои
    строки): реплика из одного названия, название восклицанием в начале,
    «в <Имя>» внутри речи. Тип у такого пункта неизвестен (None) — кроме
    реплики-места с типом («д. Заречье»), где тип назван.
    `rules` — политика правил разбора, та же, что у разбора самой реплики."""
    пункт = _реплика_целиком_пункт(текст, rules=rules)
    if пункт is not None:
        return пункт
    m = _ПУНКТ_ПЕРВОЙ_ФРАЗОЙ.match(текст)
    # Фамилия улицы восклицанием («Веселого! Жду») — та же улица, что и
    # отдельной репликой: тот же сторож, что у формы «реплика» без типа
    # (ревью 19.09: без него пункт «Веселого» дописывался в строку после
    # адреса и сбрасывал её вердикт).
    if (
        m is not None
        and _годное_имя_в_речи(m.group("name"))
        and not _фамилия_улицы(m.group("name"))
    ):
        return ПунктКонтекста(m.group("name"), None, m.group("name"), ФОРМА_ФРАЗА)
    for m in _В_ПУНКТЕ_ДВА.finditer(текст):
        # Двусловный город справочника в предложном падеже — канонизируется в
        # именительный сразу: падеж известен только здесь.
        город = city_by_name(m.group("name"), declined=True)
        if город is not None and _ЗА_ПУНКТОМ_НЕ_ПУНКТ.match(текст, m.end()) is None:
            return ПунктКонтекста(город.name, None, m.group(0), ФОРМА_В)
    for m in _В_ПУНКТЕ.finditer(текст):
        if not _годное_имя_в_речи(m.group("name")):
            continue
        if _ЗА_ПУНКТОМ_НЕ_ПУНКТ.match(текст, m.end()) is not None:
            continue
        # «в Салавате» → «Салават» (пакет 5): все дальнейшие `city_by_name(пункт.имя)`
        # остаются точными и начинают срабатывать — `locality` получает имя
        # справочника, а не падежную форму; цитата — слова клиента как есть.
        # Город-прилагательное («в Октябрьском», «в Дзержинском») без слова
        # «районе» — так же часто район города объявления, как и город
        # справочника: канонизация дала бы дом не там; остаётся пунктом как
        # написано, и область карты уступает его городу, как сегодня.
        город = city_by_name(m.group("name"), declined=True)
        имя = (
            город.name
            if город is not None and not _ГОРОД_ПРИЛАГАТЕЛЬНОЕ.search(город.name.lower())
            else m.group("name")
        )
        return ПунктКонтекста(имя, None, m.group(0), ФОРМА_В)
    return None


def _фамилия_улицы(имя: str) -> bool:
    """Голое слово в родительном падеже — фамилия улицы, не пункт. Правило
    одно на проект — `address_parse.is_street_surname` (им же подсказки карте
    отсеивают «Веселого»); здесь — имя для читателей пункта строки."""
    return address_parse.is_street_surname(имя)


def _реплика_целиком_пункт(
    текст: str, *, rules: address_parse.Rules = None
) -> ПунктКонтекста | None:
    """Реплика, которая целиком — название места: «Ялга», «Новое Заозерье»,
    «д. Заречье». Те же две ветки, что у `settlement_hints`, но БЕЗ
    имени перед запятой (ревью 18.09): «Ольга, сейчас уточню» для подсказок
    карте — годится (карта имя человека не найдёт и не помешает), а в пункт
    строки и в запрос к карте имя идти не должно. Голое слово в родительном
    падеже («Веселого») — улица, не пункт (`_фамилия_улицы`); с типом
    («п. Веселого») пункт остаётся пунктом — тип назвал сам клиент."""
    m = address_parse._РЕПЛИКА_ПУНКТ.match(текст)
    if m is not None and _годное_имя_в_речи(m.group("name")):
        имя = m.group("name").strip()
        без_типа = not текст[: m.start("name")].strip()
        if без_типа and _фамилия_улицы(имя):
            return None
        return ПунктКонтекста(имя, None, имя, ФОРМА_РЕПЛИКА)
    место = address_parse.parse_place(текст, rules=rules)
    if место is not None and место.settlement and _годное_имя_в_речи(место.settlement):
        return ПунктКонтекста(
            место.settlement, место.settlement_type, место.raw or текст.strip(), ФОРМА_РЕПЛИКА
        )
    return None


def _годное_имя_в_речи(имя: str) -> bool:
    """Стоп-списки — и по слову, и по слову без падежного окончания: за «в»
    слово стоит в предложном («в Ватсапе», «в Телеграме»), а список — в
    именительном; словарь окончаний ради этого не нужен."""
    if not address_parse._годное_имя_пункта(имя):
        return False
    for w in имя.lower().replace("ё", "е").split():
        формы = {w, w[:-1] if len(w) > 4 else w, w[:-2] if len(w) > 5 else w}
        if формы & _НЕ_ПУНКТ_В_РЕЧИ:
            return False
    return True


#: Сколько последних реплик оператора смотрим за именем и вопросом.
_ИСХОДЯЩИХ_ЗА_ИМЕНЕМ = 3


def _имя_человека(имя: str, *, client: Client | None, исходящие: list[str | None]) -> bool:
    """Слово с заглавной — имя человека, не пункт (ревью 18.09): так зовут
    клиента по карточке (первое слово имени) или так оператор написал в
    своих последних репликах — обратился к клиенту («Здравствуйте, Ольга!»)
    или подписался («Наталья, сервис»). Клиент чаще пишет имя оператора из
    подписи, чем своё, — потому смотрим исходящие, а не только карточку.
    Имя из справочника городов («Люберцы или Томилино?» → «Люберцы») —
    город, не человек."""
    if city_by_name(имя) is not None:
        return False
    слово = имя.lower().replace("ё", "е")
    первое = ((client.name if client is not None else None) or "").split()[:1]
    if первое and первое[0].lower().replace("ё", "е") == слово:
        return True
    образец = re.compile(rf"(?<![а-яё]){re.escape(слово)}(?![а-яё])")
    return any(образец.search((т or "").lower().replace("ё", "е")) for т in исходящие)


def _это_город_объявления(имя: str, conv: Conversation) -> bool:
    """«в Саранске» при объявлении в Саранске — не пункт, а тот же город."""
    город = conversation_city(conv)
    return город is not None and _тот_же_город(имя, город.name)


#: Гласные, «й» и мягкий знак: имя города на них меняет последнюю букву в
#: падеже («Тула» → «Туле», «Гай» → «Гае», «Пермь» → «Перми»); на согласную —
#: прибавляет («Бор» → «Бору», «Саранск» → «Саранске»).
_ГЛАСНАЯ = "аеиоуыэюяйь"
#: Падежное окончание не длиннее трёх букв («Пермью», «Челнах»): «Бор» и
#: «Борисоглебск» — разные города, а не падеж.
_ОКОНЧАНИЕ_НЕ_ДЛИННЕЕ = 3


def _тот_же_город(слово: str, город: str) -> bool:
    """Слово в любом падеже — имя города объявления? Без словаря окончаний:
    у имени срезается падежная буква, слово обязано начинаться с основы и
    отличаться от неё не больше чем окончанием. Ревью 18.09: прежняя мера
    «первые len−1, но не меньше четырёх букв» у имён из 3–4 букв сравнивала
    слово целиком, и «в Туле», «в Уфе», «в Чите» при объявлении там же шли
    пунктом. Многословное имя («Великий Новгород», «Ростов-на-Дону») —
    по любому своему слову: «в Новгороде», «в Ростове» — тот же город.
    """
    слово = слово.lower().replace("ё", "е")
    for часть in re.split(r"[\s-]+", город.lower().replace("ё", "е")):
        if len(часть) < 3 or len(слово) < 3:
            continue
        if слово == часть:
            return True
        основы = {часть}
        if часть[-2:] in ("ый", "ий", "ой"):
            # Имя-прилагательное («Грозный» → «Грозном», «Нижний» → «Нижнем»).
            основы.add(часть[:-2])
        elif часть[-1] in _ГЛАСНАЯ:
            основы.add(часть[:-1])
        elif len(часть) >= 4 and часть[-2] in "ео":
            # Беглая гласная: «Орёл» → «Орле», «Елец» → «Ельце» (согласная
            # может смягчиться — мягкий знак у слова не считаем).
            основы.add(часть[:-2] + часть[-1])
        for основа in основы:
            for форма in (слово, слово.replace("ь", "")):
                if (
                    форма.startswith(основа)
                    and форма != основа
                    and len(форма) - len(основа) <= _ОКОНЧАНИЕ_НЕ_ДЛИННЕЕ
                ):
                    return True
    return False


def client_speech(msg: Message) -> voice_service.Speech:
    """Речь клиента в реплике: тело, иначе расшифровка голосового ГОТОВО.

    ⚠ ЕДИНСТВЕННАЯ ТОЧКА, ГДЕ РАЗБОР КАРТОЧКИ УЗНАЁТ ТЕКСТ РЕПЛИКИ (19.09). У
    голосового `body = None`, а телефон и адрес читали только тело — расшифровка
    приезжала минуты спустя в `voice_transcript`, и её не читал никто (владелец:
    «не распознал номер из аудио»; замер за 30 дней: 708 расшифровок, строк от
    них — 0). Правило — в `voice.speech_of`; его SQL-двойник `voice.speech_sql`
    читают выборки соседних реплик, и расходиться им нельзя.
    """
    return voice_service.speech_of(msg.body, msg.voice_transcript, msg.voice_transcript_status)


def _та_же_улица(слово: str, улица: str | None) -> bool:
    """Реплика из одного слова повторяет улицу найденного адреса: «Веселого»
    → вопрос оператора → «Веселого 5» (владелец 19.09). Сравнение — по ядру
    улицы (без типа, регистра и «ё»): «Лесная» и «ул. Лесная 5» — одна улица."""
    ядро = address_parse.street_core(улица)
    return bool(ядро) and address_parse.street_core(слово) == ядро


async def _пункт_из_соседних_реплик(
    db: AsyncSession,
    conv: Conversation,
    *,
    client: Client,
    before: datetime,
    улица: str | None = None,
    rules: address_parse.Rules = None,
) -> ПунктКонтекста | None:
    """Пункт из реплик клиента ДО этой — ближайший первым; за сутки и не
    дальше `_КОНТЕКСТ_РЕПЛИК` входящих. `улица` — улица найденного адреса:
    реплика из одного слова, повторяющая её, — не пункт (та же улица,
    названная дважды), смотрим дальше назад.

    Только до ближайшей реплики, из которой уже вышла строка адреса или места:
    пункт, названный перед ТЕМ адресом, — его пункт, а не этого (стенд 15.09:
    «СНТ Ромашка» → «ул Ленина 5» → «кв 3» → «ул Пушкина 7» — Пушкина без
    массива); место отдельной репликой к дому клеит `_место_к_дому` по строке
    места. Анкеты Авито («Вот подробности…») пропускаются: их поля — не речь
    клиента о месте. Реплики оператора нужны только как сторож: слово после
    «как вас зовут?» — имя, не пункт; слово, которым оператор обратился к
    клиенту или подписался, — тоже имя (`_имя_человека`).
    """
    # Речь, а не тело: пункт, названный голосом («это посёлок Ударник»), иначе
    # терялся бы — у голосового тело пусто. У исходящих расшифровки не бывает,
    # для них `speech_sql` отдаёт тело (первая ветвь `case`).
    лента = (
        await db.execute(
            select(Message.id, Message.direction, voice_service.speech_sql().label("речь"))
            .where(
                Message.conversation_id == conv.id,
                Message.direction.in_(("in", "out")),
                Message.created_at < before,
                Message.created_at >= before - _ОКНО_ВОПРОСА,
            )
            .order_by(Message.created_at.desc())
            .limit(_КОНТЕКСТ_РЕПЛИК * 3)
        )
    ).all()
    входящие = [id_ for id_, направление, _ in лента if направление == "in"]
    if not входящие:
        return None
    с_адресом = set(
        (
            await db.execute(
                select(ClientAddressCandidate.message_id).where(
                    ClientAddressCandidate.conversation_id == conv.id,
                    ClientAddressCandidate.message_id.in_(входящие[:_КОНТЕКСТ_РЕПЛИК]),
                )
            )
        ).scalars()
    )
    входящих = 0
    for номер, (id_, направление, речь) in enumerate(лента):
        if направление != "in":
            continue
        входящих += 1
        if входящих > _КОНТЕКСТ_РЕПЛИК or id_ in с_адресом:
            break
        if not речь or address_parse.address_text(речь) != речь:
            continue
        пункт = _пункт_в_реплике(речь, rules=rules)
        if пункт is None or _это_город_объявления(пункт.имя, conv):
            continue
        if пункт.одним_словом and _та_же_улица(пункт.имя, улица):
            continue
        # Ближайшие реплики оператора перед этой: «как вас зовут?» — слово
        # после неё имя, как и имя из обращения или подписи оператора; голое
        # название годится только после вопроса об адресе.
        исходящие = [т for _, н, т in лента[номер + 1 :] if н == "out" and т][:_ИСХОДЯЩИХ_ЗА_ИМЕНЕМ]
        вопрос = исходящие[0] if исходящие else None
        if пункт.одним_словом and (
            (вопрос and _СПРОСИЛ_ИМЯ.search(вопрос))
            or _имя_человека(пункт.имя, client=client, исходящие=исходящие)
        ):
            continue
        if пункт.нужен_вопрос and not (вопрос and _ВОПРОС_ОБ_АДРЕСЕ.search(вопрос)):
            continue
        return пункт
    return None


async def _последние_исходящие(
    db: AsyncSession, conv: Conversation, *, before: datetime
) -> list[str | None]:
    """Тексты последних реплик оператора за сутки, свежие первыми: по первой
    — «как вас зовут?», по всем — имя, которым он обратился или подписался."""
    return list(
        (
            await db.execute(
                select(Message.body)
                .where(
                    Message.conversation_id == conv.id,
                    Message.direction == "out",
                    Message.created_at < before,
                    Message.created_at >= before - _ОКНО_ВОПРОСА,
                )
                .order_by(Message.created_at.desc())
                .limit(_ИСХОДЯЩИХ_ЗА_ИМЕНЕМ)
            )
        ).scalars()
    )


def с_пунктом_контекста(found: address_parse.Found, пункт: ПунктКонтекста) -> address_parse.Found:
    """Пункт из соседней реплики — к улице/дому без пункта; цитата — склейка
    «пункт; адрес», как у `_место_к_дому`. Имя из справочника городов — город
    клиента (`locality`): карта ищет дом в нём, а не пункт внутри города."""
    raw = f"{пункт.цитата}; {found.raw}"
    if пункт.тип is None and city_by_name(пункт.имя) is not None:
        return dataclasses.replace(found, raw=raw, locality=пункт.имя)
    return dataclasses.replace(found, raw=raw, settlement=пункт.имя, settlement_type=пункт.тип)


async def _дом_к_месту(
    db: AsyncSession,
    *,
    client: Client,
    conv: Conversation,
    msg: Message,
    now: datetime,
    rules: address_parse.Rules = None,
) -> address_parse.Found | None:
    """«Дом 17» отдельной репликой — продолжение места, названного раньше.

    Владелец 13.09 («адрес постепенно»): «пгт Заречный, мкр Южный» одним
    сообщением, «дом 17» — следующим. Место берётся ТОЛЬКО из этого диалога и
    только за сутки (`_ОКНО_ВОПРОСА`): дом к месту из другого заказа не
    относится. Улица места (или массив, или сам пункт) становится улицей дома
    — как у «посёлок Сосново дом 9» в одной реплике.
    """
    речь = client_speech(msg).text or ""
    дом = address_parse.house_only(речь)
    фраза = address_parse.house_phrase(речь)
    if дом is None or фраза is None:
        return None
    когда = _aware_utc(msg.created_at)
    место = await clients.latest_address_candidate(
        db, client_id=client.id, conversation_id=conv.id, since=когда - _ОКНО_ВОПРОСА, before=now
    )
    if место is None:
        return None
    # Поправка «дом 23» после склеенного «дом 17»: последняя строка — дом,
    # собранный из места (цитата из двух реплик), — продолжаем то же место.
    склеенный = место.kind == address_parse.KIND_HOUSE and "; " in (место.raw or "")
    if место.kind != address_parse.KIND_PLACE and not склеенный:
        return None
    # У склеенного дома цитата — «место; дом»: берём место. У места-улицы,
    # унаследовавшей пункт, цитата — «пункт; улица»: берём целиком, иначе
    # сторож цитаты не найдёт улицу (стенд 14.09, «пункт → улица → дом»).
    цитата_места = (место.raw or "").split("; ")[0] if склеенный else (место.raw or "")
    if склеенный:
        улица, пункт_как_улица = (место.street or "").strip(), False
        # Пункт из соседней реплики стоит впереди своим куском («Ялга;
        # Садовая 3» — 18.09), улица — во втором: берём куски до того, где
        # улица названа, иначе сторож цитаты её не найдёт.
        куски = (место.raw or "").split("; ")
        слова_улицы = [w for w in re.split(r"[^0-9a-zа-яё]+", улица.lower()) if w]
        for n in range(1, len(куски) + 1):
            цитата_места = "; ".join(куски[:n])
            if all(w in цитата_места.lower() for w in слова_улицы):
                break
    else:
        # Улица места, иначе массив («10 мкр» → «10 мкр, 15», как у одной
        # реплики), иначе сам пункт — тогда улицы у дома нет, и вердикт
        # сверяет пункт («посёлок Сосново, 9»).
        улица = (место.street or "").strip() or (
            address_parse.area_as_street(место.area) if место.area else ""
        )
        пункт_как_улица = not улица and bool(место.settlement)
        if not улица and not место.settlement:
            return None
        if пункт_как_улица:
            # «ул Ленина 5» → «это посёлок Ударник» → «дом 7»: улица названа
            # раньше в этом же диалоге — дом к ней, а не к голому пункту.
            ранний = await clients.latest_house_candidate(
                db,
                client_id=client.id,
                conversation_id=conv.id,
                since=когда - _ОКНО_ВОПРОСА,
                before=now,
            )
            if ранний is not None and (ранний.street or "").strip():
                улица, пункт_как_улица = (ранний.street or "").strip(), False
                цитата_места = f"{ранний.raw}; {цитата_места}"
    # Части, названные между местом и домом («кв 36, подъезд 4»), — у места;
    # дому они нужнее, свои части реплики с домом сверху.
    части = {
        ч: getattr(место, ч)
        for ч in ("office", "entrance", "floor", "intercom")
        if getattr(место, ч, None)
    }
    части.update(address_parse.parts_only(речь, rules=rules))
    return address_parse.Found(
        street=улица,
        house=дом,
        raw=f"{цитата_места}; {фраза}",
        start=0,
        end=0,
        level=место.level,
        parts=части,
        settlement=место.settlement,
        settlement_type=место.settlement_type,
        locality=место.locality,
        district=место.district,
        area=место.area,
    )


async def _город_пары_из_других_диалогов(
    db: AsyncSession,
    тело: str,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    имя_города: str | None,
    rules: address_parse.Rules = None,
) -> str | None:
    """Город, по которому разбирать пару «N-M»/«N/M» в реплике, когда город
    объявления сам не квартальный и не комплексный: первый по свежести
    квартальный (при паре через дефис) или комплексный (при паре через дробь)
    город среди ДРУГИХ диалогов клиента (`other_conversation_cities`). Нет
    пары — в базу не ходим; город объявления из этих словарей решает сам
    (`_maybe_extract_address`), сюда не попадает.

    В базу — только за парой, которую разбор пары вообще способен принять
    (ревью 19.09): «2-3 дня», «500-700 рублей», «через 30-40 минут», «19/09
    в 10» режутся уликами `parse_quarter_pair`/`parse_complex_pair` (единица,
    цена, диапазон, дата) до запроса, а не после. Разбор здесь — с
    `про_адрес`: вопрос оператора ещё не считан, и пара «как время» целиком
    репликой («10-12») в базу идёт — решит вызывающий, тем же порядком, что
    живой путь. Тип квартала — «квартал»: у него шире ветка («93 кв,31»),
    имя города на приём пары не влияет.
    """
    if (
        имя_города in address_parse.КВАРТАЛЬНЫЕ_ГОРОДА
        or имя_города in address_parse.КОМПЛЕКСНЫЕ_ГОРОДА
    ):
        return None
    квартальная = (
        address_parse.quarter_pair_present(тело)
        and address_parse.parse_quarter_pair(тело, "квартал", про_адрес=True, rules=rules)
        is not None
    )
    комплексная = (
        address_parse.complex_pair_present(тело)
        and address_parse.parse_complex_pair(тело, про_адрес=True, rules=rules) is not None
    )
    if not (квартальная or комплексная):
        return None
    for город in await other_conversation_cities(db, client_id, conversation_id):
        if квартальная and город.name in address_parse.КВАРТАЛЬНЫЕ_ГОРОДА:
            return город.name
        if комплексная and город.name in address_parse.КОМПЛЕКСНЫЕ_ГОРОДА:
            return город.name
    return None


async def разбор_реплики_сейчас(
    db: AsyncSession,
    conv: Conversation,
    msg: Message,
    *,
    client_id: uuid.UUID,
    rules: address_parse.Rules = None,
) -> address_parse.Found | None:
    """Дом в реплике НЫНЕШНИМИ правилами — тем порядком, что у живого пути
    (`_maybe_extract_address`), но по одной реплике, без строк-соседей: разбор;
    `про_адрес` — после вопроса оператора; формы города объявления
    («85-11» в Ангарске); пара по квартальному/комплексному городу ДРУГОГО
    диалога клиента (19.09). Для догона (`address-reparse`) и сторожа автозаписи:
    строки хранят разбор того дня, когда пришла реплика, а судить их надо
    сегодняшним. `rules` — политика правил разбора; пусто — читается из
    настройки (один раз на вызов), как у живого пути: два пути не должны
    разбирать одну реплику под разной политикой.
    """
    rules = rules if rules is not None else await parse_rules(db)
    body = address_parse.address_text(client_speech(msg).text)
    when = _aware_utc(msg.created_at)
    город = conversation_city(conv)
    имя_города = город.name if город else None
    # Вопрос оператора считается один раз и лениво: в ленту идём, только когда
    # невод промолчал, и не ходим второй раз ради формы города (ревью 19.09).
    спросил: bool | None = None

    async def после_вопроса() -> bool:
        nonlocal спросил
        if спросил is None:
            спросил = await _оператор_спросил_адрес(db, conv, before=when)
        return спросил

    found = address_parse.parse(body, rules=rules)
    if found is None or found.level == address_parse.LEVEL_C:
        if await после_вопроса():
            found = address_parse.parse(body, про_адрес=True, rules=rules) or found
    if found is None or found.level == address_parse.LEVEL_C:
        # Форма города — тем же порядком, что живой путь: без `про_адрес`, с
        # ним — только после вопроса. Первая редакция передавала `про_адрес`
        # сразу, и «10-12» целиком репликой в Ангарске становилось адресом без
        # вопроса — второй счёт того же поля (ревью 19.09).
        пара = address_parse.parse_by_city(body, имя_города, rules=rules)
        if пара is None and await после_вопроса():
            пара = address_parse.parse_by_city(body, имя_города, про_адрес=True, rules=rules)
        found = пара or found
    if found is None or found.level == address_parse.LEVEL_C:
        свой = await _город_пары_из_других_диалогов(
            db,
            body,
            client_id=client_id,
            conversation_id=conv.id,
            имя_города=имя_города,
            rules=rules,
        )
        if свой is not None:
            пара = address_parse.parse_by_city(body, свой, rules=rules)
            if пара is None and await после_вопроса():
                пара = address_parse.parse_by_city(body, свой, про_адрес=True, rules=rules)
            if пара is not None:
                found = dataclasses.replace(пара, locality=свой)
    return found


def реплика_без_адреса(msg: Message, found: address_parse.Found | None) -> bool:
    """ЕДИНСТВЕННЫЙ предикат «реплика без адреса по нынешнему разбору»
    (владелец 19.09: «Камера 4G» — дом «4G» старого разбора лёг в карточку
    через точку улицы). `found` — итог :func:`разбор_реплики_сейчас` по этой же
    реплике. Читают: догон `address-reparse` (отклоняет такие строки) и сторож
    автозаписи (`workers/geocode.autofill_address` такую строку в карточку не
    кладёт).

    Не «без адреса»: реплика с телом, где нынешний разбор дома не видит, но
    - геоточка Авито — адрес лежит во вложении, тела у неё нет или оно не про
      адрес (`записать_геоточку`);
    - «дом 9» — продолжение места из прошлой реплики (`_дом_к_месту`), в ней
      только дом, и это не речь;
    - речи нет вовсе (сообщение-вложение без расшифровки) — судить нечего.

    Анкета Авито с телом, но без адресных полей («стены, 244», ревью 15.09) —
    без адреса: судится адресный текст анкеты, а не вся она. Речь — через
    `client_speech`, как у разбора: строка от голосового судится по той же
    расшифровке, из которой родилась, а не по пустому телу.
    """
    if found is not None:
        return False
    речь = client_speech(msg).text
    if not (речь or "").strip() or _геоточка(msg.attachments) is not None:
        return False
    body = address_parse.address_text(речь)
    return address_parse.house_phrase(body) is None and address_parse.house_only(body) is None


async def parse_rules(db: AsyncSession) -> dict[str, str]:
    """Политика правил разбора для ОДНОГО входящего — настройка
    `address_parse.rules` поверх умолчаний `address_parse.PARSE_RULES`
    (пакет 6.0а, программа §0.3). Читается один раз на реплику вызывающим и
    передаётся во все разборы этой реплики (`parse`, формы города, место,
    пункт из контекста, ворота модели): разбор, сторож автозаписи и модель
    судят одну реплику под одной политикой. Внутри прохода (`one_pass`)
    чтение — из снимка настроек, повтор бесплатен. Читатель настроек отдаёт
    строку как есть (проверка — только на записи), поэтому разбор нестрогий:
    устаревшее имя правила после выкатки пропускается с предупреждением, а не
    роняет приём каждого входящего и не сбрасывает соседние пары."""
    return address_parse.rules_from_setting(
        await app_settings.get(db, app_settings.ADDRESS_PARSE_RULES), strict=False
    )


#: Эхо адреса мастерской (пакет 7а, Q24): исходящие того же диалога за неделю
#: до реплики — «привезу сам, куда?» и ответ оператора живут в одном диалоге,
#: но не обязательно в одни сутки (`_ОКНО_ВОПРОСА`).
_ОКНО_ЭХА = timedelta(days=7)
#: Сколько последних исходящих, похожих на адрес, разбирать на реплику.
_ЭХО_ИСХОДЯЩИХ = 20
ЭХО_СВОЙ_СПИСОК = "own_list"
ЭХО_ДИАЛОГА = "dialog_echo"


async def _эхо_мастерской(
    db: AsyncSession,
    conv: Conversation,
    msg: Message,
    found: address_parse.Found,
    *,
    rules: address_parse.Rules,
) -> str | None:
    """Откуда клиент взял этот дом — из наших слов? `ЭХО_СВОЙ_СПИСОК` — ключ
    «улица+дом» есть в списке своих адресов (владельца или выведенном задачей);
    `ЭХО_ДИАЛОГА` — тот же ключ прозвучал в исходящей этого диалога РАНЬШЕ
    первого упоминания клиентом; `None` — адрес клиента.

    ⚠ ПОРЯДОК РЕПЛИК, А НЕ УРОВЕНЬ. Повтор оператором «ул. Ленина 5, верно?»
    разбирается тем же ключом, что слова клиента, — различить их можно только
    тем, кто назвал адрес первым. Первое клиентское упоминание ищется по
    ТЕКСТУ входящих (`address_own.text_mentions`), не по строкам: «да это дом
    5 по ленина» разбор не берёт, а упоминание это. Зовётся только при
    правиле `workshop_echo` не `off`: при `off` — ни одного запроса.
    """
    ключ = address_own.key_of(found)
    if ключ is None:
        return None
    # Обе настройки — один раз на реплику; внутри прохода (`one_pass`) чтение
    # из снимка, бесплатно.
    свои = address_own.own_keys(
        await app_settings.get(db, app_settings.ADDRESS_OWN_ADDRESSES),
        await app_settings.get(db, app_settings.ADDRESS_OWN_ADDRESSES_AUTO),
    )
    if ключ in свои:
        return ЭХО_СВОЙ_СПИСОК
    когда = _aware_utc(msg.created_at)
    # Один SELECT исходящих на реплику с домом: серверное сито «похоже на
    # адрес» (то же, что у воронки) — разбор гоняется по ≤ 20 строкам, а не по
    # всем ответам оператора за неделю.
    исходящие = (
        await db.execute(
            select(Message.body, Message.created_at)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == "out",
                Message.sender_type.in_(("operator", "bot")),
                Message.body.is_not(None),
                Message.created_at >= когда - _ОКНО_ЭХА,
                Message.created_at < когда,
                Message.body.regexp_match(address_funnel.ADDRESS_LIKE_PATTERN),
                Message.body.regexp_match(address_funnel.DIGIT_PATTERN),
            )
            .order_by(Message.created_at.desc())
            .limit(_ЭХО_ИСХОДЯЩИХ)
        )
    ).all()
    первая_наша: datetime | None = None
    for текст, когда_наша in исходящие:
        наш = address_parse.parse(текст or "", про_адрес=True, rules=rules)
        if address_own.key_of(наш) == ключ:
            первая_наша = _aware_utc(когда_наша)
    if первая_наша is None:
        return None
    # Клиент называл этот дом раньше нашей первой исходящей с ним? Тогда мы
    # повторили ЕГО адрес — сторож молчит.
    входящие = (
        await db.execute(
            select(Message.body, Message.voice_transcript)
            .where(
                Message.conversation_id == conv.id,
                Message.direction == "in",
                Message.created_at >= когда - _ОКНО_ЭХА,
                Message.created_at < первая_наша,
            )
            .order_by(Message.created_at)
        )
    ).all()
    for тело, расшифровка in входящие:
        for текст in (тело, расшифровка):
            if not текст:
                continue
            if address_own.key_of(
                address_parse.parse(текст, про_адрес=True, rules=rules)
            ) == ключ or address_own.text_mentions(текст, ключ):
                return None
    return ЭХО_ДИАЛОГА


async def _maybe_extract_address(
    db: AsyncSession,
    conv: Conversation,
    client: Client,
    msg: Message,
    *,
    now: datetime,
    rules: address_parse.Rules = None,
) -> tuple[str | None, tuple[uuid.UUID, ...], bool]:
    """Вычитать адрес из входящего и положить его РЯДОМ с карточкой.

    Возвращает причину кадра для экрана (или ``None``), идентификаторы строк,
    которые надо проверить по карте (0, 1 или 2 — дом с дописанным пунктом
    идёт вместе со строкой места, N13), и признак «правила
    промолчали» — для чтения моделью (13.09); он ``True`` только когда разбор
    включён и не нашёл ничего, а не когда адрес прочитан и предлагать нечего.
    Проверку ставит вызывающий
    СТРОГО ПОСЛЕ COMMIT'А: задача, поставленная отсюда, прибежала бы к строке,
    которой в базе ещё нет. Сам кадр публикуется
    ПОСЛЕ commit'а — тем же порядком, что у телефона, и по той же причине:
    опубликованный внутри транзакции кадр заставляет экран перечитать ещё
    старую строку, и правка появляется только после F5 (жалоба 02.09).

    ⚠ ОТСЮДА В КАРТОЧКУ НЕ ПИШЕТСЯ НИЧЕГО. Автозапись с 11.09 есть, но живёт
    в задаче `workers/geocode.py::autofill_address` и только ПОСЛЕ того, как
    карта подтвердила дом и прошли все сторожа вердикта. Здесь строка лишь
    записывается и отдаётся на проверку.

    ⚠ УРОВЕНЬ РЕШАЕТ, ПОКАЗЫВАТЬ ЛИ, НО НЕ РЕШАЕТ, ЗАПИСЫВАТЬ ЛИ. Строка
    заводится на любом уровне: замер качества на живом трафике нужен и по тем
    находкам, которых оператор не увидит. Показ отбирает `identity_view` по
    настройке `ADDRESS_DETECT_LEVELS` (умолчание — только `A`).
    """
    if not await app_settings.get(db, app_settings.ADDRESS_DETECT_ENABLED):
        return None, (), False
    # Политика правил разбора — один раз на входящее, во все разборы ниже.
    rules = rules if rules is not None else await parse_rules(db)
    речь = client_speech(msg)
    # Анкета Авито — только её поля с адресом (стенд 15.09: «вторник, 25»).
    тело = address_parse.address_text(речь.text)
    геоточка = _геоточка(msg.attachments)
    if геоточка is not None:
        записано_точкой = await записать_геоточку(db, client=client, conv=conv, msg=msg, now=now)
        if записано_точкой is not None and записано_точкой.candidate_id is not None:
            # Точка без координат (сообщения до 15.09 — только подпись) идёт
            # к карте как обычная строка.
            на_карту = (
                записано_точкой.candidate_id
                if геоточка["lat"] is None
                and await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED)
                else None
            )
            return (
                ("address_suggested" if записано_точкой.впервые else "address_refined"),
                (на_карту,) if на_карту is not None else (),
                False,
            )
    found = address_parse.parse(тело, rules=rules)
    # Город клиента, взятый из его ДРУГИХ диалогов, а не из слов этой реплики
    # (пара в чужом городе, ниже): сторож цитаты его в тексте не ищет.
    город_из_диалогов: str | None = None
    # …и когда правила молчат вовсе (стенд 14.09: «гоголя 12», «садовая
    # 48», «мира 131 2 подьезд» — строчными в ответ на вопрос оператора):
    # ветка ниже раньше требовала строку уровня C, а строчные её не давали.
    if found is None and _ПОХОЖЕ_НА_ОТВЕТ_АДРЕСОМ.search(тело):
        if await _оператор_спросил_адрес(db, conv, before=_aware_utc(msg.created_at)):
            found = address_parse.parse(тело, про_адрес=True, rules=rules)
    elif found is not None and found.level == address_parse.LEVEL_C:
        # ⚠ «НАЗВАНИЕ ЧИСЛО» В ОТВЕТ НА ВОПРОС ОПЕРАТОРА — ЭТО АДРЕС (бой 12.09:
        # шесть скриншотов владельца подряд — «Садовая 24-31», «Лесная
        # 16», «Гоголя 15/3-12» после «куда к вам подъехать?»). Признак
        # `про_адрес` у разбора был с 09.09, но сюда его никто не передавал —
        # уровень B «почти не встречался» ровно поэтому. Запрос к ленте только
        # когда невод уже сработал (5 % входящих), не на каждое сообщение.
        if await _оператор_спросил_адрес(db, conv, before=_aware_utc(msg.created_at)):
            found = address_parse.parse(тело, про_адрес=True, rules=rules) or found
    if found is None or found.level == address_parse.LEVEL_C:
        # «85-11» В АНГАРСКЕ — 85-й квартал, дом 11 (владелец 14.09): пара
        # чисел без слова «квартал» — адрес только в городах, где так пишут.
        # Пара «как время» («10-12») всей репликой — адрес лишь в ответ на
        # вопрос об адресе; в ленту за вопросом идём, только когда пара есть.
        # Строку C невод даёт и на «Ангарск 85-11» («Ангарск, 85», кв 11) —
        # пара, если она есть, точнее (ревью 14.09).
        город = conversation_city(conv)
        имя_города = город.name if город else None
        if имя_города in address_parse.КВАРТАЛЬНЫЕ_ГОРОДА and address_parse.quarter_pair_present(
            тело
        ):
            пара = address_parse.parse_by_city(тело, имя_города, rules=rules)
            if пара is None and await _оператор_спросил_адрес(
                db, conv, before=_aware_utc(msg.created_at)
            ):
                пара = address_parse.parse_by_city(тело, имя_города, про_адрес=True, rules=rules)
            found = пара or found
        elif имя_города in address_parse.КОРПУСНЫЕ_ГОРОДА:
            # «К.512, 2 под, 7 эт, кв.64» в Зеленограде (стенд 15.09).
            found = address_parse.parse_by_city(тело, имя_города, rules=rules) or found
        elif имя_города in address_parse.КОМПЛЕКСНЫЕ_ГОРОДА and address_parse.complex_pair_present(
            тело
        ):
            # «38/07» В НАБЕРЕЖНЫХ ЧЕЛНАХ — 38-й комплекс, дом 7 (владелец
            # 18.09): пара «комплекс/дом» без слова «комплекс» — адрес только
            # там, где так пишут; тем же порядком, что квартальная пара выше.
            пара = address_parse.parse_by_city(тело, имя_города, rules=rules)
            if пара is None and await _оператор_спросил_адрес(
                db, conv, before=_aware_utc(msg.created_at)
            ):
                пара = address_parse.parse_by_city(тело, имя_города, про_адрес=True, rules=rules)
            found = пара or found
        else:
            # ПАРА В ЧУЖОМ ГОРОДЕ (владелец 19.09): «по адресу 12б-73 на 10.30»
            # при объявлении в Саранске — от клиента, который по другому
            # объявлению писал из Нефтеюганска. Объявление висит где угодно, а
            # адрес пишут так, как принято дома: пара разбирается по
            # квартальному/комплексному городу ДРУГОГО диалога клиента, и он
            # же — город клиента (`locality`), чтобы карта искала дом там.
            # Порядок тот же: без `про_адрес`, потом с ним после вопроса.
            свой = await _город_пары_из_других_диалогов(
                db,
                тело,
                client_id=client.id,
                conversation_id=conv.id,
                имя_города=имя_города,
                rules=rules,
            )
            if свой is not None:
                пара = address_parse.parse_by_city(тело, свой, rules=rules)
                if пара is None and await _оператор_спросил_адрес(
                    db, conv, before=_aware_utc(msg.created_at)
                ):
                    пара = address_parse.parse_by_city(тело, свой, про_адрес=True, rules=rules)
                if пара is not None:
                    found = dataclasses.replace(пара, locality=свой)
                    город_из_диалогов = свой
    if found is None:
        # МЕСТО БЕЗ УЛИЦЫ (владелец 13.09): «Гатчинский р-н. Д. Заречье,
        # массив Южный» — улицы ещё нет, а точка на карте нужна уже сейчас;
        # улица придёт следующей репликой и обновит адрес (`autofill_address`).
        found = address_parse.parse_place(тело, rules=rules)
    цитата = тело
    if found is None:
        found = await _дом_к_месту(db, client=client, conv=conv, msg=msg, now=now, rules=rules)
        if found is not None:
            # Улица и пункт живут в реплике с местом, дом — в этой: сторож
            # цитаты сверяет склейку обеих, и обе — слова клиента.
            цитата = found.raw
    elif found.settlement is None and found.area is None and not found.locality:
        # «СНТ Берёзка» → «Садовый проезд 4» (бой 13.09, Иркутск): место из
        # прошлой реплики — в сам запрос к карте, а не только на сверку;
        # без него DaData находила «Садовый пр-д, 4» в Саянске. И улица без
        # дома после пункта — тоже (владелец 14.09, «адрес по частям: пункт,
        # улица, дом»): «посёлок Сосново» → «ул Лесная» → «дом 5» — пункт
        # едет с улицей, а от неё — к дому.
        if found.kind == address_parse.KIND_HOUSE or (
            found.kind == address_parse.KIND_PLACE and found.street
        ):
            found = (
                await _место_к_дому(db, client=client, conv=conv, msg=msg, found=found, now=now)
                or found
            )
            цитата = found.raw if "; " in found.raw else цитата
    if (
        found is not None
        and found.settlement is None
        and found.area is None
        and not found.locality
        and (found.kind == address_parse.KIND_HOUSE or found.street)
    ):
        # ПУНКТ ИЗ СОСЕДНЕЙ РЕПЛИКИ (владелец 18.09): строки-места в диалоге
        # нет, а пункт назван — в речи («нам нужен сантехник в Сукко»),
        # одним словом («Ялга»), первой фразой («Ялга! Мы вам звонили!»).
        # В строку — ДО карты: без «Ялга» в запросе DaData отдаёт Садовая 3
        # в самом Саранске, и сверять подсказкой уже нечего. Голое имя идёт
        # без типа — область его не знает, карта уступит дому в городе
        # (`_пункт_уступает_городу`); цитата — обе реплики, сторож цитаты
        # сверяет склейку.
        пункт = await _пункт_из_соседних_реплик(
            db,
            conv,
            client=client,
            before=_aware_utc(msg.created_at),
            улица=found.street,
            rules=rules,
        )
        if пункт is not None:
            found = с_пунктом_контекста(found, пункт)
            цитата = found.raw
    if found is None:
        # ПУНКТ, НАЗВАННЫЙ ПОСЛЕ УЛИЦЫ (владелец 18.09): «Садовая 3 кв 21»
        # → «Ялга!» — дописывается в последнюю строку диалога без пункта, как
        # «кв 3» ниже, и строка идёт к карте заново. Ответ одним словом на
        # «как вас зовут?» — имя, не пункт; имя, которым оператор обратился
        # к клиенту или подписался, — тоже (ревью 18.09: «Ольга! Спасибо»).
        # «В <Слово>» внутри речи после адреса — только в ответ на вопрос об
        # адресе (ревью 18.09): «Всё в Порядке», «в Ленте купил», «работаю в
        # Газпроме» иначе переписывали пункт подтверждённой строки и
        # сбрасывали её вердикт; стоп-список слов за «в» конечен, а речь — нет.
        перепроверить: uuid.UUID | None = None
        дописан_пункт = False
        пункт = _пункт_в_реплике(тело, rules=rules)
        if пункт is not None and not _это_город_объявления(пункт.имя, conv):
            когда = _aware_utc(msg.created_at)
            исходящие = await _последние_исходящие(db, conv, before=когда)
            это_имя = пункт.одним_словом and (
                bool(исходящие and исходящие[0] and _СПРОСИЛ_ИМЯ.search(исходящие[0]))
                or _имя_человека(пункт.имя, client=client, исходящие=исходящие)
            )
            нужен_вопрос = пункт.нужен_вопрос or пункт.форма == ФОРМА_В
            годится = not это_имя and (
                not нужен_вопрос or await _оператор_спросил_адрес(db, conv, before=когда)
            )
            if годится:
                город_клиента = пункт.тип is None and city_by_name(пункт.имя) is not None
                строка = await clients.refine_address_settlement(
                    db,
                    client_id=client.id,
                    conversation_id=conv.id,
                    settlement=None if город_клиента else пункт.имя,
                    settlement_type=пункт.тип,
                    locality=пункт.имя if город_клиента else None,
                    quote=пункт.цитата,
                    since=когда - _ОКНО_ВОПРОСА,
                    before=now,
                )
                if строка is not None:
                    дописан_пункт = True
                    перепроверить = (
                        строка.id
                        if await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED)
                        else None
                    )
        # ⚠ АДРЕС НАЗЫВАЮТ ПО ЧАСТЯМ, И ВТОРАЯ РЕПЛИКА ГОЛОВЫ НЕ СОДЕРЖИТ.
        # «Ленина 5», а следом «кв 3, второй подъезд» — так устроены 23,5 %
        # адресных диалогов по замеру боя. Без этой ветки квартира терялась бы у
        # каждого четвёртого адреса: мастер приезжает к подъезду и звонит
        # уточнять. Пункт и части бывают в одной реплике («Ялга! кв 21, 3
        # подъезд», ревью 18.09) — ветка пункта не возвращается раньше этой.
        части = address_parse.parts_only(тело, rules=rules)
        дописали = await clients.refine_address_parts(
            db, client_id=client.id, conversation_id=conv.id, parts=части, before=now
        )
        # СТОП-КЛАСС РЕЧИ (пакет 7a, Q16): разбор промолчал — если реплику снял
        # класс в `on`, одна строка журнала на реплику (счётчик снятых — grep
        # по `address.stop_class`; в `parse` не пишется: он зовётся ≥ 5 раз на
        # входящее). Уровень — тот, что был бы без классов. Слов клиента нет.
        if (стоп_класс := address_parse.stopped_by(тело, rules)) is not None:
            log.info(
                "address.stop_class",
                rule=стоп_класс,
                level=address_parse.gate(
                    тело, rules=dict.fromkeys(address_parse.СТОП_КЛАССЫ, address_parse.PARSE_OFF)
                ),
                state=address_parse.PARSE_ON,
                conversation_id=str(conv.id),
                client_id=str(client.id),
            )
        return (
            ("address_refined" if (дописали or дописан_пункт) else None),
            (перепроверить,) if перепроверить is not None else (),
            True,
        )
    # ⚠ СТОРОЖ ЦИТАТЫ — ПОСЛЕДНИЙ ЗАСЛОН ПЕРЕД ЗАПИСЬЮ. Правила выдумывать не
    # умеют по построению (замер боя: не сошёлся ни разу из 6 396), но строка,
    # которой нет в сообщении, не имеет права попасть в карточку ни при каких
    # будущих правках разбора.
    # Город из других диалогов клиента — не его слова в этой реплике: сторож
    # сверяет улицу, дом и пункт, а город здесь известен так же, как город
    # объявления, — по данным диалогов, не по цитате.
    слова_клиента = (
        dataclasses.replace(found, locality=None)
        if город_из_диалогов and found.locality == город_из_диалогов
        else found
    )
    if not address_parse.quote_holds(слова_клиента, цитата):
        log.warning(
            "address.quote_mismatch",
            conversation_id=str(conv.id),
            client_id=str(client.id),
            level=found.level,
        )
        return None, (), False
    # ⚠ ЭХО АДРЕСА МАСТЕРСКОЙ (пакет 7а, Q24). «Привезу сам, куда?» → «ул.
    # Невская 7а» → «подъеду на Невскую 7а»: дом настоящий, но НАШ — карта его
    # подтвердит, и в карточке клиента окажется адрес мастерской. Строка не
    # заводится вовсе (К-5): `rejected` имеет человеческий смысл и запирает
    # повтор. Под правилом разбора `workshop_echo`: `off` — ветка не выполняется
    # (ни одного запроса), `shadow` — строка со следом `stop_shadow`, `on` —
    # только журнал без слов клиента. «Правила промолчали» — False: адрес
    # прочитан и отброшен, а модель-читатель исходящих не видит и записала бы
    # эхо сама.
    if (
        found.kind == address_parse.KIND_HOUSE
        and address_parse.rule_state(rules, address_parse.WORKSHOP_ECHO) != address_parse.PARSE_OFF
    ):
        источник_эха = await _эхо_мастерской(db, conv, msg, found, rules=rules)
        if источник_эха is not None:
            под_правилом = address_parse.under_rule(
                rules, address_parse.WORKSHOP_ECHO, old=found, new=None
            )
            log.info(
                "address.workshop_echo",
                conversation_id=str(conv.id),
                client_id=str(client.id),
                source=источник_эха,
                level=found.level,
                state=address_parse.rule_state(rules, address_parse.WORKSHOP_ECHO),
            )
            if под_правилом is None:
                return None, (), False
            found = под_правилом
    # Строки-источника уже нет (диалог удалён каскадом при отключении канала),
    # а ключ «улица, дом» на карточке тот же — дописывать некуда, предлагать
    # нечего. Ключ хранится на карточке ровно ради этого случая (ревью 11.09).
    if (
        client.address is not None
        and client.address_candidate_id is None
        and found.value in (client.address_value, client.address)
    ):
        return None, (), False
    # ⚠ ИНАЧЕ ЗАПИСЫВАЕМ ВСЕГДА, ДАЖЕ ЕСЛИ АДРЕС УЖЕ В КАРТОЧКЕ: повтор «ул
    # Ленина 5 кв 7» после автозаписи дописывает квартиру в строку-источник, а
    # «пос. Ударник» — посёлок с перепроверкой. Ранний выход до записи терял и
    # то и другое (ревью 11.09). «Предлагать нечего» — про кадр, а не про запись.
    записано = await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv.id,
        message_id=msg.id,
        message_at=msg.created_at,
        found=found,
        # Источник — по тому, откуда взят текст: адрес из расшифровки подписан
        # «голос», оператор сверяет его со звуком (правила записи те же).
        source=CANDIDATE_SOURCE_VOICE if речь.spoken else CANDIDATE_SOURCE_INBOUND,
        now=now,
    )
    ещё_на_проверку: tuple[uuid.UUID, ...] = ()
    if (
        found.kind == address_parse.KIND_PLACE
        and not found.settlement
        and внутригородской_массив(found.area)
    ):
        # МИКРОРАЙОН, НАЗВАННЫЙ ПОСЛЕ ДОМА (владелец 19.09, Волжский): «пл
        # Мира 11» → «9 микрорайон» — в строку дома того же диалога (сутки)
        # дописывается `area`, вердикт карты не сбрасывается (город тот же),
        # текст карточки пересобирается, если её держит эта строка.
        когда = _aware_utc(msg.created_at)
        дом = await clients.refine_address_area(
            db,
            client_id=client.id,
            conversation_id=conv.id,
            area=found.area or "",
            quote=found.raw,
            since=когда - _ОКНО_ВОПРОСА,
            before=now,
        )
        if дом is not None and client.address_candidate_id == дом.id:
            await clients.refresh_auto_address(db, client, дом)
    elif (
        found.kind == address_parse.KIND_PLACE
        and found.settlement
        and not (found.street or "").strip()
        and not _это_город_объявления(found.settlement, conv)
        and address_parse.place_stated_as_address(тело, found)
    ):
        # ПУНКТ С ТИПОМ ПОСЛЕ ДОМА (N13, 19.09): «Тихий 2» → карта нашла три
        # по области → «деревня Малиновка». Пункт — в последнюю строку дома
        # диалога (сутки). Два режима, один сторож смысла (`place_stated_as_
        # address`: именительный падеж, без «из/до/у…» и без «не» перед —
        # «еду из деревни Малиновка» не адрес ни в каком режиме):
        # — ОТВЕТ на вопрос оператора об адресе — как голое «Ялга» (18.09):
        #   любой статус, и `exact` тоже: клиент поправил адрес;
        # — РЕЧЬ без вопроса — только строка без вердикта или с неспокойным
        #   (`LATE_PLACE_STATUSES`): у точного дома в городе «деревня
        #   Малиновка» ничего не меняет — место живёт своей строкой.
        # Город словами («г. Шадринск») — город клиента, как в `parse`.
        # Дом с новым пунктом идёт ЖИВОЙ задачей, вместе со строкой места:
        # вопрос клиенту ждёт `pending` лишь RETRY_MAX×RETRY_DEFER_SEC
        # (workers/address_ask) — починки (≤10 мин) он не дождался бы.
        когда = _aware_utc(msg.created_at)
        ответ = await _оператор_спросил_адрес(db, conv, before=когда)
        город_словами = found.settlement_type == "город"
        дом = await clients.refine_address_settlement(
            db,
            client_id=client.id,
            conversation_id=conv.id,
            settlement=None if город_словами else found.settlement,
            settlement_type=None if город_словами else found.settlement_type,
            locality=found.settlement if город_словами else None,
            quote=found.raw,
            since=когда - _ОКНО_ВОПРОСА,
            before=now,
            kind=address_parse.KIND_HOUSE,
            only_statuses=None if ответ else geocode.LATE_PLACE_STATUSES,
        )
        if дом is not None and await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED):
            ещё_на_проверку = (дом.id,)
    # «Этот адрес уже в карточке» узнаётся по ключу «улица, дом», сохранённому
    # на карточке (`address_value`) — он переживает удаление строки-источника
    # каскадом; текст поля сравнивать нельзя — там строка карты.
    уже_в_карточке = client.address is not None and (
        client.address_candidate_id == записано.candidate_id
        or client.address == found.value  # строка до 11.09: связи нет, текст равен
    )
    # Проверять по карте есть смысл только когда карта включена; иначе строка
    # ждёт `pending`, и её возьмёт починка, когда карту включат.
    на_проверку = (
        (записано.candidate_id,)
        if записано.candidate_id is not None
        and записано.перепроверить
        and await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED)
        else ()
    ) + ещё_на_проверку
    if уже_в_карточке:
        return ("address_refined" if not записано.впервые else None), на_проверку, False
    # Кадр нужен и на дописанные части: карточка показывает квартиру и подъезд
    # строкой предложения, и «кв 3», названная следом, обязана появиться у всех.
    return ("address_suggested" if записано.впервые else "address_refined"), на_проверку, False


#: Номер кусками (бой 25.09, `phone_parse.from_fragments`): куски — реплики
#: клиента подряд, не дальше этого окна от текущей. Номер, разбитый на сообщения,
#: отправляют за секунды; десять минут — запас на медленный набор, а не на
#: разговор между кусками.
_ОКНО_КУСКОВ = timedelta(minutes=10)
#: Предыдущих реплик-кусков не больше десяти: в номере одиннадцать цифр, и одна
#: из них — в текущей реплике.
_КУСКОВ_ДО_ТЕКУЩЕЙ = 10


async def _номер_по_кускам(
    db: AsyncSession, conv: Conversation, msg: Message, речь: str | None
) -> phone_parse.Found | None:
    """«8900» → «111» → «2247» (бой 25.09, цифры вымышленные): номер тремя репликами.

    Зовётся, только когда в самой реплике номера нет, а она целиком — кусок
    номера; проверка чистая, так что прочие входящие запроса не порождают.
    Куски — входящие ПОДРЯД: исходящее между ними (ответ оператора, бота) кончает
    склейку — это уже разговор, а не диктовка номера. Только строго раньше
    текущей: при равном времени порядок кусков неизвестен, а переставленные
    куски — чужой номер.
    """
    if not phone_parse.is_fragment(речь):
        return None
    когда = _aware_utc(msg.created_at)
    лента = (
        await db.execute(
            select(Message.direction, voice_service.speech_sql().label("речь"))
            .where(
                Message.conversation_id == conv.id,
                Message.direction.in_(("in", "out")),
                Message.created_at < когда,
                Message.created_at >= когда - _ОКНО_КУСКОВ,
            )
            .order_by(Message.created_at.desc())
            .limit(_КУСКОВ_ДО_ТЕКУЩЕЙ)
        )
    ).all()
    подряд: list[str | None] = []
    for направление, текст in лента:
        if направление != "in":
            break
        подряд.append(текст)
    return phone_parse.from_fragments([*reversed(подряд), речь])


async def _maybe_extract_phone(
    db: AsyncSession,
    conv: Conversation,
    client: Client,
    msg: Message,
    *,
    now: datetime,
    backfill: bool = False,
) -> str | None:
    """Телефоны из ТЕКСТА входящего — в карточку по правилам 12.09.

    ⚠ ВОЗВРАЩАЕТ ПРИЧИНУ КАДРА, А НЕ ШЛЁТ ЕГО САМ (жалоба владельца 02.09:
    «когда клиент указывает номер, сначала пишет „ещё номера этого человека“, а
    после обновления страницы прописывает нормально»).

    Здесь стояла публикация прямо по месту — то есть ВНУТРИ транзакции, до
    commit'а. Экран получал кадр, честно перезапрашивал карточку и читал из базы
    ЕЩЁ СТАРУЮ строку, без телефона. Второго повода перезапросить не было, и
    номер появлялся только после F5.

    Правило в проекте записано давно и не мною: строки собираются внутри
    транзакции, а публикуются строго после commit'а (08 §8.1).

    ПРАВИЛА ВЛАДЕЛЬЦА 12.09 (заменили правила 12.08; разбор — в шапке
    `app/services/phone_rules.py`). Все номера сообщения, а не первый; пустой
    основной заполняется сам (`PHONE_DETECT_AUTOFILL`); остальные ложатся
    ДОПОЛНИТЕЛЬНЫМИ без вопроса; заполненный основной автоматика не меняет
    никогда; наши номера и 8-800 не пишутся вовсе. Писатель один —
    :func:`clients.absorb_phones`. Автообъединение карточек-двойников по
    появившемуся основному ставится ПОСЛЕ commit'а вызывающим (не здесь).

    МЕЖКАНАЛЬНАЯ ПРОВЕРКА ЖИВА И ПРИ ВЫКЛЮЧЕННОЙ ЗАПИСИ. `_apply_phone_evidence`
    сравнивает первый годный номер с тем, что система уже знала об этом
    человеке (:func:`_known_phone_of`: карточка, а если пусто — первый
    распознанный). У карточки без склейки (`cross_account_since IS NULL`)
    функция выходит сразу, не тронув базу.

    ЗВАТЬ ТОЛЬКО НА НАСТОЯЩЕМ ВХОДЯЩЕМ. Служебные записи Авито сюда не доходят
    вовсе (`_apply_avito_system_event` — отдельная ветка, до этой), исходящие
    оператора этот путь не обрабатывает по определению: там наш собственный
    телефон, и записать его клиенту значило бы позвонить самому себе.

    `backfill` здесь ничего не меняет в записи (у каждой строки есть своё
    `message_at`, а основной автоматика не трогает) — он нужен вызывающему,
    чтобы не ставить объединение на догрузке истории.
    """
    del backfill  # см. докстринг: решения не зависят от порядка обработки
    if not await app_settings.get(db, app_settings.PHONE_DETECT_ENABLED):
        return None
    # Речь, а не тело: номер, продиктованный в голосовом, лежит в расшифровке
    # (владелец 19.09). Та же строка идёт и в `absorb_phones`: смещения `found`
    # — от неё, и подсказка `hint_word` ищет по ней же.
    речь = client_speech(msg)
    found = phone_parse.find_all(речь.text)
    текст = речь.text
    if not found:
        # «8900» → «111» → «2247» (бой 25.09): номер кусками в репликах подряд.
        # Строка для `absorb_phones` — склейка кусков: смещения `found` от неё.
        по_кускам = await _номер_по_кускам(db, conv, msg, речь.text)
        if по_кускам is None:
            return None
        found, текст = [по_кускам], по_кускам.raw
    own = phone_rules.parse_own_numbers(await app_settings.get(db, app_settings.PHONE_OWN_NUMBERS))
    годные = [
        hit for hit in found if hit.value not in own and not phone_rules.is_toll_free(hit.value)
    ]
    if годные:
        known_phone, known_account_id = await _known_phone_of(db, client)
        if known_phone is not None:
            _apply_phone_evidence(
                client,
                годные[0].value,
                conv.account_id,
                now,
                known_phone=known_phone,
                known_account_id=known_account_id,
            )

    autofill = bool(await app_settings.get(db, app_settings.PHONE_DETECT_AUTOFILL))
    итог = await clients.absorb_phones(
        db,
        client=client,
        conversation_id=conv.id,
        account_id=conv.account_id,
        message_id=msg.id,
        message_at=_aware_utc(msg.created_at),
        text=текст,
        found=found,
        now=now,
        autofill=autofill,
        own=own,
        source=CANDIDATE_SOURCE_VOICE if речь.spoken else CANDIDATE_SOURCE_INBOUND,
    )
    return итог.reason


async def llm_read_wanted(
    db: AsyncSession,
    conv: Conversation,
    *,
    речь: str | None,
    правила_промолчали: bool,
    причина_адреса: str | None,
    before: datetime,
    rules: address_parse.Rules = None,
) -> bool:
    """ОДНИ ворота модели-читателя для живого пути и задачи голоса (19.09).

    Правила промолчали, а реплика похожа на адрес — после commit'а её перечитает
    бесплатная модель (`workers/address_llm`, владелец 13.09); без ключа — не
    ставится. «Куда выезжать?» → «Пушки на 10» (владелец 15.09): ответ на вопрос
    об адресе читает модель и без примет невода. Вынесено из `apply_inbound_event`,
    чтобы разбор голосового (`workers/voice_card`) не завёл вторую копию условия
    (класс dva-puti-raznyi-schet).
    """
    if not (правила_промолчали and причина_адреса is None and address_llm.enabled()):
        return False
    текст = речь or ""
    # Невод ворот — тот же, что у разбора, под той же политикой (стоп-класс
    # в `off` не должен будить модель на реплику, которую разбор уже отверг).
    rules = rules if rules is not None else await parse_rules(db)
    return address_llm.looks_like_address(текст, rules=rules) or (
        address_llm.worth_after_question(текст)
        and await _оператор_спросил_адрес(db, conv, before=before)
    )


class CardReplay(NamedTuple):
    """Итог разбора одного входящего догоном — то, что на живом пути стало бы
    кадром и задачами; решает вызывающий (см. :func:`replay_card_extraction`)."""

    phone_reason: str | None
    address_reason: str | None
    #: Геоточка Авито с координатами легла строкой `exact` — карта к ней не придёт,
    #: автозапись ставит вызывающий (одну на диалог).
    geopoint_ready: bool


@dataclasses.dataclass(frozen=True, slots=True)
class CardReplayFull:
    """Разбор одного входящего + то, что живой путь поставил бы задачами ПОСЛЕ
    commit'а. Решает вызывающий: догон истории карту не ставит (её делает
    починка), задача голоса у свежего голосового — ставит (19.09)."""

    card: CardReplay
    #: `адрес_на_проверку` из `_maybe_extract_address` — строки для карты.
    geocode_ids: tuple[uuid.UUID, ...]
    #: Общие ворота модели-читателя (:func:`llm_read_wanted`) сказали «да».
    llm_read_wanted: bool


@dataclasses.dataclass(slots=True)
class _Следствия:
    """Копилка полной формы: заполняется внутри :func:`replay_card_extraction`,
    когда её открыла :func:`replay_card_extraction_full`."""

    geocode_ids: tuple[uuid.UUID, ...] = ()
    llm_read_wanted: bool = False


#: ⚠ ПОЧЕМУ КОПИЛКА, А НЕ ВТОРОЕ ВОЗВРАЩАЕМОЕ ЗНАЧЕНИЕ. `replay_card_extraction`
#: — ЕДИНСТВЕННЫЙ шов «одна реплика догона» для всех путей (задача N29,
#: `cli backfill-cards`, задача голоса), и его трёхполевой итог сравнивают с
#: кортежем (`test_paket2_history_1909`), а на сам шов ставят шпионов и
#: диверсии (сбой одной реплики, чужая запись между репликами). Полная форма
#: поэтому НАД ним, а не рядом: она открывает копилку и зовёт тот же шов —
#: шпион на шве видит каждую реплику каждого пути, а вторую копию порядка
#: «телефон → адрес» никто не заводит. Без открытой копилки следствия не
#: считаются (ворота модели — запрос к ленте), и трёхполевой контракт прежний.
_СЛЕДСТВИЯ: contextvars.ContextVar[_Следствия | None] = contextvars.ContextVar(
    "card_replay_следствия", default=None
)


async def replay_card_extraction(
    db: AsyncSession, conv: Conversation, client: Client, msg: Message, *, now: datetime
) -> CardReplay:
    """Телефон и адрес ОДНОГО входящего — тем же путём, что живой приём, без задач и кадров.

    Для догона по прожитой переписке: задача `workers/cards_catchup` (историческая
    дверь, N29 19.09), `cli backfill-cards` и разбор голосового по расшифровке
    (`workers/voice_card`, 19.09). Порядок тот же, что в
    `apply_inbound_event`: сначала телефон, потом адрес — и по той же причине (по
    телефону звонят прямо сейчас). ``now`` — ВРЕМЯ СООБЩЕНИЯ, не «сейчас»: у строк
    `detected_at` = время реплики, иначе автозапись (сутки по `detected_at`) сочла бы
    прошлогодний адрес свежим, а сторожа «не позже» (`clients._не_позже`) увидели бы
    строки из будущего. Задач (карта, автозапись, модель, вопрос клиенту,
    объединение) не ставит и кадров не шлёт — это решает вызывающий; возвращает
    причины кадра и признак геоточки, чтобы он мог послать один кадр и одну
    автозапись на диалог. Что живой путь поставил бы задачами, отдаёт
    :func:`replay_card_extraction_full` (см. `_СЛЕДСТВИЯ`).
    """
    причина_телефона = await _maybe_extract_phone(db, conv, client, msg, now=now, backfill=True)
    причина_адреса: str | None = None
    на_проверку: tuple[uuid.UUID, ...] = ()
    правила_промолчали = False
    речь = client_speech(msg).text
    # Речь (тело или расшифровка ГОТОВО), иначе геоточка Авито: та приходит
    # без тела — только вложением (как в `cli backfill-cards`). По телу здесь
    # голосовое не проходило вовсе — адрес из расшифровки не разбирал никто.
    if (речь or "").strip() or _геоточка(msg.attachments) is not None:
        причина_адреса, на_проверку, правила_промолчали = await _maybe_extract_address(
            db, conv, client, msg, now=now
        )
    следствия = _СЛЕДСТВИЯ.get()
    if следствия is not None:
        следствия.geocode_ids = на_проверку
        следствия.llm_read_wanted = await llm_read_wanted(
            db,
            conv,
            речь=речь,
            правила_промолчали=правила_промолчали,
            причина_адреса=причина_адреса,
            before=now,
        )
    return CardReplay(
        phone_reason=причина_телефона,
        address_reason=причина_адреса,
        geopoint_ready=_точка_подтверждена_авито(msg, причина_адреса),
    )


async def replay_card_extraction_full(
    db: AsyncSession, conv: Conversation, client: Client, msg: Message, *, now: datetime
) -> CardReplayFull:
    """То же, что :func:`replay_card_extraction`, плюс следствия для вызывающего:
    строки на карту и ворота модели. Сама задач не ставит — только говорит,
    что поставил бы живой путь; ставить ли, решает вызывающий (догон истории —
    нет, задача голоса у свежего — да)."""
    следствия = _Следствия()
    метка = _СЛЕДСТВИЯ.set(следствия)
    try:
        card = await replay_card_extraction(db, conv, client, msg, now=now)
    finally:
        _СЛЕДСТВИЯ.reset(метка)
    return CardReplayFull(
        card=card,
        geocode_ids=следствия.geocode_ids,
        llm_read_wanted=следствия.llm_read_wanted,
    )


#: Множество id наших же отправок (все части длинных сообщений) — пишет
#: deliver сразу после каждой отправки, читает дедуп эха выше. TTL у ключа
#: обновляется на каждой записи; старше двух часов эхо уже не прилетает.
_OWN_ECHO_KEY = "echo:own:{account_id}"


#: Окно, в котором исходящее с тем же текстом считается НАШИМ эхом, а не вторым
#: ответом оператора. Две минуты: доставка через Авито и возврат вебхука укладываются
#: в секунды, запас взят на очередь доставки и повторные попытки.
_ЭХО_ОКНО = timedelta(minutes=2)


def _текст_для_бота(event: Any) -> str | None:
    """Что уходит в шаг бота: текст, а для сообщения без текста — маркер вложения.

    Пустая строка для движка означает «клиент промолчал», и присланное фото
    попадало ровно в эту дыру: сценарий досиживал таймаут и передавал диалог
    человеку с причиной «не ответил», хотя ответ был.
    """
    текст = (getattr(event, "text", None) or "").strip()
    if текст:
        return текст
    вложения = getattr(event, "attachments", None) or []
    from app.bots.runtime import МАРКЕР_ВЛОЖЕНИЯ

    return МАРКЕР_ВЛОЖЕНИЯ if вложения else текст


async def _клиент_писал_после(db: AsyncSession, conv: Conversation, момент: datetime) -> bool:
    """Есть ли входящее клиента ПОЗЖЕ этого момента.

    Один поиск по `(conversation_id, created_at)` с пределом в строку — индекс
    заведён миграцией 0002. Зовётся только на пути внешнего эха, то есть на
    ответах, написанных человеком из приложения Авито.
    """
    найдено = await db.execute(
        sa.select(sa.literal(True))
        .where(
            Message.conversation_id == conv.id,
            Message.direction == "in",
            Message.created_at > момент,
        )
        .limit(1)
    )
    return найдено.scalar() is not None


async def _своё_по_части(
    db: AsyncSession, conversation_id: uuid.UUID, text: str, created_at: datetime
) -> Message | None:
    """Своё исходящее, ЧАСТЬЮ которого пришло эхо (проверка 24.09).

    Ответ длиннее `AVITO_TEXT_LIMIT` доставка режет на части (`split_text`), и
    эхо первой части приходит раньше, чем её id попадёт в множество «свои»:
    сверка по тексту целиком его не узнавала (в бою 1 032 знака против 998 части),
    и в ленту ложилось второе исходящее без автора — со снятием бота и прочими
    действиями «ответили из другого приложения». Ищем среди своих длинных
    исходящих рядом по времени то, чья нарезка содержит текст эха.
    """
    длинные = (
        (
            await db.execute(
                sa.select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "out",
                    sa.func.length(Message.body) > AVITO_TEXT_LIMIT,
                    Message.created_at >= created_at - _ЭХО_ОКНО,
                    Message.created_at <= created_at + _ЭХО_ОКНО,
                )
                .order_by(Message.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    часть = text.strip()
    for свой in длинные:
        if часть in split_text(свой.body or ""):
            return свой
    return None


async def _apply_external_outgoing(
    db: AsyncSession,
    redis: Redis,
    account: AvitoAccount,
    event: InboundEventLike,
    *,
    publish: bool = True,
    backfill: bool = False,
) -> bool:
    """Исходящее с аккаунта, отправленное НЕ из LeadChat (Jivo, приложение).

    Правила:
    * своё эхо (external_message_id уже в базе — доставка LeadChat его
      сохраняет) — тихий дубль, False;
    * диалога нет (переписка началась до подключения) — создаётся ЗАКРЫТЫМ,
      как у backfill: очередь, боты и звонки историю не видят;
    * ожидание ответа снимается: клиенту ответили, пусть и не отсюда;
    * бот, если вёл диалог, отходит — человек вмешался из другого приложения,
      и два пишущих в один чат («ваше сообщение отключит бота») хуже тишины.
    """
    async with db.begin():
        # ⚠ КЛИЕНТА ИЗ СОБЫТИЯ БРАТЬ НЕЛЬЗЯ: автор эха — САМ АККАУНТ, и
        # _upsert_client завёл бы карточку «клиента-Тимофея», к которой
        # слиплись бы все чаты, начинающиеся с нашего сообщения (история
        # Jivo-эпохи почти вся такая). Существующий диалог живёт со своим
        # клиентом; новому — карточка-заглушка «личность = сам чат»
        # (`chat:<id>`, тот же приём, что у _upsert_client для событий без
        # автора): настоящее имя дотянет обогащение из карточки чата.
        # ⚠ `channel` ЗДЕСЬ НЕ РАДИ ТОЧНОСТИ, А РАДИ ИНДЕКСА.
        #
        # Единственный индекс с `external_chat_id` — `uq_conversations_channel_
        # external_chat_id (channel, external_chat_id)` (миграция 0001). Его
        # ведущая колонка — `channel`, и без неё поиск по нему невозможен: базе
        # остаётся полный проход. А путь горячий — сюда приходит КАЖДОЕ
        # сообщение, отправленное человеком из приложения Авито (по замеру боя
        # 23.08 это сегодня единственный способ, которым отвечают), и та же
        # дверь принимает исходящие из сверки истории. На 454 тысячах диалогов
        # это полный проход на каждый ответ оператора: ни ошибки, ни строчки в
        # журнале — просто приём начинает отставать, и лента наполняется с
        # задержкой.
        #
        # Все ОСТАЛЬНЫЕ пять мест, ищущих диалог по внешнему чату, ограничивают
        # `channel` и в индекс попадают (inbound.py:231, inbound.py:398,
        # avito_accounts.py, workers/reconciliation.py, cli.py). Здесь его
        # заменили на `account_id` — тот же промах, что чинила миграция 0051 для
        # карточек клиентов. `account_id` оставляем: пара уникальна по
        # (channel, external_chat_id), и лишнее условие ничего не стоит, зато
        # чужой диалог сюда не попадёт даже при переносе чата между каналами.
        существующий = (
            await db.execute(
                sa.select(Conversation).where(
                    Conversation.channel == "avito",
                    Conversation.account_id == account.id,
                    Conversation.external_chat_id == str(event.chat_id),
                )
            )
        ).scalar_one_or_none()
        if существующий is not None:
            conv = существующий
        else:
            stub_ext = f"chat:{event.chat_id}"
            client = (
                await db.execute(
                    sa.select(Client).where(
                        Client.channel == "avito", Client.external_id == stub_ext
                    )
                )
            ).scalar_one_or_none()
            if client is None:
                client = Client(id=uuid.uuid4(), channel="avito", external_id=stub_ext)
                db.add(client)
                await db.flush()
            conv, _created = await _upsert_conversation(
                db,
                account,
                client,
                event,
                initial_status="closed",
                offered_at=None,
            )
        dup = await db.execute(
            sa.select(Message.id)
            .where(
                Message.conversation_id == conv.id,
                Message.external_message_id == str(event.message_id),
            )
            .limit(1)
        )
        if dup.first() is not None:
            return False  # эхо собственной отправки LeadChat

        created_at = _aware_utc(event.created_at)
        # ⚠ ГОНКА: ВНЕШНИЙ ID ПИШЕТСЯ ПОЗЖЕ, ЧЕМ ПРИХОДИТ ЭХО (находка 26.08).
        # `external_message_id` проставляется в третьей фазе доставки
        # (`workers/deliver.py`), то есть ПОСЛЕ ответа Авито. Вебхук с эхом той же
        # отправки успевает раньше — проверка выше не находит ничего, и наше
        # собственное сообщение ложится в ленту ВТОРОЙ строкой, уже как «оператор».
        # Дальше срабатывает правило «ответили из другого приложения»: бот выключает
        # сам себя, приняв за оператора собственное эхо.
        #
        # Живой случай 26.08: бот отправил в 10:31:29.753, эхо пришло в 10:31:30 — тот
        # же external_message_id, но на 0,25 секунды позже, и защита не успела.
        # Владелец подтвердил: «при этом я не отвечал».
        #
        # ⚠ ЧАСТОТА ЧЕСТНАЯ: из 119 выключений бота за две недели по этой причине
        # случилось РОВНО ОДНО. Остальные 118 — живые операторы в приложении Авито.
        # Клиенту дубль при этом не уходит: Авито доставило сообщение один раз,
        # задваивается только наша лента.
        #
        # Ловим по тексту и времени: своё исходящее с тем же телом рядом по времени.
        # Оператор, дважды подряд написавший клиенту одно и то же слово в слово, —
        # случай, которого не жалко: клиент всё равно увидит это один раз.
        свой = (
            await db.execute(
                sa.select(Message)
                .where(
                    Message.conversation_id == conv.id,
                    Message.direction == "out",
                    Message.body == event.text,
                    Message.created_at >= created_at - _ЭХО_ОКНО,
                    Message.created_at <= created_at + _ЭХО_ОКНО,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if свой is None and event.text:
            свой = await _своё_по_части(db, conv.id, event.text, created_at)
        if свой is not None:
            # ⚠ ВНЕШНИЙ ID ДОСТАВЛЯЕМ НА МЕСТО. Сверка истории (reconciliation) ищет
            # наши сообщения именно по нему; без этого то же эхо приедет ещё раз
            # при следующем перечитывании канала и снова задвоит ленту.
            if not свой.external_message_id:
                свой.external_message_id = str(event.message_id)
            return False
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id=str(event.message_id),
            direction="out",
            sender_type="operator",
            sender_user_id=None,
            body=event.text,
            attachments=getattr(event, "attachments", None) or [],
            delivery_status="delivered",
            created_at=created_at,
        )
        db.add(msg)
        if conv.last_message_at is None or created_at > _aware_utc(conv.last_message_at):
            conv.last_message_at = created_at
        bot_released = False
        queue_frame = None
        left_queue = False
        # side-effects — только от СВЕЖЕГО эха: сверка (reconciliation)
        # перечитывает историю и применяет её не по порядку — старое эхо
        # стирало живой awaiting и глушило бота (аудит 17.08, №6)
        _fresh = (datetime.now(UTC) - created_at) < timedelta(minutes=15)
        if not backfill and _fresh:
            if conv.awaiting_since is not None and created_at >= _aware_utc(conv.awaiting_since):
                conv.awaiting_since = None
            if conv.bot_active:
                conv.bot_active = False
                bot_released = True
                convs_add_system_message(db, conv, "Ответили из другого приложения — бот отключён")
                # диалог юридически снова в очереди (is_waiting требует
                # not bot_active) — без кадра он невидим операторам до
                # ручного обновления (аудит 17.08, №3)
                if conv.offered_at is None:
                    conv.offered_at = datetime.now(UTC)
                from app.services import inbox as _inbox

                if _inbox.is_waiting(conv):
                    queue_frame = await _inbox.inbox_frame_addressed(db, conv)
        # ⚠ ОТВЕТИЛИ СНАРУЖИ — ДИАЛОГ БОЛЬШЕ НЕ ЖДЁТ РАЗБОРА (решение
        # владельца 19.08, находка аудита L-008). Раньше зеркало гасило
        # «клиент ждёт», но оставляло диалог в очереди новым и ничьим: на бою
        # висел живой случай — клиенту шесть раз ответили из приложения, а
        # тринадцать диспетчеров видели его как неразобранный и могли
        # ответить вторым голосом. Диалог остаётся видимым в «Все», находится
        # поиском и открывается — он просто перестаёт требовать нашего хода.
        # Обратно вернётся сам, как только клиент напишет снова: возврат
        # чинится ниже, в apply_inbound_event.
        #
        # ⚠ И ТОЛЬКО ЕСЛИ ПОСЛЕ ЭТОГО ОТВЕТА КЛИЕНТ НЕ ПИСАЛ СНОВА (28.08).
        #
        # `_fresh` смотрит на ВОЗРАСТ события по стенным часам и ничего не знает
        # о порядке. Соседняя ветка выше от этого защищена сравнением с
        # `awaiting_since` — «сверка перечитывает историю и применяет её не по
        # порядку, старое эхо стирало живой awaiting» (аудит 17.08, №6). Этой
        # ветке, добавленной 19.08, такого сравнения не досталось, а
        # `leave_queue` снимает `offered_at` И зовёт `clear_awaiting` — то есть
        # гасит ровно то, что бережёт защита выше.
        #
        # Чем это кончается: канал теряет вебхуки на десять минут, оператор
        # отвечает из приложения Авито в 10:00, клиент пишет «а когда приедете?»
        # в 10:03. Сверка приходит через пять минут и отдаёт историю СВЕЖИМИ
        # ВПЕРЁД (так отвечает Авито, так же устроен имитатор). Сначала
        # применяется сообщение клиента — диалог встаёт в очередь, у диспетчеров
        # звенит. Следом применяется эхо 10:00 — и диалог уходит из очереди.
        # Дальше он невидим целиком: `queue_condition` требует `offered_at`,
        # сторож «клиент ждёт» требует ответственного, а его нет. Ни ошибки, ни
        # строчки в журнале — обращение просто перестаёт существовать для всех,
        # пока клиент не напишет в третий раз.
        #
        # Спрашиваем прямо: писал ли клиент ПОСЛЕ этого ответа. Обещание в
        # комментарии выше — «вернётся сам, как только клиент напишет снова» —
        # только теперь и выполняется: раньше его сообщение могло примениться
        # РАНЬШЕ ответа и быть затёртым им же.
        if not backfill and _fresh:
            from app.services import inbox as _inbox_leave

            писал_после = await _клиент_писал_после(db, conv, created_at)
            if _inbox_leave.is_waiting(conv) and not писал_после:
                _inbox_leave.leave_queue(conv)
                left_queue = True
                convs_add_system_message(db, conv, "Ответили из другого приложения")

        # ⚠ ОТВЕТИЛИ — ЗНАЧИТ ДИАЛОГ БОЛЬШЕ НЕ «НОВЫЙ» (28.08, замер боя).
        #
        # `ensure_in_progress` — единственный вход для всех автоматических
        # переходов «кто-то взялся за диалог»; его зовут первый ответ оператора
        # из LeadChat, автораздача, принятие из очереди и принятие передачи. Этот
        # путь — ответ, написанный в приложении Авито, — не звал его никогда.
        #
        # А по замеру боя 23.08 отвечают сегодня ИМЕННО оттуда, то есть мимо
        # перехода проходят практически все ответы. Замер 28.08: 522 диалога, где
        # последнее видимое сообщение НАШЕ, висят в статусе «Новый», против 7
        # правильно переведённых. Оператор открывает «Новые» и видит полтысячи
        # обращений, на которые уже ответил, — отличить их там нечем.
        #
        # Условия те же, что у соседей: только свежее эхо и не импорт истории.
        # Годовалая переписка обязана остаться закрытой, а не разбудить полтысячи
        # диалогов разом. `ensure_in_progress` трогает ТОЛЬКО `new`: «Ждёт
        # клиента» не сбрасывается намеренно — ход по-прежнему за клиентом.
        #
        # Автора не передаём: Авито не говорит, КТО из сотрудников ответил.
        # Пустой `user_id` в журнале и означает «это сделала система».
        if not backfill and _fresh:
            await convs_ensure_in_progress(db, conv, source="external_reply", actor_id=None)

    if publish and not backfill:
        await publish_event(
            redis,
            "message:new",
            {
                "conversation_id": str(conv.id),
                "message": message_out(msg),
                "conversation_patch": {
                    "last_message_at": iso(conv.last_message_at),
                    "status": conv.status,
                    # ⚠ ЯКОРЬ ОЖИДАНИЯ ОБЯЗАН ЕХАТЬ В ЭТОМ ЖЕ КАДРЕ (28.08).
                    #
                    # Отметку «клиент ждёт» мы выше сняли — ответили же. Но в
                    # патч она не попадала, а строка списка на клиенте
                    # ОБНОВЛЯЕТСЯ ЭТИМ ПАТЧОМ: превью и время менялись, а
                    # `waiting_since` в строке оставался прежним — тем, что
                    # лежал там ДО ответа. Часы на клиенте тикают от якоря, и
                    # оранжевая шкала «ждёт 19 мин» продолжала расти на диалоге,
                    # где в превью уже стоит «Вы: …». Гасло это только полной
                    # перезагрузкой списка, а список сам не перезапрашивается:
                    # `staleTime` тридцать секунд и `refetchOnWindowFocus`
                    # выключен.
                    #
                    # Считаем ТЕМ ЖЕ каноническим расчётом, что и строка списка:
                    # второй способ здесь и был бы новым разнобоем.
                    "waiting_since": iso(status_dict.waiting_since(conv)),
                },
            },
        )
        if bot_released:
            await publish_event(
                redis,
                "conversation:updated",
                {"conversation_id": str(conv.id), "patch": {"bot_active": False}},
            )
            if queue_frame is not None:
                # ⚠ ВТОРОЙ ЭКЗЕМПЛЯР ТОЙ ЖЕ ОШИБКИ, что уронила звонки-лиды
                # (L-001): publish_inbox_new ждёт САМУ строку очереди и
                # заворачивает её сам. Готовый конверт давал KeyError: 'id'.
                # Здесь не выстрелило только потому, что ветка требует
                # активного бота, а бот сейчас ни к одному каналу не привязан.
                await publish_inbox_new(
                    redis,
                    queue_frame["conversation"],
                    eligible_operator_ids=queue_frame.get("eligible"),
                )
        if left_queue:
            # строка гаснет у операторов живьём — иначе диалог, ушедший из
            # очереди, висел бы на экранах до перезагрузки, и его бы всё
            # равно взяли, ответив клиенту вторым голосом
            await publish_event(
                redis,
                "inbox:claimed",
                {
                    "conversation_id": str(conv.id),
                    "claimed_by": None,
                    "claimed_at": None,
                    "waited_seconds": None,
                    "conversation_patch": {"in_inbox": False, "status": conv.status},
                },
            )
    return True


async def apply_inbound_event(
    db: AsyncSession,
    redis: Redis,
    account: AvitoAccount,
    event: InboundEventLike,
    *,
    publish: bool = True,
    backfill: bool = False,
) -> bool:
    """Apply one normalized inbound event. Returns True when the message was
    actually inserted (False — duplicate or dropped echo)."""
    # Служебная запись Авито — отдельная ветка, и она ПЕРВАЯ. Ниже начинается
    # путь переписки с клиентом: он заводит клиента по `author_id`, а у
    # служебного события автора нет вовсе — получился бы клиент с именем "None"
    # и диалог, назначенный живому оператору из-за уведомления.
    if getattr(event, "is_system", False) or avito_system_prefixed(event.text):
        # «[Системное сообщение] Пользователь создал чат, но пока ничего не
        # написал» приезжает ОБЫЧНЫМ сообщением клиента — и рождало пустышку
        # в очереди: «Сообщений пока нет» с кнопкой «Принять» и таймером
        # 15 часов (скрин владельца 17.08). Приставка = служебная запись:
        # чип в существующий диалог, сироты — мимо (диалог появится с первым
        # настоящим словом клиента).
        return await _apply_avito_system_event(db, account, event, redis, backfill=backfill)

    if event.author_id == account.avito_user_id:
        # 17.08 (работа параллельно с Jivo): исходящее С АККАУНТА — больше не
        # слепой отброс. Своё (отправленное из LeadChat) дедупится по
        # external_message_id; ЧУЖОЕ (ответ оператора из Jivo/приложения
        # Авито) вставляется в ленту — иначе переписка была однобокой, а
        # статус врал «ждёт ответа» про клиента, которому ответили.
        #
        # Первый рубеж дедупа — Redis-множество id, которые доставка кладёт
        # СРАЗУ после отправки каждой части (до записи в БД): закрывает и
        # гонку «эхо обгоняет фазу 3», и части длинных сообщений, у которых
        # в БД сохранён id только последней (аудит 17.08, №2/№4).
        try:
            if await redis_mod.aw(
                redis.sismember(_OWN_ECHO_KEY.format(account_id=account.id), str(event.message_id))
            ):
                return False
        except Exception as exc:  # noqa: BLE001 — Redis мигнул: БД-дедуп ниже подстрахует
            log.warning("inbound.own_echo_check_failed", error=type(exc).__name__)
        return await _apply_external_outgoing(
            db, redis, account, event, publish=publish, backfill=backfill
        )

    # Уведомление «клиент вернулся в закрытый диалог» (SCEN-17): СТРОКА пишется
    # внутри транзакции вместе с причиной, а кадр в браузер уходит строго после
    # commit'а (08 §8.1) — поэтому итог `notify` живёт снаружи блока.
    reopened_notice: notifications.NotifyResult | None = None
    reopened_patch = False
    # Кадр «карточка клиента изменилась» — по тому же правилу: причина считается
    # внутри транзакции, кадр уходит после commit'а (08 §8.1).
    причина_кадра_клиента: str | None = None
    основной_появился = False

    async with db.begin():
        created_at = _aware_utc(event.created_at)
        client, client_created_without_name, исходная = await _upsert_client(db, event)
        conv, conv_created = await _upsert_conversation(
            db,
            account,
            client,
            event,
            # backfill: история не должна засыпать очередь «Новые» (решение №3)
            initial_status="closed" if backfill else "new",
            offered_at=None if backfill else created_at,
            origin_client_id=исходная.id if исходная is not None else None,
        )
        if not conv_created and conv.client_id != client.id:
            # диалог мог родиться от ЭХА или звонка с клиентом-заглушкой
            # «chat:<id>» — первое настоящее сообщение клиента перевязывает
            # его на живую карточку (аудит 17.08, №5: иначе расщепление —
            # телефон и заявки уезжали бы в пустышку)
            _prev = await db.get(Client, conv.client_id)
            if _prev is not None and str(_prev.external_id).startswith("chat:"):
                conv.client_id = client.id
        # Межканальная склейка отмечается на СОЗДАНИИ диалога, а не на
        # сообщении: именно новый диалог на новом аккаунте и делает карточку
        # межканальной. Дубль вебхука сюда не доходит вторым разом — у нового
        # диалога дублей нет по определению, а у существующего `conv_created`
        # ложно. Стоит до вставки сообщения намеренно: даже если сообщение
        # окажется дублем и обработка оборвётся ниже, диалог второго аккаунта
        # уже создан — значит склейка уже случилась, и увидеть её надо.
        if conv_created:
            await _note_cross_account_link(db, account, client, now=created_at)
        msg = await _insert_message_idempotent(db, conv, event)
        if msg is None:
            return False  # дубль: ретрай вебхука / reconciliation

        if conv.last_message_at is None or created_at > _aware_utc(conv.last_message_at):
            conv.last_message_at = created_at
        # ⚠ ИСТОРИЯ НЕПРОЧИТАННЫМ НЕ СЧИТАЕТСЯ (28.08).
        #
        # Счётчик — это «сколько клиент написал, а мы не прочли». Загруженная
        # переписка прошлого года под это определение не подходит ни в одном
        # смысле: её никто не ждёт и отвечать в ней не на что.
        #
        # Раньше счёт шёл и на импорте, и это было тихо: импорт создавал диалог
        # закрытым, закрытые в очереди не видны. С 28.08 история подтягивается
        # и в ЖИВОЙ диалог, только что вставший во «Входящие» (`backfill_
        # conversation`), — и там разница видна сразу: клиент написал одну
        # строку, а бейдж показывал бы тридцать одну, по числу поднятых из
        # архива сообщений. Оператор открывает диалог, ищет тридцать
        # непрочитанных вопросов и не находит ни одного.
        #
        # Соседняя функция для служебных сообщений Авито про это же и теми же
        # словами: «иначе бейдж зовёт оператора туда, где клиент ничего не
        # спрашивал».
        if not backfill:
            conv.unread_count = (conv.unread_count or 0) + 1
        # Клиент написал и ждёт ответа — ТОЛЬКО если ещё не ждал.
        #
        # Настойчивый клиент шлёт подряд пять сообщений и ждёт с ПЕРВОГО.
        # Обновляй мы отметку на каждом, самый нетерпеливый выглядел бы самым
        # свежим — ровно наоборот тому, что нужно видеть.
        #
        # Импорт истории (`backfill`) отметку не ставит: он заливает переписку
        # годичной давности, и сторож разослал бы напоминания про диалоги,
        # которые давно разобрали.
        if not backfill and conv.awaiting_since is None:
            conv.awaiting_since = created_at
        # Новый диалог только что встал в очередь «Входящие» (offered_at выше);
        # у вернувшегося клиента это решается ниже.
        entered_queue = conv_created and not backfill

        # КЛИЕНТ НАПИСАЛ В ДИАЛОГ, КОТОРЫЙ МЫ ОТЛОЖИЛИ ИЛИ ПОСТАВИЛИ НА
        # «ЖДЁТ КЛИЕНТА» — ЖДАТЬ БОЛЬШЕ НЕЧЕГО (docs/38 §3).
        #
        # Оба состояния означают «ход за клиентом». Клиент сходил. Держать
        # диалог отложенным до завтрашнего утра после живого сообщения —
        # это молчание в ответ на вопрос, ровно то, ради чего статусы и
        # заводились.
        #
        # Стоит ВЫШЕ ветки закрытого намеренно: состояния не пересекаются, но
        # порядок чтения важнее — сначала «диалог живой и вернулся к нам»,
        # потом «диалог был закрыт и открывается заново».
        #
        # Ответственного НЕ трогаем: диалог остаётся у того, кто его вёл. Это
        # отличает пробуждение от переоткрытия закрытого, где хозяина нет по
        # определению.
        if conv.status in ("waiting_client", "snoozed") and not backfill:
            previous_status = conv.status
            status_dict.clear_snooze(conv)
            status_dict.set_status(conv, "in_progress", now=created_at)
            await write_audit(
                db,
                user_id=None,
                action="conversation.status_changed",
                entity="conversation",
                entity_id=str(conv.id),
                details={
                    "from": previous_status,
                    "to": "in_progress",
                    "by": "system",
                    "source": "client_replied",
                    "assignee_id": str(conv.assignee_id) if conv.assignee_id else None,
                },
            )

        if conv.status == "closed" and not backfill:
            # клиент вернулся (DESIGN §8.3, INT-4); reopen = ДВА audit-события (06 §0.3)
            #
            # КОГО ЗОВЁМ, ЗАПОМИНАЕМ ДО ТОГО, КАК СТЕРЁМ. Ответственный сейчас
            # обнулится (диалог уходит в общую очередь), а уведомление
            # «клиент вернулся» адресовано именно ему — см. ниже.
            returned_to_id = conv.assignee_id
            conv.assignee_id = None
            reopened_patch = True  # кадр после commit'а: без него строка во
            # «Все»/«Мои» до перезагрузки показывала прежнего ответственного,
            # и бывший владелец писал клиенту параллельно со взявшим из
            # очереди (аудит синхронизации 16.08)
            status_dict.set_status(conv, "new", now=created_at)
            # Возврат клиента — НОВОЕ ожидание, а не продолжение старого:
            # обнуляем отказы и пометку эскалации и ставим свежий offered_at.
            # Иначе оператор, отказавшийся от этого диалога месяц назад, не
            # увидит его и сегодня — хотя клиент написал заново.
            inbox.enter_queue(conv, now=created_at)
            entered_queue = True
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
            # «КЛИЕНТ ВЕРНУЛСЯ В ЗАКРЫТЫЙ ДИАЛОГ» — SCEN-17.
            #
            # Вид `conversation.reopened` числился в каталоге центра
            # уведомлений (14 §2.3), имел иконку и подпись во фронте — и не
            # создавался НИ ОДНОЙ строкой боевого кода. Здесь, в единственном
            # месте, где клиент и правда возвращается, писалась только запись
            # журнала аудита: её читает разбор постфактум, а человека она не
            # зовёт никогда.
            #
            # ПОЧЕМУ АДРЕСНО, А НЕ ВСЕЙ СМЕНЕ. Диалог и так встаёт в общую
            # очередь «Входящие» строкой со звуком — это уже сказано всем
            # тринадцати. Новость, которой нет в очереди, ровно одна и она
            # личная: «твой клиент, которого ты закрыл, вернулся». Знает
            # контекст разговора только тот, кто его вёл, и решать, забирать
            # ли диалог обратно, тоже ему.
            #
            # БЕЗ ХОЗЯИНА МОЛЧИМ. У диалога, закрытого из очереди без
            # ответственного, адресата нет; `notify` на такое честно бросает
            # ValueError, и подменять его рассылкой администраторам нельзя —
            # это чужая работа и чистый шум.
            #
            # `info`, а не `warning`: ответа клиент ждёт от очереди, и это
            # ожидание уже посчитано. Здесь — только «имей в виду».
            if returned_to_id is not None:
                reopened_notice = await notifications.notify(
                    db,
                    kind="conversation.reopened",
                    recipient_id=returned_to_id,
                    body=(
                        f"{client.name or 'Клиент'} снова написал в диалог, который вы "
                        "закрыли. Диалог вернулся в «Входящие» — его может принять любой "
                        "оператор."
                    ),
                    entity_id=str(conv.id),
                    now=created_at,
                )
        # решение владельца №2: никакого автозакрытия по таймауту — нигде.

        # Сообщение, а не текст: кандидату нужны идентификатор сообщения и его
        # время — без них предложение «распознан телефон» нечем проверить, а
        # непроверяемое предложение оператор либо примет не глядя, либо
        # перестанет замечать.
        # Причина кадра, а не сам кадр: публикуется он ниже, после commit'а —
        # иначе экран перезапросит карточку и прочитает ещё старую строку.
        причина_кадра_клиента = await _maybe_extract_phone(
            db, conv, client, msg, now=created_at, backfill=backfill
        )
        основной_появился = причина_кадра_клиента == "phone_captured"
        # ⚠ АДРЕС РАЗБИРАЕТСЯ ПОСЛЕ ТЕЛЕФОНА И НЕ ПЕРЕБИВАЕТ ЕГО ПРИЧИНУ.
        # Причина кадра нужна экрану, чтобы понять, ЧТО перезапросить, и телефон
        # здесь важнее: по нему звонят прямо сейчас. Одно сообщение с обоими
        # («Ленина 5, звоните на 8 915 …») даёт кадр с причиной телефона, а
        # карточка всё равно перечитывает личность целиком — адрес приедет тем
        # же ответом.
        причина_адреса, адрес_на_проверку, правила_промолчали = await _maybe_extract_address(
            db, conv, client, msg, now=created_at
        )
        причина_кадра_клиента = причина_кадра_клиента or причина_адреса
        # МОДЕЛЬ КАК ВТОРОЙ ЧИТАТЕЛЬ (владелец 13.09): правила промолчали, а
        # реплика похожа на адрес — после commit'а её перечитает бесплатная
        # модель (`workers/address_llm`). Ворота ОДНИ с разбором голосового
        # (`llm_read_wanted`); на живом пути расшифровки ещё нет — речь = тело.
        адрес_на_чтение = await llm_read_wanted(
            db,
            conv,
            речь=client_speech(msg).text,
            правила_промолчали=правила_промолчали,
            причина_адреса=причина_адреса,
            before=created_at,
        )
        # ОДИН ВОПРОС ОБ АДРЕСЕ (владелец 18.09). Здесь — только «ставить ли
        # проверку» по снимку настроек (`one_pass`: бесплатно) и по ПОЛЯМ уже
        # загруженных строк (`field_lock`/`card_lock` — те же замки, что в задаче,
        # одной функцией): принятые, ведомые ботом, уже спрошенные диалоги и
        # карточки с адресом задач не плодят — иначе с задержкой 600 с очередь
        # держала бы десятки наших отложенных задач и `geo_repair` молчал бы
        # (ЗАТОР=60). Решает задача под FOR UPDATE: между постановкой и
        # выполнением оператор мог ответить, а клиент — назвать адрес. Не на
        # истории (backfill), не на сообщении старше 15 минут (сверка заводит
        # пропущенные живые реплики этим же путём часами позже). Реплика с
        # найденным адресом задачу НЕ отсекает (ревью 19.09): «Хаер 75» тоже
        # заводит строку, а карта её отвергнет — и это повод спросить; строка
        # со степенью к сроку задачи станет замком `candidate_exists`.
        вопрос_об_адресе_через = (
            address_ask.enqueue_delay_sec(await app_settings.get_all(db))
            if not backfill
            and address_ask.is_fresh(created_at, now=datetime.now(UTC))
            and address_ask.field_lock(conv) is None
            and address_ask.card_lock(client) is None
            else None
        )

        # Строка очереди собирается ЗДЕСЬ, внутри транзакции: после commit'а за
        # связанными сущностями (клиент, аккаунт, последнее сообщение) пришлось
        # бы идти в базу второй раз. Публикуется она ниже — после commit'а.
        # `is_waiting` — один предикат на выборку и на кадр: пока он говорит
        # «диалог ждёт», кадр честен; если завтра здесь появится путь, который
        # оставляет ответственного, звук у тринадцати операторов не зазвонит
        # по диалогу, которого они в очереди не увидят.
        # АВТОРАСПРЕДЕЛЕНИЕ (docs/18). Пробуем отдать обращение свободному
        # оператору ДО того, как собирать строку очереди: получилось — диалог
        # уже назначен, и в очереди ему делать нечего.
        #
        # Здесь же, внутри транзакции, а не после: назначение и системная
        # запись о нём обязаны лечь одним куском. Иначе возможен диалог,
        # назначенный без следа о том, кем и почему, — и разобрать такое
        # потом нельзя ничем.
        #
        # Не получилось — ничего не делаем: обращение остаётся в очереди ровно
        # как раньше. Распределение это слой НАД очередью, а не вместо неё.
        # ⚠ ДОРОГА ОБРАТНО В ОЧЕРЕДЬ (решение владельца 19.08).
        #
        # Диалог мог выйти из очереди, оставшись живым и ничьим: чёрный список
        # (`leave_queue` ниже) и — с сегодняшнего дня — ответ, отправленный
        # клиенту снаружи. Вернуть его умел РОВНО ОДИН случай: «диалог закрыт,
        # клиент написал снова» (ветка `status == "closed"` выше). Для диалога
        # в статусе «новый» без `offered_at` возврата не было НИ ОДНОГО:
        # четыре боевых вызова `enter_queue` — два ботовых (бот ни к одному
        # каналу не привязан), один только для созданных сверкой, один только
        # для закрытых. То есть такой диалог не видел никто и не звал ни один
        # сторож, сколько бы клиент ни писал. Дыра существовала независимо от
        # зеркала: докстринг «Следующее обращение снова встанет в очередь» у
        # снятия блокировки был неправдой.
        #
        # Условие намеренно узкое: диалог живой, ничей, в очереди его нет и
        # его не ведёт бот. Стоит ДО чёрного списка, чтобы блокировка по
        # прежнему забирала диалог обратно, и до автораздачи, чтобы
        # вернувшийся диалог проходил тот же путь, что и любой другой.
        if (
            not entered_queue
            and not backfill
            and conv.status != "closed"
            and conv.assignee_id is None
            and conv.claimed_by_id is None
            and conv.offered_at is None
            and not conv.bot_active
        ):
            inbox.enter_queue(conv, now=created_at)
            entered_queue = True

        # ЧЁРНЫЙ СПИСОК (аудит 7 августа, docs/19). Сообщение помеченного
        # клиента принято, сохранено и видно в диалоге — но внимания не
        # требует: в очередь не встаёт, никому не звенит, автораздаче не
        # достаётся.
        #
        # Проверка стоит ЗДЕСЬ, а не на входе обработчика, и это важно: выйди
        # мы раньше — сообщение бы не сохранилось. Чёрный список экономит
        # время команды, а не теряет переписку; среди «надоел» однажды
        # окажется настоящий заказ от человека, который в прошлый раз был не
        # в духе.
        if entered_queue and client.blocked_at is not None:
            inbox.leave_queue(conv)
            entered_queue = False

        # ⚠ 16.08 «бот — отдельный сотрудник»: на канале с включённым АВТО-ботом
        # автораздача уступает — иначе она назначала человека раньше, чем бот
        # успевал войти своим тиком, и бот не получал ни одного диалога
        _bot_first = False
        if entered_queue and account.bot_id is not None:
            from app.bots.runtime import get_bot_for_conversation

            _bot = await get_bot_for_conversation(db, conv, account=account)
            _bot_first = bool(_bot is not None and _bot.is_enabled and _bot.mode == "auto")
        distributed_to = None
        if entered_queue and not _bot_first and inbox.is_waiting(conv):
            picked = await distribution.pick_assignee(db, redis, conv)
            if picked.assignee is not None:
                await inbox.assign_by_distribution(
                    db, conv, picked.assignee, load_before=picked.load_before, now=created_at
                )
                entered_queue = False
                distributed_to = picked.assignee

        queue_frame = (
            await inbox.inbox_frame_addressed(db, conv, now=created_at)
            if entered_queue and inbox.is_waiting(conv)
            else None
        )

    # Ленивое обогащение диалога — строго после commit'а: задача читает уже
    # видимые строки. Функция дедуплицирует себя по conversation_id и не бросает
    # наружу — упавшая очередь не должна ронять обработку вебхука.
    #
    # ДВА ПОВОДА, А НЕ ОДИН. Имя клиента вебхук не несёт никогда, объявление —
    # тоже: боевой Авито кладёт в него только числовой `item_id`, которого
    # разбор не знает (docs/33 §14а, критично). Диалог у известного клиента
    # создаётся без первого повода, но с пустым объявлением — и до 11 августа
    # такой диалог оставался с прочерком навсегда.
    #
    # ⚠ ТРЕТИЙ ПОВОД — ССЫЛКА НА ПРОФИЛЬ (просьба владельца 02.09), И БЕЗ НЕГО
    # ПРАВКА НЕ РАБОТАЕТ ВОВСЕ. Обогащение умеет добирать профиль, но задача
    # ставится ровно по двум поводам выше, а у клиента, с которым переписываются
    # давно, и имя, и объявление известны. То есть у ВСЕХ, кто был заведён до
    # 02.09, задача не поставилась бы ни разу, и кнопка не появилась бы ни у
    # кого, кроме новых. Дефект «написано, но не подключено» в чистом виде.
    #
    # ⚠ И ЭТО НЕ ПОВОД НА КАЖДОЕ СООБЩЕНИЕ. `profile_checked_at` гасит повод
    # навсегда после первого же вопроса к Авито — успешного или пустого.
    # Значит цена конечна и равна числу клиентов, которые нам написали, а не
    # числу сообщений. Заполняется по мере разговора: у тех, с кем говорят, — в
    # тот же день; у молчащей истории — никогда, и это верно, в их карточку
    # никто не смотрит.
    if client_created_without_name or not conv.item_title or client.profile_checked_at is None:
        await enqueue_enrich_client(redis, conv.id)

    # ⚠ ПРОВЕРКА АДРЕСА ПО КАРТЕ — ЗДЕСЬ, ПОСЛЕ COMMIT'А, И НЕ НА ДОГРУЗКЕ
    # ИСТОРИИ. Задача бежит к строке по идентификатору, и до commit'а строки
    # нет (класс «кадр до commit'а»). Догрузка истории кладёт тысячи старых
    # реплик разом — их проверит починка в своём темпе, а не залп в чужую карту.
    if not backfill:
        for cid in адрес_на_проверку:
            await enqueue_geocode(redis, cid)
    if _точка_подтверждена_авито(msg, причина_адреса) and not backfill:
        # Точка Авито уже «подтверждена картой» — сразу к автозаписи.
        await enqueue_autofill(redis, conv.id)
    if адрес_на_чтение and not backfill:
        await enqueue_llm_read(redis, conversation_id=conv.id, message_id=msg.id)
    # Вопрос об адресе — ЗДЕСЬ, до блока бота ниже: в нём ранний `return True`
    # на `debounced`, и поставленный после него вопрос терялся бы на каждой
    # серии. Строка входящего уже закоммичена — задача найдёт её по id.
    if вопрос_об_адресе_через is not None:
        await address_ask.enqueue_address_ask(
            redis, conversation_id=conv.id, message_id=msg.id, defer_sec=вопрос_об_адресе_через
        )
    # ⚠ ДВОЙНИКИ ПО ТЕЛЕФОНУ — ТОЖЕ ПОСЛЕ COMMIT'А И ТОЖЕ НЕ НА ИСТОРИИ. Задача
    # смотрит на две карточки в базе; до commit'а основной ещё не записан.
    # Догрузка истории идёт от новых к старым и не в том порядке, в каком люди
    # писали, — её пары соберёт ночной проход в своём темпе.
    if основной_появился and client.phone and not backfill:
        await enqueue_merge(redis, client.phone)

    # ⚠ ИСТОРИЯ ЧАТА — СРАЗУ, КАК ТОЛЬКО ОН ПОЯВИЛСЯ ВО «ВХОДЯЩИХ» (просьба
    # владельца 28.08: «чтобы если чат приходит во входящие, он автоматом
    # подтягивал историю сообщений»).
    #
    # ПОЧЕМУ ЭТОГО НЕ ДЕЛАЛА СВЕРКА. Она берёт границу истории по последнему
    # ВХОДЯЩЕМУ диалога, а у диалога, только что созданного вебхуком, это ровно
    # то самое сообщение, которым он и создан. Дальше стоит быстрый отсев
    # «последнее сообщение чата уже у нас» — и он срабатывает всегда. Переписку,
    # которая была в чате ДО первого дошедшего вебхука, не подтягивал никто и
    # никогда: оператор открывал диалог и видел одну строку без всякого «что
    # было раньше».
    #
    # ТОЛЬКО НА СОЗДАНИИ И ТОЛЬКО НА ЖИВОМ ПУТИ. Массовая загрузка (`backfill`)
    # заводит диалоги сама и своей же историей — звать оттуда значило бы на
    # каждый импортированный чат ставить задачу импортировать тот же чат.
    if conv_created and not backfill:
        from app.services.avito_accounts import enqueue_conversation_history

        await enqueue_conversation_history(
            account.id, conv.id, str(conv.external_chat_id), live_since=msg.created_at
        )

    # Второй шаг пути: сообщение в базе. Дальше по этому же `message_id` видно,
    # разбудило ли оно бота и дошёл ли ответ (`app/core/trace.py`).
    trace.step(
        "trace.inbound_stored",
        message_id=str(msg.id),
        conversation_id=str(conv.id),
        account_id=str(account.id),
        status=conv.status,
    )

    # ⚠ РАСШИФРОВКА ГОЛОСОВОГО СТАРТУЕТ СРАЗУ, А НЕ ПО НАЖАТИЮ (просьба
    # владельца «давай свой Whisper»). Не потому, что запись пропадёт: здесь
    # стояло «ссылка живёт минуты, к вечеру Авито не отдаст и её», и замер
    # 06.09 это опроверг — сама запись у Авито живёт ≥29 дней, а подписанная
    # ссылка ≥30 минут. Причина другая: текст нужен диспетчеру В МОМЕНТ, когда
    # он открывает диалог, а не через минуты счёта после нажатия.
    #
    # ⚠ ЗДЕСЬ, ПОСЛЕ COMMIT'А, И ТОЛЬКО НА ЖИВОМ ПУТИ. `backfill` — это импорт
    # прошлой переписки; ставить на неё расшифровки прямо здесь значило бы на
    # каждом подключении канала засыпать очередь сотнями задач разом. Записи
    # при этом никуда не денутся: их подберёт досчёт (`jobs/voice_repair.py`)
    # — по двадцать раз в десять минут, новые вперёд, не старше 30 дней.
    #
    # Своей ветки под ошибку нет намеренно: `enqueue_transcribe` не бросает —
    # расшифровка это удобство, и терять из-за неё живой вебхук клиента нельзя.
    if not backfill and voice_service.has_voice(msg.attachments):
        await voice_service.enqueue_transcribe(redis, msg.id, _aware_utc(msg.created_at))

    if publish and причина_кадра_клиента is not None:
        # ⚠ ЗДЕСЬ, А НЕ ПО МЕСТУ РАЗБОРА. Пока кадр уходил изнутри транзакции,
        # экран перезапрашивал карточку и читал ещё старую строку — телефон
        # появлялся только после F5 (жалоба владельца 02.09).
        await _известить_о_клиенте(redis, conv, client, reason=причина_кадра_клиента)

    if publish:  # строго ПОСЛЕ commit (08 §8.1)
        await publish_event(
            redis,
            "message:new",
            {
                "conversation_id": str(conv.id),
                "message": message_out(msg),
                "conversation_patch": {  # 01 §11.3: дельта, не абсолют
                    # ⚠ ЧЕЙ ЭТО ДИАЛОГ — ЧАСТЬ КАДРА (обратная связь 02.09).
                    #
                    # Звук на входящее играл у ВСЕХ и по ЛЮБОМУ диалогу: за
                    # смену это шестьсот сигналов на человека, из которых его
                    # касается меньше десятой части. Дословно: «пищит просто
                    # так самым омерзительным звуком… крч не работопригодно».
                    #
                    # Чтобы звенеть только по своим, экрану нужно знать хозяина
                    # диалога. Из кэша он его знает не всегда (строки может не
                    # быть вовсе), а неизвестность здесь означала бы либо тишину
                    # по своему диалогу, либо возврат шума.
                    # Только идентификатор: на вопрос «мой ли это диалог» имени
                    # не нужно, а тянуть его — лишний поход в базу на каждое
                    # входящее сообщение.
                    "assignee_id": str(conv.assignee_id) if conv.assignee_id else None,
                    "unread_delta": 1,
                    "last_message_at": iso(conv.last_message_at),
                    "status": conv.status,
                    # ⚠ И ЗАЖЕЧЬ ШКАЛУ — ТОЖЕ ЗАДАЧА ЭТОГО КАДРА (28.08).
                    #
                    # Пара к соседнему кадру эха, где ожидание ГАСНЕТ. Клиент
                    # написал в диалог, на который мы уже отвечали: в строке
                    # списка лежит `waiting_since: null` — честный ответ сервера
                    # «не ждёт» на момент последней загрузки. Без поля в патче
                    # он там и останется, и диалог, где клиент ждёт с этой
                    # секунды, будет стоять без оранжевой метки: непрочитанное
                    # видно, а СКОЛЬКО человек ждёт — нет. Ровно то место, где
                    # метка и нужна.
                    #
                    # Молчание опаснее лишней тревоги: пропущенное ожидание —
                    # это клиент, о котором забыли, а лишнее — секунда взгляда.
                    "waiting_since": iso(status_dict.waiting_since(conv)),
                },
            },
        )
        if reopened_patch:
            # переоткрытие: ответственный снят, диалог снова ничей — экраны
            # обязаны узнать сразу, а не после перезагрузки (аудит 16.08)
            await publish_event(
                redis,
                "conversation:updated",
                {
                    "conversation_id": str(conv.id),
                    "patch": {
                        "status": conv.status,
                        "assignee": None,
                        "in_inbox": inbox.is_waiting(conv),
                        "offered_at": iso(conv.offered_at),
                    },
                },
            )
        if distributed_to is not None:
            # автораздача: получатель обязан узнать о клиенте звуком и ⚑,
            # остальные — увидеть ответственного (аудит синхронизации 16.08)
            _dst = user_ref(distributed_to)
            await publish_event(
                redis,
                "conversation:assigned",
                {
                    "conversation_id": str(conv.id),
                    "assignee": _dst,
                    # У автораздачи учётной записи нет — только подпись; отдел
                    # у неё пустой, и скобок в ленте не появится.
                    "assigned_by": user_ref_parts(None, "Автораздача"),
                    "comment": None,
                },
            )
            await publish_event(
                redis,
                "conversation:updated",
                {
                    "conversation_id": str(conv.id),
                    "patch": {"assignee": _dst, "status": conv.status, "in_inbox": False},
                },
            )
        if queue_frame is not None:
            # `inbox:new` — единственный кадр очереди, который публикует не
            # ручка, а конвейер: диалог встаёт в очередь сам, без чьего-либо
            # нажатия (план 7.1). Вторым, после `message:new`: у операторов,
            # которые очередь не смотрят, порядок «сообщение → строка очереди»
            # совпадает с порядком в жизни.
            await publish_inbox_new(
                redis,
                queue_frame["conversation"],
                eligible_operator_ids=queue_frame.get("eligible"),
            )
        if reopened_notice is not None:
            # Строка уже в базе и уже посчитана колокольчиком; здесь — только
            # кадр тому, кто сейчас смотрит на экран. Под `publish` намеренно:
            # сверка истории (`publish=False`) поднимает старую переписку, и
            # всплывать у оператора ей незачем — счётчик при этом честен.
            await notifications.deliver(redis, reopened_notice)

    # Бот (DESIGN §8.3, 02 §2.2) — тоже строго после commit'а и только на
    # настоящем входящем: `should_run_bot` проверяет расписание, muted, handoff,
    # ответы менеджера и статус, и в очередь ничего не уйдёт, если бота звать не
    # надо. Импорт локальный: движок ходит обратно в app.services.messages
    # (as_arq), и на верхнем уровне это был бы цикл импортов.
    #
    # Три решения в этих шести строках:
    # 1. ПОСЛЕДНИМ шагом, после publish: свой rollback этот блок пережить может,
    #    а rollback гасит (expire) уже прочитанные ORM-объекты — событие WS,
    #    собранное после него, полезло бы за ними в БД повторно.
    # 2. Чтение и постановка разнесены (вместо однострочного
    #    `maybe_enqueue_bot_step`): SELECT'ы закрывают свою транзакцию ДО похода
    #    в Redis, иначе подвисший Redis держал бы «idle in transaction» (05 §8).
    # 3. `conversation_id` снят заранее — в ветке лога `conv` уже может быть
    #    погашен, и `conv.id` ушёл бы в БД за новым SELECT'ом.
    # ⚠⚠ 26.08: ЗДЕСЬ НЕ БЫЛО НИ ОКНА СЕРИИ, НИ ПАУЗЫ — И ЭТО БЫЛА ГЛАВНАЯ ПОЛОМКА.
    # Владелец: «бот секунда в секунду отвечает». Так и было: этот путь — единственный
    # боевой (вебхук Авито), а `debounce_claim`/`defer_by` жили в `maybe_enqueue_bot_step`,
    # который зовут ТОЛЬКО тесты. То есть окно серии не работало в бою НИКОГДА, и обе
    # прошлые правки числа (15→35→90→180 секунд) не меняли ровно ничего.
    # Отсюда же родом снимок владельца от 26.08 «клиент пишет в 03:10 и в 03:11, бот
    # отвечает дважды почти одним текстом»: серию гасить было нечем.
    #
    # ⚠ ТРИ РЕШЕНИЯ НИЖЕ СОХРАНЕНЫ ПОЛНОСТЬЮ. Чтение по-прежнему закрывает свою
    # транзакцию ДО похода в Redis, постановка идёт последним шагом, а `conversation_id`
    # снят заранее. Добавлены ровно две строки: заявка окна и отсрочка задачи.
    if not backfill:
        from app.bots.runtime import (
            debounce_claim,
            debounce_window,
            enqueue_bot_step,
            get_bot_for_conversation,
            should_run_bot,
        )

        conversation_id = conv.id
        try:
            async with db.begin():
                wake_bot = await should_run_bot(db, conv, account=account)
                бот = (
                    await get_bot_for_conversation(db, conv, account=account) if wake_bot else None
                )
            if wake_bot:
                # ⚠ ОКНО СЧИТАЕТСЯ ОДИН РАЗ НА СЕРИЮ И УХОДИТ В ОБЕ СТОРОНЫ. Оно зависит от
                # формы сообщения (фото — 40 с, вопрос — 90 с), и второй вызов без `текст`
                # молча дал бы другое число: ключ Redis протух бы раньше задачи или позже неё.
                окно_серии = debounce_window(бот, conversation_id, текст=_текст_для_бота(event))
                if not await debounce_claim(redis, conversation_id, бот, окно=окно_серии):
                    # тик на эту серию уже стоит; он прочитает историю сам и увидит всю серию
                    log.info("bot.debounced", conversation_id=str(conversation_id))
                    return True
                # ⚠ ФОТО — ЭТО ОТВЕТ (снимок владельца 26.08). Клиент прислал снимок
                # блока питания, а через две минуты бот передал диалог с причиной
                # «клиент не ответил в отведённое время». Для движка сообщение без
                # текста неотличимо от молчания: в шаг уезжает только `event.text`.
                # Отдаём маркер вложения — дальше `validate_answer` видит непустой
                # ответ, ожидание снимается, и разговор идёт к шагу ИИ, который
                # картинку как раз читать умеет.
                await enqueue_bot_step(
                    redis,
                    conversation_id,
                    _текст_для_бота(event),
                    defer_by=окно_серии,
                )
        except Exception:  # noqa: BLE001 — сообщение уже в БД; бот не повод терять вебхук
            log.exception("inbound.bot_enqueue_failed", conversation_id=str(conversation_id))

    return True
