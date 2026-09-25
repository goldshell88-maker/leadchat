"""Центр уведомлений (14 §4): подавление повторов, адресация, права, чистка.

Что здесь заперто и почему именно это:

* **подавление повторов** — без него центр превращается в мусорку за первый
  сбойный день (14 §4). Проверяются все три окна важности, счётчик повторов и
  то, что схлопывается ТОЛЬКО непрочитанное;
* **адресация** — системное видит админ, рабочее руководитель, личное только
  адресат, наблюдатель не видит ничего (DESIGN §5.1). Ошибка здесь стоит
  утечки: «клиент недоволен» с именем и суммой уезжает стажёру;
* **чистка по сроку** — просроченное не показывается, даже если задача
  планировщика ещё не отработала;
* **реестр событий как контракт** — каталог 14 §2 продублирован тестом, чтобы
  тип события нельзя было завести мимо каталога, а критичное событие — без
  кнопки действия и без явной причины, почему кнопки нет.

Роутер монтируется фикстурой этого модуля: `app/main.py` — чужая зона, строка
монтажа уедет туда отдельно (см. cross-boundary), а тесты обязаны работать уже
сейчас.
"""

import importlib
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApiError
from app.core.rbac import ROLE_PERMISSIONS
from app.core.security import create_access_token
from app.models import User
from app.models.notification import AUDIENCES, SEVERITIES, Notification, NotificationRead
from app.services import notifications as svc
from tests.unit.conftest import drain_events

A, H, M, O = "admin", "head", "manager", "observer"  # noqa: E741 — как в test_rbac.py


# --- монтаж роутера ----------------------------------------------------------


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """Базовое приложение + роутер уведомлений, если его ещё не смонтировали.

    Сегодня `app/main.py` роутер включает, и фикстура — no-op. Проверка
    «уже смонтирован» остаётся: без неё пути удвоились бы, а тест прав
    (`test_observer_sees_nothing_at_all`) проверял бы копию, смонтированную
    здесь, вместо настоящей.
    """
    if "/api/v1/notifications" not in app.openapi().get("paths", {}):
        from app.api.routes import notifications as routes

        api_v1 = APIRouter(prefix="/api/v1")
        api_v1.include_router(routes.router, tags=["notifications"])
        app.include_router(api_v1)
        app.openapi_schema = None  # схема была посчитана выше — пересобрать
    return app


ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/v1/notifications"),
    ("GET", "/api/v1/notifications/unread-count"),
    ("POST", "/api/v1/notifications/read-all"),
    ("POST", f"/api/v1/notifications/{uuid.uuid4()}/read"),
    ("POST", f"/api/v1/notifications/{uuid.uuid4()}/action"),
]


# --- вспомогательное ---------------------------------------------------------


@pytest.fixture
def notify(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[svc.NotifyResult]]:
    """Породить уведомление в своей сессии и закоммитить (как это делает
    планировщик через ``notify_now``, только без Pub/Sub)."""

    async def _notify(**kwargs: Any) -> svc.NotifyResult:
        async with db_sessionmaker() as session:
            result = await svc.notify(session, **kwargs)
            await session.commit()
            return result

    return _notify


