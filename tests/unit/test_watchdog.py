"""Сторожевые проверки планировщика — источник системных уведомлений (14 §2.1).

Главное свойство, которое здесь проверяется: **каждая проверка порождает своё
уведомление ровно один раз за окно подавления**. Проверки бегут раз в 5 минут,
и если ключ подавления «поедет» между прогонами, администратор получит за ночь
не одну строку, а полторы сотни — центр уведомлений превратится в мусорку за
первый же сбойный день (14 §4).

Второе — что тишина не путается с поломкой: ночью, до начала рабочего дня и
без подключённых аккаунтов «приём остановился» не приходит.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx
import pytest
import respx
from sqlalchemy import update

# ``center_svc`` — центр уведомлений, соседняя зона: он владеет каталогом видов и
# кнопок. Импорт ЖЁСТКИЙ намеренно. Раньше здесь стоял try/except с пропуском
# тестов на случай «центра ещё нет»; теперь центр в дереве, и мягкий импорт
# означал бы, что разъехавшийся стык (переименовали вид, изменилась сигнатура
# ``notify``) тихо превращает три сквозные проверки в skip — то есть ровно в ту
# тишину, ради устранения которой этот сторож и написан.
from app.core.config import settings
from app.models import AvitoAccount, Message
from app.scheduler.jobs import watchdog
from app.services import notifications as center_svc
from app.services import support as support_svc

# Вторник, 12:00 по Москве — глубина рабочего дня.
# 14:00 МСК, а не 12:00. Проверка тишины не срабатывает раньше, чем сам
# РАБОЧИЙ ДЕНЬ продлится дольше порога, — иначе каждое утро приходило бы
# «приём остановился» по итогам спокойной ночи. С порогом в четыре часа это
# значит «не раньше 13:00 МСК», и прежние 12:00 не достали бы до проверки
# никогда.
WORK_NOW = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)
# 02:00 по Москве — ночь: уведомлять некого (14 §5).
NIGHT_NOW = datetime(2026, 8, 4, 23, 0, tzinfo=UTC)
# 09:10 по Москве — рабочий день только начался.
EARLY_NOW = datetime(2026, 8, 4, 6, 10, tzinfo=UTC)


# --- поддельный центр уведомлений (тот же, что в test_support.py) ------------


class FakeCenter:
    """Центр уведомлений с подавлением повторов по ``dedup_key`` (14 §4)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.records: dict[str, dict[str, Any]] = {}

    async def notify(self, db: Any, redis: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        key = kwargs["dedup_key"]
        assert key is not None, "системная проверка обязана дать ключ подавления"
        existing = self.records.get(key)
        if existing is None:
            self.records[key] = {**kwargs, "repeat_count": 1}
        else:
            existing["repeat_count"] += 1


@pytest.fixture
def center(monkeypatch: pytest.MonkeyPatch) -> FakeCenter:
    fake = FakeCenter()
    monkeypatch.setattr(support_svc, "resolve_notify_impl", lambda: fake.notify)
    return fake


# --- утилиты ------------------------------------------------------------------


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_at(monkeypatch: pytest.MonkeyPatch, percent: float) -> None:
    total = 100 * 1024**3
    used = int(total * percent / 100)
    monkeypatch.setattr(
        watchdog.shutil, "disk_usage", lambda path: _Usage(total, used, total - used)
    )


def _queue_at(monkeypatch: pytest.MonkeyPatch, *, backlog: int, oldest: int) -> None:
    from app.api.routes import health

    async def _probe(redis: Any) -> dict[str, int]:
        return {"len": backlog, "pending": 0, "oldest_pending_sec": oldest, "stream_len": backlog}

    monkeypatch.setattr(health, "_queue_probe", _probe)


async def _set_inbound_at(db_sessionmaker: Any, when: datetime) -> None:
    async with db_sessionmaker() as session:
        await session.execute(
            update(Message).where(Message.direction == "in").values(created_at=when)
        )
        await session.commit()


async def _set_account_created_at(db_sessionmaker: Any, when: datetime) -> None:
    async with db_sessionmaker() as session:
        await session.execute(update(AvitoAccount).values(created_at=when))
        await session.commit()


def _write_cert(path: Any, *, expires_in_days: int) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leadchat.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=expires_in_days))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# =============================================================================
# 1. Приём сообщений остановился
# =============================================================================


async def test_a_stalled_reception_is_reported(db, redis, seed_conversation, db_sessionmaker):
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=300))

    draft = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "inbound.stalled"
    # «Важно», а не «критично»: критичной эта тревога заняла собой красные
    # плашки и вытеснила «Резервное копирование не выполнилось» за две ночи.
    assert draft.severity == "warning"
    assert draft.dedup_key == "inbound.stalled"
    assert draft.audience == "admin"  # системное — только администраторам
    assert "300" in draft.body


async def test_a_stalled_reception_never_outshouts_a_missing_backup(
    db, redis, seed_conversation, db_sessionmaker, production
):
    """Самая дорогая находка разбора, записанная тестом.

    С 6 по 11 августа «Приём сообщений остановился» шёл критичным и завалил
    колокольчик; два настоящих «Резервное копирование не выполнилось» (6 и 8
    августа) в этом потоке не увидел никто, и копий за те ночи нет. Красная
    плашка — ресурс на три строки, и делить его с событием, которое повторяется
    сотнями, нельзя.

    Проверка ломанием: верните ``severity=CRITICAL`` в ``check_inbound_stalled``
    (или ``severity="critical"`` виду ``inbound.stalled`` в каталоге центра) —
    падает этот тест.
    """
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=300))

    stalled = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)
    backup = await watchdog.check_backup_freshness(db, redis, now=WORK_NOW)

    assert stalled is not None and backup is not None
    assert backup.severity == "critical", "«копии нет» — это факт, а не подозрение"
    assert stalled.severity != "critical", (
        "тишина в приёме снова поднимает красную плашку — она уже похоронила "
        "две ночи без резервной копии"
    )
    # Каталог центра решает то же самое и должен говорить то же самое.
    assert center_svc.KINDS["inbound.stalled"].severity == "warning"
    assert center_svc.KINDS["backup.failed"].severity == "critical"


