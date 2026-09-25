"""Расшифровка голосовых своим Whisper: постановка, счёт, уборка, показ.

ЧТО ОХРАНЯЕТСЯ. Владелец выбрал свой сервер, а не облако («давай свой
Whisper»), и попросил удалять записи после расшифровки («пусть он потом их
удаляет»). Обе просьбы — не пожелания к оформлению, а требования, которые
незаметно нарушаются: запись легко утечёт в чужое облако одной строкой
конфигурации, а неудалённый временный файл виден только по «кончилось место».

⚠ КАЖДЫЙ СТОРОЖ ЗДЕСЬ ПРОВЕРЕН ДИВЕРСИЕЙ — сломано ровно то, что он стережёт,
и он покраснел. Разбор каждой диверсии — в самом тесте.

Замер боя, ради которого это делается: 597 голосовых за 60 дней, около десяти в
сутки; в голосовом клиент обычно и рассказывает суть заказа.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Client, Conversation, Message
from app.services import voice
from app.services.inbound import apply_inbound_event
from app.workers import transcribe

AVITO_USER_ID = 111222333
T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

#: Так голосовое приходит из разбора вебхука (`adapter._extract_attachments`).
ГОЛОСОВОЕ = [
    {
        "media_id": "avito_voice_2229d5a7",
        "kind": "file",
        "name": "Голосовое сообщение",
        "size": None,
        "avito_type": "voice",
    }
]

#: Что говорит клиент. Взято из замера расшифровки живой русской речи — с той
#: же особенностью, ради которой текст и подписан машинным: «стоить ремонт и
#: когда» модель слепила в «стоить ремонта когда».
РЕЧЬ = (
    "Здравствуйте, меня зовут Сергей. У меня сломался телевизор Samsung, "
    "не включается. Адрес, Санкт-Петербург, улица Рябиновая, дом 17."
)


def событие(**kw: Any) -> InboundEvent:
    защита: dict[str, Any] = {
        "external_chat_id": "chat-voice",
        "external_message_id": "am-voice-1",
        "author_id": 999001,
        "account_user_id": AVITO_USER_ID,
        "text": None,
        "created_at": T0,
        "client_name": "Сергей",
        "attachments": list(ГОЛОСОВОЕ),
    }
    защита.update(kw)
    return InboundEvent(**защита)


@pytest.fixture
async def account(make_avito_account: Any) -> Any:
    return await make_avito_account(AVITO_USER_ID)


async def задачи_расшифровки(redis: Any) -> list[str]:
    """Ключи поставленных задач расшифровки (дедуп по ``_job_id``)."""
    return sorted(k for k in await redis.keys("arq:job:voice:*"))


# --- постановка ---------------------------------------------------------------


async def test_голосовое_ставит_задачу_расшифровки(db: Any, redis: Any, account: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать вызов `enqueue_transcribe` из `services/inbound.py` —
    тест краснеет («задача обязана встать...»). Это ровно та поломка, которой
    страдали bot_step и backfill_conversation: код написан, а в бою не зовётся.
    """
    assert await apply_inbound_event(db, redis, account, событие())

    msg = (await db.execute(sa.select(Message))).scalars().one()
    assert await задачи_расшифровки(redis) == [f"arq:job:voice:{msg.id}"], (
        "задача обязана встать сразу: текст нужен в ленте раньше, чем человек "
        "откроет диалог (запись у Авито живёт ≥29 дней — замер 06.09, спешка не про неё)"
    )


async def test_обычное_сообщение_задачу_не_ставит(db: Any, redis: Any, account: Any) -> None:
    """Пятьсот входящих в сутки без голоса не должны будить модель ни разу."""
    assert await apply_inbound_event(
        db, redis, account, событие(text="Здравствуйте, почём ремонт?", attachments=[])
    )
    assert await задачи_расшифровки(redis) == []


async def test_снимок_задачу_не_ставит(db: Any, redis: Any, account: Any) -> None:
    """Фото — не голос. Признак вида берётся у Авито, а не угадывается."""
    снимок = [{"media_id": "img-1", "kind": "image", "name": "фото", "avito_type": "image"}]
    assert await apply_inbound_event(db, redis, account, событие(attachments=снимок))
    assert await задачи_расшифровки(redis) == []


