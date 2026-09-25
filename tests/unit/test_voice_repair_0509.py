"""Расшифровка голосовых: почему она не работала в бою и что теперь её держит.

⚠ ЗАМЕР БОЯ, РАДИ КОТОРОГО НАПИСАН ЭТОТ ФАЙЛ. За всё время работы расшифровка
не отработала НИ РАЗУ: `select voice_transcript_status, count(*) from messages`
на боевом сервере 05.09 → `failed = 1` при одном голосовом всего. В журнале
воркера три попытки подряд с `OSError('I/O error: Permission denied
(os error 13)')` и прямая улика от самого движка загрузки:
`Error logging to file "/srv/app/.cache/huggingface/xet/logs/..."`.

ТРИ НЕЗАВИСИМЫЕ ПОЛОМКИ, И КАЖДОЙ ХВАТАЛО ОДНОЙ, ЧТОБЫ ТЕКСТА НЕ ПОЯВИЛОСЬ:

1. `$HOME` контейнера — каталог кода, принадлежащий root. `download_root`
   уводил на том только сами веса, а движок загрузки писал свой кэш в `$HOME`
   и падал на правах.
2. ARQ хранил результат задачи час под тем же именем, под которым её ставят.
   Повторная постановка после отказа молча отбрасывалась — то есть ремонт был
   невозможен по построению. На этом уже обжигались 03.09 с `enrich_client`.
3. Поставить расшифровку заново умел ровно один путь — конвейер приёма, а он
   срабатывает в момент прихода сообщения и второй раз не срабатывает никогда.

⚠ КАЖДЫЙ СТОРОЖ НИЖЕ ПРОВЕРЕН ДИВЕРСИЕЙ: сломано ровно то, что он стережёт, и
он покраснел. Разбор каждой — в самом тесте.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.config import settings
from app.models import Client, Conversation, Message
from app.scheduler.jobs import voice_repair
from app.services import voice
from app.workers import transcribe

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

ГОЛОСОВОЕ = [
    {
        "media_id": "avito_voice_2229d5a7",
        "kind": "file",
        "name": "Голосовое сообщение",
        "avito_type": "voice",
    }
]


async def сообщение(
    db_sessionmaker: Any,
    account_id: Any,
    *,
    состояние: str | None,
    возраст: timedelta = timedelta(hours=1),
    ключ: str = "a",
) -> tuple[uuid.UUID, datetime]:
    """Входящее голосовое в заданном состоянии расшифровки."""
    created = T0 - возраст
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"vr-{ключ}", name="Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"vr-chat-{ключ}",
            account_id=account_id,
            client_id=cl.id,
            status="new",
        )
        s.add(conv)
        await s.flush()
        msg = Message(
            conversation_id=conv.id,
            direction="in",
            sender_type="client",
            body="",
            attachments=list(ГОЛОСОВОЕ),
            created_at=created,
            voice_transcript_status=состояние,
        )
        s.add(msg)
        await s.flush()
        ид = msg.id
        await s.commit()
    return ид, created


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(111222333)


# --- 1. Домашний каталог: та самая причина боевого отказа ----------------------


def test_дом_hugging_face_уведён_на_том_с_весами() -> None:
    """⚠ ДИВЕРСИЯ: убрать строку `os.environ.setdefault("HF_HOME", ...)` —
    тест краснеет.

    Это и есть боевой отказ. `download_root` уводит на том только веса; свой
    кэш и журнал движок загрузки пишет в `$HOME/.cache/huggingface/`, а `$HOME`
    в образе — каталог кода, принадлежащий root. Замер на сервере: `test -w
    $HOME` → нет, `test -w /var/leadchat/whisper` → да.
    """
    прежний = os.environ.pop("HF_HOME", None)
    try:
        путь = transcribe._домашний_каталог_hugging_face()
        assert путь.startswith(settings.whisper_model_dir), (
            f"дом HuggingFace ({путь}) обязан лежать на том же томе, что и веса "
            f"({settings.whisper_model_dir}): только он доступен на запись"
        )
        assert os.environ["HF_HOME"] == путь, "переменная обязана быть выставлена, а не вычислена"
    finally:
        os.environ.pop("HF_HOME", None)
        if прежний is not None:
            os.environ["HF_HOME"] = прежний


def test_заданный_снаружи_дом_уважаем() -> None:
    """`setdefault`, а не присвоение: иначе на машине разработчика, где тома
    `/var/leadchat` нет, увести кэш было бы нечем, кроме правки кода.
    """
    прежний = os.environ.get("HF_HOME")
    os.environ["HF_HOME"] = "/tmp/чужой-дом"
    try:
        assert transcribe._домашний_каталог_hugging_face() == "/tmp/чужой-дом"
    finally:
        os.environ.pop("HF_HOME", None)
        if прежний is not None:
            os.environ["HF_HOME"] = прежний


def test_дом_выставлен_до_импорта_библиотеки() -> None:
    """⚠ ДИВЕРСИЯ: переставить вызов `_домашний_каталог_hugging_face()` ПОСЛЕ
    `from faster_whisper import WhisperModel` — тест краснеет.

    И это не придирка к порядку строк. `huggingface_hub` читает HF_HOME на
    своём импорте и замораживает путь в константах модуля: выставь переменную
    после — она уже ни на что не повлияет. Получилось бы «написано, но не
    подключено» в самом коварном виде: код на месте, а в бою всё то же
    Permission denied.

    Проверяем по исходнику, потому что проверить порядок иначе нечем: импорт
    внутри функции происходит один раз на процесс, и в тесте библиотека может
    быть уже загружена соседним тестом.
    """
    import inspect

    текст = inspect.getsource(transcribe._загрузить_модель)
    вызов = текст.index("_домашний_каталог_hugging_face()")
    импорт = текст.index("from faster_whisper import")
    assert вызов < импорт, (
        "HF_HOME обязан выставляться ДО импорта faster_whisper: библиотека "
        "читает переменную на импорте и больше к ней не возвращается"
    )


# --- 2. Отбор: границы ремонтного прогона -------------------------------------


async def test_ремонт_берёт_несостоявшуюся_расшифровку(
    db_sessionmaker: Any, db: Any, account: Any
) -> None:
    ид, _ = await сообщение(db_sessionmaker, account.id, состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО)
    найдено = await voice_repair.найти_нерасшифрованные(db, now=T0)
    assert [r[0] for r in найдено] == [ид]


@pytest.mark.parametrize(
    "состояние",
    [voice.СОСТОЯНИЕ_ГОТОВО, voice.СОСТОЯНИЕ_В_РАБОТЕ, voice.СОСТОЯНИЕ_СЛИШКОМ_ДЛИННАЯ],
)
async def test_ремонт_не_трогает_остальные_состояния(
    db_sessionmaker: Any, db: Any, account: Any, состояние: str | None
) -> None:
    """⚠ ДИВЕРСИЯ: заменить условие на `!= СОСТОЯНИЕ_ГОТОВО` — тест краснеет
    на двух состояниях из трёх.

    Каждое лишнее здесь стоит своего: `running` — это работающая прямо сейчас
    задача, и вторая пойдёт считать ту же запись; `too_long` — заведомо
    бесполезные минуты процессора, повтор упрётся в тот же порог.

    NULL из списка ушёл 06.09 намеренно: «не начинали» теперь берётся — это
    досчёт накопленного (458 голосовых без текста по замеру), но только у
    входящих с голосовым вложением; границы того отбора — в
    `test_voice_backfill_0609.py`.
    """
    await сообщение(db_sessionmaker, account.id, состояние=состояние)
    assert await voice_repair.найти_нерасшифрованные(db, now=T0) == []


async def test_ремонт_не_берёт_старое(db_sessionmaker: Any, db: Any, account: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать условие `created_at >= порог` — тест краснеет.

    Граница держит две вещи разом. Смысловую: запись у Авито живёт долго, но
    не вечно (замер 06.09 — отдаётся на 29-й день; старше 45 дней проверить
    нечем), и за границей прогон возил бы по кругу безнадёжное вечно —
    счётчика попыток нет, границу держит только возраст. И нагрузочную:
    `messages` разбита на 28 помесячных партиций, и без условия на дату этот
    отбор читал бы их все каждые десять минут.

    Здесь стояло «ссылка живёт минуты, трёхдневное кончится отказом» — это
    было про подписанную ссылку, а не про запись, и было неверно.
    """
    await сообщение(
        db_sessionmaker,
        account.id,
        состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО,
        возраст=voice_repair.НЕ_СТАРШЕ + timedelta(hours=1),
    )
    assert await voice_repair.найти_нерасшифрованные(db, now=T0) == []


