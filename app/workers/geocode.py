"""Задачи проверки адреса по карте и автозаписи в карточку (11.09).

ПРОСЬБА ВЛАДЕЛЬЦА: «поле должно работать полностью автоматически — само
проверять адрес по картам, зная город клиента из объявления».

ТРИ ФАЗЫ, КАК У `enrich_client`, И ПО ТЕМ ЖЕ ПРИЧИНАМ:
1. читаем строку, диалог и город — простыми значениями, сессия закрывается
   ДО похода наружу (держать соединение пула на время HTTP нельзя);
2. идём к карте под замком темпа;
3. пишем вердикт УСЛОВНЫМ UPDATE (`WHERE geo_status IN ('pending','error')`):
   пока ходили, оператор мог решить строку руками, и его решение старше.

⚠ ЗАМОК ТЕМПА — `SET NX EX`, НЕ `INCR`+`EXPIRE` (урок «скользящий срок
ключа»). Nominatim просит не чаще запроса в секунду: ключ живёт секунду и
никем не удаляется — следующий запрос ждёт его смерти. Ожидание — внутри
задачи, а не через `Retry`: `Retry` тратит попытки из `max_tries`, и после
трёх занятых секунд строка навсегда оставалась бы `error`.

⚠ АВТОЗАПИСЬ — ОТДЕЛЬНАЯ ЗАДАЧА С ОКНОМ ТИШИНЫ. Карта подтвердила дом —
через девяносто секунд `autofill_address` проверяет, что за это время клиент
не назвал другой адрес и оператор не вписал свой, и пишет одним условным
`UPDATE … WHERE address IS NULL`. Гонку с ручным вводом решает база, а не
порядок чтения.

⚠ В ЖУРНАЛЕ — ТОЛЬКО ИДЕНТИФИКАТОРЫ, ПРОВАЙДЕР, СТАТУС И МИЛЛИСЕКУНДЫ. Адрес
клиента — персональные данные; `str(exc)` от httpx несёт URL с адресом, и
наружу он не уходит (`GeocodeError` без URL).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from arq import Retry
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import with_job_scope
from app.integrations import (
    ahunter,
    dadata,
    gateway,
    nominatim,
    openrouter,
    speller,
    yandex_geocoder,
    yandex_suggest,
)
from app.integrations.avito.listing_url import city_by_name
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.models.client import (
    CANDIDATE_ACCEPTED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CANDIDATE_SOURCE_LLM,
)
from app.services import address_parse, app_settings, geo_cache, geocode, voice
from app.services.clients import (
    _ЧАСТИ_АДРЕСА,
    address_candidates,
    apply_candidate_to_card,
    auto_address_yields_to,
    candidate_address_text,
    candidate_grade,
    candidate_rule,
    grade_beats,
    grade_weight,
    refresh_auto_address,
    ключ_содержания,
    одно_место,
    снимок_улик,
)
from app.services.conversations import (
    conversation_city,
    conversation_city_slug,
    other_conversation_cities,
)
from app.services.geocode_queue import enqueue_autofill, enqueue_geocode, enqueue_llm_read
from app.ws.events import publish_event

log = structlog.get_logger()

MAX_TRIES = 3
#: Замок темпа к Nominatim: секунда на запрос, ждём до полуминуты.
RATE_LOCK_KEY = "geo:lock:nominatim"
RATE_LOCK_TTL_SEC = 1
#: Полминуты было мало: строка с подсказкой делает до шести походов к OSM, и
#: в пачке починки задачи толпились у замка (бой 12.09: 38 таймаутов за два
#: часа, все закрылись повтором).
RATE_WAIT_MAX_SEC = 60.0
RATE_WAIT_STEP_SEC = 0.25
#: Автозапись поверх места берёт только свежие строки: адрес из вчерашней
#: переписки сегодня могли уже обсудить и отменить. В ПУСТУЮ карточку —
#: любой давности (владелец 13.09: 926 заявок с подтверждённым картой адресом
#: и пустой карточкой): перезаписывать нечего, а цитата и вердикт карты видны.
AUTOFILL_MAX_AGE = timedelta(hours=24)
#: ЗАПАС СУТОЧНОГО ПОТОЛКА ДЛЯ ЖИВОГО ПОТОКА (бой 13.09: догон хвоста выел
#: DaData к половине одиннадцатого утра, Яндекс — к пяти, и до полуночи
#: каждая новая реплика шла по одному OSM). Задача, поставленная починкой,
#: считает свой потолок как `потолок − запас`; живая реплика — весь потолок.
#: Яндекс и Саджест с 20.09 (пакет 5) — не запасом, а ДОЛЕЙ в процентах
#: (`address_geo.repair_share_yandex`, умолчание 30 %): решение владельца
#: «300 из 1 000», настройкой.
ЗАПАС_ЖИВЫМ: dict[str, int] = {"dadata": 1500}
#: Потолок Яндекса `None` (платный тариф без нашего потолка) для ДОЛИ починки
#: считается за бесплатную тысячу: проценты от бесконечности — не доля, а
#: залп recheck из 554 строк × 3 похода стоил бы денег (ревью 20.09, п. 8).
REPAIR_SHARE_BASE_UNLIMITED = 1000
#: Отказы карты, которые БЕЗ DaData — не приговор. DaData — справочник ФИАС и
#: первая в цепочке; OSM не знает половины домов России, Яндекс за потолком.
#: Пока DaData на сутки недоступна (потолок выбран, 403), такой отказ
#: остаётся `pending`: починка перепроверит строку, когда DaData вернётся,
#: а человек видит «проверяем по карте», а не ложное «не найдено».
#: Одно множество с судьёй карточки (`card_grade`): у такого отказа адрес
#: может лечь строкой улицы без точки — и оба читают один список.
_БЕЗ_DADATA_НЕ_ПРИГОВОР = geocode.REFUSAL_STATUSES
ORIGIN_LIVE = "live"
ORIGIN_REPAIR = "repair"
#: Дольше этого строка без DaData не ждёт: вердикт карт послабее становится
#: окончательным. Трое суток — с запасом на выходные без владельца.
ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ = timedelta(days=3)


def dadata_настроена(включена: bool, потолок: int | None) -> bool:
    """DaData в деле: включена, ключ есть, потолок не «ноль — не спрашивать»."""
    return включена and dadata.enabled() and (потолок is None or int(потолок) > 0)


@dataclass(frozen=True, slots=True)
class DryVerdict:
    """Итог одного суда карты для сухого прогона (пакет 6.0б, I-3) — то, что в
    бою легло бы в строку и повело бы дальше, без самой записи. Без ПД: строка
    карты, имя правила, политика, тень, след. `would_autofill` — в бою суд
    поставил бы автозапись (`card_grade` не None по тем же полям).

    `kept` — новый вердикт слабее прежних улик при неполном наборе карт, и
    удержан прежний (`keeps_previous`, пакет 5): `status`/`formatted`/`rule`/
    `policy`/`km` тогда — прежнего суда (из восстановленного следа), а не
    решения этого прогона, иначе правило получало бы чужой ключ (ревью 21.09,
    #7). `missing` — карты, без которых суд шёл (`_Отказы.недостаёт`): по нему
    прогон отличает свежее решение от слепого."""

    status: str
    formatted: str | None
    provider: str
    hit_city: str | None
    km: float | None
    rule: str | None
    policy: str | None
    shadow: geocode.Shadow | None
    trace: dict[str, Any] | None
    variants_n: int
    office: str | None
    would_autofill: bool
    kept: bool = False
    missing: tuple[str, ...] = ()


@dataclass(slots=True)
class DrySpill:
    """Копилка сухого суда: открывает :func:`dry_judge`, заполняет
    :func:`geocode_candidate` (или `_geocode_place`) в конце суда.
    `dadata_requests` — СВОИ походы DaData этого суда (по `_занять_dadata`,
    ответ из кэша не считается): суточный ключ `geo:dadata:calls` общий с
    живым потоком и починкой, и бюджет прогона по нему считать нельзя."""

    verdict: DryVerdict | None = None
    dadata_requests: int = 0


#: ⚠ ПОЧЕМУ КОПИЛКА, А НЕ ВТОРОЕ ВОЗВРАЩАЕМОЕ ЗНАЧЕНИЕ (образец
#: `inbound._СЛЕДСТВИЯ`). `geocode_candidate` зовут ARQ и 224 вызова тестов и
#: сравнивают его строку-статус; сухой прогон CLI — единственный, кому нужен сам
#: вердикт (строка карты, правило, км, след). Копилка открывается только им:
#: подпись и статус для всех остальных прежние, а второго пути суда «без записи»
#: (класс `dva-puti-raznyi-schet`) не заводится — сухой суд идёт тем же телом с
#: флагом `dry_run`.
_СУХОЙ_СУД: contextvars.ContextVar[DrySpill | None] = contextvars.ContextVar(
    "geocode_сухой_суд", default=None
)


def _сложить_в_копилку(вердикт: DryVerdict) -> None:
    копилка = _СУХОЙ_СУД.get()
    if копилка is not None:
        копилка.verdict = вердикт


def _посчитать_поход_в_копилку() -> None:
    """Состоявшийся поход DaData — в счёт копилки сухого прогона (бюджет
    `--dadata-budget`). Вне сухого суда копилки нет — ничего не делает."""
    копилка = _СУХОЙ_СУД.get()
    if копилка is not None:
        копилка.dadata_requests += 1


async def dry_judge(
    ctx: dict[str, Any],
    candidate_id: uuid.UUID,
    *,
    origin: str = ORIGIN_REPAIR,
    policy: Mapping[str, str] | None = None,
    spill: DrySpill | None = None,
) -> tuple[str, DryVerdict | None]:
    """Сухой суд строки: тот же :func:`geocode_candidate` с `dry_run=True`, итог
    — из копилки. `None` во второй позиции — суд до вердикта не дошёл
    (`gone`, `disabled`, `paused`). `spill` — своя копилка вызывающего (новая
    на строку): по её `dadata_requests` прогон считает походы и у суда без
    вердикта (`paused` — DaData уже сходила)."""
    копилка = spill if spill is not None else DrySpill()
    метка = _СУХОЙ_СУД.set(копилка)
    try:
        статус = await geocode_candidate(
            ctx, candidate_id, origin=origin, dry_run=True, policy=policy
        )
    finally:
        _СУХОЙ_СУД.reset(метка)
    return статус, копилка.verdict


@with_job_scope
async def geocode_candidate(
    ctx: dict[str, Any],
    candidate_id: uuid.UUID,
    *,
    origin: str = ORIGIN_LIVE,
    dry_run: bool = False,
    policy: Mapping[str, str] | None = None,
) -> str:
    """`origin` — кто поставил задачу: живая реплика (`live`) или починка
    планировщика (`repair`). Починке потолки карт даются за вычетом
    :data:`ЗАПАС_ЖИВЫМ`; задачи, поставленные до этой правки, приходят без
    аргумента и считаются живыми.

    `dry_run` (пакет 6.0б, I-3) — суд без последствий: судятся строки любого
    `geo_status` (и `exact`, и принятые), а запись вердикта, кадр, задачи
    (автозапись, повтор, модель) и тревоги не выполняются; итог — в копилку
    :data:`_СУХОЙ_СУД` (см. :func:`dry_judge`). Сухой ≠ бесплатный (программа
    М-6): походы к картам идут под теми же счётчиками и долей, кэш ответов
    работает. `policy` — политика правил вместо чтения настройки
    `address_geo.rule_policy` («как если бы включили»); `None` — читается
    настройка, один раз на задачу, как прежде."""
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    # Кэш ответов карт (13.09): повторный запрос — без похода и без счётчика.
    geo_cache.bind(redis)
    # Снимок ключей шлюза (16.09, раз в минуту): по нему `enabled()` провайдеров
    # решает, кого спрашивать; после `no_key` провайдер оживает только отсюда.
    await gateway.refresh_status()
    попытка = int(ctx.get("job_try") or 1)

    # Фаза 1 — простыми значениями.
    async with factory() as db:
        row = await db.get(ClientAddressCandidate, candidate_id)
        if row is None:
            # Диалог удалили — строка ушла каскадом. Это не сбой.
            log.info("geocode.candidate_gone", candidate_id=str(candidate_id))
            return "gone"
        # Сухой суд судит и решённые строки (`exact`, отказы): его вопрос —
        # «что решило бы правило сегодня», а не «нужна ли проверка».
        if not dry_run and row.geo_status not in _ПЕРЕПРОВЕРЯЕМЫЕ:
            return "already"
        if not await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED):
            return "disabled"
        conv = await db.get(Conversation, row.conversation_id)
        city = conversation_city(conv) if conv is not None else None
        if city is None and row.locality:
            # У объявления города нет, а клиент его назвал («Геленджик ул.
            # Цветочная 7», бой 13.09): город клиента и есть город для карты.
            city = city_by_name(row.locality, declined=True)
        elif city is not None and row.locality:
            # Клиент назвал соседний город той же области («Город Щёлково …»
            # при объявлении в Ивантеевке, владелец 14.09): дом ищем там, где
            # живёт клиент, а не где висит объявление. Другой регион — не
            # подменяем: сторож «клиент из другого города» скажет своё.
            # КРОМЕ города, по объявлению в котором тот же человек уже писал
            # (владелец 19.09: «11а-42» при объявлении в Саранске от клиента с
            # диалогом из Нефтеюганска — другой регион, но клиент живёт там,
            # где сам писал по объявлениям; по области Мордовии дом не найти).
            # `declined`: у строк до пакета 5 в `locality` лежит падежная форма
            # («Салавате» из «г Салавате») — город находится без перепарса.
            названный = city_by_name(row.locality, declined=True)
            if названный is not None and (
                geocode.region_matches(city.region, названный.region)
                or await _город_клиента_по_другим_диалогам(db, row, названный.name)
            ):
                if not geocode.region_matches(city.region, названный.region):
                    log.info(
                        "geocode.city_from_client_dialogs",
                        candidate_id=str(candidate_id),
                        listing_city=city.name,
                        client_city=названный.name,
                    )
                city = названный
        if city is None and conv is not None and conversation_city_slug(conv) is None:
            # Ни у объявления, ни в реплике города нет (стенд 15.09: 46 строк
            # `no_city` за месяц) — но тот же человек писал по другим
            # объявлениям, и у них город есть: берём самый свежий. Слаг,
            # которого справочник не знает, — не «города нет»: чужой город тут
            # не нужен (ревью 15.09), это повод пополнить справочник.
            другие = await other_conversation_cities(db, row.client_id, row.conversation_id)
            city = другие[0] if другие else None
        parsed = geocode.Parsed(
            street=row.street,
            house=row.house,
            settlement=row.settlement,
            settlement_type=row.settlement_type,
            locality=row.locality,
            level=row.level,
            # Массив внутри пункта у дома без улицы («д. Ивняково, СНТ
            # Рассвет, 17», 18.09) — улицей служит массив.
            area=row.area,
            # Район и регион словами клиента (пакет 6.0а, I-1): улики для
            # правил, которым нужно слово о месте шире пункта.
            district=row.district,
            region=row.region,
        )
        # Место с домом («СНТ Светлый 23» как место + номер) идёт путём
        # дома: у места карта ищет только точку, а здесь есть что сверять.
        row_kind = address_parse.KIND_HOUSE if (row.house or "").strip() else row.kind
        row_source, row_message_id = row.source, row.message_id
        row_level = row.level
        # Город известен человеку, если у объявления есть слаг — пусть и не из
        # справочника: спрашивать клиента о городе тогда нельзя (`ask_reason`).
        city_known = conv is not None and conversation_city_slug(conv) is not None
        # Модель перечитывает только живой диалог (реплика моложе суток) и
        # только строки уровней A/B: хвост починки и невод уровня C выели бы
        # суточный потолок впустую (ревью 13.09).
        # Отложенной без DaData строке (попыток 0, проверка была) окно вдвое
        # шире — двое суток от реплики: назавтра DaData откажет, и модель ещё
        # успеет перечитать; хвост починки месячной давности сюда не попадает
        # (ревью 13.09).
        отложенная = not row.geo_attempts and row.geo_checked_at is not None
        окно = timedelta(hours=48 if отложенная else 24)
        row_для_модели = (
            row.level != address_parse.LEVEL_C
            and row.message_at is not None
            and _aware(row.message_at) >= datetime.now(UTC) - окно
        )
        место = geocode.Place(
            settlement=row.settlement,
            settlement_type=row.settlement_type,
            area=row.area,
            district=row.district,
            level=row.level,
            street=row.street or "",
            locality=row.locality,
        )
        # Массив для точки участка (правило `area_point`, 18.09): пункт с типом
        # массива с участками («СНТ Светлый 23») — массивом, не пунктом:
        # с массивом в роли пункта `place_verdict` отдал бы посёлок-тёзку.
        место_массива = _место_массива(row, место)
        # ПУНКТ ИЗ СОСЕДНЕЙ РЕПЛИКИ (бой 12.09, Череповец): «Новое заозерье»
        # одним сообщением, «Рябиновая 8» — следующим. Без пункта карта нашла
        # три Рябиновых, 8 по области и предложила выбирать; с подсказкой
        # выбирает та самая. Подсказка — только для сверки с найденными
        # домами, в запрос к карте не идёт.
        подсказки = (
            address_parse.settlement_hints(await _соседние_реплики(db, row))
            if row.settlement is None
            else []
        )
        режим = str(await app_settings.get(db, app_settings.ADDRESS_GEO_PROVIDER) or "nominatim")
        # Имя провайдера — того, кто реально отвечал; режим цепочки в строку
        # не пишется (бой 12.09: `no_city` получил провайдера «osm_then_yandex»).
        provider = "yandex" if режим == "yandex" else "nominatim"
        потолок_яндекса = await app_settings.get(db, app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT)
        саджест_включён = bool(await app_settings.get(db, app_settings.ADDRESS_GEO_SUGGEST_ENABLED))
        потолок_саджеста = await app_settings.get(db, app_settings.ADDRESS_GEO_SUGGEST_DAILY_LIMIT)
        доля_яндекса_pct = app_settings.repair_share_pct(
            await app_settings.get(db, app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX)
        )
        # Ahunter и Спеллер (16.09): без ключа, Ahunter — без потолка, Спеллер —
        # 10 000 в сутки; хвост починки их получает наравне с живым потоком.
        ahunter_включён = bool(await app_settings.get(db, app_settings.ADDRESS_GEO_AHUNTER_ENABLED))
        speller_включён = bool(await app_settings.get(db, app_settings.ADDRESS_GEO_SPELLER_ENABLED))
        потолок_спеллера = await app_settings.get(db, app_settings.ADDRESS_GEO_SPELLER_DAILY_LIMIT)
        потолок_dadata = await app_settings.get(db, app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT)
        # Потолок 0 — «не спрашивать вовсе» (контракт настройки): это
        # выключенная DaData, а не «на сегодня выбрана» — OSM приговаривает,
        # как без ключа (ревью 13.09).
        dadata_включён = dadata_настроена(
            bool(await app_settings.get(db, app_settings.ADDRESS_GEO_DADATA_ENABLED)),
            потолок_dadata,
        )
        # Автопривязка «решить сама» (владелец 18.09): один выключатель на
        # правила, строку улицы, страну, дробь и массив. Выключен — вердикты
        # и варианты как до 18.09.
        авто_включён = bool(await app_settings.get(db, app_settings.ADDRESS_GEO_AUTO_DECIDE))
        # ПОЛИТИКА ПРАВИЛ (пакет 6.0а, §0.3) — один раз на задачу, вход
        # `auto_decide`. Чтение нестрогое: имя, снятое из реестра после
        # записи, пропускается поимённо, остальные перекрытия действуют —
        # строгий разбор обнулял бы всю строку и возвращал выключенное
        # правило к `exact` (ревью 20.09, #5/#14). Сухой прогон приносит
        # политику словарём (`policy`) — настройка тогда не читается вовсе.
        политика: Mapping[str, str] = (
            policy
            if policy is not None
            else geocode.parse_rule_policy(
                str(await app_settings.get(db, app_settings.ADDRESS_GEO_RULE_POLICY) or ""),
                strict=False,
            )
        )
        # СЛЕД строки на момент чтения — база для `trace` вердикта: ключи
        # разбора остаются, прежний суд уходит в `prev` (`verdict_trace`).
        след_строки = dict(row.trace) if isinstance(row.trace, dict) else {}
        # СНАЧАЛА origin, ПОТОМ возраст (пакет 5, 20.09). Хвост починки получает
        # Яндекс и Саджест долей — решение владельца «всё автоматически, точность
        # должна только расти»; до 20.09 ветка возраста стояла раньше и
        # обнуляла потолок починке любой строки старше недели (`доля_починки(0) == 0`).
        if origin == ORIGIN_REPAIR:
            потолок_dadata = доля_починки(потолок_dadata, "dadata")
            потолок_яндекса = доля_починки(потолок_яндекса, "yandex", yandex_pct=доля_яндекса_pct)
            потолок_саджеста = доля_починки(
                потолок_саджеста, "yandex_suggest", yandex_pct=доля_яндекса_pct
            )
        elif row.message_at is not None and _aware(row.message_at) < datetime.now(UTC) - timedelta(
            days=7
        ):
            # ЖИВАЯ задача по реплике старше недели (догон через живой путь:
            # голосовое, карточка по старой строке, обогащение города) — без
            # Яндекса и Саджеста, как с 13.09: страховка от анонимного залпа в
            # суточный потолок. Хвост починки сюда не попадает: у него доля выше,
            # а слепой вердикт живой задачи вернёт обход `kept_blind`.
            потолок_яндекса = 0
            саджест_включён = False
        # DaData настроена, но не ответила: потолок выбран, 403, сеть. Отказы
        # остальных карт при этом — не приговор (ниже). Выставляется по факту
        # похода: попадание в кэш ответов бесплатно и за потолком (ревью 13.09).
        dadata_недоступна = False
        # Отказ без DaData откладывается только у свежей строки: мёртвый ключ
        # или кончившийся тариф иначе заморозили бы проверку навсегда и молча
        # (ревью 13.09); строка старше — принимает вердикт карт послабее.
        свежая = (
            _aware(row.message_at or row.detected_at) >= datetime.now(UTC) - ОТКЛАДЫВАТЬ_НЕ_ДОЛЬШЕ
        )
        client_id, conversation_id = row.client_id, row.conversation_id
        # ⚠ СНИМОК, ПО КОТОРОМУ СЧИТАЕМ. Пока задача в пути, клиент может дописать
        # посёлок в ту же строку («ул Ленина 5» → «…, пос. Ударник»); вердикт
        # без посёлка тогда ложиться поверх не имеет права (находка ревью 11.09,
        # воспроизведена настоящим воркером ARQ). Фаза 3 сверяет снимок.
        снимок = _Снимок(settlement=row.settlement, locality=row.locality)
        содержание = ключ_содержания(row)
        # УЛИКИ ПРЕЖНЕГО ВЕРДИКТА (пакет 5): снимок из `geo_prev` — только если он
        # про ЭТУ строку. Содержание изменилось без сброса (пункт дописан старой
        # репликой, массив уточнён, `reparse`) — снимок про другой разбор:
        # стирается и не читается (ревью 20.09, п. 3).
        прежнее = row.geo_prev if isinstance(row.geo_prev, dict) else None
        if прежнее is not None and прежнее.get("content") != содержание:
            log.info(
                "geocode.rejudge_prev_stale",
                candidate_id=str(candidate_id),
                was=прежнее.get("status"),
            )
            # Сухой суд снимок не стирает — только не читает его.
            if not dry_run:
                await db.execute(
                    sa.update(ClientAddressCandidate)
                    .where(ClientAddressCandidate.id == candidate_id)
                    .values(geo_prev=None)
                )
                await db.commit()
            прежнее = None
        # СУХОЙ ПЕРЕСУД РЕШЁННОЙ СТРОКИ — С ЕЁ УЛИКАМИ (ревью 21.09, #3). У
        # решённой строки снимка нет по построению (`_с_учётом_прежнего`
        # стирает его при полном наборе), а любой боевой пересуд такой строки
        # (`address-recheck`, обход починки) сначала кладёт `снимок_улик(row)`
        # в `geo_prev` и лишь потом судит. Без снимка сухой суд при выбранной
        # доле Яндекса выносил бы `exact`-строке `not_found` там, где бой
        # удержал бы её улики (класс «два пути считают одно по-разному»).
        # Снимок — только в памяти; `content` совпадает по построению;
        # `reason="dry_run"` отличает такие `rejudge_kept` в журнале.
        if dry_run and прежнее is None and row.geo_status not in geocode.CHECKING_STATUSES:
            прежнее = снимок_улик(row, reason="dry_run")
        # Отказы карт ПО ФАКТУ походов этой строки (пакет 5, ревью 20.09 п. 5):
        # «набор карт неполный» для удержания прежних улик считается по ним, а
        # не по общему счётчику в конце суда.
        отказы = _Отказы()

    if row_kind == address_parse.KIND_PLACE:
        return await _geocode_place(
            factory,
            redis,
            candidate_id,
            место,
            city,
            снимок,
            client_id=client_id,
            conversation_id=conversation_id,
            dadata_включён=dadata_включён,
            потолок_dadata=потолок_dadata,
            потолок_яндекса=потолок_яндекса,
            origin=origin,
            свежая=свежая,
            отказы=отказы,
            прежнее=прежнее,
            содержание=содержание,
            след_строки=след_строки,
            dry_run=dry_run,
        )

    query = geocode.build_query(parsed, city)
    if (
        query is not None
        and query.city is not None
        and parsed.locality
        and city_by_name(parsed.locality, declined=True) is None
    ):
        # ГОРОД, НАЗВАННЫЙ КЛИЕНТОМ, КОТОРОГО НЕТ В СПРАВОЧНИКЕ (замер 18.09:
        # «г Цимлянск, ул Ленина 5» при объявлении в Волгодонске, «Коммунарка»):
        # дом ищем в нём, а не в городе объявления — как пункт клиента главнее
        # города объявления. Область остаётся областью объявления: другой
        # регион сторож «клиент из другого города» отметит сам. Город из
        # справочника той же области подменён выше, целиком.
        query = dataclasses.replace(query, city=parsed.locality)
    # Улики и итоги — ДО ветвления: общий хвост (решение «сама», точка, строка
    # улицы, запись) один и у строки без города, и у строки с картой.
    варианты: list[dict[str, Any]] = []
    точка_города: tuple[float, float] | None = None
    hits: list[geocode.GeoHit] = []
    # Ответы по городу с именем карты, DaData первой; ответы области и кто их
    # дал — для уступки пункта, правил автопривязки и строки улицы.
    по_городу: list[tuple[list[geocode.GeoHit], str]] = []
    # Город объявления сам спорит об улице (`geocode.city_disputes_street`, N24):
    # считается один раз по всем ответам города, ниже.
    город_спорит = False
    ответы_области: list[geocode.GeoHit] = []
    область_искали = False
    провайдер_области: str | None = None
    dadata_сбой: str | None = None
    # Поиск по стране: шёл ли, и отложен ли (потолок DaData посреди него).
    страна, страна_отложена = False, False
    # Сетевые улики правил: дом «N» для «N/M», место массива для участка,
    # точка названного пункта для соседа за забором.
    дробь: tuple[list[geocode.GeoHit], str] | None = None
    place_hits: list[geocode.PlaceHit] | None = None
    пункт_hits: list[geocode.PlaceHit] | None = None
    # Место по первому слову улицы без типа («Кола» из «Кола садовая») — улика
    # прочтения (10); None — не спрашивали.
    улица_hits: list[geocode.PlaceHit] | None = None
    # Строка улицы без точки (степень `text`) — только у отказа по содержанию.
    текст_улицы: str | None = None
    # Решение правила (пригород в цепочке или `auto_decide` после неё) и тень
    # правила в `shadow` (пакет 6.0а) — в `trace` строки.
    решение: geocode.Decision | None = None
    тень: geocode.Shadow | None = None
    начало = time.monotonic()

    def _улики() -> geocode.Evidence:
        """Всё собранное по строке — для чистого судьи. Одна сборка на гейт
        запроса места (10) в цепочке и на `auto_decide` после неё: сторожа
        прочтения смотрят те же улики, что и правило (скептик 20.09, Б-4).
        Читает переменные цепочки в момент вызова."""
        return geocode.Evidence(
            status=статус,
            parsed=parsed,
            city=city,
            hits=hits,
            city_hits=по_городу,
            region_hits=ответы_области if область_искали else [],
            variants=варианты,
            city_point=точка_города,
            hints=подсказки,
            hit=hit,
            country=страна,
            region_provider=провайдер_области,
            fraction_hits=дробь,
            place=место_массива,
            place_hits=place_hits,
            settlement_place_hits=пункт_hits,
            street_place_hits=улица_hits,
        )

    async def _посчитать_dadata() -> None:
        # Тариф считает HTTP-запросы: `search` делает до двух (с пунктом и
        # без), отказ 403/429 — тоже запрос. Занимаем место до отправки —
        # атомарно, сверх потолка не идём (бой 13.09: 403 на 8 904).
        # Зовётся интеграцией только при настоящем походе: ответ из кэша
        # ни места, ни похода не стоит — и за потолком, и после 403.
        await _занять_dadata(redis, потолок_dadata)
        # Место занято — поход состоится: в копилку сухого прогона (наш
        # `limit` бросается до HTTP и похода не стоит, потому после занятия).
        _посчитать_поход_в_копилку()

    async def _место_по_первому_слову() -> None:
        """ПУНКТ ИЗ ПЕРВОГО СЛОВА УЛИЦЫ (правило `settlement_in_street`, владелец
        20.09): «Кола садовая 4/27» при объявлении в Мурманске — разбор берёт
        «Кола садовая» улицей (пункт без типа он видит только перед типом
        улицы), город дом отвергает, а Кола — город области в 9 км.
        Единственный сетевой шаг — место по первому слову
        (`search_place(«Кола», область)`, кэш неделю), и только там, где
        правило может сработать: сторожа (а)–(в) те же, что у правила
        (`street_head_candidate` на тех же уликах), отказ из
        `STREET_HEAD_STATUSES` (или «улица не та» A/B) без спора города, DaData
        в деле и в потолке. Само прочтение сети не стоит: судятся уже
        собранные ответы.

        Два входа в цепочке — перед пригородом (`elsewhere` с одним вариантом)
        и в конце (остальные отказы); место спрашивается один раз
        (`улица_hits is None`). Пишет `улица_hits` и отказ DaData в переменные
        цепочки."""
        nonlocal улица_hits, dadata_недоступна, dadata_сбой
        if not (
            авто_включён
            and улица_hits is None
            and not город_спорит
            and city is not None
            and _цепочка_продолжается(
                статус, geocode.STREET_HEAD_STATUSES, row_level, город_спорит=город_спорит
            )
            and dadata_включён
            and not dadata_недоступна
            and (первое_слово := geocode.street_head_candidate(_улики())) is not None
        ):
            return
        try:
            улица_hits = await dadata.search_place(
                geocode.Place(
                    settlement=первое_слово[0],
                    settlement_type=None,
                    area=None,
                    district=None,
                    level=row_level,
                ),
                region=city.region,
                on_request=_посчитать_dadata,
            )
        except geocode.GeocodeError as exc:
            log.warning("geocode.street_place_failed", kind=exc.kind, status=exc.status)
            await _учесть_отказ_dadata(redis, exc)
            dadata_недоступна, dadata_сбой = True, exc.kind

    if query is None:
        статус, hit = geocode.GEO_NO_CITY, None
        # ПОИСК ПО СТРАНЕ (владелец 18.09: «поиск по стране при неизвестном
        # городе — да»). Город клиента, которого нет в справочнике, — в
        # `locations`; без города — вся страна. Принимается ровно один точный
        # дом (`country_verdict`), и воркер пометит его «приблизительно»:
        # город определила карта. Несколько — варианты с областью, статус
        # остаётся `no_city` (CHECKING): город объявления, приехавший
        # мгновением позже, перепроверит строку по области, а повтор по
        # стране бесплатен (кэш ответов).
        запрос_страны = geocode.build_country_query(parsed) if авто_включён else None
        if запрос_страны is not None and dadata_включён and not dadata_недоступна:
            try:
                hits = await dadata.search(
                    запрос_страны,
                    on_request=_посчитать_dadata,
                    # «Улица дом» без пункта по стране — шум: с пунктом ищем
                    # только с ним.
                    without_settlement_too=запрос_страны.settlement is None,
                )
                статус, hit = geocode.country_verdict(parsed, hits)
                страна, provider = True, "dadata"
                if статус == geocode.GEO_AMBIGUOUS:
                    варианты = geocode.country_variants(parsed, hits)
                if статус != geocode.GEO_EXACT:
                    статус, hit = geocode.GEO_NO_CITY, None
            except geocode.GeocodeError as exc:
                log.warning("geocode.country_failed", kind=exc.kind, status=exc.status)
                await _учесть_отказ_dadata(redis, exc)
                dadata_недоступна, dadata_сбой = True, exc.kind
                # `no_city` не откладывается как отказ (не в REFUSAL_STATUSES),
                # а попытка при потолке тратиться не должна — отдельный флаг.
                страна_отложена = exc.kind in ("blocked", "limit")
                if страна_отложена and origin == ORIGIN_LIVE and not dry_run:
                    await _тревога_потолка(redis, "dadata")
                статус, hit, hits, варианты = geocode.GEO_NO_CITY, None, [], []
    else:
        # Фаза 2 — карта.
        # DADATA — ПЕРВОЙ (12.09): справочник ФИАС с координатами, знает
        # микрорайоны и полные имена улиц, терпит опечатки. Подтвердила дом —
        # остальные карты не спрашиваем; нет — цепочка OSM → Яндекс → подсказка
        # идёт как раньше, DaData не считается отказом. Её ответ при этом
        # помним целиком: «нашла дом, но не один» ценнее пустоты OSM (ниже).
        через_dadata: tuple[str, geocode.GeoHit | None, list[geocode.GeoHit]] | None = None
        ответ_dadata: tuple[str, geocode.GeoHit | None, list[geocode.GeoHit]] | None = None

        if dadata_включён:
            try:
                hits_d = await dadata.search(query, on_request=_посчитать_dadata)
                статус_d, hit_d = geocode.verdict(parsed, city, hits_d)
                ответ_dadata = (статус_d, hit_d, hits_d)
                if статус_d == geocode.GEO_EXACT:
                    через_dadata = ответ_dadata
            except geocode.GeocodeError as exc:
                log.warning("geocode.dadata_failed", kind=exc.kind, status=exc.status)
                # 403 — суточный лимит выбран: дальше не стучим до московской
                # полуночи, а не бьёмся об отказ каждой строкой; сеть, 5xx, не
                # тот JSON — флаг «лежит» на десять минут для обхода починки.
                await _учесть_отказ_dadata(redis, exc)
                # Не ответила — по любой причине: потолок, 403, сеть, 429, не
                # тот JSON. Отказ карт послабее без неё — не приговор.
                dadata_недоступна, dadata_сбой = True, exc.kind
                if exc.kind in ("blocked", "limit") and origin == ORIGIN_LIVE and not dry_run:
                    # Тревога — о настоящем потолке, а не о доле починки.
                    await _тревога_потолка(redis, "dadata")
        # Предохранитель — ПОСЛЕ DaData: бан OSM или Яндекса её не касается.
        # Провайдер под баном — не стучим, строка остаётся `pending`, починка
        # вернётся к ней, когда счётчик блокировок за час протухнет.
        if через_dadata is None and await _провайдер_забанен(redis, provider):
            log.warning("geocode.paused", candidate_id=str(candidate_id), provider=provider)
            return "paused"
        try:
            if через_dadata is not None:
                статус, hit, hits = через_dadata
                provider = "dadata"
            else:
                hits, provider = await _search(redis, provider, query, потолок_яндекса, отказы)
        except geocode.GeocodeError as exc:
            # Отвечал не тот, с кого начали: режим `yandex` за потолком или без
            # ключа уходит в OSM, а `provider` присвоиться не успел — бан и
            # запись строки должны носить имя того, кто отказал.
            provider = exc.provider
            log.warning(
                "geocode.failed",
                candidate_id=str(candidate_id),
                provider=provider,
                kind=exc.kind,
                status=exc.status,
                attempt=попытка,
            )
            # Сеть и «помедленнее» — повтор без записи: попытки считаются
            # один раз на жизнь задачи, а не на каждый HTTP (иначе один
            # час недоступности карты сжигал бы весь потолок починки).
            if exc.kind == "network" and попытка < MAX_TRIES:
                raise Retry(defer=5 * попытка) from exc
            конечный = geocode.GEO_BLOCKED if exc.kind == "blocked" else geocode.GEO_ERROR
            if dry_run:
                # Отказ карты — тоже итог суда: в копилку без записи и без
                # счётчика блокировок (тревога — о живом потоке).
                _сложить_в_копилку(_сухой_отказ(конечный, provider))
                return конечный
            await _пометить(
                factory,
                candidate_id,
                конечный,
                None,
                provider,
                None,
                снимок,
                without_dadata=not dadata_включён or dadata_недоступна,
            )
            if конечный == geocode.GEO_BLOCKED:
                await _тревога_блокировки(redis, provider)
            return конечный
        if через_dadata is None:
            статус, hit = geocode.verdict(parsed, city, hits)
        # Цепочка: OSM дом не нашёл — спрашиваем Яндекс, если ключ есть и
        # дневной потолок не выбран. Вердикт тот же код; берём ответ Яндекса,
        # если он подтвердил дом или хотя бы что-то нашёл там, где OSM пусто.
        # Не взятый ответ Яндекса — всё равно улика по городу (A5, N24): его
        # улица годится точке улицы и строке, его дом чужого типа — спору
        # города; в `по_городу` он дописывается ниже.
        ответ_яндекса: tuple[list[geocode.GeoHit], str] | None = None
        if (
            provider == "nominatim"
            and режим == "osm_then_yandex"
            and _хотим_яндекс(статус, row_level)
        ):
            try:
                if await _яндекс_доступен(redis, потолок_яндекса, отказы):
                    hits2 = await yandex_geocoder.search(
                        query,
                        on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                    )
                    статус2, hit2 = geocode.verdict(parsed, city, hits2)
                    ответ_яндекса = (hits2, "yandex")
                    if статус2 == geocode.GEO_EXACT or статус == geocode.GEO_NOT_FOUND:
                        статус, hit, hits, provider = статус2, hit2, hits2, "yandex"
                        ответ_яндекса = None
            except geocode.GeocodeError as exc:
                # Яндекс не ответил — остаёмся с вердиктом OSM, он честный, но
                # набор карт этой строки без Яндекса неполный при ЛЮБОМ отказе
                # (сеть, 5xx, не тот JSON — не только потолок): иначе сбой
                # сети стирал бы прежние улики пересудом (ревью 20.09, C1).
                log.warning("geocode.yandex_fallback_failed", kind=exc.kind, status=exc.status)
                отказы.яндекс = True
        # Опечатка или сокращение: карты дом не нашли — просим подсказку и
        # идём к карте ещё раз с исправленной улицей. Подсказка сама ничего не
        # подтверждает; подтверждает вердикт по ответу карты, с допуском на
        # одну букву в длинном слове (`_основа_совпала`).
        # С 16.09 подсказчиков три, по порядку: Спеллер (опечатка в улице),
        # Ahunter (второй ГАР, без потолка), Геосаджест (тысяча в сутки).
        # Исправленную строку первой проверяет DaData — нормализованный
        # текст ГАР она находит там, где сырой не нашла.
        if статус in _ПОДСКАЗКА_ПОСЛЕ and (
            ahunter_включён or speller_включён or (саджест_включён and yandex_suggest.enabled())
        ):

            async def _dadata_снова(q: geocode.Query) -> list[geocode.GeoHit]:
                try:
                    return await dadata.search(q, on_request=_посчитать_dadata)
                except geocode.GeocodeError as exc:
                    await _учесть_отказ_dadata(redis, exc)
                    raise

            исправлено = await _через_подсказку(
                redis,
                parsed,
                city,
                query,
                режим,
                потолок_яндекса,
                потолок_саджеста,
                саджест_включён=саджест_включён and yandex_suggest.enabled(),
                ahunter_включён=ahunter_включён,
                speller_включён=speller_включён,
                потолок_спеллера=потолок_спеллера,
                dadata_снова=_dadata_снова if dadata_включён and not dadata_недоступна else None,
                отказы=отказы,
            )
            if исправлено is not None:
                статус, hit, hits, provider = исправлено
        # Ответы по городу — DaData первой: её дома по городу при отказе «не
        # тот пункт» до `hits` не доходят (выше берётся ответ следующей карты),
        # а уступке пункта, правилам и строке улицы нужны все.
        по_городу = [(hits, provider)]
        if ответ_dadata is not None and через_dadata is None:
            по_городу.insert(0, (ответ_dadata[2], "dadata"))
        if ответ_яндекса is not None:
            по_городу.append(ответ_яндекса)
        # СПОР ГОРОДА ОБ УЛИЦЕ (ревью N24): дом с номером клиента на улице той же
        # основы, но чужого типа уже найден в городе объявления — за карты города
        # («улица не та» → уступка, область, массив, правила) не ходим, а пригород
        # и «единственный в радиусе» решением не считаем (A8): вариант оператору.
        город_спорит = geocode.city_disputes_street(parsed, city, по_городу)
        if город_спорит:
            log.info(
                "geocode.city_disputes_street",
                candidate_id=str(candidate_id),
                status=статус,
                provider=provider,
                level=row_level,
            )
        # ДРОБЬ ГОЛОВОЙ (правило `fraction_head`, 18.09): «13/38» не нашёлся
        # ни дробью, ни корпусом — спрашиваем дом «13», если он ещё не лежит в
        # ответах по городу (при `house_mismatch` DaData уже отдала его —
        # сеть не нужна, решит правило без похода).
        голова_дроби = geocode.fraction_head(parsed)
        # Хвост дроби похож на квартиру (`flat_from_fraction`), и улица — не с
        # угловой нумерацией (`corner_numbering` по ответам города): только
        # тогда стоит тратить на голову вторую карту и область (20.09). Одно
        # определение с правилом `_fraction_head`.
        хвост_квартира = (
            голова_дроби is not None
            and geocode.flat_from_fraction(голова_дроби[1]) is not None
            and not geocode.corner_numbering(parsed, city, [h for hs, _ in по_городу for h in hs])
        )
        # Гейт похода головой ЗА город (область, А2): голову по городу
        # спрашивали (тот же `_ДРОБЬ_ГОЛОВОЙ_ПОСЛЕ`; при `house_mismatch` её
        # намеренно не спрашивают — дом «N» лежит в ответах или его нет и в
        # десятке подсказок), и ответ головой — из того же набора: на улице
        # клиента в городе домов нет вовсе. Дома на улице есть, а «N» среди них
        # нет (`house_mismatch` головой) — улицу город знает, и дом-голова из
        # пригорода был бы подменой (скептик 20.09, А-3); вторая карта по
        # ТОМУ ЖЕ городу (А1) при этом спрашивается — она спор может только
        # разрешить (Иваново 18.09).
        голова_без_дома = False
        if (
            авто_включён
            and статус in _ДРОБЬ_ГОЛОВОЙ_ПОСЛЕ
            and голова_дроби is not None
            and not any(
                geocode.verdict(dataclasses.replace(parsed, house=голова_дроби[0]), city, hs)[0]
                == geocode.GEO_EXACT
                for hs, _ in по_городу
            )
        ):
            запрос_головы = dataclasses.replace(query, house=голова_дроби[0])
            с_головой = dataclasses.replace(parsed, house=голова_дроби[0])
            try:
                if dadata_включён and not dadata_недоступна:
                    дробь = (
                        await dadata.search(запрос_головы, on_request=_посчитать_dadata),
                        "dadata",
                    )
                elif not dadata_включён:
                    дробь = await _search(redis, режим, запрос_головы, потолок_яндекса, отказы)
                статус_головы = (
                    geocode.verdict(с_головой, city, дробь[0])[0]
                    if дробь is not None
                    else geocode.GEO_NOT_FOUND
                )
                голова_без_дома = статус_головы in _ДРОБЬ_ГОЛОВОЙ_ПОСЛЕ
                if (
                    # ВТОРАЯ КАРТА ГОЛОВОЙ (А1, 20.09): у DaData и OSM дома «N»
                    # нет, у Яндекса он бывает (18.09, Иваново: дом 6 на
                    # Прядильной). Только после тех же отказов, что у второй
                    # карты по городу (`_ВТОРАЯ_КАРТА_ПОСЛЕ`): «неоднозначно» и
                    # «другой город» первой карты Яндекс не перебивает (А-6);
                    # его ответ занимает место только домом «N» под вердиктом —
                    # иначе улика первой карты остаётся. Живой поток: из
                    # потолка Яндекса, реплика старше недели сюда не доходит.
                    дробь is not None
                    and дробь[1] != "yandex"
                    and статус_головы in _ВТОРАЯ_КАРТА_ПОСЛЕ
                    and хвост_квартира
                    and not город_спорит
                    and yandex_geocoder.enabled()
                    and await _яндекс_доступен(redis, потолок_яндекса, отказы)
                ):
                    hits_y = await yandex_geocoder.search(
                        запрос_головы,
                        on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                    )
                    if geocode.verdict(с_головой, city, hits_y)[0] == geocode.GEO_EXACT:
                        дробь, голова_без_дома = (hits_y, "yandex"), False
            except geocode.GeocodeError as exc:
                log.warning(
                    "geocode.fraction_failed",
                    provider=exc.provider,
                    kind=exc.kind,
                    status=exc.status,
                )
                if exc.provider == dadata.PROVIDER:
                    await _учесть_отказ_dadata(redis, exc)
                    dadata_недоступна, dadata_сбой = True, exc.kind
                elif exc.provider == yandex_geocoder.PROVIDER:
                    отказы.яндекс = True  # набор без Яндекса неполный (C1)
        # DaData нашла дом с нужным номером в городе, но не один, а карты ниже
        # точного дома не дали — её вердикт и её дома ценнее: иначе те же дома
        # через поиск по области вернулись бы как «в другом городе» (ревью 12.09).
        # То же под автопривязкой — когда DaData улицу знает, а дома на ней нет
        # или он другой (`house_missing`/`house_mismatch`), а слабые карты
        # ответили пустотой (18.09): «не нашли» карты послабее — не приговор
        # улице, которую справочник видит; строка улицы под `not_found` не
        # пишется никогда (контракт, п. 2), и без этой уступки дома-соседи
        # DaData пропали бы. Выключатель выключен — вердикт как до 18.09.
        # Дом N на другой улице у OSM (`street_mismatch` A/B) — тоже не
        # приговор улице, которую видит справочник (N24); но если это дом с той
        # же основой и чужим типом — спор города, уступки нет: строка идёт
        # текстом (`known_street` по `по_городу`), как сегодня.
        if (
            через_dadata is None
            and ответ_dadata is not None
            and (
                ответ_dadata[0] == geocode.GEO_AMBIGUOUS
                and статус != geocode.GEO_EXACT
                or авто_включён
                and ответ_dadata[0] in _УЛИЦА_У_DADATA
                and _цепочка_продолжается(
                    статус, _УСТУПКА_ПОСЛЕ, row_level, город_спорит=город_спорит
                )
            )
        ):
            статус, hit, hits = ответ_dadata
            provider = "dadata"
        # ВАРИАНТЫ ВМЕСТО «УТОЧНИТЕ ПОСЁЛОК» (просьба владельца 12.09). Карта не
        # выбрала один дом — оператор увидит, что она нашла, и выберет сам.
        варианты = (
            geocode.variants(parsed, city, hits)
            if статус in (geocode.GEO_AMBIGUOUS, geocode.GEO_CITY_MISMATCH)
            else []
        )
        # В ГОРОДЕ ОБЪЯВЛЕНИЯ ДОМА НЕТ — ИЩЕМ ПО ОБЛАСТИ (12.09: объявление в
        # Хабаровске, дом — в Комсомольске-на-Амуре). Только Яндекс и только в
        # потолке: OSM без города отвечает шумом. Находка — не вердикт, а
        # вариант с названным городом: в карточку сам не ложится.
        # Всё, что ответила область, — для уступки пункта без типа ниже
        # (`_пункт_уступает_городу`), правил автопривязки и строки улицы.
        # ⚠ ПОТОЛОК DaData ЗДЕСЬ И НИЖЕ ПРОВЕРЯЕТ САМ ПОХОД, А НЕ ПРЕДПРОВЕРКА
        # (проверка 24.09). Интеграция сначала читает кэш — ответ из него не
        # стоит ни места, ни похода, — и только на промахе зовёт
        # `_посчитать_dadata`, а та за потолком бросает `limit`. Отказ ловит
        # `except` своего шага и ставит «без DaData»: вердикт не окончателен,
        # строку пересмотрят. Предпроверки `_в_пределах` перед шагами молча
        # пропускали шаг без флага — строка оставалась, например, `elsewhere`
        # навсегда, а неделю лежавший в кэше ответ за потолком не читался.
        if (
            not варианты
            and _цепочка_продолжается(
                статус, _ИЩЕМ_ПО_ОБЛАСТИ, row_level, город_спорит=город_спорит
            )
            and city is not None
            and query.city is not None
            and dadata_включён
            and not dadata_недоступна
        ):
            # По области — сначала DaData: она отдаёт дома по всему региону
            # одним запросом и знает города, которых у OSM нет.
            try:
                варианты_рядом: list[dict[str, Any]] = []
                # СНАЧАЛА ОКРУГА ГОРОДА (бой 12.09: по Вологодской области DaData
                # отдаёт Вологду, Бабаево, Ермаково, а клиент — в деревне под
                # Череповцом, которой среди десяти первых нет). Круг 60 км вокруг
                # города объявления ставит его деревни впереди областного центра.
                try:
                    точка_города = await _точка_города(redis, city, _посчитать_dadata)
                except geocode.GeocodeError as exc:
                    dadata_недоступна, dadata_сбой = True, exc.kind
                    точка_города = None
                статус3, hit3 = "", None
                hits_d3: list[geocode.GeoHit] = []
                if точка_города is not None:
                    hits_рядом = await dadata.search(
                        dataclasses.replace(query, city=None),
                        on_request=_посчитать_dadata,
                        near=точка_города,
                        seen=ответы_области,
                    )
                    область_искали, провайдер_области = True, "dadata"
                    статус3, hit3, parsed = _вердикт_с_подсказками(
                        parsed, city, hits_рядом, подсказки
                    )
                    if статус3 == geocode.GEO_EXACT:
                        hits_d3 = hits_рядом
                    else:
                        варианты_рядом = geocode.variants(parsed, city, hits_рядом)
                if статус3 != geocode.GEO_EXACT:
                    hits_d3 = await dadata.search(
                        dataclasses.replace(query, city=None),
                        on_request=_посчитать_dadata,
                        seen=ответы_области,
                    )
                    область_искали, провайдер_области = True, "dadata"
                    статус3, hit3, parsed = _вердикт_с_подсказками(parsed, city, hits_d3, подсказки)
                    if статус3 != geocode.GEO_EXACT and подсказки and not parsed.settlement:
                        # Среди десяти домов области нужного пункта может не
                        # быть (бой 12.09: Вологда, Бабаево, Ермаково — а клиент
                        # в Новом Заозерье). Спрашиваем DaData с пунктом из
                        # реплики, только с ним: без пункта уже искали.
                        for подсказка in подсказки[:HINT_QUERIES]:
                            try:
                                hits_h = await dadata.search(
                                    dataclasses.replace(query, city=None, settlement=подсказка),
                                    on_request=_посчитать_dadata,
                                    without_settlement_too=False,
                                )
                            except geocode.GeocodeError as exc:
                                # Уже найденное по области остаётся; вердикт
                                # без остальных подсказок не окончателен.
                                await _учесть_отказ_dadata(redis, exc)
                                dadata_недоступна, dadata_сбой = True, exc.kind
                                break
                            с_пунктом = dataclasses.replace(parsed, settlement=подсказка)
                            статус_h, hit_h = geocode.verdict(с_пунктом, city, hits_h)
                            if статус_h == geocode.GEO_EXACT:
                                статус3, hit3, hits_d3, parsed = статус_h, hit_h, hits_h, с_пунктом
                                break
                # ГОЛОВА ДРОБИ ПО ОБЛАСТИ (А2, 20.09: «Кола садовая 4/27» при
                # объявлении в Мурманске — дом «4» на Садовой есть в Коле, 9 км).
                # Область на «4/27» и «4 к 27» дроби не нашла — спрашиваем круг
                # 60 км домом «4»; ответ ложится ТОЛЬКО в `ответы_области`
                # (через `seen`) — это улика правил (радиусная ветка
                # `_fraction_head`, прочтение (10)), а не дом клиента. Только
                # когда голову по городу спрашивали и не нашли
                # (`голова_без_дома`), хвост похож на квартиру и улица без
                # угловой нумерации, уровень A/B (вывод из геометрии неводу C
                # не положен), точка города есть (без неё судья молчит — не
                # тратим) и дома-головы в ответах области ещё нет (DaData могла
                # отдать «д 4» соседом сама). Дробь целиком, найденная областью
                # вариантом («4/27» в Североморске), — тоже стоп: дробь выиграла,
                # решать будут правила `elsewhere` и пригород — а при первом
                # слове улицы, прочтённом пунктом (10), только прочтение: без
                # дома в пункте — вариант оператору. Тот же текст «Кола садовая 4»
                # с тем же `near` — общий ключ кэша с любым повтором.
                варианты_области = (
                    []
                    if статус3 == geocode.GEO_EXACT
                    else _слить_варианты(варианты_рядом, geocode.variants(parsed, city, hits_d3))
                )
                if (
                    статус3 != geocode.GEO_EXACT
                    and not варианты_области
                    and голова_без_дома
                    and not город_спорит
                    and голова_дроби is not None
                    and хвост_квартира
                    and not geocode.corner_numbering(parsed, city, ответы_области)
                    and row_level in geocode.STREET_EVIDENCE_LEVELS
                    and точка_города is not None
                    and not geocode.fraction_head_in_hits(parsed, city, ответы_области)
                ):
                    await dadata.search(
                        dataclasses.replace(query, city=None, house=голова_дроби[0]),
                        on_request=_посчитать_dadata,
                        near=точка_города,
                        seen=ответы_области,
                    )
                    область_искали, провайдер_области = True, "dadata"
                if статус3 == geocode.GEO_EXACT:
                    # Клиент назвал пункт, и по области дом в нём нашёлся
                    # («Малиновка, пер Тихий 2») — это адрес, а не вариант.
                    статус, hit, hits, provider = статус3, hit3, hits_d3, "dadata"
                else:
                    варианты = варианты_области
                    улица_в_пункте = geocode.street_in_settlement(parsed, city, ответы_области)
                    if улица_в_пункте is not None and not (
                        parsed.settlement
                        and geocode.variants_name_settlement(parsed.settlement, варианты)
                    ):
                        # УЛИЦА В НАЗВАННОМ ПУНКТЕ ЕСТЬ, ДОМА НЕТ (владелец 16.09,
                        # Псков): дома-тёзки в других пунктах области — не
                        # варианты, а подмена; оператору — точка улицы в пункте
                        # с номером клиента. Под автопривязкой вариант не
                        # нужен: точку улицы даёт правило `street_point`, а
                        # дом соседа — строку улицы без точки; вариант с
                        # точкой соседнего дома запер бы и то и другое
                        # (строка улицы пишется только без вариантов).
                        статус, provider = geocode.GEO_HOUSE_MISSING, "dadata"
                        варианты = (
                            [] if авто_включён else [geocode.street_variant(улица_в_пункте, parsed)]
                        )
                    elif варианты:
                        статус, provider = _статус_вариантов(parsed), "dadata"
            except geocode.GeocodeError as exc:
                log.warning("geocode.dadata_region_failed", kind=exc.kind, status=exc.status)
                # Основной запрос мог прийти из кэша, а 403 — здесь (ревью 13.09).
                await _учесть_отказ_dadata(redis, exc)
                dadata_недоступна, dadata_сбой = True, exc.kind
        if (
            not варианты
            and _цепочка_продолжается(
                статус, _ИЩЕМ_ПО_ОБЛАСТИ, row_level, город_спорит=город_спорит
            )
            and city is not None
            and yandex_geocoder.enabled()
        ):
            try:
                if await _яндекс_доступен(redis, потолок_яндекса, отказы):
                    hits3 = await yandex_geocoder.search(
                        dataclasses.replace(query, city=None),
                        on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                    )
                    область_искали = True
                    провайдер_области = провайдер_области or "yandex"
                    ответы_области += hits3
                    статус3, hit3, parsed = _вердикт_с_подсказками(parsed, city, hits3, подсказки)
                    if статус3 == geocode.GEO_EXACT:
                        статус, hit, hits, provider = статус3, hit3, hits3, "yandex"
                    else:
                        варианты = geocode.variants(parsed, city, hits3)
                        if варианты:
                            статус, provider = _статус_вариантов(parsed), "yandex"
            except geocode.GeocodeError as exc:
                log.warning("geocode.region_fallback_failed", kind=exc.kind, status=exc.status)
                отказы.яндекс = True  # набор без Яндекса неполный (C1)
        elif варианты and статус == geocode.GEO_CITY_MISMATCH:
            # Дом с нужным номером в другом городе области уже был среди ответов.
            статус = geocode.GEO_ELSEWHERE
        # ПРИГОРОД (владелец 18.09): у 49 % строк «в другом месте» единственный
        # найденный дом стоит в 2–19 км от центра города объявления (Реутов →
        # Москва, Мурино → Петербург, Смышляевка → Самара) — и правило 12.09
        # держало его у оператора. Единственный дом области в `SUBURB_KM` от
        # точки города объявления — подтверждён, с пунктом дома. Несколько
        # домов в радиусе — по-прежнему варианты; клиент назвал другой город
        # или пункт — дом обязан быть в нём (`suburb_hit`); точки города нет —
        # правило не применяется. (A8) Спор города об улице — вариант
        # оператору, не точка; лог `geocode.city_disputes_street` выше.
        if (
            статус == geocode.GEO_ELSEWHERE
            and len(варианты) == 1
            and not город_спорит
            and city is not None
            and dadata_включён
            and not dadata_недоступна
        ):
            if точка_города is None:
                try:
                    точка_города = await _точка_города(redis, city, _посчитать_dadata)
                except geocode.GeocodeError as exc:
                    dadata_недоступна, dadata_сбой = True, exc.kind
                    точка_города = None
            # СЛОВО КЛИЕНТА О ПУНКТЕ — РАНЬШЕ ГЕОМЕТРИИ ПРИГОРОДА (ревью 20.09,
            # C1): у «Кола садовая 4/27» единственный «4/27» области стоял в
            # Североморске за 18 км, и пригород забирал его — `_улица_не_шире`
            # прощает клиенту слово «Кола» как лишнее слово улицы. Первое слово
            # улицы похоже на пункт (`street_head_candidate`) — сначала место
            # (10); прочтение установлено — пригород не применяется, судит
            # `_settlement_in_street` (без дома в пункте — вариант оператору,
            # docs/47 §2); места нет — пригород как прежде.
            await _место_по_первому_слову()
            пригород = (
                geocode.suburb_hit(
                    parsed, city, ответы_области if область_искали else hits, точка_города
                )
                if not dadata_недоступна and geocode.street_head_reading(_улики()) is None
                else None
            )
            if пригород is not None and точка_города is not None:
                # Политика правила `suburb` (пакет 6.0а) — та же лестница, что у
                # правил `auto_decide`: `off`/тень — вердикт остаётся `elsewhere`
                # с вариантом, и цепочка идёт дальше; тень записывается следом.
                км_пригорода = geocode.distance_km(точка_города, (пригород.lat, пригород.lon))
                решение_пригорода = geocode.policy_decision(
                    политика, geocode.RULE_SUBURB, пригород, False, parsed, km=км_пригорода
                )
                if решение_пригорода is None:
                    тень_пригорода = geocode.policy_decision(
                        политика,
                        geocode.RULE_SUBURB,
                        пригород,
                        False,
                        parsed,
                        km=км_пригорода,
                        shadow_as=geocode.POLICY_EXACT,
                    )
                    if тень_пригорода is not None:
                        тень = geocode.shadow_of(None, тень_пригорода, city_point=точка_города)
                        log.info(
                            "geocode.suburb_shadow",
                            candidate_id=str(candidate_id),
                            km=round(км_пригорода),
                        )
                else:
                    log.info(
                        "geocode.suburb_accepted",
                        candidate_id=str(candidate_id),
                        provider=provider,
                        km=round(км_пригорода),
                        policy=решение_пригорода.policy,
                    )
                    решение = решение_пригорода
                    статус, hit, hits = geocode.GEO_EXACT, пригород, [пригород]
                    # Предложение (`suggest`) оставляет оператору и вариант.
                    if решение.policy != geocode.POLICY_SUGGEST:
                        варианты = []
        # ПУНКТ БЕЗ ТИПА УСТУПАЕТ ДОМУ В ГОРОДЕ ОБЪЯВЛЕНИЯ (стенд 15.09: «В
        # городе, на Малинниках, ул. Заводская, 17» при объявлении в Калуге —
        # Малинники это район Калуги, карта его в адресе дома не пишет, и
        # строка ушла в «есть в области»). Голое имя перед запятой — слабая
        # улика: пункт с типом («деревня Малиновка») отказ держит, а голое имя
        # уступает — но только когда область спросили и ни один её ответ
        # такого пункта не знает: деревню Малиновку область находит, и до
        # уступки дело не доходит.
        if (
            область_искали
            and статус in (geocode.GEO_SETTLEMENT_MISMATCH, geocode.GEO_ELSEWHERE)
            and _пункт_уступает_городу(parsed, ответы_области)
        ):
            без_пункта = dataclasses.replace(parsed, settlement=None, settlement_type=None)
            for hits_города, provider_города in по_городу:
                статус_г, hit_г = geocode.verdict(без_пункта, city, hits_города)
                if статус_г != geocode.GEO_EXACT:
                    continue
                log.info(
                    "geocode.settlement_yielded",
                    candidate_id=str(candidate_id),
                    provider=provider_города,
                )
                статус, hit, hits, provider, parsed = (
                    статус_г,
                    hit_г,
                    hits_города,
                    provider_города,
                    без_пункта,
                )
                варианты = []
                break
        # ТОЧКА МАССИВА (правило `area_point`, 18.09): участок в СНТ/ДНТ/КП
        # («СНТ Светлый 23») — дома у ФИАС нет, а массив карта знает:
        # спрашиваем место массива, как путь места, но в районах области, не в
        # черте города (массивы живут за городом).
        if (
            авто_включён
            and место_массива is not None
            and _цепочка_продолжается(
                статус, _ТОЧКА_МАССИВА_ПОСЛЕ, row_level, город_спорит=город_спорит
            )
            and not варианты
            and city is not None
            and dadata_включён
            and not dadata_недоступна
        ):
            try:
                place_hits = await dadata.search_place(
                    место_массива,
                    region=city.region,
                    city=место_массива.locality or None,
                    on_request=_посчитать_dadata,
                )
            except geocode.GeocodeError as exc:
                log.warning("geocode.area_failed", kind=exc.kind, status=exc.status)
                await _учесть_отказ_dadata(redis, exc)
                dadata_недоступна, dadata_сбой = True, exc.kind
        # СОСЕД ЗА ЗАБОРОМ (правило `neighbour_settlement`, владелец 20.09):
        # «деревня Липки, вишнёвая 23» при объявлении в Одинцове — дом на
        # улице клиента нашёлся в Дубках, за общим забором, а улицы в Липках
        # карта не знает. Точка названного пункта — один запрос места, и только
        # когда без него правило заведомо не сработает: итог «в другом месте»
        # ПОСЛЕ области (в её ответах — и дом-сосед, и эхо улицы пункта, если
        # оно есть; по городу `seen` не собирается, и там сторож слеп), уровень
        # A/B, кандидат уже лежит в ответах (`neighbour_candidates`). Иначе
        # запрос уходил бы на каждую строку с пунктом впустую.
        место_пункта = geocode.settlement_place(parsed)
        if (
            авто_включён
            and статус == geocode.GEO_ELSEWHERE
            and область_искали
            and bool(ответы_области)
            and not город_спорит
            and city is not None
            and место_пункта is not None
            and row_level in geocode.STREET_EVIDENCE_LEVELS
            and geocode.neighbour_candidates(
                parsed, city, [h for hs, _ in по_городу for h in hs] + ответы_области
            )
            and dadata_включён
            and not dadata_недоступна
        ):
            try:
                пункт_hits = await dadata.search_place(
                    место_пункта, region=city.region, on_request=_посчитать_dadata
                )
            except geocode.GeocodeError as exc:
                log.warning("geocode.settlement_place_failed", kind=exc.kind, status=exc.status)
                await _учесть_отказ_dadata(redis, exc)
                dadata_недоступна, dadata_сбой = True, exc.kind
        # ПУНКТ ИЗ ПЕРВОГО СЛОВА УЛИЦЫ (правило `settlement_in_street`): второй
        # вход — для отказов, до которых пригород не дошёл (`house_missing`,
        # `not_found`, `house_mismatch`, «улица не та» A/B); при `elsewhere` с
        # одним вариантом место уже спрошено выше, до пригорода.
        await _место_по_первому_слову()

    # РЕШИТЬ САМА (владелец 18.09: «адрес привязывается автоматически,
    # оператор ничего не подтверждает»). После всей цепочки один раз зовётся
    # чистое `auto_decide` на собранных уликах; правила — именованные, без
    # признака решения нет. DaData настроена, но на сутки нет — по слабым
    # картам ни решений, ни строки улицы: `ambiguous`/`elsewhere` иначе стали
    # бы окончательными без справочника.
    авто_действует = авто_включён and not (dadata_включён and dadata_недоступна)
    if (
        авто_действует
        and статус == geocode.GEO_ELSEWHERE
        and len(варианты) >= 2
        and точка_города is None
        and city is not None
    ):
        try:
            точка_города = await _точка_города(redis, city, _посчитать_dadata)
        except geocode.GeocodeError as exc:
            dadata_недоступна, dadata_сбой = True, exc.kind
            точка_города = None
    # Прочтение первого слова улицы пунктом установлено картой (10): решает
    # только оно, а без дома в пункте — никто, и строка улицы без точки по
    # сырому разбору не пишется (тот же довод, что у диспетчера `auto_decide`).
    прочтение: tuple[str, str] | None = None
    if авто_действует and решение is None:
        улики = _улики()
        прочтение = geocode.street_head_reading(улики)
        решение = geocode.auto_decide(улики, политика)
        # Тень (§0.3 `shadow`): второй суд с тенью как `exact`; разница — в
        # `trace.shadow` и журнал, без ПД (ключ дома — только в след). Тень
        # пригорода, если была, уступает тени правил: та считана по полным
        # уликам цепочки.
        тень = geocode.shadow_decide(улики, политика) or тень
        if тень is not None:
            log.info(
                "geocode.rule_shadow",
                candidate_id=str(candidate_id),
                rule=тень.rule,
                status=тень.status,
                instead=тень.instead,
                km=round(тень.km, 2) if тень.km is not None else None,
            )
        if решение is not None:
            log.info(
                "geocode.auto_decided",
                candidate_id=str(candidate_id),
                rule=решение.rule,
                policy=решение.policy,
                provider=решение.provider or provider,
                was=статус,
                approx=решение.approx,
                # Расстояние, которым решило правило (сосед за забором: дом →
                # точка названного пункта; голова по области и прочтение (10):
                # дом → точка города объявления) — по нему копится замер радиуса.
                rule_km=round(решение.km, 2) if решение.km is not None else None,
                # Пункт, который правило прочло из слов клиента (подсказка из
                # соседней реплики или первое слово улицы) — строка его не
                # хранит, прочтение видно только здесь и в `geo_formatted`.
                settlement_read=(
                    решение.parsed.settlement
                    if решение.parsed.settlement != parsed.settlement
                    else None
                ),
                km=(
                    [
                        round(geocode.distance_km(точка_города, (v["lat"], v["lon"])))
                        for v in варианты
                    ]
                    if точка_города is not None
                    else None
                ),
            )
            статус, hit, hits, варианты, parsed = (
                geocode.GEO_EXACT,
                решение.hit,
                [решение.hit],
                # Предложение (`suggest`) оставляет оператору и варианты: он
                # принимает решение правила одним нажатием или выбирает другой.
                варианты if решение.policy == geocode.POLICY_SUGGEST else [],
                решение.parsed,
            )
            provider = решение.provider or provider
        elif прочтение is not None:
            log.info(
                "geocode.street_head_unresolved",
                candidate_id=str(candidate_id),
                status=статус,
                settlement=прочтение[0],
            )
    # ТОЧКА ДОМА — У ВТОРОЙ КАРТЫ (18.09: «Лучистая 7к3» в Сертолове —
    # DaData знает дом, а точку ставит в центр микрорайона, за полтора
    # километра; Яндекс тот же дом отдаёт точно). Дом с приблизительной
    # точкой спрашиваем у Яндекса по компонентам найденного дома; тот же
    # дом с точной точкой — берём его координаты, иначе строка помечается
    # «точка приблизительная», а вердикт остаётся.
    if статус == geocode.GEO_EXACT and hit is not None and not hit.precise:
        hit, provider = await _уточнить_точку(
            redis, hit, city, provider, потолок_яндекса=потолок_яндекса, отказы=отказы
        )
    if решение is not None and решение.approx:
        # Выбор среди нескольких, дом «N» для «N/M», семья дома, город от
        # карты — «приблизительно» независимо от точности самой точки.
        provider = geocode.mark_approx(provider)
    if решение is not None and решение.policy == geocode.POLICY_SUGGEST:
        # Политика `suggest` (§0.3): решение лежит в строке, в карточку не идёт
        # (`card_grade` → None по хвосту), автозапись не ставится, оператор
        # принимает одним нажатием — принятие снимает хвост.
        provider = geocode.mark_suggest(provider)
    # СТРОКА УЛИЦЫ БЕЗ ТОЧКИ (степень `text`, владелец 18.09: «адрес без
    # точки допустим, но только когда карта знает улицу клиента в месте
    # клиента»). Отказ по содержанию без вариантов, уровня A/B, и хоть один
    # ответ по городу или области знает улицу (`known_street`, STREET_KNOWN):
    # в `geo_formatted` — строка улицы с номером клиента, координаты NULL.
    # `not_found` — не отказ по содержанию, а пустота: под ним строки улицы
    # нет никогда (контракт, п. 2 — это вопрос клиенту); улица, которую знает
    # DaData при пустых слабых картах, приходит сюда её вердиктом (выше).
    if (
        авто_действует
        and решение is None
        and прочтение is None
        and статус in _ТЕКСТ_ПОСЛЕ
        and not варианты
        and city is not None
        and row_level in geocode.STREET_EVIDENCE_LEVELS
    ):
        улица = geocode.known_street(
            parsed, city, [h for hs, _ in по_городу for h in hs] + ответы_области
        )
        if улица is not None:
            текст_улицы = geocode.street_variant(улица, parsed)["formatted"]
            log.info("geocode.street_text", candidate_id=str(candidate_id), status=статус)
    log.info(
        "geocode.done",
        candidate_id=str(candidate_id),
        provider=provider,
        status=статус,
        hits=len(hits),
        ms=round((time.monotonic() - начало) * 1000),
    )

    # БЕЗ DADATA ОТКАЗ — НЕ ПРИГОВОР (бой 13.09): OSM не нашёл, Яндекс за
    # потолком — строка остаётся `pending` с именем отказавшей карты, а
    # починка перепроверит её, когда DaData вернётся. Попытка не считается,
    # когда недоступность плановая (потолок, 403 на сутки); сеть и не тот JSON
    # — считается: пауза починки растёт, залп в лежащую DaData не повторяется.
    # Строка починки откладывается в любом возрасте: доля выбрана посреди
    # порции — остаток порции не должен получить приговор OSM (ревью 13.09).
    отложено = (
        dadata_недоступна
        and статус in _БЕЗ_DADATA_НЕ_ПРИГОВОР
        and (свежая or origin == ORIGIN_REPAIR)
    )
    if отложено:
        log.info(
            "geocode.deferred",
            candidate_id=str(candidate_id),
            provider=provider,
            verdict=статус,
            reason=f"dadata_{dadata_сбой or 'unavailable'}",
        )
        статус, hit, варианты, текст_улицы = geocode.GEO_PENDING, None, [], None
    # Страна упёрлась в потолок DaData — попытка не тратится, как у отказа:
    # строка остаётся `no_city` с прежним числом попыток.
    считать_попытку = (
        not отложено or dadata_сбой not in ("blocked", "limit")
    ) and not страна_отложена
    # ПОПЫТКА — ТОЛЬКО ТАМ, ГДЕ ОНА ЧТО-ТО СДЕРЖИВАЕТ (ревью 19.09, C2).
    # Счётчик попыток читают пауза починки (`_пора`) и её потолок — оба у
    # строки в CHECKING. Вердикт из RECHECK_STATUSES, вынесенный без DaData
    # из-за сетевого сбоя (сеть, 5xx, не тот JSON), ложится окончательно с
    # флагом `geo_without_dadata` и вернётся обходом `dadata_back`, который
    # сам обнуляет `geo_checked_at`: паузы у такой строки нет, и попытка
    # лишь приближала бы потолок, за которым строка выпадает из обхода — и из
    # пересмотра новым судьёй — навсегда (в бою: час лежащей DaData). Отложенный
    # по сети отказ (`pending`) попытку тратит по-прежнему: он остаётся в
    # CHECKING, и пауза починки на нём работает. Один источник истины — вид
    # сбоя и статус записи; отдельного признака «пришла из обхода» нет: первая
    # проверка и повторная судятся одним правилом.
    сетевой_сбой_dadata = dadata_недоступна and dadata_сбой not in ("blocked", "limit")
    if not отложено and сетевой_сбой_dadata and статус in geocode.RECHECK_STATUSES:
        считать_попытку = False

    # Фаза 3 — условная запись и кадр. Строка карты — дом при вердикте,
    # иначе строка улицы без точки (текст), иначе ничего. Факт «без DaData»
    # считается ПОСЛЕ всех ветвей, выставляющих `dadata_недоступна`.
    без_dadata = not dadata_включён or dadata_недоступна
    formatted = geocode.format_address(hit, parsed) if hit is not None else текст_улицы
    # МОНОТОННОСТЬ ПЕРЕСУДА (пакет 5): новый вердикт слабее прежних улик и
    # часть карт этой строке отказала — удерживаются прежние; снимок остаётся
    # у слепого суда любого исхода, полный суд его стирает.
    новое = _Вердикт(статус, formatted, provider, hit, варианты, без_dadata)
    недостаёт = отказы.недостаёт(dadata=dadata_включён and dadata_недоступна)
    вердикт, prev = _с_учётом_прежнего(
        candidate_id,
        новое,
        прежнее,
        kind=address_parse.KIND_HOUSE,
        недостаёт=недостаёт,
        содержание=содержание,
        origin=origin,
    )
    # УДЕРЖАНО ПРЕЖНЕЕ — решение этого суда отвергнуто целиком (ревью 21.09,
    # #7): ни его правило, ни квартира из хвоста дроби к удержанному ключу не
    # относятся. Правило удержанного ключа — в восстановленном следе.
    удержано = вердикт is not новое
    решено = None if удержано else решение
    # СЛЕД СУДА (пакет 6.0а, I-1): удержан прежний вердикт — возвращается и его
    # след (суд, ушедший в `prev` при сбросе); иначе — новый след поверх
    # прежнего, ключи разбора остаются.
    след = (
        geocode.trace_restored(след_строки)
        if удержано
        else geocode.verdict_trace(
            след_строки,
            rule=решение.rule if решение is not None else None,
            policy=решение.policy if решение is not None else None,
            km=решение.km if решение is not None else None,
            hit=hit,
            query_form=geocode.QUERY_FORM_COUNTRY if страна else geocode.QUERY_FORM_HOUSE,
            settlement_read=(
                решение.parsed.settlement
                if решение is not None and решение.parsed.settlement != снимок.settlement
                else None
            ),
            shadow=тень,
        )
    )
    статус, formatted, provider, hit, варианты = (
        вердикт.статус,
        вердикт.formatted,
        вердикт.provider,
        вердикт.hit,
        вердикт.варианты,
    )
    # Степень карточки — по тем же полям, что уходят в строку: в бою по ней
    # ставится автозапись, в сухом суде — считается `would_autofill`.
    степень = geocode.card_grade(
        address_parse.KIND_HOUSE,
        статус,
        provider,
        hit.lat if hit is not None else None,
        hit.lon if hit is not None else None,
        formatted,
    )
    # Правило, политика, км копилки — того суда, чей ключ лежит в итоге: при
    # удержании — из восстановленного следа (его может и не быть: строка без
    # следа → None), иначе — из решения.
    след_суда = след or {}
    _сложить_в_копилку(
        DryVerdict(
            status=статус,
            formatted=formatted,
            provider=provider,
            hit_city=(hit.city or hit.settlement) if hit is not None else None,
            km=след_суда.get("km") if удержано else (решено.km if решено is not None else None),
            rule=след_суда.get("rule")
            if удержано
            else (решено.rule if решено is not None else None),
            policy=(
                след_суда.get("policy")
                if удержано
                else (решено.policy if решено is not None else None)
            ),
            shadow=тень,
            trace=след,
            variants_n=len(варианты),
            office=решено.office if решено is not None else None,
            would_autofill=степень is not None and not отложено,
            kept=удержано,
            missing=tuple(недостаёт),
        )
    )
    if dry_run:
        # Сухой суд кончается здесь: ни записи, ни повтора, ни кадра, ни задач.
        return "deferred" if отложено else статус
    записали = await _пометить(
        factory,
        candidate_id,
        статус,
        formatted,
        provider,
        hit,
        снимок,
        variants=варианты,
        считать_попытку=считать_попытку,
        # Живой путь — как до 6.0б: квартира из хвоста дроби пишется и при
        # удержании прежнего вердикта (проверка правок 21.09: пакет про сухой
        # прогон бой не меняет; в копилке при удержании office пуст).
        office=решение.office if решение is not None else None,
        without_dadata=без_dadata,
        prev=prev,
        trace=след,
    )
    if not записали:
        # Строка изменилась под нами (посёлок дописан) или уже решена — наш
        # вердикт устарел. Своё имя задачи ещё занято до конца выполнения, а
        # ключ уникальности строки гасит повтор, поэтому повтор ставится под
        # именем-хвостом; не встал — доберёт починка (строка осталась pending).
        log.info("geocode.stale", candidate_id=str(candidate_id))
        await enqueue_geocode(redis, candidate_id, defer_sec=3, suffix="again", origin=origin)
        return "stale"
    if отложено:
        return "deferred"
    await _известить(redis, client_id, conversation_id, reason="address_geocoded")
    # АВТОЗАПИСЬ — ПРИ ЛЮБОЙ СТЕПЕНИ (18.09): точка дома, приблизительная точка
    # или текст без точки (`geo_formatted` при окончательном отказе, когда
    # улица клиента известна карте). Годна ли строка и кому уступить, решает
    # задача через тот же `candidate_grade`; здесь — только постановка по тем
    # же полям, что ушли в строку (степень посчитана выше, до записи). `job_id`
    # addr-fill:{conversation_id} дедуплицирует по диалогу.
    if степень is not None:
        await enqueue_autofill(redis, conversation_id)
    # Модель перечитывает реплику независимо от постановки: успела до прогона
    # автозаписи — обе строки в одном прогоне, решает «та же реплика, выше по
    # степени»; позже — лестница уступок заменит текст точным домом.
    if (
        статус in _ПЕРЕЧИТАТЬ_МОДЕЛЬЮ
        and row_для_модели
        and row_source != CANDIDATE_SOURCE_LLM
        and row_message_id is not None
        and openrouter.enabled()
    ):
        # Карта отказала — может, правила прочитали не то («11 улица
        # Озёрная» из «часов 11 улица…»): модель перечитает переписку и
        # предложит свой разбор, который пройдёт тот же путь (владелец 13.09).
        await enqueue_llm_read(
            redis,
            conversation_id=conversation_id,
            message_id=row_message_id,
            candidate_id=candidate_id,
        )
    if авто_включён:
        # Вопрос клиенту о городе — только замер: `ask_reason` пока читает один
        # воркер, живой вопрос о городе (N25) не собран; `address_ask` спрашивает
        # адрес целиком по своим замкам. Здесь — строка журнала, по которой
        # считается объём.
        причина = geocode.ask_reason(
            geo_status=статус, city_known=city_known, locality=parsed.locality, variants=варианты
        )
        if причина is not None:
            log.info(
                "geocode.ask_client",
                candidate_id=str(candidate_id),
                conversation_id=str(conversation_id),
                reason=причина,
            )
    return статус


def _место_массива(row: ClientAddressCandidate, место: geocode.Place) -> geocode.Place | None:
    """Место для точки массива у строки-дома: пункт с типом массива с
    участками («СНТ Светлый») — массивом с каноническим типом и без пункта
    (порядок `place_verdict` `{area: 0}` требует `kind == "area"`); массив
    внутри пункта («д. Ивняково, СНТ Рассвет») — как есть, без улицы; иначе
    None — точку массива искать не по чему."""
    тип = geocode._ТИПЫ_МАССИВА_С_УЧАСТКАМИ.get(geocode._норм(row.settlement_type or ""))
    if тип and row.settlement:
        return geocode.Place(
            settlement=None,
            settlement_type=None,
            area=f"{тип} {row.settlement.strip()}",
            district=row.district,
            level=row.level,
            street="",
            locality=row.locality,
        )
    if row.area:
        return dataclasses.replace(место, street="")
    return None


async def _уточнить_точку(
    redis: Redis,
    hit: geocode.GeoHit,
    city: Any,
    provider: str,
    *,
    потолок_яндекса: int | None,
    отказы: _Отказы | None = None,
) -> tuple[geocode.GeoHit, str]:
    """Точка того же дома у Яндекса — или пометка «точка приблизительная».

    Запрос — из компонентов найденного дома (город, пункт, улица, дом), не
    из слов клиента: у DaData уже полное имя улицы и пункт. Яндекс не
    настроен, за потолком или не ответил — вердикт не меняется, меняется
    только имя провайдера: экран покажет, что точка не дома.
    """
    if city is not None and yandex_geocoder.enabled():
        # Микрорайон DaData отдаёт и пунктом, и «улицей» разом («Кедровка» +
        # «мкр Кедровка», Чита, владелец 19.09): в запросе имя удвоилось бы —
        # «Чита, Кедровка, микрорайон Кедровка 7» — и Яндекс отдавал район
        # без дома. Пункт, чьи слова уже есть в улице, в запрос не идёт.
        пункт = hit.settlement
        if пункт and geocode.settlement_inside_street(пункт, hit.street or ""):
            пункт = None
        уточняющий = geocode.Query(
            region=city.region,
            city=hit.city,
            settlement=пункт,
            street=hit.street or "",
            house=hit.house or "",
        )
        try:
            if await _яндекс_доступен(redis, потолок_яндекса, отказы):
                hits_y = await yandex_geocoder.search(
                    уточняющий,
                    on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                )
                точный = geocode.precise_point(hit, city.region, hits_y)
                if точный is not None:
                    log.info("geocode.point_refined", provider=provider)
                    return точный, f"{provider}+yandex"
        except geocode.GeocodeError as exc:
            log.warning("geocode.point_refine_failed", kind=exc.kind, status=exc.status)
            if отказы is not None:
                отказы.яндекс = True  # набор без Яндекса неполный (C1)
    return hit, geocode.mark_approx(provider)


#: Отказы карты, после которых стоит перечитать реплику моделью: улица есть,
#: дома нет; улица/пункт/город не совпали; не найдено. `ambiguous`/`elsewhere`
#: — разбор верен, карта не уверена; модель тут не поможет. «Клиент назвал
#: другой город» (замер 18.09) — разбор мог взять городом не то слово речи.
_ПЕРЕЧИТАТЬ_МОДЕЛЬЮ = frozenset(
    {
        geocode.GEO_STREET_MISMATCH,
        geocode.GEO_HOUSE_MISSING,
        geocode.GEO_HOUSE_MISMATCH,
        geocode.GEO_NOT_FOUND,
        geocode.GEO_SETTLEMENT_MISMATCH,
        geocode.GEO_CITY_MISMATCH,
        geocode.GEO_OTHER_CITY,
    }
)


def _статус_вариантов(parsed: geocode.Parsed) -> str:
    """Дом нашёлся не там, где искали: клиент назвал город — «клиент назвал
    другой город» с вариантами (замер 18.09: без них строка была тупиком),
    иначе — «есть в области, проверьте вариант»."""
    return geocode.GEO_OTHER_CITY if parsed.locality else geocode.GEO_ELSEWHERE


@dataclass(frozen=True, slots=True)
class _Снимок:
    settlement: str | None
    locality: str | None


@dataclass(slots=True)
class _Отказы:
    """Карты, отказавшие ЭТОЙ строке по потолку или доле (пакет 5): «набор
    карт неполный» для удержания прежних улик считается по факту походов, а не
    по общему счётчику в конце суда (ревью 20.09, п. 5). DaData ведёт свой
    признак — `dadata_недоступна`."""

    яндекс: bool = False
    саджест: bool = False

    def недостаёт(self, *, dadata: bool) -> list[str]:
        """Имена карт, без которых вынесен вердикт, — в снимок и журнал."""
        return [
            имя
            for имя, нет in (("dadata", dadata), ("yandex", self.яндекс), ("suggest", self.саджест))
            if нет
        ]


def _место(row: ClientAddressCandidate) -> tuple[float, float] | None:
    """Точка дома для памяти об отказе; приблизительная точка (`~approx`) —
    это точка деревни или массива, и отказ по ней запирал бы автозапись
    всех домов пункта (ревью 18.09) — у такой строки места нет."""
    if row.geo_lat is None or row.geo_lon is None or geocode.point_is_approx(row.geo_provider):
        return None
    return (round(row.geo_lat, 4), round(row.geo_lon, 4))


def _aware(dt: datetime) -> datetime:
    """SQLite в проверках отдаёт наивное время; в бою — только с поясом."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _search(
    redis: Redis,
    режим: str,
    query: geocode.Query,
    потолок_яндекса: int | None,
    отказы: _Отказы | None = None,
) -> tuple[list[geocode.GeoHit], str]:
    """Ответы карты и имя провайдера, который их дал.

    `yandex` — Яндекс первым; потолок выбран или ключа нет — OSM, чтобы строки
    не стояли. `nominatim` и `osm_then_yandex` — OSM первым (вторым Яндекс
    спрашивает вызывающий по вердикту).
    """
    if (
        режим == "yandex"
        and yandex_geocoder.enabled()
        and await _яндекс_доступен(redis, потолок_яндекса, отказы)
    ):
        try:
            hits = await yandex_geocoder.search(
                query, on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы)
            )
            return hits, "yandex"
        except geocode.GeocodeError as exc:
            # Потолок выбрали между проверкой и занятием (пятьдесят задач
            # разом) — это не отказ карты, строка идёт в OSM.
            if exc.kind != "limit":
                raise

    async def темп() -> None:
        await _ждать_темпа(redis)

    # Замок берётся перед КАЖДЫМ HTTP-запросом поиска, а не один раз на поиск.
    return await nominatim.search(query, wait=темп), "nominatim"


