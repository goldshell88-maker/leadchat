"""Досчёт накопленных голосовых и живая доставка текста на экран (06.09).

⚠ ЗАМЕР БОЯ, РАДИ КОТОРОГО НАПИСАН ЭТОТ ФАЙЛ. Входящих голосовых без
расшифровки за 30 дней — 458 (до суток 3, 1–2 дня 31, 2–7 дней 97, 7–30 дней
327). Скриншот владельца: закрытый диалог, два голосовых (0:14 и 0:18) и ни
строчки текста, оператор пишет клиенту «аудио не грузит, можете написать
пожалуйста». В коде при этом стояло «у старых ссылки протухли, записи у нас
нет ни байта, расшифровать нечем» — и досчёт брал только `failed` не старше
двух суток. Замер 06.09 через `getVoiceFiles`: Авито отдаёт запись на
голосовые от 0,3 ч до ~29 дней, все до единого. Протухает подписанная ССЫЛКА
(минуты), а не ЗАПИСЬ.

Вторая половина — кадр `message:transcript`. Текст ехал только вместе с
сообщением: подпись «готовится» висела до следующего открытия диалога, а под
NULL было пусто — ровно то, что увидел владелец.

⚠ КАЖДЫЙ СТОРОЖ НИЖЕ ПРОВЕРЕН ДИВЕРСИЕЙ: сломано ровно то, что он стережёт,
и он покраснел. Разбор каждой — в самом тесте.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models import Client, Conversation, Message
from app.scheduler.jobs import voice_repair
from app.services import voice
from app.workers import transcribe
from tests.unit.conftest import drain_events

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

#: Так голосовое лежит в `attachments` после разбора вебхука.
ГОЛОСОВОЕ = [
    {
        "media_id": "avito_voice_2229d5a7",
        "kind": "file",
        "name": "Голосовое сообщение",
        "avito_type": "voice",
    }
]

#: Фото — тоже вложение, но не голос: досчёт обязан его не трогать.
СНИМОК = [{"media_id": "img-1", "kind": "image", "name": "фото", "avito_type": "image"}]


async def сообщение(
    db_sessionmaker: Any,
    account_id: Any,
    *,
    состояние: str | None = None,
    возраст: timedelta = timedelta(hours=1),
    direction: str = "in",
    вложения: list[Any] | None = None,
    текст: str | None = None,
    ключ: str | None = None,
) -> tuple[uuid.UUID, datetime]:
    """Сообщение в заданном состоянии расшифровки; по умолчанию — входящее
    голосовое, к которому не приступали (NULL) — тот самый хвост."""
    ключ = ключ or uuid.uuid4().hex[:8]
    created = T0 - возраст
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"vb-{ключ}", name="Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"vb-chat-{ключ}",
            account_id=account_id,
            client_id=cl.id,
            status="new",
        )
        s.add(conv)
        await s.flush()
        msg = Message(
            conversation_id=conv.id,
            direction=direction,
            sender_type="client" if direction == "in" else "operator",
            body=текст,
            attachments=list(ГОЛОСОВОЕ if вложения is None else вложения),
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


# --- 1. Отбор: что берём ------------------------------------------------------


async def test_не_начатое_входящее_голосовое_берётся(
    db_sessionmaker: Any, db: Any, account: Any
) -> None:
    """⚠ ДИВЕРСИЯ: убрать ветку `voice_transcript_status.is_(None)` из `or_` —
    тест краснеет. Это и есть весь хвост из 458: до 05.09 расшифровка не
    работала, конвейер приёма второй раз задачу не ставит, и без этой ветки
    хвост остался бы без текста навсегда.
    """
    ид, _ = await сообщение(db_sessionmaker, account.id, состояние=None)
    assert [r[0] for r in await voice_repair.найти_нерасшифрованные(db, now=T0)] == [ид]


async def test_не_вышедшее_по_прежнему_берётся(db_sessionmaker: Any, db: Any, account: Any) -> None:
    """Досчёт хвоста не отменяет ремонта после починки среды."""
    ид, _ = await сообщение(db_sessionmaker, account.id, состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО)
    assert [r[0] for r in await voice_repair.найти_нерасшифрованные(db, now=T0)] == [ид]


# --- 2. Отбор: что НЕ берём, и почему это обязано стоять в SQL ----------------


@pytest.mark.parametrize(
    ("вложения", "текст"),
    [([], "Здравствуйте, почём ремонт?"), (СНИМОК, None)],
    ids=["текст", "фото"],
)
async def test_текстовое_с_null_не_берётся(
    db_sessionmaker: Any, db: Any, account: Any, вложения: list[Any], текст: str | None
) -> None:
    """⚠ ДИВЕРСИЯ: убрать `voice.has_voice_sql(db)` из `where` — краснеет на
    обоих случаях.

    ⚠ ЭТО ГЛАВНЫЙ СТОРОЖ ФАЙЛА. NULL в состоянии стоит у КАЖДОЙ строки без
    расшифровки — у всех 317 тысяч текстовых тоже. Пропусти отбор их до
    Python — порция из двадцати уходила бы на пустышки (задача честно
    отступила бы на «не голосовое», НЕ меняя состояния), и те же двадцать
    пустышек, будучи самыми новыми, приходили бы КАЖДЫЙ прогон. До голосовых
    очередь не дошла бы никогда — а по журналу прогон «работал».
    """
    await сообщение(db_sessionmaker, account.id, вложения=вложения, текст=текст)
    assert await voice_repair.найти_нерасшифрованные(db, now=T0) == []


def test_условие_по_вложению_стоит_в_sql_и_на_postgresql_это_jsonb() -> None:
    """⚠ ДИВЕРСИЯ 1: вернуть в ветке PostgreSQL `LIKE` вместо `@>` — краснеет.
    ⚠ ДИВЕРСИЯ 2: операнд `{"avito_type": "voice"}` (объект) вместо
    `[{"avito_type": "voice"}]` (массив) — краснеет на операнде.
    ⚠ ДИВЕРСИЯ 3: операнд с `avito_type: image` — краснеет на операнде.

    Юниты идут на SQLite, и ветка PostgreSQL иначе не проверяется ничем:
    можно было бы сломать её на бою, оставив все тесты зелёными. Здесь
    условие компилируется диалектом PostgreSQL без соединения — видно и
    оператор, и значение параметра.

    ⚠ ОПЕРАНД ПРОВЕРЯЕТСЯ НЕ ИЗ ПЕДАНТИЗМА. До 06.09 этот тест стерёг только
    оператор, и диверсия 2 оставалась ЗЕЛЁНОЙ. Замер на PostgreSQL 16 (docker,
    06.09), шесть строк — голосовое, фото, пустой список, фото+голос, голос с
    пустым `media_id`, слово «voice» в имени файла: с массивом `@>` даёт
    t/f/f/t/t/f, ровно как задумано; с объектом — f на ВСЕХ ШЕСТИ. То есть
    та поломка обнуляла бы весь досчёт в бою молча: прогон «работал»,
    находил ноль, и 458 голосовых оставались бы без текста навсегда.
    """
    from sqlalchemy.dialects import postgresql

    ложная_сессия = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
    условие = voice.has_voice_sql(ложная_сессия)  # type: ignore[arg-type]
    скомпилировано = условие.compile(dialect=postgresql.dialect())
    sql = str(скомпилировано)
    assert "@>" in sql, f"на PostgreSQL вложение ищется вхождением jsonb, а не подстрокой: {sql}"
    assert "LIKE" not in sql.upper()
    assert list(скомпилировано.params.values()) == [[{"avito_type": "voice"}]], (
        "операнд `@>` обязан быть МАССИВОМ с объектом: `attachments` — список, "
        "и массив не «содержит» голый объект (замер PG 16: f на всех строках)"
    )


async def test_запрос_несёт_предикат_частичного_индекса() -> None:
    """⚠ ДИВЕРСИЯ: убрать ЛЮБОЕ одно из `direction == "in"` и
    `sender_type == "client"` — тест краснеет.

    Это сторож ФОРМЫ, и он здесь потому, что поведение проверить нечем:
    смысл пары не в отборе (у входящих `sender_type` всегда `client`, и
    `test_исходящее_голосовое_не_берётся` остаётся зелёным при снятии любого
    одного условия — проверено), а в частичном индексе `idx_messages_client_in`
    из миграции 0004 с предикатом `direction = 'in' AND sender_type =
    'client'`. Потеряй запрос одно из двух — планировщик не сможет взять
    индекс, и раз в десять минут отбор читал бы партицию целиком. На SQLite
    юнитов индекса нет, EXPLAIN на бою не гоняли (правило: боевой сервер не
    трогать), — остаётся смотреть на сам запрос, уходящий в PostgreSQL.
    """
    from sqlalchemy.dialects import postgresql

    перехвачено: list[Any] = []

    async def execute(stmt: Any) -> Any:
        перехвачено.append(stmt)
        return SimpleNamespace(all=lambda: [])

    ложная_сессия = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()), execute=execute
    )
    await voice_repair.найти_нерасшифрованные(ложная_сессия, now=T0)

    (запрос,) = перехвачено
    скомпилировано = запрос.compile(dialect=postgresql.dialect())
    sql = str(скомпилировано)
    assert "messages.direction = " in sql and "messages.sender_type = " in sql, sql
    значения = {v for v in скомпилировано.params.values() if isinstance(v, str)}
    assert {"in", "client"} <= значения, (
        f"пара предиката индекса idx_messages_client_in обязана уходить в запрос: {значения}"
    )


async def test_исходящее_голосовое_не_берётся(db_sessionmaker: Any, db: Any, account: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать ПАРУ `direction == "in"` и `sender_type == "client"`
    — тест краснеет. Убрать только `direction` — остаётся ЗЕЛЁНЫМ (проверено):
    у исходящих `sender_type` — `operator`/`bot`, и второе условие перекрывает
    первое. Пара избыточна по построению и стоит ради частичного индекса
    `idx_messages_client_in` (миграция 0004), а не ради смысла.

    Свои слова мы знаем текстом; задача расшифровки отступает от исходящих
    сама, но отступает МОЛЧА — состояние остаётся NULL, и такое сообщение
    занимало бы место в порции каждый прогон.
    """
    await сообщение(db_sessionmaker, account.id, direction="out")
    assert await voice_repair.найти_нерасшифрованные(db, now=T0) == []