async def test_the_stall_threshold_ignores_the_stale_half_hour_from_the_environment(
    db, redis, seed_conversation, db_sessionmaker, monkeypatch
):
    """ГЛАВНЫЙ ДЕФЕКТ РАЗБОРА: порог подняли в коде, а бой читает окружение.

    9 августа порог тишины подняли с получаса до четырёх часов по измерению
    живого потока — и поменяли ровно два умолчания в коде. Настройка же берётся
    из окружения, а там до сих пор строка из ``.env.prod.example``:
    ``WATCHDOG_INBOUND_STALL_MINUTES=30``. Она била оба умолчания, порог на бою
    остался получасовым, и тревога звонила на паузах, которые люди видели как
    «сорок восемь минут тишины» (строку заводило на 30-й минуте, а показывала
    она четвёртый повтор — текст переписывается последним).

    Проверка ломанием: верните ``_int_setting("watchdog_inbound_stall_minutes",
    240)`` вместо ``_stall_threshold_minutes()`` — падает этот тест.
    """
    monkeypatch.setattr(settings, "watchdog_inbound_stall_minutes", 30)
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=48))

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None, (
        "порог из окружения снова опустил тревогу до получаса"
    )


async def test_the_stall_threshold_can_still_be_raised_from_the_environment(
    db, redis, seed_conversation, db_sessionmaker, monkeypatch
):
    """Пол — не потолок: настройка выше измеренной работает как раньше."""
    monkeypatch.setattr(settings, "watchdog_inbound_stall_minutes", 600)
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=300))

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None
    assert watchdog._stall_threshold_minutes() == 600


async def test_a_channel_that_cannot_work_is_not_counted_as_connected(
    db, redis, seed_conversation, make_avito_account, db_sessionmaker
):
    """«Подключённых аккаунтов Авито: 3», хотя работали два.

    Третьим числилась smoke-заглушка: «Обновить токен» воскрешала её в
    ``active`` (исправлено 11 августа), и проверка, смотревшая только на
    пометку, брала её в доказательство «такой тишины не бывает». Пометку можно
    проставить ошибочно, действующий токен — нет.

    Проверка ломанием: верните условие ``AvitoAccount.status == "active"``
    в ``check_inbound_stalled`` — падает этот тест.
    """
    dead = await make_avito_account(avito_user_id=444555, title="smoke-заглушка")
    async with db_sessionmaker() as session:
        await session.execute(
            update(AvitoAccount)
            .where(AvitoAccount.id == dead.id)
            .values(token_expires_at=WORK_NOW - timedelta(days=3))
        )
        await session.commit()
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=300))

    draft = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "каналов Авито: 1" in draft.body, draft.body


async def test_without_a_single_working_channel_the_silence_is_explained(
    db, redis, seed_conversation, db_sessionmaker
):
    """Все каналы онемели — про это говорит своя критичная тревога, а тишина в
    приёме перестаёт быть новостью: принимать некому."""
    async with db_sessionmaker() as session:
        await session.execute(
            update(AvitoAccount).values(token_expires_at=WORK_NOW - timedelta(days=3))
        )
        await session.commit()
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=300))

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None


async def test_a_quiet_night_is_not_a_breakdown(db, redis, seed_conversation, db_sessionmaker):
    """Ночью сообщений и не должно быть, а уведомление всё равно никто не увидит."""
    await _set_inbound_at(db_sessionmaker, NIGHT_NOW - timedelta(hours=6))

    assert await watchdog.check_inbound_stalled(db, redis, now=NIGHT_NOW) is None


async def test_the_first_half_hour_of_the_day_is_not_a_breakdown(
    db, redis, seed_conversation, db_sessionmaker
):
    """В 09:10 «нет входящих 11 часов» — это про ночь, а не про поломку."""
    await _set_inbound_at(db_sessionmaker, EARLY_NOW - timedelta(hours=11))

    assert await watchdog.check_inbound_stalled(db, redis, now=EARLY_NOW) is None


async def test_a_live_reception_is_silent(db, redis, seed_conversation, db_sessionmaker):
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=5))

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None


async def test_without_connected_accounts_there_is_nothing_to_receive(
    db, redis, make_avito_account
):
    await make_avito_account(status="disabled")

    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None


async def test_a_freshly_connected_account_is_counted_from_its_connection(
    db, redis, make_avito_account, db_sessionmaker
):
    """Ни одного входящего за всю жизнь — считаем от подключения аккаунта,
    иначе только что подключённый аккаунт мгновенно даёт ложную тревогу."""
    await make_avito_account()

    await _set_account_created_at(db_sessionmaker, WORK_NOW - timedelta(minutes=10))
    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is None

    # Пять часов, а не три: порог тишины поднят до четырёх по измерению живого
    # потока (каждая десятая пауза между обращениями длиннее трёх часов).
    await _set_account_created_at(db_sessionmaker, WORK_NOW - timedelta(hours=5))
    assert await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW) is not None


async def test_only_a_message_from_a_client_counts_as_reception(
    db, redis, seed_conversation, db_sessionmaker
):
    """«Приём работает» — это КЛИЕНТ написал, а не системная запись в диалоге.

    Условие ``sender_type='client'`` в проверке не косметика: под ровно эту пару
    заведён частичный индекс ``idx_messages_client_in`` (миграция 0004), и без
    неё ``max(created_at)`` идёт полным проходом по всем партициям ``messages``
    каждые 5 минут. Тест держит обе стороны: и смысл, и попадание в индекс.
    """
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(hours=5))
    async with db_sessionmaker() as session:
        session.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                direction="in",
                sender_type="system",  # служебная запись, не письмо клиента
                body="диалог восстановлен сверкой",
                attachments=[],
                delivery_status="delivered",
                created_at=WORK_NOW - timedelta(minutes=1),
            )
        )
        await session.commit()

    draft = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)

    assert draft is not None, "системная запись выдала себя за входящее от клиента"
    assert "300" in draft.body  # пять часов тишины, а не минута с системной записи


# =============================================================================
# 2. Очередь входящих
# =============================================================================


