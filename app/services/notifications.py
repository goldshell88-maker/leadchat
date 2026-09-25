"""Центр уведомлений: единая точка порождения + подавление повторов (14 §4).

Уведомление — это **обращение к человеку**, а не строка лога: заголовок
человеческим языком, без кодов ошибок и трассировок; у критичных событий —
действие одной кнопкой (14 §1, §3). Поэтому здесь два реестра, а не один
свободный вызов:

* :data:`KINDS` — каталог событий 14 §2: важность по умолчанию, кому адресовано,
  как склеивать повторы, какая кнопка у события. Вызывающему остаётся текст.
* :data:`ACTIONS` — реестр действий кнопки. Сами действия живут в чужих модулях
  (переподключение аккаунта Авито, повторная ссылка установки пароля) и
  вызываются **поздним импортом** по строке ``"модуль:функция"``: центр
  уведомлений не тянет за собой пол-приложения и переживает отсутствие
  ненаписанного модуля — кнопка отвечает «действие пока недоступно», а не 500.

Транзакции (08 §8.1): :func:`notify` кладёт строку в переданную сессию и
**ничего не коммитит** — событие и его причина обязаны коммититься вместе.
Публикация в Pub/Sub — строго после commit, отдельным вызовом :func:`deliver`.
Тем, у кого уведомление и есть вся работа (планировщик, воркер доставки,
эндпоинт скрипта бэкапа), сделан :func:`notify_now` — commit и доставка внутри.

Чего этот модуль не делает: не решает, ЧТО является поломкой. Точки порождения
(планировщик, воркер, движок ботов) живут в своих зонах и зовут ``notify``.
"""

import importlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.rbac import ROLE_PERMISSIONS
from app.models import User
from app.models.notification import AUDIENCES, SEVERITIES, Notification, NotificationRead
from app.services.audit import msk_day_range
from app.ws.events import iso, publish_event

log = structlog.get_logger("app.notifications")

# Автоудаление (14 §4). Константа, а не настройка: в env-контракте 05 §4 своего
# ключа под неё нет, а config.py — чужая зона. Когда срок понадобится крутить —
# NOTIFICATION_TTL_DAYS в Settings и один аргумент по умолчанию здесь.
DEFAULT_TTL_DAYS = 90

# Окна подавления повторов (14 §4). Событие с тем же dedup_key, пришедшее
# внутри окна, не создаёт строку, а поднимает счётчик у существующей.
#
# Окно отсчитывается от ПОСЛЕДНЕГО повтора, а не от первого события — так
# требует пример из 14 §4: аккаунт отвалился в 3 ночи, планировщик пробует
# каждые полчаса, и утром админ видит одну строку «повторялось 12 раз». При
# отсчёте от первого события те же двенадцать попыток дали бы шесть строк.
# Смысл окна поэтому — «замолчало на N и вернулось»: это уже новость.
DEDUP_WINDOWS: dict[str, timedelta] = {
    "critical": timedelta(hours=1),
    "warning": timedelta(hours=6),
    "info": timedelta(days=1),
}

# Порядок важности — нужен склейке повторов (см. notify). Один и тот же
# dedup_key законно приходит с РАЗНОЙ важностью: сторож сертификата шлёт
# «истекает через 3 дн.» (info) и «истёк» (critical) под одним ключом
# `cert.expiring:<дата сертификата>`, потому что продлённый сертификат обязан
# быть новым событием, а тот же самый — тем же.
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


def reminders_due(waited: timedelta, first: timedelta, *, step_cap: timedelta) -> int:
    """Сколько напоминаний ПОЛОЖЕНО к моменту ``waited`` при первом на ``first``.

    Ступени: ``first``, дальше каждая следующая отстоит от предыдущей на всё
    прожитое ожидание (то есть удваивает его), пока шаг не упрётся в
    ``step_cap``. Для ``first=15 мин`` это 15, 30, 60, 120, 240…

    ЗАЧЕМ ЧИСЛО, А НЕ ОТВЕТ «ДА/НЕТ». Сравнивать его будем с тем, сколько
    напоминаний уже ушло (счётчик повторов строки), и такое сравнение ДОГОНЯЕТ
    пропуски. Проверка «текущий такт совпал со ступенью» была бы короче и
    теряла бы ступень целиком на каждом пропущенном такте, а потерянная
    ступень — это молчание до следующей, вдвое более дальней.

    ⚠ ЖИВЁТ ЗДЕСЬ, А НЕ В ЗАДАЧЕ НАПОМИНАНИЙ, С 14 АВГУСТА. Лестница была
    написана для `awaiting`, а у СТОРОЖЕВЫХ видов её не было вовсе: `watchdog`
    бежит раз в пять минут и слал на каждом проходе. Замер боя: «повторялось
    176 раз» — это 14 часов непрерывного условия, и ни один из 176 повторов не
    сообщал ничего нового. Две лестницы в двух файлах разъехались бы на первой
    же правке шага.
    """
    if waited < first:
        return 0
    count = 1
    at = first
    while True:
        at += min(at, step_cap)
        if waited < at:
            return count
        count += 1


def ladder_step_cap(severity: str) -> timedelta:
    """Потолок шага лестницы для этой важности — ПОЛОВИНА окна склейки.

    ⚠ ШАГ ОБЯЗАН БЫТЬ МЕНЬШЕ ОКНА, ИНАЧЕ ЛЕСТНИЦА ПРЕВРАЩАЕТСЯ ОБРАТНО В КУЧУ.
    Молчание длиннее окна означает, что следующая ступень не найдёт живой
    строки и заведёт НОВУЮ — то есть вместо одной строки с растущим счётчиком
    в колокольчике снова копятся отдельные строки, просто реже.

    Половина, а не «окно минус минута»: между двумя ступенями лежит ровно один
    шаг, и запас нужен на задержку такта (`misfire_grace_time`, длинная
    выкатка). У `critical` окно час — значит потолок полчаса; у `warning`
    шесть часов — три.
    """
    return DEDUP_WINDOWS[severity] / 2


# Соответствие важности и уровня в кадре `notify` (01 §11.3: info|warning|error).
WS_LEVELS: dict[str, str] = {"critical": "error", "warning": "warning", "info": "info"}


# --- права ------------------------------------------------------------------
#
# Своих прав (`notifications:read` / `notifications:system`) в каталоге 01 §12
# ещё нет, а app/core/rbac.py — чужая зона. Поэтому роль-получателя определяем
# через права-маркеры из той же матрицы: сравнивать роль со строкой нельзя —
# новая роль тихо получила бы чужие уведомления (08 §5.3, api/deps.has_permission).
# Когда свои права появятся, меняются ровно эти три константы.

# Раздел целиком: admin/head/manager, но не observer (DESIGN §5.1 — наблюдатель
# только читает диалоги; уведомлений он не видит вообще).
SECTION_PERMISSION = "conversations:manage"

# Кому видна рассылка по роли. `users:manage` — «Сотрудники и роли», только
# админ; `audit:read` — «Журнал аудита», админ и руководитель (DESIGN §5.1).
AUDIENCE_PERMISSION: dict[str, str] = {"admin": "users:manage", "head": "audit:read"}


def visible_audiences(role: str) -> tuple[str, ...]:
    """Какие рассылки видит роль. Порядок — как в :data:`AUDIENCES`."""
    granted = ROLE_PERMISSIONS.get(role, frozenset())
    return tuple(a for a in AUDIENCES if AUDIENCE_PERMISSION[a] in granted)