async def test_история_задачу_не_ставит(db: Any, redis: Any, account: Any) -> None:
    """⚠ ДИВЕРСИЯ: убрать `not backfill` из условия — тест краснеет.

    Подключение канала поднимает всю прошлую переписку — месяцами и годами
    назад. Ставить на неё расшифровки значит засыпать очередь сотнями задач
    разом, и большинство упрётся в «Авито не знает такой записи»: по замеру
    06.09 запись живёт ≥29 дней, старше 45 проверить нечем. Что моложе 30
    дней, доберёт досчёт `scheduler/jobs/voice_repair.py` — порциями.
    """
    assert await apply_inbound_event(db, redis, account, событие(), backfill=True, publish=False)
    assert await задачи_расшифровки(redis) == []


async def test_повтор_вебхука_второй_задачи_не_родит(db: Any, redis: Any, account: Any) -> None:
    """Авито доставляет вебхук повторно; дважды считать одну запись незачем."""
    assert await apply_inbound_event(db, redis, account, событие())
    assert not await apply_inbound_event(db, redis, account, событие())
    assert len(await задачи_расшифровки(redis)) == 1


# --- сам счёт -----------------------------------------------------------------


class ФальшивоеРаспознавание:
    """Модель, которая ничего не считает: отдаёт заданный текст и длительность.

    Настоящая модель в модульном тесте не нужна и вредна: 464 МБ весов и минуты
    счёта ради проверки того, ЧТО МЫ ДЕЛАЕМ С РЕЗУЛЬТАТОМ. Качество самого
    распознавания проверено замером на живой русской речи, а не здесь.
    """

    def __init__(self, текст: str = РЕЧЬ, длительность: float = 19.4) -> None:
        self.текст = текст
        self.длительность = длительность
        self.перебрано = False
        self.звали = 0

    def transcribe(self, путь: str, **kw: Any) -> tuple[Any, Any]:
        self.звали += 1
        self.путь = путь
        self.файл_был = __import__("os").path.exists(путь)
        self.размер = __import__("os").path.getsize(путь)

        def отрезки() -> Any:
            self.перебрано = True
            yield SimpleNamespace(text=" " + self.текст)

        return отрезки(), SimpleNamespace(duration=self.длительность)