#: Вердикты, поверх которых задача имеет право писать новый. «Город
#: неизвестен» — среди них (12.09, Чита): обогащение приносит объявление
#: мгновением позже распознавания и ставит задачу заново, а та выходила по
#: «уже решено», не дойдя до карты.
_ПЕРЕПРОВЕРЯЕМЫЕ: frozenset[str | None] = geocode.CHECKING_STATUSES
#: После каких вердиктов OSM стоит спросить Яндекс: дома нет вовсе, только
#: улица — или улица есть с другими домами (владелец 18.09, Иваново: «ул.
#: Прядильная д6» — у DaData и OSM на улице дома есть, шестого нет, Яндекс
#: отдаёт дом 6 точкой; до 18.09 `house_mismatch` был тупиком цепочки).
#: «Улица не та» у уровней A/B идёт дальше через `_хотим_яндекс` (N24, замер
#: 19.09) — дом N на другой улице у OSM значит лишь «на улице клиента дома N у
#: этой карты нет»; спор о городе второй картой по-прежнему не решается.
_ВТОРАЯ_КАРТА_ПОСЛЕ = frozenset(
    {geocode.GEO_NOT_FOUND, geocode.GEO_HOUSE_MISSING, geocode.GEO_HOUSE_MISMATCH}
)
#: После этих вердиктов зовём подсказчиков: улица с опечаткой у карты — и
#: «не нашли», и «улица не та» (стенд 30 дней: сотни `street_mismatch` на
#: «Ленена», «Звенигародская»). `house_mismatch` сюда не входит: улица у
#: карты уже есть, подсказчики чинят улицу, а не номер.
_ПОДСКАЗКА_ПОСЛЕ = frozenset(
    {geocode.GEO_NOT_FOUND, geocode.GEO_HOUSE_MISSING, geocode.GEO_STREET_MISMATCH}
)
#: Дробь «N/M» спрашивается домом «N» только когда дома нет вовсе: при
#: `house_mismatch` дом «N» уже лежит в ответах, и правило решит без сети.
#: Тот же набор — гейт головы по ОБЛАСТИ (А2, 20.09) и по статусу города, и по
#: вердикту головой: где голову по городу не спрашивали или улицу с домами
#: город знает, за город с головой не ходят. Вторая карта головой по тому же
#: городу (А1) идёт после `_ВТОРАЯ_КАРТА_ПОСЛЕ`, как и дробью.
_ДРОБЬ_ГОЛОВОЙ_ПОСЛЕ = frozenset({geocode.GEO_NOT_FOUND, geocode.GEO_HOUSE_MISSING})
#: После этих отказов у участка массива ищется точка массива. «Улица не та» у
#: уровней A/B — через `_цепочка_продолжается` (N24), кроме спора города об
#: улице (`geocode.city_disputes_street`).
_ТОЧКА_МАССИВА_ПОСЛЕ = frozenset(
    {geocode.GEO_HOUSE_MISSING, geocode.GEO_NOT_FOUND, geocode.GEO_HOUSE_MISMATCH}
)
#: Уровни, которым отказ со строкой улицы идёт в карточку текстом, — те же,
#: что у точки улицы и массива в правилах: `geocode.STREET_EVIDENCE_LEVELS`
#: (A и B; невод C — никогда). Одна константа на текст и точку — своей у
#: воркера нет намеренно (ревью 19.09).
#: После каких отказов пишется строка улицы без точки: отказы по содержанию
#: без `not_found` — пустота карты в карточку не идёт никогда (контракт 18.09,
#: п. 2: это вопрос клиенту, участок вопроса).
_ТЕКСТ_ПОСЛЕ = geocode.REFUSAL_STATUSES - {geocode.GEO_NOT_FOUND}
#: Отказы DaData, при которых её улица известна, а дома нет или он другой: такой
#: ответ ценнее пустого `not_found` карт послабее (уступка в цепочке, 18.09).
_УЛИЦА_У_DADATA = frozenset({geocode.GEO_HOUSE_MISSING, geocode.GEO_HOUSE_MISMATCH})
#: После какого вердикта слабой карты её место занимает улица DaData; «улица
#: не та» A/B без спора города — через `_цепочка_продолжается` (N24).
_УСТУПКА_ПОСЛЕ = frozenset({geocode.GEO_NOT_FOUND})
#: Ahunter не ответил (сеть) — не стучим в него столько минут: сервер один.
AHUNTER_DOWN_SEC = 10 * 60
#: После этих вердиктов дом ищется по всей области объявления — вариантом.
#: Координаты города объявления живут в Redis месяц: города не переезжают,
#: а запрос к DaData — из того же дневного потолка.
CITY_POINT_TTL_SEC = 30 * 24 * 3600
CITY_POINT_MISS_TTL_SEC = 24 * 3600