def can_read_notifications(role: str) -> bool:
    return SECTION_PERMISSION in ROLE_PERMISSIONS.get(role, frozenset())


# --- реестр действий --------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """Кнопка уведомления. ``target`` — ``"пакет.модуль:функция"``.

    Контракт вызываемой функции (её пишут в своей зоне):

        async def fn(db: AsyncSession, redis: Redis, *, entity_id: str | None,
                     actor: User) -> dict[str, Any]

    Возврат уезжает клиенту как ``result`` — например ссылка, которую надо
    открыть. Ошибку прикладного уровня функция поднимает своим ``ApiError``.
    """

    code: str
    label: str  # надпись на кнопке — человеческим языком (14 §3)
    permission: str  # без него кнопка не работает даже у видящего уведомление
    target: str


ACTIONS: dict[str, ActionSpec] = {
    "account.reconnect": ActionSpec(
        code="account.reconnect",
        label="Переподключить",
        permission="accounts:manage",
        target="app.services.avito_accounts:reconnect_account_action",
    ),
    "account.rewebhook": ActionSpec(
        code="account.rewebhook",
        label="Перерегистрировать",
        permission="accounts:manage",
        target="app.services.avito_accounts:rewebhook_action",
    ),
    "user.password_reset_link": ActionSpec(
        code="user.password_reset_link",
        label="Выслать новую ссылку",
        permission="users:manage",
        target="app.services.users:issue_password_reset_action",
    ),
}

# Цель реестра, которую в своей зоне ещё не написали: причина и владелец. Тот же
# приём, что PENDING_ACTIONS в services/audit.py, и по той же причине — список
# обязан быть осознанным, а не «оно само как-то не работает».
#
# Пока цель здесь, кнопка честно отвечает 503 «действие пока недоступно»
# (см. run_action), а не 500. Тест-страж двусторонний: он валит CI и когда
# написанная цель сломалась, и когда запись протухла — то есть функция
# появилась, а строку отсюда убрать забыли.
ACTIONS_PENDING_ZONE: dict[str, str] = {
    # Пусто: обе кнопки каталога написаны —
    # `app.services.avito_accounts:reconnect_account_action` (спринт 7) и
    # `app.services.users:issue_password_reset_action`.
}


# --- каталог событий (14 §2) ------------------------------------------------


@dataclass(frozen=True)
class KindSpec:
    """Тип события: важность, адресация, склейка повторов, кнопка."""

    severity: str
    audience: str | None  # None → адресное уведомление, recipient обязателен
    title: str  # заголовок по умолчанию, если вызывающий не дал свой
    dedup: str = "kind"  # kind | entity | none
    # Ключ кнопки в ACTIONS. Поле называется action_code, а не action,
    # намеренно: в этом коде `action="сущность.событие"` — зарезервированная
    # запись строки журнала аудита (services/audit.write_audit, реестр
    # AUDIT_ACTIONS 06 §0.3), и страж реестра ищет её по всему app/ обычным
    # текстовым поиском. Кнопка уведомления записью журнала не является, и
    # называться так же не должна — иначе она попадёт в чужой реестр.
    action_code: str | None = None
    entity_type: str | None = None


KINDS: dict[str, KindSpec] = {
    # 14 §2.1 — системные события, получатели: все администраторы
    "account.needs_reauth": KindSpec(
        severity="critical",
        audience="admin",
        title="Аккаунт Авито требует переподключения",
        dedup="entity",
        action_code="account.reconnect",
        entity_type="account",
    ),
    # «Важно», а не «критично», с 12 августа. Тишина в приёме — это ПОДОЗРЕНИЕ:
    # за ним стоит либо поломка, либо просто спокойный день, и отличить одно от
    # другого по одному лишь молчанию нельзя. Критичность у него была, и вот что
    # она дала: с 6 по 11 августа 112 строк и 456 повторов, каждая с красной
    # плашкой поверх экрана, — а два настоящих критичных «Резервное копирование
    # не выполнилось» (6 и 8 августа) в этом потоке не увидел никто. Копий за те
    # ночи нет.
    #
    # Красная плашка — ресурс на три строки (frontend MAX_CRITICAL_BANNERS), а
    # колокольчик грузит десять последних. Событие, которое умеет повторяться
    # сотнями, обязано жить в «важно»: там оно видно, но никого не вытесняет.
    # Критичным про приём остался `webhook.lost` — он не про молчание, а про
    # факт: нашей подписки на события у канала больше нет.
    "inbound.stalled": KindSpec(
        severity="warning",
        audience="admin",
        title="Приём сообщений остановился",
        dedup="entity",
        entity_type="account",
    ),
    "backup.failed": KindSpec(
        severity="critical", audience="admin", title="Резервное копирование не выполнилось"
    ),
    "scheduler.down": KindSpec(
        severity="critical", audience="admin", title="Планировщик не подаёт признаков жизни"
    ),
    # Сообщает внешний наблюдатель со второго сервера (14 §2.1, §5): проверка
    # «отвечает ли сайт снаружи» с самого сервера не переживает его падения.
    "system.unreachable": KindSpec(
        severity="critical", audience="admin", title="Система не отвечает снаружи"
    ),
    # Вида `inbox.cleaned` («Очередь разгружена автоматически») здесь больше
    # нет: разгрузку очереди убрали целиком по требованию владельца от 11
    # августа (№8) — вместе с ночным заданием, о прогонах которого он и
    # сообщал. Его лицо в интерфейсе удалено той же правкой
    # (frontend/src/features/notifications/catalog.ts); за тем, чтобы обе
    # стороны не разошлись, следит tests/unit/test_notification_catalog.py.
    "queue.backlog": KindSpec(
        severity="warning", audience="admin", title="Очередь входящих не разбирается"
    ),
    "delivery.failures": KindSpec(
        severity="warning", audience="admin", title="Сообщения не уходят клиентам"
    ),
    "disk.space": KindSpec(severity="warning", audience="admin", title="На диске мало места"),
    "cert.expiring": KindSpec(severity="info", audience="admin", title="Сертификат скоро истекает"),
    "ai.unavailable": KindSpec(severity="info", audience="admin", title="AI временно недоступен"),
    # ⚠ ВИД БЫЛ ЗАБЫТ В КАТАЛОГЕ (аудит 19.08). Сторож `check_leadbot_unavailable`
    # (scheduler/jobs/watchdog.py) заводит его с 18.08, а каталог о нём не знал:
    # уведомление всё-таки доходило (черновик по умолчанию адресован админам),
    # но приезжало второсортным — без кнопки и с записью `notification.unknown_kind`
    # в журнал на каждую тревогу. На бою не выстрелило только потому, что бот
    # ни к одному каналу не привязан и сторож ни разу не срабатывал.
    "leadbot.down": KindSpec(severity="warning", audience="admin", title="Лид-бот не отвечает"),
    # Аудит 19.08, находки L-002 и L-004: обе беды раньше были видны только тому,
    # кто читает журнал контейнера, — то есть никому.
    "avito.unreachable": KindSpec(severity="warning", audience="admin", title="Авито не отвечает"),
    # Проверка 24.09: молчащий шлюз был виден только строкой журнала на хосте.
    "gateway.down": KindSpec(
        severity="warning", audience="admin", title="Шлюз внешних сервисов не отвечает"
    ),
    "inbound.dropped_disabled": KindSpec(
        severity="critical", audience="admin", title="Клиенты пишут в выключенный канал"
    ),
    # Замер боя 08.09: 127 диалогов, где `bot_vars.counters.bot_msgs_row > 0`, а
    # реплик бота в переписке нет вовсе. Счётчик — предохранитель «не заваливать
    # клиента», и фантомный счёт закрывает боту рот за несказанное; в интерфейсе
    # при этом всё исправно. «Важно», а не «критично»: клиенты не теряются
    # (диалог целиком видят операторы), а красная плашка — ресурс на три строки,
    # и за него уже платили тишиной про резервные копии.
    "bot.phantom_reply": KindSpec(
        severity="warning",
        audience="admin",
        title="Бот считает отправленным то, чего нет",
        entity_type="conversation",
    ),
    # Автообъединение карточек само ушло в тень (12.09): люди разъединили две
    # автоматические склейки за сутки. Решение о возврате — за владельцем.
    "client_merge.autostopped": KindSpec(
        severity="warning",
        audience="admin",
        title="Автообъединение карточек остановлено",
    ),
    # Воронка адресов (18.09): за неделю адрес из переписки стал реже
    # попадать в карточку, или люди слишком часто правят автоадрес руками.
    # «Важно», не «критично»: клиенты не теряются, страдает заполненность.
    "address.funnel_dropped": KindSpec(
        severity="warning",
        audience="admin",
        title="Адреса стали реже попадать в карточку",
    ),
    # Лестница политик правил адреса (пакет 6.0а, §0.3): задача
    # `rule_policy_weekly` опустила правило на ступень или выключила его
    # (тогда в тексте — команда `address-unfill`, чтобы снять уже записанное).
    # «Важно»: карточки перестают заполняться этим правилом, но клиенты не
    # теряются. Склейка — по правилу (сущность `address_rule`, id — имя
    # правила), не по виду: два правила за неделю — два уведомления.
    "address.rule_degraded": KindSpec(
        severity="warning",
        audience="admin",
        title="Правило адреса понижено",
        dedup="entity",
        entity_type="address_rule",
    ),
    # Правило поднято на ступень — или ОБЪЯВЛЕН подъём до `exact` с окном в
    # семь дней на вето строкой `имя=approx` в настройке. Не тревога — известие.
    "address.rule_promoted": KindSpec(
        severity="info",
        audience="admin",
        title="Правило адреса повышено",
        dedup="entity",
        entity_type="address_rule",
    ),
    # 14 §2.2 — обращения сотрудников, получатели: все администраторы
    "support.password_reset": KindSpec(
        severity="warning",
        audience="admin",
        title="Сотрудник не может войти — просит новый пароль",
        dedup="entity",
        action_code="user.password_reset_link",
        entity_type="user",
    ),
    "support.message": KindSpec(
        severity="info",
        audience="admin",
        title="Сообщение администратору",
        dedup="none",  # каждое обращение — своё; склеивать их нельзя
        entity_type="user",
    ),
    "auth.account_locked": KindSpec(
        severity="warning",
        audience="admin",
        title="Учётная запись заблокирована после неудачных попыток входа",
        dedup="entity",
        action_code="user.password_reset_link",
        entity_type="user",
    ),
    # 14 §2.3 — рабочие события: руководитель и ответственный менеджер
    "conversation.negative": KindSpec(
        severity="warning",
        audience="head",
        title="Клиент недоволен",
        dedup="entity",
        entity_type="conversation",
    ),
    # Личное напоминание тому, кто ведёт диалог (требование от 7 августа).
    # Отдельно от `conversation.no_reply`: то — руководителю про чужой диалог,
    # это — оператору про свой. Самая частая причина молчания — человек
    # отвлёкся, и одного напоминания достаточно; поднимать из-за этого
    # руководителя значит приучить его не смотреть на уведомления.
    "conversation.awaiting_you": KindSpec(
        severity="warning",
        audience=None,  # адресное: получатель — ответственный
        title="Клиент ждёт вашего ответа",
        dedup="entity",
        entity_type="conversation",
    ),
    # ЗДЕСЬ БЫЛ ВИД `conversation.snooze_due` («Отложенный диалог вернулся»,
    # docs/38 §7). Снят 12 августа вместе со всей отложкой — решением владельца.
    # Слать его больше некому: сторож возврата удалён, а статуса `snoozed` нет.
    #
    # УЖЕ СОЗДАННЫЕ ЗАПИСИ ЭТОГО ВИДА В БАЗЕ ОСТАЮТСЯ, и читаются они как
    # прежде: заголовок лежит в самой строке (`notifications.title`), а не
    # берётся из этого реестра на чтении, — см. `frame()` и роут центра.
    # Пропажа вида отсюда влияет ровно на порождение, а порождать больше
    # некому. Retention (14 §4) уберёт старые строки сам; удалять их правкой
    # рук — необратимо и незачем.
    # Ответ конкретного человека не дошёл до клиента (#26). Адресное: получатель
    # — АВТОР сообщения, а не ответственный за диалог и не администратор.
    #
    # Почему именно автор. Он один знает, что хотел сказать, и он один может
    # решить — повторить, переписать или позвонить. Администратору такое
    # уведомление бесполезно (он не знает содержания), а сводка «сообщения не
    # уходят клиентам» у него уже есть отдельным видом (`delivery.failures`) и
    # приходит, когда сбой массовый.
    #
    # Склейка по сущности: десять неудачных ответов в одном диалоге — это одна
    # новость «здесь не уходит», а не десять строк в колокольчике.
    "message.undelivered": KindSpec(
        severity="warning",
        audience=None,  # адресное: получатель — автор сообщения
        title="Ваш ответ не дошёл до клиента",
        dedup="entity",
        entity_type="conversation",
    ),
    # Сообщения от клиентов, которые мы не смогли разобрать (этап 1).
    #
    # Не разобрался вебхук — сырец сохранялся, в журнал уходило
    # предупреждение, и на этом всё: никто не узнавал. На имитаторе, который
    # шлёт один текст, такого не случалось никогда; на боевом Авито с
    # фотографиями и голосовыми — случится. Склейка по виду, а не по сущности:
    # если Авито поменял формат, не разберётся не одно сообщение, а поток, и
    # строка должна быть одна с числом.
    "inbound.unparsed": KindSpec(
        severity="warning",
        audience="admin",
        title="Сообщения от клиентов не разбираются",
        dedup="kind",
        entity_type=None,
    ),
    # Канал отобрали: наша подписка на события Авито пропала (этап 1).
    #
    # Критично, потому что обращения из канала не приходят ВОВСЕ, а снаружи это
    # выглядит просто как затишье. Авито держит на аккаунт одну подписку —
    # значит на неё подписался кто-то другой.
    "webhook.lost": KindSpec(
        severity="critical",
        audience="admin",
        title="Канал отобрали: подписка на события пропала",
        dedup="kind",
        action_code="account.rewebhook",
        entity_type=None,
    ),
    "conversation.no_reply": KindSpec(
        severity="warning",
        audience="head",
        title="Диалог больше 30 минут без ответа",
        dedup="entity",
        entity_type="conversation",
    ),
    "conversation.reopened": KindSpec(
        severity="info",
        audience=None,  # ответственному менеджеру
        title="Клиент вернулся в закрытый диалог",
        dedup="entity",
        entity_type="conversation",
    ),
    # Диалог закрыл КТО-ТО ДРУГОЙ (просьба владельца 03.09: «чтобы другой
    # человек мог спокойно у меня его закрыть и чтобы это никак не помешало
    # другому человеку»).
    #
    # ⚠ БЕЗ ЭТОЙ СТРОКИ ЧУЖОЕ ЗАКРЫТИЕ ЧИТАЕТСЯ КАК ПОЛОМКА. Кадры о закрытии
    # веерные и одинаковые для всех: отличить «закрыл я» от «закрыл коллега» по
    # ним нельзя. Хозяин видит, что поле заперлось, диалог ушёл из «Моих», а
    # признак «в работе у вас» погас, — и объяснения этому на экране нет.
    #
    # ⚠ ТОЛЬКО ЧУЖОЕ И ТОЛЬКО РУКАМИ. Своё закрытие молчит: иначе сообщение
    # получили бы все 14 639 обычных закрытий за месяц против 16 чужих, и вот
    # ЭТО была бы настоящая помеха.
    #
    # `info` — значит без звука и без красной плашки: звонить на всю комнату из
    # тринадцати человек ради чужого закрытия дороже пользы.
    "conversation.closed_by_other": KindSpec(
        severity="info",
        audience=None,  # прежнему ответственному
        title="Ваш диалог закрыл коллега",
        dedup="entity",
        entity_type="conversation",
    ),
    "conversation.assigned": KindSpec(
        severity="info",
        audience=None,  # получателю передачи (01 §11.3 уже шлёт своё событие)
        title="Вам передали диалог",
        dedup="entity",
        entity_type="conversation",
    ),
    # Коллега позвал посмотреть диалог, не отдавая его. Отдельный вид, а не
    # «Вам передали»: у передачи своя карточка с «Принять», а приглашение
    # склеивалось с непрочитанной вестью о передаче и не доходило вовсе.
    # Каждое приглашение — отдельная строка.
    "conversation.invited": KindSpec(
        severity="info",
        audience=None,
        title="Вас позвали в диалог",
        dedup="none",
        entity_type="conversation",
    ),
    # Очередь «Входящие» (план 7.1): от диалога отказались ВСЕ, кому он был
    # доступен. Адресат — администраторы: разбирать такой затор нужно руками
    # (передать диалог, 01 §5.5), а не ждать, что кто-то передумает. Склейка по
    # сущности: десять брошенных диалогов дадут десять строк, а повторная
    # эскалация того же диалога — одну.
    "conversation.unclaimed": KindSpec(
        severity="warning",
        audience="admin",
        title="Диалог никто не принял",
        dedup="entity",
        entity_type="conversation",
    ),
    # Предложение передачи провисело пятнадцать минут и снято (SCEN-11).
    # ДО 11 АВГУСТА ОБ ЭТОМ НЕ УЗНАВАЛ НИКТО: планировщик снимал предложение и
    # публиковал `transfer: null`, полоса «Ждёт подтверждения» молча исчезала.
    # Передавший при этом считает, что диалог отдал, и больше на него не
    # смотрит — а диалог всё это время его, и клиент ждёт.
    #
    # Адресат персональный (передавший), поэтому audience не задан: рассылка
    # администраторам здесь была бы шумом о чужой работе.
    # ⚠ ЯВНЫЙ ОТКАЗ БЫЛ НЕМЫМ (аудит 19.08). Сервис отказа честно возвращает,
    # «кому надо сообщить: он ждёт ответа и должен узнать, что диалог
    # по-прежнему его, — иначе решит, что передал, и клиент останется без
    # ответа». Сообщать было нечем: ручка публиковала кадр с пометкой
    # `for_user_id`, а хаб персонализирует только назначение, и фронт этот
    # признак у обновления диалога не читает. Человек отказался — передавший
    # не узнал ничего. Уведомление переживает и обрыв связи, и закрытую вкладку.
    "conversation.transfer_declined": KindSpec(
        severity="warning",
        audience=None,
        title="От передачи отказались — диалог остался у вас",
        dedup="entity",
        entity_type="conversation",
    ),
    "conversation.transfer_expired": KindSpec(
        severity="warning",
        audience=None,
        title="Передачу не приняли — диалог остался у вас",
        dedup="entity",
        entity_type="conversation",
    ),
    # Передающий забрал предложение назад (просьба владельца 04.09). Адресат —
    # ПОЛУЧАТЕЛЬ, и это единственный вид передачи, направленный в его сторону
    # после самого предложения: «Вам передали диалог» он уже получил, а
    # закрывающей вести не было ни одной. Без неё у человека остаётся открытый
    # долг — диалога нет ни в «Моих», ни в очереди, и проверить, ждут ли ещё от
    # него решения, негде.
    #
    # `info`, а не `warning`: клиент при отмене ничем не рискует — диалог как
    # был, так и остался за прежним сотрудником, и звонить из-за этого на всю
    # комнату из тринадцати человек дороже пользы (тот же довод, что у
    # `conversation.closed_by_other`).
    "conversation.transfer_cancelled": KindSpec(
        severity="info",
        audience=None,
        title="Передачу отменили — принимать нечего",
        dedup="entity",
        entity_type="conversation",
    ),
}

# Критичные события без кнопки — с явной причиной, как PENDING_ACTIONS в
# журнале аудита. Тест реестра требует, чтобы список был осознанным: правило
# «у критичного есть действие одной кнопкой» (14 §3) нарушается только так.
ACTIONLESS_CRITICAL: dict[str, str] = {
    # `inbound.stalled` отсюда убран вместе с его критичностью (см. KINDS):
    # правило «у критичного есть кнопка» к «важному» не относится, и запись,
    # оставленная про запас, врала бы про важность события при первом же чтении.
    "backup.failed": "Бэкап живёт вне приложения (14 §4) — нажать на него из UI нечего",
    # ⚠ КНОПКИ ЗДЕСЬ НЕ БУДЕТ НАМЕРЕННО (аудит 19.08, находка L-004). Канал
    # выключает РУКАМИ человек, и одноклик «включить обратно» из уведомления
    # отменял бы чужое решение, не показав, чьё оно и почему. На странице
    # «Каналы» видно и канал, и его состояние, и кто его трогал по журналу.
    "inbound.dropped_disabled": (
        "Канал выключает человек — включать его обратно надо там же, на «Каналах», "
        "видя, кто и зачем выключил"
    ),
    "scheduler.down": "Планировщик поднимается на сервере; из браузера его не перезапустить",
    "system.unreachable": (
        "Сервер поднимают руками: если система не отвечает снаружи, то и кнопка "
        "в её собственном интерфейсе нажиматься неоткуда"
    ),
    # Важность повышает сторож: истёкший сертификат — это «сайт не открывается»,
    # а не «скоро истечёт» (см. watchdog.ESCALATED_CRITICAL). Кнопки нет и там:
    # сертификат перевыпускает certbot на сервере.
    "cert.expiring": "Сертификат перевыпускается на сервере (certbot), из браузера нажать нечего",
}


def spec_for(kind: str) -> KindSpec | None:
    return KINDS.get(kind)


def action_for(kind: str) -> ActionSpec | None:
    spec = KINDS.get(kind)
    return ACTIONS.get(spec.action_code) if spec and spec.action_code else None


# --- порождение -------------------------------------------------------------


@dataclass
class NotifyResult:
    """Итог :func:`notify`.

    ``created=False`` — повтор схлопнут в существующую запись (14 §4). Это не
    ошибка и не потеря: у записи выросли ``repeat_count`` и ``last_seen_at``.

    ``escalated=True`` — схлопнутый повтор оказался ГРОМЧЕ прежнего (например
    «сертификат истекает» стало «сертификат истёк»). Такой повтор молчать не
    имеет права, см. :func:`deliver`.

    ``revived=True`` — повтор пришёл в запись, которую уже подтвердили, и снял
    отметки прочтения (см. :func:`_reopen`). Для человека это возвращение
    строки в непрочитанные, то есть событие ровно того же веса, что новая
    запись: молчать о нём нельзя, иначе красная плашка не вернётся до
    перезагрузки страницы.
    """

    notification: Notification
    created: bool
    escalated: bool = False
    revived: bool = False
    ws_audience: str | None = None
    ws_users: tuple[str, ...] = field(default_factory=tuple)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт наивные метки — сравнивать их с aware нельзя (07 §1.1)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _dedup_key(
    spec: KindSpec | None, kind: str, entity_type: str | None, entity_id: str | None
) -> str | None:
    """Ключ склейки по умолчанию — из политики каталога."""
    policy = spec.dedup if spec else "kind"
    if policy == "none":
        return None
    if policy == "entity":
        # Без сущности «по сущности» вырождается в «по типу» — это лучше, чем
        # молча выключить подавление: ночной шторм одинаковых строк дороже.
        return f"{kind}:{entity_type or '-'}:{entity_id or '-'}"
    return kind


def dedup_key_for(*, kind: str, entity_type: str | None, entity_id: str | None) -> str | None:
    """Ключ склейки по каталогу — для тех, кому нужно НАЙТИ строку, не создавая её.

    Собирается ровно тем же расчётом, что и при записи: считай его вызывающий
    сам, лестница повторов однажды искала бы не ту строку, что обновляет
    ``notify``, — и начала бы либо молчать, либо звонить каждый такт.
    """
    return _dedup_key(spec_for(kind), kind, entity_type, entity_id)


async def find_live(
    db: AsyncSession,
    *,
    kind: str,
    key: str,
    severity: str,
    recipient_id: uuid.UUID | None,
    audience: str | None,
    now: datetime,
) -> Notification | None:
    """Живая строка с этим ключом склейки — публичный вход в тот же поиск.

    Нужен тем, кто решает, ПОРА ли повторять (лестница сторожа): вопрос
    «сколько раз мы уже сказали это» задают до записи, а не после.
    """
    return await _find_open_duplicate(
        db,
        kind=kind,
        key=key,
        severity=severity,
        recipient_id=recipient_id,
        audience=audience,
        now=now,
    )


async def _reopen(db: AsyncSession, row: Notification) -> bool:
    """Снять отметки прочтения у записи. True — она БЫЛА подтверждена.

    ПОЧЕМУ ПОВТОР СНИМАЕТ ПРОЧТЕНИЕ, А НЕ ЗАВОДИТ ВТОРУЮ СТРОКУ.
    Здесь стояло обратное: схлопывалось только непрочитанное, а повтор после
    подтверждения считался новостью и создавал новую запись. Рассуждение
    выглядело здравым («админ увидел поломку, значит следующий раз — это уже
    "не починилось"»), и рядом даже стоял комментарий, обещавший «не чаще
    раза в час у критичных». Обещания код не выполнял: окно важности стояло
    только в УСЛОВИИ ПОИСКА (``last_seen_at >= now - window``) и созданию
    новых строк не мешало ничем.

    На проде это дало ровно ту жалобу, ради которой всё и переписано: сторож
    «Приём сообщений остановился» бежит раз в пять минут, критичное поднимает
    красную плашку, которую человек ОБЯЗАН подтвердить, — подтвердил, через
    пять минут вторая строка, подтвердил, третья. Счётчик «повторялось N раз»
    при этом не работал никогда, потому что до второго повтора в одной строке
    дело не доходило.

    Теперь на ключ склейки внутри окна живёт РОВНО ОДНА строка, а повтор после
    подтверждения снимает отметки прочтения у неё же: ``repeat_count`` растёт,
    запись снова непрочитанная, плашка возвращается. Новость «не починилось»
    доносится тем же способом, но без размножения строк.

    ПОДТВЕРЖДЕНИЕ РАССЫЛКИ ПЕРСОНАЛЬНО. Прежняя версия считала рассылку
    подтверждённой, как только её прочитал ХОТЬ КТО-ТО из администраторов, —
    то есть один человек отвечал за всех, и остальные получали вторую строку
    вдобавок к своей первой, ещё непрочитанной. Правда про прочтение живёт в
    ``notification_reads`` по паре «уведомление × человек» (см. модель), и
    здесь мы снимаем отметки каждого по отдельности — у кого её не было, у
    того ничего и не меняется.
    """
    res = await db.execute(
        delete(NotificationRead).where(NotificationRead.notification_id == row.id)
    )
    # rowcount живёт на CursorResult; типизированный Result его не обещает.
    reopened = bool(getattr(res, "rowcount", 0) or 0)
    if row.read_at is not None:  # адресное: дубль отметки прямо в строке
        row.read_at = None
        reopened = True
    return reopened


async def _ws_targets(
    db: AsyncSession, *, recipient_id: uuid.UUID | None, audience: str | None
) -> tuple[str | None, tuple[str, ...]]:
    """Кому уедет кадр `notify`: (meta.audience, список user_id).

    Хаб (08 §5.3) умеет фильтровать по ролям ровно один случай —
    ``meta.audience == "admin"``. Кадр с любым другим audience он раздаёт
    ВСЕМ, включая наблюдателя, поэтому рассылку руководителю разбираем на
    адресные кадры (``only_user``) по актуальным ролям из базы. Когда хаб
    научится общей фильтрации по audience (см. cross-boundary), эта ветка
    схлопнется в один publish.
    """
    if recipient_id is not None:
        return None, (str(recipient_id),)
    if audience == "admin":
        return "admin", ()
    roles = [r for r in ROLE_PERMISSIONS if audience in visible_audiences(r)]
    if not roles:
        return None, ()
    ids = (
        (await db.execute(select(User.id).where(User.role.in_(roles), User.is_active.is_(True))))
        .scalars()
        .all()
    )
    return None, tuple(str(i) for i in ids)


async def notify(
    db: AsyncSession,
    *,
    kind: str,
    title: str | None = None,
    body: str | None = None,
    severity: str | None = None,
    recipient_id: uuid.UUID | None = None,
    audience: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    dedup_key: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: datetime | None = None,
) -> NotifyResult:
    """Единый помощник порождения уведомления (14 §4 «Точки порождения»).

    Транзакцией владеет вызывающий: строка кладётся в сессию, ``commit`` делает
    бизнес-код. Публикация в браузер — :func:`deliver` ПОСЛЕ коммита.

    Адресация: ``recipient_id`` **либо** ``audience``; если не задано ни то ни
    другое, берётся ``audience`` из каталога. Отсутствие адреса — ошибка
    программиста (уведомление, которое никто не увидит), поэтому ValueError, а
    не тихая запись в никуда.

    Ограничение, про которое честнее сказать вслух: два процесса, породившие
    одно и то же событие в одну миллисекунду, могут создать две строки —
    ``SELECT ... FOR UPDATE`` сериализует обновление существующей записи, но не
    вставку. На практике источники одиночные (job'ы планировщика идут с
    ``max_instances=1``), а цена промаха — лишняя строка, не потерянное событие.
    """
    now = now or datetime.now(UTC)
    spec = spec_for(kind)
    if spec is None:
        # Опечатка в kind не должна ронять бизнес-операцию: строку пишем, но
        # тест реестра валит CI, а событие видно в логах (тот же приём, что в
        # services/audit.write_audit).
        log.warning("notification.unknown_kind", kind=kind)

    severity = severity or (spec.severity if spec else "info")
    if severity not in SEVERITIES:
        raise ValueError(f"неизвестная важность уведомления: {severity!r}")
    title = title or (spec.title if spec else kind)
    entity_type = entity_type or (spec.entity_type if spec else None)
    if recipient_id is None and audience is None:
        audience = spec.audience if spec else None
    if (recipient_id is None) == (audience is None):
        raise ValueError(
            f"уведомление {kind!r}: нужен ровно один адрес — recipient_id ЛИБО audience"
        )
    if audience is not None and audience not in AUDIENCES:
        raise ValueError(f"неизвестная роль-получатель: {audience!r}")

    key = dedup_key if dedup_key is not None else _dedup_key(spec, kind, entity_type, entity_id)
    expires_at = now + timedelta(days=ttl_days)

    if key is not None:
        existing = await _find_open_duplicate(
            db,
            kind=kind,
            key=key,
            severity=severity,
            recipient_id=recipient_id,
            audience=audience,
            now=now,
        )
        if existing is not None:
            existing.repeat_count += 1
            existing.last_seen_at = now
            existing.expires_at = expires_at  # срок считается от последнего повтора
            # Повтор в подтверждённую строку возвращает её в непрочитанные —
            # копии не заводим (см. _reopen: из-за копий заказчик и получал
            # «три подряд»).
            revived = await _reopen(db, existing)
            # Важность склеенной строки только растёт, и текст едет вместе с
            # ней. Иначе повтор с повышенной важностью тихо ложится в старую
            # строку: критичный текст остаётся покрашенным как обычный, красной
            # плашки (14 §3) не будет, а бейдж колокольчика не покраснеет —
            # ровно то «узнать о поломке последним», ради чего центр и делался.
            # Понижения нет намеренно: «стало не так страшно» — не повод гасить
            # уже поднятую тревогу и подменять её заголовок успокаивающим.
            escalated = SEVERITY_RANK[severity] > SEVERITY_RANK[existing.severity]
            if SEVERITY_RANK[severity] >= SEVERITY_RANK[existing.severity]:
                existing.severity = severity
                existing.title = title  # свежий текст полезнее первого
                existing.body = body
            await db.flush()
            log.info(
                "notification.suppressed",
                kind=kind,
                dedup_key=key,
                repeat_count=existing.repeat_count,
                escalated=escalated,
                revived=revived,
            )
            ws_audience, ws_users = await _ws_targets(
                db, recipient_id=recipient_id, audience=audience
            )
            return NotifyResult(
                existing,
                created=False,
                escalated=escalated,
                revived=revived,
                ws_audience=ws_audience,
                ws_users=ws_users,
            )

    row = Notification(
        recipient_id=recipient_id,
        audience=audience,
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        dedup_key=key,
        repeat_count=1,
        last_seen_at=now,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    ws_audience, ws_users = await _ws_targets(db, recipient_id=recipient_id, audience=audience)
    return NotifyResult(row, created=True, ws_audience=ws_audience, ws_users=ws_users)


async def _find_open_duplicate(
    db: AsyncSession,
    *,
    kind: str,
    key: str,
    severity: str,
    recipient_id: uuid.UUID | None,
    audience: str | None,
    now: datetime,
) -> Notification | None:
    """Живая запись с тем же ключом внутри окна важности — прочитанная тоже.

    Прочитанность здесь НЕ проверяется намеренно: строка на ключ склейки в
    пределах окна должна быть ровно одна, а что делать с подтверждённой,
    решает :func:`_reopen`. Проверка стояла, и из-за неё каждое подтверждение
    порождало следующую копию.
    """
    window = DEDUP_WINDOWS[severity]
    conds: list[Any] = [
        Notification.dedup_key == key,
        Notification.kind == kind,
        Notification.last_seen_at >= now - window,
    ]
    conds.append(
        Notification.recipient_id == recipient_id
        if recipient_id is not None
        else Notification.recipient_id.is_(None)
    )
    conds.append(
        Notification.audience == audience
        if audience is not None
        else Notification.audience.is_(None)
    )
    row = (
        (
            await db.execute(
                select(Notification)
                .where(*conds)
                .order_by(Notification.last_seen_at.desc())
                .limit(1)
                .with_for_update()  # SQLite игнорирует, PostgreSQL сериализует повторы
            )
        )
        .scalars()
        .first()
    )
    return row


def frame(row: Notification, *, repeat: bool = False) -> dict[str, Any]:
    """Payload события `notify` (01 §11.3) — то, из чего фронт рисует тост."""
    action = action_for(row.kind)
    return {
        "id": str(row.id),
        "level": WS_LEVELS[row.severity],
        "title": row.title,
        "text": row.body or "",
        "kind": row.kind,
        "severity": row.severity,
        "entity": ({"type": row.entity_type, "id": row.entity_id} if row.entity_type else None),
        "action": {"code": action.code, "label": action.label} if action else None,
        "repeat_count": row.repeat_count,
        "is_repeat": repeat,
        "audience_hint": row.audience,
        "created_at": iso(as_utc(row.created_at)),
    }


async def deliver(redis: Redis, result: NotifyResult, *, force: bool = False) -> None:
    """Публикация в Pub/Sub — строго ПОСЛЕ commit (08 §8.1).

    Схлопнутый повтор кадр не шлёт: тост на каждую из двенадцати ночных попыток
    — ровно та мусорка, ради которой подавление и сделано (14 §4). Счётчик
    колокольчика при этом не врёт — записи не прибавилось.

    Исключение — повтор с выросшей важностью (``escalated``): «стало хуже» это
    новость, а не двенадцатый одинаковый тост. Без этого исключения критичное
    событие, схлопнутое в обычную строку, не доехало бы до браузера вообще.

    Второе исключение — ``revived``: повтор пришёл в уже подтверждённую строку
    и вернул её в непрочитанные. Кадр обязателен, потому что интерфейс о
    смене состояния иначе не узнает: строка в списке уже есть, счётчик
    колокольчика он пересчитывает по кадру, а красную плашку критичного
    рисует по непрочитанным. Молчание здесь означало бы «подтвердил — и
    поломка исчезла с экрана», хотя она продолжается. Поток это не создаёт:
    оживление бывает не чаще, чем человек нажимает «прочитано».
    """
    if not (result.created or result.escalated or result.revived or force):
        return
    data = frame(result.notification, repeat=not result.created)
    if result.ws_audience is not None:
        await publish_event(redis, "notify", data, audience=result.ws_audience)
        return
    for user_id in result.ws_users:
        await publish_event(redis, "notify", data, only_user=user_id)


async def notify_now(db: AsyncSession, redis: Redis, **kwargs: Any) -> NotifyResult:
    """``notify`` + commit + доставка — для тех, у кого уведомление и есть вся
    работа (планировщик, воркер доставки, эндпоинт скрипта бэкапа, 14 §4)."""
    result = await notify(db, **kwargs)
    await db.commit()
    await deliver(redis, result)
    return result


# --- выборки ----------------------------------------------------------------


def _read_by_me(user_id: uuid.UUID) -> ColumnElement[bool]:
    return (
        select(NotificationRead.notification_id)
        .where(
            NotificationRead.notification_id == Notification.id,
            NotificationRead.user_id == user_id,
        )
        .exists()
    )


def visibility_condition(user: User) -> ColumnElement[bool]:
    """Что этот человек имеет право видеть (14 §4, DESIGN §5.1).

    Адресное — своё; рассылка — та, чью роль он «покрывает» по матрице прав.
    Наблюдатель не покрывает ни одной и адресатом не бывает, поэтому у него
    условие вырождается в пустую выдачу даже мимо RBAC ручки.
    """
    conds: list[ColumnElement[bool]] = [Notification.recipient_id == user.id]
    conds += [Notification.audience == a for a in visible_audiences(user.role)]
    return or_(*conds)


def _alive(now: datetime) -> ColumnElement[bool]:
    """Просроченное не показываем, даже если чистка ещё не отработала."""
    return Notification.expires_at > now


async def list_for_user(
    db: AsyncSession,
    user: User,
    *,
    severity: str | None = None,
    kind: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> tuple[list[Notification], int, set[uuid.UUID]]:
    """Страница журнала + total + множество id, прочитанных этим человеком."""
    now = now or datetime.now(UTC)
    conds: list[Any] = [visibility_condition(user), _alive(now)]
    if severity:
        conds.append(Notification.severity == severity)
    if kind:
        conds.append(Notification.kind == kind)
    ts_from, ts_to = msk_day_range(date_from, date_to)  # период — по Москве (06 §0.1)
    # В период входит то, что в нём было живо: началось до его конца и
    # повторялось после его начала. Строка показана и отсортирована по
    # последнему повтору, а отбор по одному `created_at` прятал из «Сегодня»
    # тревогу, которая сегодня повторялась (проверка 24.09: 1 340 таких строк).
    if ts_from is not None:
        conds.append(Notification.last_seen_at >= ts_from)
    if ts_to is not None:
        conds.append(Notification.created_at < ts_to)
    if unread_only:
        conds.append(~_read_by_me(user.id))

    total = (
        await db.execute(select(func.count()).select_from(Notification).where(*conds))
    ).scalar_one()
    rows = list(
        (
            await db.execute(
                select(Notification)
                .where(*conds)
                # last_seen_at, а не created_at: повторившаяся поломка обязана
                # всплыть наверх. id в хвосте — стабильная пагинация (SQLite
                # штампует одинаковые метки внутри секунды).
                .order_by(Notification.last_seen_at.desc(), Notification.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return rows, total, await read_ids(db, user, [r.id for r in rows])


async def read_ids(db: AsyncSession, user: User, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await db.execute(
        select(NotificationRead.notification_id).where(
            NotificationRead.user_id == user.id,
            NotificationRead.notification_id.in_(list(ids)),
        )
    )
    return set(rows.scalars().all())


async def unread_counts(
    db: AsyncSession, user: User, *, now: datetime | None = None
) -> dict[str, int]:
    """Счётчик колокольчика: всего + разбивка по важности (14 §3)."""
    now = now or datetime.now(UTC)
    rows = await db.execute(
        select(Notification.severity, func.count())
        .where(visibility_condition(user), _alive(now), ~_read_by_me(user.id))
        .group_by(Notification.severity)
    )
    by_severity: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    for severity, count in rows.all():
        by_severity[severity] = count
    return {"unread": sum(by_severity.values()), **by_severity}


async def get_visible(
    db: AsyncSession, user: User, notification_id: uuid.UUID, *, now: datetime | None = None
) -> Notification:
    """Уведомление, если оно адресовано этому человеку. Иначе 404.

    Именно 404, а не 403: «такого уведомления у вас нет» не должно
    подтверждать, что оно существует у кого-то другого.
    """
    now = now or datetime.now(UTC)
    row = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.id == notification_id,
                    visibility_condition(user),
                    _alive(now),
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise ApiError("not_found", "Уведомление не найдено", status=404)
    return row


# --- прочтение --------------------------------------------------------------


async def mark_read(
    db: AsyncSession, user: User, row: Notification, *, now: datetime | None = None
) -> bool:
    """Отметить прочитанным ЭТИМ человеком. True — состояние изменилось.

    Отметка пишется всегда, включая адресные уведомления: единый предикат
    «непрочитано» = нет строки в ``notification_reads`` (см. модель).
    ``read_at`` у адресного дополнительно проставляется в самой строке — это
    дубль той же правды для выборок, которые ходят мимо ``notification_reads``
    (частичный индекс колокольчика). Повтор события снимает обе отметки
    сразу, см. :func:`_reopen`.
    """
    now = now or datetime.now(UTC)
    already = await db.execute(
        select(NotificationRead.user_id).where(
            NotificationRead.notification_id == row.id, NotificationRead.user_id == user.id
        )
    )
    if already.first() is not None:
        return False
    db.add(NotificationRead(notification_id=row.id, user_id=user.id, read_at=now))
    if row.recipient_id == user.id and row.read_at is None:
        row.read_at = now
    await db.flush()
    return True


#: Виды, которые гаснут сами, когда повод исчез: все говорят «диалогом никто
#: не занят» или «клиент вернулся». Стоит диалог принять, ответить, передать
#: или закрыть — висящая строка начинает врать. «Клиент вернулся» не гас
#: никогда и копился у менеджеров сотнями, пряча вести, требующие действия.
RESOLVED_BY_CONVERSATION = (
    "conversation.unclaimed",
    "conversation.awaiting_you",
    "conversation.no_reply",
    "conversation.reopened",
)


async def resolve_for_entity(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    kinds: Sequence[str],
    now: datetime | None = None,
) -> int:
    """Погасить живые уведомления, повод которых исчез. Возвращает сколько.

    ⚠ ЗАЧЕМ ЭТО ВООБЩЕ ПОНАДОБИЛОСЬ. До 14 августа в коде не было НИ ОДНОГО
    места, где уведомление гаснет по смене состояния сущности: снять его мог
    только человек нажатием или чистка по сроку в 90 дней. Замер боя: «Диалог
    никто не принял» висит 57 минут при закрытом диалоге и пустой очереди, а в
    теле у него — «Откройте „Входящие“ и возьмите его» про диалог, которого в
    очереди давно нет. Человек открывает очередь, не находит там ничего и
    делает единственный доступный вывод: колокольчик врёт.

    Цена такого вранья записана рядом, в `cli.dismiss-stale-alerts`: «пока
    завал не разобран, колокольчик бесполезен — его перестают открывать, и
    следующая настоящая тревога опоздает ровно настолько, насколько человек
    привык не смотреть».

    ГАСИМ ПРОЧТЕНИЕМ, А НЕ СРОКОМ ЖИЗНИ. Поставить `expires_at = now` было бы
    короче на десять строк — и убрало бы строку не только из колокольчика, но и
    из ЖУРНАЛА: выборка журнала стоит на том же `_alive`. А журнал нужен именно
    таким: «диалог висел непринятым сорок минут» — это факт о работе, и он
    обязан пережить приём диалога. Тот же довод записан у команды разбора
    завала: «не удаляет: тревога остаётся в журнале, из колокольчика уходит
    только счётчик».

    ⚠ ПРОЧТЕНИЕ ПИШЕТСЯ ДВУМЯ СПОСОБАМИ, И ОДНОГО МАЛО. У адресного уведомления
    непрочитанность живёт в `read_at` самой строки, у рассылки по роли — в
    отсутствии строки в `notification_reads` у КАЖДОГО, кто её видит. Поставь мы
    только `read_at`, рассылка («Диалог никто не принял» — администраторам)
    осталась бы в колокольчике у всех до единого. Это, к слову, тихая
    недоделка и в самой `dismiss-stale-alerts`: она гасит только `read_at`, и
    для рассылок её работа наполовину незаметна.

    Отметка ставится от имени того, кто её видит, а не от имени действующего:
    «прочитано» — состояние личное, и админ, который сейчас спит, обязан
    получить его так же, как тот, кто диалог принял.
    """
    now = now or datetime.now(UTC)
    if not kinds:
        return 0

    rows = list(
        (
            await db.execute(
                select(Notification).where(
                    Notification.kind.in_(list(kinds)),
                    Notification.entity_type == entity_type,
                    Notification.entity_id == entity_id,
                    _alive(now),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Кого касается рассылка — считаем ОДИН раз на все строки: получателей у
    # роли единицы, а вот запрос на каждое уведомление в цикле — это запрос на
    # каждое закрытие диалога.
    audiences = {r.audience for r in rows if r.audience}
    viewers_by_audience: dict[str, list[uuid.UUID]] = {}
    if audiences:
        people = (
            await db.execute(select(User.id, User.role).where(User.is_active.is_(True)))
        ).all()
        for a in audiences:
            viewers_by_audience[a] = [uid for uid, role in people if a in visible_audiences(role)]

    ids = [r.id for r in rows]
    seen = {
        (nid, uid)
        for nid, uid in (
            await db.execute(
                select(NotificationRead.notification_id, NotificationRead.user_id).where(
                    NotificationRead.notification_id.in_(ids)
                )
            )
        ).all()
    }

    resolved = 0
    for row in rows:
        touched = False
        if row.read_at is None:
            row.read_at = now
            touched = True
        viewers: list[uuid.UUID] = []
        if row.recipient_id is not None:
            viewers.append(row.recipient_id)
        if row.audience:
            viewers += viewers_by_audience.get(row.audience, [])
        for uid in viewers:
            if (row.id, uid) in seen:
                continue
            seen.add((row.id, uid))
            db.add(NotificationRead(notification_id=row.id, user_id=uid, read_at=now))
            touched = True
        if touched:
            resolved += 1

    await db.flush()
    if resolved:
        log.info(
            "notifications.resolved",
            entity_type=entity_type,
            entity_id=entity_id,
            resolved=resolved,
        )
    return resolved


async def resolve_conversation(
    db: AsyncSession, conversation_id: Any, *, now: datetime | None = None
) -> int:
    """Повод исчез у ДИАЛОГА: приняли, передали, закрыли.

    Отдельная обёртка, а не голый вызов на каждой из трёх точек: список видов
    обязан быть один. Разъедься он — часть уведомлений гасла бы при закрытии и
    не гасла при приёме, и объяснить это на экране было бы нечем.
    """
    return await resolve_for_entity(
        db,
        entity_type="conversation",
        entity_id=str(conversation_id),
        kinds=RESOLVED_BY_CONVERSATION,
        now=now,
    )


async def mark_all_read(db: AsyncSession, user: User, *, now: datetime | None = None) -> int:
    """«Отметить все прочитанными» (14 §3) — только видимые непрочитанные.

    ⚠ ЗДЕСЬ БЫЛО ДВА ЗАПРОСА НА КАЖДУЮ СТРОКУ. Цикл звал `mark_read`, а тот на
    каждой строке спрашивал базу «а не прочитано ли уже?» и делал свой `flush`.
    Вопрос был заведомо лишним: выборка выше уже отсекает прочитанное мной
    (`~_read_by_me`), и в одной транзакции ответ на него всегда «нет». У
    руководителя, вернувшегося из отпуска, колокольчик копит сотни строк — одно
    нажатие превращалось в сотни рейсов до базы. Теперь отметки копятся в сессии
    и уходят одним `flush`: SQLAlchemy складывает их в пакетную вставку.

    Гонку это не ухудшает: проверка-перед-вставкой и так не была атомарной, и
    при одновременном нажатии с двух вкладок обе редакции упирались в один и тот
    же уникальный ключ `notification_reads`.
    """
    now = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(Notification).where(
                    visibility_condition(user),
                    _alive(now),
                    ~_read_by_me(user.id),
                    # Критичные гасятся только поимённо — так обещает экран
                    # («останутся, пока не подтвердите каждое»). Разовая беда
                    # вроде «копия не создана» иначе исчезала после F5 навсегда.
                    Notification.severity != "critical",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        db.add(NotificationRead(notification_id=row.id, user_id=user.id, read_at=now))
        # Дубль той же правды в самой строке — для выборок, идущих мимо
        # `notification_reads` (частичный индекс колокольчика), см. `mark_read`.
        if row.recipient_id == user.id and row.read_at is None:
            row.read_at = now
    await db.flush()
    return len(rows)


# --- действия кнопки --------------------------------------------------------


def _resolve(target: str) -> Any:
    """Поздний импорт ``"модуль:функция"``.

    Поздний — намеренно: реестр действий указывает на чужие зоны, часть из
    которых пишется параллельно. Отсутствующий модуль обязан давать «действие
    пока недоступно», а не падать импортом при старте приложения.
    """
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


async def run_action(
    db: AsyncSession,
    redis: Redis,
    *,
    user: User,
    row: Notification,
    action_code: str | None = None,
) -> dict[str, Any]:
    """Выполнить действие уведомления (14 §3: «одно нажатие вместо консоли»)."""
    spec = action_for(row.kind)
    if spec is None or (action_code is not None and action_code != spec.code):
        raise ApiError(
            "action_not_supported",
            "У этого уведомления нет действия",
            status=422,
        )
    if spec.permission not in ROLE_PERMISSIONS.get(user.role, frozenset()):
        raise ApiError("forbidden", status=403)

    try:
        fn = _resolve(spec.target)
    except (ImportError, AttributeError):
        log.warning("notification.action_unavailable", action=spec.code, target=spec.target)
        raise ApiError(
            "action_unavailable",
            "Действие пока недоступно — сделайте это вручную в настройках",
            status=503,
        ) from None

    result = await fn(db, redis, entity_id=row.entity_id, actor=user)
    await mark_read(db, user, row)  # нажали кнопку — значит увидели
    return {"action": spec.code, "result": result if isinstance(result, dict) else None}


# --- чистка -----------------------------------------------------------------


async def cleanup_expired(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Удалить просроченные уведомления (14 §4, по умолчанию 90 дней).

    Задача планировщика; отметки прочтения уезжают каскадом по внешнему ключу.
    Регистрацию job'а см. cross-boundary — app/scheduler/main.py чужая зона.
    """
    now = now or datetime.now(UTC)
    res = await db.execute(delete(Notification).where(Notification.expires_at <= now))
    # rowcount живёт на CursorResult; типизированный Result его не обещает.
    deleted = getattr(res, "rowcount", 0) or 0
    if deleted:
        log.info("cleanup.notifications", deleted=deleted)
    return deleted
