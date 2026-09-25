"""Таблица диалогов с метриками (план 7.3, разбор Jivo 15 §3).

ЧТО ЭТО. Второй способ смотреть на те же диалоги: не лента для работы, а
таблица для разбора. «Покажи все обращения за неделю по каналу Парт-7, где
первый ответ был дольше пятнадцати минут» — вопрос руководителя, и в списке
диалогов на него не ответить никак.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ФЛАГ В `list_conversations`
---------------------------------------------------------
У них расходится всё, кроме источника строк: другой набор колонок (метрики
вместо превью), другая сортировка (по любой колонке, а не фиксированная),
другая пагинация и другая цена запроса. Ветка «если table, то иначе» внутри
списка означала бы, что правка рабочего экрана оператора может уронить отчёт
руководителя и наоборот.

ЦЕНА ЗАПРОСА — ГЛАВНОЕ ОГРАНИЧЕНИЕ
----------------------------------
У заказчика 454 тысячи диалогов (15 §1). Метрики считаются подзапросами по
`messages` — это дорого на строку, и считать их для всей выборки нельзя.
Поэтому порядок ровно такой:

    отфильтровать и отсортировать дешёвыми колонками
    → отрезать страницу (LIMIT/OFFSET)
    → и только для этих 50 строк посчитать метрики

Иначе «показать первую страницу за год» превратилось бы в вычисление метрик
для четырёхсот тысяч диалогов ради пятидесяти видимых.

СОРТИРОВКА ПО МЕТРИКЕ — ОТДЕЛЬНЫЙ, ДОРОГОЙ РЕЖИМ
------------------------------------------------
Долгое время сортировки по «первому ответу» не было вовсе, и довод звучал так:
она требует посчитать метрику для всей выборки, то есть ровно того, чего мы
избегаем. Довод верный, а вывод из него был неверный — потому что экран
существует ради одного вопроса, и вопрос этот записан в его же описании:
«где первый ответ был дольше пятнадцати минут». Отчёт, который не умеет
показать худшие строки первыми, на свой единственный вопрос не отвечает:
руководителю оставалось листать страницы глазами и искать красное.

Поэтому сортировка по метрике есть, но живёт в отдельном списке
(:data:`METRIC_SORTABLE`) и стоит дороже: порядок считается подзапросами по
`messages` для КАЖДОЙ строки выборки, а не для пятидесяти видимых. Цена
ограничена явно — :data:`METRIC_SORT_MAX_ROWS`: шире этого выборку сначала
надо сузить фильтром, и об этом говорится словами, а не полуминутным
ожиданием. Тот же приём, что у :data:`MAX_OFFSET` и :data:`EXPORT_MAX_ROWS`:
честный отказ вместо тихого торможения.

Когда объём вырастет настолько, что сужать станет нечем, метрику надо
денормализовать в колонку `conversations` на записи — тогда она переедет в
дешёвый :data:`SORTABLE` и потолок снимется. Пока этого не сделано, потолок
и есть граница честности отчёта.

ГЛУБИНА ПАГИНАЦИИ
-----------------
OFFSET на большой глубине действительно вреден: Postgres честно пролистывает
пропущенные строки. Но паниковать рано — до нескольких тысяч это единицы
миллисекунд. Смысла листать до четырёхсоттысячной строки нет ни у кого:
человек сужает фильтром. Поэтому глубина ограничена явно (:data:`MAX_OFFSET`),
и по достижении предела ответ говорит «уточните фильтр», а не молча тормозит.
"""

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import ApiError
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, User
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services.csv_cells import csv_cell
from app.services.user_ref import normalize_department

PAGE_LIMIT_MAX = 100
PAGE_LIMIT_DEFAULT = 50

#: Дальше листать не даём — см. «Глубина пагинации» в шапке модуля.
MAX_OFFSET = 5_000

#: Чем МОЖНО сортировать: только то, что лежит в колонках `conversations` и
#: покрыто индексом. Список закрытый намеренно — иначе первая же добавленная
#: колонка-метрика тихо превратит отчёт в полный перебор.
SORTABLE: dict[str, InstrumentedAttribute[Any]] = {
    "last_message_at": Conversation.last_message_at,
    "updated_at": Conversation.updated_at,
    "status": Conversation.status,
    "unread_count": Conversation.unread_count,
}
DEFAULT_SORT = "last_message_at"

#: Чем можно сортировать ДОРОГО: метрика считается подзапросами по `messages`
#: для всей выборки, а не для видимой страницы (см. шапку модуля). Список
#: отдельный от :data:`SORTABLE` именно поэтому — цена у них разная на порядки,
#: и смешать их в один словарь значило бы потерять единственное место, где эта
#: разница видна.
METRIC_SORTABLE: frozenset[str] = frozenset({"first_response_sec"})

#: Потолок выборки для сортировки по метрике. Шире — отказ с просьбой сузить
#: фильтр.
#:
#: ПОЧЕМУ НЕ ТО ЖЕ ЧИСЛО, ЧТО У ВЫГРУЗКИ. :data:`EXPORT_MAX_ROWS` ограничивает
#: РАЗМЕР ФАЙЛА, который откроют в Excel; здесь ограничивается СТОИМОСТЬ
#: ПРОХОДА по таблице сообщений. Величины разные по смыслу, и связать их одним
#: числом значило бы, что правка одной молча меняет другую.
METRIC_SORT_MAX_ROWS = 20_000

