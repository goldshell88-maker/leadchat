"""ARQ WorkerSettings (08 §2.3).

The inbound stream consumer lives INSIDE the ARQ process as a background
asyncio task started from on_startup — один контейнер, один healthcheck
(`arq --check`), а ``--scale worker=N`` даёт и консьюмеров группы, и
исполнителей ARQ-задач.

Run: ``arq app.workers.main.WorkerSettings``.
"""

import asyncio
import contextlib
from typing import Any, cast

from arq.connections import RedisSettings
from arq.worker import create_pool, func

from app.bots import leadbot
from app.bots.runtime import bot_ask_timeout, bot_step
from app.core import redis as redis_mod
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.observability import init_sentry, with_job_scope
from app.db import session as db_mod
from app.integrations.avito import client as avito_client
from app.services import avito_app
from app.services.avito_accounts import backfill_account, backfill_conversation
from app.services.client_enrich import enrich_client
from app.services.dialogs_export import export_dialogs
from app.services.stats import export_stats
from app.workers import inbound as inbound_module
from app.workers.address_ask import address_ask_run
from app.workers.address_catchup import address_autofill_catchup
from app.workers.address_llm import llm_address_read
from app.workers.cards_catchup import catchup_conversation_cards
from app.workers.client_merge import MAX_TRIES as MERGE_MAX_TRIES
from app.workers.client_merge import merge_twins
from app.workers.deliver import deliver_message
from app.workers.geocode import MAX_TRIES as GEOCODE_MAX_TRIES
from app.workers.geocode import autofill_address, geocode_candidate
from app.workers.inbound import inbound_consumer_loop
from app.workers.reconciliation import reconcile_account
from app.workers.transcribe import transcribe_voice
from app.workers.voice_card import voice_card_extract


@with_job_scope
async def smoke_noop(ctx: dict) -> str:  # SM-6: «worker жив и разбирает очередь» (07 §6)
    return "ok"


async def startup(ctx: dict) -> None:
    configure_logging(component="worker")
    init_sentry("worker")  # 05 §7.1: без SENTRY_DSN — no-op
    # ⚠ component="worker" — И ЭТО НЕ КОСМЕТИКА.
    #
    # Здесь стоял вызов без аргумента, то есть с умолчанием "api". Два
    # последствия, оба вскрылись разбором 03.09:
    #
    # 1. Воркеру доставался ПОТОЛОК ЗАПРОСА В 15 СЕКУНД, задуманный только для
    #    веб-процесса («за веб-запросом сидит человек, долгого дела у него
    #    нет»). У воркера дело как раз долгое: обратное заполнение истории и
    #    выгрузка статистики законно идут минутами. Отмен по потолку в бою
    #    ещё не случилось — но случилось бы на первом же большом заполнении.
    # 2. Все соединения подписывались `leadchat-api`. Регламент «кто держит
    #    пул» (05 §8) построен ровно на этой подписи, и различить по ней
    #    api, воркер и планировщик было нельзя: в pg_stat_activity сорок
    #    строк с одним именем.
    db_mod.init_engine(component="worker")
    ctx["db_session_factory"] = db_mod.get_session_factory()
    # ⚠ component="worker" — ЗДЕСЬ ПОТОЛОК СОКЕТА ПОДНЯТ, И ЭТО НЕ МЕЛОЧЬ.
    # Цикл приёма читает поток командой, которая ждёт нарочно (BLOCK 5000).
    # С общим потолком в три секунды каждый холостой заход кончался
    # TimeoutError и переподключением — 163 трассировки за двадцать минут
    # в бою 03.09. Разбор в app/core/redis.py::socket_timeout_for.
    ctx["redis"] = redis_mod.get_client(component="worker")
    ctx["arq"] = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    ctx["shutdown"] = asyncio.Event()
    # Настройки Авито — из базы, а не из `.env`: см. app/main.py::_seed_avito_config.
    # Воркер отправляет ответы клиентам, и уйти они обязаны туда же, куда
    # смотрит система по мнению владельца.
    await avito_app.seed_process(ctx["db_session_factory"], "worker")
    # Настройка лид-бота — тоже из базы. Здесь стояла подмена бэкенда через
    # ctx["bot_ai"]: заданы LEADBOT_URL и LEADBOT_TOKEN — и на лид-бота молча
    # переезжали ВСЕ боты сразу. Так нельзя: у лид-бота свой регламент, свои
    # цены и свои формулировки, и одна переменная окружения не должна менять то,
    # что клиенты читают в чате. Теперь поставщик выбирается у каждого бота
    # (`bots.ai_provider`, по умолчанию Claude), а здесь остаётся только чтение
    # адреса и токена в кэш процесса — чтобы первый же тик после старта работал
    # по настройке владельца, а не по `.env` инженера.
    await leadbot.seed_process(ctx["db_session_factory"], "worker")
    ctx["inbound_task"] = asyncio.create_task(inbound_consumer_loop(ctx))


