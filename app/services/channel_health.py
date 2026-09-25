"""Что карточка канала говорит про токен и про подписку на события.

ПРАВИЛО, РАДИ КОТОРОГО НАПИСАН ВЕСЬ МОДУЛЬ: жёлтым и красным помечается
ТОЛЬКО то, что требует действия человека. Штатное поведение — суточный токен,
который перевыпускается сам, и тишина на редком канале — показывается
нейтрально. Индикатор, который горит всегда, не значит ничего; хуже того, он
приучает не смотреть и туда, где однажды загорится настоящая поломка. Эту цену
мы уже платили дважды: «Приём сообщений остановился» похоронил две ночи без
резервной копии (``app/scheduler/jobs/watchdog.py``), а «Webhook: сбой» на
служебной заглушке читался как рабочий канал со сломанной подпиской.

ЧТО БЫЛО ЗДЕСЬ ДО 12 АВГУСТА (обе строки считал фронт, каждая по одному числу):

* «⚠️ Токен истекает через 23 ч» — жёлтая строка со знаком тревоги при
  остатке 23 часа из 24, то есть на всех каналах и почти всё время. Люди
  читали её как аварию и жали «Обновить токен» руками каждый день, хотя
  суточный токен по постоянным ``client_id``/``client_secret`` перевыпускается
  планировщиком сам (``avito_accounts.refresh_due_accounts``, каждые 30 минут,
  за два часа до срока);
* «Webhook: ⚠️ событий нет 16 ч» — тревога по АБСОЛЮТНОМУ времени тишины. На
  канале «Дамир» (пять обращений за неделю) она горела вечно, а соседний
  «Тимофей» с дневным трафиком показывал «✓ в порядке». Разница между ними не
  в исправности, а в потоке клиентов.

ТРИ РЕШЕНИЯ, ИЗ КОТОРЫХ СОСТОИТ МОДУЛЬ.

1. **Срок токена превращён в три состояния** (:func:`token_health`), и граница
   тревоги привязана не к «мало времени осталось», а к «автообновление уже
   должно было пройти и не прошло» — см. :data:`TOKEN_WARN_AHEAD`.

2. **Результат попыток обновления запоминается** (:func:`note_refresh_ok`,
   :func:`note_refresh_failed`). Схему базы мы не меняем — в этом спринте её
   правит соседняя группа, и вторая миграция на ту же ревизию означала бы две
   головы alembic. Журнал живёт в Redis: он про НАБЛЮДЕНИЕ за автоматикой, а
   не про сам доступ. Потеря Redis обнуляет наблюдение, но не ломает канал —
   карточка честно скажет «обновлений на нашей памяти не было» и останется
   нейтральной, а не покрасит канал в красное из-за перезапуска.

3. **Тишина сравнивается с ОБЫЧНЫМ РИТМОМ канала** (:func:`channel_rhythm`), и
   прежде чем обвинить подписку — подписка ПРОВЕРЯЕТСЯ у Авито
   (:func:`audit_subscription`). Пропавшая подписка это факт, тишина — только
   подозрение; на канале с пятью обращениями в неделю подозрение не стоит
   ничего.

Отсюда наружу уходят только данные: состояние, числа и готовая строка. Кнопку
рисует экран, но ПРАВО на неё определяется здесь — поле ``action``.
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AvitoAccount, Conversation, Message
from app.services import app_settings, crypto, stats
from app.services import avito_accounts as accounts_service
from app.services.audit import MSK  # все бизнес-определения времени — по Москве

log = structlog.get_logger("app.channel_health")

# =============================================================================
# Состояния и действия — словарь, общий с экраном
# =============================================================================

#: Нейтрально: ничего делать не надо. Ни цвета, ни знака тревоги.
OK = "ok"
#: Нейтрально, но с объяснением: событий нет, и для ЭТОГО канала это норма.
QUIET = "quiet"
#: Жёлтое: нужен взгляд человека, но канал ещё работает.
WARNING = "warning"
#: Красное: канал уже не делает свою работу.
CRITICAL = "critical"

#: Значения поля ``action``. БЕЗ ТОЧКИ В ИМЕНИ, и это не вкусовщина: страж
#: реестра журнала аудита (``tests/unit/test_audit.py``) ищет по всему ``app/``
#: литералы вида ``action="сущность.событие"`` и требует их описания в
#: ``AUDIT_ACTIONS``. Кнопка карточки записью журнала не является, поэтому
#: называется так, чтобы в чужой реестр не попадать.
ACTION_REFRESH_TOKEN = "refresh_token"  # POST /avito-accounts/{id}/refresh-token
ACTION_REWEBHOOK = "rewebhook"  # POST /avito-accounts/{id}/register-webhook

#: ⚠ ФРАЗА НЕ ЗОВЁТ НАЖАТЬ КНОПКУ, КОТОРАЯ И ТАК СТОИТ РЯДОМ (разбор интерфейса 13.08).
#:
#: Семь сообщений здоровья кончались словами «Нажмите «Обновить токен»» либо «Нажмите
#: «Обновить подписку»» — а карточка рисует кнопку ровно с этой подписью вплотную
#: справа (ChannelHealth.tsx). Двадцать четыре знака дубля в абзаце, который и без
#: того выходит на пять строк в колонке шириной 280.
#:
#: Причина дубля понятна: поле ``action`` появилось позже самих фраз, и текст писался
#: тогда, когда кнопки не было. Теперь кнопка есть, и говорит она о себе сама.
#:
#: ⚠ ЧТО ЭТО ЛОМАЕТ, ЕСЛИ НЕ ЗНАТЬ. Руководителю кнопка не рисуется (11 §4.1), и
#: вместо неё стояла строка «Это делает администратор.» — она держалась на хвосте:
#: «это» относилось к «нажмите». Сняв хвост, местоимению не к чему отнестись, поэтому
#: строка на фронте переписана в самодостаточную и собирается из подписи того же
#: действия. Снять здесь и не поправить там — значит оставить руководителя без
#: единственного сигнала, что вообще нужно чьё-то вмешательство.


# =============================================================================
# Пороги
# =============================================================================

#: Шаг планировщика, который обновляет токены (``token_refresh`` в
#: ``app/scheduler/main.py``, IntervalTrigger(minutes=30)).
TOKEN_REFRESH_PASS = timedelta(minutes=30)

#: ЗА СКОЛЬКО ДО ИСТЕЧЕНИЯ СТРОКА СТАНОВИТСЯ ЖЁЛТОЙ.
#:
#: Не «мало времени осталось», а «автообновление уже должно было пройти и не
#: прошло». Плановое обновление начинается за два часа до срока
#: (``avito_accounts.REFRESH_AHEAD``) и повторяется каждые тридцать минут.
#: Значит остаток между 2 ч и 1,5 ч — это НОРМАЛЬНОЕ ожидание ближайшего
#: обхода, и жёлтый в нём означал бы «планировщик ещё не добежал», то есть
#: полчаса ложной тревоги на каждом канале каждые сутки. А вот остаток меньше
#: полутора часов означает, что у автоматики была как минимум одна полная
#: попытка и она ею не воспользовалась.
TOKEN_WARN_AHEAD = accounts_service.REFRESH_AHEAD - TOKEN_REFRESH_PASS

#: Сколько неудач подряд делают из «не получилось» аварию. Три подряд — это
#: полтора часа безуспешных попыток: одна неудача бывает от сетевой икоты,
#: три означают, что само не починится.
TOKEN_FAILURES_CRITICAL = 3

#: Окно измерения ритма канала. Тридцать дней, а не неделя: на редком канале
#: за неделю набирается пять пауз, и медиана по пяти — не медиана, а случайное
#: число. Тридцать дней дают два десятка пауз даже у «Дамира».
RHYTHM_WINDOW_DAYS = 30

#: Сколько пауз нужно, чтобы вообще говорить о «ритме канала». Меньше —
#: молчим и НИКОГДА не поднимаем тревогу по тишине: только что подключённый
#: канал не с чем сравнивать, а пугать им нечем.
RHYTHM_MIN_SAMPLES = 5

#: Потолок выборки: больше трёхсот отметок медиану не уточняют, а запрос
#: удорожают. Берутся самые свежие — ритм последнего месяца, а не годовой.
RHYTHM_MAX_EVENTS = 300

#: ВО СКОЛЬКО РАЗ ПАУЗА ДОЛЖНА ПРЕВЫСИТЬ ОБЫЧНУЮ, ЧТОБЫ СТАТЬ НОВОСТЬЮ.
#: Четыре — не круглое число ради круглого: на живом потоке заказчика каждая
#: десятая пауза длиннее трёх медиан, и тройка звонила бы раз в день впустую.
QUIET_FACTOR = 4

#: Пол порога тишины В РАБОЧИХ МИНУТАХ. То же измерение, из которого выведен
#: ``watchdog.MIN_INBOUND_STALL_MINUTES``: у заказчика медиана паузы между
#: обращениями 4 минуты, но каждая десятая пауза длиннее трёх часов. Без пола
#: «четыре медианы» у бойкого канала дали бы тревогу на шестнадцатой минуте
#: молчания — то есть ровно ту же болезнь, от которой лечимся.
#:
#: Число ЖИВЁТ ЗДЕСЬ, а не берётся из сторожа: тот порог про приём целиком
#: («ни одного обращения ни в один канал»), этот — про один канал. Связывать
#: их одной настройкой значило бы, что правка ради одного экрана меняет
#: критичную тревогу планировщика.
QUIET_FLOOR_MINUTES = 240

#: Как часто разрешено спрашивать Авито про подписку одного канала. Сверка —
#: поход в чужой API за спиной у людей; на молчащем канале она нужна, но раз
#: в полчаса, а не каждые пять минут вслед за прогоном сторожа.
AUDIT_MIN_INTERVAL = timedelta(minutes=30)

# --- ключи Redis --------------------------------------------------------------
#
# Все три — НАБЛЮДЕНИЕ, а не состояние системы: их потеря делает карточку менее
# подробной и никогда — более тревожной. Это и есть условие, при котором можно
# не трогать схему базы.

#: Журнал обновления токена: когда последний раз получилось и сколько неудач
#: подряд. Месяц — чтобы «последнее успешное обновление» пережило отпуск.
REFRESH_JOURNAL_TTL = 30 * 24 * 3600

#: Итог последней сверки подписки. Неделя: строка «сверка 16:54» старше суток
#: уже почти ничего не говорит, но и врать ею нельзя — время показывается.
AUDIT_TTL = 7 * 24 * 3600

#: Ритм канала пересчитывается редко: он про месяц, за шесть часов не меняется,
#: а запрос по ``messages`` — не то, что делают на каждое открытие экрана.
RHYTHM_TTL = 6 * 3600


def _refresh_key(account_id: uuid.UUID) -> str:
    return f"token:refresh:{account_id}"


def _audit_key(account_id: uuid.UUID) -> str:
    return f"webhook:audit:{account_id}"


def _rhythm_key(account_id: uuid.UUID) -> str:
    return f"channel:rhythm:{account_id}"


# =============================================================================
# Общие мелочи
# =============================================================================


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite отдаёт naive — считаем такое UTC, как во всём проекте."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        return None


def _load_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _clock(moment: datetime) -> str:
    """«16:07» по Москве — так время подписано во всём интерфейсе."""
    return moment.astimezone(MSK).strftime("%H:%M")


def _day_clock(moment: datetime) -> str:
    """«13.08 в 16:07»: без года — карточку читают про ближайшие сутки."""
    local = moment.astimezone(MSK)
    return f"{local.strftime('%d.%m')} в {local.strftime('%H:%M')}"


def _times(count: int) -> str:
    """«1 раз», «3 раза», «5 раз».

    Мелочь, которую видно: строку читает владелец бизнеса, и «не проходит 3
    раза подряд» против «3 раз подряд» — разница между текстом, написанным для
    человека, и склеенным из переменных.
    """
    if 11 <= count % 100 <= 14:
        return f"{count} раз"
    if 2 <= count % 10 <= 4:
        return f"{count} раза"
    return f"{count} раз"


def human_minutes(minutes: int) -> str:
    """«45 мин», «1 ч 20 мин», «16 ч», «3 дн» — длительность словами.

    Точность падает с ростом числа НАМЕРЕННО: «19 ч 43 мин» человек всё равно
    читает как «почти сутки», а лишние цифры мешают увидеть главное.
    """
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    days = hours // 24
    return f"{days} дн"


# =============================================================================
# Рабочие часы: одно окно на весь модуль
# =============================================================================


@dataclass(frozen=True)
class HealthContext:
    """Рабочее окно и «сейчас» — читаются ОДИН РАЗ на запрос.

    Окно берётся из настроек (экран «Распределение», ключи
    ``stats.work_start_hour``/``stats.work_end_hour``), потому что «обычный
    ритм канала» и «тишина» обязаны считаться в тех же часах, в которых люди
    работают. Ночь и выходной вечер — не тишина канала, а закрытая смена.

    Читать настройку внутри каждой карточки нельзя: девять каналов дали бы
    девять походов в базу за одним и тем же числом (01 §5.1).
    """

    work_start: timedelta
    work_end: timedelta
    now: datetime
    #: Последнее входящее по каналам — ОДНИМ запросом на всю страницу.
    #:
    #: ⚠ ЗАМЕР 31.08: список каналов отвечал 0,8 с при медиане всех прочих
    #: ручек 26 мс — самая медленная точка продукта. Причина: `_last_event_at`
    #: звался в цикле по карточкам, а его запрос идёт по секционированной
    #: таблице сообщений; одно только ПЛАНИРОВАНИЕ стоило 35 мс, то есть на
    #: тридцати пяти каналах больше секунды впустую. Файл маршрута рядом сам
    #: запрещает такое («не по запросу на карточку — тот самый N+1»), но эта
    #: ветка запрет обходила.
    #:
    #: `None` — карту не готовили (одиночная карточка): тогда работает прежний
    #: путь с запросом на месте.
    последние_входящие: dict[uuid.UUID, datetime | None] | None = None


async def load_context(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    account_ids: Sequence[uuid.UUID] | None = None,
) -> HealthContext:
    """Общий контекст страницы каналов.

    ``account_ids`` — готовим карту последних входящих сразу на всю страницу
    (разбор — у поля :attr:`HealthContext.последние_входящие`). Без него
    поведение прежнее: карточка спросит сама.
    """
    values = await app_settings.get_all(db)
    карта: dict[uuid.UUID, datetime | None] | None = None
    if account_ids:
        карта = await последние_входящие_по_каналам(db, account_ids)
    return HealthContext(
        work_start=timedelta(hours=int(values[app_settings.STATS_WORK_START_HOUR])),
        work_end=timedelta(hours=int(values[app_settings.STATS_WORK_END_HOUR])),
        now=now or _utcnow(),
        последние_входящие=карта,
    )


async def последние_входящие_по_каналам(
    db: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime | None]:
    """Последнее входящее сообщение по каждому каналу — ОДНИМ запросом.

    Боковое соединение вместо тридцати пяти отдельных запросов: работа для базы
    та же, а планирование и обратные ходы — один раз вместо тридцати пяти.
    Именно планирование и стоило больше секунды (35 мс на запрос, замер 31.08).
    """
    ids = list(account_ids)
    if not ids:
        return {}

    # ⚠ ГРУППИРОВКА, А НЕ ПОДЗАПРОС НА КАНАЛ. Первая редакция этой функции
    # брала боковое соединение с `LIMIT 1` на каждый канал — и оказалась ХУЖЕ
    # исходного цикла: замер на боевом дал 2200 мс против 800. Коррелированный
    # подзапрос заставляет планировщик выполнять его тридцать пять раз, и ни
    # один индекс тут не спасает.
    #
    # Обычная группировка делает ту же работу ОДНИМ проходом: замер плана на
    # боевом — 80 мс на все тридцать пять каналов. Разница в двадцать семь раз
    # взялась не из индексов, а из формы запроса.
    # ⚠ ОКНО, А ПОТОМ ХВОСТ ПО ОСТАВШИМСЯ — И ЭТО НЕ ОДНО И ТО ЖЕ, ЧТО ПРОСТО
    # ОКНО. Без границы запрос обходит ВСЕ партиции `messages` (замер на бою
    # 03.09: 121 узел плана, 106,9 мс + 38,7 мс планирования). С границей в 30
    # суток — 47 узлов и 62,7 мс.
    #
    # Но окно меняет СМЫСЛ пустого ответа. Канал, молчащий сорок дней, вернул
    # бы `None`, а `None` здесь читается как «входящих не было НИКОГДА» — и
    # мёртвый канал перестал бы значиться молчащим. Ровно тот случай, ради
    # которого сторож и написан. Поэтому по каналам, которых в окне не
    # оказалось, идём вторым запросом без границы: обычно их ноль, и второго
    # запроса не будет вовсе.
    #
    # `sender_type == "client"` добавлен для совпадения с одиночным путём
    # (`_last_event_at` ниже): там он стоял, здесь его не было. Сегодня выборки
    # совпадают (все 152 449 входящих — клиентские), но два пути, считающие
    # одно поле по-разному, рано или поздно расходятся.
    async def _максимумы(остальные: list[uuid.UUID], *, с_границей: bool) -> dict:
        запрос = (
            select(Conversation.account_id, sa_func.max(Message.created_at))
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.account_id.in_(остальные),
                Message.direction == "in",
                Message.sender_type == "client",
            )
            .group_by(Conversation.account_id)
        )
        if с_границей:
            запрос = запрос.where(
                Message.created_at >= datetime.now(UTC) - timedelta(days=RHYTHM_WINDOW_DAYS)
            )
        return {row[0]: _aware(row[1]) for row in (await db.execute(запрос)).all()}

    найдено = await _максимумы(list(ids), с_границей=True)
    молчуны = [aid for aid in ids if aid not in найдено]
    if молчуны:
        найдено |= await _максимумы(молчуны, с_границей=False)
    # Канал без единого входящего в ответе группировки не появится — а карта
    # обязана отвечать по каждому запрошенному: иначе карточка решит, что карты
    # нет, и пойдёт в базу сама.
    return {aid: найдено.get(aid) for aid in ids}


def _work_minutes(ctx: HealthContext, since: datetime, until: datetime) -> int:
    """Рабочие минуты между двумя моментами.

    Считает ``stats.business_seconds_between`` — тот же порт SQL-функции, по
    которому меряется скорость первого ответа. Своя арифметика рабочих часов
    здесь означала бы, что «пауза 40 минут» на карточке и в отчёте — разные
    сорок минут.
    """
    seconds = stats.business_seconds_between(
        since, until, work_start=ctx.work_start, work_end=ctx.work_end
    )
    return int((seconds or 0) // 60)


# =============================================================================
# 1. Журнал обновления токена (Redis)
# =============================================================================


@dataclass(frozen=True)
class RefreshJournal:
    """Что мы знаем про автоматическое обновление токена этого канала."""

    #: Когда обновление ПОСЛЕДНИЙ РАЗ получилось. ``None`` — на нашей памяти
    #: ни разу (канал только подключили или Redis перезапускали).
    last_ok_at: datetime | None = None
    #: Когда последняя попытка провалилась.
    last_error_at: datetime | None = None
    #: Человеческая причина последней неудачи. Внутренних имён исключений тут
    #: нет: строку читает владелец бизнеса, а не разработчик.
    last_error: str | None = None
    #: Неудач ПОДРЯД. Успех обнуляет.
    failures: int = 0

    @property
    def last_attempt_ok(self) -> bool | None:
        """Чем кончилась последняя попытка; ``None`` — попыток не было."""
        if self.last_ok_at is None and self.last_error_at is None:
            return None
        return self.failures == 0


def _journal_from(raw: Any) -> RefreshJournal:
    data = _load_json(raw)
    if not data:
        return RefreshJournal()
    try:
        failures = int(data.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    error = data.get("error")
    return RefreshJournal(
        last_ok_at=_parse_time(data.get("ok_at")),
        last_error_at=_parse_time(data.get("error_at")),
        last_error=str(error) if error else None,
        failures=max(0, failures),
    )


def _journal_dump(journal: RefreshJournal) -> str:
    return json.dumps(
        {
            "ok_at": journal.last_ok_at.isoformat() if journal.last_ok_at else None,
            "error_at": journal.last_error_at.isoformat() if journal.last_error_at else None,
            "error": journal.last_error,
            "failures": journal.failures,
        },
        ensure_ascii=False,
    )


async def read_journal(redis: Redis, account_id: uuid.UUID) -> RefreshJournal:
    return _journal_from(await redis.get(_refresh_key(account_id)))


async def note_refresh_ok(
    redis: Redis, account_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Обновление получилось: запомнить время и обнулить счётчик неудач.

    ЗАЧЕМ ЭТО ВООБЩЕ ХРАНИТЬ. «Когда последний раз получилось» снимает
    большую часть вопросов к карточке: человек видит не обещание «обновится
    само», а доказательство того, что оно уже работало сегодня. Без него
    единственным наблюдаемым фактом был срок истечения — и любой срок выглядел
    угрозой.

    Ошибка записи не роняет обновление токена: журнал полезен, но канал важнее
    (тот же приём, что у отметки живости сторожа).
    """
    try:
        await redis.set(
            _refresh_key(account_id),
            _journal_dump(RefreshJournal(last_ok_at=now or _utcnow())),
            ex=REFRESH_JOURNAL_TTL,
        )
    except Exception:  # noqa: BLE001 — наблюдение не важнее наблюдаемого
        log.warning("channel_health.journal_write_failed", account_id=str(account_id))