SortDir = Literal["asc", "desc"]


@dataclass(frozen=True)
class TableFilters:
    """Восемь фильтров таблицы (план 7.3). Все необязательные и складываются."""

    status: str | None = None
    account_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    tag: str | None = None
    bot_active: bool | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str | None = None
    #: Бот КОГДА-ЛИБО писал в этом диалоге. Не путать с `bot_active`: тот
    #: отвечает «бот ведёт диалог прямо сейчас», а этот — «бот здесь был».
    #: Экран «Диалоги бота» (docs/45) разбирает уже случившееся, и живой
    #: признак для него бесполезен: к моменту разбора бот давно вышел.
    bot_touched: bool | None = None
    #: Вид сбоя из `handoff.OUTCOME_GROUPS`. Отбор идёт по ПОСЛЕДНЕЙ записи
    #: журнала о боте в этом диалоге — см. :func:`_bot_outcome_cond`.
    bot_outcome: str | None = None


def _searching(f: TableFilters) -> bool:
    """Задан ли поиск. Пробелы не в счёт — «   » это не запрос."""
    return bool(f.q and f.q.strip())


def _select_narrowed(db: AsyncSession, f: TableFilters, *columns: Any) -> sa.Select[Any]:
    """Выборка диалогов, суженная фильтрами: одна сборка на счёт и на страницу.

    Раньше `WHERE` собирался в трёх местах отдельно, и это работало, пока все
    условия смотрели в одну таблицу. Поиск смотрит ещё и в `clients`, а значит
    выборке нужен не только новый предикат, но и новое соединение — забыть его
    в одном из трёх мест значило бы получить счётчик и страницу, посчитанные
    по разным множествам: «Найдено: 12» над тремя строками.

    Соединение добавляется ТОЛЬКО под поиск. В остальных запросах — а их
    подавляющее большинство — выборка остаётся такой же, какой была: по одной
    таблице, без лишнего join'а на четырёхстах тысячах строк.
    """
    stmt = sa.select(*columns).select_from(Conversation)
    if _searching(f):
        stmt = stmt.join(Client, Client.id == Conversation.client_id)
    return stmt.where(*_conditions(db, f))


def _not_service() -> sa.ColumnElement[bool]:
    """Служебные записи smoke-набора (07 §6) в отчёт не попадают.

    ЧТО БЫЛО НА БОЮ (12 августа). В «Разборе диалогов» стояла строка с клиентом
    «SMOKE (служебный)» и чатом `SMOKE-CONV` — служебный диалог, который
    заводит `seed-smoke` на КАЖДОМ деплое, чтобы проверить, что система жива.
    Руководитель видел его наравне с обращениями и считал по нему: он попадал
    и в «Найдено: N», и в выгрузку для Excel, и в среднее по колонкам.

    УСЛОВИЕ ЖИВЁТ ЗДЕСЬ, А НЕ В КАЖДОМ ЗАПРОСЕ. `_conditions` — единственное
    место, где собирается `WHERE` таблицы; и счёт, и страница, и выгрузка идут
    через него (см. :func:`_select_narrowed`). Поставь мы фильтр рядом со
    строками — счётчик «Найдено» продолжал бы считать служебное, и над девятью
    строками стояло бы «Найдено: 10».

    ФИЛЬТР БЕЗУСЛОВНЫЙ, а не «по галочке»: показывать служебное в отчёте
    незачем никому. Кому нужно посмотреть на сам smoke-диалог — открывает его
    в списке диалогов по ссылке, отчёт для другого.

    ДВА ИСТОЧНИКА СЛУЖЕБНОСТИ, а не один. Аккаунт — потому что диалог
    привязан к каналу и служебный канал даёт служебные диалоги. Ответственный
    — потому что smoke входит в систему роботом и работает диалогом от его
    имени; попадись такой диалог в отчёт, руководитель увидел бы в разбивке по
    операторам сотрудника, которого нет.
    """
    service_account = (
        sa.select(sa.literal(1))
        .select_from(AvitoAccount)
        .where(AvitoAccount.id == Conversation.account_id, AvitoAccount.is_service.is_(True))
        .exists()
    )
    service_assignee = (
        sa.select(sa.literal(1))
        .select_from(User)
        .where(User.id == Conversation.assignee_id, User.is_service.is_(True))
        .exists()
    )
    return sa.not_(sa.or_(service_account, service_assignee))


def _bot_touched() -> sa.ColumnElement[bool]:
    """Бот когда-либо писал в этом диалоге.

    `sender_type='bot'` ставит ОДИН путь — `app/bots/handoff.py`, — и через
    него проходят обе разновидности бота, сценарная и Лид-бот (выбор делает
    `app/bots/provider.py`). Поэтому признак единственный и врать ему нечем.
    """
    return (
        sa.select(sa.literal(1))
        .select_from(Message)
        .where(Message.conversation_id == Conversation.id, Message.sender_type == "bot")
        .exists()
    )


