"""Отстойник недоставленных вебхуков РАЗБИРАЕТСЯ, а не только считается (FUNC-53).

ЧТО ЛЕЖИТ В `webhooks:avito:dlq`. Вебхук, который не удалось обработать пять
раз подряд. Это не мусор и не отладочный слепок: это входящее сообщение живого
клиента, которое до диспетчера не дошло.

ЧЕГО НЕ БЫЛО. 11 августа длину потока показали в `/health/deep` — и это
закрыло ровно половину беды: потери перестали быть невидимыми. ЧИТАТЬ поток
по-прежнему было некому. Сообщения копились в Redis навсегда, а единственным
способом их достать оставался человек с `redis-cli` и знанием формата записи.
Система умела сказать «потеряно 14 обращений» и не умела с ними ничего
сделать.

ПОЧЕМУ ПОВТОР, А НЕ КНОПКА. Подавляющее большинство записей попадает туда по
ВРЕМЕННОЙ причине: база была недоступна пять минут, Авито отвечал 500, воркер
перезапускали. Через полчаса те же записи обрабатываются с первого раза, и
ждать ради этого человеческого нажатия — значит терять обращения по выходным.
Обработка идемпотентна, поэтому повтор безопасен.

Проверки здесь про четыре разных способа всё испортить: не поднять поднимаемое,
поднять и не убрать (вечный повтор), убрать не подняв (потеря молча) и
гонять неразбираемое по кругу до конца света.
"""

import json
from datetime import UTC, datetime

import pytest

from app.workers import inbound as worker

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 771001
DLQ = worker.DLQ


def _webhook(message_id: str = "dlq-am-1", chat_id: str = "dlq-chat-1") -> dict:
    """Тело вебхука v3 — ровно в том виде, в каком его кладёт шлюз."""
    return {
        "payload": {
            "type": "message",
            "value": {
                "id": message_id,
                "chat_id": chat_id,
                "author_id": 880002,
                "user_id": AVITO_USER_ID,
                "created": int(datetime(2026, 8, 11, 9, 0, tzinfo=UTC).timestamp()),
                "content": {"text": "Холодильник не морозит, приедете сегодня?"},
            },
        }
    }


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def ctx(redis, db_sessionmaker):
    """Окружение воркера: ровно то, что кладёт в него `workers/main.startup`."""
    return {"redis": redis, "db_session_factory": db_sessionmaker}


async def _drop_to_dlq(redis, account, payload: dict, *, orig_id: str = "1691000000000-0") -> str:
    """Положить запись в отстойник так же, как это делает `_reclaim_stuck`."""
    return await redis.xadd(
        DLQ,
        {
            "account_id": str(account.id),
            "payload": json.dumps(payload),
            "orig_id": orig_id,
            "failed_at": "1691000000",
        },
    )


@pytest.fixture
def processed(monkeypatch):
    """Подменённая обработка записи: собирает то, что ей отдали, и не падает.

    ПОЧЕМУ ПОДМЕНА, А НЕ НАСТОЯЩИЙ ПРОХОД ДО БАЗЫ. `process_webhook_entry`
    первым делом пишет строку в `webhook_raw_log`, а у той первичный ключ
    объявлен как `Identity(always=True)` — это PostgreSQL. Юнит-набор строит
    схему на SQLite, где такая колонка получается просто `BIGINT NOT NULL`
    без автонумерации, и любая вставка падает на `NOT NULL`. Проверено на
    чистом дереве: ограничение существует давно и к разбору отстойника
    отношения не имеет — сквозной путь вебхука здесь недоступен в принципе, и
    живёт он в интеграционном наборе на настоящем PostgreSQL.

    Проверять поэтому остаётся ровно то, за что отвечает сам разбор: КОМУ он
    отдаёт запись, С КАКИМ идентификатором и что делает с потоком по итогу.
    """
    seen: list[tuple[str, dict]] = []

    async def fake(_ctx, entry_id, fields):
        seen.append((entry_id, dict(fields)))

    monkeypatch.setattr(worker, "process_webhook_entry", fake)
    return seen