async def test_ремонт_берёт_порцией_и_начиная_с_новых(
    db_sessionmaker: Any, db: Any, account: Any
) -> None:
    """Расшифровка идёт по одной на весь процесс — в том же процессе живёт
    приём вебхуков. Вываленная разом сотня заняла бы очередь на часы.

    ⚠ ПОРЯДОК ПЕРЕВЁРНУТ 06.09: было «старые вперёд — они ближе к порогу», и
    это держалось на ложном «ссылка живёт минуты». Запись у Авито живёт ≥29
    дней, старым спешить некуда, а новые — это диалоги, в которых прямо
    сейчас работает человек. Диверсия — вернуть `order_by(created_at)` без
    `desc()` — краснеет здесь.
    """
    for i in range(voice_repair.ПОРЦИЯ + 5):
        await сообщение(
            db_sessionmaker,
            account.id,
            состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО,
            возраст=timedelta(minutes=i + 1),
            ключ=f"p{i}",
        )
    найдено = await voice_repair.найти_нерасшифрованные(db, now=T0)
    assert len(найдено) == voice_repair.ПОРЦИЯ
    assert найдено == sorted(найдено, key=lambda r: r[1], reverse=True), (
        "новые вперёд: живой диалог важнее месячного, а запись у Авито никуда не денется"
    )


# --- 3. Постановка: отказ очереди обязан быть виден ----------------------------