def _bot_outcome(group: str) -> sa.ColumnElement[bool]:
    """Отбор по виду сбоя — по ПОСЛЕДНЕЙ записи журнала о боте в диалоге.

    ⚠ ИМЕННО ПОСЛЕДНЕЙ, А НЕ ЛЮБОЙ. Диалог может вернуться в очередь, и бот
    вступит снова: тогда записей о нём будет несколько. Отбор «есть хоть одна
    запись с такой причиной» показал бы диалог в плитке позапрошлого захода —
    то есть в списке «бот закрыл сам» стояли бы диалоги, которые бот потом
    благополучно передал человеку.

    «Последняя» выражена через `NOT EXISTS` более поздней, а не через
    `max(created_at)`: так запрос остаётся на индексе
    `idx_audit_entity (entity, entity_id, created_at)` и не считает агрегат по
    всем записям диалога.
    """
    from app.bots import handoff

    события = list(handoff.BOT_EVENTS)
    a = sa.orm.aliased(AuditLog)
    b = sa.orm.aliased(AuditLog)

    свой = (
        sa.select(sa.literal(1))
        .select_from(b)
        .where(
            b.entity == "conversation",
            b.entity_id == a.entity_id,
            b.action.in_(события),
            b.created_at > a.created_at,
        )
        .exists()
    )

    if group == "stuck":
        совпадение: sa.ColumnElement[bool] = a.action == handoff.STUCK_ACTION
    else:
        причины = handoff.reasons_of(group)
        if not причины:
            # Неизвестная группа не должна молча вернуть ВСЮ таблицу: пустой
            # список в `in_` даёт ложь, и экран честно покажет ноль строк.
            причины = ("",)
        # Закрытие ботом (`bot.closed`, проверка 24.09) — с той же причиной:
        # отказ по регламенту попадает в «Бот закрыл сам», где бы ни записался.
        совпадение = sa.and_(
            a.action.in_(("bot.handoff", handoff.BOT_CLOSED_ACTION)),
            a.details["reason"].as_string().in_(причины),
        )

    return (
        sa.select(sa.literal(1))
        .select_from(a)
        .where(
            a.entity == "conversation",
            a.entity_id == sa.cast(Conversation.id, sa.Text),
            a.action.in_(события),
            совпадение,
            sa.not_(свой),
        )
        .exists()
    )


def _conditions(db: AsyncSession, f: TableFilters) -> list[sa.ColumnElement[bool]]:
    conds: list[sa.ColumnElement[bool]] = [_not_service()]
    if f.status:
        conds.append(Conversation.status == f.status)
    if f.account_id:
        conds.append(Conversation.account_id == f.account_id)
    if f.assignee_id:
        conds.append(Conversation.assignee_id == f.assignee_id)
    if f.bot_active is not None:
        conds.append(Conversation.bot_active.is_(f.bot_active))
    if f.bot_touched:
        conds.append(_bot_touched())
    if f.bot_outcome:
        conds.append(_bot_outcome(f.bot_outcome))
    if f.tag:
        # Условие берётся из списка диалогов, а не пишется заново: там оно уже
        # умеет и PostgreSQL (`tags @> ARRAY[..]`), и SQLite юнит-тестов.
        # Своя копия неминуемо разъехалась бы с оригиналом.
        conds.append(convs._has_tag(db, f.tag))
    if _searching(f):
        # ПОИСК ПЕРЕСТАЛ МОЛЧАТЬ. Параметр `q` объявлен в фильтрах и принимается
        # обеими ручками таблицы с самого начала, а в условия не попадал вовсе:
        # запрос с `q=Иванов` возвращал полную выборку, молча сделав вид, что
        # поиск применён. Экран этим параметром пока не пользуется, и потому
        # никто не заметил, — но принятый и выброшенный фильтр хуже
        # отсутствующего: он врёт тому, кто дойдёт до него первым.
        #
        # Условие берётся из списка диалогов, а не пишется заново, — как и
        # `_has_tag` выше. Там оно уже умеет и полнотекстовый поиск PostgreSQL,
        # и вырождение в LIKE на SQLite юнитов, и, главное, уже решает, что
        # заметки в поиск не входят: своя копия однажды забыла бы про это, и
        # observer нашёл бы диалог по тексту, которого ему видеть нельзя.
        #
        # Условие смотрит на колонки `clients`, поэтому выборка присоединяет их
        # — см. :func:`_select_narrowed`. Обернуть его в EXISTS по клиенту и
        # обойтись без соединения НЕ ВЫХОДИТ, и это надо знать заранее: внутри
        # условия сидит второй EXISTS по `messages`, связанный с
        # `conversations.id`. Оказавшись через уровень от выборки, он перестаёт
        # находить `conversations` в объемлющем FROM и дописывает их себе сам —
        # то есть превращается в «существует ЛЮБОЙ диалог с таким текстом» и
        # отвечает «да» для каждой строки. Поиск при этом не падает и не пуст,
        # он просто возвращает всё — ровно та поломка, которую чинили.
        conds.append(convs._search_condition(db, f.q or ""))
    # Период — по последней активности: «диалоги за неделю» человек понимает
    # как «те, где на неделе что-то происходило», а не «заведённые на неделе».
    if f.date_from:
        conds.append(Conversation.last_message_at >= f.date_from)
    if f.date_to:
        conds.append(Conversation.last_message_at < f.date_to)
    return conds