class TestWhatCanBeRecoveredIsRecovered:
    async def test_a_lost_message_goes_back_through_the_normal_path(
        self, ctx, redis, account, processed
    ):
        """Главная проверка файла: обращение из отстойника снова обрабатывается.

        И обрабатывается ТЕМ ЖЕ путём, что живое, — со своим телом и своим
        аккаунтом. Причина попадания туда была временной (база лежала, Авито
        отвечал 500); теперь она прошла, и то же тело пройдёт с первого раза.
        """
        await _drop_to_dlq(redis, account, _webhook())

        recovered, parked = await worker.replay_dlq(ctx)

        assert (recovered, parked) == (1, 0)
        assert len(processed) == 1
        _entry_id, fields = processed[0]
        assert fields["account_id"] == str(account.id)
        assert json.loads(fields["payload"])["payload"]["value"]["id"] == "dlq-am-1"

    async def test_a_recovered_entry_leaves_the_stream(self, ctx, redis, account, processed):
        """Поднятое обращение из отстойника уходит.

        Иначе `/health/deep` вечно показывал бы потери, которых уже нет, а
        каждый следующий заход разбирал бы одно и то же по кругу.
        """
        await _drop_to_dlq(redis, account, _webhook())

        await worker.replay_dlq(ctx)

        assert await redis.xlen(DLQ) == 0

    async def test_the_original_stream_id_is_used(self, ctx, redis, account, processed):
        """Повтор идёт под ИСХОДНЫМ идентификатором записи потока.

        По нему в `webhook_raw_log` лежит строка с сырым телом, и повтор
        обязан дописать результат В НЕЁ, а не завести вторую запись про то же
        сообщение клиента: иначе разбор «что нам прислали» показывал бы одно
        обращение дважды с разными пометками.
        """
        dlq_id = await _drop_to_dlq(redis, account, _webhook(), orig_id="1691000000000-7")

        await worker.replay_dlq(ctx)

        (entry_id, _fields) = processed[0]
        assert entry_id == "1691000000000-7"
        assert entry_id != dlq_id


class TestNothingIsThrownAwayUnprocessed:
    async def test_a_failing_entry_stays_in_the_stream(self, ctx, redis, account, monkeypatch):
        """Не обработалось — запись НА МЕСТЕ.

        Порядок «сперва обработка, потом XDEL» здесь и проверяется: обратный
        в момент падения воркера терял бы обращение окончательно и молча —
        ровно то, ради чего отстойник и заводили.
        """

        async def still_broken(*_a, **_kw):
            raise RuntimeError("база всё ещё недоступна")

        monkeypatch.setattr(worker, "process_webhook_entry", still_broken)
        await _drop_to_dlq(redis, account, _webhook())

        recovered, parked = await worker.replay_dlq(ctx)

        assert (recovered, parked) == (0, 0)
        assert await redis.xlen(DLQ) == 1

    async def test_an_unrecoverable_entry_is_parked_after_a_few_tries(
        self, ctx, redis, account, monkeypatch
    ):
        """Неразбираемое откладывается, а не гоняется по кругу вечно.

        Есть род записей, которые не обработаются никогда: диалог ссылается на
        удалённый аккаунт, поле не лезет в колонку. Ровный поток ошибок из-за
        них — это журнал, в котором перестают замечать настоящие поломки.
        Запись при этом ОСТАЁТСЯ в потоке: молча мы не удаляем ничего.
        """
        attempts = 0

        async def always_broken(*_a, **_kw):
            nonlocal attempts
            attempts += 1
            raise ValueError("это тело не разберётся никогда")

        monkeypatch.setattr(worker, "process_webhook_entry", always_broken)
        await _drop_to_dlq(redis, account, _webhook())

        for _ in range(worker.DLQ_MAX_REPLAYS + 3):
            await worker.replay_dlq(ctx)

        assert attempts == worker.DLQ_MAX_REPLAYS, "попытки не ограничены"
        assert await redis.xlen(DLQ) == 1, "запись пропала, хотя её никто не разобрал"
        _recovered, parked = await worker.replay_dlq(ctx)
        assert parked == 1, "отложенное обязано попадать в отчёт как «разобрать руками»"

    async def test_parked_entries_do_not_hide_the_ones_behind_them(
        self, ctx, redis, account, monkeypatch
    ):
        """Отложенные лежат в НАЧАЛЕ потока и не имеют права закрывать собой остальное.

        Они самые старые — их туда положили первыми. Бери заход просто первые
        N записей, и десяток неразбираемых навсегда закрыл бы всё, что легло
        позже: сегодняшнее обращение, попавшее в отстойник из-за
        пятиминутного простоя базы, не разобралось бы никогда — при том что
        оно-то как раз поднимается.
        """
        monkeypatch.setattr(worker, "DLQ_REPLAY_BATCH", 2)
        broken: list[str] = []

        async def only_the_old_ones_fail(_ctx, entry_id, _fields):
            if entry_id.startswith("dead"):
                broken.append(entry_id)
                raise ValueError("это тело не разберётся никогда")

        monkeypatch.setattr(worker, "process_webhook_entry", only_the_old_ones_fail)

        for i in range(3):
            await _drop_to_dlq(redis, account, _webhook(), orig_id=f"dead-{i}")
        # Пока «мёртвые» набирают попытки, живой записи ещё нет — как в жизни:
        # она попадёт в отстойник позже, уже после них.
        for _ in range(worker.DLQ_MAX_REPLAYS):
            await worker.replay_dlq(ctx)
        assert await redis.xlen(DLQ) == 3

        await _drop_to_dlq(redis, account, _webhook("живое"), orig_id="alive-1")
        recovered, parked = await worker.replay_dlq(ctx)

        assert recovered == 1, "живое обращение не разобрано — его закрыли отложенные"
        # Отложенных двое, а не трое: попыток за заход всего две, и третья
        # «мёртвая» их всё это время не получала — свои она доберёт позже.
        # Важно здесь другое: живая запись разобрана НЕ ДОЖИДАЯСЬ, пока
        # разберутся все мёртвые перед ней.
        assert parked == 2
        assert await redis.xlen(DLQ) == 3, "в потоке должны остаться только неразобранные"

    async def test_an_empty_stream_costs_nothing(self, ctx, redis):
        """Пустой отстойник — обычное состояние системы, и он не должен ни падать,
        ни ходить в базу."""
        assert await worker.replay_dlq(ctx) == (0, 0)


