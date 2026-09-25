"""Центр уведомлений на настоящем PostgreSQL (07 §1.2): миграция 0006, её
ограничения и индексы, каскады и подавление повторов под настоящей блокировкой.

Юниты гоняют логику на SQLite и проверяют арифметику центра — окна важности,
счётчик, адресацию, права. Здесь проверяется ровно то, чего SQLite показать
не может, и что на этом слое либо работает, либо тихо ломается в проде:

* **миграция 0006 применилась целиком** — две таблицы, четыре индекса и
  ЧАСТИЧНЫЕ предикаты у трёх из них. Индекс с потерянным ``WHERE`` работает,
  но перестаёт быть тем индексом, ради которого его писали;
* **CHECK-ограничения — это ограничения базы, а не соглашение питона**.
  Уведомление без адреса не увидит никто, с двумя адресами — покажется дважды
  (лично и по роли). SQLAlchemy сюда не вызывается: строки идут сырым SQL;
* **каскады**: чистка удаляет уведомление, не зная про отметки прочтения;
  удаление сотрудника уносит его личный ящик и его отметки, но НЕ рассылку —
  она общая, и один уволенный не должен гасить её остальным;
* **подавление повторов под конкурентной нагрузкой**. ``with_for_update()`` на
  SQLite — молчаливый no-op, поэтому потерянное обновление счётчика юниты
  увидеть не могут в принципе. Здесь два соединения дерутся за одну строку;
* **timestamptz туда и обратно**: PostgreSQL отдаёт aware-метки, SQLite —
  наивные. Период по московскому дню (06 §0.1) считается на настоящих метках.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db import session as db_mod
from app.models import User
from app.models.notification import Notification, NotificationRead
from app.scheduler import main as scheduler_main
from app.services import notifications as svc
from app.services.audit import MSK  # период выдачи считается по Москве (06 §0.1)
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

A, H, M, O = "admin", "head", "manager", "observer"  # noqa: E741 — как в test_rbac.py


# ------------------------------------------------------------------ инфраструктура


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(text("TRUNCATE notifications, notification_reads, users CASCADE"))


@pytest.fixture
async def users(sessionmaker) -> dict[str, User]:
    """По одному человеку на роль плюс второй администратор.

    Второй админ здесь не для массовости: вся суть отдельной таблицы прочтений
    в том, что «прочитано» персонально, а увидеть это можно только на двоих.
    """
    made: dict[str, User] = {}
    async with sessionmaker() as s:
        for key, role in (("admin", A), ("admin2", A), ("head", H), ("manager", M), ("obs", O)):
            row = User(
                email=f"{key}@notify.test",
                password_hash="x",
                full_name=key,
                role=role,
                is_active=True,
            )
            s.add(row)
            made[key] = row
        await s.commit()
    return made


async def _count(sessionmaker, model) -> int:
    async with sessionmaker() as s:
        return len((await s.execute(select(model))).scalars().all())


# ------------------------------------------------------------------ миграция 0006


async def test_migration_created_both_tables_with_their_columns(pg_engine):
    async with pg_engine.connect() as conn:
        cols = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = 'notifications'"
                    )
                )
            ).all()
        }
    # Поля таблицы по 14 §4 — все на месте и в нужных типах.
    assert cols["recipient_id"] == "uuid" and cols["audience"] == "text"
    assert cols["kind"] == "text" and cols["severity"] == "text"
    assert cols["title"] == "text" and cols["body"] == "text"
    assert cols["entity_type"] == "text" and cols["entity_id"] == "text"
    assert cols["dedup_key"] == "text"
    assert cols["repeat_count"] == "integer"
    for ts in ("created_at", "last_seen_at", "read_at", "expires_at"):
        assert cols[ts] == "timestamp with time zone", ts


async def test_migration_created_the_four_indexes_with_their_partial_predicates(pg_engine):
    """Индекс без своего ``WHERE`` — уже не тот индекс.

    Частичность здесь не украшение: у колокольчика прочитанного со временем
    становится 99% таблицы, и полный индекс по получателю означал бы, что
    сервер перебирает архив ради бейджа.
    """
    async with pg_engine.connect() as conn:
        defs = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'notifications'"
                    )
                )
            ).all()
        }

    # ⚠ `ix_notifications_recipient_unread` ЗДЕСЬ БОЛЬШЕ НЕ ПРОВЕРЯЕТСЯ, И ЭТО
    # НЕ ОСЛАБЛЕНИЕ ПРОВЕРКИ, А ИСПРАВЛЕНИЕ НЕПРАВДЫ (миграция 0062).
    #
    # Индекс строился по `read_at IS NULL` и обещал «мои непрочитанные». Но
    # непрочитанность живёт в отдельной таблице `notification_reads`, и по ней
    # же считают `unread_counts` и `list_for_user`. Замер на бою: 0 сканов за
    # 20+ суток при 400 кБ, которые переписывались на каждой вставке.
    #
    # Проверка ниже сторожит обратное: индекс НЕ должен вернуться незаметно —
    # ни автогеном модели, ни «восстановлением по аналогии».
    assert "ix_notifications_recipient_unread" not in defs, (
        "мёртвый индекс вернулся: непрочитанность считается по notification_reads, "
        "а не по read_at — см. миграцию 0062"
    )

    assert "ix_notifications_audience_created" in defs
    assert "WHERE (audience IS NOT NULL)" in defs["ix_notifications_audience_created"]

    assert "ix_notifications_dedup_key" in defs
    assert "WHERE (dedup_key IS NOT NULL)" in defs["ix_notifications_dedup_key"]

    # Чистка по сроку — полный индекс намеренно: просроченными становятся все.
    assert "ix_notifications_expires_at" in defs
    assert "WHERE" not in defs["ix_notifications_expires_at"]

    async with pg_engine.connect() as conn:
        reads = {
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'notification_reads'")
                )
            ).all()
        }
    assert "ix_notification_reads_user_id" in reads
    assert "pk_notification_reads" in reads  # первичный ключ = пара (уведомление, человек)


async def _insert_raw(pg_engine, **values) -> None:
    """Сырой INSERT мимо SQLAlchemy: проверяем ограничение базы, а не питона."""
    row = {
        "id": str(uuid.uuid4()),
        "recipient_id": None,
        "audience": None,
        "kind": "backup.failed",
        "severity": "critical",
        "title": "Резервное копирование не выполнилось",
        "expires_at": datetime.now(UTC) + timedelta(days=90),
        **values,
    }
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, recipient_id, audience, kind, severity, title, expires_at) VALUES "
                "(:id, :recipient_id, :audience, :kind, :severity, :title, :expires_at)"
            ),
            row,
        )


async def test_addressing_check_rejects_both_none_and_both_set(pg_engine, users):
    """Ровно один адрес — это ограничение базы, а не договорённость кода."""
    with pytest.raises(IntegrityError, match="ck_notifications_addressing"):
        await _insert_raw(pg_engine)  # ни получателя, ни роли — не увидит никто
    with pytest.raises(IntegrityError, match="ck_notifications_addressing"):
        await _insert_raw(
            pg_engine, recipient_id=str(users["admin"].id), audience="admin"
        )  # оба — показалось бы дважды
    # А по одному — можно.
    await _insert_raw(pg_engine, audience="admin")
    await _insert_raw(pg_engine, recipient_id=str(users["manager"].id))


async def test_severity_audience_and_repeat_count_checks(pg_engine):
    with pytest.raises(IntegrityError, match="ck_notifications_severity"):
        await _insert_raw(pg_engine, audience="admin", severity="fatal")
    with pytest.raises(IntegrityError, match="ck_notifications_audience"):
        await _insert_raw(pg_engine, audience="manager")  # рассылок менеджеру не бывает
    with pytest.raises(IntegrityError, match="ck_notifications_repeat_count"):
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO notifications (id, audience, kind, severity, title, "
                    "repeat_count, expires_at) VALUES (:i,'admin','backup.failed','critical',"
                    "'т', 0, now() + interval '90 days')"
                ),
                {"i": str(uuid.uuid4())},
            )


# ------------------------------------------------------------------ каскады


async def test_cleanup_deletes_read_marks_with_the_notification(sessionmaker, users):
    """Чистка удаляет строку и не должна знать про отметки — их уносит каскад."""
    async with sessionmaker() as db:
        stale = await svc.notify(
            db, kind="cert.expiring", now=datetime.now(UTC) - timedelta(days=91), ttl_days=90
        )
        fresh = await svc.notify(db, kind="disk.space")
        await svc.mark_read(db, users["admin"], stale.notification)
        await svc.mark_read(db, users["admin"], fresh.notification)
        await db.commit()

    assert await _count(sessionmaker, NotificationRead) == 2

    async with sessionmaker() as db:
        assert await svc.cleanup_expired(db) == 1
        await db.commit()

    assert await _count(sessionmaker, Notification) == 1
    assert await _count(sessionmaker, NotificationRead) == 1  # отметка просроченного ушла следом


async def test_the_nightly_job_really_removes_expired_rows(pg_async_url, sessionmaker, users):
    """Ночная уборка обязана КОММИТИТЬ удаление, а не только его посчитать.

    ``cleanup_expired`` лишь выполняет DELETE в переданной сессии, а сессия
    планировщика (``session_scope``) на выходе делает rollback. Джоб, взявший
    её вместо ``transaction()``, каждую ночь честно печатал бы «deleted=N» и
    каждую ночь возвращал строки на место — таблица росла бы вечно, ровно как
    без уборки вообще. Отличить это можно только на настоящей базе: проверка
    идёт из ДРУГОЙ сессии, уже после того, как job закрыл свою.
    """
    async with sessionmaker() as db:
        await svc.notify(
            db, kind="cert.expiring", now=datetime.now(UTC) - timedelta(days=91), ttl_days=90
        )
        await svc.notify(db, kind="disk.space")
        await db.commit()
    assert await _count(sessionmaker, Notification) == 2

    # Планировщик — отдельный процесс: он ходит в базу через глобальный engine,
    # а не через фикстуру теста.
    await db_mod.dispose_engine()
    db_mod.init_engine(pg_async_url, component="test")
    try:
        await scheduler_main.cleanup_expired_notifications()
    finally:
        await db_mod.dispose_engine()

    assert await _count(sessionmaker, Notification) == 1


async def test_deleting_a_user_takes_his_mailbox_but_not_the_broadcast(sessionmaker, users):
    """Личное умирает с человеком, общее — нет.

    Рассылка «всем администраторам» переживает увольнение одного из них: иначе
    один ушедший сотрудник погасил бы поломку остальным.
    """
    async with sessionmaker() as db:
        personal = await svc.notify(
            db,
            kind="conversation.assigned",
            recipient_id=users["manager"].id,
            entity_id=str(uuid.uuid4()),
        )
        broadcast = await svc.notify(db, kind="backup.failed")
        await svc.mark_read(db, users["manager"], personal.notification)
        await svc.mark_read(db, users["admin"], broadcast.notification)
        await svc.mark_read(db, users["admin2"], broadcast.notification)
        await db.commit()

    async with sessionmaker() as db:
        await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(users["manager"].id)})
        await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(users["admin2"].id)})
        await db.commit()

    async with sessionmaker() as db:
        left = (await db.execute(select(Notification.id))).scalars().all()
        assert left == [broadcast.notification.id]  # личное ушло с менеджером
        marks = (await db.execute(select(NotificationRead.user_id))).scalars().all()
        assert marks == [users["admin"].id]  # отметка уволенного админа ушла, чужая цела


# --------------------------------------------- подавление повторов под блокировкой


async def test_concurrent_repeats_do_not_lose_the_counter(sessionmaker, users):
    """Потерянное обновление счётчика — то, чего SQLite показать не может.

    ``with_for_update()`` там молчаливый no-op, поэтому юниты этот класс ошибок
    не ловят в принципе. Здесь два соединения одновременно ловят один и тот же
    сбой: строка обязана остаться одной, а счётчик — досчитать до конца, а не
    показать «повторялось 2 раза» вместо десяти.
    """
    t0 = datetime.now(UTC)
    async with sessionmaker() as db:
        first = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-1", now=t0)
        await db.commit()

    async def repeat(i: int) -> None:
        async with sessionmaker() as db:
            await svc.notify(
                db,
                kind="account.needs_reauth",
                entity_id="acc-1",
                now=t0 + timedelta(seconds=i),
            )
            await db.commit()

    # Десять параллельных попыток — ровно сценарий 14 §4, только без ночи.
    await asyncio.gather(*(repeat(i) for i in range(1, 11)))

    async with sessionmaker() as db:
        rows = (await db.execute(select(Notification))).scalars().all()
    assert len(rows) == 1, "конкурентные повторы расползлись в несколько строк"
    assert rows[0].id == first.notification.id
    assert rows[0].repeat_count == 11  # первое событие + десять повторов, ни один не потерян


async def test_a_repeat_after_a_real_read_reopens_the_same_row(sessionmaker, users):
    """Повтор после подтверждения — одна строка, снова непрочитанная.

    ЗДЕСЬ БЫЛО ОБРАТНОЕ ОЖИДАНИЕ (`..._starts_a_new_row`), и оно совпадало с
    поведением кода: подтверждённая строка склейку не ловила, и каждое
    «прочитано» порождало следующую копию. На проде это и дало «три подряд»
    у сторожа приёма сообщений — см. разбор в services/notifications._reopen.

    Ценность именно ЭТОГО теста (а не юнита рядом) — в диалекте: снятие
    отметок идёт настоящим ``DELETE ... WHERE notification_id = ...`` по
    ``notification_reads``, и ``rowcount``, по которому сервис отличает
    «оживили» от «и так было непрочитано», приходит от настоящего курсора,
    а не от SQLite.
    """
    t0 = datetime.now(UTC)
    async with sessionmaker() as db:
        first = await svc.notify(db, kind="backup.failed", now=t0)
        await db.commit()
    async with sessionmaker() as db:
        row = await db.get(Notification, first.notification.id)
        assert row is not None
        await svc.mark_read(db, users["admin"], row)
        await db.commit()
    async with sessionmaker() as db:
        again = await svc.notify(db, kind="backup.failed", now=t0 + timedelta(minutes=5))
        await db.commit()
        assert again.created is False
        assert again.revived is True
        assert again.notification.id == first.notification.id
        assert again.notification.repeat_count == 2
    assert await _count(sessionmaker, Notification) == 1
    assert await _count(sessionmaker, NotificationRead) == 0
    async with sessionmaker() as db:
        assert (await svc.unread_counts(db, users["admin"]))["unread"] == 1


# ------------------------------------------------------------------ выборки на PG


async def test_broadcast_unread_is_personal_on_real_sql(sessionmaker, users):
    """``NOT EXISTS`` по отдельной таблице прочтений — на настоящем диалекте.

    Один админ прочитал, у второго колокольчик горит: ровно то, ради чего
    выбрана отдельная таблица, а не materialize при доставке (см. модель).
    """
    async with sessionmaker() as db:
        result = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-1")
        await db.commit()

    async with sessionmaker() as db:
        row = await db.get(Notification, result.notification.id)
        assert row is not None
        await svc.mark_read(db, users["admin"], row)
        await db.commit()

    async with sessionmaker() as db:
        assert (await svc.unread_counts(db, users["admin"]))["unread"] == 0
        second = await svc.unread_counts(db, users["admin2"])
        assert second == {"unread": 1, "critical": 1, "warning": 0, "info": 0}
        # Строка по-прежнему одна на всех — множится только отметка.
        assert await _count(sessionmaker, Notification) == 1


async def test_visibility_by_role_on_real_sql(sessionmaker, users):
    """Системное — админу, рабочее — руководителю и админу, наблюдателю — ничего."""
    async with sessionmaker() as db:
        await svc.notify(db, kind="backup.failed")  # audience=admin
        await svc.notify(db, kind="conversation.negative", entity_id=str(uuid.uuid4()))  # head
        await svc.notify(
            db,
            kind="conversation.assigned",
            recipient_id=users["manager"].id,
            entity_id=str(uuid.uuid4()),
        )
        await db.commit()

    async with sessionmaker() as db:
        for key, expected in (("admin", 2), ("head", 1), ("manager", 1), ("obs", 0)):
            rows, total, _ = await svc.list_for_user(db, users[key])
            assert total == expected, key
            assert len(rows) == expected, key


async def test_timestamps_come_back_aware_and_the_msk_period_filters(sessionmaker, users):
    """PostgreSQL отдаёт aware-метки: ``as_utc`` здесь обязан быть тождеством.

    Юниты живут на наивных метках SQLite, поэтому ошибку «сравнили naive с
    aware» в фильтре периода увидеть могут только эти строки.
    """
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        old = await svc.notify(db, kind="cert.expiring", now=now - timedelta(days=3))
        fresh = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-1", now=now)
        await db.commit()

    async with sessionmaker() as db:
        row = await db.get(Notification, fresh.notification.id)
        assert row is not None
        assert row.created_at.tzinfo is not None
        assert svc.as_utc(row.created_at) == row.created_at.astimezone(UTC)

        today_msk = now.astimezone(MSK).date()
        rows, total, _ = await svc.list_for_user(db, users["admin"], date_from=today_msk)
        assert [r.id for r in rows] == [fresh.notification.id], "период по МСК отсёк не то"
        assert total == 1

        every, total_all, _ = await svc.list_for_user(db, users["admin"])
        # Свежее сверху — по последнему повтору.
        assert [r.id for r in every] == [fresh.notification.id, old.notification.id]
        assert total_all == 2


async def test_expired_never_shows_even_before_cleanup_runs(sessionmaker, users):
    """Чистка ходит раз в сутки; выдача обязана быть честной между её заходами."""
    async with sessionmaker() as db:
        await svc.notify(
            db, kind="disk.space", now=datetime.now(UTC) - timedelta(days=91), ttl_days=90
        )
        await db.commit()

    async with sessionmaker() as db:
        rows, total, _ = await svc.list_for_user(db, users["admin"])
        assert rows == [] and total == 0
        assert (await svc.unread_counts(db, users["admin"]))["unread"] == 0
        assert await _count(sessionmaker, Notification) == 1  # строка ещё в базе — просто скрыта