def check_sort(sort: str) -> None:
    if sort in SORTABLE or sort in METRIC_SORTABLE:
        return
    allowed = [*SORTABLE, *sorted(METRIC_SORTABLE)]
    raise ApiError(
        "validation_error",
        "По этой колонке сортировать нельзя",
        status=400,
        details={"fields": [{"field": "sort", "rule": "enum", "message": "|".join(allowed)}]},
    )


# --- метрики: ОДНО определение на выдачу и на сортировку --------------------
#
# Подзапросы вынесены в функции и переиспользуются и колонками строки, и
# порядком сортировки. Это не борьба с дублем ради красоты: скопированное
# условие разъезжается, а разъехавшееся здесь означает таблицу, где столбец
# «Первый ответ» показывает одни числа, а отсортирована она по другим. Такую
# поломку не видно вовсе — строки же расположены «как-то», — и заметят её не
# раньше, чем кто-нибудь начнёт сверять глазами.


#: Вопрос клиента, от которого идёт отсчёт, — ОТДЕЛЬНОЙ СТРОКОЙ SQL.
#:
#: Он нужен дважды: сам по себе (начало отсчёта) и внутри условия «ответ не
#: раньше вопроса» (:func:`_first_operator_at`). Вложенный подзапрос своё имя
#: таблицы обязан иметь другое (`m2`): попади он внутрь выборки по `m`, `m`
#: перекрыло бы себя же, и условие сравнивало бы сообщение с самим собой.
_FIRST_CLIENT_SQL = (
    "(SELECT min(m2.created_at) FROM messages m2 "
    "WHERE m2.conversation_id = conversations.id "
    "AND m2.direction = 'in' AND m2.sender_type = 'client')"
)


def _first_client_at() -> Any:
    """Когда клиент написал впервые — начало отсчёта времени ответа.

    `sender_type = 'client'` СТОИТ ЗДЕСЬ НЕ ДЛЯ КРАСОТЫ. Ровно такое условие
    считает начало отсчёта в статистике (`stats.py`, `_CONV_STARTED_LIVE_CTE`)
    и в витрине (миграция 0004). Пока здесь было только `direction = 'in'`,
    два экрана про одно и то же меряли от разных моментов — и сойтись им было
    негде.

    `literal_column`, а не `text`: выражение стоит и в списке колонок (там ему
    нужен `label`), и в арифметике вычитания. `text` ни того, ни другого не
    умеет — он кусок SQL, а не значение.
    """
    return sa.literal_column(_FIRST_CLIENT_SQL)


def _first_operator_at() -> Any:
    """Когда ОПЕРАТОР ОТВЕТИЛ: не бот, не провал отправки и НЕ РАНЬШЕ ВОПРОСА.

    ЧТО БЫЛО НА БОЮ (12 августа). Столбец «Первый ответ» стоял пустым — «—» —
    во всех строках разбора, включая закрытые переписки на десять сообщений.
    При этом «Статистика» медиану первого ответа считала и показывала. Данные
    были, показать их таблица не могла: она брала САМОЕ РАННЕЕ исходящее
    оператора вообще, без оглядки на то, было ли к тому моменту о чём
    отвечать. Стоило нашему сообщению оказаться в переписке раньше первого
    вопроса клиента — а так выходит у всей истории, загруженной из Авито, где
    первым писал продавец, — и разность получалась отрицательной. Отрицательную
    :func:`_seconds_between` честно превращает в `None`, то есть в прочерк. Для
    руководителя это выглядело как «отчёт не считает время ответа», и проверить
    его было нечем.

    ПОЧЕМУ ГРАНИЦА, А НЕ `abs()`. Ответ — это ответ НА ВОПРОС: величина имеет
    смысл только от вопроса вперёд. Наше сообщение, отправленное до первого
    обращения клиента, не ответ ему ни в каком смысле, и брать его за точку
    отсчёта нельзя. Поэтому берётся первое исходящее ПОСЛЕ первого вопроса —
    слово в слово так же, как в статистике (`stats.py`: `m.created_at >=
    f.first_client_at`). Совпадение определений и есть цель правки: два экрана
    одного продукта не имеют права считать «первый ответ» по-разному.

    Клиент не писал вовсе — `_FIRST_CLIENT_SQL` даёт NULL, сравнение с ним
    ложно для каждой строки, и ответа нет. Это правда: отвечать было не на что.
    """
    return (
        sa.select(sa.func.min(sa.text("m.created_at")))
        .select_from(sa.text("messages m"))
        .where(
            sa.text("m.conversation_id = conversations.id"),
            sa.text("m.direction = 'out'"),
            sa.text("m.sender_type = 'operator'"),
            sa.text("m.delivery_status <> 'failed'"),
            sa.text(f"m.created_at >= {_FIRST_CLIENT_SQL}"),
        )
        .scalar_subquery()
    )


