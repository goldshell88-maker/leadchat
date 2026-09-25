"""Миграция 0069 на НАСТОЯЩЕМ PostgreSQL: партиции, поиск и обратимость.

⚠ ЧЕГО НЕ ВИДИТ ЮНИТ-НАБОР. Он строит схему из моделей на SQLite, где нет ни
помесячных партиций, ни колонки `search`. А ровно эти двое и есть цена ошибки
здесь: правка идёт по таблице в 377 493 строки, разложенной на 28 партиций, и
обнуление тела ВЫВОДИТ строку из полнотекстового поиска — генерируемая колонка
`search` считается из `body`.

Проверяется:

* перенос и его идемпотентность на настоящей секционированной таблице;
* граница (б): строка уходит из поиска после переноса и возвращается после
  отката — это желаемое, и оно должно быть видно, а не подразумеваться;
* граница (а): запись раньше марта 2026 (другая партиция) не тронута;
* границы (д) и «серый чип»: строка с настоящим вложением и служебная запись
  Авито остаются как были;
* (в) настоящие `alembic downgrade 0068` -> `upgrade 0069` возвращают подпись в
  тело байт в байт и уносят её обратно.

ДИВЕРСИИ (каждая проведена, результат в отчёте):

* в `перенести()` из `values()` убрано `body=sa.null()` (подпись остаётся в теле,
  вложение появляется) -> красный на `test_perenos_uvodit_stroku_iz_poiska`:
  строка по-прежнему находится поиском;
* в `отбор()` граница по дате ослаблена до `>= 2026-01-01` -> красный на
  `test_zapis_ranshe_granicy_ne_tronuta`;
* из `отбор()` убрано `attachments == []`, затем — оба условия про автора ->
  красный на `test_chuzhie_stroki_ostayutsya_kak_byli` (обе диверсии, ревью);
* `downgrade()` заменён пустышкой -> красный на
  `test_nastoyashchiy_downgrade_i_upgrade` (ревью);
* ⚠ ДИВЕРСИЯ ПОРЯДКОМ, А НЕ КОДОМ (ревью): соседу `test_cli_repair_pg` дописан
  в конец тест, оставляющий в общей базе строку со звонком, — все ЧЕТЫРЕ
  проверки этого файла стали красными при целой миграции. Отсюда `_под_правкой`
  вместо `rowcount`; после правки та же диверсия оставляет набор зелёным.

`__pycache__` миграции снесён перед прогоном: диверсия словом той же длины
оставляет старый `.pyc` и врёт в обе стороны.
"""

import asyncio
import importlib.util
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.scheduler.partitions import ensure_message_partitions
from tests.integration.conftest import REPO_ROOT, requires_docker

pytestmark = requires_docker

МИГРАЦИЯ = (
    REPO_ROOT / "app" / "db" / "migrations" / "versions" / "0069_placeholder_to_attachment.py"
)


def _миграция():  # noqa: ANN202 — модуль без объявленного интерфейса
    spec = importlib.util.spec_from_file_location("migration_0069_pg", МИГРАЦИЯ)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m0069 = _миграция()

#: Внутри окна правки; август 2026 — самая крупная партиция боя (2186 из 3276).
В_ОКНЕ = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
#: Раньше границы и в ДРУГОЙ партиции — та самая, которую отсекает условие (а).
ДО_ГРАНИЦЫ = datetime(2026, 2, 28, 9, 0, tzinfo=UTC)

НАСТОЯЩЕЕ_ФОТО: dict[str, Any] = {
    "media_id": "avito_image_9911",
    "kind": "image",
    "name": "Фотография",
    "size": None,
    "avito_type": "image",
}


def _alembic_cfg(async_url: str):  # noqa: ANN202 — тип из alembic, локальный импорт
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