async def test_a_growing_queue_is_reported(db, redis, monkeypatch):
    _queue_at(monkeypatch, backlog=settings.health_queue_len_red + 240, oldest=420)

    draft = await watchdog.check_queue_backlog(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "queue.backlog"
    assert draft.severity == "warning"
    assert "7 мин" in draft.body


async def test_a_calm_queue_is_silent(db, redis, monkeypatch):
    _queue_at(monkeypatch, backlog=3, oldest=1)

    assert await watchdog.check_queue_backlog(db, redis, now=WORK_NOW) is None


async def test_an_old_pending_message_alone_is_enough(db, redis, monkeypatch):
    """Очередь короткая, но самое старое сообщение висит — это тоже отставание."""
    _queue_at(monkeypatch, backlog=1, oldest=settings.health_oldest_pending_sec_red + 60)

    assert await watchdog.check_queue_backlog(db, redis, now=WORK_NOW) is not None


# =============================================================================
# 3. Диск
# =============================================================================


@pytest.mark.parametrize("percent", [50, 84])
async def test_a_roomy_disk_is_silent(db, redis, monkeypatch, percent):
    _disk_at(monkeypatch, percent)

    assert await watchdog.check_disk_space(db, redis, now=WORK_NOW) is None


@pytest.mark.parametrize("percent", [86, 96])
async def test_a_filling_disk_is_reported_with_the_current_number(db, redis, monkeypatch, percent):
    """Важность одна (14 §2.1 «важно»), ключ подавления один: центр обновляет
    текст непрочитанной строки, поэтому админ видит текущий процент."""
    _disk_at(monkeypatch, percent)

    draft = await watchdog.check_disk_space(db, redis, now=WORK_NOW)

    assert draft is not None
    assert (draft.kind, draft.severity) == ("disk.space", "warning")
    assert draft.dedup_key == "disk.space"
    assert f"{percent}%" in draft.title and f"{percent}%" in draft.body


async def test_a_nearly_full_disk_says_what_it_costs(db, redis, monkeypatch):
    _disk_at(monkeypatch, 97)

    draft = await watchdog.check_disk_space(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "встанет" in draft.body  # объясняем цену, а не рапортуем процент


# =============================================================================
# 4. Сертификат
# =============================================================================


async def test_an_expiring_certificate_is_reported(db, redis, monkeypatch, tmp_path):
    path = tmp_path / "fullchain.pem"
    _write_cert(path, expires_in_days=5)
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(path))

    draft = await watchdog.check_certificate_expiry(db, redis)

    assert draft is not None
    assert draft.kind == "cert.expiring"
    assert draft.severity == "info"
    assert "4 дн." in draft.title or "5 дн." in draft.title


async def test_a_fresh_certificate_is_silent(db, redis, monkeypatch, tmp_path):
    path = tmp_path / "fullchain.pem"
    _write_cert(path, expires_in_days=60)
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(path))

    assert await watchdog.check_certificate_expiry(db, redis) is None


async def test_an_expired_certificate_is_critical(db, redis, monkeypatch, tmp_path):
    path = tmp_path / "fullchain.pem"
    _write_cert(path, expires_in_days=-1)
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(path))

    draft = await watchdog.check_certificate_expiry(db, redis)

    assert draft is not None and draft.severity == "critical"
    # Критичное без кнопки — только с записанной причиной (14 §3).
    assert draft.kind in watchdog.ESCALATED_CRITICAL


async def test_a_renewed_certificate_gets_a_new_dedup_key(db, redis, monkeypatch, tmp_path):
    """Продлённый сертификат — новое событие, оно обязано пробиться сквозь
    подавление повторов, а не слиться со старым уведомлением."""
    old, new = tmp_path / "old.pem", tmp_path / "new.pem"
    _write_cert(old, expires_in_days=2)
    _write_cert(new, expires_in_days=6)

    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(old))
    first = await watchdog.check_certificate_expiry(db, redis)
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(new))
    second = await watchdog.check_certificate_expiry(db, redis)

    assert first is not None and second is not None
    assert first.dedup_key != second.dedup_key


async def test_a_missing_certificate_file_is_not_an_incident(db, redis, monkeypatch, tmp_path):
    """В контейнере планировщика /etc/letsencrypt может быть не смонтирован —
    это повод молчать, а не падать (см. cross-boundary)."""
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(tmp_path / "nope.pem"))

    assert await watchdog.check_certificate_expiry(db, redis) is None


async def test_a_broken_certificate_file_is_not_an_incident(db, redis, monkeypatch, tmp_path):
    path = tmp_path / "garbage.pem"
    path.write_bytes(b"not a certificate at all")
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(path))

    assert await watchdog.check_certificate_expiry(db, redis) is None


# =============================================================================
# 5. Отметка живости планировщика
# =============================================================================


