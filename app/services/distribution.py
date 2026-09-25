"""Автоматическое распределение обращений между операторами.

ЗАЧЕМ. Сейчас новое обращение встаёт в очередь «Входящие» и ждёт, пока
кто-нибудь нажмёт «Принять». Это честная модель, но у неё есть цена: при
потоке с девяти каналов первым берут не того, кто дольше ждёт, а того, кто
удобнее — и тринадцать человек соревнуются за одни и те же свежие обращения,
пока неудобные лежат. Автораспределение снимает и соревнование, и выбор:
обращение само приходит к тому, кто сейчас свободнее всех.

ОЧЕРЕДЬ НЕ ОТМЕНЯЕТСЯ, А СТАНОВИТСЯ СТРАХОВКОЙ
-----------------------------------------------
Раздать можно не всегда: ночью никого нет в сети, в пик у всех предел, у
канала может не быть ни одного назначенного онлайн. Во всех этих случаях
обращение остаётся в очереди ровно как раньше, и его забирают руками. То есть
распределение — это не замена очереди, а слой над ней: получилось — диалог у
человека, не получилось — он ждёт в общей.

Такое устройство даёт важное свойство: выключение распределения не ломает
ничего. Система просто возвращается к прежнему поведению.

КОГО СЧИТАТЬ ДОСТУПНЫМ
----------------------
Три условия, и каждое обязательно:

1. **Оператор канала.** Правило то же, что у очереди (``account_operators``):
   назначенные на канал, а если не назначен никто — все. Иначе обращение с
   «Парт-7» уедет тому, кто этот канал никогда не вёл.
2. **В сети.** Присутствие берётся из Redis (``ws/presence``). Раздать диалог
   человеку, закрывшему ноутбук, — худший исход из возможных: клиент не в
   очереди, где его видно всем, а «у оператора», и не ждёт никого конкретно.
3. **Не сверх предела** — если предел задан. У заказчика в Jivo лимитов нет
   вовсе: диалоги берут руками и сами решают, сколько потянут, и при ручном
   приёме это правильно. Автораздаче потолок нужен по другой причине: без него
   сотня обращений уедет тому, кто первым вышел в сеть, — не потому, что он
   справится, а потому, что оказался единственным доступным. Поэтому потолок
   настраиваемый, и «без ограничения» — законное значение.

КАК ВЫБИРАЕТСЯ ЧЕЛОВЕК
----------------------
Наименее загруженный: у кого меньше открытых диалогов. При равенстве — тот, у
кого дольше не было новых, чтобы не заваливать одного и того же (равенство при
старте смены — обычное дело: у всех по нулю).

«По кругу» (round-robin) сознательно не берём: он равномерен по ЧИСЛУ
розданных, а не по нагрузке. Оператор, которому достались три многословных
клиента, получит четвёртого наравне с тем, кто свои уже закрыл.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, User
from app.services import account_operators as acc_ops
from app.services import app_settings
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.ws import presence
from app.ws.presence import presence_status_map

log = structlog.get_logger("app.distribution")

# Диалоги, которые считаются НАГРУЗКОЙ. Не «открытые» — именно нагрузка, и
# после docs/38 это уже не одно и то же.
#
# ЧТО ИЗМЕНИЛОСЬ И ПОЧЕМУ. Раньше здесь стоял свой кортеж («new», «in_progress»)
# с подписью «открытые», и она была верна: значений было три, а не закрытый
# означало не разобранный. С появлением «Ждёт клиента» и «Отложен» совпадение
# кончилось. Оператор с двадцатью отложенными диалогами внимания на них СЕЙЧАС
# не тратит — ход не за ним, — и считать его занятым значило бы обойти его
# автораздачей в пользу того, у кого таких диалогов просто нет.
#
# Имя оставлено прежним (его знают тесты и вызовы), значение берётся из общего
# словаря: `ACTIVE_STATUSES` там и объявлен как «требует внимания сейчас».
OPEN_STATUSES: tuple[str, ...] = status_dict.ACTIVE_STATUSES


@dataclass(frozen=True)
class Candidate:
    user_id: uuid.UUID
    full_name: str
    load: int


@dataclass(frozen=True)
class DistributionResult:
    """Итог попытки раздать обращение."""

    assignee: User | None
    #: Почему не раздали — для лога и для объяснения администратору.
    reason: str | None = None
    #: Нагрузка выбранного НА МОМЕНТ выбора: попадает в audit.
    load_before: int | None = None


async def is_enabled(db: AsyncSession) -> bool:
    """Включено ли распределение.

    Читается из базы на каждом обращении, а не кэшируется. Лишний запрос по
    первичному ключу здесь дешевле правильности: выключить раздачу руководитель
    должен мочь НЕМЕДЛЕННО. Кэш на пять секунд означал бы, что после нажатия
    «выключить» диалоги ещё пять секунд продолжают уезжать людям, — и объяснить
    это человеку, который только что нажал кнопку, нечем.
    """
    return bool(await app_settings.get(db, app_settings.DISTRIBUTION_ENABLED))


async def max_active_per_operator(db: AsyncSession) -> int | None:
    """Потолок одновременных диалогов; ``None`` — без ограничения.

    «Без ограничения» — не теоретический вариант: у заказчика в Jivo лимитов
    нет вовсе, диалоги берут руками и сами решают, сколько потянут. При ручном
    приёме потолок и не нужен. Он нужен именно автораздаче: без него сотня
    обращений свалится на того, кто первым вышел в сеть.
    """
    value = await app_settings.get(db, app_settings.DISTRIBUTION_MAX_ACTIVE)
    return None if value is None else int(value)


async def _load_by_user(db: AsyncSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Сколько открытых диалогов у каждого — ОДНИМ запросом.

    Именно одним: считать по человеку в цикле означало бы тринадцать запросов
    на каждое входящее сообщение, а входящие идут пачками.
    """
    if not user_ids:
        return {}
    rows = await db.execute(
        select(Conversation.assignee_id, sa.func.count())
        .where(
            Conversation.assignee_id.in_(user_ids),
            Conversation.status.in_(OPEN_STATUSES),
        )
        .group_by(Conversation.assignee_id)
    )
    counts = {uid: int(n) for uid, n in rows.all() if uid is not None}
    # У кого диалогов нет вовсе, в GROUP BY не будет — а он-то и есть самый
    # свободный. Без этой строки такие люди никогда бы ничего не получили.
    return {uid: counts.get(uid, 0) for uid in user_ids}


