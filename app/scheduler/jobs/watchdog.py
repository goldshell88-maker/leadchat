"""Сторожевые проверки — источник системных уведомлений (14 §2.1).

Каждая проверка отвечает на вопрос «что человек увидел бы сам, если бы
смотрел», и порождает ОДНО уведомление вида из каталога центра
(``app/services/notifications.py``, KINDS) со своим ключом подавления:

===========================  ========  ===============  ======================
проверка                     важность  вид события      ключ подавления
===========================  ========  ===============  ======================
канал отобрали               критично  webhook.lost     webhook.lost
канал замолчал — сверка      критично  webhook.lost     webhook.lost
канал не может отвечать      критично  account.needs_reauth  по каналу
резервной копии нет          критично  backup.failed    backup.missing
приём сообщений остановился  важно     inbound.stalled  inbound.stalled
очередь не разбирается       важно     queue.backlog    queue.backlog
диск заполнен                важно     disk.space       disk.space
отметка живости пропала      важно     scheduler.down   scheduler.heartbeat_broken
сертификат истекает          обычно    cert.expiring    cert.expiring:{дата}
===========================  ========  ===============  ======================

«Приём сообщений остановился» стоит в «важно», а не в «критично», с 12 августа:
критичной эта тревога 112 раз за шесть дней поднимала красную плашку поверх
экрана и вытеснила из колокольчика два настоящих «Резервное копирование не
выполнилось» — за 6 и 8 августа, копий за те ночи нет. Подробности и порог —
в :func:`check_inbound_stalled` и :data:`MIN_INBOUND_STALL_MINUTES`.

«Канал замолчал — сверка» заведена 12 августа и стоит в БЫСТРОМ наборе, хотя
ходит в Авито. Противоречия здесь нет: она спрашивает Авито не про все каналы
подряд, а только про тот, чья рабочая пауза превысила ОБЫЧНЫЙ ритм этого же
канала (:func:`check_quiet_channel_subscription`), и не чаще раза в полчаса.
Смысл — не ждать утреннего обхода в единственном случае, когда ждать нельзя:
канал замолчал прямо сейчас. Подписка на месте — проверка молчит, и это её
главное свойство: тишина сама по себе больше никого не обвиняет.

Виды берутся из чужого каталога намеренно: он решает, какая у события иконка
и какая кнопка. Свои названия дали бы событие без иконки и без действия.

Ключ стабилен между прогонами НАМЕРЕННО. Проверки бегут раз в 5 минут, и без
стабильного ключа за ночь набежало бы 140 одинаковых строк. Центр уведомлений
(14 §4) схлопывает их в одну запись со счётчиком «повторялось N раз» — поэтому
подавлять повторы здесь, у источника, нельзя: локальное «не отправлять»
съело бы и счётчик тоже.

ЧЕСТНО О ГЛАВНОМ ОГРАНИЧЕНИИ (14 §5). Эти проверки живут ВНУТРИ планировщика,
поэтому «планировщик мёртв» они поймать не могут в принципе: мёртвый процесс
не запускает собственную проверку. :func:`check_scheduler_heartbeat` ловит
другое и более узкое — что отметка живости ``scheduler:alive`` пропала или
протухла, ХОТЯ ПЛАНИРОВЩИК ЖИВ (потеря Redis, рассинхрон часов, зависший
job-цикл). На этой отметке стоит docker healthcheck и SM-7, и её пропажа
означает, что настоящее зависание от этой поломки снаружи уже не отличить.
Автоперезапуска по healthcheck нет: docker помечает контейнер нездоровым, а
перезапускает только вышедший процесс (restart: unless-stopped).

Настоящую смерть планировщика видит только наблюдатель снаружи:
docker healthcheck (05 §2.2), ``/api/health/deep`` при
``HEALTH_DEEP_CHECK_SCHEDULER=true`` и внешняя проверка со второго сервера —
последняя сообщает об этом через ``POST /internal/notify`` с видом
``scheduler.dead`` (см. ``app/api/routes/internal.py``).
"""

from __future__ import annotations

import os
import pathlib
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_mod
from app.core.config import settings
from app.db import session as db_mod
from app.integrations.avito.client import AVITO_DOWN_KEY
from app.models import AvitoAccount, Message, WebhookRawLog
from app.models.leadbot import OUTCOME_UNAVAILABLE, LeadbotCall
from app.services import notifications
from app.services.audit import MSK  # все бизнес-определения времени — по Москве
from app.services.support import CRITICAL, INFO, WARNING, NotificationDraft, send_notification

log = structlog.get_logger("app.watchdog")

# --- виды событий: строки каталога центра (14 §2.1) --------------------------
KIND_INBOUND_STALLED = "inbound.stalled"
KIND_QUEUE_BACKLOG = "queue.backlog"
KIND_DISK_SPACE = "disk.space"
KIND_CERT_EXPIRING = "cert.expiring"
KIND_SCHEDULER_DOWN = "scheduler.down"
KIND_BACKUP_FAILED = "backup.failed"
KIND_INBOUND_UNPARSED = "inbound.unparsed"
KIND_WEBHOOK_LOST = "webhook.lost"
#: Авито не отвечает: сетевые отказы копятся счётчиком в клиенте Авито.
KIND_AVITO_DOWN = "avito.unreachable"
#: Клиенты пишут в канал, который выключен: их сообщения принимаются и
#: выбрасываются, и до этой проверки об этом не знал никто.
KIND_DROPPED_DISABLED = "inbound.dropped_disabled"
#: Ответы не уходят клиентам: вид был объявлен в каталоге с 11 августа и не
#: создавался ничем (аудит 19.08, находка L-012).
KIND_DELIVERY_FAILURES = "delivery.failures"
KIND_NEEDS_REAUTH = "account.needs_reauth"
#: Лид-бот отвечает отказом подряд: своя сеть, шлюз модели, кончились деньги на
#: ключе, туннель между серверами. Диалоги при этом не теряются (движок уводит
#: их человеку), но НИКТО ОБ ЭТОМ НЕ УЗНАВАЛ — бот молчал сутками, а команда
#: думала, что он работает (аудит устойчивости 18.08).
KIND_LEADBOT_DOWN = "leadbot.down"
#: Шлюз внешних сервисов (docs/46, Амстердам) молчит: карта, справочники и
#: модель-читатель адресов ходят только через него. 22.09 он молчал 12 минут, и
#: видно это было одной строкой в журнале скрипта на хосте (проверка 24.09).
KIND_GATEWAY_DOWN = "gateway.down"
GATEWAY_DOWN_KEY = "watchdog:gateway:down_in_row"
#: Сколько проверок подряд (по пять минут) шлюз молчит, прежде чем звать людей:
#: мост между странами моргает, и тревога на каждый чих приучила бы её не читать.
GATEWAY_DOWN_RED = 3

#: Сколько неразобранных сообщений за час считать бедой. Один-два — это
#: единичная невиданная форма содержимого, и на неё не будят человека; пять
#: подряд означают, что Авито поменял формат и мы теряем обращения потоком.
UNPARSED_RED = 5

# ⚠ ПЯТЬ ПОРОГОВ, КОТОРЫЕ ПРИТВОРЯЛИСЬ НАСТРОЙКАМИ. До 23.08 они читались через
# `_int_setting("watchdog_…")` — с именами, которых в `Settings` нет и не было
# (проверено: там объявлено одиннадцать полей `watchdog_*`, ни одного из этих).
# `getattr` всегда возвращал None, значит всегда работал default, а человек,
# вписавший `WATCHDOG_AVITO_DOWN_RED=42` в окружение, не менял ничего — и узнать
# об этом ему было неоткуда.
#
# Сделаны константами, а не настройками, по тому же доводу, что у `DECLINE_TTL`:
# настройкой это стало бы полем, которое заполняют один раз и забывают. Понадобится
# рычаг — заводится поле в `Settings` И строка в примере окружения, вместе.

#: Сколько минут форы даём просроченному токену, прежде чем звать человека.
TOKEN_GRACE_MINUTES = 30

#: Сколько раз Авито должен не ответить за час, чтобы это перестало быть рябью.
AVITO_DOWN_RED = 5