async def test_a_missing_heartbeat_is_reported(db, redis):
    draft = await watchdog.check_scheduler_heartbeat(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "scheduler.down"
    assert draft.dedup_key == "scheduler.heartbeat_broken"  # это не «планировщик мёртв»
    assert draft.severity == "warning"


async def test_a_fresh_heartbeat_is_silent(db, redis):
    await redis.set(
        watchdog.SCHEDULER_HEARTBEAT_KEY, (WORK_NOW - timedelta(seconds=20)).isoformat()
    )

    assert await watchdog.check_scheduler_heartbeat(db, redis, now=WORK_NOW) is None


async def test_a_stale_heartbeat_is_reported(db, redis):
    stale = WORK_NOW - timedelta(seconds=settings.scheduler_heartbeat_max_age_seconds + 60)
    await redis.set(watchdog.SCHEDULER_HEARTBEAT_KEY, stale.isoformat())

    draft = await watchdog.check_scheduler_heartbeat(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "с назад" in draft.body


async def test_the_heartbeat_notification_does_not_claim_the_scheduler_is_dead(db, redis):
    """Мёртвый планировщик это сообщение бы не отправил — текст обязан быть честным."""
    draft = await watchdog.check_scheduler_heartbeat(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "он сам" in draft.body  # «это сообщение отправил он сам»


# =============================================================================
# 6. Резервное копирование
# =============================================================================


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "env", "production")


async def test_a_missing_backup_mark_is_critical(db, redis, production):
    draft = await watchdog.check_backup_freshness(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "backup.failed"
    # Свой ключ: «скрипт сообщил о падении» и «скрипта не слышно» — разные новости.
    assert draft.dedup_key == "backup.missing"
    assert draft.severity == "critical"


async def test_a_fresh_backup_mark_is_silent(db, redis, production):
    await redis.set(watchdog.BACKUP_OK_KEY, (WORK_NOW - timedelta(hours=6)).isoformat())

    assert await watchdog.check_backup_freshness(db, redis, now=WORK_NOW) is None


async def test_a_stale_backup_mark_is_reported(db, redis, production):
    await redis.set(watchdog.BACKUP_OK_KEY, (WORK_NOW - timedelta(hours=40)).isoformat())

    draft = await watchdog.check_backup_freshness(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "40 ч назад" in draft.body


async def test_outside_production_the_backup_check_keeps_quiet(db, redis):
    """В dev и CI бэкапа нет — уведомление о нём было бы враньём."""
    assert settings.env != "production"
    assert await watchdog.check_backup_freshness(db, redis, now=WORK_NOW) is None


# =============================================================================
# Прогон: подавление повторов, устойчивость, регистрация
# =============================================================================


async def test_each_check_fires_once_per_window(db, redis, center, monkeypatch):
    """Ключ подавления стабилен между прогонами: 12 прогонов — одна строка
    со счётчиком повторов, а не 12 одинаковых (14 §4)."""
    _disk_at(monkeypatch, 96)
    checks = (watchdog.Check("disk_space", watchdog.check_disk_space),)

    for _ in range(12):
        await watchdog.run_watchdog(db, redis, checks=checks, now=WORK_NOW)

    assert len(center.calls) == 12
    assert len({call["dedup_key"] for call in center.calls}) == 1
    assert len(center.records) == 1
    assert next(iter(center.records.values()))["repeat_count"] == 12


async def test_a_full_run_sends_one_notification_per_problem(
    db, redis, center, monkeypatch, seed_conversation, db_sessionmaker, production
):
    """Всё сломалось разом: каждая беда приходит своей строкой, без дублей."""
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(hours=5))
    _disk_at(monkeypatch, 88)
    _queue_at(monkeypatch, backlog=settings.health_queue_len_red + 1, oldest=10)

    first = await watchdog.run_watchdog(db, redis, now=WORK_NOW)
    second = await watchdog.run_watchdog(db, redis, now=WORK_NOW)

    keys = {draft.dedup_key for draft in first}
    assert keys == {
        "inbound.stalled",
        "queue.backlog",
        "disk.space",
        "scheduler.heartbeat_broken",
        "backup.missing",
    }
    assert {draft.dedup_key for draft in second} == keys
    assert len(center.records) == len(keys)  # второй прогон новых строк не создал
    assert all(record["repeat_count"] == 2 for record in center.records.values())


async def test_a_healthy_system_is_completely_silent(
    db, redis, center, monkeypatch, seed_conversation, db_sessionmaker
):
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(minutes=2))
    _disk_at(monkeypatch, 41)
    _queue_at(monkeypatch, backlog=0, oldest=0)
    await redis.set(watchdog.SCHEDULER_HEARTBEAT_KEY, WORK_NOW.isoformat())

    assert await watchdog.run_watchdog(db, redis, now=WORK_NOW) == []
    assert not center.calls


async def test_one_broken_check_does_not_take_the_others_down(db, redis, center, monkeypatch):
    """Сторож, умирающий от первой ошибки, — это сторож, которого нет."""

    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("проверка сломалась")

    _disk_at(monkeypatch, 96)
    checks = (
        watchdog.Check("broken", _explode),
        watchdog.Check("disk_space", watchdog.check_disk_space),
    )

    found = await watchdog.run_watchdog(db, redis, checks=checks, now=WORK_NOW)

    assert [draft.kind for draft in found] == ["disk.space"]


async def test_the_run_leaves_its_own_alive_mark(db, redis, center, monkeypatch):
    """«Сторож молчит» должно быть видно снаружи — иначе молчание неотличимо
    от здоровья."""
    _disk_at(monkeypatch, 10)

    await watchdog.run_watchdog(db, redis, now=WORK_NOW)

    assert await redis.get(watchdog.WATCHDOG_ALIVE_KEY)


async def test_the_absent_notification_center_does_not_break_the_run(db, redis, monkeypatch):
    """Центра нет — прогон всё равно доходит до конца, а уведомление целиком
    уходит в лог: потерять его молча нельзя (это и есть «неделя тишины»)."""
    _disk_at(monkeypatch, 96)
    monkeypatch.setattr(support_svc, "resolve_notify_impl", lambda: None)

    found = await watchdog.run_watchdog(
        db, redis, checks=(watchdog.Check("disk", watchdog.check_disk_space),), now=WORK_NOW
    )

    assert [draft.kind for draft in found] == ["disk.space"]


def test_thresholds_are_read_from_settings_and_fall_back_to_the_catalog():
    """Пороги настраиваемые: поля ``watchdog_*`` живут в Settings (чужая зона) и
    подхватываются отсюда, а если поле там переименуют — проверка опустится до
    значения по умолчанию из 14 §2.1, а не уронит планировщик на импорте."""
    assert (
        watchdog._int_setting("scheduler_heartbeat_max_age_seconds", 999)
        == settings.scheduler_heartbeat_max_age_seconds
    )
    # Значение по умолчанию читается из настроек, а не из аргумента: порог
    # тишины поднят до четырёх часов по измерению живого потока.
    assert (
        watchdog._int_setting("watchdog_inbound_stall_minutes", 30)
        == settings.watchdog_inbound_stall_minutes
    )
    assert watchdog._int_setting("watchdog_disk_used_pct_warning", 85) == 85


def test_the_watchdog_registers_two_schedules():
    """Быстрые проверки — раз в 5 минут; сертификат — раз в сутки утром по МСК
    (ночное уведомление никто не увидит, 14 §5)."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    watchdog.register(scheduler)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {watchdog.JOB_FAST_ID, watchdog.JOB_DAILY_ID}
    assert "0:05:00" in str(jobs[watchdog.JOB_FAST_ID].trigger)
    assert "hour='6'" in str(jobs[watchdog.JOB_DAILY_ID].trigger)
    for job in jobs.values():
        assert job.max_instances == 1 and job.coalesce is True


def test_the_scheduler_process_runs_the_watchdog_once_it_is_wired():
    """Регистрации мало — её должен звать процесс планировщика.

    Строка ``watchdog.register(scheduler)`` в ``app/scheduler/main.py`` — чужая
    зона, и она уже стоит там. Проверка ЖЁСТКАЯ, без пропуска: если строку
    потеряют при следующей правке планировщика, сторож замолчит целиком и никто
    этого не заметит — уведомлений просто не будет, а отсутствие уведомлений
    выглядит ровно как здоровая система.
    """
    from app.scheduler.main import build_scheduler

    ids = {job.id for job in build_scheduler().get_jobs()}
    assert {watchdog.JOB_FAST_ID, watchdog.JOB_DAILY_ID} <= ids, (
        "watchdog.register() не подключён в app/scheduler/main.py — сторож не запускается"
    )


# =============================================================================
# Как это читает человек (14 §3)
# =============================================================================


async def _no_draft(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.fixture
async def all_drafts(
    db, redis, center, monkeypatch, seed_conversation, db_sessionmaker, production, tmp_path
) -> list[Any]:
    """Всё сломалось разом — полный набор уведомлений этой зоны."""
    # Пять часов, а не два: порог тишины поднят с получаса до четырёх часов по
    # измерению живого потока (на боевых данных каждая десятая пауза между
    # обращениями длиннее трёх часов).
    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(hours=5))
    # Проверка пропавшей подписки ходит в Авито по сети. Здесь она не предмет
    # разговора — набор проверяет ФОРМУ уведомлений; своё у неё ниже.
    monkeypatch.setattr(watchdog, "check_webhook_lost", _no_draft)
    _disk_at(monkeypatch, 96)
    _queue_at(monkeypatch, backlog=settings.health_queue_len_red + 1, oldest=600)
    cert = tmp_path / "fullchain.pem"
    _write_cert(cert, expires_in_days=-1)  # просроченный: единственное повышение важности
    monkeypatch.setattr(watchdog, "_cert_path", lambda: str(cert))

    drafts = await watchdog.run_watchdog(db, redis, now=WORK_NOW)
    drafts += await watchdog.run_watchdog(
        db, redis, checks=watchdog.DAILY_CHECKS, now=datetime.now(UTC)
    )
    assert len(drafts) == 6
    return drafts


async def test_every_notification_speaks_human(all_drafts):
    """Заголовок — обращение к человеку, тело объясняет цену поломки (14 §3)."""
    for draft in all_drafts:
        assert draft.kind not in draft.title, draft.kind
        assert draft.title[0].isupper(), draft.title
        assert "Traceback" not in draft.body and "rc=" not in draft.body, draft.kind
        assert len(draft.body) > 40, draft.kind  # объясняем, а не рапортуем
        assert draft.audience == "admin", draft.kind  # системное — только админам
        assert draft.dedup_key, draft.kind


async def test_every_critical_notification_has_a_button_or_a_written_reason(all_drafts):
    """14 §3: у критичного — действие одной кнопкой. Исключения перечислены
    поимённо: в каталоге центра (ACTIONLESS_CRITICAL) или здесь, когда важность
    повышена по обстоятельствам."""
    for draft in (d for d in all_drafts if d.severity == "critical"):
        assert (
            center_svc.action_for(draft.kind)
            or draft.kind in center_svc.ACTIONLESS_CRITICAL
            or draft.kind in watchdog.ESCALATED_CRITICAL
        ), f"{draft.kind}: критичное без кнопки и без объяснения"


async def test_every_kind_we_raise_is_in_the_center_catalog(all_drafts):
    """Вид вне каталога = уведомление без иконки и без кнопки."""
    unknown = {d.kind for d in all_drafts if d.kind not in center_svc.KINDS}
    assert not unknown, f"виды вне каталога центра: {sorted(unknown)}"


async def test_a_check_really_reaches_the_notification_center(
    db, redis, monkeypatch, db_sessionmaker
):
    """Сквозная проверка стыка с настоящим центром: расхождение в сигнатуре
    ``notify`` иначе всплывает только на проде — уведомление молча уходит в лог."""
    from sqlalchemy import select

    from app.models.notification import Notification

    _disk_at(monkeypatch, 91)

    await watchdog.run_watchdog(
        db, redis, checks=(watchdog.Check("disk", watchdog.check_disk_space),), now=WORK_NOW
    )
    # ⚠ ВТОРОЙ ПРОГОН — ЧЕРЕЗ СТУПЕНЬ, А НЕ В ТУ ЖЕ СЕКУНДУ (правка 14 августа).
    # Раньше здесь стояло два вызова с одним и тем же `now`, и счётчик честно
    # доходил до двух — потому что сторож слал на КАЖДОМ проходе, раз в пять
    # минут. Именно так на бою и вырастало «повторялось 176 раз». Теперь между
    # повторами лежит лестница, и чтобы проверить склейку, надо дожить до
    # первой ступени. Проверяется здесь по-прежнему стык с настоящим центром:
    # строка одна, счётчик вырос — значит `notify` позвали с правильной
    # сигнатурой, а не уронили в лог.
    await watchdog.run_watchdog(
        db,
        redis,
        checks=(watchdog.Check("disk", watchdog.check_disk_space),),
        now=WORK_NOW + watchdog.REPEAT_AFTER,
    )

    async with db_sessionmaker() as session:
        rows = (await session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1  # повтор схлопнулся настоящим подавлением, а не фейком
    assert rows[0].kind == "disk.space"
    assert rows[0].repeat_count == 2
    assert rows[0].audience == "admin"


async def test_the_guard_does_not_repeat_itself_every_five_minutes(
    db, redis, monkeypatch, db_sessionmaker
):
    """⚠ ЗАМЕР БОЯ 14 АВГУСТА: «повторялось 176 раз» и «141».

    176 повторов по пять минут — это четырнадцать часов непрерывного условия, и
    ни один из них не сообщал ничего нового: число росло само по себе, ровно по
    часам. Строка, меняющаяся каждые пять минут, перестаёт быть новостью через
    час — а перестав быть новостью, она учит не смотреть в колокольчик вообще.

    Ровно эту беду уже лечили у напоминаний про ждущих клиентов (12 августа,
    счётчик дорос до 142). У сторожевых видов лестницы просто не было.
    """
    from sqlalchemy import select

    from app.models.notification import Notification

    _disk_at(monkeypatch, 91)
    checks = (watchdog.Check("disk", watchdog.check_disk_space),)

    await watchdog.run_watchdog(db, redis, checks=checks, now=WORK_NOW)

    # Час непрерывной беды тактами по пять минут — двенадцать проходов.
    for tick in range(1, 13):
        await watchdog.run_watchdog(
            db, redis, checks=checks, now=WORK_NOW + timedelta(minutes=5 * tick)
        )

    async with db_sessionmaker() as session:
        row = (await session.execute(select(Notification))).scalars().one()

    # Ступени за час: первая тревога, +15, +30, +60 → четыре, а не тринадцать.
    assert row.repeat_count == 4, "за час — четыре ступени, а не такт каждые пять минут"


def test_the_ladder_step_stays_inside_the_dedup_window():
    """Шаг ОБЯЗАН быть меньше окна склейки — иначе лестница разваливается.

    Молчание длиннее окна означает, что следующая ступень не найдёт живой
    строки и заведёт новую: вместо одной строки с растущим счётчиком в
    колокольчике снова копятся отдельные, просто реже. Проверяем все важности
    сразу — сторож шлёт и `warning` (диск, очередь), и `critical` (бэкап).
    """
    from app.services import notifications as center

    for severity, window in center.DEDUP_WINDOWS.items():
        assert center.ladder_step_cap(severity) < window, severity
    # И первый повтор обязан помещаться в самое узкое окно (critical — час):
    # иначе первая же ступень заводила бы вторую строку вместо повтора.
    assert watchdog.REPEAT_AFTER < min(center.DEDUP_WINDOWS.values())


# --- неразобранные сообщения от клиентов (этап 1) -----------------------------


_RAW_SEQ = 0


async def _raw(db_sessionmaker, *, error: str | None, minutes_ago: float, n: int = 1) -> None:
    """Записи сырца вебхуков: столько-то штук с такой-то пометкой."""
    from app.models import WebhookRawLog

    # Идентификатор задаётся явно: в бою колонка `Identity(always=True)`, а
    # SQLite такого не умеет и требует значения. Это уступка тестовому стенду,
    # а не свойство кода — сама проверка считает строки и к идентификаторам
    # безразлична.
    global _RAW_SEQ
    async with db_sessionmaker() as s:
        for i in range(n):
            _RAW_SEQ += 1
            s.add(
                WebhookRawLog(
                    id=_RAW_SEQ,
                    account_id=None,
                    stream_id=f"{error}-{minutes_ago}-{i}-{uuid.uuid4().hex[:8]}",
                    payload={},
                    received_at=WORK_NOW - timedelta(minutes=minutes_ago),
                    processed=False,
                    error=error,
                )
            )
        await s.commit()


async def test_a_flood_of_unparsed_messages_is_reported(db, redis, db_sessionmaker):
    """ГЛАВНОЕ: «клиент написал, а до людей не доехало» перестаёт быть тихим.

    Не разобрался вебхук — сырец ложился в журнал, в лог уходило
    предупреждение, запись подтверждалась. И всё: никто не узнавал. На
    имитаторе, который шлёт один текст, такого не случалось никогда; на боевом
    Авито с фотографиями, голосовыми и видео — случится.
    """
    await _raw(db_sessionmaker, error="parse: bad message payload", minutes_ago=10, n=6)

    draft = await watchdog.check_inbound_unparsed(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "inbound.unparsed"
    assert draft.dedup_key == "inbound.unparsed"
    assert "6" in draft.body


async def test_a_single_odd_message_does_not_wake_anyone(db, redis, db_sessionmaker):
    """Порог, а не «хоть одно».

    Единичная невиданная форма содержимого — новость, а не беда: сырец
    сохранён, разберём. Будить из-за неё человека значит приучить его
    отмахиваться и от настоящего потока.
    """
    await _raw(db_sessionmaker, error="parse: unknown content", minutes_ago=10, n=2)

    assert await watchdog.check_inbound_unparsed(db, redis, now=WORK_NOW) is None


async def test_yesterdays_failures_do_not_count(db, redis, db_sessionmaker):
    """Окно — час. Иначе одна давняя поломка держала бы тревогу вечно."""
    await _raw(db_sessionmaker, error="parse: bad", minutes_ago=180, n=20)

    assert await watchdog.check_inbound_unparsed(db, redis, now=WORK_NOW) is None


async def test_a_disabled_account_is_not_a_breakdown(db, redis, db_sessionmaker):
    """Отключённый канал и smoke-заглушка — ожидаемый исход, а не поломка.

    Их пометка своя, и считать их вместе с непонятыми сообщениями значило бы
    получать тревогу каждый раз, когда владелец отключил канал.
    """
    await _raw(db_sessionmaker, error="account_missing_or_disabled", minutes_ago=10, n=50)

    assert await watchdog.check_inbound_unparsed(db, redis, now=WORK_NOW) is None


async def _with_real_token(db_sessionmaker, account):
    """Настоящий шифрованный токен вместо заглушки из общей фикстуры.

    Общая фикстура кладёт в `access_token_enc` простые байты `b"enc-access"` —
    для большинства проверок это неважно, но здесь токен ДЕШИФРУЕТСЯ перед
    походом в Авито, и на заглушке проверка честно молчала бы, ничего не
    проверив.
    """
    from app.models import AvitoAccount
    from app.services import crypto

    async with db_sessionmaker() as s:
        row = await s.get(AvitoAccount, account.id)
        row.access_token_enc = crypto.encrypt_token("live-token")
        await s.commit()
    return account


# --- канал отобрали: подписка пропала (этап 1) --------------------------------


@respx.mock
async def test_a_lost_subscription_is_critical(db, redis, make_avito_account, db_sessionmaker):
    """ГЛАВНЫЙ СИГНАЛ «приём остановился» — не тишина, а пропавшая подписка.

    Тишина — признак косвенный и на малом потоке почти бесполезный: на боевых
    данных заказчика каждая десятая пауза между обращениями длиннее трёх часов,
    и любой разумный порог либо звонит впустую, либо молчит полдня. Со старым
    получасовым порогом сторож за шесть часов выдал 27 критичных «приём
    остановился», пока приём работал.

    Пропавшая подписка — это ФАКТ, и он не зависит от объёма. Авито держит на
    аккаунт ровно ОДНУ подписку (проверено 9 августа на боевых аккаунтах),
    значит любой, кто подпишется после нас, нашу молча заменит.
    """
    await _with_real_token(db_sessionmaker, await make_avito_account())
    respx.post(f"{settings.avito_api_base}/messenger/v1/subscriptions").mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": "https://jivo.example/hook"}]}
        )
    )

    draft = await watchdog.check_webhook_lost(db, redis)

    assert draft is not None
    assert draft.kind == "webhook.lost"
    assert draft.severity == "critical"
    assert "перерегистрации" in draft.body  # сказано, чем лечить


@respx.mock
async def test_our_subscription_in_place_is_silence(db, redis, make_avito_account, db_sessionmaker):
    """Наша подписка стоит — тревоги нет."""
    from app.services import avito_accounts as acc_svc

    account = await _with_real_token(db_sessionmaker, await make_avito_account())
    respx.post(f"{settings.avito_api_base}/messenger/v1/subscriptions").mock(
        return_value=httpx.Response(
            200, json={"subscriptions": [{"url": acc_svc.webhook_url_for(account)}]}
        )
    )

    assert await watchdog.check_webhook_lost(db, redis) is None


@respx.mock
async def test_avito_being_unreachable_is_not_a_lost_channel(
    db, redis, make_avito_account, db_sessionmaker
):
    """«Не смогли спросить» и «подписки нет» — разные новости.

    Ложная тревога здесь стоила бы доверия ко всем остальным: о недоступном
    Авито скажут другие проверки, а эта обязана молчать, пока не знает.
    """
    await _with_real_token(db_sessionmaker, await make_avito_account())
    respx.post(f"{settings.avito_api_base}/messenger/v1/subscriptions").mock(
        side_effect=httpx.ConnectError("boom")
    )

    assert await watchdog.check_webhook_lost(db, redis) is None


# =============================================================================
# Канал не может отвечать клиентам (11 августа)
# =============================================================================
#
# Проверка заведена после аудита. До неё канал, у которого протух токен, не
# наблюдался НИКЕМ: и обновление токенов, и «приём остановился», и «подписку
# отобрали» перебирают только активные аккаунты, а канал в этот момент лежит в
# `needs_reauth`. Снаружи это выглядит как «обращения приходят, ответы не
# уходят», и узнаёт об этом только тот, кто попробует ответить.


async def _expire_token(db_sessionmaker, account_id, *, hours_ago: float) -> None:
    async with db_sessionmaker() as session:
        await session.execute(
            update(AvitoAccount)
            .where(AvitoAccount.id == account_id)
            .values(token_expires_at=WORK_NOW - timedelta(hours=hours_ago))
        )
        await session.commit()


async def test_a_channel_without_a_valid_token_is_reported(
    db, redis, make_avito_account, db_sessionmaker
):
    account = await make_avito_account(status="needs_reauth", title="LP-Сергей")
    await _expire_token(db_sessionmaker, account.id, hours_ago=3)

    draft = await watchdog.check_channel_mute(db, redis, now=WORK_NOW)

    assert draft is not None
    assert draft.kind == "account.needs_reauth"
    assert "LP-Сергей" in draft.body
    # Ключ повторяет формулу каталога — иначе строка сторожа и строка сервиса
    # были бы двумя разными новостями об одном канале.
    assert draft.dedup_key == f"account.needs_reauth:account:{account.id}"


async def test_a_channel_with_a_fresh_token_is_silent(db, redis, make_avito_account):
    await make_avito_account(status="active")

    assert await watchdog.check_channel_mute(db, redis, now=WORK_NOW) is None


async def test_a_short_token_dip_is_not_a_breakdown(db, redis, make_avito_account, db_sessionmaker):
    """Запас в полчаса: плановое обновление идёт каждые 30 минут."""
    account = await make_avito_account(status="active")
    await _expire_token(db_sessionmaker, account.id, hours_ago=0.2)

    assert await watchdog.check_channel_mute(db, redis, now=WORK_NOW) is None


async def test_a_manually_disabled_channel_is_not_a_breakdown(
    db, redis, make_avito_account, db_sessionmaker
):
    """Выключенный вручную канал не чинят: его выключили нарочно."""
    account = await make_avito_account(status="disabled")
    await _expire_token(db_sessionmaker, account.id, hours_ago=48)

    assert await watchdog.check_channel_mute(db, redis, now=WORK_NOW) is None


async def test_the_alarm_names_every_mute_channel(db, redis, make_avito_account, db_sessionmaker):
    first = await make_avito_account(avito_user_id=100000001, title="LP-Сергей")
    second = await make_avito_account(avito_user_id=100000002, title="LP-Олег")
    for account in (first, second):
        await _expire_token(db_sessionmaker, account.id, hours_ago=5)

    draft = await watchdog.check_channel_mute(db, redis, now=WORK_NOW)

    assert draft is not None
    assert "LP-Сергей" in draft.body and "LP-Олег" in draft.body
    assert draft.dedup_key == "account.needs_reauth"


# =============================================================================
# Контракт окружения: порог не должен разъезжаться с кодом второй раз
# =============================================================================


@pytest.mark.parametrize("name", [".env.example", ".env.prod.example"])
def test_the_environment_contract_does_not_undercut_the_measured_threshold(name: str) -> None:
    """Ровно та строка, из-за которой «поднятый» порог полтора дня был старым.

    Умолчание в коде правится в один заход, а на сервере читается окружение — и
    ``WATCHDOG_INBOUND_STALL_MINUTES=30`` из этого файла молча било и
    ``Settings``, и запасное значение проверки. Стоило это 112 ложных критичных
    строк за шесть дней и двух незамеченных «Резервное копирование не
    выполнилось».

    Проверка ломанием: верните в ``.env.prod.example`` значение 30 — падает
    этот тест.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    line = next(
        (
            row
            for row in (root / name).read_text(encoding="utf-8").splitlines()
            if row.strip().startswith("WATCHDOG_INBOUND_STALL_MINUTES=")
        ),
        None,
    )
    assert line is not None, f"{name}: переменная порога пропала из контракта окружения"
    value = int(line.split("=", 1)[1].split("#", 1)[0].strip())
    assert value >= watchdog.MIN_INBOUND_STALL_MINUTES, (
        f"{name} задаёт порог тишины {value} мин — ниже измеренного пола "
        f"{watchdog.MIN_INBOUND_STALL_MINUTES}. Ровно так тревога и звонила "
        "на получасовых паузах, пока в коде стояло четыре часа."
    )


async def test_two_stall_episodes_in_one_day_are_one_row_with_a_counter(
    db, redis, seed_conversation, db_sessionmaker
):
    """Повторы обязаны склеиваться в ОДНУ строку со счётчиком, а не копиться.

    На бою с 6 по 11 августа «Приём сообщений остановился» дал 112 строк. Дело
    не в самой склейке — она работает, — а в ОКНЕ склейки: у критичного оно
    час. Тишина прерывается обращением клиента, следующая тревога приходит
    через несколько часов, час давно вышел — и заводится новая строка. У
    «важно» окно шесть часов, то есть весь рабочий хвост дня, и эпизоды
    складываются в одну строку с честным счётчиком.

    Тревога здесь настоящая, из самой проверки; повторы разносит по времени
    ``notify`` центра — ``run_watchdog(now=...)`` до его меток не доходит
    (``NotificationDraft`` часов не несёт, и в бою они не нужны).

    Проверка ломанием: верните в ``check_inbound_stalled`` ``severity=CRITICAL``
    — окно станет часовым, и за день выйдет три строки вместо одной.
    """
    from sqlalchemy import select

    from app.models.notification import Notification

    await _set_inbound_at(db_sessionmaker, WORK_NOW - timedelta(hours=12))
    draft = await watchdog.check_inbound_stalled(db, redis, now=WORK_NOW)
    assert draft is not None

    # Три эпизода тишины за рабочий хвост дня: 13:00, 17:00 и 20:00 МСК.
    day_start = WORK_NOW - timedelta(hours=1)
    for hours in (0, 4, 7):
        await center_svc.notify_now(
            db, redis, **draft.as_kwargs(), now=day_start + timedelta(hours=hours)
        )

    async with db_sessionmaker() as session:
        rows = (await session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1, (
        f"эпизоды тишины за один день дали строк: {len(rows)} — колокольчик снова копит копии"
    )
    assert rows[0].kind == "inbound.stalled"
    assert rows[0].severity == "warning"
    assert rows[0].repeat_count == 3, "счётчик обязан считать все эпизоды"


# --- Авито не отвечает и выключенный канал (аудит 19.08, L-002 и L-004) -------


async def test_avito_silence_is_reported_with_a_reason(db, redis):
    """Площадка молчит — тревога называет и число отказов, и причину.

    БОЕВОЙ ПОВОД. В журнале за сутки лежало восемь строк `avito.unreachable` с
    ПУСТЫМ полем причины: у половины сетевых исключений httpx пустой `str(exc)`.
    Авария названа, причина — нет, и разбирать её было нечем. Хуже другое: эти
    строки не считал ни один сторож, то есть человек узнавал о них, только если
    сам открывал журнал контейнера.
    """
    await redis.set(watchdog.AVITO_DOWN_KEY, 7)
    await redis.set(f"{watchdog.AVITO_DOWN_KEY}:why", "ReadTimeout")

    draft = await watchdog.check_avito_unreachable(db, redis)

    assert draft is not None
    assert draft.kind == "avito.unreachable"
    assert "7" in draft.body, "число отказов обязано быть в тревоге"
    assert "ReadTimeout" in draft.body, "причина обязана быть названа"
    assert draft.audience == "admin"


async def test_single_avito_hiccup_is_not_an_alarm(db, redis):
    """Одиночный сбой сети — норма для чужого API, тревоги по нему нет."""
    await redis.set(watchdog.AVITO_DOWN_KEY, 2)

    assert await watchdog.check_avito_unreachable(db, redis) is None


async def test_avito_alarm_is_silent_while_the_channel_still_works(db, redis) -> None:
    """СТО ОТКАЗОВ ПРИ ТРЁХСТАХ УДАЧНЫХ — ЭТО РЯБЬ, А НЕ АВАРИЯ.

    ⚠ БОЕВОЙ СЛУЧАЙ 28.08. Порог считал только отказы: сто за час — тревога.
    А обращений в тот же час было 385, из них 285 удачных. Канал работал, просто
    с рябью; человеку при этом приходило «перестают работать сверка, история,
    отправка ответов» — тревога описывала лежащую площадку там, где она стояла
    на ногах.

    Цена ложной тревоги здесь измерена и записана у соседнего сторожа: «112
    ложных тревог закрыли собой два реальных провала резервной копии, и заметили
    их только неделю спустя».
    """
    from app.integrations.avito.client import AVITO_TRIES_KEY

    await redis.set(watchdog.AVITO_DOWN_KEY, 100)
    await redis.set(AVITO_TRIES_KEY, 385)
    assert await watchdog.check_avito_unreachable(db, redis) is None


async def test_avito_alarm_fires_when_the_platform_is_really_down(db, redis) -> None:
    """А вот сто отказов из ста десяти — площадка лежит, и молчать нельзя."""
    from app.integrations.avito.client import AVITO_TRIES_KEY

    await redis.set(watchdog.AVITO_DOWN_KEY, 100)
    await redis.set(AVITO_TRIES_KEY, 110)
    await redis.set(f"{watchdog.AVITO_DOWN_KEY}:why", "ConnectTimeout")

    draft = await watchdog.check_avito_unreachable(db, redis)
    assert draft is not None
    assert "100 раз из 110 обращений" in draft.body, (
        "в теле нет доли — человек не отличит аварию от ряби, а именно этого "
        "различия ему и не хватало"
    )


async def test_avito_alarm_falls_back_to_the_count_when_tries_are_unknown(db, redis) -> None:
    """Знаменателя нет — решаем по числу, как решали раньше.

    Счётчик попыток моложе счётчика отказов ровно один час после выкатки. Час
    неточности лучше молчания на настоящей аварии.
    """
    await redis.set(watchdog.AVITO_DOWN_KEY, 7)
    await redis.set(f"{watchdog.AVITO_DOWN_KEY}:why", "ReadTimeout")

    draft = await watchdog.check_avito_unreachable(db, redis)
    assert draft is not None
    assert "из" not in draft.body.split("Последняя причина")[0].replace("Авито", ""), (
        "доля названа при неизвестном знаменателе — это выдумка"
    )


async def test_messages_into_a_disabled_channel_are_reported(db, redis, db_sessionmaker):
    """Клиенты пишут в выключенный канал — их принимают и выбрасывают молча.

    До этой проверки о таком не знал НИКТО: соседний сторож «сообщения не
    разбираются» эти строки исключает условием, а остальные сторожа берут
    только активные каналы. Канал случайно выключили — клиенты пишут в пустоту.
    """
    from app.models import WebhookRawLog

    async with db_sessionmaker() as session:
        session.add(
            WebhookRawLog(
                id=901,  # в PG колонка IDENTITY, в SQLite её задают руками
                account_id=None,
                stream_id="901-1",
                payload={"тело": "неважно"},
                received_at=datetime.now(UTC) - timedelta(minutes=10),
                processed=True,
                error="account_missing_or_disabled",
            )
        )
        await session.commit()

    draft = await watchdog.check_dropped_disabled(db, redis)

    assert draft is not None
    assert draft.kind == "inbound.dropped_disabled"
    assert draft.severity == "critical", "клиент пишет в пустоту — это критично"
    assert "1" in draft.body


async def test_no_alarm_when_nobody_writes_into_a_disabled_channel(db, redis):
    assert await watchdog.check_dropped_disabled(db, redis) is None


def test_every_check_is_actually_registered_in_a_set() -> None:
    """Проверка, которой нет в наборе, не выполняется НИКОГДА.

    ⚠ ПЯТЬ ЗВЕНЬЕВ, А НЕ ОДНО (docs/50-AUDIT-PROTOCOL, шаг «Обещано против
    работающего»). Чтобы тревога дошла до человека, нужно: функция написана →
    зарегистрирована в наборе → набор запускается → уведомление создаётся →
    вид есть в каталоге. Проверялось из этого ровно одно звено — что функция
    написана. Сегодня я сам чуть не оставил две новые проверки вне набора:
    код есть, тесты зелёные, а на бою тишина.

    Здесь держится второе звено: КАЖДАЯ функция `check_*` обязана стоять либо
    в быстром наборе, либо в ежедневном. Заводится новая — тест назовёт её
    поимённо, а не промолчит.
    """
    объявлены = {
        имя.removeprefix("check_")
        for имя in dir(watchdog)
        if имя.startswith("check_") and callable(getattr(watchdog, имя))
    }
    зарегистрированы = {c.name for c in watchdog.FAST_CHECKS + watchdog.DAILY_CHECKS}
    забытые = sorted(объявлены - зарегистрированы)
    assert not забытые, (
        "эти проверки написаны, но не стоят ни в одном наборе — значит не "
        f"выполняются никогда: {', '.join(забытые)}. Допишите их в FAST_CHECKS "
        "или DAILY_CHECKS."
    )