async def note_refresh_failed(
    redis: Redis, account_id: uuid.UUID, *, reason: str, now: datetime | None = None
) -> None:
    """Обновление не прошло: причина плюс счётчик неудач ПОДРЯД.

    Время последнего успеха при этом сохраняется — именно оно отвечает на
    вопрос «когда всё в последний раз было хорошо».
    """
    try:
        previous = await read_journal(redis, account_id)
        await redis.set(
            _refresh_key(account_id),
            _journal_dump(
                RefreshJournal(
                    last_ok_at=previous.last_ok_at,
                    last_error_at=now or _utcnow(),
                    last_error=reason,
                    failures=previous.failures + 1,
                )
            ),
            ex=REFRESH_JOURNAL_TTL,
        )
    except Exception:  # noqa: BLE001 — см. note_refresh_ok
        log.warning("channel_health.journal_write_failed", account_id=str(account_id))


# =============================================================================
# 2. Срок токена: три состояния вместо одного
# =============================================================================


@dataclass(frozen=True)
class TokenHealth:
    """Строка «Токен …» карточки канала целиком."""

    state: str
    #: Машинный повод, по которому выбрано состояние. ``None`` — всё штатно.
    reason: str | None
    #: Готовая строка по-русски. Экран вправе собрать свою из полей ниже, но
    #: тогда он обязан повторить и правила — а это второй источник правды.
    message: str
    expires_at: datetime | None
    #: Сколько минут осталось. Отрицательных не бывает: истёкший — это 0 и
    #: состояние ``critical``, а «минус три часа» на экране читается загадкой.
    expires_in_minutes: int
    #: Когда планировщик возьмётся за токен сам (срок минус два часа).
    auto_refresh_at: datetime | None
    last_refresh_at: datetime | None
    last_refresh_ok: bool | None
    last_error: str | None
    failures: int
    action: str | None

    #: ⚠ ДВЕ ЧАСТИ ФРАЗЫ, И `message` ПРИ ЭТОМ НЕ МЕНЯЕТСЯ (разбор интерфейса 13.08).
    #:
    #: Жалоба: янтарный абзац выходит на пять-шесть строк в колонке карточки — 190
    #: знаков у silence_abnormal, 157 у refresh_failed. Читают такую строку боковым
    #: зрением, а прочитать её так нельзя.
    #:
    #: `headline` — одна фраза: что случилось и чем грозит. `detail` — числа, ритм
    #: канала, время сверки, причина отказа. Экран рисует первую янтарной со значком,
    #: вторую — мельче и серым, тем же приёмом, что уже применён к «Последний раз
    #: обновился сам …».
    #:
    #: ⚠ ПОЧЕМУ НЕ ПОРЕЗАЛИ `message`, А ЗАВЕЛИ ДВА НОВЫХ ПОЛЯ. Сервер и фронт
    #: выкатываются ПОРОЗНЬ. Укороти `message` — и в окно между выкатками человек на
    #: старой сборке теряет вторую половину фразы НАСОВСЕМ: у тишины исчезают и
    #: обычная пауза канала, и время сверки. Поэтому `message` остаётся букву в букву,
    #: а старая сборка просто не знает про новые поля. Обратный случай тоже закрыт:
    #: новый фронт при старом сервере видит пустой `headline` и рисует `message`
    #: целиком — тот же приём, что уже держит «старый сервер без состояния».
    #:
    #: Дублирование здесь — цена безопасной выкатки, а не недосмотр. Сложить обратно
    #: в одно поле можно будет, когда обе стороны выкачены.
    headline: str = ""
    detail: str = ""