#: И какую ДОЛЮ обращений эти отказы обязаны составлять.
#:
#: ⚠ ВТОРОЕ УСЛОВИЕ ПОЯВИЛОСЬ ПОСЛЕ БОЕВОГО СЛУЧАЯ 28.08. Порог считал только
#: отказы: сто отказов за час — тревога. А обращений в тот же час было 385, из
#: них 285 удачных: канал работал, просто с рябью. Человеку при этом приходило
#: «перестают работать сверка, история, отправка ответов» — то есть тревога
#: описывала лежащую площадку там, где она стояла на ногах.
#:
#: Треть выбрана так: при исправной работе доля сетевых отказов у чужого API
#: держится единицами процентов, при лежащей площадке уходит к сотне. Треть
#: лежит между этими мирами с большим запасом в обе стороны и не срабатывает на
#: коротких просадках.
#:
#: Цена ложной тревоги здесь измерена: соседний сторож помнит, как «112 ложных
#: тревог закрыли собой два реальных провала резервной копии, и заметили их
#: только неделю спустя».
AVITO_DOWN_SHARE = 0.33

#: Хоть одно обращение в выключенный канал за час — уже беда: клиент пишет, а
#: приём остановлен.
DROPPED_DISABLED_RED = 1

#: Нижняя граница порога тишины — четыре часа. НЕ ДУБЛЬ ЗНАЧЕНИЯ ПО УМОЛЧАНИЮ,
#: А ЗАЩИТА ОТ ВТОРОГО ИСТОЧНИКА ПРАВДЫ, и вот её история.
#:
#: 9 августа порог подняли с получаса до четырёх часов ПО ИЗМЕРЕНИЮ живого
#: потока: у заказчика медиана паузы между обращениями 4 минуты, но каждая
#: десятая пауза длиннее трёх часов. Подняли, однако, ровно в двух местах —
#: в ``Settings.watchdog_inbound_stall_minutes`` и в запасном значении ниже.
#: А на сервере порог берётся ИЗ ОКРУЖЕНИЯ, и там до сих пор стоит строка из
#: ``.env.prod.example``: ``WATCHDOG_INBOUND_STALL_MINUTES=30``. Настройка
#: бьёт оба «умолчания», и на бою порог как был получасовым, так и остался.
#:
#: Что это стоило. С 6 по 11 августа «Приём сообщений остановился» дал 112
#: критичных строк и 456 повторов, срабатывая на паузах от получаса. Текст
#: строки переписывается последним повтором (``notify``: «свежий текст
#: полезнее первого»), поэтому в колокольчике люди видели «48–53 мин» —
#: строку заводило на 30-й минуте, а показывала она четвёртый повтор. Этот
#: шум вытеснил из колокольчика два настоящих критичных «Резервное
#: копирование не выполнилось» (6 и 8 августа), их не увидел никто, и копий
#: за те ночи нет.
#:
#: Поэтому число живёт здесь и работает полом, а не умолчанием: настройка
#: ниже него означает не «тонкая подстройка», а «забытая строка в env» —
#: и цену этой забытой строки мы уже заплатили. Поднять порог настройкой
#: можно как угодно высоко, опустить ниже измеренного дециля пауз — нельзя.
MIN_INBOUND_STALL_MINUTES = 240

# Ключи подавления, отличающиеся от вида события. Нужны там, где под одним
# видом живут разные поводы: «скрипт бэкапа сообщил о падении» и «скрипта
# вообще не слышно» — это две разные новости, схлопывать их в одну строку
# нельзя (14 §4).
DEDUP_BACKUP_MISSING = "backup.missing"
DEDUP_SCHEDULER_HEARTBEAT = "scheduler.heartbeat_broken"

# Критичное без кнопки — только осознанно и с причиной (14 §3). Тот же приём,
# что ACTIONLESS_CRITICAL в центре уведомлений и PENDING_ACTIONS в журнале
# аудита: правило нарушается ровно здесь и ровно с объяснением.
#
# Речь про случаи, когда важность ПОВЫШЕНА по обстоятельствам и потому не
# совпадает с каталогом центра (там у этих видов severity ниже и кнопки нет).
ESCALATED_CRITICAL: dict[str, str] = {
    KIND_CERT_EXPIRING: (
        "Просроченный сертификат чинится на сервере (certbot renew) — из браузера нажать нечего"
    ),
}

# Отметка живости планировщика — тот же ключ, что пишет app/scheduler/main.py и
# читает /api/health/deep. Литерал повторён намеренно: тянуть сюда модуль
# FastAPI-ручки ради одной строки дороже, чем продублировать её с этим
# комментарием (ключ входит ещё и в docker healthcheck прод-compose).
SCHEDULER_HEARTBEAT_KEY = "scheduler:alive"
# Отметка успешного бэкапа: её ставит POST /internal/notify по сигналу
# deploy/backup.sh (скрипт живёт на хосте и в приложение импортироваться не может).
BACKUP_OK_KEY = "watchdog:backup:last_ok"
# Отметка живости самого сторожа — чтобы «сторож молчит» было видно снаружи.
WATCHDOG_ALIVE_KEY = "watchdog:alive"
WATCHDOG_ALIVE_TTL_SECONDS = 1800

JOB_FAST_ID = "watchdog_fast"
JOB_DAILY_ID = "watchdog_daily"
DEFAULTS: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}


# --- настройки с запасным значением ------------------------------------------
# Пороги настраиваемые: поля ``watchdog_*`` уже есть в Settings
# (app/core/config.py, раздел «Внешний сторож»), и окружение их переопределяет.
#
# getattr вместо прямого обращения оставлен намеренно, по двум причинам.
# Во-первых, значение по умолчанию из 14 §2.1 видно прямо здесь, рядом с самой
# проверкой, а не через открытый в соседнем окне config.py. Во-вторых, config —
# чужая зона: переименование поля там опустит проверку до значения по умолчанию,
# а не уронит весь процесс планировщика на импорте. Сторож, который не
# запускается из-за переименованной настройки, хуже сторожа с дефолтным порогом.


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, None)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("watchdog.bad_setting", setting=name, value=value)
        return default