def _first_response_expr(db: AsyncSession) -> sa.ColumnElement[Any]:
    """Время первого ответа В СЕКУНДАХ выражением SQL — для сортировки.

    ВЕТКА ПО ДИАЛЕКТУ ЗДЕСЬ НЕИЗБЕЖНА, в отличие от остального модуля: вычесть
    одну метку времени из другой PostgreSQL и SQLite умеют по-разному, и общего
    написания у этого нет. Приём в проекте принят (`conversations._has_tag`).

    ЗАЩИТА ОТ ОТРИЦАТЕЛЬНОЙ РАЗНОСТИ ОСТАЁТСЯ, хотя после правки
    :func:`_first_operator_at` (ответ не раньше вопроса) отрицательной разности
    взяться неоткуда. Стоит она полутора символами, а стережёт дорогое: если
    условие «не раньше вопроса» когда-нибудь потеряется, столбец покажет
    прочерк (`_seconds_between` вернёт `None`), а сортировка без этой ветки
    поставила бы такую строку ПЕРВОЙ среди самых быстрых — экран и порядок
    строк начали бы противоречить друг другу молча.
    """
    first_in = _first_client_at()
    first_out = _first_operator_at()
    seconds: sa.ColumnElement[Any]
    if db.get_bind().dialect.name == "postgresql":
        seconds = sa.func.extract("epoch", first_out - first_in)
    else:
        # SQLite: julianday даёт дни дробным числом, отсюда множитель.
        seconds = (sa.func.julianday(first_out) - sa.func.julianday(first_in)) * 86_400.0
    return sa.case((seconds >= 0, seconds), else_=sa.null())


#: Витрина статистики — то же имя, что `stats.MV` (этот модуль импортирует
#: `stats`, наоборот нельзя: цикл импорта).
_MV = "mv_conversation_stats"

#: Запас на время самого пересчёта витрины: отметка свежести пишется ПОСЛЕ
#: пересчёта, а снимок данных витрина берёт в его начале.
MV_LAG_MARGIN = timedelta(minutes=10)


def _first_response_sort_expr(
    db: AsyncSession, mv_refreshed_at: datetime | None
) -> sa.ColumnElement[Any]:
    """Ключ сортировки по «Первому ответу»: витрина, а живьём — только свежее.

    ЗАМЕР БОЯ 24.09. Живой счёт по всей выборке — подзапросы по `messages` на
    каждую строку — стоил 2,7 с на «7 днях» (12 112 строк), 16,3 с на «30 днях»
    (44 849) и 19,4 с на «90». Поэтому стоял потолок в 20 000 строк, и на
    умолчательном периоде экран сортировать по этой колонке отказывался — а
    ради вопроса «где ответили хуже всего» экран и существует.

    Витрина считает ту же величину тем же определением (`frt_operator_sec`,
    см. :func:`_first_operator_at`), и её строка находится по индексу на
    `conversation_id`. Устареть она может только у диалогов, в которых что-то
    происходило после пересчёта: им ответ досчитывается живьём, остальным
    берётся из витрины. Без отметки свежести витрине не верим вовсе.
    """
    live = _first_response_expr(db)
    if mv_refreshed_at is None or db.get_bind().dialect.name != "postgresql":
        return live
    from_mv = (
        sa.select(sa.literal_column("s.frt_operator_sec"))
        .select_from(sa.text(f"{_MV} s"))
        .where(sa.text("s.conversation_id = conversations.id"))
        .scalar_subquery()
    )
    fresh = Conversation.last_message_at >= mv_refreshed_at - MV_LAG_MARGIN
    return sa.func.coalesce(from_mv, sa.case((fresh, live), else_=None))


async def _without_jit(db: AsyncSession) -> None:
    """JIT PostgreSQL для запросов таблицы выключен (замер боя 24.09).

    Сортировка по «Первому ответу» за 30 дней шла 16,4 с живым счётом и 10,9 с
    с ключом из витрины. Почти всё это время уходило на КОМПИЛЯЦИЮ: план с
    подзапросами по сорока девяти партициям `messages` давал 1 254 функции,
    оптимизация 6,7 с и генерация кода 5,1 с, само выполнение — четверть
    секунды. Без JIT те же запросы — 5,3 с и 0,23 с. Запросы таблицы короткие,
    и компиляция им не окупается никогда. `SET LOCAL` живёт до конца
    транзакции запроса и соседей не трогает.
    """
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(sa.text("SET LOCAL jit = off"))


def _order_by(
    db: AsyncSession,
    sort: str,
    direction: SortDir,
    *,
    mv_refreshed_at: datetime | None = None,
) -> sa.UnaryExpression[Any]:
    """Выражение порядка: дешёвая колонка или дорогая метрика.

    `nulls_last` в ОБЕ стороны, а не только в одну. «Ещё не ответили» — это не
    ноль и не бесконечность, это отсутствие величины: у него нет места в ряду
    длительностей. Отправь мы NULL в начало при возрастании — верх отчёта
    «самые быстрые ответы» заняли бы диалоги, где не ответили вовсе.
    """
    expr: InstrumentedAttribute[Any] | sa.ColumnElement[Any] = (
        SORTABLE[sort] if sort in SORTABLE else _first_response_sort_expr(db, mv_refreshed_at)
    )
    return expr.desc().nulls_last() if direction == "desc" else expr.asc().nulls_last()