def token_health(
    account: AvitoAccount, journal: RefreshJournal, *, now: datetime | None = None
) -> TokenHealth:
    """Три состояния строки токена.

    НОРМА — до истечения больше :data:`TOKEN_WARN_AHEAD` и последняя попытка
    обновления не провалилась. Нейтрально, без знака: «Токен активен.
    Обновится автоматически 13.08 в 16:07». Именно это состояние занимает
    двадцать два часа из двадцати четырёх, и именно оно раньше показывалось
    жёлтым с восклицательным знаком.

    ВНИМАНИЕ — либо времени осталось меньше, чем нужно автообновлению на одну
    полную попытку, либо последняя попытка ЗАВЕРШИЛАСЬ ОШИБКОЙ. Второе важнее
    первого: срок сам по себе ещё ничего не значит, а провалившаяся попытка —
    уже факт.

    АВАРИЯ — токен истёк (ответы клиентам не уходят прямо сейчас) либо
    :data:`TOKEN_FAILURES_CRITICAL` неудач подряд (само не починится).

    ДВА КАНАЛА ВЫПАДАЮТ ИЗ ЭТОЙ ЛОГИКИ, И ОБА ОСОЗНАННО. Выключенный не
    обновляется вовсе — его никто не просил работать, и красная строка про
    истёкший токен была бы обвинением человеку за его же решение. Служебная
    заглушка регрессионного набора вообще не имеет токена Авито: в поле лежит
    строка-заполнитель со сроком на десять лет вперёд, и «Токен активен, до
    2036 года» на ней читалось как рабочий канал.
    """
    moment = now or _utcnow()
    expires_at = _aware(account.token_expires_at)
    auto_refresh_at = expires_at - accounts_service.REFRESH_AHEAD if expires_at else None
    left_minutes = max(0, int((expires_at - moment).total_seconds() // 60)) if expires_at else 0
    base: dict[str, Any] = {
        "expires_at": expires_at,
        "expires_in_minutes": left_minutes,
        "auto_refresh_at": auto_refresh_at,
        "last_refresh_at": journal.last_ok_at,
        "last_refresh_ok": journal.last_attempt_ok,
        "last_error": journal.last_error,
        "failures": journal.failures,
    }

    if account.is_service:
        return TokenHealth(
            state=OK,
            reason="service_stub",
            message="Служебная заглушка регрессионного набора — токена Авито у неё нет.",
            headline="Служебная заглушка регрессионного набора — токена Авито у неё нет.",
            action=None,
            **base,
        )
    if account.status == "disabled":
        return TokenHealth(
            state=OK,
            reason="disabled",
            message="Канал выключен — токен не обновляется, пока его не включат.",
            # «пока его не включат» — не деталь, а снятие двусмысленности: без него
            # «токен не обновляется» читается как поломка автоматики. Кусок, который
            # снимает двусмысленность заголовка, обязан остаться В заголовке.
            headline="Канал выключен — токен не обновляется, пока его не включат.",
            action=None,
            **base,
        )
    if expires_at is None:  # pragma: no cover — поле NOT NULL, но врать нечем
        return TokenHealth(
            state=OK,
            reason="unknown",
            message="Срок действия токена неизвестен.",
            # Ветка существует затем, чтобы НЕ выдумать дату. Делить тут нечего, и
            # дописывать тоже: состояние про незнание.
            headline="Срок действия токена неизвестен.",
            action=None,
            **base,
        )

    stamp = _day_clock(expires_at)
    if left_minutes <= 0:
        return TokenHealth(
            state=CRITICAL,
            reason="expired",
            message=(f"Токен истёк {stamp} — ответы клиентам не уходят."),
            headline="Токен истёк — ответы клиентам не уходят.",
            detail=f"Срок кончился {stamp}.",
            action=ACTION_REFRESH_TOKEN,
            **base,
        )
    if journal.failures >= TOKEN_FAILURES_CRITICAL:
        because = f" ({journal.last_error})" if journal.last_error else ""
        return TokenHealth(
            state=CRITICAL,
            reason="refresh_broken",
            message=(
                f"Автообновление не проходит {_times(journal.failures)} подряд{because}. "
                f"Когда токен истечёт ({stamp}), ответы клиентам перестанут уходить."
            ),
            headline=(
                "Автообновление токена не проходит — когда он истечёт, "
                "ответы клиентам перестанут уходить."
            ),
            # Счёт неудач и причина — вниз: с одной неудачей или с тремя вывод один.
            # Склонение обязано идти через `_times`, а не через свою запись рядом.
            detail=(
                f"Попытки не проходят {_times(journal.failures)} подряд{because}; "
                f"токен действует до {stamp}."
            ),
            action=ACTION_REFRESH_TOKEN,
            **base,
        )
    if journal.failures:
        because = f": {journal.last_error}" if journal.last_error else ""
        return TokenHealth(
            state=WARNING,
            reason="refresh_failed",
            message=(
                f"Последнее автообновление не сработало{because}. "
                f"Токен действует до {stamp}; если следующая попытка тоже не пройдёт, "
                "ответы перестанут уходить."
            ),
            # Провалившаяся попытка вперёд срока — тот же порядок, что записан выше:
            # срок сам по себе ещё ничего не значит, а провал уже факт. Условность
            # последствия («если не пройдёт и следующее») из заголовка не убирать:
            # без неё жёлтое состояние читается как уже случившаяся авария.
            # ⚠ ИЗВЕСТНАЯ НЕТОЧНОСТЬ, НАЗВАННАЯ ЯВНО. «Если не пройдёт и следующее»
            # обещает, что следующая попытка МОЖЕТ пройти. Причин отказа три
            # (avito_accounts.py), и две из них — «Авито не принял ключи приложения»
            # и «Авито отозвал доступ» — означают, что само не наладится никогда:
            # для них заголовок мягче правды.
            #
            # Не чиним здесь сознательно: разделение потребует классифицировать
            # причины отказа, а сегодня они приходят готовой строкой от Авито и
            # разбирать её по подстрокам — заводить своё правило на чужом тексте.
            # Цена неточности мала: состояние жёлтое, кнопка рядом, а деталь тут же
            # называет причину словами.
            headline=(
                "Автообновление токена не прошло — если не пройдёт и следующее, "
                "ответы перестанут уходить."
            ),
            detail=(
                f"{journal.last_error}. Токен действует до {stamp}."
                if journal.last_error
                else f"Токен действует до {stamp}."
            ),
            action=ACTION_REFRESH_TOKEN,
            **base,
        )
    if left_minutes <= TOKEN_WARN_AHEAD.total_seconds() // 60:
        return TokenHealth(
            state=WARNING,
            reason="expiring",
            message=(
                f"До истечения токена {human_minutes(left_minutes)}, а автообновление "
                "уже должно было пройти."
            ),
            # ⚠ ПОРЯДОК ПЕРЕВЁРНУТ СОЗНАТЕЛЬНО. Вести заголовок остатком времени —
            # значит вернуть отменённую строку «⚠️ Токен истекает через 23 ч»:
            # первую фразу читают боковым зрением, и число часов в ней снова станет
            # ежедневной ложной тревогой. Повод для жёлтого — не «мало осталось», а
            # «автоматика уже должна была сработать и не сработала».
            headline="Автообновление токена уже должно было пройти и не прошло.",
            detail=f"До истечения {human_minutes(left_minutes)}.",
            action=ACTION_REFRESH_TOKEN,
            **base,
        )
    assert auto_refresh_at is not None
    return TokenHealth(
        state=OK,
        reason=None,
        message=f"Токен активен. Обновится автоматически {_day_clock(auto_refresh_at)}",
        headline="Токен активен.",
        # «Обновится автоматически …» — не украшение: время планового обхода отвечает
        # на вопрос «а оно вообще само-то обновляется?», ради которого и жали кнопку.
        # Слова остаются теми же, меняется только этаж.
        detail=f"Обновится автоматически {_day_clock(auto_refresh_at)}.",
        action=None,
        **base,
    )


# =============================================================================
# 3. Обычный ритм канала
# =============================================================================


@dataclass(frozen=True)
class Rhythm:
    """Насколько часто на этом канале вообще что-то происходит.

    ``median_minutes`` — медиана паузы между обращениями В РАБОЧИХ МИНУТАХ.
    Медиана, а не среднее: одна ночная пауза в двенадцать часов сдвигает
    среднее так, что «обычной» становится пауза, которой не бывает.

    ``None`` — ритма мы не знаем (мало данных). Это НЕ «ритм нулевой»: по
    незнанию тревогу не поднимают.
    """

    median_minutes: int | None
    samples: int
    window_days: int = RHYTHM_WINDOW_DAYS


async def _event_times(
    db: AsyncSession, account_id: uuid.UUID, *, since: datetime
) -> list[datetime]:
    """Отметки времени обращений клиента по каналу, от новых к старым.

    ``direction='in' AND sender_type='client'`` — определение входящего от
    клиента, принятое во всём проекте (06 §2.3), и ровно под эту пару заведён
    частичный индекс ``idx_messages_client_in`` (миграция 0004).

    Считаем по СООБЩЕНИЯМ, а не по принятым вебхукам: сырец вебхуков живёт
    сутки-двое, а ритм нужен за месяц. Плюс сообщение — это то, что человек
    видел своими глазами, и спорить с ним нельзя.
    """
    rows = (
        await db.execute(
            select(Message.created_at)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.account_id == account_id,
                Message.direction == "in",
                Message.sender_type == "client",
                Message.created_at >= since,
            )
            .order_by(Message.created_at.desc())
            .limit(RHYTHM_MAX_EVENTS)
        )
    ).scalars()
    return [t for t in (_aware(row) for row in rows) if t is not None]


async def channel_rhythm(
    db: AsyncSession, redis: Redis, account_id: uuid.UUID, *, ctx: HealthContext
) -> Rhythm:
    """Медиана рабочей паузы между обращениями за :data:`RHYTHM_WINDOW_DAYS`.

    ЧЕРЕЗ КЭШ, И ЭТО НЕ ПРЕЖДЕВРЕМЕННАЯ ОПТИМИЗАЦИЯ. Запрос идёт по
    ``messages`` — самой горячей и единственной партиционированной таблице.
    Экран каналов открывают десятки раз в день, каналов девять; без кэша это
    девять проходов по месячному срезу на каждое открытие. Ритм при этом
    меняется за недели, а не за минуты, поэтому шесть часов жизни кэша ничего
    не портят.

    Ошибка Redis не должна ронять карточку: считаем заново, молча.
    """
    try:
        cached = _load_json(await redis.get(_rhythm_key(account_id)))
    except Exception:  # noqa: BLE001 — кэш полезен, но не обязателен
        cached = {}
    if cached:
        median = cached.get("median_minutes")
        return Rhythm(
            median_minutes=int(median) if median is not None else None,
            samples=int(cached.get("samples") or 0),
            window_days=int(cached.get("window_days") or RHYTHM_WINDOW_DAYS),
        )

    since = ctx.now - timedelta(days=RHYTHM_WINDOW_DAYS)
    times = sorted(await _event_times(db, account_id, since=since))
    gaps = [
        _work_minutes(ctx, earlier, later) for earlier, later in zip(times, times[1:], strict=False)
    ]
    rhythm = (
        Rhythm(median_minutes=int(math.ceil(statistics.median(gaps))), samples=len(gaps))
        if len(gaps) >= RHYTHM_MIN_SAMPLES
        else Rhythm(median_minutes=None, samples=len(gaps))
    )
    try:
        await redis.set(
            _rhythm_key(account_id),
            json.dumps(
                {
                    "median_minutes": rhythm.median_minutes,
                    "samples": rhythm.samples,
                    "window_days": rhythm.window_days,
                }
            ),
            ex=RHYTHM_TTL,
        )
    except Exception:  # noqa: BLE001 — см. выше
        log.warning("channel_health.rhythm_cache_failed", account_id=str(account_id))
    return rhythm


def quiet_threshold(rhythm: Rhythm) -> int | None:
    """С какой рабочей паузы тишина на ЭТОМ канале становится новостью.

    ``None`` — ритма не знаем, и порога нет вовсе: тревогу по тишине не
    поднимаем ни при каких числах. Обвинить подписку в этом случае может
    только сверка с Авито, то есть факт, а не догадка.
    """
    if rhythm.median_minutes is None:
        return None
    return max(QUIET_FLOOR_MINUTES, rhythm.median_minutes * QUIET_FACTOR)


# =============================================================================
# 4. Сверка подписки: спросить Авито, а не гадать
# =============================================================================

#: Наш адрес стоит у Авито — подписка на месте.
AUDIT_OURS = "ours"
#: Наш адрес заменён чужим. Авито держит на аккаунт ровно ОДНУ подписку
#: (проверено 9 августа на боевых аккаунтах, docs/24 §2), поэтому «чужой
#: адрес» и означает «канал отдали другой системе».
AUDIT_FOREIGN = "foreign"
#: Подписок нет вовсе: не «отобрали», а «не встала» — другая новость и другая
#: причина, хотя чинится тем же нажатием.
AUDIT_NONE = "none"
#: Спросить не удалось. Это НЕ «подписки нет»: молчание Авито доказательством
#: не является, и ложная тревога здесь стоила бы доверия ко всем остальным.
AUDIT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubscriptionAudit:
    """Итог последней сверки подписки с Авито."""

    checked_at: datetime | None = None
    result: str | None = None
    foreign_urls: list[str] = field(default_factory=list)
    error: str | None = None
    #: Сырой ответ Авито. В Redis НЕ хранится и из хранилища не читается: он
    #: нужен ровно тому, кто сверяет адреса глазами
    #: (``GET /avito-accounts/{id}/subscriptions``), а карточке — нет. Класть
    #: чужие адреса в кэш «на всякий случай» значило бы хранить сведения о
    #: чужих системах дольше, чем они кому-то нужны.
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lost(self) -> bool:
        """Подписка доказанно не наша."""
        return self.result in (AUDIT_FOREIGN, AUDIT_NONE)


def _audit_from(raw: Any) -> SubscriptionAudit:
    data = _load_json(raw)
    if not data:
        return SubscriptionAudit()
    urls = data.get("foreign_urls")
    error = data.get("error")
    return SubscriptionAudit(
        checked_at=_parse_time(data.get("at")),
        result=str(data["result"]) if data.get("result") else None,
        foreign_urls=[str(u) for u in urls] if isinstance(urls, list) else [],
        error=str(error) if error else None,
    )


async def read_audit(redis: Redis, account_id: uuid.UUID) -> SubscriptionAudit:
    return _audit_from(await redis.get(_audit_key(account_id)))


async def _store_audit(
    redis: Redis, account_id: uuid.UUID, audit: SubscriptionAudit
) -> SubscriptionAudit:
    try:
        await redis.set(
            _audit_key(account_id),
            json.dumps(
                {
                    "at": audit.checked_at.isoformat() if audit.checked_at else None,
                    "result": audit.result,
                    "foreign_urls": audit.foreign_urls,
                    "error": audit.error,
                },
                ensure_ascii=False,
            ),
            ex=AUDIT_TTL,
        )
    except Exception:  # noqa: BLE001 — сверка состоялась, запись о ней — мелочь
        log.warning("channel_health.audit_write_failed", account_id=str(account_id))
    return audit


async def audit_subscription(
    db: AsyncSession, redis: Redis, account: AvitoAccount, *, now: datetime | None = None
) -> SubscriptionAudit:
    """Спросить Авито, чей адрес стоит на этом аккаунте, и запомнить ответ.

    ЭТО ЕДИНСТВЕННЫЙ ЧЕСТНЫЙ СПОСОБ ОБВИНИТЬ ПОДПИСКУ. Тишина на канале —
    подозрение: за ней стоит либо пропавшая подписка, либо просто спокойный
    день, и отличить одно от другого по молчанию нельзя в принципе. Раньше
    карточка выбирала первое объяснение и писала про «подписку могла достаться
    другой системе» на канале, где с интеграцией всё было в порядке.

    Ходит в чужой API, поэтому зовётся редко: из ежедневного обхода сторожа и
    из быстрого — только для канала, который замолчал дольше СВОЕГО обычного
    (см. ``watchdog.check_quiet_channel_subscription``, не чаще
    :data:`AUDIT_MIN_INTERVAL`).

    Ошибку сети в приговор не превращаем: ``unknown`` — это «не смогли
    спросить», и карточка от него не краснеет.
    """
    from app.integrations.avito.client import AvitoClient

    moment = now or _utcnow()
    ours = accounts_service.webhook_url_for(account)
    try:
        client = await AvitoClient.fresh(db)
        items = await client.list_subscriptions(crypto.decrypt_token(account.access_token_enc))
    except Exception as exc:  # noqa: BLE001 — сеть, Авито, нечитаемый токен
        log.warning("channel_health.audit_unavailable", account_id=str(account.id), error=str(exc))
        return await _store_audit(
            redis,
            account.id,
            SubscriptionAudit(
                checked_at=moment, result=AUDIT_UNKNOWN, error="Авито не ответил на запрос подписок"
            ),
        )

    urls = [str(item.get("url")) for item in items if item.get("url")]
    if ours in urls:
        return await _store_audit(
            redis,
            account.id,
            SubscriptionAudit(checked_at=moment, result=AUDIT_OURS, items=items),
        )
    # Чужим считаем адрес, который не ведёт к нам ВООБЩЕ (``is_our_webhook_url``
    # сравнивает по началу): полный адрес несёт ``?secret=…``, свой у каждого
    # канала, и наш же адрес с перевыпущенным секретом чужим называть нельзя.
    foreign = [u for u in urls if not accounts_service.is_our_webhook_url(u)]
    return await _store_audit(
        redis,
        account.id,
        SubscriptionAudit(
            checked_at=moment,
            result=AUDIT_FOREIGN if foreign else AUDIT_NONE,
            foreign_urls=foreign,
            items=items,
        ),
    )


def audit_is_fresh(audit: SubscriptionAudit, *, now: datetime) -> bool:
    """Сверку можно повторять не чаще :data:`AUDIT_MIN_INTERVAL`."""
    return audit.checked_at is not None and now - audit.checked_at < AUDIT_MIN_INTERVAL


# =============================================================================
# 5. Подписка и тишина: строка «Webhook …»
# =============================================================================


@dataclass(frozen=True)
class WebhookHealth:
    """Строка «Webhook …» карточки канала целиком."""

    state: str
    reason: str | None
    message: str
    #: Как было и раньше: ok | failed | not_registered | unregister_failed.
    #: Это ПАМЯТЬ о последней попытке подписаться, а не факт о настоящем —
    #: факт даёт сверка (``check_result``).
    status: str
    url: str | None
    last_event_at: datetime | None
    #: Астрономическая тишина — то самое «событий нет N ч».
    silence_minutes: int | None
    #: Она же в РАБОЧИХ минутах: ночь и закрытая смена тишиной не считаются.
    quiet_minutes: int | None
    #: Обычная рабочая пауза этого канала; ``None`` — ритма не знаем.
    rhythm_minutes: int | None
    rhythm_samples: int
    rhythm_window_days: int
    #: С чем сравниваем ``quiet_minutes``; ``None`` — не с чем.
    threshold_minutes: int | None
    checked_at: datetime | None
    check_result: str | None
    action: str | None
    #: Две части фразы — разбор целиком записан у тех же полей в TokenHealth.
    #: `message` здесь тоже НЕ меняется: он остаётся страховкой на время
    #: раздельной выкатки сервера и фронта.
    headline: str = ""
    detail: str = ""


def _check_note(audit: SubscriptionAudit) -> str:
    """«Сверка 16:54, расхождений нет» — хвост строки про подписку.

    Время сверки на карточке снимает вопрос, ради которого раньше открывали
    журналы: смотрел ли кто-нибудь на подписку вообще и когда.
    """
    if audit.checked_at is None:
        return ""
    when = _clock(audit.checked_at)
    if audit.result == AUDIT_OURS:
        return f" Сверка подписки {when}, расхождений нет."
    if audit.result == AUDIT_UNKNOWN:
        return f" Сверка подписки {when}: Авито не ответил."
    return f" Сверка подписки {when}."


async def webhook_health(
    db: AsyncSession,
    redis: Redis,
    account: AvitoAccount,
    *,
    ctx: HealthContext,
) -> WebhookHealth:
    """Состояние приёма по каналу: подписка плюс тишина, сведённые в одно.

    ПОРЯДОК ВОПРОСОВ ЗДЕСЬ — ЭТО И ЕСТЬ ГЛАВНАЯ ПРАВКА.

    1. Что говорит СВЕРКА с Авито. Чужой адрес — красное «подписку забрала
       другая система» с кнопкой: это факт, и он не зависит от объёма
       обращений. Наш адрес — доказательство того, что с интеграцией всё в
       порядке, и оно перебивает даже пометку ``failed``, оставшуюся в Redis
       от давней неудачной попытки или потерянную при перезапуске.
    2. Только потом — тишина, и сравнивается она не с часами на стене, а с
       ОБЫЧНЫМ РИТМОМ ЭТОГО канала в рабочие часы. «Событий нет 16 ч» на
       канале с пятью обращениями в неделю — это норма, и говорить о ней надо
       нейтрально; те же 16 часов на канале с дневным потоком — новость.

    Раньше порядок был обратный: строка судила по абсолютному времени тишины и
    сама же объясняла её пропавшей подпиской, ни разу эту подписку не
    спросив.
    """
    registration = await accounts_service.get_webhook_state(redis, account.id)
    status = str(registration.get("status") or "not_registered")
    # Адрес отдаём ЗАПИСАННЫЙ при регистрации, а не вычисленный сейчас: у ещё
    # не подписанного канала его нет вовсе, и подставить сюда «как было бы»
    # значит показать человеку адрес, которого у Авито никогда не стояло.
    stored_url = registration.get("url")
    url = str(stored_url) if stored_url else None
    audit = await read_audit(redis, account.id)
    last_event_at = await _last_event_at(db, redis, account.id, ctx=ctx)
    rhythm = await channel_rhythm(db, redis, account.id, ctx=ctx)
    threshold = quiet_threshold(rhythm)

    silence_minutes: int | None = None
    quiet_minutes: int | None = None
    if last_event_at is not None:
        silence_minutes = max(0, int((ctx.now - last_event_at).total_seconds() // 60))
        quiet_minutes = _work_minutes(ctx, last_event_at, ctx.now)

    base: dict[str, Any] = {
        "status": status,
        "url": url,
        "last_event_at": last_event_at,
        "silence_minutes": silence_minutes,
        "quiet_minutes": quiet_minutes,
        "rhythm_minutes": rhythm.median_minutes,
        "rhythm_samples": rhythm.samples,
        "rhythm_window_days": rhythm.window_days,
        "threshold_minutes": threshold,
        "checked_at": audit.checked_at,
        "check_result": audit.result,
    }

    if account.is_service:
        return WebhookHealth(
            state=OK,
            reason="service_stub",
            message="Служебная заглушка регрессионного набора — события через неё не идут.",
            headline="Служебная заглушка регрессионного набора — события через неё не идут.",
            action=None,
            **base,
        )

    # «Не смогли снять подписку» — единственная новость о выключенном канале,
    # которая важна: человек думает, что откатился, а сообщения по-прежнему
    # идут к нам.
    if status == "unregister_failed":
        return WebhookHealth(
            state=WARNING,
            reason="unregister_failed",
            message=(
                "Канал выключен, но подписка на стороне Авито осталась нашей — "
                "сообщения по-прежнему идут сюда. Нажмите «Отключить» ещё раз."
            ),
            # ⚠ ЭТОТ «Нажмите» НЕ ИЗ ТЕХ СЕМИ, что сняли 13.08. Там убирали дубль
            # кнопки, стоящей вплотную справа; здесь `action=None`, кнопки в строке
            # нет вовсе, и без этих слов человек не узнает, что делать.
            # ⚠ ИНСТРУКЦИЯ ОСТАЁТСЯ В ЗАГОЛОВКЕ. У ветки `action=None`, кнопки в строке
            # нет вовсе, и «Нажмите «Отключить» ещё раз» — ЕДИНСТВЕННОЕ, что говорит
            # человеку, что делать. Первая редакция увела её в деталь заодно со всеми
            # хвостами — и состояние осталось бы без кнопки и без подсказки.
            #
            # Слова те же, что в `message` («на стороне Авито», не «у Авито»): у одной
            # фразы не должно быть двух редакций, на второй стоит фронт-тест.
            headline=(
                "Канал выключен, но подписка на стороне Авито осталась нашей — "
                "сообщения по-прежнему идут сюда. Нажмите «Отключить» ещё раз."
            ),
            action=None,
            **base,
        )
    if account.status == "disabled":
        return WebhookHealth(
            state=OK,
            reason="disabled",
            message="Канал выключен — события Авито в него не приходят.",
            headline="Канал выключен — события Авито в него не приходят.",
            action=None,
            **base,
        )

    if audit.lost:
        # ⚠ Переменная звалась `detail` и была переименована 13.08: у WebhookHealth
        # появилось поле с тем же именем, и совпадение читалось бы как «здесь и
        # собирают деталь», хотя это первая половина заголовка.
        #
        # «Забрала другая система» и «нет вовсе» — РАЗНЫЕ новости, и заголовки обязаны
        # остаться разными, хотя чинятся одним нажатием: «не встала» и «отобрали» —
        # разные причины.
        lost_kind = (
            "Подписку на события забрала другая система"
            if audit.result == AUDIT_FOREIGN
            else "Подписки на события у канала нет"
        )
        return WebhookHealth(
            state=CRITICAL,
            reason=f"subscription_{audit.result}",
            message=(
                f"{lost_kind}: обращения от клиентов к нам не приходят вовсе.{_check_note(audit)}"
            ),
            headline=f"{lost_kind} — обращения от клиентов к нам не приходят вовсе.",
            detail=_check_note(audit).strip(),
            action=ACTION_REWEBHOOK,
            **base,
        )

    subscription_confirmed = audit.result == AUDIT_OURS
    if not subscription_confirmed and status == "failed":
        return WebhookHealth(
            state=CRITICAL,
            reason="register_failed",
            message=(
                "Авито не принял нашу подписку на события: обращения от клиентов к нам не приходят."
            ),
            # 82 знака, ни чисел, ни сверки: обе половины держат друг друга, делить нечего.
            headline=(
                "Авито не принял нашу подписку на события: обращения от клиентов к нам не приходят."
            ),
            action=ACTION_REWEBHOOK,
            **base,
        )
    if not subscription_confirmed and status == "not_registered":
        return WebhookHealth(
            state=WARNING,
            reason="not_registered",
            message=(
                "Подписка на события не зарегистрирована — обращения от клиентов к нам не придут."
            ),
            headline=(
                "Подписка на события не зарегистрирована — обращения от клиентов к нам не придут."
            ),
            action=ACTION_REWEBHOOK,
            **base,
        )

    if last_event_at is None:
        return WebhookHealth(
            state=QUIET,
            reason="no_events_yet",
            message=f"Подписка стоит, событий ещё не было.{_check_note(audit)}",
            headline="Подписка стоит, событий ещё не было.",
            detail=_check_note(audit).strip(),
            action=None,
            **base,
        )

    assert silence_minutes is not None and quiet_minutes is not None
    silence = human_minutes(silence_minutes)
    if threshold is not None and quiet_minutes > threshold:
        usual = f" Обычная пауза на этом канале — {human_minutes(rhythm.median_minutes or 0)}."
        because = (
            " Подписка на месте, значит дело не в ней — проверьте приём сообщений."
            if subscription_confirmed
            else ""
        )
        return WebhookHealth(
            state=WARNING,
            reason="silence_abnormal",
            message=f"Событий нет {silence} — заметно дольше обычного.{usual}"
            f"{_check_note(audit)}{because}",
            # ⚠ ГРАНИЦА ПРОХОДИТ НЕ ПО ПЕРВОЙ ТОЧКЕ, И ЭТО САМОЕ ВАЖНОЕ ДЕЛЕНИЕ В ФАЙЛЕ.
            #
            # Из заголовка убрано АБСОЛЮТНОЕ время тишины: «Событий нет 16 ч» — это
            # ровно та отменённая строка, из-за которой канал «Дамир» горел вечно, а
            # соседний с дневным потоком показывал «в порядке». Первую фразу читают
            # боковым зрением, и число часов в ней снова стало бы мерилом — хотя
            # мерило здесь одно: ритм ЭТОГО канала.
            #
            # В деталь уезжают три куска, каждый целиком и по своей записанной причине:
            # обычная пауза канала (без неё строка судит по времени на стене), время
            # сверки (без него «смотрел ли кто-нибудь на подписку» снова уходит в
            # журналы) и вывод про подписку (без него тишину объясняют пропавшей
            # подпиской, ни разу её не спросив).
            # ⚠ ЗАГОЛОВКА ДВА, И ЭТО НЕ ПРИДИРКА. Первая редакция ставила один на оба
            # случая — «обращения могут не доходить», — и он ВРАЛ ровно там, где мы
            # знаем больше всего: при подтверждённой подписке сервер уже ДОКАЗАЛ, что
            # приём в порядке, кнопки не рисует (`action=None`), а заголовок всё равно
            # пугал недоставкой. Опровержение при этом лежало в детали, то есть
            # заголовок противоречил сам себе через строку.
            #
            # Разные случаи — разные новости: подписку не проверили (тогда тишина
            # действительно подозрительна, и рядом кнопка) и подписка на месте (тогда
            # новость только в самой длине паузы).
            headline=(
                "Событий нет заметно дольше обычного для этого канала."
                if subscription_confirmed
                else "Событий нет заметно дольше обычного — обращения могут не доходить."
            ),
            detail=f"Без событий {silence}.{usual}{_check_note(audit)}{because}",
            action=ACTION_REWEBHOOK if not subscription_confirmed else None,
            **base,
        )

    if rhythm.median_minutes is not None and quiet_minutes > rhythm.median_minutes:
        return WebhookHealth(
            state=QUIET,
            reason="quiet_but_normal",
            message=(
                f"Событий нет {silence}, для этого канала это в пределах нормы.{_check_note(audit)}"
            ),
            # Тот же порядок, что и у тревожной ветки: строка не начинается с числа
            # часов, даже когда она серая. Оценка отвечает на единственный вопрос
            # человека — «это сломалось или просто никто не писал».
            headline="Событий нет, и для этого канала это в пределах нормы.",
            detail=f"Без событий {silence}.{_check_note(audit)}",
            action=None,
            **base,
        )
    if rhythm.median_minutes is None and quiet_minutes > QUIET_FLOOR_MINUTES:
        # Ритма не знаем — говорим ровно то, что знаем, и ничем не пугаем.
        return WebhookHealth(
            state=QUIET,
            reason="quiet_unknown_rhythm",
            message=(
                f"Событий нет {silence}. Обращений на этом канале пока слишком мало, "
                f"чтобы судить, много это или обычно.{_check_note(audit)}"
            ),
            # ⚠ ЗДЕСЬ ОГОВОРКА САМА И ЕСТЬ НОВОСТЬ, поэтому она остаётся В ЗАГОЛОВКЕ.
            # «Событий нет 16 ч» с уточнением внизу — в точности запрещённая
            # конструкция: деталь снимала бы двусмысленность заголовка, а заголовок
            # обязан не врать в одиночку. Незнание ритма — не подробность, а причина,
            # по которой мы вообще не поднимаем тревогу.
            headline="Событий нет, но много это или обычно — сказать пока не по чему.",
            detail=(
                f"Без событий {silence}. Обращений на этом канале пока слишком мало, "
                f"чтобы знать его обычный ритм.{_check_note(audit)}"
            ),
            action=None,
            **base,
        )
    return WebhookHealth(
        state=OK,
        reason=None,
        message=f"События приходят, последнее в {_clock(last_event_at)}.{_check_note(audit)}",
        headline="События приходят.",
        detail=f"Последнее в {_clock(last_event_at)}.{_check_note(audit)}",
        action=None,
        **base,
    )


async def _last_event_at(
    db: AsyncSession,
    redis: Redis,
    account_id: uuid.UUID,
    *,
    ctx: HealthContext | None = None,
) -> datetime | None:
    """Когда по каналу в последний раз что-то было — по ДВУМ источникам.

    ``webhook_last:{id}`` пишет приёмник вебхуков на каждом событии; это самый
    свежий след, но живёт он в Redis и исчезает вместе с ним. Последнее
    сообщение клиента в базе переживает что угодно, но появляется только у
    событий, которые дошли до конвейера.

    Берём поздний из двух: расхождение между ними — само по себе новость
    («вебхуки идут, а сообщения не появляются»), но пугать тишиной, которой на
    самом деле не было, нельзя ни в одну сторону.
    """
    marker = _parse_time(await redis.get(f"webhook_last:{account_id}"))
    if ctx is not None and ctx.последние_входящие is not None:
        # Карта готова на всю страницу — второй раз в базу не идём.
        готовое = ctx.последние_входящие.get(account_id)
        return max(filter(None, (marker, готовое)), default=None)
    stored = _aware(
        (
            await db.execute(
                select(Message.created_at)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.account_id == account_id,
                    Message.direction == "in",
                    Message.sender_type == "client",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar()
    )
    known = [t for t in (marker, stored) if t is not None]
    return max(known) if known else None