class TestTheReplayDoesNotDisturbTodaysTraffic:
    async def test_only_one_worker_replays_at_a_time(self, ctx, redis, account, monkeypatch):
        """Реплики делят основной поток группой, а у отстойника группы нет.

        Без лока каждая реплика тащила бы те же записи, и они обрабатывались
        бы по числу реплик. Дублей не будет (вставка идемпотентна), но работа
        и запросы в базу умножатся на ровном месте.
        """
        runs = 0

        async def counting_replay(_ctx):
            nonlocal runs
            runs += 1
            return 0, 0

        monkeypatch.setattr(worker, "replay_dlq", counting_replay)
        # Держатель лока — «другая реплика».
        await redis.set(worker.DLQ_REPLAY_LOCK, "1", ex=60)

        await worker._maybe_replay_dlq(ctx, last_run=-worker.DLQ_REPLAY_EVERY_SEC * 2)

        assert runs == 0

    async def test_a_broken_replay_does_not_stop_the_intake(self, ctx, redis, monkeypatch):
        """Разбор старых потерь не имеет права остановить приём СЕГОДНЯШНИХ сообщений.

        Он идёт в том же цикле, что и чтение потока: исключение отсюда унесло
        бы виток целиком, и живые обращения ждали бы следующего.
        """

        async def boom(_ctx):
            raise RuntimeError("Redis моргнул посреди разбора")

        monkeypatch.setattr(worker, "replay_dlq", boom)

        await worker._maybe_replay_dlq(ctx, last_run=-worker.DLQ_REPLAY_EVERY_SEC * 2)

        # Лок снят: иначе следующий заход не состоялся бы никогда.
        assert await redis.get(worker.DLQ_REPLAY_LOCK) is None

    async def test_the_replay_waits_out_its_interval(self, ctx, monkeypatch):
        """Заход — раз в промежуток, а не на каждом витке цикла.

        Виток идёт раз в пять секунд ожидания потока; разбирать отстойник так
        же часто значит на каждой из них читать поток и трогать Redis ради
        записей, которые не изменятся.
        """
        runs = 0

        async def counting_replay(_ctx):
            nonlocal runs
            runs += 1
            return 0, 0

        monkeypatch.setattr(worker, "replay_dlq", counting_replay)

        import time as _time

        just_now = _time.monotonic()
        assert await worker._maybe_replay_dlq(ctx, last_run=just_now) == just_now
        assert runs == 0


class TestTheStreamNameIsShared:
    def test_the_worker_and_the_healthcheck_look_at_one_stream(self) -> None:
        """Разъехавшиеся литералы дали бы «в отстойнике пусто» при полном отстойнике.

        Проверка и разбор смотрели бы в разные ключи, и ни то ни другое не
        падало бы: оба честно отвечали бы про свой.
        """
        from app.api.routes import health

        assert health.DLQ == worker.DLQ