async def query_table(
    db: AsyncSession,
    filters: TableFilters,
    *,
    sort: str = DEFAULT_SORT,
    direction: SortDir = "desc",
    limit: int = PAGE_LIMIT_DEFAULT,
    offset: int = 0,
    mv_refreshed_at: datetime | None = None,
) -> dict[str, Any]:
    """Страница таблицы: строки с метриками + общее число.

    Метрики считаются ТОЛЬКО для строк страницы — см. шапку модуля.
    """
    check_sort(sort)
    if offset > MAX_OFFSET:
        raise ApiError(
            "validation_error",
            "Слишком глубокая страница — уточните фильтр",
            status=400,
            details={"max_offset": MAX_OFFSET},
        )
    limit = max(1, min(limit, PAGE_LIMIT_MAX))
    return await _page(
        db,
        filters,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
        guard_metric_cost=True,
        mv_refreshed_at=mv_refreshed_at,
    )


async def _page(
    db: AsyncSession,
    filters: TableFilters,
    *,
    sort: str,
    direction: SortDir,
    limit: int,
    offset: int,
    guard_metric_cost: bool = False,
    mv_refreshed_at: datetime | None = None,
) -> dict[str, Any]:
    """Страница БЕЗ проверок сортировки и глубины.

    Проверки живут в :func:`query_table`, потому что они про интерфейс: человек
    не должен уметь улистать на четырёхсотую страницу. Выгрузка
    (:func:`export_csv`) идёт по той же выборке служебно и своими
    ограничениями — потолком строк, — так что запрет глубины ей мешал бы ровно
    там, где она делает свою работу.

    `guard_metric_cost` — потолок выборки для сортировки по метрике. Проверять
    его можно только ЗДЕСЬ: он сравнивается с `total`, а тот считается внутри.
    Выгрузка его выключает не по недосмотру: она идёт в воркере, где дорогой
    проход — секунды фоновой работы, а не ожидание человека у экрана. Её
    ограничивает свой потолок (:data:`EXPORT_MAX_ROWS`).
    """
    await _without_jit(db)
    total = (await db.execute(_select_narrowed(db, filters, sa.func.count()))).scalar_one()

    # С отметкой свежести витрины ключ сортировки берётся из неё, и цена прохода
    # перестаёт расти с выборкой (:func:`_first_response_sort_expr`) — потолок
    # стережёт только живой счёт.
    if (
        guard_metric_cost
        and sort in METRIC_SORTABLE
        and mv_refreshed_at is None
        and total > METRIC_SORT_MAX_ROWS
    ):
        raise ApiError(
            "validation_error",
            f"Сортировка по этой колонке считается по всей выборке, "
            f"а в ней {total} диалогов — сузьте период или фильтры",
            status=400,
            details={"max_rows": METRIC_SORT_MAX_ROWS, "total": total, "sort": sort},
        )

    order = _order_by(db, sort, direction, mv_refreshed_at=mv_refreshed_at)

    page_ids_stmt = (
        _select_narrowed(db, filters, Conversation.id)
        # id в хвосте — стабильный порядок: у диалогов одной секунды сортировка
        # иначе может отдать одну и ту же строку на двух страницах подряд.
        .order_by(order, Conversation.id)
        .limit(limit)
        .offset(offset)
    )
    page_ids = list((await db.execute(page_ids_stmt)).scalars())
    if not page_ids:
        return {"items": [], "page": {"limit": limit, "offset": offset, "total": total}}

    rows = await _rows_with_metrics(db, page_ids, order, direction)
    return {"items": rows, "page": {"limit": limit, "offset": offset, "total": total}}


# --------------------------------------------------------------- выгрузка ---
#
# Выгрузка — та же таблица, но целиком, а не страницей: её открывают в Excel и
# считают там своё. Поэтому она обязана отдавать РОВНО то, что человек видит на
# экране, — те же фильтры, та же сортировка, те же колонки. Выгрузка, которая
# «немного другая», хуже отсутствующей: расхождение заметят не сразу и решат,
# что врёт экран.

#: Потолок строк. Больше — отказ с просьбой сузить фильтр.
#:
#: Было 10 000, и это меньше любого готового периода: на бою «7 дней» —
#: 11 191 строка, «30 дней» — 44 052, «всё время» — 76 365 (проверка 24.09).
#: Файл теперь собирает воркер (`dialogs_export`), а не запрос с потолком nginx
#: в 60 секунд, поэтому потолок — тот же, что у листа «Диалоги» статистики.
EXPORT_MAX_ROWS = 100_000

#: Порция обхода. Метрики считаются постранично тем же кодом, что и на экране,
#: — так у выгрузки нет шанса посчитать их иначе. Размер привязан к потолку
#: страницы намеренно: своим числом он однажды разъехался бы с ним, и выгрузка
#: молча теряла бы хвост каждой порции.
EXPORT_CHUNK = PAGE_LIMIT_MAX

