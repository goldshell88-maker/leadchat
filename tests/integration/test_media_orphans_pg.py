"""Уборка брошенных вложений (#22).

ЧТО БЫЛО. В шапке модуля вложений написано «осиротевшие файлы собирает
``bin/media-gc.sh``». Такого файла в репозитории нет — ни по этому пути, ни по
``deploy/media-gc.sh``, который называет расписание; строка в cron оставлена
закомментированной, а руководство по эксплуатации честно пишет «скрипта пока
нет». Три места ссылались на уборку, которой не существовало, и одно из них —
комментарий ровно там, где её стали бы искать.

ОТКУДА БЕРУТСЯ СИРОТЫ. Человек прикладывает файл к сообщению: файл улетает на
диск сразу, а ``media_id`` живёт сутки в Redis и прикрепляется только при
отправке. Передумал, закрыл вкладку, перезагрузил страницу — файл остался на
диске навсегда: ключ истечёт, а байты нет.

ПОЧЕМУ НА НАСТОЯЩЕМ POSTGRES. Решение «прикреплён или нет» читается из JSONB
внутри сообщения запросом с ``jsonb_array_length``. SQLite такого не умеет, и
на нём проверка либо не запустится, либо проверит другую логику — то есть
соврёт в самую опасную сторону: уборка УДАЛЯЕТ файлы.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models import AvitoAccount, Client, Conversation, Message
from app.services import media

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg_engine(pg_async_url: str):
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE messages, conversations, clients, avito_accounts CASCADE")
        )


@pytest.fixture
async def conversation_id(pg_engine: AsyncEngine):
    """Диалог, к сообщению которого можно прикрепить файл."""
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as s:
        account = AvitoAccount(
            title="LP-Тест",
            avito_user_id=990001,
            access_token_enc=b"x",
            refresh_token_enc=b"y",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="active",
            webhook_secret="whsec",
        )
        client = Client(channel="avito", external_id="gc-1", name="Клиент")
        s.add_all([account, client])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="gc-chat",
            account_id=account.id,
            client_id=client.id,
            status="in_progress",
            last_message_at=datetime.now(UTC),
        )
        s.add(conv)
        await s.commit()
        return conv.id


def _write(root, relpath: str, *, age_days: float) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    when = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    import os

    os.utime(path, (when, when))


async def test_an_abandoned_upload_is_removed_and_an_attached_one_is_not(
    pg_engine, conversation_id, tmp_path, monkeypatch
) -> None:
    """Главная проверка: удаляется брошенное и НЕ удаляется отправленное."""
    monkeypatch.setattr(settings, "media_root", str(tmp_path))

    attached = "2026/08/ab/attached.png"
    orphan = "2026/08/cd/orphan.png"
    fresh = "2026/08/ef/fresh.png"
    _write(tmp_path, attached, age_days=30)
    _write(tmp_path, orphan, age_days=30)
    # Свежий — тот, который прямо сейчас могут отправлять. Порог в двое суток
    # страхует от гонки «файл записан, сообщение ещё не создано».
    _write(tmp_path, fresh, age_days=0)

    sessionmaker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sessionmaker() as db:
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                direction="out",
                sender_type="operator",
                body="Прайс во вложении",
                attachments=[{"media_id": "m_1", "kind": "image", "path": attached}],
                delivery_status="delivered",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

    async with sessionmaker() as db:
        removed = await media.collect_orphans(db)

    assert removed == 1, "удалить обязано ровно брошенный файл"
    assert (tmp_path / attached).exists(), "файл отправленного сообщения трогать нельзя"
    assert not (tmp_path / orphan).exists()
    assert (tmp_path / fresh).exists(), "слишком свежий файл могут отправлять прямо сейчас"


async def test_a_half_written_file_is_left_alone(pg_engine, tmp_path, monkeypatch) -> None:
    """Недокачанный `.part` — не сирота, а файл в процессе записи.

    Запись идёт через временное имя с последующей заменой именно затем, чтобы
    уборка не увидела «сироту» в наполовину записанном файле.
    """
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    _write(tmp_path, "2026/08/aa/half.png.part", age_days=30)

    sessionmaker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sessionmaker() as db:
        assert await media.collect_orphans(db) == 0
    assert (tmp_path / "2026/08/aa/half.png.part").exists()


async def test_a_missing_storage_is_not_an_error(pg_engine, tmp_path, monkeypatch) -> None:
    """Не смонтировано хранилище — задача молчит, а не падает каждую ночь.

    Уборка не та работа, ради которой стоит будить человека: если каталога
    нет, об этом скажет сторож места на диске, а не ночная задача.
    """
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "нет-такого"))
    sessionmaker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sessionmaker() as db:
        assert await media.collect_orphans(db) == 0
