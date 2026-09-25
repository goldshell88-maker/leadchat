"""Доставка картинок клиенту: что уходит, в каком порядке и что переживает обрыв.

⚠ ЧЕГО ЭТО СТОИЛО ДО СИХ ПОР. Сообщение с вложением падало целиком с текстом
«Отправка вложений в Авито пока недоступна». Оператор прикладывал фото детали
с подписью «нужна вот такая» — и не отправлялось НИЧЕГО, а текст отказа обещал,
что «скоро включат». Авито при этом картинки принимает: `uploadImages` и
`messages/image` (docs/26-AVITO-API-CATALOG.md, строки 49 и 63). Не принимает
он произвольный файл — метода в каталоге нет вовсе, и это уже навсегда.

⚠ ГЛАВНОЕ ЗДЕСЬ — ИДЕМПОТЕНТНОСТЬ, А НЕ САМ ФАКТ ОТПРАВКИ. На одну картинку
приходится ДВА запроса подряд без ключа идемпотентности, и обрыв между ними при
повторе джобы заливает фото заново и отправляет второй раз. Про эту опасность
прямо предупреждает docs/21-ROADMAP-2026-08-07.md, строка 246. Отозвать
отправленное в Авито нельзя, поэтому два последних теста файла — про обрыв.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arq import Retry
from sqlalchemy import select

from app.integrations.avito.errors import AvitoApiError
from app.models import AvitoAccount, Client, Conversation, Message
from app.services import crypto
from app.workers import deliver

pytestmark = pytest.mark.anyio

КАРТИНКА = b"\x89PNG\r\n\x1a\n" + "фото детали".encode()
ПДФ = b"%PDF-1.7 " + "смета".encode()


class ФейкАвито:
    """Три ручки Авито, участвующие в отправке, и общий журнал вызовов.

    Экземпляр кладётся В КЛАСС вместо метода: связанный метод не дескриптор,
    поэтому `self` клиента сюда не приезжает и сигнатуры совпадают с боевыми
    (тот же приём, что в tests/integration/test_delivery_pg.py).
    """

    def __init__(self) -> None:
        self.заливки: list[tuple[str, bytes]] = []
        self.картинки: list[str] = []
        self.тексты: list[str] = []
        self.порядок: list[str] = []
        #: чем падает `messages/image` (обрыв между заливкой и отправкой)
        self.сбой_картинки: Exception | None = None
        #: чем падает отправка текста (обрыв уже после того, как фото ушло)
        self.сбой_текста: Exception | None = None

    async def upload_image(
        self, token: str, user_id: int, data: bytes, filename: str, content_type: str
    ) -> str:
        self.заливки.append((filename, data))
        self.порядок.append("upload")
        return f"img-{len(self.заливки)}"

    async def send_image(self, token: str, user_id: int, chat_id: str, image_id: str) -> str:
        if self.сбой_картинки is not None:
            raise self.сбой_картинки
        self.картинки.append(image_id)
        self.порядок.append("image")
        return f"ext-img-{len(self.картинки)}"

    async def send_message(self, token: str, user_id: int, chat_id: str, text: str) -> str:
        if self.сбой_текста is not None:
            raise self.сбой_текста
        self.тексты.append(text)
        self.порядок.append("text")
        return f"ext-txt-{len(self.тексты)}"


@pytest.fixture
def авито(monkeypatch) -> ФейкАвито:
    фейк = ФейкАвито()
    monkeypatch.setattr(deliver._OutboundClient, "upload_image", фейк.upload_image)
    monkeypatch.setattr(deliver._OutboundClient, "send_image", фейк.send_image)
    monkeypatch.setattr(deliver._OutboundClient, "send_message", фейк.send_message)
    return фейк


@pytest.fixture
def хранилище(monkeypatch, tmp_path: Path) -> Path:
    """MEDIA_ROOT на время теста. Воркер читает файл С ДИСКА, а не по ссылке."""
    from app.services import media

    monkeypatch.setattr(media, "media_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
async def канал(db_sessionmaker) -> SimpleNamespace:
    """Аккаунт с НАСТОЯЩИМИ шифрованными токенами + клиент + диалог.

    Общая фикстура `seed_conversation` кладёт в токены заглушку `b"enc-access"`,
    а доставка их расшифровывает — с ней тест падал бы на расшифровке, так и не
    дойдя до Авито.
    """
    now = datetime.now(UTC)
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Фото",
            avito_user_id=111222333,
            access_token_enc=crypto.encrypt_token("access-token"),
            refresh_token_enc=crypto.encrypt_token("refresh-token"),
            token_expires_at=now + timedelta(days=1),
            status="active",
            webhook_secret="whsec-images",
        )
        клиент = Client(channel="avito", external_id="999002", name="Иван Петров")
        s.add_all([account, клиент])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-images",
            account_id=account.id,
            client_id=клиент.id,
            status="new",
            unread_count=1,
            last_message_at=now,
        )
        s.add(conv)
        await s.commit()
        return SimpleNamespace(account_id=account.id, conversation_id=conv.id)


async def _исходящее(
    db_sessionmaker,
    канал: SimpleNamespace,
    *,
    body: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    async with db_sessionmaker() as s:
        msg = Message(
            conversation_id=канал.conversation_id,
            direction="out",
            sender_type="operator",
            body=body,
            attachments=attachments or [],
            delivery_status="pending",
            created_at=datetime.now(UTC),
        )
        s.add(msg)
        await s.commit()
        return msg.id


def _вложение(хранилище: Path, *, kind: str, имя: str, тело: bytes, mime: str) -> dict[str, Any]:
    relpath = f"2026/09/ab/{uuid.uuid4()}.{имя.rsplit('.', 1)[-1]}"
    файл = хранилище / relpath
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_bytes(тело)
    return {
        "media_id": f"m_{uuid.uuid4().hex}",
        "kind": kind,
        "name": имя,
        "size": len(тело),
        "mime": mime,
        "path": relpath,
    }


async def _статус(db_sessionmaker, message_id: uuid.UUID) -> str:
    # `session.get` не годится: таблица сообщений партиционирована, и ключ у
    # неё составной (id, created_at).
    async with db_sessionmaker() as s:
        msg = (await s.execute(select(Message).where(Message.id == message_id))).scalar_one()
        return msg.delivery_status


# ---------------------------------------------------------------------------
# Картинка вообще уходит
# ---------------------------------------------------------------------------


async def test_картинка_уходит_клиенту(
    db_sessionmaker, redis, канал, авито, хранилище, monkeypatch
):
    """Две ступени Авито: залить, затем отправить залитое.

    ⚠ ДИВЕРСИЯ, КОТОРОЙ ПРОВЕРЕН ЭТОТ СТОРОЖ: в `_send_images` заменил
    `client.send_image(...)` на возврат заглушки без похода в Авито — тест
    покраснел на `авито.картинки == []` («фото залито, но клиенту не ушло»).
    Восстановлено.
    """
    вложение = _вложение(хранилище, kind="image", имя="деталь.png", тело=КАРТИНКА, mime="image/png")
    message_id = await _исходящее(db_sessionmaker, канал, attachments=[вложение])
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    await deliver.deliver_message(ctx, message_id)

    assert авито.заливки == [("деталь.png", КАРТИНКА)], "фото не залито в Авито — отправлять нечего"
    assert авито.картинки == ["img-1"], (
        "отправлен не тот идентификатор, что вернула заливка: клиент фото не увидит"
    )
    assert await _статус(db_sessionmaker, message_id) == "delivered"


async def test_картинка_уходит_раньше_подписи(db_sessionmaker, redis, канал, авито, хранилище):
    """Порядок: сначала предмет, потом подпись.

    Текст при фото — это подпись («нужна вот такая деталь»). Пришедшая раньше
    предмета, она заставляет клиента гадать: он читает «нужна вот такая»,
    смотрит в пустой чат и переспрашивает.

    ⚠ ДИВЕРСИЯ: поменял в `deliver_message` местами вызовы `_send_images` и
    `_send_parts` — тест покраснел на `авито.порядок` (`['text', 'upload',
    'image']`). Восстановлено.
    """
    вложение = _вложение(хранилище, kind="image", имя="деталь.png", тело=КАРТИНКА, mime="image/png")
    message_id = await _исходящее(
        db_sessionmaker, канал, body="нужна вот такая деталь", attachments=[вложение]
    )
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    await deliver.deliver_message(ctx, message_id)

    assert авито.порядок == ["upload", "image", "text"], (
        "подпись ушла раньше фото — клиент читает «нужна вот такая» в пустом чате"
    )
    assert авито.тексты == ["нужна вот такая деталь"]


# ---------------------------------------------------------------------------
# Файл, который Авито не принимает
# ---------------------------------------------------------------------------


async def test_pdf_по_прежнему_отказ_и_текст_отказа_честный(
    db_sessionmaker, redis, канал, авито, хранилище
):
    """Произвольный файл Авито от нас не берёт — метода нет в каталоге.

    Отказ остаётся, а вот его текст был неправдой: «Отправка вложений в Авито
    пока недоступна» читается как «подождите, скоро включат», и оператор
    прикладывал тот же pdf снова.

    ⚠ ДИВЕРСИЯ: вернул старый текст ATTACHMENTS_ERROR («Отправка вложений в
    Авито пока недоступна») — тест покраснел на проверке слова «изображения».
    Восстановлено.
    """
    from tests.unit.conftest import drain_events

    вложение = _вложение(хранилище, kind="file", имя="смета.pdf", тело=ПДФ, mime="application/pdf")
    message_id = await _исходящее(db_sessionmaker, канал, body="вот смета", attachments=[вложение])
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await deliver.deliver_message(ctx, message_id)

    assert авито.заливки == [] and авито.тексты == [], (
        "половина сообщения ушла клиенту — это не доставка"
    )
    assert await _статус(db_sessionmaker, message_id) == "failed"

    события = await drain_events(pubsub)
    await pubsub.aclose()
    статусы = [e for e in события if e.get("type") == "message:status"]
    assert статусы, "оператор не узнал об отказе — сообщение выглядит отправленным"
    данные = статусы[-1]["data"]
    assert данные["error_code"] == "attachments_unsupported", (
        "код ошибки читает фронт — менять его нельзя"
    )
    assert "изображения" in данные["error"], (
        "текст отказа не объясняет ограничение: оператор приложит pdf ещё раз"
    )


async def test_пропавший_файл_это_отказ_а_не_трейс(db_sessionmaker, redis, канал, авито, хранилище):
    """Вложение в строке есть, файла на диске нет — понятный отказ.

    ⚠ ДИВЕРСИЯ: убрал ветку `except _ФайлПотерян` из `deliver_message` — тест
    покраснел, доставка ушла в пять попыток и `Retry` вместо отказа (файл
    повтором не появится). Восстановлено.
    """
    вложение = _вложение(хранилище, kind="image", имя="деталь.png", тело=КАРТИНКА, mime="image/png")
    (хранилище / вложение["path"]).unlink()
    message_id = await _исходящее(db_sessionmaker, канал, attachments=[вложение])
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    await deliver.deliver_message(ctx, message_id)

    assert авито.заливки == []
    assert await _статус(db_sessionmaker, message_id) == "failed"


# ---------------------------------------------------------------------------
# Обрыв. Ради этого всё и написано.
# ---------------------------------------------------------------------------


async def test_обрыв_между_заливкой_и_отправкой_не_заливает_дважды(
    db_sessionmaker, redis, канал, авито, хранилище
):
    """Залито, но не отправлено — повтор берёт ТОТ ЖЕ image_id.

    Цепочка на картинку — два запроса без ключа идемпотентности
    (docs/21-ROADMAP-2026-08-07.md, стр. 246). Повтор джобы после обрыва между
    ними залил бы фото заново, получил второй идентификатор и отправил его —
    клиент увидел бы две одинаковые картинки, а отозвать их в Авито нельзя.

    ⚠ ДИВЕРСИЯ: убрал из `_send_images` сохранение `прогресс.image_ids`
    сразу после заливки (оставил только сохранение после отправки) — тест
    покраснел: заливок стало две. Восстановлено.
    """
    вложение = _вложение(хранилище, kind="image", имя="деталь.png", тело=КАРТИНКА, mime="image/png")
    message_id = await _исходящее(db_sessionmaker, канал, attachments=[вложение])
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    авито.сбой_картинки = AvitoApiError("Авито: 500", status=500)
    with pytest.raises(Retry):
        await deliver.deliver_message(ctx, message_id)
    assert авито.заливки and авито.картинки == [], "фото ушло, хотя отправка упала"

    авито.сбой_картинки = None  # Авито починился, джоба повторилась
    await deliver.deliver_message({**ctx, "job_try": 2}, message_id)

    assert len(авито.заливки) == 1, (
        "фото залито второй раз — обрыв между двумя запросами дал клиенту дубль"
    )
    assert авито.картинки == ["img-1"], "отправлен не тот идентификатор, что уже был залит"
    assert await _статус(db_sessionmaker, message_id) == "delivered"


async def test_обрыв_после_отправки_картинки_не_шлёт_её_повторно(
    db_sessionmaker, redis, канал, авито, хранилище
):
    """Фото у клиента, подпись не ушла — повтор досылает ТОЛЬКО подпись.

    ⚠ ДИВЕРСИЯ: убрал из `_send_images` строку `прогресс.images_sent = индекс`
    после удачной отправки — тест покраснел: картинка ушла клиенту дважды
    (`авито.картинки == ['img-1', 'img-2']`). Восстановлено.
    """
    вложение = _вложение(хранилище, kind="image", имя="деталь.png", тело=КАРТИНКА, mime="image/png")
    message_id = await _исходящее(
        db_sessionmaker, канал, body="нужна вот такая деталь", attachments=[вложение]
    )
    ctx = {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}

    авито.сбой_текста = AvitoApiError("Авито: 500", status=500)
    with pytest.raises(Retry):
        await deliver.deliver_message(ctx, message_id)
    assert авито.картинки == ["img-1"] and авито.тексты == []

    авито.сбой_текста = None
    await deliver.deliver_message({**ctx, "job_try": 2}, message_id)

    assert авито.картинки == ["img-1"], "картинка отправлена клиенту второй раз"
    assert len(авито.заливки) == 1, "картинка залита второй раз"
    assert авито.тексты == ["нужна вот такая деталь"], "подпись не досланa"
    assert await _статус(db_sessionmaker, message_id) == "delivered"


# ---------------------------------------------------------------------------
# Совместимость со старой записью прогресса
# ---------------------------------------------------------------------------


async def test_старая_запись_прогресса_не_ломает_разбор(redis):
    """До выкатки ключ выглядел как {"sent": N, "external_id": ...}.

    Сообщение, застрявшее между попытками в момент выкатки, обязано читаться:
    отсутствие новых полей значит «картинок не отправлено», и это правда —
    прежняя доставка их не умела.

    ⚠ ДИВЕРСИЯ (положительная половина пары): в `_load_progress` заменил
    разбор `image_ids` на безусловный `{}` — покраснел соседний assert про
    сохранённый идентификатор. Отрицательная половина (старый ключ) без
    положительной зеленела бы и на пустом разборе.
    """
    import json

    message_id = uuid.uuid4()
    await redis.set(
        deliver._parts_key(message_id),
        json.dumps({"sent": 1, "external_id": "ext-old"}),
    )

    старый = await deliver._load_progress(redis, message_id, parts_total=2, images_total=1)
    assert старый.sent == 1 and старый.external_id == "ext-old"
    assert старый.images_sent == 0 and старый.image_ids == {}, (
        "старая запись прочиталась как «картинки уже ушли» — фото пропало бы"
    )

    новый = deliver._Прогресс(sent=1, external_id="ext", images_sent=1, image_ids={"0": "img-1"})
    await deliver._save_progress(redis, message_id, новый)
    прочитан = await deliver._load_progress(redis, message_id, parts_total=2, images_total=1)
    assert прочитан.images_sent == 1 and прочитан.image_ids == {"0": "img-1"}, (
        "новые поля не переживают запись/чтение — вся защита от дублей мимо"
    )