@pytest.fixture
def свой_временный_каталог(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Временные файлы — в каталог теста, чтобы было видно, убрали ли за собой."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@pytest.fixture
def авито_отдаёт_запись(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Ссылка от Авито и сама запись по ней — без единого похода в сеть."""
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    состояние: dict[str, Any] = {"спрашивали": 0, "скачивали": 0}

    async def ссылки(self: Any, token: Any, user_id: Any, voice_ids: Any) -> dict[str, str]:
        состояние["спрашивали"] += 1
        return {voice_ids[0]: f"https://cdn.avito.example/{voice_ids[0]}.opus"}

    async def скачать(url: str, куда: str) -> int:
        состояние["скачивали"] += 1
        with open(куда, "wb") as f:
            f.write(b"OggS" + b"\x00" * 4096)
        return 4100

    monkeypatch.setattr(AvitoClient, "get_voice_urls", ссылки)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    monkeypatch.setattr(transcribe, "_скачать", скачать)
    return состояние


async def _голосовое_в_базе(
    db_sessionmaker: Any, account: Any, **kw: Any
) -> tuple[uuid.UUID, datetime]:
    async with db_sessionmaker() as db:
        client_row = Client(
            channel="avito", external_id=f"9990{uuid.uuid4().int % 10**6:06d}", name="Сергей"
        )
        db.add(client_row)
        await db.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            last_message_at=T0,
        )
        db.add(conv)
        await db.flush()
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            external_message_id="am-voice-1",
            direction=kw.pop("direction", "in"),
            sender_type="client",
            body=None,
            attachments=list(ГОЛОСОВОЕ),
            delivery_status="delivered",
            created_at=T0,
            **kw,
        )
        db.add(msg)
        await db.commit()
        return msg.id, msg.created_at


def _ctx(db_sessionmaker: Any, redis: Any, *, попытка: int = 1) -> dict[str, Any]:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": попытка}


async def _состояние(db_sessionmaker: Any, message_id: uuid.UUID) -> tuple[Any, Any]:
    async with db_sessionmaker() as db:
        msg = (await db.execute(sa.select(Message).where(Message.id == message_id))).scalar_one()
        return msg.voice_transcript, msg.voice_transcript_status


@pytest.fixture(autouse=True)
def свой_замок_на_тест(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий замок очереди на каждый тест.

    ⚠ ЭТО НЕ ПРИЧУДА ТЕСТА, А СВОЙСТВО `asyncio.Lock`. При ПЕРВОМ ожидании (то
    есть когда за замком выстроилась очередь) он привязывается к текущему циклу
    событий, а pytest-asyncio даёт каждому тесту свой цикл. Без этой строки
    тест, где очередь возникла, «заражал» бы следующий такой же:
    `RuntimeError: ... is bound to a different event loop`.

    В бою цикл у воркера один на весь процесс, поэтому там вопроса нет.
    """
    monkeypatch.setattr(transcribe, "_замок", asyncio.Lock())
    monkeypatch.setattr(transcribe, "_желающих", 0)


@pytest.fixture(autouse=True)
def whisper_включён(monkeypatch: pytest.MonkeyPatch) -> None:
    """В .env.example расшифровка выключена (dev не должен качать 464 МБ),
    а здесь проверяется именно она."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "whisper_enabled", True)


async def test_текст_попадает_в_сообщение_а_файл_удаляется(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Главный путь: запись скачана, расшифрована, текст в базе, файла нет.

    ⚠ ДИВЕРСИЯ 1: заменить в `_посчитать` `finally:` на `else:` — краснеют ОБА
    теста уборки, и это поучительно: успешная ветка выходит из `try` через
    `return`, а `return` минует `else` начисто. То есть уборка исчезает совсем,
    и диск копит чужие голоса незаметно, до «кончилось место».
    ⚠ ДИВЕРСИЯ 2: не писать `voice_transcript` в фазе 3 — краснеет на тексте.
    """
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    текст, состояние = await _состояние(db_sessionmaker, message_id)
    assert текст == РЕЧЬ
    assert состояние == voice.СОСТОЯНИЕ_ГОТОВО
    assert модель.файл_был and модель.размер == 4100, "модели дали настоящий файл"
    assert list(свой_временный_каталог.iterdir()) == [], (
        "после расшифровки не осталось ни одной записи: владелец просил прямо — "
        "«пусть он потом их удаляет»"
    )


async def test_запись_удаляется_и_когда_расшифровка_упала(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ЭТО ГЛАВНЫЙ ИЗ ДВУХ СЛУЧАЕВ, А НЕ ЗАПАСНОЙ.

    Успешный путь легко убрать за собой и без `finally`. Копит мусор как раз
    отказ — он случается на битом кодеке, на обрыве, на нехватке памяти, и
    именно после него файл оставался бы лежать.

    ⚠ ДИВЕРСИЯ (проверена): перенести `os.unlink` из `finally` на строку перед
    `return текст, длительность` — краснеет ЭТОТ тест и тест длинной записи, а
    успешный остаётся зелёным. Ровно та пара, ради которой отрицательная
    проверка и пишется отдельно: сторож на успешном пути такой поломки не
    видит вовсе.
    """

    def падает(_путь: str, **_kw: Any) -> Any:
        raise RuntimeError("кодек не распознан")

    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: SimpleNamespace(transcribe=падает))
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    текст, состояние = await _состояние(db_sessionmaker, message_id)
    assert текст is None
    assert состояние == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО, (
        "отказ обязан быть отличим от «ещё не считали»: иначе повторный запуск "
        "не отличить от первого"
    )
    assert list(свой_временный_каталог.iterdir()) == [], "файл обязан уйти и при отказе"


async def test_длинная_запись_не_считается(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Полчаса записи — это не голосовое, и потолок задачи ARQ (300 с) их не ждёт.

    Проверяется не только состояние, но и то, что счёта НЕ БЫЛО: длительность
    известна до перебора отрезков, и тратить на такую запись процессор нельзя.

    ⚠ ДИВЕРСИЯ: снять проверку `duration > whisper_max_audio_seconds` — тест
    краснеет на `перебрано`, то есть на впустую потраченных минутах счёта.
    """
    модель = ФальшивоеРаспознавание(длительность=1800.0)
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    текст, состояние = await _состояние(db_sessionmaker, message_id)
    assert текст is None
    assert состояние == voice.СОСТОЯНИЕ_СЛИШКОМ_ДЛИННАЯ, (
        "отдельно от `failed`: «попробуйте ещё раз» здесь ложный совет — "
        "повтор упрётся в тот же порог"
    )
    assert not модель.перебрано, "на длинную запись не потрачено ни секунды счёта"
    assert list(свой_временный_каталог.iterdir()) == []


async def test_готовое_второй_раз_не_считается(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторная постановка (сверка истории, ручной повтор) не жжёт процессор.

    ⚠ ДИВЕРСИЯ: убрать проверку `ЗАКОНЧЕННЫЕ` — краснеет на счётчике походов
    в Авито.
    """
    modель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: modель)
    message_id, created_at = await _голосовое_в_базе(
        db_sessionmaker,
        account,
        voice_transcript="уже посчитано",
        voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
    )

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    assert авито_отдаёт_запись["спрашивали"] == 0
    assert modель.звали == 0
    текст, _ = await _состояние(db_sessionmaker, message_id)
    assert текст == "уже посчитано", "готовый текст не перетирается"


async def test_чужое_в_работе_уважаем_своё_нет(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`running` — не вечная блокировка, иначе убитая задача запирает запись.

    Первая попытка, увидев «в работе», отступает: считает кто-то другой. А
    повтор ТОЙ ЖЕ задачи (job_try > 1) обязан войти — «в работе» там оставила
    её же прошлая попытка, убитая потолком в 300 с или падением воркера.

    ⚠ ДИВЕРСИЯ: убрать `и попытка == 1` из условия — краснеет первая половина
    (чужой счёт задваивается). Убрать всю проверку `running` — краснеет она же.
    Оставить `running` вечной блокировкой (без исключения для повтора) —
    краснеет вторая половина, и запись остаётся «в работе» навсегда.
    """
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await _голосовое_в_базе(
        db_sessionmaker, account, voice_transcript_status=voice.СОСТОЯНИЕ_В_РАБОТЕ
    )

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)
    assert модель.звали == 0, "чужой счёт не задваиваем"

    await transcribe.transcribe_voice(
        _ctx(db_sessionmaker, redis, попытка=2), message_id, created_at
    )
    текст, состояние = await _состояние(db_sessionmaker, message_id)
    assert состояние == voice.СОСТОЯНИЕ_ГОТОВО
    assert текст == РЕЧЬ, "иначе запись висела бы «в работе» без единого работающего"


async def test_исходящее_не_расшифровываем(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Свои слова мы знаем текстом; расшифровка нужна для речи КЛИЕНТА."""
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account, direction="out")

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    assert модель.звали == 0
    assert (await _состояние(db_sessionmaker, message_id))[1] is None


async def test_выключенная_расшифровка_ничего_не_трогает(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHISPER_ENABLED=0 обязан выключать всё: и поход в Авито, и модель.

    Это единственный рычаг, которым расшифровку гасят на бою, не выкатывая код.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "whisper_enabled", False)
    модель = ФальшивоеРаспознавание()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    assert авито_отдаёт_запись["спрашивали"] == 0
    assert модель.звали == 0
    assert (await _состояние(db_sessionmaker, message_id))[1] is None


async def test_авито_без_записи_отказ_без_повтора(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Авито ответило, но записи не дало — повторять нечего, это не сбой связи.

    ⚠ ДИВЕРСИЯ: пометить эту причину `повторять=True` — задача уйдёт в Retry, и
    тест упадёт с `arq.worker.Retry` вместо состояния `failed`. Гонять
    безнадёжное по кругу — это ровный поток ошибок в журнале, в котором
    перестают замечать настоящие.
    """
    from app.integrations.avito.client import AvitoClient
    from app.services import crypto

    async def пусто(self: Any, *_a: Any, **_kw: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(AvitoClient, "get_voice_urls", пусто)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)

    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО
    assert list(свой_временный_каталог.iterdir()) == []


async def test_обрыв_связи_повторяется_но_не_бесконечно(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Временная беда — повод повторить; третий отказ подряд — повод перестать.

    ⚠ ДИВЕРСИЯ: снять потолок `whisper_max_tries` — последняя попытка тоже
    уйдёт в Retry, и запись никогда не получит состояния `failed`: диспетчер
    вечно видел бы пустое место без объяснения.
    """
    from arq import Retry

    from app.integrations.avito.client import AvitoClient
    from app.integrations.avito.errors import AvitoUnavailable
    from app.services import crypto

    async def недоступно(self: Any, *_a: Any, **_kw: Any) -> dict[str, str]:
        raise AvitoUnavailable("502")

    monkeypatch.setattr(AvitoClient, "get_voice_urls", недоступно)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    with pytest.raises(Retry):
        await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)
    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_В_РАБОТЕ

    await transcribe.transcribe_voice(
        _ctx(db_sessionmaker, redis, попытка=3), message_id, created_at
    )
    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_НЕ_ВЫШЛО


async def test_модель_не_загрузилась_повод_повторить_а_не_хоронить(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Первая расшифровка после чистой установки качает 464 МБ. Обрыв — не приговор.

    ⚠ ПОЧЕМУ ЭТО ОТДЕЛЬНОЕ РЕШЕНИЕ. Сюда приходят ровно две беды, и обе чаще
    временные, чем вечные: не докачалась модель и каталог тома приехал от root.
    Обе чинятся сами или одной командой на хосте, а запись при этом ни в чём не
    виновата — похоронить её здесь значило бы потерять расшифровку навсегда
    из-за пятиминутной беды.

    ⚠ ДИВЕРСИЯ: пометить этот отказ `повторять=False` — задача сразу запишет
    `failed`, и тест краснеет на ожидании `Retry`.
    """
    import sys
    import types

    from arq import Retry

    # Подменяем не нашу функцию, а САМ ПАКЕТ: так проверяется настоящая ветка
    # `_загрузить_модель`, включая то, что импорт `faster_whisper` там ленивый —
    # подмена в sys.modules сработала бы только для импорта внутри функции.
    поддельный = types.ModuleType("faster_whisper")

    def не_грузится(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("не докачалась модель")

    поддельный.WhisperModel = не_грузится  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", поддельный)
    monkeypatch.setattr(transcribe, "_модель", None)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    with pytest.raises(Retry):
        await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)
    assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_В_РАБОТЕ
    assert list(свой_временный_каталог.iterdir()) == [], (
        "скачанная запись обязана уйти и тогда, когда считать её оказалось нечем"
    )


class СчитающаяОдновременность:
    """Модель, которая запоминает, сколько расшифровок шло разом."""

    def __init__(self) -> None:
        self.сейчас = 0
        self.максимум = 0
        self.замок = threading.Lock()

    def transcribe(self, путь: str, **kw: Any) -> tuple[Any, Any]:
        with self.замок:
            self.сейчас += 1
            self.максимум = max(self.максимум, self.сейчас)
        time.sleep(0.05)  # окно, в котором вторая задача успела бы влезть
        with self.замок:
            self.сейчас -= 1
        return iter([SimpleNamespace(text="раз")]), SimpleNamespace(duration=5.0)


async def test_две_записи_считаются_по_очереди(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    свой_временный_каталог: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ОДНА РАСШИФРОВКА ЗА РАЗ — ЭТО ЗАЩИТА ПРИЁМА ВЕБХУКОВ, А НЕ АККУРАТНОСТЬ.

    Голосовые идут сериями по две-три подряд. Две расшифровки разом заняли бы
    четыре потока при квоте контейнера в два ядра — и cgroup замораживал бы
    контейнер ЦЕЛИКОМ на часть каждого периода, вместе с циклом приёма вебхуков
    Авито, который живёт в этом же процессе. То есть расшифровка чужой записи
    тормозила бы приём сообщений живых клиентов, и в журнале не было бы ни
    строчки.

    ⚠ ДИВЕРСИЯ: убрать взятие `_замок` — максимум становится 2, тест краснеет.
    """
    модель = СчитающаяОдновременность()
    monkeypatch.setattr(transcribe, "_загрузить_модель", lambda: модель)
    первое = await _голосовое_в_базе(db_sessionmaker, account)
    второе = await _голосовое_в_базе(db_sessionmaker, account)

    await asyncio.gather(
        transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), *первое),
        transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), *второе),
    )

    assert модель.максимум == 1, (
        f"разом считались {модель.максимум} записи: столько потоков квота "
        "контейнера не покрывает, и приём вебхуков встаёт вместе с ними"
    )
    for message_id, _ in (первое, второе):
        assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_ГОТОВО
    assert list(свой_временный_каталог.iterdir()) == []


async def test_очередь_занята_ждём_но_не_вечно(
    db_sessionmaker: Any,
    redis: Any,
    account: Any,
    авито_отдаёт_запись: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока считают чужую запись, наша ждёт очереди — но не навсегда.

    Ветка перестала быть «почти недостижимой» 06.09: досчёт ставит по двадцать
    записей за проход, и без потолка задача выбрала бы попытки ARQ и умерла бы
    совсем, оставив запись «в работе» НАВСЕГДА — диспетчер видел бы вечное
    «расшифровка готовится», и починить это было бы нечем.

    ⚠ ИСХОД ИСЧЕРПАНИЯ — «НЕ НАЧИНАЛИ» (NULL), А НЕ «НЕ ВЫШЛО» (06.09). К
    записи ни разу не приступали: она не дождалась замка за соседями. `failed`
    показывал бы «расшифровать не удалось» у записи, которую никто не пробовал,
    а досчёт через десять минут снова ставил бы её в очередь — мигание в живом
    диалоге. NULL честен, и досчёт берёт его сам.

    ⚠ ДИВЕРСИЯ: убрать `if попытка < settings.whisper_max_tries` из ветки
    занятой очереди — вторая половина теста краснеет: вместо NULL летит
    очередной `Retry`. Вторая: вернуть `состояние=voice.СОСТОЯНИЕ_НЕ_ВЫШЛО` —
    краснеет проверка «не начинали».
    """
    from arq import Retry

    monkeypatch.setattr(transcribe, "ЖДАТЬ_ОЧЕРЕДЬ_СЕК", 0.01)
    message_id, created_at = await _голосовое_в_базе(db_sessionmaker, account)

    await transcribe._замок.acquire()  # соседняя запись «считается» прямо сейчас
    try:
        with pytest.raises(Retry):
            await transcribe.transcribe_voice(_ctx(db_sessionmaker, redis), message_id, created_at)
        assert (await _состояние(db_sessionmaker, message_id))[1] == voice.СОСТОЯНИЕ_В_РАБОТЕ

        await transcribe.transcribe_voice(
            _ctx(db_sessionmaker, redis, попытка=3), message_id, created_at
        )
        assert (await _состояние(db_sessionmaker, message_id))[1] is None, (
            "не дождавшаяся замка запись обязана вернуться в «не начинали», "
            "а не лечь «не вышло»: её никто не пробовал"
        )
    finally:
        transcribe._замок.release()

    assert авито_отдаёт_запись["спрашивали"] == 0, (
        "ссылку спрашиваем ПОСЛЕ очереди, а не до: иначе она протухла бы, пока "
        "задача стоит в ожидании"
    )


async def test_задача_зарегистрирована_в_воркере() -> None:
    """⚠ БЕЗ ЭТОЙ СТРОКИ ЗАДАЧА ВИСИТ В pending, И ВИНОВАТОГО НЕ ВИДНО НИГДЕ.

    Ровно так уже обжигались `bot_step` и `backfill_conversation`: код есть,
    очередь наполняется, исполнителя нет.
    """
    from app.workers.main import registered_job_names

    assert voice.TRANSCRIBE_JOB in registered_job_names()


# --- показ --------------------------------------------------------------------


async def test_расшифровка_едет_в_описании_сообщения(
    client: Any, tokens: Any, db_sessionmaker: Any, account: Any
) -> None:
    """Текст обязан приехать вместе с сообщением: отдельного запроса за ним нет.

    ⚠ ДИВЕРСИЯ: убрать пару полей из `message_out` — тест краснеет. Экран без
    них показывает запись без расшифровки, а сервер при этом «всё посчитал»:
    поломка, невидимая ни в журнале, ни в базе.
    """
    message_id, _ = await _голосовое_в_базе(
        db_sessionmaker,
        account,
        voice_transcript=РЕЧЬ,
        voice_transcript_status=voice.СОСТОЯНИЕ_ГОТОВО,
    )
    async with db_sessionmaker() as db:
        conv_id = (
            await db.execute(sa.select(Message.conversation_id).where(Message.id == message_id))
        ).scalar_one()

    ответ = await client.get(
        f"/api/v1/conversations/{conv_id}/messages",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert ответ.status_code == 200, ответ.text
    (сообщение,) = [m for m in ответ.json()["items"] if m["id"] == str(message_id)]
    assert сообщение["voice_transcript"] == РЕЧЬ
    assert сообщение["voice_transcript_status"] == voice.СОСТОЯНИЕ_ГОТОВО


async def test_у_обычного_сообщения_поля_пустые(
    client: Any, tokens: Any, seed_conversation: Any
) -> None:
    """Форма ответа одна на все сообщения: `null` читается как «расшифровки нет»."""
    ответ = await client.get(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert ответ.status_code == 200, ответ.text
    for сообщение in ответ.json()["items"]:
        assert сообщение["voice_transcript"] is None
        assert сообщение["voice_transcript_status"] is None


async def test_разбор_вложения_один_на_ручку_и_на_расшифровку() -> None:
    """Идентификатор записи достаётся ровно одним способом.

    Разойдись два разбора — ручка отдавала бы запись, а расшифровка молчала бы
    (или наоборот), и виноватого не было бы видно нигде.
    """
    assert voice.voice_id_of(ГОЛОСОВОЕ) == "2229d5a7"
    assert voice.voice_id_of([]) is None
    assert voice.voice_id_of(None) is None
    assert voice.voice_id_of([{"avito_type": "image", "media_id": "img-1"}]) is None
    assert voice.voice_id_of([{"avito_type": "voice", "media_id": ""}]) is None
    assert voice.voice_id_of(["мусор", 42]) is None


def test_умолчания_остаются_такими_как_решено() -> None:
    """Числа из разбора: модель, потоки, порог. Меняются решением, а не случайно."""
    from app.core.config import Settings

    поля = Settings.model_fields
    assert поля["whisper_model"].default == "small"
    assert поля["whisper_cpu_threads"].default == 2, (
        "два потока при квоте контейнера в 2 ядра: четыре замораживали бы "
        "контейнер целиком, вместе с приёмом вебхуков Авито"
    )
    assert поля["whisper_max_audio_seconds"].default == 300, (
        "потолок ARQ-задачи 300 с: запись длиннее была бы убита на середине"
    )
    assert "/var/leadchat" in поля["whisper_model_dir"].default, (
        "веса живут в томе, а не в образе: 464 МБ в слое поехали бы через тоннель на каждой выкатке"
    )


def test_модель_не_попадает_в_память_веб_части() -> None:
    """⚠ ИМПОРТ CTranslate2 ОБЯЗАН ОСТАВАТЬСЯ ЛЕНИВЫМ.

    Модуль расшифровки импортирует воркер; сам по себе импорт не должен тянуть
    ни faster_whisper, ни onnxruntime. Верхнеуровневый `import faster_whisper`
    добавил бы их в КАЖДЫЙ процесс — включая два процесса uvicorn, где они не
    нужны никогда, и оба поднялись бы в памяти на сотни мегабайт.
    """
    import subprocess
    import sys

    код = (
        "import sys, app.workers.transcribe;"
        "print(any(m.startswith(('faster_whisper','ctranslate2','onnxruntime')) "
        "for m in sys.modules))"
    )
    вывод = subprocess.run(
        [sys.executable, "-c", код], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert вывод == "False", (
        "модуль расшифровки при импорте затащил CTranslate2 — он окажется в "
        "памяти api и планировщика, которым не нужен никогда"
    )