#: Метка засеянных этим набором диалогов — по ней же они и убираются.
МЕТКА = "m0069-"


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    """Двигатель к общей базе набора + уборка ЗА СОБОЙ после каждой проверки.

    ⚠ УБОРКА ЗДЕСЬ ОБЯЗАТЕЛЬНА, И НАЙДЕНО ЭТО ПАДЕНИЕМ. `перенести()` работает
    по всей таблице, а база у интеграционного набора одна на прогон: строки,
    оставленные предыдущей проверкой, попадали в счёт следующей, и та видела
    «7 перенесено» вместо своих пяти. Проверка обязана считать только то, что
    засеяла сама, иначе её зелень зависит от порядка запуска.
    """
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    # Свежая база несёт партиции только на текущий и следующий месяц (0002);
    # засев в август и февраль без этого упрётся в «no partition found».
    await ensure_message_partitions(engine)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            свои = (
                await conn.execute(
                    sa.text(
                        "SELECT id, client_id, account_id FROM conversations "
                        "WHERE external_chat_id LIKE :метка"
                    ),
                    {"метка": f"{МЕТКА}%"},
                )
            ).all()
            for conv_id, client_id, account_id in свои:
                await conn.execute(
                    sa.text("DELETE FROM messages WHERE conversation_id = :id"), {"id": conv_id}
                )
                await conn.execute(
                    sa.text("DELETE FROM conversations WHERE id = :id"), {"id": conv_id}
                )
                await conn.execute(sa.text("DELETE FROM clients WHERE id = :id"), {"id": client_id})
                await conn.execute(
                    sa.text("DELETE FROM avito_accounts WHERE id = :id"), {"id": account_id}
                )
        await engine.dispose()


async def _диалог(conn) -> str:  # noqa: ANN001 — AsyncConnection
    """Аккаунт, клиент и диалог, на которые сошлётся сообщение."""
    account_id, client_id, conv_id = (str(uuid.uuid4()) for _ in range(3))
    await conn.execute(
        sa.text(
            "INSERT INTO avito_accounts "
            "(id, title, avito_user_id, access_token_enc, refresh_token_enc, "
            " token_expires_at, status, webhook_secret) "
            "VALUES (:id, 'mig-0069', :uid, '\\x00', '\\x00', now() + interval '1 day', "
            "        'active', 'whsec')"
        ),
        {"id": account_id, "uid": int(uuid.uuid4().int % 10**9)},
    )
    await conn.execute(
        sa.text("INSERT INTO clients (id, channel, external_id) VALUES (:id, 'avito', :ext)"),
        {"id": client_id, "ext": uuid.uuid4().hex[:12]},
    )
    await conn.execute(
        sa.text(
            "INSERT INTO conversations "
            "(id, channel, external_chat_id, account_id, client_id, status, status_since, "
            " bot_active, bot_vars, tags, unread_count, declined_by) "
            "VALUES (:id, 'avito', :chat, :account, :client, 'new', now(), "
            "        false, '{}'::jsonb, '{}', 0, '{}')"
        ),
        {
            "id": conv_id,
            "chat": f"{МЕТКА}{uuid.uuid4().hex[:8]}",
            "account": account_id,
            "client": client_id,
        },
    )
    return conv_id


async def _сообщение(
    conn,  # noqa: ANN001 — AsyncConnection
    conv_id: str,
    *,
    body: str | None,
    direction: str = "in",
    sender_type: str = "client",
    attachments: list[dict[str, Any]] | None = None,
    created_at: datetime = В_ОКНЕ,
) -> str:
    ключ = str(uuid.uuid4())
    await conn.execute(
        sa.text(
            "INSERT INTO messages "
            "(id, conversation_id, external_message_id, direction, sender_type, body, "
            " attachments, delivery_status, created_at) "
            "VALUES (:id, :conv, :ext, :direction, :sender, :body, "
            "        CAST(:attachments AS jsonb), 'delivered', :created_at)"
        ),
        {
            "id": ключ,
            "conv": conv_id,
            "ext": uuid.uuid4().hex[:12],
            "direction": direction,
            "sender": sender_type,
            "body": body,
            "attachments": json.dumps(attachments if attachments is not None else []),
            "created_at": created_at,
        },
    )
    return ключ