def auth(tokens: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


# --- реестр событий как контракт (14 §2) -------------------------------------


def test_every_kind_is_wellformed():
    for kind, spec in svc.KINDS.items():
        assert spec.severity in SEVERITIES, kind
        assert spec.audience is None or spec.audience in AUDIENCES, kind
        assert spec.dedup in ("kind", "entity", "none"), kind
        assert spec.action_code is None or spec.action_code in svc.ACTIONS, kind
        assert spec.title.strip(), kind


def test_catalog_covers_the_three_groups_of_docs_14_2():
    """Системные, обращения сотрудников, рабочие — все три группы на месте."""
    # Группа определяется тем, О ЧЁМ событие, а не кому адресовано: «диалог
    # никто не принял» (очередь «Входящие», план 7.1) — событие рабочее, хотя
    # получатели у него администраторы: разбирают такой затор они, передачей
    # (01 §5.5), а не руководитель уговорами.
    system = {
        k
        for k, s in svc.KINDS.items()
        if s.audience == "admin" and not k.startswith(("support.", "auth.", "conversation."))
    }
    requests = {k for k in svc.KINDS if k.startswith(("support.", "auth."))}
    work = {k for k in svc.KINDS if k.startswith("conversation.")}
    # 14 §2.1 — девять системных событий плюс `system.unreachable`: его шлёт
    # внешний наблюдатель со второго сервера (14 §5), потому что проверка «жив
    # ли сайт» с самого сервера не переживает его падения.
    # `inbox.cleaned` (отчёт ночной уборки очереди) здесь БЫЛ и удалён вместе с
    # самой уборкой — требование владельца от 11 августа (№8): разгрузка
    # очереди убрана целиком, и сообщать стало не о чем. Отсюда 12, а не 13.
    # Плюс `inbound.unparsed` — сообщения от клиентов, которые мы не смогли
    # разобрать. До боевого Авито их не бывало: имитатор шлёт один текст. С
    # фотографиями, голосовыми и видео они появятся, и «клиент написал, а до
    # людей не доехало» обязано быть слышно.
    # Плюс `webhook.lost` — канал отобрали: наша подписка на события Авито
    # пропала. Авито держит на аккаунт одну подписку, и любой, кто подпишется
    # после нас, нашу заменит; снаружи это выглядит просто как затишье.
    # Плюс `leadbot.down` — лид-бот не отвечает (аудит 19.08): сторож заводил
    # этот вид с 18.08, а каталог о нём не знал, и уведомление приезжало без
    # кнопки и с записью `notification.unknown_kind` в журнал. Отсюда 13.
    # Плюс `avito.unreachable` и `inbound.dropped_disabled` (аудит 19.08,
    # находки L-002 и L-004): «площадка не отвечает» и «клиенты пишут в
    # выключенный канал» были видны только тому, кто читает журнал контейнера,
    # то есть никому. Отсюда 15.
    # Плюс `bot.phantom_reply` (замер 08.09): движок насчитал реплику бота,
    # которой в переписке нет. Счётчик — предохранитель «не заваливать клиента»,
    # и фантомный счёт закрывает боту рот за несказанное; на бою так прожили
    # 127 диалогов, и увидеть это можно было только сличкой счётчика с лентой
    # руками. Отсюда 16.
    # Плюс `client_merge.autostopped` (12.09): автообъединение карточек само
    # ушло в тень после двух откатов людьми за сутки — решать, включать ли
    # обратно, владельцу, и узнать он об этом обязан не из журнала. Отсюда 17.
    # Плюс `address.funnel_dropped` (18.09): адрес привязывается к карточке
    # без человека, и единственный способ узнать, что автоматика стала делать
    # это хуже, — недельный замер доли; падение обязано быть слышно, а не
    # лежать в снимке таблицы. Отсюда 18.
    # Плюс `address.rule_degraded` и `address.rule_promoted` (20.09, пакет
    # 6.0а): политику правила адреса двигает задача планировщика по интервалу
    # Уилсона — без человека; человек обязан узнать, что правило понижено (и
    # чем снять уже записанное) или что через семь дней оно станет `exact`.
    # Отсюда 20.
    # Плюс `gateway.down` (24.09): шлюз внешних сервисов в Амстердаме молчал,
    # и видно это было одной строкой журнала скрипта на хосте. Отсюда 21.
    assert len(system) == 21, sorted(system)
    assert "system.unreachable" in system
    assert len(requests) == 3, sorted(requests)  # 14 §2.2
    # 14 §2.3 — четыре рабочих события плюс `conversation.unclaimed` из 7.1.
    # Плюс `conversation.awaiting_you` — личное напоминание тому, кто ведёт
    # диалог, когда клиент ждёт. Отдельно от `no_reply`: то руководителю про
    # чужой диалог, это оператору про свой.
    #
    # Седьмой — `conversation.transfer_expired` (11 августа, SCEN-12/11):
    # предложение передачи, которое никто не подтвердил за пятнадцать минут,
    # снималось МОЛЧА. Полоса «Ждёт подтверждения» исчезала, и передавший
    # оставался в уверенности, что диалог отдал, — а диалог всё это время его.
    #
    # Восьмым был `conversation.snooze_due` (docs/38 §7) — «срок отложки истёк».
    # Снят 12 августа вместе со всей отложкой решением владельца, отсюда 7.
    # Плюс `conversation.transfer_declined` (аудит 19.08): явный отказ коллеги
    # был НЕМЫМ — сервис честно возвращал, кому сообщить, а сообщать было
    # нечем, и передавший не узнавал ничего. Отсюда 8.
    # Девятым стал `conversation.closed_by_other` (03.09): диалог закрыл не тот,
    # кто его вёл. Кадры о закрытии веерные и одинаковые для всех, и без этой
    # строки хозяин видел только, что поле заперлось, а диалог ушёл из «Моих», —
    # то есть читал чужое действие как поломку.
    # Десятый — `conversation.transfer_cancelled` (04.09): передающий забрал
    # предложение назад. Единственная весть о передаче в сторону ПОЛУЧАТЕЛЯ
    # после самого предложения: «Вам передали диалог» он уже получил, а
    # закрывающей не было — и оставался с долгом по диалогу, которого нет ни в
    # «Моих», ни в очереди.
    # Одиннадцатый — `conversation.invited` (24.09): приглашение в диалог шло
    # видом передачи, склеивалось с её непрочитанной вестью и не доходило.
    assert len(work) == 11, sorted(work)
    assert "conversation.unclaimed" in work
    assert "conversation.snooze_due" not in work, (
        "вид уведомления об отложке вернулся в реестр, а порождать его некому: "
        "сторож возврата удалён вместе со статусом snoozed"
    )


def test_every_critical_kind_has_a_button_or_an_explicit_reason():
    """14 §3: у критичного — действие одной кнопкой. Исключение только явное."""
    for kind, spec in svc.KINDS.items():
        if spec.severity != "critical":
            continue
        assert spec.action_code or kind in svc.ACTIONLESS_CRITICAL, (
            f"критичное событие {kind} без кнопки — добавьте действие в ACTIONS "
            "или причину в ACTIONLESS_CRITICAL"
        )
    assert set(svc.ACTIONLESS_CRITICAL) <= set(svc.KINDS)


def test_titles_speak_human_not_error_codes():
    """14 §4: «Никаких кодов ошибок в заголовке»."""
    for kind, spec in svc.KINDS.items():
        title = spec.title
        assert title[0].isupper(), kind
        assert "_" not in title and "{" not in title, kind
        assert not any(bad in title.lower() for bad in ("error", "exception", "traceback", "500"))


def test_button_codes_do_not_masquerade_as_audit_actions():
    """Кнопка уведомления не должна попадать в реестр журнала аудита.

    В этом коде ``action="сущность.событие"`` — зарезервированная запись строки
    журнала (06 §0.3), и её страж (tests/unit/test_audit.py) ищет литерал по
    всему ``app/`` обычным текстовым поиском. Пока поле каталога называлось
    ``action``, коды кнопок ``account.reconnect`` и ``user.password_reset_link``
    попадали в чужой реестр и валили его тест. Поле переименовано в
    ``action_code``; эта строка не даёт откатить решение молча.
    """
    source = (
        Path(svc.__file__).read_text(encoding="utf-8")
        if svc.__file__
        else pytest.fail("модуль центра уведомлений без файла")
    )
    stray = re.findall(r'action="([a-z_]+\.[a-z_]+)"', source)
    assert not stray, (
        f"литералы action= в центре уведомлений попадут в реестр аудита: {stray}. "
        "Кнопка — это action_code, запись журнала — action"
    )


def test_action_targets_are_late_import_paths():
    for code, action in svc.ACTIONS.items():
        assert code == action.code
        module, sep, attr = action.target.partition(":")
        assert sep and module.startswith("app.") and attr, code
        assert action.permission in {p for perms in ROLE_PERMISSIONS.values() for p in perms}
        assert action.label.strip()


# --- подавление повторов (14 §4) ---------------------------------------------


@pytest.mark.parametrize(
    "severity,inside,outside",
    [
        ("critical", timedelta(minutes=59), timedelta(minutes=61)),
        ("warning", timedelta(hours=5), timedelta(hours=7)),
        ("info", timedelta(hours=23), timedelta(hours=25)),
    ],
)
async def test_repeat_inside_the_window_collapses_and_outside_creates_a_row(
    db: AsyncSession, severity: str, inside: timedelta, outside: timedelta
):
    """Окно скользит от последнего повтора, а не от первого события.

    Так требует пример 14 §4: попытки раз в полчаса всю ночь дают ОДНУ строку
    «повторялось 12 раз», а не по строке в час. Смысл окна при этом — «событие
    замолчало на N и вернулось»: это уже новость, и ей нужна своя строка.
    """
    t0 = datetime.now(UTC) - timedelta(days=2)
    first = await svc.notify(
        db, kind="disk.space", severity=severity, title="Диск заполняется", now=t0
    )
    await db.commit()

    again = await svc.notify(
        db, kind="disk.space", severity=severity, title="Диск заполняется", now=t0 + inside
    )
    await db.commit()
    assert again.created is False
    assert again.notification.id == first.notification.id
    assert again.notification.repeat_count == 2
    assert svc.as_utc(again.notification.last_seen_at) == t0 + inside
    # created_at остаётся временем ПЕРВОГО события — иначе теряется «когда началось»
    assert svc.as_utc(again.notification.created_at) == t0

    quiet_then_back = t0 + inside + outside
    later = await svc.notify(
        db,
        kind="disk.space",
        severity=severity,
        title="Диск заполняется",
        now=quiet_then_back,
    )
    await db.commit()
    assert later.created is True
    assert later.notification.id != first.notification.id
    assert later.notification.repeat_count == 1


async def test_night_storm_becomes_one_line_with_a_counter(db: AsyncSession):
    """Сценарий из 14 §4: аккаунт отвалился ночью, попытка раз в 30 минут."""
    t0 = datetime.now(UTC) - timedelta(hours=8)
    account_id = str(uuid.uuid4())
    for i in range(12):
        await svc.notify(
            db,
            kind="account.needs_reauth",
            title="Аккаунт «LP-Москва» требует переподключения",
            body="Приём сообщений по этому аккаунту остановлен",
            entity_id=account_id,
            now=t0 + timedelta(minutes=30 * i),
        )
    await db.commit()

    rows = (await db.execute(select(Notification))).scalars().all()
    # Окно критичных — час, попытки идут раз в полчаса: каждая попадает в окно
    # предыдущей, поэтому строка ровно одна.
    assert len(rows) == 1
    assert rows[0].repeat_count == 12


async def test_repeat_after_read_reopens_the_same_row(
    db: AsyncSession, users_by_role: dict[str, User]
):
    """Повтор после подтверждения возвращает ТУ ЖЕ строку в непрочитанные.

    ЗДЕСЬ БЫЛ ТЕСТ С ОБРАТНЫМ ОЖИДАНИЕМ (`test_read_notification_is_not_
    collapsed`): он требовал, чтобы повтор после прочтения заводил НОВУЮ
    запись, и объяснял это тем, что «не починилось» — новость, а не дубль.
    Новость там названа верно, а вывод из неё был неверен, и держался он на
    рассуждении, а не на наблюдении.

    Наблюдение пришло с прода. Сторож «Приём сообщений остановился» бежит раз
    в пять минут, критичное поднимает красную плашку, и подтвердить её человек
    обязан, чтобы работать дальше. Заказчик получил ровно то, что этот тест
    закреплял как правильное: подтвердил — через пять минут вторая строка,
    подтвердил — третья. Обещанный каталогом счётчик «повторялось N раз» при
    этом не показывался НИКОГДА: до второго повтора в одной строке дело не
    доходило.

    Правильное поведение — одна строка на ключ склейки в пределах окна, а
    «не починилось» доносится снятием отметки прочтения у неё же.
    """
    t0 = datetime.now(UTC) - timedelta(hours=5)
    first = await svc.notify(db, kind="backup.failed", now=t0)
    await db.commit()

    await svc.mark_read(db, users_by_role[A], first.notification)
    await db.commit()

    again = await svc.notify(db, kind="backup.failed", now=t0 + timedelta(minutes=10))
    await db.commit()
    assert again.created is False
    assert again.notification.id == first.notification.id
    assert again.notification.repeat_count == 2
    assert again.revived is True
    # Строка одна, отметок прочтения не осталось: колокольчик снова горит.
    assert len((await db.execute(select(Notification))).scalars().all()) == 1
    assert (await db.execute(select(NotificationRead))).first() is None
    assert (await svc.unread_counts(db, users_by_role[A]))["unread"] == 1


async def test_twelve_repeats_with_reads_between_them_stay_one_row(
    db: AsyncSession, users_by_role: dict[str, User]
):
    """Двенадцать повторов сторожа с подтверждением после каждого.

    Это и есть жалоба №8 в чистом виде: раньше здесь выросло бы двенадцать
    строк «Приём сообщений остановился». Должна остаться одна с честным
    `repeat_count`.
    """
    t0 = datetime.now(UTC) - timedelta(hours=1)
    account_id = str(uuid.uuid4())
    for i in range(12):
        result = await svc.notify(
            db, kind="inbound.stalled", entity_id=account_id, now=t0 + timedelta(minutes=5 * i)
        )
        await db.commit()
        await svc.mark_read(db, users_by_role[A], result.notification)
        await db.commit()

    rows = (await db.execute(select(Notification))).scalars().all()
    assert len(rows) == 1
    assert rows[0].repeat_count == 12


async def test_repeat_does_not_multiply_rows_for_the_second_admin(
    db: AsyncSession, users_by_role: dict[str, User], make_user
):
    """Прочтение одним администратором — не подтверждение за всех.

    Раньше рассылка считалась подтверждённой, как только её прочитал хоть кто
    угодно: первый админ нажимал «прочитано», следующий повтор заводил вторую
    строку, и у ВТОРОГО админа в колокольчике становилось два непрочитанных
    сообщения об одной и той же поломке.
    """
    second = await make_user("admin2@leadchat.test", role=A)
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    first = await svc.notify(db, kind="backup.failed", now=t0)
    await db.commit()
    await svc.mark_read(db, users_by_role[A], first.notification)
    await db.commit()

    await svc.notify(db, kind="backup.failed", now=t0 + timedelta(minutes=10))
    await db.commit()

    assert len((await db.execute(select(Notification))).scalars().all()) == 1
    assert (await svc.unread_counts(db, second))["unread"] == 1
    assert (await svc.unread_counts(db, users_by_role[A]))["unread"] == 1


async def test_reopened_repeat_reaches_the_browser(
    db: AsyncSession, redis, users_by_role: dict[str, User]
):
    """Оживший повтор обязан доехать кадром: иначе плашка не вернётся.

    Строка в списке уже есть, и без кадра интерфейс о смене состояния не
    узнает до перезагрузки — то есть человек подтвердит поломку и увидит
    пустой экран при продолжающейся поломке.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    t0 = datetime.now(UTC)
    first = await svc.notify_now(db, redis, kind="backup.failed", now=t0)
    assert len(await drain_events(pubsub)) == 1

    # без подтверждения повтор молчит (14 §4)
    await svc.notify_now(db, redis, kind="backup.failed", now=t0 + timedelta(minutes=5))
    assert await drain_events(pubsub) == []

    await svc.mark_read(db, users_by_role[A], first.notification)
    await db.commit()

    await svc.notify_now(db, redis, kind="backup.failed", now=t0 + timedelta(minutes=10))
    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "notify"
    assert evt["data"]["is_repeat"] is True
    assert evt["data"]["repeat_count"] == 3


async def test_dedup_none_never_collapses(db: AsyncSession):
    """«Сообщение администратору» — каждое обращение своё (dedup='none')."""
    now = datetime.now(UTC)
    a = await svc.notify(db, kind="support.message", title="Вопрос про шаблоны", now=now)
    b = await svc.notify(db, kind="support.message", title="Не открывается фото", now=now)
    await db.commit()
    assert a.notification.id != b.notification.id
    assert a.notification.dedup_key is None


async def test_dedup_is_per_entity_not_per_kind(db: AsyncSession):
    """Два разных аккаунта — два уведомления, а не одно с счётчиком."""
    now = datetime.now(UTC)
    one = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-1", now=now)
    two = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-2", now=now)
    await db.commit()
    assert one.notification.id != two.notification.id


async def test_repeat_refreshes_text_and_extends_the_lifetime(db: AsyncSession):
    t0 = datetime.now(UTC) - timedelta(minutes=40)
    first = await svc.notify(db, kind="queue.backlog", body="Очередь: 120 сообщений", now=t0)
    await db.commit()
    first_expires = svc.as_utc(first.notification.expires_at)

    again = await svc.notify(
        db, kind="queue.backlog", body="Очередь: 3 400 сообщений", now=t0 + timedelta(minutes=30)
    )
    await db.commit()
    assert again.notification.body == "Очередь: 3 400 сообщений"
    assert svc.as_utc(again.notification.expires_at) > (first_expires or datetime.now(UTC))


async def test_escalation_is_not_swallowed_by_the_collapse(db: AsyncSession, redis):
    """Повтор с ПОВЫШЕННОЙ важностью поднимает важность строки и доходит до
    браузера.

    Живой источник — сторож сертификата (scheduler/jobs/watchdog): «истекает
    через 3 дн.» (info) и «истёк» (critical) идут одним ключом ``cert.expiring:
    <дата сертификата>``, потому что продлённый сертификат обязан быть новым
    событием. Если важность берётся только у первой записи, критичное событие
    тихо ложится в строку с важностью info: красной плашки (14 §3) не будет, а
    в списке останется критичный текст, покрашенный как обычный.
    """
    t0 = datetime.now(UTC) - timedelta(minutes=40)
    key = "cert.expiring:2026-09-01"
    calm = await svc.notify(
        db,
        kind="cert.expiring",
        severity="info",
        title="Сертификат сайта истекает через 1 дн.",
        dedup_key=key,
        now=t0,
    )
    await db.commit()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    loud = await svc.notify_now(
        db,
        redis,
        kind="cert.expiring",
        severity="critical",
        title="Сертификат сайта истёк",
        dedup_key=key,
        now=t0 + timedelta(minutes=30),
    )
    assert loud.notification.id == calm.notification.id  # строка та же (14 §4)
    assert loud.created is False and loud.notification.repeat_count == 2
    assert loud.notification.severity == "critical"
    assert loud.notification.title == "Сертификат сайта истёк"

    # Обычный повтор молчит (иначе центр — мусорка), но повышение важности —
    # это новость, и тост обязан уйти.
    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "notify"
    assert evt["data"]["level"] == "error"  # critical → error (01 §11.3)
    assert evt["data"]["severity"] == "critical"
    assert evt["data"]["is_repeat"] is True


async def test_collapse_never_downgrades_the_severity(db: AsyncSession):
    """Понижение важности уже показанную тревогу не гасит и текст не подменяет:
    иначе критичная строка получит успокаивающий заголовок."""
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    key = "cert.expiring:2026-09-01"
    await svc.notify(
        db,
        kind="cert.expiring",
        severity="critical",
        title="Сертификат сайта истёк",
        dedup_key=key,
        now=t0,
    )
    calmer = await svc.notify(
        db,
        kind="cert.expiring",
        severity="info",
        title="Сертификат сайта истекает через 3 дн.",
        dedup_key=key,
        now=t0 + timedelta(minutes=5),
    )
    await db.commit()
    assert calmer.created is False and calmer.notification.repeat_count == 2
    assert calmer.notification.severity == "critical"
    assert calmer.notification.title == "Сертификат сайта истёк"


# --- адресация и права (DESIGN §5.1) ------------------------------------------


def test_visible_audiences_matches_the_design_matrix():
    """Системное — только админ; рабочее — админ и руководитель; менеджеру и
    наблюдателю рассылок не положено (14 §2, DESIGN §5.1)."""
    assert svc.visible_audiences(A) == ("admin", "head")
    assert svc.visible_audiences(H) == ("head",)
    assert svc.visible_audiences(M) == ()
    assert svc.visible_audiences(O) == ()
    assert [svc.can_read_notifications(r) for r in (A, H, M, O)] == [True, True, True, False]


async def test_addressing_requires_exactly_one_address(db: AsyncSession):
    with pytest.raises(ValueError):
        await svc.notify(db, kind="support.message", recipient_id=uuid.uuid4(), audience="admin")
    with pytest.raises(ValueError):
        # conversation.assigned — адресное событие, а получателя не передали
        await svc.notify(db, kind="conversation.assigned")


async def test_system_notification_is_visible_only_to_admins(
    client, tokens, notify: Callable[..., Awaitable[svc.NotifyResult]]
):
    await notify(kind="account.needs_reauth", entity_id="acc-1")

    seen = {}
    for role in (A, H, M, O):
        r = await client.get("/api/v1/notifications", headers=auth(tokens, role))
        if role == O:
            assert r.status_code == 403, r.text
            continue
        assert r.status_code == 200, r.text
        seen[role] = [i["kind"] for i in r.json()["items"]]
    assert seen[A] == ["account.needs_reauth"]
    assert seen[H] == [] and seen[M] == []


async def test_work_notification_reaches_head_and_admin_only(
    client, tokens, notify: Callable[..., Awaitable[svc.NotifyResult]]
):
    await notify(kind="conversation.negative", entity_id=str(uuid.uuid4()))
    for role, expected in ((A, 1), (H, 1), (M, 0)):
        r = await client.get("/api/v1/notifications", headers=auth(tokens, role))
        assert r.status_code == 200, r.text
        assert r.json()["page"]["total"] == expected, role


async def test_personal_notification_reaches_only_its_recipient(
    client, tokens, users_by_role: dict[str, User], notify
):
    await notify(
        kind="conversation.assigned",
        recipient_id=users_by_role[M].id,
        entity_id=str(uuid.uuid4()),
    )
    r = await client.get("/api/v1/notifications", headers=auth(tokens, M))
    assert [i["kind"] for i in r.json()["items"]] == ["conversation.assigned"]
    # Руководитель чужой личный ящик не читает — даже он.
    head = await client.get("/api/v1/notifications", headers=auth(tokens, H))
    assert head.json()["items"] == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
async def test_observer_sees_nothing_at_all(client, tokens, method: str, path: str):
    r = await client.request(method, path, headers=auth(tokens, O))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path", ENDPOINTS)
async def test_anonymous_is_rejected(client, method: str, path: str):
    r = await client.request(method, path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_foreign_notification_is_404_not_403(client, tokens, notify):
    """«Такого уведомления у вас нет» не должно подтверждать, что оно есть у
    кого-то другого."""
    result = await notify(kind="account.needs_reauth", entity_id="acc-1")
    r = await client.post(
        f"/api/v1/notifications/{result.notification.id}/read", headers=auth(tokens, M)
    )
    assert r.status_code == 404, r.text


# --- счётчик и прочтение ------------------------------------------------------


async def test_unread_count_is_split_by_severity(client, tokens, notify):
    await notify(kind="account.needs_reauth", entity_id="acc-1")  # critical
    await notify(kind="disk.space")  # warning
    await notify(kind="cert.expiring")  # info

    r = await client.get("/api/v1/notifications/unread-count", headers=auth(tokens, A))
    assert r.status_code == 200, r.text
    assert r.json() == {"unread": 3, "critical": 1, "warning": 1, "info": 1}

    # У менеджера тот же счётчик показывает ноль: это не его уведомления.
    empty = await client.get("/api/v1/notifications/unread-count", headers=auth(tokens, M))
    assert empty.json()["unread"] == 0


async def test_broadcast_read_by_one_admin_stays_unread_for_another(
    client, tokens, make_user, notify, db: AsyncSession
):
    """Рассылка по роли: «прочитано» персонально (см. модель — отдельная
    таблица notification_reads именно ради этого)."""
    second = await make_user("admin2@leadchat.test", role=A)
    second_token = create_access_token(user_id=str(second.id), role=second.role)
    result = await notify(kind="backup.failed")

    read = await client.post(
        f"/api/v1/notifications/{result.notification.id}/read", headers=auth(tokens, A)
    )
    assert read.status_code == 200, read.text
    assert read.json() == {"marked": 1, "unread": 0}

    other = await client.get(
        "/api/v1/notifications/unread-count", headers={"Authorization": f"Bearer {second_token}"}
    )
    assert other.json()["unread"] == 1

    # Строка одна на всех, отметок — по числу прочитавших.
    assert len((await db.execute(select(Notification))).scalars().all()) == 1
    assert len((await db.execute(select(NotificationRead))).scalars().all()) == 1


async def test_read_is_idempotent(client, tokens, notify):
    result = await notify(kind="backup.failed")
    url = f"/api/v1/notifications/{result.notification.id}/read"
    assert (await client.post(url, headers=auth(tokens, A))).json()["marked"] == 1
    assert (await client.post(url, headers=auth(tokens, A))).json()["marked"] == 0


async def test_read_all_marks_only_what_this_role_sees(client, tokens, notify):
    await notify(kind="backup.failed")  # admin
    await notify(kind="conversation.negative", entity_id=str(uuid.uuid4()))  # head+admin

    head = await client.post("/api/v1/notifications/read-all", headers=auth(tokens, H))
    assert head.json() == {"marked": 1, "unread": 0}

    admin = await client.get("/api/v1/notifications/unread-count", headers=auth(tokens, A))
    assert admin.json()["unread"] == 2  # чужое «прочитано» админа не касается

    admin_all = await client.post("/api/v1/notifications/read-all", headers=auth(tokens, A))
    # Критичное «копия не создана» остаётся до поимённого подтверждения.
    assert admin_all.json() == {"marked": 1, "unread": 1}


async def test_read_all_leaves_critical_until_confirmed_one_by_one(client, tokens, notify):
    await notify(kind="backup.failed")  # critical
    await notify(kind="cert.expiring")  # не critical

    marked = await client.post("/api/v1/notifications/read-all", headers=auth(tokens, A))
    assert marked.json() == {"marked": 1, "unread": 1}

    unread = await client.get("/api/v1/notifications?unread_only=true", headers=auth(tokens, A))
    assert [i["kind"] for i in unread.json()["items"]] == ["backup.failed"]


# --- список: фильтры, сортировка, страница ------------------------------------


async def test_list_filters_and_ordering(client, tokens, notify):
    old = datetime.now(UTC) - timedelta(days=3)
    await notify(kind="cert.expiring", now=old)
    await notify(kind="account.needs_reauth", entity_id="acc-1")

    by_severity = await client.get(
        "/api/v1/notifications?severity=critical", headers=auth(tokens, A)
    )
    assert [i["kind"] for i in by_severity.json()["items"]] == ["account.needs_reauth"]

    by_kind = await client.get("/api/v1/notifications?kind=cert.expiring", headers=auth(tokens, A))
    assert [i["kind"] for i in by_kind.json()["items"]] == ["cert.expiring"]

    today = datetime.now(UTC).date().isoformat()
    by_period = await client.get(
        f"/api/v1/notifications?date_from={today}", headers=auth(tokens, A)
    )
    assert [i["kind"] for i in by_period.json()["items"]] == ["account.needs_reauth"]

    # Свежее сверху — по последнему повтору, а не по первому событию.
    all_items = await client.get("/api/v1/notifications", headers=auth(tokens, A))
    assert [i["kind"] for i in all_items.json()["items"]] == [
        "account.needs_reauth",
        "cert.expiring",
    ]
    assert all_items.json()["unread"] == 2


async def test_period_takes_what_was_alive_in_it_not_only_what_began_in_it(
    client, tokens, notify, db_sessionmaker
):
    """Строка журнала стоит и сортируется по последнему повтору — по нему же и период.

    Проверка 24.09: «Клиент ждёт вашего ответа» заведено 15.09 и повторялось до
    сегодня. Во «Всё время» оно первое со временем «сегодня 10:12», а в
    «Сегодня» его не было, потому что период резал по `created_at`. На бою 1 340
    живых строк с последним повтором в более поздний день, чем первый.
    """
    now = datetime.now(UTC)
    days_ago = now - timedelta(days=3)
    repeated = await notify(kind="cert.expiring", now=days_ago)
    await notify(kind="account.needs_reauth", entity_id="acc-1", now=days_ago)
    async with db_sessionmaker() as session:
        row = await session.get(Notification, repeated.notification.id)
        assert row is not None
        row.last_seen_at = now
        await session.commit()

    def period(day: datetime) -> str:
        # Сутки фильтра московские (06 §0.1).
        msk = (day + timedelta(hours=3)).date().isoformat()
        return f"date_from={msk}&date_to={msk}"

    today = await client.get(f"/api/v1/notifications?{period(now)}", headers=auth(tokens, A))
    assert [i["kind"] for i in today.json()["items"]] == ["cert.expiring"]

    # В день первого события строка тоже жива — оба уведомления там.
    back_then = await client.get(
        f"/api/v1/notifications?{period(days_ago)}", headers=auth(tokens, A)
    )
    assert sorted(i["kind"] for i in back_then.json()["items"]) == [
        "account.needs_reauth",
        "cert.expiring",
    ]


async def test_list_rejects_unknown_severity_and_reversed_period(client, tokens):
    bad = await client.get("/api/v1/notifications?severity=fatal", headers=auth(tokens, A))
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "validation_error"

    reversed_period = await client.get(
        "/api/v1/notifications?date_from=2026-08-05&date_to=2026-08-01", headers=auth(tokens, A)
    )
    assert reversed_period.status_code == 400


async def test_unread_only_filter(client, tokens, notify):
    first = await notify(kind="backup.failed")
    await notify(kind="disk.space")
    await client.post(
        f"/api/v1/notifications/{first.notification.id}/read", headers=auth(tokens, A)
    )

    r = await client.get("/api/v1/notifications?unread_only=true", headers=auth(tokens, A))
    assert [i["kind"] for i in r.json()["items"]] == ["disk.space"]
    full = await client.get("/api/v1/notifications", headers=auth(tokens, A))
    assert {i["kind"]: i["is_read"] for i in full.json()["items"]} == {
        "backup.failed": True,
        "disk.space": False,
    }


async def test_item_carries_the_button_of_its_kind(client, tokens, notify):
    await notify(kind="account.needs_reauth", entity_id="acc-42")
    (item,) = (await client.get("/api/v1/notifications", headers=auth(tokens, A))).json()["items"]
    assert item["action"] == {"code": "account.reconnect", "label": "Переподключить"}
    assert item["entity"] == {"type": "account", "id": "acc-42"}
    assert item["repeat_count"] == 1 and item["audience"] == "admin"


# --- доставка в браузер (01 §11.3 `notify`) -----------------------------------


async def test_notify_now_publishes_a_notify_frame_for_admins(db: AsyncSession, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    await svc.notify_now(
        db,
        redis,
        kind="account.needs_reauth",
        title="Аккаунт «LP-Москва» требует переподключения",
        entity_id="acc-1",
    )
    (evt,) = await drain_events(pubsub)
    assert evt["type"] == "notify"
    assert evt["meta"]["audience"] == "admin"  # хаб отсеет не-админов (08 §5.3)
    assert evt["data"]["level"] == "error"  # critical → error (01 §11.3)
    assert evt["data"]["title"] == "Аккаунт «LP-Москва» требует переподключения"
    assert evt["data"]["action"]["label"] == "Переподключить"


async def test_head_broadcast_is_addressed_not_shouted(
    db: AsyncSession, redis, users_by_role: dict[str, User]
):
    """Кадр рассылки руководителю уходит адресно (only_user), а не всем: хаб
    сегодня фильтрует по роли только `audience='admin'`, и общий кадр увидел бы
    даже наблюдатель."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    await svc.notify_now(db, redis, kind="conversation.negative", entity_id=str(uuid.uuid4()))
    events = await drain_events(pubsub)
    targets = {e["meta"]["only_user"] for e in events}
    assert targets == {str(users_by_role[A].id), str(users_by_role[H].id)}
    assert all(e["meta"]["audience"] is None for e in events)


async def test_suppressed_repeat_does_not_toast(db: AsyncSession, redis):
    """Двенадцать ночных попыток — один тост, а не двенадцать (14 §4)."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    t0 = datetime.now(UTC)
    await svc.notify_now(db, redis, kind="backup.failed", now=t0)
    assert len(await drain_events(pubsub)) == 1
    await svc.notify_now(db, redis, kind="backup.failed", now=t0 + timedelta(minutes=20))
    assert await drain_events(pubsub) == []


# --- действия кнопки ----------------------------------------------------------


async def _fake_reconnect(db, redis, *, entity_id, actor) -> dict[str, Any]:
    """Заглушка чужой зоны — контракт из ACTIONS.target."""
    return {"account_id": entity_id, "by": str(actor.id)}


async def test_action_runs_the_registered_function_and_marks_read(
    client, tokens, notify, monkeypatch
):
    monkeypatch.setitem(
        svc.ACTIONS,
        "account.reconnect",
        svc.ActionSpec(
            code="account.reconnect",
            label="Переподключить",
            permission="accounts:manage",
            target="tests.unit.test_notifications:_fake_reconnect",
        ),
    )
    result = await notify(kind="account.needs_reauth", entity_id="acc-7")

    r = await client.post(
        f"/api/v1/notifications/{result.notification.id}/action", headers=auth(tokens, A)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "account.reconnect"
    assert body["result"]["account_id"] == "acc-7"
    # Нажал кнопку — значит увидел: уведомление гасится само.
    assert body["unread"] == 0


async def test_action_of_an_unwritten_module_is_503_not_500(client, tokens, notify, monkeypatch):
    """Реестр указывает на чужие зоны; ненаписанный модуль — это «пока
    недоступно» человеческим языком, а не падение.

    Цель подменяется на заведомо отсутствующий модуль, а не берётся «та зона,
    которую ещё не написали»: зоны дописываются, и такой тест тихо перестал бы
    проверять 503 ровно в тот день, когда модуль появился (так и случилось с
    ``app.services.users`` — тест начал ловить 404 «сотрудник не найден» и
    больше не сторожил ветку позднего импорта).
    """
    monkeypatch.setitem(
        svc.ACTIONS,
        "user.password_reset_link",
        svc.ActionSpec(
            code="user.password_reset_link",
            label="Выслать новую ссылку",
            permission="users:manage",
            target="app.services.not_written_yet:issue_password_reset_action",
        ),
    )
    result = await notify(kind="support.password_reset", entity_id=str(uuid.uuid4()))
    r = await client.post(
        f"/api/v1/notifications/{result.notification.id}/action", headers=auth(tokens, A)
    )
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "action_unavailable"
    assert "вручную" in r.json()["error"]["message"]


def _resolves(action: svc.ActionSpec) -> bool:
    module_name, _, attr = action.target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return callable(getattr(module, attr, None))


def test_registered_action_targets_resolve_once_their_zone_exists():
    """Опечатка в имени функции реестра не должна ждать нажатия кнопки.

    Ненаписанная зона — законное состояние (ветка 503 выше именно про это), но
    только с явной записью в ``ACTIONS_PENDING_ZONE``. Всё остальное обязано
    отдавать вызываемую функцию: иначе кнопка навсегда молча превращается в
    «действие пока недоступно», и никто этого не заметит.
    """
    for code, action in svc.ACTIONS.items():
        if code in svc.ACTIONS_PENDING_ZONE:
            continue
        assert _resolves(action), (
            f"{code}: {action.target} не отдаёт вызываемую функцию — "
            "почините цель или впишите причину в ACTIONS_PENDING_ZONE"
        )


def test_pending_actions_stay_honest():
    """Обратная сторона: запись протухла — значит кнопка молчит зря.

    Тот же страж, что у PENDING_ACTIONS в журнале аудита. Как только чужая зона
    напишет функцию, этот тест потребует убрать строку — иначе критичное
    уведомление так и останется с неработающей кнопкой (14 §3).
    """
    assert set(svc.ACTIONS_PENDING_ZONE) <= set(svc.ACTIONS)
    for code, reason in svc.ACTIONS_PENDING_ZONE.items():
        assert reason.strip(), code
        assert not _resolves(svc.ACTIONS[code]), (
            f"{code}: цель уже написана — уберите строку из ACTIONS_PENDING_ZONE"
        )


async def test_notification_without_a_button_rejects_action(client, tokens, notify):
    result = await notify(kind="backup.failed")
    r = await client.post(
        f"/api/v1/notifications/{result.notification.id}/action", headers=auth(tokens, A)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "action_not_supported"


async def test_action_checks_its_own_permission(
    db: AsyncSession, redis, users_by_role: dict[str, User]
):
    """Второй рубеж: даже увидев уведомление, кнопку жмёт только тот, у кого
    есть право самого действия (`accounts:manage` — admin, 01 §12)."""
    result = await svc.notify(db, kind="account.needs_reauth", entity_id="acc-1")
    await db.commit()
    with pytest.raises(ApiError) as exc:
        await svc.run_action(db, redis, user=users_by_role[H], row=result.notification)
    assert exc.value.status == 403


# --- чистка по сроку (14 §4) --------------------------------------------------


async def test_expired_notifications_are_deleted_and_never_shown(
    client, tokens, db: AsyncSession, notify
):
    fresh = await notify(kind="disk.space")
    stale = await notify(
        kind="cert.expiring", now=datetime.now(UTC) - timedelta(days=91), ttl_days=90
    )
    # Просроченное всё ещё лежит в базе: чистка ходит раз в сутки, а выдача
    # обязана быть честной между её заходами.
    assert svc.as_utc(stale.notification.expires_at) < datetime.now(UTC)

    listed = await client.get("/api/v1/notifications", headers=auth(tokens, A))
    assert [i["id"] for i in listed.json()["items"]] == [str(fresh.notification.id)]
    assert listed.json()["unread"] == 1  # просроченное не подсвечивает колокольчик

    deleted = await svc.cleanup_expired(db)
    await db.commit()
    assert deleted == 1
    left = (await db.execute(select(Notification.id))).scalars().all()
    assert left == [fresh.notification.id]
    assert svc.DEFAULT_TTL_DAYS == 90


def test_the_scheduler_process_runs_the_cleanup():
    """Функции мало — её должен звать процесс планировщика (14 §4).

    Пока job'а нет, таблица растёт вечно: на выдачу это не влияет (просроченное
    скрывает предикат живости — см. тест выше), поэтому потерю строки в
    `app/scheduler/main.py` не заметил бы ни один сценарий приёмки.
    """
    from app.scheduler.main import build_scheduler

    jobs = {job.id: job for job in build_scheduler().get_jobs()}
    assert "notifications_cleanup" in jobs, sorted(jobs)
    job = jobs["notifications_cleanup"]
    assert "hour=" in str(job.trigger)  # раз в сутки, а не раз в минуту
    assert job.max_instances == 1 and job.coalesce is True


async def test_cleanup_keeps_the_unexpired_even_when_they_are_old(db: AsyncSession, notify):
    """Срок считается по expires_at, а не по возрасту: повторившаяся поломка
    живёт 90 дней от ПОСЛЕДНЕГО повтора, а не от первого."""
    t0 = datetime.now(UTC) - timedelta(days=89)
    await svc.notify(db, kind="backup.failed", now=t0)
    await svc.notify(db, kind="backup.failed", now=t0 + timedelta(minutes=30))
    await db.commit()

    assert await svc.cleanup_expired(db) == 0
    assert len((await db.execute(select(Notification))).scalars().all()) == 1