async def _names_and_last_assigned(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, datetime | None]]:
    """Имя и «когда в последний раз доставался диалог» — ОДНИМ запросом по
    тринадцати строкам.

    ЗДЕСЬ БЫЛО ДВА ЗАПРОСА, И ВТОРОЙ БЫЛ ДОРОГИМ (#35). Тай-брейк выводился
    из `max(updated_at)` по всем диалогам этих людей — без фильтра по статусу
    и без границы по времени. То есть на каждое новое обращение перебиралась
    вся история их работы, включая закрытое год назад; стоимость росла вместе
    с базой и никогда не падала.

    И СЧИТАЛ ОН НЕ ТО, ЧТО ОБЕЩАЛ. `updated_at` меняется от любой правки
    диалога — пришло сообщение, сменился статус. Оператор, который час назад
    получил диалог и только что ответил клиенту, выглядел только что
    получившим и уходил в конец очереди на раздачу, уступая тому, кто сидит
    без дела с утра, но чьих старых диалогов никто не трогал. Признак,
    заведённый ради справедливости, работал против неё.

    Второй запрос — за именами — грузил ПОЛНЫЕ объекты пользователей ради
    одного поля. Теперь берутся три колонки, и он же отдаёт отметку.
    """
    if not user_ids:
        return {}, {}
    rows = (
        await db.execute(
            select(User.id, User.full_name, User.last_assigned_at).where(User.id.in_(user_ids))
        )
    ).all()
    names = {uid: name for uid, name, _ in rows}
    last: dict[uuid.UUID, datetime | None] = {uid: ts for uid, _, ts in rows}
    return names, {uid: last.get(uid) for uid in user_ids}