async def _строка(engine: AsyncEngine, ключ: str) -> tuple[str | None, list[dict[str, Any]], bool]:
    """Тело, вложения и находимость поиском — три ответа об одной записи."""
    async with engine.connect() as conn:
        return tuple(
            (
                await conn.execute(
                    sa.text(
                        "SELECT body, attachments, "
                        "       search @@ plainto_tsquery('russian', 'видео') "
                        "FROM messages WHERE id = :id"
                    ),
                    {"id": ключ},
                )
            ).one()
        )


async def _под_правкой(engine: AsyncEngine, *ключи: str) -> int:
    """Сколько ИЗ НАЗВАННЫХ строк отбирает миграция.

    ⚠ СЧИТАТЬ ВОЗВРАТ `перенести()` ЗДЕСЬ НЕЛЬЗЯ, И ЭТО НЕ ОСТОРОЖНОСТЬ.
    `rowcount` считает всю таблицу, а база у интеграционного набора одна на
    прогон (`pg_async_url`, scope="session"). Соседний модуль
    `test_cli_repair_pg` заканчивает работу, оставив в ней строку
    `in`/`client`/`attachments='[]'` с телом «Клиент звонил через приложение
    Авито» — ровно ту, которую отбирает миграция; сегодня набор зелёный лишь
    потому, что ПОСЛЕДНИЙ его тест оставляет другое тело.

    Проверено диверсией на ревью 07.09: один добавленный в конец соседа тест со
    звонком -> все ЧЕТЫРЕ проверки этого файла красные, хотя миграция цела.
    Красный не о том, что сломалось, — самый быстрый способ сделать сторож
    ненужным. Поэтому спрашиваем про свои строки поимённо.
    """
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(m0069.messages)
                .where(
                    m0069.messages.c.id.in_([uuid.UUID(k) for k in ключи]),
                    m0069.отбор(),
                )
            )
        ).scalar_one()


async def test_perenos_uvodit_stroku_iz_poiska(pg_engine: AsyncEngine) -> None:
    """Перенос, его идемпотентность и граница (б) — на настоящей PG.

    ⚠ ПРО ПОИСК ГОВОРИМ ВСЛУХ. `search` генерируется из `body`, поэтому после
    переноса запись выпадает из полнотекстового поиска. Это желаемое: искали в
    ней только нашу же фразу, слов клиента там не было никогда. Но подразумевать
    такое нельзя — либо это видно проверкой, либо это сюрприз на бою.
    """
    async with pg_engine.begin() as conn:
        conv = await _диалог(conn)
        свой = await _сообщение(conn, conv, body="Видео")

    тело, вложения, находится = await _строка(pg_engine, свой)
    assert (тело, вложения) == ("Видео", [])
    assert находится is True, "до переноса запись обязана находиться поиском"

    assert await _под_правкой(pg_engine, свой) == 1
    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.перенести)
    assert await _строка(pg_engine, свой) == (None, [m0069.вложение("Видео")], False)

    # (г) ПОВТОР ПРОВЕРЯЕТСЯ СОСТОЯНИЕМ, А НЕ СЧЁТЧИКОМ: второй прогон гоняется
    # по-настоящему, и строка обязана остаться ровно с одним вложением.
    assert await _под_правкой(pg_engine, свой) == 0
    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.перенести)
    assert await _строка(pg_engine, свой) == (None, [m0069.вложение("Видео")], False), (
        "повтор завёл второе вложение"
    )

    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.вернуть)
    assert await _строка(pg_engine, свой) == ("Видео", [], True), "откат не вернул запись в поиск"