async def shutdown(ctx: dict) -> None:
    ctx["shutdown"].set()
    ctx["inbound_task"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ctx["inbound_task"]
    # ⚠ УБОРКА ИМЕНИ ПОТРЕБИТЕЛЯ — ЗДЕСЬ, А НЕ В КОНЦЕ САМОГО ЦИКЛА.
    #
    # Сперва она стояла там, и не работала НИ РАЗУ: строкой выше мы делаем
    # `cancel()`, а цикл в этот момент висит на чтении из потока — отмена
    # прилетает внутрь ожидания и выходит наружу, а всё, что написано после
    # `while`, не исполняется. В бою это было видно счётчиком: после выкатки
    # потребителей стало 163 вместо 162, то есть прежний за собой не убрал.
    #
    # Здесь отмена уже позади, соединение с Redis ещё живо — самое место.
    await inbound_module.remove_consumer(ctx["redis"])
    # Общий httpx-клиент к Авито (`integrations/avito/client.py::http_client`).
    # ARQ зовёт on_shutdown, когда задачи в полёте уже дождались, — тёплые
    # соединения больше никому не нужны; закрываем их явно, а не бросаем на
    # смерть процесса, как и остальные пулы ниже.
    await avito_client.close_http_client()
    await ctx["arq"].aclose()
    await redis_mod.close_client()
    await db_mod.dispose_engine()


class WorkerSettings:
    functions = [
        deliver_message,  # 08 §3: доставка исходящих (01 §6.2/§6.3)
        # ⚠ `keep_result=0` — И ЭТО НЕ МЕЛОЧЬ (разбор 03.09).
        #
        # Задача ставится под ПОСТОЯННЫМ именем `enrich:{conversation_id}`, и
        # это правильно: без имени поход в чужой API случался бы на каждое
        # входящее сообщение. Но ARQ хранит под тем же именем РЕЗУЛЬТАТ, и
        # повторная постановка после НЕУДАЧНОЙ попытки молча отбрасывалась,
        # пока результат не истечёт (час по умолчанию). Имя клиента не
        # появлялось в карточке до следующего часа, и не было видно ничего.
        #
        # `keep_result=0` убирает ровно это: пока задача стоит в очереди или
        # выполняется, её ключ существует и дедупликация работает; как только
        # она кончилась — неважно, успехом или отказом, — повтор возможен.
        #
        # Окно в самом имени (`enrich:{id}:{минута // 5}`) решало бы ту же беду
        # хуже: два сообщения по разные стороны границы окна дали бы ДВА
        # похода в Авито. Проверено тестом `test_enrich_enqueued_once_per_client`.
        # `cast` — из-за стабов arq: соседние функции в этом же списке
        # принимаются как есть, а `func()` объявлена строже (`WorkerCoroutine`
        # как Protocol). Поведение от приведения не меняется.
        func(cast(Any, enrich_client), keep_result=0),  # ленивое имя клиента (спринт 2, «а»)
        reconcile_account,
        backfill_account,
        # История ОДНОГО чата, как только он появился во «Входящих» (28.08).
        # Без этой строки задача ставится в очередь и висит в pending: диалог
        # так и остаётся с одной строкой, а виноватого не видно нигде.
        backfill_conversation,
        # Выгрузка статистики (06 §5.1). Тело задачи живёт в services/stats.py —
        # там же метрики; без этой строки POST /stats/export отдаёт 202, а job
        # навсегда остаётся в статусе 'pending'.
        export_stats,
        # Выгрузка «Разбора диалогов» (24.09) — та же схема, тело в
        # services/dialogs_export.py.
        export_dialogs,
        # Движок ботов (02 §2.3–2.4). Без этих двух строк задачи ставятся в
        # очередь и висят в pending: бот молчит, и виноватого не видно нигде.
        bot_step,
        bot_ask_timeout,
        # Расшифровка голосовых своим Whisper (просьба владельца «давай свой
        # Whisper»). Без этой строки задача ставится в очередь и висит в
        # pending: текста под записью не появляется никогда, и виноватого не
        # видно нигде — ровно то же, на чём уже обжигались bot_step и
        # backfill_conversation.
        #
        # ⚠ ЗДЕСЬ ЖЕ ЕДИНСТВЕННОЕ МЕСТО, ГДЕ CTranslate2 ПОПАДАЕТ В ПАМЯТЬ
        # ПРОЦЕССА. Импорт модуля сам по себе модель не грузит (внутри
        # transcribe.py импорт faster_whisper ленивый), а api и планировщик
        # этот модуль не импортируют вовсе.
        # ⚠ `keep_result=0` — ПО ТОЙ ЖЕ ПРИЧИНЕ, ЧТО У `enrich_client` ВЫШЕ, И
        # ЦЕНА ЗДЕСЬ ОКАЗАЛАСЬ БОЛЬШЕ. Задача ставится под постоянным именем
        # `voice:{message_id}` (services/voice.py) — это правильно, иначе один
        # и тот же вебхук расшифровывался бы столько раз, сколько его
        # доставили. Но ARQ хранит под тем же именем РЕЗУЛЬТАТ, и повторная
        # постановка после НЕУДАЧНОЙ попытки молча отбрасывалась целый час.
        # Значит ремонтный прогон (`scheduler/jobs/voice_repair.py`) без этой
        # строки перезапускал бы ровно ничего — и рапортовал бы об успехе.
        func(cast(Any, transcribe_voice), keep_result=0),
        # Проверка адреса по карте и автозапись (11.09). `keep_result=0` — по
        # той же причине, что у `enrich_client`: имя задачи постоянное
        # (`geocode:{id}`), и без этого повтор после неудачи молча
        # отбрасывался бы час. `max_tries=3` явно: на сетевую ошибку задача
        # просит `Retry`, а на третьей попытке пишет `error` сама и отдаёт
        # строку починке планировщика.
        func(cast(Any, geocode_candidate), keep_result=0, max_tries=GEOCODE_MAX_TRIES),
        func(cast(Any, autofill_address), keep_result=0),
        # Догон автозаписи после её включения (проверка 24.09): имя постоянное
        # (`address-autofill-catchup`), `keep_result=0` — иначе следующее
        # включение в течение часа молча не ставилось бы; одна попытка — сбой
        # чтения повторит следующее включение или `address-autofill-backlog`.
        func(cast(Any, address_autofill_catchup), keep_result=0, max_tries=1),
        # Модель читает адрес (13.09): имя задачи по сообщению, одна попытка —
        # модели пробуются по очереди внутри, повтор ничего бы не добавил.
        func(cast(Any, llm_address_read), keep_result=0, max_tries=1),
        # Двойники по телефону (12.09): имя задачи постоянное (`merge:<sha>`),
        # одна попытка — повтор после падения ничего бы не добавил, а ночной
        # проход планировщика и так вернётся к паре.
        func(cast(Any, merge_twins), keep_result=0, max_tries=MERGE_MAX_TRIES),
        # Один вопрос об адресе (18.09). `keep_result=0` — имя задачи постоянное
        # (`addr-ask:{message_id}`), иначе повтор вебхука после отказа молчал бы час;
        # `max_tries=1` — Retry не поднимаем: переходные замки переставляют себя
        # сами новым именем (`attempt`), остальное решит следующее входящее.
        func(cast(Any, address_ask_run), keep_result=0, max_tries=1),
        # N29 (19.09): догон карточки по вставленной истории (`cardcatch:{conv}`).
        # `keep_result=0` — по той же причине, что у `enrich_client`: имя задачи
        # постоянное, и пока жив `arq:result:cardcatch:{conv}`, `enqueue_job` с тем
        # же `_job_id` возвращал бы None — вторая догрузка того же диалога в течение
        # часа (кнопка «Загрузить историю» после `backfill_conversation`) осталась
        # бы без догона. Замок вопроса клиенту (`address_ask._история_едет`) тут ни
        # при чём: он читает только очередь и полёт, результат в него не входит.
        # `max_tries=1` — разбор детерминирован, сбой одной реплики изолирован
        # внутри, повтор упал бы там же.
        func(cast(Any, catchup_conversation_cards), keep_result=0, max_tries=1),
        # Разбор карточки по расшифровке голосового (19.09): ставит `transcribe_voice`
        # после commit'а ГОТОВО под именем `voicecard:{message_id}`. `keep_result=0`
        # — как у `transcribe_voice`: имя постоянное, иначе повтор после отказа
        # молчал бы час, а замок `transcribing` в `address_ask` (`_job_in_flight`)
        # залипал бы после завершения. `max_tries=3`: на сбой соединения задача
        # просит `Retry(defer=30)`; разбор детерминирован — третья попытка упадёт
        # там же и оставит след.
        func(cast(Any, voice_card_extract), keep_result=0, max_tries=3),
        smoke_noop,
    ]
    # ⚠ РЕЗУЛЬТАТ ЗАДАЧИ В ЖУРНАЛ НЕ ПИШЕМ. ARQ по умолчанию печатает
    # возвращаемое значение каждой задачи; у задач адреса это статус-слово, но
    # правило общее: в журнале воркера персональных данных быть не должно.
    log_results = False
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.arq_max_jobs  # ARQ_MAX_JOBS (05 §4)
    job_timeout = 300
    max_tries = 5
    # ⚠ ОПРОС ОЧЕРЕДИ РАЗ В 0,1 С, А НЕ РАЗ В 0,5 (умолчание ARQ). Задача не
    # стартует в момент постановки: воркер находит её на очередном опросе
    # `arq:queue`, и в среднем ждёт полшага. Замер боя 06.09 по журналу за
    # 12–24 ч: от постановки deliver_message до старта p50 0,27 с, p90 0,49
    # (n=1421) — около 45 % всего времени до галочки «доставлено» при p50 0,33 с
    # самой доставки. Цена шага в 0,1 с — десять ZRANGEBYSCORE в секунду к Redis
    # на воркер, это ничто рядом со 158 обращениями к Авито за полминуты.
    # Отметка здоровья от этого чаще не пишется: у неё свой интервал ниже.
    poll_delay = 0.1
    # compose healthcheck: `arq --check app.workers.main.WorkerSettings` (05 §2.2)
    health_check_key = "arq:queue:health-check"
    # ⚠ РАЗ В ПОЛМИНУТЫ, А НЕ РАЗ В ЧАС. По умолчанию ARQ обновляет отметку
    # здоровья раз в `job_timeout`, то есть у нас раз в 300 секунд — а по факту
    # в бою она стояла с момента запуска: за 25 минут работы значение не
    # менялось. Значит зависший воркер до часа считался живым: healthcheck не
    # помечал его нездоровым, сообщения копились в очереди, а сторож молчал.
    # (Перезапуска по healthcheck нет вовсе — docker поднимает только вышедший
    # процесс; нездоровый контейнер видно в `docker ps` и в health/deep.)
    health_check_interval = 30


def registered_job_names() -> set[str]:
    """Имена зарегистрированных задач — независимо от ФОРМЫ записи.

    ⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. В списке ниже задача лежит либо самой корутиной,
    либо `arq.worker.Function` — если ей нужны свои настройки (так записан
    `enrich_client` с `keep_result=0`). У первой имя в `__qualname__`, у второй
    в `.name`, и обращение не к тому полю роняет разбор с `AttributeError`.

    Три отдельных теста наступили на это в один вечер, каждый по-своему.
    Форма записи — дело этого модуля, и знать о ней должен он, а не читатели.
    ARQ, к слову, берёт имя ровно так же: `func()` подставляет `__qualname__`
    корутины, если имя не задано явно.
    """
    имена: set[str] = set()
    for задача in WorkerSettings.functions:
        имя = getattr(задача, "name", None) or getattr(задача, "__qualname__", None)
        if имя:
            имена.add(str(имя))
    return имена