async def test_ремонтная_задача_встаёт_рядом_с_исходной(redis: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать суффикс `:repair` из имени — тест краснеет.

    Имя задачи дедуплицирует постановку, и это правильно. Но ремонт под тем же
    именем наткнулся бы на ещё живую первую задачу и тихо не сделал бы ничего —
    ровно та беда, которую чинит `keep_result=0`, только с другой стороны.
    """
    ид, created = uuid.uuid4(), T0
    assert await voice.enqueue_transcribe(redis, ид, created) is True
    assert await voice.enqueue_transcribe(redis, ид, created, повтор=True) is True
    ключи = sorted(k for k in await redis.keys("arq:job:voice:*"))
    assert ключи == [f"arq:job:voice:{ид}", f"arq:job:voice:{ид}:repair"]


async def test_отказ_очереди_виден_вызывающему(redis: Any) -> None:
    """⚠ ДИВЕРСИЯ: вернуть `None` вместо `job is not None` — тест краснеет.

    ARQ отказывает МОЛЧА: при занятом имени `enqueue_job` отдаёт None, не
    бросая ничего. Без этого различия ремонтный прогон записал бы в журнал
    «перезапустил двадцать», не перезапустив ни одной, — и отказ очереди
    остался бы невидимым ровно так же, как оставался невидимым отказ прав.
    """
    ид, created = uuid.uuid4(), T0
    assert await voice.enqueue_transcribe(redis, ид, created, повтор=True) is True
    assert await voice.enqueue_transcribe(redis, ид, created, повтор=True) is False, (
        "вторая постановка под тем же именем не встала — вызывающий обязан это узнать"
    )


async def test_результат_задачи_не_запирает_повтор() -> None:
    """⚠ ДИВЕРСИЯ: убрать `keep_result=0` у `transcribe_voice` — тест краснеет.

    Это второй из трёх боевых замков, и он же самый незаметный. ARQ хранит
    результат под именем задачи час; пока результат жив, повторная постановка
    молча отбрасывается. То есть ремонт был невозможен ПО ПОСТРОЕНИЮ, сколько
    бы раз его ни звали. На этом уже обжигались 03.09 с `enrich_client` —
    тогда в карточке не появлялось имя клиента.
    """
    from app.workers.main import WorkerSettings

    задача = next(
        f
        for f in WorkerSettings.functions
        if getattr(f, "name", getattr(f, "__qualname__", "")) == voice.TRANSCRIBE_JOB
    )
    assert getattr(задача, "keep_result_s", None) == 0, (
        "результат расшифровки нельзя хранить под именем задачи: он запирает повтор"
    )


# --- 4. Прогон целиком --------------------------------------------------------


async def test_прогон_ставит_задачи_и_считает_вставшие(
    db_sessionmaker: Any, redis: Any, account: Any, monkeypatch: Any
) -> None:
    """⚠ ДИВЕРСИЯ: не регистрировать `voice_repair` в `scheduler/main.py` —
    краснеет соседний тест про регистрацию. Здесь проверяется само тело.
    """
    ид, _ = await сообщение(db_sessionmaker, account.id, состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО)

    monkeypatch.setattr(voice_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(voice_repair.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(
        voice_repair, "НЕ_СТАРШЕ", datetime.now(UTC) - T0 + timedelta(days=2), raising=False
    )

    assert await voice_repair.repair_transcripts() == 1
    assert await redis.keys(f"arq:job:voice:{ид}:repair")


def test_прогон_зарегистрирован_в_планировщике() -> None:
    """⚠ ДИВЕРСИЯ: убрать `voice_repair_jobs.register(scheduler)` — краснеет.

    Ровно та поломка, на которой уже обжигались `bot_step`,
    `backfill_conversation` и `transcribe_voice`: код написан целиком, а в бою
    не зовётся никогда, и виноватого не видно нигде.
    """
    from app.scheduler.main import build_scheduler

    планировщик = build_scheduler()
    assert планировщик.get_job(voice_repair.JOB_ID) is not None


def test_первый_заход_не_ждёт_интервала() -> None:
    """⚠ ДИВЕРСИЯ: убрать `start_date` из триггера — тест краснеет.

    Без него прогон мог бы не выполниться почти никогда. IntervalTrigger
    отсчитывает первый заход от старта процесса, то есть через интервал, а
    планировщик перезапускается КАЖДОЙ выкаткой — по замеру их бывает 9–22 в
    сутки. В плотный день отсчёт сбрасывался бы раньше, чем добегал до конца,
    и починенная среда так и не встретилась бы с ремонтом.
    """
    from datetime import UTC, datetime

    from app.scheduler.main import build_scheduler

    # Спрашиваем сам триггер, когда он сработал бы: `next_run_time` появляется
    # у задания только после запуска планировщика, а запускать его в тесте
    # значило бы поднять фоновый поток ради одного числа.
    задание = build_scheduler().get_job(voice_repair.JOB_ID)
    сейчас = datetime.now(UTC)
    ждать = (задание.trigger.get_next_fire_time(None, сейчас) - сейчас).total_seconds()
    assert 0 < ждать <= voice_repair.ПЕРВЫЙ_ЗАХОД.total_seconds() + 5, (
        f"первый заход через {ждать:.0f} с: выкатка перезапустит планировщик раньше"
    )