async def test_старше_тридцати_суток_не_берётся(
    db_sessionmaker: Any, db: Any, account: Any
) -> None:
    """⚠ ДИВЕРСИЯ: убрать `Message.created_at >= порог` — тест краснеет.

    Граница — 30 суток, и она по замеру ЗАПИСИ, а не ссылки: 06.09 Авито отдал
    запись на голосовые возрастом до ~29 дней, все до единого; старше 45 дней
    в базе сообщений нет, проверить нечем. Двое суток, стоявшие раньше, были
    про подписанную ссылку — и отсекали 424 из 458 записей хвоста, у которых
    запись у Авито ещё есть.
    """
    assert voice_repair.НЕ_СТАРШЕ == timedelta(days=30), "граница — по замеру 06.09"
    await сообщение(db_sessionmaker, account.id, возраст=timedelta(days=30, hours=1))
    месячное, _ = await сообщение(db_sessionmaker, account.id, возраст=timedelta(days=29))
    найдено = await voice_repair.найти_нерасшифрованные(db, now=T0)
    assert [r[0] for r in найдено] == [месячное], (
        "29-дневное — берём (замер: запись есть), 30 суток и час — нет"
    )


async def test_канал_не_в_работе_не_берётся(
    db_sessionmaker: Any, db: Any, make_avito_account: Any
) -> None:
    """⚠ ДИВЕРСИЯ: убрать `AvitoAccount.status == "active"` — тест краснеет.

    По сообщениям канала в `needs_reauth` расшифровка кончается отказом
    «канал требует переподключения» — и раз счётчика попыток нет, она
    повторялась бы каждые десять минут по КАЖДОМУ голосовому канала месяц
    подряд: при десяти голосовых в сутки это тысячи бесполезных походов в
    Авито в день, и все мимо ведра `bulk`.
    """
    чужой = await make_avito_account(555000111, status="needs_reauth")
    await сообщение(db_sessionmaker, чужой.id)
    assert await voice_repair.найти_нерасшифрованные(db, now=T0) == []


