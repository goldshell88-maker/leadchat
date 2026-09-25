"""Миграции статусной модели на НАСТОЯЩЕМ PostgreSQL: ограничение и обратимость.

ПОЧЕМУ ЭТОГО НЕ ПРОВЕРИТЬ ЮНИТ-ТЕСТОМ. Юнит-набор строит схему из метаданных
моделей на SQLite (`Base.metadata.create_all`) и alembic не запускает вовсе.
То есть сами файлы миграций до этой задачи не исполнялись ни одним тестом,
кроме `upgrade head` при старте интеграционного набора, — а `downgrade` в
проекте не проверялся НИ РАЗУ.

Цена непроверенного `downgrade` — не теоретическая. Откат релиза на боевой
базе после того, как хоть один диалог побывал в новом статусе, оставил бы
строки со значением, которого старый код не знает: он покажет его латиницей и
не даст сменить. Поэтому downgrade сводит такие строки в `in_progress`, и
именно это здесь проверяется.
"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import REPO_ROOT, requires_docker

pytestmark = requires_docker


def _alembic_cfg(async_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


async def _seed_refs(conn) -> tuple[str, str]:
    """Аккаунт и клиент, на которые ссылается диалог.

    Заводятся тестом, а не берутся из базы: набор мигрирует пустую схему, и
    `SELECT ... LIMIT 1` по пустой таблице дал бы NULL — то есть тест падал бы
    на `NOT NULL`, а не на том, что проверяет.
    """
    account_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())
    await conn.execute(
        sa.text(
            "INSERT INTO avito_accounts "
            "(id, title, avito_user_id, access_token_enc, refresh_token_enc, "
            " token_expires_at, status, webhook_secret) "
            "VALUES (:id, 'mig-test', :uid, '\\x00', '\\x00', now() + interval '1 day', "
            "        'active', 'whsec')"
        ),
        {"id": account_id, "uid": int(uuid.uuid4().int % 10**9)},
    )
    await conn.execute(
        sa.text("INSERT INTO clients (id, channel, external_id) VALUES (:id, 'avito', :ext)"),
        {"id": client_id, "ext": uuid.uuid4().hex[:12]},
    )
    return account_id, client_id


async def test_check_constraint_refuses_an_unknown_status(pg_async_url):
    """В колонку `status` больше нельзя записать что угодно.

    До 0026 ограничения не было вовсе: в неё писали тринадцать мест кода, и
    охраняли её только три независимые python-проверки. Любая ветка, обошедшая
    все три (миграция данных, ремонтный `UPDATE`, чужой скрипт), портила
    данные молча.
    """
    engine = create_async_engine(pg_async_url)
    try:
        async with engine.begin() as conn:
            account_id, client_id = await _seed_refs(conn)
        async with engine.begin() as conn:
            with pytest.raises(Exception) as exc:
                await conn.execute(
                    sa.text(
                        "INSERT INTO conversations "
                        "(id, channel, external_chat_id, account_id, client_id, status, "
                        " bot_active, bot_vars, tags, unread_count, declined_by) "
                        "VALUES (:id, 'avito', :chat, :account, :client, 'на_паузе', "
                        "        false, '{}'::jsonb, '{}', 0, '{}')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "chat": f"ck-{uuid.uuid4().hex[:8]}",
                        "account": account_id,
                        "client": client_id,
                    },
                )
        assert "ck_conversations_status" in str(exc.value), str(exc.value)
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade_survives_data_in_new_statuses(pg_async_url):
    """`upgrade → downgrade → upgrade` проходит, и данные не остаются мусором.

    Главная проверка — та, ради которой тест и написан: диалог в статусе,
    появившемся после 0025, ПЕРЕЖИВАЕТ откат, превратившись в `in_progress`.
    Без этой строчки в `downgrade` он остался бы значением, которого
    откатившийся код не знает: латиница в отчёте владельца и невозможность
    сменить статус руками.

    БРАЛСЯ `snoozed`, СТАЛО `waiting_client`. Отложку сняли 12 августа, и
    завести диалог в снятом статусе не даёт уже сама база (CHECK из 0033) —
    вставка падала бы на подготовке, не дойдя до проверки отката. Смысл теста
    от подмены не пострадал: `waiting_client` заведён той же миграцией 0027,
    её же `downgrade` и сводит его в `in_progress`.
    """
    from alembic import command

    engine = create_async_engine(pg_async_url)
    chat = f"mig-{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            account_id, client_id = await _seed_refs(conn)
            await conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, channel, external_chat_id, account_id, client_id, status, "
                    " status_since, bot_active, bot_vars, tags, "
                    " unread_count, declined_by) "
                    "VALUES (:id, 'avito', :chat, :account, :client, 'waiting_client', now(), "
                    "        false, '{}'::jsonb, '{}', 0, '{}')"
                ),
                {"id": conv_id, "chat": chat, "account": account_id, "client": client_id},
            )
        await engine.dispose()

        cfg = _alembic_cfg(pg_async_url)
        # Alembic поднимает СВОЙ event loop (`asyncio.run` в env.py), поэтому
        # зовём его в отдельном потоке: изнутри работающего цикла он падает.
        await asyncio.to_thread(command.downgrade, cfg, "0025")

        engine = create_async_engine(pg_async_url)
        async with engine.begin() as conn:
            status = (
                await conn.execute(
                    sa.text("SELECT status FROM conversations WHERE id = :id"), {"id": conv_id}
                )
            ).scalar_one()
            assert status == "in_progress", "откат оставил статус, которого старый код не знает"
            cols = {
                r[0]
                for r in (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'conversations'"
                        )
                    )
                ).all()
            }
            assert "status_since" not in cols
            assert "snoozed_until" not in cols
        await engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        engine = create_async_engine(pg_async_url)
        async with engine.begin() as conn:
            # Повторный upgrade заполняет `status_since` у всех строк, включая
            # ту, что пережила откат: интерфейс не должен показывать прочерк
            # там, где через сутки работы будет число у 100 % диалогов.
            filled = (
                await conn.execute(
                    sa.text("SELECT status_since IS NOT NULL FROM conversations WHERE id = :id"),
                    {"id": conv_id},
                )
            ).scalar_one()
            assert filled is True
    finally:
        await engine.dispose()
        async with create_async_engine(pg_async_url).begin() as conn:
            await conn.execute(sa.text("DELETE FROM conversations WHERE id = :id"), {"id": conv_id})