async def test_zapis_ranshe_granicy_ne_tronuta(pg_engine: AsyncEngine) -> None:
    """Граница (а) на настоящих партициях: февраль 2026 лежит в другой таблице.

    Условие по `created_at` отсекает двадцать партиций из двадцати восьми —
    именно оно превращает проход по 377 493 строкам в правку по восьми
    партициям (EXPLAIN боя 07.09, стоимость 19.70..18870.16, оценка 5697 строк;
    цифра пересняна на ревью тем же чтением и совпала с докстрингом миграции —
    в этом файле она была от ЧЕРНОВИКА оператора и расходилась с ним).
    """
    async with pg_engine.begin() as conn:
        conv = await _диалог(conn)
        старый = await _сообщение(conn, conv, body="Видео", created_at=ДО_ГРАНИЦЫ)
        свежий = await _сообщение(conn, conv, body="Видео")

    assert await _под_правкой(pg_engine, старый, свежий) == 1, "под правку идёт только свежий"
    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.перенести)

    тело, вложения, _ = await _строка(pg_engine, старый)
    assert (тело, вложения) == ("Видео", []), "миграция зашла за границу по дате"
    assert (await _строка(pg_engine, свежий))[0] is None


async def test_chuzhie_stroki_ostayutsya_kak_byli(pg_engine: AsyncEngine) -> None:
    """Границы (д) и «серый чип»: настоящее вложение и запись Авито не трогаем.

    На бою записей Авито о звонке 24 290 против 2834 наших. Их подпись живой
    разбор оставляет телом намеренно: серый чип вложений не показывает вовсе, а
    бот считает по слову в её теле звонки клиента.
    """
    async with pg_engine.begin() as conn:
        conv = await _диалог(conn)
        с_вложением = await _сообщение(conn, conv, body="Фотография", attachments=[НАСТОЯЩЕЕ_ФОТО])
        чип = await _сообщение(
            conn,
            conv,
            body="Клиент звонил через приложение Авито",
            direction="system",
            sender_type="avito",
        )

    assert await _под_правкой(pg_engine, с_вложением, чип) == 0
    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.перенести)

    assert (await _строка(pg_engine, с_вложением))[:2] == ("Фотография", [НАСТОЯЩЕЕ_ФОТО])
    assert (await _строка(pg_engine, чип))[:2] == ("Клиент звонил через приложение Авито", [])


async def test_nastoyashchiy_downgrade_i_upgrade(pg_async_url: str, pg_engine: AsyncEngine) -> None:
    """(в) `alembic downgrade 0068` -> `upgrade 0069` на засеянных данных.

    Проверяются обе стороны файла миграции целиком, а не только её внутренности:
    цена непроверенного `downgrade` — откат релиза, после которого лента и бот
    ищут подпись в теле, а она осталась во вложении.
    """
    from alembic import command

    засеяно: dict[str, str] = {}
    async with pg_engine.begin() as conn:
        conv = await _диалог(conn)
        for подпись in m0069.ПОДПИСИ:
            засеяно[подпись] = await _сообщение(conn, conv, body=подпись)
    assert await _под_правкой(pg_engine, *засеяно.values()) == len(засеяно)
    async with pg_engine.begin() as conn:
        await conn.run_sync(m0069.перенести)

    cfg = _alembic_cfg(pg_async_url)
    # Alembic поднимает СВОЙ event loop (`asyncio.run` в env.py), поэтому зовём
    # его в отдельном потоке: изнутри работающего цикла он падает.
    await asyncio.to_thread(command.downgrade, cfg, "0068")
    for подпись, ключ in засеяно.items():
        assert (await _строка(pg_engine, ключ))[:2] == (подпись, []), (
            f"откат не вернул подпись «{подпись}» в тело"
        )

    await asyncio.to_thread(command.upgrade, cfg, "head")
    for подпись, ключ in засеяно.items():
        assert (await _строка(pg_engine, ключ))[:2] == (None, [m0069.вложение(подпись)])