def _str_setting(name: str, default: str) -> str:
    value = getattr(settings, name, None)
    return str(value) if value else default


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """Наивное время считаем UTC — во всём проекте так (SQLite отдаёт naive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# =============================================================================
# 1. Приём сообщений остановился (14 §2.1, критично)
# =============================================================================


def _in_work_hours(local: datetime) -> bool:
    start = _int_setting("watchdog_work_start_hour", 9)
    end = _int_setting("watchdog_work_end_hour", 21)
    return start <= local.hour < end


def _work_minutes_elapsed(local: datetime) -> int:
    """Сколько минут рабочего дня уже прошло (по Москве)."""
    start = _int_setting("watchdog_work_start_hour", 9)
    return max(0, (local.hour - start) * 60 + local.minute)


def _stall_threshold_minutes() -> int:
    """Порог тишины, но не ниже измеренного пола (:data:`MIN_INBOUND_STALL_MINUTES`).

    Обычный ``_int_setting`` здесь не годится: он отдаёт значение окружения как
    есть, а на бою в окружении лежит порог, отменённый ещё 9 августа. Молчаливое
    подчинение чужой настройке стоило двух ночей без резервной копии, поэтому
    здесь настройка проверяется и о её подмене говорится вслух — иначе в
    следующий раз разбираться пришлось бы снова с нуля.
    """
    configured = _int_setting("watchdog_inbound_stall_minutes", MIN_INBOUND_STALL_MINUTES)
    if configured < MIN_INBOUND_STALL_MINUTES:
        log.warning(
            "watchdog.stall_threshold_too_low",
            env="WATCHDOG_INBOUND_STALL_MINUTES",
            configured=configured,
            used=MIN_INBOUND_STALL_MINUTES,
        )
        return MIN_INBOUND_STALL_MINUTES
    return configured


def _not_a_stub() -> Any:
    """Аккаунт заказчика, а не служебная заглушка регрессионного набора.

    ЗАЧЕМ ЭТО ВЕЗДЕ, ГДЕ СТОРОЖ СЧИТАЕТ КАНАЛЫ. Заглушку заводит
    ``app/cli.py seed-smoke``: у неё нет токена Авито вовсе (в поле лежит
    строка ``smoke-account-has-no-avito-token``), а срок действия проставлен
    на десять лет вперёд — иначе `conversations.account_id` не к чему было бы
    привязать служебный диалог `SMOKE-CONV`. Для сторожа такой канал — идеальный
    самозванец: «токен живой» у него вечно, обращений не бывает никогда.

    Что это дало на боевой системе 11 августа. Заглушку включили кнопкой
    (`enable` не отличал её от канала — теперь отличает, см.
    ``app/api/routes/avito_connect.py``), и она стала «работающим каналом» для
    :func:`check_inbound_stalled`. Дальше проверка рассуждала совершенно
    правильно: работающий канал есть, входящих нет — значит приём встал. И так
    каждые пять минут, вечно, потому что обращений в заглушке не появится
    никогда. Тревога, которая звонит впустую, приучает не смотреть в
    колокольчик — а следующей за ней была «Резервное копирование не
    выполнилось».

    ПОЧЕМУ ПО ПОМЕТКЕ, А НЕ ПО ЖИВОСТИ ТОКЕНА. Прежняя шапка
    :func:`_working_channels_condition` обещала, что заглушку отсекает как раз
    проверка токена: «пометку можно проставить ошибочно, действующий токен
    подделать нечем». Обещание не выполнялось ни дня — у заглушки срок токена
    на десять лет вперёд, и по этому условию она проходит первой. Пометку же
    ставит тот, кто заглушку и создал, — это единственный признак, который
    вообще что-то знает о её природе.
    """
    return AvitoAccount.is_service.is_(False)


def _working_channels_condition(moment: datetime) -> Any:
    """Канал, который РАБОТАЕТ: не заглушка, включён и с живым токеном.

    ПОЧЕМУ НЕ ПРОСТО ``status='active'``. В тексте тревоги стояло «подключённых
    аккаунтов Авито: 3», хотя работали два. Пометка «включён» ставится и
    ошибочно, и по инерции: до 11 августа «Обновить токен» возвращала в
    ``active`` даже выключенный канал (см.
    ``tests/unit/test_city_and_disabled_channel.py``). Действующий токен —
    признак более честный, поэтому он здесь и остаётся.

    Но одного токена мало: у служебной заглушки он «живой» на десять лет
    вперёд, и по этому условию она проходит первой из всех. Поэтому заглушку
    отсекает отдельное условие — :func:`_not_a_stub`, там же и разбор.

    Живость токена считается ТЕМ ЖЕ ЗАПАСОМ, что у :func:`check_channel_mute`
    (``watchdog_token_grace_minutes``), и это одна настройка на две проверки
    намеренно: с двумя определениями «работающего канала» одна проверка
    говорила бы «канал не может отвечать», а вторая тем же каналом доказывала,
    что тишины не бывает.
    """
    grace = timedelta(minutes=TOKEN_GRACE_MINUTES)
    return sa.and_(
        _not_a_stub(),
        AvitoAccount.status == "active",
        AvitoAccount.token_expires_at.is_not(None),
        AvitoAccount.token_expires_at >= moment - grace,
    )


async def check_inbound_stalled(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Активный аккаунт есть, а входящих нет N минут.

    ПОРОГ ПОДНЯТ С ПОЛУЧАСА ДО ЧЕТЫРЁХ, И ЭТО ИЗМЕРЕНО, А НЕ УГАДАНО. Полчаса
    выведены из проектного допущения «10 000 сообщений в сутки на тридцати
    аккаунтах» — там тишина в полчаса действительно поломка. На боевых данных
    заказчика (два канала, пятнадцать обращений в сутки на канал) картина
    другая: медиана паузы между входящими — 4 минуты, но КАЖДАЯ ДЕСЯТАЯ пауза
    длиннее трёх часов, а самая долгая — двенадцать.

    Что из этого вышло: за шесть часов сторож выдал 27 критичных «приём
    остановился», пока приём работал. Тревога, которая звонит впустую, хуже
    отсутствующей — она приучает не смотреть в колокольчик, и следующая,
    настоящая, тоже останется непрочитанной.

    ЧЕТЫРЕ ЧАСА — не идеал, а честный компромисс: это между девятым и десятым
    децилем настоящих пауз. Число вынесено в настройку: у другого объёма оно
    другое, и подбирать его надо глядя на живой поток, а не на допущение.

    ПОДНЯТЬ ПОРОГ ОКАЗАЛОСЬ МАЛО — ЕГО ЕЩЁ НАДО БЫЛО ДОНЕСТИ ДО СЕРВЕРА. Правка
    9 августа поменяла умолчание в двух местах кода и ни одного — в окружении,
    откуда порог и берётся; на бою он остался получасовым, и тревога звонила на
    паузах, которые видели глазами как «сорок восемь минут тишины». Разбор
    и пол, который это закрывает, — :data:`MIN_INBOUND_STALL_MINUTES`.

    ВАЖНОСТЬ ПОНИЖЕНА ДО «ВАЖНО», И ЭТО ГЛАВНОЕ. Критичное поднимает красную
    плашку поверх экрана и держится, пока человек не подтвердит. Эта тревога
    занимала плашки сотнями строк — и вытеснила из колокольчика «Резервное
    копирование не выполнилось» за две ночи подряд. Тишина в приёме это
    подозрение, за которым может стоять просто спокойный день; «копии за ночь
    нет» — свершившийся факт, восстанавливать систему не из чего. Критичность
    оставлена второму. Настоящий и не зависящий от объёма сигнал про приём —
    `check_webhook_lost`, он критичным и остался.

    ГЛАВНЫЙ ЖЕ СИГНАЛ ТЕПЕРЬ ДРУГОЙ — `check_webhook_lost` ниже. Он не зависит
    от объёма вовсе: спрашивает у Авито, стоит ли ещё наша подписка. Тишина —
    признак косвенный и на малом потоке почти бесполезный; пропавшая подписка
    это факт.

    Тишина ночью — это не поломка, а ночь: проверка работает только в рабочее
    время и не раньше, чем пройдёт N минут САМОГО рабочего дня. Иначе каждое
    утро в 09:00 приходило бы «приём остановился» по итогам спокойной ночи.

    Считаем по входящим СООБЩЕНИЯМ, а не по принятым вебхукам: та поломка,
    ради которой проверка написана (сменили секрет вебхука), одинаково гасит и
    то и другое, а вот «вебхуки идут, а конвейер их роняет» видно только по
    сообщениям.
    """
    threshold = _stall_threshold_minutes()
    moment = _now(now)
    local = moment.astimezone(MSK)
    if not _in_work_hours(local) or _work_minutes_elapsed(local) < threshold:
        return None

    # Считаем РАБОТАЮЩИЕ каналы, а не помеченные активными: канал без живого
    # токена ни доказательством «тишины не бывает» не является, ни новостью —
    # про него уже говорит своя критичная тревога (`check_channel_mute`).
    working = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AvitoAccount)
                .where(_working_channels_condition(moment))
            )
        ).scalar_one()
    )
    if not working:
        return None  # приёму неоткуда взяться — это не поломка

    # ``direction='in' AND sender_type='client'`` — определение входящего от
    # клиента, принятое во всём проекте (06 §2.3, app/services/stats.py). Второе
    # условие здесь не косметика: под эту ровно пару заведён частичный индекс
    # ``idx_messages_client_in ON messages (created_at) WHERE direction='in' AND
    # sender_type='client'`` (миграция 0004). По одному ``direction`` планировщик
    # PostgreSQL частичный индекс взять не может — предикат запроса не влечёт
    # предикат индекса, — и ``max(created_at)`` вырождается в полный проход по
    # ВСЕМ месячным партициям ``messages``. Раз в 5 минут, на самой горячей
    # таблице, вечно. С обоими условиями это обратный проход по индексу.
    last_inbound = (
        await db.execute(
            select(func.max(Message.created_at)).where(
                Message.direction == "in", Message.sender_type == "client"
            )
        )
    ).scalar()
    if last_inbound is None:
        # Ни одного входящего за всю жизнь: точка отсчёта — подключение самого
        # старого активного аккаунта, иначе только что подключённый аккаунт
        # мгновенно давал бы «приём остановился».
        last_inbound = (
            await db.execute(
                select(func.min(AvitoAccount.created_at)).where(_working_channels_condition(moment))
            )
        ).scalar()
    if last_inbound is None:
        return None

    minutes = int((moment - _aware(last_inbound)).total_seconds() // 60)
    if minutes < threshold:
        return None

    return NotificationDraft(
        kind=KIND_INBOUND_STALLED,
        # «Важно», а не «критично»: см. разбор в docstring — критичность этой
        # тревоги похоронила два настоящих «Резервное копирование не выполнилось».
        severity=WARNING,
        title="Приём сообщений остановился",
        body=(
            f"Последнее сообщение от клиента пришло {minutes} мин назад, "
            f"хотя работающих каналов Авито: {working}. В рабочее время такой "
            "тишины не бывает — скорее всего, приём встал и клиенты пишут в пустоту."
        ),
        dedup_key=KIND_INBOUND_STALLED,
        # entity_type здесь НЕ задаём: у вида ``inbound.stalled`` он объявлен в
        # каталоге центра (``account``), и вторая копия того же значения просто
        # разъедется с ним при первой правке. Конкретного аккаунта у этой
        # проверки нет — она смотрит на приём целиком (см. open question про
        # многоаккаунтную установку).
    )


# =============================================================================
# 2. Очередь входящих не разбирается (14 §2.1, важно)
# =============================================================================


async def check_queue_backlog(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Отставание очереди выше порога — сообщения не доезжают до менеджеров.

    «Отставание» считает ``_queue_probe`` из ``/api/health/deep``: второе
    определение того же числа рано или поздно разъедется с первым, а цена
    расхождения — спор «у мониторинга 0, у уведомлений 1200».
    """
    try:
        from app.api.routes.health import _queue_probe
    except ImportError:  # pragma: no cover — модуль ручки всегда на месте
        log.warning("watchdog.queue_probe_unavailable")
        return None

    probe = await _queue_probe(redis)
    backlog = int(probe.get("len") or 0)
    oldest = int(probe.get("oldest_pending_sec") or 0)
    len_red = _int_setting("watchdog_queue_backlog_red", settings.health_queue_len_red)
    age_red = _int_setting("watchdog_queue_oldest_sec_red", settings.health_oldest_pending_sec_red)
    if backlog <= len_red and oldest <= age_red:
        return None

    return NotificationDraft(
        kind=KIND_QUEUE_BACKLOG,
        severity=WARNING,
        title="Очередь входящих не разбирается",
        body=(
            f"В очереди {backlog} сообщений, самое старое ждёт {oldest // 60} мин. "
            "Сообщения от клиентов приходят, но до менеджеров не доезжают."
        ),
        dedup_key=KIND_QUEUE_BACKLOG,
    )


# =============================================================================
# 3. Диск (14 §2.1, важно; при 100% система встаёт целиком)
# =============================================================================


def _disk_path() -> str:
    for candidate in (_str_setting("watchdog_disk_path", ""), settings.media_root, "/"):
        if candidate and os.path.isdir(candidate):
            return candidate
    return "/"


async def check_disk_space(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    path = _disk_path()
    usage = shutil.disk_usage(path)
    if usage.total <= 0:  # pragma: no cover — не бывает на живой ФС
        return None
    used_pct = int(round(usage.used * 100 / usage.total))
    warn_at = _int_setting("watchdog_disk_used_pct_warning", 85)
    crit_at = _int_setting("watchdog_disk_used_pct_critical", 95)
    if used_pct < warn_at:
        return None
    free_gb = usage.free / 1024**3
    # Важность одна и та же (14 §2.1: «важно») — растёт не она, а прямота
    # текста. Ключ подавления тоже один: центр обновляет текст у непрочитанной
    # строки, поэтому админ видит текущий процент, а не первый замеченный.
    if used_pct >= crit_at:
        body = (
            f"Занято {used_pct}%, свободно {free_gb:.1f} ГБ. Это уже край: когда "
            "места не останется, встанет всё — приём сообщений, вложения и "
            "резервные копии."
        )
    else:
        body = (
            f"Занято {used_pct}%, свободно {free_gb:.1f} ГБ. Пока всё работает, "
            "но место кончается — стоит убрать старые копии или расширить диск."
        )
    return NotificationDraft(
        kind=KIND_DISK_SPACE,
        severity=WARNING,
        title=f"Диск заполнен на {used_pct}%",
        body=body,
        dedup_key=KIND_DISK_SPACE,
    )


# =============================================================================
# 4. Сертификат истекает (14 §2.1, обычно) — раз в сутки
# =============================================================================


def _cert_path() -> str:
    return _str_setting(
        "watchdog_cert_path", f"/etc/letsencrypt/live/{settings.domain}/fullchain.pem"
    )


def _cert_not_after(path: str) -> datetime | None:
    """Дата окончания сертификата из PEM-файла; None — прочитать не удалось.

    «НЕТ ФАЙЛА» И «НЕТ ПРАВ» — РАЗНЫЕ БЕДЫ, И РАНЬШЕ ОНИ БЫЛИ ОДНОЙ.

    Оба случая писались как `cert_file_absent` уровнем info — «штатная ситуация
    вне прода». На боевой системе 12 августа выяснилось, что ситуация там
    совсем не штатная: том `/etc/letsencrypt` планировщику ПРОКИНУТ и путь
    верный, а `ls` внутри контейнера отвечает `Permission denied` — Let's
    Encrypt держит `live/` и `archive/` в режиме 0700 для root, а контейнер
    работает не от root.

    То есть сторож, который обязан предупредить о протухающем сертификате,
    молчал с самого начала — и молчал ТИХО, строкой уровня info, которую никто
    не ищет. Сертификат продлевается сам, поэтому беды не случилось; случилась
    бы она в тот день, когда продление сломается.

    Теперь нехватка прав — предупреждение с прямой подсказкой: чинится одной
    командой на хосте (`chmod 755 /etc/letsencrypt/live /etc/letsencrypt/archive`
    — сам ключ остаётся 600, а `fullchain.pem` и так публичен).
    """
    try:
        raw = pathlib.Path(path).read_bytes()
    except PermissionError:
        log.warning(
            "watchdog.cert_unreadable_permissions",
            path=path,
            hint="chmod 755 /etc/letsencrypt/live /etc/letsencrypt/archive на хосте",
        )
        return None
    except OSError:
        # Файла правда нет — штатно вне прода, где сертификата не бывает.
        log.info("watchdog.cert_file_absent", path=path)
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(raw)
    except Exception:
        log.warning("watchdog.cert_unreadable", path=path)
        return None
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return _aware(not_after)


async def check_certificate_expiry(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Автопродление обычно работает — но «обычно» это не «всегда» (14 §2.1)."""
    not_after = _cert_not_after(_cert_path())
    if not_after is None:
        return None
    moment = _now(now)
    days_left = int((not_after - moment).total_seconds() // 86400)
    if days_left > _int_setting("watchdog_cert_expiry_days", 25):
        return None

    # Дата в ключе: продлённый сертификат — это НОВОЕ событие, и оно обязано
    # пробиться сквозь подавление повторов, а не слиться со старым.
    dedup = f"{KIND_CERT_EXPIRING}:{not_after.date().isoformat()}"
    if days_left < 0:
        return NotificationDraft(
            kind=KIND_CERT_EXPIRING,
            severity=CRITICAL,
            title="Сертификат сайта истёк",
            body=(
                "Браузеры показывают предупреждение вместо системы, "
                "десктоп-клиенты не подключаются. Автопродление не сработало."
            ),
            dedup_key=dedup,
        )
    return NotificationDraft(
        kind=KIND_CERT_EXPIRING,
        severity=INFO,
        title=f"Сертификат сайта истекает через {days_left} дн.",
        body=(
            "Обычно он продлевается сам за месяц до конца. Если этого до сих пор "
            "не случилось — стоит проверить продление, иначе сайт перестанет открываться."
        ),
        dedup_key=dedup,
    )


# =============================================================================
# 5. Отметка живости планировщика (см. предупреждение в шапке модуля)
# =============================================================================


async def check_scheduler_heartbeat(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Сам себя планировщик проверить не может — проверяем его отметку живости.

    Это уведомление говорит не «планировщик умер» (умерший его бы не отправил),
    а «у нас пропал СПОСОБ узнать, что он умер».
    """
    raw = await redis.get(SCHEDULER_HEARTBEAT_KEY)
    max_age = _int_setting(
        "scheduler_heartbeat_max_age_seconds", settings.scheduler_heartbeat_max_age_seconds
    )
    age: int | None = None
    if raw is not None:
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            beat = _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            beat = None
        if beat is not None:
            age = int((_now(now) - beat).total_seconds())
            if age <= max_age:
                return None
    detail = "отметка пропала совсем" if raw is None else f"последняя отметка была {age} с назад"
    return NotificationDraft(
        kind=KIND_SCHEDULER_DOWN,
        severity=WARNING,  # ниже, чем «планировщик мёртв»: он-то как раз жив
        title="Проверка живости планировщика не работает",
        body=(
            f"Планировщик работает — это сообщение отправил он сам, — но {detail}. "
            "Пока так, проверка контейнера и внешний сторож видят его мёртвым, и "
            "настоящее зависание от этой поломки не отличить."
        ),
        dedup_key=DEDUP_SCHEDULER_HEARTBEAT,
    )


# =============================================================================
# 6. Резервное копирование не выполнилось (14 §2.1, критично)
# =============================================================================


async def check_backup_freshness(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Отметку об успехе ставит сам скрипт бэкапа через POST /internal/notify.

    Смысл проверки именно в ОТСУТСТВИИ отметки: упавший скрипт про себя ещё
    сообщит, а вот скрипт, который вообще не запустился (снесли строку в cron,
    не поднялся контейнер), молчит — и это ровно та «неделя молчания» из 14 §1.

    Работает только в проде: в dev и CI бэкапа нет, и уведомление было бы враньём.
    """
    if settings.env != "production":
        return None
    max_age_hours = _int_setting("watchdog_backup_max_age_hours", 26)  # сутки + запас
    raw = await redis.get(BACKUP_OK_KEY)
    moment = _now(now)
    if raw is not None:
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            last_ok = _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            last_ok = None
        if last_ok is not None:
            hours = (moment - last_ok).total_seconds() / 3600
            if hours <= max_age_hours:
                return None
            detail = f"Последняя успешная копия сделана {int(hours)} ч назад"
        else:
            detail = "Отметка о последней копии испорчена"
    else:
        detail = "Успешных копий не было ни разу с момента запуска"
    return NotificationDraft(
        kind=KIND_BACKUP_FAILED,
        severity=CRITICAL,
        title="Резервное копирование не выполнилось",
        body=(
            f"{detail}, а копия должна делаться каждую ночь. Пока это так, "
            "восстанавливать систему в случае аварии будет не из чего."
        ),
        # Свой ключ: «скрипт сообщил о падении» и «скрипта не слышно» — разные
        # новости под одним видом события.
        dedup_key=DEDUP_BACKUP_MISSING,
    )


# =============================================================================
# Прогон
# =============================================================================

CheckFn = Callable[..., Awaitable[NotificationDraft | None]]


@dataclass(frozen=True)
class Check:
    name: str
    run: CheckFn


async def check_inbound_unparsed(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Сообщения от клиентов, которые мы не смогли разобрать (этап 1).

    ЧТО БЫЛО. Не разобрался вебхук — сырец ложится в `webhook_raw_log` с
    пометкой об ошибке, в журнал уходит предупреждение, запись подтверждается.
    И всё: НИКТО НЕ УЗНАЁТ. Это буквально «клиент написал, а до людей не
    доехало». На имитаторе, который шлёт один текст, такого не случалось
    никогда; на боевом Авито с фотографиями, голосовыми и видео — случится.

    ПОЧЕМУ СТОРОЖЕМ, А НЕ ПИСЬМОМ НА КАЖДЫЙ СЛУЧАЙ. Если Авито поменяет форму
    содержимого, не разберётся не одно сообщение, а поток. Уведомление на
    каждое превратило бы центр в ленту одинаковых строк, и её перестали бы
    читать — ровно тогда, когда читать надо. Здесь одна строка в час с числом.

    ПОРОГ, А НЕ «ХОТЬ ОДНО». Единичная невиданная форма — не беда, а новость,
    и будить из-за неё человека не за чем: сырец сохранён, разберём. Пять за
    час означают, что мы теряем обращения потоком.

    Отключённые аккаунты и smoke-заглушка сюда НЕ попадают: у них своя пометка
    (`account_missing_or_disabled`), и это ожидаемый исход, а не поломка.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(hours=1)
    threshold = UNPARSED_RED

    total = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(WebhookRawLog)
            .where(
                WebhookRawLog.received_at >= since,
                WebhookRawLog.error.is_not(None),
                WebhookRawLog.error.not_like("account_missing_or_disabled%"),
            )
        )
    ).scalar_one()

    if int(total or 0) < threshold:
        return None

    return NotificationDraft(
        kind=KIND_INBOUND_UNPARSED,
        severity=WARNING,
        title="Сообщения от клиентов не разбираются",
        body=(
            f"За последний час не разобрано {total} сообщений от Авито. "
            "Они сохранены целиком, но до операторов не дошли: скорее всего "
            "Авито поменял формат. Разбирать — по журналу webhook_raw_log."
        ),
        dedup_key=KIND_INBOUND_UNPARSED,
    )


async def check_webhook_lost(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """У активного канала пропала НАША подписка на события Авито.

    ЗАЧЕМ ЭТО ГЛАВНЫЙ СИГНАЛ, А НЕ ТИШИНА. «Входящих нет N минут» — признак
    косвенный, и на малом потоке почти бесполезный: у заказчика каждая десятая
    пауза между обращениями длиннее трёх часов, и любой разумный порог либо
    звонит впустую, либо молчит полдня. Пропавшая подписка — это ФАКТ, и он не
    зависит от объёма вовсе.

    ЧТО ИМЕННО ЛОВИТСЯ. Авито держит на аккаунт ровно ОДНУ подписку (проверено
    9 августа на боевых аккаунтах, docs/24 §2). Значит любой, кто подпишется
    после нас, — Jivo, которую переподключили, другая система, сам владелец из
    кабинета, — нашу молча заменит. Снаружи это выглядит как «обращения
    перестали приходить», и без этой проверки разбираться в причине пришлось
    бы часами.

    ХОДИТ В АВИТО, И ПОТОМУ РЕДКО. Проверка живёт в ежедневном наборе, а не в
    пятиминутном: канал не отбирают каждые пять минут, а лишний поход к чужому
    API за спиной у людей — плохая привычка. Ошибка сети не считается потерей:
    «не смогли спросить» и «подписки нет» — разные новости.

    ИТОГ КАЖДОЙ СВЕРКИ ТЕПЕРЬ ЗАПОМИНАЕТСЯ (12 августа). Сам поход в Авито
    делает :func:`channel_health.audit_subscription`, и он же кладёт ответ в
    Redis. Благодаря этому карточка канала может написать «сверка 16:54,
    расхождений нет» — то есть показать не догадку по тишине, а факт, и притом
    с датой. Раньше ответ Авито жил ровно до конца этого цикла и наружу не
    выходил никогда.
    """
    accounts = (
        (
            await db.execute(
                # Заглушка сюда не идёт (:func:`_not_a_stub`): спрашивать у Авито
                # про подписку строкой ``smoke-account-has-no-avito-token``
                # значит гарантированно получить отказ и записать в лог
                # «не смогли спросить» — раз в сутки, вечно и ни о чём.
                select(AvitoAccount).where(_not_a_stub(), AvitoAccount.status == "active")
            )
        )
        .scalars()
        .all()
    )
    if not accounts:
        return None

    from app.services import channel_health

    moment = _now(now)
    lost: list[str] = []
    for account in accounts:
        audit = await channel_health.audit_subscription(db, redis, account, now=moment)
        if audit.result == channel_health.AUDIT_UNKNOWN:
            # «Не смогли спросить» — не «подписки нет». Молчим: о недоступном
            # Авито скажут другие проверки, а ложная тревога здесь стоила бы
            # доверия ко всем остальным.
            log.warning("watchdog.subscriptions_unavailable", account_id=str(account.id))
            continue
        if audit.lost:
            lost.append(account.title)

    if not lost:
        return None

    return _webhook_lost_draft(lost)


def _webhook_lost_draft(titles: list[str]) -> NotificationDraft:
    """Одна строка про пропавшую подписку — на оба места, где это ловится.

    Ежедневный обход и быстрая проверка замолчавшего канала находят одно и то
    же состояние; два текста про него разъехались бы на первой правке, а
    склеиваются они всё равно в одну строку центра (склейка по виду).
    """
    names = ", ".join(f"«{t}»" for t in titles)
    return NotificationDraft(
        kind=KIND_WEBHOOK_LOST,
        severity=CRITICAL,
        title="Канал отобрали: подписка на события пропала",
        body=(
            f"У аккаунтов {names} наша подписка на события Авито больше не стоит. "
            "Обращения из них не приходят вовсе. Авито держит на аккаунт одну "
            "подписку — значит на неё подписался кто-то другой. Лечится кнопкой "
            "перерегистрации вебхука на карточке канала."
        ),
        dedup_key=KIND_WEBHOOK_LOST,
    )


async def check_quiet_channel_subscription(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Канал молчит дольше СВОЕГО обычного — сверить подписку, не дожидаясь суток.

    ЗАЧЕМ ЕЩЁ ОДНА ПРОВЕРКА ПРО ПОДПИСКУ. Ежедневный обход выше отвечает на
    вопрос «не отобрали ли канал» раз в сутки и по всем каналам сразу. Этого
    мало в единственном случае, который и есть настоящая беда: канал замолчал
    ПРЯМО СЕЙЧАС, и до утреннего обхода никто ничего не узнает. Спрашивать же
    Авито про все каналы каждые пять минут нельзя — это чужой API, и стучаться
    в него без повода мы не будем.

    ПОВОД — ТИШИНА, НО НЕ ЛЮБАЯ. Сравнивается не абсолютное время («событий
    нет 16 ч»), а рабочая пауза с ОБЫЧНЫМ ритмом этого канала
    (``channel_health.quiet_threshold``). На канале с пятью обращениями в
    неделю сутки тишины — норма, и повода нет; на канале с дневным потоком
    четыре рабочих часа тишины — уже повод спросить.

    ЕСЛИ РИТМА НЕ ЗНАЕМ — НЕ СПРАШИВАЕМ ВОВСЕ. Только что подключённый канал
    сравнивать не с чем, и любое число здесь было бы выдумкой. Такие каналы
    закрывает ежедневный обход, который ходит по всем.

    ТРЕВОГА ПОДНИМАЕТСЯ ТОЛЬКО ПО ФАКТУ. Сверка сказала «адрес чужой» или
    «подписки нет» — критичное уведомление. Сверка сказала «адрес наш» —
    МОЛЧИМ: тишина объяснилась, и это ровно тот случай, ради которого всё
    затевалось. Не смогли спросить — тоже молчим.

    УЖЕ ОТОБРАННЫЙ КАНАЛ ПЕРЕСПРАШИВАЕТСЯ, а не помнится. Иначе выходило бы
    вот что: подписку вернули кнопкой, а сторож ещё сутки, до следующего
    ежедневного обхода, повторял бы «канал отобрали» по устаревшей записи. И
    наоборот — верить старой записи «всё хорошо» тоже нельзя. Поэтому решает
    сверка не старше получаса, а не то, что мы записали неделю назад.
    """
    from app.services import channel_health

    moment = _now(now)
    accounts = (
        (
            await db.execute(
                select(AvitoAccount).where(_not_a_stub(), AvitoAccount.status == "active")
            )
        )
        .scalars()
        .all()
    )
    if not accounts:
        return None

    ctx = await channel_health.load_context(db, now=moment)
    lost: list[str] = []
    for account in accounts:
        health = await channel_health.webhook_health(db, redis, account, ctx=ctx)
        audit = await channel_health.read_audit(redis, account.id)
        # Два повода спросить Авито: канал замолчал дольше своего обычного либо
        # прошлая сверка уже нашла чужой адрес (и надо узнать, починили ли).
        if health.reason != "silence_abnormal" and not audit.lost:
            continue
        if not channel_health.audit_is_fresh(audit, now=moment):
            audit = await channel_health.audit_subscription(db, redis, account, now=moment)
        if audit.lost:
            lost.append(account.title)

    return _webhook_lost_draft(lost) if lost else None


async def check_channel_mute(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Канал, который принимает обращения, но не может на них ответить.

    ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ПРОВЕРКА, А НЕ «СТАТУС КАНАЛА». Смотреть на статус
    мало: до 11 августа канал уходил в `needs_reauth` от любой сетевой ошибки и
    оставался там навсегда, потому что и обновление токенов, и оба соседних
    сторожа перебирают ТОЛЬКО активные аккаунты. Аккаунт выпадал из наблюдения
    ровно тогда, когда за ним надо было следить. Здесь берутся все каналы,
    кроме выключенных вручную, и проверяется факт, а не пометка: есть ли у
    канала действующий токен.

    ЧТО ИМЕННО ЛОМАЕТСЯ ДЛЯ ЛЮДЕЙ. Вебхуки Авито нашего токена не требуют,
    поэтому обращения продолжают приходить и видны в интерфейсе. А любой ответ
    падает сразу («Аккаунт Авито требует переподключения»). Канал становится
    односторонним, и узнаёт об этом только тот, кто попробует ответить.

    ЗАПАС В ПОЛЧАСА. Плановое обновление идёт за два часа до истечения и
    повторяется каждые тридцать минут, поэтому короткая просадка — норма и
    тревоги не стоит. Полчаса после фактического истечения означают, что
    обновление не сработало ни разу.
    """
    moment = _now(now)
    grace = timedelta(minutes=TOKEN_GRACE_MINUTES)
    rows = (
        (
            await db.execute(
                select(AvitoAccount).where(
                    # Заглушке отвечать некому и нечем (:func:`_not_a_stub`):
                    # «Канал не может отвечать клиентам» про неё — правда,
                    # которая никому не нужна, и критичная притом.
                    _not_a_stub(),
                    AvitoAccount.status != "disabled",
                    sa.or_(
                        AvitoAccount.token_expires_at.is_(None),
                        AvitoAccount.token_expires_at < moment - grace,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    names = ", ".join(f"«{a.title}»" for a in rows)
    single = rows[0] if len(rows) == 1 else None
    return NotificationDraft(
        kind=KIND_NEEDS_REAUTH,
        severity=CRITICAL,
        title="Канал не может отвечать клиентам",
        body=(
            f"У {names} нет действующего токена Авито дольше получаса. "
            "Обращения продолжают приходить, но ответы не уходят. "
            "Лечится кнопкой «Повторить сейчас» на карточке канала; если она не "
            "помогает — проверьте, включено ли приложение в кабинете Авито."
        ),
        entity_type="account" if single is not None else None,
        entity_id=str(single.id) if single is not None else None,
        # Ключ повторяет формулу каталога для dedup="entity"
        # (``notifications._dedup_key``) НАМЕРЕННО: тогда строка сторожа и
        # строка, которую заводит сам сервис в момент отказа, — одна и та же
        # запись со счётчиком, а не две одинаковые новости об одном канале.
        dedup_key=(
            f"{KIND_NEEDS_REAUTH}:account:{single.id}" if single is not None else KIND_NEEDS_REAUTH
        ),
    )


async def check_leadbot_unavailable(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Лид-бот отвечает «недоступен» подряд — внешняя беда, но знать надо.

    ЧТО ЭТО ЛОВИТ. Всё, что не наш код и не действия людей: упал шлюз модели,
    кончились деньги на ключе, оборвался туннель между серверами, площадка
    модели легла. Диалоги в этот момент не теряются — движок уводит их
    человеку (02 §3.3), — но без тревоги команда узнаёт об этом случайно,
    через сутки, по «что-то бот молчит».

    ПОРОГ — ТРИ ПОДРЯД, А НЕ ОДИН. Одиночная недоступность бывает от сетевого
    чиха и лечится сама следующим вызовом; звонить на каждую значит приучить
    не читать колокольчик (тот же урок, что с «приёмом сообщений»).

    ОКНО — ПОЛЧАСА. Если за это время бота не звали вовсе, проверка молчит:
    тишина в потоке — не поломка бота.
    """
    moment = _now(now)
    since = moment - timedelta(minutes=30)
    rows = list(
        (
            await db.execute(
                sa.select(LeadbotCall.outcome, LeadbotCall.error)
                .where(LeadbotCall.created_at >= since)
                .order_by(LeadbotCall.created_at.desc())
                .limit(5)
            )
        ).all()
    )
    if len(rows) < 3:
        return None  # бота почти не звали — говорить не о чем
    последние = rows[:3]
    if not all(outcome == OUTCOME_UNAVAILABLE for outcome, _ in последние):
        return None
    причина = next((err for _, err in последние if err), "причина не названа")
    return NotificationDraft(
        kind=KIND_LEADBOT_DOWN,
        severity=WARNING,
        title="Лид-бот не отвечает",
        body=(
            f"Последние три обращения к лид-боту закончились ничем: {причина}. "
            "Диалоги уходят людям, клиенты не теряются — но бот сейчас не работает. "
            "Проверьте его сервер, ключ доступа к модели и связь между серверами."
        ),
        dedup_key=KIND_LEADBOT_DOWN,
    )


async def check_gateway_unreachable(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Шлюз внешних сервисов не отвечает три проверки подряд.

    Спрашивает `/status` шлюза сам: помощники адреса ходят к нему не всегда, и
    по одним их отказам молчащий ночью шлюз не заметить до утра. Удачная
    проверка обнуляет счёт подряд.
    """
    from app.integrations import gateway  # noqa: PLC0415 — шлюз тянет httpx-клиента

    if not gateway.enabled():
        return None
    await gateway.refresh_status(force=True)
    if gateway.last_status_error is None:
        await redis.delete(GATEWAY_DOWN_KEY)
        return None
    in_row = int(await redis.incr(GATEWAY_DOWN_KEY))
    await redis.expire(GATEWAY_DOWN_KEY, 3600)
    if in_row < GATEWAY_DOWN_RED:
        return None
    return NotificationDraft(
        kind=KIND_GATEWAY_DOWN,
        severity=WARNING,
        title="Шлюз внешних сервисов не отвечает",
        body=(
            f"Шлюз в Амстердаме не отвечает {in_row * 5} минут "
            f"(последняя причина: {gateway.last_status_error}). Пока это так, адреса "
            "из переписки не проверяются картой и не читаются моделью. Проверьте "
            "сервер шлюза и мост между серверами."
        ),
        dedup_key=KIND_GATEWAY_DOWN,
    )


async def check_avito_unreachable(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Авито не отвечает — молчание площадки видно только нам (находка L-002).

    ЧТО ЭТО ЛОВИТ. Сетевые отказы при обращении к Авито: не дозвонились, оборвалось
    чтение, вышло время. Каждый такой отказ клиент Авито считает в Redis
    (``AVITO_DOWN_KEY``, ключ живёт час), а здесь решается, беда это или рябь.

    ПОЧЕМУ НЕ ПО ОДНОМУ ОТКАЗУ. Одиночный сбой сети — норма для чужого API, и
    поднимать по нему тревогу значит приучить людей её не читать. Порог в пять
    отказов за час отделяет рябь от лежащей площадки: в боевом журнале 18 августа
    таких было восемь за сутки двумя парами — то есть две короткие просадки, о
    которых сообщать было незачем.

    ЧТО ЭТО НЕ ЛОВИТ, И ЭТО ВАЖНО. Вебхуки Авито в нашем счёте не участвуют:
    они приходят к нам, а не мы к ним. Если Авито перестанет ПРИСЫЛАТЬ, увидят
    другие сторожа — «приём остановился» и «канал замолчал».
    """
    порог = AVITO_DOWN_RED
    raw = await redis.get(AVITO_DOWN_KEY)
    if raw is None:
        return None
    try:
        сколько = int(raw)
    except (TypeError, ValueError):
        return None
    if сколько < порог:
        return None
    # ДОЛЯ, А НЕ ТОЛЬКО ЧИСЛО (см. довод у `AVITO_DOWN_SHARE`). Знаменатель
    # считает тот же клиент, тем же часовым ключом.
    from app.integrations.avito.client import AVITO_TRIES_KEY  # noqa: PLC0415 — зона OAuth

    сырые_попытки = await redis.get(AVITO_TRIES_KEY)
    try:
        попыток = int(сырые_попытки) if сырые_попытки is not None else 0
    except (TypeError, ValueError):
        попыток = 0
    # Знаменатель меньше числителя означает, что счётчик попыток моложе счётчика
    # отказов (выкатка посреди часа) — тогда доля неизвестна, и решаем по числу,
    # как решали раньше. Один час неточности лучше молчания на настоящей аварии.
    доля = (сколько / попыток) if попыток >= сколько and попыток > 0 else None
    if доля is not None and доля < AVITO_DOWN_SHARE:
        return None
    why = await redis.get(f"{AVITO_DOWN_KEY}:why")
    причина = "причина не названа"
    if why:
        причина = why.decode() if isinstance(why, bytes) else str(why)
    return NotificationDraft(
        kind=KIND_AVITO_DOWN,
        severity=WARNING,
        title="Авито не отвечает",
        body=(
            f"За последний час не удалось достучаться до Авито {сколько} раз"
            + (f" из {попыток} обращений" if доля is not None else "")
            + f". Последняя причина: {причина}. "
            "Входящие сообщения при этом продолжают приходить вебхуками — "
            "перестают работать наши обращения: сверка, история, отправка ответов."
        ),
        dedup_key=KIND_AVITO_DOWN,
    )


async def check_dropped_disabled(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Клиенты пишут в ВЫКЛЮЧЕННЫЙ канал — их сообщения принимаются и выбрасываются.

    БОЕВОЙ ПОВОД (находка L-004). Обработчик входящих на выключенном канале
    подтверждает вебхук и выходит, пометив запись ``account_missing_or_disabled``.
    Соседний сторож «сообщения не разбираются» эти строки ИСКЛЮЧАЕТ условием, а
    остальные сторожа берут только активные каналы. То есть канал случайно
    выключили — клиенты пишут в пустоту, и об этом не узнаёт никто.

    ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ПРОВЕРКА, А НЕ СНЯТОЕ ИСКЛЮЧЕНИЕ. Выключенный канал —
    это НАМЕРЕННОЕ действие человека, и мешать его в кучу с «формат разъехался»
    нельзя: разные беды с разными действиями. Здесь тревога честно говорит, что
    канал выключен, а обращения идут.
    """
    порог = DROPPED_DISABLED_RED
    since = _now(now) - timedelta(hours=1)
    сколько = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(WebhookRawLog)
            .where(
                WebhookRawLog.received_at >= since,
                WebhookRawLog.error.like("account_missing_or_disabled%"),
            )
        )
    ).scalar() or 0
    if сколько < порог:
        return None
    return NotificationDraft(
        kind=KIND_DROPPED_DISABLED,
        severity=CRITICAL,
        title="Клиенты пишут в выключенный канал",
        body=(
            f"За час пришло обращений на выключенный канал: {сколько}. "
            "Их приняли и выбросили — клиенты пишут в пустоту и ответа не получат. "
            "Включите канал на странице «Каналы» либо снимите объявления с публикации."
        ),
        dedup_key=KIND_DROPPED_DISABLED,
    )


async def check_delivery_failures(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> NotificationDraft | None:
    """Ответы не доходят до клиентов — пачкой, а не по одному.

    ⚠ ЭТОТ ВИД ОБЕЩАЛИ И НЕ СДЕЛАЛИ (аудит 19.08, находка L-012). Каталог
    объявлял «Сообщения не уходят клиентам» с 11 августа, комментарий рядом
    ссылался на него как на живой механизм, а создавать его было некому.

    ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ЛИЧНОГО «ответ не дошёл». Каждая отдельная неудача уже
    доходит до АВТОРА: колокольчик, красная метка диалога и возврат отметки
    ожидания. Этого хватает, когда сломалось одно сообщение. Но когда за час не
    ушло десять — это не десять человек ошиблись, а канал лёг, и знать об этом
    надо администратору, а не десяти операторам по отдельности.

    Порог берётся из настроек здоровья: там он уже выбран для сводки, и заводить
    второе число, которое разъедется с первым, незачем.
    """
    # Число берём у /health/deep, а не своё: два порога про одну беду разъедутся.
    порог = settings.health_failed_last_hour_red
    since = _now(now) - timedelta(hours=1)
    сколько = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.created_at >= since, Message.delivery_status == "failed")
        )
    ).scalar() or 0
    if сколько <= порог:
        return None
    return NotificationDraft(
        kind=KIND_DELIVERY_FAILURES,
        severity=WARNING,
        title="Сообщения не уходят клиентам",
        body=(
            f"За час не доставлено ответов: {сколько}. Похоже, дело не в отдельном "
            "сообщении — проверьте канал Авито и связь с площадкой. Каждый неотправленный "
            "ответ виден его автору отдельно, но столько сразу означает общую беду."
        ),
        dedup_key=KIND_DELIVERY_FAILURES,
    )


FAST_CHECKS: tuple[Check, ...] = (
    Check("leadbot_unavailable", check_leadbot_unavailable),
    Check("inbound_stalled", check_inbound_stalled),
    Check("inbound_unparsed", check_inbound_unparsed),
    Check("channel_mute", check_channel_mute),
    Check("quiet_channel_subscription", check_quiet_channel_subscription),
    Check("queue_backlog", check_queue_backlog),
    Check("disk_space", check_disk_space),
    Check("scheduler_heartbeat", check_scheduler_heartbeat),
    Check("backup_freshness", check_backup_freshness),
    Check("avito_unreachable", check_avito_unreachable),
    Check("gateway_unreachable", check_gateway_unreachable),
    Check("dropped_disabled", check_dropped_disabled),
    Check("delivery_failures", check_delivery_failures),
)

DAILY_CHECKS: tuple[Check, ...] = (
    Check("certificate_expiry", check_certificate_expiry),
    Check("webhook_lost", check_webhook_lost),
)


#: Через сколько после ПЕРВОЙ тревоги уходит первый повтор. Дальше ожидание
#: удваивается (15 → 30 → 60 → 120…), пока шаг не упрётся в потолок важности.
REPEAT_AFTER = timedelta(minutes=15)


async def _repeat_is_due(db: AsyncSession, draft: NotificationDraft, now: datetime) -> bool:
    """Пора ли повторять эту тревогу, или условие держится с прошлого раза.

    ⚠ У СТОРОЖЕВЫХ ВИДОВ ЛЕСТНИЦЫ НЕ БЫЛО ВОВСЕ. Задача бежит раз в пять минут
    и слала на КАЖДОМ проходе: склейка честно складывала повторы в одну строку,
    поэтому шторма в колокольчике не было — но счётчик рос сам по себе, ровно
    по часам. Замер боя 14 августа: «повторялось 176 раз» у одной строки и
    «141» у другой. 176 повторов по пять минут — это 14 часов непрерывного
    условия, и ни один из них не сообщал ничего нового. Строка, меняющаяся
    каждые пять минут, перестаёт быть новостью через час.

    Ровно эту беду уже лечили у напоминаний про ждущих клиентов (12 августа,
    счётчик дорос до 142) — здесь та же лестница и та же функция.

    ЧТО СЧИТАЕТСЯ ЗА «ЖДЁМ». Момент первой тревоги — `created_at` живой строки:
    сторож сам по себе не помнит, когда условие началось, а строка помнит. Она
    же несёт число уже случившихся повторов, поэтому сравнение ДОГОНЯЕТ
    пропущенные такты: после выкатки или заминки планировщика ступень не
    теряется.

    Строки нет — тревога первая, и молчать нельзя ни секунды.
    """
    spec = notifications.spec_for(draft.kind)
    severity = draft.severity or (spec.severity if spec else "warning")
    key = draft.dedup_key or notifications.dedup_key_for(
        kind=draft.kind,
        entity_type=draft.entity_type,
        entity_id=draft.entity_id,
    )
    if key is None:  # склейки у вида нет — каждая тревога своя, лестница ни при чём
        return True

    row = await notifications.find_live(
        db,
        kind=draft.kind,
        key=key,
        severity=severity,
        recipient_id=draft.recipient_id,
        audience=draft.audience,
        now=now,
    )
    if row is None:
        return True

    created = notifications.as_utc(row.created_at)
    if created is None:
        return True
    # +1 — сама первая тревога: она уже случилась и учтена в `repeat_count`.
    due = 1 + notifications.reminders_due(
        now - created, REPEAT_AFTER, step_cap=notifications.ladder_step_cap(severity)
    )
    return due > row.repeat_count


async def run_watchdog(
    db: AsyncSession,
    redis: Redis,
    *,
    checks: tuple[Check, ...] = FAST_CHECKS,
    now: datetime | None = None,
) -> list[NotificationDraft]:
    """Прогнать набор проверок и отправить всё, что они нашли.

    Упавшая проверка не уносит с собой остальные: сторож, который умирает от
    первой же ошибки, — это сторож, которого нет.
    """
    found: list[NotificationDraft] = []
    for check in checks:
        try:
            draft = await check.run(db, redis, now=now)
        except Exception:
            log.exception("watchdog.check_failed", check=check.name)
            # ⚠ ОТКАТ ОБЯЗАТЕЛЕН, ИНАЧЕ ОБЕЩАНИЕ ВЫШЕ НЕ ВЫПОЛНЯЕТСЯ.
            #
            # Все двенадцать проверок бегут по ОДНОЙ сессии, а `session_scope`
            # транзакцию не открывает и не закрывает — откат делает только выход
            # из области. В PostgreSQL любой упавший запрос переводит транзакцию
            # в состояние aborted, и КАЖДЫЙ следующий SELECT по той же сессии
            # получает «current transaction is aborted». То есть первая упавшая
            # проверка молча выключала все остальные: в журнале одна строка
            # `watchdog.check_failed`, а не двенадцать, и понять, что сторож
            # ослеп целиком, было не по чему.
            #
            # Docstring обещает «упавшая проверка не уносит с собой остальные» —
            # без этого отката обещание держалось на честном слове.
            try:
                await db.rollback()
            except Exception:
                # Сессия могла умереть вместе с соединением: тогда и откатывать
                # нечего, а следующие проверки поднимут своё исключение и будут
                # так же пропущены. Молчать здесь нельзя — это уже другая беда.
                log.exception("watchdog.rollback_failed", check=check.name)
            continue
        if draft is None:
            continue
        found.append(draft)
        if not await _repeat_is_due(db, draft, _now(now)):
            continue
        # Запись, commit и доставку в браузер делает центр (notify_now, 14 §4).
        # `now` передаём НАРОЧНО: лестница выше считает возраст строки, и штамп
        # записи обязан идти по тем же часам, что и решение о повторе.
        await send_notification(db, redis, draft, now=_now(now))
    try:
        await redis.set(WATCHDOG_ALIVE_KEY, _now(now).isoformat(), ex=WATCHDOG_ALIVE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — отметка полезна, но не ценой прогона
        log.warning("watchdog.alive_marker_failed")
    return found


async def _run_in_own_scope(checks: tuple[Check, ...]) -> None:
    async with db_mod.session_scope() as db:
        await run_watchdog(db, redis_mod.get_client(), checks=checks)


async def watchdog_fast() -> None:
    """Каждые 5 минут: приём, очередь, диск, отметка живости, бэкап."""
    await _run_in_own_scope(FAST_CHECKS)


async def watchdog_daily() -> None:
    """Раз в сутки утром: сертификат (файл меняется раз в 60 дней)."""
    await _run_in_own_scope(DAILY_CHECKS)


def register(scheduler: Any) -> None:
    """Регистрация в APScheduler — вызывается из ``app/scheduler/main.py``.

    Ежедневная проверка стоит на 06:15 UTC = 09:15 МСК: уведомление, которое
    придёт в 4 утра, никто не увидит (14 §5), а сертификату всё равно, в какой
    час суток о нём напомнили.
    """
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(watchdog_fast, IntervalTrigger(minutes=5), id=JOB_FAST_ID, **DEFAULTS)
    scheduler.add_job(
        watchdog_daily, CronTrigger(hour=6, minute=15, timezone="UTC"), id=JOB_DAILY_ID, **DEFAULTS
    )
