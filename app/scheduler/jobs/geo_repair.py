"""Досчёт проверок адреса по карте: что не поставилось и что не ответило.

ЗАЧЕМ. Проверку строки по карте ставит ровно один путь — приём входящего,
после commit'а. Он не срабатывает второй раз никогда. Значит без этого прогона
навсегда остались бы без вердикта: строки, записанные до 11.09 (миграция
пометила их `pending`); строки, чью постановку отбросил Redis; строки,
упёршиеся в сеть на третьей попытке (`error`); и всё, что пришло догрузкой
истории — её конвейер задачи не ставит намеренно, чтобы не устроить залп в
чужую карту.

ГРАНИЦЫ, КАЖДАЯ — ПРОТИВ СВОЕГО ВРЕДА:

* ТЕМП — :data:`ПОРЦИЯ` строк раз в :data:`ИНТЕРВАЛ`, задачи ставятся с
  шагом :data:`ШАГ_СЕК`. Политика Nominatim: не чаще запроса в секунду и без
  «тяжёлого использования»; починка идёт медленнее живого потока, а не рядом
  с ним. Хвост из 25 строк добирается за один заход, из тысячи — за сутки.
* ПОПЫТКИ КОНЕЧНЫ — `geo_attempts` < :data:`ПОТОЛОК_ПОПЫТОК`, а между
  попытками пауза растёт вдвое (:func:`_пора`). Строка, по которой карта
  третий раз молчит, ждёт час, потом два — и после потолка не берётся вовсе:
  повторять до бесконечности значит рано или поздно получить `blocked` на
  весь сервер.
* `blocked` ПЕРЕСПРАШИВАЕТСЯ НЕ РАНЬШЕ ЧЕМ ЧЕРЕЗ ШЕСТЬ ЧАСОВ (:data:`BLOCKED_ПАУЗА`)
  и с тем же потолком попыток: бан не вечен, а смена провайдера в настройках
  сбрасывает статус в `pending` сразу.
* ВЫКЛЮЧАТЕЛЬ — `address_geo.enabled`: выключено — прогон молчит.

ПОРЯДОК — НОВЫЕ ВПЕРЁД: по ним прямо сейчас работает человек, а вчерашней
строке спешить некуда (автозапись её всё равно не возьмёт — старше суток).

ПОВТОРНАЯ ПРИВЯЗКА (N13, 19.09) — этот же прогон, без новой очереди и команды:
строки с пересматриваемым вердиктом (`geocode.RECHECK_STATUSES`), судимые без
DaData или прежней версией судьи, возвращаются в `pending` в свободный остаток
порции и встают в очередь в том же заходе (:func:`перепривязать`). Моложе
:data:`REQUEUE_MIN_AGE` не берутся — окно вопроса клиенту. Возврат DaData
ждёт, пока она лежит по сети (ключ воркера `geo:dadata:down`): иначе обход
гонял бы одни и те же строки в лежащую карту каждые десять минут.

КВОТА ЯНДЕКСА И СЛЕПОЙ СУД (пакет 5, 20.09). Строка со снимком улик
(`geo_prev`, «в пересуде, ждёт полного набора карт») ставится в очередь только
в остаток доли Яндекса на сегодня (:func:`квота_яндекса`): доля из 300 при
порции 300 иначе выедалась бы первой сотней строк, а остальные судились бы
вслепую (ревью 20.09). Триггер `kept_blind` возвращает строки, судимые без
части карт, когда доля снова есть, — не чаще раза в :data:`KEPT_BLIND_PAUSE`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.integrations import yandex_geocoder
from app.models import ClientAddressCandidate
from app.models.client import CANDIDATE_PENDING
from app.services import app_settings, clients, geocode
from app.services.geocode_queue import enqueue_geocode
from app.workers.geocode import (
    ORIGIN_REPAIR,
    _день_яндекса,
    dadata_calls_key,
    dadata_calls_today,
    dadata_down,
    dadata_exhausted,
    dadata_настроена,
    suggest_calls_key,
    yandex_calls_key,
    yandex_calls_today,
    доля_починки,
)

log = structlog.get_logger("app.geo_repair")

#: 80, а не 40 (13.09): догон карточек по 60 дням переписки поставил в очередь
#: 6,5 тыс. строк — по 40 в десять минут это трое суток. DaData отвечает
#: за 0,1 с и держит 30 в секунду; OSM ходит под своим замком темпа.
#: 200 по 3 с (13.09): очередь 4,8 тыс. при 80/10 мин — десять часов; DaData
#: держит 30 в секунду, у OSM свой замок темпа внутри задачи.
ПОРЦИЯ = 300
ШАГ_СЕК = 2
#: Столько задач в общей очереди — порцию не кладём (живые реплики вперёд).
ЗАТОР = 60
ИНТЕРВАЛ = timedelta(minutes=10)
#: Попытка = жизнь одной задачи, кончившейся отказом (не каждый HTTP). Восемь
#: с удвоением паузы — около суток недоступности карты, прежде чем строку
#: оставят в покое.
ПОТОЛОК_ПОПЫТОК = 8
#: Обход N13 — МОЛОЖЕ НЕ БЕРЁМ: окно вопроса клиенту. `address_ask.candidate_lock`
#: судит ВСЕ строки диалога за `address_ask.ОКНО` (24 ч по detected_at) и при
#: `pending` у строки A/B ждёт `RETRY_MAX`×`RETRY_DEFER_SEC` (workers/address_ask)
#: и выходит skip — вопрос не задаётся. Вопрос ставится с задержкой до
#: `ADDRESS_ASK_DELAY_MAX_SEC` (3600) и живёт ещё `MAX_AGE` (1 ч):
#: 24 ч + 1 ч + 1 ч + 3 мин < 27 ч. Число стережёт
#: test_paket2_requeue_1909::test_min_age_перекрывает_окно_вопроса — из констант.
REQUEUE_MIN_AGE = timedelta(hours=27)
#: СТАРШЕ НЕ БЕРЁМ: замер стенда — 30 дней; пустую карточку любой давности
#: заполняет отдельная команда (`backfill-cards`).
REQUEUE_MAX_AGE = timedelta(days=30)
#: `blocked` — «вас не пускают»; переспрашиваем не раньше чем через шесть часов
#: и с тем же потолком попыток: бан не вечен, а смена провайдера сбрасывает
#: статус сразу (ручка настроек).
BLOCKED_ПАУЗА = timedelta(hours=6)
#: Пауза после n-й неудачи: 10 мин, 20, 40, 80, 160 — растёт вдвое.
БАЗОВАЯ_ПАУЗА = timedelta(minutes=10)
#: Сколько походов к Яндексу стоит строка в пересуде (пакет 5): вторая карта,
#: область, уточнение точки, подсказки — по замеру кода 2–4. Квота строк на
#: заход — остаток доли, делённый на это число.
ПОХОДОВ_ЯНДЕКСА_НА_СТРОКУ = 3
#: Слепо судимая строка (снимок с `missing`) берётся снова не раньше чем через
#: 20 ч: московские сутки доли сменятся, а одна строка не крутится в пределах
#: одной выбранной доли. Вместе с `ПОТОЛОК_ПОПЫТОК` — не дольше восьми суток.
KEPT_BLIND_PAUSE = timedelta(hours=20)
#: `pending` без единой попытки берём сразу; `pending` с попытками (сетевой
#: `Retry` пометил `error`, потом снова `pending` — редкость) ждёт паузу.
DEFAULTS: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
JOB_ID = "geo_repair"
ПЕРВЫЙ_ЗАХОД = timedelta(minutes=3)


async def repair_geocodes() -> int:
    """Точка входа планировщика. Возвращает число задач, ВСТАВШИХ в очередь."""
    redis = redis_mod.get_client()
    # DaData на сегодня выбрана (13.09: 403 на 8 904, потом наш потолок к
    # половине одиннадцатого) — хвост не гоним через OSM с окончательными
    # отказами, ждём московской полуночи; живые реплики идут своим путём. И не
    # подкладываем порцию, пока очередь не рассосалась: живая реплика не
    # должна ждать за тремя сотнями строк догона.
    try:
        в_очереди = int(await redis.zcard("arq:queue"))
    except Exception:  # noqa: BLE001
        в_очереди = 0
    if в_очереди > ЗАТОР:
        log.info("geo.repair", skipped="queue_busy", queued=в_очереди)
        return 0
    async with db_mod.session_scope() as db:
        if not await app_settings.get(db, app_settings.ADDRESS_GEO_ENABLED):
            return 0
        # ЗАПАС ЖИВЫМ (13.09): починка выела DaData к утру, и до полуночи новые
        # реплики шли по одному OSM. Хвосту — потолок минус запас; сверх него
        # починка молчит, пока не наступят новые московские сутки. Флаг 403 и
        # доля смотрятся только при настроенной DaData: выключил владелец
        # DaData — хвост идёт по OSM, как и живые реплики (ревью 13.09).
        потолок = await app_settings.get(db, app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT)
        dadata_включена = dadata_настроена(
            bool(await app_settings.get(db, app_settings.ADDRESS_GEO_DADATA_ENABLED)), потолок
        )
        if dadata_включена and await dadata_exhausted(redis):
            log.info("geo.repair", skipped="dadata_exhausted")
            return 0
        # Порция — не больше, чем осталось до доли: строка стоит до двух
        # запросов DaData, и порция из трёхсот, поставленная у самой доли,
        # иначе шла бы по OSM с отложенными строками впустую (ревью 13.09).
        порция = ПОРЦИЯ
        if dadata_включена and потолок is not None:
            израсходовано = await dadata_calls_today(redis)
            доля = доля_починки(потолок, "dadata")
            if доля is not None and израсходовано >= доля:
                log.info(
                    "geo.repair",
                    skipped="dadata_reserved_for_live",
                    used=израсходовано,
                    repair_share=доля,
                )
                return 0
            if доля is not None:
                порция = max(1, min(ПОРЦИЯ, (доля - израсходовано) // 2))
        # КВОТА ЯНДЕКСА (пакет 5): строки со снимком — только в остаток доли.
        потолок_яндекса = await app_settings.get(db, app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT)
        доля_яндекса_pct = app_settings.repair_share_pct(
            await app_settings.get(db, app_settings.ADDRESS_GEO_REPAIR_SHARE_YANDEX)
        )
        расход_яндекса = await yandex_calls_today(redis)
        квота = квота_яндекса(потолок_яндекса, расход_яндекса, yandex_pct=доля_яндекса_pct)
        await _суточные_счётчики(redis, share=доля_яндекса_pct)
        строки = await найти_непроверенные(db)
        now = datetime.now(UTC)
        к_постановке = []
        для_квоты = 0
        for s in строки:
            if not _пора(s[1], s[2], now, статус=s[3]):
                continue
            if s[4]:
                # Строка со снимком: ей нужен Яндекс — в квоту, иначе ждёт.
                if квота is not None and для_квоты >= квота:
                    continue
                для_квоты += 1
            к_постановке.append(s[:4])
        # ПОВТОРНАЯ ПРИВЯЗКА (N13, 19.09): два триггера, один исполнитель — эта
        # же починка. Сбрасываем ТОЛЬКО в свободный остаток порции: сброшенная
        # строка встаёт в очередь в этом же заходе, а не лежит `pending` без
        # вариантов до полуночи (ревью 19.09). Живой хвост заполнил порцию —
        # обход ждёт следующего захода. Возврат DaData — только когда она в
        # деле прямо сейчас (ранние выходы выше отсеяли потолок и долю); смена
        # версии судьи — всегда: судимые прежней версией (или до колонки —
        # NULL) пересматриваются один раз, повторно судимые получают текущую
        # версию и в обход не попадают. `pending` не в RECHECK_STATUSES —
        # строка, сброшенная первым триггером, второму не достаётся.
        свободно = порция - len(к_постановке)
        триггеры: list[tuple[str, Any]] = []
        квота_на_сброс = None if квота is None else max(0, квота - для_квоты)
        # DaData ЛЕЖИТ ПО СЕТИ (ключ воркера `geo:dadata:down`, ревью 19.09 C2) —
        # возврата нет, и обход `dadata_back` не включаем: иначе каждый заход
        # снова сбрасывал бы те же строки, воркер судил бы их без DaData и
        # записывал с тем же флагом. Потолок и 403 отсеяны ранними выходами
        # выше; здесь — сеть, 5xx, не тот JSON, у которых суточного флага нет.
        # Смена версии судьи от DaData не зависит и идёт своим чередом.
        if dadata_включена and await dadata_down(redis):
            log.info("geo.repair", paused="dadata_back", reason="dadata_down")
        elif dadata_включена:
            триггеры.append(("dadata_back", ClientAddressCandidate.geo_without_dadata.is_(True)))
        триггеры.append(
            (
                "verdict_version",
                ClientAddressCandidate.geo_verdict_version.is_distinct_from(
                    geocode.VERDICT_VERSION
                ),
            )
        )
        # СЛЕПО СУДИМЫЕ (пакет 5): снимок с `missing` остался в строке — часть
        # карт ей отказала. Берём, когда доля Яндекса есть (квота > 0; Яндекс
        # не настроен — нехватка была только по DaData, и ранние выходы уже
        # гарантируют, что она в деле), не чаще раза в `KEPT_BLIND_PAUSE`.
        # Ключ есть, а доля починке 0 (квота `None` при настроенном Яндексе,
        # C2) — пауза: Яндекс к таким строкам не вернётся, пока владелец не
        # даст долю, и каждые 20 ч был бы тот же слепой суд.
        if квота_на_сброс is None and yandex_geocoder.enabled():
            log.info("geo.repair", paused="kept_blind", reason="yandex_share_zero")
        elif квота_на_сброс is None or квота_на_сброс > 0:
            триггеры.append(
                (
                    "kept_blind",
                    sa.and_(
                        ClientAddressCandidate.geo_prev.is_not(None),
                        ClientAddressCandidate.geo_checked_at < now - KEPT_BLIND_PAUSE,
                    ),
                )
            )
        else:
            log.info("geo.repair", paused="kept_blind", reason="yandex_share_used")
        возвращено = 0
        for reason, условие in триггеры:
            # Сбросы — в остаток порции И в остаток квоты Яндекса: сброшенная
            # строка получает снимок и судится в этом же заходе.
            предел = свободно if квота_на_сброс is None else min(свободно, квота_на_сброс)
            ids = await перепривязать(db, reason=reason, условие=условие, limit=предел)
            к_постановке += [(cid, 1, None, geocode.GEO_PENDING) for cid in ids]
            свободно -= len(ids)
            if квота_на_сброс is not None:
                квота_на_сброс -= len(ids)
            возвращено += len(ids)
        if возвращено:
            await db.commit()  # session_scope сам не коммитит

    поставлено = 0
    for candidate_id, *_ in к_постановке:
        # Своё имя задачи (`:repair`): живая постановка по той же строке не
        # гасится дедупом об отложенную на минуты задачу починки и идёт с
        # полными потолками; двойной прогон безопасен — второй выйдет по
        # «уже решено» или условной записи (ревью 13.09).
        if await enqueue_geocode(
            redis,
            candidate_id,
            defer_sec=поставлено * ШАГ_СЕК or None,
            suffix="repair",
            origin=ORIGIN_REPAIR,
        ):
            поставлено += 1
        if поставлено >= порция:
            break
    if строки or возвращено:
        log.info("geo.repair", found=len(строки), enqueued=поставлено, requeued=возвращено)
    return поставлено


def квота_яндекса(
    потолок: int | None,
    расход: int,
    *,
    yandex_pct: int = app_settings.REPAIR_SHARE_YANDEX_DEFAULT,
) -> int | None:
    """Сколько строк в пересуде можно поставить в заход, чтобы им хватило доли
    Яндекса на сегодня: `(доля − расход) // ПОХОДОВ_ЯНДЕКСА_НА_СТРОКУ`, не меньше
    нуля. `None` — Яндексу в починке делать нечего: ключа нет ИЛИ доля меньше
    походов одной строки (`repair_share_yandex=0`, потолок 0 — или потолок в
    единицы, где 30 % не дают и трёх походов). Квота — это «подождать долю»;
    когда доли нет по настройке, ждать нечего, и квота 0 «навсегда» запирала
    бы строки со снимком в `pending`, а триггеры сброса — пределом 0 (ревью
    20.09, C2). Без квоты такие строки судятся DaData и OSM: воркер при
    потолке 0 ставит отказ Яндекса и удерживает прежние улики."""
    if not yandex_geocoder.enabled():
        return None
    доля = доля_починки(потолок, "yandex", yandex_pct=yandex_pct)
    assert доля is not None  # у Яндекса доля всегда число (None → база 1000)
    if доля < ПОХОДОВ_ЯНДЕКСА_НА_СТРОКУ:
        return None
    return max(0, (доля - int(расход)) // ПОХОДОВ_ЯНДЕКСА_НА_СТРОКУ)


async def _суточные_счётчики(redis: Any, *, share: int) -> None:
    """Раз в московские сутки — расход карт ЗА ВЧЕРА в журнал (ревью 20.09,
    п. 14): без этой строки долю Яндекса не по чему настраивать. Именно за
    вчера (C3): строка пишется первым заходом после полуночи, когда
    сегодняшних ключей ещё нет — журнал получал бы нули, а итоги дня не
    попадали бы в него никогда; вчерашние ключи живут ещё сутки (`_занять`:
    `EX` двое суток). Замок `SET NX` — тот же приём, что у `_тревога_потолка`."""
    вчера = datetime.now(UTC) - timedelta(days=1)
    день = _день_яндекса(вчера)
    ключ = f"geo:daily_counters_logged:{день}"
    try:
        if not await redis.set(ключ, 1, nx=True, ex=2 * 24 * 3600):
            return
        dadata, yandex, suggest = [
            int(await redis.get(k) or 0)
            for k in (dadata_calls_key(вчера), yandex_calls_key(вчера), suggest_calls_key(вчера))
        ]
    except Exception as exc:  # noqa: BLE001
        # Замок мог встать, а чтение сорваться: строка за этот день потеряна —
        # об этом и предупреждаем, с днём.
        log.warning("geocode.counter_failed", error=type(exc).__name__, day=день)
        return
    log.info(
        "geocode.daily_counters",
        day=день,
        dadata=dadata,
        yandex=yandex,
        suggest=suggest,
        repair_share_yandex=share,
    )


async def перепривязать(db: Any, *, reason: str, условие: Any, limit: int) -> list[uuid.UUID]:
    """Вернуть в очередь строки с пересматриваемым вердиктом по одному триггеру
    (N13) — не больше `limit`, новые вперёд. ПОСТРОЧНО с оптимистическим
    условием (пакет 5): снимок улик строится в Python из тех же колонок одним
    строителем (`clients.снимок_улик`) — SQL-двойник был бы второй формой
    снимка; `geo_checked_at IS NOT DISTINCT FROM :seen` вместе с `общие`
    держит прежнюю гарантию: решение оператора или запись воркера между
    выбором и записью дают `rowcount 0`, строка пропускается и берётся
    следующим заходом. Попытки сохраняются (`с_попытками=True`): у строки
    уже был вердикт. Возвращает id сброшенных — вызывающий ставит их в
    очередь в этом же заходе."""
    if limit <= 0:
        return []
    now = datetime.now(UTC)
    общие = (
        ClientAddressCandidate.geo_status.in_(tuple(geocode.RECHECK_STATUSES)),
        ClientAddressCandidate.status == CANDIDATE_PENDING,
        ClientAddressCandidate.resolved_by_id.is_(None),
        ClientAddressCandidate.detected_at <= now - REQUEUE_MIN_AGE,
        ClientAddressCandidate.detected_at >= now - REQUEUE_MAX_AGE,
        ClientAddressCandidate.geo_attempts < ПОТОЛОК_ПОПЫТОК,
        условие,
    )
    строки = (
        (
            await db.execute(
                sa.select(ClientAddressCandidate)
                .where(*общие)
                .order_by(ClientAddressCandidate.detected_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    ids: list[uuid.UUID] = []
    for row in строки:
        снимок = clients.снимок_улик(row, reason=reason)
        result = await db.execute(
            sa.update(ClientAddressCandidate)
            .where(
                ClientAddressCandidate.id == row.id,
                *общие,  # общие повторены намеренно: строка могла измениться
                ClientAddressCandidate.geo_checked_at.is_not_distinct_from(row.geo_checked_at),
            )
            .values(**clients.сброс_вердикта(с_попытками=True, улики=снимок))
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0)) == 1:
            ids.append(row.id)
    if ids:
        log.info("geocode.requeue", reason=reason, rows=len(ids))
    return ids


async def найти_непроверенные(
    db: Any,
) -> list[tuple[Any, int, datetime | None, str | None, bool]]:
    """`pending`, `error` и `blocked` с запасом попыток — новые вперёд, не больше порции.

    NULL считается `pending`: колонка со значением по умолчанию, но строку
    старее правки или вставленную в окне выкатки терять нельзя. Пятым —
    признак снимка улик (`geo_prev IS NOT NULL`): такая строка ставится в
    квоту Яндекса (пакет 5).
    """
    rows = await db.execute(
        sa.select(
            ClientAddressCandidate.id,
            ClientAddressCandidate.geo_attempts,
            ClientAddressCandidate.geo_checked_at,
            ClientAddressCandidate.geo_status,
            ClientAddressCandidate.geo_prev.is_not(None),
        )
        .where(
            sa.or_(
                ClientAddressCandidate.geo_status.in_(
                    (
                        geocode.GEO_PENDING,
                        geocode.GEO_ERROR,
                        geocode.GEO_BLOCKED,
                        # «Город неизвестен» — не приговор: объявление
                        # дотягивается обогащением позже распознавания (12.09).
                        geocode.GEO_NO_CITY,
                    )
                ),
                ClientAddressCandidate.geo_status.is_(None),
            ),
            ClientAddressCandidate.geo_attempts < ПОТОЛОК_ПОПЫТОК,
        )
        .order_by(ClientAddressCandidate.detected_at.desc())
        .limit(ПОРЦИЯ * 3)
    )
    return [
        (r.id, int(r.geo_attempts or 0), r.geo_checked_at, r.geo_status, bool(r[4])) for r in rows
    ]


def _пора(
    попыток: int, когда: datetime | None, now: datetime, *, статус: str | None = None
) -> bool:
    """Пауза между попытками растёт вдвое; первая попытка — сразу; бан ждёт часы."""
    if попыток <= 0 or когда is None:
        return True
    if когда.tzinfo is None:  # SQLite в проверках отдаёт наивное время
        когда = когда.replace(tzinfo=UTC)
    пауза = БАЗОВАЯ_ПАУЗА * (2 ** (попыток - 1))
    if статус == geocode.GEO_BLOCKED:
        пауза = max(пауза, BLOCKED_ПАУЗА)
    return now - когда >= пауза


def register(scheduler: Any) -> None:
    scheduler.add_job(
        repair_geocodes,
        IntervalTrigger(
            seconds=int(ИНТЕРВАЛ.total_seconds()),
            start_date=datetime.now(UTC) + ПЕРВЫЙ_ЗАХОД,
        ),
        id=JOB_ID,
        **DEFAULTS,
    )