CSV_HEADER: tuple[str, ...] = (
    "Клиент",
    "Телефон",
    "Канал",
    "Статус",
    "Оператор",
    "Объявление",
    "Метки",
    "Сообщений",
    "Первый ответ, сек",
    "Длительность, сек",
    "Последнее сообщение",
)

#: Отчётность считается по Москве (06 §0.1) — выгрузка не исключение.
MSK = ZoneInfo("Europe/Moscow")

# Имя оставлено: на него ссылается лист «Диалоги» выгрузки статистики
# (`services/stats.py`) — там статус печатается ЭТИМ словарём с тех пор, как в
# отчёте владельца обнаружилась латиница. Содержимое теперь общее (docs/38 §0).
STATUS_RU = status_dict.LABELS


async def export_csv(
    db: AsyncSession,
    filters: TableFilters,
    *,
    sort: str = DEFAULT_SORT,
    direction: SortDir = "desc",
    mv_refreshed_at: datetime | None = None,
) -> str:
    """Вся выборка одним CSV для русского Excel: `;`, CRLF (как в 06 §5.2).

    Зовёт его воркер выгрузки (`dialogs_export`); BOM добавляется при записи
    файла. Текст собирается в памяти целиком: сто тысяч строк — это около
    двадцати мегабайт, воркеру это по силам, а один проход по выборке проще
    потока, который пришлось бы держать открытым вместе с сессией БД.
    """
    check_sort(sort)
    await _without_jit(db)
    total = (await db.execute(_select_narrowed(db, filters, sa.func.count()))).scalar_one()
    if total > EXPORT_MAX_ROWS:
        raise ApiError(
            "validation_error",
            f"Слишком много строк ({total}) — сузьте период или фильтры",
            status=400,
            details={"max_rows": EXPORT_MAX_ROWS, "total": total},
        )

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)

    # ПОРЯДОК ВЫБИРАЕТСЯ ОДИН РАЗ НА ВСЮ ВЫГРУЗКУ, а не заново на каждую порцию.
    #
    # Раньше выгрузка звала `_page` в цикле, и каждый вызов заново считал COUNT
    # по всей выборке и заново сортировал её целиком, чтобы отрезать сотню
    # строк. На дешёвой сортировке это было просто расточительно; с приходом
    # сортировки по метрике (см. шапку модуля) стало неприемлемо: порядок там
    # считается подзапросами по `messages` на каждую строку выборки, и сто
    # порций означали бы сто таких проходов — десятки секунд на файле, который
    # экран показывает мгновенно.
    #
    # Метрики по-прежнему считаются порциями: тяжёлое здесь — они, и держать в
    # одном запросе десять тысяч строк незачем.
    order = _order_by(db, sort, direction, mv_refreshed_at=mv_refreshed_at)
    ordered_ids = list(
        (
            await db.execute(
                _select_narrowed(db, filters, Conversation.id).order_by(order, Conversation.id)
            )
        ).scalars()
    )

    for start in range(0, len(ordered_ids), EXPORT_CHUNK):
        for r in await _rows_with_metrics(
            db, ordered_ids[start : start + EXPORT_CHUNK], order, direction
        ):
            cells = [
                r["client_name"] or "",
                r["client_phone"] or "",
                r["account_title"] or "",
                STATUS_RU.get(r["status"], r["status"]),
                r["assignee_name"] or "",
                r["item_title"] or "",
                ", ".join(r["tags"]),
                r["messages_count"],
                # Пустая клетка, а не ноль: ноль в Excel посчитается как
                # «ответили мгновенно» и занизит среднее по столбцу ровно
                # там, где клиента не обслужили вовсе.
                "" if r["first_response_sec"] is None else r["first_response_sec"],
                "" if r["duration_sec"] is None else r["duration_sec"],
                _iso_minutes(r["last_message_at"]),
            ]
            writer.writerow([csv_cell(cell) for cell in cells])

    return buf.getvalue()