def city_point_key(city: Any) -> str:
    return f"geo:dadata:city:{hashlib.sha1(f'{city.region}|{city.name}'.encode()).hexdigest()[:16]}"


async def _точка_города(redis: Redis, city: Any, посчитать: Any) -> tuple[float, float] | None:
    """Координаты города объявления из кэша или от DaData; None — не знаем.
    Потолок проверяет `посчитать` на промахе кэша — за ним бросается `limit`."""
    ключ = city_point_key(city)
    try:
        кэш = await redis.get(ключ)
    except Exception as exc:  # noqa: BLE001 — кэш не обязан работать
        log.warning("geocode.city_point_cache_failed", error=type(exc).__name__)
        кэш = None
    if кэш is not None:
        текст = кэш.decode() if isinstance(кэш, bytes) else str(кэш)
        if текст == "-":
            return None
        lat, lon = текст.split(",")
        return float(lat), float(lon)
    try:
        точка = await dadata.city_point(city.name, city.region, on_request=посчитать)
    except geocode.GeocodeError as exc:
        log.warning("geocode.city_point_failed", kind=exc.kind, status=exc.status)
        if exc.kind in ("limit", "blocked"):
            # Потолок или бан — шагу, который спрашивал точку: без неё круг
            # вокруг города и выбор среди вариантов не окончательны («без
            # DaData», проверка 24.09). Сбой сети по-прежнему значит «точки
            # нет» — шаг идёт без неё.
            await _учесть_отказ_dadata(redis, exc)
            raise
        return None
    try:
        if точка is None:
            await redis.set(ключ, "-", ex=CITY_POINT_MISS_TTL_SEC)
        else:
            await redis.set(ключ, f"{точка[0]},{точка[1]}", ex=CITY_POINT_TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.city_point_cache_failed", error=type(exc).__name__)
    return точка


def _слить_варианты(*списки: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Варианты рядом с городом — впереди областных; один адрес — один раз."""
    out: list[dict[str, Any]] = []
    видели: set[str] = set()
    for список in списки:
        for v in список:
            if v["formatted"] in видели:
                continue
            видели.add(v["formatted"])
            out.append(v)
    return out[: geocode.MAX_VARIANTS]


#: Окно, в котором реплика клиента считается соседней с адресом.
HINT_WINDOW = timedelta(hours=24)
HINT_MESSAGES = 8
#: Сколько подсказок проверяется отдельным запросом к DaData.
HINT_QUERIES = 2


async def _соседние_реплики(db: Any, row: ClientAddressCandidate) -> list[str | None]:
    """Речь входящих того же диалога до сообщения с адресом — свежие первыми;
    первым — само сообщение с адресом («Пашковка, Ковыльная 214/2»). Речь, а не
    тело (`voice.speech_sql`, 19.09): у строки от голосового «своя реплика» по
    телу была бы `None`, а соседние голосовые невидимы — подсказка пункта карте
    молчала бы ровно там, где карта обещана сразу."""
    до = row.message_at or row.detected_at
    соседи = list(
        (
            await db.execute(
                sa.select(voice.speech_sql())
                .where(
                    Message.conversation_id == row.conversation_id,
                    Message.direction == "in",
                    Message.created_at < до,
                    Message.created_at >= до - HINT_WINDOW,
                )
                .order_by(Message.created_at.desc())
                .limit(HINT_MESSAGES)
            )
        ).scalars()
    )
    своё = (
        (
            await db.execute(sa.select(voice.speech_sql()).where(Message.id == row.message_id))
        ).scalar()
        if row.message_id is not None
        else None
    )
    return [своё, *соседи]


async def _город_клиента_по_другим_диалогам(
    db: AsyncSession, row: ClientAddressCandidate, имя: str
) -> bool:
    """Названный клиентом город — город объявления другого его диалога."""
    города = await other_conversation_cities(db, row.client_id, row.conversation_id)
    return any(c.name == имя for c in города)


def _пункт_уступает_городу(parsed: geocode.Parsed, ответы_области: list[geocode.GeoHit]) -> bool:
    """Пункт назван голым именем, и область его не знает — дом в городе
    объявления годится. Имя с типом, имя города из справочника («Обнинск,
    ул. Ленина 12» при объявлении в Калуге — ревью 15.09) или имя, которое
    хоть один ответ области называет (пусть улицей, без дома), уступать не
    должно: клиент назвал настоящий пункт, и дом там просто не подтвердился."""
    if not parsed.settlement or parsed.settlement_type:
        return False
    if city_by_name(parsed.settlement) is not None:
        return False
    return not geocode.settlement_seen(parsed.settlement, ответы_области)


def _вердикт_с_подсказками(
    parsed: geocode.Parsed, city: Any, hits: list[geocode.GeoHit], подсказки: list[str]
) -> tuple[str, geocode.GeoHit | None, geocode.Parsed]:
    """Вердикт по домам области; пункт из соседней реплики — если с ним дом
    подтверждается. Возвращает и разбор: с подтверждённым пунктом адрес
    печатается «…, Новое Заозерье», а не голым «Рябиновая, 8»."""
    статус, hit = geocode.verdict(parsed, city, hits)
    if статус == geocode.GEO_EXACT or parsed.settlement:
        return статус, hit, parsed
    for подсказка in подсказки:
        с_пунктом = dataclasses.replace(parsed, settlement=подсказка)
        статус_п, hit_п = geocode.verdict(с_пунктом, city, hits)
        if статус_п == geocode.GEO_EXACT:
            return статус_п, hit_п, с_пунктом
    return статус, hit, parsed


#: После этих вердиктов дом ищется по всей области: его нет в городе
#: объявления — или клиент назвал пункт, которого в городе нет («Малиновка,
#: пер Тихий 2» при объявлении в Костроме → `settlement_mismatch`), или
#: город, в котором карта послабее дома не нашла («г Цимлянск» → OSM отдал
#: дом-тёзку в Волгодонске, замер 18.09) — DaData по области найдёт его там.
#: `house_mismatch` — с 18.09 (замер: «хутор Весёлый, Степная 2» при
#: объявлении в Шахтах — дом искался только в городе, где такой улицы с таким
#: номером нет, а в хуторе за 15 км он есть; тупик цепочки). «Улица не та» у
#: уровней A/B идёт дальше через `_цепочка_продолжается` (N24, замер 19.09),
#: кроме спора города об улице (`geocode.city_disputes_street`); спор о городе
#: второй картой по-прежнему не решается.
_ИЩЕМ_ПО_ОБЛАСТИ = frozenset(
    {
        geocode.GEO_NOT_FOUND,
        geocode.GEO_HOUSE_MISSING,
        geocode.GEO_HOUSE_MISMATCH,
        geocode.GEO_CITY_MISMATCH,
        geocode.GEO_SETTLEMENT_MISMATCH,
        geocode.GEO_OTHER_CITY,
    }
)


async def _через_подсказку(
    redis: Redis,
    parsed: geocode.Parsed,
    city: Any,
    query: geocode.Query,
    режим: str,
    потолок_яндекса: int | None,
    потолок_саджеста: int | None,
    *,
    саджест_включён: bool = True,
    ahunter_включён: bool = False,
    speller_включён: bool = False,
    потолок_спеллера: int | None = None,
    dadata_снова: Callable[[geocode.Query], Awaitable[list[geocode.GeoHit]]] | None = None,
    отказы: _Отказы | None = None,
) -> tuple[str, geocode.GeoHit | None, list[geocode.GeoHit], str] | None:
    """Подсказчики → исправленная улица/дом → карта → вердикт. `None` — не помогло.

    Порядок — от дешёвого и честного к дорогому: Спеллер (только опечатка в
    словах улицы, без ключа), Ahunter (второй ГАР, без потолка), Геосаджест
    (тысяча в сутки, чужая лицензия). Каждый кандидат сначала идёт к DaData
    (если она есть и в потолке): нормализованный текст ГАР она находит там, где
    сырой не нашла, — потом к картам режима.

    ⚠ ПОДСКАЗКА НЕ ДОБАВЛЯЕТ ПУНКТ, КОТОРОГО КЛИЕНТ НЕ НАЗЫВАЛ: «Ленина 5» в
    Костроме у Ahunter первой строкой — «д Малиновка, ул Ленина, дом 5», и дом в
    деревне подтвердил бы чужой адрес. Берём только подсказки того же города и
    того же пункта (или без пункта, когда клиент его не называл). Вердикт
    считается по исходному разбору с одной поправкой — улицей подсказки.
    """

    # Подсказчики — ЛЕНИВО, по одному: следующий спрашивается, только если
    # кандидаты предыдущего не подтвердились (ревью 16.09: иначе тысяча
    # Геосаджеста тратилась и там, где хватило Спеллера).
    async def спеллер() -> list[geocode.Query]:
        if not speller_включён or not await _в_пределах(
            redis, speller_calls_key(), потолок_спеллера
        ):
            return []
        try:
            улица = await speller.fix_street(
                query.street,
                on_request=lambda: _занять(redis, speller_calls_key(), потолок_спеллера, "speller"),
            )
        except geocode.GeocodeError as exc:
            log.warning("geocode.speller_failed", kind=exc.kind, status=exc.status)
            return []
        if not улица or улица == query.street:
            return []
        log.info("geocode.speller_fixed", was=query.street, now=улица)
        return [dataclasses.replace(query, street=улица)]

    async def ahunter_() -> list[geocode.Query]:
        if not ahunter_включён or await _ahunter_лежит(redis):
            return []
        try:
            подсказки_ahunter = await ahunter.suggest(
                query, on_request=lambda: _посчитать(redis, ahunter_calls_key())
            )
        except geocode.GeocodeError as exc:
            # Любой отказ — сеть, 403, HTML вместо JSON — кладёт Ahunter на
            # десять минут: сервер один, и стучать в лежащий строкой за
            # строкой незачем.
            log.warning("geocode.ahunter_failed", kind=exc.kind, status=exc.status)
            await _ahunter_прилёг(redis)
            return []
        out: list[geocode.Query] = []
        for п in подсказки_ahunter:
            # Тот же город и пункт, что у клиента, и ТОТ ЖЕ дом: Ahunter
            # отдаёт дома по началу номера («13» → 13, 13а, 130 — ревью
            # 16.09), а чужой номер карта подтвердила бы как настоящий.
            # Микрорайон клиента справочник пишет в строке улицы («мкр
            # Байкальск, ул Боткина»), не пунктом: для мягких типов пункт
            # ищется и там (ревью 16.09).
            пункт = п.settlement
            if (
                пункт is None
                and geocode._норм(parsed.settlement_type or "") in geocode._ТИПЫ_МАССИВА_МЯГКИЕ
            ):
                пункт = п.street
            if not _подсказка_о_том_же(query, п.city, пункт):
                continue
            if geocode.house_key(п.house) != geocode.house_key(query.house):
                continue
            out.append(dataclasses.replace(query, street=п.street or query.street))
        return out

    async def саджест() -> list[geocode.Query]:
        if not саджест_включён:
            return []
        if not await _в_пределах(redis, suggest_calls_key(), потолок_саджеста):
            # Потолок или доля Саджеста выбраны — отказ по факту (пакет 5).
            if отказы is not None:
                отказы.саджест = True
            return []
        try:
            подсказки = await yandex_suggest.suggest(
                query,
                on_request=lambda: _занять(redis, suggest_calls_key(), потолок_саджеста, "suggest"),
            )
        except geocode.GeocodeError as exc:
            # Любой отказ Саджеста — сеть, 5xx, не тот JSON, потолок — набор
            # подсказчиков этой строки неполный (ревью 20.09, C1).
            log.warning("geocode.suggest_failed", kind=exc.kind, status=exc.status)
            if отказы is not None:
                отказы.саджест = True
            return []
        return [dataclasses.replace(query, street=пя.street, house=пя.house) for пя in подсказки]

    проверенные: set[tuple[str, str]] = {(query.street.lower(), geocode.house_key(query.house))}
    dadata_жива = dadata_снова
    for источник, подсказчик in (("speller", спеллер), ("ahunter", ahunter_), ("suggest", саджест)):
        for исправленный in await подсказчик():
            ключ = (исправленный.street.lower(), geocode.house_key(исправленный.house))
            if ключ in проверенные:
                continue
            проверенные.add(ключ)
            # Вердикт — по разбору клиента: подсказка на ЧУЖУЮ улицу
            # («Советская» на «Звенигародскую») обязана отказать, как и
            # раньше. Только правка Спеллера входит в вердикт: она ограничена
            # двумя буквами в слове и одним вариантом, а «Ленена» против
            # «Ленина» иначе — «улица не та».
            для_вердикта = (
                dataclasses.replace(parsed, street=исправленный.street)
                if источник == "speller"
                else parsed
            )
            статус, hit, hits, provider = "", None, [], ""
            if dadata_жива is not None:
                try:
                    hits = await dadata_жива(исправленный)
                    статус, hit = geocode.verdict(для_вердикта, city, hits)
                    provider = "dadata"
                except geocode.GeocodeError as exc:
                    # Потолок или отказ DaData — не приговор кандидату: дальше
                    # его проверят карты режима, а DaData до конца не зовём.
                    log.warning("geocode.suggest_dadata_failed", kind=exc.kind, status=exc.status)
                    dadata_жива = None
            try:
                if статус != geocode.GEO_EXACT:
                    hits, provider = await _search(
                        redis, режим, исправленный, потолок_яндекса, отказы
                    )
                    статус, hit = geocode.verdict(для_вердикта, city, hits)
                # Здесь набор без N24 намеренно: подсказанная улица — уже
                # догадка, «улица не та» по ней второй картой не перепроверяется.
                if (
                    статус in _ВТОРАЯ_КАРТА_ПОСЛЕ
                    and режим == "osm_then_yandex"
                    and provider == "nominatim"
                    and yandex_geocoder.enabled()
                    and await _яндекс_доступен(redis, потолок_яндекса, отказы)
                ):
                    hits = await yandex_geocoder.search(
                        исправленный,
                        on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                    )
                    статус, hit = geocode.verdict(для_вердикта, city, hits)
                    provider = "yandex"
            except geocode.GeocodeError as exc:
                log.warning("geocode.suggest_recheck_failed", kind=exc.kind, status=exc.status)
                if exc.provider == yandex_geocoder.PROVIDER and отказы is not None:
                    отказы.яндекс = True  # набор без Яндекса неполный (C1)
                continue
            if статус == geocode.GEO_EXACT:
                log.info("geocode.suggest_helped", source=источник, provider=provider)
                return статус, hit, hits, f"{источник}+{provider}"
    return None


def _подсказка_о_том_же(query: geocode.Query, город: str | None, пункт: str | None) -> bool:
    """Подсказка про тот же город и тот же пункт, что у клиента."""
    if город and query.city and geocode._норм(город) != geocode._норм(query.city):
        return False
    if query.settlement:
        return bool(пункт) and geocode._пункт_назван(
            geocode._норм(query.settlement), geocode._слова(пункт)
        )
    # Клиент пункта не называл: подсказка с пунктом — про другое место.
    return not пункт or (bool(query.city) and geocode._норм(пункт) == geocode._норм(query.city))


async def _ahunter_лежит(redis: Redis) -> bool:
    try:
        return bool(await redis.get("geo:ahunter:down"))
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)
        return False


async def _ahunter_прилёг(redis: Redis) -> None:
    try:
        await redis.set("geo:ahunter:down", "1", ex=AHUNTER_DOWN_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)


def _день_яндекса(day: datetime | None = None) -> str:
    """Сутки Яндекса — московские (бой 13.09: Саджест заблокирован на 1 084 из
    1 000 при нашем счётчике 845 — наш день начинался в 03:00 МСК)."""
    return (day or datetime.now(UTC)).astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d")


def suggest_calls_key(day: datetime | None = None) -> str:
    return "geo:yandex_suggest:calls:" + _день_яндекса(day)


def ahunter_calls_key(day: datetime | None = None) -> str:
    return "geo:ahunter:calls:" + _день_яндекса(day)


def speller_calls_key(day: datetime | None = None) -> str:
    return "geo:speller:calls:" + _день_яндекса(day)


async def ahunter_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(ahunter_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_read_failed", error=type(exc).__name__)
        return 0


async def speller_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(speller_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_read_failed", error=type(exc).__name__)
        return 0


async def suggest_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(suggest_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.suggest_counter_read_failed", error=type(exc).__name__)
        return 0


async def _в_пределах(redis: Redis, ключ: str, потолок: int | None) -> bool:
    if потолок is None:
        return True
    try:
        return int(await redis.get(ключ) or 0) < int(потолок)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_read_failed", error=type(exc).__name__)
        return False


async def _посчитать(redis: Redis, ключ: str) -> None:
    """`SET NX EX` + `INCR`: срок ключа не скользит, сутки — это сутки."""
    try:
        await redis.set(ключ, 0, nx=True, ex=2 * 24 * 3600)
        await redis.incr(ключ)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)


def _хотим_яндекс(статус: str, уровень: str | None) -> bool:
    """Вторая карта по городу: спор города об улице её не останавливает — она
    спрашивает тот же город и спор может только разрешить (N24, §2.7)."""
    return (
        статус in _ВТОРАЯ_КАРТА_ПОСЛЕ or geocode.street_mismatch_continues(статус, уровень)
    ) and yandex_geocoder.enabled()


def _цепочка_продолжается(
    статус: str, после: frozenset[str], уровень: str | None, *, город_спорит: bool
) -> bool:
    """Отказ, после которого идём дальше ЗА карты города: из набора `после` —
    или «улица не та» у уровней A/B без спора города (N24; один сторож —
    `geocode.street_mismatch_continues_beyond_city`)."""
    return статус in после or geocode.street_mismatch_continues_beyond_city(
        статус, уровень, city_disputes=город_спорит
    )


def yandex_calls_key(day: datetime | None = None) -> str:
    return "geo:yandex:calls:" + _день_яндекса(day)


def dadata_calls_key(day: datetime | None = None) -> str:
    return "geo:dadata:calls:" + _день_яндекса(day)


async def dadata_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(dadata_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.dadata_counter_read_failed", error=type(exc).__name__)
        return 0


async def yandex_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(yandex_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.yandex_counter_read_failed", error=type(exc).__name__)
        return 0


async def _яндекс_доступен(
    redis: Redis, потолок: int | None, отказы: _Отказы | None = None
) -> bool:
    """Дневной потолок бесплатного тарифа: сверх него Яндекс не спрашиваем.
    `отказы` запоминает отказ по факту — для удержания улик (пакет 5)."""
    доступен = await _в_пределах(redis, yandex_calls_key(), потолок)
    if not доступен and отказы is not None:
        отказы.яндекс = True
    return доступен


async def _посчитать_яндекс(
    redis: Redis, потолок: int | None = None, отказы: _Отказы | None = None
) -> None:
    try:
        await _занять(redis, yandex_calls_key(), потолок, "yandex")
    except geocode.GeocodeError as exc:
        # Потолок выбрали между проверкой и занятием — тот же отказ по факту.
        if exc.kind == "limit" and отказы is not None:
            отказы.яндекс = True
        raise


async def _занять(redis: Redis, ключ: str, потолок: int | None, provider: str) -> None:
    """Занять запрос в суточном потолке ДО похода: счётчик растёт атомарно, и
    пятьдесят параллельных задач не проскочат «меньше 900» разом (бой 13.09).
    Сверх потолка — `limit`: интеграция не ходит, задача отдаёт строку
    следующей карте. Это НАШ потолок, а не бан провайдера (`blocked`): строку
    он не осуждает и тревогу блокировки не поднимает. Несостоявшийся поход
    из счётчика вычитается — иначе к вечеру счётчик показывал бы 9 773 при
    потолке 9 000, а починка с меньшим потолком сдвигала бы счёт живым."""
    try:
        await redis.set(ключ, 0, nx=True, ex=2 * 24 * 3600)
        n = int(await redis.incr(ключ))
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)
        return
    if потолок is None or n <= int(потолок):
        return
    # Отказ — независимо от того, удалось ли вернуть счётчик: иначе упавший
    # DECR отправил бы поход сверх потолка (ревью 13.09).
    try:
        await redis.decr(ключ)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)
    raise geocode.GeocodeError(provider, "limit")


async def _занять_dadata(redis: Redis, потолок: int | None) -> None:
    """Место в потолке DaData — или `limit`, если она на сутки закрыта (403)."""
    if await _закрыт_на_сутки(redis, "dadata"):
        raise geocode.GeocodeError("dadata", "limit")
    await _занять(redis, dadata_calls_key(), потолок, "dadata")


def доля_починки(
    потолок: int | None,
    provider: str,
    *,
    yandex_pct: int = app_settings.REPAIR_SHARE_YANDEX_DEFAULT,
) -> int | None:
    """Потолок для задачи починки — ОДИН источник истины «что получает починка».

    Яндекс и Саджест — `yandex_pct` процентов суточного потолка (пакет 5,
    решение владельца 20.09); потолок `None` считается за
    :data:`REPAIR_SHARE_BASE_UNLIMITED`. Остальные (DaData) — суточный минус
    :data:`ЗАПАС_ЖИВЫМ`, но не меньше половины: маленький потолок в настройках не
    должен выключать починку насовсем. Живая задача сверяется со всем потолком,
    задача починки — с долей по тому же счётчику: починка ходит, пока
    суммарный счёт дня меньше доли, живому потоку гарантирован остаток."""
    if provider in ("yandex", "yandex_suggest"):
        база = REPAIR_SHARE_BASE_UNLIMITED if потолок is None else int(потолок)
        return база * int(yandex_pct) // 100
    if потолок is None:
        return None
    return max(int(потолок) - ЗАПАС_ЖИВЫМ.get(provider, 0), int(потолок) // 2)


async def _тревога_потолка(redis: Redis, provider: str) -> None:
    """Один раз в московские сутки — в журнал: потолок карты выбран, дальше
    без неё. Каждая строка писала бы это тысячу раз."""
    ключ = f"geo:limit_alerted:{provider}:{_день_яндекса()}"
    try:
        if await redis.set(ключ, 1, nx=True, ex=2 * 24 * 3600):
            log.warning("geocode.daily_limit_reached", provider=provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.limit_alert_failed", error=type(exc).__name__)


#: Столько блокировок за час — и провайдер считается забаненным: стучать
#: дальше значит продлевать бан (политика OSM).
BLOCKED_THRESHOLD = 3


def _до_московской_полуночи() -> int:
    сейчас = datetime.now(ZoneInfo("Europe/Moscow"))
    полночь = (сейчас + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((полночь - сейчас).total_seconds()))


async def _закрыть_на_сутки(redis: Redis, provider: str) -> None:
    """Провайдер ответил «лимит выбран» — до московской полуночи к нему не ходим."""
    try:
        await redis.set(f"geo:exhausted:{provider}", 1, ex=_до_московской_полуночи())
        log.warning("geocode.exhausted_today", provider=provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.exhausted_flag_failed", error=type(exc).__name__)


async def _закрыт_на_сутки(redis: Redis, provider: str) -> bool:
    try:
        return bool(await redis.get(f"geo:exhausted:{provider}"))
    except Exception:  # noqa: BLE001
        return False


async def dadata_exhausted(redis: Redis) -> bool:
    """Для починки: DaData на сегодня выбрана — хвост ждёт завтра."""
    return await _закрыт_на_сутки(redis, "dadata")


#: DaData НЕ ОТВЕТИЛА ПО СЕТИ (обрыв, таймаут, 5xx, не тот JSON) — столько
#: починка не включает обход `dadata_back` (ревью 19.09, C2). У 403 и нашего
#: потолка свои флаги (`geo:exhausted:dadata`, доля починки), и починка при них
#: выходит раньше; у сетевого сбоя флага не было — и каждый заход снова
#: сбрасывал те же строки в лежащую DaData, воркер судил их без неё и
#: записывал с тем же флагом. Ключ продлевает каждый новый сбой; как
#: `geo:ahunter:down`, живёт в Redis, а не в базе: это состояние провайдера,
#: не свойство строки.
DADATA_DOWN_KEY = "geo:dadata:down"
DADATA_DOWN_SEC = 10 * 60


async def dadata_down(redis: Redis) -> bool:
    """Для починки: DaData лежит по сети — обход `dadata_back` ждёт."""
    try:
        return bool(await redis.get(DADATA_DOWN_KEY))
    except Exception as exc:  # noqa: BLE001 — предохранитель не важнее задачи
        log.warning("geocode.counter_failed", error=type(exc).__name__)
        return False


async def _dadata_прилегла(redis: Redis) -> None:
    try:
        await redis.set(DADATA_DOWN_KEY, "1", ex=DADATA_DOWN_SEC)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.counter_failed", error=type(exc).__name__)


async def _учесть_отказ_dadata(redis: Redis, exc: geocode.GeocodeError) -> None:
    """Один учёт отказа DaData на все её походы в задаче (страна, дом, подсказка,
    дробь, область, массив, место): `blocked` — 403, закрыта до московской
    полуночи; `limit` — наш потолок, его держит счётчик, флагов не надо; всё
    остальное (сеть, 5xx, не тот JSON) — «лежит» на :data:`DADATA_DOWN_SEC`."""
    if exc.kind == "blocked":
        await _закрыть_на_сутки(redis, "dadata")
    elif exc.kind != "limit":
        await _dadata_прилегла(redis)


async def _провайдер_забанен(redis: Redis, provider: str) -> bool:
    try:
        n = await redis.get(f"geo:blocked:{provider}")
    except Exception as exc:  # noqa: BLE001 — предохранитель не важнее задачи
        log.warning("geocode.blocked_counter_read_failed", error=type(exc).__name__)
        return False
    return int(n or 0) >= BLOCKED_THRESHOLD


async def _ждать_темпа(redis: Redis) -> None:
    """Не чаще запроса в секунду — на весь воркер, а не на задачу."""
    ждали = 0.0
    while ждали < RATE_WAIT_MAX_SEC:
        if await redis.set(RATE_LOCK_KEY, "1", nx=True, ex=RATE_LOCK_TTL_SEC):
            return
        await asyncio.sleep(RATE_WAIT_STEP_SEC)
        ждали += RATE_WAIT_STEP_SEC
    # Очередь у замка длиннее полминуты — это не сбой карты, а толпа: повтор
    # через Retry, строку не трогаем.
    raise geocode.GeocodeError(nominatim.PROVIDER, "network")


async def _geocode_place(
    factory: Any,
    redis: Redis,
    candidate_id: uuid.UUID,
    место: geocode.Place,
    city: Any,
    снимок: _Снимок,
    *,
    client_id: uuid.UUID,
    conversation_id: uuid.UUID,
    dadata_включён: bool,
    потолок_dadata: int | None,
    потолок_яндекса: int | None = None,
    origin: str = ORIGIN_LIVE,
    свежая: bool = True,
    отказы: _Отказы | None = None,
    прежнее: dict[str, Any] | None = None,
    содержание: dict[str, Any] | None = None,
    след_строки: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> str:
    """Место без улицы (владелец 13.09): точку даёт DaData — пункт, массив, СНТ.

    OSM сюда не ходит: у места нет дома, и вердикт по дому неприменим. Яндекс
    — второй, ПОСЛЕ отказа DaData (владелец 18.09, «ЖК Дубровино» при
    объявлении в Москве: DaData `not_found`, Яндекс на «Москва, ЖК Дубровино»
    отдаёт место), под сторожами региона и слов места (`yandex_place_hit`)
    и в своём потолке; Саджест на такой запрос пуст — не спрашивается. Без
    DaData вовсе (ключа нет, выключена) место остаётся «не найдено», если и
    Яндекс его не знает, — честнее, чем точка райцентра; DaData есть, но на
    сегодня выбрана — место ждёт её `pending`, попытка не тратится. Найденное
    место ложится в карточку так же, как дом (степень «приблизительно»:
    точка пункта или массива — `card_grade`): автозапись берёт уровень A, и
    следующая реплика с улицей и домом заменит его (`autofill_address`,
    «адрес постепенно»).
    """
    начало = time.monotonic()
    отказы = отказы if отказы is not None else _Отказы()
    статус, hit_места, provider = geocode.GEO_NOT_FOUND, None, "none"
    отложено, считать_попытку = False, True
    if city is None:
        статус = geocode.GEO_NO_CITY
    elif dadata_включён:
        provider = "dadata"

        async def _посчитать_dadata() -> None:
            await _занять_dadata(redis, потолок_dadata)
            _посчитать_поход_в_копилку()

        try:
            hits = await dadata.search_place(
                место,
                region=city.region,
                # Город назвал клиент — ищем в нём; место без пункта —
                # микрорайон самого города объявления.
                city=место.locality
                or (None if место.settlement or geocode._регион_вместо_города(city) else city.name),
                on_request=_посчитать_dadata,
            )
            статус, hit_места = geocode.place_verdict(место, city, hits)
        except geocode.GeocodeError as exc:
            log.warning("geocode.place_failed", kind=exc.kind, status=exc.status)
            плановый = exc.kind in ("blocked", "limit")
            await _учесть_отказ_dadata(redis, exc)
            if плановый and origin == ORIGIN_LIVE and not dry_run:
                await _тревога_потолка(redis, "dadata")
            # Место ждёт DaData: у OSM и Яндекса точки места нет. Свежее —
            # без попытки (потолок вернётся), старое — с попыткой: потолок
            # попыток починки остановит ожидание, когда DaData не вернётся.
            статус, отложено = geocode.GEO_PENDING, True
            считать_попытку = not плановый or (not свежая and origin != ORIGIN_REPAIR)
            provider = "none"
    if статус in geocode.REFUSAL_STATUSES and city is not None and yandex_geocoder.enabled():
        # ВТОРАЯ КАРТА ЗА МЕСТОМ (владелец 18.09). Свободная строка «регион,
        # город, место»; дальше первого прошедшего сторожа ответа — мусор
        # вплоть до другой страны, и его отсекают регион и слова места.
        try:
            if await _яндекс_доступен(redis, потолок_яндекса, отказы):
                hits_y = await yandex_geocoder.search(
                    _запрос_места_яндексу(место, city),
                    on_request=lambda: _посчитать_яндекс(redis, потолок_яндекса, отказы),
                )
                место_я = geocode.yandex_place_hit(место, city, hits_y)
                if место_я is not None:
                    log.info(
                        "geocode.place_yandex",
                        candidate_id=str(candidate_id),
                        was=статус,
                        kind=место_я.kind,
                    )
                    статус, hit_места, provider = geocode.GEO_EXACT, место_я, "yandex"
        except geocode.GeocodeError as exc:
            # Яндекс не ответил — остаёмся с отказом DaData, он честный, а
            # набор карт места без Яндекса неполный при любом отказе (C1).
            log.warning("geocode.place_yandex_failed", kind=exc.kind, status=exc.status)
            отказы.яндекс = True
    hit = (
        geocode.GeoHit(
            street=hit_места.name,
            house="",
            settlement=hit_места.settlement,
            city=hit_места.city,
            region=hit_места.region,
            lat=hit_места.lat,
            lon=hit_места.lon,
            house_level=False,
        )
        if hit_места is not None
        else None
    )
    formatted = geocode.format_place(hit_места, место) if hit_места is not None else None
    log.info(
        "geocode.place_done" if not отложено else "geocode.deferred",
        candidate_id=str(candidate_id),
        provider=provider,
        status=статус,
        ms=round((time.monotonic() - начало) * 1000),
    )
    без_dadata = not dadata_включён or отложено
    # Место вариантов не пишет — удерживаются точка (4) и слово отказа (1);
    # `недостаёт` без DaData: её отказ у места — `pending`, а не суд.
    новое = _Вердикт(статус, formatted, provider, hit, [], без_dadata)
    недостаёт = отказы.недостаёт(dadata=False)
    вердикт, prev = _с_учётом_прежнего(
        candidate_id,
        новое,
        прежнее,
        kind=address_parse.KIND_PLACE,
        недостаёт=недостаёт,
        содержание=содержание,
        origin=origin,
    )
    # След суда места (пакет 6.0а): правила нет, в след — регион и пункт
    # ответа карты и форма запроса `place`; удержание — как у дома.
    след = (
        geocode.trace_restored(след_строки)
        if вердикт is not новое
        else geocode.verdict_trace(
            след_строки,
            rule=None,
            policy=None,
            km=None,
            hit=hit,
            query_form=geocode.QUERY_FORM_PLACE,
        )
    )
    статус, formatted, provider, hit = (
        вердикт.статус,
        вердикт.formatted,
        вердикт.provider,
        вердикт.hit,
    )
    # Место годно только с точкой (степень `approx` по контракту 18.09);
    # словами — никогда: задача на заведомый `skip` не ставится. Считается до
    # записи: сухому суду нужна та же степень без записи.
    степень = geocode.card_grade(
        address_parse.KIND_PLACE,
        статус,
        provider,
        hit.lat if hit is not None else None,
        hit.lon if hit is not None else None,
        formatted,
    )
    _сложить_в_копилку(
        DryVerdict(
            status=статус,
            formatted=formatted,
            provider=provider,
            hit_city=(hit.city or hit.settlement) if hit is not None else None,
            km=None,
            rule=None,
            policy=None,
            shadow=None,
            trace=след,
            variants_n=0,
            office=None,
            would_autofill=степень is not None and not отложено,
            kept=вердикт is not новое,
            missing=tuple(недостаёт),
        )
    )
    if dry_run:
        return "deferred" if отложено else статус
    записали = await _пометить(
        factory,
        candidate_id,
        статус,
        formatted,
        provider,
        hit,
        снимок,
        считать_попытку=считать_попытку,
        without_dadata=без_dadata,
        prev=prev,
        trace=след,
    )
    if not записали:
        log.info("geocode.stale", candidate_id=str(candidate_id))
        await enqueue_geocode(redis, candidate_id, defer_sec=3, suffix="again", origin=origin)
        return "stale"
    if отложено:
        return "deferred"
    await _известить(redis, client_id, conversation_id, reason="address_geocoded")
    if степень is not None:
        await enqueue_autofill(redis, conversation_id)
    return статус


def _запрос_места_яндексу(место: geocode.Place, city: Any) -> geocode.Query:
    """«Москва, ЖК Дубровино»: регион объявления, город (клиента или
    объявления, если он не повторяет регион и слаг — не регион), само место
    строкой улицы без дома."""
    город = место.locality or (None if geocode._регион_вместо_города(city) else city.name)
    if город and geocode._норм(город) == geocode._норм(city.region):
        город = None
    return geocode.Query(
        region=city.region, city=город, settlement=None, street=место.query_text, house=""
    )


#: «Снимок не трогать» — умолчание `_пометить`: отличать от `None` (стереть).
_СНИМОК_НЕ_ТРОГАТЬ: object = object()


def _сухой_отказ(статус: str, provider: str) -> DryVerdict:
    """Итог сухого суда, когда карта отказала до вердикта (`blocked`/`error`):
    строки карты нет, правила нет, автозаписи не было бы."""
    return DryVerdict(
        status=статус,
        formatted=None,
        provider=provider,
        hit_city=None,
        km=None,
        rule=None,
        policy=None,
        shadow=None,
        trace=None,
        variants_n=0,
        office=None,
        would_autofill=False,
    )


@dataclass(frozen=True, slots=True)
class _Вердикт:
    """Что задача собралась записать — до сверки с прежними уликами."""

    статус: str
    formatted: str | None
    provider: str
    hit: geocode.GeoHit | None
    варианты: list[dict[str, Any]]
    без_dadata: bool


def _с_учётом_прежнего(
    candidate_id: uuid.UUID,
    новое: _Вердикт,
    прежнее: dict[str, Any] | None,
    *,
    kind: str,
    недостаёт: list[str],
    содержание: dict[str, Any] | None,
    origin: str,
) -> tuple[_Вердикт, dict[str, Any] | None | object]:
    """Монотонность пересуда (пакет 5, решение владельца 20.09).

    Возвращает вердикт к записи и что положить в `geo_prev`:
    * `pending`/`error`/`blocked`/`no_city` — записи вердикта нет, снимок не
      трогается (настоящий пересуд впереди);
    * набор карт полный (`недостаёт` пуст) — новый вердикт, снимок стирается,
      а ухудшение при полном наборе — журнал `rejudge_weaker` (карта поправила
      справочник, это новая информация);
    * набор неполный и новый СЛАБЕЕ прежнего (`keeps_previous`) — в строку
      возвращаются улики снимка; флаг «без DaData» — по ЭТОМУ суду (ревью
      20.09, п. 4: из снимка он заводил петлю с `dadata_back`);
    * набор неполный в любом исходе — в `geo_prev` снимок ЗАПИСЫВАЕМОГО
      вердикта с `missing`: строка остаётся в пересуде, обход `kept_blind`
      вернёт её, когда карты снова в деле (ревью 20.09, п. 1). Слепой суд
      без снимка (первый суд старой живой строки) получает снимок так же.
    """
    if новое.статус in geocode.CHECKING_STATUSES:
        return новое, _СНИМОК_НЕ_ТРОГАТЬ
    hit = новое.hit
    ранг_нового = geocode.verdict_rank(
        kind,
        новое.статус,
        новое.provider,
        hit.lat if hit is not None else None,
        hit.lon if hit is not None else None,
        новое.formatted,
        новое.варианты,
    )
    ранг_прежнего = int(прежнее.get("rank") or 0) if прежнее is not None else 0
    удержать = прежнее is not None and geocode.keeps_previous(
        ранг_нового, ранг_прежнего, bool(недостаёт)
    )
    if удержать:
        assert прежнее is not None
        lat, lon = прежнее.get("lat"), прежнее.get("lon")
        точка = (
            geocode.GeoHit(
                street=None,
                house=None,
                settlement=None,
                city=None,
                region=None,
                lat=float(lat),
                lon=float(lon),
                house_level=False,
            )
            if lat is not None and lon is not None
            else None
        )
        итог = _Вердикт(
            str(прежнее.get("status") or новое.статус),
            прежнее.get("formatted"),
            str(прежнее.get("provider") or новое.provider),
            точка,
            list(прежнее.get("variants") or []),
            новое.без_dadata,
        )
        log.info(
            "geocode.rejudge_kept",
            candidate_id=str(candidate_id),
            was=(итог.статус, итог.provider),
            new=(новое.статус, новое.provider),
            rank_was=ранг_прежнего,
            rank_new=ранг_нового,
            missing=недостаёт,
            reason=прежнее.get("reason"),
            origin=origin,
        )
    else:
        итог = новое
        if прежнее is not None and ранг_нового < ранг_прежнего:
            log.info(
                "geocode.rejudge_weaker",
                candidate_id=str(candidate_id),
                was=(прежнее.get("status"), прежнее.get("provider")),
                new=(новое.статус, новое.provider),
                rank_was=ранг_прежнего,
                rank_new=ранг_нового,
                origin=origin,
            )
    if not недостаёт or итог.статус not in geocode.RECHECK_STATUSES:
        # Полный набор — или вердикт, который обход не пересматривает (`exact`
        # в карточке): снимок стирается, пересуд окончен.
        return итог, None
    hit = итог.hit
    снимок = снимок_улик(
        {
            "kind": kind,
            "geo_status": итог.статус,
            "geo_formatted": итог.formatted,
            "geo_variants": итог.варианты or None,
            "geo_provider": итог.provider,
            "geo_lat": hit.lat if hit is not None else None,
            "geo_lon": hit.lon if hit is not None else None,
            "geo_verdict_version": geocode.VERDICT_VERSION,
            "geo_without_dadata": итог.без_dadata,
            "geo_checked_at": datetime.now(UTC),
            **(содержание or {}),
        },
        reason="kept" if удержать else "blind",
        missing=недостаёт,
    )
    return итог, снимок


async def _пометить(
    factory: Any,
    candidate_id: uuid.UUID,
    статус: str,
    formatted: str | None,
    provider: str,
    hit: geocode.GeoHit | None,
    снимок: _Снимок,
    variants: list[dict[str, Any]] | None = None,
    считать_попытку: bool = True,
    office: str | None = None,
    *,
    without_dadata: bool = False,
    prev: dict[str, Any] | None | object = _СНИМОК_НЕ_ТРОГАТЬ,
    trace: dict[str, Any] | None | object = _СНИМОК_НЕ_ТРОГАТЬ,
) -> bool:
    """Записать вердикт, если строка та же, по которой считали.

    Условие — не только «ещё не решена», но и «посёлок и город клиента те же,
    что в снимке фазы 1»: иначе вердикт без посёлка ложился бы поверх строки,
    в которую посёлок дописали, пока мы ходили к карте.

    `считать_попытку=False` — отказ отложен до возвращения DaData: попытка
    не тратится, иначе неделя без DaData закрыла бы строке дверь починки.
    `office` — квартира из хвоста дроби («10/77» → кв 77, правило
    `fraction_head`): пишется только в пустое поле, названную клиентом не
    перебивает.
    `without_dadata` — вердикт вынесен без ответа DaData (N13, 19.09): флаг
    ложится и на `pending` (отложено), и на `exact` от OSM — потребитель
    (обход починки) смотрит только `RECHECK_STATUSES`; один код на все
    статусы проще условного. Версия судьи пишется всегда.
    `prev` — что положить в `geo_prev` (пакет 5): снимок при слепом суде,
    `None` при полном; по умолчанию не трогается — у `pending`/`error`/
    `blocked` настоящий пересуд ещё впереди, снимок ждёт его.
    `trace` — след суда (пакет 6.0а, `geocode.verdict_trace`): пишется вместе с
    вердиктом и по тому же условию, что `geo_prev` — не у `pending`/`error`/
    `blocked`, где суда ещё не было и прежний след обязан остаться.
    """
    async with factory() as db:
        значения: dict[str, Any] = {}
        if office:
            значения["office"] = sa.func.coalesce(ClientAddressCandidate.office, office)
        if prev is not _СНИМОК_НЕ_ТРОГАТЬ and статус not in geocode.CHECKING_STATUSES:
            значения["geo_prev"] = prev
        if trace is not _СНИМОК_НЕ_ТРОГАТЬ and статус not in geocode.CHECKING_STATUSES:
            значения["trace"] = trace
        result = await db.execute(
            sa.update(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.id == candidate_id,
                sa.or_(
                    ClientAddressCandidate.geo_status.in_(tuple(s for s in _ПЕРЕПРОВЕРЯЕМЫЕ if s)),
                    ClientAddressCandidate.geo_status.is_(None),
                ),
                ClientAddressCandidate.settlement.is_not_distinct_from(снимок.settlement),
                ClientAddressCandidate.locality.is_not_distinct_from(снимок.locality),
            )
            .values(
                geo_status=статус,
                geo_formatted=formatted,
                geo_lat=hit.lat if hit else None,
                geo_lon=hit.lon if hit else None,
                geo_variants=variants or None,
                geo_provider=provider,
                geo_checked_at=datetime.now(UTC),
                geo_attempts=ClientAddressCandidate.geo_attempts + (1 if считать_попытку else 0),
                geo_verdict_version=geocode.VERDICT_VERSION,
                geo_without_dadata=without_dadata,
                **значения,
            )
        )
        записали = bool(getattr(result, "rowcount", 0))
        if записали and статус not in geocode.CHECKING_STATUSES:
            # Строка — источник адреса карточки, записанного автоматикой, и
            # карта ответила заново (пункт дописан из соседней реплики,
            # части пришли, пока вердикт был сброшен): текст карточки
            # пересобирается из новой строки карты — или, если карта
            # окончательно отказала, честно словами клиента (владелец 18.09).
            # Ровно те статусы, при которых пересборка возможна: `pending`
            # (отложено без DaData), `error`, `blocked` не тратят два
            # `db.get` на каждую запись вердикта.
            row = await db.get(ClientAddressCandidate, candidate_id)
            client = await db.get(Client, row.client_id) if row is not None else None
            if row is not None and client is not None and client.address_candidate_id == row.id:
                await refresh_auto_address(db, client, row)
        await db.commit()
        return записали


_МЕСЯЦЫ = frozenset(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
)


def _по_местам(строки: list[ClientAddressCandidate]) -> list[ClientAddressCandidate]:
    """Одна строка на место: выше по степени (`candidate_grade`: exact >
    approx > text), затем точнее по дому («31 А» против «31» — стенд 15.09),
    затем ранняя."""

    def вес(r: ClientAddressCandidate) -> tuple[int, int, float]:
        return (
            grade_weight(candidate_grade(r)),
            len(geocode.house_key(r.house)) if r.kind == address_parse.KIND_HOUSE else 0,
            -_aware(r.detected_at).timestamp(),
        )

    места: list[ClientAddressCandidate] = []
    for r in строки:
        for i, m in enumerate(места):
            if одно_место(r, m):
                if вес(r) > вес(m):
                    места[i] = r
                break
        else:
            места.append(r)
    return места


def _похоже_на_дату(street: str) -> bool:
    """«Октября 7», «Мая 9»: улица из одного слова-месяца — это дата."""
    слова = [w for w in street.lower().split() if w]
    return len(слова) == 1 and слова[0] in _МЕСЯЦЫ


#: Причины выбора строки из нескольких годных (`_выбрать_из_нескольких`).
PICK_ANSWER_TO_QUESTION = "answer_to_question"
PICK_LAST_NAMED = "last_named"
PICK_SAME_REPLY_HIGHER_GRADE = "same_reply_higher_grade"
PICK_TWO_IN_ONE_REPLY = "two_in_one_reply"


async def _выбрать_из_нескольких(
    db: AsyncSession, conv: Conversation, годные: list[ClientAddressCandidate]
) -> tuple[ClientAddressCandidate | None, str]:
    """Два и больше РАЗНЫХ места в одном диалоге — выбор без человека
    (владелец 18.09). Ответ на вопрос об адресе (оператора или системы —
    участок бота формулирует вопрос словом из `inbound._ВОПРОС_ОБ_АДРЕСЕ`)
    важнее всего: это и есть адрес выезда; иначе — последний названный: о нём
    говорили только что (как у `refine_address_parts` — «только в последний»).

    Две головы в ОДНОЙ реплике: равной степени — перечисление («три клуба»
    из вакансии, замер ok-39), не выбираем; разной — карта уже рассудила,
    чей разбор верен: строка модели-читателя рождается с `message_id` строки
    правил (`address_llm.py`) и при задаче addr-fill, ждущей 90 с, попадает в
    тот же прогон — «11 улица Озёрная» (street_mismatch, text) против
    «Озёрная 11» (exact) обязана дать exact, а не вечный skip. То же
    правило, что `YIELD_SAME_REPLY` в лестнице уступок.

    Остальные строки остаются `pending` и показываются «Также назван»; между
    двумя записанными местами карточка потом не переезжает
    (`auto_address_yields_to`).
    """
    from app.services.inbound import _оператор_спросил_адрес  # лениво: inbound импортирует clients

    ответы = [
        r
        for r in годные
        if r.message_at is not None
        and await _оператор_спросил_адрес(db, conv, before=_aware(r.message_at))
    ]
    пул = ответы or годные

    def когда(r: ClientAddressCandidate) -> datetime:
        return _aware(r.message_at or r.detected_at)

    пул = sorted(пул, key=когда, reverse=True)
    верх = пул[0]
    та_же_реплика = [
        r
        for r in пул
        if (верх.message_id is not None and r.message_id == верх.message_id)
        or когда(r) == когда(верх)
    ]
    if len(та_же_реплика) > 1:
        по_степени = sorted(
            та_же_реплика, key=lambda r: grade_weight(candidate_grade(r)), reverse=True
        )
        лучшая, вторая = по_степени[0], по_степени[1]
        if not grade_beats(candidate_grade(лучшая), candidate_grade(вторая)):
            return None, PICK_TWO_IN_ONE_REPLY
        return лучшая, PICK_SAME_REPLY_HIGHER_GRADE
    return верх, (PICK_ANSWER_TO_QUESTION if ответы else PICK_LAST_NAMED)


async def _известить(
    redis: Redis, client_id: uuid.UUID, conversation_id: uuid.UUID, *, reason: str
) -> None:
    """Кадр `client:updated` — «перечитайте карточку». Не роняет задачу."""
    try:
        await publish_event(
            redis,
            "client:updated",
            {
                "client_id": str(client_id),
                "conversation_id": str(conversation_id),
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "geocode.frame_not_published", client_id=str(client_id), error=type(exc).__name__
        )


async def _тревога_блокировки(redis: Redis, provider: str) -> None:
    """Счётчик блокировок за час — `SET NX EX` + `INCR`, срок не скользит."""
    ключ = f"geo:blocked:{provider}"
    try:
        await redis.set(ключ, 0, nx=True, ex=3600)
        n = await redis.incr(ключ)
        log.warning("geocode.blocked", provider=provider, blocked_last_hour=int(n))
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode.blocked_counter_failed", error=type(exc).__name__)


async def _отсеять_речь(
    db: AsyncSession,
    conv: Conversation,
    годные: list[ClientAddressCandidate],
    отклонённые: list[uuid.UUID],
) -> list[ClientAddressCandidate]:
    """Годные строки без тех, чья реплика по нынешнему разбору без адреса:
    такие отклоняются (без человека) и записываются сразу — решение о строке
    не должно зависеть от того, чем кончится запись карточки. Строки
    модели-читателя, места, строки без сообщения не судятся: модель читала
    там, где правила молчали; место разбирается иначе; без реплики нечего
    перечитывать. Идентификаторы отклонённых — в `отклонённые`: кадр о них
    задача даёт после сессии (`autofill_address`), сама функция кадров не шлёт."""
    # Лениво: inbound импортирует clients и половину сервисов, воркеру они
    # нужны только здесь (как у `_выбрать_из_нескольких`).
    from app.services.inbound import разбор_реплики_сейчас, реплика_без_адреса

    остались: list[ClientAddressCandidate] = []
    for r in годные:
        if (
            r.kind != address_parse.KIND_HOUSE
            or r.source == CANDIDATE_SOURCE_LLM
            or r.message_id is None
        ):
            остались.append(r)
            continue
        # Сообщения — секционированная таблица с составным ключом, `db.get`
        # по одному id их не находит. По `(id, message_at)` выборка идёт в одну
        # секцию (как у `voice.load_message`); без `message_at` (старые строки)
        # — обход всех секций по одному id.
        if r.message_at is not None:
            msg = await voice.load_message(db, r.message_id, r.message_at)
        else:
            msg = (
                await db.execute(sa.select(Message).where(Message.id == r.message_id).limit(1))
            ).scalar_one_or_none()
        if msg is None:
            остались.append(r)
            continue
        found = await разбор_реплики_сейчас(db, conv, msg, client_id=r.client_id)
        if not реплика_без_адреса(msg, found):
            остались.append(r)
            continue
        r.status = CANDIDATE_REJECTED
        r.resolved_at = datetime.now(UTC)
        r.resolved_by_id = None
        отклонённые.append(r.id)
        log.info(
            "geocode.autofill_rejected",
            conversation_id=str(conv.id),
            candidate_id=str(r.id),
            reason="speech_by_current_parse",
            level=r.level,
            geo_status=r.geo_status,
        )
    if отклонённые:
        await db.commit()
    return остались


@with_job_scope
async def autofill_address(ctx: dict[str, Any], conversation_id: uuid.UUID) -> str:
    """Записать годную строку (`candidate_grade`) в карточку — со всеми сторожами.

    Строка ещё `pending`, со степенью (точка дома, приблизительная точка или
    текст без точки), моложе суток — если карточка не пуста; из нескольких
    разных мест выбирает `_выбрать_из_нескольких` (ответ на вопрос об адресе
    > последний названный); заполненная карточка уступает только по лестнице
    `clients.auto_address_yields_to`; отказ помнится по месту (человек —
    целиком, автоматика — по степени); карточка не присоединена к другой.
    Запись — одним условным UPDATE: успел оператор раньше — `rowcount = 0`,
    и ничего не трогаем.

    Решение — в `_автозапись`, внутри сессии; кадры `client:updated` — здесь,
    после неё и после commit'а: один на диалог. Записали — `address_autofilled`;
    не записали, но сторож речи отклонил предложения (`_отсеять_речь` commit'ит
    их сам) — `address_candidate_rejected`: иначе экран держал бы снятое
    предложение до F5, а клик по нему давал 409 (ревью 19.09).
    """
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    отклонённые: list[uuid.UUID] = []
    async with factory() as db:
        итог, client_id, candidate_id = await _автозапись(db, conversation_id, отклонённые)
    if итог == "filled" and client_id is not None:
        await _известить(redis, client_id, conversation_id, reason="address_autofilled")
        log.info(
            "geocode.autofilled",
            conversation_id=str(conversation_id),
            candidate_id=str(candidate_id),
        )
    elif отклонённые and client_id is not None:
        await _известить(redis, client_id, conversation_id, reason="address_candidate_rejected")
    return итог


async def _автозапись(
    db: AsyncSession, conversation_id: uuid.UUID, отклонённые: list[uuid.UUID]
) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    """Итог автозаписи внутри сессии: `(исход, клиент, записанная строка)`.
    Исходы `disabled`/`gone`/`skip`/`raced`/`filled`; клиент известен со
    строки после его чтения — кадр после сессии адресуется ему."""
    if not await app_settings.get(db, app_settings.ADDRESS_DETECT_AUTOFILL):
        return "disabled", None, None
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        return "gone", None, None
    client = await db.get(Client, conv.client_id)
    if client is None or client.merged_into_id is not None:
        return "skip", None, None
    # Идентификатор — сразу: после `rollback` (исход `raced`) атрибуты объекта
    # протухают, и `client.id` пошёл бы в базу из синхронного места.
    client_id = client.id
    # Всё, что писал или подтверждал человек, не трогается — дальше решает
    # `clients.auto_address_yields_to`, когда строка-претендент выбрана.
    if client.address is not None and (
        client.address_set_at is not None or client.address_candidate_id is None
    ):
        return "skip", client_id, None
    заменяемое: uuid.UUID | None = None
    строки = list(
        (
            await db.execute(
                sa.select(ClientAddressCandidate).where(
                    ClientAddressCandidate.conversation_id == conversation_id
                )
            )
        )
        .scalars()
        .all()
    )
    свежо_после = (
        datetime.now(UTC) - AUTOFILL_MAX_AGE if client.address is not None else datetime.min
    ).replace(tzinfo=UTC)
    # ГОДНОСТЬ — ОДНИМ ПРЕДИКАТОМ (18.09): `candidate_grade` — точка дома,
    # приблизительная точка или текст без точки; «проверяется» и отказ
    # без текста — не годны. УРОВЕНЬ C ТОЖЕ, КОГДА КАРТА ПОДТВЕРДИЛА ДОМ
    # (владелец 13.09, Елец: «Тополиная д 14» назавтра после вопроса
    # ждало кнопки оператора). Замер 30 дней: 227 строк уровня C с exact —
    # сплошь настоящие улицы с домами в городе объявления. Исключение —
    # «дата, похожая на адрес» («Октября 7»): месяц улицей не считаем.
    # Политика правила — сегодняшняя, а не дня суда: понижение лестницей
    # действует и на решения, вынесенные этим правилом раньше
    # (`geocode.rule_writes_card`). Чтение нестрогое, как у суда.
    policy = geocode.parse_rule_policy(
        str(await app_settings.get(db, app_settings.ADDRESS_GEO_RULE_POLICY) or ""), strict=False
    )
    годные = [
        r
        for r in строки
        if r.status == CANDIDATE_PENDING
        and candidate_grade(r) is not None
        and geocode.rule_writes_card(policy, candidate_rule(r))
        and (r.level in ("A", "B") or not _похоже_на_дату(r.street))
        and _aware(r.detected_at) >= свежо_после
    ]
    # ХВОСТ СТАРОГО РАЗБОРА В КАРТОЧКУ НЕ ИДЁТ (владелец 19.09: «Камера
    # 4G» — старый разбор дал дом «4G», карта поставила точку улицы, и
    # строка легла в карточку автозаписью). Строка хранит разбор своего
    # дня; судим её реплику сегодняшним — одним предикатом с догоном
    # `address-reparse` (`inbound.реплика_без_адреса`). Речь — в
    # отклонённые с журналом, чтобы не возвращалась следующим прогоном.
    годные = await _отсеять_речь(db, conv, годные, отклонённые)
    # Дом важнее места: есть годный дом — место больше не кандидат.
    дома = [r for r in годные if r.kind != address_parse.KIND_PLACE]
    if дома:
        годные = дома
    elif client.address is not None:
        return "skip", client_id, None  # место поверх места не пишем
    # Две строки одного места («ул. Ленина 5» и «Ленина 5» до 13.09) — один
    # адрес: берём выше по степени и раннюю (ревью 13.09).
    годные = _по_местам(годные)
    if not годные:
        log.info("geocode.autofill_skipped", conversation_id=str(conversation_id), exact=0)
        return "skip", client_id, None
    if len(годные) == 1:
        строка = годные[0]
    else:
        # ДВА АДРЕСА В ОДНОМ ДИАЛОГЕ — выбор без человека (владелец 18.09).
        выбранная, почему = await _выбрать_из_нескольких(db, conv, годные)
        if выбранная is None:
            log.info(
                "geocode.autofill_skipped",
                conversation_id=str(conversation_id),
                reason=почему,
                candidates=len(годные),
            )
            return "skip", client_id, None
        строка = выбранная
        log.info(
            "geocode.autofill_picked",
            conversation_id=str(conversation_id),
            candidate_id=str(строка.id),
            reason=почему,
            of=len(годные),
        )
    # АДРЕС ПОСТЕПЕННО (13.09) И АВТОМАТИКА ИСПРАВЛЯЕТ СЕБЯ (18.09): место
    # уступает дому, строка того же сообщения — своему новому разбору,
    # то же место — строго высшей степени (text → approx → exact).
    # Правило целиком — в `clients.auto_address_yields_to`; здесь только
    # ответ «кому уступить».
    if client.address is not None:
        прежняя, причина = await auto_address_yields_to(db, client, строка)
        if прежняя is None or причина is None:
            return "skip", client_id, None
        заменяемое = прежняя.id
        log.info(
            "geocode.autofill_replaced",
            conversation_id=str(conversation_id),
            candidate_id=str(строка.id),
            previous_id=str(прежняя.id),
            reason=причина,
        )
    # ПУНКТ, НАЗВАННЫЙ КЛИЕНТОМ ДЛЯ ТОЙ ЖЕ УЛИЦЫ, ДЕРЖИТ АВТОЗАПИСЬ
    # (владелец 16.09, Псков: «Деревня ольхово улица кленовая дом
    # 8» — строка с деревней отказана картой, строка модели без деревни
    # подтверждена в Пскове и легла в карточку: точка неверная). Дом в
    # городе объявления, когда клиент назвал деревню, — не тот дом:
    # решает оператор, не автоматика.
    ядро = address_parse.street_core(строка.street)
    if (
        строка.settlement is None
        and any(
            r.id != строка.id
            and r.kind == address_parse.KIND_HOUSE
            and r.status != CANDIDATE_REJECTED
            and r.settlement
            # Улица той строки — та же или с пунктом впереди («Деревня
            # ольхово улица кленовая» у старого разбора).
            and address_parse.street_core(r.street)[-len(ядро) :] == ядро
            for r in строки
        )
    ):
        log.info(
            "geocode.autofill_skipped",
            conversation_id=str(conversation_id),
            reason="settlement_named_for_street",
        )
        return "skip", client_id, None
    # ⚠ ОТКАЗ ПОМНИТСЯ ПО МЕСТУ, А НЕ ПО БУКВАМ. «ул звенигородская 1» стёрли,
    # клиент пишет «Звенигородская 1» — другой ключ, другая строка, тот же
    # дом. Текст строки карты тоже разойдётся (без «п» нет и «посёлок»).
    # Единственное, что у двух написаний одного дома совпадает, —
    # координаты; сравниваем их с точностью до ~10 метров; стёртое «31»
    # запирает и «31 А» той же семьи (ревью 15.09: для показа они одно
    # место, для памяти — тоже).
    # Отказы — по ВСЕЙ карточке, а не по одному диалогу: стёртый вчера дом
    # не должен возвращаться из сегодняшнего разговора того же человека.
    # ЧЕЛОВЕК — ЦЕЛИКОМ, АВТОМАТИКА — ПО СТЕПЕНИ (владелец 18.09: «правки
    # руками автоматика не трогает никогда»): стёр или нажал «Не адрес»
    # (`resolved_by_id`) — «это не его адрес», место закрыто любой
    # степенью; отказала автоматика (замена при уступке) — та же или
    # низшая степень того же места не пишется, строго высшая — пишется:
    # стёртый приблизительный не запирает точный дом.
    все_строки = await address_candidates(db, client.id)
    степень = candidate_grade(строка)
    assert степень is not None  # строка прошла фильтр
    for r in все_строки:
        if r.status != CANDIDATE_REJECTED:
            continue
        то_же_место = (
            строка.value == r.value
            or (_место(строка) is not None and _место(строка) == _место(r))
            or одно_место(строка, r)
        )
        if not то_же_место:
            continue
        if r.resolved_by_id is not None:
            log.info(
                "geocode.autofill_skipped",
                conversation_id=str(conversation_id),
                reason="rejected_by_human",
                grade=степень,
            )
            return "skip", client_id, None
        # Отклонённая без степени (отказ без текста, вердикт сброшен) —
        # степень неизвестна: считаем место закрытым, а не открытым.
        степень_отказа = candidate_grade(r)
        if степень_отказа is None or not grade_beats(степень, степень_отказа):
            log.info(
                "geocode.autofill_skipped",
                conversation_id=str(conversation_id),
                reason="rejected_same_place",
                grade=степень,
                rejected_grade=степень_отказа,
            )
            return "skip", client_id, None

    # Условная запись: база, а не порядок чтения, решает гонку с человеком.
    текст = candidate_address_text(строка, parts={ч: getattr(строка, ч) for ч in _ЧАСТИ_АДРЕСА})
    result = await db.execute(
        sa.update(Client)
        .where(
            Client.id == client.id,
            (
                Client.address_candidate_id == заменяемое
                if заменяемое is not None
                else Client.address.is_(None)
            ),
            Client.merged_into_id.is_(None),
        )
        .values(
            address=текст,
            address_candidate_id=строка.id,
            address_conversation_id=conversation_id,
            address_set_by_id=None,
            address_set_at=None,
        )
    )
    if not getattr(result, "rowcount", 0):
        await db.rollback()
        return "raced", client_id, None
    # Журнал и связь — тем же сборщиком, что у кнопки «Подтвердить»: текст
    # он соберёт тот же (строка та же), а строку журнала напишет с
    # источником `geocoder` и без человека. «Было пусто» передаём явно:
    # ORM синхронизирует объект после UPDATE, и без этого журнал сказал бы
    # «исправлен» о первом заполнении (поймано тестом 11.09).
    await apply_candidate_to_card(
        db, client, строка, actor_id=None, source="geocoder", previous=None
    )
    строка.status = CANDIDATE_ACCEPTED
    строка.resolved_at = datetime.now(UTC)
    строка.resolved_by_id = None
    if заменяемое is not None:
        # Прежняя строка автоматики — в отказанные без человека: отказ по
        # месту тогда не даст ей вернуться следующим прогоном.
        прежняя_строка = await db.get(ClientAddressCandidate, заменяемое)
        if прежняя_строка is not None and прежняя_строка.kind != address_parse.KIND_PLACE:
            # Части принадлежат адресу, а не разбору: «кв 5» у «17» едет с
            # карточкой на «17 А» (и на поправку той же реплики), иначе
            # следующая пересборка (`_части_места` отклонённые не читает)
            # её потеряла бы (ревью 18.09).
            for ч in _ЧАСТИ_АДРЕСА:
                if getattr(строка, ч) is None and getattr(прежняя_строка, ч) is not None:
                    setattr(строка, ч, getattr(прежняя_строка, ч))
            прежняя_строка.status = CANDIDATE_REJECTED
            прежняя_строка.resolved_at = datetime.now(UTC)
            прежняя_строка.resolved_by_id = None
    await db.commit()
    return "filled", client_id, строка.id