async def eligible_candidates(
    db: AsyncSession, redis: Redis, conv: Conversation
) -> list[Candidate]:
    """Кому МОЖНО отдать этот диалог, от самого свободного к самому занятому."""
    # 1. Операторы канала — то же правило, что у очереди (7.2).
    assigned = await acc_ops.operator_ids_for_account(db, conv.account_id)
    if assigned:
        ids = {uuid.UUID(x) for x in assigned}
    else:
        # Канал без назначенных доступен всем живым операторам.
        rows = await db.execute(select(User.id).where(*convs.operator_pool_conditions()))
        ids = set(rows.scalars())
    if not ids:
        return []

    # 2. В сети И НЕ ОТОШЁЛ (#34).
    #
    # «Отошёл» — это «я за столом, но новых не берите»: обед, перекур,
    # разговор по телефону, разбор сложного случая с коллегой. Раньше такого
    # состояния не было вовсе, и отойти можно было только закрыв приложение —
    # то есть перестав видеть свои же диалоги и потеряв их через три минуты
    # к сторожу возврата.
    #
    # Хуже того: отошедший обычно САМЫЙ СВОБОДНЫЙ по числу открытых диалогов,
    # а выбор идёт от самого свободного (сортировка ниже). То есть обращения
    # доставались бы в первую очередь тому, кого нет за столом.
    #
    # Читаем именно статус, а не «подключён ли»: `presence_map` возвращает
    # bool и «away» от «online» не отличает — на этом задача и стояла.
    status = await presence_status_map(redis, sorted(ids))
    ids = {uid for uid in ids if status.get(uid) == presence.ONLINE}
    if not ids:
        return []

    # 3. Не сверх предела. Потолка может не быть вовсе — тогда шаг пропускается,
    # но нагрузка всё равно нужна: по ней выбирается самый свободный.
    loads = await _load_by_user(db, ids)
    cap = await max_active_per_operator(db)
    if cap is not None:
        ids = {uid for uid in ids if loads.get(uid, 0) < cap}
        if not ids:
            return []

    names, last = await _names_and_last_assigned(db, ids)
    # Сортировка: сначала меньшая нагрузка, при равенстве — тот, кому дольше
    # не доставалось. `datetime.min` для «не доставалось никогда» ставит
    # новичка впереди, и это правильно: у него точно есть место.
    never = datetime.min.replace(tzinfo=UTC)

    def key(uid: uuid.UUID) -> tuple[int, datetime]:
        ts = last.get(uid)
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (loads.get(uid, 0), ts or never)

    return [
        Candidate(user_id=uid, full_name=names.get(uid, "—"), load=loads.get(uid, 0))
        for uid in sorted(ids, key=key)
    ]


async def pick_assignee(db: AsyncSession, redis: Redis, conv: Conversation) -> DistributionResult:
    """Выбрать, кому отдать обращение. ``assignee=None`` — оставить в очереди.

    Функция НИЧЕГО НЕ ПИШЕТ: она только выбирает. Присвоение делает вызывающий,
    внутри своей транзакции и вместе с системной записью в ленту — иначе диалог
    мог бы оказаться назначенным без следа о том, кем и почему.
    """
    if not await is_enabled(db):
        return DistributionResult(None, reason="distribution_off")

    candidates = await eligible_candidates(db, redis, conv)
    if not candidates:
        # Разделять причины здесь не нужно: для очереди все они означают одно —
        # «раздать некому, пусть ждёт». Подробность уедет в лог выше по стеку.
        return DistributionResult(None, reason="no_available_operator")

    best = candidates[0]
    user = await db.get(User, best.user_id)
    if user is None:  # гонка: сотрудника удалили между выборкой и чтением
        return DistributionResult(None, reason="no_available_operator")
    return DistributionResult(user, load_before=best.load)