def _iso_minutes(value: Any) -> str:
    """Дата для Excel — без секунд, по Москве, как вся отчётность (06 §0.1).

    Зона обязательна: в базе время лежит в UTC, и напечатанное как есть оно
    отличалось бы от экрана на несколько часов. Руководитель, у которого файл
    и экран показывают разное время одного и того же диалога, перестаёт верить
    обоим — а разобраться, кто из них прав, ему нечем.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        # SQLite отдаёт наивное время, но лежит там UTC — как и в PostgreSQL.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK).strftime("%Y-%m-%d %H:%M")


async def _rows_with_metrics(
    db: AsyncSession,
    ids: list[uuid.UUID],
    order: sa.UnaryExpression[Any],
    direction: SortDir,
) -> list[dict[str, Any]]:
    """Метрики для ПЯТИДЕСЯТИ строк, а не для всей выборки.

    На SQLite (юниты) коррелированные подзапросы работают так же, как на
    PostgreSQL, — отдельной ветки не нужно.
    """
    # Те же самые подзапросы, по которым строится и порядок сортировки
    # (:func:`_first_response_expr`). Одно определение на показ и на сортировку
    # — см. комментарий у самих функций.
    first_client = _first_client_at()
    first_operator = _first_operator_at()
    messages_count = (
        sa.select(sa.func.count())
        .select_from(sa.text("messages m"))
        .where(
            sa.text("m.conversation_id = conversations.id"),
            # Заметки и системные записи — не переписка с клиентом: считать их
            # «сообщениями диалога» значит завышать объём работы на пустом месте.
            sa.text("m.direction in ('in','out')"),
        )
        .scalar_subquery()
    )
    # Длительность переписки — от первого сообщения до последнего. Поля «когда
    # закрыли» в `conversations` нет, и выдумывать его ради колонки не стоит:
    # «сколько длилась переписка» отвечает на вопрос руководителя ровно так же.
    #
    # Границы берутся ИЗ САМИХ СООБЩЕНИЙ, а не из `conversations.last_message_at`.
    # Причина не в недоверии к колонке, а в согласованности: рядом в той же
    # строке стоят число сообщений и время первого ответа, посчитанные по
    # `direction in ('in','out')`. Возьми длительность из колонки — и она стала
    # бы считаться по другому множеству (заметка, поднявшая `last_message_at`,
    # удлинила бы «переписку», которой не было), а две метрики одной строки,
    # противоречащие друг другу, дискредитируют весь отчёт.
    first_any = (
        sa.select(sa.func.min(sa.text("m.created_at")))
        .select_from(sa.text("messages m"))
        .where(
            sa.text("m.conversation_id = conversations.id"),
            sa.text("m.direction in ('in','out')"),
        )
        .scalar_subquery()
    )
    last_any = (
        sa.select(sa.func.max(sa.text("m.created_at")))
        .select_from(sa.text("messages m"))
        .where(
            sa.text("m.conversation_id = conversations.id"),
            sa.text("m.direction in ('in','out')"),
        )
        .scalar_subquery()
    )

    stmt = (
        sa.select(
            Conversation,
            Client.name.label("client_name"),
            Client.phone.label("client_phone"),
            User.full_name.label("assignee_name"),
            User.department.label("assignee_department"),
            AvitoAccount.title.label("account_title"),
            first_client.label("first_client_at"),
            first_operator.label("first_operator_at"),
            first_any.label("first_any_at"),
            last_any.label("last_any_at"),
            messages_count.label("messages_count"),
        )
        .join(Client, Client.id == Conversation.client_id)
        .join(AvitoAccount, AvitoAccount.id == Conversation.account_id)
        .outerjoin(User, User.id == Conversation.assignee_id)
        .where(Conversation.id.in_(ids))
        .order_by(order, Conversation.id)
    )
    result = await db.execute(stmt)

    items: list[dict[str, Any]] = []
    for row in result:
        conv: Conversation = row[0]
        first_in = row.first_client_at
        first_out = row.first_operator_at
        items.append(
            {
                "id": str(conv.id),
                "status": conv.status,
                "client_name": row.client_name,
                "client_phone": row.client_phone,
                # Идентификатор ответственного рядом с именем — ради фильтра
                # «Оператор» на экране. Справочник `/users/assignable` отдаёт
                # тех, кому МОЖНО дать диалог, а в этой колонке стоят и те,
                # кому уже нельзя: уволенные, снятые с диалогов, роль head.
                # Экран добирает недостающих прямо из строк, и добирать их по
                # имени нельзя — два однофамильца слились бы в одну опцию.
                "assignee_id": str(conv.assignee_id) if conv.assignee_id else None,
                "assignee_name": row.assignee_name,
                # Отдел ответственного — подписью «(ОКК)» рядом с именем в
                # колонке «Оператор». В «Разборе» строк по сотне, а у
                # заказчика есть однофамильцы и просто похожие имена: без
                # отдела строка не отвечает на вопрос «чей это диалог».
                "assignee_department": normalize_department(row.assignee_department),
                "item_title": conv.item_title,
                "account_id": str(conv.account_id),
                "account_title": row.account_title,
                "tags": list(conv.tags or []),
                "bot_active": conv.bot_active,
                "unread_count": conv.unread_count,
                "last_message_at": conv.last_message_at,
                "messages_count": int(row.messages_count or 0),
                # Время первого ответа — то же определение, что в статистике
                # (06 §3): от первого сообщения клиента до первого ответа
                # оператора. `None` означает «ещё не ответили», и это не то же
                # самое, что ноль: ноль читался бы как «ответили мгновенно».
                "first_response_sec": _seconds_between(first_in, first_out),
                # Длительность переписки: от первого сообщения до последнего.
                # У диалога из одного сообщения это ноль — и ноль здесь честен,
                # в отличие от времени ответа: переписка действительно длилась
                # нисколько.
                "duration_sec": _seconds_between(row.first_any_at, row.last_any_at),
            }
        )
    return items


def _seconds_between(a: Any, b: Any) -> int | None:
    if a is None or b is None:
        return None
    if isinstance(a, str) or isinstance(b, str):
        # SQLite отдаёт даты строками — приводим, чтобы юниты считали то же,
        # что и прод.
        a = datetime.fromisoformat(str(a))
        b = datetime.fromisoformat(str(b))
    delta = (b - a).total_seconds()
    return int(delta) if delta >= 0 else None