# --- 3. Порядок и порция ------------------------------------------------------


async def test_новые_вперёд_и_порцией(db_sessionmaker: Any, db: Any, account: Any) -> None:
    """⚠ ДИВЕРСИЯ: снять `.desc()` с `order_by` — краснеет на порядке.

    Живой диалог важнее месячного: новое голосовое — это человек, который
    прямо сейчас ждёт ответа (скриншот владельца), а месячному закрытому
    четыре часа очереди ничего не стоят. И спешить к старым незачем: запись
    у Авито живёт ≥29 дней. Порция — потому что расшифровка идёт по одной на
    весь процесс, а в том же процессе живёт приём вебхуков.
    """
    от_новых_к_старым = [
        (await сообщение(db_sessionmaker, account.id, возраст=timedelta(days=i)))[0]
        for i in range(voice_repair.ПОРЦИЯ + 5)
    ]
    найдено = await voice_repair.найти_нерасшифрованные(db, now=T0)
    assert len(найдено) == voice_repair.ПОРЦИЯ
    assert [r[0] for r in найдено] == от_новых_к_старым[: voice_repair.ПОРЦИЯ]


async def test_прогон_считает_вставшие_а_не_найденные(
    db_sessionmaker: Any, redis: Any, account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ДИВЕРСИЯ: `return len(кандидаты)` вместо `return поставлено` — краснеет.

    Докстринг `repair_transcripts` обещает «число ВСТАВШИХ, не найденных», а
    стерёг это обещание до 06.09 только тест на уровне `enqueue_transcribe`:
    сам прогон проверялся с ОДНИМ кандидатом, где найдено == встало, и
    подмена одного на другое оставалась зелёной. Здесь кандидатов два, и у
    одного ремонтное имя задачи уже занято — ARQ отказывает молча.
    """
    первое, created_1 = await сообщение(db_sessionmaker, account.id, возраст=timedelta(hours=1))
    await сообщение(db_sessionmaker, account.id, возраст=timedelta(hours=2))
    # Занять имя `voice:<id>:repair` заранее: вторая постановка под ним не встанет.
    assert await voice.enqueue_transcribe(redis, первое, created_1, повтор=True) is True

    monkeypatch.setattr(voice_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(voice_repair.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(
        voice_repair, "НЕ_СТАРШЕ", datetime.now(UTC) - T0 + timedelta(days=2), raising=False
    )

    assert await voice_repair.repair_transcripts() == 1, "найдено два, встало одно"


def test_прогон_ходит_раз_в_десять_минут() -> None:
    """⚠ ДИВЕРСИЯ: вернуть `minutes=30` в триггер — тест краснеет.

    458 записей при порции 20: раз в 10 минут — около четырёх часов; раз в
    полчаса — половина суток, и всё это время новые голосовые живых диалогов
    стояли бы в одной очереди с месячными. Чаще незачем: расшифровка всё
    равно идёт по одной.
    """
    from app.scheduler.main import build_scheduler

    задание = build_scheduler().get_job(voice_repair.JOB_ID)
    assert задание.trigger.interval == timedelta(minutes=10)
    assert voice_repair.ИНТЕРВАЛ == timedelta(minutes=10)


# --- 4. Задача расшифровки берёт NULL как новое --------------------------------


@pytest.fixture(autouse=True)
def свой_замок_на_тест(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий замок очереди на каждый тест — см. разбор в test_voice_transcript_0509."""
    import asyncio

    monkeypatch.setattr(transcribe, "_замок", asyncio.Lock())
    monkeypatch.setattr(transcribe, "_желающих", 0)
    monkeypatch.setattr(settings, "whisper_enabled", True)


class ФальшивоеРаспознавание:
    """Отдаёт заданный текст, ничего не считая: проверяется, что мы делаем с
    результатом, а не качество модели."""

    def __init__(self, текст: str = "Здравствуйте, сломался телевизор") -> None:
        self.текст = текст
        self.звали = 0

    def transcribe(self, путь: str, **kw: Any) -> tuple[Any, Any]:
        self.звали += 1
        return iter([SimpleNamespace(text=self.текст)]), SimpleNamespace(duration=14.0)


@pytest.fixture
def авито_отдаёт_запись(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> dict[str, Any]:
    """Ссылка от Авито и сама запись по ней — без единого похода в сеть."""
    import tempfile

    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    состояние: dict[str, Any] = {"спрашивали": 0}

    async def ссылки(self: Any, token: Any, user_id: Any, voice_ids: Any) -> dict[str, str]:
        состояние["спрашивали"] += 1
        return {voice_ids[0]: f"https://cdn.avito.example/{voice_ids[0]}.opus"}

    async def скачать(url: str, куда: str) -> int:
        with open(куда, "wb") as f:
            f.write(b"OggS" + b"\x00" * 64)
        return 68

    monkeypatch.setattr(AvitoClient, "get_voice_urls", ссылки)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    monkeypatch.setattr(transcribe, "_скачать", скачать)
    return состояние


def _ctx(db_sessionmaker: Any, redis: Any, *, попытка: int = 1) -> dict[str, Any]:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": попытка}


async def _состояние(db_sessionmaker: Any, message_id: uuid.UUID) -> tuple[Any, Any]:
    async with db_sessionmaker() as db:
        msg = (await db.execute(sa.select(Message).where(Message.id == message_id))).scalar_one()
        return msg.voice_transcript, msg.voice_transcript_status


@pytest.mark.parametrize("состояние", [None, voice.СОСТОЯНИЕ_НЕ_ВЫШЛО])
async def test_задача_берёт_null_и_failed_с_первой_попытки(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    состояние: str | None,
) -> None:
    """Досчёт ставит ту же задачу, что и конвейер приёма, с `job_try=1`. Она
    обязана отступать только от `running` (чужой счёт), `done` и `too_long`,
    а NULL и `failed` считать как новое — иначе досчёт ставил бы задачи, а
    те молча выходили бы, и хвост не двигался бы при «работающем» прогоне.

    ⚠ ДИВЕРСИЯ: добавить `voice.СОСТОЯНИЕ_НЕ_ВЫШЛО` в `voice.ЗАКОНЧЕННЫЕ` —
    краснеет вторая половина; добавить в фазе 1 `if status is None: return` —
    первая.
    """
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await сообщение(db_sessionmaker, account.id, состояние=состояние)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    assert модель.звали == 1
    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_ГОТОВО


# --- 5. Кадр message:transcript: после commit'а, и при отказе тоже -------------


def _фабрика_с_журналом(db_sessionmaker: Any, журнал: list[str]) -> Any:
    """Фабрика сессий, записывающая каждый commit в журнал — чтобы порядок
    «commit, потом кадр» был виден как последовательность, а не как догадка."""

    def фабрика() -> Any:
        сессия = db_sessionmaker()
        настоящий_commit = сессия.commit

        async def commit() -> None:
            await настоящий_commit()
            журнал.append("commit")

        сессия.commit = commit  # type: ignore[method-assign]
        return сессия

    return фабрика


@pytest.fixture
def кадры(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[dict[str, Any]]]:
    """Подмена публикации: пишет в общий журнал «publish» и запоминает поля."""
    журнал: list[str] = []
    ушло: list[dict[str, Any]] = []

    async def публикация(redis: Any, **поля: Any) -> None:
        журнал.append("publish")
        ушло.append(поля)

    monkeypatch.setattr(transcribe, "publish_transcript", публикация)
    return журнал, ушло


async def test_кадр_уходит_после_commit_с_текстом_и_состоянием(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    кадры: tuple[list[str], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ДИВЕРСИЯ 1: перенести `_известить` в `_записать_исход` на строку ПЕРЕД
    `await db.commit()` — краснеет на порядке. Кадр до commit'а заставляет
    экран перечитать ещё старую строку: «появляется только после F5» (та же
    беда, что с телефоном клиента 02.09).
    ⚠ ДИВЕРСИЯ 2: не передать `voice_transcript` в кадр — краснеет на полях.
    """
    журнал, ушло = кадры
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await сообщение(db_sessionmaker, account.id)
    async with db_sessionmaker() as db:
        conv_id = (
            await db.execute(sa.select(Message.conversation_id).where(Message.id == message_id))
        ).scalar_one()

    await transcribe.transcribe_voice(
        _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis), message_id, created_at
    )

    assert журнал == ["commit", "commit", "publish"], (
        "фаза 1 (занять `running`), фаза 3 (текст и исход) и только ПОТОМ кадр"
    )
    assert ушло == [
        {
            "conversation_id": conv_id,
            "message_id": message_id,
            "voice_transcript": модель.текст,
            "voice_transcript_status": voice.СОСТОЯНИЕ_ГОТОВО,
        }
    ]


async def test_кадр_уходит_и_при_отказе(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    кадры: tuple[list[str], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ДИВЕРСИЯ: обойти `_известить` в ветке `failed` (звать commit напрямую)
    — тест краснеет.

    Без кадра подпись «расшифровка готовится» висела бы до F5 — при том, что в
    базе уже честный `failed`. Текст в кадре — `null`: экрану нечего показать,
    но сменить подпись он обязан.
    """
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    async def пусто(self: Any, *_a: Any, **_kw: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(AvitoClient, "get_voice_urls", пусто)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    журнал, ушло = кадры
    message_id, created_at = await сообщение(db_sessionmaker, account.id)

    await transcribe.transcribe_voice(
        _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis), message_id, created_at
    )

    assert журнал == ["commit", "commit", "publish"]
    assert ушло[0]["voice_transcript"] is None
    assert ушло[0]["voice_transcript_status"] == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    assert ушло[0]["message_id"] == message_id


async def test_кадр_доходит_до_шины_событий(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без подмен: настоящий `publish_transcript` -> `publish_event` -> Pub/Sub.

    ⚠ ДИВЕРСИЯ: опечатка в имени кадра (`message:transcrip`) — краснеет.
    Хаб шлёт всё, что не `control:*`, а фронт разбирает кадры по имени: с
    опечаткой кадр честно уехал бы и был бы молча выброшен на экране.
    """
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await сообщение(db_sessionmaker, account.id)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    события = [e for e in await drain_events(pubsub) if e["type"] == "message:transcript"]
    await pubsub.aclose()
    assert len(события) == 1
    assert события[0]["data"]["message_id"] == str(message_id)
    assert события[0]["data"]["voice_transcript"] == модель.текст
    assert события[0]["data"]["voice_transcript_status"] == voice.СОСТОЯНИЕ_ГОТОВО


async def test_отказ_шины_не_роняет_задачу(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ДИВЕРСИЯ 1: убрать `try/except` из `_известить` (или сузить до
    `ValueError`) — тест падает с `ConnectionError` вместо `done` в базе.
    ⚠ ДИВЕРСИЯ 2: оставить `except`, но убрать `log.warning` — краснеет на
    журнале. Глотать отказ молча запрещено правилами проекта: без строки в
    журнале отказавшая шина выглядела бы как «кадры доходят, просто экран не
    обновился», и искали бы виноватого на фронте.

    Текст к этому моменту уже в базе и приедет со следующим открытием диалога;
    ронять из-за Redis задачу, только что отработавшую минуты счёта, значило
    бы считать её заново.
    """
    import structlog

    async def шина_лежит(*_a: Any, **_kw: Any) -> None:
        raise ConnectionError("redis: connection refused")

    monkeypatch.setattr(transcribe, "publish_transcript", шина_лежит)
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await сообщение(db_sessionmaker, account.id)

    with structlog.testing.capture_logs() as журнал:
        await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    текст, состояние = await _состояние(db_sessionmaker, message_id)
    assert состояние == voice.СОСТОЯНИЕ_ГОТОВО and текст == модель.текст
    отказы = [з for з in журнал if з["event"] == "voice.publish_failed"]
    assert len(отказы) == 1, "отказ шины обязан быть виден в журнале, а не проглочен"
    assert отказы[0]["log_level"] == "warning"
    assert отказы[0]["message_id"] == str(message_id)
    assert "connection refused" in отказы[0]["reason"]


async def test_кадр_уходит_и_при_слишком_длинной(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    кадры: tuple[list[str], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ДИВЕРСИЯ: в `_записать_исход` отступить от кадра при `too_long` —
    тест краснеет.

    Третий исход после `done` и `failed`. «Готовится» здесь особенно лживо:
    расшифровки не будет НИКОГДА, и подпись обязана сказать это сразу, а не
    после F5. Текст в кадре — `null`.
    """
    журнал, ушло = кадры
    модель = ФальшивоеРаспознавание()  # длительность 14 с
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    monkeypatch.setattr(settings, "whisper_max_audio_seconds", 10)
    message_id, created_at = await сообщение(db_sessionmaker, account.id)

    await transcribe.transcribe_voice(
        _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis), message_id, created_at
    )

    assert журнал == ["commit", "commit", "publish"]
    assert ушло[0]["voice_transcript"] is None
    assert ушло[0]["voice_transcript_status"] == voice.СОСТОЯНИЕ_СЛИШКОМ_ДЛИННАЯ
    assert ушло[0]["message_id"] == message_id


async def test_кадр_уходит_и_без_канала(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    кадры: tuple[list[str], list[dict[str, Any]]],
) -> None:
    """⚠ ДИВЕРСИЯ: убрать вызов `_известить` из ветки «нет канала» фазы 1 —
    тест краснеет.

    Четвёртый путь к исходу, и единственный, который лежит не в
    `_записать_исход`, а прямо в фазе 1: аккаунт без токена. В модели токен
    `NOT NULL`, так что в жизни это пустые байты — например после неудачного
    переподключения. Исход `failed` пишется одним commit'ом, и кадр обязан
    идти после него: журнал — `commit`, потом `publish`.
    """
    from app.models import AvitoAccount

    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(AvitoAccount)
            .where(AvitoAccount.id == account.id)
            .values(access_token_enc=b"")
        )
        await s.commit()
    журнал, ушло = кадры
    message_id, created_at = await сообщение(db_sessionmaker, account.id)

    await transcribe.transcribe_voice(
        _ctx(_фабрика_с_журналом(db_sessionmaker, журнал), redis), message_id, created_at
    )

    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    assert журнал == ["commit", "publish"], "исход в базе, и только потом кадр"
    assert ушло[0]["voice_transcript"] is None
    assert ушло[0]["voice_transcript_status"] == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    assert ушло[0]["message_id"] == message_id


# --- разнос порции по времени (06.09) ------------------------------------------


async def test_отложенная_постановка_уходит_в_arq_со_сроком(redis: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать `_defer_by=отложить` из enqueue_transcribe — краснеет.

    ARQ хранит момент старта задачи как счёт в упорядоченном множестве
    `arq:queue` (миллисекунды). Задача «через 20 секунд» обязана лечь туда со
    счётом на 20 000 больше, чем поставленная «сейчас», — иначе параметр
    принимается и тихо выбрасывается.
    """
    import time

    сейчас_мс = time.time() * 1000
    ид, created = uuid.uuid4(), T0
    assert await voice.enqueue_transcribe(
        redis, ид, created, повтор=True, отложить=timedelta(seconds=20)
    )
    счёт = await redis.zscore("arq:queue", f"voice:{ид}:repair")
    assert счёт is not None
    assert сейчас_мс + 19_000 <= счёт <= сейчас_мс + 22_000, (
        f"задача встала на {счёт - сейчас_мс:.0f} мс от «сейчас», а просили 20 000"
    )


async def test_порция_разнесена_по_времени_с_шагом(
    db_sessionmaker: Any, redis: Any, account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ДИВЕРСИЯ: `задержка = None` для всех — краснеет; `номер * 0` — краснеет.

    Без разноса все двадцать задач порции стартуют разом и встают за одним
    замком `transcribe._замок`: ждать его каждая согласна 120 с, потом
    откладывается на 60 с, и так три попытки — хвост порции не дождался бы ни
    одной. Здесь три кандидата: первый (самый новый) стартует сразу, второй —
    через ШАГ, третий — через два ШАГА.
    """
    import time

    ид1, _ = await сообщение(db_sessionmaker, account.id, возраст=timedelta(hours=1))
    ид2, _ = await сообщение(db_sessionmaker, account.id, возраст=timedelta(hours=2))
    ид3, _ = await сообщение(db_sessionmaker, account.id, возраст=timedelta(hours=3))

    monkeypatch.setattr(voice_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(voice_repair.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(
        voice_repair, "НЕ_СТАРШЕ", datetime.now(UTC) - T0 + timedelta(days=2), raising=False
    )
    сейчас_мс = time.time() * 1000
    assert await voice_repair.repair_transcripts() == 3

    старты = [await redis.zscore("arq:queue", f"voice:{ид}:repair") for ид in (ид1, ид2, ид3)]
    assert all(s is not None for s in старты)
    шаг_мс = voice_repair.ШАГ_СЕК * 1000
    assert старты[0] - сейчас_мс < 2_000, "самая новая запись стартует сразу"
    for i in (1, 2):
        разница = старты[i] - старты[0]
        assert i * шаг_мс - 1_000 <= разница <= i * шаг_мс + 2_000, (
            f"{i}-я задача отстоит от первой на {разница:.0f} мс, ждали {i * шаг_мс}"
        )
